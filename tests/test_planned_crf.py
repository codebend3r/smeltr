"""The per-title start CRF: chosen in the queue, honoured by the driver.

The picker in the CRF column writes an override that `.autopilot.sh` reads
through `smeltr crf` immediately before it spawns HandBrake. Everything here
guards the two ways that becomes a lie:

  * the value not surviving the round trip -- a skip or a reorder writing the
    overrides file and dropping the CRF map with it, which is silent and only
    shows up hours later as an encode at the wrong rung;
  * `smeltr crf` failing loudly. The driver substitutes its stdout straight
    into -q, so anything but an integer on stdout is a HandBrake that dies at
    startup or, worse, an empty -q. It must answer CRF_DEFAULT to every failure.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from pipeline import core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OverrideRoundTrip(unittest.TestCase):
    def setUp(self):
        self._orig = core.OVERRIDES
        self._tmp = tempfile.TemporaryDirectory()
        core.OVERRIDES = os.path.join(self._tmp.name, "queue_overrides.json")

    def tearDown(self):
        core.OVERRIDES = self._orig
        self._tmp.cleanup()

    def write(self, obj):
        with open(core.OVERRIDES, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_absent_file_reads_as_the_default(self):
        self.assertEqual(core.load_overrides()["crf"], {})
        self.assertEqual(core.planned_crf("Anything (1999)"), core.CRF_DEFAULT)

    def test_round_trip(self):
        core.save_overrides([], [], {"Kubo (2016)": 12})
        self.assertEqual(core.planned_crf("Kubo (2016)"), 12)

    def test_matched_case_insensitively_like_every_other_key(self):
        core.save_overrides([], [], {"Kubo (2016)": 20})
        self.assertEqual(core.planned_crf("kubo (2016)"), 20)
        self.assertEqual(core.planned_crf("KUBO (2016)"), 20)

    def test_a_skip_does_not_drop_the_crf_map(self):
        """save_overrides(skip, priority) predates the CRF map.

        Three callers still pass two arguments. If the third defaulted to {},
        every hand-picked CRF would be erased by the next skip click -- with
        no error, no banner, and no sign until an encode started at the default.
        """
        core.save_overrides([], [], {"Kubo (2016)": 10})
        core.save_overrides(["Shrek (2001)"], [])
        self.assertEqual(core.planned_crf("Kubo (2016)"), 10)
        self.assertEqual(core.load_overrides()["skip"], ["Shrek (2001)"])

    def test_an_off_menu_value_is_dropped_not_clamped(self):
        """The value becomes -q. Guessing a neighbouring rung on the
        operator's behalf is a multi-hour encode nobody asked for."""
        self.write({"skip": [], "priority": [], "crf": {"A (1)": 17,
                                                        "B (2)": 40,
                                                        "C (3)": 0}})
        self.assertEqual(core.load_overrides()["crf"], {})
        self.assertEqual(core.planned_crf("A (1)"), core.CRF_DEFAULT)

    def test_junk_shapes_never_raise(self):
        """The driver must never crash on a UI-written file."""
        for junk in ([], "nope", {"crf": []}, {"crf": {"A": "16"}},
                     {"crf": {"A": True}}, {"crf": {"A": 16.0}}):
            self.write(junk)
            self.assertEqual(core.planned_crf("A"), core.CRF_DEFAULT)

    def test_a_corrupt_file_still_reports_corrupt(self):
        with open(core.OVERRIDES, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        ov = core.load_overrides()
        self.assertTrue(ov["corrupt"])
        self.assertEqual(ov["crf"], {})


class TheMenuIsTheLadder(unittest.TestCase):
    def test_even_rungs_ten_to_twentytwo(self):
        self.assertEqual(core.CRF_CHOICES, (10, 12, 14, 16, 18, 20, 22))

    def test_the_default_is_on_the_menu(self):
        """The picker's "auto" option is labelled with CRF_DEFAULT and the
        driver falls back to it; a default off the menu would be a value the
        page could display but never re-select."""
        self.assertIn(core.CRF_DEFAULT, core.CRF_CHOICES)


class TheLauncherEntryPoint(unittest.TestCase):
    """`smeltr crf <title>` -- what .autopilot.sh actually runs.

    Driven as a subprocess on purpose: the contract the driver depends on is
    "one integer on stdout, exit 0", and that is a property of the process,
    not of planned_crf().
    """

    def run_crf(self, *args, env=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run([sys.executable,
                               os.path.join(ROOT, "pipeline", "crf.py"),
                               *args],
                              capture_output=True, text=True, env=e)

    def test_prints_one_integer_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "queue_overrides.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"skip": [], "priority": [], "crf": {"Kubo": 14}}, fh)
            r = self.run_crf("Kubo", env={"SMELTR_DIR": d})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "14")

    def test_no_title_is_the_default_not_an_error(self):
        r = self.run_crf()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), core.CRF_DEFAULT)

    def test_an_unknown_title_is_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_crf("Nothing Like This (1999)", env={"SMELTR_DIR": d})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), core.CRF_DEFAULT)

    def test_an_unreadable_overrides_file_is_the_default(self):
        """Exit 0 with the default, never a non-zero the driver would handle:
        the alternative is idling the CPU over a preference."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "queue_overrides.json"), "w",
                      encoding="utf-8") as fh:
                fh.write("{ truncated")
            r = self.run_crf("Kubo", env={"SMELTR_DIR": d})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), core.CRF_DEFAULT)


if __name__ == "__main__":
    unittest.main()
