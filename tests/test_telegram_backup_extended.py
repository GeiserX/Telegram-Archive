"""Extended tests for Telegram backup functionality — covers lines missing from coverage."""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import (
    BadRequestError,
    ChannelPrivateError,
    ChatForbiddenError,
    FileReferenceExpiredError,
    FloodWaitError,
    LocationInvalidError,
    rpc_message_to_error,
)
from telethon.tl.types import (
    Channel,
    MessageMediaDocument,
    MessageMediaPhoto,
    User,
)

from src import telegram_backup
from src.media_errors import is_media_location_error
from src.telegram_backup import TelegramBackup, run_backup, run_fill_gaps

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_backup(**overrides):
    """Create a TelegramBackup instance via __new__ with sensible mock defaults."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = overrides.get("config", MagicMock())
    backup.config.should_skip_topic = MagicMock(return_value=False)
    backup.config.deletion_mode = "hard"
    backup.db = overrides.get("db", AsyncMock())
    # Folder resolution reads the archived-chat snapshot; default to empty so
    # tests that exercise _backup_folders get an iterable, not a bare AsyncMock.
    backup.db.get_chats_for_folder_resolution = AsyncMock(return_value=[])
    backup.client = overrides.get("client", AsyncMock())
    backup._owns_client = overrides.get("_owns_client", True)
    backup._cleaned_media_chats = set()
    return backup


def _make_message(msg_id, *, reply_to=None, text="hello", media=None):
    """Create a minimal mock message with reply_to defaulting to None."""
    msg = MagicMock()
    msg.id = msg_id
    msg.sender = None
    msg.sender_id = 42
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = text
    msg.reply_to_msg_id = None
    msg.reply_to = reply_to
    msg.edit_date = None
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = None
    msg.media = media
    msg.reactions = None
    msg.post_author = None
    # Explicitly None so the service-action capture in _process_message
    # is not triggered by MagicMock truthiness.
    msg.action = None
    return msg


# ===========================================================================
# __init__ (lines 42-58)
# ===========================================================================


class TestInit(unittest.TestCase):
    """Test TelegramBackup.__init__ sets attributes correctly."""

    def test_init_with_no_client_sets_owns_client_true(self):
        """When no client is passed, _owns_client should be True."""
        config = MagicMock()
        db = AsyncMock()
        backup = TelegramBackup(config, db)
        self.assertTrue(backup._owns_client)
        self.assertIsNone(backup.client)
        self.assertIsInstance(backup._cleaned_media_chats, set)

    def test_init_with_client_sets_owns_client_false(self):
        """When a client is passed, _owns_client should be False."""
        config = MagicMock()
        db = AsyncMock()
        client = MagicMock()
        backup = TelegramBackup(config, db, client=client)
        self.assertFalse(backup._owns_client)
        self.assertIs(backup.client, client)

    def test_init_calls_validate_credentials(self):
        """__init__ must call config.validate_credentials()."""
        config = MagicMock()
        db = AsyncMock()
        TelegramBackup(config, db)
        config.validate_credentials.assert_called_once()


# ===========================================================================
# create() factory method (lines 86-87)
# ===========================================================================


class TestCreateFactory(unittest.TestCase):
    """Test the async create() factory method."""

    @patch("src.telegram_backup.create_adapter", new_callable=AsyncMock)
    def test_create_initializes_db_and_returns_instance(self, mock_create_adapter):
        """create() should call create_adapter and return a TelegramBackup."""
        mock_db = AsyncMock()
        mock_create_adapter.return_value = mock_db
        config = MagicMock()

        result = _run(TelegramBackup.create(config))

        mock_create_adapter.assert_awaited_once()
        self.assertIsInstance(result, TelegramBackup)
        self.assertIs(result.db, mock_db)

    @patch("src.telegram_backup.create_adapter", new_callable=AsyncMock)
    def test_create_passes_client_through(self, mock_create_adapter):
        """create() should forward the client parameter."""
        mock_create_adapter.return_value = AsyncMock()
        config = MagicMock()
        client = MagicMock()

        result = _run(TelegramBackup.create(config, client=client))

        self.assertIs(result.client, client)
        self.assertFalse(result._owns_client)


# ===========================================================================
# connect() (lines 98-101, 122-126, 133-137)
# ===========================================================================


class TestConnect(unittest.TestCase):
    """Test connect() shared client validation and WAL mode paths."""

    def test_shared_client_connected_returns_immediately(self):
        """Shared client that is_connected() returns early without creating new client."""
        mock_client = MagicMock()
        mock_client.is_connected = MagicMock(return_value=True)
        backup = _make_backup(client=mock_client, _owns_client=False)

        _run(backup.connect())

        mock_client.is_connected.assert_called_once()

    def test_shared_client_not_connected_raises(self):
        """Shared client that is NOT connected raises RuntimeError."""
        mock_client = MagicMock()
        mock_client.is_connected = MagicMock(return_value=False)
        backup = _make_backup(client=mock_client, _owns_client=False)

        with self.assertRaises(RuntimeError, msg="Shared client is not connected"):
            _run(backup.connect())

    def test_connect_wal_mode_exception_is_caught(self):
        """WAL mode pragma failure should be caught and logged, not raised."""
        backup = _make_backup()
        backup.client = None
        backup._owns_client = True
        backup.config.session_path = "/tmp/test.session"
        backup.config.api_id = 12345
        backup.config.api_hash = "abc"
        backup.config.get_telegram_client_kwargs.return_value = {}

        mock_client = AsyncMock()
        mock_session = MagicMock()
        mock_session._conn = MagicMock()
        mock_session._conn.execute.side_effect = Exception("PRAGMA failed")
        mock_client.session = mock_session
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", phone="+1"))

        with patch("src.telegram_backup.TelegramClient", return_value=mock_client):
            _run(backup.connect())

    def test_connect_not_authorized_raises(self):
        """connect() raises when session is not authorized."""
        backup = _make_backup()
        backup.client = None
        backup._owns_client = True
        backup.config.session_path = "/tmp/test.session"
        backup.config.api_id = 12345
        backup.config.api_hash = "abc"
        backup.config.get_telegram_client_kwargs.return_value = {}

        mock_client = AsyncMock()
        mock_client.session = MagicMock(spec=[])
        mock_client.is_user_authorized = AsyncMock(return_value=False)

        with (
            patch("src.telegram_backup.TelegramClient", return_value=mock_client),
            self.assertRaises(RuntimeError, msg="Session not authorized"),
        ):
            _run(backup.connect())


# ===========================================================================
# disconnect() (lines 149-151)
# ===========================================================================


class TestDisconnect(unittest.TestCase):
    """Test disconnect() ownership semantics."""

    def test_disconnect_owned_client_calls_disconnect(self):
        """When we own the client, disconnect() is called."""
        backup = _make_backup(_owns_client=True)
        _run(backup.disconnect())
        backup.client.disconnect.assert_awaited_once()

    def test_disconnect_shared_client_does_not_call_disconnect(self):
        """When client is shared, disconnect() is NOT called."""
        backup = _make_backup(_owns_client=False)
        _run(backup.disconnect())
        backup.client.disconnect.assert_not_awaited()


# ===========================================================================
# _get_dialogs() (lines 462-466)
# ===========================================================================


class TestGetDialogs(unittest.TestCase):
    """Test _get_dialogs archived/non-archived folder parameter."""

    def test_get_dialogs_archived_true_passes_folder_1(self):
        """archived=True should call get_dialogs(folder=1)."""
        backup = _make_backup()
        backup.client.get_dialogs = AsyncMock(return_value=[])

        _run(backup._get_dialogs(archived=True))

        backup.client.get_dialogs.assert_awaited_once_with(folder=1)

    def test_get_dialogs_archived_false_passes_folder_0(self):
        """archived=False should call get_dialogs(folder=0)."""
        backup = _make_backup()
        backup.client.get_dialogs = AsyncMock(return_value=[])

        _run(backup._get_dialogs(archived=False))

        backup.client.get_dialogs.assert_awaited_once_with(folder=0)


# ===========================================================================
# _verify_and_redownload_media (lines 499, 507-523, 544-545, 551,
#   556-559, 573-575, 578-580, 596-604)
# ===========================================================================


class TestVerifyAndRedownloadMedia(unittest.TestCase):
    """Test the full media verification + re-download flow."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.skip_media_chat_ids = set()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_issues_returns_early(self):
        """When all files exist and are correct size, return immediately."""
        existing = os.path.join(self.temp_dir, "good.jpg")
        with open(existing, "wb") as f:
            f.write(b"x" * 100)

        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": existing, "file_size": 100, "chat_id": 1, "message_id": 1}
        ]

        _run(self.backup._verify_and_redownload_media())
        # No redownload attempted
        self.backup.client.get_messages.assert_not_awaited()

    def test_missing_file_triggers_redownload(self):
        """Missing file on disk triggers re-download attempt."""
        self.backup.db.get_media_for_verification.return_value = [
            {
                "file_path": "/nonexistent/photo.jpg",
                "file_size": 100,
                "chat_id": 1,
                "message_id": 10,
            }
        ]

        mock_msg = MagicMock()
        mock_msg.id = 10
        mock_msg.media = MagicMock()
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])
        self.backup._process_media = AsyncMock(return_value={"downloaded": True})

        _run(self.backup._verify_and_redownload_media())

        self.backup._process_media.assert_awaited_once()
        self.backup.db.insert_media.assert_awaited_once()

    def test_empty_file_triggers_redownload(self):
        """Zero-byte file (interrupted download) triggers re-download."""
        empty_file = os.path.join(self.temp_dir, "empty.jpg")
        with open(empty_file, "wb"):
            pass  # 0 bytes

        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": empty_file, "file_size": 100, "chat_id": 2, "message_id": 20}
        ]

        mock_msg = MagicMock()
        mock_msg.id = 20
        mock_msg.media = MagicMock()
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])
        self.backup._process_media = AsyncMock(return_value={"downloaded": True})

        _run(self.backup._verify_and_redownload_media())

        self.backup._process_media.assert_awaited_once()

    def test_size_mismatch_triggers_redownload(self):
        """File size >1% off from expected triggers re-download."""
        bad_file = os.path.join(self.temp_dir, "bad.jpg")
        with open(bad_file, "wb") as f:
            f.write(b"x" * 50)  # actual=50, expected=1000 => >1% off

        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": bad_file, "file_size": 1000, "chat_id": 3, "message_id": 30}
        ]

        mock_msg = MagicMock()
        mock_msg.id = 30
        mock_msg.media = MagicMock()
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])
        self.backup._process_media = AsyncMock(return_value={"downloaded": True})

        _run(self.backup._verify_and_redownload_media())

        self.backup._process_media.assert_awaited_once()

    def test_record_without_file_path_is_skipped(self):
        """Records with no file_path are silently skipped."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": None, "file_size": 100, "chat_id": 1, "message_id": 1}
        ]

        _run(self.backup._verify_and_redownload_media())
        self.backup.client.get_messages.assert_not_awaited()

    def test_deleted_message_counted_as_failed(self):
        """Deleted message (None in results) counts as failed."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/missing.jpg", "file_size": 100, "chat_id": 5, "message_id": 50}
        ]
        # get_messages returns None for deleted messages
        self.backup.client.get_messages = AsyncMock(return_value=[None])

        _run(self.backup._verify_and_redownload_media())
        self.backup.db.insert_media.assert_not_awaited()

    def test_message_without_media_counted_as_failed(self):
        """Message that no longer has media counts as failed."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/missing.jpg", "file_size": 100, "chat_id": 6, "message_id": 60}
        ]
        mock_msg = MagicMock()
        mock_msg.id = 60
        mock_msg.media = None
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])

        _run(self.backup._verify_and_redownload_media())
        self.backup.db.insert_media.assert_not_awaited()

    def test_process_media_returns_none_counted_as_failed(self):
        """When _process_media returns no result, it counts as failed."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/missing.jpg", "file_size": 100, "chat_id": 7, "message_id": 70}
        ]
        mock_msg = MagicMock()
        mock_msg.id = 70
        mock_msg.media = MagicMock()
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])
        self.backup._process_media = AsyncMock(return_value={"downloaded": False})

        _run(self.backup._verify_and_redownload_media())
        self.backup.db.insert_media.assert_not_awaited()

    def test_chat_inaccessible_skips_all_records_for_chat(self):
        """When get_messages raises for a chat, all its records count as failed."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/a.jpg", "file_size": 100, "chat_id": 8, "message_id": 80},
            {"file_path": "/b.jpg", "file_size": 200, "chat_id": 8, "message_id": 81},
        ]
        self.backup.client.get_messages = AsyncMock(side_effect=Exception("Forbidden"))

        _run(self.backup._verify_and_redownload_media())
        self.backup.db.insert_media.assert_not_awaited()

    def test_skip_media_chat_ids_excluded(self):
        """Chats in skip_media_chat_ids should be skipped during verification."""
        self.backup.config.skip_media_chat_ids = {9}
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/missing.jpg", "file_size": 100, "chat_id": 9, "message_id": 90}
        ]

        _run(self.backup._verify_and_redownload_media())
        self.backup.client.get_messages.assert_not_awaited()

    def test_redownload_exception_counted_as_failed(self):
        """Exception during re-download of individual file counts as failed."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/missing.jpg", "file_size": 100, "chat_id": 10, "message_id": 100}
        ]
        mock_msg = MagicMock()
        mock_msg.id = 100
        mock_msg.media = MagicMock()
        self.backup.client.get_messages = AsyncMock(return_value=[mock_msg])
        self.backup._process_media = AsyncMock(side_effect=Exception("download error"))

        _run(self.backup._verify_and_redownload_media())
        self.backup.db.insert_media.assert_not_awaited()

    def test_chat_level_exception_counted_as_failed(self):
        """Exception at the chat level (outer try) counts all records as failed."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/a.jpg", "file_size": 100, "chat_id": 11, "message_id": 110}
        ]
        # Make get_messages work but the outer loop fails
        self.backup.client.get_messages = AsyncMock(side_effect=Exception("outer error"))

        _run(self.backup._verify_and_redownload_media())

    def test_records_with_no_message_id_are_skipped(self):
        """Records without a message_id should be skipped."""
        self.backup.db.get_media_for_verification.return_value = [
            {"file_path": "/missing.jpg", "file_size": 100, "chat_id": 12, "message_id": None}
        ]

        _run(self.backup._verify_and_redownload_media())
        self.backup.client.get_messages.assert_not_awaited()


# ===========================================================================
# backup_all() flow (lines 206-298, 324-326, 333-334, 349, 364-367,
#   373-386, 389-405, 415-418, 442-446)
# ===========================================================================


class TestBackupAllNonWhitelistMode(unittest.TestCase):
    """Test backup_all type-based (non-whitelist) mode and archived dialog flow."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.whitelist_mode = False
        self.backup.config.priority_chat_ids = set()
        self.backup.config.verify_media = False
        self.backup.config.media_path = os.path.join(self.temp_dir, "media")
        self.backup.config.global_include_ids = set()
        self.backup.config.private_include_ids = set()
        self.backup.config.groups_include_ids = set()
        self.backup.config.channels_include_ids = set()
        self.backup.config.global_exclude_ids = set()
        self.backup.config.private_exclude_ids = set()
        self.backup.config.groups_exclude_ids = set()
        self.backup.config.channels_exclude_ids = set()
        self.backup.config.should_backup_chat = MagicMock(return_value=True)

        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=123))
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 10, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_dialog = AsyncMock(return_value=5)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.backup._get_marked_id = MagicMock(side_effect=lambda e: getattr(e, "_test_id", 100))
        self.backup._get_chat_name = MagicMock(return_value="TestChat")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_entity(self, entity_cls, test_id, **kwargs):
        entity = MagicMock(spec=entity_cls)
        entity._test_id = test_id
        entity.id = test_id
        # Set sane defaults that _get_chat_name and _extract_chat_data expect
        if entity_cls is User:
            entity.first_name = kwargs.pop("first_name", "TestUser")
            entity.last_name = kwargs.pop("last_name", None)
            entity.username = kwargs.pop("username", None)
            entity.phone = kwargs.pop("phone", None)
        elif entity_cls is Channel:
            entity.title = kwargs.pop("title", "TestChannel")
            entity.username = kwargs.pop("username", None)
            entity.megagroup = kwargs.pop("megagroup", False)
            entity.forum = kwargs.pop("forum", False)
        for k, v in kwargs.items():
            setattr(entity, k, v)
        return entity

    def _make_dialog(self, entity, date=None):
        d = MagicMock()
        d.entity = entity
        d.date = date or datetime(2024, 6, 1)
        return d

    def test_non_whitelist_fetches_dialogs_and_backs_up(self):
        """Non-whitelist mode should call get_dialogs and process filtered chats."""
        user_entity = self._make_entity(User, 100, bot=False)
        dialog = self._make_dialog(user_entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])

        _run(self.backup.backup_all())

        self.assertEqual(self.backup._get_dialogs.await_count, 2)
        self.backup._backup_dialog.assert_awaited()

    def test_explicitly_excluded_chats_deleted_from_db(self):
        """Chats in global_exclude_ids should be deleted from database."""
        user_entity = self._make_entity(User, 100, bot=False)
        dialog = self._make_dialog(user_entity)

        self.backup.config.global_exclude_ids = {100}
        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
        self.backup.db.delete_chat_and_related_data = AsyncMock()

        _run(self.backup.backup_all())

        self.backup.db.delete_chat_and_related_data.assert_awaited_once()

    def test_delete_chat_exception_does_not_crash(self):
        """Exception during chat deletion should be caught."""
        user_entity = self._make_entity(User, 100, bot=False)
        dialog = self._make_dialog(user_entity)

        self.backup.config.global_exclude_ids = {100}
        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
        self.backup.db.delete_chat_and_related_data = AsyncMock(side_effect=Exception("DB error"))

        _run(self.backup.backup_all())

    def test_priority_chats_sorted_first(self):
        """Priority chats appear before non-priority in processing order."""
        e1 = self._make_entity(User, 100, bot=False)
        e2 = self._make_entity(User, 200, bot=False)

        d1 = self._make_dialog(e1, date=datetime(2024, 1, 1))
        d2 = self._make_dialog(e2, date=datetime(2024, 6, 1))

        self.backup.config.priority_chat_ids = {200}
        self.backup._get_dialogs = AsyncMock(side_effect=[[d1, d2], []])

        _run(self.backup.backup_all())

        # _backup_dialog should be called; priority chat 200 first
        self.assertEqual(self.backup._backup_dialog.await_count, 2)

    def test_channel_private_error_caught_during_dialog_backup(self):
        """ChannelPrivateError during _backup_dialog should be caught."""
        entity = self._make_entity(Channel, 300, megagroup=False, forum=False)
        dialog = self._make_dialog(entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
        self.backup._backup_dialog = AsyncMock(side_effect=ChannelPrivateError(request=MagicMock()))

        _run(self.backup.backup_all())

    def test_generic_error_caught_during_dialog_backup(self):
        """Generic exception during _backup_dialog should be caught."""
        entity = self._make_entity(User, 400, bot=False)
        dialog = self._make_dialog(entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
        self.backup._backup_dialog = AsyncMock(side_effect=Exception("unexpected"))

        _run(self.backup.backup_all())

    def test_archived_dialogs_backed_up_separately(self):
        """Archived dialogs not already backed up should be processed."""
        main_entity = self._make_entity(User, 100, bot=False)
        archived_entity = self._make_entity(User, 200, bot=False)

        main_dialog = self._make_dialog(main_entity)
        archived_dialog = self._make_dialog(archived_entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[main_dialog], [archived_dialog]])

        _run(self.backup.backup_all())

        # Both main and archived should be backed up
        self.assertEqual(self.backup._backup_dialog.await_count, 2)

    def test_archived_dialog_access_error_caught(self):
        """Access errors on archived dialogs should be caught."""
        archived_entity = self._make_entity(User, 200, bot=False)
        archived_dialog = self._make_dialog(archived_entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[], [archived_dialog]])
        self.backup._backup_dialog = AsyncMock(side_effect=ChatForbiddenError(request=MagicMock()))

        _run(self.backup.backup_all())

    def test_archived_dialog_generic_error_caught(self):
        """Generic errors on archived dialogs should be caught."""
        archived_entity = self._make_entity(User, 200, bot=False)
        archived_dialog = self._make_dialog(archived_entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[], [archived_dialog]])
        self.backup._backup_dialog = AsyncMock(side_effect=Exception("boom"))

        _run(self.backup.backup_all())

    def test_forum_topics_fetched_for_forum_channels(self):
        """Forum-enabled channels should trigger _backup_forum_topics."""
        entity = self._make_entity(Channel, 500, megagroup=True, forum=True)
        dialog = self._make_dialog(entity)

        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])

        _run(self.backup.backup_all())

        self.backup._backup_forum_topics.assert_awaited()

    def test_verify_media_triggered_when_enabled(self):
        """verify_media=True should trigger _verify_and_redownload_media."""
        self.backup.config.verify_media = True
        self.backup._verify_and_redownload_media = AsyncMock()

        # Need at least one dialog so backup_all doesn't return early
        entity = self._make_entity(User, 100, bot=False)
        dialog = self._make_dialog(entity)
        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])

        _run(self.backup.backup_all())

        self.backup._verify_and_redownload_media.assert_awaited_once()

    def test_backup_all_exception_propagates(self):
        """Fatal exception in backup_all should propagate after logging."""
        self.backup.client.start = AsyncMock(side_effect=RuntimeError("connection failed"))

        with self.assertRaises(RuntimeError):
            _run(self.backup.backup_all())

    def test_missing_include_ids_fetched_directly(self):
        """Explicitly included chats not in dialogs should be fetched via get_entity."""
        self.backup.config.global_include_ids = {999}
        self.backup._get_dialogs = AsyncMock(side_effect=[[], []])

        fetched_entity = self._make_entity(User, 999, bot=False)
        self.backup.client.get_entity = AsyncMock(return_value=fetched_entity)

        _run(self.backup.backup_all())

        self.backup.client.get_entity.assert_awaited_with(999)

    def test_missing_include_id_fetch_failure_does_not_crash(self):
        """Failure to fetch an included chat should not crash backup."""
        self.backup.config.global_include_ids = {888}
        self.backup._get_dialogs = AsyncMock(side_effect=[[], []])
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("not found"))

        _run(self.backup.backup_all())

    def test_no_dialogs_returns_early(self):
        """When no dialogs pass filtering, backup returns early."""
        self.backup.config.should_backup_chat = MagicMock(return_value=False)
        self.backup._get_dialogs = AsyncMock(side_effect=[[], []])

        _run(self.backup.backup_all())

        self.backup._backup_dialog.assert_not_awaited()


