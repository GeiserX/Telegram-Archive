"""backfill-details: old locations, venues, live locations, contacts and polls (docs/design/location-and-contact.md).

Messages archived before these kinds were kept have a media row of the kind
and ``raw_data`` "{}". The command re-reads them from Telegram in batches and
adds only the missing key, and clears the leftover ``.bin`` path releases up
to v7.28.0 left on these rows, without touching the disk. The adapter tests
and the end-to-end runs use the real SQLite and PostgreSQL engines.

Demo coordinates, names and numbers only.
"""

import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl.types import (
    GeoPoint,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaGeoLive,
    MessageMediaPhoto,
    MessageMediaVenue,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_location_contact_capture import _poll  # noqa: E402

import telegram_archive.telegram_backup as telegram_backup  # noqa: E402
from telegram_archive.db.models import Media, Message  # noqa: E402
from telegram_archive.message_utils import contact_payload_from_vcard  # noqa: E402
from telegram_archive.telegram_backup import TelegramBackup  # noqa: E402

CHAT_A = -1001000000001
CHAT_B = -1001000000002
DEMO_LAT = 40.416775
DEMO_LONG = -3.70379
DEMO_PHONE = "15555550100"
SENT = datetime(2026, 3, 1, 12, 0)

# The file Telethon's _download_contact wrote for a contact (first name first in N).
TELETHON_VCARD = (
    "BEGIN:VCARD\nVERSION:4.0\nN:Alex;Demo;;;\nFN:Alex Demo\nTEL;TYPE=cell;VALUE=uri:tel:+15555550100\nEND:VCARD\n"
)


def _geo_point():
    return GeoPoint(long=DEMO_LONG, lat=DEMO_LAT, access_hash=1, accuracy_radius=None)


def _media(kind):
    if kind == "geo":
        return MessageMediaGeo(geo=_geo_point())
    if kind == "contact":
        return MessageMediaContact(phone_number=DEMO_PHONE, first_name="Alex", last_name="Demo", vcard="", user_id=0)
    if kind == "venue":
        return MessageMediaVenue(
            geo=_geo_point(), title="Demo Cafe", address="1 Example St", provider="", venue_id="", venue_type=""
        )
    if kind == "geo_live":
        return MessageMediaGeoLive(geo=_geo_point(), period=900)
    if kind == "poll":
        return _poll()
    raise AssertionError(kind)


# ---------------------------------------------------------------------------
# The vCard reader
# ---------------------------------------------------------------------------


class TestContactPayloadFromVcard:
    def test_reads_the_file_telethon_wrote(self):
        payload = contact_payload_from_vcard(TELETHON_VCARD.encode())
        assert payload == {
            "first_name": "Alex",
            "last_name": "Demo",
            "phone_number": DEMO_PHONE,
            "vcard": TELETHON_VCARD,
        }

    def test_folded_lines_and_crlf_are_unfolded(self):
        data = b"BEGIN:VCARD\r\nN:Al\r\n ex;Demo;;;\r\nTEL:tel:+1555\r\n 5550100\r\nEND:VCARD\r\n"
        payload = contact_payload_from_vcard(data)
        assert payload["first_name"] == "Alex"
        assert payload["phone_number"] == DEMO_PHONE

    def test_a_full_name_stands_in_when_there_is_no_n(self):
        payload = contact_payload_from_vcard(b"BEGIN:VCARD\nFN:Alex Demo\nEND:VCARD\n")
        assert payload["first_name"] == "Alex Demo"
        assert payload["phone_number"] == ""

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x00\x01\x02 not a vcard",
            b"\xff\xfe\xfa",
            b"hello\nworld\n",
            b"BEGIN:VCARD\nVERSION:4.0\nEND:VCARD\n",
            b"BEGIN:VCARD\n" + b"X" * (70 * 1024),
        ],
    )
    def test_anything_else_does_not_parse(self, data):
        assert contact_payload_from_vcard(data) is None


# ---------------------------------------------------------------------------
# Real engines: seed helpers
# ---------------------------------------------------------------------------


async def _seed_chat(adapter, chat_id=CHAT_A):
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "fixture chat"}, account_id=1)


