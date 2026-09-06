"""Event timeline for the dashboard's Events tab — pure observation.

Parses the driver's `.autopilot.log` (every line is stamped
`YYYY-MM-DD HH:MM:SS`) plus the per-title `.watch-*.log` files (the band
ladder's KILLED/FINAL/COMPLETE lines) into one newest-first list a human can debug
from. Imported ONLY by `dashboard/server.py`; the decision path never loads
this.

Honesty rules, same as the rest of the page:

- A missing timestamp renders as a missing timestamp, never a guess. The one
  exception is the LAST line of a watch log when it is a KILLED or FAILED:
  the watcher writes it and exits, so the file's mtime IS that line's write
  time. Earlier lines in the same file carry no time (`ts: null`) and are
  ordered by the file's mtime — an ordering hint is not a displayed claim.
  COMPLETE and FINAL are exempt because they stamp themselves; FINAL has to,
  because it does NOT exit and stops being the last line within the hour.
- Indented driver lines ("  HandBrake pid 1234") and unstamped shell output
  ("ssh push: …") are DETAIL of the event above them, not events of their
  own — the timeline stays one decision per row.
- No log (X9 unmounted) is an empty list, never an error page.
"""

import os
import re
import time

import pipeline.core as core

LOG_NAME = ".autopilot.log"
# Read at most this much of the log's tail; the file grows forever.
TAIL_BYTES = 256 * 1024
DEFAULT_LIMIT = 250
DETAIL_CAP = 600

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})  (\s*)(.*)$")

# First word of a stamped line -> kind. Kind drives only the chip colour and
# label on the page; an unlisted word is "info", never an error.
_KINDS = {
    "HALTED:": "halted",
    "START": "start",
    "JUDGE": "judge",
    "RECORD": "record",
    "SYNC": "sync",
    "CYCLE": "cycle",
    "PLANNED": "planned",
    "LADDER": "ladder",
    "STALE": "stale",
    "DEFER": "defer",
    "REPLENISH": "replenish",
    "PURGE": "purge",
    "===": "up",
}


def _watch_logs():
    try:
        names = os.listdir(core.X9)
    except OSError:
        return []
    return [
        n
        for n in names
        if n.startswith(".watch-") and n.endswith(".log") and not n.startswith("._")
    ]


def _driver_events():
    path = os.path.join(core.X9, LOG_NAME)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if len(chunk) == TAIL_BYTES and lines:
        lines = lines[1:]  # first line is almost certainly cut mid-way
    out = []
    for line in lines:
        if not line.strip():
            continue
        m = _TS.match(line)
        if m and not m.group(2):
            word = m.group(3).split(" ", 1)[0]
            out.append(
                {
                    "ts": m.group(1),
                    "kind": _KINDS.get(word, "info"),
                    "text": m.group(3),
                    "detail": None,
                    "src": "driver",
                }
            )
        elif out:
            # Indented-but-stamped ("  tracks OK 4a/6s") or raw shell output
            # ("size guard PASS: …") — detail of the event above.
            frag = m.group(3) if m else line.strip()
            if frag:
                # .sync-to-library.sh divides by 1073741824 and prints "GB":
                # the value is GiB. Relabel, or the size guard in front of a
                # deletion reads 7.4% low against History's GiB column.
                if frag.startswith("size guard"):
                    frag = frag.replace(" GB", " GiB")
                d = out[-1]["detail"]
                d = frag if d is None else d + " · " + frag
                out[-1]["detail"] = d[:DETAIL_CAP]
    return out