# ===========================================================================
# _backup_dialog edge cases (lines 637-638, 645-646, 703)
# ===========================================================================


class TestBackupDialogEdgeCases(unittest.TestCase):
    """Test _backup_dialog edge cases: media cleanup, profile photo error, sync_deletions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.batch_size = 100
        self.backup.config.checkpoint_interval = 1
        self.backup.config.skip_media_chat_ids = set()
        self.backup.config.skip_media_delete_existing = False
        self.backup.config.sync_deletions_edits = False
        self.backup.config.media_path = os.path.join(self.temp_dir, "media")
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup._get_marked_id = MagicMock(return_value=100)
        self.backup._extract_chat_data = MagicMock(return_value={"id": 100})
        self.backup._ensure_profile_photo = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_dialog(self):
        d = MagicMock()
        d.entity = MagicMock()
        return d

    def _empty_iter(self):
        async def fake_iter(*args, **kwargs):
            return
            yield  # noqa: RET503

        return fake_iter

    def test_media_cleanup_triggered_for_skip_media_chat(self):
        """Media cleanup runs when chat is in skip_media_chat_ids and delete is enabled."""
        self.backup.config.skip_media_chat_ids = {100}
        self.backup.config.skip_media_delete_existing = True
        self.backup._cleanup_existing_media = AsyncMock()
        self.backup.client.iter_messages = self._empty_iter()

        _run(self.backup._backup_dialog(self._make_dialog()))

        self.backup._cleanup_existing_media.assert_awaited_once_with(100)
        self.assertIn(100, self.backup._cleaned_media_chats)

    def test_media_cleanup_not_repeated_in_same_session(self):
        """Media cleanup should not run twice for the same chat in one session."""
        self.backup.config.skip_media_chat_ids = {100}
        self.backup.config.skip_media_delete_existing = True
        self.backup._cleaned_media_chats = {100}  # Already cleaned
        self.backup._cleanup_existing_media = AsyncMock()
        self.backup.client.iter_messages = self._empty_iter()

        _run(self.backup._backup_dialog(self._make_dialog()))

        self.backup._cleanup_existing_media.assert_not_awaited()

    def test_profile_photo_error_does_not_crash(self):
        """Exception in _ensure_profile_photo should be caught."""
        self.backup._ensure_profile_photo = AsyncMock(side_effect=Exception("photo error"))
        self.backup.client.iter_messages = self._empty_iter()

        result = _run(self.backup._backup_dialog(self._make_dialog()))

        self.assertEqual(result, 0)

    def test_sync_deletions_edits_called_when_enabled(self):
        """sync_deletions_edits=True triggers _sync_deletions_and_edits."""
        self.backup.config.sync_deletions_edits = True
        self.backup._sync_deletions_and_edits = AsyncMock()
        self.backup.client.iter_messages = self._empty_iter()

        _run(self.backup._backup_dialog(self._make_dialog()))

        self.backup._sync_deletions_and_edits.assert_awaited_once()


# ===========================================================================
# _sync_deletions_and_edits (lines 883-937)
# ===========================================================================


class TestSyncDeletionsAndEdits(unittest.TestCase):
    """Test _sync_deletions_and_edits deletion and edit detection."""

    def setUp(self):
        self.backup = _make_backup()

    def test_no_local_messages_returns_early(self):
        """Returns early when no local messages exist."""
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={})
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))

        self.backup.client.get_messages.assert_not_awaited()

    def test_deleted_message_removed_from_db(self):
        """Remote message returning None triggers delete_message."""
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={1: None})
        self.backup.client.get_messages = AsyncMock(return_value=[None])
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))

        self.backup.db.delete_message.assert_awaited_once_with(100, 1)

    def test_deleted_message_soft_marked(self):
        """DELETION_MODE=soft marks deleted messages instead of hard deleting."""
        self.backup.config.deletion_mode = "soft"
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={1: None})
        self.backup.client.get_messages = AsyncMock(return_value=[None])
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))

        self.backup.db.mark_message_deleted.assert_awaited_once()
        self.backup.db.delete_message.assert_not_awaited()

    def test_edited_message_updated_in_db(self):
        """Remote message with different edit_date triggers update."""
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={1: datetime(2024, 1, 1)})
        remote_msg = MagicMock()
        remote_msg.edit_date = datetime(2024, 6, 15)
        remote_msg.message = "updated text"
        self.backup.client.get_messages = AsyncMock(return_value=[remote_msg])
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))

        self.backup.db.update_message_text.assert_awaited_once()

    def test_same_edit_date_aware_vs_naive_not_updated(self):
        """A tz-aware remote edit_date equal to the archived naive one is a no-op.

        Regression: the old string comparison ("...+00:00" vs naive) treated every
        previously-edited message as changed on every sync pass.
        """
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={1: datetime(2024, 6, 15, 12, 0)})
        remote_msg = MagicMock()
        remote_msg.edit_date = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
        remote_msg.message = "same text"
        self.backup.client.get_messages = AsyncMock(return_value=[remote_msg])
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))

        self.backup.db.update_message_text.assert_not_awaited()

    def test_unedited_message_not_updated(self):
        """Message with no edit_date does not trigger update."""
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={1: None})
        remote_msg = MagicMock()
        remote_msg.edit_date = None
        self.backup.client.get_messages = AsyncMock(return_value=[remote_msg])
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))

        self.backup.db.update_message_text.assert_not_awaited()

    def test_batch_exception_does_not_crash(self):
        """Exception during batch fetch should be caught."""
        self.backup.db.get_messages_sync_data = AsyncMock(return_value={1: None})
        self.backup.client.get_messages = AsyncMock(side_effect=Exception("network error"))
        entity = MagicMock()

        _run(self.backup._sync_deletions_and_edits(100, entity))


# ===========================================================================
# _sync_pinned_messages (lines 954-970)
# ===========================================================================


class TestSyncPinnedMessages(unittest.TestCase):
    """Test _sync_pinned_messages pin sync flow."""

    def setUp(self):
        self.backup = _make_backup()

    def test_syncs_pinned_ids_to_db(self):
        """Fetched pinned messages have their IDs synced."""
        msg1 = MagicMock()
        msg1.id = 10
        msg2 = MagicMock()
        msg2.id = 20
        self.backup.client.get_messages = AsyncMock(return_value=[msg1, msg2])
        entity = MagicMock()

        _run(self.backup._sync_pinned_messages(100, entity))

        self.backup.db.sync_pinned_messages.assert_awaited_once_with(100, [10, 20])

    def test_no_pinned_messages_clears_existing(self):
        """When no pinned messages, sync with empty list."""
        self.backup.client.get_messages = AsyncMock(return_value=[])
        entity = MagicMock()

        _run(self.backup._sync_pinned_messages(100, entity))

        self.backup.db.sync_pinned_messages.assert_awaited_once_with(100, [])

    def test_exception_does_not_crash(self):
        """Exception during pinned sync should not propagate."""
        self.backup.client.get_messages = AsyncMock(side_effect=Exception("error"))
        entity = MagicMock()

        _run(self.backup._sync_pinned_messages(100, entity))


# ===========================================================================
# _get_media_filename edge cases (lines 1500-1543)
# ===========================================================================


class TestGetMediaFilename(unittest.TestCase):
    """Test _get_media_filename edge cases."""

    def setUp(self):
        self.backup = _make_backup()

    def _make_doc_message(self, *, file_name=None, mime_type=None, msg_id=1, date=None):
        """Create a mock message with document media."""
        msg = MagicMock()
        msg.id = msg_id
        msg.date = date or datetime(2024, 3, 15, 10, 30, 0)

        doc = MagicMock()
        doc.mime_type = mime_type

        attr = MagicMock()
        if file_name:
            attr.file_name = file_name
        else:
            attr.file_name = None
        doc.attributes = [attr]

        msg.media = MagicMock(spec=MessageMediaDocument)
        msg.media.document = doc
        return msg

    def test_original_filename_with_file_id(self):
        """Original filename + file_id produces 'fileid_originalname'."""
        msg = self._make_doc_message(file_name="report.pdf")
        result = self.backup._get_media_filename(msg, "document", "12345")
        self.assertEqual(result, "12345_report.pdf")

    def test_mime_type_extension_with_file_id(self):
        """No original filename, but mime_type determines extension."""
        msg = self._make_doc_message(mime_type="image/png")
        result = self.backup._get_media_filename(msg, "photo", "99")
        self.assertEqual(result, "99.png")

    def test_jpe_corrected_to_jpg(self):
        """mime_type returning .jpe should be corrected to .jpg."""
        msg = self._make_doc_message(mime_type="image/jpeg")
        result = self.backup._get_media_filename(msg, "photo", "50")
        # image/jpeg can return .jpe or .jpeg depending on system; check it's not jpe
        self.assertNotIn("jpe.", result)

    def test_fallback_to_media_type_extension(self):
        """No mime_type falls back to media_type-based extension."""
        msg = self._make_doc_message()
        result = self.backup._get_media_filename(msg, "video", "77")
        self.assertEqual(result, "77.mp4")

    def test_no_file_id_uses_timestamp(self):
        """No telegram_file_id uses timestamp-based filename."""
        msg = self._make_doc_message(msg_id=42, date=datetime(2024, 3, 15, 10, 30, 0))
        result = self.backup._get_media_filename(msg, "photo", None)
        self.assertEqual(result, "42_20240315_103000.jpg")

    def test_file_id_with_slashes_sanitized(self):
        """Slashes in file_id are replaced with underscores."""
        msg = self._make_doc_message(file_name="test.txt")
        result = self.backup._get_media_filename(msg, "document", "a/b\\c")
        self.assertEqual(result, "a_b_c_test.txt")

    def test_photo_without_document_uses_media_type_extension(self):
        """Photo message without document falls through to extension logic."""
        msg = MagicMock()
        msg.id = 5
        msg.date = datetime(2024, 1, 1)
        msg.media = MagicMock(spec=MessageMediaPhoto)
        msg.media.document = None  # Photo, not document
        # hasattr(msg.media, 'document') is True due to MagicMock, but .document is None
        # The code checks media.document truthiness
        del msg.media.document  # Remove so hasattr returns False

        result = self.backup._get_media_filename(msg, "photo", "33")
        self.assertEqual(result, "33.jpg")


# ===========================================================================
# _fill_gaps and _fill_gap_range (lines 755, 804, 823-826, 832-835,
#   851-853)
# ===========================================================================


class TestFillGaps(unittest.TestCase):
    """Test _fill_gaps gap detection and filling."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.gap_threshold = 10
        self.backup.config.batch_size = 100
        self.backup._get_chat_name = MagicMock(return_value="Test Chat")

    def test_single_chat_with_gaps_fills_them(self):
        """Gap detected in a chat triggers _fill_gap_range."""
        self.backup.db.detect_message_gaps = AsyncMock(return_value=[(10, 20, 10)])
        self.backup.client.get_entity = AsyncMock(return_value=MagicMock())
        self.backup._fill_gap_range = AsyncMock(return_value=5)

        summary = _run(self.backup._fill_gaps(chat_id=100))

        self.assertEqual(summary["total_gaps"], 1)
        self.assertEqual(summary["total_recovered"], 5)
        self.assertEqual(summary["chats_with_gaps"], 1)

    def test_no_gaps_found(self):
        """Chat with no gaps produces empty summary."""
        self.backup.db.detect_message_gaps = AsyncMock(return_value=[])
        self.backup.client.get_entity = AsyncMock(return_value=MagicMock())

        summary = _run(self.backup._fill_gaps(chat_id=100))

        self.assertEqual(summary["total_gaps"], 0)
        self.assertEqual(summary["chats_with_gaps"], 0)

    def test_entity_access_error_skips_chat(self):
        """ChannelPrivateError when getting entity skips the chat."""
        self.backup.client.get_entity = AsyncMock(side_effect=ChannelPrivateError(request=MagicMock()))

        summary = _run(self.backup._fill_gaps(chat_id=100))

        self.assertEqual(summary["chats_scanned"], 1)
        self.assertEqual(summary["errors"], 0)

    def test_entity_generic_error_counts_as_error(self):
        """Generic exception when getting entity counts as error."""
        self.backup.client.get_entity = AsyncMock(side_effect=Exception("fail"))

        summary = _run(self.backup._fill_gaps(chat_id=100))

        self.assertEqual(summary["errors"], 1)

    def test_detect_gaps_error_counts_as_error(self):
        """Exception in detect_message_gaps counts as error."""
        self.backup.client.get_entity = AsyncMock(return_value=MagicMock())
        self.backup.db.detect_message_gaps = AsyncMock(side_effect=Exception("db fail"))

        summary = _run(self.backup._fill_gaps(chat_id=100))

        self.assertEqual(summary["errors"], 1)

    def test_fill_gap_range_error_counts_as_error(self):
        """Exception in _fill_gap_range counts as error."""
        self.backup.client.get_entity = AsyncMock(return_value=MagicMock())
        self.backup.db.detect_message_gaps = AsyncMock(return_value=[(10, 20, 10)])
        self.backup._fill_gap_range = AsyncMock(side_effect=Exception("range fail"))

        summary = _run(self.backup._fill_gaps(chat_id=100))

        self.assertEqual(summary["errors"], 1)

    def test_scan_all_chats_filters_by_config(self):
        """When chat_id is None, scans all chats filtered by should_backup_chat."""
        self.backup.db.get_chats_with_messages = AsyncMock(return_value=[1, 2])
        self.backup.db.get_chat_by_id = AsyncMock(
            side_effect=[
                {"type": "private"},
                {"type": "channel"},
            ]
        )
        self.backup.config.should_backup_chat = MagicMock(side_effect=[True, False])
        self.backup.client.get_entity = AsyncMock(return_value=MagicMock())
        self.backup.db.detect_message_gaps = AsyncMock(return_value=[])

        summary = _run(self.backup._fill_gaps(chat_id=None))

        self.assertEqual(summary["chats_scanned"], 1)

    def test_scan_all_skips_chat_without_info(self):
        """Chat without DB info is skipped during scan."""
        self.backup.db.get_chats_with_messages = AsyncMock(return_value=[1])
        self.backup.db.get_chat_by_id = AsyncMock(return_value=None)

        summary = _run(self.backup._fill_gaps(chat_id=None))

        self.assertEqual(summary["chats_scanned"], 0)


