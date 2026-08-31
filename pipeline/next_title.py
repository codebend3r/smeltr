#!/usr/bin/env python3
"""
Print the next title to encode, or nothing if the stop condition is reached.

Deliberately NOT derived from the rendered report. The first version of the
unattended driver scraped `smeltr report --queue` with awk and took column 2 --
which is RANK, not bitrate. Every value compared below the 70 Mb/s threshold, so
the driver would have concluded the job was finished and exited cleanly with 110
titles still queued. Structured data, not table scraping.

Exit codes: 0 a title was printed | 1 stop condition | 2 library incomplete
            3 wait, don't exit: paused from the dashboard, staged candidates
              hand-skipped, or a replenish pull still landing
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core


def main() -> int:
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else core.STOP_MBPS

    offline = core.offline_roots()

    # NOT the stop condition: the user asked for the CPU back, not for the
    # job to end. Exit 3 is the driver's wait-and-recheck path, so removing
    # the flag resumes without touching the driver. Checked FIRST: pause is
    # the one sanctioned reason to withhold an encode, so nothing below --
    # including a perfectly pickable staged title -- may outrank it. While
    # also blind, both facts are stated (the operator chose one, the other
    # is a sensor state the dashboard banners regardless).
    if core.paused():
        msg = "paused from the dashboard; waiting"
        if offline:
            msg += (" (and library incomplete: "
                    f"{', '.join(core.volume_name(r) for r in offline)}"
                    " not mounted)")
        print(msg, file=sys.stderr)
        return 3

    # The pick itself lives in core.pick_next -- ONE implementation, shared
    # with the dashboard's next_up/ready row, so the two can never disagree.
    # It runs BEFORE the offline check on purpose: a staged title encodes
    # from the X9 and needs nothing from the NAS, so an unreachable root
    # must not idle the CPU while staged work exists ("no gap between
    # encodes" -- the operator's standing requirement; only the sync side
    # waits for the NAS, and the driver defers that separately). queue()
    # keeps a staged row whose root is offline for exactly this reason.
    row, waits = core.pick_next(core.queue_cached(min_mbps=threshold))
    if row is not None:
        print(row["title"])
        return 0

    # Never make a "nothing left to do" decision while blind. An unreachable
    # NAS empties the rest of the queue, which is indistinguishable from
    # having finished -- so with nothing staged to encode, report blindness,
    # never the stop condition.
    if offline:
        print(f"library incomplete: {', '.join(core.volume_name(r) for r in offline)} "
              f"not mounted", file=sys.stderr)
        return 2

    if waits:
        # NOT the stop condition: work remains. Name WHICH wait it is -- the
        # driver logs this line verbatim, and a guessed message here once
        # sent the operator debugging overrides that were fine. .get(w, w):
        # a reason core.pick_next learns later logs its own name and still
        # waits -- a KeyError would exit non-zero, which the driver reads as
        # the stop condition. No universal quantifiers: the reasons compose,
        # and "every title is skipped" is false beside an arriving one.
        msgs = {"arriving": "a replenish pull is still landing",
                "skipped": "staged titles are hand-skipped"}
        print("; ".join(msgs.get(w, w) for w in sorted(waits)) + " -- waiting",
              file=sys.stderr)
        return 3
    print(f"stop condition: nothing staged above {threshold:.0f} Mb/s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
