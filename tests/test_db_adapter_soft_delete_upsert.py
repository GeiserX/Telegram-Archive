import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Message, MessageVersion


@pytest.fixture
async def sqlite_adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'telegram_archive.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _get_message(adapter: DatabaseAdapter, message_id: int, chat_id: int) -> Message:
    async with adapter.db_manager.async_session_factory() as session:
        # v8.0.0 PK order: (account_id, chat_id, id).
        message = await session.get(Message, (1, chat_id, message_id))
        assert message is not None
        return message


async def _get_versions(adapter: DatabaseAdapter, message_id: int, chat_id: int) -> list[MessageVersion]:
    async with adapter.db_manager.async_session_factory() as session:
        result = await session.execute(
            select(MessageVersion)
            .where(MessageVersion.message_id == message_id, MessageVersion.chat_id == chat_id)
            .order_by(MessageVersion.id.asc())
        )
        return list(result.scalars())


@pytest.mark.asyncio
async def test_insert_message_upsert_preserves_soft_delete_marker(sqlite_adapter):
    deleted_at = datetime(2026, 6, 25, 10, 30)

    await sqlite_adapter.insert_message(
        {
            "id": 1,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 10, 0),
            "text": "original",
        },
        account_id=1,
    )
    await sqlite_adapter.mark_message_deleted(100, 1, deleted_at, account_id=1)

    await sqlite_adapter.insert_message(
        {
            "id": 1,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 10, 0),
            "text": "reprocessed",
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 1, 100)
    # Re-processing without edit evidence (no edit_date) must NOT overwrite
    # archived text — upserts only replace text with a newer/equal edit_date.
    assert message.text == "original"
    assert message.is_deleted == 1
    assert message.deleted_at == deleted_at


@pytest.mark.asyncio
async def test_insert_messages_batch_upsert_preserves_soft_delete_marker(sqlite_adapter):
    deleted_at = datetime(2026, 6, 25, 11, 30)

    await sqlite_adapter.insert_messages_batch(
        [
            {
                "id": 2,
                "chat_id": 100,
                "date": datetime(2026, 6, 25, 11, 0),
                "text": "original",
            }
        ],
        account_id=1,
    )
    await sqlite_adapter.mark_message_deleted(100, 2, deleted_at, account_id=1)

    await sqlite_adapter.insert_messages_batch(
        [
            {
                "id": 2,
                "chat_id": 100,
                "date": datetime(2026, 6, 25, 11, 0),
                "text": "reprocessed",
            }
        ],
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 2, 100)
    # Same invariant as above for the batch path: no edit evidence, no overwrite.
    assert message.text == "original"
    assert message.is_deleted == 1
    assert message.deleted_at == deleted_at


@pytest.mark.asyncio
async def test_fresh_insert_not_marked_deleted(sqlite_adapter):
    """A brand-new message inserts as not-deleted with a null deleted_at."""
    await sqlite_adapter.insert_message(
        {"id": 3, "chat_id": 100, "date": datetime(2026, 6, 25, 12, 0), "text": "hello"}, account_id=1
    )

    message = await _get_message(sqlite_adapter, 3, 100)
    assert message.is_deleted == 0
    assert message.deleted_at is None


@pytest.mark.asyncio
async def test_single_upsert_preserves_first_nonblank_sender_name(sqlite_adapter):
    message_data = {
        "id": 8,
        "chat_id": 100,
        "date": datetime(2026, 7, 27, 12, 0),
        "text": "hello",
        "sender_name": "  Original Name  ",
    }
    await sqlite_adapter.insert_message(message_data, account_id=1)
    await sqlite_adapter.insert_message({**message_data, "sender_name": "Renamed User"}, account_id=1)
    await sqlite_adapter.insert_message({**message_data, "sender_name": "   "}, account_id=1)
    await sqlite_adapter.insert_message(
        {key: value for key, value in message_data.items() if key != "sender_name"}, account_id=1
    )

    message = await _get_message(sqlite_adapter, 8, 100)
    assert message.sender_name == "Original Name"


