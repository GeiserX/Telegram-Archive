"""Real-time reaction listener handler tests (#219)."""

import asyncio
from datetime import datetime
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
    config.reaction_resweep_days = 0
    config.reaction_resweep_max_per_chat = 500
    config.skip_topic_ids = {}
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    return config


def _reactions(*pairs, is_min=False):
    obj = SimpleNamespace(results=[SimpleNamespace(reaction=SimpleNamespace(emoticon=e), count=c) for e, c in pairs])
    obj.min = is_min
    return obj


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


def _build_all(listen_reactions=True, listen_edits=True, listen_new_messages=True):
    """Build a listener exposing every captured handler by name (#221).

    Mirrors ``_build`` but returns the full handler dict so the edit-vector and
    new-message reaction paths can be exercised alongside the reaction handler.
    """
    db = AsyncMock()
    db.reconcile_reactions = AsyncMock(return_value="reconciled")
    db.update_message_text = AsyncMock(return_value="applied")
    db.insert_message = AsyncMock()
    config = _config(listen_reactions)
    config.listen_edits = listen_edits
    config.listen_new_messages = listen_new_messages
    listener = TelegramListener(config, db)
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
    return listener, handlers, db


def _edit_event(msg_id=42, text="hi", reactions=None):
    message = SimpleNamespace(id=msg_id, text=text, edit_date=None, reactions=reactions, reply_to=None)
    return SimpleNamespace(chat_id=TRACKED, message=message)


class TestEditVectorReactions:
    """Reaction changes delivered as edit events feed the same debounce buffer (#221)."""

    def test_reaction_only_edit_buffers_before_noop_return(self):
        # A reaction-only edit leaves text unchanged, so update_message_text is a
        # noop and the handler early-returns; reactions must be buffered BEFORE that.
        listener, handlers, db = _build_all()
        db.update_message_text = AsyncMock(return_value="noop")
        asyncio.run(handlers["on_message_edited"](_edit_event(reactions=_reactions(("👍", 2)))))
        assert listener._reaction_pending[(TRACKED, 42)] == [{"emoji": "👍", "count": 2}]

    def test_edit_with_reactions_flush_reconciles(self):
        listener, handlers, db = _build_all()
        asyncio.run(handlers["on_message_edited"](_edit_event(reactions=_reactions(("🔥", 1)))))
        asyncio.run(listener._flush_reactions())
        db.reconcile_reactions.assert_awaited_once_with(42, TRACKED, [{"emoji": "🔥", "count": 1}], mark_removed=True)

    def test_edit_without_reactions_buffers_empty_snapshot(self):
        # extract_reactions(None) == [] is a valid empty snapshot: an edit whose
        # message carries no reactions object means the message has zero reactions.
        listener, handlers, _db = _build_all()
        asyncio.run(handlers["on_message_edited"](_edit_event(reactions=None)))
        assert listener._reaction_pending[(TRACKED, 42)] == []

    def test_min_payload_not_buffered(self):
        listener, handlers, _db = _build_all()
        asyncio.run(handlers["on_message_edited"](_edit_event(reactions=_reactions(("👍", 2), is_min=True))))
        assert listener._reaction_pending == {}

    def test_listen_reactions_false_not_buffered(self):
        listener, handlers, _db = _build_all(listen_reactions=False)
        asyncio.run(handlers["on_message_edited"](_edit_event(reactions=_reactions(("👍", 2)))))
        assert listener._reaction_pending == {}


def _new_message_event(msg_id=77, reactions=None):
    message = SimpleNamespace(
        id=msg_id,
        text="hello",
        edit_date=None,
        reactions=reactions,
        reply_to=None,
        sender=None,
        sender_id=1,
        date=datetime(2026, 7, 18, 12, 0),
        out=False,
        media=None,
        grouped_id=None,
        reply_to_msg_id=None,
    )
    event = SimpleNamespace(chat_id=TRACKED, message=message)
    event.get_chat = AsyncMock(return_value=None)
    return event


class TestNewMessageReactions:
    """New messages can arrive already carrying reactions and must be buffered (#221)."""

    def test_new_message_with_reactions_buffered(self):
        listener, handlers, db = _build_all()
        asyncio.run(handlers["on_new_message"](_new_message_event(reactions=_reactions(("👍", 1)))))
        db.insert_message.assert_awaited_once()
        assert listener._reaction_pending[(TRACKED, 77)] == [{"emoji": "👍", "count": 1}]

    def test_new_message_min_payload_not_buffered(self):
        listener, handlers, _db = _build_all()
        asyncio.run(handlers["on_new_message"](_new_message_event(reactions=_reactions(("👍", 1), is_min=True))))
        assert listener._reaction_pending == {}

    def test_new_message_reactions_disabled_not_buffered(self):
        listener, handlers, _db = _build_all(listen_reactions=False)
        asyncio.run(handlers["on_new_message"](_new_message_event(reactions=_reactions(("👍", 1)))))
        assert listener._reaction_pending == {}
