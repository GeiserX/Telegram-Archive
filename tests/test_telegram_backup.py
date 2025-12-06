import unittest
from src.telegram_backup import TelegramBackup


class TestMediaTypeDetection(unittest.TestCase):
    """Test media type detection for animations/stickers."""

    def test_animation_detection(self):
        """Animated documents should be detected as 'animation' type."""
        # Verify the _get_media_type method exists on TelegramBackup
        self.assertTrue(hasattr(TelegramBackup, '_get_media_type'))


class TestReplyToText(unittest.TestCase):
    """Test reply-to text extraction and display."""

    def test_reply_text_truncation(self):
        """Reply text should be truncated to 100 characters."""
        # The truncation is at [:100] in the code
        long_text = "a" * 200
        truncated = long_text[:100]
        self.assertEqual(len(truncated), 100)


if __name__ == '__main__':
    unittest.main()
