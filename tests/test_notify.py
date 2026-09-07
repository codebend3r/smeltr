"""The notifier (dashboard/notify.py) — email + Slack on the events that
matter, delivered exactly once, never from inside the decision path.

It rides `dashboard/events.py`, the same parser the Events tab reads, so a
notification can never disagree with the tab. What it must get right:

- the FIRST snapshot is a baseline, never a backlog: a fresh cursor beside
  the ledger must not replay 250 log lines into a Slack channel;
- an empty snapshot (X9 unmounted) is not a baseline either — initialising
  from it would replay the whole tail when the drive comes back;
- the seen-set is a UNION, never a replacement: events.py degrades per
  source, so one unreadable watch log leaves a non-empty snapshot, and a
  replaced seen-set would re-send every deletion notice a tick later;
- a watcher line's timestamp FLIPS to None when the log is re-used (the
  KILLED line stops being the final line), and that must not re-send it;
- a failed send is retried with backoff, a half-sent event (Slack ok,
  email down) does not resend the half that landed — even across a restart
  — and pending work survives a restart;
- the deletion notice is built from the LEDGER row, never the folded shell
  output: syncs are backgrounded and unstamped, so another title's size
  guard can land under this title's SYNC line.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import notify
from pipeline import core

DRIVER_LOG = """\
2026-09-01 15:24:41  === autopilot up (dry-run=false) ===
2026-09-01 15:24:46  START Addams Family 2, The (2021) at CRF 14
2026-09-01 15:24:46    HandBrake pid 75467
2026-09-01 15:30:29  JUDGE Species (1995)
2026-09-01 15:30:30  RECORD Species (1995)
2026-09-01 15:30:31  SYNC Species (1995)
size guard PASS: 40.83 GB replaces 65.94 GB (frees 25.11 GB)
2026-09-01 15:35:00  CYCLE COMPLETE Species (1995)
2026-09-01 15:40:00  ERROR Oldboy (2003): needs a human: suspect - 9.1% of source - marked for review, moving on
2026-09-01 15:40:00    Nothing was deleted. Delete /x9/.error-Oldboy (2003) after review.
2026-09-01 15:41:00  LADDER Movie (2000) -> CRF 12
"""

WATCH_LOG = """\
QUARTER|Movie (2000)|25%|current 1 GB|projected 5 GB|original 50 GB|10%|OK|ETA 1h
COMPLETE|Movie (2000)|2026-08-31 19:33:54|5.46 GB from 56.90 GB (90% smaller)
KILLED|Movie (2000)|projected 11.3% of original at 5.22% (CRF 14)|band 30-80|partial deleted|next: CRF 12
"""

GIB = 1073741824


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _write_secret(path, text):
    _write(path, text)
    os.chmod(path, 0o600)


class _Transport:
    """A fake channel: records payloads, fails on demand, counts attempts."""

    def __init__(self):
        self.sent = []
        self.fail = False
        self.calls = 0

    def __call__(self, cfg, message):
        self.calls += 1
        if self.fail:
            raise OSError("channel down")
        self.sent.append(message)


class _Base(unittest.TestCase):
    def setUp(self):
        self.x9 = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.driver_log = os.path.join(self.x9.name, ".autopilot.log")
        self.watch_log = os.path.join(self.x9.name, ".watch-movie2000.log")
        self.ledger = os.path.join(self.home.name, "ledger.jsonl")
        self.patches = [
            mock.patch.object(core, "X9", self.x9.name),
            mock.patch.object(core, "LEDGER", self.ledger),
        ]
        for p in self.patches:
            p.start()
        self.slack = _Transport()
        self.email = _Transport()
        self.config = {
            "slack": {"webhook": "https://hooks.slack.com/x"},
            "email": {
                "host": "smtp.gmail.com",
                "port": 587,
                "user": "u@gmail.com",
                "password": "p",
                "to": "u@gmail.com",
            },
        }
        self.errs = []
        self.clock = [1_000_000.0]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.x9.cleanup()
        self.home.cleanup()

    def notifier(self, config=None, slack=None, email=None):
        return notify.Notifier(
            self.home.name,
            config=config or self.config,
            slack=slack or self.slack,
            email=email or self.email,
            now=lambda: self.clock[0],
            err=self.errs.append,
        )

    def record(self, title, source_bytes, output_bytes):
        with open(self.ledger, "a") as f:
            f.write(
                json.dumps(
                    {
                        "title": title,
                        "source_bytes": source_bytes,
                        "output_bytes": output_bytes,
                        "finished_at": "2026-09-01 15:00:00",
                    }
                )
                + "\n"
            )

    def cursor(self):
        with open(os.path.join(self.home.name, "notify.cursor")) as fh:
            return json.load(fh)


class Classify(unittest.TestCase):
    """Event dict -> notification, or None for the noise."""

    def _ev(self, kind, text, src="driver", ts="2026-09-01 15:00:00", approx=False):
        return {
            "kind": kind,
            "text": text,
            "src": src,
            "ts": ts,
            "detail": None,
            "approx": approx,
            "count": 1,
        }

    def test_encode_done_is_not_a_verdict(self):
        """COMPLETE is HandBrake exiting. The judge runs seconds later, so the
        message must not read as success — Little Mermaid's only message was a
        green tick, and its verdict was halt-decoder-errors."""
        n = notify.classify(
            self._ev(
                "complete",
                "COMPLETE Kubo (2016) — 5.46 GiB from 56.90 GiB (90% smaller)",
                src="watcher",
            )
        )
        self.assertEqual(n["what"], "finished")
        self.assertEqual(n["title"], "Kubo (2016)")
        self.assertIn("5.46 GiB from 56.90 GiB", n["text"])
        self.assertIn("verdict pending", n["text"])
        self.assertNotIn("Finished", notify.WHATS["finished"][1])

    def test_handbrake_died_is_failed(self):
        n = notify.classify(
            self._ev(
                "failed",
                "FAILED Kubo (2016) — HandBrakeCLI died at 42% (reboot/kill?) — delete partial and restart",
                src="watcher",
                ts=None,
            )
        )
        self.assertEqual(n["what"], "failed")
        self.assertEqual(n["title"], "Kubo (2016)")
        self.assertIn("died at 42%", n["text"])

    def test_ladder_comes_from_the_drivers_stamped_line(self):
        """The driver truncates the watch log within 30 s of a KILLED line;
        its own LADDER line is stamped and permanent. Direction is the rung's
        side of the pivot: every rung above CRF_DEFAULT is an up-rung."""
        n = notify.classify(self._ev("ladder", "LADDER Movie (2000) -> CRF 12"))
        self.assertEqual(n["what"], "ladder-down")
        self.assertEqual(n["title"], "Movie (2000)")
        self.assertIn("CRF 14 → 12", n["text"])
        self.assertIn("BELOW", n["text"])
        self.assertIn("partial deleted", n["text"])
        n = notify.classify(self._ev("ladder", "LADDER Movie (2000) -> CRF 16"))
        self.assertEqual(n["what"], "ladder-up")
        self.assertIn("CRF 14 → 16", n["text"])
        self.assertIn("ABOVE", n["text"])
        n = notify.classify(self._ev("ladder", "LADDER Movie (2000) -> CRF 20"))
        self.assertIn("CRF 18 → 20", n["text"])

    def test_a_watcher_kill_is_silent_on_its_own(self):
        n = notify.classify(
            self._ev(
                "killed",
                "KILLED Movie (2000) — projected 11.3% of original at 5.22% (CRF 14) — band 30-80 — partial deleted — next: CRF 12",
                src="watcher",
            )
        )
        self.assertIsNone(n)

    def test_exhaustion_is_left_to_the_drivers_error_line(self):
        n = notify.classify(
            self._ev(
                "exhausted",
                "KILLED Movie (2000) — projected 9% … — next: CRF none-too-small",
                src="watcher",
            )
        )
        self.assertIsNone(n)

    def test_exhausted_error_names_the_direction(self):
        n = notify.classify(
            self._ev(
                "info",
                "ERROR Kubo (2016): CRF ladder exhausted (none-too-small) - marked for review, moving on",
            )
        )
        self.assertEqual(n["what"], "error")
        self.assertIn("going DOWN", n["text"])
        self.assertIn("30%", n["text"])
        n = notify.classify(
            self._ev(
                "info",
                "ERROR Kubo (2016): CRF ladder exhausted (none-too-big) - marked for review, moving on",
            )
        )
        self.assertIn("going UP", n["text"])
        self.assertIn("80%", n["text"])

    def test_error_state(self):
        n = notify.classify(
            self._ev(
                "info",
                "ERROR Oldboy (2003): needs a human: suspect - 9.1% of source - marked for review, moving on",
            )
        )
        self.assertEqual(n["what"], "error")
        self.assertEqual(n["title"], "Oldboy (2003)")
        self.assertIn("needs a human: suspect", n["text"])
        self.assertIn("nothing deleted", n["text"].lower())

    def test_a_kept_done_line_is_notified_with_its_verdict(self):
        """A finished encode is DONE whatever the verdict (2026-09-07). The
        DONE line replaced the ERROR line a thin/suspect verdict used to log,
        so it must carry the verdict word or the operator loses the only
        message that result ever produced."""
        n = notify.classify(
            self._ev(
                "done",
                "DONE Island, The (2005): verdict thin - Real but thin saving - "
                "worth a human call.; recorded; nothing synced, nothing deleted - moving on",
            )
        )
        self.assertEqual(n["what"], "kept")
        self.assertEqual(n["title"], "Island, The (2005)")
        self.assertIn("verdict thin", n["text"])
        self.assertIn("complete/", n["text"])
        self.assertNotIn("moving on", n["text"])
        self.assertIn("kept", notify.WHATS)
        self.assertIn("kept", notify.PRIORITY)

    def test_an_old_halt_is_an_error_too(self):
        """The driver no longer halts, but the parser still yields the kind
        from old logs; a HALTED line must never fall through to silence."""
        n = notify.classify(
            self._ev(
                "halted", "HALTED: Species (1995) needs a human: halt-decoder-errors"
            )
        )
        self.assertEqual(n["what"], "error")
        self.assertEqual(n["title"], "Species (1995)")

    def test_original_deleted(self):
        n = notify.classify(self._ev("cycle", "CYCLE COMPLETE Species (1995)"))
        self.assertEqual(n["what"], "deleted")
        self.assertEqual(n["title"], "Species (1995)")

    def test_a_failed_or_aborted_sync_is_not_silent(self):
        """Every failure of the deletion path used to be silent while every
        success was loud: a 30 GiB output stranded on the X9 said nothing."""
        n = notify.classify(
            self._ev(
                "sync",
                "SYNC FAILED for Kubo (2016) - nothing deleted, folder left for review",
            )
        )
        self.assertEqual(n["what"], "sync-failed")
        self.assertEqual(n["title"], "Kubo (2016)")
        self.assertIn("nothing deleted", n["text"])
        n = notify.classify(
            self._ev(
                "sync", "SYNC ABORTED: record failed for Kubo (2016) - nothing deleted"
            )
        )
        self.assertEqual(n["what"], "sync-failed")
        self.assertEqual(n["title"], "Kubo (2016)")
        self.assertIsNone(notify.classify(self._ev("sync", "SYNC Kubo (2016)")))

    def test_the_stop_condition_is_an_event(self):
        n = notify.classify(
            self._ev(
                "info",
                "STOP CONDITION: nothing staged above 40 Mb/s. Encoding paused; staging continues.",
            )
        )
        self.assertEqual(n["what"], "stopped")
        self.assertIn("nothing staged above 40 Mb/s", n["text"])

    def test_approx_stamp_is_carried(self):
        n = notify.classify(
            self._ev(
                "failed",
                "FAILED Kubo (2016) — HandBrakeCLI died at 42%",
                src="watcher",
                ts="2026-09-01 15:00:00",
                approx=True,
            )
        )
        self.assertTrue(n["approx"])

    def test_noise_is_none(self):
        for kind, text in (
            ("start", "START X at CRF 14"),
            ("judge", "JUDGE X"),
            ("record", "RECORD X"),
            ("sync", "SYNC X"),
            ("defer", "DEFER X: library roots unreachable"),
            ("info", "waiting: paused from the dashboard"),
            ("up", "=== autopilot up ==="),
        ):
            self.assertIsNone(notify.classify(self._ev(kind, text)), text)

    def test_an_unparseable_ladder_line_is_loud(self):
        errs = []
        self.assertIsNone(
            notify.classify(
                self._ev("ladder", "LADDER Movie (2000) -> rung?"), err=errs.append
            )
        )
        self.assertTrue(errs)


class Baseline(_Base):
    def test_first_snapshot_sends_nothing(self):
        _write(self.driver_log, DRIVER_LOG)
        _write(self.watch_log, WATCH_LOG)
        n = self.notifier()
        self.assertEqual(n.tick(), 0)
        self.assertEqual(self.slack.sent, [])
        self.assertEqual(self.email.sent, [])
        self.assertTrue(os.path.exists(os.path.join(self.home.name, "notify.cursor")))

    def test_an_empty_snapshot_is_not_a_baseline(self):
        """X9 unmounted at first tick: no cursor is written, so the tail is
        not replayed as 'new' when the drive mounts."""
        n = self.notifier()
        self.assertEqual(n.tick(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.home.name, "notify.cursor")))
        _write(self.driver_log, DRIVER_LOG)
        _write(self.watch_log, WATCH_LOG)
        self.assertEqual(n.tick(), 0)
        self.assertEqual(self.slack.sent, [])

    def test_an_empty_snapshot_after_a_baseline_is_ignored(self):
        _write(self.driver_log, DRIVER_LOG)
        n = self.notifier()
        n.tick()
        os.remove(self.driver_log)  # the drive went away
        n.tick()
        _write(self.driver_log, DRIVER_LOG)  # and came back, unchanged
        self.assertEqual(n.tick(), 0)

    def test_a_null_cursor_is_a_fresh_baseline(self):
        _write(self.driver_log, DRIVER_LOG)
        _write(os.path.join(self.home.name, "notify.cursor"), "null")
        n = self.notifier()
        self.assertEqual(n.tick(), 0)
        self.assertEqual(self.slack.sent, [])

    def test_an_empty_seen_list_is_no_baseline(self):
        """`{"seen": []}` must re-baseline, not replay the whole tail."""
        _write(self.driver_log, DRIVER_LOG)
        _write(
            os.path.join(self.home.name, "notify.cursor"),
            json.dumps({"seen": [], "pending": []}),
        )
        n = self.notifier()
        self.assertEqual(n.tick(), 0)
        self.assertEqual(self.slack.sent, [])

    def test_a_malformed_pending_item_is_dropped_not_wedging(self):
        """One bad persisted item used to raise out of _deliver every tick
        AFTER the seen-set had advanced — nothing delivered again, ever."""
        _write(self.driver_log, DRIVER_LOG)
        _write(
            os.path.join(self.home.name, "notify.cursor"),
            json.dumps(
                {
                    "seen": [],
                    "pending": [
                        {"note": {"what": "deleted", "title": "T", "ts": 123456}},
                        "junk",
                        {"note": None, "sent": [], "since": 0},
                    ],
                }
            ),
        )
        n = self.notifier()
        self.assertEqual(n.pending, [])
        self.assertTrue(any("pending" in e for e in self.errs))
        self.assertEqual(n.tick(), 0)  # baseline
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:00:00  CYCLE COMPLETE Kubo (2016)\n")
        self.assertEqual(n.tick(), 1)


class NewEvents(_Base):
    def setUp(self):
        super().setUp()
        _write(self.driver_log, DRIVER_LOG)
        _write(self.watch_log, WATCH_LOG)
        self.n = self.notifier()
        self.n.tick()

    def test_a_new_line_is_sent_to_both_channels_once(self):
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:00:00  CYCLE COMPLETE Kubo (2016)\n")
        self.assertEqual(self.n.tick(), 1)
        self.assertEqual(self.n.tick(), 0)
        self.assertEqual(len(self.slack.sent), 1)
        self.assertEqual(len(self.email.sent), 1)
        self.assertIn("Kubo (2016)", self.slack.sent[0]["text"])
        self.assertIn("Kubo (2016)", self.email.sent[0]["subject"])

    def test_deleted_is_built_from_the_ledger_row_not_the_log_blob(self):
        """RECORD writes the ledger row BEFORE the sync, so the row is the
        authoritative size of what was deleted. The folded shell output is
        not: it carried an ssh user@ip, once an unrelated 'autopilot already
        running' line, and — because syncs are backgrounded and their output
        is unstamped — another title's size guard. Here Species's guard line
        lands under Kubo's SYNC and must be ignored."""
        self.record("Kubo (2016)", 60 * GIB, 20 * GIB)
        with open(self.driver_log, "a") as f:
            f.write(
                "2026-09-01 16:00:00  SYNC Kubo (2016)\n"
                "size guard PASS: 40.83 GB replaces 65.94 GB (frees 25.11 GB)\n"
                "ssh push: Kubo (2016) 2160p HEVC.mkv (20.00 GB) -> crivas@192.168.50.3:/volume1/x\n"
                "2026-09-01 16:05:00  CYCLE COMPLETE Kubo (2016)\n"
                "autopilot already running (lock: /x9/.autopilot.lock)\n"
            )
        self.n.tick()
        text = self.slack.sent[-1]["text"]
        self.assertIn("60.00 GiB", text)
        self.assertIn("DELETED", text)
        self.assertIn("20.00 GiB", text)
        self.assertIn("frees 40.00 GiB", text)
        self.assertNotIn("65.94", text)
        self.assertNotIn(" GB", text)
        self.assertNotIn("192.168", text)
        self.assertNotIn("autopilot already running", text)
        self.assertIn("60.00 GiB", self.email.sent[-1]["subject"])

    def test_deleted_uses_the_newest_ledger_row_for_the_title(self):
        self.record("Kubo (2016)", 90 * GIB, 30 * GIB)
        self.record("Kubo (2016)", 60 * GIB, 20 * GIB)
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:05:00  CYCLE COMPLETE Kubo (2016)\n")
        self.n.tick()
        self.assertIn("60.00 GiB", self.slack.sent[-1]["text"])

    def test_deleted_without_a_ledger_row_says_so(self):
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:05:00  CYCLE COMPLETE Kubo (2016)\n")
        self.n.tick()
        self.assertIn("not in the ledger", self.slack.sent[-1]["text"])

    def test_a_ledger_row_with_no_source_size_never_invents_one(self):
        self.record("Kubo (2016)", None, 20 * GIB)
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:05:00  CYCLE COMPLETE Kubo (2016)\n")
        self.n.tick()
        text = self.slack.sent[-1]["text"]
        self.assertIn("20.00 GiB", text)
        self.assertNotIn("frees", text)
        self.assertIn("original size not recorded", text)

    def test_one_source_dropping_out_for_a_tick_resends_nothing(self):
        """events.py degrades PER SOURCE (an unreadable watch log is just
        absent from an otherwise full snapshot), so the snapshot is not
        empty and the empty-snapshot guard does not fire. The seen-set must
        be a union, never a replacement, or a one-tick USB stall re-sends
        every deletion notice in the tail when the file comes back."""
        os.remove(self.watch_log)
        self.assertEqual(self.n.tick(), 0)
        _write(self.watch_log, WATCH_LOG)
        self.assertEqual(self.n.tick(), 0)
        os.remove(self.driver_log)
        self.assertEqual(self.n.tick(), 0)
        _write(self.driver_log, DRIVER_LOG)
        self.assertEqual(self.n.tick(), 0)
        self.assertEqual(self.slack.sent, [])

    def test_the_seen_set_is_bounded(self):
        with open(self.driver_log, "a") as f:
            for i in range(notify.SEEN_CAP + 50):
                f.write(f"2026-09-01 16:00:{i % 60:02d}  waiting: tick {i}\n")
        self.n.tick()
        self.assertLessEqual(len(self.n.seen), notify.SEEN_CAP)

    def test_the_kill_projection_is_remembered_across_the_truncation(self):
        """The real sequence: the watcher's KILLED line sits for up to 30 s,
        then the driver logs LADDER and truncates the watch log in the same
        pass. The notifier saw the KILLED line in an earlier tick and must
        remember it, or the enrichment never fires in production."""
        with open(self.watch_log, "a") as f:
            f.write(
                "KILLED|Movie (2000)|projected 9.0% of original at 5.10% (CRF 12)|band 30-80|partial deleted|next: CRF 10\n"
            )
        self.n.tick()
        self.assertEqual(self.slack.sent, [])
        _write(self.watch_log, "")
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:00:00  LADDER Movie (2000) -> CRF 10\n")
        self.assertEqual(self.n.tick(), 1)
        text = self.slack.sent[0]["text"]
        self.assertIn("CRF 12 → 10", text)
        self.assertIn("9.0% of original at 5.10% progress", text)

    def test_a_ladder_line_still_sends_with_no_kill_ever_seen(self):
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:00:00  LADDER Other (2001) -> CRF 10\n")
        _write(self.watch_log, "")
        self.assertEqual(self.n.tick(), 1)
        self.assertIn("CRF 12 → 10", self.slack.sent[0]["text"])

    def test_a_reused_watch_log_does_not_resend(self):
        """The final KILLED line carries the file's mtime; once the retry
        appends a QUARTER after it, its ts flips to None. Same event."""
        with open(self.watch_log, "a") as f:
            f.write(
                "FAILED|Movie (2000)|HandBrakeCLI died at 47% (reboot/kill?) — delete partial and restart\n"
            )
        self.assertEqual(self.n.tick(), 1)
        with open(self.watch_log, "a") as f:
            f.write(
                "QUARTER|Movie (2000)|25%|current 1 GB|projected 5 GB|original 50 GB|10%|OK|ETA 1h\n"
            )
        self.assertEqual(self.n.tick(), 0)

    def test_the_cursor_survives_a_restart(self):
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:00:00  CYCLE COMPLETE Kubo (2016)\n")
        self.n.tick()
        again = self.notifier()
        self.assertEqual(again.tick(), 0)

    def test_a_burst_keeps_the_deletions_and_errors_and_names_what_it_dropped(self):
        """Oldest-first capping kept ten ladder lines and dropped the newest
        deletion. Severity decides what survives the cap, and the summary
        says what kind of thing was left out."""
        with open(self.driver_log, "a") as f:
            for i in range(notify.BURST_CAP + 3):
                f.write(
                    f"2026-09-01 16:{i:02d}:00  LADDER Filler {i} (2000) -> CRF 16\n"
                )
            f.write(
                "2026-09-01 17:00:00  ERROR Vital (1999): needs a human: suspect - marked for review, moving on\n"
            )
            f.write("2026-09-01 17:01:00  CYCLE COMPLETE Newest (2010)\n")
        self.n.tick()
        texts = [m["text"] for m in self.slack.sent]
        self.assertEqual(len(texts), notify.BURST_CAP + 1)
        self.assertTrue(any("Newest (2010)" in t for t in texts))
        self.assertTrue(any("Vital (1999)" in t for t in texts))
        self.assertIn("5 more", texts[-1])
        self.assertIn("Ladder UP", texts[-1])
        self.assertEqual(self.n.tick(), 0)

    def test_within_one_class_the_newest_survive_the_cap(self):
        with open(self.driver_log, "a") as f:
            for i in range(notify.BURST_CAP + 3):
                f.write(
                    f"2026-09-01 16:{i:02d}:00  CYCLE COMPLETE Title {i:02d} (2000)\n"
                )
        self.n.tick()
        texts = " ".join(m["text"] for m in self.slack.sent)
        self.assertIn(f"Title {notify.BURST_CAP + 2:02d} (2000)", texts)
        self.assertNotIn("Title 00 (2000)", texts)


