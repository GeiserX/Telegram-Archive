"""An edit that replaces a message's photo or file keeps the old one (036).

Telegram lets a sender replace a message's media on edit. The edit paths
compared only the text, so the old file stayed, the new media was never
downloaded and the new caption sat beside the old picture; a later
VERIFY_MEDIA pass could even fetch the new media into the old row.

These tests run the real paths against a real engine (SQLite and PostgreSQL,
see ``real_adapter`` in conftest): the scheduled sync, the listener's edit
handler and VERIFY_MEDIA each keep the old media in ``media_versions`` and
download the new file into the message's media row, never into the old one.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telethon import events
from telethon.tl.types import MessageMediaPhoto, PhotoSize

from telegram_archive.db.models import Media, MediaTranscript, MediaVersion, Message, MessageVersion
from telegram_archive.listener import TelegramListener
from telegram_archive.message_utils import build_media_filename, media_file_id, stored_media_file_id
from telegram_archive.telegram_backup import TelegramBackup

CHAT_ID = -1009990001
OTHER_CHAT_ID = -1009990002
MESSAGE_ID = 5
SENT = datetime(2026, 3, 1, 9, 0, 0)
EDITED = datetime(2026, 3, 1, 9, 30, 0)
EDITED_AGAIN = datetime(2026, 3, 1, 10, 0, 0)
OLD_PHOTO = 7000000000000000111
NEW_PHOTO = 7000000000000000222
THIRD_PHOTO = 7000000000000000333
FOURTH_PHOTO = 7000000000000000444


def _photo(photo_id: int):
    media = MagicMock(spec=MessageMediaPhoto)
    media.photo = SimpleNamespace(id=photo_id, sizes=[PhotoSize(type="m", w=320, h=240, size=4000)])
    return media


def _telegram_message(photo_id: int, *, text: str = "Look at this", edit_date: datetime | None = None):
    """A Telegram message as the sync and VERIFY_MEDIA read it (tz-aware dates)."""
    return SimpleNamespace(
        id=MESSAGE_ID,
        date=SENT.replace(tzinfo=UTC),
        edit_date=edit_date.replace(tzinfo=UTC) if edit_date else None,
        edit_hide=False,
        message=text,
        entities=None,
        media=_photo(photo_id),
        reactions=None,
        reply_to=None,
        reply_to_msg_id=None,
        sender=None,
        sender_id=4242,
        grouped_id=None,
        out=False,
        fwd_from=None,
        action=None,
    )


def _named(class_name: str, **attrs):
    """An object whose class has ``class_name``, which is what the classifier reads."""
    obj = type(class_name, (), {})()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _preview(photo_id: int):
    """A link preview whose card picture is photo ``photo_id``."""
    photo = SimpleNamespace(id=photo_id, sizes=[PhotoSize(type="m", w=320, h=240, size=4000)])
    webpage = _named("WebPage", url="https://example.com/page", photo=photo, document=None)
    return _named("MessageMediaWebPage", webpage=webpage)


def _preview_message(photo_id: int, *, edit_date: datetime | None = None):
    message = _telegram_message(photo_id, text="Read https://example.com/page", edit_date=edit_date)
    message.media = _preview(photo_id)
    return message


def _event(message) -> MagicMock:
    """A NewMessage or MessageEdited event carrying ``message``."""
    event = MagicMock()
    event.chat_id = CHAT_ID
    event.message = message
    event.get_chat = AsyncMock(return_value=None)
    event.get_sender = AsyncMock(return_value=None)
    return event


def _listener(adapter, media_root: str) -> tuple[TelegramListener, dict]:
    """A listener on the real adapter, with its registered handlers by event type."""
    config = _config(media_root)
    config.listen_edits = True
    config.listen_new_messages = True
    config.listen_new_messages_media = True
    config.listen_deletions = False
    config.listen_chat_actions = False
    config.whitelist_mode = False
    config.chat_ids = set()
    config.global_include_ids = set()
    config.private_include_ids = set()
    config.groups_include_ids = set()
    config.channels_include_ids = set()
    config.should_backup_chat = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    listener = TelegramListener(config, adapter, account_id=1)
    listener._tracked_chat_ids = {CHAT_ID}
    listener._notifier = None
    listener._enqueue_transcription = MagicMock()
    handlers = {}
    client = MagicMock()

    def capture_on(event_type):
        def decorator(fn):
            handlers[event_type] = fn
            return fn

        return decorator

    client.on = capture_on
    client.download_media = AsyncMock(side_effect=_fake_download)
    listener.client = client
    listener._register_handlers()
    return listener, handlers


def _held_download(photo_id: int) -> tuple[AsyncMock, asyncio.Event, asyncio.Event]:
    """A download that waits for ``release`` before writing ``photo_id``'s file.

    ``started`` is set once that download is running, so a test can deliver an
    event while it is still in flight. Every other photo downloads at once.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def download(message, path):
        if media_file_id(message.media) == str(photo_id):
            started.set()
            await release.wait()
        return await _fake_download(message, path)

    return AsyncMock(side_effect=download), started, release


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


def _config(media_root: str) -> MagicMock:
    config = MagicMock()
    config.media_path = media_root
    config.deduplicate_media = False
    config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
    config.max_filename_bytes = 255
    config.download_youtube_videos = False
    config.parallel_download_enabled = False
    config.media_flood_sleep_threshold = 0
    config.should_download_media_for_chat = MagicMock(return_value=True)
    config.should_skip_topic = MagicMock(return_value=False)
    config.skip_media_chat_ids = set()
    config.deletion_mode = "soft"
    config.download_media_types = set()
    config.download_document_mime_types = set()
    config.download_document_mime_extensions = set()
    config.max_media_download_attempts = 5
    return config


async def _fake_download(message, path):
    """Write bytes that name the photo, so each file shows which media it holds."""
    with open(path, "wb") as handle:
        handle.write(f"photo {media_file_id(message.media)}".encode())
    return path


