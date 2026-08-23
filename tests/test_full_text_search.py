"""Full-text search replaces the leading-wildcard ILIKE scan (9t6.11.1).

ILIKE '%q%' cannot use any index, so every search scanned the chat. With
migration 028 SQLite gets an external-content FTS5 table synced by triggers
and PostgreSQL a generated tsvector + GIN; the adapter probes for the layer
once and swaps ONLY the text predicate — pagination, scoping and topic
filters are untouched, and databases without the layer (or punctuation-only
searches that tokenize to nothing) keep the old ILIKE substring behavior.
Semantics on the indexed path are official-app word-prefix AND: every word
of the query must prefix-match a word of the message.
"""

import os
import sys
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.fts import (
    SQLITE_CREATE_FTS,
    SQLITE_REBUILD,
    SQLITE_TRIGGERS,
    fts_match_query,
    pg_tsquery,
)
from src.db.models import Base, Chat, Message

CHAT_ID = -1001


def _msg(msg_id: int, msg_text: str) -> Message:
    return Message(
        id=msg_id,
        chat_id=CHAT_ID,
        sender_id=None,
        date=datetime(2026, 1, 1, 10, 0, msg_id % 60),
        text=msg_text,
        account_id=1,
    )


async def _make_adapter(with_fts: bool) -> DatabaseAdapter:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # create_all installs the FTS layer via the models' after_create
        # listener — the same DDL migration 028 runs on upgraded databases.
        await conn.run_sync(Base.metadata.create_all)
        if not with_fts:
            # A database whose migration has not run yet (e.g. an exotic
            # SQLite without FTS5): strip the layer to prove the fallback.
            for trigger_name in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
                await conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")
            await conn.exec_driver_sql("DROP TABLE IF EXISTS messages_fts")

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="channel", title="Chat A"))
        session.add(_msg(1, "hola mundo cruel"))
        session.add(_msg(2, "Café con leche"))
        session.add(_msg(3, "unrelated content entirely"))
        await session.commit()

    return DatabaseAdapter(db_manager)


@pytest_asyncio.fixture
async def adapter():
    return await _make_adapter(with_fts=True)


@pytest_asyncio.fixture
async def adapter_without_fts():
    return await _make_adapter(with_fts=False)


async def _search_ids(adapter_, query: str) -> list[int]:
    rows = await adapter_.get_messages_paginated(CHAT_ID, limit=50, search=query, account_id=1)
    return sorted(m["id"] for m in rows)


# ---------------------------------------------------------------------------
# Indexed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_word_and_prefix_matches(adapter):
    assert await _search_ids(adapter, "mundo") == [1]
    assert await _search_ids(adapter, "mun") == [1]  # word prefix
    assert await _search_ids(adapter, "HOLA") == [1]  # case-insensitive
    assert await _search_ids(adapter, "hola cruel") == [1]  # all words required
    assert await _search_ids(adapter, "hola unrelated") == []


@pytest.mark.asyncio
async def test_diacritics_fold(adapter):
    """remove_diacritics 2: searching 'cafe' finds 'Café'."""
    assert await _search_ids(adapter, "cafe") == [2]


@pytest.mark.asyncio
async def test_prefix_not_substring(adapter):
    """The indexed path is word-PREFIX, not substring — the official-app trade.

    'undo' is inside 'mundo' but no word starts with it; ILIKE used to match.
    This is deliberate (the bead's goal is official-client search semantics).
    """
    assert await _search_ids(adapter, "undo") == []


@pytest.mark.asyncio
async def test_edits_and_deletes_stay_in_sync(adapter):
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(sa_text("UPDATE messages SET text = 'texto nuevo' WHERE id = 1"))
        await session.execute(sa_text("DELETE FROM messages WHERE id = 3"))
        await session.commit()
    assert await _search_ids(adapter, "hola") == []  # old text un-indexed
    assert await _search_ids(adapter, "nuevo") == [1]  # new text indexed
    assert await _search_ids(adapter, "unrelated") == []  # deleted row gone


@pytest.mark.asyncio
async def test_operator_injection_is_inert(adapter):
    """FTS5 syntax in user input must neither crash nor widen the match."""
    for hostile in ('mundo" OR "cruel', "mundo OR unrelated", 'x" NEAR y --', "(((", "*"):
        rows = await adapter.get_messages_paginated(CHAT_ID, limit=50, search=hostile, account_id=1)
        assert isinstance(rows, list)
    # OR is a quoted term, not an operator: message must contain a word
    # starting with "or" — none does.
    assert await _search_ids(adapter, "mundo OR unrelated") == []


@pytest.mark.asyncio
async def test_query_plan_uses_the_index(adapter):
    """A check that cannot fail is not a check: the plan must NAME messages_fts."""
    async with adapter.db_manager.async_session_factory() as session:
        fts_predicate = await adapter._text_search_predicate(session, "mundo")
        assert fts_predicate is not None
        compiled = str(fts_predicate)
        assert "messages_fts" in compiled and "MATCH" in compiled
        plan = await session.execute(
            sa_text(
                "EXPLAIN QUERY PLAN SELECT id FROM messages WHERE "
                "messages.rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH :q)"
            ).bindparams(q='"mundo"*')
        )
        plan_text = " ".join(str(row) for row in plan.fetchall())
        assert "messages_fts" in plan_text


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_the_layer_search_keeps_substring_semantics(adapter_without_fts):
    assert await _search_ids(adapter_without_fts, "undo") == [1]  # ILIKE substring
    assert await _search_ids(adapter_without_fts, "HOLA") == [1]
    assert adapter_without_fts._fts_ready_cache is False


@pytest.mark.asyncio
async def test_punctuation_only_search_falls_back_even_with_the_layer(adapter):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_msg(9, "signs +++ only"))
        await session.commit()
    assert await _search_ids(adapter, "+++") == [9]  # ILIKE literal match


# ---------------------------------------------------------------------------
# Query builders + migration idempotence
# ---------------------------------------------------------------------------


def test_fts_match_query_shapes():
    assert fts_match_query("hola mundo") == '"hola"* "mundo"*'
    assert fts_match_query("covid-19") == '"covid"* "19"*'
    assert fts_match_query('a "b" OR (c)') == '"a"* "b"* "OR"* "c"*'
    assert fts_match_query("$$$") is None
    assert fts_match_query("") is None


def test_pg_tsquery_shapes():
    assert pg_tsquery("hola mundo") == "'hola':* & 'mundo':*"
    assert pg_tsquery("covid-19") == "'covid':* & '19':*"
    assert pg_tsquery("!!!") is None


@pytest.mark.asyncio
async def test_create_sequence_is_idempotent():
    """Running migration 028's SQLite sequence twice must be a no-op."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for _ in range(2):
            await conn.exec_driver_sql(SQLITE_CREATE_FTS)
            for trigger_sql in SQLITE_TRIGGERS:
                await conn.exec_driver_sql(trigger_sql)
        await conn.exec_driver_sql(SQLITE_REBUILD)
        triggers = await conn.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'messages_fts_%'"
        )
        assert triggers.first()[0] == 3
