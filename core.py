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
from dataclasses import dataclass, field, asdict
from typing import Optional

GIB = 1073741824
HOME = os.path.expanduser("~")
# Data lives beside the code so the checkout is self-contained and relocatable.
# Resolved from this file's own location rather than a hardcoded path, so moving
# or cloning the repo does not strand the ledger. Override with SMELTR_DIR.
SMELTR_DIR = os.environ.get("SMELTR_DIR") or os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(SMELTR_DIR, "ledger.jsonl")

X9 = os.environ.get("SMELTR_X9", "/Volumes/Crucial X9/4K Movies")
LIBRARY_ROOTS = [
    "/Volumes/Vhagar/Media/4K Movies",
    "/Volumes/Vermithor/Media/4K Movies",
    "/Volumes/Vermithor/Media/4K Family Movies",
]
INDEX_NAME = ".bitrates-4k-combined.json"

# Titles the user has permanently excluded. Substring match, lowercased, on the
# full path. Kept here so the app and the terminal report can never disagree.
SKIP = ("lord of the rings",)

# The stop condition: encoding pauses once nothing above this remains.
STOP_MBPS = 70.0

# Hand overrides from the dashboard: titles to skip, and titles to encode
# first. One JSON file beside the ledger, written ONLY through
# save_overrides() (atomic replace), so the driver can never see a
# half-written file mid-cycle. Semantics are deliberately soft: a skipped
# title stays visible in every view, marked, and is excluded from the pick
# and from every queue total; deleting the file restores stock behaviour.
OVERRIDES = os.path.join(SMELTR_DIR, "queue_overrides.json")

# Pause-after-current, from the dashboard: an empty flag file beside the
# ledger. While it exists next_title.py answers exit 3 -- the driver's
# existing wait-and-recheck path -- so the running encode still finishes,
# records and syncs, but no new encode starts. Deleting the file resumes
# within one driver wait (300 s). A flag file, not an overrides key:
# .replenish-queue.sh parses the overrides JSON and must keep staging
# while paused (pausing frees the CPU, not the disk).
PAUSE_FLAG = os.path.join(SMELTR_DIR, "pause")


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


def set_paused(on: bool) -> None:
    if on:
        with open(PAUSE_FLAG, "a", encoding="utf-8"):
            pass
    else:
        try:
            os.unlink(PAUSE_FLAG)
        except FileNotFoundError:
            pass


def load_overrides() -> dict:
    """{"skip": [titles], "priority": [titles]}. Tolerant of a missing or
    malformed file -- the driver must never crash on a UI-written file."""
    try:
        with open(OVERRIDES, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError:
        return {"skip": [], "priority": [], "corrupt": False}
    except ValueError:
        # Present but unparseable: fall back to stock order but SAY SO --
        # summary() carries the flag and both views raise a loud banner,
        # because silently dropping the user's skips is the worst failure.
        return {"skip": [], "priority": [], "corrupt": True}
    if not isinstance(raw, dict):
        return {"skip": [], "priority": [], "corrupt": True}

    def strs(key):
        v = raw.get(key)
        return [s for s in v if isinstance(s, str)] if isinstance(v, list) else []
    return {"skip": strs("skip"), "priority": strs("priority"), "corrupt": False}


def save_overrides(skip: list[str], priority: list[str]) -> None:
    """Atomic write via os.replace: the driver reads this file between
    cycles, and a torn read must be impossible, not merely unlikely."""
    os.makedirs(SMELTR_DIR, exist_ok=True)
    tmp = OVERRIDES + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"skip": skip, "priority": priority},
                            ensure_ascii=False, indent=2) + "\n")
        # fsync before the replace: os.replace alone guarantees atomicity,
        # not durability, and a crash could leave a zero-length file that
        # fails open to "nothing is skipped".
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, OVERRIDES)

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

    m = CRF_RE.search(head)
    out["crf"] = float(m.group(1)) if m else None

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


