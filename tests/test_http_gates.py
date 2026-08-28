"""The four gates standing between the LAN and a deletion, none of them tested.

The dashboard binds a LAN address by default and its POST handlers spawn
HandBrake, kill it, queue NAS pulls, and un-skip titles (which re-arms a
deletion). Four checks guard that, and until this suite none had a test:

  _host_ok      DNS-rebinding: a browser pointed at an attacker's name that
                resolves to our LAN IP arrives with THAT name in Host.
  _token_ok     the shared secret, compared in constant time over BYTES.
  _writes_ok    network peers are read-only unless SMELTR_LAN_WRITES=1.
  _is_private   which addresses may be bound at all -- a VPN or public
                address must fail CLOSED to loopback, because the token
                crosses the wire in cleartext.

The gates are pure functions of the request, so they are exercised directly on
a Handler built without a socket. Driving them over a real connection would
test http.server, not this.
"""
import os
import sys
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
        for host in ("127.0.0.1", "127.0.0.1:8787", "localhost",
                     "localhost:8787"):
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
        for host in ("evil.example.com", "evil.example.com:8787",
                     "127.0.0.1.evil.com", "localhost.evil.com"):
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
            self.assertTrue(handler(peer="127.0.0.1", local="127.0.0.1")
                            ._writes_ok())

    def test_lan_peer_is_read_only_by_default(self):
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertFalse(handler(peer="192.168.1.50", local="192.168.1.9")
                             ._writes_ok())

    def test_this_mac_over_its_own_lan_url_writes(self):
        """Source address == the listener's own address is this Mac talking to
        itself: the operator opened the LAN URL in a local browser. A remote
        peer cannot spoof it over TCP -- the SYN-ACK routes back to us."""
        with mock.patch.object(server, "LAN_WRITES", False):
            self.assertTrue(handler(peer="192.168.1.9", local="192.168.1.9")
                            ._writes_ok())

    def test_opt_in_lets_the_lan_write(self):
        with mock.patch.object(server, "LAN_WRITES", True):
            self.assertTrue(handler(peer="192.168.1.50", local="192.168.1.9")
                            ._writes_ok())


class PrivateAddressGate(unittest.TestCase):
    """What may be bound. The token crosses the wire in cleartext, so anything
    that is not demonstrably a home LAN must fail closed to loopback."""

    def test_rfc1918_and_cgnat_are_lan(self):
        for addr in ("192.168.1.9", "10.0.0.4", "172.16.5.5", "172.31.255.254",
                     "100.64.0.1", "100.127.255.255"):
            with self.subTest(addr=addr):
                self.assertTrue(server._is_private(addr))

    def test_public_and_link_local_are_not_lan(self):
        for addr in ("8.8.8.8", "1.1.1.1", "169.254.10.5", "172.32.0.1",
                     "100.128.0.1", "203.0.113.5"):
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


if __name__ == "__main__":
    unittest.main()
