# tests/test_stream_workload.py
"""Regression tests for the streaming route's workload accounting and range serving.

Covers two bugs that made the deployed bot unusable:

1. ``work_loads[client_id]`` was decremented twice on the failure path (once
   before raising ``HTTPServiceUnavailable`` in ``_serve_media_response`` and
   once again in the route handler). Loads drifted negative, ``/status``
   reported nonsense and the capacity gate in ``select_optimal_client()``
   stopped working.
2. The pre-fetched first chunk must line up with the requested byte range:
   every request for a media file goes through that path now.

Run with::

    python -m unittest discover -s tests -t .
"""

import asyncio
import os
import types
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from Thunder.bot import StreamBot, work_loads
from Thunder.server import stream_routes as sr
from Thunder.utils.metrics import metrics

CHUNK = 1024 * 1024
FILE_SIZE = 3 * CHUNK + 1234
SECURE_HASH = "abc123"
UNIQUE_ID = SECURE_HASH + "deadbeefcafe"
MEDIA_URL = f"/7?hash={SECURE_HASH}"


def make_data(size=FILE_SIZE):
    pattern = bytes(range(256))
    return (pattern * (size // len(pattern) + 1))[:size]


class FakeStreamer:
    """Stands in for ByteStreamer without touching Telegram.

    Mirrors the real chunking contract: ``offset``/``limit`` are chunk indices,
    each chunk is 1 MiB and chunks are aligned to the file start.
    """

    def __init__(self, data, fail=False):
        self.data = data
        self.fail = fail
        # the real ByteStreamer exposes the client it streams with
        self.client = types.SimpleNamespace(name="fake")
        self.chat_ids = []
        self.info = {
            "message_id": 7,
            "file_size": len(data),
            "file_name": "sample.bin",
            "mime_type": "application/octet-stream",
            "unique_id": UNIQUE_ID,
        }

    async def get_file_info(self, message_id):
        return self.info

    async def stream_file(self, media_ref, offset=0, limit=0,
                          fallback_message_id=None, on_fallback_message=None,
                          chat_id=None):
        self.chat_ids.append(chat_id)
        if self.fail:
            raise RuntimeError("simulated upstream stream failure")

        first = offset // CHUNK
        if limit > 0:
            last = (offset + limit - 1) // CHUNK
        else:
            last = (len(self.data) - 1) // CHUNK

        for index in range(first, last + 1):
            yield self.data[index * CHUNK:(index + 1) * CHUNK]


class StreamWorkloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = make_data()
        self.streamer = FakeStreamer(self.data)
        self._original_select = sr.select_optimal_client
        sr.select_optimal_client = lambda: (0, self.streamer)

        work_loads.clear()
        work_loads[0] = 0
        StreamBot.username = "test_bot"

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        sr.select_optimal_client = self._original_select

    def assertLoadReleased(self):
        self.assertEqual(
            work_loads[0], 0,
            "workload was not released exactly once (negative drift defeats the "
            "capacity gate in select_optimal_client)"
        )

    async def test_full_download_serves_whole_file_and_releases_load(self):
        async with self.client.get(MEDIA_URL) as resp:
            body = await resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(int(resp.headers["Content-Length"]), FILE_SIZE)
            self.assertEqual(body, self.data)
        self.assertLoadReleased()

    async def test_aligned_range_request(self):
        start, end = CHUNK, 2 * CHUNK - 1
        async with self.client.get(MEDIA_URL, headers={"Range": f"bytes={start}-{end}"}) as resp:
            body = await resp.read()
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.headers["Content-Range"], f"bytes {start}-{end}/{FILE_SIZE}")
            self.assertEqual(body, self.data[start:end + 1])
        self.assertLoadReleased()

    async def test_unaligned_range_request_skips_into_prefetched_chunk(self):
        start, end = CHUNK + 5000, CHUNK + 200000
        async with self.client.get(MEDIA_URL, headers={"Range": f"bytes={start}-{end}"}) as resp:
            body = await resp.read()
            self.assertEqual(resp.status, 206)
            self.assertEqual(body, self.data[start:end + 1])
        self.assertLoadReleased()

    async def test_head_request_releases_load(self):
        async with self.client.head(MEDIA_URL) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(int(resp.headers["Content-Length"]), FILE_SIZE)
        self.assertLoadReleased()

    async def test_metrics_count_the_stream(self):
        before = metrics.snapshot()["streams"]
        async with self.client.get(MEDIA_URL) as resp:
            await resp.read()

        after = metrics.snapshot()["streams"]
        self.assertEqual(after["started"], before["started"] + 1)
        self.assertEqual(after["completed"], before["completed"] + 1)
        self.assertEqual(after["bytes_served"], before["bytes_served"] + FILE_SIZE)

    async def test_status_endpoint_exposes_metrics(self):
        async with self.client.get("/status") as resp:
            data = await resp.json()

        self.assertIn("metrics", data)
        self.assertIn("streams", data["metrics"])
        self.assertIn("per_user_streams", data["resources"])

    async def test_stream_failure_returns_503_and_releases_load_once(self):
        sr.select_optimal_client = lambda: (0, FakeStreamer(self.data, fail=True))
        async with self.client.get(MEDIA_URL) as resp:
            self.assertEqual(resp.status, 503)
        self.assertLoadReleased()


class ClientSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_overloaded_client_is_not_selected(self):
        self._original_limit = sr.MAX_CONCURRENT_PER_CLIENT
        sr.MAX_CONCURRENT_PER_CLIENT = 2
        work_loads.clear()
        work_loads[0] = 2
        work_loads[1] = 1
        sr.multi_clients.setdefault(0, object())
        sr.multi_clients.setdefault(1, object())
        try:
            client_id, _ = sr.select_optimal_client()
            self.assertEqual(client_id, 1)
        finally:
            sr.MAX_CONCURRENT_PER_CLIENT = self._original_limit
            work_loads.clear()
            sr.multi_clients.pop(0, None)
            sr.multi_clients.pop(1, None)
            sr.streamers.clear()


class CallerKeyTest(unittest.TestCase):
    def _request(self, headers=None, remote="10.0.0.5"):
        return types.SimpleNamespace(headers=headers or {}, remote=remote)

    def test_falls_back_to_the_socket_address(self):
        self.assertEqual(sr.caller_key(self._request()), "10.0.0.5")

    def test_uses_the_last_forwarded_entry(self):
        # earlier entries are client-supplied and spoofable
        request = self._request({"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
        self.assertEqual(sr.caller_key(request), "203.0.113.9")

    def test_single_forwarded_entry(self):
        request = self._request({"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(sr.caller_key(request), "203.0.113.9")

    def test_empty_forwarded_header_falls_back(self):
        request = self._request({"X-Forwarded-For": "  "})
        self.assertEqual(sr.caller_key(request), "10.0.0.5")

    def test_missing_remote(self):
        self.assertEqual(sr.caller_key(self._request(remote=None)), "unknown")


class CallerScopedCapTest(unittest.TestCase):
    def test_callers_do_not_share_a_bucket(self):
        limiter = sr.UserStreamLimiter(1)
        self.assertTrue(limiter.acquire("198.51.100.1"))
        self.assertFalse(limiter.acquire("198.51.100.1"))
        self.assertTrue(
            limiter.acquire("198.51.100.2"),
            "one viewer must not consume another viewer's allowance"
        )


class FakeDcClient:
    def __init__(self, dc_id):
        self.storage = types.SimpleNamespace(dc_id=dc_id)


class DcAwareSelectionTest(unittest.TestCase):
    def setUp(self):
        self._original_limit = sr.MAX_CONCURRENT_PER_CLIENT
        sr.MAX_CONCURRENT_PER_CLIENT = 16
        self._original_clients = dict(sr.multi_clients)
        sr.multi_clients.clear()
        sr.streamers.clear()
        work_loads.clear()

    def tearDown(self):
        sr.MAX_CONCURRENT_PER_CLIENT = self._original_limit
        sr.multi_clients.clear()
        sr.multi_clients.update(self._original_clients)
        sr.streamers.clear()
        work_loads.clear()

    def test_prefers_a_client_in_the_files_data_centre(self):
        sr.multi_clients[0] = FakeDcClient(dc_id=2)
        sr.multi_clients[1] = FakeDcClient(dc_id=4)
        work_loads[0] = 0
        work_loads[1] = 3          # busier, but in the right DC

        client_id, _ = sr.select_optimal_client(preferred_dc=4)
        self.assertEqual(client_id, 1)

    def test_falls_back_to_the_least_loaded_client_without_a_dc_match(self):
        sr.multi_clients[0] = FakeDcClient(dc_id=2)
        sr.multi_clients[1] = FakeDcClient(dc_id=2)
        work_loads[0] = 5
        work_loads[1] = 1

        client_id, _ = sr.select_optimal_client(preferred_dc=9)
        self.assertEqual(client_id, 1)

    def test_full_client_in_the_right_dc_does_not_get_the_traffic(self):
        sr.multi_clients[0] = FakeDcClient(dc_id=4)
        sr.multi_clients[1] = FakeDcClient(dc_id=2)
        work_loads[0] = sr.MAX_CONCURRENT_PER_CLIENT
        work_loads[1] = 0

        client_id, _ = sr.select_optimal_client(preferred_dc=4)
        self.assertEqual(client_id, 1)

    def test_dc_from_file_id_handles_missing_and_broken_ids(self):
        self.assertIsNone(sr.dc_from_file_id(None))
        self.assertIsNone(sr.dc_from_file_id(""))
        self.assertIsNone(sr.dc_from_file_id(12345))
        self.assertIsNone(sr.dc_from_file_id("not-a-real-file-id"))

    def test_client_dc_reads_session_dc_id(self):
        client = types.SimpleNamespace(session=types.SimpleNamespace(dc_id=5))
        self.assertEqual(sr._client_dc(client), 5)

        # And handles callable storage.dc_id gracefully
        async def dummy_dc():
            return 5
        client_with_method = types.SimpleNamespace(storage=types.SimpleNamespace(dc_id=dummy_dc))
        self.assertIsNone(sr._client_dc(client_with_method))


class BlockingStreamer(FakeStreamer):
    """Keeps the first chunk in flight until the test releases it."""

    def __init__(self, data):
        super().__init__(data)
        self.release = asyncio.Event()

    async def stream_file(self, media_ref, offset=0, limit=0,
                          fallback_message_id=None, on_fallback_message=None,
                          chat_id=None):
        await self.release.wait()
        async for chunk in super().stream_file(
            media_ref, offset=offset, limit=limit,
            fallback_message_id=fallback_message_id,
            on_fallback_message=on_fallback_message,
            chat_id=chat_id
        ):
            yield chunk


class PerUserStreamCapTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = make_data()
        self.streamer = BlockingStreamer(self.data)
        self._original_select = sr.select_optimal_client
        self._original_limiter = sr.user_streams
        sr.select_optimal_client = lambda: (0, self.streamer)
        sr.user_streams = sr.UserStreamLimiter(1)

        work_loads.clear()
        work_loads[0] = 0

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.streamer.release.set()
        await self.client.close()
        sr.select_optimal_client = self._original_select
        sr.user_streams = self._original_limiter

    async def test_second_stream_from_the_same_caller_is_rejected(self):
        in_flight = asyncio.create_task(self.client.get(MEDIA_URL))
        await asyncio.sleep(0.05)  # let the first request take its slot

        async with self.client.get(MEDIA_URL) as second:
            self.assertEqual(second.status, 429)
            self.assertIn("Retry-After", second.headers)

        self.streamer.release.set()
        first = await in_flight
        self.assertEqual(first.status, 200)
        await first.read()

        self.assertEqual(
            sr.user_streams.total(), 0,
            "the per-user slot must be released when the stream ends"
        )

    async def test_disabled_limiter_lets_everything_through(self):
        sr.user_streams = sr.UserStreamLimiter(0)
        self.streamer.release.set()

        async with self.client.get(MEDIA_URL) as resp:
            self.assertEqual(resp.status, 200)
            await resp.read()


if __name__ == "__main__":
    unittest.main()