def _ps_handbrake() -> list[dict]:
    try:
        raw = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

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
        io = PS_IO_RE.search(cmd)
        if not io:
            continue
        q = PS_Q_RE.search(cmd)
        procs.append({
            "pid": pid,
            "source_name": io.group(1).strip(),
            "output_name": io.group(2).strip(),
            "crf": float(q.group(1)) if q else None,
        })
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
    try:
        folders = os.listdir(X9)
    except OSError:
        return None
    hits = [os.path.join(X9, f, filename) for f in folders
            if not f.startswith(".") and os.path.isfile(os.path.join(X9, f, filename))]
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


def is_downscale(src_geom: Optional[str], out_geom: Optional[str],
                 autocrop: Optional[tuple] = None) -> bool:
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
            cropped_w = int(autocrop[2]) + int(autocrop[3])   # left + right
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
OUTLIER_FACTOR = 0.40      # this far under the median is not routine
OUTLIER_FLOOR_NORM = 6.0   # per retained pixel; below this, not physically credible
MIN_HISTORY = 3            # below this there is no distribution to speak of


def history_ratios(normalised: bool = False, hist: Optional[list] = None) -> list[float]:
    """output/source as a percentage, for every fully measured past encode.

    normalised=True scales each row by its own crop factor where the ledger
    recorded geometry, so the comparison is per retained pixel on BOTH sides.
    Rows predating geometry capture fall back to their raw ratio.
    """
    rows = ledger() if hist is None else hist
    out = []
    for r in rows:
        sb, ob = r.get("source_bytes"), r.get("output_bytes")
        if not sb or not ob:
            continue
        ratio = ob / sb * 100.0
        if normalised:
            ratio *= crop_factor(r.get("source_geometry"), r.get("output_geometry"))
        out.append(ratio)
    return sorted(out)


def _verdict(ratio: Optional[float], hist: Optional[list[float]] = None,
             norm_ratio: Optional[float] = None,
             downscaled: bool = False) -> tuple[str, str]:
    """The CRF ladder, a downscale check, and an outlier check.

    ratio      -- projected output / source, by bytes
    norm_ratio -- the same, normalised for auto-cropped pixels; this is the
                  figure compared against history
    """
    if downscaled:
        return "downscale", ("RESOLUTION LOST: the output frame is narrower than the "
                             "source. This is not letterbox cropping. Do not delete "
                             "the original.")
    if ratio is None:
        return "unknown", "Too early to project a final size."
    if ratio >= 100:
        return "blowup", "Projecting LARGER than the source - kill it and restart at CRF 20."
    # 80, not 85: this is the top of the 30-80% target band the dashboard draws.
    # Nothing in 12 titles has ever exceeded 71.3%, so this end has never fired --
    # but when it does, the strip and the verdict must say the same thing.
    if ratio >= 80:
        return "no-saving", "Above the 30-80% target band - kill it and restart at CRF 18."

    hist = history_ratios(normalised=True) if hist is None else hist
    cmp_ratio = ratio if norm_ratio is None else norm_ratio
    base = statistics.median(hist) if len(hist) >= MIN_HISTORY else None
    # Both tests are on the NORMALISED ratio now, so a heavily auto-cropped
    # title is judged on the pixels it actually encoded rather than punished
    # for the rows it correctly threw away.
    below_floor = cmp_ratio < OUTLIER_FLOOR_NORM
    below_base = base is not None and cmp_ratio < base * OUTLIER_FACTOR
    if below_floor or below_base:
        detail = (f"the typical encode in this job keeps {base:.1f}% "
                  f"(median of {len(hist)})" if base is not None
                  else "implausibly small for 4K at CRF 16")
        if below_floor:
            detail = (f"under the {OUTLIER_FLOOR_NORM:.0f}% floor below which a 4K "
                      f"CRF-16 encode is not physically credible; " + detail)
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
            caveat = (" The baseline is only partly crop-adjusted — older ledger "
                      "rows have no geometry — so it reads lower than it should "
                      "for a cropped title.")
        return "suspect", (
            f"UNUSUAL: this encode {lead} — {detail}.{caveat} "
            "Frame width is unchanged, so no resolution was thrown away; "
            "check picture quality on a scene before deleting the original.")

    if ratio >= 70:
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
    by_output = {os.path.basename(lg["output_name"]): lg
                 for lg in logs if lg.get("output_name")}

    hist = history_ratios(normalised=True)
    result = []
    for proc in _ps_handbrake():
        if not _alive(proc["pid"]):
            continue
        src_name = os.path.basename(proc["source_name"])
        out_name = os.path.basename(proc["output_name"])
        lg = by_output.get(out_name, {})
        src = (proc["source_name"] if os.sep in proc["source_name"]
               and os.path.isfile(proc["source_name"])
               else _find_in_staging(src_name))
        out = (proc["output_name"] if os.sep in proc["output_name"]
               and os.path.isfile(proc["output_name"])
               else _find_in_staging(out_name))
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
        code, note = _verdict(ratio, hist, norm,
                              is_downscale(src_geom, out_geom, lg.get("autocrop")))

        title = re.sub(r"\s+(Remux-)?2160p.*$", "", src_name).strip()
        result.append({
            "title": title,
            "folder": os.path.basename(os.path.dirname(src)) if src else title,
            "pid": proc["pid"],
            "crf": proc["crf"] if proc.get("crf") is not None else lg.get("crf"),
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
        })
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
        d = os.path.join(X9, folder)
        try:
            files = [f for f in os.listdir(d) if not f.startswith("._")]
        except OSError:
            continue
        outs = [f for f in files if "2160p HEVC" in f and f.endswith(".mkv")]
        srcs = [f for f in files if f.endswith(".mkv") and f not in outs
                and not f.endswith(".partial")]
        src = os.path.join(d, srcs[0]) if srcs else None
        rows.append({
            "folder": folder,
            "source_bytes": _size(src),
            "has_output": bool(outs),
        })
    return rows


