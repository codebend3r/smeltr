#!/bin/bash
# The replenisher pulls EVERY pick in a run, and the download budget caps it
# (2026-09-07). Sandboxed: nothing here touches the X9, the NAS or the ledger.
#
# THE BUG THIS PINS: the pull loop reads $PICKS on stdin, and .ssh-xfer.sh
# runs `ssh ... cat` with no `-n`, so ssh inherited that stdin and drained the
# remaining picks. "need 12 more" pulled ONE title and exited, every run, and
# the queue sat at 2 of 10 for an hour. A string match cannot tell one pull
# from twelve -- so the fake transfer below drains its stdin exactly the way
# ssh does, and the test counts landings.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RQ="$REPO/staging/replenish-queue.sh"
[ -f "$RQ" ] || { echo "FAIL: $RQ not found"; exit 1; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
X9="$TMP/x9"; mkdir -p "$X9/queue" "$TMP/home" "$TMP/bin"
export SMELTR_DIR="$TMP/home"; echo '{"skip":[],"priority":[],"crf":{}}' > "$SMELTR_DIR/queue_overrides.json"

# The script under test, pointed at the sandbox. Its pull call is a hard-coded
# X9 path, so that is rewritten too.
sed "s|/Volumes/Crucial X9/4K Movies|$X9|g" "$RQ" > "$TMP/rq.sh"; chmod +x "$TMP/rq.sh"
# A transfer that behaves like ssh: DRAINS STDIN, then lands the file.
cat > "$X9/.ssh-xfer.sh" <<'FAKE'
#!/bin/bash
cat > /dev/null            # what `ssh ... cat` does to the loop's stdin
mkdir -p "$3"; : > "$3/$(basename "$2")"; echo "ssh pull OK: 1 bytes"
FAKE
chmod +x "$X9/.ssh-xfer.sh"
# `ssh` on PATH is a fake NAS. It RUNS the remote command the probe sends,
# against a tree under $TMP/nas, so quoting, the library-root check and the
# HEVC listing are exercised for real. It DRAINS STDIN like real ssh without
# `-n`, so a probe that forgets `-n` empties the candidate list. $SSH_MODE
# makes Vhagar (.6) misbehave: up, unreachable (exit 255, the 2026-10-20
# "No route to host") or hang (a stat stalled under load).
export NAS="$TMP/nas" SSH_MODE="$TMP/ssh_mode"; echo up > "$SSH_MODE"
cat > "$TMP/bin/ssh" <<'FAKESSH'
#!/bin/bash
[ "$1" = -n ] || cat > /dev/null
host=${*: -2:1}; cmd=${*: -1}
if [ "$host" = crivas@192.168.50.6 ]; then
  case "$(cat "$SSH_MODE")" in
    unreachable) echo "ssh: connect to host 192.168.50.6 port 22: No route to host" >&2; exit 255 ;;
    hang) exec sleep 60 ;;
  esac