async def _seed(adapter, chat_id, message_id, kind, *, raw_data=None, file_path=None, text="", downloaded=True):
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": SENT,
            "text": text,
            "raw_data": raw_data or {},
            "version_source": "backup",
        },
        account_id=1,
    )
    await adapter.insert_media(
        {
            "id": f"{chat_id}_{message_id}_{kind}",
            "type": kind,
            "message_id": message_id,
            "chat_id": chat_id,
            "file_path": file_path,
            "file_name": os.path.basename(file_path) if file_path else None,
            "file_size": 0,
            "downloaded": downloaded if file_path else False,
            "download_date": SENT if file_path else None,
        },
        account_id=1,
    )


async def _message(adapter, chat_id, message_id):
    async with adapter.db_manager.async_session_factory() as session:
        row = (
            await session.execute(
                select(Message.raw_data, Message.text, Message.date).where(
                    Message.account_id == 1, Message.chat_id == chat_id, Message.id == message_id
                )
            )
        ).one()
    return json.loads(row.raw_data) if row.raw_data else {}, row.text, row.date


async def _media_row(adapter, chat_id, message_id):
    async with adapter.db_manager.async_session_factory() as session:
        return (
            await session.execute(
                select(Media.file_path, Media.file_name, Media.download_date, Media.downloaded, Media.type).where(
                    Media.account_id == 1, Media.chat_id == chat_id, Media.message_id == message_id
                )
            )
        ).one()


# ---------------------------------------------------------------------------
# Adapter methods
# ---------------------------------------------------------------------------


class TestWorkList:
    async def test_lists_missing_keys_and_leftover_paths_grouped_and_ordered(self, real_adapter):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        await _seed(real_adapter, CHAT_A, 9, "geo")
        await _seed(real_adapter, CHAT_A, 3, "contact")
        await _seed(real_adapter, CHAT_A, 5, "poll", raw_data={"poll": {"question": "Q"}})
        await _seed(real_adapter, CHAT_A, 6, "venue", raw_data={"venue": {"title": "Demo Cafe"}}, file_path="/x.bin")
        await _seed(real_adapter, CHAT_B, 1, "geo_live")
        await _seed(real_adapter, CHAT_B, 2, "photo")
        await _seed(real_adapter, CHAT_B, 4, "dice")

        groups = await real_adapter.get_payload_backfill_rows(account_id=1)

        assert sorted(groups) == sorted([CHAT_A, CHAT_B])
        assert [(r["message_id"], r["type"], r["has_payload"]) for r in groups[CHAT_A]] == [
            (3, "contact", False),
            (6, "venue", True),
            (9, "geo", False),
        ]
        assert [(r["message_id"], r["type"]) for r in groups[CHAT_B]] == [(1, "geo_live")]
        assert groups[CHAT_A][1]["file_path"] == "/x.bin"

    async def test_one_chat_only(self, real_adapter):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        await _seed(real_adapter, CHAT_A, 1, "geo")
        await _seed(real_adapter, CHAT_B, 1, "geo")
        groups = await real_adapter.get_payload_backfill_rows(account_id=1, chat_id=CHAT_B)
        assert list(groups) == [CHAT_B]

    async def test_another_account_is_not_listed(self, real_adapter):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed(real_adapter, CHAT_A, 1, "geo")
        assert await real_adapter.get_payload_backfill_rows(account_id=2) == {}


class TestAddMissingRawDataKeys:
    async def test_adds_a_missing_key_and_touches_nothing_else(self, real_adapter):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "geo", raw_data={"forward_from_name": "Channel A"}, text="see you")
        before_raw, before_text, before_date = await _message(real_adapter, CHAT_A, 1)

        added = await real_adapter.add_missing_raw_data_keys(
            CHAT_A, 1, {"geo": {"lat": DEMO_LAT, "long": DEMO_LONG}}, account_id=1
        )

        raw, text, date = await _message(real_adapter, CHAT_A, 1)
        assert added is True
        assert raw == {**before_raw, "geo": {"lat": DEMO_LAT, "long": DEMO_LONG}}
        assert (text, date) == (before_text, before_date)

    async def test_never_replaces_a_key_the_row_holds(self, real_adapter):
        await _seed_chat(real_adapter)
        kept = {"question": "Archived tally", "results": {"total_voters": 9}}
        await _seed(real_adapter, CHAT_A, 1, "poll", raw_data={"poll": kept})

        added = await real_adapter.add_missing_raw_data_keys(
            CHAT_A, 1, {"poll": {"question": "A newer read"}}, account_id=1
        )

        assert added is False
        assert (await _message(real_adapter, CHAT_A, 1))[0] == {"poll": kept}

    async def test_the_second_call_adds_nothing(self, real_adapter):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "geo")
        payload = {"geo": {"lat": DEMO_LAT, "long": DEMO_LONG}}
        assert await real_adapter.add_missing_raw_data_keys(CHAT_A, 1, payload, account_id=1) is True
        assert await real_adapter.add_missing_raw_data_keys(CHAT_A, 1, payload, account_id=1) is False

    async def test_an_unknown_message_adds_nothing(self, real_adapter):
        await _seed_chat(real_adapter)
        assert await real_adapter.add_missing_raw_data_keys(CHAT_A, 404, {"geo": {}}, account_id=1) is False


