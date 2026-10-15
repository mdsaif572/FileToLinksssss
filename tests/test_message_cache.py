# tests/test_message_cache.py
"""Regression tests for Telegram file-reference handling in ByteStreamer.

The production failure this locks down (seen as ``FILE_REFERENCE_EXPIRED`` in
``bot.txt``): the message was read with the primary bot, cached globally, and
then handed to a *secondary* client to stream. A Telegram file reference is
bound to the session that fetched it, so the streaming client always got
"expired file reference", and the shared 5-minute cache kept re-using it.

Also covers the retry that recovers a stream when a reference has genuinely
expired: drop the cached message, re-read it with the streaming client, retry
the same chunk.

Run with::

    python -m unittest discover -s tests -t .
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

import Thunder.bot
from pyrogram.errors import FileReferenceExpired

from Thunder.server.exceptions import FileNotFound
from Thunder.utils import custom_dl

CHUNK_OK = b"payload-1" * 4


class FakeMessage:
    def __init__(self, message_id, owner):
        self.id = message_id
        self.media = object()          # only truthiness is checked
        self.owner = owner


class FakeClient:
    """Minimal stand-in for a pyrogram Client used by ByteStreamer."""

    def __init__(self, name, can_read_bin_channel=True, fail_reference_times=0):
        self.name = name
        self.can_read_bin_channel = can_read_bin_channel
        self.fail_reference_times = fail_reference_times
        self.fetch_calls = 0
        self.stream_runs = 0

    async def get_messages(self, chat_id, message_id):
        self.fetch_calls += 1
        if not self.can_read_bin_channel:
            raise RuntimeError(f"{self.name} cannot read BIN_CHANNEL")
        return FakeMessage(message_id, self.name)

    def stream_media(self, message, offset=0, limit=0):
        async def generator():
            self.stream_runs += 1
            if self.stream_runs <= self.fail_reference_times:
                raise FileReferenceExpired()
            yield CHUNK_OK

        return generator()


class ByteStreamerReferenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        custom_dl._GLOBAL_MSG_CACHE.clear()
        self.primary = FakeClient("primary")
        patcher = mock.patch.object(Thunder.bot, "StreamBot", self.primary)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(custom_dl._GLOBAL_MSG_CACHE.clear)

    async def test_repeated_reads_use_the_cache(self):
        streamer = custom_dl.ByteStreamer(FakeClient("only"))

        first = await streamer.get_message(5)
        second = await streamer.get_message(5)

        self.assertIs(first, second)
        self.assertEqual(streamer.client.fetch_calls, 1)

    async def test_secondary_client_reads_the_message_itself(self):
        secondary = FakeClient("secondary")
        streamer = custom_dl.ByteStreamer(secondary)

        message = await streamer.get_message(5)

        self.assertEqual(message.owner, "secondary")
        self.assertEqual(secondary.fetch_calls, 1)
        self.assertEqual(
            self.primary.fetch_calls, 0,
            "the message must be read by the streaming client, otherwise the "
            "file reference belongs to a different session and Telegram rejects it"
        )

    async def test_primary_is_only_a_fallback_when_streaming_client_cannot_read(self):
        secondary = FakeClient("secondary", can_read_bin_channel=False)
        streamer = custom_dl.ByteStreamer(secondary)

        message = await streamer.get_message(5)

        self.assertEqual(message.owner, "primary")
        self.assertEqual(secondary.fetch_calls, 1)
        self.assertEqual(self.primary.fetch_calls, 1)

    async def test_force_refresh_bypasses_the_cache(self):
        streamer = custom_dl.ByteStreamer(FakeClient("only"))

        await streamer.get_message(5)
        await streamer.get_message(5)
        self.assertEqual(streamer.client.fetch_calls, 1)

        await streamer.get_message(5, force_refresh=True)
        self.assertEqual(streamer.client.fetch_calls, 2)

    async def test_invalidate_drops_entries_for_every_client(self):
        first = FakeClient("first")
        second = FakeClient("second")
        await custom_dl.ByteStreamer(first).get_message(5)
        await custom_dl.ByteStreamer(second).get_message(5)

        custom_dl.invalidate_cached_message(5)
        self.assertEqual(custom_dl._GLOBAL_MSG_CACHE, {})

        await custom_dl.ByteStreamer(first).get_message(5)
        self.assertEqual(first.fetch_calls, 2)

    async def test_stream_recovers_from_a_rejected_file_reference(self):
        client = FakeClient("only", fail_reference_times=1)
        streamer = custom_dl.ByteStreamer(client)

        chunks = [chunk async for chunk in streamer.stream_file(5)]

        self.assertEqual(chunks, [CHUNK_OK])
        self.assertEqual(client.stream_runs, 1)
        self.assertEqual(
            self.primary.stream_runs, 1,
            "the retry must run on the other session instead of replaying the "
            "reference Telegram just rejected"
        )
        self.assertEqual(client.fetch_calls + self.primary.fetch_calls, 2)

    async def test_streams_with_the_client_that_owns_the_reference(self):
        secondary = FakeClient("secondary", can_read_bin_channel=False)
        streamer = custom_dl.ByteStreamer(secondary)

        chunks = [chunk async for chunk in streamer.stream_file(5)]

        self.assertEqual(chunks, [CHUNK_OK])
        self.assertEqual(
            secondary.stream_runs, 0,
            "a client that could not read the message must not be asked to stream it"
        )
        self.assertEqual(self.primary.stream_runs, 1)

    async def test_reference_rejection_rotates_to_the_other_client(self):
        # The streaming client can read BIN_CHANNEL, so the fallback path is not
        # involved - Telegram simply keeps rejecting the reference it hands out.
        secondary = FakeClient("secondary", fail_reference_times=99)
        streamer = custom_dl.ByteStreamer(secondary)

        chunks = [chunk async for chunk in streamer.stream_file(5)]

        self.assertEqual(chunks, [CHUNK_OK])
        self.assertEqual(secondary.stream_runs, 1)
        self.assertEqual(self.primary.stream_runs, 1)

    async def test_stream_gives_up_after_bounded_retries(self):
        # Both sessions reject the reference - the shape of the production log
        # where every attempt on either client failed.
        client = FakeClient("only", fail_reference_times=99)
        self.primary.fail_reference_times = 99
        streamer = custom_dl.ByteStreamer(client)

        with self.assertRaises(FileNotFound):
            [chunk async for chunk in streamer.stream_file(5)]

        stream_runs = client.stream_runs + self.primary.stream_runs
        fetch_calls = client.fetch_calls + self.primary.fetch_calls
        self.assertEqual(stream_runs, 5)
        self.assertEqual(fetch_calls, 5)


if __name__ == "__main__":
    unittest.main()
