#!/usr/bin/env python3
"""Write auth.json beside the ledger: the dashboard's sign-in credential.

    ./smeltr set-password [username]

Prompts twice for the password with no echo. The plaintext is never written,
logged, or passed as an argument -- an argv password is visible in `ps` to
every process on the machine and lands in shell history.

Rotating regenerates the cookie-signing secret too, so every signed-in device
is logged out. Delete auth.json to turn sign-in off entirely.
"""

from __future__ import annotations

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import auth  # noqa: E402
from pipeline import core  # noqa: E402

MIN_LEN = 8


def main() -> int:
    user = sys.argv[1] if len(sys.argv) > 1 else ""
    if not user:
        user = input("username: ").strip()
    if not user:
        print("no username; nothing written", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print(
            "refusing to read a password from a pipe -- run this in a terminal",
            file=sys.stderr,
        )
        return 1
    pw = getpass.getpass("password: ")
    if len(pw) < MIN_LEN:
        print(f"password must be at least {MIN_LEN} characters", file=sys.stderr)
        return 1
    if pw != getpass.getpass("password again: "):
        print("passwords did not match; nothing written", file=sys.stderr)
        return 1
    p = auth.write_config(core.SMELTR_DIR, user, pw)
    print(f"wrote {p} (0600) for {user!r}")
    print("every signed-in device was logged out; run `./smeltr restart`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
