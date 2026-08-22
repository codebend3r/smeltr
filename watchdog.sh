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
#   */5 * * * *  or a launchd StartInterval. Safe to run concurrently.
#   touch "$X9/.watchdog-off"  disables it without unloading anything.
set -uo pipefail


X9="/Volumes/Crucial X9/4K Movies"
SMELTR="$HOME/Developer/git/smeltr/smeltr"
LOG="$X9/.autopilot.log"
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

# --supervise: loop here instead of relying on launchd. Launch it from a shell
# that can already read the staging drive and it works with no TCC grant at all;
# it just does not survive a reboot, which the LaunchAgent does.
if [ "${1:-}" = "--supervise" ]; then
  wlog "supervise mode up (pid $$)"
  while true; do "$0"; sleep 300; done
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

# Not halted, just absent. Is there anything to do? `smeltr next` is the same
# decision the driver itself makes, so the watchdog cannot disagree with it.
"$SMELTR" next 70 >/dev/null 2>&1; nrc=$?
case $nrc in
  0) : ;;                                            # work waiting -- restart
  2) wlog "library not fully mounted; not restarting"; exit 0 ;;
  *) exit 0 ;;                                       # stop condition / all skipped
esac

# A finished folder with no driver means a sync was interrupted. Restarting is
# how it gets finished -- but core.record() has no duplicate guard, so say so
# loudly enough that a double-counted row can be spotted in the log later.
if find "$X9" -mindepth 2 -maxdepth 2 -name '*2160p HEVC*.mkv' ! -name '._*' \
     -print -quit 2>/dev/null | grep -q .; then
  wlog "NOTE: a finished output is on the staging drive; restarting driver will judge it"
fi

wlog "driver absent with work queued — restarting"
notify "Autopilot was down. Restarting it."
cd "$X9" || exit 0
nohup ./.autopilot.sh >> "$LOG" 2>&1 &
sleep 3
if pgrep -f 'autopilot\.sh' >/dev/null 2>&1; then
  wlog "restarted OK (pid $(pgrep -f 'autopilot\.sh' | head -1))"
else
  wlog "RESTART FAILED — see $LOG"
  notify "Autopilot restart FAILED."
fi
