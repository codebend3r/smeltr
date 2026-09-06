"""What `bun run currentp` may and may not say about a running process.

The load-bearing test here is the stale rule. This tool prints a `kill`
command, so a row wrongly marked stale is a suggestion to kill something
that is working -- and the two shapes that make that easy to get wrong are
both real: the driver runs detached at `ppid 1` by design, and the Claude
Code harness that a person would be running this FROM also sits at `ppid 1`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import current_processes as cp  # noqa: E402


def row(pid, ppid, etime, cmd):
    return {"pid": pid, "ppid": ppid, "etime": etime, "cmd": cmd}


DRIVER = "/bin/bash ./.autopilot.sh"
HANDBRAKE = (
    "HandBrakeCLI -i /Volumes/Crucial X9/4K Movies/Bloodsport (1988)/"
    "Bloodsport (1988) WEBDL-2160p.mkv -o /Volumes/Crucial X9/4K Movies/"
    "Bloodsport (1988)/Bloodsport (1988) 2160p HEVC.mkv -f av_mkv "
    "-e vt_h265_10bit -q 60 --all-audio --aencoder copy --all-subtitles"
)
SHELL = "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh && smeltr"
CLAUDE = "/opt/homebrew/Caskroom/claude-code@latest/2.1.0/claude daemon run --origin transient"


class HumanAge(unittest.TestCase):
    def test_two_units_at_most(self):
        self.assertEqual(cp.human_age(45), "45s")
        self.assertEqual(cp.human_age(60), "1m")
        self.assertEqual(cp.human_age(47 * 60 + 17), "47m")
        self.assertEqual(cp.human_age(2 * 3600 + 19 * 60), "2h 19m")
        self.assertEqual(cp.human_age(3 * 3600), "3h")
        self.assertEqual(cp.human_age(12 * 86400 + 20 * 3600), "12d 20h")
        self.assertEqual(cp.human_age(5 * 86400), "5d")

    def test_every_ps_elapsed_format(self):
        self.assertEqual(cp._etime_seconds("47:17"), 47 * 60 + 17)
        self.assertEqual(cp._etime_seconds("01:02:03"), 3723)
        self.assertEqual(cp._etime_seconds("12-20:32:02"), 12 * 86400 + 20 * 3600 + 32 * 60 + 2)

    def test_an_unparseable_elapsed_is_zero_not_a_crash(self):
        self.assertEqual(cp._etime_seconds("wat"), 0)


class StaleIsNarrow(unittest.TestCase):
    def _one(self, rows, pid):
        return next(p for p in cp.collect(rows) if p["pid"] == pid)

    def test_the_detached_driver_is_never_stale(self):
        """`ppid 1` IS the driver's normal state -- it is launched detached."""
        proc = self._one([row(100, 1, "53:00", DRIVER)], 100)
        self.assertEqual(proc["purpose"], "driver")
        self.assertFalse(proc["stale"])

    def test_a_detached_encode_is_never_stale(self):
        proc = self._one([row(101, 1, "53:00", HANDBRAKE)], 101)
        self.assertFalse(proc["stale"])
        self.assertIn("Bloodsport (1988)", proc["detail"])
        self.assertIn("vt_h265_10bit CQ 60", proc["detail"])

    def test_the_claude_harness_is_never_stale_even_orphaned(self):
        """It is what a person is running this from. Suggesting `kill` on it
        would end the session that asked the question."""
        proc = self._one([row(102, 1, "07:00", CLAUDE)], 102)
        self.assertFalse(proc["stale"])

    def test_an_unrecognised_process_is_never_stale(self):
        """Not recognising something is not evidence it is finished with."""
        proc = self._one([row(103, 1, "07:00", "/bin/bash /some/smeltr/thing.sh")], 103)
        self.assertEqual(proc["purpose"], "unrecognised")
        self.assertFalse(proc["stale"])

    def test_an_orphaned_session_shell_is_stale(self):
        proc = self._one([row(104, 1, "21:00", SHELL)], 104)
        self.assertEqual(proc["purpose"], "Claude Code shell")
        self.assertTrue(proc["stale"])

    def test_a_session_shell_with_a_live_parent_is_not_stale(self):
        rows = [row(200, 1, "07:00", CLAUDE), row(104, 200, "21:00", SHELL)]
        self.assertFalse(self._one(rows, 104)["stale"])

    def test_nothing_unrelated_is_listed_at_all(self):
        self.assertEqual(cp.collect([row(105, 1, "01:00", "/usr/sbin/cupsd -l")]), [])


