#!/usr/bin/env python3
"""
Smeltr -- local dashboard for the 4K HEVC re-encode pipeline.

Security posture (this serves real filesystem data, so it is deliberate).
Two modes, chosen by SMELTR_BIND (default 127.0.0.1):

  LOOPBACK (BIND=127.0.0.1): unreachable off-box. The token is optional
  (SMELTR_REQUIRE_TOKEN=1 to force it) -- the bind is the real control.

  LAN (BIND=lan, or a literal PRIVATE address): reachable from the network,
  so the token is FORCED on and IS the auth, and the two write endpoints are
  REFUSED for network peers unless SMELTR_LAN_WRITES=1 (this Mac's own loopback
  requests still write) -- a phone reading progress needs no ability to steer
  (or un-steer) the queue that deletes originals.

In both modes: Host header allowlisted (blocks DNS-rebinding); no CORS headers;
the client can never name a path (fixed file set); mutations go only through
core.save_overrides() (skip list, priority, per-title CRF) and also require a
custom X-Smeltr header a cross-origin page cannot attach; CSP is nonce-based with no external
origins; every value reaches the DOM via textContent. A LAN bind refuses any
non-private address and fails closed to loopback -- the token rides in the URL
in cleartext HTTP, which is acceptable on a home LAN and not on the internet.
"""
from __future__ import annotations

import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core
from dashboard import events as events_mod
from dashboard import sysmon

# Token persists across restarts in a 0600 file, so LAN devices survive the
# `./smeltr restart` that every dashboard edit needs; a fresh mint would 403
# every bookmarked phone and freeze its SSE stream permanently.
_TOKEN_FILE = os.path.join(core.SMELTR_DIR, "token")