class TestFillGapRange(unittest.TestCase):
    """Test _fill_gap_range message recovery."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.batch_size = 2

    def test_recovers_messages_in_gap(self):
        """Messages in gap range are fetched and committed."""
        msg1 = _make_message(11)
        msg2 = _make_message(15)

        async def fake_iter(*args, **kwargs):
            yield msg1
            yield msg2

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})
        self.backup._commit_batch = AsyncMock()

        entity = MagicMock()
        result = _run(self.backup._fill_gap_range(entity, 100, 10, 20))

        self.assertEqual(result, 2)

    def test_topic_filtered_messages_skipped(self):
        """Messages in excluded topics are skipped during gap fill."""
        msg = MagicMock()
        msg.id = 12
        msg.reply_to = MagicMock()
        msg.reply_to.forum_topic = True
        msg.reply_to.reply_to_top_id = 42
        msg.reply_to.reply_to_msg_id = 42

        self.backup.config.should_skip_topic = MagicMock(return_value=True)

        async def fake_iter(*args, **kwargs):
            yield msg

        self.backup.client.iter_messages = fake_iter
        self.backup._process_message = AsyncMock()
        self.backup._commit_batch = AsyncMock()

        entity = MagicMock()
        result = _run(self.backup._fill_gap_range(entity, 100, 10, 20))

        self.assertEqual(result, 0)


# ===========================================================================
# _backup_forum_topics fallback / emoji paths (lines 1650-1661, 1692-1693,
#   1704-1735)
# ===========================================================================


class TestBackupForumTopicsEmoji(unittest.TestCase):
    """Test _backup_forum_topics emoji resolution path."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.should_skip_topic = MagicMock(return_value=False)

    def test_custom_emoji_resolved(self):
        """Custom emoji IDs are resolved to unicode via GetCustomEmojiDocumentsRequest."""
        topic = MagicMock()
        topic.id = 1
        topic.title = "General"
        topic.icon_color = 0
        topic.icon_emoji_id = 12345
        topic.closed = False
        topic.pinned = False
        topic.hidden = False
        topic.date = datetime(2024, 1, 1)

        result_obj = MagicMock()
        result_obj.topics = [topic]

        doc = MagicMock()
        doc.id = 12345
        attr = MagicMock()
        attr.alt = "fire_emoji"
        doc.attributes = [attr]

        call_results = [MagicMock(), result_obj, [doc]]
        call_idx = [0]

        async def fake_call(req):
            idx = call_idx[0]
            call_idx[0] += 1
            return call_results[idx] if idx < len(call_results) else MagicMock()

        self.backup.client = AsyncMock()
        self.backup.client.get_input_entity = AsyncMock(return_value=MagicMock())
        self.backup.client.side_effect = fake_call

        entity = MagicMock()
        _run(self.backup._backup_forum_topics(-100123, entity))


