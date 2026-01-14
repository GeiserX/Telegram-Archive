"""Tests for database adapter - specifically data type handling."""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

# Import the helper function directly
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.db.adapter import _strip_tz


class TestStripTimezone:
    """Test the _strip_tz helper function for PostgreSQL compatibility."""
    
    def test_strip_tz_with_utc(self):
        """Timezone-aware datetime should have timezone stripped."""
        dt_aware = datetime(2025, 1, 14, 12, 30, 0, tzinfo=timezone.utc)
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
        dt_aware = datetime(2025, 1, 14, 12, 30, 45, 123456, tzinfo=timezone.utc)
        result = _strip_tz(dt_aware)
        
        assert result is not None
        assert result.microsecond == 123456


class TestDataConsistency:
    """Test that all DB operations handle data types consistently."""
    
    def test_message_data_types_documented(self):
        """
        Verify that message data types are handled consistently.
        
        This test documents the expected types from Telethon:
        - message.date: datetime (timezone-aware from Telegram)
        - message.edit_date: datetime or None (timezone-aware)
        - message.id: int
        - message.text: str or None
        
        All datetime fields must be stripped of timezone before PostgreSQL insert/update.
        """
        # This is a documentation/reminder test
        # The actual data flow is:
        # 1. Telethon returns datetime with tzinfo=timezone.utc
        # 2. _strip_tz removes the timezone
        # 3. PostgreSQL stores as TIMESTAMP WITHOUT TIME ZONE
        
        # Example of what Telethon returns
        telegram_edit_date = datetime(2025, 1, 14, 15, 30, 0, tzinfo=timezone.utc)
        
        # What we must send to PostgreSQL
        db_edit_date = _strip_tz(telegram_edit_date)
        
        assert db_edit_date.tzinfo is None, "Database dates must be timezone-naive"


class TestChatIdConsistency:
    """Test that chat IDs are handled consistently (marked ID format)."""
    
    def test_marked_id_format_documented(self):
        """
        Document the expected chat ID formats.
        
        Telegram uses "marked" IDs:
        - Users: positive (e.g., 123456789)
        - Basic groups (Chat): negative (e.g., -123456789)
        - Supergroups/Channels: -1000000000000 - channel_id (e.g., -1001234567890)
        
        All code paths must use get_peer_id(entity) for consistency.
        """
        # Basic group ID calculation
        basic_group_raw_id = 798230299
        basic_group_marked_id = -basic_group_raw_id
        assert basic_group_marked_id == -798230299
        
        # Channel/Supergroup ID calculation
        channel_raw_id = 1234567890
        channel_marked_id = -1000000000000 - channel_raw_id
        assert channel_marked_id == -1001234567890
        
        # User IDs stay positive
        user_id = 123456789
        assert user_id > 0
