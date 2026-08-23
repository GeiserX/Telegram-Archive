"""Link-preview photos download like any photo (9t6.10.4 remainder).

The mf7 card captured url/title/description at archive time, but the
preview IMAGE was lost: ``MessageMediaWebPage`` fell out of both
``_get_media_type`` ladders as None, so no media row existed and nothing
downloaded. Official apps show the thumbnail; a dead link in an old archive
kept only text. Now a webpage whose ``WebPage`` carries a photo or document
classifies as ``webpage``, its payload is unwrapped for every size/id/
filename sniffer (Telethon's ``download_media`` unwraps the same way), and
the viewer's card renders the downloaded image.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.listener import TelegramListener
from src.message_utils import downloadable_media_payload, fallback_media_filename
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001


def _named(class_name: str, **attrs):
    obj = type(class_name, (), {})()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _webpage_media(photo=None, document=None, page_class="WebPage"):
    webpage = _named(page_class, photo=photo, document=document)
    return _named("MessageMediaWebPage", webpage=webpage)


def _preview_photo(photo_id=987, size=4000):
    return SimpleNamespace(id=photo_id, sizes=[SimpleNamespace(type="m", size=size)])


class TestDownloadableMediaPayload(unittest.TestCase):
    def test_webpage_unwraps_to_the_webpage_object(self):
        media = _webpage_media(photo=_preview_photo())
        self.assertIs(downloadable_media_payload(media), media.webpage)

    def test_webpage_empty_passes_through(self):
        media = _webpage_media(page_class="WebPageEmpty")
        self.assertIs(downloadable_media_payload(media), media)

    def test_other_media_and_mocks_pass_through(self):
        photo = _named("MessageMediaPhoto", photo=_preview_photo())
        self.assertIs(downloadable_media_payload(photo), photo)
        mock = MagicMock()
        self.assertIs(downloadable_media_payload(mock), mock)


class TestWebpageMediaType(unittest.TestCase):
    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.listener = TelegramListener.__new__(TelegramListener)

    def test_webpage_with_photo_classifies_on_both_ladders(self):
        media = _webpage_media(photo=_preview_photo())
        self.assertEqual(self.backup._get_media_type(media), "webpage")
        self.assertEqual(self.listener._get_media_type(media), "webpage")

    def test_webpage_with_document_classifies(self):
        media = _webpage_media(document=SimpleNamespace(id=5, size=100, mime_type="image/gif", attributes=[]))
        self.assertEqual(self.backup._get_media_type(media), "webpage")

    def test_card_only_previews_stay_none(self):
        self.assertIsNone(self.backup._get_media_type(_webpage_media()))
        self.assertIsNone(self.backup._get_media_type(_webpage_media(page_class="WebPageEmpty")))
        self.assertIsNone(self.listener._get_media_type(_webpage_media()))


class TestSweepDownloadsPreviewPhoto(unittest.TestCase):
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_backup(self, media_root):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = media_root
        backup.config.deduplicate_media = False
        backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        backup.db = AsyncMock()
        backup.client = AsyncMock()

        async def fake_download(_message, path, _size, _chat_id):
            with open(path, "wb") as handle:
                handle.write(b"previewbytes")
            return path

        backup._download_media_to_path = AsyncMock(side_effect=fake_download)
        return backup

    def test_webpage_photo_flows_through_the_download_path(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = self._make_backup(media_root)

        message = MagicMock()
        message.id = 31
        message.media = _webpage_media(photo=_preview_photo(photo_id=987, size=4000))

        result = self._run(backup._process_media(message, CHAT_ID))
        self.assertEqual(result["type"], "webpage")
        # The record keeps the ACTUAL on-disk size (#263), not Telegram's estimate.
        self.assertEqual(result["file_size"], len(b"previewbytes"))
        self.assertTrue(result["downloaded"])
        self.assertTrue(result["file_name"].startswith("987"))
        backup._download_media_to_path.assert_awaited()

    def test_oversized_preview_is_size_capped(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = self._make_backup(media_root)
        backup.config.get_max_media_size_bytes = MagicMock(return_value=100)

        message = MagicMock()
        message.id = 32
        message.media = _webpage_media(photo=_preview_photo(size=5000))

        result = self._run(backup._process_media(message, CHAT_ID))
        self.assertEqual(result["type"], "webpage")
        self.assertNotIn("downloaded", result)
        backup._download_media_to_path.assert_not_awaited()


class TestListenerDownloadsPreviewPhoto(unittest.TestCase):
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_listener_download_path_handles_webpage(self):
        listener = TelegramListener.__new__(TelegramListener)
        listener.db = AsyncMock()
        listener.account_id = 1
        listener.config = MagicMock()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        listener.config.media_path = tmp
        listener.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        listener.config.deduplicate_media = False
        listener.client = AsyncMock()

        async def fake_download(_message, path):
            with open(path, "wb") as handle:
                handle.write(b"previewbytes")
            return path

        listener.client.download_media = AsyncMock(side_effect=fake_download)

        message = MagicMock()
        message.id = 33
        message.media = _webpage_media(photo=_preview_photo(photo_id=654, size=2000))

        result = self._run(listener._download_media(message, CHAT_ID))
        self.assertIsNotNone(result)
        _path, file_name, _hash = result
        self.assertTrue(file_name.startswith("654"))
        listener.client.download_media.assert_awaited()


class TestFilenameFallback(unittest.TestCase):
    def test_webpage_defaults_to_jpg(self):
        self.assertEqual(fallback_media_filename("987", "webpage", None), "987.jpg")
        self.assertEqual(fallback_media_filename(None, "webpage", None, 55), "55_webpage.jpg")


class TestViewerTemplate(unittest.TestCase):
    def test_card_shows_downloaded_image_and_generic_block_excludes_webpage(self):
        html = open(os.path.join(os.path.dirname(__file__), "..", "src", "web", "templates", "index.html")).read()
        self.assertIn("msg.media?.type === 'webpage' && msg.media?.file_path", html)
        self.assertIn("msg.media?.type !== 'webpage' && !getExtendedMediaChip(msg)", html)
