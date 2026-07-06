"""Integration tests for folder-membership resolution against real SQLite.

Exercises the actual SQL in get_chats_for_folder_resolution (the users outer
join) and the chunked existence check in sync_folder_members — paths that the
mock-based unit tests only reason about.
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
from src.db.models import Base, Chat, ChatFolder, ChatFolderMember, User


@pytest_asyncio.fixture
async def adapter():
    """In-memory SQLite adapter with the folder tables created."""
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


class TestGetChatsForFolderResolutionSQL:
    @pytest.mark.asyncio
    async def test_join_resolves_is_bot_and_never_collides(self, adapter):
        """The users join sets is_bot for bot privates only; group ids never collide."""
        async with adapter.db_manager.async_session_factory() as session:
            session.add(Chat(id=500, type="private", title=None))  # bot private
            session.add(Chat(id=600, type="private", title=None))  # human private
            session.add(Chat(id=700, type="private", title=None))  # no user row at all
            session.add(Chat(id=-100, type="group", title="G"))  # negative id
            session.add(User(id=500, is_bot=1))
            session.add(User(id=600, is_bot=0))
            await session.commit()

        rows = await adapter.get_chats_for_folder_resolution()
        by_id = {r["id"]: r for r in rows}

        assert by_id[500]["is_bot"] is True
        assert by_id[600]["is_bot"] is False
        assert by_id[700]["is_bot"] is False  # coalesced from a missing user row
        assert by_id[-100]["is_bot"] is False  # negative group id can't match a user id
        assert by_id[-100]["type"] == "group"


class TestSyncFolderMembersChunking:
    @pytest.mark.asyncio
    async def test_large_member_set_is_chunked_and_existence_filtered(self, adapter):
        """More members than the chunk size persist correctly; non-existent ids drop."""
        n = adapter._FOLDER_MEMBER_CHUNK * 2 + 37  # forces 3 chunks
        async with adapter.db_manager.async_session_factory() as session:
            session.add(ChatFolder(id=1, title="Big", emoticon=None, sort_order=0))
            for cid in range(1, n + 1):
                session.add(Chat(id=cid, type="group", title=f"c{cid}"))
            await session.commit()

        # Real ids + a duplicate + an id with no chat row.
        member_ids = list(range(1, n + 1)) + [1, 1, 99999999]
        await adapter.sync_folder_members(1, member_ids)

        async with adapter.db_manager.async_session_factory() as session:
            from sqlalchemy import func, select

            total = (
                await session.execute(
                    select(func.count(ChatFolderMember.chat_id)).where(ChatFolderMember.folder_id == 1)
                )
            ).scalar()
            has_bogus = (
                await session.execute(
                    select(ChatFolderMember.chat_id).where(
                        ChatFolderMember.folder_id == 1, ChatFolderMember.chat_id == 99999999
                    )
                )
            ).first()

        assert total == n  # every real id once (deduped), none doubled
        assert has_bogus is None  # non-existent chat filtered out

    @pytest.mark.asyncio
    async def test_empty_member_set_clears_existing(self, adapter):
        """Syncing an empty set removes all of a folder's members."""
        async with adapter.db_manager.async_session_factory() as session:
            session.add(ChatFolder(id=2, title="F", emoticon=None, sort_order=0))
            session.add(Chat(id=10, type="group", title="c"))
            session.add(ChatFolderMember(folder_id=2, chat_id=10))
            await session.commit()

        await adapter.sync_folder_members(2, [])

        async with adapter.db_manager.async_session_factory() as session:
            from sqlalchemy import func, select

            total = (
                await session.execute(
                    select(func.count(ChatFolderMember.chat_id)).where(ChatFolderMember.folder_id == 2)
                )
            ).scalar()
        assert total == 0
