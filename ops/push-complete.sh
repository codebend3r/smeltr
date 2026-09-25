#!/bin/bash
# The independent PUSHER (operator's rule, 2026-09-10).
#
#   ops/push-complete.sh              one tick (what the LaunchAgent runs every 60 s)
#   ops/push-complete.sh --supervise  tick forever, in the foreground
#
# "When a movie has finished encoding it gets transferred to complete/, the
# original larger file gets deleted immediately, and that movie starts being
# transferred back to the NAS. Never delete any files on the NAS unless I
# tell you. If a transfer is in progress, queue it up. The transfer status
# should reflect in the UI."
#
# The driver is NOT involved: done_out() moves a finished folder into
# complete/ and this loop does the rest on its own clock, the way the
# replenisher keeps queue/ full without the driver's help. One tick:
#
#   1. PURGE   every complete/<title>/ holding a finished, probe-able encode
#              loses its SOURCE file (the 50-90 GB original the encode was
#              made from) -- but ONLY once its library folder resolves to
#              exactly one place and the NAS original there is proven, over
#              ssh, to be the same byte count, and the encode runs the
#              source's length. The X9 copy is the only thing that can free
#              the staging drive (the 100 GiB floor); the NAS copy is the
#              only thing that makes deleting it safe.
#   2. PUSH    the LARGEST pending encode goes to its own library folder over
#              SSH (.ssh-xfer.sh push: lands as <name>.partial, renamed only
#              on a byte-count match), BESIDE the original. Largest first is
#              the operator's order. Exactly one library folder must match
#              (the same rule .sync-to-library.sh applies); zero or two is a
#              PERMANENT failure marked on the title, never a guess.
#   3. VERIFY  over SSH, never over the SMB mount: the byte count again, then
#              the NAS's own ffmpeg reads the copy's header -- a Duration and
#              the same audio/subtitle stream counts as the local file.
#   4. CLEAN   only then is the X9 folder removed. `.pushed-<title>` is written
#              beside the driver's other markers so the dashboard, the report
#              and the replenisher all know the title left the drive.
#
#   ...then back to 2 until nothing is pending. ONE transfer at a time: the
#   lock, plus a hold while any `.ssh-xfer.sh push` from this drive is alive
#   (a hand-started one included). That hold IS the queue -- the rows wait in
#   complete/, largest first, and the dashboard numbers them.
#
# NOTHING ON THE NAS IS EVER REMOVED BY THIS SCRIPT. The only remote commands
# it runs are `stat`, `ffmpeg -i` (header read) and what .ssh-xfer.sh runs
# (`cat >`, `stat`, `mv`); the only remote `rm` anywhere is .ssh-xfer.sh
# discarding its OWN .partial on a byte-count mismatch. A verify failure
# leaves BOTH copies where they are and says so.
#
# Transient (NAS unreachable, a root not mounted, the wire busy) HOLDS the
# tick and retries on the next one; permanent (no/ambiguous library folder,
# a copy that fails verification) writes `.push-failed-<title>` with the
# reason and moves on to the next title, so one dead title never wedges the
# queue behind it. Delete the marker to retry.
#
# Same unattended-run guards as replenisher.sh: prove the drive is READABLE
# (a LaunchAgent without Full Disk Access reads complete/ as empty -- harmless
# here, but say so), log to ~/Library/Logs, and stamp the driver's log in its
# own format so the Events tab and the notifier see every move.
#
# Installed PER MACHINE, like the watchdog:
#   cp ops/com.smeltr.push.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.smeltr.push.plist
# Stop it without unloading anything: touch "$X9/.push-off".
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
X9="${SMELTR_X9:-/Volumes/Crucial X9/4K Movies}"
COMPLETE="$X9/complete"
XFER="${SMELTR_XFER:-$X9/.ssh-xfer.sh}"
SSH="${SMELTR_SSH:-ssh}"
# The SMB mount prefix the library roots live under. Only the mapping to an
# SSH host and a /volume1 path reads it (the tests point it at a sandbox).
VOLUMES="${SMELTR_VOLUMES:-/Volumes}"
DRIVER_LOG="$X9/.autopilot.log"
PLOG="${SMELTR_PUSH_LOG:-$HOME/Library/Logs/smeltr/push.log}"
TICK="${SMELTR_PUSH_TICK:-60}"
LOCK="$X9/.push.lock"
# An encode is not FINISHED until HandBrake has been done with it for a while
# (record.py's own 120 s settle window). Below this age the folder is skipped.
SETTLE_SECONDS="${SMELTR_PUSH_SETTLE:-125}"
mkdir -p "$(dirname "$PLOG")" 2>/dev/null

