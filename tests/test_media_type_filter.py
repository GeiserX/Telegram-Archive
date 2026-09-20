"""DOWNLOAD_MEDIA_TYPES / DOWNLOAD_DOCUMENT_MIME_TYPES: the download filter.

Pins the contract of the media-type filter across all three lanes:

- ``_process_media`` records filtered media as a metadata row (name, MIME,
  dimensions, size) with NO ``downloaded`` key — the same shape the oversize
  skip uses, so a tightened filter never un-downloads a stored file.
- ``_retry_pending_media_downloads`` drops filtered rows BEFORE the per-chat
  re-fetch: a MIME filter over a large archive must not re-request its
  filtered-out messages from Telegram on every run.
- ``Config`` parses, validates and derives filename extensions from the
  configured MIME types (application/pdf -> .pdf) so mislabeled documents
  (application/octet-stream carrying a .pdf name) still pass.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001
OTHER_CHAT_ID = -2002


# ---------------------------------------------------------------------------
# Telethon-shaped fixtures
# ---------------------------------------------------------------------------


def _document_media(mime_type, file_name=None, size=20000, doc_id=555000111):
    """A MessageMediaDocument shaped like a generic Telegram file."""
    from telethon.tl.types import DocumentAttributeFilename, MessageMediaDocument

    attributes = [DocumentAttributeFilename(file_name=file_name)] if file_name else []
    media = MagicMock(spec=MessageMediaDocument)
    media.document = SimpleNamespace(
        id=doc_id,
        size=size,
        mime_type=mime_type,
        attributes=attributes,
    )
    return media


def _video_media(size=90000, duration=12):
    """A MessageMediaDocument shaped like a Telegram video."""
    from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

    media = MagicMock(spec=MessageMediaDocument)
    media.document = SimpleNamespace(
        id=123456789,
        size=size,
        mime_type="video/mp4",
        attributes=[DocumentAttributeVideo(duration=duration, w=640, h=480)],
    )
    return media


def _photo_media(size=8000):
    from telethon.tl.types import MessageMediaPhoto, PhotoSize

    media = MagicMock(spec=MessageMediaPhoto)
    media.photo = SimpleNamespace(id=99, sizes=[PhotoSize(type="m", w=320, h=240, size=size)])
    return media


def _message(msg_id, media):
    msg = MagicMock()
    msg.id = msg_id
    msg.media = media
    msg.date = datetime(2026, 1, 1, 10)
    msg.reply_to = None
    msg.action = None
    return msg


def _build_config(media_root, **env_overrides):
    """A REAL Config (the predicates under test live on it), paths pointed at tmp."""
    env = {"BACKUP_PATH": media_root, "DATABASE_DIR": media_root}
    env.update(env_overrides)
    with patch.dict(os.environ, env):
        config = Config()
    config.media_path = media_root
    config.deduplicate_media = False
    return config


def _make_backup(media_root, **env_overrides):
    """TelegramBackup with a real Config, wired for a real, dedup-free download."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = _build_config(media_root, **env_overrides)
    backup.db = AsyncMock()
    backup.db.reconcile_media_row = AsyncMock(return_value=None)
    backup.client = AsyncMock()
    backup._owns_client = True
    backup._cleaned_media_chats = set()

    async def fake_download(_message, path):
        with open(path, "wb") as handle:
            handle.write(b"mediabytes")
        return path

    backup.client.download_media = AsyncMock(side_effect=fake_download)
    return backup


PDF_FILTER_ENV = {
    "DOWNLOAD_MEDIA_TYPES": "document",
    "DOWNLOAD_DOCUMENT_MIME_TYPES": "application/pdf",
}


# ---------------------------------------------------------------------------
# Database fixture (real in-memory SQLite, so the upsert SQL actually runs)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def adapter():
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
        session.add(Chat(id=CHAT_ID, type="channel", title="Chat A"))
        session.add(Chat(id=OTHER_CHAT_ID, type="channel", title="Chat B"))
        session.add(Message(id=7, chat_id=CHAT_ID, sender_id=None, date=datetime(2026, 1, 1, 10), text=""))
        session.add(Message(id=8, chat_id=OTHER_CHAT_ID, sender_id=None, date=datetime(2026, 6, 1, 10), text=""))
        await session.commit()

    return DatabaseAdapter(db_manager)


