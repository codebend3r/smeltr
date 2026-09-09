#!/bin/bash
# A verdict exit 4 that says "died mid-write" is a RETRY, not the ERROR state
# (2026-09-07, operator's call: "this does not qualify as an error").
#
#   bash tests/test_midwrite_retry.sh [path-to-.autopilot.sh]
#
# WHAT WENT WRONG. `.watch-encode.sh` kills a band violation and deletes the
# partial; the driver's 30 s pass can land between those two steps, find an
# output with no live encoder, and hand it to verdict.py -- which correctly
# refuses to judge an unfinished file (exit 4, "died mid-write"). The driver
# mapped EVERY exit 4 to error_out(), so a title the ladder was about to
# retry got a `.error-` marker instead. pick_next passes over marked titles,
# so with a thin queue the encoder simply stopped: Minions 1h35m idle on
# 2026-09-06, Shazam 2h27m on 2026-09-07.
#
# `tests/test_watch_kill_race.sh` closes the window in the watcher. This is
# the other half: even with no race at all -- a HandBrake that segfaults
# leaves the same unfinished output -- an output that was never completed is
# a worthless partial, not something a human is owed a look at. There is
# nothing to review and nothing was produced.
#
# The one case that IS still a human's: an encode failing this way over and
# over with no KILLED line to explain it is HandBrake crashing, not the
# ladder working, and it must not spin forever.
set -uo pipefail

