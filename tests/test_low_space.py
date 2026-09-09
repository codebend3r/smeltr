"""The 100 GiB staging-drive floor (operator's rule, 2026-09-08).

A new encode may start only while the X9 has at least LOW_SPACE_FLOOR_BYTES
free. Under the floor next_title.py answers exit 3 -- the driver's existing
wait-and-recheck path -- so the RUNNING encode still finishes, records and
moves to complete/, and the next one starts by itself the moment space is
freed. The driver logs ONE stamped `LOW SPACE:` line per episode (tested in
test_autopilot_helpers.sh), which is what emails the operator.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

from pipeline import core
from pipeline import next_title


class Floor(unittest.TestCase):
    def test_the_floor_is_100_gib(self):
        # "100GB" (operator) read as GiB: the stricter of the two readings
        # by 7 GiB, and every other figure on the page is GiB.
        self.assertEqual(core.LOW_SPACE_FLOOR_BYTES, 100 * 1024**3)


class FreeBytes(unittest.TestCase):
    def setUp(self):
        self._x9 = core.X9
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name

    def tearDown(self):
        core.X9 = self._x9
        self._tmp.cleanup()

    def test_reads_the_staging_drive(self):
        st = os.statvfs(self._tmp.name)
        free = core.x9_free_bytes()
        self.assertIsNotNone(free)
        # Same order of magnitude as a statvfs taken beside it; the exact
        # figure moves under a live filesystem.
        self.assertAlmostEqual(free, st.f_bavail * st.f_frsize, delta=1 << 30)

    def test_unreadable_drive_is_none_not_zero(self):
        # An X9 that cannot be stat'd is a different fact from a full one.
        # Zero would trip the floor and send the operator freeing space on
        # a drive that is simply unmounted; the offline/no-source paths own
        # that failure.
        core.X9 = os.path.join(self._tmp.name, "gone")
        self.assertIsNone(core.x9_free_bytes())


class LowSpace(unittest.TestCase):
    def setUp(self):
        self._saved = core.x9_free_bytes

    def tearDown(self):
        core.x9_free_bytes = self._saved

    def test_under_the_floor_blocks(self):
        core.x9_free_bytes = lambda: core.LOW_SPACE_FLOOR_BYTES - 1
        blocked, free = core.low_space()
        self.assertTrue(blocked)
        self.assertEqual(free, core.LOW_SPACE_FLOOR_BYTES - 1)

    def test_at_the_floor_does_not_block(self):
        core.x9_free_bytes = lambda: core.LOW_SPACE_FLOOR_BYTES
        self.assertEqual(core.low_space(), (False, core.LOW_SPACE_FLOOR_BYTES))

    def test_unreadable_drive_does_not_block(self):
        core.x9_free_bytes = lambda: None
        self.assertEqual(core.low_space(), (False, None))


class DriverContract(unittest.TestCase):
    """next_title's exit code IS the API the driver consumes."""

    def setUp(self):
        self._saved = (
            core.offline_roots,
            core.paused,
            core.queue_cached,
            core.pick_next,
            core.low_space,
        )
        core.offline_roots = lambda: []
        core.paused = lambda: False
        core.queue_cached = lambda min_mbps=None: []
        # A pickable staged title: the floor must outrank it.
        core.pick_next = lambda rows: ({"title": "Alpha (2001)", "staged": True}, set())

    def tearDown(self):
        (
            core.offline_roots,
            core.paused,
            core.queue_cached,
            core.pick_next,
            core.low_space,
        ) = self._saved

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        argv, sys.argv = sys.argv, ["next_title.py"]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = next_title.main()
        finally:
            sys.argv = argv
        return rc, out.getvalue(), err.getvalue()

    def test_low_space_is_exit_3_even_with_a_pickable_title(self):
        core.low_space = lambda: (True, 87 * 1024**3 + 300 * 1024**2)
        rc, out, err = self._run()
        self.assertEqual(rc, 3)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("low space"), err)
        self.assertIn("87.3 GiB free", err)
        self.assertIn("100 GiB", err)

    def test_enough_space_picks_as_before(self):
        core.low_space = lambda: (False, 500 * 1024**3)
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "Alpha (2001)")

    def test_paused_outranks_low_space(self):
        # Pause is the operator's own choice and is reported first; the
        # low-space wait is a sensor state the dashboard shows regardless.
        core.paused = lambda: True
        core.low_space = lambda: (True, 1)
        rc, _, err = self._run()
        self.assertEqual(rc, 3)
        self.assertTrue(err.startswith("paused"), err)

    def test_low_space_while_blind_states_both(self):
        core.offline_roots = lambda: ["/Volumes/Vhagar/Media/4K Movies"]
        core.low_space = lambda: (True, 1)
        rc, _, err = self._run()
        self.assertEqual(rc, 3)
        self.assertIn("low space", err)
        self.assertIn("library incomplete", err)


