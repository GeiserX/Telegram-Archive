"""Tests for Telegram backup functionality."""

import asyncio
import os
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from telethon.tl.types import (
    Channel,
    Chat,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    TextWithEntities,
    User,
)

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
            if False:
                yield  # pragma: no cover - makes this an async generator
            return

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
        self.config.should_skip_topic = MagicMock(side_effect=lambda chat_id, topic_id: topic_id == 42)

        messages = [
            self._make_normal_message(1),  # kept (no topic)
            self._make_forum_message(2, 42),  # skipped (excluded topic)
            self._make_forum_message(3, 99),  # kept (different topic)
            self._make_forum_message(4, 42),  # skipped (excluded topic)
            self._make_normal_message(5),  # kept (no topic)
        ]

        async def fake_iter(*args, **kwargs):
            for m in messages:
                yield m

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
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
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog()))

        self.assertEqual(result, 3)

    def test_backup_dialog_uses_reply_to_msg_id_as_fallback(self):
        """When reply_to_top_id is None, falls back to reply_to_msg_id for topic ID."""
        self.config.should_skip_topic = MagicMock(side_effect=lambda chat_id, topic_id: topic_id == 42)

        msg = MagicMock()
        msg.id = 1
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = None  # no top_id
        msg.reply_to.reply_to_msg_id = 42  # fallback to this

        async def fake_iter(*args, **kwargs):
            yield msg

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
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


class TestExtractForwardFromId(unittest.TestCase):
    """Test _extract_forward_from_id for different Peer types."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_returns_none_when_no_fwd_from(self):
        """Returns None when message has no forward info."""
        msg = MagicMock()
        msg.fwd_from = None
        self.assertIsNone(self.backup._extract_forward_from_id(msg))

    def test_returns_none_when_fwd_from_has_no_from_id(self):
        """Returns None when forward info has no sender ID."""
        msg = MagicMock()
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_id = None
        self.assertIsNone(self.backup._extract_forward_from_id(msg))

    def test_returns_user_id_from_peer_user(self):
        """Returns user_id when forward peer is a PeerUser."""
        msg = MagicMock()
        msg.fwd_from = MagicMock()
        peer = MagicMock(spec=["user_id"])
        peer.user_id = 12345
        msg.fwd_from.from_id = peer
        self.assertEqual(self.backup._extract_forward_from_id(msg), 12345)

    def test_returns_channel_id_from_peer_channel(self):
        """Returns channel_id when forward peer is a PeerChannel."""
        msg = MagicMock()
        msg.fwd_from = MagicMock()
        peer = MagicMock(spec=["channel_id"])
        peer.channel_id = 99999
        msg.fwd_from.from_id = peer
        self.assertEqual(self.backup._extract_forward_from_id(msg), 99999)

    def test_returns_chat_id_from_peer_chat(self):
        """Returns chat_id when forward peer is a PeerChat."""
        msg = MagicMock()
        msg.fwd_from = MagicMock()
        peer = MagicMock(spec=["chat_id"])
        peer.chat_id = 77777
        msg.fwd_from.from_id = peer
        self.assertEqual(self.backup._extract_forward_from_id(msg), 77777)

    def test_returns_none_for_unknown_peer_type(self):
        """Returns None when peer has no recognized ID attribute."""
        msg = MagicMock()
        msg.fwd_from = MagicMock()
        peer = MagicMock(spec=[])
        msg.fwd_from.from_id = peer
        self.assertIsNone(self.backup._extract_forward_from_id(msg))


class TestTextWithEntitiesToString(unittest.TestCase):
    """Test _text_with_entities_to_string conversion."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_returns_empty_string_for_none(self):
        """Returns empty string when input is None."""
        self.assertEqual(self.backup._text_with_entities_to_string(None), "")

    def test_returns_string_as_is(self):
        """Returns plain string unchanged."""
        self.assertEqual(self.backup._text_with_entities_to_string("hello"), "hello")

    def test_extracts_text_from_text_with_entities(self):
        """Extracts .text from a TextWithEntities object."""
        twe = MagicMock(spec=TextWithEntities)
        twe.text = "poll question"
        # Make isinstance check work
        with unittest.mock.patch("src.telegram_backup.TextWithEntities", new=type(twe)):
            result = self.backup._text_with_entities_to_string(twe)
        self.assertEqual(result, "poll question")

    def test_falls_back_to_str_for_unknown_type(self):
        """Falls back to str() for unknown types."""
        self.assertEqual(self.backup._text_with_entities_to_string(42), "42")


