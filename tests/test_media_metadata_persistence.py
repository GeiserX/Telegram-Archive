"""#263 follow-ups: the media metadata written by live capture must SURVIVE.

Three defects, one theme — a value that reached the ``media`` table got lost again:

- The scheduled sweep re-extracted mime_type/width/height/duration with its own
  (weaker) code and upserted the row, blanking what the listener had stored. Both
  paths now share ``extract_media_attributes``, and the upsert COALESCEs the
  metadata columns so an ingest path with nothing to say cannot null them.
- ``duration`` arrives from Telethon as a float for video, but the column is
  INTEGER.
- ``PhotoSizeProgressive`` scored 0 in the "largest size" pick, so dimensions were
  read off a thumbnail.

Plus the cross-chat cursor oracle in ``get_media_paginated``.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message
from src.message_utils import extract_media_attributes, media_display_filename, utcnow_naive
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001
OTHER_CHAT_ID = -2002


# ---------------------------------------------------------------------------
# Telethon-shaped fixtures
# ---------------------------------------------------------------------------


def _video_media(width=640, height=480, duration=12, size=90000):
    """A MessageMediaDocument shaped like a Telegram video."""
    from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

    media = MagicMock(spec=MessageMediaDocument)
    media.document = SimpleNamespace(
        id=123456789,
        size=size,
        mime_type="video/mp4",
        attributes=[DocumentAttributeVideo(duration=duration, w=width, h=height)],
    )
    return media


def _photo_media(*sizes):
    """A MessageMediaPhoto whose ``photo.sizes`` are the given size objects."""
    from telethon.tl.types import MessageMediaPhoto

    media = MagicMock(spec=MessageMediaPhoto)
    media.photo = SimpleNamespace(id=99, sizes=list(sizes))
    return media


def _plain_size(width=320, height=240, size=8000):
    from telethon.tl.types import PhotoSize

    return PhotoSize(type="m", w=width, h=height, size=size)


def _message(msg_id, media):
    msg = MagicMock()
    msg.id = msg_id
    msg.media = media
    msg.date = datetime(2026, 1, 1, 10)
    msg.reply_to = None
    msg.action = None
    return msg


def _make_backup(media_root):
    """TelegramBackup wired for a real, dedup-free download into ``media_root``."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = MagicMock()
    backup.config.media_path = media_root
    backup.config.deduplicate_media = False
    backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
    backup.db = AsyncMock()
    # No import rows by default: a truthy mock would make the adoption
    # hook short-circuit _process_media before the paths under test.
    backup.db.adopt_import_media = AsyncMock(return_value=None)
    backup.client = AsyncMock()
    backup._owns_client = True
    backup._cleaned_media_chats = set()

    async def fake_download(_message, path):
        with open(path, "wb") as handle:
            handle.write(b"mediabytes")
        return path

    backup.client.download_media = AsyncMock(side_effect=fake_download)
    # config is a MagicMock, so an unset predicate returns a truthy mock. Pin the
    # skip predicates to False: a future test routing through _process_message or
    # _backup_dialog with this fixture would otherwise silently skip every message
    # and pass vacuously.
    backup.config.should_skip_topic.return_value = False
    return backup


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
        # The other chat's media is NEWER, so an unscoped cursor would have left
        # this chat's whole (older) page visible instead of an empty one.
        session.add(Message(id=8, chat_id=OTHER_CHAT_ID, sender_id=None, date=datetime(2026, 6, 1, 10), text=""))
        await session.commit()

    return DatabaseAdapter(db_manager)


async def _media_row(adapter_, media_id):
    async with adapter_.db_manager.async_session_factory() as session:
        result = await session.execute(select(Media).where(Media.id == media_id))
        return result.scalar_one()


# ---------------------------------------------------------------------------
# extract_media_attributes
# ---------------------------------------------------------------------------