@pytest.mark.asyncio
async def test_single_upsert_fills_missing_sender_name_once(sqlite_adapter):
    message_data = {
        "id": 9,
        "chat_id": 100,
        "date": datetime(2026, 7, 27, 12, 1),
        "text": "hello",
    }
    await sqlite_adapter.insert_message(message_data, account_id=1)
    await sqlite_adapter.insert_message({**message_data, "sender_name": "First Snapshot"}, account_id=1)
    await sqlite_adapter.insert_message({**message_data, "sender_name": "Later Snapshot"}, account_id=1)

    message = await _get_message(sqlite_adapter, 9, 100)
    assert message.sender_name == "First Snapshot"


@pytest.mark.asyncio
async def test_batch_upsert_fills_blank_sender_name_once(sqlite_adapter):
    message_data = {
        "id": 10,
        "chat_id": 100,
        "date": datetime(2026, 7, 27, 12, 2),
        "text": "hello",
        "sender_name": "",
    }
    await sqlite_adapter.insert_messages_batch([message_data], account_id=1)
    await sqlite_adapter.insert_messages_batch([{**message_data, "sender_name": "Batch Snapshot"}], account_id=1)
    await sqlite_adapter.insert_messages_batch([{**message_data, "sender_name": "Changed Later"}], account_id=1)

    message = await _get_message(sqlite_adapter, 10, 100)
    assert message.sender_name == "Batch Snapshot"


@pytest.mark.asyncio
async def test_sender_name_exposed_in_message_media_and_export_projections(sqlite_adapter):
    message_data = {
        "id": 11,
        "chat_id": 200,
        "sender_id": 501,
        "date": datetime(2026, 7, 27, 12, 3),
        "text": "hello",
        "sender_name": "Captured Name",
    }
    await sqlite_adapter.insert_message(message_data, account_id=1)
    await sqlite_adapter.upsert_user({"id": 501, "first_name": "Current", "last_name": "Name", "username": "current"})
    await sqlite_adapter.insert_media(
        {
            "id": "media-11",
            "message_id": 11,
            "chat_id": 200,
            "type": "photo",
            "file_path": "/archive/photo.jpg",
            "downloaded": True,
        },
        account_id=1,
    )

    messages = await sqlite_adapter.get_messages_by_date_range(chat_id=200)
    media = await sqlite_adapter.get_media_paginated(200)
    exported = [row async for row in sqlite_adapter.get_messages_for_export(200)]

    assert messages[0]["sender_name"] == "Captured Name"
    assert media["items"][0]["sender_name"] == "Captured Name"
    assert exported[0]["sender"]["name"] == "Captured Name"


@pytest.mark.asyncio
async def test_upsert_with_is_deleted_and_timestamp_sets_marker(sqlite_adapter):
    """An upsert whose payload carries is_deleted + deleted_at sets both on conflict."""
    deleted_at = datetime(2026, 6, 25, 13, 30)

    await sqlite_adapter.insert_message(
        {"id": 4, "chat_id": 100, "date": datetime(2026, 6, 25, 13, 0), "text": "original"}, account_id=1
    )
    await sqlite_adapter.insert_message(
        {
            "id": 4,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 13, 0),
            "text": "now deleted",
            "is_deleted": 1,
            "deleted_at": deleted_at,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 4, 100)
    assert message.is_deleted == 1
    assert message.deleted_at == deleted_at


@pytest.mark.asyncio
async def test_upsert_with_is_deleted_no_timestamp_preserves_existing(sqlite_adapter):
    """An upsert with is_deleted=1 but no deleted_at keeps the existing timestamp."""
    first_deleted_at = datetime(2026, 6, 25, 14, 30)

    await sqlite_adapter.insert_message(
        {"id": 5, "chat_id": 100, "date": datetime(2026, 6, 25, 14, 0), "text": "original"}, account_id=1
    )
    await sqlite_adapter.mark_message_deleted(100, 5, first_deleted_at, account_id=1)
    await sqlite_adapter.insert_message(
        {
            "id": 5,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 14, 0),
            "text": "reprocessed",
            "is_deleted": 1,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 5, 100)
    assert message.is_deleted == 1
    assert message.deleted_at == first_deleted_at