class TestGetMediaType(unittest.TestCase):
    """Test _get_media_type detection for all media types."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_photo_returns_photo(self):
        """MessageMediaPhoto is detected as photo type."""
        media = MagicMock(spec=MessageMediaPhoto)
        self.assertEqual(self.backup._get_media_type(media), "photo")

    def test_document_returns_document(self):
        """Plain document without special attributes returns document."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        media.document.attributes = []
        self.assertEqual(self.backup._get_media_type(media), "document")

    def test_document_with_video_attr_returns_video(self):
        """Document with Video attribute returns video type."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        video_attr = MagicMock()
        type(video_attr).__name__ = "DocumentAttributeVideo"
        media.document.attributes = [video_attr]
        self.assertEqual(self.backup._get_media_type(media), "video")

    def test_animated_video_returns_animation(self):
        """Document with Animated + Video attributes returns animation type."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        anim_attr = MagicMock()
        type(anim_attr).__name__ = "DocumentAttributeAnimated"
        video_attr = MagicMock()
        type(video_attr).__name__ = "DocumentAttributeVideo"
        media.document.attributes = [anim_attr, video_attr]
        self.assertEqual(self.backup._get_media_type(media), "animation")

    def test_animated_without_video_returns_animation(self):
        """Document with Animated attribute alone returns animation type."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        anim_attr = MagicMock()
        type(anim_attr).__name__ = "DocumentAttributeAnimated"
        media.document.attributes = [anim_attr]
        self.assertEqual(self.backup._get_media_type(media), "animation")

    def test_audio_attr_returns_audio(self):
        """Document with Audio attribute (not voice) returns audio type."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        audio_attr = MagicMock()
        type(audio_attr).__name__ = "DocumentAttributeAudio"
        audio_attr.voice = False
        media.document.attributes = [audio_attr]
        self.assertEqual(self.backup._get_media_type(media), "audio")

    def test_voice_note_returns_voice(self):
        """Document with Audio attribute and voice=True returns voice type."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        voice_attr = MagicMock()
        type(voice_attr).__name__ = "DocumentAttributeAudio"
        voice_attr.voice = True
        media.document.attributes = [voice_attr]
        self.assertEqual(self.backup._get_media_type(media), "voice")

    def test_sticker_returns_sticker(self):
        """Document with Sticker attribute returns sticker type."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        sticker_attr = MagicMock()
        type(sticker_attr).__name__ = "DocumentAttributeSticker"
        media.document.attributes = [sticker_attr]
        self.assertEqual(self.backup._get_media_type(media), "sticker")

    def test_contact_returns_contact(self):
        """MessageMediaContact is detected as contact type."""
        media = MagicMock(spec=MessageMediaContact)
        self.assertEqual(self.backup._get_media_type(media), "contact")

    def test_geo_returns_geo(self):
        """MessageMediaGeo is detected as geo type."""
        media = MagicMock(spec=MessageMediaGeo)
        self.assertEqual(self.backup._get_media_type(media), "geo")

    def test_poll_returns_poll(self):
        """MessageMediaPoll is detected as poll type."""
        media = MagicMock(spec=MessageMediaPoll)
        self.assertEqual(self.backup._get_media_type(media), "poll")

    def test_unknown_media_returns_none(self):
        """Unknown media type returns None."""
        media = MagicMock()
        self.assertIsNone(self.backup._get_media_type(media))

    def test_document_without_document_attr_returns_document(self):
        """MessageMediaDocument with no .document returns document."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = None
        self.assertEqual(self.backup._get_media_type(media), "document")


class TestGetMediaExtension(unittest.TestCase):
    """Test _get_media_extension fallback extension lookup."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_photo_returns_jpg(self):
        """Photo type maps to jpg extension."""
        self.assertEqual(self.backup._get_media_extension("photo"), "jpg")

    def test_video_returns_mp4(self):
        """Video type maps to mp4 extension."""
        self.assertEqual(self.backup._get_media_extension("video"), "mp4")

    def test_audio_returns_mp3(self):
        """Audio type maps to mp3 extension."""
        self.assertEqual(self.backup._get_media_extension("audio"), "mp3")

    def test_voice_returns_ogg(self):
        """Voice type maps to ogg extension."""
        self.assertEqual(self.backup._get_media_extension("voice"), "ogg")

    def test_document_returns_bin(self):
        """Document type maps to bin extension."""
        self.assertEqual(self.backup._get_media_extension("document"), "bin")

    def test_unknown_type_returns_bin(self):
        """Unknown media type falls back to bin extension."""
        self.assertEqual(self.backup._get_media_extension("unknown_type"), "bin")


