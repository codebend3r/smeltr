#!/usr/bin/env bash
# The entry points with NOTHING mounted -- no NAS, no staging drive, no ledger.
#
# "An unmounted NAS must not look like a finished job" is a non-negotiable, and
# the code path that renders it is only reachable when the roots really are
# gone. That is the condition a CI runner is inherently in, which is why this
# is `bun run smoke` on the `smoke` job and NOT part of `bun run test`: on the
# Mac that runs the job the roots are mounted and these assertions are false
# by construction.
#
#   smoke_offline.sh report   report.py says LIBRARY INCOMPLETE and PARTIAL
#   smoke_offline.sh next     next_title.py answers 2 or 3 -- never 0 or 1
#
# SMELTR_DIR must point at an EMPTY scratch directory (created if missing) so
# no real ledger, overrides file or pause flag is read.
set -euo pipefail

cd "$(dirname "$0")/.."
: "${SMELTR_DIR:?set SMELTR_DIR to an empty scratch directory}"
mkdir -p "$SMELTR_DIR"
export SMELTR_DIR
PY="${SMELTR_PYTHON:-python3}"

case "${1:-}" in
  report)
    out=$("$PY" dashboard/report.py)
    printf '%s\n' "$out"
    # The whole point: it must say the library is incomplete, and must not
    # present an empty queue as a finished job.
    grep -q 'LIBRARY INCOMPLETE' <<<"$out"
    grep -q 'PARTIAL' <<<"$out"
    echo "ok: report says LIBRARY INCOMPLETE and PARTIAL"
    ;;
  next)
    set +e
    "$PY" pipeline/next_title.py 70 >/dev/null 2>"$SMELTR_DIR/next.err"
    rc=$?
    set -e
    cat "$SMELTR_DIR/next.err"
    # 1 is the stop condition and would tell the driver to EXIT. With the
    # roots unreachable the only honest answers are 2 (not mounted) or
    # 3 (wait). Anything else, and a blind machine ends the job.
    if [ "$rc" = "1" ] || [ "$rc" = "0" ]; then
      echo "FAIL: exit $rc with no library mounted -- a blind run must never look finished"
      exit 1
    fi
    echo "ok: exit $rc"
    ;;
  *)
    echo "usage: $0 report|next" >&2
    exit 2
    ;;
esac
