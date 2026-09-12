#!/bin/bash
# Relaunch the autopilot when it is down for no good reason.
#
# The driver halts rather than guessing, and that is correct -- a `suspect` or
# `downscale` verdict is the last thing standing between a bad encode and a
# deleted original. But NOTHING relaunched it afterwards, so a halt caused by a
# *bug* parked the whole pipeline silently. On 2026-08-22 a log-matching bug
# halted it at 16:24 and it sat idle for hours; the dashboard correctly showed
# an empty pipeline, which reads as "quiet", not "stopped".
#
# So: restart a driver that is merely absent. NEVER restart through an
# unreviewed HALTED: line -- notify a human instead, once.
#
# It also sweeps the staging drive before restarting. An encode killed
# mid-write leaves an unfinalised .mkv that the driver matches as FINISHED and
# halts on, so restarting into one just trades a dead pipeline for a halted
# one. See the triage block for what is and is not deleted.
#
#   */5 * * * *  or a launchd StartInterval. Safe to run concurrently.
#   touch "$X9/.watchdog-off"  disables it without unloading anything.
set -uo pipefail

# The driver this script relaunches must find HandBrakeCLI and ffprobe, which
# are Homebrew's. launchd starts us with /usr/bin:/bin:/usr/sbin:/sbin, and a
# driver born from that PATH fails every start with "HandBrakeCLI: No such
# file or directory" while the dashboard shows a title as next up forever
# (2026-09-11). The plist sets PATH too; this line holds whoever launches us.
case ":$PATH:" in *:/opt/homebrew/bin:*) ;; *) PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" ;; esac
export PATH

X9="${SMELTR_X9:-/Volumes/Crucial X9/4K Movies}"
SMELTR="${SMELTR_BIN:-$HOME/Developer/git/smeltr/smeltr}"
LOG="$X9/.autopilot.log"
# The driver's own mkdir lock, and this watchdog's claim on clearing it when it
# goes stale. See the restart block at the bottom for why the claim exists.
LOCK="$X9/.autopilot.lock"
CLAIM="$X9/.watchdog-restart.claim"
# Logs live in ~/Library/Logs, NOT on the staging drive. A LaunchAgent may not
# be able to read /Volumes at all (Full Disk Access), and a watchdog whose only
# log is on the volume it cannot see fails completely silently -- which is the
# exact failure mode it exists to prevent.
WLOG="$HOME/Library/Logs/smeltr/watchdog.log"
FLAG="$HOME/Library/Logs/smeltr/.halt-notified"
DRIVEFLAG="$HOME/Library/Logs/smeltr/.drive-unseen"
mkdir -p "$HOME/Library/Logs/smeltr" 2>/dev/null

wlog() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$WLOG"; }
notify() {
  osascript -e "display notification \"$(printf '%s' "$1" | tr '"' "'" | cut -c1-200)\" \
                with title \"Smeltr\" sound name \"Basso\"" >/dev/null 2>&1 || true
}

# --- staging-output triage (the tests extract this block) -----------------
# Is HandBrakeCLI writing this exact file right now? Matched on argv with a
# literal `case` glob, never a pgrep pattern -- every folder name carries a
# `(YYYY)` that pgrep would read as a regex group and match nothing.
hb_holds() {
  local pid args
  for pid in $(pgrep -x HandBrakeCLI 2>/dev/null); do
    args=$(ps -p "$pid" -o args= 2>/dev/null)
    case "$args" in *"$1"*) return 0 ;; esac
  done
  return 1
}

# Container duration in seconds, or NOTHING. An MKV killed mid-write was never
# finalised and carries no duration header at all -- "nothing" is the signal.
probe_duration() {
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null \
    | head -1 | grep -Ex '[0-9]+(\.[0-9]+)?'
}

# Seconds since last write, or nothing if it cannot be read.
mtime_age() {
  local m; m=$(stat -f %m "$1" 2>/dev/null)
  [ -n "$m" ] || return 1
  echo $(( $(date +%s) - m ))
}

