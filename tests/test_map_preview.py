"""A location's map picture, fetched from Telegram itself (docs/design/location-and-contact.md, "Map picture").

The official apps draw a location as a picture Telegram's servers render
(``upload.getWebFile`` with an ``inputWebFileGeoPointLocation`` sent to the
server config's ``webfile_dc_id``) and paint the pin over it. The archive
makes the same request, keeps the answer as the file of the location's own
media row, and the viewer serves it like any file. No third party sees the
point.

The Telegram side runs against a fake client that records the requests; the
database side runs on the real SQLite and PostgreSQL engines. Demo
coordinates only.
"""

import importlib
import os
import tempfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from telethon.errors import FloodWaitError, LocationInvalidError, RPCError
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.functions.upload import GetWebFileRequest
from telethon.tl.types import (
    GeoPoint,
    GeoPointEmpty,
    InputWebFileGeoPointLocation,
    MessageMediaGeo,
    MessageMediaGeoLive,
    MessageMediaVenue,
)

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_map_preview_"))

import telegram_archive.map_preview as map_preview  # noqa: E402
import telegram_archive.telegram_backup as telegram_backup  # noqa: E402
from telegram_archive.db.models import Chat, Media  # noqa: E402
from telegram_archive.message_utils import MAP_PREVIEW_TYPES, is_map_preview_name  # noqa: E402
from telegram_archive.telegram_backup import TelegramBackup  # noqa: E402

CHAT_ID = -1001000000077
DEMO_LAT = 40.416775
DEMO_LONG = -3.70379
DEMO_HASH = 5550100
SENT = datetime(2026, 3, 1, 12, 0)
PNG = b"\x89PNG\r\n\x1a\n" + b"demo map picture"
JPEG = b"\xff\xd8\xff\xe0" + b"demo map picture"


def _geo(lat=DEMO_LAT, long=DEMO_LONG, access_hash=DEMO_HASH):
    return GeoPoint(long=long, lat=lat, access_hash=access_hash, accuracy_radius=None)


def _media(kind, geo=None):
    geo = geo if geo is not None else _geo()
    if kind == "geo":
        return MessageMediaGeo(geo=geo)
    if kind == "venue":
        return MessageMediaVenue(
            geo=geo, title="Demo Cafe", address="1 Example St", provider="", venue_id="", venue_type=""
        )
    if kind == "geo_live":
        return MessageMediaGeoLive(geo=geo, period=900)
    raise AssertionError(kind)


class FakeTelegram:
    """The client calls fetch_map_preview makes, recorded; ``answers`` are returned or raised in turn."""

    def __init__(self, answers=None, *, home_dc=2, webfile_dc=4):
        self.answers = list(answers) if answers is not None else [PNG]
        self.session = SimpleNamespace(dc_id=home_dc)
        self.webfile_dc = webfile_dc
        self._sender = "home-sender"
        self.calls = []  # (sender, request, flood_sleep_threshold)
        self.borrowed = []
        self.returned = []
        self.config_calls = 0

    async def __call__(self, request):
        assert isinstance(request, GetConfigRequest), request
        self.config_calls += 1
        return SimpleNamespace(webfile_dc_id=self.webfile_dc)

    async def _borrow_exported_sender(self, dc_id):
        self.borrowed.append(dc_id)
        return f"sender-{dc_id}"

    async def _return_exported_sender(self, sender):
        self.returned.append(sender)

    async def _call(self, sender, request, flood_sleep_threshold=None):
        self.calls.append((sender, request, flood_sleep_threshold))
        answer = self.answers.pop(0) if self.answers else PNG
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(bytes=answer, mime_type="image/png", size=len(answer))


@pytest.fixture(autouse=True)
def _fresh_config(monkeypatch):
    """help.getConfig is read once per process; every test starts unread."""
    monkeypatch.setattr(map_preview, "_webfile_dc_id", None)
    monkeypatch.setattr(telegram_backup, "PAYLOAD_BACKFILL_PAUSE_SECONDS", 0)


# ---------------------------------------------------------------------------
# The request and the file
# ---------------------------------------------------------------------------