class TestClearMetadataMediaPath:
    async def test_clears_the_file_fields_and_keeps_the_row(self, real_adapter):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "geo", file_path="/media/geo.bin")

        assert await real_adapter.clear_metadata_media_path(CHAT_A, f"{CHAT_A}_1_geo", account_id=1) is True

        row = await _media_row(real_adapter, CHAT_A, 1)
        assert (row.file_path, row.file_name, row.download_date, row.downloaded, row.type) == (
            None,
            None,
            None,
            0,
            "geo",
        )
        assert await real_adapter.clear_metadata_media_path(CHAT_A, f"{CHAT_A}_1_geo", account_id=1) is False

    async def test_a_file_backed_row_is_never_cleared(self, real_adapter):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "photo", file_path="/media/photo.jpg")
        assert await real_adapter.clear_metadata_media_path(CHAT_A, f"{CHAT_A}_1_photo", account_id=1) is False
        assert (await _media_row(real_adapter, CHAT_A, 1)).file_path == "/media/photo.jpg"


# ---------------------------------------------------------------------------
# The backfill, end to end on the real engines
# ---------------------------------------------------------------------------


class FakeTelegram:
    """get_entity / get_messages over a dict of served messages, recording every call."""

    def __init__(self, served, *, refused=(), reverse=False, flood_first=False, entity_error=None, batch_error=None):
        self.served = served  # {(chat, message_id): media}
        self.refused = set(refused)
        self.reverse = reverse
        self.flood_first = flood_first
        self.entity_error = entity_error  # raised by every get_entity
        self.batch_error = batch_error  # raised by every get_messages
        self.calls = []
        self.entity_calls = []

    async def get_entity(self, chat):
        self.entity_calls.append(chat)
        if chat in self.refused:
            raise ChannelPrivateError(request=None)
        if self.entity_error is not None:
            raise self.entity_error
        return SimpleNamespace(chat=chat)

    async def get_messages(self, entity, ids):
        self.calls.append(list(ids))
        if self.batch_error is not None:
            raise self.batch_error
        if self.flood_first:
            self.flood_first = False
            raise FloodWaitError(request=None, capture=3)
        out = []
        for mid in ids:
            media = self.served.get((entity.chat, mid))
            out.append(None if media is None else SimpleNamespace(id=mid, media=media, date=SENT, edit_date=None))
        return list(reversed(out)) if self.reverse else out


def _backup(adapter, client, media_root):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.db = adapter
    backup.client = client
    backup.config = MagicMock()
    backup.config.media_path = str(media_root)
    return backup


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(telegram_backup, "PAYLOAD_BACKFILL_PAUSE_SECONDS", 0)


async def _seed_all_kinds(adapter):
    await _seed_chat(adapter)
    for mid, kind in enumerate(("geo", "contact", "venue", "geo_live", "poll"), start=1):
        await _seed(adapter, CHAT_A, mid, kind)
    return {(CHAT_A, mid): _media(kind) for mid, kind in enumerate(("geo", "contact", "venue", "geo_live", "poll"), 1)}


