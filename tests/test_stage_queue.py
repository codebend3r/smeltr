"""The stage-on-demand PULL QUEUE: many titles queued, one transfer at a time.

Before this, POST /api/stage/start refused outright whenever the wire was
busy, so staging four titles meant sitting on the page waiting for each one.
Now a click enqueues and a single pump dispatches. The queue is in memory
only -- a restart drops it and the rows offer "stage" again.

What these tests pin, all of it about the pump rather than the click, because
the pump is where the dangerous decisions live:

  * a busy wire QUEUES, it does not refuse. That is the whole feature.
  * FIFO. The order clicked is the order pulled.
  * every gate is re-run at DISPATCH, not at click time. A check made when
    the button was pressed can be hours stale by the time the wire frees up.
  * a transient refusal (no room yet, NAS unreachable, replenisher running)
    HOLDS the head and says why. It must never silently drop queued work --
    the drive frees up as encodes sync.
  * a permanent refusal drops exactly ONE title and the queue continues, so a
    single dead title cannot wedge everything behind it.
  * the running pull cannot be cancelled. There is no abort path for a
    transfer, and pretending otherwise would strand a hidden folder.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import server


GIB = 1024 ** 3


def row(title, **kw):
    r = {"title": title, "bytes": 50 * GIB, "mbps": 95.0, "skipped": False,
         "staged": False, "arriving_bytes": None}
    r.update(kw)
    return r


class Fixture(unittest.TestCase):
    """A staging drive with room, no other pullers, and a clean queue."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._x9 = core.X9
        core.X9 = self._tmp.name
        server._stage_queue[:] = []
        server._stage_active["title"] = None
        server._stage_wait["why"] = None
        # The banner is module state and a "bad" note deliberately resists
        # being overwritten by an "ok" one for 15 minutes; a leftover from a
        # neighbouring test would mask the kind under assertion here.
        server._encode_note.update({"msg": None, "kind": None, "at": 0.0})
        self.started = []
        # Never spawn a real .ssh-xfer.sh. The pump's contract is "decide,
        # then call this"; what it calls is covered by the stage-control
        # code it shares with the replenisher.
        self.begin = mock.patch.object(
            server, "_begin_pull_locked",
            side_effect=lambda r, s, z: self.started.append(r["title"]) or None)
        self.begin.start()
        self.pgrep = mock.patch.object(server, "_pgrep", return_value=False)
        self.pgrep.start()
        self.index = mock.patch.object(core, "load_index", return_value=[])
        self.index.start()
        # A staging drive with ROOM -- 500 GiB free, fixed. Without this the
        # free-space gate reads the real filesystem under the tmpdir, so every
        # dispatch test silently depended on how full the host happened to be:
        # green on a dev Mac, red on a CI runner with under 20 GiB spare, and
        # the failure looks like a queue bug rather than a disk reading.
        # 500 GiB is chosen to sit between the two sizes the space tests use --
        # a 10 GiB pull still fits, a 900 GiB one still does not -- so those
        # keep testing the gate and not the host.
        self.statvfs = mock.patch.object(
            server.os, "statvfs",
            return_value=mock.Mock(f_bavail=500 * GIB, f_frsize=1))
        self.statvfs.start()

    def tearDown(self):
        mock.patch.stopall()
        core.X9 = self._x9
        server._stage_queue[:] = []
        server._stage_active["title"] = None
        server._stage_wait["why"] = None
        self._tmp.cleanup()

    def index_of(self, *titles):
        """Point the bitrate index at one source file per title."""
        self.index.stop()
        recs = [{"path": os.path.join("/lib", t[0].upper(), t, t + ".mkv")}
                for t in titles]
        for r in recs:
            os.makedirs(os.path.dirname(r["path"].replace("/lib", self._tmp.name)),
                        exist_ok=True)
        self.index = mock.patch.object(core, "load_index", return_value=recs)
        self.index.start()
        return recs

    def pump(self, rows):
        with server._stage_lock:
            if server._stage_queue and server._stage_active["title"] is None:
                server._pump_once_locked(rows)


