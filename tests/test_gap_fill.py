"""Functional tests for the gap-fill feature (v7.3.0).

Tests cover:
- detect_message_gaps: real SQL queries against an in-memory SQLite database
- _fill_gaps / _fill_gap_range: Telegram client mocks exercising actual control flow
- Config: env-var parsing for FILL_GAPS and GAP_THRESHOLD
"""

import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.telegram_backup import TelegramBackup

# ---------------------------------------------------------------------------
# Helpers — lightweight in-memory async SQLite setup
# ---------------------------------------------------------------------------


async def _create_in_memory_adapter():
    """Create a DatabaseAdapter backed by an in-memory SQLite database.

    Returns (adapter, engine) so the caller can dispose the engine after use.
    """
    # StaticPool + check_same_thread=False keeps a single shared in-memory DB
    # across all connections, which is required for aiosqlite in-memory testing.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # Create the minimal schema needed for gap detection
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS chats ("
                "  id INTEGER PRIMARY KEY,"
                "  type TEXT NOT NULL DEFAULT 'channel',"
                "  title TEXT,"
                "  username TEXT,"
                "  first_name TEXT,"
                "  last_name TEXT,"
                "  phone TEXT,"
                "  description TEXT,"
                "  participants_count INTEGER,"
                "  is_forum INTEGER DEFAULT 0,"
                "  is_archived INTEGER DEFAULT 0,"
                "  last_synced_message_id INTEGER DEFAULT 0,"
                "  created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                "  updated_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS messages ("
                "  id INTEGER NOT NULL,"
                "  chat_id INTEGER NOT NULL,"
                "  sender_id INTEGER,"
                "  date TEXT NOT NULL DEFAULT '2025-01-01 00:00:00',"
                "  text TEXT,"
                "  reply_to_msg_id INTEGER,"
                "  reply_to_top_id INTEGER,"
                "  reply_to_text TEXT,"
                "  forward_from_id INTEGER,"
                "  edit_date TEXT,"
                "  raw_data TEXT,"
                "  created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
                "  is_outgoing INTEGER DEFAULT 0,"
                "  is_pinned INTEGER DEFAULT 0,"
                "  PRIMARY KEY (id, chat_id)"
                ")"
            )
        )

    # Wire up a real DatabaseManager (skip its init() — we supply our own engine)
    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    adapter = DatabaseAdapter(db_manager)
    return adapter, engine


async def _insert_messages(adapter: DatabaseAdapter, chat_id: int, msg_ids: list[int]):
    """Insert message rows with the given IDs into the test database."""
    async with adapter.db_manager.async_session_factory() as session:
        for mid in msg_ids:
            await session.execute(
                text("INSERT INTO messages (id, chat_id, date) VALUES (:id, :cid, '2025-01-01 00:00:00')"),
                {"id": mid, "cid": chat_id},
            )
        await session.commit()


async def _insert_chat(adapter: DatabaseAdapter, chat_id: int, title: str = "Test Chat"):
    """Insert a chat row into the test database."""
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            text("INSERT INTO chats (id, title, type) VALUES (:id, :title, 'channel')"), {"id": chat_id, "title": title}
        )
        await session.commit()


# ===========================================================================
# 1. TestDetectMessageGaps — real SQL against in-memory SQLite
# ===========================================================================