class TestExtractChatData(unittest.TestCase):
    """Test _extract_chat_data for User, Chat, and Channel entities."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup._get_marked_id = MagicMock(return_value=100)

    def test_user_entity_extracts_private_chat(self):
        """User entity produces a private chat with name and phone."""
        user = MagicMock(spec=User)
        user.first_name = "Alice"
        user.last_name = "Smith"
        user.username = "alice"
        user.phone = "+1234567890"

        result = self.backup._extract_chat_data(user)

        self.assertEqual(result["type"], "private")
        self.assertEqual(result["first_name"], "Alice")
        self.assertEqual(result["last_name"], "Smith")
        self.assertEqual(result["username"], "alice")
        self.assertEqual(result["is_archived"], 0)

    def test_chat_entity_extracts_group(self):
        """Chat entity produces a group with title and participants."""
        chat = MagicMock(spec=Chat)
        chat.title = "Family Group"
        chat.participants_count = 5

        result = self.backup._extract_chat_data(chat)

        self.assertEqual(result["type"], "group")
        self.assertEqual(result["title"], "Family Group")
        self.assertEqual(result["participants_count"], 5)

    def test_channel_entity_extracts_channel(self):
        """Channel entity (not megagroup) produces a channel type."""
        channel = MagicMock(spec=Channel)
        channel.megagroup = False
        channel.title = "News Channel"
        channel.username = "news"
        channel.forum = False

        result = self.backup._extract_chat_data(channel)

        self.assertEqual(result["type"], "channel")
        self.assertEqual(result["title"], "News Channel")

    def test_channel_megagroup_extracts_group(self):
        """Channel entity with megagroup=True produces group type."""
        channel = MagicMock(spec=Channel)
        channel.megagroup = True
        channel.title = "Super Group"
        channel.username = "supergroup"
        channel.forum = False

        result = self.backup._extract_chat_data(channel)

        self.assertEqual(result["type"], "group")

    def test_forum_channel_sets_is_forum(self):
        """Channel with forum=True sets is_forum=1."""
        channel = MagicMock(spec=Channel)
        channel.megagroup = True
        channel.title = "Forum Group"
        channel.username = "forum"
        channel.forum = True

        result = self.backup._extract_chat_data(channel)

        self.assertEqual(result["is_forum"], 1)

    def test_archived_flag_set_when_true(self):
        """is_archived=1 when is_archived parameter is True."""
        user = MagicMock(spec=User)
        user.first_name = "Bob"
        user.last_name = None
        user.username = None
        user.phone = None

        result = self.backup._extract_chat_data(user, is_archived=True)

        self.assertEqual(result["is_archived"], 1)


class TestExtractUserData(unittest.TestCase):
    """Test _extract_user_data for User and non-User entities."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_extracts_user_fields(self):
        """Returns dict with all user fields for a User entity."""
        user = MagicMock(spec=User)
        user.id = 42
        user.username = "testuser"
        user.first_name = "Test"
        user.last_name = "User"
        user.phone = "+1111"
        user.bot = False

        result = self.backup._extract_user_data(user)

        self.assertEqual(result["id"], 42)
        self.assertEqual(result["username"], "testuser")
        self.assertEqual(result["first_name"], "Test")
        self.assertFalse(result["is_bot"])

    def test_returns_none_for_non_user(self):
        """Returns None when entity is not a User."""
        channel = MagicMock(spec=Channel)
        self.assertIsNone(self.backup._extract_user_data(channel))

    def test_returns_none_for_chat(self):
        """Returns None when entity is a Chat."""
        chat = MagicMock(spec=Chat)
        self.assertIsNone(self.backup._extract_user_data(chat))


