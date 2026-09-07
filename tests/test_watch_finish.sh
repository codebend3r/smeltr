#!/bin/bash
# The end of the band ladder, driven for real (2026-09-06).
#
# `tests/test_watch_ladder.sh` pins the rung MAPPING by sourcing next_rung().
# This suite runs the whole watcher loop against a sandbox, because the bug
# that made it necessary was invisible to a string match: a single flag
# ("the ladder is done") switched the band check off in BOTH directions, so
# one noisy low sample at 6% progress removed the only ceiling on the rest of
# a multi-hour encode and a 500%-of-source blowup ran unopposed.
#
# The watcher hardcodes the X9 paths, so the copy under test is `sed`-rewritten
# to a temp dir. NOTHING here touches the staging drive, the ledger, or any
# real encode -- the "HandBrake" is a `sleep` and the "video" is /dev/zero.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WE="$REPO/staging/watch-encode.sh"
[ -f "$WE" ] || { echo "FAIL: $WE not found"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; kill $HB $W 2>/dev/null' EXIT
FOLDER="Sandbox (2020)"
mkdir -p "$TMP/sb/$FOLDER"
# sleep 60 -> sleep 1 so a 60 s tick does not make this a 5-minute suite.
sed "s|/Volumes/Crucial X9/4K Movies|$TMP/sb|g; s|sleep 60|sleep 1|" "$WE" > "$TMP/we.sh"
chmod +x "$TMP/we.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "PASS $1"; pass=$((pass+1))
      else echo "FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }

# 100 MiB "source"; "output" size is what moves the projection.
dd if=/dev/zero of="$TMP/sb/$FOLDER/src.mkv" bs=1048576 count=100 2>/dev/null
out(){ dd if=/dev/zero of="$TMP/sb/$FOLDER/out.mkv" bs=1048576 count="$1" 2>/dev/null; }
# 12% progress: 1 MiB projects to 8.3% of source (too small, under the 10%
# floor of the 10-70 band), 60 MiB to 500% (a blowup).
printf 'task 1 of 1, 12.00 %%\n' > "$TMP/sb/.hb-sandbox.log"

# Wait for a pattern, or give up -- never hang the suite.
waitfor(){ n=0; until grep -q "$1" "$TMP/log.txt" 2>/dev/null; do
             n=$((n+1)); [ "$n" -gt 40 ] && return 1; sleep 0.5; done; return 0; }

out 1
sleep 600 & HB=$!
# Off the job table, or bash prints "Terminated: 15" when the watcher kills it.
disown "$HB" 2>/dev/null || true
"$TMP/we.sh" sandbox "$FOLDER" src.mkv out.mkv "$HB" 10 x265_10bit > "$TMP/log.txt" 2>&1 &
W=$!

# --- 1. a direction with no rung left finishes instead of killing ---------
# CRF 10 is the END of the too-small arm -- the biggest-file rung the x265
# menu has -- so a too-SMALL violation there has nowhere to step. Since
# 2026-09-07 that is the ONLY thing that exhausts a direction: the ladder no
# longer treats a rung as one-directional because of which side of the
# default it sits on. The old behaviour killed the encode here and exhausted
# the title into the ERROR state.
if waitfor '^FINAL|'; then
  ck "a too-small projection at CRF 10 reports FINAL" 0 0
else
  ck "a too-small projection at CRF 10 reports FINAL" 1 0
fi
line=$(grep '^FINAL|' "$TMP/log.txt" | head -1)
kill -0 "$HB" 2>/dev/null && alive=yes || alive=no
ck "the encode is NOT killed"           "$alive" yes
ck "the partial is NOT deleted"         "$([ -f "$TMP/sb/$FOLDER/out.mkv" ] && echo yes || echo no)" yes
ck "the line carries its own timestamp" \
   "$(printf '%s' "$line" | cut -d'|' -f3 | grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}$')" 1
ck "the rung names its scale"           "$(printf '%s' "$line" | grep -c 'CRF 10')" 1
ck "it does not claim an arm it is not on" \
   "$(printf '%s' "$line" | grep -c 'last rung on the')" 0
ck "it names the direction that ran out" \
   "$(printf '%s' "$line" | grep -c 'too-small')" 1

# --- 2. THE OTHER DIRECTION KEEPS ITS KILL AUTHORITY ----------------------
# This is the whole point. CRF 10 has run out of too-small rungs but still
# has a real up-rung (12), and a blowup is exactly what the too-big arm is
# there to stop. A single "the ladder is done" flag disarmed both.
out 60
if waitfor '^KILLED|'; then
  ck "a blowup after a FINAL is still killed" 0 0
else
  ck "a blowup after a FINAL is still killed" 1 0
fi
kline=$(grep '^KILLED|' "$TMP/log.txt" | head -1)
ck "it ladders to the next rung"  "$(printf '%s' "$kline" | grep -c 'next: Q 12')" 1
n=0; while kill -0 "$HB" 2>/dev/null && [ "$n" -lt 40 ]; do n=$((n+1)); sleep 0.5; done
ck "the encode is killed"         "$(kill -0 "$HB" 2>/dev/null && echo yes || echo no)" no
ck "the partial is deleted"       "$([ -f "$TMP/sb/$FOLDER/out.mkv" ] && echo yes || echo no)" no

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
