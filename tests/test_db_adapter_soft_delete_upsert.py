from datetime import datetime

import pytest

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Message


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
        message = await session.get(Message, (message_id, chat_id))
        assert message is not None
        return message


@pytest.mark.asyncio
async def test_insert_message_upsert_preserves_soft_delete_marker(sqlite_adapter):
    deleted_at = datetime(2026, 6, 25, 10, 30)

    await sqlite_adapter.insert_message(
        {
            "id": 1,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 10, 0),
            "text": "original",
        }
    )
    await sqlite_adapter.mark_message_deleted(100, 1, deleted_at)

    await sqlite_adapter.insert_message(
        {
            "id": 1,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 10, 0),
            "text": "reprocessed",
        }
    )

    message = await _get_message(sqlite_adapter, 1, 100)
    assert message.text == "reprocessed"
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
        ]
    )
    await sqlite_adapter.mark_message_deleted(100, 2, deleted_at)

    await sqlite_adapter.insert_messages_batch(
        [
            {
                "id": 2,
                "chat_id": 100,
                "date": datetime(2026, 6, 25, 11, 0),
                "text": "reprocessed",
            }
        ]
    )

    message = await _get_message(sqlite_adapter, 2, 100)
    assert message.text == "reprocessed"
    assert message.is_deleted == 1
    assert message.deleted_at == deleted_at


@pytest.mark.asyncio
async def test_fresh_insert_not_marked_deleted(sqlite_adapter):
    """A brand-new message inserts as not-deleted with a null deleted_at."""
    await sqlite_adapter.insert_message(
        {"id": 3, "chat_id": 100, "date": datetime(2026, 6, 25, 12, 0), "text": "hello"}
    )

    message = await _get_message(sqlite_adapter, 3, 100)
    assert message.is_deleted == 0
    assert message.deleted_at is None


@pytest.mark.asyncio
async def test_upsert_with_is_deleted_and_timestamp_sets_marker(sqlite_adapter):
    """An upsert whose payload carries is_deleted + deleted_at sets both on conflict."""
    deleted_at = datetime(2026, 6, 25, 13, 30)

    await sqlite_adapter.insert_message(
        {"id": 4, "chat_id": 100, "date": datetime(2026, 6, 25, 13, 0), "text": "original"}
    )
    await sqlite_adapter.insert_message(
        {
            "id": 4,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 13, 0),
            "text": "now deleted",
            "is_deleted": 1,
            "deleted_at": deleted_at,
        }
    )

    message = await _get_message(sqlite_adapter, 4, 100)
    assert message.is_deleted == 1
    assert message.deleted_at == deleted_at


@pytest.mark.asyncio
async def test_upsert_with_is_deleted_no_timestamp_preserves_existing(sqlite_adapter):
    """An upsert with is_deleted=1 but no deleted_at keeps the existing timestamp."""
    first_deleted_at = datetime(2026, 6, 25, 14, 30)

    await sqlite_adapter.insert_message(
        {"id": 5, "chat_id": 100, "date": datetime(2026, 6, 25, 14, 0), "text": "original"}
    )
    await sqlite_adapter.mark_message_deleted(100, 5, first_deleted_at)
    await sqlite_adapter.insert_message(
        {
            "id": 5,
            "chat_id": 100,
            "date": datetime(2026, 6, 25, 14, 0),
            "text": "reprocessed",
            "is_deleted": 1,
        }
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
        {"id": 6, "chat_id": 100, "date": datetime(2026, 6, 25, 14, 0), "text": "original"}
    )
    await sqlite_adapter.mark_message_deleted(100, 6, first)
    await sqlite_adapter.mark_message_deleted(100, 6, second)

    message = await _get_message(sqlite_adapter, 6, 100)
    assert message.is_deleted == 1
    assert message.deleted_at == first


@pytest.mark.asyncio
async def test_mark_message_deleted_defaults_timestamp_when_none(sqlite_adapter):
    """Omitting deleted_at falls back to a server-generated timestamp."""
    await sqlite_adapter.insert_message(
        {"id": 7, "chat_id": 100, "date": datetime(2026, 6, 25, 14, 0), "text": "original"}
    )
    await sqlite_adapter.mark_message_deleted(100, 7)

    message = await _get_message(sqlite_adapter, 7, 100)
    assert message.is_deleted == 1
    assert message.deleted_at is not None


@pytest.mark.asyncio
async def test_get_messages_sync_data_excludes_soft_deleted(sqlite_adapter):
    """Soft-deleted rows are excluded from the sync set so they aren't re-checked."""
    await sqlite_adapter.insert_message(
        {"id": 10, "chat_id": 200, "date": datetime(2026, 6, 25, 17, 0), "text": "live"}
    )
    await sqlite_adapter.insert_message(
        {"id": 11, "chat_id": 200, "date": datetime(2026, 6, 25, 17, 1), "text": "to delete"}
    )
    await sqlite_adapter.mark_message_deleted(200, 11)

    sync_data = await sqlite_adapter.get_messages_sync_data(200)
    assert set(sync_data.keys()) == {10}
