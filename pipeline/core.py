#!/usr/bin/env python3
"""
Smeltr -- core data layer for the 4K HEVC re-encode pipeline.

READ-ONLY BY DESIGN. Nothing here writes to the media library, the staging
drive, or any HandBrake process. The only file Smeltr ever writes is its own
ledger, and only through record().

Four sources, in descending order of authority:
  1. ps(1)             -- is an encode actually alive right now
  2. the HandBrake log -- progress, geometry, track counts, start time
  3. the staging drive -- real byte sizes on disk
  4. ledger.jsonl      -- durable history of finished encodes

Authority order matters. A log that ends at "99.9 %" proves nothing if the pid
is gone; a ledger row is history, never live state. Never infer liveness from
a log tail alone -- that bug shipped twice in the shell version of this job.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import Optional

GIB = 1073741824
HOME = os.path.expanduser("~")
# Data lives beside the code so the checkout is self-contained and relocatable.
# Resolved from this file's own location rather than a hardcoded path, so moving
# or cloning the repo does not strand the ledger. Override with SMELTR_DIR.
# The repo root, NOT this package: the ledger, the auth token, the pause flag
# and `queue_overrides.json` all live beside `smeltr`. Resolving this to
# `dirname(__file__)` after the move to `pipeline/` would silently relocate
# the ledger -- the one irreplaceable file here.
SMELTR_DIR = os.environ.get("SMELTR_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
LEDGER = os.path.join(SMELTR_DIR, "ledger.jsonl")

X9 = os.environ.get("SMELTR_X9", "/Volumes/Crucial X9/4K Movies")
# The staging LAYOUT (operator's rule, 2026-09-07): movie folders live in two
# subfolders of the X9, never at its root. `queue/` holds every title waiting
# to encode, encoding, arriving, errored -- everything the pipeline may still
# act on. `complete/` holds a finished encode with its source beside it (the
# no-delete policy keeps both); nothing in the pipeline reads it except to
# know the title is done. The scripts, logs, markers and the bitrate index
# stay at the root, where they always were. A movie folder still found AT the
# root is the pre-2026-09-07 layout: every reader below falls back to it, so
# a deploy mid-encode leaves the running title findable, and .autopilot.sh
# moves such folders into place on every pass (never the one being encoded).
STAGE_DIRNAME = "queue"
COMPLETE_DIRNAME = "complete"


def stage_dir() -> str:
    """$X9/queue. A function, not a constant: the test suites swap core.X9 at
    runtime, and a path frozen at import would keep pointing at the live
    drive while every other reader had moved to the fixture."""
    return os.path.join(X9, STAGE_DIRNAME)


def complete_dir() -> str:
    """$X9/complete. See stage_dir()."""
    return os.path.join(X9, COMPLETE_DIRNAME)
LIBRARY_ROOTS = [
    "/Volumes/Vhagar/Media/4K Movies",
    "/Volumes/Vermithor/Media/4K Movies",
    "/Volumes/Vermithor/Media/4K Family Movies",
]
INDEX_NAME = ".bitrates-4k-combined.json"

# The container extensions a SOURCE may arrive in. `.scan-bitrates.sh` has
# always indexed BOTH .mkv and .mp4, and `.sync-to-library.sh` has always
# resolved a library original as .mkv/.mp4/.m2ts -- but every consumer that
# looked for a source on the staging drive spelled the test `endswith(".mkv")`.
# So a .mp4 remux was ranked in the queue, pulled onto the X9 by
# .replenish-queue.sh, and then invisible to the encoder forever. Skyscraper
# (2018) sat staged and unencodable for three days that way, holding 64 GiB:
# pick_next() classed it "arriving", which is a WAIT, so nothing went red and
# nothing halted -- it just silently fell through to the next title.
#
# The OUTPUT is still always .mkv (HandBrake runs -f av_mkv). This tuple is
# about what goes IN. `.autopilot.sh` (start_encode + library_path_of) carries
# the same list and must move with it.
SOURCE_EXTS = (".mkv", ".mp4", ".m2ts")

# Titles the user has permanently excluded. Substring match, lowercased, on the
# full path. Kept here so the app and the terminal report can never disagree.
# .replenish-queue.sh on the X9 carries its OWN copy of this tuple (it cannot
# import core) -- a title added here and not there is re-staged from the
# library by the next replenish and encoded again. Keep the two identical.
SKIP = (
    "lord of the rings",
    # Blacklisted by the operator 2026-09-06 -- staging folders rm -rf'd by
    # hand, never to be attempted again.
    "skyscraper (2018)",
    "timecop (1994)",
    "mechanic resurrection (2016)",
    "bloodsport (1988)",  # blacklisted 2026-09-06, mid-pull
)

# The stop condition: encoding pauses once nothing above this remains.
STOP_MBPS = 70.0

# The TARGET BAND, % of source (operator's call, 2026-09-06: 10-70, was 30-80).
# Three consumers must agree on it: .watch-encode.sh kills a projection that
# lands outside it (BAND_LO/BAND_HI there), _verdict() below calls a finished
# file at or above the top `no-saving`, and the dashboard draws it on the
# projection strip. `summary()` carries both numbers (`band_lo`/`band_hi`) for
# any reader that wants them; the page still types its own copy, and
# test_repo_invariants.py::TargetBand is what holds the four renditions to one
# pair of numbers.
# NOTE the absolute plausibility floor OUTLIER_FLOOR_NORM (15.0) sits INSIDE
# this band on purpose: a 10-15% encode is in band for the ladder (it will
# finish rather than be killed) and still `suspect` for the verdict (a human
# looks before the original is deleted). The band is a target; the floor is a
# deletion policy.
BAND_LO = 10.0
BAND_HI = 70.0

# Hand overrides from the dashboard: titles to skip, and titles to encode
# first. One JSON file beside the ledger, written ONLY through
# save_overrides() (atomic replace), so the driver can never see a
# half-written file mid-cycle. Semantics are deliberately soft: a skipped
# title stays visible in every view, marked, and is excluded from the pick
# and from every queue total; deleting the file restores stock behaviour.
OVERRIDES = os.path.join(SMELTR_DIR, "queue_overrides.json")

# The CRF menu. It lives HERE, not in the dashboard, because the driver now
# reads a hand-picked CRF out of the overrides file (`smeltr crf <title>`) --
# so the set of values a person may choose is a decision-path fact, and one
# widened in server.py alone would let the page offer a rung the driver would
# refuse. dashboard/server.py imports this tuple rather than keeping its own.
#
# The menu IS the ladder, exactly: .watch-encode.sh maps 14->16->18->20->22 when
# a projection runs too big and 14->12->10 when it runs too small. Nothing
# outside 10..22 belongs here -- a blowup at an off-ladder rung is auto-killed
# and never retried, which is a dead end wearing the costume of a choice.
#
# THE LADDER PIVOTS ON CRF_DEFAULT, so the two move together (2026-09-03,
# 16 -> 14). The pivot is the one rung both arms leave from; every other rung
# is one-directional. Moving this constant without re-anchoring
# .watch-encode.sh makes the default a DOWN-ONLY rung, so the first too-big
# kill of a default encode exhausts to none-too-big with nothing left to try
# -- and a lower CRF makes a BIGGER file, which is exactly the direction the
# move to 14 makes more likely.
#
# Consequence of a hand-picked start rung, accepted: the watcher cannot tell
# "started at 20 by hand" from "laddered up to 20", so the opposite-direction
# rule still applies. A hand-picked 20 that comes in TOO SMALL exhausts to
# none-too-small (the ERROR state) rather than stepping back down -- and since
# the pivot moved, a hand-picked 16 that comes in too small does too.
CRF_CHOICES = (10, 12, 14, 16, 18, 20, 22)
CRF_DEFAULT = 14

# Pause-after-current, from the dashboard: an empty flag file beside the
# ledger. While it exists next_title.py answers exit 3 -- the driver's
# existing wait-and-recheck path -- so the running encode still finishes,
# records and syncs, but no new encode starts. Deleting the file resumes
# within one driver wait (300 s). A flag file, not an overrides key:
# .replenish-queue.sh parses the overrides JSON and must keep staging
# while paused (pausing frees the CPU, not the disk).
PAUSE_FLAG = os.path.join(SMELTR_DIR, "pause")

# Per-title encoder overrides from the dashboard: which encoder (and quality)
# a title encodes with. One JSON file beside the ledger, written ONLY through
# save_encoder_overrides() (atomic replace, same discipline as OVERRIDES).
# The default -- and the fallback on ANY failure to read or validate -- is the
# proven software path, x265_10bit at CRF_DEFAULT. The hardware path exists because
# the M1 Max media engine encodes 4K 10-bit HEVC at ~20x the speed of x265,
# but with materially worse quality-per-bit: the 2026-08-25 A/B measured a
# VMAF ceiling of ~84 on grain-heavy film at ANY size, so vt_h265_10bit is a
# per-title choice for clean digital/animated sources, never a blanket switch.
ENCODER_OVERRIDES = os.path.join(SMELTR_DIR, "encoder_overrides.json")

# The full menu of encoders the pipeline may run, each with its selectable
# quality values. x265 quality is CRF (lower = better); VideoToolbox quality
# is CQ on Apple's reversed 0-100 scale (higher = better). The two scales are
# NOT comparable -- never map one onto the other. VT values are provisional
# pending the clean-digital half of the A/B (grain-heavy is already measured:
# CQ 65 spends the full source bitrate for VMAF 84).
# NOTE the x265 menu IS CRF_CHOICES, not a second copy of it. The rungs the
# page offers must be rungs .watch-encode.sh can ladder to, and a duplicated
# tuple drifts the moment the pivot moves again (16 -> 14 already happened).
ENCODER_CHOICES: dict = {
    "x265_10bit": CRF_CHOICES,
    # CQ 50-100 in steps of 5 (operator's call, 2026-09-06; was 50-70).
    # Apple's scale runs 0-100, HIGHER = better = bigger file.
    "vt_h265_10bit": tuple(range(50, 101, 5)),
}
# Per-encoder default quality for a start that names an encoder but no
# quality. NEVER derived from menu position: index [0] is the BEST x265
# CRF and the WORST VideoToolbox CQ -- the same expression means opposite
# things on the two scales. x265 tracks CRF_DEFAULT so the pivot cannot
# move under the ladder; VT's 70 is the pivot .watch-encode.sh leaves from
# (60 -> 70 -> 75 -> back to 70 on 2026-09-06, operator's call; the two MUST move
# together, exactly like CRF_DEFAULT and the x265 ladder).
DEFAULT_QUALITIES: dict = {"x265_10bit": CRF_DEFAULT, "vt_h265_10bit": 70}
# THE GLOBAL DEFAULT IS VIDEOTOOLBOX CQ 70 (operator's call, 2026-09-06, restated 23:58; was
# x265 CRF 14). Every title with no encoder override starts here. With no VT
# rows in the ledger the relative check in _verdict() has no baseline and only
# the absolute floor applies (the "first encodes on a new encoder are suspect"
# gate was removed the same day -- see _verdict()); under the no-delete policy
# a `good` VT encode is recorded `--kept` and left beside its source.
DEFAULT_ENCODER = "vt_h265_10bit"
DEFAULT_QUALITY = DEFAULT_QUALITIES[DEFAULT_ENCODER]
# Ledger rows written before the `encoder` field existed are x265 -- the ONLY
# encoder that existed then. This is what a missing field means, and it must
# never follow DEFAULT_ENCODER: the day the default moved to VideoToolbox,
# 34 software rows would otherwise have become the hardware baseline.
LEGACY_ENCODER = "x265_10bit"


def paused() -> bool:
    # Fail CLOSED: waiting is always the safe direction. os.path.exists
    # answers False on ANY OSError, so an unreadable SMELTR_DIR (the silent-
    # blindness class watchdog.sh exists for) would read as "not paused" and
    # resume a pipeline the operator believes is stopped. Never raise either:
    # an exception would escape next_title.main() and exit non-zero, which
    # the driver reads as the stop condition.
    try:
        os.stat(PAUSE_FLAG)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


# THE STAGING-DRIVE FLOOR (operator's rule, 2026-09-08): a new encode may
# start only while the X9 has at least this much free. "100GB" read as GiB --
# the stricter of the two readings by 7 GiB, and every other figure on the
# page is GiB. Under it next_title.py answers exit 3 (a WAIT, never the stop
# condition) so the running encode still finishes and the next one starts by
# itself once space is freed; the driver logs ONE stamped `LOW SPACE:` line
# per episode, which is what emails the operator. The replenisher is not
# gated here: its own ENCODE_RESERVE (150 GiB on top of the source) already
# holds above this floor.
LOW_SPACE_FLOOR_BYTES = 100 * 1024**3


def x9_free_bytes() -> Optional[int]:
    """Free bytes on the staging drive, or None when it cannot be stat'd.

    None, never 0: an unmounted or unreadable X9 is a different fact from a
    full one, and 0 would trip the floor and send the operator freeing space
    on a drive that is simply absent. The offline/no-source paths own that
    failure."""
    try:
        st = os.statvfs(X9)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def low_space() -> tuple[bool, Optional[int]]:
    """(blocked, free_bytes). Blocked iff the drive is readable AND under
    LOW_SPACE_FLOOR_BYTES."""
    free = x9_free_bytes()
    return (free is not None and free < LOW_SPACE_FLOOR_BYTES), free


def set_paused(on: bool) -> None:
    if on:
        with open(PAUSE_FLAG, "a", encoding="utf-8"):
            pass
    else:
        try:
            os.unlink(PAUSE_FLAG)
        except FileNotFoundError:
            pass


_EMPTY_OV = {"skip": [], "priority": [], "crf": {}, "corrupt": False}


def load_overrides() -> dict:
    """{"skip": [titles], "priority": [titles], "crf": {title: int}}.
    Tolerant of a missing or malformed file -- the driver must never crash on
    a UI-written file."""
    try:
        with open(OVERRIDES, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError:
        return dict(_EMPTY_OV)
    except ValueError:
        # Present but unparseable: fall back to stock order but SAY SO --
        # summary() carries the flag and both views raise a loud banner,
        # because silently dropping the user's skips is the worst failure.
        return dict(_EMPTY_OV, corrupt=True)
    if not isinstance(raw, dict):
        return dict(_EMPTY_OV, corrupt=True)

    def strs(key):
        v = raw.get(key)
        return [s for s in v if isinstance(s, str)] if isinstance(v, list) else []

    # A CRF the menu no longer offers is DROPPED, not clamped: the value is
    # about to be handed to HandBrake as -q, and guessing a neighbour rung on
    # the operator's behalf is a multi-hour encode nobody asked for. Falling
    # back to CRF_DEFAULT is the same thing the row would have done with no
    # override at all, which is the only safe reading of an unreadable one.
    crf = raw.get("crf")
    crfs = {}
    if isinstance(crf, dict):
        for title, value in crf.items():
            if (
                isinstance(title, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value in CRF_CHOICES
            ):
                crfs[title] = value
    return {
        "skip": strs("skip"),
        "priority": strs("priority"),
        "crf": crfs,
        "corrupt": False,
    }


def planned_crf(title: str) -> int:
    """The CRF this title's NEXT encode starts at -- the hand-picked value if
    there is one, otherwise CRF_DEFAULT.

    Three callers, and they must agree or the page lies: the queue row the
    dashboard renders, the dashboard's own start button, and `smeltr crf`,
    which is what .autopilot.sh asks before it spawns HandBrake. Matched
    case-insensitively, like every other override key.

    The CRF LADDER still outranks this. A retry after an auto-kill is the
    watcher's call, not a plan made hours earlier against a projection that
    has since been measured and rejected.
    """
    low = title.lower()
    for key, value in load_overrides()["crf"].items():
        if key.lower() == low:
            return value
    return CRF_DEFAULT


def save_overrides(
    skip: list[str], priority: list[str], crf: Optional[dict] = None
) -> None:
    """Atomic write via os.replace: the driver reads this file between
    cycles, and a torn read must be impossible, not merely unlikely.

    `crf=None` means CARRY THE EXISTING MAP FORWARD. Three callers predate the
    CRF map and pass two arguments; making them pass a third they do not care
    about is how a skip click silently resets every hand-picked CRF. Every
    caller runs under the server's _ov_lock, so the read-modify-write here
    cannot interleave with another write.
    """
    if crf is None:
        crf = load_overrides()["crf"]
    os.makedirs(SMELTR_DIR, exist_ok=True)
    tmp = OVERRIDES + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"skip": skip, "priority": priority, "crf": crf},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        # fsync before the replace: os.replace alone guarantees atomicity,
        # not durability, and a crash could leave a zero-length file that
        # fails open to "nothing is skipped".
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, OVERRIDES)


def load_encoder_overrides() -> dict:
    """{"map": {title: {"encoder": str, "quality": num}}, "corrupt": bool}.

    Tolerant like load_overrides(): the driver reads this through
    encoder_for() on every start and must never crash on a UI-written file.
    Entries that name an unknown encoder or an off-menu quality are DROPPED
    here (falling back to the x265 default for that title), because the only
    thing worse than ignoring a hand-set override is spawning HandBrake with
    a quality number that means something else on the other encoder's scale.
    """
    try:
        with open(ENCODER_OVERRIDES, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError:
        return {"map": {}, "corrupt": False}
    except ValueError:
        return {"map": {}, "corrupt": True}
    if not isinstance(raw, dict):
        return {"map": {}, "corrupt": True}
    out = {}
    for title, v in raw.items():
        if not (isinstance(title, str) and isinstance(v, dict)):
            continue
        enc, q = v.get("encoder"), v.get("quality")
        # ints only: 16.0 == 16 would pass a bare membership test, and the
        # stated invariant is that encoder_for() can only return a menu entry.
        if enc in ENCODER_CHOICES and isinstance(q, int) \
                and not isinstance(q, bool) and q in ENCODER_CHOICES[enc]:
            out[title] = {"encoder": enc, "quality": q}
    return {"map": out, "corrupt": False}


def save_encoder_overrides(mapping: dict) -> None:
    """Atomic + fsync'd, exactly like save_overrides(), and for the same
    reason: the driver reads this file between cycles."""
    os.makedirs(SMELTR_DIR, exist_ok=True)
    tmp = ENCODER_OVERRIDES + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ENCODER_OVERRIDES)


def encoder_for(title: str) -> tuple:
    """(encoder, quality) for a title: its override, else the x265 default.

    Case-insensitive on the folder name, matching how skip/priority match.
    Any failure -- missing file, corrupt file, invalid entry -- answers the
    default: the proven software path is the fallback in every direction.
    """
    ov = load_encoder_overrides()["map"]
    want = title.lower()
    for t, v in ov.items():
        if t.lower() == want:
            return v["encoder"], v["quality"]
    return DEFAULT_ENCODER, DEFAULT_QUALITY

# ---------------------------------------------------------------- log parsing

# HandBrake writes progress with carriage returns and only sometimes includes
# the fps/ETA tail, so both shapes have to parse.
PROGRESS_RE = re.compile(
    r"task (\d+) of (\d+), *([\d.]+) *%"
    r"(?: *\( *([\d.]+) fps, avg ([\d.]+) fps, ETA ([0-9hms]+) *\))?"
)
DEST_RE = re.compile(r'"File"\s*:\s*"([^"]+)"')
START_RE = re.compile(r"Starting work at: (.+)")
CRF_RE = re.compile(r"Rate Control / qCompress\s*:\s*CRF-([\d.]+)")
# VideoToolbox logs its rate control differently: no "Rate Control" line, but
# the job header carries '+ quality: 60.00 (CQ)' and the JSON job config names
# the encoder. Both live in the log HEAD alongside the lines above.
CQ_RE = re.compile(r"\+ quality: ([\d.]+) \(CQ\)")
ENC_RE = re.compile(r'"Encoder"\s*:\s*"([^"]+)"')
GEOM_RE = re.compile(r"\+ storage dimensions: (\d+) x (\d+)")
SRC_GEOM_RE = re.compile(r"scan: \d+ previews, (\d+)x(\d+)")
# The scan block enumerates the SOURCE's tracks; the job-configuration block
# enumerates what will actually be WRITTEN. Only the second can detect track
# loss, and track parity is the gate that permits deleting an original.
JOB_TRACK_RE = re.compile(r"^\[[0-9:]+\]\s+\* (audio|subtitle) track ", re.M)
DECODE_ERR_RE = re.compile(r"(\d+) decoder errors")


def _read_slice(path: str, nbytes: int, from_end: bool) -> str:
    """Read at most nbytes from one end of a file. Logs reach 20 MB; never slurp."""
    try:
        with open(path, "rb") as fh:
            if from_end:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - nbytes))
            return fh.read(nbytes).decode("utf-8", "replace")
    except OSError:
        return ""


def _eta_seconds(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", text)
    if not m:
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


# HandBrake reports its own auto-crop as top/bottom/left/right. It is the only
# evidence that distinguishes "trimmed dead pixels" from "resampled smaller".
AUTOCROP_RE = re.compile(r"autocrop\s*[=:]\s*(\d+)/(\d+)/(\d+)/(\d+)")


def parse_log(path: str) -> dict:
    """Pull everything useful out of one HandBrake log without reading it all."""
    head = _read_slice(path, 262144, from_end=False)
    tail = _read_slice(path, 131072, from_end=True)

    out: dict = {"log": path}

    m = DEST_RE.search(head)
    out["output_name"] = m.group(1) if m else None

    m = START_RE.search(head)
    out["started_text"] = m.group(1).strip() if m else None

    m = CRF_RE.search(head) or CQ_RE.search(head)
    out["crf"] = float(m.group(1)) if m else None

    # First "Encoder" that names a VIDEO encoder we know: HandBrake's job JSON
    # also carries an "Encoder" key inside AudioList, and it appears FIRST --
    # a build that writes it as a string would otherwise win this search.
    out["encoder"] = next((m.group(1) for m in ENC_RE.finditer(head)
                           if m.group(1) in ENCODER_CHOICES), None)

    m = GEOM_RE.search(head)
    out["geometry"] = f"{m.group(1)}x{m.group(2)}" if m else None
    m = SRC_GEOM_RE.search(head)
    out["source_geometry"] = f"{m.group(1)}x{m.group(2)}" if m else None
    m = AUTOCROP_RE.search(head)
    out["autocrop"] = tuple(int(g) for g in m.groups()) if m else None

    # Track counts come from the scan block, which lists one "+ N, <lang>" line
    # per track under an "audio tracks:" / "subtitle tracks:" header.
    out["src_audio"] = _count_tracks(head, "audio tracks:")
    out["src_subs"] = _count_tracks(head, "subtitle tracks:")
    # Distinguish "no job block yet" (unknown) from "job block says zero tracks"
    # (total loss). `count() or None` collapsed the worst case into silence.
    if "job configuration:" in head:
        kinds = JOB_TRACK_RE.findall(head)
        out["audio"] = kinds.count("audio")
        out["subs"] = kinds.count("subtitle")
    else:
        out["audio"] = out["subs"] = None

    prog = None
    for prog in PROGRESS_RE.finditer(tail.replace("\r", "\n")):
        pass
    if prog:
        out["task"] = int(prog.group(1))
        out["tasks"] = int(prog.group(2))
        out["pct"] = float(prog.group(3))
        out["fps"] = float(prog.group(4)) if prog.group(4) else None
        out["avg_fps"] = float(prog.group(5)) if prog.group(5) else None
        out["eta_s"] = _eta_seconds(prog.group(6))
    else:
        out["pct"] = None

    # HandBrake emits one line per decoder (dca, ac3, hevc...). Reporting the
    # first meant a clean audio decoder masked errors from the video one.
    counts = [int(x) for x in DECODE_ERR_RE.findall(tail)]
    out["decoder_errors"] = max(counts) if counts else None
    return out


def _count_tracks(head: str, header: str) -> Optional[int]:
    """Count the '+ N, ...' lines beneath a scan header. None if header absent."""
    i = head.find(header)
    if i < 0:
        return None
    n = 0
    for line in head[i:].splitlines()[1:]:
        if re.match(r"^\s+\+ \d+,", line):
            n += 1
        elif line.strip().startswith("+ ") or not line.strip():
            break
    return n


# ------------------------------------------------------------ live processes

# ps(1) does not quote arguments, so filenames with spaces run together. The
# only reliable anchors are the literal flag tokens that always follow.
PS_IO_RE = re.compile(r"-i\s+(.+?)\s+-o\s+(.+?)(?:\s+-[a-zA-Z-]|\s*$)")
PS_Q_RE = re.compile(r"-q\s+([\d.]+)")
PS_E_RE = re.compile(r"-e\s+(\S+)")


def _ps_handbrake() -> list[dict]:
    try:
        raw = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_ps(raw)


def _parse_ps(raw: str) -> list[dict]:
    procs = []
    for line in raw.splitlines():
        line = line.strip()
        if "HandBrakeCLI" not in line or line.startswith("grep"):
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        # The EXECUTABLE must be HandBrakeCLI, not merely mentioned in the
        # command line: a shell whose -c script quotes a HandBrakeCLI
        # invocation (a test harness, a grep) otherwise renders as a ghost
        # live card with unexpanded $variables for -i/-o, inflates the
        # queue's encoding count, and pins the header beacon on. Same
        # lesson as the driver's `pgrep -x`. First token only -- the
        # binary's own path never contains a space here, and file ARGUMENTS
        # with spaces come later in the line.
        if os.path.basename(cmd.split(" ", 1)[0]) != "HandBrakeCLI":
            continue
        io = PS_IO_RE.search(cmd)
        if not io:
            continue
        q = PS_Q_RE.search(cmd)
        e = PS_E_RE.search(cmd)
        procs.append(
            {
                "pid": pid,
                "source_name": io.group(1).strip(),
                "output_name": io.group(2).strip(),
                "crf": float(q.group(1)) if q else None,
                "encoder": e.group(1) if e else None,
            }
        )
    return procs


def _alive(pid: int) -> bool:
    """Liveness via signal 0. Never pgrep -f: '(2012)' is a regex group."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _find_in_staging(filename: str) -> Optional[str]:
    """Locate a bare filename inside a staged movie folder. One level deep."""
    if not filename or os.sep in filename:
        return None
    hits = [
        os.path.join(folder_dir(f), filename)
        for f in staged_folders()
        if os.path.isfile(os.path.join(folder_dir(f), filename))
    ]
    # Duplicate basenames genuinely exist in this library. Guessing one would put
    # the wrong file's size in the denominator of a deletion decision.
    return hits[0] if len(hits) == 1 else None


