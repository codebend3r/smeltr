"""Everything the pipeline will never encode, and WHY -- for the Blacklist tab.

There are two separate mechanisms and the operator asked for both in one view
on 2026-09-15, having looked for the hard list in the UI and not found it (it
was never surfaced anywhere: `queue()` drops a SKIP match before a row exists,
so the page could not have shown it even in principle):

  CODE      `core.SKIP` -- a substring match, lowercased, on the full library
            path. Hardcoded, mirrored by hand in `staging/replenish-queue.sh`,
            and changed only by an edit plus `ops/deploy-staging.sh`. One entry
            can block SEVERAL titles ("lord of the rings" blocks three), which
            is exactly why this view lists the matches rather than the pattern
            alone: the pattern is not the blast radius.

  DASHBOARD `queue_overrides.json` "skip" -- written by clicking. Those titles
            already appear on the Errors tab, greyed with a restore button;
            they are repeated here so ONE screen answers "what is excluded".

This module is dashboard-only and read-only: it imports `core` for the lists
and the bitrate index and writes nothing. Un-blacklisting stays a code edit on
purpose -- a click that silently re-staged a 50 GiB title the operator had
already `rm -rf`'d by hand is the outcome the hard list exists to prevent.
"""

import os

from pipeline import core


def _index_rows():
    """The bitrate index keyed for matching. Empty when the index is missing."""
    out = []
    for rec in core.load_index():
        path = rec.get("path", "")
        if not path:
            continue
        out.append(
            (
                path,
                path.lower(),
                os.path.basename(os.path.dirname(path)),
                (rec.get("overall_bitrate") or 0) / 1e6,
            )
        )
    return out


def rows() -> list[dict]:
    """One row per blacklist ENTRY, each carrying the titles it blocks.

    Ordered code-first, then the dashboard skips, because the code list is the
    one with no other home in the UI. Titles inside an entry are ordered by
    bitrate, highest first -- the same ranking the queue uses, so the cost of
    an exclusion reads off the top of its own list.
    """
    idx = _index_rows()
    ov = core.load_overrides()
    seen_dash = set()
    out = []

    for pat in core.SKIP:
        hits = []
        for path, low, folder, mbps in idx:
            if pat in low:
                hits.append(
                    {
                        "title": folder,
                        "mbps": round(mbps, 1),
                        "bytes": core._size(path) if os.path.exists(path) else None,
                        "location": core._library_of(path),
                        "online": os.path.exists(path),
                    }
                )
        hits.sort(key=lambda h: -h["mbps"])
        out.append(
            {
                "pattern": pat,
                "kind": "code",
                "where": "pipeline/core.py + staging/replenish-queue.sh",
                "titles": hits,
            }
        )

    for title in ov["skip"]:
        low = title.lower()
        if low in seen_dash:
            continue
        seen_dash.add(low)
        hits = []
        for path, _low, folder, mbps in idx:
            if folder.lower() == low:
                hits.append(
                    {
                        "title": folder,
                        "mbps": round(mbps, 1),
                        "bytes": core._size(path) if os.path.exists(path) else None,
                        "location": core._library_of(path),
                        "online": os.path.exists(path),
                    }
                )
        hits.sort(key=lambda h: -h["mbps"])
        out.append(
            {
                "pattern": title,
                "kind": "dashboard",
                "where": "queue_overrides.json",
                "titles": hits,
            }
        )
    return out


def count() -> int:
    """Entries, not matched titles -- the rail counts what the tab lists."""
    return len(core.SKIP) + len({t.lower() for t in core.load_overrides()["skip"]})