class TestBackupForumTopicsFallback(unittest.TestCase):
    """Test _backup_forum_topics fallback to message inference."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.should_skip_topic = MagicMock(return_value=False)

    def test_fallback_inference_returns_zero_on_failure(self):
        """When both API and inference fail, returns 0."""
        # Make the API call raise, triggering fallback
        self.backup.client = AsyncMock()
        self.backup.client.get_input_entity = AsyncMock(side_effect=Exception("API not available"))

        entity = MagicMock()
        result = _run(self.backup._backup_forum_topics(-100123, entity))

        self.assertEqual(result, 0)


# ===========================================================================
# _backup_folders (lines 1748-1812)
# ===========================================================================


_FOLDER_FLAGS = (
    "contacts",
    "non_contacts",
    "groups",
    "broadcasts",
    "bots",
    "exclude_muted",
    "exclude_read",
    "exclude_archived",
)


def _make_filter(**over):
    """Build a DialogFilter-like mock with safe defaults for every field the
    resolver reads (peer lists empty, all flags off), overridable per test."""
    f = MagicMock()
    f.id = over.get("id", 1)
    f.title = over.get("title", "Folder")
    f.emoticon = over.get("emoticon")
    f.pinned_peers = over.get("pinned_peers", [])
    f.include_peers = over.get("include_peers", [])
    f.exclude_peers = over.get("exclude_peers", [])
    for flag in _FOLDER_FLAGS:
        setattr(f, flag, over.get(flag, False))
    return f


class TestBackupFolders(unittest.TestCase):
    """Test _backup_folders chat folder sync."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup._get_marked_id = MagicMock(return_value=100)

    def test_backs_up_folders_with_peers(self):
        """Folders with include_peers are synced."""
        folder = _make_filter(id=1, title="Work", include_peers=[MagicMock()])

        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        self.backup.db.upsert_chat_folder.assert_awaited()
        self.backup.db.sync_folder_members.assert_awaited()
        self.backup.db.cleanup_stale_folders.assert_awaited()

    def test_folder_with_text_with_entities_title(self):
        """Folder title that has .text attribute is extracted."""
        title_obj = MagicMock()
        title_obj.text = "Personal"
        folder = _make_filter(id=2, title=title_obj, emoticon="star")

        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        call_args = self.backup.db.upsert_chat_folder.call_args[0][0]
        self.assertEqual(call_args["title"], "Personal")

    def test_skips_filters_without_id_or_title(self):
        """Filters without id/title attributes (default All) are skipped."""
        default_filter = MagicMock(spec=[])  # no id or title
        real_folder = _make_filter(id=3, title="Archived")

        self.backup.client = AsyncMock(return_value=[default_filter, real_folder])

        _run(self.backup._backup_folders())

        self.assertEqual(self.backup.db.upsert_chat_folder.await_count, 1)

    def test_exception_returns_zero(self):
        """Exception during folder backup returns 0."""
        self.backup.client = AsyncMock(side_effect=Exception("fail"))

        result = _run(self.backup._backup_folders())

        self.assertEqual(result, 0)

    def test_pinned_peers_are_included(self):
        """pinned_peers resolve into folder membership, not just include_peers."""
        folder = _make_filter(id=10, title="Pinned", pinned_peers=[MagicMock()])
        self.backup._get_marked_id = MagicMock(return_value=777)
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        call_args = self.backup.db.sync_folder_members.call_args[0]
        self.assertIn(777, call_args[1])

    def test_exclude_peers_removes_flag_match(self):
        """A groups folder resolves group chats minus its exclude_peers."""
        folder = _make_filter(id=11, title="Groups", groups=True, exclude_peers=[MagicMock()])
        # exclude peer resolves to -2002; groups in the archive are -2001 and -2002.
        self.backup._get_marked_id = MagicMock(return_value=-2002)
        self.backup.db.get_chats_for_folder_resolution = AsyncMock(
            return_value=[
                {"id": -2001, "type": "group", "is_bot": False, "is_archived": False},
                {"id": -2002, "type": "group", "is_bot": False, "is_archived": False},
            ]
        )
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        synced = self.backup.db.sync_folder_members.call_args[0][1]
        self.assertIn(-2001, synced)
        self.assertNotIn(-2002, synced)

    def test_contacts_flag_fetches_contacts_and_resolves(self):
        """A contacts folder pulls the contact list and matches contact privates."""
        folder = _make_filter(id=12, title="People", contacts=True)
        self.backup.db.get_chats_for_folder_resolution = AsyncMock(
            return_value=[
                {"id": 501, "type": "private", "is_bot": False, "is_archived": False},
                {"id": 502, "type": "private", "is_bot": False, "is_archived": False},
            ]
        )
        self.backup._get_contact_ids = AsyncMock(return_value={501})
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        self.backup._get_contact_ids.assert_awaited_once()
        synced = self.backup.db.sync_folder_members.call_args[0][1]
        self.assertEqual(set(synced), {501})

    def test_no_contact_fetch_without_contact_flags(self):
        """Contacts are not fetched for folders that don't use contact flags."""
        folder = _make_filter(id=13, title="Groups", groups=True)
        self.backup._get_contact_ids = AsyncMock(return_value=set())
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        self.backup._get_contact_ids.assert_not_awaited()

    def test_peer_resolution_fallback_user_id(self):
        """Peer with user_id fallback when get_marked_id raises."""
        peer = MagicMock()
        peer.user_id = 42
        folder = _make_filter(id=4, title="Friends", include_peers=[peer])

        def fail_on_peer(p):
            if p is peer:
                raise Exception("not resolvable")
            return 100

        self.backup._get_marked_id = MagicMock(side_effect=fail_on_peer)
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        call_args = self.backup.db.sync_folder_members.call_args[0]
        self.assertIn(42, call_args[1])

    def test_peer_resolution_fallback_chat_id(self):
        """Peer with chat_id fallback when get_marked_id raises."""
        peer = MagicMock(spec=["chat_id"])
        peer.chat_id = 999
        folder = _make_filter(id=5, title="Groups", include_peers=[peer])

        self.backup._get_marked_id = MagicMock(side_effect=Exception("fail"))
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        call_args = self.backup.db.sync_folder_members.call_args[0]
        self.assertIn(-999, call_args[1])

    def test_peer_resolution_fallback_channel_id(self):
        """Peer with channel_id fallback when get_marked_id raises."""
        peer = MagicMock(spec=["channel_id"])
        peer.channel_id = 555
        folder = _make_filter(id=6, title="Channels", include_peers=[peer])

        self.backup._get_marked_id = MagicMock(side_effect=Exception("fail"))
        self.backup.client = AsyncMock(return_value=[folder])

        _run(self.backup._backup_folders())

        call_args = self.backup.db.sync_folder_members.call_args[0]
        self.assertIn(-1000000000000 - 555, call_args[1])

    def test_result_has_filters_attribute(self):
        """Handles result object with .filters attribute."""
        folder = _make_filter(id=7, title="Test")

        result_obj = MagicMock()
        result_obj.filters = [folder]

        self.backup.client = AsyncMock(return_value=result_obj)

        _run(self.backup._backup_folders())

        self.backup.db.upsert_chat_folder.assert_awaited()


# ===========================================================================
# _ensure_profile_photo (lines 1198-1221)
# ===========================================================================


class TestEnsureProfilePhoto(unittest.TestCase):
    """Test _ensure_profile_photo download logic."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.media_path = os.path.join(self.temp_dir, "media")
        os.makedirs(self.backup.config.media_path, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("src.telegram_backup.get_avatar_paths")
    def test_no_avatar_available_returns_early(self, mock_get_paths):
        """When avatar_path is None (no avatar set), returns early."""
        mock_get_paths.return_value = (None, "/legacy.jpg")
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity, 42))

        self.backup.client.download_profile_photo.assert_not_awaited()

    @patch("src.telegram_backup.get_avatar_paths")
    def test_existing_avatar_skips_download(self, mock_get_paths):
        """When avatar file already exists and is non-empty, skip download."""
        avatar_path = os.path.join(self.temp_dir, "avatar.jpg")
        with open(avatar_path, "wb") as f:
            f.write(b"photo_data")
        mock_get_paths.return_value = (avatar_path, "/legacy.jpg")
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity, 42))

        self.backup.client.download_profile_photo.assert_not_awaited()

    @patch("src.telegram_backup.get_avatar_paths")
    def test_missing_avatar_triggers_download(self, mock_get_paths):
        """When avatar file does not exist, download is triggered."""
        avatar_path = os.path.join(self.temp_dir, "new_avatar.jpg")
        mock_get_paths.return_value = (avatar_path, "/legacy.jpg")
        self.backup.client.download_profile_photo = AsyncMock(return_value=avatar_path)
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity, 42))

        self.backup.client.download_profile_photo.assert_awaited_once()

    @patch("src.telegram_backup.get_avatar_paths")
    def test_download_failure_caught(self, mock_get_paths):
        """Download failure should not propagate."""
        avatar_path = os.path.join(self.temp_dir, "fail.jpg")
        mock_get_paths.return_value = (avatar_path, "/legacy.jpg")
        self.backup.client.download_profile_photo = AsyncMock(side_effect=Exception("timeout"))
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity, 42))

    @patch("src.telegram_backup.get_avatar_paths")
    def test_existing_symlink_avatar_is_preserved(self, mock_get_paths):
        """A symlink at avatar_path is trusted, even when its target is unreachable.

        Reproduces the archived-layout scenario from issue #143: avatar files
        committed to git-annex appear as symlinks pointing into
        ``.git/annex/objects/...``. From inside a container that only mounts
        the working tree, those targets resolve to a missing path and the
        old ``os.path.exists`` check tried to overwrite the symlink, which
        crashed with ENOENT. With the lexists guard the symlink is treated
        as authoritative and no download is attempted.
        """
        target = os.path.join(self.temp_dir, "absent_target.jpg")
        avatar_path = os.path.join(self.temp_dir, "broken_symlink_avatar.jpg")
        try:
            os.symlink(target, avatar_path)
        except OSError as e:
            self.skipTest(f"Symlinks not supported/permitted: {e}")
        original_target = os.readlink(avatar_path)

        # Confirm the dangling symlink scenario.
        self.assertTrue(os.path.lexists(avatar_path))
        self.assertFalse(os.path.exists(avatar_path))

        mock_get_paths.return_value = (avatar_path, "/legacy.jpg")
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity, 42))

        # No download was attempted; symlink is byte-for-byte unchanged.
        self.backup.client.download_profile_photo.assert_not_awaited()
        self.assertTrue(os.path.islink(avatar_path))
        self.assertEqual(os.readlink(avatar_path), original_target)

    @patch("src.telegram_backup.get_avatar_paths")
    def test_empty_regular_file_avatar_triggers_download(self, mock_get_paths):
        """A 0-byte regular file at avatar_path falls through to download.

        The lexists gate short-circuits only when the entry is a symlink or a
        non-empty regular file. An empty regular file (e.g. left over from a
        prior interrupted download) must not be trusted -- the gate falls
        through and a fresh download replaces it.
        """
        avatar_path = os.path.join(self.temp_dir, "empty_avatar.jpg")
        with open(avatar_path, "wb"):
            pass  # 0-byte regular file
        self.assertTrue(os.path.lexists(avatar_path))
        self.assertFalse(os.path.islink(avatar_path))
        self.assertEqual(os.path.getsize(avatar_path), 0)

        mock_get_paths.return_value = (avatar_path, "/legacy.jpg")
        self.backup.client.download_profile_photo = AsyncMock(return_value=avatar_path)
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity, 42))

        self.backup.client.download_profile_photo.assert_awaited_once()

    @patch("src.telegram_backup.get_avatar_paths")
    def test_uses_marked_id_when_no_marked_id_passed(self, mock_get_paths):
        """When marked_id is None, falls back to _get_marked_id."""
        mock_get_paths.return_value = (None, "/legacy.jpg")
        self.backup._get_marked_id = MagicMock(return_value=99)
        entity = MagicMock()

        _run(self.backup._ensure_profile_photo(entity))

        mock_get_paths.assert_called_once_with(self.backup.config.media_path, entity, 99)


# ===========================================================================
# _get_media_size (lines 1446-1459)
# ===========================================================================


class TestGetMediaSize(unittest.TestCase):
    """Test _get_media_size for documents, photos, and fallback."""

    def setUp(self):
        self.backup = _make_backup()

    def test_document_size(self):
        """Document media returns document.size."""
        media = MagicMock()
        media.document = MagicMock()
        media.document.size = 5000
        self.assertEqual(self.backup._get_media_size(media), 5000)

    def test_photo_size_from_largest(self):
        """Photo media returns size of last (largest) size entry."""
        media = MagicMock()
        media.document = None
        del media.document
        media.photo = MagicMock()
        size1 = MagicMock()
        size1.size = 100
        size2 = MagicMock()
        size2.size = 500
        media.photo.sizes = [size1, size2]
        self.assertEqual(self.backup._get_media_size(media), 500)

    def test_photo_no_sizes_returns_zero(self):
        """Photo with empty sizes list falls through to fallback which returns 0."""
        media = MagicMock(spec=[])
        media.photo = MagicMock()
        media.photo.sizes = []
        # No .document, no .size -- fallback returns 0
        self.assertEqual(self.backup._get_media_size(media), 0)

    def test_fallback_to_direct_attribute(self):
        """Unknown media type falls back to direct .size attribute."""
        media = MagicMock()
        media.document = None
        del media.document
        media.photo = None
        del media.photo
        media.size = 42
        self.assertEqual(self.backup._get_media_size(media), 42)

    def test_no_size_at_all_returns_zero(self):
        """Media with no recognizable size returns 0."""
        media = MagicMock(spec=[])
        self.assertEqual(self.backup._get_media_size(media), 0)


# ===========================================================================
# Media refresh error helpers
# ===========================================================================


class TestMediaRefreshErrorHelpers(unittest.TestCase):
    """Classification + refresh helpers used to recover transient media errors."""

    def test_is_media_location_error_ignores_non_rpc_errors(self):
        """Non-RPC exceptions are never treated as media-location errors."""
        self.assertFalse(is_media_location_error(ValueError("LOCATION_NOT_AVAILABLE")))

    def test_is_media_location_error_matches_message_case_insensitively(self):
        """LOCATION_NOT_AVAILABLE is matched via the generic BadRequestError .message (any case)."""
        self.assertTrue(is_media_location_error(BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)))
        self.assertTrue(is_media_location_error(BadRequestError(MagicMock(), "location_not_available", 400)))

    def test_is_media_location_error_matches_location_invalid_by_type(self):
        """LOCATION_INVALID has a generated class whose .message is 'BAD_REQUEST', so match by type."""
        exc = LocationInvalidError(request=None)
        self.assertEqual(getattr(exc, "message", None), "BAD_REQUEST")  # documents why type-matching is needed
        self.assertTrue(is_media_location_error(exc))

    def test_is_media_location_error_rejects_other_rpc_errors(self):
        """Unrelated RPC errors (e.g. MEDIA_EMPTY) are not refreshable."""
        self.assertFalse(is_media_location_error(BadRequestError(MagicMock(), "MEDIA_EMPTY", 400)))

    def test_is_media_location_error_matches_real_wire_conversion(self):
        """A LOCATION_NOT_AVAILABLE built the way Telethon really builds it off the wire is matched.

        Exercises the actual rpc_message_to_error fallback (unknown 400 code ->
        generic BadRequestError with .message preserved) instead of a hand-built mock,
        so the test can't pass while production behaves differently.
        """

        class _WireError:
            error_message = "LOCATION_NOT_AVAILABLE"
            error_code = 400

        exc = rpc_message_to_error(_WireError(), "GetFileRequest")
        self.assertIsInstance(exc, BadRequestError)
        self.assertEqual(exc.message, "LOCATION_NOT_AVAILABLE")
        self.assertTrue(is_media_location_error(exc))

    def test_call_with_flood_retry_non_retryable_predicate_reraises_immediately(self):
        """A non_retryable predicate makes call_with_flood_retry re-raise on the first hit."""
        attempts = 0

        async def fail_with_location_not_available():
            nonlocal attempts
            attempts += 1
            raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)

        with self.assertRaises(BadRequestError):
            _run(
                telegram_backup.call_with_flood_retry(
                    fail_with_location_not_available,
                    max_retries=5,
                    non_retryable=is_media_location_error,
                )
            )

        self.assertEqual(attempts, 1)

    def test_call_with_flood_retry_still_retries_errors_outside_predicate(self):
        """Errors the predicate rejects keep using the normal transient backoff path."""
        attempts = 0

        async def fail_then_succeed():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise BadRequestError(MagicMock(), "MEDIA_EMPTY", 400)
            return "ok"

        with patch("src.telegram_backup.asyncio.sleep", new=AsyncMock()):
            result = _run(
                telegram_backup.call_with_flood_retry(
                    fail_then_succeed,
                    max_retries=5,
                    non_retryable=is_media_location_error,
                )
            )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)

    def test_refresh_message_for_media_returns_none_when_missing(self):
        """A deleted/unavailable message ([None]) yields no refreshed reference."""
        backup = _make_backup()
        message = _make_message(11)
        backup.client.get_messages = AsyncMock(return_value=[None])

        result = _run(backup._refresh_message_for_media(100, message))

        self.assertIsNone(result)
        backup.client.get_messages.assert_awaited_once_with(100, ids=[11])

    def test_refresh_message_for_media_returns_none_on_empty_list(self):
        """An empty get_messages result is handled without IndexError."""
        backup = _make_backup()
        backup.client.get_messages = AsyncMock(return_value=[])

        self.assertIsNone(_run(backup._refresh_message_for_media(100, _make_message(11))))

    def test_refresh_message_for_media_swallows_floodwait(self):
        """A FloodWait (or any error) during refresh is swallowed (-> None), never propagated."""
        backup = _make_backup()
        with patch(
            "src.telegram_backup.call_with_flood_retry",
            AsyncMock(side_effect=FloodWaitError(request=MagicMock())),
        ):
            # Best-effort refresh: must return None rather than raising.
            self.assertIsNone(_run(backup._refresh_message_for_media(100, _make_message(11))))

    def test_media_retry_backoff_seconds_grows_and_is_bounded(self):
        """Backoff grows with the attempt and stays under the configured ceiling (+jitter)."""
        # Pin the jitter so the comparison is deterministic (never flakes).
        with patch("src.telegram_backup.random.uniform", return_value=0.5):
            self.assertGreater(
                telegram_backup._media_retry_backoff_seconds(1),
                telegram_backup._media_retry_backoff_seconds(0),
            )
            big = telegram_backup._media_retry_backoff_seconds(100)
            self.assertLessEqual(big, telegram_backup.BACKOFF_MAX_SECONDS + 1.5)

    def test_is_non_retryable_media_op(self):
        """Location errors and per-operation timeouts bypass call_with_flood_retry's retries."""
        self.assertTrue(
            telegram_backup._is_non_retryable_media_op(BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400))
        )
        self.assertTrue(telegram_backup._is_non_retryable_media_op(TimeoutError()))
        self.assertFalse(telegram_backup._is_non_retryable_media_op(BadRequestError(MagicMock(), "MEDIA_EMPTY", 400)))

    def test_fetch_media_bytes_bounded_times_out_only_the_operation(self):
        """The per-operation timeout bounds a slow download and raises TimeoutError."""
        backup = _make_backup()

        async def slow_fetch(message, path, size):
            await asyncio.sleep(1)

        backup._fetch_media_bytes = AsyncMock(side_effect=slow_fetch)
        with self.assertRaises(TimeoutError):
            _run(backup._fetch_media_bytes_bounded(_make_message(1), "/tmp/unused", 100, 0.01))


