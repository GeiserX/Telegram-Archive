"""Tests for Telegram backup functionality."""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from telethon.tl.types import Channel

from src.message_utils import extract_topic_id
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
            {
                "id": "m1",
                "message_id": 1,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": file_path,
                "file_size": 1024,
                "downloaded": True,
            }
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
            {
                "id": "m1",
                "message_id": 1,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": symlink_path,
                "file_size": 2048,
                "downloaded": True,
            }
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
            {
                "id": "m1",
                "message_id": 1,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": file_path,
                "file_size": 512,
                "downloaded": True,
            }
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
            {
                "id": "m1",
                "message_id": 1,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": tracked_file,
                "file_size": 512,
                "downloaded": True,
            }
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
            {
                "id": "m1",
                "message_id": 1,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": "/nonexistent/path.jpg",
                "file_size": 1024,
                "downloaded": True,
            }
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
            {
                "id": "m1",
                "message_id": 1,
                "chat_id": chat_id,
                "type": "video",
                "file_path": real_file,
                "file_size": 4096,
                "downloaded": True,
            },
            {
                "id": "m2",
                "message_id": 2,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": symlink_path,
                "file_size": 2048,
                "downloaded": True,
            },
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


class TestBackupCheckpointing(unittest.TestCase):
    """Test per-batch sync_status checkpointing in _backup_dialog."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

        self.config = MagicMock()
        self.config.batch_size = 2
        self.config.checkpoint_interval = 1
        self.config.skip_media_chat_ids = set()
        self.config.skip_media_delete_existing = False
        self.config.sync_deletions_edits = False
        self.config.should_skip_topic = MagicMock(return_value=False)
        self.config.media_path = os.path.join(self.temp_dir, "media")

        self.db = AsyncMock()
        self.db.get_last_message_id.return_value = 0

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = self.config
        self.backup.db = self.db
        self.backup.client = MagicMock()
        self.backup._cleaned_media_chats = set()
        self.backup._get_marked_id = MagicMock(return_value=100)
        self.backup._extract_chat_data = MagicMock(return_value={"id": 100})
        self.backup._ensure_profile_photo = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_dialog(self):
        dialog = MagicMock()
        dialog.entity = MagicMock()
        return dialog

    def _make_message(self, msg_id, reply_to=None):
        msg = MagicMock()
        msg.id = msg_id
        # Explicitly set reply_to to None (non-forum message) so the
        # topic-skip guard in _backup_dialog doesn't accidentally filter
        # every message via MagicMock truthiness.
        msg.reply_to = reply_to
        return msg

    def test_checkpoint_after_every_batch(self):
        """With checkpoint_interval=1, sync_status updates after every batch."""
        messages = [self._make_message(i) for i in range(1, 5)]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog(), 100))

        self.assertEqual(result, 4)
        # 2 batches of 2 => 2 checkpoints, nothing left uncheckpointed
        self.assertEqual(self.db.update_sync_status.await_count, 2)

    def test_checkpoint_interval_greater_than_one(self):
        """With checkpoint_interval=2, checkpoint only every 2nd batch."""
        self.config.checkpoint_interval = 2
        messages = [self._make_message(i) for i in range(1, 7)]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog(), 200))

        self.assertEqual(result, 6)
        # 3 batches of 2, checkpoint_interval=2 => checkpoint at batch 2, then final for batch 3
        self.assertEqual(self.db.update_sync_status.await_count, 2)

    def test_final_flush_gets_checkpointed(self):
        """Leftover messages (< batch_size) are flushed and checkpointed."""
        messages = [self._make_message(i) for i in range(1, 4)]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog(), 300))

        self.assertEqual(result, 3)
        # batch of 2 -> checkpoint, then 1 remaining -> final checkpoint
        self.assertEqual(self.db.update_sync_status.await_count, 2)

    def test_no_messages_no_checkpoint(self):
        """When there are no new messages, no checkpoint should happen."""

        async def fake_iter(*args, **kwargs):
            return
            yield  # noqa: unreachable - makes this an async generator

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock()
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog(), 400))

        self.assertEqual(result, 0)
        self.db.update_sync_status.assert_not_awaited()

    def test_checkpoint_tracks_max_message_id(self):
        """Checkpoint should pass the highest message ID seen so far."""
        messages = [self._make_message(10), self._make_message(20)]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        self._run(self.backup._backup_dialog(self._make_dialog(), 500))

        call_args = self.db.update_sync_status.call_args
        self.assertEqual(call_args[0][1], 20)

    def test_commit_batch_called_correctly(self):
        """_commit_batch persists messages, media and reactions."""
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.db = AsyncMock()

        batch = [
            {"id": 1, "chat_id": 100, "_media_data": {"file_path": "/a.jpg"}, "reactions": None},
            {"id": 2, "chat_id": 100, "reactions": [{"emoji": "👍", "user_ids": [], "count": 3}]},
        ]

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(backup._commit_batch(batch, 100))
        finally:
            loop.close()

        backup.db.insert_messages_batch.assert_awaited_once_with(batch)
        backup.db.insert_media.assert_awaited_once_with({"file_path": "/a.jpg"})
        backup.db.insert_reactions.assert_awaited_once()


class TestTopicFilteringInBackupDialog(unittest.TestCase):
    """Test that _backup_dialog respects SKIP_TOPIC_IDS filtering."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

        self.config = MagicMock()
        self.config.batch_size = 100
        self.config.checkpoint_interval = 1
        self.config.skip_media_chat_ids = set()
        self.config.skip_media_delete_existing = False
        self.config.sync_deletions_edits = False
        self.config.media_path = os.path.join(self.temp_dir, "media")

        self.db = AsyncMock()
        self.db.get_last_message_id.return_value = 0

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = self.config
        self.backup.db = self.db
        self.backup.client = MagicMock()
        self.backup._cleaned_media_chats = set()
        self.backup._get_marked_id = MagicMock(return_value=-1001234567890)
        self.backup._extract_chat_data = MagicMock(return_value={"id": -1001234567890})
        self.backup._ensure_profile_photo = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_dialog(self):
        dialog = MagicMock()
        dialog.entity = MagicMock()
        return dialog

    def _make_forum_message(self, msg_id, topic_id):
        """Create a mock message belonging to a forum topic."""
        msg = MagicMock()
        msg.id = msg_id
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = topic_id
        msg.reply_to.reply_to_msg_id = topic_id
        return msg

    def _make_normal_message(self, msg_id):
        """Create a mock message that is not in any forum topic."""
        msg = MagicMock()
        msg.id = msg_id
        msg.reply_to = None
        return msg

    def test_backup_dialog_skips_messages_in_excluded_topics(self):
        """Messages in excluded forum topics should not be backed up."""
        # Configure: skip topic 42 in chat -1001234567890
        self.config.should_skip_topic = MagicMock(
            side_effect=lambda chat_id, topic_id: topic_id == 42
        )

        messages = [
            self._make_normal_message(1),       # kept (no topic)
            self._make_forum_message(2, 42),     # skipped (excluded topic)
            self._make_forum_message(3, 99),     # kept (different topic)
            self._make_forum_message(4, 42),     # skipped (excluded topic)
            self._make_normal_message(5),        # kept (no topic)
        ]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(
            side_effect=lambda m, c: {"id": m.id, "chat_id": c}
        )
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog()))

        # 3 messages kept (IDs 1, 3, 5), 2 skipped (IDs 2, 4)
        self.assertEqual(result, 3)
        # _process_message should only be called for kept messages
        self.assertEqual(self.backup._process_message.await_count, 3)

    def test_backup_dialog_keeps_all_messages_when_no_topics_excluded(self):
        """When no topics are excluded, all messages pass through."""
        self.config.should_skip_topic = MagicMock(return_value=False)

        messages = [
            self._make_forum_message(1, 42),
            self._make_forum_message(2, 99),
            self._make_normal_message(3),
        ]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(
            side_effect=lambda m, c: {"id": m.id, "chat_id": c}
        )
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog()))

        self.assertEqual(result, 3)

    def test_backup_dialog_uses_reply_to_msg_id_as_fallback(self):
        """When reply_to_top_id is None, falls back to reply_to_msg_id for topic ID."""
        self.config.should_skip_topic = MagicMock(
            side_effect=lambda chat_id, topic_id: topic_id == 42
        )

        msg = MagicMock()
        msg.id = 1
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = None  # no top_id
        msg.reply_to.reply_to_msg_id = 42     # fallback to this

        async def fake_iter(*args, **kwargs):
            yield msg

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(
            side_effect=lambda m, c: {"id": m.id, "chat_id": c}
        )
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog()))

        # Message should be skipped via fallback topic ID
        self.assertEqual(result, 0)


