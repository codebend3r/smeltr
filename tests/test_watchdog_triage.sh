#!/bin/bash
# What the watchdog does with a *2160p HEVC*.mkv it finds on the staging drive.
#
#   bash tests/test_watchdog_triage.sh [path-to-watchdog.sh]
#
# The stakes: a "corpse" (an encode killed mid-write) is matched by the driver
# as FINISHED, handed to verdict.py, and halts the pipeline. A genuinely
# finished output looks almost identical on disk and MUST survive untouched --
# deleting one throws away hours of encoding. These pin that boundary.
set -uo pipefail

WD="${1:-$(cd "$(dirname "$0")/.." && pwd)/ops/watchdog.sh}"
[ -f "$WD" ] || { echo "FAIL: $WD not found"; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "SKIP: ffprobe not installed"; exit 0; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; mkdir -p "$X9"

# Source the real block out of the shipped script, so this tests shipped code.
awk '/^# --- staging-output triage/,/^# --- end staging-output triage/' "$WD" > "$TMP/triage.sh"
grep -q 'triage_outputs()' "$TMP/triage.sh" || { echo "FAIL: could not extract triage block"; exit 1; }
WLOGGED="$TMP/wlog.txt"
wlog() { printf '%s\n' "$*" >> "$WLOGGED"; }
# shellcheck disable=SC1090
source "$TMP/triage.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; exit 1; fi; }

# A real, tiny, finalised MKV -- ffprobe reports a duration for it.
mk_real() { ffmpeg -v error -y -f lavfi -i testsrc=size=32x32:rate=5 -t "${2:-2}" \
                   -c:v libx264 -preset ultrafast "$1" </dev/null >/dev/null 2>&1; }
# Two corpse shapes, because they probe completely differently and only one of
# them was visible in the live failure:
#   garbage - killed before the header was finalised. ffprobe reports NOTHING.
#             This is the real Kubo partial of 2026-08-22.
#   short   - a muxer that wrote its duration up front, then got truncated. It
#             probes perfectly and just runs short. A missing-duration test
#             walks straight past this one, so it is pinned here on purpose.
mk_corpse()       { head -c 20000 /dev/urandom > "$1"; }
mk_corpse_short() { mk_real "$1" 1; }          # 1 s output beside a 3 s source

age_out() { touch -t 202001010000 "$1"; }   # far older than the 120 s floor

triage() { find "$X9" -mindepth 2 -maxdepth 2 -type f -name '*2160p HEVC*.mkv' \
             ! -name '._*' 2>/dev/null | triage_outputs; }

# --- a finished encode must survive -------------------------------------
mkdir -p "$X9/Beta (2002)"
mk_real "$X9/Beta (2002)/Beta (2002) Remux-2160p.mkv" 3
mk_real "$X9/Beta (2002)/Beta (2002) 2160p HEVC.mkv"  3
age_out "$X9/Beta (2002)/Beta (2002) 2160p HEVC.mkv"
ck "finished output is reported finished" "$(triage)" "finished"
ck "finished output is NOT deleted" \
   "$([ -f "$X9/Beta (2002)/Beta (2002) 2160p HEVC.mkv" ] && echo kept || echo GONE)" "kept"
rm -rf "$X9/Beta (2002)"

# --- a corpse must be swept ---------------------------------------------
mkdir -p "$X9/Gamma (2003)"
mk_real   "$X9/Gamma (2003)/Gamma (2003) Remux-2160p.mkv" 3
mk_corpse "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
touch     "$X9/Gamma (2003)/._Gamma (2003) 2160p HEVC.mkv"
age_out   "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
ck "unfinalised output is a corpse"       "$(triage)" "corpse"
ck "corpse is deleted" \
   "$([ -f "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" ] && echo KEPT || echo gone)" "gone"
ck "its AppleDouble goes too" \
   "$([ -f "$X9/Gamma (2003)/._Gamma (2003) 2160p HEVC.mkv" ] && echo KEPT || echo gone)" "gone"
ck "the source is never touched" \
   "$([ -f "$X9/Gamma (2003)/Gamma (2003) Remux-2160p.mkv" ] && echo kept || echo GONE)" "kept"

# --- a truncated-but-probeable output is a corpse too -------------------
mk_corpse_short "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
age_out         "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
ck "output far shorter than its source is a corpse" "$(triage)" "corpse"
ck "short corpse is deleted" \
   "$([ -f "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" ] && echo KEPT || echo gone)" "gone"

# --- and the tolerance does not eat a good encode -----------------------
# A real encode matches its source to milliseconds; nothing legitimate lands
# anywhere near the 2% floor, but a slightly-off duration must still survive.
mk_real "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" 3
age_out "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
ck "an exact-length encode survives" "$(triage)" "finished"

# --- an unreadable source blocks the whole judgement --------------------
mk_corpse "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
age_out   "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
mv "$X9/Gamma (2003)/Gamma (2003) Remux-2160p.mkv" "$TMP/src-held.mkv"
: > "$X9/Gamma (2003)/Gamma (2003) Remux-2160p.mkv"     # present but unprobeable
ck "unprobeable source yields 'unknown'"  "$(triage)" "unknown"
ck "and the output survives that" \
   "$([ -f "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" ] && echo kept || echo GONE)" "kept"
mv -f "$TMP/src-held.mkv" "$X9/Gamma (2003)/Gamma (2003) Remux-2160p.mkv"

# --- a freshly written output is never judged ---------------------------
mk_corpse "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"   # mtime = now
ck "output written seconds ago is 'fresh'" "$(triage)" "fresh"
ck "and is left on disk" \
   "$([ -f "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" ] && echo kept || echo GONE)" "kept"

# --- a live encode is never judged, however old the file looks ----------
age_out "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv"
pgrep() { [ "${1:-}" = "-x" ] && echo 4242; }
ps()    { printf 'HandBrakeCLI -i %s -o %s/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv\n' \
                 "src" "$X9"; }
ck "live encode is reported live"          "$(triage)" "live"
ck "live encode is not deleted" \
   "$([ -f "$X9/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" ] && echo kept || echo GONE)" "kept"
unset -f pgrep ps

# --- narration must never reach stdout ----------------------------------
# triage_outputs() is captured with $(...); a stray echo becomes a phantom
# verdict word and the caller acts on it.
ck "wlog narration stayed off stdout" \
   "$(grep -c . "$WLOGGED" >/dev/null 2>&1 && echo logged || echo empty)" "logged"
ck "every verdict is a known word" \
   "$(triage | grep -Evc '^(live|fresh|unknown|finished|corpse)$')" "0"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
