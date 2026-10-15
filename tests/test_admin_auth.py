# tests/test_admin_auth.py
"""Admin dashboard authentication: password check, signed cookies, throttling."""

import os
import types
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from Thunder.server import admin_auth  # noqa: E402
from Thunder.vars import Var  # noqa: E402

PASSWORD = "correct horse battery staple"


class AdminPasswordTest(unittest.TestCase):
    def setUp(self):
        self._original = Var.WEB_ADMIN_PASSWORD
        self.addCleanup(lambda: setattr(Var, "WEB_ADMIN_PASSWORD", self._original))

    def test_correct_password_is_accepted(self):
        Var.WEB_ADMIN_PASSWORD = PASSWORD
        self.assertTrue(admin_auth.verify_admin_password(PASSWORD))
        self.assertTrue(admin_auth.password_is_configured())

    def test_wrong_password_is_rejected(self):
        Var.WEB_ADMIN_PASSWORD = PASSWORD
        self.assertFalse(admin_auth.verify_admin_password("nope"))
        self.assertFalse(admin_auth.verify_admin_password(""))
        self.assertFalse(admin_auth.verify_admin_password(None))

    def test_unset_password_never_authenticates(self):
        Var.WEB_ADMIN_PASSWORD = ""
        self.assertFalse(admin_auth.password_is_configured())
        self.assertFalse(admin_auth.verify_admin_password(""))
        self.assertFalse(admin_auth.verify_admin_password("anything"))

    def test_surrounding_whitespace_is_ignored(self):
        Var.WEB_ADMIN_PASSWORD = f"  {PASSWORD}  "
        self.assertTrue(admin_auth.verify_admin_password(PASSWORD))


class AdminSessionTest(unittest.TestCase):
    def test_round_trip(self):
        token = admin_auth.create_admin_session()
        self.assertTrue(admin_auth.verify_session_token(token))

    def test_tampered_signature_is_rejected(self):
        token = admin_auth.create_admin_session()
        issued, signature = token.split(".")
        self.assertFalse(admin_auth.verify_session_token(f"{issued}.{'0' * len(signature)}"))

    def test_tampered_issue_time_is_rejected(self):
        token = admin_auth.create_admin_session()
        issued, signature = token.split(".")
        self.assertFalse(admin_auth.verify_session_token(f"{int(issued) + 600}.{signature}"))

    def test_expired_session_is_rejected(self):
        token = admin_auth.create_admin_session(now=1000)
        self.assertTrue(admin_auth.verify_session_token(token, now=1000 + 60))
        self.assertFalse(
            admin_auth.verify_session_token(token, now=1000 + admin_auth.SESSION_TTL_SECONDS + 1)
        )

    def test_malformed_tokens_are_rejected(self):
        for token in ("", "nonsense", "12345", ".", "abc.def", None):
            self.assertFalse(admin_auth.verify_session_token(token))

    def test_cookie_is_httponly_and_samesite(self):
        kwargs = admin_auth.session_cookie_kwargs()
        self.assertTrue(kwargs["httponly"])
        self.assertEqual(kwargs["samesite"], "Lax")
        self.assertEqual(kwargs["max_age"], admin_auth.SESSION_TTL_SECONDS)
        self.assertEqual(kwargs["path"], "/")

    def test_cookie_is_secure_only_with_ssl(self):
        original = Var.HAS_SSL
        self.addCleanup(lambda: setattr(Var, "HAS_SSL", original))
        Var.HAS_SSL = True
        self.assertTrue(admin_auth.session_cookie_kwargs()["secure"])
        Var.HAS_SSL = False
        self.assertFalse(admin_auth.session_cookie_kwargs()["secure"])


class RequestSessionTest(unittest.TestCase):
    def _request(self, token=None):
        cookies = {admin_auth.COOKIE_NAME: token} if token is not None else {}
        return types.SimpleNamespace(cookies=cookies)

    def test_request_with_a_valid_cookie_is_authenticated(self):
        request = self._request(admin_auth.create_admin_session())
        self.assertTrue(admin_auth.verify_admin_session(request))

    def test_request_without_a_cookie_is_not_authenticated(self):
        self.assertFalse(admin_auth.verify_admin_session(self._request()))
        self.assertFalse(admin_auth.verify_admin_session(self._request("garbage")))


class LoginThrottleTest(unittest.TestCase):
    def setUp(self):
        self.ip = "198.51.100.33"
        admin_auth.clear_login_failures(self.ip)

    def tearDown(self):
        admin_auth.clear_login_failures(self.ip)

    def test_failures_accumulate_into_a_lockout(self):
        for _ in range(admin_auth.MAX_LOGIN_FAILURES):
            self.assertTrue(admin_auth.login_allowed(self.ip))
            admin_auth.record_login_failure(self.ip)

        self.assertFalse(admin_auth.login_allowed(self.ip))

    def test_the_window_rolls_over(self):
        for _ in range(admin_auth.MAX_LOGIN_FAILURES):
            admin_auth.record_login_failure(self.ip, now=1000)

        self.assertFalse(admin_auth.login_allowed(self.ip, now=1000))
        self.assertTrue(
            admin_auth.login_allowed(self.ip, now=1000 + admin_auth.LOGIN_WINDOW_SECONDS + 1)
        )

    def test_clearing_failures_restores_access(self):
        for _ in range(admin_auth.MAX_LOGIN_FAILURES):
            admin_auth.record_login_failure(self.ip)
        admin_auth.clear_login_failures(self.ip)
        self.assertTrue(admin_auth.login_allowed(self.ip))


if __name__ == "__main__":
    unittest.main()