def _backup(adapter, media_root: str, remote_messages: list) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = _config(media_root)
    backup.db = adapter
    backup.client = AsyncMock()
    backup.client.download_media = AsyncMock(side_effect=_fake_download)
    backup.client.get_messages = AsyncMock(return_value=remote_messages)
    backup._owns_client = True
    backup._cleaned_media_chats = set()
    backup._parallel_downloader = None
    return backup


async def _seed(adapter, *, text: str = "Look at this") -> None:
    await adapter.upsert_chat({"id": CHAT_ID, "type": "group", "title": "Test Group A"}, account_id=1)
    await adapter.insert_message(
        {"id": MESSAGE_ID, "chat_id": CHAT_ID, "sender_id": 4242, "date": SENT, "text": text, "raw_data": {}},
        account_id=1,
    )


async def _archive_old_photo(adapter, media_root: str) -> dict:
    """The message as first archived: its photo downloaded by the sweep."""
    backup = _backup(adapter, media_root, [])
    row = await backup._process_media(_telegram_message(OLD_PHOTO), CHAT_ID)
    await adapter.insert_media(row, account_id=1)
    return row


async def _rows(adapter, model) -> list:
    async with adapter.db_manager.async_session_factory() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars())


def _read(media_root: str, file_path: str) -> str:
    path = file_path if os.path.isabs(file_path) else os.path.join(media_root, file_path)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestSyncKeepsReplacedMedia:
    async def test_a_replaced_photo_is_kept_and_the_new_one_downloaded(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        (before,) = await _rows(real_adapter, Media)
        first_seen = before.created_at

        # Telegram now has another photo under the same caption.
        remote = _telegram_message(NEW_PHOTO, edit_date=EDITED)
        backup = _backup(real_adapter, media_root, [remote])
        await backup._sync_deletions_and_edits(CHAT_ID, object())

        (media,) = await _rows(real_adapter, Media)
        assert media.id != original["id"]
        # The new media was first recorded now, not when the old one was.
        assert media.created_at > first_seen
        assert media.telegram_file_id == str(NEW_PHOTO)
        assert media.downloaded == 1
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"

        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.media_id == original["id"]
        assert kept.telegram_file_id == str(OLD_PHOTO)
        assert kept.file_path == original["file_path"]
        assert kept.downloaded == 1
        assert kept.date == SENT
        assert kept.source == "sync"
        assert kept.first_seen == first_seen
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"

        # The edit is an edit: the date moved, and the text shown beside the
        # old photo is a version dated like it.
        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date == EDITED
        (version,) = await _rows(real_adapter, MessageVersion)
        assert (version.text, version.date) == ("Look at this", SENT)

        versions = await real_adapter.get_message_versions(CHAT_ID, MESSAGE_ID, account_id=1)
        assert len(versions) == 1
        assert [m["file_name"] for m in versions[0]["media"]] == [kept.file_name]

    async def test_a_second_sync_of_the_same_edit_changes_nothing(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        remote = _telegram_message(NEW_PHOTO, edit_date=EDITED)
        backup = _backup(real_adapter, media_root, [remote])

        await backup._sync_deletions_and_edits(CHAT_ID, object())
        first = [(m.id, m.file_path) for m in await _rows(real_adapter, Media)]
        await backup._sync_deletions_and_edits(CHAT_ID, object())

        assert [(m.id, m.file_path) for m in await _rows(real_adapter, Media)] == first
        assert len(await _rows(real_adapter, MediaVersion)) == 1
        assert len(await _rows(real_adapter, MessageVersion)) == 1

    async def test_a_caption_edit_keeps_the_media_row(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        remote = _telegram_message(OLD_PHOTO, text="Look at this one", edit_date=EDITED)

        await _backup(real_adapter, media_root, [remote])._sync_deletions_and_edits(CHAT_ID, object())

        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.file_path) == (original["id"], original["file_path"])
        assert await _rows(real_adapter, MediaVersion) == []
        (message,) = await _rows(real_adapter, Message)
        assert message.text == "Look at this one"

    async def test_skip_media_keeps_the_old_file_and_downloads_nothing(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        backup.client.download_media.reset_mock()

        await backup._sync_deletions_and_edits(CHAT_ID, object())

        backup.client.download_media.assert_not_awaited()
        (media,) = await _rows(real_adapter, Media)
        assert (media.downloaded, media.file_path, media.telegram_file_id) == (0, None, str(NEW_PHOTO))
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"

    async def test_an_oversize_replacement_is_recorded_without_its_file(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        backup.config.get_max_media_size_bytes = MagicMock(return_value=1)

        await backup._sync_deletions_and_edits(CHAT_ID, object())

        (media,) = await _rows(real_adapter, Media)
        assert (media.downloaded, media.file_path, media.skip_reason) == (0, None, "oversize")
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"

    async def test_an_oversize_old_media_keeps_its_reason(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        backup.config.get_max_media_size_bytes = MagicMock(return_value=1)
        await real_adapter.insert_media(
            await backup._process_media(_telegram_message(OLD_PHOTO), CHAT_ID), account_id=1
        )

        await backup._sync_deletions_and_edits(CHAT_ID, object())

        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.telegram_file_id, kept.downloaded, kept.skip_reason) == (str(OLD_PHOTO), 0, "oversize")


class TestVerifyMediaNeverFillsAnOldRow:
    async def test_a_missing_old_file_is_kept_as_a_version_not_refilled(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        os.remove(original["file_path"])  # the old file went missing

        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        await backup._verify_and_redownload_media()

        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.media_id, kept.telegram_file_id) == (original["id"], str(OLD_PHOTO))
        assert kept.file_path == original["file_path"]
        (media,) = await _rows(real_adapter, Media)
        assert media.id != original["id"]
        assert media.file_path != original["file_path"]
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"
        # The edit that replaced it is an edit: the date moves here, since a
        # later sync finds the new media in place and moves nothing.
        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date == EDITED

    async def test_a_damaged_old_file_is_left_where_it_is(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        with open(original["file_path"], "wb") as handle:
            handle.write(b"")  # truncated: VERIFY_MEDIA judges it corrupted

        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        await backup._verify_and_redownload_media()

        assert os.path.exists(original["file_path"])
        assert not os.path.exists(original["file_path"] + ".verify-bak")
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.file_path == original["file_path"]
        (media,) = await _rows(real_adapter, Media)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"

    async def test_control_the_same_photo_is_still_redownloaded_into_its_row(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        os.remove(original["file_path"])

        backup = _backup(real_adapter, media_root, [_telegram_message(OLD_PHOTO)])
        await backup._verify_and_redownload_media()

        assert await _rows(real_adapter, MediaVersion) == []
        (media,) = await _rows(real_adapter, Media)
        assert media.id == original["id"]
        assert _read(media_root, media.file_path) == f"photo {OLD_PHOTO}"


class TestPendingDownloadNeverFillsAnOldRow:
    async def test_a_pending_row_for_a_replaced_photo_is_kept_as_a_version(self, real_adapter, tmp_path, caplog):
        """A download that never happened, for a photo an edit then replaced:
        the retry keeps the old row as a version and fetches the new photo into
        the message's media row."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        pending_id = f"{CHAT_ID}_{MESSAGE_ID}_photo"
        await real_adapter.insert_media(
            {
                "id": pending_id,
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_size": 4000,
                "downloaded": False,
                "telegram_file_id": str(OLD_PHOTO),
            },
            account_id=1,
        )

        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        with caplog.at_level(logging.INFO, logger="telegram_archive.telegram_backup"):
            await backup._retry_pending_media_downloads()

        # One download, and no row reported as a removed duplicate: the pending
        # row was kept as a version, not deleted.
        assert "Pending media retry: 1 downloaded" in caplog.text
        assert "duplicate" not in caplog.text
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.media_id, kept.telegram_file_id, kept.downloaded) == (pending_id, str(OLD_PHOTO), 0)
        (media,) = await _rows(real_adapter, Media)
        assert media.id != pending_id
        assert (media.telegram_file_id, media.downloaded) == (str(NEW_PHOTO), 1)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"
        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date == EDITED


class TestListenerKeepsReplacedMedia:
    async def test_an_edit_event_keeps_the_old_photo_and_stores_the_new(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        listener, handlers = _listener(real_adapter, media_root)

        await handlers[events.MessageEdited](
            _event(_telegram_message(NEW_PHOTO, text="Look at this instead", edit_date=EDITED))
        )

        (media,) = await _rows(real_adapter, Media)
        assert media.telegram_file_id == str(NEW_PHOTO)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.media_id, kept.source, kept.date) == (original["id"], "listener", SENT)
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"
        (message,) = await _rows(real_adapter, Message)
        assert (message.text, message.edit_date) == ("Look at this instead", EDITED)
        # One version: the old text beside the old photo, both dated SENT.
        (version,) = await _rows(real_adapter, MessageVersion)
        assert (version.text, version.date) == ("Look at this", SENT)
        assert listener.stats["edits_applied"] == 1

    async def test_a_media_only_edit_is_an_edit(self, real_adapter, tmp_path):
        """Same caption, other photo: the edit moves edit_date and is counted."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        listener, handlers = _listener(real_adapter, media_root)

        await handlers[events.MessageEdited](_event(_telegram_message(NEW_PHOTO, edit_date=EDITED)))

        (message,) = await _rows(real_adapter, Message)
        assert (message.text, message.edit_date) == ("Look at this", EDITED)
        assert listener.stats["edits_applied"] == 1
        (version,) = await _rows(real_adapter, MessageVersion)
        assert (version.text, version.date) == ("Look at this", SENT)
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.telegram_file_id == str(OLD_PHOTO)

    async def test_an_edit_during_the_first_download_keeps_both_files(self, real_adapter, tmp_path):
        """A file sent and replaced seconds later: the edit arrives while the
        new-message handler still downloads the first file (Telethon runs the
        handlers concurrently). The edit waits for that row, then keeps it."""
        media_root = str(tmp_path / "media")
        await real_adapter.upsert_chat({"id": CHAT_ID, "type": "group", "title": "Test Group A"}, account_id=1)
        listener, handlers = _listener(real_adapter, media_root)
        listener.client.download_media, started, release = _held_download(OLD_PHOTO)

        new_message = asyncio.create_task(handlers[events.NewMessage](_event(_telegram_message(OLD_PHOTO))))
        await started.wait()
        edit = asyncio.create_task(
            handlers[events.MessageEdited](
                _event(_telegram_message(NEW_PHOTO, text="Look at this instead", edit_date=EDITED))
            )
        )
        await _settle()
        release.set()
        await asyncio.gather(new_message, edit)

        assert listener.stats["errors"] == 0
        (media,) = await _rows(real_adapter, Media)
        assert (media.telegram_file_id, media.downloaded) == (str(NEW_PHOTO), 1)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.telegram_file_id, kept.downloaded) == (str(OLD_PHOTO), 1)
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"
        (message,) = await _rows(real_adapter, Message)
        assert (message.text, message.edit_date) == ("Look at this instead", EDITED)

    async def test_two_quick_replacing_edits_keep_every_file(self, real_adapter, tmp_path):
        """B->C arrives while the listener still downloads B."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        listener, handlers = _listener(real_adapter, media_root)
        listener.client.download_media, started, release = _held_download(NEW_PHOTO)

        first = asyncio.create_task(
            handlers[events.MessageEdited](_event(_telegram_message(NEW_PHOTO, text="Second", edit_date=EDITED)))
        )
        await started.wait()
        second = asyncio.create_task(
            handlers[events.MessageEdited](_event(_telegram_message(THIRD_PHOTO, text="Third", edit_date=EDITED_AGAIN)))
        )
        await _settle()
        release.set()
        await asyncio.gather(first, second)

        (media,) = await _rows(real_adapter, Media)
        assert (media.telegram_file_id, media.downloaded) == (str(THIRD_PHOTO), 1)
        assert _read(media_root, media.file_path) == f"photo {THIRD_PHOTO}"
        kept = await _rows(real_adapter, MediaVersion)
        assert sorted((v.telegram_file_id, v.downloaded) for v in kept) == [(str(OLD_PHOTO), 1), (str(NEW_PHOTO), 1)]
        assert sorted(_read(media_root, v.file_path) for v in kept) == [f"photo {OLD_PHOTO}", f"photo {NEW_PHOTO}"]


class TestTheLiveEditFrameCarriesTheMedia:
    """2A (9.0): an edit that replaced the media carries the message's current
    media in its live frame, so an open chat swaps it in at once."""

    @staticmethod
    def _edit_frames(listener) -> list[dict]:
        from telegram_archive.realtime import NotificationType

        return [
            call.args[2] for call in listener._notifier.notify.await_args_list if call.args[0] == NotificationType.EDIT
        ]

    def _listening(self, adapter, media_root: str) -> tuple[TelegramListener, dict]:
        listener, handlers = _listener(adapter, media_root)
        listener._notifier = MagicMock()
        listener._notifier.notify = AsyncMock()
        return listener, handlers

    async def test_a_replacing_edit_carries_the_new_file(self, real_adapter, tmp_path):
        from telegram_archive.listener import _FRAME_MEDIA_KEYS

        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        listener, handlers = self._listening(real_adapter, media_root)

        await handlers[events.MessageEdited](_event(_telegram_message(NEW_PHOTO, edit_date=EDITED)))

        (frame,) = self._edit_frames(listener)
        media = frame["media"]
        assert tuple(media) == _FRAME_MEDIA_KEYS
        # The re-keyed row: the viewer turns its _v1 into the URL's ?v=1.
        assert media["id"] == f"{original['id']}_v1"
        assert media["type"] == "photo"
        assert _read(media_root, media["file_path"]) == f"photo {NEW_PHOTO}"

    async def test_without_a_live_download_the_frame_carries_the_empty_row(self, real_adapter, tmp_path):
        """LISTEN_NEW_MESSAGES_MEDIA off: the new file waits for the backup, and the
        frame carries the row as the archive holds it, with no file."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        listener, handlers = self._listening(real_adapter, media_root)
        listener.config.listen_new_messages_media = False

        await handlers[events.MessageEdited](_event(_telegram_message(NEW_PHOTO, edit_date=EDITED)))

        (frame,) = self._edit_frames(listener)
        assert frame["media"]["id"] == f"{original['id']}_v1"
        assert frame["media"]["type"] == "photo"
        assert frame["media"]["file_path"] is None

    async def test_control_a_caption_edit_carries_no_media(self, real_adapter, tmp_path):
        """The same photo with a new caption: no media key, the viewer keeps its photo."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        listener, handlers = self._listening(real_adapter, media_root)

        await handlers[events.MessageEdited](
            _event(_telegram_message(OLD_PHOTO, text="Look at this one", edit_date=EDITED))
        )

        (frame,) = self._edit_frames(listener)
        assert frame["new_text"] == "Look at this one"
        assert "media" not in frame


class TestADownloadNeverLandsInNewerMedia:
    async def test_a_drain_download_finishing_after_an_edit_fills_the_kept_version(self, real_adapter, tmp_path):
        """The drain asks for the row of photo A and starts downloading it; the
        listener then sees the edit A->B, keeps A's row as a version and
        downloads B; then A's download lands."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        pending_id = f"{CHAT_ID}_{MESSAGE_ID}_photo"
        await real_adapter.insert_media(
            {
                "id": pending_id,
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_size": 4000,
                "downloaded": False,
                "telegram_file_id": str(OLD_PHOTO),
            },
            account_id=1,
        )
        _, handlers = _listener(real_adapter, media_root)
        edit = _event(_telegram_message(NEW_PHOTO, text="Look at this instead", edit_date=EDITED))

        async def download_during_the_edit(message, path):
            if media_file_id(message.media) == str(OLD_PHOTO):
                await handlers[events.MessageEdited](edit)
            return await _fake_download(message, path)

        backup = _backup(real_adapter, media_root, [_telegram_message(OLD_PHOTO)])
        backup.client.download_media = AsyncMock(side_effect=download_during_the_edit)
        await backup._retry_pending_media_downloads()

        (media,) = await _rows(real_adapter, Media)
        assert media.id != pending_id
        assert (media.telegram_file_id, media.downloaded) == (str(NEW_PHOTO), 1)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.media_id, kept.telegram_file_id, kept.downloaded) == (pending_id, str(OLD_PHOTO), 1)
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"

        # The next read of B finds B current, not the old row.
        row = await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), edit_date=EDITED
        )
        assert row["id"] == media.id
        assert "superseded" not in row and "replaced" not in row

    async def test_a_late_write_fills_the_kept_version_never_a_second_current_row(self, real_adapter, tmp_path):
        """B->C replaced the row while B was downloading: B's write fills B's version."""
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, str(tmp_path / "media"))
        second = await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), edit_date=EDITED
        )
        third = await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(THIRD_PHOTO), edit_date=EDITED
        )
        assert second["replaced"] is True and third["replaced"] is True

        written = await real_adapter.insert_media(
            {
                "id": second["id"],
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_name": f"{NEW_PHOTO}_photo.jpg",
                "file_path": f"{CHAT_ID}/{NEW_PHOTO}_photo.jpg",
                "downloaded": True,
                "telegram_file_id": str(NEW_PHOTO),
            },
            account_id=1,
        )

        assert written is None
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.telegram_file_id, media.downloaded) == (third["id"], str(THIRD_PHOTO), 0)
        kept = await _rows(real_adapter, MediaVersion)
        assert [(v.media_id, v.telegram_file_id, v.downloaded) for v in kept] == [
            (original["id"], str(OLD_PHOTO), 1),
            (second["id"], str(NEW_PHOTO), 1),
        ]
        assert kept[1].file_path == f"{CHAT_ID}/{NEW_PHOTO}_photo.jpg"

    async def test_a_batch_download_after_the_listener_stored_other_media_is_kept(self, real_adapter, tmp_path):
        """The backup read the message with photo A and downloaded it. Before
        its batch was written, the listener stored the message with photo B
        under the same id. A's file is kept as an earlier media."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        read = await _backup(real_adapter, media_root, [])._process_media(_telegram_message(OLD_PHOTO), CHAT_ID)
        await real_adapter.insert_media(
            {
                "id": read["id"],
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_name": f"{NEW_PHOTO}.jpg",
                "file_path": f"{CHAT_ID}/{NEW_PHOTO}.jpg",
                "downloaded": True,
                "telegram_file_id": str(NEW_PHOTO),
            },
            account_id=1,
        )

        assert await real_adapter.insert_media(read, account_id=1) is None

        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.telegram_file_id) == (read["id"], str(NEW_PHOTO))
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.media_id == f"{read['id']}_v1"
        assert (kept.telegram_file_id, kept.downloaded, kept.file_path) == (str(OLD_PHOTO), 1, read["file_path"])
        assert (kept.source, kept.date) == ("backup", SENT)
        assert _read(media_root, kept.file_path) == f"photo {OLD_PHOTO}"
        # The same write again keeps nothing twice.
        assert await real_adapter.insert_media(read, account_id=1) is None
        assert len(await _rows(real_adapter, MediaVersion)) == 1

    async def test_a_fresh_row_after_a_rekeyed_id_counts_from_the_stem(self, real_adapter, tmp_path):
        """The id a kept version holds may itself be re-keyed (``_v1``): the
        next free id is ``_v2``, never ``_v1_v1``."""
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, str(tmp_path / "media"))
        for photo in (NEW_PHOTO, THIRD_PHOTO):
            row = await real_adapter.reconcile_media_row(
                CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(photo), edit_date=EDITED
            )
            assert row["replaced"] is True
        base = f"{CHAT_ID}_{MESSAGE_ID}_photo"
        (current,) = await _rows(real_adapter, Media)
        assert current.id == f"{base}_v2"
        await real_adapter.delete_media_records([current.id], account_id=1)

        written = await real_adapter.insert_media(
            {
                "id": f"{base}_v1",
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "downloaded": True,
                "file_path": f"{CHAT_ID}/{FOURTH_PHOTO}_photo.jpg",
                "telegram_file_id": str(FOURTH_PHOTO),
            },
            account_id=1,
        )

        assert written == f"{base}_v2"

    async def test_a_fresh_row_never_takes_an_id_a_kept_version_holds(self, real_adapter):
        """A row-level cleanup took the current row; the next write must not
        reuse the id the earlier media keeps, or every later replacement of
        this message would collide with it."""
        await _seed(real_adapter)
        media_id = f"{CHAT_ID}_{MESSAGE_ID}_photo"
        async with real_adapter.db_manager.async_session_factory() as session:
            session.add(
                MediaVersion(
                    account_id=1,
                    chat_id=CHAT_ID,
                    message_id=MESSAGE_ID,
                    media_id=media_id,
                    type="photo",
                    telegram_file_id=str(OLD_PHOTO),
                    downloaded=0,
                    date=SENT,
                    captured_at=SENT,
                    source="sync",
                )
            )
            await session.commit()

        written = await real_adapter.insert_media(
            {
                "id": media_id,
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "downloaded": True,
                "file_path": f"{CHAT_ID}/{NEW_PHOTO}_photo.jpg",
                "telegram_file_id": str(NEW_PHOTO),
            },
            account_id=1,
        )
        assert written == f"{media_id}_v1"

        row = await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(THIRD_PHOTO), edit_date=EDITED
        )
        assert row["replaced"] is True
        kept = await _rows(real_adapter, MediaVersion)
        assert [v.telegram_file_id for v in kept] == [str(OLD_PHOTO), str(NEW_PHOTO)]


class TestStaleReadsReplaceNothing:
    async def test_a_sync_read_older_than_the_archived_edit_replaces_nothing(self, real_adapter, tmp_path):
        """The listener applied A->B and then B->C. The sync fetched the message
        between the two edits, so it still shows B with the first edit's date."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        _, handlers = _listener(real_adapter, media_root)
        await handlers[events.MessageEdited](_event(_telegram_message(NEW_PHOTO, text="Second", edit_date=EDITED)))
        await handlers[events.MessageEdited](
            _event(_telegram_message(THIRD_PHOTO, text="Third", edit_date=EDITED_AGAIN))
        )

        stale = _telegram_message(NEW_PHOTO, text="Second", edit_date=EDITED)
        backup = _backup(real_adapter, media_root, [stale])
        await backup._sync_deletions_and_edits(CHAT_ID, object())

        backup.client.download_media.assert_not_awaited()
        (media,) = await _rows(real_adapter, Media)
        assert (media.telegram_file_id, media.downloaded) == (str(THIRD_PHOTO), 1)
        kept = await _rows(real_adapter, MediaVersion)
        assert [v.telegram_file_id for v in kept] == [str(OLD_PHOTO), str(NEW_PHOTO)]
        (message,) = await _rows(real_adapter, Message)
        assert (message.text, message.edit_date) == ("Third", EDITED_AGAIN)

    async def test_the_sweep_does_not_process_media_older_than_the_archive(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), edit_date=EDITED
        )
        await real_adapter.update_message_text(
            CHAT_ID, MESSAGE_ID, "Look at this", EDITED, account_id=1, source="sync", media_changed=True
        )
        backup = _backup(real_adapter, media_root, [])

        assert await backup._process_media(_telegram_message(OLD_PHOTO), CHAT_ID) is None
        backup.client.download_media.assert_not_awaited()
        assert len(await _rows(real_adapter, MediaVersion)) == 1


