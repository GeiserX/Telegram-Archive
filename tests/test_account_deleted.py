"""A deleted Telegram account is recorded beside the identity the archive kept.

Since #636 the archive keeps a contact's names when Telegram reports the
account deleted. These tests pin the other half: ``account_deleted_at`` on
chats and users records when the archive first saw the deletion, every
builder that holds a Telethon ``User`` passes ``User.deleted``, the API rows
carry the field, and the importer never replaces captured names with the
export's one display string. The database tests run on SQLite and PostgreSQL.
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select, update
from telethon import events
from telethon.tl.types import User

from telegram_archive.db.adapter import ChatScope
from telegram_archive.db.models import Chat
from telegram_archive.db.models import User as UserRow
from telegram_archive.telegram_backup import TelegramBackup
from telegram_archive.telegram_import import TelegramImporter

NAMED = {"id": 201, "type": "private", "first_name": "Sample", "last_name": "Person", "username": "sample_user"}
DELETED = {"id": 201, "type": "private", "first_name": None, "last_name": None, "username": None, "phone": None}
FIRST_SEEN = datetime(2026, 1, 2, 3, 4, 5)


async def _chat(adapter, chat_id=201, account_id=1):
    return await adapter.get_chat_by_id(chat_id, account_id=account_id)


async def _backdate(adapter, model, **where):
    """Move a recorded deletion to a fixed past time, so a later write that moved it would show."""
    async with adapter.db_manager.async_session_factory() as session:
        stmt = update(model).values(account_deleted_at=FIRST_SEEN)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        await session.execute(stmt)
        await session.commit()


# ---------------------------------------------------------------------------
# Adapter: the column is set once, never cleared, never moved
# ---------------------------------------------------------------------------


async def test_deleted_flag_records_the_time_and_keeps_the_name(real_adapter):
    await real_adapter.upsert_chat(NAMED, account_id=1)
    assert (await _chat(real_adapter))["account_deleted_at"] is None

    await real_adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    row = await _chat(real_adapter)
    assert isinstance(row["account_deleted_at"], datetime)
    assert (row["first_name"], row["last_name"], row["username"]) == ("Sample", "Person", "sample_user")


async def test_recorded_deletion_is_never_cleared_or_moved(real_adapter):
    await real_adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    await _backdate(real_adapter, Chat, id=201)

    # A later snapshot that reports it deleted again keeps the first time.
    await real_adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    assert (await _chat(real_adapter))["account_deleted_at"] == FIRST_SEEN
    # A snapshot without the flag, or with it false, never clears it.
    await real_adapter.upsert_chat({"id": 201, "type": "private", "title": None}, account_id=1)
    await real_adapter.upsert_chat({**NAMED, "account_deleted": False}, account_id=1)
    assert (await _chat(real_adapter))["account_deleted_at"] == FIRST_SEEN


async def test_live_snapshot_never_sets_it(real_adapter):
    await real_adapter.upsert_chat({**NAMED, "account_deleted": False}, account_id=1)
    await real_adapter.upsert_chat({**DELETED}, account_id=1)  # the old all-empty shape, no flag
    row = await _chat(real_adapter)
    assert row["account_deleted_at"] is None
    assert row["first_name"] == "Sample"  # #636 still keeps the name without the flag


async def test_flag_keeps_identity_even_on_a_partial_snapshot(real_adapter):
    # The listener's metadata refresh sends only title and username. With the
    # flag, a deleted account's username is kept; without it, it still clears.
    await real_adapter.upsert_chat(NAMED, account_id=1)
    await real_adapter.upsert_chat(
        {"id": 201, "type": "private", "title": None, "username": None, "account_deleted": True}, account_id=1
    )
    assert (await _chat(real_adapter))["username"] == "sample_user"
    await real_adapter.upsert_chat({**NAMED, "id": 202}, account_id=1)
    await real_adapter.upsert_chat({"id": 202, "type": "private", "username": None}, account_id=1)
    assert (await _chat(real_adapter, 202))["username"] is None


async def test_flag_on_a_new_chat_inserts_the_time(real_adapter):
    await real_adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    assert isinstance((await _chat(real_adapter))["account_deleted_at"], datetime)


async def test_flag_is_per_account(real_adapter):
    await real_adapter.upsert_chat(NAMED, account_id=1)
    await real_adapter.upsert_chat(NAMED, account_id=2)
    await real_adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    assert (await _chat(real_adapter, account_id=1))["account_deleted_at"] is not None
    assert (await _chat(real_adapter, account_id=2))["account_deleted_at"] is None


async def test_user_deletion_is_recorded_once_and_keeps_the_identity(real_adapter):
    named = {"id": 301, "first_name": "Sample", "last_name": "Person", "username": "sample_user", "phone": "15550100"}
    await real_adapter.upsert_user(named)
    assert (await real_adapter.get_user_by_id(301))["account_deleted_at"] is None

    deleted = {"id": 301, "first_name": None, "last_name": None, "username": None, "phone": None}
    await real_adapter.upsert_user({**deleted, "account_deleted": True})
    user = await real_adapter.get_user_by_id(301)
    assert isinstance(user["account_deleted_at"], datetime)
    assert (user["first_name"], user["last_name"], user["username"], user["phone"]) == (
        "Sample",
        "Person",
        "sample_user",
        "15550100",
    )

    await _backdate(real_adapter, UserRow, id=301)
    await real_adapter.upsert_user({**deleted, "account_deleted": True})
    await real_adapter.upsert_user({"id": 301, "first_name": "Sample"})
    assert (await real_adapter.get_user_by_id(301))["account_deleted_at"] == FIRST_SEEN


async def test_user_flag_keeps_names_on_a_partial_snapshot(real_adapter):
    await real_adapter.upsert_user({"id": 302, "first_name": "Sample", "username": "sample_user"})
    await real_adapter.upsert_user({"id": 302, "username": None, "account_deleted": True})
    user = await real_adapter.get_user_by_id(302)
    assert (user["first_name"], user["username"]) == ("Sample", "sample_user")
    assert user["account_deleted_at"] is not None


# ---------------------------------------------------------------------------
# Adapter read paths the API serves
# ---------------------------------------------------------------------------


async def test_chat_rows_carry_the_field(real_adapter):
    await real_adapter.upsert_chat(NAMED, account_id=1)
    await real_adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    await real_adapter.upsert_chat({"id": -1001, "type": "channel", "title": "News"}, account_id=1)
    rows = {row["id"]: row for row in await real_adapter.get_all_chats(account_id=1)}
    assert rows[201]["account_deleted_at"] is not None
    assert rows[-1001]["account_deleted_at"] is None
    by_ref = await real_adapter.get_chat_by_ref(rows[201]["ref"], account_id=1)
    assert by_ref["account_deleted_at"] == rows[201]["account_deleted_at"]
    # Search by the kept name still finds the deleted account.
    assert [row["id"] for row in await real_adapter.get_all_chats(search="Sample Person", account_id=1)] == [201]


async def _seed_group_message(adapter, sender_id, message_id=1, *, pinned=False):
    await adapter.upsert_chat({"id": -5001, "type": "group", "title": "Sample group"}, account_id=1)
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": -5001,
            "sender_id": sender_id,
            "date": datetime(2026, 1, 1, 12, 0, message_id),
            "text": f"sample text {message_id}",
            "sender_name": "Sample Person",
            "is_pinned": 1 if pinned else 0,
            "raw_data": {},
        },
        account_id=1,
    )


async def test_message_rows_carry_the_sender_deletion(real_adapter):
    await real_adapter.upsert_user({"id": 401, "first_name": "Sample"})
    await real_adapter.upsert_user({"id": 401, "first_name": None, "last_name": None, "username": None})
    await real_adapter.upsert_user({"id": 401, "account_deleted": True})
    await real_adapter.upsert_user({"id": 402, "first_name": "Live"})
    await _seed_group_message(real_adapter, 401, 1, pinned=True)
    await _seed_group_message(real_adapter, 402, 2)

    rows = {row["id"]: row for row in await real_adapter.get_messages_paginated(-5001, account_id=1)}
    assert rows[1]["sender_account_deleted_at"] is not None
    assert rows[1]["first_name"] == "Sample"
    assert rows[2]["sender_account_deleted_at"] is None

    pinned = await real_adapter.get_pinned_messages(-5001, account_id=1)
    assert [row["sender_account_deleted_at"] is not None for row in pinned] == [True]

    by_date = await real_adapter.find_message_by_date_with_joins(-5001, datetime(2026, 1, 1), account_id=1)
    assert by_date["id"] == 1
    assert by_date["sender_account_deleted_at"] is not None


async def test_global_search_hit_names_the_deleted_chat(real_adapter):
    await real_adapter.upsert_chat({**NAMED, "account_deleted": True}, account_id=1)
    await real_adapter.insert_message(
        {
            "id": 1,
            "chat_id": 201,
            "sender_id": 201,
            "date": datetime(2026, 1, 1, 12),
            "text": "zebracrossing sample",
            "raw_data": {},
        },
        account_id=1,
    )
    payload = await real_adapter.search_messages_global("zebracrossing", scope=ChatScope.build())
    assert [row["chat_account_deleted_at"] is not None for row in payload["results"]] == [True]


# ---------------------------------------------------------------------------
# Builders pass Telethon's User.deleted explicitly
# ---------------------------------------------------------------------------


def test_account_deleted_reads_only_a_real_true():
    from telegram_archive.message_utils import account_deleted

    assert account_deleted(User(id=1, deleted=True)) is True
    assert account_deleted(User(id=1, deleted=False)) is False
    assert account_deleted(User(id=1)) is False
    # A bare MagicMock attribute is truthy; it must not read as a deletion.
    assert account_deleted(MagicMock()) is False
    assert account_deleted(None) is False


def _backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.db = AsyncMock()
    return backup


def test_backup_chat_data_carries_the_flag():
    backup = _backup()
    deleted = backup._extract_chat_data(User(id=201, deleted=True))
    assert deleted["account_deleted"] is True
    assert deleted["type"] == "private"
    live = backup._extract_chat_data(User(id=202, first_name="Sample", deleted=False))
    assert live["account_deleted"] is False


def test_backup_user_data_carries_the_flag():
    backup = _backup()
    assert backup._extract_user_data(User(id=201, deleted=True))["account_deleted"] is True
    assert backup._extract_user_data(User(id=202, first_name="Sample"))["account_deleted"] is False


async def test_backup_sender_cache_writes_again_when_the_account_is_deleted():
    # Same names, now deleted: the fingerprint must differ, or the deletion is never written.
    backup = _backup()
    await backup._save_sender(User(id=201, first_name="Sample"))
    await backup._save_sender(User(id=201, first_name="Sample", deleted=True))
    assert backup.db.upsert_user.await_count == 2
    assert backup.db.upsert_user.call_args[0][0]["account_deleted"] is True


async def test_listener_new_message_passes_the_flag_for_chat_and_sender():
    from test_listener_extended import _make_listener_with_handlers

    listener, handlers, db, _config = _make_listener_with_handlers()
    listener._tracked_chat_ids.add(201)
    handler = handlers[events.NewMessage]

    event = MagicMock()
    event.chat_id = 201
    msg = MagicMock()
    msg.reply_to = None
    msg.id = 42
    msg.sender_id = 201
    msg.date = datetime(2026, 1, 1)
    msg.text = "sample text"
    msg.reply_to_msg_id = None
    msg.edit_date = None
    msg.out = False
    msg.grouped_id = None
    msg.media = None
    msg.sender = User(id=201, deleted=True)
    event.message = msg
    event.get_chat = AsyncMock(return_value=User(id=201, deleted=True))

    await handler(event)

    assert db.upsert_chat.call_args[0][0]["account_deleted"] is True
    assert db.upsert_user.call_args[0][0]["account_deleted"] is True


# ---------------------------------------------------------------------------
# Importer: the export's display string never replaces a captured name
# ---------------------------------------------------------------------------


def _export(tmp_path: Path, chats: list[dict]) -> str:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    payload = {"personal_information": {"user_id": 9000, "first_name": "Owner"}, "chats": {"list": chats}}
    (export_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return str(export_dir)


def _message(msg_id: int, sender: str | None, sender_id: int) -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "date": f"2024-01-15T10:00:{msg_id:02d}",
        "from": sender,
        "from_id": f"user{sender_id}",
        "text": "sample text",
    }


async def _import(real_adapter, tmp_path, chats):
    importer = TelegramImporter(real_adapter, str(tmp_path / "media"), account_id=1)
    return await importer.run(_export(tmp_path, chats), merge=True)


async def test_import_keeps_captured_names_and_fills_only_missing_ones(real_adapter, tmp_path):
    await real_adapter.upsert_chat(NAMED, account_id=1)
    await real_adapter.upsert_chat({"id": -1000000000777, "type": "group", "title": "Captured title"}, account_id=1)
    await real_adapter.upsert_chat({"id": -1000000000779, "type": "group", "title": "Kept title"}, account_id=1)
    await real_adapter.upsert_user({"id": 201, "first_name": "Sample", "last_name": "Person"})
    await real_adapter.upsert_user({"id": 501, "first_name": None})

    chats = [
        # A deleted account exports with no name; a live one with one display string.
        {"name": None, "type": "personal_chat", "id": 201, "messages": [_message(1, None, 201)]},
        {
            "name": "Export title",
            "type": "private_supergroup",
            "id": 777,
            "messages": [_message(1, "Sample Person", 201), _message(2, "Filled Name", 501), _message(3, "New", 502)],
        },
        # A blank export title never replaces the archived one.
        {"name": "", "type": "private_supergroup", "id": 779, "messages": [_message(1, "Sample Person", 201)]},
    ]
    await _import(real_adapter, tmp_path, chats)

    chat = await _chat(real_adapter)
    assert (chat["first_name"], chat["last_name"], chat["username"]) == ("Sample", "Person", "sample_user")
    # A group renamed after the last backup takes the export's title.
    assert (await _chat(real_adapter, -1000000000777))["title"] == "Export title"
    assert (await _chat(real_adapter, -1000000000779))["title"] == "Kept title"
    sender = await real_adapter.get_user_by_id(201)
    assert (sender["first_name"], sender["last_name"]) == ("Sample", "Person")
    assert (await real_adapter.get_user_by_id(501))["first_name"] == "Filled Name"
    assert (await real_adapter.get_user_by_id(502))["first_name"] == "New"


async def test_import_names_a_chat_the_archive_has_no_name_for(real_adapter, tmp_path):
    await real_adapter.upsert_chat({"id": 203, "type": "private"}, account_id=1)
    chats = [
        {"name": "Sample Person", "type": "personal_chat", "id": 203, "messages": [_message(1, "Sample", 203)]},
        {"name": "Export title", "type": "private_supergroup", "id": 778, "messages": [_message(1, "Sample", 203)]},
    ]
    await _import(real_adapter, tmp_path, chats)
    assert (await _chat(real_adapter, 203))["first_name"] == "Sample Person"
    assert (await _chat(real_adapter, -1000000000778))["title"] == "Export title"
    async with real_adapter.db_manager.async_session_factory() as session:
        user = (await session.execute(select(UserRow).where(UserRow.id == 203))).scalar_one()
    assert user.first_name == "Sample"


async def _backup_private_dialog(user: User, user_row_error: Exception | None = None):
    from test_telegram_backup import TestBackupCheckpointing

    case = TestBackupCheckpointing("test_checkpoint_after_every_batch")
    case.setUp()
    try:
        backup = case.backup
        backup._extract_chat_data = TelegramBackup._extract_chat_data.__get__(backup)
        backup.config.download_chat_description = False

        async def no_messages(*args, **kwargs):
            return
            yield

        backup.client.iter_messages = no_messages
        backup._sync_pinned_messages = AsyncMock()
        if user_row_error is not None:
            case.db.upsert_user.side_effect = user_row_error
        dialog = MagicMock()
        dialog.entity = user
        await backup._backup_dialog(dialog, False)
        return case.db
    finally:
        case.tearDown()


async def test_backup_dialog_of_a_deleted_account_records_it_on_the_user_row():
    # A deleted account sends no more messages, so the sender path never
    # writes its users row again; the dialog refresh must.
    db = await _backup_private_dialog(User(id=100, deleted=True))
    assert db.upsert_chat.call_args[0][0]["account_deleted"] is True
    db.upsert_user.assert_awaited_once()
    assert db.upsert_user.call_args[0][0]["account_deleted"] is True

    live = await _backup_private_dialog(User(id=100, first_name="Sample"))
    live.upsert_user.assert_not_awaited()


async def test_backup_dialog_of_a_deleted_account_survives_a_failed_user_row():
    # Recording the deletion on the users row is best effort: a failure there
    # never stops the dialog's message backup.
    db = await _backup_private_dialog(User(id=100, deleted=True), user_row_error=RuntimeError("users row"))
    db.upsert_user.assert_awaited_once()
    db.get_last_message_id.assert_awaited()
