"""The decision path must never import the dashboard.

CLAUDE.md states this as a hand-verified fact -- "Verified: verdict.py does not
load server.py" -- and hand-verified facts about live infrastructure are the
ones that rot. The whole safety story rests on it: server.py now spawns
HandBrake, kills it, and queues NAS pulls, and its worst case is documented as
"the page breaks and the pipeline keeps running". That is only true while no
module the driver executes can drag server.py (or its sampler thread) into the
process.

Transitive, not direct: `record.py -> report.py -> server.py` would be just as
fatal as a direct import, and no eyeball check catches a two-hop path.
"""
import ast
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What `.autopilot.sh` executes on every cycle, via the `smeltr` launcher:
#   smeltr next    -> next_title.py     smeltr verdict -> verdict.py
#   smeltr record  -> record.py         (all three import core)
DECISION_PATH = ("core", "verdict", "next_title", "record")

# Importing any of these from the decision path is the failure. server.py
# spawns and kills encodes; sysmon.py starts a 1 Hz daemon thread and mmaps a
# ring buffer -- neither belongs in a process that decides on a deletion.
DASHBOARD_ONLY = frozenset({"server", "sysmon", "report", "seed_ledger"})


def local_modules() -> frozenset:
    return frozenset(f[:-3] for f in os.listdir(ROOT)
                     if f.endswith(".py") and not f.startswith("_"))


def imports_of(module: str) -> set:
    """Local modules `module` imports, at any nesting depth in the file."""
    path = os.path.join(ROOT, module + ".py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    local = local_modules()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & local


def closure(module: str) -> set:
    seen, stack = set(), [module]
    while stack:
        cur = stack.pop()
        for dep in imports_of(cur):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


class DecisionPathIsSelfContained(unittest.TestCase):
    def test_no_decision_path_module_reaches_the_dashboard(self):
        for module in DECISION_PATH:
            with self.subTest(module=module):
                reached = closure(module) & DASHBOARD_ONLY
                self.assertEqual(
                    reached, set(),
                    f"{module}.py transitively imports {sorted(reached)} — "
                    f"the driver would now load the dashboard to decide on a "
                    f"deletion. Path: {module} -> {sorted(closure(module))}")

    def test_the_dashboard_may_still_import_core(self):
        """The allowed direction, asserted so the rule reads as a DIRECTION
        rather than as 'these files never touch'. One pick, one verdict, one
        threshold table -- server.py is supposed to reuse core."""
        self.assertIn("core", closure("server"))

    def test_every_decision_path_module_exists(self):
        for module in DECISION_PATH:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, module + ".py")),
                            f"{module}.py is gone or moved — if the layout "
                            f"changed, update DECISION_PATH here too")


if __name__ == "__main__":
    unittest.main()
