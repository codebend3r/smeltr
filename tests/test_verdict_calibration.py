"""The size thresholds that decide whether a ~90 GB original gets deleted.

Only a `good` verdict syncs and deletes (verdict.py: good -> 0, everything else
-> 2 or 3, and the driver halts on both). So every boundary here is the line
between an unattended deletion and a human being asked to look.

Recalibrated 2026-08-22 against the first 12 measured encodes. The two facts
that drove it, both pinned below:

  * The floor used to test the RAW ratio. Flight (2012) auto-crops
    3840x2160 -> 3840x1600 and keeps 9.4% raw but 12.6% per encoded pixel; the
    raw figure tripped a 12% floor meant to describe full frames.
  * 12% sat above what this library legitimately produces at all. Flight is the
    thinnest output ever made here and its ledger row records SSIM 0.9931/0.9945
    against the cropped original.
"""
import json
import os
import statistics
import unittest

import core


HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "..", "ledger.jsonl")
# The 22 rows the 2026-08-22 recalibration was reasoned about, frozen. The live
# ledger is gitignored (it is appended to on every driver cycle, so a tracked
# copy conflicts constantly), which means a fresh clone -- CI, or this machine
# after `git rm --cached` -- has no ledger at all and these tests used to ERROR
# rather than run. They are the thresholds standing between a bad encode and a
# deleted ~90 GB original, so they must not be the suite's first casualty.
FIXTURE = os.path.join(HERE, "fixtures", "ledger-calibration.jsonl")

# Prefer the LIVE ledger when it exists. That is deliberate and is what makes
# test_flight_still_asks_for_a_human a drift monitor: the baseline moves as new
# encodes land, and this suite is meant to fail when it moves far enough that a
# Flight-class result would auto-sync. The fixture only stands in where there is
# no live ledger to watch, so CI still exercises the replay instead of skipping.
LEDGER = LIVE if os.path.exists(LIVE) else FIXTURE
USING_FIXTURE = LEDGER is FIXTURE

# core.history_ratios() reads core.LEDGER, not this module's. Point it at the
# same file so the baseline and the replayed rows can never come from different
# ledgers -- a mismatch there would silently compare each row against a history
# it was not part of.
core.LEDGER = os.path.abspath(LEDGER)


def rows():
    with open(LEDGER) as fh:
        return [json.loads(l) for l in fh if l.strip()]


class Constants(unittest.TestCase):
    def test_floor_is_normalised_not_raw(self):
        """The old name encoded the bug. Nothing may reintroduce it."""
        self.assertFalse(hasattr(core, "OUTLIER_FLOOR_RAW"))
        self.assertEqual(core.OUTLIER_FLOOR_NORM, 6.0)

    def test_floor_sits_below_the_thinnest_legitimate_encode(self):
        """Flight keeps 12.6% per encoded pixel and is verified good."""
        self.assertLess(core.OUTLIER_FLOOR_NORM, 12.6)

    def test_relative_factor(self):
        self.assertEqual(core.OUTLIER_FACTOR, 0.40)


class Floor(unittest.TestCase):
    """With too little history there is no baseline, so only the floor applies."""

    NO_HIST = []          # len < MIN_HISTORY -> base is None

    def v(self, raw, norm):
        return core._verdict(raw, self.NO_HIST, norm, False)[0]

    def test_below_floor_is_suspect(self):
        self.assertEqual(self.v(4.0, 5.9), "suspect")

    def test_at_floor_is_good(self):
        self.assertEqual(self.v(4.5, 6.0), "good")

    def test_above_floor_is_good(self):
        self.assertEqual(self.v(4.5, 6.1), "good")

    def test_heavy_crop_is_judged_on_encoded_pixels(self):
        """The regression that halted Flight.

        raw 4.5% would have tripped the old 12% raw floor; 12.0% per retained
        pixel is comfortably above the new one. A title must not be called
        implausible for the rows auto-crop correctly threw away.
        """
        self.assertEqual(self.v(4.5, 12.0), "good")

    def test_uncropped_title_is_unaffected(self):
        """With no crop, raw and normalised are the same number."""
        self.assertEqual(self.v(5.9, 5.9), "suspect")
        self.assertEqual(self.v(6.1, 6.1), "good")


