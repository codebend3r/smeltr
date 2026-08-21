---
name: smeltr-release
description: Use when cutting a release, bumping the version, or creating a version tag in the smeltr repo (path contains `smeltr`) — "bump the version", "tag a release", "npm version patch", "push the tag", "what version are we on". Covers the one command, the preconditions it enforces, and how to undo a bad bump before or after it is pushed.
---

# smeltr Releases

## Overview

One command cuts a release. It bumps `package.json`, commits, creates an
annotated tag, and pushes the commit and the tag to `origin`:

```bash
npm version patch     # 0.1.0 -> 0.1.1
npm version minor     # 0.1.0 -> 0.2.0
npm version major     # 0.1.0 -> 1.0.0
```

Nothing else. There is no build, no publish, no changelog generation, and no
deploy. **The tag is the release.**

`package.json` exists only to drive that command — smeltr is Python 3 stdlib with
no dependencies and no npm packages. It is `"private": true`, so `npm publish`
refuses; never remove that flag.

## Quick Reference

| Want | Command |
|---|---|
| Cut a patch release | `npm version patch` |
| Cut a minor / major release | `npm version minor` / `npm version major` |
| Current version | `node -p "require('./package.json').version"` |
| Every release so far | `git tag -l --sort=-v:refname` |
| What changed since the last tag | `git log $(git describe --tags --abbrev=0)..HEAD --oneline` |

## What the one command actually does

| Step | Mechanism | Notes |
|---|---|---|
| Refuse on a dirty tree | npm built-in | Commit or stash first — no `--force` |
| Byte-compile the Python | `preversion` script | Fails the release on a syntax error |
| Bump `version` in `package.json` | npm built-in | The only file it edits |
| Commit | `.npmrc` `message` | Subject is `SMLTR: Release v1.2.3` |
| Tag `v1.2.3`, annotated | `.npmrc` `tag-version-prefix`, `sign-git-tag` | Must stay annotated — see below |
| Push commit + tag to `origin` | `postversion` script | `git push --follow-tags origin HEAD` |

Two settings are load-bearing and coupled:

- `sign-git-tag = false` keeps the tag **annotated**. `git push --follow-tags`
  pushes annotated tags only, so a lightweight tag would be created locally and
  silently never reach `origin`.
- `message = "SMLTR: Release v%s"` makes the auto-generated commit obey
  `smeltr-commit-format`. Without it npm writes a bare `1.2.3` subject, which
  breaks the `SMLTR:` prefix rule for a commit nobody hand-writes.

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
| `npm version` on a dirty tree | Aborts before doing anything | Commit or stash |
| Hand-editing `version` in `package.json` | No commit, no tag, drifts from `git tag` | Always use `npm version` |
| Making the tag lightweight | Tag never pushed; `origin` has the commit but no release | Keep `sign-git-tag = false` |
| `git push` by hand afterwards | Pushes the commit without the tag | `postversion` already pushed both |
| Removing `"private": true` | `npm publish` becomes possible for a repo with no package | Leave it |
| Adding `__version__` to a `.py` file | Second source of truth, drifts immediately | The tag is the version |
| Releasing while the autopilot is mid-encode | Harmless — no tracked file the driver reads changes | No action needed |

## Relationship to other skills

The release commit is written by npm, not by hand, but it still obeys
`smeltr-commit-format` — that is what `message` in `.npmrc` is for. If that skill's
subject rules ever change, `.npmrc` has to change with it.
