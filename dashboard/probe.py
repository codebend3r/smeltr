"""Process probes: who is on the wire, by `pgrep` and `ps`.

Shared by the encode controls and the stage engine, so it lives apart from
both. Dashboard only.
"""

import re
import subprocess

from dashboard import procs


def slug_of(title: str) -> str:
    """Byte-identical to .autopilot.sh slug_of(): lowercase, alnum only, 20."""
    return re.sub(r"[^a-z0-9]", "", title.lower())[:20]


def pgrep(pattern: str) -> list:
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.stdout.split() if x.isdigit()]


def pgrep_cmds(pattern: str) -> list:
    """Command lines of the processes `pattern` matches, not just their pids.

    `ps`, NOT `pgrep -a`: the listing flags diverge between platforms and the
    divergence is silent. BSD `pgrep -a` is not GNU's "print the command
    line" -- on this Mac `pgrep -af` prints bare pids, so the parse below
    found no command, answered [], and _wire_busy_locked() read a live
    replenisher as a STALE lock (caught 2026-09-07, before it shipped).
    `ps -axo pid=,command=` means the same thing everywhere and is what
    dashboard/procs.py already runs.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    cmds = []
    for line in out.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        cmd = cmd.strip()
        if not pid.isdigit() or not cmd:
            continue
        # Our own `ps` is not a match, and neither is a grep that merely
        # mentions the script -- the same exclusion procs.classify() makes.
        head = cmd.split(" ", 1)[0]
        if re.search(r"\b(grep|pgrep|tail|less|vim|nano|cat|ps)\b", head):
            continue
        if rx.search(cmd):
            cmds.append(cmd)
    return cmds


def wire_title(pattern: str, pids_pattern: str = ""):
    """(busy, title) for the first process matching `pattern`.

    The title is the staging DESTINATION -- the last `Name (YYYY)` on the
    command line -- read through the SAME procs._title_of the Processes tab
    uses, so the two surfaces can never name a transfer differently.

    If the command lines cannot be read but PIDS can, the answer is
    busy-and-unnamed, never idle. A hold whose reason we cannot phrase is
    still a hold; answering "idle" there starts a SECOND transfer on a wire
    that already has one.
    """
    cmds = pgrep_cmds(pattern)
    if not cmds:
        return bool(pgrep(pids_pattern or pattern)), None
    for cmd in cmds:
        t = procs._title_of(cmd)
        if t:
            return True, t
    return True, None
