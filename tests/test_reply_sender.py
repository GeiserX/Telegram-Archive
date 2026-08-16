"""Replied-to sender/media exposed on the messages payload — #268.

The reply quote block could only render the bare word "Message" because the
payload carried ``reply_to_msg_id``/``reply_to_text`` and nothing about WHO was
replied to. These tests pin the new fields, the null contract for targets that
are not in the archive, and — because the endpoint is paginated — the fact that
resolving them costs ONE extra query for the whole page, not one per row.
"""

import os
import sys
from datetime import datetime

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message, User

CHAT_ID = -1001111111111
OTHER_CHAT_ID = -1002222222222


class _QueryCounter:
    """Counts SQL statements executed on an engine."""

    def __init__(self, engine):
        self._sync_engine = engine.sync_engine
        self.statements: list[str] = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self._sync_engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self._sync_engine, "before_cursor_execute", self._record)
        return False

    @property
    def count(self) -> int:
        return len(self.statements)

    def matching(self, needle: str) -> list[str]:
        return [s for s in self.statements if needle in s]


async def _make_adapter():
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
    return DatabaseAdapter(db_manager), engine, db_manager


@pytest_asyncio.fixture
async def env():
    adapter, engine, db_manager = await _make_adapter()

    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="supergroup", title="Group"))
        session.add(Chat(id=OTHER_CHAT_ID, type="supergroup", title="Other Group"))
        # 100: has a capture-time snapshot AND a current profile (snapshot wins)
        session.add(User(id=100, first_name="Alice", last_name="Smith", username="alice"))
        # 200: no snapshot, current profile name only
        session.add(User(id=200, first_name="Bob", last_name=None, username="bob"))
        # 300: no snapshot, no names, username only
        session.add(User(id=300, first_name=None, last_name=None, username="carol"))
        # 400: nothing resolvable at all
        session.add(User(id=400, first_name=None, last_name=None, username=None))

        base = datetime(2026, 3, 1, 12)

        def msg(mid, sender_id, text, sender_name=None, reply_to=None, is_deleted=0):
            return Message(
                id=mid,
                chat_id=CHAT_ID,
                sender_id=sender_id,
                sender_name=sender_name,
                date=base.replace(minute=mid % 60, second=mid % 60),
                text=text,
                reply_to_msg_id=reply_to,
                is_deleted=is_deleted,
            )

        # Reply targets
        session.add(msg(1, 100, "target with snapshot", sender_name="Alice (2024)"))
        session.add(msg(2, 200, "target with profile only"))
        session.add(msg(3, 300, "target with username only"))
        session.add(msg(4, 400, "target with no resolvable name"))
        session.add(msg(5, 100, None, sender_name="Alice (2024)"))  # voice-only target
        session.add(msg(6, 200, "soft-deleted target", is_deleted=1))

        # Repliers
        session.add(msg(11, 200, "re: snapshot", reply_to=1))
        session.add(msg(12, 200, "re: profile", reply_to=2))
        session.add(msg(13, 200, "re: username", reply_to=3))
        session.add(msg(14, 200, "re: nameless", reply_to=4))
        session.add(msg(15, 200, "re: voice", reply_to=5))
        session.add(msg(16, 200, "re: soft-deleted", reply_to=6))
        session.add(msg(17, 200, "re: missing", reply_to=9999))
        session.add(msg(18, 200, "re: other chat only", reply_to=7777))
        session.add(msg(19, 200, "not a reply"))

        session.add(
            Media(
                id=f"{CHAT_ID}_5_voice",
                message_id=5,
                chat_id=CHAT_ID,
                type="voice",
                file_path=f"{CHAT_ID}/voice_5.ogg",
                file_name="voice_5.ogg",
                file_size=1234,
                mime_type="audio/ogg",
                duration=7,
                downloaded=1,
            )
        )

        # Same message id, different chat — must NOT resolve for CHAT_ID.
        session.add(
            Message(
                id=7777,
                chat_id=OTHER_CHAT_ID,
                sender_id=100,
                sender_name="Somebody Else",
                date=base,
                text="secret",
            )
        )
        await session.commit()

    return adapter, engine


