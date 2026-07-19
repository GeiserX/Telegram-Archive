"""Self-reaction recovery tests (#221).

Covers the three mitigations for reactions Telegram never pushes to the archive's
own session:

- config knobs ``REACTION_RESWEEP_DAYS`` / ``REACTION_RESWEEP_MAX_PER_CHAT``
  (defaults, parsing, clamping);
- the ``get_message_ids_since`` read helper (window + cap + newest-first order),
  on real SQLite;
- ``TelegramBackup._resweep_reactions`` (primary ``GetMessagesReactionsRequest``
  path with per-chunk fallback to ``get_messages``, min/None guards, echo-only
  reconcile, opt-in gate in ``_backup_dialog``, aggregate-only logging);
- the piggyback reconcile inside ``_sync_deletions_and_edits``.
"""

import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telethon.tl.types import PeerChannel, UpdateMessageReactions

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Message
from src.telegram_backup import TelegramBackup

CHAT = -100


def _reactions(*pairs, is_min=False):
    """Build a MessageReactions-like snapshot matching extract_reactions' shape."""
    obj = SimpleNamespace(results=[SimpleNamespace(reaction=SimpleNamespace(emoticon=e), count=c) for e, c in pairs])
    obj.min = is_min
    return obj


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


class TestReactionResweepConfig:
    def _config(self, **env):
        base = {"CHAT_TYPES": "private"}
        base.update(env)
        with patch("os.makedirs"), patch.dict(os.environ, base, clear=True):
            return Config()

    def test_defaults_disabled(self):
        c = self._config()
        assert c.reaction_resweep_days == 0.0
        assert c.reaction_resweep_max_per_chat == 500

    def test_parses_values(self):
        c = self._config(REACTION_RESWEEP_DAYS="7", REACTION_RESWEEP_MAX_PER_CHAT="250")
        assert c.reaction_resweep_days == 7.0
        assert c.reaction_resweep_max_per_chat == 250

    def test_negative_days_clamped_to_zero(self):
        assert self._config(REACTION_RESWEEP_DAYS="-5").reaction_resweep_days == 0.0

    def test_max_per_chat_floored_at_one(self):
        assert self._config(REACTION_RESWEEP_MAX_PER_CHAT="0").reaction_resweep_max_per_chat == 1


# ---------------------------------------------------------------------------
# get_message_ids_since (real SQLite)
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

    yield DatabaseAdapter(db_manager)
    await engine.dispose()


class TestGetMessageIdsSince:
    async def test_window_excludes_old_and_other_chats(self, adapter):
        now = datetime(2026, 7, 18, 12, 0)
        async with adapter.db_manager.async_session_factory() as session:
            session.add(Message(id=1, chat_id=CHAT, date=now - timedelta(days=1), text="a"))
            session.add(Message(id=2, chat_id=CHAT, date=now - timedelta(days=3), text="b"))
            session.add(Message(id=3, chat_id=CHAT, date=now - timedelta(days=5), text="c"))
            session.add(Message(id=9, chat_id=CHAT, date=now - timedelta(days=30), text="old"))  # before cutoff
            session.add(Message(id=99, chat_id=-200, date=now, text="other"))  # different chat
            await session.commit()

        ids = await adapter.get_message_ids_since(CHAT, now - timedelta(days=7), 500)
        assert ids == [3, 2, 1]  # newest id first; old + other-chat excluded

    async def test_cap_limits_results_newest_first(self, adapter):
        base = datetime(2026, 7, 18, 12, 0)
        async with adapter.db_manager.async_session_factory() as session:
            for i in range(1, 11):
                session.add(Message(id=i, chat_id=CHAT, date=base, text="x"))
            await session.commit()

        ids = await adapter.get_message_ids_since(CHAT, base - timedelta(days=1), 3)
        assert ids == [10, 9, 8]

    async def test_empty_when_nothing_in_window(self, adapter):
        base = datetime(2026, 7, 18, 12, 0)
        async with adapter.db_manager.async_session_factory() as session:
            session.add(Message(id=1, chat_id=CHAT, date=base - timedelta(days=100), text="a"))
            await session.commit()
        assert await adapter.get_message_ids_since(CHAT, base - timedelta(days=7), 500) == []