class TestAReplacementThatCannotBeKept:
    async def test_verify_never_downloads_into_a_row_it_could_not_keep(self, real_adapter, tmp_path):
        """The media row's id is already taken in media_versions, so keeping
        it fails every time. VERIFY_MEDIA must leave the row as it is."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        async with real_adapter.db_manager.async_session_factory() as session:
            session.add(
                MediaVersion(
                    account_id=1,
                    chat_id=CHAT_ID,
                    message_id=MESSAGE_ID,
                    media_id=original["id"],
                    type="photo",
                    telegram_file_id=str(THIRD_PHOTO),
                    downloaded=0,
                    date=SENT,
                    captured_at=SENT,
                    source="sync",
                )
            )
            await session.commit()
        os.remove(original["file_path"])

        backup = _backup(real_adapter, media_root, [_telegram_message(NEW_PHOTO, edit_date=EDITED)])
        await backup._verify_and_redownload_media()
        # The missing file sends the row to the pending drain, which must not
        # fill it with the new photo either.
        await backup._retry_pending_media_downloads()

        backup.client.download_media.assert_not_awaited()
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.telegram_file_id, media.file_name) == (
            original["id"],
            str(OLD_PHOTO),
            original["file_name"],
        )
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.telegram_file_id == str(THIRD_PHOTO)


class TestSweepUpsertOfAMediaOnlyEdit:
    async def test_the_sweep_reading_a_media_only_edit_moves_edit_date(self, real_adapter, tmp_path):
        """The backup's own read finds the photo replaced and the caption unchanged."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        backup = _backup(real_adapter, media_root, [])

        data = await backup._process_message(_telegram_message(NEW_PHOTO, edit_date=EDITED), CHAT_ID)
        await backup._commit_batch([data], CHAT_ID)

        (message,) = await _rows(real_adapter, Message)
        assert (message.text, message.edit_date) == ("Look at this", EDITED)
        (version,) = await _rows(real_adapter, MessageVersion)
        assert (version.text, version.date) == ("Look at this", SENT)
        (media,) = await _rows(real_adapter, Media)
        assert (media.telegram_file_id, media.downloaded) == (str(NEW_PHOTO), 1)

    async def test_control_a_reread_of_the_same_photo_moves_nothing(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        backup = _backup(real_adapter, media_root, [])

        # Telegram moved edit_date for a reaction; the photo is the same.
        data = await backup._process_message(_telegram_message(OLD_PHOTO, edit_date=EDITED), CHAT_ID)
        await backup._commit_batch([data], CHAT_ID)

        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date is None
        assert await _rows(real_adapter, MessageVersion) == []


class TestAReplacementNeedsAVisibleEdit:
    async def test_a_read_without_an_edit_date_replaces_nothing(self, real_adapter, tmp_path):
        """A read of a message nobody edited that carries another file id (the
        stored id does not name the file) leaves the archive as it is: no
        earlier media, no earlier text, no edited mark."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        backup = _backup(real_adapter, media_root, [])

        assert await backup._process_media(_telegram_message(NEW_PHOTO), CHAT_ID) is None

        backup.client.download_media.assert_not_awaited()
        assert await _rows(real_adapter, MediaVersion) == []
        assert await _rows(real_adapter, MessageVersion) == []
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.telegram_file_id) == (original["id"], str(OLD_PHOTO))
        stats = await real_adapter.get_chat_stats(CHAT_ID, account_id=1, with_kept_changes=True)
        assert stats["edited_messages"] == 0

    async def test_a_hidden_edit_replaces_nothing(self, real_adapter, tmp_path):
        """A hidden edit is a reaction: Telegram shows the message as not edited."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        listener, handlers = _listener(real_adapter, media_root)
        reaction = _telegram_message(NEW_PHOTO, edit_date=EDITED)
        reaction.edit_hide = True

        await handlers[events.MessageEdited](_event(reaction))

        listener.client.download_media.assert_not_awaited()
        assert await _rows(real_adapter, MediaVersion) == []
        assert await _rows(real_adapter, MessageVersion) == []
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.telegram_file_id) == (original["id"], str(OLD_PHOTO))


