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

One nuance since the skip/reorder feature: `server.py` is no longer a pure
reader — its two POST endpoints write `queue_overrides.json`, which the
decision path READS (see below). That file steers only *which title encodes or
stages next*, never the verdict, the sync, or a deletion. A broken or deleted
overrides file degrades to stock bitrate order everywhere.

## Queue overrides (skip + drag-to-reorder)

`queue_overrides.json` lives beside the ledger:
`{"skip": [titles], "priority": [titles]}` — exact folder names, matched
case-insensitively. Written ONLY by `core.save_overrides()` (atomic
`os.replace`, so the driver can never see a torn file); the dashboard's
`POST /api/queue/skip` and `POST /api/queue/order` are the only callers, and
they accept only titles the queue itself just reported — never a path.

Who honours it:

- `core.queue()` marks rows `skipped`/`pinned` and sorts pinned-first,
  skipped-last; `summary()` excludes skipped rows from the queue totals —
  EXCEPT `job_progress_pct`, whose goal deliberately keeps skipped bytes so
  skipping work can never render as finishing it.
- `next_title.py` never picks a skipped row. When every staged candidate is
  hand-skipped it exits **3** (not the stop condition), and `.autopilot.sh`
  waits 300 s instead of exiting on a false "nothing left" message.
- `.replenish-queue.sh` (staging drive) never stages a skipped title, stops
  counting skipped staged folders toward its 10-folder target (so skipping a
  staged title pulls the next library title in), and stages `priority`
  titles first. It tops up at most 3 titles per run and gates every pull on
  `df` free space (source size + 10 GiB margin).
- A skipped title stays **visible** in the queue (greyed, at the bottom, with
  a restore button). A skip that vanished would read as "finished".

Failure modes are loud, not silent: `save_overrides` fsyncs before its atomic
replace, and a present-but-unparseable file falls back to stock order while
`summary()` raises `overrides_corrupt` — both views show a banner saying
skips are NOT being applied.

The skip endpoint refuses three states with a 409 rather than pretending:
a title that is **encoding** right now, a title whose encode **already
finished** (the driver finds finished folders by disk scan and will
judge/record/sync regardless of the overrides — accepting the skip would show
a greyed row while the library original is deleted), and a **duplicate folder
basename** (overrides key on the folder name; one click must never act on two
files — full path keying is the known follow-up if the threshold is ever
lowered past the duplicate pairs).

Drag semantics in the UI: a drop pins the MINIMAL head of the visible order
needed to reproduce it under the server sort — the longest tail already in
descending-bitrate order stays unpinned, and dropping a row back into pure
bitrate order clears the pins entirely. The table always encodes in exactly
the order shown; "Reset order" clears the head. Mutating requests require the
`X-Smeltr: 1` header (CSRF: a cross-origin page cannot attach it without a
CORS preflight the server never grants), and every denied request closes its
connection so a rejected POST's body can never be replayed as a smuggled
second request.

Before touching `core.py` / `verdict.py` / `next_title.py` / `record.py`, pause
the driver:

```bash
pkill -f autopilot.sh && rm -rf "/Volumes/Crucial X9/4K Movies/.autopilot.lock"
```

(`pkill` can miss the detached process — verify with `ps aux | grep autopilot.sh`
and `kill <pid>` directly if needed. A running HandBrake encode is independent
and survives the pause.) Restart it afterwards, exactly as its header documents:

```bash
cd "/Volumes/Crucial X9/4K Movies" && nohup ./.autopilot.sh >> .autopilot.log 2>&1 &
```

## Editing the look and feel

Everything visual is one string, `_PAGE`, in `server.py` lines ~302–1057.
(The POST override endpoints sit above it, lines ~192–300.)

| Lines | What |
|---|---|
| 310–332 | `:root` dark design tokens — colours, radius, motion accents. **Start here.** |
| 333–352 | `:root[data-theme="light"]` — the light overrides, same token names |
| 353–384 | base typography, `body`, header, theme toggle, pulsing status dot |
| 385–405 | stat card grid + change-flash + first-paint entrance stagger |
| 406–416 | panels + live encode card |
| 417–445 | the molten progress bar (`.barrow`/`.bar`) + 3-decimal readout |
| 446–452 | tabs |
| 453–514 | tables, hover-only scrollbar, pin/skip marks, grip + drop indicators, skip buttons, pane fade |
| 515–556 | responsive media queries — ≤700px pinned title column + `.cut`; coarse pointer hides drag grips (no DnD on touch) |
| 558–569 | pre-paint theme script (runs in `<head>`) |
| 570–597 | markup (incl. the hidden Reset-order button) |
| 621–651 | `api()` POST helper — the page's only writes |
| 639–952 | `renderAlert` `renderStats` `renderLive` `renderQueue`+`wireDrag` `renderLedger` `paint` |
| 954–1010 | seam-blanking script — blanks any column sliced at the pane edges or the pinned title |

Animation ground rules: the live card updates **in place** (`liveRefs`) —
rebuilding it every SSE frame restarts every CSS animation and kills the bar's
width transition, which is exactly the bug the old renderer had. Entrance
animations are gated on `body:not(.booted)` so SSE rebuilds don't replay them.
`prefers-reduced-motion` disables all animation and transitions globally.

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
