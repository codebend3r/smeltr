"""Event timeline for the dashboard's Events tab — pure observation.

Parses the driver's `.autopilot.log` (every line is stamped
`YYYY-MM-DD HH:MM:SS`) plus the per-title `.watch-*.log` files (the band
ladder's KILLED/COMPLETE lines) into one newest-first list a human can debug
from. Imported ONLY by `dashboard/server.py`; the decision path never loads
this.

Honesty rules, same as the rest of the page:

- A missing timestamp renders as a missing timestamp, never a guess. The one
  exception is the FINAL line of a watch log: the watcher writes it and
  exits, so the file's mtime IS that line's write time. Earlier lines in the
  same file carry no time (`ts: null`) and are ordered by the file's mtime —
  an ordering hint is not a displayed claim.
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
    return [n for n in names
            if n.startswith(".watch-") and n.endswith(".log")
            and not n.startswith("._")]


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
            out.append({"ts": m.group(1),
                        "kind": _KINDS.get(word, "info"),
                        "text": m.group(3), "detail": None,
                        "src": "driver"})
        elif out:
            # Indented-but-stamped ("  tracks OK 4a/6s") or raw shell output
            # ("size guard PASS: …") — detail of the event above.
            frag = m.group(3) if m else line.strip()
            if frag:
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
            if word == "KILLED":
                # The watcher writes KILLED and exits, so for the FINAL line
                # the file's mtime is the write time. Earlier lines claim no
                # time at all.
                ts = stamp if i == len(lines) - 1 else None
                out.append({"ts": ts, "kind": "killed",
                            "text": "KILLED " + " — ".join(parts[1:]),
                            "detail": None, "src": "watcher",
                            "_ord": stamp})
            elif word == "COMPLETE" and len(parts) >= 3:
                out.append({"ts": parts[2], "kind": "complete",
                            "text": "COMPLETE " + parts[1] + " — " +
                                    " — ".join(parts[3:]),
                            "detail": None, "src": "watcher",
                            "_ord": parts[2]})
    return out


def events(limit=DEFAULT_LIMIT):
    """Newest-first merged timeline, at most `limit` rows."""
    rows = []
    for e in _driver_events():
        e["_ord"] = e["ts"]
        rows.append(e)
    rows.extend(_watch_events())
    # Stable sort: equal-ordinal rows keep source order (a watch KILLED with
    # only an mtime hint lands beside the driver lines of the same second).
    rows.sort(key=lambda e: e["_ord"])
    for e in rows:
        del e["_ord"]
    return list(reversed(rows[-limit:]))


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
