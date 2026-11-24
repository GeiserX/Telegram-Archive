"""The rotating full deletion and edit pass, SYNC_DELETIONS_EDITS_FULL_PER_RUN (#641).

With SYNC_DELETIONS_EDITS_DAYS above 0 a run re-checks only recent messages.
The rotation re-checks older messages a budgeted slice per run, over the chats
whose last full pass is oldest, resuming each chat under a saved cursor.

The rotation runs on ``real_adapter``, so its queries and the sync_status
bookkeeping are compiled and run by SQLite and PostgreSQL. Telegram is a small
fake that answers ``get_messages`` by id and records every request.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from telegram_archive import telegram_backup
from telegram_archive.config import Config, sync_deletions_edits_full_budget
from telegram_archive.message_utils import utcnow_naive
from telegram_archive.telegram_backup import TelegramBackup

# Invented chats. Obviously fake ids, no titles.
CHAT_A = 641001
CHAT_B = 641002
CHAT_C = 641003
DAYS = 30.0


class FakeTelegram:
    """Answers ``get_messages(entity, ids=...)`` in order, None for a gone id."""

    def __init__(self, gone=(), fail_on_call=None, error=None):
        self.calls: list[tuple[object, list[int]]] = []
        self.gone = set(gone)
        self.fail_on_call = fail_on_call
        self.error = error

    async def get_messages(self, entity, ids):
        self.calls.append((entity, list(ids)))
        if self.fail_on_call == len(self.calls):
            raise self.error
        return [None if i in self.gone else SimpleNamespace(id=i, edit_date=None, reactions=None) for i in ids]

    def checked(self, entity=None) -> list[int]:
        return [i for e, ids in self.calls if entity is None or e == entity for i in ids]


def _backup(adapter, telegram, *, budget, account_id=1, days=DAYS, deletion_mode="soft"):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = account_id
    backup.config = SimpleNamespace(
        sync_deletions_edits=True,
        sync_deletions_edits_days=days,
        sync_deletions_edits_full_per_run=budget,
        deletion_mode=deletion_mode,
        log_chat_titles=False,
    )
    backup.db = adapter
    backup.client = telegram
    backup._keep_snapshots = AsyncMock()
    return backup


async def _seed(adapter, chat_id, *, older=(), recent=(), account_id=1):
    """A private chat with ``older`` ids posted 100 days ago and ``recent`` ids 1 day ago."""
    await adapter.upsert_chat({"id": chat_id, "type": "private", "first_name": "Sample"}, account_id=account_id)
    now = utcnow_naive()
    rows = [{"id": i, "chat_id": chat_id, "date": now - timedelta(days=100), "text": "t"} for i in older]
    rows += [{"id": i, "chat_id": chat_id, "date": now - timedelta(days=1), "text": "t"} for i in recent]
    if rows:
        await adapter.insert_messages_batch(rows, account_id=account_id)


async def _state(adapter, chat_id, account_id=1):
    return (await adapter.get_full_sync_rotation(account_id=account_id)).get(chat_id, (None, None))


@pytest.mark.asyncio
async def test_the_slice_reads_only_older_messages_newest_first(real_adapter):
    """The window is the per-chat pass's job; the rotation reads below it, highest id first."""
    await _seed(real_adapter, CHAT_A, older=range(1, 51), recent=range(51, 61))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=1000)._rotate_full_sync({CHAT_A: "entity-A"})

    assert telegram.checked() == list(range(50, 0, -1))
    assert [len(ids) for _, ids in telegram.calls] == [50]


@pytest.mark.asyncio
async def test_a_large_chat_resumes_from_its_cursor_then_wraps(real_adapter):
    """250 older messages, 120 per run: two slices, a completing slice, then the top again."""
    await _seed(real_adapter, CHAT_A, older=range(1, 251))

    run1 = FakeTelegram()
    await _backup(real_adapter, run1, budget=120)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run1.checked() == list(range(250, 130, -1))
    assert [len(ids) for _, ids in run1.calls] == [100, 20]
    assert await _state(real_adapter, CHAT_A) == (131, None)

    run2 = FakeTelegram()
    await _backup(real_adapter, run2, budget=120)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run2.checked() == list(range(130, 10, -1))
    assert await _state(real_adapter, CHAT_A) == (11, None)

    run3 = FakeTelegram()
    await _backup(real_adapter, run3, budget=120)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run3.checked() == list(range(10, 0, -1))
    cursor, completed_at = await _state(real_adapter, CHAT_A)
    assert cursor is None
    assert completed_at is not None

    run4 = FakeTelegram()
    await _backup(real_adapter, run4, budget=120)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run4.checked() == list(range(250, 130, -1))


