---
name: 4k-hevc-preview-encode
description: Use when the user wants a short preview re-encode of a 4K UHD source before committing to a multi-hour full encode — to QC the picture quality, confirm track passthrough, and project the final file size at a given CRF. Triggers include "preview the encode first", "do a 5-minute test", "let me see what CRF 22 looks like before the full run", "encode a sample", "test the settings on a clip". Encodes the first 5 minutes (00:00–05:00) only, using the same flags as `4k-hevc-reencoding` so the preview is representative. Always preserves all audio + subtitle tracks. Pairs with `4k-hevc-reencoding` for the full encode after QC.
---

# 4K HEVC Preview Encode (5-minute clip)

Re-encode only the first **5 minutes** (00:00:00 → 00:05:00) of a 4K source using the same flags as a full encode. Lets the user verify picture quality, audio/subtitle passthrough, and projected file size in ~5–15 minutes instead of 5+ hours, before committing to the full job.

> **IRON RULE — same as the parent skill.** The preview MUST use the same mandatory audio + subtitle passthrough flags. A preview that drops tracks does not represent the full encode and gives the user a misleading QC signal. See [[4k-hevc-reencoding]] §4 and §4.5 for the rationale and the empirical failure that motivated it.

> **CHECK THE DRIVER FIRST.** `pgrep -f autopilot.sh` — if the unattended driver
> is alive it will start a full encode on its own schedule, and a preview
> sharing the CPU makes both crawl. See [[4k-hevc-reencoding]]'s first section
> before running anything by hand.

## When to use this skill vs the full encode

| Situation | Skill |
|---|---|
| User is unsure which CRF to pick, or hasn't seen the codec's behavior on this source | `4k-hevc-preview-encode` first |
| User has already QC'd a preview at this CRF and approved it | `4k-hevc-reencoding` (full) |
| Source is unusual (heavy grain, animation, DV, weird color) and risk of bad output is real | `4k-hevc-preview-encode` first |
| User explicitly asked for a "test", "sample", "preview", or "5-minute clip" | This skill |

## Step 1 — Inspect source (same as parent §1)

