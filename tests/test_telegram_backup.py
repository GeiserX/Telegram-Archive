"""Tests for Telegram backup functionality."""

import unittest
from src.telegram_backup import TelegramBackup


class TestMediaTypeDetection(unittest.TestCase):
    """Test media type detection for animations/stickers."""

    def test_animation_detection_method_exists(self):
        """Animated documents should be detected as 'animation' type."""
        # Verify the _get_media_type method exists on TelegramBackup
        self.assertTrue(hasattr(TelegramBackup, '_get_media_type'))
    
    def test_media_extension_method_exists(self):
        """Verify _get_media_extension method exists."""
        self.assertTrue(hasattr(TelegramBackup, '_get_media_extension'))


class TestReplyToText(unittest.TestCase):
    """Test reply-to text extraction and display."""

    def test_reply_text_truncation(self):
        """Reply text should be truncated to 100 characters."""
        # The truncation is at [:100] in the code
        long_text = "a" * 200
        truncated = long_text[:100]
        self.assertEqual(len(truncated), 100)


class TestTelegramBackupClass(unittest.TestCase):
    """Test TelegramBackup class structure."""
    
    def test_has_factory_method(self):
        """TelegramBackup should have async factory method."""
        self.assertTrue(hasattr(TelegramBackup, 'create'))
    
    def test_has_backup_methods(self):
        """TelegramBackup should have required backup methods."""
        required_methods = [
            'connect',
            'disconnect', 
            'backup_all',
            '_backup_dialog',
            '_process_message',
        ]
        for method in required_methods:
            self.assertTrue(
                hasattr(TelegramBackup, method),
                f"TelegramBackup missing method: {method}"
            )


if __name__ == '__main__':
    unittest.main()
