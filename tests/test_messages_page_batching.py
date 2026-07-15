"""Real-SQLite regression tests for the get_messages_paginated batching fix.

Before this fix, get_messages_paginated's per-message loop awaited
get_reactions() once per row and issued a reply-text fallback SELECT per reply
row -- up to ~100 sequential round-trips per 50-row page. These tests pin the
response shape (so the batched queries produce byte-identical output to the
old per-row calls) and pin the query count (so the N+1 pattern can't regress
silently), plus cover the new get_pending_media_downloads(limit=...) bound.
"""

import logging
import os
import sys
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Media, Message, Reaction

CHAT_ID = -500
LONG_REPLY_TEXT = "L" * 150


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


async def _seed_page(adapter):
    """60 messages (ids 1-60); a 50-row newest-first page covers ids 11-60.

    - Message 6 carries a >100 char body so the reply-truncation rule is exercised.
    - Message 12 replies to message 3 (short text) with reply_to_text unset.
    - Message 44 replies to message 6 (long text) with reply_to_text unset.
    - Messages 20 and 35 carry reactions across multiple emoji/users.
    """
    async with adapter.db_manager.async_session_factory() as session:
        base = datetime(2026, 1, 1, 12, 0, 0)
        for i in range(1, 61):
            text = LONG_REPLY_TEXT if i == 6 else f"message {i}"
            reply_to_msg_id = {12: 3, 44: 6}.get(i)
            session.add(
                Message(
                    id=i,
                    chat_id=CHAT_ID,
                    date=base + timedelta(minutes=i),
                    text=text,
                    reply_to_msg_id=reply_to_msg_id,
                    reply_to_text=None,
                )
            )
        await session.commit()

        session.add_all(
            [
                Reaction(message_id=20, chat_id=CHAT_ID, emoji="thumbsup", user_id=1001, count=1),
                Reaction(message_id=20, chat_id=CHAT_ID, emoji="thumbsup", user_id=1002, count=1),
                Reaction(message_id=20, chat_id=CHAT_ID, emoji="heart", user_id=1003, count=1),
                Reaction(message_id=35, chat_id=CHAT_ID, emoji="fire", user_id=1004, count=2),
            ]
        )
        await session.commit()


class TestMessagesPageBatchingShape:
    @pytest.mark.asyncio
    async def test_batched_page_matches_expected_reactions_and_replies(self, adapter):
        await _seed_page(adapter)

        rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, limit=50)
        by_id = {r["id"]: r for r in rows}

        assert len(rows) == 50
        assert set(by_id) == set(range(11, 61))

        # Reactions: grouped by emoji, counts summed, user_ids collected.
        msg20 = by_id[20]
        reactions_by_emoji = {r["emoji"]: r for r in msg20["reactions"]}
        assert reactions_by_emoji["thumbsup"]["count"] == 2
        assert set(reactions_by_emoji["thumbsup"]["user_ids"]) == {1001, 1002}
        assert reactions_by_emoji["heart"]["count"] == 1
        assert reactions_by_emoji["heart"]["user_ids"] == [1003]

        msg35 = by_id[35]
        reactions_by_emoji_35 = {r["emoji"]: r for r in msg35["reactions"]}
        assert reactions_by_emoji_35["fire"]["count"] == 2
        assert reactions_by_emoji_35["fire"]["user_ids"] == [1004]

        # Messages with no reactions still get an empty (not missing) list.
        assert by_id[11]["reactions"] == []

        # Reply-text backfill: short text copied verbatim, long text truncated to 100.
        assert by_id[12]["reply_to_text"] == "message 3"
        assert by_id[44]["reply_to_text"] == LONG_REPLY_TEXT[:100]
        assert len(by_id[44]["reply_to_text"]) == 100

        # version_count is still populated (untouched by this fix, but part of
        # the same per-message assembly loop -- guards against a regression there).
        assert by_id[20]["version_count"] == 0

    @pytest.mark.asyncio
    async def test_page_fetch_issues_constant_number_of_queries(self, adapter):
        await _seed_page(adapter)

        statements = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sync_engine = adapter.db_manager.engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _capture)
        try:
            rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, limit=50)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _capture)

        assert len(rows) == 50
        select_count = sum(1 for s in statements if s.strip().upper().startswith("SELECT"))
        # Main page query + version-count query + batched reply-text query +
        # batched reactions query == 4. Anything approaching 50+ would mean the
        # N+1 per-row pattern (get_reactions/reply SELECT per row) came back.
        assert select_count <= 6, f"expected a small constant query count, got {select_count}: {statements}"


class TestPendingMediaDownloadsLimit:
    async def _add_media(self, adapter, media_id, attempts):
        async with adapter.db_manager.async_session_factory() as session:
            session.add(
                Media(
                    id=media_id,
                    message_id=1,
                    chat_id=CHAT_ID,
                    type="document",
                    downloaded=0,
                    download_attempts=attempts,
                )
            )
            await session.commit()

    @pytest.mark.asyncio
    async def test_honors_limit_and_deterministic_ordering(self, adapter):
        # Inserted out of (attempts, id) order to prove the ORDER BY drives results.
        await self._add_media(adapter, "c", attempts=2)
        await self._add_media(adapter, "a", attempts=0)
        await self._add_media(adapter, "b", attempts=0)
        await self._add_media(adapter, "d", attempts=1)

        limited = await adapter.get_pending_media_downloads(limit=2)
        assert [m["id"] for m in limited] == ["a", "b"]

        unlimited = await adapter.get_pending_media_downloads(limit=None)
        assert [m["id"] for m in unlimited] == ["a", "b", "d", "c"]

    @pytest.mark.asyncio
    async def test_default_limit_is_bounded(self, adapter):
        for i in range(5):
            await self._add_media(adapter, f"m{i}", attempts=0)

        # Default limit=1000 is well above 5 rows, so nothing gets truncated.
        pending = await adapter.get_pending_media_downloads()
        assert len(pending) == 5

    @pytest.mark.asyncio
    async def test_logs_only_when_truncation_actually_happens(self, adapter, caplog):
        for i in range(5):
            await self._add_media(adapter, f"m{i}", attempts=0)

        with caplog.at_level(logging.INFO, logger="src.db.adapter"):
            limited = await adapter.get_pending_media_downloads(limit=3)
        assert len(limited) == 3
        assert any("media retry: processing 3 of 5 pending" in r.getMessage() for r in caplog.records)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="src.db.adapter"):
            not_truncated = await adapter.get_pending_media_downloads(limit=5)
        assert len(not_truncated) == 5
        assert not any("media retry" in r.getMessage() for r in caplog.records)
