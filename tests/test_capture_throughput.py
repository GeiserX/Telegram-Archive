"""Capture-throughput guards: batched reaction probe + sender-fingerprint memo.

`_commit_batch` must keep #219's removals-to-zero (an empty snapshot with
stored rows still reconciles) while skipping the guaranteed-no-op reconcile
for messages with no stored rows. `_save_sender` must write a sender once per
distinct profile, re-write on change, and never memoize a failed upsert.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl.types import User

from telegram_archive.telegram_backup import TelegramBackup


def _backup_with_mock_db() -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.db = MagicMock()
    backup.db.insert_messages_batch = AsyncMock()
    backup.db.insert_media = AsyncMock()
    backup.db.reconcile_reactions = AsyncMock()
    backup.db.get_message_ids_with_reaction_rows = AsyncMock(return_value=set())
    backup.db.upsert_user = AsyncMock()
    backup.account_id = 1
    return backup


def _sender(user_id: int = 42, username: str | None = "sender") -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id
    user.username = username
    user.first_name = "Sender"
    user.last_name = None
    user.phone = None
    user.bot = False
    return user


class TestCommitBatchReactionProbe(unittest.TestCase):
    def setUp(self):
        self.backup = _backup_with_mock_db()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_rowless_empty_snapshots_skip_reconcile(self):
        """[] with no stored rows is a guaranteed no-op — never dispatched."""
        batch = [
            {"id": 1, "reactions": [{"emoji": "👍", "count": 2}]},
            {"id": 2, "reactions": []},
            {"id": 3, "reactions": None},
        ]
        self._run(self.backup._commit_batch(batch, -100500))

        self.backup.db.get_message_ids_with_reaction_rows.assert_awaited_once_with(-100500, [2], account_id=1)
        self.backup.db.reconcile_reactions.assert_awaited_once_with(
            1, -100500, [{"emoji": "👍", "count": 2}], mark_removed=True, account_id=1
        )

    def test_empty_snapshot_with_stored_rows_still_reconciles(self):
        """#219: removal-to-zero must reach reconcile when rows exist."""
        self.backup.db.get_message_ids_with_reaction_rows.return_value = {2}
        batch = [
            {"id": 1, "reactions": [{"emoji": "👍", "count": 2}]},
            {"id": 2, "reactions": []},
        ]
        self._run(self.backup._commit_batch(batch, -100500))

        calls = self.backup.db.reconcile_reactions.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args, (2, -100500, []))

    def test_no_probe_without_empty_snapshots(self):
        batch = [
            {"id": 1, "reactions": [{"emoji": "👍", "count": 1}]},
            {"id": 3, "reactions": None},
        ]
        self._run(self.backup._commit_batch(batch, -100500))

        self.backup.db.get_message_ids_with_reaction_rows.assert_not_awaited()
        self.backup.db.reconcile_reactions.assert_awaited_once()

    def test_min_snapshot_is_stored_for_a_message_without_rows(self):
        """A min snapshot of a message with no stored reactions can only add
        rows, so it is kept rather than dropped."""
        batch = [{"id": 4, "reactions": [{"emoji": "👍", "count": 3}], "_reactions_min": True}]
        self._run(self.backup._commit_batch(batch, -100500))

        self.backup.db.get_message_ids_with_reaction_rows.assert_awaited_once_with(-100500, [4], account_id=1)
        self.backup.db.reconcile_reactions.assert_awaited_once_with(
            4, -100500, [{"emoji": "👍", "count": 3}], mark_removed=True, account_id=1
        )

    def test_min_snapshot_never_reconciles_over_stored_rows(self):
        """A min snapshot may leave out this account's own reaction: with rows
        stored it could mark that reaction as taken back, so it waits."""
        self.backup.db.get_message_ids_with_reaction_rows.return_value = {4}
        batch = [
            {"id": 4, "reactions": [{"emoji": "👍", "count": 3}], "_reactions_min": True},
            {"id": 5, "reactions": [], "_reactions_min": True},
        ]
        self._run(self.backup._commit_batch(batch, -100500))

        self.backup.db.get_message_ids_with_reaction_rows.assert_awaited_once_with(-100500, [4], account_id=1)
        self.backup.db.reconcile_reactions.assert_not_awaited()


