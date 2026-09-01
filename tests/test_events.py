"""The Events tab's timeline parser (dashboard/events.py).

The tab exists to debug outages like 2026-09-01's (a dead hand-run sync
halting the driver while the ladder killed the encode), so the parser's
honesty rules matter more than its completeness: detail lines fold into the
event above instead of becoming noise rows, and a line with no timestamp is
shown with none — the ONE mtime exception is a watch log's final line,
because the watcher writes it and exits.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import events
from pipeline import core

DRIVER_LOG = """\
2026-09-01 15:24:41  === autopilot up (dry-run=false) ===
2026-09-01 15:24:46  START Addams Family 2, The (2021) at CRF 14
2026-09-01 15:24:46    HandBrake pid 75467
2026-09-01 15:24:52    tracks OK 1a/10s
2026-09-01 15:30:29  JUDGE Species (1995)
2026-09-01 15:30:29  HALTED: Species (1995) needs a human: halt-decoder-errors
ssh push: Species (1995) 2160p HEVC.mkv (40.83 GB) -> host:/dir
2026-09-01 15:35:00  CYCLE COMPLETE Species (1995)
"""

WATCH_LOG = """\
QUARTER|Movie (2000)|25%|current 1 GB|projected 5 GB|original 50 GB|10%|OK|ETA 1h
COMPLETE|Movie (2000)|2026-08-31 19:33:54|5.46 GB from 56.90 GB (90% smaller)
KILLED|Movie (2000)|projected 11.3% of original at 5.22% (CRF 14)|band 30-80|partial deleted|next: CRF 12
"""


class Timeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, ".autopilot.log"), "w") as f:
            f.write(DRIVER_LOG)
        watch = os.path.join(self.tmp.name, ".watch-movie2000.log")
        with open(watch, "w") as f:
            f.write(WATCH_LOG)
        # Deterministic ordering: the watch log's mtime (which stamps its
        # final KILLED line) predates every driver event in the fixture.
        import time
        old = time.time() - 2 * 86400
        os.utime(watch, (old, old))
        self.patch = mock.patch.object(core, "X9", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_newest_first_and_kinds(self):
        evs = events.events()
        self.assertEqual(evs[0]["kind"], "cycle")           # 15:35 CYCLE
        kinds = [e["kind"] for e in evs]
        for k in ("halted", "judge", "start", "up", "killed", "complete"):
            self.assertIn(k, kinds)

    def test_indented_and_unstamped_lines_are_detail_not_events(self):
        evs = events.events()
        start = [e for e in evs if e["kind"] == "start"][0]
        self.assertIn("HandBrake pid 75467", start["detail"])
        self.assertIn("tracks OK 1a/10s", start["detail"])
        halted = [e for e in evs if e["kind"] == "halted"][0]
        self.assertIn("ssh push", halted["detail"])
        # None of the folded lines appear as rows of their own.
        self.assertFalse(any("HandBrake pid" in e["text"] for e in evs))
        self.assertFalse(any(e["text"].startswith("ssh push") for e in evs))

    def test_watch_killed_final_line_gets_mtime_earlier_lines_none(self):
        evs = events.events()
        killed = [e for e in evs if e["kind"] == "killed"][0]
        # KILLED is the file's last line -> stamped from mtime (today).
        self.assertIsNotNone(killed["ts"])
        complete = [e for e in evs if e["kind"] == "complete"][0]
        self.assertEqual(complete["ts"], "2026-08-31 19:33:54")
        # QUARTER progress noise never becomes an event.
        self.assertFalse(any("QUARTER" in e["text"] for e in evs))

    def test_killed_not_final_claims_no_time(self):
        with open(os.path.join(self.tmp.name, ".watch-movie2000.log"), "w") as f:
            f.write("KILLED|Movie (2000)|projected 90%|band 30-80|partial deleted|next: CRF 18\n"
                    "COMPLETE|Movie (2000)|2026-08-31 19:33:54|done\n")
        killed = [e for e in events.events() if e["kind"] == "killed"][0]
        self.assertIsNone(killed["ts"])

    def test_limit_keeps_the_newest(self):
        evs = events.events(limit=2)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["kind"], "cycle")

    def test_unmounted_x9_is_empty_never_an_error(self):
        with mock.patch.object(core, "X9", os.path.join(self.tmp.name, "gone")):
            self.assertEqual(events.events(), [])
            self.assertIn("nolog", events.rev())

    def test_rev_moves_when_the_log_grows(self):
        before = events.rev()
        with open(os.path.join(self.tmp.name, ".autopilot.log"), "a") as f:
            f.write("2026-09-01 16:00:00  REPLENISH\n")
        self.assertNotEqual(before, events.rev())


if __name__ == "__main__":
    unittest.main()