@pytest.mark.asyncio
async def test_a_slice_that_ends_exactly_on_the_oldest_completes_next_run_for_free(real_adapter):
    await _seed(real_adapter, CHAT_A, older=range(1, 101))

    await _backup(real_adapter, FakeTelegram(), budget=100)._rotate_full_sync({CHAT_A: "entity-A"})
    assert await _state(real_adapter, CHAT_A) == (1, None)

    run2 = FakeTelegram()
    await _backup(real_adapter, run2, budget=100)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run2.calls == []
    cursor, completed_at = await _state(real_adapter, CHAT_A)
    assert cursor is None and completed_at is not None


@pytest.mark.asyncio
async def test_the_budget_is_shared_across_chats(real_adapter):
    """80 + 80 older messages and a budget of 120: the first chat finishes, the second gets 40."""
    await _seed(real_adapter, CHAT_A, older=range(1, 81))
    await _seed(real_adapter, CHAT_B, older=range(1, 81))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=200)._rotate_full_sync({CHAT_A: "entity-A", CHAT_B: "entity-B"})

    assert len(telegram.checked("entity-A")) == 80
    assert telegram.checked("entity-B") == list(range(80, 0, -1))
    assert (await _state(real_adapter, CHAT_A))[1] is not None
    assert (await _state(real_adapter, CHAT_B))[1] is not None


@pytest.mark.asyncio
async def test_each_request_costs_a_full_batch_of_budget(real_adapter):
    """80 + 80 older messages and a budget of 120: the first chat's one request costs 100, the second gets 20."""
    await _seed(real_adapter, CHAT_A, older=range(1, 81))
    await _seed(real_adapter, CHAT_B, older=range(1, 81))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=120)._rotate_full_sync({CHAT_A: "entity-A", CHAT_B: "entity-B"})

    assert len(telegram.checked("entity-A")) == 80
    assert telegram.checked("entity-B") == list(range(80, 60, -1))
    assert await _state(real_adapter, CHAT_B) == (61, None)


@pytest.mark.asyncio
async def test_many_small_chats_make_no_more_requests_than_the_budget_allows(real_adapter):
    """Telegram limits requests, not ids: 30 chats of 3 older messages and a budget of 1000 make 10 requests."""
    chats = {CHAT_A + n: f"entity-{n}" for n in range(30)}
    for chat_id in chats:
        await _seed(real_adapter, chat_id, older=range(1, 4))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=1000)._rotate_full_sync(chats)

    assert len(telegram.calls) == 10
    state = await real_adapter.get_full_sync_rotation(account_id=1)
    assert sum(1 for chat_id in chats if state.get(chat_id, (None, None))[1] is not None) == 10


@pytest.mark.asyncio
async def test_the_chat_whose_last_pass_is_oldest_goes_first_never_completed_before_all(real_adapter):
    for chat_id in (CHAT_A, CHAT_B, CHAT_C):
        await _seed(real_adapter, chat_id, older=range(1, 11))
    now = utcnow_naive()
    await real_adapter.save_full_sync_progress(CHAT_A, account_id=1, cursor=None, completed_at=now - timedelta(days=3))
    await real_adapter.save_full_sync_progress(CHAT_C, account_id=1, cursor=None, completed_at=now - timedelta(days=9))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=1000)._rotate_full_sync(
        {CHAT_A: "entity-A", CHAT_B: "entity-B", CHAT_C: "entity-C"}
    )

    assert [entity for entity, _ in telegram.calls] == ["entity-B", "entity-C", "entity-A"]


@pytest.mark.asyncio
async def test_a_chat_in_the_middle_of_a_pass_goes_before_one_that_has_not_started(real_adapter):
    """A chat added to the selection, with a lower id, must not push a half-checked chat back."""
    await _seed(real_adapter, CHAT_A, older=range(1, 301))
    await _seed(real_adapter, CHAT_B, older=range(1, 301))
    await real_adapter.save_full_sync_progress(CHAT_B, account_id=1, cursor=201)
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=100)._rotate_full_sync({CHAT_A: "entity-A", CHAT_B: "entity-B"})

    assert telegram.checked() == list(range(200, 100, -1))
    assert [entity for entity, _ in telegram.calls] == ["entity-B"]