async def _media_row(adapter_, media_id):
    async with adapter_.db_manager.async_session_factory() as session:
        result = await session.execute(select(Media).where(Media.id == media_id))
        return result.scalar_one()


# ---------------------------------------------------------------------------
# _process_media: the filtered-row shape
# ---------------------------------------------------------------------------


class TestFilteredMediaRows:
    """Filtered media is recorded with metadata and without a download outcome."""

    def setup_method(self):
        self.media_root = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.media_root, ignore_errors=True)

    async def test_filtered_video_row_records_metadata_without_downloaded_key(self, adapter):
        backup = _make_backup(self.media_root, DOWNLOAD_MEDIA_TYPES="document")
        row = await backup._process_media(_message(7, _video_media()), CHAT_ID)

        assert "downloaded" not in row
        assert row["type"] == "video"
        assert row["mime_type"] == "video/mp4"
        assert row["file_size"] == 90000
        assert row["duration"] == 12
        assert isinstance(row["file_name"], str) and row["file_name"]

    async def test_filtered_document_row_keeps_its_original_name(self, adapter):
        backup = _make_backup(self.media_root, **PDF_FILTER_ENV)
        row = await backup._process_media(_message(7, _document_media("video/mp4", "clip.mp4")), CHAT_ID)

        assert "downloaded" not in row
        assert row["mime_type"] == "video/mp4"
        assert "clip" in row["file_name"]

    async def test_allowed_pdf_document_is_downloaded(self, adapter):
        backup = _make_backup(self.media_root, **PDF_FILTER_ENV)
        row = await backup._process_media(_message(7, _document_media("application/pdf", "report.pdf")), CHAT_ID)

        assert row["downloaded"] is True
        assert row["file_path"] == os.path.join(self.media_root, str(CHAT_ID), row["file_name"])
        await adapter.insert_media(row, account_id=1)
        stored = await _media_row(adapter, row["id"])
        assert stored.downloaded == 1

    async def test_extension_fallback_passes_mislabeled_pdf(self, adapter):
        backup = _make_backup(self.media_root, **PDF_FILTER_ENV)
        row = await backup._process_media(
            _message(7, _document_media("application/octet-stream", "report.PDF")), CHAT_ID
        )

        assert row["downloaded"] is True

    async def test_wrong_extension_stays_metadata_only(self, adapter):
        backup = _make_backup(self.media_root, **PDF_FILTER_ENV)
        row = await backup._process_media(
            _message(7, _document_media("application/octet-stream", "archive.zip")), CHAT_ID
        )

        assert "downloaded" not in row
        assert "zip" in row["file_name"]

    async def test_type_whitelist_passes_the_allowed_type_through(self, adapter):
        backup = _make_backup(self.media_root, DOWNLOAD_MEDIA_TYPES="photo")
        row = await backup._process_media(_message(7, _photo_media()), CHAT_ID)

        assert row["downloaded"] is True

    async def test_tightening_the_filter_never_undownloads_a_stored_file(self, adapter):
        """Same COALESCE contract as the oversize skip: the stored flag survives."""
        media_id = f"{CHAT_ID}_7_document"

        permissive = _make_backup(self.media_root)
        downloaded_row = await permissive._process_media(
            _message(7, _document_media("application/pdf", "report.pdf")), CHAT_ID
        )
        await adapter.insert_media(downloaded_row, account_id=1)

        strict = _make_backup(self.media_root, **PDF_FILTER_ENV)
        strict.config.download_media_types = {"photo"}
        filtered_row = await strict._process_media(
            _message(7, _document_media("application/pdf", "report.pdf")), CHAT_ID
        )
        assert "downloaded" not in filtered_row
        await adapter.insert_media(filtered_row, account_id=1)

        stored = await _media_row(adapter, media_id)
        assert stored.downloaded == 1


# ---------------------------------------------------------------------------
# The pending drain: filtered rows are dropped before the re-fetch
# ---------------------------------------------------------------------------


