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
# `ssh` on PATH answers the pre-pull size check.
printf '#!/bin/bash\necho 1000\n' > "$TMP/bin/ssh"; chmod +x "$TMP/bin/ssh"
export PATH="$TMP/bin:$PATH"
# Three library titles above the threshold.
python3 - "$X9/.bitrates-4k-combined.json" <<'PY'
import json, sys
files = [{"path": f"/Volumes/Vhagar/Media/4K Movies/T/Title {i} (200{i})/Title {i} (200{i}) Remux-2160p.mkv",
          "overall_bitrate": 80_000_000 + i} for i in range(1, 4)]
json.dump({"files": files}, open(sys.argv[1], "w"))
PY

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1: got '$2' want '$3'"; fail=$((fail+1)); fi; }
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

echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
