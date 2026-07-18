"""Real-time reaction listener handler tests (#219)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.listener import TelegramListener

TRACKED = -1001234567890


def _config(listen_reactions=True):
    config = MagicMock()
    config.api_id = 12345
    config.api_hash = "test_hash"
    config.phone = "+1234567890"
    config.session_path = "/tmp/test_session"
    config.validate_credentials = MagicMock()
    config.whitelist_mode = False
    config.chat_ids = set()
    config.listen_edits = True
    config.listen_deletions = False
    config.deletion_mode = "hard"
    config.listen_new_messages = True
    config.listen_new_messages_media = False
    config.listen_chat_actions = False
    config.listen_reactions = listen_reactions
    config.reaction_debounce_seconds = 0.01
    config.skip_topic_ids = {}
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    return config


def _reactions(*pairs):
    return SimpleNamespace(results=[SimpleNamespace(reaction=SimpleNamespace(emoticon=e), count=c) for e, c in pairs])


def _build(listen_reactions=True):
    db = AsyncMock()
    db.reconcile_reactions = AsyncMock(return_value="reconciled")
    listener = TelegramListener(_config(listen_reactions), db)
    listener._tracked_chat_ids = {TRACKED}
    listener._get_marked_id = MagicMock(return_value=TRACKED)
    listener._notifier = None

    handlers = {}

    def capture_on(event_type):
        def decorator(fn):
            handlers[fn.__name__] = fn
            return fn

        return decorator

    client = MagicMock()
    client.on = capture_on
    listener.client = client
    listener._register_handlers()
    return listener, handlers["on_message_reactions"], db


def _event(msg_id=42, reactions=None, top_msg_id=None):
    return SimpleNamespace(peer=SimpleNamespace(), msg_id=msg_id, reactions=reactions, top_msg_id=top_msg_id)


class TestReactionHandler:
    def test_buffers_snapshot(self):
        listener, handler, _db = _build()
        asyncio.run(handler(_event(reactions=_reactions(("👍", 2)))))
        assert listener._reaction_pending[(TRACKED, 42)] == [{"emoji": "👍", "count": 2}]
        assert listener.stats["reactions_received"] == 1

    def test_disabled_when_flag_false(self):
        listener, handler, _db = _build(listen_reactions=False)
        asyncio.run(handler(_event(reactions=_reactions(("👍", 2)))))
        assert listener._reaction_pending == {}

    def test_coalesces_latest_snapshot(self):
        listener, handler, _db = _build()
        asyncio.run(handler(_event(reactions=_reactions(("👍", 1)))))
        asyncio.run(handler(_event(reactions=_reactions(("👍", 5), ("🔥", 2)))))
        # Only the latest full snapshot is kept for that message.
        assert listener._reaction_pending[(TRACKED, 42)] == [{"emoji": "👍", "count": 5}, {"emoji": "🔥", "count": 2}]

    def test_untracked_chat_skipped(self):
        listener, handler, _db = _build()
        listener._get_marked_id = MagicMock(return_value=-999)
        asyncio.run(handler(_event(reactions=_reactions(("👍", 1)))))
        assert listener._reaction_pending == {}

    def test_excluded_topic_skipped(self):
        listener, handler, _db = _build()
        listener.config.should_skip_topic = MagicMock(return_value=True)
        asyncio.run(handler(_event(reactions=_reactions(("👍", 1)), top_msg_id=7)))
        assert listener._reaction_pending == {}
        listener.config.should_skip_topic.assert_called_once_with(TRACKED, 7)

    def test_flush_reconciles_and_notifies(self):
        listener, handler, db = _build()
        listener._notify_update = AsyncMock()
        asyncio.run(handler(_event(reactions=_reactions(("👍", 3)))))
        asyncio.run(listener._flush_reactions())

        db.reconcile_reactions.assert_awaited_once_with(42, TRACKED, [{"emoji": "👍", "count": 3}], mark_removed=True)
        listener._notify_update.assert_awaited_once()
        args = listener._notify_update.await_args[0]
        assert args[0] == "reaction"
        assert args[1]["message_id"] == 42
        assert args[1]["reactions"] == [{"emoji": "👍", "count": 3}]
        assert listener.stats["reactions_applied"] == 1
        assert listener._reaction_pending == {}  # buffer drained

    def test_flush_noop_outcome_does_not_notify(self):
        listener, handler, db = _build()
        db.reconcile_reactions = AsyncMock(return_value="noop")
        listener._notify_update = AsyncMock()
        asyncio.run(handler(_event(reactions=_reactions(("👍", 3)))))
        asyncio.run(listener._flush_reactions())
        listener._notify_update.assert_not_awaited()

    def test_handler_errors_do_not_propagate(self):
        listener, handler, _db = _build()
        listener._get_marked_id = MagicMock(side_effect=RuntimeError("boom"))
        asyncio.run(handler(_event(reactions=_reactions(("👍", 1)))))  # must not raise
        assert listener.stats["errors"] == 1


@pytest.mark.asyncio
async def test_notify_update_maps_reaction_type():
    listener, _handler, _db = _build()
    listener._notifier = MagicMock()
    listener._notifier.notify = AsyncMock()
    await listener._notify_update("reaction", {"chat_id": TRACKED, "message_id": 42, "reactions": []})
    listener._notifier.notify.assert_awaited_once()
    from src.realtime import NotificationType

    assert listener._notifier.notify.await_args[0][0] == NotificationType.REACTION