class TestFetch:
    async def test_asks_telegram_desktops_picture_on_the_webfile_data_centre(self, tmp_path):
        client = FakeTelegram()
        result = await map_preview.fetch_map_preview(client, _media("geo"), str(tmp_path), flood_sleep_threshold=60)

        assert result["status"] == "saved"
        ((sender, request, threshold),) = client.calls
        assert isinstance(request, GetWebFileRequest)
        assert (request.offset, request.limit) == (0, 512 * 1024)
        location = request.location
        assert isinstance(location, InputWebFileGeoPointLocation)
        assert (location.w, location.h, location.zoom, location.scale) == (320, 240, 15, 2)
        assert location.access_hash == DEMO_HASH
        assert (location.geo_point.lat, location.geo_point.long) == (DEMO_LAT, DEMO_LONG)
        assert location.geo_point.accuracy_radius is None
        # Sent on the server config's webfile_dc_id, through a borrowed sender that goes back.
        assert client.config_calls == 1
        assert sender == "sender-4" and client.borrowed == [4] and client.returned == ["sender-4"]
        assert threshold == 60

    async def test_from_the_webfile_data_centre_itself_the_own_sender_is_used(self, tmp_path):
        client = FakeTelegram(home_dc=4)
        await map_preview.fetch_map_preview(client, _media("venue"), str(tmp_path))
        assert client.calls[0][0] == "home-sender"
        assert client.borrowed == [] and client.returned == []

    async def test_the_server_config_is_read_once(self, tmp_path):
        client = FakeTelegram()
        await map_preview.fetch_map_preview(client, _media("geo"), str(tmp_path))
        await map_preview.fetch_map_preview(client, _media("geo", _geo(lat=1.5, long=2.5)), str(tmp_path))
        assert client.config_calls == 1
        assert len(client.calls) == 2

    async def test_no_config_answer_falls_back_to_data_centre_4(self, tmp_path):
        client = FakeTelegram(webfile_dc=0)
        await map_preview.fetch_map_preview(client, _media("geo"), str(tmp_path))
        assert client.borrowed == [4]

    @pytest.mark.parametrize("kind", MAP_PREVIEW_TYPES)
    async def test_each_kind_keeps_its_picture_on_disk(self, tmp_path, kind):
        result = await map_preview.fetch_map_preview(FakeTelegram(), _media(kind), str(tmp_path))
        assert result["status"] == "saved"
        assert is_map_preview_name(result["file_name"])
        assert result["file_path"] == str(tmp_path / result["file_name"])
        assert (tmp_path / result["file_name"]).read_bytes() == PNG
        assert (result["file_size"], result["mime_type"], result["width"], result["height"]) == (
            len(PNG),
            "image/png",
            640,
            480,
        )
        # No partial file is left behind.
        assert [p.name for p in tmp_path.iterdir()] == [result["file_name"]]

    async def test_the_extension_follows_the_bytes(self, tmp_path):
        result = await map_preview.fetch_map_preview(FakeTelegram([JPEG]), _media("geo"), str(tmp_path))
        assert result["file_name"].endswith(".jpg") and result["mime_type"] == "image/jpeg"
        other = await map_preview.fetch_map_preview(
            FakeTelegram([PNG]), _media("geo", _geo(lat=1.5, long=2.5)), str(tmp_path)
        )
        assert other["file_name"].endswith(".png")

    async def test_an_answer_that_is_no_picture_writes_nothing(self, tmp_path):
        result = await map_preview.fetch_map_preview(FakeTelegram([b"<html>"]), _media("geo"), str(tmp_path))
        assert result == {"status": "error"}
        assert list(tmp_path.iterdir()) == []

    async def test_a_picture_in_several_parts_is_joined(self, tmp_path):
        first = PNG + b"x" * (512 * 1024 - len(PNG))
        client = FakeTelegram([first, b"tail"])
        result = await map_preview.fetch_map_preview(client, _media("geo"), str(tmp_path))
        assert [request.offset for _sender, request, _t in client.calls] == [0, 512 * 1024]
        assert (tmp_path / result["file_name"]).read_bytes() == first + b"tail"

    async def test_an_existing_picture_is_reused_byte_for_byte_with_no_request(self, tmp_path):
        stem = map_preview.map_preview_stem(DEMO_LAT, DEMO_LONG)
        kept = tmp_path / f"{stem}.jpg"
        kept.write_bytes(JPEG)
        before = kept.stat().st_mtime_ns
        client = FakeTelegram([PNG])

        result = await map_preview.fetch_map_preview(client, _media("venue"), str(tmp_path))

        assert client.calls == [] and client.config_calls == 0
        assert result["file_path"] == str(kept) and result["mime_type"] == "image/jpeg"
        assert kept.read_bytes() == JPEG and kept.stat().st_mtime_ns == before
        assert [p.name for p in tmp_path.iterdir()] == [kept.name]

    async def test_the_name_is_the_point_and_never_the_message(self, tmp_path):
        one = await map_preview.fetch_map_preview(FakeTelegram(), _media("geo"), str(tmp_path))
        same = await map_preview.fetch_map_preview(FakeTelegram(), _media("geo_live"), str(tmp_path))
        other = await map_preview.fetch_map_preview(FakeTelegram(), _media("geo", _geo(lat=1.5)), str(tmp_path))
        assert one["file_name"] == same["file_name"] != other["file_name"]

    @pytest.mark.parametrize(
        "media",
        [
            MessageMediaGeo(geo=GeoPointEmpty()),
            MessageMediaGeo(geo=_geo(access_hash=0)),
            MessageMediaGeo(geo=None),
            MagicMock(),
        ],
        ids=["empty-point", "no-access-hash", "no-point", "magicmock"],
    )
    async def test_nothing_telegram_can_render_makes_no_request(self, tmp_path, media):
        client = FakeTelegram()
        assert await map_preview.fetch_map_preview(client, media, str(tmp_path)) == {"status": "no_point"}
        assert client.calls == [] and client.config_calls == 0
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (LocationInvalidError(request=None), "not_served"),
            (RPCError(request=None, message="WEBFILE_NOT_AVAILABLE", code=400), "not_served"),
            (RPCError(request=None, message="SOMETHING_ELSE", code=400), "error"),
            (ConnectionError("down"), "error"),
        ],
        ids=["location-invalid", "webfile-not-available", "other-rpc", "network"],
    )
    async def test_a_refusal_or_an_error_writes_nothing(self, tmp_path, error, status):
        client = FakeTelegram([error])
        assert await map_preview.fetch_map_preview(client, _media("geo"), str(tmp_path)) == {"status": status}
        assert list(tmp_path.iterdir()) == []
        assert client.returned == ["sender-4"]  # the borrowed sender goes back on error too

    async def test_a_long_flood_wait_says_how_long(self, tmp_path):
        client = FakeTelegram([FloodWaitError(request=None, capture=900)])
        assert await map_preview.fetch_map_preview(client, _media("geo"), str(tmp_path)) == {
            "status": "flood",
            "seconds": 900,
        }


