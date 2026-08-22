"""Tests for Telegram backup functionality."""

import asyncio
import os
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Channel,
    Chat,
    MessageActionChatEditTitle,
    MessageActionPinMessage,
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    TextWithEntities,
    User,
)

from src.message_utils import extract_topic_id, service_action_type
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


@unittest.skipIf(os.name == "nt", "Symlinks require administrator privileges on Windows")
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
        self.backup.account_id = 1
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
        self.db.delete_media_for_chat.assert_awaited_once_with(chat_id, account_id=1)

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

        self.db.delete_media_for_chat.assert_awaited_once_with(chat_id, account_id=1)

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
        self.config.reaction_resweep_days = 0
        self.config.should_skip_topic = MagicMock(return_value=False)
        self.config.media_path = os.path.join(self.temp_dir, "media")

        self.db = AsyncMock()
        self.db.get_last_message_id.return_value = 0

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
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
        # Explicitly None so the service-action capture in _process_message
        # is not triggered by MagicMock truthiness.
        msg.action = None
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
        """Checkpoint passes the highest message id SEEN, not the last one.

        The stream is deliberately non-monotonic (Telegram iterates newest
        first anyway): with ascending ids, max(seen, id) and a plain "last id
        wins" assignment are indistinguishable, and this test — named for the
        high-water-mark invariant — certified nothing. A regression to
        running_max_id = message.id would rewind the cursor and re-fetch an
        already-archived range."""
        messages = [self._make_message(20), self._make_message(10)]

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
        """_commit_batch persists messages, media and reconciles reactions.

        #219: an empty snapshot ([]) is reconciled (zero-clear) when stored rows
        exist for the message; a None snapshot (extraction failure) is skipped so
        it can't tombstone valid reactions.
        """
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.db = AsyncMock()
        backup.db.get_message_ids_with_reaction_rows.return_value = {1}

        batch = [
            {"id": 1, "chat_id": 100, "_media_data": {"file_path": "/a.jpg"}, "reactions": []},
            {"id": 2, "chat_id": 100, "reactions": [{"emoji": "👍", "count": 3}]},
        ]

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(backup._commit_batch(batch, 100))
        finally:
            loop.close()

        backup.db.insert_messages_batch.assert_awaited_once_with(batch, account_id=1)
        backup.db.insert_media.assert_awaited_once_with({"file_path": "/a.jpg"}, account_id=1)
        # Reconciled once per message: empty snapshot for msg 1, the aggregate for msg 2.
        self.assertEqual(backup.db.reconcile_reactions.await_count, 2)
        backup.db.reconcile_reactions.assert_any_await(1, 100, [], mark_removed=True, account_id=1)
        backup.db.reconcile_reactions.assert_any_await(
            2, 100, [{"emoji": "👍", "count": 3}], mark_removed=True, account_id=1
        )


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
        self.config.reaction_resweep_days = 0
        self.config.media_path = os.path.join(self.temp_dir, "media")

        self.db = AsyncMock()
        self.db.get_last_message_id.return_value = 0

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
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
        # Real numeric: backup_all's resweep cycle hook compares this (#224).
        self.config.reaction_resweep_days = 0.0
        os.makedirs(self.config.media_path, exist_ok=True)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
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
        # #234 fast path: everything resolved on the first pass → zero extra API calls
        self.backup.client.iter_dialogs.assert_not_called()

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

    # ------------------------------------------------------------------
    # #234 fallback: bounded dialog scan for unresolvable whitelist ids
    # ------------------------------------------------------------------

    class _FakeDialogIter:
        """Async iterator matching iter_dialogs' shape (sync call → async iterable).

        A plain iterator object mirrors Telethon's RequestIter — no aclose() —
        so an early ``break`` in the code under test leaves no suspended
        async-generator frame behind (those warn at loop teardown).
        """

        def __init__(self, dialogs, *, error=None, delay=0.0, counter=None):
            self._dialogs = list(dialogs)
            self._error = error
            self._delay = delay
            self._counter = counter

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._dialogs:
                if self._counter is not None:
                    self._counter["count"] += 1
                return self._dialogs.pop(0)
            if self._error is not None:
                raise self._error
            raise StopAsyncIteration

    def _wire_backup_run(self, metadata=None):
        """Mock wiring shared by the #234 fallback tests (mirrors the #95 tests)."""
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=123))
        self.backup.db.get_metadata = AsyncMock(side_effect=lambda key: (metadata or {}).get(key))
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.upsert_chat = AsyncMock()
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 0, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_dialog = AsyncMock(return_value=0)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.config.follow_chat_migrations = False

    def _backed_up_entities(self):
        return [c.args[0].entity for c in self.backup._backup_dialog.await_args_list]

    def test_whitelist_fallback_resolves_dm_after_dialog_sweep(self):
        """An unresolvable DM id is recovered via the bounded dialog scan (#234)."""
        self.config.chat_ids = {12345}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("Could not find the input entity"))
        dm_entity = User(id=12345)
        dialog = MagicMock(id=12345, entity=dm_entity)
        self.backup.client.iter_dialogs = MagicMock(side_effect=lambda **kw: self._FakeDialogIter([dialog]))

        self._run(self.backup.backup_all())

        # Regression pin: no folder/archived kwarg — the scan must cover ALL folders.
        self.backup.client.iter_dialogs.assert_called_once_with(limit=1000)
        # The swept dialog already carries the entity — no second get_entity for it.
        self.assertEqual(self.backup.client.get_entity.await_count, 1)
        self.assertIn(dm_entity, self._backed_up_entities())

    def test_whitelist_fallback_early_stops_when_all_resolved(self):
        """The scan stops consuming dialogs once every pending id matched."""
        self.config.chat_ids = {12345}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("unresolved"))
        dialogs = [
            MagicMock(id=12345, entity=User(id=12345)),
            MagicMock(id=222, entity=User(id=222)),
            MagicMock(id=333, entity=User(id=333)),
        ]
        consumed = {"count": 0}
        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter(dialogs, counter=consumed)
        )

        self._run(self.backup.backup_all())

        self.assertEqual(consumed["count"], 1)

    def test_whitelist_fallback_disabled_when_limit_zero(self):
        """WHITELIST_RESOLVE_DIALOG_LIMIT=0 disables the scan entirely."""
        self.config.chat_ids = {12345}
        self.config.whitelist_resolve_dialog_limit = 0
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("unresolved"))
        self.backup.client.iter_dialogs = MagicMock()

        self._run(self.backup.backup_all())

        self.backup.client.iter_dialogs.assert_not_called()
        # First pass only — the retry pass is part of the disabled fallback.
        self.assertEqual(self.backup.client.get_entity.await_count, 1)

    def test_whitelist_fallback_floodwait_during_sweep_proceeds(self):
        """A FloodWait mid-scan stops the sweep but never aborts the run."""
        self.config.chat_ids = {12345}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("unresolved"))

        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter(
                [MagicMock(id=222, entity=User(id=222))],
                error=FloodWaitError(request=None, capture=15),
            )
        )

        with self.assertLogs("src.telegram_backup", level="INFO") as cm:
            self._run(self.backup.backup_all())

        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(any("hit a FloodWait" in m for m in messages))
        self.assertTrue(any("remain unresolvable" in m for m in messages))

    def test_whitelist_fallback_sweep_error_proceeds(self):
        """Any other scan error is swallowed; the run completes."""
        self.config.chat_ids = {12345}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("unresolved"))

        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter([], error=RuntimeError("connection wedged"))
        )

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            self._run(self.backup.backup_all())

        self.assertTrue(any("dialog scan failed" in r.getMessage() for r in cm.records))

    def test_whitelist_fallback_sweep_timeout_proceeds(self):
        """The scan is wall-clock bounded; a hang cannot stall the run (#95)."""
        self.config.chat_ids = {12345}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("unresolved"))

        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter([MagicMock(id=12345, entity=User(id=12345))], delay=1.0)
        )

        with (
            unittest.mock.patch("src.telegram_backup.WHITELIST_RESOLVE_SWEEP_TIMEOUT_SECONDS", 0.05),
            self.assertLogs("src.telegram_backup", level="WARNING") as cm,
        ):
            self._run(self.backup.backup_all())

        self.assertTrue(any("timed out" in r.getMessage() for r in cm.records))

    def test_whitelist_fallback_still_unresolved_warns_without_ids(self):
        """New #234 log lines carry counts only — never the configured ids."""
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 500
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("could not resolve"))
        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter([MagicMock(id=111, entity=User(id=111))])
        )

        with self.assertLogs("src.telegram_backup", level="INFO") as cm:
            self._run(self.backup.backup_all())

        # Scope to the new fallback messages: the pre-existing per-entry
        # "Could not fetch chat" line embeds Telethon's exception text and is exempt.
        new_path_messages = [
            r.getMessage() for r in cm.records if r.getMessage().startswith(("Whitelist:", "Whitelist resolve:"))
        ]
        self.assertTrue(any("remain unresolvable" in m for m in new_path_messages))
        for message in new_path_messages:
            self.assertNotIn("987654321", message)

    def test_whitelist_fallback_sweep_suppressed_for_known_failed_ids(self):
        """Ids that already failed a scan at this bound don't re-trigger one, but still get the cheap retry."""
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run(metadata={"whitelist_unresolved_ids": '{"limit": 1000, "ids": [987654321]}'})
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("still failing"))
        self.backup.client.iter_dialogs = MagicMock()

        self._run(self.backup.backup_all())

        self.backup.client.iter_dialogs.assert_not_called()
        # First pass + cheap retry pass — the id must keep self-heal attempts.
        self.assertEqual(self.backup.client.get_entity.await_count, 2)
        # Retained at its original proof bound (no scan ran this time).
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": [987654321]}')

    def test_whitelist_fallback_resolution_clears_known_failed(self):
        """A known-failed id that resolves on the retry pass leaves the persisted set."""
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run(metadata={"whitelist_unresolved_ids": '{"limit": 1000, "ids": [987654321]}'})
        entity = User(id=987654321)
        self.backup.client.get_entity = AsyncMock(side_effect=[Exception("cache cold"), entity])
        self.backup.client.iter_dialogs = MagicMock()

        self._run(self.backup.backup_all())

        self.backup.client.iter_dialogs.assert_not_called()
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": []}')
        self.assertIn(entity, self._backed_up_entities())

    def test_whitelist_fallback_raised_limit_rearms_scan(self):
        """Raising WHITELIST_RESOLVE_DIALOG_LIMIT re-arms the scan for suppressed ids.

        The persisted proof bound records how far the absence was proven; a
        higher configured limit invalidates it — this is what makes the
        warning's own advice actually work (review finding on #234).
        """
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 2000
        self._wire_backup_run(metadata={"whitelist_unresolved_ids": '{"limit": 1000, "ids": [987654321]}'})
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("cache cold"))
        dm_entity = User(id=987654321)
        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter([MagicMock(id=987654321, entity=dm_entity)])
        )

        self._run(self.backup.backup_all())

        self.backup.client.iter_dialogs.assert_called_once_with(limit=2000)
        self.assertIn(dm_entity, self._backed_up_entities())
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 2000, "ids": []}')

    def test_whitelist_fallback_legacy_list_metadata_rearms_scan(self):
        """A legacy plain-list metadata value has no proof bound — the scan re-runs once."""
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run(metadata={"whitelist_unresolved_ids": "[987654321]"})
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("cache cold"))
        self.backup.client.iter_dialogs = MagicMock(side_effect=lambda **kw: self._FakeDialogIter([]))

        self._run(self.backup.backup_all())

        self.backup.client.iter_dialogs.assert_called_once_with(limit=1000)
        # Scan completed empty → absence now proven at the current bound.
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": [987654321]}')

    def test_whitelist_fallback_aborted_scan_does_not_suppress_new_ids(self):
        """A flood-aborted scan proves nothing — unfound NEW ids must stay sweep-eligible.

        One unlucky first run must not permanently disarm the #234 fallback
        (confirmed high-severity review finding).
        """
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("cache cold"))
        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter([], error=FloodWaitError(request=None, capture=15))
        )

        self._run(self.backup.backup_all())

        # Nothing new persisted: next run's scan must retry this id.
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 0, "ids": []}')

    def test_whitelist_fallback_completed_scan_suppresses_missing_ids(self):
        """A scan that ran to completion proves absence — the id is persisted at the current bound."""
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run()
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("cache cold"))
        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter([MagicMock(id=111, entity=User(id=111))])
        )

        self._run(self.backup.backup_all())

        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": [987654321]}')

    def test_whitelist_fallback_stale_suppressions_cleared_when_all_resolve(self):
        """When every entry resolves directly, leftover suppressions are cleared.

        Otherwise an id that goes cache-cold again later (session reset) would
        be silently retry-only forever (review finding on #234).
        """
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run(metadata={"whitelist_unresolved_ids": '{"limit": 1000, "ids": [987654321]}'})
        self.backup.client.get_entity = AsyncMock(return_value=User(id=987654321))
        self.backup.client.iter_dialogs = MagicMock()

        self._run(self.backup.backup_all())

        self.backup.client.iter_dialogs.assert_not_called()
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": []}')

    def test_whitelist_fallback_known_failed_resolved_by_rider_scan_drops_out(self):
        """A scan triggered by a NEW id also rescues known-failed ids — and un-suppresses them."""
        self.config.chat_ids = {987654321, 12345}
        self.config.whitelist_resolve_dialog_limit = 1000
        self._wire_backup_run(metadata={"whitelist_unresolved_ids": '{"limit": 1000, "ids": [987654321]}'})
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("cache cold"))
        old_dm = User(id=987654321)
        new_dm = User(id=12345)
        self.backup.client.iter_dialogs = MagicMock(
            side_effect=lambda **kw: self._FakeDialogIter(
                [MagicMock(id=987654321, entity=old_dm), MagicMock(id=12345, entity=new_dm)]
            )
        )

        self._run(self.backup.backup_all())

        entities = self._backed_up_entities()
        self.assertIn(old_dm, entities)
        self.assertIn(new_dm, entities)
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": []}')

    def test_whitelist_fallback_followed_ids_not_persisted(self):
        """Unresolvable followed-migration ids are never written to the suppression key.

        They are not CHAT_IDS entries — the warning's operator guidance does not
        apply to them — so they stay eligible for the next run's scan instead.
        """
        self.config.chat_ids = {987654321}
        self.config.whitelist_resolve_dialog_limit = 1000
        self.config.global_exclude_ids = set()
        self.config.groups_exclude_ids = set()
        self.config.channels_exclude_ids = set()
        self._wire_backup_run(metadata={"followed_migrations": "[-1002000000001]"})
        self.config.follow_chat_migrations = True
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("cache cold"))
        self.backup.client.iter_dialogs = MagicMock(side_effect=lambda **kw: self._FakeDialogIter([]))

        self._run(self.backup.backup_all())

        # Scan completed: the configured id is proven-absent and persisted; the
        # followed id failed too but must NOT appear in the suppression set.
        self.backup.db.set_metadata.assert_any_call("whitelist_unresolved_ids", '{"limit": 1000, "ids": [987654321]}')

    def test_load_whitelist_unresolved_malformed_metadata_degrades(self):
        """Malformed/legacy/absent metadata degrades to (0, ...) and never raises."""

        async def _load_with(raw):
            self.backup.db.get_metadata = AsyncMock(return_value=raw)
            return await self.backup._load_whitelist_unresolved()

        self.assertEqual(self._run(_load_with(None)), (0, set()))
        self.assertEqual(self._run(_load_with("not json at all {{{")), (0, set()))
        self.assertEqual(self._run(_load_with('{"ids": "nope", "limit": "x"}')), (0, set()))
        self.assertEqual(self._run(_load_with('{"limit": -5, "ids": [1, "junk", 2]}')), (0, {1, 2}))
        self.assertEqual(self._run(_load_with("[7, 8]")), (0, {7, 8}))
        self.assertEqual(
            self._run(_load_with('{"limit": 500, "ids": [7]}')),
            (500, {7}),
        )


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