class TestLinkPreviews:
    async def _archive_preview(self, adapter, media_root: str) -> dict:
        row = await _backup(adapter, media_root, [])._process_media(_preview_message(OLD_PHOTO), CHAT_ID)
        assert (row["type"], row["telegram_file_id"]) == ("webpage", str(OLD_PHOTO))
        await adapter.insert_media(row, account_id=1)
        return row

    async def test_a_preview_picture_crawled_again_is_no_edit(self, real_adapter, tmp_path):
        """Telegram served another card picture for the same link, in a read
        that carries a visible edit date: the preview is not compared."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter, text="Read https://example.com/page")
        original = await self._archive_preview(real_adapter, media_root)
        backup = _backup(real_adapter, media_root, [])

        await backup._process_media(_preview_message(NEW_PHOTO, edit_date=EDITED), CHAT_ID)

        backup.client.download_media.assert_not_awaited()
        assert await _rows(real_adapter, MediaVersion) == []
        assert await _rows(real_adapter, MessageVersion) == []
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.file_path) == (original["id"], original["file_path"])

    async def test_control_a_photo_read_the_same_way_is_replaced(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)

        result = await _backup(real_adapter, media_root, [])._process_media(
            _telegram_message(NEW_PHOTO, edit_date=EDITED), CHAT_ID
        )

        assert result["replaced"] is True
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.telegram_file_id == str(OLD_PHOTO)

    async def test_the_drain_fetches_a_new_preview_picture_into_its_row(self, real_adapter, tmp_path):
        """A preview whose download failed, read again with another card
        picture: the new picture fills the row and nothing is kept as a version."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter, text="Read https://example.com/page")
        pending_id = f"{CHAT_ID}_{MESSAGE_ID}_webpage"
        await real_adapter.insert_media(
            {
                "id": pending_id,
                "type": "webpage",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_size": 4000,
                "downloaded": False,
                "telegram_file_id": str(OLD_PHOTO),
            },
            account_id=1,
        )

        await _backup(real_adapter, media_root, [_preview_message(NEW_PHOTO)])._retry_pending_media_downloads()

        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.telegram_file_id, media.downloaded) == (pending_id, str(NEW_PHOTO), 1)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"
        assert await _rows(real_adapter, MediaVersion) == []
        assert await _rows(real_adapter, MessageVersion) == []