def _size(path: Optional[str]) -> Optional[int]:
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# An implausibly small output is as dangerous as an oversized one, and the plain
# CRF ladder cannot see it: the "good" band is everything under 70%, so a
# routine 69% and a physically improbable 9% both render as "good - let it run".
# That is the sentence a person reads at 1am before deleting a 90 GB original
# that exists nowhere else. So the distribution of encodes we have ACTUALLY
# measured gets a vote, not just the fixed thresholds.
def _area(geom: Optional[str]) -> Optional[int]:
    if not geom:
        return None
    try:
        w, h = geom.lower().split("x")
        return int(w) * int(h)
    except ValueError:
        return None


def crop_factor(src_geom: Optional[str], out_geom: Optional[str]) -> float:
    """Source pixels per output pixel. 1.0 when nothing was cropped.

    Byte ratios are not comparable across aspect ratios. A 2.40:1 film has
    ~26% of its frame auto-cropped away as letterbox, so it encodes far fewer
    pixels and lands at a much lower byte ratio than a 16:9 title of identical
    quality. Comparing raw ratios would fire the outlier alarm on most scope
    films in the library -- and an alarm that cries wolf gets ignored exactly
    when it finally matters.
    """
    sa, oa = _area(src_geom), _area(out_geom)
    if not sa or not oa:
        return 1.0
    return sa / oa


