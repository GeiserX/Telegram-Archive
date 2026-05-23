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
