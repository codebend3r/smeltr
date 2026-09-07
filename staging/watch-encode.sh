#!/bin/bash
# watch-encode.sh <slug> <folder> <source-filename> <output-filename> <handbrake-pid> [quality] [encoder]
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
# projection lands OUTSIDE the 10-70% target band, this script KILLS the encode and
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
#   projection > 70%  (too big)  -> next CRF UP:   14-16-18-20-22, then FINISH at 22
#   projection < 10%  (too small)-> next CRF DOWN: 14-12-10, then FINISH at 10
#   (10-70% since 2026-09-06, operator's call; was 30-80%. BAND_LO/BAND_HI below
#   are the ONE place the numbers live in this script.)
#
# PAST THE LAST RUNG THE ENCODE IS NOT KILLED (operator's rule, 2026-09-06).
# There is no better rung to retry at, so the run in flight IS the answer: it
# runs to completion at the terminal rung and this script emits FINAL| instead
# of KILLED|, then stops band-checking. Killing there produced no output at all
# and burned the whole encode; an out-of-band file is something a human can
# judge. THE VERDICT, NOT THE LADDER, DECIDES WHAT HAPPENS TO THE ORIGINAL --
# and the two arms end differently, so do not read this as "nothing is
# deleted":
#   too BIG  -> the finished file is >70% of source -> `no-saving` -> the
#              driver marks the ERROR state and the library original survives.
#   too SMALL-> anything from 15% to 70% of source is a `good` verdict (the
#              band is a TARGET, not a defect threshold -- Kubo 23.7% and
#              Minions 17.2% are both good), so it SYNCS and the ~90 GB
#              library original IS DELETED unattended. Under the old
#              kill-at-the-end behaviour that encode never existed to be
#              judged. Below 15% the absolute floor still catches it
#              (`suspect`, no deletion).
#
# AND IT IS ENCODER-AWARE (2026-08-25). Those numbers are x265 CRF, where LOWER
# means a bigger file. VideoToolbox quality is CQ on Apple's reversed scale,
# where HIGHER means a bigger file, so both arms invert: too big steps CQ DOWN
# 70-65-60-55-50, too small steps CQ UP 70-75-...-100. Mapping a rung with the
# wrong scale would re-run the blowup LARGER, so the mapping lives in one
# function keyed on the encoder, and the KILLED line reports only the next
# NUMBER ("next: Q 55") -- the driver passes it back as a quality override
# without knowing either scale. It also names the encoder that produced the
# rung, so a driver holding a rung from a DIFFERENT encoder can drop it rather
# than read 18 as a CQ.
#
# THE STARTING QUALITY DOES NOT MATTER (operator's rule, 2026-09-07): every
# rung steps in BOTH directions, all the way to its own menu's ends. There is
# no pivot any more and no one-directional rung. See next_rung() for what that
# replaced and why.
#
# Set SMELTR_NO_AUTOKILL=1 to return to report-only behaviour.
# (SMELTER_NO_AUTOKILL is still honoured -- the app was renamed 2026-08-21 and a
#  watcher launched before the rename is still running against the old name.)
SLUG="$1"; FOLDER="$2"; SRCNAME="$3"; OUTNAME="$4"; HBPID="$5"; Q="${6:-70}"; ENC="${7:-vt_h265_10bit}"

# The rung menus, ordered SMALLEST FILE FIRST. That ordering is what lets one
# direction rule cover two opposite scales: "too big" steps one rung towards a
# smaller file, "too small" one rung towards a bigger one. x265 quality is CRF,
# where a HIGHER number is a smaller file; VideoToolbox quality is CQ on
# Apple's reversed scale, where a LOWER number is a smaller file. A rung mapped
# with the wrong scale re-runs the blowup LARGER, so the inversion is expressed
# ONCE, here, as the order of each list -- never as hand-written per-encoder
# arms that have to be kept mirrored by eye.
rung_menu() { # $1=encoder -- smallest file first
  case "$1" in
    vt_h265_10bit) echo "50 55 60 65 70 75 80 85 90 95 100" ;;
    *)             echo "22 20 18 16 14 12 10" ;;
  esac
}