def is_downscale(
    src_geom: Optional[str], out_geom: Optional[str], autocrop: Optional[tuple] = None
) -> bool:
    """True only if width was lost to RESAMPLING rather than to cropping.

    A narrower frame usually means resolution was thrown away, but HandBrake's
    auto-crop is four-sided: it trims dead columns as well as letterbox rows.
    Kubo scanned as `autocrop = 278/278/0/2` and came out 3838 wide from a 3840
    source -- two dead columns, nothing resampled -- which the old width-only
    test called a downscale and halted the driver on after a five-hour encode.

    So subtract the columns auto-crop accounts for and judge the remainder. With
    no autocrop data we cannot tell the two apart, and the conservative answer
    is the one that refuses to delete an original: any width loss counts.
    """
    if not src_geom or not out_geom:
        return False
    try:
        src_w = int(src_geom.lower().split("x")[0])
        out_w = int(out_geom.lower().split("x")[0])
    except ValueError:
        return False
    cropped_w = 0
    if autocrop and len(autocrop) == 4:
        try:
            cropped_w = int(autocrop[2]) + int(autocrop[3])  # left + right
        except (TypeError, ValueError):
            cropped_w = 0
    return out_w < src_w - cropped_w


# Two independent defences, deliberately measured on different scales.
#
# FLOOR is a physical-plausibility check and must be tested against the RAW byte
# ratio. Testing it post-normalisation let a 2.40:1 film at 11.5% of source read
# as routine, because dividing by the crop factor lifted it over the line -- the
# normalisation intended to stop false alarms was silencing true ones.
#
# BASELINE uses the MEDIAN, not the minimum. With min(), a single legitimate
# outlier permanently widens "normal": recording Flight at 9.4% dropped the
# trigger from 14.8% to 5.6%, so the detector was disarmed by the very encode it
# had correctly flagged.
# Recalibrated 2026-08-22 against the first 12 measured encodes.
#
# The floor was 12.0 applied to the RAW ratio, and it fired exactly once, on
# Flight (2012) at 9.4% -- wrongly. That encode is in the ledger carrying
# "VERIFIED by SSIM against the cropped original: 0.9931 @45:00 and 0.9945
# @10:00". Two things were wrong with it:
#
#   1. It tested the RAW ratio. Flight auto-crops 3840x2160 -> 3840x1600 and so
#      discards 26% of its rows; per pixel actually encoded it keeps 12.6%, not
#      9.4%. A plausibility floor is a question about the pixels that were
#      encoded, so it belongs on the NORMALISED ratio. (The relative check
#      below already compares normalised to normalised.)
#   2. 12% sat above what this library legitimately produces. Flight is the
#      thinnest output ever made here -- 8.35 Mb/s for 4K, from a 2K DI upscale
#      with no true 4K detail to spend bits on.
#
# 6.0 normalised is a little under half of Flight's 12.6, i.e. "less than half
# the bitrate of the thinnest encode this job has ever legitimately produced".
OUTLIER_FACTOR = 0.40  # this far under the median is not routine
# Raised 6.0 -> 15.0 on 2026-08-30, and the meaning changed with it: 6.0 asked
# "is this physically possible for 4K at CRF 16", 15.0 asks "is this a
# reduction this job is willing to make unattended". Flight (2012) kept 12.6%
# per retained pixel and that was judged, by the operator, too small to be a
# result worth keeping -- not a plausibility failure, a quality one.
#
# It also has to be ABSOLUTE, because the relative check drifts. `base *
# OUTLIER_FACTOR` falls as the median falls, and on 2026-08-30 Flight crossed
# from `suspect` to `good` by +0.0003 pct-points with no threshold edited by
# anyone -- the baseline simply moved under it. A fixed floor cannot drift.
#
# 15.0 sits in the gap between Flight (12.64% normalised) and the thinnest
# encode this library has produced that IS wanted (Croods, 17.05%). Replaying
# all 23 measured ledger rows moves exactly one verdict.
OUTLIER_FLOOR_NORM = 15.0  # per retained pixel; below this, ask a human
MIN_HISTORY = 3  # below this there is no distribution to speak of


