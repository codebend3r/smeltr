#!/usr/bin/env python3
"""Print the encoder and quality a title should encode with, for the driver.

    smeltr encoder "Warfare (2025)"   ->   vt_h265_10bit 60
    smeltr encoder "Timecop (1994)"   ->   x265_10bit 16

One line on stdout, always exactly two tokens, always exit 0 on a readable
title. The driver captures this with $(...) inside start_encode(); a crash or
a chatty stdout here becomes HandBrake flags, so this stays as small as it
looks. Every failure path inside core.encoder_for() already answers the x265
default -- this wrapper adds nothing but the print.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: smeltr encoder <folder>", file=sys.stderr)
        return 2
    enc, q = core.encoder_for(sys.argv[1])
    print(f"{enc} {q}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
