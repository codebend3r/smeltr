# Smeltr

[![CI](https://github.com/codebend3r/smeltr/actions/workflows/main-sanity-check.yml/badge.svg)](https://github.com/codebend3r/smeltr/actions/workflows/main-sanity-check.yml)

Local dashboard, terminal report, and append-only ledger for a long-running 4K
HEVC re-encode job. UHD remuxes are re-encoded with `HandBrakeCLI`, verified,
and moved back to the NAS with every audio and subtitle track kept.

Python 3 stdlib and vanilla JS. No runtime dependencies, no build step, no CDN.
`bun` is the only JS runtime, package manager, and task runner.

## Use

```bash
bun install            # dev tooling only (lint, format, hooks)
bun run start          # dashboard at http://127.0.0.1:8787/
bun run report         # terminal snapshot
bun run status         # is the server up
bun run restart        # after any UI or core edit
bun run set-password   # sign-in for the public door
```

The unattended driver, watchdog, replenisher, and pusher are launchd agents on
the staging drive. Their supervisors: `bun run watchdog`, `bun run replenisher`,
`bun run pusher`.

## Layout

| Path | What |
|---|---|
| `pipeline/` | Decision path the driver calls: `next`, `verdict`, `record`, `crf`, `encoder` |
| `dashboard/` | Server, report, events, processes, pusher state, morning brief |
| `web/` | The page: HTML, CSS, JS, no framework |
| `ops/` | Watchdog, replenisher, pusher, launchd plists, staging deploy |
| `staging/` | Byte-for-byte mirrors of the scripts that run on the staging drive |
| `tests/` | Python, shell, and TypeScript suites, all sandboxed |

## Check

```bash
bun run lint           # oxlint, tsc, ruff, shellcheck, actionlint, oxfmt, page assembly
bun run test           # every suite in parallel, fail fast
bun run verify         # lint, test, build (also pre-push)
```

Commit subjects are `SMLTR: <Verb> ...`, enforced by hooks and CI.

## Notifications

Off. Only the 09:00 morning brief emails. Its channel is a gitignored
`notify.json` beside the ledger, `chmod 600`:

```json
{
  "email": {
    "host": "smtp.gmail.com", "port": 587,
    "user": "you@gmail.com", "password": "app password",
    "to": "you@gmail.com"
  }
}
```

`./smeltr notify-test` sends one test message through it.
