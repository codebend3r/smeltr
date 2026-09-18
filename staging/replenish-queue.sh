#!/bin/bash
# Rolling-queue replenishment for the 4K HEVC re-encode job.
#
# RULE: keep 10 movie folders staged on the Crucial X9. When the count drops to 9 or fewer,
# COPY (never move) the highest-bitrate eligible 4K movie from the library onto the X9.
# The library keeps its original until that movie's encode finishes and syncs back.
#
# Eligible = in Vhagar or Vermithor "Media/4K Movies", NOT already staged on the X9,
# NOT already re-encoded (filename contains "2160p HEVC"), and NOT on the SKIP list below.
#
# SKIP LIST (never stage, never encode):
#   - Lord of the Rings (all entries) — user directive 2026-08-15
#   - Skyscraper (2018), Timecop (1994), Mechanic Resurrection (2016) — user directive 2026-09-06
#
# Usage: ./.replenish-queue.sh [--dry-run] [--target N]
set -euo pipefail

X9="/Volumes/Crucial X9/4K Movies"
# Dashboard queue overrides (skip list + hand priority), written by Smeltr
# through an atomic replace -- safe to read at any moment.
OV="${SMELTR_DIR:-$HOME/Developer/git/smeltr}/queue_overrides.json"
JSON="/tmp/bitrates-4k-combined.json"
# X9 index override: /tmp is wiped by reboots and took replenishment down on 2026-08-19.
# Prefer the X9 copy; rebuild it with .scan-bitrates.sh when it is missing or stale.
[ -f "$X9/.bitrates-4k-combined.json" ] && JSON="$X9/.bitrates-4k-combined.json"
if [ ! -f "$JSON" ]; then
  echo "bitrate index missing — rebuilding with .scan-bitrates.sh"
  bash "$X9/.scan-bitrates.sh" || { echo "index rebuild failed" >&2; exit 1; }
  JSON="$X9/.bitrates-4k-combined.json"
fi
TARGET=10
# THE STAGING FOLDER HOLDS 10-18 MOVIES, ALWAYS (operator's rule, 2026-09-06).
# TARGET is the trigger (below it, replenish); FILL is how far one run tops
# up, sitting above the trigger so a finished encode does not immediately put
# the count back under it; MAX is the ceiling no run may cross. The old
# "3 per run" cap is gone -- every pull is still gated on free space below.
FILL=14
MAX=18
# Room the ENCODES need, kept free on top of every pull (2026-09-08). The gate
# below used to reserve 10 GiB beside the source, which is room for a pull
# and not for the encode written next to it: the drive reached 0 bytes free
# at 06:43 and five titles errored in an hour. Two encodes' worth at the top
# of the band (~2 x 0.8 x 70 GiB) plus margin.
ENCODE_RESERVE_KB=157286400   # 150 GiB
DRY=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY=true; shift ;;
    --target)   TARGET="${2:?--target needs a value}"; shift 2 ;;
    --target=*) TARGET="${1#*=}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$TARGET" in ''|*[!0-9]*) echo "--target must be a number, got '$TARGET'" >&2; exit 2 ;; esac

# Single-instance lock. Two concurrent runs previously raced on a shared temp file and
# over-staged: one run's pick list was overwritten by another's, so it kept walking down a
# 30-item list and started copying films nobody asked for (2026-08-17, Indecent Proposal
# killed mid-copy at 24/76 GB). Never allow a second instance.
LOCK="$X9/.replenish.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another replenish is running (lock: $LOCK) — exiting"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# Per-run temp file. NEVER a fixed path — that was the actual race.
PICKS=$(mktemp -t replenish-picks) || exit 1
trap 'rm -f "$PICKS"; rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# LAYOUT (2026-09-07): movie folders live in $X9/queue; a pull lands there.
# A folder still at the root is the old layout and counts until the driver
# moves it. complete/ is never a slot: a finished title is not work.
STAGE="$X9/queue"
mkdir -p "$STAGE"
COUNT=$( { find "$STAGE" -mindepth 1 -maxdepth 1 -type d ! -name ".*" 2>/dev/null
           find "$X9" -mindepth 1 -maxdepth 1 -type d ! -name ".*" ! -name queue ! -name complete 2>/dev/null
         } | wc -l | tr -d ' ')
