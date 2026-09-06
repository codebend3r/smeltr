#!/usr/bin/env python3
"""What of this repo is running right now, and how long it has been running.

WHY THIS EXISTS
---------------
Answering "what is running?" by hand means a `ps -axo | grep -E` with six
alternations, and the answer that comes back is a wall of full command lines
in which the one thing you wanted -- is the driver up, is anything encoding,
how long has that push been going -- is the hardest part to see. Worse, the
grep is written fresh each time, so it drifts: a run that forgets
`.replenish-queue.sh` reports a quiet machine that is in fact mid-pull.

This is that grep, written down once, with a PURPOSE beside every row and an
age a person can read. It is a pure observer: it runs `ps` and prints. It
starts nothing, kills nothing, and touches neither the staging drive nor the
NAS, so it is safe to run against a live pipeline and safe to run when the
X9 is unmounted.

WHAT COUNTS AS "RELATED TO THIS REPO"
-------------------------------------
Three groups, and the distinction matters because only one of them is ever
worth cleaning up:

  PIPELINE   the driver, HandBrake, the band watcher, the transfers. These
             are detached on purpose -- `ppid 1` is their NORMAL state and
             never means anything is wrong.
  SERVICE    the dashboard, the watchdog, the heartbeat. Also long-lived.
  SESSION    shells and helpers spawned by a Claude Code session working in
             this repo, plus the headless Chrome the visual harness starts.
             These are meant to die with their session.

A SESSION row whose parent is gone is reported `stale` -- its session exited
and left it polling. That is a report, not an action; nothing here kills it.
The check is the parent, not merely `ppid 1`: a live Claude Code shell has a
live `claude` parent, while a deliberately detached job also lands at pid 1,
so `ppid 1` alone would libel the driver as stale.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core  # noqa: E402

PIPELINE, SERVICE, SESSION, OTHER = "pipeline", "service", "session", "other"

# Rows that are pure noise: a headless Chrome forks a gpu/network/renderer
# helper per job, and listing seven rows for one browser buries the driver.
# The parent carries the age and the verdict; the helpers die with it.
SKIP = ("Google Chrome Helper", "--type=renderer", "--type=gpu-process", "--type=utility")

# Ordered: the first matcher that hits names the row. Sorted output follows
# this order too, so the driver is always the first line and stray session
# shells are always the last -- the reading order a person actually wants.
MATCHERS = [
    ("autopilot.sh", PIPELINE, "driver", "encodes, judges, syncs, deletes originals"),
    ("HandBrakeCLI", PIPELINE, "encode in flight", ""),
    ("watch-encode.sh", PIPELINE, "band ladder", "kills or finishes an out-of-band encode"),
    ("sync-to-library.sh", PIPELINE, "library sync", "verifies, then deletes the original"),
    ("replenish-queue.sh", PIPELINE, "queue replenisher", "stages the next titles onto the X9"),
    ("ssh-xfer.sh", PIPELINE, "transfer", ""),
    ("scan-bitrates.sh", PIPELINE, "bitrate index scan", ""),
    ("dashboard/server.py", SERVICE, "dashboard server", ""),
    ("dashboard/heartbeat.py", SERVICE, "heartbeat check", "hourly proof something is encoding"),
    ("watchdog.sh", SERVICE, "watchdog", "relaunches an absent driver"),
    ("smeltr-live-", SESSION, "headless Chrome", "visual harness browser"),
    ("smeltr-visual", SESSION, "headless Chrome", "visual harness browser"),
    ("shell-snapshots", SESSION, "Claude Code shell", ""),
    ("claude-501/-Users-snowball-Developer-git-smeltr", SESSION, "session scratchpad script", ""),
    # Below here: things that live in this repo's working directory but are
    # NOT its work. They are listed because a wedged one explains a lot (a
    # commit editor still open is why `git commit` appears to hang), and they
    # are deliberately unreapable -- see REAPABLE.
    ("COMMIT_EDITMSG", OTHER, "commit editor", ""),
    ("Visual Studio Code", OTHER, "editor", ""),
    ("claude daemon", OTHER, "Claude Code daemon", "the harness itself"),
    ("/claude ", OTHER, "Claude Code session", "the harness itself"),
]

# The ONLY purposes that may ever be called stale. Everything else -- an
# unrecognised helper, an editor, the Claude Code harness -- is reported and
# left alone. An hourly report that told somebody to `kill` the daemon
# running it would be worse than no report, and "I did not recognise this"
# is never evidence that a thing is finished with.
REAPABLE = ("Claude Code shell", "session scratchpad script", "headless Chrome")

# `code -w` blocks git on a marker file it is handed on the command line, and
# git deletes that marker the moment the commit completes. So the marker is a
# DIRECT reading of whether anything is still waiting -- far better than the
# parent-pid guess, which says nothing here. No marker means the commit
# finished and this window is a leftover; the row says which, rather than
# asserting "git is waiting" about a commit that landed a day ago.
WAIT_MARKER_RE = re.compile(r"--waitMarkerFilePath\s+(\S+)")

# A row is only ours if it is one of the matchers above OR it names the repo
# or the staging drive. Kept separate from MATCHERS so an unrecognised helper
# still shows up as a row rather than silently not existing.
OURS = ("smeltr", "Crucial X9", "4K Movies")

RANK = {PIPELINE: 0, SERVICE: 1, SESSION: 2, OTHER: 3}

HEADINGS = {
    PIPELINE: "PIPELINE — the decision path",
    SERVICE: "SERVICES — observers, they steer nothing",
    SESSION: "SESSION — spawned by a Claude Code session in this repo",
    OTHER: "ALSO IN THIS DIRECTORY — not this repo's work",
}


def _ps() -> list:
    """One `ps` for everything: pid, ppid, elapsed, full command."""
    try:
        raw = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in raw.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "etime": parts[2],
                    "cmd": parts[3],
                }
            )
        except ValueError:
            continue
    return rows


def _etime_seconds(etime: str) -> int:
    """`ps` elapsed time -> seconds. Formats are MM:SS, HH:MM:SS, DD-HH:MM:SS."""
    days = 0
    if "-" in etime:
        d, _, etime = etime.partition("-")
        try:
            days = int(d)
        except ValueError:
            return 0
    try:
        parts = [int(p) for p in etime.split(":")]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def human_age(seconds: int) -> str:
    """Coarse, and deliberately so: two units is all anyone reads off this.

    An encode that has been running `2h 14m` is the fact; `2h 14m 09s` is the
    same fact plus a digit that has already changed by the time it is read.
    """
    if seconds < 60:
        return "%ds" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if hours else "%dd" % days


def _handbrake_detail(pid: int, encodes: list) -> str:
    """Title, encoder and rung for a live HandBrake, via core's own parser.

    Reusing `core._parse_ps` rather than re-deriving `-i`/`-o` here is the
    point: folder names contain spaces and parentheses, and this repo has
    already paid for that lesson once.
    """
    for enc in encodes:
        if enc["pid"] != pid:
            continue
        title = os.path.splitext(os.path.basename(enc["output_name"]))[0]
        title = re.sub(r"\s*2160p HEVC\s*$", "", title)
        encoder = enc.get("encoder") or core.DEFAULT_ENCODER
        quality = enc.get("crf")
        if quality is None:
            return title
        scale = "CQ" if encoder != core.DEFAULT_ENCODER else "CRF"
        return "%s · %s %s %g" % (title, encoder, scale, quality)
    return ""


def _commit_editor(cmd: str) -> tuple:
    """(detail, still-waiting) for a `code -w` holding a commit message open.

    Returns `None` for the middle value when the command carries no marker
    path to check -- unknown, and the row is left alone. Only a marker that
    is provably GONE licenses calling this a leftover.
    """
    match = WAIT_MARKER_RE.search(cmd)
    if not match:
        return "editing a commit message", None
    if os.path.exists(match.group(1)):
        return "git is waiting on this to close", True
    return "leftover — the commit it was opened for already finished", False


def _classify(row: dict) -> tuple:
    cmd = row["cmd"]
    for needle, group, purpose, detail in MATCHERS:
        if needle in cmd:
            return group, purpose, detail
    return SESSION, "unrecognised", ""


def collect(rows: "list | None" = None) -> list:
    """Every process belonging to this repo's world, most important first.

    `rows` is the seam the tests drive: classification is the part with a
    safety property worth pinning, and it should not need a live machine in
    a particular state to exercise it.
    """
    rows = _ps() if rows is None else rows
    me = os.getpid()
    alive = {r["pid"] for r in rows}
    by_pid = {r["pid"]: r for r in rows}
    encodes = core._parse_ps("\n".join("%d %s" % (r["pid"], r["cmd"]) for r in rows))

    out = []
    for row in rows:
        cmd = row["cmd"]
        if row["pid"] == me or "current_processes.py" in cmd:
            continue
        if any(n in cmd for n in SKIP):
            continue
        hit = any(n in cmd for n, _, _, _ in MATCHERS) or any(o in cmd for o in OURS)
        if not hit:
            continue
        group, purpose, detail = _classify(row)
        waiting = None
        if purpose == "encode in flight":
            detail = _handbrake_detail(row["pid"], encodes) or detail
        elif purpose == "transfer":
            detail = "pull from the NAS" if " pull" in cmd else "push to the NAS"
        elif purpose == "commit editor":
            detail, waiting = _commit_editor(cmd)

        # Stale is a SESSION-only verdict. A pipeline process at ppid 1 is
        # detached exactly as designed; saying otherwise about the driver is
        # how a report gets somebody to kill the thing that was working.
        parent = by_pid.get(row["ppid"])
        orphaned = row["ppid"] == 1 or row["ppid"] not in alive
        stale = purpose in REAPABLE and orphaned
        if purpose == "commit editor" and waiting is False:
            stale = True
        out.append(
            {
                "pid": row["pid"],
                "ppid": row["ppid"],
                "parent": os.path.basename(parent["cmd"].split(" ", 1)[0]) if parent else "—",
                "group": group,
                "purpose": purpose,
                "detail": detail,
                "seconds": _etime_seconds(row["etime"]),
                "stale": stale,
                "cmd": cmd,
            }
        )
    out.sort(key=lambda r: (RANK[r["group"]], -r["seconds"]))
    return _collapse(out)


def _collapse(procs: list) -> list:
    """Fold repeats of one purpose in the ALSO group into a single `x N` row.

    A Claude Code session is five processes and VS Code is three; spelling
    each one out pushes the driver off the top of a short terminal. The same
    rule the Events tab uses on the driver's repeated wait lines. Only the
    ALSO group folds -- every pipeline process is a distinct fact.
    """
    folded, seen = [], {}
    for proc in procs:
        if proc["group"] != OTHER:
            folded.append(proc)
            continue
        first = seen.get(proc["purpose"])
        if first is None:
            seen[proc["purpose"]] = proc
            proc["count"] = 1
            folded.append(proc)
        else:
            first["count"] += 1
            first["seconds"] = max(first["seconds"], proc["seconds"])
    return folded


def render(procs: list) -> str:
    if not procs:
        return "Nothing from this repo is running — no driver, no encode, no dashboard."

    head = ("PID", "AGE", "PURPOSE", "DETAIL")
    body = [
        (
            str(p["pid"]),
            human_age(p["seconds"]),
            p["purpose"]
            + (" (stale)" if p["stale"] else "")
            + (" x %d" % p["count"] if p.get("count", 1) > 1 else ""),
            p["detail"],
        )
        for p in procs
    ]
    widths = [max(len(r[i]) for r in [head] + body) for i in range(4)]

    def line(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    lines = [line(head), "  ".join("-" * w for w in widths)]
    group = None
    for proc, cells in zip(procs, body):
        if proc["group"] != group:
            group = proc["group"]
            lines.append("")
            lines.append(HEADINGS[group])
        lines.append(line(cells))

    stale = [p for p in procs if p["stale"]]
    if stale:
        lines.append("")
        # Deliberately not "parent session exited": one of the two staleness
        # proofs is the commit marker, and a footer that names only the other
        # would be describing the wrong evidence for that row.
        lines.append(
            "%d stale — nothing is waiting on these any more. Clean up with:" % len(stale)
        )
        lines.append("  kill %s" % " ".join(str(p["pid"]) for p in stale))
    return "\n".join(lines)


def main() -> int:
    procs = collect()
    print(render(procs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
