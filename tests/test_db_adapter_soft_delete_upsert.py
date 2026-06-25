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
