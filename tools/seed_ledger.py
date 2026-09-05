#!/usr/bin/env python3
"""
One-shot ledger seed from the pre-Smeltr text state file.

Provenance is recorded honestly per row. Three tiers appear here:
  state-file  -- both sizes were written down at the time; trustworthy to 2dp
  recovered   -- the encode finished but the state file was never updated;
                 the output was found on the NAS, the original is deleted and
                 its size is genuinely unrecoverable. source_bytes stays null
                 rather than being back-solved from a half-remembered guess.
Nothing here is interpolated to make the totals look tidier.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core
from core import Entry, GIB

# title, source GiB, output GiB, audio, subs, dest, note
STATE = [
    ("GoodFellas (1990)", 81.40, 40.07, 3, 26, "Vhagar/G", ""),
    (
        "Armageddon (1998)",
        78.12,
        31.23,
        3,
        5,
        "Vhagar/A",
        "restarted after a reboot killed the first run at ~75%",
    ),
    ("What Dreams May Come (1998)", 75.22, 27.84, 3, 63, "Vermithor/W", ""),
    ("Sudden Death (1995)", 74.64, 24.79, 3, 1, "Vhagar/S", ""),
    (
        "Oldboy (Oldeuboi) (2003)",
        73.91,
        52.71,
        1,
        5,
        "Vhagar/O",
        "heavy grain; weakest ratio of the job",
    ),
    ("Riddick (2013)", 71.52, 17.92, 1, 1, "Vhagar/R", ""),
    (
        "Gemini Man (2019)",
        76.35,
        18.87,
        9,
        19,
        "Vhagar/G",
        "59.94 fps HFR; largest single reclaim of the job",
    ),
    (
        "Leaving Las Vegas (1995)",
        70.64,
        40.99,
        4,
        4,
        "Vhagar/L",
        "heavy 90s film grain; 14h51m; 0 decoder errors",
    ),
]

if os.path.exists(core.LEDGER):
    sys.exit(f"refusing to seed: {core.LEDGER} already exists")

for title, src, out, a, s, dest, note in STATE:
    core.record(
        Entry(
            title=title,
            source_bytes=round(src * GIB),
            output_bytes=round(out * GIB),
            audio=a,
            subs=s,
            dest=dest,
            note=note,
            provenance="state-file",
            exact=False,
        )
    )

core.record(
    Entry(
        title="Wanted (2008)",
        source_bytes=None,  # deleted before its size was recorded
        output_bytes=os.path.getsize(
            "/Volumes/Vermithor/Media/4K Movies/W/Wanted (2008)/Wanted (2008) 2160p HEVC.mkv"
        ),
        audio=1,
        subs=28,
        dest="Vermithor/W",
        note="state file was never flipped from RUNNING; output found on the NAS. "
        "Original size unrecoverable, so this row is excluded from reclaim totals.",
        provenance="recovered",
        exact=False,
    )
)

rows = core.ledger()
print(f"seeded {len(rows)} rows -> {core.LEDGER}")
known = [r for r in rows if r["saved_bytes"]]
print(
    f"reclaimed (from {len(known)} rows with both sizes): "
    f"{sum(r['saved_bytes'] for r in known) / GIB:.2f} GiB"
)
print(f"rows with unknown original size: {len(rows) - len(known)}")
