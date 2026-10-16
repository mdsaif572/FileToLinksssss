# tests/test_log_tail_and_filters.py
"""Two production annoyances: /log was slow, and storage-channel copies leaked
into the media handler once there was more than one storage channel."""

import os
import tempfile
import unittest

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from Thunder.bot.plugins import stream as stream_plugin  # noqa: E402
from Thunder.utils.logger import read_log_tail  # noqa: E402
from Thunder.vars import Var  # noqa: E402


class ReadLogTailTest(unittest.TestCase):
    def _write(self, content: bytes) -> str:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as handle:
            handle.write(content)
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_a_small_file_is_returned_whole(self):
        path = self._write(b"one\ntwo\n")
        self.assertEqual(read_log_tail(path, max_bytes=4096), b"one\ntwo\n")

    def test_the_tail_starts_on_a_line_boundary(self):
        path = self._write(b"".join(f"line{index}\n".encode() for index in range(1000)))

        tail = read_log_tail(path, max_bytes=100)

        self.assertLessEqual(len(tail), 100)
        self.assertTrue(tail.startswith(b"line"), "a partial first line must be dropped")
        self.assertTrue(tail.endswith(b"\n"))

    def test_the_newest_lines_are_kept(self):
        path = self._write(b"old\nnewest\n")
        self.assertIn(b"newest", read_log_tail(path, max_bytes=8))

    def test_an_empty_file_returns_nothing(self):
        path = self._write(b"")
        self.assertEqual(read_log_tail(path, max_bytes=1024), b"")


class StorageChatFilterTest(unittest.TestCase):
    def test_the_filter_matches_every_storage_channel(self):
        chat_filter = stream_plugin.build_storage_chat_filter([-1001, -1002, -1003])
        self.assertEqual(set(chat_filter), {-1001, -1002, -1003})

    def test_it_defaults_to_the_running_configuration(self):
        self.assertEqual(set(stream_plugin.build_storage_chat_filter()), set(Var.BIN_CHANNELS))

    def test_the_handler_uses_a_filter_covering_all_channels(self):
        # the media handler excludes ~STORAGE_CHAT_FILTER, so a copy into any
        # storage channel stays out of it
        self.assertEqual(set(stream_plugin.STORAGE_CHAT_FILTER), set(Var.BIN_CHANNELS))
        self.assertGreaterEqual(len(stream_plugin.STORAGE_CHAT_FILTER), 1)


class AdminLogCommandTest(unittest.IsolatedAsyncioTestCase):
    def _write(self, content: bytes) -> str:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as handle:
            handle.write(content)
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_read_recent_logs_missing_and_empty(self):
        from Thunder.bot.plugins.admin import read_recent_logs
        self.assertEqual(read_recent_logs("/nonexistent/path/bot.txt"), "")
        empty_path = self._write(b"")
        self.assertEqual(read_recent_logs(empty_path), "")

    def test_read_recent_logs_reads_content(self):
        from Thunder.bot.plugins.admin import read_recent_logs
        content = b"line 1\nline 2\nline 3\n"
        path = self._write(content)
        self.assertEqual(read_recent_logs(path), "line 1\nline 2\nline 3")

    async def test_send_logs_text_mode(self):
        from unittest import mock
        from Thunder.bot.plugins.admin import send_logs

        path = self._write(b"2026-10-16 info: test log entry\n")

        fake_status_msg = mock.AsyncMock()
        fake_message = mock.AsyncMock()
        fake_message.id = 1
        fake_message.command = ["log", "text"]
        fake_message.reply_text = mock.AsyncMock(return_value=fake_status_msg)

        with mock.patch("Thunder.bot.plugins.admin.LOG_FILE", path):
            await send_logs(mock.MagicMock(), fake_message)

        fake_status_msg.edit_text.assert_called_once()
        self.assertIn("Recent Logs", fake_status_msg.edit_text.call_args[0][0])
        self.assertIn("test log entry", fake_status_msg.edit_text.call_args[0][0])

    async def test_send_logs_tail_document(self):
        from unittest import mock
        from Thunder.bot.plugins.admin import send_logs

        path = self._write(b"2026-10-16 info: line 1\n2026-10-16 info: line 2\n")

        fake_status_msg = mock.AsyncMock()
        fake_message = mock.AsyncMock()
        fake_message.id = 1
        fake_message.command = ["log"]
        fake_message.reply_text = mock.AsyncMock(return_value=fake_status_msg)
        fake_message.reply_document = mock.AsyncMock()

        with mock.patch("Thunder.bot.plugins.admin.LOG_FILE", path):
            await send_logs(mock.MagicMock(), fake_message)

        fake_message.reply_document.assert_called_once()
        # Document uploaded should be a BytesIO stream with name bot_tail.txt
        payload = fake_message.reply_document.call_args[0][0]
        self.assertEqual(payload.name, "bot_tail.txt")
        self.assertIn(b"line 2", payload.getvalue())
        fake_status_msg.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()

