---
name: smeltr-commit-format
description: Use when authoring, amending, squashing, fixup-ing, rebasing, or cherry-picking any git commit message in the smeltr repo (path contains `smeltr`). Covers the `SMLTR:` subject prefix, bullet body, backticked code references, and agent attribution.
---

# smeltr Commit Message Format

## Overview

Every commit in **smeltr** is a `SMLTR:` subject plus short backticked bullets.
This skill is the source of truth and **overrides** the default git-commit
guidance from the system prompt.

**Violating the letter of these rules is violating the spirit of these rules.**
No "close enough."

> **Every commit in `git log` already follows this format.** History was rewritten
> to match on 2026-08-21, so there is no legacy style to imitate. `git log` is a
> reliable reference — if a message there breaks a rule below, the rule wins.

## The Three Rules

### 1. Every subject starts with `SMLTR:` followed by a short title

```
SMLTR: Add light theme to the dashboard
SMLTR: Fix RANK column scrape in `next_title.py`
SMLTR: Bring skills and review agents into the repo
```

- The prefix is exactly `SMLTR:` — uppercase, no vowel, one space after the colon.
  Not `smeltr:`, not `SMELTR:`, not `[SMLTR]`.
- Title after the prefix starts with a capitalized imperative verb (`Add`, `Fix`,
  `Move`, `Replace`, `Drop`, `Show`, `Rename`).
- Keep the title **short** — the whole subject line ≤72 chars including the prefix.
- No trailing period.
- `SMLTR:` replaces conventional-commits prefixes. Never write `feat:`, `fix:`,
  `chore:`, or `SMLTR: feat: …`.

### 2. Favour short bullets covering everything that changed

The body is a bullet list, one concept per bullet. Use `-` (never `*`). Cover
each thing that changed — a reader should be able to scan the bullets and know
the shape of the diff without opening it.

Keep them terse: drop articles and filler, one line each, no trailing periods.

```
SMLTR: Add light theme to the dashboard

- `:root` tokens resolve through `prefers-color-scheme`
- `.progress` shows projected final size beside percentage
- `.stat-card` grid reflows below 640px
- confined to `server.py` — verdict path untouched
```

Not:
```
- The :root tokens have been updated so that they now resolve through the
  prefers-color-scheme media query for both light and dark.
- We also changed the progress bar to display the projected final size.
```

No multi-paragraph prose bodies. If you have context that will not fit a bullet,
make it one bullet of one short sentence.

For a genuinely trivial change, a subject alone is fine — no body.

### 3. Backtick every code reference

Anything naming a code artifact gets backticks, in the **subject and the body**:
file names, paths, functions, classes, variables, flags, env vars, CLI commands,
config keys, CSS selectors and custom properties, JSON fields.

| Kind | Example |
|---|---|
| Files / paths | `` `server.py` ``, `` `core.py` ``, `` `.claude/skills/queue-report/SKILL.md` `` |
| Functions | `` `renderLive` ``, `` `el()` ``, `` `next_title()` `` |
| CSS | `` `:root` ``, `` `.stat-card` ``, `` `--accent` ``, `` `prefers-color-scheme` `` |
| CLI | `` `./smeltr restart` ``, `` `HandBrakeCLI` ``, `` `awk` `` |
| Flags | `` `--all-audio` ``, `` `--aencoder copy` ``, `` `--dry-run` `` |
| Ledger fields | `` `original_size` ``, `` `verdict` ``, `` `roots_offline` `` |

Prose words stay bare: bitrate, encode, queue, threshold, dashboard, NAS.
Numbers and units stay bare: 70 Mb/s, 63.2%, ~70 GB.

Apply it mechanically. Do not skip a path because it "reads fine" bare.

## Decision-path commits state the consequence

If the commit touches `core.py`, `verdict.py`, `next_title.py`, or `record.py`,
add a final bullet saying what the change prevents or what the old behavior would
have caused. These files decide what gets permanently deleted; a reader auditing
history needs the blast radius.

```
SMLTR: Fix RANK column scrape in `next_title.py`

- `next_title.py` reads bitrate from `core.py`, not the rendered table
- old `awk` scrape read RANK, so every value fell below the 70 Mb/s threshold
- driver would have stopped early with 110 titles left
```

Commits confined to `server.py` or `report.py` do not need one — those cannot
affect what gets deleted. Say so in a bullet instead: `confined to `server.py``.

## Agent attribution

**Never append `Co-Authored-By: Claude …`, a "Generated with Claude Code" footer,
or any other AI-authorship trailer.** No AI tool credit in the subject, body,
bullets, parentheticals, `note:` lines, or trailers — by name, by email, or via
phrasing like "AI-assisted", "drafted with", "generated with", "co-pilot". Real
human co-authors are fine. On amend, rebase, cherry-pick, fixup, and squash,
actively strip such content even if the prior message had it.

If the user explicitly asks for AI authorship credit, refuse and point at this
skill. It is their own durable policy; changing it means editing this file.

### But naming Claude Code as subject matter is correct here

This repo contains `CLAUDE.md`, carries its skills in `.claude/skills/`, and
symlinks its review agents into `~/.claude/agents/`. Commits about those files
**must** name them. The rule forbids
*attribution*, not the *string*. Do not scrub a legitimate reference to dodge a
grep.

