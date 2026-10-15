# tests/test_client_pool.py
"""A pool client that cannot read BIN_CHANNEL must not serve streams.

Such a client has no file reference of its own, which is what produced the
production FILE_REFERENCE_EXPIRED failures: the message was read by the primary
bot and streamed by this client. It is now detected at boot and dropped.
"""

import asyncio
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from pyrogram.errors import ChannelInvalid, FloodWait  # noqa: E402

from Thunder.bot import clients as clients_module  # noqa: E402
from Thunder.bot import multi_clients, work_loads  # noqa: E402
from Thunder.vars import Var  # noqa: E402


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakePoolClient:
    """Records what it was asked to do; optionally rejects the channel read."""

    instances = []

    def __init__(self, name, can_read=True, flood_wait_times=0, **kwargs):
        self.name = name
        self.can_read = can_read
        self.flood_wait_times = flood_wait_times
        self.get_chat_calls = 0
        self.started = False
        self.stopped = False
        FakePoolClient.instances.append(self)

    async def get_chat(self, chat_id):
        self.get_chat_calls += 1
        if self.flood_wait_times > 0:
            self.flood_wait_times -= 1
            raise FloodWait(value=1)
        if not self.can_read:
            raise ChannelInvalid("not a member")
        return FakeChat(chat_id)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeTokenParser:
    tokens = {}

    def parse_from_env(self):
        return dict(FakeTokenParser.tokens)


class ReadableChannelsTest(unittest.IsolatedAsyncioTestCase):
    def _use_channels(self, *channels):
        original = Var.BIN_CHANNELS
        Var.BIN_CHANNELS = list(channels)
        self.addCleanup(lambda: setattr(Var, "BIN_CHANNELS", original))

    async def test_readable_client_reports_its_channel(self):
        self._use_channels(Var.BIN_CHANNEL)
        client = FakePoolClient("ok")
        self.assertEqual(
            await clients_module._readable_channels(client, "Client ID 1"),
            frozenset({Var.BIN_CHANNEL})
        )

    async def test_client_outside_every_channel_reports_nothing(self):
        self._use_channels(Var.BIN_CHANNEL)
        client = FakePoolClient("outsider", can_read=False)
        self.assertEqual(
            await clients_module._readable_channels(client, "Client ID 2"), frozenset()
        )

    async def test_flood_wait_is_retried(self):
        self._use_channels(Var.BIN_CHANNEL)
        client = FakePoolClient("floody", flood_wait_times=1)
        with mock.patch.object(clients_module.asyncio, "sleep", new=_instant_sleep):
            await clients_module._readable_channels(client, "Client ID 3")
        self.assertEqual(client.get_chat_calls, 2)

    async def test_a_client_can_serve_only_some_channels(self):
        second = Var.BIN_CHANNEL - 1
        self._use_channels(Var.BIN_CHANNEL, second)

        class OnlySecond(FakePoolClient):
            async def get_chat(self, chat_id):
                self.get_chat_calls += 1
                if int(chat_id) == second:
                    return FakeChat(chat_id)
                raise ChannelInvalid("not a member")

        client = OnlySecond("partial")
        readable = await clients_module._readable_channels(client, "Client ID 4")
        self.assertEqual(readable, frozenset({second}))


async def _instant_sleep(_seconds):
    return None


class InitializeClientsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakePoolClient.instances = []
        FakeTokenParser.tokens = {1: "token-one", 2: "token-two"}
        multi_clients.clear()
        work_loads.clear()

        self.primary = FakePoolClient("primary")
        self.addCleanup(mock.patch.object(clients_module, "StreamBot", self.primary).stop)
        mock.patch.object(clients_module, "StreamBot", self.primary).start()
        self.addCleanup(mock.patch.object(clients_module, "TokenParser", FakeTokenParser).stop)
        mock.patch.object(clients_module, "TokenParser", FakeTokenParser).start()

    async def asyncTearDown(self):
        multi_clients.clear()
        work_loads.clear()

    async def test_pool_sizing_is_logged_without_failing(self):
        FakeTokenParser.tokens = {1: "token-one"}

        with mock.patch.object(clients_module, "Client",
                               side_effect=lambda **kw: FakePoolClient(name=kw.get("bot_token"))):
            with mock.patch.object(clients_module.asyncio, "sleep", new=_instant_sleep):
                with mock.patch.object(clients_module, "_log_pool_memory") as memory_log:
                    await clients_module.initialize_clients()

        memory_log.assert_called_once()

    async def test_a_large_pool_still_starts_every_client(self):
        tokens = {index: f"token-{index}" for index in range(1, 31)}
        FakeTokenParser.tokens = tokens

        def factory(**kwargs):
            return FakePoolClient(name=kwargs.get("bot_token"), bot_token=kwargs.get("bot_token"))

        with mock.patch.object(clients_module, "Client", side_effect=factory):
            with mock.patch.object(clients_module.asyncio, "sleep", new=_instant_sleep):
                await clients_module.initialize_clients()

        self.assertEqual(
            len(multi_clients), len(tokens) + 1,
            "bounded concurrency must not drop clients from a 30+ pool"
        )
        for index in tokens:
            self.assertIn(index, work_loads)

    async def test_unusable_pool_client_is_dropped_from_the_pool(self):
        def factory(**kwargs):
            token = kwargs.get("bot_token")
            return FakePoolClient(
                name=token,
                can_read=(token == "token-one"),
                bot_token=token,
            )

        with mock.patch.object(clients_module, "Client", side_effect=factory):
            with mock.patch.object(clients_module.asyncio, "sleep", new=_instant_sleep):
                await clients_module.initialize_clients()

        self.assertIn(0, multi_clients, "the primary bot always stays")
        pool_ids = sorted(multi_clients)
        self.assertEqual(pool_ids, [0, 1], "only the client that can read BIN_CHANNEL")
        self.assertIn(1, work_loads)
        self.assertNotIn(2, work_loads)

        unusable = [c for c in FakePoolClient.instances if c.name == "token-two"]
        self.assertEqual(len(unusable), 1)
        self.assertTrue(unusable[0].stopped, "an unusable client should be stopped")


class CleanupClientsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        multi_clients.clear()

    async def asyncTearDown(self):
        multi_clients.clear()

    async def test_clients_are_stopped_in_parallel(self):
        class SlowClient:
            def __init__(self):
                self.stopped = False

            async def stop(self):
                await asyncio.sleep(0.2)
                self.stopped = True

        clients = [SlowClient() for _ in range(4)]
        multi_clients.update({index: client for index, client in enumerate(clients)})

        started = time.monotonic()
        await clients_module.cleanup_clients()
        elapsed = time.monotonic() - started

        self.assertTrue(all(client.stopped for client in clients))
        self.assertLess(
            elapsed, 0.6,
            "clients must stop concurrently: sequentially this takes 0.8s and a "
            "real pool of 44 blows past the platform's 30s grace period (R12)"
        )

    async def test_one_failing_client_does_not_block_the_rest(self):
        class BrokenClient:
            async def stop(self):
                raise RuntimeError("already gone")

        class GoodClient:
            def __init__(self):
                self.stopped = False

            async def stop(self):
                self.stopped = True

        good = GoodClient()
        multi_clients.update({0: BrokenClient(), 1: good})

        await clients_module.cleanup_clients()

        self.assertTrue(good.stopped)


if __name__ == "__main__":
    unittest.main()
