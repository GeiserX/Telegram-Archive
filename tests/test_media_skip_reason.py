"""Why a media row will never download on its own (#465).

A row at ``downloaded = 0`` with no file is one of three things: a failed
download the retry drain will pick up, a file over ``MAX_MEDIA_SIZE_MB``, or a
file the media-type whitelist declined. The viewer used to call all three
"pending". The backup now records ``media.skip_reason`` (migration 030) and
re-derives it every run, and the viewer reads it.

Also here: the config startup summary that never reached the log (#445) and
the skip-media cleanup default (#444).
"""

import importlib.util
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Media
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001234567890
INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


@pytest.fixture
async def adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'telegram_archive.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _row(adapter_, media_id: str) -> Media:
    async with adapter_.db_manager.async_session_factory() as session:
        return (await session.execute(select(Media).where(Media.id == media_id))).scalar_one()


async def _seed(adapter_, media_id: str, msg_id: int, **extra) -> None:
    await adapter_.insert_message(
        {"id": msg_id, "chat_id": CHAT_ID, "date": datetime(2026, 7, 3, 9, msg_id), "text": "m"}, account_id=1
    )
    await adapter_.insert_media({"id": media_id, "message_id": msg_id, "chat_id": CHAT_ID, **extra}, account_id=1)


# ============================================================================
# Migration 030
# ============================================================================

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_spec = importlib.util.spec_from_file_location("migration_030", _VERSIONS_DIR / "20260923_030_add_media_skip_reason.py")
migration_030 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_030)


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _media_columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("media")}


def _create_media_table(conn, with_column: bool = False):
    extra = ", skip_reason VARCHAR(16)" if with_column else ""
    conn.execute(
        sa.text(
            "CREATE TABLE media (account_id INTEGER NOT NULL DEFAULT 1, id VARCHAR(255) NOT NULL, "
            f"type VARCHAR(50), downloaded INTEGER{extra}, PRIMARY KEY (account_id, id))"
        )
    )


