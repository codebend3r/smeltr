---
name: 4k-hevc-reencoding
description: Use when shrinking a 4K UHD remux/Bluray/WEB-DL video to a smaller MKV while keeping it 4K, via HandBrakeCLI. Triggers include "shrink this 4K file", "make this remux smaller", "lower the bitrate but keep 4K", "re-encode to HEVC", "convert 60GB remux to something smaller", "shrink the video but keep audio/subs", or operating on a `.mkv` whose bitrate is 60–100+ Mb/s. Covers ffprobe inspection, the **CRF ladder (always start at 16; mandatory size projections at 25/50/75% step down to 18 or 20 if the encode is heading for a blowup)**, checking whether the unattended `.autopilot.sh` driver is already running this, MKV container forcing, **mandatory passthrough of ALL audio and subtitle tracks (non-negotiable)**, post-kickoff verification that tracks were actually included, background execution, progress monitoring (with the `\r`-tailing trick), and Dolby Vision caveats.
---

# 4K HEVC Re-encoding (HandBrake)

Shrink a 4K UHD remux (typically 60–100 GB at ~90 Mb/s) into a smaller HEVC MKV while preserving 4K resolution **and every original audio + subtitle track**. Output goes next to the original; **never overwrite or delete the source** until the user has verified the encode.

> **IRON RULE — ALWAYS KEEP ALL AUDIO + SUBTITLE TRACKS.**
> The user has been burned by encodes that silently dropped audio and subs. There is no shortcut, no "smaller-file" preset, and no time pressure that makes it acceptable to drop tracks. If your command does not include `--all-audio --aencoder copy --audio-fallback ac3 --all-subtitles`, you are running the wrong command. **Do NOT add `--audio-lang-list` or `--subtitle-lang-list` flags** — see the trap callout in §4. After kickoff you MUST verify the log shows every source track was picked up (see §4.5) — a multi-hour encode that drops tracks is a multi-hour waste.

> **IRON RULE — START AT CRF 16, AND TAKE THE 25% CHECKPOINT.**
> Every encode begins at `--quality 16`. You do not get to guess a higher CRF because a source "looks grainy". Instead, when task 2 crosses 25%, project the final size (§5.5) and step down the ladder on evidence: ≥100% of original → restart at CRF 20; 85–99% → restart at CRF 18; <85% → let it run. Skipping this check is how a 6-hour encode finishes *bigger* than the remux it replaced.

> **Unsure of settings?** Run a 5-minute preview encode first via the [[4k-hevc-preview-encode]] skill, then come back here for the full encode once the user has approved the look.

## FIRST — is the unattended driver already doing this?

`.autopilot.sh` on the staging drive runs this entire workflow unattended: it
picks the title, starts the encode at CRF 16, verifies tracks, watches the
checkpoints, ladders the CRF, judges, records, syncs, and deletes the library
original. **Check before you type anything:**

```bash
pgrep -f autopilot.sh          # driver alive?
pgrep -fl HandBrakeCLI         # something already encoding?
```

If either returns a pid, this skill is a *reference for what the driver is
doing*, not a set of commands to run. Two consequences, both load-bearing:

- **Never `pkill -f HandBrakeCLI`.** It kills whatever the driver is encoding,
  and every `kill`/`pkill` instruction below is written for an encode you
  started yourself. Kill by the pid you captured at kickoff.
- **Never start a second encode by hand.** The driver refuses to run two, but
  nothing stops you; two encodes share one CPU and both crawl.

To take over manually, pause the driver first — `kill -9` the pid and remove
`.autopilot.lock` by hand. `CLAUDE.md` in the smeltr repo has the exact
procedure and the three traps in it. To start one encode without leaving the
pipeline, use the dashboard's per-title CRF picker instead of this skill.

## Workflow at a glance

