---
name: 4k-hevc-library-sync
description: Use when a 4K HEVC re-encode on Crucial X9 has completed and passed track-parity verification, or when sweeping for already-completed re-encodes not yet moved to the library. Triggers include finishing an encode from the 4k-hevc-reencoding skill, "move this to the library", "sync completed re-encodes", "clear space on Crucial X9", or a movie folder containing both an original source file and a "2160p HEVC" file.
---

# 4K HEVC Library Sync

Move a verified re-encode from the Crucial X9 working drive into the real library (Vermithor or Vhagar), replacing the old larger file there, then wipe the movie's folder on Crucial X9 entirely. This is the step that actually reclaims space — pairs with [[4k-hevc-reencoding]], which only shrinks the file in place.

> **PREFER THE SCRIPT.** `.sync-to-library.sh "<Movie Name (YYYY)>"` on the
> staging drive is this whole procedure, already written, and it is what the
> unattended driver runs. It transfers over SSH rather than SMB (18 MB/s vs
> 9 MB/s, benchmarked 2026-08-17) and adds a track-parity check on the copy that
> the steps below do not have. The steps below are the reference for what it
> does and the fallback when it cannot run — reach for the script first.
>
> Check `pgrep -f autopilot.sh` before syncing anything by hand. If the driver
> is alive it will find the finished folder by disk scan and sync it itself; a
> hand-sync racing it double-counts the reclaim in the ledger.

> **IRON RULE — NEVER DELETE BEFORE VERIFYING.**
> Nothing gets deleted — not the library's old file, not the Crucial X9 originals — until the copy in the destination is confirmed byte-identical in size AND playable via `ffprobe`. A cross-volume copy that silently truncates (SMB hiccup, drive sleep, killed process) is exactly how a movie gets permanently lost. Verify first, always.

> **IRON RULE — RECORD THE LEDGER ROW BEFORE THE SYNC DELETES ANYTHING.**
> `smeltr record` measures the source and the output while both are still on the
> staging drive. This sync deletes the library original; after that its size is
> gone for good, the row can never be completed, and it renders forever as
> `recovered` with a `—` for the original. Record first, sync second — that is
> the order the driver uses. See [[queue-report]] for the exact command.

> **IRON RULE — NEVER REPLACE A LIBRARY FILE WITH A LARGER ONE.**
> Some re-encodes blow up (heavy grain film stock at low CRF can end up *larger* than the source — this has happened repeatedly with this exact workflow). If the re-encode is not strictly smaller than what's already in the library, abort and flag it. Never silently degrade the library with a bigger, lower-quality-per-byte file.

## Workflow at a glance

