# tests/test_url_upload_and_stats.py
"""Upload-from-URL plumbing and the /mystats aggregation."""

import os
import unittest
from unittest import mock

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")
os.environ.setdefault("FQDN", "cdn.example.com")
os.environ.setdefault("HAS_SSL", "True")
os.environ.setdefault("NO_PORT", "True")

from pymongo.errors import DuplicateKeyError  # noqa: E402

from Thunder.bot.plugins import stream as stream_plugin  # noqa: E402
from Thunder.utils.bot_utils import filename_from_url  # noqa: E402
from Thunder.utils.database import db  # noqa: E402

RECORD = {
    "file_unique_id": "abc123def456",
    "public_hash": "ab" * 10,
    "canonical_message_id": 99,
    "file_id": "file-id",
    "file_name": "series.s01e01.mkv",
    "mime_type": "video/x-matroska",
    "file_size": 5 * 1024 * 1024,
}


class FilenameFromUrlTest(unittest.TestCase):
    def test_plain_path(self):
        self.assertEqual(
            filename_from_url("https://host/files/movie.mkv"), "movie.mkv"
        )

    def test_query_string_is_dropped(self):
        self.assertEqual(
            filename_from_url("https://host/dl/movie.mkv?token=abc&x=1"), "movie.mkv"
        )

    def test_url_encoded_name(self):
        self.assertEqual(
            filename_from_url("https://host/dl/My%20Movie%20(2026).mkv"), "My Movie (2026).mkv"
        )

    def test_directory_url_gets_a_generated_name(self):
        name = filename_from_url("https://host/downloads/")
        self.assertTrue(name.startswith("download_"))

    def test_path_separators_and_control_chars_are_stripped(self):
        name = filename_from_url("https://host/dl/..%2F..%2Fetc%2Fpasswd")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)


class LooksLikeUrlTest(unittest.TestCase):
    def test_single_url(self):
        self.assertTrue(stream_plugin.looks_like_url("https://host/file.mkv"))

    def test_surrounding_whitespace(self):
        self.assertTrue(stream_plugin.looks_like_url("  http://host/file.mkv  "))

    def test_plain_text(self):
        self.assertFalse(stream_plugin.looks_like_url("hello there"))

    def test_url_with_extra_words(self):
        self.assertFalse(stream_plugin.looks_like_url("please grab https://host/file.mkv"))

    def test_command(self):
        self.assertFalse(stream_plugin.looks_like_url("/start abc123"))

    def test_empty(self):
        self.assertFalse(stream_plugin.looks_like_url(""))
        self.assertFalse(stream_plugin.looks_like_url(None))


class FakeStatusMessage:
    def __init__(self):
        self.edits = []
        self.reply_markup = None

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)
        self.reply_markup = kwargs.get("reply_markup")
        return self

    @property
    def last_text(self):
        return self.edits[-1] if self.edits else ""


class FakeChat:
    id = 777


class FakeMessage:
    id = 12
    chat = FakeChat()
    text = "https://host/dl/series.s01e01.mkv"

    def __init__(self):
        self.status = FakeStatusMessage()

    async def reply_text(self, text, **kwargs):
        self.status.edits.append(text)
        return self.status


class FakeUploadClient:
    def __init__(self):
        self.uploads = []

    async def send_document(self, chat_id, document, file_name=None, **kwargs):
        self.uploads.append((chat_id, document, file_name))
        return object()  # the stored BIN_CHANNEL message


class HandleUrlUploadTest(unittest.IsolatedAsyncioTestCase):
    async def test_replies_with_a_canonical_link(self):
        client = FakeUploadClient()
        message = FakeMessage()

        with mock.patch.object(stream_plugin, "build_file_record", return_value=dict(RECORD)):
            with mock.patch.object(stream_plugin.db, "create_file_record", new=_async_none()):
                links = await stream_plugin.handle_url_upload(client, message, shortener=False)

        self.assertIsNotNone(links)
        self.assertIn(f"/f/{RECORD['public_hash']}/series.s01e01.mkv", links["online_link"])
        self.assertEqual(client.uploads[0][0], -1001234567890)
        self.assertEqual(client.uploads[0][2], "series.s01e01.mkv")
        self.assertIn("series.s01e01.mkv", message.status.last_text)

    async def test_duplicate_upload_reuses_the_existing_record(self):
        client = FakeUploadClient()
        message = FakeMessage()
        existing = dict(RECORD, public_hash="cd" * 10)

        with mock.patch.object(stream_plugin, "build_file_record", return_value=dict(RECORD)):
            with mock.patch.object(stream_plugin.db, "create_file_record",
                                   new=_raise(DuplicateKeyError("dup"))):
                with mock.patch.object(stream_plugin, "get_file_by_hash",
                                       new=_returns(existing)):
                    links = await stream_plugin.handle_url_upload(client, message, shortener=False)

        self.assertIn(f"/f/{existing['public_hash']}/series.s01e01.mkv", links["online_link"])

    async def test_failed_upload_tells_the_user(self):
        class RefusingClient(FakeUploadClient):
            async def send_document(self, *args, **kwargs):
                raise RuntimeError("Telegram says: file too big")

        message = FakeMessage()
        links = await stream_plugin.handle_url_upload(RefusingClient(), message)

        self.assertIsNone(links)
        self.assertIn("Could not fetch", message.status.last_text)


class FakeAggregateCursor:
    def __init__(self, documents):
        self._documents = documents

    async def to_list(self, length=None):
        return self._documents


class FakeFilesCollection:
    def __init__(self, documents=None, fail=False):
        self.documents = documents or []
        self.fail = fail
        self.pipelines = []

    async def aggregate(self, pipeline):
        # pymongo's async collections make aggregate() a coroutine. Faking it as
        # a plain method is what let the real
        # "'coroutine' object has no attribute 'to_list'" crash ship.
        if self.fail:
            raise RuntimeError("mongo is down")
        self.pipelines.append(pipeline)
        return FakeAggregateCursor(self.documents)


class SourceStatsTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_uploader_returns_zeroes(self):
        stats = await db.get_source_stats(None)
        self.assertEqual(stats, {"files": 0, "downloads": 0, "bytes": 0})

    async def test_aggregation_result_is_mapped(self):
        collection = FakeFilesCollection(
            [{"files": 7, "bytes": 12345, "downloads": 3}]
        )
        with mock.patch.object(db, "files_col", collection):
            stats = await db.get_source_stats(777)

        self.assertEqual(stats, {"files": 7, "downloads": 3, "bytes": 12345})
        self.assertEqual(collection.pipelines[0][0], {"$match": {"first_source_chat_id": 777}})

    async def test_no_documents_returns_zeroes(self):
        with mock.patch.object(db, "files_col", FakeFilesCollection([])):
            self.assertEqual(
                await db.get_source_stats(777), {"files": 0, "downloads": 0, "bytes": 0}
            )

    async def test_database_error_is_swallowed(self):
        with mock.patch.object(db, "files_col", FakeFilesCollection(fail=True)):
            self.assertEqual(
                await db.get_source_stats(777), {"files": 0, "downloads": 0, "bytes": 0}
            )


def _async_none():
    async def _noop(*args, **kwargs):
        return None

    return _noop


def _raise(error):
    async def _raiser(*args, **kwargs):
        raise error

    return _raiser


def _returns(value):
    async def _returner(*args, **kwargs):
        return value

    return _returner


if __name__ == "__main__":
    unittest.main()