def offline_roots() -> list[str]:
    """Library roots that are not currently mounted.

    This is load-bearing. queue() drops any index row whose file is missing, so
    an unmounted NAS produces an EMPTY queue -- rendering byte-for-byte
    identically to "the job is finished". Success and total sensor failure must
    never look the same on the screen someone consults before wiping a drive.
    """
    return [r for r in LIBRARY_ROOTS if not os.path.isdir(r)]


def staged_folders() -> list[str]:
    try:
        return sorted(
            n for n in os.listdir(X9)
            if not n.startswith(".") and os.path.isdir(os.path.join(X9, n))
        )
    except OSError:
        return []


# The queue costs one SMB stat() per candidate -- ~23 s cold across a mounted
# NAS. Its contents change when an encode finishes or the drive is restaged,
# i.e. on the order of hours, so it is cached far longer than the live panel.
# Liveness (which title is ENCODING) is re-stamped on every read, so the cheap
# fast-moving part stays current while the expensive part does not re-run.
QUEUE_TTL = 90.0
_queue_cache: dict = {"at": 0.0, "key": None, "rows": []}


def queue(min_mbps: float = STOP_MBPS, live: Optional[list] = None,
          hist: Optional[list] = None) -> list[dict]:
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
        if any(path.startswith(r.rstrip("/") + os.sep) for r in offline):
            continue
        if not os.path.exists(path):
            continue
        rows.append({
            "title": folder,
            "mbps": round(mbps, 1),
            "bytes": _size(path),
            "staged": folder.lower() in staged,
            "encoding": folder.lower() in encoding,
            "location": _library_of(path),
        })
    ov = load_overrides()
    skips = {t.lower() for t in ov["skip"]}
    pri = {t.lower(): i for i, t in enumerate(ov["priority"])}
    for r in rows:
        low_t = r["title"].lower()
        r["skipped"] = low_t in skips
        r["pinned"] = (not r["skipped"]) and low_t in pri
    # Skipped rows stay IN PLACE in the bitrate ranking -- the views grey
    # them out as disabled rows rather than sinking or dropping them, so a
    # skip can never read as a vanished (or finished) title. Pinned rows come
    # first in the hand-chosen order; everything else keeps the bitrate
    # ranking. (A skip clears any pin, so pinned rows are never skipped.)
    rows.sort(key=lambda r: (pri.get(r["title"].lower(), len(pri)),
                             -r["mbps"]))
    return rows