class TestPreviewName:
    @pytest.mark.parametrize("name", ["map_0123456789abcdef.png", "map_0123456789abcdef.jpg"])
    def test_a_picture_name(self, name):
        assert is_map_preview_name(name)

    @pytest.mark.parametrize(
        "name",
        [None, "", "123456.bin", "map_0123456789abcdef.webp", "map_0123456789ABCDEF.png", "x_map_0123456789abcdef.png"],
    )
    def test_anything_else(self, name):
        assert not is_map_preview_name(name)


# ---------------------------------------------------------------------------
# Real engines: shared seed helpers
# ---------------------------------------------------------------------------


async def _seed_message(adapter, message_id, raw_data=None, chat_id=CHAT_ID):
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "fixture chat"}, account_id=1)
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": SENT,
            "text": "",
            "raw_data": raw_data or {},
            "version_source": "backup",
        },
        account_id=1,
    )


async def _media_row(adapter, message_id, chat_id=CHAT_ID):
    async with adapter.db_manager.async_session_factory() as session:
        return (
            await session.execute(
                select(Media).where(Media.account_id == 1, Media.chat_id == chat_id, Media.message_id == message_id)
            )
        ).scalar_one_or_none()


def _telegram_message(message_id, media):
    message = MagicMock()
    message.id = message_id
    message.media = media
    message.edit_date = None
    message.edit_hide = False
    message.date = SENT.replace(tzinfo=UTC)
    message.reply_to = None
    return message


def _backup(adapter, client, media_root):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.db = adapter
    backup.client = client
    backup.config = MagicMock()
    backup.config.media_path = str(media_root)
    backup.config.should_download_media_for_chat = MagicMock(return_value=True)
    backup.config.should_skip_topic = MagicMock(return_value=False)
    backup.config.download_youtube_videos = False
    backup.config.media_flood_sleep_threshold = 60
    return backup


# ---------------------------------------------------------------------------
# The scheduled backup
# ---------------------------------------------------------------------------


