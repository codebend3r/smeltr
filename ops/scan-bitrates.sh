#!/bin/bash
# Rebuild the library bitrate index on a schedule, safely.
#
# The index ($X9/.bitrates-4k-combined.json) is the ONLY source of the queue:
# core.queue() iterates load_index() and nothing else. It is rebuilt by
# .scan-bitrates.sh on the X9 -- which until now ran on exactly one trigger,
# .replenish-queue.sh finding the file MISSING. It never is, so a movie added
# to the library after the last scan was invisible to the queue for as long as
# nobody rebuilt it by hand. This runs it nightly instead.
#
# It does NOT reimplement the scan. It runs the live X9 script, which stays the
# one place the roots and the ffprobe live. What it adds is the two guards that
# a scheduled, unattended rebuild needs and a hand-run does not:
#
# 1. PROVE THE ROOTS ARE READABLE FIRST. A LaunchAgent (or cron) without Full
#    Disk Access stats /Volumes fine and reads every directory as EMPTY. The
#    scan's `find` would then match zero files, and the script would happily
#    write {"files": []} over a good index -- emptying the queue, which
#    next_title.py reports as exit 1, THE STOP CONDITION. A blind nightly job
#    would stop the pipeline and look like a finished one. Refuse instead.
# 2. INSTALL THE RESULT ATOMICALLY. .scan-bitrates.sh writes the index in
#    place with a plain open(...,'w'). That is a ~200 KB non-atomic write, and
#    a reader landing inside it gets a ValueError -> load_index() returns []
#    -> same empty queue, same stop condition, cached for up to 90 s. So the
#    scan is pointed at a temp file on the X9 and `mv`d in: same filesystem,
#    so rename(2), so a reader sees the old index or the new one, never half.
#    The redirect is a sed of the OUT= line asserted to hit EXACTLY one line;
#    the X9 script itself is never edited (it is a live staging script, and
#    editing one is the thing the pause procedure exists to prevent).
#
# Plus a shrink guard: a scan that returns far fewer files than the index it
# would replace is refused. A whole root can go unreachable mid-scan without
# `find` saying a word about it.
#
#   ops/scan-bitrates.sh [--force] [--dry-run]
#     --force    install even if the row count dropped >10% (real deletions)
#     --dry-run  scan and validate, never install
#
# Nightly at 03:00 via ops/com.smeltr.scan.plist -- installed PER MACHINE, the
# way the watchdog's is (~/Library/LaunchAgents is not versioned):
#   cp ops/com.smeltr.scan.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.smeltr.scan.plist
# It carries the watchdog's Full Disk Access caveat: without FDA on /bin/bash
# the agent reads every root as empty, and guard 1 makes it stand down loudly
# in ~/Library/Logs/smeltr/scan.log rather than destroy the index.
set -uo pipefail

# launchd hands a job the bare system PATH (/usr/bin:/bin:/usr/sbin:/sbin).
# ffprobe is Homebrew's, and the X9 script swallows its absence: every
# `ffprobe ... 2>/dev/null` comes back empty, every bitrate is written as 0,
# and the row count is right. On 2026-09-07 03:00 that installed 1415 rows
# of zeros over a good index, the queue read "nothing above 70 Mb/s", and the
# driver idled for hours. Put Homebrew on PATH here, and prove ffprobe
# resolves before the scan runs (guard 3 below catches the same failure by
# its output, in case some other reason zeroes it).
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

X9="${SMELTR_X9:-/Volumes/Crucial X9/4K Movies}"
REPO="${SMELTR_DIR:-$HOME/Developer/git/smeltr}"
PY="${SMELTR_PYTHON:-python3}"
SCAN="$X9/.scan-bitrates.sh"
INDEX="$X9/.bitrates-4k-combined.json"
# Hidden, and on the X9 so the install is a rename and not a copy. Nothing
# globs .bitrates* -- every reader names the index exactly.
STAGE="$X9/.bitrates-scan.inflight.json"
# Same reasoning as watchdog.sh: the log lives in ~/Library/Logs, never on the
# volume this script may turn out to be unable to read.
SLOG="$HOME/Library/Logs/smeltr/scan.log"
mkdir -p "$HOME/Library/Logs/smeltr" 2>/dev/null

FORCE=false
DRY=false
while [ $# -gt 0 ]; do
  case "$1" in
    --force)   FORCE=true; shift ;;
    --dry-run) DRY=true; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

slog() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$SLOG"; }
die()  { slog "ABORT: $*"; exit 1; }

rows_in() {  # row count of a bitrate index, or empty when it will not parse
  "$PY" - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    print(len(json.load(open(sys.argv[1], encoding='utf-8'))["files"]))
except Exception:
    pass
PY
}

nonzero_in() {  # rows of a bitrate index with a bitrate > 0, or empty when it will not parse
  "$PY" - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    rows = json.load(open(sys.argv[1], encoding='utf-8'))["files"]
    print(sum(1 for r in rows if (r.get("overall_bitrate") or 0) > 0))
except Exception:
    pass
PY
}

trap 'rm -f "$STAGE"' EXIT

slog "--- scan start ---"

