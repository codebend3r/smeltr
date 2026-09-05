#!/usr/bin/env python3
"""`bun run build` -- Smeltr has no compile step, so "build" means: assemble
the page the browser receives and build the state payload the SSE stream
carries, without binding a socket or touching the running dashboard.

Fails if an asset is missing (`_asset()` raises), a placeholder survives, a CSP
nonce went missing, or `build_state()` cannot produce the summary and queue.
Read-only against the ledger, the X9 and the NAS.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import server  # noqa: E402

page = server._PAGE
left = [m for m in ("__APP_CSS__", "__THEME_JS__", "__APP_JS__") if m in page]
if left:
    sys.exit("build: unsubstituted placeholder(s): " + ", ".join(left))
nonces = page.count('nonce="__NONCE__"')
if nonces != 3:
    sys.exit("build: expected 3 CSP nonces, found %d" % nonces)
state = server.build_state()
for key in ("summary", "queue"):
    if key not in state:
        sys.exit(
            "build: state payload has no %r (keys: %s)"
            % (key, ", ".join(sorted(state)))
        )
print("build ok: page %d bytes, state keys: %s" % (len(page), ", ".join(sorted(state))))
