#!/bin/bash
# watch-encode.sh <slug> <folder> <source-filename> <output-filename> <handbrake-pid> [crf]
#
# Emits QUARTER events at 25/50/75% with a band verdict, then COMPLETE or FAILED.
# Lives on the Crucial X9, NOT in /tmp — the scratchpad gets wiped on reboot and by
# periodic /tmp cleanup, which killed monitors mid-job twice (exit 127).
#
# Liveness is `kill -0 $PID`, NOT `pgrep -f "...$FOLDER"`: movie folders contain "(YYYY)"
# and pgrep treats parens as regex groups, so the pattern never matches the real process
# and the watcher reports a phantom FAILED on its first tick.
#
# AUTO-KILL (2026-08-20, at the user's direction; band rules 2026-08-31): when the
# projection lands OUTSIDE the 30-80% target band, this script KILLS the encode and
# deletes the partial. Steel Magnolias flagged NOSAVING->CRF18 at its 25% checkpoint
# and nobody was listening, so it ran a further ~12 hours to produce a file 1% smaller
# than its source. Detection without authority to act is just a slower way to waste a day.
#
# THE BAND CHECK RUNS EVERY TICK once the projection opens at 5% progress (the same
# opening point the dashboard's strip uses) — a quarter checkpoint on a 4.5 h encode
# is over an hour away, and the 2026-08-31 Addams Family 2 encode ran its full 4.5 h
# to produce a 9.6% file the verdict then refused. Two guards keep the early
# projection honest:
#   - CUR=0 is a stat glitch (or a file not yet created), never a 0% projection —
#     the tick is skipped, because killing a healthy encode over a momentary
#     unreadable stat deletes hours of work.
#   - TWO consecutive ticks must agree on the same violation before the kill fires,
#     so a single noisy sample (studio logos, black frames) cannot kill on its own.
#
# THE LADDER RUNS BOTH WAYS (operator's rule, 2026-08-31):
#   projection > 80%  (too big)  -> next CRF UP:   14-16-18-20-22, then none-too-big
#   projection < 30%  (too small)-> next CRF DOWN: 14-12-10, then none-too-small
# The ladder PIVOTS on the default start rung, which moved 16 -> 14 on 2026-09-03.
# Both arms have to move with it: leaving the pivot at 16 makes 14 a down-only rung,
# so every default encode that came in too big would exhaust to none-too-big on its
# first kill with no rung left to try -- and CRF 14 makes a BIGGER file than 16, so
# too big is exactly the direction the default change makes more likely.
# A violation in the OPPOSITE direction of a rung already laddered to (too small at
# 16/18/20/22, too big at 12/10) is "none-*" immediately: a source whose projection
# flips sides between adjacent rungs would otherwise oscillate forever. "none-*"
# tells .autopilot.sh to mark the title's ERROR state and move on — never delete,
# never skip, never halt.
#
# Set SMELTR_NO_AUTOKILL=1 to return to report-only behaviour.
# (SMELTER_NO_AUTOKILL is still honoured -- the app was renamed 2026-08-21 and a
#  watcher launched before the rename is still running against the old name.)
SLUG="$1"; FOLDER="$2"; SRCNAME="$3"; OUTNAME="$4"; HBPID="$5"; CRF="${6:-14}"
LOG="/tmp/handbrake-${SLUG}.log"
# X9 log override: HandBrake logs now live on the X9 too, so /tmp cleanup can't
# strand the watcher against a vanished log. Prefer it when present.
[ -f "/Volumes/Crucial X9/4K Movies/.hb-${SLUG}.log" ] && LOG="/Volumes/Crucial X9/4K Movies/.hb-${SLUG}.log"
BASE="/Volumes/Crucial X9/4K Movies/${FOLDER}"
SRC="${BASE}/${SRCNAME}"; OUT="${BASE}/${OUTNAME}"
NEXT=25
STRIKES=0; STRIKEDIR=""
SRCSZ=$(stat -f%z "$SRC")