class TestDetectMessageGaps:
    """Exercise detect_message_gaps with a real async SQLite database."""

    async def test_no_gaps_consecutive_ids(self):
        """Consecutive message IDs should produce zero gaps."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            await _insert_messages(adapter, chat_id=100, msg_ids=list(range(1, 51)))
            gaps = await adapter.detect_message_gaps(chat_id=100, threshold=50)
            assert gaps == []
        finally:
            await engine.dispose()

    async def test_single_large_gap(self):
        """IDs 1-50 then 100-150 should return one gap (50, 100, 50)."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            ids = list(range(1, 51)) + list(range(100, 151))
            await _insert_messages(adapter, chat_id=200, msg_ids=ids)
            gaps = await adapter.detect_message_gaps(chat_id=200, threshold=49)

            assert len(gaps) == 1
            gap_start, gap_end, gap_size = gaps[0]
            assert gap_start == 50
            assert gap_end == 100
            assert gap_size == 50
        finally:
            await engine.dispose()

    async def test_multiple_gaps_sorted(self):
        """Multiple gaps should all be returned, sorted by gap_start."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            # Gap 1: between 10 and 100 (size 90)
            # Gap 2: between 110 and 300 (size 190)
            ids = list(range(1, 11)) + list(range(100, 111)) + list(range(300, 311))
            await _insert_messages(adapter, chat_id=300, msg_ids=ids)
            gaps = await adapter.detect_message_gaps(chat_id=300, threshold=50)

            assert len(gaps) == 2
            assert gaps[0] == (10, 100, 90)
            assert gaps[1] == (110, 300, 190)
            # Verify sorted by gap_start
            assert gaps[0][0] < gaps[1][0]
        finally:
            await engine.dispose()

    async def test_gap_below_threshold_not_returned(self):
        """A gap smaller than or equal to the threshold should not appear."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            # IDs 1-10 then 60-70 → gap of 50 at threshold=50 means gap_size > threshold
            # gap_size = 60 - 10 = 50, and the query uses > threshold, so 50 is NOT > 50
            ids = list(range(1, 11)) + list(range(60, 71))
            await _insert_messages(adapter, chat_id=400, msg_ids=ids)
            gaps = await adapter.detect_message_gaps(chat_id=400, threshold=50)

            assert gaps == [], f"Gap of exactly threshold should not be returned, got {gaps}"
        finally:
            await engine.dispose()

    async def test_gap_just_above_threshold_returned(self):
        """A gap of threshold+1 should be returned."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            # IDs 1-10 then 62-70 → gap_size = 62 - 10 = 52 > 50
            ids = list(range(1, 11)) + list(range(62, 71))
            await _insert_messages(adapter, chat_id=401, msg_ids=ids)
            gaps = await adapter.detect_message_gaps(chat_id=401, threshold=50)

            assert len(gaps) == 1
            assert gaps[0] == (10, 62, 52)
        finally:
            await engine.dispose()

    async def test_single_message_no_gaps(self):
        """A single message in the chat should produce no gaps."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            await _insert_messages(adapter, chat_id=500, msg_ids=[42])
            gaps = await adapter.detect_message_gaps(chat_id=500, threshold=50)
            assert gaps == []
        finally:
            await engine.dispose()

    async def test_empty_chat_no_gaps(self):
        """A chat with zero messages should produce no gaps."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            gaps = await adapter.detect_message_gaps(chat_id=999, threshold=50)
            assert gaps == []
        finally:
            await engine.dispose()

    async def test_different_chats_isolated(self):
        """Gaps in one chat should not appear in another chat's results."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            # Chat 1: has a gap
            await _insert_messages(adapter, chat_id=10, msg_ids=[1, 2, 3, 100])
            # Chat 2: no gap
            await _insert_messages(adapter, chat_id=20, msg_ids=[1, 2, 3, 4, 5])

            gaps_chat1 = await adapter.detect_message_gaps(chat_id=10, threshold=50)
            gaps_chat2 = await adapter.detect_message_gaps(chat_id=20, threshold=50)

            assert len(gaps_chat1) == 1
            assert gaps_chat1[0] == (3, 100, 97)
            assert gaps_chat2 == []
        finally:
            await engine.dispose()


# ===========================================================================
# 2. TestGetChatsWithMessages — real SQL
# ===========================================================================


class TestGetChatsWithMessages:
    """Exercise get_chats_with_messages with a real async SQLite database."""

    async def test_returns_all_chat_ids(self):
        """Should return all chat IDs from the chats table."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            await _insert_chat(adapter, chat_id=-1001, title="Chat A")
            await _insert_chat(adapter, chat_id=-1002, title="Chat B")
            await _insert_chat(adapter, chat_id=-1003, title="Chat C")

            result = await adapter.get_chats_with_messages()
            assert sorted(result) == [-1003, -1002, -1001]
        finally:
            await engine.dispose()

    async def test_returns_empty_when_no_chats(self):
        """Should return empty list when no chats exist."""
        adapter, engine = await _create_in_memory_adapter()
        try:
            result = await adapter.get_chats_with_messages()
            assert result == []
        finally:
            await engine.dispose()


# ===========================================================================
# 3. TestFillGaps — mocked Telegram client, exercises _fill_gaps control flow
# ===========================================================================


def _make_backup_instance(db_mock=None, client_mock=None, config_mock=None):
    """Create a TelegramBackup instance with mocked dependencies."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.db = db_mock or AsyncMock()
    backup.client = client_mock or AsyncMock()
    backup.config = config_mock or MagicMock()
    backup.config.gap_threshold = 50
    backup.config.batch_size = 100
    backup.config.should_skip_topic = MagicMock(return_value=False)
    return backup


