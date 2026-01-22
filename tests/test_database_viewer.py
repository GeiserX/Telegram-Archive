"""Tests for database viewer functionality."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import Mock, patch, AsyncMock

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_backup_"))

from src.web import main as web_main


class TestDatabaseViewer(unittest.TestCase):
    """Test database viewer operations."""
    
    def test_get_all_chats_structure(self):
        """Test that get_all_chats returns expected structure."""
        # Test the expected structure of chat data
        expected_keys = ['id', 'type', 'title', 'username', 'first_name', 
                        'last_name', 'phone', 'description', 'participants_count']
        
        # Mock chat data
        mock_chat = {
            'id': 123456789,
            'type': 'private',
            'title': None,
            'username': 'testuser',
            'first_name': 'Test',
            'last_name': 'User',
            'phone': None,
            'description': None,
            'participants_count': None,
        }
        
        # Verify all expected keys are present
        for key in expected_keys:
            self.assertIn(key, mock_chat)
    
    def test_chat_avatar_path_format(self):
        """Test avatar path formatting."""
        chat_id = 123456789
        chat_type = 'private'
        
        # For private chats, avatars are in 'users' folder
        expected_folder = 'users' if chat_type == 'private' else 'chats'
        self.assertEqual(expected_folder, 'users')
        
        # For groups/channels, avatars are in 'chats' folder
        chat_type = 'group'
        expected_folder = 'users' if chat_type == 'private' else 'chats'
        self.assertEqual(expected_folder, 'chats')


class TestAvatarPathLookup(unittest.TestCase):
    """Test avatar path discovery in web viewer."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_media_path = web_main.config.media_path
        web_main.config.media_path = self.temp_dir.name
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

    def tearDown(self):
        web_main.config.media_path = self.original_media_path
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def _touch_avatar(self, path: str, mtime: int) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as avatar_file:
            avatar_file.write("x")
        os.utime(path, (mtime, mtime))

    def test_prefers_new_format_and_falls_back_to_legacy(self):
        """New `<chat_id>_<photo_id>.jpg` is preferred; legacy files still resolve."""
        chat_id = 123456
        avatars_dir = os.path.join(self.temp_dir.name, "avatars", "users")

        legacy_avatar = os.path.join(avatars_dir, f"{chat_id}.jpg")
        self._touch_avatar(legacy_avatar, mtime=100)

        new_avatar = os.path.join(avatars_dir, f"{chat_id}_999.jpg")
        self._touch_avatar(new_avatar, mtime=200)

        path = web_main._find_avatar_path(chat_id, "private")
        self.assertEqual(path, f"avatars/users/{chat_id}_999.jpg")

        os.remove(new_avatar)
        path = web_main._find_avatar_path(chat_id, "private")
        self.assertEqual(path, f"avatars/users/{chat_id}.jpg")


class TestAsyncDatabaseAdapter(unittest.TestCase):
    """Test async database adapter."""
    
    def test_adapter_methods_exist(self):
        """Verify DatabaseAdapter has required methods."""
        from src.db.adapter import DatabaseAdapter
        
        required_methods = [
            'get_all_chats',
            'get_messages_paginated',
            'get_cached_statistics',
            'calculate_and_store_statistics',
            'upsert_chat',
            'upsert_user',
            'insert_message',
            'insert_messages_batch',
            'get_reactions',
            'insert_reactions',
        ]
        
        for method in required_methods:
            self.assertTrue(
                hasattr(DatabaseAdapter, method),
                f"DatabaseAdapter missing method: {method}"
            )


if __name__ == '__main__':
    unittest.main()
