# tests/test_stream_stall_recovery.py
"""Regression tests for the stalled-read failure that killed live streams.

The production log (2026-10-16) showed a viewer that received ~5 MB and then sat
silent until the Heroku router dropped the connection::

    2026-10-16T17:52:27 ... File "Thunder/utils/custom_dl.py", line 311, in stream_file
    Thunder.server.exceptions.FileNotFound: Unable to stream file:
    17:53:22 heroku[router]: at=error code=H15 desc="Idle connection" ... status=200 bytes=5243058

Two defects in that traceback, both covered here:

1. The message named no cause. The last error was ``asyncio.TimeoutError()``,
   whose ``str()`` is ``""``, so nothing in the log said *why* the read failed.
2. The recovery was useless by construction: five 20s attempts on the *same*
   session is up to 100s of silence, well past the gateway's 55s idle window -
   the retry could only ever be killed by the router. A session that just stalled
   is the one bound to whatever stalled it (FloodWait sleep, its rate limit), so
   the retry belongs on the other session - that is what the client pool is for.

There is a second group here: which client a request is served by. Picking
``min(load)`` alone always returned the lowest client id, so a pool of idle bots
served every stream through one session (the throttled one) while the rest sat
idle.

Run with::

    python -m unittest discover -s tests -t .
"""

import asyncio
import os
import time
import types
import unittest
from unittest import mock

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pyrogram.errors import FloodWait

import Thunder.bot
from Thunder.server import stream_routes as sr
from Thunder.server.exceptions import FileNotFound, StreamUnavailable
from Thunder.utils import custom_dl
from Thunder.utils.metrics import metrics

CHUNK = 1024 * 1024


class FakeMessage:
    def __init__(self, message_id, owner):
        self.id = message_id
        self.media = object()          # only truthiness is checked
        self.owner = owner


class StallClient:
    """A pyrogram client whose reads stall, flood-wait or work, on demand."""

    def __init__(self, name, *, stall_every=False, stall_at=None,
                 flood_wait=None, can_read=True):
        self.name = name
        self.stall_every = stall_every
        self.stall_at = stall_at
        self.flood_wait = flood_wait
        self.can_read = can_read
        self.fetch_calls = 0
        self.stream_runs = 0

    async def get_messages(self, chat_id, message_id):
        self.fetch_calls += 1
        if not self.can_read:
            raise RuntimeError(f"{self.name} cannot read the storage channel")
        return FakeMessage(message_id, self.name)

    def stream_media(self, message, offset=0, limit=0):
        self_stream = self

        async def generator():
            self_stream.stream_runs += 1
            if self_stream.flood_wait is not None:
                wait, self_stream.flood_wait = self_stream.flood_wait, None
                raise FloodWait(value=wait)

            count = limit if limit > 0 else 1
            for index in range(offset, offset + count):
                if self_stream.stall_every or self_stream.stall_at == index:
                    self_stream.stall_at = None
                    await asyncio.sleep(3600)      # a read that never returns
                yield b"chunk"

        return generator()


class StreamStallRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        custom_dl._GLOBAL_MSG_CACHE.clear()
        self.primary = StallClient("primary")
        patcher = mock.patch.object(Thunder.bot, "StreamBot", self.primary)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(custom_dl._GLOBAL_MSG_CACHE.clear)

    async def _collect(self, streamer, **kwargs):
        return [chunk async for chunk in streamer.stream_file(5, **kwargs)]

    async def test_a_stalled_read_resumes_on_the_other_session(self):
        # The production H15: the response sat silent until the router dropped it.
        client = StallClient("only", stall_at=1)
        streamer = custom_dl.ByteStreamer(client)

        with mock.patch.object(custom_dl, "CHUNK_READ_TIMEOUT", 0.05):
            chunks = await self._collect(streamer, offset=0, limit=CHUNK + 5)

        self.assertEqual(chunks, [b"chunk", b"chunk"])
        self.assertEqual(client.stream_runs, 1)
        self.assertEqual(
            self.primary.stream_runs, 1,
            "the retry must run on the other session: the one that stalled is "
            "bound to whatever stalled it (a FloodWait sleep, its rate limit)"
        )

    async def test_a_stream_that_never_delivers_data_gives_up_inside_the_budget(self):
        # Both sessions stall forever. Retrying past the gateway's idle window
        # cannot recover the stream, so the budget has to end it.
        client = StallClient("only", stall_every=True)
        self.primary.stall_every = True
        streamer = custom_dl.ByteStreamer(client)

        started = time.monotonic()
        with mock.patch.object(custom_dl, "CHUNK_READ_TIMEOUT", 0.05), \
                mock.patch.object(custom_dl, "STREAM_STALL_BUDGET_SECONDS", 0.12):
            with self.assertRaises(StreamUnavailable):
                await self._collect(streamer)
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed, 2.0,
            "the stall budget must bound the silence, not the retry count"
        )

    async def test_the_failure_names_its_cause_client_and_message(self):
        # The pasted production line was `Unable to stream file: ` - empty,
        # because str(asyncio.TimeoutError()) == "".
        streamer = custom_dl.ByteStreamer(StallClient("only", stall_every=True))
        self.primary.stall_every = True

        with mock.patch.object(custom_dl, "CHUNK_READ_TIMEOUT", 0.05), \
                mock.patch.object(custom_dl, "STREAM_STALL_BUDGET_SECONDS", 0.12):
            with self.assertRaises(StreamUnavailable) as ctx:
                await self._collect(streamer)

        detail = str(ctx.exception)
        self.assertTrue(detail.strip(), "a stream failure must say something")
        self.assertIn("TimeoutError", detail)
        self.assertIn("message 5", detail)
        self.assertIn("StallClient:", detail)
        self.assertIn("without data", detail)

    async def test_a_floodwait_longer_than_the_budget_is_not_slept(self):
        # FloodWait(300) used to be slept in place: the response sat silent for
        # five minutes holding a pool slot, and the router killed it at 55s.
        client = StallClient("only", flood_wait=300)
        streamer = custom_dl.ByteStreamer(client)

        started = time.monotonic()
        with mock.patch.object(custom_dl, "STREAM_STALL_BUDGET_SECONDS", 0.5):
            chunks = await asyncio.wait_for(self._collect(streamer), timeout=5)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, "a 300s FloodWait must not be slept in place")
        self.assertEqual(chunks, [b"chunk"])
        self.assertEqual(
            self.primary.stream_runs, 1,
            "an unaffordable FloodWait rotates the stream instead of waiting it out"
        )

    async def test_the_give_up_is_counted_as_a_severe_stream_failure(self):
        before = metrics.failures_in_window(3600)
        streamer = custom_dl.ByteStreamer(StallClient("only", stall_every=True))
        self.primary.stall_every = True

        with mock.patch.object(custom_dl, "CHUNK_READ_TIMEOUT", 0.05), \
                mock.patch.object(custom_dl, "STREAM_STALL_BUDGET_SECONDS", 0.12):
            with self.assertRaises(StreamUnavailable):
                await self._collect(streamer)

        self.assertGreater(
            metrics.failures_in_window(3600), before,
            "a dead stream has to feed the owner alert, not vanish into a traceback"
        )


class FakeStreamer:
    """Stands in for ByteStreamer without touching Telegram."""

    def __init__(self, data, fail_at=None, fail_with=None):
        self.data = data
        self.fail_at = fail_at
        self.fail_with = fail_with
        self.client = types.SimpleNamespace(name="fake")
        self.info = {
            "message_id": 7,
            "file_size": len(data),
            "file_name": "sample.bin",
            "mime_type": "application/octet-stream",
            "unique_id": "abc123deadbeefcafe",
        }

    async def get_file_info(self, message_id):
        return self.info

    async def stream_file(self, media_ref, offset=0, limit=0,
                          fallback_message_id=None, on_fallback_message=None,
                          chat_id=None):
        first = offset // CHUNK
        last = (offset + limit - 1) // CHUNK if limit > 0 else (len(self.data) - 1) // CHUNK
        for index in range(first, last + 1):
            if self.fail_at == index:
                raise self.fail_with
            yield self.data[index * CHUNK:(index + 1) * CHUNK]