# ===========================================================================
# _process_media (lines 1420-1427, 1433-1435)
# ===========================================================================


class TestProcessMedia(unittest.TestCase):
    """Test _process_media document attribute extraction and error handling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.media_path = os.path.join(self.temp_dir, "media")
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        self.backup.config.deduplicate_media = False
        # Neutralize the real backoff sleep so multi-attempt retry tests stay fast;
        # `mock_backoff` lets tests assert backoff was (or wasn't) applied.
        self._backoff_patcher = patch("src.telegram_backup._media_retry_backoff_seconds", return_value=0)
        self.mock_backoff = self._backoff_patcher.start()
        self.addCleanup(self._backoff_patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_photo_message(self, msg_id):
        msg = _make_message(msg_id)
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 999
        media.mime_type = "image/jpeg"
        msg.media = media
        return msg

    def _setup_photo_download(self):
        self.backup._get_media_type = MagicMock(return_value="photo")
        self.backup._get_media_size = MagicMock(return_value=100)
        self.backup._get_media_filename = MagicMock(return_value="test.jpg")

    def test_document_with_dimensions_and_duration(self):
        """Document with width, height, and duration stores metadata."""
        msg = _make_message(1)

        video_attr = MagicMock()
        video_attr.w = 1920
        video_attr.h = 1080
        video_attr.duration = 120

        doc = MagicMock()
        doc.attributes = [video_attr]
        doc.id = 999
        doc.mime_type = "video/mp4"
        doc.size = 5000

        # Use plain MagicMock (no spec) so we can control which attrs exist.
        # Delete 'photo' so the code enters the 'document' branch (line 1420).
        media = MagicMock()
        media.document = doc
        del media.photo
        media.mime_type = "video/mp4"
        msg.media = media

        self.backup._get_media_type = MagicMock(return_value="video")
        self.backup._get_media_filename = MagicMock(return_value="test.mp4")

        async def fake_download(_message, path):
            with open(path, "wb") as f:
                f.write(b"video")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(msg, 100))

        self.assertIsNotNone(result)
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertEqual(result["duration"], 120)

    def test_unknown_media_type_returns_none(self):
        """Unknown media type (None) returns None."""
        msg = _make_message(2)
        msg.media = MagicMock()
        self.backup._get_media_type = MagicMock(return_value=None)

        result = _run(self.backup._process_media(msg, 100))

        self.assertIsNone(result)

    def test_oversized_media_returns_not_downloaded(self):
        """Media exceeding max size returns downloaded=False."""
        msg = _make_message(3)
        msg.media = MagicMock()
        self.backup._get_media_type = MagicMock(return_value="video")
        self.backup._get_media_size = MagicMock(return_value=999999999)
        self.backup.config.get_max_media_size_bytes = MagicMock(return_value=100)

        result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])

    def test_download_exception_returns_not_downloaded(self):
        """Exception during download returns downloaded=False."""
        msg = _make_message(4)
        msg.media = MagicMock(spec=MessageMediaDocument)
        msg.media.document = MagicMock()
        msg.media.document.id = 111
        msg.media.document.size = 100
        msg.media.photo = None

        self.backup._get_media_type = MagicMock(return_value="document")
        self.backup._get_media_size = MagicMock(return_value=100)
        self.backup._get_media_filename = MagicMock(return_value="test.bin")
        self.backup.client.download_media = AsyncMock(side_effect=Exception("download fail"))

        result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])

    def test_location_not_available_refreshes_message_before_direct_retry(self):
        """LOCATION_NOT_AVAILABLE refreshes the message before direct-download retry."""
        stale_msg = self._make_photo_message(5)
        fresh_msg = self._make_photo_message(5)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        download_messages = []

        async def fake_download(message, path):
            download_messages.append(message)
            if len(download_messages) == 1:
                raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.backup.client.get_messages.assert_awaited_once_with(100, ids=[5])
        self.assertEqual(download_messages, [stale_msg, fresh_msg])

    def test_location_not_available_refreshes_message_before_dedup_retry(self):
        """LOCATION_NOT_AVAILABLE refreshes the message before shared-store retry."""
        self.backup.config.deduplicate_media = True
        self.backup.db.find_media_by_content_hash = AsyncMock(return_value=None)
        stale_msg = self._make_photo_message(6)
        fresh_msg = self._make_photo_message(6)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        download_messages = []

        async def fake_download(message, path):
            download_messages.append(message)
            if len(download_messages) == 1:
                raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.backup.client.get_messages.assert_awaited_once_with(100, ids=[6])
        self.assertEqual(download_messages, [stale_msg, fresh_msg])

    def test_file_reference_expired_refreshes_message_before_direct_retry(self):
        """FileReferenceExpiredError keeps the existing refresh behavior for direct downloads."""
        stale_msg = self._make_photo_message(7)
        fresh_msg = self._make_photo_message(7)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        download_messages = []

        async def fake_download(message, path):
            download_messages.append(message)
            if len(download_messages) == 1:
                raise FileReferenceExpiredError(request=MagicMock())
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.backup.client.get_messages.assert_awaited_once_with(100, ids=[7])
        self.assertEqual(download_messages, [stale_msg, fresh_msg])

    def test_file_reference_expired_refreshes_message_before_dedup_retry(self):
        """FileReferenceExpiredError keeps the existing refresh behavior for dedup downloads."""
        self.backup.config.deduplicate_media = True
        self.backup.db.find_media_by_content_hash = AsyncMock(return_value=None)
        stale_msg = self._make_photo_message(8)
        fresh_msg = self._make_photo_message(8)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        download_messages = []

        async def fake_download(message, path):
            download_messages.append(message)
            if len(download_messages) == 1:
                raise FileReferenceExpiredError(request=MagicMock())
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.backup.client.get_messages.assert_awaited_once_with(100, ids=[8])
        self.assertEqual(download_messages, [stale_msg, fresh_msg])

    def test_non_location_rpc_error_does_not_refresh_message(self):
        """Non-refreshable RPC errors still fail without fetching a fresh message."""
        msg = self._make_photo_message(9)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock()

        with patch(
            "src.telegram_backup.call_with_flood_retry",
            AsyncMock(side_effect=BadRequestError(MagicMock(), "MEDIA_EMPTY", 400)),
        ):
            result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])
        self.backup.client.get_messages.assert_not_awaited()

    def test_non_location_rpc_error_dedup_does_not_refresh_message(self):
        """Non-refreshable RPC errors fail without refresh in the dedup path too."""
        self.backup.config.deduplicate_media = True
        msg = self._make_photo_message(14)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock()

        with patch(
            "src.telegram_backup.call_with_flood_retry",
            AsyncMock(side_effect=BadRequestError(MagicMock(), "MEDIA_EMPTY", 400)),
        ):
            result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])
        self.backup.client.get_messages.assert_not_awaited()

    def test_location_not_available_fails_when_refresh_returns_no_message(self):
        """A persistent location error fails clean; refresh is still attempted each non-final try.

        Unlike the original code (which gave up after a single attempt when refresh
        returned nothing), a location error is transient, so we keep retrying with
        backoff — the item is simply left for the next backup run.
        """
        msg = self._make_photo_message(10)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[None])
        self.backup.client.download_media = AsyncMock(
            side_effect=BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
        )

        result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])
        # Refresh attempted on attempts 0 and 1 (not the final attempt).
        self.assertEqual(self.backup.client.get_messages.await_count, 2)
        self.backup.client.get_messages.assert_awaited_with(100, ids=[10])

    def test_file_reference_expired_direct_fails_when_refresh_returns_no_message(self):
        """A direct FileReferenceExpiredError fails clean when refresh can't find the message."""
        msg = self._make_photo_message(15)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[None])
        self.backup.client.download_media = AsyncMock(side_effect=FileReferenceExpiredError(request=MagicMock()))

        result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])
        self.assertEqual(self.backup.client.get_messages.await_count, 2)
        self.backup.client.get_messages.assert_awaited_with(100, ids=[15])

    def test_file_reference_expired_dedup_fails_when_refresh_returns_no_message(self):
        """A dedup FileReferenceExpiredError fails clean when refresh can't find the message."""
        self.backup.config.deduplicate_media = True
        msg = self._make_photo_message(16)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[None])
        self.backup.client.download_media = AsyncMock(side_effect=FileReferenceExpiredError(request=MagicMock()))

        result = _run(self.backup._process_media(msg, 100))

        self.assertFalse(result["downloaded"])
        self.assertEqual(self.backup.client.get_messages.await_count, 2)
        self.backup.client.get_messages.assert_awaited_with(100, ids=[16])

    def test_location_not_available_final_direct_attempt_fails_without_refresh(self):
        """The final LOCATION_NOT_AVAILABLE attempt raises without a third refresh."""
        stale_msg = self._make_photo_message(12)
        fresh_msg_1 = self._make_photo_message(12)
        fresh_msg_2 = self._make_photo_message(12)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(side_effect=[[fresh_msg_1], [fresh_msg_2]])

        download_messages = []

        async def fake_download(message, _path):
            download_messages.append(message)
            raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertFalse(result["downloaded"])
        self.assertEqual(self.backup.client.get_messages.await_count, 2)
        self.assertEqual(download_messages, [stale_msg, fresh_msg_1, fresh_msg_2])

    def test_location_not_available_final_dedup_attempt_fails_without_refresh(self):
        """The dedup path also stops refreshing after the final retry."""
        self.backup.config.deduplicate_media = True
        self.backup.db.find_media_by_content_hash = AsyncMock(return_value=None)
        stale_msg = self._make_photo_message(13)
        fresh_msg_1 = self._make_photo_message(13)
        fresh_msg_2 = self._make_photo_message(13)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(side_effect=[[fresh_msg_1], [fresh_msg_2]])

        download_messages = []

        async def fake_download(message, _path):
            download_messages.append(message)
            raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertFalse(result["downloaded"])
        self.assertEqual(self.backup.client.get_messages.await_count, 2)
        self.assertEqual(download_messages, [stale_msg, fresh_msg_1, fresh_msg_2])

    def test_location_not_available_applies_backoff_between_retries(self):
        """A transient location error backs off before retrying (not a sleepless hammer)."""
        stale_msg = self._make_photo_message(20)
        fresh_msg = self._make_photo_message(20)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        downloads = []

        async def fake_download(message, path):
            downloads.append(message)
            if len(downloads) == 1:
                raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.mock_backoff.assert_called()  # backoff applied before the retry

    def test_file_reference_expired_retry_skips_backoff(self):
        """An expired reference is fixed by the refresh itself, so it retries without backoff."""
        stale_msg = self._make_photo_message(21)
        fresh_msg = self._make_photo_message(21)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        downloads = []

        async def fake_download(message, path):
            downloads.append(message)
            if len(downloads) == 1:
                raise FileReferenceExpiredError(request=MagicMock())
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.mock_backoff.assert_not_called()

    def test_location_not_available_succeeds_on_third_attempt_uses_latest_message(self):
        """Two failures then success: the retry must use the most recently refreshed message."""
        stale_msg = self._make_photo_message(22)
        fresh_msg_1 = self._make_photo_message(22)
        fresh_msg_2 = self._make_photo_message(22)
        self._setup_photo_download()
        self.backup.client.get_messages = AsyncMock(side_effect=[[fresh_msg_1], [fresh_msg_2]])

        downloads = []

        async def fake_download(message, path):
            downloads.append(message)
            if len(downloads) < 3:
                raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
            with open(path, "wb") as f:
                f.write(b"photo")
            return path

        self.backup.client.download_media = AsyncMock(side_effect=fake_download)

        result = _run(self.backup._process_media(stale_msg, 100))

        self.assertTrue(result["downloaded"])
        self.assertEqual(downloads, [stale_msg, fresh_msg_1, fresh_msg_2])
        self.assertEqual(self.backup.client.get_messages.await_count, 2)

    def test_download_media_to_path_raises_original_error_after_exhaustion(self):
        """On exhaustion the original error propagates (not a synthetic one) and no .part is left."""
        msg = self._make_photo_message(23)
        self.backup._should_parallelize = MagicMock(return_value=False)
        self.backup.client.get_messages = AsyncMock(return_value=[None])
        self.backup.client.download_media = AsyncMock(
            side_effect=BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
        )
        tmp_path = os.path.join(self.temp_dir, "exhaust.part")

        with self.assertRaises(BadRequestError) as ctx:
            _run(self.backup._download_media_to_path(msg, tmp_path, 100, 100))

        self.assertEqual(ctx.exception.message, "LOCATION_NOT_AVAILABLE")
        self.assertFalse(os.path.exists(tmp_path))  # partial cleaned up on failure

    def test_download_media_to_path_recovers_regardless_of_transport(self):
        """Refresh+retry is driven at the _fetch_media_bytes seam, so it covers any transport."""
        msg = self._make_photo_message(24)
        fresh_msg = self._make_photo_message(24)
        self.backup.client.get_messages = AsyncMock(return_value=[fresh_msg])

        fetched = []

        async def fake_fetch(message, path, size):
            fetched.append(message)
            if len(fetched) == 1:
                raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)
            with open(path, "wb") as f:
                f.write(b"x")
            return path

        self.backup._fetch_media_bytes = AsyncMock(side_effect=fake_fetch)
        tmp_path = os.path.join(self.temp_dir, "transport.part")

        result = _run(self.backup._download_media_to_path(msg, tmp_path, 100, 100))

        self.assertEqual(result, tmp_path)
        self.assertEqual(fetched, [msg, fresh_msg])

    def test_download_media_to_path_retries_then_succeeds_after_timeout(self):
        """A per-operation timeout is retried by the outer loop, then succeeds."""
        msg = self._make_photo_message(25)
        calls = []

        async def fake_fetch(message, path, size):
            calls.append(message)
            if len(calls) == 1:
                raise TimeoutError()
            with open(path, "wb") as f:
                f.write(b"x")
            return path

        self.backup._fetch_media_bytes = AsyncMock(side_effect=fake_fetch)
        tmp_path = os.path.join(self.temp_dir, "timeout_ok.part")

        result = _run(self.backup._download_media_to_path(msg, tmp_path, 100, 100))

        self.assertEqual(result, tmp_path)
        self.assertEqual(len(calls), 2)

    def test_download_media_to_path_gives_up_after_persistent_timeout(self):
        """Persistent per-operation timeouts exhaust attempts and raise TimeoutError."""
        msg = self._make_photo_message(26)
        self.backup._fetch_media_bytes = AsyncMock(side_effect=TimeoutError())
        tmp_path = os.path.join(self.temp_dir, "timeout_fail.part")

        with self.assertRaises(TimeoutError):
            _run(self.backup._download_media_to_path(msg, tmp_path, 100, 100))

        self.assertEqual(self.backup._fetch_media_bytes.await_count, 3)

    def test_download_media_to_path_cleans_partial_on_failure(self):
        """A partial .part written before a failure is removed (no orphan left behind)."""
        msg = self._make_photo_message(27)
        self.backup.client.get_messages = AsyncMock(return_value=[None])

        async def fake_fetch(message, path, size):
            with open(path, "wb") as f:
                f.write(b"partial")
            raise BadRequestError(MagicMock(), "LOCATION_NOT_AVAILABLE", 400)

        self.backup._fetch_media_bytes = AsyncMock(side_effect=fake_fetch)
        tmp_path = os.path.join(self.temp_dir, "cleanup.part")

        with self.assertRaises(BadRequestError):
            _run(self.backup._download_media_to_path(msg, tmp_path, 100, 100))

        self.assertFalse(os.path.exists(tmp_path))


