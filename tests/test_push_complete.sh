#!/bin/bash
# ops/push-complete.sh -- the independent pusher, against a sandbox drive, a
# sandbox "NAS" and a fake ssh. Nothing here touches the X9, the NAS or the
# ledger. Pins the operator's rules of 2026-09-10: the SOURCE in complete/ is
# purged as soon as the encode probes; the LARGEST pending encode is pushed
# first; the push lands BESIDE the NAS original and removes nothing there; the
# X9 folder goes only after the NAS copy verifies; a title with no library
# folder is marked failed and does not wedge the titles behind it; a busy wire
# holds the queue; a verify failure keeps BOTH copies.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
P="$REPO/ops/push-complete.sh"
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || { echo "SKIP: ffmpeg/ffprobe not installed"; exit 0; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; LIBROOT="$TMP/lib"; LIB="$LIBROOT/Vhagar/Media/4K Movies"; NAS="$TMP/nas"
mkdir -p "$X9/complete" "$X9/queue" "$LIB" "$NAS" "$TMP/bin"
export SMELTR_X9="$X9" SMELTR_XFER="$X9/.ssh-xfer.sh" SMELTR_SSH="$TMP/bin/fakessh" \
       SMELTR_VOLUMES="$LIBROOT" SMELTR_LIB_ROOTS="$LIB" SMELTR_PUSH_LOG="$TMP/push.log" \
       SMELTR_PUSH_SETTLE=0 SMELTR_PUSH_TICK=1

# ssh that runs the command HERE, with /volume1 mapped onto the sandbox NAS.
# A `lie` file makes the remote stat of an EXISTING file answer 1 byte (a
# verify failure after the push; a missing file still reads as absent).
cat > "$TMP/bin/fakessh" <<FAKE
#!/bin/bash
while [ "\${1:-}" = "-o" ]; do shift 2; done
shift   # host
cmd="\$*"; cmd="\${cmd//\\/volume1\\//$NAS/}"
cmd="\${cmd//stat -c%s/stat -f%z}"   # GNU stat on the NAS, BSD stat here
if [ -f "$TMP/lie" ] && [[ "\$cmd" == stat* ]]; then   # lie about a file that EXISTS
  out=\$(bash -c "\$cmd"); [ "\$out" != 0 ] && echo 1 || echo 0; exit 0
fi
exec bash -c "\$cmd"
FAKE
chmod +x "$TMP/bin/fakessh"
# A transfer that lands the file in the sandbox NAS. `--sleep` holds the wire.
export FAKE_NAS="$NAS"
cat > "$X9/.ssh-xfer.sh" <<'FAKE'
#!/bin/bash
[ "${2:-}" = "--sleep" ] && { sleep 4; exit 0; }
rdir="${3/#$SMELTR_VOLUMES/$FAKE_NAS}"; mkdir -p "$rdir"; cp "$2" "$rdir/$(basename "$2")" && echo "ssh push OK"
FAKE
chmod +x "$X9/.ssh-xfer.sh"

# A real, probe-able encode: N seconds of test pattern + one audio track.
mkenc() { ffmpeg -v error -y -f lavfi -i "testsrc=duration=$2:size=64x64:rate=10" -f lavfi -i "anullsrc=r=8000:cl=mono" -t "$2" -c:v mpeg4 -c:a pcm_s16le "$1"; }
# A finished title on the X9 (encode + the source it was made from) with its
# library folder holding the original.
title() { # name seconds bucket
  mkdir -p "$X9/complete/$1" "$LIB/$3/$1"
  mkenc "$X9/complete/$1/$1 2160p HEVC.mkv" "$2"
  head -c 9000 /dev/zero > "$X9/complete/$1/$1 Remux-2160p.mkv"
  head -c 9000 /dev/zero > "$LIB/$3/$1/$1 Remux-2160p.mkv"
  mkdir -p "$NAS/Vhagar/Media/4K Movies/$3/$1"; cp "$LIB/$3/$1/$1 Remux-2160p.mkv" "$NAS/Vhagar/Media/4K Movies/$3/$1/"
}

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; exit 1; fi; }

