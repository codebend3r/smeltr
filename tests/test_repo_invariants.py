"""The rules CLAUDE.md calls non-negotiable, enforced instead of remembered.

Everything here was previously a sentence in a document and nothing else. Each
test names the specific failure it is standing in front of, because every one
of them is silent: the pipeline keeps running, the page keeps rendering, and
the damage is only visible later -- in a synced file missing its commentary
track, in a driver halted by a path the dashboard could not resolve, or in an
auth token pushed to a public remote.

Nothing here touches the live pipeline. It reads tracked files only.
"""

import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core  # noqa: E402
from dashboard import server  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _git(*args):
    out = subprocess.run(
        ("git",) + args, cwd=REPO, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def tracked():
    """What is IN THE INDEX. The set that a push would publish."""
    return _git("ls-files")


def committable():
    """Index plus new files git would happily add -- minus anything ignored.

    The content scans below have to see a file the moment it lands, not only
    after someone remembers to `git add` it; a new .sh that drops --all-audio
    is exactly as dangerous before it is staged as after.
    """
    return _git("ls-files", "--cached", "--others", "--exclude-standard")


def ignored(rel):
    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO)
    return r.returncode == 0


def shell_scripts():
    return [p for p in committable() if p.endswith(".sh")] + ["smeltr"]


# ------------------------------------------------------------------ encoding

# CLAUDE.md, "Non-negotiables in the domain": every audio and subtitle track is
# preserved. This is the whole point of the job -- a 30 GB output that dropped
# the commentary and the forced subs is not a smaller copy of the original, it
# is a lossy one, and .sync-to-library.sh then DELETES the ~90 GB original that
# still had them.
TRACK_FLAGS = (
    "--all-audio",
    "--aencoder",
    "copy",
    "--audio-fallback",
    "ac3",
    "--all-subtitles",
)


class TrackPreservation(unittest.TestCase):
    """The flags that keep every audio and subtitle track."""

    ENCODERS = ("staging/autopilot.sh", "dashboard/server.py")

    def test_every_encoder_passes_the_track_flags(self):
        for rel in self.ENCODERS:
            src = read(rel)
            self.assertTrue("HandBrakeCLI" in src, f"{rel} no longer starts an encode?")
            for flag in TRACK_FLAGS:
                # assertTrue, not assertIn: assertIn prints the whole haystack,
                # and these haystacks are 3,700-line files.
                self.assertTrue(
                    flag in src,
                    f"{rel} lost {flag} -- an encode from here would "
                    f"drop tracks and the original still gets deleted",
                )

    def test_no_track_selection_anywhere(self):
        """`--audio-lang-list` selects a subset. It must never appear."""
        for rel in committable():
            if not rel.endswith((".sh", ".py")) and rel != "smeltr":
                continue
            if rel.startswith("tests/"):
                continue
            self.assertTrue(
                "--audio-lang-list" not in read(rel),
                f"{rel} selects a subset of audio tracks",
            )


class EncodeFlagParity(unittest.TestCase):
    """dashboard/server.py and autopilot.sh are two implementations of ONE procedure.

    CLAUDE.md: "The encode-control block in server.py deliberately mirrors
    .autopilot.sh start_encode() -- same flags [...] a change to either must be
    mirrored in the other." Nothing enforced that, so a CRF or preset changed in
    one place would quietly produce two different encodes depending on whether
    the driver or the dashboard started it -- and only one of them is the one
    the ledger's history was calibrated against.
    """

    # Flags that decide what the output IS. Deliberately excludes -i/-o/-q,
    # which are per-title by nature.
    SHAPE = (
        "-f",
        "av_mkv",
        "-e",
        "x265_10bit",
        "--encoder-preset",
        "medium",
    ) + TRACK_FLAGS

    def test_both_build_the_same_encode(self):
        for rel in ("staging/autopilot.sh", "dashboard/server.py"):
            src = read(rel)
            for flag in self.SHAPE:
                self.assertTrue(flag in src, f"{rel} is missing {flag}")

    def test_crf_menu_is_the_ladder(self):
        """10..22 even -- the menu IS .watch-encode.sh's ladder, exactly.

        The old menu carried 24, which no ladder map reaches: a blowup there
        was auto-killed and never restarted, a dead end wearing the costume of
        a choice. Every rung offered here must be one the watcher can step
        from, or the picker hands the operator a trap.
        """
        self.assertEqual(core.CRF_CHOICES, (10, 12, 14, 16, 18, 20, 22))
        self.assertEqual(server.CRF_CHOICES, core.CRF_CHOICES)
        self.assertIn(core.CRF_DEFAULT, core.CRF_CHOICES)
        watch = read("staging/watch-encode.sh")
        for rung in core.CRF_CHOICES:
            self.assertIn(
                str(rung),
                watch,
                f"CRF {rung} is on the menu but .watch-encode.sh's "
                f"ladder never mentions it",
            )

    def test_the_driver_asks_for_the_planned_crf(self):
        """A picker the driver ignores is a lie on every row it starts.

        The dashboard reaches HandBrake for at most one encode; .autopilot.sh
        starts all the others. It must consult `smeltr crf` for the start rung
        rather than hard-coding 16, or a CRF chosen in the queue applies only
        to the one title the operator happens to launch by hand.
        """
        ap = read("staging/autopilot.sh")
        self.assertIn('"$SMELTR" crf "$title"', ap)
        self.assertIn('crf) shift; exec "$PY" "$DIR/pipeline/crf.py"', read("smeltr"))