1. **Find the re-encode** on Crucial X9 (single-movie mode) or **scan for a backlog** of unsynced ones (bulk mode).
2. **Locate the matching folder** by exact name across **all three** library roots — `/Volumes/Vhagar/Media/4K Movies`, `/Volumes/Vermithor/Media/4K Movies`, and `/Volumes/Vermithor/Media/4K Family Movies` (search all letter-bucket subfolders, one level deep). The family root was added 2026-08-21 and has the identical `letter/title/file` layout; omitting it reports every family title as "no library match".
3. **Zero or two matches → stop and report.** Don't guess which drive/letter a new movie belongs on, and don't silently pick one if it exists on both (that's a real anomaly worth a human look).
4. **One match → size-guard, copy, verify size, verify playable, verify track parity, then delete** — old library file, then both Crucial X9 files, then the now-empty Crucial X9 folder.
5. **Report** what moved, what got skipped, and how much space was freed.

## Step 1 — Confirm both library drives are mounted

```bash
for d in "/Volumes/Vhagar/Media/4K Movies" "/Volumes/Vermithor/Media/4K Movies" \
         "/Volumes/Vermithor/Media/4K Family Movies"; do
  [ -d "$d" ] || { echo "MISSING: $d not mounted — stop, don't guess"; }
done
```

If either is missing, stop. Don't create paths, don't fall back to the other drive alone — report it and wait.

## Step 2 — Identify the re-encode on Crucial X9

Single-movie mode (called right after a `4k-hevc-reencoding` completion + track-parity check passes):

```bash
SRC_DIR="/Volumes/Crucial X9/4K Movies/<Movie Name (YYYY)>"
REENCODE=$(find "$SRC_DIR" -maxdepth 1 -iname "* 2160p HEVC*.mkv" -not -name "._*")
[ -n "$REENCODE" ] || { echo "No '2160p HEVC' file in $SRC_DIR — nothing to sync"; exit; }
```

Bulk/backlog mode (sweep the whole drive for anything already re-encoded but not yet synced — the folder still existing on Crucial X9 at all means it hasn't been synced, since a synced folder is deleted entirely):

```bash
find "/Volumes/Crucial X9/4K Movies" -mindepth 2 -maxdepth 2 -iname "* 2160p HEVC*.mkv" -not -name "._*"
# each result's parent directory is one candidate; run steps 3-6 per candidate
```

## Step 3 — Locate the matching library folder

```bash
FOLDER_NAME=$(basename "$SRC_DIR")
MATCHES=$(find "/Volumes/Vhagar/Media/4K Movies" "/Volumes/Vermithor/Media/4K Movies" \
  "/Volumes/Vermithor/Media/4K Family Movies" \
  -mindepth 2 -maxdepth 2 -type d -iname "$FOLDER_NAME")
COUNT=$(echo "$MATCHES" | grep -c .)
```

| Match count | Action |
|---|---|
| 0 | **Skip.** Leave both Crucial X9 files untouched. Report "no library match found for $FOLDER_NAME" so the user can decide manually — don't create a new library folder, don't guess a letter bucket. |
| 2 (found on both drives) | **Skip.** This is an anomaly (duplicate across libraries). Report it, touch nothing. |
| 1 | Proceed to Step 4. |

## Step 4 — Size guard

```bash
DEST_DIR=$(dirname "$MATCHES")  # the single match
DEST_FILE=$(find "$DEST_DIR" -maxdepth 1 -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.m2ts" \) \
  -not -name "._*" -not -name "*2160p HEVC*" | head -1)
SRC_SIZE=$(stat -f%z "$REENCODE")
DEST_SIZE=$(stat -f%z "$DEST_FILE" 2>/dev/null || echo 0)

if [ -n "$DEST_FILE" ] && [ "$SRC_SIZE" -ge "$DEST_SIZE" ]; then
  echo "ABORT: re-encode ($SRC_SIZE bytes) is not smaller than the library file ($DEST_SIZE bytes) — skipping, flag for manual review (CRF retry needed)"
  exit
fi
```

`*2160p HEVC*` is excluded deliberately: after a partial or repeated run the destination can already hold a re-encode, and matching it would make the script size-guard the new file against itself and then delete it in Step 5.

If `DEST_FILE` is empty (folder exists but has no video in it — unusual), skip the size guard and proceed, but note it in the report.

## Step 5 — Copy, verify, then delete (in this order, never reordered)

```bash
cp -p "$REENCODE" "$DEST_DIR/"
COPIED="$DEST_DIR/$(basename "$REENCODE")"

# Verify 1: byte-identical size
COPIED_SIZE=$(stat -f%z "$COPIED")
[ "$COPIED_SIZE" -eq "$SRC_SIZE" ] || { echo "ABORT: copy size mismatch ($COPIED_SIZE != $SRC_SIZE) — leaving everything, investigate"; exit; }

# Verify 2: playable (catches a truncated-but-right-sized copy from a bad SMB write)
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$COPIED" 2>/dev/null)
[ -n "$DUR" ] || { echo "ABORT: copied file unplayable per ffprobe — leaving everything, investigate"; exit; }

# Verify 3: track parity — the whole point of the encode is that nothing was lost
for t in audio subtitle; do
  a=$(ffprobe -v error -show_streams "$REENCODE" | grep -c "^codec_type=$t")
  b=$(ffprobe -v error -show_streams "$COPIED"   | grep -c "^codec_type=$t")
  [ "$a" -eq "$b" ] || { echo "ABORT: $t parity $a != $b — leaving everything"; exit; }
done

# Only now: delete the old, larger library file (+ its sidecar)
[ -n "$DEST_FILE" ] && rm -f "$DEST_FILE" "$(dirname "$DEST_FILE")/._$(basename "$DEST_FILE")"

# Only now: delete BOTH files on Crucial X9 (original source + the re-encode) and the emptied folder
rm -f "$SRC_DIR"/*.mkv "$SRC_DIR"/._*
rmdir "$SRC_DIR" 2>/dev/null || rm -rf "$SRC_DIR"   # rmdir if truly empty, else clean up leftover junk (.DS_Store etc.)
```

## Step 6 — Report

For each movie processed, state: destination drive/path, freed space (`DEST_SIZE - SRC_SIZE` in the library, plus the full `SRC_SIZE` + original-source size reclaimed on Crucial X9), and total across a batch. List skips separately with their reason (no match / ambiguous match / size guard tripped).

## Common mistakes

| Mistake | Fix |
|---|---|
| Deleting the old library file before the new copy is verified | Always copy → verify size → verify ffprobe → verify track parity → **then** delete. Never reorder. |
| Syncing before the ledger row is written | The original's size dies with it. `smeltr record` first, always. |
| Searching only the two `4K Movies` roots | There are three. `4K Family Movies` on Vermithor was added 2026-08-21. |
| Guessing a letter-bucket folder for a "no match" movie | Don't. Report it and let the user place it — wrong bucket placement is hard to notice later. |
| Replacing a library file with a same-size-or-larger re-encode | The size guard exists because CRF-16 blowups are a known failure mode of the reencoding skill (Broker, Labyrinth both did this). Respect the guard. |
| Deleting the Crucial X9 source before the library copy is confirmed | The Crucial X9 files are the only copies until the destination is verified. Delete order in Step 5 is not optional. |
| Assuming a folder existing on Crucial X9 means it's unsynced | True by construction here — a fully-synced movie's folder is deleted entirely (Step 5) — but if this skill is ever partially run or interrupted, a folder could contain only leftovers. Check for the `2160p HEVC` file specifically (Step 2), not just folder presence. |

## Red flags — STOP

- About to `rm` the library's existing file before `ffprobe` has confirmed the new copy plays → **stop, verify first.**
- About to replace a library file where `SRC_SIZE >= DEST_SIZE` → **stop, this is exactly the Broker/Labyrinth blowup pattern re-entering the library.**
- Found the movie folder on neither drive, or on both → **stop, report, do not guess.**
- Any of the three library roots isn't mounted → **stop, do not proceed with a partial set of roots. A root that is merely unmounted looks exactly like a title that has no library match, and the difference is whether an original gets deleted.**
