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


LEDGER = os.path.join(os.path.dirname(__file__), "..", "ledger.jsonl")


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
    def setUp(self):
        self.hist = core.history_ratios(normalised=True)
        self.base = statistics.median(self.hist)

    def test_baseline_is_what_the_ledger_actually_holds(self):
        self.assertGreaterEqual(len(self.hist), core.MIN_HISTORY)
        self.assertAlmostEqual(self.base, 35.1, places=1)

    def test_threshold(self):
        self.assertAlmostEqual(self.base * core.OUTLIER_FACTOR, 14.0, places=1)

    def test_just_below_is_suspect(self):
        self.assertEqual(core._verdict(13.9, self.hist, 13.9, False)[0], "suspect")

    def test_just_above_is_good(self):
        self.assertEqual(core._verdict(14.1, self.hist, 14.1, False)[0], "good")

    def test_flight_still_asks_for_a_human(self):
        """Deliberately retained: 12.6% normalised is under the 14.0% line.

        The smallest output this library has ever produced, against a ~92 GB
        original. The SSIM check that cleared it was worth having.
        """
        code, note = core._verdict(9.4, self.hist, 12.6, False)
        self.assertEqual(code, "suspect")
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
        """4 of the first 12 landed under 30% and every one was good."""
        for r in (30.0, 25.1, 22.8, 18.6, 14.1):
            self.assertEqual(self.v(r), "good", f"{r}% should be good")


class Precedence(unittest.TestCase):
    def test_downscale_outranks_every_size_verdict(self):
        hist = core.history_ratios(normalised=True)
        for r in (5.0, 45.0, 90.0, 130.0):
            self.assertEqual(core._verdict(r, hist, r, True)[0], "downscale")

    def test_no_projection_is_unknown(self):
        self.assertEqual(core._verdict(None, [], None, False)[0], "unknown")


class LedgerRegression(unittest.TestCase):
    """Recalibration must not silently reclassify work already shipped."""

    EXPECTED = {
        "Flight (2012)": "suspect",
        "Shrek (2001)": "good",
        "Hunt for the Wilderpeople (2016)": "good",
        "Gemini Man (2019)": "good",
        "Riddick (2013)": "good",
        "Sudden Death (1995)": "good",
        "What Dreams May Come (1998)": "good",
        "Armageddon (1998)": "good",
        "GoodFellas (1990)": "good",
        "Leaving Las Vegas (1995)": "good",
        "Steel Magnolias (1989)": "good",
        "Oldboy (Oldeuboi) (2003)": "thin",
    }

    def test_every_measured_row_keeps_its_verdict(self):
        hist = core.history_ratios(normalised=True)
        seen = set()
        for r in rows():
            sb, ob = r.get("source_bytes"), r.get("output_bytes")
            if not sb or not ob:
                continue          # Wanted (2008): no source size, never back-solved
            raw = ob / sb * 100.0
            norm = raw * core.crop_factor(r.get("source_geometry"),
                                          r.get("output_geometry"))
            code, _ = core._verdict(raw, hist, norm, False)
            self.assertEqual(code, self.EXPECTED[r["title"]],
                             f"{r['title']} at {raw:.1f}% raw / {norm:.1f}% norm")
            seen.add(r["title"])
        self.assertEqual(seen, set(self.EXPECTED))

    def test_the_unmeasurable_row_is_excluded(self):
        missing = [r for r in rows() if not r.get("source_bytes")]
        self.assertEqual(len(missing), 1)
        self.assertNotIn(missing[0]["title"], self.EXPECTED)


if __name__ == "__main__":
    unittest.main()