class TestMigration030:
    def test_revision_chain(self):
        assert migration_030.revision == "030"
        assert migration_030.down_revision == "029"

    def test_upgrade_adds_the_column_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_media_table(conn)
            _run(conn, migration_030.upgrade)
            assert "skip_reason" in _media_columns(conn)
            _run(conn, migration_030.upgrade)
            assert "skip_reason" in _media_columns(conn)

    def test_upgrade_noop_on_create_all_shape(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_media_table(conn, with_column=True)
            _run(conn, migration_030.upgrade)
            assert "skip_reason" in _media_columns(conn)

    def test_upgrade_noop_without_media_table(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_030.upgrade)
            assert "media" not in sa.inspect(conn).get_table_names()

    def test_downgrade_drops_the_column_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_media_table(conn, with_column=True)
            _run(conn, migration_030.downgrade)
            assert "skip_reason" not in _media_columns(conn)
            _run(conn, migration_030.downgrade)
            assert "skip_reason" not in _media_columns(conn)


# ============================================================================
# insert_media: the presence rule
# ============================================================================


class TestInsertMediaSkipReason:
    async def test_named_reason_is_stored(self, adapter):
        await _seed(adapter, "m1", 1, type="video", file_size=10, skip_reason="oversize")
        assert (await _row(adapter, "m1")).skip_reason == "oversize"

    async def test_writer_that_knows_nothing_keeps_it(self, adapter):
        await _seed(adapter, "m1", 1, type="video", skip_reason="filtered")
        await adapter.insert_media({"id": "m1", "message_id": 1, "chat_id": CHAT_ID, "type": "video"}, account_id=1)
        assert (await _row(adapter, "m1")).skip_reason == "filtered"

    async def test_observed_download_outcome_clears_it(self, adapter):
        """A relaxed filter lets the drain fetch the file; its row must stop saying 'filtered'."""
        await _seed(adapter, "m1", 1, type="video", skip_reason="filtered")
        await adapter.insert_media(
            {"id": "m1", "message_id": 1, "chat_id": CHAT_ID, "type": "video", "downloaded": True, "file_path": "x"},
            account_id=1,
        )
        row = await _row(adapter, "m1")
        assert row.downloaded == 1
        assert row.skip_reason is None

    async def test_explicit_none_clears_it(self, adapter):
        await _seed(adapter, "m1", 1, type="video", skip_reason="oversize")
        await adapter.insert_media(
            {"id": "m1", "message_id": 1, "chat_id": CHAT_ID, "type": "video", "skip_reason": None}, account_id=1
        )
        assert (await _row(adapter, "m1")).skip_reason is None


# ============================================================================
# The two skip paths name their reason
# ============================================================================


def _make_message(msg_id: int) -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.reply_to = None
    msg.action = None
    msg.date = datetime(2026, 1, 1)
    return msg


class TestProcessMediaRecordsReason:
    def setup_method(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
        self.backup.db = AsyncMock()
        self.backup.db.reconcile_media_row = AsyncMock(return_value=None)
        self.backup.client = AsyncMock()
        self.backup.config = MagicMock()
        self.backup.config.download_youtube_videos = False
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100)
        self.backup.config.media_download_allowed = None
        self.backup._get_media_type = MagicMock(return_value="video")
        self.backup._get_media_filename = MagicMock(return_value="clip.mp4")

    async def test_oversize_row_says_oversize(self):
        msg = _make_message(3)
        msg.media = MagicMock()
        self.backup._get_media_size = MagicMock(return_value=999)
        with patch("src.telegram_backup.media_download_allowed", return_value=True):
            row = await self.backup._process_media(msg, CHAT_ID)
        assert "downloaded" not in row
        assert row["skip_reason"] == "oversize"

    async def test_filtered_row_says_filtered(self):
        msg = _make_message(4)
        msg.media = MagicMock()
        with patch("src.telegram_backup.media_download_allowed", return_value=False):
            row = await self.backup._process_media(msg, CHAT_ID)
        assert "downloaded" not in row
        assert row["skip_reason"] == "filtered"


# ============================================================================
# reconcile_media_skip_reasons: settings change, reasons follow
# ============================================================================


class TestReconcileSkipReasons:
    async def test_marks_oversize_and_filtered_and_leaves_pending_alone(self, adapter):
        await _seed(adapter, "big", 1, type="video", file_size=5000, downloaded=False)
        await _seed(adapter, "doc", 2, type="document", file_size=10, downloaded=False)
        await _seed(adapter, "photo", 3, type="photo", file_size=10, downloaded=False)
        await _seed(adapter, "poll", 4, type="poll", file_size=0, downloaded=False)  # metadata-only, untouched

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1, media_types={"photo"})

        assert counts == {"oversize": 1, "filtered": 1, "cleared": 0}
        assert (await _row(adapter, "big")).skip_reason == "oversize"
        assert (await _row(adapter, "doc")).skip_reason == "filtered"
        assert (await _row(adapter, "photo")).skip_reason is None
        assert (await _row(adapter, "poll")).skip_reason is None

    async def test_relaxed_settings_clear_the_reason(self, adapter):
        await _seed(adapter, "big", 1, type="video", file_size=5000, downloaded=False, skip_reason="oversize")
        await _seed(adapter, "doc", 2, type="document", file_size=10, downloaded=False, skip_reason="filtered")

        counts = await adapter.reconcile_media_skip_reasons(10_000, account_id=1, media_types=None)

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 2}
        assert (await _row(adapter, "big")).skip_reason is None
        assert (await _row(adapter, "doc")).skip_reason is None

    async def test_downloaded_rows_and_other_accounts_are_untouched(self, adapter):
        await _seed(adapter, "have", 1, type="video", file_size=5000, downloaded=True)
        await adapter.insert_message(
            {"id": 9, "chat_id": CHAT_ID, "date": datetime(2026, 7, 3, 9, 9), "text": "m"}, account_id=2
        )
        await adapter.insert_media(
            {"id": "other", "message_id": 9, "chat_id": CHAT_ID, "type": "video", "file_size": 5000}, account_id=2
        )

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1)

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 0}
        assert (await _row(adapter, "have")).skip_reason is None
        assert (await _row(adapter, "other")).skip_reason is None

    async def test_drain_reconciles_before_it_reads_pending(self):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.get_max_media_size_bytes = MagicMock(return_value=123)
        backup.config.max_media_download_attempts = 5
        backup.config.skip_media_chat_ids = {CHAT_ID}
        backup.config.download_media_types = {"photo"}
        backup.config.download_document_mime_types = set()
        backup.config.download_document_mime_extensions = set()
        backup.db = AsyncMock()
        backup.db.reconcile_media_skip_reasons = AsyncMock(return_value={"oversize": 0, "filtered": 0, "cleared": 0})
        backup.db.get_pending_media_downloads = AsyncMock(return_value=[])
        backup.db.count_capped_media_downloads = AsyncMock(return_value=0)

        await backup._retry_pending_media_downloads()

        backup.db.reconcile_media_skip_reasons.assert_awaited_once_with(
            123,
            account_id=1,
            media_types={"photo"},
            document_mime_types=set(),
            document_mime_extensions=set(),
            skip_media_chat_ids={CHAT_ID},
        )


