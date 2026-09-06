#!/bin/bash
# Push staging/*.sh from this repo onto the X9, the copy that actually runs.
#
# WHY THIS EXISTS: `staging/autopilot.sh` sat 11 lines ahead of the live script
# for five days. The fix was committed (11b1c15), `tests/test_staging_in_sync.sh`
# went red, and nobody deployed it -- the driver kept running the pre-fix
# `next_reason()` while CLAUDE.md documented the fixed one as current. A red
# check nobody can act on in one command is a check that gets ignored.
#
# WHY `mv` AND NOT `cp`: CLAUDE.md forbids editing a staging script while an
# instance runs, because bash reads a script by byte offset and an in-place
# rewrite can garble the remaining commands of the RUNNING copy -- including
# its deletion steps. A rename does not touch the running process at all: it
# swaps the directory entry while bash keeps reading the old inode. The live
# driver finishes its cycle on the old code and the next launch picks up the
# new one. Zero downtime, and nothing is ever half-written.
#
# The temp file is created ON the X9 so the rename stays within one filesystem
# -- a cross-device `mv` degrades to copy-then-unlink, which is the unsafe
# thing this script exists to avoid.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
X9="${SMELTR_X9:-/Volumes/Crucial X9/4K Movies}"
DRY=false
YES=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    --yes|-y)  YES=true ;;
    *) echo "usage: $0 [--dry-run] [--yes]" >&2; exit 2 ;;
  esac
done

[ -d "$X9" ] || { echo "staging drive not mounted: $X9" >&2; exit 1; }
[ -w "$X9" ] || { echo "staging drive is not writable: $X9" >&2; exit 1; }

# A REAL deploy while the driver is alive needs an explicit --yes. Swapping by
# `mv` is safe for the running bash (it keeps its old inode), but the pause
# procedure exists for the rest of it: a driver mid-cycle picks the new script
# up on its next launch, which is not always the moment you meant. This is a
# guard against a mistyped script name, not against the operator -- --yes is
# one word.
if ! $DRY && ! $YES && pgrep -f autopilot.sh >/dev/null 2>&1; then
  echo "REFUSING: the driver is running (pid $(pgrep -f autopilot.sh | tr '\n' ' '))." >&2
  echo "Pause it first (see CLAUDE.md), or pass --yes to deploy anyway." >&2
  exit 1
fi

rc=0
# ORDER IS LOAD-BEARING and the glob supplies it: autopilot.sh sorts before
# watch-encode.sh. The old driver parses a rung with `sed 's/.*next: CRF //'`,
# a no-op on the new watcher's `next: Q 55` wording -- the whole KILLED line
# would reach `-q`, which HandBrake reads as quality 0.0, x265 LOSSLESS, until
# the drive fills. The driver must learn the new wording BEFORE the watcher
# starts emitting it. A script added here that must land before autopilot.sh
# needs an explicit list, not this glob.
for src in "$REPO"/staging/*.sh; do
  name=".$(basename "$src")"
  dst="$X9/$name"
  if [ ! -f "$dst" ]; then
    echo "SKIP $name — no live copy at $dst (not deploying a new file blind)"
    continue
  fi
  if diff -q "$src" "$dst" >/dev/null 2>&1; then
    echo "OK   $name already matches"
    continue
  fi
  echo "DIFF $name"
  diff -u "$dst" "$src" | sed 's/^/     /' | head -40
  if $DRY; then echo "     (dry run — not deploying)"; continue; fi

  # Keep one rollback copy, timestamped by the live file's own mtime so
  # repeated runs cannot clobber the last known-good version.
  stamp=$(date -r "$dst" +%Y%m%d-%H%M%S 2>/dev/null || echo unknown)
  if ! cp -p "$dst" "$X9/$name.bak-$stamp"; then
    echo "     FAILED to back up $name; refusing to deploy" >&2; rc=1; continue
  fi
  tmp="$X9/$name.deploy.$$"
  if ! cp "$src" "$tmp"; then
    echo "     FAILED to stage $name" >&2; rm -f "$tmp"; rc=1; continue
  fi
  chmod --reference="$dst" "$tmp" 2>/dev/null || chmod 700 "$tmp"
  # The atomic step. Same filesystem, so this is a rename, not a copy.
  if mv -f "$tmp" "$dst"; then
    echo "     DEPLOYED (rollback: $name.bak-$stamp)"
  else
    echo "     FAILED to swap $name into place" >&2; rm -f "$tmp"; rc=1
  fi
done

echo
if pgrep -f autopilot.sh >/dev/null 2>&1; then
  echo "NOTE the driver is running and keeps the OLD script until it next"
  echo "     starts. That is the point: nothing is interrupted."
fi
exit $rc
