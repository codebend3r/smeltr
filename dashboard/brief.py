#!/usr/bin/env python3
"""The morning brief: one email a day saying what the last 24 hours produced.

WHY THIS EXISTS
---------------
The hourly heartbeat (dashboard/heartbeat.py) flagged every idle hour, and on
a deliberately paused pipeline that was the same sentence to Slack and Gmail
twenty-four times a day. The operator asked for it to stop and for this
instead (2026-09-08): a single message at 09:00 that says what was CONVERTED
in the past day, what ERRORED, and anything else worth knowing.

WHAT IT READS
-------------
  * the LEDGER for the conversions -- `finished_at` inside the window. The
    ledger is the record the History tab and `smeltr report` are built from,
    so the brief can never disagree with them about what finished.
  * the EVENTS timeline (dashboard/events.py, the same parser the Events tab
    and the notifier read) for errors, ladder moves, deletions, replenish
    picks, driver restarts. One parser, so a line the tab shows red is the
    line this email lists under errors.
  * the `.error-<title>` markers on the staging drive, which are the ERROR
    STATE as it stands right now -- an error from three days ago that nobody
    has cleared is still an error this morning.
  * a few live readings for the closing section: what is encoding, whether
    the driver is alive, the pause flag, the staging drive's free space, the
    download budget.

HONESTY RULES
-------------
  * a number that cannot be read is "—" or a sentence saying so, never 0.
    An unmounted staging drive is reported as unreadable; it is not "no
    errors".
  * the events parser reads a bounded tail of .autopilot.log. If that tail
    starts AFTER the window opens, the brief says which part of the day it
    could not see rather than presenting a shorter day as a quiet one.
  * sizes are GiB throughout, like the ledger and the History tab.
  * every clock is the local wall clock the ledger and log already carry;
    nothing is re-parsed through a timezone.

This module is a pure OBSERVER. It reads and it sends; it starts nothing,
kills nothing, and writes nothing on disk. `tests/test_layering.py` lists it
DASHBOARD_ONLY -- the decision path must never import it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import events, heartbeat, notify  # noqa: E402
from pipeline import core  # noqa: E402

HOURS = 24
GIB = 1073741824
STAMP = "%Y-%m-%d %H:%M:%S"

# Event kinds the Events tab draws red, plus the driver lines that are red
# by their first word (ERROR is not in events._KINDS, so it arrives as
# "info" and has to be recognised by text here).
_ERROR_KINDS = frozenset({"halted", "failed", "exhausted", "lastrung"})
_ERROR_PREFIXES = ("ERROR", "HALTED:", "SYNC FAILED", "SYNC ABORTED")
# Kinds worth a line under "of note". Not START/JUDGE/DONE/RECORDED -- the
# ledger section already says what finished -- and not the driver's 300 s
# wait reasons, which are the noise the Events tab collapses.
_NOTE_KINDS = frozenset(
    {"ladder", "killed", "cycle", "replenish", "defer", "stale", "purge", "up", "planned"}
)
_NOTE_PREFIXES = ("BUDGET", "STOP CONDITION", "LAYOUT:", "SKIP", "RETRY")
_VERDICT = re.compile(r"verdict ([a-z][a-z-]*)")
# A pick is a title, and a title ends in "(YYYY)". The event detail is
# capped (events.DETAIL_CAP), so a name cut mid-word must not be listed as
# a title the replenisher chose.
_PICK = re.compile(r"PICK [\d.]+ Mb/s\s+(.+? \(\d{4}\))")
MAX_LINES = 15


# ---------------------------------------------------------------- windows --


def window(now: Optional[float] = None, hours: int = HOURS) -> tuple:
    """(since, until) as ledger-format local stamps. Strings compare in order
    because the format is zero-padded, and the ledger's own stamps are the
    same format, so no row is ever re-parsed through a timezone."""
    until = time.time() if now is None else now
    since = until - hours * 3600
    return time.strftime(STAMP, time.localtime(since)), time.strftime(
        STAMP, time.localtime(until)
    )


def _in_window(ts, since: str, until: str) -> bool:
    return isinstance(ts, str) and since <= ts <= until


# ---------------------------------------------------------------- ledger ---


def converted(rows: list, since: str, until: str) -> list:
    """Ledger rows finished inside the window, oldest first."""
    hit = [r for r in rows if _in_window(r.get("finished_at"), since, until)]
    hit.sort(key=lambda r: r.get("finished_at") or "")
    return hit


def quality_label(row: dict) -> str:
    """`VT CQ 65` / `CRF 14` -- the two scales are not comparable, so a bare
    number is never printed. Same rule as qLabel() in web/app.js."""
    # A ledger row with no encoder predates the field and is x265 (CLAUDE.md,
    # "old rows: None, all x265") -- NOT core.DEFAULT_ENCODER, which moved to
    # VideoToolbox for the 2026-09-06 batch.
    enc = row.get("encoder") or "x265"
    q = row.get("crf")
    if q is None:
        return "—"
    if isinstance(q, float) and q.is_integer():
        q = int(q)
    scale = "VT CQ" if str(enc).startswith("vt") else "CRF"
    return f"{scale} {q}"


def verdict_of(row: dict) -> str:
    """The verdict word the driver wrote into the note, or what the row's
    outcome implies. A replaced original was a `good` verdict by
    construction; anything else unlabelled is honestly unknown."""
    m = _VERDICT.search(row.get("note") or "")
    if m:
        return m.group(1)
    if row.get("dest") and not row.get("kept"):
        return "good"
    return "?"


def outcome_of(row: dict) -> str:
    if row.get("kept"):
        return "kept in place"
    if row.get("dest"):
        return "ORIGINAL REPLACED"
    return "recorded"


def gib(b) -> str:
    return "—" if not isinstance(b, (int, float)) else f"{b / GIB:.2f} GiB"


def duration(seconds) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "—"
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def totals(rows: list) -> dict:
    """Sums over rows carrying BOTH sizes only -- the same rule
    core.summary() applies, so a row with an unknown original is counted as
    an encode but never contributes to a byte total."""
    paired = [r for r in rows if r.get("source_bytes") and r.get("output_bytes")]
    src = sum(r["source_bytes"] for r in paired)
    out = sum(r["output_bytes"] for r in paired)
    return {
        "count": len(rows),
        "measured": len(paired),
        "source_bytes": src,
        "output_bytes": out,
        "saved_bytes": src - out,
        "saved_pct": round((1 - out / src) * 100, 1) if src else None,
        "encode_seconds": sum(
            r["encode_seconds"]
            for r in rows
            if isinstance(r.get("encode_seconds"), (int, float))
        ),
        "replaced": len([r for r in rows if r.get("dest") and not r.get("kept")]),
        "kept": len([r for r in rows if r.get("kept")]),
    }


# ---------------------------------------------------------------- events ---


def split_events(evs: list, since: str, until: str) -> dict:
    """{errors, notes, coverage} from a newest-first events payload.

    `coverage` is the oldest stamp the parser could see at all. When it is
    later than `since`, the log tail did not reach back to the start of the
    window and the brief must say so.
    """
    errors, notes = [], []
    oldest = None
    for e in evs:
        ts = e.get("ts")
        if isinstance(ts, str) and (oldest is None or ts < oldest):
            oldest = ts
        if not _in_window(ts, since, until):
            continue
        kind, text = e.get("kind"), e.get("text") or ""
        if kind in _ERROR_KINDS or text.startswith(_ERROR_PREFIXES):
            errors.append(e)
        elif kind in _NOTE_KINDS or text.startswith(_NOTE_PREFIXES):
            notes.append(e)
    errors.sort(key=lambda e: e["ts"])
    notes.sort(key=lambda e: e["ts"])
    return {"errors": errors, "notes": notes, "coverage": oldest}


def replenish_picks(notes: list) -> list:
    """Titles the replenisher PICKED inside the window, de-duplicated in
    order of first appearance. The independent tick logs the same pick every
    60 s while it waits for room, so a raw list is the same title 200
    times."""
    seen, out = set(), []
    for e in notes:
        if e.get("kind") != "replenish":
            continue
        for m in _PICK.finditer(e.get("detail") or ""):
            t = m.group(1).strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def error_markers(since: str) -> Optional[list]:
    """Every `.error-<title>` marker on the staging drive, with its note and
    whether it appeared inside the window. None when the drive cannot be
    listed -- an unreadable drive is not a clean one."""
    try:
        names = os.listdir(core.X9)
    except OSError:
        return None
    out = []
    for n in names:
        if not n.startswith(".error-") or n.startswith("._"):
            continue
        title = n[len(".error-") :]
        path = os.path.join(core.X9, n)
        try:
            ts = time.strftime(STAMP, time.localtime(os.path.getmtime(path)))
        except OSError:
            ts = None
        out.append(
            {
                "title": title,
                "note": core.error_marker(title) or "",
                "ts": ts,
                "new": bool(ts and ts >= since),
            }
        )
    out.sort(key=lambda m: m["ts"] or "")
    return out


# ------------------------------------------------------------------ live ---


def _int_file(name: str) -> Optional[int]:
    try:
        with open(os.path.join(core.SMELTR_DIR, name), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def status() -> dict:
    """The closing readings. Every one degrades to None, never to a guess."""
    x9 = os.path.isdir(core.X9)
    st = {
        "x9_online": x9,
        "driver": heartbeat._driver_running(),
        "paused": core.paused(),
        "encoding": None,
        "free_bytes": None,
        "total_bytes": None,
        "queue_folders": None,
        "complete_folders": None,
        "downloads_done": _int_file("downloads_done"),
        "download_budget": _int_file("download_budget"),
    }
    try:
        if core._ps_handbrake():
            st["encoding"] = heartbeat._describe_live()
    except Exception:  # noqa: BLE001 - a reading, not the report
        pass
    if x9:
        try:
            du = shutil.disk_usage(core.X9)
            st["free_bytes"], st["total_bytes"] = du.free, du.total
        except OSError:
            pass
        st["queue_folders"] = len(core._movie_dirs(core.stage_dir()))
        st["complete_folders"] = len(core._movie_dirs(core.complete_dir()))
    return st


# --------------------------------------------------------------- compose ---


def _day(ts: str) -> str:
    """`Tue 8 Sep 09:00` from a ledger stamp, without going through a
    timezone: the fields are read straight off the string."""
    try:
        t = time.strptime(ts, STAMP)
    except ValueError:
        return ts
    return time.strftime("%a %-d %b %H:%M", t)


def _clock(ts) -> str:
    return _day(ts) if isinstance(ts, str) else "—"


def _cap(lines: list, label: str) -> list:
    if len(lines) <= MAX_LINES:
        return lines
    return lines[:MAX_LINES] + [f"  … and {len(lines) - MAX_LINES} more {label}"]


def compose(
    since: str,
    until: str,
    rows: list,
    split: dict,
    markers: Optional[list],
    st: dict,
    hours: int = HOURS,
) -> dict:
    """{"subject", "body"} -- plain text, GiB, 24 h wall clock."""
    tot = totals(rows)
    out = []
    out.append(f"Smeltr morning brief — {_day(until)}")
    out.append(f"Window: {_day(since)} → {_day(until)} ({hours} h)")
    out.append("")

    # -- converted -------------------------------------------------------
    out.append(f"CONVERTED ({tot['count']})")
    if not rows:
        out.append("  nothing finished in this window")
    for r in rows:
        sb, ob = r.get("source_bytes"), r.get("output_bytes")
        kept = (
            f"{ob / sb * 100:.1f}% of source" if sb and ob else "size ratio unknown"
        )
        out.append(
            f"  {_clock(r.get('finished_at'))}  {r.get('title')}"
            f"  ·  {quality_label(r)}  ·  {gib(sb)} → {gib(ob)} ({kept})"
            f"  ·  verdict {verdict_of(r)}  ·  {duration(r.get('encode_seconds'))}"
            f"  ·  {outcome_of(r)}"
        )
    if rows:
        line = (
            f"  Total: {tot['count']} encodes · {gib(tot['source_bytes'])} → "
            f"{gib(tot['output_bytes'])} · {gib(tot['saved_bytes'])} smaller"
        )
        if tot["saved_pct"] is not None:
            line += f" ({tot['saved_pct']}%)"
        line += f" · {duration(tot['encode_seconds'])} encoding"
        if tot["measured"] < tot["count"]:
            line += (
                f" · {tot['count'] - tot['measured']} with an unknown size left out"
            )
        out.append(line)
        out.append(
            f"  Originals replaced: {tot['replaced']} · kept in place: {tot['kept']}"
        )
    out.append("")

    # -- errors ----------------------------------------------------------
    errs = split["errors"]
    open_markers = markers or []
    new_markers = [m for m in open_markers if m["new"]]
    # The headline counts TITLES in the error state, not log lines: one
    # title's failure is typically a FAILED watcher line, an ERROR driver
    # line and a marker, and 14 for five titles reads as a worse night than
    # it was.
    n = len(open_markers)
    if markers is None:
        out.append("ERRORS (staging drive unreadable)")
    else:
        head = f"ERRORS ({n} in the error state"
        head += f", {len(new_markers)} new" if n else ""
        out.append(head + ")")
    if markers is None:
        out.append(
            f"  could not list {core.X9} — the error markers live there, so this "
            "section is BLIND, not clear"
        )
    elif not errs and not open_markers:
        out.append("  none")
    if open_markers:
        out.append(
            f"  In the ERROR state now ({len(open_markers)}, {len(new_markers)} new "
            "this window) — each waits for a human to delete its marker:"
        )
        for m in _cap(open_markers, "markers"):
            if isinstance(m, str):
                out.append(m)
                continue
            tag = "NEW  " if m["new"] else "old  "
            out.append(f"    {tag}{_clock(m['ts'])}  {m['title']} — {m['note']}")
    if errs:
        out.append("  Logged this window:")
        for e in _cap(errs, "error lines"):
            if isinstance(e, str):
                out.append(e)
                continue
            out.append(f"    {_stamp(e)}  {e['text']}{_count(e)}")
            if e.get("detail"):
                out.append(f"          {e['detail']}")
    out.append("")

    # -- of note ---------------------------------------------------------
    notes = split["notes"]
    out.append("OF NOTE")
    picks = replenish_picks(notes)
    any_note = False
    if picks:
        any_note = True
        out.append(f"  Replenisher picked {len(picks)}: " + ", ".join(picks))
    for e in _cap([e for e in notes if e.get("kind") != "replenish"], "lines"):
        any_note = True
        if isinstance(e, str):
            out.append(e)
            continue
        out.append(f"  {_stamp(e)}  {e['text']}{_count(e)}")
    if not any_note:
        out.append("  nothing beyond the conversions above")
    cov = split.get("coverage")
    if cov is None:
        out.append(
            "  ⚠ no driver/watcher events could be read at all — the staging drive "
            "is unreadable or the log is empty"
        )
    elif cov > since:
        out.append(
            f"  ⚠ the log tail this brief can read starts at {_day(cov)}; events "
            f"between {_day(since)} and then are NOT covered"
        )
    out.append("")

    # -- right now -------------------------------------------------------
    out.append("RIGHT NOW")
    if not st["x9_online"]:
        out.append(f"  Staging drive: NOT READABLE at {core.X9}")
    else:
        if st["free_bytes"] is not None:
            pct = (
                st["free_bytes"] / st["total_bytes"] * 100 if st["total_bytes"] else 0
            )
            warn = "  ⚠ FULL — nothing can be pulled or encoded" if pct < 1 else ""
            if not warn and st["free_bytes"] < core.LOW_SPACE_FLOOR_BYTES:
                # The 100 GiB encode floor (2026-09-08): the driver is WAITING
                # for a human to free space -- a different fact from FULL.
                warn = (
                    f"  ⚠ LOW SPACE — under the {core.LOW_SPACE_FLOOR_BYTES // 1024**3} GiB "
                    "encode floor; nothing new starts until space is freed"
                )
            out.append(
                f"  Staging drive: {gib(st['free_bytes'])} free of "
                f"{gib(st['total_bytes'])} ({pct:.0f}%){warn}"
            )
        out.append(
            f"  queue/: {st['queue_folders']} folders · complete/: "
            f"{st['complete_folders']} folders"
        )
    out.append(
        "  Encoding: " + (st["encoding"] or "nothing")
    )
    drv = "running" if st["driver"] else "NOT RUNNING"
    pau = " · PAUSED from the dashboard" if st["paused"] else ""
    out.append(f"  Driver: {drv}{pau}")
    if st["download_budget"] is not None or st["downloads_done"] is not None:
        done = st["downloads_done"]
        bud = st["download_budget"]
        out.append(
            "  Downloads: "
            + ("—" if done is None else str(done))
            + (f" of {bud} budget" if bud is not None else " (no budget set)")
        )
    body = "\n".join(out) + "\n"

    # -- subject ---------------------------------------------------------
    parts = [f"{tot['count']} converted"]
    if markers is None:
        parts.append("errors unreadable")
    elif n:
        parts.append(f"{n} in error ({len(new_markers)} new)")
    elif errs:
        parts.append(f"{len(errs)} error lines logged")
    if st["paused"]:
        parts.append("paused")
    if st["x9_online"] and st["free_bytes"] is not None and st["total_bytes"]:
        if st["free_bytes"] / st["total_bytes"] < 0.01:
            parts.append("X9 FULL")
        elif st["free_bytes"] < core.LOW_SPACE_FLOOR_BYTES:
            parts.append("low space")
    subject = f"smeltr brief {_day(until)}: " + " · ".join(parts)
    return {"subject": subject, "body": body}


def _stamp(e: dict) -> str:
    s = _clock(e.get("ts"))
    return ("~" + s) if e.get("approx") else s


def _count(e: dict) -> str:
    c = e.get("count") or 1
    return f"  (× {c})" if c > 1 else ""


# ----------------------------------------------------------------- send ----


def build(now: Optional[float] = None, hours: int = HOURS) -> dict:
    since, until = window(now, hours)
    rows = converted(core.ledger(), since, until)
    try:
        evs = events.events(limit=1_000_000)["events"]
    except Exception:  # noqa: BLE001 - an unreadable log is reported, not fatal
        evs = []
    return compose(
        since,
        until,
        rows,
        split_events(evs, since, until),
        error_markers(since),
        status(),
        hours,
    )


def send(message: dict, err=print) -> int:
    """Email only -- that is what was asked for. Reuses notify.json and
    notify.send_email so this cannot become a second, differently
    configured mail path. Never touches notify.cursor."""
    cfg = notify.load_config(os.path.join(core.SMELTR_DIR, notify.CONFIG_NAME))
    if cfg is None or "email" not in cfg:
        err(
            f"smeltr brief: no email channel in {notify.CONFIG_NAME}, so the brief "
            "could not be sent. Body follows.\n" + message["body"]
        )
        return 1
    try:
        notify.send_email(cfg["email"], message)
    except Exception as e:  # noqa: BLE001 - report, exit non-zero
        err(f"smeltr brief: email FAILED — {e}")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="smeltr brief",
        description="Email the morning brief: the last 24 h of conversions, errors, and notes.",
    )
    ap.add_argument("--hours", type=int, default=HOURS, help="window length (default 24)")
    ap.add_argument(
        "--dry-run", action="store_true", help="print the brief, send nothing"
    )
    args = ap.parse_args(argv)
    msg = build(hours=max(1, args.hours))
    if args.dry_run:
        print(msg["subject"])
        print()
        print(msg["body"], end="")
        return 0
    rc = send(msg, err=lambda s: print(s, file=sys.stderr))
    stamp = time.strftime(STAMP)
    if rc == 0:
        print(f"{stamp}  SENT — {msg['subject']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
