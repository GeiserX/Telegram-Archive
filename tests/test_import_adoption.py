"""HTML-import media adoption + topic backfill (9t6.14).

A Telegram Desktop import stores media rows as ``import_{chat}_{msg}`` with
the files already on disk. When the API sweep later reaches the same message
it built its own id, re-downloaded the bytes, and left a duplicate row and a
duplicate file. Adoption re-keys the import row to the sweep's id the moment
the sweep meets it, so the upsert converges on the existing record and file.

``backfill-topics`` is the companion CLI: imports carry no forum-topic
metadata, so the command zeroes the chat's sync cursor and resweeps it
text-only (downloads off, deletion sync off) to refresh reply_to_top_id.
"""

import asyncio
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telethon.tl.types import MessageMediaPhoto

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.__main__ import run_backfill_topics
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, SyncStatus
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001
OTHER_CHAT_ID = -1002
MSG_ID = 77
IMPORT_ID = f"import_{CHAT_ID}_{MSG_ID}"
SWEEP_ID = f"{CHAT_ID}_{MSG_ID}_photo"


def _import_row(account_id: int = 1, downloaded: int = 1, media_id: str = IMPORT_ID) -> Media:
    return Media(
        account_id=account_id,
        id=media_id,
        message_id=MSG_ID,
        chat_id=CHAT_ID,
        type="photo",
        file_name="photo_1.jpg",
        file_path=f"/data/media/{CHAT_ID}/photo_1.jpg",
        file_size=123,
        mime_type="image/jpeg",
        width=10,
        height=20,
        duration=None,
        content_hash="ab" * 32,
        downloaded=downloaded,
        download_date=datetime(2026, 8, 1, 12, 0, 0),
    )


async def _make_adapter() -> DatabaseAdapter:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with db_manager.async_session_factory() as session:
        session.add(Chat(account_id=1, id=CHAT_ID, type="channel", title="A", last_synced_message_id=500))
        session.add(Chat(account_id=2, id=CHAT_ID, type="channel", title="A", last_synced_message_id=700))
        session.add(Chat(account_id=1, id=OTHER_CHAT_ID, type="channel", title="B", last_synced_message_id=900))
        # The cursor the sweep ACTUALLY reads (get_last_message_id -> min_id)
        # lives in sync_status; account 2 deliberately has no row.
        session.add(SyncStatus(account_id=1, chat_id=CHAT_ID, last_message_id=500))
        session.add(SyncStatus(account_id=1, chat_id=OTHER_CHAT_ID, last_message_id=900))
        await session.commit()

    return DatabaseAdapter(db_manager)


@pytest_asyncio.fixture
async def adapter():
    return await _make_adapter()


async def _media_ids(adapter_, account_id: int = 1) -> set[str]:
    async with adapter_.db_manager.async_session_factory() as session:
        rows = await session.execute(select(Media.id).where(Media.account_id == account_id))
        return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Adapter: reconcile_media_row
#
# This replaces adopt_import_media. Adoption re-KEYED an import row to the
# sweep's spelling; reconciliation instead treats the id as an opaque token the
# row keeps forever and corrects only the type, which is the value every reader
# consults. Same purpose -- meet a message that already has its media and reuse
# it instead of downloading again -- with the id no longer caching a judgement
# that can change.
# ---------------------------------------------------------------------------


async def test_reconciles_the_row_the_message_already_has(adapter):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_import_row())
        await session.commit()

    record = await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "photo", account_id=1)

    assert record is not None
    assert record["downloaded"] is True
    assert record["file_path"] == f"/data/media/{CHAT_ID}/photo_1.jpg"
    assert record["file_name"] == "photo_1.jpg"
    assert record["type"] == "photo"
    assert record["content_hash"] == "ab" * 32
    # The id is KEPT, not re-keyed: it is an opaque token, and nothing reads it.
    assert record["id"] == IMPORT_ID
    assert await _media_ids(adapter) == {IMPORT_ID}


