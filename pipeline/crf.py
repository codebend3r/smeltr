#!/usr/bin/env python3
"""
Print the CRF the named title's next encode should START at.

`.autopilot.sh` asks this immediately before it spawns HandBrake, so that a
CRF chosen on the dashboard for a title still sitting in the queue is the CRF
the driver actually uses. Without it the picker would be a lie on every row
the driver starts -- which is nearly all of them.

Deliberately a separate entry point rather than an extra line on `smeltr
next`: next_title.py's stdout is captured with $(...) and IS the folder name.
Anything else printed there is prepended to a path.

Always prints exactly one integer and exits 0, including for a title with no
override, an unreadable overrides file, or a title that is not in the queue at
all. The driver substitutes this straight into `-q`, and the safe answer to
every failure is the value it would have used before this existed. Failing
loudly here would idle the CPU over a preference.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        crf = core.planned_crf(title) if title else core.CRF_DEFAULT
    except Exception:  # noqa: BLE001 - see the module docstring: never idle
        crf = core.CRF_DEFAULT
    print(crf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