class TestFillGaps:
    """Exercise _fill_gaps logic with mocked DB and Telegram client."""

    async def test_fill_gaps_no_chat_id_scans_all_chats(self):
        """When chat_id=None, _fill_gaps should query all chats from DB."""
        db = AsyncMock()
        db.get_chats_with_messages = AsyncMock(return_value=[-1001, -1002])
        db.detect_message_gaps = AsyncMock(return_value=[])

        client = AsyncMock()
        entity = MagicMock()
        entity.title = "Test Channel"
        entity.id = 1001
        client.get_entity = AsyncMock(return_value=entity)

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        result = await backup._fill_gaps(chat_id=None)

        db.get_chats_with_messages.assert_awaited_once()
        assert result["chats_scanned"] == 2

    async def test_fill_gaps_with_specific_chat_id(self):
        """When chat_id is provided, only that chat should be scanned."""
        db = AsyncMock()
        db.detect_message_gaps = AsyncMock(return_value=[])

        client = AsyncMock()
        entity = MagicMock()
        entity.title = "Specific Chat"
        entity.id = 5555
        client.get_entity = AsyncMock(return_value=entity)

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        result = await backup._fill_gaps(chat_id=-1005555)

        # Should NOT have called get_chats_with_messages
        db.get_chats_with_messages.assert_not_awaited()
        assert result["chats_scanned"] == 1
        client.get_entity.assert_awaited_once_with(-1005555)

    async def test_fill_gaps_chat_id_zero_is_not_none(self):
        """chat_id=0 is falsy but valid — must scan only chat 0, not all chats.

        This tests the critical `if chat_id is not None` fix (vs `if chat_id`).
        """
        db = AsyncMock()
        db.detect_message_gaps = AsyncMock(return_value=[])

        client = AsyncMock()
        entity = MagicMock()
        entity.title = "Chat Zero"
        entity.id = 0
        client.get_entity = AsyncMock(return_value=entity)

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        result = await backup._fill_gaps(chat_id=0)

        # The key assertion: get_chats_with_messages must NOT be called
        db.get_chats_with_messages.assert_not_awaited()
        assert result["chats_scanned"] == 1
        client.get_entity.assert_awaited_once_with(0)

    async def test_fill_gaps_skips_inaccessible_chats(self):
        """Chats raising ChannelPrivateError should be skipped, not crash."""
        from telethon.errors import ChannelPrivateError

        db = AsyncMock()
        db.get_chats_with_messages = AsyncMock(return_value=[-1001, -1002, -1003])

        accessible_entity = MagicMock()
        accessible_entity.title = "Accessible"
        accessible_entity.id = 1003

        client = AsyncMock()

        async def fake_get_entity(cid):
            if cid == -1001:
                raise ChannelPrivateError(request=None)
            if cid == -1002:
                raise ChannelPrivateError(request=None)
            return accessible_entity

        client.get_entity = AsyncMock(side_effect=fake_get_entity)
        db.detect_message_gaps = AsyncMock(return_value=[])

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        result = await backup._fill_gaps(chat_id=None)

        # All 3 scanned, but only 1 was accessible
        assert result["chats_scanned"] == 3
        # The 2 inaccessible chats had no gaps detected (skipped before gap query)
        assert result["total_gaps"] == 0

    async def test_fill_gaps_processes_detected_gaps(self):
        """When gaps are found, _fill_gap_range should be called for each."""
        db = AsyncMock()
        db.get_chats_with_messages = AsyncMock(return_value=[-1001])
        db.detect_message_gaps = AsyncMock(
            return_value=[
                (50, 100, 50),
                (200, 300, 100),
            ]
        )

        client = AsyncMock()
        entity = MagicMock()
        entity.title = "Gapped Chat"
        entity.id = 1001
        client.get_entity = AsyncMock(return_value=entity)

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        # Mock _fill_gap_range to return counts
        backup._fill_gap_range = AsyncMock(side_effect=[10, 25])

        result = await backup._fill_gaps(chat_id=None)

        assert result["chats_scanned"] == 1
        assert result["chats_with_gaps"] == 1
        assert result["total_gaps"] == 2
        assert result["total_recovered"] == 35  # 10 + 25
        assert len(result["details"]) == 1
        assert result["details"][0]["chat_id"] == -1001
        assert result["details"][0]["gaps"] == 2
        assert result["details"][0]["recovered"] == 35

    async def test_fill_gaps_chat_without_gaps_not_in_details(self):
        """Chats with no gaps should not appear in the details list."""
        db = AsyncMock()
        db.get_chats_with_messages = AsyncMock(return_value=[-1001, -1002])
        db.detect_message_gaps = AsyncMock(
            side_effect=[
                [],  # chat -1001: no gaps
                [(10, 100, 90)],  # chat -1002: one gap
            ]
        )

        client = AsyncMock()
        entity1 = MagicMock()
        entity1.title = "No Gaps"
        entity1.id = 1001
        entity2 = MagicMock()
        entity2.title = "Has Gaps"
        entity2.id = 1002

        client.get_entity = AsyncMock(side_effect=[entity1, entity2])

        backup = _make_backup_instance(db_mock=db, client_mock=client)
        backup._fill_gap_range = AsyncMock(return_value=15)

        result = await backup._fill_gaps(chat_id=None)

        assert result["chats_scanned"] == 2
        assert result["chats_with_gaps"] == 1
        assert len(result["details"]) == 1
        assert result["details"][0]["chat_id"] == -1002

    async def test_fill_gaps_uses_config_threshold(self):
        """The threshold passed to detect_message_gaps should come from config."""
        db = AsyncMock()
        db.get_chats_with_messages = AsyncMock(return_value=[-1001])
        db.detect_message_gaps = AsyncMock(return_value=[])

        client = AsyncMock()
        entity = MagicMock()
        entity.title = "Test"
        entity.id = 1001
        client.get_entity = AsyncMock(return_value=entity)

        backup = _make_backup_instance(db_mock=db, client_mock=client)
        backup.config.gap_threshold = 123

        await backup._fill_gaps(chat_id=None)

        db.detect_message_gaps.assert_awaited_once_with(-1001, 123)


