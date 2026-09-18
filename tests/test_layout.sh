#!/bin/bash
# The staging LAYOUT in .autopilot.sh (operator's rule, 2026-09-07): movie
# folders live in $X9/queue and $X9/complete, never at the root.
#
#   bash tests/test_layout.sh [path-to-.autopilot.sh]
#
# Pins folder_dir(), staged_dirs(), move_to_complete() and migrate_layout()
# against a fake drive: a root folder is migrated into queue/ (or complete/
# when its .done- marker exists), the one HandBrake is writing into is left
# alone, a name already taken is never merged, and complete/ is never staged.
set -uo pipefail

LIVE="/Volumes/Crucial X9/4K Movies/.autopilot.sh"
if [ -n "${1:-}" ]; then AP="$1"
elif [ -f "$LIVE" ]; then AP="$LIVE"
else AP="$(dirname "$0")/../staging/autopilot.sh"; echo "(staging drive not mounted - testing the tracked staging/autopilot.sh)"; fi
[ -f "$AP" ] || { echo "FAIL: $AP not found"; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; mkdir -p "$X9"
STAGE="$X9/queue"; COMPLETE="$X9/complete"
# shellcheck disable=SC2034  # read by the autopilot helpers sourced below
LOG=/dev/null
# shellcheck disable=SC2034  # referenced by a helper the block also carries
SMELTR=/usr/bin/true
LOGGED=""
log() { LOGGED="$LOGGED$*"$'\n'; }

awk '/^slug_of\(\)/,/^# Resolve the library folder/' "$AP" | sed '$d' > "$TMP/helpers.sh"
grep -q 'migrate_layout()' "$TMP/helpers.sh" || { echo "FAIL: could not extract the layout helpers"; exit 1; }
# shellcheck disable=SC1090
source "$TMP/helpers.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; exit 1; fi; }

# ---- folder_dir: queue/ first, then the legacy root, else where it WOULD be
mkdir -p "$STAGE/Alpha (2001)" "$X9/Beta (2002)" "$STAGE/Gamma (2003)" "$X9/Gamma (2003)"
ck "folder_dir finds queue/"                 "$(folder_dir "Alpha (2001)")" "$STAGE/Alpha (2001)"
ck "folder_dir falls back to the root"       "$(folder_dir "Beta (2002)")"  "$X9/Beta (2002)"
ck "folder_dir prefers queue/ over the root" "$(folder_dir "Gamma (2003)")" "$STAGE/Gamma (2003)"
ck "folder_dir names queue/ for a new title" "$(folder_dir "Delta (2004)")" "$STAGE/Delta (2004)"
ck "folder_dir never treats queue as a title"    "$(folder_dir queue)"    "$STAGE/queue"
ck "folder_dir never treats complete as a title" "$(folder_dir complete)" "$STAGE/complete"

# ---- staged_dirs: queue/ plus legacy root folders; never complete/, never
#      the two layout folders themselves
mkdir -p "$COMPLETE/Done (1999)"
got=$(staged_dirs | xargs -I{} basename {} | sort | tr '\n' '|')
ck "staged_dirs lists queue/ and the root, not complete/" "$got" "Alpha (2001)|Beta (2002)|Gamma (2003)|Gamma (2003)|"

# ---- migrate_layout: root -> queue/ (or complete/ with a marker); the one
#      being encoded and a taken name are left alone
rm -rf "$X9/Gamma (2003)"
mkdir -p "$X9/Enc (2005)" "$X9/Fin (2006)" "$X9/Taken (2007)" "$STAGE/Taken (2007)"
touch "$X9/Enc (2005)/Enc (2005) Remux-2160p.mkv" "$X9/Enc (2005)/Enc (2005) 2160p HEVC.mkv"
touch "$X9/Fin (2006)/Fin (2006) Remux-2160p.mkv" "$X9/Fin (2006)/Fin (2006) 2160p HEVC.mkv"
mkdir -p "$X9/Landing (2011)"; touch "$X9/Landing (2011)/Landing (2011) Remux-2160p.mkv.partial"
echo "Fin (2006): verdict good" > "$X9/.done-Fin (2006)"
pgrep() { echo 4242; }
ps()    { printf 'HandBrakeCLI -i %s/Enc (2005)/Enc (2005) Remux-2160p.mkv -o %s/Enc (2005)/Enc (2005) 2160p HEVC.mkv\n' "$X9" "$X9"; }
kill()  { return 1; }
LOGGED=""
migrate_layout
ck "root folder moved into queue/"              "$([ -d "$STAGE/Beta (2002)" ] && [ ! -e "$X9/Beta (2002)" ] && echo yes || echo no)" "yes"
ck "done-marked root folder moved to complete/" "$([ -d "$COMPLETE/Fin (2006)" ] && [ ! -e "$X9/Fin (2006)" ] && echo yes || echo no)" "yes"
ck "the folder HandBrake writes into stays put" "$([ -d "$X9/Enc (2005)" ] && [ ! -e "$STAGE/Enc (2005)" ] && echo yes || echo no)" "yes"
ck "a folder a pull is landing in stays put"   "$([ -d "$X9/Landing (2011)" ] && [ ! -e "$STAGE/Landing (2011)" ] && echo yes || echo no)" "yes"
ck "a taken name is not merged or overwritten"  "$([ -d "$X9/Taken (2007)" ] && [ -d "$STAGE/Taken (2007)" ] && echo yes || echo no)" "yes"
ck "the collision is logged"                    "$(printf '%s' "$LOGGED" | grep -c 'Taken (2007) is at the root')" "1"
ck "queue/ itself is never migrated"            "$([ -d "$STAGE" ] && [ ! -e "$STAGE/queue" ] && echo yes || echo no)" "yes"
ck "complete/ itself is never migrated"         "$([ -d "$COMPLETE" ] && [ ! -e "$STAGE/complete" ] && echo yes || echo no)" "yes"
unset -f pgrep ps kill

# ---- move_to_complete: a finished queue/ folder goes to complete/ whole
mkdir -p "$STAGE/Ok (2008)"; touch "$STAGE/Ok (2008)/Ok (2008) Remux-2160p.mkv" "$STAGE/Ok (2008)/Ok (2008) 2160p HEVC.mkv"
move_to_complete "Ok (2008)"
ck "move_to_complete moves the whole folder" "$(ls "$COMPLETE/Ok (2008)" | sort | tr '\n' '|')" "Ok (2008) 2160p HEVC.mkv|Ok (2008) Remux-2160p.mkv|"
ck "and it leaves queue/"                    "$([ -e "$STAGE/Ok (2008)" ] && echo still || echo gone)" "gone"
mkdir -p "$STAGE/Ok (2008)" "$COMPLETE/Ok (2008)"
LOGGED=""; move_to_complete "Ok (2008)"
ck "an existing complete/ name is never overwritten" "$([ -d "$STAGE/Ok (2008)" ] && echo kept || echo lost)" "kept"
ck "and that is logged" "$(printf '%s' "$LOGGED" | grep -c 'already exists')" "1"
ck "a title with no folder is a no-op" "$(move_to_complete "Nowhere (2009)"; echo $?)" "0"

# ---- finished_folder reads the layout: a finished title in queue/ is found,
#      one in complete/ never is
mkdir -p "$STAGE/Judge (2010)"; touch "$STAGE/Judge (2010)/Judge (2010) Remux-2160p.mkv" "$STAGE/Judge (2010)/Judge (2010) 2160p HEVC.mkv"
pgrep() { :; }
rm -rf "$STAGE/Beta (2002)" "$STAGE/Gamma (2003)" "$STAGE/Alpha (2001)" "$STAGE/Taken (2007)" "$X9/Taken (2007)" "$X9/Enc (2005)" "$X9/Landing (2011)"
ck "finished_folder finds the finished title in queue/" "$(finished_folder)" "Judge (2010)"
mv "$STAGE/Judge (2010)" "$COMPLETE/"
ck "and never one in complete/" "$(finished_folder)" ""
unset -f pgrep

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
