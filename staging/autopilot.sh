#!/bin/bash
# Unattended driver for the 4K HEVC re-encode job.
#
# Runs the full cycle with nobody watching: start encode -> wait -> judge ->
# record -> sync -> purge -> replenish -> next. It is the piece that was always
# missing; every prior "autonomous" run still needed a human to notice a
# COMPLETE and type the next command.
#
# IT NEVER GUESSES, AND IT NEVER STOPS (operator's standing rule, 2026-09-04).
# The only path that deletes a library original is a `good` verdict. A FINISHED
# encode with any other verdict -- suspect / thin / no-saving / downscale /
# decoder-errors / a ladder code -- is still DONE (operator's rule, 2026-09-07:
# "it doesn't matter if the output is thin or not, it should ALWAYS be moved
# to complete/ when it's done"): recorded --kept with the verdict in its note,
# `.done-` marked, and moved to complete/ beside its source, nothing synced and
# nothing deleted. Only what is NOT a finished encode -- no source file, a track
# mismatch, an output that cannot be evaluated, an unresolvable library original
# for a good verdict -- puts THAT TITLE into the ERROR state (`$X9/.error-<title>`
# marker, red row on the dashboard), and the loop moves on to the next title. Flight (2012) projected 9.4% of source and
# was genuinely fine, but that took an SSIM measurement to establish, not a
# size comparison, so the CALL stays with a human; the CPU does not wait for
# it. A halt on The Little Mermaid at 04:05 on 2026-09-04 idled the encoder
# for five hours with nine staged titles waiting; that is the last one.
#
# Usage:  nohup ./.autopilot.sh          >> .autopilot.log 2>&1 &
#         ./.autopilot.sh --dry-run      # decide and print, change nothing
set -uo pipefail

X9="/Volumes/Crucial X9/4K Movies"
# The staging LAYOUT (operator's rule, 2026-09-07). Movie folders live in
# $X9/queue (everything still to be acted on: waiting, arriving, encoding,
# errored) and $X9/complete (finished, source + output kept under the
# no-delete policy). Scripts, logs, markers and the index stay at the root.
# A movie folder still AT the root is the old layout: every lookup falls back
# to it, and migrate_layout() moves it into place on each pass -- never the
# one HandBrake is writing into. Mirrors core.STAGE / core.COMPLETE.
STAGE="$X9/queue"
COMPLETE="$X9/complete"
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
  # The mid-write strike count is per-attempt state, not history: a title
  # that reaches a terminal state must not carry it into a later life.
  rm -f "$X9/.midwrite-$(slug_of "$title")"
  printf '%s: %s at %s\n' "$title" "$*" "$(date '+%Y-%m-%d %H:%M:%S')" > "$X9/.error-$title"
  log "ERROR $title: $* - marked for review, moving on"
  log "  Nothing was deleted. Delete $X9/.error-$title after review."
}
# A FINISHED title that is not being synced -- a non-good verdict under any
# policy, or a good one under the no-delete policy: not an error, just done and
# left beside its source in complete/. The marker keeps finished_folder() and
# next_title.py off it; the dashboard renders it green with the verdict in the
# ledger note.
done_out() {
  local title="$1"; shift
  rm -f "$X9/.midwrite-$(slug_of "$title")"
  printf '%s: %s at %s\n' "$title" "$*" "$(date '+%Y-%m-%d %H:%M:%S')" > "$X9/.done-$title"
  log "DONE $title: $* - moving on"
  move_to_complete "$title"
  # A folder leaving queue/ is a slot to refill NOW (operator's rule,
  # 2026-09-07: "every time a file is finished encoded and gets moved from
  # this folder to complete/, it should immediately start the download of
  # the next movie"). The independent replenisher would get there within its
  # 60 s tick; this makes it the same pass. Its single-instance lock makes
  # the call a no-op while a pull is already landing.
  replenish_async
}

# Single instance. Two drivers would both pick "the next title" and both start it.
LOCK="$X9/.autopilot.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "autopilot already running (lock: $LOCK)"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

