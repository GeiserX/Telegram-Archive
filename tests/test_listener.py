"""
Tests for the real-time listener module.
"""

import asyncio
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import events

from src.listener import MassOperationProtector, TelegramListener


class TestTelegramListener:
    """Tests for TelegramListener class."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config object."""
        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "test_hash"
        config.phone = "+1234567890"
        config.session_path = "/tmp/test_session"
        config.global_include_ids = set()
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        # CHAT_IDS whitelist mode (v6.0.0)
        config.whitelist_mode = False
        config.chat_ids = set()
        # Mass operation protection settings
        config.listen_edits = True
        config.listen_deletions = False
        config.mass_operation_threshold = 10
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        return config

    @pytest.fixture
    def mock_db(self):
        """Create a mock database adapter."""
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[{"id": -1001234567890}, {"id": 123456789}, {"id": -987654321}])
        db.update_message_text = AsyncMock()
        db.delete_message = AsyncMock()
        db.resolve_message_chat_id = AsyncMock(return_value=-1001234567890)
        db.close = AsyncMock()
        return db

    def test_init(self, mock_config, mock_db):
        """Test listener initialization."""
        listener = TelegramListener(mock_config, mock_db)

        assert listener.config == mock_config
        assert listener.db == mock_db
        assert listener.client is None
        assert listener._running is False
        assert listener._tracked_chat_ids == set()

    def test_load_tracked_chats(self, mock_config, mock_db):
        """Test loading tracked chats from database."""
        listener = TelegramListener(mock_config, mock_db)

        # Run async method synchronously
        asyncio.run(listener._load_tracked_chats())

        assert listener._tracked_chat_ids == {-1001234567890, 123456789, -987654321}
        mock_db.get_all_chats.assert_called_once()

    def test_should_process_chat_tracked(self, mock_config, mock_db):
        """Test _should_process_chat returns True for tracked chats."""
        listener = TelegramListener(mock_config, mock_db)
        listener._tracked_chat_ids = {-1001234567890, 123456789}

        assert listener._should_process_chat(-1001234567890) is True
        assert listener._should_process_chat(123456789) is True
        assert listener._should_process_chat(999999999) is False

    def test_should_process_chat_include_list(self, mock_config, mock_db):
        """Test _should_process_chat returns True for included chats."""
        mock_config.global_include_ids = {-1009999999}
        listener = TelegramListener(mock_config, mock_db)
        listener._tracked_chat_ids = set()

        assert listener._should_process_chat(-1009999999) is True
        assert listener._should_process_chat(-1008888888) is False

    def test_get_marked_id(self, mock_config, mock_db):
        """Test _get_marked_id handles various inputs."""
        listener = TelegramListener(mock_config, mock_db)

        # Test with raw integer
        assert listener._get_marked_id(123456789) == 123456789

        # Test with object having id attribute
        mock_entity = MagicMock()
        mock_entity.id = 987654321
        # This will try get_peer_id first, which will fail, then fall back to .id
        result = listener._get_marked_id(mock_entity)
        assert result == 987654321

    def test_close(self, mock_config, mock_db):
        """Test clean shutdown."""
        listener = TelegramListener(mock_config, mock_db)
        listener.client = AsyncMock()
        listener.client.is_connected = MagicMock(return_value=False)

        # Run async method synchronously
        asyncio.run(listener.close())

        mock_db.close.assert_called_once()

    def test_stats_initialization(self, mock_config, mock_db):
        """Test statistics are properly initialized."""
        listener = TelegramListener(mock_config, mock_db)

        # Check stats keys match actual implementation
        assert listener.stats["edits_received"] == 0
        assert listener.stats["edits_applied"] == 0
        assert listener.stats["deletions_received"] == 0
        assert listener.stats["deletions_applied"] == 0
        assert listener.stats["deletions_skipped"] == 0
        assert listener.stats["operations_discarded"] == 0
        assert listener.stats["errors"] == 0
        assert listener.stats["start_time"] is None