fi
exec bash -c 'stat() { [ "$1" = -c%s ] && shift; wc -c < "$1" | tr -d " "; }; '"${cmd//\/volume1\//$NAS/volume1/}"
FAKESSH
chmod +x "$TMP/bin/ssh"
# `df` answers from a file this test writes, so the free-space gate is a fact
# the sandbox STATES rather than whatever disk the runner happens to have.
# ENCODE_RESERVE_KB is 150 GiB; a CI box with 14 GiB free skipped every pull
# and all six landing assertions read as "nothing lands" (2026-09-09).
FREE_KB="$TMP/free_kb"; echo 838860800 > "$FREE_KB"   # 800 GiB
cat > "$TMP/bin/df" <<FAKEDF
#!/bin/bash
echo "Filesystem 1024-blocks Used Avail Capacity Mounted on"
echo "sandbox 0 0 \$(cat "$FREE_KB") 1% /"
FAKEDF
chmod +x "$TMP/bin/df"
export PATH="$TMP/bin:$PATH"
# Three library titles above the threshold on Vhagar, and (from section 5)
# a fourth on Vermithor whose name needs quoting. Each exists on the fake NAS.
index(){
  python3 - "$X9/.bitrates-4k-combined.json" "$NAS" "$1" <<'PY'
import json, os, sys
idx, nas, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
files = [f"/Volumes/Vhagar/Media/4K Movies/T/Title {i} (200{i})/Title {i} (200{i}) Remux-2160p.mkv"
         for i in range(1, 4)]
files.append("/Volumes/Vermithor/Media/4K Family Movies/O/Ocean's Eleven, A & B (2004)/"
             "Ocean's Eleven, A & B (2004) Remux-2160p.mkv")
for p in files:
    dst = nas + "/volume1/" + p[len("/Volumes/"):]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w") as f:
        f.write("x" * 1000)
json.dump({"files": [{"path": p, "overall_bitrate": 80_000_000 + i}
                     for i, p in enumerate(files[:n])]}, open(idx, "w"))
PY
}
index 3

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; exit 1; fi; }
run(){ rm -rf "$X9/queue"/* "$X9/.replenish.lock"; "$TMP/rq.sh" > "$TMP/out.txt" 2>&1; echo $?; }

# --- 1. no budget file: every pick lands, nothing aborts ------------------
rm -f "$SMELTR_DIR/download_budget" "$SMELTR_DIR/downloads_done"
rc=$(run)
ck "a run with no budget file does not abort"     "$rc" 0
ck "every pick is pulled, not just the first"     "$(grep -c 'staged OK' "$TMP/out.txt")" 3
ck "three folders landed in queue/"               "$(ls "$X9/queue" | wc -l | tr -d ' ')" 3
ck "no budget line without a budget"              "$(grep -c 'BUDGET' "$TMP/out.txt")" 0

# --- 2. a download budget caps the run and is counted on landing -----------
echo 2 > "$SMELTR_DIR/download_budget"; echo 0 > "$SMELTR_DIR/downloads_done"
rc=$(run)
ck "the budget caps the run at what is left"      "$(grep -c 'staged OK' "$TMP/out.txt")" 2
ck "downloads_done counts the landings"           "$(cat "$SMELTR_DIR/downloads_done")" 2
ck "the run says the budget is reached"           "$(grep -c 'BUDGET REACHED' "$TMP/out.txt")" 1
rc=$(run)
ck "at the budget the next run pulls nothing"     "$(grep -c 'staged OK' "$TMP/out.txt")" 0
ck "and says so"                                  "$(grep -c 'BUDGET REACHED' "$TMP/out.txt")" 1

# --- 3. the free-space gate: no room, no pull, and it says why -------------
# Pinned here because it is what silently emptied this suite on CI: an
# unstubbed gate reads the runner's own disk, and every assertion above
# passes vacuously the day it trips.
rm -f "$SMELTR_DIR/download_budget" "$SMELTR_DIR/downloads_done"
echo 1024 > "$FREE_KB"                       # 1 MiB, under the 150 GiB reserve
rc=$(run)
ck "no room: nothing is pulled"                   "$(grep -c 'staged OK' "$TMP/out.txt")" 0
ck "no room: nothing landed in queue/"            "$(ls "$X9/queue" | wc -l | tr -d ' ')" 0
ck "no room: every pick says why"                 "$(grep -c 'SKIPPING' "$TMP/out.txt")" 3
echo 838860800 > "$FREE_KB"

# --- 4. a FINISHED title never comes back (2026-09-10) ---------------------
# The pusher removes a finished folder from the X9 once its encode is on the
# NAS, but the library index still lists the ORIGINAL at full bitrate. The
# .done-/.pushed- markers and the ledger are what keep it out.
touch "$X9/.done-Title 1 (2001)"
touch "$X9/.pushed-Title 2 (2002)"
echo '{"title": "Title 3 (2003)", "finished_at": "2026-09-10 12:00:00"}' > "$SMELTR_DIR/ledger.jsonl"
rc=$(run)
ck "a done-marked title is not pulled again"      "$(grep -c 'Title 1' "$TMP/out.txt")" 0
ck "a pushed title is not pulled again"           "$(grep -c 'Title 2' "$TMP/out.txt")" 0
ck "a ledger-recorded title is not pulled again"  "$(grep -c 'Title 3' "$TMP/out.txt")" 0
ck "nothing landed"                               "$(ls "$X9/queue" | wc -l | tr -d ' ')" 0
rm -f "$X9/.done-Title 1 (2001)" "$X9/.pushed-Title 2 (2002)" "$SMELTR_DIR/ledger.jsonl"

# --- 5. an unreachable NAS is never a finished library (2026-10-20) -------
# Homebrew python3 under launchd was denied the LAN, every probe failed, and
# the run wrote .replenish-empty, so the driver logged STOP CONDITION and quit
# with 160 titles still waiting. A dead NAS is its own line and no marker.
index 4
date '+%Y-%m-%d %H:%M:%S' > "$X9/.replenish-empty"   # a stale one from earlier
rc=$(run)
ck "both up: the quoted Vermithor title lands"    "$(grep -c "staged OK" "$TMP/out.txt")" 4
ck "both up: a landing clears the marker"         "$([ -e "$X9/.replenish-empty" ] && echo yes || echo no)" no
echo unreachable > "$SSH_MODE"
date '+%Y-%m-%d %H:%M:%S' > "$X9/.replenish-empty"
rc=$(run)
ck "one NAS down: the other still pulls"          "$(grep -c 'staged OK' "$TMP/out.txt")" 1
ck "one NAS down: the failure has its own line"   "$(grep -c 'NAS CHECK FAILED' "$TMP/out.txt")" 1
ck "one NAS down: ssh's own reason is kept"       "$(grep -c 'No route to host' "$TMP/out.txt")" 1
index 3
rc=$(run)
ck "unreachable: nothing is pulled"               "$(grep -c 'staged OK' "$TMP/out.txt")" 0
ck "unreachable: the run says so"                 "$(grep -c 'NAS UNREACHABLE' "$TMP/out.txt")" 1
ck "unreachable: never 'nothing left'"            "$(grep -c 'nothing left' "$TMP/out.txt")" 0
ck "unreachable: no .replenish-empty survives"    "$([ -e "$X9/.replenish-empty" ] && echo yes || echo no)" no

# --- 6. a NAS that answers with its library volume gone --------------------
echo up > "$SSH_MODE"
mv "$NAS/volume1/Vhagar/Media/4K Movies" "$NAS/volume1/Vhagar/Media/4K Movies.gone"
rc=$(run)
ck "volume gone: nothing is pulled"               "$(grep -c 'staged OK' "$TMP/out.txt")" 0
ck "volume gone: the root is named"               "$(grep -c '4K Movies is missing' "$TMP/out.txt")" 1
ck "volume gone: never 'nothing left'"            "$(grep -c 'nothing left' "$TMP/out.txt")" 0
ck "volume gone: no .replenish-empty"             "$([ -e "$X9/.replenish-empty" ] && echo yes || echo no)" no
mv "$NAS/volume1/Vhagar/Media/4K Movies.gone" "$NAS/volume1/Vhagar/Media/4K Movies"

# --- 7. a probe that stalls is cut off, not waited on forever --------------
echo hang > "$SSH_MODE"
export SMELTR_PROBE_TIMEOUT=2
t0=$(date +%s); rc=$(run); t1=$(date +%s)
ck "hang: the run ends well inside the stall"     "$([ $((t1 - t0)) -lt 20 ] && echo yes || echo no)" yes
ck "hang: the stall is named"                     "$(grep -c 'did not finish a size check' "$TMP/out.txt")" 1
ck "hang: no .replenish-empty"                    "$([ -e "$X9/.replenish-empty" ] && echo yes || echo no)" no
unset SMELTR_PROBE_TIMEOUT; echo up > "$SSH_MODE"

# --- 8. an HEVC already in the library folder is skipped by name ----------
# A long listing too: under pipefail a piped `grep -q` SIGPIPEd past ~64 KB.
for i in 1 2 3; do
  d="$NAS/volume1/Vhagar/Media/4K Movies/T/Title $i (200$i)"
  : > "$d/Title $i (200$i) 2160p HEVC.mkv"
  for j in $(seq 1 3000); do : > "$d/extra-padding-file-name-number-$j.nfo"; done
done
rc=$(run)
ck "hevc: the run completes"                      "$rc" 0
ck "hevc: nothing is pulled"                      "$(grep -c 'staged OK' "$TMP/out.txt")" 0
ck "hevc: every candidate says why"               "$(grep -c 'an HEVC file is already' "$TMP/out.txt")" 3
ck "hevc: a reachable, finished library is empty" "$([ -e "$X9/.replenish-empty" ] && echo yes || echo no)" yes
rm -f "$X9/.replenish-empty"

printf '\n%d passed, %d failed\n' "$pass" "$fail"; [ "$fail" -eq 0 ]
