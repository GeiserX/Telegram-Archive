"""Integration tests for media gallery adapter methods (get_media_paginated, get_media_counts).

Uses real in-memory SQLite to exercise the actual SQL queries.
"""

import os
import sys
from datetime import datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message, User


@pytest_asyncio.fixture
async def adapter():
    """In-memory SQLite with seeded media data."""
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

    chat_id = -1001

    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=chat_id, type="channel", title="Test Chat"))
        session.add(User(id=100, first_name="Alice", last_name="Smith", username="alice"))
        session.add(User(id=200, first_name="Bob", last_name=None, username="bob"))

        # Messages with different dates
        session.add(Message(id=1, chat_id=chat_id, sender_id=100, date=datetime(2026, 1, 1, 10), text="photo msg"))
        session.add(Message(id=2, chat_id=chat_id, sender_id=100, date=datetime(2026, 1, 2, 10), text="album msg"))
        session.add(Message(id=3, chat_id=chat_id, sender_id=200, date=datetime(2026, 1, 3, 10), text="video msg"))
        session.add(Message(id=4, chat_id=chat_id, sender_id=None, date=datetime(2026, 1, 4, 10), text="channel post"))

        # Media items — including album (multiple media per message)
        session.add(
            Media(
                id="photo_1",
                message_id=1,
                chat_id=chat_id,
                type="photo",
                file_path="-1001/photo_1.jpg",
                file_name="photo_1.jpg",
                file_size=100000,
                mime_type="image/jpeg",
                width=1920,
                height=1080,
                downloaded=1,
            )
        )
        session.add(
            Media(
                id="photo_2a",
                message_id=2,
                chat_id=chat_id,
                type="photo",
                file_path="-1001/photo_2a.jpg",
                file_name="photo_2a.jpg",
                file_size=200000,
                mime_type="image/jpeg",
                width=1920,
                height=1080,
                downloaded=1,
            )
        )
        session.add(
            Media(
                id="photo_2b",
                message_id=2,
                chat_id=chat_id,
                type="photo",
                file_path="-1001/photo_2b.jpg",
                file_name="photo_2b.jpg",
                file_size=150000,
                mime_type="image/jpeg",
                width=1920,
                height=1080,
                downloaded=1,
            )
        )
        session.add(
            Media(
                id="video_3",
                message_id=3,
                chat_id=chat_id,
                type="video",
                file_path="-1001/video_3.mp4",
                file_name="video_3.mp4",
                file_size=5000000,
                mime_type="video/mp4",
                width=1280,
                height=720,
                duration=30,
                downloaded=1,
            )
        )
        session.add(
            Media(
                id="doc_4",
                message_id=4,
                chat_id=chat_id,
                type="document",
                file_path="-1001/report.pdf",
                file_name="report.pdf",
                file_size=50000,
                mime_type="application/pdf",
                downloaded=1,
            )
        )
        # Undownloaded media — should not appear
        session.add(
            Media(
                id="photo_hidden",
                message_id=1,
                chat_id=chat_id,
                type="photo",
                file_path=None,
                file_name=None,
                file_size=None,
                mime_type="image/jpeg",
                downloaded=0,
            )
        )
        await session.commit()

    db_adapter = DatabaseAdapter(db_manager)
    return db_adapter


