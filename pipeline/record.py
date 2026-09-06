#!/usr/bin/env python3
"""
Record a finished encode into the Smeltr ledger.

TIMING IS THE WHOLE POINT. This must run while BOTH the source and the output
still sit in the staging folder -- that is the only moment the original's exact
size is knowable. Run it after the sync and the original is already deleted,
which is precisely how "Wanted (2008)" ended up in the ledger with a null
source size that can never be recovered.

Usage:  record.py "Flight (2012)" [--dest Vhagar/F] [--note "..."]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core
from pipeline.core import Entry


def probe_tracks(path: str) -> tuple[int | None, int | None]:
    """Count audio and subtitle streams. None on failure -- never guess 0.

    The return code and empty output BOTH have to be checked. Previously a
    failed ffprobe produced empty stdout, which counted as (0, 0) on each side,
    so the parity gate compared (0,0) == (0,0) and passed on zero evidence --
    on the very check that authorises deleting an original.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=nw=1:nk=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None
    kinds = proc.stdout.split()
    return kinds.count("audio"), kinds.count("subtitle")


def encode_seconds(folder: str, output_name: str) -> int | None:
    """Wall-clock encode time, from the log's start line to the log's mtime."""
    info = core.log_for_output(output_name)
    started = info.get("started_text")
    if not started:
        return None
    import time as _t

    try:
        t0 = _t.mktime(_t.strptime(started, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None
    return int(os.path.getmtime(info["log"]) - t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help='staging folder, e.g. "Flight (2012)"')
    ap.add_argument("--dest", default=None, help="where it was moved, e.g. Vhagar/F")
    # Required: it is the ledger's dedup key. A row without it can never be
    # matched against the library index, so the title stays in the queue
    # forever and can be re-encoded on top of its own output.
    ap.add_argument(
        "--source-path",
        required=True,
        help="full library path of the original (the dedup key)",
    )
    ap.add_argument("--note", default="")
    ap.add_argument(
        "--verified",
        default=None,
        help="evidence that a SUSPECT encode was independently checked",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    d = os.path.join(core.X9, args.folder)
    if not os.path.isdir(d):
        sys.exit(f"no such staging folder: {d}")

    # "._" files are macOS AppleDouble sidecars that appear for every real file
    # on the exFAT staging drive. They match the same globs and made this script
    # see two sources and two outputs.
    files = [f for f in os.listdir(d) if not f.startswith("._")]
    outs = [f for f in files if "2160p HEVC" in f and f.endswith(".mkv")]
    srcs = [
        f
        for f in files
        if f.endswith(core.SOURCE_EXTS)
        and f not in outs
        and not f.endswith(".partial")
    ]
    if len(outs) != 1 or len(srcs) != 1:
        sys.exit(
            f"expected exactly one source and one output in {args.folder}; "
            f"found sources={srcs} outputs={outs}"
        )

    src, out = os.path.join(d, srcs[0]), os.path.join(d, outs[0])

    # LIVENESS GATE, before anything else. Recording a still-growing output
    # writes a permanently wrong size into the ledger -- and that row is the
    # last thing standing between an irreplaceable original and rm.
    for proc in core._ps_handbrake():
        # basename both sides: the autopilot's -o is a full path (see
        # core.log_for_output), so a raw compare never fired this gate -- only
        # the mtime check below stood between a growing output and the ledger.
        if os.path.basename(proc["output_name"]) == outs[0] and core._alive(
            proc["pid"]
        ):
            sys.exit(
                f"refusing to record: HandBrake pid {proc['pid']} is still "
                f"writing {outs[0]}"
            )
    age = time.time() - os.path.getmtime(out)
    if age < 120:
        sys.exit(
            f"refusing to record: {outs[0]} was modified {age:.0f}s ago; "
            "wait for the encode to settle"
        )

    sb, ob = os.path.getsize(src), os.path.getsize(out)

    # A ledger row claiming a saving that did not happen is worse than no row.
    if ob >= sb:
        sys.exit(f"refusing to record: output ({ob}) is not smaller than source ({sb})")

    sa, ss = probe_tracks(src)
    oa, osb = probe_tracks(out)
    if None in (sa, ss, oa, osb):
        sys.exit(
            "refusing to record: ffprobe could not read one of the files, "
            "so track parity is unverified"
        )
    if (sa, ss) != (oa, osb):
        sys.exit(
            f"refusing to record: track parity failed — "
            f"source {sa}a/{ss}s vs output {oa}a/{osb}s"
        )

    info = core.log_for_output(outs[0])

    entry = Entry(
        title=args.folder,
        source_path=args.source_path,
        source_geometry=info.get("source_geometry"),
        output_geometry=info.get("geometry"),
        source_bytes=sb,
        output_bytes=ob,
        audio=oa,
        subs=osb,
        dest=args.dest,
        finished_at=__import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        crf=info.get("crf"),
        encoder=info.get("encoder"),
        encode_seconds=encode_seconds(args.folder, outs[0]),
        note=args.note,
        verified=args.verified,
        provenance="live",
        exact=True,
    )
    saved = sb - ob
    print(
        f"{entry.title}: {sb / core.GIB:.2f} GiB -> {ob / core.GIB:.2f} GiB "
        f"({saved / core.GIB:.2f} GiB saved, {(1 - ob / sb) * 100:.1f}% smaller) "
        f"{oa}a/{osb}s parity OK"
    )
    if args.dry_run:
        print("dry run — nothing written")
        return 0
    core.record(entry)
    print(f"recorded -> {core.LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