# ---------------------------------------------------------------- preflight
[ -f "$SCAN" ] || die "$SCAN is missing (X9 not mounted?)"
command -v ffprobe >/dev/null 2>&1 || die "ffprobe is not on PATH ($PATH).
  The X9 scan silences ffprobe errors and writes bitrate 0 for every file,
  which empties the queue while the row count looks fine. Refusing to scan."
entries=$(ls -1 "$X9" 2>/dev/null | wc -l | tr -d ' ')
[ "${entries:-0}" -ge 1 ] || die "$X9 is mounted but unreadable from here (0 entries).
  A blind scan writes an EMPTY index, which empties the queue and reads as the
  stop condition. Fix: System Settings > Privacy & Security > Full Disk Access
  > add /bin/bash, or run this from a terminal that can already see the drive."

# The roots come from core.LIBRARY_ROOTS -- the list is already duplicated in
# four places by hand and this is not going to be the fifth.
roots=$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from pipeline import core; print("\n".join(core.LIBRARY_ROOTS))' "$REPO") \
  || die "could not read core.LIBRARY_ROOTS"
[ -n "$roots" ] || die "core.LIBRARY_ROOTS is empty"

while IFS= read -r root; do
  [ -n "$root" ] || continue
  [ -d "$root" ] || die "library root not mounted: $root"
  # Readability, not existence. This is the Full Disk Access trap: the mount
  # point stats fine and the contents come back empty. Depth 3 is letter/title/file.
  n=$(find "$root" -maxdepth 3 -type f \( -name '*.mkv' -o -name '*.mp4' \) 2>/dev/null | head -1 | wc -l | tr -d ' ')
  [ "${n:-0}" -ge 1 ] || die "library root is mounted but reads as EMPTY: $root
  Refusing to rebuild the index from a root this process cannot see."
done <<< "$roots"

before=$(rows_in "$INDEX")
slog "preflight ok: all roots readable; current index holds ${before:-0} rows"

# ------------------------------------------------------------------- scan
# Redirect the live script's output without touching the file on disk.
tmpscript=$(mktemp -t smeltr-scan) || die "mktemp failed"
hits=$(grep -c '^OUT=' "$SCAN")
[ "$hits" = "1" ] || { rm -f "$tmpscript"; die "expected exactly one OUT= line in $SCAN, found $hits.
  The script changed shape -- refusing to guess where it writes."; }
sed 's|^OUT=.*|OUT="'"$STAGE"'"|' "$SCAN" > "$tmpscript"

slog "scanning..."
if ! out=$(bash "$tmpscript" 2>&1); then
  rm -f "$tmpscript"
  die "scan failed: $out"
fi
rm -f "$tmpscript"
slog "  $out"

# --------------------------------------------------------------- validate
after=$(rows_in "$STAGE")
[ -n "$after" ] || die "scan produced no readable index at $STAGE"
[ "$after" -ge 1 ] || die "scan indexed 0 files -- refusing to install an empty index"

# Guard 3: the VALUES, not just the row count. A scan whose ffprobe never ran
# (missing binary, dead mount mid-scan) indexes every file at bitrate 0 --
# 1415 rows, all of them below the stop threshold -- and the queue empties
# exactly as it would on an empty index. Refuse when nothing has a bitrate,
# and apply the same 90% floor to the count of real bitrates that the row
# count already gets.
after_nz=$(nonzero_in "$STAGE"); after_nz=${after_nz:-0}
before_nz=$(nonzero_in "$INDEX"); before_nz=${before_nz:-0}
[ "$after_nz" -ge 1 ] || die "scan found $after files but NONE has a bitrate (ffprobe produced nothing).
  Installing this would empty the queue. Not installing."
if [ "$before_nz" -gt 0 ]; then
  floor_nz=$(( before_nz * 9 / 10 ))
  if [ "$after_nz" -lt "$floor_nz" ] && [ "$FORCE" != true ]; then
    die "scan measured $after_nz bitrates, index holds $before_nz (floor $floor_nz).
  ffprobe failed on a large share of the library. Not installing. Re-run with
  --force if the drop is genuine."
  fi
fi

if [ -n "${before:-}" ] && [ "$before" -gt 0 ]; then
  # 90% floor: a root that vanished mid-scan takes a third of the library with
  # it and `find` reports nothing wrong.
  floor=$(( before * 9 / 10 ))
  if [ "$after" -lt "$floor" ] && [ "$FORCE" != true ]; then
    die "scan found $after files, index holds $before (floor $floor).
  That is a big enough drop to be a root going away mid-scan rather than a
  real deletion. Not installing. Re-run with --force if the drop is genuine."
  fi
fi

if [ "$DRY" = true ]; then
  slog "DRY RUN: would install $after rows (was ${before:-0}). Leaving index untouched."
  exit 0
fi

# ---------------------------------------------------------------- install
# rename(2) on one filesystem: a concurrent load_index() sees the old file or
# the new one. Never a torn read, which is what an empty queue is made of.
mv -f "$STAGE" "$INDEX" || die "could not install the new index -- old one is intact"
slog "installed: $after rows, $after_nz with a bitrate (was ${before:-0} rows, ${before_nz:-0} with a bitrate)"