def history_ratios(
    normalised: bool = False,
    hist: Optional[list] = None,
    encoder: Optional[str] = None,
) -> list[float]:
    """output/source as a percentage, for every fully measured past encode.

    normalised=True scales each row by its own crop factor where the ledger
    recorded geometry, so the comparison is per retained pixel on BOTH sides.
    Rows predating geometry capture fall back to their raw ratio.

    encoder=<name> keeps only rows made by that encoder. Rows with no
    recorded encoder are x265: every row before 2026-08-25 was. Software and
    hardware rows have different rate-quality curves, so a baseline that
    blends them moves the outlier line for BOTH.
    """
    rows = ledger() if hist is None else hist
    out = []
    for r in rows:
        if encoder is not None and \
                (r.get("encoder") or LEGACY_ENCODER) != encoder:
            continue
        sb, ob = r.get("source_bytes"), r.get("output_bytes")
        if not sb or not ob:
            continue
        ratio = ob / sb * 100.0
        if normalised:
            ratio *= crop_factor(r.get("source_geometry"), r.get("output_geometry"))
        out.append(ratio)
    return sorted(out)


def _verdict(
    ratio: Optional[float],
    hist: Optional[list[float]] = None,
    norm_ratio: Optional[float] = None,
    downscaled: bool = False,
    encoder: str = DEFAULT_ENCODER,
) -> tuple[str, str]:
    """The quality ladder, a downscale check, and an outlier check.

    ratio      -- projected output / source, by bytes
    norm_ratio -- the same, normalised for auto-cropped pixels; this is the
                  figure compared against history
    encoder    -- who made the output. The size checks are absolute, but the
                  outlier comparison only means anything against SAME-encoder
                  history, and a non-default encoder with no such history is
                  `suspect` by construction: the first hardware encodes get a
                  human, not a baseline borrowed from a different curve.
    """
    if downscaled:
        return "downscale", (
            "RESOLUTION LOST: the output frame is narrower than the "
            "source. This is not letterbox cropping. Do not delete "
            "the original."
        )
    if ratio is None:
        return "unknown", "Too early to project a final size."
    if ratio >= 100:
        return (
            "blowup",
            "Projecting LARGER than the source - kill it and restart at the "
            "next ladder rung.",
        )
    # BAND_HI, not a literal: this is the top of the target band the dashboard
    # draws and the watcher ladders against, and the three must say the same
    # thing. (Was 80; the band moved to 10-70 on 2026-09-06.)
    if ratio >= BAND_HI:
        return (
            "no-saving",
            f"Above the {BAND_LO:.0f}-{BAND_HI:.0f}% target band - kill it and "
            "restart at the next ladder rung.",
        )

    hist = history_ratios(normalised=True, encoder=encoder) \
        if hist is None else hist
    # No "first encodes on a new encoder are suspect" gate any more
    # (operator's call, 2026-09-06: "did I tell you to consider them
    # suspect?"). It existed to hold a DELETION until a human had seen a new
    # encoder's first results; nothing is deleted under the no-delete policy,
    # and an encode that clears the band and the floor is simply done. With
    # no same-encoder history the relative check below has no base and only
    # the floor applies -- exactly how x265 was judged with an empty ledger.
    cmp_ratio = ratio if norm_ratio is None else norm_ratio
    base = statistics.median(hist) if len(hist) >= MIN_HISTORY else None
    # Both tests are on the NORMALISED ratio now, so a heavily auto-cropped
    # title is judged on the pixels it actually encoded rather than punished
    # for the rows it correctly threw away.
    below_floor = cmp_ratio < OUTLIER_FLOOR_NORM
    below_base = base is not None and cmp_ratio < base * OUTLIER_FACTOR
    if below_floor or below_base:
        detail = (
            f"the typical encode in this job keeps {base:.1f}% (median of {len(hist)})"
            if base is not None
            else "implausibly small for a 4K encode"
        )
        if below_floor:
            detail = (
                f"under the {OUTLIER_FLOOR_NORM:.0f}% floor, below which this "
                f"job does not delete an original unattended; " + detail
            )
        cropped = norm_ratio is not None and abs(norm_ratio - ratio) >= 0.05
        lead = f"keeps {ratio:.1f}% of source bytes"
        if cropped:
            lead += f" ({norm_ratio:.1f}% per retained pixel after auto-crop)"
        # Most ledger rows predate geometry capture, so history_ratios() cannot
        # crop-adjust them and returns their raw figure. Comparing an adjusted
        # encode against a partly-unadjusted baseline flatters the outlier, so
        # say so rather than implying the two are like for like.
        caveat = ""
        if cropped and base is not None:
            caveat = (
                " The baseline is only partly crop-adjusted — older ledger "
                "rows have no geometry — so it reads lower than it should "
                "for a cropped title."
            )
        return "suspect", (
            f"UNUSUAL: this encode {lead} — {detail}.{caveat} "
            "Frame width is unchanged, so no resolution was thrown away; "
            "check picture quality on a scene before deleting the original."
        )

    # The ten points under the top of the band, as it always was (70-80 under
    # the old 80 ceiling): a real saving that is close to not worth it. Only
    # ever asks for a human; never authorises a deletion.
    if ratio >= BAND_HI - 10:
        return "thin", "Real but thin saving - worth a human call."
    return "good", "Solid reduction - let it run."