def queue_cached(min_mbps: float = STOP_MBPS, live: Optional[list] = None,
                 hist: Optional[list] = None) -> list[dict]:
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
    key = (min_mbps, idx_mtime, ov_mtime, len(hist), tuple(staged_folders()),
           tuple(offline_roots()))
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
    finished_at: Optional[str] = None
    crf: Optional[float] = 16.0
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

def summary(hist: Optional[list] = None, q: Optional[list] = None) -> dict:
    hist = ledger() if hist is None else hist
    q = queue(hist=hist) if q is None else q
    # Only rows carrying BOTH sizes may contribute to a ratio. Mixing a row
    # with a known output but an unknown original into these sums silently
    # skews every headline percentage.
    paired = [r for r in hist if r.get("source_bytes") and r.get("output_bytes")]
    reclaimed = sum(r["saved_bytes"] for r in paired)
    src_total = sum(r["source_bytes"] for r in paired)
    out_total = sum(r["output_bytes"] for r in paired)
    unknown = len(hist) - len(paired)
    offline = offline_roots()
    complete = not offline
    live = live_encodes()
    # Hand-skipped rows are OUT of every queue total: a skipped title is work
    # the pipeline will not do, and counting it would overstate what is left.
    q_active = [r for r in q if not r.get("skipped")]
    q_skipped = [r for r in q if r.get("skipped")]
    queue_bytes = sum(r["bytes"] or 0 for r in q_active)
    # Count RUNNING PROCESSES, not queue rows. A title whose library file is
    # unreachable drops out of the queue, and counting rows then reported
    # "0 encoding" while HandBrake was demonstrably at 99%.
    encoding = len(live)
    staged = staged_detail()
    staged_unencoded = [r for r in staged
                        if not r["has_output"] and r["source_bytes"]]
    # Weighted ratio (total-in vs total-out), NOT the mean of per-title
    # percentages. The two happen to agree today and will diverge; the label
    # downstream says which one this is.
    shrink = (1 - out_total / src_total) * 100 if src_total else None
    # Queue size is SOURCE bytes. What a person actually wants when budgeting
    # NAS space is how much of it comes back, so project it at the rate we have
    # measured rather than making them do it in their head.
    reclaimable = int(queue_bytes * shrink / 100) if shrink is not None else None
    # Job progress measures against the ORIGINAL scope: skipped bytes stay in
    # the goal, so skipping work can never render as finishing it.
    skipped_bytes = sum(r["bytes"] or 0 for r in q_skipped)
    goal = (int((queue_bytes + skipped_bytes) * shrink / 100)
            if shrink is not None else None)
    return {
        "completed": len(hist),
        "completed_measured": len(paired),
        "completed_unknown_source": unknown,
        "reclaimed_bytes": reclaimed,
        "source_total_bytes": src_total,
        "output_total_bytes": out_total,
        "avg_saved_pct": round(shrink, 1) if shrink is not None else None,
        "queue_count": len(q_active),
        "queue_waiting": max(0, len(q_active) - encoding),
        "queue_skipped": len(q_skipped),
        "queue_skipped_bytes": skipped_bytes,
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
            else None),
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
        "roots_offline": [f"{volume_name(r)}/{os.path.basename(r)}"
                          for r in offline],
        "library_complete": complete,
        "overrides_corrupt": bool(load_overrides().get("corrupt")),
        # In summary, not only the dashboard payload: `smeltr report` must
        # never render a paused pipeline as a healthy one (same rule as
        # overrides_corrupt -- every view says it, or the state is invisible
        # from exactly the terminal a 1am SSH session uses).
        "paused": paused(),
        "stop_mbps": STOP_MBPS,
        "x9_online": os.path.isdir(X9),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
