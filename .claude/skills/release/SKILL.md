---
name: release
description: Use when cutting a release, bumping the version, or creating a version tag in the smeltr repo (path contains `smeltr`) — "bump the version", "tag a release", "bun run release patch", "push the tag", "what version are we on". Covers the one command, the preconditions it enforces, and how to undo a bad bump before or after it is pushed.
---

# smeltr Releases

## Overview

One command cuts a release. It bumps `package.json`, commits, creates an
annotated tag, and pushes the commit and the tag to `origin`:

```bash
bun run release patch     # 0.1.0 -> 0.1.1
bun run release minor     # 0.1.0 -> 0.2.0
bun run release major     # 0.1.0 -> 1.0.0
```

Nothing else. There is no build, no publish, no changelog generation, and no
deploy. **The tag is the release.**

`package.json` exists only to drive that command — smeltr is Python 3 stdlib with
no runtime dependencies. It is `"private": true`, so `bun publish` refuses;
never remove that flag. bun is the only package manager and task runner: the
`preinstall` guard turns `npm install`/`yarn`/`pnpm` away, and there is no
`.npmrc` and no `package-lock.json` — `bun.lock` is the lockfile.

## Quick Reference

| Want | Command |
|---|---|
| Cut a patch release | `bun run release patch` |
| Cut a minor / major release | `bun run release minor` / `bun run release major` |
| Current version | `bun pm pkg get version` |
| Every release so far | `git tag -l --sort=-v:refname` |
| What changed since the last tag | `git log $(git describe --tags --abbrev=0)..HEAD --oneline` |

## What the one command actually does

| Step | Mechanism | Notes |
|---|---|---|
| Refuse on a dirty tree | `bun pm version` built-in | Commit or stash first — no `--force` |
| Run the full gate | `preversion` script (`bun run verify`) | lint → every suite → build; fails the release on any red |
| Bump `version` in `package.json` | `bun pm version` built-in | The only file it edits |
| Commit | `-m "SMLTR: Release v%s"` in the `release` script | Subject is `SMLTR: Release v1.2.3` |
| Tag `v1.2.3`, annotated | `bun pm version` built-in | Always annotated, never signed — see below |
| Push commit + tag to `origin` | `postversion` script | `git push --follow-tags origin HEAD` |

Two settings are load-bearing and coupled:

- `bun pm version` creates an **annotated** tag (verified: `git cat-file -t`
  answers `tag`). `git push --follow-tags` pushes annotated tags only, so a
  lightweight tag would be created locally and silently never reach `origin`.
- `-m "SMLTR: Release v%s"` lives in the `release` script, because bun does
  NOT read `.npmrc`'s `message`. Without it bun writes a bare `v1.2.3`
  subject, which breaks the `SMLTR:` prefix rule for a commit nobody
  hand-writes. Always release through `bun run release`, never a bare
  `bun pm version`.

## Before running it

- [ ] On `main`, and `git status` is clean
- [ ] `git pull --rebase` — the push at the end is not `--force`, and a stale
      branch means the release aborts *after* the tag exists locally
- [ ] The work being released is already committed in the repo's normal format

The version number lives **only** in `package.json` and the tag. No Python module
carries a `__version__`, the dashboard does not display one, and nothing in the
decision path reads it. Do not add one as part of a release.

## Undoing a bump

**Not yet pushed** (`postversion` failed, or you caught it in time):

```bash
git tag -d v1.2.3
git reset --hard HEAD~1
```

**Already pushed** — only worth it for a genuinely wrong tag, and only if nobody
has pulled it:

```bash
git push origin :refs/tags/v1.2.3     # delete the remote tag
git tag -d v1.2.3                     # delete it locally
git revert <release-commit>           # revert, do NOT force-push main
```

Never force-push `main` to unwind a release. Re-releasing under a fresh patch
number is almost always cheaper than rewriting published history.

## Common Mistakes

| Mistake | What happens | Fix |
|---|---|---|
| `bun run release` on a dirty tree | Aborts before doing anything | Commit or stash |
| Hand-editing `version` in `package.json` | No commit, no tag, drifts from `git tag` | Always use `bun run release` |
| A bare `bun pm version patch` | Commit subject is `v1.2.3`, no `SMLTR:` prefix | Use the `release` script, which carries `-m` |
| `git push` by hand afterwards | Pushes the commit without the tag | `postversion` already pushed both |
| Removing `"private": true` | `bun publish` becomes possible for a repo with no package | Leave it |
| Adding `__version__` to a `.py` file | Second source of truth, drifts immediately | The tag is the version |
| Releasing while the autopilot is mid-encode | Harmless — no tracked file the driver reads changes | No action needed |

## Relationship to other skills

The release commit is written by bun, not by hand, but it still obeys
`commit-format` — that is what the `-m` in the `release` script is for.
If that skill's subject rules ever change, the script has to change with it.