class TestInitConfig:
    """Tests for __init__ with various configuration settings."""

    @pytest.fixture
    def base_config(self):
        """Config with all attributes the __init__ method accesses."""
        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "test_hash"
        config.phone = "+1234567890"
        config.session_path = "/tmp/test_session"
        config.global_include_ids = set()
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        config.whitelist_mode = False
        config.chat_ids = set()
        config.listen_edits = True
        config.listen_deletions = False
        config.listen_new_messages = False
        config.listen_new_messages_media = False
        config.listen_chat_actions = False
        config.skip_topic_ids = {}
        config.mass_operation_threshold = 10
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        return config

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.close = AsyncMock()
        return db

    def test_init_calls_validate_credentials(self, base_config, mock_db):
        """Test that __init__ validates credentials."""
        TelegramListener(base_config, mock_db)
        base_config.validate_credentials.assert_called_once()

    def test_init_with_existing_client(self, base_config, mock_db):
        """Test initialization with an externally provided client."""
        external_client = MagicMock()
        listener = TelegramListener(base_config, mock_db, client=external_client)

        assert listener.client is external_client
        assert listener._owns_client is False

    def test_init_without_client_owns_client(self, base_config, mock_db):
        """Test initialization without client sets _owns_client to True."""
        listener = TelegramListener(base_config, mock_db)

        assert listener.client is None
        assert listener._owns_client is True

    def test_init_creates_protector_with_config_values(self, base_config, mock_db):
        """Test that MassOperationProtector receives config thresholds."""
        base_config.mass_operation_threshold = 25
        base_config.mass_operation_window_seconds = 60

        listener = TelegramListener(base_config, mock_db)

        assert listener._protector.threshold == 25
        assert listener._protector.window_seconds == 60

    def test_init_stats_include_new_messages_counters(self, base_config, mock_db):
        """Test that stats dict includes new_messages counters."""
        listener = TelegramListener(base_config, mock_db)

        assert listener.stats["new_messages_received"] == 0
        assert listener.stats["new_messages_saved"] == 0
        assert listener.stats["bursts_intercepted"] == 0

    def test_init_with_listen_new_messages_enabled(self, base_config, mock_db):
        """Test init logs correctly when listen_new_messages is true."""
        base_config.listen_new_messages = True
        base_config.listen_new_messages_media = True
        # Should not raise
        listener = TelegramListener(base_config, mock_db)
        assert listener.config.listen_new_messages is True

    def test_init_with_skip_topic_ids(self, base_config, mock_db):
        """Test init logs topic exclusion info when skip_topic_ids is set."""
        base_config.skip_topic_ids = {-1001234: {100, 200}, -1005678: {300}}
        # Should not raise
        listener = TelegramListener(base_config, mock_db)
        assert listener.config.skip_topic_ids == {-1001234: {100, 200}, -1005678: {300}}

    def test_init_with_listen_deletions_enabled(self, base_config, mock_db):
        """Test init when deletions are enabled triggers warning log path."""
        base_config.listen_deletions = True
        listener = TelegramListener(base_config, mock_db)
        assert listener.config.listen_deletions is True