# ============================================================================
# What the viewer sees
# ============================================================================


class TestViewerSurfaces:
    async def test_status_counts_split_skipped_from_pending(self, adapter):
        await _seed(adapter, "pend", 1, type="photo", downloaded=False)
        await _seed(adapter, "big", 2, type="video", downloaded=False, skip_reason="oversize")
        await _seed(adapter, "doc", 3, type="document", downloaded=False, skip_reason="filtered")
        await _seed(adapter, "have", 4, type="photo", downloaded=True)

        await _seed(adapter, "tired", 5, type="video", downloaded=False, skip_reason="oversize")
        for _ in range(7):
            await adapter.increment_media_download_attempts("tired", account_id=1)
        await _seed(adapter, "havebut", 6, type="photo", downloaded=True, skip_reason="filtered")
        await _seed(adapter, "poll", 7, type="poll", downloaded=False, skip_reason="filtered")

        counts = await adapter.get_operator_status_counts(max_attempts=5)

        assert counts == {"downloaded": 2, "pending": 1, "exhausted": 0, "skipped": 3}
        # The "raise the retry cap" warning must not count a row the cap would not retry.
        assert await adapter.count_capped_media_downloads(5, account_id=1) == 0

    async def test_message_payload_carries_the_reason(self, adapter):
        await adapter.upsert_chat({"id": CHAT_ID, "type": "channel", "title": "Chat A"}, account_id=1)
        await _seed(adapter, "big", 1, type="video", file_size=5000, downloaded=False, skip_reason="oversize")

        messages = await adapter.get_messages_paginated(CHAT_ID, limit=10, account_id=1)
        media = [m["media"] for m in messages if m["id"] == 1][0]

        assert media["skip_reason"] == "oversize"
        assert media["file_path"] is None

    def test_placeholder_names_the_reason(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert "msg.media?.skip_reason === 'oversize'" in html
        assert "Not downloaded: larger than the size limit" in html
        assert "msg.media?.skip_reason === 'filtered'" in html
        assert "Not downloaded: excluded by the media filter" in html
        assert "Will download on next backup" in html  # still the wording for a genuine pending row
        assert "statusData.media.skipped" in html


# ============================================================================
# #445: the startup summary is a call, not a construction side effect
# ============================================================================


class TestConfigSummaryLogging:
    def _env(self):
        return {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "BACKUP_PATH": tempfile.mkdtemp(),
            "LOG_CHAT_TITLES": "true",
        }

    def test_construction_emits_no_summary(self, caplog):
        caplog.set_level("DEBUG", logger="src.config")
        with patch.dict(os.environ, self._env(), clear=True):
            Config()
        assert "Configuration loaded successfully" not in caplog.text
        assert "LOG_CHAT_TITLES enabled" not in caplog.text

    def test_log_summary_emits_it_once(self, caplog):
        caplog.set_level("DEBUG", logger="src.config")
        with patch.dict(os.environ, self._env(), clear=True):
            config = Config()
        config.log_summary()
        assert caplog.text.count("Configuration loaded successfully") == 1
        assert "LOG_CHAT_TITLES enabled" in caplog.text

    def test_every_entrypoint_logs_the_summary_after_logging_is_configured(self):
        """Each ``setup_logging(config)`` is followed by ``config.log_summary()`` before the next one."""
        src = Path(__file__).resolve().parents[1] / "src"
        for name in (
            "__main__.py",
            "scheduler.py",
            "listener.py",
            "telegram_backup.py",
            "setup_auth.py",
            "export_backup.py",
        ):
            text = (src / name).read_text(encoding="utf-8")
            starts = [i for i in range(len(text)) if text.startswith("setup_logging(config)", i)]
            assert starts, name
            assert text.count("config.log_summary()") == len(starts), name
            for pos, nxt in zip(starts, starts[1:] + [len(text)], strict=True):
                summary = text.find("config.log_summary()", pos)
                assert summary != -1 and summary < nxt, (
                    f"{name}: setup_logging at {pos} has no log_summary before {nxt}"
                )
        viewer = (src / "web" / "main.py").read_text(encoding="utf-8")
        assert viewer.index("logging.basicConfig(") < viewer.index("config.log_summary()")


# ============================================================================
# #444: the skip-media cleanup says what it did not free
# ============================================================================


class TestSkipMediaCleanupHonesty:
    async def test_symlink_removal_names_the_shared_bytes_it_left(self, tmp_path, caplog):
        chat_dir = tmp_path / str(CHAT_ID)
        chat_dir.mkdir()
        blob = tmp_path / "_shared" / "ab" / "abcdef.mp4"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"x" * 4096)
        link = chat_dir / "clip.mp4"
        link.symlink_to(blob)

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = str(tmp_path)
        backup.db = AsyncMock()
        backup.db.get_media_for_chat = AsyncMock(return_value=[{"file_path": str(link)}])
        backup.db.delete_media_for_chat = AsyncMock(return_value=1)

        caplog.set_level("INFO", logger="src.telegram_backup")
        await backup._cleanup_existing_media(CHAT_ID)

        assert not link.exists()
        assert blob.exists()  # the bytes stay: nothing here reclaims _shared/
        assert "1 symlinks removed (their shared files stay in _shared/)" in caplog.text
        assert "MB freed" not in caplog.text