# ---------------------------------------------------------------------------
# _resweep_reactions
# ---------------------------------------------------------------------------


def _backup(days=7.0, max_per_chat=500):
    b = TelegramBackup.__new__(TelegramBackup)
    b.config = MagicMock()
    b.config.reaction_resweep_days = days
    b.config.reaction_resweep_max_per_chat = max_per_chat
    b.config.deletion_mode = "hard"
    b.db = AsyncMock()
    b.db.reconcile_reactions = AsyncMock(return_value="reconciled")
    # b.client is awaited directly for the raw request (await client(request)).
    b.client = AsyncMock()
    b.client.get_messages = AsyncMock()
    return b


class TestResweepReactions:
    async def test_no_recent_ids_makes_no_calls(self):
        # _resweep_reactions is only reached via the _backup_dialog days>0 gate
        # (covered by test_disabled_days_zero_skips_resweep); prove it also never
        # touches the API when the window selects nothing.
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[])
        await b._resweep_reactions(MagicMock(), CHAT)
        b.client.assert_not_awaited()
        b.db.reconcile_reactions.assert_not_awaited()

    async def test_chunks_250_ids_into_3_requests(self):
        b = _backup()
        ids = list(range(1, 251))
        b.db.get_message_ids_since = AsyncMock(return_value=ids)
        b.client.return_value = SimpleNamespace(updates=[])  # nothing echoed → nothing reconciled
        await b._resweep_reactions(MagicMock(), CHAT)
        assert b.client.await_count == 3
        # Each request must carry exactly its 100-id slice (review finding: the
        # payload was never asserted, only the request count).
        sent = [call.args[0].id for call in b.client.await_args_list]
        assert [len(chunk) for chunk in sent] == [100, 100, 50]
        assert [i for chunk in sent for i in chunk] == ids
        b.client.get_messages.assert_not_awaited()

    async def test_reconciles_only_echoed_updates(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[10, 11, 12])
        peer = PeerChannel(123)
        b.client.return_value = SimpleNamespace(
            updates=[
                UpdateMessageReactions(peer=peer, msg_id=10, reactions=_reactions(("👍", 3))),
                SimpleNamespace(msg_id=99),  # foreign update type → ignored by isinstance guard
                UpdateMessageReactions(peer=peer, msg_id=12, reactions=_reactions(("🔥", 1))),
            ]
        )
        await b._resweep_reactions(MagicMock(), CHAT)

        reconciled = {c.args[0]: c.args[2] for c in b.db.reconcile_reactions.await_args_list}
        assert set(reconciled) == {10, 12}  # id 11 absent from response → left untouched
        assert reconciled[10] == [{"emoji": "👍", "count": 3}]
        assert all(c.kwargs.get("mark_removed") is True for c in b.db.reconcile_reactions.await_args_list)
        b.client.get_messages.assert_not_awaited()

    async def test_skips_min_payload(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[10])
        peer = PeerChannel(123)
        b.client.return_value = SimpleNamespace(
            updates=[UpdateMessageReactions(peer=peer, msg_id=10, reactions=_reactions(("👍", 3), is_min=True))]
        )
        await b._resweep_reactions(MagicMock(), CHAT)
        b.db.reconcile_reactions.assert_not_awaited()

    async def test_falls_back_to_get_messages_on_primary_error(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[10, 11])
        # A non-RPC exception is NOT retried by call_with_flood_retry, so it
        # propagates immediately (no sleeps) and triggers the per-chunk fallback.
        b.client.side_effect = RuntimeError("MSG_ID_INVALID")
        msg10 = SimpleNamespace(id=10, reactions=_reactions(("👍", 2)))
        b.client.get_messages = AsyncMock(return_value=[msg10, None])  # None placeholder for id 11
        await b._resweep_reactions(MagicMock(), CHAT)

        b.client.get_messages.assert_awaited_once()
        b.db.reconcile_reactions.assert_awaited_once()
        args = b.db.reconcile_reactions.await_args
        assert args.args[0] == 10
        assert args.args[2] == [{"emoji": "👍", "count": 2}]

    async def test_fallback_reactions_none_reconciles_to_zero(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[10])
        b.client.side_effect = RuntimeError("boom")
        # reactions=None on a fetched message is a definitive empty snapshot.
        b.client.get_messages = AsyncMock(return_value=[SimpleNamespace(id=10, reactions=None)])
        await b._resweep_reactions(MagicMock(), CHAT)

        b.db.reconcile_reactions.assert_awaited_once()
        assert b.db.reconcile_reactions.await_args.args[2] == []  # → tombstone to zero

    async def test_fallback_skips_min_payload(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[10])
        b.client.side_effect = RuntimeError("boom")
        b.client.get_messages = AsyncMock(
            return_value=[SimpleNamespace(id=10, reactions=_reactions(("👍", 2), is_min=True))]
        )
        await b._resweep_reactions(MagicMock(), CHAT)
        b.db.reconcile_reactions.assert_not_awaited()

    async def test_logs_aggregate_only_never_chat_id(self, caplog):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[10])
        peer = PeerChannel(123)
        b.client.return_value = SimpleNamespace(
            updates=[UpdateMessageReactions(peer=peer, msg_id=10, reactions=_reactions(("👍", 3)))]
        )
        with caplog.at_level(logging.INFO, logger="src.telegram_backup"):
            await b._resweep_reactions(MagicMock(), CHAT)

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "Reaction resweep: checked 1 ids, reconciled 1" in text
        assert str(CHAT) not in text  # never log the chat id (PII)


