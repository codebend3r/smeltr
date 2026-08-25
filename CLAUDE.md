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

`server.py` is NOT a pure observer any more. Four escalations, in order:

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
   a server restart); held while the replenisher is mid-run (its lock +
   pgrep — a lock with no live process is reported as STALE, with the rmdir
   to run, since it silently starves the replenisher too). `_arrivals()`
   tracks growth like `_transfers()`: >120 s without growth renders
   `stalled`, never a bar. Failures report through `encode_note` (a `bad`
   note resists `ok` overwrites for 15 min — it is often the only record);
   log in `.pull-<slug>.log`. A completed pull is NEVER deleted over a
   rename failure — the note says where the file is. (Since 2026-08-23
   `next_title.py` also passes over a visible folder with no source `.mkv`
   and exits 3 — a wait, not a halt — so the hidden folder is now
   defence-in-depth rather than the only thing preventing that halt.)

   **The pull QUEUE (2026-08-23).** `POST /api/stage/start` no longer refuses
   a busy wire — it ENQUEUES. `_stage_queue` is an in-memory FIFO of titles;
   one `_stage_pump_loop` daemon thread is the only thing that starts a pull,
   ticking every `STAGE_PUMP_SECONDS` (20 s). Still exactly one transfer at a
   time. In memory ON PURPOSE: a restart drops the list and the rows offer
   "stage" again, rather than carrying a plan across the code change that
   prompted the restart. `POST /api/stage/cancel` removes a **pending**
   title; the running pull cannot be cancelled (there is no abort path for a
   transfer, and faking one strands a hidden folder).

   **Every gate is re-run at DISPATCH, not at click time** — a check made
   when the button was pressed is hours stale by the time the wire frees up.
   `_stage_candidate()` is shared by the click and the pump so the two cannot
   drift. The split that matters:
   - **Transient → HOLD the head and say why on the row** (wire busy, another
     puller, replenisher running, no room yet, NAS unreachable). Queued work
     is never silently dropped: the drive frees up as encodes sync, and a
     queue that empties itself during a mount blip is worse than one that
     waits.
   - **Permanent → drop exactly ONE title and continue** (left the queue,
     hand-skipped, no/ambiguous index source, or already staged — that last
     one is an `ok` note, not `bad`: the replenisher got there first, which
     is the outcome the click wanted). One dead title must not wedge the
     queue behind it.

   `_stage_wait["why"]` renders on the head row only. It deliberately carries
   **no volatile number** — free space moves every second as the encode
   writes, and a reason string that changed every frame would put the row's
   shape back in `paint()`'s repaint key and rebuild the table twice a
   second. The measured figure goes to `encode_note` once per transition.
   Lock order is `_stage_lock` → `_state_lock`; `_pump_once_locked()` takes a
   queue snapshot built OUTSIDE the lock because `build_state()` takes
   `_stage_lock` itself, and building it inside would deadlock the pump
   against every open page. `tests/test_stage_queue.py` pins all of it.
4. Pause-after-current (2026-08-23): `POST /api/pause` writes/removes a
   `pause` flag file beside the ledger (gitignored). While it exists
   `next_title.py` answers exit **3** — the driver's existing
   wait-and-recheck path, no `.autopilot.sh` structural change — so the
   RUNNING encode still finishes, records, syncs and **deletes its library
   original**; only the next encode is withheld. The live-card switch says
   exactly that. Resume is picked up within 300 s. A paused pipeline can
   never exit 0: pause is checked before the queue scan, so an empty queue
   while paused waits instead of claiming the stop condition.
   `core.paused()` fails CLOSED (an unreadable `SMELTR_DIR` reads as paused
   — waiting is the safe direction). `summary()` carries `paused` so
   `smeltr report` banners it too; the paused idle card yields to the
   louder facts first (`x9_online` false, then no driver process).

### The driver is CONCURRENT as of 2026-08-22

