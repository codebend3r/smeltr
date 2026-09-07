"""The favicon: three routes, one drawing, served without the token.

`web/favicon.svg` is the drawing. `tools/render_favicon.sh` renders it to the
two PNGs beside it with headless Chrome, and the server ships all three: the
SVG for Chrome and Firefox, the 32 px PNG wrapped in a 22-byte ICO container
for Safari (which cannot use an SVG favicon -- and Safari is the iPad, where
the dashboard is actually watched), and an opaque 180 px PNG for the iOS
home screen.

What is worth pinning here:

  the routes     every <link> href is a path the server serves. A link to a
                 path that 404s is a blank tab with no error anywhere.
  the gate       the icons are the ONLY paths besides /healthz that answer
                 without the token -- browsers probe /favicon.ico and
                 /apple-touch-icon.png on their own with no query string --
                 and it is a CLOSED list: a neighbouring path stays 403.
  the CSP        img-src is 'self', not 'none'. Firefox applies img-src to
                 the favicon fetch; 'none' there is a blank tab in one
                 browser and not the others, which nobody notices.
  the pixels     the tab icon's corners are transparent (it is a rounded
                 tile, not a hard square); the touch icon's are OPAQUE and
                 the SVG's own panel colour (iOS paints black under
                 transparency and then applies its own corner mask, so a
                 transparent tile shows black ears); and the centre of each
                 is orange -- a Chrome that failed to load the SVG
                 screenshots a blank page, which is still a valid PNG of
                 exactly the right size.

The PNG decoder below is 30 lines of stdlib (zlib + the five scanline
filters) so the pixel checks run wherever the Python suite runs, with no
imaging library and no browser.
"""

import io
import os
import re
import struct
import sys
import unittest
import zlib
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dashboard import server  # noqa: E402

WEB = os.path.join(REPO, "web")
PANEL = (15, 17, 22)  # #0f1116, the dashboard's --panel


def read(name: str) -> bytes:
    with open(os.path.join(WEB, name), "rb") as fh:
        return fh.read()


def png_decode(data: bytes):
    """(width, height, colour_type, pixel(x, y) -> (r, g, b, a)).

    8-bit, non-interlaced, colour type 2 (RGB) or 6 (RGBA) -- what Chrome
    writes. Anything else is a failure, not a fallback.
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, w, h, ctype = 8, b"", None, None, None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos : pos + 4])
        tag, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and interlace == 0, (depth, interlace)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    bpp = {2: 3, 6: 4}[ctype]
    raw, stride = zlib.decompress(idat), w * bpp
    rows, prev = [], bytearray(stride)
    for y in range(h):
        base = y * (stride + 1)
        f, line = raw[base], bytearray(raw[base + 1 : base + 1 + stride])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        rows.append(bytes(line))
        prev = line

    def pixel(x, y):
        o = x * bpp
        r, g, b = rows[y][o : o + 3]
        return r, g, b, (rows[y][o + 3] if bpp == 4 else 255)

    return w, h, ctype, pixel


def is_orange(rgb) -> bool:
    r, g, b = rgb
    return r > 200 and 60 < g < 180 and b < 120 and r > g > b


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeSocket:
    def getsockname(self):
        return ("127.0.0.1", 8787)


def get(path: str, host: str = "127.0.0.1:8787"):
    """Drive Handler.do_GET with no socket; (status, headers, body)."""
    h = object.__new__(server.Handler)
    h.headers = FakeHeaders({"Host": host})
    h.client_address = ("127.0.0.1", 54321)
    h.connection = FakeSocket()
    h.path, h.command = path, "GET"
    h.request_version, h.requestline = "HTTP/1.1", f"GET {path} HTTP/1.1"
    h.close_connection = False
    h.wfile = io.BytesIO()
    h.do_GET()
    head, _, body = h.wfile.getvalue().partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(": ")
        headers[k.lower()] = v
    return status, headers, body


class Links(unittest.TestCase):
    def head(self) -> str:
        return server.PAGE[: server.PAGE.index("</head>")]

    def icon_links(self):
        return re.findall(
            r"<link\s+rel=\"(icon|apple-touch-icon)\"([^>]*)>", self.head()
        )

    def test_three_icon_links_in_head(self):
        rels = [rel for rel, _ in self.icon_links()]
        self.assertEqual(rels, ["icon", "icon", "apple-touch-icon"])

    def test_every_href_is_a_served_route(self):
        """A <link> to a path the server 404s is a blank tab and no error."""
        hrefs = [
            re.search(r'href="([^"]+)"', attrs).group(1)
            for _, attrs in self.icon_links()
        ]
        self.assertEqual(
            hrefs, ["/favicon.ico", "/favicon.svg", "/apple-touch-icon.png"]
        )
        for href in hrefs:
            self.assertIn(href, server.ICONS)
        # And nothing is served that the page does not name.
        self.assertEqual(set(server.ICONS), set(hrefs))

    def test_ico_first_and_sized_svg_typed(self):
        """Safari ignores the SVG only if it can tell it is one before
        fetching (type=), and Chrome prefers the SVG's implicit any-size
        over the ICO's 32x32."""
        (_, ico), (_, svg), _ = self.icon_links()
        self.assertIn('href="/favicon.ico"', ico)
        self.assertIn('sizes="32x32"', ico)
        self.assertIn('href="/favicon.svg"', svg)
        self.assertIn('type="image/svg+xml"', svg)


