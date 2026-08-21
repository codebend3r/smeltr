#!/usr/bin/env python3
"""
Print the next title to encode, or nothing if the stop condition is reached.

Deliberately NOT derived from the rendered report. The first version of the
unattended driver scraped `smeltr report --queue` with awk and took column 2 --
which is RANK, not bitrate. Every value compared below the 70 Mb/s threshold, so
the driver would have concluded the job was finished and exited cleanly with 110
titles still queued. Structured data, not table scraping.

Exit codes: 0 a title was printed | 1 stop condition | 2 library incomplete
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core


def main() -> int:
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else core.STOP_MBPS

    # Never make a "nothing left to do" decision while blind. An unreachable NAS
    # empties the queue, which is indistinguishable from having finished.
    offline = core.offline_roots()
    if offline:
        print(f"library incomplete: {', '.join(core.volume_name(r) for r in offline)} "
              f"not mounted", file=sys.stderr)
        return 2

    for row in core.queue_cached(min_mbps=threshold):
        if not row["staged"]:
            continue
        d = os.path.join(core.X9, row["title"])
        try:
            files = [f for f in os.listdir(d) if not f.startswith("._")]
        except OSError:
            continue
        if any("2160p HEVC" in f and f.endswith(".mkv") for f in files):
            continue          # already encoded, awaiting sync
        print(row["title"])
        return 0

    print(f"stop condition: nothing staged above {threshold:.0f} Mb/s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