HandBrake is never idle waiting on I/O. One pass of the loop dispatches a
finished encode's record+sync to the background (`sync_async`) and starts the
next encode in the same pass, so the push to the NAS, the replenish pull, and
the next encode all overlap. Only ONE encode runs at a time.

What that breaks if you forget it:

- A running encode's output already matches `*2160p HEVC*.mkv`, so
  `finished_folder()` would hand a LIVE encode to `verdict.py`, which returns 4
  and halts. `encoding_this()` excludes it — matched with a literal `case` glob,
  never pgrep (folder names contain `(YYYY)` and pgrep reads the parens as a
  group).
- A backgrounded sync leaves its folder on disk until `.sync-to-library.sh`
  removes it last, so the loop would re-find and re-RECORD it 30 s later.
  `core.record()` appends with no duplicate guard, so that is a second ledger
  row and a double-counted reclaim. `.syncing-<slug>` holds the sync's pid;
  a marker whose pid is gone is STALE and cleared. `.recorded-<slug>` means
  record already ran, so an interrupted sync resumes without re-recording.
- `sync_in_flight()` logs to **stderr**. `finished_folder()` is read with
  `$(...)`, so anything on stdout is prepended to the folder name it returns.
- The stop-condition `exit 0` is blocked while `syncs_in_flight()` — that
  sync's replenish is what stages the next titles.

Deletion safety is unchanged. `.sync-to-library.sh` still copies, size-verifies,
ffprobe-verifies and track-verifies before removing a library original.

The CRF ladder now fires from the `next_title` path. It used to hang off
`finished_folder()`, but `.watch-encode.sh` deletes the partial when it
auto-kills a blowup, so there was no finished folder and the branch could never
run — the title simply restarted at the CRF that had just blown up.

`staging/autopilot.sh` in this repo tracks the live script for history.
**The X9 copy is what runs.** `tests/test_staging_in_sync.sh` fails on drift.

### The watchdog, and why it may be blind

`watchdog.sh` relaunches the driver when it is merely absent. It NEVER restarts
past an unreviewed `HALTED:` line — a halt is the thing standing between a bad
verdict and a deleted original.

**A LaunchAgent cannot read `/Volumes` without Full Disk Access, and the failure
is silent.** `[ -d ]` succeeds, `test -r` on a file inside returns true, and
every read comes back empty. Blind, `smeltr next` reports STOP CONDITION — the
job looks finished. The watchdog proves readability first and stands down
loudly if it cannot. Grant `/bin/bash` Full Disk Access for the LaunchAgent to
work, or run `watchdog.sh --supervise` from a shell that can already see the
drive (works immediately, does not survive a reboot).

**A running `--supervise` races any intentional driver stop** — it relaunches
the moment the driver is absent (twice on 2026-08-23; the lock let one
instance win, harmlessly, but a relaunch mid-script-edit would not be). Stop
the watchdog BEFORE stopping the driver on purpose; step 1 of the pause
procedure below.

**A stale `.autopilot.lock` used to defeat every restart path** (fixed
2026-08-25). The lock is a `mkdir` directory removed by the driver's
`trap ... EXIT INT TERM` — and `kill -9`, the *documented* way to stop the
driver, skips that trap. The leftover directory then makes every relaunch exit
"autopilot already running", so the watchdog logged `RESTART FAILED` forever
with nothing running. That cost six hours of downtime on 2026-08-25. The
restart block now clears a lock it can prove is stale (no `autopilot.sh`
process), serialised on `.watchdog-restart.claim` so two watchdogs — the
LaunchAgent and a `--supervise` loop tick independently — can never each start
a driver, which would double-record a finished encode against a ledger with no
duplicate guard. `RESTART FAILED` now logs the driver's last line, because the
bare message sent the investigation to the wrong place.

**Both causes have to be fixed together.** The stale lock only stranded the job
because the LaunchAgent watchdog was *also* blind (no Full Disk Access), so
nothing ever reached the restart. Check both: `launchctl list | grep smeltr`
proves it is loaded, which is NOT the same as it being able to see the drive —
`~/Library/Logs/smeltr/watchdog.err.log` showing `Operation not permitted` is
the tell.

