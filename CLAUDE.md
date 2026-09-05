# Smeltr — working notes

A local dashboard and ledger for a long-running 4K HEVC re-encode job. UHD
remuxes (60–100 Mb/s) are re-encoded with HandBrakeCLI, verified, and moved back
to a NAS — replacing an ~80 GB original with an ~30 GB file that keeps every
audio and subtitle track.

Python 3 stdlib + vanilla JS. **No runtime dependencies, no build step, no
CDN.** The only `devDependencies` are lint/format/type tooling (oxlint, oxfmt,
typescript, `@types/bun`, husky, lint-staged) — nothing the app imports or
serves, and `dependencies` is empty
(`test_repo_invariants.py::TypeCheckingIsNotABuildStep`). TypeScript is a
CHECKER here and never a compiler: `tsconfig.json` is `noEmit`, `web/*.js`
stays `.js` and is checked where it lies through `checkJs`, and only the bun
test suites are `.ts`. See *TypeScript — checked, never compiled*. **bun is the only JS runtime,
package manager and task runner** (`packageManager` pins it; a `preinstall`
guard turns `npm install`/`yarn`/`pnpm` away; `bun.lock` is the lockfile,
there is no `package-lock.json` and no `.npmrc`). No `node`, no `npm`,
anywhere — `test_repo_invariants.py::BunIsTheOnlyRunner` pins it.
Run `./smeltr report` or open <http://127.0.0.1:8787/>.

## THIS IS LIVE INFRASTRUCTURE — read before editing

A detached `autopilot.sh` on the staging drive is running unattended right now.
It encodes, judges, syncs, and **permanently deletes ~70 GB library originals**
on a `good` verdict. It calls this repo on every cycle.

**The folder is the boundary.** Everything the driver executes lives in
`pipeline/`; everything it does not lives in `dashboard/`, `web/` or `ops/`.

| File | Autopilot depends on it | Safe to edit freely |
|---|---|---|
| `pipeline/core.py` | **YES** — the verdict and queue logic | no |
| `pipeline/verdict.py` | **YES** — exit code decides sync-or-halt | no |
| `pipeline/next_title.py` | **YES** — picks what encodes next | no |
| `pipeline/crf.py` | **YES** — picks the CRF the next encode starts at | no |
| `pipeline/record.py` | **YES** — writes the ledger | no |
| `dashboard/server.py` | no — never imported by the decision path (but see below: it now spawns/kills encodes) | mostly |
| `dashboard/sysmon.py` | no — imported only by `server.py` | mostly |
| `dashboard/events.py` · `dashboard/notify.py` | no — imported only by `server.py` | **yes** |
| `dashboard/report.py` | no | **yes** |
| `web/*` | no — markup, CSS and JS for the page | **yes** |
| `ops/watchdog.sh` | no — relaunches the driver | mostly |

`pipeline/` must never import from `dashboard/`; the reverse is the allowed
direction. This used to be a hand-verified sentence — it is now a TRANSITIVE
import check in `tests/test_layering.py`. Worst case the page breaks and the
pipeline keeps running.

Repo layout:

