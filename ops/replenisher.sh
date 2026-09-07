#!/bin/bash
# The INDEPENDENT replenisher (operator's rule, 2026-09-07).
#
#   ops/replenisher.sh              one tick (what the LaunchAgent runs every 60 s)
#   ops/replenisher.sh --supervise  tick forever, in the foreground
#
# "The replenisher is independent of the encoder. Its only job is to ensure
# the queue folder has enough files to encode: at least 10, at most 18. Once
# the folder has fewer than 10 it starts downloading more automatically."
#
# Until now .replenish-queue.sh ran only when the DRIVER called it -- from its
# wait and stop paths, and never while paused -- so a paused or dead driver
# meant an empty queue/ the moment it came back. This loop runs the same X9
# script on its own clock. The script keeps its own single-instance lock and
# its own 10/14/18 thresholds (TARGET/FILL/MAX at the top of it), so a tick
# that lands while a pull is in flight, or while the driver's own call is
# running, exits "another replenish is running" and nothing is doubled.
#
# It does NOT reimplement the pick. The X9 script stays the one place the
# threshold, the skip lists, the free-space gate and the pull live.
#
# Two guards a scheduled, unattended run needs:
# 1. PROVE THE DRIVE IS READABLE. A LaunchAgent without Full Disk Access
#    stats the X9 fine and reads it as EMPTY -- and an empty queue/ is
#    exactly the signal that starts pulling. Stand down loudly instead
#    (same rule as watchdog.sh and scan-bitrates.sh).
# 2. Log to ~/Library/Logs (a volume that is always there) AND fold the
#    script's own lines into .autopilot.log with the driver's stamp format,
#    so the Events tab and the notifier see a pull the same way whether the
#    driver or this loop started it.
#
# Installed PER MACHINE, like the watchdog (~/Library/LaunchAgents is not
# versioned):
#   cp ops/com.smeltr.replenish.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.smeltr.replenish.plist
# Disable a tick without unloading anything: touch "$X9/.replenish-off".
set -uo pipefail

X9="${SMELTR_X9:-/Volumes/Crucial X9/4K Movies}"
SCRIPT="$X9/.replenish-queue.sh"
DRIVER_LOG="$X9/.autopilot.log"
RLOG="${SMELTR_REPLENISH_LOG:-$HOME/Library/Logs/smeltr/replenish.log}"
TICK="${SMELTR_REPLENISH_TICK:-60}"
mkdir -p "$(dirname "$RLOG")" 2>/dev/null

rlog() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$RLOG"; }
# The driver's own log line format, so events.py parses these like its own.
dlog() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$DRIVER_LOG" 2>/dev/null; }

tick() {
  [ -f "$X9/.replenish-off" ] && return 0
  if [ ! -d "$X9" ]; then
    rlog "staging drive not visible at $X9 - standing down"
    return 0
  fi
  # Readability, not existence: the Full Disk Access trap.
  entries=$(ls -1 "$X9" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${entries:-0}" -lt 1 ]; then
    rlog "BLIND: $X9 is mounted but unreadable from here. Grant /bin/bash Full Disk Access. NOT replenishing."
    return 0
  fi
  [ -f "$SCRIPT" ] || { rlog "$SCRIPT is missing - nothing to run"; return 0; }
  # THIS drive's script, by full path: a fixture drive in the tests must not
  # see the live one, and the live one must not see a fixture.
  if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
    return 0   # a pull is landing (this loop's or the driver's); the lock would refuse anyway
  fi
  # STREAMED, line by line, because a run that pulls three titles lasts an
  # hour and a log written at the end is an hour of "nothing happening".
  # The script decides whether anything is needed (below TARGET) and says
  # so on its first lines; only a run that actually PICKS is worth a stamp
  # in the driver's log, so the header goes there on the first PICK and
  # every line after it follows. Everything goes to the replenish log.
  local stamped=0 l
  while IFS= read -r l; do
    rlog "  $l"
    case "$l" in PICK\ *) if [ "$stamped" = 0 ]; then dlog "REPLENISH (independent): topping queue/ up"; stamped=1; fi ;; esac
    [ "$stamped" = 1 ] && dlog "  $l"
  done < <(bash "$SCRIPT" 2>&1)
  return 0
}

if [ "${1:-}" = "--supervise" ]; then
  rlog "--- supervise start (tick ${TICK}s) ---"
  while true; do tick; sleep "$TICK"; done
fi
if [ "${1:-}" = "--tick" ] || [ $# -eq 0 ]; then
  tick
  exit 0
fi
echo "usage: $0 [--supervise|--tick]" >&2; exit 2
