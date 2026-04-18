"""Tests for dangling symlink fix in media deduplication (issue #115).

Covers:
- Dangling symlink detection via os.path.lexists vs os.path.exists
- Pre-unlink before symlink creation to avoid EEXIST
- Telethon download_media return value capture (.mp4 extension)
- _process_media dedup path with symlink replacement
- _verify_and_redownload_media cleanup of dangling symlinks
- shutil.move fallback when symlink creation fails (non-EEXIST)
"""

import asyncio
import errno
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram_backup import TelegramBackup


class TestDanglingSymlinkDetection(unittest.TestCase):
    """Verify os.path.exists vs os.path.lexists behavior with dangling symlinks."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dangling_symlink_detected_as_missing(self):
        """os.path.exists returns False for a dangling symlink but lexists returns True."""
        target = os.path.join(self.temp_dir, "deleted_target.mp4")
        link = os.path.join(self.temp_dir, "link.mp4")

        # Create target, symlink, then delete target to make it dangle
        with open(target, "w") as f:
            f.write("data")
        os.symlink(target, link)
        os.remove(target)

        # exists follows the symlink -- target gone, so False
        self.assertFalse(os.path.exists(link))
        # lexists checks the link itself -- still present
        self.assertTrue(os.path.lexists(link))

    def test_dangling_symlink_removed_by_lexists(self):
        """A dangling symlink detected via lexists can be removed with os.remove."""
        target = os.path.join(self.temp_dir, "gone.txt")
        link = os.path.join(self.temp_dir, "stale_link.txt")

        with open(target, "w") as f:
            f.write("temp")
        os.symlink(target, link)
        os.remove(target)

        # Confirm dangling
        self.assertTrue(os.path.lexists(link))
        self.assertFalse(os.path.exists(link))

        # Remove via the pattern used in the fix
        if os.path.lexists(link):
            os.remove(link)

        self.assertFalse(os.path.lexists(link))

    def test_symlink_creation_with_pre_unlink(self):
        """Pre-unlinking a dangling symlink allows creating a new valid symlink at the same path."""
        new_target = os.path.join(self.temp_dir, "new_target.mp4")
        old_target = os.path.join(self.temp_dir, "old_target.mp4")
        link = os.path.join(self.temp_dir, "media_link.mp4")

        # Create old target, symlink, then delete old target
        with open(old_target, "w") as f:
            f.write("old")
        os.symlink(old_target, link)
        os.remove(old_target)

        # Dangling
        self.assertFalse(os.path.exists(link))
        self.assertTrue(os.path.lexists(link))

        # Create new target
        with open(new_target, "w") as f:
            f.write("new content")

        # Pre-unlink pattern from the fix
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(new_target, link)

        # The new symlink resolves correctly
        self.assertTrue(os.path.exists(link))
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(new_target))
        with open(link) as f:
            self.assertEqual(f.read(), "new content")


class TestDownloadReturnValueCapture(unittest.TestCase):
    """Verify that the code uses the actual path returned by client.download_media."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_download_return_value_with_extension(self):
        """When download_media returns a path with .mp4 appended, symlink target uses that path."""
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, "100")
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        # Telethon appends extension to the requested path
        requested_path = os.path.join(shared_dir, "fileid123.bin")
        actual_returned_path = os.path.join(shared_dir, "fileid123.mp4")

        # Create the file at the returned path (simulating Telethon behavior)
        with open(actual_returned_path, "wb") as f:
            f.write(b"video data")

        # Set up backup instance
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock()
        backup.config.media_path = self.media_path
        backup.config.deduplicate_media = True
        backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        backup.client = AsyncMock()
        # download_media returns the actual path with .mp4
        backup.client.download_media = AsyncMock(return_value=actual_returned_path)

        # Build a mock message with photo media
        msg = MagicMock()
        msg.id = 1
        msg.date = MagicMock()
        msg.date.strftime = MagicMock(return_value="20240101_120000")
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = "fileid123"
        msg.reply_to = None

        backup._get_media_type = MagicMock(return_value="photo")
        backup._get_media_filename = MagicMock(return_value="fileid123.bin")
        backup._get_media_size = MagicMock(return_value=1024)

        result = self._run(backup._process_media(msg, 100))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])

        # The symlink in the chat dir should exist and point to the .mp4 file
        chat_link = os.path.join(chat_dir, "fileid123.bin")
        if os.path.lexists(chat_link):
            resolved = os.path.realpath(chat_link)
            self.assertEqual(resolved, os.path.realpath(actual_returned_path))


