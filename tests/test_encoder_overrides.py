"""Per-title encoder overrides: the file, the fallback, and the log parse.

The hybrid encoder path exists because the M1 Max media engine encodes 4K
10-bit HEVC ~20x faster than x265 but with materially worse quality-per-bit
(VMAF ceiling ~84 on grain at ANY size, measured 2026-08-25). These tests pin
the safety property that makes it deployable at all: EVERY failure -- missing
file, corrupt file, unknown encoder, off-menu quality -- answers the proven
x265 default. A quality number applied on the wrong encoder's scale is the
one outcome this file must make impossible: 16 is near-lossless as a CRF and
garbage as a CQ.
"""
import json
import os
import tempfile
import unittest

from pipeline import core


class OverrideFile(unittest.TestCase):
    def setUp(self):
        self._orig = core.ENCODER_OVERRIDES
        self._tmp = tempfile.TemporaryDirectory()
        core.ENCODER_OVERRIDES = os.path.join(
            self._tmp.name, "encoder_overrides.json")

    def tearDown(self):
        core.ENCODER_OVERRIDES = self._orig
        self._tmp.cleanup()

    def _write(self, obj):
        with open(core.ENCODER_OVERRIDES, "w", encoding="utf-8") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)

    def test_missing_file_answers_the_default(self):
        self.assertEqual(core.encoder_for("Warfare (2025)"),
                         (core.DEFAULT_ENCODER, core.DEFAULT_QUALITY))
        self.assertFalse(core.load_encoder_overrides()["corrupt"])

    def test_roundtrip_and_case_insensitive_match(self):
        core.save_encoder_overrides(
            {"Warfare (2025)": {"encoder": "vt_h265_10bit", "quality": 60}})
        self.assertEqual(core.encoder_for("warfare (2025)"),
                         ("vt_h265_10bit", 60))
        # Other titles keep the default.
        self.assertEqual(core.encoder_for("Timecop (1994)"),
                         (core.DEFAULT_ENCODER, core.DEFAULT_QUALITY))

    def test_unknown_encoder_is_dropped(self):
        self._write({"Warfare (2025)": {"encoder": "nvenc", "quality": 60}})
        self.assertEqual(core.encoder_for("Warfare (2025)"),
                         (core.DEFAULT_ENCODER, core.DEFAULT_QUALITY))
        self.assertFalse(core.load_encoder_overrides()["corrupt"])

    def test_off_menu_quality_is_dropped(self):
        # CRF 16 is on the x265 menu but NOT the VT menu: applying it there
        # would mean near-garbage output, so the entry must fall away whole.
        self._write({"Warfare (2025)":
                     {"encoder": "vt_h265_10bit", "quality": 16}})
        self.assertEqual(core.encoder_for("Warfare (2025)"),
                         (core.DEFAULT_ENCODER, core.DEFAULT_QUALITY))

    def test_corrupt_file_flags_and_defaults(self):
        self._write("{not json")
        ov = core.load_encoder_overrides()
        self.assertTrue(ov["corrupt"])
        self.assertEqual(ov["map"], {})
        self.assertEqual(core.encoder_for("Warfare (2025)"),
                         (core.DEFAULT_ENCODER, core.DEFAULT_QUALITY))

    def test_non_dict_top_level_is_corrupt(self):
        self._write(["Warfare (2025)"])
        self.assertTrue(core.load_encoder_overrides()["corrupt"])

    def test_float_quality_is_dropped(self):
        # 16.0 == 16 passes a bare membership test; the invariant is ints only.
        self._write({"Warfare (2025)":
                     {"encoder": "x265_10bit", "quality": 16.0}})
        self.assertEqual(core.encoder_for("Warfare (2025)"),
                         (core.DEFAULT_ENCODER, core.DEFAULT_QUALITY))

    def test_menu_sanity(self):
        # The default must be a real menu entry, and every menu non-empty --
        # the dashboard renders these and the driver trusts them.
        self.assertIn(core.DEFAULT_ENCODER, core.ENCODER_CHOICES)
        self.assertIn(core.DEFAULT_QUALITY,
                      core.ENCODER_CHOICES[core.DEFAULT_ENCODER])
        for enc, menu in core.ENCODER_CHOICES.items():
            self.assertTrue(menu, f"{enc} has an empty quality menu")
        # Every encoder has an explicit default ON its own menu. Deriving a
        # default from menu position is forbidden: index [0] is the BEST
        # x265 CRF and the WORST VideoToolbox CQ.
        for enc, menu in core.ENCODER_CHOICES.items():
            self.assertIn(enc, core.DEFAULT_QUALITIES)
            self.assertIn(core.DEFAULT_QUALITIES[enc], menu)


