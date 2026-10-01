"""Locations, venues, live locations and contacts are kept (docs/design/location-and-contact.md).

A plain location (MessageMediaGeo) and a shared contact (MessageMediaContact)
used to keep nothing: the media row said "geo" or "contact" and raw_data stayed
"{}". Every writer now stores ``raw_data["geo"]`` and ``raw_data["contact"]``
beside the venue, live location and poll payloads, through one builder,
``extract_media_payload``. The listener keeps polls too. The message upsert
keeps a payload the archive holds when a later read lacks it, and a live
location keeps every position the archive's reads saw.

Demo coordinates, names and numbers only.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telethon import events
from telethon.tl.types import (
    GeoPoint,
    GeoPointEmpty,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaGeoLive,
    MessageMediaPoll,
    MessageMediaVenue,
    Poll,
    PollAnswer,
    PollAnswerVoters,
    PollResults,
    TextWithEntities,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_listener_extended import _make_listener_with_handlers  # noqa: E402

from telegram_archive.db.adapter import _keep_archived_payloads  # noqa: E402
from telegram_archive.db.models import Message  # noqa: E402
from telegram_archive.message_utils import (  # noqa: E402
    MEDIA_PAYLOAD_KEYS,
    METADATA_ONLY_MEDIA_TYPES,
    _poll_payload,
    extract_media_payload,
    merge_geo_live,
    message_seen_at,
)
from telegram_archive.telegram_backup import TelegramBackup  # noqa: E402
from telegram_archive.telegram_import import TelegramImporter, _export_media_payload  # noqa: E402

CHAT_ID = -1001234567890
DEMO_LAT = 40.416775
DEMO_LONG = -3.70379
DEMO_PHONE = "15555550100"
SENT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _geo(lat=DEMO_LAT, long=DEMO_LONG, radius=None):
    return GeoPoint(long=long, lat=lat, access_hash=987654321, accuracy_radius=radius)


def _contact(**overrides):
    fields = {
        "phone_number": DEMO_PHONE,
        "first_name": "Alex",
        "last_name": "Demo",
        "vcard": "BEGIN:VCARD\nVERSION:3.0\nFN:Alex Demo\nEND:VCARD",
        "user_id": 0,
    }
    fields.update(overrides)
    return MessageMediaContact(**fields)


def _poll():
    question = TextWithEntities(text="Where next?", entities=[])
    answers = [
        PollAnswer(text=TextWithEntities(text="Lake", entities=[]), option=b"0"),
        PollAnswer(text=TextWithEntities(text="Ridge", entities=[]), option=b"1"),
    ]
    results = PollResults(
        results=[PollAnswerVoters(option=b"0", voters=3), PollAnswerVoters(option=b"1", voters=1)], total_voters=4
    )
    return MessageMediaPoll(poll=Poll(id=77, question=question, answers=answers, hash=0, quiz=False), results=results)


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


class TestExtractMediaPayload:
    def test_location_keeps_its_coordinates_and_radius(self):
        assert extract_media_payload(MessageMediaGeo(geo=_geo(radius=25))) == (
            "geo",
            {"lat": DEMO_LAT, "long": DEMO_LONG, "accuracy_radius": 25},
        )

    def test_location_without_radius_leaves_it_out(self):
        assert extract_media_payload(MessageMediaGeo(geo=_geo())) == ("geo", {"lat": DEMO_LAT, "long": DEMO_LONG})

    def test_access_hash_is_never_stored(self):
        _, payload = extract_media_payload(MessageMediaGeo(geo=_geo(radius=5)))
        assert "access_hash" not in payload
        assert 987654321 not in payload.values()

    def test_an_empty_point_stores_no_coordinates(self):
        assert extract_media_payload(MessageMediaGeo(geo=GeoPointEmpty())) == ("geo", {})

    def test_contact_keeps_every_field(self):
        assert extract_media_payload(_contact()) == (
            "contact",
            {
                "first_name": "Alex",
                "last_name": "Demo",
                "phone_number": DEMO_PHONE,
                "vcard": "BEGIN:VCARD\nVERSION:3.0\nFN:Alex Demo\nEND:VCARD",
                "user_id": 0,
            },
        )

    def test_contact_with_empty_names_and_phone_is_kept_as_sent(self):
        media = _contact(first_name="", last_name="", phone_number="", vcard="", user_id=4242)
        assert extract_media_payload(media) == (
            "contact",
            {"first_name": "", "last_name": "", "phone_number": "", "vcard": "", "user_id": 4242},
        )

    def test_poll_has_the_shape_the_sweep_always_wrote(self):
        media = _poll()
        key, payload = extract_media_payload(media)
        assert key == "poll"
        assert payload == _poll_payload(media)
        assert payload["question"] == "Where next?"
        assert payload["answers"] == [{"text": "Lake", "option": "MA=="}, {"text": "Ridge", "option": "MQ=="}]
        assert payload["results"]["total_voters"] == 4
        assert payload["results"]["results"][0] == {"option": "MA==", "voters": 3, "correct": None}

    def test_venue_gains_its_id_type_and_radius(self):
        media = MessageMediaVenue(
            geo=_geo(radius=40),
            title="Demo Cafe",
            address="1 Example Street, Demo City",
            provider="foursquare",
            venue_id="demo-venue-1",
            venue_type="food/cafe",
        )
        assert extract_media_payload(media) == (
            "venue",
            {
                "title": "Demo Cafe",
                "address": "1 Example Street, Demo City",
                "provider": "foursquare",
                "venue_id": "demo-venue-1",
                "venue_type": "food/cafe",
                "lat": DEMO_LAT,
                "long": DEMO_LONG,
                "accuracy_radius": 40,
            },
        )

    def test_live_location_keeps_heading_radius_and_when_it_was_seen(self):
        media = MessageMediaGeoLive(geo=_geo(radius=10), period=900, heading=90)
        seen = datetime(2026, 3, 1, 12, 14, tzinfo=UTC)
        assert extract_media_payload(media, seen_at=seen) == (
            "geo_live",
            {
                "lat": DEMO_LAT,
                "long": DEMO_LONG,
                "period": 900,
                "heading": 90,
                "accuracy_radius": 10,
                "at": "2026-03-01T12:14:00",
            },
        )

    def test_a_bare_magicmock_stays_inert(self):
        assert extract_media_payload(MagicMock()) is None
        assert extract_media_payload(None) is None

    def test_a_broken_payload_degrades_to_none_and_never_raises(self):
        broken = MagicMock(spec=MessageMediaPoll)
        broken.poll = None  # .answers on None raises inside the builder
        assert extract_media_payload(broken) is None

    def test_every_metadata_only_kind_has_a_payload_key(self):
        assert set(MEDIA_PAYLOAD_KEYS) == METADATA_ONLY_MEDIA_TYPES

    def test_seen_at_counts_a_hidden_edit(self):
        """Telegram moves a live location with hidden edits: the last one dates the position."""
        msg = SimpleNamespace(date=SENT, edit_date=SENT + timedelta(minutes=5), edit_hide=True)
        assert message_seen_at(msg) == SENT + timedelta(minutes=5)
        assert message_seen_at(SimpleNamespace(date=SENT, edit_date=None)) == SENT


# ---------------------------------------------------------------------------
# merge_geo_live
# ---------------------------------------------------------------------------


class TestMergeGeoLive:
    OLD = {"lat": 1.0, "long": 2.0, "period": 900, "at": "2026-03-01T12:02:00"}
    NEW = {"lat": 1.5, "long": 2.5, "period": 900, "heading": 45, "at": "2026-03-01T12:14:00"}

    def test_the_newer_read_takes_the_top_and_the_older_goes_into_earlier(self):
        merged = merge_geo_live(self.OLD, self.NEW)
        assert {k: merged[k] for k in ("lat", "long", "at")} == {"lat": 1.5, "long": 2.5, "at": "2026-03-01T12:14:00"}
        assert merged["earlier"] == [{"lat": 1.0, "long": 2.0, "at": "2026-03-01T12:02:00"}]

    def test_an_older_read_never_takes_the_top(self):
        merged = merge_geo_live(self.NEW, self.OLD)
        assert merged["lat"] == 1.5
        assert merged["earlier"] == [{"lat": 1.0, "long": 2.0, "at": "2026-03-01T12:02:00"}]

    def test_a_read_with_no_coordinates_only_updates_the_period(self):
        merged = merge_geo_live(self.NEW, {"period": 60, "at": "2026-03-01T13:00:00"})
        assert merged == {**self.NEW, "period": 60}

    def test_the_same_read_twice_adds_nothing(self):
        assert merge_geo_live(self.NEW, dict(self.NEW)) == self.NEW

    def test_earlier_positions_are_never_lost_and_stay_oldest_first(self):
        first = merge_geo_live(self.OLD, self.NEW)
        newest = {"lat": 1.9, "long": 2.9, "period": 900, "at": "2026-03-01T12:20:00"}
        merged = merge_geo_live(first, newest)
        assert merged["lat"] == 1.9
        assert [p["at"] for p in merged["earlier"]] == ["2026-03-01T12:02:00", "2026-03-01T12:14:00"]
        # A read the archive already saw does not come back twice.
        assert merge_geo_live(merged, self.OLD)["earlier"] == merged["earlier"]

    def test_a_payload_without_at_counts_as_the_oldest(self):
        legacy = {"lat": 0.5, "long": 0.5, "period": 900}
        merged = merge_geo_live(legacy, self.NEW)
        assert merged["lat"] == 1.5
        assert merged["earlier"] == [{"lat": 0.5, "long": 0.5}]


# ---------------------------------------------------------------------------
# Both writers
# ---------------------------------------------------------------------------


def _telegram_message(msg_id, media, *, date=SENT, edit_date=None):
    msg = MagicMock()
    msg.id = msg_id
    msg.sender = None
    msg.sender_id = 4242
    msg.date = date
    msg.text = ""
    msg.message = ""
    msg.entities = None
    msg.reply_to_msg_id = None
    msg.reply_to = None
    msg.edit_date = edit_date
    msg.edit_hide = False
    msg.out = False
    msg.pinned = False
    msg.grouped_id = None
    msg.fwd_from = None
    msg.media = media
    msg.reactions = None
    msg.post_author = None
    msg.action = None
    msg.rich_message = None
    return msg


def _backup():
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.db = AsyncMock()
    backup.config = MagicMock()
    backup.config.should_download_media_for_chat = MagicMock(return_value=True)
    backup.config.should_skip_topic = MagicMock(return_value=False)
    backup.config.download_youtube_videos = False
    backup.client = AsyncMock()
    backup._process_media = AsyncMock(return_value=None)
    return backup


async def _listener_capture(media, *, date=SENT, edit_date=None):
    """What on_new_message passes to insert_message, and the WebSocket frame it sends."""
    listener, handlers, db, _config = _make_listener_with_handlers(listen_new_messages_media=False)
    listener._notifier = MagicMock()
    listener._notifier.notify = AsyncMock()
    event = MagicMock()
    event.chat_id = CHAT_ID
    event.message = _telegram_message(5, media, date=date, edit_date=edit_date)
    event.get_chat = AsyncMock(return_value=MagicMock())
    await handlers[events.NewMessage](event)
    assert db.insert_message.await_count == 1, "the listener did not store the message"
    stored = db.insert_message.call_args[0][0]
    frame = listener._notifier.notify.call_args[0][2]["message"]
    return stored, frame


MEDIA_CASES = {
    "geo": lambda: MessageMediaGeo(geo=_geo(radius=25)),
    "contact": lambda: _contact(),
    "poll": _poll,
    "venue": lambda: MessageMediaVenue(
        geo=_geo(), title="Demo Cafe", address="1 Example St", provider="", venue_id="", venue_type=""
    ),
    "geo_live": lambda: MessageMediaGeoLive(geo=_geo(), period=900),
}


class TestBothWritersStoreTheSamePayload:
    @pytest.mark.parametrize("kind", sorted(MEDIA_CASES))
    async def test_sweep_and_listener_agree(self, kind):
        media = MEDIA_CASES[kind]()
        edit = SENT + timedelta(minutes=3)
        swept = await _backup()._process_message(_telegram_message(5, media, edit_date=edit), CHAT_ID)
        stored, frame = await _listener_capture(media, edit_date=edit)
        assert kind in swept["raw_data"], swept["raw_data"]
        assert stored["raw_data"][kind] == swept["raw_data"][kind]
        # The live row carries the card before the next poll.
        assert frame["raw_data"][kind] == swept["raw_data"][kind]

    async def test_the_sweep_downloads_nothing_for_a_poll(self):
        backup = _backup()
        result = await backup._process_message(_telegram_message(6, _poll()), CHAT_ID)
        assert result["raw_data"]["poll"]["question"] == "Where next?"
        backup._process_media.assert_not_awaited()

    async def test_the_sweep_still_records_the_metadata_row_for_a_location(self):
        backup = _backup()
        await backup._process_message(_telegram_message(7, MessageMediaGeo(geo=_geo())), CHAT_ID)
        backup._process_media.assert_awaited_once()

    async def test_a_live_location_is_dated_by_its_last_edit(self):
        edit = SENT + timedelta(minutes=14)
        swept = await _backup()._process_message(
            _telegram_message(8, MessageMediaGeoLive(geo=_geo(), period=900), edit_date=edit), CHAT_ID
        )
        assert swept["raw_data"]["geo_live"]["at"] == "2026-03-01T12:14:00"

    async def test_an_edit_of_an_unknown_message_stores_the_card(self):
        """on_message_edited falls back to on_new_message for a message not archived yet."""
        listener, handlers, db, _config = _make_listener_with_handlers(listen_new_messages_media=False)
        db.update_message_text = AsyncMock(return_value=("not_found", None))
        listener._keep_replaced_media = AsyncMock(return_value=None)
        event = MagicMock()
        event.chat_id = CHAT_ID
        event.message = _telegram_message(9, _contact())
        event.get_chat = AsyncMock(return_value=MagicMock())
        await handlers[events.MessageEdited](event)
        assert db.insert_message.call_args[0][0]["raw_data"]["contact"]["phone_number"] == DEMO_PHONE


# ---------------------------------------------------------------------------
# Telegram Desktop JSON import
# ---------------------------------------------------------------------------


class TestJsonImportPayload:
    """Field names from Telegram Desktop's export_output_json.cpp."""

    def test_location(self):
        msg = {"location_information": {"latitude": DEMO_LAT, "longitude": DEMO_LONG}}
        assert _export_media_payload(msg, None) == ("geo", {"lat": DEMO_LAT, "long": DEMO_LONG})

    def test_a_null_location_is_unavailable(self):
        assert _export_media_payload({"location_information": None}, None) == ("geo", {})

    def test_live_location(self):
        msg = {"location_information": {"latitude": 1.5, "longitude": 2.5}, "live_location_period_seconds": 900}
        seen = datetime(2026, 3, 1, 12, 14)
        assert _export_media_payload(msg, seen) == (
            "geo_live",
            {"lat": 1.5, "long": 2.5, "period": 900, "at": "2026-03-01T12:14:00"},
        )

    def test_venue(self):
        msg = {
            "place_name": "Demo Cafe",
            "address": "1 Example Street",
            "location_information": {"latitude": 1.0, "longitude": 2.0},
        }
        assert _export_media_payload(msg, None) == (
            "venue",
            {"title": "Demo Cafe", "address": "1 Example Street", "lat": 1.0, "long": 2.0},
        )

    def test_contact(self):
        msg = {"contact_information": {"first_name": "Alex", "last_name": "Demo", "phone_number": "+1 555 555 0100"}}
        assert _export_media_payload(msg, None) == (
            "contact",
            {"first_name": "Alex", "last_name": "Demo", "phone_number": "+1 555 555 0100"},
        )

    def test_poll(self):
        msg = {
            "poll": {
                "question": [{"type": "plain", "text": "Where next?"}],
                "closed": True,
                "total_voters": 4,
                "answers": [
                    {"text": "Lake", "voters": 3, "chosen": True},
                    {"text": "Ridge", "voters": 1, "chosen": False},
                ],
            }
        }
        assert _export_media_payload(msg, None) == (
            "poll",
            {
                "question": "Where next?",
                "answers": [{"text": "Lake", "option": "0"}, {"text": "Ridge", "option": "1"}],
                "closed": True,
                "results": {
                    "total_voters": 4,
                    "results": [
                        {"option": "0", "voters": 3, "chosen": True},
                        {"option": "1", "voters": 1, "chosen": False},
                    ],
                },
            },
        )

    def test_a_plain_message_has_none(self):
        assert _export_media_payload({"text": "hello"}, None) is None
        assert _export_media_payload({"location_information": {"latitude": True, "longitude": 2}}, None) == (
            "geo",
            {},
        )


