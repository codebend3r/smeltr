# shellcheck shell=sh
# The `SMLTR:` commit-subject rules -- ONE copy, sourced by commit-msg (every
# commit as it is written) and by pre-push (every commit about to leave this
# machine). They mirror the `commits` job in .github/workflows/pr.yml, which
# only ever sees a pull request: `bun run release` and a plain `git push` land
# on main with no PR, so before 2026-09-05 a direct-to-main commit was checked
# nowhere. The rules themselves are .claude/skills/commit-format.
#
# check_message FILE [allow_fixup]
#   Reads a commit message file, sets $problems (one violation per line) and
#   returns 1 if there are any. Comment lines and everything after git's
#   scissors line are ignored, so a `commit -v` diff is never read as body.
#   `fixup!`/`squash!` subjects pass ONLY with allow_fixup: commit-msg allows
#   them (git writes them for --fixup/--squash and autosquash consumes them),
#   pre-push does not, so one can never land on a remote.
check_message() {
  problems=""
  msg=$(sed -e '/^# ------------------------ >8 ------------------------$/,$d' -e '/^#/d' "$1")
  subject=$(printf '%s\n' "$msg" | sed -n '/./{p;q;}')
  body=$(printf '%s\n' "$msg" | awk 'f { print } !f && NF { f = 1 }')
  case "$subject" in
    "") return 0 ;;          # git refuses an empty message on its own
    "Merge "*) return 0 ;;   # merge commits are exempt, as in pr.yml (--no-merges)
    "fixup! "* | "squash! "*)
      [ "${2:-}" = "allow_fixup" ] && return 0
      problems="'fixup!'/'squash!' is for autosquash -- rebase it away before pushing"
      return 1 ;;
  esac
  add() { problems="${problems}${problems:+
}$1"; }
  printf '%s\n' "$subject" | grep -Eq '^SMLTR: [A-Z]' \
    || add "subject must start with exactly 'SMLTR: ' + a Capitalized imperative verb"
  case "$subject" in *.) add "no trailing period on the subject" ;; esac
  [ "${#subject}" -le 72 ] \
    || add "subject is ${#subject} chars -- max 72 including the prefix"
  printf '%s\n' "$subject" | grep -Eq '^SMLTR: (feat|fix|chore|docs|refactor|test|ci|build|perf|style)(\(|:)' \
    && add "'SMLTR:' REPLACES conventional-commits prefixes -- drop the second one"
  printf '%s\n' "$subject" | grep -Eq '^SMLTR: (Generated|Co-authored|AI)' \
    && add "no AI-authorship credit in the subject"
  printf '%s\n' "$body" | grep -qiE 'Generated with \[?Claude Code|Co-Authored-By:.*(Claude|Anthropic)|AI-assisted' \
    && add "no AI-authorship trailer in the body"
  printf '%s\n' "$body" | grep -qE '^\*' \
    && add "bullets are '-', never '*'"
  [ -z "$problems" ]
}
