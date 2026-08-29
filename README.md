# Smeltr

[![CI](https://github.com/codebend3r/smeltr/actions/workflows/ci.yml/badge.svg)](https://github.com/codebend3r/smeltr/actions/workflows/ci.yml)

Remuxes in · ingots out.

A local dashboard, terminal report, and append-only ledger for a long-running
4K HEVC re-encode job: UHD remuxes (60–100 Mb/s) are re-encoded with
HandBrakeCLI at CRF 16, verified, and moved back to the NAS — replacing an
~80 GB original with an ~30 GB file that keeps **every** audio and subtitle
track. 12 encodes in, the job has reclaimed just over 500 GiB with ~5.7 TiB
projected to go.

Python 3 stdlib and vanilla JS. **No dependencies, no build step, no CDN.**

```bash
./smeltr report      # terminal snapshot
./smeltr open        # live dashboard at http://127.0.0.1:8787/
```

## What Smeltr is — and is not

Smeltr **observes** the pipeline and keeps its history. It parses HandBrake
logs, checks process liveness with `ps`, ranks the remaining library by
bitrate, and judges each finished encode. It does not start, stop, or alter an
encode, and it never writes to the media library.

The one thing it *decides* is what the unattended driver does next. A detached
`autopilot.sh` on the staging drive runs the full cycle — encode → judge →
record → sync → purge → replenish — and calls this repo at every decision
point:

```
                        ┌──────────────────────── this repo ────────────────────────┐
   NAS library          │                                                           │
  (Vhagar/Vermithor) ──▶ next_title.py ──▶ encode ──▶ verdict.py ──▶ record.py ──▶ ledger.jsonl
        ▲                     queue            │        exit code      refusal          │
        │                    ranking           │      sync-or-halt      gates           │
        └── verified sync ◀────────────────────┘                                        │
             + delete            server.py / report.py  ◀───── observe only ────────────┘
             original                (dashboard)
```

The only path that deletes a library original is a `good` verdict followed by a
byte-verified, track-parity-checked sync. Everything ambiguous halts loudly and
leaves the drive as-is.

## Use

```bash
./smeltr report                # totals, live encode, queue, recent history
./smeltr report --all          # every row, no truncation
./smeltr report --queue        # queue only        (--history for the ledger)
./smeltr start|stop|restart|status|url|open
./smeltr verdict "<Folder>"    # judge a finished encode; exit code drives the driver
./smeltr next <min-mbps>       # highest-bitrate staged title not yet encoded
./smeltr record "<Folder>" --source-path <path-to-original> \
    [--dest Vhagar/S] [--note "..."] [--verified "ssim 0.9931/0.9945"]
```

`report` starts the dashboard automatically if it isn't up. The dashboard
updates over Server-Sent Events; the queue there is hand-editable — rows drag
to reorder (the order you drop is the order the pipeline picks from) and any
non-encoding row can be skipped. Skipped rows keep their place, greyed with a
restore button: a skip that vanished would read as "finished".

While a finished file travels back to the NAS the queue and History tabs show a
live **transferring** bar (observed from the destination's growing `.partial`,
with rate and time left — a `.partial` that stops growing flips to **stalled**,
never fake progress). A staging pull still in flight shows as **arriving**, not
"staged".

Every top-level block folds — the totals strip, the live encode card, the
"This Mac" monitor, and the queue/history table — each in its own outlined
panel with the chevron in one column at the right edge. The fold is
remembered per browser. Folding never hides a fact that block was the only
one showing: the totals strip keeps a live one-line digest (and still
refuses a queue number while a library root is offline), the tab bar keeps
both row counts, the live card keeps its verdict chip, a refused action
stays visible, and a loud card — a bad note, an offline NAS, an alarming
verdict — always opens regardless of a stored fold.

## Layout

| File | Role |
|---|---|
| `core.py` | Data layer: log parsing, `ps` liveness, queue ranking, ledger, verdict maths |
| `verdict.py` | Judges one finished encode; its **exit code** is the driver's sync-or-halt |
| `next_title.py` | Picks what encodes next; distinct exit codes for stop / offline / all-skipped |
| `record.py` | Appends to the ledger, behind liveness, size, and readability refusal gates |
| `server.py` | Stdlib HTTP + SSE server; the whole dashboard is one embedded page |
| `report.py` | Terminal table renderer |
| `smeltr` | Launcher / subcommand dispatcher |
| `ledger.jsonl` | **The durable history.** Append-only, one JSON object per encode |
| `queue_overrides.json` | Dashboard skip + priority state, written atomically, read by the driver |

Paths resolve from the script's own location, so the checkout can live
anywhere. `SMELTR_DIR` overrides where the ledger and runtime files live;
`SMELTR_X9` overrides the staging drive. `ffprobe` is needed only by
`record.py`.

The library spans three roots across two NAS volumes — `Vhagar/Media/4K
Movies`, `Vermithor/Media/4K Movies`, `Vermithor/Media/4K Family Movies` —
listed in `core.py` `LIBRARY_ROOTS` and mirrored by the driver scripts on the
staging drive.

## The safety model

This pipeline deletes irreplaceable originals, so every number a human reads
before authorising that is treated as load-bearing:

- **The ledger is written before the sync, never after.** Once the original is
  deleted its size is unrecoverable and the row can never be completed. One
  title (`Wanted (2008)`) is in exactly that state and is excluded from every
  total rather than back-solved into something that looks tidy.
- **Provenance is recorded per row** — `measured at finish`, `hand-migrated`,
  `found after the fact` — because those are not the same evidence, and the
  totals do not pretend otherwise.
- **An unmounted NAS must not look like a finished job.** The queue drops rows
  whose files it cannot see, so the summary carries `library_complete` /
  `roots_offline` and both views banner a partial queue rather than presenting
  it as authoritative.
- **The verdict has two independent checks on different scales.** A raw floor
  catches output too small to be physically plausible for 4K; a median
  baseline catches anything far below what this job actually achieves,
  measured per retained pixel so letterboxed films are not punished for their
  auto-crop. The baseline is the median, not the best-ever: with a minimum,
  one legitimate outlier permanently widens "normal" and the detector disarms
  itself.
- **`record.py` refuses rather than guesses**: while HandBrake is still
  writing, while the output was touched in the last two minutes, when the
  output is not smaller than the source, or when ffprobe cannot read either
  file. A ledger row is the evidence that authorises a deletion; it is never
  written on a guess.
- **Skipping work can never render as finishing it** — the job-progress goal
  deliberately keeps skipped bytes.

Two adversarial review agents live in `agents/` — one pointed at the code, one
at the numbers a person actually reads. Between them they have caught an
unmounted NAS rendering identically to a finished job, a vacuous ffprobe
parity check that passed on zero evidence, a ledger write with no liveness
gate, an uppercase transform quietly turning Mb/s into MB/S, and a progress
row that painted once and froze for an entire 45-minute transfer.

## Reaching it from other devices

The launcher binds the dashboard to this machine's LAN IPv4 **and** loopback
(`SMELTR_BIND=lan`; set `SMELTR_BIND=127.0.0.1` for loopback-only). Open the
tokened URL that `./smeltr url` prints on any phone, tablet, or TV on the
network — the token is part of the URL, so a bookmark just works and survives a
`./smeltr restart` (the token persists in a 0600, gitignored `token` file;
delete it to rotate). From the network the dashboard is **read-only**: skip and
reorder work only from this Mac. Set `SMELTR_LAN_WRITES=1` to allow queue edits
from other devices too.

## Dashboard security

Off loopback the URL token is forced on and **is** the auth — every route
except `/healthz` returns 403 without it. A LAN bind refuses any non-private
address (VPN tunnels, public IPs) and fails closed to loopback: the token
travels in cleartext HTTP, fine on a home LAN, never on the internet. The
`Host` header is allowlisted (defeating DNS rebinding — loopback names plus,
when LAN-bound, the bind address, `<hostname>`, `.local`, and `.lan`), no CORS
headers are sent, connections carry a read timeout so a pre-auth peer can't pin
a thread, and the page has a nonce-based CSP with no external origins; every
value reaches the DOM via `textContent`.

The only two mutating routes write `queue_overrides.json` — they accept only
titles the queue itself just reported (never a path), are refused for network
peers by default (loopback only, unless `SMELTR_LAN_WRITES=1`), require the
`X-Smeltr: 1` header (a cross-origin page cannot attach it without a CORS
preflight the server never grants), and every denied request closes its
connection so a rejected body can never be replayed as a smuggled second
request. `X-Smeltr` guards against hostile web pages; the token and the
loopback-write gate are what guard against devices that already hold the URL.

## Skills and agents

The Claude Code skills for this job are **project-scoped** — they live in
`.claude/skills/` and load automatically whenever the working directory is
inside this repo. Nothing needs wiring up on a fresh machine, and they leave no
trace in `~/.claude/skills`:

```
.claude/skills/queue-report/           this dashboard's own skill
.claude/skills/4k-hevc-reencoding/     the CRF ladder and the mandatory track passthrough
.claude/skills/4k-hevc-library-sync/   verified sync back to the NAS
.claude/skills/4k-hevc-preview-encode/ 5-minute sample before committing to a long run
.claude/skills/smeltr-commit-format/   `SMLTR:` commit subjects and bullet bodies
.claude/skills/smeltr-pr-format/       the five required pull request sections
.claude/skills/smeltr-release/         `npm version` -> tag -> push
```

The two review agents are **not** project-scoped — they are symlinked into
`~/.claude/agents/` so they can be dispatched from anywhere:

```
agents/smeltr-code-critic.md   ruthless code reviewer
agents/smeltr-data-critic.md   ruthless reviewer of the numbers a human reads
```

```bash
ln -s "$PWD/agents/<name>.md" ~/.claude/agents/<name>.md
```

## Tests and CI

```bash
bash tests/run-all.sh
```

145 Python tests plus three `node` suites executed against functions pulled
straight out of the embedded page, and three bash suites against the shipped
driver and watchdog scripts. Nothing in the suite touches the NAS, the staging
drive, or the running pipeline.

CI (`.github/workflows/ci.yml`) runs all of it on every push to `main` and
every pull request, behind one required `ci` check: lint (actionlint,
`shellcheck -S error`, ruff), the Python suite across 3.9/3.11/3.12/3.13 on
Linux and 3.13 on macOS, the node suites, the bash suites **on macOS** (they
test BSD-targeted scripts — `stat -f %m` returns nothing under GNU
coreutils), and a smoke job that runs the entry points with nothing mounted.

That last job is the one worth explaining. "An unmounted NAS must not look
like a finished job" is a safety rule, and a runner with no `/Volumes` is the
only place the offline path is actually reachable: the job asserts
`report.py` prints LIBRARY INCOMPLETE and PARTIAL, and that `next_title.py`
answers 2 (not mounted) or 3 (wait) — never 1, which is the stop condition and
would tell a blind driver the job is done.

Actions are pinned to commit SHAs rather than tags, the workflow's
`permissions` are read-only, and no job has access to a secret.

## Releasing

```bash
npm version patch     # 0.1.1 -> 0.1.2
```

That one command bumps `package.json`, commits as `SMLTR: Release v0.1.2`,
creates the annotated tag, and pushes the commit and the tag to `origin`.
There is no build, no publish, no deploy — **the tag is the release**. A
`preversion` hook byte-compiles every module first, so a release cannot be
tagged over a syntax error in the decision path. `package.json` exists only to
drive this and is `"private": true`; the version lives in `package.json` and
the tag and nowhere else.

See `.claude/skills/smeltr-release/SKILL.md`, including how to undo a bump.
