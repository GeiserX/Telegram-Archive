"""backfill-details: the edit flag of rows archived before migration 034 (docs/design/open-questions.md, 3B).

Telegram moves a message's edit time when only its reactions change and flags
that edit as hidden (``edit_hide``). Rows archived before 034 have
``edit_date`` set and ``edit_hide`` NULL, so the viewer draws a pencil for a
reaction. The command reads those rows from Telegram, in the same pass and
the same requests as the payload backfill, and fills the flag with
``fill_edit_hide``. Real SQLite and PostgreSQL engines throughout.

Demo data only.
"""

import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from telethon.errors import ChannelPrivateError, FloodWaitError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_payload_backfill import CHAT_A, CHAT_B, SENT, _backup, _media, _seed, _seed_chat  # noqa: E402

import telegram_archive.telegram_backup as telegram_backup  # noqa: E402
from telegram_archive.db.models import Message, MessageVersion  # noqa: E402

EDITED = datetime(2026, 3, 1, 12, 5)
LATER = datetime(2026, 3, 2, 9, 0)


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(telegram_backup, "PAYLOAD_BACKFILL_PAUSE_SECONDS", 0)


async def _seed_edited(adapter, chat_id, message_id, *, edit_date=EDITED, edit_hide=None, is_deleted=0):
    """A message archived with an edit time and, before 034, no flag."""
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": SENT,
            "text": f"demo text {message_id}",
            "edit_date": edit_date,
            "edit_hide": edit_hide,
            "is_deleted": is_deleted,
            "raw_data": {},
        },
        account_id=1,
    )


async def _keep_version(adapter, chat_id, message_id):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            MessageVersion(
                account_id=1,
                chat_id=chat_id,
                message_id=message_id,
                text="demo earlier text",
                date=SENT,
                change_hash=f"demo-{chat_id}-{message_id}",
            )
        )
        await session.commit()


async def _row(adapter, chat_id, message_id):
    async with adapter.db_manager.async_session_factory() as session:
        return (
            await session.execute(
                select(Message.edit_date, Message.edit_hide, Message.text).where(
                    Message.account_id == 1, Message.chat_id == chat_id, Message.id == message_id
                )
            )
        ).one()


def _shows_pencil(row):
    # The viewer's shownEditDate (index.html): an edit time, unless edit_hide says not to show it.
    return bool(row.edit_date) and not row.edit_hide


def _read(message_id, *, edit_date=EDITED, edit_hide=False, media=None):
    """A message as Telethon returns it: a tz-aware UTC edit time."""
    return SimpleNamespace(
        id=message_id,
        media=media,
        date=SENT,
        edit_date=edit_date.replace(tzinfo=UTC) if edit_date else None,
        edit_hide=edit_hide,
    )


class FakeTelegram:
    """get_entity / get_messages over {(chat, message_id): message}, recording every call."""

    def __init__(self, served, *, refused=(), batch_error=None, fail_first=False):
        self.served = served
        self.refused = set(refused)
        self.batch_error = batch_error
        self.fail_first = fail_first
        self.calls = []
        self.entity_calls = []

    async def get_entity(self, chat):
        self.entity_calls.append(chat)
        if chat in self.refused:
            raise ChannelPrivateError(request=None)
        return SimpleNamespace(chat=chat)

    async def get_messages(self, entity, ids):
        self.calls.append((entity.chat, list(ids)))
        if self.batch_error is not None:
            raise self.batch_error
        if self.fail_first:
            self.fail_first = False
            raise ConnectionError("transient")
        return [self.served.get((entity.chat, mid)) for mid in ids]


def _asked(client):
    return [mid for _chat, ids in client.calls for mid in ids]


class TestWorkList:
    async def test_lists_unflagged_edits_without_a_version_in_id_order(self, real_adapter):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        await _seed_edited(real_adapter, CHAT_A, 7)
        await _seed_edited(real_adapter, CHAT_A, 3)
        await _seed_edited(real_adapter, CHAT_A, 4, edit_hide=1)  # flagged since 034
        await _seed_edited(real_adapter, CHAT_A, 5, edit_hide=0)
        await _seed_edited(real_adapter, CHAT_A, 6, edit_date=None)  # never edited
        await _seed_edited(real_adapter, CHAT_A, 8, is_deleted=1)  # Telegram no longer serves it
        await _seed_edited(real_adapter, CHAT_A, 9)
        await _keep_version(real_adapter, CHAT_A, 9)  # a real edit the archive saw
        await _seed_edited(real_adapter, CHAT_B, 1)

        assert await real_adapter.get_edit_hide_backfill_rows(CHAT_A, account_id=1) == [(3, EDITED), (7, EDITED)]
        assert await real_adapter.get_edit_hide_backfill_rows(CHAT_B, account_id=1) == [(1, EDITED)]
        assert await real_adapter.get_edit_hide_backfill_rows(CHAT_A, account_id=2) == []