class Delivery(_Base):
    def setUp(self):
        super().setUp()
        _write(self.driver_log, DRIVER_LOG)
        self.n = self.notifier()
        self.n.tick()
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:00:00  CYCLE COMPLETE Kubo (2016)\n")

    def test_a_failed_send_is_retried_after_the_backoff(self):
        self.slack.fail = True
        self.assertEqual(self.n.tick(), 0)
        self.assertTrue(any("slack" in e for e in self.errs))
        self.slack.fail = False
        self.clock[0] += notify.RETRY_SECONDS
        self.assertEqual(self.n.tick(), 1)
        self.assertEqual(len(self.slack.sent), 1)

    def test_a_failed_channel_backs_off_and_the_wait_doubles(self):
        """1440 Gmail logins a day against a wrong app password gets the
        account throttled. A failed channel waits, and the wait doubles."""
        self.slack.fail = True
        self.n.tick()
        self.assertEqual(self.slack.calls, 1)
        self.n.tick()
        self.assertEqual(self.slack.calls, 1)  # not yet
        self.clock[0] += notify.RETRY_SECONDS
        self.n.tick()
        self.assertEqual(self.slack.calls, 2)
        self.clock[0] += notify.RETRY_SECONDS
        self.n.tick()
        self.assertEqual(self.slack.calls, 2)  # doubled: 120 s now
        self.clock[0] += notify.RETRY_SECONDS
        self.n.tick()
        self.assertEqual(self.slack.calls, 3)

    def test_a_broken_channel_does_not_delay_the_working_one(self):
        self.slack.fail = True
        self.n.tick()
        self.assertEqual(len(self.email.sent), 1)
        with open(self.driver_log, "a") as f:
            f.write("2026-09-01 16:10:00  CYCLE COMPLETE Other (2001)\n")
        self.n.tick()  # slack is inside its backoff; email must still go
        self.assertEqual(len(self.email.sent), 2)
        self.assertEqual(self.slack.calls, 1)

    def test_the_half_that_landed_is_not_resent(self):
        self.email.fail = True
        self.n.tick()
        self.assertEqual(len(self.slack.sent), 1)
        self.email.fail = False
        self.clock[0] += notify.RETRY_SECONDS
        self.n.tick()
        self.assertEqual(len(self.slack.sent), 1)
        self.assertEqual(len(self.email.sent), 1)

    def test_a_landed_channel_is_persisted_before_the_next_is_tried(self):
        """`smeltr restart` SIGKILLs after 5 s; a 15 s SMTP handshake after a
        successful Slack send is a wide window. What landed must already be
        on disk when the next channel starts."""
        seen_on_disk = []

        def email(cfg, message):
            seen_on_disk.append(self.cursor()["pending"][0]["sent"])
            raise OSError("down")

        n = self.notifier(email=email)
        n.tick()
        self.assertEqual(seen_on_disk, [["slack"]])

    def test_pending_survives_a_restart(self):
        self.slack.fail = True
        self.email.fail = True
        self.n.tick()
        self.slack.fail = self.email.fail = False
        self.clock[0] += notify.RETRY_SECONDS
        again = self.notifier()
        self.assertEqual(again.tick(), 1)
        self.assertEqual(len(self.slack.sent), 1)

    def test_pending_is_dropped_after_max_age_with_a_log_line(self):
        self.slack.fail = True
        self.email.fail = True
        self.n.tick()
        self.clock[0] += notify.MAX_PENDING_SECONDS + 1
        self.slack.fail = self.email.fail = False
        self.assertEqual(self.n.tick(), 0)
        self.assertEqual(self.slack.sent, [])
        self.assertTrue(any("dropped" in e for e in self.errs))

    def test_a_channel_that_is_not_configured_is_skipped_not_failed(self):
        del self.config["email"]
        n = self.notifier()
        self.assertEqual(n.tick(), 1)
        self.assertEqual(self.email.sent, [])
        self.assertEqual(self.errs, [])

    def test_the_tmp_file_is_never_left_beside_the_cursor(self):
        self.n.tick()
        left = [f for f in os.listdir(self.home.name) if f.startswith("notify.cursor.")]
        self.assertEqual(left, [])


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "notify.json")
        self.errs = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_means_off(self):
        self.assertIsNone(notify.load_config(self.path, err=self.errs.append))
        self.assertEqual(self.errs, [])

    def test_a_world_readable_file_is_refused(self):
        """It holds the Gmail app password. The docstring said 0600; nothing
        enforced it. Refuse the way ssh refuses a loose key."""
        _write(
            self.path, json.dumps({"slack": {"webhook": "https://hooks.slack.com/x"}})
        )
        os.chmod(self.path, 0o644)
        self.assertIsNone(notify.load_config(self.path, err=self.errs.append))
        self.assertTrue(any("chmod 600" in e for e in self.errs))

    def test_malformed_file_is_off_and_loud(self):
        _write_secret(self.path, "{not json")
        self.assertIsNone(notify.load_config(self.path, err=self.errs.append))
        self.assertTrue(self.errs and "notify.json" in self.errs[0])

    def test_no_channel_at_all_is_off_and_loud(self):
        _write_secret(self.path, json.dumps({"slack": {}, "email": {"host": "h"}}))
        self.assertIsNone(notify.load_config(self.path, err=self.errs.append))
        self.assertTrue(self.errs)

    def test_one_channel_is_enough(self):
        _write_secret(
            self.path, json.dumps({"slack": {"webhook": "https://hooks.slack.com/x"}})
        )
        cfg = notify.load_config(self.path, err=self.errs.append)
        self.assertIn("slack", cfg)
        self.assertNotIn("email", cfg)

    def test_email_needs_every_field(self):
        _write_secret(
            self.path,
            json.dumps(
                {"email": {"host": "smtp.gmail.com", "user": "u", "password": "p"}}
            ),
        )
        self.assertIsNone(notify.load_config(self.path, err=self.errs.append))
        self.assertTrue(any("to" in e for e in self.errs))

    def test_a_bad_port_turns_email_off_instead_of_raising(self):
        """load_config runs in server main() above the socket bind; a typo
        in an optional file must not cost the page its pause switch."""
        _write_secret(
            self.path,
            json.dumps(
                {
                    "email": {
                        "host": "h",
                        "user": "u",
                        "password": "p",
                        "to": "t",
                        "port": "five87",
                    }
                }
            ),
        )
        self.assertIsNone(notify.load_config(self.path, err=self.errs.append))
        self.assertTrue(any("port" in e for e in self.errs))

    def test_email_defaults(self):
        _write_secret(
            self.path,
            json.dumps(
                {
                    "email": {
                        "host": "smtp.gmail.com",
                        "user": "u@gmail.com",
                        "password": "p",
                        "to": "me@x",
                    }
                }
            ),
        )
        cfg = notify.load_config(self.path, err=self.errs.append)
        self.assertEqual(cfg["email"]["port"], 587)
        self.assertEqual(cfg["email"]["from"], "u@gmail.com")


