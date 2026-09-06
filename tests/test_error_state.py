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
import subprocess
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
    base = {
        "title": title,
        "mbps": 80.0,
        "bytes": 1,
        "staged": True,
        "encoding": False,
        "skipped": False,
        "error": False,
    }
    base.update(kw)
    return base


class MarkerRead(unittest.TestCase):
    def test_marker_first_line_is_the_note(self):
        with tempfile.TemporaryDirectory() as x9, mock.patch.object(core, "X9", x9):
            with open(os.path.join(x9, ".error-Tron (1982)"), "w") as fh:
                fh.write("Tron (1982): CRF ladder exhausted (none-too-small)\n")
            self.assertEqual(
                core.error_marker("Tron (1982)"),
                "Tron (1982): CRF ladder exhausted (none-too-small)",
            )

    def test_no_marker_means_none(self):
        with tempfile.TemporaryDirectory() as x9, mock.patch.object(core, "X9", x9):
            self.assertIsNone(core.error_marker("Tron (1982)"))

    def test_empty_marker_still_reads_as_an_error(self):
        """A zero-byte marker (interrupted write) is still an error state --
        an empty note must not silently clear the red row."""
        with tempfile.TemporaryDirectory() as x9, mock.patch.object(core, "X9", x9):
            open(os.path.join(x9, ".error-Tron (1982)"), "w").close()
            self.assertEqual(core.error_marker("Tron (1982)"), "CRF ladder exhausted")


class PickExclusion(unittest.TestCase):
    def test_errored_title_is_passed_over_with_its_own_reason(self):
        with tempfile.TemporaryDirectory() as x9, mock.patch.object(core, "X9", x9):
            _stage(x9, "A (2020)")
            _stage(x9, "B (2021)")
            rows = [_row("A (2020)", error=True), _row("B (2021)")]
            pick, reasons = core.pick_next(rows)
            self.assertEqual(pick["title"], "B (2021)")
            self.assertIn("errored", reasons)

    def test_only_errored_titles_left_is_a_wait_not_a_pick(self):
        with tempfile.TemporaryDirectory() as x9, mock.patch.object(core, "X9", x9):
            _stage(x9, "A (2020)")
            pick, reasons = core.pick_next([_row("A (2020)", error=True)])
            self.assertIsNone(pick)
            self.assertEqual(reasons, {"errored"})

    def test_error_outranks_skip_in_the_reason(self):
        """A row both errored and skipped reports errored -- the error is
        the pipeline's state, the skip merely the operator's overlay."""
        with tempfile.TemporaryDirectory() as x9, mock.patch.object(core, "X9", x9):
            _stage(x9, "A (2020)")
            pick, reasons = core.pick_next([_row("A (2020)", error=True, skipped=True)])
            self.assertIsNone(pick)
            self.assertEqual(reasons, {"errored"})


def _read_repo_file(*parts):
    """Read a tracked file relative to the repo root, closing it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *parts), encoding="utf-8") as fh:
        return fh.read()


def _next_rung(encoder, quality, direction):
    """next_rung() out of the real staging/watch-encode.sh.

    WE_TEST=1 makes the script define the function and return before the
    watch loop, so this touches no filesystem and starts nothing.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    we = os.path.join(root, "staging", "watch-encode.sh")
    out = subprocess.run(
        ["bash", "-c",
         'WE_TEST=1 . "$1" 1 f s o 99999 %d x265_10bit; next_rung "$2" "$3" "$4"'
         % core.CRF_DEFAULT,
         "_", we, encoder, str(quality), direction],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


class DriverContract(unittest.TestCase):
    """next_title.py must translate the reason, and the driver's ladder
    branch must write the marker the pick reads -- one spelling."""

    def test_next_title_names_the_errored_wait(self):
        src = _read_repo_file("pipeline", "next_title.py")
        self.assertIn('"errored"', src)

    def test_autopilot_writes_the_marker_core_reads(self):
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertIn(".error-$title", src)
        self.assertIn("none*)", src)
        # The exhausted branch must move ON, not halt: the operator's rule is
        # that a dead ladder costs one title, never the pipeline.
        self.assertNotIn("pathological source", src)

    def test_the_driver_has_no_halt_left(self):
        """A non-good verdict is the TITLE's error state, never a halt.

        Operator's standing rule (2026-09-04): the only sanctioned gap
        between encodes is the pause toggle. A halt on a decoder-error
        verdict idled the encoder for five hours with nine staged titles
        waiting. Every per-title failure -- verdict 2/3/4, no source file,
        track mismatch, an unresolvable library original -- must write the
        marker and move on, so `halt` may not exist as a callable at all.
        """
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertNotIn("halt()", src)
        self.assertNotIn('halt "', src)
        self.assertIn("error_out()", src)
        # Each of the old halt sites now goes through error_out.
        for needle in (
            "needs a human:",
            "no source file",
            "track mismatch:",
            "cannot locate the library original",
        ):
            line = next(ln for ln in src.splitlines() if needle in ln)
            self.assertIn("error_out", line, needle)

    def test_finished_folder_passes_over_an_errored_title(self):
        """The errored folder keeps source AND finished output for a human.

        Without this guard finished_folder() re-finds it every 30 s pass and
        re-judges it forever -- and with the marker present that is the one
        folder the loop must never touch again until a human clears it.
        """
        src = _read_repo_file("staging", "autopilot.sh")
        body = src.split("finished_folder() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('[ -e "$X9/.error-$b" ] && continue', body)

    def test_watcher_ladders_both_ways_from_the_default_rung(self):
        """The ladder PIVOTS on core.CRF_DEFAULT, and both arms leave from it.

        Every other rung is one-directional, so the pivot is the only rung
        with a step in each direction. Move CRF_DEFAULT without re-anchoring
        .watch-encode.sh and the new default becomes a one-way rung: the
        first kill in the unsupported direction exhausts to none-* with
        nothing left to try. That is silent -- it shows up hours later as a
        title gone red on its first auto-kill -- so it is pinned here.
        """
        src = _read_repo_file("staging", "watch-encode.sh")
        ladder = sorted(core.CRF_CHOICES)
        pivot = ladder.index(core.CRF_DEFAULT)
        up = ladder[pivot:]  # too big  -> coarser, starting at the pivot
        down = ladder[: pivot + 1][::-1]  # too small -> finer, starting at the pivot
        self.assertGreater(len(up), 1, "the pivot has nowhere to go when too big")
        self.assertGreater(len(down), 1, "the pivot has nowhere to go when too small")
        # Ask the real function rather than matching its source: the mapping is
        # keyed on the encoder now, so a string match would pass on a table
        # that answers correctly for VideoToolbox and wrongly for x265.
        for arm, direction in ((up, "big"), (down, "small")):
            for a, b in zip(arm, arm[1:]):
                self.assertEqual(_next_rung("x265_10bit", a, direction), str(b))
            # The far end exhausts rather than wrapping.
            self.assertEqual(
                _next_rung("x265_10bit", arm[-1], direction), "none-too-" + direction
            )
        # A violation opposite to a rung's own direction exhausts immediately.
        self.assertEqual(_next_rung("x265_10bit", up[-1], "small"), "none-too-small")
        self.assertEqual(_next_rung("x265_10bit", down[-1], "big"), "none-too-big")
        # The watcher's own default argument is the pivot too: a caller that
        # omits [quality] must not land on a rung the ladder cannot leave.
        self.assertIn('Q="${6:-%d}"' % core.CRF_DEFAULT, src)


if __name__ == "__main__":
    unittest.main()