plog() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$PLOG"; }
# The driver's own log line format, so events.py parses these like its own.
dlog() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$DRIVER_LOG" 2>/dev/null; plog "$*"; }
gib() { awk -v b="$1" 'BEGIN{printf "%.2f", b/1073741824}'; }
q() { python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$1"; }

# The library roots come from pipeline/core.py, not a sixth hand-synced copy.
lib_roots() {
  if [ -n "${SMELTR_LIB_ROOTS:-}" ]; then printf '%s\n' "$SMELTR_LIB_ROOTS"; return; fi
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from pipeline import core; print("\n".join(core.LIBRARY_ROOTS))' "$REPO" 2>/dev/null
}

# Map a library dir under the SMB mount to "host|/volume1/..." -- the same
# table .ssh-xfer.sh keeps. Every byte and every stat goes over ssh.
remote_for() {
  local p="$1"
  case "$p" in
    "$VOLUMES"/Vhagar/*)    echo "crivas@192.168.50.6|/volume1/Vhagar/${p#"$VOLUMES"/Vhagar/}" ;;
    "$VOLUMES"/Vermithor/*) echo "crivas@192.168.50.3|/volume1/Vermithor/${p#"$VOLUMES"/Vermithor/}" ;;
    *) echo "UNMAPPED|$p" ;;
  esac
}

# ps, not `kill -0`: a pid this user may not signal (pid 1) answers EPERM to
# kill and would read as dead.
alive() { [ -n "${1:-}" ] && ps -p "$1" >/dev/null 2>&1; }

take_lock() {
  local pid
  if mkdir "$LOCK" 2>/dev/null; then echo "$$" > "$LOCK/pid"; return 0; fi
  pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  if alive "$pid"; then plog "tick skipped: pusher pid ${pid} holds the lock"; return 1; fi
  # Claim the stale lock by RENAME -- atomic, one winner -- so two pushers
  # (the launchd tick and a --supervise) cannot both clear it and both run.
  mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null || return 1
  rm -rf "$LOCK.stale.$$"
  plog "cleared a stale push lock (pid ${pid:-?} is gone)"
  mkdir "$LOCK" 2>/dev/null || return 1
  echo "$$" > "$LOCK/pid"
}
# Only the owner releases: a lock re-taken by another pusher is not ours.
release_lock() { [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK"; return 0; }

# Every "<root>/<bucket>/<title>" that exists, matched LITERALLY: `find -name`
# reads [ ] * ? in a title as a glob ("Se7en [Director's Cut] (1995)" matched
# nothing). ROOTS is set once per tick.
ROOTS=""
lib_matches() {
  local title="$1" root b
  while IFS= read -r root; do
    [ -n "$root" ] || continue
    for b in "$root"/*/; do
      b="${b%/}"; [ -d "$b" ] || continue
      [ -d "$b/$title" ] && printf '%s\n' "$b/$title"
    done
  done <<< "$ROOTS"
}
dur_of() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null; }
# Remote byte count of one library file; "0" when absent. Non-zero exit = ssh
# itself failed (the NAS is unreachable), which is a HOLD, never an answer.
nas_size() { $SSH -o BatchMode=yes -o ConnectTimeout=15 "$1" "stat -c%s $(q "$2") 2>/dev/null || echo 0"; }

# The one finished encode in a folder, or nothing (two is a state record.py
# refuses; zero is a folder someone moved here by hand).
one_hevc() {
  local n; n=$(find "$1" -maxdepth 1 -type f -name '*2160p HEVC*.mkv' ! -name '._*' 2>/dev/null)
  [ "$(printf '%s\n' "$n" | grep -c .)" -eq 1 ] || return 1
  printf '%s\n' "$n"
}
# A .partial (pull still landing) or .killing (the watcher mid-kill) means
# the folder is not ours to touch yet.
in_flux() { find "$1" -maxdepth 1 \( -name '*.partial' -o -name '*.killing' \) 2>/dev/null | grep -q .; }
settled() {
  local age; age=$(( $(date +%s) - $(stat -f%m "$1" 2>/dev/null || echo 0) ))
  [ "$age" -ge "$SETTLE_SECONDS" ]
}
probe_ok() { [ -n "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null)" ]; }
tracks_of() { # "<audio> <subs>" of a local file
  local s; s=$(ffprobe -v error -show_streams "$1" 2>/dev/null)
  echo "$(printf '%s\n' "$s" | grep -c '^codec_type=audio') $(printf '%s\n' "$s" | grep -c '^codec_type=subtitle')"
}