# A staged folder the user has SKIPPED on the dashboard must not hold one of
# the queue slots, or skipping a staged title would never pull the next one in.
if [ -f "$OV" ]; then
  SKIPPED_STAGED=$(python3 - "$X9" "$OV" <<'PYEOF2'
import json, os, sys
x9, ov = sys.argv[1], sys.argv[2]
try:
    sk = {s.lower() for s in json.load(open(ov)).get("skip", []) if isinstance(s, str)}
except Exception:
    sk = set()
def staged(x9):
    for parent in (os.path.join(x9, 'queue'), x9):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for n in names:
            if n.startswith('.') or n in ('queue', 'complete'): continue
            if os.path.isdir(os.path.join(parent, n)): yield n
print(sum(1 for n in staged(x9) if n.lower() in sk))
PYEOF2
  )
  case "$SKIPPED_STAGED" in ''|*[!0-9]*) SKIPPED_STAGED=0 ;; esac
  if [ "$SKIPPED_STAGED" -gt 0 ]; then
    echo "($SKIPPED_STAGED staged folder(s) hand-skipped - not counted as queue slots)"
    COUNT=$((COUNT - SKIPPED_STAGED))
  fi
fi
# An ERRORED staged folder is not a queue slot either. It holds a source (and
# often a finished output) that nothing will encode until a human deletes its
# .error-<title> marker, so counting it keeps the drive "full" of work the
# driver structurally cannot pick. On 2026-09-06 six of seven staged folders
# were errored: the driver idled while this script reported it was above
# threshold. Same reasoning as the hand-skipped discount above.
ERRORED_STAGED=$(python3 - "$X9" <<'PYEOF3'
import os, sys
x9 = sys.argv[1]
def staged(x9):
    for parent in (os.path.join(x9, 'queue'), x9):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for nm in names:
            if nm.startswith('.') or nm in ('queue', 'complete'): continue
            if os.path.isdir(os.path.join(parent, nm)): yield nm
n = 0
for name in staged(x9):
    if os.path.exists(os.path.join(x9, ".error-" + name)):
        n += 1
print(n)
PYEOF3
)
case "$ERRORED_STAGED" in ''|*[!0-9]*) ERRORED_STAGED=0 ;; esac
if [ "$ERRORED_STAGED" -gt 0 ]; then
  echo "($ERRORED_STAGED staged folder(s) in the error state - not counted as queue slots)"
  COUNT=$((COUNT - ERRORED_STAGED))
fi
# A DONE staged folder (no-delete policy: judged good, kept beside its source
# under .done-<title>) is not a queue slot either. The driver never re-picks
# it, so counting it holds the drive "full" of work that is already finished:
# on 2026-09-07 five done folders plus five mis-picks made 10 of 10 and the
# replenisher refused to pull while the driver idled with nothing startable.
DONE_STAGED=$(python3 - "$X9" <<'PYEOF4'
import os, sys
x9 = sys.argv[1]
def staged(x9):
    for parent in (os.path.join(x9, 'queue'), x9):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for nm in names:
            if nm.startswith('.') or nm in ('queue', 'complete'): continue
            if os.path.isdir(os.path.join(parent, nm)): yield nm
n = 0
for name in staged(x9):
    if os.path.exists(os.path.join(x9, ".done-" + name)):
        n += 1
print(n)
PYEOF4
)
case "$DONE_STAGED" in ''|*[!0-9]*) DONE_STAGED=0 ;; esac
if [ "$DONE_STAGED" -gt 0 ]; then
  echo "($DONE_STAGED staged folder(s) done and kept in place - not counted as queue slots)"
  COUNT=$((COUNT - DONE_STAGED))