while true; do
  PCT=$(tr '\r' '\n' < "$LOG" 2>/dev/null | grep -o "task [0-9]* of [0-9]*, *[0-9.]*" | tail -1 | grep -o "[0-9.]*$")
  [ -z "$PCT" ] && PCT=0

  if ! kill -0 "$HBPID" 2>/dev/null; then
    if grep -qi "Finished work\|Encode done" "$LOG" 2>/dev/null; then
      CUR=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
      echo "COMPLETE|${FOLDER}|$(date '+%Y-%m-%d %H:%M:%S')|$(awk -v c=$CUR -v s=$SRCSZ 'BEGIN{printf "%.2f GB from %.2f GB (%.0f%% smaller)", c/1073741824, s/1073741824, (1-c/s)*100}')"
    else
      echo "FAILED|${FOLDER}|HandBrakeCLI died at ${PCT}% (reboot/kill?) — delete partial and restart"
    fi
    break
  fi

  # The projection, once it opens at 5%. CUR=0 is a stat glitch, never data.
  RATIO=""
  if awk -v p="$PCT" 'BEGIN{exit !(p>=5)}'; then
    CUR=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
    [ "$CUR" -gt 0 ] && RATIO=$(awk -v p="$PCT" -v c="$CUR" -v s="$SRCSZ" 'BEGIN{printf "%.1f", (c/(p/100))/s*100}')
  fi

  # Quarter checkpoints: reporting only — the kill authority lives below.
  if awk -v p="$PCT" -v n="$NEXT" 'BEGIN{exit !(p>=n)}'; then
    CUR=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
    ETA=$(tr '\r' '\n' < "$LOG" | grep -o "ETA [0-9hms]*" | tail -1)
    awk -v p="$PCT" -v c="$CUR" -v s="$SRCSZ" -v n="$NEXT" -v e="$ETA" -v f="$FOLDER" 'BEGIN{
      proj=c/(p/100); r=proj/s*100
      v=(r>80 ? "OVER-BAND(>80%)" : r<30 ? "UNDER-BAND(<30%)" : "OK(30-80%)")
      printf "QUARTER|%s|%d%% (actual %.2f%%)|current %.2f GB|projected %.2f GB|original %.2f GB|%.1f%% of original|%s|%s\n",
        f, n, p, c/1073741824, proj/1073741824, s/1073741824, r, v, e}'
    NEXT=$((NEXT+25)); [ $NEXT -gt 75 ] && NEXT=101
  fi

  # The band check, every tick. Two consecutive agreeing violations kill.
  if [ -n "$RATIO" ] && [ "${SMELTR_NO_AUTOKILL:-${SMELTER_NO_AUTOKILL:-0}}" != "1" ]; then
    DIR=""
    awk -v r="$RATIO" 'BEGIN{exit !(r>80)}' && DIR=big
    awk -v r="$RATIO" 'BEGIN{exit !(r<30)}' && DIR=small
    if [ -n "$DIR" ] && [ "$DIR" = "$STRIKEDIR" ]; then
      STRIKES=$((STRIKES+1))
    else
      STRIKES=1; STRIKEDIR="$DIR"
    fi
    [ -z "$DIR" ] && { STRIKES=0; STRIKEDIR=""; }

    if [ -n "$DIR" ] && [ "$STRIKES" -ge 2 ]; then
      if [ "$DIR" = "big" ]; then
        case "$CRF" in
          14) NEXTCRF=16 ;; 16) NEXTCRF=18 ;; 18) NEXTCRF=20 ;; 20) NEXTCRF=22 ;;
          *)  NEXTCRF=none-too-big ;;
        esac
      else
        case "$CRF" in
          14) NEXTCRF=12 ;; 12) NEXTCRF=10 ;;
          *)  NEXTCRF=none-too-small ;;
        esac
      fi
      kill "$HBPID" 2>/dev/null
      for _ in $(seq 1 60); do kill -0 "$HBPID" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$HBPID" 2>/dev/null && kill -9 "$HBPID" 2>/dev/null
      # The partial is worthless and would otherwise be mistaken for a finished
      # encode by the sync and record steps, both of which key off "2160p HEVC".
      rm -f "$OUT"
      echo "KILLED|${FOLDER}|projected ${RATIO}% of original at ${PCT}% (CRF ${CRF})|band 30-80|partial deleted|next: CRF ${NEXTCRF}"
      break
    fi
  fi

  sleep 60
done
