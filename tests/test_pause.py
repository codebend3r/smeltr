"""Pause-after-current: the dashboard's pause flag and its driver contract.

The whole mechanism is two moving parts: the server writes core.PAUSE_FLAG,
and next_title.py answers exit 3 while it exists -- the driver's existing
wait-and-recheck path, so no autopilot.sh change was needed. These tests pin
the facts that make that safe:

  * paused is NEVER the stop condition. Exit 1 makes the driver EXIT; exit 3
    makes it wait 300 s and recheck, which is what lets resume work without
    touching the driver.
  * the flag file round-trips and is idempotent both ways.
  * a request from this machine's own bound address may write (the operator
    on the LAN URL), other network peers may not without the opt-in.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

from pipeline import core
from pipeline import next_title


class FlagFile(unittest.TestCase):
    def setUp(self):
        self._orig = core.PAUSE_FLAG
        self._tmp = tempfile.TemporaryDirectory()
        core.PAUSE_FLAG = os.path.join(self._tmp.name, "pause")

    def tearDown(self):
        core.PAUSE_FLAG = self._orig
        self._tmp.cleanup()

    def test_round_trip(self):
        self.assertFalse(core.paused())
        core.set_paused(True)
        self.assertTrue(core.paused())
        core.set_paused(False)
        self.assertFalse(core.paused())

    def test_idempotent_both_ways(self):
        core.set_paused(True)
        core.set_paused(True)
        self.assertTrue(core.paused())
        core.set_paused(False)
        core.set_paused(False)
        self.assertFalse(core.paused())

    def test_fails_closed_when_the_flag_cannot_be_read(self):
        # An unreadable SMELTR_DIR must read as PAUSED, not as "resume":
        # waiting is always the safe direction, and os.path.exists would
        # have answered False here.
        core.set_paused(True)
        os.chmod(self._tmp.name, 0o000)
        try:
            self.assertTrue(core.paused())
        finally:
            os.chmod(self._tmp.name, 0o700)
        self.assertTrue(core.paused())


class DriverContract(unittest.TestCase):
    """next_title's exit code IS the API the driver consumes."""

    def setUp(self):
        self._saved = (core.offline_roots, core.paused, core.queue_cached)

    def tearDown(self):
        core.offline_roots, core.paused, core.queue_cached = self._saved

    def _main(self):
        # main() reads sys.argv for the threshold; under unittest argv[1]
        # is the runner's own "discover".
        argv, sys.argv = sys.argv, ["next_title.py"]
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                return next_title.main()
        finally:
            sys.argv = argv

    def test_paused_is_exit_3_never_the_stop_condition(self):
        # An empty queue normally exits 1 and the driver exits for good on
        # it. Paused must be checked BEFORE the queue scan so a paused idle
        # pipeline waits instead of concluding the job is finished.
        core.offline_roots = lambda: []
        core.paused = lambda: True
        core.queue_cached = lambda min_mbps=None: []
        self.assertEqual(self._main(), 3)

    def test_paused_beats_offline_and_states_both(self):
        # Reversed 2026-08-31 (offline used to be reported first). Pause is
        # now checked before EVERYTHING: it is the one sanctioned reason to
        # withhold an encode, and since the offline check moved below the
        # pick (a staged title encodes with the NAS gone), a pre-emptive
        # exit 2 would have let blindness outrank the operator's own choice.
        # Both codes still make the driver wait; the stderr carries both
        # facts so neither is hidden.
        core.offline_roots = lambda: ["/Volumes/Vhagar/Media/4K Movies"]
        core.paused = lambda: True
        core.queue_cached = lambda min_mbps=None: []
        err = io.StringIO()
        argv, sys.argv = sys.argv, ["next_title.py"]
        try:
            with contextlib.redirect_stderr(err):
                rc = next_title.main()
        finally:
            sys.argv = argv
        self.assertEqual(rc, 3)
        self.assertIn("paused", err.getvalue())
        self.assertIn("library incomplete", err.getvalue())

    def test_offline_with_a_staged_pick_still_encodes(self):
        # THE no-gap rule (operator requirement, 2026-08-31): an unreachable
        # NAS must not idle the CPU while staged work exists. The staged copy
        # is byte-for-byte the library original; only record/sync needs the
        # NAS, and the driver defers that side separately. A mount blip at
        # judge time used to halt everything for 4h16m.
        core.offline_roots = lambda: ["/Volumes/Vhagar/Media/4K Movies"]
        core.paused = lambda: False
        core.queue_cached = lambda min_mbps=None: []
        row = {"title": "Alpha (2001)", "staged": True}
        saved = core.pick_next
        core.pick_next = lambda rows: (row, set())
        try:
            out = io.StringIO()
            argv, sys.argv = sys.argv, ["next_title.py"]
            try:
                with (
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    rc = next_title.main()
            finally:
                sys.argv = argv
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "Alpha (2001)")
        finally:
            core.pick_next = saved

    def test_offline_with_nothing_staged_is_2_never_the_stop_condition(self):
        # With no staged candidate the old rule stands in full: blind and
        # idle answers "library incomplete", never "job finished".
        core.offline_roots = lambda: ["/Volumes/Vhagar/Media/4K Movies"]
        core.paused = lambda: False
        core.queue_cached = lambda min_mbps=None: []
        self.assertEqual(self._main(), 2)

    def test_unpaused_empty_queue_is_still_the_stop_condition(self):
        core.offline_roots = lambda: []
        core.paused = lambda: False
        core.queue_cached = lambda min_mbps=None: []
        self.assertEqual(self._main(), 1)