def _write_export(export_dir, messages):
    export_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "about": "Telegram Desktop export",
        "personal_information": {"user_id": 4242, "first_name": "Account A"},
        "chats": {"list": [{"name": "Chat A", "type": "personal_chat", "id": 901001, "messages": messages}]},
    }
    (export_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def _export_message(msg_id, **extra):
    base = {
        "id": msg_id,
        "type": "message",
        "date": f"2024-01-15T10:{msg_id:02d}:00",
        "from": "Account A",
        "from_id": "user4242",
        "text": "",
    }
    base.update(extra)
    return base


async def _raw_data(adapter, chat_id, message_id):
    async with adapter.db_manager.async_session_factory() as session:
        raw = (
            await session.execute(select(Message.raw_data).where(Message.chat_id == chat_id, Message.id == message_id))
        ).scalar_one()
    return json.loads(raw) if raw else {}


async def test_json_import_writes_the_card_payloads(real_adapter, tmp_path):
    _write_export(
        tmp_path / "export",
        [
            _export_message(1, location_information={"latitude": DEMO_LAT, "longitude": DEMO_LONG}),
            _export_message(2, contact_information={"first_name": "Alex", "last_name": "", "phone_number": "+1 555"}),
            _export_message(3, place_name="Demo Cafe", address="1 Example St"),
        ],
    )
    importer = TelegramImporter(real_adapter, str(tmp_path / "media"), account_id=1)
    await importer.run(str(tmp_path / "export"))
    assert (await _raw_data(real_adapter, 901001, 1))["geo"] == {"lat": DEMO_LAT, "long": DEMO_LONG}
    assert (await _raw_data(real_adapter, 901001, 2))["contact"]["first_name"] == "Alex"
    assert (await _raw_data(real_adapter, 901001, 3))["venue"]["title"] == "Demo Cafe"


# ---------------------------------------------------------------------------
# The upsert keeps payloads (SQLite and PostgreSQL)
# ---------------------------------------------------------------------------


def _row(message_id, raw_data, *, text="", edit_date=None, source="backup"):
    return {
        "id": message_id,
        "chat_id": CHAT_ID,
        "sender_id": 4242,
        "date": datetime(2026, 3, 1, 12, 0),
        "text": text,
        "edit_date": edit_date,
        "raw_data": raw_data,
        "version_source": source,
    }


async def _seed_chat(adapter):
    await adapter.upsert_chat({"id": CHAT_ID, "type": "group", "title": "fixture chat"}, account_id=1)


class TestUpsertKeepsPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            {"poll": {"question": "Where next?", "answers": []}},
            {"geo": {"lat": DEMO_LAT, "long": DEMO_LONG}},
            {"contact": {"first_name": "Alex", "phone_number": DEMO_PHONE}},
            {"venue": {"title": "Demo Cafe"}},
        ],
    )
    async def test_an_import_merge_read_without_the_payload_keeps_it(self, real_adapter, payload):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(_row(11, payload), account_id=1)
        # A Telegram Desktop import of the same forwarded message carries only its forward name.
        await real_adapter.insert_message(_row(11, {"forward_from_name": "Channel A"}, source="import"), account_id=1)
        assert await _raw_data(real_adapter, CHAT_ID, 11) == {**payload, "forward_from_name": "Channel A"}

    async def test_a_newer_poll_tally_still_replaces_the_old_one(self, real_adapter):
        await _seed_chat(real_adapter)
        old = {"poll": {"question": "Q", "results": {"total_voters": 1}}}
        new = {"poll": {"question": "Q", "results": {"total_voters": 9}}}
        await real_adapter.insert_message(_row(12, old), account_id=1)
        await real_adapter.insert_message(_row(12, new), account_id=1)
        assert (await _raw_data(real_adapter, CHAT_ID, 12))["poll"]["results"]["total_voters"] == 9

    async def test_a_text_edit_with_other_extras_keeps_the_payload(self, real_adapter):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(_row(13, {"geo": {"lat": 1.0, "long": 2.0}}), account_id=1)
        edited = _row(13, {"post_author": "Author A"}, text="caption", edit_date=datetime(2026, 3, 1, 12, 5))
        await real_adapter.insert_message(edited, account_id=1)
        raw = await _raw_data(real_adapter, CHAT_ID, 13)
        assert raw["geo"] == {"lat": 1.0, "long": 2.0}
        assert raw["post_author"] == "Author A"

    async def test_live_location_reads_merge_through_the_upsert(self, real_adapter):
        await _seed_chat(real_adapter)
        first = {"geo_live": {"lat": 1.0, "long": 2.0, "period": 900, "at": "2026-03-01T12:02:00"}}
        later = {"geo_live": {"lat": 1.5, "long": 2.5, "period": 900, "at": "2026-03-01T12:14:00"}}
        await real_adapter.insert_message(_row(14, later), account_id=1)
        await real_adapter.insert_messages_batch([_row(14, first)], account_id=1)
        live = (await _raw_data(real_adapter, CHAT_ID, 14))["geo_live"]
        assert (live["lat"], live["at"]) == (1.5, "2026-03-01T12:14:00")
        assert live["earlier"] == [{"lat": 1.0, "long": 2.0, "at": "2026-03-01T12:02:00"}]

    async def test_an_unchanged_rescan_writes_nothing(self, real_adapter):
        await _seed_chat(real_adapter)
        payload = {"geo": {"lat": 1.0, "long": 2.0}, "grouped_id": "1"}
        await real_adapter.insert_message(_row(15, payload), account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            before = (await session.execute(select(Message.raw_data).where(Message.id == 15))).scalar_one()
        await real_adapter.insert_message(_row(15, {"grouped_id": "1"}), account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            after = (await session.execute(select(Message.raw_data).where(Message.id == 15))).scalar_one()
        assert after == before

    @pytest.mark.parametrize("kind", ["poll", "contact", "venue", "geo", "geo_live"])
    async def test_an_import_never_replaces_a_captured_payload(self, real_adapter, kind):
        """An export's stand-in is thinner than what Telegram served: no option bytes, no vCard, no provider."""
        await _seed_chat(real_adapter)
        stored, _frame = await _listener_capture(MEDIA_CASES[kind]())
        captured = stored["raw_data"] if isinstance(stored["raw_data"], dict) else json.loads(stored["raw_data"])
        assert kind in captured
        await real_adapter.insert_message(_row(16, captured, source="listener"), account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            before = (await session.execute(select(Message.raw_data).where(Message.id == 16))).scalar_one()
        export_shapes = {
            "poll": {
                "question": "Where next?",
                "answers": [{"text": "Lake", "option": "0"}, {"text": "Ridge", "option": "1"}],
                "results": {"total_voters": 2, "results": [{"option": "0", "voters": 2}]},
            },
            "contact": {"first_name": "Alex", "last_name": "Demo", "phone_number": "+1 555 555 0100"},
            "venue": {"title": "Demo Cafe", "address": "1 Example St", "lat": DEMO_LAT, "long": DEMO_LONG},
            "geo": {"lat": DEMO_LAT, "long": DEMO_LONG},
            # Same moment as the capture, so a newer-wins merge would have taken it.
            "geo_live": {"lat": 1.5, "long": 2.5, "period": 900, "at": captured.get("geo_live", {}).get("at")},
        }
        await real_adapter.insert_message(_row(16, {kind: export_shapes[kind]}, source="import"), account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            after = (await session.execute(select(Message.raw_data).where(Message.id == 16))).scalar_one()
        assert after == before

    async def test_an_import_fills_a_payload_the_archive_lacks(self, real_adapter):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(_row(17, {"grouped_id": "1"}), account_id=1)
        await real_adapter.insert_message(_row(17, {"geo": {"lat": 1.0, "long": 2.0}}, source="import"), account_id=1)
        assert (await _raw_data(real_adapter, CHAT_ID, 17))["geo"] == {"lat": 1.0, "long": 2.0}


class TestKeepArchivedPayloads:
    def test_returns_the_archived_string_when_nothing_changes(self):
        archived = json.dumps({"geo": {"lat": 1.0, "long": 2.0}, "grouped_id": "1"})
        assert _keep_archived_payloads(archived, json.dumps({"grouped_id": "1"}), incoming_wins=True) is archived

    def test_non_payload_keys_are_not_kept(self):
        archived = json.dumps({"post_author": "Author A"})
        incoming = json.dumps({"grouped_id": "1"})
        assert _keep_archived_payloads(archived, incoming, incoming_wins=True) == incoming

    def test_an_unreadable_archive_leaves_the_incoming_read(self):
        assert _keep_archived_payloads("not json", '{"geo": {}}', incoming_wins=False) == '{"geo": {}}'

    def test_only_a_telegram_read_replaces_a_held_payload(self):
        archived = json.dumps({"poll": {"question": "Q", "id": "77"}})
        incoming = json.dumps({"poll": {"question": "Q"}})
        assert _keep_archived_payloads(archived, incoming, incoming_wins=True) == incoming
        assert _keep_archived_payloads(archived, incoming, incoming_wins=False) is archived


# ---------------------------------------------------------------------------
# What the viewer reads: reply quotes, the export, media URLs
# ---------------------------------------------------------------------------


async def test_a_reply_names_a_card_with_no_media_row(real_adapter):
    """The listener writes no media row for a location or a contact; its payload names the kind."""
    await _seed_chat(real_adapter)
    venue = {"venue": {"title": "Demo Cafe", "address": "1 Example St"}}
    await real_adapter.insert_message(_row(21, venue), account_id=1)
    await real_adapter.insert_message(_row(22, {"contact": {"first_name": "Alex"}}), account_id=1)
    for reply_id, target in ((23, 21), (24, 22)):
        reply = _row(reply_id, {}, text="thanks")
        reply["reply_to_msg_id"] = target
        await real_adapter.insert_message(reply, account_id=1)
    rows = {m["id"]: m for m in await real_adapter.get_messages_paginated(CHAT_ID, limit=10, account_id=1)}
    assert rows[23]["reply_to_media_type"] == "venue"
    assert rows[23]["reply_to_media_title"] == "Demo Cafe"
    assert rows[24]["reply_to_media_type"] == "contact"
    assert "reply_to_media_title" not in rows[24]


async def test_the_viewer_export_carries_the_card_payloads(real_adapter):
    await _seed_chat(real_adapter)
    await real_adapter.insert_message(
        _row(31, {"geo": {"lat": 1.0, "long": 2.0}, "entities": [{"type": "bold"}]}), account_id=1
    )
    await real_adapter.insert_message(_row(32, {}, text="plain"), account_id=1)
    exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT_ID, account_id=1)}
    assert exported[31]["media_payload"] == {"geo": {"lat": 1.0, "long": 2.0}}
    assert "media_payload" not in exported[32]


def test_a_metadata_only_row_gets_no_media_url(monkeypatch, tmp_path):
    """A release from 2025-12 to 2026-04 left a .bin path on these rows; it must not become a download link."""
    pytest.importorskip("fastapi")
    from telegram_archive.web import main

    monkeypatch.setattr(main, "_media_root", tmp_path)
    monkeypatch.setattr(main, "_get_cached_avatar_path", lambda *_args: None)
    chat = SimpleNamespace(ref="r1")
    messages = [
        {"id": 1, "sender_id": 1, "media": {"id": "m1", "type": "contact", "file_path": f"{CHAT_ID}/1.bin"}},
        {"id": 2, "sender_id": 1, "media": {"id": "m2", "type": "photo", "file_path": f"{CHAT_ID}/2.jpg"}},
    ]
    main._attach_message_payload_urls(messages, chat)
    assert messages[0]["media"]["url"] is None
    assert messages[1]["media"]["url"]
