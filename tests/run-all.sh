#!/bin/bash
# Every test. Bash suites skip cleanly when the staging drive is not mounted.
set -uo pipefail
cd "$(dirname "$0")/.."
rc=0
echo "=== python ==="; python3 -m unittest discover -s tests || rc=1
echo; echo "=== autopilot helpers ==="; bash tests/test_autopilot_helpers.sh || rc=1
echo; echo "=== watchdog triage ==="; bash tests/test_watchdog_triage.sh || rc=1
echo; echo "=== projection UI ==="
if command -v node >/dev/null 2>&1; then node tests/test_projection_ui.js || rc=1
else echo "SKIP: node not installed"; fi
echo; echo "=== staging drift ==="; bash tests/test_staging_in_sync.sh || rc=1
echo; [ $rc -eq 0 ] && echo "ALL SUITES PASSED" || echo "FAILURES ABOVE"
exit $rc
