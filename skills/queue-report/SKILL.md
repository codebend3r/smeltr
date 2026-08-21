---
name: queue-report
description: Show the 4K HEVC re-encode queue as a table — live encode progress, the remaining queue ranked by bitrate, and the full ledger of finished encodes. Use when the user asks "show me the queue", "queue status", "what's encoding", "show the entire queue", "what's left", "how much have we saved", "re-encode status", or wants the Smeltr dashboard link. Always renders as aligned tables and always prints the live dashboard URL.
---

# queue-report

A terminal snapshot of the 4K re-encode pipeline, plus the link to **Smeltr**,
the live localhost dashboard that shows the same data updating in real time.

The snapshot is a still frame. The dashboard is the moving picture. Always give
the user both — the tables answer the question now, the link lets them watch.

## Run it

```bash
~/Developer/git/smeltr/smeltr report              # live encode + top 12 queue + recent history
~/Developer/git/smeltr/smeltr report --all        # every row, no truncation
~/Developer/git/smeltr/smeltr report --queue      # queue only
~/Developer/git/smeltr/smeltr report --history    # ledger only
~/Developer/git/smeltr/smeltr report --limit 25   # custom row count
```

`report` starts the dashboard automatically if it isn't already up, so the URL
in the output is always valid. Other commands:

```bash
~/Developer/git/smeltr/smeltr start|stop|restart|status|url
```

## Recording a finished encode

The ledger only grows when something writes to it. Record **while the source
and output are both still on the staging drive** — after the sync deletes the
original, its size is gone for good and the row can never be completed:

```bash
~/Developer/git/smeltr/smeltr record "Flight (2012)" \
  --dest "Vhagar/F" \
  --source-path "/Volumes/Vhagar/Media/4K Movies/F/Flight (2012)/Flight (2012) Remux-2160p.mkv" \
  --note "..." --verified "ssim 0.9931/0.9945"
```

`--source-path` is required: it is the dedup key that removes the title from
the queue. A row without it leaves the title queued forever, where it can be
re-encoded on top of its own output.

`record` refuses to write if HandBrake is still producing the file, if the
output was touched in the last two minutes, if the output is not smaller than
the source, or if ffprobe cannot read either file. A ledger row is the evidence
that authorises deleting an original — it must never be written on a guess.

Use `--verified` to record independent evidence (an SSIM measurement, a scene
you watched) when a `suspect` encode has been checked and accepted.

## Presenting the output

Paste the tables into your reply **inside a fenced code block**. They are drawn
with Unicode box characters and depend on a monospace font; in prose they
collapse into unreadable ragged lines.

Then, outside the code block, state:

1. **The dashboard URL on its own line** so the terminal makes it clickable.
   This is required on every single invocation, even when the user only asked
   about one encode.
2. **What actually changed** since the user last looked, if you know — a new
   title started, a checkpoint verdict landed, the queue was replenished.
3. **Anything that needs a decision.** A `thin` or `blowup` verdict, a stalled
   encode, an unmounted drive. Lead with it; don't bury it under the tables.

## Reading the numbers

| Column | Meaning |
|---|---|
| `SRC Mb/s` | Bitrate of the **original** — megabits, and the ranking key for the whole job |
| `SRC SIZE` | Size of the **original** on the NAS |
| `PROJECTED FINAL` | Output size extrapolated from bytes written so far |
| `SHRINK` | How much smaller the output is. **Bigger is better, everywhere** |
| `SOURCE OF RECORD` | Provenance — see below |

Never write the bitrate column as `MB/S`. Capital B means megabytes and is an
8× error against the actual megabits, sitting immediately beside a size column.
That exact ambiguity already corrupted this job's old text state file.

`SHRINK` runs one direction in every table: higher means a smaller output. Do
not reintroduce an "of source" column alongside it — two inverse scales on one
screen made the best result look like the worst.

`VERDICT` is advisory, never automatic:

| Verdict | Projected output | What it means |
|---|---|---|
| `good` | < 70% of original | Real shrink; let it finish |
| `thin` | 70–84% | Passes, but thin — **tell the user, let them call it** |
| `no-saving` | 85–99% | Kill and restart at CRF 18 |
| `blowup` | ≥ 100% | Kill and restart at CRF 20 |
| `suspect` | implausibly small, or far below the job's median | **Stop and verify — see below** |
| `downscale` | output frame is narrower than the source | **Resolution was thrown away. Never delete the original** |

**`suspect` is the one that matters most.** The plain ladder cannot see an
implausibly *small* output: its "good" band is everything under 70%, so a
routine 69% and a physically improbable 9% both render as "let it run" — and
that is the sentence someone reads before deleting a 90 GB original that exists
nowhere else. Smeltr therefore compares each projection against the
distribution of encodes actually recorded in the ledger, and flags anything far
outside it. On `suspect`: report it prominently, check the output geometry for
an unintended downscale, and recommend watching a scene before the original is
deleted. Do not let the sync proceed silently on a `suspect` verdict.

Two independent checks produce `suspect`, deliberately measured on different
scales. A **raw floor** catches output that is too small to be physically
plausible for 4K at CRF 16, tested on the unadjusted byte ratio. A **median
baseline** catches anything far below what this job typically achieves, tested
per retained pixel so letterboxed films are not punished for their auto-crop.
The baseline is the median rather than the best-ever: with a minimum, one
legitimate outlier permanently widens "normal" and the detector disarms itself.

Projections are suppressed below 5% progress — opening logos and black frames
encode to almost nothing and would flatter the estimate into meaninglessness.

## Provenance — say where a number came from

The `RECORD` column is not decoration. It marks how much a row can be trusted:

- **`live`** — written by Smeltr the moment the encode finished. Exact bytes.
- **`state-file`** — migrated from the old text state file. Sizes are accurate
  to two decimals but were transcribed by hand.
- **`recovered`** — the encode finished but was never recorded; the output was
  found on the NAS afterwards and the original is already deleted. The original
  size is genuinely unknown.

**Never invent a missing original size to make a row look complete.** Rows
without an original are excluded from the reclaim total and the average shrink
on purpose. If a user asks why a row shows `—`, the honest answer is that the
original was deleted before its size was recorded — not a back-solved estimate.

## Guardrails

- This skill is **read-only**. It never starts, stops, or alters an encode, and
  never touches the media library. Reporting and acting stay separate.
- If the staging drive is unmounted the report says so and sizes will be
  partial. Surface that warning rather than presenting the numbers as complete.
- Titles the user has excluded never appear. That list lives in `SKIP` in
  `~/Developer/git/smeltr/core.py` so the dashboard and this report cannot disagree.
- The `#` in the queue table is display position, not a stable id. Rank shifts
  as titles complete; don't refer to "number 7" across sessions.