class TestPendingDrainSkipsFilteredRows:
    """A MIME filter must not re-request its filtered-out messages every run."""

    def setup_method(self):
        self.media_root = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.media_root, ignore_errors=True)

    async def test_drain_excludes_filtered_rows_before_the_refetch(self, adapter):
        # Chat A holds a row the filter excluded (metadata present, downloaded=0);
        # chat B holds a metadata-less row for an ALLOWED document — a genuine
        # failed download that must keep its retry.
        seeder = _make_backup(self.media_root, **PDF_FILTER_ENV)
        filtered_row = await seeder._process_media(_message(7, _document_media("video/mp4", "clip.mp4")), CHAT_ID)
        await adapter.insert_media(filtered_row, account_id=1)
        await adapter.insert_media(
            {
                "id": f"{OTHER_CHAT_ID}_8_document",
                "message_id": 8,
                "chat_id": OTHER_CHAT_ID,
                "type": "document",
                "file_size": 20000,
                "downloaded": False,
            },
            account_id=1,
        )

        drainer = _make_backup(self.media_root, **PDF_FILTER_ENV)
        drainer.db = adapter
        pdf_message = _message(8, _document_media("application/pdf", "invoice.pdf"))
        drainer.client.get_messages = AsyncMock(return_value=[pdf_message])

        await drainer._retry_pending_media_downloads()

        # The filtered row's chat was never re-fetched; the allowed one was.
        fetched_chats = [call.args[0] for call in drainer.client.get_messages.await_args_list]
        assert fetched_chats == [OTHER_CHAT_ID]

        # Filtered: untouched (pending, but never charged an attempt).
        stored_filtered = await _media_row(adapter, filtered_row["id"])
        assert stored_filtered.downloaded == 0
        assert stored_filtered.download_attempts == 0

        # Allowed: the retry went through end to end.
        stored_allowed = await _media_row(adapter, f"{OTHER_CHAT_ID}_8_document")
        assert stored_allowed.downloaded == 1


# ---------------------------------------------------------------------------
# Config parsing and predicates
# ---------------------------------------------------------------------------


class TestConfigMediaFilters(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.env_patcher = patch.dict(
            os.environ, {"BACKUP_PATH": self.temp_dir, "DATABASE_DIR": self.temp_dir}, clear=True
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unset_filters_are_permissive(self):
        config = Config()

        self.assertEqual(config.download_media_types, set())
        self.assertEqual(config.download_document_mime_types, set())
        self.assertEqual(config.download_document_mime_extensions, set())
        self.assertTrue(config.should_download_media_type("voice"))
        self.assertTrue(config.should_download_media_type(None))
        self.assertTrue(config.document_mime_allowed("video/mp4", "clip.mp4"))
        self.assertTrue(config.document_mime_allowed(None, None))

    def test_lists_parse_lowercased_with_derived_extensions(self):
        with patch.dict(
            os.environ,
            {
                "DOWNLOAD_MEDIA_TYPES": " Document , PHOTO ",
                "DOWNLOAD_DOCUMENT_MIME_TYPES": "Application/PDF",
            },
        ):
            config = Config()

        self.assertEqual(config.download_media_types, {"document", "photo"})
        self.assertEqual(config.download_document_mime_types, {"application/pdf"})
        self.assertEqual(config.download_document_mime_extensions, {".pdf"})

    def test_invalid_media_type_raises_value_error(self):
        with patch.dict(os.environ, {"DOWNLOAD_MEDIA_TYPES": "hologram"}), self.assertRaises(ValueError) as ctx:
            Config()

        self.assertIn("Invalid media types", str(ctx.exception))
        self.assertIn("document", str(ctx.exception))

    def test_predicates_answer_the_combined_configuration(self):
        with patch.dict(os.environ, {**PDF_FILTER_ENV, "DOWNLOAD_DOCUMENT_MIME_TYPES": "application/pdf"}):
            config = Config()

        self.assertFalse(config.should_download_media_type("sticker"))
        self.assertTrue(config.should_download_media_type("document"))
        self.assertTrue(config.document_mime_allowed("application/pdf", None))
        self.assertTrue(config.document_mime_allowed("APPLICATION/PDF", None))
        self.assertTrue(config.document_mime_allowed("application/octet-stream", "scan.PDF"))
        self.assertFalse(config.document_mime_allowed("application/octet-stream", "archive.zip"))
        self.assertFalse(config.document_mime_allowed(None, None))