class OfflineRootKeepsStagedRows(unittest.TestCase):
    """queue() must keep a row whose library root is offline IF the title is
    staged on the X9 -- the staged copy is what encodes, and dropping it idled
    the CPU for the length of every NAS outage. An offline row that is NOT
    staged still drops (its size cannot be observed and it cannot encode)."""

    def setUp(self):
        self._saved = (
            core.offline_roots,
            core.load_index,
            core.X9,
            core.live_encodes,
            core.ledger,
            core.load_overrides,
        )
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name
        root = "/Volumes/Vhagar/Media/4K Movies"
        self._root = root
        core.offline_roots = lambda: [root]
        core.live_encodes = lambda: []
        core.ledger = lambda: []
        core.load_overrides = lambda: {
            "skip": [],
            "priority": [],
            "crf": {},
            "corrupt": False,
        }
        core.load_index = lambda: [
            {
                "path": root + "/A/Alpha (2001)/Alpha (2001) Remux-2160p.mkv",
                "overall_bitrate": 90e6,
            },
            {
                "path": root + "/B/Beta (2002)/Beta (2002) Remux-2160p.mkv",
                "overall_bitrate": 80e6,
            },
        ]
        d = os.path.join(self._tmp.name, "Alpha (2001)")
        os.makedirs(d)
        with open(os.path.join(d, "Alpha (2001) Remux-2160p.mkv"), "w") as fh:
            fh.write("x" * 1024)

    def tearDown(self):
        (
            core.offline_roots,
            core.load_index,
            core.X9,
            core.live_encodes,
            core.ledger,
            core.load_overrides,
        ) = self._saved
        self._tmp.cleanup()

    def test_staged_row_survives_its_root_going_offline(self):
        rows = core.queue(min_mbps=70)
        titles = [r["title"] for r in rows]
        self.assertIn("Alpha (2001)", titles)

    def test_its_size_is_read_from_the_staged_copy(self):
        row = next(r for r in core.queue(min_mbps=70) if r["title"] == "Alpha (2001)")
        # The library path cannot be statted; the staged copy is the same
        # bytes. Never None, never a guess.
        self.assertEqual(row["bytes"], 1024)

    def test_unstaged_offline_row_still_drops(self):
        titles = [r["title"] for r in core.queue(min_mbps=70)]
        self.assertNotIn("Beta (2002)", titles)


class ArrivingFolders(unittest.TestCase):
    """A staged folder holding only a replenish .partial must never be
    printed: the concurrent driver reaches next_title while its backgrounded
    sync's pull is in flight, and start_encode halts on "no source file".
    The dashboard's next_up and this pick share core.pick_next, so agreement
    is by construction; this pins the driver-facing behavior."""

    def setUp(self):
        self._saved = (core.offline_roots, core.paused, core.queue_cached, core.X9)
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name
        core.offline_roots = lambda: []
        core.paused = lambda: False

    def tearDown(self):
        (core.offline_roots, core.paused, core.queue_cached, core.X9) = self._saved
        self._tmp.cleanup()

    def _folder(self, title, files):
        d = os.path.join(self._tmp.name, title)
        os.makedirs(d)
        for f in files:
            with open(os.path.join(d, f), "w"):
                pass

    def _main(self):
        argv, sys.argv = sys.argv, ["next_title.py"]
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()) as out,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc = next_title.main()
            return rc, out.getvalue().strip()
        finally:
            sys.argv = argv

    def test_arriving_folder_is_passed_over(self):
        self._folder("Arriving (2012)", ["Arriving (2012).mkv.partial"])
        self._folder("Ready (1999)", ["Ready (1999) Remux-2160p.mkv"])
        core.queue_cached = lambda min_mbps=None: [
            {"title": "Arriving (2012)", "staged": True},
            {"title": "Ready (1999)", "staged": True},
        ]
        self.assertEqual(self._main(), (0, "Ready (1999)"))

    def test_only_arriving_folders_wait_not_stop(self):
        # Falling through to exit 1 here would make the driver exit 0 while
        # a pull is landing -- "finished" claimed with work still arriving.
        self._folder("Arriving (2012)", ["Arriving (2012).mkv.partial"])
        core.queue_cached = lambda min_mbps=None: [
            {"title": "Arriving (2012)", "staged": True},
        ]
        self.assertEqual(self._main()[0], 3)


