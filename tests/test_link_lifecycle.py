# tests/test_link_lifecycle.py
"""Link expiry and revocation: /revoke, LINK_TTL_HOURS and the 410 responses."""

import asyncio
import datetime
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

from Thunder.bot import work_loads  # noqa: E402
from Thunder.bot.plugins.admin import _parse_public_hash  # noqa: E402
from Thunder.server import stream_routes as sr  # noqa: E402
from Thunder.utils.canonical_files import (default_expiry,  # noqa: E402
                                           record_unavailable_reason)
from Thunder.utils.metrics import metrics  # noqa: E402
from Thunder.vars import Var  # noqa: E402
from tests.test_stream_workload import FakeStreamer, make_data  # noqa: E402

HASH = "ab" * 10
UTC = datetime.timezone.utc


def _record(**overrides):
    now = datetime.datetime.now(UTC)
    record = {
        "public_hash": HASH,
        "file_unique_id": HASH + "extra",
        "unique_id": HASH + "extra",
        "canonical_message_id": 42,
        "file_id": "stored-file-id",
        "file_name": "sample.mkv",
        "mime_type": "video/x-matroska",
        "file_size": 4096,
        "first_source_chat_id": 555,
        "created_at": now,
        "expires_at": None,
        "revoked": False,
    }
    record.update(overrides)
    return record


class RecordStateTest(unittest.TestCase):
    def test_plain_record_is_servable(self):
        self.assertIsNone(record_unavailable_reason(_record()))

    def test_missing_record_reports_missing(self):
        self.assertEqual(record_unavailable_reason(None), "missing")

    def test_future_expiry_is_servable(self):
        expires = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)
        self.assertIsNone(record_unavailable_reason(_record(expires_at=expires)))

    def test_past_expiry_is_expired(self):
        expires = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1)
        self.assertEqual(record_unavailable_reason(_record(expires_at=expires)), "expired")

    def test_naive_datetime_from_mongo_is_handled(self):
        # pymongo returns naive UTC datetimes unless tz_aware is set
        naive_past = datetime.datetime.utcnow() - datetime.timedelta(hours=2)
        self.assertEqual(
            record_unavailable_reason(_record(expires_at=naive_past)), "expired"
        )

    def test_revoked_beats_expiry(self):
        expires = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)
        self.assertEqual(
            record_unavailable_reason(_record(expires_at=expires, revoked=True)), "revoked"
        )

    def test_default_expiry_follows_the_configured_ttl(self):
        original = Var.LINK_TTL_HOURS
        try:
            Var.LINK_TTL_HOURS = 0
            self.assertIsNone(default_expiry())

            Var.LINK_TTL_HOURS = 24
            now = datetime.datetime.now(UTC)
            expires = default_expiry(now)
            self.assertAlmostEqual(
                (expires - now).total_seconds(), 24 * 3600, delta=2
            )
        finally:
            Var.LINK_TTL_HOURS = original


class PublicHashParsingTest(unittest.TestCase):
    class FakeMessage:
        def __init__(self, text):
            self.text = text

    def test_bare_hash(self):
        self.assertEqual(_parse_public_hash(self.FakeMessage(f"/revoke {HASH}")), HASH)

    def test_full_link(self):
        text = f"/revoke https://example.com/f/{HASH}/sample.mkv"
        self.assertEqual(_parse_public_hash(self.FakeMessage(text)), HASH)

    def test_uppercase_is_normalised(self):
        self.assertEqual(_parse_public_hash(self.FakeMessage(f"/revoke {HASH.upper()}")), HASH)

    def test_missing_argument(self):
        self.assertIsNone(_parse_public_hash(self.FakeMessage("/revoke")))


class CanonicalRouteStateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = make_data(2048)
        self.streamer = FakeStreamer(self.data)
        self._original_select = sr.select_optimal_client
        self._original_lookup = sr.get_file_by_hash
        self._original_limiter = sr.user_streams

        sr.select_optimal_client = lambda **kwargs: (0, self.streamer)
        sr.user_streams = sr.UserStreamLimiter(0)
        sr.get_file_by_hash = self._lookup

        work_loads.clear()
        work_loads[0] = 0

        self.record = _record(file_size=len(self.data))

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        sr.select_optimal_client = self._original_select
        sr.get_file_by_hash = self._original_lookup
        sr.user_streams = self._original_limiter
        work_loads.clear()

    async def _lookup(self, public_hash, **kwargs):
        return self.record

    async def test_servable_record_streams(self):
        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            body = await resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, self.data)

    async def test_revoked_record_returns_410(self):
        self.record = _record(file_size=len(self.data), revoked=True)

        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 410)
            self.assertIn("revoked", (await resp.text()).lower())

        async with self.client.get(f"/watch/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 410)

    async def test_expired_record_returns_410_on_both_routes(self):
        expired = datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)
        self.record = _record(file_size=len(self.data), expires_at=expired)

        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 410)
            self.assertIn("expired", (await resp.text()).lower())

        async with self.client.get(f"/watch/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 410)

    async def test_gone_requests_do_not_leak_workload(self):
        self.record = _record(file_size=len(self.data), revoked=True)
        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            self.assertEqual(resp.status, 410)

        self.assertEqual(work_loads[0], 0)
        self.assertGreaterEqual(metrics.snapshot()["errors"].get("gone", 0), 1)


if __name__ == "__main__":
    unittest.main()