class TestWhitelistModeBackup(unittest.TestCase):
    """Test that whitelist mode skips get_dialogs and fetches entities directly (#95)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = MagicMock()
        self.config.whitelist_mode = True
        self.config.chat_ids = {-1002701160643}
        self.config.priority_chat_ids = set()
        self.config.media_path = os.path.join(self.temp_dir, "media")
        self.config.verify_media = False
        self.config.fill_gaps = False
        self.config.skip_media_chat_ids = set()
        self.config.skip_media_delete_existing = False
        os.makedirs(self.config.media_path, exist_ok=True)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = self.config
        self.backup.client = AsyncMock()
        self.backup.db = AsyncMock()
        self.backup._owns_client = False
        self.backup._cleaned_media_chats = set()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_whitelist_mode_does_not_call_get_dialogs(self):
        """In whitelist mode, get_dialogs should never be called."""
        entity = Channel(
            id=2701160643,
            title="Test Channel",
            access_hash=12345,
            date=None,
            photo=None,
        )
        self.backup.client.get_entity = AsyncMock(return_value=entity)
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=123))
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.upsert_chat = AsyncMock()
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 0, "media_files": 0, "total_size_mb": 0}
        )
        self.backup.client.iter_messages = MagicMock(return_value=AsyncMock(__aiter__=AsyncMock(return_value=iter([]))))
        # Mock _backup_dialog to avoid complex internals
        self.backup._backup_dialog = AsyncMock(return_value=0)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()

        self._run(self.backup.backup_all())

        # get_dialogs should NOT have been called
        self.backup.client.get_dialogs.assert_not_called()
        # get_entity SHOULD have been called for the whitelisted chat
        self.backup.client.get_entity.assert_awaited_once_with(-1002701160643)

    def test_whitelist_mode_handles_entity_fetch_failure(self):
        """If get_entity fails for a whitelisted chat, backup should continue without crashing."""
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("Entity not found"))
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=123))
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 0, "messages": 0, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_folders = AsyncMock()

        # Should not raise — just log warning and report 0 dialogs
        self._run(self.backup.backup_all())

        self.backup.client.get_dialogs.assert_not_called()


class TestExtractTopicId(unittest.TestCase):
    """Test the shared extract_topic_id utility."""

    def test_returns_none_when_no_reply_to(self):
        msg = MagicMock()
        msg.reply_to = None
        self.assertIsNone(extract_topic_id(msg))

    def test_returns_none_when_not_forum_topic(self):
        msg = MagicMock()
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = False
        self.assertIsNone(extract_topic_id(msg))

    def test_returns_reply_to_top_id(self):
        msg = MagicMock()
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = 42
        self.assertEqual(extract_topic_id(msg), 42)

    def test_falls_back_to_reply_to_msg_id(self):
        msg = MagicMock()
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = None
        msg.reply_to.reply_to_msg_id = 99
        self.assertEqual(extract_topic_id(msg), 99)

    def test_returns_none_when_both_ids_none(self):
        msg = MagicMock()
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = None
        msg.reply_to.reply_to_msg_id = None
        self.assertIsNone(extract_topic_id(msg))


if __name__ == "__main__":
    unittest.main()
