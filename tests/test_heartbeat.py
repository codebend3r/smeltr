"""The hourly "is anything encoding" check.

What is worth pinning here, and why each one:

  * the ALL-CLEAR is the only path that exits 0. Everything else must flag.
    A heartbeat that stays quiet on a state it does not recognise is worse
    than no heartbeat, because it converts an outage into a green tick.
  * the TWO-SAMPLE rule. One idle reading is not an alert -- the driver hands
    off between encodes in seconds and an hourly probe will eventually land
    in that window. This is the same strike-and-confirm guard
    .watch-encode.sh uses before killing an encode, and the bug it prevents
    (crying wolf every few days, until the alert is ignored) is exactly the
    bug that makes an alerting system worthless.
  * BLINDNESS IS NOT "FINISHED". A check that cannot read the staging drive
    must say so. The watchdog's documented failure is that a LaunchAgent
    without Full Disk Access reads an empty drive and reports STOP
    CONDITION -- the job looks done. That must never happen here, so the
    core probe is `ps` (which needs no FDA) and the unreadable-drive branch
    is tested explicitly.
  * it WRITES NOTHING. This runs beside a live pipeline holding irreplaceable
    files; a diagnostic that can mutate state is a diagnostic that can cause
    the outage it reports.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import heartbeat, notify  # noqa: E402
from pipeline import core  # noqa: E402


class Fake:
    """Swaps the module's readings out for scripted ones. Nothing here touches
    a real process table, the staging drive or a webhook."""

    def __init__(self, test, **kw):
        self.test, self.kw, self.saved = test, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            target = heartbeat if hasattr(heartbeat, k) else core
            self.saved[k] = (target, getattr(target, k))
            setattr(target, k, v)
        return self

    def __exit__(self, *a):
        for k, (target, v) in self.saved.items():
            setattr(target, k, v)


def _hb(running):
    return lambda: ([{"pid": 1}] if running else [])


class AllClear(unittest.TestCase):
    def test_a_running_handbrake_is_the_only_ok(self):
        with Fake(self, _handbrakes=_hb(True), live_encodes=lambda: []):
            r = heartbeat.check(confirm_seconds=0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], heartbeat.ENCODING)

    def test_the_ok_line_names_the_encoder_and_its_own_scale(self):
        """A bare number is unreadable across two mirrored scales: CQ 60 and
        CRF 14 mean opposite things, and the ladder inverts between them."""
        live = [{"folder": "Bloodsport (1988)", "encoder": "vt_h265_10bit",
                 "crf": 60.0, "pct": 6.5}]
        with Fake(self, _handbrakes=_hb(True), live_encodes=lambda: live):
            r = heartbeat.check(confirm_seconds=0)
        self.assertIn("CQ 60", r["text"])
        self.assertNotIn("CQ 60.0", r["text"])  # every rung is an integer
        self.assertIn("Bloodsport", r["text"])

    def test_a_broken_diagnosis_cannot_turn_a_healthy_check_into_an_alert(self):
        def boom():
            raise OSError("ps exploded")

        with Fake(self, _handbrakes=_hb(True), live_encodes=boom):
            r = heartbeat.check(confirm_seconds=0)
        self.assertTrue(r["ok"])


class TwoSamples(unittest.TestCase):
    def test_an_encode_starting_during_the_wait_is_not_an_alert(self):
        """The gap between one encode finishing and the next starting is real
        and a few seconds long. Flagging it would train the operator to
        ignore this alert, which is the only failure mode that matters."""
        seen = {"n": 0}

        def probe():
            seen["n"] += 1
            return [] if seen["n"] == 1 else [{"pid": 1}]

        slept = []
        # The first idle reading runs diagnose() for real. Under `bun run test`
        # the shell suites run sandboxed copies of autopilot.sh, so the real
        # _driver_running() sees one and diagnose() walks on to core.queue(),
        # which stats the NAS mounts -- a wedged SMB share then hangs the run.
        with Fake(self, _handbrakes=probe, live_encodes=lambda: [],
                  X9=os.path.dirname(__file__), _driver_running=lambda: False):
            r = heartbeat.check(confirm_seconds=60, sleep=slept.append)
        self.assertTrue(r["ok"])
        self.assertEqual(slept, [60], "the confirming wait must actually happen")

    def test_two_idle_readings_do_flag(self):
        with Fake(self, _handbrakes=_hb(False), _driver_running=lambda: True,
                  paused=lambda: True):
            r = heartbeat.check(confirm_seconds=60, sleep=lambda s: None)
        self.assertFalse(r["ok"])

    def test_confirm_zero_skips_the_wait_entirely(self):
        slept = []
        with Fake(self, _handbrakes=_hb(False), _driver_running=lambda: True,
                  paused=lambda: True):
            heartbeat.check(confirm_seconds=0, sleep=slept.append)
        self.assertEqual(slept, [])


class Diagnosis(unittest.TestCase):
    """Each state is a different thing for a person to go and do, so the
    precedence between them is behaviour, not presentation."""

    def test_an_unreadable_drive_reports_blindness_never_finished(self):
        with Fake(self, X9="/nope/not/mounted"):
            state, text = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.BLIND)
        self.assertNotIn("finished", text.lower())
        self.assertIn("Full Disk Access", text)

    def test_blindness_outranks_every_other_explanation(self):
        """Tested first ON PURPOSE. A blind check cannot know whether the
        driver is running or the queue is empty, so any answer it gave about
        them would be a guess presented as a fact."""
        with Fake(self, X9="/nope", _driver_running=lambda: False,
                  paused=lambda: True):
            state, _ = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.BLIND)

    def test_no_driver(self):
        with Fake(self, X9=os.path.dirname(__file__), _driver_running=lambda: False):
            state, text = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.NO_DRIVER)
        self.assertIn("autopilot.sh", text)

    def test_paused_still_flags_but_says_it_was_deliberate(self):
        """A pause is not a fault. It is still an idle encoder, and the
        operator asked to be told when the encoder is idle -- so it flags,
        and the sentence makes clear nobody needs to panic."""
        with Fake(self, X9=os.path.dirname(__file__), _driver_running=lambda: True,
                  paused=lambda: True):
            state, text = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.PAUSED)
        self.assertIn("deliberate", text)

    def test_wedged_is_ready_plus_alive_plus_nothing_running(self):
        """The worst case and the reason this file exists: every precondition
        met and still no encode. Nothing inside the pipeline reports this."""
        with Fake(self, X9=os.path.dirname(__file__), _driver_running=lambda: True,
                  paused=lambda: False,
                  queue=lambda: [{"title": "Skyscraper (2018)"}],
                  pick_next=lambda rows: (rows[0], set())):
            state, text = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.WEDGED)
        self.assertIn("Skyscraper (2018)", text)
        self.assertIn(".autopilot.log", text)

    def test_a_wait_reason_is_quoted_back_in_words(self):
        with Fake(self, X9=os.path.dirname(__file__), _driver_running=lambda: True,
                  paused=lambda: False, queue=lambda: [],
                  pick_next=lambda rows: (None, {"errored"})):
            state, text = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.WAITING)
        self.assertIn("Errors tab", text)

    def test_an_empty_queue_says_confirm_the_mount_first(self):
        """An unmounted NAS empties the queue, which renders identically to a
        finished job. Same rule as core.queue()/report: never present total
        sensor failure as success."""
        with Fake(self, X9=os.path.dirname(__file__), _driver_running=lambda: True,
                  paused=lambda: False, queue=lambda: [],
                  pick_next=lambda rows: (None, set())):
            state, text = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.STOPPED)
        self.assertIn("mounted", text)

    def test_a_queue_that_cannot_be_read_is_blindness_not_a_verdict(self):
        def boom():
            raise OSError("SMB timeout")

        with Fake(self, X9=os.path.dirname(__file__), _driver_running=lambda: True,
                  paused=lambda: False, queue=boom):
            state, _ = heartbeat.diagnose()
        self.assertEqual(state, heartbeat.BLIND)


class Alerting(unittest.TestCase):
    def test_idle_is_first_in_priority_so_a_burst_never_drops_it(self):
        self.assertEqual(notify.PRIORITY[0], "idle")

    def test_the_kind_has_a_label(self):
        """An unmapped kind renders as "?" -- which is what the operator would
        receive at the exact moment the pipeline stopped."""
        self.assertIn("idle", notify.WHATS)
        _, label = notify.WHATS["idle"]
        self.assertIn("NOTHING IS ENCODING", label)

    def test_no_config_still_exits_nonzero_and_says_where_it_went(self):
        """Silence is the one unacceptable outcome. If it cannot send, it must
        still fail loudly on stderr rather than exiting 0."""
        said = []
        with Fake(self, SMELTR_DIR="/nope/no/config"):
            rc = heartbeat.alert({"text": "nothing is encoding"}, err=said.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any("NOTHING IS ENCODING" in s for s in said))

    def test_it_never_touches_the_event_notifier_cursor(self):
        """notify.cursor is the event notifier's seen-set. Writing it here
        would silently swallow encode/deletion notifications.

        Checked against the parsed NAMES, not the file text -- the module
        docstring says "notify.cursor" out loud, and a substring test that
        cannot tell prose from code fails on the comment explaining the rule
        it is enforcing.
        """
        import ast

        with open(heartbeat.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        used = {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        } | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertNotIn("CURSOR_NAME", used)
        self.assertNotIn("Notifier", used)
        self.assertNotIn("cursor_path", used)


class PureObserver(unittest.TestCase):
    def test_it_starts_kills_and_deletes_nothing(self):
        """It runs beside a pipeline holding irreplaceable files. A diagnostic
        that can mutate state is a diagnostic that can cause the outage it
        reports."""
        with open(heartbeat.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("Popen", "os.remove", "os.unlink", "shutil.rmtree",
                    "os.kill", "rename", '"w"', "'w'"):
            self.assertNotIn(bad, src, f"heartbeat.py must not {bad}")

    def test_the_launch_agent_fires_on_the_hour(self):
        """StartInterval counts from LOAD, so a reload at 14:37 would pin every
        future check to :37 past. The operator asked for the hour."""
        plist = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ops", "com.smeltr.heartbeat.plist",
        )
        with open(plist, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("StartCalendarInterval", text)
        self.assertNotIn("<key>StartInterval</key>", text)
        self.assertIn("<key>Minute</key><integer>0</integer>", text)
        # launchd fails the job with EX_CONFIG before it runs if it cannot
        # create the log, and the staging drive may be unmounted.
        self.assertNotIn("/Volumes/", text)


if __name__ == "__main__":
    unittest.main()
