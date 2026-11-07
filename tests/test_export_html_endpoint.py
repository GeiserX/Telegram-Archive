"""The viewer's chat export as HTML pages in a ZIP, end to end (real adapter, both databases).

``GET /api/chats/{ref}/export?format=html`` streams one ZIP in Telegram
Desktop's layout. Media is copied only for the kinds asked for, under a
per-file limit, and only through the media root's containment check: no
traversal path, no link out of the root and no symlink entry reaches the
ZIP. The JSON export is untouched, byte for byte (a golden file recorded
from the code before the HTML export existed). Runs on SQLite and
PostgreSQL (``real_adapter``).
"""

import asyncio
import io
import json
import os
import re
import shutil
import stat
import subprocess
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("bs4")

from httpx import ASGITransport, AsyncClient

from telegram_archive.db.adapter import DatabaseAdapter
from telegram_archive.db.base import DatabaseManager
from telegram_archive.db.models import (
    Media,
    Message,
    MessageVersion,
    Reaction,
    ReactionHistory,
)
from telegram_archive.telegram_import import TelegramImporter
from telegram_archive.web import export_html
from telegram_archive.web import main as web_main

CHAT = -1009900000001
OTHER_CHAT = -1009900000002
T0 = datetime(2026, 3, 1, 9, 0, 0)
STORED = datetime(2026, 3, 2, 0, 0, 0)
GOLDEN = Path(__file__).parent / "fixtures" / "export_json_golden.json"
TOP = "ChatExport_Fixture Export Chat"

# message id -> (media type, stored file name, bytes or None for no file, mime)
FILES = {
    2: ("photo", "900002_holiday.jpg", b"\xff\xd8\xff fixture photo", "image/jpeg"),
    3: ("video", "900003_clip.mp4", b"fixture video bytes", "video/mp4"),
    4: ("voice", "900004_voice.ogg", b"OggS fixture voice", "audio/ogg"),
    5: ("video_note", "900005_round.mp4", b"fixture round video", "video/mp4"),
    6: ("animation", "900006_loop.mp4", b"fixture gif video", "video/mp4"),
    7: ("sticker", "900007_sticker.webp", b"RIFF fixture sticker", "image/webp"),
    8: ("document", "900008_report.pdf", b"%PDF fixture report", "application/pdf"),
    9: ("audio", "900009_song.mp3", b"ID3 fixture song", "audio/mpeg"),
}
BIG, NOT_DOWNLOADED, MISSING, BROKEN_SHARED, TRAVERSAL, ABSOLUTE, LINK_OUT, SHARED_A, SHARED_B = range(11, 20)
REPLY, DELETED, EDITED, GEO, SERVICE, FORWARDED, HOSTILE_NAME = 10, 20, 21, 22, 23, 24, 25
BIG_BYTES = 1024 * 1024 + 4096


def _message(message_id: int, *, chat_id: int = CHAT, **extra) -> Message:
    values = {
        "account_id": 1,
        "id": message_id,
        "chat_id": chat_id,
        "date": T0 + timedelta(minutes=message_id),
        "text": f"fixture text {message_id}",
        "sender_name": "Fixture Sender",
        "created_at": STORED,
        "raw_data": json.dumps({"entities": []}),
    }
    values.update(extra)
    return Message(**values)


