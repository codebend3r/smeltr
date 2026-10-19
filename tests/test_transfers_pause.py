"""Pause-all-transfers: the dashboard button's marker and the stage pump.

The button writes `.transfers-paused` on the staging drive. The shell
starters (replenisher tick, replenish script, pusher tick) honour it in their
own suites; this one pins the dashboard half: the marker round-trips, the
stage pump holds against it with a reason the row can show, the endpoint
refuses an unmounted drive, and the route is LAN-only.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import pushes, server
from pipeline import core


class TransfersPause(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.x9 = os.path.join(self.tmp.name, "x9")
        os.makedirs(self.x9)
        patch = mock.patch.object(core, "X9", self.x9)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_marker_is_the_file_the_shell_starters_look_for(self):
        pushes.TRANSFERS_PAUSED.set(True)
        self.assertTrue(os.path.exists(os.path.join(self.x9, ".transfers-paused")))
        pushes.TRANSFERS_PAUSED.set(False)
        self.assertFalse(pushes.TRANSFERS_PAUSED.is_set())

    def test_stage_pump_holds_while_paused_and_releases_after(self):
        with mock.patch.object(server, "_stage_active", {"title": None}), mock.patch.object(
            server, "_wire_title", return_value=(False, None)
        ):
            self.assertIsNone(server._wire_busy_locked())
            pushes.TRANSFERS_PAUSED.set(True)
            self.assertEqual(server._wire_busy_locked(), "waiting — transfers are paused")
            pushes.TRANSFERS_PAUSED.set(False)
            self.assertIsNone(server._wire_busy_locked())

    def test_endpoint_writes_the_marker_and_validates_the_body(self):
        h = server.Handler.__new__(server.Handler)
        self.assertEqual(h._apply_transfers_pause({}), "expected {paused: bool}")
        self.assertEqual(h._apply_transfers_pause({"paused": "yes"}), "expected {paused: bool}")
        self.assertIsNone(h._apply_transfers_pause({"paused": True}))
        self.assertTrue(pushes.TRANSFERS_PAUSED.is_set())
        self.assertIsNone(h._apply_transfers_pause({"paused": False}))
        self.assertFalse(pushes.TRANSFERS_PAUSED.is_set())

    def test_endpoint_refuses_an_unmounted_drive(self):
        with mock.patch.object(core, "X9", os.path.join(self.tmp.name, "gone")):
            h = server.Handler.__new__(server.Handler)
            self.assertIn("not mounted", h._apply_transfers_pause({"paused": True}))

    def test_route_is_lan_only_not_public(self):
        self.assertIn("/api/transfers/pause", server.LAN_WRITE_ROUTES)
        self.assertNotIn("/api/transfers/pause", server.PUBLIC_WRITE_ROUTES)


if __name__ == "__main__":
    unittest.main()
