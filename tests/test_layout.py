"""The staging LAYOUT in core (operator's rule, 2026-09-07).

Movie folders live in $X9/queue (everything the pipeline may still act on)
and $X9/complete (finished, kept). Scripts, logs, markers and the index stay
at the root. A folder still at the root is the pre-layout drive and every
reader falls back to it, so a deploy mid-encode never loses the running
title. `complete/` is never staged, and the two layout folders are never
titles.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core  # noqa: E402


class Layout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._x9 = core.X9
        core.X9 = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, core, "X9", self._x9)

    def folder(self, parent, title, *files):
        d = os.path.join(parent, title)
        os.makedirs(d, exist_ok=True)
        for f in files:
            with open(os.path.join(d, f), "wb") as fh:
                fh.write(b"x" * 8)
        return d

    def test_layout_paths_follow_x9_at_call_time(self):
        # The suites swap core.X9; a path frozen at import would keep
        # pointing at the live drive.
        self.assertEqual(core.stage_dir(), os.path.join(core.X9, "queue"))
        self.assertEqual(core.complete_dir(), os.path.join(core.X9, "complete"))

    def test_staged_folders_reads_queue_and_the_legacy_root_never_complete(self):
        self.folder(core.stage_dir(), "Alpha (2001)", "Alpha (2001) Remux-2160p.mkv")
        self.folder(core.X9, "Beta (2002)", "Beta (2002) Remux-2160p.mkv")
        self.folder(core.complete_dir(), "Done (1999)", "Done (1999) 2160p HEVC.mkv")
        self.assertEqual(core.staged_folders(), ["Alpha (2001)", "Beta (2002)"])

    def test_the_layout_folders_are_never_titles(self):
        os.makedirs(core.stage_dir())
        os.makedirs(core.complete_dir())
        self.assertEqual(core.staged_folders(), [])
        self.assertEqual(core.staged_state("queue"), None)

    def test_folder_dir_prefers_queue_then_the_root_then_names_queue(self):
        q = self.folder(core.stage_dir(), "Alpha (2001)")
        r = self.folder(core.X9, "Beta (2002)")
        self.folder(core.stage_dir(), "Gamma (2003)")
        self.folder(core.X9, "Gamma (2003)")
        self.assertEqual(core.folder_dir("Alpha (2001)"), q)
        self.assertEqual(core.folder_dir("Beta (2002)"), r)
        self.assertEqual(core.folder_dir("Gamma (2003)"), os.path.join(core.stage_dir(), "Gamma (2003)"))
        self.assertEqual(core.folder_dir("New (2004)"), os.path.join(core.stage_dir(), "New (2004)"))
        # A finished title in complete/ resolves there as a LAST resort, so
        # `record`/`verdict` can be pointed at it by hand; it is still never staged.
        c = self.folder(core.complete_dir(), "Done (1999)")
        self.assertEqual(core.folder_dir("Done (1999)"), c)
        self.assertNotIn("Done (1999)", core.staged_folders())
        # `queue` and `complete` resolve INTO queue/, never to the layout folders.
        self.assertEqual(core.folder_dir("queue"), os.path.join(core.stage_dir(), "queue"))
        self.assertEqual(core.folder_dir("complete"), os.path.join(core.stage_dir(), "complete"))

    def test_staged_state_reads_the_folder_where_it_is(self):
        self.folder(core.stage_dir(), "Alpha (2001)", "Alpha (2001) Remux-2160p.mkv")
        self.folder(core.X9, "Beta (2002)", "Beta (2002) Remux-2160p.mkv", "Beta (2002) 2160p HEVC.mkv")
        self.assertEqual(core.staged_state("Alpha (2001)"), core.STAGE_READY)
        self.assertEqual(core.staged_state("Beta (2002)"), core.STAGE_OUTPUT)
        self.assertIsNone(core.staged_state("Nowhere (2009)"))

    def test_a_folder_in_complete_is_done_without_a_marker(self):
        self.folder(core.complete_dir(), "Done (1999)", "Done (1999) 2160p HEVC.mkv")
        self.assertIsNotNone(core.done_marker("Done (1999)"))
        self.assertIsNone(core.done_marker("Alpha (2001)"))

    def test_the_marker_still_wins_when_both_exist(self):
        self.folder(core.complete_dir(), "Done (1999)")
        with open(os.path.join(core.X9, ".done-Done (1999)"), "w", encoding="utf-8") as fh:
            fh.write("Done (1999): verdict good\n")
        self.assertEqual(core.done_marker("Done (1999)"), "Done (1999): verdict good")

    def test_find_in_staging_looks_inside_the_layout(self):
        self.folder(core.stage_dir(), "Alpha (2001)", "Alpha (2001) Remux-2160p.mkv")
        self.assertEqual(
            core._find_in_staging("Alpha (2001) Remux-2160p.mkv"),
            os.path.join(core.stage_dir(), "Alpha (2001)", "Alpha (2001) Remux-2160p.mkv"),
        )
        # A finished title in complete/ is not staging: never a size operand.
        self.folder(core.complete_dir(), "Done (1999)", "Done (1999) Remux-2160p.mkv")
        self.assertIsNone(core._find_in_staging("Done (1999) Remux-2160p.mkv"))


if __name__ == "__main__":
    unittest.main()
