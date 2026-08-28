"""The boot skeleton: what the page shows before the first SSE frame lands.

The body ships with #stats, #liveWrap and #pane EMPTY -- nothing renders until
es.onmessage fires paint(). A cold build_state() stats three NAS roots over SMB
and measures ~2 s, so every cold load showed a dark, empty page for that whole
window; a stale token, which permanently CLOSES the EventSource, showed it
forever with only a header hint.

The fix is static ghost content inside those three containers. It needs no
teardown code because all three clear unconditionally on the first real paint
(renderStats and renderQueue/renderLedger call replaceChildren() outright;
renderLive's sig is never empty and a fresh element has no data-sig, so it can
never match). These tests pin the parts that would otherwise break SILENTLY --
a skeleton that renders but is invisible, unthemed, or unstoppable is worse
than none, because it is the thing standing between a black screen and a
diagnosis.
"""
import re
import unittest

import server


def css_block(name: str) -> str:
    """The body of the first CSS rule whose selector contains `name`."""
    i = server.PAGE.index(name)
    return server.PAGE[i:server.PAGE.index("}", i)]


class Tokens(unittest.TestCase):
    """Colours are tokens, never hex literals -- both themes must reach them.

    A --skel defined only in the dark :root leaves the light theme drawing
    ghosts in a dark-theme colour, which is exactly how the page stayed
    half-dark before.
    """

    def test_defined_in_both_root_blocks(self):
        dark = server.PAGE.index(":root{")
        light = server.PAGE.index(':root[data-theme="light"]')
        dark_block = server.PAGE[dark:light]
        light_block = server.PAGE[light:server.PAGE.index("}", light)]
        for tok in ("--skel", "--skel-hi"):
            self.assertIn(tok + ":", dark_block, f"{tok} missing from dark :root")
            self.assertIn(tok + ":", light_block, f"{tok} missing from light :root")

    def test_skeleton_css_uses_no_hex_literals(self):
        for sel in (".skel{", ".boot-note{", ".boot-fail{"):
            block = css_block(sel)
            self.assertNotRegex(
                block, r"#[0-9a-fA-F]{3,8}\b",
                f"{sel} carries a hex literal the light theme cannot reach")


class Motion(unittest.TestCase):
    def test_shimmer_is_disabled_under_reduced_motion(self):
        i = server.PAGE.index("prefers-reduced-motion")
        block = server.PAGE[i:i + 900]
        self.assertIn("animation:none", block.replace(" ", ""))

    def test_ghosts_are_excluded_from_entrance_animations(self):
        """body:not(.booted) .stat animates every card in. The ghosts must sit
        that out, or they play an entrance animation and are then immediately
        replaced by real cards playing it again."""
        self.assertIn("body:not(.booted) .stat:not(.skel)", server.PAGE)

    def test_skeleton_is_delayed_so_a_warm_load_never_flashes_it(self):
        """State is cached, so a second load paints almost instantly. A
        skeleton visible at 0 ms would strobe on every such load.

        The delay must live in the ANIMATION (with `both` fill), not in a bare
        opacity:0 -- prefers-reduced-motion kills animations globally, and a
        skeleton whose only opacity came from a dead animation is invisible."""
        self.assertRegex(server.PAGE, r"@keyframes skelin\{from\{opacity:0\}\}")
        self.assertRegex(css_block(".skelwrap{"),
                         r"animation:skelin [\d.]+s [a-z-]+ \.25s both")
        self.assertNotRegex(css_block(".skelwrap{"), r"(?<!-)opacity:0")

    def test_stats_ghosts_keep_the_delay_despite_display_contents(self):
        """#stats is a grid, so the wrapper is display:contents or the six
        ghosts stack in one column. A display:contents box generates no box and
        cannot animate opacity, so the reveal must move to the children."""
        block = css_block(".skelgrid{")
        self.assertIn("display:contents", block)
        self.assertIn("animation:none", block)
        self.assertRegex(css_block(".skelgrid>*{"), r"animation:skelin .*both")


class Markup(unittest.TestCase):
    def test_ghosts_ship_inside_all_three_empty_containers(self):
        # #stats is the collapsible section; its ghosts live one level in,
        # inside the grid the head strip now sits above.
        for cid in ("statsGrid", "liveWrap", "pane"):
            m = re.search(r'id="%s"[^>]*>(.*?)</(?:section|div)>' % cid,
                          server.PAGE, re.S)
            self.assertIsNotNone(m, f"#{cid} not found")
            self.assertIn("skel", m.group(1),
                          f"#{cid} ships empty -- it is a black region on boot")

    def test_skeleton_is_hidden_from_assistive_tech(self):
        """Ghost rows carry no information; announcing them is noise."""
        for m in re.finditer(r'<div class="skelwrap"[^>]*>', server.PAGE):
            self.assertIn('aria-hidden="true"', m.group(0))

    def test_no_server_data_in_the_skeleton(self):
        """Rule 2: never innerHTML with server data. The skeleton is static,
        and a substitution token appearing inside it would break that."""
        i = server.PAGE.index('id="stats"')
        j = server.PAGE.index("</div>", server.PAGE.index('id="pane"'))
        self.assertNotIn("__", server.PAGE[i:j])


class Failure(unittest.TestCase):
    """A spinner that spins forever is the lie this project forbids elsewhere
    ('stalled, never a progress bar'). Boot has two degraded states."""

    def test_slow_boot_note_exists_and_is_time_gated(self):
        self.assertIn("BOOT_SLOW_MS", server.PAGE)
        self.assertIn("still waiting on the library", server.PAGE)

    def test_closed_stream_error_is_gated_on_never_having_booted(self):
        """A MID-SESSION disconnect must keep the last-known data on screen and
        change only the header dot. Only a boot that never produced data may
        replace the body with an error."""
        i = server.PAGE.index("function bootFail")
        head = server.PAGE[i:i + 200].replace(" ", "")
        self.assertIn("if(booted)return", head,
                      "bootFail must bail once real data has painted")
        j = server.PAGE.index("function bootSlow")
        self.assertIn("if(booted)return",
                      server.PAGE[j:j + 200].replace(" ", ""))

    def test_closed_stream_error_names_the_stale_token(self):
        """Must be in the RENDERED string, not just a source comment -- the
        stale-token 403 is the one boot failure the user can actually fix."""
        i = server.PAGE.index("function bootFail")
        j = server.PAGE.index("es.onmessage")
        self.assertIn("stale token", server.PAGE[i:j])
        self.assertIn("reload the page", server.PAGE[i:j])

    def test_error_text_is_built_with_el_not_innerhtml(self):
        i = server.PAGE.index("function bootFail")
        self.assertNotIn("innerHTML", server.PAGE[i:i + 900])


class Csp(unittest.TestCase):
    def test_nonce_count_unchanged(self):
        """One <style> and two <script> blocks carry the nonce. A block without
        it never runs -- silently."""
        self.assertEqual(server.PAGE.count('nonce="'), 3)

    def test_no_inline_handlers(self):
        self.assertNotRegex(server.PAGE, r"\son[a-z]+\s*=\s*\"")

    def test_no_inline_style_attributes(self):
        """style-src is 'nonce-...' with NO 'unsafe-inline', so a style
        attribute is dropped silently -- every ghost width would collapse to
        zero and the skeleton would render as nothing at all. Widths are
        classes for exactly this reason."""
        self.assertNotIn('style="', server.PAGE)


if __name__ == "__main__":
    unittest.main()
