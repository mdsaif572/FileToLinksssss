# tests/test_log_upload.py
"""Regression tests for the `/log` upload failure.

The production log (2026-10-16) ended with::

    ThunderBot - ERROR - Error sending log file:
    File "Thunder/bot/plugins/admin.py", line 284, in send_logs
        await asyncio.wait_for(_upload_doc(), timeout=25.0)
    TimeoutError

Two defects behind that, both covered here:

1. A 25s cap is not a budget for an MTProto upload. The upload waits for a slot
   in the client's transfer pipeline, which is shared with the file streams - so
   on a busy bot the upload was cancelled while it was still queued.
2. The FloodWait retry re-sent the payload from wherever the cancelled attempt
   had left the stream. ``save_file`` reads a file object from its *current*
   position, so the retry uploaded a truncated (often empty) document - and
   reported success.

Run with::

    python -m unittest discover -s tests -t .
"""

import asyncio
import os
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from io import BytesIO

from pyrogram.errors import FloodWait

from Thunder.bot.plugins.admin import upload_log_document

PAYLOAD = b"log line\n" * 500


class FlakyMessage:
    """A Message whose document upload fails once, the way Telegram does."""

    def __init__(self, *, flood_wait=None, hang=False, failures=1):
        self.flood_wait = flood_wait
        self.hang = hang
        self.failures = failures
        self.attempts = 0
        self.uploaded = []          # what each attempt actually read

    async def reply_document(self, payload, caption=None):
        self.attempts += 1
        start_position = payload.tell()
        data = payload.read()
        self.uploaded.append((start_position, data))

        if self.hang:
            await asyncio.sleep(30)          # a read that never returns
        if self.failures > 0:
            self.failures -= 1
            raise FloodWait(value=self.flood_wait or 1)
        return "sent"


class LogUploadTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_floodwait_retry_re_sends_the_whole_payload(self):
        message = FlakyMessage(flood_wait=1)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        await upload_log_document(
            message, BytesIO(PAYLOAD), "logs", timeout=5, sleep=fake_sleep
        )

        self.assertEqual(message.attempts, 2)
        self.assertEqual(
            message.uploaded[0][0], 0,
            "the first attempt must start at byte 0"
        )
        self.assertEqual(
            message.uploaded[1][0], 0,
            "a retry after a failed attempt has to seek back to the start - "
            "save_file reads from the current position, so it would otherwise "
            "upload an empty document and report success"
        )
        self.assertEqual(message.uploaded[1][1], PAYLOAD)
        self.assertEqual(sleeps, [1], "the FloodWait has to be waited out")

    async def test_an_upload_that_stalls_raises_a_named_timeout(self):
        message = FlakyMessage(hang=True, failures=0)

        with self.assertRaises(asyncio.TimeoutError) as ctx:
            await upload_log_document(message, BytesIO(PAYLOAD), "logs", timeout=0.05)

        self.assertIn(
            "log upload", str(ctx.exception),
            "the production log said only `TimeoutError`, which names nothing"
        )

    async def test_a_floodwait_longer_than_the_cap_gives_up_without_sleeping(self):
        # A 10 minute wait is not worth holding the owner's command open for -
        # the handler falls back to sending the log tail as text.
        message = FlakyMessage(flood_wait=600)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with self.assertRaises(FloodWait):
            await upload_log_document(
                message, BytesIO(PAYLOAD), "logs", timeout=5, sleep=fake_sleep
            )

        self.assertEqual(message.attempts, 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
