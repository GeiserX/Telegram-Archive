"""Per-account metadata keys for filter-adjacent caches (8.1, #313).

``followed_migrations`` and ``whitelist_unresolved_ids`` were keyed globally;
with per-account filters two accounts would overwrite each other's state.
Account 1 keeps the bare legacy key (single-account installs unchanged);
other accounts get a suffixed key.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.db.models import account_metadata_key
from src.listener import TelegramListener
from src.telegram_backup import TelegramBackup


def _backup(account_id):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = MagicMock()
    backup.config.follow_chat_migrations = True
    backup.db = MagicMock()
    backup.db.get_metadata = AsyncMock(return_value=None)
    backup.db.set_metadata = AsyncMock()
    backup.account_id = account_id
    return backup


class TestAccountMetadataKey(unittest.TestCase):
    def test_account_one_keeps_the_legacy_key(self):
        self.assertEqual(account_metadata_key("followed_migrations", 1), "followed_migrations")

    def test_other_accounts_get_a_suffixed_key(self):
        self.assertEqual(account_metadata_key("followed_migrations", 7), "followed_migrations_account_7")
        self.assertEqual(account_metadata_key("whitelist_unresolved_ids", 2), "whitelist_unresolved_ids_account_2")


class TestBackupUsesScopedKeys(unittest.TestCase):
    async def _load_followed(self, account_id):
        backup = _backup(account_id)
        await backup._load_followed_migrations()
        return backup.db.get_metadata.call_args[0][0]

    def test_followed_migrations_read_is_account_scoped(self):
        import asyncio

        self.assertEqual(asyncio.run(self._load_followed(1)), "followed_migrations")
        self.assertEqual(asyncio.run(self._load_followed(2)), "followed_migrations_account_2")

    def test_whitelist_unresolved_roundtrip_is_account_scoped(self):
        import asyncio

        backup = _backup(2)
        asyncio.run(backup._load_whitelist_unresolved())
        self.assertEqual(backup.db.get_metadata.call_args[0][0], "whitelist_unresolved_ids_account_2")

        asyncio.run(backup._save_whitelist_unresolved({-100111}, 500))
        key, payload = backup.db.set_metadata.call_args[0]
        self.assertEqual(key, "whitelist_unresolved_ids_account_2")
        self.assertEqual(json.loads(payload), {"limit": 500, "ids": [-100111]})

    def test_account_one_roundtrip_keeps_legacy_keys(self):
        import asyncio

        backup = _backup(1)
        asyncio.run(backup._load_whitelist_unresolved())
        self.assertEqual(backup.db.get_metadata.call_args[0][0], "whitelist_unresolved_ids")
        asyncio.run(backup._save_whitelist_unresolved(set(), 0))
        self.assertEqual(backup.db.set_metadata.call_args[0][0], "whitelist_unresolved_ids")


class TestListenerUsesScopedKeys(unittest.TestCase):
    def _listener(self, account_id):
        listener = TelegramListener.__new__(TelegramListener)
        listener.config = MagicMock()
        listener.config.follow_chat_migrations = True
        listener.db = MagicMock()
        listener.db.get_metadata = AsyncMock(return_value=None)
        listener.account_id = account_id
        return listener

    def test_followed_migrations_read_is_account_scoped(self):
        import asyncio

        listener = self._listener(2)
        asyncio.run(listener._load_followed_migration_ids())
        self.assertEqual(listener.db.get_metadata.call_args[0][0], "followed_migrations_account_2")

        listener = self._listener(1)
        asyncio.run(listener._load_followed_migration_ids())
        self.assertEqual(listener.db.get_metadata.call_args[0][0], "followed_migrations")


class TestRemainingScopedKeys(unittest.TestCase):
    """The other per-account caches flagged in review: resweep, failures, listener status."""

    def test_message_failure_key_is_account_scoped(self):
        self.assertEqual(TelegramBackup._message_failure_key(-100111, 1), "message_failures_-100111")
        self.assertEqual(TelegramBackup._message_failure_key(-100111, 2), "message_failures_-100111_account_2")

    def test_reaction_resweep_state_is_account_scoped(self):
        import asyncio

        backup = _backup(2)
        backup.config.reaction_resweep_days = 7
        asyncio.run(backup._load_resweep_cycle())
        self.assertEqual(backup.db.get_metadata.call_args[0][0], "reaction_resweep_cycle_done_account_2")

    def test_listener_status_key_is_account_scoped(self):
        self.assertEqual(account_metadata_key("listener_active_since", 1), "listener_active_since")
        self.assertEqual(account_metadata_key("listener_active_since", 3), "listener_active_since_account_3")


if __name__ == "__main__":
    unittest.main()
