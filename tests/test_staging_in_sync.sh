#!/bin/bash
# The staging drive runs the scripts; the repo only holds copies for history.
# A copy that silently drifts from the live one is the exact failure this repo
# already carries elsewhere (four hand-synced library-root lists). Fail loudly.
# watch-encode.sh joined the mirror 2026-08-31 with the two-direction band
# ladder -- it kills encodes and deletes partials, which is exactly the class
# of script whose drift must not go unnoticed.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
X9="/Volumes/Crucial X9/4K Movies"
[ -d "$X9" ] || { echo "SKIP: staging drive not mounted"; exit 0; }
rc=0
for name in autopilot.sh watch-encode.sh; do
  LIVE="$X9/.$name"
  if [ ! -f "$LIVE" ]; then
    printf '\nFAIL no live copy at %s\n' "$LIVE"
    rc=1
    continue
  fi
  if diff -q "$REPO/staging/$name" "$LIVE" >/dev/null; then
    printf '.'
  else
    printf '\nFAIL staging/%s has drifted from %s\n' "$name" "$LIVE"
    diff -u "$REPO/staging/$name" "$LIVE" | head -40
    rc=1
  fi
done
echo
exit $rc