fi
echo "staged on X9: $COUNT (target $TARGET)"
if [ "$COUNT" -gt $((TARGET - 1)) ]; then
  echo "above threshold — no replenishment needed"
  exit 0
fi

if [ "$COUNT" -lt 0 ]; then COUNT=0; fi
NEED=$((FILL - COUNT))
if [ $((COUNT + NEED)) -gt "$MAX" ]; then NEED=$((MAX - COUNT)); fi
[ "$NEED" -lt 1 ] && NEED=1

# ---- THE DOWNLOAD BUDGET (operator's rule, 2026-09-07) -----------------------
# "The budget has always and only been for the number of files downloaded and
# ready to be encoded, not the actual encoding." `download_budget` beside the
# ledger is an integer; `downloads_done` counts every pull this script lands
# (and every dashboard stage pull that commits). At the budget this script
# pulls NOTHING MORE -- the driver goes on encoding whatever is staged and
# stops on its own when that runs out. The old `encode_budget`/`encode_done`
# pair counted JUDGED ENCODES in .autopilot.sh and wrote the pause flag; that
# was the wrong quantity and is gone. Raise or clear the budget to pull again;
# `echo 0 > downloads_done` starts a new batch.
# Absent files are the NORMAL case (no budget = no cap). Under `set -e` a
# failed `< file` redirection aborts the whole run -- which it did on
# 2026-09-07 14:47, one tick after this landed, with the queue at 2 of 10.
SMELTR_HOME="${SMELTR_DIR:-$HOME/Developer/git/smeltr}"
BUDGET=""; DONE_DL=0
if [ -f "$SMELTR_HOME/download_budget" ]; then
  BUDGET=$(tr -cd '0-9' < "$SMELTR_HOME/download_budget" || true)
fi
if [ -f "$SMELTR_HOME/downloads_done" ]; then
  DONE_DL=$(tr -cd '0-9' < "$SMELTR_HOME/downloads_done" || true)
fi
case "$DONE_DL" in ''|*[!0-9]*) DONE_DL=0 ;; esac
if [ -n "$BUDGET" ]; then
  if [ "$DONE_DL" -ge "$BUDGET" ]; then
    echo "BUDGET REACHED: $DONE_DL of $BUDGET downloads - not pulling (raise or clear download_budget to continue)"
    exit 0
  fi
  if [ "$NEED" -gt $((BUDGET - DONE_DL)) ]; then
    NEED=$((BUDGET - DONE_DL))
    echo "download budget: $DONE_DL of $BUDGET used - capping this run at $NEED"
  fi
fi
echo "need $NEED more (fill to $FILL, ceiling $MAX)"

# Rebuild candidate list fresh each run so newly-synced movies are excluded correctly.
python3 - "$JSON" "$X9" "$NEED" "$OV" <<'PYEOF' > "$PICKS"
import json, os, sys
jsonp, x9, need = sys.argv[1], sys.argv[2], int(sys.argv[3])
ovp = sys.argv[4] if len(sys.argv) > 4 else ''
d = json.load(open(jsonp))
# Already on the drive: queue/ (waiting), complete/ (finished, kept), or a
# legacy folder at the root. Any of the three means never pull it again.
onx9 = set()
for parent in (os.path.join(x9, 'queue'), os.path.join(x9, 'complete'), x9):
    try:
        onx9.update(n.lower() for n in os.listdir(parent) if not n.startswith('.'))
    except OSError:
        pass

# Never stage or encode these, regardless of bitrate. Substring match on the full path, lowercased.
SKIP = ('lord of the rings',
        # operator blacklist 2026-09-06 -- mirrors pipeline/core.py SKIP.
        # Bloodsport (1988) un-blacklisted 2026-09-15; dropped from both copies.
        'skyscraper (2018)', 'timecop (1994)', 'mechanic resurrection (2016)')

