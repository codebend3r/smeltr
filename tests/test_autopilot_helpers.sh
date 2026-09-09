#!/bin/bash
# The concurrency guards in .autopilot.sh, against a fake staging drive.
#
#   bash tests/test_autopilot_helpers.sh [path-to-.autopilot.sh]
#
# These are the functions that decide whether a folder gets judged and RECORDED.
# core.record() appends with no duplicate guard, so a folder judged twice writes
# a second ledger row and double-counts the reclaim -- that is what these guard.
set -uo pipefail

# Prefer the LIVE script; with no argument and no drive, fall back to the
# tracked mirror so a clone (and CI) still exercises the guards instead of
# skipping the suite that keeps a folder from being recorded twice.
LIVE="/Volumes/Crucial X9/4K Movies/.autopilot.sh"
if [ -n "${1:-}" ]; then AP="$1"
elif [ -f "$LIVE" ]; then AP="$LIVE"
else AP="$(dirname "$0")/../staging/autopilot.sh"; echo "(staging drive not mounted - testing the tracked staging/autopilot.sh)"; fi
[ -f "$AP" ] || { echo "FAIL: $AP not found"; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; mkdir -p "$X9"
# shellcheck disable=SC2034  # read by the autopilot helpers sourced below
LOG=/dev/null
# The helper block derives SMELTR_HOME from $SMELTR, which the script sets
# above the extracted range; under `set -u` that line otherwise prints
# "SMELTR: unbound variable" on every run and leaves SMELTR_HOME unset.
# shellcheck disable=SC2034  # read by the sourced helper block
SMELTR="$(dirname "$0")/../smeltr"
log() { :; }

# Pull the helper block out of the real script so this tests shipped code.
awk '/^slug_of\(\)/,/^# Resolve the library folder/' "$AP" | sed '$d' > "$TMP/helpers.sh"
grep -q 'finished_folder()' "$TMP/helpers.sh" || { echo "FAIL: could not extract helpers"; exit 1; }
# shellcheck disable=SC1090
source "$TMP/helpers.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; fail=$((fail+1)); fi; }

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
# stderr is dropped here only to keep the run quiet: the assertion is on stdout.
log() { printf 'NOISE\n'; }
sleep 600 & dead=$!; kill -9 $dead 2>/dev/null; wait $dead 2>/dev/null
echo "$dead" > "$X9/.syncing-$(slug_of "Beta (2002)")"
ck "stale log never reaches stdout"       "$(finished_folder 2>/dev/null)" "Beta (2002)"
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

# library_roots_online: the fact that turns "cannot locate the original" from
# a halt into a deferral. An empty find is two different facts -- "the title
# is not there" and "the NAS is not there" -- and halting on the second cost
# 4h16m of encoding on 2026-08-31. Overriding LIB_ROOTS is the point: the
# helper must read the array, never a second hardcoded list.
mkdir -p "$TMP/rootA/A" "$TMP/rootB/B"
# shellcheck disable=SC2034  # read inside library_roots_online, sourced above
LIB_ROOTS=("$TMP/rootA" "$TMP/rootB")
ck "all roots listable = online"          "$(library_roots_online && echo yes || echo no)" "yes"
# shellcheck disable=SC2034
LIB_ROOTS=("$TMP/rootA" "$TMP/rootGone")
ck "a missing root = offline"             "$(library_roots_online && echo yes || echo no)" "no"
mkdir -p "$TMP/rootEmpty"
# shellcheck disable=SC2034
LIB_ROOTS=("$TMP/rootA" "$TMP/rootEmpty")
ck "a mounted-but-EMPTY root = offline"   "$(library_roots_online && echo yes || echo no)" "no"

# The main loop must DEFER on unreachable roots, never halt -- and the halt
# that remains must claim the roots are reachable, because that is the only
# state in which "no unique match" means a human problem.
ck "unresolved+offline defers"            "$(grep -c 'DEFER \$done_folder: library roots unreachable' "$AP")" "1"
ck "the remaining halt asserts roots up"  "$(grep -c 'roots ARE reachable' "$AP")" "1"
ck "stop condition waits on a deferred folder" "$(grep -c 'syncs_in_flight || \[ -n "\$done_folder" \]' "$AP")" "1"

# The staging-drive floor (operator's rule, 2026-09-08). next_title answers
# exit 3 with a reason starting "low space" while the X9 has under 100 GiB
# free; the driver logs that wait every 60 s. low_space_note() turns the FIRST
# such wait of an episode into ONE stamped `LOW SPACE:` line -- the line that
# emails the operator -- and stays silent until a wait for any other reason
# (or a start) closes the episode. A repeated line would be a repeated email.
LOWSPACE_LOG="$TMP/lowspace.log"
# shellcheck disable=SC2034  # read by low_space_note, sourced above
SMELTR_HOME="$TMP/home"; mkdir -p "$SMELTR_HOME"
LOWSPACE_MARK="$SMELTR_HOME/lowspace"
log() { printf '%s\n' "$*" >> "$LOWSPACE_LOG"; }
: > "$LOWSPACE_LOG"
low_space_note "low space on the staging drive: 87.3 GiB free, encodes resume at 100 GiB -- free up space; waiting"
ck "first low-space wait logs LOW SPACE"  "$(grep -c '^LOW SPACE: low space on the staging drive: 87.3 GiB free' "$LOWSPACE_LOG")" "1"
ck "and leaves the episode marker"        "$([ -e "$LOWSPACE_MARK" ] && echo yes || echo no)" "yes"
low_space_note "low space on the staging drive: 86.9 GiB free, encodes resume at 100 GiB -- free up space; waiting"
low_space_note "low space on the staging drive: 86.5 GiB free, encodes resume at 100 GiB -- free up space; waiting"
ck "repeated low-space waits log nothing" "$(grep -c '^LOW SPACE' "$LOWSPACE_LOG")" "1"
low_space_note "paused from the dashboard; waiting"
ck "another wait reason closes the episode" "$([ -e "$LOWSPACE_MARK" ] && echo yes || echo no)" "no"
ck "and logs no LOW SPACE line itself"    "$(grep -c '^LOW SPACE' "$LOWSPACE_LOG")" "1"
low_space_note "low space on the staging drive: 12.0 GiB free, encodes resume at 100 GiB -- free up space; waiting"
ck "a new episode logs again"             "$(grep -c '^LOW SPACE' "$LOWSPACE_LOG")" "2"
low_space_note ""
ck "an empty reason (a start) closes it"  "$([ -e "$LOWSPACE_MARK" ] && echo yes || echo no)" "no"
log() { :; }
# The loop must feed it: the exit-3 wait passes its reason, and a pick clears.
ck "exit-3 branch notes the reason"       "$(grep -c 'low_space_note "\$(next_reason)"' "$AP")" "1"
ck "a pick closes the episode"            "$(grep -c 'low_space_note ""' "$AP")" "1"

echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
