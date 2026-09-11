#!/usr/bin/env python3
"""
Final verdict for a FINISHED encode, as an exit code the shell can branch on.

This exists so the unattended pipeline and the dashboard cannot disagree about
whether an encode is safe to sync. Both call the same core._verdict(), against
the same ledger-derived baseline. A second implementation in bash would drift,
and the thing it would drift on is whether to delete an irreplaceable original.

Exit codes:
  0  good       -- proceed: record, sync, delete the original
  2  halt       -- suspect / thin / downscale: a human must look at this
  3  ladder     -- no-saving / blowup: should have been auto-killed; retry lower
  4  error      -- could not evaluate (missing files, unreadable log)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core

# Auto-killed by `.watch-encode.sh` and retried one CRF rung lower. Every
# OTHER non-good word falls through to 2 and stops the driver, including a
# word this file has never heard of -- an unrecognised verdict must never
# be able to authorise a deletion.
LADDER = {"no-saving", "blowup"}


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: verdict.py "<staged folder>"', file=sys.stderr)
        return 4
    folder = sys.argv[1]
    d = core.folder_dir(folder)
    if not os.path.isdir(d):
        print(json.dumps({"error": f"no such staged folder: {d}"}))
        return 4

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
        print(
            json.dumps(
                {
                    "error": "expected exactly one source and one output",
                    "sources": srcs,
                    "outputs": outs,
                }
            )
        )
        return 4

    src, out = os.path.join(d, srcs[0]), os.path.join(d, outs[0])

    # An encode still running, or one that died mid-write, leaves an output that
    # looks like a finished file and scores absurdly well. Refuse to judge until
    # HandBrake says it finished.
    for proc in core._ps_handbrake():
        # basename both sides: the autopilot's -o is a full path (see
        # core.log_for_output), so a raw compare never fired this gate.
        if os.path.basename(proc["output_name"]) == outs[0] and core._alive(
            proc["pid"]
        ):
            print(json.dumps({"error": "encode still running", "pid": proc["pid"]}))
            return 4

    sb, ob = os.path.getsize(src), os.path.getsize(out)
    if not sb:
        print(json.dumps({"error": "source is zero bytes"}))
        return 4

    # Geometry comes from the encode's own log, so the crop factor is the real
    # one rather than an assumption.
    info = core.log_for_output(outs[0])

    if not info:
        print(json.dumps({"error": f"no HandBrake log found for {outs[0]}"}))
        return 4
    log_text = core._read_slice(info["log"], 131072, from_end=True)
    if "Encode done" not in log_text and "Finished work" not in log_text:
        print(
            json.dumps(
                {"error": "log shows no completion marker; encode died mid-write"}
            )
        )
        return 4

    src_geom, out_geom = info.get("source_geometry"), info.get("geometry")
    ratio = ob / sb * 100.0
    norm = ratio * core.crop_factor(src_geom, out_geom)
    # Judge against SAME-encoder history only. A LEDGER row with no recorded
    # encoder is x265 (history_ratios maps it to core.LEGACY_ENCODER); a log
    # whose encoder line did not parse is judged as the current default. With
    # no same-encoder baseline the relative check has no base and only the
    # absolute floor applies -- the no-baseline `suspect` gate was removed
    # 2026-09-06 (see core._verdict()).
    enc = info.get("encoder") or core.DEFAULT_ENCODER
    code, note = core._verdict(
        ratio,
        core.history_ratios(normalised=True, encoder=enc),
        norm,
        core.is_downscale(src_geom, out_geom, info.get("autocrop")),
        encoder=enc,
    )

    # A decoder-error count is not part of the size verdict but must never be
    # silently ignored: a corrupt output can still be small and well-shaped.
    errs = info.get("decoder_errors")
    if errs:
        code, note = "halt-decoder-errors", f"{errs} decoder errors in the source"

    print(
        json.dumps(
            {
                "folder": folder,
                "verdict": code,
                "note": note,
                "encoder": enc,
                "source_bytes": sb,
                "output_bytes": ob,
                "ratio_pct": round(ratio, 1),
                "norm_ratio_pct": round(norm, 1),
                "shrink_pct": round(100 - ratio, 1),
                "source_geometry": src_geom,
                "output_geometry": out_geom,
                "decoder_errors": errs,
            }
        )
    )
    if code == "good":
        return 0
    if code in LADDER:
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
