#!/usr/bin/env python3
"""The page must still assemble (`bun run lint:page`).

`dashboard/server.py` inlines web/index.html + app.css + theme.js + app.js at
import. A missing or renamed asset is a blank screen behind a working HTTP 200,
so `_asset()` raises -- and this proves no placeholder survived either.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import server  # noqa: E402

PLACEHOLDERS = ("__NONCE__", "__SCOPE__", "__APP_CSS__", "__THEME_JS__", "__APP_JS__")
missing = [m for m in PLACEHOLDERS if m in server.PAGE]
if missing:
    sys.exit("unsubstituted placeholder(s): " + ", ".join(missing))
