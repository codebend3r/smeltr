---
name: heartbeat
description: Install, inspect, or debug the hourly check that something is actually encoding. Use when the user asks to "set up the heartbeat", "is the heartbeat running", "why didn't I get flagged", "check if anything is encoding", "the encoder went idle and nobody told me", or reports an outage nothing alerted them about. Also use before and after any pause procedure, since the heartbeat will flag a deliberately stopped driver.
---

# heartbeat

An outside observer that asks one question on the hour: **is a HandBrakeCLI
process running?** If yes, nothing happens. If no, every channel in
`notify.json` gets flagged immediately, with a sentence naming which of the
seven idle states it is and what to do about it.

Everything else in this repo watches the pipeline from the inside. This
watches it from outside, because every real outage this job has had was a
component that had stopped being able to report on itself:

| When | What happened | What noticed |
|---|---|---|
| 2026-08-25 | stale `.autopilot.lock` defeated every relaunch; 6 h down | a human, next morning |
| 2026-08-31 | NAS blip during the judge → HALT, 4 h 16 m idle | a human |
| 2026-09-04 | decoder-error verdict halted the driver at 04:05 | a human, 5 h later |
| 2026-09-06 | Skyscraper staged 3 days, unencodable (`.mp4` vs `.mkv`) | **nothing at all** |

The last row is the one this exists for: no error, no halt, no red row. The
pipeline believed it was healthy. `is HandBrake running` would have caught it
on the first hour.

## Run it by hand

```bash
bun run heartbeat            # the real thing: checks, and flags if idle
bun run heartbeat:check      # verdict only, sends nothing, no 60 s wait
./smeltr heartbeat --dry-run # same, but keeps the confirming wait
```

Exit **0** = something is encoding. Exit **1** = it is not, and the alert went
out (or could not be sent — that reason is on stderr).

## Install the hourly agent

```bash
mkdir -p ~/Library/Logs/smeltr
cp ops/com.smeltr.heartbeat.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.smeltr.heartbeat.plist
launchctl list | grep smeltr          # proves it is LOADED
tail -f ~/Library/Logs/smeltr/heartbeat.out.log
```

`RunAtLoad` fires one check immediately, so installing it proves it works
rather than leaving up to 59 minutes of silence that could mean either.

To stop it: `launchctl unload ~/Library/LaunchAgents/com.smeltr.heartbeat.plist`.

**`StartCalendarInterval`, not `StartInterval`** — the operator asked for *on
the hour*. `StartInterval 3600` counts from load, so a reload at 14:37 pins
every future check to :37 past.

## What it reports

The probe is `ps -axo`, so **it works with no Full Disk Access and with the
staging drive unmounted.** That is deliberate: this is the one reading that
must stay true when the volume is unreadable, and it is why the heartbeat can
catch the exact failure the LaunchAgent watchdog cannot (see below).

Everything past the probe is *diagnosis*, and it degrades to "could not tell"
rather than to a confident wrong answer. States, in the order they are tested
— the first that applies is the one the message leads with, because each is a
different thing to go and do:

| State | Means | Do |
|---|---|---|
| `encoding` | HandBrake is running | nothing — exit 0, one OK line in the log |
| `blind` | `$X9` is not readable | remount, or grant Full Disk Access |
| `no-driver` | no `autopilot.sh` process | relaunch it, or press play on the dashboard |
| `paused` | the dashboard pause flag is set | deliberate — resume when ready |
| `wedged` | a title is READY, driver alive, nothing encoding | **the bad one.** Read `.autopilot.log` |
| `waiting` | staged work exists but none is startable | check the reason it names (arriving / errored / skipped) |
| `stopped` | nothing above the stop threshold | confirm the library is fully mounted before believing it |

`paused` still flags. A pause is not a fault, but an hour of idle encoder is
worth knowing about either way — the message says plainly that you chose it.

## Two samples, never one

An idle reading is **re-read 60 s later** and only a second idle reading
counts. The driver hands off between encodes in seconds, and an hourly probe
that landed in that window would flag a perfectly healthy pipeline. Same
strike-and-confirm rule `.watch-encode.sh` uses before it kills an encode.

`--confirm-seconds 0` skips it. Only do that in tests or when you want an
instant answer by hand.

## How it flags you

Through `dashboard/notify.py`'s transports and the same `notify.json` (Slack
webhook + Gmail app password) the event notifier already uses — deliberately
not a second, separately-configured alerting path that would work on the day
the real one does not. The alert is `what: "idle"`, which is **first in
`PRIORITY`**: in a burst it is the last message dropped.

It never touches `notify.cursor`. That file is the event notifier's seen-set,
and writing it here would silently swallow encode events.

No `notify.json`? The check still runs and still exits 1, and says on stderr
that it had nowhere to send. `bun run notify:test` proves the channels.

## Before you pause the driver

The pause procedure in CLAUDE.md stops the watchdog and then the driver. **The
heartbeat will flag that within the hour** — correctly, since nothing is
encoding. Either expect the message, or unload the agent for the duration:

```bash
launchctl unload ~/Library/LaunchAgents/com.smeltr.heartbeat.plist
# ... pause procedure, edits, deploy, restart ...
launchctl load ~/Library/LaunchAgents/com.smeltr.heartbeat.plist
```

## This is NOT the watchdog

They fail differently and you want both.

|  | `ops/watchdog.sh` | heartbeat |
|---|---|---|
| Asks | is the driver PROCESS absent | is anything ENCODING |
| Acts | relaunches the driver | tells you, changes nothing |
| Needs the X9 readable | **yes** — blind, it reports STOP CONDITION | **no** — `ps` only |
| Catches a wedged-but-alive driver | no | **yes** |
| Catches a title nothing can encode | no | **yes** |

The watchdog going blind without Full Disk Access is a documented, silent
failure. The heartbeat's core probe cannot go blind that way, which is the
whole point of running it as well.

## Files

| Path | What |
|---|---|
| `dashboard/heartbeat.py` | the check, the diagnosis, the alert. Pure observer — starts nothing, kills nothing |
| `ops/com.smeltr.heartbeat.plist` | the hourly LaunchAgent |
| `tests/test_heartbeat.py` | pins every state, the two-sample rule, and that it writes nothing |
| `smeltr heartbeat` | the launcher subcommand the agent runs |

It is imported by nothing in `pipeline/`. `tests/test_layering.py` lists it
`DASHBOARD_ONLY`, so a decision-path import fails the suite.