# --- the backlog: two titles, an orphan, a source-only folder, a landing pull
title "Big (2001)" 3 B
title "Small (2002)" 1 S
mkdir -p "$X9/complete/Orphan (2003)"; mkenc "$X9/complete/Orphan (2003)/Orphan (2003) 2160p HEVC.mkv" 1
head -c 100 /dev/zero > "$X9/complete/Orphan (2003)/Orphan (2003) Remux-2160p.mkv"
mkdir -p "$X9/complete/NoEncode (2004)"; head -c 100 /dev/zero > "$X9/complete/NoEncode (2004)/NoEncode (2004) Remux-2160p.mkv"
mkdir -p "$X9/complete/Landing (2005)"; mkenc "$X9/complete/Landing (2005)/Landing (2005) 2160p HEVC.mkv" 1
head -c 100 /dev/zero > "$X9/complete/Landing (2005)/Landing (2005) Remux-2160p.mkv"; : > "$X9/complete/Landing (2005)/x.partial"
# a title whose NAS original is a different size: the source must stay
title "Differs (2010)" 1 D; head -c 4000 /dev/zero > "$NAS/Vhagar/Media/4K Movies/D/Differs (2010)/Differs (2010) Remux-2160p.mkv"
# a stale marker from a crashed push
printf '999999\nBig (2001)\n' > "$X9/.pushing-Big (2001)"

bash "$P" --tick
LOG="$X9/.autopilot.log"
ck "the source beside a finished encode is purged"   "$([ -e "$X9/complete/Small (2002)/Small (2002) Remux-2160p.mkv" ] && echo kept || echo gone)" gone
ck "a purge is stamped into the driver's log"         "$(grep -c '  PURGED SOURCE Big (2001):' "$LOG")" 1
ck "a folder with no encode keeps its source"         "$([ -e "$X9/complete/NoEncode (2004)/NoEncode (2004) Remux-2160p.mkv" ] && echo kept || echo gone)" kept
ck "a folder with a .partial is not touched"          "$([ -e "$X9/complete/Landing (2005)/Landing (2005) Remux-2160p.mkv" ] && echo kept || echo gone)" kept
ck "the LARGEST encode is pushed first"               "$(grep -o 'PUSHED [A-Za-z]* ' "$LOG" | head -2 | tr -d '\n')" "PUSHED Big PUSHED Small "
ck "the encode lands beside the NAS original"         "$(ls "$NAS/Vhagar/Media/4K Movies/B/Big (2001)" | wc -l | tr -d ' ')" 2
ck "the NAS original is untouched"                    "$(stat -f%z "$NAS/Vhagar/Media/4K Movies/B/Big (2001)/Big (2001) Remux-2160p.mkv")" 9000
ck "the X9 folder is removed after the push verifies" "$([ -d "$X9/complete/Big (2001)" ] && echo kept || echo gone)" gone
ck "the pushed marker names the destination"         "$(grep -c "pushed to $LIB/B/Big (2001) at" "$X9/.pushed-Big (2001)")" 1
ck "no library folder = failed marker, folder kept"  "$([ -f "$X9/.push-failed-Orphan (2003)" ] && [ -d "$X9/complete/Orphan (2003)" ] && echo yes || echo no)" yes
ck "no library folder = the SOURCE is kept too"      "$([ -e "$X9/complete/Orphan (2003)/Orphan (2003) Remux-2160p.mkv" ] && echo kept || echo gone)" kept
ck "a NAS original of a different size keeps the source" "$([ -e "$X9/complete/Differs (2010)/Differs (2010) Remux-2160p.mkv" ] && echo kept || echo gone)" kept
ck "...but its encode is still pushed"                "$([ -f "$NAS/Vhagar/Media/4K Movies/D/Differs (2010)/Differs (2010) 2160p HEVC.mkv" ] && echo pushed || echo no)" pushed
ck "a failed title does not wedge the one behind it"  "$([ -d "$X9/complete/Small (2002)" ] && echo kept || echo gone)" gone
ck "the stale pushing marker was cleared"             "$([ -f "$X9/.pushing-Big (2001)" ] && echo kept || echo gone)" gone
ck "the lock is released"                             "$([ -d "$X9/.push.lock" ] && echo held || echo free)" free
ck "nothing on the NAS was removed"                   "$(find "$NAS" -type f | wc -l | tr -d ' ')" 6

# --- a failed title is left alone on the next tick, a fresh one still moves
bash "$P" --tick
ck "the failed title is not retried while marked"     "$(grep -c 'PUSH FAILED Orphan' "$LOG")" 1

# --- verify failure keeps BOTH copies
title "Lied (2006)" 1 L; touch "$TMP/lie"
bash "$P" --tick; rm -f "$TMP/lie"
ck "a size mismatch marks the title failed"           "$(grep -c 'size mismatch' "$X9/.push-failed-Lied (2006)")" 1
ck "...and keeps the X9 copy"                         "$([ -f "$X9/complete/Lied (2006)/Lied (2006) 2160p HEVC.mkv" ] && echo kept || echo gone)" kept
ck "...and keeps the NAS copy"                        "$([ -f "$NAS/Vhagar/Media/4K Movies/L/Lied (2006)/Lied (2006) 2160p HEVC.mkv" ] && echo kept || echo gone)" kept