class TestADeclinedReplacementIsStillAnEdit:
    async def test_a_photo_replaced_by_a_declined_preview_video_moves_edit_date(self, real_adapter, tmp_path):
        """The edit swapped the photo for a YouTube link whose video the archive
        declines (DOWNLOAD_YOUTUBE_VIDEOS off): no file to fetch, still an edit."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, media_root)
        video = SimpleNamespace(
            id=NEW_PHOTO,
            size=4000,
            mime_type="video/mp4",
            attributes=[_named("DocumentAttributeVideo", w=1280, h=720, duration=60, round_message=False)],
        )
        remote = _telegram_message(NEW_PHOTO, edit_date=EDITED)
        remote.media = _named(
            "MessageMediaWebPage",
            webpage=_named("WebPage", url="https://www.youtube.com/watch?v=abc123", photo=None, document=video),
        )
        backup = _backup(real_adapter, media_root, [])

        data = await backup._process_message(remote, CHAT_ID)
        await backup._commit_batch([data], CHAT_ID)

        backup.client.download_media.assert_not_awaited()
        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date == EDITED
        (kept,) = await _rows(real_adapter, MediaVersion)
        assert kept.telegram_file_id == str(OLD_PHOTO)
        (media,) = await _rows(real_adapter, Media)
        assert (media.telegram_file_id, media.downloaded) == (str(NEW_PHOTO), 0)


class TestLegacyFileNames:
    async def test_a_millisecond_timestamp_name_is_not_a_file_id(self, real_adapter, tmp_path):
        """The first release kept a document's own name, and a phone names a
        photo after the time in milliseconds. That number is not Telegram's
        id, so a caption edit replaces no media."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        file_name = "1704067200000.jpg"
        file_path = os.path.join(media_root, str(CHAT_ID), file_name)
        os.makedirs(os.path.dirname(file_path))
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(f"photo {OLD_PHOTO}")
        media_id = f"{CHAT_ID}_{MESSAGE_ID}_photo"
        await real_adapter.insert_media(
            {
                "id": media_id,
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_name": file_name,
                "file_path": file_path,
                "downloaded": True,
            },
            account_id=1,
        )

        remote = _telegram_message(OLD_PHOTO, text="Look at this one", edit_date=EDITED)
        backup = _backup(real_adapter, media_root, [remote])
        await backup._sync_deletions_and_edits(CHAT_ID, object())

        backup.client.download_media.assert_not_awaited()
        assert await _rows(real_adapter, MediaVersion) == []
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.file_path) == (media_id, file_path)
        (message,) = await _rows(real_adapter, Message)
        assert (message.text, message.edit_date) == ("Look at this one", EDITED)

    async def test_a_reaction_on_a_legacy_named_photo_is_no_replacement(self, real_adapter, tmp_path):
        """Names from early releases start with the message id or a date, not a
        file id. A reaction moves Telegram's edit_date; the sync must not read
        that as a replaced photo."""
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        file_name = f"{MESSAGE_ID}_20251201_101010.jpg"
        file_path = os.path.join(media_root, str(CHAT_ID), file_name)
        os.makedirs(os.path.dirname(file_path))
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(f"photo {OLD_PHOTO}")
        media_id = f"{CHAT_ID}_{MESSAGE_ID}_photo"
        await real_adapter.insert_media(
            {
                "id": media_id,
                "type": "photo",
                "message_id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "file_name": file_name,
                "file_path": file_path,
                "downloaded": True,
            },
            account_id=1,
        )

        backup = _backup(real_adapter, media_root, [_telegram_message(OLD_PHOTO, edit_date=EDITED)])
        await backup._sync_deletions_and_edits(CHAT_ID, object())

        backup.client.download_media.assert_not_awaited()
        assert await _rows(real_adapter, MediaVersion) == []
        (media,) = await _rows(real_adapter, Media)
        assert (media.id, media.file_path) == (media_id, file_path)
        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date is None


