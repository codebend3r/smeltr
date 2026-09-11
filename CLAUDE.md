# CLAUDE.md

Operating rules for this repo.

## What Smeltr is

- Local dashboard and ledger for a long-running 4K HEVC re-encode job: UHD remuxes are re-encoded with HandBrakeCLI, verified, and moved back to the NAS with every audio and subtitle track kept.
- Python 3 stdlib plus vanilla JS. No runtime dependencies, no build step, no CDN. TypeScript is a checker only, never a compiler.
- bun is the only JS runtime, package manager, and task runner. No node, no npm. Every operator command is a `bun run` script wrapping the `./smeltr` launcher.
- The decision-path subcommands (`next`, `verdict`, `record`, `crf`, `encoder`) are deliberately not bun scripts. They are the driver's interface, and a stray `record` would double-count a ledger row.

## Workflow

- Do not commit anything until I tell you to. Finishing a change is not permission to commit it.
- Do not push anything until I tell you to. Once I have told you to commit on a branch that already tracks a remote, push it in the same step — don't ask again.
- Do not merge anything until I tell you to.
- Do not create a PR until I tell you to.

## Branching

- Do not create a branch until I tell you to.
- Branch names are flat. Never put a branch in a folder — no `feature/`, `fix/`, `bug/`, or any other prefix folder, and no slashes anywhere in the name.
- Branch names are kebab-case and 1 to 5 words, describing what the branch is for: `fixing-broken-tests`, `refactoring-component-x`, `skills-cleanup`. See the `git-branch-naming` skill.

## Live infrastructure

- A detached `.autopilot.sh` on the staging drive (X9) runs unattended and calls this repo every cycle. It can permanently delete library originals on a `good` verdict.
- `pipeline/` is the decision path and must be paused before editing. `dashboard/`, `web/`, and `ops/` are safe to edit and never imported by the pipeline. A test enforces the import direction.
- The X9 copy of each staging script is what runs. `staging/` holds byte-for-byte mirrors and a drift test fails when they differ. Deploy with `ops/deploy-staging.sh`, which swaps by atomic rename.
- Never edit a shell script while a copy is running. Bash reads by byte offset and an in-place edit garbles the running copy.
- The exact pause procedure: stop the watchdog first, find the driver with `pgrep -f autopilot.sh`, `kill -9` it, remove the lock directory by hand, then restart both afterwards.

## Dashboard escalations (what server.py can now do)

- Skip and reorder write `queue_overrides.json`, which steers only which title runs next, never a verdict or deletion.
- Encode start and abort spawn or kill HandBrakeCLI. This block mirrors the driver's `start_encode()` and the two must be kept in sync by hand.
- Stage-on-demand pulls a library title into a hidden `.pull-<title>` folder and renames it only when bytes match. Pulls are queued in memory, one at a time, with every gate re-run at dispatch.
- Orphan pulls from a server restart are adopted on the next start. A complete pull is renamed into place. A dead half-pull is cleaned up. Nothing complete is ever deleted.
- Pause-after-current writes a flag file. The running encode finishes and records. Only the next start is withheld. The pause control is one native button that never disables a click.
- Driver start launches `.autopilot.sh` from the dashboard. It refuses while the driver is alive, any encode runs, or no X9 is mounted.
- Per-title CRF and encoder pickers write override files the driver actually reads. The two endpoints clear each other so the row settles in one round trip.
- Pause, driver start, and stage pulls are the only routes a LAN peer may write. The public door behind Tailscale Funnel gets only pause and driver start, sign-in only.

## Encoders, ladders, and verdicts

- The hybrid encoder: VideoToolbox (`vt_h265_10bit`) is the global default at CQ 70 since 2026-09-06. That number is the operator's and never moves without their say. x265 rungs are CRF 10 through 22.
- All menus live in `core.ENCODER_CHOICES`. VT uses Apple's reversed CQ scale where higher means bigger. The band watcher inverts the ladder per encoder.
- The band ladder kills an encode projecting outside 10 to 70 percent of source, after two agreeing ticks, and retries at the next rung. Every rung steps both ways. The starting quality does not matter.
- Past the last rung the encode finishes rather than being killed. Finality is per direction, so a mid-ladder rung that exhausted downward still ladders up.
- The watcher renames the partial out of the `*.mkv` glob before killing, so the driver can never judge a corpse mid-kill. An unfinished output is a retry, never an error, until three unexplained mid-write deaths.
- A finished encode is done whatever the verdict. It is recorded kept, marked `.done-`, and moved to `complete/`. The verdict only decides whether a `good` encode syncs and deletes.
- The driver never halts any more. Every human-needed condition writes an `.error-<title>` marker, shows a red row, and the loop moves on.
- Verdict floor is an absolute 15.0 percent on the normalised ratio, bracketed above Flight and below Croods. Verdicts compare only against same-encoder history.

## Batch policy, layout, and space

- The `no-delete` flag means nothing syncs and nothing deletes. Good verdicts are recorded kept and left beside their source. This is the current mode.
- The X9 layout is `queue/` for anything the pipeline may act on and `complete/` for finished encodes. The driver migrates legacy root folders every pass.
- The replenisher is independent of the encoder. It keeps `queue/` between 10 and 18 folders, ticks every 60 s via launchd, and pulls one title per landing with stdin from `/dev/null`.
- The download budget counts pulls landed, never encodes. At the budget the replenisher stops pulling and the driver simply runs out.
- The pusher moves finished encodes from `complete/` back to the NAS beside their originals over SSH, purges the X9 source only after the NAS copy verifies, and never removes anything on the NAS.
- A 100 GiB floor on the X9 blocks new encode starts with a wait, not an error. One `LOW SPACE` log line per episode. The email for it is built but notifications are off.
- A full staging drive once produced five fake errors. Start now refuses without 80 percent of source plus 10 GiB free, and the replenisher reserves 150 GiB above every pull.

## Observability

- The resource monitor keeps 7 days of 1 Hz samples in a ring file with twelve fixed zoom stops. Gaps are gaps, never interpolation. No-data regions ride a labelled wash.
- The Events tab parses the driver and watcher logs. The Processes tab classifies `ps` output by purpose. Both are dashboard-only modules.
- Notifications are off since 2026-09-09 at the operator's request. The morning brief at 09:00 is the one daily email and replaced the hourly heartbeat.
- Repaint keys carry shape, never a live number. Byte counts reach the DOM through in-place updates so transfer bars move without rebuilding the table.

## Tooling and tests

- `bun run test` runs every suite in parallel and each fails fast. Shell suites run in sandboxes with fake HandBrake and fake ssh. Nothing touches the live X9 or NAS.
- `bun run lint` is the whole gate: oxlint, bun syntax check, tsc, ruff, compileall, shellcheck, actionlint, oxfmt check, page assembly. A missing tool fails rather than skips.
- Husky hooks are tracked: pre-commit refuses runtime artifacts and runs lint-staged with `--no-stash`, commit-msg enforces the `SMLTR:` subject format, pre-push re-checks subjects and runs the full verify.
- CI runs five jobs and every step is a `bun run` script. Shell suites run on macOS because of BSD `stat`. The Python floor is 3.9.

## Non-negotiables

- Never use SMB to move data or take stats against the NAS. Always SSH. Mounted shares are for reading the library's shape only.
- Never drop an audio or subtitle track. Never invent a missing number. An unmounted NAS must never look like a finished job. A transfer that is not moving is stalled, never a progress bar.
- Keep the three CSP nonces, never `innerHTML` server data, colours are tokens in both themes, every clock is 12-hour, and restart the server after any UI or core edit.
