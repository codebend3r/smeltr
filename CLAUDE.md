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
| `server.py` | no — never imported by the decision path (but see below: it now spawns/kills encodes) | mostly |
| `report.py` | no | **yes** |

Verified: `verdict.py` does not load `server.py`. Worst case the page breaks
and the pipeline keeps running.

`server.py` is NOT a pure observer any more. Two escalations, in order:

1. Skip/reorder (2026-08-21): `POST /api/queue/skip|order` write
   `queue_overrides.json`, which the decision path READS. Steers only *which
   title encodes or stages next*, never the verdict, sync, or a deletion. A
   broken or deleted overrides file degrades to stock bitrate order.
2. Encode control (2026-08-22): `POST /api/encode/start` SPAWNS HandBrakeCLI
   (per-title CRF from a fixed 16/18/20/22/24 menu) and `POST /api/encode/abort`
   KILLS one, deletes its partial, and writes a skip. The encode-control block
   in `server.py` deliberately mirrors `.autopilot.sh start_encode()` — same
   flags, same 120 s track-parity gate (kill + delete on mismatch), same
   handoff to `.watch-encode.sh` so the ladder/auto-kill still apply. The two
   are SEPARATE IMPLEMENTATIONS of one procedure: a change to either must be
   mirrored in the other. What `server.py` still cannot do: judge, record,
   sync, or delete anything in the library. Start refuses while
   `autopilot.sh` is alive (pgrep, not the lock file) or any encode runs;
   abort's skip-write is what stops a running driver relaunching the same
   title 30 s later. CRF 22/24 sit OUTSIDE the ladder (`.watch-encode.sh`
   maps only 16→18→20): a blowup at 22/24 is still auto-killed but nothing
   restarts it. Slow halves (parity gate, kill grace) run on daemon threads
   and report through `encode_note` in the state payload.
3. Stage-on-demand (2026-08-22): `POST /api/stage/start` SPAWNS an
   `.ssh-xfer.sh pull` of one library title onto the staging drive (the
   hover "stage" button on library-only rows). It mirrors the per-title pull
   in `.replenish-queue.sh` — same free-space gate (source + in-flight pulls
   + 10 GiB margin), same delete-the-folder cleanup on failure — with ONE
   deliberate deviation: **the pull lands in a hidden `.pull-<title>` folder
   and is renamed into place only when the byte count checks out.** The
   replenisher may pull into a visible folder ONLY because the driver calls
   it synchronously — it structurally cannot be at the `next_title` step
   while its own pull is in flight. A dashboard pull is asynchronous, and a
   visible folder holding no source `.mkv` is picked by `next_title.py` and
   HALTS the driver (`no source file`, exit 2 — confirmed in a sandbox).
   Hidden means invisible to `next_title.py`, `core.staged_folders()`, and
   the replenisher's `find`; a leftover from a crashed server can only ever
   render as a stalled arrival, never as an encodable folder. One pull at a
   time (in-process flag AND `pgrep ssh-xfer.sh pull`, so the guard survives
   a server restart); refused while the replenisher is mid-run (its lock +
   pgrep — a lock with no live process is reported as STALE, with the rmdir
   to run, since it silently starves the replenisher too). `_arrivals()`
   tracks growth like `_transfers()`: >120 s without growth renders
   `stalled`, never a bar. Failures report through `encode_note` (a `bad`
   note resists `ok` overwrites for 15 min — it is often the only record);
   log in `.pull-<slug>.log`. A completed pull is NEVER deleted over a
   rename failure — the note says where the file is.

### Pausing the driver — the exact procedure

Before touching `core.py` / `verdict.py` / `next_title.py` / `record.py`, or
any `.sh` on the staging drive, pause the driver. Learned the hard way on
2026-08-21 — three traps in this, all confirmed live:

1. **`pkill -f autopilot.sh` misses the detached process.** Find the pid with
   `pgrep -f autopilot.sh` and signal it directly.