@pytest.mark.asyncio
async def test_a_mid_pass_cursor_survives_while_the_completion_time_stays(real_adapter):
    """Saving a cursor must not clear the last completion, or the chat would jump the queue."""
    await _seed(real_adapter, CHAT_A, older=range(1, 301))
    earlier = utcnow_naive() - timedelta(days=5)
    await real_adapter.save_full_sync_progress(CHAT_A, account_id=1, cursor=None, completed_at=earlier)

    await _backup(real_adapter, FakeTelegram(), budget=100)._rotate_full_sync({CHAT_A: "entity-A"})

    cursor, completed_at = await _state(real_adapter, CHAT_A)
    assert cursor == 201
    assert completed_at == earlier.replace(microsecond=completed_at.microsecond)


@pytest.mark.asyncio
async def test_a_chat_with_no_older_messages_completes_without_a_request(real_adapter):
    await _seed(real_adapter, CHAT_A, recent=range(1, 6))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=50)._rotate_full_sync({CHAT_A: "entity-A"})

    assert telegram.calls == []
    assert (await _state(real_adapter, CHAT_A))[1] is not None


@pytest.mark.asyncio
async def test_only_the_chats_handed_in_are_touched(real_adapter):
    """A chat outside this run's selection keeps its state and costs nothing."""
    await _seed(real_adapter, CHAT_A, older=range(1, 11))
    await _seed(real_adapter, CHAT_B, older=range(1, 11))
    telegram = FakeTelegram()

    await _backup(real_adapter, telegram, budget=1000)._rotate_full_sync({CHAT_A: "entity-A"})

    assert {entity for entity, _ in telegram.calls} == {"entity-A"}
    assert await _state(real_adapter, CHAT_B) == (None, None)


@pytest.mark.asyncio
async def test_a_flood_wait_keeps_the_cursor_and_ends_the_runs_rotation(real_adapter):
    await _seed(real_adapter, CHAT_A, older=range(1, 251))
    await _seed(real_adapter, CHAT_B, older=range(1, 11))
    flood = FloodWaitError(request=None, capture=telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1)
    telegram = FakeTelegram(fail_on_call=2, error=flood)

    await _backup(real_adapter, telegram, budget=1000)._rotate_full_sync({CHAT_A: "entity-A", CHAT_B: "entity-B"})

    assert await _state(real_adapter, CHAT_A) == (151, None)
    assert telegram.checked("entity-B") == []
    assert await _state(real_adapter, CHAT_B) == (None, None)

    resumed = FakeTelegram()
    await _backup(real_adapter, resumed, budget=1000)._rotate_full_sync({CHAT_A: "entity-A"})
    assert resumed.checked("entity-A")[0] == 150


class PoisonTelegram(FakeTelegram):
    """Fails every request that asks for ``poison``, the way a bug on one odd message would."""

    def __init__(self, poison):
        super().__init__()
        self.poison = poison

    async def get_messages(self, entity, ids):
        if self.poison in ids:
            self.calls.append((entity, list(ids)))
            raise RuntimeError("boom")
        return await super().get_messages(entity, ids)


@pytest.mark.asyncio
async def test_another_error_skips_the_batch_and_the_chat_carries_on(real_adapter):
    """Like the window pass: the failed batch is skipped, the rest of the chat is still checked."""
    await _seed(real_adapter, CHAT_A, older=range(1, 251))
    await _seed(real_adapter, CHAT_B, older=range(1, 11))
    telegram = FakeTelegram(fail_on_call=2, error=RuntimeError("boom"))

    await _backup(real_adapter, telegram, budget=1000)._rotate_full_sync({CHAT_A: "entity-A", CHAT_B: "entity-B"})

    assert telegram.checked("entity-A") == list(range(250, 0, -1))
    assert (await _state(real_adapter, CHAT_A))[1] is not None
    assert len(telegram.checked("entity-B")) == 10
    assert (await _state(real_adapter, CHAT_B))[1] is not None


