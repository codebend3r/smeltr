#!/bin/bash
# Every test. Bash suites skip cleanly when the staging drive is not mounted.
set -uo pipefail
cd "$(dirname "$0")/.."
rc=0
echo "=== python ==="; python3 -m unittest discover -s tests || rc=1
# Prefer the LIVE script; fall back to the tracked copy so a clone with no
# staging drive (and CI) still exercises the concurrency guards instead of
# skipping the suite that keeps a folder from being recorded twice.
echo; echo "=== autopilot helpers ==="
if [ -f "/Volumes/Crucial X9/4K Movies/.autopilot.sh" ]; then
  bash tests/test_autopilot_helpers.sh || rc=1
else
  echo "(staging drive not mounted - testing the tracked staging/autopilot.sh)"
  bash tests/test_autopilot_helpers.sh staging/autopilot.sh || rc=1
fi
echo; echo "=== watchdog triage ==="; bash tests/test_watchdog_triage.sh || rc=1
echo; echo "=== projection UI ==="
if command -v node >/dev/null 2>&1; then node tests/test_projection_ui.js || rc=1
else echo "SKIP: node not installed"; fi
echo; echo "=== repaint key ==="
if command -v node >/dev/null 2>&1; then node tests/test_repaint_key.js || rc=1
else echo "SKIP: node not installed"; fi
echo; echo "=== sysmon ui ==="
if command -v node >/dev/null 2>&1; then node tests/test_sysmon_ui.js || rc=1
else echo "SKIP: node not installed"; fi
echo; echo "=== staging drift ==="; bash tests/test_staging_in_sync.sh || rc=1
echo; [ $rc -eq 0 ] && echo "ALL SUITES PASSED" || echo "FAILURES ABOVE"
exit $rc