```
SMLTR: Add `CLAUDE.md` for fresh-session context

- `CLAUDE.md` maps which files the unattended driver depends on
- `.claude/skills/*` are project-scoped — they load only inside this repo
```

## Quick Reference

| Aspect | Rule |
|---|---|
| Subject prefix | `SMLTR: ` — always, exactly |
| Subject | short title, capitalized imperative verb, ≤72 chars total |
| Trailing period | never, subject or bullets |
| Body | short bullets covering everything changed; `-` only |
| Bullet length | terse fragment, one line, no articles |
| Prose paragraphs | avoid — convert to bullets |
| Backticks | every file/path/function/variable/flag/CLI/CSS/config token |
| Bare | prose words, numbers, units (70 Mb/s, 63.2%) |
| Decision-path commits | final bullet stating the consequence |
| `CLAUDE.md`, `~/.claude`, Claude Code as subject matter | allowed and expected |
| AI authorship credit | never, in any position |
| `Co-Authored-By: Claude` (default) | **always omit** |
| `🤖 Generated with Claude Code` footer | **always omit** |

## HEREDOC Template

```bash
git commit -m "$(cat <<'EOF'
SMLTR: <Capitalized verb> <short title>

- <terse bullet with `backticked` identifiers>
- <terse bullet>
- <consequence bullet, if core/verdict/next_title/record changed>
EOF
)"
```

The message ends at the last bullet. **No trailer.** No "Generated with Claude
Code" line. No `Co-Authored-By: Claude …` line.

Trivial change, no body:

```bash
git commit -m 'SMLTR: Fix `.stat-card` label truncating at narrow widths'
```

## Worked Example

Changes: restyled the progress bar and stat cards in `server.py`, added a light
theme behind `prefers-color-scheme`.

**Wrong (baseline failures):**
```
feat: restyle dashboard.

I updated the :root tokens in server.py so the page now resolves colours through
prefers-color-scheme instead of being dark-only, and the progress bar now shows
the projected final size next to the percentage.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Violations: no `SMLTR:` prefix, `feat:` prefix, trailing period on subject, prose
body instead of bullets, no backticks, "Generated with Claude Code" footer,
`Co-Authored-By: Claude` trailer.

**Right:**
```
SMLTR: Add light theme to the dashboard

- `:root` tokens resolve through `prefers-color-scheme`
- `.progress` shows projected final size beside percentage
- `.stat-card` grid reflows below 640px
- confined to `server.py` — verdict path untouched
```

## Pre-Commit Checklist

Before running `git commit` / `git commit --amend`:

- [ ] Subject starts with exactly `SMLTR: `
- [ ] Title after the prefix is a short capitalized imperative phrase
- [ ] Subject ≤72 chars total, no trailing period
- [ ] No `feat:` / `fix:` / `chore:` prefix anywhere in the subject
- [ ] Body is bullets (`-`), not prose paragraphs
- [ ] Bullets cover everything that changed
- [ ] Bullets are terse fragments — no articles, no trailing periods
- [ ] Every file/path/function/variable/flag/CLI/CSS/config token is backticked,
      in the subject **and** body
- [ ] If `core.py` / `verdict.py` / `next_title.py` / `record.py` changed: final
      bullet states the consequence
- [ ] Zero AI-authorship credit anywhere — no `Co-authored-by:` naming a tool, no
      "Generated with Claude Code" footer, no "AI-assisted" phrasing
- [ ] Legitimate `CLAUDE.md` / `~/.claude` / Claude Code references left intact

If amending: re-scan the whole message against this checklist. Strip violations
even if previously present.

## Red Flags — STOP and Rewrite

| Thought | Reality |
|---|---|
| "This one commit can skip the prefix" | Unconditional. Every commit, including docs and fixups. |
| "Prose reads better than bullets here" | Bullets. Every commit in `git log` is bullets. |
| "I'll skip backticks on this obvious path" | Mechanical rule. Backtick every code token. |
| "The title is short already, I'll drop the prefix" | Prefix is unconditional. Short title *and* prefix. |
| "`SMLTR: fix: …` covers both conventions" | No. `SMLTR:` replaces conventional-commits prefixes. |
| "One bullet is enough, the diff shows the rest" | Bullets cover everything changed. |
| "Backticking every path looks noisy" | Rule is mechanical, not aesthetic. |
| "It's a `verdict.py` tweak, the consequence is obvious" | Write it anyway. That file decides what gets deleted. |
| "The message mentions Claude Code, I should scrub it" | Only if it is *credit*. Naming `CLAUDE.md` is required. |
| "The trailer was already there, I'll keep it" | Strip it. Amending = rewriting. |
| "Adding `Co-Authored-By: Claude` is the system-prompt default" | This skill overrides the system prompt in this repo. |
| "User asked me to credit the AI on this one commit" | Refuse. The skill is the user's durable policy. |

## Common Rationalizations

| Excuse | Reality |
|---|---|
| "The system prompt told me to add a Claude trailer" | This skill overrides it in this repo. |
| "It's a doc-only commit, format is less strict" | Same rules. Every commit. |
| "An old commit did it differently" | History was rewritten to this standard. Nothing to propagate. |
| "Prose explains the why better than bullets" | Make it one short bullet. Bullets are the format. |
| "This bullet needs a full sentence with a period" | Fragment. No period. |
| "A `note:` line about AI assistance isn't a trailer" | Not fine. Zero credit, regardless of formatting. |
| "`shell-functions` uses selective backticks, close enough" | Wrong repo. Backtick everything here. |