class PickNext(unittest.TestCase):
    """core.pick_next is the ONE pick, shared by next_title.py and the
    dashboard's _mark_ready. Its wait reasons reach the driver log verbatim
    through next_title's stderr, so they must name the actual state -- a
    guessed message once sent the operator debugging overrides that were
    fine."""

    def setUp(self):
        self._saved = (core.X9, core.queue_cached)
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name

    def tearDown(self):
        core.X9, core.queue_cached = self._saved
        self._tmp.cleanup()

    def _folder(self, title, files):
        d = os.path.join(self._tmp.name, title)
        os.makedirs(d)
        for f in files:
            with open(os.path.join(d, f), "w"):
                pass

    def test_live_output_excludes_the_row_without_a_reason(self):
        # The in-progress encode's own file matches *2160p HEVC*.mkv, so the
        # live row is passed silently -- awaiting sync is not a wait state.
        self._folder("Live (2020)", ["Live (2020).mkv", "Live (2020) 2160p HEVC.mkv"])
        row, waits = core.pick_next([{"title": "Live (2020)", "staged": True}])
        self.assertIsNone(row)
        self.assertEqual(waits, set())

    def test_reasons_name_each_actual_state(self):
        self._folder("Arriving (2012)", ["Arriving (2012).mkv.partial"])
        self._folder("Skipped (1999)", ["Skipped (1999) Remux-2160p.mkv"])
        row, waits = core.pick_next(
            [
                {"title": "Arriving (2012)", "staged": True},
                {"title": "Skipped (1999)", "staged": True, "skipped": True},
            ]
        )
        self.assertIsNone(row)
        self.assertEqual(waits, {"arriving", "skipped"})

    def test_skipped_arriving_folder_counts_as_arriving(self):
        # No source file exists to encode regardless of the skip.
        self._folder("Both (2005)", ["Both (2005).mkv.partial"])
        row, waits = core.pick_next(
            [{"title": "Both (2005)", "staged": True, "skipped": True}]
        )
        self.assertIsNone(row)
        self.assertEqual(waits, {"arriving"})

    def _next_title_stderr(self):
        argv, sys.argv = sys.argv, ["next_title.py"]
        saved = (core.offline_roots, core.paused)
        core.offline_roots = lambda: []
        core.paused = lambda: False
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                rc = next_title.main()
            return rc, err.getvalue()
        finally:
            sys.argv = argv
            core.offline_roots, core.paused = saved

    def test_wait_message_names_arriving_not_skipped(self):
        self._folder("Arriving (2012)", ["Arriving (2012).mkv.partial"])
        core.queue_cached = lambda min_mbps=None: [
            {"title": "Arriving (2012)", "staged": True}
        ]
        rc, err = self._next_title_stderr()
        self.assertEqual(rc, 3)
        self.assertIn("still landing", err)
        self.assertNotIn("hand-skipped", err)

    def test_wait_message_names_skipped_not_arriving(self):
        self._folder("Skipped (1999)", ["Skipped (1999) Remux-2160p.mkv"])
        core.queue_cached = lambda min_mbps=None: [
            {"title": "Skipped (1999)", "staged": True, "skipped": True}
        ]
        rc, err = self._next_title_stderr()
        self.assertEqual(rc, 3)
        self.assertIn("hand-skipped", err)
        self.assertNotIn("still landing", err)