class TestBackup:
    async def test_a_location_read_keeps_its_picture_on_its_row(self, real_adapter, tmp_path):
        await _seed_message(real_adapter, 1)
        backup = _backup(real_adapter, FakeTelegram(), tmp_path)

        row = await backup._process_media(_telegram_message(1, _media("geo")), CHAT_ID)
        await real_adapter.insert_media(row, account_id=1)

        stored = await _media_row(real_adapter, 1)
        assert stored.type == "geo"
        assert stored.downloaded == 1
        assert is_map_preview_name(stored.file_name)
        assert stored.file_path == str(tmp_path / str(CHAT_ID) / stored.file_name)
        assert (stored.file_size, stored.mime_type, stored.width, stored.height) == (len(PNG), "image/png", 640, 480)
        assert os.path.isfile(stored.file_path)

    async def test_a_later_read_keeps_the_picture_and_asks_nothing(self, real_adapter, tmp_path):
        """A read used to send downloaded False and size 0, which reset both on a row holding a file."""
        await _seed_message(real_adapter, 2)
        first = await _backup(real_adapter, FakeTelegram(), tmp_path)._process_media(
            _telegram_message(2, _media("venue")), CHAT_ID
        )
        await real_adapter.insert_media(first, account_id=1)
        before = await _media_row(real_adapter, 2)

        client = FakeTelegram()
        again = await _backup(real_adapter, client, tmp_path)._process_media(
            _telegram_message(2, _media("venue")), CHAT_ID
        )
        await real_adapter.insert_media(again, account_id=1)

        after = await _media_row(real_adapter, 2)
        assert client.calls == []
        assert (after.downloaded, after.file_size, after.file_path, after.file_name) == (
            1,
            before.file_size,
            before.file_path,
            before.file_name,
        )

    async def test_a_backup_read_keeps_a_picture_the_listener_stored(self, real_adapter, tmp_path):
        await _seed_message(real_adapter, 3)
        picture = tmp_path / str(CHAT_ID) / (map_preview.map_preview_stem(1.5, DEMO_LONG) + ".png")
        picture.parent.mkdir(parents=True)
        picture.write_bytes(PNG)
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_3_geo_live",
                "type": "geo_live",
                "message_id": 3,
                "chat_id": CHAT_ID,
                "file_name": picture.name,
                "file_path": str(picture),
                "file_size": len(PNG),
                "downloaded": True,
            },
            account_id=1,
        )

        client = FakeTelegram()
        row = await _backup(real_adapter, client, tmp_path)._process_media(
            _telegram_message(3, _media("geo_live", _geo(lat=1.5))), CHAT_ID
        )
        await real_adapter.insert_media(row, account_id=1)

        stored = await _media_row(real_adapter, 3)
        assert (stored.downloaded, stored.file_size, stored.file_path) == (1, len(PNG), str(picture))
        assert client.calls == []

    async def test_a_moved_live_location_gets_the_new_points_picture(self, real_adapter, tmp_path):
        """The Telegram apps draw the latest position; the earlier picture stays on disk."""
        await _seed_message(real_adapter, 8)
        first = await _backup(real_adapter, FakeTelegram(), tmp_path)._process_media(
            _telegram_message(8, _media("geo_live")), CHAT_ID
        )
        await real_adapter.insert_media(first, account_id=1)
        before = await _media_row(real_adapter, 8)

        client = FakeTelegram([JPEG])
        moved = await _backup(real_adapter, client, tmp_path)._process_media(
            _telegram_message(8, _media("geo_live", _geo(lat=1.5))), CHAT_ID
        )
        await real_adapter.insert_media(moved, account_id=1)

        after = await _media_row(real_adapter, 8)
        assert len(client.calls) == 1
        assert after.id == before.id
        assert after.file_name == map_preview.map_preview_stem(1.5, DEMO_LONG) + ".jpg"
        assert (after.downloaded, after.mime_type) == (1, "image/jpeg")
        assert os.path.isfile(after.file_path)
        assert os.path.isfile(before.file_path)  # never removed

    async def test_a_venue_read_at_another_point_keeps_its_picture(self, real_adapter, tmp_path):
        """Control: only a live location moves, so only its picture follows the point."""
        await _seed_message(real_adapter, 9)
        first = await _backup(real_adapter, FakeTelegram(), tmp_path)._process_media(
            _telegram_message(9, _media("venue")), CHAT_ID
        )
        await real_adapter.insert_media(first, account_id=1)
        client = FakeTelegram()
        await _backup(real_adapter, client, tmp_path)._process_media(
            _telegram_message(9, _media("venue", _geo(lat=1.5))), CHAT_ID
        )
        assert client.calls == []

    async def test_a_picture_never_lands_behind_an_old_bin_link(self, real_adapter, tmp_path):
        """A row up to 7.28.0 can name a dangling link into _shared; the picture keeps its own path."""
        await _seed_message(real_adapter, 10)
        chat_dir = tmp_path / str(CHAT_ID)
        chat_dir.mkdir()
        (tmp_path / "_shared").mkdir()
        link = chat_dir / "123.bin"
        os.symlink(os.path.join("..", "_shared", "ab", "123.bin"), link)
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_10_geo",
                "type": "geo",
                "message_id": 10,
                "chat_id": CHAT_ID,
                "file_name": "123.bin",
                "file_path": str(link),
                "file_size": 0,
                "downloaded": True,
            },
            account_id=1,
        )

        row = await _backup(real_adapter, FakeTelegram(), tmp_path)._process_media(
            _telegram_message(10, _media("geo")), CHAT_ID
        )
        await real_adapter.insert_media(row, account_id=1)

        stored = await _media_row(real_adapter, 10)
        assert is_map_preview_name(stored.file_name)
        assert stored.file_path == str(chat_dir / stored.file_name)
        assert [entry for _dir, _subdirs, files in os.walk(tmp_path / "_shared") for entry in files] == []

    async def test_no_picture_leaves_the_metadata_row(self, real_adapter, tmp_path):
        await _seed_message(real_adapter, 4)
        row = await _backup(real_adapter, FakeTelegram([LocationInvalidError(None)]), tmp_path)._process_media(
            _telegram_message(4, _media("geo")), CHAT_ID
        )
        await real_adapter.insert_media(row, account_id=1)
        stored = await _media_row(real_adapter, 4)
        assert (stored.type, stored.downloaded, stored.file_path, stored.file_name) == ("geo", 0, None, None)
        assert stored.skip_reason == "map_not_served"

    async def test_one_flood_answer_stops_the_pictures_for_the_run(self, real_adapter, tmp_path):
        await _seed_message(real_adapter, 5)
        await _seed_message(real_adapter, 6)
        client = FakeTelegram([FloodWaitError(request=None, capture=900), PNG])
        backup = _backup(real_adapter, client, tmp_path)

        await backup._process_media(_telegram_message(5, _media("geo")), CHAT_ID)
        second = await backup._process_media(_telegram_message(6, _media("geo", _geo(lat=1.5))), CHAT_ID)

        assert len(client.calls) == 1
        assert "file_path" not in second

    async def test_a_contact_row_is_unchanged(self, real_adapter, tmp_path):
        """Control: the other metadata-only kinds keep the row they always had."""
        from telethon.tl.types import MessageMediaContact

        await _seed_message(real_adapter, 7)
        contact = MessageMediaContact(phone_number="15555550100", first_name="A", last_name="B", vcard="", user_id=0)
        row = await _backup(real_adapter, FakeTelegram(), tmp_path)._process_media(
            _telegram_message(7, contact), CHAT_ID
        )
        assert (row["type"], row["file_size"], row["downloaded"]) == ("contact", 0, False)


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