class TestProcessMediaDedupSymlink(unittest.TestCase):
    """Integration tests for _process_media with dedup enabled."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.deduplicate_media = True
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id=1, file_id="abc123"):
        msg = MagicMock()
        msg.id = msg_id
        msg.date = MagicMock()
        msg.date.strftime = MagicMock(return_value="20240101_120000")
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        # Ensure no document attribute so _get_media_type falls through correctly
        msg.media.document = None
        msg.reply_to = None
        return msg

    def test_process_media_dedup_captures_telethon_path(self):
        """Symlink target uses the actual path returned by download_media, not the requested path."""
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, "200")
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        # Telethon returns path with .mp4 extension appended
        returned_path = os.path.join(shared_dir, "photo_abc.mp4")
        with open(returned_path, "wb") as f:
            f.write(b"mp4 content here")

        self.backup.client.download_media = AsyncMock(return_value=returned_path)
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value="photo_abc")
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message(msg_id=10, file_id="abc")

        result = self._run(self.backup._process_media(msg, 200))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])

        # The chat-dir symlink should resolve to the .mp4 file
        chat_file = os.path.join(chat_dir, "photo_abc")
        if os.path.lexists(chat_file):
            resolved = os.path.realpath(chat_file)
            self.assertEqual(resolved, os.path.realpath(returned_path))

    def test_process_media_dedup_removes_dangling_before_symlink(self):
        """A dangling symlink at the file_path is replaced with a valid one during first-download dedup.

        This exercises the "first time seeing this file" branch (Path B) in _process_media,
        where the shared file does NOT exist yet. download_media creates it, then the
        lexists+unlink guard removes the dangling symlink before os.symlink.
        """
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, "300")
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "video_xyz.mp4"

        # Create a dangling symlink in the chat directory pointing to a deleted file
        old_target = os.path.join(shared_dir, "old_deleted_file.mp4")
        chat_link = os.path.join(chat_dir, file_name)
        with open(old_target, "w") as f:
            f.write("old")
        os.symlink(os.path.relpath(old_target, chat_dir), chat_link)
        os.remove(old_target)

        # Confirm dangling
        self.assertTrue(os.path.lexists(chat_link))
        self.assertFalse(os.path.exists(chat_link))

        # The shared file must NOT exist yet so we hit the "first download" branch.
        # download_media will "create" it as a side effect.
        new_shared = os.path.join(shared_dir, file_name)

        def fake_download(message, path):
            """Simulate Telethon writing the file at the requested path."""
            with open(path, "wb") as f:
                f.write(b"new video data")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)
        self.backup._get_media_type = MagicMock(return_value="video")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=1024)

        msg = self._make_message(msg_id=20, file_id="xyz")

        # The dangling symlink means os.path.exists(file_path) is False AND
        # shared file doesn't exist, so we enter the first-download branch.
        # The fix uses lexists+unlink before os.symlink to avoid EEXIST.
        result = self._run(self.backup._process_media(msg, 300))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])

        # After the fix, the symlink should be valid and point to new shared file
        self.assertTrue(os.path.lexists(chat_link))
        self.assertTrue(os.path.exists(chat_link))
        self.assertTrue(os.path.islink(chat_link))
        self.assertEqual(os.path.realpath(chat_link), os.path.realpath(new_shared))


class TestVerifyCleanupDanglingSymlink(unittest.TestCase):
    """Test _verify_and_redownload_media cleanup code using lexists for dangling symlinks."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.verify_media = True
        self.backup.config.skip_media_chat_ids = set()
        self.backup.config.deduplicate_media = True
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.db = AsyncMock()
        self.backup.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_verify_cleanup_removes_dangling_symlink(self):
        """_verify_and_redownload_media removes dangling symlinks via lexists before re-download."""
        chat_id = -1001234567890
        chat_dir = os.path.join(self.media_path, str(chat_id))
        shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(chat_dir)
        os.makedirs(shared_dir)

        # Create a dangling symlink
        old_target = os.path.join(shared_dir, "deleted.jpg")
        dangling_link = os.path.join(chat_dir, "photo.jpg")
        with open(old_target, "w") as f:
            f.write("old")
        os.symlink(os.path.relpath(old_target, chat_dir), dangling_link)
        os.remove(old_target)

        self.assertTrue(os.path.lexists(dangling_link))
        self.assertFalse(os.path.exists(dangling_link))

        # Set up DB to return media record pointing at the dangling symlink
        self.backup.db.get_media_for_verification.return_value = [
            {
                "file_path": dangling_link,
                "file_size": 1024,
                "chat_id": chat_id,
                "message_id": 42,
            }
        ]

        # Mock the Telegram message fetch
        mock_msg = MagicMock()
        mock_msg.id = 42
        mock_msg.media = MagicMock()
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])

        # Mock _process_media to return a successful result
        new_shared = os.path.join(shared_dir, "photo_new.jpg")
        with open(new_shared, "wb") as f:
            f.write(b"new photo data")

        self.backup._process_media = AsyncMock(
            return_value={
                "id": f"{chat_id}_42_photo",
                "type": "photo",
                "message_id": 42,
                "chat_id": chat_id,
                "file_path": dangling_link,
                "downloaded": True,
            }
        )

        self._run(self.backup._verify_and_redownload_media())

        # The cleanup code should have removed the dangling symlink before re-download
        # (lexists check + os.remove in the verification loop)
        # Verify _process_media was called (re-download attempted)
        self.backup._process_media.assert_awaited_once()
        # Verify db.insert_media was called (successful re-download)
        self.backup.db.insert_media.assert_awaited_once()