class Files(unittest.TestCase):
    def test_svg_is_32_square_with_no_script_or_external_ref(self):
        svg = read("favicon.svg").decode("utf-8")
        self.assertIn('viewBox="0 0 32 32"', svg)
        self.assertNotIn("<script", svg)
        self.assertNotRegex(svg, r'href="(https?:|//)')

    def test_tab_icon_is_32_rgba_rounded_and_drawn(self):
        w, h, ctype, px = png_decode(read("favicon.png"))
        self.assertEqual((w, h, ctype), (32, 32, 6))
        for x, y in ((0, 0), (31, 0), (0, 31), (31, 31)):
            self.assertEqual(px(x, y)[3], 0, f"corner {x},{y} is not transparent")
        self.assertTrue(is_orange(px(16, 16)[:3]), px(16, 16))

    def test_touch_icon_is_180_opaque_panel_and_drawn(self):
        w, h, ctype, px = png_decode(read("apple-touch-icon.png"))
        self.assertEqual((w, h, ctype), (180, 180, 2))
        for x, y in ((0, 0), (179, 0), (0, 179), (179, 179)):
            self.assertEqual(px(x, y), PANEL + (255,), f"corner {x},{y}")
        self.assertTrue(is_orange(px(90, 90)[:3]), px(90, 90))

    def test_touch_background_is_the_svg_tile_colour(self):
        """tools/render_favicon.sh paints the page PANEL and favicon.svg
        fills its tile with a literal -- the two are typed separately, and
        the seam between them is invisible only while they agree."""
        svg = read("favicon.svg").decode("utf-8")
        tile = re.search(r'<rect [^>]*fill="#([0-9a-fA-F]{6})"', svg).group(1)
        self.assertEqual(tuple(int(tile[i : i + 2], 16) for i in (0, 2, 4)), PANEL)
        with open(os.path.join(REPO, "tools", "render_favicon.sh")) as fh:
            self.assertIn(f'PANEL="#{tile}"', fh.read())


class Ico(unittest.TestCase):
    def test_container_wraps_the_tab_png_verbatim(self):
        png = read("favicon.png")
        ico = server.ICONS["/favicon.ico"][1]
        self.assertEqual(struct.unpack("<HHH", ico[:6]), (0, 1, 1))
        w, h, colours, res, planes, bpp, size, off = struct.unpack(
            "<BBBBHHII", ico[6:22]
        )
        self.assertEqual((w, h, colours, res, planes, bpp), (32, 32, 0, 0, 1, 32))
        self.assertEqual((size, off), (len(png), 22))
        self.assertEqual(ico[22:], png)
        self.assertEqual(len(ico), 22 + len(png))


class Gate(unittest.TestCase):
    """With the token required, the icons answer and nothing else does."""

    def setUp(self):
        patcher = mock.patch.object(server, "REQUIRE_TOKEN", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_icons_answer_without_the_token(self):
        for route, (ctype, body) in server.ICONS.items():
            status, headers, got = get(route)
            self.assertEqual(status, 200, route)
            self.assertEqual(headers["content-type"], ctype, route)
            self.assertEqual(got, body, route)
            self.assertEqual(headers["content-length"], str(len(body)))

    def test_the_page_still_needs_a_credential(self):
        """No token, no session: never the dashboard.

        Which REFUSAL depends on whether sign-in is configured -- a 403 with
        auth off, the sign-in form with auth on -- so both are asserted, and
        the assertion that matters in both branches is that the dashboard
        itself did not come back. Pinning only one shape made this test pass
        or fail on whether the machine running it happened to have an
        auth.json beside its ledger.
        """
        status, _, body = get("/")
        if server.AUTH_ON:
            self.assertEqual(status, 200)
            self.assertIn(b"Sign in", body)
        else:
            self.assertEqual(status, 403)
        self.assertNotIn(b"monSpanLbl", body)

    def test_the_exemption_is_a_closed_list(self):
        """/favicon.png is a real file in web/ and NOT a route; it stays
        behind the token (403, not 404 -- the gate answers first)."""
        for path in (
            "/favicon.png",
            "/favicon.svg.bak",
            "/icons/x.svg",
            "/apple-touch-icon-precomposed.png",
        ):
            status, _, _ = get(path)
            self.assertEqual(status, 403, path)

    def test_host_allowlist_still_applies(self):
        status, _, _ = get("/favicon.ico", host="rebinder.example")
        self.assertEqual(status, 421)

    def test_page_csp_allows_same_origin_images_only(self):
        with mock.patch.object(server, "REQUIRE_TOKEN", False):
            _, headers, _ = get("/")
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("img-src 'self'", csp)
        self.assertNotIn("img-src 'none'", csp)


if __name__ == "__main__":
    unittest.main()
