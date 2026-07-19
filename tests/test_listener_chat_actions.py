"""Real-time chat-action listener handler tests (#222).

The rewritten ``on_chat_action`` handler archives the REAL service message (its
actual Telegram id and date) instead of fabricating a wall-clock-derived id,
classifies the action from the ``MessageAction`` class using the shared sweep
vocabulary (``service_action_type``), and skips participant-only sync events
that carry no service message. These tests lock that contract in.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telethon import events
from telethon.tl.types import (
    MessageActionChatAddUser,
    MessageActionChatDeletePhoto,
    MessageActionChatDeleteUser,
    MessageActionChatEditPhoto,
    MessageActionChatEditTitle,
    MessageActionChatJoinedByLink,
)

from src.listener import TelegramListener
from src.message_utils import service_message_text

TRACKED = -1001234567890
ACTOR_ID = 777
MSG_ID = 500123
MSG_DATE = datetime(2026, 7, 18, 12, 0, 0)


def _config(**overrides):
    config = MagicMock()
    config.api_id = 12345
    config.api_hash = "test_hash"
    config.phone = "+1234567890"
    config.session_path = "/tmp/test_session"
    config.validate_credentials = MagicMock()
    config.whitelist_mode = False
    config.chat_ids = set()
    config.global_include_ids = set()
    config.private_include_ids = set()
    config.groups_include_ids = set()
    config.channels_include_ids = set()
    config.listen_edits = True
    config.listen_deletions = False
    config.deletion_mode = "hard"
    config.listen_new_messages = True
    config.listen_new_messages_media = False
    config.listen_reactions = False
    config.reaction_debounce_seconds = 0.01
    config.listen_chat_actions = True
    config.skip_topic_ids = {}
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _entity(first_name="Alice", last_name=None, title="Group Title", username="grp"):
    """A stand-in Telegram entity used for both actor and chat-metadata lookups."""
    return SimpleNamespace(first_name=first_name, last_name=last_name, title=title, username=username)


def _build(**config_overrides):
    """Create a listener with the ChatAction handler captured for direct calls."""
    db = AsyncMock()
    db.insert_message = AsyncMock()
    db.upsert_chat = AsyncMock()
    listener = TelegramListener(_config(**config_overrides), db)
    listener._tracked_chat_ids = {TRACKED}
    listener._get_marked_id = MagicMock(return_value=TRACKED)
    listener._download_avatar = AsyncMock()
    listener._notifier = None

    handlers = {}

    def capture_on(event_type):
        def decorator(fn):
            handlers[event_type] = fn
            return fn

        return decorator

    client = MagicMock()
    client.on = capture_on
    client.get_entity = AsyncMock(return_value=_entity())
    listener.client = client
    listener._register_handlers()
    return listener, handlers[events.ChatAction], db


def _service_msg(action, *, msg_id=MSG_ID, sender_id=ACTOR_ID, out=False, reply_to=None):
    """A mock MessageService carrying a real id/date/action (reply_to=None).

    ``reply_to`` defaults to ``None`` to dodge the MagicMock truthiness pitfall
    in ``extract_topic_id``.
    """
    return SimpleNamespace(
        id=msg_id,
        action=action,
        sender_id=sender_id,
        date=MSG_DATE,
        reply_to_msg_id=None,
        reply_to=reply_to,
        out=out,
    )


def _event(
    action_message,
    *,
    chat_id=TRACKED,
    new_photo=False,
    photo=None,
    new_title=None,
    created=False,
    user_joined=False,
    user_added=False,
    user_left=False,
    user_kicked=False,
    user_id=None,
):
    return SimpleNamespace(
        chat_id=chat_id,
        action_message=action_message,
        new_photo=new_photo,
        photo=photo,
        new_title=new_title,
        created=created,
        user_joined=user_joined,
        user_added=user_added,
        user_left=user_left,
        user_kicked=user_kicked,
        user_id=user_id,
    )


class TestChatActionHandler:
    """on_chat_action archives real service rows and skips peer-only syncs."""

    async def test_join_via_invite_link_uses_real_id_and_vocabulary(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0)),
            user_joined=True,
            user_id=ACTOR_ID,
        )
        await handler(event)

        db.insert_message.assert_called_once()
        data = db.insert_message.call_args[0][0]
        assert data["id"] == MSG_ID
        assert data["date"] == MSG_DATE
        assert data["sender_id"] == ACTOR_ID
        assert data["raw_data"]["service_type"] == "service"
        assert data["raw_data"]["action_type"] == "chat_joined_by_link"
        assert data["raw_data"]["action_type"] != "photo_removed"
        assert data["text"] == "Alice joined the group via invite link"

    async def test_participant_only_event_writes_no_row(self):
        listener, handler, db = _build()
        event = _event(None, user_joined=True, user_id=ACTOR_ID)

        await handler(event)  # must not raise

        db.insert_message.assert_not_called()
        assert listener.stats["chat_actions"] == 1

    async def test_delete_photo_refreshes_metadata_without_avatar(self):
        listener, handler, db = _build()
        # Telethon builds MessageActionChatDeletePhoto as new_photo=True, photo=None.
        event = _event(
            _service_msg(MessageActionChatDeletePhoto()),
            new_photo=True,
            photo=None,
            user_id=ACTOR_ID,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_delete_photo"
        assert "removed" in data["text"].lower()
        assert "photo" in data["text"].lower()
        db.upsert_chat.assert_awaited_once()

    async def test_join_does_not_refresh_chat_metadata(self):
        # The metadata-refresh side effect fires only for photo/title changes; a
        # join (no photo flags, no new_title) must not touch chat metadata
        # (review finding: the old catch-all classifier did exactly that).
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0)),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        db.upsert_chat.assert_not_awaited()
        listener._download_avatar.assert_not_awaited()

    async def test_row_carries_real_topic_reply_and_outgoing_fields(self):
        # reply_to_top_id / reply_to_msg_id / is_outgoing come from the real
        # service message, not hardcoded defaults (review finding).
        listener, handler, db = _build()
        msg = _service_msg(MessageActionChatJoinedByLink(inviter_id=0), out=True)
        msg.reply_to_msg_id = 4242
        msg.reply_to = SimpleNamespace(reply_to_top_id=99, reply_to_msg_id=4242, forum_topic=True)
        event = _event(msg, user_joined=True, user_id=ACTOR_ID)

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["is_outgoing"] == 1
        assert data["reply_to_msg_id"] == 4242
        assert data["reply_to_top_id"] == 99

    async def test_self_join_via_username_reads_joined_not_added(self):
        # ChatAddUser where the user added themselves (Telethon maps it to
        # user_joined=True) must read "joined the group", not "was added".
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatAddUser(users=[ACTOR_ID])),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_add_user"
        assert data["text"] == "Alice joined the group"
        listener._download_avatar.assert_not_called()

    async def test_edit_photo_downloads_avatar(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatEditPhoto(photo=MagicMock())),
            new_photo=True,
            photo=MagicMock(),  # a Photo present -> changed, not removed
            user_id=ACTOR_ID,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_edit_photo"
        assert "changed the group photo" in data["text"].lower()
        db.upsert_chat.assert_awaited_once()
        listener._download_avatar.assert_awaited_once()

    async def test_edit_title_sets_new_title_and_upserts_chat(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatEditTitle(title="New Group Name")),
            new_title="New Group Name",
            user_id=ACTOR_ID,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_edit_title"
        assert data["raw_data"]["new_title"] == "New Group Name"
        assert "New Group Name" in data["text"]
        db.upsert_chat.assert_awaited_once()
        listener._download_avatar.assert_not_called()

    async def test_delete_user_left_when_sender_is_affected(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatDeleteUser(user_id=ACTOR_ID), sender_id=ACTOR_ID),
            user_left=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_delete_user"
        assert data["text"] == "Alice left the group"

    async def test_delete_user_removed_when_kicked_by_other(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatDeleteUser(user_id=999), sender_id=ACTOR_ID),
            user_kicked=True,
            user_id=999,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_delete_user"
        assert data["text"] == "Alice was removed from the group"

    async def test_added_user(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatAddUser(users=[ACTOR_ID])),
            user_added=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        data = db.insert_message.call_args[0][0]
        assert data["raw_data"]["action_type"] == "chat_add_user"
        assert data["text"] == "Alice was added to the group"

    async def test_skipped_topic_writes_no_row(self):
        listener, handler, db = _build()
        listener.config.should_skip_topic = MagicMock(return_value=True)
        reply_to = SimpleNamespace(forum_topic=True, reply_to_top_id=99, reply_to_msg_id=1)
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0), reply_to=reply_to),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        db.insert_message.assert_not_called()
        listener.config.should_skip_topic.assert_called_once_with(TRACKED, 99)

    async def test_disabled_flag_skips(self):
        listener, handler, db = _build(listen_chat_actions=False)
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0)),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        db.insert_message.assert_not_called()

    async def test_untracked_chat_skipped(self):
        listener, handler, db = _build()
        listener._get_marked_id = MagicMock(return_value=-999)
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0)),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        db.insert_message.assert_not_called()

    async def test_error_increments_error_stat(self):
        listener, handler, db = _build()
        listener._get_marked_id = MagicMock(side_effect=Exception("crash"))

        await handler(_event(_service_msg(MessageActionChatJoinedByLink(inviter_id=0))))

        assert listener.stats["errors"] == 1

    async def test_actor_without_user_id_uses_someone(self):
        listener, handler, db = _build()
        event = _event(
            _service_msg(MessageActionChatAddUser(users=[123])),
            user_added=True,
            user_id=None,
        )

        await handler(event)

        # No user_id and no metadata refresh for an add -> no entity lookup at all.
        listener.client.get_entity.assert_not_awaited()
        data = db.insert_message.call_args[0][0]
        assert data["text"] == "Someone was added to the group"
        assert data["sender_id"] == ACTOR_ID  # real msg.sender_id, never a fabricated id

    async def test_actor_last_name_appended(self):
        listener, handler, db = _build()
        listener.client.get_entity = AsyncMock(return_value=_entity(first_name="John", last_name="Smith"))
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0)),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)

        assert db.insert_message.call_args[0][0]["text"] == "John Smith joined the group via invite link"

    async def test_insert_message_failure_counts_as_error(self):
        listener, handler, db = _build()
        db.insert_message = AsyncMock(side_effect=Exception("DB error"))
        event = _event(
            _service_msg(MessageActionChatJoinedByLink(inviter_id=0)),
            user_joined=True,
            user_id=ACTOR_ID,
        )

        await handler(event)  # must not raise

        assert listener.stats["chat_actions"] == 1
        assert listener.stats["errors"] == 1

    async def test_metadata_refresh_failure_is_isolated(self):
        listener, handler, db = _build()
        db.upsert_chat = AsyncMock(side_effect=Exception("chat update failed"))
        event = _event(
            _service_msg(MessageActionChatEditPhoto(photo=MagicMock())),
            new_photo=True,
            photo=MagicMock(),
            user_id=ACTOR_ID,
        )

        await handler(event)  # metadata failure must not crash nor count as a handler error

        db.insert_message.assert_called_once()  # the service row was still saved
        assert listener.stats["chat_actions"] == 1
        assert listener.stats["errors"] == 0


class TestServiceMessageText:
    """Unit tests for the shared service_message_text helper."""

    def test_join_by_link(self):
        text = service_message_text(MessageActionChatJoinedByLink(inviter_id=0), actor_name="Bo")
        assert text == "Bo joined the group via invite link"

    def test_left_vs_removed(self):
        action = MessageActionChatDeleteUser(user_id=1)
        assert service_message_text(action, actor_name="X", affected_left=True) == "X left the group"
        assert service_message_text(action, actor_name="X", affected_left=False) == "X was removed from the group"

    def test_edit_title_includes_title(self):
        text = service_message_text(MessageActionChatEditTitle(title="Hello"), actor_name="Y")
        assert text == 'Y changed the group name to "Hello"'

    def test_edit_photo_and_delete_photo(self):
        assert service_message_text(MessageActionChatEditPhoto(photo=MagicMock()), actor_name="Z") == (
            "Z changed the group photo"
        )
        assert service_message_text(MessageActionChatDeletePhoto(), actor_name="Z") == "Z removed the group photo"

    def test_falsy_actor_becomes_someone(self):
        assert (
            service_message_text(MessageActionChatDeletePhoto(), actor_name=None) == "Someone removed the group photo"
        )
        assert service_message_text(MessageActionChatDeletePhoto(), actor_name="") == "Someone removed the group photo"

    def test_unknown_action_returns_none(self):
        class MessageActionSomethingExotic:
            pass

        assert service_message_text(MessageActionSomethingExotic(), actor_name="Zed") is None