class TestFillGapRange:
    """Exercise _fill_gap_range with a mocked Telegram client."""

    async def test_fill_gap_range_returns_count(self):
        """_fill_gap_range should return the total recovered message count."""
        db = AsyncMock()
        client = AsyncMock()

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        # Simulate 5 messages returned from iter_messages
        messages = []
        for i in range(51, 56):
            msg = MagicMock()
            msg.id = i
            msg.reply_to = None
            messages.append(msg)

        async def fake_iter_messages(entity, min_id=None, max_id=None, reverse=None):
            for m in messages:
                yield m

        client.iter_messages = fake_iter_messages
        backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        backup._commit_batch = AsyncMock()

        entity = MagicMock()
        count = await backup._fill_gap_range(entity, chat_id=-1001, gap_start=50, gap_end=100)

        assert count == 5
        backup._commit_batch.assert_awaited_once()

    async def test_fill_gap_range_batches_commits(self):
        """Large gaps should be committed in batches according to config.batch_size."""
        db = AsyncMock()
        client = AsyncMock()

        backup = _make_backup_instance(db_mock=db, client_mock=client)
        backup.config.batch_size = 3

        messages = []
        for i in range(51, 59):  # 8 messages
            msg = MagicMock()
            msg.id = i
            msg.reply_to = None
            messages.append(msg)

        async def fake_iter_messages(entity, min_id=None, max_id=None, reverse=None):
            for m in messages:
                yield m

        client.iter_messages = fake_iter_messages
        backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        backup._commit_batch = AsyncMock()

        entity = MagicMock()
        count = await backup._fill_gap_range(entity, chat_id=-1001, gap_start=50, gap_end=100)

        assert count == 8
        # 8 messages / batch_size 3 = 2 full batches (3+3) + 1 flush (2) = 3 calls
        assert backup._commit_batch.await_count == 3

    async def test_fill_gap_range_empty_gap(self):
        """When no messages exist in the gap range, should return 0."""
        db = AsyncMock()
        client = AsyncMock()

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        async def fake_iter_messages(entity, min_id=None, max_id=None, reverse=None):
            return
            yield  # noqa: F811 - unreachable yield makes this an async generator

        client.iter_messages = fake_iter_messages
        backup._process_message = AsyncMock()
        backup._commit_batch = AsyncMock()

        entity = MagicMock()
        count = await backup._fill_gap_range(entity, chat_id=-1001, gap_start=50, gap_end=100)

        assert count == 0
        backup._commit_batch.assert_not_awaited()

    async def test_fill_gap_range_passes_correct_ids_to_client(self):
        """iter_messages should be called with min_id=gap_start, max_id=gap_end, reverse=True."""
        db = AsyncMock()
        client = AsyncMock()

        backup = _make_backup_instance(db_mock=db, client_mock=client)

        call_kwargs = {}

        async def fake_iter_messages(entity, min_id=None, max_id=None, reverse=None):
            call_kwargs["min_id"] = min_id
            call_kwargs["max_id"] = max_id
            call_kwargs["reverse"] = reverse
            return
            yield  # noqa: F811 - unreachable yield makes this an async generator

        client.iter_messages = fake_iter_messages
        backup._process_message = AsyncMock()
        backup._commit_batch = AsyncMock()

        entity = MagicMock()
        await backup._fill_gap_range(entity, chat_id=-1001, gap_start=50, gap_end=100)

        assert call_kwargs["min_id"] == 50
        assert call_kwargs["max_id"] == 100
        assert call_kwargs["reverse"] is True