# A `*2160p HEVC*.mkv` on the staging drive is one of three very different
# things, and restarting the driver blindly gets one of them catastrophically
# wrong:
#
#   live      HandBrake is still writing it. The driver's encoding_this()
#             excludes it, so a restart is harmless -- but it is not
#             "finished", and calling it that in the log is how this was
#             misread for a whole afternoon.
#   finished  a real encode whose sync was interrupted. Restarting is exactly
#             how it gets recorded and synced. Must be left strictly alone.
#   corpse    an encode killed mid-write -- reboot, power loss, OOM. The
#             driver STILL matches it as finished, hands it to verdict.py and
#             HALTS, parking the whole pipeline on a file that can only ever
#             be garbage. Confirmed 2026-08-22: a reboot left a 3.0 GB Kubo
#             partial and nothing moved again until a human looked.
#
# A corpse shows up in one of two ways, and BOTH must be caught. The Kubo one
# had no container duration at all -- killed before the header was finalised.
# But a muxer that writes its duration up front leaves a truncated file that
# probes "fine" and merely reports a duration far short of the source, so
# testing only for a missing duration would have walked straight past it.
# So: compare against the source, which is the same check
# `.sync-to-library.sh` already trusts to clear a deletion.
#
# Only a corpse is deleted, and only on positive proof of all of: nothing
# holds it open, it has not been written for 120 s, ffprobe reads the SOURCE
# beside it in the same breath, and the output is missing or short against it.
# Probing the source is the load-bearing half -- it is what stops a transient
# I/O error from deleting a good finished encode, because a drive too sick to
# probe the output cannot probe the source either. The 2% tolerance is enormous
# headroom: a real encode matches its source to milliseconds (Shrek, 7 ms in
# 5431 s), while a killed one is short by whatever percent it had left to run.
# A corpse costs one re-encode to rebuild; the source is untouched and no
# library original is ever in scope here.
#
# Echoes one verdict word per output. wlog writes to the log file, so none of
# this narration can reach stdout and poison the caller's $(...) capture.
triage_outputs() {
  local out dir src age d_out d_src
  while IFS= read -r out; do
    [ -n "$out" ] || continue
    dir=$(dirname "$out")
    if hb_holds "$out"; then
      wlog "  live encode writing $(basename "$dir") - left alone"
      echo live; continue
    fi
    age=$(mtime_age "$out")
    if [ -z "${age:-}" ] || [ "$age" -lt 120 ]; then
      wlog "  $(basename "$dir") written ${age:-?}s ago - too fresh to judge, left alone"
      echo fresh; continue
    fi
    src=$(find "$dir" -maxdepth 1 -type f -name '*.mkv' \
            ! -name '*2160p HEVC*' ! -name '._*' -print -quit 2>/dev/null)
    d_src=""
    [ -n "$src" ] && d_src=$(probe_duration "$src")
    if [ -z "${d_src:-}" ]; then
      wlog "  cannot probe the SOURCE in $(basename "$dir") - refusing to judge its output"
      echo unknown; continue
    fi
    d_out=$(probe_duration "$out")
    if [ -n "${d_out:-}" ] && \
       awk -v o="$d_out" -v s="$d_src" 'BEGIN{exit !(o >= s * 0.98)}'; then
      wlog "  NOTE: finished output in $(basename "$dir") (${d_out}s of ${d_src}s) - restart will judge and RECORD it"
      echo finished; continue
    fi
    wlog "  CORPSE: $(basename "$out") runs ${d_out:-nothing readable} against a ${d_src}s source. Deleting."
    wlog "    the source beside it probes clean, so the file is bad, not the drive."
    rm -f "$out" "$dir/._$(basename "$out")"
    echo corpse
  done
}
# --- end staging-output triage -------------------------------------------

# --supervise: loop here instead of relying on launchd. Launch it from a shell
# that can already read the staging drive and it works with no TCC grant at all;
# it just does not survive a reboot, which the LaunchAgent does.
if [ "${1:-}" = "--supervise" ]; then
  wlog "supervise mode up (pid $$)"
  # 60s, not 300s: this interval IS the worst-case downtime after a driver
  # death, and downtime is the thing this exists to prevent. A healthy tick is
  # only pgrep + ls + wc -- it exits at the "driver is up" check long before the
  # ffprobe triage, so the cost of ticking often is nil while an encode runs.
  while true; do "$0"; sleep 60; done
fi

[ -f "$X9/.watchdog-off" ] && exit 0

# An unmounted staging drive is not a stopped pipeline -- but a drive that is
# mounted and merely invisible to US is a broken watchdog, and the two look
# identical from here. Say so once, so the difference can be checked by hand.
if [ ! -d "$X9" ]; then
  if [ ! -f "$DRIVEFLAG" ]; then
    wlog "staging drive not visible at $X9 - standing down."
    wlog "  if the drive IS mounted, this agent lacks Full Disk Access:"
    wlog "  System Settings > Privacy & Security > Full Disk Access > add /bin/bash"
    : > "$DRIVEFLAG"
  fi
  exit 0
fi
rm -f "$DRIVEFLAG"

# The mount POINT being visible is not the same as its CONTENTS being readable.
# A LaunchAgent without Full Disk Access stats /Volumes/... fine, `test -r` on a
# file inside even returns true, and every read then comes back EMPTY. Blind,
# `smeltr next` reports "stop condition" -- the job looks FINISHED. Never act on
# that. Prove readability before trusting a single decision below.
entries=$(ls -1 "$X9" 2>/dev/null | wc -l | tr -d ' ')
loglines=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ')
if [ "${entries:-0}" -lt 1 ] || [ -z "${loglines:-}" ]; then
  if [ ! -f "$DRIVEFLAG" ]; then
    wlog "BLIND: $X9 is mounted but unreadable from here (saw '${entries:-0}' entries)."
    wlog "  Refusing to act -- a blind 'smeltr next' reports STOP CONDITION,"
    wlog "  which is indistinguishable from the job being finished."
    wlog "  Fix: System Settings > Privacy & Security > Full Disk Access > add /bin/bash,"
    wlog "  or run this script's supervise mode from a terminal that already has access:"
    wlog "    nohup $0 --supervise >/dev/null 2>&1 &"
    notify "Watchdog is blind to the staging drive - see watchdog.log"
    : > "$DRIVEFLAG"
  fi
  exit 0
