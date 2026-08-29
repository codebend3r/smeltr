#!/usr/bin/env python3
"""Log parsing and the two gates that decide whether an original may be deleted.

Self-contained: every test builds a synthetic HandBrake log in a temp dir and
points core.X9 at it, so nothing here depends on what is on the staging drive.

    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import core


def fake_log(out_path, src_geom=(3840, 2160), out_geom=(3840, 2076),
             autocrop=(42, 42, 0, 0), crf=16.0):
    """The handful of lines core.parse_log() actually reads."""
    t, b, l, r = autocrop
    return (
        f'[10:00:00] scan: 10 previews, {src_geom[0]}x{src_geom[1]}, 24.000 fps, '
        f'autocrop = {t}/{b}/{l}/{r}, aspect 16:9, PAR 1:1\n'
        f'  + autocrop: {t}/{b}/{l}/{r}\n'
        f'[10:00:01] Starting work at: Sat Aug 22 10:00:01 2026\n'
        f'"File": "{out_path}"\n'
        f'[10:00:01] job configuration:\n'
        f'[10:00:01]  * audio track 1\n'
        f'[10:00:01]  * subtitle track 1\n'
        f'[10:00:01]      + Rate Control / qCompress    : CRF-{crf}\n'
        f'[10:00:01]      + storage dimensions: {out_geom[0]} x {out_geom[1]}\n'
        f'[11:00:00] Finished work at: Sat Aug 22 11:00:00 2026\n'
        f'Encode done!\n'
    )


class LogLookup(unittest.TestCase):
    """The autopilot passes a full -o path; callers hold a bare listdir entry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(core, "X9", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def write(self, slug, out_path, **kw):
        path = os.path.join(self.tmp.name, f".hb-{slug}.log")
        with open(path, "w") as fh:
            fh.write(fake_log(out_path, **kw))
        return path

    def test_matches_full_path_o_against_bare_basename(self):
        # The regression that halted the driver after a five-hour Shrek encode.
        self.write("shrek2001", "/Volumes/Crucial X9/4K Movies/Shrek (2001)/"
                                "Shrek (2001) 2160p HEVC.mkv")
        got = core.log_for_output("Shrek (2001) 2160p HEVC.mkv")
        self.assertTrue(got, "full-path -o log must match a bare basename")
        self.assertEqual(got["crf"], 16.0)

    def test_matches_bare_o_too(self):
        # Logs written before the driver switched to full paths.
        self.write("old", "Old Movie (1999) 2160p HEVC.mkv")
        self.assertTrue(core.log_for_output("Old Movie (1999) 2160p HEVC.mkv"))

    def test_no_match_returns_empty(self):
        self.write("shrek2001", "/x/Shrek (2001) 2160p HEVC.mkv")
        self.assertEqual(core.log_for_output("Some Other Film 2160p HEVC.mkv"), {})

    def test_does_not_confuse_two_titles(self):
        self.write("a", "/x/A (2001)/A (2001) 2160p HEVC.mkv", crf=16.0)
        self.write("b", "/x/B (2002)/B (2002) 2160p HEVC.mkv", crf=20.0)
        self.assertEqual(core.log_for_output("B (2002) 2160p HEVC.mkv")["crf"], 20.0)

    def test_autocrop_is_captured(self):
        self.write("k", "/x/K/K 2160p HEVC.mkv", autocrop=(278, 278, 0, 2))
        self.assertEqual(core.log_for_output("K 2160p HEVC.mkv")["autocrop"],
                         (278, 278, 0, 2))


class Downscale(unittest.TestCase):
    """Four-sided auto-crop is cropping. Resampling is not."""

    def test_pure_height_crop_is_not_a_downscale(self):
        self.assertFalse(core.is_downscale("3840x2160", "3840x1600", (280, 280, 0, 0)))

    def test_side_crop_is_not_a_downscale(self):
        # Kubo: two dead columns on the right, nothing resampled.
        self.assertFalse(core.is_downscale("3840x2160", "3838x1604", (278, 278, 0, 2)))

    def test_real_downscale_still_trips(self):
        self.assertTrue(core.is_downscale("3840x2160", "1920x1080", (0, 0, 0, 0)))

    def test_width_loss_beyond_crop_still_trips(self):
        self.assertTrue(core.is_downscale("3840x2160", "3000x1604", (278, 278, 0, 2)))

    def test_missing_autocrop_stays_conservative(self):
        # Cannot tell crop from scale: refuse to call it safe.
        self.assertTrue(core.is_downscale("3840x2160", "3838x1604", None))

    def test_unknown_geometry_is_not_a_downscale(self):
        self.assertFalse(core.is_downscale(None, "3840x1604", None))
        self.assertFalse(core.is_downscale("3840x2160", None, None))

    def test_verdict_halts_on_a_real_downscale(self):
        code, _ = core._verdict(40.0, [40.0] * 5, 40.0, downscaled=True)
        self.assertEqual(code, "downscale")


if __name__ == "__main__":
    unittest.main()