# A `.pushing-<title>` whose pid is gone is a crashed push, not a live one.
clear_stale_markers() {
  local f pid
  for f in "$X9"/.pushing-*; do
    [ -f "$f" ] || continue
    pid=$(head -1 "$f" 2>/dev/null || true)
    alive "$pid" && continue
    plog "clearing stale $(basename "$f") (pid ${pid:-?} is gone)"
    rm -f "$f"
  done
}

# ---- 1. PURGE -------------------------------------------------------------
purge_sources() {
  local dir title hevc src sz matches n dest map host rdir sd ed rsz
  for dir in "$COMPLETE"/*/; do
    dir="${dir%/}"; [ -d "$dir" ] || continue
    title=$(basename "$dir")
    case "$title" in .*) continue ;; esac
    in_flux "$dir" && continue
    [ -f "$X9/.push-failed-$title" ] && continue
    # A folder whose encode already went out (the .pushed- marker) still
    # owes its source: a title that settled while another push held the
    # wire was pushed before any purge saw it. The encode was verified on
    # the NAS at push time, so only the size check below remains.
    if hevc=$(one_hevc "$dir"); then
      settled "$hevc" || continue
    elif [ -f "$X9/.pushed-$title" ]; then
      hevc=""
    else
      continue
    fi
    while IFS= read -r src; do
      [ -n "$src" ] || continue
      # The X9 source may go ONLY once the NAS is proven to hold the same
      # bytes: exactly one library folder, and the original there at the
      # same size. Anything less keeps the source and says why.
      matches=$(lib_matches "$title"); n=$(printf '%s\n' "$matches" | grep -c . || true)
      if [ "$n" -ne 1 ]; then plog "  $title: source kept - $n library folders match"; break; fi
      dest=$(printf '%s\n' "$matches" | head -1)
      map=$(remote_for "$dest"); host="${map%%|*}"; rdir="${map#*|}"
      [ "$host" = "UNMAPPED" ] && { plog "  $title: source kept - $dest maps to no SSH host"; break; }
      if [ -n "$hevc" ]; then
        if ! probe_ok "$hevc"; then plog "  $title: the encode does not probe - source kept"; break; fi
        # A truncated encode can carry a full-length header (the corpse shape
        # watchdog.sh triages): the encode must run the source's length.
        sd=$(dur_of "$src"); ed=$(dur_of "$hevc")
        if [ -n "$sd" ] && [ -n "$ed" ] && awk -v s="$sd" -v e="$ed" 'BEGIN{exit !(e < s*0.98)}'; then
          fail_out "$title" "the encode runs short of the source (${ed%.*} s vs ${sd%.*} s)" || return 1
          break
        fi
      fi
      sz=$(stat -f%z "$src" 2>/dev/null || echo 0)
      if ! rsz=$(nas_size "$host" "$rdir/$(basename "$src")"); then hold "NAS unreachable: $host"; return 1; fi
      if [ "$rsz" != "$sz" ]; then
        plog "  $title: source kept - the NAS original is $rsz bytes, the X9 copy $sz"
        break
      fi
      rm -f -- "$src" "$dir/._$(basename "$src")"
      dlog "PURGED SOURCE $title: $(basename "$src") ($(gib "$sz") GiB freed on the X9; the NAS holds the same $sz bytes)"
    done < <(find "$dir" -maxdepth 1 -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m2ts' \) ! -name '._*' ! -name '*2160p HEVC*' 2>/dev/null)
    # An already-pushed folder left holding only junk goes with its source.
    if [ -z "$hevc" ]; then
      find "$dir" -maxdepth 1 -type f \( -name '._*' -o -name '.DS_Store' \) -delete 2>/dev/null
      rmdir "$dir" 2>/dev/null && rm -f -- "$COMPLETE/._$title"
    fi
  done
  return 0
}

# ---- 2-4. PUSH, VERIFY, CLEAN ----------------------------------------------
# Largest pending encode: "<bytes>\t<title>", or nothing.
pending_largest() {
  local dir title hevc
  for dir in "$COMPLETE"/*/; do
    dir="${dir%/}"; [ -d "$dir" ] || continue
    title=$(basename "$dir")
    case "$title" in .*) continue ;; esac
    [ -f "$X9/.push-failed-$title" ] && continue
    hevc=$(one_hevc "$dir") || continue
    in_flux "$dir" && continue
    settled "$hevc" || continue
    printf '%s\t%s\n' "$(stat -f%z "$hevc" 2>/dev/null || echo 0)" "$title"
  done | sort -t "$(printf '\t')" -k1,1 -rn | head -1
}