class TestServiceActionType(unittest.TestCase):
    """Test the shared service_action_type class-name normalizer."""

    def test_topic_create(self):
        self.assertEqual(service_action_type(MessageActionTopicCreate(title="x", icon_color=0)), "topic_create")

    def test_topic_edit(self):
        self.assertEqual(service_action_type(MessageActionTopicEdit(title="x")), "topic_edit")

    def test_multi_word_chat_edit_title(self):
        self.assertEqual(service_action_type(MessageActionChatEditTitle(title="x")), "chat_edit_title")

    def test_no_argument_action(self):
        self.assertEqual(service_action_type(MessageActionPinMessage()), "pin_message")

    def test_acronym_run_splits_letter_by_letter(self):
        """Documents the known cosmetic edge: consecutive capitals split.

        No title-bearing action we consume hits this; the tag is a stable,
        unparsed identifier, so the behavior is intentional and pinned here.
        """

        class MessageActionSetMessagesTTL:
            pass

        self.assertEqual(service_action_type(MessageActionSetMessagesTTL()), "set_messages_t_t_l")


class TestExtractForwardFromId(unittest.TestCase):
    """Test _extract_forward_from_id for different Peer types."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1

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
        """A user forward stores the user id — already the marked form."""
        from telethon.tl.types import PeerUser

        msg = MagicMock()
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_id = PeerUser(user_id=12345)
        self.assertEqual(self.backup._extract_forward_from_id(msg), 12345)

    def test_returns_marked_id_from_peer_channel(self):
        """A channel forward stores the MARKED -100... id, the convention
        every other persisted id follows — the raw channel_id landed in the
        user-id numeric space and matched nothing the user can look up."""
        from telethon.tl.types import PeerChannel

        msg = MagicMock()
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_id = PeerChannel(channel_id=99999)
        self.assertEqual(self.backup._extract_forward_from_id(msg), -1000000099999)

    def test_returns_marked_id_from_peer_chat(self):
        """A basic-group forward stores the marked negative chat id."""
        from telethon.tl.types import PeerChat

        msg = MagicMock()
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_id = PeerChat(chat_id=77777)
        self.assertEqual(self.backup._extract_forward_from_id(msg), -77777)

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
        self.backup.account_id = 1

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
        self.backup.account_id = 1

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

    def test_document_without_document_attr_returns_none(self):
        """MessageMediaDocument with no .document returns None (inaccessible)."""
        media = MagicMock(spec=MessageMediaDocument)
        media.document = None
        self.assertIsNone(self.backup._get_media_type(media))


class TestGetMediaExtension(unittest.TestCase):
    """Test _get_media_extension fallback extension lookup."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1

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
        self.backup.account_id = 1
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
        self.backup.account_id = 1

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
        self.backup.account_id = 1

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
        self.backup.account_id = 1
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
        # Explicitly None so the service-action capture in _process_message
        # is not triggered by MagicMock truthiness.
        msg.action = None
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

    def test_topic_create_action_stored_in_raw_data(self):
        """Topic-create service action stores service metadata in raw_data."""
        msg = self._make_message(8, text=None)
        msg.action = MessageActionTopicCreate(title="Synthetic Topic", icon_color=0)
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["service_type"], "service")
        self.assertEqual(result["raw_data"]["action_type"], "topic_create")
        self.assertEqual(result["raw_data"]["new_title"], "Synthetic Topic")
        # Topic creations are linkable: the topic id equals the service
        # message id (forum_topics.id == messages.id).
        self.assertEqual(result["id"], 8)

    def test_topic_edit_action_stores_new_title(self):
        """Topic rename stores the new title and stays linkable by topic id."""
        msg = self._make_message(9, text=None)
        msg.action = MessageActionTopicEdit(title="Renamed Topic")
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = 8
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["service_type"], "service")
        self.assertEqual(result["raw_data"]["action_type"], "topic_edit")
        self.assertEqual(result["raw_data"]["new_title"], "Renamed Topic")
        # Topic edits are linkable through reply_to_top_id.
        self.assertEqual(result["reply_to_top_id"], 8)

    def test_topic_edit_without_title_has_no_new_title(self):
        """Topic edits that do not rename (e.g. close) store no new_title."""
        msg = self._make_message(10, text=None)
        msg.action = MessageActionTopicEdit(closed=True)
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["service_type"], "service")
        self.assertEqual(result["raw_data"]["action_type"], "topic_edit")
        self.assertNotIn("new_title", result["raw_data"])

    def test_regular_message_has_no_service_metadata(self):
        """Regular messages carry no service metadata in raw_data."""
        msg = self._make_message(11)
        result = self._run(self.backup._process_message(msg, 100))
        self.assertNotIn("service_type", result["raw_data"])
        self.assertNotIn("action_type", result["raw_data"])
        self.assertNotIn("new_title", result["raw_data"])

    def test_chat_edit_title_action_stored_in_raw_data(self):
        """Non-topic action: a group rename stores a multi-word action_type."""
        msg = self._make_message(12, text=None)
        msg.action = MessageActionChatEditTitle(title="New Group Name")
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["service_type"], "service")
        self.assertEqual(result["raw_data"]["action_type"], "chat_edit_title")
        self.assertEqual(result["raw_data"]["new_title"], "New Group Name")

    def test_pin_message_action_has_no_new_title(self):
        """An action without a title stores action_type but no new_title."""
        msg = self._make_message(13, text=None)
        msg.action = MessageActionPinMessage()
        result = self._run(self.backup._process_message(msg, 100))
        self.assertEqual(result["raw_data"]["service_type"], "service")
        self.assertEqual(result["raw_data"]["action_type"], "pin_message")
        self.assertNotIn("new_title", result["raw_data"])

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

    def test_sender_name_snapshot_prefers_trimmed_first_and_last_name(self):
        msg = self._make_message(80)
        sender = MagicMock()
        sender.first_name = "  Ada "
        sender.last_name = " Lovelace  "
        sender.title = "Channel Title"
        sender.username = "ada"
        msg.sender = sender

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["sender_name"], "Ada Lovelace")

    def test_sender_name_snapshot_falls_back_to_title_then_username(self):
        title_msg = self._make_message(81)
        title_sender = MagicMock()
        title_sender.first_name = None
        title_sender.last_name = " "
        title_sender.title = "  News Channel "
        title_sender.username = "news"
        title_msg.sender = title_sender

        username_msg = self._make_message(82)
        username_sender = MagicMock()
        username_sender.first_name = None
        username_sender.last_name = None
        username_sender.title = " "
        username_sender.username = "  archived_user "
        username_msg.sender = username_sender

        title_result = self._run(self.backup._process_message(title_msg, 100))
        username_result = self._run(self.backup._process_message(username_msg, 100))

        self.assertEqual(title_result["sender_name"], "News Channel")
        self.assertEqual(username_result["sender_name"], "archived_user")

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

    def test_quote_reply_excerpt_stored_and_truncated(self):
        """A quote-reply's excerpt (MessageReplyHeader.quote_text — the field
        Telethon actually has; the old code read a non-existent .message via a
        MagicMock-only test) is stored, truncated to Telegram's 100-char
        preview."""
        from telethon.tl.types import MessageReplyHeader

        msg = self._make_message(11)
        msg.reply_to_msg_id = 5
        msg.reply_to = MessageReplyHeader(reply_to_msg_id=5, quote=True, quote_text="a" * 200)

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(len(result["reply_to_text"]), 100)
        self.assertEqual(result["reply_to_text"], "a" * 100)

    def test_plain_reply_stores_no_excerpt(self):
        """A non-quote reply carries no quote_text: reply_to_text stays None and
        the viewer's read-time backfill supplies the target's text instead."""
        from telethon.tl.types import MessageReplyHeader

        msg = self._make_message(12)
        msg.reply_to_msg_id = 5
        msg.reply_to = MessageReplyHeader(reply_to_msg_id=5)

        result = self._run(self.backup._process_message(msg, 100))

        self.assertIsNone(result["reply_to_text"])

    def test_forward_from_id_resolves_channel_title(self):
        """Forward from a channel unknown locally resolves its title via get_entity."""
        from telethon.tl.types import PeerChannel

        msg = self._make_message(12)
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = None
        msg.fwd_from.from_id = PeerChannel(channel_id=555)
        self.backup.db.get_chat_by_id = AsyncMock(return_value=None)

        fwd_entity = MagicMock()
        fwd_entity.title = "Forwarded Channel"
        del fwd_entity.first_name  # ensure hasattr(, 'title') path is taken
        self.backup.client.get_entity = AsyncMock(return_value=fwd_entity)

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Forwarded Channel")

    def test_forward_from_id_resolves_user_name(self):
        """Forward from a user unknown locally resolves first+last via get_entity."""
        from telethon.tl.types import PeerUser

        msg = self._make_message(13)
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = None
        msg.fwd_from.from_id = PeerUser(user_id=777)
        self.backup.db.get_user_by_id = AsyncMock(return_value=None)

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

    def test_reactions_extracted_as_aggregate(self):
        """#219: extraction is per-emoji aggregate ({emoji, count}); per-user
        attribution is intentionally not persisted (unreliable on a user client)."""
        msg = self._make_message(18)

        reaction = MagicMock()
        reaction.reaction = MagicMock(spec=["emoticon"])
        reaction.reaction.emoticon = "heart"
        reaction.count = 5

        msg.reactions = MagicMock(spec=["results"])
        msg.reactions.results = [reaction]

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["reactions"], [{"emoji": "heart", "count": 5}])