@pytest.mark.asyncio
async def test_mark_message_deleted_twice_keeps_first_timestamp(sqlite_adapter):
    """Re-marking a soft-deleted message preserves the original deletion time (coalesce)."""
    first = datetime(2026, 6, 25, 15, 0)
    second = datetime(2026, 6, 25, 16, 0)

    await sqlite_adapter.insert_message(
        {"id": 6, "chat_id": 100, "date": datetime(2026, 6, 25, 14, 0), "text": "original"}, account_id=1
    )
    await sqlite_adapter.mark_message_deleted(100, 6, first, account_id=1)
    await sqlite_adapter.mark_message_deleted(100, 6, second, account_id=1)

    message = await _get_message(sqlite_adapter, 6, 100)
    assert message.is_deleted == 1
    assert message.deleted_at == first


@pytest.mark.asyncio
async def test_mark_message_deleted_defaults_timestamp_when_none(sqlite_adapter):
    """Omitting deleted_at falls back to a server-generated timestamp."""
    await sqlite_adapter.insert_message(
        {"id": 7, "chat_id": 100, "date": datetime(2026, 6, 25, 14, 0), "text": "original"}, account_id=1
    )
    await sqlite_adapter.mark_message_deleted(100, 7, account_id=1)

    message = await _get_message(sqlite_adapter, 7, 100)
    assert message.is_deleted == 1
    assert message.deleted_at is not None


@pytest.mark.asyncio
async def test_get_messages_sync_data_excludes_soft_deleted(sqlite_adapter):
    """Soft-deleted rows are excluded from the sync set so they aren't re-checked."""
    await sqlite_adapter.insert_message(
        {"id": 10, "chat_id": 200, "date": datetime(2026, 6, 25, 17, 0), "text": "live"}, account_id=1
    )
    await sqlite_adapter.insert_message(
        {"id": 11, "chat_id": 200, "date": datetime(2026, 6, 25, 17, 1), "text": "to delete"}, account_id=1
    )
    await sqlite_adapter.mark_message_deleted(200, 11, account_id=1)

    sync_data = await sqlite_adapter.get_messages_sync_data(200, account_id=1)
    assert set(sync_data.keys()) == {10}


@pytest.mark.asyncio
async def test_update_message_text_records_previous_version(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 20, "chat_id": 300, "date": datetime(2026, 6, 26, 9, 0), "text": "original"}, account_id=1
    )

    edit_date = datetime(2026, 6, 26, 9, 5)
    await sqlite_adapter.update_message_text(300, 20, "edited", edit_date, account_id=1)

    message = await _get_message(sqlite_adapter, 20, 300)
    versions = await _get_versions(sqlite_adapter, 20, 300)
    assert message.text == "edited"
    assert message.edit_date == edit_date
    assert len(versions) == 1
    assert versions[0].text == "original"
    assert versions[0].date == datetime(2026, 6, 26, 9, 0)


@pytest.mark.asyncio
async def test_update_message_text_is_idempotent(sqlite_adapter):
    edit_date = datetime(2026, 6, 26, 10, 5)
    await sqlite_adapter.insert_message(
        {"id": 21, "chat_id": 300, "date": datetime(2026, 6, 26, 10, 0), "text": "original"}, account_id=1
    )

    await sqlite_adapter.update_message_text(300, 21, "edited", edit_date, account_id=1)
    await sqlite_adapter.update_message_text(300, 21, "edited", edit_date, account_id=1)

    versions = await _get_versions(sqlite_adapter, 21, 300)
    assert len(versions) == 1
    assert versions[0].text == "original"


@pytest.mark.asyncio
async def test_update_message_text_same_text_is_noop_and_does_not_bump_edit_date(sqlite_adapter):
    # #219: a reaction-only change bumps Telegram's edit_date with unchanged text.
    # The archive must NOT advance its stored edit_date (it would surface a phantom
    # "edited" marker with no version). Same-text edits are a no-op.
    await sqlite_adapter.insert_message(
        {"id": 28, "chat_id": 300, "date": datetime(2026, 6, 26, 10, 0), "text": "original"}, account_id=1
    )

    reaction_edit_date = datetime(2026, 6, 26, 10, 15)
    outcome, _ = await sqlite_adapter.update_message_text(300, 28, "original", reaction_edit_date, account_id=1)

    message = await _get_message(sqlite_adapter, 28, 300)
    versions = await _get_versions(sqlite_adapter, 28, 300)
    assert outcome == "noop"
    assert message.text == "original"
    assert message.edit_date is None  # not bumped by the reaction-only "edit"
    assert versions == []