# Dashboard overrides: skipped titles are never staged; hand-prioritised
# titles are staged FIRST, in the chosen order, so a reorder on the dashboard
# also decides what gets pulled from the library next.
try:
    ov = json.load(open(ovp)) if ovp and os.path.exists(ovp) else {}
except Exception:
    ov = {}
user_skip = {s.lower() for s in ov.get('skip', []) if isinstance(s, str)}
# Every title the ledger has recorded, by folder name. ledger.jsonl sits
# beside queue_overrides.json; unreadable reads as "nothing recorded", which
# only ever risks a redundant pull, never a missed one.
recorded = set()
try:
    with open(os.path.join(os.path.dirname(ovp), 'ledger.jsonl')) as lf:
        for line in lf:
            try:
                t = json.loads(line).get('title')
            except Exception:
                continue
            if isinstance(t, str):
                recorded.add(t.lower())
except Exception:
    pass
rank = {s.lower(): i for i, s in enumerate(ov.get('priority', []))
        if isinstance(s, str)}

MIN_BPS = 60 * 1000 * 1000
picks = []
for f in d['files']:
    p = f['path']
    base = os.path.basename(p)
    folder = os.path.basename(os.path.dirname(p))
    if any(s in p.lower() for s in SKIP):  # user skip list
        continue
    if folder.lower() in user_skip:       # dashboard skip list
        continue
    if '2160p hevc' in base.lower():      # already re-encoded
        continue
    if folder.lower() in onx9:            # already staged
        continue
    # A title in the ERROR state (its .error-<title> marker exists, even if
    # the folder itself was removed by hand) is unpickable until a human
    # clears the marker -- pulling it fills a slot with ~60 GB that cannot
    # encode. 2026-09-06.
    if os.path.exists(os.path.join(x9, '.error-' + folder)):
        continue
    # A FINISHED title never comes back (2026-09-10). Since the pusher
    # (ops/push-complete.sh) sends a finished encode back to the NAS and
    # removes its X9 folder, the library still lists the ORIGINAL at its
    # full bitrate and the folder is no longer "on the drive" -- the two
    # checks above would pull it again into a slot nothing can encode. The
    # .done- / .pushed- markers outlive the folder, and the ledger is the
    # record of record.
    if os.path.exists(os.path.join(x9, '.done-' + folder)) or \
       os.path.exists(os.path.join(x9, '.pushed-' + folder)):
        continue
    # ...unless the operator pinned it on the dashboard: a `priority` entry
    # is the one way to re-stage a recorded title without editing the ledger.
    if folder.lower() in recorded and folder.lower() not in rank:
        continue
    # Below the stop threshold is never encoded (mirrors core.STOP_MBPS --
    # the fifth hand-synced copy of that number). With every bitrate 0 on
    # 2026-09-07 this loop pulled five titles alphabetically; a threshold
    # here means a broken index pulls NOTHING rather than the wrong things.
    if (f.get('overall_bitrate') or 0) < MIN_BPS:
        continue
    picks.append((p, folder, f.get('overall_bitrate') or 0))
picks.sort(key=lambda t: (rank.get(t[1].lower(), len(rank)), -t[2]))
# Stat LAZILY, best candidate first, and stop at `need`. Every candidate
# used to be stat'd over SMB before the sort -- 1400 round trips, and at
# the ~1 s each the NAS answers under load that is a twenty-minute pick
# during which nothing downloads (2026-09-07 08:24). Now it is `need` stats
# plus however many have moved since the scan.
# OVER SSH, NEVER THE SMB MOUNT (operator's rule, repeated 2026-09-07). The
# size feeds the free-space gate and the exists check drops a title the
# library has moved since the scan; both come from the NAS by ssh, using
# the same host/path map .ssh-xfer.sh pulls with. A stat over the mounted
# share hung this pick for minutes under load.
import shlex, subprocess
def remote_for(path):
    if path.startswith('/Volumes/Vhagar/'):
        return 'crivas@192.168.50.6', '/volume1/Vhagar/' + path[len('/Volumes/Vhagar/'):]
    if path.startswith('/Volumes/Vermithor/'):
        return 'crivas@192.168.50.3', '/volume1/Vermithor/' + path[len('/Volumes/Vermithor/'):]
    return None, None