class RelativeToHistory(unittest.TestCase):
    """The baseline MOVES: every completed encode appends a row and shifts the
    median. So assert relationships, never a snapshot of today's number -- an
    earlier version of this file hardcoded 35.1% and 14.0% and broke the moment
    Kubo landed."""

    def setUp(self):
        self.hist = core.history_ratios(normalised=True)
        self.base = statistics.median(self.hist)
        self.thr = self.base * core.OUTLIER_FACTOR

    def test_baseline_has_enough_history_to_mean_anything(self):
        self.assertGreaterEqual(len(self.hist), core.MIN_HISTORY)

    def test_baseline_is_plausible_for_this_library(self):
        """Wide bounds on purpose. This catches a broken ledger, not drift."""
        self.assertTrue(10.0 < self.base < 80.0, f"median {self.base:.1f}%")

    def test_just_below_the_threshold_is_suspect(self):
        r = self.thr - 0.1
        self.assertEqual(core._verdict(r, self.hist, r, False)[0], "suspect")

    def test_just_above_the_threshold_is_good(self):
        r = self.thr + 0.1
        self.assertEqual(core._verdict(r, self.hist, r, False)[0], "good")

    def test_flight_still_asks_for_a_human(self):
        """A deliberate decision, not a side effect -- re-decide if this fails.

        Flight (2012) keeps 12.6% per encoded pixel against a ~92 GB original:
        the smallest output this job has ever produced. The SSIM check that
        cleared it was worth having, so OUTLIER_FACTOR was set to keep it above
        the line rather than to auto-sync it.

        The baseline drops as more thin encodes land. If it drops far enough
        that 12.6% clears, this fails ON PURPOSE -- that is a safety threshold
        moving on its own, and it needs a human decision, not a green suite.
        """
        code, note = core._verdict(9.4, self.hist, 12.6, False)
        self.assertEqual(
            code, "suspect",
            f"Flight-class (12.6% normalised) now clears the relative line "
            f"({self.thr:.1f}%). The baseline has drifted to {self.base:.1f}%. "
            f"Deleting a ~92 GB original unreviewed is now possible -- decide "
            f"deliberately whether that is wanted before changing this test.")
        # It trips the RELATIVE rule, not the floor -- the note must not claim
        # a floor that did not fire.
        self.assertNotIn("floor", note)
        self.assertIn("12.6% per retained pixel", note)

    def test_contaminated_baseline_is_disclosed(self):
        """Most ledger rows predate geometry capture and cannot be crop-adjusted.

        Comparing an adjusted encode against a partly-unadjusted baseline
        flatters the outlier; the note has to say so.
        """
        _, note = core._verdict(9.4, self.hist, 12.6, False)
        self.assertIn("only partly crop-adjusted", note)


class Band(unittest.TestCase):
    """The dashboard draws a 30-80% target band; the verdict must agree with it."""

    def setUp(self):
        self.hist = core.history_ratios(normalised=True)

    def v(self, r):
        return core._verdict(r, self.hist, r, False)[0]

    def test_top_of_band_is_thin_not_no_saving(self):
        self.assertEqual(self.v(79.9), "thin")

    def test_above_band_is_no_saving(self):
        self.assertEqual(self.v(80.0), "no-saving")

    def test_no_saving_names_the_band(self):
        self.assertIn("30-80% target band",
                      core._verdict(80.0, self.hist, 80.0, False)[1])

    def test_thin_starts_at_70(self):
        self.assertEqual(self.v(69.9), "good")
        self.assertEqual(self.v(70.0), "thin")

    def test_blowup(self):
        self.assertEqual(self.v(100.0), "blowup")

    def test_below_band_is_not_a_defect(self):
        """4 of the first 12 landed under 30% and every one was good.

        Only values above the relative line are asserted -- below it the
        outlier rule legitimately takes over, and that boundary is pinned in
        RelativeToHistory, not here.
        """
        thr = statistics.median(self.hist) * core.OUTLIER_FACTOR
        for r in (30.0, 25.1, 22.8, 18.6):
            if r <= thr:
                continue
            self.assertEqual(self.v(r), "good", f"{r}% should be good")


class Precedence(unittest.TestCase):
    def test_downscale_outranks_every_size_verdict(self):
        hist = core.history_ratios(normalised=True)
        for r in (5.0, 45.0, 90.0, 130.0):
            self.assertEqual(core._verdict(r, hist, r, True)[0], "downscale")

    def test_no_projection_is_unknown(self):
        self.assertEqual(core._verdict(None, [], None, False)[0], "unknown")