def _listener(adapter, client, media_root):
    from test_listener_extended import _make_config

    from telegram_archive.listener import TelegramListener

    config = _make_config(media_path=str(media_root), media_flood_sleep_threshold=60)
    listener = TelegramListener(config, adapter, account_id=1)
    listener.client = client
    return listener


class TestListener:
    async def test_a_new_location_writes_its_row_with_the_picture(self, real_adapter, tmp_path, monkeypatch):
        await _seed_message(real_adapter, 11)
        listener = _listener(real_adapter, FakeTelegram(), tmp_path)

        ws_media = await listener._store_message_media(_telegram_message(11, _media("venue")), CHAT_ID, "venue")

        stored = await _media_row(real_adapter, 11)
        assert stored is not None and stored.downloaded == 1 and is_map_preview_name(stored.file_name)
        assert stored.id == f"{CHAT_ID}_11_venue"
        assert ws_media["type"] == "venue" and ws_media["file_name"] == stored.file_name
        # The live frame the viewer receives carries the picture's URL.
        pytest.importorskip("fastapi")
        from telegram_archive.web import main

        monkeypatch.setattr(main, "_media_root", tmp_path.resolve())
        shaped, no_download = main._frame_media(ws_media, 11, "r1")
        assert shaped["url"] == "/media/r1/11_venue"
        assert no_download["url"] is None

    async def test_no_picture_writes_no_row(self, real_adapter, tmp_path):
        await _seed_message(real_adapter, 12)
        listener = _listener(real_adapter, FakeTelegram([LocationInvalidError(None)]), tmp_path)
        assert await listener._store_message_media(_telegram_message(12, _media("geo")), CHAT_ID, "geo") is None
        assert await _media_row(real_adapter, 12) is None

    async def test_media_off_fetches_nothing(self, real_adapter, tmp_path):
        await _seed_message(real_adapter, 13)
        client = FakeTelegram()
        listener = _listener(real_adapter, client, tmp_path)
        listener.config.listen_new_messages_media = False
        assert await listener._store_message_media(_telegram_message(13, _media("geo")), CHAT_ID, "geo") is None
        assert client.calls == []


# ---------------------------------------------------------------------------
# backfill-details
# ---------------------------------------------------------------------------


class BackfillTelegram(FakeTelegram):
    """FakeTelegram plus get_entity / get_messages over ``served`` {message_id: media}."""

    def __init__(self, served, answers=None):
        super().__init__(answers)
        self.served = served
        self.read_calls = []

    async def get_entity(self, chat):
        return SimpleNamespace(chat=chat)

    async def get_messages(self, entity, ids):
        self.read_calls.append(list(ids))
        return [
            SimpleNamespace(id=mid, media=self.served[mid], date=SENT, edit_date=None) if mid in self.served else None
            for mid in ids
        ]