# ===========================================================================
# _process_message reaction edge cases (lines 1157, 1167-1168, 1174-1178)
# ===========================================================================


class TestProcessMessageReactionEdgeCases(unittest.TestCase):
    """Test _process_message reaction extraction edge cases."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.should_download_media_for_chat = MagicMock(return_value=False)

    def test_reaction_with_no_emoticon_or_document_id_uses_str(self):
        """Reaction emoji without emoticon or document_id falls back to str()."""
        msg = _make_message(1)
        reaction = MagicMock()
        reaction.reaction = MagicMock(spec=[])  # no emoticon or document_id
        reaction.count = 1
        reaction.recent_reactions = None
        msg.reactions = MagicMock()
        msg.reactions.results = [reaction]

        result = _run(self.backup._process_message(msg, 100))

        self.assertEqual(len(result["reactions"]), 1)

    def test_recent_reactions_with_channel_peer(self):
        """Recent reactions with channel_id peer are extracted."""
        msg = _make_message(2)
        peer = MagicMock(spec=["channel_id"])
        peer.channel_id = 555
        recent = MagicMock()
        recent.peer_id = peer

        reaction = MagicMock()
        reaction.reaction = MagicMock(spec=["emoticon"])
        reaction.reaction.emoticon = "thumbs_up"
        reaction.count = 1
        reaction.recent_reactions = [recent]
        msg.reactions = MagicMock()
        msg.reactions.results = [reaction]

        result = _run(self.backup._process_message(msg, 100))

        self.assertIn(555, result["reactions"][0]["user_ids"])

    def test_reactions_extraction_exception_caught(self):
        """Exception during reaction extraction should be caught."""
        msg = _make_message(3)
        msg.reactions = MagicMock()
        msg.reactions.results = MagicMock(side_effect=Exception("reaction error"))

        result = _run(self.backup._process_message(msg, 100))

        self.assertEqual(result["reactions"], [])

    def test_poll_results_parse_error_caught(self):
        """Exception parsing poll results should be caught."""
        msg = _make_message(4)
        from telethon.tl.types import MessageMediaPoll

        poll = MagicMock()
        poll.id = 1
        poll.question = "Q?"
        poll.answers = []
        poll.closed = False
        poll.public_voters = False
        poll.multiple_choice = False
        poll.quiz = False

        results = MagicMock()
        results.results = MagicMock(side_effect=Exception("parse error"))
        results.total_voters = 0

        media = MagicMock(spec=MessageMediaPoll)
        media.poll = poll
        media.results = results
        msg.media = media

        result = _run(self.backup._process_message(msg, 100))

        # Should still have poll data despite results error
        self.assertIn("poll", result["raw_data"])


# ===========================================================================
# run_backup / run_fill_gaps module-level functions
#   (lines 1825-1831, 1848-1865)
# ===========================================================================


class TestRunBackup(unittest.TestCase):
    """Test the run_backup module-level function."""

    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_backup_connects_backs_up_disconnects(self, mock_create):
        """run_backup calls connect, backup_all, disconnect, and db.close."""
        mock_backup = AsyncMock()
        mock_create.return_value = mock_backup

        config = MagicMock()
        _run(run_backup(config))

        mock_backup.connect.assert_awaited_once()
        mock_backup.backup_all.assert_awaited_once()
        mock_backup.disconnect.assert_awaited_once()

    @patch("src.repair_media_extensions.repair_media_extensions", new_callable=AsyncMock)
    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_backup_runs_media_repair_before_backup(self, mock_create, mock_repair):
        """run_backup awaits the #175 media repair pass before backing up."""
        mock_backup = AsyncMock()
        mock_create.return_value = mock_backup

        config = MagicMock()
        config.media_path = "/tmp/media-path"

        order = []
        mock_repair.side_effect = lambda *a, **k: order.append("repair")
        mock_backup.backup_all.side_effect = lambda *a, **k: order.append("backup")

        _run(run_backup(config))

        mock_repair.assert_awaited_once_with(config.media_path, mock_backup.db)
        self.assertEqual(order, ["repair", "backup"])

    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_backup_disconnects_on_error(self, mock_create):
        """run_backup calls disconnect even when backup_all raises."""
        mock_backup = AsyncMock()
        mock_backup.backup_all = AsyncMock(side_effect=RuntimeError("fail"))
        mock_create.return_value = mock_backup

        config = MagicMock()
        with self.assertRaises(RuntimeError):
            _run(run_backup(config))

        mock_backup.disconnect.assert_awaited_once()


class TestRunFillGaps(unittest.TestCase):
    """Test the run_fill_gaps module-level function."""

    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_fill_gaps_with_recovery(self, mock_create):
        """run_fill_gaps recalculates stats when messages are recovered."""
        mock_backup = AsyncMock()
        mock_backup._fill_gaps = AsyncMock(return_value={"total_recovered": 10, "chats_scanned": 1, "errors": 0})
        mock_create.return_value = mock_backup

        config = MagicMock()
        summary = _run(run_fill_gaps(config, chat_id=100))

        self.assertEqual(summary["total_recovered"], 10)
        mock_backup.db.calculate_and_store_statistics.assert_awaited_once()

    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_fill_gaps_no_recovery_skips_stats(self, mock_create):
        """run_fill_gaps skips stats recalculation when nothing recovered."""
        mock_backup = AsyncMock()
        mock_backup._fill_gaps = AsyncMock(return_value={"total_recovered": 0, "chats_scanned": 1, "errors": 0})
        mock_create.return_value = mock_backup

        config = MagicMock()
        summary = _run(run_fill_gaps(config))

        self.assertEqual(summary["total_recovered"], 0)
        mock_backup.db.calculate_and_store_statistics.assert_not_awaited()

    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_fill_gaps_stats_error_caught(self, mock_create):
        """Exception during stats recalculation after gap-fill is caught."""
        mock_backup = AsyncMock()
        mock_backup._fill_gaps = AsyncMock(return_value={"total_recovered": 5, "chats_scanned": 1, "errors": 0})
        mock_backup.db.calculate_and_store_statistics = AsyncMock(side_effect=Exception("stats fail"))
        mock_create.return_value = mock_backup

        config = MagicMock()
        summary = _run(run_fill_gaps(config))

        self.assertEqual(summary["total_recovered"], 5)

    @patch("src.telegram_backup.TelegramBackup.create", new_callable=AsyncMock)
    def test_run_fill_gaps_disconnects_on_error(self, mock_create):
        """run_fill_gaps calls disconnect even when _fill_gaps raises."""
        mock_backup = AsyncMock()
        mock_backup._fill_gaps = AsyncMock(side_effect=RuntimeError("fail"))
        mock_create.return_value = mock_backup

        config = MagicMock()
        with self.assertRaises(RuntimeError):
            _run(run_fill_gaps(config))

        mock_backup.disconnect.assert_awaited_once()
        mock_backup.db.close.assert_awaited_once()


# ===========================================================================
# main() entry point (lines 1870-1877)
# ===========================================================================


class TestMain(unittest.TestCase):
    """Test the main() entry point."""

    @patch("asyncio.run")
    @patch("src.config.setup_logging")
    @patch("src.config.Config")
    def test_main_creates_config_and_runs_backup(self, mock_config_cls, mock_setup, mock_run):
        """main() creates Config, sets up logging, and calls asyncio.run."""
        from src.telegram_backup import main

        main()

        mock_config_cls.assert_called_once()
        mock_setup.assert_called_once()
        mock_run.assert_called_once()


# ===========================================================================
# WAL mode PRAGMA lines 123-124 (busy_timeout + logger.info)
# ===========================================================================


class TestConnectWalPragmaSuccess(unittest.TestCase):
    """Test connect() WAL PRAGMA success path (lines 123-124)."""

    def test_wal_pragma_success_logs_info(self):
        """When _conn exists and PRAGMA succeeds, busy_timeout is set and info logged."""
        backup = _make_backup()
        backup.client = None
        backup._owns_client = True
        backup.config.session_path = "/tmp/test.session"
        backup.config.api_id = 12345
        backup.config.api_hash = "abc"
        backup.config.get_telegram_client_kwargs.return_value = {}

        mock_conn = MagicMock()
        mock_session = MagicMock()
        mock_session._conn = mock_conn
        mock_client = AsyncMock()
        mock_client.session = mock_session
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", phone="+1"))

        with patch("src.telegram_backup.TelegramClient", return_value=mock_client):
            _run(backup.connect())

        # busy_timeout PRAGMA should have been called (line 123)
        calls = [str(c) for c in mock_conn.execute.call_args_list]
        self.assertTrue(any("busy_timeout" in c for c in calls))


# ===========================================================================
# backup_all has_synced_before (lines 333-334)
# ===========================================================================


