"""DOWNLOAD_CHAT_DESCRIPTION: the chat's "about" text from a full-info request (#433).

The dialog entity never carries it, so the fetch is one extra request per chat
per run and is off by default. Whatever happens on that request, the chat's own
backup must go on and the row must keep the description it already has:
upsert_chat updates only the keys present in chat_data, so "nothing fetched" is
spelled as "no key", never as None.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telethon.tl.types import Channel, Chat, User

from src.telegram_backup import TelegramBackup


def _run(coro):
    return asyncio.run(coro)


def _backup(client_answer):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.config = SimpleNamespace(download_chat_description=True)
    backup.client = client_answer
    return backup


class TestFetchChatDescription(unittest.TestCase):
    def test_a_channel_answer_gives_the_description_and_the_member_count(self):
        answer = SimpleNamespace(full_chat=SimpleNamespace(about="A place for things", participants_count=1234))
        backup = _backup(AsyncMock(return_value=answer))
        fields = _run(backup._fetch_chat_description(MagicMock(spec=Channel)))
        self.assertEqual(fields, {"description": "A place for things", "participants_count": 1234})
        request = backup.client.await_args.args[0]
        self.assertEqual(type(request).__name__, "GetFullChannelRequest")

    def test_a_basic_group_is_asked_by_id_and_a_user_for_its_bio(self):
        chat = MagicMock(spec=Chat)
        chat.id = 777
        answer = SimpleNamespace(full_chat=SimpleNamespace(about="group blurb", participants_count=None))
        backup = _backup(AsyncMock(return_value=answer))
        self.assertEqual(_run(backup._fetch_chat_description(chat)), {"description": "group blurb"})
        request = backup.client.await_args.args[0]
        self.assertEqual((type(request).__name__, request.chat_id), ("GetFullChatRequest", 777))

        user_answer = SimpleNamespace(full_user=SimpleNamespace(about="a bio"))
        backup = _backup(AsyncMock(return_value=user_answer))
        self.assertEqual(_run(backup._fetch_chat_description(MagicMock(spec=User))), {"description": "a bio"})
        self.assertEqual(type(backup.client.await_args.args[0]).__name__, "GetFullUserRequest")

    def test_an_empty_about_clears_a_stale_description(self):
        """The chat had a description once and removed it: the row must follow, not keep the old text."""
        answer = SimpleNamespace(full_chat=SimpleNamespace(about="", participants_count=5))
        backup = _backup(AsyncMock(return_value=answer))
        fields = _run(backup._fetch_chat_description(MagicMock(spec=Channel)))
        self.assertEqual(fields, {"description": None, "participants_count": 5})

    def test_an_unknown_entity_is_not_asked_at_all(self):
        backup = _backup(AsyncMock())
        self.assertEqual(_run(backup._fetch_chat_description(object())), {})
        backup.client.assert_not_called()

    def test_a_failed_request_keeps_the_stored_description_and_logs_only_the_error_type(self):
        class ChannelPrivateError(Exception):
            def __str__(self):
                return "The channel Secret Lair (-1001234) is private"

        backup = _backup(AsyncMock(side_effect=ChannelPrivateError()))
        with self.assertLogs("src.telegram_backup", level="WARNING") as captured:
            fields = _run(backup._fetch_chat_description(MagicMock(spec=Channel)))
        self.assertEqual(fields, {})
        joined = "\n".join(captured.output)
        self.assertIn("ChannelPrivateError", joined)
        self.assertNotIn("Secret Lair", joined)
        self.assertNotIn("1001234", joined)


class TestBackupDialogWiring(unittest.TestCase):
    """The fetch is merged into the chat row exactly when the flag is on."""

    def _backup(self, flag: bool):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock()
        backup.config.download_chat_description = flag
        backup.config.skip_media_chat_ids = set()
        backup.config.skip_media_delete_existing = False
        backup.config.sync_deletions_edits = False
        backup.config.reaction_resweep_days = 0
        backup.config.batch_size = 1
        backup.config.checkpoint_interval = 1
        backup.account_id = 1
        backup.client = MagicMock()
        backup.db = MagicMock()
        backup.db.upsert_chat = AsyncMock()
        backup._cleaned_media_chats = set()
        backup._get_marked_id = MagicMock(return_value=-100123)
        backup._extract_chat_data = MagicMock(return_value={"id": -100123, "title": "t"})
        backup._fetch_chat_description = AsyncMock(return_value={"description": "fresh", "participants_count": 9})
        return backup

    def test_on_the_fetched_fields_reach_upsert_chat(self):
        backup = self._backup(True)
        try:
            _run(backup._backup_dialog(MagicMock()))
        except Exception:
            pass  # everything after the chat row is unmocked; the row write is what this test is about
        backup._fetch_chat_description.assert_awaited_once()
        chat_data = backup.db.upsert_chat.await_args.args[0]
        self.assertEqual(chat_data["description"], "fresh")
        self.assertEqual(chat_data["participants_count"], 9)

    def test_off_no_request_is_made_and_no_key_is_written(self):
        backup = self._backup(False)
        try:
            _run(backup._backup_dialog(MagicMock()))
        except Exception:
            pass
        backup._fetch_chat_description.assert_not_awaited()
        chat_data = backup.db.upsert_chat.await_args.args[0]
        self.assertNotIn("description", chat_data)


if __name__ == "__main__":
    unittest.main()