class Enqueue(Fixture):
    def test_busy_wire_queues_rather_than_refusing(self):
        """The bug this feature exists to fix."""
        server._stage_active["title"] = "Croods, The (2013)"
        rows = [row("Cinderella (1950)")]
        with mock.patch.object(server, "_stage_candidate",
                               return_value=(rows[0], "/lib/x.mkv", 50 * GIB,
                                             None, False)):
            fake = mock.Mock(_queue_rows=mock.Mock(return_value=rows))
            err = server.Handler._apply_stage_start(fake, {"title": rows[0]["title"]})
        self.assertIsNone(err, "a busy wire must queue, not 409")
        self.assertEqual(server._stage_queue, ["Cinderella (1950)"])

    def test_second_click_on_a_queued_title_reports_its_position(self):
        server._stage_queue[:] = ["A (1990)", "B (1991)"]
        fake = mock.Mock(_queue_rows=mock.Mock(return_value=[]))
        err = server.Handler._apply_stage_start(fake, {"title": "b (1991)"})
        self.assertIn("position 2", err)

    def test_the_title_being_pulled_now_is_refused(self):
        server._stage_active["title"] = "A (1990)"
        fake = mock.Mock(_queue_rows=mock.Mock(return_value=[]))
        err = server.Handler._apply_stage_start(fake, {"title": "a (1990)"})
        self.assertIn("right now", err)

    def test_non_string_title_is_rejected(self):
        fake = mock.Mock(_queue_rows=mock.Mock(return_value=[]))
        self.assertIn("expected", server.Handler._apply_stage_start(fake,
                                                                   {"title": 3}))


class Dispatch(Fixture):
    def test_fifo(self):
        titles = ["A (1990)", "B (1991)", "C (1992)"]
        server._stage_queue[:] = list(titles)
        rows = [row(t) for t in titles]
        cand = mock.patch.object(
            server, "_stage_candidate",
            side_effect=lambda t, rs: (row(t), "/lib/x.mkv", GIB, None, False))
        cand.start()
        for expected in titles:
            self.pump(rows)
            self.assertEqual(self.started[-1], expected)
            # _begin_pull_locked is stubbed, so clear the wire by hand.
            server._stage_active["title"] = None
        self.assertEqual(self.started, titles)

    def test_a_pull_in_flight_holds_the_queue(self):
        server._stage_queue[:] = ["A (1990)"]
        server._stage_active["title"] = "Z (1999)"
        self.pump([row("A (1990)")])
        self.assertEqual(self.started, [])
        self.assertEqual(server._stage_queue, ["A (1990)"])

    def test_a_foreign_puller_holds_the_queue(self):
        """A pull orphaned by a restart owns the wire; pgrep is what sees it."""
        server._stage_queue[:] = ["A (1990)"]
        with mock.patch.object(server, "_pgrep", return_value=True):
            self.pump([row("A (1990)")])
        self.assertEqual(self.started, [])
        self.assertEqual(server._stage_queue, ["A (1990)"])
        self.assertIn("wire", server._stage_wait["why"])

    def test_a_stale_replenish_lock_is_named_as_stale(self):
        os.mkdir(os.path.join(core.X9, ".replenish.lock"))
        server._stage_queue[:] = ["A (1990)"]
        self.pump([row("A (1990)")])   # _pgrep is False: no replenisher alive
        self.assertEqual(self.started, [])
        self.assertIn("STALE", server._stage_wait["why"])