**It sweeps the staging drive before it restarts anything** (2026-08-22). A
reboot mid-encode leaves an unfinalised `*2160p HEVC*.mkv`, which
`finished_folder()` matches as FINISHED and `verdict.py` then halts on — so a
naive relaunch just trades a dead pipeline for a halted one. `triage_outputs()`
sorts every staged output into `live` / `fresh` / `finished` / `corpse` /
`unknown` and deletes ONLY a corpse. A corpse needs all of: nothing holds it
open, no write for 120 s, the SOURCE beside it probes clean, and the output is
missing or >2% short against that source. Probing the source is what makes the
deletion safe — a drive too sick to probe the output cannot probe the source
either, so an I/O blip yields `unknown`, which refuses the restart and notifies
instead. `finished` is never touched: that is an interrupted sync, and
restarting is how it completes. Two corpse shapes exist and both are pinned in
the tests — no duration at all (the live Kubo case), and a truncated file whose
muxer wrote its duration up front and so probes "fine" but runs short.

### Tests

`bash tests/run-all.sh`. The bash suites skip cleanly when the X9 is not
mounted. They pin the log-matching and downscale gates, the concurrency
markers, the watchdog's corpse/finished boundary, the projection band's
boundaries and wording, the stage pull queue's hold-vs-drop split, and
repo/live drift. The projection and repaint-key suites run under `node`
against the functions pulled straight out of `_PAGE`, and skip cleanly where
`node` is absent.

### Pausing the driver — the exact procedure

Before touching `core.py` / `verdict.py` / `next_title.py` / `record.py`, or
any `.sh` on the staging drive, pause the driver. Learned the hard way on
2026-08-21 (and extended 2026-08-23) — four traps in this, all confirmed live:

1. **Stop the watchdog first.** A running `watchdog.sh --supervise` relaunches
   an absent driver and will race the kill below. `pgrep -fl "watchdog.sh
   --supervise"`, plain `kill` it (it has no trap games), and relaunch it at
   the end — it lives in the REPO, not on the X9.
2. **`pkill -f autopilot.sh` misses the detached process.** Find the pid with
   `pgrep -f autopilot.sh` and signal it directly.
3. **Plain `kill` (SIGTERM) does not stop it.** Bash defers the signal while
   waiting on a child, and its `trap 'rmdir $LOCK' EXIT INT TERM` removes the
   lock and then **continues the loop** — now unlocked. Use `kill -9 <pid>`,
   then remove the lock yourself:

   ```bash
   kill -9 "$(pgrep -f autopilot.sh)"
   rm -rf "/Volumes/Crucial X9/4K Movies/.autopilot.lock"
   ```

4. **Killing the driver does not kill its children.** A running HandBrake
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

Restart it afterwards, exactly as its header documents, then the watchdog
(from the repo — `./watchdog.sh` does not exist on the X9):

