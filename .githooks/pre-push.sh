# shellcheck shell=sh
# Two gates before anything leaves this machine. First, every commit not yet on
# ANY remote carries a `SMLTR:` subject and none is a leftover `fixup!` --
# commit-msg already refused the subject at write time, but --no-verify,
# --amend on an old commit and a rebase all bypass it, and the PR format
# checker only ever sees a pull request. Then `bun run verify` (lint, every suite, build,
# ~20 s) -- the same gate `bun run release` runs as `preversion`. A push that
# only deletes refs (all-zero local sha on stdin) has no tree to test and is
# let through.
# The ref list arrives on stdin only because lefthook.json sets `use_stdin`;
# without it every push would look like a deletion and skip both gates.
# Bypass: --no-verify, LEFTHOOK=0, or SMELTR_SKIP_HOOKS=1 (lefthook.json `skip`).
cd "$(git rev-parse --show-toplevel)" || exit 1
set --
while read -r _lref sha _rref _rsha; do
  case "$sha" in *[!0]*) set -- "$@" "$sha" ;; esac
done
[ "$#" -gt 0 ] || exit 0
. ./.githooks/commit-rules.sh
tmp=$(mktemp) || exit 1
trap 'rm -f "$tmp"' EXIT
rc=0
for c in $(git rev-list --no-merges "$@" --not --remotes); do
  git log -1 --format=%B "$c" > "$tmp"
  if ! check_message "$tmp"; then
    printf 'pre-push: refused commit %s -- %s\n' "$(git rev-parse --short "$c")" "$(git log -1 --format=%s "$c")"
    printf '%s\n' "$problems" | sed 's/^/  /'
    rc=1
  fi
done
[ "$rc" -eq 0 ] || { printf 'rules: .claude/skills/commit-format/SKILL.md -- reword with git rebase -i, then push again\n'; exit 1; }
bun run --silent verify
