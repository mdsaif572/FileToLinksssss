# tests/test_multi_channel.py
"""More than one storage channel, and the bot-capability routing that needs.

Telegram caps channel administrators at 50 (a bot can only be added to a channel
as an administrator), so a large bot pool spans several channels. Files are
assigned to a channel by a stable hash of their identity, and a file may only be
served by a pool client that administers its channel.
"""

import asyncio
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
from Thunder.server import stream_routes as sr  # noqa: E402
from Thunder.utils.canonical_files import (choose_bin_channel,  # noqa: E402
                                           record_bin_channel)
from Thunder.utils.metrics import metrics  # noqa: E402
from Thunder.vars import Var, parse_bin_channels  # noqa: E402
from tests.test_link_lifecycle import HASH, _record  # noqa: E402
from tests.test_stream_workload import FakeStreamer, make_data  # noqa: E402

PRIMARY = int(Var.BIN_CHANNEL)
SECOND = PRIMARY - 1


class ParseBinChannelsTest(unittest.TestCase):
    def test_primary_only(self):
        self.assertEqual(parse_bin_channels({}, PRIMARY), [PRIMARY])

    def test_extra_channels_in_key_order(self):
        environ = {"BIN_CHANNEL_2": str(SECOND), "BIN_CHANNEL_3": "-100333"}
        self.assertEqual(parse_bin_channels(environ, PRIMARY), [PRIMARY, SECOND, -100333])

    def test_quoted_values_and_duplicates(self):
        environ = {"BIN_CHANNEL_2": f'"{PRIMARY}"', "BIN_CHANNEL_3": " -100333 "}
        self.assertEqual(parse_bin_channels(environ, PRIMARY), [PRIMARY, -100333])

    def test_garbage_is_ignored(self):
        environ = {"BIN_CHANNEL_2": "not-a-number", "BIN_CHANNEL_3": ""}
        self.assertEqual(parse_bin_channels(environ, PRIMARY), [PRIMARY])


class ChannelAssignmentTest(unittest.TestCase):
    def _use_channels(self, *channels):
        original = Var.BIN_CHANNELS
        Var.BIN_CHANNELS = list(channels)
        self.addCleanup(lambda: setattr(Var, "BIN_CHANNELS", original))

    def test_single_channel_is_always_used(self):
        self._use_channels(PRIMARY)
        for index in range(20):
            self.assertEqual(choose_bin_channel(f"file-{index}"), PRIMARY)

    def test_choice_is_stable_for_the_same_file(self):
        self._use_channels(PRIMARY, SECOND)
        first = choose_bin_channel("unique-id-abc")
        self.assertEqual(first, choose_bin_channel("unique-id-abc"))

    def test_files_spread_across_the_configured_channels(self):
        self._use_channels(PRIMARY, SECOND)
        picked = {choose_bin_channel(f"unique-{index}") for index in range(200)}
        self.assertEqual(picked, {PRIMARY, SECOND})

    def test_records_without_a_channel_fall_back_to_the_primary(self):
        self._use_channels(PRIMARY, SECOND)
        self.assertEqual(record_bin_channel({"public_hash": HASH}), PRIMARY)
        self.assertEqual(record_bin_channel({"bin_channel_id": SECOND}), SECOND)


class ChannelAwareSelectionTest(unittest.TestCase):
    def setUp(self):
        self._original_limit = sr.MAX_CONCURRENT_PER_CLIENT
        sr.MAX_CONCURRENT_PER_CLIENT = 16
        self._original_clients = dict(sr.multi_clients)
        self._original_capabilities = dict(client_channels)
        sr.multi_clients.clear()
        sr.streamers.clear()
        client_channels.clear()
        work_loads.clear()

    def tearDown(self):
        sr.MAX_CONCURRENT_PER_CLIENT = self._original_limit
        sr.multi_clients.clear()
        sr.multi_clients.update(self._original_clients)
        client_channels.clear()
        client_channels.update(self._original_capabilities)
        sr.streamers.clear()
        work_loads.clear()

    def test_only_clients_that_administer_the_channel_are_chosen(self):
        sr.multi_clients[0] = object()
        sr.multi_clients[1] = object()
        work_loads[0] = 0
        work_loads[1] = 4          # busier, but the only one for this channel
        client_channels[0] = frozenset({PRIMARY})
        client_channels[1] = frozenset({PRIMARY, SECOND})

        client_id, _ = sr.select_optimal_client(required_channel=SECOND)
        self.assertEqual(client_id, 1)

    def test_falls_back_to_load_balancing_when_nobody_is_capable(self):
        sr.multi_clients[0] = object()
        work_loads[0] = 2
        client_channels[0] = frozenset({PRIMARY})
        before = metrics.snapshot()["errors"].get("no_capable_client", 0)

        client_id, _ = sr.select_optimal_client(required_channel=SECOND)

        self.assertEqual(client_id, 0)
        self.assertEqual(
            metrics.snapshot()["errors"].get("no_capable_client", 0), before + 1,
            "an incapable pool must be visible in the metrics, not silently served"
        )

    def test_capability_is_ignored_before_the_pool_is_known(self):
        sr.multi_clients[0] = object()
        work_loads[0] = 0
        client_id, _ = sr.select_optimal_client(required_channel=SECOND)
        self.assertEqual(client_id, 0)


class ChannelAwareStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_channels = Var.BIN_CHANNELS
        Var.BIN_CHANNELS = [PRIMARY, SECOND]

        self.data = make_data(4096)
        self.streamer = FakeStreamer(self.data)
        self._original_select = sr.select_optimal_client
        self._original_lookup = sr.get_file_by_hash
        self._original_limiter = sr.user_streams

        sr.select_optimal_client = lambda **kwargs: (0, self.streamer)
        sr.user_streams = sr.UserStreamLimiter(0)
        sr.get_file_by_hash = self._lookup

        work_loads.clear()
        work_loads[0] = 0
        self.record = _record(file_size=len(self.data), bin_channel_id=SECOND)

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        Var.BIN_CHANNELS = self._original_channels
        sr.select_optimal_client = self._original_select
        sr.get_file_by_hash = self._original_lookup
        sr.user_streams = self._original_limiter
        work_loads.clear()

    async def _lookup(self, public_hash, **kwargs):
        return self.record

    async def test_file_is_streamed_from_the_channel_that_holds_it(self):
        async with self.client.get(f"/f/{HASH}/sample.mkv") as resp:
            body = await resp.read()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body, self.data)
        self.assertEqual(
            self.streamer.chat_ids, [SECOND],
            "the stream must read the record's channel, not the primary one"
        )


if __name__ == "__main__":
    unittest.main()