```bash
cd "/Volumes/Crucial X9/4K Movies" && nohup ./.autopilot.sh >> .autopilot.log 2>&1 &
nohup ~/Developer/git/smeltr/watchdog.sh --supervise >/dev/null 2>&1 &
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
- `next_title.py` never picks a skipped row. Exit **3** (not the stop
  condition) now covers three wait states — every staged candidate
  hand-skipped, paused from the dashboard, or a staged folder holding only a
  still-landing `.partial` — and `.autopilot.sh` waits 300 s, logging the
  actual reason via `next_reason()` instead of a hard-coded guess. The
  reason comes from the SAME invocation as the exit code: `next_title()`
  writes stderr to a reason file (out of the `$(...)` capture) and
  `next_reason()` just reads it — a second call re-ran the whole scan and
  could race to a reason that was no longer true. The file lives under
  `$TMPDIR`, never on the X9: a redirect that cannot open returns 1 without
  running `smeltr next`, and 1 is the stop condition — a reason file on an
  X9 gone read-only would have logged "job finished" on a drive that still
  reads. If even the probe fails it degrades to `/dev/null`: reason lost,
  exit code intact.
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
  server-side by `_mark_ready()`, which calls the same `core.pick_next()`
  the driver's `next_title.py` calls — ONE pick, agreement by construction,
  where two mirrored renditions once had to be edited in lockstep (NOT
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

- **The resource monitor (2026-08-25)** is a "This Mac" card between the live
  card and the tabs: three canvas charts (Utilization %, Network MiB/s, Disk
  I/O · all volumes MiB/s), 1 Hz samples, 24 h of history, a log-scale
  1 h–24 h zoom slider. `sysmon.py` (imported ONLY by `server.py` — the
  decision path never loads it) samples on a daemon thread and persists to
  `sysmon.ring` beside the ledger: 16-byte magic header + 86400 slots of
  `<I7f` keyed `ts % 86400`, so a restart costs seconds of gap, not the
  chart. History reaches the page as raw ring records
  (`GET /api/sysmon/history`, DataView-parsed); live samples as 1 s
  `event: mon` SSE frames interleaved with the 2 s state frames on the same
  connection — `build_state()` still runs at 2 s, never 1 Hz. The charts
  live entirely OUTSIDE `paint()` and its repaint keys; the redraw clock is
  a client interval, so the window keeps sliding and the legend goes to em
  dashes when the sampler dies (the server never re-sends an unadvanced
  sample). Honesty rules, pinned in `tests/test_sysmon.py` +
  `tests/test_sysmon_ui.js`: a missing second is a line GAP, never an
  interpolation; unreadable metrics are NaN → absent line and `—`, never 0;
  decimation is min/max band + mean line so a 1 s spike survives a 24 h
  window (the header says "shade = min–max · line = mean"); the window
  before the oldest held sample is washed with `--skel` and captioned
  "history since HH:MM" so an empty ring cannot read as an idle machine;
  throughput axes have a hard 1 MiB/s floor (background chatter must not
  autoscale into a mountain range) and sub-MiB values print as KiB/s so a
  live trickle never rounds to 0. Chart series colours are the `--ch-*`
  tokens (both themes, CVD-validated); the canvas resolves them at draw
  time via getComputedStyle.

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
- Both tabs render transfer bars, and this has swung wrong in BOTH directions.
  Transfers were once omitted from `paint()`'s repaint key entirely, and the
  row painted a single still frame at ~0 bytes for a whole 45-minute push.
  Putting the raw byte counts IN the key fixed that and broke the other edge:
  the key then changed on every 2 s SSE frame, so the whole table was torn
  down and rebuilt twice a second — `#pane>table{animation:fadein}` replayed,
  scroll position reset, and the page visibly blinked for the length of every
  transfer.
- **The rule (2026-08-23): the key carries SHAPE, never a live number.**
  `qShape()`/`xShape()` are the key's row projections and hold only what
  decides which nodes exist — including the booleans derived from the byte
  counts (`arriving_bytes!=null`, `stalled`), because those change the row's
  structure. The counts themselves reach the DOM through `updateProgress()`,
  which `paint()` runs on EVERY frame (rebuilt or not — an armed confirm or
  an active drag must not freeze a transfer). `renderQueue`/`renderLedger`
  drop an empty `progSlot()`; `progWrite()` fills it, rebuilding the slot's
  children only when the shape changes, so the fill keeps its width
  transition. Same in-place discipline as the live card, for the same reason.
  `tests/test_repaint_key.js` pins both edges: growing bytes must NOT move
  the key, shape changes MUST.

## Editing the look and feel

Everything visual is one string, `_PAGE`, in `server.py` (starts ~line 1217;
the bind/token/allowlist config, GET/POST handlers, encode control, and stage
control sit above it). Map as of the stage-on-demand change:

