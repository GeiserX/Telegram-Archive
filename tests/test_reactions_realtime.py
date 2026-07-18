"""Real-time reaction reconcile + extractor tests (#219), real SQLite.

Covers reconcile_reactions (aggregate-only, retain-on-removal tombstone,
created_at preservation, zero-clear, legacy-row collapse, skip-if-absent,
read-excludes-removed) and the shared extract_reactions helper.
"""

import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Message, Reaction
from src.message_utils import extract_reactions, normalize_reaction_emoji

CHAT_ID = -100
MSG_ID = 42


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
        session.add(Message(id=MSG_ID, chat_id=CHAT_ID, date=datetime(2026, 7, 18, 12, 0), text="hi"))
        await session.commit()

    yield adapter
    await engine.dispose()


async def _rows(adapter, *, include_removed=True):
    async with adapter.db_manager.async_session_factory() as session:
        stmt = select(Reaction).where(and_cond(MSG_ID, CHAT_ID))
        result = await session.execute(stmt)
        rows = list(result.scalars())
    if not include_removed:
        rows = [r for r in rows if r.removed_at is None]
    return rows


def and_cond(mid, cid):
    return (Reaction.message_id == mid) & (Reaction.chat_id == cid)


class TestReconcileReactions:
    async def test_add_creates_aggregate_row(self, adapter):
        out = await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 3}])
        assert out == "reconciled"
        active = await adapter.get_reactions(MSG_ID, CHAT_ID)
        assert active == [{"emoji": "👍", "user_id": None, "count": 3}]

    async def test_idempotent_noop(self, adapter):
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 3}])
        out = await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 3}])
        assert out == "noop"

    async def test_count_change_updates_in_place_preserving_created_at(self, adapter):
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 3}])
        rows = await _rows(adapter)
        created = rows[0].created_at
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 5}])
        rows = await _rows(adapter)
        assert len(rows) == 1
        assert rows[0].count == 5
        assert rows[0].created_at == created  # first-seen preserved (F6)

    async def test_removal_tombstones_and_excludes_from_live_count(self, adapter):
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 1}, {"emoji": "🔥", "count": 2}])
        # 🔥 removed
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 1}])
        active = await adapter.get_reactions(MSG_ID, CHAT_ID)
        assert active == [{"emoji": "👍", "user_id": None, "count": 1}]
        all_rows = await _rows(adapter)
        fire = [r for r in all_rows if r.emoji == "🔥"][0]
        assert fire.removed_at is not None  # retained, not deleted

    async def test_zero_clear_tombstones_all(self, adapter):
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 1}])
        out = await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [])
        assert out == "reconciled"
        assert await adapter.get_reactions(MSG_ID, CHAT_ID) == []
        assert all(r.removed_at is not None for r in await _rows(adapter))

    async def test_readd_revives_tombstone_keeping_created_at(self, adapter):
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 1}])
        created = (await _rows(adapter))[0].created_at
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [])  # removed
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 2}])  # re-added
        active = await adapter.get_reactions(MSG_ID, CHAT_ID)
        assert active == [{"emoji": "👍", "user_id": None, "count": 2}]
        rows = await _rows(adapter)
        assert len(rows) == 1  # revived, not a second row
        assert rows[0].created_at == created

    async def test_hard_delete_when_mark_removed_false(self, adapter):
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 1}])
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [], mark_removed=False)
        assert await _rows(adapter) == []

    async def test_collapses_legacy_multi_row_per_emoji(self, adapter):
        # Simulate legacy per-user rows for one emoji (old full-replace era).
        async with adapter.db_manager.async_session_factory() as session:
            session.add_all(
                [
                    Reaction(message_id=MSG_ID, chat_id=CHAT_ID, emoji="👍", user_id=10, count=1),
                    Reaction(message_id=MSG_ID, chat_id=CHAT_ID, emoji="👍", user_id=None, count=2),
                ]
            )
            await session.commit()
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 4}])
        rows = await _rows(adapter)
        assert len(rows) == 1
        assert rows[0].user_id is None
        assert rows[0].count == 4

    async def test_skips_when_message_not_archived(self, adapter):
        out = await adapter.reconcile_reactions(999999, CHAT_ID, [{"emoji": "👍", "count": 1}])
        assert out == "no_message"
        assert await _rows(adapter) == []

    async def test_unknown_reaction_variant_ignored(self, adapter):
        # Extractor yields nothing for an unrecognized variant, so reconcile is a noop.
        out = await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [])
        assert out == "noop"

    async def test_zero_or_negative_count_treated_as_absent(self, adapter):
        # A snapshot entry with count<=0 must not revive/retain a reaction.
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 2}])
        await adapter.reconcile_reactions(MSG_ID, CHAT_ID, [{"emoji": "👍", "count": 0}])
        assert await adapter.get_reactions(MSG_ID, CHAT_ID) == []
        assert all(r.removed_at is not None for r in await _rows(adapter))


class TestExtractReactions:
    def test_none_returns_empty(self):
        assert extract_reactions(None) == []

    def test_emoji_and_custom_and_paid(self):
        mr = SimpleNamespace(
            results=[
                SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=3),
                SimpleNamespace(reaction=SimpleNamespace(document_id=555, emoticon=None), count=1),
                SimpleNamespace(reaction=_Paid(), count=7),
            ]
        )
        assert extract_reactions(mr) == [
            {"emoji": "👍", "count": 3},
            {"emoji": "custom_555", "count": 1},
            {"emoji": "paid", "count": 7},
        ]

    def test_zero_and_unknown_dropped(self):
        mr = SimpleNamespace(
            results=[
                SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=0),  # dropped
                SimpleNamespace(reaction=_Empty(), count=2),  # empty -> dropped
                SimpleNamespace(reaction=SimpleNamespace(emoticon=None, document_id=None), count=1),  # unknown
            ]
        )
        assert extract_reactions(mr) == []

    def test_failure_returns_none_sentinel(self):
        # A broken shape must return None (skip reconcile), NOT [] (which would
        # tombstone valid reactions). None is distinct from a valid empty snapshot.
        assert extract_reactions(SimpleNamespace(results=object())) is None
        assert extract_reactions(None) == []  # valid: message has no reactions

    def test_normalize_variants(self):
        assert normalize_reaction_emoji(SimpleNamespace(emoticon="🔥")) == "🔥"
        assert normalize_reaction_emoji(SimpleNamespace(document_id=9, emoticon=None)) == "custom_9"
        assert normalize_reaction_emoji(_Paid()) == "paid"
        assert normalize_reaction_emoji(_Empty()) is None
        assert normalize_reaction_emoji(None) is None


class _Paid:
    """Stand-in for telethon ReactionPaid (name contains 'Paid')."""


class _Empty:
    """Stand-in for telethon ReactionEmpty (name contains 'Empty')."""


@pytest.mark.asyncio
async def test_reaction_only_edit_does_not_create_phantom(adapter):
    """#219 e2e: a reaction-only edit (unchanged text, newer edit_date) must not
    bump edit_date, so the viewer never shows a phantom 'edited' marker."""
    reaction_edit = datetime(2026, 7, 18, 12, 30)
    outcome = await adapter.update_message_text(CHAT_ID, MSG_ID, "hi", reaction_edit)
    assert outcome == "noop"
    async with adapter.db_manager.async_session_factory() as session:
        msg = (
            await session.execute(select(Message).where((Message.id == MSG_ID) & (Message.chat_id == CHAT_ID)))
        ).scalar_one()
    assert msg.edit_date is None
