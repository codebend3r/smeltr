"""Titles the operator added to the queue by hand.

A hand-added title is one whose source was dropped straight onto the staging
drive and given a bitrate-index entry, rather than being found in the library
by `.scan-bitrates.sh`. It encodes exactly like any other title -- and then it
STOPS: no library folder of that name exists, so `library_path_of()` in
`.autopilot.sh` resolves nothing, the driver puts the title into the error
state, and the finished output is left beside its source for a human to move.
Nothing is synced and nothing is deleted.

That ending is the whole reason this file exists. On screen a hand-added row is
otherwise indistinguishable from a staged row that will sync itself, so a
person would sit waiting for a push that is never coming.

DASHBOARD-ONLY, deliberately. The decision path reads nothing here and behaves
identically with or without the file; this is presentation, and a title that
drops off the list loses a badge, never a safeguard. One reader serves both the
web UI (`server.py`) and the terminal report (`report.py`) so the two can never
disagree about which rows are hand-added.
"""

from __future__ import annotations

import json
import os

from pipeline import core

# Beside the ledger, with the other hand-written state files. Not gitignored by
# accident: it names movie folders and nothing else, but it is per-machine
# runtime state like `queue_overrides.json`.
PATH = os.path.join(core.SMELTR_DIR, "manual_adds.json")


def titles() -> set:
    """Lowercased folder names, or an empty set. Never raises.

    Every failure -- missing file, corrupt JSON, wrong shape, a non-string in
    the list -- answers "nothing was hand-added", which is the reading that
    marks no rows rather than marking the wrong ones.
    """
    try:
        with open(PATH, encoding="utf-8") as fh:
            names = json.load(fh).get("titles", [])
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(names, list):
        return set()
    return {t.lower() for t in names if isinstance(t, str)}