# Next rung for this encoder and direction, or "none-too-*" past the END OF THE
# MENU -- and only there.
#
# THE STARTING QUALITY DOES NOT MATTER (operator's rule, 2026-09-07). Every
# rung steps both ways. This replaced a one-directional rule that keyed
# exhaustion on which side of the default a rung sat: a rung above the default
# could only step further up, on the theory that it had been laddered up to and
# stepping back would oscillate. That theory holds for a rung the ladder
# reached on its own and is false for a hand-picked START rung -- which is what
# `encoder_overrides.json` writes. The 2026-09-06 batch pinned every title at
# CQ 75, so `next_rung vt_h265_10bit 75 big` answered none-too-big and Shazam
# (2019) ran to 37% projecting 131% OF SOURCE with CQ 70/65/60/55/50 sitting
# unused underneath it. The ladder had no way down from the rung the whole
# batch started on.
#
# What that trade gives up: a projection that flips sides between adjacent
# rungs can now walk back rather than exhausting. Two things bound it -- the
# caller needs TWO consecutive agreeing ticks before it acts, and each menu is
# finite, so any walk ends at CRF 22/10 or CQ 50/100. An unbounded oscillation
# was the reason for the old rule; a blowup that cannot be stepped away from is
# worse, and it is the one that actually happened.
#
# An OFF-MENU quality snaps to the nearest rung on the requested side instead
# of exhausting. It is only reachable by hand-editing the override file --
# core.encoder_for() can return a menu entry and nothing else -- but refusing
# to ladder a number somebody typed is how a blowup runs unopposed, which is
# exactly the failure above. An unknown ENCODER takes the x265 menu, the proven
# path; a CRF number handed to the VT arm (or the reverse) is off that menu
# entirely and snaps to its nearest end rather than being read on the wrong
# scale.
next_rung() { # $1=encoder $2=quality $3=big|small
  awk -v cur="$2" -v dir="$3" -v menu="$(rung_menu "$1")" 'BEGIN{
    n = split(menu, r, " ")
    # The VT menu ascends (50..100) and the CRF menu descends (22..10); both
    # are smallest-file-first, so this is how "smaller file" reads numerically.
    asc = (r[1] + 0 < r[n] + 0)
    best = ""
    if (dir == "big") {
      # Towards a smaller file. Keeping the LAST match walking up the list
      # yields the closest such rung, so an on-menu quality moves exactly one.
      for (i = 1; i <= n; i++) {
        smaller = asc ? (r[i] + 0 < cur + 0) : (r[i] + 0 > cur + 0)
        if (smaller) best = r[i]
      }
      if (best == "") { print "none-too-big"; exit }
    } else {
      for (i = n; i >= 1; i--) {
        bigger = asc ? (r[i] + 0 > cur + 0) : (r[i] + 0 < cur + 0)
        if (bigger) best = r[i]
      }
      if (best == "") { print "none-too-small"; exit }
    }
    print best
  }'
}

# The scale a quality number is on. CRF (x265, LOWER = bigger file) and CQ
# (VideoToolbox, Apple's reversed scale, HIGHER = bigger file) are NOT
# comparable, so a bare "Q10" beside a "Q70" describing the same situation is
# unreadable. Every number this script puts in front of a human carries its
# scale -- the same rule qLabel() enforces on the dashboard.
q_label() { # $1=encoder
  case "$1" in vt*) echo "VT CQ" ;; *) echo CRF ;; esac
}

# Sourced-for-test escape hatch: tests read next_rung() without touching the
# filesystem or running the loop. Must sit before the stat below. `return`
# outside a function does NOT stop an EXECUTED script under bash 3.2, so the
# exit fallback keeps a stray WE_TEST=1 in the environment from falling
# through into the live loop.
if [ "${WE_TEST:-0}" = "1" ]; then return 0 2>/dev/null || exit 0; fi

