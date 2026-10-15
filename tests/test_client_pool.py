# tests/test_client_pool.py
"""A pool client that cannot read BIN_CHANNEL must not serve streams.

Such a client has no file reference of its own, which is what produced the
production FILE_REFERENCE_EXPIRED failures: the message was read by the primary
bot and streamed by this client. It is now detected at boot and dropped.
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


class CanReadBinChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_readable_client_passes(self):
        client = FakePoolClient("ok")
        self.assertTrue(await clients_module._can_read_bin_channel(client, "Client ID 1"))

    async def test_client_outside_the_channel_fails(self):
        client = FakePoolClient("outsider", can_read=False)
        self.assertFalse(await clients_module._can_read_bin_channel(client, "Client ID 2"))

    async def test_flood_wait_is_retried(self):
        client = FakePoolClient("floody", flood_wait_times=1)
        with mock.patch.object(clients_module.asyncio, "sleep", new=_instant_sleep):
            self.assertTrue(await clients_module._can_read_bin_channel(client, "Client ID 3"))
        self.assertEqual(client.get_chat_calls, 2)


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


if __name__ == "__main__":
    unittest.main()