class TestExtractMediaAttributes(unittest.TestCase):
    """Pure-extraction behaviour shared by the sweep and the listener."""

    def test_float_video_duration_is_coerced_to_int(self):
        """The ``duration`` column is INTEGER; Telethon reports video seconds as float."""
        attributes = extract_media_attributes(_video_media(duration=12.4))
        self.assertEqual(attributes["duration"], 12)
        self.assertIsInstance(attributes["duration"], int)
        self.assertNotIsInstance(attributes["duration"], float)

    def test_float_duration_rounds_like_the_integer_column_cast(self):
        self.assertEqual(extract_media_attributes(_video_media(duration=12.6))["duration"], 13)

    def test_int_duration_is_left_alone(self):
        attributes = extract_media_attributes(_video_media(duration=12))
        self.assertEqual(attributes["duration"], 12)
        self.assertIsInstance(attributes["duration"], int)

    def test_progressive_photo_size_wins_over_smaller_plain_sizes(self):
        """PhotoSizeProgressive has no ``size`` — scoring it 0 picked a thumbnail."""
        from telethon.tl.types import PhotoSize, PhotoSizeProgressive

        thumbnail = PhotoSize(type="m", w=320, h=240, size=8000)
        full = PhotoSizeProgressive(type="y", w=1280, h=960, sizes=[1000, 20000, 90000])

        attributes = extract_media_attributes(_photo_media(thumbnail, full))

        self.assertEqual((attributes["width"], attributes["height"]), (1280, 960))
        self.assertEqual(attributes["file_size"], 90000)

    def test_plain_photo_size_still_wins_when_it_is_the_largest(self):
        from telethon.tl.types import PhotoSize, PhotoSizeProgressive

        small_progressive = PhotoSizeProgressive(type="x", w=200, h=150, sizes=[100, 900])
        big_plain = PhotoSize(type="y", w=1600, h=1200, size=500000)

        attributes = extract_media_attributes(_photo_media(small_progressive, big_plain))

        self.assertEqual((attributes["width"], attributes["height"]), (1600, 1200))
        self.assertEqual(attributes["file_size"], 500000)

    def test_document_mime_type_comes_from_the_document(self):
        """The MessageMediaDocument wrapper has no mime_type; the document does."""
        self.assertEqual(extract_media_attributes(_video_media())["mime_type"], "video/mp4")

    def test_photo_without_sizes_yields_all_none(self):
        attributes = extract_media_attributes(_photo_media())
        self.assertEqual(
            attributes,
            {"file_size": None, "mime_type": None, "width": None, "height": None, "duration": None},
        )

    def test_returned_key_set_is_the_pinned_contract(self):
        """Both callers spread this dict into a media row, so the key set is a contract.

        Adding/renaming a key silently changes what the sweep and the listener
        write (and which one of them then overrides ``file_size``). Pin it for
        every media shape, not just the empty one.
        """
        expected = {"file_size", "mime_type", "width", "height", "duration"}
        for media in (_video_media(), _photo_media(), _photo_media(_plain_size())):
            self.assertEqual(set(extract_media_attributes(media)), expected)

    def test_file_size_is_telegrams_declared_size_not_a_disk_size(self):
        """The collision both callers resolve: this value is overridden on-disk."""
        self.assertEqual(extract_media_attributes(_video_media(size=90000))["file_size"], 90000)


class TestMediaDisplayFilename(unittest.TestCase):
    """Server-side twin of the viewer's getMediaDisplayName."""

    def test_strips_the_file_id_prefix(self):
        self.assertEqual(media_display_filename("123456789_holiday.jpg"), "holiday.jpg")

    def test_strips_the_import_media_id_prefix(self):
        self.assertEqual(media_display_filename("import_-1001_5_report.pdf"), "report.pdf")

    def test_strips_at_most_one_prefix(self):
        """Only the FIRST component can be lost — the second digit run survives."""
        self.assertEqual(media_display_filename("77_2026_report.pdf"), "2026_report.pdf")

    def test_a_digit_first_component_is_stripped_like_the_viewer(self):
        """Documented wart: an original name starting with digits loses them.

        A storage prefix and a genuine leading digit run are indistinguishable, and
        the viewer's getMediaDisplayName strips ``^[0-9]+_`` unconditionally — the
        download name must match the label the gallery shows, so it strips too.
        """
        self.assertEqual(media_display_filename("2026_report.pdf"), "report.pdf")

    def test_non_ascii_digits_are_not_treated_as_a_prefix(self):
        """``[0-9]``, not ``\\d`` — the viewer's regex never matches these."""
        self.assertEqual(media_display_filename("١٢٣_report.pdf"), "١٢٣_report.pdf")

    def test_unprefixed_name_is_unchanged(self):
        self.assertEqual(media_display_filename("clip.mp4"), "clip.mp4")

    def test_never_returns_an_empty_name(self):
        self.assertEqual(media_display_filename("12345_"), "12345_")