def _media(message_id: int, media_type: str, name: str, *, path: str | None = None, **extra) -> Media:
    values = {
        "account_id": 1,
        "id": f"{CHAT}_{message_id}_{media_type}",
        "message_id": message_id,
        "chat_id": CHAT,
        "type": media_type,
        "file_name": name,
        "file_path": path if path is not None else f"{CHAT}/{name}",
        "file_size": 100,
        "mime_type": None,
        "downloaded": 1,
        "created_at": STORED,
    }
    values.update(extra)
    return Media(**values)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def _seed(adapter: DatabaseAdapter, root: Path) -> None:
    """The fixture chat, and its files under ``root`` (``root/media`` is the media root)."""
    media_root = root / "media"
    folder = media_root / str(CHAT)
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Export Chat"}, account_id=1)
    await adapter.upsert_chat({"id": OTHER_CHAT, "type": "group", "title": "Fixture Other Chat"}, account_id=1)
    rows: list = [
        _message(
            1,
            text="Hello world https://example.org",
            raw_data=json.dumps({"entities": [{"type": "bold", "offset": 6, "length": 5}]}),
        ),
        _message(1, chat_id=OTHER_CHAT, text="other chat text"),
    ]
    media: list = []
    for message_id, (media_type, name, data, mime) in FILES.items():
        rows.append(_message(message_id, text="" if message_id != 3 else "a caption"))
        _write(folder / name, data)
        media.append(_media(message_id, media_type, name, mime_type=mime, file_size=len(data), duration=75))
    rows.append(_message(REPLY, text="a reply", reply_to_msg_id=1, reply_to_text="Hello"))
    for message_id in (BIG, NOT_DOWNLOADED, MISSING, BROKEN_SHARED, TRAVERSAL, ABSOLUTE, LINK_OUT, SHARED_A, SHARED_B):
        rows.append(_message(message_id, text=""))
    _write(folder / "900011_big.bin", b"\0" * BIG_BYTES)
    media.append(_media(BIG, "document", "900011_big.bin", file_size=BIG_BYTES))
    media.append(_media(NOT_DOWNLOADED, "photo", "900012_pending.jpg", downloaded=0, file_path=None))
    media.append(_media(MISSING, "photo", "900013_gone.jpg"))
    (media_root / "_shared").mkdir(parents=True, exist_ok=True)
    os.symlink("../_shared/no_such_blob.jpg", folder / "900014_broken.jpg")
    media.append(_media(BROKEN_SHARED, "photo", "900014_broken.jpg"))
    _write(root / "outside" / "secret.txt", b"outside the media root")
    _write(root / "secret.txt", b"outside the media root")
    media.append(_media(TRAVERSAL, "document", "secret.txt", path="../../secret.txt"))
    media.append(_media(ABSOLUTE, "document", "secret.txt", path=str((root / "outside" / "secret.txt").resolve())))
    os.symlink(str((root / "outside" / "secret.txt").resolve()), folder / "900017_link.txt")
    media.append(_media(LINK_OUT, "document", "900017_link.txt"))
    _write(media_root / "_shared" / "blob.jpg", b"\xff\xd8\xff one shared blob")
    for message_id in (SHARED_A, SHARED_B):
        os.symlink("../_shared/blob.jpg", folder / f"9000{message_id}_shared.jpg")
        media.append(_media(message_id, "photo", f"9000{message_id}_shared.jpg"))
    # A real file in the root whose stored name tries to climb out: only our name reaches the ZIP.
    rows.append(_message(HOSTILE_NAME, text=""))
    _write(folder / "900025_evil.jpg", b"\xff\xd8\xff hostile name")
    media.append(_media(HOSTILE_NAME, "photo", "../../evil.jpg", path=f"{CHAT}/900025_evil.jpg"))
    rows += [
        _message(DELETED, text="a deleted message", is_deleted=1, deleted_at=T0 + timedelta(days=1)),
        _message(EDITED, text="the edited text", edit_date=T0 + timedelta(hours=1), edit_hide=0),
        MessageVersion(
            account_id=1,
            chat_id=CHAT,
            message_id=EDITED,
            text="the first text",
            date=T0 + timedelta(minutes=EDITED),
            change_hash="0" * 64,
            captured_at=T0 + timedelta(hours=1),
            source="listener",
        ),
        _message(GEO, text="", raw_data=json.dumps({"entities": [], "geo": {"lat": 40.4168, "long": -3.7038}})),
        _message(
            SERVICE,
            text="Fixture Sender changed the group name",
            raw_data=json.dumps({"service_type": "service", "action_type": "chat_edit_title"}),
        ),
        _message(
            FORWARDED,
            text="forwarded text",
            raw_data=json.dumps({"entities": [], "forward_from_name": "Account B"}),
            is_pinned=1,
        ),
        Reaction(account_id=1, chat_id=CHAT, message_id=1, emoji="👍", count=2, created_at=STORED),
        # A later message whose emoji sorts first: the walk must follow the messages, not the emoji.
        Reaction(account_id=1, chat_id=CHAT, message_id=EDITED, emoji="🎉", count=3, created_at=STORED),
        Reaction(account_id=1, chat_id=CHAT, message_id=1, emoji="🔥", count=1, created_at=STORED, removed_at=STORED),
        # The history says 5: the viewer's chips read the reactions table, and so must the export.
        ReactionHistory(
            account_id=1, chat_id=CHAT, message_id=1, emoji="👍", count=5, observed_at=STORED, source="baseline"
        ),
    ]
    async with adapter.db_manager.async_session_factory() as session:
        session.add_all(rows)
        await session.flush()
        session.add_all(media)
        await session.commit()


