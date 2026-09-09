"""The Events tab's timeline parser (dashboard/events.py).

The tab exists to debug outages like 2026-09-01's (a dead hand-run sync
halting the driver while the ladder killed the encode), so the parser's
honesty rules matter more than its completeness: detail lines fold into the
event above instead of becoming noise rows, and a line with no timestamp is
shown with none — the ONE mtime exception is a watch log's final line
(surfaced as APPROXIMATE), because the watcher writes it and exits.

The data-critic findings of 2026-09-01 are each pinned here: watcher "GB"
strings are GiB and are relabelled; FAILED lines (a HandBrake that DIED) are
events, not silence; ladder exhaustion is distinct from a routine kill;
identical runs collapse so a long pause cannot evict the real history; and
`total` exists so truncation is disclosed, never silent.
"""

import os
import sys
import tempfile
import time
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
2026-09-01 15:25:00  waiting: paused from the dashboard; waiting
2026-09-01 15:30:00  waiting: paused from the dashboard; waiting
2026-09-01 15:30:29  JUDGE Species (1995)
2026-09-01 15:30:29  HALTED: Species (1995) needs a human: halt-decoder-errors
size guard PASS: 40.83 GB replaces 65.94 GB (frees 25.11 GB)
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
        self.watch = os.path.join(self.tmp.name, ".watch-movie2000.log")
        with open(self.watch, "w") as f:
            f.write(WATCH_LOG)
        # Deterministic ordering: the watch log's mtime (which stamps its
        # final KILLED line) predates every driver event in the fixture.
        self._age(self.watch)
        self.patch = mock.patch.object(core, "X9", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _age(self, path):
        # An ABSOLUTE instant, not "now minus two days": the fixture's driver
        # lines are dated 2026-09-01, and a relative age overtook them on
        # 2026-09-03, putting the mtime-stamped KILLED line at the top.
        old = time.mktime((2026, 8, 31, 12, 0, 0, 0, 0, -1))
        os.utime(path, (old, old))

    def _evs(self, **kw):
        return events.events(**kw)["events"]

    def test_newest_first_and_kinds(self):
        evs = self._evs()
        self.assertEqual(evs[0]["kind"], "cycle")  # 15:35 CYCLE
        kinds = [e["kind"] for e in evs]
        for k in ("halted", "judge", "start", "up", "killed", "complete"):
            self.assertIn(k, kinds)

    def test_low_space_is_its_own_kind(self):
        # The driver writes ONE stamped LOW SPACE line per episode (the 60 s
        # "waiting: low space" lines are noise). It is the line that emails
        # the operator, so the tab must chip it, never fold it into info.
        with open(os.path.join(self.tmp.name, ".autopilot.log"), "a") as f:
            f.write(
                "2026-09-01 16:00:00  LOW SPACE: low space on the staging drive: "
                "87.3 GiB free, encodes resume at 100 GiB -- free up space; waiting\n"
            )
        evs = self._evs()
        self.assertEqual(evs[0]["kind"], "lowspace")
        self.assertTrue(evs[0]["text"].startswith("LOW SPACE: "))

    def test_indented_and_unstamped_lines_are_detail_not_events(self):
        evs = self._evs()
        start = [e for e in evs if e["kind"] == "start"][0]
        self.assertIn("HandBrake pid 75467", start["detail"])
        self.assertIn("tracks OK 1a/10s", start["detail"])
        self.assertFalse(any("HandBrake pid" in e["text"] for e in evs))
        self.assertFalse(any(e["text"].startswith("size guard") for e in evs))

    def test_watcher_gb_labels_are_relabelled_gib(self):
        # .watch-encode.sh divides by 1073741824 and prints "GB" — the value
        # is GiB, and History labels the identical bytes GiB. Same for the
        # sync script's size-guard line folded into a driver event's detail.
        evs = self._evs()
        complete = [e for e in evs if e["kind"] == "complete"][0]
        self.assertIn("5.46 GiB from 56.90 GiB", complete["text"])
        self.assertNotIn(" GB", complete["text"])
        halted = [e for e in evs if e["kind"] == "halted"][0]
        self.assertIn("40.83 GiB replaces 65.94 GiB", halted["detail"])

    def test_watch_killed_final_line_is_mtime_stamped_and_approx(self):
        evs = self._evs()
        killed = [e for e in evs if e["kind"] == "killed"][0]
        self.assertIsNotNone(killed["ts"])
        self.assertTrue(killed["approx"])
        complete = [e for e in evs if e["kind"] == "complete"][0]
        self.assertEqual(complete["ts"], "2026-08-31 19:33:54")
        self.assertFalse(complete["approx"])
        self.assertFalse(any("QUARTER" in e["text"] for e in evs))

    def test_killed_not_final_claims_no_time(self):
        with open(self.watch, "w") as f:
            f.write(
                "KILLED|Movie (2000)|projected 90%|band 30-80|partial deleted|next: CRF 18\n"
                "COMPLETE|Movie (2000)|2026-08-31 19:33:54|done\n"
            )
        self._age(self.watch)
        killed = [e for e in self._evs() if e["kind"] == "killed"][0]
        self.assertIsNone(killed["ts"])
        self.assertFalse(killed["approx"])

    def test_a_dead_handbrake_is_an_event_not_silence(self):
        # .watch-encode.sh emits FAILED when HandBrake dies — the single most
        # likely reason someone opens this tab.
        with open(self.watch, "w") as f:
            f.write(
                "FAILED|Movie (2000)|HandBrakeCLI died at 47% (reboot/kill?) — delete partial and restart\n"
            )
        self._age(self.watch)
        failed = [e for e in self._evs() if e["kind"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("HandBrakeCLI died at 47%", failed[0]["text"])
        self.assertTrue(failed[0]["approx"])

    def test_ladder_exhaustion_is_distinct_from_a_routine_kill(self):
        # The watcher writes `next: CRF ${NEXTCRF}` and NEXTCRF is the word
        # none-too-small at the bottom rung -- so the line reads
        # "next: CRF none-too-small". The old test matched "next: none-",
        # a string the watcher never writes, and the tab drew every
        # exhaustion as a routine retry (2026-09-05 code-critic finding).
        with open(self.watch, "w") as f:
            f.write(
                "KILLED|Movie (2000)|projected 8% at 10% (CRF 10)|band 30-80|partial deleted|next: CRF none-too-small\n"
            )
        self._age(self.watch)
        kinds = [e["kind"] for e in self._evs()]
        self.assertIn("exhausted", kinds)
        self.assertNotIn("killed", kinds)

    def test_a_three_field_complete_has_no_dangling_separator(self):
        with open(self.watch, "w") as f:
            f.write("COMPLETE|Other (2001)|2026-08-30 10:00:00\n")
        self._age(self.watch)
        complete = [e for e in self._evs() if e["kind"] == "complete"][0]
        self.assertEqual(complete["text"], "COMPLETE Other (2001)")

    def test_identical_runs_collapse_with_a_count(self):
        # A 300 s pause tick repeated for hours must not evict the history.
        evs = self._evs()
        waits = [e for e in evs if e["text"].startswith("waiting:")]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["count"], 2)
        self.assertEqual(waits[0]["ts"], "2026-09-01 15:30:00")  # newest
        singles = [e for e in evs if e["kind"] == "start"][0]
        self.assertEqual(singles["count"], 1)

    def test_total_discloses_truncation(self):
        tl = events.events(limit=2)
        self.assertEqual(len(tl["events"]), 2)
        self.assertGreater(tl["total"], 2)
        self.assertEqual(tl["events"][0]["kind"], "cycle")

    def test_unmounted_x9_is_empty_never_an_error(self):
        with mock.patch.object(core, "X9", os.path.join(self.tmp.name, "gone")):
            tl = events.events()
            self.assertEqual(tl["events"], [])
            self.assertEqual(tl["total"], 0)
            self.assertIn("nolog", events.rev())

    def test_rev_moves_when_the_log_grows(self):
        before = events.rev()
        with open(os.path.join(self.tmp.name, ".autopilot.log"), "a") as f:
            f.write("2026-09-01 16:00:00  REPLENISH\n")
        self.assertNotEqual(before, events.rev())


if __name__ == "__main__":
    unittest.main()