# ===========================================================================
# 4. TestConfig — env-var parsing for gap-fill settings
# ===========================================================================


class TestGapFillConfig:
    """Test FILL_GAPS and GAP_THRESHOLD configuration."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _base_env(self, **extra):
        env = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
        }
        env.update(extra)
        return env

    def test_fill_gaps_default_false(self):
        """FILL_GAPS should default to False when not set."""
        with patch.dict(os.environ, self._base_env(), clear=True):
            config = Config()
            assert config.fill_gaps is False

    def test_fill_gaps_true(self):
        """FILL_GAPS=true should set fill_gaps=True."""
        with patch.dict(os.environ, self._base_env(FILL_GAPS="true"), clear=True):
            config = Config()
            assert config.fill_gaps is True

    def test_fill_gaps_True_uppercase(self):
        """FILL_GAPS=True (capitalized) should also work."""
        with patch.dict(os.environ, self._base_env(FILL_GAPS="True"), clear=True):
            config = Config()
            assert config.fill_gaps is True

    def test_fill_gaps_false_explicit(self):
        """FILL_GAPS=false should set fill_gaps=False."""
        with patch.dict(os.environ, self._base_env(FILL_GAPS="false"), clear=True):
            config = Config()
            assert config.fill_gaps is False

    def test_fill_gaps_nonsense_is_false(self):
        """FILL_GAPS=banana should evaluate to False (only 'true' is truthy)."""
        with patch.dict(os.environ, self._base_env(FILL_GAPS="banana"), clear=True):
            config = Config()
            assert config.fill_gaps is False

    def test_gap_threshold_default(self):
        """GAP_THRESHOLD should default to 50."""
        with patch.dict(os.environ, self._base_env(), clear=True):
            config = Config()
            assert config.gap_threshold == 50

    def test_gap_threshold_custom(self):
        """GAP_THRESHOLD=100 should set gap_threshold=100."""
        with patch.dict(os.environ, self._base_env(GAP_THRESHOLD="100"), clear=True):
            config = Config()
            assert config.gap_threshold == 100

    def test_gap_threshold_small(self):
        """GAP_THRESHOLD=1 should be accepted."""
        with patch.dict(os.environ, self._base_env(GAP_THRESHOLD="1"), clear=True):
            config = Config()
            assert config.gap_threshold == 1

    def test_gap_threshold_large(self):
        """GAP_THRESHOLD=10000 should be accepted."""
        with patch.dict(os.environ, self._base_env(GAP_THRESHOLD="10000"), clear=True):
            config = Config()
            assert config.gap_threshold == 10000

    def test_both_settings_together(self):
        """FILL_GAPS and GAP_THRESHOLD can be set simultaneously."""
        with patch.dict(os.environ, self._base_env(FILL_GAPS="true", GAP_THRESHOLD="200"), clear=True):
            config = Config()
            assert config.fill_gaps is True
            assert config.gap_threshold == 200