def _watch_events():
    out = []
    for name in _watch_logs():
        path = os.path.join(core.X9, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        for i, line in enumerate(lines):
            parts = line.split("|")
            word = parts[0]
            if word in ("KILLED", "FAILED"):
                # The watcher writes its last line and exits, so for the
                # FINAL line the file's mtime is the write time — surfaced
                # as APPROXIMATE (the page draws "~", minutes precision).
                # An earlier line of a re-used log claims no time at all.
                # FAILED is a HandBrake that DIED (reboot/kill) — the single
                # most likely reason someone opens this tab.
                final = i == len(lines) - 1
                # The watcher writes `next: Q ${NEXTQ}` (a pre-2026-08-25
                # watcher says `next: CRF ${NEXTCRF}`) and that value is the
                # WORD none-too-small/none-too-big at an end rung, so the line
                # reads "next: Q none-too-small". Matching "next: none-" drew
                # every exhaustion as a routine retry, and matching only the
                # CRF wording stopped drawing them at all once the encoder-
                # aware watcher deployed. Both wordings, one test.
                if word == "KILLED" and (
                    "next: CRF none-" in line or "next: Q none-" in line
                ):
                    # Ladder exhausted under a PRE-2026-09-06 watcher: the
                    # encode was killed and .autopilot.sh writes .error-<title>
                    # and moves on — a human owes that row a decision, so it
                    # must not scan identically to a routine retry.
                    kind = "exhausted"
                else:
                    kind = "failed" if word == "FAILED" else "killed"
                out.append(
                    {
                        "ts": stamp if final else None,
                        "kind": kind,
                        "text": _regib(word + " " + " — ".join(parts[1:])),
                        "detail": None,
                        "src": "watcher",
                        "approx": final,
                        "_ord": stamp,
                    }
                )
            elif word == "FINAL" and len(parts) >= 4:
                # END OF THE LADDER (2026-09-06): no rung left, so the watcher
                # left the encode RUNNING instead of killing it. It carries
                # its OWN stamp (parts[2]) because it does not exit — QUARTER
                # lines follow it, so the file mtime is not its write time and
                # an mtime stamp would decay to "—" within the hour on the one
                # row that records the ladder ending.
                out.append(
                    {
                        "ts": parts[2],
                        "kind": "lastrung",
                        "text": _regib("FINAL " + parts[1] + " — "
                                       + " — ".join(parts[3:])),
                        "detail": None,
                        "src": "watcher",
                        "approx": False,
                        "_ord": parts[2],
                    }
                )
            elif word == "COMPLETE" and len(parts) >= 3:
                tail = " — ".join(parts[3:])
                out.append(
                    {
                        "ts": parts[2],
                        "kind": "complete",
                        "text": _regib(
                            "COMPLETE " + parts[1] + ((" — " + tail) if tail else "")
                        ),
                        "detail": None,
                        "src": "watcher",
                        "approx": False,
                        "_ord": parts[2],
                    }
                )
    return out


def _regib(text: str) -> str:
    """.watch-encode.sh divides bytes by 1073741824 and labels the result
    "GB" — the value is GiB. Relabel so this tab can never be read as
    disagreeing with History about the size of the original being deleted."""
    return text.replace(" GB", " GiB")


def events(limit=DEFAULT_LIMIT):
    """Newest-first merged timeline: {"events": [...], "total": n}.

    `total` counts every (collapsed) event the sources held, so the page can
    say "250 of 266" instead of a bare count that reads as "that is
    everything"."""
    rows = []
    for e in _driver_events():
        e["_ord"] = e["ts"]
        e["approx"] = False
        rows.append(e)
    rows.extend(_watch_events())
    # Stable sort: equal-ordinal rows keep source order (a watch KILLED with
    # only an mtime hint lands beside the driver lines of the same second).
    rows.sort(key=lambda e: e["_ord"])
    # Collapse runs of the identical event. The driver logs its wait reason
    # every 300 s, so a 21 h pause is ~250 copies of one sentence — enough to
    # evict the entire real history from the row budget and render the tab
    # as "nothing has ever happened here". The collapsed row keeps the
    # NEWEST stamp and says how many lines it stands for.
    merged = []
    for e in rows:
        p = merged[-1] if merged else None
        if (
            p is not None
            and p["kind"] == e["kind"]
            and p["text"] == e["text"]
            and p["src"] == e["src"]
        ):
            p["count"] += 1
            p["ts"] = e["ts"] or p["ts"]
            p["approx"] = e["approx"] if e["ts"] else p["approx"]
            p["_ord"] = e["_ord"]
            if e["detail"]:
                p["detail"] = e["detail"]
        else:
            e["count"] = 1
            merged.append(e)
    for e in merged:
        del e["_ord"]
    return {"events": list(reversed(merged[-limit:])), "total": len(merged)}


def rev() -> str:
    """Cheap change marker: the page refetches /api/events only when this
    moves. Log mtimes+sizes, never content."""
    parts = []
    try:
        st = os.stat(os.path.join(core.X9, LOG_NAME))
        parts.append("%d:%d" % (st.st_mtime_ns, st.st_size))
    except OSError:
        parts.append("nolog")
    latest = 0
    for name in _watch_logs():
        try:
            latest = max(latest, os.stat(os.path.join(core.X9, name)).st_mtime_ns)
        except OSError:
            continue
    parts.append(str(latest))
    return "|".join(parts)
