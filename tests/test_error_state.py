"""The ladder-exhausted ERROR state (2026-08-31).

When .watch-encode.sh has killed a title at every rung (up 14-16-18-20-22 for
a too-big projection, down 14-12-10 for too-small), .autopilot.sh writes
$X9/.error-<title> and moves on. That marker must make the title unpickable
-- re-picking would loop the same doomed encode forever -- while never
deleting or skipping anything: the row stays in the queue, red, until a human
deletes the marker.

Pinned here: the marker read, the pick exclusion with its distinct wait
reason (the driver logs it verbatim), and that an errored title neither
halts the pick of OTHER titles nor claims the stop condition.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core


def _stage(x9, title, source=True, output=False):
    d = os.path.join(x9, title)
    os.makedirs(d, exist_ok=True)
    if source:
        with open(os.path.join(d, title + " Remux-2160p.mkv"), "w") as fh:
            fh.write("x")
    if output:
        with open(os.path.join(d, title + " 2160p HEVC.mkv"), "w") as fh:
            fh.write("x")
    return d


def _row(title, **kw):
    base = {"title": title, "mbps": 80.0, "bytes": 1, "staged": True,
            "encoding": False, "skipped": False, "error": False}
    base.update(kw)
    return base


class MarkerRead(unittest.TestCase):
    def test_marker_first_line_is_the_note(self):
        with tempfile.TemporaryDirectory() as x9, \
                mock.patch.object(core, "X9", x9):
            with open(os.path.join(x9, ".error-Tron (1982)"), "w") as fh:
                fh.write("Tron (1982): CRF ladder exhausted (none-too-small)\n")
            self.assertEqual(
                core.error_marker("Tron (1982)"),
                "Tron (1982): CRF ladder exhausted (none-too-small)")

    def test_no_marker_means_none(self):
        with tempfile.TemporaryDirectory() as x9, \
                mock.patch.object(core, "X9", x9):
            self.assertIsNone(core.error_marker("Tron (1982)"))

    def test_empty_marker_still_reads_as_an_error(self):
        """A zero-byte marker (interrupted write) is still an error state --
        an empty note must not silently clear the red row."""
        with tempfile.TemporaryDirectory() as x9, \
                mock.patch.object(core, "X9", x9):
            open(os.path.join(x9, ".error-Tron (1982)"), "w").close()
            self.assertEqual(core.error_marker("Tron (1982)"),
                             "CRF ladder exhausted")


class PickExclusion(unittest.TestCase):
    def test_errored_title_is_passed_over_with_its_own_reason(self):
        with tempfile.TemporaryDirectory() as x9, \
                mock.patch.object(core, "X9", x9):
            _stage(x9, "A (2020)")
            _stage(x9, "B (2021)")
            rows = [_row("A (2020)", error=True), _row("B (2021)")]
            pick, reasons = core.pick_next(rows)
            self.assertEqual(pick["title"], "B (2021)")
            self.assertIn("errored", reasons)

    def test_only_errored_titles_left_is_a_wait_not_a_pick(self):
        with tempfile.TemporaryDirectory() as x9, \
                mock.patch.object(core, "X9", x9):
            _stage(x9, "A (2020)")
            pick, reasons = core.pick_next([_row("A (2020)", error=True)])
            self.assertIsNone(pick)
            self.assertEqual(reasons, {"errored"})

    def test_error_outranks_skip_in_the_reason(self):
        """A row both errored and skipped reports errored -- the error is
        the pipeline's state, the skip merely the operator's overlay."""
        with tempfile.TemporaryDirectory() as x9, \
                mock.patch.object(core, "X9", x9):
            _stage(x9, "A (2020)")
            pick, reasons = core.pick_next(
                [_row("A (2020)", error=True, skipped=True)])
            self.assertIsNone(pick)
            self.assertEqual(reasons, {"errored"})


class DriverContract(unittest.TestCase):
    """next_title.py must translate the reason, and the driver's ladder
    branch must write the marker the pick reads -- one spelling."""

    def test_next_title_names_the_errored_wait(self):
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pipeline", "next_title.py"), encoding="utf-8").read()
        self.assertIn('"errored"', src)

    def test_autopilot_writes_the_marker_core_reads(self):
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "staging", "autopilot.sh"), encoding="utf-8").read()
        self.assertIn('.error-$title', src)
        self.assertIn("none*)", src)
        # The exhausted branch must move ON, not halt: the operator's rule is
        # that a dead ladder costs one title, never the pipeline.
        self.assertNotIn("pathological source", src)

    def test_watcher_ladders_both_ways_from_the_default_rung(self):
        """The ladder PIVOTS on core.CRF_DEFAULT, and both arms leave from it.

        Every other rung is one-directional, so the pivot is the only rung
        with a step in each direction. Move CRF_DEFAULT without re-anchoring
        .watch-encode.sh and the new default becomes a one-way rung: the
        first kill in the unsupported direction exhausts to none-* with
        nothing left to try. That is silent -- it shows up hours later as a
        title gone red on its first auto-kill -- so it is pinned here.
        """
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "staging", "watch-encode.sh"), encoding="utf-8").read()
        ladder = sorted(core.CRF_CHOICES)
        pivot = ladder.index(core.CRF_DEFAULT)
        up = ladder[pivot:]          # too big  -> coarser, starting at the pivot
        down = ladder[:pivot + 1][::-1]   # too small -> finer, starting at the pivot
        self.assertGreater(len(up), 1, "the pivot has nowhere to go when too big")
        self.assertGreater(len(down), 1, "the pivot has nowhere to go when too small")
        for a, b in zip(up, up[1:]):
            self.assertIn("%d) NEXTCRF=%d" % (a, b), src)
        for a, b in zip(down, down[1:]):
            self.assertIn("%d) NEXTCRF=%d" % (a, b), src)
        # The far ends exhaust rather than wrapping, and the watcher's own
        # default argument is the pivot too (a caller that omits [crf] must
        # not land on a rung the ladder cannot leave).
        self.assertIn("none-too-big", src)
        self.assertIn("none-too-small", src)
        self.assertNotIn("%d) NEXTCRF=" % up[-1], src)
        self.assertNotIn("%d) NEXTCRF=" % down[-1], src)
        self.assertIn('CRF="${6:-%d}"' % core.CRF_DEFAULT, src)


if __name__ == "__main__":
    unittest.main()
