#!/bin/bash
# The encoder-aware band ladder in staging/watch-encode.sh.
#
# The band check runs BOTH ways (30-80% of source), and the two encoders read
# their quality numbers on opposite scales, so every arm inverts:
#
#   x265 quality is CRF, LOWER = bigger file
#     too big   -> UP    14-16-18-20-22, then none-too-big
#     too small -> DOWN  14-12-10,       then none-too-small
#   VideoToolbox quality is CQ on Apple's reversed scale, HIGHER = bigger file
#     too big   -> DOWN  60-55-50,       then none-too-big
#     too small -> UP    60-65-70,       then none-too-small
#
# "none-too-*" is the END of the ladder, and since 2026-09-06 it no longer
# kills anything: the watcher lets that encode finish at the rung it is on
# (CRF 22 / CRF 10, CQ 50 / CQ 70) and logs FINAL| instead of KILLED|. This
# suite still pins the MAPPING -- what "the last rung" is on each arm of each
# scale is exactly what decides where an encode is allowed to end up. The
# behaviour past it is pinned in tests/test_error_state.py::LastRungFinishes.
#
# A rung mapped with the wrong scale RE-RUNS THE VIOLATION HARDER, and a rung
# stepped in the direction opposite to the one it was laddered to would
# oscillate forever -- so both the direction of every rung and the immediate
# exhaustion of every one-directional rung are pinned here, against the REPO
# copy: this must pass before the deploy, not after.
set -uo pipefail
WE="$(cd "$(dirname "$0")/.." && pwd)/staging/watch-encode.sh"
[ -f "$WE" ] || { echo "FAIL: $WE not found"; exit 1; }

# WE_TEST=1 makes the script return after defining next_rung(), so sourcing
# it loads the function without starting the watch loop.
# shellcheck disable=SC1090
WE_TEST=1 source "$WE" 1 f s o 99999 14 x265_10bit

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "PASS $1"; pass=$((pass+1))
      else echo "FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }

# --- x265, too big: CRF steps UP from the pivot --------------------------
ck "x265 big 14 -> 16 (the pivot)"  "$(next_rung x265_10bit 14 big)" 16
ck "x265 big 16 -> 18"              "$(next_rung x265_10bit 16 big)" 18
ck "x265 big 18 -> 20"              "$(next_rung x265_10bit 18 big)" 20
ck "x265 big 20 -> 22"              "$(next_rung x265_10bit 20 big)" 22
ck "x265 big 22 is the last rung"   "$(next_rung x265_10bit 22 big)" none-too-big

# --- x265, too small: CRF steps DOWN from the pivot ----------------------
ck "x265 small 14 -> 12 (the pivot)" "$(next_rung x265_10bit 14 small)" 12
ck "x265 small 12 -> 10"             "$(next_rung x265_10bit 12 small)" 10
ck "x265 small 10 is the last rung"  "$(next_rung x265_10bit 10 small)" none-too-small

# --- x265, opposite direction on an already-laddered rung: exhaust NOW ----
ck "x265 small at an up-rung exhausts" "$(next_rung x265_10bit 20 small)" none-too-small
ck "x265 big at a down-rung exhausts"  "$(next_rung x265_10bit 12 big)"   none-too-big

# --- VideoToolbox: BOTH arms are the mirror image ------------------------
ck "vt big at 75 (an up-rung now) exhausts" "$(next_rung vt_h265_10bit 75 big)" none-too-big
ck "vt big 70 -> 65 (DOWN, reversed scale; 70 is the pivot)" "$(next_rung vt_h265_10bit 70 big)" 65
ck "vt big 65 -> 60"                        "$(next_rung vt_h265_10bit 65 big)" 60
ck "vt big 60 -> 55"                        "$(next_rung vt_h265_10bit 60 big)" 55
ck "vt big 55 -> 50"                        "$(next_rung vt_h265_10bit 55 big)" 50
ck "vt big 50 is the last rung"             "$(next_rung vt_h265_10bit 50 big)" none-too-big
ck "vt small 70 -> 75 (UP, reversed scale; 70 is the pivot)" "$(next_rung vt_h265_10bit 70 small)" 75
ck "vt small 75 -> 80"                      "$(next_rung vt_h265_10bit 75 small)" 80
ck "vt small 95 -> 100"                     "$(next_rung vt_h265_10bit 95 small)" 100
ck "vt small 100 is the last rung"          "$(next_rung vt_h265_10bit 100 small)" none-too-small
ck "vt small at a big-rung exhausts"        "$(next_rung vt_h265_10bit 50 small)" none-too-small
ck "vt small at 60 (a down-rung now) exhausts" "$(next_rung vt_h265_10bit 60 small)" none-too-small
ck "vt big at the pivot steps down, never exhausts" "$(next_rung vt_h265_10bit 70 big)" 65

# A CRF number handed to the VT arm must NOT be read as a CQ, and vice versa:
# each is off the other's ladder entirely, so it exhausts rather than
# stepping to a rung that means something else on that scale.
ck "a CRF rung is not a CQ rung" "$(next_rung vt_h265_10bit 18 big)"   none-too-big
ck "a CQ rung is not a CRF rung" "$(next_rung x265_10bit 60 big)"      none-too-big

# An unknown encoder falls back to the x265 mapping -- the proven direction.
ck "unknown encoder uses x265 rungs" "$(next_rung mystery 14 big)" 16

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
