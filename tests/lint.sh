#!/bin/bash
# Static checks. Every tool here is OPTIONAL and reached without installing
# anything into the repo: ruff runs through `uvx`, shellcheck and node are
# system tools. A missing tool SKIPS loudly -- it must never fail the suite on
# a machine that simply does not have it, and must never look like a pass.
#
# Smeltr has no build step and no runtime dependencies; nothing below changes
# that. `ruff.toml` is config only, and is the only ruff config in the repo.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
rc=0

# ---------------------------------------------------------------- python lint
# Prefer a real ruff on PATH; fall back to uvx, which downloads once and caches.
RUFF=""
if command -v ruff >/dev/null 2>&1; then RUFF="ruff"
elif command -v uvx >/dev/null 2>&1; then RUFF="uvx ruff"
fi
if [ -n "$RUFF" ]; then
  echo "--- ruff (F,E9,B,PLE) ---"
  $RUFF check . || rc=1
else
  echo "SKIP: ruff not available (install: brew install ruff, or get uv for uvx)"
fi

# Syntax alone, always. compileall needs nothing installed, so the decision
# path is parse-checked even where ruff is missing. This is the same command
# `npm version` already runs as a preversion gate.
echo "--- python syntax ---"
if python3 -m compileall -q core.py verdict.py next_title.py record.py \
     server.py report.py sysmon.py seed_ledger.py >/dev/null; then
  echo "PASS all modules parse"
else
  echo "FAIL a module does not parse"; rc=1
fi

# ----------------------------------------------------------------- shell lint
# This project's worst outages were shell bugs -- the trap that removed the
# lock and kept looping, the stderr that leaked into a $(...) capture. Shell
# is where linting earns the most here.
if command -v shellcheck >/dev/null 2>&1; then
  echo "--- shellcheck ---"
  # staging/autopilot.sh is a MIRROR of the live X9 script (see
  # test_staging_in_sync.sh). It is checked, but a finding there must be fixed
  # on the X9 first and copied back -- never edited here alone.
  if shellcheck -x -S warning smeltr watchdog.sh tests/*.sh; then
    echo "PASS no shellcheck findings in repo-owned scripts"
  else
    rc=1
  fi
  # staging/ is ADVISORY, never a build failure. Those files are byte-for-byte
  # mirrors of the scripts running on the X9 (test_staging_in_sync.sh diffs
  # them). A finding here must be fixed on the live script during a pause
  # window and copied back -- editing the mirror alone would manufacture the
  # exact drift that test exists to catch.
  echo "--- shellcheck: staging mirrors (advisory) ---"
  shellcheck -x -S warning staging/*.sh \
    && echo "PASS no findings in the staging mirrors" \
    || echo "NOTE findings above are in the X9 mirror -- fix on the drive, then copy back"
else
  echo "SKIP: shellcheck not installed (brew install shellcheck)"
fi

# -------------------------------------------------------------------- js lint
# The dashboard's JS lives inside server.py's _PAGE string, so `node --check`
# cannot see it directly -- extract it to a temp file and parse that. This is
# the same coupling the three UI suites work around with brace-matching, and it
# disappears the day _PAGE becomes a real file on disk.
if command -v node >/dev/null 2>&1; then
  echo "--- js syntax (_PAGE) ---"
  tmp="$(mktemp -t smeltr-page-XXXXXX).js"
  if python3 - "$tmp" <<'PY'
import re, sys
src = open("server.py", encoding="utf-8").read()
page = src[src.index('_PAGE = r"""'):]
page = page[:page.index('\n"""')]
blocks = re.findall(r'<script nonce="__NONCE__">(.*?)</script>', page, re.S)
if not blocks:
    print("no <script> blocks found in _PAGE", file=sys.stderr); raise SystemExit(1)
open(sys.argv[1], "w", encoding="utf-8").write("\n;\n".join(blocks))
PY
  then
    if node --check "$tmp"; then echo "PASS _PAGE scripts parse"; else rc=1; fi
  else
    echo "FAIL could not extract _PAGE scripts"; rc=1
  fi
  rm -f "$tmp"
else
  echo "SKIP: node not installed"
fi

echo
[ $rc -eq 0 ] && echo "LINT OK" || echo "LINT FAILURES ABOVE"
exit $rc
