# Smeltr — working notes

A local dashboard and ledger for a long-running 4K HEVC re-encode job. UHD
remuxes (60–100 Mb/s) are re-encoded with HandBrakeCLI, verified, and moved back
to a NAS — replacing an ~80 GB original with an ~30 GB file that keeps every
audio and subtitle track.

Python 3 stdlib + vanilla JS. **No dependencies, no build step, no CDN.**
Run `./smeltr report` or open <http://127.0.0.1:8787/>.

## THIS IS LIVE INFRASTRUCTURE — read before editing

A detached `autopilot.sh` on the staging drive is running unattended right now.
It encodes, judges, syncs, and **permanently deletes ~70 GB library originals**
on a `good` verdict. It calls this repo on every cycle.

| File | Autopilot depends on it | Safe to edit freely |
|---|---|---|
| `core.py` | **YES** — the verdict and queue logic | no |
| `verdict.py` | **YES** — exit code decides sync-or-halt | no |
| `next_title.py` | **YES** — picks what encodes next | no |
| `record.py` | **YES** — writes the ledger | no |
| `server.py` | no — never imported by the decision path | **yes** |
| `report.py` | no | **yes** |

Verified: `verdict.py` does not load `server.py`. **Editing the dashboard cannot
affect what gets deleted.** Worst case the page breaks and the pipeline keeps
running.

Before touching `core.py` / `verdict.py` / `next_title.py` / `record.py`, pause
the driver:

```bash
pkill -f autopilot.sh && rm -rf "/Volumes/Crucial X9/4K Movies/.autopilot.lock"
```

## Editing the look and feel

Everything visual is one string, `_PAGE`, in `server.py` lines ~210–624.

| Lines | What |
|---|---|
| 218–236 | `:root` dark design tokens — colours, radius. **Start here.** |
| 237–254 | `:root[data-theme="light"]` — the light overrides, same token names |
| 255–283 | base typography, `body`, header, theme toggle button |
| 284–290 | stat card grid |
| 291–301 | panels + live encode card |
| 302–313 | progress bar |
| 314–319 | tabs |
| 320–352 | tables + the hover-only scrollbar |
| 354–363 | pre-paint theme script (runs in `<head>`) |
| 367–387 | markup |
| 413–560 | `renderAlert` `renderStats` `renderLive` `renderQueue` `renderLedger` |

**Three rules. Breaking any of them breaks the page silently:**

1. **Keep the CSP nonces.** One `<style>` and **two** `<script>` blocks carry
   `nonce="__NONCE__"`, substituted at serve time — the second script is the
   pre-paint theme block in `<head>`. A block without the nonce never runs. CSP is
   `default-src 'none'` — no inline `onclick`, no external CSS/JS, no CDN, no
   Google Fonts.
2. **Never `innerHTML` with server data.** Everything goes through `textContent`
   via the `el()` helper. Movie titles are filesystem strings.
3. **`./smeltr restart`** after editing, then hard-reload.

**Colours are tokens, never hex literals.** Both themes are token sets with the
same names; a hex written anywhere below `:root` is a colour the light theme
cannot reach, which is exactly how the page stayed half-dark before. If you add
a colour, add it to both `:root` blocks.

Theme resolution, in order: an explicit choice in `localStorage["smeltr.theme"]`
wins; otherwise `prefers-color-scheme` wins and keeps winning as the OS flips.
The `<head>` script applies it before first paint — moving that logic into the
main script at the bottom reintroduces a flash of the wrong theme on load.

`--ink-3` carries the small uppercase labels, muted cells, and footer. In light
it is set to clear 4.5:1 on all three surfaces. **In dark it sits at ~3.3–3.6:1
and always has** — pre-existing, not yet addressed.

## Reviewing changes

Two adversarial agents live in `agents/` and are symlinked into `~/.claude`:
`smeltr-code-critic` (code) and `smeltr-data-critic` (the numbers a human reads
before authorising a deletion). Use the data critic after changing anything the
dashboard displays — it has caught mislabelled units, totals computed over
mismatched row sets, and a verdict that reassured on an implausible result.

## Releasing and pull requests

`npm version patch|minor|major` is the entire release: it bumps `package.json`,
commits as `SMLTR: Release vX.Y.Z`, creates the annotated tag, and pushes the
commit and tag to `origin`. No build, no publish, no deploy — the tag is the
release. `package.json`, `.npmrc`, and `.github/` are outside the decision path
and are safe to edit while the driver runs.

Commit messages follow the `smeltr-commit-format` skill. PR titles and bodies
follow `smeltr-pr-format` — five required sections, and `## What changed` copies
the commit bullets verbatim. `.github/pull_request_template.md` is that same
structure mechanically; if one changes, change the other.

## Non-negotiables in the domain

- **Every audio and subtitle track is preserved.** `--all-audio --aencoder copy
  --audio-fallback ac3 --all-subtitles`. Never `--audio-lang-list`.
- **Never invent a missing number.** One ledger row has no original size because
  the file was deleted before it was recorded; it is excluded from every total
  rather than back-solved. Provenance is shown per row.
- **An unmounted NAS must not look like a finished job.** The queue empties when
  a library root is unreachable; `library_complete` / `roots_offline` exist so
  neither view presents that as "nothing left".

`~/.claude/skills/*` and `~/.claude/agents/smeltr-*.md` are **symlinks into this
repo** — editing them edits tracked files.
