"""Regression test: forum-topic message filter must include General (topic_id=1).

Bug: get_forum_topics() coalesces NULL reply_to_top_id to 1 when counting
General messages (pre-v6.2.0 messages and pre-forum messages all have NULL),
but get_messages_paginated() used a strict equality filter
(``reply_to_top_id == topic_id``), so ``?topic_id=1`` returned zero rows
even when the sidebar showed "67 messages".

Fix: apply the same ``coalesce(reply_to_top_id, 1) == topic_id`` in the
messages query so General sees its NULL messages while non-General topics
still filter strictly.
"""

import os
import sys
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Message


async def _make_adapter_with_messages():
    """Spin up an in-memory SQLite DB seeded with a forum chat + mixed-topic messages."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    chat_id = -1002010842190
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=chat_id, type="group", title="Forum Chat", is_forum=1))
        rows = [
            # (id, date, text, reply_to_top_id)
            (1, datetime(2024, 1, 1, 10), "pre-forum general #1", None),
            (2, datetime(2024, 1, 2, 10), "pre-forum general #2", None),
            (3, datetime(2024, 3, 1, 10), "sexy topic msg", 47),
            (4, datetime(2024, 3, 2, 10), "another sexy msg", 47),
            (5, datetime(2024, 4, 1, 10), "photos topic", 144),
        ]
        for mid, dt, body, top in rows:
            session.add(Message(id=mid, chat_id=chat_id, date=dt, text=body, reply_to_top_id=top))
        await session.commit()

    return DatabaseAdapter(db_manager), engine, chat_id


@pytest.mark.asyncio
async def test_general_topic_includes_null_reply_to_top_id():
    """topic_id=1 (General) must match messages whose reply_to_top_id IS NULL."""
    adapter, engine, chat_id = await _make_adapter_with_messages()
    try:
        messages = await adapter.get_messages_paginated(chat_id=chat_id, topic_id=1)
        ids = sorted(m["id"] for m in messages)
        assert ids == [1, 2], f"General (topic_id=1) must return the 2 NULL-top messages, got {ids}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_general_topic_filters_strictly():
    """Non-General topic IDs must still filter by strict equality on reply_to_top_id."""
    adapter, engine, chat_id = await _make_adapter_with_messages()
    try:
        sexy = await adapter.get_messages_paginated(chat_id=chat_id, topic_id=47)
        assert sorted(m["id"] for m in sexy) == [3, 4]

        photos = await adapter.get_messages_paginated(chat_id=chat_id, topic_id=144)
        assert sorted(m["id"] for m in photos) == [5]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_topic_filter_returns_all():
    """Without topic_id, all messages in the chat must be returned regardless of reply_to_top_id."""
    adapter, engine, chat_id = await _make_adapter_with_messages()
    try:
        all_msgs = await adapter.get_messages_paginated(chat_id=chat_id)
        assert sorted(m["id"] for m in all_msgs) == [1, 2, 3, 4, 5]
    finally:
        await engine.dispose()