slug_of() { echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]' | cut -c1-20; }

# Defaults for the helper block when it is sourced on its own (the suites
# extract slug_of..library_path_of and set only X9). No-ops in the script.
: "${STAGE:=$X9/queue}" "${COMPLETE:=$X9/complete}"

# Where a staged title's folder is: queue/<title>, else the legacy root
# folder, else queue/<title> (where it WOULD be). Same resolver as
# core.folder_dir(), so the driver and the dashboard never disagree.
folder_dir() {
  local t="$1"
  if [ -d "$STAGE/$t" ]; then printf '%s\n' "$STAGE/$t"
  elif [ "$t" != queue ] && [ "$t" != complete ] && [ -d "$X9/$t" ]; then printf '%s\n' "$X9/$t"
  else printf '%s\n' "$STAGE/$t"
  fi
}

# Every movie folder the pipeline may still act on, one per line: queue/ plus
# any legacy folder at the root. complete/ is never listed -- done is done.
staged_dirs() {
  find "$STAGE" -mindepth 1 -maxdepth 1 -type d ! -name ".*" 2>/dev/null
  find "$X9" -mindepth 1 -maxdepth 1 -type d ! -name ".*" ! -name queue ! -name complete 2>/dev/null
}

# A finished folder goes to complete/ with its source and output both inside.
# mv on one volume is a rename. A name already there is left for a human:
# nothing is ever merged or overwritten.
move_to_complete() {
  local t="$1" from
  from=$(folder_dir "$t")
  [ -d "$from" ] || return 0
  mkdir -p "$COMPLETE"
  if [ -e "$COMPLETE/$t" ]; then
    log "  NOT moved: $COMPLETE/$t already exists - $from left where it is"
    return 0
  fi
  if mv "$from" "$COMPLETE/$t"; then
    log "  moved to complete/: $t"
  else
    log "  could not move $from to complete/ - left where it is"
  fi
}

# Bring a root-level movie folder into the layout: complete/ if its .done-
# marker exists, queue/ otherwise. Skips the folder HandBrake is writing into
# and any with a sync in flight; a name already taken in the destination is
# left alone and logged once per pass. Runs every pass and is a no-op once
# the root holds only scripts, logs and the two layout folders.
migrate_layout() {
  local d b dest
  mkdir -p "$STAGE" "$COMPLETE"
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    b=$(basename "$d")
    encoding_this "$b" && continue
    sync_in_flight "$b" && continue
    # A .partial inside is a pull still landing (replenisher or dashboard):
    # .ssh-xfer.sh sizes and renames it by the path it was given, so moving
    # the folder under it strands the transfer as a dead half-file.
    [ -n "$(find "$d" -maxdepth 1 -name '*.partial' ! -name '._*' 2>/dev/null | head -1)" ] && continue
    if [ -e "$X9/.done-$b" ]; then dest="$COMPLETE/$b"; else dest="$STAGE/$b"; fi
    if [ -e "$dest" ]; then
      log "LAYOUT: $b is at the root and $dest already exists - not touching either"
      continue
    fi
    if mv "$d" "$dest"; then log "LAYOUT: moved $b into ${dest#"$X9/"}"
    else log "LAYOUT: could not move $b into ${dest#"$X9/"} - left at the root"; fi
  done < <(find "$X9" -mindepth 1 -maxdepth 1 -type d ! -name ".*" ! -name queue ! -name complete 2>/dev/null)
}


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
    # AppleDouble sidecars are NOT an output. This volume is exFAT, so every
    # write leaves a `._<name>` beside the file -- and when .watch-encode.sh
    # kills a band violation it deletes the partial and leaves that sidecar
    # behind. A bare `*2160p HEVC*.mkv` glob matched the sidecar, so the driver
    # read "finished encode" while verdict.py (which excludes `._*`, as does
    # sync_async below) read "no output" and returned 4 -- and error_out then
    # fired on a title whose KILLED line was asking for a ladder retry. That
    # cost 1h35m of idle CPU on 2026-09-06 with Minions at CQ 65: the retry at
    # Q70 never ran, the marker made the title unpickable, and every other
    # staged folder was already errored, so the loop had nothing left to pick.
    # Same `find ... ! -name '._*'` as every other output lookup in this file.
    [ -n "$(find "$d" -maxdepth 1 -name '*2160p HEVC*.mkv' ! -name '._*' 2>/dev/null | head -1)" ] || continue
    # An ERROR-state title keeps its finished output beside the source for a
    # human to look at; re-judging it every 30 s pass would just re-fail it.
    [ -e "$X9/.error-$b" ] && continue
    [ -e "$X9/.done-$b" ] && continue
    encoding_this "$b" && continue
    sync_in_flight "$b" && continue
    printf '%s\n' "$b"
    return
  done < <(staged_dirs)
}

