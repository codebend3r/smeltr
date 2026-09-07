"""Username + password gate for the dashboard, and its signed session cookie.

Imported ONLY by ``server.py``. The decision path never loads it --
``tests/test_layering.py`` lists it in ``DASHBOARD_ONLY``.

OFF unless ``auth.json`` sits beside the ledger, so a loopback-only install and
every existing LAN bookmark behave exactly as before. The file is 0600 and
gitignored; it holds a PBKDF2-HMAC-SHA256 *verifier* and a cookie-signing
secret, never the password. It is refused 0600-or-stricter the same way
``notify.json`` is, for the same reason: what is in it is a credential.

Why PBKDF2 and not scrypt/argon2: no runtime dependency is allowed here, and
``hashlib.scrypt`` needs an OpenSSL that stock macOS python3.9 -- the floor the
CI matrix pins -- does not reliably ship. ``pbkdf2_hmac`` is pure stdlib
everywhere. 600k iterations of SHA-256 is the OWASP figure and costs ~0.2 s on
this Mac, which is the point: it is the per-guess cost an attacker pays.

The cookie is stateless and HMAC-signed rather than a server-side session
table, so ``./smeltr restart`` -- which happens on every UI edit -- does not
log the operator's iPad out. Rotating the password regenerates the secret,
which invalidates every outstanding cookie; that is the logout-everywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# Tuned so a guess is expensive and a login is not. Stored per-record, so
# raising it later does not invalidate an existing auth.json -- the file's own
# value is what verify() replays.
ITERATIONS = 600_000
SALT_BYTES = 16
DKLEN = 32
COOKIE_NAME = "smeltr_session"
# Long on purpose: the operator watches this from an iPad and a gate that
# re-prompts weekly is a gate that gets a weaker password.
SESSION_SECONDS = 30 * 24 * 3600

# Failed-attempt backoff, per peer address, in memory. A public listener gets
# knocked on; 600k iterations already makes online guessing slow, and this
# stops it consuming the thread pool as well. Cleared by a restart, which is
# fine -- the cost is bounded either way.
_FAIL_AFTER = 5
_FAIL_BASE = 5.0
_FAIL_CAP = 900.0
_fails: dict = {}
_fail_lock = threading.Lock()

_cfg = None
_cfg_lock = threading.Lock()


def path(smeltr_dir: str) -> str:
    return os.path.join(smeltr_dir, "auth.json")


def hash_password(password: str, iterations: int = ITERATIONS) -> dict:
    """A verifier for `password`. The plaintext is never returned or stored."""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=DKLEN
    )
    return {
        "algo": "pbkdf2_sha256",
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": dk.hex(),
    }


def write_config(smeltr_dir: str, username: str, password: str) -> str:
    """Write auth.json 0600 with a fresh verifier AND a fresh cookie secret.

    The new secret is what makes a password change log every device out; a
    rotation that left old cookies valid would not be a rotation.
    """
    rec = hash_password(password)
    rec["username"] = username
    rec["secret"] = secrets.token_hex(32)
    p = path(smeltr_dir)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(rec, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    reload_config()
    return p


def reload_config() -> None:
    global _cfg
    with _cfg_lock:
        _cfg = None


def _load(smeltr_dir: str):
    """The parsed auth.json, or None. Cached; a bad file is a hard None.

    A file readable by anyone else is REFUSED rather than used: it carries the
    verifier and the signing secret, and a signing secret another account can
    read is a cookie anyone can forge.
    """
    global _cfg
    with _cfg_lock:
        if _cfg is not None:
            return _cfg or None
        p = path(smeltr_dir)
        try:
            st = os.stat(p)
        except OSError:
            _cfg = {}
            return None
        if st.st_mode & 0o077:
            print(
                f"smeltr: {p} is group/world readable; refusing to use it. "
                f"chmod 600 it.",
                flush=True,
            )
            _cfg = {}
            return None
        try:
            with open(p) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            _cfg = {}
            return None
        if not all(
            isinstance(rec.get(k), str) for k in ("username", "salt", "hash", "secret")
        ):
            _cfg = {}
            return None
        _cfg = rec
        return rec


def enabled(smeltr_dir: str) -> bool:
    return _load(smeltr_dir) is not None


def username(smeltr_dir: str) -> str:
    rec = _load(smeltr_dir)
    return (rec or {}).get("username", "")


def verify(smeltr_dir: str, user: str, password: str) -> bool:
    """Constant-time check of both halves.

    The username is compared with compare_digest too. A plain ``==`` leaks the
    correct username through timing, and here the username is the only part an
    attacker cannot already guess from the URL.
    """
    rec = _load(smeltr_dir)
    if not rec:
        return False
    want_user = rec["username"].encode("utf-8", "surrogatepass")
    got_user = (user or "").encode("utf-8", "surrogatepass")
    user_ok = hmac.compare_digest(want_user, got_user)
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            (password or "").encode("utf-8"),
            bytes.fromhex(rec["salt"]),
            int(rec.get("iterations") or ITERATIONS),
            dklen=DKLEN,
        )
    except (ValueError, TypeError):
        return False
    # Both branches always run: no early return on a wrong username, or the
    # response time itself answers "is that the right user".
    pass_ok = hmac.compare_digest(dk, bytes.fromhex(rec["hash"]))
    return user_ok and pass_ok


# ------------------------------------------------------------------- cookies
def _sign(secret: str, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_cookie(smeltr_dir: str) -> str:
    rec = _load(smeltr_dir)
    if not rec:
        return ""
    exp = int(time.time()) + SESSION_SECONDS
    payload = f"v1.{exp}"
    return f"{payload}.{_sign(rec['secret'], payload)}"


def check_cookie(smeltr_dir: str, value: str) -> bool:
    rec = _load(smeltr_dir)
    if not rec or not value:
        return False
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_sign(rec["secret"], payload), parts[2]):
        return False
    try:
        return int(parts[1]) > time.time()
    except ValueError:
        return False


def parse_cookie_header(raw: str) -> str:
    """Our cookie's value out of a Cookie header, or ""."""
    for part in (raw or "").split(";"):
        name, _, val = part.strip().partition("=")
        if name == COOKIE_NAME:
            return val.strip().strip('"')
    return ""


# ------------------------------------------------------------- rate limiting
def blocked_for(peer: str) -> float:
    """Seconds this peer must wait, 0 if it may try now."""
    with _fail_lock:
        n, until = _fails.get(peer, (0, 0.0))
    return max(0.0, until - time.time())


def note_failure(peer: str) -> None:
    with _fail_lock:
        n, _ = _fails.get(peer, (0, 0.0))
        n += 1
        wait = 0.0
        if n >= _FAIL_AFTER:
            wait = min(_FAIL_CAP, _FAIL_BASE * (2 ** (n - _FAIL_AFTER)))
        _fails[peer] = (n, time.time() + wait)
        # Bounded: an attacker rotating source addresses must not grow this
        # without limit. Dropping the oldest costs them nothing they did not
        # already have, and keeps the process honest.
        if len(_fails) > 4000:
            for k in sorted(_fails, key=lambda k: _fails[k][1])[:1000]:
                _fails.pop(k, None)


def note_success(peer: str) -> None:
    with _fail_lock:
        _fails.pop(peer, None)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")