2. **Plain `kill` (SIGTERM) does not stop it.** Bash defers the signal while
   waiting on a child, and its `trap 'rmdir $LOCK' EXIT INT TERM` removes the
   lock and then **continues the loop** — now unlocked. Use `kill -9 <pid>`,
   then remove the lock yourself:

   ```bash
   kill -9 "$(pgrep -f autopilot.sh)"
   rm -rf "/Volumes/Crucial X9/4K Movies/.autopilot.lock"
   ```

3. **Killing the driver does not kill its children.** A running HandBrake
   encode, an in-flight `.sync-to-library.sh` push, and a `.replenish-queue.sh`
   pull all survive as orphans, keep logging into `.autopilot.log`, and finish
   their own verify/cleanup safely. That is fine — but it means:
   - **Never edit a staging-drive script while an instance of it is running**
     (bash reads scripts by byte offset; an in-place edit can garble the
     remaining commands of the running copy — including its deletion steps).
   - **Never restart the driver while a finished staging folder still exists
     or a sync is mid-flight.** `core.record()` blindly appends — no duplicate
     guard — so the restarted driver re-finds the finished folder by disk
     scan, re-judges it, and writes a second ledger row (double-counted
     reclaim). Wait for `SYNCED:` in the log / the staging folder to vanish.

Restart it afterwards, exactly as its header documents:

```bash
cd "/Volumes/Crucial X9/4K Movies" && nohup ./.autopilot.sh >> .autopilot.log 2>&1 &
```

## Library roots — kept in sync BY HAND in four places

The library spans three roots: `Vhagar/Media/4K Movies`,
`Vermithor/Media/4K Movies`, `Vermithor/Media/4K Family Movies` (added
2026-08-21; identical `letter/title/file` layout). The list is duplicated in
**four** places and nothing enforces agreement — a root added to three of four
fails in whichever path was missed:

1. `core.py` `LIBRARY_ROOTS` — queue, offline detection, transfer observation
2. `.scan-bitrates.sh` (X9) — the `find` roots that feed the bitrate index
3. `.autopilot.sh` `library_path_of()` (X9) — resolves the library original for
   `record --source-path`; uses the same exactly-one `find` match as sync.
   Never reintroduce the first-letter bucket guess: "A Bug's Life (1998)"
   files under B, "1917 (2019)" under `#`, and a wrong guess halts the driver
   after a multi-hour encode
4. `.sync-to-library.sh` `LIBS` (X9) — requires exactly one library match or
   aborts

`.replenish-queue.sh` and `.ssh-xfer.sh` are root-agnostic (index paths and
prefix mapping) and need no edit when a root is added.

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
- A skipped title stays **in place** in the queue — faded, title struck
  through, rank "—", with a restore button. A skip that vanished (or sank out
  of view) would read as "finished".
- There is no dedicated skip column any more: skip/restore, the CRF picker +
  "start encode" (only on the single `ready` row, only while `can_start`),
  "stage" (only on library-only rows: not staged, not arriving), and abort
  (only on the encoding row) all live in the title cell as hover
  actions — revealed on `:hover`/`:focus-within`, always visible on coarse
  pointers, because the LAN phones have no hover. The `ready` row is computed
  server-side by `_mark_ready()`, which mirrors `next_title.py`'s pick (NOT
  simply rank 1 — library-only rows outrank staged ones constantly).

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

## Live telemetry — how the dashboard observes without steering

- `core.live_encodes()` keys HandBrake logs and staging lookups by
  **basename** — the autopilot passes full `-i`/`-o` paths, and raw paths once
  nulled the live sizes and `folder`, hiding the encoding badge and defeating
  the skip guard's title match. Keep it basename-keyed.
- `server._transfers()` reports a push back to the NAS by statting the
  destination's growing `<name>.partial` (`.ssh-xfer.sh` renames only on a
  byte-count match, so the `.partial` IS the transfer). Destination resolved
  by the same exactly-one bucket match as sync — never a first-letter guess.
  A `.partial` that stops growing for >120 s reports `stalled`, never
  progress; `done > total` means a stale leftover from an older attempt.
- `server._arrivals()` marks staged folders holding only a replenish
  `.partial` as *arriving* — present on disk but not yet encodable.
- Both tabs render transfer bars, and `paint()`'s repaint key must cover them
  (queue AND ledger variants). Transfers were once omitted from the key and
  the row painted a single still frame at ~0 bytes for a whole 45-minute push.

