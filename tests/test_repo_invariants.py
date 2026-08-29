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
import unittest

import core


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _git(*args):
    out = subprocess.run(("git",) + args, cwd=REPO,
                         capture_output=True, text=True, check=True)
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
TRACK_FLAGS = ("--all-audio", "--aencoder", "copy",
               "--audio-fallback", "ac3", "--all-subtitles")


class TrackPreservation(unittest.TestCase):
    """The flags that keep every audio and subtitle track."""

    ENCODERS = ("staging/autopilot.sh", "server.py")

    def test_every_encoder_passes_the_track_flags(self):
        for rel in self.ENCODERS:
            src = read(rel)
            self.assertTrue("HandBrakeCLI" in src,
                            f"{rel} no longer starts an encode?")
            for flag in TRACK_FLAGS:
                # assertTrue, not assertIn: assertIn prints the whole haystack,
                # and these haystacks are 3,700-line files.
                self.assertTrue(flag in src,
                                f"{rel} lost {flag} -- an encode from here would "
                                f"drop tracks and the original still gets deleted")

    def test_no_track_selection_anywhere(self):
        """`--audio-lang-list` selects a subset. It must never appear."""
        for rel in committable():
            if not rel.endswith((".sh", ".py")) and rel != "smeltr":
                continue
            if rel.startswith("tests/"):
                continue
            self.assertTrue("--audio-lang-list" not in read(rel),
                            f"{rel} selects a subset of audio tracks")


class EncodeFlagParity(unittest.TestCase):
    """server.py and autopilot.sh are two implementations of ONE procedure.

    CLAUDE.md: "The encode-control block in server.py deliberately mirrors
    .autopilot.sh start_encode() -- same flags [...] a change to either must be
    mirrored in the other." Nothing enforced that, so a CRF or preset changed in
    one place would quietly produce two different encodes depending on whether
    the driver or the dashboard started it -- and only one of them is the one
    the ledger's history was calibrated against.
    """

    # Flags that decide what the output IS. Deliberately excludes -i/-o/-q,
    # which are per-title by nature.
    SHAPE = ("-f", "av_mkv", "-e", "x265_10bit", "--encoder-preset", "medium") + TRACK_FLAGS

    def test_both_build_the_same_encode(self):
        for rel in ("staging/autopilot.sh", "server.py"):
            src = read(rel)
            for flag in self.SHAPE:
                self.assertTrue(flag in src, f"{rel} is missing {flag}")

    def test_crf_menu_is_the_documented_set(self):
        """16/18/20/22/24. 22 and 24 sit OUTSIDE the ladder on purpose.

        .watch-encode.sh maps only 16->18->20, so a blowup at 22 or 24 is
        auto-killed and never restarted. Widening this menu silently adds more
        of those dead ends.
        """
        import server
        self.assertEqual(server.CRF_CHOICES, (16, 18, 20, 22, 24))


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
            self.assertTrue(root in ap,
                            f"core.LIBRARY_ROOTS has {root}; staging/autopilot.sh "
                            f"cannot resolve a library original under it")

    def test_core_queues_every_root_autopilot_resolves(self):
        ap = read("staging/autopilot.sh")
        # Media/ scopes this to the NAS library roots. The staging drive
        # (/Volumes/Crucial X9/4K Movies) is core.X9, not a library root.
        for root in re.findall(r'"(/Volumes/[^"]*/Media/4K[^"]*Movies)"', ap):
            self.assertIn(root, core.LIBRARY_ROOTS,
                          f"staging/autopilot.sh reads {root}; core.py never "
                          f"queues or offline-checks it")


# -------------------------------------------------------------- decision path

class DecisionPathIsolation(unittest.TestCase):
    """The four files the driver calls must not depend on the dashboard.

    CLAUDE.md: "Verified: verdict.py does not load server.py. Worst case the
    page breaks and the pipeline keeps running." That guarantee is one import
    away from being false -- server.py binds sockets, spawns threads and starts
    the sysmon sampler at import, so pulling it into verdict.py would make a
    port conflict able to halt an encode's verdict.
    """

    DECISION_PATH = ("core.py", "verdict.py", "next_title.py", "record.py")
    FORBIDDEN = ("server", "sysmon", "report")

    def test_no_dashboard_import(self):
        for rel in self.DECISION_PATH:
            for line in read(rel).splitlines():
                m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][\w.]*)", line)
                if not m:
                    continue
                self.assertNotIn(m.group(1), self.FORBIDDEN,
                                 f"{rel} imports {m.group(1)} -- the decision "
                                 f"path can now be broken by the dashboard")


# ------------------------------------------------------------------- secrets

class NothingSecretIsTracked(unittest.TestCase):
    """server.log and url carry the auth token; `token` IS the token.

    The remote is public. A single `git add -A` on a machine whose .gitignore
    got clobbered publishes a token that grants write access to the dashboard --
    which starts encodes, aborts them, and un-skips titles (re-arming a
    deletion).
    """

    NEVER = ("token", "url", "server.pid", "server.log", "sysmon.ring",
             "ledger.jsonl", "queue_overrides.json", "pause")

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
                r = subprocess.run(["bash", "-n", os.path.join(REPO, rel)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr.strip()}")


# ---------------------------------------------------------------------- page

PAGE = read("server.py")


class UnitsSurviveCSS(unittest.TestCase):
    """`th{text-transform:uppercase}` turns Mb/s into MB/S -- an 8x lie.

    CLAUDE.md calls this out by name: unit confusion on this exact column
    already corrupted the old state file. Any header carrying a unit needs
    class="unit" (th.unit{text-transform:none}).
    """

    UNIT = re.compile(r"\b(?:[KMGT]i?[Bb]/s|Mb/s|GiB|MiB|%)")

    def test_every_unit_bearing_header_opts_out_of_uppercase(self):
        headers = re.findall(r'\{label:"([^"]*)"((?:,[^{}]*)?)\}', PAGE)
        checked = 0
        for label, rest in headers:
            if not self.UNIT.search(label):
                continue
            checked += 1
            self.assertIn("unit", rest,
                          f'header "{label}" lacks cls:"unit" and will render '
                          f'uppercased -- its unit becomes a different unit')
        self.assertTrue(checked, "no unit-bearing header found; regex has rotted")

    def test_the_rule_it_relies_on_still_exists(self):
        self.assertTrue("th.unit{text-transform:none}" in PAGE)


class ThemeTokens(unittest.TestCase):
    """Both themes are token sets with the same names.

    CLAUDE.md: "a hex written anywhere below :root is a colour the light theme
    cannot reach, which is exactly how the page stayed half-dark before."
    """

    COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|color-mix\(|oklch\(")

    def blocks(self):
        i = PAGE.index("_PAGE")
        page = PAGE[i:]
        def one(sel):
            j = page.index(sel)
            return page[j:page.index("}", j)]
        return one(":root{"), one(':root[data-theme="light"]{')

    def test_every_token_used_is_defined(self):
        dark, _ = self.blocks()
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", dark))
        for tok in set(re.findall(r"var\((--[a-z0-9-]+)", PAGE)):
            self.assertIn(tok, defined,
                          f"{tok} is used but defined in neither root block")

    def test_every_colour_token_exists_in_both_themes(self):
        dark, light = self.blocks()
        light_names = set(re.findall(r"(--[a-z0-9-]+)\s*:", light))
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:([^;]+);", dark):
            if not self.COLOUR.search(value):
                continue          # --r (radius), --mono (font stack)
            self.assertIn(name, light_names,
                          f"{name} is a colour with no light-theme value")


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