@pytest.mark.asyncio
async def test_update_message_text_older_edit_date_does_not_roll_back(sqlite_adapter):
    current_edit_date = datetime(2026, 6, 26, 10, 30)
    old_edit_date = datetime(2026, 6, 26, 10, 10)
    await sqlite_adapter.insert_message(
        {
            "id": 27,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 10, 0),
            "text": "current",
            "edit_date": current_edit_date,
        },
        account_id=1,
    )

    await sqlite_adapter.update_message_text(300, 27, "older", old_edit_date, account_id=1)

    message = await _get_message(sqlite_adapter, 27, 300)
    versions = await _get_versions(sqlite_adapter, 27, 300)
    assert message.text == "current"
    assert message.edit_date == current_edit_date
    assert versions == []


@pytest.mark.asyncio
async def test_text_only_edit_records_previous_version(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 22, "chat_id": 300, "date": datetime(2026, 6, 26, 11, 0), "text": "caption"}, account_id=1
    )

    await sqlite_adapter.update_message_text(300, 22, "caption edited", None, account_id=1)

    message = await _get_message(sqlite_adapter, 22, 300)
    versions = await _get_versions(sqlite_adapter, 22, 300)
    assert message.text == "caption edited"
    assert message.edit_date is None
    assert len(versions) == 1
    assert versions[0].text == "caption"
    assert versions[0].date == datetime(2026, 6, 26, 11, 0)


