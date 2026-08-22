#!/bin/bash
# The staging drive runs the script; the repo only holds a copy for history.
# A copy that silently drifts from the live one is the exact failure this repo
# already carries elsewhere (four hand-synced library-root lists). Fail loudly.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIVE="/Volumes/Crucial X9/4K Movies/.autopilot.sh"
[ -f "$LIVE" ] || { echo "SKIP: staging drive not mounted"; exit 0; }
if diff -q "$REPO/staging/autopilot.sh" "$LIVE" >/dev/null; then
  echo "PASS staging/autopilot.sh matches the live script"
else
  echo "FAIL staging/autopilot.sh has drifted from $LIVE"
  diff -u "$REPO/staging/autopilot.sh" "$LIVE" | head -40
  exit 1
fi