class LedgerRegression(unittest.TestCase):
    """Replay real shipped rows. Anchors are named; everything else is an
    invariant, so a completed encode does not have to be pasted in here."""

    # The rows the calibration was reasoned about. New titles are covered by
    # the invariant below rather than being added to this list.
    ANCHORS = {
        "Flight (2012)": "suspect",      # SSIM-verified good, kept for review
        "Oldboy (Oldeuboi) (2003)": "thin",
        "Shrek (2001)": "good",
        "Hunt for the Wilderpeople (2016)": "good",   # heavy crop: 22.8 raw, 30.8 norm
    }

    def measured(self):
        hist = core.history_ratios(normalised=True)
        for r in rows():
            sb, ob = r.get("source_bytes"), r.get("output_bytes")
            if not sb or not ob:
                continue          # Wanted (2008): no source size, never back-solved
            raw = ob / sb * 100.0
            norm = raw * core.crop_factor(r.get("source_geometry"),
                                          r.get("output_geometry"))
            yield r["title"], raw, norm, core._verdict(raw, hist, norm, False)[0]

    def test_anchor_rows_keep_their_verdict(self):
        seen = {}
        for title, raw, norm, code in self.measured():
            if title in self.ANCHORS:
                seen[title] = code
                self.assertEqual(code, self.ANCHORS[title],
                                 f"{title} at {raw:.1f}% raw / {norm:.1f}% norm")
        self.assertEqual(set(seen), set(self.ANCHORS), "an anchor row left the ledger")

    def test_no_shipped_row_reads_as_kill_it(self):
        """Every row here is work that completed. `blowup` / `no-saving` /
        `downscale` all mean "kill it and start over", which cannot be true of
        something already on the NAS -- if one fires, a threshold is wrong."""
        for title, raw, norm, code in self.measured():
            self.assertIn(code, ("good", "thin", "suspect"),
                          f"{title} at {raw:.1f}% raw / {norm:.1f}% norm -> {code}")

    def test_the_unmeasurable_row_is_excluded(self):
        missing = [r for r in rows() if not r.get("source_bytes")]
        self.assertEqual(len(missing), 1)
        self.assertNotIn(missing[0]["title"], self.ANCHORS)


class FixtureIntegrity(unittest.TestCase):
    """Replay the FROZEN rows, always -- even on a machine with a live ledger.

    Without this the fixture is dead weight on the only machine that runs the
    driver: LEDGER resolves to the live file there, so a fixture that rotted
    (an anchor deleted, a row corrupted, the file emptied by a bad merge) would
    stay green here and fail only in CI, on a branch, hours later. Running both
    keeps the two ledgers honest against the same thresholds.
    """

    def setUp(self):
        self.saved = core.LEDGER
        core.LEDGER = os.path.abspath(FIXTURE)
        with open(FIXTURE) as fh:
            self.rows = [json.loads(l) for l in fh if l.strip()]

    def tearDown(self):
        core.LEDGER = self.saved

    def test_fixture_is_the_calibration_corpus(self):
        """12+ measured rows, or the baseline it feeds means nothing."""
        measured = [r for r in self.rows
                    if r.get("source_bytes") and r.get("output_bytes")]
        self.assertGreaterEqual(len(measured), 12,
                                "the frozen corpus lost rows the calibration relied on")

    def test_frozen_anchors_keep_their_verdict(self):
        hist = core.history_ratios(normalised=True)
        seen = {}
        for r in self.rows:
            sb, ob = r.get("source_bytes"), r.get("output_bytes")
            if not sb or not ob or r["title"] not in LedgerRegression.ANCHORS:
                continue
            raw = ob / sb * 100.0
            norm = raw * core.crop_factor(r.get("source_geometry"),
                                          r.get("output_geometry"))
            seen[r["title"]] = core._verdict(raw, hist, norm, False)[0]
        self.assertEqual(seen, LedgerRegression.ANCHORS)

    def test_no_frozen_row_reads_as_kill_it(self):
        hist = core.history_ratios(normalised=True)
        for r in self.rows:
            sb, ob = r.get("source_bytes"), r.get("output_bytes")
            if not sb or not ob:
                continue
            raw = ob / sb * 100.0
            norm = raw * core.crop_factor(r.get("source_geometry"),
                                          r.get("output_geometry"))
            code = core._verdict(raw, hist, norm, False)[0]
            self.assertIn(code, ("good", "thin", "suspect"),
                          f"{r['title']} at {raw:.1f}% raw -> {code}")


if __name__ == "__main__":
    unittest.main()