async def _seed_location(adapter, message_id, kind="geo", *, payload=True, file_path=None, file_name=None):
    raw = {kind: {"lat": DEMO_LAT, "long": DEMO_LONG}} if payload else {}
    await _seed_message(adapter, message_id, raw)
    await adapter.insert_media(
        {
            "id": f"{CHAT_ID}_{message_id}_{kind}",
            "type": kind,
            "message_id": message_id,
            "chat_id": CHAT_ID,
            "file_path": file_path,
            "file_name": file_name if file_name is not None else (os.path.basename(file_path) if file_path else None),
            "file_size": 0,
            "downloaded": bool(file_path),
        },
        account_id=1,
    )


def _media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "other.jpg").write_bytes(b"demo")  # the media folder is there and holds files
    return root


class TestBackfill:
    async def test_an_old_location_gets_its_picture_and_a_second_run_asks_nothing(self, real_adapter, tmp_path):
        root = _media_root(tmp_path)
        await _seed_location(real_adapter, 1, "geo")
        await _seed_location(real_adapter, 2, "venue", payload=False)
        client = BackfillTelegram({1: _media("geo"), 2: _media("venue", _geo(lat=1.5))})
        backup = _backup(real_adapter, client, root)

        first = await backup.backfill_details(apply=True)
        rows = {mid: await _media_row(real_adapter, mid) for mid in (1, 2)}
        second = await backup.backfill_details(apply=True)

        assert first["maps"] == {"saved": 2, "not_served": 0, "no_point": 0, "deferred": 0, "errors": 0}
        assert first["kinds"]["venue"]["filled"] == 1
        # Listed for its picture only, not for a leftover path.
        assert first["kinds"]["geo"]["already_present"] == 0
        for row in rows.values():
            assert row.downloaded == 1 and is_map_preview_name(row.file_name) and os.path.isfile(row.file_path)
        assert second["maps"]["saved"] == 0
        # One read for both rows in the first run, and nothing left to read in the second.
        assert client.read_calls == [[1, 2]]
        assert len(client.calls) == 2

    async def test_a_picture_path_is_never_taken_for_a_leftover(self, real_adapter, tmp_path):
        root = _media_root(tmp_path)
        picture = root / str(CHAT_ID) / "map_0123456789abcdef.png"
        picture.parent.mkdir()
        picture.write_bytes(PNG)
        await _seed_location(real_adapter, 3, "geo", file_path=str(picture))
        client = BackfillTelegram({3: _media("geo")})

        summary = await _backup(real_adapter, client, root).backfill_details(apply=True)

        assert summary["paths_cleared"] == 0
        assert client.read_calls == []
        stored = await _media_row(real_adapter, 3)
        assert (stored.file_path, stored.downloaded) == (str(picture), 1)

    async def test_a_leftover_bin_path_gives_way_to_the_picture(self, real_adapter, tmp_path):
        """The leftover is cleared only while the row still holds it, so the picture just stored stays."""
        root = _media_root(tmp_path)
        gone = str(root / str(CHAT_ID) / "123.bin")
        await _seed_location(real_adapter, 4, "geo", payload=False, file_path=gone)
        client = BackfillTelegram({4: _media("geo")})

        summary = await _backup(real_adapter, client, root).backfill_details(apply=True)

        stored = await _media_row(real_adapter, 4)
        assert is_map_preview_name(stored.file_name)
        assert stored.file_path.endswith(stored.file_name) and os.path.isfile(stored.file_path)
        assert stored.downloaded == 1
        assert summary["paths_cleared"] == 1
        assert summary["maps"]["saved"] == 1

    async def test_the_clear_never_touches_a_row_whose_path_changed(self, real_adapter, tmp_path):
        await _seed_location(real_adapter, 5, "geo", file_path="/media/5.bin")
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_5_geo",
                "type": "geo",
                "message_id": 5,
                "chat_id": CHAT_ID,
                "file_path": "/media/map_0123456789abcdef.png",
                "file_name": "map_0123456789abcdef.png",
                "downloaded": True,
            },
            account_id=1,
        )
        cleared = await real_adapter.clear_metadata_media_path(
            CHAT_ID, f"{CHAT_ID}_5_geo", account_id=1, file_path="/media/5.bin"
        )
        assert cleared is False
        assert (await _media_row(real_adapter, 5)).file_path == "/media/map_0123456789abcdef.png"
        # Control: the exact path is cleared.
        assert await real_adapter.clear_metadata_media_path(
            CHAT_ID, f"{CHAT_ID}_5_geo", account_id=1, file_path="/media/map_0123456789abcdef.png"
        )

    async def test_the_cap_defers_the_rest_and_a_dry_run_fetches_nothing(self, real_adapter, tmp_path, monkeypatch):
        monkeypatch.setattr(telegram_backup, "MAP_PREVIEW_BACKFILL_MAX_PER_RUN", 2)
        root = _media_root(tmp_path)
        served = {}
        for mid in range(1, 5):
            await _seed_location(real_adapter, mid, "geo")
            served[mid] = _media("geo", _geo(lat=float(mid)))
        client = BackfillTelegram(served)
        backup = _backup(real_adapter, client, root)

        dry = await backup.backfill_details(apply=False)
        assert client.calls == []
        assert (dry["maps"]["saved"], dry["maps"]["deferred"]) == (2, 2)
        for mid in range(1, 5):
            assert (await _media_row(real_adapter, mid)).file_name is None
        # A deferred row is not read either.
        assert client.read_calls == [[1, 2]]

        applied = await backup.backfill_details(apply=True)
        assert (applied["maps"]["saved"], applied["maps"]["deferred"]) == (2, 2)
        assert applied["kinds"]["geo"]["already_present"] == 0
        assert len(client.calls) == 2
        assert client.read_calls[1:] == [[1, 2]]
        rest = await backup.backfill_details(apply=True)
        assert (rest["maps"]["saved"], rest["maps"]["deferred"]) == (2, 0)
        assert client.read_calls[2:] == [[3, 4]]

        # A run with every picture deferred reads nothing.
        monkeypatch.setattr(telegram_backup, "MAP_PREVIEW_BACKFILL_MAX_PER_RUN", 0)
        await _seed_location(real_adapter, 5, "geo")
        none_left = await backup.backfill_details(apply=True)
        assert (none_left["maps"]["saved"], none_left["maps"]["deferred"]) == (0, 1)
        assert len(client.read_calls) == 3

    async def test_no_point_and_refusals_are_counted_and_not_asked_again(self, real_adapter, tmp_path):
        """A refusal leaves the list, so it never spends the cap of a later run; an error stays."""
        root = _media_root(tmp_path)
        for mid in range(1, 5):
            await _seed_location(real_adapter, mid, "geo")
        client = BackfillTelegram(
            {
                1: _media("geo", _geo(access_hash=0)),
                2: _media("geo", _geo(lat=1.5)),
                3: _media("geo", _geo(lat=2.5)),
                # 4: Telegram no longer returns the message.
            },
            answers=[LocationInvalidError(None), RPCError(request=None, message="SOMETHING_ELSE", code=400)],
        )
        backup = _backup(real_adapter, client, root)

        dry = await backup.backfill_details(apply=False)
        assert client.calls == []
        listed = await real_adapter.get_payload_backfill_rows(account_id=1)
        assert sorted(row["message_id"] for row in listed[CHAT_ID]) == [1, 2, 3, 4]
        assert (dry["maps"]["no_point"], dry["maps"]["not_served"]) == (1, 1)

        summary = await backup.backfill_details(apply=True)
        assert (summary["maps"]["no_point"], summary["maps"]["not_served"], summary["maps"]["errors"]) == (1, 2, 1)
        assert len(client.calls) == 2
        listed = await real_adapter.get_payload_backfill_rows(account_id=1)
        assert [row["message_id"] for row in listed[CHAT_ID]] == [3]
        for mid in (1, 2, 4):
            row = await _media_row(real_adapter, mid)
            assert (row.skip_reason, row.downloaded, row.file_path, row.file_size) == ("map_not_served", 0, None, 0)
        assert (await _media_row(real_adapter, 3)).skip_reason is None

        again = await backup.backfill_details(apply=True)
        assert client.read_calls[-1] == [3]
        assert again["maps"]["saved"] == 1

    async def test_a_long_flood_wait_stops_the_run(self, real_adapter, tmp_path):
        root = _media_root(tmp_path)
        await _seed_location(real_adapter, 1, "geo")
        await _seed_location(real_adapter, 2, "geo")
        client = BackfillTelegram(
            {1: _media("geo"), 2: _media("geo", _geo(lat=1.5))}, answers=[FloodWaitError(request=None, capture=900)]
        )

        summary = await _backup(real_adapter, client, root).backfill_details(apply=True)

        assert summary["flood_wait_seconds"] == 900
        assert len(client.calls) == 1
        assert summary["maps"]["saved"] == 0

    async def test_a_skipped_chat_and_a_missing_media_folder_fetch_nothing(self, real_adapter, tmp_path):
        root = _media_root(tmp_path)
        await _seed_location(real_adapter, 1, "geo")
        client = BackfillTelegram({1: _media("geo")})
        backup = _backup(real_adapter, client, root)
        backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        skipped = await backup.backfill_details(apply=True)
        assert client.read_calls == [] and client.calls == []
        # A chat the settings skip is never fetched, so nothing waits for a later run.
        assert skipped["maps"]["deferred"] == 0

        empty = tmp_path / "empty"
        empty.mkdir()
        missing = await _backup(real_adapter, client, empty).backfill_details(apply=True)
        assert client.read_calls == [] and client.calls == []
        # Left for a run where the media folder is there: never reported as done.
        assert missing["maps"]["deferred"] == 1

    async def test_the_work_list_flags_rows_that_need_a_picture(self, real_adapter):
        await _seed_location(real_adapter, 1, "geo")
        await _seed_location(real_adapter, 2, "geo_live", file_path="/m/map_0123456789abcdef.jpg")
        await _seed_message(real_adapter, 3, {"venue": {"title": "Demo Cafe"}})
        await real_adapter.insert_media(
            {"id": "v3", "type": "venue", "message_id": 3, "chat_id": CHAT_ID, "downloaded": False}, account_id=1
        )
        await _seed_location(real_adapter, 4, "geo")
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_4_geo",
                "type": "geo",
                "message_id": 4,
                "chat_id": CHAT_ID,
                "skip_reason": "map_not_served",
            },
            account_id=1,
        )
        groups = await real_adapter.get_payload_backfill_rows(account_id=1)
        assert [(r["message_id"], r["needs_map"], r["file_path"]) for r in groups[CHAT_ID]] == [(1, True, None)]