1. **Inspect** the source with `ffprobe` (codec, bitrate, dimensions, HDR/DV, **count audio + subtitle tracks — record exact numbers**).
2. **Pick** a CRF — **start at 16** (see §2).
3. **Encode** to MKV in the background (multi-hour job), passthrough audio + subs.
4. **Verify within 60 s of kickoff** that the log shows every source audio + subtitle track was picked up. If not, kill the job and re-run.
5. **Monitor** with the `\r → \n` tail trick.
6. **At 25% progress, project the final size and apply the CRF ladder** (see §5.5) — abort and restart at a higher CRF if the projection is at or near the original size. This is mandatory, not optional.
7. **Report** final size + effective bitrate when done, and re-count tracks in the output to confirm parity with the source.

## Step 1 — Inspect first

```bash
ffprobe -v error -show_format -show_streams "<file>.mkv" 2>&1 | head -100
ls -la "<file>.mkv"
```

Also list tracks compactly so you know what you're preserving:

```bash
ffprobe -v error -show_streams "<file>.mkv" 2>&1 \
  | grep -E "^index=|^codec_type=|^codec_name=|^channel_layout=|TAG:language=|TAG:title=" \
  | paste - - - - - - - -
```

Look for:
- `bit_rate=` in `[FORMAT]` → overall bitrate (bps).
- Video stream `codec_name` (usually `hevc` for UHD remuxes) and `pix_fmt` (`yuv420p10le` = 10-bit — important).
- `width=3840 height=2160` confirms 4K.
- `[SIDE_DATA]` block with `DOVI configuration record` → **Dolby Vision present** (see §6).
- Number of audio streams + their codecs (DTS-HD MA, TrueHD, E-AC3 Atmos, etc.).
- Number of subtitle streams + languages (PGS for bitmap, SRT/ASS for text).

Tell the user the track list before encoding — they should know what's being preserved. **Record the exact counts** (e.g. "3 audio tracks, 35 subtitle tracks") so you can verify parity after kickoff and in the output.

## Step 2 — Pick CRF

**CRF is inverse:** lower number = bigger file + better quality; higher = smaller + worse. This trips people up.

| Goal | CRF | Expected video bitrate | Expected size from 60 GB remux |
|---|---|---|---|
| Maximum-fidelity archive **(start here — always)** | **16** | ~40–55 Mb/s | **~35–50 GB** |
| Near-lossless archive | 18 | ~30–40 Mb/s | ~25–35 GB |
| Visually transparent | 20 | ~20–25 Mb/s | ~18–25 GB |
| Visually transparent, smaller | 22 | ~15–17 Mb/s | ~12–18 GB |
| Middle-ground | 26 | ~8–10 Mb/s | ~8–12 GB |
| Aggressive shrink (visible grain loss) | 30 | ~4–6 Mb/s | ~4–8 GB |
| Very small (artifacts visible) | 32+ | <4 Mb/s | <4 GB |

**Note on size estimates:** these are total file sizes assuming audio + subs are passed through unchanged. Audio passthrough adds 1–4 GB depending on tracks (DTS-HD MA is ~2 Mb/s, Atmos can be 5+ Mb/s).

Real-world calibration from this skill's reference encode (1996 live-action film, 1h 26m, HQ preset CRF 22 with audio transcode): 60 GB → 12 GB (~80% reduction). Modern action films with more motion/detail will compress less efficiently.

### The CRF ladder — always start at 16, step down only on evidence

**Every encode starts at `--quality 16`.** Do not pre-emptively pick a higher CRF because the source "looks grainy" or because you want it to finish faster — you don't get to guess. The 25% checkpoint in §5.5 decides, using the actual measured output growth.

The ladder has exactly three rungs and you only ever move **down** it (16 → 18 → 20), never up. The dashboard's per-title picker also offers **22 and 24, which sit OUTSIDE the ladder**: a blowup at 22 or 24 is still auto-killed, but nothing restarts it, so a hand-picked 22/24 encode that fails is a dead end you have to notice yourself.

