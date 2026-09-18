"""The Processes tab: every process on this Mac that belongs to smeltr, with
what it is for. Imported ONLY by server.py (test_layering.DASHBOARD_ONLY).

One `ps` per snapshot, classified against RULES in order. A process that
matches no rule is not ours and is not shown -- the tab is "what is active
in this pipeline", not a task manager. Each rule carries the PURPOSE
sentence a person reads at a glance; the detail (which title, which
transfer) is pulled out of the command line where the rule knows how.

Also lists the smeltr LaunchAgents (`launchctl list`): they are the things
that START processes when nothing is running, so an absent driver reads
differently with a loaded watchdog beside it than without.

Honesty rules:
- "Running for" is ps's own etime, rendered as given; never a guess.
- A bare `./.autopilot.sh` is either THE driver or one of its detached
  subshells (a replenish run, a backgrounded sync) -- bash names them all
  the same. The one whose parent is launchd/init and that owns no such
  child is the driver; a copy with a replenisher/sync child is labelled as
  that subshell. Two driver rows is itself a finding (the lock should make
  it impossible) and is flagged, not hidden.
- The sampler, the notifier and the stage pump are THREADS of the
  dashboard, not processes; they render as one row for the server with a
  purpose line that names them.
"""

import os
import re
import subprocess
import time
from typing import Optional

# (pattern on the command line, kind, label, purpose)
RULES = [
    (r"(^|/)HandBrakeCLI(\s|$)", "encode", "HandBrake",
     "The encode itself: re-encoding one title to 4K HEVC. One at a time, ever."),
    (r"\.watch-encode\.sh\b", "watcher", "band watcher",
     "Watches the running encode's projected size; kills and re-rungs a blowup outside the band."),
    (r"\.autopilot\.sh\b", "driver", "driver",
     "The autopilot loop: judges finished encodes, starts the next one, records the ledger."),
    (r"\.replenish-queue\.sh\b", "replenish", "replenish run",
     "One replenish run: picks the next library titles and pulls them into queue/ until it holds 10-18."),
    (r"ops/replenisher\.sh\b", "replenisher", "replenisher",
     "The independent replenisher tick: runs every 60 s so queue/ never drops below 10, whatever the encoder is doing."),
    (r"ops/push-complete\.sh\b", "pusher", "pusher",
     "The independent pusher tick: purges the source beside a finished encode and sends the encode back to its library folder over SSH, largest first, one at a time."),
    (r"\.ssh-xfer\.sh pull\b", "pull", "pull",
     "A file coming onto the staging drive from the NAS over SSH (lands as .partial, renamed when the bytes match)."),
    (r"\.ssh-xfer\.sh push\b", "push", "push",
     "A finished encode travelling back to the NAS over SSH."),
    (r"\.sync-to-library\.sh\b", "sync", "sync",
     "Copy, verify, then replace a library original with its encode. The only thing that deletes."),
    (r"ops/watchdog\.sh\b", "watchdog", "watchdog",
     "Relaunches a driver that is merely absent; sweeps corpses first; never restarts past an unreviewed halt."),
    (r"scan-bitrates\.sh\b", "scan", "bitrate scan",
     "Rebuilds the library bitrate index that ranks the queue (nightly at 03:00, or by hand)."),
    (r"dashboard/server\.py\b|dashboard\.server\b|smeltr\S* (serve|start)\b", "server", "dashboard",
     "This page's server. Its threads: the 1 Hz resource sampler, the stage-pull pump, the notifier."),
    (r"ops/heartbeat|heartbeat\.py\b", "heartbeat", "heartbeat",
     "The hourly idle check that emails/Slacks when the pipeline has gone quiet."),
    (r"dashboard/brief\.py\b|smeltr\S* brief\b", "brief", "morning brief",
     "The 09:00 email: the last 24 h of conversions, the error state, anything of note."),
    (r"sweep_root\.py\b", "sweeper", "root sweeper",
     "Temporary: moves a folder that lands at the X9 root into queue/ the instant its pull completes."),
    (r"(^|/)caffeinate\b", "caffeinate", "caffeinate",
     "Keeps the Mac awake so an overnight encode is never put to sleep."),
]

AGENTS = {
    "com.smeltr.watchdog": "Runs the watchdog every 60 s (relaunch an absent driver).",
    "com.smeltr.scan": "Rebuilds the bitrate index nightly at 03:00.",
    "com.smeltr.replenish": "Ticks the independent replenisher every 60 s (queue/ holds 10-18).",
    "com.smeltr.heartbeat": "Hourly idle check with email/Slack (disabled 2026-09-08; the brief replaced it).",
    "com.smeltr.brief": "Emails the morning brief at 09:00.",
    "com.smeltr.push": "Ticks the independent pusher every 60 s (complete/ -> the NAS, beside the original).",
}

_TITLE = re.compile(r"/([^/]+ \(\d{4}\))(?:/|$)")
# The watcher gets the bare folder NAME as its second argument, no slashes.
_BARE_TITLE = re.compile(r"^\S+ (.+? \(\d{4}\))")
_CACHE = {"at": 0.0, "snap": None}
CACHE_SECONDS = 3.0