# ============================================================================
# Review follow-ups: the drain writes reasons the SQL cannot derive, and a
# settings change converges in one reconcile pass
# ============================================================================


def _drain_backup(pending_record: dict) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = MagicMock()
    backup.config.get_max_media_size_bytes = MagicMock(return_value=1000)
    backup.config.max_media_download_attempts = 5
    backup.config.skip_media_chat_ids = set()
    backup.config.download_media_types = set()
    backup.config.download_document_mime_types = {"application/pdf"}
    backup.config.download_document_mime_extensions = {"pdf"}
    backup.config.download_youtube_videos = False
    backup.db = AsyncMock()
    backup.db.reconcile_media_skip_reasons = AsyncMock(return_value={"oversize": 0, "filtered": 0, "cleared": 0})
    backup.db.get_pending_media_downloads = AsyncMock(return_value=[pending_record])
    backup.db.count_capped_media_downloads = AsyncMock(return_value=0)
    msg = _make_message(pending_record["message_id"])
    msg.media = MagicMock()
    backup.client = AsyncMock()
    backup.client.get_messages = AsyncMock(return_value=[msg])
    backup._get_media_type = MagicMock(return_value=pending_record["type"])
    return backup


class TestDrainWritesUnderivableReasons:
    """A failed first download leaves mime, name and size NULL, so the SQL mirror
    cannot classify the row; the drain, which has the live message, must."""

    async def test_filter_decline_stores_the_reason_without_charging(self):
        record = {"id": f"{CHAT_ID}_7_document", "message_id": 7, "chat_id": CHAT_ID, "type": "document"}
        backup = _drain_backup(record)
        skip_row = {**record, "file_name": "report.zip", "mime_type": "application/zip", "skip_reason": "filtered"}
        backup._process_media = AsyncMock(return_value=skip_row)

        await backup._retry_pending_media_downloads()

        backup.db.insert_media.assert_awaited_once_with(skip_row, account_id=1)
        backup.db.increment_media_download_attempts.assert_not_awaited()

    async def test_oversize_decline_stores_the_reason_without_charging(self):
        record = {"id": f"{CHAT_ID}_8_video", "message_id": 8, "chat_id": CHAT_ID, "type": "video"}
        backup = _drain_backup(record)
        skip_row = {**record, "file_size": 5000, "skip_reason": "oversize"}
        backup._process_media = AsyncMock(return_value=skip_row)

        await backup._retry_pending_media_downloads()

        backup.db.insert_media.assert_awaited_once_with(skip_row, account_id=1)
        backup.db.increment_media_download_attempts.assert_not_awaited()

    async def test_a_real_failure_still_charges_an_attempt(self):
        record = {"id": f"{CHAT_ID}_9_video", "message_id": 9, "chat_id": CHAT_ID, "type": "video"}
        backup = _drain_backup(record)
        backup._process_media = AsyncMock(return_value={**record, "downloaded": False})

        await backup._retry_pending_media_downloads()

        backup.db.insert_media.assert_not_awaited()
        backup.db.increment_media_download_attempts.assert_awaited_once_with(record["id"], account_id=1)

    async def test_reconcile_summary_line_is_logged_only_when_something_changed(self, caplog):
        record = {"id": f"{CHAT_ID}_10_video", "message_id": 10, "chat_id": CHAT_ID, "type": "video"}
        backup = _drain_backup(record)
        backup.db.get_pending_media_downloads = AsyncMock(return_value=[])
        caplog.set_level("INFO", logger="src.telegram_backup")

        await backup._retry_pending_media_downloads()
        assert "Media skip reasons" not in caplog.text

        backup.db.reconcile_media_skip_reasons = AsyncMock(return_value={"oversize": 2, "filtered": 1, "cleared": 3})
        await backup._retry_pending_media_downloads()
        assert "Media skip reasons: 2 over the size cap, 1 outside the media filter, 3 cleared" in caplog.text