@pytest.fixture
async def viewer(real_adapter, tmp_path):
    """The real app on the seeded archive with its media root, auth on, one session per grant."""
    await _seed(real_adapter, tmp_path)
    refs = {
        chat_id: (await real_adapter.get_chat_by_id(chat_id, account_id=1))["ref"] for chat_id in (CHAT, OTHER_CHAT)
    }
    saved = (
        web_main.db,
        web_main.AUTH_ENABLED,
        web_main.ALLOW_ANONYMOUS_VIEWER,
        dict(web_main._sessions),
        web_main._media_root,
        web_main._thumb_cache_dir,
    )
    web_main.db = real_adapter
    web_main.AUTH_ENABLED = True
    web_main.ALLOW_ANONYMOUS_VIEWER = False
    web_main._media_root = (tmp_path / "media").resolve()
    web_main._thumb_cache_dir = tmp_path / "thumbs"
    web_main._sessions.clear()
    for token, grant, no_download in (
        ("html-all", None, False),
        ("html-other", {refs[OTHER_CHAT]}, False),
        ("html-nodl", None, True),
    ):
        web_main._sessions[token] = web_main.SessionData(
            username=token, role="viewer", allowed_chat_refs=grant, no_download=no_download, created_at=time.time()
        )
    try:
        yield refs
    finally:
        (
            web_main.db,
            web_main.AUTH_ENABLED,
            web_main.ALLOW_ANONYMOUS_VIEWER,
            sessions,
            web_main._media_root,
            web_main._thumb_cache_dir,
        ) = saved
        web_main._sessions.clear()
        web_main._sessions.update(sessions)


def _client(token: str = "html-all") -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")
    client.cookies.set("viewer_auth", token)
    return client


async def _export(ref: str, **params) -> zipfile.ZipFile:
    async with _client() as client:
        resp = await client.get(f"/api/chats/{ref}/export", params={"format": "html", **params})
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _page(archive: zipfile.ZipFile, name: str = "messages.html") -> str:
    return archive.read(f"{TOP}/{name}").decode("utf-8")


def _message_html(archive: zipfile.ZipFile, message_id: int) -> str:
    """The one line of ``messages.html`` that draws ``message_id`` (each message is one line)."""
    marker = f'id="message{message_id}"'
    lines = [line for line in _page(archive).splitlines() if line.startswith('<div class="message') and marker in line]
    assert len(lines) == 1, message_id
    return lines[0]


ALL_MEDIA = "photo,video,voice,video_note,gif,sticker,file"