# ---------------------------------------------------------------------------
# The viewer
# ---------------------------------------------------------------------------


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    for key, value in {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module._media_root = tmp_path.resolve()
    return module


async def _viewer_seed(adapter, media_root):
    folder = media_root / str(CHAT_ID)
    folder.mkdir()
    (folder / "map_0123456789abcdef.png").write_bytes(PNG)
    await _seed_location(
        adapter, 21, "geo", file_path=f"{CHAT_ID}/map_0123456789abcdef.png", file_name="map_0123456789abcdef.png"
    )
    # An old row: a placeholder path a release before 9.0 left, downloaded=1.
    await _seed_location(adapter, 22, "geo", file_path=f"{CHAT_ID}/22.bin")
    (folder / "22.bin").write_bytes(b"")
    await adapter.upsert_chat({"id": CHAT_ID - 1, "type": "group", "title": "other chat"}, account_id=1)
    async with adapter.db_manager.async_session_factory() as session:
        refs = dict((await session.execute(select(Chat.id, Chat.ref).where(Chat.account_id == 1))).all())
    return refs[CHAT_ID], refs[CHAT_ID - 1]


async def _page(main_mod, chat, user):
    return {
        row["id"]: row
        for row in await main_mod.get_messages(
            chat=chat,
            user=user,
            limit=50,
            offset=0,
            search=None,
            before_date=None,
            before_id=None,
            after_id=None,
            topic_id=None,
            deleted_only=False,
            edited_only=False,
        )
    }


class TestViewer:
    async def test_a_picture_gets_a_url_and_an_old_bin_row_none(self, main_mod, real_adapter, tmp_path):
        ref, _other = await _viewer_seed(real_adapter, tmp_path)
        main_mod.db = real_adapter
        chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=ref, type="group")

        page = await _page(main_mod, chat, main_mod.UserContext(username="viewer", role="viewer"))
        assert page[21]["media"]["url"] == f"/media/{ref}/21_geo"
        assert page[22]["media"]["url"] is None

        hidden = await _page(main_mod, chat, main_mod.UserContext(username="viewer", role="viewer", no_download=True))
        assert hidden[21]["media"]["url"] is None and hidden[21]["media"]["file_path"] is None

    async def test_the_media_route_serves_it_under_the_chats_own_rules(self, main_mod, real_adapter, tmp_path):
        ref, other_ref = await _viewer_seed(real_adapter, tmp_path)
        main_mod.db = real_adapter
        reader = main_mod.UserContext(username="viewer", role="viewer")

        chat = await main_mod._resolve_chat_ref(ref, reader)
        response = await main_mod.serve_media("21_geo", download=0, chat=chat, user=reader)
        assert os.path.samefile(response.path, tmp_path / str(CHAT_ID) / "map_0123456789abcdef.png")
        assert response.media_type == "image/png"

        no_download = main_mod.UserContext(username="viewer", role="viewer", no_download=True)
        with pytest.raises(main_mod.HTTPException) as exc:
            await main_mod.serve_media("21_geo", download=0, chat=chat, user=no_download)
        assert exc.value.status_code == 403

        # A share token and a restricted login without this chat never reach the row.
        for user in (
            main_mod.UserContext(username="token:1", role="token", allowed_chat_refs={other_ref}),
            main_mod.UserContext(username="viewer", role="viewer", allowed_chat_refs={other_ref}),
        ):
            with pytest.raises(main_mod.HTTPException) as exc:
                await main_mod._resolve_chat_ref(ref, user)
            assert exc.value.status_code == 404