# How many unexplained mid-write deaths a title gets before a human is owed a
# look. A ladder retry is not one of them (see midwrite_route).
MIDWRITE_STRIKES=3

# Throw away a partial output and every shape it leaves behind: the file, its
# AppleDouble sidecar (this volume is exFAT), and the `.killing` rename
# .watch-encode.sh makes before it kills a band violation -- which is left
# behind if the watcher itself is killed between the rename and its cleanup.
# The SOURCE is never matched: every name here carries "2160p HEVC", which is
# the driver's own output naming and never a remux's.
drop_partial() {
  local dir; dir=$(folder_dir "$1")
  find "$dir" -maxdepth 1 \
       \( -name '*2160p HEVC*.mkv' -o -name '*2160p HEVC*.mkv.killing' \) \
       -exec rm -f {} + 2>/dev/null
}

# WHAT TO DO WITH A VERDICT EXIT 4 THAT SAYS "died mid-write" (2026-09-07,
# operator's call: "this does not qualify as an error").
#
# An output with no completion marker in its HandBrake log was never
# finished. There is nothing for a human to look at and nothing was produced
# -- it is a worthless partial, the same thing the watcher's auto-kill throws
# away. Mapping it to error_out() parked titles the ladder was mid-way
# through retrying, and because pick_next passes over a marked title, a thin
# queue then had nothing to start: Minions idled the encoder 1h35m on
# 2026-09-06, Shazam 2h27m on 2026-09-07.
#
# Answers one word on stdout:
#   wait   an encode is ALIVE, so this partial cannot be proved a corpse --
#          it may be the running encode's own output that ps raced us on.
#          Touch nothing and look again next pass. Deleting here would throw
#          away hours of live work.
#   retry  drop the partial and let the next pass re-pick the title. The
#          KILLED line, if there is one, supplies the next rung as usual.
#   error  it has died this way MIDWRITE_STRIKES times with no watcher line
#          to explain any of them. That is HandBrake crashing, not the ladder
#          working, and the driver must not spin on one title forever.
#
# A KILLED line CLEARS the count: each rung is progress, not a repeat of the
# same failure, and a ladder that legitimately walks five rungs must never
# trip the crash bound.
midwrite_route() {
  local title="$1" slug f n
  slug=$(slug_of "$title")
  f="$X9/.midwrite-$slug"
  if hb_running; then printf 'wait\n'; return; fi
  if grep -q '^KILLED|' "$X9/.watch-$slug.log" 2>/dev/null; then
    rm -f "$f"; printf 'retry\n'; return
  fi
  n=$(cat "$f" 2>/dev/null)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  n=$((n + 1)); printf '%s\n' "$n" > "$f"
  if [ "$n" -ge "$MIDWRITE_STRIKES" ]; then printf 'error\n'; else printf 'retry\n'; fi
}