class TestReconcileConvergesInOnePass:
    async def test_filter_removed_on_an_oversize_row_reads_oversize_at_once(self, adapter):
        await _seed(adapter, "bigdoc", 1, type="document", file_size=5000, downloaded=False, skip_reason="filtered")

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1, media_types=None)

        assert counts == {"oversize": 1, "filtered": 0, "cleared": 1}
        assert (await _row(adapter, "bigdoc")).skip_reason == "oversize"

    async def test_cap_raised_on_a_filtered_row_reads_filtered_at_once(self, adapter):
        await _seed(adapter, "bigdoc", 1, type="document", file_size=5000, downloaded=False, skip_reason="oversize")

        counts = await adapter.reconcile_media_skip_reasons(10_000, account_id=1, media_types={"photo"})

        assert counts == {"oversize": 0, "filtered": 1, "cleared": 1}
        assert (await _row(adapter, "bigdoc")).skip_reason == "filtered"

    async def test_a_row_at_the_cap_is_not_oversize(self, adapter):
        await _seed(adapter, "edge", 1, type="video", file_size=1000, downloaded=False)

        await adapter.reconcile_media_skip_reasons(1000, account_id=1)

        assert (await _row(adapter, "edge")).skip_reason is None
        pending = await adapter.get_pending_media_downloads(1000, 5, account_id=1)
        assert [m["id"] for m in pending] == ["edge"]

    async def test_a_filtered_row_over_the_cap_keeps_its_reason(self, adapter):
        await _seed(adapter, "bigdoc", 1, type="document", file_size=5000, downloaded=False, skip_reason="filtered")

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1, media_types={"photo"})

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 0}
        assert (await _row(adapter, "bigdoc")).skip_reason == "filtered"


class TestOtherMessageRoutesCarryTheReason:
    async def test_pinned_and_date_jump_payloads(self, adapter):
        await adapter.upsert_chat({"id": CHAT_ID, "type": "channel", "title": "Chat A"}, account_id=1)
        await adapter.insert_message(
            {"id": 1, "chat_id": CHAT_ID, "date": datetime(2026, 7, 3, 9, 1), "text": "m", "is_pinned": 1},
            account_id=1,
        )
        await adapter.insert_media(
            {
                "id": "big",
                "message_id": 1,
                "chat_id": CHAT_ID,
                "type": "video",
                "file_size": 5000,
                "skip_reason": "oversize",
            },
            account_id=1,
        )

        pinned = await adapter.get_pinned_messages(CHAT_ID, account_id=1)
        assert pinned[0]["media"]["skip_reason"] == "oversize"

        jumped = await adapter.find_message_by_date_with_joins(CHAT_ID, datetime(2026, 7, 3, 9, 1), None, account_id=1)
        assert jumped["media"]["skip_reason"] == "oversize"


