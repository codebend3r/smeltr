"""The four gates standing between the LAN and a deletion, none of them tested.

The dashboard binds a LAN address by default and its POST handlers spawn
HandBrake, kill it, queue NAS pulls, and un-skip titles (which re-arms a
deletion). Four checks guard that, and until this suite none had a test:

  _host_ok      DNS-rebinding: a browser pointed at an attacker's name that
                resolves to our LAN IP arrives with THAT name in Host.
  _token_ok     the shared secret, compared in constant time over BYTES.
  _writes_ok    network peers are read-only unless SMELTR_LAN_WRITES=1 --
                EXCEPT /api/pause, which any device holding the URL may call.
  _is_private   which addresses may be bound at all -- a VPN or public
                address must fail CLOSED to loopback, because the token
                crosses the wire in cleartext.

The gates are pure functions of the request, so they are exercised directly on
a Handler built without a socket. Driving them over a real connection would
test http.server, not this.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import server


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeSocket:
    def __init__(self, local):
        self._local = local

    def getsockname(self):
        return (self._local, 8787)


def handler(host=None, peer="127.0.0.1", local="127.0.0.1"):
    """A Handler with just enough attributes for the gates, and no socket."""
    h = object.__new__(server.Handler)
    h.headers = FakeHeaders({} if host is None else {"Host": host})
    h.client_address = (peer, 54321)
    h.connection = FakeSocket(local)
    return h


class HostGate(unittest.TestCase):
    """A Host the server does not answer to is a rebinding attempt."""

    def test_plain_and_ported_loopback_pass(self):
        for host in ("127.0.0.1", "127.0.0.1:8787", "localhost", "localhost:8787"):
            with self.subTest(host=host):
                self.assertTrue(handler(host)._host_ok())

    def test_case_and_trailing_dot_are_the_same_host(self):
        """Some resolvers append the root label; some clients upper-case."""
        for host in ("LOCALHOST", "localhost.", "LocalHost.:8787"):
            with self.subTest(host=host):
                self.assertTrue(handler(host)._host_ok())

    def test_ipv6_literal_keeps_its_brackets(self):
        """Neither split(':')[0] nor a colon count strips an IPv6 port."""
        self.assertTrue(handler("[::1]")._host_ok())
        self.assertTrue(handler("[::1]:8787")._host_ok())

    def test_absent_host_is_refused(self):
        """HTTP/1.1 requires Host; an absent one cannot match the allowlist."""
        self.assertFalse(handler(None)._host_ok())
        self.assertFalse(handler("")._host_ok())

    def test_foreign_host_is_refused(self):
        for host in (
            "evil.example.com",
            "evil.example.com:8787",
            "127.0.0.1.evil.com",
            "localhost.evil.com",
        ):
            with self.subTest(host=host):
                self.assertFalse(handler(host)._host_ok())


class TokenGate(unittest.TestCase):
    def test_correct_token_passes_when_required(self):
        with mock.patch.object(server, "REQUIRE_TOKEN", True):
            self.assertTrue(handler()._token_ok({"t": [server.TOKEN]}))

    def test_wrong_absent_and_empty_tokens_fail(self):
        with mock.patch.object(server, "REQUIRE_TOKEN", True):
            h = handler()
            self.assertFalse(h._token_ok({}))
            self.assertFalse(h._token_ok({"t": [""]}))
            self.assertFalse(h._token_ok({"t": ["not-the-token"]}))
            self.assertFalse(h._token_ok({"t": [server.TOKEN + "x"]}))

    def test_non_ascii_token_is_rejected_not_raised(self):
        """The regression this encoding exists for: compare_digest raises
        TypeError on a non-ASCII str, which killed the handler thread with no
        response and appended an unbounded traceback to the log -- sprayable
        by any local page."""
        with mock.patch.object(server, "REQUIRE_TOKEN", True):
            h = handler()
            for bad in ("café", "\udcff", "🔑"):
                with self.subTest(token=bad):
                    self.assertFalse(h._token_ok({"t": [bad]}))

    def test_loopback_only_run_needs_no_token(self):
        with mock.patch.object(server, "REQUIRE_TOKEN", False):
            self.assertTrue(handler()._token_ok({}))


class WriteGate(unittest.TestCase):
    """Un-skipping a title re-arms a deletion. Read-only is the LAN default."""

    def test_loopback_always_writes(self):
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertTrue(
                handler(peer="127.0.0.1", local="127.0.0.1")._writes_ok(
                    "/api/queue/skip"
                )
            )

    def test_lan_peer_is_read_only_by_default(self):
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertFalse(
                handler(peer="192.168.1.50", local="192.168.1.9")._writes_ok(
                    "/api/queue/skip"
                )
            )

    def test_lan_peer_may_pause(self):
        """The one write allowed off-box. Its worst case is the pipeline
        WAITING -- it cannot delete, encode, reorder or stage. The operator
        watches this on an iPad and could not stop the job from it."""
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertTrue(
                handler(peer="192.168.1.50", local="192.168.1.9")._writes_ok(
                    "/api/pause"
                )
            )

    def test_lan_peer_may_pause_but_still_not_delete_or_encode(self):
        """Every OTHER mutating route stays on this Mac. Un-skipping re-arms a
        ~90 GB deletion; encode control spawns and kills HandBrake."""
        with mock.patch.object(server, "LAN_WRITES", False):
            h = handler(peer="192.168.1.50", local="192.168.1.9")
            for route in (
                "/api/queue/skip",
                "/api/queue/order",
                "/api/queue/crf",
                "/api/encode/start",
                "/api/encode/abort",
                "/api/stage/start",
                "/api/stage/cancel",
            ):
                self.assertFalse(h._writes_ok(route), route)

    def test_the_crf_picker_is_NOT_lan_writable(self):
        """It looks like a preference and is not. A CRF chosen too high
        produces an encode the verdict legitimately calls `good`, which syncs
        and replaces a ~90 GB original with a worse picture -- the same class
        of consequence as un-skipping, not the same class as pausing."""
        self.assertNotIn("/api/queue/crf", server.LAN_WRITE_ROUTES)

    def test_an_unnamed_route_gets_the_STRICT_answer(self):
        """A caller that forgets the route must not fall through to the loose
        branch. A new endpoint is refused off-box until it is listed."""
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertFalse(
                handler(peer="192.168.1.50", local="192.168.1.9")._writes_ok()
            )

    def test_lan_peer_may_start_the_driver(self):
        """Added 2026-08-31 with the big play/pause toggle. The operator
        presses play from the iPad, and even this Mac's own browser arrives
        via the LAN URL. Start's worst case is the pipeline running exactly
        as designed -- the same class as resume, already LAN-allowed."""
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertTrue(
                handler(peer="192.168.1.50", local="192.168.1.9")._writes_ok(
                    "/api/driver/start"
                )
            )

    def test_the_lan_routes_are_spelled_the_way_do_POST_dispatches_them(self):
        """LAN_WRITE_ROUTES is matched against parsed.path, so a typo here
        would silently re-lock the iPad rather than fail loudly."""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "dashboard",
            "server.py",
        )
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        for route in ("/api/pause", "/api/driver/start"):
            self.assertIn(route, server.LAN_WRITE_ROUTES)
            self.assertIn('parsed.path == "%s"' % route, src)
        self.assertIn("self._writes_ok(parsed.path)", src)

    def test_this_mac_over_its_own_lan_url_writes(self):
        """Source address == the listener's own address is this Mac talking to
        itself: the operator opened the LAN URL in a local browser. A remote
        peer cannot spoof it over TCP -- the SYN-ACK routes back to us."""
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertTrue(
                handler(peer="192.168.1.9", local="192.168.1.9")._writes_ok(
                    "/api/queue/skip"
                )
            )

    def test_opt_in_lets_the_lan_write(self):
        with mock.patch.object(server, "LAN_WRITES", True):
            self.assertTrue(
                handler(peer="192.168.1.50", local="192.168.1.9")._writes_ok(
                    "/api/queue/skip"
                )
            )


class PrivateAddressGate(unittest.TestCase):
    """What may be bound. The token crosses the wire in cleartext, so anything
    that is not demonstrably a home LAN must fail closed to loopback."""

    def test_rfc1918_and_cgnat_are_lan(self):
        for addr in (
            "192.168.1.9",
            "10.0.0.4",
            "172.16.5.5",
            "172.31.255.254",
            "100.64.0.1",
            "100.127.255.255",
        ):
            with self.subTest(addr=addr):
                self.assertTrue(server._is_private(addr))

    def test_public_and_link_local_are_not_lan(self):
        for addr in (
            "8.8.8.8",
            "1.1.1.1",
            "169.254.10.5",
            "172.32.0.1",
            "100.128.0.1",
            "203.0.113.5",
        ):
            with self.subTest(addr=addr):
                self.assertFalse(server._is_private(addr))

    def test_garbage_is_not_lan(self):
        for addr in ("", "not-an-ip", "999.1.1.1", "::1"):
            with self.subTest(addr=addr):
                self.assertFalse(server._is_private(addr))

    def test_tunnel_interfaces_are_named_for_exclusion(self):
        """Tailscale hands out 100.64/10, which _is_private accepts for real
        CGNAT homes. The interface filter is what stops a VPN address being
        bound anyway -- if it is emptied, every tunnel becomes bindable."""
        for iface in ("utun", "tun", "ipsec", "awdl"):
            self.assertIn(iface, server._TUNNEL_IFACES)


class Allowlist(unittest.TestCase):
    def test_loopback_names_are_always_answerable(self):
        """A LAN bind must not KILL loopback: local bookmarks, CLAUDE.md and
        the terminal report all say 127.0.0.1."""
        for name in ("127.0.0.1", "localhost", "[::1]"):
            self.assertIn(name, server.ALLOWED_HOSTS)

    def test_allowlist_is_lowercased_and_undotted(self):
        for host in server.ALLOWED_HOSTS:
            self.assertEqual(host, host.lower())
            self.assertFalse(host.endswith("."))


class DriverStart(unittest.TestCase):
    """_apply_driver_start spawns the PIPELINE. Every path here mocks the
    spawn out -- a test must never launch the real driver."""

    @staticmethod
    def _h():
        return object.__new__(server.Handler)

    def test_refuses_while_the_driver_is_alive(self):
        with (
            mock.patch.object(server, "_driver_pids", return_value=[123]),
            mock.patch.object(server.subprocess, "Popen") as pop,
        ):
            err = self._h()._apply_driver_start({})
        self.assertIn("already running", err)
        pop.assert_not_called()

    def test_refuses_while_an_encode_runs(self):
        """A driver started beside a dashboard encode would pick and start a
        SECOND encode; only one may ever run."""
        with (
            mock.patch.object(server, "_driver_pids", return_value=[]),
            mock.patch.object(
                server.core, "live_encodes", return_value=[{"title": "x"}]
            ),
            mock.patch.object(server.subprocess, "Popen") as pop,
        ):
            err = self._h()._apply_driver_start({})
        self.assertIn("encode", err)
        pop.assert_not_called()

    def test_refuses_with_no_staging_drive(self):
        with (
            mock.patch.object(server, "_driver_pids", return_value=[]),
            mock.patch.object(server.core, "live_encodes", return_value=[]),
            mock.patch.object(server.core, "X9", "/nonexistent/smeltr-test-x9"),
            mock.patch.object(server.subprocess, "Popen") as pop,
        ):
            err = self._h()._apply_driver_start({})
        self.assertIn("not mounted", err)
        pop.assert_not_called()

    def test_launch_clears_pause_stale_lock_and_detaches(self):
        """Success: the stale mkdir lock is cleared (kill -9 skips the
        driver's trap), the pause flag is cleared (play means play), and the
        spawn is detached with cwd on the X9 -- the driver must survive a
        dashboard restart."""
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, ".autopilot.lock"))
            with (
                mock.patch.object(server, "_driver_pids", return_value=[]),
                mock.patch.object(server.core, "live_encodes", return_value=[]),
                mock.patch.object(server.core, "X9", d),
                mock.patch.object(server.core, "set_paused") as sp,
                mock.patch.object(server.subprocess, "Popen") as pop,
            ):
                err = self._h()._apply_driver_start({})
            self.assertIsNone(err)
            self.assertFalse(os.path.isdir(os.path.join(d, ".autopilot.lock")))
            sp.assert_called_once_with(False)
            kw = pop.call_args.kwargs
            self.assertEqual(kw["cwd"], d)
            self.assertTrue(kw["start_new_session"])
            self.assertEqual(pop.call_args.args[0], ["./.autopilot.sh"])


if __name__ == "__main__":
    unittest.main()