class ServerGates(unittest.TestCase):
    def setUp(self):
        from dashboard import server

        self.server = server
        self._flag = core.PAUSE_FLAG
        self._tmp = tempfile.TemporaryDirectory()
        core.PAUSE_FLAG = os.path.join(self._tmp.name, "pause")

    def tearDown(self):
        core.PAUSE_FLAG = self._flag
        self._tmp.cleanup()

    def _handler(self, ip):
        h = object.__new__(self.server.Handler)
        h.client_address = (ip, 12345)
        return h

    def test_apply_pause_validates_body(self):
        h = self._handler("127.0.0.1")
        self.assertIsNotNone(h._apply_pause({}))
        self.assertIsNotNone(h._apply_pause({"paused": "yes"}))
        self.assertIsNotNone(h._apply_pause({"paused": 1}))
        self.assertFalse(core.paused())

    def test_apply_pause_writes_and_clears_the_flag(self):
        h = self._handler("127.0.0.1")
        self.assertIsNone(h._apply_pause({"paused": True}))
        self.assertTrue(core.paused())
        self.assertIsNone(h._apply_pause({"paused": False}))
        self.assertFalse(core.paused())

    def test_self_connection_counts_as_this_mac(self):
        # The operator loading the page via http://<lan-ip>:8787 on this Mac
        # arrives with the machine's own address as peer, not loopback. The
        # predicate is peer == the LISTENER'S OWN live address -- never a
        # cached address list, which goes stale when an interface drops and
        # DHCP hands its address to another device.
        class _Sock:
            def __init__(self, ip):
                self._ip = ip

            def getsockname(self):
                return (self._ip, 8787)

        def handler(peer, local):
            h = self._handler(peer)
            h.connection = _Sock(local)
            return h

        self.assertTrue(handler("192.0.2.10", "192.0.2.10")._writes_ok())
        self.assertTrue(handler("127.0.0.1", "127.0.0.1")._writes_ok())
        if not self.server.LAN_WRITES:
            # A different LAN peer -- including an address this machine
            # USED to hold -- may not write.
            self.assertFalse(handler("192.0.2.99", "192.0.2.10")._writes_ok())

    def test_summary_is_the_one_carrier_of_paused(self):
        # report.py's banner and the page both read summary["paused"] -- one
        # fact, one carrier. A second top-level copy in the payload once
        # existed and could disagree with this one within a frame.
        saved = (core.live_encodes, core.offline_roots, core.paused)
        try:
            core.live_encodes = lambda: []
            core.offline_roots = lambda: []
            core.paused = lambda: True
            self.assertTrue(core.summary(hist=[], q=[])["paused"])
            core.paused = lambda: False
            self.assertFalse(core.summary(hist=[], q=[])["paused"])
        finally:
            core.live_encodes, core.offline_roots, core.paused = saved


class MarkReady(unittest.TestCase):
    """ready is server-computed WHOLE: paused gating lives in _mark_ready,
    never re-derived in the page. next_up survives a pause -- the queue pill
    says "next after resume" -- but the green row and the start button do
    not, because green means "going now" and while paused nothing goes."""

    def setUp(self):
        from dashboard import server

        self.server = server
        self._x9 = core.X9
        self._tmp = tempfile.TemporaryDirectory()
        core.X9 = self._tmp.name
        d = os.path.join(self._tmp.name, "Ready (1999)")
        os.makedirs(d)
        with open(os.path.join(d, "Ready (1999) Remux-2160p.mkv"), "w"):
            pass

    def tearDown(self):
        core.X9 = self._x9
        self._tmp.cleanup()

    def _rows(self):
        return [{"title": "Ready (1999)", "staged": True}]

    def test_idle_unpaused_row_is_ready(self):
        rows = self._rows()
        self.server._mark_ready(rows, live=[], paused=False)
        self.assertTrue(rows[0]["ready"])
        self.assertTrue(rows[0]["next_up"])

    def test_paused_keeps_next_up_but_never_ready(self):
        rows = self._rows()
        self.server._mark_ready(rows, live=[], paused=True)
        self.assertFalse(rows[0]["ready"])
        self.assertTrue(rows[0]["next_up"])

    def test_a_live_encode_keeps_next_up_but_never_ready(self):
        rows = self._rows()
        self.server._mark_ready(rows, live=[{"title": "Other (2000)"}], paused=False)
        self.assertFalse(rows[0]["ready"])
        self.assertTrue(rows[0]["next_up"])

    def test_an_arriving_pull_suppresses_both_flags(self):
        # A dashboard pull lands in a hidden .pull-<title> dir; _arrivals()
        # keys it onto the visible row as arriving_bytes, which pick_next
        # cannot see from the folder's own contents. The row must carry
        # neither flag: /api/encode/start would refuse it with a 409, and
        # the green row must never promise a click that cannot land.
        rows = self._rows()
        rows[0]["arriving_bytes"] = 4096
        self.server._mark_ready(rows, live=[], paused=False)
        self.assertFalse(rows[0]["ready"])
        self.assertFalse(rows[0]["next_up"])


if __name__ == "__main__":
    unittest.main()
