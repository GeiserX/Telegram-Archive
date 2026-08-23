"""Message entities: capture and store formatting instead of flattening (9t6.10.1).

The archive stored ``message.text`` — Telethon's markdown re-serialization —
so ``**bold**`` markers leaked into archived text, spoilers arrived silently
PRE-REVEALED (markdown.unparse drops them), and entity offsets never aligned
with what was stored. Now both writers store ``message.raw_text`` (the wire
text entity offsets actually index) plus ``raw_data["entities"]`` as JSON,
and both edit paths refresh entities — including formatting-only edits, which
merge silently without the phantom "edited" marker #219 removed.
"""

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Message
from src.message_utils import message_plain_text, serialize_message_entities
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001


class TestSerializeMessageEntities(unittest.TestCase):
    def test_real_telethon_entities_serialize_with_extras(self):
        from telethon.tl.types import (
            MessageEntityBlockquote,
            MessageEntityBold,
            MessageEntityCustomEmoji,
            MessageEntityMentionName,
            MessageEntityPre,
            MessageEntitySpoiler,
            MessageEntityTextUrl,
        )

        serialized = serialize_message_entities(
            [
                MessageEntityBold(offset=0, length=4),
                MessageEntityTextUrl(offset=5, length=3, url="https://x.example"),
                MessageEntitySpoiler(offset=9, length=2),
                MessageEntityPre(offset=12, length=5, language="py"),
                MessageEntityBlockquote(offset=18, length=4, collapsed=True),
                MessageEntityMentionName(offset=23, length=2, user_id=42),
                MessageEntityCustomEmoji(offset=26, length=2, document_id=999),
            ]
        )
        self.assertEqual(
            serialized,
            [
                {"type": "bold", "offset": 0, "length": 4},
                {"type": "text_url", "offset": 5, "length": 3, "url": "https://x.example"},
                {"type": "spoiler", "offset": 9, "length": 2},
                {"type": "pre", "offset": 12, "length": 5, "language": "py"},
                {"type": "blockquote", "offset": 18, "length": 4, "collapsed": True},
                {"type": "mention_name", "offset": 23, "length": 2, "user_id": 42},
                {"type": "custom_emoji", "offset": 26, "length": 2, "document_id": 999},
            ],
        )

    def test_payload_is_json_safe(self):
        from telethon.tl.types import MessageEntityItalic

        serialized = serialize_message_entities([MessageEntityItalic(offset=0, length=3)])
        json.dumps(serialized)  # raises if anything non-serializable leaked

    def test_magicmock_and_garbage_are_inert(self):
        self.assertIsNone(serialize_message_entities([MagicMock()]))
        self.assertIsNone(serialize_message_entities(None))
        self.assertIsNone(serialize_message_entities([]))
        self.assertIsNone(serialize_message_entities("not a list"))

    def test_invalid_offsets_are_skipped(self):
        from telethon.tl.types import MessageEntityBold

        bad = MessageEntityBold(offset=0, length=4)
        bad.offset = -1
        zero = MessageEntityBold(offset=0, length=4)
        zero.length = 0
        good = MessageEntityBold(offset=2, length=3)
        self.assertEqual(
            serialize_message_entities([bad, zero, good]),
            [{"type": "bold", "offset": 2, "length": 3}],
        )


class TestMessagePlainText(unittest.TestCase):
    def test_raw_text_wins_over_markdown_serialization(self):
        class Msg:
            raw_text = "hola mundo"
            text = "**hola** mundo"

        self.assertEqual(message_plain_text(Msg()), "hola mundo")

    def test_mock_fixture_falls_back_to_text(self):
        mock = MagicMock()
        mock.text = "plain"
        self.assertEqual(message_plain_text(mock), "plain")

    def test_service_message_is_empty(self):
        svc = MagicMock()
        svc.raw_text = None
        svc.text = None
        self.assertEqual(message_plain_text(svc), "")