@pytest.mark.asyncio
async def test_upsert_with_newer_edit_date_records_previous_version(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 23, "chat_id": 300, "date": datetime(2026, 6, 26, 12, 0), "text": "original"}, account_id=1
    )
    edit_date = datetime(2026, 6, 26, 12, 30)

    await sqlite_adapter.insert_message(
        {
            "id": 23,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "edited via backup",
            "edit_date": edit_date,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 23, 300)
    versions = await _get_versions(sqlite_adapter, 23, 300)
    assert message.text == "edited via backup"
    assert message.edit_date == edit_date
    assert len(versions) == 1
    assert versions[0].text == "original"
    assert versions[0].date == datetime(2026, 6, 26, 12, 0)


@pytest.mark.asyncio
async def test_upsert_with_same_edit_date_records_previous_version(sqlite_adapter):
    edit_date = datetime(2026, 6, 26, 12, 30)
    await sqlite_adapter.insert_message(
        {
            "id": 33,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "original",
            "edit_date": edit_date,
        },
        account_id=1,
    )

    await sqlite_adapter.insert_message(
        {
            "id": 33,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "edited via backup",
            "edit_date": edit_date,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 33, 300)
    versions = await _get_versions(sqlite_adapter, 33, 300)
    assert message.text == "edited via backup"
    assert message.edit_date == edit_date
    assert len(versions) == 1
    assert versions[0].text == "original"
    assert versions[0].date == edit_date


@pytest.mark.asyncio
async def test_upsert_with_same_aware_edit_date_records_previous_version(sqlite_adapter):
    edit_date = datetime(2026, 6, 26, 12, 30)
    aware_edit_date = datetime(2026, 6, 26, 12, 30, tzinfo=UTC)
    await sqlite_adapter.insert_message(
        {
            "id": 34,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "original",
            "edit_date": edit_date,
        },
        account_id=1,
    )

    await sqlite_adapter.insert_message(
        {
            "id": 34,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "edited via backup",
            "edit_date": aware_edit_date,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 34, 300)
    versions = await _get_versions(sqlite_adapter, 34, 300)
    assert message.text == "edited via backup"
    assert message.edit_date == edit_date
    assert len(versions) == 1
    assert versions[0].text == "original"
    assert versions[0].date == edit_date


@pytest.mark.asyncio
async def test_repeated_upsert_with_same_edit_date_is_idempotent(sqlite_adapter):
    edit_date = datetime(2026, 6, 26, 12, 30)
    edited_message = {
        "id": 35,
        "chat_id": 300,
        "date": datetime(2026, 6, 26, 12, 0),
        "text": "edited via backup",
        "edit_date": edit_date,
    }
    await sqlite_adapter.insert_message(
        {
            "id": 35,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "original",
            "edit_date": edit_date,
        },
        account_id=1,
    )

    await sqlite_adapter.insert_message(edited_message, account_id=1)
    await sqlite_adapter.insert_message(edited_message, account_id=1)

    message = await _get_message(sqlite_adapter, 35, 300)
    versions = await _get_versions(sqlite_adapter, 35, 300)
    assert message.text == "edited via backup"
    assert message.edit_date == edit_date
    assert len(versions) == 1
    assert versions[0].text == "original"
    assert versions[0].date == edit_date


@pytest.mark.asyncio
async def test_upsert_same_text_does_not_bump_edit_date(sqlite_adapter):
    # #219: the backup/gap-fill/import upsert path must also NOT advance edit_date
    # on an unchanged-text re-scan (a re-fetched message whose only server-side
    # change was a reaction), or the phantom "edited" marker resurfaces post-sweep.
    await sqlite_adapter.insert_message(
        {"id": 29, "chat_id": 300, "date": datetime(2026, 6, 26, 12, 0), "text": "original"}, account_id=1
    )
    reaction_edit_date = datetime(2026, 6, 26, 12, 30)

    await sqlite_adapter.insert_message(
        {
            "id": 29,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 12, 0),
            "text": "original",
            "edit_date": reaction_edit_date,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 29, 300)
    versions = await _get_versions(sqlite_adapter, 29, 300)
    assert message.text == "original"
    assert message.edit_date is None  # same text -> edit_date not bumped
    assert versions == []


@pytest.mark.asyncio
async def test_upsert_with_older_edit_date_does_not_roll_back(sqlite_adapter):
    current_edit_date = datetime(2026, 6, 26, 13, 30)
    old_edit_date = datetime(2026, 6, 26, 13, 5)
    await sqlite_adapter.insert_message(
        {
            "id": 24,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 13, 0),
            "text": "current text",
            "edit_date": current_edit_date,
        },
        account_id=1,
    )

    await sqlite_adapter.insert_message(
        {
            "id": 24,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 13, 0),
            "text": "old import text",
            "edit_date": old_edit_date,
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 24, 300)
    versions = await _get_versions(sqlite_adapter, 24, 300)
    assert message.text == "current text"
    assert message.edit_date == current_edit_date
    assert versions == []


@pytest.mark.asyncio
async def test_concurrent_upserts_keep_newest_edit_date(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 32, "chat_id": 300, "date": datetime(2026, 6, 26, 15, 0), "text": "original"}, account_id=1
    )

    async def upsert(text: str, edit_date: datetime) -> None:
        await sqlite_adapter.insert_message(
            {
                "id": 32,
                "chat_id": 300,
                "date": datetime(2026, 6, 26, 15, 0),
                "text": text,
                "edit_date": edit_date,
            },
            account_id=1,
        )

    newer = datetime(2026, 6, 26, 15, 30)
    older = datetime(2026, 6, 26, 15, 10)
    await asyncio.gather(upsert("newer", newer), upsert("older", older))

    message = await _get_message(sqlite_adapter, 32, 300)
    assert message.text == "newer"
    assert message.edit_date == newer


@pytest.mark.asyncio
async def test_upsert_filling_empty_text_preserves_existing_edit_date(sqlite_adapter):
    current_edit_date = datetime(2026, 6, 26, 13, 45)
    await sqlite_adapter.insert_message(
        {
            "id": 26,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 13, 40),
            "text": "",
            "edit_date": current_edit_date,
        },
        account_id=1,
    )

    await sqlite_adapter.insert_message(
        {
            "id": 26,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 13, 40),
            "text": "filled text",
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 26, 300)
    versions = await _get_versions(sqlite_adapter, 26, 300)
    assert message.text == "filled text"
    assert message.edit_date == current_edit_date
    assert len(versions) == 1
    assert versions[0].text == ""
    assert versions[0].date == current_edit_date


