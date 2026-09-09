#!/usr/bin/env python3
"""
Smeltr -- terminal snapshot. The streamlined twin of the web dashboard.

Rendering choices are deliberate. Output from this script is routinely pasted
into chat transcripts and markdown, where ANSI escapes render as garbage and
OSC-8 hyperlinks vanish. So:
  * colour only when stdout is a real TTY
  * the link is printed as bare text as well as an OSC-8 sequence, so it stays
    clickable in a terminal and readable everywhere else
  * box drawing is plain Unicode, which survives a copy-paste intact
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import manual as manual_mod
from pipeline import core

GIB = core.GIB
TIB = GIB * 1024
TTY = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def gib(b) -> str:
    # `if not b` swallowed a genuine zero, so "0 bytes reclaimed" rendered as an
    # em dash -- indistinguishable from "unknown".
    if b is None:
        return "\u2014"
    if b == 0:
        return "0 GiB"
    return f"{b / TIB:.2f} TiB" if b >= TIB else f"{b / GIB:.2f} GiB"


def dur(s) -> str:
    """Sub-hour durations keep their seconds: rounding 107 s down to "1m" is a
    44% understatement at the moment someone decides whether to wait up."""
    if s is None:
        return "\u2014"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def app_url():
    try:
        with open(os.path.join(core.SMELTR_DIR, "url"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def link(url: str, label: str) -> str:
    if TTY:
        return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"
    return label


def indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + ln for ln in text.splitlines())


def render_table(headers, rows, aligns) -> str:
    """Fixed-width Unicode table. Widths measured over headers and cells.

    headers=None draws a headerless grid -- used for key/value blocks, where a
    blank header row and its separator are pure noise.
    """
    cols = len(aligns)
    widths = [len(str(h)) for h in (headers or [""] * cols)]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i])))

    def line(l, mid, r):
        return l + mid.join("\u2500" * (w + 2) for w in widths) + r

    def fmt(cells):
        out = []
        for i, cell in enumerate(cells):
            text = str(cell)
            out.append(
                text.rjust(widths[i]) if aligns[i] == "r" else text.ljust(widths[i])
            )
        return "\u2502" + "\u2502".join(f" {v} " for v in out) + "\u2502"

    parts = [line("\u250c", "\u252c", "\u2510")]
    if headers:
        parts += [fmt(headers), line("\u251c", "\u253c", "\u2524")]
    parts += [fmt(r) for r in rows]
    parts.append(line("\u2514", "\u2534", "\u2518"))
    return "\n".join(parts)


def bar(pct: float, width: int = 34) -> str:
    filled = int(round((pct or 0) / 100 * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


# ------------------------------------------------------------------ sections

RECORD_LABEL = {
    "live": "measured at finish",
    "state-file": "hand-migrated",
    "recovered": "found after the fact",
}


def kv_table(pairs) -> str:
    """Two-column key/value. Preferred for totals: a wide row of bare numbers
    makes the reader invent the labels, and they invent the wrong ones."""
    return render_table(None, [[k, v] for k, v in pairs], ["l", "l"])


def section_stats(s) -> str:
    measured = f"{s['completed_measured']} of {s['completed']} encodes measured"
    avg = s["avg_saved_pct"]
    complete = s.get("library_complete", True)
    pairs = [
        ("Reclaimed so far", f"{gib(s['reclaimed_bytes'])}   ({measured})"),
        (
            "Average shrink",
            f"{avg:.1f}%   (weighted: {gib(s['source_total_bytes'])} -> "
            f"{gib(s['output_total_bytes'])})"
            if avg is not None
            else "—",
        ),
        (
            "Encodes recorded",
            f"{s['completed']}   ({s['completed_unknown_source']} without a recorded original)"
            if s["completed_unknown_source"]
            else str(s["completed"]),
        ),
        (
            "Still queued",
            (
                f"{s['queue_waiting']} waiting above {s['stop_mbps']:.0f} Mb/s"
                f" + {s['queue_encoding']} encoding   ({gib(s['queue_bytes'])} of"
                f" originals{', incl. the encoding' if s['queue_encoding'] else ''})"
            )
            if complete
            else f"unknown — library not fully mounted ({s['queue_waiting']} readable)",
        ),
        (
            "Still to reclaim",
            f"~{gib(s['queue_reclaimable_bytes'])}   (projected at the measured {avg:.1f}%)"
            if complete and s.get("queue_reclaimable_bytes")
            else ("unknown — library not fully mounted" if not complete else "0 GiB"),
        ),
        (
            "Job progress",
            f"~{s['job_progress_pct']:.1f}%   (by reclaimed bytes, not title count"
            f"{'; goal still counts the set-aside titles' if (s.get('queue_skipped') or s.get('queue_errored')) else ''})"
            if s.get("job_progress_pct") is not None
            else (
                "cannot be computed — "
                + ", ".join(s.get("roots_offline") or [])
                + " not mounted"
                if not complete
                else "—"
            ),
        ),
        # Always derived from the drive itself, so it stays true when the NAS is
        # unreachable -- the moment someone is most likely to think the job is
        # over and start deleting.
        (
            "Staged on drive",
            f"{s['staged']} folders, {gib(s['staged_bytes'])}   "
            f"({s['queue_encoding']} encoding, {s['staged_unencoded']} not yet encoded"
            f" = {gib(s['staged_unencoded_bytes'])})",
        ),
    ]
    # Two separate lines, never one "excluded" total: a skip is a preference
    # you can undo, an error is work the pipeline refused and will not resume
    # until a marker file is deleted. Merging them hides the half that needs
    # a human.
    if s.get("queue_errored"):
        pairs.insert(
            4,
            (
                "!! Errored",
                f"{s['queue_errored']} titles ({gib(s['queue_errored_bytes'])})"
                " — out of Still queued and Still to reclaim;"
                " Job progress keeps them in its goal. See SET ASIDE below",
            ),
        )
    if s.get("queue_skipped"):
        pairs.insert(
            5 if s.get("queue_errored") else 4,
            (
                "Skipped by hand",
                f"{s['queue_skipped']} titles ({gib(s['queue_skipped_bytes'])})"
                " — out of Still queued and Still to reclaim;"
                " Job progress keeps them in its goal",
            ),
        )
    if not s.get("library_complete", True):
        pairs.insert(
            0,
            (
                "!! LIBRARY",
                "INCOMPLETE — "
                + ", ".join(s.get("roots_offline") or [])
                + " not mounted; queue figures are partial",
            ),
        )
    return indent(kv_table(pairs), 2) + "\n"


def section_live(live) -> str:
    if not live:
        return "  Nothing encoding right now.\n"
    out = []
    for e in live:
        pct = e["pct"] or 0
        bits = []
        if e["crf"] is not None:
            # CQ, never a bare "CRF n", for a VideoToolbox encode: the two
            # quality scales are not comparable and 60 means opposite things.
            vt = (e.get("encoder") or "").startswith("vt")
            bits.append(f"{'CQ' if vt else 'CRF'} {e['crf']:g}")
        # Source AND output geometry. "3840x1600" on its own cannot be told
        # apart from an unwanted downscale, and that check gates a deletion.
        if e.get("source_geometry") and e["geometry"]:
            same = e["source_geometry"] == e["geometry"]
            bits.append(
                e["geometry"]
                if same
                else f"{e['source_geometry']} -> {e['geometry']} (auto-crop)"
            )
        elif e["geometry"]:
            bits.append(e["geometry"])
        if e["audio"] is not None:
            # Counts come from the job-configuration block, i.e. what is being
            # WRITTEN. Show the source counts alongside so any loss is visible.
            sa, ss = e.get("src_audio"), e.get("src_subs")
            if sa is not None and (sa, ss) != (e["audio"], e["subs"]):
                bits.append(f"TRACK LOSS {sa}a/{ss}s -> {e['audio']}a/{e['subs']}s")
            else:
                bits.append(f"{e['audio']} audio / {e['subs']} subs")
        # Silence is indistinguishable from zero, so state it either way.
        de = e.get("decoder_errors")
        bits.append(
            "0 decoder errors"
            if de == 0
            else (f"{de} DECODER ERRORS" if de else "decoder errors not yet reported")
        )
        bits.append(f"pid {e['pid']}")
        out.append(f"  {c(e['title'], '1')}" + c("   " + " · ".join(bits), "2"))

        # The ETA is derived from the average rate, so show the average. Pairing
        # it with HandBrake's instantaneous fps put two numbers on one line that
        # could not both be true.
        avg = e.get("avg_fps") or 0
        out.append(
            f"  {bar(pct)} {pct:6.2f}%   ETA {dur(e['eta_s'])}   {avg:.1f} fps avg"
        )
        out.append(
            indent(
                render_table(
                    [
                        "ORIGINAL",
                        "WRITTEN SO FAR",
                        "PROJECTED FINAL",
                        "SHRINK",
                        "VERDICT",
                    ],
                    [
                        [
                            gib(e["source_bytes"]),
                            gib(e["output_bytes"]),
                            gib(e["projected_bytes"]),
                            "—"
                            if e.get("shrink_pct") is None
                            else f"{e['shrink_pct']:.1f}%",
                            e["verdict"].upper(),
                        ]
                    ],
                    ["r", "r", "r", "r", "l"],
                ),
                2,
            )
        )
        loud = e["verdict"] in ("suspect", "blowup", "no-saving", "downscale")
        style = (
            "1;31"
            if e["verdict"] in ("blowup", "no-saving", "downscale")
            else ("1;33" if e["verdict"] in ("suspect", "thin") else "2")
        )
        out.append(("  !! " if loud else "  ") + c(e["verdict_note"], style))
    return "\n".join(out) + "\n"


def _arriving_on_x9(title: str, staged: bool) -> bool:
    """A pull still landing: a hidden ".pull-<title>" dir (dashboard pull) or
    a visible staged folder holding only a .partial (replenisher pull). The
    web dashboard reports these as "arriving"; without this check the table
    calls a mostly-missing file "staged", as if it were encodable right now.
    """
    if os.path.isdir(os.path.join(core.stage_dir(), ".pull-" + title)) or os.path.isdir(
        os.path.join(core.X9, ".pull-" + title)
    ):
        return True
    if not staged:
        return False
    try:
        names = [
            n
            for n in os.listdir(core.folder_dir(title))
            if not n.startswith("._")
        ]
    except OSError:
        return False
    return any(n.endswith(".partial") for n in names) and not any(
        n.endswith(core.SOURCE_EXTS) for n in names
    )


def split_set_aside(q):
    """(live queue, set-aside rows) -- the terminal's half of the dashboard's
    Queue/Errors tab split (2026-09-06). core.queue() still returns one list
    in one order; both views cut it in the same place so the rank a person
    reads here is the rank they read there.
    """
    aside = [r for r in q if r.get("error") or r.get("skipped")]
    return [r for r in q if not (r.get("error") or r.get("skipped"))], aside


# The driver writes the marker as "<title>: <what happened>", which is right
# for a bare file on disk and wrong in a table that already has a TITLE
# column: repeating it pushed this column past 200 chars and off the screen.
# Truncated rather than WRAPPED because render_table() measures cells by
# len() and draws one line per row -- a newline inside a cell breaks the box
# borders around it. The dashboard's Errors tab wraps and shows it whole.
_NOTE_MAX = 92


def _trim_note(note, title: str) -> str:
    note = " ".join((note or core.EMPTY_ERROR_NOTE).split())
    if note.lower().startswith(title.lower() + ":"):
        note = note[len(title) + 1 :].strip()
    if len(note) > _NOTE_MAX:
        # Cut on a word boundary so the tail is not a half-word, and keep the
        # ellipsis so nobody reads a clipped sentence as the whole reason.
        note = note[:_NOTE_MAX].rsplit(" ", 1)[0] + " …"
    return note


def section_errors(aside) -> str:
    """Titles that will not encode until a person acts. Never truncated: the
    whole point is that there are few of them and each one is owed a decision.
    """
    rows = []
    for r in aside:
        if r.get("error"):
            state = c("ERROR", "1;31")
            why = _trim_note(r.get("error_note"), r["title"])
            if r.get("skipped"):
                state += " + skipped"
        elif r.get("done"):
            # Finished and kept in place under the no-delete policy -- the
            # job succeeding, never an error.
            state = c("DONE", "1;32")
            why = _trim_note(r.get("done_note"), r["title"])
        else:
            state = "skipped"
            why = "skipped by hand — nothing wrong with it"
        rows.append([state, f"{r['mbps']:.1f}", gib(r["bytes"]), r["title"], why])
    body = render_table(
        ["STATE", "SRC Mb/s", "SRC SIZE", "TITLE", "WHAT HAPPENED"],
        rows,
        ["l", "r", "r", "l", "l"],
    )
    note = c(
        "\n  Notes are trimmed; the Errors tab shows each in full."
        "\n  An ERROR ends when you delete its "
        f"{os.path.join(core.X9, '.error-<title>')} marker. "
        "A skip ends from the dashboard's restore button.",
        "2",
    )
    return indent(body, 2) + note + "\n"


def section_queue(q, limit) -> str:
    shown = q if limit in (None, 0) else q[:limit]
    manual = manual_mod.titles()
    rows = []
    rank_n = 0
    for r in shown:
        if r["encoding"]:
            status = "ENCODING"
        elif _arriving_on_x9(r["title"], r["staged"]):
            status = "arriving"
        elif r["staged"]:
            status = "staged"
        else:
            status = "library"
        if r.get("pinned"):
            status = "pinned · " + status
        # Hand-added titles have no library original to sync back to, so they
        # encode and then stop with the output left in place. Saying so here
        # keeps the terminal view from reading like every other staged row.
        if r["title"].lower() in manual:
            status = "manual · " + status
        rank_n += 1
        rank = rank_n
        rows.append(
            [
                rank,
                f"{r['mbps']:.1f}",
                gib(r["bytes"]),
                r["title"],
                r["location"],
                status,
            ]
        )
    # "MB/S" reads as megabytes and is 8x wrong; and neither the bitrate nor the
    # size column said whether it described the original or the output.
    body = render_table(
        ["RANK", "SRC Mb/s", "SRC SIZE", "TITLE", "NAS", "STATUS"],
        rows,
        ["r", "r", "r", "l", "l", "l"],
    )
    note = ""
    hidden = q[len(shown) :]
    if hidden:
        note = c(
            f"\n  … {len(hidden)} more waiting not shown — "
            "use --all for the full queue.",
            "2",
        )
    return indent(body, 2) + note + "\n"


def section_ledger(hist, limit) -> str:
    # hist is oldest-first; number by that stable sequence, then show newest
    # first. Enumerate rather than .index() -- identical dicts would collide.
    numbered = list(enumerate(hist, 1))
    ordered = list(reversed(numbered))
    shown = ordered if limit in (None, 0) else ordered[:limit]

    rows, notes = [], []
    for n, r in shown:
        approx = "~" if r.get("exact") is False else ""
        rows.append(
            [
                n,
                r["title"],
                "—" if not r.get("source_bytes") else approx + gib(r["source_bytes"]),
                "—" if not r.get("output_bytes") else approx + gib(r["output_bytes"]),
                "—" if not r.get("saved_bytes") else approx + gib(r["saved_bytes"]),
                "—" if r.get("saved_pct") is None else f"{r['saved_pct']:.1f}%",
                # A recorded VT row prints "CQ 60": this column sits beside the
                # size of an original that was DELETED on the strength of it,
                # and 60 read as a CRF says the opposite of what it means.
                "—"
                if r.get("crf") is None
                else ("CQ " if (r.get("encoder") or "").startswith("vt") else "")
                + f"{r['crf']:g}",
                "—" if r.get("audio") is None else f"{r['audio']}a/{r['subs']}s",
                # Matches the dashboard's two-pill rendering: volume · bucket,
                # one field, not a path claiming to be complete.
                # A kept row never moved: say so, never a dash that reads as
                # "unknown" beside a saving that was not reclaimed.
                "kept on X9" if r.get("kept") else (r.get("dest") or "—").replace("/", " · "),
                RECORD_LABEL.get(r.get("provenance"), r.get("provenance") or "—"),
                (r.get("finished_at") or "—")[:10],
            ]
        )
        if r.get("note"):
            notes.append(f"{n}. {r['title']} — {r['note']}")

    # A column that is "—" in every row costs ~12 terminal columns to say
    # nothing, and width is what makes these tables wrap when pasted.
    heads = [
        "#",
        "TITLE",
        "ORIGINAL",
        "OUTPUT",
        "SAVED",
        "SHRINK",
        "QUALITY",
        "TRACKS",
        "MOVED TO",
        "SOURCE OF RECORD",
        "FINISHED",
    ]
    aligns = ["r", "l", "r", "r", "r", "r", "r", "l", "l", "l", "l"]
    if all(r[-1] == "—" for r in rows):
        heads, aligns = heads[:-1], aligns[:-1]
        rows = [r[:-1] for r in rows]
    body = render_table(heads, rows, aligns)

    tail = []
    if len(shown) < len(ordered):
        tail.append(
            c(f"  … {len(ordered) - len(shown)} older entries — use --all.", "2")
        )
    if any(r.get("exact") is False for _, r in shown):
        tail.append(
            c(
                "  ~ not measured byte-for-byte at encode time "
                "(see SOURCE OF RECORD for how each row was obtained).",
                "2",
            )
        )
    for line in notes:
        tail.append(c("  " + line, "2"))
    return indent(body, 2) + ("\n" + "\n".join(tail) if tail else "") + "\n"


# ---------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="queue-report", description="Snapshot of the 4K re-encode queue."
    )
    ap.add_argument("--all", action="store_true", help="show every row, no truncation")
    ap.add_argument("--limit", type=int, default=12, help="rows per table (default 12)")
    ap.add_argument("--queue", action="store_true", help="queue only")
    ap.add_argument("--history", action="store_true", help="history only")
    args = ap.parse_args()

    limit = None if args.all else args.limit
    want_queue = args.queue or not args.history
    want_hist = args.history or not args.queue
    # --queue / --history mean "only that section", as the skill documents.
    focused = args.queue or args.history

    # Compute each source once and thread it through, exactly as the server
    # does. Calling summary() and then queue_cached() separately built the queue
    # twice -- ~46 s cold over SMB.
    hist = core.ledger()
    live = core.live_encodes()
    q = core.queue_cached(live=live, hist=hist)
    s = core.summary(hist=hist, q=q)

    print()
    print(f"  {c('SMELTR', '1;38;5;208')}  {c('· 4K re-encode queue', '2')}")
    url = app_url()
    # The URL carries the auth token when LAN-bound. Printing it to a pipe or
    # file (report > out, | tee, tmux capture) would leak the token into
    # plaintext, so strip the query when stdout is not a terminal.
    shown = url
    if url and not TTY and "?" in url:
        shown = url.split("?", 1)[0]
    print(
        f"  {c('live dashboard', '2')}  {link(url, shown)}"
        if url
        else f"  {c('dashboard not running — start it with: ~/.smeltr/smeltr start', '2')}"
    )
    if not s["x9_online"]:
        print(
            f"  {c('WARNING: staging drive not mounted — sizes below are incomplete', '1;33')}"
        )
    # An offline library root silently empties the queue, which is
    # indistinguishable from having finished. Say so, loudly, every time.
    if s.get("overrides_corrupt"):
        print(
            f"  {c('!! queue_overrides.json is unreadable — skips and priorities are NOT applied.', '1;33')}"
        )
    # A paused pipeline must never render as a healthy one -- this is the
    # view a 1am SSH session uses, and without the line it is byte-identical
    # to a running job.
    if s.get("paused"):
        print(
            f"  {c('PAUSED — the current encode (if any) still finishes and syncs; nothing new starts. Resume from the dashboard.', '1;33')}"
        )
    if not s.get("library_complete", True):
        missing = ", ".join(s.get("roots_offline") or ["unknown"])
        print(f"  {c('!! LIBRARY INCOMPLETE: ' + missing + ' not mounted.', '1;31')}")
        # Built outside the f-string: a backslash inside an f-string expression
        # is a SyntaxError before Python 3.12, and `smeltr report` is the view a
        # 1am SSH session uses -- on a machine whose python3 is the stock 3.9
        # this whole file failed to import, so the report was simply gone.
        partial = "   The queue below is PARTIAL. Do not read it as 'nothing left'."
        print(f"  {c(partial, '1;31')}")
    print()

    if not focused:
        print(f"  {c('TOTALS', '1')}")
        print(section_stats(s))
        print(f"  {c('NOW ENCODING', '1')}")
        print(section_live(live))

    q_live, aside = split_set_aside(q)
    if want_queue:
        sub = (
            f"{s['queue_waiting']} waiting above {s['stop_mbps']:.0f} Mb/s"
            f" · {gib(s['queue_bytes'])} of originals"
        )
        if not s.get("library_complete", True):
            sub += "  [PARTIAL — library not fully mounted]"
        print(f"  {c('QUEUE', '1')}  {c(sub, '2')}")
        print(section_queue(q_live, limit))
        # Printed under the queue, ALWAYS when non-empty and never truncated.
        # These rows used to sit greyed inside the queue table; four of them
        # scattered through 130 live ones is not visibility, and since the
        # rank column became a running order a row that will never run
        # cannot sit inside it.
        if aside:
            n_err = sum(1 for r in aside if r.get("error"))
            n_done = sum(1 for r in aside if r.get("done") and not r.get("error"))
            n_skip = len(aside) - n_err - n_done
            bits = (
                ([f"{n_err} errored"] if n_err else [])
                + ([f"{n_done} done, kept in place"] if n_done else [])
                + ([f"{n_skip} skipped by hand"] if n_skip else [])
            )
            print(f"  {c('SET ASIDE', '1;31' if n_err else '1')}  "
                  f"{c(' · '.join(bits) + ' — not encoding until you act', '2')}")
            print(section_errors(aside))

    if want_hist:
        sub = f"{len(hist)} encodes · {gib(s['reclaimed_bytes'])} reclaimed"
        print(f"  {c('HISTORY', '1')}  {c(sub, '2')}")
        print(section_ledger(hist, limit))

    print(
        f"  {c('snapshot ' + s['generated_at'] + ' — the dashboard above is live.', '2')}"
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