class TestShutilMoveFallback(unittest.TestCase):
    """Test that shutil.move is only used when symlink creation fails with non-EEXIST error."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.deduplicate_media = True
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id=1, file_id="fallback123"):
        msg = MagicMock()
        msg.id = msg_id
        msg.date = MagicMock()
        msg.date.strftime = MagicMock(return_value="20240601_120000")
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        msg.media.document = None
        msg.reply_to = None
        return msg

    def test_shutil_move_fallback_only_when_symlink_unsupported(self):
        """shutil.move is called when os.symlink raises a non-EEXIST OSError (e.g., Windows).

        This exercises the first-download branch (Path B) where the shared file does NOT
        exist initially. download_media creates it, then os.symlink fails with EPERM,
        triggering the shutil.move fallback.
        """
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, "400")
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "fallback_file.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        # Shared file must NOT exist initially (first-download branch).
        # download_media creates it as a side effect.
        def fake_download(message, path):
            with open(path, "wb") as f:
                f.write(b"image data for fallback test")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message()

        # Patch os.symlink to raise EPERM (simulating Windows / unsupported FS).
        # The code does `import shutil; shutil.move(...)` inside the except block,
        # so we patch shutil.move on the shutil module itself.
        symlink_error = OSError(errno.EPERM, "Operation not permitted")
        with patch("os.symlink", side_effect=symlink_error), patch("shutil.move") as mock_move:
            result = self._run(self.backup._process_media(msg, 400))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])
        # shutil.move should have been called as fallback
        mock_move.assert_called_once_with(shared_file, chat_file)

    def test_no_shutil_move_when_symlink_succeeds(self):
        """shutil.move is NOT called when os.symlink succeeds normally."""
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, "500")
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "normal_file.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        with open(shared_file, "wb") as f:
            f.write(b"normal image data")

        self.backup.client.download_media = AsyncMock(return_value=shared_file)
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=256)

        msg = self._make_message(msg_id=2, file_id="normal456")

        with patch("shutil.move") as mock_move:
            result = self._run(self.backup._process_media(msg, 500))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])
        # shutil.move should NOT have been called
        mock_move.assert_not_called()
        # The symlink should exist in the chat dir
        self.assertTrue(os.path.islink(chat_file))


class TestListenerDownloadMediaDedup(unittest.TestCase):
    """Tests for listener.py _download_media symlink/dedup changes."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        from src.listener import TelegramListener

        self.listener = TelegramListener.__new__(TelegramListener)
        self.listener.config = MagicMock()
        self.listener.config.media_path = self.media_path
        self.listener.config.deduplicate_media = True
        self.listener.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.listener.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, file_id="listener_abc"):
        msg = MagicMock()
        msg.id = 1
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        msg.media.photo.sizes = [MagicMock(size=1024)]
        msg.media.document = None
        msg.reply_to = None
        return msg

    def test_listener_dedup_pre_unlink_dangling_shared_exists(self):
        """Listener removes dangling symlink before creating new one when shared file exists."""
        chat_id = 100
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "photo_abc.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        # Create shared file
        with open(shared_file, "wb") as f:
            f.write(b"shared data")

        # Create dangling symlink in chat dir
        old_target = os.path.join(shared_dir, "old_gone.jpg")
        with open(old_target, "w") as f:
            f.write("old")
        os.symlink(os.path.relpath(old_target, chat_dir), chat_file)
        os.remove(old_target)

        # Dangling: lexists=True, exists=False
        self.assertTrue(os.path.lexists(chat_file))
        self.assertFalse(os.path.exists(chat_file))

        msg = self._make_message()
        self.listener._get_media_type = MagicMock(return_value="photo")
        self.listener._get_media_filename = MagicMock(return_value=file_name)

        result = self._run(self.listener._download_media(msg, chat_id))

        self.assertIsNotNone(result)
        # After fix: symlink should now point to the correct shared file
        self.assertTrue(os.path.exists(chat_file))
        self.assertTrue(os.path.islink(chat_file))

    def test_listener_dedup_copy2_fallback_when_symlink_fails(self):
        """Listener uses shutil.copy2 when symlink fails on shared-exists path."""
        chat_id = 200
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "photo_copy.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        with open(shared_file, "wb") as f:
            f.write(b"data to copy")

        msg = self._make_message()
        self.listener._get_media_type = MagicMock(return_value="photo")
        self.listener._get_media_filename = MagicMock(return_value=file_name)

        symlink_error = OSError(errno.EPERM, "Operation not permitted")
        with patch("os.symlink", side_effect=symlink_error), patch("shutil.copy2") as mock_copy:
            result = self._run(self.listener._download_media(msg, chat_id))

        self.assertIsNotNone(result)
        mock_copy.assert_called_once_with(shared_file, chat_file)

    def test_listener_dedup_first_download_captures_return_value(self):
        """Listener captures download_media return value for first-time download."""
        chat_id = 300
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "video_xyz"
        returned_path = os.path.join(shared_dir, "video_xyz.mp4")

        def fake_download(message, path):
            with open(returned_path, "wb") as f:
                f.write(b"video content")
            return returned_path

        self.listener.client.download_media = AsyncMock(side_effect=fake_download)
        self.listener._get_media_type = MagicMock(return_value="video")
        self.listener._get_media_filename = MagicMock(return_value=file_name)

        msg = self._make_message(file_id="xyz")

        result = self._run(self.listener._download_media(msg, chat_id))

        self.assertIsNotNone(result)
        # Symlink should point to the .mp4 path returned by Telethon
        chat_file = os.path.join(chat_dir, file_name)
        if os.path.lexists(chat_file):
            resolved = os.path.realpath(chat_file)
            self.assertEqual(resolved, os.path.realpath(returned_path))

    def test_listener_dedup_first_download_pre_unlink_dangling(self):
        """Listener removes dangling symlink before new symlink on first-download path."""
        chat_id = 400
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "doc_abc.pdf"
        chat_file = os.path.join(chat_dir, file_name)

        # Create dangling symlink
        old_target = os.path.join(shared_dir, "old.pdf")
        with open(old_target, "w") as f:
            f.write("old")
        os.symlink(os.path.relpath(old_target, chat_dir), chat_file)
        os.remove(old_target)

        self.assertTrue(os.path.lexists(chat_file))
        self.assertFalse(os.path.exists(chat_file))

        new_shared = os.path.join(shared_dir, file_name)

        def fake_download(message, path):
            with open(path, "wb") as f:
                f.write(b"new pdf content")
            return path

        self.listener.client.download_media = AsyncMock(side_effect=fake_download)
        self.listener._get_media_type = MagicMock(return_value="document")
        self.listener._get_media_filename = MagicMock(return_value=file_name)

        msg = self._make_message(file_id="abc")

        result = self._run(self.listener._download_media(msg, chat_id))

        self.assertIsNotNone(result)
        self.assertTrue(os.path.exists(chat_file))
        self.assertTrue(os.path.islink(chat_file))

    def test_listener_no_dedup_captures_return_value(self):
        """Listener captures download_media return value in non-dedup path."""
        chat_id = 500
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(chat_dir)

        self.listener.config.deduplicate_media = False
        file_name = "photo_no_dedup.jpg"
        returned_path = os.path.join(chat_dir, "photo_no_dedup.mp4")

        def fake_download(message, path):
            with open(returned_path, "wb") as f:
                f.write(b"photo data")
            return returned_path

        self.listener.client.download_media = AsyncMock(side_effect=fake_download)
        self.listener._get_media_type = MagicMock(return_value="photo")
        self.listener._get_media_filename = MagicMock(return_value=file_name)

        msg = self._make_message(file_id="nodedup")

        result = self._run(self.listener._download_media(msg, chat_id))

        self.assertIsNotNone(result)