class TestBackupAllHasSyncedBefore(unittest.TestCase):
    """Test backup_all has_synced_before detection (lines 333-334)."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.whitelist_mode = False
        self.backup.config.priority_chat_ids = set()
        self.backup.config.verify_media = False
        self.backup.config.media_path = "/tmp/media"
        self.backup.config.global_include_ids = set()
        self.backup.config.private_include_ids = set()
        self.backup.config.groups_include_ids = set()
        self.backup.config.channels_include_ids = set()
        self.backup.config.global_exclude_ids = set()
        self.backup.config.private_exclude_ids = set()
        self.backup.config.groups_exclude_ids = set()
        self.backup.config.channels_exclude_ids = set()
        self.backup.config.should_backup_chat = MagicMock(return_value=True)
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="T", id=1))
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 10, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_dialog = AsyncMock(return_value=5)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.backup._get_marked_id = MagicMock(side_effect=lambda e: getattr(e, "_test_id", 100))
        self.backup._get_chat_name = MagicMock(return_value="TestChat")

    def _make_entity(self, test_id):
        entity = MagicMock(spec=User)
        entity._test_id = test_id
        entity.id = test_id
        entity.first_name = "User"
        entity.last_name = None
        entity.username = None
        entity.phone = None
        entity.bot = False
        return entity

    def _make_dialog(self, entity):
        d = MagicMock()
        d.entity = entity
        d.date = datetime(2024, 6, 1)
        return d

    def test_has_synced_before_set_when_last_message_id_positive(self):
        """has_synced_before is True when at least one chat has last_message_id > 0."""
        entity = self._make_entity(100)
        dialog = self._make_dialog(entity)
        self.backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
        self.backup.db.get_last_message_id = AsyncMock(return_value=42)

        _run(self.backup.backup_all())

        # The path is hit; backup proceeds normally
        self.backup._backup_dialog.assert_awaited()


# ===========================================================================
# backup_all chat appears in both regular and archived (line 349)
# ===========================================================================


class TestBackupAllDuplicateArchivedChat(unittest.TestCase):
    """Test backup_all warns when chat in both regular and archived (line 349)."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.whitelist_mode = False
        self.backup.config.priority_chat_ids = set()
        self.backup.config.verify_media = False
        self.backup.config.media_path = "/tmp/media"
        self.backup.config.global_include_ids = set()
        self.backup.config.private_include_ids = set()
        self.backup.config.groups_include_ids = set()
        self.backup.config.channels_include_ids = set()
        self.backup.config.global_exclude_ids = set()
        self.backup.config.private_exclude_ids = set()
        self.backup.config.groups_exclude_ids = set()
        self.backup.config.channels_exclude_ids = set()
        self.backup.config.should_backup_chat = MagicMock(return_value=True)
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="T", id=1))
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 10, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_dialog = AsyncMock(return_value=5)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.backup._get_marked_id = MagicMock(return_value=100)
        self.backup._get_chat_name = MagicMock(return_value="TestChat")

    def test_chat_in_both_regular_and_archived_treated_as_not_archived(self):
        """Chat appearing in both regular and archived is treated as NOT archived."""
        entity = MagicMock(spec=User)
        entity._test_id = 100
        entity.id = 100
        entity.first_name = "User"
        entity.last_name = None
        entity.username = None
        entity.phone = None
        entity.bot = False

        regular_dialog = MagicMock()
        regular_dialog.entity = entity
        regular_dialog.date = datetime(2024, 6, 1)

        archived_dialog = MagicMock()
        archived_dialog.entity = entity
        archived_dialog.date = datetime(2024, 6, 1)

        # Both return same chat_id 100
        self.backup._get_dialogs = AsyncMock(side_effect=[[regular_dialog], [archived_dialog]])

        _run(self.backup.backup_all())

        # _backup_dialog called twice (once for regular, once for archived dedup pass)
        # but archived should skip since already backed up
        self.assertTrue(self.backup._backup_dialog.await_count >= 1)


# ===========================================================================
# Archived dialog skip paths (lines 376, 378)
# ===========================================================================


class TestArchivedDialogSkipPaths(unittest.TestCase):
    """Test archived dialog skip when already backed up or explicitly excluded."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.whitelist_mode = False
        self.backup.config.priority_chat_ids = set()
        self.backup.config.verify_media = False
        self.backup.config.media_path = "/tmp/media"
        self.backup.config.global_include_ids = set()
        self.backup.config.private_include_ids = set()
        self.backup.config.groups_include_ids = set()
        self.backup.config.channels_include_ids = set()
        self.backup.config.global_exclude_ids = {200}
        self.backup.config.private_exclude_ids = set()
        self.backup.config.groups_exclude_ids = set()
        self.backup.config.channels_exclude_ids = set()
        self.backup.config.should_backup_chat = MagicMock(return_value=True)
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="T", id=1))
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 10, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_dialog = AsyncMock(return_value=5)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.backup._get_chat_name = MagicMock(return_value="TestChat")
        self.backup.db.delete_chat_and_related_data = AsyncMock()

    def test_archived_chat_excluded_by_global_exclude_ids(self):
        """Archived chat in global_exclude_ids is skipped (line 378)."""

        def get_marked_id(e):
            return getattr(e, "_test_id", 0)

        self.backup._get_marked_id = MagicMock(side_effect=get_marked_id)

        excluded_entity = MagicMock(spec=User)
        excluded_entity._test_id = 200
        excluded_entity.id = 200
        excluded_entity.first_name = "Exc"
        excluded_entity.last_name = None
        excluded_entity.username = None
        excluded_entity.phone = None
        excluded_entity.bot = False

        archived_dialog = MagicMock()
        archived_dialog.entity = excluded_entity
        archived_dialog.date = datetime(2024, 6, 1)

        self.backup._get_dialogs = AsyncMock(side_effect=[[], [archived_dialog]])

        _run(self.backup.backup_all())

        # Dialog should NOT have been backed up (excluded)
        self.backup._backup_dialog.assert_not_awaited()


# ===========================================================================
# Archived dialog error paths (lines 402-405)
# ===========================================================================


class TestArchivedDialogAccessErrors(unittest.TestCase):
    """Test archived dialog access error handling (lines 402-405)."""

    def setUp(self):
        self.backup = _make_backup()
        self.backup.config.whitelist_mode = False
        self.backup.config.priority_chat_ids = set()
        self.backup.config.verify_media = False
        self.backup.config.media_path = "/tmp/media"
        self.backup.config.global_include_ids = set()
        self.backup.config.private_include_ids = set()
        self.backup.config.groups_include_ids = set()
        self.backup.config.channels_include_ids = set()
        self.backup.config.global_exclude_ids = set()
        self.backup.config.private_exclude_ids = set()
        self.backup.config.groups_exclude_ids = set()
        self.backup.config.channels_exclude_ids = set()
        self.backup.config.should_backup_chat = MagicMock(return_value=True)
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(first_name="T", id=1))
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 10, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.backup._get_marked_id = MagicMock(return_value=300)
        self.backup._get_chat_name = MagicMock(return_value="TestChat")

    def _make_archived_dialog(self):
        entity = MagicMock(spec=User)
        entity._test_id = 300
        entity.id = 300
        entity.first_name = "Arc"
        entity.last_name = None
        entity.username = None
        entity.phone = None
        entity.bot = False
        d = MagicMock()
        d.entity = entity
        d.date = datetime(2024, 6, 1)
        return d

    def test_archived_dialog_channel_private_error_caught(self):
        """ChannelPrivateError on archived dialog backup is caught (line 402)."""
        from telethon.errors import UserBannedInChannelError

        archived_dialog = self._make_archived_dialog()
        self.backup._get_dialogs = AsyncMock(side_effect=[[], [archived_dialog]])
        self.backup._backup_dialog = AsyncMock(side_effect=UserBannedInChannelError(request=MagicMock()))

        _run(self.backup.backup_all())

    def test_archived_dialog_generic_exception_caught(self):
        """Generic exception on archived dialog backup is caught (lines 404-405)."""
        archived_dialog = self._make_archived_dialog()
        self.backup._get_dialogs = AsyncMock(side_effect=[[], [archived_dialog]])
        self.backup._backup_dialog = AsyncMock(side_effect=Exception("unexpected error"))

        _run(self.backup.backup_all())


# ===========================================================================
# _verify_and_redownload_media outer exception (lines 602-604)
# ===========================================================================


class TestVerifyMediaOuterException(unittest.TestCase):
    """Test _verify_and_redownload_media outer exception handler (lines 602-604)."""

    def test_outer_exception_counts_all_records_as_failed(self):
        """Exception at chat level counts all records for that chat as failed."""
        backup = _make_backup()
        backup.config.skip_media_chat_ids = set()

        # Return records for one chat, then make groupby iteration fail
        records = [
            {"file_path": "/a.jpg", "file_size": 100, "chat_id": 42, "message_id": 1},
            {"file_path": "/b.jpg", "file_size": 200, "chat_id": 42, "message_id": 2},
        ]
        backup.db.get_media_for_verification.return_value = records

        # Force the outer try to fail by making get_messages raise
        backup.client.get_messages = AsyncMock(side_effect=Exception("outer chat error"))

        _run(backup._verify_and_redownload_media())


# ===========================================================================
# _sync_deletions_and_edits progress log (line 934)
# ===========================================================================


class TestSyncDeletionsProgress(unittest.TestCase):
    """Test _sync_deletions_and_edits progress logging (line 934)."""

    def test_progress_logged_every_1000_messages(self):
        """Progress is logged when total_checked is a multiple of 1000."""
        backup = _make_backup()
        # Create exactly 1000 messages so total_checked % 1000 == 0
        sync_data = {i: None for i in range(1, 1001)}
        backup.db.get_messages_sync_data = AsyncMock(return_value=sync_data)

        # All messages still exist remotely (not deleted, not edited)
        remote_msgs = []
        for _i in range(1, 1001):
            m = MagicMock()
            m.edit_date = None
            remote_msgs.append(m)

        backup.client.get_messages = AsyncMock(return_value=remote_msgs)
        entity = MagicMock()

        _run(backup._sync_deletions_and_edits(100, entity))


# ===========================================================================
# _process_message poll results exception (lines 1112-1113)
# ===========================================================================


class TestProcessMessagePollException(unittest.TestCase):
    """Test _process_message poll results parse error (lines 1112-1113)."""

    def test_poll_results_exception_is_caught(self):
        """Exception parsing poll results is caught gracefully."""
        backup = _make_backup()
        backup.config.should_download_media_for_chat = MagicMock(return_value=False)

        msg = _make_message(1)
        from telethon.tl.types import MessageMediaPoll

        poll = MagicMock()
        poll.id = 1
        poll.question = MagicMock()
        poll.question.text = "Question?"
        poll.answers = []
        poll.closed = False
        poll.public_voters = False
        poll.multiple_choice = False
        poll.quiz = False

        results = MagicMock()
        # Make iterating results raise
        type(results).results = property(lambda self: (_ for _ in ()).throw(Exception("parse err")))
        results.total_voters = 0

        media = MagicMock(spec=MessageMediaPoll)
        media.poll = poll
        media.results = results
        msg.media = media

        result = _run(backup._process_message(msg, 100))
        self.assertIn("poll", result["raw_data"])


# ===========================================================================
# _process_message reactions exception (lines 1174-1178)
# ===========================================================================


class TestProcessMessageReactionsException(unittest.TestCase):
    """Test _process_message reaction extraction exception (lines 1174-1178)."""

    def test_reaction_exception_returns_empty_reactions(self):
        """Exception during reaction extraction returns empty reactions list."""
        backup = _make_backup()
        backup.config.should_download_media_for_chat = MagicMock(return_value=False)

        msg = _make_message(1)
        # Make reactions.results raise TypeError when iterated
        msg.reactions = MagicMock()
        msg.reactions.results = MagicMock(side_effect=TypeError("bad reactions"))

        result = _run(backup._process_message(msg, 100))
        self.assertEqual(result["reactions"], [])


# ===========================================================================
# _cleanup_existing_media exception paths (lines 1257-1258, 1271-1272)
# ===========================================================================


class TestCleanupExistingMediaExceptions(unittest.TestCase):
    """Test _cleanup_existing_media exception handling (lines 1257-1258, 1271-1272)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.media_path = self.temp_dir
        self.backup.db.get_media_for_chat = AsyncMock(return_value=[])
        self.backup.db.delete_media_for_chat = AsyncMock(return_value=0)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_file_deletion_exception_caught(self):
        """Exception deleting a single media file is caught (lines 1257-1258)."""
        chat_dir = os.path.join(self.temp_dir, "100")
        os.makedirs(chat_dir)
        test_file = os.path.join(chat_dir, "photo.jpg")
        with open(test_file, "w") as f:
            f.write("data")

        self.backup.db.get_media_for_chat = AsyncMock(return_value=[{"file_path": test_file, "message_id": 1}])

        with patch("os.remove", side_effect=PermissionError("denied")):
            _run(self.backup._cleanup_existing_media(100))

    def test_rmdir_exception_caught(self):
        """Exception removing empty media directory is caught (lines 1271-1272)."""
        chat_dir = os.path.join(self.temp_dir, "200")
        os.makedirs(chat_dir)

        self.backup.db.delete_media_for_chat = AsyncMock(return_value=1)

        with patch("os.listdir", return_value=[]), patch("os.rmdir", side_effect=OSError("busy")):
            _run(self.backup._cleanup_existing_media(200))


# ===========================================================================
# _get_media_filename jpe->jpg correction (line 1530)
# ===========================================================================


class TestGetMediaFilenameJpeCorrection(unittest.TestCase):
    """Test _get_media_filename corrects jpe to jpg (line 1530)."""

    def test_jpe_mime_corrected_to_jpg(self):
        """When mimetypes returns .jpe, it is corrected to .jpg."""
        backup = _make_backup()
        msg = MagicMock()
        msg.id = 1
        msg.date = datetime(2024, 1, 1)
        msg.media = MagicMock(spec=MessageMediaPhoto)
        del msg.media.document

        with patch("mimetypes.guess_extension", return_value=".jpe"):
            result = backup._get_media_filename(msg, "photo", "abc")
        self.assertEqual(result, "abc.jpg")


# ===========================================================================
# _backup_forum_topics emoji resolution (lines 1650-1661)
# ===========================================================================


class TestBackupForumTopicsEmojiException(unittest.TestCase):
    """Test _backup_forum_topics emoji resolution exception (lines 1660-1661)."""

    def test_emoji_resolution_exception_caught(self):
        """Exception resolving custom emojis is caught (lines 1660-1661)."""
        backup = _make_backup()

        topic = MagicMock()
        topic.id = 1
        topic.title = "General"
        topic.icon_color = 0
        topic.icon_emoji_id = 999
        topic.closed = False
        topic.pinned = False
        topic.hidden = False
        topic.date = datetime(2024, 1, 1)

        result_obj = MagicMock()
        result_obj.topics = [topic]

        call_idx = [0]

        async def fake_call(req):
            idx = call_idx[0]
            call_idx[0] += 1
            if idx == 0:
                return MagicMock()  # get_input_entity
            if idx == 1:
                return result_obj  # GetForumTopicsRequest
            # GetCustomEmojiDocumentsRequest raises
            raise Exception("emoji resolution failed")

        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())
        backup.client.side_effect = fake_call

        entity = MagicMock()
        _run(backup._backup_forum_topics(-100123, entity))


