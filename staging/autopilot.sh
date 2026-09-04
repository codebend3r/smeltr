#!/bin/bash
# Unattended driver for the 4K HEVC re-encode job.
#
# Runs the full cycle with nobody watching: start encode -> wait -> judge ->
# record -> sync -> purge -> replenish -> next. It is the piece that was always
# missing; every prior "autonomous" run still needed a human to notice a
# COMPLETE and type the next command.
#
# IT NEVER GUESSES, AND IT NEVER STOPS (operator's standing rule, 2026-09-04).
# The only path that deletes a library original is a `good` verdict. suspect /
# thin / downscale / decoder-errors / an unresolvable library original all put
# THAT TITLE into the ERROR state -- `$X9/.error-<title>` marker, output and
# source left intact on the staging drive, red row on the dashboard -- and the
# loop moves on to the next title. Flight (2012) projected 9.4% of source and
# was genuinely fine, but that took an SSIM measurement to establish, not a
# size comparison, so the CALL stays with a human; the CPU does not wait for
# it. A halt on The Little Mermaid at 04:05 on 2026-09-04 idled the encoder
# for five hours with nine staged titles waiting; that is the last one.
#
# Usage:  nohup ./.autopilot.sh          >> .autopilot.log 2>&1 &
#         ./.autopilot.sh --dry-run      # decide and print, change nothing
set -uo pipefail

X9="/Volumes/Crucial X9/4K Movies"
SMELTR="$HOME/Developer/git/smeltr/smeltr"
LOG="$X9/.autopilot.log"
STOP_MBPS=70
DRY=false
[ "${1:-}" = "--dry-run" ] && DRY=true

# Write to stdout only; the caller redirects into $LOG. Using tee AND a stdout
# redirect wrote every line twice.
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
# The ERROR state. One title, never the pipeline: write the marker core.py
# reads (error_marker), log it, and let the caller move on. finished_folder()
# and next_title.py both pass over a marked title, so a folder holding a
# source AND a finished output is neither re-judged every pass nor re-encoded.
# Nothing here deletes anything; a human ends the state by deleting the marker.
error_out() {
  local title="$1"; shift
  printf '%s: %s at %s\n' "$title" "$*" "$(date '+%Y-%m-%d %H:%M:%S')" > "$X9/.error-$title"
  log "ERROR $title: $* - marked for review, moving on"
  log "  Nothing was deleted. Delete $X9/.error-$title after review."
}

# Single instance. Two drivers would both pick "the next title" and both start it.
LOCK="$X9/.autopilot.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "autopilot already running (lock: $LOCK)"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

slug_of() { echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]' | cut -c1-20; }

# -x matches the process NAME exactly. -f would also match any shell whose
# arguments merely mention HandBrakeCLI, and a false positive here leaves the
# CPU idle because the loop thinks an encode is already running.
hb_running() { pgrep -x HandBrakeCLI >/dev/null 2>&1; }

# Is HandBrake writing into THIS folder right now? A running encode's output
# already matches "*2160p HEVC*.mkv", so without this the loop hands a live
# encode to verdict.py, which returns 4 and halts the driver. Matched with a
# literal `case` glob, never a regex: folder names contain "(YYYY)" and pgrep
# would read the parens as a capture group and never match.
encoding_this() {
  local pid args
  for pid in $(pgrep -x HandBrakeCLI 2>/dev/null); do
    args=$(ps -p "$pid" -o args= 2>/dev/null)
    case "$args" in *"/$1/"*) return 0 ;; esac
  done
  return 1
}

# A sync now runs in the background, so its folder survives on disk until
# .sync-to-library.sh removes it at the very end. Without this marker the loop
# would re-find that folder 30s later and judge + RECORD it a second time --
# core.record() appends blindly, and a duplicate row double-counts the reclaim.
sync_in_flight() {
  local sent="$X9/.syncing-$(slug_of "$1")" pid
  [ -f "$sent" ] || return 1
  pid=$(cat "$sent" 2>/dev/null)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
  # stderr, NOT stdout: finished_folder() is captured with $(...) and calls this,
  # so a line on stdout would be prepended to the folder name it returns. The
  # caller redirects 2>&1, so this still lands in .autopilot.log.
  log "STALE sync marker for $1 (pid ${pid:-none} gone) - clearing, folder will be re-judged" >&2
  rm -f "$sent"
  return 1
}

