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

Everything visual is one string, `_PAGE`, in `server.py` lines ~210–532.

| Lines | What |
|---|---|
| 218–226 | `:root` design tokens — colours, radius. **Start here.** |
| 227–243 | base typography, `body` |
| 244–250 | stat card grid |
| 251–261 | panels + live encode card |
| 262–273 | progress bar |
| 274–281 | tabs |
| 282–300 | tables |
| 303–322 | markup |
| 347–531 | `renderAlert` `renderStats` `renderLive` `renderQueue` `renderLedger` |

**Three rules. Breaking any of them breaks the page silently:**

1. **Keep the CSP nonces.** `<style nonce="__NONCE__">` and
   `<script nonce="__NONCE__">` are substituted at serve time. CSP is
   `default-src 'none'` — no inline `onclick`, no external CSS/JS, no CDN, no
   Google Fonts.
2. **Never `innerHTML` with server data.** Everything goes through `textContent`
   via the `el()` helper. Movie titles are filesystem strings.
3. **`./smeltr restart`** after editing, then hard-reload.

Currently dark-only; there is no light theme or `prefers-color-scheme` handling.

## Reviewing changes

Two adversarial agents live in `agents/` and are symlinked into `~/.claude`:
`smeltr-code-critic` (code) and `smeltr-data-critic` (the numbers a human reads
before authorising a deletion). Use the data critic after changing anything the
dashboard displays — it has caught mislabelled units, totals computed over
mismatched row sets, and a verdict that reassured on an implausible result.

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