class TestReconcileMediaRow:
    async def test_only_a_known_different_id_is_a_replacement(self, real_adapter):
        await _seed(real_adapter)
        await real_adapter.insert_media(
            {"id": "m1", "type": "photo", "message_id": MESSAGE_ID, "chat_id": CHAT_ID, "file_name": "notes.jpg"},
            account_id=1,
        )
        # No id in the name and none recorded: unknown, never a guess.
        row = await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), edit_date=EDITED
        )
        assert row["id"] == "m1" and "replaced" not in row
        assert await _rows(real_adapter, MediaVersion) == []

    async def test_two_paths_that_see_the_same_edit_keep_one_version(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, str(tmp_path / "media"))

        first, second = await asyncio.gather(
            real_adapter.reconcile_media_row(
                CHAT_ID,
                MESSAGE_ID,
                "photo",
                account_id=1,
                telegram_file_id=str(NEW_PHOTO),
                source="listener",
                edit_date=EDITED,
            ),
            real_adapter.reconcile_media_row(
                CHAT_ID,
                MESSAGE_ID,
                "photo",
                account_id=1,
                telegram_file_id=str(NEW_PHOTO),
                source="sync",
                edit_date=EDITED,
            ),
        )

        assert [first.get("replaced"), second.get("replaced")].count(True) == 1
        assert first["id"] == second["id"]
        assert len(await _rows(real_adapter, MediaVersion)) == 1
        assert len(await _rows(real_adapter, Media)) == 1

    async def test_each_replacement_gets_its_own_version(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, str(tmp_path / "media"))

        for photo in (NEW_PHOTO, OLD_PHOTO, NEW_PHOTO):
            row = await real_adapter.reconcile_media_row(
                CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(photo), edit_date=EDITED
            )
            assert row["replaced"] is True

        kept = await _rows(real_adapter, MediaVersion)
        assert [v.telegram_file_id for v in kept] == [str(OLD_PHOTO), str(NEW_PHOTO), str(OLD_PHOTO)]
        assert len({v.media_id for v in kept}) == 3
        (media,) = await _rows(real_adapter, Media)
        assert media.telegram_file_id == str(NEW_PHOTO)


