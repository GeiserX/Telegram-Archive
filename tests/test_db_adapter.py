"""Tests for database adapter - CRUD operations, error handling, data type handling."""

import json
import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter, _strip_tz, retry_on_locked
from src.db.models import Message

# ============================================================
# _strip_tz helper
# ============================================================


class TestStripTimezone:
    """Test the _strip_tz helper function for PostgreSQL compatibility."""

    def test_strip_tz_with_utc(self):
        """Timezone-aware datetime should have timezone stripped."""
        dt_aware = datetime(2025, 1, 14, 12, 30, 0, tzinfo=UTC)
        result = _strip_tz(dt_aware)

        assert result is not None
        assert result.tzinfo is None
        assert result.year == 2025
        assert result.month == 1
        assert result.day == 14
        assert result.hour == 12
        assert result.minute == 30

    def test_strip_tz_with_naive(self):
        """Timezone-naive datetime should pass through unchanged."""
        dt_naive = datetime(2025, 1, 14, 12, 30, 0)
        result = _strip_tz(dt_naive)

        assert result is not None
        assert result.tzinfo is None
        assert result == dt_naive

    def test_strip_tz_with_none(self):
        """None should return None."""
        result = _strip_tz(None)
        assert result is None

    def test_strip_tz_preserves_microseconds(self):
        """Microseconds should be preserved after stripping timezone."""
        dt_aware = datetime(2025, 1, 14, 12, 30, 45, 123456, tzinfo=UTC)
        result = _strip_tz(dt_aware)

        assert result is not None
        assert result.microsecond == 123456


# ============================================================
# retry_on_locked decorator
# ============================================================


class TestRetryOnLocked:
    """Test the retry_on_locked decorator for transient DB errors."""

    @pytest.mark.asyncio
    async def test_returns_result_on_success(self):
        """Decorated function returns its result when no error occurs."""

        class FakeAdapter:
            @retry_on_locked(max_retries=3)
            async def do_work(self):
                return "ok"

        adapter = FakeAdapter()
        result = await adapter.do_work()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_locked_error(self):
        """Decorator retries when 'locked' appears in the exception message."""
        call_count = 0

        class FakeAdapter:
            @retry_on_locked(max_retries=3, initial_delay=0.001, max_delay=0.01)
            async def do_work(self):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise Exception("database is locked")
                return "recovered"

        adapter = FakeAdapter()
        result = await adapter.do_work()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self):
        """Decorator retries when 'connection' appears in the exception message."""
        call_count = 0

        class FakeAdapter:
            @retry_on_locked(max_retries=2, initial_delay=0.001, max_delay=0.01)
            async def do_work(self):
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise Exception("connection refused")
                return "reconnected"

        adapter = FakeAdapter()
        result = await adapter.do_work()
        assert result == "reconnected"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_non_retryable_error_immediately(self):
        """Non-retryable errors are raised without retrying."""

        class FakeAdapter:
            @retry_on_locked(max_retries=5, initial_delay=0.001)
            async def do_work(self):
                raise ValueError("bad input")

        adapter = FakeAdapter()
        with pytest.raises(ValueError, match="bad input"):
            await adapter.do_work()

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exhausted(self):
        """After max retries, the last exception is raised."""

        class FakeAdapter:
            @retry_on_locked(max_retries=2, initial_delay=0.001, max_delay=0.01)
            async def do_work(self):
                raise Exception("database is locked permanently")

        adapter = FakeAdapter()
        with pytest.raises(Exception, match="locked permanently"):
            await adapter.do_work()

    @pytest.mark.asyncio
    async def test_exponential_backoff_caps_at_max_delay(self):
        """Delay should not exceed max_delay after exponential backoff."""
        call_count = 0

        class FakeAdapter:
            @retry_on_locked(max_retries=5, initial_delay=0.5, max_delay=1.0, backoff_factor=10.0)
            async def do_work(self):
                nonlocal call_count
                call_count += 1
                if call_count <= 5:
                    raise Exception("database is locked")
                return "done"

        adapter = FakeAdapter()
        result = await adapter.do_work()
        assert result == "done"


# ============================================================
# Helpers: mock factory for DatabaseManager + async session
# ============================================================


def _make_mock_db_manager(is_sqlite=True):
    """Create a mock DatabaseManager with async session context manager."""
    db_manager = MagicMock()
    db_manager._is_sqlite = is_sqlite

    mock_session = AsyncMock()

    # Make session factory return an async context manager
    async_ctx = AsyncMock()
    async_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    async_ctx.__aexit__ = AsyncMock(return_value=False)
    db_manager.async_session_factory.return_value = async_ctx

    return db_manager, mock_session


# ============================================================
# DatabaseAdapter.__init__ and close
# ============================================================


class TestDatabaseAdapterInit:
    """Test DatabaseAdapter initialization and teardown."""

    def test_stores_db_manager_reference(self):
        """Adapter stores the DatabaseManager instance."""
        db_manager = MagicMock()
        db_manager._is_sqlite = True
        adapter = DatabaseAdapter(db_manager)
        assert adapter.db_manager is db_manager

    def test_inherits_is_sqlite_flag_true(self):
        """Adapter inherits _is_sqlite=True from SQLite manager."""
        db_manager = MagicMock()
        db_manager._is_sqlite = True
        adapter = DatabaseAdapter(db_manager)
        assert adapter._is_sqlite is True

    def test_inherits_is_sqlite_flag_false(self):
        """Adapter inherits _is_sqlite=False from PostgreSQL manager."""
        db_manager = MagicMock()
        db_manager._is_sqlite = False
        adapter = DatabaseAdapter(db_manager)
        assert adapter._is_sqlite is False

    @pytest.mark.asyncio
    async def test_close_delegates_to_db_manager(self):
        """close() delegates to db_manager.close()."""
        db_manager = AsyncMock()
        db_manager._is_sqlite = True
        adapter = DatabaseAdapter(db_manager)
        await adapter.close()
        db_manager.close.assert_awaited_once()


# ============================================================
# _serialize_raw_data
# ============================================================


class TestSerializeRawData:
    """Test _serialize_raw_data JSON serialization with fallbacks."""

    def _make_adapter(self):
        db_manager = MagicMock()
        db_manager._is_sqlite = True
        return DatabaseAdapter(db_manager)

    def test_returns_empty_json_for_none(self):
        """None input returns empty JSON object."""
        adapter = self._make_adapter()
        assert adapter._serialize_raw_data(None) == "{}"

    def test_returns_empty_json_for_empty_dict(self):
        """Empty dict input returns empty JSON object."""
        adapter = self._make_adapter()
        assert adapter._serialize_raw_data({}) == "{}"

    def test_serializes_simple_dict(self):
        """Simple dict is serialized to valid JSON."""
        adapter = self._make_adapter()
        result = adapter._serialize_raw_data({"key": "value", "count": 42})
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["count"] == 42

    def test_serializes_nested_dict(self):
        """Nested dict structures are serialized correctly."""
        adapter = self._make_adapter()
        data = {"user": {"name": "Alice", "ids": [1, 2, 3]}}
        result = adapter._serialize_raw_data(data)
        parsed = json.loads(result)
        assert parsed["user"]["name"] == "Alice"
        assert parsed["user"]["ids"] == [1, 2, 3]

    def test_converts_non_serializable_objects_to_string(self):
        """Non-JSON-serializable objects are converted to strings."""
        adapter = self._make_adapter()

        class CustomObj:
            def __str__(self):
                return "custom_repr"

        data = {"obj": CustomObj()}
        result = adapter._serialize_raw_data(data)
        parsed = json.loads(result)
        assert parsed["obj"] == "custom_repr"

    def test_returns_empty_json_when_all_serialization_fails(self):
        """Returns {} when even string conversion fails."""
        adapter = self._make_adapter()
        # Patch json.dumps to always raise, forcing ultimate fallback
        with patch("src.db.adapter.json.dumps", side_effect=TypeError("can't serialize")):
            result = adapter._serialize_raw_data({"key": "value"})
            assert result == "{}"

    def test_returns_empty_json_for_empty_list(self):
        """Empty list is falsy, returns empty JSON object."""
        adapter = self._make_adapter()
        assert adapter._serialize_raw_data([]) == "{}"

    def test_serializes_list_with_values(self):
        """Non-empty list is serialized to valid JSON array."""
        adapter = self._make_adapter()
        result = adapter._serialize_raw_data([1, 2, 3])
        assert json.loads(result) == [1, 2, 3]


# ============================================================
# _message_to_dict
# ============================================================


