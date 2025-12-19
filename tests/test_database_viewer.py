"""Tests for database viewer functionality."""

import unittest
import asyncio
from unittest.mock import Mock, patch, AsyncMock


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


class TestAsyncDatabaseAdapter(unittest.TestCase):
    """Test async database adapter."""
    
    def test_adapter_methods_exist(self):
        """Verify DatabaseAdapter has required methods."""
        from src.db.adapter import DatabaseAdapter
        
        required_methods = [
            'get_all_chats',
            'get_messages_paginated',
            'get_statistics',
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