class TestBackfill:
    async def test_a_dry_run_reads_and_counts_but_writes_nothing(self, real_adapter, tmp_path):
        served = await _seed_all_kinds(real_adapter)
        (tmp_path / "1_photo.jpg").write_bytes(b"demo")  # the media folder is there and holds files
        await _seed(real_adapter, CHAT_A, 9, "geo", file_path=str(tmp_path / "gone.bin"))
        served[(CHAT_A, 9)] = _media("geo")

        summary = await _backup(real_adapter, FakeTelegram(served), tmp_path).backfill_details()

        assert sum(k["filled"] for k in summary["kinds"].values()) == 6
        assert summary["paths_cleared"] == 1
        for mid in (1, 2, 3, 4, 5, 9):
            assert (await _message(real_adapter, CHAT_A, mid))[0] == {}
        assert (await _media_row(real_adapter, CHAT_A, 9)).file_path == str(tmp_path / "gone.bin")

    async def test_apply_fills_every_kind_and_a_second_run_fills_nothing(self, real_adapter, tmp_path):
        served = await _seed_all_kinds(real_adapter)
        client = FakeTelegram(served)
        backup = _backup(real_adapter, client, tmp_path)

        first = await backup.backfill_details(apply=True)
        stored = {mid: (await _message(real_adapter, CHAT_A, mid))[0] for mid in range(1, 6)}
        second = await backup.backfill_details(apply=True)

        assert {k: v["filled"] for k, v in first["kinds"].items()} == dict.fromkeys(
            ("contact", "geo", "geo_live", "poll", "venue"), 1
        )
        assert stored[1]["geo"] == {"lat": DEMO_LAT, "long": DEMO_LONG}
        assert stored[2]["contact"]["phone_number"] == DEMO_PHONE
        assert stored[3]["venue"]["title"] == "Demo Cafe"
        assert stored[4]["geo_live"]["period"] == 900
        assert stored[5]["poll"]["question"] == "Where next?"
        assert sum(v["filled"] for v in second["kinds"].values()) == 0
        # Nothing is left on the work list, so the second run asks Telegram nothing.
        assert len(client.calls) == 1
        assert {mid: (await _message(real_adapter, CHAT_A, mid))[0] for mid in range(1, 6)} == stored

    async def test_an_existing_key_is_never_replaced(self, real_adapter, tmp_path):
        """The race the row lock guards: a writer stores the key after the work list was read."""
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "poll")
        backup = _backup(real_adapter, FakeTelegram({(CHAT_A, 1): _media("poll")}), tmp_path)
        archived = {"question": "Archived tally", "results": {"total_voters": 9}}
        groups = await real_adapter.get_payload_backfill_rows(account_id=1)
        await real_adapter.add_missing_raw_data_keys(CHAT_A, 1, {"poll": archived}, account_id=1)

        with patch.object(real_adapter, "get_payload_backfill_rows", AsyncMock(return_value=groups)):
            summary = await backup.backfill_details(apply=True)

        assert summary["kinds"]["poll"] == {"filled": 0, "already_present": 1, "not_served": 0}
        assert (await _message(real_adapter, CHAT_A, 1))[0] == {"poll": archived}

    async def test_unserved_and_changed_messages_count_as_not_served(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "geo")
        await _seed(real_adapter, CHAT_A, 2, "contact")
        await _seed(real_adapter, CHAT_A, 3, "venue")
        # Message 1 is gone (None); 2 now holds a location and 3 a photo.
        served = {(CHAT_A, 2): _media("geo"), (CHAT_A, 3): MessageMediaPhoto(photo=None)}

        summary = await _backup(real_adapter, FakeTelegram(served), tmp_path).backfill_details(apply=True)

        assert summary["kinds"]["geo"] == {"filled": 0, "already_present": 0, "not_served": 1}
        assert summary["kinds"]["contact"] == {"filled": 0, "already_present": 0, "not_served": 1}
        assert summary["kinds"]["venue"] == {"filled": 0, "already_present": 0, "not_served": 1}
        for mid in (1, 2, 3):
            assert (await _message(real_adapter, CHAT_A, mid))[0] == {}, mid

    async def test_a_refused_chat_is_skipped_and_counted(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        await _seed(real_adapter, CHAT_A, 1, "geo")
        await _seed(real_adapter, CHAT_A, 2, "geo")
        await _seed(real_adapter, CHAT_B, 1, "geo")
        client = FakeTelegram({(CHAT_B, 1): _media("geo")}, refused={CHAT_A})

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert summary["chats_unavailable"] == 1
        assert summary["kinds"]["geo"] == {"filled": 1, "already_present": 0, "not_served": 2}
        assert "geo" in (await _message(real_adapter, CHAT_B, 1))[0]

    async def test_a_flood_wait_is_slept_out(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "geo")
        client = FakeTelegram({(CHAT_A, 1): _media("geo")}, flood_first=True)
        slept = []

        async def _fast_sleep(seconds):
            slept.append(seconds)

        with patch.object(telegram_backup.asyncio, "sleep", _fast_sleep):
            summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert slept and slept[0] >= 3
        assert len(client.calls) == 2
        assert summary["kinds"]["geo"]["filled"] == 1

    @pytest.mark.parametrize("where", ["get_messages", "get_entity"])
    async def test_a_flood_wait_too_long_to_sleep_out_stops_the_run(self, real_adapter, tmp_path, where):
        await _seed_chat(real_adapter, CHAT_A)
        await _seed_chat(real_adapter, CHAT_B)
        # The work list runs in chat id order, so CHAT_B (the lower id) comes first.
        for mid in range(1, 151):
            await _seed(real_adapter, CHAT_B, mid, "geo")
        await _seed(real_adapter, CHAT_A, 1, "geo")
        flood = FloodWaitError(request=None, capture=telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1)
        client = FakeTelegram({}, **({"batch_error": flood} if where == "get_messages" else {"entity_error": flood}))

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert summary["flood_wait_seconds"] == telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1
        # One refused call, then nothing more: not the second batch, not the next chat.
        assert client.entity_calls == [CHAT_B]
        assert len(client.calls) == (1 if where == "get_messages" else 0)
        assert summary["chats_unavailable"] == 0
        assert summary["kinds"]["geo"]["not_served"] == 0
        for chat, mid in ((CHAT_B, 1), (CHAT_B, 150), (CHAT_A, 1)):
            assert (await _message(real_adapter, chat, mid))[0] == {}

    async def test_results_out_of_order_land_on_the_right_ids(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await _seed(real_adapter, CHAT_A, 1, "contact")
        await _seed(real_adapter, CHAT_A, 2, "contact")
        served = {
            (CHAT_A, 1): MessageMediaContact(
                phone_number="15555550101", first_name="One", last_name="", vcard="", user_id=0
            ),
            (CHAT_A, 2): MessageMediaContact(
                phone_number="15555550102", first_name="Two", last_name="", vcard="", user_id=0
            ),
        }

        await _backup(real_adapter, FakeTelegram(served, reverse=True), tmp_path).backfill_details(apply=True)

        assert (await _message(real_adapter, CHAT_A, 1))[0]["contact"]["first_name"] == "One"
        assert (await _message(real_adapter, CHAT_A, 2))[0]["contact"]["first_name"] == "Two"

    async def test_ids_go_in_batches_of_one_hundred(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        served = {}
        for mid in range(1, 151):
            await _seed(real_adapter, CHAT_A, mid, "geo")
            served[(CHAT_A, mid)] = _media("geo")
        client = FakeTelegram(served)

        summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert [len(c) for c in client.calls] == [100, 50]
        assert summary["kinds"]["geo"]["filled"] == 150


class TestLeftoverPaths:
    async def test_cleanup_clears_only_what_points_at_nothing_or_is_kept(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        media = tmp_path / "media"
        shared = media / "_shared"
        shared.mkdir(parents=True)
        (shared / "vcard.bin").write_text(TELETHON_VCARD)
        (shared / "junk.bin").write_bytes(b"\x00\x01 not a vcard at all")
        (shared / "empty.bin").write_bytes(b"")
        (media / "dangling.bin").symlink_to(shared / "never-written.bin")
        (media / "vcard-link.bin").symlink_to(shared / "vcard.bin")
        on_disk_before = sorted(p.name for p in media.rglob("*"))

        await _seed(real_adapter, CHAT_A, 1, "geo", raw_data={"geo": {"lat": 1.0, "long": 2.0}}, file_path="/x/1.bin")
        await _seed(real_adapter, CHAT_A, 2, "geo", file_path=str(media / "missing.bin"))
        await _seed(real_adapter, CHAT_A, 3, "poll", file_path=str(shared / "empty.bin"))
        await _seed(real_adapter, CHAT_A, 4, "geo", file_path=str(media / "dangling.bin"))
        await _seed(real_adapter, CHAT_A, 5, "contact", file_path=str(media / "vcard-link.bin"))
        await _seed(real_adapter, CHAT_A, 6, "contact", file_path=str(shared / "junk.bin"))
        # Telegram serves none of them: the cleanup decides on its own.
        backup = _backup(real_adapter, FakeTelegram({}), media)

        summary = await backup.backfill_details(apply=True)

        assert summary["paths_cleared"] == 5
        assert summary["paths_kept"] == 1
        assert summary["vcards_recovered"] == 1
        for mid in (1, 2, 3, 4, 5):
            assert (await _media_row(real_adapter, CHAT_A, mid)).file_path is None, mid
        assert (await _media_row(real_adapter, CHAT_A, 6)).file_path == str(shared / "junk.bin")
        contact = (await _message(real_adapter, CHAT_A, 5))[0]["contact"]
        assert (contact["first_name"], contact["last_name"], contact["phone_number"]) == ("Alex", "Demo", DEMO_PHONE)
        assert (await _message(real_adapter, CHAT_A, 1))[0] == {"geo": {"lat": 1.0, "long": 2.0}}
        # The disk is left alone: every file and link is still there.
        assert sorted(p.name for p in media.rglob("*")) == on_disk_before

        again = await backup.backfill_details(apply=True)
        assert again["paths_cleared"] == 0
        assert again["paths_kept"] == 1
        assert again["vcards_recovered"] == 0

    async def test_a_payload_filled_by_this_run_clears_its_path(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        stale = tmp_path / "geo.bin"
        stale.write_bytes(b"leftover bytes")
        await _seed(real_adapter, CHAT_A, 1, "geo", file_path=str(stale))

        await _backup(real_adapter, FakeTelegram({(CHAT_A, 1): _media("geo")}), tmp_path).backfill_details(apply=True)

        assert (await _media_row(real_adapter, CHAT_A, 1)).file_path is None
        assert stale.read_bytes() == b"leftover bytes"

    async def test_a_relative_path_resolves_under_the_media_root(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "c.bin").write_text(TELETHON_VCARD)
        await _seed(real_adapter, CHAT_A, 1, "contact", file_path="chat/c.bin")

        summary = await _backup(real_adapter, FakeTelegram({}), tmp_path).backfill_details(apply=True)

        assert summary["vcards_recovered"] == 1
        assert (await _message(real_adapter, CHAT_A, 1))[0]["contact"]["first_name"] == "Alex"

    @pytest.mark.parametrize("media_folder", ["empty", "absent"])
    async def test_no_path_is_cleared_when_the_media_folder_is_not_there(self, real_adapter, tmp_path, media_folder):
        """A host install, an unmounted volume or a moved root: every stored path reads as missing."""
        await _seed_chat(real_adapter)
        media = tmp_path / "media"
        if media_folder == "empty":
            media.mkdir()
        await _seed(real_adapter, CHAT_A, 1, "contact", file_path=str(media / "chat" / "1.bin"))
        await _seed(real_adapter, CHAT_A, 2, "geo", raw_data={"geo": {"lat": 1.0}}, file_path=str(media / "2.bin"))
        await _seed(real_adapter, CHAT_A, 3, "poll", file_path="/data/backups/media/chat/3.bin")

        summary = await _backup(real_adapter, FakeTelegram({}), media).backfill_details(apply=True)

        assert summary["paths_cleared"] == 0
        assert summary["paths_kept"] == 3
        for mid in (1, 2, 3):
            assert (await _media_row(real_adapter, CHAT_A, mid)).file_path is not None, mid

    async def test_a_missing_file_outside_the_root_or_under_a_missing_folder_keeps_its_path(
        self, real_adapter, tmp_path
    ):
        await _seed_chat(real_adapter)
        media = tmp_path / "media"
        media.mkdir()
        (media / "1_photo.jpg").write_bytes(b"demo")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        await _seed(real_adapter, CHAT_A, 1, "contact", file_path=str(elsewhere / "1.bin"))
        await _seed(real_adapter, CHAT_A, 2, "contact", file_path=str(media / "gone-folder" / "2.bin"))
        await _seed(real_adapter, CHAT_A, 3, "geo", file_path=str(media / "3.bin"))

        summary = await _backup(real_adapter, FakeTelegram({}), media).backfill_details(apply=True)

        assert (await _media_row(real_adapter, CHAT_A, 1)).file_path == str(elsewhere / "1.bin")
        assert (await _media_row(real_adapter, CHAT_A, 2)).file_path == str(media / "gone-folder" / "2.bin")
        # The positive control: a missing file in a folder of the root is cleared.
        assert (await _media_row(real_adapter, CHAT_A, 3)).file_path is None
        assert (summary["paths_cleared"], summary["paths_kept"]) == (1, 2)

    @pytest.mark.parametrize("failure", ["batch", "chat", "unresolved"])
    async def test_a_row_telegram_never_answered_keeps_its_vcard_path(self, real_adapter, tmp_path, failure):
        await _seed_chat(real_adapter)
        (tmp_path / "c.bin").write_text(TELETHON_VCARD)
        await _seed(real_adapter, CHAT_A, 1, "contact", file_path=str(tmp_path / "c.bin"))
        # "unresolved": Telethon's ValueError for a peer this session cannot
        # resolve. Telegram refused nothing, so the chat is not "no longer served".
        error = ValueError("unresolved peer") if failure == "unresolved" else ConnectionError("transient")
        client = FakeTelegram(
            {(CHAT_A, 1): _media("contact")},
            **({"batch_error": error} if failure == "batch" else {"entity_error": error}),
        )

        with patch.object(telegram_backup, "call_with_flood_retry", lambda fn, *a, **k: fn(*a, **k)):
            summary = await _backup(real_adapter, client, tmp_path).backfill_details(apply=True)

        assert summary["errors"] == 1
        assert summary["chats_unavailable"] == 0
        assert summary["vcards_recovered"] == 0
        assert (await _media_row(real_adapter, CHAT_A, 1)).file_path == str(tmp_path / "c.bin")
        assert (await _message(real_adapter, CHAT_A, 1))[0] == {}

    async def test_a_vcard_that_could_not_be_stored_keeps_its_path(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        (tmp_path / "c.bin").write_text(TELETHON_VCARD)
        await _seed(real_adapter, CHAT_A, 1, "contact", file_path=str(tmp_path / "c.bin"))
        backup = _backup(real_adapter, FakeTelegram({}), tmp_path)

        with patch.object(real_adapter, "add_missing_raw_data_keys", AsyncMock(return_value=False)):
            summary = await backup.backfill_details(apply=True)

        assert summary["vcards_recovered"] == 0
        assert (summary["paths_cleared"], summary["paths_kept"]) == (0, 1)
        assert (await _media_row(real_adapter, CHAT_A, 1)).file_path == str(tmp_path / "c.bin")

    async def test_a_contact_a_concurrent_writer_stored_first_clears_the_path(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        (tmp_path / "c.bin").write_text(TELETHON_VCARD)
        await _seed(real_adapter, CHAT_A, 1, "contact", file_path=str(tmp_path / "c.bin"))
        backup = _backup(real_adapter, FakeTelegram({}), tmp_path)
        real_add = real_adapter.add_missing_raw_data_keys

        async def _writer_wins(chat, message_id, payload, *, account_id):
            await real_add(chat, message_id, {"contact": {"first_name": "Listener"}}, account_id=account_id)
            return await real_add(chat, message_id, payload, account_id=account_id)

        with patch.object(real_adapter, "add_missing_raw_data_keys", _writer_wins):
            summary = await backup.backfill_details(apply=True)

        assert summary["vcards_recovered"] == 1
        assert (await _media_row(real_adapter, CHAT_A, 1)).file_path is None
        assert (await _message(real_adapter, CHAT_A, 1))[0] == {"contact": {"first_name": "Listener"}}


# ---------------------------------------------------------------------------
# Accounts and the command line
# ---------------------------------------------------------------------------


def _summary(filled=0, errors=0):
    kinds = {
        k: {"filled": 0, "already_present": 0, "not_served": 0} for k in ("contact", "geo", "geo_live", "poll", "venue")
    }
    kinds["geo"]["filled"] = filled
    return {
        "kinds": kinds,
        "edits": {"hidden": 0, "shown": 0, "date_changed": 0, "already_filled": 0, "not_served": 0},
        "chats_scanned": 1,
        "chats_unavailable": 0,
        "paths_cleared": 0,
        "paths_kept": 0,
        "vcards_recovered": 0,
        "errors": errors,
    }


class TestAccounts:
    def _config(self, n):
        config = MagicMock()
        config.accounts = [SimpleNamespace(index=i) for i in range(1, n + 1)]
        config.for_account = MagicMock(side_effect=lambda index: f"config-{index}")
        return config

    def _patch(self, monkeypatch, outcomes):
        calls = []

        async def _create(cfg, **kwargs):
            calls.append(kwargs)
            backup = MagicMock()
            backup.connect = AsyncMock()
            backup.disconnect = AsyncMock()
            backup.db = MagicMock(close=AsyncMock())
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                backup.backfill_details = AsyncMock(side_effect=outcome)
            else:
                backup.backfill_details = AsyncMock(return_value=outcome)
            return backup

        monkeypatch.setattr(telegram_backup.TelegramBackup, "create", _create)
        return calls

    async def test_each_account_runs_with_its_resolver_and_the_counts_add_up(self, monkeypatch):
        calls = self._patch(monkeypatch, [_summary(filled=2), _summary(filled=3)])
        total = await telegram_backup.run_backfill_details(self._config(2), apply=True)
        assert total["kinds"]["geo"]["filled"] == 5
        assert total["chats_scanned"] == 2
        assert all(c["account"] is not None and c["account_resolver"] is not None for c in calls)

    async def test_one_failed_account_does_not_stop_the_other(self, monkeypatch):
        self._patch(monkeypatch, [RuntimeError("boom"), _summary(filled=1)])
        total = await telegram_backup.run_backfill_details(self._config(2))
        assert total["kinds"]["geo"]["filled"] == 1
        assert total["errors"] == 1

    async def test_a_single_account_failure_propagates(self, monkeypatch):
        self._patch(monkeypatch, [RuntimeError("boom")])
        with pytest.raises(RuntimeError):
            await telegram_backup.run_backfill_details(self._config(1))


class TestCommandLine:
    def _run(self, monkeypatch, argv, outcome):
        import telegram_archive.__main__ as cli

        seen = {}

        async def _fake(config, chat_id=None, apply=False):
            seen.update(chat_id=chat_id, apply=apply)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(telegram_backup, "run_backfill_details", _fake)
        monkeypatch.setattr("telegram_archive.config.Config", MagicMock())
        monkeypatch.setattr("telegram_archive.config.setup_logging", MagicMock())
        args = cli.create_parser().parse_args(argv)
        return cli.run_backfill_details(args), seen

    def test_it_is_a_dry_run_unless_given_apply(self, monkeypatch, capsys):
        rc, seen = self._run(monkeypatch, ["backfill-details"], _summary(filled=4))
        out = capsys.readouterr().out
        assert rc == 0
        assert seen == {"chat_id": None, "apply": False}
        assert "[DRY RUN] Details backfill complete:" in out
        assert "Nothing was written" in out

    def test_apply_and_one_chat(self, monkeypatch, capsys):
        rc, seen = self._run(monkeypatch, ["backfill-details", "--apply", "-c", "-1001"], _summary(filled=4))
        out = capsys.readouterr().out
        assert rc == 0
        assert seen == {"chat_id": -1001, "apply": True}
        assert "[DRY RUN]" not in out
        assert "Nothing was written" not in out

    def test_a_flood_wait_stop_is_printed(self, monkeypatch, capsys):
        summary = _summary(filled=1)
        summary["flood_wait_seconds"] = 7200
        rc, _seen = self._run(monkeypatch, ["backfill-details", "--apply"], summary)
        out = capsys.readouterr().out
        assert rc == 1
        assert "Stopped after a FloodWait of 7200 s" in out

    def test_a_failure_exits_one_with_the_type_only(self, monkeypatch, capsys):
        rc, _seen = self._run(monkeypatch, ["backfill-details"], RuntimeError("+15555550100"))
        err = capsys.readouterr().err
        assert rc == 1
        assert "RuntimeError" in err
        assert DEMO_PHONE not in err

    def test_main_dispatches_the_command(self, monkeypatch):
        import telegram_archive.__main__ as cli

        monkeypatch.setattr(cli, "run_backfill_details", MagicMock(return_value=0))
        monkeypatch.setattr(sys, "argv", ["telegram-archive", "backfill-details"])
        assert cli.main() == 0
        cli.run_backfill_details.assert_called_once()
