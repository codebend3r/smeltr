# shellcheck shell=sh
# The first pre-commit job in lefthook.json, run before any linter: refuses a
# staged runtime artifact, then trailing whitespace or a conflict marker in a
# staged hunk. The per-file lint jobs that follow it live in lefthook.json and
# run over the STAGED FILES ONLY; the whole-tree `bun run lint` still runs at
# pre-push and in CI.
# The artifact list mirrors test_repo_invariants.py::RuntimeArtifacts -- that
# test reads `git ls-files`, so it only trips after the commit exists. The
# filter is `d` (everything but a deletion): a RENAME or copy onto `token`
# shows as R/C and slipped past the old `AM`.
# Bypass: --no-verify, LEFTHOOK=0, or SMELTR_SKIP_HOOKS=1 (lefthook.json `skip`).
cd "$(git rev-parse --show-toplevel)" || exit 1
bad=$(git diff --cached --name-only --diff-filter=d \
  | grep -Ex 'token|url|server\.pid|server\.log|sysmon\.ring|queue_overrides\.json|pause|notify\.json|auth\.json|notify\.cursor|notify\.cursor\..*|ledger\.jsonl|\.claude/worktrees/.*' || true)
if [ -n "$bad" ]; then
  printf 'pre-commit: refused, runtime artifact staged:\n%s\nunstage with: git restore --staged <file>\n' "$bad"
  exit 1
fi
# Python and shell have no formatter, so this is the only thing enforcing
# .editorconfig on them. staging/ and tests/fixtures/ are byte-for-byte copies
# of something else and opt out in .gitattributes.
if ! git diff --cached --check; then
  printf 'pre-commit: refused, trailing whitespace or a conflict marker in a staged hunk (listed above)\n'
  exit 1
fi
