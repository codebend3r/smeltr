# shellcheck shell=sh
# Every commit subject is `SMLTR: <Capitalized verb> ...` -- the rules live in
# commit-rules.sh and are shared with pre-push.sh. Checked HERE, at write time,
# because a rejected subject costs one --edit; the same subject refused at
# push time costs a rebase.
# Bypass: --no-verify, LEFTHOOK=0, or SMELTR_SKIP_HOOKS=1 (lefthook.json `skip`).
cd "$(git rev-parse --show-toplevel)" || exit 1
# An unreadable message file reads as an empty subject, which check_message
# lets through (git refuses an empty message itself) -- so refuse it here.
[ -r "$1" ] || { printf 'commit-msg: cannot read the message file %s\n' "$1"; exit 1; }
. ./.githooks/commit-rules.sh
if ! check_message "$1" allow_fixup; then
  printf 'commit-msg: refused -- %s\n' "$(sed -n '/./{p;q;}' "$1")"
  printf '%s\n' "$problems" | sed 's/^/  /'
  printf 'rules: .claude/skills/commit-format/SKILL.md\nyour message is kept in .git/COMMIT_EDITMSG -- fix it with: git commit --edit -F .git/COMMIT_EDITMSG\n'
  exit 1
fi
