#!/bin/bash
# The launcher's server-discovery helpers, against fake processes.
#
#   bash tests/test_launcher_pids.sh
#
# These decide whether `stop` can SEE the running dashboard. When it cannot it
# reports "not running", deletes the pidfile, and `start` binds a second server
# to a second port with a second token -- two dashboards, and "the" URL becomes
# ambiguous. That is not hypothetical: the tree split moved the server from
# $DIR/server.py to $DIR/dashboard/server.py while a server was up on the old
# path, and the pattern in server_pids() stopped matching it.
#
# The other edge is worse: honouring a pidfile whose pid has been RECYCLED
# would make `stop` kill an unrelated process. Both are pinned here.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"; kill -9 $LIVE 2>/dev/null' EXIT

# Pull the helper block out of the launcher so this tests shipped code.
awk '/^server_pids\(\)/,/^start\(\)/' "$ROOT/smeltr" | sed '$d' > "$TMP/helpers.sh"
grep -q 'pidfile_pid()' "$TMP/helpers.sh" || { echo "FAIL: could not extract helpers"; exit 1; }

DIR="$TMP/checkout"; mkdir -p "$DIR"
PIDF="$DIR/server.pid"
# shellcheck disable=SC1090
source "$TMP/helpers.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then printf '.'; pass=$((pass+1))
      else printf '\nFAIL %s: got %s want %s\n' "$1" "'$2'" "'$3'"; fail=$((fail+1)); fi; }

# A real process, so kill -0 answers honestly. Its command line is irrelevant:
# `ps` is shadowed below so each case can name the command line it is about.
sleep 600 & LIVE=$!
FAKE_CMD=""
ps()    { printf '%s\n' "$FAKE_CMD"; }
PGREP_OUT=""
pgrep() { [ -n "$PGREP_OUT" ] && printf '%s\n' "$PGREP_OUT"; }

# --- pidfile_pid: what it accepts -------------------------------------------
echo "$LIVE" > "$PIDF"

# The regression. Command line carries the PRE-SPLIT path, which server_pids()
# cannot match; the pidfile is the only thing that still knows about it.
FAKE_CMD="/usr/bin/python3 $DIR/server.py"
ck "pidfile finds a server on the old path"  "$(pidfile_pid)" "$LIVE"

FAKE_CMD="/usr/bin/python3 $DIR/dashboard/server.py"
ck "pidfile finds a server on the new path"  "$(pidfile_pid)" "$LIVE"

# A path that could plausibly exist after another move.
FAKE_CMD="/usr/bin/python3 $DIR/web/ui/server.py --port 8787"
ck "pidfile survives a future move"          "$(pidfile_pid)" "$LIVE"

# --- pidfile_pid: what it refuses -------------------------------------------
# A recycled pid. Alive, but not ours -- killing it would take out a stranger.
FAKE_CMD="/usr/sbin/cupsd -l -f"
ck "recycled pid is refused"                 "$(pidfile_pid)" ""

# Another clone of this repo, running its own dashboard on its own port.
FAKE_CMD="/usr/bin/python3 $TMP/other-clone/dashboard/server.py"
ck "another checkout's server is refused"    "$(pidfile_pid)" ""

# Right path, wrong program: $DIR is on the command line only as an argument.
FAKE_CMD="/usr/bin/tail -f $DIR/server.log"
ck "a non-server holding the dir is refused"    "$(pidfile_pid)" ""

FAKE_CMD="/usr/bin/python3 $DIR/dashboard/server.py"
sleep 600 & DEAD=$!; kill -9 $DEAD 2>/dev/null; wait $DEAD 2>/dev/null
echo "$DEAD" > "$PIDF"
ck "dead pid is refused"                     "$(pidfile_pid)" ""

echo "not-a-number" > "$PIDF"
ck "garbage pidfile is refused"              "$(pidfile_pid)" ""
: > "$PIDF"
ck "empty pidfile is refused"                "$(pidfile_pid)" ""
rm -f "$PIDF"
ck "missing pidfile is refused"              "$(pidfile_pid)" ""

# A negative number would reach `kill -0 -123`, which signals a PROCESS GROUP.
echo "-1" > "$PIDF"
ck "negative pid is refused"                 "$(pidfile_pid)" ""

# --- all_server_pids ---------------------------------------------------------
echo "$LIVE" > "$PIDF"
FAKE_CMD="/usr/bin/python3 $DIR/dashboard/server.py"

PGREP_OUT="$LIVE"
ck "one pid when both agree"                 "$(all_server_pids)" "$LIVE"

PGREP_OUT=""
ck "pidfile alone is enough"                 "$(all_server_pids)" "$LIVE"

# The pattern still stands alone: a server started outside the launcher writes
# no pidfile, and `stop` has always had to find it.
rm -f "$PIDF"; PGREP_OUT="4242"
ck "pattern alone is enough"                 "$(all_server_pids)" "4242"

# Both, naming different pids -- the exact two-servers state stop() must clear.
echo "$LIVE" > "$PIDF"; PGREP_OUT="4242"
ck "both pids, sorted, deduped"              "$(all_server_pids | sort -n | tr '\n' ' ')" \
                                             "$(printf '%s\n4242\n' "$LIVE" | sort -n | tr '\n' ' ')"

PGREP_OUT=""; rm -f "$PIDF"
ck "nothing running is empty"                "$(all_server_pids)" ""

# --- running() ---------------------------------------------------------------
echo "$LIVE" > "$PIDF"
FAKE_CMD="/usr/bin/python3 $DIR/server.py"
ck "running() sees the old-path server"      "$(running && echo yes || echo no)" "yes"

# Stale pidfile, nothing alive: `start` must proceed, not report a dead URL.
echo "$DEAD" > "$PIDF"; PGREP_OUT=""
ck "running() false on a stale pidfile"      "$(running && echo yes || echo no)" "no"

# Started outside the launcher: adopted, and the pid written down.
echo "$DEAD" > "$PIDF"; PGREP_OUT="4242"
running >/dev/null
ck "running() adopts a pattern match"        "$(cat "$PIDF")" "4242"

echo; echo "$pass passed, $fail failed"
[ $fail -eq 0 ]