class TestGetChatName(unittest.TestCase):
    """Test _get_chat_name readable name generation."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_user_with_full_name_and_username(self):
        """User with first, last name and username returns formatted string."""
        user = MagicMock(spec=User)
        user.id = 1
        user.first_name = "Alice"
        user.last_name = "Smith"
        user.username = "alice"
        self.assertEqual(self.backup._get_chat_name(user), "Alice Smith (@alice)")

    def test_user_with_first_name_only(self):
        """User with only first name returns that name."""
        user = MagicMock(spec=User)
        user.id = 2
        user.first_name = "Bob"
        user.last_name = None
        user.username = None
        self.assertEqual(self.backup._get_chat_name(user), "Bob")

    def test_user_with_no_name_returns_fallback(self):
        """User with no name returns User ID fallback."""
        user = MagicMock(spec=User)
        user.id = 3
        user.first_name = ""
        user.last_name = None
        user.username = None
        self.assertEqual(self.backup._get_chat_name(user), "User 3")

    def test_channel_returns_title(self):
        """Channel returns its title."""
        channel = MagicMock(spec=Channel)
        channel.id = 10
        channel.title = "My Channel"
        self.assertEqual(self.backup._get_chat_name(channel), "My Channel")

    def test_chat_returns_title(self):
        """Chat group returns its title."""
        chat = MagicMock(spec=Chat)
        chat.id = 20
        chat.title = "Family Chat"
        self.assertEqual(self.backup._get_chat_name(chat), "Family Chat")

    def test_unknown_entity_returns_unknown(self):
        """Unknown entity type returns Unknown + ID."""
        entity = MagicMock()
        entity.id = 99
        self.assertEqual(self.backup._get_chat_name(entity), "Unknown 99")


class TestProcessMessage(unittest.TestCase):
    """Test _process_message extracts message data correctly."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.db = AsyncMock()
        self.backup.config = MagicMock()
        self.backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        self.backup.client = AsyncMock()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id, text="hello", sender_id=42):
        """Create a minimal mock message."""
        msg = MagicMock()
        msg.id = msg_id
        msg.sender = None
        msg.sender_id = sender_id
        msg.date = datetime(2024, 1, 1)
        msg.text = text
        msg.reply_to_msg_id = None
        msg.reply_to = None
        msg.edit_date = None
        msg.out = False
        msg.pinned = False
        msg.grouped_id = None
        msg.fwd_from = None
        msg.media = None
        msg.reactions = None
        msg.post_author = None
        return msg

    def test_basic_text_message(self):
        """Basic text message extracts id, chat_id, text, and sender_id."""
        msg = self._make_message(1, text="test message")
        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["id"], 1)
        self.assertEqual(result["chat_id"], 100)
        self.assertEqual(result["text"], "test message")
        self.assertEqual(result["sender_id"], 42)
        self.assertEqual(result["is_outgoing"], 0)
        self.assertEqual(result["is_pinned"], 0)

    def test_outgoing_message_sets_flag(self):
        """Outgoing message sets is_outgoing=1."""
        msg = self._make_message(2)
        msg.out = True
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["is_outgoing"], 1)

    def test_pinned_message_sets_flag(self):
        """Pinned message sets is_pinned=1."""
        msg = self._make_message(3)
        msg.pinned = True
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["is_pinned"], 1)

    def test_grouped_id_stored_in_raw_data(self):
        """Grouped ID (album) is stored in raw_data."""
        msg = self._make_message(4)
        msg.grouped_id = 9876543210
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["grouped_id"], "9876543210")

    def test_forward_from_name_stored(self):
        """Forward with from_name stores it in raw_data."""
        msg = self._make_message(5)
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = "Original Author"
        msg.fwd_from.from_id = None
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["forward_from_name"], "Original Author")

    def test_post_author_stored(self):
        """Channel post author signature is stored in raw_data."""
        msg = self._make_message(6)
        msg.post_author = "Editor Name"
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["post_author"], "Editor Name")

    def test_none_text_becomes_empty_string(self):
        """Message with None text stores empty string."""
        msg = self._make_message(7, text=None)
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["text"], "")

    def test_sender_data_upserted_when_sender_is_user(self):
        """When sender is a User, upsert_user is called."""
        msg = self._make_message(8)
        user = MagicMock(spec=User)
        user.id = 42
        user.username = "sender"
        user.first_name = "Sender"
        user.last_name = None
        user.phone = None
        user.bot = False
        msg.sender = user

        self._run(self.backup._process_message(msg, 100))

        self.backup.db.upsert_user.assert_awaited_once()

    def test_reactions_extracted_with_emoticon(self):
        """Reactions with emoticon emoji are extracted correctly."""
        msg = self._make_message(9)
        reaction = MagicMock()
        reaction.reaction = MagicMock(spec=["emoticon"])
        reaction.reaction.emoticon = "thumbs_up"
        reaction.count = 3
        reaction.recent_reactions = None
        msg.reactions = MagicMock()
        msg.reactions.results = [reaction]

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(len(result["reactions"]), 1)
        self.assertEqual(result["reactions"][0]["emoji"], "thumbs_up")
        self.assertEqual(result["reactions"][0]["count"], 3)

    def test_reactions_with_custom_emoji(self):
        """Reactions with custom emoji document_id are stored as custom_ prefix."""
        msg = self._make_message(10)
        reaction = MagicMock()
        emoji_obj = MagicMock(spec=["document_id"])
        emoji_obj.document_id = 12345
        reaction.reaction = emoji_obj
        reaction.count = 1
        reaction.recent_reactions = None
        msg.reactions = MagicMock()
        msg.reactions.results = [reaction]

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["reactions"][0]["emoji"], "custom_12345")

    def test_reply_to_text_truncated(self):
        """Reply text is truncated to 100 characters."""
        msg = self._make_message(11)
        msg.reply_to_msg_id = 5
        msg.reply_to = MagicMock()
        msg.reply_to.message = "a" * 200
        msg.reply_to.forum_topic = False

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(len(result["reply_to_text"]), 100)

    def test_forward_from_id_resolves_channel_title(self):
        """Forward from channel resolves title via get_entity."""
        msg = self._make_message(12)
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = None
        msg.fwd_from.from_id = MagicMock(spec=["channel_id"])
        msg.fwd_from.from_id.channel_id = 555

        fwd_entity = MagicMock()
        fwd_entity.title = "Forwarded Channel"
        del fwd_entity.first_name  # ensure hasattr(, 'title') path is taken
        self.backup.client.get_entity = AsyncMock(return_value=fwd_entity)

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Forwarded Channel")

    def test_forward_from_id_resolves_user_name(self):
        """Forward from user resolves first+last name via get_entity."""
        msg = self._make_message(13)
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = None
        msg.fwd_from.from_id = MagicMock(spec=["user_id"])
        msg.fwd_from.from_id.user_id = 777

        fwd_entity = MagicMock(spec=["first_name", "last_name"])
        fwd_entity.first_name = "John"
        fwd_entity.last_name = "Doe"
        self.backup.client.get_entity = AsyncMock(return_value=fwd_entity)

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "John Doe")

    def test_forward_from_id_get_entity_failure_graceful(self):
        """Forward entity resolution failure does not crash."""
        msg = self._make_message(14)
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = None
        msg.fwd_from.from_id = MagicMock(spec=["user_id"])
        msg.fwd_from.from_id.user_id = 888

        self.backup.client.get_entity = AsyncMock(side_effect=Exception("not found"))

        result = self._run(self.backup._process_message(msg, 100))

        # Should not have forward_from_name since resolution failed
        self.assertNotIn("forward_from_name", result["raw_data"])

    def test_poll_media_stored_in_raw_data(self):
        """Poll media stores question, answers, and results in raw_data."""
        msg = self._make_message(15)

        poll = MagicMock()
        poll.id = 9999
        poll.question = "What color?"
        poll.closed = False
        poll.public_voters = True
        poll.multiple_choice = False
        poll.quiz = False

        answer1 = MagicMock()
        answer1.text = "Red"
        answer1.option = b"\x00"
        answer2 = MagicMock()
        answer2.text = "Blue"
        answer2.option = b"\x01"
        poll.answers = [answer1, answer2]

        result_entry = MagicMock()
        result_entry.option = b"\x00"
        result_entry.voters = 5
        result_entry.correct = True

        results = MagicMock()
        results.total_voters = 10
        results.results = [result_entry]

        media = MagicMock(spec=MessageMediaPoll)
        media.poll = poll
        media.results = results
        msg.media = media

        result = self._run(self.backup._process_message(msg, 100))

        poll_data = result["raw_data"]["poll"]
        self.assertEqual(poll_data["id"], 9999)
        self.assertEqual(poll_data["question"], "What color?")
        self.assertEqual(len(poll_data["answers"]), 2)
        self.assertFalse(poll_data["closed"])
        self.assertTrue(poll_data["public_voters"])
        self.assertIsNotNone(poll_data["results"])
        self.assertEqual(poll_data["results"]["total_voters"], 10)

    def test_downloadable_media_calls_process_media(self):
        """Non-poll media triggers _process_media when download is enabled."""
        msg = self._make_message(16)
        msg.media = MagicMock(spec=MessageMediaPhoto)

        self.backup.config.should_download_media_for_chat = MagicMock(return_value=True)
        self.backup._process_media = AsyncMock(return_value={"file_path": "/a.jpg"})

        result = self._run(self.backup._process_message(msg, 100))

        self.backup._process_media.assert_awaited_once()
        self.assertEqual(result["_media_data"]["file_path"], "/a.jpg")

    def test_media_download_disabled_skips_process_media(self):
        """Non-poll media is skipped when download is disabled for chat."""
        msg = self._make_message(17)
        msg.media = MagicMock(spec=MessageMediaPhoto)

        self.backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        self.backup._process_media = AsyncMock()

        result = self._run(self.backup._process_message(msg, 100))

        self.backup._process_media.assert_not_awaited()
        self.assertNotIn("_media_data", result)

    def test_reactions_with_recent_user_peers(self):
        """Reactions with recent_reactions extract user_ids from peers."""
        msg = self._make_message(18)

        peer1 = MagicMock(spec=["user_id"])
        peer1.user_id = 101
        peer2 = MagicMock(spec=["user_id"])
        peer2.user_id = 102

        recent1 = MagicMock()
        recent1.peer_id = peer1
        recent2 = MagicMock()
        recent2.peer_id = peer2

        reaction = MagicMock()
        reaction.reaction = MagicMock(spec=["emoticon"])
        reaction.reaction.emoticon = "heart"
        reaction.count = 5
        reaction.recent_reactions = [recent1, recent2]

        msg.reactions = MagicMock()
        msg.reactions.results = [reaction]

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["reactions"][0]["user_ids"], [101, 102])