class TestCleanupLogHasNoEmptyFragment:
    async def test_rows_only_cleanup_reads_cleanly(self, tmp_path, caplog):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = str(tmp_path)
        backup.db = AsyncMock()
        backup.db.get_media_for_chat = AsyncMock(return_value=[{"file_path": None}])
        backup.db.delete_media_for_chat = AsyncMock(return_value=3)
        caplog.set_level("INFO", logger="src.telegram_backup")

        await backup._cleanup_existing_media(CHAT_ID)

        assert "Cleaned up existing media for chat: 3 DB records deleted" in caplog.text
        assert ": ," not in caplog.text


# ============================================================================
# The document MIME mirror with one of its two columns NULL
# ============================================================================


class TestDocumentMimeMirrorWithOneNullColumn:
    """A document row with a name but no MIME, or the reverse, used to evaluate
    the mirror to NULL: the drain never fetched it and reconcile never marked it."""

    PDF_ONLY = {"document_mime_types": {"application/pdf"}, "document_mime_extensions": {".pdf"}}

    async def test_name_without_mime_outside_the_filter_is_filtered(self, adapter):
        await _seed(adapter, "zip", 1, type="document", file_name="report.zip", mime_type=None, downloaded=False)

        counts = await adapter.reconcile_media_skip_reasons(None, account_id=1, **self.PDF_ONLY)

        assert counts == {"oversize": 0, "filtered": 1, "cleared": 0}
        assert (await _row(adapter, "zip")).skip_reason == "filtered"

    async def test_mime_without_name_outside_the_filter_is_filtered(self, adapter):
        await _seed(adapter, "zip", 1, type="document", file_name=None, mime_type="application/zip", downloaded=False)

        counts = await adapter.reconcile_media_skip_reasons(None, account_id=1, **self.PDF_ONLY)

        assert counts == {"oversize": 0, "filtered": 1, "cleared": 0}
        assert (await _row(adapter, "zip")).skip_reason == "filtered"

    async def test_name_without_mime_inside_the_filter_stays_pending(self, adapter):
        await _seed(adapter, "pdf", 1, type="document", file_name="a.pdf", mime_type=None, downloaded=False)

        await adapter.reconcile_media_skip_reasons(None, account_id=1, **self.PDF_ONLY)

        assert (await _row(adapter, "pdf")).skip_reason is None
        pending = await adapter.get_pending_media_downloads(None, 5, account_id=1, **self.PDF_ONLY)
        assert [m["id"] for m in pending] == ["pdf"]


# ============================================================================
# SKIP_MEDIA_CHAT_IDS: the drain never fetches these chats
# ============================================================================


class TestSkippedChatsReadFiltered:
    async def test_a_row_in_a_skipped_chat_is_filtered_and_counted_skipped(self, adapter):
        await _seed(adapter, "photo", 1, type="photo", file_size=10, downloaded=False)

        counts = await adapter.reconcile_media_skip_reasons(
            1000, account_id=1, media_types={"photo"}, skip_media_chat_ids={CHAT_ID}
        )

        assert counts == {"oversize": 0, "filtered": 1, "cleared": 0}
        assert (await _row(adapter, "photo")).skip_reason == "filtered"
        status = await adapter.get_operator_status_counts(max_attempts=5)
        assert status == {"downloaded": 0, "pending": 0, "exhausted": 0, "skipped": 1}

    async def test_the_next_pass_keeps_the_mark_instead_of_clearing_and_remarking(self, adapter):
        await _seed(adapter, "photo", 1, type="photo", file_size=10, downloaded=False)
        settings = {"media_types": {"photo"}, "skip_media_chat_ids": {CHAT_ID}}
        await adapter.reconcile_media_skip_reasons(1000, account_id=1, **settings)

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1, **settings)

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 0}
        assert (await _row(adapter, "photo")).skip_reason == "filtered"

    async def test_removing_the_chat_from_the_set_clears_it(self, adapter):
        await _seed(adapter, "photo", 1, type="photo", file_size=10, downloaded=False)
        await adapter.reconcile_media_skip_reasons(1000, account_id=1, skip_media_chat_ids={CHAT_ID})

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1, skip_media_chat_ids=set())

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 1}
        assert (await _row(adapter, "photo")).skip_reason is None
        status = await adapter.get_operator_status_counts(max_attempts=5)
        assert status["pending"] == 1 and status["skipped"] == 0


# ============================================================================
# Remaining assertions from the 8.14.0 review
# ============================================================================