# ------------------------------------------------------------- library roots


class LibraryRoots(unittest.TestCase):
    """The root list is duplicated by hand in four places; two are in this repo.

    CLAUDE.md: "nothing enforces agreement -- a root added to three of four
    fails in whichever path was missed". `4K Family Movies` was added on
    2026-08-21 and that is exactly the shape of the bug: the queue would show a
    title the driver's library_path_of() could not resolve, and the driver halts
    after a multi-hour encode.
    """

    def test_autopilot_resolves_every_root_core_queues(self):
        ap = read("staging/autopilot.sh")
        for root in core.LIBRARY_ROOTS:
            self.assertTrue(
                root in ap,
                f"core.LIBRARY_ROOTS has {root}; staging/autopilot.sh "
                f"cannot resolve a library original under it",
            )

    def test_core_queues_every_root_autopilot_resolves(self):
        ap = read("staging/autopilot.sh")
        # Media/ scopes this to the NAS library roots. The staging drive
        # (/Volumes/Crucial X9/4K Movies) is core.X9, not a library root.
        for root in re.findall(r'"(/Volumes/[^"]*/Media/4K[^"]*Movies)"', ap):
            self.assertIn(
                root,
                core.LIBRARY_ROOTS,
                f"staging/autopilot.sh reads {root}; core.py never "
                f"queues or offline-checks it",
            )


# -------------------------------------------------------------- decision path


class DecisionPathIsolation(unittest.TestCase):
    """The four files the driver calls must not depend on the dashboard.

    CLAUDE.md: "Verified: verdict.py does not load server.py. Worst case the
    page breaks and the pipeline keeps running." That guarantee is one import
    away from being false -- server.py binds sockets, spawns threads and starts
    the sysmon sampler at import, so pulling it into verdict.py would make a
    port conflict able to halt an encode's verdict.
    """

    DECISION_PATH = (
        "pipeline/core.py",
        "pipeline/verdict.py",
        "pipeline/next_title.py",
        "pipeline/record.py",
    )
    FORBIDDEN = (
        "server",
        "sysmon",
        "report",
        "dashboard",
        "dashboard.server",
        "dashboard.sysmon",
        "dashboard.report",
        "events",
        "notify",
        "dashboard.events",
        "dashboard.notify",
    )

    def test_no_dashboard_import(self):
        for rel in self.DECISION_PATH:
            for line in read(rel).splitlines():
                m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][\w.]*)", line)
                if not m:
                    continue
                self.assertNotIn(
                    m.group(1),
                    self.FORBIDDEN,
                    f"{rel} imports {m.group(1)} -- the decision "
                    f"path can now be broken by the dashboard",
                )


# ------------------------------------------------------------------- secrets


