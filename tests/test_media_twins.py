"""Leftover twin media rows: the retry drain's phantom download, and voice notes shown twice.

Before #426 a media row's id was minted from its type, so a message classified
one way and later another got a SECOND row instead of a corrected one. #426
stopped new twins; these tests cover what the old ones still broke.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.models import Media
from src.telegram_backup import TelegramBackup

PENDING_ROW = "pending-row"
CANONICAL_ROW = "canonical-row"
CHAT = -1001555000001


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _drain_backup(process_result):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = MagicMock()
    backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
    backup.config.max_media_download_attempts = 5
    backup.config.skip_media_chat_ids = set()
    backup.config.download_youtube_videos = False
    backup.db = AsyncMock()
    backup.db.get_pending_media_downloads = AsyncMock(
        return_value=[{"id": PENDING_ROW, "message_id": 10, "chat_id": -100, "type": "document"}]
    )
    backup.db.count_capped_media_downloads = AsyncMock(return_value=0)
    backup.db.increment_media_download_attempts = AsyncMock()
    backup.db.insert_media = AsyncMock()
    backup.db.delete_media_records = AsyncMock(return_value=1)
    message = MagicMock()
    message.id = 10
    message.media = MagicMock()
    backup.client = MagicMock()
    backup.client.get_messages = AsyncMock(return_value=[message])
    backup._process_media = AsyncMock(return_value=process_result)
    return backup


class TestRetryDrainLeftoverTwin(unittest.TestCase):
    def test_a_leftover_twin_is_removed_not_reported_as_a_download(self):
        """The production shape: the message's file is held by its canonical
        row, so _process_media hands back THAT row. The pending twin used to be
        counted as downloaded, keep downloaded=0 and 0 attempts, and be
        re-requested from Telegram on every run."""
        canonical = {"id": CANONICAL_ROW, "downloaded": True}
        backup = _drain_backup(canonical)

        _run(backup._retry_pending_media_downloads())

        backup.db.delete_media_records.assert_awaited_once_with([PENDING_ROW], account_id=1)
        backup.db.increment_media_download_attempts.assert_not_awaited()
        backup.db.insert_media.assert_awaited_once_with(canonical, account_id=1)

    def test_a_real_download_keeps_its_row(self):
        backup = _drain_backup({"id": PENDING_ROW, "downloaded": True})

        _run(backup._retry_pending_media_downloads())

        backup.db.delete_media_records.assert_not_awaited()
        backup.db.insert_media.assert_awaited_once()

    def test_the_summary_line_no_longer_claims_a_download(self):
        backup = _drain_backup({"id": CANONICAL_ROW, "downloaded": True})

        with self.assertLogs("src.telegram_backup", level="INFO") as logs:
            _run(backup._retry_pending_media_downloads())

        summary = [line for line in logs.output if "Pending media retry:" in line]
        self.assertEqual(len(summary), 1)
        self.assertIn("0 downloaded", summary[0])
        self.assertIn("1 duplicate row(s) removed", summary[0])


async def _seed_messages(adapter, account_id, message_ids):
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "fixture chat"}, account_id=account_id)
    for message_id in message_ids:
        await adapter.insert_message(
            {"id": message_id, "chat_id": CHAT, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
            account_id=account_id,
        )


async def _add_media(adapter, *, message_id, media_type, file_path, downloaded=1, account_id=1):
    """Straight through the ORM, so nothing reconciles the twin away before the test sees it."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            Media(
                account_id=account_id,
                id=f"{CHAT}_{message_id}_{media_type}",
                message_id=message_id,
                chat_id=CHAT,
                type=media_type,
                file_path=file_path,
                downloaded=downloaded,
            )
        )
        await session.commit()


async def _types_for(adapter, message_id, *, account_id=1):
    found = []
    for media_type in ("audio", "voice"):
        if await adapter.get_media_for_message(CHAT, message_id, media_type, account_id=account_id):
            found.append(media_type)
    return found


async def test_only_the_audio_twin_of_a_voice_note_goes(real_adapter):
    await _seed_messages(real_adapter, 1, range(1, 7))
    # 1: the production shape -- two downloaded rows naming one file
    await _add_media(real_adapter, message_id=1, media_type="audio", file_path="/m/c/n1.ogg")
    await _add_media(real_adapter, message_id=1, media_type="voice", file_path="/m/c/n1.ogg")
    # 2: two different files on one message -- not a twin
    await _add_media(real_adapter, message_id=2, media_type="audio", file_path="/m/c/song.mp3")
    await _add_media(real_adapter, message_id=2, media_type="voice", file_path="/m/c/n2.ogg")
    # 3: an ordinary music file
    await _add_media(real_adapter, message_id=3, media_type="audio", file_path="/m/c/track.mp3")
    # 4: the voice twin never downloaded, so the audio row is the copy on record
    await _add_media(real_adapter, message_id=4, media_type="audio", file_path="/m/c/n4.ogg")
    await _add_media(real_adapter, message_id=4, media_type="voice", file_path="/m/c/n4.ogg", downloaded=0)
    # 6: the SAME file as message 1's pair, on a different message with no voice row
    await _add_media(real_adapter, message_id=6, media_type="audio", file_path="/m/c/n1.ogg")

    assert await _types_for(real_adapter, 1) == ["audio", "voice"]  # the precondition really holds

    assert await real_adapter.delete_voice_note_audio_twins(account_id=1) == 1

    assert await _types_for(real_adapter, 1) == ["voice"]
    assert await _types_for(real_adapter, 2) == ["audio", "voice"]
    assert await _types_for(real_adapter, 3) == ["audio"]
    assert await _types_for(real_adapter, 4) == ["audio", "voice"]
    assert await _types_for(real_adapter, 6) == ["audio"]
    assert await real_adapter.delete_voice_note_audio_twins(account_id=1) == 0  # nothing left to find