# A transient reason is stamped into the driver's log ONCE per change, not
# once per 60 s tick: the Events tab would otherwise fill with it.
hold() {
  local last; last=$(cat "$X9/.push-hold" 2>/dev/null || true)
  if [ "$last" != "$1" ]; then
    dlog "waiting: push held - $1"
    printf '%s' "$1" > "$X9/.push-hold" 2>/dev/null
  else
    plog "hold: $1"
  fi
}
clear_hold() { rm -f "$X9/.push-hold"; }

# PERMANENT: marker, both copies kept. Returns 1 when the marker could not be
# written (a full X9 -- the very thing the pusher exists to relieve): the
# caller must then HOLD, because an unmarked title would be picked again.
fail_out() { # title, reason
  local title="$1" line; shift
  rm -f "$X9/.pushing-$title"
  line="$title: $* at $(date '+%Y-%m-%d %H:%M:%S')"
  printf '%s\n' "$line" 2>/dev/null > "$X9/.push-failed-$title"
  if [ "$(head -1 "$X9/.push-failed-$title" 2>/dev/null)" != "$line" ]; then
    hold "cannot write .push-failed-$title (drive full?) - $*"
    return 1
  fi
  dlog "PUSH FAILED $title: $* - X9 copy kept, nothing on the NAS removed; delete .push-failed-$title to retry"
}

# 0 = this title is settled (pushed, or failed for good) - go on to the next
# 1 = transient - stop this tick, retry next one
push_one() {
  local title="$1" dir hevc name matches n dest map host rdir lsz rsz hdr ra rs lt f
  dir="$COMPLETE/$title"
  hevc=$(one_hevc "$dir") || return 0
  name=$(basename "$hevc")
  matches=$(lib_matches "$title")
  n=$(printf '%s\n' "$matches" | grep -c . || true)
  if [ "$n" -ne 1 ]; then fail_out "$title" "$n library folders match, need exactly one"; return $?; fi
  dest=$(printf '%s\n' "$matches" | head -1)
  map=$(remote_for "$dest"); host="${map%%|*}"; rdir="${map#*|}"
  [ "$host" = "UNMAPPED" ] && { fail_out "$title" "cannot map $dest to an SSH host"; return $?; }
  lsz=$(stat -f%z "$hevc" 2>/dev/null || echo 0)
  [ "$lsz" -gt 0 ] || { fail_out "$title" "the encode is empty"; return $?; }

  # Already on the NAS at the right size? (A push that finished before a
  # crash, or a hand-run one.) Then only verify and clean.
  if ! rsz=$(nas_size "$host" "$rdir/$name"); then hold "NAS unreachable: $host"; return 1; fi
  if [ "$rsz" = "$lsz" ]; then
    dlog "PUSH $title: already on the NAS at $(gib "$lsz") GiB - verifying"
  elif [ "${rsz:-0}" != "0" ]; then
    # A DIFFERENT file already wears this name on the NAS. .ssh-xfer.sh's
    # final `mv` would replace it, and replacing anything on the NAS is the
    # one thing this script may never do. A human decides.
    fail_out "$title" "a different file of the same name is already on the NAS ($rsz bytes vs $lsz here) - not overwriting"
    return $?
  else
    # Keyed by the FULL title: slug_of() truncates to 20 chars and the two
    # Mockingjay films share one slug.
    printf '%s\n%s\n' "$$" "$title" > "$X9/.pushing-$title"
    dlog "PUSH $title -> $dest ($(gib "$lsz") GiB over ssh, beside the original; nothing on the NAS is removed)"
    if ! bash "$XFER" push "$hevc" "$dest" 2>&1 | while IFS= read -r l; do dlog "  $l"; done; then
      rm -f "$X9/.pushing-$title"
      dlog "PUSH FAILED $title: transfer aborted - X9 copy kept, retrying next tick"
      return 1
    fi
    rm -f "$X9/.pushing-$title"
    if ! rsz=$(nas_size "$host" "$rdir/$name"); then hold "NAS unreachable after the push: $host"; return 1; fi
  fi
  [ "$rsz" = "$lsz" ] || { fail_out "$title" "size mismatch: NAS has $rsz bytes, X9 has $lsz"; return $?; }
  # The NAS's own ffmpeg reads the header (it ships no ffprobe). Status is
  # cat's: ffmpeg with no output exits 1 by design. An ffmpeg that is not on
  # the non-interactive ssh PATH is a HOLD with a name, never "every title
  # failed" -- the launchd-PATH trap that zeroed the bitrate index once.
  if ! hdr=$($SSH -o BatchMode=yes -o ConnectTimeout=15 "$host" "if command -v ffmpeg >/dev/null 2>&1; then ffmpeg -hide_banner -i $(q "$rdir/$name") 2>&1 </dev/null | cat; else echo NOFFMPEG; fi"); then
    hold "NAS unreachable for the header check: $host"; return 1
  fi
  [ "$hdr" = "NOFFMPEG" ] && { hold "ffmpeg is not on the ssh PATH of $host - cannot verify"; return 1; }
  printf '%s\n' "$hdr" | grep -q 'Duration: [0-9]' || { fail_out "$title" "the NAS cannot read the copy's header"; return $?; }
  ra=$(printf '%s\n' "$hdr" | grep -c 'Stream #.*: Audio: '); rs=$(printf '%s\n' "$hdr" | grep -c 'Stream #.*: Subtitle: ')
  lt=$(tracks_of "$hevc")
  [ "$lt" = "$ra $rs" ] || { fail_out "$title" "track parity: X9 ${lt% *}a/${lt#* }s, NAS ${ra}a/${rs}s"; return $?; }
  # Verified. Every delete below is one explicit local path: the encode, its
  # sidecar, and junk (.DS_Store, AppleDouble). A SOURCE still here was kept
  # on purpose by purge_sources (the NAS original differs) and stays; the
  # folder is removed only when that leaves it empty.
  rm -f -- "$hevc" "$dir/._$name"
  while IFS= read -r f; do [ -n "$f" ] && rm -f -- "$f"; done \
    < <(find "$dir" -maxdepth 1 -type f \( -name '._*' -o -name '.DS_Store' \) 2>/dev/null)
  printf '%s: pushed to %s at %s\n' "$title" "$dest" "$(date '+%Y-%m-%d %H:%M:%S')" > "$X9/.pushed-$title"
  if rmdir "$dir" 2>/dev/null; then
    dlog "PUSHED $title -> $dest ($(gib "$lsz") GiB verified on the NAS: ${ra}a/${rs}s; X9 copy removed; NAS original kept)"
  else
    dlog "PUSHED $title -> $dest ($(gib "$lsz") GiB verified on the NAS: ${ra}a/${rs}s; encode removed from the X9; the folder stays because its source was kept - see the purge note above)"
  fi
  return 0
}