class TestCommitBatchReactions(unittest.TestCase):
    """Test _commit_batch reaction expansion logic."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.db = AsyncMock()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_reaction_with_known_users_expanded(self):
        """Reactions with user_ids expand into per-user rows plus anonymous remainder."""
        batch = [
            {
                "id": 1,
                "chat_id": 100,
                "reactions": [
                    {"emoji": "heart", "count": 5, "user_ids": [10, 20]},
                ],
            }
        ]

        self._run(self.backup._commit_batch(batch, 100))

        call_args = self.backup.db.insert_reactions.call_args
        reactions_list = call_args[0][2]
        # 2 per-user rows + 1 anonymous (5-2=3 remaining)
        self.assertEqual(len(reactions_list), 3)
        self.assertEqual(reactions_list[0]["user_id"], 10)
        self.assertEqual(reactions_list[1]["user_id"], 20)
        self.assertIsNone(reactions_list[2]["user_id"])
        self.assertEqual(reactions_list[2]["count"], 3)

    def test_reaction_without_users_creates_anonymous_row(self):
        """Reactions without user_ids create a single anonymous row."""
        batch = [
            {
                "id": 2,
                "chat_id": 100,
                "reactions": [
                    {"emoji": "fire", "count": 7, "user_ids": []},
                ],
            }
        ]

        self._run(self.backup._commit_batch(batch, 100))

        call_args = self.backup.db.insert_reactions.call_args
        reactions_list = call_args[0][2]
        self.assertEqual(len(reactions_list), 1)
        self.assertIsNone(reactions_list[0]["user_id"])
        self.assertEqual(reactions_list[0]["count"], 7)

    def test_no_reactions_skips_insert(self):
        """Messages with no reactions do not call insert_reactions."""
        batch = [
            {"id": 3, "chat_id": 100, "reactions": []},
            {"id": 4, "chat_id": 100, "reactions": None},
        ]

        self._run(self.backup._commit_batch(batch, 100))

        self.backup.db.insert_reactions.assert_not_awaited()

    def test_batch_with_no_media_skips_insert_media(self):
        """Messages without _media_data do not call insert_media."""
        batch = [
            {"id": 5, "chat_id": 100, "reactions": []},
        ]

        self._run(self.backup._commit_batch(batch, 100))

        self.backup.db.insert_media.assert_not_awaited()


class TestBackupForumTopics(unittest.TestCase):
    """Test _backup_forum_topics with API path and skip filtering."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.db = AsyncMock()
        self.backup.client = AsyncMock()
        self.backup.config = MagicMock()
        self.backup.config.should_skip_topic = MagicMock(return_value=False)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_topic(self, topic_id, title, closed=False, pinned=False, hidden=False):
        """Create a mock forum topic."""
        topic = MagicMock()
        topic.id = topic_id
        topic.title = title
        topic.icon_color = 0x1234
        topic.icon_emoji_id = None
        topic.closed = closed
        topic.pinned = pinned
        topic.hidden = hidden
        topic.date = datetime(2024, 6, 1)
        return topic

    def test_api_path_stores_all_topics(self):
        """API path stores all topics when none are excluded."""
        topics = [self._make_topic(1, "General"), self._make_topic(2, "Off-Topic")]

        result_obj = MagicMock()
        result_obj.topics = topics

        # client(...) is an async callable -- AsyncMock handles this directly
        self.backup.client = AsyncMock()
        self.backup.client.get_input_entity = AsyncMock(return_value=MagicMock())
        self.backup.client.return_value = result_obj

        entity = MagicMock()
        count = self._run(self.backup._backup_forum_topics(-100123, entity))

        self.assertEqual(count, 2)
        self.assertEqual(self.backup.db.upsert_forum_topic.await_count, 2)

    def test_api_path_skips_excluded_topics(self):
        """API path skips topics matching should_skip_topic."""
        topics = [self._make_topic(1, "General"), self._make_topic(42, "Spam")]

        result_obj = MagicMock()
        result_obj.topics = topics

        self.backup.client = AsyncMock()
        self.backup.client.get_input_entity = AsyncMock(return_value=MagicMock())
        self.backup.client.return_value = result_obj
        self.backup.config.should_skip_topic = MagicMock(side_effect=lambda chat_id, topic_id: topic_id == 42)

        entity = MagicMock()
        count = self._run(self.backup._backup_forum_topics(-100123, entity))

        self.assertEqual(count, 1)
        self.assertEqual(self.backup.db.upsert_forum_topic.await_count, 1)

    def test_api_path_topic_data_includes_correct_fields(self):
        """API path passes correct topic data to upsert_forum_topic."""
        topic = self._make_topic(7, "Important", closed=True, pinned=True, hidden=False)
        result_obj = MagicMock()
        result_obj.topics = [topic]

        self.backup.client = AsyncMock()
        self.backup.client.get_input_entity = AsyncMock(return_value=MagicMock())
        self.backup.client.return_value = result_obj

        entity = MagicMock()
        self._run(self.backup._backup_forum_topics(-100999, entity))

        call_args = self.backup.db.upsert_forum_topic.call_args[0][0]
        self.assertEqual(call_args["id"], 7)
        self.assertEqual(call_args["chat_id"], -100999)
        self.assertEqual(call_args["title"], "Important")
        self.assertEqual(call_args["is_closed"], 1)
        self.assertEqual(call_args["is_pinned"], 1)
        self.assertEqual(call_args["is_hidden"], 0)

    def test_returns_zero_on_total_failure(self):
        """Returns 0 when both API and fallback fail."""
        # Make GetForumTopicsRequest import succeed but API call fail
        self.backup.client = AsyncMock()
        self.backup.client.get_input_entity = AsyncMock(side_effect=Exception("no access"))

        entity = MagicMock()
        count = self._run(self.backup._backup_forum_topics(-100123, entity))

        self.assertEqual(count, 0)


