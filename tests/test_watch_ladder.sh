#!/bin/bash
# The encoder-aware band ladder in staging/watch-encode.sh.
#
# The band check runs BOTH ways (10-70% of source), and the two encoders read
# their quality numbers on opposite scales, so the step inverts per encoder:
#
#   x265 quality is CRF, LOWER = bigger file
#     too big   -> UP    10-12-14-16-18-20-22, then none-too-big at 22
#     too small -> DOWN  22-20-18-16-14-12-10, then none-too-small at 10
#   VideoToolbox quality is CQ on Apple's reversed scale, HIGHER = bigger file
#     too big   -> DOWN  100-95-...-55-50,     then none-too-big at 50
#     too small -> UP    50-55-...-95-100,     then none-too-small at 100
#
# THE STARTING QUALITY DOES NOT MATTER (operator's rule, 2026-09-07). Every
# rung steps in BOTH directions and "none-too-*" means the END OF THE MENU and
# nothing else. That replaced a one-directional rule keyed on which side of the
# default a rung sat: a rung above the default could only step further up, on
# the theory that it had been laddered up to and stepping back would oscillate.
# True of a rung the ladder reached itself, false of a hand-picked START rung --
# and `encoder_overrides.json` writes start rungs. The 2026-09-06 batch pinned
# every title at CQ 75, so `next_rung vt_h265_10bit 75 big` answered
# none-too-big and Shazam (2019) ran to 37% projecting 131% OF SOURCE with
# CQ 70/65/60/55/50 sitting unused underneath it.
#
# "none-too-*" no longer kills anything (2026-09-06): the watcher lets that
# encode finish at the rung it is on and logs FINAL| instead of KILLED|. This
# suite pins the MAPPING -- where an encode is allowed to end up. The behaviour
# past it is pinned in tests/test_error_state.py::LastRungFinishes.
#
# A rung mapped with the wrong scale RE-RUNS THE VIOLATION HARDER, so the
# direction of every rung is pinned here against the REPO copy: this must pass
# before the deploy, not after.
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

# --- x265, too big: CRF steps UP, from EVERY rung ------------------------
ck "x265 big 10 -> 12"              "$(next_rung x265_10bit 10 big)" 12
ck "x265 big 12 -> 14 (was one-directional)" "$(next_rung x265_10bit 12 big)" 14
ck "x265 big 14 -> 16"              "$(next_rung x265_10bit 14 big)" 16
ck "x265 big 16 -> 18"              "$(next_rung x265_10bit 16 big)" 18
ck "x265 big 18 -> 20"              "$(next_rung x265_10bit 18 big)" 20
ck "x265 big 20 -> 22"              "$(next_rung x265_10bit 20 big)" 22
ck "x265 big 22 is the end of the menu" "$(next_rung x265_10bit 22 big)" none-too-big

# --- x265, too small: CRF steps DOWN, from EVERY rung --------------------
ck "x265 small 22 -> 20 (was one-directional)" "$(next_rung x265_10bit 22 small)" 20
ck "x265 small 20 -> 18 (was one-directional)" "$(next_rung x265_10bit 20 small)" 18
ck "x265 small 16 -> 14 (was one-directional)" "$(next_rung x265_10bit 16 small)" 14
ck "x265 small 14 -> 12"             "$(next_rung x265_10bit 14 small)" 12
ck "x265 small 12 -> 10"             "$(next_rung x265_10bit 12 small)" 10
ck "x265 small 10 is the end of the menu" "$(next_rung x265_10bit 10 small)" none-too-small

# --- VideoToolbox: both arms are the mirror image ------------------------
# THE regression: a hand-picked CQ 75 projecting too big must step DOWN.
ck "vt big 75 -> 70 (the Shazam case)"      "$(next_rung vt_h265_10bit 75 big)" 70
ck "vt big 100 -> 95"                       "$(next_rung vt_h265_10bit 100 big)" 95
ck "vt big 80 -> 75"                        "$(next_rung vt_h265_10bit 80 big)" 75
ck "vt big 70 -> 65"                        "$(next_rung vt_h265_10bit 70 big)" 65
ck "vt big 65 -> 60"                        "$(next_rung vt_h265_10bit 65 big)" 60
ck "vt big 60 -> 55"                        "$(next_rung vt_h265_10bit 60 big)" 55
ck "vt big 55 -> 50"                        "$(next_rung vt_h265_10bit 55 big)" 50
ck "vt big 50 is the end of the menu"       "$(next_rung vt_h265_10bit 50 big)" none-too-big
ck "vt small 50 -> 55 (was one-directional)" "$(next_rung vt_h265_10bit 50 small)" 55
ck "vt small 60 -> 65 (was one-directional)" "$(next_rung vt_h265_10bit 60 small)" 65
ck "vt small 70 -> 75"                      "$(next_rung vt_h265_10bit 70 small)" 75
ck "vt small 75 -> 80"                      "$(next_rung vt_h265_10bit 75 small)" 80
ck "vt small 95 -> 100"                     "$(next_rung vt_h265_10bit 95 small)" 100
ck "vt small 100 is the end of the menu"    "$(next_rung vt_h265_10bit 100 small)" none-too-small

# An OFF-MENU quality snaps to the nearest rung on the requested side rather
# than exhausting. Only reachable by hand-editing the override file, but
# refusing to ladder a typed number is exactly how a blowup runs unopposed.
ck "an off-menu CQ snaps down"  "$(next_rung vt_h265_10bit 72 big)"   70
ck "an off-menu CQ snaps up"    "$(next_rung vt_h265_10bit 72 small)" 75

# A CRF number handed to the VT arm must NOT be read as a CQ, and vice versa:
# each is off the other's menu entirely, so it exhausts rather than stepping
# to a rung that means something else on that scale.
ck "a CRF rung is not a CQ rung" "$(next_rung vt_h265_10bit 18 big)"   none-too-big
ck "a CQ rung is not a CRF rung" "$(next_rung x265_10bit 60 big)"      none-too-big

# An unknown encoder falls back to the x265 mapping -- the proven direction.
ck "unknown encoder uses x265 rungs" "$(next_rung mystery 14 big)" 16

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