LOG="/tmp/handbrake-${SLUG}.log"
# X9 log override: HandBrake logs now live on the X9 too, so /tmp cleanup can't
# strand the watcher against a vanished log. Prefer it when present.
[ -f "/Volumes/Crucial X9/4K Movies/.hb-${SLUG}.log" ] && LOG="/Volumes/Crucial X9/4K Movies/.hb-${SLUG}.log"
# Layout (2026-09-07): movie folders live in queue/; a folder still at the
# root is the old layout. Same fallback as the driver's folder_dir().
BASE="/Volumes/Crucial X9/4K Movies/queue/${FOLDER}"
[ -d "$BASE" ] || BASE="/Volumes/Crucial X9/4K Movies/${FOLDER}"
SRC="${BASE}/${SRCNAME}"; OUT="${BASE}/${OUTNAME}"
NEXT=25
STRIKES=0; STRIKEDIR=""
# The directions the ladder has already run out of, space-separated ("big",
# "small"). PER DIRECTION, never a single flag: a low reading at CRF 22 (a rung
# reached because the encode was too BIG) has no rung left downwards and is
# reported once -- but CRF 22 still has to be watched for the blowup that put
# it there, and a mid-ladder rung like CRF 16 still has a real up-rung to
# ladder to. A single flag switched the whole band check off, so one noisy
# 6%-progress sample removed the only ceiling on the rest of a multi-hour run.
FINAL_DIRS=""
# The target band, % of source. Both kill checks, both log wordings and the
# QUARTER label read these two -- a number typed anywhere else drifts.
BAND_LO=10
BAND_HI=70
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
    awk -v p="$PCT" -v c="$CUR" -v s="$SRCSZ" -v n="$NEXT" -v e="$ETA" -v f="$FOLDER" -v lo="$BAND_LO" -v hi="$BAND_HI" 'BEGIN{
      proj=c/(p/100); r=proj/s*100
      v=(r>hi ? "OVER-BAND(>" hi "%)" : r<lo ? "UNDER-BAND(<" lo "%)" : "OK(" lo "-" hi "%)")
      printf "QUARTER|%s|%d%% (actual %.2f%%)|current %.2f GB|projected %.2f GB|original %.2f GB|%.1f%% of original|%s|%s\n",
        f, n, p, c/1073741824, proj/1073741824, s/1073741824, r, v, e}'
    NEXT=$((NEXT+25)); [ $NEXT -gt 75 ] && NEXT=101
  fi

  # The band check, every tick. Two consecutive agreeing violations kill.
  if [ -n "$RATIO" ] && [ "${SMELTR_NO_AUTOKILL:-${SMELTER_NO_AUTOKILL:-0}}" != "1" ]; then
    DIR=""
    awk -v r="$RATIO" -v hi="$BAND_HI" 'BEGIN{exit !(r>hi)}' && DIR=big
    awk -v r="$RATIO" -v lo="$BAND_LO" 'BEGIN{exit !(r<lo)}' && DIR=small
    if [ -n "$DIR" ] && [ "$DIR" = "$STRIKEDIR" ]; then
      STRIKES=$((STRIKES+1))
    else
      STRIKES=1; STRIKEDIR="$DIR"
    fi
    [ -z "$DIR" ] && { STRIKES=0; STRIKEDIR=""; }

    # A direction already reported as out of rungs is not re-reported: every
    # later tick would say the same thing with the same answer. The OTHER
    # direction keeps its full strike-and-kill authority.
    case " $FINAL_DIRS " in
      *" $DIR "*) STRIKES=0; STRIKEDIR="" ;;
    esac

    if [ -n "$DIR" ] && [ "$STRIKES" -ge 2 ]; then
      NEXTQ=$(next_rung "$ENC" "$Q" "$DIR")
      case "$NEXTQ" in
        none-*)
          # END OF THE LADDER -> FINISH THE ENCODE (operator's rule, 2026-09-06).
          # There is no rung left to try in this direction, so the run this
          # script is watching is the best this ladder can produce: x265 CRF 22
          # (too big) / CRF 10 (too small), VideoToolbox CQ 50 (too big) /
          # CQ 70 (too small) -- and the same applies to a one-directional rung
          # violated in the direction it cannot step (the alternative there is
          # an oscillation, not a better rung). Killing here deleted hours of
          # work and left the title in the ERROR state with NO output at all;
          # an out-of-band file a human can look at beats no file.
          # The ladder stops deciding here; the VERDICT still does, and on the
          # too-small arm that verdict can be `good` (15-30% of source is
          # below the band but above the floor), which syncs and deletes the
          # library original. See the header -- the two arms do not end the
          # same way, and this branch must not be read as "nothing is
          # deleted".
          # Reported ONCE for THIS direction -- every later tick would say the
          # same thing with the same answer. The opposite direction keeps its
          # kill authority: a rung reached by laddering up is still watched for
          # the blowup that sent it there, and a mid-ladder rung still has a
          # real rung to step to on its own arm.
          # Self-stamped like COMPLETE, NOT left to the file's mtime: this
          # branch does not exit, so the loop writes QUARTER lines after it
          # and FINAL stops being the log's last line within the hour. An
          # mtime-derived stamp would silently become "—" on the one row that
          # is the only record of the ladder ending.
          # "no rung left in this direction" is the honest wording for all
          # eight cases that reach here: four are a genuine terminal rung
          # (CRF 22/10, CQ 50/70) and four are the oscillation guard -- a rung
          # violated in the direction its own arm cannot step. Calling a
          # too-small projection at CRF 16 "the last rung of the small arm"
          # names a rung that is not on that arm at all.
          QL=$(q_label "$ENC")
          echo "FINAL|${FOLDER}|$(date '+%Y-%m-%d %H:%M:%S')|projected ${RATIO}% of original at ${PCT}% (${ENC} ${QL} ${Q})|outside the ${BAND_LO}-${BAND_HI}% target band|no rung left for a too-${DIR} projection from ${QL} ${Q} - finishing there, not killed"
          FINAL_DIRS="$FINAL_DIRS $DIR"
          STRIKES=0; STRIKEDIR=""
          ;;
        *)
          kill "$HBPID" 2>/dev/null
          for _ in $(seq 1 60); do kill -0 "$HBPID" 2>/dev/null || break; sleep 0.5; done
          kill -0 "$HBPID" 2>/dev/null && kill -9 "$HBPID" 2>/dev/null
          # The partial is worthless and would otherwise be mistaken for a finished
          # encode by the sync and record steps, both of which key off "2160p HEVC".
          # The AppleDouble sidecar goes with it: on this exFAT volume the driver's
          # finished_folder() once matched a leftover `._*2160p HEVC*.mkv` as a
          # finished encode and errored the title instead of laddering (2026-09-06).
          rm -f "$OUT" "$(dirname "$OUT")/._$(basename "$OUT")"
          echo "KILLED|${FOLDER}|projected ${RATIO}% of original at ${PCT}% (${ENC} Q${Q})|band ${BAND_LO}-${BAND_HI}|partial deleted|next: Q ${NEXTQ}"
          break
          ;;
      esac
    fi
  fi

  sleep 60
done