class TestSweepCapturesEntities(unittest.TestCase):
    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
        self.backup.db = AsyncMock()
        self.backup.config = MagicMock()
        self.backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        self.backup.client = AsyncMock()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, msg_id):
        msg = MagicMock()
        msg.id = msg_id
        msg.sender = None
        msg.sender_id = 42
        msg.date = datetime(2026, 1, 1)
        msg.text = "hello"
        msg.reply_to_msg_id = None
        msg.reply_to = None
        msg.edit_date = None
        msg.out = False
        msg.pinned = False
        msg.grouped_id = None
        msg.fwd_from = None
        msg.media = None
        msg.reactions = None
        msg.post_author = None
        msg.action = None
        return msg

    def test_raw_text_and_entities_are_stored(self):
        from telethon.tl.types import MessageEntityBold

        msg = self._make_message(1)
        msg.raw_text = "hola mundo"
        msg.text = "**hola** mundo"  # what Telethon's markdown parse mode serves
        msg.entities = [MessageEntityBold(offset=0, length=4)]

        result = self._run(self.backup._process_message(msg, CHAT_ID))
        self.assertEqual(result["text"], "hola mundo")
        self.assertEqual(
            result["raw_data"]["entities"],
            [{"type": "bold", "offset": 0, "length": 4}],
        )

    def test_plain_message_stores_no_entities_key(self):
        msg = self._make_message(2)
        result = self._run(self.backup._process_message(msg, CHAT_ID))
        self.assertNotIn("entities", result["raw_data"])


# ---------------------------------------------------------------------------
# Adapter: entity merge on the edit paths (real in-memory SQLite)
# ---------------------------------------------------------------------------


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

    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="channel", title="Chat A"))
        session.add(
            Message(
                id=1,
                chat_id=CHAT_ID,
                sender_id=None,
                date=datetime(2026, 1, 1),
                text="original",
                raw_data=json.dumps({"webpage": {"url": "https://keep.example"}}),
                account_id=1,
            )
        )
        await session.commit()

    return DatabaseAdapter(db_manager)


async def _row(adapter_):
    async with adapter_.db_manager.async_session_factory() as session:
        result = await session.execute(select(Message).where(Message.id == 1))
        return result.scalar_one()


BOLD = [{"type": "bold", "offset": 0, "length": 3}]
ITALIC = [{"type": "italic", "offset": 0, "length": 3}]


@pytest.mark.asyncio
async def test_text_edit_stores_entities_and_preserves_raw_data(adapter):
    outcome, _ = await adapter.update_message_text(
        CHAT_ID, 1, "new text", datetime(2026, 1, 2), account_id=1, entities=BOLD, update_entities=True
    )
    assert outcome == "applied"
    row = await _row(adapter)
    raw = json.loads(row.raw_data)
    assert raw["entities"] == BOLD
    assert raw["webpage"] == {"url": "https://keep.example"}  # merge, not overwrite
    assert row.text == "new text"


@pytest.mark.asyncio
async def test_formatting_only_edit_merges_silently(adapter):
    """Same text + different entities: entities refresh, but NO edit_date bump
    (that would resurrect the phantom 'edited' marker #219 removed)."""
    outcome, _ = await adapter.update_message_text(
        CHAT_ID, 1, "original", datetime(2026, 1, 2), account_id=1, entities=ITALIC, update_entities=True
    )
    assert outcome == "noop"
    row = await _row(adapter)
    assert json.loads(row.raw_data)["entities"] == ITALIC
    assert row.edit_date is None


@pytest.mark.asyncio
async def test_edit_that_dropped_formatting_removes_the_key(adapter):
    await adapter.update_message_text(CHAT_ID, 1, "original", None, account_id=1, entities=BOLD, update_entities=True)
    outcome, _ = await adapter.update_message_text(
        CHAT_ID, 1, "original", None, account_id=1, entities=None, update_entities=True
    )
    assert outcome == "noop"
    raw = json.loads((await _row(adapter)).raw_data)
    assert "entities" not in raw
    assert raw["webpage"] == {"url": "https://keep.example"}


@pytest.mark.asyncio
async def test_legacy_caller_without_flag_leaves_raw_data_alone(adapter):
    outcome, _ = await adapter.update_message_text(CHAT_ID, 1, "newer", datetime(2026, 1, 3), account_id=1)
    assert outcome == "applied"
    raw = json.loads((await _row(adapter)).raw_data)
    assert "entities" not in raw
    assert raw["webpage"] == {"url": "https://keep.example"}
