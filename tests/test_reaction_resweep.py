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

import json
import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel, UpdateMessageReactions

from src.config import Config
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Message
from src.message_utils import utcnow_naive
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

    def test_batch_delay_default_and_clamp(self):
        # #224: global inter-request spacing, default 2s, negative clamps to 0.
        assert self._config().reaction_resweep_batch_delay_seconds == 2.0
        assert self._config(REACTION_RESWEEP_BATCH_DELAY_SECONDS="7.5").reaction_resweep_batch_delay_seconds == 7.5
        assert self._config(REACTION_RESWEEP_BATCH_DELAY_SECONDS="-1").reaction_resweep_batch_delay_seconds == 0.0


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


def _backup(days=7.0, max_per_chat=500, delay=0.0):
    b = TelegramBackup.__new__(TelegramBackup)
    b.config = MagicMock()
    b.config.reaction_resweep_days = days
    b.config.reaction_resweep_max_per_chat = max_per_chat
    b.config.reaction_resweep_batch_delay_seconds = delay
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


# ---------------------------------------------------------------------------
# #224: burst-rate pacing, flood deferral, and the chat-keyed cycle cursor
# ---------------------------------------------------------------------------


class TestResweepPacing:
    """getMessagesReactions floods accumulate ACROSS chats (#224): requests are
    globally spaced, the first FloodWait defers the rest of the run (no retry
    burn, no fallback onto a second bucket), and a persisted cycle cursor lets
    the next run resume with the chats that were deferred."""

    async def test_requests_paced_globally_across_chats(self, monkeypatch):
        b = _backup(delay=10.0)
        clock = {"t": 100.0}
        monkeypatch.setattr("src.telegram_backup.time", SimpleNamespace(monotonic=lambda: clock["t"]))
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(round(s, 6))

        monkeypatch.setattr("src.telegram_backup.asyncio.sleep", fake_sleep)
        b.db.get_message_ids_since = AsyncMock(return_value=[1])
        b.client.return_value = SimpleNamespace(updates=[])

        await b._resweep_reactions(MagicMock(), CHAT)  # cold start: elapsed >> delay, no sleep
        await b._resweep_reactions(MagicMock(), -200)  # immediately after: full delay owed

        assert sleeps == [10.0]
        assert b.client.await_count == 2

    async def test_no_sleep_when_delay_disabled(self, monkeypatch):
        b = _backup(delay=0.0)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr("src.telegram_backup.asyncio.sleep", fake_sleep)
        b.db.get_message_ids_since = AsyncMock(return_value=[1])
        b.client.return_value = SimpleNamespace(updates=[])
        await b._resweep_reactions(MagicMock(), CHAT)
        assert sleeps == []

    async def test_flood_on_raw_pauses_without_fallback(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[1, 2])
        b.client.side_effect = FloodWaitError(request=None, capture=60)

        await b._resweep_reactions(MagicMock(), CHAT)

        assert b._resweep_flood_until is not None  # cooldown recorded, no sleep, no retry
        b.client.get_messages.assert_not_awaited()  # flood must NOT fall back (second bucket)
        assert CHAT not in b._resweep_cycle_done  # deferred, not done

        # Chats reached while still cooling down: no queries, no API calls — just counted.
        b.db.get_message_ids_since.reset_mock()
        await b._resweep_reactions(MagicMock(), -200)
        b.db.get_message_ids_since.assert_not_awaited()
        assert b._resweep_dialogs_deferred == 2
        assert b._resweep_deferred_any is True

    async def test_flood_on_fallback_also_pauses(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[1])
        b.client.side_effect = RuntimeError("peer unsupported")  # raw path → genuine non-flood error
        b.client.get_messages = AsyncMock(side_effect=FloodWaitError(request=None, capture=60))

        await b._resweep_reactions(MagicMock(), CHAT)

        assert b._resweep_flood_until is not None
        assert CHAT not in b._resweep_cycle_done

    async def test_resweep_resumes_within_run_after_cooldown(self, monkeypatch):
        # #224 follow-up: a FloodWait pauses the re-sweep, and once wall-clock
        # passes the server-requested window the SAME run resumes — full
        # deferral to the next run only happens if the window outlives the run.
        clock = {"t": 1000.0}
        monkeypatch.setattr("src.telegram_backup.time", SimpleNamespace(monotonic=lambda: clock["t"]))
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[1])
        b.client.side_effect = [FloodWaitError(request=None, capture=50), SimpleNamespace(updates=[])]

        await b._resweep_reactions(MagicMock(), CHAT)  # floods → cooldown until 1052
        assert b._resweep_flood_until == 1000.0 + 50 + 2.0

        await b._resweep_reactions(MagicMock(), -200)  # still cooling → skipped
        assert b._resweep_dialogs_deferred == 2

        clock["t"] = 1060.0  # window elapsed
        await b._resweep_reactions(MagicMock(), -300)  # resumes in the same run
        assert b._resweep_flood_until is None
        assert -300 in b._resweep_cycle_done
        assert b._resweep_dialogs_deferred == 2  # the resumed chat was NOT deferred

    async def test_hard_defer_after_repeated_floods(self, monkeypatch):
        # Repeated floods in one run signal a degraded bucket: after the cap,
        # the rest of the run defers outright instead of poking it again.
        clock = {"t": 1000.0}
        monkeypatch.setattr("src.telegram_backup.time", SimpleNamespace(monotonic=lambda: clock["t"]))
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[1])
        b.client.side_effect = FloodWaitError(request=None, capture=10)  # floods on every call

        for n, chat in enumerate((CHAT, -200, -300), start=1):
            await b._resweep_reactions(MagicMock(), chat)
            assert b._resweep_flood_count == n
            clock["t"] += 100  # let each cooldown elapse so the next chat retries

        assert b._resweep_hard_deferred is True
        assert b._resweep_flood_until is None

        # Once hard-deferred: no queries, no API calls, regardless of the clock.
        b.db.get_message_ids_since.reset_mock()
        await b._resweep_reactions(MagicMock(), -400)
        b.db.get_message_ids_since.assert_not_awaited()
        assert b._resweep_dialogs_deferred == 4

    async def test_completed_chat_marked_and_skipped_within_cycle(self):
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=[1])
        b.client.return_value = SimpleNamespace(updates=[])

        await b._resweep_reactions(MagicMock(), CHAT)
        assert CHAT in b._resweep_cycle_done

        b.db.get_message_ids_since.reset_mock()
        b.client.reset_mock()
        await b._resweep_reactions(MagicMock(), CHAT)  # same cycle: skipped entirely
        b.db.get_message_ids_since.assert_not_awaited()
        b.client.assert_not_awaited()

    def _cursor(self, days=7.0, age_hours=0, done=(123, 456), partial=None):
        return json.dumps(
            {
                "saved_at": (utcnow_naive() - timedelta(hours=age_hours)).isoformat(),
                "days": days,
                "done": list(done),
                "partial": {str(c): n for c, n in (partial or {}).items()},
            }
        )

    async def test_cycle_state_loads_persists_and_clears(self):
        b = _backup()
        b.db.get_metadata = AsyncMock(return_value=self._cursor(partial={999: 200}))
        await b._load_resweep_cycle()
        # The load must read the SAME key the persist writes (key-pinned both ways).
        b.db.get_metadata.assert_awaited_once_with("reaction_resweep_cycle_done")
        assert b._resweep_cycle_done == {123, 456}
        assert b._resweep_partial == {999: 200}

        b._resweep_deferred_any = True
        b._resweep_cycle_done.add(789)
        b._resweep_dialogs_deferred = 3
        b.db.set_metadata = AsyncMock()
        await b._finalize_resweep_cycle()
        key, payload = b.db.set_metadata.await_args.args
        assert key == "reaction_resweep_cycle_done"
        state = json.loads(payload)
        assert state["done"] == [123, 456, 789]
        assert state["partial"] == {"999": 200}
        assert state["days"] == 7.0
        assert "saved_at" in state

        b._resweep_deferred_any = False
        b.db.set_metadata.reset_mock()
        await b._finalize_resweep_cycle()  # clean run: cycle complete, cursor cleared
        b.db.set_metadata.assert_awaited_once_with("reaction_resweep_cycle_done", "{}")

    async def test_finalize_deferred_with_nothing_completed(self):
        # Deferred on the very first dialog: nothing done, nothing partial —
        # the persisted state must still be valid and loadable.
        b = _backup()
        b._ensure_resweep_state()
        b._resweep_deferred_any = True
        b._resweep_dialogs_deferred = 1
        b.db.set_metadata = AsyncMock()
        await b._finalize_resweep_cycle()
        _key, payload = b.db.set_metadata.await_args.args
        state = json.loads(payload)
        assert state["done"] == []
        assert state["partial"] == {}

    async def test_cycle_state_discarded_when_window_changed_or_stale(self):
        # days mismatch: coverage from a different window is meaningless.
        b = _backup(days=3.0)
        b.db.get_metadata = AsyncMock(return_value=self._cursor(days=7.0))
        await b._load_resweep_cycle()
        assert b._resweep_cycle_done == set()

        # older than 48h (e.g. feature disabled then re-enabled weeks later).
        b2 = _backup()
        b2.db.get_metadata = AsyncMock(return_value=self._cursor(age_hours=72))
        await b2._load_resweep_cycle()
        assert b2._resweep_cycle_done == set()

    async def test_cycle_state_corrupt_or_legacy_tolerated(self):
        b = _backup()
        b.db.get_metadata = AsyncMock(return_value="{not json")
        await b._load_resweep_cycle()  # must not raise
        assert b._resweep_cycle_done == set()

        b2 = _backup()
        b2.db.get_metadata = AsyncMock(return_value="[123, 456]")  # legacy bare-list shape
        await b2._load_resweep_cycle()
        assert b2._resweep_cycle_done == set()

    async def test_cycle_state_ignored_when_resweep_disabled(self):
        b = _backup(days=0)
        b.db.get_metadata = AsyncMock()
        b.db.set_metadata = AsyncMock()
        await b._load_resweep_cycle()
        await b._finalize_resweep_cycle()
        b.db.get_metadata.assert_not_awaited()
        b.db.set_metadata.assert_not_awaited()

    async def test_flood_mid_chat_records_partial_progress(self):
        # 250 ids, flood on the SECOND chunk: the chat is NOT done, and the
        # cursor records that the newest 100 ids were covered so the next run
        # resumes there instead of flooding at the same chunk forever.
        b = _backup()
        b.db.get_message_ids_since = AsyncMock(return_value=list(range(1, 251)))
        b.client.side_effect = [SimpleNamespace(updates=[]), FloodWaitError(request=None, capture=60)]

        await b._resweep_reactions(MagicMock(), CHAT)

        assert b._resweep_flood_until is not None
        assert CHAT not in b._resweep_cycle_done
        assert b._resweep_partial == {CHAT: 100}

    async def test_partial_chat_resumes_and_completes_next_run(self):
        # Cursor says the newest 100 of this chat were covered: only the
        # remaining 150 are fetched-through (2 requests), then the chat is done
        # and its partial entry is dropped.
        b = _backup()
        b._ensure_resweep_state()
        b._resweep_partial[CHAT] = 100
        b.db.get_message_ids_since = AsyncMock(return_value=list(range(1, 251)))
        b.client.return_value = SimpleNamespace(updates=[])

        await b._resweep_reactions(MagicMock(), CHAT)

        assert b.client.await_count == 2
        sent = [call.args[0].id for call in b.client.await_args_list]
        assert [len(chunk) for chunk in sent] == [100, 50]
        assert CHAT in b._resweep_cycle_done
        assert CHAT not in b._resweep_partial

    async def test_done_chat_after_flood_not_counted_deferred(self):
        # Gate order: a chat already covered this cycle is "done", not
        # "deferred", even when encountered while hard-deferred.
        b = _backup()
        b._ensure_resweep_state()
        b._resweep_cycle_done.add(CHAT)
        b._resweep_hard_deferred = True
        b._resweep_dialogs_deferred = 1

        await b._resweep_reactions(MagicMock(), CHAT)

        assert b._resweep_dialogs_deferred == 1  # unchanged
