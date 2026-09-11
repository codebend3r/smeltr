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
import statistics
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
            self.assertEqual(core.error_marker("Tron (1982)"), core.EMPTY_ERROR_NOTE)

    def test_empty_marker_names_the_write_failure_not_a_ladder(self):
        """2026-09-08: three zero-byte markers were written by a driver on a
        drive at 0 bytes free, and the page captioned every one of them
        "CRF ladder exhausted" -- on titles that ran VideoToolbox. The
        fallback may not guess a cause; it names the one thing an empty
        marker proves, and every surface reads the same constant."""
        self.assertNotIn("CRF", core.EMPTY_ERROR_NOTE)
        self.assertIn("empty", core.EMPTY_ERROR_NOTE)
        for rel in (("web", "app.js"), ("dashboard", "report.py"), ("pipeline", "next_title.py")):
            self.assertNotIn("CRF ladder exhausted", _read_repo_file(*rel), rel)
        self.assertIn(core.EMPTY_ERROR_NOTE, _read_repo_file("web", "app.js").replace('" +\n    "', ""))


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
        # Each of the old halt sites that is NOT a finished encode still goes
        # through error_out.
        for needle in (
            "could not be evaluated",
            "no source file",
            "track mismatch:",
            "cannot locate the library original",
        ):
            line = next(
                ln
                for ln in src.splitlines()
                if needle in ln and not ln.strip().startswith("#")
            )
            self.assertIn("error_out", line, needle)

    def test_a_finished_encode_is_done_whatever_the_verdict(self):
        """Operator's rule (2026-09-07): "it doesn't matter if the output is
        thin or not, it should ALWAYS be moved to complete/ when it's done."

        The Island (2005) finished at 63.6% of source, was judged `thin`, and
        sat red in queue/ as an ERROR with a finished 50 GB file beside its
        source until a human moved it. A verdict decides whether a `good`
        encode SYNCS; it must not decide whether a finished encode is done.
        Exit 2 (a non-good word) and 3 (a ladder code) both route to the
        kept-in-place branch: record --kept, `.done-` marker, complete/.
        Exit 4 -- the output could not be EVALUATED -- can still be an
        error, but since 2026-09-07 not unconditionally: an output that was
        never finished is a partial, not a finished encode, and retries.
        See UnfinishedOutputIsNotAnError below.
        """
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertNotIn("needs a human:", src)
        case = src.split("case $vrc in", 1)[1].split("\n      esac", 1)[0]
        two_three = next(ln for ln in case.splitlines() if ln.strip().startswith("2|3)"))
        self.assertIn("judged=done", two_three)
        self.assertNotIn("error_out", two_three)
        four = case.split("\n        4)", 1)[1]
        self.assertIn("error_out", four)
        self.assertIn("judged=error", four)
        # The kept branch takes `done` under ANY policy, not only no-delete.
        self.assertIn(
            'if [ "$judged" = done ] || { [ "$judged" = ok ] && no_delete_policy; }; then',
            src,
        )
        kept = src.split('if [ "$judged" = done ] ||', 1)[1].split("\n      fi\n", 1)[0]
        self.assertIn("--kept", kept)
        self.assertIn("done_out", kept)
        self.assertNotIn("error_out", kept)
        # A permanent record refusal (output not smaller than source) still
        # moves the folder: done is done, and a retry would never land.
        self.assertIn("not smaller than source", kept)

    def test_finished_folder_passes_over_an_errored_title(self):
        """The errored folder keeps source AND finished output for a human.

        Without this guard finished_folder() re-finds it every 30 s pass and
        re-judges it forever -- and with the marker present that is the one
        folder the loop must never touch again until a human clears it.
        """
        src = _read_repo_file("staging", "autopilot.sh")
        body = src.split("finished_folder() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('[ -e "$X9/.error-$b" ] && continue', body)

    def test_every_rung_ladders_both_ways(self):
        """The starting quality does not matter (operator's rule, 2026-09-07).

        Every rung of every menu steps in BOTH directions, and "none-too-*"
        means the END OF THE MENU and nothing else. This replaced a
        one-directional rule keyed on which side of the default a rung sat:
        a rung above the default could only step further up, on the theory
        that it had been laddered up to. That is true of a rung the ladder
        reached itself and false of a hand-picked START rung, which is what
        `encoder_overrides.json` writes -- the 2026-09-06 batch pinned every
        title at CQ 75, so `next_rung vt_h265_10bit 75 big` answered
        none-too-big and Shazam (2019) ran to 37% projecting 131% of source
        with CQ 70/65/60/55/50 unused underneath it.

        Pinned against the REPO copy, so it has to pass before a deploy.
        """
        for enc, menu in core.ENCODER_CHOICES.items():
            # Smallest file first: CRF descends (22 is the smallest file), CQ
            # ascends. That ordering is what "too big" steps towards.
            rungs = sorted(menu, reverse=(enc != "vt_h265_10bit"))
            self.assertGreater(len(rungs), 2, enc)
            # a is the smaller-file rung of each adjacent pair, b the bigger.
            for a, b in zip(rungs, rungs[1:]):
                self.assertEqual(
                    _next_rung(enc, b, "big"), str(a),
                    "%s: too big at %s must step to %s" % (enc, b, a))
                self.assertEqual(
                    _next_rung(enc, a, "small"), str(b),
                    "%s: too small at %s must step to %s" % (enc, a, b))
            # Only the two ends of a menu exhaust.
            self.assertEqual(_next_rung(enc, rungs[0], "big"), "none-too-big")
            self.assertEqual(_next_rung(enc, rungs[-1], "small"), "none-too-small")

    def test_a_hand_picked_start_rung_can_step_down(self):
        """The exact case that shipped a 131%-of-source encode.

        CQ 75 is above the VT default, so under the old rule it was treated
        as a rung the ladder had climbed to and a too-big projection there
        exhausted with five real rungs beneath it. A start rung is not a
        laddered rung, and the watcher cannot tell them apart -- so neither
        may be one-directional.
        """
        self.assertEqual(_next_rung("vt_h265_10bit", 75, "big"), "70")
        self.assertEqual(_next_rung("x265_10bit", 12, "big"), "14")
        self.assertEqual(_next_rung("x265_10bit", 20, "small"), "18")
        self.assertEqual(_next_rung("vt_h265_10bit", 60, "small"), "65")

    def test_an_off_menu_rung_snaps_instead_of_exhausting(self):
        """A hand-edited quality still ladders.

        core.encoder_for() can only answer a menu entry, so this is reachable
        only by editing the override file by hand -- but refusing to ladder a
        number somebody typed is how a blowup runs unopposed, which is the
        failure this whole change is about. A number off the OTHER encoder's
        scale is off this menu entirely and exhausts rather than being read
        as a rung it is not.
        """
        self.assertEqual(_next_rung("vt_h265_10bit", 72, "big"), "70")
        self.assertEqual(_next_rung("vt_h265_10bit", 72, "small"), "75")
        self.assertEqual(_next_rung("vt_h265_10bit", 18, "big"), "none-too-big")
        self.assertEqual(_next_rung("x265_10bit", 60, "big"), "none-too-big")

    def test_the_no_delete_policy_does_not_disarm_the_ladder(self):
        """no-delete is about the LIBRARY ORIGINAL, not the staging partial.

        The auto-kill deletes a worthless half-encode on the X9 that the
        driver would otherwise match as finished by disk scan. Wiring it to
        `no_delete_policy` switched the entire band check off, which is why
        Shazam (2019) had no ceiling at all: the watcher took
        SMELTR_NO_AUTOKILL=1 and never counted a strike.
        """
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertNotIn("local nokill=", src)
        self.assertNotIn('SMELTR_NO_AUTOKILL="$nokill"', src)
        self.assertIn(
            'SMELTR_NO_AUTOKILL="${SMELTR_NO_AUTOKILL:-0}" nohup "$X9/.watch-encode.sh"',
            src,
        )

    def test_the_watcher_defaults_to_the_default_encoder_and_quality(self):
        """A caller that omits [quality]/[encoder] lands on the real default."""
        src = _read_repo_file("staging", "watch-encode.sh")
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
        # The line that decides is the HIGHER of the relative line and the
        # 15.0 floor (see `test_verdict_calibration.effective_line`): the
        # relative line drifts with the median and had already risen past
        # the floor by 2026-09-11, so a literal 15.1 here went `suspect`
        # while the arm it describes still ends in a `good` verdict.
        hist = core.history_ratios()
        if len(hist) >= core.MIN_HISTORY:
            line = max(
                statistics.median(hist) * core.OUTLIER_FACTOR,
                core.OUTLIER_FLOOR_NORM,
            )
            self.assertLess(line, 29.9, "no `good` range left for a clean source")
            lo = line + 0.1
            self.assertEqual(core._verdict(lo, hist, lo)[0], "good")
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


class UnfinishedOutputIsNotAnError(unittest.TestCase):
    """A verdict exit 4 that says "died mid-write" is a RETRY (2026-09-07).

    Operator's call, on finding Shazam in the error tab: "this does not
    qualify as an error." An output with no completion marker in its
    HandBrake log was never finished -- there is nothing to review and
    nothing was produced. It is the same worthless partial the auto-kill
    throws away.

    Two independent defects produced it, and BOTH are pinned here because
    either one alone still parks a title:

    1. `.watch-encode.sh` killed HandBrake and THEN deleted the partial. In
       between, the file sat on disk with no live encoder -- and
       finished_folder()'s only live-encode guard is encoding_this(), which
       goes false the instant the process dies.
    2. `.autopilot.sh` mapped EVERY exit 4 to error_out(), so the title the
       ladder was mid-way through retrying got a `.error-` marker instead.
       pick_next passes over marked titles, so a thin queue then had nothing
       to start.

    Minions at CQ 65 idled the encoder 1h35m on 2026-09-06 (the sidecar
    variant, patched then with a `! -name '._*'` that did not address
    either defect); Shazam at Q70 idled it 2h27m on 2026-09-07.
    """

    def test_exit_4_is_no_longer_unconditionally_an_error(self):
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertNotIn(
            '4) error_out "$done_folder" "could not be evaluated (verdict exit 4)"',
            src,
        )
        # The mid-write case routes; every OTHER exit 4 still errors, because
        # a missing log or an ambiguous file count really is a human's.
        self.assertIn("died mid-write", src)
        self.assertIn('error_out "$done_folder" "could not be evaluated (verdict exit 4)"', src)

    def test_the_routing_lives_in_the_helper_block(self):
        """Sourced and driven for real by tests/test_midwrite_retry.sh --
        a string match cannot tell wait from retry from error."""
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertIn("midwrite_route() {", src)
        self.assertIn("drop_partial() {", src)
        self.assertIn("case $(midwrite_route \"$done_folder\") in", src)

    def test_only_the_word_error_errors(self):
        """2026-09-08: on a full X9 the strike write failed with ENOSPC and
        bash 3.2 leaked the unwritten count into the captured answer
        ("1\\nretry"), which matched no arm and fell to `*)` -- error_out on
        the FIRST strike, captioned "never completed on 3 attempts". The
        error arm is now the literal word; anything else retries."""
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertIn("bank_strike() {", src)
        # the write happens in a subshell whose stdout IS the file
        self.assertIn("( exec >\"$f\" 2>/dev/null || exit 1; printf '%s\\n' \"$n\" ) 2>/dev/null", src)
        # and only a persisted count is a strike
        self.assertIn("[ \"$(cat \"$f\" 2>/dev/null)\" = \"$n\" ] || return 1", src)
        i = src.index('case $(midwrite_route "$done_folder") in')
        block = src[i : src.index("esac", i)]
        j = block.index("error)")
        self.assertIn("never completed on $MIDWRITE_STRIKES attempts", block[j:])
        star = block[block.index("*)") :]
        self.assertNotIn("error_out", star)
        self.assertIn("judged=retry", star)

    def test_no_room_is_a_wait_and_no_job_is_not_a_track_verdict(self):
        """The two other faces of the same full drive (2026-09-08): an encode
        must not START into a drive that cannot hold it, and a HandBrake that
        never wrote its job configuration has said nothing about tracks."""
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertIn("ENCODE_HEADROOM_PCT=80", src)
        gate = src.index('log "NO ROOM $title:')
        self.assertLess(gate, src.index('log "START $title with $enc at Q$q"'))
        self.assertIn("return 2", src[gate : gate + 400])
        self.assertNotIn("error_out", src[gate : gate + 400])
        nojob = src.index("if ! grep -q 'job configuration:' \"$X9/.hb-${slug}.log\"")
        self.assertLess(nojob, src.index('error_out "$title" "track mismatch:'))
        self.assertIn('log "START FAILED $title: HandBrake wrote no job configuration', src[nojob:])
        self.assertIn('bank_strike "$title"', src[nojob:])

    def test_a_retry_is_bounded(self):
        """A HandBrake crashing on one source leaves the same unfinished
        output every time and would otherwise spin forever. A KILLED line
        clears the count, so a ladder walking five rungs never trips it."""
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertIn("MIDWRITE_STRIKES=3", src)
        self.assertIn('[ "$n" -ge "$MIDWRITE_STRIKES" ]', src)

    def test_a_live_encode_is_never_touched(self):
        """`wait` exists so a partial that might belong to a RUNNING encode
        is never deleted: ps can race, and hours of work is the cost."""
        src = _read_repo_file("staging", "autopilot.sh")
        self.assertIn("if hb_running; then printf 'wait", src)

    def test_the_watcher_hides_the_partial_before_it_kills(self):
        """The ORDERING is the fix -- narrowing the window is not.
        tests/test_watch_kill_race.sh drives it for real."""
        src = _read_repo_file("staging", "watch-encode.sh")
        rename = src.index('mv -f "$OUT" "$OUT.killing"')
        kill = src.index('kill "$HBPID" 2>/dev/null')
        self.assertLess(rename, kill, "the partial must be renamed BEFORE the kill")
        # Both names are cleaned up: the rename can fail on a full or locked
        # volume, and the original is still there if it does.
        self.assertIn('rm -f "$OUT" "$OUT.killing"', src)


if __name__ == "__main__":
    unittest.main()