def log_for_output(output_name: str) -> dict:
    """The parsed HandBrake log that wrote `output_name`, or {}.

    Matched by BASENAME on both sides. The autopilot invokes HandBrake with a
    full `-o` path, so the log records a full path, while every caller here has
    a bare directory entry from os.listdir(). Comparing the two raw silently
    never matched: it halted the driver with "no HandBrake log found" after a
    five-hour encode, and left the ledger's geometry/CRF columns null on the
    rows that did get written. live_encodes() keys by basename for the same
    reason -- keep all of them basename-keyed.
    """
    want = os.path.basename(output_name)
    try:
        names = os.listdir(X9)
    except OSError:
        return {}
    for name in names:
        if not (name.startswith(".hb-") and name.endswith(".log")):
            continue
        got = parse_log(os.path.join(X9, name))
        if got.get("output_name") and os.path.basename(got["output_name"]) == want:
            return got
    return {}


def live_encodes() -> list[dict]:
    """Every HandBrake encode actually running right now, with progress."""
    logs = []
    try:
        for name in os.listdir(X9):
            if name.startswith(".hb-") and name.endswith(".log"):
                logs.append(parse_log(os.path.join(X9, name)))
    except OSError:
        pass
    # Key logs and processes by BASENAME. The autopilot passes -i/-o as full
    # paths, and a raw path here made _find_in_staging refuse (it rejects
    # anything containing a separator), which nulled the live sizes and
    # projections and left "folder" as a whole path -- so the queue's
    # encoding badge and the skip guard's title match both missed.
    by_output = {
        os.path.basename(lg["output_name"]): lg for lg in logs if lg.get("output_name")
    }

    # Baselines are per-encoder now, computed lazily: only ONE encode runs at
    # a time, so this is one ledger pass in practice.
    hists: dict = {}
    result = []
    for proc in _ps_handbrake():
        if not _alive(proc["pid"]):
            continue
        src_name = os.path.basename(proc["source_name"])
        out_name = os.path.basename(proc["output_name"])
        lg = by_output.get(out_name, {})
        # The outlier baseline must come from the SAME encoder: software and
        # hardware rows have different rate-quality curves, and a blended
        # median moves the line for both. Memoised per encoder per call.
        enc = proc.get("encoder") or lg.get("encoder") or DEFAULT_ENCODER
        if enc not in hists:
            hists[enc] = history_ratios(normalised=True, encoder=enc)
        hist = hists[enc]
        src = (
            proc["source_name"]
            if os.sep in proc["source_name"] and os.path.isfile(proc["source_name"])
            else _find_in_staging(src_name)
        )
        out = (
            proc["output_name"]
            if os.sep in proc["output_name"] and os.path.isfile(proc["output_name"])
            else _find_in_staging(out_name)
        )
        src_b, out_b = _size(src), _size(out)

        pct = lg.get("pct")
        projected = ratio = None
        # Below 5% the projection is dominated by studio logos and black
        # frames, which encode to almost nothing and flatter the estimate.
        if pct and pct >= 5 and out_b:
            projected = out_b / (pct / 100.0)
            if src_b:
                ratio = projected / src_b * 100.0
        src_geom, out_geom = lg.get("source_geometry"), lg.get("geometry")
        cf = crop_factor(src_geom, out_geom)
        norm = ratio * cf if ratio is not None else None
        code, note = _verdict(
            ratio,
            hist,
            norm,
            is_downscale(src_geom, out_geom, lg.get("autocrop")),
            encoder=enc,
        )

        title = re.sub(r"\s+(Remux-)?2160p.*$", "", src_name).strip()
        result.append(
            {
                "title": title,
                "folder": os.path.basename(os.path.dirname(src)) if src else title,
                "pid": proc["pid"],
                "crf": proc["crf"] if proc.get("crf") is not None else lg.get("crf"),
                "encoder": proc.get("encoder") or lg.get("encoder"),
                "pct": pct,
                "fps": lg.get("fps"),
                "avg_fps": lg.get("avg_fps"),
                "eta_s": lg.get("eta_s"),
                "started_text": lg.get("started_text"),
                "geometry": lg.get("geometry"),
                "source_geometry": lg.get("source_geometry"),
                "audio": lg.get("audio"),
                "subs": lg.get("subs"),
                "src_audio": lg.get("src_audio"),
                "src_subs": lg.get("src_subs"),
                "decoder_errors": lg.get("decoder_errors"),
                "source_bytes": src_b,
                "output_bytes": out_b,
                "projected_bytes": int(projected) if projected else None,
                "ratio_pct": round(ratio, 1) if ratio else None,
                "norm_ratio_pct": round(norm, 1) if norm else None,
                "crop_factor": round(cf, 3),
                "shrink_pct": round(100 - ratio, 1) if ratio else None,
                "verdict": code,
                "verdict_note": note,
                "log": lg.get("log"),
            }
        )
    return result


# --------------------------------------------------------------------- queue

_index_cache: dict = {"mtime": None, "rows": []}


def _index_path() -> str:
    return os.path.join(X9, INDEX_NAME)


def load_index() -> list[dict]:
    """Bitrate index, cached against mtime. Rebuilt by .scan-bitrates.sh."""
    path = _index_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _index_cache["mtime"] == mtime:
        return _index_cache["rows"]
    try:
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh).get("files", [])
    except (OSError, ValueError):
        return []
    _index_cache["mtime"] = mtime
    _index_cache["rows"] = rows
    return rows


