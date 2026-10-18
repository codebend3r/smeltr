"""dashboard/flags.py -- the one marker-file primitive the dashboard shares."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.flags import Flag


class FlagFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "flag")

    def test_set_and_clear_are_idempotent(self):
        f = Flag(self.path)
        self.assertFalse(f.is_set())
        f.set()
        f.set(True)
        self.assertTrue(f.is_set())
        f.set(False)
        f.set(False)
        self.assertFalse(f.is_set())

    def test_a_callable_path_is_resolved_at_call_time(self):
        where = {"p": self.path}
        f = Flag(lambda: where["p"])
        f.set()
        self.assertTrue(os.path.exists(self.path))
        where["p"] = os.path.join(self.tmp.name, "elsewhere")
        self.assertFalse(f.is_set())

    def test_note_is_the_first_line_or_none(self):
        f = Flag(self.path)
        self.assertIsNone(f.note())
        f.set()
        self.assertEqual(f.note(), "")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("  library root not mounted \nsecond line\n")
        self.assertEqual(f.note(), "library root not mounted")

    def test_set_raises_when_the_write_cannot_happen(self):
        with self.assertRaises(OSError):
            Flag(os.path.join(self.tmp.name, "no-such-dir", "flag")).set()


if __name__ == "__main__":
    unittest.main()