@pytest.mark.asyncio
async def test_get_message_versions_returns_dicts(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 25, "chat_id": 300, "date": datetime(2026, 6, 26, 14, 0), "text": "v1"}, account_id=1
    )
    await sqlite_adapter.update_message_text(300, 25, "v2", datetime(2026, 6, 26, 14, 5), account_id=1)
    await sqlite_adapter.update_message_text(300, 25, "v3", datetime(2026, 6, 26, 14, 10), account_id=1)

    versions = await sqlite_adapter.get_message_versions(300, 25)
    assert len(versions) == 2
    assert versions[0]["message_id"] == 25
    assert versions[0]["chat_id"] == 300
    assert "id" not in versions[0]
    assert "change_hash" not in versions[0]
    assert "captured_at" not in versions[0]
    assert [version["text"] for version in versions] == ["v2", "v1"]
    assert [version["date"] for version in versions] == [
        datetime(2026, 6, 26, 14, 5),
        datetime(2026, 6, 26, 14, 0),
    ]

    limited_versions = await sqlite_adapter.get_message_versions(300, 25, limit=1)
    assert [version["text"] for version in limited_versions] == ["v2"]


@pytest.mark.asyncio
async def test_get_messages_paginated_includes_version_counts(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 40, "chat_id": 300, "date": datetime(2026, 6, 26, 15, 0), "text": "v1"}, account_id=1
    )
    await sqlite_adapter.insert_message(
        {"id": 41, "chat_id": 300, "date": datetime(2026, 6, 26, 15, 1), "text": "unchanged"}, account_id=1
    )
    await sqlite_adapter.insert_message(
        {"id": 42, "chat_id": 300, "date": datetime(2026, 6, 26, 15, 2), "text": "text-only"}, account_id=1
    )
    await sqlite_adapter.update_message_text(300, 40, "v2", datetime(2026, 6, 26, 15, 5), account_id=1)
    await sqlite_adapter.update_message_text(300, 40, "v3", datetime(2026, 6, 26, 15, 10), account_id=1)
    await sqlite_adapter.update_message_text(300, 42, "text-only edited", None, account_id=1)

    messages = await sqlite_adapter.get_messages_paginated(300, limit=10)
    counts = {message["id"]: message["version_count"] for message in messages}
    assert counts[40] == 2
    assert counts[41] == 0
    assert counts[42] == 1


@pytest.mark.asyncio
async def test_get_message_versions_by_date_range_filters_version_dates(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 30, "chat_id": 300, "date": datetime(2026, 6, 25, 14, 0), "text": "old"}, account_id=1
    )
    await sqlite_adapter.insert_message(
        {"id": 31, "chat_id": 300, "date": datetime(2026, 6, 26, 14, 0), "text": "new"}, account_id=1
    )
    await sqlite_adapter.update_message_text(300, 30, "old edited", datetime(2026, 6, 25, 14, 5), account_id=1)
    await sqlite_adapter.update_message_text(300, 31, "new edited", datetime(2026, 6, 26, 14, 5), account_id=1)

    versions = await sqlite_adapter.get_message_versions_by_date_range(
        chat_id=300,
        start_date=datetime(2026, 6, 26),
        end_date=datetime(2026, 6, 27),
    )

    assert [row["message_id"] for row in versions] == [31]


@pytest.mark.asyncio
async def test_update_message_text_reports_outcome(sqlite_adapter):
    await sqlite_adapter.insert_message(
        {"id": 50, "chat_id": 300, "date": datetime(2026, 6, 26, 9, 0), "text": "original"}, account_id=1
    )

    applied, applied_prior = await sqlite_adapter.update_message_text(
        300, 50, "edited", datetime(2026, 6, 26, 9, 5), account_id=1
    )
    noop, noop_prior = await sqlite_adapter.update_message_text(
        300, 50, "edited", datetime(2026, 6, 26, 9, 5), account_id=1
    )
    missing, missing_prior = await sqlite_adapter.update_message_text(
        300, 999, "x", datetime(2026, 6, 26, 9, 5), account_id=1
    )

    assert applied == "applied"
    assert applied_prior == {"text": "original", "sender_id": None, "sender_name": None}
    assert noop == "noop"
    assert noop_prior is None
    assert missing == "not_found"
    assert missing_prior is None