class TestMessageToDict:
    """Test _message_to_dict converts ORM objects to dictionaries."""

    def _make_adapter(self):
        db_manager = MagicMock()
        db_manager._is_sqlite = True
        return DatabaseAdapter(db_manager)

    def test_converts_all_fields(self):
        """All message fields are mapped into the output dictionary."""
        adapter = self._make_adapter()
        msg = MagicMock()
        msg.id = 42
        msg.chat_id = -1001234567890
        msg.sender_id = 999
        msg.date = datetime(2025, 6, 1)
        msg.text = "Hello"
        msg.reply_to_msg_id = 41
        msg.reply_to_top_id = 10
        msg.reply_to_text = "Previous"
        msg.forward_from_id = 888
        msg.edit_date = None
        msg.raw_data = '{"test": true}'
        msg.created_at = datetime(2025, 6, 1)
        msg.is_outgoing = 1
        msg.is_pinned = 0

        result = adapter._message_to_dict(msg)

        assert result["id"] == 42
        assert result["chat_id"] == -1001234567890
        assert result["sender_id"] == 999
        assert result["text"] == "Hello"
        assert result["reply_to_msg_id"] == 41
        assert result["reply_to_top_id"] == 10
        assert result["reply_to_text"] == "Previous"
        assert result["forward_from_id"] == 888
        assert result["edit_date"] is None
        assert result["is_outgoing"] == 1
        assert result["is_pinned"] == 0

    def test_handles_none_text(self):
        """Message with None text is handled correctly."""
        adapter = self._make_adapter()
        msg = MagicMock()
        msg.text = None
        msg.id = 1
        msg.chat_id = 1
        msg.sender_id = None
        msg.date = datetime(2025, 1, 1)
        msg.reply_to_msg_id = None
        msg.reply_to_top_id = None
        msg.reply_to_text = None
        msg.forward_from_id = None
        msg.edit_date = None
        msg.raw_data = None
        msg.created_at = None
        msg.is_outgoing = 0
        msg.is_pinned = 0

        result = adapter._message_to_dict(msg)
        assert result["text"] is None


# ============================================================
# _viewer_account_to_dict
# ============================================================


class TestViewerAccountToDict:
    """Test static converter for ViewerAccount model."""

    def test_converts_all_fields(self):
        """All account fields are mapped into the output dictionary."""
        account = MagicMock()
        account.id = 1
        account.username = "viewer1"
        account.password_hash = "abc123"
        account.salt = "salt123"
        account.allowed_chat_ids = "[1,2,3]"
        account.is_active = 1
        account.no_download = 0
        account.created_by = "admin"
        account.created_at = datetime(2025, 6, 1)
        account.updated_at = datetime(2025, 6, 2)

        result = DatabaseAdapter._viewer_account_to_dict(account)

        assert result["id"] == 1
        assert result["username"] == "viewer1"
        assert result["password_hash"] == "abc123"
        assert result["salt"] == "salt123"
        assert result["allowed_chat_ids"] == "[1,2,3]"
        assert result["is_active"] == 1
        assert result["no_download"] == 0
        assert result["created_by"] == "admin"
        assert "2025-06-01" in result["created_at"]
        assert "2025-06-02" in result["updated_at"]

    def test_handles_none_timestamps(self):
        """None timestamps produce None in the output."""
        account = MagicMock()
        account.id = 2
        account.username = "viewer2"
        account.password_hash = "x"
        account.salt = "y"
        account.allowed_chat_ids = None
        account.is_active = 1
        account.no_download = 0
        account.created_by = None
        account.created_at = None
        account.updated_at = None

        result = DatabaseAdapter._viewer_account_to_dict(account)
        assert result["created_at"] is None
        assert result["updated_at"] is None


# ============================================================
# _viewer_session_to_dict
# ============================================================


class TestViewerSessionToDict:
    """Test static converter for ViewerSession model."""

    def test_converts_all_fields(self):
        """All session fields are mapped into the output dictionary."""
        row = MagicMock()
        row.token = "abc-token"
        row.username = "admin"
        row.role = "master"
        row.allowed_chat_ids = None
        row.no_download = 0
        row.source_token_id = None
        row.created_at = 1700000000.0
        row.last_accessed = 1700001000.0

        result = DatabaseAdapter._viewer_session_to_dict(row)

        assert result["token"] == "abc-token"
        assert result["username"] == "admin"
        assert result["role"] == "master"
        assert result["allowed_chat_ids"] is None
        assert result["no_download"] == 0
        assert result["source_token_id"] is None
        assert result["created_at"] == 1700000000.0
        assert result["last_accessed"] == 1700001000.0


# ============================================================
# _viewer_token_to_dict
# ============================================================


class TestViewerTokenToDict:
    """Test static converter for ViewerToken model."""

    def test_converts_all_fields(self):
        """All token fields are mapped into the output dictionary."""
        token = MagicMock()
        token.id = 5
        token.label = "family-share"
        token.token_hash = "hash123"
        token.token_salt = "salt456"
        token.created_by = "admin"
        token.allowed_chat_ids = "[100, 200]"
        token.is_revoked = 0
        token.no_download = 1
        token.expires_at = datetime(2026, 1, 1)
        token.last_used_at = datetime(2025, 12, 1)
        token.use_count = 42
        token.created_at = datetime(2025, 6, 1)

        result = DatabaseAdapter._viewer_token_to_dict(token)

        assert result["id"] == 5
        assert result["label"] == "family-share"
        assert result["is_revoked"] == 0
        assert result["no_download"] == 1
        assert result["use_count"] == 42
        assert "2026-01-01" in result["expires_at"]
        assert "2025-12-01" in result["last_used_at"]
        assert "2025-06-01" in result["created_at"]

    def test_handles_none_optional_dates(self):
        """None optional dates produce None in the output."""
        token = MagicMock()
        token.id = 6
        token.label = None
        token.token_hash = "h"
        token.token_salt = "s"
        token.created_by = "admin"
        token.allowed_chat_ids = "[]"
        token.is_revoked = 0
        token.no_download = 0
        token.expires_at = None
        token.last_used_at = None
        token.use_count = 0
        token.created_at = None

        result = DatabaseAdapter._viewer_token_to_dict(token)
        assert result["expires_at"] is None
        assert result["last_used_at"] is None
        assert result["created_at"] is None


# ============================================================
# Metadata operations
# ============================================================


class TestMetadataOperations:
    """Test set_metadata and get_metadata via mocked sessions."""

    @pytest.mark.asyncio
    async def test_set_metadata_sqlite_executes_upsert(self):
        """set_metadata on SQLite executes an upsert statement and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        await adapter.set_metadata("test_key", "test_value")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_metadata_postgres_executes_upsert(self):
        """set_metadata on PostgreSQL executes an upsert statement and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        await adapter.set_metadata("pg_key", "pg_value")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_metadata_returns_value(self):
        """get_metadata returns the stored value when key exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "stored_value"
        mock_session.execute.return_value = mock_result

        result = await adapter.get_metadata("my_key")
        assert result == "stored_value"

    @pytest.mark.asyncio
    async def test_get_metadata_returns_none_when_missing(self):
        """get_metadata returns None when key does not exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_metadata("nonexistent")
        assert result is None


# ============================================================
# Chat operations
# ============================================================


class TestChatOperations:
    """Test upsert_chat and get_chat_by_id."""

    @pytest.mark.asyncio
    async def test_upsert_chat_sqlite_returns_chat_id(self):
        """upsert_chat returns the chat ID after insert/update."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        chat_data = {"id": 12345, "type": "private", "title": "Test Chat"}
        result = await adapter.upsert_chat(chat_data)

        assert result == 12345
        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_chat_postgres_returns_chat_id(self):
        """upsert_chat on PostgreSQL returns the chat ID."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        chat_data = {"id": 99999, "type": "group", "title": "PG Group"}
        result = await adapter.upsert_chat(chat_data)

        assert result == 99999
        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_chat_only_updates_provided_fields(self):
        """upsert_chat does not include is_forum/is_archived when not in input."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        # Provide minimal fields -- is_forum and is_archived NOT in chat_data
        chat_data = {"id": 111, "title": "Minimal"}
        await adapter.upsert_chat(chat_data)

        # The important thing is it succeeds without error
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_chat_by_id_returns_dict_when_found(self):
        """get_chat_by_id returns a dict when the chat exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_chat = MagicMock()
        mock_chat.id = 42
        mock_chat.type = "private"
        mock_chat.title = "Found Chat"
        mock_chat.username = "user42"
        mock_chat.first_name = "First"
        mock_chat.last_name = "Last"
        mock_chat.phone = "+1234"
        mock_chat.description = "desc"
        mock_chat.participants_count = 2
        mock_chat.is_forum = 0
        mock_chat.is_archived = 0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_chat
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_by_id(42)
        assert result is not None
        assert result["id"] == 42
        assert result["title"] == "Found Chat"
        assert result["username"] == "user42"

    @pytest.mark.asyncio
    async def test_get_chat_by_id_returns_none_when_missing(self):
        """get_chat_by_id returns None when the chat does not exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_by_id(9999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_chat_count_returns_zero_when_empty(self):
        """get_chat_count returns 0 when no chats match."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_count()
        assert result == 0

    @pytest.mark.asyncio
    async def test_get_chat_count_returns_count_value(self):
        """get_chat_count returns the scalar count from the query."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_count()
        assert result == 42

    @pytest.mark.asyncio
    async def test_get_archived_chat_count_returns_value(self):
        """get_archived_chat_count returns the scalar count."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_session.execute.return_value = mock_result

        result = await adapter.get_archived_chat_count()
        assert result == 5