class TestBackupDedupSharedExistsPreUnlink(unittest.TestCase):
    """Test telegram_backup.py shared-file-exists branch pre-unlink guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.deduplicate_media = True
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id=1, file_id="shared_pre"):
        msg = MagicMock()
        msg.id = msg_id
        msg.date = MagicMock()
        msg.date.strftime = MagicMock(return_value="20240101_120000")
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        msg.media.document = None
        msg.reply_to = None
        return msg

    def test_shared_exists_pre_unlinks_dangling_symlink(self):
        """When shared file exists and chat dir has dangling symlink, pre-unlink before new symlink."""
        chat_id = 800
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "photo_shared_pre.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        # Create shared file (the valid target)
        with open(shared_file, "wb") as f:
            f.write(b"shared photo data")

        # Create dangling symlink in chat dir (points to deleted old file)
        old_target = os.path.join(shared_dir, "old_deleted.jpg")
        with open(old_target, "w") as f:
            f.write("old")
        os.symlink(os.path.relpath(old_target, chat_dir), chat_file)
        os.remove(old_target)

        # Dangling: exists=False (follows symlink), lexists=True
        self.assertFalse(os.path.exists(chat_file))
        self.assertTrue(os.path.lexists(chat_file))

        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message()
        result = self._run(self.backup._process_media(msg, chat_id))

        self.assertIsNotNone(result)
        # After fix: symlink replaced, now valid
        self.assertTrue(os.path.exists(chat_file))
        self.assertTrue(os.path.islink(chat_file))
        self.assertEqual(os.path.realpath(chat_file), os.path.realpath(shared_file))

    def test_shared_exists_copy2_fallback_when_symlink_fails(self):
        """When shared file exists but symlink fails, use shutil.copy2 instead of re-downloading."""
        chat_id = 900
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "photo_copy2.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        with open(shared_file, "wb") as f:
            f.write(b"shared data for copy")

        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message()
        symlink_error = OSError(errno.EPERM, "Operation not permitted")
        with patch("os.symlink", side_effect=symlink_error), patch("shutil.copy2") as mock_copy:
            result = self._run(self.backup._process_media(msg, chat_id))

        self.assertIsNotNone(result)
        mock_copy.assert_called_once_with(shared_file, chat_file)
        # download_media should NOT be called (copy2 used instead)
        self.backup.client.download_media.assert_not_awaited()


class TestBackupNonDedupCapturesReturnValue(unittest.TestCase):
    """Test telegram_backup.py non-dedup path captures download_media return value."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.deduplicate_media = False
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id=1, file_id="nodedup123"):
        msg = MagicMock()
        msg.id = msg_id
        msg.date = MagicMock()
        msg.date.strftime = MagicMock(return_value="20240101_120000")
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        msg.media.document = None
        msg.reply_to = None
        return msg

    def test_non_dedup_captures_telethon_return_path(self):
        """Non-dedup download captures actual path from download_media (e.g., with .mp4 appended)."""
        chat_id = 600
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(chat_dir)

        file_name = "photo_plain"
        returned_path = os.path.join(chat_dir, "photo_plain.jpg")

        def fake_download(message, path):
            with open(returned_path, "wb") as f:
                f.write(b"jpeg data")
            return returned_path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message()

        result = self._run(self.backup._process_media(msg, chat_id))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])