class StreamRouteStatusTest(unittest.IsolatedAsyncioTestCase):
    """A stalled read is retryable, so it must not be reported as `404`."""

    async def asyncSetUp(self):
        self.data = bytes(range(256)) * (3 * CHUNK // 256 + 1)
        self._original_select = sr.select_optimal_client
        work_loads = sr.work_loads
        work_loads.clear()
        work_loads[0] = 0

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        sr.select_optimal_client = self._original_select

    async def test_a_stall_before_the_first_chunk_answers_503_with_retry_after(self):
        stalled = FakeStreamer(
            self.data, fail_at=0,
            fail_with=StreamUnavailable("Unable to stream file: TimeoutError")
        )
        sr.select_optimal_client = lambda: (0, stalled)

        async with self.client.get("/7?hash=abc123") as resp:
            self.assertEqual(
                resp.status, 503,
                "a read that failed after its retries is temporary - a 404 tells "
                "a media player the file is gone and it gives up"
            )
            self.assertIn("Retry-After", resp.headers)

        self.assertEqual(sr.work_loads[0], 0)

    async def test_a_record_without_a_unique_id_still_answers_404(self):
        # The 404 contract stays where it belongs: the link itself does not
        # resolve (no stored unique id), so a player should not keep retrying.
        broken = FakeStreamer(self.data)
        broken.info["unique_id"] = None
        sr.select_optimal_client = lambda: (0, broken)

        async with self.client.get("/7?hash=abc123") as resp:
            self.assertEqual(resp.status, 404)
        self.assertEqual(sr.work_loads[0], 0)

    async def test_a_stream_aborted_mid_body_releases_the_slot_and_counts_it(self):
        failing = FakeStreamer(
            self.data, fail_at=1,
            fail_with=StreamUnavailable("Unable to stream file: TimeoutError")
        )
        sr.select_optimal_client = lambda: (0, failing)
        before = metrics.snapshot()["errors"].get("stream", 0)

        async with self.client.get("/7?hash=abc123") as resp:
            self.assertEqual(resp.status, 200)
            try:
                await asyncio.wait_for(resp.read(), timeout=5)
            except asyncio.TimeoutError:
                self.fail(
                    "the aborted stream held the connection open: the client "
                    "waits for the Content-Length we promised and never sent "
                    "(this is the production H15/H27 shape)"
                )
            except Exception:
                pass          # the server closed the connection, as it must

        self.assertEqual(
            sr.work_loads[0], 0,
            "an aborted stream must release its pool slot exactly once"
        )
        self.assertGreater(
            metrics.snapshot()["errors"].get("stream", 0), before,
            "an aborted body is a stream failure the operator has to see"
        )


class ClientSpreadingTest(unittest.TestCase):
    """Idle clients must share the traffic: one session serves one stream."""

    def setUp(self):
        self._original_limit = sr.MAX_CONCURRENT_PER_CLIENT
        self._original_clients = dict(sr.multi_clients)
        sr.MAX_CONCURRENT_PER_CLIENT = 16
        sr.multi_clients.clear()
        sr.streamers.clear()
        sr.work_loads.clear()

    def tearDown(self):
        sr.MAX_CONCURRENT_PER_CLIENT = self._original_limit
        sr.multi_clients.clear()
        sr.multi_clients.update(self._original_clients)
        sr.streamers.clear()
        sr.work_loads.clear()

    def _add_idle_clients(self, count):
        for client_id in range(count):
            sr.multi_clients[client_id] = types.SimpleNamespace(name=f"c{client_id}")
            sr.work_loads[client_id] = 0

    def test_consecutive_streams_use_different_idle_clients(self):
        self._add_idle_clients(3)

        chosen = [sr.select_optimal_client()[0] for _ in range(3)]

        self.assertEqual(
            sorted(chosen), [0, 1, 2],
            "with three idle clients every stream was served by the lowest id - "
            "one session taking the whole load, and its rate limits with it"
        )
        self.assertEqual(sr.select_optimal_client()[0] in (0, 1, 2), True)

    def test_a_busier_client_is_still_skipped(self):
        self._add_idle_clients(3)
        sr.work_loads[0] = 5

        chosen = {sr.select_optimal_client()[0] for _ in range(2)}

        self.assertEqual(chosen, {1, 2})

    def test_the_data_centre_preference_still_narrows_the_pool(self):
        sr.multi_clients[0] = types.SimpleNamespace(
            name="c0", storage=types.SimpleNamespace(dc_id=2))
        sr.multi_clients[1] = types.SimpleNamespace(
            name="c1", storage=types.SimpleNamespace(dc_id=4))
        sr.multi_clients[2] = types.SimpleNamespace(
            name="c2", storage=types.SimpleNamespace(dc_id=4))
        for client_id in range(3):
            sr.work_loads[client_id] = 0

        chosen = {sr.select_optimal_client(preferred_dc=4)[0] for _ in range(4)}

        self.assertEqual(
            chosen, {1, 2},
            "spreading must not undo the data-centre preference (or pick a "
            "client that is in the wrong DC and full)"
        )

    def test_rotation_candidates_prefer_idle_multi_clients(self):
        c0 = types.SimpleNamespace(name="c0")
        c1 = types.SimpleNamespace(name="c1")
        c2 = types.SimpleNamespace(name="c2")
        Thunder.bot.multi_clients[0] = c0
        Thunder.bot.multi_clients[1] = c1
        Thunder.bot.multi_clients[2] = c2
        Thunder.bot.work_loads[0] = 5
        Thunder.bot.work_loads[1] = 0
        Thunder.bot.work_loads[2] = 2

        # Rotating off c1 should pick least-loaded other client: c2 (load 2) before c0 (load 5)
        candidates = custom_dl.get_rotation_candidates(c1)
        self.assertEqual(candidates, [c2, c0])

    def test_rotation_candidates_round_robins_equally_least_loaded_clients(self):
        c0 = types.SimpleNamespace(name="c0")
        c1 = types.SimpleNamespace(name="c1")
        c2 = types.SimpleNamespace(name="c2")
        c3 = types.SimpleNamespace(name="c3")
        Thunder.bot.multi_clients[0] = c0
        Thunder.bot.multi_clients[1] = c1
        Thunder.bot.multi_clients[2] = c2
        Thunder.bot.multi_clients[3] = c3
        Thunder.bot.work_loads[0] = 0
        Thunder.bot.work_loads[1] = 0
        Thunder.bot.work_loads[2] = 0
        Thunder.bot.work_loads[3] = 0

        # Consecutive rotations off c0 should rotate the first candidate across c1, c2, c3
        first_picks = [custom_dl.get_rotation_candidates(c0)[0] for _ in range(3)]
        self.assertEqual(sorted([p.name for p in first_picks]), ["c1", "c2", "c3"])


if __name__ == "__main__":
    unittest.main()