class SummaryCarrier(unittest.TestCase):
    """summary() is the ONE carrier of the floor state -- the same rule as
    `paused`: the live card, the big toggle, the queue's ready row and
    `smeltr report` all read it from here, so no view can disagree."""

    def setUp(self):
        self._saved = (core.live_encodes, core.offline_roots, core.low_space)
        core.live_encodes = lambda: []
        core.offline_roots = lambda: []

    def tearDown(self):
        core.live_encodes, core.offline_roots, core.low_space = self._saved

    def test_summary_carries_low_space_and_the_free_figure(self):
        core.low_space = lambda: (True, 87 * 1024**3)
        s = core.summary(hist=[], q=[])
        self.assertTrue(s["low_space"])
        self.assertEqual(s["x9_free_bytes"], 87 * 1024**3)
        self.assertEqual(s["low_space_floor_bytes"], core.LOW_SPACE_FLOOR_BYTES)

    def test_summary_clear_when_room(self):
        core.low_space = lambda: (False, 900 * 1024**3)
        s = core.summary(hist=[], q=[])
        self.assertFalse(s["low_space"])
        self.assertEqual(s["x9_free_bytes"], 900 * 1024**3)

    def test_unreadable_drive_is_none_and_not_low(self):
        core.low_space = lambda: (False, None)
        s = core.summary(hist=[], q=[])
        self.assertFalse(s["low_space"])
        self.assertIsNone(s["x9_free_bytes"])


class MarkReady(unittest.TestCase):
    """The green row and the start button obey the same floor the driver
    does; next_up survives (the pill says which title waits)."""

    def setUp(self):
        from dashboard import server

        self.server = server
        self._x9 = core.X9
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name
        d = os.path.join(self._tmp.name, "Ready (1999)")
        os.makedirs(d)
        with open(os.path.join(d, "Ready (1999) Remux-2160p.mkv"), "w"):
            pass

    def tearDown(self):
        core.X9 = self._x9
        self._tmp.cleanup()

    def test_low_space_keeps_next_up_but_never_ready(self):
        rows = [{"title": "Ready (1999)", "staged": True}]
        self.server._mark_ready(rows, live=[], paused=False, low_space=True)
        self.assertFalse(rows[0]["ready"])
        self.assertTrue(rows[0]["next_up"])

    def test_room_is_ready(self):
        rows = [{"title": "Ready (1999)", "staged": True}]
        self.server._mark_ready(rows, live=[], paused=False, low_space=False)
        self.assertTrue(rows[0]["ready"])


class HandStartRefuses(unittest.TestCase):
    """POST /api/encode/start is the OTHER thing that spawns HandBrake; it
    must refuse under the floor with a reason, like every other 409."""

    def setUp(self):
        from dashboard import server

        self.server = server
        self._saved = (core.low_space, core.X9)
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name

    def tearDown(self):
        core.low_space, core.X9 = self._saved
        self._tmp.cleanup()

    def test_refused_under_the_floor(self):
        core.low_space = lambda: (True, 87 * 1024**3)
        h = self.server.Handler.__new__(self.server.Handler)
        err = self.server.Handler._apply_encode_start(
            h, {"title": "Alpha (2001)", "encoder": "vt_h265_10bit", "crf": 70}
        )
        self.assertIsInstance(err, str)
        self.assertIn("87.0 GiB free", err)
        self.assertIn("100 GiB", err)


if __name__ == "__main__":
    unittest.main()