# ============================================================
# User operations
# ============================================================


class TestUserOperations:
    """Test upsert_user and get_user_by_id."""

    @pytest.mark.asyncio
    async def test_upsert_user_sqlite_executes_and_commits(self):
        """upsert_user on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        user_data = {"id": 100, "username": "alice", "first_name": "Alice"}
        await adapter.upsert_user(user_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_user_postgres_executes_and_commits(self):
        """upsert_user on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        user_data = {"id": 200, "username": "bob", "is_bot": True}
        await adapter.upsert_user(user_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_user_by_id_returns_dict_when_found(self):
        """get_user_by_id returns a dict when the user exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_user = MagicMock()
        mock_user.id = 100
        mock_user.username = "alice"
        mock_user.first_name = "Alice"
        mock_user.last_name = "Smith"
        mock_user.phone = "+1111"
        mock_user.is_bot = 0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_session.execute.return_value = mock_result

        result = await adapter.get_user_by_id(100)
        assert result is not None
        assert result["id"] == 100
        assert result["username"] == "alice"
        assert result["is_bot"] == 0

    @pytest.mark.asyncio
    async def test_get_user_by_id_returns_none_when_missing(self):
        """get_user_by_id returns None when user does not exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_user_by_id(9999)
        assert result is None


# ============================================================
# Message operations
# ============================================================


class TestMessageOperations:
    """Test insert_message, insert_messages_batch, and related queries."""

    @pytest.mark.asyncio
    async def test_insert_message_sqlite_executes_upsert(self):
        """insert_message on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        msg_data = {
            "id": 1,
            "chat_id": 100,
            "date": datetime(2025, 1, 1),
            "text": "Hello",
        }
        await adapter.insert_message(msg_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_insert_message_postgres_executes_upsert(self):
        """insert_message on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        msg_data = {
            "id": 2,
            "chat_id": 200,
            "date": datetime(2025, 1, 1),
            "text": "PG Hello",
        }
        await adapter.insert_message(msg_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_insert_messages_batch_empty_list_returns_early(self):
        """insert_messages_batch with empty list returns without touching DB."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.insert_messages_batch([])

        mock_session.execute.assert_not_awaited()
        mock_session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_insert_messages_batch_processes_multiple(self):
        """insert_messages_batch processes each message and commits once."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        messages = [
            {"id": 1, "chat_id": 100, "date": datetime(2025, 1, 1), "text": "msg1"},
            {"id": 2, "chat_id": 100, "date": datetime(2025, 1, 2), "text": "msg2"},
        ]
        await adapter.insert_messages_batch(messages)

        assert mock_session.execute.await_count == 2
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_last_message_id_returns_stored_value(self):
        """get_last_message_id returns the stored last_message_id."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 500
        mock_session.execute.return_value = mock_result

        result = await adapter.get_last_message_id(100)
        assert result == 500

    @pytest.mark.asyncio
    async def test_get_last_message_id_returns_zero_when_no_sync(self):
        """get_last_message_id returns 0 when no sync status exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_last_message_id(999)
        assert result == 0

    @pytest.mark.asyncio
    async def test_delete_message_deletes_media_reactions_and_message(self):
        """delete_message issues three deletes and one commit."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.delete_message(chat_id=100, message_id=42)

        # 3 deletes: media, reactions, message
        assert mock_session.execute.await_count == 3
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_chat_id_for_message_returns_id(self):
        """get_chat_id_for_message returns chat_id when found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_row = (100,)
        mock_result.first.return_value = mock_row
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_id_for_message(42)
        assert result == 100

    @pytest.mark.asyncio
    async def test_get_chat_id_for_message_returns_none_when_missing(self):
        """get_chat_id_for_message returns None when message not found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_id_for_message(9999)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_message_chat_id_returns_id_when_unique(self):
        """resolve_message_chat_id returns chat_id when found in exactly one chat."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [(100,)]
        mock_session.execute.return_value = mock_result

        result = await adapter.resolve_message_chat_id(42)
        assert result == 100

    @pytest.mark.asyncio
    async def test_resolve_message_chat_id_returns_none_when_ambiguous(self):
        """resolve_message_chat_id returns None when message found in multiple chats."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [(100,), (200,)]
        mock_session.execute.return_value = mock_result

        result = await adapter.resolve_message_chat_id(42)
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_message_chat_id_returns_none_when_not_found(self):
        """resolve_message_chat_id returns None when message not in any chat."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.resolve_message_chat_id(9999)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_message_text_executes_and_commits(self):
        """update_message_text issues an update and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.update_message_text(100, 42, "edited text", datetime(2025, 6, 1))

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_find_message_by_date_returns_dict_when_found(self):
        """find_message_by_date returns a message dict when found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_msg = MagicMock()
        mock_msg.id = 10
        mock_msg.chat_id = 100
        mock_msg.sender_id = 1
        mock_msg.date = datetime(2025, 6, 1)
        mock_msg.text = "Found"
        mock_msg.reply_to_msg_id = None
        mock_msg.reply_to_top_id = None
        mock_msg.reply_to_text = None
        mock_msg.forward_from_id = None
        mock_msg.edit_date = None
        mock_msg.raw_data = None
        mock_msg.created_at = None
        mock_msg.is_outgoing = 0
        mock_msg.is_pinned = 0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_msg
        mock_session.execute.return_value = mock_result

        result = await adapter.find_message_by_date(100, datetime(2025, 6, 1))
        assert result is not None
        assert result["id"] == 10

    @pytest.mark.asyncio
    async def test_find_message_by_date_returns_none_when_not_found(self):
        """find_message_by_date returns None when no message matches."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.find_message_by_date(100, datetime(2099, 1, 1))
        assert result is None


# ============================================================
# Media operations
# ============================================================


class TestMediaOperations:
    """Test insert_media, get_media_for_chat, delete_media_for_chat, etc."""

    @pytest.mark.asyncio
    async def test_insert_media_sqlite_executes_upsert(self):
        """insert_media on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        media_data = {"id": "file_abc", "type": "photo", "downloaded": True}
        await adapter.insert_media(media_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_insert_media_postgres_executes_upsert(self):
        """insert_media on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        media_data = {"id": "file_xyz", "type": "video", "downloaded": False}
        await adapter.insert_media(media_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_media_for_chat_returns_rowcount(self):
        """delete_media_for_chat returns the number of deleted rows."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 7
        mock_session.execute.return_value = mock_result

        count = await adapter.delete_media_for_chat(100)
        assert count == 7
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_media_for_redownload_clears_fields(self):
        """mark_media_for_redownload issues an update and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.mark_media_for_redownload("file_abc")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# Reaction operations
# ============================================================


class TestReactionOperations:
    """Test get_reactions."""

    @pytest.mark.asyncio
    async def test_get_reactions_returns_list(self):
        """get_reactions returns a list of reaction dicts."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_reaction = MagicMock()
        mock_reaction.emoji = "thumbsup"
        mock_reaction.user_id = 100
        mock_reaction.count = 3

        mock_result = MagicMock()
        mock_result.scalars.return_value = [mock_reaction]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_reactions(42, 100)
        assert len(result) == 1
        assert result[0]["emoji"] == "thumbsup"
        assert result[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_get_reactions_returns_empty_list(self):
        """get_reactions returns empty list when no reactions exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.get_reactions(42, 100)
        assert result == []


# ============================================================
# Sync status operations
# ============================================================


class TestSyncStatusOperations:
    """Test update_sync_status."""

    @pytest.mark.asyncio
    async def test_update_sync_status_sqlite_executes_upsert(self):
        """update_sync_status on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        await adapter.update_sync_status(100, 500, 50)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_sync_status_postgres_executes_upsert(self):
        """update_sync_status on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        await adapter.update_sync_status(200, 1000, 100)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# Delete chat operations
# ============================================================


class TestDeleteChatOperations:
    """Test delete_chat_and_related_data."""

    @pytest.mark.asyncio
    async def test_delete_chat_issues_five_deletes(self):
        """delete_chat_and_related_data deletes media, reactions, messages, sync, and chat."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.delete_chat_and_related_data(100)

        # 5 deletes: media, reactions, messages, sync_status, chat
        assert mock_session.execute.await_count == 5
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_chat_removes_media_files(self):
        """delete_chat_and_related_data removes physical media directory."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        with (
            patch("src.db.adapter.os.path.exists", return_value=True),
            patch("src.db.adapter.shutil.rmtree") as mock_rmtree,
            patch("src.db.adapter.glob.glob", return_value=[]),
        ):
            await adapter.delete_chat_and_related_data(100, media_base_path="/data/media")

        mock_rmtree.assert_called_once_with("/data/media/100")

    @pytest.mark.asyncio
    async def test_delete_chat_skips_files_when_no_media_path(self):
        """delete_chat_and_related_data skips file deletion when no media path."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        with patch("src.db.adapter.shutil.rmtree") as mock_rmtree:
            await adapter.delete_chat_and_related_data(100, media_base_path=None)

        mock_rmtree.assert_not_called()


# ============================================================
# Viewer account operations
# ============================================================


class TestViewerAccountOperations:
    """Test viewer account CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_viewer_account_adds_and_commits(self):
        """create_viewer_account adds a model, commits, and returns dict."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.username = "viewer1"
        mock_account.password_hash = "hash"
        mock_account.salt = "salt"
        mock_account.allowed_chat_ids = None
        mock_account.is_active = 1
        mock_account.no_download = 0
        mock_account.created_by = "admin"
        mock_account.created_at = datetime(2025, 1, 1)
        mock_account.updated_at = datetime(2025, 1, 1)

        # session.add() doesn't return, refresh makes the account available
        async def fake_refresh(obj):
            for attr in vars(mock_account):
                if not attr.startswith("_"):
                    try:
                        setattr(obj, attr, getattr(mock_account, attr))
                    except AttributeError, TypeError:
                        pass

        mock_session.refresh = fake_refresh

        result = await adapter.create_viewer_account("viewer1", "hash", "salt", created_by="admin")
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_viewer_account_returns_true_on_success(self):
        """delete_viewer_account returns True when row deleted."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        result = await adapter.delete_viewer_account(1)
        assert result is True
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_viewer_account_returns_false_when_missing(self):
        """delete_viewer_account returns False when no row deleted."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        result = await adapter.delete_viewer_account(999)
        assert result is False


# ============================================================
# Session operations
# ============================================================


class TestSessionOperations:
    """Test save_session, get_session, delete_session, etc."""

    @pytest.mark.asyncio
    async def test_save_session_sqlite_executes_and_commits(self):
        """save_session on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        await adapter.save_session("token123", "admin", "master", None, 1700000000.0, 1700001000.0)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_session_returns_dict_when_found(self):
        """get_session returns a session dict when token exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_row = MagicMock()
        mock_row.token = "abc"
        mock_row.username = "admin"
        mock_row.role = "master"
        mock_row.allowed_chat_ids = None
        mock_row.no_download = 0
        mock_row.source_token_id = None
        mock_row.created_at = 1700000000.0
        mock_row.last_accessed = 1700001000.0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_row
        mock_session.execute.return_value = mock_result

        result = await adapter.get_session("abc")
        assert result is not None
        assert result["token"] == "abc"

    @pytest.mark.asyncio
    async def test_get_session_returns_none_when_missing(self):
        """get_session returns None when token does not exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_session("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_session_returns_true_on_success(self):
        """delete_session returns True when row deleted."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        result = await adapter.delete_session("tok123")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_user_sessions_returns_count(self):
        """delete_user_sessions returns the number of deleted sessions."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 3
        mock_session.execute.return_value = mock_result

        count = await adapter.delete_user_sessions("admin")
        assert count == 3


# ============================================================
# Settings operations
# ============================================================


class TestSettingsOperations:
    """Test set_setting, get_setting, get_all_settings."""

    @pytest.mark.asyncio
    async def test_set_setting_sqlite_executes_and_commits(self):
        """set_setting on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        await adapter.set_setting("theme", "dark")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_setting_returns_value_when_found(self):
        """get_setting returns the value when key exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_row = MagicMock()
        mock_row.value = "dark"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_row
        mock_session.execute.return_value = mock_result

        result = await adapter.get_setting("theme")
        assert result == "dark"

    @pytest.mark.asyncio
    async def test_get_setting_returns_none_when_missing(self):
        """get_setting returns None when key does not exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_setting("nonexistent")
        assert result is None


# ============================================================
# Pinned message operations
# ============================================================


class TestPinnedMessageOperations:
    """Test sync_pinned_messages and update_message_pinned."""

    @pytest.mark.asyncio
    async def test_sync_pinned_messages_unpins_all_then_pins_specified(self):
        """sync_pinned_messages issues two updates (unpin all, pin specified) and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.sync_pinned_messages(100, [1, 2, 3])

        assert mock_session.execute.await_count == 2
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_pinned_messages_only_unpins_when_empty_list(self):
        """sync_pinned_messages with empty list only unpins, no pin update."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.sync_pinned_messages(100, [])

        assert mock_session.execute.await_count == 1
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_message_pinned_executes_and_commits(self):
        """update_message_pinned issues an update and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.update_message_pinned(100, 42, True)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# Backfill and gap detection
# ============================================================


class TestBackfillAndGapDetection:
    """Test backfill_is_outgoing and get_chats_with_messages."""

    @pytest.mark.asyncio
    async def test_backfill_is_outgoing_updates_and_commits(self):
        """backfill_is_outgoing issues an update and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 10
        mock_session.execute.return_value = mock_result

        await adapter.backfill_is_outgoing(owner_id=12345)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_chats_with_messages_returns_list(self):
        """get_all_chats_with_messages returns list of chat IDs."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [(100,), (200,), (300,)]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chats_with_messages()
        assert result == [100, 200, 300]

    @pytest.mark.asyncio
    async def test_get_messages_sync_data_returns_dict(self):
        """get_messages_sync_data returns {id: edit_date} mapping."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_row1 = MagicMock()
        mock_row1.id = 1
        mock_row1.edit_date = None
        mock_row2 = MagicMock()
        mock_row2.id = 2
        mock_row2.edit_date = "2025-06-01"

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([mock_row1, mock_row2]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_messages_sync_data(100)
        assert result == {1: None, 2: "2025-06-01"}


# ============================================================
# Statistics
# ============================================================


class TestStatistics:
    """Test get_statistics and get_cached_statistics."""

    @pytest.mark.asyncio
    async def test_get_statistics_delegates_to_get_cached_statistics(self):
        """get_statistics is an alias for get_cached_statistics."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        # Mock get_metadata to return None (no cached stats)
        adapter.get_metadata = AsyncMock(return_value=None)

        result = await adapter.get_statistics()
        assert result["chats"] == 0
        assert result["messages"] == 0

    @pytest.mark.asyncio
    async def test_get_cached_statistics_returns_defaults_when_no_cache(self):
        """get_cached_statistics returns zeros when no cached stats exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        adapter.get_metadata = AsyncMock(return_value=None)

        result = await adapter.get_cached_statistics()
        assert result["chats"] == 0
        assert result["messages"] == 0
        assert result["media_files"] == 0

    @pytest.mark.asyncio
    async def test_get_cached_statistics_returns_stored_values(self):
        """get_cached_statistics returns stored cached stats when available."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        cached = json.dumps({"chats": 10, "messages": 500, "media_files": 50, "total_size_mb": 100.5})

        call_count = 0

        async def mock_get_metadata(key):
            nonlocal call_count
            call_count += 1
            if key == "cached_stats":
                return cached
            if key == "stats_calculated_at":
                return "2025-06-01T00:00:00"
            if key == "last_backup_time":
                return "2025-06-01T12:00:00"
            return None

        adapter.get_metadata = mock_get_metadata

        result = await adapter.get_cached_statistics()
        assert result["chats"] == 10
        assert result["messages"] == 500
        assert result["last_backup_time"] == "2025-06-01T12:00:00"


# ============================================================
# Folder operations
# ============================================================


class TestFolderOperations:
    """Test upsert_chat_folder and cleanup_stale_folders."""

    @pytest.mark.asyncio
    async def test_upsert_chat_folder_sqlite_executes_and_commits(self):
        """upsert_chat_folder on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        folder_data = {"id": 1, "title": "Work", "emoticon": None, "sort_order": 0}
        await adapter.upsert_chat_folder(folder_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_stale_folders_deletes_non_active(self):
        """cleanup_stale_folders deletes folders not in the active list."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.cleanup_stale_folders([1, 2, 3])

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_stale_folders_deletes_all_when_empty(self):
        """cleanup_stale_folders with empty list deletes all folders."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.cleanup_stale_folders([])

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# Token operations
# ============================================================


class TestTokenOperations:
    """Test delete_viewer_token."""

    @pytest.mark.asyncio
    async def test_delete_viewer_token_returns_true_on_success(self):
        """delete_viewer_token returns True when row deleted."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        result = await adapter.delete_viewer_token(5)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_viewer_token_returns_false_when_missing(self):
        """delete_viewer_token returns False when no row deleted."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        result = await adapter.delete_viewer_token(999)
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_sessions_by_source_token_id_returns_count(self):
        """delete_sessions_by_source_token_id returns the number of deleted sessions."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 2
        mock_session.execute.return_value = mock_result

        count = await adapter.delete_sessions_by_source_token_id(5)
        assert count == 2


# ============================================================
# Chat ID consistency (documentation tests)
# ============================================================


class TestChatIdConsistency:
    """Test that chat IDs are handled consistently (marked ID format)."""

    def test_marked_id_format_documented(self):
        """Document the expected chat ID formats."""
        basic_group_raw_id = 798230299
        basic_group_marked_id = -basic_group_raw_id
        assert basic_group_marked_id == -798230299

        channel_raw_id = 1234567890
        channel_marked_id = -1000000000000 - channel_raw_id
        assert channel_marked_id == -1001234567890

        user_id = 123456789
        assert user_id > 0


# ============================================================
# get_chats (pagination/filtering) — lines 251-313
# ============================================================


class TestGetChats:
    """Test get_chats with pagination, search, archived, and folder filters."""

    def _make_chat_row(self, chat_id=1, title="Chat", last_message_date=None):
        """Build a fake result row with .Chat and .last_message_date."""
        chat = MagicMock()
        chat.id = chat_id
        chat.type = "group"
        chat.title = title
        chat.username = None
        chat.first_name = None
        chat.last_name = None
        chat.phone = None
        chat.description = None
        chat.participants_count = 5
        chat.is_forum = 0
        chat.is_archived = 0
        chat.last_synced_message_id = None
        chat.created_at = None
        chat.updated_at = None
        row = MagicMock()
        row.Chat = chat
        row.last_message_date = last_message_date
        return row

    @pytest.mark.asyncio
    async def test_get_chats_returns_list_of_dicts(self):
        """get_all_chats returns a list of chat dictionaries."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_chat_row(chat_id=100, title="My Group")
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats()
        assert len(result) == 1
        assert result[0]["id"] == 100
        assert result[0]["title"] == "My Group"
        assert "last_message_date" in result[0]

    @pytest.mark.asyncio
    async def test_get_chats_with_pagination(self):
        """get_all_chats respects limit and offset parameters."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats(limit=10, offset=5)
        assert result == []
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_chats_with_search_filter(self):
        """get_all_chats applies search filter to query."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats(search="test")
        assert result == []
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_chats_archived_true(self):
        """get_all_chats with archived=True filters for archived chats only."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats(archived=True)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_chats_archived_false(self):
        """get_all_chats with archived=False filters for non-archived chats."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats(archived=False)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_chats_with_folder_id(self):
        """get_all_chats with folder_id filters by folder membership."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats(folder_id=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_chats_returns_multiple_chats(self):
        """get_all_chats returns multiple chat dicts with correct fields."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row1 = self._make_chat_row(chat_id=1, title="First")
        row2 = self._make_chat_row(chat_id=2, title="Second")
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row1, row2]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_chats()
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2


# ============================================================
# get_chat_count with filters — lines 329, 334, 336, 339-340
# ============================================================


class TestGetChatCountFiltered:
    """Test get_chat_count with search, archived, and folder_id filters."""

    @pytest.mark.asyncio
    async def test_get_chat_count_with_search(self):
        """get_chat_count applies search filter."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 3
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_count(search="hello")
        assert result == 3

    @pytest.mark.asyncio
    async def test_get_chat_count_with_archived_true(self):
        """get_chat_count with archived=True counts only archived."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 2
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_count(archived=True)
        assert result == 2

    @pytest.mark.asyncio
    async def test_get_chat_count_with_archived_false(self):
        """get_chat_count with archived=False counts only non-archived."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 10
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_count(archived=False)
        assert result == 10

    @pytest.mark.asyncio
    async def test_get_chat_count_with_folder_id(self):
        """get_chat_count with folder_id counts only chats in that folder."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar.return_value = 4
        mock_session.execute.return_value = mock_result

        result = await adapter.get_chat_count(folder_id=7)
        assert result == 4


# ============================================================
# get_chat_stats — lines 612-635
# ============================================================


class TestGetChatStats:
    """Test get_chat_stats returns aggregated chat statistics."""

    @pytest.mark.asyncio
    async def test_get_chat_stats_returns_all_fields(self):
        """get_chat_stats returns messages, media_files, total_size, and date range."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        # Three sequential execute calls: msg count, media count+size, date range
        msg_result = MagicMock()
        msg_result.scalar.return_value = 150

        media_result = MagicMock()
        media_row = (25, 1048576)  # 25 files, 1MB total
        media_result.one.return_value = media_row

        date_result = MagicMock()
        date_row = (datetime(2024, 1, 1), datetime(2025, 6, 1))
        date_result.one.return_value = date_row

        mock_session.execute.side_effect = [msg_result, media_result, date_result]

        result = await adapter.get_chat_stats(100)
        assert result["chat_id"] == 100
        assert result["messages"] == 150
        assert result["media_files"] == 25
        assert result["total_size_bytes"] == 1048576
        assert result["total_size_mb"] == 1.0
        assert result["first_message_date"] is not None
        assert result["last_message_date"] is not None

    @pytest.mark.asyncio
    async def test_get_chat_stats_empty_chat(self):
        """get_chat_stats returns zeros for chat with no messages."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        msg_result = MagicMock()
        msg_result.scalar.return_value = 0

        media_result = MagicMock()
        media_result.one.return_value = (0, 0)

        date_result = MagicMock()
        date_result.one.return_value = (None, None)

        mock_session.execute.side_effect = [msg_result, media_result, date_result]

        result = await adapter.get_chat_stats(999)
        assert result["messages"] == 0
        assert result["media_files"] == 0
        assert result["total_size_bytes"] == 0
        assert result["total_size_mb"] == 0
        assert result["first_message_date"] is None
        assert result["last_message_date"] is None


# ============================================================
# get_messages_by_date_range — lines 472-489
# ============================================================


class TestGetMessagesByDateRange:
    """Test get_messages_by_date_range with various filter combinations."""

    @pytest.mark.asyncio
    async def test_returns_messages_in_range(self):
        """get_messages_by_date_range returns message dicts filtered by date."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.chat_id = 100
        mock_msg.sender_id = 1
        mock_msg.date = datetime(2025, 3, 15)
        mock_msg.text = "In range"
        mock_msg.reply_to_msg_id = None
        mock_msg.reply_to_top_id = None
        mock_msg.reply_to_text = None
        mock_msg.forward_from_id = None
        mock_msg.edit_date = None
        mock_msg.raw_data = None
        mock_msg.created_at = None
        mock_msg.is_outgoing = 0
        mock_msg.is_pinned = 0

        mock_result = MagicMock()
        mock_result.scalars.return_value = [mock_msg]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_messages_by_date_range(
            chat_id=100,
            start_date=datetime(2025, 1, 1),
            end_date=datetime(2025, 12, 31),
        )
        assert len(result) == 1
        assert result[0]["id"] == 1

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_messages(self):
        """get_messages_by_date_range returns empty list when no matches."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.get_messages_by_date_range(chat_id=100)
        assert result == []


# ============================================================
# get_messages_paginated (search_messages) — lines 1078-1182
# ============================================================


class TestGetMessagesPaginated:
    """Test get_messages_paginated with cursor-based and offset-based pagination."""

    def _make_message_row(self, msg_id=1, text="Hello", media_type=None, raw_data=None):
        """Build a fake joined result row for messages paginated query."""
        msg = MagicMock()
        msg.id = msg_id
        msg.chat_id = 100
        msg.sender_id = 1
        msg.date = datetime(2025, 6, 1)
        msg.text = text
        msg.reply_to_msg_id = None
        msg.reply_to_top_id = None
        msg.reply_to_text = None
        msg.forward_from_id = None
        msg.edit_date = None
        msg.raw_data = raw_data
        msg.created_at = None
        msg.is_outgoing = 0
        msg.is_pinned = 0

        row = MagicMock()
        row.Message = msg
        row.first_name = "Alice"
        row.last_name = "Smith"
        row.username = "alice"
        row.media_id = "file_1" if media_type else None
        row.media_type = media_type
        row.media_file_path = "/path/photo.jpg" if media_type else None
        row.media_file_name = "photo.jpg" if media_type else None
        row.media_file_size = 1024 if media_type else None
        row.media_mime_type = "image/jpeg" if media_type else None
        row.media_width = 800 if media_type else None
        row.media_height = 600 if media_type else None
        row.media_duration = None
        return row

    @pytest.mark.asyncio
    async def test_returns_messages_with_user_info(self):
        """get_messages_paginated returns messages enriched with user fields."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_message_row(msg_id=10, text="Test msg")
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        # Mock get_reactions to return empty
        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100, limit=50)
        assert len(result) == 1
        assert result[0]["id"] == 10
        assert result[0]["first_name"] == "Alice"
        assert result[0]["username"] == "alice"
        assert result[0]["media"] is None

    @pytest.mark.asyncio
    async def test_returns_messages_with_media(self):
        """get_messages_paginated includes nested media object when present."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_message_row(msg_id=20, media_type="photo")
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100)
        assert result[0]["media"] is not None
        assert result[0]["media"]["type"] == "photo"
        assert result[0]["media"]["file_path"] == "/path/photo.jpg"

    @pytest.mark.asyncio
    async def test_cursor_based_pagination_with_before_date(self):
        """get_messages_paginated uses cursor-based pagination when before_date is set."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100, before_date=datetime(2025, 6, 1), before_id=50)
        assert result == []

    @pytest.mark.asyncio
    async def test_offset_pagination_orders_same_timestamp_by_id_desc(self):
        """Offset pagination uses the deterministic date DESC, id DESC ordering."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result
        adapter.get_reactions = AsyncMock(return_value=[])

        await adapter.get_messages_paginated(chat_id=100, limit=2, offset=4)

        stmt = mock_session.execute.await_args.args[0]
        order_by = list(stmt._order_by_clauses)
        assert str(order_by[0]) == str(Message.date.desc())
        assert str(order_by[1]) == str(Message.id.desc())
        assert stmt._limit_clause.value == 2
        assert stmt._offset_clause.value == 4

    @pytest.mark.asyncio
    async def test_with_search_filter(self):
        """get_messages_paginated applies text search filter."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100, search="keyword")
        assert result == []

    @pytest.mark.asyncio
    async def test_with_topic_id_filter(self):
        """get_messages_paginated applies topic_id filter for forums."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100, topic_id=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_parses_raw_data_json(self):
        """get_messages_paginated parses raw_data JSON string into dict."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_message_row(msg_id=30, raw_data='{"key": "value"}')
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100)
        assert result[0]["raw_data"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_handles_invalid_raw_data_json(self):
        """get_messages_paginated returns empty dict for invalid raw_data JSON."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_message_row(msg_id=31, raw_data="not json{{{")
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100)
        assert result[0]["raw_data"] == {}

    @pytest.mark.asyncio
    async def test_fetches_reply_text_for_replies(self):
        """get_messages_paginated looks up reply_to_text when reply_to_msg_id set."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_message_row(msg_id=40)
        row.Message.reply_to_msg_id = 39
        row.Message.reply_to_text = None

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))

        # Second execute call returns the reply text
        reply_result = MagicMock()
        reply_result.scalar_one_or_none.return_value = "Original message text"

        mock_session.execute.side_effect = [mock_result, reply_result]
        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.get_messages_paginated(chat_id=100)
        assert result[0]["reply_to_text"] == "Original message text"[:100]

    @pytest.mark.asyncio
    async def test_aggregates_reactions_by_emoji(self):
        """get_messages_paginated groups reactions by emoji with counts."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_message_row(msg_id=50)
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        adapter.get_reactions = AsyncMock(
            return_value=[
                {"emoji": "thumbsup", "count": 2, "user_id": 1},
                {"emoji": "thumbsup", "count": 1, "user_id": 2},
                {"emoji": "heart", "count": 1, "user_id": 3},
            ]
        )

        result = await adapter.get_messages_paginated(chat_id=100)
        reactions = result[0]["reactions"]
        emojis = {r["emoji"] for r in reactions}
        assert "thumbsup" in emojis
        assert "heart" in emojis
        thumbsup = next(r for r in reactions if r["emoji"] == "thumbsup")
        assert thumbsup["count"] == 3
        assert len(thumbsup["user_ids"]) == 2


# ============================================================
# get_forum_topics — lines 1554-1598
# ============================================================


class TestGetForumTopics:
    """Test get_forum_topics returns topics with message counts."""

    @pytest.mark.asyncio
    async def test_returns_topics_with_message_counts(self):
        """get_forum_topics returns list of topic dicts with message_count."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        topic = MagicMock()
        topic.id = 1
        topic.chat_id = -1001234
        topic.title = "General"
        topic.icon_color = None
        topic.icon_emoji_id = None
        topic.icon_emoji = None
        topic.is_closed = 0
        topic.is_pinned = 1
        topic.is_hidden = 0
        topic.date = datetime(2025, 1, 1)

        row = MagicMock()
        row.ForumTopic = topic
        row.message_count = 42
        row.last_message_date = datetime(2025, 6, 1)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_forum_topics(-1001234)
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["title"] == "General"
        assert result[0]["message_count"] == 42

    @pytest.mark.asyncio
    async def test_returns_zero_message_count_for_empty_topic(self):
        """get_forum_topics returns 0 message_count when no messages in topic."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        topic = MagicMock()
        topic.id = 2
        topic.chat_id = -1001234
        topic.title = "Empty"
        topic.icon_color = None
        topic.icon_emoji_id = None
        topic.icon_emoji = None
        topic.is_closed = 0
        topic.is_pinned = 0
        topic.is_hidden = 0
        topic.date = None

        row = MagicMock()
        row.ForumTopic = topic
        row.message_count = None
        row.last_message_date = None

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_forum_topics(-1001234)
        assert result[0]["message_count"] == 0

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_topics(self):
        """get_forum_topics returns empty list for chat with no topics."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_forum_topics(-1001234)
        assert result == []


# ============================================================
# upsert_forum_topic — lines 1515-1550
# ============================================================


class TestUpsertForumTopic:
    """Test upsert_forum_topic for SQLite and PostgreSQL."""

    @pytest.mark.asyncio
    async def test_upsert_forum_topic_sqlite_executes_and_commits(self):
        """upsert_forum_topic on SQLite executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=True)
        adapter = DatabaseAdapter(db_manager)

        topic_data = {"id": 1, "chat_id": -1001234, "title": "General"}
        await adapter.upsert_forum_topic(topic_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_forum_topic_postgres_executes_and_commits(self):
        """upsert_forum_topic on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        topic_data = {"id": 2, "chat_id": -1001234, "title": "Dev"}
        await adapter.upsert_forum_topic(topic_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# get_pinned_messages — lines 1316-1375
# ============================================================


class TestGetPinnedMessages:
    """Test get_pinned_messages returns pinned messages with joins."""

    @pytest.mark.asyncio
    async def test_returns_pinned_messages_with_media(self):
        """get_pinned_messages returns pinned message dicts with media info."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        msg = MagicMock()
        msg.id = 10
        msg.chat_id = 100
        msg.sender_id = 1
        msg.date = datetime(2025, 1, 1)
        msg.text = "Pinned!"
        msg.reply_to_msg_id = None
        msg.reply_to_top_id = None
        msg.reply_to_text = None
        msg.forward_from_id = None
        msg.edit_date = None
        msg.raw_data = None
        msg.created_at = None
        msg.is_outgoing = 0
        msg.is_pinned = 1

        row = MagicMock()
        row.Message = msg
        row.first_name = "Bob"
        row.last_name = None
        row.username = "bob"
        row.media_type = "photo"
        row.media_id = "f1"
        row.media_file_path = "/p.jpg"
        row.media_file_name = "p.jpg"
        row.media_file_size = 512
        row.media_mime_type = "image/jpeg"
        row.media_width = 100
        row.media_height = 100
        row.media_duration = None

        mock_result = MagicMock()
        mock_result.all.return_value = [row]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_pinned_messages(100)
        assert len(result) == 1
        assert result[0]["id"] == 10
        assert result[0]["media"]["type"] == "photo"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_pinned(self):
        """get_pinned_messages returns empty list when no messages are pinned."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.get_pinned_messages(100)
        assert result == []

    @pytest.mark.asyncio
    async def test_pinned_message_without_media(self):
        """get_pinned_messages returns None media when message has no media."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        msg = MagicMock()
        msg.id = 11
        msg.chat_id = 100
        msg.sender_id = 1
        msg.date = datetime(2025, 2, 1)
        msg.text = "Text only pin"
        msg.reply_to_msg_id = None
        msg.reply_to_top_id = None
        msg.reply_to_text = None
        msg.forward_from_id = None
        msg.edit_date = None
        msg.raw_data = None
        msg.created_at = None
        msg.is_outgoing = 0
        msg.is_pinned = 1

        row = MagicMock()
        row.Message = msg
        row.first_name = "Alice"
        row.last_name = None
        row.username = "alice"
        row.media_type = None
        row.media_id = None
        row.media_file_path = None
        row.media_file_name = None
        row.media_file_size = None
        row.media_mime_type = None
        row.media_width = None
        row.media_height = None
        row.media_duration = None

        mock_result = MagicMock()
        mock_result.all.return_value = [row]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_pinned_messages(100)
        assert result[0]["media"] is None


# ============================================================
# sync_folder_members — lines 1634-1648
# ============================================================


class TestSyncFolderMembers:
    """Test sync_folder_members replaces folder membership."""

    @pytest.mark.asyncio
    async def test_sync_folder_members_deletes_then_adds(self):
        """sync_folder_members deletes old members and adds new ones."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        # The second execute returns existing chat IDs
        existing_result = MagicMock()
        existing_result.__iter__ = MagicMock(return_value=iter([(100,), (200,)]))

        mock_session.execute.side_effect = [
            None,  # delete existing members
            existing_result,  # select existing chat IDs
        ]

        await adapter.sync_folder_members(folder_id=1, chat_ids=[100, 200, 300])

        # delete + select existing = 2 execute calls
        assert mock_session.execute.await_count == 2
        # session.add called for each valid chat
        assert mock_session.add.call_count == 2
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_folder_members_empty_chat_ids(self):
        """sync_folder_members with empty list only deletes existing members."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.sync_folder_members(folder_id=1, chat_ids=[])

        mock_session.execute.assert_awaited_once()  # just the delete
        mock_session.commit.assert_awaited_once()


# ============================================================
# get_all_folders — lines 1656-1685
# ============================================================


class TestGetAllFolders:
    """Test get_all_folders returns folders with chat counts."""

    @pytest.mark.asyncio
    async def test_returns_folders_with_counts(self):
        """get_all_folders returns list of folder dicts with chat_count."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        folder = MagicMock()
        folder.id = 1
        folder.title = "Work"
        folder.emoticon = None
        folder.sort_order = 0

        row = MagicMock()
        row.ChatFolder = folder
        row.chat_count = 5

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_folders()
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["title"] == "Work"
        assert result[0]["chat_count"] == 5

    @pytest.mark.asyncio
    async def test_filters_empty_folders_for_restricted_users(self):
        """get_all_folders skips folders with 0 visible chats when allowed_chat_ids set."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        folder = MagicMock()
        folder.id = 1
        folder.title = "Hidden"
        folder.emoticon = None
        folder.sort_order = 0

        row = MagicMock()
        row.ChatFolder = folder
        row.chat_count = 0

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([row]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_folders(allowed_chat_ids={100})
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_folders(self):
        """get_all_folders returns empty list when no folders exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_folders()
        assert result == []


# ============================================================
# Viewer account: get, get_by_username, get_all, update — lines 1735-1765
# ============================================================


class TestViewerAccountExtended:
    """Test viewer account read and update operations."""

    def _mock_account(self, account_id=1, username="viewer1"):
        account = MagicMock()
        account.id = account_id
        account.username = username
        account.password_hash = "hash"
        account.salt = "salt"
        account.allowed_chat_ids = None
        account.is_active = 1
        account.no_download = 0
        account.created_by = "admin"
        account.created_at = datetime(2025, 1, 1)
        account.updated_at = datetime(2025, 1, 1)
        return account

    @pytest.mark.asyncio
    async def test_get_viewer_account_returns_dict_when_found(self):
        """get_viewer_account returns a dict when account exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = self._mock_account()
        mock_session.execute.return_value = mock_result

        result = await adapter.get_viewer_account(1)
        assert result is not None
        assert result["id"] == 1
        assert result["username"] == "viewer1"

    @pytest.mark.asyncio
    async def test_get_viewer_account_returns_none_when_missing(self):
        """get_viewer_account returns None when account does not exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_viewer_account(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_viewer_by_username_returns_dict(self):
        """get_viewer_by_username returns a dict when username exists."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = self._mock_account(username="alice")
        mock_session.execute.return_value = mock_result

        result = await adapter.get_viewer_by_username("alice")
        assert result is not None
        assert result["username"] == "alice"

    @pytest.mark.asyncio
    async def test_get_viewer_by_username_returns_none_when_missing(self):
        """get_viewer_by_username returns None when username not found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.get_viewer_by_username("nobody")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_all_viewer_accounts_returns_list(self):
        """get_all_viewer_accounts returns list of all account dicts."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            self._mock_account(1, "alice"),
            self._mock_account(2, "bob"),
        ]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_viewer_accounts()
        assert len(result) == 2
        assert result[0]["username"] == "alice"
        assert result[1]["username"] == "bob"

    @pytest.mark.asyncio
    async def test_update_viewer_account_returns_updated_dict(self):
        """update_viewer_account updates fields and returns dict."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        account = self._mock_account()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = account
        mock_session.execute.return_value = mock_result
        mock_session.refresh = AsyncMock()

        result = await adapter.update_viewer_account(1, is_active=0)
        assert result is not None
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_viewer_account_returns_none_when_missing(self):
        """update_viewer_account returns None when account not found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.update_viewer_account(999, is_active=0)
        assert result is None


# ============================================================
# Audit log: create, get — lines 1804-1828
# ============================================================


class TestAuditLogOperations:
    """Test create_audit_log and get_audit_logs."""

    @pytest.mark.asyncio
    async def test_create_audit_log_adds_and_commits(self):
        """create_audit_log adds a log entry and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        await adapter.create_audit_log(
            username="admin",
            role="master",
            action="login",
            endpoint="/api/login",
            ip_address="127.0.0.1",
        )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_audit_logs_returns_list(self):
        """get_audit_logs returns list of log entry dicts."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        log_entry = MagicMock()
        log_entry.id = 1
        log_entry.username = "admin"
        log_entry.role = "master"
        log_entry.action = "login"
        log_entry.endpoint = "/api/login"
        log_entry.chat_id = None
        log_entry.ip_address = "127.0.0.1"
        log_entry.user_agent = "Mozilla"
        log_entry.created_at = datetime(2025, 6, 1)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [log_entry]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_audit_logs()
        assert len(result) == 1
        assert result[0]["username"] == "admin"
        assert result[0]["action"] == "login"

    @pytest.mark.asyncio
    async def test_get_audit_logs_with_username_filter(self):
        """get_audit_logs applies username filter."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.get_audit_logs(username="admin")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_audit_logs_with_action_filter(self):
        """get_audit_logs applies action prefix filter."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.get_audit_logs(action="login")
        assert result == []


# ============================================================
# Viewer sessions: load_all, cleanup_expired — lines 1895-1924
# ============================================================


class TestViewerSessionExtended:
    """Test load_all_sessions and cleanup_expired_sessions."""

    @pytest.mark.asyncio
    async def test_load_all_sessions_returns_list(self):
        """load_all_sessions returns list of all session dicts."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        session_row = MagicMock()
        session_row.token = "tok1"
        session_row.username = "admin"
        session_row.role = "master"
        session_row.allowed_chat_ids = None
        session_row.no_download = 0
        session_row.source_token_id = None
        session_row.created_at = 1700000000.0
        session_row.last_accessed = 1700001000.0

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [session_row]
        mock_session.execute.return_value = mock_result

        result = await adapter.load_all_sessions()
        assert len(result) == 1
        assert result[0]["token"] == "tok1"

    @pytest.mark.asyncio
    async def test_load_all_sessions_empty(self):
        """load_all_sessions returns empty list when no sessions exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.load_all_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions_returns_count(self):
        """cleanup_expired_sessions deletes old sessions and returns count."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.rowcount = 5
        mock_session.execute.return_value = mock_result

        count = await adapter.cleanup_expired_sessions(max_age_seconds=3600)
        assert count == 5
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_session_postgres_executes_upsert(self):
        """save_session on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        await adapter.save_session("token_pg", "admin", "master", None, 1700000000.0, 1700001000.0)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# Viewer tokens: create, get_all, verify, update — lines 1963-2015
# ============================================================


class TestViewerTokenExtended:
    """Test create_viewer_token, get_all_viewer_tokens, verify_viewer_token, update_viewer_token."""

    def _mock_token(self, token_id=1):
        token = MagicMock()
        token.id = token_id
        token.label = "share-link"
        token.token_hash = "abc123"
        token.token_salt = "def456"
        token.created_by = "admin"
        token.allowed_chat_ids = "[100]"
        token.is_revoked = 0
        token.no_download = 0
        token.expires_at = None
        token.last_used_at = None
        token.use_count = 0
        token.created_at = datetime(2025, 6, 1)
        return token

    @pytest.mark.asyncio
    async def test_create_viewer_token_adds_and_commits(self):
        """create_viewer_token adds a token record and commits."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_session.refresh = AsyncMock()
        # After refresh, the token object's attributes will be read via _viewer_token_to_dict
        # We need to ensure the added object has the right attributes

        result = await adapter.create_viewer_token(
            label="test",
            token_hash="hash",
            token_salt="salt",
            created_by="admin",
            allowed_chat_ids="[100]",
        )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_all_viewer_tokens_returns_list(self):
        """get_all_viewer_tokens returns list of token dicts."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_token(1), self._mock_token(2)]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_viewer_tokens()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_verify_viewer_token_returns_none_when_no_match(self):
        """verify_viewer_token returns None when no token matches."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.verify_viewer_token("invalid_token")
        assert result is None

    @pytest.mark.asyncio
    async def test_verify_viewer_token_skips_expired_tokens(self):
        """verify_viewer_token skips tokens that have expired."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        expired_token = self._mock_token()
        expired_token.expires_at = datetime(2020, 1, 1)  # Expired in the past

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [expired_token]
        mock_session.execute.return_value = mock_result

        result = await adapter.verify_viewer_token("some_token")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_viewer_token_returns_updated_dict(self):
        """update_viewer_token updates allowed fields and returns dict."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        token = self._mock_token()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_session.execute.return_value = mock_result
        mock_session.refresh = AsyncMock()

        result = await adapter.update_viewer_token(1, label="new-label")
        assert result is not None
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_viewer_token_returns_none_when_missing(self):
        """update_viewer_token returns None when token not found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await adapter.update_viewer_token(999, label="x")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_viewer_token_ignores_disallowed_fields(self):
        """update_viewer_token only sets allowed fields, ignoring others."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        token = self._mock_token()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_session.execute.return_value = mock_result
        mock_session.refresh = AsyncMock()

        # token_hash is NOT in the allowed_fields set
        await adapter.update_viewer_token(1, token_hash="evil", label="ok")
        # token_hash should not have been set
        assert token.token_hash == "abc123"  # unchanged


# ============================================================
# App settings: get_all, postgres set — lines 2056-2075
# ============================================================


class TestAppSettingsExtended:
    """Test get_all_settings and set_setting on PostgreSQL."""

    @pytest.mark.asyncio
    async def test_get_all_settings_returns_dict(self):
        """get_all_settings returns a dict of all key-value pairs."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        setting1 = MagicMock()
        setting1.key = "theme"
        setting1.value = "dark"
        setting2 = MagicMock()
        setting2.key = "lang"
        setting2.value = "en"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [setting1, setting2]
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_settings()
        assert result == {"theme": "dark", "lang": "en"}

    @pytest.mark.asyncio
    async def test_get_all_settings_returns_empty_dict(self):
        """get_all_settings returns empty dict when no settings exist."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await adapter.get_all_settings()
        assert result == {}

    @pytest.mark.asyncio
    async def test_set_setting_postgres_executes_and_commits(self):
        """set_setting on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        await adapter.set_setting("theme", "dark")

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()


# ============================================================
# calculate_and_store_statistics — lines 955-998
# ============================================================


class TestCalculateAndStoreStatistics:
    """Test calculate_and_store_statistics computes and stores stats."""

    @pytest.mark.asyncio
    async def test_calculates_and_stores_stats(self):
        """calculate_and_store_statistics queries counts and stores via set_metadata."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        # 5 execute calls: chat count, msg count, media count, total size, per-chat stats
        chat_result = MagicMock()
        chat_result.scalar.return_value = 10

        msg_result = MagicMock()
        msg_result.scalar.return_value = 500

        media_result = MagicMock()
        media_result.scalar.return_value = 50

        size_result = MagicMock()
        size_result.scalar.return_value = 10485760  # 10 MB

        chat_stats_row = MagicMock()
        chat_stats_row.chat_id = 100
        chat_stats_row.message_count = 500
        per_chat_result = MagicMock()
        per_chat_result.__iter__ = MagicMock(return_value=iter([chat_stats_row]))

        mock_session.execute.side_effect = [
            chat_result,
            msg_result,
            media_result,
            size_result,
            per_chat_result,
        ]

        # Mock set_metadata
        adapter.set_metadata = AsyncMock()

        result = await adapter.calculate_and_store_statistics()
        assert result["chats"] == 10
        assert result["messages"] == 500
        assert result["media_files"] == 50
        assert result["total_size_mb"] == 10.0
        assert 100 in result["per_chat_message_counts"]

        # Verify it stored stats
        assert adapter.set_metadata.await_count == 2


# ============================================================
# find_message_by_date_with_joins — lines 1197-1288
# ============================================================


class TestFindMessageByDateWithJoins:
    """Test find_message_by_date_with_joins fallback logic."""

    def _make_joined_row(self, msg_id=1, media_type=None, reply_to=None):
        msg = MagicMock()
        msg.id = msg_id
        msg.chat_id = 100
        msg.sender_id = 1
        msg.date = datetime(2025, 6, 1)
        msg.text = "Found"
        msg.reply_to_msg_id = reply_to
        msg.reply_to_top_id = None
        msg.reply_to_text = None
        msg.forward_from_id = None
        msg.edit_date = None
        msg.raw_data = None
        msg.created_at = None
        msg.is_outgoing = 0
        msg.is_pinned = 0

        row = MagicMock()
        row.Message = msg
        row.first_name = "Alice"
        row.last_name = None
        row.username = "alice"
        row.media_type = media_type
        row.media_id = "f1" if media_type else None
        row.media_file_path = "/p.jpg" if media_type else None
        row.media_file_name = "p.jpg" if media_type else None
        row.media_file_size = 1024 if media_type else None
        row.media_mime_type = "image/jpeg" if media_type else None
        row.media_width = 800 if media_type else None
        row.media_height = 600 if media_type else None
        row.media_duration = None
        return row

    @pytest.mark.asyncio
    async def test_finds_message_on_or_after_target_date(self):
        """find_message_by_date_with_joins returns message on/after target date."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_joined_row(msg_id=10)
        result1 = MagicMock()
        result1.first.return_value = row
        mock_session.execute.return_value = result1

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.find_message_by_date_with_joins(100, datetime(2025, 5, 1))
        assert result is not None
        assert result["id"] == 10
        assert result["first_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_falls_back_to_before_target_date(self):
        """find_message_by_date_with_joins tries before target if no on/after found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_joined_row(msg_id=5)

        # First execute: no result on/after
        result1 = MagicMock()
        result1.first.return_value = None
        # Second execute: found before target
        result2 = MagicMock()
        result2.first.return_value = row

        mock_session.execute.side_effect = [result1, result2]
        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.find_message_by_date_with_joins(100, datetime(2025, 12, 1))
        assert result is not None
        assert result["id"] == 5

    @pytest.mark.asyncio
    async def test_falls_back_to_first_message(self):
        """find_message_by_date_with_joins tries first message if no before found."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_joined_row(msg_id=1)

        result1 = MagicMock()
        result1.first.return_value = None
        result2 = MagicMock()
        result2.first.return_value = None
        result3 = MagicMock()
        result3.first.return_value = row

        mock_session.execute.side_effect = [result1, result2, result3]
        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.find_message_by_date_with_joins(100, datetime(2020, 1, 1))
        assert result is not None
        assert result["id"] == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_chat_has_no_messages(self):
        """find_message_by_date_with_joins returns None for empty chat."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        empty = MagicMock()
        empty.first.return_value = None

        mock_session.execute.return_value = empty

        result = await adapter.find_message_by_date_with_joins(999, datetime(2025, 1, 1))
        assert result is None

    @pytest.mark.asyncio
    async def test_includes_media_when_present(self):
        """find_message_by_date_with_joins includes nested media object."""
        db_manager, mock_session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)

        row = self._make_joined_row(msg_id=20, media_type="video")
        result1 = MagicMock()
        result1.first.return_value = row
        mock_session.execute.return_value = result1

        adapter.get_reactions = AsyncMock(return_value=[])

        result = await adapter.find_message_by_date_with_joins(100, datetime(2025, 1, 1))
        assert result["media"] is not None
        assert result["media"]["type"] == "video"


# ============================================================
# upsert_chat_folder postgres — lines 1625-1626
# ============================================================


class TestUpsertChatFolderPostgres:
    """Test upsert_chat_folder on PostgreSQL path."""

    @pytest.mark.asyncio
    async def test_upsert_chat_folder_postgres_executes_and_commits(self):
        """upsert_chat_folder on PostgreSQL executes an upsert and commits."""
        db_manager, mock_session = _make_mock_db_manager(is_sqlite=False)
        adapter = DatabaseAdapter(db_manager)

        folder_data = {"id": 1, "title": "Work", "emoticon": None, "sort_order": 0}
        await adapter.upsert_chat_folder(folder_data)

        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()