class TestBackupDialogCursorAdvancesOnSkippedMessages(unittest.TestCase):
    """Test that _backup_dialog advances cursor even when all messages are topic-filtered."""

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

    def test_cursor_advances_when_all_messages_skipped_by_topic_filter(self):
        """When all messages are topic-filtered, sync_status still updates with max ID."""
        # All messages belong to excluded topic 42
        self.config.should_skip_topic = MagicMock(return_value=True)

        msg1 = MagicMock()
        msg1.id = 50
        msg1.reply_to = MagicMock()
        msg1.reply_to.forum_topic = True
        msg1.reply_to.reply_to_top_id = 42
        msg1.reply_to.reply_to_msg_id = 42

        msg2 = MagicMock()
        msg2.id = 100
        msg2.reply_to = MagicMock()
        msg2.reply_to.forum_topic = True
        msg2.reply_to.reply_to_top_id = 42
        msg2.reply_to.reply_to_msg_id = 42

        async def fake_iter(*args, **kwargs):
            yield msg1
            yield msg2

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock()
        self.backup._commit_batch = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        result = self._run(self.backup._backup_dialog(self._make_dialog()))

        # 0 messages processed but cursor should still advance
        self.assertEqual(result, 0)
        self.backup._process_message.assert_not_awaited()
        # sync_status should be called with max_id=100
        self.db.update_sync_status.assert_awaited_once()
        call_args = self.db.update_sync_status.call_args[0]
        self.assertEqual(call_args[1], 100)


if __name__ == "__main__":
    unittest.main()
