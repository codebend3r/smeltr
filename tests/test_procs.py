"""dashboard/procs.py -- the Processes tab's classifier, against fake ps rows.

Pins: every rule carries a purpose sentence; a driver copy with a replenish
child is a SUBSHELL, not a second driver; two real drivers are flagged, not
hidden; the title a pull/encode works on is the staging destination, never
the library source; shell wrappers that merely mention a script are not
ours; the agent list is sorted and every known label has a purpose.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SMELTR_PROCS_NOCACHE"] = "1"

from dashboard import procs  # noqa: E402

X9 = "/Volumes/Crucial X9/4K Movies"


def row(pid, ppid, cmd, etime="01:00", cpu="0.0", rss="1000"):
    return (pid, ppid, etime, cpu, rss, cmd)


class Rules(unittest.TestCase):
    def test_every_rule_has_a_purpose_sentence(self):
        for _, kind, label, purpose in procs.RULES:
            self.assertTrue(kind and label, kind)
            self.assertGreater(len(purpose), 30, kind)
            self.assertTrue(purpose.endswith("."), kind)

    def test_every_known_agent_has_a_purpose(self):
        for label, purpose in procs.AGENTS.items():
            self.assertTrue(label.startswith("com.smeltr."), label)
            self.assertGreater(len(purpose), 20, label)


class Classify(unittest.TestCase):
    def test_handbrake_is_the_encode_with_its_title(self):
        s = procs.snapshot(rows=[row(5, 1, "HandBrakeCLI -i %s/queue/Alpha (2001)/Alpha (2001) Remux-2160p.mkv -o %s/queue/Alpha (2001)/Alpha (2001) 2160p HEVC.mkv" % (X9, X9))], agents=[])
        self.assertEqual([(p["kind"], p["detail"]) for p in s["procs"]], [("encode", "Alpha (2001)")])

    def test_a_pull_names_the_staging_destination_not_the_library_source(self):
        cmd = "/bin/bash %s/.ssh-xfer.sh pull /Volumes/Vhagar/Media/4K Movies/B/Beta (2002)/Beta (2002) WEBDL-2160p.mkv %s/queue/Beta (2002)" % (X9, X9)
        s = procs.snapshot(rows=[row(7, 1, cmd)], agents=[])
        self.assertEqual(s["procs"][0]["kind"], "pull")
        self.assertEqual(s["procs"][0]["detail"], "Beta (2002)")

    def test_a_driver_copy_with_a_replenish_child_is_a_subshell(self):
        rows = [
            row(10, 1, "/bin/bash ./.autopilot.sh"),
            row(20, 1, "/bin/bash ./.autopilot.sh"),
            row(21, 20, "bash %s/.replenish-queue.sh" % X9),
            row(22, 20, "/bin/bash ./.autopilot.sh"),
        ]
        s = procs.snapshot(rows=rows, agents=[])
        kinds = {p["pid"]: p["kind"] for p in s["procs"]}
        self.assertEqual(kinds[10], "driver")
        self.assertEqual(kinds[20], "driver-sub")
        self.assertEqual(kinds[22], "driver-sub")
        self.assertEqual(kinds[21], "replenish")
        self.assertFalse(s["procs"][0]["dup"])

    def test_two_real_drivers_are_flagged_not_hidden(self):
        s = procs.snapshot(rows=[row(10, 1, "/bin/bash ./.autopilot.sh"), row(11, 1, "/bin/bash ./.autopilot.sh")], agents=[])
        self.assertEqual([p["dup"] for p in s["procs"]], [True, True])

    def test_a_shell_that_merely_mentions_a_script_is_not_ours(self):
        for cmd in (
            "grep -n autopilot.sh /tmp/x",
            "tail -f %s/.autopilot.log" % X9,
            "pgrep -f autopilot.sh",
        ):
            self.assertIsNone(procs.classify(cmd), cmd)

    def test_unrelated_processes_are_not_listed(self):
        s = procs.snapshot(rows=[row(1, 0, "/sbin/launchd"), row(2, 1, "/usr/bin/python3 something_else.py")], agents=[])
        self.assertEqual(s["procs"], [])

    def test_order_is_encode_first_then_the_machinery(self):
        rows = [
            row(30, 1, "/usr/bin/caffeinate -dimsu"),
            row(31, 1, "/bin/bash %s/ops/watchdog.sh --supervise" % os.path.expanduser("~/Developer/git/smeltr")),
            row(32, 1, "/bin/bash ./.autopilot.sh"),
            row(33, 32, "HandBrakeCLI -i a -o %s/queue/Gamma (2003)/Gamma (2003) 2160p HEVC.mkv" % X9),
        ]
        s = procs.snapshot(rows=rows, agents=[])
        self.assertEqual([p["kind"] for p in s["procs"]], ["encode", "driver", "watchdog", "caffeinate"])

    def test_a_long_command_is_truncated_for_the_tooltip(self):
        s = procs.snapshot(rows=[row(40, 1, "HandBrakeCLI " + "x" * 400)], agents=[])
        self.assertLessEqual(len(s["procs"][0]["cmd"]), 160)
        self.assertTrue(s["procs"][0]["cmd"].endswith("..."))

    def test_the_watcher_names_its_title_from_the_bare_argument(self):
        cmd = "/bin/bash %s/.watch-encode.sh unbearableweight Unbearable Weight of Massive Talent, The (2022) Unbearable Weight of Massive Talent, The (2022) WEBDL-2160p.mkv Unbearable Weight of Massive Talent, The (2022) 2160p HEVC.mkv 60660 75 vt_h265_10bit" % X9
        s = procs.snapshot(rows=[row(50, 1, cmd)], agents=[])
        self.assertEqual(s["procs"][0]["kind"], "watcher")
        self.assertEqual(s["procs"][0]["detail"], "Unbearable Weight of Massive Talent, The (2022)")

    def test_a_fork_of_the_same_command_is_one_process(self):
        rep = "/bin/bash %s/ops/replenisher.sh" % os.path.expanduser("~/Developer/git/smeltr")
        s = procs.snapshot(rows=[row(60, 1, rep), row(61, 60, rep)], agents=[])
        self.assertEqual([p["pid"] for p in s["procs"]], [60])

    def test_agents_pass_through_with_purpose(self):
        agents = [{"label": "com.smeltr.watchdog", "pid": None, "status": "0", "purpose": "x"}]
        s = procs.snapshot(rows=[], agents=agents)
        self.assertEqual(s["agents"], agents)


if __name__ == "__main__":
    unittest.main()