async def test_another_accounts_twins_are_that_accounts_business(real_adapter):
    for account_id in (1, 2):
        await _seed_messages(real_adapter, account_id, [1])
        await _add_media(real_adapter, account_id=account_id, message_id=1, media_type="audio", file_path="/m/c/n1.ogg")
        await _add_media(real_adapter, account_id=account_id, message_id=1, media_type="voice", file_path="/m/c/n1.ogg")

    assert await real_adapter.delete_voice_note_audio_twins(account_id=1) == 1

    assert await _types_for(real_adapter, 1, account_id=1) == ["voice"]
    assert await _types_for(real_adapter, 1, account_id=2) == ["audio", "voice"]


async def test_the_voice_tab_shows_and_counts_the_note_once(real_adapter):
    """The two reads behind the Voice tab: its grid lists voice,audio and its
    badge adds both counts (index.html typeMap and loadMediaCounts)."""
    await _seed_messages(real_adapter, 1, [1, 2])
    await _add_media(real_adapter, message_id=1, media_type="audio", file_path="/m/c/n1.ogg")
    await _add_media(real_adapter, message_id=1, media_type="voice", file_path="/m/c/n1.ogg")
    await _add_media(real_adapter, message_id=2, media_type="voice", file_path="/m/c/n2.ogg")

    async def voice_tab():
        counts = await real_adapter.get_media_counts(CHAT, account_id=1)
        page = await real_adapter.get_media_paginated(CHAT, media_types=["voice", "audio"], account_id=1)
        return counts.get("voice", 0) + counts.get("audio", 0), len(page["items"])

    assert await voice_tab() == (3, 3)  # two voice notes, shown and counted as three
    await real_adapter.delete_voice_note_audio_twins(account_id=1)
    assert await voice_tab() == (2, 2)


def _backup_run():
    """A real backup_all over no dialogs, modelled on test_log_chat_titles._sweep_backup."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    cfg = MagicMock()
    cfg.log_chat_titles = False
    cfg.whitelist_mode = False
    cfg.chat_ids = set()
    cfg.phone = "+1234567890"
    cfg.priority_chat_ids = set()
    cfg.verify_media = False
    cfg.follow_chat_migrations = False
    cfg.should_backup_chat = MagicMock(return_value=True)
    backup.config = cfg
    db = AsyncMock()
    db.get_last_message_id = AsyncMock(return_value=0)
    db.calculate_and_store_statistics = AsyncMock(
        return_value={"chats": 0, "messages": 0, "media_files": 0, "total_size_mb": 0}
    )
    db.delete_voice_note_audio_twins = AsyncMock(return_value=0)
    backup.db = db
    client = AsyncMock()
    me = MagicMock()
    me.first_name, me.id = "T", 1
    client.get_me = AsyncMock(return_value=me)
    client.start = AsyncMock()
    backup.client = client
    backup._get_marked_id = MagicMock(return_value=-1)
    backup._get_dialogs = AsyncMock(return_value=[])
    backup._followed_migration_ids = set()
    backup._load_resweep_cycle = AsyncMock()
    backup._load_followed_migrations = AsyncMock()
    backup._finalize_resweep_cycle = AsyncMock()
    backup._reconcile_migrations = AsyncMock()
    backup._backup_folders = AsyncMock()
    backup._retry_pending_media_downloads = AsyncMock()
    backup._backup_dialog = AsyncMock(return_value=0)
    return backup


class TestEveryBackupRunRemovesTheTwins(unittest.IsolatedAsyncioTestCase):
    async def test_the_run_asks_for_this_accounts_twins(self):
        backup = _backup_run()
        await backup.backup_all()
        backup.db.delete_voice_note_audio_twins.assert_awaited_once_with(account_id=1)

    async def test_a_failed_cleanup_does_not_stop_the_backup(self):
        backup = _backup_run()
        backup.db.delete_voice_note_audio_twins = AsyncMock(side_effect=RuntimeError("db down"))
        with self.assertLogs("src.telegram_backup", level="WARNING") as logs:
            await backup.backup_all()
        backup._get_dialogs.assert_awaited()  # the run went on past the cleanup
        self.assertTrue(any("duplicate voice-note rows" in line for line in logs.output))