@pytest.mark.asyncio
async def test_upsert_preserves_is_pinned_when_absent(sqlite_adapter):
    """Re-backups without pinning data must not reset the pinned flag.

    Regression guard for the _message_conflict_update_values pop: the old
    unconditional upsert reset is_pinned to 0 on every re-scan.
    """
    await sqlite_adapter.insert_message(
        {"id": 51, "chat_id": 300, "date": datetime(2026, 6, 26, 10, 0), "text": "pin me", "is_pinned": 1}, account_id=1
    )

    await sqlite_adapter.insert_message(
        {"id": 51, "chat_id": 300, "date": datetime(2026, 6, 26, 10, 0), "text": "pin me"}, account_id=1
    )

    message = await _get_message(sqlite_adapter, 51, 300)
    assert message.is_pinned == 1


@pytest.mark.asyncio
async def test_upsert_different_text_without_any_edit_date_is_refused(sqlite_adapter):
    """Documented conservative case: differing text with NO edit evidence on either
    side is not applied and records no version (an upsert source must prove
    freshness via edit_date before replacing archived text)."""
    await sqlite_adapter.insert_message(
        {"id": 52, "chat_id": 300, "date": datetime(2026, 6, 26, 11, 0), "text": "archived"}, account_id=1
    )

    await sqlite_adapter.insert_message(
        {"id": 52, "chat_id": 300, "date": datetime(2026, 6, 26, 11, 0), "text": "import text"}, account_id=1
    )

    message = await _get_message(sqlite_adapter, 52, 300)
    versions = await _get_versions(sqlite_adapter, 52, 300)
    assert message.text == "archived"
    assert versions == []


@pytest.mark.asyncio
async def test_version_record_failure_does_not_abort_message_update(sqlite_adapter, monkeypatch):
    """A poisoned version insert is contained by its SAVEPOINT: the text update
    still lands, the failure only costs the history entry."""
    await sqlite_adapter.insert_message(
        {"id": 53, "chat_id": 300, "date": datetime(2026, 6, 26, 12, 0), "text": "original"}, account_id=1
    )

    def _broken_stmt(values):
        raise RuntimeError("simulated version-insert failure")

    monkeypatch.setattr(sqlite_adapter, "_insert_message_version_stmt", _broken_stmt)

    outcome, _ = await sqlite_adapter.update_message_text(300, 53, "edited", datetime(2026, 6, 26, 12, 5), account_id=1)

    message = await _get_message(sqlite_adapter, 53, 300)
    versions = await _get_versions(sqlite_adapter, 53, 300)
    assert outcome == "applied"
    assert message.text == "edited"
    assert versions == []


@pytest.mark.asyncio
async def test_unchanged_reupsert_is_a_semantic_noop(sqlite_adapter):
    """Re-scanning an identical message changes nothing and records nothing
    (the fast path returns before taking the write lock)."""
    data = {
        "id": 54,
        "chat_id": 300,
        "date": datetime(2026, 6, 26, 13, 0),
        "text": "stable",
        "edit_date": datetime(2026, 6, 26, 13, 5),
    }
    await sqlite_adapter.insert_message(data, account_id=1)

    await sqlite_adapter.insert_message(dict(data), account_id=1)

    message = await _get_message(sqlite_adapter, 54, 300)
    versions = await _get_versions(sqlite_adapter, 54, 300)
    assert message.text == "stable"
    assert message.edit_date == datetime(2026, 6, 26, 13, 5)
    assert versions == []


# ---------------------------------------------------------------------------
# Deletion snapshots for the event webhook (#336)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_message_deleted_returns_pre_tombstone_snapshot(sqlite_adapter):
    """First mark returns is_deleted=0 (webhook fires); a re-mark returns 1
    (webhook skips); a never-archived id returns None. Tombstone semantics
    (coalesce, idempotent UPDATE) are covered by the tests above."""
    await sqlite_adapter.insert_message(
        {
            "id": 60,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 16, 0),
            "text": "doomed",
            "sender_id": 501,
            "sender_name": "Ana",
        },
        account_id=1,
    )

    first = await sqlite_adapter.mark_message_deleted(300, 60, datetime(2026, 6, 26, 16, 5), account_id=1)
    again = await sqlite_adapter.mark_message_deleted(300, 60, datetime(2026, 6, 26, 16, 10), account_id=1)
    missing = await sqlite_adapter.mark_message_deleted(300, 999, account_id=1)

    assert first == {
        "text": "doomed",
        "sender_id": 501,
        "sender_name": "Ana",
        "date": datetime(2026, 6, 26, 16, 0),
        "is_deleted": 0,
        "media_type": None,
    }
    assert again["is_deleted"] == 1
    assert again["text"] == "doomed"
    assert missing is None