# --- an encode that runs short of its source is refused, source kept
title "Short (2011)" 1 S
mkenc "$X9/complete/Short (2011)/Short (2011) Remux-2160p.mkv" 3   # a 3 s "source" beside a 1 s encode
bash "$P" --tick
ck "a short encode marks the title failed"            "$(grep -c 'runs short' "$X9/.push-failed-Short (2011)")" 1
ck "...and keeps the source"                          "$([ -e "$X9/complete/Short (2011)/Short (2011) Remux-2160p.mkv" ] && echo kept || echo gone)" kept

# --- a title with glob characters resolves literally
title "Cut [Director's] (2012)" 1 C
bash "$P" --tick
ck "brackets in a title still find the library folder" "$([ -f "$NAS/Vhagar/Media/4K Movies/C/Cut [Director's] (2012)/Cut [Director's] (2012) 2160p HEVC.mkv" ] && echo pushed || echo no)" pushed

# --- a failure marker that cannot be written HOLDS the tick instead of spinning
mkdir -p "$X9/complete/Nowhere (2013)"; mkenc "$X9/complete/Nowhere (2013)/Nowhere (2013) 2160p HEVC.mkv" 1
mkdir "$X9/.push-failed-Nowhere (2013)"          # a directory: printf > it fails
bash "$P" --tick & TP=$!
for _ in $(seq 1 40); do kill -0 "$TP" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$TP" 2>/dev/null; then kill "$TP"; spun=yes; else spun=no; fi
ck "an unwritable marker ends the tick, no spin"      "$spun" no
ck "...and says why"                                  "$(grep -c 'cannot write .push-failed-Nowhere' "$LOG")" 1
rmdir "$X9/.push-failed-Nowhere (2013)"; rm -rf "$X9/complete/Nowhere (2013)" "$X9/.push.lock"

# --- a held lock is logged, not silent
mkdir -p "$X9/.push.lock"; echo $$ > "$X9/.push.lock/pid"
bash "$P" --tick
ck "a live lock is reported"                          "$(grep -c "tick skipped: pusher pid $$ holds the lock" "$TMP/push.log")" 1
rm -rf "$X9/.push.lock"

# --- a same-named file already on the NAS with a different size is never overwritten
title "Taken (2009)" 1 T
head -c 777 /dev/zero > "$NAS/Vhagar/Media/4K Movies/T/Taken (2009)/Taken (2009) 2160p HEVC.mkv"
bash "$P" --tick
ck "a different same-named NAS file marks the title failed" "$(grep -c 'not overwriting' "$X9/.push-failed-Taken (2009)")" 1
ck "...and the NAS file is untouched"                 "$(stat -f%z "$NAS/Vhagar/Media/4K Movies/T/Taken (2009)/Taken (2009) 2160p HEVC.mkv")" 777
ck "...and the X9 copy is kept"                       "$([ -f "$X9/complete/Taken (2009)/Taken (2009) 2160p HEVC.mkv" ] && echo kept || echo gone)" kept

# --- a busy wire holds the queue; the hold is stamped once
title "Waiting (2007)" 1 W
bash "$X9/.ssh-xfer.sh" push --sleep & SLP=$!
sleep 1
bash "$P" --tick; bash "$P" --tick
ck "nothing is pushed while a push is on the wire"    "$([ -d "$X9/complete/Waiting (2007)" ] && echo held || echo pushed)" held
ck "the hold is stamped ONCE, not once per tick"      "$(grep -c 'waiting: push held - a push from this drive' "$LOG")" 1
wait "$SLP" 2>/dev/null
bash "$P" --tick
ck "the queue drains once the wire frees up"          "$([ -d "$X9/complete/Waiting (2007)" ] && echo held || echo pushed)" pushed

# --- off switch, unknown args
title "Off (2008)" 1 O; touch "$X9/.push-off"
bash "$P" --tick
ck "the off switch stops the tick"                    "$([ -f "$X9/complete/Off (2008)/Off (2008) Remux-2160p.mkv" ] && echo kept || echo gone)" kept
rm -f "$X9/.push-off"
ck "unknown args are refused"                         "$(bash "$P" --bogus 2>/dev/null; echo $?)" 2

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