class TestTheZip:
    async def test_text_only_without_a_media_parameter(self, viewer):
        archive = await _export(viewer[CHAT])
        names = set(archive.namelist())
        assert {f"{TOP}/index.html", f"{TOP}/messages.html", f"{TOP}/css/style.css"} <= names
        assert not [name for name in names if name.count("/") > 1 and "/css/" not in name]
        assert archive.testzip() is None
        assert "Hello <strong>world</strong>" in _page(archive)
        assert "Not included in this export" in _message_html(archive, 2)

    async def test_each_media_kind_is_copied_byte_for_byte_and_linked(self, viewer):
        archive = await _export(viewer[CHAT], media=ALL_MEDIA)
        folders = {
            "photo": "photos",
            "video": "video_files",
            "voice": "voice_messages",
            "video_note": "round_video_messages",
            "animation": "animations",
            "sticker": "stickers",
            "document": "files",
            "audio": "files",
        }
        for message_id, (media_type, name, data, _mime) in FILES.items():
            entry = f"{folders[media_type]}/{message_id}_1{os.path.splitext(name)[1]}"
            assert archive.read(f"{TOP}/{entry}") == data, entry
            assert f'href="{entry}"' in _message_html(archive, message_id), entry
        assert '<video controls preload="none" src="video_files/3_1.mp4">' in _message_html(archive, 3)
        assert '<audio controls preload="none" src="voice_messages/4_1.ogg">' in _message_html(archive, 4)
        assert 'download="report.pdf"' in _message_html(archive, 8)

    async def test_a_file_over_the_limit_is_left_out(self, viewer):
        archive = await _export(viewer[CHAT], media=ALL_MEDIA, max_file_mb="1")
        assert not [name for name in archive.namelist() if "/11_" in name]
        card = _message_html(archive, BIG)
        assert "Larger than the 1 MB limit of this export" in card and "href" not in card
        assert f"{TOP}/files/8_1.pdf" in archive.namelist()

    async def test_a_type_not_selected_is_left_out(self, viewer):
        archive = await _export(viewer[CHAT], media="video")
        assert f"{TOP}/video_files/3_1.mp4" in archive.namelist()
        assert not [name for name in archive.namelist() if "/photos/" in name]
        card = _message_html(archive, 2)
        assert "Not included in this export" in card and "href" not in card

    async def test_pending_missing_and_broken_files_keep_their_card_and_are_counted(self, viewer):
        archive = await _export(viewer[CHAT], media=ALL_MEDIA)
        assert "Not downloaded to the archive" in _message_html(archive, NOT_DOWNLOADED)
        for message_id in (MISSING, BROKEN_SHARED):
            card = _message_html(archive, message_id)
            assert "Missing from the archive" in card and "href" not in card
        index = _page(archive, "index.html")
        assert "Not downloaded to the archive: 1</li>" in index
        # Missing: the gone file, the broken _shared link, the traversal, the absolute path and the link out.
        assert "Missing from the archive&#x27;s storage: 5</li>" in index

    async def test_nothing_outside_the_media_root_and_no_symlink_reaches_the_zip(self, viewer):
        archive = await _export(viewer[CHAT], media=ALL_MEDIA)
        for message_id in (TRAVERSAL, ABSOLUTE, LINK_OUT):
            assert not [name for name in archive.namelist() if f"/{message_id}_" in name]
            assert "Missing from the archive" in _message_html(archive, message_id)
        assert archive.read(f"{TOP}/photos/{HOSTILE_NAME}_1.jpg") == b"\xff\xd8\xff hostile name"
        for info in archive.infolist():
            assert not stat.S_ISLNK(info.external_attr >> 16), info.filename
            assert info.filename.startswith(f"{TOP}/") and ".." not in info.filename.split("/")
            assert b"outside the media root" not in archive.read(info)

    async def test_a_shared_blob_is_written_once(self, viewer):
        archive = await _export(viewer[CHAT], media="photo")
        shared = [name for name in archive.namelist() if f"/{SHARED_A}_" in name or f"/{SHARED_B}_" in name]
        assert shared == [f"{TOP}/photos/{SHARED_A}_1.jpg"]
        assert f'href="photos/{SHARED_A}_1.jpg"' in _message_html(archive, SHARED_B)

    async def test_filters_are_named_in_the_index(self, viewer):
        archive = await _export(viewer[CHAT], **{"from": "2026-03-01T09:05:00", "to": "2026-03-01"})
        index = _page(archive, "index.html")
        assert "Filtered export, not the full history." in index
        assert "from: 2026-03-01T09:05:00" in index
        assert 'id="message1"' not in _page(archive) and 'id="message5"' in _page(archive)

    async def test_changed_since_keeps_the_changed_messages_and_says_so(self, viewer):
        # Only DELETED changed after this instant: the archive saw it deleted the next day.
        archive = await _export(viewer[CHAT], changed_since="2026-03-02T01:00:00")
        index = _page(archive, "index.html")
        assert "Filtered export, not the full history." in index and "changed_since: 2026-03-02T01:00:00" in index
        ids = [int(part.split('"')[0]) for part in _page(archive).split('id="message')[1:]]
        assert ids == [DELETED]

    async def test_reactions_are_the_ones_the_viewer_shows(self, viewer):
        archive = await _export(viewer[CHAT])
        html = _message_html(archive, 1)
        async with _client() as client:
            resp = await client.get(f"/api/chats/{viewer[CHAT]}/messages")
        data = resp.json()
        listed = data["messages"] if isinstance(data, dict) else data
        shown = next(m for m in listed if m["id"] == 1)["reactions"]
        assert [(r["emoji"], r["count"]) for r in shown] == [("👍", 2)]
        assert '<span class="reaction">👍 2</span>' in html and "🔥" not in html and "🎉" not in html
        edited = _message_html(archive, EDITED)
        assert '<span class="reaction">🎉 3</span>' in edited and "👍" not in edited

    async def test_a_cached_thumbnail_becomes_the_poster(self, viewer, tmp_path):
        _write(tmp_path / "thumbs" / "400" / str(CHAT) / "900003_clip.webp", b"RIFF cached poster")
        archive = await _export(viewer[CHAT], media="video")
        assert archive.read(f"{TOP}/video_files/3_1_thumb.webp") == b"RIFF cached poster"
        assert 'poster="video_files/3_1_thumb.webp"' in _message_html(archive, 3)

    @pytest.mark.parametrize(("value", "players"), [("false", False), ("FALSE", False), ("TRUE", True), ("true", True)])
    async def test_the_players_switch(self, viewer, value, players):
        archive = await _export(viewer[CHAT], media=ALL_MEDIA, players=value)
        page = _page(archive)
        assert ("<video" in page) is players and ("<audio" in page) is players
        assert 'href="video_files/3_1.mp4"' in page and 'href="voice_messages/4_1.ogg"' in page

    async def test_replies_deleted_edited_service_and_forwards(self, viewer):
        archive = await _export(viewer[CHAT])
        reply = _message_html(archive, REPLY)
        assert 'href="messages.html#go_to_message1"' in reply
        assert '<span class="reply_quote">Hello</span>' in reply
        assert '<span class="pinned">Pinned</span>' in _message_html(archive, FORWARDED)
        assert "Pinned" not in reply
        assert "Deleted from Telegram on" in _message_html(archive, DELETED)
        edited = _message_html(archive, EDITED)
        assert ">edited</span>" in edited and "Earlier versions (1)" in edited and "the first text" in edited
        assert 'class="message service"' in _message_html(archive, SERVICE)
        assert "Account B" in _message_html(archive, FORWARDED)
        assert "https://www.openstreetmap.org/?mlat=40.416800&amp;mlon=-3.703800" in _message_html(archive, GEO)

    async def test_it_opens_with_unzip(self, viewer, tmp_path):
        unzip = shutil.which("unzip")
        if unzip is None:
            pytest.skip("no unzip on this machine")
        async with _client() as client:
            resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params={"format": "html", "media": ALL_MEDIA})
        target = tmp_path / "export.zip"
        target.write_bytes(resp.content)
        result = subprocess.run([unzip, "-t", str(target)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "No errors detected" in result.stdout


class TestTheRequest:
    async def test_no_download_is_refused_for_both_formats(self, viewer):
        async with _client("html-nodl") as client:
            for params in (
                {},
                {"format": "json"},
                {"format": "html"},
                {"format": "html", "media": "photo"},
                # Refused before the parameters are looked at.
                {"format": "pdf"},
                {"format": "html", "max_file_mb": "0"},
                {"format": "json", "media": "photo"},
            ):
                resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params=params)
                assert resp.status_code == 403, params

    async def test_a_chat_outside_the_grant_is_a_404(self, viewer):
        async with _client("html-other") as client:
            denied = await client.get(f"/api/chats/{viewer[CHAT]}/export", params={"format": "html"})
            allowed = await client.get(f"/api/chats/{viewer[OTHER_CHAT]}/export", params={"format": "html"})
        assert denied.status_code == 404
        assert allowed.status_code == 200
        page = zipfile.ZipFile(io.BytesIO(allowed.content)).read("ChatExport_Fixture Other Chat/messages.html")
        assert b"other chat text" in page and b"fixture text" not in page and b"Hello" not in page

    @pytest.mark.parametrize(
        "params",
        [
            {"format": "pdf"},
            {"format": "html", "media": "foo"},
            {"format": "html", "max_file_mb": "0"},
            {"format": "html", "max_file_mb": "3901"},
            {"format": "html", "max_file_mb": "ten"},
            {"format": "html", "max_file_mb": " 50 "},
            {"format": "html", "max_file_mb": "1_000"},
            {"format": "html", "max_file_mb": "٥٠"},
            {"format": "html", "players": "maybe"},
            {"format": "json", "media": "photo"},
            {"media": "photo"},
            {"max_file_mb": "50"},
            {"format": "json", "players": "true"},
        ],
    )
    async def test_bad_parameters_are_a_400(self, viewer, params):
        async with _client() as client:
            resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params=params)
        assert resp.status_code == 400, resp.text
        if params.get("media") == "foo":
            assert "foo" in resp.json()["detail"]

    async def test_the_database_walk_ends_before_the_zip_starts(self, viewer, monkeypatch):
        """The read snapshot must not stay open while a slow client downloads."""
        events = []
        real_walk = web_main.db.get_messages_for_export
        real_zip = export_html.iter_html_zip

        async def walk(*args, **kwargs):
            try:
                async for msg in real_walk(*args, **kwargs):
                    yield msg
            finally:
                events.append("walk closed")

        async def zip_stream(*args, **kwargs):
            async for piece in real_zip(*args, **kwargs):
                events.append("piece")
                yield piece

        monkeypatch.setattr(web_main.db, "get_messages_for_export", walk)
        monkeypatch.setattr(export_html, "iter_html_zip", zip_stream)
        # Tiny pieces: the first media file would hand one on while the walk still ran.
        monkeypatch.setattr(export_html, "CHUNK", 16)
        await _export(viewer[CHAT], media=ALL_MEDIA)
        assert events[0] == "walk closed" and "piece" in events

    async def test_headers(self, viewer):
        async with _client() as client:
            resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params={"format": "html"})
        assert resp.headers["content-type"] == "application/zip"
        assert resp.headers["content-disposition"] == "attachment; filename*=UTF-8''Fixture%20Export%20Chat_export.zip"
        assert resp.headers["cache-control"] == "private, no-store"
        assert "content-length" not in resp.headers