class Formatting(unittest.TestCase):
    def test_slack_payload_is_one_text_line_with_a_dated_clock(self):
        n = {
            "what": "finished",
            "title": "Kubo (2016)",
            "ts": "2026-09-01 15:24:46",
            "text": "5.46 GiB from 56.90 GiB (90% smaller)",
            "detail": None,
        }
        p = notify.slack_message(n)
        self.assertEqual(set(p), {"text"})
        self.assertIn("Kubo (2016)", p["text"])
        self.assertIn("3:24 PM Tue Sep 1", p["text"])
        self.assertNotIn("~", p["text"])

    def test_an_estimated_stamp_is_marked(self):
        """The Events tab draws ~ on a watch log's mtime-derived stamp; the
        phone must not hide what the tab discloses."""
        n = {
            "what": "failed",
            "title": "Kubo (2016)",
            "ts": "2026-09-01 15:24:46",
            "text": "died",
            "detail": None,
            "approx": True,
        }
        t = notify.slack_message(n)["text"]
        self.assertIn("~3:24 PM", t)
        self.assertIn("estimated", t)

    def test_a_garbled_stamp_prints_no_clock(self):
        for ts in ("garbage", 123456, None):
            n = {"what": "failed", "title": "K", "ts": ts, "text": "", "detail": None}
            self.assertNotIn("(at", notify.slack_message(n)["text"])

    def test_email_subject_and_body(self):
        n = {
            "what": "ladder-up",
            "title": "Movie (2000)",
            "ts": None,
            "text": "CRF 14 → 16 (projected 91.0% of original at 6.0%)",
            "detail": None,
        }
        m = notify.email_message(n)
        self.assertTrue(m["subject"].startswith("smeltr: "))
        self.assertIn("Movie (2000)", m["subject"])
        self.assertIn("CRF 14 → 16", m["body"])

    def test_every_what_has_a_label(self):
        for what in notify.WHATS:
            n = {"what": what, "title": "T", "ts": None, "text": "", "detail": None}
            self.assertNotIn("?", notify.slack_message(n)["text"])
            self.assertNotIn("?", notify.email_message(n)["subject"])

    def test_the_test_message_is_labelled_as_a_test(self):
        self.assertIn("test", notify.WHATS)
        self.assertIn("test", notify.WHATS["test"][1].lower())

    def test_the_two_ladder_labels_cannot_be_confused_at_a_glance(self):
        up, down = notify.WHATS["ladder-up"], notify.WHATS["ladder-down"]
        self.assertNotEqual(up[0], down[0])
        self.assertIn("UP", up[1])
        self.assertIn("DOWN", down[1])


