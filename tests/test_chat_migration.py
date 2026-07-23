"""Tests for group→supergroup migration handling (issue #228).

Covers the three stories:
- US-200: ``_process_message`` persists the migration pointer id in raw_data.
- US-201: the always-on, count-only warning fired by ``_reconcile_migrations``.
- US-202: the opt-in ``FOLLOW_CHAT_MIGRATIONS`` adopt-and-capture behaviour.

Plus the ``get_migration_markers`` adapter read used for offline detection.
"""

import asyncio
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl.types import (
    MessageActionChannelMigrateFrom,
    MessageActionChatEditTitle,
    MessageActionChatMigrateTo,
    PeerChannel,
    PeerChat,
)
from telethon.utils import get_peer_id

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.telegram_backup import TelegramBackup


def _run(coro):
    """Run a coroutine in a fresh event loop (unittest.TestCase style)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_mock_db_manager(is_sqlite=True):
    """Mock DatabaseManager whose session factory yields an async context manager."""
    db_manager = MagicMock()
    db_manager._is_sqlite = is_sqlite
    mock_session = AsyncMock()
    async_ctx = AsyncMock()
    async_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    async_ctx.__aexit__ = AsyncMock(return_value=False)
    db_manager.async_session_factory.return_value = async_ctx
    return db_manager, mock_session


# ===========================================================================
# US-200 — _process_message persists the migration pointer id
# ===========================================================================


def _make_service_message(action, text=""):
    """Minimal mock service message carrying ``action`` (reply_to explicitly None)."""
    msg = MagicMock()
    msg.id = 4242
    msg.sender = None
    msg.sender_id = 42
    msg.date = datetime(2024, 1, 15, 12, 0, 0)
    msg.text = text
    msg.reply_to_msg_id = None
    msg.reply_to = None  # avoid MagicMock truthiness triggering topic filtering
    msg.edit_date = None
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = None
    msg.media = None
    msg.reactions = None
    msg.post_author = None
    msg.action = action
    return msg


def _make_process_backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.db = AsyncMock()
    backup.client = AsyncMock()
    return backup


class TestProcessMessageMigrationPointer(unittest.TestCase):
    """US-200: the new supergroup / old group pointer id is persisted."""

    def test_migrate_to_persists_marked_channel_pointer(self):
        """MessageActionChatMigrateTo(channel_id=N) → raw_data.migrate_to_id == -(1e12+N)."""
        backup = _make_process_backup()
        action = MessageActionChatMigrateTo(channel_id=555)
        result = _run(backup._process_message(_make_service_message(action), -100200300))
        self.assertEqual(result["raw_data"]["migrate_to_id"], -(10**12 + 555))
        self.assertEqual(result["raw_data"]["migrate_to_id"], get_peer_id(PeerChannel(555)))
        self.assertEqual(result["raw_data"]["action_type"], "chat_migrate_to")
        self.assertNotIn("migrate_from_id", result["raw_data"])

    def test_migrate_from_persists_marked_chat_pointer(self):
        """MessageActionChannelMigrateFrom(chat_id=M) → raw_data.migrate_from_id == marked chat id."""
        backup = _make_process_backup()
        action = MessageActionChannelMigrateFrom(title="Old Group", chat_id=777)
        result = _run(backup._process_message(_make_service_message(action), -1000000000999))
        self.assertEqual(result["raw_data"]["migrate_from_id"], get_peer_id(PeerChat(777)))
        self.assertEqual(result["raw_data"]["action_type"], "channel_migrate_from")
        self.assertNotIn("migrate_to_id", result["raw_data"])

    def test_normal_service_action_has_no_migration_pointer(self):
        """A non-migration service message (title change) carries neither pointer."""
        backup = _make_process_backup()
        action = MessageActionChatEditTitle(title="New Title")
        result = _run(backup._process_message(_make_service_message(action), -100200300))
        self.assertNotIn("migrate_to_id", result["raw_data"])
        self.assertNotIn("migrate_from_id", result["raw_data"])
        self.assertEqual(result["raw_data"]["action_type"], "chat_edit_title")


# ===========================================================================
# US-201 / US-202 — _reconcile_migrations warn / follow
# ===========================================================================

_LOGGER = "src.telegram_backup"


def _make_reconcile_backup(follow=False):
    backup = TelegramBackup.__new__(TelegramBackup)
    cfg = MagicMock()
    cfg.follow_chat_migrations = follow
    cfg.chat_ids = set()
    cfg.global_include_ids = set()
    cfg.groups_include_ids = set()
    cfg.channels_include_ids = set()
    cfg.global_exclude_ids = set()
    cfg.groups_exclude_ids = set()
    cfg.channels_exclude_ids = set()
    backup.config = cfg
    backup.db = AsyncMock()
    backup.db.get_migration_markers = AsyncMock(return_value=[])
    backup.client = AsyncMock()
    backup._followed_migration_ids = set()
    backup._backup_dialog = AsyncMock(return_value=5)
    return backup


def _migrated_dialog(channel_id):
    """A dialog whose entity is a migrated basic group (carries .migrated_to)."""
    entity = SimpleNamespace(migrated_to=SimpleNamespace(channel_id=channel_id))
    return SimpleNamespace(entity=entity)


class TestReconcileMigrationsWarn(unittest.TestCase):
    """US-201: always-on, count-only warning; no ids leaked; deduped per run."""

    def test_warns_for_primary_migrated_entity(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100200300)
        new_id = get_peer_id(PeerChannel(555))
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        joined = "\n".join(cm.output)
        self.assertIn("migrated to a supergroup not in scope", joined)
        self.assertIn("1 tracked group", joined)
        # PII: counts only — neither the new nor the old marked id appears.
        self.assertNotIn(str(new_id), joined)
        self.assertNotIn("100200300", joined)
        backup.db.set_metadata.assert_not_awaited()

    def test_warns_from_secondary_stored_marker(self):
        """Offline migration: detected purely from a stored chat_migrate_to marker."""
        backup = _make_reconcile_backup(follow=False)
        new_id = get_peer_id(PeerChannel(999))
        backup.db.get_migration_markers = AsyncMock(return_value=[(-4242, new_id)])
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            _run(backup._reconcile_migrations([], set()))
        joined = "\n".join(cm.output)
        self.assertIn("1 tracked group", joined)
        self.assertNotIn(str(new_id), joined)

    def test_suppressed_when_new_id_already_captured(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], {new_id}))

    def test_suppressed_when_new_id_in_configured_include(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        backup.config.groups_include_ids = {new_id}
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))

    def test_suppressed_when_new_id_explicitly_excluded(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        backup.config.groups_exclude_ids = {new_id}
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))

    def test_dedups_to_single_warning_per_run(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(side_effect=[-101, -102])
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            _run(backup._reconcile_migrations([_migrated_dialog(555), _migrated_dialog(666)], set()))
        warn_lines = [line for line in cm.output if "migrated to a supergroup" in line]
        self.assertEqual(len(warn_lines), 1)
        self.assertIn("2 tracked group", warn_lines[0])

    def test_off_persists_nothing(self):
        backup = _make_reconcile_backup(follow=False)
        backup._get_marked_id = MagicMock(return_value=-100)
        _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        backup.db.set_metadata.assert_not_awaited()
        backup._backup_dialog.assert_not_awaited()


class TestReconcileMigrationsFollow(unittest.TestCase):
    """US-202: opt-in follow persists + captures this run; guards inaccessibility."""

    def test_follow_persists_and_captures_without_warning(self):
        backup = _make_reconcile_backup(follow=True)
        backup._get_marked_id = MagicMock(return_value=-100)
        backup.client.get_entity = AsyncMock(return_value=MagicMock())
        new_id = get_peer_id(PeerChannel(555))
        backed = set()
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], backed))
        # Persisted to the metadata KV under followed_migrations.
        backup.db.set_metadata.assert_awaited_once()
        key, value = backup.db.set_metadata.await_args.args
        self.assertEqual(key, "followed_migrations")
        self.assertIn(new_id, json.loads(value))
        # Captured THIS run and merged into the followed set + backed-up set.
        backup._backup_dialog.assert_awaited_once()
        self.assertIn(new_id, backup._followed_migration_ids)
        self.assertIn(new_id, backed)

    def test_follow_already_followed_is_noop(self):
        backup = _make_reconcile_backup(follow=True)
        backup._get_marked_id = MagicMock(return_value=-100)
        new_id = get_peer_id(PeerChannel(555))
        backup._followed_migration_ids = {new_id}
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            _run(backup._reconcile_migrations([_migrated_dialog(555)], set()))
        backup.db.set_metadata.assert_not_awaited()
        backup._backup_dialog.assert_not_awaited()

    def test_follow_inaccessible_channel_does_not_raise(self):
        backup = _make_reconcile_backup(follow=True)
        backup._get_marked_id = MagicMock(return_value=-100)
        backup.client.get_entity = AsyncMock(side_effect=Exception("no access"))
        new_id = get_peer_id(PeerChannel(555))
        backed = set()
        # Must not raise even though capture fails.
        _run(backup._reconcile_migrations([_migrated_dialog(555)], backed))
        backup.db.set_metadata.assert_awaited_once()  # persisted before capture attempt
        self.assertIn(new_id, backup._followed_migration_ids)
        backup._backup_dialog.assert_not_awaited()
        self.assertNotIn(new_id, backed)


# ===========================================================================
# US-202 — config flag + followed-set loading / scope predicate
# ===========================================================================


class TestFollowChatMigrationsConfig(unittest.TestCase):
    def test_default_off(self):
        with patch("os.makedirs"), patch.dict(os.environ, {"CHAT_TYPES": "private"}, clear=True):
            self.assertFalse(Config().follow_chat_migrations)

    def test_enabled_true(self):
        with (
            patch("os.makedirs"),
            patch.dict(os.environ, {"CHAT_TYPES": "private", "FOLLOW_CHAT_MIGRATIONS": "true"}, clear=True),
        ):
            self.assertTrue(Config().follow_chat_migrations)

    def test_enabled_accepts_common_truthy_variants(self):
        for variant in ("1", "yes", "on", "TRUE"):
            with (
                patch("os.makedirs"),
                patch.dict(os.environ, {"CHAT_TYPES": "private", "FOLLOW_CHAT_MIGRATIONS": variant}, clear=True),
            ):
                self.assertTrue(Config().follow_chat_migrations, variant)


class TestFollowedMigrationScope(unittest.TestCase):
    """_load_followed_migrations + _is_followed_migration (sweep scope predicate)."""

    def test_load_short_circuits_when_off(self):
        backup = _make_reconcile_backup(follow=False)
        backup.db.get_metadata = AsyncMock(return_value=json.dumps([-100, -200]))
        _run(backup._load_followed_migrations())
        self.assertEqual(backup._followed_migration_ids, set())
        backup.db.get_metadata.assert_not_awaited()

    def test_load_reads_metadata_when_on(self):
        backup = _make_reconcile_backup(follow=True)
        followed = get_peer_id(PeerChannel(555))
        backup.db.get_metadata = AsyncMock(return_value=json.dumps([followed]))
        _run(backup._load_followed_migrations())
        self.assertIn(followed, backup._followed_migration_ids)
        self.assertTrue(backup._is_followed_migration(followed))

    def test_load_malformed_degrades_to_empty(self):
        backup = _make_reconcile_backup(follow=True)
        backup.db.get_metadata = AsyncMock(return_value="not json{")
        _run(backup._load_followed_migrations())
        self.assertEqual(backup._followed_migration_ids, set())

    def test_load_missing_value_degrades_to_empty(self):
        backup = _make_reconcile_backup(follow=True)
        backup.db.get_metadata = AsyncMock(return_value=None)
        _run(backup._load_followed_migrations())
        self.assertEqual(backup._followed_migration_ids, set())

    def test_is_followed_false_when_flag_off(self):
        backup = _make_reconcile_backup(follow=False)
        backup._followed_migration_ids = {-100}
        self.assertFalse(backup._is_followed_migration(-100))

    def test_is_followed_true_when_on_and_present(self):
        backup = _make_reconcile_backup(follow=True)
        backup._followed_migration_ids = {-100}
        self.assertTrue(backup._is_followed_migration(-100))


# ===========================================================================
# get_migration_markers adapter read (offline detection source)
# ===========================================================================


class TestGetMigrationMarkers(unittest.TestCase):
    def _adapter_returning(self, rows):
        db_manager, session = _make_mock_db_manager()
        adapter = DatabaseAdapter(db_manager)
        mock_result = MagicMock()
        mock_result.all.return_value = rows
        session.execute.return_value = mock_result
        return adapter

    def test_parses_migrate_to_markers(self):
        raw = json.dumps(
            {
                "service_type": "service",
                "action_type": "chat_migrate_to",
                "migrate_to_id": get_peer_id(PeerChannel(555)),
            }
        )
        adapter = self._adapter_returning([(-777, raw)])
        self.assertEqual(_run(adapter.get_migration_markers()), [(-777, get_peer_id(PeerChannel(555)))])

    def test_skips_non_migration_rows(self):
        raw = json.dumps({"action_type": "chat_edit_title", "new_title": "x"})
        adapter = self._adapter_returning([(-1, raw)])
        self.assertEqual(_run(adapter.get_migration_markers()), [])

    def test_skips_malformed_and_empty_rows(self):
        adapter = self._adapter_returning([(-1, "not valid json"), (-2, None)])
        self.assertEqual(_run(adapter.get_migration_markers()), [])

    def test_skips_when_pointer_missing_or_non_int(self):
        no_ptr = json.dumps({"action_type": "chat_migrate_to"})
        bad_ptr = json.dumps({"action_type": "chat_migrate_to", "migrate_to_id": "nope"})
        adapter = self._adapter_returning([(-1, no_ptr), (-2, bad_ptr)])
        self.assertEqual(_run(adapter.get_migration_markers()), [])


if __name__ == "__main__":
    unittest.main()