class TestReconcileTypeAndMimeFiltersTogether:
    async def test_mime_filter_inside_the_type_whitelist(self, adapter):
        exe = {"mime_type": "application/x-msdownload", "file_name": "setup.exe"}
        await _seed(adapter, "exe", 1, type="document", downloaded=False, **exe)
        await _seed(
            adapter, "pdf", 2, type="document", mime_type="application/pdf", file_name="a.pdf", downloaded=False
        )
        await _seed(adapter, "photo", 3, type="photo", downloaded=False)

        counts = await adapter.reconcile_media_skip_reasons(
            None,
            account_id=1,
            media_types={"document", "photo"},
            document_mime_types={"application/pdf"},
            document_mime_extensions={".pdf"},
        )

        assert counts == {"oversize": 0, "filtered": 1, "cleared": 0}
        assert (await _row(adapter, "exe")).skip_reason == "filtered"
        assert (await _row(adapter, "pdf")).skip_reason is None
        assert (await _row(adapter, "photo")).skip_reason is None

        relaxed = await adapter.reconcile_media_skip_reasons(None, account_id=1, media_types={"document", "photo"})

        assert relaxed["cleared"] == 1
        assert (await _row(adapter, "exe")).skip_reason is None


class TestStaleReasonOnADownloadedRow:
    """The over-size writer leaves ``downloaded`` alone, so a file an earlier,
    higher-capped run fetched can carry "oversize" while it sits on disk."""

    async def test_an_on_disk_row_over_the_cap_loses_the_reason(self, adapter):
        await _seed(adapter, "have", 1, type="video", file_size=5000, downloaded=True, skip_reason="oversize")

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1)

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 1}
        assert (await _row(adapter, "have")).skip_reason is None

    async def test_an_on_disk_row_within_the_cap_loses_the_reason(self, adapter):
        await _seed(adapter, "have", 1, type="video", file_size=10, downloaded=True, skip_reason="oversize")

        counts = await adapter.reconcile_media_skip_reasons(1000, account_id=1)

        assert counts == {"oversize": 0, "filtered": 0, "cleared": 1}
        assert (await _row(adapter, "have")).skip_reason is None


class TestGalleryNeverShowsASkippedRow:
    async def test_only_files_on_disk_reach_the_gallery(self, adapter):
        await _seed(adapter, "have", 1, type="photo", file_path="x/have.jpg", downloaded=True)
        await _seed(adapter, "big", 2, type="photo", file_size=5000, downloaded=False, skip_reason="oversize")
        await _seed(adapter, "doc", 3, type="photo", downloaded=False, skip_reason="filtered")

        page = await adapter.get_media_paginated(CHAT_ID, limit=10, account_id=1)

        assert [item["id"] for item in page["items"]] == ["have"]


class TestNoDownloadSessionKeepsTheReason:
    def test_the_strip_keeps_skip_reason(self):
        pytest.importorskip("fastapi")
        os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_skip_"))
        from src.web import main as web_main

        messages = [{"media": {"file_path": None, "type": "video", "skip_reason": "oversize"}}]
        web_main._strip_original_media_paths(messages)

        assert messages[0]["media"]["no_download"] is True
        assert messages[0]["media"]["skip_reason"] == "oversize"

    def test_the_placeholder_reason_branches_do_not_depend_on_the_session(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        start = html.index("<!-- Placeholder for media not yet downloaded -->")
        block = html[start : html.index("Will download on next backup", start)]

        assert '<div v-if="!msg.media?.file_path && msg.media?.type"' in block
        assert "<div v-if=\"msg.media?.skip_reason === 'oversize'\"" in block
        assert "<div v-else-if=\"msg.media?.skip_reason === 'filtered'\"" in block


class TestScriptsLogTheConfigSummary:
    def test_every_script_that_builds_a_config_logs_the_summary(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        building = sorted(p.name for p in scripts.glob("*.py") if "Config()" in p.read_text(encoding="utf-8"))
        assert building == ["deduplicate_media.py", "detect_albums.py", "fix_media_sizes.py", "update_media_sizes.py"]
        for name in building:
            text = (scripts / name).read_text(encoding="utf-8")
            assert text.count("config.log_summary()") == text.count("Config()"), name
            configured = text.index("logging.basicConfig(")
            built = text.index("config = Config()")
            summary = text.index("config.log_summary()")
            assert configured < built < summary, name
