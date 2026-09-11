"""YouTube link-preview videos are opt-in, and removable (#440).

Telegram attaches the playable file to its own link preview, so a posted
YouTube link archives like any other document -- tens of megabytes per link,
duplicating bytes that still live at the URL. (There is no downloader in this
project; the reporter assumed yt-dlp, but declining Telegram's attachment is
the whole mechanism.)

``DOWNLOAD_YOUTUBE_VIDEOS`` is therefore off by default, and a second flag,
``YOUTUBE_VIDEOS_DELETE_EXISTING``, removes what earlier runs downloaded. Two
flags, not one: the download flag defaults to off, so deleting on that alone
would erase already-archived video on every existing deployment the first time
it upgraded.

Three things the tests below pin, because each was a real trap:

* the thumbnail is NOT the video. A ``WebPage`` carries a ``.photo`` (the card
  picture, tens of KB) or a ``.document`` (the file). Only the document side is
  gated, so a gated archive keeps its cards.
* a gated row is declined, not failed. Charging it a download attempt would walk
  it to MEDIA_MAX_DOWNLOAD_ATTEMPTS and have the run warn it gave up on a file
  nobody asked it to fetch.
* deleting the chat-folder entry frees nothing. With DEDUPLICATE_MEDIA on it is
  a symlink into ``_shared/``, and the archive that prompted this had ONE 56 MB
  blob behind three rows. The blob must go too -- but only once the last row
  referencing its hash is gone, counted across all accounts.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.listener import TelegramListener
from src.message_utils import is_youtube_preview_video, is_youtube_url
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _named(class_name: str, **attrs):
    obj = type(class_name, (), {})()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _webpage_media(url=YOUTUBE_URL, photo=None, document=None, page_class="WebPage"):
    webpage = _named(page_class, url=url, photo=photo, document=document)
    return _named("MessageMediaWebPage", webpage=webpage)


def _video_doc(doc_id=555, size=56_000_000):
    return SimpleNamespace(
        id=doc_id,
        size=size,
        mime_type="video/mp4",
        attributes=[_named("DocumentAttributeVideo", w=1280, h=720, duration=212, round_message=False)],
    )


def _preview_photo(photo_id=987, size=120_000):
    return SimpleNamespace(id=photo_id, sizes=[SimpleNamespace(type="m", size=size)])


def _youtube_video_media():
    return _webpage_media(document=_video_doc())


class TestIsYoutubeUrl(unittest.TestCase):
    def test_the_hosts_youtube_actually_serves_watch_pages_from(self):
        for url in (
            "https://www.youtube.com/watch?v=abc",
            "https://youtube.com/watch?v=abc",
            "https://m.youtube.com/watch?v=abc",
            "https://music.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
            "https://www.youtube-nocookie.com/embed/abc",
            "https://www.youtubekids.com/watch?v=abc",
            "http://YouTube.COM/watch?v=abc",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_youtube_url(url))

    def test_display_url_has_no_scheme(self):
        """``raw_data.webpage`` stores display_url too, and urlsplit reads a
        scheme-less value as a bare PATH with no hostname at all."""
        self.assertTrue(is_youtube_url("youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_url("youtu.be/abc"))

    def test_a_trailing_root_dot_is_still_the_same_host(self):
        self.assertTrue(is_youtube_url("https://youtube.com./watch?v=abc"))

    def test_lookalike_hosts_are_not_youtube(self):
        """The reason this matches whole labels instead of a substring: every
        one of these contains 'youtube.com' or 'youtu.be'."""
        for url in (
            "https://youtube.com.evil.example/watch?v=abc",
            "https://notyoutube.com/watch?v=abc",
            "https://myyoutu.be/abc",
            "https://example.com/?next=https://youtube.com/watch?v=abc",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_youtube_url(url))

    def test_other_sites_and_junk(self):
        for url in ("https://vimeo.com/123", "https://twitch.tv/x", "", None, 42, b"https://youtube.com/x"):
            with self.subTest(url=url):
                self.assertFalse(is_youtube_url(url))

    def test_a_malformed_url_is_answered_false_not_raised(self):
        self.assertFalse(is_youtube_url("http://[oops"))


class TestIsYoutubePreviewVideo(unittest.TestCase):
    def test_a_document_backed_youtube_preview_is_the_video(self):
        self.assertTrue(is_youtube_preview_video(_youtube_video_media()))

    def test_the_card_thumbnail_is_not_the_video(self):
        """The whole point of gating on .document: a photo-backed preview is the
        card picture (tens of KB) and the archive keeps it."""
        self.assertFalse(is_youtube_preview_video(_webpage_media(photo=_preview_photo())))

    def test_a_document_preview_from_another_site_is_untouched(self):
        self.assertFalse(is_youtube_preview_video(_webpage_media(url="https://vimeo.com/1", document=_video_doc())))

    def test_a_video_sent_in_the_chat_is_untouched(self):
        """A real upload is MessageMediaDocument, never a link preview -- this
        flag must never reach the videos people actually send."""
        self.assertFalse(is_youtube_preview_video(_named("MessageMediaDocument", document=_video_doc())))

    def test_unresolved_and_empty_previews(self):
        for page_class in ("WebPageEmpty", "WebPagePending", "WebPageNotModified"):
            with self.subTest(page_class=page_class):
                self.assertFalse(is_youtube_preview_video(_webpage_media(document=_video_doc(), page_class=page_class)))

    def test_a_preview_with_neither_payload(self):
        self.assertFalse(is_youtube_preview_video(_webpage_media()))

    def test_inert_against_a_bare_mock(self):
        """Name-based dispatch, like every sibling detector: a MagicMock must
        not classify as a YouTube video just because every attribute is truthy."""
        self.assertFalse(is_youtube_preview_video(MagicMock()))


class _AsyncCase(unittest.TestCase):
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


class TestSweepDeclinesTheVideo(_AsyncCase):
    def _make_backup(self, *, download_youtube_videos):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = media_root
        backup.config.deduplicate_media = False
        backup.config.download_youtube_videos = download_youtube_videos
        backup.config.get_max_media_size_bytes = MagicMock(return_value=1024 * 1024 * 1024)
        backup.db = AsyncMock()
        backup.db.reconcile_media_row = AsyncMock(return_value=None)
        backup.client = AsyncMock()

        async def fake_download(_message, path, _size, _chat_id):
            with open(path, "wb") as handle:
                handle.write(b"videobytes")
            return path

        backup._download_media_to_path = AsyncMock(side_effect=fake_download)
        return backup

    def _message(self, media, message_id=41):
        message = MagicMock()
        message.id = message_id
        message.media = media
        return message

    def test_off_by_default_declines_and_writes_no_row(self):
        """No row at all, not a pending one: a downloaded=0 row under the size
        cap is exactly what get_pending_media_downloads re-attempts every run."""
        backup = self._make_backup(download_youtube_videos=False)
        self.assertIsNone(self._run(backup._process_media(self._message(_youtube_video_media()), CHAT_ID)))
        backup._download_media_to_path.assert_not_awaited()

    def test_enabling_it_restores_todays_behaviour(self):
        backup = self._make_backup(download_youtube_videos=True)
        result = self._run(backup._process_media(self._message(_youtube_video_media()), CHAT_ID))
        self.assertEqual(result["type"], "webpage")
        self.assertTrue(result["downloaded"])
        backup._download_media_to_path.assert_awaited()

    def test_the_card_thumbnail_still_downloads_while_gated(self):
        backup = self._make_backup(download_youtube_videos=False)
        result = self._run(backup._process_media(self._message(_webpage_media(photo=_preview_photo())), CHAT_ID))
        self.assertEqual(result["type"], "webpage")
        self.assertTrue(result["downloaded"])

    def test_another_sites_preview_video_still_downloads(self):
        backup = self._make_backup(download_youtube_videos=False)
        media = _webpage_media(url="https://vimeo.com/1", document=_video_doc())
        result = self._run(backup._process_media(self._message(media), CHAT_ID))
        self.assertTrue(result["downloaded"])

    def test_a_copy_already_on_disk_keeps_its_row(self):
        """The gate declines; it never deletes. Only
        YOUTUBE_VIDEOS_DELETE_EXISTING removes what is already archived."""
        backup = self._make_backup(download_youtube_videos=False)
        on_disk = os.path.join(backup.config.media_path, str(CHAT_ID), "555_video.mp4")
        os.makedirs(os.path.dirname(on_disk), exist_ok=True)
        with open(on_disk, "wb") as handle:
            handle.write(b"already here")
        existing = {"id": f"{CHAT_ID}_41_webpage", "type": "webpage", "downloaded": 1, "file_path": on_disk}
        backup.db.reconcile_media_row = AsyncMock(return_value=existing)

        result = self._run(backup._process_media(self._message(_youtube_video_media()), CHAT_ID))
        self.assertIs(result, existing)
        self.assertTrue(os.path.exists(on_disk))


class TestRetryDrainDeclinesWithoutCharging(_AsyncCase):
    def test_a_gated_row_is_skipped_without_burning_an_attempt(self):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.download_youtube_videos = False
        backup.config.skip_media_chat_ids = set()
        backup.config.max_media_download_attempts = 3
        backup.config.get_max_media_size_bytes = MagicMock(return_value=1024 * 1024 * 1024)
        backup.db = AsyncMock()
        backup.db.get_pending_media_downloads = AsyncMock(
            return_value=[{"id": "row-1", "chat_id": CHAT_ID, "message_id": 41, "type": "webpage"}]
        )
        backup.db.count_capped_media_downloads = AsyncMock(return_value=0)
        backup.db.increment_media_download_attempts = AsyncMock()
        backup._process_media = AsyncMock()

        message = MagicMock()
        message.id = 41
        message.media = _youtube_video_media()
        backup.client = MagicMock()
        backup.client.get_messages = AsyncMock(return_value=[message])

        self._run(backup._retry_pending_media_downloads())

        backup._process_media.assert_not_awaited()
        backup.db.increment_media_download_attempts.assert_not_awaited()


def _listener_with_handlers(*, download_youtube_videos):
    """The live lane, driven through its real ``on_new_message`` closure.

    Both capture lanes or neither: the sweep and the listener each own a copy of
    the media decision, and a gate only one of them applies means the file
    arrives anyway on whichever lane saw the message first.
    """
    from telethon import events

    config = MagicMock()
    config.api_id = 12345
    config.api_hash = "test@value/here"
    config.phone = "+1234567890"
    config.session_path = "/tmp/test_session"
    config.validate_credentials = MagicMock()
    config.whitelist_mode = False
    config.listen_new_messages = True
    config.listen_new_messages_media = True
    config.listen_edits = False
    config.listen_deletions = False
    config.listen_chat_actions = False
    config.should_skip_topic = MagicMock(return_value=False)
    config.should_download_media_for_chat = MagicMock(return_value=True)
    config.should_backup_chat = MagicMock(return_value=True)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    config.download_youtube_videos = download_youtube_videos

    db = AsyncMock()
    db.insert_message = AsyncMock()
    db.insert_media = AsyncMock()
    db.reconcile_media_row = AsyncMock(return_value=None)
    db.upsert_chat = AsyncMock()
    db.upsert_user = AsyncMock()

    listener = TelegramListener(config, db, account_id=1)
    listener._tracked_chat_ids = {CHAT_ID}
    listener._notifier = None
    listener._download_media = AsyncMock(return_value=("/data/media/x/v.mp4", "v.mp4", "ab" * 32))

    handlers = {}

    def capture_on(event_type):
        def decorator(fn):
            handlers[event_type] = fn
            return fn

        return decorator

    client = MagicMock()
    client.on = capture_on
    listener.client = client
    listener._register_handlers()
    return listener, handlers[events.NewMessage]


def _youtube_event():
    event = MagicMock()
    event.chat_id = CHAT_ID
    event.is_private = False
    event.is_group = True
    event.is_channel = False
    chat_entity = SimpleNamespace(id=abs(CHAT_ID), title="t", username=None, first_name=None, last_name=None)
    event.get_chat = AsyncMock(return_value=chat_entity)
    event.get_sender = AsyncMock(return_value=None)

    message = MagicMock()
    message.id = 41
    message.reply_to = None
    message.reply_to_msg_id = None
    message.grouped_id = None
    message.sender = None
    message.sender_id = 7
    message.date = datetime(2026, 9, 11, 12, 0, 0)
    message.edit_date = None
    message.entities = None
    message.out = False
    message.media = _youtube_video_media()
    event.message = message
    return event


def test_the_live_lane_declines_the_video_too():
    listener, handler = _listener_with_handlers(download_youtube_videos=False)
    asyncio.run(handler(_youtube_event()))

    listener.db.insert_message.assert_awaited()  # the message and its card are archived
    listener._download_media.assert_not_awaited()
    listener.db.insert_media.assert_not_awaited()


def test_the_live_lane_downloads_it_when_enabled():
    listener, handler = _listener_with_handlers(download_youtube_videos=True)
    asyncio.run(handler(_youtube_event()))

    listener._download_media.assert_awaited()
    listener.db.insert_media.assert_awaited()


class TestCleanupExistingVideos(_AsyncCase):
    """The file-side behaviour of _cleanup_youtube_videos, on a real tree."""

    def _make_backup(self, *, download_youtube_videos=False, delete_existing=True):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = media_root
        backup.config.download_youtube_videos = download_youtube_videos
        backup.config.youtube_videos_delete_existing = delete_existing
        backup.db = AsyncMock()
        backup.db.delete_media_records = AsyncMock(return_value=0)
        backup.db.count_media_by_content_hash = AsyncMock(return_value={})
        return backup

    def _plant_deduplicated(self, backup, *, content_hash, file_name, chats, blob_bytes=b"x" * 4096):
        """One shared blob, one symlink per chat -- the production layout."""
        shared_dir = os.path.join(backup.config.media_path, "_shared", content_hash[:2])
        os.makedirs(shared_dir, exist_ok=True)
        blob_path = os.path.join(shared_dir, file_name)
        with open(blob_path, "wb") as handle:
            handle.write(blob_bytes)
        links = []
        for chat_id in chats:
            chat_dir = os.path.join(backup.config.media_path, str(chat_id))
            os.makedirs(chat_dir, exist_ok=True)
            link_path = os.path.join(chat_dir, file_name)
            os.symlink(blob_path, link_path)
            links.append(link_path)
        return blob_path, links

    def _record(self, media_id, chat_id, file_path, file_name, content_hash, url=YOUTUBE_URL):
        return {
            "id": media_id,
            "chat_id": chat_id,
            "message_id": 41,
            "file_path": file_path,
            "file_name": file_name,
            "file_size": 4096,
            "content_hash": content_hash,
            "downloaded": 1,
            "url": url,
        }

    def test_the_shared_blob_goes_when_its_last_row_does(self):
        """Three rows, one 56 MB blob -- the exact shape of the archive that
        prompted this. Deleting only the symlinks would free zero bytes."""
        backup = self._make_backup()
        blob, links = self._plant_deduplicated(backup, content_hash="ab" * 32, file_name="v.mp4", chats=[-1, -2, -3])
        records = [
            self._record(f"r{i}", chat, link, "v.mp4", "ab" * 32)
            for i, (chat, link) in enumerate(zip([-1, -2, -3], links, strict=True))
        ]
        backup.db.get_webpage_preview_documents = AsyncMock(return_value=records)
        backup.db.delete_media_records = AsyncMock(return_value=3)
        backup.db.count_media_by_content_hash = AsyncMock(return_value={})  # nothing references it now

        self._run(backup._cleanup_youtube_videos())

        for link in links:
            self.assertFalse(os.path.lexists(link))
        self.assertFalse(os.path.exists(blob))
        backup.db.delete_media_records.assert_awaited_once()
        self.assertEqual(sorted(backup.db.delete_media_records.await_args.args[0]), ["r0", "r1", "r2"])

    def test_a_blob_another_row_still_points_at_survives(self):
        """The safety that makes the reap legal at all. One chat's link is
        removed; a row elsewhere still references the hash, so the bytes stay."""
        backup = self._make_backup()
        blob, links = self._plant_deduplicated(backup, content_hash="cd" * 32, file_name="v.mp4", chats=[-1, -2])
        backup.db.get_webpage_preview_documents = AsyncMock(
            return_value=[self._record("r0", -1, links[0], "v.mp4", "cd" * 32)]
        )
        backup.db.delete_media_records = AsyncMock(return_value=1)
        backup.db.count_media_by_content_hash = AsyncMock(return_value={"cd" * 32: 1})

        self._run(backup._cleanup_youtube_videos())

        self.assertFalse(os.path.lexists(links[0]))
        self.assertTrue(os.path.lexists(links[1]))
        self.assertTrue(os.path.exists(blob))

    def test_a_refcount_query_that_fails_deletes_no_bytes(self):
        backup = self._make_backup()
        blob, links = self._plant_deduplicated(backup, content_hash="ef" * 32, file_name="v.mp4", chats=[-1])
        backup.db.get_webpage_preview_documents = AsyncMock(
            return_value=[self._record("r0", -1, links[0], "v.mp4", "ef" * 32)]
        )
        backup.db.delete_media_records = AsyncMock(return_value=1)
        backup.db.count_media_by_content_hash = AsyncMock(side_effect=RuntimeError("db down"))

        self._run(backup._cleanup_youtube_videos())
        self.assertTrue(os.path.exists(blob))

    def test_an_undeduplicated_file_is_removed_directly(self):
        backup = self._make_backup()
        backup.config.deduplicate_media = False
        chat_dir = os.path.join(backup.config.media_path, "-1")
        os.makedirs(chat_dir, exist_ok=True)
        real_file = os.path.join(chat_dir, "v.mp4")
        with open(real_file, "wb") as handle:
            handle.write(b"y" * 2048)
        backup.db.get_webpage_preview_documents = AsyncMock(
            return_value=[self._record("r0", -1, real_file, "v.mp4", None)]
        )
        backup.db.delete_media_records = AsyncMock(return_value=1)

        self._run(backup._cleanup_youtube_videos())
        self.assertFalse(os.path.exists(real_file))

    def test_rows_from_other_sites_are_left_alone(self):
        backup = self._make_backup()
        _blob, links = self._plant_deduplicated(backup, content_hash="12" * 32, file_name="v.mp4", chats=[-1])
        backup.db.get_webpage_preview_documents = AsyncMock(
            return_value=[self._record("r0", -1, links[0], "v.mp4", "12" * 32, url="https://vimeo.com/1")]
        )
        self._run(backup._cleanup_youtube_videos())
        self.assertTrue(os.path.lexists(links[0]))
        backup.db.delete_media_records.assert_not_awaited()

    def test_it_does_nothing_while_the_download_is_enabled(self):
        backup = self._make_backup(download_youtube_videos=True, delete_existing=True)
        backup.db.get_webpage_preview_documents = AsyncMock(return_value=[])
        self._run(backup._cleanup_youtube_videos())
        backup.db.get_webpage_preview_documents.assert_not_awaited()

    def test_it_does_nothing_without_the_second_flag(self):
        """The flag that stops an upgrade deleting other people's archives:
        DOWNLOAD_YOUTUBE_VIDEOS is off on every deployment by default."""
        backup = self._make_backup(download_youtube_videos=False, delete_existing=False)
        backup.db.get_webpage_preview_documents = AsyncMock(return_value=[])
        self._run(backup._cleanup_youtube_videos())
        backup.db.get_webpage_preview_documents.assert_not_awaited()

    def test_a_scan_failure_is_survivable(self):
        backup = self._make_backup()
        backup.db.get_webpage_preview_documents = AsyncMock(side_effect=RuntimeError("db down"))
        self._run(backup._cleanup_youtube_videos())
        backup.db.delete_media_records.assert_not_awaited()


@pytest.fixture
async def sqlite_adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'telegram_archive.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _plant(adapter, *, media_id, chat_id, message_id, mime_type, url, content_hash="ab" * 32):
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "text": "look at this",
            "date": datetime(2026, 9, 11, 12, 0, 0),
            "raw_data": {"webpage": {"url": url}} if url else {},
        },
        account_id=1,
    )
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "type": "webpage",
            "file_path": f"/data/media/{chat_id}/v.mp4",
            "file_name": "v.mp4",
            "file_size": 4096,
            "mime_type": mime_type,
            "content_hash": content_hash,
            "downloaded": True,
        },
        account_id=1,
    )


@pytest.mark.asyncio
async def test_adapter_returns_document_previews_with_their_url(sqlite_adapter):
    await _plant(sqlite_adapter, media_id="vid", chat_id=-1, message_id=1, mime_type="video/mp4", url=YOUTUBE_URL)
    await _plant(sqlite_adapter, media_id="thumb", chat_id=-1, message_id=2, mime_type=None, url=YOUTUBE_URL)

    rows = await sqlite_adapter.get_webpage_preview_documents(account_id=1)

    # The thumbnail row (mime_type NULL) is not a candidate for deletion.
    assert [row["id"] for row in rows] == ["vid"]
    assert rows[0]["url"] == YOUTUBE_URL
    assert rows[0]["content_hash"] == "ab" * 32


@pytest.mark.asyncio
async def test_adapter_survives_a_message_with_no_webpage_card(sqlite_adapter):
    await _plant(sqlite_adapter, media_id="vid", chat_id=-1, message_id=1, mime_type="video/mp4", url=None)
    rows = await sqlite_adapter.get_webpage_preview_documents(account_id=1)
    assert rows[0]["url"] is None


@pytest.mark.asyncio
async def test_adapter_deletes_only_the_named_rows(sqlite_adapter):
    await _plant(sqlite_adapter, media_id="a", chat_id=-1, message_id=1, mime_type="video/mp4", url=YOUTUBE_URL)
    await _plant(sqlite_adapter, media_id="b", chat_id=-1, message_id=2, mime_type="video/mp4", url=YOUTUBE_URL)

    assert await sqlite_adapter.delete_media_records(["a"], account_id=1) == 1
    assert [row["id"] for row in await sqlite_adapter.get_webpage_preview_documents(account_id=1)] == ["b"]
    assert await sqlite_adapter.delete_media_records([], account_id=1) == 0


@pytest.mark.asyncio
async def test_content_hash_refcount_counts_surviving_rows(sqlite_adapter):
    await _plant(sqlite_adapter, media_id="a", chat_id=-1, message_id=1, mime_type="video/mp4", url=YOUTUBE_URL)
    await _plant(sqlite_adapter, media_id="b", chat_id=-2, message_id=2, mime_type="video/mp4", url=YOUTUBE_URL)

    assert await sqlite_adapter.count_media_by_content_hash(["ab" * 32]) == {"ab" * 32: 2}
    await sqlite_adapter.delete_media_records(["a", "b"], account_id=1)
    assert await sqlite_adapter.count_media_by_content_hash(["ab" * 32]) == {}
    assert await sqlite_adapter.count_media_by_content_hash([]) == {}
    assert await sqlite_adapter.count_media_by_content_hash([None]) == {}