@pytest.mark.asyncio
async def test_delete_message_returns_snapshot_including_media_type(sqlite_adapter):
    """Hard delete snapshots the row (text available WITHOUT soft mode) and
    the media type from the media table, then still removes everything."""
    await sqlite_adapter.insert_message(
        {
            "id": 61,
            "chat_id": 300,
            "date": datetime(2026, 6, 26, 17, 0),
            "text": "hard-deleted",
            "sender_id": 502,
            "sender_name": "Bo",
        },
        account_id=1,
    )
    await sqlite_adapter.insert_media(
        {"id": "300_61_photo", "message_id": 61, "chat_id": 300, "type": "photo"}, account_id=1
    )

    snapshot = await sqlite_adapter.delete_message(300, 61, account_id=1)
    missing = await sqlite_adapter.delete_message(300, 999, account_id=1)

    assert snapshot == {
        "text": "hard-deleted",
        "sender_id": 502,
        "sender_name": "Bo",
        "date": datetime(2026, 6, 26, 17, 0),
        "is_deleted": 0,
        "media_type": "photo",
    }
    assert missing is None
    async with sqlite_adapter.db_manager.async_session_factory() as session:
        assert await session.get(Message, (1, 300, 61)) is None


@pytest.mark.asyncio
async def test_deletion_snapshot_media_type_is_deterministic_across_rows(sqlite_adapter):
    """A message with several media rows must report the same media_type on
    every delivery — the snapshot orders by Media.id before LIMIT 1."""
    await sqlite_adapter.insert_message(
        {"id": 62, "chat_id": 300, "date": datetime(2026, 6, 26, 18, 0), "text": "album head"}, account_id=1
    )
    await sqlite_adapter.insert_media(
        {"id": "300_62_video", "message_id": 62, "chat_id": 300, "type": "video"}, account_id=1
    )
    await sqlite_adapter.insert_media(
        {"id": "300_62_photo", "message_id": 62, "chat_id": 300, "type": "photo"}, account_id=1
    )

    snapshot = await sqlite_adapter.mark_message_deleted(300, 62, datetime(2026, 6, 26, 18, 5), account_id=1)

    # Lexicographic Media.id order: "300_62_photo" < "300_62_video".
    assert snapshot["media_type"] == "photo"


@pytest.mark.asyncio
async def test_version_export_window_uses_the_export_contract(sqlite_adapter):
    """iter_message_versions_for_export windows with (>= from, < to) — the
    export contract — without touching the shared query's inclusive end_date
    that other callers own."""
    await sqlite_adapter.insert_message(
        {"id": 80, "chat_id": 300, "date": datetime(2026, 6, 28, 9, 0), "text": "v1"}, account_id=1
    )
    await sqlite_adapter.update_message_text(300, 80, "v2", datetime(2026, 6, 28, 10, 0), account_id=1)
    await sqlite_adapter.update_message_text(300, 80, "v3", datetime(2026, 6, 28, 11, 0), account_id=1)

    everything = [v async for v in sqlite_adapter.iter_message_versions_for_export(300, account_id=1)]
    assert len(everything) == 2

    windowed = [
        v
        async for v in sqlite_adapter.iter_message_versions_for_export(
            300, account_id=1, from_date=datetime(2026, 6, 28, 9, 30), to_date=datetime(2026, 6, 28, 10, 0)
        )
    ]
    # v1's version row carries the superseded text's date (9:00) -> below from;
    # v2's row (10:00) -> excluded by the EXCLUSIVE to bound.
    assert windowed == []

    windowed = [
        v
        async for v in sqlite_adapter.iter_message_versions_for_export(
            300, account_id=1, from_date=datetime(2026, 6, 28, 9, 0), to_date=datetime(2026, 6, 28, 10, 1)
        )
    ]
    assert [v["text"] for v in windowed] == ["v1", "v2"]
