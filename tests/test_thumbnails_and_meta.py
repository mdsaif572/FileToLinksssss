# tests/test_thumbnails_and_meta.py
"""Thumbnail endpoint and the OpenGraph tags that make links unfurl."""

import io
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

from Thunder.server import stream_routes as sr  # noqa: E402
from Thunder.utils.canonical_files import _largest_thumb_file_id  # noqa: E402
from Thunder.utils.render_template import render_media_page  # noqa: E402
from tests.test_link_lifecycle import HASH, _record  # noqa: E402

THUMB_BYTES = b"\xff\xd8\xff\xe0thumbnail-bytes"


class Thumb:
    def __init__(self, file_id, width, height):
        self.file_id = file_id
        self.width = width
        self.height = height


class Media:
    def __init__(self, thumbs):
        self.thumbs = thumbs


class LargestThumbTest(unittest.TestCase):
    def test_picks_the_biggest_thumbnail(self):
        media = Media([Thumb("small", 90, 60), Thumb("big", 320, 180), Thumb("mid", 160, 90)])
        self.assertEqual(_largest_thumb_file_id(media), "big")

    def test_no_thumbs(self):
        self.assertIsNone(_largest_thumb_file_id(Media([])))
        self.assertIsNone(_largest_thumb_file_id(object()))


class RenderMetaTest(unittest.IsolatedAsyncioTestCase):
    async def test_og_tags_with_a_thumbnail(self):
        page = await render_media_page(
            "movie.mkv", "https://cdn/f/abc/movie.mkv", "stream",
            thumb_url="https://cdn/thumb/abc"
        )
        self.assertIn('property="og:title" content="movie.mkv"', page)
        self.assertIn('property="og:image" content="https://cdn/thumb/abc"', page)
        self.assertIn('name="twitter:card" content="summary_large_image"', page)

    async def test_og_tags_without_a_thumbnail(self):
        page = await render_media_page("movie.mkv", "https://cdn/f/abc/movie.mkv", "download")
        self.assertIn('property="og:title" content="movie.mkv"', page)
        self.assertNotIn("og:image", page)
        self.assertIn('name="twitter:card" content="summary"', page)


class FakeBot:
    def __init__(self, payload=THUMB_BYTES, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = []

    async def download_media(self, file_id, in_memory=False):
        self.calls.append((file_id, in_memory))
        if self.fail:
            raise RuntimeError("telegram said no")
        return io.BytesIO(self.payload)


class ThumbnailRouteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_lookup = sr.get_file_by_hash
        self.bot = FakeBot()
        self._bot_patch = mock.patch.object(sr, "StreamBot", self.bot)
        self._bot_patch.start()
        self.addCleanup(self._bot_patch.stop)

        self.record = _record(thumb_file_id="thumb-file-id")
        sr.get_file_by_hash = self._lookup

        app = web.Application()
        app.add_routes(sr.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        sr.get_file_by_hash = self._original_lookup

    async def _lookup(self, public_hash, **kwargs):
        return self.record

    async def test_serves_the_thumbnail(self):
        async with self.client.get(f"/thumb/{HASH}") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
            self.assertEqual(await resp.read(), THUMB_BYTES)

        self.assertEqual(self.bot.calls, [("thumb-file-id", True)])

    async def test_404_when_the_record_has_no_thumbnail(self):
        self.record = _record()
        async with self.client.get(f"/thumb/{HASH}") as resp:
            self.assertEqual(resp.status, 404)

    async def test_410_when_the_link_is_revoked(self):
        self.record = _record(thumb_file_id="thumb-file-id", revoked=True)
        async with self.client.get(f"/thumb/{HASH}") as resp:
            self.assertEqual(resp.status, 410)

    async def test_500_when_telegram_refuses(self):
        self.bot.fail = True
        async with self.client.get(f"/thumb/{HASH}") as resp:
            self.assertEqual(resp.status, 500)

    async def test_watch_page_references_the_thumbnail(self):
        async with self.client.get(f"/watch/f/{HASH}/sample.mkv") as resp:
            page = await resp.text()
        self.assertEqual(resp.status, 200)
        self.assertIn(f"/thumb/{HASH}", page)


if __name__ == "__main__":
    unittest.main()
