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
core.save_overrides() (skip list + priority) and also require a custom X-Smeltr
header a cross-origin page cannot attach; CSP is nonce-based with no external
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

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
        rate = None
        if rec is None:
            rec = {"done": done, "t": now, "grew": now}
            _xfer_track[folder] = rec
        elif done > rec["done"]:
            dt = now - rec["t"]
            if dt > 0:
                rate = (done - rec["done"]) / dt
            rec.update(done=done, t=now, grew=now)
        elif done < rec["done"]:
            # A smaller partial is a NEW attempt; restart tracking.
            rec.update(done=done, t=now, grew=now)
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
        _mark_ready(q, live)
        # Snapshot the note under the lock: a torn read could pair a failure
        # message with the previous note's "ok" kind.
        with _state_lock:
            note = {"msg": _encode_note["msg"], "kind": _encode_note["kind"]}
        payload = {
            "summary": core.summary(hist=hist, q=q),
            "live": live,
            "ledger": hist,
            "queue": q,
            "transfers": _transfers(),
            "encode_note": note,
            "crf_choices": list(CRF_CHOICES),
            "can_start": not live and not _driver_pids(),
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
CRF_CHOICES = (16, 18, 20, 22, 24)
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


def _mark_ready(rows: list, live: list) -> None:
    """Flag the ONE row the pipeline would actually encode next.

    Deliberately mirrors next_title.py: staged, no 2160p HEVC output yet, not
    skipped, not still arriving. It is NOT "row 1" -- the queue lists library
    titles that are not on the staging drive, so the top row is frequently a
    title next_title.py skips straight past, and painting that one green would
    promise an encode that cannot start. Nothing is ready while an encode is
    running: the amber encoding row is the live one.
    """
    for r in rows:
        r["ready"] = False
    if live:
        return
    for r in rows:
        if r.get("skipped") or not r.get("staged"):
            continue
        if r.get("arriving_bytes") is not None:
            continue
        _, src, out = _staging_files(r["title"])
        if out is not None or not src:
            continue
        r["ready"] = True
        return


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


def _stage_worker(title: str, src: str, hidden: str) -> None:
    """Run the pull into the hidden folder, then move it into place.

    The rename is the commit. Until .ssh-xfer.sh's byte-count check passes,
    nothing outside ".pull-<title>" exists, so next_title.py, the
    replenisher's folder count, and the queue's staged flag never see a
    half-arrived title -- a VISIBLE folder without a source .mkv is exactly
    the state that halts the driver ("no source file", exit 2). A leftover
    from a crashed server stays hidden too: it can only ever render as a
    stalled arrival, never as an encodable folder.
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

    def _writes_ok(self) -> bool:
        # Loopback peers always; LAN peers only with the explicit opt-in.
        return LAN_WRITES or self._peer_is_loopback()

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
        if route == "/api/stream":
            return self._stream()
        return self._deny(404, "not found")

    def _stream(self) -> None:
        # An HTTP/1.1 keep-alive response with neither Content-Length nor
        # chunked framing is undelimited. Close-delimited is correct for SSE here.
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8",
                      {"Connection": "close", "X-Accel-Buffering": "no"})
        self.end_headers()
        try:
            while True:
                chunk = f"data: {json.dumps(build_state())}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(POLL_SECONDS)
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
        if not self._writes_ok():
            return self._deny(403, "read-only from the network — "
                              "skip/reorder, encode start/abort and stage "
                              "pulls only from this Mac (127.0.0.1)")
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
            elif parsed.path == "/api/encode/start":
                err = self._apply_encode_start(body)
            elif parsed.path == "/api/encode/abort":
                err = self._apply_encode_abort(body)
            elif parsed.path == "/api/stage/start":
                err = self._apply_stage_start(body)
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
            crf = 16
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
        title = body.get("title")
        if not isinstance(title, str):
            return "expected {title: str}"
        if not os.path.isdir(core.X9):
            return "the staging drive is not mounted"
        with _stage_lock:
            if _stage_active["title"]:
                return ("already pulling %s — one pull at a time"
                        % _stage_active["title"])
            # A pull orphaned by a server restart still owns the wire and the
            # free space; the in-process flag cannot see it, pgrep can.
            if _pgrep(r"ssh-xfer\.sh pull"):
                return ("a pull is already running (it survived a server "
                        "restart) — one at a time")
            # The replenisher rebuilds its pick list from a live listdir; two
            # pullers choosing at the same moment could pick the same title.
            # Liveness by pgrep, not the lock alone: the pause procedure kills
            # orphans with -9 and leaves mkdir locks behind.
            if os.path.isdir(os.path.join(core.X9, ".replenish.lock")):
                if _pgrep(r"replenish-queue\.sh"):
                    return ("the replenisher is running — it may be staging "
                            "this title already; try again in a minute")
                return ("a STALE .replenish.lock is on the staging drive (no "
                        "replenisher is running) — it blocks the replenisher "
                        "too; remove it by hand: rmdir '%s'"
                        % os.path.join(core.X9, ".replenish.lock"))
            rows = self._queue_rows()
            matches = [r for r in rows
                       if r["title"].lower() == title.lower()]
            if not matches:
                return "title is not in the queue"
            if len(matches) > 1:
                return ("two queue rows share this folder name; refusing to "
                        "act on both")
            row = matches[0]
            if row.get("skipped"):
                return "this title is skipped — restore it first"
            # Arriving before staged: "already there" is the wrong message
            # for a file that is mostly missing.
            if row.get("arriving_bytes") is not None:
                pct = (" (%.0f%% pulled)"
                       % (row["arriving_bytes"] / row["bytes"] * 100)
                       if row.get("bytes") else "")
                return ("this title is already being copied to the staging "
                        "drive%s" % pct)
            if row.get("staged"):
                return "this title is already on the staging drive"
            srcs = set()
            for rec in core.load_index():
                p = rec.get("path", "")
                if not p or "2160p hevc" in os.path.basename(p).lower():
                    continue
                if os.path.basename(os.path.dirname(p)).lower() \
                        == row["title"].lower():
                    srcs.add(p)
            if not srcs:
                return "this title's file is not in the bitrate index"
            if len(srcs) > 1:
                return ("the index lists more than one source file for this "
                        "title; refusing to pick one")
            src = srcs.pop()
            if not os.path.isfile(src):
                return "the library file is unreachable — is the NAS mounted?"
            try:
                size = os.path.getsize(src)
            except OSError as e:
                return "cannot stat the library file: %s" % e
            # Free-space gate, same margin as .replenish-queue.sh (source +
            # 10 GiB headroom for the encode written alongside), PLUS the
            # bytes other in-flight pulls have promised but not yet written —
            # statvfs only counts what has already landed.
            try:
                st = os.statvfs(core.X9)
                avail = st.f_bavail * st.f_frsize
            except OSError:
                return "cannot read free space on the staging drive"
            inbound = sum(max(0, (r.get("bytes") or 0) - r["arriving_bytes"])
                          for r in rows
                          if r.get("arriving_bytes") is not None)
            need = size + inbound + 10 * 1024 ** 3
            if avail < need:
                return ("only %.0f GiB free on the staging drive — need "
                        "%.0f GiB (source + in-flight pulls + 10 GiB margin)"
                        % (avail / 1073741824.0, need / 1073741824.0))
            # The pull lands HIDDEN (dot-prefixed, so invisible to
            # next_title.py, core.staged_folders(), and the replenisher's
            # find) and is renamed into place only once the byte count checks
            # out. A visible folder with no source file HALTS the driver.
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
            _set_note("Staging %s — pulling %.2f GiB from the library over "
                      "SSH." % (row["title"], size / 1073741824.0), kind="ok")
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


_PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Smeltr</title>
<style nonce="__NONCE__">
:root{
  --r:10px;
  /* Utility voice for machine-facing text: status chips, tagline, footer.
     Data cells stay proportional + tabular-nums — tnum aligns digits without
     monospace's width penalty, and reads better at 13px. Exception: the
     queue's Src Mb/s / Src size / Src folder columns are mono by request. */
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  color-scheme:dark;
  --bg:#08090b; --panel:#0f1116; --panel-2:#13161c; --line:#1e232c;
  --ink:#e8ecf2; --ink-2:#98a2b3; --ink-3:#5f6a7d;
  --hot:#ff7a2f; --hot-soft:#ff9a5c; --cool:#4cc9f0; --good:#3ddc97;
  --warn:#ffc857; --bad:#ff5c5c;
  /* Derived surfaces. Every colour that used to be a hex literal further down
     is named here instead -- a theme cannot override what it cannot name, and
     the dozen inline hexes were exactly the things that stayed dark. */
  --live-bd:#3a2415; --live-bg:#16110d;
  --alert-bd:#5a1d1d; --alert-bg:#1c0f0f;
  --good-bd:#1d4435; --warn-bd:#4a3b18; --bad-bd:#4a1f1f;
  --cool-bd:#1c3a47; --hot-bd:#4a3018;
  --bar-bg:#1a1f27; --bar-inset:rgba(0,0,0,.5);
  --tab-bd:#2c3543; --td-line:#14181f;
  --row-hover:#12151b; --row-enc:#171208; --row-ready:#0b1712;
  --thumb:#2a313d; --thumb-hover:#3b4553;
  /* Motion accents: the molten bar's sheen, tip and heat glow, and the
     status-dot halo. Tokens in BOTH themes, like every other colour. */
  --sheen:rgba(255,255,255,.30); --tip:#ffe2c4; --glow:rgba(255,122,47,.40);
  --halo-good:rgba(61,220,151,.15); --halo-good-2:rgba(61,220,151,.04);
  /* Size-projection band. Ticks only, never a fill -- a filled zone on a
     pill-shaped track reads as a value. 4.52:1 on --bar-bg (WCAG 1.4.11). */
  --band-bd:#459473;
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#f5f7fa; --panel:#ffffff; --panel-2:#eef1f6; --line:#dde2ea;
  --ink:#141922; --ink-2:#4d5768; --ink-3:#646d7e;
  /* Accents darken rather than invert: the same hues, pulled down until they
     carry on white. The 22px stat values only need 3:1, the 11px labels need
     4.5:1, and --ink-3 is the one doing most of the small-text work. */
  --hot:#c2540f; --hot-soft:#b04e0c; --cool:#0b6f9e; --good:#0f7a55;
  --warn:#8a5a00; --bad:#c22f2d;
  --live-bd:#f0cbab; --live-bg:#fff6ee;
  --alert-bd:#f0bab8; --alert-bg:#fff4f3;
  --good-bd:#a9dcc6; --warn-bd:#e4cd97; --bad-bd:#f0bab8;
  --cool-bd:#a6d3e6; --hot-bd:#f0cbab;
  --bar-bg:#e3e7ee; --bar-inset:rgba(20,25,34,.12);
  --tab-bd:#c6cedb; --td-line:#eff2f6;
  --row-hover:#f4f7fa; --row-enc:#fff6ea; --row-ready:#edfaf3;
  --thumb:#c8cfda; --thumb-hover:#a8b2c1;
  --sheen:rgba(255,255,255,.60); --tip:#ffd9ae; --glow:rgba(194,84,15,.30);
  --halo-good:rgba(15,122,85,.18); --halo-good-2:rgba(15,122,85,.05);
  --band-bd:#2f7357;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  background:var(--bg); color:var(--ink);
  font:14px/1.5 ui-sans-serif,-apple-system,"SF Pro Text",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
  font-variant-numeric:tabular-nums; padding:28px 24px 64px; max-width:1680px; margin:0 auto;
}
.num,td.n,th.n{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
header{display:flex;align-items:baseline;gap:14px;margin-bottom:22px;flex-wrap:wrap}
.wordmark{font-size:19px;font-weight:680;letter-spacing:-.02em}
.wordmark b{color:var(--hot)}
.tag{color:var(--ink-3);font-family:var(--mono);font-size:12px;letter-spacing:.01em}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ink-3);display:inline-block;
     margin-right:6px;vertical-align:middle}
.dot.on{background:var(--good);box-shadow:0 0 0 3px var(--halo-good);
        animation:beat 2.4s ease-in-out infinite}
@keyframes beat{0%,100%{box-shadow:0 0 0 3px var(--halo-good)}
                50%{box-shadow:0 0 0 7px var(--halo-good-2)}}
.dot.off{background:var(--bad);box-shadow:0 0 0 3px rgba(255,92,92,.15)}
.conn{margin-left:auto;font-size:12px;color:var(--ink-3)}
.themeseg{align-self:center;display:flex;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;overflow:hidden}
.themebtn{background:none;border:0;color:var(--ink-2);width:30px;height:28px;
  padding:0;cursor:pointer;display:grid;place-items:center}
.themebtn+.themebtn{border-left:1px solid var(--line)}
.themebtn:hover{color:var(--ink)}
.themebtn:focus-visible{outline:2px solid var(--cool);outline-offset:-2px}
.themebtn[aria-pressed="true"]{background:var(--panel-2);color:var(--ink)}
/* One glyph family, drawn from currentColor rather than emoji or font glyphs,
   so it inverts with the theme and cannot render as a colour emoji:
   hollow circle = light, filled = dark, half-filled = follow the system. */
.themebtn i{display:block;width:13px;height:13px;border-radius:50%;
  border:1.5px solid currentColor}
.themebtn i.full{background:currentColor}
.themebtn i.half{background:linear-gradient(90deg,currentColor 0 50%,transparent 50% 100%)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px}
.stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-3)}
.stat .v{font-size:22px;font-weight:640;letter-spacing:-.02em;margin-top:5px;line-height:1.15}
.stat .s{font-size:11.5px;color:var(--ink-3);margin-top:2px}
.v.hot{color:var(--hot-soft)} .v.cool{color:var(--cool)}
/* A stat that just changed flashes its frame once. The entrance runs only on
   the first paint -- body.booted turns it off, or every SSE rebuild would
   replay it and the page would twitch every two seconds. */
.stat.flash{animation:statflash .9s ease-out}
@keyframes statflash{from{border-color:var(--hot);box-shadow:0 0 10px var(--glow)}}
body:not(.booted) .stat,body:not(.booted) .card{
  animation:rise .5s cubic-bezier(.2,.7,.3,1) both}
body:not(.booted) .stats .stat:nth-child(2){animation-delay:.05s}
body:not(.booted) .stats .stat:nth-child(3){animation-delay:.1s}
body:not(.booted) .stats .stat:nth-child(4){animation-delay:.15s}
body:not(.booted) .stats .stat:nth-child(5){animation-delay:.2s}
body:not(.booted) .stats .stat:nth-child(6){animation-delay:.25s}
body:not(.booted) #liveWrap .card{animation-delay:.12s}
@keyframes rise{from{opacity:0;transform:translateY(7px)}}

.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
      padding:18px 20px;margin-bottom:18px}
.card.live{border-color:var(--live-bd);background:linear-gradient(180deg,var(--live-bg),var(--panel))}
.card.alert{border-color:var(--alert-bd);background:linear-gradient(180deg,var(--alert-bg),var(--panel))}
.card.alert .live-title{color:var(--bad)}
/* A start/abort note that reports success is framed calm, not alarming. */
.card.alert.okline{border-color:var(--good-bd);background:var(--panel)}
.card.alert.okline .verdict{color:var(--good)}
.live-top{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.live-title{font-size:17px;font-weight:640;letter-spacing:-.015em}
.chip{font-size:11px;padding:2.5px 8px;border-radius:999px;border:1px solid var(--line);
      color:var(--ink-2);background:var(--panel-2);white-space:nowrap}
.chip.good{color:var(--good);border-color:var(--good-bd)} .chip.thin{color:var(--warn);border-color:var(--warn-bd)}
.chip.bad{color:var(--bad);border-color:var(--bad-bd)}
/* The molten pour. The one deliberately loud thing on the page: a heat glow,
   a sheen flowing along the fill, a white-hot leading tip, and the exact
   percentage beside it. The fill node is persistent across SSE frames (see
   renderLive), so the width transition and the keyframes actually run. */
.barrow{display:flex;align-items:center;gap:16px;margin:14px 0 10px}
.bar{flex:1;height:10px;border-radius:99px;background:var(--bar-bg);
     box-shadow:inset 0 1px 2px var(--bar-inset)}
.bar>i{position:relative;display:block;height:100%;border-radius:99px;overflow:hidden;
       background:linear-gradient(90deg,var(--hot),var(--hot-soft));
       box-shadow:0 0 12px var(--glow);
       transition:width .9s cubic-bezier(.4,0,.2,1)}
.bar>i::before{content:"";position:absolute;top:0;bottom:0;left:0;width:45%;
       background:linear-gradient(100deg,transparent 15%,var(--sheen) 50%,transparent 85%);
       animation:sheen 2.1s ease-in-out infinite}
.bar>i::after{content:"";position:absolute;right:0;top:0;bottom:0;width:7px;
       border-radius:99px;background:var(--tip);
       animation:tipglow 1.4s ease-in-out infinite alternate}
@keyframes sheen{from{transform:translateX(-110%)}to{transform:translateX(340%)}}
@keyframes tipglow{from{box-shadow:0 0 4px 1px var(--glow);opacity:.7}
                   to{box-shadow:0 0 14px 4px var(--glow);opacity:1}}
.pctbig{font-size:26px;font-weight:680;letter-spacing:-.02em;line-height:1;
        color:var(--hot-soft);white-space:nowrap}
/* Size projection -- the whole point of the job, in one strip. The scale is
   0-100% OF THE SOURCE, so further left is always better, and the shaded zone
   is the 30-80% target band. It renders before a projection exists: an empty
   scale reads as "not known yet", where a missing component reads as "nothing
   to say". No overflow:hidden -- the marker overhangs the track deliberately
   so it stays visible at 0% and 100%. */
.proj{margin:14px 0 2px;padding:12px 14px;border:1px solid var(--line);
      border-radius:var(--r);background:var(--panel-2)}
.proj-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
.proj-head span{display:block;font-size:11px;color:var(--ink-3);
      text-transform:uppercase;letter-spacing:.06em;margin-top:3px}
/* Units survive CSS. uppercase renders GiB as GIB -- the same defect that once
   turned Mb/s into MB/S on this page, one line under a correctly-cased GiB. */
.proj-ratio span{text-transform:none;letter-spacing:0}
.proj-size b,.proj-ratio b{font-size:22px;font-weight:680;letter-spacing:-.02em;line-height:1}
.proj-ratio{text-align:right;margin-left:auto}
.proj-ratio b{color:var(--ink-2)}
.proj-ratio.on b{color:var(--good)}   .proj-ratio.under b{color:var(--cool)}
.proj-ratio.over b{color:var(--warn)} .proj-ratio.bad b{color:var(--bad)}
.proj-scale{position:relative;height:12px;margin:14px 0 6px;border-radius:99px;
      background:var(--bar-bg);box-shadow:inset 0 1px 2px var(--bar-inset)}
/* Ticks and a hairline, NOT a fill. This track sits 40px under the progress
   bar, which is the same shape and where filled MEANS value -- a shaded zone
   here read as "we are about halfway", especially before a marker exists. */
.proj-zone{position:absolute;top:-4px;bottom:-4px;left:30%;width:50%;
      border-left:2px solid var(--band-bd);border-right:2px solid var(--band-bd)}
.proj-zone::after{content:"";position:absolute;left:0;right:0;top:50%;
      height:1px;background:var(--band-bd)}
.proj-mark{position:absolute;top:-4px;bottom:-4px;width:3px;border-radius:2px;
      transform:translateX(-50%);background:var(--ink);
      transition:left .9s cubic-bezier(.4,0,.2,1)}
.proj-mark.on{background:var(--good)}   .proj-mark.under{background:var(--cool)}
.proj-mark.over{background:var(--warn)} .proj-mark.bad{background:var(--bad)}
.proj-legend{display:flex;justify-content:space-between;gap:8px;font-size:10.5px;
      color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.proj-lead{margin-top:10px;font-size:12.5px;font-weight:600;letter-spacing:.03em;
      color:var(--ink-2);max-width:70ch}
.proj-lead.on{color:var(--good)} .proj-lead.warn{color:var(--warn)}
.proj-lead.bad{color:var(--bad)}
.proj-detail{margin-top:4px;font-size:12px;color:var(--ink-3);max-width:70ch}
/* A downscale makes the ratio meaningless -- the pixel count changed, so the
   bytes are not comparable. Grey the figures rather than colour them. */
.proj-ratio.lost b,.proj.lost .proj-size b{color:var(--ink-3)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px 18px;margin-top:12px}
.kv div span{display:block;font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em}
.kv div b{font-weight:580;font-size:14px}
.verdict{margin-top:12px;font-size:12.5px;color:var(--ink-2);max-width:70ch}
.verdict.loud{color:var(--warn);font-weight:560}
.note{padding:10px 14px;font-size:11.5px;color:var(--ink-3);border-top:1px solid var(--line)}

.tabs{display:flex;gap:6px;margin:0 0 12px}
.tab{background:var(--panel);border:1px solid var(--line);color:var(--ink-2);
     padding:6px 13px;border-radius:8px;font:inherit;font-size:12.5px;cursor:pointer;
     transition:color .15s,border-color .15s,background .15s}
.tab[aria-selected="true"]{background:var(--panel-2);color:var(--ink);border-color:var(--tab-bd)}
.tab:focus-visible{outline:2px solid var(--cool);outline-offset:2px}

.wrap{border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:var(--panel)}
/* NO scrollbar, and no space reserved for one. The pane still scrolls in both
   axes -- only the bar is hidden.
   Two properties are needed and BOTH are load-bearing. scrollbar-width:none is
   the standard one; ::-webkit-scrollbar{display:none} is what actually removes
   it in Safari/Chrome. Omitting the webkit rule and merely leaving the bar
   unstyled does NOT work: macOS "Show scroll bars: Always" then paints a
   permanent classic bar that also consumes layout width -- worse than the
   11px gutter this replaced. Do not remove either line. */
.scroll{max-height:60vh;overflow:auto;overflow-x:auto;scrollbar-width:none}
.scroll::-webkit-scrollbar{display:none}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px;line-height:1.4}
th,td{padding:8px 12px;text-align:left;white-space:nowrap}
th{position:sticky;top:0;z-index:1;background:var(--panel-2);color:var(--ink-3);
   font-weight:560;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
   border-bottom:1px solid var(--line)}
td{border-bottom:1px solid var(--td-line)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--row-hover)}
td.n,th.n{text-align:right}
.title-cell{white-space:normal;min-width:230px}
.muted{color:var(--ink-3)}
td.mono{font-family:var(--mono);font-size:12.5px}
td.src-dir{font-family:var(--mono);font-size:11.5px;color:var(--ink-2)}
/* th uppercases everything, which would turn Mb/s into MB/S -- an 8x unit
   lie on the one column whose unit confusion already corrupted the old state
   file. Headers carrying a unit keep their own case. */
th.unit{text-transform:none}
.mark.xfer{color:var(--warn);border-color:var(--warn-bd)}
.mark.stall{color:var(--bad);border-color:var(--bad-bd)}
.rowxfer td{background:var(--row-hover)}
.minibar{display:inline-block;vertical-align:middle;margin-left:8px;width:84px;height:4px;
         border-radius:99px;background:var(--bar-bg);overflow:hidden}
.minibar i{display:block;height:100%;background:var(--warn)}
.minibar.pull i{background:var(--bad)}
.xfer-pct{margin-left:6px;font-family:var(--mono);font-size:10.5px;color:var(--ink-2)}
.mark{font-family:var(--mono);font-size:10.5px;padding:1.5px 6px;border-radius:5px;
      border:1px solid var(--line);color:var(--ink-3)}
.mark.staged{color:var(--cool);border-color:var(--cool-bd)}
.mark.enc{color:var(--hot-soft);border-color:var(--hot-bd)}
.mark.pin{color:var(--cool);border-color:var(--cool-bd)}
.mark.skip{color:var(--ink-2);border-style:dashed}
/* Categorical tint per NAS, stable per name. Known roots get fixed hues;
   an unknown volume falls back to a name hash so it still colours stably. */
.mark.nas-cool{color:var(--cool);border-color:var(--cool-bd)}
.mark.nas-good{color:var(--good);border-color:var(--good-bd)}
.mark.nas-warn{color:var(--warn);border-color:var(--warn-bd)}
.mark.nas-hot{color:var(--hot-soft);border-color:var(--hot-bd)}
.mark+.mark{margin-left:6px}
.rowenc td{background:var(--row-enc)}
.rowready td{background:var(--row-ready)}

/* ---- Numeric queue columns. Colour here CARRIES INFORMATION, it is not
   decoration: SRC Mb/s is the ranking key for the whole job, so it is banded
   by value (>=90 hot, 80-89.9 warm, below that plain) and the eye can find the
   fattest remaining sources without reading digits. Rank is quiet because it
   is only a position; CRF is italic and muted because on an unstarted row it
   is a PLAN, not a measurement. tabular-nums keeps the columns from dancing
   as digits change. Every colour is a token so both themes reach it. ---- */
td.q-rank{font-variant-numeric:tabular-nums;color:var(--ink-3);font-weight:560}
td.q-mbps{font-variant-numeric:tabular-nums;font-weight:680}
td.q-mbps.mbps-hi{color:var(--hot-soft)}
td.q-mbps.mbps-mid{color:var(--warn)}
td.q-mbps.mbps-lo{color:var(--ink-2)}
td.q-size{font-variant-numeric:tabular-nums;font-weight:560;color:var(--cool)}
td.q-crf{font-variant-numeric:tabular-nums;font-style:italic;color:var(--ink-3)}
/* The live row shows the encoder's ACTUAL CRF -- a measurement, so it loses
   the italic that marks a planned value. */
.rowenc td.q-crf{font-style:normal;font-weight:640;color:var(--hot-soft)}
.rowskip td.q-mbps,.rowskip td.q-size{color:var(--ink-3);font-weight:400}

/* ---- Row actions live in the title cell and appear on hover. The reveal is
   :hover OR :focus-within OR a coarse pointer -- a phone has no hover, and
   this dashboard is bound to the LAN specifically so phones can reach it.
   Hover-only here would mean the feature does not exist on the devices the
   firewall was opened for. ---- */
.title-cell{position:relative}
.tcell{display:flex;align-items:center;gap:9px;justify-content:space-between}
.tname{min-width:0}
.rowacts{display:flex;align-items:center;gap:6px;flex:0 0 auto;
         opacity:0;transition:opacity .12s}
tr:hover .rowacts,tr:focus-within .rowacts,.rowacts.armed{opacity:1}
.crfsel{background:var(--panel-2);border:1px solid var(--line);color:var(--ink-2);
        font:inherit;font-size:11px;font-family:var(--mono);border-radius:6px;
        padding:2px 4px;cursor:pointer}
.crfsel:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
.act.go:hover{color:var(--good);border-color:var(--good-bd)}
.act.armed{color:var(--bad);border-color:var(--bad-bd);font-weight:600}
.rowskip td{opacity:.45}
.rowskip td:last-child{opacity:1}
.rowskip .title-cell{opacity:1}
.rowskip .tname{color:var(--ink-3);text-decoration:line-through}
/* Queue control. The grip drags, the button skips; both write only the
   overrides file on the server, nothing else. */
.grip{cursor:grab;color:var(--ink-3);user-select:none;-webkit-user-select:none;
      letter-spacing:2px}
th.gripcol,td.gripcol{width:30px;padding-right:2px}
tr.dragsrc td{opacity:.35}
tr.dropline td{box-shadow:inset 0 2px 0 0 var(--cool)}
tr.dropline-after td{box-shadow:inset 0 -2px 0 0 var(--cool)}
tr.pin-end td{border-bottom:2px solid var(--cool-bd)}
.act{background:none;border:1px solid var(--line);color:var(--ink-3);border-radius:6px;
     font:inherit;font-size:11px;padding:2.5px 9px;margin:-4px 0;cursor:pointer;
     transition:color .15s,border-color .15s}
.act:hover{color:var(--bad);border-color:var(--bad-bd)}
.act.restore:hover{color:var(--good);border-color:var(--good-bd)}
.act:focus-visible{outline:2px solid var(--cool);outline-offset:2px}
.act:disabled{opacity:.5;cursor:default}
.uinote{margin:0 0 12px;padding:10px 14px;border:1px solid var(--warn-bd);
        border-radius:var(--r);background:var(--panel);color:var(--warn);font-size:12.5px}
.empty{padding:28px;text-align:center;color:var(--ink-3);font-size:13px}
.lnotes{padding:10px 14px;font-size:11.5px;color:var(--ink-2);
        border-top:1px solid var(--line);display:grid;gap:6px}
#pane>table,#pane>.empty{animation:fadein .22s ease-out}
@keyframes fadein{from{opacity:0}}
footer{margin-top:22px;font-family:var(--mono);font-size:11px;color:var(--ink-3);
       display:flex;gap:14px;flex-wrap:wrap}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}

/* ---- Responsive. Below 700px the tables keep scrolling sideways: the
   title column pins to the left edge (the title is the identity of a row --
   an index column carries nothing the row order does not), and any column
   sliced at a boundary is blanked by the seam script rather than clipped.
   Every colour here is an existing token, so both themes are covered. ---- */
@media (max-width:700px){
  body{padding:16px 12px 48px}
  .kv{grid-template-columns:repeat(auto-fit,minmax(104px,1fr))}
  /* 60vh, floored so portrait phones keep a useful table, capped so the
     floor cannot exceed a short landscape viewport. */
  .scroll{max-height:min(max(60vh,340px),72dvh)}
  th,td{padding:7px 9px}
  /* Row colour lives on the tr so the pinned title inherits it opaquely --
     a transparent pinned cell shows the columns sliding through it. */
  tr{background:var(--panel)}
  thead tr{background:var(--panel-2)}
  tbody tr:hover{background:var(--row-hover)}
  .rowenc{background:var(--row-enc)}
  /* Both tables mark the title th and td with .title-cell, so no per-table
     column arithmetic is needed and left:0 is the only constant. Until the
     scroll reaches it the title sits in its natural column; from then on it
     docks at the left edge and the rest slides beneath it. */
  .title-cell{min-width:190px;position:sticky;left:0;z-index:1;
    background:inherit;border-right:1px solid var(--line)}
  /* Headers float above the pinned title cells; the pinned corner above both. */
  th{z-index:2}
  th.title-cell{z-index:3}
  /* A column sliced at a boundary renders EMPTY, not clipped. Clipped on its
     left, a right-aligned "70.64 GiB" still parses -- as "0.64 GiB"; clipped
     on its right it loses its unit and "78.24" bare could be Mb/s or GiB.
     The .cut class is applied by the seam script at the bottom of the page. */
  td.cut,th.cut{color:transparent}
  td.cut *,th.cut *{visibility:hidden}
}
@media (pointer:coarse){
  .themebtn{width:44px;height:44px}
  .tab{padding:11px 16px}
  /* HTML5 drag-and-drop does not exist on touch; hide the handles rather
     than advertise a gesture that cannot work. Skip buttons stay. */
  th.gripcol,td.gripcol{display:none}
  .act{padding:9px 14px;margin:-10px 0}
  /* No hover exists here, so the row actions are always shown. */
  .rowacts{opacity:1}
  .crfsel{padding:6px 6px;font-size:12px}
}
</style>
<script nonce="__NONCE__">
/* Runs before first paint. Anything later flashes the wrong theme on load. */
(function(){
  var v=null;
  try{ v=localStorage.getItem("smeltr.theme"); }catch(e){}
  if(v!=="light"&&v!=="dark"){
    v=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme",v);
})();
</script>
</head>
<body>
<header>
  <div class="wordmark">SMELTR<b>.</b></div>
  <div class="tag">remuxes in &middot; ingots out</div>
  <div class="conn"><span class="dot" id="dot"></span><span id="connText">connecting</span></div>
  <div class="themeseg" role="group" aria-label="Theme">
    <button class="themebtn" id="themeLight" type="button" title="Light theme" aria-label="Light theme" aria-pressed="false"><i></i></button>
    <button class="themebtn" id="themeDark" type="button" title="Dark theme" aria-label="Dark theme" aria-pressed="false"><i class="full"></i></button>
    <button class="themebtn" id="themeSystem" type="button" title="Follow system theme" aria-label="Follow system theme" aria-pressed="false"><i class="half"></i></button>
  </div>
</header>

<section id="alert"></section>
<section class="stats" id="stats"></section>
<section id="liveWrap"></section>

<div class="tabs" role="tablist">
  <button class="tab" id="tabQueue"  role="tab" aria-selected="true"  aria-controls="paneQueue">Queue</button>
  <button class="tab" id="tabLedger" role="tab" aria-selected="false" aria-controls="paneLedger">History</button>
  <button class="tab" id="resetOrder" type="button" hidden>Reset order</button>
</div>
<div class="uinote" id="uiNotice" hidden></div>
<div class="wrap"><div class="scroll" id="pane"></div></div>

<footer>
  <span id="gen"></span><span id="stopnote"></span><span>__SCOPE__ &middot; writes: skip/reorder, encode start/abort, stage pulls &middot; never judges, syncs or deletes a library original</span>
</footer>

<script nonce="__NONCE__">
(function(){
"use strict";
var GIB=1073741824, TIB=GIB*1024;
var token=new URLSearchParams(location.search).get("t")||"";
var tab="queue", last={};
var RECORD={live:"measured at finish","state-file":"hand-migrated",
            recovered:"found after the fact"};

function gib(b){ if(b==null) return "—";
  return b>=TIB ? (b/TIB).toFixed(2)+" TiB" : (b/GIB).toFixed(2)+" GiB"; }
function pct(v){ return v==null ? "—" : v.toFixed(1)+"%"; }
/* Live encode progress at HandBrake's own precision -- two decimals. The log
   never carries a third digit, so printing one was always a trailing zero. */
function pctLive(v){ return v==null ? "—" : v.toFixed(2)+"%"; }
function dur(s){ if(s==null) return "—";
  var h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return h ? h+"h "+String(m).padStart(2,"0")+"m" : m+"m"; }
function el(tag,cls,text){ var n=document.createElement(tag);
  if(cls) n.className=cls; if(text!=null) n.textContent=text; return n; }

function statCard(k,v,s,cls){
  var d=el("div","stat"); d.appendChild(el("div","k",k));
  d.appendChild(el("div","v "+(cls||""),v));
  if(s) d.appendChild(el("div","s",s)); return d;
}

/* The only two writes the page can make. Both name titles by exact match
   against the queue the server just sent; the response is a fresh state
   snapshot, painted immediately so the click lands without waiting for SSE. */
var NAS_FIXED={vhagar:"nas-cool",vermithor:"nas-good"};
var NAS_CLASSES=["nas-cool","nas-good","nas-warn","nas-hot"];
function nasMark(name){
  if(!name||name==="?") return el("span","muted",name||"—");
  var cls=NAS_FIXED[name.toLowerCase()];
  if(!cls){
    var h=0;
    for(var i=0;i<name.length;i++) h=(h*31+name.charCodeAt(i))>>>0;
    cls=NAS_CLASSES[h%NAS_CLASSES.length];
  }
  return el("span","mark "+cls,name);
}

/* Parent folder of the original, relative to the NAS volume; the title
   folder itself is dropped (it repeats the Title cell). Ambiguous or
   unknown paths come through as null and stay an honest em dash. */
function srcDirTd(srcDir,title){
  var td=el("td","src-dir","—");
  if(srcDir){
    var rel=srcDir.replace(/^\/Volumes\/[^/]+\//,"");
    var tail="/"+title;
    if(rel.slice(-tail.length)===tail) rel=rel.slice(0,-tail.length);
    td.textContent=rel; td.title=srcDir;
  }
  return td;
}

var noticeTimer=null;
function notice(msg){
  var n=document.getElementById("uiNotice");
  n.textContent=msg; n.hidden=false;
  clearTimeout(noticeTimer);
  noticeTimer=setTimeout(function(){ n.hidden=true; },5000);
}

var posting=false;
function api(path,payload){
  if(posting) return Promise.resolve();
  posting=true;
  return fetch(path+(token?"?t="+encodeURIComponent(token):""),{
    method:"POST",
    headers:{"Content-Type":"application/json","X-Smeltr":"1"},
    body:JSON.stringify(payload)})
  .then(function(r){
    if(!r.ok) return r.text().then(function(t){ throw new Error(t||"request failed"); });
    return r.json();
  })
  .then(function(s){ last.state=s; last.key=null; paint(s); })
  .catch(function(err){ notice((err&&err.message||"action failed").slice(0,120)); })
  .finally(function(){ posting=false; });
}

function renderAlert(s, note){
  var host=document.getElementById("alert"); host.replaceChildren();
  /* Outcome of the last start/abort. The slow halves (track parity, the kill
     grace) finish long after their POST returned, so this banner is how they
     report. kind=bad stays until the next action; ok/warn are informational. */
  if(note && note.msg){
    var nc=el("div","card alert"+(note.kind==="ok"?" okline":""));
    nc.appendChild(el("div","verdict"+(note.kind==="bad"?" loud":""),note.msg));
    host.appendChild(nc);
  }
  if(s.overrides_corrupt){
    var oc=el("div","card alert");
    oc.appendChild(el("div","live-title","queue_overrides.json is unreadable"));
    oc.appendChild(el("div","verdict",
      "Skips and hand-priorities are NOT being applied — the pipeline is "+
      "running in stock bitrate order. Fix or delete the file; any skip or "+
      "reorder here rewrites it cleanly."));
    host.appendChild(oc);
  }
  if(s.library_complete!==false) return;
  var c=el("div","card alert");
  c.appendChild(el("div","live-title","Library incomplete — "+
    (s.roots_offline||[]).join(", ")+" not mounted"));
  c.appendChild(el("div","verdict",
    "An unmounted NAS empties the queue, which looks identical to having "+
    "finished. Every queue figure below is PARTIAL — do not read it as "+
    "\u201cnothing left to encode\u201d."));
  host.appendChild(c);
}

var prevStats=null;
function renderStats(s){
  var host=document.getElementById("stats"); host.replaceChildren();
  var seen={};
  function add(k,v,sub,cls){
    var d=statCard(k,v,sub,cls);
    if(prevStats && prevStats[k]!==undefined && prevStats[k]!==v)
      d.classList.add("flash");
    seen[k]=v; host.appendChild(d);
  }
  add("Reclaimed", gib(s.reclaimed_bytes),
    s.completed_measured+" of "+s.completed+" encodes measured","hot");
  add("Average shrink", pct(s.avg_saved_pct),
    "weighted · "+gib(s.source_total_bytes)+" → "+gib(s.output_total_bytes));
  var complete=s.library_complete!==false;
  var skipnote=s.queue_skipped ? " · excludes "+s.queue_skipped+" skipped" : "";
  if(complete){
    add("Still queued", String(s.queue_waiting),
      gib(s.queue_bytes)+" of originals above "+s.stop_mbps+" Mb/s"+
      (s.queue_encoding ? " · incl. "+s.queue_encoding+" encoding" : "")+
      (s.queue_skipped ? " · "+s.queue_skipped+" skipped" : ""),"cool");
    add("Still to reclaim",
      s.queue_reclaimable_bytes==null ? "—" : "~"+gib(s.queue_reclaimable_bytes),
      "projected at "+pct(s.avg_saved_pct)+skipnote);
    add("Job progress",
      s.job_progress_pct==null ? "—" : "~"+pct(s.job_progress_pct),
      "by reclaimed bytes, not titles"+
      (s.queue_skipped ? " · goal still counts "+s.queue_skipped+" skipped" : ""));
  }else{
    /* An unmounted NAS empties the queue; a hard 0 in these slots is the
       most dangerous cell on the page. Refuse to print a number, exactly
       as the terminal report does. */
    add("Still queued","unknown",
      "library not fully mounted · "+s.queue_waiting+" readable");
    add("Still to reclaim","unknown","library not fully mounted — partial");
    add("Job progress","unknown","cannot be computed while a root is offline");
  }
  add("Staged", String(s.staged),
    gib(s.staged_bytes)+" of originals · "+s.queue_encoding+" encoding · "+
    s.staged_unencoded+" not yet encoded");
  prevStats=seen;
}

function liveChips(e){
  var top=el("div","live-top");
  /* e.title can arrive as a full staging path; show only the movie's name.
     The folder name is the identity everywhere else, so prefer it, falling
     back to the last path segment. Full value stays in the tooltip. */
  var name=String(e.folder||e.title).split("/").pop();
  var t=el("div","live-title",name);
  if(name!==e.title) t.title=e.title;
  top.appendChild(t);
  if(e.crf!=null) top.appendChild(el("span","chip","CRF "+e.crf));
  if(e.geometry) top.appendChild(el("span","chip",
    (e.source_geometry && e.source_geometry!==e.geometry)
      ? e.source_geometry+" → "+e.geometry+" (auto-crop)" : e.geometry));
  if(e.audio!=null){
    var loss = e.src_audio!=null && (e.src_audio!==e.audio || e.src_subs!==e.subs);
    top.appendChild(el("span","chip"+(loss?" bad":""), loss
      ? "TRACK LOSS "+e.src_audio+"a/"+e.src_subs+"s → "+e.audio+"a/"+e.subs+"s"
      : e.audio+" audio · "+e.subs+" subs"));
  }
  top.appendChild(e.decoder_errors
    ? el("span","chip bad", e.decoder_errors+" decoder errors")
    : el("span","chip", e.decoder_errors===0
        ? "0 decoder errors" : "decoder errors not yet reported"));
  var vc = e.verdict==="good"?"good":
           (e.verdict==="thin"||e.verdict==="suspect")?"thin":
           (e.verdict==="unknown"?"":"bad");
  if(e.verdict==="downscale") vc="bad";
  if(e.shrink_pct!=null) top.appendChild(el("span","chip "+vc, pct(e.shrink_pct)+" smaller"));
  top.appendChild(el("span","chip "+vc, e.verdict.toUpperCase()));
  return top;
}

function liveFields(e){
  return [["ETA", dur(e.eta_s)],
    ["Speed", e.avg_fps==null?"—":e.avg_fps.toFixed(1)+" fps avg"],
    ["Written", gib(e.output_bytes)],
    ["Started", e.started_text||"—"],
    ["PID", String(e.pid)]];
}

/* The strip's colour and headline come from `e.verdict` -- the SAME verdict
   `core._verdict()` computed and the pipeline acts on. An earlier version
   classified the ratio again here, in the browser, against its own copy of the
   thresholds. It disagreed with the server in three ranges, and two of those
   were dangerous:
     - a `downscale` (output frame narrower than source, the ONE state where the
       original must survive) scored 45% and rendered GREEN, "IN THE TARGET
       BAND", "frees 39 GiB" -- while the real warning sat below in grey prose.
     - Flight (2012) at 9.4% rendered RED "IMPLAUSIBLY SMALL". That encode is in
       the ledger with a recorded SSIM of 0.9931/0.9945 against the cropped
       original. The abort button is a hover away on that row.
   One threshold table, one verdict, one colour. Do not reintroduce a second.

   The 30-80% band is the user's TARGET, so it stays -- but as an uncoloured
   position on the scale and a plain sentence, never as a severity. */
var PROJ_CLASS={good:"on", thin:"warn", suspect:"warn",
                "no-saving":"bad", blowup:"bad", downscale:"bad", unknown:""};
var PROJ_LEAD={
  good:"SOLID REDUCTION",
  thin:"THIN SAVING — worth a human call",
  suspect:"UNUSUAL FOR THIS JOB — verify the picture before deleting the original",
  "no-saving":"BARELY SMALLER THAN THE SOURCE",
  blowup:"LARGER THAN THE SOURCE",
  downscale:"RESOLUTION LOST — DO NOT DELETE THE ORIGINAL",
  unknown:""};

/* Where the ratio sits against the user's target. Plain words, no severity:
   4 of the first 12 completed encodes landed under 30% and every one was a
   good encode, so "below" must never read as "broken". */
function bandText(r){
  if(r==null) return "";
  if(r>80)   return "above the 30–80% target band";
  if(r>=30)  return "in the 30–80% target band";
  return "below the 30–80% target band — normal for a clean digital source";
}

/* Estimates are marked. The stat cards and the History table already prefix
   "~" on anything derived; this is a linear extrapolation off a part-finished
   encode, so it gets the tilde and whole GiB rather than two decimals. */
function gibApprox(b){ return b==null ? "—" : "~"+Math.round(b/GIB)+" GiB"; }

function projBlock(){
  var refs={}, n=el("div","proj"); refs.root=n;
  var head=el("div","proj-head");
  var lhs=el("div","proj-size");
  refs.size=el("b",null,"—"); lhs.appendChild(refs.size);
  lhs.appendChild(el("span",null,"projected final size"));
  var rhs=el("div","proj-ratio"); refs.ratioWrap=rhs;
  refs.ratio=el("b",null,"—"); rhs.appendChild(refs.ratio);
  refs.srcCap=el("span",null,"of source"); rhs.appendChild(refs.srcCap);
  head.appendChild(lhs); head.appendChild(rhs); n.appendChild(head);

  refs.scale=el("div","proj-scale");
  refs.scale.setAttribute("role","img");
  refs.scale.appendChild(el("i","proj-zone"));
  refs.mark=el("i","proj-mark"); refs.mark.hidden=true;
  refs.scale.appendChild(refs.mark);
  n.appendChild(refs.scale);

  var lg=el("div","proj-legend");
  lg.appendChild(el("span",null,"0% = nothing kept"));
  lg.appendChild(el("span",null,"target 30–80%"));
  lg.appendChild(el("span",null,"100% = source"));
  n.appendChild(lg);
  refs.lead=el("div","proj-lead",""); n.appendChild(refs.lead);
  refs.detail=el("div","proj-detail",""); n.appendChild(refs.detail);
  return {node:n, refs:refs};
}

function updateProj(p, e){
  var r=e.ratio_pct, v=e.verdict||"unknown";
  var cls=PROJ_CLASS[v]==null ? "" : PROJ_CLASS[v];
  var lost=(v==="downscale");
  p.size.textContent=gib(e.projected_bytes);
  p.ratio.textContent=pct(r);
  p.root.className=lost ? "proj lost" : "proj";
  p.ratioWrap.className="proj-ratio "+(lost ? "lost" : cls);
  p.srcCap.textContent=e.source_bytes==null
    ? "source size unknown" : "kept, of "+gib(e.source_bytes)+" source";
  p.lead.className="proj-lead "+cls;
  p.lead.textContent=PROJ_LEAD[v]||"";

  if(r==null){
    p.mark.hidden=true;
    /* Never fill the gap with a guess. Below 5% the projection is dominated by
       studio logos and black frames, which encode to almost nothing. Say which
       of the two gaps this is -- a size with no ratio is not "no projection". */
    p.detail.textContent = e.projected_bytes!=null
      ? "Source size unknown, so the percentage cannot be computed."
      : (e.pct!=null && e.pct<5
          ? "Estimate opens at 5% — logos and black frames flatter it before that."
          : "No projection yet.");
    p.scale.setAttribute("aria-label", e.projected_bytes!=null
      ? "Projected size known, but the ratio to the source cannot be computed"
      : "Projected size not available yet");
    return;
  }
  p.mark.hidden=false;
  p.mark.style.left=Math.max(0,Math.min(100,r))+"%";
  p.mark.className="proj-mark "+(lost ? "bad" : cls);

  var bits=[];
  /* A downscale changed the pixel count, so output/source bytes are not
     comparable -- offering a band position for it would dress up a number
     that means nothing. */
  if(lost){
    bits.push("frame is narrower than the source; the size ratio is not comparable");
  }else{
    bits.push(bandText(r));
    if(e.shrink_pct!=null) bits.push(pct(e.shrink_pct)+" smaller");
    if(e.crop_factor>1.01 && e.norm_ratio_pct!=null)
      bits.push(pct(e.norm_ratio_pct)+" per retained pixel after auto-crop");
    if(e.source_bytes!=null && e.projected_bytes!=null)
      bits.push(gibApprox(e.source_bytes-e.projected_bytes)+" freed when it finishes");
  }
  p.detail.textContent=bits.join(". ")+".";
  p.scale.setAttribute("aria-label", lost
    ? "Resolution was lost; the size ratio is not comparable"
    : "Projected output keeps "+pct(r)+" of the source. Target band is 30 to 80 percent.");
}

/* The live card updates IN PLACE. Rebuilding it on every SSE frame silently
   restarted every CSS animation and defeated the bar's width transition --
   the fill was always a brand-new node, so it could never animate. Structure
   is rebuilt only when the set of running encodes changes; numbers and chips
   update on the nodes already there. Progress lives beside the bar at
   HandBrake's full precision, and only there -- one number, one precision. */
var liveRefs={};
function renderLive(live, s){
  var host=document.getElementById("liveWrap");
  var sig=live.map(function(e){ return e.title; }).join("|");
  if(host.dataset.sig!==sig){
    host.replaceChildren(); liveRefs={}; host.dataset.sig=sig;
    if(!live.length){
      var c=el("div","card");
      c.appendChild(el("div","live-title","Nothing encoding"));
      c.appendChild(el("div","verdict", s.x9_online
        ? "The staging drive is mounted and idle."
        : "The staging drive is not mounted."));
      host.appendChild(c); return;
    }
    live.forEach(function(e){
      var c=el("div","card live"), refs={};
      refs.top=liveChips(e); c.appendChild(refs.top);
      var row=el("div","barrow"), bar=el("div","bar");
      bar.setAttribute("role","progressbar");
      bar.setAttribute("aria-label","Encode progress");
      bar.setAttribute("aria-valuemin","0"); bar.setAttribute("aria-valuemax","100");
      refs.bar=bar; refs.fill=el("i");
      bar.appendChild(refs.fill); row.appendChild(bar);
      refs.pct=el("div","pctbig num","—"); row.appendChild(refs.pct);
      c.appendChild(row);
      var pj=projBlock(); refs.proj=pj.refs; c.appendChild(pj.node);
      var kv=el("div","kv"); refs.kv={};
      liveFields(e).forEach(function(p){
        var d=el("div"); d.appendChild(el("span",null,p[0]));
        var b=el("b",null,"—"); refs.kv[p[0]]=b; d.appendChild(b);
        kv.appendChild(d);
      });
      c.appendChild(kv);
      refs.verdict=el("div","verdict",""); c.appendChild(refs.verdict);
      host.appendChild(c);
      liveRefs[e.title]=refs;
    });
  }
  live.forEach(function(e){
    var refs=liveRefs[e.title]; if(!refs) return;
    var top=liveChips(e); refs.top.replaceWith(top); refs.top=top;
    refs.fill.style.width=(e.pct||0)+"%";
    refs.bar.setAttribute("aria-valuenow", String(e.pct||0));
    refs.pct.textContent=pctLive(e.pct);
    updateProj(refs.proj, e);
    liveFields(e).forEach(function(p){
      var b=refs.kv[p[0]]; if(b) b.textContent=p[1];
    });
    refs.verdict.textContent=e.verdict_note;
    refs.verdict.className=
      (e.verdict==="suspect"||e.verdict==="blowup"||e.verdict==="no-saving"
       ||e.verdict==="downscale")
        ? "verdict loud" : "verdict";
  });
}

function table(cols, rows, build){
  var t=el("table"), thead=el("thead"), tr=el("tr");
  cols.forEach(function(c){
    var th=el("th",[c.n?"n":"",c.cls||""].join(" ").trim()||null,c.label);
    tr.appendChild(th); });
  thead.appendChild(tr); t.appendChild(thead);
  var tb=el("tbody");
  rows.forEach(function(r,i){ tb.appendChild(build(r,i)); });
  t.appendChild(tb); return t;
}

/* The queue is hand-editable: rows drag to reorder (the order you drop is
   the order the pipeline picks from) and any non-encoding row can be
   skipped. Skipped rows keep their place at the BOTTOM, greyed, with a
   restore button -- a skip that vanished would read as "finished". */
/* Two-step confirm shared by start and abort: first click arms the button and
   rewrites its label with the consequence; the second click within 6s acts.
   SSE repaints every 2s would disarm it mid-decision, so an armed control
   pins its .rowacts visible via the armed class and paint() skips rebuilds
   while one is armed (see armedTitle). */
var armedTitle=null;
function arm(b,acts,armedLabel,fire){
  if(b.dataset.armed==="1"){
    b.dataset.armed=""; armedTitle=null;
    b.disabled=true;
    fire();
    return;
  }
  b.dataset.armed="1"; armedTitle=b.dataset.title;
  var plain=b.textContent;
  b.textContent=armedLabel;
  b.classList.add("armed"); acts.classList.add("armed");
  setTimeout(function(){
    if(b.dataset.armed!=="1") return;
    b.dataset.armed=""; armedTitle=null;
    b.textContent=plain;
    b.classList.remove("armed"); acts.classList.remove("armed");
    if(last.pending&&last.state){ last.pending=false; paint(last.state); }
  },6000);
}

function rowActions(r,s){
  var acts=el("span","rowacts");
  if(r.encoding){
    /* Abort kills HandBrake, deletes the partial, and writes a skip so the
       driver cannot immediately restart the same title. The armed label says
       what is being thrown away. */
    var ab=el("button","act","abort");
    ab.type="button"; ab.dataset.title=r.title;
    ab.title="Stop this encode, discard the partial output, and skip the title";
    ab.addEventListener("click",function(){
      arm(ab,acts,"discard the encode so far?",function(){
        api("/api/encode/abort",{title:r.title});
      });
    });
    acts.appendChild(ab);
    return acts;
  }
  if(r.ready && s && s.can_start!==false){
    var sel=el("select","crfsel");
    sel.title="CRF for this encode — 16 is the pipeline default; 22 and 24 are "+
      "outside the auto-retry ladder";
    (s.crf_choices||[16,18,20,22,24]).forEach(function(c){
      var o=el("option",null,"CRF "+c); o.value=String(c);
      if(c===16) o.selected=true;
      sel.appendChild(o);
    });
    /* Interacting with the select must not start a drag on the row. */
    sel.addEventListener("mousedown",function(e){ e.stopPropagation(); });
    var go=el("button","act go","start encode");
    go.type="button"; go.dataset.title=r.title;
    go.title="Start encoding this title now at the chosen CRF";
    go.addEventListener("click",function(){
      arm(go,acts,"start at "+sel.options[sel.selectedIndex].text+"?",function(){
        api("/api/encode/start",{title:r.title,crf:parseInt(sel.value,10)});
      });
    });
    acts.appendChild(sel); acts.appendChild(go);
  }
  if(!r.skipped && !r.staged && r.arriving_bytes==null){
    /* Library-only rows can be pulled onto the staging drive on demand. The
       armed label states the cost up front — this is a multi-GiB transfer. */
    var pull=el("button","act","stage");
    pull.type="button"; pull.dataset.title=r.title;
    pull.title="Copy this title's file from the library to the staging drive now";
    pull.addEventListener("click",function(){
      arm(pull,acts,(r.bytes==null?"pull this title (size unknown) to the X9?"
                    :"pull "+gib(r.bytes)+" to the X9?"),function(){
        api("/api/stage/start",{title:r.title});
      });
    });
    acts.appendChild(pull);
  }
  var b=el("button","act"+(r.skipped?" restore":""),
           r.skipped?"restore":"skip");
  b.type="button";
  b.title=r.skipped
    ? "Put this title back in the queue"
    : "Skip this title — the pipeline moves on to the next one";
  b.addEventListener("click",function(){
    b.disabled=true;
    api("/api/queue/skip",{title:r.title,skipped:!r.skipped})
      .finally(function(){ b.disabled=false; });
  });
  acts.appendChild(b);
  return acts;
}

function renderQueue(q, s, live, xfers, hist){
  var liveCrf={};
  (live||[]).forEach(function(e){
    if(e.crf!=null) liveCrf[(e.folder||e.title).toLowerCase()]=e.crf;
  });
  var pane=document.getElementById("pane"); pane.replaceChildren();
  /* The offline warning renders even when transfers keep the table
     non-empty: an unmounted NAS must never look like a finished job. */
  if(s && s.library_complete===false)
    pane.appendChild(el("div","empty",
      "Library not fully mounted — "+(s.roots_offline||[]).join(", ")+
      " offline. This list is PARTIAL, not empty."));
  if(!q.length && !(xfers&&xfers.length)){
    if(!(s && s.library_complete===false))
      pane.appendChild(el("div","empty","Nothing left above the stop threshold."));
    return; }
  var pinned=q.filter(function(r){ return r.pinned&&!r.skipped; }).length;
  var active=q.filter(function(r){ return !r.skipped; }).length;
  var ranks=[], rn=0;
  q.forEach(function(r,idx){ ranks[idx]=r.skipped?null:++rn; });
  pane.appendChild(table(
    [{label:"",cls:"gripcol"},{label:"Rank",n:true},{label:"SRC Mb/s",n:true,cls:"unit"},
     {label:"Src size",n:true},{label:"CRF",n:true},
     {label:"Title",cls:"title-cell"},{label:"NAS"},{label:"Src folder"},
     {label:"Status"}],
    q, function(r,i){
      /* encoding (amber) beats ready (green) beats skipped. ready is the ONE
         row next_title.py would pick -- server-computed, and absent entirely
         while anything is encoding. */
      var tr=el("tr", r.encoding?"rowenc":(r.ready?"rowready":(r.skipped?"rowskip":null)));
      tr.dataset.title=r.title; tr.dataset.idx=String(i);
      var grip=el("td","gripcol");
      if(!r.skipped){
        var g=el("span","grip","⋮⋮");
        g.title="Drag to reorder"; grip.appendChild(g);
        tr.draggable=true;
      }
      tr.appendChild(grip);
      tr.appendChild(el("td","n q-rank", ranks[i]==null?"—":String(ranks[i])));
      var band=r.mbps>=90?"mbps-hi":(r.mbps>=80?"mbps-mid":"mbps-lo");
      tr.appendChild(el("td","n mono q-mbps "+band,r.mbps.toFixed(1)));
      tr.appendChild(el("td","n mono q-size",gib(r.bytes)));
      /* CRF: the encoding row shows the encoder's ACTUAL value (the ladder
         may have stepped it down); everything else shows the planned start,
         muted, because every encode begins at 16. Skipped rows will not
         encode, so no number is claimed. */
      var lc=liveCrf[r.title.toLowerCase()];
      var crfTd;
      if(r.skipped){ crfTd=el("td","n q-crf","—"); }
      else if(r.encoding && lc!=null){ crfTd=el("td","n q-crf",String(lc)); }
      else{
        crfTd=el("td","n q-crf","16");
        crfTd.title="Planned start — encodes begin at CRF 16 unless started by hand; the ladder may step down";
      }
      tr.appendChild(crfTd);
      var titleTd=el("td","title-cell");
      var cell=el("div","tcell");
      var name=el("span","tname",r.title);
      if(r.skipped) name.className="tname struck";
      cell.appendChild(name);
      cell.appendChild(rowActions(r,s));
      titleTd.appendChild(cell);
      tr.appendChild(titleTd);
      var nasTd=el("td"); nasTd.appendChild(nasMark(r.location));
      tr.appendChild(nasTd);
      tr.appendChild(srcDirTd(r.src_dir,r.title));
      var td=el("td");
      if(r.skipped) td.appendChild(el("span","mark skip","skipped"));
      else{
        if(r.pinned) td.appendChild(el("span","mark pin","pinned"));
        if(r.encoding) td.appendChild(el("span","mark enc","encoding"));
        else if(r.arriving_bytes!=null){
          /* A pull still landing -- replenisher or dashboard, same thing.
             Denominator is the row's own library original. A partial that is
             not moving is "stalled", never a progress bar; one LARGER than
             the source is a stale leftover, not progress. */
          if(r.arriving_stalled||(r.bytes&&r.arriving_bytes>r.bytes)){
            td.appendChild(el("span","mark stall","stalled"));
            td.appendChild(el("span","xfer-pct",
              gib(r.arriving_bytes)+" of "+gib(r.bytes)+" pulled — not moving"));
          }else{
            td.appendChild(el("span","mark xfer","arriving"));
            if(r.bytes){
              var apc=Math.max(0,Math.min(100,r.arriving_bytes/r.bytes*100));
              var abar=el("span","minibar pull"), afill=el("i");
              afill.style.width=apc+"%"; abar.appendChild(afill);
              td.appendChild(abar);
              var atxt=apc.toFixed(1)+"% · "+gib(r.arriving_bytes)+" of "+
                       gib(r.bytes)+" pulled";
              if(r.arriving_rate_bps>0)
                atxt+=" · "+(r.arriving_rate_bps/1e6).toFixed(0)+" MB/s · "+
                     dur((r.bytes-r.arriving_bytes)/r.arriving_rate_bps)+" left";
              td.appendChild(el("span","xfer-pct",atxt));
            }
          }
        }
        else if(r.staged) td.appendChild(el("span","mark staged","staged"));
        else td.appendChild(el("span","mark","library"));
      }
      tr.appendChild(td);
      if(r.pinned && !r.skipped && ranks[i]===pinned && pinned<active)
        tr.classList.add("pin-end");
      return tr;
    }));
  /* A recorded title leaves the queue before its file has finished travelling
     back to the NAS. While the .partial grows in the library folder the title
     gets a synthetic, non-draggable row up top: "transferring" plus a small
     bar. Src size and CRF come from the row just written to the ledger; the
     bitrate column is not on a ledger row, so it stays an honest em dash. */
  var tbl=pane.querySelector("table");
  (xfers||[]).forEach(function(t,ix){
    var led=null;
    (hist||[]).forEach(function(r){ if(r.title===t.title) led=r; });
    var tr=el("tr","rowxfer");
    tr.appendChild(el("td","gripcol"));
    tr.appendChild(el("td","n muted","—"));
    tr.appendChild(el("td","n muted","—"));
    /* Ledger sizes migrated by hand carry exact:false; show them with the
       same "~" the History tab uses rather than as a measurement. */
    tr.appendChild(el("td","n mono",
      led&&led.source_bytes!=null
        ?(led.exact===false?"~":"")+gib(led.source_bytes):"—"));
    var crfTd=el("td","n muted", led&&led.crf!=null?String(led.crf):"—");
    crfTd.title="CRF this encode was recorded at";
    tr.appendChild(crfTd);
    tr.appendChild(el("td","title-cell",t.title));
    var nasTd=el("td"); nasTd.appendChild(nasMark(t.nas)); tr.appendChild(nasTd);
    tr.appendChild(srcDirTd(t.src_dir,t.title));
    var st=el("td");
    var stall=!!t.stalled;
    st.appendChild(el("span","mark "+(stall?"stall":"xfer"),
                      stall?"stalled":"transferring"));
    if(!stall && t.pct!=null){
      var bar=el("span","minibar"), fill=el("i");
      fill.style.width=Math.max(0,Math.min(100,t.pct))+"%";
      bar.appendChild(fill); st.appendChild(bar);
    }
    /* Both operands carry their unit and the sentence names the file being
       moved -- three sizes share this row and only labels keep them apart. */
    var moved=gib(t.done_bytes)+" of "+gib(t.total_bytes)+" copied to "+t.nas;
    var txt = stall ? "no progress — "+moved
            : t.pct!=null ? pct(t.pct)+" · "+moved : moved;
    if(!stall && t.rate_bps>0){
      txt+=" · "+(t.rate_bps/1e6).toFixed(0)+" MB/s · "+
           dur((t.total_bytes-t.done_bytes)/t.rate_bps)+" left";
    }
    st.appendChild(el("span","xfer-pct",txt));
    tr.appendChild(st);
    tbl.tBodies[0].insertBefore(tr, tbl.tBodies[0].rows[ix]||null);
  });
  wireDrag(tbl, q);
}

/* Drag semantics: dropping a row at position K pins the first K+1 visible
   titles as the explicit head of the queue, so the table always encodes in
   exactly the order shown. Everything below the pinned head keeps the
   bitrate ranking. "Reset order" clears the head. */
var drag=null;
function wireDrag(tbl,q){
  var tb=tbl.tBodies[0];
  function clearMarks(){
    Array.prototype.forEach.call(tb.rows,function(r){
      r.classList.remove("dropline","dropline-after"); });
  }
  function rowOf(ev){
    var n=ev.target;
    while(n && n.nodeName!=="TR") n=n.parentNode;
    return n && n.dataset && n.dataset.title!=null ? n : null;
  }
  tb.addEventListener("dragstart",function(ev){
    var tr=rowOf(ev); if(!tr||!tr.draggable) return;
    drag={title:tr.dataset.title};
    tr.classList.add("dragsrc");
    ev.dataTransfer.effectAllowed="move";
    try{ ev.dataTransfer.setData("text/plain",tr.dataset.title); }catch(e){}
  });
  tb.addEventListener("dragend",function(ev){
    clearMarks();
    var tr=rowOf(ev); if(tr) tr.classList.remove("dragsrc");
    drag=null;
    if(last.pending){ last.pending=false; last.key=null;
      if(last.state) paint(last.state); }
  });
  tb.addEventListener("dragover",function(ev){
    if(!drag) return;
    var tr=rowOf(ev); if(!tr||tr.dataset.title===drag.title) return;
    var i=parseInt(tr.dataset.idx,10);
    if(q[i] && q[i].skipped) return;   // no dropping into the skipped zone
    ev.preventDefault(); ev.dataTransfer.dropEffect="move";
    clearMarks();
    var rect=tr.getBoundingClientRect();
    tr.classList.add(ev.clientY>rect.top+rect.height/2 ? "dropline-after" : "dropline");
  });
  tb.addEventListener("drop",function(ev){
    if(!drag) return; ev.preventDefault();
    var tr=rowOf(ev); clearMarks(); if(!tr) return;
    var ti=parseInt(tr.dataset.idx,10);
    if(!q[ti]||q[ti].skipped) return;
    var rect=tr.getBoundingClientRect();
    var after=ev.clientY>rect.top+rect.height/2;
    /* Skipped rows sit inline in the table, so a q index is not an index
       into the active sequence; count the active rows above the drop. */
    var target=0;
    for(var k=0;k<ti;k++) if(!q[k].skipped) target++;
    if(after) target++;
    var order=q.filter(function(r){ return !r.skipped; })
               .map(function(r){ return r.title; });
    var from=order.indexOf(drag.title); if(from<0) return;
    if(target>from) target--;
    order.splice(from,1);
    if(target>order.length) target=order.length;
    order.splice(target,0,drag.title);
    /* Pin the MINIMAL head that reproduces this exact visible order under
       the server sort (priority first, then bitrate): the longest tail that
       is already in descending-bitrate order needs no pinning. Dropping a
       row back into pure bitrate order therefore clears the pins. */
    var mb={}; q.forEach(function(r){ mb[r.title]=r.mbps; });
    var cut=order.length-1;
    while(cut>0 && mb[order[cut-1]]>=mb[order[cut]]) cut--;
    api("/api/queue/order",{order:order.slice(0,cut)});
  });
}

function renderLedger(rows, xfers){
  var pane=document.getElementById("pane"); pane.replaceChildren();
  if(!rows.length){ pane.appendChild(el("div","empty","No encodes recorded yet.")); return; }
  /* "Moved to" is written at record time -- a promise, not an observation.
     While the file is still travelling, show the observed transfer instead
     of presenting the promise as done. */
  var moving={};
  (xfers||[]).forEach(function(t){ moving[t.title]=t; });
  var ordered=rows.slice().reverse();
  pane.appendChild(table(
    [{label:"#",n:true},{label:"Title",cls:"title-cell"},{label:"Original",n:true},{label:"Output",n:true},
     {label:"Saved",n:true},{label:"Shrink",n:true},{label:"CRF",n:true},{label:"Tracks"},{label:"Moved to"},
     {label:"Source of record"},{label:"Finished"}],
    ordered, function(r,i){
      var tr=el("tr");
      tr.appendChild(el("td","n muted",String(ordered.length-i)));
      tr.appendChild(el("td","title-cell",r.title));
      var ap = r.exact===false ? "~" : "";
      tr.appendChild(el("td","n",r.source_bytes?ap+gib(r.source_bytes):"—"));
      tr.appendChild(el("td","n",r.output_bytes?ap+gib(r.output_bytes):"—"));
      tr.appendChild(el("td","n",r.saved_bytes?ap+gib(r.saved_bytes):"—"));
      tr.appendChild(el("td","n",pct(r.saved_pct)));
      tr.appendChild(el("td","n"+(r.crf==null?" muted":""),
        r.crf==null?"—":String(r.crf)));
      tr.appendChild(el("td","muted",
        (r.audio==null?"—":r.audio+"a / "+r.subs+"s")));
      var destTd=el("td");
      if(r.dest){
        var vol=r.dest.split("/")[0];
        destTd.appendChild(nasMark(vol));
        var rest=r.dest.slice(vol.length);
        if(rest) destTd.appendChild(el("span","muted",rest));
      }else destTd.appendChild(el("span","muted","—"));
      var mv=moving[r.title];
      if(mv){
        var stall=!!mv.stalled;
        destTd.appendChild(el("span","mark "+(stall?"stall":"xfer"),
                              stall?"stalled":"transferring"));
        if(!stall && mv.pct!=null){
          var bar=el("span","minibar"), fill=el("i");
          fill.style.width=Math.max(0,Math.min(100,mv.pct))+"%";
          bar.appendChild(fill); destTd.appendChild(bar);
        }
        destTd.appendChild(el("span","xfer-pct",
          (stall?"no progress — ":mv.pct!=null?pct(mv.pct)+" · ":"")+
          gib(mv.done_bytes)+" of "+gib(mv.total_bytes)+" copied"));
      }
      tr.appendChild(destTd);
      tr.appendChild(el("td","muted",RECORD[r.provenance]||r.provenance||"—"));
      tr.appendChild(el("td","muted",(r.finished_at||"—").slice(0,10)));
      if(r.note){ tr.title=r.note; }
      return tr;
    }));
  var noted=[];
  ordered.forEach(function(r,i){
    if(r.note) noted.push((ordered.length-i)+". "+r.title+" — "+r.note);
  });
  if(noted.length){
    var box=el("div","lnotes");
    noted.forEach(function(t){ box.appendChild(el("div",null,t)); });
    pane.appendChild(box);
  }
}

function paint(s){
  renderAlert(s.summary, s.encode_note);
  renderStats(s.summary);
  renderLive(s.live, s.summary);
  /* The key must cover EVERYTHING the pane draws — both tabs render live
     transfer progress, so transfers belong in BOTH keys. They were once
     omitted, and the transferring row painted a single still frame (at
     ~0 bytes) that never advanced for the whole 45-minute push. */
  var xk=(s.transfers||[]).map(function(t){ return [t.title,t.done_bytes,t.stalled]; });
  var key=tab+"|"+JSON.stringify(tab==="queue"
    ? [s.queue, s.live.map(function(e){ return [e.folder, e.crf]; }),
       xk, s.summary.library_complete, s.summary.roots_offline,
       s.can_start, s.encode_note]
    : [s.ledger, xk]);
  if(last.key!==key){
    /* An armed confirm or an active drag must survive the 2s SSE repaint. */
    if(tab==="queue"&&(drag||armedTitle!==null)){ last.pending=true; }
    else{ last.key=key;
      (tab==="queue"?renderQueue(s.queue,
          Object.assign({},s.summary,
            {can_start:s.can_start,crf_choices:s.crf_choices}),
          s.live, s.transfers, s.ledger)
                    :renderLedger(s.ledger, s.transfers)); }
  }
  var nq=s.summary.queue_count!=null?s.summary.queue_count:s.queue.length;
  document.getElementById("tabQueue").textContent="Queue ("+nq+
    (s.summary.queue_skipped ? " · "+s.summary.queue_skipped+" skipped" : "")+")";
  document.getElementById("tabLedger").textContent="History ("+s.ledger.length+")";
  var anyPin=s.queue.some(function(r){ return r.pinned&&!r.skipped; });
  document.getElementById("resetOrder").hidden=!(tab==="queue"&&anyPin);
  document.getElementById("gen").textContent="updated "+s.summary.generated_at;
  document.getElementById("stopnote").textContent=
    "pausing below "+s.summary.stop_mbps+" Mb/s";
}

function setTab(name){
  tab=name; last.key=null;
  document.getElementById("tabQueue").setAttribute("aria-selected", String(name==="queue"));
  document.getElementById("tabLedger").setAttribute("aria-selected", String(name==="ledger"));
  if(last.state) paint(last.state);
}
document.getElementById("tabQueue").addEventListener("click",function(){setTab("queue");});
document.getElementById("tabLedger").addEventListener("click",function(){setTab("ledger");});
document.getElementById("resetOrder").addEventListener("click",function(){
  api("/api/queue/order",{order:[]});
});

/* Seam blanking. Below 700px the title column pins while the rest scrolls,
   and a cell HALF hidden is worse than one fully hidden: sliced at the pane's
   left edge or the pinned title's edge, a right-aligned size keeps its
   trailing digits and still parses as a plausible size; sliced at the right
   edge it keeps its digits but loses its unit. Whichever column straddles a
   boundary gets .cut (blank but layout-stable) until it is fully clear. The
   straddle test uses the text box (cell inset by its padding), so a value
   whose glyphs are fully visible is never blanked. Title cells are never
   cut: a clipped title misleads no one, a missing one identifies nothing.
   Geometry only -- reads no data, writes no text. */
(function(){
  var pane=document.getElementById("pane"), cuts=[], pend=false;
  function applyCut(tbl,i,on){
    for(var r=0;r<tbl.rows.length;r++){
      var c=tbl.rows[r].cells[i];
      if(c) c.classList.toggle("cut",on);
    }
  }
  function recut(){
    var tbl=pane.querySelector("table");
    if(!tbl||!tbl.rows.length){ cuts=[]; return; }
    var head=tbl.rows[0], styles=[], sticky=[], pinnedRight=null, i, rc;
    for(i=0;i<head.cells.length;i++){
      styles[i]=getComputedStyle(head.cells[i]);
      sticky[i]=styles[i].position==="sticky"&&styles[i].left!=="auto";
      if(sticky[i]){
        rc=head.cells[i].getBoundingClientRect();
        if(pinnedRight==null||rc.right>pinnedRight) pinnedRight=rc.right;
      }
    }
    var next=[];
    if(pinnedRight!=null){  /* the pinned title exists only below 700px */
      var pr=pane.getBoundingClientRect();
      var bounds=[pr.left,pinnedRight,pr.left+pane.clientWidth];
      for(i=0;i<head.cells.length;i++){
        if(sticky[i]||head.cells[i].classList.contains("title-cell")) continue;
        rc=head.cells[i].getBoundingClientRect();
        var L=rc.left+parseFloat(styles[i].paddingLeft),
            R=rc.right-parseFloat(styles[i].paddingRight);
        for(var b=0;b<bounds.length;b++){
          if(L<bounds[b]-1&&R>bounds[b]+1){ next.push(i); break; }
        }
      }
    }
    cuts.forEach(function(c){ if(next.indexOf(c)<0) applyCut(tbl,c,false); });
    next.forEach(function(c){ if(cuts.indexOf(c)<0) applyCut(tbl,c,true); });
    cuts=next;
  }
  function schedule(){ if(pend) return; pend=true;
    requestAnimationFrame(function(){ pend=false; recut(); }); }
  pane.addEventListener("scroll",schedule,{passive:true});
  window.addEventListener("resize",schedule);
  new MutationObserver(function(){ cuts=[]; schedule(); })
    .observe(pane,{childList:true});
  schedule();
})();

/* Theme. Three segments: an explicit light/dark choice is stored and always
   wins; "system" CLEARS the stored choice, so the OS preference wins and
   KEEPS winning -- flipping the system theme mid-session moves the page with
   it. The pressed segment shows the choice, not the resolved colour. */
var THEME_KEY="smeltr.theme";
var mql=window.matchMedia?window.matchMedia("(prefers-color-scheme: light)"):null;
function storedTheme(){
  try{ var v=localStorage.getItem(THEME_KEY);
       return (v==="light"||v==="dark")?v:null; }catch(e){ return null; }
}
var themeBtns={light:document.getElementById("themeLight"),
               dark:document.getElementById("themeDark"),
               system:document.getElementById("themeSystem")};
function applyTheme(){
  var mode=storedTheme()||"system";
  var shown=mode==="system" ? (mql&&mql.matches?"light":"dark") : mode;
  document.documentElement.setAttribute("data-theme",shown);
  for(var k in themeBtns)
    themeBtns[k].setAttribute("aria-pressed",k===mode?"true":"false");
}
applyTheme();
if(mql&&mql.addEventListener){
  mql.addEventListener("change",function(){ if(!storedTheme()) applyTheme(); });
}
["light","dark"].forEach(function(name){
  themeBtns[name].addEventListener("click",function(){
    try{ localStorage.setItem(THEME_KEY,name); }catch(e){}
    applyTheme();
  });
});
themeBtns.system.addEventListener("click",function(){
  try{ localStorage.removeItem(THEME_KEY); }catch(e){}
  applyTheme();
});

function conn(state,text){
  document.getElementById("dot").className="dot "+state;
  document.getElementById("connText").textContent=text;
}

var es=new EventSource("/api/stream?t="+encodeURIComponent(token));
es.onopen=function(){ conn("on","live"); };
es.onerror=function(){
  /* The spec permanently CLOSES an EventSource on a non-200 (e.g. a 403 from
     a stale token) -- it will never reconnect, so "reconnecting" would be a
     lie over frozen data. Say so plainly instead. */
  conn("off", es.readyState===EventSource.CLOSED
        ? "disconnected — reload the page" : "reconnecting");
};
var booted=false;
es.onmessage=function(ev){
  try{ var s=JSON.parse(ev.data); }catch(_){ return; }
  last.state=s; conn("on","live"); paint(s);
  if(!booted){ booted=true;
    setTimeout(function(){ document.body.classList.add("booted"); },900); }
};
})();
</script>
</body></html>
"""

# The footer states the page's actual exposure; a LAN-bound page claiming
# "127.0.0.1 only" would be lying about its own reachability. BIND is
# operator-controlled but still HTML-escaped -- the page never interpolates a
# raw value, on principle.
# Off-box the page is read-only for network peers by default; say so unless the
# LAN-writes opt-in is on. (Loopback can always write regardless.)
_scope_text = (" + ".join(BINDS) + " · token required"
               + ("" if LAN_WRITES
                  else " · network peers read-only — writes from this Mac only")
               if LAN_EXPOSED else "127.0.0.1 only")
_SCOPE = html.escape(_scope_text)
PAGE = _PAGE.replace("__NONCE__", NONCE).replace("__SCOPE__", _SCOPE)


def main() -> None:
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
