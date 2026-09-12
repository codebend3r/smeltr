"""Every pipeline spawn carries Homebrew on PATH.

2026-09-11: launchd restarted the driver with PATH=/usr/bin:/bin:/usr/sbin:/sbin.
HandBrakeCLI is Homebrew's, so every start died with "nohup: HandBrakeCLI: No
such file or directory" and the dashboard showed the title as next up for six
minutes after a play click. Three launchers can spawn the driver or HandBrake
-- the watchdog plist, the watchdog script, and server.py -- and each must put
/opt/homebrew/bin on PATH itself rather than trust its parent.
"""
import os
import plistlib
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))

HOMEBREW = "/opt/homebrew/bin"


class SpawnPath(unittest.TestCase):
    def test_every_launchd_plist_sets_a_homebrew_path(self):
        ops = os.path.join(ROOT, "ops")
        # The jobs that spawn the driver, HandBrake, or ffprobe. The brief
        # sends one email and needs no Homebrew tool.
        for name in (
            "com.smeltr.watchdog.plist",
            "com.smeltr.push.plist",
            "com.smeltr.replenish.plist",
            "com.smeltr.scan.plist",
        ):
            with open(os.path.join(ops, name), "rb") as fh:
                data = plistlib.load(fh)
            path = data.get("EnvironmentVariables", {}).get("PATH", "")
            self.assertTrue(
                path.startswith(HOMEBREW + ":"),
                "%s: launchd PATH lacks Homebrew (%r)" % (name, path),
            )

    def test_watchdog_script_exports_homebrew_before_relaunching(self):
        with open(os.path.join(ROOT, "ops", "watchdog.sh")) as fh:
            src = fh.read()
        export_at = src.find("export PATH")
        relaunch_at = src.find("nohup ./.autopilot.sh")
        self.assertGreater(export_at, 0, "watchdog.sh never exports PATH")
        self.assertGreater(relaunch_at, export_at, "PATH must be set before the relaunch")
        self.assertIn(HOMEBREW, src[:relaunch_at])

    def test_tool_env_prepends_homebrew_to_a_bare_path(self):
        import server  # noqa: E402

        saved = os.environ.get("PATH")
        try:
            os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            env = server._tool_env()
            self.assertEqual(env["PATH"].split(":")[0], HOMEBREW)
            self.assertIn("/usr/bin", env["PATH"].split(":"))
            os.environ["PATH"] = HOMEBREW + ":/usr/bin:/bin"
            self.assertEqual(server._tool_env()["PATH"], HOMEBREW + ":/usr/bin:/bin")
        finally:
            os.environ["PATH"] = saved

    def test_server_spawns_driver_handbrake_and_watcher_with_tool_env(self):
        with open(os.path.join(ROOT, "dashboard", "server.py")) as fh:
            src = fh.read()
        for marker in ('["./.autopilot.sh"]', '"HandBrakeCLI",', '".watch-encode.sh")'):
            at = src.find(marker)
            self.assertGreater(at, 0, marker)
            window = src[at : at + 900]
            self.assertRegex(window, re.compile(r"env=_tool_env\(\)"), "%s spawn lacks env=_tool_env()" % marker)


if __name__ == "__main__":
    unittest.main()