class TestEventHandlers:
    """Tests for the on_new_message, on_message_edited, on_message_deleted handlers.

    The handlers are inner closures registered inside _register_handlers().
    We capture them by mocking self.client.on() as a decorator that stores
    the wrapped function, keyed by event type.
    """

    @pytest.fixture
    def full_config(self):
        """Config with all attributes accessed by handlers."""
        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "test_hash"
        config.phone = "+1234567890"
        config.session_path = "/tmp/test_session"
        config.global_include_ids = set()
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        config.whitelist_mode = False
        config.chat_ids = set()
        config.listen_edits = True
        config.listen_deletions = True
        config.listen_new_messages = True
        config.listen_new_messages_media = False
        config.listen_chat_actions = False
        config.skip_topic_ids = {}
        config.should_skip_topic = MagicMock(return_value=False)
        config.mass_operation_threshold = 100
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        config.should_download_media_for_chat = MagicMock(return_value=False)
        return config

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.update_message_text = AsyncMock()
        db.delete_message = AsyncMock()
        db.resolve_message_chat_id = AsyncMock(return_value=None)
        db.upsert_chat = AsyncMock()
        db.upsert_user = AsyncMock()
        db.insert_message = AsyncMock()
        db.insert_media = AsyncMock()
        db.close = AsyncMock()
        return db

    @pytest.fixture
    def listener_with_handlers(self, full_config, mock_db):
        """Create a listener and register handlers, capturing them for direct invocation."""
        listener = TelegramListener(full_config, mock_db)
        listener._tracked_chat_ids = {-1001234567890}
        listener._notifier = None  # Disable notifications for unit tests

        # Capture handlers registered by _register_handlers
        handlers = {}
        mock_client = MagicMock()

        def capture_on(event_type):
            """Return a decorator that stores the handler function."""

            def decorator(fn):
                handlers[event_type] = fn
                return fn

            return decorator

        mock_client.on = capture_on
        listener.client = mock_client
        listener._register_handlers()

        return listener, handlers

    # ----------------------------------------------------------------
    # on_new_message tests
    # ----------------------------------------------------------------

    def test_on_new_message_skips_untracked_chat(self, listener_with_handlers):
        """Test new message handler returns early for untracked chats."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.NewMessage]

        event = MagicMock()
        event.chat_id = 99999  # Not in tracked set
        event.message = MagicMock()
        event.message.reply_to = None

        asyncio.run(handler(event))

        assert listener.stats["new_messages_received"] == 0
        listener.db.insert_message.assert_not_called()

    def test_on_new_message_topic_filtering_skips_excluded_topic(self, listener_with_handlers, full_config):
        """Test new message handler skips messages in excluded forum topics."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.NewMessage]

        # Configure topic to be skipped
        full_config.should_skip_topic = MagicMock(return_value=True)

        event = MagicMock()
        event.chat_id = -1001234567890  # Tracked chat
        msg = MagicMock()
        msg.reply_to = None  # Prevent MagicMock truthiness issue
        event.message = msg

        asyncio.run(handler(event))

        # Should NOT count as received (skipped before the counter)
        assert listener.stats["new_messages_received"] == 0
        listener.db.insert_message.assert_not_called()

    def test_on_new_message_listen_new_messages_false_returns_early(self, listener_with_handlers, full_config):
        """Test new message handler returns early when listen_new_messages is disabled."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.NewMessage]

        full_config.listen_new_messages = False

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        event.message = msg

        asyncio.run(handler(event))

        # Counter IS incremented (message was received), but not saved
        assert listener.stats["new_messages_received"] == 1
        assert listener.stats["new_messages_saved"] == 0
        listener.db.insert_message.assert_not_called()

    def test_on_new_message_saves_message_to_db(self, listener_with_handlers, full_config):
        """Test new message handler inserts message into database."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.NewMessage]

        from datetime import datetime

        event = MagicMock()
        event.chat_id = -1001234567890

        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1, tzinfo=UTC)
        msg.text = "Hello world"
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = None
        msg.sender = None  # No sender entity
        event.message = msg

        # get_chat returns a chat entity
        chat_entity = MagicMock()
        chat_entity.title = "Test Chat"
        chat_entity.username = None
        chat_entity.first_name = None
        chat_entity.last_name = None
        event.get_chat = AsyncMock(return_value=chat_entity)

        asyncio.run(handler(event))

        assert listener.stats["new_messages_received"] == 1
        assert listener.stats["new_messages_saved"] == 1
        listener.db.insert_message.assert_called_once()
        listener.db.upsert_chat.assert_called_once()

    def test_on_new_message_adds_untracked_chat_to_tracking(self, listener_with_handlers, full_config):
        """Test new message from untracked-but-included chat gets added to tracking."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.NewMessage]

        # Chat not in tracked set, but in global include list
        new_chat_id = -1009999999
        full_config.global_include_ids = {new_chat_id}
        listener._tracked_chat_ids = set()  # Empty

        event = MagicMock()
        event.chat_id = new_chat_id
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 1
        msg.sender_id = 111
        msg.date = MagicMock()
        msg.text = "test"
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = None
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=MagicMock())

        asyncio.run(handler(event))

        assert new_chat_id in listener._tracked_chat_ids

    def test_on_new_message_increments_error_on_exception(self, listener_with_handlers):
        """Test error counter increments when handler raises an exception."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.NewMessage]

        event = MagicMock()
        # Force _get_marked_id to raise
        event.chat_id = MagicMock(side_effect=Exception("boom"))
        # _get_marked_id calls get_peer_id which will fail, then tries .id
        # Make .id also raise
        event.chat_id.id = property(lambda self: (_ for _ in ()).throw(Exception("boom")))

        # We need to make _get_marked_id raise inside the try block
        listener._get_marked_id = MagicMock(side_effect=Exception("test error"))

        asyncio.run(handler(event))

        assert listener.stats["errors"] == 1

    # ----------------------------------------------------------------
    # on_message_edited tests
    # ----------------------------------------------------------------

    def test_on_message_edited_skips_when_listen_edits_false(self, listener_with_handlers, full_config):
        """Test edit handler returns immediately when listen_edits is disabled."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        full_config.listen_edits = False

        event = MagicMock()
        event.chat_id = -1001234567890
        event.message = MagicMock()
        event.message.reply_to = None

        asyncio.run(handler(event))

        assert listener.stats["edits_received"] == 0
        listener.db.update_message_text.assert_not_called()

    def test_on_message_edited_skips_untracked_chat(self, listener_with_handlers):
        """Test edit handler ignores edits from untracked chats."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        event = MagicMock()
        event.chat_id = 99999  # Not tracked
        event.message = MagicMock()
        event.message.reply_to = None

        asyncio.run(handler(event))

        assert listener.stats["edits_received"] == 0
        listener.db.update_message_text.assert_not_called()

    def test_on_message_edited_skips_excluded_topic(self, listener_with_handlers, full_config):
        """Test edit handler skips messages in excluded forum topics."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        full_config.should_skip_topic = MagicMock(return_value=True)

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        event.message = msg

        asyncio.run(handler(event))

        assert listener.stats["edits_received"] == 0
        listener.db.update_message_text.assert_not_called()

    def test_on_message_edited_applies_edit(self, listener_with_handlers):
        """Test edit handler applies edit to database when allowed."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        from datetime import datetime

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.text = "Updated text"
        msg.edit_date = datetime(2025, 1, 1, tzinfo=UTC)
        event.message = msg

        asyncio.run(handler(event))

        assert listener.stats["edits_received"] == 1
        assert listener.stats["edits_applied"] == 1
        listener.db.update_message_text.assert_called_once_with(
            chat_id=-1001234567890,
            message_id=42,
            new_text="Updated text",
            edit_date=msg.edit_date,
        )

    def test_on_message_edited_handles_none_text(self, listener_with_handlers):
        """Test edit handler treats None text as empty string."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.text = None
        msg.edit_date = None
        event.message = msg

        asyncio.run(handler(event))

        assert listener.stats["edits_applied"] == 1
        call_kwargs = listener.db.update_message_text.call_args[1]
        assert call_kwargs["new_text"] == ""

    def test_on_message_edited_increments_error_on_exception(self, listener_with_handlers):
        """Test error counter increments when edit handler raises."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        listener._get_marked_id = MagicMock(side_effect=Exception("db error"))

        event = MagicMock()
        event.chat_id = -1001234567890

        asyncio.run(handler(event))

        assert listener.stats["errors"] == 1

    # ----------------------------------------------------------------
    # on_message_deleted tests
    # ----------------------------------------------------------------

    def test_on_message_deleted_skips_when_listen_deletions_false(self, listener_with_handlers, full_config):
        """Test delete handler skips and counts when deletions are disabled."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        full_config.listen_deletions = False

        event = MagicMock()
        event.deleted_ids = [1, 2, 3]

        asyncio.run(handler(event))

        assert listener.stats["deletions_skipped"] == 3
        assert listener.stats["deletions_received"] == 0
        listener.db.delete_message.assert_not_called()

    def test_on_message_deleted_skips_untracked_chat(self, listener_with_handlers):
        """Test delete handler ignores deletions from untracked chats."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        event = MagicMock()
        event.chat_id = 99999  # Not tracked
        event.deleted_ids = [1, 2]

        asyncio.run(handler(event))

        assert listener.stats["deletions_received"] == 0
        listener.db.delete_message.assert_not_called()

    def test_on_message_deleted_applies_deletion(self, listener_with_handlers):
        """Test delete handler applies each deletion to the database."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.deleted_ids = [10, 20]

        asyncio.run(handler(event))

        assert listener.stats["deletions_received"] == 2
        assert listener.stats["deletions_applied"] == 2
        assert listener.db.delete_message.call_count == 2

    def test_on_message_deleted_resolves_chat_when_chat_id_none(self, listener_with_handlers, mock_db):
        """Test delete handler resolves chat_id from DB when event has None chat_id."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        # chat_id is None, must resolve from DB
        mock_db.resolve_message_chat_id = AsyncMock(return_value=-1001234567890)

        event = MagicMock()
        event.chat_id = None
        event.deleted_ids = [42]

        asyncio.run(handler(event))

        mock_db.resolve_message_chat_id.assert_called_once_with(42)
        assert listener.stats["deletions_applied"] == 1

    def test_on_message_deleted_skips_unresolvable_message(self, listener_with_handlers, mock_db):
        """Test delete handler skips messages that cannot be resolved to a chat."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        mock_db.resolve_message_chat_id = AsyncMock(return_value=None)

        event = MagicMock()
        event.chat_id = None
        event.deleted_ids = [42]

        asyncio.run(handler(event))

        assert listener.stats["deletions_applied"] == 0
        listener.db.delete_message.assert_not_called()

    def test_on_message_deleted_increments_error_on_exception(self, listener_with_handlers):
        """Test error counter increments when delete handler raises."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        listener._get_marked_id = MagicMock(side_effect=Exception("crash"))

        event = MagicMock()
        event.chat_id = -1001234567890
        event.deleted_ids = [1]

        asyncio.run(handler(event))

        assert listener.stats["errors"] == 1


