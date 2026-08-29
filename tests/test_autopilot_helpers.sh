#!/bin/bash
# The concurrency guards in .autopilot.sh, against a fake staging drive.
#
#   bash tests/test_autopilot_helpers.sh [path-to-.autopilot.sh]
#
# These are the functions that decide whether a folder gets judged and RECORDED.
# core.record() appends with no duplicate guard, so a folder judged twice writes
# a second ledger row and double-counts the reclaim -- that is what these guard.
set -uo pipefail

AP="${1:-/Volumes/Crucial X9/4K Movies/.autopilot.sh}"
[ -f "$AP" ] || { echo "SKIP: $AP not found (staging drive not mounted?)"; exit 0; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; mkdir -p "$X9"
# shellcheck disable=SC2034  # read by the autopilot helpers sourced below
LOG=/dev/null
log() { :; }

# Pull the helper block out of the real script so this tests shipped code.
awk '/^slug_of\(\)/,/^# Resolve the library folder/' "$AP" | sed '$d' > "$TMP/helpers.sh"
grep -q 'finished_folder()' "$TMP/helpers.sh" || { echo "FAIL: could not extract helpers"; exit 1; }
# shellcheck disable=SC1090
source "$TMP/helpers.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "PASS $1"; pass=$((pass+1))
      else echo "FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }

mkdir -p "$X9/Alpha (2001)" "$X9/Beta (2002)" "$X9/Gamma (2003)"
touch "$X9/Alpha (2001)/Alpha (2001) Remux-2160p.mkv"
touch "$X9/Beta (2002)/Beta (2002) Remux-2160p.mkv"  "$X9/Beta (2002)/Beta (2002) 2160p HEVC.mkv"
touch "$X9/Gamma (2003)/Gamma (2003) Remux-2160p.mkv" "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"

ck "source-only folder is not 'finished'" "$(finished_folder)" "Beta (2002)"

sleep 600 & live=$!
echo "$live" > "$X9/.syncing-$(slug_of "Beta (2002)")"
ck "in-flight sync hides its folder"      "$(finished_folder)" "Gamma (2003)"
ck "syncs_in_flight sees it"              "$(syncs_in_flight && echo yes || echo no)" "yes"

kill -9 $live 2>/dev/null; wait $live 2>/dev/null
ck "marker with a dead pid is stale"      "$(finished_folder)" "Beta (2002)"
ck "stale marker file is removed"         "$([ -f "$X9/.syncing-$(slug_of "Beta (2002)")" ] && echo yes || echo no)" "no"
ck "syncs_in_flight false once cleared"   "$(syncs_in_flight && echo yes || echo no)" "no"

# The stale branch logs. finished_folder() is captured with $(...), so anything
# it writes to stdout is prepended to the folder name and poisons the caller.
log() { printf 'NOISE\n'; }
sleep 600 & dead=$!; kill -9 $dead 2>/dev/null; wait $dead 2>/dev/null
echo "$dead" > "$X9/.syncing-$(slug_of "Beta (2002)")"
ck "stale log never reaches stdout"       "$(finished_folder)" "Beta (2002)"
log() { :; }

# encoding_this: shadow pgrep/ps so this runs without a real encode.
pgrep() { [ "${1:-}" = "-x" ] && echo 4242; }
ps()    { printf 'HandBrakeCLI -i %s/Gamma (2003)/Gamma (2003) Remux-2160p.mkv -o %s/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv\n' "$X9" "$X9"; }
ck "encoding_this matches its folder"     "$(encoding_this "Gamma (2003)" && echo yes || echo no)" "yes"
ck "encoding_this ignores other folders"  "$(encoding_this "Beta (2002)" && echo yes || echo no)" "no"
rm -f "$X9/.syncing-$(slug_of "Beta (2002)")"
# A live encode's output matches *2160p HEVC*.mkv too; without this guard the
# loop hands a running encode to verdict.py, which returns 4 and halts.
ck "finished_folder skips the live encode" "$(finished_folder)" "Beta (2002)"
unset -f pgrep ps

ck "slug_of matches the log filenames"    "$(slug_of "Shrek (2001)")" "shrek2001"
ck "slug_of caps at 20 chars"             "$(slug_of "Kubo and the Two Strings (2016)")" "kuboandthetwostrings"

echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