# ---------------------------------------------------------------------------
# The sweep must not undo the listener's capture
# ---------------------------------------------------------------------------


class TestSweepPreservesLiveCapturedMetadata:
    """A scheduled sweep re-upserting a live-captured message keeps its metadata."""

    def setup_method(self):
        self.media_root = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _listener_row(self, media_id, media):
        """The row the listener writes (listener.py builds it from the same helper)."""
        return {
            "id": media_id,
            "message_id": 7,
            "chat_id": CHAT_ID,
            "type": "video",
            "file_path": os.path.join(self.media_root, str(CHAT_ID), "clip.mp4"),
            "file_name": "clip.mp4",
            "content_hash": "hash123",
            "downloaded": True,
            "download_date": utcnow_naive(),
            **extract_media_attributes(media),
        }

    async def test_sweep_after_listener_capture_keeps_the_metadata(self, adapter):
        media = _video_media(width=640, height=480, duration=12, size=90000)
        message = _message(7, media)
        media_id = f"{CHAT_ID}_7_video"

        await adapter.insert_media(self._listener_row(media_id, media), account_id=1)
        before = await _media_row(adapter, media_id)
        assert (before.mime_type, before.width, before.height, before.duration) == ("video/mp4", 640, 480, 12)

        backup = _make_backup(self.media_root)
        sweep_row = await backup._process_media(message, CHAT_ID)
        assert sweep_row is not None
        await adapter.insert_media(sweep_row, account_id=1)

        after = await _media_row(adapter, media_id)
        assert (after.mime_type, after.width, after.height, after.duration) == ("video/mp4", 640, 480, 12)

    async def test_sweep_extraction_matches_the_listener_extraction(self, adapter):
        """Same media object in, same five columns out — one shared extractor."""
        media = _video_media(width=1280, height=720, duration=30.2, size=4242)
        message = _message(7, media)

        backup = _make_backup(self.media_root)
        sweep_row = await backup._process_media(message, CHAT_ID)

        listener_attributes = extract_media_attributes(media)
        for column in ("mime_type", "width", "height", "duration"):
            assert sweep_row[column] == listener_attributes[column]
        # file_size is the ONE deliberate difference: the sweep measures the file
        # it just wrote instead of trusting Telegram's declared size.
        assert sweep_row["file_size"] == len(b"mediabytes")

    async def test_sweep_writes_an_int_duration_into_the_integer_column(self, adapter):
        media = _video_media(duration=12.4)
        backup = _make_backup(self.media_root)
        sweep_row = await backup._process_media(_message(7, media), CHAT_ID)

        await adapter.insert_media(sweep_row, account_id=1)

        stored = await _media_row(adapter, f"{CHAT_ID}_7_video")
        assert stored.duration == 12
        assert isinstance(stored.duration, int)

    async def test_upsert_without_metadata_cannot_null_stored_values(self, adapter):
        """Any writer with nothing to say (e.g. the not-downloaded row) must not blank it.

        The failed re-attempt row is exactly what ``_process_media`` returns from its
        except branch: id/type/message_id/chat_id and ``downloaded: False``. It used
        to erase the file record of a file that is still on disk.
        """
        media_id = f"{CHAT_ID}_7_video"
        listener_row = self._listener_row(media_id, _video_media())
        await adapter.insert_media(listener_row, account_id=1)

        await adapter.insert_media(
            {
                "id": media_id,
                "message_id": 7,
                "chat_id": CHAT_ID,
                "type": "video",
                "downloaded": False,
            },
            account_id=1,
        )

        stored = await _media_row(adapter, media_id)
        assert (stored.mime_type, stored.width, stored.height, stored.duration) == ("video/mp4", 640, 480, 12)
        assert stored.file_path == listener_row["file_path"]
        assert stored.file_name == "clip.mp4"
        assert stored.file_size == 90000
        assert stored.content_hash == "hash123"
        assert stored.download_date is not None

    async def test_failed_download_leaves_the_row_retryable(self, adapter):
        """A row whose file is gone must return to the pending-download queue.

        The except branch of ``_process_media`` reports ``downloaded: False``, and
        that is the ONLY always-on recovery signal: ``_retry_pending_media_downloads``
        drains ``get_pending_media_downloads`` every cycle, while the disk-stat
        scan ``_verify_and_redownload_media`` is gated on VERIFY_MEDIA (default
        false). Pinning the flag at 1 stranded the row pointing at a missing file.
        """
        media_id = f"{CHAT_ID}_7_video"
        await adapter.insert_media(self._listener_row(media_id, _video_media()), account_id=1)

        await adapter.insert_media(
            {
                "id": media_id,
                "message_id": 7,
                "chat_id": CHAT_ID,
                "type": "video",
                "downloaded": False,
            },
            account_id=1,
        )

        stored = await _media_row(adapter, media_id)
        assert stored.downloaded == 0
        pending = await adapter.get_pending_media_downloads(100 * 1024 * 1024, 5, account_id=1)
        assert [row["id"] for row in pending] == [media_id]

    async def test_oversize_skip_row_does_not_strand_an_already_downloaded_file(self, adapter):
        """Lowering MAX_MEDIA_SIZE must not un-download a file that is already stored.

        The skip row never attempted a download, so it states no ``downloaded``
        outcome at all and the stored flag is kept. Flipping it to 0 would hide the
        file from the gallery (``get_media_paginated`` filters ``downloaded == 1``)
        while the retry queue skips it for being over the limit — orphaned on disk.
        """
        media_id = f"{CHAT_ID}_7_video"
        listener_row = self._listener_row(media_id, _video_media())
        await adapter.insert_media(listener_row, account_id=1)

        backup = _make_backup(self.media_root)
        backup.config.get_max_media_size_bytes = MagicMock(return_value=1)
        skip_row = await backup._process_media(_message(7, _video_media()), CHAT_ID)
        assert "downloaded" not in skip_row
        await adapter.insert_media(skip_row, account_id=1)

        stored = await _media_row(adapter, media_id)
        assert stored.downloaded == 1
        assert stored.file_path == listener_row["file_path"]
        # The one thing the skip row DOES know is the real size — a value overwrites.
        assert stored.file_size == skip_row["file_size"]

    async def test_oversize_skip_row_is_never_retried(self, adapter):
        """Even as a first sighting (flag 0), its over-limit size keeps it out of the queue."""
        backup = _make_backup(self.media_root)
        backup.config.get_max_media_size_bytes = MagicMock(return_value=1)
        skip_row = await backup._process_media(_message(7, _video_media(size=90000)), CHAT_ID)
        await adapter.insert_media(skip_row, account_id=1)

        stored = await _media_row(adapter, skip_row["id"])
        assert stored.downloaded == 0
        assert stored.file_size == 90000
        assert await adapter.get_pending_media_downloads(1, 5, account_id=1) == []
        # Positive control: raise the limit and the very same row IS retryable.
        assert [row["id"] for row in await adapter.get_pending_media_downloads(100 * 1024 * 1024, 5, account_id=1)] == [
            skip_row["id"]
        ]

    async def test_redownload_to_a_new_path_still_updates_the_row(self, adapter):
        """Positive control: COALESCE only falls back on NULL, it never pins a value."""
        media_id = f"{CHAT_ID}_7_video"
        await adapter.insert_media(self._listener_row(media_id, _video_media()), account_id=1)

        new_path = os.path.join(self.media_root, "_shared", "ab", "moved.mp4")
        await adapter.insert_media(
            {
                "id": media_id,
                "message_id": 7,
                "chat_id": CHAT_ID,
                "type": "video",
                **extract_media_attributes(_video_media()),
                "file_path": new_path,
                "file_name": "moved.mp4",
                "file_size": 4242,
                "content_hash": "hash456",
                "downloaded": True,
                "download_date": utcnow_naive(),
            },
            account_id=1,
        )

        stored = await _media_row(adapter, media_id)
        assert stored.file_path == new_path
        assert stored.file_name == "moved.mp4"
        assert stored.file_size == 4242
        assert stored.content_hash == "hash456"

    async def test_deliberate_clear_path_still_clears(self, adapter):
        """mark_media_for_redownload is the ONE writer allowed to null the file record."""
        media_id = f"{CHAT_ID}_7_video"
        await adapter.insert_media(self._listener_row(media_id, _video_media()), account_id=1)

        await adapter.mark_media_for_redownload(media_id, account_id=1)

        stored = await _media_row(adapter, media_id)
        assert stored.downloaded == 0
        assert stored.file_path is None
        assert stored.download_date is None

    async def test_upsert_compiles_on_both_dialects(self):
        """The conflict clause must compile identically on SQLite and psycopg2."""
        from sqlalchemy.dialects import postgresql, sqlite

        async def _compiled(media_data, dialect):
            db_manager = MagicMock()
            db_manager._is_sqlite = dialect.name == "sqlite"

            session = AsyncMock()
            async_ctx = AsyncMock()
            async_ctx.__aenter__ = AsyncMock(return_value=session)
            async_ctx.__aexit__ = AsyncMock(return_value=False)
            db_manager.async_session_factory.return_value = async_ctx

            await DatabaseAdapter(db_manager).insert_media(media_data, account_id=1)
            return str(session.execute.await_args[0][0].compile(dialect=dialect))

        for dialect in (sqlite.dialect(), postgresql.psycopg2.dialect()):
            stated = await _compiled({"id": "m1", "type": "photo", "downloaded": False}, dialect)
            assert "coalesce(excluded.file_path, media.file_path)" in stated
            # A stated outcome is written, not preserved.
            assert "downloaded = media.downloaded" not in stated

            unstated = await _compiled({"id": "m1", "type": "photo"}, dialect)
            assert "coalesce(excluded.file_path, media.file_path)" in unstated
            assert "downloaded = media.downloaded" in unstated

    async def test_upsert_still_overwrites_a_real_value_with_a_real_value(self, adapter):
        media_id = f"{CHAT_ID}_7_video"
        await adapter.insert_media(self._listener_row(media_id, _video_media()), account_id=1)

        await adapter.insert_media(
            {
                "id": media_id,
                "message_id": 7,
                "chat_id": CHAT_ID,
                "type": "video",
                "mime_type": "video/webm",
                "width": 1920,
                "height": 1080,
                "duration": 99,
                "downloaded": True,
            },
            account_id=1,
        )

        stored = await _media_row(adapter, media_id)
        assert (stored.mime_type, stored.width, stored.height, stored.duration) == ("video/webm", 1920, 1080, 99)


