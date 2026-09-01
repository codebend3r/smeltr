#!/bin/bash
# Point git at the tracked hooks in ops/hooks/.
#
# `.git/hooks/` is not versioned, so the hooks live in the repo and git is told
# where to find them. `core.hooksPath` is per-clone config -- run this once per
# clone (and once per worktree that has its own config).
#
# NOTE: core.hooksPath REPLACES .git/hooks entirely. Only the hooks in
# ops/hooks/ run; the .sample files git ships are inert either way.
#
# Undo with:  git config --unset core.hooksPath
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

chmod +x ops/hooks/*
git config core.hooksPath ops/hooks

echo "hooks installed -> $(git config core.hooksPath)"
for h in ops/hooks/*; do
  echo "  $(basename "$h")"
done
echo
echo "pre-commit: tests/lint.sh (~1 s)   pre-push: tests/run-all.sh (~10 s)"
echo "Bypass either with --no-verify, or SMELTR_SKIP_HOOKS=1."