class TestGetMediaPaginated:
    """Tests for get_media_paginated with real SQLite."""

    async def test_returns_all_downloaded_media(self, adapter):
        result = await adapter.get_media_paginated(-1001)
        assert len(result["items"]) == 5
        assert result["has_more"] is False

    async def test_excludes_undownloaded_media(self, adapter):
        result = await adapter.get_media_paginated(-1001)
        ids = [item["id"] for item in result["items"]]
        assert "photo_hidden" not in ids

    async def test_filters_by_media_type(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["photo"])
        assert len(result["items"]) == 3
        for item in result["items"]:
            assert item["type"] == "photo"

    async def test_filters_multiple_types(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["photo", "video"])
        assert len(result["items"]) == 4
        types = {item["type"] for item in result["items"]}
        assert types == {"photo", "video"}

    async def test_orders_by_date_desc(self, adapter):
        result = await adapter.get_media_paginated(-1001)
        dates = [item["message_date"] for item in result["items"]]
        assert dates == sorted(dates, reverse=True)

    async def test_resolves_sender_name_from_user(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["photo"])
        # All photos belong to message_id 1 or 2, sender_id=100 (Alice Smith)
        for item in result["items"]:
            assert item["sender_name"] == "Alice Smith"

    async def test_sender_name_without_last_name(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["video"])
        assert result["items"][0]["sender_name"] == "Bob"

    async def test_sender_name_null_for_no_user(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["document"])
        assert result["items"][0]["sender_name"] is None

    async def test_limit_works(self, adapter):
        result = await adapter.get_media_paginated(-1001, limit=2)
        assert len(result["items"]) == 2
        assert result["has_more"] is True

    async def test_cursor_pagination_no_duplicates(self, adapter):
        """Fetching page 1 then page 2 returns all items with no overlap."""
        page1 = await adapter.get_media_paginated(-1001, limit=3)
        assert len(page1["items"]) == 3
        assert page1["has_more"] is True

        last_id = page1["items"][-1]["id"]
        page2 = await adapter.get_media_paginated(-1001, limit=3, before_id=last_id)

        all_ids = [item["id"] for item in page1["items"]] + [item["id"] for item in page2["items"]]
        assert len(all_ids) == len(set(all_ids)), "Duplicate items across pages"
        assert len(all_ids) == 5

    async def test_cursor_with_album_messages(self, adapter):
        """Album messages (multiple media per message_id) paginate correctly."""
        # Get page with limit=1 starting from newest
        page1 = await adapter.get_media_paginated(-1001, limit=1)
        page2 = await adapter.get_media_paginated(-1001, limit=1, before_id=page1["items"][0]["id"])
        page3 = await adapter.get_media_paginated(-1001, limit=1, before_id=page2["items"][0]["id"])

        ids = [page1["items"][0]["id"], page2["items"][0]["id"], page3["items"][0]["id"]]
        assert len(ids) == len(set(ids)), "Duplicate items when paginating through album"

    async def test_cursor_not_found_returns_empty(self, adapter):
        result = await adapter.get_media_paginated(-1001, before_id="nonexistent_id")
        assert result["items"] == []
        assert result["has_more"] is False

    async def test_empty_chat_returns_empty(self, adapter):
        result = await adapter.get_media_paginated(-9999)
        assert result["items"] == []
        assert result["has_more"] is False

    async def test_item_fields_complete(self, adapter):
        result = await adapter.get_media_paginated(-1001, media_types=["video"])
        item = result["items"][0]
        assert item["id"] == "video_3"
        assert item["message_id"] == 3
        assert item["chat_id"] == -1001
        assert item["type"] == "video"
        assert item["file_path"] == "-1001/video_3.mp4"
        assert item["file_name"] == "video_3.mp4"
        assert item["file_size"] == 5000000
        assert item["mime_type"] == "video/mp4"
        assert item["width"] == 1280
        assert item["height"] == 720
        assert item["duration"] == 30
        assert item["message_date"] is not None
        assert item["sender_name"] == "Bob"


class TestGetMediaCounts:
    """Tests for get_media_counts with real SQLite."""

    async def test_returns_counts_by_type(self, adapter):
        counts = await adapter.get_media_counts(-1001)
        assert counts["photo"] == 3
        assert counts["video"] == 1
        assert counts["document"] == 1

    async def test_excludes_undownloaded(self, adapter):
        counts = await adapter.get_media_counts(-1001)
        total = sum(counts.values())
        assert total == 5  # not 6 (excludes the undownloaded one)

    async def test_empty_chat_returns_empty_dict(self, adapter):
        counts = await adapter.get_media_counts(-9999)
        assert counts == {}


# ===========================================================================
# #257 -- ordering / tiebreak of get_media_paginated
#
# The pre-existing pagination tests above only assert "no duplicates" and every
# fixture message has a unique date, so the tiebreak branch was never exercised.
# These fixtures use PRODUCTION-format composite ids (f"{chat_id}_{msg_id}_{type}")
# because the defect was Media.id sorting LEXICALLY.
# ===========================================================================


async def _build_adapter(seed) -> DatabaseAdapter:
    """In-memory SQLite adapter, seeded by the given ``seed(session, chat_id)``."""
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
        await seed(session)
        await session.commit()

    return DatabaseAdapter(db_manager)


async def _walk_all(adapter: DatabaseAdapter, chat_id: int, limit: int) -> list[dict]:
    """Page through every media row using before_id cursors until exhaustion."""
    collected: list[dict] = []
    before_id = None
    for _ in range(1000):  # guard: a broken cursor must fail the test, not hang it
        page = await adapter.get_media_paginated(chat_id, limit=limit, before_id=before_id)
        collected.extend(page["items"])
        if not page["has_more"]:
            return collected
        assert page["items"], "has_more=True with an empty page would loop forever"
        before_id = page["items"][-1]["id"]
    raise AssertionError("pagination did not terminate")


