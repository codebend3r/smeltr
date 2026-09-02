#!/usr/bin/env bash
# Render web/favicon.svg into the two PNGs the dashboard serves beside it.
#
#   web/favicon.png            32x32, transparent corners  -> /favicon.ico
#   web/apple-touch-icon.png  180x180, opaque panel colour -> /apple-touch-icon.png
#
# The SVG is the drawing; these are derived and checked in, because the server
# reads them at import and must never need a rasteriser at runtime. Run this
# after editing favicon.svg and commit all three files together.
#
# Headless Chrome does the rasterising: the Chrome already on this Mac, the
# same one tests/visual/shoot.mjs drives, and a browser is the renderer that
# will actually draw the SVG in the tab, so the PNG Safari gets is what Chrome
# and Firefox draw from the vector. Nothing is installed.
#
# The touch icon is rendered onto the panel colour rather than transparent
# because iOS paints BLACK under transparent pixels and then applies its own
# corner mask, so a transparent-cornered tile would show black ears. The
# panel colour here must match the <rect> fill in favicon.svg;
# tests/test_favicon.py compares the two.
#
# tests/test_favicon.py pins the sizes, the corner alpha of each PNG, and
# that an ingot actually got drawn (a Chrome that failed to load the SVG
# screenshots a blank page, which is still a valid PNG of the right size).
set -euo pipefail
cd "$(dirname "$0")/.."

PANEL="#0f1116"
CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
  [ -x "$c" ] && { CHROME="$c"; break; }
done
if [ -z "$CHROME" ]; then
  echo "render_favicon: no Chrome or Chromium found; nothing rendered" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
svg="file://$PWD/web/favicon.svg"

# $1 pixel size, $2 page background (transparent or a colour), $3 output.
render() {
  cat > "$tmp/page.html" <<HTML
<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:$2}
img{display:block;width:$1px;height:$1px}
</style></head><body><img src="$svg"></body></html>
HTML
  rm -f "$3"
  # No --user-data-dir: headless Chrome makes its own temporary profile when
  # none is given, and WITH one --screenshot writes the file and then never
  # exits (measured: 2 s without, killed at 30 s with).
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --no-first-run --no-default-browser-check --disable-extensions \
    --force-device-scale-factor=1 \
    --default-background-color=00000000 --window-size="$1,$1" \
    --screenshot="$3" "file://$tmp/page.html" >/dev/null 2>&1 || true
  if [ ! -s "$3" ]; then
    echo "render_favicon: Chrome wrote nothing to $3" >&2
    exit 1
  fi
  echo "wrote $3 (${1}x${1})"
}

render 32  transparent web/favicon.png
render 180 "$PANEL"    web/apple-touch-icon.png