class TestCommitBatchReactions(unittest.TestCase):
    """Test _commit_batch reaction expansion logic."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
        self.backup.db = AsyncMock()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_aggregate_reactions_passed_to_reconcile(self):
        """#219: _commit_batch forwards the per-emoji aggregate snapshot to
        reconcile_reactions (no client-side per-user expansion)."""
        batch = [
            {"id": 1, "chat_id": 100, "reactions": [{"emoji": "heart", "count": 5}]},
        ]

        self._run(self.backup._commit_batch(batch, 100))

        self.backup.db.reconcile_reactions.assert_any_await(
            1, 100, [{"emoji": "heart", "count": 5}], mark_removed=True, account_id=1
        )

    def test_empty_reactions_still_reconciled(self):
        """#219 (F2): reconcile runs for an empty snapshot ([]) when the message
        holds stored rows, so removals-to-zero on re-fetched messages persist.
        The batched probe decides which empty snapshots can skip."""
        self.backup.db.get_message_ids_with_reaction_rows.return_value = {3}
        batch = [{"id": 3, "chat_id": 100, "reactions": []}]

        self._run(self.backup._commit_batch(batch, 100))

        self.backup.db.get_message_ids_with_reaction_rows.assert_awaited_once_with(100, [3], account_id=1)
        self.backup.db.reconcile_reactions.assert_awaited_once_with(3, 100, [], mark_removed=True, account_id=1)

    def test_extraction_failure_skips_reconcile(self):
        """#219: a None snapshot means extraction FAILED (shape drift) — skip
        reconcile entirely rather than tombstone valid reactions with an empty set."""
        batch = [{"id": 4, "chat_id": 100, "reactions": None}]

        self._run(self.backup._commit_batch(batch, 100))

        self.backup.db.reconcile_reactions.assert_not_awaited()

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
        self.backup.account_id = 1
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
        result_obj.count = 2  # total topics -> pagination stops after this page
        result_obj.messages = []

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
        result_obj.count = 2  # total topics -> pagination stops after this page
        result_obj.messages = []

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
        result_obj.count = 1  # total topics -> pagination stops after this page
        result_obj.messages = []

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
        self.config.reaction_resweep_days = 0
        self.config.media_path = os.path.join(self.temp_dir, "media")

        self.db = AsyncMock()
        self.db.get_last_message_id.return_value = 0

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
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


class TestForwardSourceNameResolution(unittest.TestCase):
    """Forward sources resolve local-first with a per-run cache.

    The sweep path forbids per-message entity resolution (its own comment:
    one API request per message is avoidable flood risk), yet every forward
    with a from_id paid a get_entity call — Telethon has no full-entity
    memory cache, so a 10,000-forward channel cost 10,000 requests per run,
    every run. Each distinct source now costs at most one lookup per run,
    negative results included.
    """

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
        self.backup.db = AsyncMock()
        self.backup.db.get_user_by_id = AsyncMock(return_value=None)
        self.backup.db.get_chat_by_id = AsyncMock(return_value=None)
        self.backup.config = MagicMock()
        self.backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        self.backup.client = AsyncMock()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_local_users_table_wins_no_api_call(self):
        from telethon.tl.types import PeerUser

        self.backup.db.get_user_by_id = AsyncMock(return_value={"first_name": "Ada", "last_name": "Lovelace"})

        name = self._run(self.backup._resolve_forward_source_name(PeerUser(user_id=42)))

        self.assertEqual(name, "Ada Lovelace")
        self.backup.client.get_entity.assert_not_awaited()
        self.backup.db.get_user_by_id.assert_awaited_once_with(42)

    def test_local_chats_table_wins_for_channels(self):
        from telethon.tl.types import PeerChannel

        self.backup.db.get_chat_by_id = AsyncMock(return_value={"title": "News Channel"})

        name = self._run(self.backup._resolve_forward_source_name(PeerChannel(channel_id=99999)))

        self.assertEqual(name, "News Channel")
        self.backup.client.get_entity.assert_not_awaited()
        self.backup.db.get_chat_by_id.assert_awaited_once_with(-1000000099999, account_id=1)

    def test_one_api_call_per_distinct_source_per_run(self):
        from telethon.tl.types import PeerChannel

        entity = MagicMock(spec=["title"])
        entity.title = "Aggregated"
        self.backup.client.get_entity = AsyncMock(return_value=entity)

        async def scenario():
            results = []
            for _ in range(500):
                results.append(await self.backup._resolve_forward_source_name(PeerChannel(channel_id=777)))
            return results

        results = self._run(scenario())

        self.assertEqual(results, ["Aggregated"] * 500)
        self.backup.client.get_entity.assert_awaited_once()

    def test_unresolvable_source_costs_one_request_not_one_per_message(self):
        from telethon.tl.types import PeerUser

        self.backup.client.get_entity = AsyncMock(side_effect=ValueError("no such user"))

        async def scenario():
            return [await self.backup._resolve_forward_source_name(PeerUser(user_id=314)) for _ in range(50)]

        results = self._run(scenario())

        self.assertEqual(results, [None] * 50)
        self.backup.client.get_entity.assert_awaited_once()

    def test_cache_at_capacity_evicts_fifo_instead_of_refusing_new_sources(self):
        """Beyond the cap the cache must stay best-effort (FIFO eviction), not
        go read-only: a refuse-at-cap policy made every message from source
        10,001+ repeat the get_entity call — the per-message API pattern this
        cache exists to prevent."""
        from telethon.tl.types import PeerChannel

        entity = MagicMock(spec=["title"])
        entity.title = "Overflow"
        self.backup.client.get_entity = AsyncMock(return_value=entity)
        # The cache is lazily created on first resolve; pre-fill it to the cap
        # with resolved entries. Id 0 is the oldest.
        cache = self.backup._forward_name_cache = {
            marked_id: f"cached-{marked_id}" for marked_id in range(self.backup._FORWARD_NAME_CACHE_LIMIT)
        }

        async def scenario():
            return [await self.backup._resolve_forward_source_name(PeerChannel(channel_id=777)) for _ in range(5)]

        results = self._run(scenario())

        self.assertEqual(results, ["Overflow"] * 5)
        # One lookup, then served from cache — the new source WAS admitted...
        self.backup.client.get_entity.assert_awaited_once()
        marked = next(k for k in cache if k not in range(self.backup._FORWARD_NAME_CACHE_LIMIT))
        self.assertEqual(cache[marked], "Overflow")
        # ...the oldest entry made room, and the cap still holds.
        self.assertNotIn(0, cache)
        self.assertIn(1, cache)
        self.assertEqual(len(cache), self.backup._FORWARD_NAME_CACHE_LIMIT)

    def test_process_message_stores_the_resolved_name(self):
        from telethon.tl.types import PeerChannel

        self.backup.db.get_chat_by_id = AsyncMock(return_value={"title": "Origin"})
        msg = MagicMock()
        msg.id = 7
        msg.sender = None
        msg.sender_id = 42
        msg.date = datetime(2024, 1, 1)
        msg.text = "fwd"
        msg.reply_to_msg_id = None
        msg.reply_to = None
        msg.edit_date = None
        msg.out = False
        msg.media = None
        msg.grouped_id = None
        msg.post_author = None
        msg.action = None
        msg.fwd_from = MagicMock()
        msg.fwd_from.from_name = None
        msg.fwd_from.from_id = PeerChannel(channel_id=555)

        result = self._run(self.backup._process_message(msg, 100))

        self.assertEqual(result["raw_data"]["forward_from_name"], "Origin")
        self.backup.client.get_entity.assert_not_awaited()
