# tests/test_stats_aggregation.py
"""Regression tests for the two errors in the 2026-10-16 production log::

    ThunderBot - ERROR - Error aggregating stats for 5784009732:
      'coroutine' object has no attribute 'to_list'
    File "Thunder/utils/database.py", line 575, in get_source_stats
        documents = await cursor.to_list(length=1)
    RuntimeWarning: coroutine 'AsyncCollection.aggregate' was never awaited

    File "Thunder/bot/plugins/common.py", line 296, in my_stats_command
        await reply(
    NameError: name 'reply' is not defined

``/mystats`` answered nothing at all: pymongo's async collections make
``aggregate()`` a coroutine, so ``get_source_stats`` was reading a cursor off a
coroutine object and then the handler died on a name it never imported.

Run with::

    python -m unittest discover -s tests -t .
"""

import asyncio
import os
import types
import unittest
from unittest import mock

os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BIN_CHANNEL", "-1001234567890")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "mongodb://127.0.0.1:27017/test")

from Thunder.bot.plugins import common
from Thunder.utils.database import Database


class FakeCursor:
    """Stands in for pymongo's AsyncCommandCursor: async to_list/iteration."""

    def __init__(self, documents):
        self.documents = documents

    async def to_list(self, length=None):
        return list(self.documents)

    def __aiter__(self):
        async def walk():
            for document in self.documents:
                yield document
        return walk()


class FakeAsyncCollection:
    """The one property that matters: aggregate() is a coroutine."""

    def __init__(self, documents=None, error=None):
        self.documents = documents or []
        self.error = error
        self.pipelines = []
        self.deleted = []

    async def aggregate(self, pipeline, **kwargs):
        self.pipelines.append(pipeline)
        if self.error:
            raise self.error
        return FakeCursor(self.documents)

    async def delete_many(self, query):
        self.deleted.append(query)
        return types.SimpleNamespace(deleted_count=1)


class SourceStatsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database("mongodb://127.0.0.1:27017/test", "test")

    async def test_the_aggregate_cursor_is_awaited_before_it_is_read(self):
        self.db.files_col = FakeAsyncCollection(
            [{"files": 3, "bytes": 2048, "downloads": 5}]
        )

        stats = await self.db.get_source_stats(5784009732)

        self.assertEqual(stats, {"files": 3, "downloads": 5, "bytes": 2048})
        self.assertEqual(len(self.db.files_col.pipelines), 1)

    async def test_an_unknown_uploader_reports_zeroes(self):
        self.db.files_col = FakeAsyncCollection([])

        self.assertEqual(
            await self.db.get_source_stats(1), {"files": 0, "downloads": 0, "bytes": 0}
        )

    async def test_a_database_error_does_not_break_the_command(self):
        self.db.files_col = FakeAsyncCollection(error=RuntimeError("connection lost"))

        self.assertEqual(
            await self.db.get_source_stats(1), {"files": 0, "downloads": 0, "bytes": 0}
        )

    async def test_deduplication_iterates_the_awaited_cursor(self):
        collection = FakeAsyncCollection([{"_id": "keep"}, {"_id": "older"}])
        self.db.col = collection

        await self.db._deduplicate_users()

        self.assertEqual(len(collection.pipelines), 1)
        self.assertEqual(collection.deleted, [{"_id": {"$nin": ["keep", "older"]}}])

    async def test_get_regular_users_count_awaits_cursor(self):
        collection = FakeAsyncCollection([{"count": 42}])
        self.db.col = collection

        count = await self.db.get_regular_users_count()
        self.assertEqual(count, 42)


class MyStatsCommandTest(unittest.IsolatedAsyncioTestCase):
    """``/mystats`` must answer something - it used to die on a NameError."""

    def _message(self):
        sent = {}

        async def reply_text(**kwargs):
            sent.update(kwargs)
            return "sent"

        message = types.SimpleNamespace(
            id=5,
            from_user=types.SimpleNamespace(id=5784009732),
            reply_text=reply_text,
            command=["mystats"],
        )
        return message, sent

    async def test_it_replies_with_the_uploaders_stats(self):
        message, sent = self._message()

        with mock.patch.object(
            common.db, "get_source_stats",
            new=mock.AsyncMock(return_value={"files": 2, "downloads": 9, "bytes": 4096})
        ):
            await common.my_stats_command(None, message)

        self.assertIn("text", sent, "the handler produced no message at all")
        self.assertIn("2", sent["text"])
        self.assertIn("9", sent["text"])

    async def test_a_database_failure_still_replies(self):
        message, sent = self._message()

        with mock.patch.object(
            common.db, "get_source_stats",
            new=mock.AsyncMock(side_effect=RuntimeError("mongo down"))
        ):
            await common.my_stats_command(None, message)

        self.assertIn("text", sent)


if __name__ == "__main__":
    unittest.main()