push_all() {
  local pick title tried="|"
  while :; do
    if pgrep -f "$XFER push" >/dev/null 2>&1; then hold "a push from this drive is already on the wire"; return 0; fi
    # Purge before EVERY push, not once per tick: a push holds the wire for
    # up to an hour, and a title that settles meanwhile owes its source first.
    purge_sources || return 0
    pick=$(pending_largest); [ -n "$pick" ] || { clear_hold; return 0; }
    title="${pick#*$'\t'}"
    # Progress guarantee: a title that was already tried this tick and is
    # STILL pending (its marker could not be written) ends the tick rather
    # than spinning the lock at 100% CPU.
    case "$tried" in *"|$title|"*) hold "$title is still pending after an attempt this tick"; return 0 ;; esac
    tried="$tried$title|"
    push_one "$title" || return 0
    clear_hold
  done
}

tick() {
  [ -f "$X9/.push-off" ] && return 0
  if [ ! -d "$X9" ]; then plog "staging drive not visible at $X9 - standing down"; return 0; fi
  entries=$(ls -1 "$X9" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${entries:-0}" -lt 1 ]; then
    plog "BLIND: $X9 is mounted but unreadable from here. Grant /bin/bash Full Disk Access. NOT pushing."
    return 0
  fi
  [ -d "$COMPLETE" ] || return 0
  take_lock || return 0
  clear_stale_markers
  ROOTS=$(lib_roots)
  if [ -z "$ROOTS" ]; then
    hold "cannot read the library roots from pipeline/core.py"
  elif ! roots_mounted; then
    : # hold() already named the root
  else
    push_all
  fi
  release_lock
  return 0
}
roots_mounted() {
  local root
  while IFS= read -r root; do
    [ -n "$root" ] || continue
    [ -d "$root" ] || { hold "library root not mounted: $root (run mount-all-drives)"; return 1; }
  done <<< "$ROOTS"
  return 0
}

if [ "${1:-}" = "--supervise" ]; then
  plog "--- supervise start (tick ${TICK}s) ---"
  while true; do tick; sleep "$TICK"; done
fi
if [ "${1:-}" = "--tick" ] || [ $# -eq 0 ]; then
  tick
  exit 0
fi
echo "usage: $0 [--supervise|--tick]" >&2; exit 2
