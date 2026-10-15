# tests/test_web_admin_dashboard.py
"""Web admin dashboard: auth guard, stats API, IP ban/unban and /healthz."""

import os
import unittest
from unittest import mock

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from Thunder.bot import client_channels, work_loads  # noqa: E402
from Thunder.server import admin_auth, admin_routes  # noqa: E402
from Thunder.server import stream_routes as sr  # noqa: E402
from Thunder.utils import ip_blocker  # noqa: E402
from Thunder.utils.database import db  # noqa: E402
from Thunder.utils.metrics import metrics  # noqa: E402
from Thunder.vars import Var  # noqa: E402
from tests.test_link_lifecycle import HASH, _record  # noqa: E402
from tests.test_stream_workload import FakeStreamer, make_data  # noqa: E402

PASSWORD = "s3cret-admin-pass"


def _async_ok(*args, **kwargs):
    async def _call(*_args, **_kwargs):
        return True

    return _call()


class DashboardWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_password = Var.WEB_ADMIN_PASSWORD
        self._original_ssl = Var.HAS_SSL
        Var.WEB_ADMIN_PASSWORD = PASSWORD
        Var.HAS_SSL = False          # the test client speaks plain http
        self.addCleanup(lambda: setattr(Var, "WEB_ADMIN_PASSWORD", self._original_password))
        self.addCleanup(lambda: setattr(Var, "HAS_SSL", self._original_ssl))

        ip_blocker.clear_cache()
        admin_auth.clear_login_failures("127.0.0.1")
        self.addCleanup(ip_blocker.clear_cache)
        self.addCleanup(admin_auth.clear_login_failures, "127.0.0.1")

        # streaming side, so a ban can be proven to take effect end to end
        self.data = make_data(2048)
        self.streamer = FakeStreamer(self.data)
        self._original_select = sr.select_optimal_client
        self._original_lookup = sr.get_file_by_hash
        self._original_limiter = sr.user_streams
        sr.select_optimal_client = lambda **kwargs: (0, self.streamer)
        sr.user_streams = sr.UserStreamLimiter(0)
        sr.get_file_by_hash = self._lookup
        self.record = _record(file_size=len(self.data))

        self._original_clients = dict(sr.multi_clients)
        sr.multi_clients.clear()
        sr.multi_clients[0] = type("FakeClient", (), {"me": None, "storage": None})()
        work_loads.clear()
        work_loads[0] = 2
        client_channels.clear()
        client_channels[0] = frozenset({int(Var.BIN_CHANNEL)})

        app = web.Application()
        app.add_routes(admin_routes.routes)
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        sr.select_optimal_client = self._original_select
        sr.get_file_by_hash = self._original_lookup
        sr.user_streams = self._original_limiter
        sr.multi_clients.clear()
        sr.multi_clients.update(self._original_clients)
        client_channels.clear()
        work_loads.clear()

    async def _lookup(self, public_hash, **kwargs):
        return self.record

    async def _login(self):
        return await self.client.post(
            "/admin/login", data={"password": PASSWORD}, allow_redirects=False
        )

    # ---- login view -----------------------------------------------------
    async def test_login_page_renders(self):
        async with self.client.get("/admin") as resp:
            body = await resp.text()
            self.assertEqual(resp.status, 200)
            self.assertIn("Cloud Station Admin", body)
            self.assertNotIn("WEB_ADMIN_PASSWORD is not set", body)

    async def test_login_page_warns_when_no_password_is_configured(self):
        Var.WEB_ADMIN_PASSWORD = ""
        async with self.client.get("/admin") as resp:
            self.assertIn("WEB_ADMIN_PASSWORD is not set", await resp.text())

        response = await self.client.post(
            "/admin/login", data={"password": "anything"}, allow_redirects=False
        )
        self.assertEqual(response.status, 302)
        self.assertNotIn(admin_auth.COOKIE_NAME, response.cookies)

    # ---- sessions -------------------------------------------------------
    async def test_wrong_password_is_rejected(self):
        response = await self.client.post(
            "/admin/login", data={"password": "wrong"}, allow_redirects=False
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/admin?error=1")

        redirect = await self.client.get("/admin/dashboard", allow_redirects=False)
        self.assertEqual(redirect.status, 302)

    async def test_login_then_dashboard_then_logout(self):
        response = await self._login()

        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/admin/dashboard")
        cookie = response.cookies[admin_auth.COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

        async with self.client.get("/admin/dashboard") as dashboard:
            self.assertEqual(dashboard.status, 200)
            self.assertIn("Bot pool", await dashboard.text())

        logout = await self.client.post("/admin/logout", allow_redirects=False)
        self.assertEqual(logout.status, 302)

        after = await self.client.get("/admin/dashboard", allow_redirects=False)
        self.assertEqual(after.status, 302)

    async def test_repeated_failures_are_throttled(self):
        for _ in range(admin_auth.MAX_LOGIN_FAILURES):
            await self.client.post("/admin/login", data={"password": "wrong"},
                                   allow_redirects=False)

        response = await self.client.post(
            "/admin/login", data={"password": PASSWORD}, allow_redirects=False
        )
        self.assertEqual(response.status, 429)

    # ---- stats API ------------------------------------------------------
    async def test_stats_requires_a_session(self):
        async with self.client.get("/api/admin/stats") as resp:
            self.assertEqual(resp.status, 401)

    async def test_stats_payload_shape(self):
        await self._login()

        async with self.client.get("/api/admin/stats") as resp:
            data = await resp.json()

        self.assertEqual(resp.status, 200)
        self.assertEqual(data["server"]["version"], admin_routes.__version__)
        self.assertIn("streams", data["metrics"])
        self.assertEqual(len(data["bots"]), 1)
        self.assertEqual(data["bots"][0]["load"], 2)
        self.assertTrue(data["bots"][0]["channels"][0]["readable"])
        self.assertEqual(data["limits"]["per_user"], 0)
        self.assertIn("callers", data)
        self.assertIn("banned_ips", data)

    # ---- ban / unban ----------------------------------------------------
    async def test_ban_api_requires_a_session(self):
        response = await self.client.post("/api/admin/ban_ip", json={"ip": "198.51.100.9"})
        self.assertEqual(response.status, 401)
        self.assertFalse(ip_blocker.is_ip_banned("198.51.100.9"))

    async def test_ban_then_unban_controls_streaming(self):
        await self._login()

        async with self.client.get(f"/f/{HASH}/sample.mkv") as allowed:
            self.assertEqual(allowed.status, 200)
            await allowed.read()

        with mock.patch.object(db, "add_banned_ip", new=_async_ok):
            async with self.client.post("/api/admin/ban_ip",
                                        json={"ip": "127.0.0.1", "reason": "test"}) as banned:
                payload = await banned.json()
        self.assertEqual(banned.status, 200)
        self.assertEqual(payload["banned"]["ip"], "127.0.0.1")
        self.assertTrue(ip_blocker.is_ip_banned("127.0.0.1"))

        load_before_ban = work_loads[0]
        async with self.client.get(f"/f/{HASH}/sample.mkv") as blocked:
            self.assertEqual(blocked.status, 403)
        self.assertEqual(
            work_loads[0], load_before_ban,
            "a blocked caller must be refused before a slot is taken"
        )

        async with self.client.get("/api/admin/stats") as stats:
            data = await stats.json()
        self.assertEqual([entry["ip"] for entry in data["banned_ips"]], ["127.0.0.1"])

        with mock.patch.object(db, "remove_banned_ip", new=_async_ok):
            async with self.client.post("/api/admin/unban_ip",
                                        json={"ip": "127.0.0.1"}) as unbanned:
                self.assertEqual(unbanned.status, 200)

        async with self.client.get(f"/f/{HASH}/sample.mkv") as restored:
            self.assertEqual(restored.status, 200)
            self.assertEqual(await restored.read(), self.data)

    async def test_ban_requires_an_ip(self):
        await self._login()
        async with self.client.post("/api/admin/ban_ip", json={}) as resp:
            self.assertEqual(resp.status, 400)

    # ---- health ---------------------------------------------------------
    async def test_healthz_is_public(self):
        async with self.client.get("/healthz") as resp:
            data = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["clients"], 1)
        self.assertIn("active_streams", data)

    # ---- metrics --------------------------------------------------------
    async def test_healthz_does_not_expose_configuration(self):
        async with self.client.get("/healthz") as resp:
            body = await resp.text()
        for secret in ("BOT_TOKEN", "PASSWORD", "DATABASE_URL", Var.BOT_TOKEN):
            self.assertNotIn(secret, body)


if __name__ == "__main__":
    unittest.main()
