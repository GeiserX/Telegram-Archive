"""Listener wiring tests for the outbound event webhook (#336).

Verifies the fire points — edit applied, soft delete, hard delete — build the
right context from the adapter's prior-state snapshots, that non-events
(noop edits, never-archived deletions, tombstone re-marks, filtered chats)
never fire, and that a webhook failure can never take down a handler.
Delivery mechanics live in test_event_webhook.py; here the sender's fire()
is spied on and the listener's own contract is what's under test.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from telethon import events

from src.event_webhook import DEFAULT_BODY_TEMPLATE, render_template
from src.listener import TelegramListener

CHAT_ID = -1001234567890


def _make_config(**overrides):
    """Mock Config mirroring test_listener_extended, plus real webhook attrs."""
    config = MagicMock()
    config.api_id = 12345
    config.api_hash = "test_hash"
    config.phone = "+1234567890"
    config.session_path = os.path.normpath("/tmp/test_session")
    config.media_path = os.path.normpath("/tmp/test_media")
    config.global_include_ids = set()
    config.private_include_ids = set()
    config.groups_include_ids = set()
    config.channels_include_ids = set()
    config.validate_credentials = MagicMock()
    config.max_filename_bytes = 255
    config.whitelist_mode = False
    config.chat_ids = set()
    config.listen_edits = True
    config.listen_deletions = True
    config.deletion_mode = "hard"
    config.listen_new_messages = True
    config.listen_new_messages_media = False
    config.listen_chat_actions = True
    config.skip_topic_ids = {}
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    config.should_download_media_for_chat = MagicMock(return_value=True)
    config.get_max_media_size_bytes = MagicMock(return_value=50 * 1024 * 1024)
    config.deduplicate_media = True
    account = MagicMock()
    account.index = 1
    account.label = "default"
    account.session_path = config.session_path
    account.api_id = config.api_id
    account.api_hash = config.api_hash
    account.phone = config.phone
    config.accounts = [account]
    # Real webhook config values (a bare MagicMock would be normalized to
    # disabled by the sender's type checks).
    config.event_webhook_enabled = True
    config.event_webhook_url = "https://hooks.example.test/secret"
    config.event_webhook_method = "POST"
    config.event_webhook_headers = {"Content-Type": "application/json; charset=utf-8"}
    config.event_webhook_events = {"message_edited", "message_deleted"}
    config.event_webhook_chat_ids = set()
    config.event_webhook_body_template = ""
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _make_db():
    db = AsyncMock()
    db.get_all_chats = AsyncMock(return_value=[])
    db.update_message_text = AsyncMock(return_value=("applied", None))
    db.delete_message = AsyncMock(return_value=None)
    db.mark_message_deleted = AsyncMock(return_value=None)
    db.resolve_message_chat_id = AsyncMock(return_value=None)
    db.get_chat_by_id = AsyncMock(return_value={"title": "Test Group"})
    db.close = AsyncMock()
    return db


def _make_listener(**config_overrides):
    config = _make_config(**config_overrides)
    db = _make_db()
    listener = TelegramListener(config, db, account_id=1)
    listener._notifier = None
    # Spy on the sender: wants() stays real (it reads the real config values
    # above), fire() is captured so tests can inspect the built context.
    listener._event_webhook.fire = MagicMock()
    return listener, db


def _make_listener_with_handlers(**config_overrides):
    listener, db = _make_listener(**config_overrides)
    listener._tracked_chat_ids = {CHAT_ID}
    handlers = {}
    mock_client = MagicMock()

    def capture_on(event_type):
        def decorator(fn):
            handlers[event_type] = fn
            return fn

        return decorator

    mock_client.on = capture_on
    listener.client = mock_client
    listener._register_handlers()
    return listener, handlers, db


def _edit_event(text="Updated text"):
    event = MagicMock()
    event.chat_id = CHAT_ID
    msg = MagicMock()
    msg.reply_to = None
    msg.id = 42
    msg.text = text
    msg.edit_date = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    msg.media = None
    msg.sender_id = 777
    event.message = msg
    return event


DELETION_SNAPSHOT = {
    "text": "deleted content",
    "sender_id": 555,
    "sender_name": "Ana",
    "date": datetime(2026, 8, 20, 9, 0),
    "is_deleted": 0,
    "media_type": "photo",
}


class TestEditFirePoint:
    def test_applied_edit_fires_with_prior_snapshot(self):
        listener, handlers, db = _make_listener_with_handlers()
        db.update_message_text = AsyncMock(
            return_value=("applied", {"text": "old text", "sender_id": 555, "sender_name": "Ana"})
        )

        asyncio.run(handlers[events.MessageEdited](_edit_event()))

        assert listener.stats["edits_applied"] == 1
        listener._event_webhook.fire.assert_called_once()
        event_name, context = listener._event_webhook.fire.call_args[0]
        assert event_name == "message_edited"
        assert context["chat_id"] == CHAT_ID
        assert context["account_id"] == 1
        assert context["chat_title"] == "Test Group"
        assert context["message_id"] == 42
        assert context["sender_id"] == 555
        assert context["sender_name"] == "Ana"
        assert context["old_text"] == "old text"
        assert context["new_text"] == "Updated text"
        assert context["text"] == "Updated text"
        assert context["media_type"] is None
        # The default template renders this context to valid JSON end to end.
        body = json.loads(render_template(DEFAULT_BODY_TEMPLATE, context))
        assert body["old_text"] == "old text"
        assert body["chat_id"] == CHAT_ID

    def test_noop_edit_does_not_fire(self):
        listener, handlers, db = _make_listener_with_handlers()
        db.update_message_text = AsyncMock(return_value=("noop", None))

        asyncio.run(handlers[events.MessageEdited](_edit_event()))

        listener._event_webhook.fire.assert_not_called()

    def test_disabled_webhook_never_fires(self):
        listener, handlers, db = _make_listener_with_handlers(event_webhook_enabled=False)
        db.update_message_text = AsyncMock(return_value=("applied", {"text": "old"}))

        asyncio.run(handlers[events.MessageEdited](_edit_event()))

        assert listener.stats["edits_applied"] == 1
        listener._event_webhook.fire.assert_not_called()

    def test_chat_filter_skips_other_chats(self):
        listener, handlers, db = _make_listener_with_handlers(event_webhook_chat_ids={-999})
        db.update_message_text = AsyncMock(return_value=("applied", {"text": "old"}))

        asyncio.run(handlers[events.MessageEdited](_edit_event()))

        assert listener.stats["edits_applied"] == 1
        listener._event_webhook.fire.assert_not_called()

    def test_fire_exception_never_reaches_the_handler(self):
        listener, handlers, db = _make_listener_with_handlers()
        db.update_message_text = AsyncMock(return_value=("applied", {"text": "old"}))
        listener._event_webhook.fire = MagicMock(side_effect=RuntimeError("boom"))

        asyncio.run(handlers[events.MessageEdited](_edit_event()))

        # The edit still applied and the failure was NOT counted as a listener
        # error: losing a ping is not an archiving failure.
        assert listener.stats["edits_applied"] == 1
        assert listener.stats["errors"] == 0

    def test_title_lookup_failure_is_swallowed(self):
        listener, handlers, db = _make_listener_with_handlers()
        db.update_message_text = AsyncMock(return_value=("applied", {"text": "old"}))
        db.get_chat_by_id = AsyncMock(side_effect=RuntimeError("db down"))

        asyncio.run(handlers[events.MessageEdited](_edit_event()))

        assert listener.stats["edits_applied"] == 1
        assert listener.stats["errors"] == 0
        listener._event_webhook.fire.assert_not_called()


class TestDeletionFirePoint:
    def test_hard_delete_fires_with_snapshot(self):
        listener, db = _make_listener()
        db.delete_message = AsyncMock(return_value=dict(DELETION_SNAPSHOT))

        asyncio.run(listener._apply_message_deletion(CHAT_ID, 42))

        listener._event_webhook.fire.assert_called_once()
        event_name, context = listener._event_webhook.fire.call_args[0]
        assert event_name == "message_deleted"
        assert context["text"] == "deleted content"
        assert context["sender_name"] == "Ana"
        assert context["media_type"] == "photo"
        assert context["chat_title"] == "Test Group"
        assert isinstance(context["date"], datetime)
        # Deletions carry no old/new text; the template renders them blank.
        body = json.loads(render_template(DEFAULT_BODY_TEMPLATE, context))
        assert body["old_text"] == ""
        assert body["text"] == "deleted content"

    def test_soft_delete_fires_with_snapshot_and_event_time(self):
        listener, db = _make_listener(deletion_mode="soft")
        db.mark_message_deleted = AsyncMock(return_value=dict(DELETION_SNAPSHOT))

        asyncio.run(listener._apply_message_deletion(CHAT_ID, 42))

        listener._event_webhook.fire.assert_called_once()
        _, context = listener._event_webhook.fire.call_args[0]
        assert context["text"] == "deleted content"
        deleted_at_kwarg = db.mark_message_deleted.call_args.kwargs["deleted_at"]
        assert context["date"] == deleted_at_kwarg

    def test_already_tombstoned_re_mark_does_not_fire(self):
        listener, db = _make_listener(deletion_mode="soft")
        db.mark_message_deleted = AsyncMock(return_value={**DELETION_SNAPSHOT, "is_deleted": 1})

        asyncio.run(listener._apply_message_deletion(CHAT_ID, 42))

        listener._event_webhook.fire.assert_not_called()

    def test_never_archived_message_does_not_fire(self):
        listener, db = _make_listener()
        db.delete_message = AsyncMock(return_value=None)

        asyncio.run(listener._apply_message_deletion(CHAT_ID, 42))

        listener._event_webhook.fire.assert_not_called()


class TestWebhookLogHygiene:
    def test_no_chat_id_or_content_in_logs(self, caplog):
        listener, handlers, db = _make_listener_with_handlers()
        db.update_message_text = AsyncMock(return_value=("applied", {"text": "secret old text"}))
        listener._event_webhook.fire = MagicMock(side_effect=RuntimeError(f"leaks {CHAT_ID}"))

        with caplog.at_level(logging.DEBUG, logger="src.listener"):
            asyncio.run(handlers[events.MessageEdited](_edit_event(text="secret new text")))

        for record in caplog.records:
            rendered = record.getMessage()
            assert str(CHAT_ID) not in rendered
            assert "secret old text" not in rendered
            assert "secret new text" not in rendered
            assert "Test Group" not in rendered
