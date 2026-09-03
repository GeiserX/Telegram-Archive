"""Global message-search backend tests."""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.db.adapter import ChatScope, DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Message
from src.web.global_search import search_messages_global


def _message(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_to_msg_id: int | None = None,
    reply_to_top_id: int | None = None,
) -> Message:
    return Message(
        id=message_id,
        chat_id=chat_id,
        sender_id=message_id,
        sender_name=f"User {message_id}",
        date=datetime(2026, 1, 1, 10, 0, message_id),
        text=text,
        account_id=1,
        reply_to_msg_id=reply_to_msg_id,
        reply_to_top_id=reply_to_top_id,
    )


@pytest_asyncio.fixture
async def adapter():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    manager = DatabaseManager.__new__(DatabaseManager)
    manager.engine = engine
    manager.database_url = "sqlite+aiosqlite://"
    manager._is_sqlite = True
    manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with manager.async_session_factory() as session:
        session.add_all(
            [
                Chat(id=-1001, type="channel", title="Chat A", account_id=1),
                Chat(id=-1002, type="channel", title="Chat B", account_id=1),
                _message(-1001, 1, "HAARP and microwave research"),
                _message(-1002, 2, "The same HAARP term in another chat", reply_to_msg_id=1, reply_to_top_id=42),
                _message(-1001, 3, "completely unrelated"),
            ]
        )
        await session.commit()

    result = DatabaseAdapter(manager)
    try:
        yield result
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_searches_across_all_visible_chats(adapter):
    rows, has_more = await search_messages_global(
        adapter,
        query="HAARP",
        scope=ChatScope.build(),
        limit=50,
        offset=0,
    )

    assert has_more is False
    assert [row["message_id"] for row in rows] == [2, 1]
    assert {row["chat"]["title"] for row in rows} == {"Chat A", "Chat B"}


@pytest.mark.asyncio
async def test_search_respects_chat_scope(adapter):
    rows, has_more = await search_messages_global(
        adapter,
        query="HAARP",
        scope=ChatScope.build(ids={-1001}),
        limit=50,
        offset=0,
    )

    assert has_more is False
    assert [row["message_id"] for row in rows] == [1]
    assert rows[0]["chat"]["title"] == "Chat A"


@pytest.mark.asyncio
async def test_search_paginates_and_preserves_topic_navigation_fields(adapter):
    rows, has_more = await search_messages_global(
        adapter,
        query="HAARP",
        scope=ChatScope.build(),
        limit=1,
        offset=0,
    )

    assert has_more is True
    assert len(rows) == 1
    assert rows[0]["reply_to_msg_id"] == 1
    assert rows[0]["reply_to_top_id"] == 42


@pytest.mark.asyncio
async def test_empty_scope_fails_closed(adapter):
    rows, has_more = await search_messages_global(
        adapter,
        query="HAARP",
        scope=ChatScope.build(refs=set()),
        limit=50,
        offset=0,
    )

    assert rows == []
    assert has_more is False


@pytest.mark.asyncio
async def test_punctuation_only_search_is_empty(adapter):
    rows, has_more = await search_messages_global(
        adapter,
        query="+++",
        scope=ChatScope.build(),
        limit=50,
        offset=0,
    )
    assert rows == []
    assert has_more is False