def ssh_size(path):
    host, rpath = remote_for(path)
    if not host:
        return None
    try:
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
                            'stat -c%s ' + shlex.quote(rpath)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return int(out) if r.returncode == 0 and out.isdigit() else None
chosen = 0
for p, folder, br in picks:
    if chosen >= need:
        break
    sz = ssh_size(p)
    if sz is None:                        # library moved on since the scan, or the NAS did not answer
        continue
    print(f"{br}\t{sz}\t{folder}\t{p}")
    chosen += 1
PYEOF

# Nothing left to stage -> say so in a file the driver can read. Removed the
# moment a pull lands, so a stale one can never outlive a refilled drive.
EMPTY="$X9/.replenish-empty"
if ! grep -q . "$PICKS"; then
  echo "nothing left to stage above the threshold"
  date '+%Y-%m-%d %H:%M:%S' > "$EMPTY"
  exit 0
fi
rm -f "$EMPTY"

while IFS=$'\t' read -r br size folder path; do
  [ -z "${path:-}" ] && continue
  printf 'PICK %.1f Mb/s  %s\n' "$(echo "$br" | awk '{print $1/1000000}')" "$folder"
  if $DRY; then continue; fi
  # Free-space gate: never start a pull the drive cannot hold. Source size
  # plus ENCODE_RESERVE_KB for the encodes written alongside it.
  avail_kb=$(df -k "$X9" | awk 'NR==2{print $4}')
  need_kb=$(( size / 1024 + ENCODE_RESERVE_KB ))
  if [ -n "${avail_kb:-}" ] && [ "$avail_kb" -lt "$need_kb" ]; then
    echo "  SKIPPING $folder - only $((avail_kb/1048576)) GiB free, need $((need_kb/1048576)) GiB (source + $((ENCODE_RESERVE_KB/1048576)) GiB encode reserve)"
    continue
  fi
  # Pull over SSH, not SMB. Benchmarked 2026-08-17: SSH cat 18 MB/s vs SMB cp 9 MB/s.
  # .ssh-xfer.sh stages to .partial and renames only on a byte-count match, so a
  # half-arrived film can never be picked up by the encoder as if it were complete.
  # `< /dev/null` IS THE FIX FOR "one pull per run" (2026-09-07). This loop
  # reads $PICKS on stdin, and .ssh-xfer.sh runs `ssh ... cat` with no `-n`:
  # ssh inherits that stdin and drains the remaining picks into the remote
  # side, so `read` found nothing after the first title and every run ended
  # one pull in -- "need 12 more" pulled Goosebumps and stopped, and the
  # queue sat at 2 of 10 for an hour while the operator asked why.
  if "/Volumes/Crucial X9/4K Movies/.ssh-xfer.sh" pull "$path" "$STAGE/$folder" < /dev/null; then
    echo "  staged OK — library original left in place"
    # One more file downloaded and ready to encode. Counted on the LANDING,
    # never the pick: a failed transfer is cleaned up below and was never a
    # file anyone can encode.
    if [ -n "$BUDGET" ]; then
      DONE_DL=$((DONE_DL + 1)); printf '%s\n' "$DONE_DL" > "$SMELTR_HOME/downloads_done"
      echo "  BUDGET: $DONE_DL of $BUDGET downloads"
      if [ "$DONE_DL" -ge "$BUDGET" ]; then
        echo "  BUDGET REACHED: $DONE_DL downloads - no more pulls until download_budget is raised or cleared"
        break
      fi
    fi
  else
    echo "  TRANSFER FAILED — cleaning up $folder"
    rm -rf -- "${STAGE:?}/${folder:?}"
  fi
done < "$PICKS"