class Transports(unittest.TestCase):
    """The real senders, against fakes of the stdlib they call."""

    def test_slack_posts_json_to_the_webhook(self):
        calls = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok"

        def opener(req, timeout):
            calls.append(
                (
                    req.full_url,
                    req.get_header("Content-type"),
                    json.loads(req.data.decode()),
                    timeout,
                )
            )
            return Resp()

        with mock.patch.object(notify, "urlopen", opener):
            notify.send_slack({"webhook": "https://hooks.slack.com/x"}, {"text": "hi"})
        self.assertEqual(calls[0][0], "https://hooks.slack.com/x")
        self.assertEqual(calls[0][1], "application/json")
        self.assertEqual(calls[0][2], {"text": "hi"})

    def test_slack_non_2xx_raises(self):
        class Resp:
            status = 500

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"no"

        with mock.patch.object(notify, "urlopen", lambda req, timeout: Resp()):
            with self.assertRaises(OSError):
                notify.send_slack(
                    {"webhook": "https://hooks.slack.com/x"}, {"text": "hi"}
                )

    def test_email_uses_starttls_and_login(self):
        seq = []

        class SMTP:
            def __init__(self, host, port, timeout):
                seq.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self, context=None):
                seq.append(("starttls",))

            def login(self, u, p):
                seq.append(("login", u, p))

            def send_message(self, msg):
                seq.append(("send", msg["To"], msg["Subject"]))

        cfg = {
            "host": "smtp.gmail.com",
            "port": 587,
            "user": "u@gmail.com",
            "password": "p",
            "to": "me@x",
            "from": "u@gmail.com",
        }
        with mock.patch.object(notify.smtplib, "SMTP", SMTP):
            notify.send_email(cfg, {"subject": "s", "body": "b"})
        self.assertEqual([s[0] for s in seq], ["connect", "starttls", "login", "send"])
        self.assertEqual(seq[-1], ("send", "me@x", "s"))