class TestStatsTracking:
    """Tests verifying stats counters are incremented correctly across operations."""

    @pytest.fixture
    def full_config(self):
        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "test_hash"
        config.phone = "+1234567890"
        config.session_path = "/tmp/test_session"
        config.global_include_ids = set()
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        config.whitelist_mode = False
        config.chat_ids = set()
        config.listen_edits = True
        config.listen_deletions = True
        config.listen_new_messages = True
        config.listen_new_messages_media = False
        config.listen_chat_actions = False
        config.skip_topic_ids = {}
        config.should_skip_topic = MagicMock(return_value=False)
        config.mass_operation_threshold = 100
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        config.should_download_media_for_chat = MagicMock(return_value=False)
        return config

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.update_message_text = AsyncMock()
        db.delete_message = AsyncMock()
        db.resolve_message_chat_id = AsyncMock(return_value=None)
        db.upsert_chat = AsyncMock()
        db.upsert_user = AsyncMock()
        db.insert_message = AsyncMock()
        db.close = AsyncMock()
        return db

    @pytest.fixture
    def listener_with_handlers(self, full_config, mock_db):
        listener = TelegramListener(full_config, mock_db)
        listener._tracked_chat_ids = {-1001234567890}
        listener._notifier = None

        handlers = {}
        mock_client = MagicMock()

        def capture_on(event_type):
            def decorator(fn):
                handlers[event_type] = fn
                return fn

            return decorator

        mock_client.on = capture_on
        listener.client = mock_client
        listener._register_handlers()

        return listener, handlers

    def test_multiple_edits_increment_counters(self, listener_with_handlers):
        """Test that multiple edits correctly increment both received and applied counters."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageEdited]

        for i in range(5):
            event = MagicMock()
            event.chat_id = -1001234567890
            msg = MagicMock()
            msg.reply_to = None
            msg.id = i
            msg.text = f"edit {i}"
            msg.edit_date = None
            event.message = msg
            asyncio.run(handler(event))

        assert listener.stats["edits_received"] == 5
        assert listener.stats["edits_applied"] == 5

    def test_multiple_deletions_increment_counters(self, listener_with_handlers):
        """Test that a batch deletion increments per-message counters."""
        listener, handlers = listener_with_handlers
        handler = handlers[events.MessageDeleted]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.deleted_ids = [1, 2, 3, 4, 5]

        asyncio.run(handler(event))

        assert listener.stats["deletions_received"] == 5
        assert listener.stats["deletions_applied"] == 5

    def test_mixed_operations_track_independently(self, listener_with_handlers):
        """Test that edit and deletion stats are tracked independently."""
        listener, handlers = listener_with_handlers
        edit_handler = handlers[events.MessageEdited]
        delete_handler = handlers[events.MessageDeleted]

        # 3 edits
        for i in range(3):
            event = MagicMock()
            event.chat_id = -1001234567890
            msg = MagicMock()
            msg.reply_to = None
            msg.id = i
            msg.text = "x"
            msg.edit_date = None
            event.message = msg
            asyncio.run(edit_handler(event))

        # 2 deletions
        del_event = MagicMock()
        del_event.chat_id = -1001234567890
        del_event.deleted_ids = [100, 200]
        asyncio.run(delete_handler(del_event))

        assert listener.stats["edits_received"] == 3
        assert listener.stats["edits_applied"] == 3
        assert listener.stats["deletions_received"] == 2
        assert listener.stats["deletions_applied"] == 2
        assert listener.stats["errors"] == 0


class TestWhitelistMode:
    """Tests for CHAT_IDS whitelist mode in _should_process_chat."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "test_hash"
        config.phone = "+1234567890"
        config.session_path = "/tmp/test_session"
        config.global_include_ids = {-1009999999}
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        config.whitelist_mode = True
        config.chat_ids = {-1001111111}
        config.listen_edits = True
        config.listen_deletions = False
        config.listen_new_messages = False
        config.listen_new_messages_media = False
        config.listen_chat_actions = False
        config.skip_topic_ids = {}
        config.mass_operation_threshold = 10
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        return config

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.close = AsyncMock()
        return db

    def test_whitelist_mode_allows_whitelisted_chat(self, mock_config, mock_db):
        """Test whitelist mode allows chat in CHAT_IDS."""
        listener = TelegramListener(mock_config, mock_db)
        assert listener._should_process_chat(-1001111111) is True

    def test_whitelist_mode_blocks_non_whitelisted_chat(self, mock_config, mock_db):
        """Test whitelist mode blocks chat NOT in CHAT_IDS."""
        listener = TelegramListener(mock_config, mock_db)
        assert listener._should_process_chat(-1002222222) is False

    def test_whitelist_mode_ignores_include_lists(self, mock_config, mock_db):
        """Test whitelist mode ignores global_include_ids even if chat matches."""
        listener = TelegramListener(mock_config, mock_db)
        # -1009999999 is in global_include_ids but NOT in chat_ids
        assert listener._should_process_chat(-1009999999) is False

    def test_whitelist_mode_ignores_tracked_chats(self, mock_config, mock_db):
        """Test whitelist mode ignores tracked chats if not in CHAT_IDS."""
        listener = TelegramListener(mock_config, mock_db)
        listener._tracked_chat_ids = {-1003333333}
        assert listener._should_process_chat(-1003333333) is False