| Projected final size at 25% | Verdict | Action |
|---|---|---|
| **≥ 100% of original** | Blowup — the encode is *bigger* than the source | Kill, delete partial, **restart at CRF 20** |
| **85–99% of original** | "About the same size" — not worth the hours | Kill, delete partial, **restart at CRF 18** |
| **70–84% of original** | Close call — passes, but the saving is thin | **Keep running** and **report the numbers** in the status update. Don't kill it, don't stop to ask. |
| **< 70% of original** | Real shrink | **Let it run to completion** |

**The 70–84% close-call band.** The 85% line is a hard rule, but a film projecting 82% is saving under a fifth of its size for a full day of encoding — worth a human decision even though the rule says continue. Report the projection, the absolute GB saved, the remaining ETA, and a recommendation; keep the encode running while you wait, so a silent user costs nothing. Do **not** kill on your own inside this band, and do **not** stay silent about it either.

Grain is the usual cause: heavy film-stock grain at CRF 16 costs more bits than a modern digital source, because x265 is faithfully reproducing the grain. Oldboy (2003) projected 82.8% — the reference case for this band.

Rationale: heavy film grain at CRF 16 costs more bits to reproduce than the source's own encoder spent, so a grain-heavy catalog title can finish *larger* than the remux it came from. That failure has happened repeatedly with this workflow (Broker, Labyrinth). Catching it at 25% costs ~1 hour instead of ~6.

**Each restart gets its own fresh 25% checkpoint.** A CRF 18 retry that still projects ≥ 85% steps down to CRF 20. A CRF 20 retry that *still* projects ≥ 100% is a genuinely pathological source — stop, don't step down further on your own, and report it to the user for a manual call.

## Step 3 — Prerequisites

HandBrake.app being in `/Applications/` does **NOT** mean `HandBrakeCLI` is installed. They are separate. Always check:

```bash
which HandBrakeCLI || brew install handbrake
```

`brew install handbrake` pulls in the CLI binary plus deps. Takes ~30 s.

## Step 4 — Encode command — THE ONLY COMMAND

There is exactly one command for this skill. It re-encodes video only; **all audio is passed through losslessly, all subtitle tracks are copied through unchanged.** There is no "smaller file" variant, no "quick" variant, no HandBrake-preset shortcut — those silently drop or transcode tracks and are forbidden by the iron rule at the top of this skill.