fi

# Healthy. Clear the notify latch so the next halt speaks up again.
if pgrep -f 'autopilot\.sh' >/dev/null 2>&1; then
  rm -f "$FLAG"
  exit 0
fi

# Down. Everything logged since the most recent launch banner tells us why.
since_up=$(awk '/=== autopilot up/{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}' \
           "$LOG" 2>/dev/null)

if printf '%s' "$since_up" | grep -q 'HALTED:'; then
  reason=$(printf '%s' "$since_up" | grep 'HALTED:' | tail -1 | sed 's/.*HALTED: //')
  if [ ! -f "$FLAG" ]; then
    wlog "halted, NOT restarting: $reason"
    notify "Pipeline halted and needs you: $reason"
    : > "$FLAG"
  fi
  exit 0
fi

# Sort out what is already sitting on the staging drive BEFORE asking what to
# do next, so the pick is made against a clean drive and the driver is never
# handed a file that can only halt it.
# Movie folders live in $X9/queue since 2026-09-07; a folder still at the
# root is the old layout and is swept too. complete/ holds FINISHED encodes
# and is never triaged: nothing there is a corpse, whatever its duration.
verdicts=$( { find "$X9/queue" -mindepth 2 -maxdepth 2 -type f -name '*2160p HEVC*.mkv' \
                ! -name '._*' 2>/dev/null
              find "$X9" -mindepth 1 -maxdepth 1 -type d ! -name '.*' ! -name queue ! -name complete 2>/dev/null \
                | while IFS= read -r d; do
                    find "$d" -mindepth 1 -maxdepth 1 -type f -name '*2160p HEVC*.mkv' ! -name '._*' 2>/dev/null
                  done
            } | triage_outputs)
if printf '%s\n' "$verdicts" | grep -q '^unknown$'; then
  wlog "NOT restarting: an output could not be judged - the drive may be sick"
  notify "Autopilot is down and an encode cannot be judged - needs you."
  exit 0
fi

# Not halted, just absent. Is there anything to do? `smeltr next` is the same
# decision the driver itself makes, so the watchdog cannot disagree with it.
"$SMELTR" next 70 >/dev/null 2>&1; nrc=$?
case $nrc in
  0) : ;;                                            # work waiting -- restart
  2) wlog "library not fully mounted; not restarting"; exit 0 ;;
  *) exit 0 ;;                                       # stop condition / all skipped
esac

wlog "driver absent with work queued — restarting"

# Serialise the clear-and-launch below. Two watchdogs can reach this together
# (the LaunchAgent and a --supervise loop both tick on their own schedule), and
# two that each cleared the lock would each start a driver. Two live drivers
# double-record a finished encode -- `core.record()` appends with no duplicate
# guard -- so the ledger would carry a phantom reclaim and the queue would lose
# the title. Whoever wins this mkdir does the restart; the loser stands down.
if ! mkdir "$CLAIM" 2>/dev/null; then
  wlog "another watchdog is already restarting; standing down"
  exit 0
fi
trap 'rmdir "$CLAIM" 2>/dev/null' EXIT

# Re-check UNDER the claim. The triage and `smeltr next` above take seconds, and
# a driver may have come up in that window -- restarting into a live one is the
# double-record this whole block exists to prevent.
if pgrep -f 'autopilot\.sh' >/dev/null 2>&1; then
  wlog "a driver came up while deciding; nothing to do"
  exit 0
fi

# A lock with no process behind it is STALE, and it refuses every restart
# forever. `kill -9` is the documented way to stop the driver (bash defers
# SIGTERM while waiting on a child), and -9 skips the driver's
# `trap 'rmdir $LOCK' EXIT INT TERM`, so the directory outlives it. On
# 2026-08-25 that stranded the pipeline for six hours. We only get here with
# the claim held and pgrep just proven empty, so nothing else can hold it.
if [ -d "$LOCK" ]; then
  wlog "clearing STALE lock $LOCK (no autopilot process holds it)"
  rmdir "$LOCK" 2>/dev/null || rm -rf "$LOCK" 2>/dev/null
fi

notify "Autopilot was down. Restarting it."
cd "$X9" || exit 0
nohup ./.autopilot.sh >> "$LOG" 2>&1 &
sleep 3
if pgrep -f 'autopilot\.sh' >/dev/null 2>&1; then
  wlog "restarted OK (pid $(pgrep -f 'autopilot\.sh' | head -1))"
else
  # Say WHY. "RESTART FAILED" alone sent me looking at the wrong thing.
  wlog "RESTART FAILED — last driver output: $(tail -1 "$LOG" 2>/dev/null)"
  notify "Autopilot restart FAILED."
fi