def _load_or_mint_token() -> str:
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(core.SMELTR_DIR, exist_ok=True)
        fd = os.open(_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tok + "\n")
        os.chmod(_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return tok


TOKEN = _load_or_mint_token()


def _lan_ip() -> str | None:
    """This machine's primary LAN IPv4, via a connected UDP socket (sends no
    packet -- connect() on UDP only selects the route and local address)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


# The address families a home LAN actually uses. NOT ipaddress.is_private,
# which also returns True for the documentation/benchmarking/TEST-NET ranges
# (192.0.2/24, 198.51.100/24, 203.0.113/24, 198.18/15) -- a public-facing
# 203.0.113.x would sail through that check. Membership is explicit instead.
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


def _is_private(addr: str) -> bool:
    """True only for RFC1918 / RFC6598 (CGNAT) space. A VPN tunnel, a
    link-local autoconfig address, or a routable public IP is NOT 'the LAN'."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _LAN_NETS)


# Interfaces that are NOT "the LAN" even when they carry an RFC1918/CGNAT
# address: VPN/tunnel devices and the AWDL/link-local radios. Tailscale hands
# out 100.64/10, which _is_private deliberately accepts for real CGNAT homes --
# binding every private address WITHOUT this filter would newly expose the
# dashboard across a VPN that the old single-address bind never reached.
_TUNNEL_IFACES = ("lo", "utun", "tun", "tap", "ipsec", "ppp", "gif", "stf",
                  "awdl", "llw", "anpi", "bridge", "ap")


def _lan_ips() -> list[str]:
    """Every private LAN IPv4 configured on a real, UP interface.

    _lan_ip() alone returns only the DEFAULT-ROUTE address. A Mac with both
    Ethernet and Wi-Fi live on one subnet has two, and binding just the routed
    one left the dashboard dead on the other: a phone that resolved the host to
    the unbound address got connection-refused, which renders as a blank page.

    Parsed from ifconfig because getaddrinfo(gethostname()) reports only the
    primary on macOS (verified live: it returned 1 of this machine's 2).
    Ordered primary-first so the printed URL and the footer keep naming the
    routed address. Falls back to the single-address behaviour when ifconfig is
    missing or tells us nothing -- never widens on a parse failure.
    """
    found: list[str] = []
    try:
        out = subprocess.run(["ifconfig", "-a"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    iface, up = "", False
    for line in out.splitlines():
        head = re.match(r"^([A-Za-z0-9._-]+):\s.*<(.*)>", line)
        if head:
            iface = head.group(1)
            up = "UP" in head.group(2).split(",")
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and up and not iface.startswith(_TUNNEL_IFACES):
            addr = m.group(1)
            if _is_private(addr) and addr not in found:
                found.append(addr)
    primary = _lan_ip()
    if primary and _is_private(primary):
        found = [primary] + [a for a in found if a != primary]
    return found


# Where to listen. "127.0.0.1" (the default) is loopback-only; "lan" resolves
# to this machine's current private LAN IPv4; a literal address is used as-is.
# An empty/whitespace value is treated as unset -- SMELTR_BIND="" must NOT mean
# 0.0.0.0. A resolved or literal address that is not private fails CLOSED to
# loopback with a loud stderr line: the token crosses the wire in cleartext and
# must never guard a public listener.
_bind_req = (os.environ.get("SMELTR_BIND") or "127.0.0.1").strip() or "127.0.0.1"
if _bind_req == "lan":
    # EVERY private LAN IPv4 this machine holds, not just the routed one --
    # see _lan_ips(). The unbound sibling address is the blank-page bug.
    BINDS = _lan_ips()
    if not BINDS:
        cand = _lan_ip()
        why = ("no network" if not cand else f"{cand} is not a private LAN address")
        print(f"smeltr: SMELTR_BIND=lan -> {why}; binding loopback only",
              file=sys.stderr, flush=True)
        BINDS = ["127.0.0.1"]
elif _bind_req == "127.0.0.1":
    BINDS = ["127.0.0.1"]
elif _is_private(_bind_req):
    # An explicit address means exactly that address. Never widened: naming one
    # interface is how an operator deliberately narrows the exposure.
    BINDS = [_bind_req]
else:
    print(f"smeltr: SMELTR_BIND={_bind_req!r} is not a private LAN address; "
          "binding loopback only", file=sys.stderr, flush=True)
    BINDS = ["127.0.0.1"]
# The primary keeps naming the printed URL, the footer, and the log lines.
BIND = BINDS[0]
LAN_EXPOSED = BIND != "127.0.0.1"
# Sibling LAN addresses; each gets its own listener alongside the primary.
EXTRA_BINDS = [a for a in BINDS[1:] if a != "127.0.0.1"]

# The token is REQUIRED whenever the socket is reachable off-box, or when asked.
REQUIRE_TOKEN = os.environ.get("SMELTR_REQUIRE_TOKEN") == "1" or LAN_EXPOSED
# Write endpoints (skip / reorder) are gated PER REQUEST by peer address, not
# globally: a request arriving over loopback (you, on this Mac, via the aux
# 127.0.0.1 listener) may always write; a request from a real LAN peer is
# refused unless SMELTR_LAN_WRITES=1. Read-only is the safe default off-box --
# un-skipping a title puts an irreplaceable original back on the deletion path,
# and monitoring from a phone needs none of that.
LAN_WRITES = os.environ.get("SMELTR_LAN_WRITES") == "1"
# ...with ONE exception, added 2026-08-30 because the operator watches this on
# an iPad and could not stop the pipeline from it. Pause/resume is the only
# write whose worst case is the pipeline WAITING. It cannot delete, encode,
# reorder or stage anything: it creates or removes a flag file that makes
# next_title.py answer exit 3, and waiting is already the direction
# core.paused() fails towards. Everything else stays on this Mac -- un-skipping
# re-arms a ~90 GB deletion, and encode control spawns and kills HandBrake.
# The token still gates it, so this is "any device you have handed the URL to",
# not "anyone on the network".
# /api/driver/start joined it 2026-08-31 with the big play/pause toggle: the
# operator presses play from the iPad, and even this Mac's own browser reaches
# the page via the LAN URL, so a loopback-only play button is a dead button.
# Start's worst case is the pipeline running exactly as designed -- the same
# class as resume, which was already LAN-allowed -- and it deletes nothing the
# normal verified pipeline would not.
LAN_WRITE_ROUTES = ("/api/pause", "/api/driver/start")
NONCE = secrets.token_urlsafe(16)
POLL_SECONDS = 2.0
# Must be BELOW POLL_SECONDS. Above it, every second SSE frame was a
# byte-identical duplicate and the effective refresh halved to 4 s. The build
# lock -- not the TTL -- is what prevents concurrent work.
STATE_TTL = 1.5


# Host-header allowlist (defeats DNS rebinding). Loopback names always pass;
# when bound to the LAN, the bind address and this machine's own names join
# them so http://<ip>:8787 and http://<hostname>.local:8787 both work. Stored
# lowercased and dot-stripped to match _host_ok. Never a wildcard: an arbitrary
# Host is exactly what a rebinding attack sends.
def _allowed_hosts() -> frozenset:
    hosts = {"127.0.0.1", "localhost", "[::1]"}
    if LAN_EXPOSED:
        # Every address we listen on, not just the primary: a Host naming the
        # sibling interface is us, and 421-ing it renders as a blank page.
        hosts.update(a.lower() for a in BINDS)
        name = socket.gethostname().lower().strip(".")
        if name:
            short = name.split(".")[0]
            # Cover the FQDN, the bare name, and the search-domain forms LAN
            # clients actually send: .local (mDNS) and .lan (common router).
            hosts.update({name, short, short + ".local", short + ".lan"})
    return frozenset(h for h in hosts if h)


ALLOWED_HOSTS = _allowed_hosts()

_state_lock = threading.Lock()
_build_lock = threading.Lock()
_ov_lock = threading.Lock()
_state_cache = {"at": 0.0, "payload": None}


# Per-folder transfer telemetry across snapshots: last seen size, when it was
# seen, and when it last GREW. Lets the payload report a rate and call a
# non-growing .partial stalled instead of rendering it as live progress.
_xfer_track: dict = {}
# How long a measured rate may be re-reported with no growth observed. Longer
# than any SMB stat cadence seen live (a few seconds), far shorter than the
# 120 s stall line -- past this the caption keeps its byte counts and drops
# the rate/ETA rather than counting down from stale evidence.
RATE_HOLD_SECONDS = 60


def _transfers() -> list:
    """In-flight pushes back to the library, observed through the SMB mount.

    .ssh-xfer.sh stages every push as "<name>.partial" in the destination
    folder and renames it only on a byte-count match, so a growing .partial IS
    the transfer. The destination folder is resolved like .sync-to-library.sh
    resolves it -- exactly one <root>/<bucket>/<folder> match across all
    library roots -- never guessed from the title's first letter (the "#"
    bucket and article-sorted titles break that guess). An abandoned .partial
    from a crashed sync must render as stalled, never as a live transfer.
    Pure observation -- nothing here steers the sync.
    """
    rows = []
    now = time.monotonic()
    seen = set()
    for folder in core.staged_folders():
        stage = os.path.join(core.X9, folder)
        try:
            names = [n for n in os.listdir(stage)
                     if n.endswith(".mkv") and "2160p hevc" in n.lower()
                     and not n.startswith("._")]
        except OSError:
            continue
        # Two finished outputs in one folder is a state record.py refuses to
        # record; refuse to guess which of them is travelling.
        if len(names) != 1:
            continue
        name = names[0]
        try:
            total = os.path.getsize(os.path.join(stage, name))
        except OSError:
            continue
        dests = []
        for root in core.LIBRARY_ROOTS:
            try:
                buckets = [e.path for e in os.scandir(root) if e.is_dir()]
            except OSError:
                continue
            for b in buckets:
                d = os.path.join(b, folder)
                if os.path.isdir(d):
                    dests.append((root, d))
        if len(dests) != 1:
            continue
        root, dest = dests[0]
        part = os.path.join(dest, name + ".partial")
        try:
            done = os.path.getsize(part)
        except OSError:
            continue
        seen.add(folder)
        rec = _xfer_track.get(folder)
        if rec is None:
            rec = {"done": done, "t": now, "grew": now}
            _xfer_track[folder] = rec
        elif done > rec["done"]:
            dt = now - rec["t"]
            if dt > 0:
                # Mean over the gap since the last growth frame (SMB stat
                # caching makes many frames report no growth at all), lightly
                # smoothed so the ETA doesn't thrash between bursts. Weighted
                # by dt: a 2 s frame and a 40 s gap are not equal evidence
                # (equal weighting read up to 21% high in live sampling).
                inst = (done - rec["done"]) / dt
                prev = rec.get("rate")
                rec["rate"] = (inst if prev is None
                               else (prev * 20 + inst * dt) / (20 + dt))
            rec.update(done=done, t=now, grew=now)
        elif done < rec["done"]:
            # A smaller partial is a NEW attempt; restart tracking.
            rec.update(done=done, t=now, grew=now)
            rec.pop("rate", None)
        try:
            fresh_mtime = (time.time() - os.path.getmtime(part)) < 120
        except OSError:
            fresh_mtime = False
        stalled = (now - rec["grew"] > 120) and not fresh_mtime
        if total <= 0 or done > total:
            # done > total means the partial is from a DIFFERENT (older)
            # output than the one staged now -- a stale leftover, not progress.
            stalled = True
        pct = (round(done / total * 100, 1)
               if not stalled and total > 0 else None)
        # The held rate is reported on EVERY non-stalled frame, not only the
        # frames where growth was observed: a no-growth frame is a stat-cadence
        # artifact, and a rate/ETA caption that blinked in and out every few
        # seconds read as the page glitching. But the hold is BOUNDED by its
        # own age: a .partial whose mtime keeps refreshing while its size does
        # not never trips `stalled`, and a rate measured minutes ago republished
        # with a countdown that never counts down is a lie the old
        # report-on-growth-only code could not tell. 60 s covers every stat
        # cadence seen live; past it the caption drops rate/ETA and keeps the
        # byte counts, which are still facts.
        fresh_rate = (now - rec["grew"]) <= RATE_HOLD_SECONDS
        rate = rec.get("rate") if not stalled and fresh_rate else None
        rows.append({
            "title": folder,
            "nas": core.volume_name(root),
            "src_dir": dest,
            "done_bytes": done,
            "total_bytes": total,
            "pct": pct,
            "rate_bps": round(rate) if rate else None,
            "stalled": stalled,
        })
    for k in list(_xfer_track):
        if k not in seen:
            del _xfer_track[k]
    return rows


_arr_track: dict = {}


def _sync_in_flight() -> bool:
    """True while any `.syncing-<slug>` marker names a LIVE pid.

    Mirrors the driver's sync_in_flight(): the marker alone is not evidence
    (a stale one survives a crash); only a marker whose pid still answers
    signal 0 counts. Surfaced so the header beacon does not go dark during
    the sync's verify-and-delete of a library original — the one stretch of
    the cycle where "is anything happening?" matters most.
    """
    try:
        names = os.listdir(core.X9)
    except OSError:
        return False
    for n in names:
        if not n.startswith(".syncing-"):
            continue
        try:
            with open(os.path.join(core.X9, n), encoding="utf-8") as fh:
                pid = int(fh.read().strip() or "0")
        except (OSError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        return True
    return False


def _arrivals() -> dict:
    """Pulls still landing on the staging drive, keyed by folder (lowercased).

    Two shapes: a visible staged folder holding only the replenisher's growing
    .partial, and a hidden ".pull-<folder>" directory holding a dashboard pull
    (renamed into place only once complete, so the decision path never sees a
    half-arrived title). Growth is tracked exactly like _transfers(): a
    partial that stops growing for >120s reports stalled, never progress --
    a leftover partial is not proof of life.
    """
    found = {}
    for folder in core.staged_folders():
        stage = os.path.join(core.X9, folder)
        try:
            names = [n for n in os.listdir(stage) if not n.startswith("._")]
        except OSError:
            continue
        partials = [n for n in names if n.endswith(".partial")]
        full = [n for n in names if n.endswith((".mkv", ".mp4", ".m2ts"))]
        if partials and not full:
            found[folder.lower()] = os.path.join(stage, partials[0])
    try:
        hidden = [n for n in os.listdir(core.X9)
                  if n.startswith(".pull-")
                  and os.path.isdir(os.path.join(core.X9, n))]
    except OSError:
        hidden = []
    for n in hidden:
        d = os.path.join(core.X9, n)
        try:
            names = [m for m in os.listdir(d) if not m.startswith("._")]
        except OSError:
            continue
        if names:
            found.setdefault(n[len(".pull-"):].lower(),
                             os.path.join(d, names[0]))
    out = {}
    now = time.monotonic()
    for key, path in found.items():
        try:
            done = os.path.getsize(path)
        except OSError:
            continue
        rec = _arr_track.get(key)
        rate = None
        if rec is None:
            rec = {"done": done, "t": now, "grew": now}
            _arr_track[key] = rec
        elif done > rec["done"]:
            dt = now - rec["t"]
            if dt > 0:
                rate = (done - rec["done"]) / dt
            rec.update(done=done, t=now, grew=now)
        elif done < rec["done"]:
            # A smaller partial is a NEW attempt; restart tracking.
            rec.update(done=done, t=now, grew=now)
        try:
            fresh = (time.time() - os.path.getmtime(path)) < 120
        except OSError:
            fresh = False
        out[key] = {"done": done,
                    "stalled": (now - rec["grew"] > 120) and not fresh,
                    "rate": round(rate) if rate else None}
    for k in list(_arr_track):
        if k not in out:
            del _arr_track[k]
    return out


def _arr_fields(a) -> dict:
    return {"arriving_bytes": a["done"] if a else None,
            "arriving_stalled": bool(a and a["stalled"]),
            "arriving_rate_bps": a["rate"] if a else None}


def _src_dirs() -> dict:
    """Folder basename (lowercased) -> library directory of the original.

    Read from the bitrate index, the same source queue() ranks from. A basename
    that maps to two distinct directories maps to None: the cell must show an
    honest dash, never one of two possible paths.
    """
    dirs: dict = {}
    for rec in core.load_index():
        path = rec.get("path", "")
        if not path or "2160p hevc" in os.path.basename(path).lower():
            continue
        d = os.path.dirname(path)
        key = os.path.basename(d).lower()
        if key in dirs and dirs[key] != d:
            dirs[key] = None
        else:
            dirs[key] = d
    return dirs


def build_state() -> dict:
    """Snapshot of everything the UI renders, cached briefly.

    Recomputing hits ps(1) and stats a few hundred files; with several browser
    tabs and SSE streams open that would otherwise run many times a second.
    """
    with _state_lock:
        cached = _state_cache["payload"]
        if cached is not None and time.monotonic() - _state_cache["at"] < STATE_TTL:
            return cached

    # One build at a time. Without this, every open tab and SSE stream starts
    # its own concurrent build against the same drive HandBrake is writing to.
    with _build_lock:
        with _state_lock:
            cached = _state_cache["payload"]
            if cached is not None and time.monotonic() - _state_cache["at"] < STATE_TTL:
                return cached
        # Compute each source ONCE and thread it through. summary() calls
        # queue(), which calls live_encodes() and ledger(); recomputing them
        # here too meant ~6 log parses and several hundred stat() calls per
        # snapshot, on the drive the encode is writing to.
        hist = core.ledger()
        live = core.live_encodes()
        q = core.queue_cached(live=live, hist=hist)
        # Copy rows before annotating: queue_cached hands back shared cached
        # dicts, and these decorations are a dashboard concern only.
        sd = _src_dirs()
        arr = _arrivals()
        q = [dict(r, src_dir=sd.get(r["title"].lower()),
                  **_arr_fields(arr.get(r["title"].lower()))) for r in q]
        # summary() is the ONE carrier of paused -- the same field report.py
        # banners -- and it feeds _mark_ready so "ready" and the paused banner
        # can never come from two reads that disagree within one snapshot.
        summary = core.summary(hist=hist, q=q, live=live)
        _mark_ready(q, live, summary["paused"])
        # Pending dashboard pulls, annotated onto the rows they belong to. The
        # rows are already private copies (see above), so this is safe.
        with _stage_lock:
            pending = list(_stage_queue)
            staging_now = _stage_active["title"]
            waiting = _stage_wait["why"]
        at = {t.lower(): i + 1 for i, t in enumerate(pending)}
        for r in q:
            n = at.get(r["title"].lower())
            if n is None:
                continue
            r["stage_queued"] = n
            # Only the head can be the one being held up; saying it on every
            # queued row would read as five separate problems.
            if n == 1 and waiting:
                r["stage_wait"] = waiting
        # Snapshot the note under the lock: a torn read could pair a failure
        # message with the previous note's "ok" kind.
        with _state_lock:
            note = {"msg": _encode_note["msg"], "kind": _encode_note["kind"]}
        # One pgrep serves both fields: can_start and driver_alive read the
        # same processes, and two calls could disagree within one payload.
        driver = bool(_driver_pids())
        payload = {
            "summary": summary,
            "live": live,
            "ledger": hist,
            "queue": q,
            "transfers": _transfers(),
            "syncing": _sync_in_flight(),
            "encode_note": note,
            "crf_choices": list(CRF_CHOICES),
            # The picker's "auto" option is labelled with this, so a moved
            # default renames the option instead of quietly meaning something
            # else on rows nobody has touched.
            "crf_default": core.CRF_DEFAULT,
            "stage_queue": pending,
            "stage_active": staging_now,
            "can_start": not live and not driver,
            # The paused card asserts what the driver will do; it may only
            # do that when a driver actually exists to do it.
            "driver_alive": driver,
            # Change marker only — the timeline itself is /api/events, so the
            # 2 s SSE frames stay small and the page refetches only when a
            # log actually moved.
            "events_rev": events_mod.rev(),
        }
        with _state_lock:
            # Stamp AFTER the build. Stamping before meant a slow cold build was
            # already older than the TTL the instant it was stored, so the cache
            # never hit.
            _state_cache["at"] = time.monotonic()
            _state_cache["payload"] = payload
    return payload


# ------------------------------------------------------------ encode control
# THIS is the one part of Smeltr that does not merely observe: it spawns and
# kills HandBrake. Everything here is kept deliberately parallel to
# .autopilot.sh's start_encode() -- same flags, same track-parity gate, same
# handoff to .watch-encode.sh -- because the two are now separate
# implementations of one procedure and will drift if they stop matching.
#
# What it deliberately does NOT do: judge, record, sync, or delete anything in
# the library. A Smeltr-started encode produces a file on the staging drive and
# stops. The driver is still the only thing that can delete an original.
# The menu is core's, not ours: the DRIVER now honours a hand-picked CRF (see
# core.planned_crf / `smeltr crf`), so a value offered here that core rejects
# would be accepted by the page and silently dropped on the way to HandBrake.
CRF_CHOICES = core.CRF_CHOICES
_encode_lock = threading.Lock()
# Surfaced in the state payload and rendered as a banner. The parity gate and
# the abort both run in background threads, long after their POST returned 200,
# so this is the only channel they have back to the page.
_encode_note: dict = {"msg": None, "kind": None, "at": 0.0}


def _set_note(msg, kind="warn") -> None:
    """One banner slot shared by encode and stage control. A "bad" note (a
    killed encode, a failed pull) is often the ONLY record of the event, so an
    "ok" note may not paper over it for 15 minutes; warn/bad always write.
    """
    with _state_lock:
        if kind == "ok" and _encode_note["kind"] == "bad" \
                and time.monotonic() - _encode_note["at"] < 900:
            return
        _encode_note["msg"], _encode_note["kind"] = msg, kind
        _encode_note["at"] = time.monotonic()
        _state_cache["payload"] = None


def _mark_ready(rows: list, live: list, paused: bool) -> None:
    """Flag the ONE row the pipeline would actually encode next.

    The pick IS core.pick_next -- the same call next_title.py makes -- so the
    dashboard's green row and the driver's choice cannot disagree. It is NOT
    "row 1": the queue lists library titles that are not on the staging
    drive, so the top row is frequently a title the pick skips straight past,
    and painting that one green would promise an encode that cannot start.

    Two flags from the one pick. "next_up" is set ALWAYS -- including while an
    encode runs (the live row's own output file excludes it from the pick) and
    while paused (the pill says "next after resume") -- because "which movie
    encodes next" must be readable at a glance, not inferred from rank.
    "ready" additionally requires that nothing is encoding AND that the
    pipeline is not paused: it carries the green row and the start button,
    and must never promise an encode that cannot start. Gated HERE, not in
    the page -- ready is server-computed, whole, or the invariant leaks.
    """
    for r in rows:
        r["ready"] = False
        r["next_up"] = False
    # pick_next reads only the folder's own contents; a dashboard pull into a
    # hidden .pull-<title> dir is invisible to it but keyed onto this row by
    # _arrivals(). A row still arriving must never carry the green row or the
    # start button -- /api/encode/start refuses it with a 409, and a promise
    # the click cannot keep is the exact bug ready exists to prevent.
    pick, _ = core.pick_next(
        [r for r in rows if r.get("arriving_bytes") is None])
    if pick is not None:
        pick["next_up"] = True
        if not live and not paused:
            pick["ready"] = True


def _slug_of(title: str) -> str:
    """Byte-identical to .autopilot.sh slug_of(): lowercase, alnum only, 20."""
    return re.sub(r"[^a-z0-9]", "", title.lower())[:20]


def _pgrep(pattern: str) -> list:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.stdout.split() if x.isdigit()]


def _driver_pids() -> list:
    return _pgrep(r"autopilot\.sh")


def _staging_files(title: str):
    """(folder, source mkv, finished-or-partial HEVC output) for a staged title."""
    d = os.path.join(core.X9, title)
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("._"))
    except OSError:
        return None, None, None
    src = out = None
    for n in names:
        if not n.endswith(".mkv"):
            continue
        if "2160p HEVC" in n:
            out = out or os.path.join(d, n)
        elif src is None:
            src = os.path.join(d, n)
    return d, src, out


def _track_counts(path: str):
    """(audio, subtitle) stream counts from ffprobe, or (None, None)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", path],
                             capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    return (out.count("codec_type=audio"), out.count("codec_type=subtitle"))


def _parity_gate(proc, src: str, out: str, log: str, title: str,
                 slug: str, crf: int) -> None:
    """Verify HandBrake is writing every track, then hand off to the watcher.

    A multi-hour encode that silently dropped audio or subtitles is a
    multi-hour waste, and the whole point of this job is that every track
    survives. Runs on a thread: it waits up to 120s, which no request handler
    can afford to block on.
    """
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                if "job configuration:" in fh.read():
                    break
        except OSError:
            pass
        time.sleep(1)
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read().replace("\r", "\n")
    except OSError:
        text = ""
    oa = len(re.findall(r"^\[[0-9:]+\]\s+\* audio track", text, re.M))
    os_ = len(re.findall(r"^\[[0-9:]+\]\s+\* subtitle track", text, re.M))
    sa, ss = _track_counts(src)
    if sa is None:
        _set_note("Started %s, but ffprobe could not read the source to verify "
                  "track parity. Encode is RUNNING and UNVERIFIED." % title)
        return
    if (oa, os_) != (sa, ss):
        _kill(proc.pid)
        try:
            os.remove(out)
        except OSError:
            pass
        _set_note("Refused %s: source has %da/%ds but the job writes %da/%ds. "
                  "Encode killed and the partial deleted."
                  % (title, sa, ss, oa, os_), kind="bad")
        return
    watch_log = os.path.join(core.X9, ".watch-%s.log" % slug)
    try:
        # Truncate: a leftover KILLED| line from an earlier attempt is what the
        # driver's ladder reads, and it would ladder against the wrong run.
        open(watch_log, "w").close()
        with open(watch_log, "ab") as wf:
            subprocess.Popen(
                ["bash", os.path.join(core.X9, ".watch-encode.sh"), slug, title,
                 os.path.basename(src), os.path.basename(out), str(proc.pid),
                 str(crf)],
                stdout=wf, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        _set_note("%s is encoding (%da/%ds verified) but the progress watcher "
                  "failed to start (%s) — no CRF ladder or auto-kill on this "
                  "run." % (title, sa, ss, e))
        return
    _set_note("%s encoding at CRF %d — %d audio, %d subtitle tracks verified."
              % (title, crf, sa, ss), kind="ok")


def _kill(pid: int) -> None:
    """SIGTERM, 30s grace, then SIGKILL. Mirrors .watch-encode.sh's auto-kill."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(60):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _abort_worker(pid: int, title: str, out) -> None:
    """Kill the encode, delete its partial, and skip the title.

    The partial MUST go: sync and record both key off "2160p HEVC", so a
    half-written file left behind is indistinguishable from a finished encode
    to the next disk scan. And the skip MUST be written, or a running driver
    sees no HandBrake, asks next_title.py, gets this same title back, and
    restarts it within 30 seconds.
    """
    _kill(pid)
    freed = None
    if out and os.path.isfile(out):
        try:
            freed = os.path.getsize(out)
            os.remove(out)
        except OSError as e:
            _set_note("Killed %s but could NOT delete its partial (%s). Delete "
                      "it by hand before the driver runs — it will be mistaken "
                      "for a finished encode." % (title, e), kind="bad")
            return
    with _ov_lock:
        ov = core.load_overrides()
        skip = [t for t in ov["skip"] if t.lower() != title.lower()]
        pri = [t for t in ov["priority"] if t.lower() != title.lower()]
        skip.append(title)
        try:
            core.save_overrides(skip, pri)
        except OSError as e:
            _set_note("Killed %s and deleted its partial, but could not write "
                      "the skip (%s) — a running driver may restart it."
                      % (title, e), kind="bad")
            return
    _set_note("Aborted %s — partial deleted%s, title skipped so it is not "
              "picked up again. Restore it from the queue to re-arm."
              % (title, "" if freed is None else
                 " (%.2f GiB discarded)" % (freed / 1073741824.0)), kind="ok")


# ------------------------------------------------------------- stage control
# The third thing server.py can DO (after queue overrides and encode control):
# pull one library title onto the staging drive on demand. It deliberately
# mirrors the per-title pull in .replenish-queue.sh -- same .ssh-xfer.sh pull,
# same free-space margin, same delete-the-folder cleanup on failure -- because
# the two are separate implementations of one procedure and must not drift.
# Progress needs no plumbing of its own: the pull lands as "<name>.partial",
# which _arrivals() already reports and the queue row already draws as a bar.
# Still inside the sanctioned boundary: this steers which title is staged,
# never the verdict, sync, or a deletion.
_stage_lock = threading.Lock()
# One dashboard-initiated pull at a time; the title travelling right now.
_stage_active: dict = {"title": None}
# Titles waiting their turn on the wire, in click order. IN MEMORY ONLY: a
# server restart drops the list and the rows simply offer "stage" again,
# which is the honest failure. A persisted plan would outlive the code change
# that prompted the restart and could dispatch against stale assumptions.
_stage_queue: list = []
# Why the head of the queue is not moving, in the row's own words, or None.
# Set by the pump, rendered on the queued row -- a queue that silently sits
# still is indistinguishable from one that is broken.
_stage_wait: dict = {"why": None}
_stage_pump: dict = {"on": False}
# The pump only pgreps and statvfs's; it is cheap enough to tick often and
# slow enough not to matter beside a multi-GiB transfer.
STAGE_PUMP_SECONDS = 20.0


def _drop_state_cache() -> None:
    with _state_lock:
        _state_cache["payload"] = None


def _wire_busy_locked():
    """Reason a pull cannot start RIGHT NOW, or None. Caller holds the lock.

    All three conditions are transient by nature, so the queue holds against
    them rather than dropping work. Liveness is pgrep, not the lock file: the
    documented pause procedure kills orphans with -9 and leaves mkdir locks
    behind, and a lock with no process starves the replenisher too -- so that
    case is called out by name with the command to fix it.
    """
    if _stage_active["title"]:
        return "waiting — %s is on the wire" % _stage_active["title"]
    if _pgrep(r"ssh-xfer\.sh pull"):
        return "waiting — another pull owns the wire"
    if os.path.isdir(os.path.join(core.X9, ".replenish.lock")):
        if _pgrep(r"replenish-queue\.sh"):
            return "waiting — the replenisher is staging"
        return "waiting — a STALE .replenish.lock is blocking the replenisher"
    return None


def _stage_candidate(title: str, rows: list):
    """Resolve a title to (row, src, size). Returns (row, src, size, err, hold).

    `hold` distinguishes "not yet" from "never": a hold keeps the title in the
    queue and retries, an err without hold drops it. Used by BOTH the enqueue
    POST and the pump, so a click is refused for the same reasons a dispatch
    is -- one implementation, no drift between the two moments.
    """
    matches = [r for r in rows if r["title"].lower() == title.lower()]
    if not matches:
        return None, None, None, "it is no longer in the queue", False
    if len(matches) > 1:
        return None, None, None, ("two queue rows share this folder name; "
                                  "refusing to act on both"), False
    row = matches[0]
    if row.get("skipped"):
        return None, None, None, "it is skipped — restore it first", False
    # Arriving before staged: "already there" is the wrong message for a file
    # that is mostly missing.
    if row.get("arriving_bytes") is not None:
        pct = (" (%.0f%% pulled)"
               % (row["arriving_bytes"] / row["bytes"] * 100)
               if row.get("bytes") else "")
        return None, None, None, ("it is already being copied to the staging "
                                  "drive%s" % pct), False
    if row.get("staged"):
        return None, None, None, "it is already on the staging drive", False
    srcs = set()
    for rec in core.load_index():
        p = rec.get("path", "")
        if not p or "2160p hevc" in os.path.basename(p).lower():
            continue
        if os.path.basename(os.path.dirname(p)).lower() == row["title"].lower():
            srcs.add(p)
    if not srcs:
        return None, None, None, "its file is not in the bitrate index", False
    if len(srcs) > 1:
        return None, None, None, ("the index lists more than one source file "
                                  "for this title; refusing to pick one"), False
    src = srcs.pop()
    # An unreachable NAS is a HOLD, not a drop: the share remounts, and a
    # queue that empties itself during a blip is worse than one that waits.
    if not os.path.isfile(src):
        return None, None, None, "waiting — the library file is unreachable", True
    try:
        size = os.path.getsize(src)
    except OSError as e:
        return None, None, None, "waiting — cannot stat the library file (%s)" % e, True
    return row, src, size, None, False


def _space_hold(size: int, rows: list):
    """Free-space gate, same margin as .replenish-queue.sh, or None.

    The message deliberately carries NO volatile number: `need` is stable for
    a given title, but free space moves every second as the encode writes, and
    a reason string that changes every frame would rebuild the table on every
    frame. The measured figure goes to the banner note instead, once.
    """
    try:
        st = os.statvfs(core.X9)
        avail = st.f_bavail * st.f_frsize
    except OSError:
        return "waiting — cannot read free space on the staging drive", None
    # PLUS the bytes other in-flight pulls have promised but not yet written:
    # statvfs only counts what has already landed.
    inbound = sum(max(0, (r.get("bytes") or 0) - r["arriving_bytes"])
                  for r in rows if r.get("arriving_bytes") is not None)
    need = size + inbound + 10 * 1024 ** 3
    if avail < need:
        return ("waiting for room — needs %.0f GiB free"
                % (need / 1073741824.0)), avail
    return None, avail


def _begin_pull_locked(row: dict, src: str, size: int):
    """Create the hidden folder and start the worker. Caller holds the lock."""
    # The pull lands HIDDEN (dot-prefixed, so invisible to next_title.py,
    # core.staged_folders(), and the replenisher's find) and is renamed into
    # place only once the byte count checks out. A visible folder with no
    # source file HALTS the driver.
    hidden = os.path.join(core.X9, ".pull-" + row["title"])
    try:
        os.makedirs(hidden, exist_ok=True)
    except OSError as e:
        return "could not create the pull folder: %s" % e
    _stage_active["title"] = row["title"]
    try:
        threading.Thread(target=_stage_worker,
                         args=(row["title"], src, hidden),
                         daemon=True).start()
    except RuntimeError as e:
        _stage_active["title"] = None
        shutil.rmtree(hidden, ignore_errors=True)
        return "could not start the pull thread: %s" % e
    _set_note("Staging %s — pulling %.2f GiB from the library over SSH."
              % (row["title"], size / 1073741824.0), kind="ok")
    return None


def _pump_once_locked(rows: list) -> None:
    """Try to start the head of the queue. Caller holds _stage_lock.

    `rows` is a queue snapshot the CALLER built outside the lock -- build_state
    takes _stage_lock itself to report the queue, so building it in here would
    deadlock the dispatcher against the page.

    EVERY gate is re-applied here, at dispatch, because a check made when the
    button was clicked can be hours stale by the time the wire frees up --
    the replenisher may have staged the title meanwhile, the drive may have
    filled, the NAS may have gone. A transient refusal holds the head and
    says so on the row; a permanent one drops that ONE title and moves on, so
    a single dead title cannot wedge the whole queue.
    """
    def hold(why):
        if _stage_wait["why"] != why:
            _stage_wait["why"] = why
        return

    def drop(title, why, kind):
        _stage_queue.pop(0)
        _stage_wait["why"] = None
        _set_note("Dropped the queued pull of %s — %s." % (title, why),
                  kind=kind)

    title = _stage_queue[0]
    busy = _wire_busy_locked()
    if busy:
        return hold(busy)
    row, src, size, err, held = _stage_candidate(title, rows)
    if err:
        if held:
            return hold(err)
        # "already staged" is a success, not a failure: the replenisher got
        # there first and the title is exactly where the click wanted it.
        done = "already on the staging drive" in err or "already being copied" in err
        return drop(title, err, "ok" if done else "bad")
    why, avail = _space_hold(size, rows)
    if why:
        if _stage_wait["why"] != why and avail is not None:
            _set_note("Pull queue is waiting for room on the staging drive: "
                      "%s needs %.0f GiB free, %.0f GiB available."
                      % (title, (size + 10 * 1024 ** 3) / 1073741824.0,
                         avail / 1073741824.0), kind="warn")
        return hold(why)
    err = _begin_pull_locked(row, src, size)
    if err:
        return drop(title, err, "bad")
    _stage_queue.pop(0)
    _stage_wait["why"] = None


def _stage_pump_loop() -> None:
    """One dispatcher for the whole queue; exits when the queue drains."""
    try:
        while True:
            with _stage_lock:
                if not _stage_queue:
                    _stage_wait["why"] = None
                    return
                idle = _stage_active["title"] is None
            # OUTSIDE the lock: build_state() takes _stage_lock to report the
            # queue, so holding it here would deadlock the pump against every
            # open page. A slightly stale snapshot is fine -- it only chooses
            # whether to try; _begin_pull_locked is what commits.
            rows = build_state()["queue"] if idle else []
            with _stage_lock:
                before = (_stage_wait["why"], len(_stage_queue),
                          _stage_active["title"])
                if idle and _stage_queue and _stage_active["title"] is None:
                    _pump_once_locked(rows)
                changed = before != (_stage_wait["why"], len(_stage_queue),
                                     _stage_active["title"])
            if changed:
                _drop_state_cache()
            time.sleep(STAGE_PUMP_SECONDS)
    finally:
        with _stage_lock:
            _stage_pump["on"] = False
        _drop_state_cache()


def _ensure_pump_locked() -> None:
    """Start the dispatcher if it is not already running. Caller holds lock."""
    if _stage_pump["on"]:
        return
    _stage_pump["on"] = True
    try:
        threading.Thread(target=_stage_pump_loop, daemon=True).start()
    except RuntimeError:
        _stage_pump["on"] = False
        raise


def _stage_worker(title: str, src: str, hidden: str) -> None:
    """Run the pull into the hidden folder, then move it into place.

    The rename is the commit. Until .ssh-xfer.sh's byte-count check passes,
    nothing outside ".pull-<title>" exists, so next_title.py, the
    replenisher's folder count, and the queue's staged flag never see a
    half-arrived title -- a VISIBLE folder without a source .mkv is exactly
    the state that halts the driver ("no source file", exit 2). A leftover
    from a crashed server stays hidden too: it can only ever render as a
    stalled arrival, never as an encodable folder -- until the next server
    start, when _adopt_orphan_pulls() finishes what this thread could not.
    """
    slug = _slug_of(title)
    log = os.path.join(core.X9, ".pull-%s.log" % slug)
    xfer = os.path.join(core.X9, ".ssh-xfer.sh")
    keep = False
    try:
        rc = None
        try:
            with open(log, "wb") as fh:
                proc = subprocess.Popen(
                    ["/bin/bash", xfer, "pull", src, hidden],
                    stdout=fh, stderr=subprocess.STDOUT,
                    start_new_session=True)
            try:
                rc = proc.wait(timeout=6 * 3600)
            except subprocess.TimeoutExpired:
                # Kill the whole group: bash's ssh child keeps the partial's
                # fd open otherwise, and deleting a folder around a live
                # writer leaks the bytes into an unlinked inode df still
                # counts but no listing shows.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                _set_note("Staging %s gave up after 6 hours — killed the pull;"
                          " removing the folder." % title, kind="bad")
        except OSError as e:
            _set_note("Staging %s FAILED before the transfer started (%s)."
                      % (title, e), kind="bad")
        if rc == 0:
            dest = os.path.join(core.X9, title)
            try:
                os.rename(hidden, dest)
                keep = True
                _set_note("Staged %s — a copy. The library original is "
                          "untouched now, and is deleted only when a good "
                          "encode of it syncs back." % title, kind="ok")
            except OSError as e:
                # Never delete a completed 70 GB pull over a rename problem.
                keep = True
                _set_note("Pulled %s but could NOT move it into place (%s) — "
                          "the complete file is in %s; move it by hand."
                          % (title, e, os.path.basename(hidden)), kind="bad")
        elif rc is not None:
            _set_note("Staging %s FAILED — see %s. Removing the folder."
                      % (title, os.path.basename(log)), kind="bad")
        if not keep:
            # Delete only inside the staging drive, however hidden was built.
            real = os.path.realpath(hidden)
            if real.startswith(os.path.realpath(core.X9) + os.sep):
                shutil.rmtree(real, ignore_errors=True)
    finally:
        with _stage_lock:
            _stage_active["title"] = None
        with _state_lock:
            _state_cache["payload"] = None


def _finish_orphan_locked(title: str, hidden: str) -> None:
    """Commit or clean up ONE orphaned pull folder. Caller holds _stage_lock.

    The commit evidence is the folder's contents, not a return code: the
    .ssh-xfer.sh script renames "<name>.partial" to its final name only on a
    byte-count match, so a final .mkv with no .partial beside it IS the
    completed, verified pull. Anything else is a dead half-pull and gets the
    same delete-the-folder cleanup as the worker's failure path.
    """
    try:
        files = os.listdir(hidden)
    except OSError:
        return
    real = [f for f in files if not f.startswith("._")]
    complete = (any(f.endswith(".mkv") for f in real)
                and not any(f.endswith(".partial") for f in real))
    if complete:
        dest = os.path.join(core.X9, title)
        try:
            if os.path.exists(dest):
                raise OSError("a folder named %s already exists" % title)
            os.rename(hidden, dest)
            _set_note("Staged %s — finished a pull orphaned by a server "
                      "restart. The library original is untouched, and is "
                      "deleted only when a good encode of it syncs back."
                      % title, kind="ok")
        except OSError as e:
            # Never delete a completed 70 GB pull over a rename problem.
            _set_note("Found the completed orphaned pull of %s but could "
                      "NOT move it into place (%s) — the file is in %s; "
                      "move it by hand."
                      % (title, e, os.path.basename(hidden)), kind="bad")
        return
    # Delete only inside the staging drive, however hidden was built.
    realpath = os.path.realpath(hidden)
    if realpath.startswith(os.path.realpath(core.X9) + os.sep):
        shutil.rmtree(realpath, ignore_errors=True)
    _set_note("Removed the half-finished pull of %s left behind by a server "
              "restart — stage it again." % title, kind="bad")


def _sweep_orphans_once() -> bool:
    """Finish pulls orphaned by a server restart. True when nothing is left.

    The pull child is start_new_session'd, so it survives a server restart --
    but the _stage_worker thread that waits on it and does the commit rename
    dies with the old process. On 2026-08-31 that stranded a complete,
    verified 61 GB pull as a stalled arrival until a human renamed it. This
    sweep applies the commit-or-cleanup the dead worker would have.

    False means "come back later": a puller is still alive (the orphaned
    transfer itself, or the replenisher's -- pgrep cannot tell them apart and
    does not need to), or a worker in THIS process owns a hidden folder
    (_stage_active covers the pre-spawn and post-exit windows pgrep misses).
    """
    try:
        names = os.listdir(core.X9)
    except OSError:
        # No staging drive to sweep. The next restart with it mounted adopts
        # whatever is there; retrying here would tick forever on a machine
        # that simply has no X9.
        return True
    orphans = [n for n in names if n.startswith(".pull-")
               and os.path.isdir(os.path.join(core.X9, n))]
    if not orphans:
        return True
    with _stage_lock:
        if _stage_active["title"] is not None:
            return False
        if _pgrep(r"ssh-xfer\.sh pull"):
            return False
        for n in orphans:
            _finish_orphan_locked(n[len(".pull-"):], os.path.join(core.X9, n))
    _drop_state_cache()
    return True


def _adopt_orphan_pulls() -> None:
    """Startup: finish pulls a previous server run left behind, off-thread.

    Off the startup path because the cleanup half can rmtree tens of GB. The
    loop keeps ticking while an orphan's transfer is still running -- the
    child survived the restart -- and commits once it exits.
    """
    try:
        if not any(n.startswith(".pull-")
                   and os.path.isdir(os.path.join(core.X9, n))
                   for n in os.listdir(core.X9)):
            return
    except OSError:
        return

    def loop():
        while not _sweep_orphans_once():
            time.sleep(STAGE_PUMP_SECONDS)

    threading.Thread(target=loop, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "Smeltr"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # A LAN-reachable socket must not let an idle or slow-loris peer pin a
    # handler thread forever before auth. StreamRequestHandler honours this on
    # the connection's reads, so a stalled request headers/body times out.
    timeout = 15

    # -------------------------------------------------------------- security
    def _host_ok(self) -> bool:
        # An IPv6 Host is bracketed and contains its own colons, so neither
        # split(":")[0] nor a colon count can strip the port correctly.
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False  # HTTP/1.1 requires a Host; absent one can't match
        if raw.startswith("["):
            host = raw[:raw.index("]") + 1] if "]" in raw else raw
        else:
            host = raw.rsplit(":", 1)[0] if ":" in raw else raw
        # Case-insensitive, and a trailing-dot FQDN (mr-meeseeks.local.) is the
        # same host as without it -- some resolvers append the root label.
        return host.lower().rstrip(".") in ALLOWED_HOSTS

    def _peer_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except (ValueError, IndexError):
            return False

    def _writes_ok(self, route: str = "") -> bool:
        # Loopback peers always; LAN peers only with the explicit opt-in, or
        # for the one route in LAN_WRITE_ROUTES.
        # A connection whose SOURCE address equals the listener's own local
        # address is this Mac talking to itself -- the operator loaded the
        # page via the LAN URL in a local browser. Same keyboard, so it
        # writes. Compare against the live socket, never a cached address
        # list: a snapshot goes stale when an interface drops and DHCP hands
        # its address to another device, which would inherit write access.
        # A remote peer cannot spoof this over TCP -- the SYN-ACK would
        # route back to us, not to it.
        if (LAN_WRITES or self._peer_is_loopback()
                or self.connection.getsockname()[0] == self.client_address[0]):
            return True
        # Default "" is deliberately NOT in LAN_WRITE_ROUTES: a caller that
        # forgets to pass the route gets the strict answer, never the loose
        # one.
        return route in LAN_WRITE_ROUTES

    def _token_ok(self, query: dict) -> bool:
        if not REQUIRE_TOKEN:
            return True
        supplied = (query.get("t") or [""])[0]
        # Compare BYTES. compare_digest raises TypeError on non-ASCII str, which
        # killed the handler thread with no response and appended an unbounded
        # traceback to the log -- trivially sprayable by any local page.
        return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"),
                                   TOKEN.encode("utf-8"))

    def _headers(self, status: int, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"style-src 'nonce-{NONCE}'; script-src 'nonce-{NONCE}'; "
            "connect-src 'self'; img-src 'none'; font-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self._headers(status, ctype, {"Content-Length": str(len(body))})
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny(self, status: int, msg: str) -> None:
        # Close on every denial. A rejected POST leaves its unread body on
        # the wire; keep-alive would parse those bytes as the NEXT request,
        # letting a hostile page smuggle a header-checked request through.
        self.close_connection = True
        self._send(status, "text/plain; charset=utf-8", msg.encode())

    # ------------------------------------------------------------- endpoints
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._deny(421, "bad host")
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if route == "/healthz":
            return self._send(200, "text/plain; charset=utf-8", b"ok")
        if not self._token_ok(query):
            return self._deny(403, "missing or invalid token")
        if route == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        if route == "/api/state":
            body = json.dumps(build_state()).encode()
            return self._send(200, "application/json; charset=utf-8", body)
        if route == "/api/events":
            # The driver/watcher timeline for the Events tab. Read-only, and
            # fetched on demand rather than riding the SSE frames.
            body = json.dumps({"rev": events_mod.rev(),
                               "events": events_mod.events()}).encode()
            return self._send(200, "application/json; charset=utf-8", body)
        if route == "/api/stream":
            return self._stream()
        if route == "/api/sysmon/history":
            # Up to 7 d of samples as raw `<I7f` records (ts + 7 float32,
            # NaN = missing); the page parses them with a DataView. Binary
            # because the same day of samples as JSON is ~2x the bytes for
            # no gain.
            #
            # `span` is the window the page is actually drawing. The full
            # ring is ~19 MB and the default zoom shows a day of it, so the
            # page asks for what it will draw and re-asks when you zoom
            # wider. A missing or unparseable span means the whole ring --
            # narrower is an optimisation, never a default.
            try:
                span = int(query.get("span", ["0"])[0])
            except ValueError:
                span = 0
            mon = sysmon.get()
            body = mon.history_bytes(span or sysmon.SLOTS) if mon else b""
            return self._send(200, "application/octet-stream", body)
        return self._deny(404, "not found")

    def _stream(self) -> None:
        # An HTTP/1.1 keep-alive response with neither Content-Length nor
        # chunked framing is undelimited. Close-delimited is correct for SSE here.
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8",
                      {"Connection": "close", "X-Accel-Buffering": "no"})
        self.end_headers()
        # Two cadences on one connection: the full state every POLL_SECONDS
        # (build_state stats NAS roots over SMB and must NOT run at 1 Hz),
        # and `mon` frames for the resource charts. The state gate is
        # ELAPSED TIME, not a tick counter -- sleep(1)+work drifts, and a
        # counter both truncates a fractional POLL_SECONDS and speeds up as
        # build_state slows down. The mon frame is a CURSOR: every sample
        # after the last one sent, so a slow iteration ships the seconds it
        # stepped over instead of silently dropping them -- a dropped second
        # renders as a gap, and a gap means "the sampler could not read
        # this", which would be a lie.
        last_state = 0.0
        last_mon_t = None
        try:
            while True:
                chunks = []
                now = time.time()
                if now - last_state >= POLL_SECONDS:
                    last_state = now
                    chunks.append(f"data: {json.dumps(build_state())}\n\n")
                mon = sysmon.get()
                samples = mon.since(last_mon_t) if mon else []
                if samples:
                    last_mon_t = samples[-1]["t"]
                    chunks.append(f"event: mon\ndata: "
                                  f"{json.dumps(samples)}\n\n")
                if chunks:
                    self.wfile.write("".join(chunks).encode())
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------- queue-override writes
    # The only two mutating routes on the server. Both funnel into
    # core.save_overrides(), and both can name a title only by exact match
    # against what the queue itself just reported -- never a path.
    MAX_BODY = 65536

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._deny(421, "bad host")
        parsed = urlparse(self.path)
        if not self._token_ok(parse_qs(parsed.query)):
            return self._deny(403, "missing or invalid token")
        # CSRF gate: a browser cannot attach a custom header cross-origin
        # without a CORS preflight, and this server never answers one.
        if self.headers.get("X-Smeltr") != "1":
            return self._deny(403, "missing X-Smeltr header")
        # Read-only for LAN peers unless explicitly opted in. Un-skipping a
        # title re-arms a deletion; a device merely holding the URL must not be
        # able to, by default -- but this Mac's own loopback requests still can.
        # _deny closes the connection, so the unread body on the wire can never
        # be replayed as a smuggled request.
        if not self._writes_ok(parsed.path):
            return self._deny(403, "read-only from the network — "
                              "skip/reorder, encode start/abort and stage "
                              "pulls only from this Mac. Pause/resume works "
                              "from any device holding this URL.")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._deny(400, "bad length")
        if not 0 < length <= self.MAX_BODY:
            return self._deny(413, "missing or oversized body")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._deny(400, "invalid JSON")
        if not isinstance(body, dict):
            return self._deny(400, "invalid JSON")

        with _ov_lock:
            if parsed.path == "/api/queue/skip":
                err = self._apply_skip(body)
            elif parsed.path == "/api/queue/order":
                err = self._apply_order(body)
            elif parsed.path == "/api/queue/crf":
                err = self._apply_crf(body)
            elif parsed.path == "/api/encode/start":
                err = self._apply_encode_start(body)
            elif parsed.path == "/api/encode/abort":
                err = self._apply_encode_abort(body)
            elif parsed.path == "/api/stage/start":
                err = self._apply_stage_start(body)
            elif parsed.path == "/api/stage/cancel":
                err = self._apply_stage_cancel(body)
            elif parsed.path == "/api/pause":
                err = self._apply_pause(body)
            elif parsed.path == "/api/driver/start":
                err = self._apply_driver_start(body)
            else:
                return self._deny(404, "not found")
        if err:
            return self._deny(409, err)
        with _state_lock:
            _state_cache["payload"] = None  # next read sees the new overrides
        payload = json.dumps(build_state()).encode()
        self._send(200, "application/json; charset=utf-8", payload)

    # ------------------------------------------------------- encode control
    # These two SPAWN and KILL HandBrake. Both return as soon as the process
    # has been signalled; the slow halves (a 120s parity gate, a 30s kill
    # grace) run on threads and report back through the state payload's
    # encode_note, because a handler thread must not block for minutes.

    def _apply_encode_start(self, body: dict):
        title, crf = body.get("title"), body.get("crf")
        if not isinstance(title, str):
            return "expected {title: str, crf: int}"
        if crf is None:
            # No CRF in the body means "whatever this row is planned at" --
            # the same value the queue cell shows and the driver would use.
            # Defaulting to a bare 16 here would have the start button
            # quietly disagree with the number under the operator's cursor.
            crf = core.planned_crf(title)
        if crf not in CRF_CHOICES:
            return "CRF must be one of %s" % ", ".join(
                str(c) for c in CRF_CHOICES)
        if not os.path.isdir(core.X9):
            return "the staging drive is not mounted"
        with _encode_lock:
            # Refuse rather than race. The driver checks hb_running, then asks
            # next_title.py, then spawns -- seconds during which it cannot see
            # a HandBrake we started. Closing that window properly needs a
            # change inside .autopilot.sh; until then the rule is that only one
            # of us runs at a time.
            if _driver_pids():
                return ("autopilot.sh is running — stop the driver first, or "
                        "let it pick the next title itself")
            if core.live_encodes():
                return "an encode is already running"
            matches = [r for r in self._queue_rows()
                       if r["title"].lower() == title.lower()]
            if not matches:
                return "title is not in the queue"
            if len(matches) > 1:
                return ("two queue rows share this folder name; refusing to "
                        "act on both")
            row = matches[0]
            if row.get("skipped"):
                return "this title is skipped — restore it first"
            if not row.get("staged"):
                return ("this title is not on the staging drive yet — only "
                        "staged titles can be encoded")
            if row.get("arriving_bytes") is not None:
                return "this title is still being copied to the staging drive"
            folder, src, out = _staging_files(row["title"])
            if out is not None:
                return ("this title already has a 2160p HEVC output — delete "
                        "it first, or let the driver record and sync it")
            if not src:
                return "no source .mkv in the staging folder"
            slug = _slug_of(row["title"])
            log = os.path.join(core.X9, ".hb-%s.log" % slug)
            dest = os.path.join(folder, "%s 2160p HEVC.mkv" % row["title"])
            try:
                fh = open(log, "wb")
                proc = subprocess.Popen(
                    ["HandBrakeCLI", "-i", src, "-o", dest,
                     "-f", "av_mkv", "-e", "x265_10bit", "-q", str(crf),
                     "--encoder-preset", "medium",
                     "--all-audio", "--aencoder", "copy",
                     "--audio-fallback", "ac3", "--all-subtitles"],
                    stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                return "could not start HandBrakeCLI: %s" % e
            _set_note("Starting %s at CRF %d — verifying track parity before "
                      "letting it run." % (row["title"], crf), kind="ok")
            threading.Thread(
                target=_parity_gate,
                args=(proc, src, dest, log, row["title"], slug, crf),
                daemon=True).start()
        return None

    def _apply_encode_abort(self, body: dict):
        title = body.get("title")
        if not isinstance(title, str):
            return "expected {title: str}"
        with _encode_lock:
            live = [e for e in core.live_encodes()
                    if (e.get("folder") or e.get("title", "")).lower()
                    == title.lower()]
            if not live:
                return "that title is not encoding right now"
            if len(live) > 1:
                return "more than one encode matches that title; refusing"
            enc = live[0]
            pid = enc.get("pid")
            if not pid:
                return "could not identify the HandBrake process"
            folder = enc.get("folder") or title
            _, _, out = _staging_files(folder)
            _set_note("Aborting %s — stopping HandBrake, then discarding the "
                      "partial." % folder, kind="warn")
            threading.Thread(target=_abort_worker, args=(pid, folder, out),
                             daemon=True).start()
        return None

    def _apply_stage_start(self, body: dict):
        """Enqueue a pull. The wire being busy means QUEUED, not refused.

        Everything that can be decided at click time still is, so a bad click
        gets an immediate 4xx instead of a silent drop twenty minutes later.
        What is NOT decided here is anything that can change while the title
        waits -- free space, the replenisher, the wire -- because a check made
        now is worthless by the time this title's turn arrives. The pump
        re-runs all of them at dispatch.
        """
        title = body.get("title")
        if not isinstance(title, str):
            return "expected {title: str}"
        if not os.path.isdir(core.X9):
            return "the staging drive is not mounted"
        rows = self._queue_rows()
        with _stage_lock:
            if _stage_active["title"] \
                    and _stage_active["title"].lower() == title.lower():
                return "this title is being pulled right now"
            if any(t.lower() == title.lower() for t in _stage_queue):
                return ("this title is already in the pull queue at position "
                        "%d" % (1 + [t.lower() for t in _stage_queue]
                                .index(title.lower())))
            row, src, size, err, held = _stage_candidate(title, rows)
            if err:
                return err if not held else err.replace("waiting — ", "")
            _stage_queue.append(row["title"])
            pos = len(_stage_queue)
            try:
                _ensure_pump_locked()
            except RuntimeError as e:
                _stage_queue.pop()
                return "could not start the pull dispatcher: %s" % e
            busy = _wire_busy_locked()
        if busy or pos > 1:
            _set_note("Queued %s (%.2f GiB) — position %d. One transfer runs "
                      "at a time; the rest wait their turn."
                      % (row["title"], size / 1073741824.0, pos), kind="ok")
        # Position 1 with a free wire: the pump starts it on its next tick,
        # and _begin_pull_locked writes the note that says so.
        return None

    def _apply_stage_cancel(self, body: dict):
        """Take a PENDING title out of the queue.

        It deliberately cannot touch the pull that is already running: there
        is no abort path for a transfer, and pretending otherwise would leave
        a half-written hidden folder that nothing owns.
        """
        title = body.get("title")
        if not isinstance(title, str):
            return "expected {title: str}"
        with _stage_lock:
            if _stage_active["title"] \
                    and _stage_active["title"].lower() == title.lower():
                return ("that pull is already running — it finishes or it "
                        "fails; the dashboard does not abort a transfer")
            keep = [t for t in _stage_queue if t.lower() != title.lower()]
            if len(keep) == len(_stage_queue):
                return "that title is not in the pull queue"
            _stage_queue[:] = keep
            if not _stage_queue:
                _stage_wait["why"] = None
        _set_note("Removed %s from the pull queue." % title, kind="ok")
        return None

    def _apply_driver_start(self, body: dict):
        # The big play button's "nothing is running" half. Launches
        # .autopilot.sh exactly the way CLAUDE.md's restart line does --
        # detached, cwd on the X9, appending to .autopilot.log -- because a
        # stopped driver previously had NO control at all: the operator had
        # to ssh in and paste a nohup line. Pause/resume stays the other
        # half; this route never touches a live pipeline (it refuses one).
        #
        # Refusals mirror can_start in reverse: two drivers double-record
        # against a ledger with no duplicate guard, and a driver started
        # beside a dashboard encode would pick and start a SECOND encode.
        # Two simultaneous clicks can both pass the pgrep -- the driver's own
        # mkdir lock then lets one win and the loser exits "already
        # running", the documented-harmless race.
        if _driver_pids():
            return "the driver is already running"
        if core.live_encodes():
            return ("an encode is already running — the driver would start "
                    "a second one beside it; abort it or let it finish first")
        if not os.path.isdir(core.X9):
            return "the staging drive is not mounted"
        # kill -9 (the documented stop) skips the driver's trap, so a lock
        # with no live process is provably stale -- the watchdog's rule --
        # and would make the fresh launch exit "already running" forever.
        lock = os.path.join(core.X9, ".autopilot.lock")
        if os.path.isdir(lock):
            try:
                os.rmdir(lock)
            except OSError as e:
                return "stale .autopilot.lock could not be cleared: %s" % e
        # Play means play: a leftover pause flag would leave the fresh driver
        # answering exit 3 in 300 s waits -- a start button that lies.
        try:
            core.set_paused(False)
        except OSError as e:
            return "could not clear the pause flag: %s" % e
        try:
            logf = open(os.path.join(core.X9, ".autopilot.log"), "ab")
        except OSError as e:
            return "could not open .autopilot.log: %s" % e
        try:
            # start_new_session: the driver must survive a dashboard restart
            # -- it is the pipeline, the server is only its window.
            subprocess.Popen(["./.autopilot.sh"], cwd=core.X9, stdout=logf,
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return "could not launch the driver: %s" % e
        finally:
            logf.close()
        # NOT the watchdog: a play-launched driver has no reboot recovery
        # until ops/watchdog.sh is relaunched by hand, and silently spawning
        # a supervisor that relaunches drivers is not what play means.
        _set_note("driver launched — it sweeps the drive, then picks the "
                  "next title within ~30 s", "ok")
        return None

    def _apply_pause(self, body: dict):
        # Pause-after-current: writes/removes core.PAUSE_FLAG. The running
        # encode is untouched -- it finishes, records and syncs as normal --
        # but next_title.py answers exit 3 while the flag exists, so the
        # driver waits instead of starting the next one. Nothing to refuse:
        # pausing while idle just keeps the driver waiting, and resuming is
        # picked up within one driver wait (300 s).
        on = body.get("paused")
        if not isinstance(on, bool):
            return "expected {paused: bool}"
        try:
            core.set_paused(on)
        except OSError as e:
            return "could not write the pause flag: %s" % e
        # Through the note banner, because the moment needs stating at the
        # TOP of the page, and resume's 5-minute pickup latency is otherwise
        # stated only while it does not yet apply and withdrawn when it does.
        _set_note("Paused — the current encode (if any) still finishes, "
                  "verifies and syncs; nothing new starts until resumed."
                  if on else
                  "Resumed — the driver picks up the next title within "
                  "5 minutes.", kind="ok")
        return None

    def _queue_rows(self) -> list[dict]:
        return build_state()["queue"]

    def _apply_skip(self, body: dict):
        title, skipped = body.get("title"), body.get("skipped")
        if not isinstance(title, str) or not isinstance(skipped, bool):
            return "expected {title: str, skipped: bool}"
        matches = [r for r in self._queue_rows()
                   if r["title"].lower() == title.lower()]
        if not matches:
            return "title is not in the queue"
        if len(matches) > 1:
            # Overrides key on the folder name; two distinct library files can
            # share one. One click must never act on both.
            return "two queue rows share this folder name; refusing to act on both"
        row = matches[0]
        if skipped and row.get("encoding"):
            return "cannot skip a title that is encoding right now"
        if skipped:
            # An encode that already FINISHED is beyond skipping: the driver
            # finds the folder by disk scan and will judge/record/sync it
            # regardless of the overrides file. Accepting the skip would show
            # a greyed row while the library original is deleted.
            d = os.path.join(core.X9, row["title"])
            try:
                files = os.listdir(d)
            except OSError:
                files = []
            if any("2160p HEVC" in f and f.endswith(".mkv")
                   and not f.startswith("._") for f in files):
                return ("this title's encode is already finished — it will be "
                        "recorded and synced; skipping cannot stop that")
        ov = core.load_overrides()
        skip = [t for t in ov["skip"] if t.lower() != row["title"].lower()]
        pri = [t for t in ov["priority"] if t.lower() != row["title"].lower()]
        if skipped:
            skip.append(row["title"])
        core.save_overrides(skip, pri)
        return None

    def _apply_crf(self, body: dict):
        """Set (or clear) the CRF a queued title will START its encode at.

        This is the fourth thing that steers the pipeline rather than watching
        it, and the narrowest: it decides `-q` for ONE title's next encode and
        nothing else. It cannot judge, sync, or delete -- but a CRF chosen too
        high produces a legitimately-verdicted `good` encode that syncs and
        replaces a ~90 GB original with a worse picture, so it is NOT in
        LAN_WRITE_ROUTES. Loopback only, like skip and reorder.

        `crf: null` clears the override back to the default rather than
        writing CRF_DEFAULT as a choice: a row nobody has touched must keep
        tracking the default if the default ever moves.
        """
        title, crf = body.get("title"), body.get("crf")
        if not isinstance(title, str):
            return "expected {title: str, crf: int|null}"
        if crf is not None and (isinstance(crf, bool)
                                or not isinstance(crf, int)
                                or crf not in CRF_CHOICES):
            return "CRF must be null or one of %s" % ", ".join(
                str(c) for c in CRF_CHOICES)
        matches = [r for r in self._queue_rows()
                   if r["title"].lower() == title.lower()]
        if not matches:
            return "title is not in the queue"
        if len(matches) > 1:
            # Overrides key on the folder name; two distinct library files can
            # share one. One click must never act on both.
            return "two queue rows share this folder name; refusing to act on both"
        row = matches[0]
        # An encode already running is past the point where -q means anything;
        # accepting the change would show a new number on a row whose encoder
        # is committed to the old one for the next several hours.
        if row.get("encoding"):
            return "this title is encoding right now — abort it first"
        ov = core.load_overrides()
        crfs = {t: v for t, v in ov["crf"].items()
                if t.lower() != row["title"].lower()}
        if crf is not None:
            crfs[row["title"]] = crf
        core.save_overrides(ov["skip"], ov["priority"], crfs)
        return None

    def _apply_order(self, body: dict):
        order = body.get("order")
        if not isinstance(order, list) or \
                not all(isinstance(t, str) for t in order):
            return "expected {order: [titles]}"
        if len(order) > 500:
            return "order list too long"
        rows, dups = {}, set()
        for r in self._queue_rows():
            low = r["title"].lower()
            if low in rows:
                dups.add(low)
            rows[low] = r
        pri, seen = [], set()
        for t in order:
            row = rows.get(t.lower())
            if row is None:
                return "a title in the order is not in the queue"
            if t.lower() in dups:
                return "two queue rows share this folder name; refusing to act on both"
            if row.get("skipped"):
                return "cannot prioritise a skipped title"
            if row["title"].lower() in seen:
                continue
            seen.add(row["title"].lower())
            pri.append(row["title"])
        ov = core.load_overrides()
        core.save_overrides(ov["skip"], pri)
        return None

    def log_message(self, fmt, *args) -> None:
        """Silence per-request logging; this runs alongside a live encode."""
        return


def free_port(preferred: int = 8787) -> int:
    """Prefer a stable port so the dashboard URL stays bookmarkable.

    SO_REUSEADDR matters here: without it a just-restarted server finds its own
    previous socket in TIME_WAIT, silently falls through to an ephemeral port,
    and every restart hands out a different URL.

    A port is accepted only if free on EVERY address we will listen on -- all
    of BINDS, plus loopback when LAN-exposed. Probing one address alone could
    hand back a port a squatter already holds on another, so one of the
    documented URLs would reach the squatter instead of us.
    """
    addrs = list(BINDS)
    if LAN_EXPOSED and "127.0.0.1" not in addrs:
        addrs.append("127.0.0.1")
    for want in (preferred, 0):
        primary = socket.socket()
        primary.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            primary.bind((addrs[0], want))
        except OSError:
            primary.close()
            continue
        port = primary.getsockname()[1]  # resolve the real port before re-probing
        probes = []
        try:
            # Hold each probe open until all have passed: closing as we go
            # would let a racing squatter take an address we already cleared.
            for addr in addrs[1:]:
                sock = socket.socket()
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((addr, port))
                except OSError:
                    sock.close()
                    break  # port held elsewhere; try the next candidate
                probes.append(sock)
            else:
                return port
        finally:
            for sock in probes:
                sock.close()
            primary.close()
    raise SystemExit("no free port available on " + " + ".join(addrs))


# ------------------------------------------------------------------- the page
# The dashboard's markup, CSS and JS live in web/ as real files. They used to
# be one 2383-line r-string right here, which is why every UI test still has to
# pull functions out by brace-matching and why the JS was never syntax-checked
# by anything.
#
# This is NOT a build step and NOT a CDN. The files are read once at import and
# inlined into the same nonce'd <style>/<script> blocks the string used to
# carry, so the browser still receives ONE self-contained document. CSP stays
# `default-src 'none'` -- nothing is fetched over the network, and there is
# still no dependency to install.
_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "web")


def _asset(name: str) -> str:
    """One web/ file, or a hard failure.

    Serving a page with a missing stylesheet or a missing script would render
    as a blank screen with a working HTTP 200 -- the exact failure the boot
    skeleton exists to make impossible. Refuse to start instead.
    """
    path = os.path.join(_WEB, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise SystemExit(f"smeltr: cannot read {path} ({e}); the dashboard "
                         f"assets are missing from this checkout") from e


_PAGE = (_asset("index.html")
         .replace("__APP_CSS__", _asset("app.css"))
         .replace("__THEME_JS__", _asset("theme.js"))
         .replace("__APP_JS__", _asset("app.js")))


# The footer states the page's actual exposure; a LAN-bound page claiming
# "127.0.0.1 only" would be lying about its own reachability. BIND is
# operator-controlled but still HTML-escaped -- the page never interpolates a
# raw value, on principle.
# Off-box the page is read-only for network peers by default; say so unless the
# LAN-writes opt-in is on. (Loopback can always write regardless.)
_scope_text = (" + ".join(BINDS) + " · token required"
               + ("" if LAN_WRITES
                  else " · network peers may pause/resume — all other "
                       "writes from this Mac only")
               if LAN_EXPOSED else "127.0.0.1 only")
_SCOPE = html.escape(_scope_text)
PAGE = _PAGE.replace("__NONCE__", NONCE).replace("__SCOPE__", _SCOPE)


def main() -> None:
    # The resource sampler: one daemon thread, 1 Hz, history in sysmon.ring
    # beside the ledger. Started before serving so the first page load
    # already has whatever the previous run persisted.
    sysmon.start(core.SMELTR_DIR)
    # Adopt pulls a previous server run left behind: the transfer child
    # survives a restart, its wait-then-rename worker thread does not.
    _adopt_orphan_pulls()
    port = free_port()
    httpd = ThreadingHTTPServer((BIND, port), Handler)
    httpd.daemon_threads = True
    # A LAN bind must not KILL loopback: local bookmarks, CLAUDE.md, and the
    # terminal report all say 127.0.0.1. Serve both -- same handler, same
    # gates -- with the loopback socket best-effort (it dies with the process
    # via daemon threads either way).
    aux_servers = []
    if LAN_EXPOSED:
        # Loopback AND every sibling LAN address. One socket cannot cover two
        # specific IPs, and 0.0.0.0 is forbidden here (it would also publish
        # any VPN or public interface), so each address gets its own listener
        # sharing the same Handler, the same token, and the same write gate.
        for addr in EXTRA_BINDS + ["127.0.0.1"]:
            try:
                extra = ThreadingHTTPServer((addr, port), Handler)
                extra.daemon_threads = True
                threading.Thread(target=extra.serve_forever, daemon=True).start()
                aux_servers.append(extra)
            except OSError as e:
                # free_port already required this port free on every address,
                # so this is a genuine surprise -- surface it rather than
                # silently leaving a documented URL dead.
                print(f"smeltr: listener on {addr}:{port} failed ({e}); "
                      f"http://{addr}:{port}/ will not work this run",
                      file=sys.stderr, flush=True)
    # Print the plain URL unless the token is actually required -- a link the
    # user cannot retype is a link they cannot use. When LAN-bound the URL
    # names the LAN address (that is the whole point) and always carries the
    # token, since REQUIRE_TOKEN is forced on above.
    url = (f"http://{BIND}:{port}/?t={TOKEN}" if REQUIRE_TOKEN
           else f"http://{BIND}:{port}/")
    urlfile = os.path.join(core.SMELTR_DIR, "url")
    pidfile = os.path.join(core.SMELTR_DIR, "server.pid")

    # The server owns its own pidfile. Matching on the command line instead was
    # unreliable: the same server started as "python3 server.py" from its own
    # directory and as "python3 ~/.smeltr/server.py" produced two different ps
    # patterns, so one invocation went unnoticed, kept port 8787, and every
    # later start silently drifted to an ephemeral port.
    # The URL embeds the auth token, so it must not be world-readable -- and
    # neither must the ledger. Default umask left both at 0644.
    try:
        os.chmod(core.SMELTR_DIR, 0o700)
        if os.path.exists(core.LEDGER):
            os.chmod(core.LEDGER, 0o600)
    except OSError:
        pass
    for path, data in ((pidfile, f"{os.getpid()}\n"), (urlfile, url + "\n")):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        # os.open's mode is ignored when the file already exists, so a
        # pre-existing 0644 url file kept its permissions and the token stayed
        # world-readable. chmod unconditionally.
        os.chmod(path, 0o600)
    # Only print the URL to a terminal. The launcher redirects stdout to
    # server.log, which is how the token ended up sitting in a 0644 file in
    # plaintext despite the header claiming it is never logged.
    if sys.stdout.isatty():
        print(url, flush=True)

    # The launcher stops the server with SIGTERM, whose default handler skips
    # the finally below and leaves the token-bearing url file on disk. Turn it
    # into a clean shutdown so the runtime files are always removed.
    import signal

    def _term(_signo, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError):
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        for extra in aux_servers:
            extra.shutdown()
            extra.server_close()
        for path in (pidfile, urlfile):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