@pytest.mark.asyncio
async def test_a_batch_that_always_fails_does_not_stall_the_chat_across_runs(real_adapter, caplog):
    """A batch that fails on every run is passed over, so the pass reaches the chat's oldest message."""
    await _seed(real_adapter, CHAT_A, older=range(1, 501))

    run1 = PoisonTelegram(poison=350)
    await _backup(real_adapter, run1, budget=200)._rotate_full_sync({CHAT_A: "entity-A"})
    assert await _state(real_adapter, CHAT_A) == (301, None)

    run2 = PoisonTelegram(poison=350)
    await _backup(real_adapter, run2, budget=200)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run2.checked()[0] == 300
    assert await _state(real_adapter, CHAT_A) == (101, None)

    run3 = PoisonTelegram(poison=350)
    with caplog.at_level("INFO", logger="telegram_archive.telegram_backup"):
        await _backup(real_adapter, run3, budget=200)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run3.checked() == list(range(100, 0, -1))
    assert (await _state(real_adapter, CHAT_A))[1] is not None

    # The next pass starts at the top and asks for the failed batch again.
    run4 = PoisonTelegram(poison=350)
    await _backup(real_adapter, run4, budget=200)._rotate_full_sync({CHAT_A: "entity-A"})
    assert 350 in run4.checked()


@pytest.mark.asyncio
async def test_the_summary_counts_skipped_messages(real_adapter, caplog):
    await _seed(real_adapter, CHAT_A, older=range(1, 201))

    with caplog.at_level("INFO", logger="telegram_archive.telegram_backup"):
        await _backup(real_adapter, PoisonTelegram(poison=150), budget=1000)._rotate_full_sync({CHAT_A: "entity-A"})

    assert "re-checked 100 older messages in 1 chat(s)" in caplog.text
    assert "100 skipped after errors, re-checked next pass" in caplog.text


@pytest.mark.asyncio
async def test_a_database_error_in_one_chat_keeps_its_progress_and_the_next_chat_runs(real_adapter):
    await _seed(real_adapter, CHAT_A, older=range(1, 251))
    await _seed(real_adapter, CHAT_B, older=range(1, 11))
    await real_adapter.save_full_sync_progress(CHAT_A, account_id=1, cursor=201)
    backup = _backup(real_adapter, FakeTelegram(), budget=1000)
    real_read = real_adapter.get_messages_sync_data

    async def failing_read(chat_id, **kwargs):
        if chat_id == CHAT_A:
            raise RuntimeError("database gone for this chat")
        return await real_read(chat_id, **kwargs)

    with patch.object(real_adapter, "get_messages_sync_data", side_effect=failing_read):
        await backup._rotate_full_sync({CHAT_A: "entity-A", CHAT_B: "entity-B"})

    assert await _state(real_adapter, CHAT_A) == (201, None)
    assert len(backup.client.checked("entity-B")) == 10
    assert (await _state(real_adapter, CHAT_B))[1] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion_mode", ["soft", "hard"])
async def test_a_message_deleted_at_the_cursor_does_not_stall_the_pass(real_adapter, deletion_mode):
    """The cursor is an id bound, not a row: the message under it may be gone next run."""
    await _seed(real_adapter, CHAT_A, older=range(1, 201))
    telegram = FakeTelegram(gone={101})

    await _backup(real_adapter, telegram, budget=100, deletion_mode=deletion_mode)._rotate_full_sync(
        {CHAT_A: "entity-A"}
    )
    assert await _state(real_adapter, CHAT_A) == (101, None)
    remaining = await real_adapter.get_messages_sync_data(CHAT_A, account_id=1)
    assert 101 not in remaining

    run2 = FakeTelegram()
    await _backup(real_adapter, run2, budget=150)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run2.checked() == list(range(100, 0, -1))
    assert (await _state(real_adapter, CHAT_A))[1] is not None


@pytest.mark.asyncio
async def test_each_account_keeps_its_own_rotation(real_adapter):
    await _seed(real_adapter, CHAT_A, older=range(1, 201), account_id=1)
    await _seed(real_adapter, CHAT_A, older=range(1, 201), account_id=2)

    await _backup(real_adapter, FakeTelegram(), budget=100, account_id=1)._rotate_full_sync({CHAT_A: "entity-A"})

    assert await _state(real_adapter, CHAT_A, account_id=1) == (101, None)
    assert await _state(real_adapter, CHAT_A, account_id=2) == (None, None)
    run = FakeTelegram()
    await _backup(real_adapter, run, budget=100, account_id=2)._rotate_full_sync({CHAT_A: "entity-A"})
    assert run.checked()[0] == 200


