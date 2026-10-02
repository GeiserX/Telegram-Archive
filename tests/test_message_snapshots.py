"""Later states of polls and link previews (``message_snapshots``, 038).

``raw_data`` keeps a poll and a link preview as first captured. When a later
read shows another state (votes, results, closing; the card's fields), a row
is added to ``message_snapshots``; a read showing the newest kept state adds
nothing. Live locations are not followed.

The writers run for real against SQLite and PostgreSQL (``real_adapter``):
the backup's message upsert, the scheduled sync, the listener's new-message
and edit handlers and its ``UpdateMessagePoll`` handler. The messages read and
both exports return the snapshots, inside the same chat and account scope.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from telethon.tl.types import (
    MessageMediaPoll,
    MessageMediaWebPage,
    PeerChannel,
    Poll,
    PollAnswer,
    PollAnswerVoters,
    PollResults,
    TextWithEntities,
    UpdateMessagePoll,
    WebPage,
)

from telegram_archive.db.adapter import (
    _keep_archived_snapshot_keys,
    _merge_poll_state,
    _snapshot_plan,
)
from telegram_archive.db.models import Message, MessageSnapshot
from telegram_archive.listener import TelegramListener
from telegram_archive.message_utils import extract_media_poll, extract_poll_state
from telegram_archive.telegram_backup import TelegramBackup

CHANNEL_ID = 9990301  # the chat's channel id, as a peer names it
CHAT_ID = -1000000000000 - CHANNEL_ID  # its marked id, as the archive keys it
OTHER_CHAT_ID = CHAT_ID - 1
MESSAGE_ID = 11
POLL_ID = 5550000000000000301
SENT = datetime(2026, 3, 1, 9, 0, 0)
EDITED = datetime(2026, 3, 1, 9, 30, 0)


def _poll(*, closed: bool = False) -> Poll:
    return Poll(
        id=POLL_ID,
        question=TextWithEntities(text="Demo question?", entities=[]),
        answers=[
            PollAnswer(text=TextWithEntities(text="Answer A", entities=[]), option=b"\x00"),
            PollAnswer(text=TextWithEntities(text="Answer B", entities=[]), option=b"\x01"),
        ],
        hash=0,
        closed=closed,
        public_voters=False,
        multiple_choice=False,
        quiz=False,
    )


def _results(a: int, b: int, *, hidden: bool = False) -> PollResults:
    if hidden:
        return PollResults(total_voters=a + b)
    return PollResults(
        results=[PollAnswerVoters(option=b"\x00", voters=a), PollAnswerVoters(option=b"\x01", voters=b)],
        total_voters=a + b,
    )


def _poll_media(a: int, b: int, *, closed: bool = False) -> MessageMediaPoll:
    return MessageMediaPoll(poll=_poll(closed=closed), results=_results(a, b))


def _preview_media(title: str) -> MessageMediaWebPage:
    return MessageMediaWebPage(
        webpage=WebPage(
            id=1,
            url="https://example.com/page",
            display_url="example.com/page",
            hash=0,
            site_name="Example",
            title=title,
        )
    )


def _poll_state(a: int, b: int, *, closed: bool = False) -> dict:
    return extract_media_poll(_poll_media(a, b, closed=closed))


def _preview_state(title: str) -> dict:
    return {
        "url": "https://example.com/page",
        "display_url": "example.com/page",
        "site_name": "Example",
        "title": title,
    }


def _message_data(raw_data: dict, *, source: str = "backup", text: str = "", edit_date=None) -> dict:
    return {
        "id": MESSAGE_ID,
        "chat_id": CHAT_ID,
        "sender_id": 4242,
        "date": SENT,
        "text": text,
        "edit_date": edit_date,
        "raw_data": raw_data,
        "keeps_older_text": source == "backup",
        "version_source": source,
    }


async def _seed_chat(adapter, account_id: int = 1, chat_id: int = CHAT_ID) -> None:
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "Test Group A"}, account_id=account_id)


async def _seed(adapter, raw_data: dict, *, account_id: int = 1) -> None:
    await _seed_chat(adapter, account_id)
    await adapter.insert_message(_message_data(raw_data), account_id=account_id)


async def _snapshots(adapter) -> list[MessageSnapshot]:
    async with adapter.db_manager.async_session_factory() as session:
        return list((await session.execute(select(MessageSnapshot).order_by(MessageSnapshot.id))).scalars())


async def _raw_data(adapter, account_id: int = 1) -> dict:
    async with adapter.db_manager.async_session_factory() as session:
        row = (
            await session.execute(
                select(Message.raw_data).where(
                    Message.account_id == account_id, Message.chat_id == CHAT_ID, Message.id == MESSAGE_ID
                )
            )
        ).scalar_one()
    return json.loads(row) if row else {}


# ---------------------------------------------------------------------------
# The plan, without a database
# ---------------------------------------------------------------------------


class TestThePlan:
    def test_the_same_state_writes_nothing(self):
        first = _poll_state(3, 1)
        assert _snapshot_plan(json.dumps({"poll": first}), {}, {"poll": _poll_state(3, 1)}) == ([], {})

    def test_other_votes_add_a_row_and_keep_the_first_capture(self):
        first = _poll_state(3, 1)
        rows, fills = _snapshot_plan(json.dumps({"poll": first}), {}, {"poll": _poll_state(5, 2)})
        assert fills == {}
        ((kind, state),) = rows
        assert kind == "poll"
        assert state["results"]["total_voters"] == 7

    def test_the_newest_row_is_what_a_read_is_compared_with(self):
        first, newer = _poll_state(3, 1), _poll_state(5, 2)
        assert _snapshot_plan(json.dumps({"poll": first}), {"poll": newer}, {"poll": _poll_state(5, 2)}) == ([], {})
        # Back to the first state is a change from the newest kept.
        rows, _ = _snapshot_plan(json.dumps({"poll": first}), {"poll": newer}, {"poll": _poll_state(3, 1)})
        assert [kind for kind, _ in rows] == ["poll"]

    def test_a_missing_first_capture_is_filled_without_a_row(self):
        rows, fills = _snapshot_plan(json.dumps({"grouped_id": "1"}), {}, {"preview": _preview_state("Title")})
        assert rows == []
        assert fills == {"webpage": _preview_state("Title")}

    def test_results_alone_with_nothing_kept_add_a_row_and_fill_nothing(self):
        rows, fills = _snapshot_plan("{}", {}, {"poll": {"results": {"total_voters": 4, "results": []}}})
        assert fills == {}
        assert rows == [("poll", {"results": {"total_voters": 4, "results": []}})]

    def test_a_key_that_is_not_an_object_or_a_broken_raw_data_is_never_filled_over(self):
        rows, fills = _snapshot_plan(json.dumps({"webpage": "broken"}), {}, {"preview": _preview_state("T")})
        assert fills == {} and [kind for kind, _ in rows] == ["preview"]
        rows, fills = _snapshot_plan("{not json", {}, {"preview": _preview_state("T")})
        assert fills == {} and [kind for kind, _ in rows] == ["preview"]

    def test_a_read_never_takes_away_known_counts_or_a_correct_mark(self):
        kept = _poll_state(3, 1)
        kept["results"]["results"][0]["correct"] = True
        hidden = extract_poll_state(_poll(), _results(4, 1, hidden=True))
        merged = _merge_poll_state(kept, hidden)
        assert merged["results"]["total_voters"] == 5
        assert [o["voters"] for o in merged["results"]["results"]] == [3, 1]
        # A min update leaves the quiz's answer out; the mark stays.
        merged = _merge_poll_state(kept, extract_poll_state(None, _results(6, 1)))
        assert [o["voters"] for o in merged["results"]["results"]] == [6, 1]
        assert merged["results"]["results"][0]["correct"] is True
        assert merged["question"] == "Demo question?"

    def test_the_archived_poll_and_preview_stay_in_a_rewritten_raw_data(self):
        archived = json.dumps({"poll": _poll_state(3, 1), "webpage": _preview_state("Old")})
        incoming = json.dumps({"poll": _poll_state(9, 9), "webpage": _preview_state("New"), "grouped_id": "1"})
        kept = json.loads(_keep_archived_snapshot_keys(archived, incoming))
        assert kept == {"poll": _poll_state(3, 1), "webpage": _preview_state("Old"), "grouped_id": "1"}
        # A key the archive lacks is left to the snapshot step.
        assert json.loads(_keep_archived_snapshot_keys("{}", incoming)) == {"grouped_id": "1"}


# ---------------------------------------------------------------------------
# The backup's reads (the message upsert)
# ---------------------------------------------------------------------------


class TestBackupReads:
    async def test_other_votes_add_a_row_and_raw_data_keeps_the_first_capture(self, real_adapter):
        first = _poll_state(3, 1)
        await _seed(real_adapter, {"poll": first})

        await real_adapter.insert_message(_message_data({"poll": _poll_state(5, 2)}), account_id=1)

        assert (await _raw_data(real_adapter))["poll"] == first
        (row,) = await _snapshots(real_adapter)
        assert (row.kind, row.source, row.account_id, row.chat_id, row.message_id) == (
            "poll",
            "backup",
            1,
            CHAT_ID,
            MESSAGE_ID,
        )
        assert json.loads(row.payload)["results"]["total_voters"] == 7
        assert row.observed_at is not None

    async def test_a_read_with_the_newest_state_adds_nothing(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        for _ in range(2):
            await real_adapter.insert_message(_message_data({"poll": _poll_state(3, 1)}), account_id=1)
        assert await _snapshots(real_adapter) == []
        await real_adapter.insert_message(_message_data({"poll": _poll_state(5, 2)}), account_id=1)
        await real_adapter.insert_message(_message_data({"poll": _poll_state(5, 2)}), account_id=1)
        assert len(await _snapshots(real_adapter)) == 1

    async def test_a_closed_poll_and_a_changed_preview_each_add_their_row(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1), "webpage": _preview_state("Old title")})
        await real_adapter.insert_message(
            _message_data({"poll": _poll_state(3, 1, closed=True), "webpage": _preview_state("New title")}),
            account_id=1,
        )
        rows = await _snapshots(real_adapter)
        assert sorted(row.kind for row in rows) == ["poll", "preview"]
        by_kind = {row.kind: json.loads(row.payload) for row in rows}
        assert by_kind["poll"]["closed"] is True
        assert by_kind["preview"]["title"] == "New title"
        raw = await _raw_data(real_adapter)
        assert raw["poll"]["closed"] is False and raw["webpage"]["title"] == "Old title"

    async def test_a_preview_first_seen_later_fills_raw_data_and_adds_no_row(self, real_adapter):
        await _seed(real_adapter, {"grouped_id": "77"})
        await real_adapter.insert_message(
            _message_data({"grouped_id": "77", "webpage": _preview_state("Resolved")}), account_id=1
        )
        assert await _snapshots(real_adapter) == []
        assert (await _raw_data(real_adapter)) == {"grouped_id": "77", "webpage": _preview_state("Resolved")}

    async def test_a_read_with_a_changed_poll_and_the_same_extras_leaves_raw_data_alone(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1), "grouped_id": "77"})
        await real_adapter.insert_message(_message_data({"poll": _poll_state(5, 2), "grouped_id": "77"}), account_id=1)
        assert (await _raw_data(real_adapter)) == {"poll": _poll_state(3, 1), "grouped_id": "77"}
        assert len(await _snapshots(real_adapter)) == 1

    async def test_an_import_never_adds_a_row(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        await real_adapter.insert_message(_message_data({"poll": _poll_state(5, 2)}, source="import"), account_id=1)
        # Also when the import changes something else, so the row is rewritten.
        await real_adapter.insert_message(
            _message_data({"poll": _poll_state(6, 2)}, source="import", text="Imported caption"), account_id=1
        )
        assert await _snapshots(real_adapter) == []
        assert (await _raw_data(real_adapter))["poll"] == _poll_state(3, 1)

    async def test_a_text_edit_and_a_poll_change_in_one_read_keep_both(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        await real_adapter.insert_message(
            _message_data({"poll": _poll_state(5, 2)}, text="Edited caption", edit_date=EDITED), account_id=1
        )
        assert len(await _snapshots(real_adapter)) == 1
        assert len(await real_adapter.get_message_versions(CHAT_ID, MESSAGE_ID, account_id=1)) == 1


# ---------------------------------------------------------------------------
# record_message_snapshots, find_poll_messages and the deletes
# ---------------------------------------------------------------------------


class TestRecordAndFind:
    async def test_record_returns_none_for_a_message_not_archived(self, real_adapter):
        await _seed_chat(real_adapter)
        result = await real_adapter.record_message_snapshots(
            CHAT_ID, MESSAGE_ID, {"poll": _poll_state(1, 1)}, account_id=1, source="listener"
        )
        assert result is None
        assert await _snapshots(real_adapter) == []

    async def test_record_adds_a_row_once_per_state(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        closed = {"poll": _poll_state(3, 1, closed=True)}
        assert await real_adapter.record_message_snapshots(
            CHAT_ID, MESSAGE_ID, closed, account_id=1, source="listener"
        ) == ["poll"]
        assert (
            await real_adapter.record_message_snapshots(CHAT_ID, MESSAGE_ID, closed, account_id=1, source="sync") == []
        )
        (row,) = await _snapshots(real_adapter)
        assert row.source == "listener"

    async def test_rows_are_per_account(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)}, account_id=1)
        await _seed(real_adapter, {"poll": _poll_state(3, 1)}, account_id=2)
        await real_adapter.record_message_snapshots(
            CHAT_ID, MESSAGE_ID, {"poll": _poll_state(4, 1)}, account_id=2, source="listener"
        )
        (row,) = await _snapshots(real_adapter)
        assert row.account_id == 2

    async def test_find_poll_messages_matches_the_exact_id_of_this_account(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)}, account_id=1)
        await _seed_chat(real_adapter, chat_id=OTHER_CHAT_ID)
        near = _poll_state(3, 1)
        near["id"] = POLL_ID * 10 + 1  # starts with the same digits
        await real_adapter.insert_message(
            {**_message_data({"poll": near}), "chat_id": OTHER_CHAT_ID, "id": MESSAGE_ID + 1}, account_id=1
        )
        await _seed(real_adapter, {"poll": _poll_state(3, 1)}, account_id=2)

        assert await real_adapter.find_poll_messages(POLL_ID, account_id=1) == [(CHAT_ID, MESSAGE_ID, None)]
        assert await real_adapter.find_poll_messages(POLL_ID, account_id=2) == [(CHAT_ID, MESSAGE_ID, None)]
        assert await real_adapter.find_poll_messages(POLL_ID + 1, account_id=1) == []

    async def test_a_hard_deleted_message_or_chat_takes_its_rows(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        await real_adapter.record_message_snapshots(
            CHAT_ID, MESSAGE_ID, {"poll": _poll_state(4, 1)}, account_id=1, source="listener"
        )
        await real_adapter.delete_message(CHAT_ID, MESSAGE_ID, account_id=1)
        assert await _snapshots(real_adapter) == []

        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        await real_adapter.record_message_snapshots(
            CHAT_ID, MESSAGE_ID, {"poll": _poll_state(4, 1)}, account_id=1, source="listener"
        )
        await real_adapter.delete_chat_and_related_data(CHAT_ID, account_id=1)
        assert await _snapshots(real_adapter) == []


# ---------------------------------------------------------------------------
# The messages read and both exports
# ---------------------------------------------------------------------------


class TestReads:
    async def _two_states(self, adapter, account_id: int = 1) -> None:
        await _seed(adapter, {"poll": _poll_state(3, 1), "webpage": _preview_state("Old")}, account_id=account_id)
        for state in (_poll_state(4, 1), _poll_state(5, 2, closed=True)):
            await adapter.record_message_snapshots(
                CHAT_ID, MESSAGE_ID, {"poll": state}, account_id=account_id, source="listener"
            )

    async def test_the_page_carries_the_newest_state_of_each_kind(self, real_adapter):
        await self._two_states(real_adapter)
        (msg,) = await real_adapter.get_messages_paginated(CHAT_ID, account_id=1)
        poll = msg["snapshots"]["poll"]
        assert poll["payload"]["closed"] is True
        assert poll["payload"]["results"]["total_voters"] == 7
        assert (poll["count"], poll["source"], poll["differs_from_first"]) == (2, "listener", True)
        assert "preview" not in msg["snapshots"]
        # The first capture is still what raw_data holds.
        assert msg["raw_data"]["poll"]["results"]["total_voters"] == 4

    async def test_a_newest_state_equal_to_the_first_capture_says_so(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        for state in (_poll_state(4, 1), _poll_state(3, 1)):
            await real_adapter.record_message_snapshots(
                CHAT_ID, MESSAGE_ID, {"poll": state}, account_id=1, source="listener"
            )
        (msg,) = await real_adapter.get_messages_paginated(CHAT_ID, account_id=1)
        assert msg["snapshots"]["poll"]["differs_from_first"] is False

    async def test_a_message_without_rows_carries_an_empty_map(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        (msg,) = await real_adapter.get_messages_paginated(CHAT_ID, account_id=1)
        assert msg["snapshots"] == {}

    async def test_the_page_of_one_account_never_shows_another_accounts_rows(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)}, account_id=1)
        await self._two_states(real_adapter, account_id=2)
        (msg,) = await real_adapter.get_messages_paginated(CHAT_ID, account_id=1)
        assert msg["snapshots"] == {}
        (msg,) = await real_adapter.get_messages_paginated(CHAT_ID, account_id=2)
        assert msg["snapshots"]["poll"]["count"] == 2

    async def test_the_viewer_export_lists_every_state_oldest_first(self, real_adapter):
        await self._two_states(real_adapter)
        await _seed(real_adapter, {"poll": _poll_state(3, 1)}, account_id=2)
        exported = [msg async for msg in real_adapter.get_messages_for_export(CHAT_ID, account_id=1)]
        (msg,) = exported
        totals = [s["payload"]["results"]["total_voters"] for s in msg["snapshots"]]
        assert totals == [5, 7]
        assert {s["kind"] for s in msg["snapshots"]} == {"poll"}
        assert isinstance(msg["snapshots"][0]["observed_at"], str)
        assert msg["snapshots"][0]["source"] == "listener"

    async def test_the_command_export_lists_every_state_per_message(self, real_adapter):
        await self._two_states(real_adapter)
        messages = await real_adapter.get_messages_for_backup_export(CHAT_ID, account_id=1)
        (msg,) = messages
        assert [s["payload"]["results"]["total_voters"] for s in msg["snapshots"]] == [5, 7]
        assert isinstance(msg["snapshots"][0]["observed_at"], datetime)


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


def _telegram_message(media, *, edit_date=None):
    return SimpleNamespace(
        id=MESSAGE_ID,
        date=SENT.replace(tzinfo=UTC),
        edit_date=edit_date.replace(tzinfo=UTC) if edit_date else None,
        edit_hide=False,
        message="",
        entities=None,
        media=media,
        reactions=None,
        reply_to=None,
        reply_to_msg_id=None,
        sender=None,
        sender_id=4242,
        grouped_id=None,
        out=False,
        fwd_from=None,
        action=None,
    )


def _event(message) -> MagicMock:
    event = MagicMock()
    event.chat_id = CHAT_ID
    event.message = message
    event.get_chat = AsyncMock(return_value=None)
    event.get_sender = AsyncMock(return_value=None)
    return event


def _listener(adapter, *, listen_edits: bool = True) -> tuple[TelegramListener, dict]:
    config = MagicMock()
    config.listen_edits = listen_edits
    config.listen_new_messages = True
    config.listen_new_messages_media = False
    config.listen_deletions = False
    config.listen_chat_actions = False
    config.listen_reactions = False
    config.whitelist_mode = False
    config.chat_ids = set()
    config.global_include_ids = set()
    config.private_include_ids = set()
    config.groups_include_ids = set()
    config.channels_include_ids = set()
    config.should_backup_chat = MagicMock(return_value=False)
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    listener = TelegramListener(config, adapter, account_id=1)
    listener._tracked_chat_ids = {CHAT_ID}
    listener._notifier = None
    handlers = {}
    client = MagicMock()

    def capture_on(event_type):
        def decorator(fn):
            handlers[fn.__name__] = fn
            return fn

        return decorator

    client.on = lambda event_type: capture_on(event_type)
    listener.client = client
    listener._register_handlers()
    return listener, handlers


class TestListener:
    async def test_a_new_poll_is_captured_in_raw_data(self, real_adapter):
        await _seed_chat(real_adapter)
        _, handlers = _listener(real_adapter)
        await handlers["on_new_message"](_event(_telegram_message(_poll_media(3, 1))))
        assert (await _raw_data(real_adapter))["poll"] == _poll_state(3, 1)
        assert await _snapshots(real_adapter) == []

    async def test_an_edit_closing_the_poll_or_changing_the_preview_adds_rows(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        _, handlers = _listener(real_adapter)
        closing = _event(_telegram_message(_poll_media(3, 2, closed=True), edit_date=EDITED))
        await handlers["on_message_edited"](closing)
        await handlers["on_message_edited"](closing)
        (row,) = await _snapshots(real_adapter)
        assert (row.kind, row.source) == ("poll", "listener")
        assert json.loads(row.payload)["closed"] is True
        assert (await _raw_data(real_adapter))["poll"]["closed"] is False

    async def test_an_edit_bringing_a_preview_card_fills_the_first_capture(self, real_adapter):
        await _seed(real_adapter, {})
        _, handlers = _listener(real_adapter)
        await handlers["on_message_edited"](_event(_telegram_message(_preview_media("Late title"), edit_date=EDITED)))
        assert (await _raw_data(real_adapter))["webpage"]["title"] == "Late title"
        await handlers["on_message_edited"](_event(_telegram_message(_preview_media("Newer title"), edit_date=EDITED)))
        (row,) = await _snapshots(real_adapter)
        assert (row.kind, json.loads(row.payload)["title"]) == ("preview", "Newer title")

    async def test_a_poll_update_naming_the_message_adds_a_row(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        _, handlers = _listener(real_adapter)
        update = UpdateMessagePoll(
            poll_id=POLL_ID, results=_results(6, 1), peer=PeerChannel(channel_id=CHANNEL_ID), msg_id=MESSAGE_ID
        )
        await handlers["on_message_poll"](update)
        await handlers["on_message_poll"](update)
        (row,) = await _snapshots(real_adapter)
        payload = json.loads(row.payload)
        assert payload["results"]["total_voters"] == 7
        # The update carried the results alone; the question stays from the first capture.
        assert payload["question"] == "Demo question?"

    async def test_a_poll_update_naming_only_the_poll_finds_it_once(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        listener, handlers = _listener(real_adapter)
        real_find = real_adapter.find_poll_messages
        listener.db = SimpleNamespace(
            find_poll_messages=AsyncMock(side_effect=real_find),
            record_message_snapshots=real_adapter.record_message_snapshots,
        )
        for a in (6, 7):
            await handlers["on_message_poll"](UpdateMessagePoll(poll_id=POLL_ID, results=_results(a, 1)))
        assert [json.loads(r.payload)["results"]["total_voters"] for r in await _snapshots(real_adapter)] == [7, 8]
        listener.db.find_poll_messages.assert_awaited_once()

    async def test_a_poll_not_archived_is_not_looked_up_again_for_a_while(self, real_adapter):
        await _seed_chat(real_adapter)
        listener, handlers = _listener(real_adapter)
        listener.db = SimpleNamespace(find_poll_messages=AsyncMock(return_value=[]))
        for _ in range(3):
            await handlers["on_message_poll"](UpdateMessagePoll(poll_id=POLL_ID, results=_results(1, 1)))
        listener.db.find_poll_messages.assert_awaited_once()

    async def test_a_poll_not_archived_is_looked_up_again_only_after_hours(self, real_adapter):
        """Each lookup reads the whole messages table, and polls not found are
        mostly in chats the archive does not keep: a miss lasts 6 hours."""
        await _seed_chat(real_adapter)
        listener, handlers = _listener(real_adapter)
        listener.db = SimpleNamespace(find_poll_messages=AsyncMock(return_value=[]))
        clock = [1000.0]
        update = UpdateMessagePoll(poll_id=POLL_ID, results=_results(1, 1))
        with patch("telegram_archive.listener.time.monotonic", side_effect=lambda: clock[0]):
            await handlers["on_message_poll"](update)
            clock[0] += 11 * 60
            await handlers["on_message_poll"](update)
            assert listener.db.find_poll_messages.await_count == 1
            clock[0] += 6 * 60 * 60
            await handlers["on_message_poll"](update)
        assert listener.db.find_poll_messages.await_count == 2

    async def test_the_poll_lookup_cache_stays_bounded(self, real_adapter):
        from telegram_archive.listener import POLL_LOOKUP_CACHE_SIZE

        await _seed_chat(real_adapter)
        listener, handlers = _listener(real_adapter)
        listener.db = SimpleNamespace(find_poll_messages=AsyncMock(return_value=[]))
        for poll_id in range(POLL_LOOKUP_CACHE_SIZE + 10):
            await handlers["on_message_poll"](UpdateMessagePoll(poll_id=poll_id, results=_results(1, 1)))
        assert len(listener._poll_messages) == POLL_LOOKUP_CACHE_SIZE

    async def test_a_poll_the_listener_stores_replaces_its_miss(self, real_adapter):
        await _seed_chat(real_adapter)
        listener, handlers = _listener(real_adapter)
        real_find = real_adapter.find_poll_messages
        listener.db = SimpleNamespace(
            find_poll_messages=AsyncMock(side_effect=real_find),
            record_message_snapshots=real_adapter.record_message_snapshots,
        )
        await handlers["on_message_poll"](UpdateMessagePoll(poll_id=POLL_ID, results=_results(1, 1)))
        await real_adapter.insert_message(_message_data({"poll": _poll_state(3, 1)}), account_id=1)
        listener._remember_poll_message(_poll_state(3, 1), CHAT_ID, MESSAGE_ID, None)

        await handlers["on_message_poll"](UpdateMessagePoll(poll_id=POLL_ID, results=_results(6, 1)))

        listener.db.find_poll_messages.assert_awaited_once()
        assert [json.loads(r.payload)["results"]["total_voters"] for r in await _snapshots(real_adapter)] == [7]

    async def _seed_in_topic(self, adapter, topic_id: int) -> None:
        await _seed_chat(adapter)
        await adapter.insert_message(
            {**_message_data({"poll": _poll_state(3, 1)}), "reply_to_top_id": topic_id}, account_id=1
        )

    @staticmethod
    def _skipping(listener, topic_id: int) -> None:
        """SKIP_TOPIC_IDS holds ``topic_id`` of the chat."""
        listener.config.should_skip_topic = MagicMock(
            side_effect=lambda chat_id, topic: chat_id == CHAT_ID and topic == topic_id
        )

    @pytest.mark.parametrize("names_message", [False, True])
    async def test_a_poll_update_in_a_skipped_topic_keeps_nothing(self, real_adapter, names_message):
        await self._seed_in_topic(real_adapter, 7)
        listener, handlers = _listener(real_adapter)
        self._skipping(listener, 7)
        if names_message:
            update = UpdateMessagePoll(
                poll_id=POLL_ID,
                results=_results(6, 1),
                peer=PeerChannel(channel_id=CHANNEL_ID),
                msg_id=MESSAGE_ID,
                top_msg_id=7,
            )
        else:
            update = UpdateMessagePoll(poll_id=POLL_ID, results=_results(6, 1))
        await handlers["on_message_poll"](update)
        assert await _snapshots(real_adapter) == []

    async def test_a_poll_update_naming_only_the_poll_still_keeps_a_poll_in_another_topic(self, real_adapter):
        await self._seed_in_topic(real_adapter, 8)
        listener, handlers = _listener(real_adapter)
        self._skipping(listener, 7)
        await handlers["on_message_poll"](UpdateMessagePoll(poll_id=POLL_ID, results=_results(6, 1)))
        (row,) = await _snapshots(real_adapter)
        assert json.loads(row.payload)["results"]["total_voters"] == 7

    async def test_a_poll_update_waits_for_listen_edits(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        _, handlers = _listener(real_adapter, listen_edits=False)
        await handlers["on_message_poll"](
            UpdateMessagePoll(
                poll_id=POLL_ID, results=_results(6, 1), peer=PeerChannel(channel_id=CHANNEL_ID), msg_id=MESSAGE_ID
            )
        )
        assert await _snapshots(real_adapter) == []


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------


def _backup(adapter, remote_messages: list) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = MagicMock()
    backup.config.deletion_mode = "soft"
    backup.db = adapter
    backup.client = AsyncMock()
    backup.client.get_messages = AsyncMock(return_value=remote_messages)
    return backup


class TestSync:
    async def test_other_votes_with_the_same_edit_date_add_a_row(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        backup = _backup(real_adapter, [_telegram_message(_poll_media(8, 1))])
        await backup._sync_deletions_and_edits(CHAT_ID, object())
        await backup._sync_deletions_and_edits(CHAT_ID, object())
        (row,) = await _snapshots(real_adapter)
        assert (row.kind, row.source) == ("poll", "sync")
        assert json.loads(row.payload)["results"]["total_voters"] == 9

    async def test_an_unchanged_poll_adds_nothing(self, real_adapter):
        await _seed(real_adapter, {"poll": _poll_state(3, 1)})
        backup = _backup(real_adapter, [_telegram_message(_poll_media(3, 1))])
        await backup._sync_deletions_and_edits(CHAT_ID, object())
        assert await _snapshots(real_adapter) == []


@pytest.mark.parametrize("hidden", [False, True])
def test_extract_poll_state_matches_the_backups_shape(hidden):
    state = extract_poll_state(_poll(), _results(2, 1, hidden=hidden))
    assert state["answers"] == [{"text": "Answer A", "option": "AA=="}, {"text": "Answer B", "option": "AQ=="}]
    assert state["results"]["total_voters"] == 3
    assert (state["results"]["results"] == []) is hidden
