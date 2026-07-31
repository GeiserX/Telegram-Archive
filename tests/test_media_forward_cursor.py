"""Forward (``after_id``) media pagination — #266.

The audio queue must be able to extend FORWARD in time on demand. Without a
forward cursor it had to pre-collect every item newer than the playing track,
costing pages proportional to depth.

Everything here runs against real in-memory SQLite so the actual SQL (cursor
predicate + ORDER BY) is exercised, and uses production-format composite media
ids (``f"{chat_id}_{msg_id}_{type}"``) — toy ids hide the string-vs-numeric
ordering trap that #257 was about.
"""

import os
import sys
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message, User

CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210

# Deliberately spans a lexical/numeric divergence (8, 9, 89, 98, 99, 100...) and
# ties several voice notes onto shared timestamps so the tiebreak is exercised.
MESSAGE_IDS = [8, 9, 89, 98, 99, 100, 700, 701, 3150, 3350, 3653, 3739]


def _media_id(chat_id: int, msg_id: int, media_type: str = "voice") -> str:
    """Production composite media id."""
    return f"{chat_id}_{msg_id}_{media_type}"


async def _build_adapter() -> DatabaseAdapter:
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
        session.add(Chat(id=CHAT_ID, type="channel", title="Voice Chat"))
        session.add(Chat(id=OTHER_CHAT_ID, type="channel", title="Other Chat"))
        session.add(User(id=100, first_name="Alice", last_name="Smith", username="alice"))

        # Three timestamps only, so most rows tie and the (message_id, id)
        # tiebreak decides the order.
        dates = {
            8: datetime(2026, 1, 1, 10),
            9: datetime(2026, 1, 1, 10),
            89: datetime(2026, 1, 1, 10),
            98: datetime(2026, 1, 1, 10),
            99: datetime(2026, 1, 1, 10),
            100: datetime(2026, 1, 1, 10),
            700: datetime(2026, 1, 2, 10),
            701: datetime(2026, 1, 2, 10),
            3150: datetime(2026, 1, 3, 10),
            3350: datetime(2026, 1, 3, 10),
            3653: datetime(2026, 1, 3, 10),
            3739: datetime(2026, 1, 3, 10),
        }
        for msg_id in MESSAGE_IDS:
            session.add(Message(id=msg_id, chat_id=CHAT_ID, sender_id=100, date=dates[msg_id], text=None))
            session.add(
                Media(
                    id=_media_id(CHAT_ID, msg_id),
                    message_id=msg_id,
                    chat_id=CHAT_ID,
                    type="voice",
                    file_path=f"{CHAT_ID}/voice_{msg_id}.ogg",
                    file_name=f"voice_{msg_id}.ogg",
                    file_size=1000 + msg_id,
                    mime_type="audio/ogg",
                    duration=5,
                    downloaded=1,
                )
            )

        # A foreign chat's row, used for the cross-chat cursor test.
        session.add(Message(id=555, chat_id=OTHER_CHAT_ID, sender_id=100, date=datetime(2026, 6, 1, 10)))
        session.add(
            Media(
                id=_media_id(OTHER_CHAT_ID, 555),
                message_id=555,
                chat_id=OTHER_CHAT_ID,
                type="voice",
                file_path=f"{OTHER_CHAT_ID}/voice_555.ogg",
                file_name="voice_555.ogg",
                file_size=999,
                mime_type="audio/ogg",
                downloaded=1,
            )
        )
        await session.commit()

    return DatabaseAdapter(db_manager)


@pytest_asyncio.fixture
async def adapter():
    return await _build_adapter()


async def _walk_forward(adapter: DatabaseAdapter, page_size: int) -> list[dict]:
    """Page forward from the oldest row to exhaustion."""
    first = await adapter.get_media_paginated(CHAT_ID, limit=1_000)
    oldest = first["items"][-1]
    collected = [oldest]
    cursor = oldest["id"]
    while True:
        page = await adapter.get_media_paginated(CHAT_ID, limit=page_size, after_id=cursor)
        collected.extend(page["items"])
        if not page["has_more"]:
            break
        assert page["items"], "has_more=True with an empty page would loop forever"
        cursor = page["items"][-1]["id"]
    return collected


