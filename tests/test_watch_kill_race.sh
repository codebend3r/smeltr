#!/bin/bash
# The auto-kill must hide the partial BEFORE it kills HandBrake (2026-09-07).
#
# THE BUG THIS PINS. `.watch-encode.sh` used to kill, wait, then `rm` the
# partial. Between HandBrake's death and that `rm` the partial sat on disk
# with no live encoder -- and the driver's 30 s pass reads exactly that as a
# FINISHED encode, because finished_folder()'s only live-encode guard is
# encoding_this(), which goes false the instant the process dies. verdict.py
# then correctly refuses to judge it ("died mid-write", exit 4) and the
# driver used to error the title out, parking a mid-ladder retry behind a
# marker only a human can clear. Minions at CQ 65 on 2026-09-06 (via the
# AppleDouble sidecar) cost 1h35m of idle encoder; Shazam at Q70 on
# 2026-09-07 (via the real file) cost 2h27m.
#
# Narrowing that window is not a fix -- closing it is. The partial is RENAMED
# out of the driver's `*2160p HEVC*.mkv` glob first; HandBrake holds the fd
# and keeps writing to the same inode, so nothing is lost, but from that
# instant there is no name on disk the driver can mistake for an encode.
#
# The test is deterministic rather than a race the harness has to win: the
# fake HandBrake IGNORES SIGTERM, so the watcher's 30 s kill grace becomes a
# 30 s window in which the process is provably still alive. If the rename
# already happened by then, it happened before the kill could complete.
#
# Nothing here touches the staging drive, the ledger, or any real encode.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WE="$REPO/staging/watch-encode.sh"
[ -f "$WE" ] || { echo "FAIL: $WE not found"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; kill -9 $HB $W 2>/dev/null' EXIT
HB=""; W=""
FOLDER="Sandbox (2020)"
mkdir -p "$TMP/sb/$FOLDER"
sed "s|/Volumes/Crucial X9/4K Movies|$TMP/sb|g; s|sleep 60|sleep 1|" "$WE" > "$TMP/we.sh"
chmod +x "$TMP/we.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "PASS $1"; pass=$((pass+1))
      else echo "FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }

# 100 MiB "source", 60 MiB "output" at 12% progress -> 500% of source, a
# blowup, and CRF 16 has a real up-rung (18) so this KILLS rather than FINALs.
dd if=/dev/zero of="$TMP/sb/$FOLDER/src.mkv" bs=1048576 count=100 2>/dev/null
dd if=/dev/zero of="$TMP/sb/$FOLDER/out.mkv" bs=1048576 count=60 2>/dev/null
printf 'task 1 of 1, 12.00 %%\n' > "$TMP/sb/.hb-sandbox.log"

# A HandBrake that ignores SIGTERM, so the watcher's 30 s grace loop runs and
# the "process still alive" half of the assertion is not a timing accident.
bash -c 'trap "" TERM; while :; do sleep 0.2; done' & HB=$!
disown "$HB" 2>/dev/null || true

"$TMP/we.sh" sandbox "$FOLDER" src.mkv out.mkv "$HB" 16 x265_10bit > "$TMP/log.txt" 2>&1 &
W=$!

# --- 1. the partial leaves the glob BEFORE the process dies ---------------
# Poll for the rename. Two strikes are needed before the watcher acts, so
# allow a few ticks; the loop exits the moment the renamed file appears.
seen_renamed=no; seen_alive=no; leaked=no
n=0
while [ "$n" -lt 120 ]; do
  n=$((n+1))
  if [ -e "$TMP/sb/$FOLDER/out.mkv.killing" ]; then
    seen_renamed=yes
    kill -0 "$HB" 2>/dev/null && seen_alive=yes
    break
  fi
  # THE LEAK: the encoder is gone and the un-renamed partial is still there.
  # That is the exact state the driver misreads as a finished encode.
  if ! kill -0 "$HB" 2>/dev/null && [ -e "$TMP/sb/$FOLDER/out.mkv" ]; then
    leaked=yes; break
  fi
  sleep 0.25
done

ck "the partial is renamed out of the *.mkv glob"      "$seen_renamed" yes
ck "the rename lands while HandBrake is still alive"   "$seen_alive"   yes
ck "the encoder never dies with the partial still named .mkv" "$leaked" no
ck "the .mkv name is gone the moment the rename lands" \
   "$([ -e "$TMP/sb/$FOLDER/out.mkv" ] && echo yes || echo no)" no

# --- 2. the kill still completes and still cleans up ----------------------
n=0; until grep -q '^KILLED|' "$TMP/log.txt" 2>/dev/null; do
  n=$((n+1)); [ "$n" -gt 160 ] && break; sleep 0.5
done
ck "the kill still reports KILLED" \
   "$(grep -c '^KILLED|' "$TMP/log.txt" 2>/dev/null)" 1
ck "it still ladders to the next rung" \
   "$(grep '^KILLED|' "$TMP/log.txt" | grep -c 'next: Q 18')" 1
n=0; while kill -0 "$HB" 2>/dev/null && [ "$n" -lt 40 ]; do n=$((n+1)); sleep 0.5; done
ck "the encode is killed"        "$(kill -0 "$HB" 2>/dev/null && echo yes || echo no)" no
ck "the partial is deleted"      "$([ -e "$TMP/sb/$FOLDER/out.mkv" ] && echo yes || echo no)" no
ck "the renamed partial is deleted too" \
   "$([ -e "$TMP/sb/$FOLDER/out.mkv.killing" ] && echo yes || echo no)" no

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