Run the ffprobe checks from [[4k-hevc-reencoding]] §1. Record:
- Total runtime in minutes (you'll need this for size projection in §4).
- Audio track count, subtitle track count (you'll verify these in §3).

## Step 2 — Preview encode command

Identical to the parent skill's §4 command **except** for one added flag: `--stop-at seconds:300`. HandBrake's `--stop-at` is a duration from the start (default 0), so this encodes exactly the first 5 minutes.

```bash
HandBrakeCLI \
  -i "/path/to/Movie (YYYY) Remux-2160p.mkv" \
  -o "/path/to/Movie (YYYY) PREVIEW 0-5min CRF18.mkv" \
  -f av_mkv \
  --encoder x265_10bit \
  --quality 18 \
  --encoder-preset medium \
  --stop-at seconds:300 \
  --all-audio \
  --aencoder copy \
  --audio-fallback ac3 \
  --all-subtitles \
  > "/Volumes/Crucial X9/4K Movies/.preview-<slug>.log" 2>&1 &
```

> **TRAP — do NOT add `--audio-lang-list` or `--subtitle-lang-list`** (see [[4k-hevc-reencoding]] §4 trap callout). `--all-audio` and `--all-subtitles` default to "any language" only when no lang-list is set. Passing `--audio-lang-list "und"` filters to ISO 639-2 "undetermined" — which matches almost no real source tracks — and silently drops everything.

Naming convention for the output:
- Include `PREVIEW` in the name so it can't be mistaken for the final encode.
- Include the time range (`0-5min`) and CRF used (`CRF18`) so multiple previews at different CRFs don't collide.
- Keep it next to the source, alongside any prior full or preview encodes.

If the user wants to compare two CRFs (e.g. 18 vs 22), run two previews — change `--quality` and the filename, dispatch both in parallel as separate background jobs.

## Step 3 — Verify track preservation (MANDATORY, same as parent §4.5)

Within ~30 seconds of kickoff, confirm tracks were picked up. A preview that silently drops audio or subtitles is worthless for QC and you must NOT report it as "done":

```bash
grep -E "AudioList|SubtitleList|scan: audio|scan: subtitle|\+ audio tracks|\+ subtitle tracks" "/Volumes/Crucial X9/4K Movies/.preview-<slug>.log"
```

Apply the GREEN/RED signal interpretation from [[4k-hevc-reencoding]] §4.5 verbatim. If RED: `kill <pid>` (the pid from `$!` at kickoff — **never** `pkill -f HandBrakeCLI`, which would also kill whatever the driver is encoding), delete the partial output, re-run.

The log deliberately goes to the staging drive rather than `/tmp` (which gets wiped and has stranded watchers mid-job), and is named `.preview-<slug>.log` rather than `.hb-<slug>.log` so `core.live_encodes()` does not scan it — a 5-minute clip rendered as a live encode would corrupt the dashboard's projections and the queue's `encoding` badge.

## Step 4 — Report and let the user decide

When the background job finishes (typically 5–15 min on Apple Silicon at CRF 18, medium preset), gather:

1. **Preview file size** — `ls -la "<preview>.mkv"`.
2. **Projected full-encode size** — `preview_size × (full_runtime_minutes / 5)`. State the math explicitly so the user can sanity-check.
3. **Effective video bitrate** of the preview — `ffprobe -v error -show_format "<preview>.mkv" | grep bit_rate`.
4. **Track parity** — re-count audio + subtitle tracks in the preview and compare to source counts from §1. They must match.
5. **Visual QC suggestion** — tell the user to open both the source and the preview in IINA/VLC and scrub the same timestamps. Note that the first 5 minutes are often title cards / opening — easier to compress than action scenes — so the projection is a floor on quality, not a ceiling on size.

**Then ask the user how to proceed. Do NOT auto-kick the full encode.** Present three options:

- **Accept** → invoke [[4k-hevc-reencoding]] at the same CRF for the full file.
- **Try a different CRF** → re-run this skill with the adjusted `--quality` and a new output filename.
- **Abandon** → leave the source alone, delete the preview if desired.

## Common mistakes

| Mistake | Fix |
|---|---|
| Used `--start-at seconds:0 --stop-at seconds:300` and got 0 seconds of output | `--stop-at` is a duration from start — `--stop-at 300` alone is correct. Some HandBrake versions misparse the combination; omit `--start-at` for previews starting at 0. |
| Forgot the `--all-audio` / `--all-subtitles` flags "because it's just a preview" | The point of the preview is to verify track passthrough too. Iron rule still applies. |
| Reported "preview done" without running §3 | A preview with dropped tracks is a misleading QC signal. Always verify. |
| Projected full size by multiplying preview size × 12 (using hours instead of minutes) | Use minutes: `preview_size × (full_runtime_min / 5)`. A 5 min preview of a 120 min film projects to `× 24`, not `× 12`. |
| Output filename didn't include `PREVIEW` and got confused with the real encode | Always name preview outputs with `PREVIEW` + time range + CRF. |
| Picked a different CRF for preview than planned for full | The preview is only meaningful if it uses the same settings the full encode will use. Match them. |

## Red flags — STOP

- About to omit `--all-audio` / `--all-subtitles` / `--aencoder copy` / `--audio-fallback ac3` "to make the preview faster" → **stop, the preview must mirror the full encode exactly except for duration**.
- About to skip §3 verification because "it's only 10 minutes anyway" → **stop, a wrong-flags preview wastes the user's QC time and produces a false signal. Verify.**
- About to automatically kick off the full encode after the preview finishes → **stop, the user gets to decide. That's the whole point of a preview.**
- About to overwrite a prior preview at a different CRF → **stop, use a distinct filename so the user can compare side by side**.
- About to leave a `PREVIEW` file in a staging folder → **stop, delete it. `verdict.py` and `record.py` both require exactly one source and one output per folder, so a third `.mkv` makes them refuse and halts the driver; and `.autopilot.sh` picks its source with `find … | head -1`, which can pick the 5-minute clip and re-encode that instead of the film.**
- Preview filename does not contain `PREVIEW` → **stop, rename before kicking off; this is what stops it being confused with a real encode**.
