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
#   smeltr record  -> record.py         smeltr crf     -> crf.py
#   smeltr encoder -> encoder_of.py
#   (all five import core)
DECISION_PATH = ("core", "verdict", "next_title", "record", "crf", "encoder_of")
PIPELINE_DIR = "pipeline"
DASHBOARD_DIR = "dashboard"

# Importing any of these from the decision path is the failure. server.py
# spawns and kills encodes; sysmon.py starts a 1 Hz daemon thread and mmaps a
# ring buffer; notify.py posts to Slack and Gmail over the network -- none of
# them belongs in a process that decides on a deletion.
DASHBOARD_ONLY = frozenset(
    {"server", "sysmon", "report", "events", "notify", "heartbeat", "auth",
     "manual", "procs", "brief", "pushes"}
)


def _modules_in(pkg: str) -> frozenset:
    return frozenset(
        f[:-3]
        for f in os.listdir(os.path.join(ROOT, pkg))
        if f.endswith(".py") and not f.startswith("__")
    )


def _path_of(module: str) -> str:
    for pkg in (PIPELINE_DIR, DASHBOARD_DIR):
        p = os.path.join(ROOT, pkg, module + ".py")
        if os.path.isfile(p):
            return p
    raise AssertionError(
        f"no module {module}.py in {PIPELINE_DIR}/ or {DASHBOARD_DIR}/"
    )


def imports_of(module: str) -> set:
    """Local modules `module` imports, at any nesting depth in the file.

    `from pipeline import core` and `from dashboard import sysmon` both land
    as the imported NAME, so the check reads the same either side of the
    package move.
    """
    with open(_path_of(module), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    local = _modules_in(PIPELINE_DIR) | _modules_in(DASHBOARD_DIR)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
            if node.module.split(".")[0] in (PIPELINE_DIR, DASHBOARD_DIR):
                found.update(a.name for a in node.names)
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
                    reached,
                    set(),
                    f"{module}.py transitively imports {sorted(reached)} — "
                    f"the driver would now load the dashboard to decide on a "
                    f"deletion. Path: {module} -> {sorted(closure(module))}",
                )

    def test_no_dashboard_module_lives_in_the_pipeline_package(self):
        """The folder IS the boundary now. A dashboard module filed under
        `pipeline/` reads as pause-before-editing when it is not."""
        self.assertEqual(_modules_in(PIPELINE_DIR), set(DECISION_PATH))

    def test_the_dashboard_may_still_import_core(self):
        """The allowed direction, asserted so the rule reads as a DIRECTION
        rather than as 'these files never touch'. One pick, one verdict, one
        threshold table -- server.py is supposed to reuse core."""
        self.assertIn("core", closure("server"))

    def test_every_decision_path_module_exists(self):
        for module in DECISION_PATH:
            self.assertTrue(
                os.path.isfile(os.path.join(ROOT, PIPELINE_DIR, module + ".py")),
                f"{module}.py is not in {PIPELINE_DIR}/ — the decision path "
                f"must stay together, and this list must follow it",
            )


if __name__ == "__main__":
    unittest.main()