# ---------------------------------------------------------------------------
# Opt-in gate in _backup_dialog
# ---------------------------------------------------------------------------


def _dialog_backup(days):
    b = TelegramBackup.__new__(TelegramBackup)
    b.config = MagicMock()
    b.config.batch_size = 100
    b.config.checkpoint_interval = 1
    b.config.skip_media_chat_ids = set()
    b.config.skip_media_delete_existing = False
    b.config.sync_deletions_edits = False
    b.config.reaction_resweep_days = days
    b.config.should_skip_topic = MagicMock(return_value=False)
    b.db = AsyncMock()
    b.db.get_last_message_id = AsyncMock(return_value=0)
    b._get_marked_id = MagicMock(return_value=CHAT)
    b._extract_chat_data = MagicMock(return_value={"id": CHAT})
    b._ensure_profile_photo = AsyncMock()
    b._sync_pinned_messages = AsyncMock()
    b._resweep_reactions = AsyncMock()
    b._cleaned_media_chats = set()
    b.client = MagicMock()

    async def _empty_iter(*args, **kwargs):
        return
        yield  # noqa: RET503

    b.client.iter_messages = _empty_iter
    dialog = MagicMock()
    dialog.entity = MagicMock()
    return b, dialog


class TestResweepGate:
    async def test_disabled_days_zero_skips_resweep(self):
        b, dialog = _dialog_backup(0)
        await b._backup_dialog(dialog)
        b._resweep_reactions.assert_not_awaited()

    async def test_enabled_days_positive_runs_resweep(self):
        b, dialog = _dialog_backup(7)
        await b._backup_dialog(dialog)
        b._resweep_reactions.assert_awaited_once()


# ---------------------------------------------------------------------------
# Piggyback reconcile inside _sync_deletions_and_edits (zero extra API cost)
# ---------------------------------------------------------------------------


class TestPiggybackReconcile:
    async def test_reconciles_reactions_on_synced_message(self):
        b = _backup()
        b.db.get_messages_sync_data = AsyncMock(return_value={10: None})
        remote = SimpleNamespace(id=10, edit_date=None, message="x", reactions=_reactions(("👍", 4)))
        b.client.get_messages = AsyncMock(return_value=[remote])

        await b._sync_deletions_and_edits(CHAT, MagicMock())

        b.db.reconcile_reactions.assert_awaited_once()
        args = b.db.reconcile_reactions.await_args
        assert args.args[0] == 10
        assert args.args[2] == [{"emoji": "👍", "count": 4}]
        assert args.kwargs.get("mark_removed") is True

    async def test_skips_min_payload_on_synced_message(self):
        b = _backup()
        b.db.get_messages_sync_data = AsyncMock(return_value={10: None})
        remote = SimpleNamespace(id=10, edit_date=None, message="x", reactions=_reactions(("👍", 4), is_min=True))
        b.client.get_messages = AsyncMock(return_value=[remote])

        await b._sync_deletions_and_edits(CHAT, MagicMock())
        b.db.reconcile_reactions.assert_not_awaited()