class TestBackfill:
    async def test_a_reaction_loses_its_pencil_and_a_real_edit_keeps_it(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)  # reaction-bumped
        await _seed_edited(real_adapter, CHAT_A, 2)  # really edited before the archive first read it
        served = {(CHAT_A, 1): _read(1, edit_hide=True), (CHAT_A, 2): _read(2, edit_hide=False)}
        assert _shows_pencil(await _row(real_adapter, CHAT_A, 1))

        summary = await _backup(real_adapter, FakeTelegram(served), tmp_path).backfill_details(apply=True)

        hidden, shown = await _row(real_adapter, CHAT_A, 1), await _row(real_adapter, CHAT_A, 2)
        assert (hidden.edit_hide, shown.edit_hide) == (1, 0)
        assert not _shows_pencil(hidden)
        assert _shows_pencil(shown)
        # The flag only: the edit time and the text stay as they were.
        assert (hidden.edit_date, hidden.text) == (EDITED, "demo text 1")
        assert summary["edits"] == {"hidden": 1, "shown": 1, "date_changed": 0, "already_filled": 0, "not_served": 0}
        assert summary["chats_scanned"] == 1
        assert await real_adapter.get_unflagged_edit_ids(CHAT_A, account_id=1) == set()

    async def test_a_row_with_a_kept_version_or_a_flag_is_not_read(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)
        await _keep_version(real_adapter, CHAT_A, 1)
        await _seed_edited(real_adapter, CHAT_A, 2, edit_hide=0)
        await _seed_edited(real_adapter, CHAT_A, 3)
        client = FakeTelegram({(CHAT_A, mid): _read(mid, edit_hide=True) for mid in (1, 2, 3)})

        await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert _asked(client) == [3]
        assert (await _row(real_adapter, CHAT_A, 1)).edit_hide is None

    async def test_a_dry_run_counts_and_writes_nothing(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)
        await _seed_edited(real_adapter, CHAT_A, 2)
        served = {(CHAT_A, 1): _read(1, edit_hide=True), (CHAT_A, 2): _read(2)}

        summary = await _backup(real_adapter, FakeTelegram(served), tmp_path).backfill_details()

        assert (summary["edits"]["hidden"], summary["edits"]["shown"]) == (1, 1)
        for mid in (1, 2):
            assert (await _row(real_adapter, CHAT_A, mid)).edit_hide is None

    async def test_a_second_run_fills_nothing_and_asks_telegram_nothing(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)
        client = FakeTelegram({(CHAT_A, 1): _read(1, edit_hide=True)})
        backup = _backup(real_adapter, client, tmp_path)

        first = await backup.backfill_details(apply=True)
        second = await backup.backfill_details(apply=True)

        assert first["edits"]["hidden"] == 1
        assert second["edits"] == dict.fromkeys(first["edits"], 0)
        assert second["chats_scanned"] == 0
        assert len(client.calls) == 1
        assert client.entity_calls == [CHAT_A]

    async def test_a_later_edit_time_on_telegram_is_not_written(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)
        # A newer edit since the archive read it: its flag is not the stored edit's flag.
        client = FakeTelegram({(CHAT_A, 1): _read(1, edit_date=LATER, edit_hide=True)})

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        row = await _row(real_adapter, CHAT_A, 1)
        assert (row.edit_date, row.edit_hide, row.text) == (EDITED, None, "demo text 1")
        assert summary["edits"]["date_changed"] == 1
        assert summary["edits"]["hidden"] == 0

    async def test_a_message_telegram_no_longer_returns_is_counted(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)

        summary = await _backup(real_adapter, FakeTelegram({}), tmp_path).backfill_details(apply=True)

        assert summary["edits"]["not_served"] == 1
        assert (await _row(real_adapter, CHAT_A, 1)).edit_hide is None

    async def test_a_flag_another_writer_filled_first_is_left_alone(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)
        listed = await real_adapter.get_edit_hide_backfill_rows(CHAT_A, account_id=1)
        await real_adapter.fill_edit_hide(CHAT_A, 1, EDITED, 0, account_id=1)  # the sync got there first
        backup = _backup(real_adapter, FakeTelegram({(CHAT_A, 1): _read(1, edit_hide=True)}), tmp_path)

        with patch.object(real_adapter, "get_edit_hide_backfill_rows", AsyncMock(return_value=listed)):
            summary = await backup.backfill_details(apply=True)

        assert summary["edits"]["already_filled"] == 1
        assert (await _row(real_adapter, CHAT_A, 1)).edit_hide == 0

    async def test_a_refused_chat_is_counted_and_the_next_chat_still_runs(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        await _seed_edited(real_adapter, CHAT_A, 1)
        await _seed_edited(real_adapter, CHAT_A, 2)
        await _seed_edited(real_adapter, CHAT_B, 1)
        client = FakeTelegram({(CHAT_B, 1): _read(1, edit_hide=True)}, refused={CHAT_A})

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert summary["chats_unavailable"] == 1
        assert summary["edits"]["not_served"] == 2
        assert summary["edits"]["hidden"] == 1
        assert (await _row(real_adapter, CHAT_B, 1)).edit_hide == 1
        assert (await _row(real_adapter, CHAT_A, 1)).edit_hide is None

    async def test_a_transient_failure_leaves_the_rows_for_the_next_run(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed_edited(real_adapter, CHAT_A, 1)
        client = FakeTelegram({(CHAT_A, 1): _read(1, edit_hide=True)}, fail_first=True)
        backup = _backup(real_adapter, client, tmp_path)

        with patch.object(telegram_backup, "call_with_flood_retry", lambda fn, *a, **k: fn(*a, **k)):
            first = await backup.backfill_details(apply=True)
            assert (await _row(real_adapter, CHAT_A, 1)).edit_hide is None
            second = await backup.backfill_details(apply=True)

        assert first["errors"] == 1
        assert first["edits"]["not_served"] == 0
        assert second["edits"]["hidden"] == 1
        assert (await _row(real_adapter, CHAT_A, 1)).edit_hide == 1

    async def test_a_flood_wait_too_long_to_sleep_out_stops_the_run(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        # Chats run in id order, so CHAT_B (the lower id) comes first.
        for mid in range(1, 151):
            await _seed_edited(real_adapter, CHAT_B, mid)
        await _seed_edited(real_adapter, CHAT_A, 1)
        flood = FloodWaitError(request=None, capture=telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1)
        client = FakeTelegram({}, batch_error=flood)

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert summary["flood_wait_seconds"] == telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1
        # One refused call, then nothing more: not the second batch, not the next chat.
        assert client.entity_calls == [CHAT_B]
        assert len(client.calls) == 1
        assert summary["edits"] == {"hidden": 0, "shown": 0, "date_changed": 0, "already_filled": 0, "not_served": 0}
        for chat, mid in ((CHAT_B, 1), (CHAT_B, 150), (CHAT_A, 1)):
            assert (await _row(real_adapter, chat, mid)).edit_hide is None

    async def test_payload_rows_and_edit_rows_share_requests(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        served = {}
        # 1 to 60 miss a location payload; 41 to 140 have an unflagged edit; 41 to 60 are on both lists.
        for mid in range(1, 141):
            if mid <= 60:
                await _seed(real_adapter, CHAT_A, mid, "geo")
            else:
                await _seed_edited(real_adapter, CHAT_A, mid)
            served[(CHAT_A, mid)] = _read(mid, edit_hide=True, media=_media("geo"))
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                update(Message)
                .where(Message.account_id == 1, Message.chat_id == CHAT_A, Message.id.between(41, 60))
                .values(edit_date=EDITED)
            )
            await session.commit()
        client = FakeTelegram(served)

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        # Each id once, 100 per request: 140 ids cost two requests, not three.
        assert [len(ids) for _chat, ids in client.calls] == [100, 40]
        assert sorted(_asked(client)) == list(range(1, 141))
        assert client.entity_calls == [CHAT_A]
        assert summary["kinds"]["geo"]["filled"] == 60
        assert summary["edits"]["hidden"] == 100
        assert (await _row(real_adapter, CHAT_A, 50)).edit_hide == 1


class TestCommandLine:
    def test_the_summary_prints_the_edit_flags(self, monkeypatch, capsys):
        import telegram_archive.__main__ as cli

        summary = telegram_backup._empty_backfill_summary()
        summary["edits"].update(hidden=3, shown=2, date_changed=1, not_served=4)

        async def _fake(config, chat_id=None, apply=False):
            return summary

        monkeypatch.setattr(telegram_backup, "run_backfill_details", _fake)
        monkeypatch.setattr("telegram_archive.config.Config", lambda: SimpleNamespace(log_summary=lambda: None))
        monkeypatch.setattr("telegram_archive.config.setup_logging", lambda config: None)
        args = cli.create_parser().parse_args(["backfill-details", "--apply"])

        assert cli.run_backfill_details(args) == 0
        out = capsys.readouterr().out
        assert "Edit flags filled, hidden:       3" in out
        assert "Edit flags filled, shown:        2" in out
        assert "Edits with a later edit time:    1" in out
        assert "Edits not served:                4" in out
