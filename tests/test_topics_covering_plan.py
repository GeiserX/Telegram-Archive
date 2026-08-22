"""The topic sidebar's aggregate must be a covering index scan, not a heap walk.

Measured before this rewrite: GET /api/chats/{id}/topics grouped every message
row of the chat on coalesce(reply_to_top_id, 1) — a temp b-tree plus a heap
lookup per row, raw_data included (17.4 ms at 60k rows, linear in chat size).
These tests capture the exact aggregate SQL the adapter emits against real
SQLite and EXPLAIN it: the aggregate must run off idx_messages_topic as a
COVERING index, and the Python-side fold of the NULL bucket into the General
topic must reproduce the old SQL's numbers and ordering exactly.
"""

import os
import sys
from datetime import datetime

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, ForumTopic, Message

CHAT_ID = -1004242


async def _build():
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

    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="channel", title="Forum", is_forum=1))
        session.add(ForumTopic(chat_id=CHAT_ID, id=1, title="General", is_pinned=0))
        session.add(ForumTopic(chat_id=CHAT_ID, id=7, title="Busy", is_pinned=0))
        session.add(ForumTopic(chat_id=CHAT_ID, id=9, title="Pinned", is_pinned=1))
        session.add(ForumTopic(chat_id=CHAT_ID, id=11, title="Silent", is_pinned=0))
        rows = [
            # Pre-forum history: NULL topic, folds into General (id=1).
            (101, None, datetime(2026, 1, 1, 10)),
            (102, None, datetime(2026, 1, 2, 10)),
            # Explicit General-topic message, newer than the NULL bucket.
            (103, 1, datetime(2026, 1, 5, 10)),
            # Busy topic — the newest traffic in the chat.
            (104, 7, datetime(2026, 2, 1, 10)),
            (105, 7, datetime(2026, 2, 2, 10)),
            (106, 7, datetime(2026, 2, 3, 10)),
            # Pinned topic, older traffic.
            (107, 9, datetime(2026, 1, 10, 10)),
        ]
        for message_id, topic_id, date in rows:
            session.add(Message(id=message_id, chat_id=CHAT_ID, reply_to_top_id=topic_id, date=date, text=None))
        await session.commit()

    return DatabaseAdapter(db_manager), engine


@pytest_asyncio.fixture
async def built():
    return await _build()


async def test_aggregate_is_a_covering_index_scan(built):
    adapter, engine = built
    captured: list[tuple[str, tuple]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await adapter.get_forum_topics(CHAT_ID)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    agg_sql = [(s, p) for s, p in captured if "group by" in s.lower()]
    assert agg_sql, "the aggregate statement was not captured"
    async with engine.connect() as conn:
        for statement, parameters in agg_sql:
            result = await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
            plan = " | ".join(str(row[-1]) for row in result.fetchall())
            assert "COVERING INDEX idx_messages_topic" in plan, f"not covering: {plan}"
            assert "TEMP B-TREE" not in plan.upper(), f"temp b-tree grouping: {plan}"


async def test_null_bucket_folds_into_general_and_order_is_preserved(built):
    adapter, _engine = built
    topics = await adapter.get_forum_topics(CHAT_ID)

    by_id = {topic["id"]: topic for topic in topics}
    # 2 pre-forum NULL rows + 1 explicit General row, newest date of the three.
    assert by_id[1]["message_count"] == 3
    assert by_id[1]["last_message_date"] == datetime(2026, 1, 5, 10)
    assert by_id[7]["message_count"] == 3
    assert by_id[7]["last_message_date"] == datetime(2026, 2, 3, 10)
    assert by_id[9]["message_count"] == 1
    assert by_id[11]["message_count"] == 0
    assert by_id[11]["last_message_date"] is None

    # Same order the SQL produced: pinned first, then newest last-message
    # first, never-posted topics at the end.
    assert [topic["id"] for topic in topics] == [9, 7, 1, 11]
