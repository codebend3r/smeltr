#!/bin/bash
# ops/replenisher.sh -- the independent replenisher's tick, against a fake
# drive. Pins: a blind (empty) X9 is refused, an off-switch file is honoured,
# a readable drive runs the X9 script, and a run that PICKS is stamped into
# the driver's log in the driver's own format (events.py reads it).
set -uo pipefail
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; mkdir -p "$X9/queue"
export SMELTR_X9="$X9" SMELTR_REPLENISH_LOG="$TMP/replenish.log" SMELTR_REPLENISH_TICK=1
R="$(dirname "$0")/../ops/replenisher.sh"
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; fail=$((fail+1)); fi; }

# blind drive: mounted, zero entries
rmdir "$X9/queue"
bash "$R" --tick
ck "blind drive is refused loudly" "$(grep -c BLIND "$TMP/replenish.log")" "1"

# readable drive, script that says nothing needed
mkdir -p "$X9/queue"
printf '#!/bin/bash\necho "staged on X9: 12 (target 10)"\necho "above threshold"\n' > "$X9/.replenish-queue.sh"
bash "$R" --tick
ck "script ran"                       "$(grep -c 'above threshold' "$TMP/replenish.log")" "1"
ck "a no-op run is not stamped into the driver log" "$([ -f "$X9/.autopilot.log" ] && grep -c REPLENISH "$X9/.autopilot.log" || echo 0)" "0"

# a run that picks is stamped into the driver's log
printf '#!/bin/bash\necho "staged on X9: 4 (target 10)"\necho "need 10 more"\nprintf "PICK 83.0 Mb/s  Alpha (2001)\\n"\n' > "$X9/.replenish-queue.sh"
bash "$R" --tick
ck "a picking run is stamped"         "$(grep -c '^20[0-9-]* [0-9:]*  REPLENISH (independent)' "$X9/.autopilot.log")" "1"
ck "and its PICK line is folded in"   "$(grep -c 'PICK 83.0' "$X9/.autopilot.log")" "1"

# off switch
touch "$X9/.replenish-off"
bash "$R" --tick
ck "the off switch stops the tick"    "$(grep -c 'PICK 83.0' "$TMP/replenish.log")" "1"

# unknown argument
ck "unknown args are refused" "$(bash "$R" --bogus 2>/dev/null; echo $?)" "2"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
