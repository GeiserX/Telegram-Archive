"""Integration tests for the failed-download retry cap (#212), real SQLite.

Covers get_pending_media_downloads(max_attempts=...) filtering and
increment_media_download_attempts against actual SQL.
"""

import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Media


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


async def _add(adapter, media_id, *, downloaded=0, attempts=0, mtype="video"):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            Media(
                id=media_id, message_id=1, chat_id=-100, type=mtype, downloaded=downloaded, download_attempts=attempts
            )
        )
        await session.commit()


class TestRetryCap:
    @pytest.mark.asyncio
    async def test_pending_excludes_maxed_out_rows(self, adapter):
        await _add(adapter, "under", attempts=2)  # below cap
        await _add(adapter, "at_cap", attempts=5)  # at cap
        await _add(adapter, "over_cap", attempts=9)  # past cap
        await _add(adapter, "done", downloaded=1, attempts=0)  # already downloaded

        pending = await adapter.get_pending_media_downloads(max_attempts=5)
        ids = {p["id"] for p in pending}

        assert ids == {"under"}
        assert pending[0]["download_attempts"] == 2

    @pytest.mark.asyncio
    async def test_no_cap_returns_all_pending(self, adapter):
        await _add(adapter, "a", attempts=99)
        await _add(adapter, "b", attempts=0)

        pending = await adapter.get_pending_media_downloads()  # max_attempts=None → no cap
        assert {p["id"] for p in pending} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_increment_bumps_and_eventually_excludes(self, adapter):
        await _add(adapter, "m", attempts=4)

        await adapter.increment_media_download_attempts("m")

        async with adapter.db_manager.async_session_factory() as session:
            from sqlalchemy import select

            val = (await session.execute(select(Media.download_attempts).where(Media.id == "m"))).scalar()
        assert val == 5
        # now at the cap → no longer pending
        assert await adapter.get_pending_media_downloads(max_attempts=5) == []

    @pytest.mark.asyncio
    async def test_metadata_types_never_pending(self, adapter):
        await _add(adapter, "geo", mtype="geo")
        await _add(adapter, "poll", mtype="poll")
        await _add(adapter, "real", mtype="document")

        pending = await adapter.get_pending_media_downloads(max_attempts=5)
        assert {p["id"] for p in pending} == {"real"}
