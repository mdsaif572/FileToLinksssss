# Thunder/server/admin_auth.py
"""Password + signed-cookie authentication for the web admin dashboard.

No new dependencies: the session token is ``<issued_at>.<hmac>`` where the HMAC
is keyed by a secret generated at process start, so restarting the process
invalidates every session. Comparisons are constant time.
"""

import hashlib
import hmac
import secrets
import time
from typing import Optional

from Thunder.vars import Var

COOKIE_NAME = "cs_admin_session"
SESSION_TTL_SECONDS = 12 * 3600
LOGIN_WINDOW_SECONDS = 300.0
MAX_LOGIN_FAILURES = 8

# Regenerated per process: an old cookie stops working after a restart, which is
# the cheapest way to revoke everything without a session store.
_SESSION_KEY = secrets.token_bytes(32)
_login_failures: dict = {}


def password_is_configured() -> bool:
    return bool((getattr(Var, "WEB_ADMIN_PASSWORD", "") or "").strip())


def verify_admin_password(candidate: Optional[str]) -> bool:
    """Constant-time password check; False whenever nothing is configured."""
    expected = (getattr(Var, "WEB_ADMIN_PASSWORD", "") or "").strip()
    if not expected:
        return False
    return hmac.compare_digest(expected, (candidate or "").strip())


def _sign(issued_at: int) -> str:
    return hmac.new(_SESSION_KEY, f"admin:{issued_at}".encode(), hashlib.sha256).hexdigest()


def create_admin_session(now: Optional[float] = None) -> str:
    issued_at = int(now if now is not None else time.time())
    return f"{issued_at}.{_sign(issued_at)}"


def verify_session_token(token: Optional[str], now: Optional[float] = None) -> bool:
    if not token or "." not in token:
        return False

    issued_part, _, signature = token.partition(".")
    try:
        issued_at = int(issued_part)
    except ValueError:
        return False

    if not hmac.compare_digest(_sign(issued_at), signature):
        return False

    age = (now if now is not None else time.time()) - issued_at
    return 0 <= age <= SESSION_TTL_SECONDS


def verify_admin_session(request) -> bool:
    """True when the request carries a valid admin cookie."""
    try:
        token = request.cookies.get(COOKIE_NAME)
    except Exception:
        return False
    return verify_session_token(token)


def session_cookie_kwargs() -> dict:
    """Cookie attributes: not readable by JS, not sent cross-site."""
    return {
        "httponly": True,
        "samesite": "Lax",
        "secure": bool(getattr(Var, "HAS_SSL", False)),
        "max_age": SESSION_TTL_SECONDS,
        "path": "/",
    }


def login_allowed(ip: str, now: Optional[float] = None) -> bool:
    """Throttle password guessing: MAX_LOGIN_FAILURES per LOGIN_WINDOW_SECONDS."""
    now = now if now is not None else time.time()
    recent = [stamp for stamp in _login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    _login_failures[ip] = recent
    return len(recent) < MAX_LOGIN_FAILURES


def record_login_failure(ip: str, now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    _login_failures.setdefault(ip, []).append(now)


def clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)