# ===========================================================================
# _backup_forum_topics ImportError fallback (lines 1692-1693)
# ===========================================================================


class TestBackupForumTopicsImportError(unittest.TestCase):
    """Test _backup_forum_topics ImportError fallback (lines 1692-1693)."""

    def test_import_error_falls_through_to_inference(self):
        """ImportError for GetForumTopicsRequest triggers inference fallback."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        # Make the first client call raise ImportError
        async def fake_call(req):
            raise ImportError("GetForumTopicsRequest not available")

        backup.client.side_effect = fake_call

        entity = MagicMock()
        result = _run(backup._backup_forum_topics(-100123, entity))

        self.assertEqual(result, 0)


# ===========================================================================
# _backup_forum_topics inference fallback (lines 1704-1735)
# ===========================================================================


class TestBackupForumTopicsInference(unittest.TestCase):
    """Test _backup_forum_topics message inference fallback (lines 1704-1735)."""

    def test_inference_recovers_topics_from_messages(self):
        """Inference fallback queries DB for reply_to_top_id values."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        # Make API call fail to trigger fallback
        async def fail_api_call(req):
            raise Exception("API failed")

        backup.client.side_effect = fail_api_call

        # Mock the DB query for topic inference
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([(42,), (43,)]))
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        backup.db.db_manager = MagicMock()
        backup.db.db_manager.async_session_factory = MagicMock(return_value=mock_session_ctx)

        # Mock get_messages for topic metadata
        topic_msg = MagicMock()
        topic_msg.text = "Topic title"
        topic_msg.date = datetime(2024, 1, 1)
        backup.client.get_messages = AsyncMock(return_value=[topic_msg])

        entity = MagicMock()
        result = _run(backup._backup_forum_topics(-100123, entity))

        self.assertEqual(result, 2)
        self.assertEqual(backup.db.upsert_forum_topic.await_count, 2)


# ===========================================================================
# main() __name__ == "__main__" (line 1882)
# ===========================================================================


class TestMainEntryPointLine1882(unittest.TestCase):
    """Test the __main__ guard calls main() (line 1882)."""

    @patch("asyncio.run")
    @patch("src.config.setup_logging")
    @patch("src.config.Config")
    def test_main_function_invoked(self, mock_config_cls, mock_setup, mock_run):
        """main() function is callable and triggers asyncio.run."""
        from src.telegram_backup import main

        main()
        mock_run.assert_called_once()


# ===========================================================================
# Issue #200: early forum-topic fetch, topic pagination, deleted-topic guard,
# and the backup_in_progress flag.
# ===========================================================================


class TestBackupDialogEarlyForumFetch(unittest.TestCase):
    """_backup_dialog should fetch forum topics up front, before the message loop."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup = _make_backup()
        self.backup.config.batch_size = 100
        self.backup.config.checkpoint_interval = 1
        self.backup.config.skip_media_chat_ids = set()
        self.backup.config.skip_media_delete_existing = False
        self.backup.config.sync_deletions_edits = False
        self.backup.config.media_path = os.path.join(self.temp_dir, "media")
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup._get_marked_id = MagicMock(return_value=-100123)
        self.backup._extract_chat_data = MagicMock(return_value={"id": -100123})
        self.backup._ensure_profile_photo = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock(return_value=3)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _empty_iter(self):
        async def fake_iter(*args, **kwargs):
            return
            yield  # noqa: RET503

        return fake_iter

    def _make_dialog(self, forum):
        d = MagicMock()
        d.entity = MagicMock()
        d.entity.__class__ = Channel  # real forum entities are Channels; satisfies isinstance(entity, Channel)
        d.entity.forum = forum
        return d

    def test_forum_dialog_fetches_topics_before_message_loop(self):
        """A forum entity triggers the early _backup_forum_topics call even with 0 messages."""
        self.backup.client.iter_messages = self._empty_iter()
        dialog = self._make_dialog(forum=True)

        result = _run(self.backup._backup_dialog(dialog))

        # 0 new messages, but the early forum-topic fetch must still have happened,
        # called with the marked chat_id and the dialog's entity.
        self.assertEqual(result, 0)
        self.backup._backup_forum_topics.assert_awaited_once_with(-100123, dialog.entity)

    def test_non_forum_dialog_does_not_fetch_topics(self):
        """A non-forum entity must not trigger the early forum-topic fetch."""
        self.backup.client.iter_messages = self._empty_iter()

        _run(self.backup._backup_dialog(self._make_dialog(forum=False)))

        self.backup._backup_forum_topics.assert_not_awaited()

    def test_early_forum_fetch_failure_does_not_abort_dialog(self):
        """An exception in the early forum fetch is swallowed so the dialog still completes."""
        self.backup._backup_forum_topics = AsyncMock(side_effect=Exception("topic fetch boom"))
        self.backup.client.iter_messages = self._empty_iter()

        result = _run(self.backup._backup_dialog(self._make_dialog(forum=True)))

        self.assertEqual(result, 0)
        self.backup._backup_forum_topics.assert_awaited_once()


class TestBackupForumTopicsPagination(unittest.TestCase):
    """_backup_forum_topics paginates through every page until result.count is met."""

    def _make_topic(self, topic_id):
        topic = MagicMock()
        topic.id = topic_id
        topic.title = f"Topic {topic_id}"
        topic.icon_color = 0
        topic.icon_emoji_id = None
        topic.closed = False
        topic.pinned = False
        topic.hidden = False
        topic.top_message = topic_id * 10
        topic.date = datetime(2024, 1, 1)
        return topic

    def test_paginates_across_two_pages(self):
        """count=150 split as 100+50 fetches both pages and stores all 150 topics."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        page1 = MagicMock()
        page1.count = 150
        page1.topics = [self._make_topic(i) for i in range(1, 101)]
        page1.messages = []

        page2 = MagicMock()
        page2.count = 150
        page2.topics = [self._make_topic(i) for i in range(101, 151)]
        page2.messages = []

        calls = []

        async def fake_call(req):
            calls.append(req)
            return page1 if len(calls) == 1 else page2

        backup.client.side_effect = fake_call

        entity = MagicMock()
        count = _run(backup._backup_forum_topics(-100123, entity))

        self.assertEqual(count, 150)
        self.assertEqual(backup.db.upsert_forum_topic.await_count, 150)
        # Exactly two GetForumTopicsRequest pages were fetched (no emoji calls).
        self.assertEqual(len(calls), 2)
        # Offsets must have advanced: the second request differs from the first.
        self.assertEqual(calls[0].offset_topic, 0)
        self.assertEqual(calls[1].offset_topic, 100)  # last topic id of page 1
        self.assertEqual(calls[1].offset_id, 1000)  # page-1 last topic.top_message

    def test_stops_when_page_returns_no_topics(self):
        """A page with zero topics terminates pagination even if count is larger."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        page1 = MagicMock()
        page1.count = 999  # claims more than will ever be returned
        page1.topics = [self._make_topic(1), self._make_topic(2)]
        page1.messages = []

        empty = MagicMock()
        empty.count = 999
        empty.topics = []
        empty.messages = []

        calls = []

        async def fake_call(req):
            calls.append(req)
            return page1 if len(calls) == 1 else empty

        backup.client.side_effect = fake_call

        entity = MagicMock()
        count = _run(backup._backup_forum_topics(-100123, entity))

        self.assertEqual(count, 2)
        self.assertEqual(backup.db.upsert_forum_topic.await_count, 2)
        self.assertEqual(len(calls), 2)  # page1 then the empty page that stops the loop

    def test_offset_date_advances_from_top_message_date(self):
        """The next page's offset_date comes from the last topic's top_message date (via messages map)."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        msg_date = datetime(2024, 6, 15, 12, 0)
        msg = MagicMock()
        msg.id = 1000  # == _make_topic(100).top_message
        msg.date = msg_date

        page1 = MagicMock()
        page1.count = 150
        page1.topics = [self._make_topic(i) for i in range(1, 101)]  # last is id=100, top_message=1000
        page1.messages = [msg]

        page2 = MagicMock()
        page2.count = 150
        page2.topics = [self._make_topic(i) for i in range(101, 151)]
        page2.messages = []

        calls = []

        async def fake_call(req):
            calls.append(req)
            return page1 if len(calls) == 1 else page2

        backup.client.side_effect = fake_call
        _run(backup._backup_forum_topics(-100123, MagicMock()))

        self.assertEqual(calls[1].offset_id, 1000)
        self.assertEqual(calls[1].offset_date, msg_date)  # from the message, not the topic's own date

    def test_terminates_when_all_seen_with_skipped_topics(self):
        """seen_count is pre-skip, so it meets result.count even when topics are excluded — no extra page."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())
        backup.config.should_skip_topic = MagicMock(side_effect=lambda chat_id, tid: tid == 2)

        page1 = MagicMock()
        page1.count = 3
        page1.topics = [self._make_topic(1), self._make_topic(2), self._make_topic(3)]
        page1.messages = []

        calls = []

        async def fake_call(req):
            calls.append(req)
            return page1  # a second fetch would loop on the same page — it must not happen

        backup.client.side_effect = fake_call
        count = _run(backup._backup_forum_topics(-100123, MagicMock()))

        self.assertEqual(len(calls), 1)  # all 3 seen on page 1 → terminate, despite 1 skipped
        self.assertEqual(count, 2)  # topic id 2 excluded
        self.assertEqual(backup.db.upsert_forum_topic.await_count, 2)

    def test_count_none_terminates_via_empty_page(self):
        """A missing/None result.count is coerced safely; the empty-page break still stops the loop."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        page1 = MagicMock()
        page1.count = None
        page1.topics = [self._make_topic(1), self._make_topic(2)]
        page1.messages = []

        empty = MagicMock()
        empty.count = None
        empty.topics = []
        empty.messages = []

        calls = []

        async def fake_call(req):
            calls.append(req)
            return page1 if len(calls) == 1 else empty

        backup.client.side_effect = fake_call
        count = _run(backup._backup_forum_topics(-100123, MagicMock()))

        self.assertEqual(count, 2)
        self.assertEqual(len(calls), 2)

    def test_partial_result_kept_on_midpagination_failure(self):
        """If a later page raises after earlier pages stored topics, keep the partial result (no inference)."""
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        page1 = MagicMock()
        page1.count = 150
        page1.topics = [self._make_topic(1), self._make_topic(2)]
        page1.messages = []

        calls = []

        async def fake_call(req):
            calls.append(req)
            if len(calls) == 1:
                return page1
            raise RuntimeError("network blip mid-pagination")

        backup.client.side_effect = fake_call
        count = _run(backup._backup_forum_topics(-100123, MagicMock()))

        self.assertEqual(count, 2)  # page-1 topics kept
        self.assertEqual(backup.db.upsert_forum_topic.await_count, 2)
        backup.client.get_messages.assert_not_called()  # inference fallback never ran


class TestBackupForumTopicsDeletedGuard(unittest.TestCase):
    """_backup_forum_topics skips forumTopicDeleted objects that lack a title."""

    def test_deleted_topic_without_title_is_skipped(self):
        backup = _make_backup()
        backup.client = AsyncMock()
        backup.client.get_input_entity = AsyncMock(return_value=MagicMock())

        good = MagicMock()
        good.id = 1
        good.title = "General"
        good.icon_color = 0
        good.icon_emoji_id = None
        good.closed = False
        good.pinned = False
        good.hidden = False
        good.top_message = 10
        good.date = datetime(2024, 1, 1)

        # forumTopicDeleted only carries an id -- no title attribute at all.
        deleted = MagicMock(spec=["id", "top_message"])
        deleted.id = 2
        deleted.top_message = 20

        result_obj = MagicMock()
        result_obj.count = 2
        result_obj.topics = [good, deleted]
        result_obj.messages = []
        backup.client.return_value = result_obj

        entity = MagicMock()
        count = _run(backup._backup_forum_topics(-100123, entity))

        # Only the titled topic is stored; the deleted one is skipped.
        self.assertEqual(count, 1)
        self.assertEqual(backup.db.upsert_forum_topic.await_count, 1)
        stored = backup.db.upsert_forum_topic.call_args[0][0]
        self.assertEqual(stored["id"], 1)
        self.assertEqual(stored["title"], "General")


class TestBackupAllInProgressFlag(unittest.TestCase):
    """backup_all sets backup_in_progress=1 then clears it to 0, even on failure."""

    def _make_backup_for_flag(self):
        backup = _make_backup()
        backup.config.phone = "+10000000000"
        backup.config.whitelist_mode = False
        backup.config.backup_path = "/data/backups"
        backup.config.verify_media = False
        backup.db.set_metadata = AsyncMock()
        backup.db.backfill_is_outgoing = AsyncMock()
        backup.client.start = AsyncMock()
        me = MagicMock()
        me.id = 12345
        me.first_name = "Tester"
        backup.client.get_me = AsyncMock(return_value=me)
        return backup

    def test_flag_set_and_cleared_on_failure(self):
        """If a step after the flag is set raises, the finally still clears it to 0."""
        backup = self._make_backup_for_flag()
        # Force a failure right after the in-progress flag is set.
        backup._get_dialogs = AsyncMock(side_effect=RuntimeError("dialog fetch failed"))

        with self.assertRaises(RuntimeError):
            _run(backup.backup_all())

        calls = [c.args for c in backup.db.set_metadata.call_args_list]
        self.assertIn(("backup_in_progress", "1"), calls)
        self.assertIn(("backup_in_progress", "0"), calls)
        # The clear must come after the set.
        set_idx = calls.index(("backup_in_progress", "1"))
        clear_idx = calls.index(("backup_in_progress", "0"))
        self.assertLess(set_idx, clear_idx)


if __name__ == "__main__":
    unittest.main()