class TestReadsAndRemovals:
    async def _replaced(self, adapter, tmp_path) -> tuple[dict, MediaVersion]:
        await _seed(adapter)
        original = await _archive_old_photo(adapter, str(tmp_path / "media"))
        await adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), edit_date=EDITED
        )
        (kept,) = await _rows(adapter, MediaVersion)
        return original, kept

    async def test_a_version_is_served_only_inside_its_chat_and_account(self, real_adapter, tmp_path):
        original, kept = await self._replaced(real_adapter, tmp_path)

        # Numbered within the message: 1 is its first earlier media.
        row = await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID, 1, account_id=1)
        assert (row["id"], row["file_path"]) == (kept.media_id, original["file_path"])
        assert await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID, 2, account_id=1) is None
        assert await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID, 0, account_id=1) is None
        assert await real_adapter.get_media_version(OTHER_CHAT_ID, MESSAGE_ID, 1, account_id=1) is None
        assert await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID, 1, account_id=2) is None
        assert await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID + 1, 1, account_id=1) is None

    async def test_a_blob_a_kept_version_names_is_still_counted(self, real_adapter, tmp_path):
        """A cleanup frees a shared file only when nothing names its content
        hash; the kept version still does."""
        _, kept = await self._replaced(real_adapter, tmp_path)
        assert kept.content_hash

        assert await real_adapter.count_media_by_content_hash([kept.content_hash]) == {kept.content_hash: 1}

    async def test_a_media_only_edit_moves_edit_date_once(self, real_adapter, tmp_path):
        await self._replaced(real_adapter, tmp_path)

        outcome, _ = await real_adapter.update_message_text(
            CHAT_ID, MESSAGE_ID, "Look at this", EDITED, account_id=1, source="sync", media_changed=True
        )

        assert outcome == "applied"
        (message,) = await _rows(real_adapter, Message)
        assert message.edit_date == EDITED
        assert len(await _rows(real_adapter, MessageVersion)) == 1

    async def test_control_the_same_text_without_new_media_is_no_edit(self, real_adapter):
        await _seed(real_adapter)
        outcome, _ = await real_adapter.update_message_text(
            CHAT_ID, MESSAGE_ID, "Look at this", EDITED, account_id=1, source="sync"
        )
        assert outcome == "noop"

    async def test_media_without_its_text_version_is_still_listed(self, real_adapter, tmp_path):
        _, kept = await self._replaced(real_adapter, tmp_path)
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(MessageVersion.__table__.delete())
            await session.commit()

        (version,) = await real_adapter.get_message_versions(CHAT_ID, MESSAGE_ID, account_id=1)
        assert version["media_only"] is True and version["text"] is None
        assert [(m["id"], m["number"]) for m in version["media"]] == [(kept.id, 1)]

    async def test_a_hard_delete_takes_the_earlier_media_and_its_transcripts(self, real_adapter, tmp_path):
        _, kept = await self._replaced(real_adapter, tmp_path)
        await real_adapter.enqueue_media_transcript(kept.media_id, account_id=1)
        assert len(await _rows(real_adapter, MediaTranscript)) == 1

        await real_adapter.delete_message(CHAT_ID, MESSAGE_ID, account_id=1)

        assert await _rows(real_adapter, MediaVersion) == []
        assert await _rows(real_adapter, MediaTranscript) == []

    async def test_skip_media_delete_existing_lists_and_removes_earlier_media(self, real_adapter, tmp_path):
        original, kept = await self._replaced(real_adapter, tmp_path)

        records = await real_adapter.get_media_for_chat(CHAT_ID, account_id=1)
        assert [r["file_path"] for r in records if r.get("version")] == [original["file_path"]]

        await real_adapter.delete_media_for_chat(CHAT_ID, account_id=1)
        assert await _rows(real_adapter, MediaVersion) == []

    async def test_control_a_soft_delete_keeps_the_earlier_media(self, real_adapter, tmp_path):
        await self._replaced(real_adapter, tmp_path)
        await real_adapter.mark_message_deleted(CHAT_ID, MESSAGE_ID, account_id=1)
        assert len(await _rows(real_adapter, MediaVersion)) == 1