async def test_a_changed_judgement_corrects_the_type_in_place(adapter):
    """The round-video case: the classifier now says video_note about a row
    filed as video. One row, re-typed -- not a second row."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_import_row(media_id=SWEEP_ID))
        await session.commit()

    record = await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "video_note", account_id=1)

    assert record["type"] == "video_note"
    assert record["id"] == SWEEP_ID
    assert await _media_ids(adapter) == {SWEEP_ID}
    # and it is persisted, not just returned
    again = await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "video_note", account_id=1)
    assert again["type"] == "video_note"


async def test_a_duplicate_pair_resolves_to_the_row_readers_see(adapter):
    """An archive can hold two rows for one message. The writer must pick the
    same one get_media_for_message does, or they describe different files."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_import_row(media_id=SWEEP_ID, downloaded=0))
        session.add(_import_row(downloaded=1))
        await session.commit()

    record = await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "photo", account_id=1)

    assert record["id"] == IMPORT_ID  # downloaded wins over the pending twin
    assert await _media_ids(adapter) == {IMPORT_ID, SWEEP_ID}


async def test_an_undownloaded_row_is_still_returned(adapter):
    """Reconciliation reports the row; whether its BYTES can be reused is the
    caller's call, and _process_media makes it by looking at the disk."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_import_row(downloaded=0))
        await session.commit()

    record = await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "photo", account_id=1)

    assert record is not None
    assert record["downloaded"] is False


async def test_no_row_returns_none(adapter):
    assert await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "photo", account_id=1) is None


async def test_reconciliation_is_account_scoped(adapter):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_import_row(account_id=1))
        await session.commit()

    assert await adapter.reconcile_media_row(CHAT_ID, MSG_ID, "photo", account_id=2) is None
    assert await _media_ids(adapter, account_id=1) == {IMPORT_ID}


# ---------------------------------------------------------------------------
# Adapter: reset_chat_sync_cursor
# ---------------------------------------------------------------------------


async def test_reset_cursor_zeroes_every_account_for_that_chat_only(adapter):
    assert await adapter.reset_chat_sync_cursor(CHAT_ID) == 2

    async with adapter.db_manager.async_session_factory() as session:
        rows = await session.execute(select(Chat.account_id, Chat.id, Chat.last_synced_message_id))
        cursors = {(acc, chat): cursor for acc, chat, cursor in rows}
    assert cursors[(1, CHAT_ID)] == 0
    assert cursors[(2, CHAT_ID)] == 0
    assert cursors[(1, OTHER_CHAT_ID)] == 900  # untouched


async def test_reset_cursor_zeroes_the_cursor_the_sweep_reads(adapter):
    """_backup_dialog takes min_id from sync_status.last_message_id, NOT from
    chats.last_synced_message_id — zeroing only the chat column makes the
    resweep resume where it left off and backfill-topics silently no-op.
    """
    assert await adapter.reset_chat_sync_cursor(CHAT_ID) == 2
    assert await adapter.get_last_message_id(CHAT_ID, account_id=1) == 0
    # The untargeted chat's sweep cursor stays put.
    assert await adapter.get_last_message_id(OTHER_CHAT_ID, account_id=1) == 900


async def test_reset_cursor_unknown_chat_returns_zero(adapter):
    assert await adapter.reset_chat_sync_cursor(-999999) == 0


# ---------------------------------------------------------------------------
# Sweep hook in _process_media
# ---------------------------------------------------------------------------


class TestSweepAdoptionHook(unittest.TestCase):
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
                handle.write(b"mediabytes")
            return path

        backup._download_media_to_path = AsyncMock(side_effect=fake_download)
        return backup

    def _photo_message(self):
        message = MagicMock()
        message.id = MSG_ID
        media = object.__new__(MessageMediaPhoto)
        media.photo = SimpleNamespace(id=42, sizes=[SimpleNamespace(type="m", size=1000)])
        message.media = media
        return message

    def test_an_existing_file_on_disk_short_circuits_the_download(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        os.makedirs(os.path.join(media_root, str(CHAT_ID)))
        rel = f"{CHAT_ID}/photo_1.jpg"
        with open(os.path.join(media_root, rel), "wb") as fh:
            fh.write(b"already here")
        backup = self._make_backup(media_root)
        existing = {"id": IMPORT_ID, "type": "photo", "downloaded": True, "file_path": rel}
        backup.db.reconcile_media_row = AsyncMock(return_value=existing)

        result = self._run(backup._process_media(self._photo_message(), CHAT_ID))

        self.assertIs(result, existing)
        backup.db.reconcile_media_row.assert_awaited_once_with(CHAT_ID, MSG_ID, "photo", account_id=1)
        backup._download_media_to_path.assert_not_awaited()

    def test_a_row_whose_file_is_gone_is_downloaded_again(self):
        """THE control, and a real bug it closes: reuse used to be decided by the
        row's downloaded flag alone, with no look at the disk. A verify pass
        therefore counted a missing or corrupted import as re-downloaded and
        removed the file it had just sidestepped, destroying the only copy."""
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = self._make_backup(media_root)
        backup.db.reconcile_media_row = AsyncMock(
            return_value={"id": IMPORT_ID, "type": "photo", "downloaded": True, "file_path": f"{CHAT_ID}/gone.jpg"}
        )

        self._run(backup._process_media(self._photo_message(), CHAT_ID))

        backup._download_media_to_path.assert_awaited()

    def test_the_existing_row_keeps_its_id_so_the_upsert_lands_on_it(self):
        """The id is an opaque token. Minting a fresh one from the type is what
        turned a reclassified round video into a second row while the first
        stayed pending forever."""
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = self._make_backup(media_root)
        backup.db.reconcile_media_row = AsyncMock(
            return_value={"id": IMPORT_ID, "type": "photo", "downloaded": False, "file_path": None}
        )

        result = self._run(backup._process_media(self._photo_message(), CHAT_ID))

        self.assertEqual(IMPORT_ID, result["id"])

    def test_without_an_existing_row_the_sweep_downloads_normally(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        backup = self._make_backup(media_root)
        backup.db.reconcile_media_row = AsyncMock(return_value=None)

        result = self._run(backup._process_media(self._photo_message(), CHAT_ID))

        self.assertEqual(result["id"], SWEEP_ID)
        self.assertTrue(result["downloaded"])
        backup._download_media_to_path.assert_awaited()


# ---------------------------------------------------------------------------
# backfill-topics CLI
# ---------------------------------------------------------------------------


class TestBackfillTopicsCommand(unittest.TestCase):
    def _adapter_returning(self, rowcount: int):
        adapter_mock = AsyncMock()
        adapter_mock.reset_chat_sync_cursor = AsyncMock(return_value=rowcount)
        adapter_mock.close = AsyncMock()

        async def fake_create_adapter(database_url=None):
            return adapter_mock

        return adapter_mock, fake_create_adapter

    def test_unknown_chat_exits_1_without_sweeping(self):
        adapter_mock, fake_create = self._adapter_returning(0)
        backup_main = MagicMock(return_value=0)
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("src.db.create_adapter", new=fake_create),
            mock.patch("src.telegram_backup.main", new=backup_main),
        ):
            result = run_backfill_topics(SimpleNamespace(chat_id=CHAT_ID))

        self.assertEqual(result, 1)
        backup_main.assert_not_called()
        adapter_mock.close.assert_awaited()

    def test_reset_failure_exits_1_without_a_traceback(self):
        """Sibling commands print 'X failed: e' and return 1 — so does this."""

        async def broken_create_adapter(database_url=None):
            raise RuntimeError("db is sideways")

        backup_main = MagicMock(return_value=0)
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("src.db.create_adapter", new=broken_create_adapter),
            mock.patch("src.telegram_backup.main", new=backup_main),
            contextlib.redirect_stderr(stderr),
        ):
            result = run_backfill_topics(SimpleNamespace(chat_id=CHAT_ID))

        self.assertEqual(result, 1)
        backup_main.assert_not_called()
        self.assertIn("Topic backfill failed: db is sideways", stderr.getvalue())

    def test_known_chat_resweeps_with_pinned_env(self):
        adapter_mock, fake_create = self._adapter_returning(2)
        backup_main = MagicMock(return_value=0)
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("src.db.create_adapter", new=fake_create),
            mock.patch("src.telegram_backup.main", new=backup_main),
        ):
            result = run_backfill_topics(SimpleNamespace(chat_id=CHAT_ID))
            pinned = {
                key: os.environ.get(key)
                for key in ("DOWNLOAD_MEDIA", "SYNC_DELETIONS_EDITS", "VERIFY_MEDIA", "CHAT_IDS")
            }

        self.assertEqual(result, 0)
        backup_main.assert_called_once()
        adapter_mock.reset_chat_sync_cursor.assert_awaited_once_with(CHAT_ID)
        self.assertEqual(
            pinned,
            {
                "DOWNLOAD_MEDIA": "false",
                "SYNC_DELETIONS_EDITS": "false",
                "VERIFY_MEDIA": "false",
                "CHAT_IDS": str(CHAT_ID),
            },
        )