# ---------------------------------------------------------------------------
# Cursor scoping
# ---------------------------------------------------------------------------


class TestMediaCursorIsChatScoped:
    """A media id from another chat must not steer this chat's page."""

    async def _seed(self, adapter):
        async with adapter.db_manager.async_session_factory() as session:
            session.add(
                Media(
                    id="mine_1",
                    message_id=7,
                    chat_id=CHAT_ID,
                    type="photo",
                    file_path=f"{CHAT_ID}/mine.jpg",
                    file_name="mine.jpg",
                    downloaded=1,
                )
            )
            session.add(
                Media(
                    id="theirs_1",
                    message_id=8,
                    chat_id=OTHER_CHAT_ID,
                    type="photo",
                    file_path=f"{OTHER_CHAT_ID}/theirs.jpg",
                    file_name="theirs.jpg",
                    downloaded=1,
                )
            )
            await session.commit()

    async def test_foreign_cursor_returns_an_empty_page(self, adapter):
        """Unscoped, this leaked that 'theirs_1' exists and is NEWER than our rows."""
        await self._seed(adapter)

        result = await adapter.get_media_paginated(CHAT_ID, before_id="theirs_1")

        assert result == {"items": [], "has_more": False}

    async def test_foreign_cursor_is_indistinguishable_from_an_unknown_one(self, adapter):
        await self._seed(adapter)

        foreign = await adapter.get_media_paginated(CHAT_ID, before_id="theirs_1")
        unknown = await adapter.get_media_paginated(CHAT_ID, before_id="no_such_media_id")

        assert foreign == unknown

    async def test_own_cursor_still_paginates(self, adapter):
        """Positive control: scoping must not break the real cursor."""
        await self._seed(adapter)

        first = await adapter.get_media_paginated(CHAT_ID)
        assert [item["id"] for item in first["items"]] == ["mine_1"]

        after = await adapter.get_media_paginated(CHAT_ID, before_id="mine_1")
        assert after["items"] == []