Always:
- Output to a **new filename** next to the original.
- Force MKV container with `-f av_mkv` (HandBrake's 4K presets default to MP4).
- Use `x265_10bit` for HDR sources (HDR10 is fundamentally 10-bit; plain `x265` is 8-bit and causes banding).
- Include EVERY one of the audio + subtitle flags below. None are optional.
- Redirect the log to `$X9/.hb-<slug>.log` on the staging drive, **not** `/tmp`.
  Two reasons, both learned live: `/tmp` gets wiped on reboot and by periodic
  cleanup, which killed the checkpoint watcher mid-job twice (exit 127); and
  `core.live_encodes()` scans only `.hb-*.log` on the staging drive, so an
  encode logged to `/tmp` is invisible to the dashboard and the report.
- Run in the **background** (5–12+ hours typical on Apple Silicon CPU at CRF 16).

```bash
HandBrakeCLI \
  -i "/path/to/Movie (YYYY) Remux-2160p.mkv" \
  -o "/path/to/Movie (YYYY) 2160p HEVC.mkv" \
  -f av_mkv \
  --encoder x265_10bit \
  --quality 16 \
  --encoder-preset medium \
  --all-audio \
  --aencoder copy \
  --audio-fallback ac3 \
  --all-subtitles \
  > "/Volumes/Crucial X9/4K Movies/.hb-<slug>.log" 2>&1 &
```

`<slug>` is the folder name lowercased with everything non-alphanumeric stripped,
truncated to 20 chars — `Shrek (2001)` becomes `shrek2001`. Match that, or the
dashboard pairs the log with the wrong title.

**Flag breakdown — every row is mandatory:**

| Flag | Purpose | Removing it does what |
|---|---|---|
| `-f av_mkv` | Force MKV container (avoids MP4 default) | Output silently becomes `.mp4` |
| `--encoder x265_10bit` | 10-bit HEVC — required for HDR10 to avoid banding | Banding on HDR sources |
| `--quality 16` | CRF 16 = the mandatory starting rung. **Never start higher.** Only §5.5's 25% checkpoint may step it down to 18, then 20. | Wrong starting quality; the ladder in §2 no longer applies |
| `--encoder-preset medium` | x265 speed/efficiency balance. Same as HandBrake's "HQ" preset. | Slower or worse compression |
| `--all-audio` | Include **every** audio track regardless of language (uses "any language" default when no lang-list is set) | **Only one audio track survives** |
| `--aencoder copy` | **Passthrough** audio without re-encoding — preserves DTS-HD MA / TrueHD / Atmos losslessly | DTS-HD MA / TrueHD / Atmos transcoded to lossy AAC/AC3 |
| `--audio-fallback ac3` | Safety net: if a track can't be copied, convert to AC3 instead of failing | Encode aborts on incompatible track |
| `--all-subtitles` | Include all subtitle tracks (PGS, SRT, ASS) regardless of language | **Every subtitle track is dropped** |

> **TRAP — do NOT add `--audio-lang-list` or `--subtitle-lang-list`.** From `HandBrakeCLI --help`: "`--all-audio` … selects all audio tracks matching languages in the specified language list. **Any language if list is not specified.**" If you pass `--audio-lang-list "und"`, HandBrake filters to ONLY tracks tagged as undetermined language (ISO 639-2 `und`). Almost no real-world source tags tracks `und` — they're tagged `eng`, `fre`, `spa`, etc. — so the filter matches zero tracks and everything gets dropped. **Omit the lang-list flags entirely** and `--all-audio` / `--all-subtitles` will correctly pick up every track.

**The empirical failures that motivated the iron rule and the trap callout:**
1. A previous run omitted the audio/subtitle flags entirely. Log: `audio tracks:` empty, `"SubtitleList": []`. 3 audio + 35 subs → 1 audio + 0 subs. ~5 hours wasted.
2. A subsequent run included `--all-audio --all-subtitles` BUT also included `--audio-lang-list "und" --subtitle-lang-list "und"`. Same symptom — log showed empty track lists — because no source tracks were tagged `und`. Caught at §4.5 within 30 seconds; ~10 minutes of preview wasted instead of 5 hours of full encode.

Both failures are caught by §4.5 if you actually run the check. Run the check.

## Step 4.5 — Verify track preservation within 60 seconds of kickoff (MANDATORY)

A HandBrake encode is multi-hour. If the wrong flags were used, you only find out after it finishes — unless you check now. **Do not walk away or report "encode started" without this check.**

Within ~30–60 seconds of kickoff, the log will contain HandBrake's parsed job config and its per-track scan output. Run:

```bash
grep -E "AudioList|SubtitleList|scan: audio|scan: subtitle|\+ audio tracks|\+ subtitle tracks" "/Volumes/Crucial X9/4K Movies/.hb-<slug>.log"
```

**What you must see — the GREEN signal:**

- `"AudioList": [` followed by one block **per source audio track** (each with `"Track":` and `"Encoder":`).
- `"SubtitleList": [` followed by one block **per source subtitle track** (each with `"Track":` and `"Source":`).
- A `+ audio tracks:` line followed by `    + N, English (...)` lines — one per source audio track.
- A `+ subtitle tracks:` line followed by `    + N, <Language> (...)` lines — one per source subtitle track.

**The RED signal — abort immediately:**

- `"SubtitleList": []` (empty array) — subtitles will be dropped.
- `"AudioList": [` followed by only one block when the source has multiple — extra audio will be dropped.
- `+ audio tracks:` with no `+ N, ...` lines under it — every audio track will be dropped.
- `+ subtitle tracks:` with no `+ N, ...` lines under it — every subtitle track will be dropped.
- `Using preset: CLI Default` with no overrides — confirms the flags were not applied.

**If you see the red signal:**

1. Kill the job by **its own pid** — `kill <pid>`, captured with `$!` at
   kickoff. Not `pkill -f HandBrakeCLI`: if the driver is running, that kills
   its encode too.
2. Delete the partial output file: `rm "<output>.mkv"`.
3. Re-run the §4 command, double-checking that every audio + subtitle flag is on its own backslash-continued line and nothing got dropped during shell quoting.
4. Re-verify within 60 s. Do **not** assume the second attempt is correct without checking the log again.

Only after the green signal is confirmed is it acceptable to "let it run" and move on.

## Step 5 — Monitor progress

(Only after §4.5 confirmed all tracks were picked up.)

**The `\r` trap:** HandBrakeCLI writes progress with carriage returns, not newlines. Plain `tail` shows one giant unreadable line. Always pipe through `tr`:

```bash
tr '\r' '\n' < "/Volumes/Crucial X9/4K Movies/.hb-<slug>.log" | tail -3
```

Output looks like:
```
Encoding: task 2 of 2, 1.29 % (4.61 fps, avg 7.38 fps, ETA 04h37m11s)
```

The encode runs as **two tasks**: task 1 is the subtitle/foreign-audio scan (fast, seconds), task 2 is the actual video encode (hours). Don't celebrate when task 1 hits 100%.

Also check the growing output file:
```bash
ls -la "<output-folder>/"
```

## Step 5.5 — The size-projection checkpoints (MANDATORY)

At CRF 16 a grain-heavy source can encode *larger* than the remux it came from. Waiting until the end to discover that wastes 6+ hours. **When task 2 crosses 25%, project the final size and apply the §2 CRF ladder**, and take the reading again at 50% and 75%. This check is not optional and is not "nice to have if you remember" — it is the entire reason the encode starts at 16 rather than a safe-but-mushy 20.

> **The pipeline already does this, and it acts.** `.watch-encode.sh` takes the
> reading at 25/50/75%, and at ≥85% it **kills the encode itself**, deletes the
> partial, and emits a `KILLED|…|next: CRF N` line that the driver picks up to
> relaunch one rung lower. Detection without authority to act was a slower way
> to waste a day: Steel Magnolias flagged `NOSAVING->CRF18` at its 25%
> checkpoint with nobody listening and ran a further ~12 hours to produce a file
> 1% smaller than its source. `SMELTR_NO_AUTOKILL=1` returns it to
> report-only. It never auto-kills inside the 70–84% close-call band — that one
> is still a human call. **If a watcher is attached, do not take these readings
> by hand and do not kill anything; read `.watch-<slug>.log`.**

### Taking the measurement

The output file grows roughly linearly with encode progress (audio + subs are interleaved proportionally as it goes), so:

```
projected_final_size = current_output_size / (task2_percent / 100)
```

```bash
SRC="/path/to/Movie (YYYY) Remux-2160p.mkv"
OUT="/path/to/Movie (YYYY) 2160p HEVC.mkv"
LOG="/Volumes/Crucial X9/4K Movies/.hb-<slug>.log"

PCT=$(tr '\r' '\n' < "$LOG" | grep -o "task 2 of 2, *[0-9.]*" | tail -1 | grep -o "[0-9.]*$")
CUR=$(stat -f%z "$OUT"); SRCSZ=$(stat -f%z "$SRC")
awk -v p="$PCT" -v c="$CUR" -v s="$SRCSZ" 'BEGIN{
  proj=c/(p/100); r=proj/s*100
  printf "progress %.2f%%  current %.2f GB  projected %.2f GB  original %.2f GB  = %.1f%% of original\n",
    p, c/1073741824, proj/1073741824, s/1073741824, r
  print (r>=100 ? "VERDICT: BLOWUP -> restart at CRF 20" : r>=85 ? "VERDICT: NO REAL SAVING -> restart at CRF 18" : "VERDICT: OK -> let it run")
}'
```

Take the reading **at or just past 25%**, not at 5% — early frames are unrepresentative (opening titles and dark studio logos encode tiny, which flatters the projection). Anywhere in the 25–30% band is fine.

### Acting on the verdict

| Projected / original | Verdict | Action |
|---|---|---|
| **≥ 100%** | Blowup | `kill <pid>`, `rm` the partial output, re-run §4 with `--quality 20` |
| **85–99%** | Not worth the hours | `kill <pid>`, `rm` the partial output, re-run §4 with `--quality 18` |
| **70–84%** | Close call | **Keep running**, but surface it to the user with numbers + recommendation and let them decide |
| **< 70%** | Genuine shrink | Let it run to completion |
| **implausibly small** | `suspect` | **Stop.** Too small to be physically plausible, or far below this job's median. Verify picture quality before anything is deleted |
| **narrower output frame** | `downscale` | **Stop.** Resolution was thrown away, not letterboxing. Never delete the original |

**The `awk` above cannot see the last two rows.** A bare byte ratio reads a
physically improbable 9% and a routine 69% identically as "let it run" — and
that is the sentence someone reads before deleting a 90 GB original. Smeltr
compares each projection against the distribution of encodes actually in the
ledger, and checks output geometry against the source. Prefer the real
evaluator over the hand projection:

```bash
~/Developer/git/smeltr/smeltr report                # live projection + verdict
~/Developer/git/smeltr/smeltr verdict "<folder>"    # finished encode; JSON + exit code
```

`verdict` exits `0` good, `2` halt (`suspect` / `thin` / `downscale` — a human
must look), `3` ladder (`no-saving` / `blowup` — retry a rung lower), `4` could
not evaluate. It is the same `core._verdict()` the dashboard uses, so the two
can never disagree about whether an original is safe to delete.

After any restart: **re-run §4.5 (track verification) within 60 s, then take a fresh §5.5 checkpoint at 25% of the new encode.** The ladder is re-evaluated per attempt — a CRF 18 retry that still projects ≥ 85% steps down to CRF 20.

If a CRF 20 attempt *still* projects ≥ 100% of original, stop laddering. Report it to the user and let them decide — a source that inflates at CRF 20 is pathological (extreme grain, or an already-efficient HEVC source with little left to squeeze) and may simply not be worth re-encoding at all.

**Report the checkpoint to the user when you take it** — projected size, ratio, and verdict. It's the first real evidence of whether the multi-hour job is worth finishing.

## Step 6 — Dolby Vision warning

If `ffprobe` showed a `DOVI configuration record` with `dv_profile=7` (dual-layer FEL — common on UHD Blu-ray remuxes):

- A vanilla HandBrake encode **strips Dolby Vision** entirely. Output is HDR10 only.
- For most viewers on most TVs this is fine — HDR10 still looks great.
- To preserve DV you need `dovi_tool` to extract the RPU, encode the base layer with x265 separately, then re-inject. HandBrake cannot do this. Mention this tradeoff explicitly to the user before encoding so they can decide.

Profile 5 (single-layer, e.g. some streaming sources) has similar caveats.

## Step 7 — Report when done

When the background job finishes, report:
- Final file size (`ls -la`).
- Effective bitrate (`ffprobe -show_format` → `bit_rate`).
- Size reduction vs original (e.g. "60 GB → 28 GB, 53% smaller").
- **Track parity check** — re-run the inspect command on the output and compare counts against the numbers recorded in §1. Audio count must match. Subtitle count must match. If either is short, the encode failed regardless of how good the picture looks; tell the user and offer to re-run.
- **Record the ledger row before anything syncs or is deleted** — `smeltr record`
  needs the source and the output both still on the staging drive. Once the sync
  deletes the library original its size is gone for good and the row can never be
  completed. See [[4k-hevc-library-sync]].
- Note any cropping HandBrake auto-applied (output dimensions may differ from source, e.g. 3840×2076 instead of 3840×2160 when black bars were detected and removed — this is correct active-picture).
- **Compare durations, and diagnose any gap before reporting it.** Two very different causes look identical in `format=duration`:
  - **Container artifact (harmless).** MKV `format.duration` is the longest *stream*, and subtitle tracks often carry trailing padding past the last frame. A source can advertise 6823 s while its video ends at 6766 s. Confirm by comparing the last video **packet timestamps**, not the container duration:
    `ffprobe -v error -select_streams v:0 -read_intervals <near-end>%+120 -show_entries packet=pts_time -of csv=p=0 "<file>" | sort -n | tail -2`
    If source and output video end at the same pts, nothing was lost — say so rather than alarming the user.
  - **Real tail truncation.** If the output's last video pts is genuinely earlier than the source's, frames were dropped. This correlates with `N decoder errors` in the log — unreadable source frames at the tail. A couple of seconds of end-credits black is not worth a multi-hour re-encode; report it and move on. A large gap is a different matter — investigate before syncing.

Quick parity check:
```bash
echo "Source:"; ffprobe -v error -show_streams "<source>.mkv"   | grep -c "^codec_type=audio"
echo "Output:"; ffprobe -v error -show_streams "<output>.mkv"   | grep -c "^codec_type=audio"
echo "Source subs:"; ffprobe -v error -show_streams "<source>.mkv" | grep -c "^codec_type=subtitle"
echo "Output subs:"; ffprobe -v error -show_streams "<output>.mkv" | grep -c "^codec_type=subtitle"
```

## Common mistakes

| Mistake | Fix |
|---|---|
| Tailing log shows one giant line | Pipe through `tr '\r' '\n'` first |
| Output is `.mp4` despite HEVC preset | Add `-f av_mkv` |
| `HandBrakeCLI: command not found` even though HandBrake.app exists | `brew install handbrake` (CLI is separate) |
| Encode finishes in 2 minutes | That was task 1 (subtitle scan). Task 2 is the real encode. |
| Output dimensions don't match source | HandBrake auto-cropped black bars — usually correct, but verify with `ffprobe` |
| Higher CRF made the file *bigger* | CRF is inverse: lower CRF = bigger. Direction swapped. |
| Encode finished *larger* than the source | Expected at CRF 16 on grain-heavy film stock — but you should have caught it at 25% via §5.5 and restarted at CRF 20 hours earlier. |
| Started the encode at CRF 20 "because the source looks grainy" | You don't get to guess. Every encode starts at 16; §5.5's measurement steps it down on evidence. |
| Took the size projection at 3% and declared it fine | Opening titles/dark logos encode tiny and flatter the projection. Measure at 25–30%. |
| Used a HandBrake `--preset` shortcut, lost all subtitles + got transcoded audio | Presets silently drop subs and transcode audio. The only command for this skill is §4 — no preset shortcuts. |
| Encode kicked off, agent walked away, hours later output is missing tracks | Skipped §4.5. Always verify the log shows every source audio + subtitle track within 60 s of kickoff. |
| Log shows `"SubtitleList": []` or empty `+ audio tracks:` line | The §4 flags were not applied (or you added `--audio-lang-list "und"` which filtered everything out — see §4 trap). Kill, delete partial output, re-run §4 verbatim, re-verify. |
| Added `--audio-lang-list "und"` / `--subtitle-lang-list "und"` thinking "und = any language" | **Wrong.** `und` is ISO 639-2 for "undetermined" — a real language tag almost no source uses. Filters to zero tracks. Omit the lang-list flags entirely; `--all-audio` defaults to "any language". |
| Output has banding in dark scenes / sky gradients | You used `x265` (8-bit) on a 10-bit HDR source. Must use `x265_10bit`. |
| DTS-HD MA / Atmos got transcoded to AC3 | Missing `--aencoder copy`. Add it. |
| Dolby Vision missing from output | Expected — vanilla HandBrake strips DV. Use `dovi_tool` workflow if preservation matters. |
| Original got deleted | Never delete the source until the user confirms quality. Output to a new filename always. |

## Red flags — STOP

- About to run a HandBrake command that does **not** include `--all-audio --aencoder copy --audio-fallback ac3 --all-subtitles` → **stop, this violates the iron rule. Use §4 verbatim.**
- About to add `--audio-lang-list "und"` or `--subtitle-lang-list "und"` to "be explicit about any language" → **stop, `und` means "undetermined" and matches almost no real tracks. Omit the lang-list flags. See §4 trap.**
- About to use any `--preset "..."` HandBrake preset for this job → **stop, all built-in 4K presets strip or transcode tracks. Use §4 verbatim.**
- About to report "encode started" or walk away without running §4.5 → **stop, verify track preservation in the log first.**
- About to start an encode at any CRF other than 16 (without §5.5 having measured a step-down on this exact file) → **stop, start at 16.**
- Encode passed 25% and you haven't taken the §5.5 size projection → **stop, take it now; a blowup caught at 25% saves 5 hours.**
- §5.5 projected ≥ 85% of original and you're thinking "let's just see how it ends up" → **stop, kill and step down the ladder. That's what the checkpoint is for.**
- Log shows `"SubtitleList": []`, `Using preset: CLI Default` with no overrides, or `+ audio tracks:` / `+ subtitle tracks:` with no entries beneath them → **stop, kill the job, delete the partial output, re-run §4.**
- About to run `pkill -f HandBrakeCLI` → **stop, check `pgrep -f autopilot.sh` first. Kill your own pid, not every encode on the machine.**
- About to overwrite source file → **stop, output to a new name**.
- About to delete source before user verified output → **stop, leave the original**.
- About to use `--encoder x265` on a 10-bit HDR source → **stop, use `x265_10bit`**.
- User said "shrink to under X GB" but you picked a CRF → **switch approach and explain that exact size targets need two-pass or trial-and-error; CRF is quality-based, not size-based**.
- Source has DV Profile 7 and user didn't acknowledge the strip → **stop, warn explicitly before kicking off a multi-hour encode**.

## Rationalization table — do not fall for these

| Excuse | Reality |
|---|---|
| "The user just wants it smaller, the alt audio tracks aren't important" | The user has explicitly said "always keep all audio tracks and subtitles". There is no inferred exception. |
| "The HQ preset is what HandBrake recommends for 4K, so it must be right" | HandBrake's 4K presets drop subs and transcode audio. They are forbidden here. |
| "Adding `--all-audio --all-subtitles` is verbose, the CLI defaults are probably fine" | The CLI default picks one audio track and zero subtitles. That is precisely the failure case the iron rule exists to prevent. |
| "I'll just check tracks after it finishes" | "After it finishes" is 5+ hours from now. Check in §4.5, within 60 s, while you can still abort. |
| "Verification is overkill, I typed the flags correctly" | The previous run's agent thought the same thing. The log doesn't lie; trust it over your memory of what you typed. |
| "This one's obviously grainy, I'll save time and start at CRF 20" | Not your call. Start at 16 every time and let §5.5's measurement decide. Guessing high permanently costs quality on sources that would have compressed fine. |
| "The projection says 92% of original but the back half is probably calmer" | Maybe, maybe not — and 92% means you'd burn 4 more hours to save 8% of the file. Step down to CRF 18. |
| "83% is under 85, so I'll just say nothing and let it run" | Under 85 means *don't kill it* — not *don't mention it*. 70–84% is the close-call band: keep running AND flag it. Silence here hides a day of compute spent for a thin saving. |
| "83% is close enough to 85, I'll kill it to be safe" | Also wrong. Don't kill inside the close-call band on your own — the user decides. Killing unilaterally throws away hours they may have wanted. |
| "I'll do the 25% check on the next one, this one's already at 40%" | Take it now anyway. A blowup caught at 40% still saves hours over one caught at 100%. |
| "Re-running adds another 5 hours" | Yes, and skipping verification cost the user 5 hours already. The second 5 hours is still cheaper than shipping a broken output. |
