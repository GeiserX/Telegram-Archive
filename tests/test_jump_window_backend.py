"""End-to-end window queries for jump-to-message (#213), real SQLite.

The v7.21.0 jump fetched `?before_id=<target+1>` alone, and the adapter only
honored before_id inside the before_date branch — the query silently returned
the latest page and every jump landed on the newest messages. These tests run
the real SQL so that contract can't regress silently again.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Message

CHAT_ID = -100
NEWEST_ID = 200


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

    adapter = DatabaseAdapter(db_manager)
    async with db_manager.async_session_factory() as session:
        base = datetime(2026, 1, 1, 12, 0, 0)
        for i in range(1, NEWEST_ID + 1):
            session.add(Message(id=i, chat_id=CHAT_ID, date=base + timedelta(minutes=i), text=f"m{i}"))
        await session.commit()

    yield adapter
    await engine.dispose()


class TestJumpWindowQueries:
    async def test_lone_before_id_returns_history_window_not_latest_page(self, adapter):
        target = 60
        rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, before_id=target + 1, limit=50)
        ids = [r["id"] for r in rows]

        # Exclusive bound: the target itself is the newest row of the window.
        assert ids[0] == target
        assert ids == list(range(target, target - 50, -1))
        assert NEWEST_ID not in ids

    async def test_after_id_returns_forward_context_newest_first(self, adapter):
        rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, after_id=60, limit=50)
        ids = [r["id"] for r in rows]

        # The LIMIT takes the rows closest to the target (61..110), and the
        # response keeps the newest-first contract of every other mode.
        assert ids == list(range(110, 60, -1))

    async def test_after_id_short_page_marks_the_live_tail(self, adapter):
        rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, after_id=190, limit=50)
        ids = [r["id"] for r in rows]

        assert ids == list(range(NEWEST_ID, 190, -1))
        # The viewer relies on a short page to detect that the window already
        # reaches the newest message (stays in live/tail mode).
        assert len(rows) < 50

    async def test_after_id_is_chat_scoped(self, adapter):
        async with adapter.db_manager.async_session_factory() as session:
            session.add(Message(id=500, chat_id=-999, date=datetime(2026, 2, 1), text="other chat"))
            await session.commit()

        rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, after_id=190, limit=50)
        assert all(r["chat_id"] == CHAT_ID for r in rows)
        assert 500 not in [r["id"] for r in rows]