def _ps() -> list[tuple[int, int, str, str, str, str]]:
    """(pid, ppid, etime, %cpu, rss_kb, command) for every process."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,%cpu=,rss=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3], parts[4], parts[5]))
        except ValueError:
            continue
    return rows


def _title_of(cmd: str) -> Optional[str]:
    """The `Name (YYYY)` folder named in a command line, if any -- the LAST
    one, because a pull names the library source first and the staging
    destination second, and the destination is the title being staged."""
    hits = _TITLE.findall(cmd)
    return hits[-1] if hits else None


def classify(cmd: str) -> Optional[tuple[str, str, str]]:
    """(kind, label, purpose) for a command line, or None if it is not ours.

    Shell wrappers that merely MENTION one of our scripts (`sh -c "... grep
    autopilot"`, an editor, a `tail -f` on the log) are excluded: the
    pattern must be the thing being run, not a word in an argument.
    """
    low = cmd
    if re.search(r"\b(grep|pgrep|tail|less|vim|nano|cat|ps)\b", low.split(" ", 1)[0] if " " in low else low):
        return None
    for pat, kind, label, purpose in RULES:
        if re.search(pat, cmd):
            return kind, label, purpose
    return None


def _agents() -> list[dict]:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2].startswith("com.smeltr."):
            continue
        pid, status, label = parts
        rows.append({
            "label": label,
            "pid": int(pid) if pid.strip().isdigit() else None,
            "status": status.strip(),
            "purpose": AGENTS.get(label, "A smeltr LaunchAgent."),
        })
    rows.sort(key=lambda r: r["label"])
    return rows


def snapshot(rows: Optional[list] = None, agents: Optional[list] = None) -> dict:
    """{"procs": [...], "agents": [...], "generated_at": ...}. `rows`/`agents`
    are injectable for the tests; production reads ps and launchctl."""
    if rows is None and agents is None:
        now = time.monotonic()
        if _CACHE["snap"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
            return _CACHE["snap"]
    rows = _ps() if rows is None else rows
    agents = _agents() if agents is None else agents
    by_pid = {r[0]: r for r in rows}
    children: dict[int, list] = {}
    for r in rows:
        children.setdefault(r[1], []).append(r)
    procs = []
    for pid, ppid, etime, cpu, rss, cmd in rows:
        c = classify(cmd)
        if not c:
            continue
        kind, label, purpose = c
        # A fork whose parent runs the IDENTICAL command line is bash's copy
        # of itself for a `$(...)` or a `( ... ) &` -- one process to a
        # person, not two. The driver's detached subshells are the one
        # copy kept (below), because they are doing something distinct.
        parent = by_pid.get(ppid)
        if parent and parent[5] == cmd and kind != "driver":
            continue
        detail = None
        if kind == "driver":
            # A driver subshell is the copy whose children are a replenish
            # run or a sync; the real driver has none of those, and its
            # parent is launchd/init (1) or the watchdog.
            kids = [k for k in children.get(pid, []) if re.search(r"replenish-queue|sync-to-library|ssh-xfer", k[5])]
            parent_is_driver = ppid in by_pid and ".autopilot.sh" in by_pid[ppid][5]
            if kids or parent_is_driver:
                what = "replenish" if any("replenish" in k[5] for k in kids) else "sync" if any("sync-to-library" in k[5] for k in kids) else "background"
                kind, label = "driver-sub", "driver subshell"
                purpose = "A detached half of the driver: its %s run, left to finish on its own so the encoder never waits on it." % what
        elif kind in ("encode", "pull", "push", "sync"):
            detail = _title_of(cmd)
        elif kind == "watcher":
            m = _BARE_TITLE.search(cmd.split(".watch-encode.sh", 1)[1].strip()) if ".watch-encode.sh" in cmd else None
            detail = m.group(1) if m else _title_of(cmd)
        elif kind == "caffeinate":
            detail = cmd.split(None, 1)[1] if " " in cmd else None
        procs.append({
            "pid": pid, "ppid": ppid, "kind": kind, "label": label, "purpose": purpose,
            "detail": detail, "elapsed": etime, "cpu": cpu, "rss_kb": int(rss) if str(rss).isdigit() else None,
            "cmd": cmd if len(cmd) <= 160 else cmd[:157] + "...",
        })
    order = {k: i for i, (_, k, _, _) in enumerate(RULES)}
    order["driver-sub"] = order["driver"] + 0.5
    procs.sort(key=lambda p: (order.get(p["kind"], 99), p["pid"]))
    drivers = [p for p in procs if p["kind"] == "driver"]
    for p in drivers:
        p["dup"] = len(drivers) > 1
    snap = {"procs": procs, "agents": agents, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if os.environ.get("SMELTR_PROCS_NOCACHE") is None:
        _CACHE["at"] = time.monotonic()
        _CACHE["snap"] = snap
    return snap