LIVE="/Volumes/Crucial X9/4K Movies/.autopilot.sh"
if [ -n "${1:-}" ]; then AP="$1"
elif [ -f "$LIVE" ]; then AP="$LIVE"
else AP="$(dirname "$0")/../staging/autopilot.sh"; echo "(staging drive not mounted - testing the tracked staging/autopilot.sh)"; fi
[ -f "$AP" ] || { echo "FAIL: $AP not found"; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; STAGE="$X9/queue"; COMPLETE="$X9/complete"
mkdir -p "$STAGE" "$COMPLETE"
# shellcheck disable=SC2034  # read by the helpers sourced below
LOG=/dev/null
# shellcheck disable=SC2034  # the helper block reads it at source time
SMELTR=/nonexistent
log() { :; }

awk '/^slug_of\(\)/,/^# Resolve the library folder/' "$AP" | sed '$d' > "$TMP/helpers.sh"
grep -q 'midwrite_route()' "$TMP/helpers.sh" || { echo "FAIL: midwrite_route() is not in the helper block"; exit 1; }
# shellcheck disable=SC1090
source "$TMP/helpers.sh"

# hb_running is stubbed: this machine may well be encoding for real while the
# suite runs, and the routing question is "what if an encode is alive", not
# "is one alive here".
HBR=1
hb_running() { return "$HBR"; }

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "PASS $1"; pass=$((pass+1))
      else echo "FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }

T="Sandbox (2020)"
SLUG=$(slug_of "$T")
mkdir -p "$STAGE/$T"
reset(){ rm -f "$X9/.midwrite-$SLUG" "$X9/.watch-$SLUG.log" "$X9/.error-$T"; }
partial(){ : > "$STAGE/$T/$T 2160p HEVC.mkv"; : > "$STAGE/$T/._$T 2160p HEVC.mkv"; }
haspartial(){ [ -n "$(find "$STAGE/$T" -maxdepth 1 -name '*2160p HEVC*' 2>/dev/null | head -1)" ] && echo yes || echo no; }

# --- 1. the ladder's own kill: retry, and never a marker ------------------
reset; partial
printf 'KILLED|%s|projected 500%%|band 30-80|partial deleted|next: Q 18\n' "$T" > "$X9/.watch-$SLUG.log"
HBR=1  # no encode running
ck "a killed encode routes to retry"        "$(midwrite_route "$T")" retry
ck "no error marker is written"             "$([ -e "$X9/.error-$T" ] && echo yes || echo no)" no
ck "a ladder retry banks no strike"         "$([ -e "$X9/.midwrite-$SLUG" ] && echo yes || echo no)" no

# --- 2. an encode is alive: prove nothing, touch nothing ------------------
# The partial may belong to a RUNNING encode that ps merely raced us on.
# Deleting it there would throw away a live encode's work.
reset; partial
HBR=0  # hb_running true
ck "a live encode routes to wait"           "$(midwrite_route "$T")" wait
ck "waiting banks no strike"                "$([ -e "$X9/.midwrite-$SLUG" ] && echo yes || echo no)" no
ck "waiting writes no marker"               "$([ -e "$X9/.error-$T" ] && echo yes || echo no)" no

# --- 3. a crash with no KILLED line: retry, but counted -------------------
# No watcher line means nothing explains the death, so this is HandBrake
# crashing rather than the ladder working. Retry -- but bounded, or the
# driver spins on one title forever.
reset; partial; HBR=1
ck "an unexplained death retries first"     "$(midwrite_route "$T")" retry
ck "and banks a strike"                     "$(cat "$X9/.midwrite-$SLUG" 2>/dev/null)" 1
ck "a second one retries too"               "$(midwrite_route "$T")" retry
ck "the strike count rises"                 "$(cat "$X9/.midwrite-$SLUG" 2>/dev/null)" 2
ck "the third is the human's"               "$(midwrite_route "$T")" error

# --- 4. a ladder line CLEARS the strikes ----------------------------------
# A ladder that legitimately walks five rungs must never trip the crash
# bound: each rung is progress, not a repeat of the same failure.
reset; partial; HBR=1
midwrite_route "$T" >/dev/null
midwrite_route "$T" >/dev/null
printf 'KILLED|%s|projected 500%%|band 30-80|partial deleted|next: Q 18\n' "$T" > "$X9/.watch-$SLUG.log"
ck "a ladder line clears the count"         "$(midwrite_route "$T")" retry
ck "the strike file is gone"                "$([ -e "$X9/.midwrite-$SLUG" ] && echo yes || echo no)" no
rm -f "$X9/.watch-$SLUG.log"
ck "so the next crash starts from one again" "$(midwrite_route "$T")" retry
ck "at strike 1"                            "$(cat "$X9/.midwrite-$SLUG" 2>/dev/null)" 1

# --- 4b. a strike the drive would not record is not a strike ---------------
# 2026-09-08: the X9 at 0 bytes free. `printf > "$f"` failed with ENOSPC,
# bash 3.2 flushed the unwritten count into the function's captured stdout
# ("1\nretry"), and the caller errored the title on its FIRST death as
# "never completed on 3 attempts". Two shapes of "cannot record": a strike
# path that cannot be OPENED (a directory stands in), and one that opens but
# cannot be WRITTEN -- a real 1 MiB HFS+ image filled to the last byte, the
# only way to make bash's stdio take the failed-flush path. Each must answer
# exactly one word, always retry, never error.
reset; partial; HBR=1
mkdir -p "$X9/.midwrite-$SLUG"
r1=$(midwrite_route "$T"); r2=$(midwrite_route "$T"); r3=$(midwrite_route "$T")
ck "an unopenable strike answers one word"  "$(printf '%s' "$r1" | wc -l | tr -d ' ')" 0
ck "and never escalates: first"             "$r1" retry
ck "and never escalates: second"            "$r2" retry
ck "and never escalates: third"             "$r3" retry
ck "no error marker is written"             "$([ -e "$X9/.error-$T" ] && echo yes || echo no)" no
rmdir "$X9/.midwrite-$SLUG"

if command -v hdiutil >/dev/null 2>&1; then
  IMG="$TMP/full.dmg"
  hdiutil create -quiet -size 1m -fs HFS+ -volname SMELTRFULL "$IMG" -ov >/dev/null 2>&1
  FULL=$(hdiutil attach -nobrowse "$IMG" 2>/dev/null | awk '/SMELTRFULL/{print $NF}')
  if [ -n "$FULL" ] && [ -d "$FULL" ]; then
    : > "$FULL/.midwrite-$SLUG"
    for bs in 65536 4096 512 1; do i=0
      while dd if=/dev/zero of="$FULL/fill.$bs.$i" bs=$bs count=1 2>/dev/null; do
        i=$((i+1)); [ $i -gt 4000 ] && break
      done
    done
    # The group is what silences it: `> file 2>/dev/null` sets up the failing
    # redirect BEFORE stderr is moved, so bash reports ENOSPC on the real one.
    if { printf 'x' > "$FULL/.probe"; } 2>/dev/null && [ "$(stat -f%z "$FULL/.probe")" = 1 ]; then
      echo "(could not fill the test image to the last byte - skipping the ENOSPC half)"
    else
      # No reset here: it would delete the pre-created strike file, and a
      # CREATE on a full volume fails at open (no leak) -- the bug needs an
      # existing file that opens and then refuses the write.
      SAVE_X9="$X9"; X9="$FULL"; HBR=1
      r1=$(midwrite_route "$T"); r2=$(midwrite_route "$T"); r3=$(midwrite_route "$T")
      X9="$SAVE_X9"
      ck "ENOSPC: the answer is one word, nothing leaks" "$(printf '%s' "$r1" | wc -l | tr -d ' ')" 0
      ck "ENOSPC: first death retries"      "$r1" retry
      ck "ENOSPC: second death retries"     "$r2" retry
      ck "ENOSPC: third death still retries (no strike was ever recorded)" "$r3" retry
    fi
    hdiutil detach -quiet "$FULL" >/dev/null 2>&1 || hdiutil detach -force -quiet "$FULL" >/dev/null 2>&1
  else
    echo "(hdiutil could not attach a test image - skipping the ENOSPC half)"
  fi
else
  echo "(no hdiutil - skipping the ENOSPC half)"
fi

# --- 5. drop_partial removes every shape of the corpse --------------------
# The output, its AppleDouble sidecar, and the `.killing` rename the watcher
# leaves if it is killed between the rename and its own cleanup.
reset; partial
: > "$STAGE/$T/$T 2160p HEVC.mkv.killing"
: > "$STAGE/$T/._$T 2160p HEVC.mkv.killing"
: > "$STAGE/$T/$T Remux-2160p.mkv"          # the SOURCE must survive
drop_partial "$T"
ck "every partial shape is deleted"         "$(haspartial)" no
ck "the source is untouched"                "$([ -e "$STAGE/$T/$T Remux-2160p.mkv" ] && echo yes || echo no)" yes

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
