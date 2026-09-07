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
        # The watcher's own default arguments are the DEFAULT encoder and its
        # pivot (VideoToolbox CQ 70 since 2026-09-06): a caller that omits
        # [quality]/[encoder] must not land on a rung the ladder cannot leave.
        self.assertIn('Q="${6:-%d}"' % core.DEFAULT_QUALITY, src)
        self.assertIn('ENC="${7:-%s}"' % core.DEFAULT_ENCODER, src)
        # And the VT ladder pivots on that default too, both arms, on the
        # reversed scale: too big steps DOWN, too small steps UP.
        vt = sorted(core.ENCODER_CHOICES["vt_h265_10bit"])
        vp = vt.index(core.DEFAULT_QUALITIES["vt_h265_10bit"])
        vt_up = vt[vp:]            # too small -> higher CQ (bigger file)
        vt_down = vt[: vp + 1][::-1]  # too big -> lower CQ (smaller file)
        for arm, direction in ((vt_up, "small"), (vt_down, "big")):
            for a, b in zip(arm, arm[1:]):
                self.assertEqual(_next_rung("vt_h265_10bit", a, direction), str(b))
            self.assertEqual(
                _next_rung("vt_h265_10bit", arm[-1], direction), "none-too-" + direction
            )


class LastRungFinishes(unittest.TestCase):
    """Past the last rung the encode RUNS ON (operator's rule, 2026-09-06).

    The ladder used to kill an out-of-band encode it had no rung left to
    retry at, which threw away hours of work and left the title in the
    ERROR state with no output at all. Now the terminal rung -- x265 CRF 22
    when too big and CRF 10 when too small, VideoToolbox CQ 50 and CQ 70 on
    the mirrored arms -- is allowed to finish, and the finished file is
    judged like any other. Deletion safety is NOT part of this: verdict.py
    never reads the ladder, so an out-of-band file still gets a non-good
    verdict and the library original still survives.
    """

    def _kill_block(self):
        """The `case "$NEXTQ" in ... esac` the strike counter fires."""
        src = _read_repo_file("staging", "watch-encode.sh")
        body = src.split('NEXTQ=$(next_rung', 1)[1]
        return body.split("      esac", 1)[0]

    def _arm(self, name, code_only=False):
        """One arm of that case, `none-*)` or the `*)` fallback.

        `code_only` drops comment lines: the assertions about what an arm
        DOES must not be satisfied or broken by prose that happens to
        contain the word.
        """
        block = self._kill_block()
        arms = block.split("        none-*)", 1)[1]
        end, _, rest = arms.partition("        *)")
        out = end if name == "none" else rest
        if code_only:
            out = "\n".join(
                ln for ln in out.splitlines() if not ln.lstrip().startswith("#")
            )
        return out

    def test_the_terminal_rung_is_not_killed(self):
        end = self._arm("none", code_only=True)
        self.assertNotIn("kill ", end)
        self.assertNotIn('rm -f "$OUT"', end)
        self.assertIn("FINAL|", end)
        # ...and it must NOT break out of the loop: the watcher still has to
        # report COMPLETE (or FAILED) for the run it just let through.
        self.assertNotIn("break", end)

    def test_a_rung_that_exists_is_still_killed(self):
        rest = self._arm("rest", code_only=True)
        self.assertIn('kill "$HBPID"', rest)
        self.assertIn('rm -f "$OUT"', rest)
        self.assertIn("KILLED|", rest)
        self.assertIn("break", rest)

    def test_the_finality_gate_is_per_direction(self):
        """One arm running out must NOT switch off the other arm's kill.

        A single flag did exactly that: a noisy low sample at 6% progress on
        a rung reached by laddering UP reported FINAL and disarmed the band
        check entirely, so the blowup that put the encode on that rung ran
        unopposed to 1000% of source. The other direction keeps full
        strike-and-kill authority; `tests/test_watch_finish.sh` drives it.
        """
        src = _read_repo_file("staging", "watch-encode.sh")
        self.assertIn('FINAL_DIRS="$FINAL_DIRS $DIR"', src)
        self.assertIn('case " $FINAL_DIRS " in', src)
        self.assertNotIn("LASTRUNG", src)

    def test_the_driver_never_sees_a_ladder_it_can_error_on(self):
        """A current watcher writes no `next: none-*`, so the driver's
        exhausted branch cannot fire -- but it stays, because a watcher
        launched before this deploy is still running and already killed its
        encode."""
        we = _read_repo_file("staging", "watch-encode.sh")
        self.assertNotIn("next: Q ${NEXTQ}", self._arm("none", code_only=True))
        self.assertIn("next: Q ${NEXTQ}", we)
        self.assertIn("none*)", _read_repo_file("staging", "autopilot.sh"))

    def test_the_final_line_carries_its_own_clock(self):
        """It does NOT exit, so QUARTER lines follow it.

        KILLED and FAILED are stamped from the log's mtime because the
        watcher writes them and leaves. FINAL keeps running, so within the
        hour it stops being the last line and an mtime stamp decays to "—"
        on the one row that records the ladder ending.
        """
        end = self._arm("none", code_only=True)
        self.assertIn("date '+%Y-%m-%d %H:%M:%S'", end)
        ev = _read_repo_file("dashboard", "events.py")
        self.assertIn('"ts": parts[2]', ev.split('word == "FINAL"', 1)[1])
        # ...and parts[2] is only trusted when it looks like a timestamp: an
        # earlier wording put the projection there, which became both the
        # Time cell and the sort key and pinned the row to the top forever.
        self.assertIn("_STAMP.match(parts[2])", ev)

    def test_the_quality_carries_its_scale(self):
        """CRF and CQ are mirrored scales; a bare Q10 beside a Q70 for the
        same situation cannot be read."""
        src = _read_repo_file("staging", "watch-encode.sh")
        self.assertIn("q_label()", src)
        self.assertIn("${QL}", self._arm("none", code_only=True))

    def test_the_wording_does_not_claim_a_rung_it_is_not_on(self):
        """Four of the eight cases that reach FINAL are the oscillation
        guard -- a rung violated in the direction its own arm cannot step.
        Calling a too-small projection at CRF 16 "the last rung of the small
        arm" names a rung that is not on that arm at all."""
        end = self._arm("none", code_only=True)
        self.assertIn("no rung left", end)
        self.assertNotIn("last rung on the", end)

    def test_neither_arm_is_told_nothing_is_deleted(self):
        """The too-SMALL arm lands where the verdict says `good`.

        15.0-30.0% of source is below the band but above the floor, which
        syncs and deletes the ~90 GB library original unattended -- the old
        behaviour killed that encode so it never existed to be judged. A
        message averaging the two arms into one reassurance is the one a
        tired person goes back to sleep on.
        """
        src = _read_repo_file("dashboard", "notify.py")
        block = src.split('if kind == "lastrung":', 1)[1].split("if kind ==", 1)[0]
        # Fails toward the arm that deletes: only a positive "too-big"
        # earns the reassuring wording.
        self.assertIn('"too-big" not in rest', block)
        self.assertIn("SYNCS and deletes", block)
        self.assertIn("no-saving", block)
        self.assertNotIn("nothing is deleted on this", src)
        # ...and the verdict really does say that, on the live baseline.
        hist = core.history_ratios()
        if len(hist) >= core.MIN_HISTORY:
            self.assertEqual(core._verdict(15.1, hist, 15.1)[0], "good")
            self.assertEqual(core._verdict(29.9, hist, 29.9)[0], "good")
            self.assertEqual(core._verdict(86.4, hist, 86.4)[0], "no-saving")

    def test_the_finish_is_reported_where_a_human_reads(self):
        """No driver line follows a FINAL, so the tab and the notifier are
        the only places it can surface."""
        ev = _read_repo_file("dashboard", "events.py")
        self.assertIn('"FINAL"', ev)
        self.assertIn('"kind": "lastrung"', ev)
        # Both watcher wordings of an OLD exhaustion still classify.
        self.assertIn('"next: CRF none-" in line', ev)
        self.assertIn('"next: Q none-" in line', ev)
        app = _read_repo_file("web", "app.js")
        self.assertIn('lastrung: "bad"', app)
        self.assertIn('lastrung: "last rung"', app)
        self.assertIn('"last-rung"', _read_repo_file("dashboard", "notify.py"))


if __name__ == "__main__":
    unittest.main()
