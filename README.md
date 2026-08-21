# Smeltr

Remuxes in · ingots out.

A local dashboard and terminal report for a long-running 4K HEVC re-encode job:
UHD remuxes (60–100 Mb/s) are re-encoded with HandBrakeCLI at CRF 16, verified,
and moved back to the NAS, replacing an ~80 GB original with an ~30 GB file that
keeps every audio and subtitle track.

Smeltr does not run the encodes. It **observes** them and keeps the ledger.
Reporting and acting are deliberately separate: nothing here starts, stops, or
alters an encode, and nothing writes to the media library.

## Use

```bash
./smeltr report                 # terminal snapshot: totals, live encode, queue, history
./smeltr report --all           # every row
./smeltr open                   # open the dashboard in a browser
./smeltr start|stop|status|url
./smeltr record "<Folder>" --source-path <path-to-original> [--dest Vhagar/F]
```

The dashboard is at <http://127.0.0.1:8787/> and updates over Server-Sent Events.

## Layout

| File | Role |
|---|---|
| `core.py` | Read-only data layer: log parsing, `ps` liveness, queue, ledger, verdicts |
| `server.py` | Stdlib HTTP + SSE server; the dashboard HTML/CSS/JS is embedded |
| `report.py` | Terminal table renderer |
| `record.py` | Appends a finished encode to the ledger, behind liveness + parity gates |
| `smeltr` | Launcher |
| `ledger.jsonl` | **The durable history.** Append-only, one JSON object per encode |
| `seed_ledger.py` | One-shot migration from the pre-Smeltr text state file |

No dependencies beyond Python 3 and a browser. No build step. `ffprobe` is
needed only by `record.py`.

Paths resolve from the script's own location, so the checkout can live anywhere.
`SMELTR_DIR` overrides where the ledger and runtime files are kept, `SMELTR_X9`
overrides the staging drive.

## Skills and agents

The Claude Code skills and review agents for this job live in the repo too, and
are symlinked into `~/.claude` so Claude Code still finds them:

```
skills/queue-report/           this dashboard's own skill
skills/4k-hevc-reencoding/     the CRF ladder and the mandatory track passthrough
skills/4k-hevc-library-sync/   verified sync back to the NAS
skills/4k-hevc-preview-encode/ 5-minute sample before committing to a long run
skills/smeltr-commit-format/   `SMLTR:` commit subjects and bullet bodies
skills/smeltr-pr-format/       the five required pull request sections
skills/smeltr-release/         `npm version` -> tag -> push
agents/smeltr-code-critic.md   ruthless code reviewer
agents/smeltr-data-critic.md   ruthless reviewer of the numbers a human reads
```

To wire them up on a fresh machine:

```bash
ln -s "$PWD/skills/<name>" ~/.claude/skills/<name>
ln -s "$PWD/agents/<name>.md" ~/.claude/agents/<name>.md
```

The two critics exist because this pipeline **deletes irreplaceable originals**.
They are deliberately adversarial and are pointed at different targets — one at
the code, one at the numbers a person actually reads before authorising a
deletion. Between them they caught, among others: an unmounted NAS rendering
identically to a finished job, track counts read from the source scan block
(so track loss could never be detected), a vacuous ffprobe parity check that
passed on zero evidence, and a ledger write with no liveness gate.

## Releasing

```bash
npm version patch     # 0.1.0 -> 0.1.1
```

That one command bumps `package.json`, commits as `SMLTR: Release v0.1.1`, creates
the annotated tag `v0.1.1`, and pushes the commit and the tag to `origin`. There is
no build, no publish, and no deploy — **the tag is the release**.

`package.json` exists only to drive that command; smeltr itself has no npm
dependencies and is `"private": true` so it can never be published. The version
lives in `package.json` and the tag and nowhere else — no Python module carries a
`__version__`. A `preversion` hook byte-compiles every module first, so a release
cannot be tagged over a syntax error in the decision path.

See `skills/smeltr-release/SKILL.md`, including how to undo a bump.

## Design notes

**The ledger is written before the sync, never after.** Once the original is
deleted its size is unrecoverable, and a row without it can never be completed.
One title (`Wanted (2008)`) is in exactly that state and is excluded from every
total rather than being back-solved into something that looks tidy.

**Provenance is recorded per row** — `measured at finish`, `hand-migrated`, or
`found after the fact` — because those are not the same evidence and the totals
should not pretend otherwise.

**An unmounted NAS must not look like a finished job.** The queue drops rows
whose files it cannot see, so an unreachable library silently empties it. The
summary carries `library_complete` / `roots_offline`, and both views refuse to
present a partial queue as authoritative.

**The verdict has two independent checks on different scales.** A raw floor
catches output too small to be physically plausible; a median baseline catches
output far below what this job actually achieves, measured per retained pixel so
letterboxed films are not punished for their auto-crop. The baseline is the
median rather than the best-ever: with a minimum, one legitimate outlier
permanently widens "normal" and the detector disarms itself.

## Security

The server binds `127.0.0.1` only, allowlists the `Host` header (defeating DNS
rebinding), sends no CORS headers, and serves a nonce-based CSP with no external
origins. Every value reaches the DOM via `textContent`. No endpoint mutates
anything. Set `SMELTR_REQUIRE_TOKEN=1` to additionally require a per-launch
token on every request — worth it on a shared machine, needless on a personal
one, where it mainly breaks the bookmarkable URL.
