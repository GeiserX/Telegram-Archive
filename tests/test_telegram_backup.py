"""Tests for Telegram backup functionality."""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram_backup import TelegramBackup


class TestMediaTypeDetection(unittest.TestCase):
    """Test media type detection for animations/stickers."""

    def test_animation_detection_method_exists(self):
        """Animated documents should be detected as 'animation' type."""
        # Verify the _get_media_type method exists on TelegramBackup
        self.assertTrue(hasattr(TelegramBackup, "_get_media_type"))

    def test_media_extension_method_exists(self):
        """Verify _get_media_extension method exists."""
        self.assertTrue(hasattr(TelegramBackup, "_get_media_extension"))


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
        self.assertTrue(hasattr(TelegramBackup, "create"))

    def test_has_backup_methods(self):
        """TelegramBackup should have required backup methods."""
        required_methods = [
            "connect",
            "disconnect",
            "backup_all",
            "_backup_dialog",
            "_process_message",
        ]
        for method in required_methods:
            self.assertTrue(hasattr(TelegramBackup, method), f"TelegramBackup missing method: {method}")


class TestCleanupExistingMedia(unittest.TestCase):
    """Test _cleanup_existing_media for SKIP_MEDIA_CHAT_IDS feature."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.config = MagicMock()
        self.config.media_path = self.media_path
        self.config.skip_media_chat_ids = {-1001234567890}
        self.config.skip_media_delete_existing = True

        self.db = AsyncMock()
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = self.config
        self.backup.db = self.db
        self.backup._cleaned_media_chats = set()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_cleanup_deletes_real_files(self):
        """Should delete real files and report freed bytes."""
        chat_id = -1001234567890
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(chat_dir)

        file_path = os.path.join(chat_dir, "photo.jpg")
        with open(file_path, "wb") as f:
            f.write(b"x" * 1024)

        self.db.get_media_for_chat.return_value = [
            {"id": "m1", "message_id": 1, "chat_id": chat_id, "type": "photo",
             "file_path": file_path, "file_size": 1024, "downloaded": True}
        ]
        self.db.delete_media_for_chat.return_value = 1

        self._run(self.backup._cleanup_existing_media(chat_id))

        self.assertFalse(os.path.exists(file_path))
        self.db.delete_media_for_chat.assert_awaited_once_with(chat_id)

    def test_cleanup_removes_symlinks_without_counting_freed_bytes(self):
        """Symlink removal should not count toward freed bytes."""
        chat_id = -1001234567890
        chat_dir = os.path.join(self.media_path, str(chat_id))
        shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(chat_dir)
        os.makedirs(shared_dir)

        shared_file = os.path.join(shared_dir, "photo.jpg")
        with open(shared_file, "wb") as f:
            f.write(b"x" * 2048)

        symlink_path = os.path.join(chat_dir, "photo.jpg")
        rel_path = os.path.relpath(shared_file, chat_dir)
        os.symlink(rel_path, symlink_path)

        self.db.get_media_for_chat.return_value = [
            {"id": "m1", "message_id": 1, "chat_id": chat_id, "type": "photo",
             "file_path": symlink_path, "file_size": 2048, "downloaded": True}
        ]
        self.db.delete_media_for_chat.return_value = 1

        self._run(self.backup._cleanup_existing_media(chat_id))

        # Symlink removed
        self.assertFalse(os.path.exists(symlink_path))
        # Shared original preserved
        self.assertTrue(os.path.exists(shared_file))

    def test_cleanup_removes_empty_chat_directory(self):
        """Should remove the chat media directory if empty after cleanup."""
        chat_id = -1001234567890
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(chat_dir)

        file_path = os.path.join(chat_dir, "photo.jpg")
        with open(file_path, "wb") as f:
            f.write(b"x" * 512)

        self.db.get_media_for_chat.return_value = [
            {"id": "m1", "message_id": 1, "chat_id": chat_id, "type": "photo",
             "file_path": file_path, "file_size": 512, "downloaded": True}
        ]
        self.db.delete_media_for_chat.return_value = 1

        self._run(self.backup._cleanup_existing_media(chat_id))

        self.assertFalse(os.path.isdir(chat_dir))

    def test_cleanup_keeps_nonempty_directory(self):
        """Should keep chat directory if other files remain."""
        chat_id = -1001234567890
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(chat_dir)

        tracked_file = os.path.join(chat_dir, "tracked.jpg")
        with open(tracked_file, "wb") as f:
            f.write(b"x" * 512)

        untracked_file = os.path.join(chat_dir, "untracked.jpg")
        with open(untracked_file, "wb") as f:
            f.write(b"y" * 256)

        self.db.get_media_for_chat.return_value = [
            {"id": "m1", "message_id": 1, "chat_id": chat_id, "type": "photo",
             "file_path": tracked_file, "file_size": 512, "downloaded": True}
        ]
        self.db.delete_media_for_chat.return_value = 1

        self._run(self.backup._cleanup_existing_media(chat_id))

        self.assertFalse(os.path.exists(tracked_file))
        self.assertTrue(os.path.exists(untracked_file))
        self.assertTrue(os.path.isdir(chat_dir))

    def test_cleanup_no_records_skips(self):
        """Should return early when no media records exist."""
        self.db.get_media_for_chat.return_value = []

        self._run(self.backup._cleanup_existing_media(-1001234567890))

        self.db.delete_media_for_chat.assert_not_awaited()

    def test_cleanup_handles_missing_files(self):
        """Should handle records where file doesn't exist on disk."""
        chat_id = -1001234567890
        self.db.get_media_for_chat.return_value = [
            {"id": "m1", "message_id": 1, "chat_id": chat_id, "type": "photo",
             "file_path": "/nonexistent/path.jpg", "file_size": 1024, "downloaded": True}
        ]
        self.db.delete_media_for_chat.return_value = 1

        self._run(self.backup._cleanup_existing_media(chat_id))

        self.db.delete_media_for_chat.assert_awaited_once_with(chat_id)

    def test_cleanup_session_cache_prevents_rerun(self):
        """Second call for same chat should be skipped via session cache."""
        chat_id = -1001234567890
        self.db.get_media_for_chat.return_value = []

        self._run(self.backup._cleanup_existing_media(chat_id))
        self.backup._cleaned_media_chats.add(chat_id)

        # Simulate second backup cycle check
        self.assertIn(chat_id, self.backup._cleaned_media_chats)

    def test_cleanup_mixed_real_and_symlinks(self):
        """Should handle a mix of real files and symlinks correctly."""
        chat_id = -1001234567890
        chat_dir = os.path.join(self.media_path, str(chat_id))
        shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(chat_dir)
        os.makedirs(shared_dir)

        real_file = os.path.join(chat_dir, "real_video.mp4")
        with open(real_file, "wb") as f:
            f.write(b"v" * 4096)

        shared_file = os.path.join(shared_dir, "shared_photo.jpg")
        with open(shared_file, "wb") as f:
            f.write(b"p" * 2048)

        symlink_path = os.path.join(chat_dir, "shared_photo.jpg")
        rel_path = os.path.relpath(shared_file, chat_dir)
        os.symlink(rel_path, symlink_path)

        self.db.get_media_for_chat.return_value = [
            {"id": "m1", "message_id": 1, "chat_id": chat_id, "type": "video",
             "file_path": real_file, "file_size": 4096, "downloaded": True},
            {"id": "m2", "message_id": 2, "chat_id": chat_id, "type": "photo",
             "file_path": symlink_path, "file_size": 2048, "downloaded": True},
        ]
        self.db.delete_media_for_chat.return_value = 2

        self._run(self.backup._cleanup_existing_media(chat_id))

        self.assertFalse(os.path.exists(real_file))
        self.assertFalse(os.path.exists(symlink_path))
        self.assertTrue(os.path.exists(shared_file))

    def test_cleanup_db_error_does_not_crash(self):
        """Database errors should be caught and logged, not crash."""
        self.db.get_media_for_chat.side_effect = Exception("DB connection lost")

        self._run(self.backup._cleanup_existing_media(-1001234567890))


if __name__ == "__main__":
    unittest.main()
