"""Real-SQLite regressions for message-date availability queries."""

import os
import sys
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Message

CHAT_ID = 100
OTHER_CHAT_ID = 200


def _day_range(day: str) -> tuple[str, datetime, datetime]:
    start = datetime.fromisoformat(day)
    return day, start, start + timedelta(days=1)


@pytest_asyncio.fixture
async def adapter():
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

    yield DatabaseAdapter(db_manager)
    await engine.dispose()


async def _seed(adapter: DatabaseAdapter, *messages: Message) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        session.add_all(messages)
        await session.commit()


async def test_returns_sorted_sparse_dates_once_with_one_statement(adapter):
    await _seed(
        adapter,
        Message(id=1, chat_id=CHAT_ID, date=datetime(2026, 1, 3, 12)),
        Message(id=2, chat_id=CHAT_ID, date=datetime(2026, 1, 1, 12)),
        Message(id=1, chat_id=OTHER_CHAT_ID, date=datetime(2026, 1, 2, 12)),
    )
    ranges = [
        _day_range("2026-01-03"),
        _day_range("2026-01-02"),
        _day_range("2026-01-01"),
        _day_range("2026-01-03"),
    ]
    statements = []

    def record_statement(*_args):
        statements.append(1)

    event.listen(adapter.db_manager.engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        result = await adapter.get_message_dates(CHAT_ID, ranges)
    finally:
        event.remove(adapter.db_manager.engine.sync_engine, "before_cursor_execute", record_statement)

    assert result == ["2026-01-01", "2026-01-03"]
    assert len(statements) == 1


async def test_topic_filters_include_general_null_and_explicit_topics(adapter):
    await _seed(
        adapter,
        Message(id=1, chat_id=CHAT_ID, date=datetime(2026, 2, 1, 12), reply_to_top_id=None),
        Message(id=2, chat_id=CHAT_ID, date=datetime(2026, 2, 2, 12), reply_to_top_id=1),
        Message(id=3, chat_id=CHAT_ID, date=datetime(2026, 2, 3, 12), reply_to_top_id=7),
        Message(id=4, chat_id=CHAT_ID, date=datetime(2026, 2, 4, 12), reply_to_top_id=8),
    )
    ranges = [_day_range(f"2026-02-0{day}") for day in range(1, 5)]

    assert await adapter.get_message_dates(CHAT_ID, ranges, topic_id=1) == [
        "2026-02-01",
        "2026-02-02",
    ]
    assert await adapter.get_message_dates(CHAT_ID, ranges, topic_id=7) == ["2026-02-03"]


async def test_deleted_and_service_rows_make_days_available(adapter):
    await _seed(
        adapter,
        Message(
            id=1,
            chat_id=CHAT_ID,
            date=datetime(2026, 3, 1, 12),
            text="",
            is_deleted=1,
            deleted_at=datetime(2026, 3, 2),
        ),
        Message(
            id=2,
            chat_id=CHAT_ID,
            date=datetime(2026, 3, 2, 12),
            sender_id=None,
            text=None,
        ),
    )

    result = await adapter.get_message_dates(
        CHAT_ID,
        [_day_range("2026-03-01"), _day_range("2026-03-02")],
    )

    assert result == ["2026-03-01", "2026-03-02"]


async def test_uses_utc_half_open_boundaries(adapter):
    boundary = datetime(2026, 4, 2)
    await _seed(
        adapter,
        Message(id=1, chat_id=CHAT_ID, date=boundary),
    )

    result = await adapter.get_message_dates(
        CHAT_ID,
        [
            ("2026-04-01", datetime(2026, 4, 1), boundary),
            ("2026-04-02", boundary, datetime(2026, 4, 3)),
        ],
    )

    assert result == ["2026-04-02"]
