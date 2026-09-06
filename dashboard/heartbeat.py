#!/usr/bin/env python3
"""Hourly proof that the encoder is actually working, and a loud flag when it is not.

WHY THIS EXISTS
---------------
The operator's standing rule is that the only sanctioned gap between one
encode finishing and the next starting is the dashboard's pause toggle.
Everything else in this repo enforces that from the INSIDE -- the driver no
longer halts, the watchdog relaunches an absent driver, the band ladder stops
killing at the terminal rung. Each of those is a component reporting on
itself, and every outage this job has actually had was a component that had
stopped being able to report:

  2026-08-25  a stale .autopilot.lock defeated every relaunch path; the
              watchdog logged RESTART FAILED for six hours with nothing running.
  2026-08-31  a NAS blip at the exact second the judge ran; the driver HALTED
              and idled the CPU for 4h16m with nine staged titles waiting.
  2026-09-04  a decoder-error verdict halted the driver at 04:05 and it sat
              five hours until a human looked.
  2026-09-06  Skyscraper had been staged and unencodable for three days
              because every source lookup spelled `.mkv` and its remux is
              a `.mp4`. Nothing went red. It was simply skipped over.

The last one is the case this file is really for. It produced NO error, NO
halt and NO alert -- the pipeline believed it was healthy the whole time.
An outside observer that only asks "is HandBrake running right now" would
have caught it on the first hour.

WHAT IT CHECKS
--------------
One question: is a HandBrakeCLI process running. That probe is `ps -axo`, so
it works even when this process cannot read /Volumes -- which matters,
because a LaunchAgent without Full Disk Access reads the staging drive as
empty and the watchdog's own history shows how convincingly that impersonates
a finished job. Everything else this file reports is DIAGNOSIS, gathered
after the fact to tell the operator where to look, and every part of it
degrades to "could not tell" rather than to a confident wrong answer.

TWO SAMPLES, NOT ONE
--------------------
The driver is concurrent but not instantaneous: there is a real gap of a few
seconds between one encode finishing and the next spawning, and an hourly
check that landed inside it would cry wolf. So an idle reading is confirmed
by a second reading CONFIRM_SECONDS later, the same strike-and-confirm rule
`.watch-encode.sh` uses before it kills an encode. Two idle samples separated
by a minute is not a gap between encodes.

This module is a pure OBSERVER. It reads processes, the queue and the pause
flag; it starts nothing, kills nothing, and writes nothing except its own
alert. `tests/test_layering.py` lists it DASHBOARD_ONLY -- the decision path
must never import it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import notify  # noqa: E402
from pipeline import core  # noqa: E402

# Seconds between the first idle reading and the confirming one. A minute is
# comfortably longer than the driver's own encode-to-encode handoff (the log
# shows ~10 s: START, then the 120 s track gate runs in the background) and
# comfortably shorter than an hour.
CONFIRM_SECONDS = 60

# The states this can report, worst first. The ORDER is the diagnosis: each
# one is a different thing for a person to go and do, so the first that
# applies is the one the message leads with.
BLIND = "blind"  # cannot read the staging drive at all
NO_DRIVER = "no-driver"  # nothing is driving the pipeline
WEDGED = "wedged"  # a title is ready to start and nothing started it
PAUSED = "paused"  # the dashboard's pause flag is set
WAITING = "waiting"  # staged work exists but none of it is startable
STOPPED = "stopped"  # nothing left above the stop threshold
ENCODING = "encoding"  # all is well


def _handbrakes() -> list:
    """Running HandBrakeCLI processes. `ps`, not the staging drive: this is the
    one reading that must stay true when the volume is unreadable."""
    return core._ps_handbrake()


def _describe_live() -> str:
    """What is encoding, for the all-clear line. Best effort -- the encoder,
    quality and title come from parsing the command line, and a parse that
    comes back empty must not turn a healthy check into an alert."""
    try:
        live = core.live_encodes()
    except Exception:  # noqa: BLE001 - diagnosis may never fail the check
        return "an encode is running"
    if not live:
        return "an encode is running"
    e = live[0]
    title = e.get("folder") or e.get("title") or "?"
    enc = e.get("encoder") or core.DEFAULT_ENCODER
    scale = "CQ" if str(enc).startswith("vt") else "CRF"
    q = e.get("crf")
    # HandBrake's -q is parsed as a float, and "CQ 60.0" beside "CRF 14.0"
    # reads as a precision this ladder does not have: every rung on both
    # scales is an integer.
    if isinstance(q, float) and q.is_integer():
        q = int(q)
    at = f" at {scale} {q}" if q is not None else ""
    pct = e.get("pct")
    prog = f", {pct:.1f}%" if isinstance(pct, (int, float)) else ""
    return f"{title} on {enc}{at}{prog}"


def diagnose() -> tuple:
    """(state, sentence) for a machine with no HandBrake running.

    Ordered by what the operator would have to DO about it. Every branch is
    reachable without the staging drive except the ones that say so, and the
    blind branch is FIRST for the reason CLAUDE.md gives about the watchdog:
    a check that cannot see the drive reports blindness, never "finished".
    """
    if not os.path.isdir(core.X9):
        return BLIND, (
            f"The staging drive is not readable at {core.X9}. Nothing can encode "
            "until it is back. If this is the hourly LaunchAgent, check that "
            "/bin/bash has Full Disk Access — a blind agent sees an empty drive, "
            "not an error."
        )

    if not _driver_running():
        return NO_DRIVER, (
            "No autopilot.sh process is running, so nothing will start the next "
            "encode. Relaunch it (see CLAUDE.md's restart line) or press play on "
            "the dashboard."
        )

    if core.paused():
        return PAUSED, (
            "The pipeline is paused from the dashboard. That is a deliberate "
            "state, not a fault — but the encoder has been idle for at least an "
            "hour because of it. Resume when you are ready."
        )

    try:
        rows = core.queue()
        row, reasons = core.pick_next(rows)
    except Exception as e:  # noqa: BLE001 - a broken scan is itself the news
        return BLIND, (
            f"Could not read the queue to work out why nothing is encoding: {e}"
        )

    if row is not None:
        # The worst case, and the one worth waking up for: everything the
        # driver needs is in place and it still is not encoding.
        return WEDGED, (
            f"{row['title']} is staged and ready to start, the driver is running, "
            "and nothing is encoding. That combination should not happen — the "
            "driver is stuck. Check the tail of .autopilot.log."
        )

    if reasons:
        why = {
            "arriving": "the only staged titles are still being copied onto the drive",
            "errored": "every staged title has errored out and is waiting for you "
            "to clear its marker (see the Errors tab)",
            "skipped": "every staged title is hand-skipped",
        }
        parts = [why.get(r, r) for r in sorted(reasons)]
        return WAITING, (
            "Nothing is encoding because " + ", and ".join(parts) + "."
        )

    active = [r for r in rows if not (r.get("error") or r.get("skipped"))]
    if not active:
        return STOPPED, (
            f"Nothing is left above the {core.STOP_MBPS:.0f} Mb/s stop threshold. "
            "The job may genuinely be finished — but confirm the library is fully "
            "mounted before believing it."
        )
    return WAITING, (
        f"{len(active)} titles are still queued but none of them is on the staging "
        "drive yet. The replenisher should be pulling; check .autopilot.log."
    )


def _driver_running() -> bool:
    """True when an autopilot.sh process exists. Deliberately its own reading
    rather than a queue field: this must work with the drive unmounted."""
    import subprocess

    try:
        raw = subprocess.run(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        "autopilot.sh" in line and not line.lstrip().startswith("grep")
        for line in raw.splitlines()
    )


def check(confirm_seconds: int = CONFIRM_SECONDS, sleep=time.sleep) -> dict:
    """One heartbeat. Returns {state, ok, text, confirmed}.

    An idle FIRST reading is not an alert. The driver hands off between
    encodes in seconds, and an hourly probe that happened to land in that
    window would flag a perfectly healthy pipeline — the same false positive
    the band ladder's two-consecutive-ticks rule exists to prevent. So idle
    is re-read after confirm_seconds and only a second idle reading counts.
    """
    if _handbrakes():
        return {
            "state": ENCODING,
            "ok": True,
            "text": _describe_live(),
            "confirmed": True,
        }
    # Diagnose BEFORE the wait as well as after: "ready and not started" is
    # much more convincing when it was true a minute apart, and the wait is
    # long enough for the queue to have changed underneath us.
    first_state, _ = diagnose()
    if confirm_seconds > 0:
        sleep(confirm_seconds)
        if _handbrakes():
            return {
                "state": ENCODING,
                "ok": True,
                "text": _describe_live() + " (started during the confirming wait)",
                "confirmed": True,
            }
    state, text = diagnose()
    if confirm_seconds > 0 and state != first_state:
        # Two different explanations a minute apart means something is moving.
        # Report the later one and say so rather than pretending to certainty.
        text += (
            f" (this changed during the check — it read '{first_state}' a minute "
            "earlier, so the pipeline may be in transition)"
        )
    return {"state": state, "ok": False, "text": text, "confirmed": True}


def alert(result: dict, err=print) -> int:
    """Send the flag through every configured notify channel.

    Reuses dashboard/notify.py's transports and notify.json so this cannot
    become a second, differently-configured alerting path that works on the
    day the real one does not. It deliberately does NOT touch notify.cursor:
    that file is the event notifier's seen-set, and writing it here would
    silently swallow encode events.
    """
    cfg = notify.load_config(os.path.join(core.SMELTR_DIR, notify.CONFIG_NAME))
    if cfg is None:
        err(
            "smeltr heartbeat: NOTHING IS ENCODING and there is no usable "
            f"{notify.CONFIG_NAME}, so this could not be sent anywhere. "
            f"{result['text']}"
        )
        return 1
    note = {
        "what": "idle",
        "title": "nothing is encoding",
        "text": result["text"],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "detail": None,
    }
    failed = 0
    for name, (send, fmt) in (
        ("slack", (notify.send_slack, notify.slack_message)),
        ("email", (notify.send_email, notify.email_message)),
    ):
        if name not in cfg:
            continue
        try:
            send(cfg[name], fmt(note))
        except Exception as e:  # noqa: BLE001 - try every channel, then report
            err(f"smeltr heartbeat: {name} FAILED — {e}")
            failed += 1
    return failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="smeltr heartbeat",
        description="Flag immediately if nothing is encoding.",
    )
    ap.add_argument(
        "--confirm-seconds",
        type=int,
        default=CONFIRM_SECONDS,
        help="re-read after this long before calling an idle encoder idle "
        "(0 disables the second reading; only do that in tests)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the verdict, send nothing",
    )
    args = ap.parse_args(argv)

    result = check(confirm_seconds=max(0, args.confirm_seconds))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if result["ok"]:
        # One line an hour on stdout, so the log is a record of the job being
        # alive rather than only a record of its failures.
        print(f"{stamp}  OK — {result['text']}")
        return 0
    print(f"{stamp}  ALERT [{result['state']}] — {result['text']}", file=sys.stderr)
    if args.dry_run:
        return 1
    alert(result)
    return 1


if __name__ == "__main__":
    sys.exit(main())