class NothingSecretIsTracked(unittest.TestCase):
    """server.log and url carry the auth token; `token` IS the token.

    The remote is public. A single `git add -A` on a machine whose .gitignore
    got clobbered publishes a token that grants write access to the dashboard --
    which starts encodes, aborts them, and un-skips titles (re-arming a
    deletion).
    """

    NEVER = (
        "token",
        "url",
        "server.pid",
        "server.log",
        "sysmon.ring",
        "ledger.jsonl",
        "queue_overrides.json",
        "pause",
        "notify.json",
        "notify.cursor",
    )

    def test_runtime_artifacts_are_untracked(self):
        files = set(tracked())
        for name in self.NEVER:
            self.assertNotIn(name, files, f"{name} is tracked and must not be")

    def test_gitignore_still_covers_them(self):
        ignored = read(".gitignore")
        for name in self.NEVER:
            self.assertTrue(name in ignored, f".gitignore no longer names {name}")

    def test_no_worktree_is_tracked(self):
        """Each worktree is a full second copy carrying its own live token."""
        for path in tracked():
            self.assertFalse(path.startswith(".claude/worktrees/"), path)


# --------------------------------------------------------------------- shell


class ShellScriptsParse(unittest.TestCase):
    """`bash -n` on every tracked script.

    Cheap, and it catches the one edit this repo is structurally exposed to:
    bash reads a script by byte offset, so editing one while an instance runs
    can garble the remaining commands of the running copy -- including its
    deletion steps.
    """

    def test_every_script_is_syntactically_valid(self):
        for rel in shell_scripts():
            with self.subTest(script=rel):
                r = subprocess.run(
                    ["bash", "-n", os.path.join(REPO, rel)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr.strip()}")


HOOKS = (
    ".husky/pre-commit",
    ".husky/commit-msg",
    ".husky/pre-push",
    ".husky/commit-rules.sh",
)


# ----------------------------------------------------------------- runner


class BunIsTheOnlyRunner(unittest.TestCase):
    """bun is the JS runtime, the package manager and the task runner.

    npm was removed as the task runner on 2026-09-05. The failure this pins is
    a quiet one: a script, hook or CI step that says `npm run` or `node` still
    WORKS on a machine that happens to have both installed, and only fails on
    the one that does not -- a fresh clone, or a runner. Every entry point must
    spell bun so the requirement is one requirement.
    """

    NPM_ISH = re.compile(r"\b(npm|npx|yarn|pnpm|corepack)\b")
    NODE_CALL = re.compile(r"(^|[\s;&|(`'\"])node(\s|$)")

    def scripts(self):
        return json.loads(read("package.json"))["scripts"]

    def test_no_script_invokes_npm_or_node(self):
        for name, cmd in self.scripts().items():
            with self.subTest(script=name):
                # The preinstall guard NAMES npm/yarn/pnpm in its refusal
                # message; it is the one script allowed to.
                if name != "preinstall":
                    self.assertIsNone(self.NPM_ISH.search(cmd), cmd)
                self.assertIsNone(self.NODE_CALL.search(cmd), cmd)

    def test_umbrella_scripts_use_buns_own_sequencer(self):
        for name in (
            "lint",
            "test",
            "verify",
            "system-check",
            "format",
            "format:check",
        ):
            with self.subTest(script=name):
                self.assertTrue(
                    self.scripts()[name].startswith("bun run --sequential"),
                    self.scripts()[name],
                )

    def test_every_js_suite_runs_under_bun(self):
        js = {k: v for k, v in self.scripts().items() if k.startswith("test:js:")}
        self.assertTrue(js)
        for name, cmd in js.items():
            with self.subTest(script=name):
                self.assertTrue(cmd.startswith("bun tests/"), cmd)
        self.assertTrue(self.scripts()["visual"].startswith("bun tests/"))

    def test_preinstall_turns_other_package_managers_away(self):
        guard = self.scripts()["preinstall"]
        self.assertIn("npm_config_user_agent", guard)
        self.assertIn("bun/*)", guard)
        self.assertIn("exit 1", guard)

    def test_release_carries_the_commit_subject(self):
        # bun pm version ignores .npmrc's `message`; without -m the release
        # commit is a bare `v1.2.3`, which breaks the SMLTR: subject rule.
        self.assertEqual(
            self.scripts()["release"], 'bun pm version -m "SMLTR: Release v%s"'
        )
        self.assertEqual(self.scripts()["preversion"], "bun run verify")

    def test_bun_is_the_pinned_package_manager(self):
        pm = json.loads(read("package.json")).get("packageManager", "")
        self.assertRegex(pm, r"^bun@\d+\.\d+\.\d+$")
        self.assertNotIn(
            "npm-run-all2", json.loads(read("package.json")).get("devDependencies", {})
        )

    def test_only_the_bun_lockfile_is_tracked(self):
        files = set(tracked())
        self.assertIn("bun.lock", files)
        for stray in (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            ".npmrc",
        ):
            self.assertNotIn(stray, files)

    def test_hooks_and_ci_spell_bun(self):
        for rel in HOOKS + (
            ".github/workflows/ci.yml",
            ".github/workflows/pr.yml",
            ".github/dependabot.yml",
        ):
            with self.subTest(file=rel):
                for n, line in enumerate(read(rel).splitlines(), 1):
                    code = line.split("#", 1)[0]
                    self.assertIsNone(self.NPM_ISH.search(code), f"{rel}:{n}: {line}")
                    self.assertIsNone(self.NODE_CALL.search(code), f"{rel}:{n}: {line}")
        self.assertNotIn("setup-node", read(".github/workflows/ci.yml"))
        # No `bun` ecosystem either, for now: Dependabot's bun updater cannot
        # parse the lockfileVersion 2 that bun 1.4 writes, and the entry made
        # every update run fail (dependabot/dependabot-core#16026). The guard
        # above already refuses `npm`; this pins that the reason is on record.
        self.assertIn("dependabot-core#16026", read(".github/dependabot.yml"))


# ------------------------------------------------------------------- hooks


class HooksCoverTheGaps(unittest.TestCase):
    """The 2026-09-05 hook audit, pinned so it cannot quietly un-happen.

    Each of these is a check that was found MISSING: a commit-msg hook (pr.yml
    only sees a PR; `bun run release` pushes straight to main), actionlint
    outside CI, a rename onto `token` slipping past `--diff-filter=AM`, a
    hand-written shellcheck file list CI did not share, staging scripts that
    CI blocked on and the hook did not, and nothing at all enforcing
    .editorconfig on Python and shell.
    """

    def scripts(self):
        return json.loads(read("package.json"))["scripts"]

    def test_all_four_hook_files_are_committable(self):
        files = set(committable())
        for rel in HOOKS:
            self.assertIn(rel, files)

    def test_pre_commit_names_every_runtime_artifact(self):
        hook = read(".husky/pre-commit")
        m = re.search(r"grep -Ex '([^']+)'", hook)
        self.assertIsNotNone(m, "the artifact grep is gone from pre-commit")
        names = set(m.group(1).split("|"))
        for name in NothingSecretIsTracked.NEVER:
            self.assertIn(name.replace(".", r"\."), names, name)
        self.assertIn(r"\.claude/worktrees/.*", names)

    def test_pre_commit_catches_a_rename_onto_an_artifact(self):
        hook = read(".husky/pre-commit")
        self.assertIn("--diff-filter=d", hook)
        self.assertNotIn("--diff-filter=AM", hook)

    def test_pre_commit_runs_diff_check_and_the_mirrors_opt_out(self):
        self.assertIn("git diff --cached --check", read(".husky/pre-commit"))
        attrs = read(".gitattributes")
        self.assertRegex(attrs, r"(?m)^staging/\*\*\s+-whitespace$")
        self.assertRegex(attrs, r"(?m)^tests/fixtures/\*\*\s+-whitespace$")

    def test_commit_msg_and_pre_push_share_one_rules_file(self):
        for rel in (".husky/commit-msg", ".husky/pre-push"):
            with self.subTest(hook=rel):
                self.assertIn(". ./.husky/commit-rules.sh", read(rel))
                self.assertIn("check_message", read(rel))
        # pre-push judges only what no remote has, and refuses a fixup!.
        push = read(".husky/pre-push")
        self.assertIn("--not --remotes", push)
        self.assertIn("--no-merges", push)
        self.assertNotIn("allow_fixup", push)
        self.assertIn("allow_fixup", read(".husky/commit-msg"))

    def test_lint_runs_actionlint_and_the_hooks_are_shellchecked(self):
        s = self.scripts()
        self.assertEqual(s["lint:ci"], "actionlint")
        self.assertIn("lint:ci", s["lint"].split())
        self.assertIn(".husky/[a-z]*", s["lint:sh"])
        self.assertIn("git ls-files -co --exclude-standard '*.sh'", s["lint:sh"])
        self.assertIn(":!staging/*", s["lint:sh"])
        self.assertTrue(
            s["lint:sh:staging"].startswith("shellcheck -x -S error staging/*.sh &&"),
            s["lint:sh:staging"],
        )

    def test_ci_installs_the_pinned_actionlint_for_bun_run_lint(self):
        ci = read(".github/workflows/ci.yml")
        self.assertRegex(
            ci, r"go install github\.com/rhysd/actionlint/cmd/actionlint@v\d+\.\d+\.\d+"
        )
        self.assertNotIn("rhysd/actionlint@sha256", ci)


class CiRunsThroughBun(unittest.TestCase):
    """Every check in ci.yml is `bun run <script>`, one script per step.

    A step that ran `shellcheck ...` or `python -m unittest ...` by hand was a
    SECOND rendition of the gate the hooks run, and two renditions drift the
    moment one is edited alone -- test_crf_picker.js was in the local runner
    for four days before CI ran it. So: every `run:` in a checking job is
    `bun run <script>` naming a script that exists, every member of `lint`
    and every leaf `test:*` script is a step somewhere, and the only commands
    called directly are the ones that INSTALL a tool the scripts need.
    """

    SETUP = (
        "bun install --frozen-lockfile --ignore-scripts",
        "pip install --quiet uv",
        "brew install ffmpeg",
        "go install github.com/rhysd/actionlint/cmd/actionlint@",
    )

    def scripts(self):
        return json.loads(read("package.json"))["scripts"]

    def runs(self):
        """(job id, run text) for every `run:` step, block runs joined."""
        out, job, lines = [], None, read(".github/workflows/ci.yml").splitlines()
        i = lines.index("jobs:")  # `defaults: run:` above it is not a job
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^  ([a-z-]+):\s*$", line)
            if m:
                job = m.group(1)
            m = re.match(r"^(\s+)run:\s*(.*)$", line)
            if m and job:
                indent, text = m.group(1), m.group(2).strip()
                if text == "|":
                    block = []
                    i += 1
                    while i < len(lines) and (
                        not lines[i].strip() or lines[i].startswith(indent + " ")
                    ):
                        block.append(lines[i].strip())
                        i += 1
                    text = "\n".join(b for b in block if b and not b.startswith("#"))
                    out.append((job, text))
                    continue
                out.append((job, text))
            i += 1
        self.assertTrue(out)
        return out

    def bun_run_steps(self):
        return {
            r.split()[2]
            for j, r in self.runs()
            if j != "ci" and r.startswith("bun run ")
        }

    def test_every_checking_step_is_bun_run_or_a_tool_install(self):
        scripts = self.scripts()
        for job, run in self.runs():
            if job == "ci":  # the verdict loop over needs.*.result
                continue
            with self.subTest(job=job, run=run.splitlines()[0]):
                if run.startswith("bun run "):
                    parts = run.split()
                    self.assertEqual(len(parts), 3, run)
                    self.assertIn(parts[2], scripts, "no such package.json script")
                else:
                    self.assertTrue(
                        any(run.startswith(s) for s in self.SETUP),
                        f"called directly, not through bun: {run}",
                    )

    def test_every_member_of_lint_is_its_own_step(self):
        members = self.scripts()["lint"].split()[3:]  # after `bun run --sequential`
        self.assertGreater(len(members), 5)
        steps = self.bun_run_steps()
        for name in members:
            with self.subTest(script=name):
                self.assertIn(name, steps)
        self.assertNotIn("lint", steps, "unrolled: never one `bun run lint` step")

    def test_every_leaf_test_script_is_its_own_step(self):
        leaves = [k for k in self.scripts() if k.startswith("test:")]
        self.assertIn("test:py", leaves)
        steps = self.bun_run_steps()
        for name in leaves:
            with self.subTest(script=name):
                self.assertIn(name, steps)
        self.assertNotIn("test", steps, "unrolled: never one `bun run test` step")

    def test_smoke_is_unrolled_too(self):
        members = self.scripts()["smoke"].split()[3:]
        self.assertEqual(members, ["smoke:report", "smoke:next", "build"])
        steps = self.bun_run_steps()
        for name in members:
            with self.subTest(script=name):
                self.assertIn(name, steps)
        # smoke_offline.sh is what those two scripts run, and it is NOT a
        # `test:sh:*` suite: on the Mac the roots are mounted and its
        # assertions are false by construction.
        self.assertIn("tests/smoke_offline.sh", self.scripts()["smoke:report"])
        self.assertNotIn(
            "smoke_offline",
            " ".join(v for k, v in self.scripts().items() if k.startswith("test:")),
        )


class LintStagedMirrorsLint(unittest.TestCase):
    """pre-commit runs lint-staged, whose per-glob commands are a SECOND
    rendition of the `lint:*` scripts. Two renditions of one gate drift the
    moment one is edited alone -- a ruff bump in `lint:py` that the hook keeps
    running at the old pin is a commit that passes locally and fails in CI.
    `--no-stash --no-hide-partially-staged` are pinned because lint-staged's
    default stashes unstaged work to lint the exact index content, and that
    round-trip loses edits.
    """

    def pkg(self):
        return json.loads(read("package.json"))

    def staged(self):
        cfg = self.pkg()["lint-staged"]
        flat = []
        for glob, cmds in cfg.items():
            for c in cmds if isinstance(cmds, list) else [cmds]:
                flat.append((glob, c))
        return flat

    def test_pre_commit_runs_lint_staged_and_not_the_whole_tree(self):
        hook = read(".husky/pre-commit")
        self.assertIn("bun run --silent lint:staged", hook)
        self.assertNotRegex(hook, r"(?m)^bun run --silent lint$")
        s = self.pkg()["scripts"]["lint:staged"]
        self.assertTrue(s.startswith("lint-staged "), s)
        self.assertIn("--no-stash", s)
        # --no-stash ALONE still checks out the index copy of a partially
        # staged file and, when a task fails, does not put the unstaged half
        # back (2026-09-05: one failing run wiped the unstaged edits on seven
        # files; .git/lint-staged_unstaged.patch was the only copy).
        self.assertIn("--no-hide-partially-staged", s)
        self.assertIn("--relative", s)
        self.assertIn("lint-staged", self.pkg()["devDependencies"])

    def test_every_staged_command_is_check_only(self):
        # The hook never writes: a formatter that FIXES silently re-adds the
        # rewritten file to a commit the author did not review.
        for glob, c in self.staged():
            with self.subTest(glob=glob):
                self.assertNotRegex(c, r"\boxfmt(?! --check)")
                self.assertNotIn("ruff@", c) if "format" in c else None
                self.assertNotIn("--fix", c)
                self.assertNotIn("--write", c)

    def test_ruff_pin_matches_lint_py(self):
        s = self.pkg()["scripts"]
        pin = re.search(r"uvx ruff@(\S+) check", s["lint:py"]).group(1)
        py = [c for g, c in self.staged() if "ruff" in c]
        self.assertEqual(len(py), 1, py)
        self.assertIn("uvx ruff@%s check" % pin, py[0])

    def test_flags_match_the_lint_scripts(self):
        s = self.pkg()["scripts"]
        cmds = dict(self.staged())
        flat = "\n".join(c for _, c in self.staged())
        self.assertIn(s["lint:js"], flat)  # oxlint --deny-warnings
        self.assertIn(s["lint:ci"], flat)  # actionlint
        self.assertIn("shellcheck -x -S warning", flat)
        self.assertIn("shellcheck -x -S error", flat)
        self.assertIn("bun build --no-bundle", flat)
        self.assertIn("tests/check_page.py", flat)
        self.assertIn("py_compile", flat)
        # staging/ blocks at error and is advisory above it, as lint:sh:staging.
        sh = [c for g, c in self.staged() if "shellcheck" in c]
        self.assertEqual(len(sh), 1)
        self.assertIn("staging/*)", sh[0])
        self.assertIn("advisory above error level", sh[0])
        # Every shell entry point lint:sh covers is reachable by the glob.
        globs = list(cmds)
        shglob = next(g for g in globs if "*.sh" in g)
        for part in ("smeltr", ".husky/[a-z]*", "**/*.sh"):
            self.assertIn(part, shglob)
        # The page-assembly check fires on the files that assemble the page.
        pg = next(g for g in globs if "check_page" in cmds[g])
        self.assertIn("web/*", pg)
        self.assertIn("dashboard/server.py", pg)
        wf = next(g for g in globs if cmds[g] == "actionlint")
        self.assertEqual(wf, ".github/workflows/*.yml")


class CommitRules(unittest.TestCase):
    """Runs the REAL `check_message` out of .husky/commit-rules.sh.

    The rules are pr.yml's `commits` job transcribed into POSIX sh; the
    messages here are the ones that job would reject, plus the exemptions it
    grants (merges) and the one the hook adds (fixup!/squash! at commit time
    only). A string assertion on the hook could not tell a working regex from
    a broken one.
    """

    def check(self, message, *args):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".msg", delete=False) as f:
            f.write(message)
        try:
            r = subprocess.run(
                [
                    "sh",
                    "-c",
                    '. ./.husky/commit-rules.sh; check_message "$1" $2; rc=$?; '
                    "printf '%s' \"$problems\"; exit $rc",
                    "_",
                    f.name,
                    *args,
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
        finally:
            os.unlink(f.name)
        return r.returncode, r.stdout

    def test_a_house_style_message_passes(self):
        rc, why = self.check("SMLTR: Add a thing\n\n- `x` now does y\n")
        self.assertEqual((rc, why), (0, ""))

    def test_comments_and_the_scissors_diff_are_not_body(self):
        rc, why = self.check(
            "SMLTR: Add a thing\n# Please enter the commit message\n"
            "# ------------------------ >8 ------------------------\n"
            "* Co-Authored-By: Claude\n"
        )
        self.assertEqual((rc, why), (0, ""))

    def test_each_rule_fires_on_its_own(self):
        cases = {
            "add a thing\n": "SMLTR: ",
            "SMLTR: add a thing\n": "Capitalized",
            "SMLTR: feat: Add a thing\n": "conventional-commits",
            "SMLTR: Add a thing.\n": "trailing period",
            "SMLTR: " + "Add a thing " * 6 + "\n": "max 72",
            "SMLTR: Generated by a robot\n": "AI-authorship credit in the subject",
            "SMLTR: Add a thing\n\n* bullet\n": "bullets are '-'",
            "SMLTR: Add a thing\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n": "AI-authorship trailer",
            "SMLTR: Add a thing\n\nGenerated with [Claude Code](https://claude.ai)\n": "AI-authorship trailer",
        }
        for msg, expect in cases.items():
            with self.subTest(msg=msg.splitlines()[0]):
                rc, why = self.check(msg)
                self.assertEqual(rc, 1, why)
                self.assertIn(expect, why)

    def test_fixup_passes_at_commit_time_and_never_at_push_time(self):
        for prefix in ("fixup! ", "squash! "):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    self.check(prefix + "SMLTR: Add a thing\n", "allow_fixup")[0], 0
                )
                rc, why = self.check(prefix + "SMLTR: Add a thing\n")
                self.assertEqual(rc, 1)
                self.assertIn("autosquash", why)

    def test_merge_and_empty_are_left_to_git(self):
        self.assertEqual(self.check("Merge branch 'feature'\n")[0], 0)
        self.assertEqual(self.check("\n# nothing but comments\n")[0], 0)


# ---------------------------------------------------------------------- page

# The page used to be one r-string in server.py. It is now assembled from
# web/index.html + app.css + theme.js + app.js at import. Assert against the
# ASSEMBLED result, not the sources: that is what a browser receives, and it
# fails loudly if a placeholder ever stops resolving.
PAGE = server._PAGE
# The dense form of the page (no whitespace around { } : ; > ,): oxfmt writes
# the CSS one declaration per line, and these pin rules, not layout.
DENSE = re.sub(r"\s*([{}:;>,])\s*", r"\1", PAGE)


class UnitsSurviveCSS(unittest.TestCase):
    """`th{text-transform:uppercase}` turns Mb/s into MB/S -- an 8x lie.

    CLAUDE.md calls this out by name: unit confusion on this exact column
    already corrupted the old state file. Any header carrying a unit needs
    class="unit" (th.unit{text-transform:none}).
    """

    UNIT = re.compile(r"\b(?:[KMGT]i?[Bb]/s|Mb/s|GiB|MiB|%)")

    def test_every_unit_bearing_header_opts_out_of_uppercase(self):
        headers = re.findall(r'\{\s*label:\s*"([^"]*)"((?:,[^{}]*)?)\}', PAGE)
        checked = 0
        for label, rest in headers:
            if not self.UNIT.search(label):
                continue
            checked += 1
            self.assertIn(
                "unit",
                rest,
                f'header "{label}" lacks cls:"unit" and will render '
                f"uppercased -- its unit becomes a different unit",
            )
        self.assertTrue(checked, "no unit-bearing header found; regex has rotted")

    def test_the_rule_it_relies_on_still_exists(self):
        self.assertIn("th.unit{text-transform:none", DENSE)


class StickyHeaderOutranksTheTitleColumn(unittest.TestCase):
    """`th{position:sticky}` is one element selector -- a class beats it.

    `.title-cell{position:relative}` (0,1,0) silently un-stuck the TITLE
    header while every other header kept sticking, so the rows of that one
    column scrolled through the gap and painted over the header band. Any
    `position` handed to a bare `.title-cell` outside the pinned-column
    media query re-opens that; qualify it (`td.title-cell`) instead.
    """

    def test_no_bare_title_cell_sets_position(self):
        css = re.sub(r"/\*.*?\*/", " ", read("web/app.css"), flags=re.S)
        # Drop every @media block: inside the <=700px one `.title-cell`
        # legitimately pins, and both halves are named there so the td rule
        # cannot outrank it.
        while "@media" in css:
            i = css.index("@media")
            j = css.index("{", i)
            depth = 0
            for k in range(j, len(css)):
                if css[k] == "{":
                    depth += 1
                elif css[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
            css = css[:i] + css[k + 1 :]
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if not re.search(r"(^|[\s,>+~])\.title-cell\b", sel):
                continue
            self.assertNotIn(
                "position:",
                body,
                f"bare `.title-cell` sets position in `{sel.strip()}` "
                f"-- it outranks th{{position:sticky}} and unsticks "
                f"the TITLE header",
            )

    def test_the_rule_it_protects_still_exists(self):
        self.assertIn("th{position:sticky;top:0;z-index:1", DENSE)


class ThemeTokens(unittest.TestCase):
    """Both themes are token sets with the same names.

    CLAUDE.md: "a hex written anywhere below :root is a colour the light theme
    cannot reach, which is exactly how the page stayed half-dark before."
    """

    COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|color-mix\(|oklch\(")

    def blocks(self):
        def one(sel):
            j = DENSE.index(sel)
            return DENSE[j : DENSE.index("}", j)]

        return one(":root{"), one(':root[data-theme="light"]{')

    def test_every_token_used_is_defined(self):
        dark, _ = self.blocks()
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", dark))
        for tok in set(re.findall(r"var\((--[a-z0-9-]+)", PAGE)):
            self.assertIn(
                tok, defined, f"{tok} is used but defined in neither root block"
            )

    def test_every_colour_token_exists_in_both_themes(self):
        dark, light = self.blocks()
        light_names = set(re.findall(r"(--[a-z0-9-]+)\s*:", light))
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:([^;]+);", dark):
            if not self.COLOUR.search(value):
                continue  # --r (radius), --mono (font stack)
            self.assertIn(
                name, light_names, f"{name} is a colour with no light-theme value"
            )


class CSPNonces(unittest.TestCase):
    """One <style> and TWO <script> blocks carry nonce="__NONCE__".

    CSP is default-src 'none'. A block that loses its nonce never runs, and the
    second one is the pre-paint theme script in <head> -- losing that is a flash
    of the wrong theme on every load, which looks like a rendering bug rather
    than a missing attribute.
    """

    def test_three_nonced_blocks(self):
        self.assertEqual(PAGE.count('nonce="__NONCE__"'), 3)

    def test_the_placeholder_is_substituted_at_serve_time(self):
        self.assertTrue("__NONCE__" in PAGE)
        self.assertTrue("replace(" in PAGE)


# -------------------------------------------------------------------- ledger


class LedgerFixture(unittest.TestCase):
    """The frozen calibration corpus must stay parseable and complete."""

    PATH = os.path.join(REPO, "tests", "fixtures", "ledger-calibration.jsonl")

    def test_every_line_is_a_ledger_row(self):
        with open(self.PATH, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("title", r)
            self.assertIn("output_bytes", r)

    def test_it_is_not_swallowed_by_gitignore(self):
        """`ledger.jsonl` is ignored; an over-broad rule would take this too.

        A fixture git refuses to carry is a fixture CI never sees, and the
        calibration suite goes back to erroring on a fresh clone -- the exact
        state this file exists to end.
        """
        self.assertFalse(ignored("tests/fixtures/ledger-calibration.jsonl"))


if __name__ == "__main__":
    unittest.main()