class Wiring(unittest.TestCase):
    """The thread starts from server.main() and from nowhere in the decision
    path; with no notify.json it starts nothing; and nothing it does can
    take the server down."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_server_main_starts_the_notifier(self):
        with open(
            os.path.join(self.ROOT, "dashboard", "server.py"), encoding="utf-8"
        ) as fh:
            src = fh.read()
        main = src[src.index("def main() -> None:") :]
        self.assertIn("notify.start(", main)

    def test_no_config_starts_no_thread(self):
        with tempfile.TemporaryDirectory() as d:
            before = {t.name for t in threading.enumerate()}
            self.assertFalse(notify.start(d))
            self.assertEqual({t.name for t in threading.enumerate()} - before, set())

    def test_a_notifier_that_cannot_start_never_takes_the_server_down(self):
        """start() runs in main() above the socket bind."""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                notify, "load_config", side_effect=RuntimeError("boom")
            ):
                self.assertFalse(notify.start(d))

    def test_the_launcher_offers_a_test_send(self):
        with open(os.path.join(self.ROOT, "smeltr"), encoding="utf-8") as fh:
            self.assertIn("notify-test)", fh.read())

    def test_cli_test_send_refuses_without_config(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(
                [
                    sys.executable,
                    os.path.join(self.ROOT, "dashboard", "notify.py"),
                    "--test",
                ],
                env=dict(os.environ, SMELTR_DIR=d),
                capture_output=True,
                text=True,
            )
        self.assertEqual(r.returncode, 1)
        self.assertIn("notify.json", r.stderr)


if __name__ == "__main__":
    unittest.main()