def staged_detail() -> list[dict]:
    """Every folder physically on the staging drive, and what is in it.

    Derived from the drive itself, so it stays truthful when the library NAS is
    unreachable -- which is exactly when someone is most likely to conclude the
    job is finished and start deleting.
    """
    rows = []
    for folder in staged_folders():
        d = folder_dir(folder)
        try:
            files = [f for f in os.listdir(d) if not f.startswith("._")]
        except OSError:
            continue
        outs = [f for f in files if "2160p HEVC" in f and f.endswith(".mkv")]
        srcs = [
            f
            for f in files
            if f.endswith(SOURCE_EXTS) and f not in outs and not f.endswith(".partial")
        ]
        src = os.path.join(d, srcs[0]) if srcs else None
        rows.append(
            {
                "folder": folder,
                "source_bytes": _size(src),
                "has_output": bool(outs),
            }
        )
    return rows


def offline_roots() -> list[str]:
    """Library roots that are not currently mounted.

    This is load-bearing. queue() drops any index row whose file is missing, so
    an unmounted NAS produces an EMPTY queue -- rendering byte-for-byte
    identically to "the job is finished". Success and total sensor failure must
    never look the same on the screen someone consults before wiping a drive.
    """
    return [r for r in LIBRARY_ROOTS if not os.path.isdir(r)]


def done_marker(title: str) -> Optional[str]:
    """First line of $X9/.done-<title>, or None.

    Written by .autopilot.sh under the no-delete policy when an encode is
    judged good: the output stays beside its source and the folder moves to
    $X9/complete/. NOT an error -- the title is finished. Unpickable and
    never re-judged, like the error marker; rendered green, not red. A folder
    sitting in complete/ is done even with no marker (the operator may move
    one there by hand), so the replenisher can never pull it back.
    """
    try:
        with open(os.path.join(X9, ".done-" + title), encoding="utf-8") as fh:
            return fh.readline().strip() or "done"
    except OSError:
        pass
    if os.path.isdir(os.path.join(complete_dir(), title)):
        return "done - in complete/"
    return None


# What an EMPTY marker means. The driver writes the reason into the marker
# with one printf; on 2026-09-08 the X9 was at 0 bytes free and three markers
# landed as zero-byte files. The old fallback text was "CRF ladder exhausted"
# -- a guess from the day the marker was invented, and wrong on every one of
# those rows (all three ran VideoToolbox). An empty file says the write
# failed, and the one thing that fails a 200-byte write is a full drive.
EMPTY_ERROR_NOTE = (
    "error marker is empty - the driver could not write the reason "
    "(the staging drive was almost certainly full)"
)


def error_marker(title: str) -> Optional[str]:
    """First line of $X9/.error-<title>, or None when the title is fine.

    Written by .autopilot.sh's error_out() for anything that is NOT a finished
    encode: no source file, a genuine track mismatch at the 120 s gate, an
    output that cannot be evaluated, repeated unexplained deaths. The marker
    is the title's ERROR state: never deleted, never skipped (a skip is the
    operator's click, 2026-08-31), just unpickable and rendered red until a
    human deletes the marker file. An empty marker reads EMPTY_ERROR_NOTE.
    """
    try:
        with open(os.path.join(X9, ".error-" + title), encoding="utf-8") as fh:
            return fh.readline().strip() or EMPTY_ERROR_NOTE
    except OSError:
        return None


def _movie_dirs(parent: str) -> list[str]:
    try:
        return [
            n
            for n in os.listdir(parent)
            if not n.startswith(".") and os.path.isdir(os.path.join(parent, n))
        ]
    except OSError:
        return []


def staged_folders() -> list[str]:
    """Every movie folder the pipeline may still act on: queue/, plus any
    legacy folder still at the X9 root (see stage_dir). complete/ is NOT staged
    -- a finished title is not work -- and the two layout folders themselves
    are never titles."""
    names = set(_movie_dirs(stage_dir()))
    names.update(
        n for n in _movie_dirs(X9) if n not in (STAGE_DIRNAME, COMPLETE_DIRNAME)
    )
    return sorted(names)


def folder_dir(title: str) -> str:
    """Where a staged title's folder is: queue/<title>, else the legacy root
    folder if one exists, else queue/<title> (the place it WOULD be). One
    resolver for every reader -- verdict, record, the queue, the dashboard --
    so no two of them can disagree about which folder a title means."""
    d = os.path.join(stage_dir(), title)
    if os.path.isdir(d):
        return d
    if title not in (STAGE_DIRNAME, COMPLETE_DIRNAME):
        legacy = os.path.join(X9, title)
        if os.path.isdir(legacy):
            return legacy
        # Last resort: a finished title in complete/. Never staged (see
        # staged_folders), but `verdict`/`record` may be pointed at one by
        # hand -- the 2026-09-07 case was three folders moved to complete/
        # before their ledger rows existed.
        done = os.path.join(complete_dir(), title)
        if os.path.isdir(done):
            return done
    return d


# What a staged folder holds. The integers ARE the queue sort's second key,
# so the names and the numbers must stay together -- and the numbers are
# ordered by "how soon does this title need the encoder", NOT by pipeline
# stage: READY is first because rank 1 of the queue table has to be the title
# that starts next. An OUTPUT folder has already had its turn and is waiting
# on judge/record/sync, so it sorts below the row it would otherwise displace.
STAGE_READY = 0  # a source file and no output -- this is what pick_next starts
STAGE_OUTPUT = 1  # a *2160p HEVC*.mkv exists: encoding now, or awaiting sync
STAGE_ARRIVING = 2  # only a .partial: a replenish or stage pull still landing
STAGE_NOSOURCE = 3  # a visible folder with neither. See SOURCE_EXTS.


def staged_state(folder: str) -> Optional[int]:
    """Which STAGE_* the folder on the drive is in, or None if it is not there.

    ONE reading of the drive for three callers -- pick_next()'s predicate,
    queue()'s ordering, and the dashboard's status column -- because these
    were drifting renditions of the same listdir and the drift is invisible:
    a folder classed "arriving" by one and "ready" by another shows a bar on
    the page for a title the driver will never start.
    """
    try:
        files = [f for f in os.listdir(folder_dir(folder)) if not f.startswith("._")]
    except OSError:
        return None
    if any("2160p HEVC" in f and f.endswith(".mkv") for f in files):
        return STAGE_OUTPUT
    if any(f.endswith(SOURCE_EXTS) and not f.endswith(".partial") for f in files):
        return STAGE_READY
    if any(f.endswith(".partial") for f in files):
        return STAGE_ARRIVING
    return STAGE_NOSOURCE


# The queue costs one SMB stat() per candidate -- ~23 s cold across a mounted
# NAS. Its contents change when an encode finishes or the drive is restaged,
# i.e. on the order of hours, so it is cached far longer than the live panel.
# Liveness (which title is ENCODING) is re-stamped on every read, so the cheap
# fast-moving part stays current while the expensive part does not re-run.
QUEUE_TTL = 90.0
_queue_cache: dict = {"at": 0.0, "key": None, "rows": []}


