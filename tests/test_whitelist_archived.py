"""Whitelist mode and is_archived: never fabricate 0, fetch real membership.

Whitelist mode skips get_dialogs (#95), so it used to write is_archived=0 for
every chat on every run — emptying the viewer's Archived section and
destroying values a type-based run had computed. Membership now comes from
batched GetPeerDialogs over exactly the resolved peers; a failed probe yields
None and the chat row keeps its stored value (upsert_chat's presence guard).
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl.types import Channel, PeerChannel, PeerChat, PeerUser

from src.telegram_backup import TelegramBackup


async def _passthrough(coro_fn, *args, **kwargs):
    return await coro_fn(*args, **kwargs)


def _channel(cid: int = 2701160643) -> Channel:
    return Channel(id=cid, title="Test Channel", access_hash=12345, date=None, photo=None)


class TestFetchArchivedMembership(unittest.TestCase):
    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1

    def _run(self, coro):
        return asyncio.run(coro)

    def test_only_folder_one_counts_and_ids_are_marked(self):
        resp = MagicMock()
        resp.dialogs = [
            MagicMock(folder_id=1, peer=PeerChannel(2701160643)),
            MagicMock(folder_id=None, peer=PeerUser(42)),
            MagicMock(folder_id=0, peer=PeerChat(77)),
        ]
        self.backup.client = AsyncMock(return_value=resp)
        self.backup.client.get_input_entity = AsyncMock(side_effect=lambda e: e)

        with patch("src.telegram_backup.call_with_flood_retry", _passthrough):
            got = self._run(self.backup._fetch_archived_membership([_channel()]))

        self.assertEqual(got, {-1002701160643})

    def test_batches_of_one_hundred(self):
        resp = MagicMock()
        resp.dialogs = []
        self.backup.client = AsyncMock(return_value=resp)
        self.backup.client.get_input_entity = AsyncMock(side_effect=lambda e: e)

        with patch("src.telegram_backup.call_with_flood_retry", _passthrough):
            got = self._run(self.backup._fetch_archived_membership([_channel(i) for i in range(1, 102)]))

        self.assertEqual(got, set())
        self.assertEqual(self.backup.client.await_count, 2)
        first, second = self.backup.client.await_args_list
        self.assertEqual(len(first.args[0].peers), 100)
        self.assertEqual(len(second.args[0].peers), 1)

    def test_failure_returns_none_not_empty(self):
        self.backup.client = AsyncMock(side_effect=RuntimeError("api down"))
        self.backup.client.get_input_entity = AsyncMock(side_effect=lambda e: e)

        with patch("src.telegram_backup.call_with_flood_retry", _passthrough):
            got = self._run(self.backup._fetch_archived_membership([_channel()]))

        self.assertIsNone(got)


class TestExtractChatDataTriState(unittest.TestCase):
    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)

    def test_none_omits_the_key_entirely(self):
        data = self.backup._extract_chat_data(_channel(), is_archived=None)
        self.assertNotIn("is_archived", data)

    def test_known_values_still_written(self):
        self.assertEqual(self.backup._extract_chat_data(_channel(), is_archived=True)["is_archived"], 1)
        self.assertEqual(self.backup._extract_chat_data(_channel(), is_archived=False)["is_archived"], 0)


class TestWhitelistBackupUsesMembership(unittest.TestCase):
    """backup_all in whitelist mode feeds the fetched membership (or None on
    probe failure) into _backup_dialog instead of a fabricated False."""

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
        self.config.reaction_resweep_days = 0.0
        os.makedirs(self.config.media_path, exist_ok=True)

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
        self.backup.config = self.config
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

    def _wire(self, raw_response):
        entity = _channel()
        client = (
            AsyncMock(return_value=raw_response)
            if raw_response is not None
            else AsyncMock(side_effect=RuntimeError("probe down"))
        )
        client.get_entity = AsyncMock(return_value=entity)
        client.get_input_entity = AsyncMock(side_effect=lambda e: e)
        client.start = AsyncMock()
        client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=123))
        self.backup.client = client
        self.backup.db.get_last_message_id = AsyncMock(return_value=0)
        self.backup.db.backfill_is_outgoing = AsyncMock()
        self.backup.db.set_metadata = AsyncMock()
        self.backup.db.upsert_chat = AsyncMock()
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 0, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_dialog = AsyncMock(return_value=0)
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()

    def test_archived_member_reaches_backup_dialog_as_true(self):
        resp = MagicMock()
        resp.dialogs = [MagicMock(folder_id=1, peer=PeerChannel(2701160643))]
        self._wire(resp)

        with patch("src.telegram_backup.call_with_flood_retry", _passthrough):
            self._run(self.backup.backup_all())

        self.assertIs(self.backup._backup_dialog.await_args.kwargs["is_archived"], True)

    def test_probe_failure_passes_none_never_false(self):
        self._wire(None)

        with patch("src.telegram_backup.call_with_flood_retry", _passthrough):
            self._run(self.backup.backup_all())

        self.assertIsNone(self.backup._backup_dialog.await_args.kwargs["is_archived"])


if __name__ == "__main__":
    unittest.main()