class HoldsRatherThanDrops(Fixture):
    """The queue must never quietly throw away work it was asked to do."""

    def test_a_full_drive_holds_the_head(self):
        server._stage_queue[:] = ["A (1990)", "B (1991)"]
        rows = [row("A (1990)"), row("B (1991)")]
        with mock.patch.object(server, "_stage_candidate",
                               return_value=(rows[0], "/lib/a.mkv", 900 * GIB,
                                             None, False)):
            self.pump(rows)
        self.assertEqual(self.started, [], "must not start what will not fit")
        self.assertEqual(server._stage_queue, ["A (1990)", "B (1991)"],
                         "a full drive must not drop queued work")
        self.assertIn("room", server._stage_wait["why"])

    def test_the_wait_reason_carries_no_volatile_number(self):
        """Free space moves every second as the encode writes.

        A reason string that changed every frame would put the row's shape
        back in paint()'s repaint key and rebuild the table twice a second --
        the exact bug the in-place progress rewrite removed. The measured
        figure goes to the banner note instead, once per transition.
        """
        with mock.patch.object(server.os, "statvfs") as sv:
            sv.return_value = mock.Mock(f_bavail=1, f_frsize=GIB)
            why, avail = server._space_hold(50 * GIB, [])
        self.assertIn("needs 60 GiB free", why)
        self.assertNotIn("available", why)
        self.assertEqual(avail, GIB)

    def test_an_unreachable_library_holds(self):
        """A remount must find the queue intact, not emptied by a blip."""
        recs = [{"path": "/nowhere/A (1990)/A (1990).mkv"}]
        with mock.patch.object(core, "load_index", return_value=recs):
            r, src, size, err, held = server._stage_candidate(
                "A (1990)", [row("A (1990)")])
        self.assertTrue(held, "an unmounted NAS is 'not yet', not 'never'")
        self.assertIn("unreachable", err)


class DropsExactlyOne(Fixture):
    def test_a_title_staged_meanwhile_is_dropped_and_the_queue_continues(self):
        """The replenisher got there first. That is success, not failure."""
        server._stage_queue[:] = ["A (1990)", "B (1991)"]
        rows = [row("A (1990)", staged=True), row("B (1991)")]
        self.pump(rows)
        self.assertEqual(server._stage_queue, ["B (1991)"])
        self.assertEqual(self.started, [], "nothing to pull; it is already there")
        self.assertEqual(server._encode_note["kind"], "ok",
                         "already-staged is not an error")
        # ...and the next one is not wedged behind it.
        with mock.patch.object(server, "_stage_candidate",
                               return_value=(rows[1], "/lib/b.mkv", 10 * GIB,
                                             None, False)):
            self.pump(rows)
        self.assertEqual(self.started, ["B (1991)"])

    def test_a_title_skipped_while_pending_is_dropped(self):
        server._stage_queue[:] = ["A (1990)"]
        self.pump([row("A (1990)", skipped=True)])
        self.assertEqual(server._stage_queue, [])
        self.assertEqual(self.started, [])

    def test_a_title_that_left_the_queue_is_dropped(self):
        server._stage_queue[:] = ["A (1990)"]
        self.pump([row("B (1991)")])
        self.assertEqual(server._stage_queue, [])

    def test_duplicate_folder_names_refuse_to_act_on_either(self):
        r, src, size, err, held = server._stage_candidate(
            "A (1990)", [row("A (1990)"), row("A (1990)")])
        self.assertIn("two queue rows", err)
        self.assertFalse(held)

    def test_a_title_missing_from_the_bitrate_index_is_dropped(self):
        r, src, size, err, held = server._stage_candidate(
            "A (1990)", [row("A (1990)")])
        self.assertIn("bitrate index", err)
        self.assertFalse(held)


class Cancel(Fixture):
    def setUp(self):
        super().setUp()
        self.fake = mock.Mock()

    def test_removes_a_pending_title(self):
        server._stage_queue[:] = ["A (1990)", "B (1991)"]
        err = server.Handler._apply_stage_cancel(self.fake, {"title": "a (1990)"})
        self.assertIsNone(err)
        self.assertEqual(server._stage_queue, ["B (1991)"])

    def test_refuses_the_pull_that_is_already_running(self):
        server._stage_active["title"] = "A (1990)"
        err = server.Handler._apply_stage_cancel(self.fake, {"title": "A (1990)"})
        self.assertIn("already running", err)

    def test_refuses_a_title_that_is_not_queued(self):
        err = server.Handler._apply_stage_cancel(self.fake, {"title": "Z (1999)"})
        self.assertIn("not in the pull queue", err)

    def test_emptying_the_queue_clears_the_wait_reason(self):
        """A stale 'waiting for room' under an empty queue reads as a fault."""
        server._stage_queue[:] = ["A (1990)"]
        server._stage_wait["why"] = "waiting for room — needs 60 GiB free"
        server.Handler._apply_stage_cancel(self.fake, {"title": "A (1990)"})
        self.assertIsNone(server._stage_wait["why"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
