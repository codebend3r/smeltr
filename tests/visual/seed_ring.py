#!/usr/bin/env python3
"""Write a synthetic 7 d `sysmon.ring` into a directory, for visual checks.

    python3 tests/visual/seed_ring.py <dir> [--depth-seconds N]

The shooter points a throwaway dashboard at that directory (SMELTR_DIR), so
the charts render a full week WITHOUT touching the live ring beside the
ledger -- the one the running server is writing to every second.

The shape is deliberate, not noise: a daily encode/idle rhythm so the day
boundaries are visible at the 7 d stop, a one-second spike every hour so
decimation can be judged by eye at every zoom, two sampler outages so the
gap rule is visible, and an unreadable GPU for one stretch so the em-dash
path renders. `--depth-seconds` seeds a PARTIAL ring, which is what puts the
--skel wash and the 0 baseline on screen.
"""

import argparse
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from dashboard import sysmon  # noqa: E402

NAN = float("nan")


def sample(ts: int, day_phase: float):
    """One second of plausible telemetry. Bounded, so the axes stay sane."""
    minute = ts // 60
    # A long encode pins the CPU; the idle stretches between are quiet.
    busy = 0.5 + 0.5 * math.sin(day_phase * 2 * math.pi)
    cpu = 12 + 85 * busy + 6 * math.sin(ts / 7.0)
    gpu = 8 + 30 * busy + 10 * math.sin(ts / 11.0)
    ram = 55 + 12 * busy + 2 * math.sin(ts / 601.0)
    # Network moves in bursts (the push back to the NAS), disk follows it.
    burst = 1.0 if (minute % 47) < 6 else 0.05
    net_in = 60e3 * burst * (1 + 0.4 * math.sin(ts / 3.0))
    net_out = 42e6 * burst * (1 + 0.3 * math.sin(ts / 5.0))
    disk_r = 9e6 * busy * (1 + 0.5 * math.sin(ts / 13.0))
    disk_w = 26e6 * burst * (1 + 0.2 * math.sin(ts / 2.0))
    # One 1 s spike per hour: it must survive decimation into the min/max
    # band at every stop, which is the whole point of the band.
    if ts % 3600 == 0:
        cpu, net_out, disk_w = 100.0, 118e6, 240e6
    # A stretch where the GPU is unreadable -- absent line, em dash legend.
    if 40000 < (ts % 86400) < 47000:
        gpu = None
    vals = [cpu, gpu, ram, net_in, net_out, disk_r, disk_w]
    return [None if v is None else max(0.0, float(v)) for v in vals]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument(
        "--depth-seconds",
        type=int,
        default=sysmon.SLOTS,
        help="how much history to write (default: the whole ring)",
    )
    args = ap.parse_args()

    depth = max(1, min(args.depth_seconds, sysmon.SLOTS))
    now = int(time.time())
    os.makedirs(args.directory, exist_ok=True)
    path = os.path.join(args.directory, sysmon.RING_NAME)

    # Build the whole file in memory and write it once. Ring.write() is a
    # pwrite per second, and 604800 syscalls turns a 2 s seed into a minute.
    buf = bytearray(sysmon.SLOTS * sysmon.SLOT_BYTES)
    # Two sampler outages, so a GAP is visible next to the flat baseline and
    # the two cannot be confused by eye.
    holes = (
        (now - 5 * 3600, now - 5 * 3600 + 900),
        (now - 40 * 3600, now - 40 * 3600 + 7200),
    )
    written = 0
    for ts in range(now - depth + 1, now + 1):
        if any(lo <= ts < hi for lo, hi in holes):
            continue
        vals = sample(ts, ((ts % 86400) / 86400.0))
        packed = sysmon.SLOT.pack(ts, *[NAN if v is None else v for v in vals])
        off = (ts % sysmon.SLOTS) * sysmon.SLOT_BYTES
        buf[off : off + sysmon.SLOT_BYTES] = packed
        written += 1

    with open(path, "wb") as fh:
        fh.write(sysmon.MAGIC)
        fh.write(buf)
    os.chmod(path, 0o600)
    print(
        f"{path}: {written} samples, {depth} s deep "
        f"({struct.calcsize('<I7f')} B/slot, {os.path.getsize(path) >> 20} MiB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