MULTI_DATE_CHAT = -100257
MULTI_DATE_TOTAL = 120
SAME_DATE_CHAT = -100258
SAME_DATE_MESSAGE_IDS = [7, 8, 9, 80, 89, 99, 120, 1000]


@pytest_asyncio.fixture
async def multi_date_adapter():
    """120 media rows spread over 4 timestamps (30 rows share each timestamp)."""

    async def seed(session) -> None:
        session.add(Chat(id=MULTI_DATE_CHAT, type="channel", title="Bulk"))
        for index in range(MULTI_DATE_TOTAL):
            message_id = index + 1
            day = 1 + index // 30
            session.add(
                Message(
                    id=message_id,
                    chat_id=MULTI_DATE_CHAT,
                    sender_id=None,
                    date=datetime(2026, 3, day, 12),
                    text="",
                )
            )
            session.add(
                Media(
                    id=f"{MULTI_DATE_CHAT}_{message_id}_voice",
                    message_id=message_id,
                    chat_id=MULTI_DATE_CHAT,
                    type="voice",
                    file_path=f"{MULTI_DATE_CHAT}/voice_{message_id}.ogg",
                    file_name=f"voice_{message_id}.ogg",
                    downloaded=1,
                )
            )

    return await _build_adapter(seed)


@pytest_asyncio.fixture
async def same_date_adapter():
    """Every media row shares ONE timestamp; message_ids differ in digit length."""

    async def seed(session) -> None:
        session.add(Chat(id=SAME_DATE_CHAT, type="channel", title="Tiebreak"))
        shared_date = datetime(2026, 4, 1, 9, 30)
        for message_id in SAME_DATE_MESSAGE_IDS:
            session.add(
                Message(
                    id=message_id,
                    chat_id=SAME_DATE_CHAT,
                    sender_id=None,
                    date=shared_date,
                    text="",
                )
            )
            session.add(
                Media(
                    id=f"{SAME_DATE_CHAT}_{message_id}_voice",
                    message_id=message_id,
                    chat_id=SAME_DATE_CHAT,
                    type="voice",
                    file_path=f"{SAME_DATE_CHAT}/voice_{message_id}.ogg",
                    file_name=f"voice_{message_id}.ogg",
                    downloaded=1,
                )
            )

    return await _build_adapter(seed)


class TestMediaPaginationOrdering:
    """#257: full-walk integrity plus numeric (not lexical) tiebreak ordering."""

    async def test_full_walk_returns_every_row_exactly_once(self, multi_date_adapter) -> None:
        items = await _walk_all(multi_date_adapter, MULTI_DATE_CHAT, limit=7)
        ids = [item["id"] for item in items]

        assert len(ids) == MULTI_DATE_TOTAL
        assert len(set(ids)) == MULTI_DATE_TOTAL, "duplicate rows across the walk"
        expected = {f"{MULTI_DATE_CHAT}_{n}_voice" for n in range(1, MULTI_DATE_TOTAL + 1)}
        assert set(ids) == expected, "the walk skipped rows"

    async def test_full_walk_dates_are_non_increasing(self, multi_date_adapter) -> None:
        items = await _walk_all(multi_date_adapter, MULTI_DATE_CHAT, limit=7)
        dates = [item["message_date"] for item in items]

        assert all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1)), "message_date is not descending"

    async def test_full_walk_is_numerically_descending_within_each_date(self, multi_date_adapter) -> None:
        items = await _walk_all(multi_date_adapter, MULTI_DATE_CHAT, limit=7)
        message_ids = [item["message_id"] for item in items]

        assert message_ids == sorted(message_ids, reverse=True)

    async def test_identical_dates_tiebreak_numerically_not_lexically(self, same_date_adapter) -> None:
        items = await _walk_all(same_date_adapter, SAME_DATE_CHAT, limit=3)
        message_ids = [item["message_id"] for item in items]

        assert message_ids == sorted(SAME_DATE_MESSAGE_IDS, reverse=True)

        # A lexical sort on the composite Media.id puts 9 before 89 before 8 -- the
        # exact symptom of #257. Assert we are NOT producing that order.
        lexical = sorted(SAME_DATE_MESSAGE_IDS, key=lambda n: f"{SAME_DATE_CHAT}_{n}_voice", reverse=True)
        assert message_ids != lexical

    async def test_identical_dates_walk_has_no_duplicates_or_gaps(self, same_date_adapter) -> None:
        items = await _walk_all(same_date_adapter, SAME_DATE_CHAT, limit=3)
        ids = [item["id"] for item in items]

        assert len(ids) == len(set(ids))
        assert set(ids) == {f"{SAME_DATE_CHAT}_{n}_voice" for n in SAME_DATE_MESSAGE_IDS}