def queue(
    min_mbps: float = STOP_MBPS,
    live: Optional[list] = None,
    hist: Optional[list] = None,
) -> list[dict]:
    """Everything still above the stop threshold, highest bitrate first.

    `live` and `hist` are injectable so one snapshot can compute them once;
    building a payload used to re-run ps(1) and re-read every log several times.
    """
    staged = {n.lower() for n in staged_folders()}
    live = live_encodes() if live is None else live
    hist = ledger() if hist is None else hist
    encoding = {e["folder"].lower() for e in live}
    done = {e["title"].lower() for e in hist}

    # Dedup on the full source path, not the folder name. Two distinct files can
    # share a folder basename (e.g. ".../O/Once Upon a Time in Hollywood (2019)"
    # and ".../Once Upon a Time in Hollywood (2019)"); keying on the name alone
    # made encoding one of them silently hide BOTH from the queue.
    done_paths = {r["source_path"] for r in hist if r.get("source_path")}

    offline = offline_roots()
    rows = []
    for rec in load_index():
        path = rec.get("path", "")
        low = path.lower()
        base = os.path.basename(path)
        if any(s in low for s in SKIP):
            continue
        if "2160p hevc" in base.lower():
            continue
        mbps = (rec.get("overall_bitrate") or 0) / 1e6
        if mbps < min_mbps:
            continue
        folder = os.path.basename(os.path.dirname(path))
        if path in done_paths:
            continue
        # Legacy rows carry no source_path, so fall back to the title match --
        # but only when no path-keyed row already covers this file.
        if not done_paths and folder.lower() in done:
            continue
        # A row whose root is offline is dropped -- UNLESS the title is
        # staged on the X9. The staging copy is byte-for-byte the library
        # original and is what actually gets encoded, so a NAS outage must
        # not stop the encode side of the pipeline: the driver keeps working
        # through the staged titles and only the record/sync side waits
        # (autopilot defers it and retries). Size comes from the staged copy
        # -- same bytes, and the offline path cannot be statted. Halting
        # encodes on a mount blip cost 4h16m on 2026-08-31.
        root_offline = any(path.startswith(r.rstrip("/") + os.sep) for r in offline)
        staged_here = folder.lower() in staged
        if root_offline and not staged_here:
            continue
        if not root_offline and not os.path.exists(path):
            continue
        size = (
            _size(os.path.join(folder_dir(folder), os.path.basename(path)))
            if root_offline
            else _size(path)
        )
        rows.append(
            {
                "title": folder,
                "mbps": round(mbps, 1),
                "bytes": size,
                "staged": staged_here,
                "encoding": folder.lower() in encoding,
                "location": _library_of(path),
            }
        )
    ov = load_overrides()
    skips = {t.lower() for t in ov["skip"]}
    pri = {t.lower(): i for i, t in enumerate(ov["priority"])}
    crfs = {t.lower(): v for t, v in ov["crf"].items()}
    for r in rows:
        low_t = r["title"].lower()
        r["skipped"] = low_t in skips
        r["pinned"] = (not r["skipped"]) and low_t in pri
        # The row carries the planned START rung and whether a human chose
        # it. Both, not just the number: the page renders a hand-picked 16
        # differently from the default 16, and only the flag can tell them
        # apart once the value is the same.
        r["crf"] = crfs.get(low_t, CRF_DEFAULT)
        r["crf_set"] = low_t in crfs
        # The ladder's ERROR state rides the row so every view (queue tab,
        # report, pick) reads ONE source. Checked regardless of staged: a
        # marker whose folder was hand-removed still needs its red row --
        # the state ends when the human deletes the marker, not before.
        note = error_marker(r["title"])
        r["error"] = note is not None
        r["error_note"] = note
        dnote = done_marker(r["title"])
        r["done"] = dnote is not None
        r["done_note"] = dnote
        # One listdir per staged folder, cached onto the row: pick_next() and
        # the sort below both need it, and the dashboard renders it.
        r["stage_state"] = staged_state(r["title"]) if r["staged"] else None

    # ---- QUEUE ORDER (2026-09-06, operator's rule: "sort it by queue order
    # ALWAYS without exception"). Rank 1 is WHAT ENCODES NEXT.
    #
    # It used to be plain descending bitrate, which is the job's ranking key
    # but is NOT the order titles run in: on 2026-09-06 the table's rank 1
    # was Skyscraper while the driver was encoding Bloodsport at rank 47,
    # because ranks 1-3 were variously unencodable and nothing on screen
    # said so. A rank column that does not predict the next title is a rank
    # column a person has to reverse-engineer every time they look at it.
    #
    # Bitrate still decides everything WITHIN a band -- and it is still what
    # .replenish-queue.sh pulls by, which is untouched by this. The bands:
    _ENCODING, _STAGED, _LIBRARY, _DONE, _ERRORED, _SKIPPED = 0, 1, 2, 3, 4, 5

    def _band(r: dict) -> tuple:
        if r["encoding"]:
            return (_ENCODING, 0)
        # error BEFORE skipped, so a title that is both (Little Mermaid was,
        # on 2026-09-06) files under the more serious fact. A skip is a
        # preference; an error is the pipeline reporting it could not finish.
        if r["error"]:
            return (_ERRORED, 0)
        if r.get("done"):
            return (_DONE, 0)
        if r["skipped"]:
            return (_SKIPPED, 0)
        if r["staged"]:
            # STAGE_* orders the drive's own bands: the one that can start
            # now, then an output awaiting sync, then bytes still landing,
            # then a folder holding nothing usable.
            return (_STAGED, r["stage_state"] if r["stage_state"] is not None else STAGE_NOSOURCE)
        return (_LIBRARY, 0)

    # Errored and skipped rows sort LAST rather than staying in place. They
    # used to hold their rank so a skip could never read as a vanished title;
    # they are now lifted onto the Errors tab instead, which says far more
    # loudly that they exist and why. Keeping them here as well would put a
    # dead title between two live ones in the column that claims to be a
    # running order.
    #
    # Pins are the operator's explicit order and win inside a band -- never
    # across one: a pinned LIBRARY title still cannot encode before a staged
    # one, and promising otherwise is the same lie in a new column.
    rows.sort(key=lambda r: (_band(r), pri.get(r["title"].lower(), len(pri)), -r["mbps"]))
    return rows


def queue_cached(
    min_mbps: float = STOP_MBPS,
    live: Optional[list] = None,
    hist: Optional[list] = None,
) -> list[dict]:
    live = live_encodes() if live is None else live
    hist = ledger() if hist is None else hist
    try:
        idx_mtime = os.path.getmtime(_index_path())
    except OSError:
        idx_mtime = None
    try:
        ov_mtime = os.path.getmtime(OVERRIDES)
    except OSError:
        ov_mtime = None
    # Mount state MUST be in the key. Without it, a NAS remount served the
    # cached empty queue for up to 90 s -- restoring the exact "nothing left to
    # encode" illusion, only now with the offline banner gone.
    key = (
        min_mbps,
        idx_mtime,
        ov_mtime,
        len(hist),
        tuple(staged_folders()),
        tuple(offline_roots()),
    )
    now = time.monotonic()
    if _queue_cache["key"] == key and now - _queue_cache["at"] < QUEUE_TTL:
        rows = _queue_cache["rows"]
    else:
        rows = queue(min_mbps, live=live, hist=hist)
        _queue_cache.update(at=now, key=key, rows=rows)
    encoding = {e["folder"].lower() for e in live}
    for r in rows:
        r["encoding"] = r["title"].lower() in encoding
    return rows


def pick_next(rows: list) -> tuple[Optional[dict], set]:
    """The ONE implementation of "which staged title encodes next".

    Both consumers read it -- next_title.py (the driver's pick) and
    server._mark_ready (the dashboard's next_up/ready row). These were two
    deliberately-mirrored renditions of the same predicate, and every new
    rule (the arriving check) had to land in both or the green row promised
    an encode the driver would not start.

    Returns (row, wait_reasons): the first row in queue order that is staged,
    holds a source file in any SOURCE_EXTS container, has no 2160p HEVC
    output yet, and is not hand-skipped -- or None. wait_reasons says why
    staged work was passed over: "arriving" (a folder holding only a
    replenish .partial, or nothing usable) and/or "skipped". A row that is
    both counts as arriving: no source file exists to encode regardless of
    the skip.

    Since 2026-09-06 queue() returns rows already in PICK order, so the
    answer is normally rows[0] or the row just after the live encode. The
    scan is kept because the ordering is a presentation guarantee and this
    is the correctness one -- they must not be able to disagree.

    A LIVE encode's own folder is excluded by the output check -- its
    in-progress file already matches *2160p HEVC*.mkv -- never by pgrep
    (folder names contain "(YYYY)" and pgrep reads the parens as a group).
    """
    reasons: set = set()
    for row in rows:
        if not row.get("staged"):
            continue
        # queue() already read the drive once and put the answer on the row;
        # fall back to reading it here for a caller that built rows by hand.
        st = row.get("stage_state")
        if st is None:
            st = staged_state(row["title"])
        if st is None or st == STAGE_OUTPUT:
            continue  # gone, or already encoded (or encoding), awaiting sync
        if st != STAGE_READY:
            # A replenish pull still landing, or a folder holding nothing we
            # can feed to HandBrake: picking either would make start_encode
            # error out on "no source file".
            reasons.add("arriving")
            continue
        if row.get("done"):
            # Finished and kept in place under the no-delete policy. Not an
            # error and not a wait: the row is simply complete.
            continue
        if row.get("error"):
            # Ladder-exhausted titles wait for a human; re-picking one would
            # loop the same doomed encode forever. Distinct from "skipped":
            # the operator never chose this, the pipeline did.
            reasons.add("errored")
            continue
        if row.get("skipped"):
            reasons.add("skipped")
            continue
        return row, reasons
    return None, reasons


def volume_name(path: str) -> str:
    """Volume a path lives on: /Volumes/Vhagar/Media/... -> Vhagar."""
    parts = path.strip("/").split("/")
    if len(parts) > 1 and parts[0] == "Volumes":
        return parts[1]
    return parts[0] if parts else "?"


def _library_of(path: str) -> str:
    for root in LIBRARY_ROOTS:
        if path.startswith(root.rstrip("/") + os.sep):
            return volume_name(root)
    return "?"


# -------------------------------------------------------------------- ledger