class PerEncoderVerdict(unittest.TestCase):
    """The verdict that authorises a deletion must never judge one encoder's
    output against another encoder's history."""

    def test_vt_with_no_vt_baseline_is_judged_on_band_and_floor(self):
        # The "first encodes on a new encoder are suspect" gate is GONE
        # (operator's call, 2026-09-06): it held a deletion for a human, and
        # nothing is deleted any more. With no same-encoder history the
        # relative check has no base, so the band and the 15% floor decide.
        code, _ = core._verdict(40.0, [], 40.0, False, encoder="vt_h265_10bit")
        self.assertEqual(code, "good")
        code, _ = core._verdict(5.0, [], 12.0, False, encoder="vt_h265_10bit")
        self.assertEqual(code, "suspect")  # the floor still applies

    def test_vt_with_a_vt_baseline_can_pass(self):
        hist = [35.0, 40.0, 45.0]  # MIN_HISTORY same-encoder rows
        code, _ = core._verdict(40.0, hist, 40.0, False,
                                encoder="vt_h265_10bit")
        self.assertEqual(code, "good")

    def test_absolute_checks_still_fire_for_vt(self):
        # blowup/no-saving are size-absolute; no baseline needed.
        self.assertEqual(core._verdict(101.0, [], 101.0, False,
                                       encoder="vt_h265_10bit")[0], "blowup")
        self.assertEqual(core._verdict(85.0, [], 85.0, False,
                                       encoder="vt_h265_10bit")[0], "no-saving")

    def test_history_filter_treats_none_as_x265(self):
        rows = [
            {"source_bytes": 100, "output_bytes": 30},                # pre-2026-08-25 row
            {"source_bytes": 100, "output_bytes": 50,
             "encoder": "x265_10bit"},
            {"source_bytes": 100, "output_bytes": 70,
             "encoder": "vt_h265_10bit"},
        ]
        self.assertEqual(core.history_ratios(hist=rows,
                                             encoder="x265_10bit"),
                         [30.0, 50.0])
        self.assertEqual(core.history_ratios(hist=rows,
                                             encoder="vt_h265_10bit"),
                         [70.0])
        self.assertEqual(len(core.history_ratios(hist=rows)), 3)


class LogParse(unittest.TestCase):
    """VideoToolbox logs carry no 'Rate Control / qCompress' line; quality
    and encoder come from the job header instead. Both shapes must parse --
    the ledger's crf column and the live card's chip read them."""

    def _log(self, text):
        path = os.path.join(self._tmp.name, "hb.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_vt_log_head_parses_cq_and_encoder(self):
        info = core.parse_log(self._log(
            '        "Encoder": "vt_h265_10bit",\n'
            '        "Quality": 60.0,\n'
            "[16:00:23]    + encoder: H.265 10-bit (VideoToolbox)\n"
            "[16:00:23]      + quality: 60.00 (CQ)\n"
        ))
        self.assertEqual(info["crf"], 60.0)
        self.assertEqual(info["encoder"], "vt_h265_10bit")

    def test_x265_log_head_still_parses_crf(self):
        info = core.parse_log(self._log(
            '        "Encoder": "x265_10bit",\n'
            "[10:00:00] Rate Control / qCompress    : CRF-16.0 / 0.60\n"
        ))
        self.assertEqual(info["crf"], 16.0)
        self.assertEqual(info["encoder"], "x265_10bit")


class PsParse(unittest.TestCase):
    """The live-card process scan must key on the EXECUTABLE, not on the
    string "HandBrakeCLI" anywhere in a command line: a shell whose script
    text quotes a HandBrakeCLI invocation rendered a ghost live card with
    unexpanded $variables as its filename (seen live 2026-08-26)."""

    REAL = ('  123 HandBrakeCLI -i /x9/A (2001)/A (2001) Remux.mkv '
            '-o /x9/A (2001)/A (2001) 2160p HEVC.mkv -f av_mkv '
            '-e vt_h265_10bit -q 60 --all-audio')
    PATHED = ('  124 /opt/homebrew/bin/HandBrakeCLI -i /x9/b.mkv '
              '-o /x9/b out.mkv -e x265_10bit -q 16')
    GHOST = ('  125 /bin/bash -c nice -n 20 HandBrakeCLI -i "$S/ref-$title.mkv" '
             '-o "$S/vt-$title-cq$cq.mkv" -e vt_h265_10bit -q "$cq"')

    def test_real_process_parses(self):
        procs = core._parse_ps(self.REAL + "\n" + self.PATHED)
        self.assertEqual([p["pid"] for p in procs], [123, 124])
        self.assertEqual(procs[0]["encoder"], "vt_h265_10bit")
        self.assertEqual(procs[0]["crf"], 60.0)

    def test_shell_quoting_a_handbrake_command_is_ignored(self):
        self.assertEqual(core._parse_ps(self.GHOST), [])


if __name__ == "__main__":
    unittest.main()