class Noise(unittest.TestCase):
    def test_chrome_helpers_do_not_each_get_a_row(self):
        rows = [
            row(1, 1, "01:00", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                               "--headless=new --user-data-dir=/tmp/smeltr-live-abc"),
            row(2, 1, "01:00", "/Applications/Google Chrome Helper --type=renderer "
                               "--user-data-dir=/tmp/smeltr-live-abc"),
            row(3, 1, "01:00", "/Applications/Google Chrome Helper --type=gpu-process "
                               "--user-data-dir=/tmp/smeltr-live-abc"),
        ]
        self.assertEqual([p["pid"] for p in cp.collect(rows)], [1])

    def test_repeats_fold_only_outside_the_pipeline(self):
        rows = [
            row(1, 1, "07:00", CLAUDE),
            row(2, 1, "09:00", CLAUDE),
            row(3, 1, "53:00", DRIVER),
            row(4, 1, "53:00", DRIVER),
        ]
        procs = cp.collect(rows)
        harness = [p for p in procs if p["purpose"] == "Claude Code daemon"]
        drivers = [p for p in procs if p["purpose"] == "driver"]
        self.assertEqual(len(harness), 1)
        self.assertEqual(harness[0]["count"], 2)
        self.assertEqual(harness[0]["seconds"], 9 * 60, "the fold keeps the OLDEST age")
        self.assertEqual(len(drivers), 2, "every pipeline process is its own fact")


class Rendering(unittest.TestCase):
    def test_an_empty_machine_says_so_rather_than_printing_a_bare_header(self):
        self.assertIn("Nothing from this repo is running", cp.render([]))

    def test_the_kill_line_lists_only_stale_pids(self):
        rows = [row(1, 1, "53:00", DRIVER), row(2, 1, "21:00", SHELL)]
        out = cp.render(cp.collect(rows))
        self.assertIn("kill 2", out)
        self.assertNotIn("kill 1", out)


if __name__ == "__main__":
    unittest.main()


class CommitEditor(unittest.TestCase):
    """`code -w` staleness is read off git's marker file, not off the parent.

    The parent says nothing useful here, and the first version of this tool
    asserted "git is waiting on this to close" about a commit that had landed
    a day earlier. The marker is the actual evidence: git deletes it when the
    commit completes.
    """

    CODE = "/Applications/Visual Studio Code.app/Contents/MacOS/Code -w %s/.git/COMMIT_EDITMSG"

    def test_a_live_marker_means_git_really_is_waiting(self):
        import tempfile

        with tempfile.NamedTemporaryFile() as marker:
            cmd = self.CODE % "/repo" + " --waitMarkerFilePath " + marker.name
            proc = cp.collect([row(1, 1, "01:00", cmd)])[0]
            self.assertFalse(proc["stale"])
            self.assertIn("git is waiting", proc["detail"])

    def test_a_vanished_marker_means_the_commit_already_landed(self):
        cmd = self.CODE % "/repo" + " --waitMarkerFilePath /nonexistent/marker-xyz"
        proc = cp.collect([row(1, 1, "01:00", cmd)])[0]
        self.assertTrue(proc["stale"])
        self.assertIn("leftover", proc["detail"])

    def test_no_marker_at_all_is_unknown_and_never_stale(self):
        proc = cp.collect([row(1, 1, "01:00", self.CODE % "/repo")])[0]
        self.assertFalse(proc["stale"], "unknown must never license a kill")