@dataclass
class Entry:
    title: str
    # Full path of the original. The dedup key -- folder basenames are not
    # unique in this library and matching on them hid real files from the queue.
    source_path: Optional[str] = None
    # Source and output geometry, so past encodes can be compared on equal
    # terms once auto-crop is accounted for.
    source_geometry: Optional[str] = None
    output_geometry: Optional[str] = None
    source_bytes: Optional[int] = None
    output_bytes: Optional[int] = None
    audio: Optional[int] = None
    subs: Optional[int] = None
    dest: Optional[str] = None
    # Kept in place under the no-delete policy: encoded and judged, nothing
    # synced, nothing deleted. summary() leaves these out of the RECLAIMED
    # totals -- no original was removed -- and they never carry a dest.
    kept: bool = False
    finished_at: Optional[str] = None
    crf: Optional[float] = 16.0
    # Which encoder produced the row. Old rows carry None (all were x265).
    # Recorded so verdict baselines can tell software and hardware rows apart:
    # vt_h265_10bit output runs larger at like-for-like quality, and mixing the
    # two silently would contaminate history_ratios() a second way.
    encoder: Optional[str] = None
    encode_seconds: Optional[int] = None
    note: str = ""
    # How this row was obtained. Rows reconstructed from a text state file or
    # from a file left on disk are NOT the same evidence as a row written the
    # moment an encode finished, and the UI says so rather than blurring them.
    provenance: str = "live"
    exact: bool = True
    # Set when an encode flagged SUSPECT was independently checked (e.g. SSIM
    # against the original) and accepted.
    verified: Optional[str] = None

    def saved_bytes(self) -> Optional[int]:
        if self.source_bytes and self.output_bytes:
            return self.source_bytes - self.output_bytes
        return None

    def saved_pct(self) -> Optional[float]:
        if self.source_bytes and self.output_bytes:
            return (1 - self.output_bytes / self.source_bytes) * 100.0
        return None


def ledger() -> list[dict]:
    """Full history, newest last. Malformed lines are skipped, not fatal."""
    rows = []
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    for r in rows:
        sb, ob = r.get("source_bytes"), r.get("output_bytes")
        r["saved_bytes"] = (sb - ob) if (sb and ob) else None
        r["saved_pct"] = round((1 - ob / sb) * 100, 1) if (sb and ob) else None
    return rows


def record(entry: Entry) -> None:
    """Append one finished encode. The only write Smeltr ever performs."""
    os.makedirs(SMELTR_DIR, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


# --------------------------------------------------------------------- totals


def summary(
    hist: Optional[list] = None, q: Optional[list] = None, live: Optional[list] = None
) -> dict:
    hist = ledger() if hist is None else hist
    q = queue(hist=hist) if q is None else q
    # Only rows carrying BOTH sizes may contribute to a ratio. Mixing a row
    # with a known output but an unknown original into these sums silently
    # skews every headline percentage.
    paired = [r for r in hist if r.get("source_bytes") and r.get("output_bytes")]
    # A KEPT row (no-delete policy) is an encode, not a reclaim: its original
    # is still on the NAS, so its saving must not be added to bytes freed.
    freed = [r for r in paired if not r.get("kept")]
    reclaimed = sum(r["saved_bytes"] for r in freed)
    src_total = sum(r["source_bytes"] for r in freed)
    out_total = sum(r["output_bytes"] for r in freed)
    kept_rows = len(paired) - len(freed)
    unknown = len(hist) - len(paired)
    offline = offline_roots()
    complete = not offline
    # Injectable like hist and q: two ps(1) reads within one snapshot could
    # disagree -- an encode starting between them yields a payload whose live
    # card and stat cards describe different worlds.
    live = live_encodes() if live is None else live
    # Rows that are SET ASIDE are out of every queue total: a skipped title is
    # work the pipeline will not do, and an errored one is work it has already
    # refused to do until a human clears the marker. Counting either overstates
    # what is left -- and since 2026-09-06 both are lifted off the queue view
    # onto their own tab, so a total that still counted them would disagree
    # with the table directly under it (it did: "132 waiting" over 129 rows).
    # error is checked FIRST so a title that is both lands in one bucket only.
    q_errored = [r for r in q if r.get("error")]
    q_skipped = [r for r in q if r.get("skipped") and not r.get("error")]
    q_active = [r for r in q if not (r.get("error") or r.get("skipped"))]
    queue_bytes = sum(r["bytes"] or 0 for r in q_active)
    # Count RUNNING PROCESSES, not queue rows. A title whose library file is
    # unreachable drops out of the queue, and counting rows then reported
    # "0 encoding" while HandBrake was demonstrably at 99%.
    encoding = len(live)
    staged = staged_detail()
    staged_unencoded = [r for r in staged if not r["has_output"] and r["source_bytes"]]
    # Weighted ratio (total-in vs total-out), NOT the mean of per-title
    # percentages. The two happen to agree today and will diverge; the label
    # downstream says which one this is.
    shrink = (1 - out_total / src_total) * 100 if src_total else None
    # Queue size is SOURCE bytes. What a person actually wants when budgeting
    # NAS space is how much of it comes back, so project it at the rate we have
    # measured rather than making them do it in their head.
    reclaimable = int(queue_bytes * shrink / 100) if shrink is not None else None
    # Job progress measures against the ORIGINAL scope: set-aside bytes stay
    # in the goal, so neither skipping work nor erroring out of it can render
    # as finishing it.
    skipped_bytes = sum(r["bytes"] or 0 for r in q_skipped)
    errored_bytes = sum(r["bytes"] or 0 for r in q_errored)
    goal = (
        int((queue_bytes + skipped_bytes + errored_bytes) * shrink / 100)
        if shrink is not None
        else None
    )
    low, free = low_space()
    return {
        "completed": len(hist),
        "completed_measured": len(paired),
        "completed_unknown_source": unknown,
        "reclaimed_bytes": reclaimed,
        "kept_rows": kept_rows,
        "source_total_bytes": src_total,
        "output_total_bytes": out_total,
        "avg_saved_pct": round(shrink, 1) if shrink is not None else None,
        "queue_count": len(q_active),
        "queue_waiting": max(0, len(q_active) - encoding),
        "queue_skipped": len(q_skipped),
        "queue_skipped_bytes": skipped_bytes,
        "queue_errored": len(q_errored),
        "queue_errored_bytes": errored_bytes,
        "queue_encoding": encoding,
        "queue_bytes": queue_bytes,
        "queue_reclaimable_bytes": reclaimable,
        # `reclaimable` is 0 -- not None -- the moment the queue empties, and 0 is
        # falsy. Guarding on truthiness made the progress figure vanish at exactly
        # 100%: the one moment it is worth reading.
        # Progress is meaningless while any library root is unreachable: the
        # queue collapses to nothing, so the formula returns 100% precisely when
        # the tool has gone blind. Refuse to print a number instead.
        "job_progress_pct": (
            round(reclaimed / (reclaimed + goal) * 100, 1)
            if complete and goal is not None and (reclaimed + goal) > 0
            else None
        ),
        # Folders physically on the drive, NOT queue rows that happen to be
        # staged -- a staged title already in the ledger occupies disk but is
        # filtered out of the queue, and this is the number read before
        # clearing space.
        "staged": len(staged),
        "staged_bytes": sum(r["source_bytes"] or 0 for r in staged),
        "staged_unencoded": len(staged_unencoded),
        "staged_unencoded_bytes": sum(r["source_bytes"] or 0 for r in staged_unencoded),
        "staged_in_queue": len([r for r in q_active if r["staged"]]),
        # Label identifies the ROOT, not just the volume: two roots share
        # Vermithor, and "Vermithor, Vermithor offline" names neither.
        "roots_offline": [f"{volume_name(r)}/{os.path.basename(r)}" for r in offline],
        "library_complete": complete,
        "overrides_corrupt": bool(load_overrides().get("corrupt")),
        "encoder_overrides_corrupt":
            bool(load_encoder_overrides().get("corrupt")),
        # In summary, not only the dashboard payload: `smeltr report` must
        # never render a paused pipeline as a healthy one (same rule as
        # overrides_corrupt -- every view says it, or the state is invisible
        # from exactly the terminal a 1am SSH session uses).
        "paused": paused(),
        # The staging-drive floor, same one-carrier rule as `paused`: the
        # live card, the big toggle, the ready row and `smeltr report` all
        # read these three, so no view can disagree with the driver's wait.
        "low_space": low,
        "x9_free_bytes": free,
        "low_space_floor_bytes": LOW_SPACE_FLOOR_BYTES,
        "stop_mbps": STOP_MBPS,
        "band_lo": BAND_LO,
        "band_hi": BAND_HI,
        "x9_online": os.path.isdir(X9),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
