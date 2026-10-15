# tests/test_ip_blocker.py
"""Caller-IP blocking: immediate in-memory effect, durable copy in Mongo."""

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

from Thunder.server import stream_routes as sr  # noqa: E402
from Thunder.utils import ip_blocker  # noqa: E402
from Thunder.utils.database import db  # noqa: E402
from Thunder.utils.metrics import metrics  # noqa: E402
from tests.test_stream_workload import FakeStreamer, make_data  # noqa: E402
from tests.test_link_lifecycle import HASH, _record  # noqa: E402

BLOCKED = "203.0.113.7"


def _async_noop(*args, **kwargs):
    async def _call(*_args, **_kwargs):
        return None

    return _call()


class IpBlockerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ip_blocker.clear_cache()
        self.addCleanup(ip_blocker.clear_cache)

    async def test_unlisted_caller_is_allowed(self):
        self.assertFalse(ip_blocker.is_ip_banned("198.51.100.1"))

    async def test_ban_is_immediate_and_persisted(self):
        with mock.patch.object(db, "add_banned_ip", new=_async_noop()):
            await ip_blocker.ban_ip(BLOCKED, reason="abuse", banned_by=1)

        self.assertTrue(ip_blocker.is_ip_banned(BLOCKED))
        self.assertEqual(ip_blocker.get_banned_ips()[0]["reason"], "abuse")

    async def test_whitespace_is_trimmed(self):
        with mock.patch.object(db, "add_banned_ip", new=_async_noop()):
            await ip_blocker.ban_ip(f"  {BLOCKED}  ")

        self.assertTrue(ip_blocker.is_ip_banned(BLOCKED))

    async def test_empty_address_is_rejected(self):
        with self.assertRaises(ValueError):
            await ip_blocker.ban_ip("   ")

    async def test_unban_restores_access(self):
        with mock.patch.object(db, "add_banned_ip", new=_async_noop()):
            await ip_blocker.ban_ip(BLOCKED)
        with mock.patch.object(db, "remove_banned_ip", new=_async_noop_returns(True)):
            self.assertTrue(await ip_blocker.unban_ip(BLOCKED))

        self.assertFalse(ip_blocker.is_ip_banned(BLOCKED))
        self.assertEqual(ip_blocker.get_banned_ips(), [])

    async def test_ban_stays_in_effect_when_the_database_is_down(self):
        with mock.patch.object(db, "add_banned_ip", side_effect=RuntimeError("mongo down")):
            await ip_blocker.ban_ip(BLOCKED)

        self.assertTrue(
            ip_blocker.is_ip_banned(BLOCKED),
            "a database problem must not silently reopen access",
        )

    async def test_load_restores_bans_after_a_restart(self):
        records = [{"ip": BLOCKED, "reason": "abuse"}]
        with mock.patch.object(db, "get_banned_ips", new=_async_returns(records)):
            loaded = await ip_blocker.load_banned_ips()

        self.assertEqual(loaded, 1)
        self.assertTrue(ip_blocker.is_ip_banned(BLOCKED))

    async def test_empty_address_is_never_banned(self):
        self.assertFalse(ip_blocker.is_ip_banned(""))


def _async_noop_returns(value):
    async def _call(*args, **kwargs):
        return value

    return _call


def _async_returns(value):
    async def _call(*args, **kwargs):
        return value

    return _call


class BannedCallerRouteTest(unittest.IsolatedAsyncioTestCase):
    """A blocked caller is refused before any pool slot is consumed."""

    async def asyncSetUp(self):
        self.data = make_data(2048)
        self.streamer = FakeStreamer(self.data)
        self._original_select = sr.select_optimal_client
        self._original_lookup = sr.get_file_by_hash
        self._original_limiter = sr.user_streams

        sr.select_optimal_client = lambda **kwargs: (0, self.streamer)
        sr.user_streams = sr.UserStreamLimiter(0)
        sr.get_file_by_hash = self._lookup

        self.record = _record(file_size=len(self.data))
        ip_blocker.clear_cache()

        from Thunder.bot import work_loads
        work_loads.clear()
        work_loads[0] = 0

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        sr.select_optimal_client = self._original_select
        sr.get_file_by_hash = self._original_lookup
        sr.user_streams = self._original_limiter
        ip_blocker.clear_cache()

    async def _lookup(self, public_hash, **kwargs):
        return self.record

    async def test_banned_caller_gets_403_without_using_a_slot(self):
        from Thunder.bot import work_loads

        with mock.patch.object(db, "add_banned_ip", new=_async_noop()):
            await ip_blocker.ban_ip("127.0.0.1")
        before = metrics.snapshot()["errors"].get("blocked_ip", 0)

        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 403)

        self.assertEqual(work_loads[0], 0, "no pool slot may be taken for a blocked caller")
        self.assertEqual(metrics.snapshot()["errors"].get("blocked_ip", 0), before + 1)

    async def test_unbanning_restores_streaming_without_a_restart(self):
        with mock.patch.object(db, "add_banned_ip", new=_async_noop()):
            await ip_blocker.ban_ip("127.0.0.1")
        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 403)

        with mock.patch.object(db, "remove_banned_ip", new=_async_noop_returns(True)):
            await ip_blocker.unban_ip("127.0.0.1")

        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(await resp.read(), self.data)


if __name__ == "__main__":
    unittest.main()