```
smeltr              launcher — the only interface `.autopilot.sh` calls
                    (`next` · `verdict` · `record` · `crf`); `notify-test`
                    proves a notify.json before the first real event
pipeline/           DECISION PATH. pause the driver before editing
dashboard/          server, resource sampler, terminal report
web/                index.html · app.css · theme.js · app.js
ops/                watchdog.sh · com.smeltr.watchdog.plist
.husky/             pre-commit (lint) · commit-msg (subject) · pre-push (subjects + full suite) — installed by `bun install`
staging/            byte-for-byte mirrors of the live X9 scripts
tools/              one-off maintenance (`seed_ledger.py` · `render_favicon.sh`)
tests/              check_page.py · suites (run via `bun run test`); the bun
                    halves are `.ts`, the visual harness `.mts`
tsconfig.json       the type-check gate — `noEmit`, `checkJs` over `web/*.js`
ledger.jsonl        the irreplaceable record, beside the launcher
```

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
   title 30 s later. A dashboard start at CRF 24 sits outside the ladder
   maps (see *The band ladder* below): an out-of-band kill at 24 goes
   straight to `none-*`. Slow halves (parity gate, kill grace) run on
   daemon threads and report through `encode_note` in the state payload.
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
   render as a stalled arrival, never as an encodable folder — and since
   2026-09-01 the next server start adopts it (see *Orphan adoption*
   below). One pull at a
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

   **Orphan adoption (2026-09-01).** The pull child is `start_new_session`'d,
   so it survives a server restart — but the `_stage_worker` thread that
   waits on it and does the commit rename dies with the old process. On
   2026-08-31 that stranded a complete, verified 61 GB pull of Addams
   Family 2 as a stalled arrival until a human renamed it. `main()` now
   calls `_adopt_orphan_pulls()`: if any `.pull-<title>` folder exists, a
   daemon thread ticks `_sweep_orphans_once()` every 20 s until done. The
   commit evidence is the folder's CONTENTS, not a return code — a final
   `.mkv` with no `.partial` beside it IS the completed pull, because
   `.ssh-xfer.sh` renames the `.partial` only on a byte-count match — so a
   complete orphan is renamed into place; a `.partial`/empty/junk-only
   folder is a dead half-pull and gets the worker's delete-the-folder
   cleanup. The sweep defers (returns "come back later") while ANY
   `ssh-xfer.sh pull` is alive or `_stage_active` is set, so it can never
   touch a folder something is still writing; a rename failure (including
   a destination that already exists) keeps the pull and says "move it by
   hand" — never deleted. Pinned in `test_stage_queue.py::OrphanPulls`.

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

   **The switch must never refuse a click (fixed 2026-08-30).** It used to
   set `disabled` for the whole POST round-trip — and that round-trip is not
   short, because `/api/pause` answers with a freshly built state payload
   that stats the NAS roots over SMB (0.5–1.0 s). A click inside that window
   hit a disabled button, so it never reached `api()` and never even raised
   the "another action is still in flight" notice, which is the ONE outcome
   that notice exists to prevent: driving the real page, **six of ten rapid
   clicks vanished with no feedback of any kind**. Pause/resume is the
   control a person hammers, so that read as a broken toggle. The switch now
   never disables: `pauseState()` draws the user's UNSETTLED intent over the
   server's committed value so the flip lands on the click, and the LAST
   click wins — a click during a write is recorded and the in-flight call
   drains it when it lands, so two round-trips still never race but nothing
   is silently dropped (a round trip back to the starting value sends
   nothing at all). Intent is released the moment a write settles, so a
   denied LAN write snaps the switch back to the truth rather than leaving
   the optimistic flip standing. The queue tab's "next after resume" mark
   deliberately stays on the server's value: an unconfirmed intent may draw
   the control under the finger, never a claim about what the driver will
   do.

   **The control is ONE native `<button>` — pill and sentence inside it.**
   Two separate failures put it there. First, only the 36×20 pill was
   clickable: the label beside it is a SENTENCE ("will pause after this
   encode — *title* still finishes, syncs, and replaces its 90.35 GiB library
   original") and a person reads it and aims at it, but it was inert.
   Second, moving the handler onto a wrapping `<div>` fixed the mouse and NOT
   the iPad — **which is where this is actually watched**. iOS Safari only
   synthesises a click from a tap on natively interactive elements (or ones
   carrying `cursor:pointer`), so a listener on a plain div is a coin-toss
   across platforms. A `<button>` takes the event from a mouse, a finger, a
   pen and the keyboard everywhere, with no touch shims and no double-fire,
   and it is the accessible control for free (`role="switch"` +
   `aria-checked`, the sentence as its name; the pill is `aria-hidden`
   decoration, never a second focus stop). `.pauserow` carries the button
   reset and is `inline-flex` so it hugs pill+sentence instead of making the
   card's whole width a pipeline control; `touch-action:manipulation` drops
   the double-tap delay and `@media (pointer:coarse)` gives a finger a 44px
   target.

   **A synthetic `element.click()` cannot catch either of those** and two
   successive "fixes" shipped believing it had. `.click()` skips hit testing
   AND skips the platform's tap→click synthesis, so it passes on a control
   nothing can actually reach with a real input. Verify pointer-driven UI
   with `Input.dispatchMouseEvent` at real coordinates, `Input.dispatchTouchEvent`
   under `Emulation.setTouchEmulationEnabled`, and `document.elementFromPoint`
   — the way `tests/visual/shoot.mjs` already drives Chrome.
   `tests/test_pause_toggle.js` pins the structure (it IS a button, one
   handler, the CSS target rules); the pointer/touch runs are manual.

   **Pause/resume is the ONE write a network peer may make (2026-08-30).**
   The reason the toggle looked dead on the iPad was neither of the bugs
   above: `_writes_ok()` refused every POST from a network peer, so the
   switch flipped optimistically, took a 403 and snapped back. That gate is
   right for skip/reorder, encode start/abort and stage pulls — un-skipping
   re-arms a ~90 GB deletion and encode control spawns and kills HandBrake —
   but pause's worst case is the pipeline WAITING, which is the direction
   `core.paused()` already fails towards. `LAN_WRITE_ROUTES` is checked per
   request; `_writes_ok()` takes the route and its default
   `""` is deliberately NOT in the tuple, so a caller that forgets the route
   gets the STRICT answer and a new endpoint stays refused off-box until it
   is listed. `SMELTR_LAN_WRITES=1` still opens everything. The token still
   gates it all, so this is "any device you handed the URL to", not "anyone
   on the network". `tests/test_http_gates.py` pins each route on both sides.
   (Since 2026-08-31 `/api/driver/start` is the SECOND LAN-writable route —
   see 5.)
5. Driver start (2026-08-31): `POST /api/driver/start` LAUNCHES
   `.autopilot.sh` — the big play/pause toggle's "nothing is running" half,
   because a stopped driver previously had no dashboard control at all. It
   spawns the driver exactly the way the restart line below does (detached
   via `start_new_session`, cwd on the X9, appending to `.autopilot.log`),
   so a dashboard restart cannot kill it. Refuses while the driver is alive
   (pgrep), while ANY encode runs (a driver started beside a dashboard
   encode would pick and start a second one), or with no X9 mounted. First
   it clears a provably-stale `.autopilot.lock` (no live process — the
   watchdog's rule) and the pause flag (play means play; a leftover flag
   would leave the fresh driver waiting in 300 s ticks). It deliberately
   does NOT start the watchdog: a play-launched driver has no reboot
   recovery until `ops/watchdog.sh` is relaunched by hand. LAN-writable for
   the same reason as pause: the operator presses play from the iPad, and
   start's worst case is the pipeline running exactly as designed. The big
   toggle (`bigToggle` in `web/app.js`) has three states — start (driver
   dead), resume (paused; it shares `pauseSend` with the pause switch so
   the two controls cannot race), and pause-after-current — and its "next
   up" line is always the server's `next_up` pick, never re-derived
   client-side. A start in flight renders through the same
   unsettled-intent pattern as the switch (`driverWant`), so a refused
   start snaps back instead of lying.
6. Per-title start CRF (2026-09-01): `POST /api/queue/crf` writes a
   `{"crf": {title: int}}` map into `queue_overrides.json`, and **the DRIVER
   reads it** — `.autopilot.sh` asks `smeltr crf "$title"` for the start rung
   immediately before it spawns HandBrake, in place of a hard-coded rung.
   That is the whole point: the dashboard starts at most one encode by hand,
   so a picker only the dashboard honoured would be a lie on every row the
   driver starts, which is nearly all of them. It is the FIRST override the
   driver reads for anything other than *which* title runs.

   **The menu IS the ladder, and `core.CRF_CHOICES` is where it lives** —
   10·12·14·16·18·20·22, imported by `dashboard/server.py` rather than
   duplicated. It used to be 16/18/20/22/24 in `server.py` alone; 24 was a
   rung `.watch-encode.sh` maps from nowhere, so a blowup there was killed
   and never retried — a dead end wearing the costume of a choice. Now that
   core owns it, a value the page offers is one the driver will accept.

   **Consequence the operator gets, accepted:** the watcher cannot tell
   "started at 20 by hand" from "laddered up to 20", so the
   opposite-direction rule still applies — a hand-picked 20 that projects
   TOO SMALL exhausts straight to `none-too-small` (the ERROR state) instead
   of stepping back down. Picking a rung narrows the ladder to one direction.

   The ladder still OUTRANKS the plan: a `KILLED|` line in the watch log wins,
   because a retry after a measured, rejected projection is not something a
   choice made hours earlier should override. `smeltr crf` always prints one
   integer and exits 0 — a missing override, an unreadable file, an unknown
   title and a broken checkout all answer `core.CRF_DEFAULT` (14 since
   2026-09-03), because its stdout becomes `-q`
   and there is no failure here worth idling the CPU over. NOT in
   `LAN_WRITE_ROUTES`: it looks like a preference and is not. A CRF chosen too
   high produces an encode the verdict legitimately calls `good`, which syncs
   and replaces a ~90 GB original with a worse picture — the same class of
   consequence as un-skipping, not the same class as pausing.
   `tests/test_planned_crf.py` and `tests/test_crf_picker.js` pin it.

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

### The band ladder and the ERROR state (2026-08-31, operator's spec)

`.watch-encode.sh` (now mirrored in `staging/`, drift-checked like
`autopilot.sh`) kills a running encode whose PROJECTION lands outside the
**30–80% band** — both directions, checked **every 60 s tick** once the
projection opens at 5% progress (the dashboard strip's opening point), not
just at quarter checkpoints: Addams Family 2 ran its full 4.5 h to produce a
9.6% file the verdict then refused. Two honesty guards on the early
projection: a `CUR=0` stat is a glitch and skips the tick (killing a healthy
encode over a momentary unreadable stat deletes hours of work), and TWO
consecutive ticks must agree on the same violation before the kill fires.

On a kill the partial is deleted and the driver retries at the next rung —
**up 14→16→18→20→22 when too big, down 14→12→10 when too small**. A
violation OPPOSITE to the rung's own direction (too small at 16/18/20/22, too
big at 12/10) exhausts immediately — a projection that flips sides
between adjacent rungs would oscillate forever.

**The ladder pivots on `core.CRF_DEFAULT` and the two must move together**
(the pivot went 16 → 14 on 2026-09-03). The pivot is the one rung both arms
leave from; every other rung is one-directional. Moving the constant alone
would make the default a DOWN-ONLY rung, so the first too-big kill of a
default encode would exhaust to `none-too-big` with no rung left to try —
and a lower CRF makes a BIGGER file, so too-big is exactly the direction
the move to 14 makes more likely. Moving the pivot also means a hand-picked
16 that comes in too small now exhausts rather than stepping to 14, which is
the same "picking a rung narrows the ladder" rule applied one rung up. Exhaustion (`none-too-big` /
`none-too-small`) is the **ERROR state**: `.autopilot.sh` writes
`$X9/.error-<title>` and MOVES ON — never a halt, never a skip, never a
deletion. `core.error_marker()` puts `error`/`error_note` on the queue row;
`pick_next` passes it over (wait reason `errored`); the queue tab renders
the title red with a ❗ (hover for the note). The state ends when a human
deletes the marker file. Known consequence the operator accepted: clean
digital/animated sources that legitimately land under 30% (Kubo 23.7%,
Minions 17.2%) will now ladder DOWN and may end red at CRF 10 —
`tests/test_error_state.py` pins the mechanics.

### No gap between encodes — the operator's standing requirement (2026-08-31)

**The only sanctioned gap between one encode finishing and the next starting
is the dashboard's pause toggle.**

**The driver no longer halts on anything (2026-09-04).** `halt()` is gone
from `.autopilot.sh`; `error_out()` replaced every call site. A non-`good`
verdict (2/3/4 from `verdict.py`), a folder with no source file, a track
mismatch at the 120 s gate, and an unresolvable library original (roots
reachable, zero-or-multiple matches) all put THAT TITLE into the ERROR
state — `$X9/.error-<title>` marker, finished output left beside the source,
red ❗ row — and the same pass goes on to step 2 and starts the next encode.
`finished_folder()` passes over a folder whose marker exists, so the errored
folder (source + output, both kept) is never re-judged and never re-encoded;
`pick_next` already passed it over. The Little Mermaid halted at 04:05 on
2026-09-04 (`halt-decoder-errors`, 2 HEVC decoder errors) and idled the
encoder for five hours with nine staged titles waiting — the operator's rule
is that they check the error by hand while the queue keeps moving. Deletion
safety is untouched: only `good` still reaches `sync_async`. The watchdog's
`HALTED:` rule stays for old logs but can no longer fire.
`tests/test_error_state.py::DriverContract` pins that no `halt` remains and
that `finished_folder()` skips the marker.

Everything below exists because a NAS blip
at 05:11 on 2026-08-31 hit the exact second the judge block ran
`library_path_of()`, the empty find was treated as "needs a human", and the
HALT idled the CPU for 4h16m while an already-staged title sat unencoded.
Two changes, which must both stay:

- **`.autopilot.sh` DEFERS instead of halting when the library roots are
  unreachable.** Resolution now runs before judging; an empty result plus
  `library_roots_online()` false logs `DEFER` once, leaves the folder in
  place (the loop re-finds and retries every pass), and step 2 still starts
  the next encode. Judge/record/sync all happen when the roots return —
  nothing about deletion changes, it just happens later. The halt that
  remains fires only when the roots are provably reachable and the match is
  zero-or-multiple, which really is a human problem. `LIB_ROOTS` is now the
  one in-script root list (still one of the four hand-synced copies), and
  the stop condition refuses to fire while a deferred folder exists.
- **`next_title.py` picks before it reports blindness.** A staged title
  encodes from the X9 and needs nothing from the NAS, so `core.queue()`
  keeps a row whose root is offline IF the title is staged (size read from
  the staged copy — same bytes; an unstaged offline row still drops), and
  exit 2 is now only the answer when blind AND nothing staged is pickable.
  Precedence: paused (3, stating both facts when also blind — this reversed
  the old "offline beats paused") → pick (0) → offline (2) → waits (3) →
  stop (1). The CI smoke job's "2 or 3, never 0 or 1 with nothing mounted"
  still holds: no X9 means nothing staged.

`tests/test_pause.py` (driver contract + `OfflineRootKeepsStagedRows`) and
`tests/test_autopilot_helpers.sh` pin all of it.

`staging/autopilot.sh` in this repo tracks the live script for history.
**The X9 copy is what runs.** `tests/test_staging_in_sync.sh` fails on drift.

Deploy with **`ops/deploy-staging.sh`** (`--dry-run` first). It swaps each
script by atomic `mv`, never `cp`: a rename replaces the directory entry while
the running `bash` keeps reading its old inode, so the live driver finishes its
cycle on the old code and the next launch picks up the new one. An in-place
rewrite can garble the remaining commands of a running copy — including its
deletion steps — which is why the "never edit a running staging script" rule
exists and why a rename is the exception to it. Every swap leaves a
timestamped `.bak-` beside the original.

That drift is not hypothetical: the `next_reason()` fix committed in
`11b1c15` sat undeployed for five days while `test_staging_in_sync.sh` stayed
red, so the driver ran the racy version CLAUDE.md described as fixed.

### The watchdog, and why it may be blind

`ops/watchdog.sh` relaunches the driver when it is merely absent. It NEVER restarts
past an unreviewed `HALTED:` line — a rule that only old logs can trigger now
that the driver writes error markers instead of halting (2026-09-04).

**A LaunchAgent cannot read `/Volumes` without Full Disk Access, and the failure
is silent.** `[ -d ]` succeeds, `test -r` on a file inside returns true, and
every read comes back empty. Blind, `smeltr next` reports STOP CONDITION — the
job looks finished. The watchdog proves readability first and stands down
loudly if it cannot. Grant `/bin/bash` Full Disk Access for the LaunchAgent to
work, or run `ops/watchdog.sh --supervise` from a shell that can already see the
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

`bun run test` (`bun run --sequential --no-exit-on-error test:py 'test:sh:*'
'test:js:*'` — every suite is also its own `test:*` script; bun's built-in
`--sequential` runs them in order with a label on every line, and
`--no-exit-on-error` runs every suite before reporting the first failure's
exit code). They pin the log-matching and downscale gates, the
concurrency markers, the watchdog's corpse/finished boundary, the projection
band's boundaries and wording, the stage pull queue's hold-vs-drop split, and
repo/live drift. The projection and repaint-key suites run under `bun`
against the functions pulled straight out of `web/app.js`. Every bun suite is
TypeScript since 2026-09-05 (`tests/*.ts`, `tests/visual/shoot.mts`); bun runs
them directly, and `bun run lint:ts` is what type-checks them.

Two suites used to need the X9 mounted and now do not, because a suite that
skips is a suite nobody notices has stopped running:

- the concurrency guards are tested against the LIVE `.autopilot.sh` when the
  drive is there and against the tracked `staging/autopilot.sh` when it is
  not. `test_staging_in_sync.sh` is what keeps those two the same file, and
  it is still the one suite that genuinely cannot run off the drive.
- `test_verdict_calibration.py` reads `ledger.jsonl` when it exists — that is
  deliberate, and `test_flight_still_asks_for_a_human` is a DRIFT MONITOR: the
  baseline moves as encodes land, and the test fails on purpose if it moves far
  enough that a Flight-class result would auto-sync. `ledger.jsonl` is
  gitignored, so on a fresh clone it falls back to
  `tests/fixtures/ledger-calibration.jsonl` — the 22 rows the 2026-08-22
  recalibration was reasoned about, frozen. `FixtureIntegrity` replays the
  frozen rows on EVERY machine, so the fixture cannot rot unnoticed on the one
  box that has a live ledger.

Two suites landed 2026-09-01 with the per-title CRF picker:

- `test_planned_crf.py` — the override round-trip and the `smeltr crf`
  process contract. The one it exists for is
  `test_a_skip_does_not_drop_the_crf_map`: a two-argument `save_overrides`
  erasing every hand-picked CRF is silent and only shows up hours later as
  an encode at the wrong rung.
- `test_crf_picker.js` — drives the real `crfPicker`/`rowActions` out of
  `web/app.js`: the menu is the ladder and nothing else, `auto` sends
  `crf: null` rather than pinning the default, the encoding row gets no
  control, and there is exactly ONE CRF control per row.

  The pointer half is manual, the way the pause toggle's is, and was run:
  `document.elementFromPoint` at the cell centre lands on the `<select>`
  under both a fine and a coarse pointer, and a real `Input.dispatchKeyEvent`
  type-ahead on the live page wrote the override end to end. (`ArrowDown`
  does NOT work for this on macOS — it opens the popup instead of moving the
  value; type-ahead is what fires a genuine `change`.)

Three suites were added 2026-08-28 to cover the highest-stakes gaps:

- `test_verdict_exit_codes.py` — `verdict.py`'s word→exit-code mapping had NO
  test at all, and exit 0 is what authorises deleting a ~90 GB original. It
  reads the verdict words out of `core._verdict()` by AST, so a word added
  there fails this suite instead of silently inheriting the `return 2`
  fallthrough. It also pins that `HALT` is **dead** — defined, never read —
  so nobody edits it believing it changes behaviour.
- `test_layering.py` — turns "Verified: `verdict.py` does not load
  `server.py`" from a hand-checked claim into a **transitive** import check
  over the decision path. A two-hop `record → report → server` path is just
  as fatal and no eyeball catches it.
- `test_http_gates.py` — `_host_ok` / `_token_ok` / `_writes_ok` /
  `_is_private` had no coverage despite guarding a LAN-bound server whose
  POSTs spawn HandBrake and re-arm deletions. Includes the non-ASCII token
  that used to raise `TypeError` out of `compare_digest`.

### Seeing the charts — visual confirmation

Two halves, because each catches what the other cannot. The 7 d widening
passed every arithmetic test and still shipped a chart that was 92% empty
wash, and the fix for THAT shipped a 15 min window whose line rendered
dotted. Neither was visible in a number.

- **`tests/test_sysmon_render.js`** (in `bun run test` and CI) hands the real
  `drawMon()` a RECORDING 2D context and asserts the ops it emits: every one
  of the twelve stops draws a line across the full plot width, the wash
  appears if and only if the window overhangs the ring and is that overhang
  to the pixel, the no-data stretch rides 0 *and* carries its wash, a gap
  inside the history still breaks the path, gridlines stay at 3–16 per
  window, and no two tick labels collide. No browser, no dependency, no
  golden images — it runs wherever the other bun suites do.
- **`bun tests/visual/shoot.mjs`** (`bun run visual`) is the eyeball half
  and is NOT wired into CI. It seeds a synthetic 7 d ring
  (`tests/visual/seed_ring.py`) into a temp `SMELTR_DIR`, starts a
  THROWAWAY dashboard against it — the live ring beside the ledger is never
  touched — drives the Chrome already installed on this Mac over the
  DevTools Protocol with bun's built-in `WebSocket` (no package, no
  Playwright, nothing installed; it SKIPS LOUDLY with no Chrome), and
  screenshots all twelve stops in both themes, then montages them into one
  contact sheet per theme. `--depth-seconds N` seeds a PARTIAL ring, which
  is how the wash and the 0 baseline get on screen. It reads the stop count
  off the slider and the label off `#monSpanLbl` rather than page globals,
  because `web/app.js` is an IIFE and because a visual suite should see
  exactly what a person sees.

Known artifact, not a bug: the seeded ring ends at `now` and the throwaway
server's own sampler starts a few seconds later, so the shots show a small
real gap at the right edge.

`tests/test_repo_invariants.py` enforces the rules this file calls
non-negotiable and nothing previously checked: every encode path passes
`--all-audio`/`--all-subtitles` and nothing passes `--audio-lang-list`;
`dashboard/server.py` and `staging/autopilot.sh` build the same encode;
`LIBRARY_ROOTS` agrees with the roots `autopilot.sh` resolves (2 of the 4 hand-synced copies
live in this repo); the decision path imports neither `server` nor `sysmon`;
no runtime artifact carrying the auth token is in the index; unit-bearing
table headers still carry `class="unit"`; every colour token exists in both
themes; and the three CSP nonces are still there.

### TypeScript — checked, never compiled

Adopted 2026-09-05, in the one shape that costs the no-build posture nothing.

**`tsconfig.json` is `noEmit`, and that is the whole design.** `web/app.js`
and `web/theme.js` stay `.js` and are type-checked WHERE THEY LIE, through
`allowJs` + `checkJs`. `dashboard/server.py` still inlines the same bytes at
import, so the browser still receives one self-contained document and CSP
stays `default-src 'none'`. Rename `web/app.js` to `.ts` and something has to
strip the annotations before a browser can run it — either a generated file
committed beside the source, or a `bun build` inside `_asset()`. Both are the
build step this repo does not have, and both put a stale artifact between the
file a person edits and the file the page runs, which renders as a page
quietly one version behind a working HTTP 200.
`test_repo_invariants.py::TypeCheckingIsNotABuildStep` pins it: `noEmit` on,
no `outDir`, no `.ts` under `web/`, no tracked `.js.map` or `.d.ts`, and
`dependencies` empty.

**The bun suites ARE TypeScript** (`tests/*.ts`, `tests/visual/shoot.mts`).
They are the half nothing serves, so nothing has to strip anything: bun runs
`.ts` directly. The rename is runtime-neutral — they stay CommonJS-shaped
(`require`, `__dirname`), which bun honours in a `.ts` file, so the diff is
the extension plus type annotations and nothing else.
`moduleDetection: "force"` is what keeps each one its own scope; without it
they are global scripts and every `const fs` collides with the next file's.

What the conversion actually surfaced, all of it type-only:

- `monZoomEl.value` — `getElementById` answers `HTMLElement`, which has no
  `.value`. Annotated `HTMLInputElement` (index.html declares it an
  `<input type="range">`), and the assignment beside it now writes
  `String(monPos)` where it wrote a number. Runtime-identical: the DOM
  coerces, and the line under it already spelled `String(monPos)`.
- Trailing test-helper parameters that call sites omit (`check(name, cond,
  detail?)`, `node(tag, cls?, text?)`, `ok(cond, msg?)`, …). A `.js` file
  lets TS infer those as optional; a `.ts` file does not.
- `shoot.mts`'s CDP client got field declarations (`declare`, so they erase
  and the emitted class is unchanged) and a generic `send<T>`, where the
  caller states the reply shape the DevTools protocol promises. The single
  `as` cast inside it is that claim meeting the `unknown` a socket delivers;
  there is no `any` and no double cast anywhere in the repo.

The visual harness was run end to end after the rename — 24 shots, both
themes, all twelve stops — because `bun build --no-bundle` proves it parses
and nothing else does.

### Linting and formatting

`bun run lint` is the whole gate — `bun run verify` and CI's `lint` job run
exactly it, and the pre-commit hook runs the SAME checks over the staged
files only (`bun run lint:staged`, see *Git hooks*). It is
`bun run --sequential` (built into bun — no `npm-run-all`) over these scripts
in order, stopping at the first failure: `lint:js` (oxlint) · `lint:js:syntax`
(`bun build --no-bundle`, which parses and fails on a syntax error) ·
`lint:ts` (`tsc --noEmit`) · `lint:py` (`uvx ruff@<pinned>`) · `lint:py:syntax` (compileall) · `lint:sh`
(shellcheck) · `lint:sh:staging` (blocks at `-S error`, advisory above it) ·
`lint:ci` (actionlint over `.github/workflows/`) · `format:js:check`
(oxfmt) · `lint:page` (`tests/check_page.py`). Each is runnable alone. Output
is label-prefixed tool output only; there is no wrapper script and no
summary line. There is still no build step: `ruff` is reached through `uvx`,
`shellcheck` and `actionlint` are system tools (`brew install shellcheck
actionlint`; CI `go install`s the same actionlint version), `bun` is the
runtime, oxlint/oxfmt/tsc come from `bun install`. A missing tool now FAILS the
script rather than skipping — the old
`tests/lint.sh` skipped loudly, and that was replaced 2026-09-04 because a
gate with an optional half is a gate nobody can compare across machines.
Config is `ruff.toml`, `.oxlintrc.json`, `.oxfmtrc.json` + `.editorconfig`.

- **ruff** runs a deliberately TIGHT set — `F, E9, B, PLE`. The wide default
  flags 123 mostly-stylistic issues across the decision path, and a gate that
  is red on day one is a gate that gets ignored (which is exactly what
  happened to the staging-drift test). Widen it only in a commit that also
  fixes what it surfaces.
- **`target-version = "py39"`** is the floor, not a taste: `smeltr` resolves
  `${SMELTR_PYTHON:-python3}`, which on an unprepared Mac is stock 3.9, and the
  CI matrix runs it. `report.py` once put backslash escapes inside an f-string
  replacement field (PEP 701) and was a `SyntaxError` there; it was fixed, not
  declared away. Nothing may assume newer syntax or newer stdlib signatures —
  `zip(strict=)` is 3.10+ and is spelled as a bare `zip()` in `sysmon.py`.
- **shellcheck** gates `smeltr`, every hook in `.husky/`, and every `.sh`
  `git ls-files -co` can see outside `staging/` — tracked OR untracked, so a
  new script is linted before it is ever added, and a script in a new
  directory cannot be linted by CI and missed here (2026-09-05; the list used
  to be four hand-written globs). `-S warning`. `staging/*.sh` **blocks at
  `-S error` and is advisory above that** (the same line CI draws) — those
  are byte-for-byte mirrors of the live X9 scripts, so a finding must be
  fixed on the drive during a pause window and copied back. Editing the
  mirror alone manufactures the drift `test_staging_in_sync.sh` exists to
  catch.
- **actionlint** (`bun run lint:ci`) checks `.github/workflows/*.yml` — the
  expression syntax, the `runs-on` labels, the action inputs, and (through
  its shellcheck integration) every `run:` block as the shell it declares.
  It was CI-only until 2026-09-05, which meant a workflow edit was checked
  only by the workflow it broke.
- **`bun build --no-bundle web/*.js`** (`lint:js:syntax`) — the dashboard's
  JS had never been syntax-checked at all before 2026-09-04. bun has no
  `--check` flag; a no-bundle build parses every file and exits 1 on a
  syntax error, writing nothing (`--outfile /dev/null`). `lint:page` (`tests/check_page.py`)
  asserts the page assembles with no `__PLACEHOLDER__` left, because a
  missing asset would otherwise render as a blank screen behind a working
  HTTP 200.
- **tsc** (`bun run lint:ts`, `tsc --noEmit -p tsconfig.json`) is the type
  gate. Same TIGHT-set reasoning as ruff, and the numbers made the case: full
  `strict` reports 612 findings across `web/` and `tests/`, `noImplicitAny`
  alone 563 and `strictNullChecks` alone 160, and a gate that is red on day
  one is a gate that gets ignored. What is ON is everything that was already
  green: `alwaysStrict`, `noImplicitThis`, `useUnknownInCatchVariables`,
  `strictFunctionTypes`, `strictBindCallApply`, `noImplicitReturns`,
  `noFallthroughCasesInSwitch`, `allowUnreachableCode: false`,
  `allowUnusedLabels: false`. Widen it only in a commit that also fixes what
  it surfaces. `noUnusedLocals`/`noUnusedParameters` are deliberately OFF
  even though they are green — oxlint's `no-unused-vars` already owns that,
  with an `^_` ignore pattern tsc has no equivalent for, and two renditions
  of one gate drift the moment one is edited alone.
- **oxlint** (`bun run lint:js`, `oxlint --deny-warnings`) lints every
  `.js`/`.mjs`/`.ts`/`.mts` at its default `correctness` category — the same TIGHT-set
  reasoning as ruff. `.oxlintrc.json` allows unused `catch (e)` bindings and
  ternaries-as-statements, both of which `web/app.js` uses on purpose.
- **oxfmt** (`bun run format:js`, checked by `bun run format:js:check`) formats
  JS, CSS, JSON and YAML — 2026-09-04 was the "format the world" commit for
  those. `web/index.html` is EXCLUDED: it is a template, and the HTML
  formatter rewrote `<script nonce>__THEME_JS__</script>` with a stray `;`.
  Markdown, `staging/` and `tests/fixtures/` are excluded too. **The UI
  suites pull code out of `web/app.js` and `web/app.css` by string match**,
  so every anchor there is whitespace-tolerant (`var NAME\s*=`, the
  `DENSE` view in the Python suites); a new anchor must be too.
- **The Python formatter is configured but NOT adopted** (`bun run format:py`,
  checked by `bun run format:py:check`). `ruff format` rewrites 862
  lines across all 14 Python files, including every decision-path module, and
  it expands the compact dict literals this codebase deliberately keeps dense
  (`verdict.py`'s `json.dumps` goes 9 lines → 12). Adopting it is a single
  "format the world" commit that lands during a pause window, never mixed
  into a behaviour change. CI gates on `lint`, never on `format --check`.
- **`bun run format` / `bun run format:check` are the umbrellas** — each runs
  the JS half then the Python half (`format:js` + `format:py`, `format:js:check`
  + `format:py:check`). Because the Python half is not adopted, `format:check`
  is RED today; every gate (`lint`, `system-check`, the hooks, CI) runs
  `format:js:check` alone until the Python "format the world" commit lands,
  at which point they can widen to `format:check`.

`bun run release` now runs the **full suite** as its `preversion` gate, not
just `compileall`. A release therefore cannot be cut while the repo's
`staging/autopilot.sh` differs from what the X9 is actually running.

### Git hooks

`.husky/pre-commit`, `.husky/commit-msg`, `.husky/pre-push` and the rules
file they share, `.husky/commit-rules.sh`, are tracked; `bun install` runs
the `prepare` script (`husky`), which points `core.hooksPath` at `.husky/_`.
**Per clone** — `.git/hooks` is not versioned and `core.hooksPath` REPLACES it.
husky runs each hook with `sh -e`, so they are POSIX sh (no bashisms) and any
command allowed to fail carries `|| true`. All four are shellchecked by
`lint:sh` (the `.husky/[a-z]*` glob — `.husky/_` is husky's own).

- **pre-commit (~5 s)** refuses a staged runtime artifact (`token`, `url`,
  `server.log`, `ledger.jsonl`, …), refuses trailing whitespace or a conflict
  marker in a staged hunk (`git diff --cached --check` — Python and shell
  have no formatter, so this is the only thing enforcing `.editorconfig` on
  them; `staging/` and `tests/fixtures/` opt out in `.gitattributes` because
  they are byte-for-byte copies of something else), and then runs `bun run
  lint:staged`. The artifact check duplicates
  `test_repo_invariants.py::RuntimeArtifacts` on purpose: that test reads
  `git ls-files`, so a `git add -f token` only trips it once the commit
  already exists — this reads the INDEX and refuses first. Its diff filter
  is `d` (everything but a deletion): a rename or copy onto `token` shows as
  `R`/`C` and slipped past the old `AM`. The lint is **lint-staged**
  (`bun run lint:staged` = `lint-staged --no-stash
  --no-hide-partially-staged --relative`, config in `package.json` under
  `lint-staged`): the same tools and flags as the `lint:*` scripts, run
  only over the files in the commit, so a one-file commit is not held for
  a whole-tree shellcheck+actionlint+page-assembly pass. **Both flags are
  the point, not options.** lint-staged's default stashes unstaged work to
  lint the exact index content, and that stash round-trip is how a hook
  loses somebody's edits — so the checks read the **working-tree copy of
  each staged file**. `--no-stash` ALONE is not enough: it still checks
  out the index copy of every partially staged file and, when a task
  FAILS, does not put the unstaged half back — confirmed 2026-09-05, when
  one failing run wiped the unstaged edits on seven files and the
  `.git/lint-staged_unstaged.patch` it left behind was the only copy
  (`git apply --unidiff-zero` on it is the recovery). The per-glob commands are
  check-only (`oxfmt --check`, never `oxfmt`): the hook never writes and
  nothing is silently re-added. `web/*` and `dashboard/server.py` trigger
  the page-assembly check; a staged `web/*.js`, `tests/*.ts`,
  `tests/visual/*.mts` or `tsconfig.json` triggers the whole-program
  `tsc --noEmit`, because a type check has no per-file form — the program is
  the unit, the same way page assembly is;
  `.github/workflows/*.yml` triggers actionlint;
  `staging/*.sh` blocks at `-S error` and everything else at `-S warning`,
  the same line `lint:sh`/`lint:sh:staging` draw.
  `test_repo_invariants.py::LintStagedMirrorsLint` pins the parity — the
  ruff pin, `--deny-warnings`, the shellcheck levels, `--no-stash` — because
  two renditions of one gate drift the moment one is edited alone. The
  whole-tree `bun run lint` still runs at pre-push and in CI.
- **commit-msg (instant)** holds the subject to the `commit-format`
  rules — `SMLTR: ` + Capitalized verb, ≤72 chars, no trailing period, no
  conventional-commits prefix behind it, no AI-authorship trailer, `-`
  bullets. The rules are the `commits` job of `.github/workflows/pr.yml`
  transcribed into POSIX sh, and they exist locally because that job only
  ever sees a pull request: `bun run release` and a plain `git push` land on
  `main` with no PR, so a direct-to-main commit was checked nowhere.
  `fixup!`/`squash!` subjects pass here (git writes them for `--fixup` and
  autosquash consumes them) and are refused by pre-push, so one can never
  reach a remote. Merge subjects are exempt, as in pr.yml. Comment lines and
  everything below git's scissors line are ignored, so a `commit -v` diff is
  never read as body text. `test_repo_invariants.py::CommitRules` runs the
  real function against a dozen messages.
- **pre-push (~20 s)** first re-checks every commit not yet on ANY remote
  against the same rules (`--amend` on an old commit, a rebase and
  `--no-verify` all bypass commit-msg), then runs `bun run verify` (lint →
  every suite → build) — the same gate `bun run release` uses as
  `preversion`, so a push and a release are held to one standard. `bun run
  system-check` is `format:js:check` → `lint` → `test`, the check-only
  sequence with no build step and no write. A push that only DELETES refs
  skips both (an all-zero local sha on stdin): there is no tree to test, and
  running the suite there would only be a way to refuse a branch cleanup.

None of them WRITES to the NAS, the staging drive, or the running driver.
pre-push does READ both: `build` calls `build_state()`, which stats the
library roots over SMB, and `test:sh:drift` compares `staging/` against the
X9 — so a stalled mount stalls the push rather than the suite lying about
it. Bypass with `--no-verify`, `HUSKY=0`, or `SMELTR_SKIP_HOOKS=1`.

### CI

`.github/workflows/ci.yml`, on every push to `main` and every PR. Five jobs
behind one required `ci` check:

| Job | Runner | What it proves |
|---|---|---|
| `lint` | ubuntu | `bun run lint` unrolled — one step per member (`lint:js` · `lint:ts` … `lint:page`) |
| `python` | ubuntu 3.9/3.11/3.12/3.13 + macOS 3.13 | `lint:py:syntax` + `test:py` |
| `browser-logic` | ubuntu | one step per `test:js:*` script |
| `shell` | **macOS** | one step per `test:sh:*` script, with `ffmpeg` installed |
| `smoke` | ubuntu | `bun run smoke` unrolled — the entry points with NOTHING mounted |

**Every check is `bun run <script>` — nothing is called directly (2026-09-05).**
A step that ran `shellcheck …` or `python -m unittest …` by hand was a second
rendition of the gate the hooks run, and two renditions drift the moment one
is edited alone (`test_crf_picker.js` was in the local runner for four days
before CI ran it). The lint job is `bun run lint`'s sequence unrolled one
member per step, in the same order, so the Actions UI names the tool that
went red; the test jobs unroll `bun run test` the same way. The only commands
called directly are the ones that INSTALL a tool the scripts need (`bun
install`, `pip install uv`, `go install actionlint`, `brew install ffmpeg`).
`bun run smoke` (`smoke:report` · `smoke:next` · `build`, backed by
`tests/smoke_offline.sh`) is deliberately NOT part of `bun run test`: on the
Mac the roots are mounted and its assertions are false by construction.
`test_repo_invariants.py::CiRunsThroughBun` pins all of it — every `run:` is
`bun run` or a tool install, every member of `lint` and every leaf `test:*`
script is a step somewhere, and no step is a bare `bun run lint`/`test`.

Three choices worth not undoing:

- **The bash suites run on macOS, not ubuntu.** `watchdog.sh`'s `mtime_age()`
  uses BSD `stat -f %m`, which returns nothing under GNU coreutils — the
  corpse/finished boundary would be tested against behaviour that never runs
  in production.
- **The Python floor is 3.9**, which is stock macOS `python3`. `smeltr`
  resolves `${SMELTR_PYTHON:-python3}`, so a clone on an unprepared Mac gets
  it. A 3.12-only f-string in `report.py` had already made `smeltr report` a
  `SyntaxError` there; the matrix is what caught it.
- **`smoke` runs with no NAS and no staging drive**, which is the one
  environment that reaches the offline paths: it asserts `report.py` says
  LIBRARY INCOMPLETE and PARTIAL, and that `next_title.py` answers 2 or 3 —
  never 0 or 1, because 1 is the stop condition and would tell a blind driver
  the job is finished.

Actions are pinned to commit SHAs, not tags; Dependabot proposes the bumps
monthly. `permissions: contents: read` at the top level and nothing widens it.
No runner ever sees the NAS, the staging drive, or a secret.

### Pausing the driver — the exact procedure

Before touching anything in `pipeline/`, or
any `.sh` on the staging drive, pause the driver. Learned the hard way on
2026-08-21 (and extended 2026-08-23) — four traps in this, all confirmed live:

1. **Stop the watchdog first.** A running `ops/watchdog.sh --supervise`
   relaunches an absent driver and will race the kill below. `pgrep -fl "watchdog.sh
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
(from the repo — `ops/watchdog.sh` does not exist on the X9):

```bash
cd "/Volumes/Crucial X9/4K Movies" && nohup ./.autopilot.sh >> .autopilot.log 2>&1 &
nohup ~/Developer/git/smeltr/ops/watchdog.sh --supervise >/dev/null 2>&1 &
```

## Library roots — kept in sync BY HAND in four places

The library spans three roots: `Vhagar/Media/4K Movies`,
`Vermithor/Media/4K Movies`, `Vermithor/Media/4K Family Movies` (added
2026-08-21; identical `letter/title/file` layout). The list is duplicated in
**four** places and nothing enforces agreement — a root added to three of four
fails in whichever path was missed:

1. `pipeline/core.py` `LIBRARY_ROOTS` — queue, offline detection, transfer observation
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

## Queue overrides (skip + drag-to-reorder + per-title CRF)

`queue_overrides.json` lives beside the ledger:
`{"skip": [titles], "priority": [titles], "crf": {title: int}}` — exact
folder names, matched case-insensitively. Written ONLY by
`core.save_overrides()` (atomic `os.replace`, so the driver can never see a
torn file); the dashboard's `POST /api/queue/skip`, `/api/queue/order` and
`/api/queue/crf` are the only callers, and they accept only titles the queue
itself just reported — never a path.

**`save_overrides(skip, priority, crf=None)` carries the CRF map forward when
it is omitted.** Three callers predate the map and pass two arguments; making
them pass a third they do not care about is how a skip click silently resets
every hand-picked CRF, with no error and no sign until an encode starts at
16. The read-modify-write is safe because every caller runs under the
server's `_ov_lock`. A CRF that is not in `core.CRF_CHOICES` is DROPPED on
read, never clamped: the value becomes `-q`, and guessing a neighbouring rung
on the operator's behalf is a multi-hour encode nobody asked for.

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
- `core.planned_crf()` answers the start rung, and `core.queue()` puts it on
  every row as `crf` + `crf_set` (the value AND whether a human chose it —
  the page renders a hand-picked 16 differently from a default 16, and only
  the flag can tell them apart once the number is the same). Three consumers
  read the one function: the queue cell, the dashboard's start button, and
  `smeltr crf`, which is what `.autopilot.sh` asks.
- **The CRF picker lives in the CRF COLUMN, on every unskipped row that is
  not encoding** — that is what "select the CRF on anything that has not
  started yet" means, and the column already existed to show the number. Its
  `auto` option CLEARS the override rather than pinning the current default
  as a choice; with no way back a row is opted out of a default that later
  moves and nothing on screen says so. The encoding row is read-only text
  (`-q` is fixed for the next several hours), skipped rows still claim no
  number. `crf`/`crf_set` are in `qShape()` — they decide which option the
  cell renders, so they are shape, not a live number.
- There is no dedicated skip column any more: skip/restore,
  "start encode" (only on the single `ready` row, only while `can_start`, and
  it starts at the row's planned CRF — the hover actions carry NO second CRF
  control, which was a rung that applied only to a hand-started encode
  sitting one column away from a number that meant something else),
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
  I/O · all volumes MiB/s), 1 Hz samples, **7 d of history**, and a zoom
  slider that **snaps to twelve named stops** — 15 min · 30 min · 1 h · 2 h
  · 4 h · 6 h · 12 h · 24 h · 2 d · 3 d · 5 d · 7 d (widened 2026-08-29 from
  a continuous log curve over 1 h–24 h; a continuous curve handed out
  windows like "3.4 h" that two readings of the chart could not be compared
  across). `dashboard/sysmon.py` (imported ONLY by `server.py` — the
  decision path never loads it) samples on a daemon thread and persists to
  `sysmon.ring` beside the ledger: 16-byte magic header + 604800 slots of
  `<I7f` keyed `ts % 604800`, so a restart costs seconds of gap, not the
  chart. **Widening SLOTS re-keys every slot index**, so `MAGIC` went to
  `SMLTRMON2` and the old 24 h file is recreated empty on first launch — a
  one-time loss, never a misread. History reaches the page as raw ring
  records (`GET /api/sysmon/history`, DataView-parsed), **sized to the
  visible window**: the whole ring is ~19 MB and the default stop draws a
  day, so the page sends `&span=<seconds>` and re-fetches only when a wider
  stop asks for more than it holds — and says "loading history…" rather
  than "history since" while that is in flight, because an unfetched window
  is not a claim about the sampler. `history_bytes()` emits contiguous
  RUNS, not one slice per slot (per-slot slicing built 604800 short-lived
  objects per request). Live samples arrive as 1 s
  `event: mon` SSE frames interleaved with the 2 s state frames on the same
  connection — `build_state()` still runs at 2 s, never 1 Hz. The charts
  live entirely OUTSIDE `paint()` and its repaint keys; the redraw clock is
  a client interval, so the window keeps sliding and the legend goes to em
  dashes when the sampler dies (the server never re-sends an unadvanced
  sample). Honesty rules, pinned in `tests/test_sysmon.py` +
  `tests/test_sysmon_ui.js`: a missing second is a line GAP, never an
  interpolation; unreadable metrics are NaN → absent line and `—`, never 0;
  decimation is min/max band + mean line so a 1 s spike survives a 7 d
  window (the header says "shade = min–max · line = mean (smoothed at wide
  zooms)"); the mean line is lightly smoothed (2026-09-01) ONLY where a
  bucket aggregates 4+ samples — at the narrow stops the band collapses to
  ~1 px of 16% alpha, the line is the only evidence on screen, and smoothing
  it redrew a measured 98 MiB/s burst at 33; the smoothing window is
  symmetric and stops at a gap or the edge, so the line's tip and the points
  beside a gap are always the raw bucket mean; the window
  before the oldest held sample rides a flat 0 baseline UNDER a `--nodata`
  wash, a `--nodata-bd` rule at the boundary and an inline "no samples
  before HH:MM" — **the zeros and the disclosure ship together or neither
  is honest**. The line is drawn so a window wider than the ring reads as
  one chart rather than a stub hanging off the right edge; the wash, rule
  and words are what stop those zeros reading as an idle machine. It is NOT
  `--skel`: the boot skeleton is deliberately near-invisible against
  `--panel`, and reusing it left the dark theme showing a flat 0 line with
  no visible disclaimer at all. A gap INSIDE the history still breaks the
  path — that is a fact about the machine, where the baseline is a fact
  about how long we have been recording;
  past 24 h the axis ticks and both stamps carry a weekday, because a bare
  "06:00" names three different mornings at the 7 d stop; there are never
  MORE buckets than the window has seconds (a 15 min window on a 1200 px
  canvas has 900 samples for 1156 columns, and one-bucket-per-column drew a
  fully-sampled 1 Hz series as a DOTTED line — the samples were not
  missing, the screen simply had more resolution than the data);
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
  The measured rate is **held across no-growth frames** (SMB stat caching
  makes many frames report no growth; the rate/ETA caption used to blink in
  and out every few seconds) but the hold is **bounded at 60 s of no growth**
  (`RATE_HOLD_SECONDS`) — a `.partial` whose mtime keeps refreshing while its
  size does not never trips `stalled`, and republishing a minutes-old rate
  with a frozen countdown is the lie the bound prevents. Growth samples are
  dt-weighted into the smoothed rate. Captions print the rate as **MiB/s**
  (binary, matching the GiB operands and the sysmon axis — `/1e6` MB/s read
  4.9% high) and mark the ETA `~` like every other extrapolation. An empty
  queue while a push is still travelling says so instead of "nothing left"
  (the driver's stop condition refuses to fire mid-sync; the page may not
  claim what the driver won't). `tests/test_transfers.py` pins it.
- `server._arrivals()` marks staged folders holding only a replenish
  `.partial` as *arriving* — present on disk but not yet encodable.
- **The Events tab (2026-09-01)** is the driver/watcher timeline, built for
  debugging outages like 2026-09-01's (a dead hand-run sync halted the driver
  while the band ladder killed the encode — six log reads to reconstruct;
  the tab shows the whole chain at a glance). `dashboard/events.py` (imported
  ONLY by `server.py`) parses the tail of `.autopilot.log` — stamped lines
  become events, indented/unstamped lines fold into the event above as
  `detail` — plus the watch logs' `KILLED`/`COMPLETE` verdicts, newest first,
  served at `GET /api/events`. The 2 s SSE frames carry only `events_rev`
  (log mtimes+sizes); the page refetches at most once per rev move and only
  while the tab is open, so the timeline never rides the state payload.
  Honesty rules (each one a 2026-09-01 data-critic finding, all pinned in
  `tests/test_events.py`):
  - a `KILLED`/`FAILED` line that is the FINAL line of its watch log is
    stamped from the file's mtime (the watcher writes it and exits) but
    rendered as an ESTIMATE — `~` prefix, minutes precision, tooltip naming
    the inference; any earlier line of a re-used log shows "—", never a
    guess.
  - `FAILED` (a HandBrake that DIED — the most likely reason the tab is
    open) is an event, and ladder exhaustion (`next: none-*`) is kind
    `exhausted`, distinct from a routine `killed` retry.
  - watcher/sync "GB" strings are relabelled **GiB** (`.watch-encode.sh` and
    `.sync-to-library.sh` divide by 1073741824) so the tab can never be read
    as disagreeing with History about the size of the original being deleted.
  - runs of the identical event COLLAPSE to one row with "× N" (the driver
    logs its wait reason every 300 s; a 21 h pause must not evict the whole
    real history from the row budget).
  - truncation is disclosed: the payload carries `total`, the tab label says
    "(250 of 266)" and a footer names where the older history lives.
  - a failed fetch renders "could not load — retrying" and retries on the
    next frame, never a permanent spinner; an empty tab distinguishes "the
    X9 is offline" (from `x9_online`) from "no events recorded yet".
  `QUARTER` progress noise never becomes an event. Severity chips reuse the
  page's three colour tokens; an unmapped kind renders as a plain pill,
  never an error.
- **Notifications (2026-09-05)** — `dashboard/notify.py`, imported ONLY by
  `server.py`, started from `main()` when `notify.json` sits beside the
  ledger (gitignored, holds the Slack webhook and the Gmail app password;
  `./smeltr notify-test` sends one message per channel). Email via stdlib
  `smtplib` + STARTTLS, Slack via `urllib` to an incoming webhook — no
  dependency, no decision-path import (`test_layering.py` lists it in
  `DASHBOARD_ONLY`). It is an OBSERVER of `events.events()`, the same parser
  the Events tab reads, so a message can never disagree with the tab. One
  daemon thread ticks when `events.rev()` moves (5 s poll), diffs the
  snapshot against a persisted seen-set (`notify.cursor`), classifies what
  is new, and delivers: `complete` → "Encode done · verdict pending"
  (COMPLETE is HandBrake exiting, not a result — Little Mermaid's only
  message would otherwise have been a green tick on a halt-decoder-errors
  verdict), `failed` → failed, driver `LADDER` → Ladder UP/DOWN, driver
  `ERROR` (and an old-log `HALTED:`) → error state, `CYCLE COMPLETE` →
  "Original deleted". **The ladder anchors on the driver's stamped `LADDER`
  line, not the watcher's `KILLED` line**: the driver truncates the watch
  log within 30 s of a kill, so a 5 s poll could miss it, whereas the
  LADDER line is permanent. Both rungs and the direction follow from the
  target rung alone (one rung per retry; every rung above `CRF_DEFAULT` is
  an up-rung); the kill's projection is added only when the watcher line
  is still there. Watcher `killed`/`exhausted` are therefore silent on
  their own; the kill's projection is REMEMBERED from the earlier tick
  that saw it and added to the ladder message when known. The deletion
  message is built from the LEDGER row (`record` writes it before the
  sync; subject carries the original's GiB), never from the folded shell
  output under the SYNC line — syncs are backgrounded and unstamped, so
  that text can be ANOTHER title's size guard, and it carried the ssh
  user@host and, once, an unrelated "autopilot already running" line
  inside a deletion notice; with no ledger row it says so. Also notified:
  `SYNC FAILED`/`SYNC ABORTED` (the deletion path's failures were silent
  while its successes were loud) and `STOP CONDITION`. The seen-set is a
  UNION bounded at 4000 keys, never a replacement — events.py degrades per
  source, so one unreadable watch log leaves a non-empty snapshot and a
  replaced set would re-send every deletion notice a tick later. A landed
  channel is persisted BEFORE the next channel is tried (`smeltr restart`
  SIGKILLs inside a 15 s SMTP handshake), a failed channel backs off
  60 s → 1 h, and `notify.json` must be 0600 or it is refused — it holds
  the app password. `start()` never raises: it runs in `main()` above the
  socket bind. A watcher stamp that
  the Events tab draws as `~` (mtime-derived) renders as `~` here too, and
  every clock carries a weekday and date because a retried message can be
  a day late. A burst keeps the most severe ten (deleted > error > failed
  > ladder > done) and the summary names what it dropped. Rules pinned in
  `tests/test_notify.py`: the FIRST snapshot is a baseline, never a backlog;
  an EMPTY snapshot (X9 unmounted) is neither a baseline nor a diff, or the
  whole tail would arrive as "new" on mount; identity omits `ts` for
  watcher lines because the KILLED line's mtime stamp flips to None once
  the log is re-used; a failed send stays pending (persisted, so a restart
  cannot lose the deletion notice), each channel is ticked off separately
  so the half that landed is never resent, and pending older than 24 h is
  dropped with a `server.log` line; a burst above 10 sends 10 plus one
  summary. Never in `LAN_WRITE_ROUTES` territory: there is no endpoint, the
  config is a file only this Mac can write.
- **A push renders on the History tab ONLY** (operator's call 2026-09-01): a
  recorded title has left the queue, and the synthetic "transferring" row the
  Queue tab used to draw up top read as work still waiting to encode. The
  Queue tab keeps its *arrival* bars (replenish/stage pulls landing on the
  X9); History draws the push as the full-width row under the ledger row.
  Transfers therefore sit in the LEDGER repaint key only.
- Transfer bars have swung wrong in BOTH directions. Transfers were once
  omitted from `paint()`'s repaint key entirely, and the
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

The page is four real files in `web/`, read once at import by
`dashboard/server.py` and inlined into nonce'd `<style>`/`<script>` blocks:

| File | What |
|---|---|
| `web/index.html` | markup — header, stat cards, live card, tabs, `#uiNotice`, `__SCOPE__` footer |
| `web/app.css` | `:root` dark tokens **start here**, `:root[data-theme="light"]` overrides, typography, panels, `.proj` strip, tables, `prefers-reduced-motion`, ≤700px |
| `web/theme.js` | the pre-paint theme block — must stay in `<head>` |
| `web/app.js` | `renderAlert` `renderStats` `renderLive`+`projBlock`/`updateProj` `renderQueue`+`wireDrag` `renderLedger` `paint()` repaint keys, SSE wiring, the `mon` charts |
| `web/favicon.svg` | the tab icon: a hot ingot on the panel tile, in the page's own colours written out as literals. THE drawing; edit this one |
| `web/favicon.png` · `web/apple-touch-icon.png` | rendered FROM the SVG by `tools/render_favicon.sh` (headless Chrome, nothing installed). Re-run it after editing the SVG and commit all three together |

Until 2026-08-28 all of this was one 2383-line `_PAGE` r-string inside
`server.py`, which is why the UI suites still pull functions out by
brace-matching. They now read `web/app.js` instead of a Python string, and
`bun build --no-bundle` covers the files for the first time.

**This is still not a build step.** The files are inlined at import, so the
browser receives one self-contained document and CSP stays `default-src
'none'`. Adding a `<link>` or `<script src>` would break it.

**The favicon is the one thing the page fetches (2026-09-01).** Three
`<link>` tags in `<head>` point at `/favicon.ico`, `/favicon.svg` and
`/apple-touch-icon.png`, served from the `ICONS` table in `server.py`.
Safari (the iPad) cannot use an SVG favicon, so the SVG is rendered to a
32 px PNG that ships inside a 22-byte ICO wrapper (`_ico()`, built at
import) and to an opaque 180 px PNG for the iOS home screen (iOS paints
black under transparent pixels, then applies its own corner mask). Two
deliberate choices: the icon routes answer BEFORE the token gate, beside
`/healthz`, because browsers probe `/favicon.ico` and
`/apple-touch-icon.png` on their own with no query string (bookmarking,
the start page, add-to-home-screen) and a 403 there is a blank icon on
exactly the surfaces an icon is for; it is a closed list, so a neighbouring
path stays 403. And CSP `img-src` is `'self'`, not `'none'`, because
Firefox applies `img-src` to the favicon fetch; the page still names no
other image. `tests/test_favicon.py` pins the routes, the gate's closed
list, the sizes, the corner alpha of each PNG, and that an ingot actually
got drawn (a Chrome that failed to load the SVG screenshots a blank page,
which is still a valid PNG of the right size).

`_asset()` raises `SystemExit` when a file is missing rather than serving a
page with no stylesheet — a blank screen behind HTTP 200 is the failure the
boot skeleton exists to prevent.

### Verdict thresholds — recalibrated 2026-08-22

Only `good` syncs and deletes (`verdict.py`: `good` → 0; everything else → 2 or
3, and the driver halts on both). So every threshold below is the line between
an unattended deletion and a human being asked to look.

| | Before | After | Why |
|---|---|---|---|
| plausibility floor | `OUTLIER_FLOOR_RAW = 12.0`, tested on the **raw** ratio | `OUTLIER_FLOOR_NORM = 6.0`, tested on the **normalised** ratio | see below |
| the same floor, **raised 2026-08-30** | `OUTLIER_FLOOR_NORM = 6.0` — a plausibility question | `15.0` — a policy question | Flight was judged too small to keep; see *The floor is a policy line now* |
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

### The floor is a policy line now — raised to 15.0 on 2026-08-30

**A relative threshold drifts, and this one drifted onto the case it was
guarding.** `below_base` is `cmp_ratio < median × 0.40`, so the line falls as
the median falls. By 24 ledger rows the median had reached 31.6058% and the
line 12.6423% — and Flight sits at 12.6426%. It crossed from `suspect` to
`good` by **+0.0003 percentage points**, with no threshold edited by anyone.
`test_anchor_rows_keep_their_verdict` is what caught it; that is the drift
monitor doing exactly its job.

**The operator's call was that Flight should never have been that small.** A
90.6% reduction is not a result this job wants to keep, so the fix is NOT to
lower `OUTLIER_FACTOR` to re-cover it — that would have re-armed the same
drifting rule. The absolute floor was raised instead, from 6.0 to 15.0, and
its meaning changed with it: 6.0 asked *is this physically possible for 4K at
CRF 16*, 15.0 asks *is this a reduction we are willing to make unattended*.
An absolute floor cannot drift when the median moves.

**15.0 is bracketed on both sides, and the test says so.** Above Flight
(12.64% normalised), below Croods (17.05%) — the thinnest output this library
has produced that IS wanted. Anything at or above 17.0 starts halting good
work, and a gate that halts routinely gets waved through.
`test_floor_brackets_the_one_result_this_job_rejected` pins both ends.

**Replaying all 23 measured rows moves exactly one verdict**: Flight, `good` →
`suspect`. Oldboy stays `thin`; nothing else changes. Flight itself is already
synced and its original already deleted — this is about what happens next, and
the row is the operator's to revisit.

**The floor is the binding rule today, and that is itself pinned.** 15.0 sits
above the relative line (12.64%), so the relative check decides nothing until
the median reaches 37.5%. `test_the_floor_is_what_binds_today` fails when that
stops being true, because the boundary tests around it would otherwise be
asserting a line that no longer decides anything — which is how the old
`test_just_above_the_threshold_is_good` came to pass while testing nothing.

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

Network scope (`dashboard/server.py` config block, top of file):

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
   holds `core` in memory — a `pipeline/core.py` edit is invisible until restart.

**Colours are tokens, never hex literals.** Both themes are token sets with the
same names; a hex written anywhere below `:root` is a colour the light theme
cannot reach, which is exactly how the page stayed half-dark before. If you add
a colour, add it to both `:root` blocks.

**Every clock on the page is 12-hour** (operator's pick 2026-09-03): the
History stamp, the Events timeline, the live card's start line, the monitor's
axis and stamps, and the footer. `clock12()` in `web/app.js` REWRITES the
`HH:MM[:SS]` inside a string and never re-parses the string as a date —
`finished_at`, `generated_at`, an event `ts` and HandBrake's own header line
are all local wall-clock carrying no offset, so handing one to `Date()` lets a
browser running in another zone shift every row (the +4h that once moved the
monitor's axis labels). `smeltr report` is a terminal view and keeps 24-hour.

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

`bun run release patch|minor|major` is the entire release: it bumps
`package.json`, commits as `SMLTR: Release vX.Y.Z`, creates the annotated tag,
and pushes the commit and tag to `origin`. No build, no publish, no deploy —
the tag is the release. The `release` script is `bun pm version -m "SMLTR:
Release v%s"`: bun does NOT read `.npmrc`'s `message`, so a bare `bun pm
version` would commit as `v0.1.9` with no prefix. `package.json`, `bun.lock`,
and `.github/` are outside the decision path and are safe to edit while the
driver runs.

Commit messages follow the `commit-format` skill. PR titles and bodies
follow `pr-format` — five required sections, and `## What changed` copies
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
