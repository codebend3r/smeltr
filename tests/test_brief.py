"""The 09:00 morning brief (dashboard/brief.py).

What is pinned, and why each one:

  * the WINDOW is a string comparison on ledger-format stamps, never a Date
    round trip. `finished_at` and the log stamps are local wall clock with no
    offset; re-parsing them is how the monitor's axis once shifted +4 h.
  * an ERROR driver line reaches events.py as kind "info" (it is not in
    events._KINDS). The brief must list it under errors anyway, by its first
    word -- the whole reason the section exists is the night the driver wrote
    five of those.
  * the error headline counts TITLES, not log lines. One failure is a FAILED
    watcher line, an ERROR driver line and a marker; 14 for five titles is a
    lie in the safe-sounding direction.
  * an UNREADABLE staging drive is reported as blind, never as "no errors".
    Same rule as the watchdog and the heartbeat, for the same reason.
  * a log tail that starts after the window opens is DISCLOSED. The events
    parser reads a bounded tail; a quiet-looking morning must not be a
    truncated one.
  * a replenish PICK cut off by the detail cap is not a title.
  * it sends EMAIL ONLY, through notify.json, and never touches notify.cursor
    (the event notifier's seen-set -- writing it here swallows deletions).
  * the LaunchAgent fires at 09:00 by calendar, runs `smeltr brief`, and logs
    off the staging drive (launchd fails a job whose log path is on an
    unmounted volume).
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import brief, notify  # noqa: E402
from pipeline import core  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINCE, UNTIL = "2026-09-07 09:00:00", "2026-09-08 09:00:00"


def row(title, finished, src=60 * brief.GIB, out=30 * brief.GIB, **kw):
    r = {
        "title": title,
        "finished_at": finished,
        "source_bytes": src,
        "output_bytes": out,
        "kept": True,
        "dest": None,
        "crf": 70.0,
        "encoder": "vt_h265_10bit",
        "encode_seconds": 3600,
        "note": "verdict good; no-delete policy: kept in place",
    }
    r.update(kw)
    return r


def ev(ts, kind, text, **kw):
    e = {"ts": ts, "kind": kind, "text": text, "detail": None, "approx": False, "count": 1}
    e.update(kw)
    return e


def st(**kw):
    s = {
        "x9_online": True,
        "driver": True,
        "paused": False,
        "encoding": None,
        "free_bytes": 500 * brief.GIB,
        "total_bytes": 1800 * brief.GIB,
        "queue_folders": 10,
        "complete_folders": 3,
        "downloads_done": 4,
        "download_budget": 10,
    }
    s.update(kw)
    return s


def compose(rows=(), evs=(), markers=None, status=None, since=SINCE, until=UNTIL):
    return brief.compose(
        since,
        until,
        brief.converted(list(rows), since, until),
        brief.split_events(list(evs), since, until),
        markers,
        status or st(),
    )


class Window(unittest.TestCase):
    def test_window_is_24h_of_ledger_stamps(self):
        import time

        now = time.mktime(time.strptime(UNTIL, brief.STAMP))
        since, until = brief.window(now)
        self.assertEqual((since, until), (SINCE, UNTIL))

    def test_converted_keeps_only_the_window_oldest_first(self):
        rows = [
            row("late", "2026-09-08 09:00:01"),
            row("b", "2026-09-08 02:00:00"),
            row("early", "2026-09-07 08:59:59"),
            row("a", "2026-09-07 09:00:00"),
            {"title": "no stamp"},
        ]
        got = [r["title"] for r in brief.converted(rows, SINCE, UNTIL)]
        self.assertEqual(got, ["a", "b"])


class LedgerRows(unittest.TestCase):
    def test_quality_names_its_scale(self):
        self.assertEqual(brief.quality_label(row("x", UNTIL)), "VT CQ 70")
        self.assertEqual(
            brief.quality_label(row("x", UNTIL, crf=14.0, encoder=None)), "CRF 14"
        )
        self.assertEqual(brief.quality_label(row("x", UNTIL, crf=None)), "—")

    def test_verdict_comes_from_the_note_or_the_outcome(self):
        self.assertEqual(brief.verdict_of(row("x", UNTIL, note="verdict thin: 63%")), "thin")
        self.assertEqual(
            brief.verdict_of(row("x", UNTIL, note="", kept=False, dest="/lib/x.mkv")),
            "good",
        )
        self.assertEqual(brief.verdict_of(row("x", UNTIL, note="")), "?")

    def test_totals_count_every_row_but_sum_only_paired_sizes(self):
        rows = [row("a", UNTIL), row("b", UNTIL, src=None)]
        t = brief.totals(rows)
        self.assertEqual((t["count"], t["measured"]), (2, 1))
        self.assertEqual(t["source_bytes"], 60 * brief.GIB)
        self.assertEqual(t["saved_pct"], 50.0)
        body = compose(rows)["body"]
        self.assertIn("1 with an unknown size left out", body)
        self.assertIn("size ratio unknown", body)

    def test_a_replaced_original_is_shouted(self):
        body = compose([row("a", UNTIL, kept=False, dest="/lib/a.mkv")])["body"]
        self.assertIn("ORIGINAL REPLACED", body)
        self.assertIn("Originals replaced: 1", body)

    def test_empty_window_says_so(self):
        body = compose([])["body"]
        self.assertIn("CONVERTED (0)", body)
        self.assertIn("nothing finished in this window", body)


class Events(unittest.TestCase):
    def test_error_by_first_word_even_when_kind_is_info(self):
        evs = [
            ev("2026-09-08 06:46:00", "info", "ERROR Hereditary (2018): track mismatch"),
            ev("2026-09-08 06:45:00", "failed", "FAILED X — HandBrakeCLI died", approx=True),
            ev("2026-09-08 05:00:00", "lastrung", "FINAL X — no rung left"),
            ev("2026-09-08 04:00:00", "info", "SYNC FAILED for X - ssh"),
            ev("2026-09-08 03:00:00", "info", "waiting: paused from the dashboard"),
            ev("2026-09-08 02:00:00", "ladder", "LADDER X -> Q65"),
            ev("2026-09-08 01:00:00", "judge", "JUDGE X"),
            ev("2026-09-06 01:00:00", "info", "ERROR out of window"),
        ]
        s = brief.split_events(evs, SINCE, UNTIL)
        self.assertEqual(
            [e["text"].split()[0] for e in s["errors"]],
            ["SYNC", "FINAL", "FAILED", "ERROR"],
        )
        self.assertEqual([e["kind"] for e in s["notes"]], ["ladder"])
        self.assertEqual(s["coverage"], "2026-09-06 01:00:00")

    def test_approx_stamp_keeps_its_tilde(self):
        evs = [ev("2026-09-08 06:45:00", "failed", "FAILED X", approx=True)]
        body = compose(evs=evs, markers=[])["body"]
        self.assertIn("~Tue 8 Sep 06:45  FAILED X", body)

    def test_collapsed_count_is_shown(self):
        evs = [ev("2026-09-08 06:45:00", "ladder", "LADDER X -> Q65", count=3)]
        self.assertIn("(× 3)", compose(evs=evs, markers=[])["body"])

    def test_log_tail_short_of_the_window_is_disclosed(self):
        evs = [ev("2026-09-07 15:00:00", "ladder", "LADDER X -> Q65")]
        body = compose(evs=evs, markers=[])["body"]
        self.assertIn("starts at Mon 7 Sep 15:00", body)
        self.assertIn("NOT covered", body)
        full = compose(
            evs=evs + [ev("2026-09-07 08:00:00", "info", "older")], markers=[]
        )["body"]
        self.assertNotIn("NOT covered", full)

    def test_no_events_at_all_is_disclosed(self):
        self.assertIn("no driver/watcher events could be read", compose(markers=[])["body"])

    def test_replenish_picks_dedupe_and_drop_truncated_names(self):
        detail = (
            "PICK 79.7 Mb/s  Addams Family Values (1993) · SKIPPING Addams Family "
            "Values (1993) - only 0 GiB free · PICK 79.1 Mb/s  Space Jam (1996) · "
            "PICK 70.0 Mb/s  Kick-Ass (201"
        )
        evs = [
            ev("2026-09-08 06:00:00", "replenish", "REPLENISH (independent)", detail=detail),
            ev("2026-09-08 05:00:00", "replenish", "REPLENISH (independent)", detail=detail),
        ]
        picks = brief.replenish_picks(brief.split_events(evs, SINCE, UNTIL)["notes"])
        self.assertEqual(picks, ["Addams Family Values (1993)", "Space Jam (1996)"])
        self.assertIn("Replenisher picked 2:", compose(evs=evs, markers=[])["body"])


class ErrorMarkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = core.X9
        core.X9 = self.tmp

    def tearDown(self):
        core.X9 = self.saved

    def _mark(self, title, note, when):
        p = os.path.join(self.tmp, ".error-" + title)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(note + "\n")
        import time

        t = time.mktime(time.strptime(when, brief.STAMP))
        os.utime(p, (t, t))

    def test_lists_markers_with_new_flag_and_skips_appledouble(self):
        self._mark("Old (1989)", "needs a human", "2026-09-04 09:20:00")
        self._mark("New (2018)", "CRF ladder exhausted", "2026-09-08 06:46:00")
        with open(os.path.join(self.tmp, "._.error-Ghost (2000)"), "wb") as fh:
            fh.write(b"\x00")
        got = brief.error_markers(SINCE)
        self.assertEqual([m["title"] for m in got], ["Old (1989)", "New (2018)"])
        self.assertEqual([m["new"] for m in got], [False, True])
        self.assertEqual(got[1]["note"], "CRF ladder exhausted")

    def test_headline_counts_titles_not_log_lines(self):
        self._mark("New (2018)", "track mismatch", "2026-09-08 06:46:00")
        evs = [
            ev("2026-09-08 06:46:00", "info", "ERROR New (2018): track mismatch"),
            ev("2026-09-08 06:45:00", "failed", "FAILED New (2018)"),
        ]
        m = compose(evs=evs, markers=brief.error_markers(SINCE))
        self.assertIn("ERRORS (1 in the error state, 1 new)", m["body"])
        self.assertIn("1 in error (1 new)", m["subject"])
        self.assertIn("NEW  Tue 8 Sep 06:46  New (2018) — track mismatch", m["body"])

    def test_unreadable_drive_is_blind_not_clear(self):
        core.X9 = os.path.join(self.tmp, "gone")
        self.assertIsNone(brief.error_markers(SINCE))
        m = compose(markers=None, status=st(x9_online=False))
        self.assertIn("ERRORS (staging drive unreadable)", m["body"])
        self.assertIn("BLIND, not clear", m["body"])
        self.assertIn("errors unreadable", m["subject"])
        self.assertIn("NOT READABLE", m["body"])
        self.assertNotIn("none\n", m["body"].split("ERRORS")[1].split("OF NOTE")[0])

    def test_clean_state_says_none(self):
        self.assertIn("ERRORS (0 in the error state)\n  none", compose(markers=[])["body"])


class RightNow(unittest.TestCase):
    def test_full_drive_is_flagged_in_body_and_subject(self):
        m = compose(markers=[], status=st(free_bytes=0))
        self.assertIn("⚠ FULL", m["body"])
        self.assertIn("X9 FULL", m["subject"])
        ok = compose(markers=[], status=st())
        self.assertNotIn("FULL", ok["subject"])

    def test_under_the_encode_floor_is_flagged(self):
        # The 100 GiB floor (2026-09-08): well above the 1% FULL flag, and a
        # different fact -- the driver is WAITING for the human. Name it.
        m = compose(markers=[], status=st(free_bytes=87 * 1024**3, total_bytes=4000 * 1024**3))
        self.assertIn("LOW SPACE", m["body"])
        self.assertIn("100 GiB", m["body"])
        self.assertIn("low space", m["subject"])
        ok = compose(markers=[], status=st(free_bytes=900 * 1024**3, total_bytes=4000 * 1024**3))
        self.assertNotIn("LOW SPACE", ok["body"])

    def test_paused_and_dead_driver_are_named(self):
        m = compose(markers=[], status=st(paused=True, driver=False))
        self.assertIn("Driver: NOT RUNNING · PAUSED from the dashboard", m["body"])
        self.assertIn("paused", m["subject"])

    def test_encoding_line(self):
        m = compose(markers=[], status=st(encoding="X on vt at CQ 70, 12.0%"))
        self.assertIn("Encoding: X on vt at CQ 70, 12.0%", m["body"])
        self.assertIn("Encoding: nothing", compose(markers=[])["body"])

    def test_downloads_with_and_without_a_budget(self):
        self.assertIn("Downloads: 4 of 10 budget", compose(markers=[])["body"])
        self.assertIn(
            "Downloads: 4 (no budget set)",
            compose(markers=[], status=st(download_budget=None))["body"],
        )

    def test_status_reads_live_without_raising(self):
        saved = core.X9, brief.heartbeat._driver_running
        core.X9 = tempfile.mkdtemp()
        brief.heartbeat._driver_running = lambda: False
        try:
            s = brief.status()
        finally:
            core.X9, brief.heartbeat._driver_running = saved
        self.assertTrue(s["x9_online"])
        self.assertEqual(s["queue_folders"], 0)
        self.assertIsNotNone(s["free_bytes"])


class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = core.SMELTR_DIR, notify.send_email
        core.SMELTR_DIR = self.tmp
        self.sent = []
        notify.send_email = lambda cfg, msg: self.sent.append((cfg, msg))

    def tearDown(self):
        core.SMELTR_DIR, notify.send_email = self.saved

    def _cfg(self, obj):
        p = os.path.join(self.tmp, notify.CONFIG_NAME)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.chmod(p, 0o600)

    def test_no_email_channel_prints_the_body_and_fails(self):
        out = io.StringIO()
        rc = brief.send({"subject": "s", "body": "BODY\n"}, err=out.write)
        self.assertEqual(rc, 1)
        self.assertIn("BODY", out.getvalue())
        self.assertEqual(self.sent, [])

    def test_email_only_through_notify_json(self):
        self._cfg(
            {
                "slack": {"webhook": "https://hooks.slack.com/x"},
                "email": {
                    "host": "smtp", "port": 587, "user": "u", "password": "p",
                    "from": "a@b", "to": "a@b",
                },
            }
        )
        called = []
        saved = notify.send_slack
        notify.send_slack = lambda *a: called.append(a)
        try:
            rc = brief.send({"subject": "s", "body": "b"}, err=lambda s: None)
        finally:
            notify.send_slack = saved
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], {"subject": "s", "body": "b"})
        self.assertEqual(called, [], "the brief is email only")
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, notify.CURSOR_NAME)),
            "the brief must never write the notifier's seen-set",
        )

    def test_a_failed_send_exits_nonzero(self):
        self._cfg({"email": {"host": "h", "port": 1, "user": "u", "password": "p",
                             "from": "a@b", "to": "a@b"}})

        def boom(cfg, msg):
            raise OSError("smtp down")

        notify.send_email = boom
        out = io.StringIO()
        self.assertEqual(brief.send({"subject": "s", "body": "b"}, err=out.write), 1)
        self.assertIn("smtp down", out.getvalue())

    def test_dry_run_prints_and_sends_nothing(self):
        saved = brief.build
        brief.build = lambda hours=24: {"subject": "SUBJ", "body": "BODY\n"}
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            rc = brief.main(["--dry-run"])
        finally:
            sys.stdout = real
            brief.build = saved
        self.assertEqual(rc, 0)
        self.assertIn("SUBJ", out.getvalue())
        self.assertIn("BODY", out.getvalue())
        self.assertEqual(self.sent, [])


class Wiring(unittest.TestCase):
    def test_launcher_and_bun_script(self):
        with open(os.path.join(ROOT, "smeltr"), encoding="utf-8") as fh:
            self.assertIn("brief) shift; exec", fh.read())
        with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as fh:
            scripts = json.load(fh)["scripts"]
        self.assertEqual(scripts.get("brief"), "./smeltr brief")
        self.assertEqual(scripts.get("brief:check"), "./smeltr brief --dry-run")

    def test_launchagent_fires_at_nine_and_logs_off_the_drive(self):
        with open(os.path.join(ROOT, "ops", "com.smeltr.brief.plist"), "rb") as fh:
            p = plistlib.load(fh)
        self.assertEqual(p["Label"], "com.smeltr.brief")
        self.assertEqual(p["ProgramArguments"][-1], "brief")
        self.assertTrue(p["ProgramArguments"][0].endswith("/smeltr"))
        self.assertEqual(p["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertFalse(p.get("RunAtLoad", False), "loading it must not send a brief")
        for k in ("StandardOutPath", "StandardErrorPath"):
            self.assertNotIn("/Volumes/", p[k])

    def test_layering_lists_it_dashboard_only(self):
        from tests import test_layering

        self.assertIn("brief", test_layering.DASHBOARD_ONLY)


if __name__ == "__main__":
    unittest.main()
