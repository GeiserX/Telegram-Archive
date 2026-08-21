"""The gallery key query must be served by idx_media_gallery, not a sort.

Measured before this index existed: every gallery page fetched, joined and
sorted every media row in the chat to return 51 (43 ms first page, 494 ms on a
deep cursor at 120k rows, linear in chat size). These tests capture the exact
SQL the adapter emits against real SQLite and EXPLAIN it: the media access must
be an index seek on idx_media_gallery, never a full media scan. The tiny outer
sort of one hydrated page (<= limit+1 rows) is O(page) and allowed.
"""

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
from src.db.models import Base, Chat, Media, Message

CHAT_ID = -1001234567890
ROWS = 600  # enough that the planner never prefers a full scan


async def _build() -> tuple[DatabaseAdapter, object]:
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
        session.add(Chat(id=CHAT_ID, type="channel", title="Big Gallery"))
        base_date = datetime(2026, 1, 1, 10)
        for n in range(1, ROWS + 1):
            session.add(Message(id=n, chat_id=CHAT_ID, date=base_date + timedelta(minutes=n), text=None))
            session.add(
                Media(
                    id=f"{CHAT_ID}_{n}_photo",
                    message_id=n,
                    chat_id=CHAT_ID,
                    type="photo",
                    file_path=f"{CHAT_ID}/photo_{n}.jpg",
                    file_name=f"photo_{n}.jpg",
                    file_size=1000 + n,
                    mime_type="image/jpeg",
                    downloaded=1,
                )
            )
        await session.commit()

    return DatabaseAdapter(db_manager), engine


@pytest_asyncio.fixture
async def built():
    return await _build()


async def _explain_page_statements(adapter, engine, **page_kwargs) -> list[str]:
    """Run one page call, EXPLAIN the SQL that selects the page keys."""
    captured: list[tuple[str, tuple]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await adapter.get_media_paginated(CHAT_ID, **page_kwargs)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    page_sql = [(s, p) for s, p in captured if "page_media_id" in s]
    assert page_sql, "the page statement was not captured"
    plans = []
    async with engine.connect() as conn:
        for statement, parameters in page_sql:
            result = await conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
            plans.append(" | ".join(str(row[-1]) for row in result.fetchall()))
    return plans


async def test_first_page_seeks_the_gallery_index(built):
    adapter, engine = built
    for plan in await _explain_page_statements(adapter, engine, limit=50):
        assert "idx_media_gallery" in plan, f"gallery index unused: {plan}"
        assert "SCAN media" not in plan, f"full media scan: {plan}"


async def test_deep_cursor_page_stays_an_index_seek(built):
    adapter, engine = built
    # A cursor near the oldest row — the case measured at 494 ms pre-index.
    deep_cursor = f"{CHAT_ID}_20_photo"
    for plan in await _explain_page_statements(adapter, engine, limit=50, before_id=deep_cursor):
        assert "idx_media_gallery" in plan, f"gallery index unused: {plan}"
        assert "SCAN media" not in plan, f"full media scan: {plan}"


async def test_forward_cursor_page_stays_an_index_seek(built):
    adapter, engine = built
    for plan in await _explain_page_statements(adapter, engine, limit=50, after_id=f"{CHAT_ID}_20_photo"):
        assert "idx_media_gallery" in plan, f"gallery index unused: {plan}"
        assert "SCAN media" not in plan, f"full media scan: {plan}"