# The replenisher, DETACHED, its output folded into this log. Detached because
# it now fills the drive to 14 folders in one run (operator's 10-18 rule,
# 2026-09-06) and a synchronous call would park this loop behind hours of
# transfers while the FIRST landed title sat unencoded. Safe to background
# since 2026-08-23: next_title.py passes over a folder holding only a
# .partial (exit 3, a wait), so a landing pull can never be picked early.
# The replenisher's own lock keeps two instances apart.
# ---- OPERATOR POLICY FLAGS, beside the ledger (2026-09-06) ------------------
# `no-delete`     : NOTHING is synced and NOTHING is deleted, locally or on the
#                   NAS, whatever the verdict. A `good` encode is kept beside
#                   its source under a `.done-` marker in complete/, exactly
#                   like a non-good one. The watcher's auto-kill ladder is OFF under this
#                   policy too, so every encode finishes at exactly the
#                   quality it started on ("encode 10 movies using VT CQ 70").
# The BUDGET is not here (2026-09-07, operator's rule: "the budget has always
# and only been for the number of files downloaded and ready to be encoded,
# not the actual encoding"). `download_budget` / `downloads_done` beside the
# ledger are read and written by .replenish-queue.sh, which stops PULLING at
# the budget; this loop keeps encoding whatever is staged and simply runs out.
# The old `encode_budget`/`encode_done` pair counted judged encodes here and
# wrote the pause flag -- the wrong quantity, removed.
SMELTR_HOME="$(dirname "$SMELTR")"
PAUSE_FLAG="$SMELTR_HOME/pause"
no_delete_policy() { [ -e "$SMELTR_HOME/no-delete" ]; }
paused_flag()      { [ -e "$PAUSE_FLAG" ]; }

replenish_running() { pgrep -f "replenish-queue.sh" >/dev/null 2>&1; }
replenish_async() {
  paused_flag && { log "  paused - not replenishing (do nothing at all)"; return 0; }
  replenish_running && { log "  replenisher already running"; return 0; }
  log "REPLENISH (detached): topping the staging drive up"
  ( bash "$X9/.replenish-queue.sh" 2>&1 | while IFS= read -r rl; do log "  $rl"; done ) &
}