class TestSaveSenderMemo(unittest.TestCase):
    def setUp(self):
        self.backup = _backup_with_mock_db()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_identical_sender_upserts_once(self):
        self._run(self.backup._save_sender(_sender()))
        self._run(self.backup._save_sender(_sender()))
        self.backup.db.upsert_user.assert_awaited_once()

    def test_profile_change_reupserts(self):
        self._run(self.backup._save_sender(_sender(username="old")))
        self._run(self.backup._save_sender(_sender(username="new")))
        self.assertEqual(self.backup.db.upsert_user.await_count, 2)

    def test_failed_upsert_is_not_memoized(self):
        self.backup.db.upsert_user.side_effect = [RuntimeError("db down"), None]
        with self.assertRaises(RuntimeError):
            self._run(self.backup._save_sender(_sender()))
        self._run(self.backup._save_sender(_sender()))
        self.assertEqual(self.backup.db.upsert_user.await_count, 2)

    def test_non_user_sender_is_ignored(self):
        self._run(self.backup._save_sender(MagicMock()))  # not spec'd as User
        self.backup.db.upsert_user.assert_not_awaited()

    def test_cache_bound_clears_and_keeps_working(self):
        with patch("telegram_archive.telegram_backup.SENDER_CACHE_MAX_ENTRIES", 1):
            self._run(self.backup._save_sender(_sender(user_id=1)))
            self._run(self.backup._save_sender(_sender(user_id=2)))  # clears, re-adds
            self._run(self.backup._save_sender(_sender(user_id=1)))  # evicted → writes
        self.assertEqual(self.backup.db.upsert_user.await_count, 3)
        self.assertEqual(len(self.backup._sender_cache), 1)


async def _seed_message(adapter, chat_id: int, message_id: int) -> None:
    from datetime import datetime

    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": datetime(2026, 1, 1, 12, 0, 0),
            "text": "seed",
            "is_outgoing": 0,
            "sender_name": "Fixture Sender",
            "raw_data": {},
        },
        account_id=1,
    )


class TestReactionRowProbeRealEngines:
    async def test_probe_reports_live_and_tombstoned_rows(self, real_adapter):
        chat_id = 920001
        await real_adapter.upsert_chat({"id": chat_id, "type": "group", "title": "probe"}, account_id=1)
        await _seed_message(real_adapter, chat_id, 1)
        await _seed_message(real_adapter, chat_id, 2)
        await real_adapter.reconcile_reactions(
            1, chat_id, [{"emoji": "👍", "count": 3}], mark_removed=True, account_id=1
        )

        found = await real_adapter.get_message_ids_with_reaction_rows(chat_id, [1, 2], account_id=1)
        assert found == {1}

        # Tombstone the row (removal-to-zero); the probe must still report it so
        # later empty snapshots keep reconciling instead of skipping.
        await real_adapter.reconcile_reactions(1, chat_id, [], mark_removed=True, account_id=1)
        found = await real_adapter.get_message_ids_with_reaction_rows(chat_id, [1, 2], account_id=1)
        assert found == {1}

        # Wrong account sees nothing.
        found = await real_adapter.get_message_ids_with_reaction_rows(chat_id, [1, 2], account_id=9)
        assert found == set()

    async def test_probe_crosses_chunk_boundary_and_handles_empty_input(self, real_adapter):
        chat_id = 920002
        await real_adapter.upsert_chat({"id": chat_id, "type": "group", "title": "probe"}, account_id=1)
        await _seed_message(real_adapter, chat_id, 7)
        await real_adapter.reconcile_reactions(
            7, chat_id, [{"emoji": "🔥", "count": 1}], mark_removed=True, account_id=1
        )

        # 501 ids place the hit in the second 500-id chunk.
        probe_ids = list(range(10_000, 10_500)) + [7]
        found = await real_adapter.get_message_ids_with_reaction_rows(chat_id, probe_ids, account_id=1)
        assert found == {7}

        assert await real_adapter.get_message_ids_with_reaction_rows(chat_id, [], account_id=1) == set()


if __name__ == "__main__":
    unittest.main()