| Lines | What |
|---|---|
| 1288–1317 | `:root` dark design tokens — colours, radius, motion accents, `--band`. **Start here.** |
| 1318–1345 | `:root[data-theme="light"]` — the light overrides, same token names |
| 1346–1438 | base typography, header, stat cards, panels, live card, molten bar, tabs |
| 1439–1470 | `.proj` size-projection strip — head, 0–100% scale, target zone, marker, legend |
| 1471–1591 | tables, scroll-reveal scrollbar, pin/skip/NAS/transfer marks, `.minibar`, `th.unit`, grips |
| 1592–1640 | `prefers-reduced-motion` + responsive ≤700px (pinned title column, `.cut`) |
| 1642–1653 | pre-paint theme script (runs in `<head>`) |
| 1655–1682 | markup (incl. Reset-order button, `#uiNotice`, `__SCOPE__` footer) |
| 1741–1764 | `notice()` + `api()` POST helper — the page's only writes |
| 1765–2407 | `renderAlert` `renderStats` `renderLive`+`projBand`/`projBlock`/`updateProj` `renderQueue`+`wireDrag` `renderLedger` |
| 2408–2450 | `paint()` — repaint keys; MUST cover transfers on both tabs |
| 2451–2681 | SSE wiring, Reset-order, theme toggle, seam-blanking + scrolling-class scripts |

Line numbers drift on every edit. Re-derive them with a `grep -n` on the
anchors above rather than trusting the table after a few changes.

### Verdict thresholds — recalibrated 2026-08-22

Only `good` syncs and deletes (`verdict.py`: `good` → 0; everything else → 2 or
3, and the driver halts on both). So every threshold below is the line between
an unattended deletion and a human being asked to look.

| | Before | After | Why |
|---|---|---|---|
| plausibility floor | `OUTLIER_FLOOR_RAW = 12.0`, tested on the **raw** ratio | `OUTLIER_FLOOR_NORM = 6.0`, tested on the **normalised** ratio | see below |
| relative outlier | `OUTLIER_FACTOR = 0.45` → 15.8% | `0.40` → 14.0% | keeps the human check on the thinnest encodes |
| not worth doing | `no-saving` at 85% | `no-saving` at 80% | matches the 30–80% band the dashboard draws |

**The floor was applied to the wrong ratio.** It fired exactly once, on Flight
(2012) at 9.4% — wrongly. That ledger row carries `VERIFIED by SSIM against the
cropped original: 0.9931 @45:00 and 0.9945 @10:00`. Flight auto-crops
3840x2160 → 3840x1600 and so discards 26% of its rows; per pixel *actually
encoded* it keeps 12.6%, not 9.4%. A plausibility floor asks a question about
the pixels that were encoded, so it belongs on the normalised ratio — which is
what the relative check beside it already compares. 12% also sat above what
this library legitimately produces: Flight is the thinnest output ever made
here, 8.35 Mb/s for 4K from a 2K DI upscale with no true 4K detail to spend
bits on. 6.0 normalised is a little under half of that.

**Flight-class encodes still halt, deliberately.** 12.6% normalised is under
the 14.0% relative line. That was a decision, not an oversight — it is the
smallest output this job has ever produced, against a ~92 GB original, and the
SSIM check that cleared it was worth having. Lowering `OUTLIER_FACTOR` to 0.30
would auto-sync it.

**Replaying all 12 measured ledger rows through the new rules changes no
verdict** — `tests/test_verdict_calibration.py` pins that, plus every boundary
and the fact that `OUTLIER_FLOOR_RAW` no longer exists.

**The baseline is contaminated and now says so.** Most ledger rows predate
geometry capture, so `history_ratios(normalised=True)` cannot crop-adjust them
and returns their raw figure. Comparing an adjusted encode against a
partly-unadjusted baseline flatters the outlier, so the `suspect` note
discloses it rather than implying the two are like for like. The `caveat`
variable that had been dead since it was written now carries this.

**The distribution is bimodal, so treat a single median with suspicion.**
Grain-heavy 1989–2003 film lands 33–71%; clean digital and animated sources
land 9–25%. A flat percentage floor is partly a source-age proxy, not a defect
test. The structural checks — duration, track parity, geometry, decoder errors
— are the real net; size is a weak last signal.

