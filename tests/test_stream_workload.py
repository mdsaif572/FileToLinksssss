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

import os
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from Thunder.bot import work_loads
from Thunder.server import stream_routes as sr

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
                          fallback_message_id=None, on_fallback_message=None):
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


if __name__ == "__main__":
    unittest.main()