syncs_in_flight() {
  local f pid
  for f in "$X9"/.syncing-*; do
    [ -e "$f" ] || continue
    pid=$(cat "$f" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
  done
  return 1
}

# The one staged folder holding a FINISHED output: not the one being encoded,
# not one already travelling to the NAS.
finished_folder() {
  local d b
  while IFS= read -r d; do
    b=$(basename "$d")
    ls "$d"/*"2160p HEVC"*.mkv >/dev/null 2>&1 || continue
    # An ERROR-state title keeps its finished output beside the source for a
    # human to look at; re-judging it every 30 s pass would just re-fail it.
    [ -e "$X9/.error-$b" ] && continue
    encoding_this "$b" && continue
    sync_in_flight "$b" && continue
    printf '%s\n' "$b"
    return
  done < <(find "$X9" -mindepth 1 -maxdepth 1 -type d ! -name ".*")
}

# Record + push + purge + replenish, detached, so the CPU is never idle waiting
# on a 45-minute push. Deletion safety is unchanged: .sync-to-library.sh still
# copies, size-verifies, ffprobe-verifies and track-verifies BEFORE it removes
# the library original, and still aborts leaving everything in place.
sync_async() {
  local folder="$1" srcpath="$2" dest="$3" slug
  slug=$(slug_of "$folder")
  (
    out=$(find "$X9/$folder" -maxdepth 1 -name '*2160p HEVC*.mkv' ! -name '._*' | head -1)
    # record.py refuses an output touched in the last 120s -- a still-growing
    # file would write a permanently wrong size into the ledger. Wait it out
    # here rather than blocking the loop.
    while [ -n "$out" ]; do
      age=$(( $(date +%s) - $(stat -f%m "$out" 2>/dev/null || echo 0) ))
      [ "$age" -ge 125 ] && break
      sleep $(( 125 - age ))
    done

    if [ -f "$X9/.recorded-$slug" ]; then
      log "RECORD $folder - already recorded before an interrupted sync, skipping"
    else
      log "RECORD $folder"
      "$SMELTR" record "$folder" --dest "$dest" --source-path "$srcpath" \
        --note "autopilot" 2>&1 || {
          log "SYNC ABORTED: record failed for $folder - nothing deleted"
          rm -f "$X9/.syncing-$slug"; exit 1; }
      : > "$X9/.recorded-$slug"
    fi

    log "SYNC $folder"
    bash "$X9/.sync-to-library.sh" "$folder" 2>&1 || {
      log "SYNC FAILED for $folder - nothing deleted, folder left for review"
      rm -f "$X9/.syncing-$slug"; exit 1; }

    log "PURGE recycle bins"
    bash "$X9/.purge-recycle.sh" >/dev/null 2>&1 || log "  (purge reported an error, continuing)"

    # Staging the next pull only starts now, once the push has freed its space.
    log "REPLENISH"
    bash "$X9/.replenish-queue.sh" 2>&1

    rm -f "$X9/.syncing-$slug" "$X9/.recorded-$slug"
    log "CYCLE COMPLETE $folder"
  ) >> "$LOG" 2>&1 &
  # Claimed from the parent: the loop is single-threaded, so it cannot reach
  # finished_folder() again between the & above and this write.
  echo "$!" > "$X9/.syncing-$slug"
  log "  sync dispatched for $folder (pid $!) - encoding continues"
}

# Highest-bitrate staged title with no output yet, above the cutoff.
# Structured command, never a scrape of the rendered table -- the first version
# parsed column 2 (RANK, not Mb/s) and would have declared the job finished.
# Exit 1 = stop condition, 2 = library not fully mounted (never treat as done),
# 3 = wait, don't exit: paused from the dashboard, every staged title
#     hand-skipped, or a replenish pull still landing.
# stderr lands in a FILE, never the $(...) that captures the title (the
# sync_in_flight lesson) -- and the reason logged on a wait comes from the
# SAME invocation as the exit code. A second call re-ran the whole queue
# scan and raced the first: the state could change between them, logging a
# reason that was no longer true, or nothing at all.
# The file lives OFF the staging drive. A redirect that cannot open returns
# 1 WITHOUT running smeltr -- and 1 is the stop condition, so a reason file
# on an X9 gone read-only would have logged "job finished" on a drive that
# still reads. The probe degrades to /dev/null: reason lost, exit code
# intact -- the write must never be able to invent an exit code.
NEXT_REASON="${TMPDIR:-/tmp}/smeltr-next-reason"
next_title() {
  : >"$NEXT_REASON" 2>/dev/null || NEXT_REASON=/dev/null
  "$SMELTR" next "$STOP_MBPS" 2>"$NEXT_REASON"
}
next_reason() { tr '\n' ' ' 2>/dev/null <"$NEXT_REASON"; }

# The ONE list of library roots in this script; library_path_of and
# library_roots_online must never disagree about what "the library" is.
# (Still one of the four hand-synced copies CLAUDE.md documents.)
LIB_ROOTS=("/Volumes/Vhagar/Media/4K Movies" "/Volumes/Vermithor/Media/4K Movies" \
           "/Volumes/Vermithor/Media/4K Family Movies")

# Can this process actually SEE the library right now? An empty find is two
# very different facts -- "the title is not there" and "the NAS is not there"
# -- and halting on the second cost 4h16m of encoding on 2026-08-31 when a
# mount blip hit the exact second the judge block ran. A root is online when
# it lists at least one entry: every root always holds letter buckets, and an
# unmounted /Volumes path either vanishes or reads empty.
library_roots_online() {
  local r
  for r in "${LIB_ROOTS[@]}"; do
    [ -n "$(find "$r" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] || return 1
  done
  return 0
}

# Resolve the library folder the same way .sync-to-library.sh does: exactly
# one <root>/<bucket>/<title> match across all roots. Never guess the bucket
# from the first letter -- "A Bug's Life (1998)" files under B and "1917
# (2019)" under "#", and a wrong guess here halts the driver AFTER a
# multi-hour encode. Zero or multiple matches return nothing, and the caller
# halts loudly before record() can write a row against the wrong file.
library_path_of() {
  local title="$1" matches n
  matches=$(find "${LIB_ROOTS[@]}" \
                 -mindepth 2 -maxdepth 2 -type d -name "$title" 2>/dev/null)
  n=$(printf '%s\n' "$matches" | grep -c . || true)
  [ "$n" -eq 1 ] || return 0
  find "$(printf '%s\n' "$matches" | head -1)" -maxdepth 1 -name '*.mkv' \
       ! -name '._*' ! -name '*2160p HEVC*' | head -1
}

start_encode() {
  local title="$1" crf="${2:-16}" slug src out
  slug=$(slug_of "$title")
  src=$(find "$X9/$title" -maxdepth 1 -name '*.mkv' ! -name '._*' ! -name '*2160p HEVC*' | head -1)
  [ -z "$src" ] && { error_out "$title" "no source file"; return 1; }
  # Named after the FOLDER, not the source file. An earlier version derived it
  # from the source basename with a sed strip and was overwritten on the very
  # next line -- a dead store that read like it was doing the naming.
  out="$X9/$title/${title} 2160p HEVC.mkv"
  log "START $title at CRF $crf"
  $DRY && { log "(dry run) would encode: $src -> $out"; return 0; }

  nohup HandBrakeCLI -i "$src" -o "$out" \
    -f av_mkv -e x265_10bit -q "$crf" --encoder-preset medium \
    --all-audio --aencoder copy --audio-fallback ac3 --all-subtitles \
    > "$X9/.hb-${slug}.log" 2>&1 &
  local pid=$!
  log "  HandBrake pid $pid"

  # Track verification is not optional: a multi-hour encode that dropped audio
  # or subtitles is a multi-hour waste, and unattended it would be synced.
  for _ in $(seq 1 120); do
    grep -q 'job configuration:' "$X9/.hb-${slug}.log" 2>/dev/null && break
    sleep 1
  done
  local sa ss oa os
  oa=$(tr '\r' '\n' < "$X9/.hb-${slug}.log" | grep -cE '^\[[0-9:]+\][[:space:]]+\* audio track')
  os=$(tr '\r' '\n' < "$X9/.hb-${slug}.log" | grep -cE '^\[[0-9:]+\][[:space:]]+\* subtitle track')
  sa=$(ffprobe -v error -show_streams "$src" | grep -c '^codec_type=audio')
  ss=$(ffprobe -v error -show_streams "$src" | grep -c '^codec_type=subtitle')
  if [ "$oa" != "$sa" ] || [ "$os" != "$ss" ]; then
    kill "$pid" 2>/dev/null; rm -f "$out"
    error_out "$title" "track mismatch: source ${sa}a/${ss}s but job writes ${oa}a/${os}s"
    return 1
  fi
  log "  tracks OK ${oa}a/${os}s"

  nohup "$X9/.watch-encode.sh" "$slug" "$title" "$(basename "$src")" "$(basename "$out")" "$pid" "$crf" \
    >> "$X9/.watch-${slug}.log" 2>&1 &
  log "  watcher pid $! (detached)"
}

# ---------------------------------------------------------------- main loop

log "=== autopilot up (dry-run=$DRY) ==="
deferred=""

# Two independent jobs per pass, in this order, so a finished encode hands its
# push to the background and the NEXT encode starts in the same iteration --
# transfer and encode overlap, and so do the replenish pull and the encode.
while true; do

  # ---- 1. A finished encode: judge here (fast), then detach record + sync.
  #         Resolution runs FIRST, and an empty result is only a halt when the
  #         roots are provably reachable. A NAS blip at this exact line used to
  #         halt the whole loop -- including the next encode -- for as long as
  #         nobody was awake (4h16m on 2026-08-31). Now it DEFERS: the folder
  #         stays put, the loop re-finds it every pass and retries, and step 2
  #         below still starts the next encode. Judging waits with the sync (it
  #         needs nothing from the NAS, but re-judging every 30s pass of a long
  #         outage is pure log spam). Nothing about deletion changes: record,
  #         sync, and the verify-before-delete all still happen, just later.
  done_folder=$(finished_folder)
  if [ -n "$done_folder" ]; then
    srcpath=$(library_path_of "$done_folder")
    if [ -z "$srcpath" ] && ! library_roots_online; then
      if [ "$deferred" != "$done_folder" ]; then
        log "DEFER $done_folder: library roots unreachable - record+sync retries every pass; encoding continues"
        deferred="$done_folder"
      fi
    else
      [ -n "$deferred" ] && log "RESUME $deferred: library roots reachable again"
      deferred=""
      log "JUDGE $done_folder"
      vjson=$("$SMELTR" verdict "$done_folder"); vrc=$?
      log "  $vjson"
      # Anything but `good` is this title's ERROR state, never a halt: the
      # marker goes on, the output stays beside the source for a human, and
      # the loop carries on to step 2 in this same pass.
      judged=ok
      case $vrc in
        0) ;;
        2) error_out "$done_folder" "needs a human: $(echo "$vjson" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("verdict"),"-",d.get("note",""))')"; judged=error ;;
        3) error_out "$done_folder" "returned a ladder verdict after finishing - unexpected"; judged=error ;;
        4) error_out "$done_folder" "could not be evaluated (verdict exit 4)"; judged=error ;;
      esac

      # Roots reachable and still no (or no unique) match: that is the real
      # "needs a human" -- deleting depends on exactly-one match. Still one
      # title's problem, not the pipeline's.
      if [ "$judged" = ok ] && [ -z "$srcpath" ]; then
        error_out "$done_folder" "cannot locate the library original (roots ARE reachable - zero or multiple matches)"
        judged=error
      fi
      if [ "$judged" = ok ]; then
        letter=$(echo "$done_folder" | cut -c1 | tr '[:lower:]' '[:upper:]')
        nas=$(echo "$srcpath" | cut -d/ -f3)

        if $DRY; then
          log "(dry run) would record + sync $done_folder -> $nas/$letter"
        else
          sync_async "$done_folder" "$srcpath" "$nas/$letter"
        fi
      fi
    fi
  fi

  # ---- 2. Keep HandBrake fed. Reached in the SAME pass as the dispatch above,
  #         so the next encode starts alongside the push, not after it.
  if ! hb_running; then
    title=$(next_title); nrc=$?
    if [ $nrc -eq 2 ]; then
      log "library not fully mounted - waiting rather than assuming the job is done"
      sleep 300; continue
    fi
    if [ $nrc -eq 3 ]; then
      # Log the REASON, not a guess: 3 now covers paused, all-skipped, and
      # a pull still landing, and a wrong hard-coded message sent the
      # operator debugging overrides that were fine.
      log "waiting: $(next_reason)"
      sleep 300; continue
    fi
    if [ $nrc -ne 0 ] || [ -z "$title" ]; then
      # Never exit while a sync is still running (its replenish stages the
      # next titles) OR while a finished folder sits deferred/just-dispatched:
      # exiting would strand an encode that never reached the ledger.
      if syncs_in_flight || [ -n "$done_folder" ]; then
        log "nothing startable yet - waiting on an in-flight or deferred sync"
        sleep 60; continue
      fi
      log "STOP CONDITION: nothing staged above ${STOP_MBPS} Mb/s. Encoding paused; staging continues."
      exit 0
    fi

    # The CRF ladder. .watch-encode.sh deletes the partial when it auto-kills
    # an out-of-band projection (30-80% of source, both directions since
    # 2026-08-31), so there is no finished folder left to carry the retry --
    # the old ladder branch hung off finished_folder() and could never fire,
    # and the title simply restarted at the same CRF that had just blown up.
    # The watcher picks the next rung: UP 16-18-20-22 when the projection is
    # too big, DOWN 16-14-12-10 when it is too small. "none-*" means the
    # ladder is exhausted (22 still over, 10 still under, or the projection
    # flipped sides on an already-laddered rung).
    # The START rung. 16 unless a human picked one on the dashboard for this
    # title -- `smeltr crf` reads queue_overrides.json and always answers with
    # an integer, so a missing override, an unreadable file or a broken
    # checkout all come back as 16 rather than idling the CPU. The numeric
    # guard is belt-and-braces: an empty or non-numeric answer would reach
    # HandBrake as -q and kill the encode at startup.
    crf=$("$SMELTR" crf "$title" 2>/dev/null)
    case "$crf" in ''|*[!0-9]*) crf=16 ;; esac
    [ "$crf" = 16 ] || log "PLANNED $title -> CRF $crf (chosen on the dashboard)"
    kl="$X9/.watch-$(slug_of "$title").log"
    if grep -q '^KILLED|' "$kl" 2>/dev/null; then
      crf=$(grep '^KILLED|' "$kl" | tail -1 | sed 's/.*next: CRF //')
      case "$crf" in
        none*)
          # Ladder exhausted. NOT a halt and NOT a skip (operator's rule,
          # 2026-08-31): the title goes to an ERROR state -- marker on the
          # X9, red row in the queue -- and the loop moves on to the next
          # title. next_title.py passes over marked titles. The source and
          # the library original are untouched; the watcher already deleted
          # the partial. A human clears the state by deleting the marker.
          printf '%s: CRF ladder exhausted (%s) at %s\n' \
            "$title" "$crf" "$(date '+%Y-%m-%d %H:%M:%S')" > "$X9/.error-$title"
          log "ERROR $title: CRF ladder exhausted ($crf) - marked for review, moving on"
          : > "$kl"
          continue ;;
      esac
      log "LADDER $title -> CRF $crf"
      : > "$kl"
    fi

    start_encode "$title" "$crf"
  fi

  sleep 30
done