### The size-projection strip (2026-08-22)

The live card carries a `.proj` block: projected final size, that size as a
percentage of the source, and a 0–100%-of-source scale with the **30–80%
target band** ticked and a marker where this encode is heading. `projBlock()`
builds the DOM once, `updateProj()` writes into it in place with the rest of
the live card.

**Colour and headline come from `e.verdict` — the server's verdict, the same
one the pipeline acts on. Never re-derive it in the browser.** The first
version did exactly that, with its own copy of the thresholds, and disagreed
with `core._verdict()` in three ranges. Two were dangerous:

- A `downscale` — output frame narrower than source, the ONE state where the
  original must survive — scored 45%, landed mid-band, and rendered GREEN with
  "IN THE TARGET BAND · frees 39 GiB", while the real warning sat below it in
  grey prose. `renderLive`'s loud-class test had also omitted `downscale`, so
  the only true warning on the card rendered unstyled. Both fixed.
- Flight (2012) at 9.4% rendered RED "IMPLAUSIBLY SMALL". That row is in the
  ledger with a recorded SSIM of 0.9931/0.9945 against the cropped original.
  The abort button is one hover away on that row.

One threshold table, one verdict, one colour. `PROJ_CLASS` maps every code
`core._verdict()` can return (`good` `thin` `suspect` `no-saving` `blowup`
`downscale` `unknown`); a code missing from it renders unstyled, so the test
suite asserts the map is total.

**The 30–80% band is the user's target, not a defect threshold**, so it renders
as an uncoloured position on the scale and a plain sentence — never a severity.
4 of the first 12 completed encodes landed under 30% and every one was good;
the split is by *source type*, not defect: grain-heavy 1989–2003 film lands
33–71%, clean digital and animated sources land 9–25%. Kubo (2016 stop-motion)
projected 23.7% and the server called it `good`. Below-band therefore reads
"normal for a clean digital source".

Other rules the strip has to keep:

- The band ticks are an **outline, never a fill**. A filled zone on a pill
  track 40px under the progress bar — where filled *means* value — read as
  "we're about halfway", especially before a marker exists.
- Judged on `ratio_pct` (raw bytes, what fills the drive). `norm_ratio_pct` is
  cited beside it when `crop_factor > 1.01`, because that is what explains a
  low number. A `downscale` gets no band position at all: the pixel count
  changed, so the byte ratio is not comparable.
- Both polarities are shown — `23.7% kept, of source` and `76.3% smaller` —
  because the History tab and `smeltr report` both column on SHRINK, and the
  strip's headline is the complement.
- Estimates carry `~` and whole GiB (`gibApprox`), matching the stat cards and
  the History table. The projected *size* keeps two decimals; the extrapolated
  *saving* does not.
- `.proj-ratio span{text-transform:none}` — the caption carries GiB, and the
  surrounding uppercase rendered it GIB, one line under a correct one.
- Under 5% progress the server sends `null` and the strip says the estimate
  opens at 5%. A known size with an unknown source size is a *different* gap
  and says so; the `aria-label` must agree with the visible text.
- `Projected`, `Of source` and `Source` were removed from the `.kv` grid when
  this landed: one number, one place, one precision.

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

Three adversarial agents live in `agents/` and are symlinked into
`~/.claude/agents/`: `smeltr-code-critic` (code), `smeltr-data-critic` (the
numbers a human reads before authorising a deletion), and
`smeltr-encode-efficiency` (is the encode actually optimizing — projected
output smaller than the original, and inside the 30–80% target band). A newly
added agent is not selectable until the next session; the registry loads at
startup. Use the data critic after changing anything the dashboard displays — it has caught mislabelled units, totals computed over
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

The skills in `.claude/skills/` are **project-scoped**: Claude Code loads them
only while the working directory is inside this repo, and they have no presence
in `~/.claude/skills`. The review agents are the exception:
`~/.claude/agents/smeltr-*.md` are still **symlinks into `agents/` in this
repo**, so editing them edits tracked files.
