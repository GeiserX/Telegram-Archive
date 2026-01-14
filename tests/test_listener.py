"""
Tests for the real-time listener module.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.listener import TelegramListener


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
        return config
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database adapter."""
        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[
            {'id': -1001234567890},
            {'id': 123456789},
            {'id': -987654321}
        ])
        db.update_message_text = AsyncMock()
        db.delete_message = AsyncMock()
        db.delete_message_by_id_any_chat = AsyncMock(return_value=True)
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
        asyncio.get_event_loop().run_until_complete(listener._load_tracked_chats())
        
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
        asyncio.get_event_loop().run_until_complete(listener.close())
        
        mock_db.close.assert_called_once()
    
    def test_stats_initialization(self, mock_config, mock_db):
        """Test statistics are properly initialized."""
        listener = TelegramListener(mock_config, mock_db)
        
        assert listener.stats['edits_processed'] == 0
        assert listener.stats['deletions_processed'] == 0
        assert listener.stats['errors'] == 0
        assert listener.stats['start_time'] is None


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