async def _walk_backward(adapter: DatabaseAdapter, page_size: int) -> list[dict]:
    """Page backward from the newest row to exhaustion."""
    page = await adapter.get_media_paginated(CHAT_ID, limit=page_size)
    collected = list(page["items"])
    while page["has_more"]:
        cursor = page["items"][-1]["id"]
        page = await adapter.get_media_paginated(CHAT_ID, limit=page_size, before_id=cursor)
        collected.extend(page["items"])
    return collected


class TestForwardWalk:
    async def test_forward_walk_returns_every_row_exactly_once(self, adapter):
        collected = await _walk_forward(adapter, page_size=3)
        ids = [item["id"] for item in collected]
        assert len(ids) == len(MESSAGE_IDS)
        assert len(set(ids)) == len(ids), "a forward walk must not duplicate rows"
        assert set(ids) == {_media_id(CHAT_ID, m) for m in MESSAGE_IDS}, "no row may be skipped"

    async def test_forward_walk_is_non_decreasing_by_date(self, adapter):
        collected = await _walk_forward(adapter, page_size=5)
        dates = [item["message_date"] for item in collected]
        assert dates == sorted(dates)

    async def test_forward_ties_break_numerically_by_message_id(self, adapter):
        """#257's bug in the other direction: composite ids are strings, so a
        lexical tiebreak would order 8, 89, 9, 98, 99 instead of 8, 9, 89, 98, 99."""
        collected = await _walk_forward(adapter, page_size=4)
        same_day = [item["message_id"] for item in collected if item["message_date"].startswith("2026-01-01")]
        assert same_day == [8, 9, 89, 98, 99, 100]

    async def test_forward_page_is_ordered_oldest_first(self, adapter):
        page = await adapter.get_media_paginated(CHAT_ID, limit=4, after_id=_media_id(CHAT_ID, 8))
        assert [item["message_id"] for item in page["items"]] == [9, 89, 98, 99]
        assert page["has_more"] is True

    async def test_has_more_false_on_the_last_forward_page(self, adapter):
        page = await adapter.get_media_paginated(CHAT_ID, limit=50, after_id=_media_id(CHAT_ID, 3653))
        assert [item["message_id"] for item in page["items"]] == [3739]
        assert page["has_more"] is False

    async def test_newest_row_has_no_forward_page(self, adapter):
        page = await adapter.get_media_paginated(CHAT_ID, limit=50, after_id=_media_id(CHAT_ID, 3739))
        assert page == {"items": [], "has_more": False}

    async def test_forward_and_backward_walks_are_exact_reverses(self, adapter):
        forward = [item["id"] for item in await _walk_forward(adapter, page_size=5)]
        backward = [item["id"] for item in await _walk_backward(adapter, page_size=5)]
        assert forward == list(reversed(backward))

    async def test_forward_walk_respects_the_type_filter(self, adapter):
        page = await adapter.get_media_paginated(
            CHAT_ID, media_types=["photo"], limit=50, after_id=_media_id(CHAT_ID, 8)
        )
        assert page == {"items": [], "has_more": False}


class TestForwardCursorContract:
    async def test_unknown_token_returns_an_empty_page(self, adapter):
        """Matches the backward path exactly, so a client can treat both alike."""
        forward = await adapter.get_media_paginated(CHAT_ID, limit=50, after_id="no_such_media_id")
        backward = await adapter.get_media_paginated(CHAT_ID, limit=50, before_id="no_such_media_id")
        assert forward == {"items": [], "has_more": False}
        assert backward == {"items": [], "has_more": False}

    async def test_foreign_chat_token_leaks_nothing(self, adapter):
        """A cursor from a chat the caller did not ask for must not shape this
        chat's window, and must be indistinguishable from a deleted token."""
        foreign = _media_id(OTHER_CHAT_ID, 555)
        page = await adapter.get_media_paginated(CHAT_ID, limit=50, after_id=foreign)
        deleted = await adapter.get_media_paginated(CHAT_ID, limit=50, after_id="deleted_token")
        assert page == deleted == {"items": [], "has_more": False}

    async def test_both_cursors_is_rejected(self, adapter):
        with pytest.raises(ValueError, match="mutually exclusive"):
            await adapter.get_media_paginated(
                CHAT_ID,
                limit=50,
                before_id=_media_id(CHAT_ID, 3739),
                after_id=_media_id(CHAT_ID, 8),
            )

    async def test_no_cursor_still_returns_the_newest_page(self, adapter):
        page = await adapter.get_media_paginated(CHAT_ID, limit=2)
        assert [item["message_id"] for item in page["items"]] == [3739, 3653]
        assert page["has_more"] is True