class TestMassOperationProtector:
    """Tests for the MassOperationProtector rate limiting class."""

    def test_allows_operations_under_threshold(self):
        """Test that operations under the threshold are allowed."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        for _ in range(5):
            allowed, reason = protector.check_operation(-100, "edit")
            assert allowed is True
            assert reason == "allowed"

    def test_blocks_operations_over_threshold(self):
        """Test that operations exceeding threshold are blocked."""
        protector = MassOperationProtector(threshold=3, window_seconds=30)
        results = []
        for _ in range(5):
            allowed, reason = protector.check_operation(-100, "deletion")
            results.append(allowed)

        # First 3 allowed, 4th triggers block (count 4 > threshold 3)
        assert results[:3] == [True, True, True]
        assert results[3] is False  # 4th triggers rate limit
        assert results[4] is False  # 5th also blocked

    def test_get_stats_returns_counts(self):
        """Test get_stats returns meaningful statistics."""
        protector = MassOperationProtector(threshold=2, window_seconds=30)
        protector.check_operation(-100, "edit")
        protector.check_operation(-100, "edit")
        protector.check_operation(-100, "edit")  # triggers rate limit

        stats = protector.get_stats()
        assert stats["operations_applied"] >= 2
        assert "rate_limits_triggered" in stats
        assert "currently_blocked" in stats

    def test_separate_chats_have_independent_limits(self):
        """Test that rate limits are tracked per-chat independently."""
        protector = MassOperationProtector(threshold=2, window_seconds=30)

        # Fill up chat A
        protector.check_operation(-100, "edit")
        protector.check_operation(-100, "edit")
        protector.check_operation(-100, "edit")  # May trigger block for -100

        # Chat B should still be allowed
        allowed, _ = protector.check_operation(-200, "edit")
        assert allowed is True


class TestListenerEventHandling:
    """Tests for event handling behavior."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "test_hash"
        config.phone = "+1234567890"
        config.session_path = "/tmp/test_session"
        config.global_include_ids = set()
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        # CHAT_IDS whitelist mode (v6.0.0)
        config.whitelist_mode = False
        config.chat_ids = set()
        # Mass operation protection settings
        config.listen_edits = True
        config.listen_deletions = False
        config.mass_operation_threshold = 10
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        return config

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.update_message_text = AsyncMock()
        db.delete_message = AsyncMock()
        db.close = AsyncMock()
        return db

    def test_listener_filters_untracked_chats(self, mock_config, mock_db):
        """Test that events from untracked chats are ignored."""
        listener = TelegramListener(mock_config, mock_db)
        listener._tracked_chat_ids = {-1001234567890}

        # Should process
        assert listener._should_process_chat(-1001234567890) is True

        # Should NOT process
        assert listener._should_process_chat(-1009999999999) is False