class TestTheDialog:
    def test_run_export_sends_the_parameters_the_route_reads(self):
        """The dialog's request, as ``runExport`` builds it, names what ``export_chat`` parses."""
        template = (Path(web_main.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        start = template.index("const runExport = async () => {")
        body = template[start : template.index("const resp = await fetch(url", start)]
        for name in ("'format', 'html'", "'media'", "'max_file_mb'", "'players'", "'from'", "'to'"):
            assert f"params.set({name}" in body, name
        assert "exportPlayers.value ? 'true' : 'false'" in body
        assert 'min="1" max="3900"' in template
        assert f"mb <= {export_html.MAX_FILE_MB_LIMIT}" in template
        # Every kind the route knows, in its order, and all but files ticked.
        kinds = re.findall(r"\{ value: '(\w+)', label: '[^']+', on: (true|false) \}", template)
        assert [name for name, _on in kinds] == list(export_html.MEDIA_KINDS)
        assert [name for name, on in kinds if on == "false"] == ["file"]


class TestJsonUnchanged:
    async def test_the_json_export_is_byte_identical_to_before(self, viewer):
        async with _client() as client:
            default = await client.get(f"/api/chats/{viewer[CHAT]}/export")
            explicit = await client.get(f"/api/chats/{viewer[CHAT]}/export", params={"format": "json"})
        assert default.status_code == 200
        assert default.content == explicit.content
        # The chat's ref is random per run; everything else is the fixture's.
        recorded = GOLDEN.read_bytes().replace(b"@REF@", viewer[CHAT].encode())
        assert default.content == recorded

    async def test_without_render_the_adapter_yields_the_same_keys(self, viewer):
        keys = set()
        media_keys = set()
        async for msg in web_main.db.get_messages_for_export(CHAT, account_id=1):
            keys |= set(msg)
            for media in msg["media"]:
                media_keys |= set(media)
        assert keys == {
            "id",
            "date",
            "sender",
            "text",
            "is_outgoing",
            "reply_to",
            "is_deleted",
            "deleted_at",
            "edit_date",
            "edit_hide",
            "media",
            "media_payload",
            "versions",
            "reaction_history",
            "snapshots",
        }
        assert media_keys == {"media_id", "type", "file_name", "file_size", "mime_type", "width", "height", "duration"}


class TestRoundTrip:
    async def test_the_importer_reads_the_export_back(self, viewer, tmp_path):
        archive = await _export(viewer[CHAT], media=ALL_MEDIA)
        target = tmp_path / "unzipped"
        archive.extractall(target)
        manager = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        await manager.init()
        fresh = DatabaseAdapter(manager)
        try:
            importer = TelegramImporter(fresh, str(tmp_path / "fresh_media"), account_id=1)
            await importer.run(str(target / TOP), chat_id_override=CHAT)
            back = {m["id"]: m async for m in fresh.get_messages_for_export(CHAT, account_id=1)}
        finally:
            await manager.close()
        original = {m["id"]: m async for m in web_main.db.get_messages_for_export(CHAT, account_id=1)}
        assert set(back) == set(original)
        for message_id, msg in original.items():
            assert (back[message_id]["text"] or "") == (msg["text"] or ""), message_id
            if message_id != SERVICE:
                assert back[message_id]["reply_to"] == msg["reply_to"], message_id
        copied = {mid: [m["type"] for m in msg["media"]] for mid, msg in original.items() if mid in FILES}
        assert {mid: [m["type"] for m in back[mid]["media"]] for mid in FILES} == copied


class TestMemory:
    async def test_a_large_file_streams_in_bounded_pieces(self, viewer, tmp_path):
        big = tmp_path / "media" / str(CHAT) / FILES[8][1]
        with open(big, "wb") as handle:
            handle.truncate(64 * 1024 * 1024)
        # httpx's ASGI transport joins the body, so read the ASGI messages themselves.
        sizes = []
        body = io.BytesIO()
        query = b"format=html&media=file&max_file_mb=100"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/chats/{viewer[CHAT]}/export",
            "raw_path": f"/api/chats/{viewer[CHAT]}/export".encode(),
            "query_string": query,
            "headers": [(b"host", b"test"), (b"cookie", b"viewer_auth=html-all")],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "root_path": "",
        }
        status = []

        requested = []
        done = asyncio.Event()

        async def receive():
            if not requested:
                requested.append(True)
                return {"type": "http.request", "body": b"", "more_body": False}
            await done.wait()  # the client stays connected until the body is read
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                status.append(message["status"])
            elif message["type"] == "http.response.body" and message.get("body"):
                sizes.append(len(message["body"]))
                body.write(message["body"])

        await web_main.app(scope, receive, send)
        done.set()
        assert status == [200]
        assert len(sizes) > 32
        assert max(sizes) <= 2 * 1024 * 1024
        assert zipfile.ZipFile(body).getinfo(f"{TOP}/files/8_1.pdf").file_size == 64 * 1024 * 1024