async def _by_id(adapter, **kwargs) -> dict[int, dict]:
    messages = await adapter.get_messages_paginated(CHAT_ID, limit=100, **kwargs)
    return {m["id"]: m for m in messages}


class TestReplySenderName:
    async def test_uses_capture_time_snapshot_first(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[11]["reply_to_sender_name"] == "Alice (2024)"

    async def test_falls_back_to_current_profile_name(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[12]["reply_to_sender_name"] == "Bob"

    async def test_falls_back_to_username(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[13]["reply_to_sender_name"] == "carol"

    async def test_unnameable_sender_is_null_not_a_placeholder(self, env):
        """The server states "not known"; the viewer owns the placeholder."""
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[14]["reply_to_sender_name"] is None
        assert rows[14]["reply_to_media_type"] is None

    async def test_matches_the_senders_own_resolution(self, env):
        """The name shown in the quote block is the same string the target
        message's own bubble resolves to — one rule, no drift."""
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[11]["reply_to_sender_name"] == rows[1]["sender_name"]


class TestReplyMediaKind:
    async def test_media_only_target_reports_its_kind(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[15]["reply_to_media_type"] == "voice"
        assert rows[15]["reply_to_sender_name"] == "Alice (2024)"

    async def test_text_target_has_no_media_kind(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[11]["reply_to_media_type"] is None


class TestReplyNullContract:
    async def test_target_missing_from_the_archive_degrades_to_null(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[17]["reply_to_sender_name"] is None
        assert rows[17]["reply_to_media_type"] is None
        assert rows[17]["reply_to_text"] is None

    async def test_target_in_another_chat_does_not_resolve(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[18]["reply_to_sender_name"] is None
        assert rows[18]["reply_to_media_type"] is None

    async def test_soft_deleted_target_still_resolves(self, env):
        """A deleted-on-Telegram message is still archived history."""
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[16]["reply_to_sender_name"] == "Bob"

    async def test_non_reply_carries_neither_key(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert "reply_to_sender_name" not in rows[19]
        assert "reply_to_media_type" not in rows[19]

    async def test_reply_text_backfill_still_works(self, env):
        adapter, _ = env
        rows = await _by_id(adapter)
        assert rows[11]["reply_to_text"] == "target with snapshot"


async def _seed_replies(count: int, *, pinned: bool = False):
    """A chat of ``count`` replies, each to a distinct target."""
    adapter, engine, db_manager = await _make_adapter()
    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="supergroup", title="Group"))
        session.add(User(id=200, first_name="Bob", last_name=None, username="bob"))
        base = datetime(2026, 4, 1, 9)
        for i in range(count):
            target = 1000 + i
            session.add(
                Message(
                    id=target,
                    chat_id=CHAT_ID,
                    sender_id=200,
                    sender_name=f"Sender {i}",
                    date=base,
                    text=f"target {i}",
                )
            )
            session.add(
                Message(
                    id=2000 + i,
                    chat_id=CHAT_ID,
                    sender_id=200,
                    date=base,
                    text=f"reply {i}",
                    reply_to_msg_id=target,
                    is_pinned=1 if pinned else 0,
                )
            )
        await session.commit()
    return adapter, engine


class TestNoNPlusOne:
    async def test_query_count_is_flat_as_replies_grow(self, env):
        """1 reply and 25 distinct reply targets cost the SAME number of
        statements — the proof that reply resolution is batched, not per row."""
        small_adapter, small_engine = await _seed_replies(1)
        with _QueryCounter(small_engine) as small:
            await small_adapter.get_messages_paginated(CHAT_ID, limit=100)

        big_adapter, big_engine = await _seed_replies(25)
        with _QueryCounter(big_engine) as big:
            await big_adapter.get_messages_paginated(CHAT_ID, limit=100)

        assert small.count == big.count, f"query count grew with reply count: {small.count} -> {big.count} (N+1)"

    async def test_exactly_one_statement_resolves_the_pages_replies(self, env):
        adapter, engine = await _seed_replies(25)
        with _QueryCounter(engine) as counter:
            messages = await adapter.get_messages_paginated(CHAT_ID, limit=100)

        assert sum(1 for m in messages if m.get("reply_to_msg_id")) == 25
        reply_statements = counter.matching("messages.reply_to_msg_id IN")
        reply_statements += [s for s in counter.matching("messages.id IN") if "media" in s]
        assert len(reply_statements) == 1, counter.statements
        assert counter.count <= 5, counter.statements

    async def test_page_without_replies_skips_the_reply_query(self, env):
        adapter, engine, db_manager = await _make_adapter()
        async with db_manager.async_session_factory() as session:
            session.add(Chat(id=CHAT_ID, type="supergroup", title="Group"))
            session.add(Message(id=1, chat_id=CHAT_ID, sender_id=None, date=datetime(2026, 5, 1, 9), text="hi"))
            await session.commit()

        with _QueryCounter(engine) as counter:
            messages = await adapter.get_messages_paginated(CHAT_ID, limit=100)

        assert "reply_to_sender_name" not in messages[0]
        assert not [s for s in counter.statements if "media" in s and "messages.id IN" in s]


_REPLY_KEYS = ("reply_to_sender_name", "reply_to_media_type", "reply_to_text")
_MISSING = object()


class TestPinnedListParity:
    """The pinned-only view swaps its list into the SAME message renderer, so a
    pinned reply must arrive with the same fields as in the normal list.
    Before this, one message rendered "Reply to <name>" in the list and a bare
    "Reply to / Message" in the pinned view."""

    async def _pinned_by_id(self, adapter, ids):
        await adapter.sync_pinned_messages(CHAT_ID, ids, account_id=1)
        return {m["id"]: m for m in await adapter.get_pinned_messages(CHAT_ID)}

    async def test_pinned_rows_carry_the_same_reply_fields_as_the_list(self, env):
        adapter, _ = env
        ids = [11, 12, 13, 14, 15, 16, 17, 18, 19]
        pinned = await self._pinned_by_id(adapter, ids)
        listed = await _by_id(adapter)

        assert set(pinned) == set(ids)
        for mid in ids:
            for key in _REPLY_KEYS:
                assert pinned[mid].get(key, _MISSING) == listed[mid].get(key, _MISSING), (
                    f"message {mid} renders differently in the pinned list ({key})"
                )

    async def test_pinned_reply_names_the_sender(self, env):
        adapter, _ = env
        pinned = await self._pinned_by_id(adapter, [11])
        assert pinned[11]["reply_to_sender_name"] == "Alice (2024)"
        assert pinned[11]["reply_to_text"] == "target with snapshot"

    async def test_pinned_reply_to_media_reports_the_kind(self, env):
        adapter, _ = env
        pinned = await self._pinned_by_id(adapter, [15])
        assert pinned[15]["reply_to_media_type"] == "voice"
        assert pinned[15]["reply_to_sender_name"] == "Alice (2024)"

    async def test_pinned_null_contract_matches(self, env):
        adapter, _ = env
        pinned = await self._pinned_by_id(adapter, [17, 19])
        assert pinned[17]["reply_to_sender_name"] is None
        assert pinned[17]["reply_to_media_type"] is None
        assert "reply_to_sender_name" not in pinned[19]
        assert "reply_to_media_type" not in pinned[19]

    async def test_pinned_list_resolves_every_reply_in_one_query(self, env):
        adapter, engine = await _seed_replies(25, pinned=True)
        with _QueryCounter(engine) as counter:
            pinned = await adapter.get_pinned_messages(CHAT_ID)

        assert sum(1 for m in pinned if m.get("reply_to_sender_name")) == 25
        reply_statements = [s for s in counter.matching("messages.id IN") if "media" in s]
        assert len(reply_statements) == 1, counter.statements
        # A small fixed set of statements for 25 replies — nothing per row.
        assert counter.count <= 4, counter.statements

    async def test_pinned_query_count_is_flat_as_pinned_replies_grow(self, env):
        small_adapter, small_engine = await _seed_replies(1, pinned=True)
        with _QueryCounter(small_engine) as small:
            await small_adapter.get_pinned_messages(CHAT_ID)

        big_adapter, big_engine = await _seed_replies(25, pinned=True)
        with _QueryCounter(big_engine) as big:
            await big_adapter.get_pinned_messages(CHAT_ID)

        assert small.count == big.count, f"query count grew with reply count: {small.count} -> {big.count} (N+1)"

    async def test_pinned_list_without_replies_skips_the_reply_query(self, env):
        adapter, engine, db_manager = await _make_adapter()
        async with db_manager.async_session_factory() as session:
            session.add(Chat(id=CHAT_ID, type="supergroup", title="Group"))
            session.add(
                Message(id=1, chat_id=CHAT_ID, sender_id=None, date=datetime(2026, 5, 1, 9), text="hi", is_pinned=1)
            )
            await session.commit()

        with _QueryCounter(engine) as counter:
            pinned = await adapter.get_pinned_messages(CHAT_ID)

        assert "reply_to_sender_name" not in pinned[0]
        assert not [s for s in counter.matching("messages.id IN") if "media" in s], counter.statements


class TestByDateLookupParity:
    """``/messages/by-date`` used to run its own text-only reply lookup — a
    second resolution rule for the same field, which is exactly the drift that
    produced #259. It now shares the one helper."""

    async def test_by_date_row_matches_the_list_row(self, env):
        adapter, _ = env
        listed = await _by_id(adapter)
        found = await adapter.find_message_by_date_with_joins(CHAT_ID, datetime(2026, 3, 1, 12, 11))

        assert found["id"] == 11
        for key in _REPLY_KEYS:
            assert found.get(key, _MISSING) == listed[11].get(key, _MISSING)
        assert found["reply_to_sender_name"] == "Alice (2024)"

    async def test_by_date_media_only_target_reports_its_kind(self, env):
        adapter, _ = env
        found = await adapter.find_message_by_date_with_joins(CHAT_ID, datetime(2026, 3, 1, 12, 15))
        assert found["id"] == 15
        assert found["reply_to_media_type"] == "voice"

    async def test_by_date_non_reply_carries_neither_key(self, env):
        adapter, _ = env
        found = await adapter.find_message_by_date_with_joins(CHAT_ID, datetime(2026, 3, 1, 12, 19))
        assert found["id"] == 19
        assert "reply_to_sender_name" not in found
        assert "reply_to_media_type" not in found

    async def test_by_date_still_costs_one_reply_statement(self, env):
        adapter, engine = env
        with _QueryCounter(engine) as counter:
            await adapter.find_message_by_date_with_joins(CHAT_ID, datetime(2026, 3, 1, 12, 11))

        reply_statements = [s for s in counter.matching("messages.id IN") if "media" in s]
        assert len(reply_statements) == 1, counter.statements


class TestPathsAuditedAsUnaffected:
    """Read paths that return reply information but never feed the quote block.

    ``get_messages_for_export`` backs the JSON export download, whose ``reply_to``
    is the raw target id in an archival document — not a rendered quote. Adding
    resolved names there would change a published file format, and the export is
    streamed row by row, so it is left alone deliberately."""

    async def test_export_rows_expose_the_target_id_only(self, env):
        adapter, _ = env
        rows = {m["id"]: m async for m in adapter.get_messages_for_export(CHAT_ID)}

        assert rows[11]["reply_to"] == 1
        assert "reply_to_sender_name" not in rows[11]
        assert "reply_to_media_type" not in rows[11]
