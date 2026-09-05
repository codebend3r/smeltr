---
name: pr-format
description: Use when opening, retitling, or rewriting the body of any pull request in the smeltr repo (path contains `smeltr`) — `gh pr create`, `gh pr edit`, "open a PR", "write the PR description", "update the PR body". Covers the `SMLTR:` title, the five required sections, and how the PR body reuses the commit bullets verbatim.
---

# smeltr Pull Request Format

## Overview

**A smeltr commit is a simplified PR. A smeltr PR is that commit plus its
evidence.** The two share a title format and share bullets verbatim — the PR adds
the motivation, the blast radius, and the proof.

This skill is the source of truth for PR bodies and **overrides** any default
pull-request guidance. `.github/pull_request_template.md` is the same structure in
mechanical form; keep the two in step.

**Violating the letter of these rules is violating the spirit of these rules.**
No "close enough."

**REQUIRED BACKGROUND:** `commit-format` defines the title rules, the
bullet style, the backtick rules, and the AI-attribution ban. All of them apply
here unchanged. This skill does not restate them — it says where they land.

## The Two Rules

### 1. The title is a commit subject

Identical rules, no exceptions: `SMLTR:` prefix, capitalized imperative verb,
≤72 chars total, no trailing period, backtick every code reference.

```
SMLTR: Fix RANK column scrape in `next_title.py`
SMLTR: Add light theme to the dashboard
```

- **One commit in the PR** — the title *is* that commit's subject, character for
  character. Never paraphrase it.
- **Several commits** — write one subject covering all of them. Do not chain
  them with "and", and do not fall back to a branch name.
- Never `feat:`, never `[SMLTR]`, never a bare `Update server.py`.

### 2. Five sections, always, in this order

```markdown
## What changed
## Why
## Blast radius
## Verification
## Review
```

Every section is required on every PR, including a one-line typo fix. Do not add
a sixth, do not drop one, do not reorder. A section with nothing to say gets one
bullet saying so — `- none` is a real answer, an empty section is not.

Body content is bullets. `-` only, one concept per line, terse fragments, no
articles, no trailing periods, no prose paragraphs, no checkbox theatre. Same
bar as a commit body.

## What goes in each section

| Section | Content | Sourced from |
|---|---|---|
| `## What changed` | The shape of the diff | **Verbatim the commit bullets** |
| `## Why` | What forced the change — the problem, not a restatement of the diff | New writing |
| `## Blast radius` | What this can and cannot cause to be deleted | The commit's consequence bullet, expanded |
| `## Verification` | Commands actually run, each with its outcome | New writing |
| `## Review` | Which critic agent ran and what it found | New writing |

### `## What changed` is copied, not rewritten

The commit bullets already cover everything that changed in the house style.
Paste them. Rewriting them produces two divergent descriptions of one diff and
is the single most common failure here.

Several commits: concatenate their bullets in commit order and drop exact
duplicates. Still verbatim — do not "smooth them out" while merging.

### `## Blast radius` is never omitted

This pipeline **permanently deletes ~70 GB originals**. A reviewer needs the
answer without opening the diff.

Touching `core.py`, `verdict.py`, `next_title.py`, or `record.py` — state what
changes about what gets deleted, and what the old behavior would have caused:

```markdown
## Blast radius

- `next_title.py` picks encode order — wrong bitrate source stops the driver early
- old `awk` scrape read RANK, so every value fell below the 70 Mb/s threshold
- driver would have stopped with 110 titles left, deleting nothing extra
```

Confined to `server.py` or `report.py` — say so, and say why it is safe:

```markdown
## Blast radius

- confined to `server.py` — never imported by `verdict.py`, cannot affect deletions
- worst case the page breaks and the pipeline keeps running
```

### `## Verification` is commands and outcomes

Name what was run and what came back. "Tested locally" is not verification.

```markdown
## Verification

- `./smeltr report` — 132 queued, totals unchanged
- `./smeltr restart` + hard reload — dashboard renders, SSE reconnects
- `python3 -m compileall` clean across all modules
```

A dashboard change puts its screenshot in this section. There is no separate
screenshots section.

### `## Review` names the critic

```markdown
## Review

- `smeltr-data-critic` — flagged GB/GiB mislabel in the totals row, fixed
- `smeltr-code-critic` not run — no logic change
```

## Agent attribution

Same ban as commits, extended to the PR surface. **No `Generated with Claude
Code` footer, no `Co-Authored-By: Claude`, no "AI-assisted" phrasing** in the
title, body, section bullets, or review comments. `gh pr create` does not add one
— do not add it yourself.

Naming `CLAUDE.md`, `~/.claude`, or a skill file as *subject matter* stays
correct and expected. The rule forbids attribution, not the string.

## Quick Reference

| Aspect | Rule |
|---|---|
| Title | `SMLTR: ` + capitalized imperative verb, ≤72 chars, no trailing period |
| Single-commit PR title | that commit's subject, character for character |
| Sections | exactly five, exactly that order, never omitted |
| Empty section | one bullet — `- none` |
| Body | bullets, `-` only, terse, no trailing periods |
| `## What changed` | verbatim the commit bullets |
| Prose paragraphs | never |
| Checkbox / task lists | never |
| Backticks | every file, path, function, flag, CLI, CSS token, ledger field |
| Bare | prose words, numbers, units (70 Mb/s, 63.2%) |
| Blast radius on `server.py`-only PRs | still required — state it is confined |
| AI authorship credit | never, in any position |

## Template

```bash
gh pr create --title 'SMLTR: <Capitalized verb> <short title>' --body "$(cat <<'EOF'
## What changed

- <commit bullet, verbatim>
- <commit bullet, verbatim>

## Why

- <the problem that forced this>

## Blast radius

- <what this changes about what gets deleted, or why it cannot>

## Verification

- `<command>` — <outcome>

## Review

- `smeltr-code-critic` — <finding, or why not run>
EOF
)"
```

The body ends at the last bullet. **No trailer.**

## Common Mistakes

| Mistake | Fix |
|---|---|
| Rewording the commit bullets for the PR | Paste them verbatim |
| Dropping `## Blast radius` on a "trivial" change | Required on every PR — `- confined to `server.py`` |
| `## Why` restating the diff | Why is the problem; What is the diff |
| "Tested locally" under Verification | Name the command and its outcome |
| Prose paragraph instead of bullets | Convert; one concept per bullet |
| Adding `## Screenshots` | Screenshot goes in `## Verification` |
| Branch name as the title | Write a real `SMLTR:` subject |
| Checkbox list of intentions | Verification is what you ran, not what you plan |
