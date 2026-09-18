"""dashboard/pushes.py -- the push-back queue as the page and the report read
it -- and _transfers() seeing a push that leaves from complete/.

The pusher (ops/push-complete.sh, 2026-09-10) leaves only files behind: the
folders still in complete/, a `.pushing-<slug>` while one is on the wire,
`.push-failed-<title>` when it gave up, `.pushed-<title>` once the X9 copy is
gone. Everything here is read off those, in the pusher's own order (largest
first), so the History row can never number the queue differently from the
loop that drains it. Filesystem is real (temp dirs); core.X9 and
core.LIBRARY_ROOTS are patched.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import pushes, server
from pipeline import core


def _enc(d, title, n):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{title} 2160p HEVC.mkv"), "wb") as f:
        f.write(b"x" * n)


class PushQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.x9 = os.path.join(self.tmp.name, "x9")
        self.comp = os.path.join(self.x9, "complete")
        os.makedirs(os.path.join(self.x9, "queue"))
        self.root = os.path.join(self.tmp.name, "Volumes", "Vhagar", "Media", "4K Movies")
        os.makedirs(self.root)
        self.patches = [
            mock.patch.object(core, "X9", self.x9),
            mock.patch.object(core, "LIBRARY_ROOTS", [self.root]),
            # volume_name() reads the real /Volumes/<NAS>/ layout; the sandbox
            # root lives under a temp dir.
            mock.patch.object(core, "volume_name", lambda p: "Vhagar"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_pending_is_largest_first_and_numbered(self):
        _enc(os.path.join(self.comp, "Small (2002)"), "Small (2002)", 10)
        _enc(os.path.join(self.comp, "Big (2001)"), "Big (2001)", 100)
        _enc(os.path.join(self.comp, "Mid (2003)"), "Mid (2003)", 50)
        p = pushes.pushes()
        self.assertEqual(
            [(p[k]["title"], p[k]["pos"]) for k in ("big (2001)", "mid (2003)", "small (2002)")],
            [("Big (2001)", 1), ("Mid (2003)", 2), ("Small (2002)", 3)],
        )
        self.assertTrue(all(v["state"] == "queued" and v["total"] == 3 for v in p.values()))

    def test_a_folder_in_flux_or_without_one_encode_is_not_pending(self):
        d = os.path.join(self.comp, "Landing (2005)")
        _enc(d, "Landing (2005)", 10)
        open(os.path.join(d, "x.partial"), "w").close()
        os.makedirs(os.path.join(self.comp, "NoEncode (2004)"))
        with open(os.path.join(self.comp, "NoEncode (2004)", "NoEncode (2004) Remux.mkv"), "wb") as f:
            f.write(b"x")
        self.assertEqual(pushes.pushes(), {})

    def test_on_the_wire_only_while_the_pid_answers(self):
        _enc(os.path.join(self.comp, "Big (2001)"), "Big (2001)", 100)
        _enc(os.path.join(self.comp, "Small (2002)"), "Small (2002)", 10)
        with open(os.path.join(self.x9, ".pushing-Big (2001)"), "w") as f:
            f.write(f"{os.getpid()}\nBig (2001)\n")
        p = pushes.pushes()
        self.assertEqual(p["big (2001)"]["state"], "pushing")
        # the one behind it is #1 of 1 pending now -- the title on the wire
        # is not counted among the waiting
        self.assertEqual(
            (p["small (2002)"]["state"], p["small (2002)"]["pos"], p["small (2002)"]["total"]),
            ("queued", 1, 1),
        )
        # a crashed push's marker names nothing
        with open(os.path.join(self.x9, ".pushing-Big (2001)"), "w") as f:
            f.write("999999\nBig (2001)\n")
        self.assertEqual(pushes.pushes()["big (2001)"]["state"], "queued")
        # pid 1 exists but is not ours to signal: that is ALIVE, not stale
        with open(os.path.join(self.x9, ".pushing-Big (2001)"), "w") as f:
            f.write("1\nBig (2001)\n")
        self.assertEqual(pushes.pushes()["big (2001)"]["state"], "pushing")

    def test_failed_carries_its_reason_and_leaves_the_numbering(self):
        _enc(os.path.join(self.comp, "Big (2001)"), "Big (2001)", 100)
        _enc(os.path.join(self.comp, "Small (2002)"), "Small (2002)", 10)
        with open(os.path.join(self.x9, ".push-failed-Big (2001)"), "w") as f:
            f.write("Big (2001): 0 library folders match, need exactly one at 2026-09-10 22:00:00\n")
        p = pushes.pushes()
        self.assertEqual(p["big (2001)"]["state"], "failed")
        self.assertIn("0 library folders", p["big (2001)"]["note"])
        self.assertEqual((p["small (2002)"]["pos"], p["small (2002)"]["total"]), (1, 1))

    def test_pushed_names_the_nas_and_bucket(self):
        dest = os.path.join(self.root, "B", "Big (2001)")
        with open(os.path.join(self.x9, ".pushed-Big (2001)"), "w") as f:
            f.write(f"Big (2001): pushed to {dest} at 2026-09-10 22:00:00\n")
        p = pushes.pushes()["big (2001)"]
        self.assertEqual((p["state"], p["nas"], p["bucket"], p["dest"]), ("pushed", "Vhagar", "B", dest))

    def test_a_title_with_at_and_colon_in_its_name_still_parses(self):
        dest = os.path.join(self.root, "L", "Look at: Me (2001)")
        with open(os.path.join(self.x9, ".pushed-Look at: Me (2001)"), "w") as f:
            f.write(f"Look at: Me (2001): pushed to {dest} at 2026-09-10 22:00:00\n")
        self.assertEqual(pushes.pushes()["look at: me (2001)"]["dest"], dest)

    def test_push_off_flag_and_hold_reason(self):
        self.assertFalse(pushes.push_off())
        open(os.path.join(self.x9, ".push-off"), "w").close()
        self.assertTrue(pushes.push_off())
        self.assertIsNone(pushes.push_hold())
        with open(os.path.join(self.x9, ".push-hold"), "w") as f:
            f.write("NAS unreachable: crivas@192.168.50.6")
        self.assertEqual(pushes.push_hold(), "NAS unreachable: crivas@192.168.50.6")

    def test_transfers_sees_a_push_leaving_from_complete(self):
        _enc(os.path.join(self.comp, "Big (2001)"), "Big (2001)", 1000)
        # only a title a LIVE .pushing- marker names is looked up (complete/
        # is unbounded; a lookup per folder per 2 s frame is an SMB storm)
        with open(os.path.join(self.x9, ".pushing-Big (2001)"), "w") as f:
            f.write(f"{os.getpid()}\nBig (2001)\n")
        os.makedirs(os.path.join(self.root, "B", "Big (2001)"))
        with open(os.path.join(self.root, "B", "Big (2001)", "Big (2001) 2160p HEVC.mkv.partial"), "wb") as f:
            f.write(b"x" * 250)
        server._xfer_track.clear()
        rows = [r for r in server._transfers() if r["title"] == "Big (2001)"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["done_bytes"], rows[0]["total_bytes"], rows[0]["nas"]), (250, 1000, "Vhagar"))
        os.remove(os.path.join(self.x9, ".pushing-Big (2001)"))
        server._xfer_track.clear()
        self.assertEqual([r for r in server._transfers() if r["title"] == "Big (2001)"], [])


if __name__ == "__main__":
    unittest.main()