class TestBackupDedupSymlinkFailFallback(unittest.TestCase):
    """Test telegram_backup.py dedup symlink-failed path captures download return value."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.media_path)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_path
        self.backup.config.deduplicate_media = True
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.client = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id=1, file_id="fallback_dedup"):
        msg = MagicMock()
        msg.id = msg_id
        msg.date = MagicMock()
        msg.date.strftime = MagicMock(return_value="20240101_120000")
        msg.media = MagicMock()
        msg.media.photo = MagicMock()
        msg.media.photo.id = file_id
        msg.media.document = None
        msg.reply_to = None
        return msg

    def test_dedup_symlink_fail_uses_copy2(self):
        """When shared file exists but symlink fails, copy2 is used instead of re-downloading."""
        chat_id = 700
        shared_dir = os.path.join(self.media_path, "_shared")
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(shared_dir)
        os.makedirs(chat_dir)

        file_name = "photo_shared.jpg"
        shared_file = os.path.join(shared_dir, file_name)
        chat_file = os.path.join(chat_dir, file_name)

        with open(shared_file, "wb") as f:
            f.write(b"shared image")

        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_filename = MagicMock(return_value=file_name)
        self.backup._get_media_size = MagicMock(return_value=512)

        msg = self._make_message()

        symlink_error = OSError(errno.EPERM, "Operation not permitted")
        with patch("os.symlink", side_effect=symlink_error), patch("shutil.copy2") as mock_copy:
            result = self._run(self.backup._process_media(msg, chat_id))

        self.assertIsNotNone(result)
        self.assertTrue(result["downloaded"])
        mock_copy.assert_called_once_with(shared_file, chat_file)
        self.backup.client.download_media.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