@pytest.mark.asyncio
async def test_a_chat_with_no_sync_status_row_gets_one(real_adapter):
    """An imported chat may have messages and no sync_status row yet."""
    await _seed(real_adapter, CHAT_A, older=range(1, 151))

    await _backup(real_adapter, FakeTelegram(), budget=100)._rotate_full_sync({CHAT_A: "entity-A"})

    assert await _state(real_adapter, CHAT_A) == (51, None)
    assert await real_adapter.get_last_message_id(CHAT_A, account_id=1) == 0


@pytest.mark.asyncio
async def test_the_summary_counts_chats_without_naming_them(real_adapter, caplog):
    await _seed(real_adapter, CHAT_A, older=range(1, 11))
    await _seed(real_adapter, CHAT_B, older=range(1, 201))

    with caplog.at_level("INFO", logger="telegram_archive.telegram_backup"):
        await _backup(real_adapter, FakeTelegram(gone={5}), budget=200)._rotate_full_sync(
            {CHAT_A: "entity-A", CHAT_B: "entity-B"}
        )

    assert (
        "Older-message rotation: re-checked 110 older messages in 2 chat(s), 1 deleted, 0 updated; "
        "1 chat(s) finished a pass, 1 of 2 have not finished one yet"
    ) in caplog.text
    assert str(CHAT_A) not in caplog.text and str(CHAT_B) not in caplog.text


# --- Off means today's behaviour --------------------------------------------------


class TestTheBudget(unittest.TestCase):
    def test_it_is_zero_unless_sync_is_on_with_a_window(self):
        on = SimpleNamespace(
            sync_deletions_edits=True, sync_deletions_edits_days=14.0, sync_deletions_edits_full_per_run=500
        )
        self.assertEqual(sync_deletions_edits_full_budget(on), 500)
        self.assertEqual(
            sync_deletions_edits_full_budget(SimpleNamespace(**{**vars(on), "sync_deletions_edits": False})), 0
        )
        self.assertEqual(
            sync_deletions_edits_full_budget(SimpleNamespace(**{**vars(on), "sync_deletions_edits_days": 0.0})), 0
        )
        self.assertEqual(
            sync_deletions_edits_full_budget(SimpleNamespace(**{**vars(on), "sync_deletions_edits_full_per_run": 0})), 0
        )

    def test_a_mock_config_reads_as_off(self):
        self.assertEqual(sync_deletions_edits_full_budget(MagicMock()), 0)
        config = MagicMock()
        config.sync_deletions_edits = True
        config.sync_deletions_edits_days = 14.0
        self.assertEqual(sync_deletions_edits_full_budget(config), 0)

    def test_off_makes_no_database_call(self):
        for budget, days, sync in ((0, 14.0, True), (500, 0.0, True), (500, 14.0, False)):
            with self.subTest(budget=budget, days=days, sync=sync):
                backup = TelegramBackup.__new__(TelegramBackup)
                backup.account_id = 1
                backup.config = SimpleNamespace(
                    sync_deletions_edits=sync,
                    sync_deletions_edits_days=days,
                    sync_deletions_edits_full_per_run=budget,
                )
                backup.db = AsyncMock()
                backup.client = AsyncMock()
                asyncio.run(backup._rotate_full_sync({CHAT_A: "entity-A"}))
                self.assertEqual(backup.db.mock_calls, [])
                self.assertEqual(backup.client.mock_calls, [])


class TestTheSetting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **env):
        base = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, **env}
        with patch.dict(os.environ, base, clear=True):
            return Config()

    def test_unset_is_off(self):
        self.assertEqual(self._config().sync_deletions_edits_full_per_run, 0)

    def test_parses_a_count(self):
        self.assertEqual(
            self._config(SYNC_DELETIONS_EDITS_FULL_PER_RUN="20000").sync_deletions_edits_full_per_run, 20000
        )

    def test_negative_floored_at_zero(self):
        self.assertEqual(self._config(SYNC_DELETIONS_EDITS_FULL_PER_RUN="-5").sync_deletions_edits_full_per_run, 0)

    def test_non_integer_stops_startup(self):
        with self.assertRaisesRegex(ValueError, "SYNC_DELETIONS_EDITS_FULL_PER_RUN must be an integer"):
            self._config(SYNC_DELETIONS_EDITS_FULL_PER_RUN="lots")

    def test_banner_names_the_rotation_only_when_it_runs(self):
        config = self._config(
            SYNC_DELETIONS_EDITS="true", SYNC_DELETIONS_EDITS_DAYS="14", SYNC_DELETIONS_EDITS_FULL_PER_RUN="20000"
        )
        with self.assertLogs("telegram_archive.config", level="WARNING") as logs:
            config.log_summary()
        self.assertIn("SYNC_DELETIONS_EDITS_FULL_PER_RUN=20000", "\n".join(logs.output))

        config = self._config(SYNC_DELETIONS_EDITS="true", SYNC_DELETIONS_EDITS_FULL_PER_RUN="20000")
        with self.assertLogs("telegram_archive.config", level="WARNING") as logs:
            config.log_summary()
        self.assertNotIn("SYNC_DELETIONS_EDITS_FULL_PER_RUN", "\n".join(logs.output))