# Record + push + purge + replenish, detached, so the CPU is never idle waiting
# on a 45-minute push. Deletion safety is unchanged: .sync-to-library.sh still
# copies, size-verifies, ffprobe-verifies and track-verifies BEFORE it removes
# the library original, and still aborts leaving everything in place.
sync_async() {
  local folder="$1" srcpath="$2" dest="$3" slug
  slug=$(slug_of "$folder")
  (
    out=$(find "$(folder_dir "$folder")" -maxdepth 1 -name '*2160p HEVC*.mkv' ! -name '._*' | head -1)
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

# The container extensions a SOURCE may arrive in. `.scan-bitrates.sh` indexes
# .mkv AND .mp4, and .sync-to-library.sh has always resolved a library
# original as .mkv/.mp4/.m2ts -- but both finds below looked for '*.mkv'
# alone. A .mp4 remux was therefore ranked, staged, and then unencodable
# forever: Skyscraper (2018) held 64 GiB on the X9 for three days while
# next_title.py read it as "still arriving" and quietly picked something else.
# Mirrors core.SOURCE_EXTS; the two must move together. The OUTPUT stays .mkv
# (-f av_mkv below).
SRC_FIND=( \( -name '*.mkv' -o -name '*.mp4' -o -name '*.m2ts' \) )

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
  find "$(printf '%s\n' "$matches" | head -1)" -maxdepth 1 "${SRC_FIND[@]}" \
       ! -name '._*' ! -name '*2160p HEVC*' | head -1
}

start_encode() {
  local title="$1" q="${2:-}" slug src out enc defq
  slug=$(slug_of "$title")
  local dir; dir=$(folder_dir "$title")
  src=$(find "$dir" -maxdepth 1 "${SRC_FIND[@]}" ! -name '._*' ! -name '*2160p HEVC*' | head -1)
  [ -z "$src" ] && { error_out "$title" "no source file"; return 1; }
  # Named after the FOLDER, not the source file. An earlier version derived it
  # from the source basename with a sed strip and was overwritten on the very
  # next line -- a dead store that read like it was doing the naming.
  out="$dir/${title} 2160p HEVC.mkv"

  # Anything left over from a previous attempt at this title: the partial
  # itself, and the `.killing` rename .watch-encode.sh makes before it kills
  # a band violation, which survives if the watcher is killed between the
  # rename and its own cleanup. Neither is an encode; both waste the drive
  # this encode is about to write ~30 GB into.
  drop_partial "$title"

  # Which encoder, at what quality. Read through the SAME core code the
  # dashboard writes through (encoder_overrides.json beside the ledger); any
  # failure -- helper missing, file corrupt, entry invalid -- answers the
  # proven default, x265_10bit at CRF_DEFAULT. A ladder retry passes $2 to override the
  # QUALITY only: the encoder choice always comes from the override file, so
  # a vt title ladders down the CQ scale and an x265 title up the CRF scale.
  # Crash-safe fallback = the global default (VideoToolbox CQ 70 since
  # 2026-09-06, operator's call; was x265 CRF 14). core.DEFAULT_ENCODER /
  # DEFAULT_QUALITY are the source of truth; this literal only answers when
  # the checkout itself is broken.
  read -r enc defq <<<"$("$SMELTR" encoder "$title" 2>/dev/null || echo "vt_h265_10bit 70")"
  [ -z "$enc" ] && enc=vt_h265_10bit
  # A ladder rung is only meaningful on the ENCODER whose violation produced
  # it: CRF 18 handed to VideoToolbox is CQ 18, near the bottom of the
  # reversed scale -- a valid file with full track parity that only the size
  # verdict could catch, one step from an unattended deletion. If the override
  # changed since the kill, drop the rung and start fresh on the new encoder's
  # own default.
  local rung_enc="${3:-}"
  if [ -n "$q" ] && [ -n "$rung_enc" ] && [ "$rung_enc" != "$enc" ]; then
    log "  ladder rung Q$q was produced by $rung_enc; $title is now $enc - starting fresh"
    q=""
  fi
  if [ -z "$q" ]; then
    q="${defq:-75}"
    # The dashboard's per-title CRF picker (`smeltr crf`, queue_overrides.json)
    # is an x265 RUNG and means nothing on VideoToolbox's reversed CQ scale, so
    # it is consulted only for x265. Like `smeltr encoder` it always answers an
    # integer -- a missing override, an unreadable file or a broken checkout all
    # come back as the default rather than idling the CPU -- and the numeric
    # guard is belt-and-braces, because an empty answer would reach HandBrake as
    # -q and kill the encode at startup. The LADDER still outranks it: a retry
    # arrives as $2 and never reaches this branch.
    if [ "$enc" = x265_10bit ]; then
      local planned; planned=$("$SMELTR" crf "$title" 2>/dev/null)
      case "$planned" in
        ''|*[!0-9]*) ;;
        *) [ "$planned" = "$q" ] || log "PLANNED $title -> CRF $planned (chosen on the dashboard)"
           q="$planned" ;;
      esac
    fi
  fi
  # Flags per encoder. x265's --encoder-preset is a speed knob (medium is the
  # calibrated choice); VideoToolbox presets are quality-based and its default
  # is correct -- passing "medium" there would CHANGE the quality, not the pace.
  local encflags=(-e x265_10bit -q "$q" --encoder-preset medium)
  [ "$enc" = "vt_h265_10bit" ] && encflags=(-e vt_h265_10bit -q "$q")

  log "START $title with $enc at Q$q"
  $DRY && { log "(dry run) would encode: $src -> $out"; return 0; }

  nohup HandBrakeCLI -i "$src" -o "$out" \
    -f av_mkv "${encflags[@]}" \
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

  # The band ladder stays ARMED under the no-delete policy (2026-09-07). The
  # auto-kill deletes the PARTIAL OUTPUT on the staging drive -- a worthless
  # half-encode the driver would otherwise match as finished by disk scan --
  # and it never touches a library original, which is the only thing the
  # no-delete policy is about. Wiring the two together removed the sole
  # ceiling on an out-of-band encode: `no_delete_policy && nokill=1` switched
  # the whole band check off, and Shazam (2019) reached 37% projecting 131%
  # OF SOURCE with the watcher silent, no strikes and no FINAL line. Deletion
  # safety is unchanged -- only a `good` verdict still reaches sync_async.
  # SMELTR_NO_AUTOKILL=1 in the environment stays as the manual report-only
  # escape hatch, and is the only thing that can disarm the ladder now.
  SMELTR_NO_AUTOKILL="${SMELTR_NO_AUTOKILL:-0}" nohup "$X9/.watch-encode.sh" "$slug" "$title" "$(basename "$src")" "$(basename "$out")" "$pid" "$q" "$enc" \
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

  # ---- 0. The layout: any movie folder still at the X9 root goes into
  #         queue/ or complete/. A no-op on a migrated drive.
  migrate_layout

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
      # A FINISHED encode is DONE, whatever the verdict (operator's rule,
      # 2026-09-07: "it doesn't matter if the output is thin or not, it should
      # ALWAYS be moved to complete/ when it's done"). The verdict decides ONE
      # thing -- whether a `good` encode syncs and deletes its library
      # original -- and nothing else. thin / suspect / no-saving / downscale /
      # a ladder code is recorded (--kept, the word in the note), marked and
      # moved to complete/ beside its source, and the operator reads the
      # verdict off the History tab. It used to be the ERROR state, which left
      # the folder red in queue/ with a finished 50 GB file a human had to
      # move (The Island, 63.6% of source, `thin`, 2026-09-07). Exit 4 -- the
      # output could not be EVALUATED at all -- is not a finished encode and
      # stays an error. Never a halt: the loop carries on to step 2 in this
      # same pass either way.
      judged=ok; vword=""
      case $vrc in
        0) ;;
        2|3) vword=$(echo "$vjson" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("verdict"),"-",d.get("note",""))'); judged=done ;;
        4)
          # NOT EVERY EXIT 4 IS A HUMAN'S PROBLEM (2026-09-07, operator's
          # call: "this does not qualify as an error"). "died mid-write"
          # means the output has no completion marker in its HandBrake log,
          # so it was never finished: an auto-kill's partial caught in the
          # window before .watch-encode.sh renamed it away, or a HandBrake
          # that crashed. Nothing was produced and there is nothing to
          # review -- the answer is to throw the partial away and re-pick the
          # title, which is what its KILLED line is already asking for.
          # Erroring it instead parked Minions on 2026-09-06 (1h35m of idle
          # encoder) and Shazam on 2026-09-07 (2h27m). midwrite_route() owns
          # the wait/retry/error split and the crash bound; see its comment.
          # Every OTHER exit 4 -- no HandBrake log at all, a zero-byte
          # source, an ambiguous file count -- really does need a human.
          if printf '%s' "$vjson" | grep -q 'died mid-write'; then
            case $(midwrite_route "$done_folder") in
              wait)
                log "  $done_folder has an unfinished output and an encode is running - leaving it for the next pass"
                judged=retry ;;
              retry)
                log "  $done_folder: unfinished output from an encode that never completed - dropping the partial and retrying"
                drop_partial "$done_folder"
                judged=retry ;;
              *)
                drop_partial "$done_folder"
                error_out "$done_folder" "the encode never completed on $MIDWRITE_STRIKES attempts and no watcher line explains it - HandBrake is failing on this source"
                judged=error ;;
            esac
          else
            error_out "$done_folder" "could not be evaluated (verdict exit 4)"; judged=error
          fi ;;
      esac

      # KEPT IN PLACE: a non-good verdict under any policy, or a good verdict
      # under the NO-DELETE POLICY (which outranks it). Sync nothing, delete
      # nothing, keep the output beside the source, mark it so it is never
      # re-judged, move the folder to complete/.
      if [ "$judged" = done ] || { [ "$judged" = ok ] && no_delete_policy; }; then
        if [ "$judged" = done ]; then why="verdict $vword"; else why="verdict good; no-delete policy"; fi
        # RECORD it -- a finished encode is history, and the History tab is
        # where the operator expects to see it -- flagged --kept so its
        # saving is never counted as reclaimed. The dedup key is the library
        # original when it resolves, else the staged source itself (a
        # hand-added title has no library folder).
        keysrc="$srcpath"
        [ -z "$keysrc" ] && keysrc=$(find "$(folder_dir "$done_folder")" -maxdepth 1 "${SRC_FIND[@]}" ! -name '._*' ! -name '*2160p HEVC*' | head -1)
        # The marker goes on ONLY once the row is in the ledger. `record`
        # refuses an output modified <120 s ago, and this block runs within
        # seconds of HandBrake exiting -- so the first attempt usually loses.
        # A marker written anyway said "recorded" over an encode the ledger
        # never saw (Addams Family 2, HTTYD and John Wick 2 on 2026-09-07),
        # and finished_folder() then skipped the folder forever. With no
        # marker the next pass re-finds it, re-judges (read-only, cheap) and
        # records once the file has settled. The budget counts the encode
        # once, on the pass that lands the row.
        rout=$("$SMELTR" record "$done_folder" --source-path "$keysrc" --kept \
                 --note "$why: kept in place - nothing synced, nothing deleted" 2>&1); rrc=$?
        [ -n "$rout" ] && printf '%s\n' "$rout" | while IFS= read -r rl; do log "  $rl"; done
        if [ "$rrc" -eq 0 ]; then
          log "RECORDED $done_folder (kept)"
          done_out "$done_folder" "$why; recorded; nothing synced, nothing deleted"
          judged=kept
        elif printf '%s' "$rout" | grep -q "not smaller than source"; then
          # A PERMANENT refusal: the ledger never takes a row claiming a saving
          # that did not happen, and retrying it would leave the folder in
          # queue/ forever. Done is done -- mark and move, and say there is no
          # ledger row.
          log "  not recordable (output not smaller than source) - marking done without a ledger row"
          done_out "$done_folder" "$why; NOT recorded (output not smaller than source); nothing synced, nothing deleted"
          judged=kept
        else
          log "  record failed for $done_folder - no marker written; will re-judge and retry next pass (nothing deleted)"
          judged=retry
        fi
      fi
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
      # AN IDLE ENCODER MUST GO AND FETCH WORK (operator's standing rule, and
      # the reason this call is here as well as in sync_async). Replenishment
      # used to run ONLY after a successful sync, so a run of ERRORED titles
      # starved the drive: no sync, no replenish, no new staged folders. On
      # 2026-09-06 six of seven staged folders were errored and the loop sat
      # in 300 s waits with 128 library titles unqueued -- it had work to do
      # and no way to reach for it. Skipped and paused are the two states
      # where staging more is still right (the drive fills with encodable
      # work for when the human clears the state); a pull already landing is
      # held off by the replenisher's own lock, so calling it is a no-op
      # there. Synchronous ON PURPOSE: the replenisher may pull into a
      # VISIBLE folder only because the driver cannot be at the next_title
      # step while its own pull is in flight, and backgrounding it here would
      # break exactly that.
      replenish_async
      sleep 60; continue
    fi
    if [ $nrc -ne 0 ] || [ -z "$title" ]; then
      # Never exit while a sync is still running (its replenish stages the
      # next titles) OR while a finished folder sits deferred/just-dispatched:
      # exiting would strand an encode that never reached the ledger.
      if syncs_in_flight || [ -n "$done_folder" ]; then
        log "nothing startable yet - waiting on an in-flight or deferred sync"
        sleep 60; continue
      fi
      # One more reach for work before claiming the job is done: the stop
      # condition means nothing PICKABLE is staged, which is not the same as
      # nothing being LEFT. If the replenisher stages something, the next
      # pass picks it up instead of exiting on a drive that had simply run
      # dry.
      # The stop condition is claimed ONLY when the replenisher has just said
      # there is nothing left to stage (.replenish-empty, written by it, and
      # removed the moment a pull lands). Anything else -- a pull in flight,
      # no verdict from the replenisher yet -- is a wait, never an exit.
      if replenish_running; then
        log "nothing startable - a replenish pull is in flight; waiting for it to land"
        sleep 60; continue
      fi
      if [ -f "$X9/.replenish-empty" ] && [ $(( $(date +%s) - $(stat -f%m "$X9/.replenish-empty" 2>/dev/null || echo 0) )) -lt 600 ]; then
        log "STOP CONDITION: nothing staged above ${STOP_MBPS} Mb/s and the replenisher found nothing left to stage. Encoding paused; staging continues."
        exit 0
      fi
      log "nothing startable - asking the replenisher for more before stopping"
      replenish_async
      sleep 60; continue
    fi

    # The quality ladder. .watch-encode.sh deletes the partial when it
    # auto-kills an out-of-band projection (30-80% of source, both directions
    # since 2026-08-31), so there is no finished folder left to carry the
    # retry -- the old ladder branch hung off finished_folder() and could
    # never fire, and the title simply restarted at the quality that had just
    # blown up. The watcher owns the ENCODER-AWARE rung mapping (x265 steps
    # CRF UP 14-16-18-20-22 when too big and DOWN 14-12-10 when too small;
    # VideoToolbox steps CQ the opposite way on both arms, 60-55-50 and
    # 60-65-70) and reports only the next NUMBER here -- this script never
    # needs to know either scale. Past the LAST rung there is no KILLED line
    # at all (2026-09-06): the watcher lets the encode finish at the terminal
    # rung, so this block does not fire and the finished folder reaches the
    # judge like any other.
    # An empty q means "no ladder state": start_encode falls back to the
    # title's override, or the default. Both wordings are parsed, because a
    # watcher launched before this deploy still says "next: CRF 18".
    q=""; rung_enc=""
    kl="$X9/.watch-$(slug_of "$title").log"
    if grep -q '^KILLED|' "$kl" 2>/dev/null; then
      kline=$(grep '^KILLED|' "$kl" | tail -1)
      q=$(printf '%s' "$kline" | sed 's/.*next: //; s/^CRF //; s/^Q //')
      # Which encoder the killed run used, from the KILLED line itself --
      # "(vt_h265_10bit Q60)" new wording, "(CRF 16)" from a pre-deploy
      # watcher. start_encode drops the rung if the override has moved to a
      # different encoder since the kill.
      case "$kline" in
        *"(vt_h265_10bit Q"*) rung_enc=vt_h265_10bit ;;
        *)                    rung_enc=x265_10bit ;;
      esac
      case "$q" in
        none*)
          # LEGACY PATH as of 2026-09-06: a current .watch-encode.sh never
          # writes "next: none-*" any more -- past the last rung it leaves the
          # encode RUNNING and logs FINAL| instead, so the title finishes at
          # CRF 22 / CRF 10 (CQ 50 / CQ 70 on VideoToolbox) and is judged on
          # the file it actually produced. Only a watcher launched BEFORE that
          # deploy can still reach this branch, and it has to keep working for
          # as long as one is alive: that watcher already killed the encode and
          # deleted the partial, so there is nothing left to finish.
          # NOT a halt and NOT a skip (operator's rule, 2026-08-31): the title
          # goes to an ERROR state -- marker on the X9, red row in the queue --
          # and the loop moves on to the next title. next_title.py passes over
          # marked titles. The source and the library original are untouched.
          # A human clears the state by deleting the marker.
          # Truncate FIRST: a stale "next: none" re-read on the next pass
          # would re-mark a title a human had just cleared.
          : > "$kl"
          error_out "$title" "quality ladder exhausted ($q)"
          continue ;;
        # A rung must be a bare number. An old driver fed a new-format KILLED
        # line once passed the WHOLE line to -q, which HandBrake read as
        # quality 0.0 -- x265 lossless, writing until the drive filled.
        ''|*[!0-9]*)
          : > "$kl"
          error_out "$title" "unparseable ladder rung '$q'"
          continue ;;
      esac
      log "LADDER $title -> Q$q"
      : > "$kl"
    fi

    start_encode "$title" "$q" "$rung_enc"
  fi

  sleep 30
done