## Editing the look and feel

Everything visual is one string, `_PAGE`, in `server.py` (starts ~line 1217;
the bind/token/allowlist config, GET/POST handlers, encode control, and stage
control sit above it). Map as of the stage-on-demand change:

| Lines | What |
|---|---|
| 1225–1252 | `:root` dark design tokens — colours, radius, motion accents. **Start here.** |
| 1253–1274 | `:root[data-theme="light"]` — the light overrides, same token names |
| 1275–1391 | base typography, header, stat cards, panels, live card, molten bar, tabs |
| 1392–1497 | tables, scroll-reveal scrollbar, pin/skip/NAS/transfer marks, `.minibar` (+ red `.minibar.pull`), `th.unit`, grips |
| 1498–1546 | `prefers-reduced-motion` + responsive ≤700px (pinned title column, `.cut`) |
| 1548–1560 | pre-paint theme script (runs in `<head>`) |
| 1561–1588 | markup (incl. Reset-order button, `#uiNotice`, `__SCOPE__` footer) |
| 1647–1670 | `notice()` + `api()` POST helper — the page's only writes |
| 1671–2242 | `renderAlert` `renderStats` `renderLive` `renderQueue`+`wireDrag` `renderLedger` |
| 2243–2284 | `paint()` — repaint keys; MUST cover transfers on both tabs |
| 2285–2405 | SSE wiring, Reset-order, theme toggle, seam-blanking + scrolling-class scripts |

Network scope (`server.py` config block, top of file):

- The launcher exports `SMELTR_BIND=lan`, resolved at startup to this machine's
  **private** LAN IPv4 (`_is_private()` — RFC1918/RFC6598 only). A VPN/tunnel/
  public address, or `lan` with no network, **fails closed to loopback** with a
  stderr line: the token crosses the wire in cleartext and must never guard a
  public listener. `SMELTR_BIND=""` is treated as unset, never `0.0.0.0`.
- Off loopback the server binds the LAN IP **and** loopback (so `127.0.0.1`
  URLs keep working), forces the token on (`REQUIRE_TOKEN`), and adds the LAN
  IP, `<hostname>`, `.local`, `.lan` to the Host allowlist (lowercased,
  trailing-dot tolerant). `free_port()` requires the port free on **both**
  addresses so a loopback squatter can't shadow us.
- **The token persists** in a `token` file (0600, gitignored) beside the ledger
  — a fresh mint per launch would 403 every bookmarked phone and permanently
  close its SSE stream. Delete the file to rotate.
- Writes (every POST: skip/reorder/encode/stage) are gated **per request** by peer address: loopback
  (you, on this Mac) always writes; a network peer is refused unless
  `SMELTR_LAN_WRITES=1`. Un-skipping re-arms a deletion, so read-only is the
  network default. `X-Smeltr` is CSRF protection against browsers, not against
  devices holding the URL — the token and this peer gate are what stop them.
- `Handler.timeout = 15` keeps a slow/idle pre-auth peer from pinning a thread.
  The footer's `__SCOPE__` states the actual exposure (`html.escape`d).

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
3. **`./smeltr restart`** after editing, then hard-reload. The server process
   holds `core` in memory — a `core.py` edit is invisible until restart.

**Colours are tokens, never hex literals.** Both themes are token sets with the
same names; a hex written anywhere below `:root` is a colour the light theme
cannot reach, which is exactly how the page stayed half-dark before. If you add
a colour, add it to both `:root` blocks.

**Units survive CSS.** `th{text-transform:uppercase}` turns Mb/s into MB/S — an
8× lie on the column whose unit confusion already corrupted the old state file.
Any header carrying a unit gets `class="unit"` (`th.unit{text-transform:none}`).

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
mismatched row sets, a verdict that reassured on an implausible result, and a
transfer bar frozen at 0% for the length of the entire push.

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
- **A transfer that is not moving is "stalled", never a progress bar.** Log
  tails and leftover `.partial`s are not proof of life.

`~/.claude/skills/*` and `~/.claude/agents/smeltr-*.md` are **symlinks into this
repo** — editing them edits tracked files.