# --- Wiring in backup_all ----------------------------------------------------------


class TestBackupAllHandsInOnlyBackedUpChats(unittest.TestCase):
    """The rotation sees exactly the chats this run backed up, regular and archived."""

    def setUp(self):
        from test_telegram_backup_extended import _make_backup

        self.backup = _make_backup()
        config = self.backup.config
        config.whitelist_mode = False
        config.priority_chat_ids = set()
        config.verify_media = False
        config.media_path = tempfile.mkdtemp()
        for name in (
            "global_include_ids",
            "private_include_ids",
            "groups_include_ids",
            "channels_include_ids",
            "global_exclude_ids",
            "private_exclude_ids",
            "groups_exclude_ids",
            "channels_exclude_ids",
        ):
            setattr(config, name, set())
        config.should_backup_chat = MagicMock(return_value=True)
        config.sync_deletions_edits = True
        config.sync_deletions_edits_days = 14.0
        config.sync_deletions_edits_full_per_run = 500
        self.backup.client.start = AsyncMock()
        self.backup.client.get_me = AsyncMock(return_value=MagicMock(id=123))
        self.backup.db.calculate_and_store_statistics = AsyncMock(
            return_value={"chats": 1, "messages": 1, "media_files": 0, "total_size_mb": 0}
        )
        self.backup._backup_folders = AsyncMock()
        self.backup._backup_forum_topics = AsyncMock()
        self.backup._get_marked_id = MagicMock(side_effect=lambda e: e._test_id)
        self.backup._rotate_full_sync = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.backup.config.media_path, ignore_errors=True)

    def _dialog(self, test_id):
        entity = MagicMock(spec=User)
        entity._test_id = test_id
        entity.id = test_id
        entity.bot = False
        return MagicMock(entity=entity, date=datetime(2024, 6, 1))

    def test_a_failed_chat_is_left_out_and_an_archived_one_is_in(self):
        from telethon.errors import ChannelPrivateError

        ok, forbidden, archived = self._dialog(CHAT_A), self._dialog(CHAT_B), self._dialog(CHAT_C)

        async def backup_dialog(dialog, is_archived=False):
            if dialog is forbidden:
                raise ChannelPrivateError(request=None)
            return 0

        self.backup._backup_dialog = AsyncMock(side_effect=backup_dialog)
        self.backup._get_dialogs = AsyncMock(side_effect=[[ok, forbidden], [archived]])

        asyncio.run(self.backup.backup_all())

        self.backup._rotate_full_sync.assert_awaited_once()
        entities = self.backup._rotate_full_sync.await_args.args[0]
        self.assertEqual(entities, {CHAT_A: ok.entity, CHAT_C: archived.entity})

    def test_off_never_calls_the_rotation(self):
        self.backup.config.sync_deletions_edits_full_per_run = 0
        self.backup._backup_dialog = AsyncMock(return_value=0)
        self.backup._get_dialogs = AsyncMock(side_effect=[[self._dialog(CHAT_A)], []])

        asyncio.run(self.backup.backup_all())

        self.backup._rotate_full_sync.assert_not_awaited()

    def test_a_rotation_failure_does_not_abort_the_run(self):
        self.backup._rotate_full_sync = AsyncMock(side_effect=RuntimeError("boom"))
        self.backup._backup_dialog = AsyncMock(return_value=0)
        self.backup._get_dialogs = AsyncMock(side_effect=[[self._dialog(CHAT_A)], []])

        asyncio.run(self.backup.backup_all())

        self.backup._backup_folders.assert_awaited_once()
