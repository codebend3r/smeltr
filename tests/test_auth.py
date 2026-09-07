"""The sign-in gate, and the public listener it exists to guard.

`tailscale funnel` puts this dashboard on the open internet, and the whole
reason that is survivable is in this file:

  * a PUBLIC request does not accept the URL token. The token lives in the
    query string of every URL that has ever been bookmarked or pasted into a
    chat window; that is fine for a device on the LAN and not for the internet.
  * a PUBLIC request cannot skip, reorder, start/abort an encode or stage a
    pull -- even signed in. Un-skipping re-arms a ~90 GB deletion.
  * the untrusted flag is a property of the LISTENER, not of a header. Funnel
    proxies from 127.0.0.1, so every header-sniffing version of this check is
    one spoofed header away from handing a loopback peer everything.

Plus the credential mechanics: the verifier never stores the password, both
halves compare in constant time, a cookie cannot be forged or replayed past its
expiry, and rotating the password logs every device out.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import auth
from dashboard import server

PW = "correct horse battery staple"
USER = "cjrivas"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        auth.reload_config()
        self.addCleanup(auth.reload_config)
        # Fast KDF: these tests assert the LOGIC, and 600k iterations x the
        # number of cases below is a minute of CPU for no extra coverage.
        # ITERATIONS is stored per-record and replayed from the file, which is
        # exactly what makes this substitution safe.
        p = mock.patch.object(auth, "ITERATIONS", 1000)
        p.start()
        self.addCleanup(p.stop)


class Verifier(Tmp):
    def test_the_password_is_not_in_the_file(self):
        auth.write_config(self.dir, USER, PW)
        with open(auth.path(self.dir)) as fh:
            blob = fh.read()
        self.assertNotIn(PW, blob)
        self.assertIn("pbkdf2_sha256", blob)

    def test_written_0600(self):
        """It holds the verifier AND the cookie-signing secret."""
        auth.write_config(self.dir, USER, PW)
        self.assertEqual(os.stat(auth.path(self.dir)).st_mode & 0o777, 0o600)

    def test_round_trip(self):
        auth.write_config(self.dir, USER, PW)
        self.assertTrue(auth.verify(self.dir, USER, PW))

    def test_wrong_password_and_wrong_user_both_fail(self):
        auth.write_config(self.dir, USER, PW)
        self.assertFalse(auth.verify(self.dir, USER, PW + "!"))
        self.assertFalse(auth.verify(self.dir, "someone", PW))
        self.assertFalse(auth.verify(self.dir, "", ""))

    def test_salt_makes_two_identical_passwords_differ(self):
        a = auth.hash_password(PW, 1000)
        b = auth.hash_password(PW, 1000)
        self.assertNotEqual(a["hash"], b["hash"])
        self.assertNotEqual(a["salt"], b["salt"])

    def test_no_file_is_off_not_open(self):
        """The absent-config case must deny, never default to allowing."""
        self.assertFalse(auth.enabled(self.dir))
        self.assertFalse(auth.verify(self.dir, USER, PW))
        self.assertFalse(auth.check_cookie(self.dir, "anything"))

    def test_a_group_readable_file_is_refused(self):
        """A signing secret another account can read is a forgeable cookie."""
        auth.write_config(self.dir, USER, PW)
        os.chmod(auth.path(self.dir), 0o640)
        auth.reload_config()
        self.assertFalse(auth.enabled(self.dir))
        self.assertFalse(auth.verify(self.dir, USER, PW))

    def test_a_corrupt_file_is_refused_not_crashed(self):
        with open(auth.path(self.dir), "w") as fh:
            fh.write("{not json")
        os.chmod(auth.path(self.dir), 0o600)
        auth.reload_config()
        self.assertFalse(auth.enabled(self.dir))

    def test_iterations_are_replayed_from_the_record(self):
        """Raising ITERATIONS later must not invalidate an existing file."""
        auth.write_config(self.dir, USER, PW)
        with mock.patch.object(auth, "ITERATIONS", 999_999):
            self.assertTrue(auth.verify(self.dir, USER, PW))


class Cookies(Tmp):
    def setUp(self):
        super().setUp()
        auth.write_config(self.dir, USER, PW)

    def test_round_trip(self):
        self.assertTrue(auth.check_cookie(self.dir, auth.make_cookie(self.dir)))

    def test_a_tampered_signature_is_refused(self):
        c = auth.make_cookie(self.dir)
        head, _, _ = c.rpartition(".")
        self.assertFalse(auth.check_cookie(self.dir, head + ".deadbeef"))

    def test_an_extended_expiry_is_refused(self):
        """The expiry is INSIDE the signed payload, not beside it."""
        c = auth.make_cookie(self.dir)
        _, _, sig = c.rpartition(".")
        self.assertFalse(auth.check_cookie(self.dir, f"v1.{int(time.time()) + 10**6}.{sig}"))

    def test_an_expired_cookie_is_refused(self):
        with mock.patch.object(auth, "SESSION_SECONDS", -1):
            c = auth.make_cookie(self.dir)
        self.assertFalse(auth.check_cookie(self.dir, c))

    def test_garbage_is_refused_not_crashed(self):
        for bad in ("", "x", "v1.notanint.aa", "v2.1.2", "a.b.c.d", "...."):
            with self.subTest(bad=bad):
                self.assertFalse(auth.check_cookie(self.dir, bad))

    def test_rotating_the_password_logs_every_device_out(self):
        c = auth.make_cookie(self.dir)
        self.assertTrue(auth.check_cookie(self.dir, c))
        auth.write_config(self.dir, USER, "a different one")
        self.assertFalse(auth.check_cookie(self.dir, c))

    def test_header_parsing_picks_our_cookie_out_of_a_crowd(self):
        self.assertEqual(
            auth.parse_cookie_header(f"a=1; {auth.COOKIE_NAME}=abc; z=2"), "abc"
        )
        self.assertEqual(auth.parse_cookie_header('smeltr_session="q"'), "q")
        self.assertEqual(auth.parse_cookie_header("a=1; b=2"), "")
        self.assertEqual(auth.parse_cookie_header(""), "")

    def test_a_cookie_named_like_ours_but_longer_is_not_ours(self):
        self.assertEqual(auth.parse_cookie_header("smeltr_session_x=abc"), "")


class Throttle(unittest.TestCase):
    def setUp(self):
        auth._fails.clear()
        self.addCleanup(auth._fails.clear)

    def test_the_first_attempts_are_free(self):
        for _ in range(auth._FAIL_AFTER - 1):
            auth.note_failure("1.2.3.4")
        self.assertEqual(auth.blocked_for("1.2.3.4"), 0.0)

    def test_then_it_backs_off_and_keeps_growing(self):
        for _ in range(auth._FAIL_AFTER):
            auth.note_failure("1.2.3.4")
        first = auth.blocked_for("1.2.3.4")
        self.assertGreater(first, 0)
        auth.note_failure("1.2.3.4")
        self.assertGreater(auth.blocked_for("1.2.3.4"), first)

    def test_it_is_per_peer(self):
        for _ in range(auth._FAIL_AFTER + 3):
            auth.note_failure("1.2.3.4")
        self.assertEqual(auth.blocked_for("5.6.7.8"), 0.0)

    def test_a_success_clears_it(self):
        for _ in range(auth._FAIL_AFTER + 3):
            auth.note_failure("1.2.3.4")
        auth.note_success("1.2.3.4")
        self.assertEqual(auth.blocked_for("1.2.3.4"), 0.0)

    def test_the_table_is_bounded(self):
        """An attacker rotating source addresses must not grow it forever."""
        for i in range(5000):
            auth.note_failure(f"10.0.{i // 256}.{i % 256}")
        self.assertLessEqual(len(auth._fails), 4000)


def _handler(cls, peer="127.0.0.1", local="127.0.0.1", cookie=""):
    h = object.__new__(cls)

    class H(dict):
        def get(self, key, default=None):
            for k, v in self.items():
                if k.lower() == key.lower():
                    return v
            return default

    h.headers = H({"Cookie": cookie} if cookie else {})
    h.client_address = (peer, 54321)

    class S:
        def getsockname(self):
            return (local, 8787)

    h.connection = S()
    return h


class PublicListener(unittest.TestCase):
    """The Funnel-facing door. Everything here is why it is survivable."""

    def test_it_is_untrusted_and_the_normal_one_is_not(self):
        self.assertTrue(server.PublicHandler.untrusted)
        self.assertFalse(server.Handler.untrusted)

    def test_untrusted_is_a_class_flag_not_a_header(self):
        """A header test would be one spoofed header from full write access.

        Funnel connects from 127.0.0.1, so _peer_is_loopback() and the
        sockname branch of _writes_ok() BOTH say yes for a public request. The
        listener the request arrived on is the only fact a client cannot lie
        about, so that is what the flag records.
        """
        h = _handler(server.PublicHandler)
        self.assertTrue(h._peer_is_loopback())
        self.assertFalse(h._writes_ok("/api/queue/skip"))

    def test_a_public_peer_cannot_reach_the_dangerous_writes(self):
        h = _handler(server.PublicHandler)
        for route in (
            "/api/queue/skip",
            "/api/queue/order",
            "/api/queue/crf",
            "/api/queue/encoder",
            "/api/encode/start",
            "/api/encode/abort",
            "/api/stage/start",
            "/api/stage/cancel",
            "",
        ):
            with self.subTest(route=route):
                self.assertFalse(h._writes_ok(route))

    def test_a_public_peer_may_still_pause_and_press_play(self):
        """Their worst case is the pipeline waiting, or running as designed."""
        h = _handler(server.PublicHandler)
        for route in server.PUBLIC_WRITE_ROUTES:
            with self.subTest(route=route):
                self.assertTrue(h._writes_ok(route))

    def test_lan_writes_opt_in_does_not_reach_the_public_door(self):
        """SMELTR_LAN_WRITES is about the LAN. It is not about the internet."""
        with mock.patch.object(server, "LAN_WRITES", True):
            self.assertFalse(
                _handler(server.PublicHandler)._writes_ok("/api/queue/skip")
            )
            # ...and the LAN door is unaffected by this suite's existence.
            self.assertTrue(
                _handler(server.Handler, peer="192.168.1.9", local="192.168.1.2")
                ._writes_ok("/api/queue/skip")
            )

    def test_the_token_does_not_open_the_public_door(self):
        with mock.patch.object(server, "AUTH_ON", True):
            h = _handler(server.PublicHandler)
            with mock.patch.object(server.Handler, "_token_ok", lambda s, q: True):
                self.assertFalse(h._authorized({"t": ["whatever"]}))

    def test_the_token_still_opens_the_lan_door(self):
        h = _handler(server.Handler)
        with mock.patch.object(server.Handler, "_token_ok", lambda s, q: True):
            self.assertTrue(h._authorized({"t": ["whatever"]}))

    def test_a_session_opens_either_door(self):
        with mock.patch.object(server, "AUTH_ON", True):
            with mock.patch.object(server.Handler, "_session_ok", lambda s: True):
                self.assertTrue(_handler(server.PublicHandler)._authorized({}))
                self.assertTrue(_handler(server.Handler)._authorized({}))

    def test_no_credential_opens_neither(self):
        with mock.patch.object(server, "AUTH_ON", True):
            with mock.patch.object(server.Handler, "_session_ok", lambda s: False):
                with mock.patch.object(server.Handler, "_token_ok", lambda s, q: False):
                    self.assertFalse(_handler(server.PublicHandler)._authorized({}))
                    self.assertFalse(_handler(server.Handler)._authorized({}))

    def test_the_cookie_is_httponly_samesite_and_secure_only_in_public(self):
        pub = _handler(server.PublicHandler)._set_session("v")["Set-Cookie"]
        lan = _handler(server.Handler)._set_session("v")["Set-Cookie"]
        for c in (pub, lan):
            self.assertIn("HttpOnly", c)
            self.assertIn("SameSite=Strict", c)
            self.assertIn("Path=/", c)
        # Funnel is always HTTPS; the LAN door is plain http, where a Secure
        # cookie is one the browser silently refuses to store.
        self.assertIn("Secure", pub)
        self.assertNotIn("Secure", lan)

    def test_logout_expires_it(self):
        self.assertIn(
            "Max-Age=0", _handler(server.Handler)._set_session("", clear=True)["Set-Cookie"]
        )


class PublicRequiresAPassword(unittest.TestCase):
    def test_the_public_listener_refuses_to_exist_without_auth(self):
        """PUBLIC_ON is `SMELTR_PUBLIC=1 AND a password`.

        Funnelling an unauthenticated dashboard would publish encode control
        and the whole ledger to the internet behind a query-string token.
        """
        with open(os.path.join(ROOT, "dashboard", "server.py")) as fh:
            src = fh.read()
        self.assertIn('PUBLIC_ON = os.environ.get("SMELTR_PUBLIC") == "1" and AUTH_ON', src)

    def test_the_public_socket_is_loopback_only(self):
        """Funnel dials it locally; binding it to the LAN would publish a
        second port whose design assumes Funnel's HTTPS in front."""
        with open(os.path.join(ROOT, "dashboard", "server.py")) as fh:
            src = fh.read()
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", PUBLIC_PORT), PublicHandler)', src)


class LoginPage(unittest.TestCase):
    def test_it_carries_the_nonce_and_leaks_no_addresses(self):
        """The one page a stranger on the internet may see.

        _SCOPE names this machine's LAN and tailnet addresses, so the sign-in
        page gets a neutral footer instead.
        """
        self.assertIn(f'nonce="{server.NONCE}"', server.LOGIN_PAGE)
        self.assertNotIn("__NONCE__", server.LOGIN_PAGE)
        self.assertNotIn("__SCOPE__", server.LOGIN_PAGE)
        for addr in server.BINDS:
            if addr != "127.0.0.1":
                self.assertNotIn(addr, server.LOGIN_PAGE)

    def test_it_carries_none_of_the_dashboard(self):
        """No queue, no ledger, no state -- markup and a fetch to /login."""
        for marker in ("monSpanLbl", "renderLedger", "api/state"):
            self.assertNotIn(marker, server.LOGIN_PAGE)

    def test_it_posts_with_the_csrf_header(self):
        self.assertIn('"X-Smeltr": "1"', server.LOGIN_PAGE)


if __name__ == "__main__":
    unittest.main()