class TestStoredMediaFileId:
    @pytest.mark.parametrize(
        ("recorded", "file_name", "expected"),
        [
            ("222", "111.jpg", "222"),
            (None, "7000000000000000111.jpg", "7000000000000000111"),
            (None, "-7000000000000000111_holiday.jpg", "-7000000000000000111"),
            (None, "7000000000000000111_holiday.jpg", "7000000000000000111"),
            (None, build_media_filename("import_-1001_5", "photo.jpg", 255), None),
            (None, "5_photo.jpg", None),
            # Names from releases before the file id led the name.
            (None, "5_20251201_101010.jpg", None),
            (None, "20251201_101010_5.jpg", None),
            (None, "5_holiday.jpg", None),
            (None, "5_video.mp4", None),
            (None, "2024_report.pdf", None),
            # A sender's own name that starts with a long number: a millisecond
            # timestamp (13 digits) or a scanner's date and time (14 digits).
            (None, "1704067200000.jpg", None),
            (None, "1695901234567_scan.pdf", None),
            (None, "20240101123045_scan.pdf", None),
            (None, "700000000000011_holiday.jpg", "700000000000011"),
            (None, "holiday.jpg", None),
            (None, None, None),
        ],
    )
    def test_identity(self, recorded, file_name, expected):
        assert stored_media_file_id(recorded, file_name) == expected

    def test_media_file_id_reads_the_photo_or_the_document(self):
        assert media_file_id(_photo(123)) == "123"
        document = SimpleNamespace(document=SimpleNamespace(id=456), photo=None)
        assert media_file_id(document) == "456"
        assert media_file_id(SimpleNamespace(photo=None, document=None)) is None
        assert media_file_id(SimpleNamespace(photo=SimpleNamespace(id=None))) is None
