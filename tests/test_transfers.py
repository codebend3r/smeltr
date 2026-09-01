"""_transfers() rate reporting — the caption must not flicker.

The push rate is measured from growth between observation frames, but SMB
stat caching makes many frames report no growth at all. The rate used to be
reported ONLY on a growth frame, so the "· 39 MB/s · 16m left" suffix blinked
in and out of the History tab's transfer caption every few seconds — reported
live on 2026-09-01. The rule now: a measured rate is HELD across no-growth
frames and cleared by exactly two things — a stall (>120 s without growth,
a fact about the wire) or a new attempt (a partial that shrank).

Filesystem is real (temp dirs); core.X9 and core.LIBRARY_ROOTS are patched.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import server
from pipeline import core

FOLDER = "Movie (2000)"
OUT = "Movie (2000) 2160p HEVC.mkv"


class TransferRateHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        x9 = os.path.join(self.tmp.name, "x9")
        os.makedirs(os.path.join(x9, FOLDER))
        with open(os.path.join(x9, FOLDER, OUT), "wb") as f:
            f.write(b"x" * 1000)
        root = os.path.join(self.tmp.name, "lib")
        os.makedirs(os.path.join(root, "M", FOLDER))
        self.part = os.path.join(root, "M", FOLDER, OUT + ".partial")
        self._partial(100)
        self.patches = [mock.patch.object(core, "X9", x9),
                        mock.patch.object(core, "LIBRARY_ROOTS", [root])]
        for p in self.patches:
            p.start()
        server._xfer_track.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _partial(self, n):
        with open(self.part, "wb") as f:
            f.write(b"x" * n)

    def _row(self):
        rows = [r for r in server._transfers() if r["title"] == FOLDER]
        self.assertEqual(len(rows), 1, "expected exactly one transfer row")
        return rows[0]

    def test_first_frame_claims_no_rate(self):
        # One observation is not a measurement.
        self.assertIsNone(self._row()["rate_bps"])

    def test_rate_appears_on_growth_and_holds_without_it(self):
        self._row()                      # anchor
        time.sleep(0.02)
        self._partial(400)
        grown = self._row()
        self.assertIsNotNone(grown["rate_bps"])
        # THE regression: the next frame sees no growth (stat cadence, not a
        # stopped wire) and must keep reporting the held rate, same value.
        held = self._row()
        self.assertEqual(held["rate_bps"], grown["rate_bps"])
        self.assertFalse(held["stalled"])

    def test_stall_clears_the_held_rate(self):
        self._row()
        time.sleep(0.02)
        self._partial(400)
        self.assertIsNotNone(self._row()["rate_bps"])
        rec = server._xfer_track[FOLDER]
        rec["grew"] -= 200               # no growth for >120 s
        old = time.time() - 200
        os.utime(self.part, (old, old))  # and the mtime agrees
        row = self._row()
        self.assertTrue(row["stalled"])
        self.assertIsNone(row["rate_bps"])

    def test_a_rate_older_than_the_hold_window_is_dropped(self):
        # Not yet "stalled" (that needs >120 s AND a stale mtime), but the
        # held measurement has aged past RATE_HOLD_SECONDS — the caption
        # keeps its byte counts and drops the countdown.
        self._row()
        time.sleep(0.02)
        self._partial(400)
        self.assertIsNotNone(self._row()["rate_bps"])
        server._xfer_track[FOLDER]["grew"] -= (server.RATE_HOLD_SECONDS + 1)
        row = self._row()
        self.assertFalse(row["stalled"])
        self.assertIsNone(row["rate_bps"])

    def test_a_shrunken_partial_is_a_new_attempt_with_no_rate(self):
        self._row()
        time.sleep(0.02)
        self._partial(400)
        self.assertIsNotNone(self._row()["rate_bps"])
        self._partial(50)                # smaller = restarted push
        row = self._row()
        self.assertIsNone(row["rate_bps"])
        self.assertFalse(row["stalled"])


if __name__ == "__main__":
    unittest.main()
