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
OLD_PHOTO = 7000000000000000111
NEW_PHOTO = 7000000000000000222


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
    )


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

        # Telegram now has another photo under the same caption.
        remote = _telegram_message(NEW_PHOTO, edit_date=EDITED)
        backup = _backup(real_adapter, media_root, [remote])
        await backup._sync_deletions_and_edits(CHAT_ID, object())

        (media,) = await _rows(real_adapter, Media)
        assert media.id != original["id"]
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
    async def test_a_pending_row_for_a_replaced_photo_is_kept_as_a_version(self, real_adapter, tmp_path):
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
        await backup._retry_pending_media_downloads()

        (kept,) = await _rows(real_adapter, MediaVersion)
        assert (kept.media_id, kept.telegram_file_id, kept.downloaded) == (pending_id, str(OLD_PHOTO), 0)
        (media,) = await _rows(real_adapter, Media)
        assert media.id != pending_id
        assert (media.telegram_file_id, media.downloaded) == (str(NEW_PHOTO), 1)
        assert _read(media_root, media.file_path) == f"photo {NEW_PHOTO}"


class TestListenerKeepsReplacedMedia:
    def _listener(self, adapter, media_root: str):
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
        return listener, handlers[events.MessageEdited]

    async def test_an_edit_event_keeps_the_old_photo_and_stores_the_new(self, real_adapter, tmp_path):
        media_root = str(tmp_path / "media")
        await _seed(real_adapter)
        original = await _archive_old_photo(real_adapter, media_root)
        listener, on_message_edited = self._listener(real_adapter, media_root)

        event = MagicMock()
        event.chat_id = CHAT_ID
        event.message = _telegram_message(NEW_PHOTO, text="Look at this instead", edit_date=EDITED)
        await on_message_edited(event)

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


class TestReconcileMediaRow:
    async def test_only_a_known_different_id_is_a_replacement(self, real_adapter):
        await _seed(real_adapter)
        await real_adapter.insert_media(
            {"id": "m1", "type": "photo", "message_id": MESSAGE_ID, "chat_id": CHAT_ID, "file_name": "notes.jpg"},
            account_id=1,
        )
        # No id in the name and none recorded: unknown, never a guess.
        row = await real_adapter.reconcile_media_row(
            CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO)
        )
        assert row["id"] == "m1" and "replaced" not in row
        assert await _rows(real_adapter, MediaVersion) == []

    async def test_two_paths_that_see_the_same_edit_keep_one_version(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        await _archive_old_photo(real_adapter, str(tmp_path / "media"))

        first, second = await asyncio.gather(
            real_adapter.reconcile_media_row(
                CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), source="listener"
            ),
            real_adapter.reconcile_media_row(
                CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO), source="sync"
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
                CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(photo)
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
        await adapter.reconcile_media_row(CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id=str(NEW_PHOTO))
        (kept,) = await _rows(adapter, MediaVersion)
        return original, kept

    async def test_a_version_is_served_only_inside_its_chat_and_account(self, real_adapter, tmp_path):
        original, kept = await self._replaced(real_adapter, tmp_path)

        row = await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID, kept.id, account_id=1)
        assert row["file_path"] == original["file_path"]
        assert await real_adapter.get_media_version(OTHER_CHAT_ID, MESSAGE_ID, kept.id, account_id=1) is None
        assert await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID, kept.id, account_id=2) is None
        assert await real_adapter.get_media_version(CHAT_ID, MESSAGE_ID + 1, kept.id, account_id=1) is None

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
        assert [m["id"] for m in version["media"]] == [kept.id]

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
            (None, "111.jpg", "111"),
            (None, "-111_holiday.jpg", "-111"),
            (None, "111_holiday.jpg", "111"),
            (None, build_media_filename("import_-1001_5", "photo.jpg", 255), None),
            (None, "5_photo.jpg", None),
            (None, "holiday.jpg", None),
            (None, None, None),
        ],
    )
    def test_identity(self, recorded, file_name, expected):
        assert stored_media_file_id(recorded, file_name, 5, "photo") == expected

    def test_media_file_id_reads_the_photo_or_the_document(self):
        assert media_file_id(_photo(123)) == "123"
        document = SimpleNamespace(document=SimpleNamespace(id=456), photo=None)
        assert media_file_id(document) == "456"
        assert media_file_id(SimpleNamespace(photo=None, document=None)) is None
        assert media_file_id(SimpleNamespace(photo=SimpleNamespace(id=None))) is None
