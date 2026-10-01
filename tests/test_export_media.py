"""Both chat exports list media, and each version is complete (9.0).

Every message carries ``media``: its current media rows, with the id its
transcripts name and what a reader needs to tell it apart, never a file path.
Every entry of ``versions`` carries ``source``, ``entities``,
``rich_message`` and ``media``: the earlier media (``media_versions``) that
version was shown with. So every transcript in a file names a media listed in
the same file. The flat top-level ``message_versions`` list is gone. Runs on
SQLite and PostgreSQL (``real_adapter``).
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession

from telegram_archive.db.models import Media, MediaVersion, MessageVersion
from telegram_archive.export_backup import BackupExporter

CHAT = -1001900000001
SENT = datetime(2026, 10, 1, 10, 38, 0)
EDITED = datetime(2026, 10, 1, 10, 40, 0)
OLD_FILE_ID = "700000000000000111"
NEW_FILE_ID = "700000000000000222"
MEDIA_FIELDS = ["media_id", "type", "file_name", "file_size", "mime_type", "width", "height", "duration"]
VERSION_FIELDS = ["text", "date", "captured_at", "source", "entities", "rich_message", "media"]


async def _message(adapter, message_id: int, *, text: str = "", when: datetime = SENT) -> None:
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Group"}, account_id=1)
    await adapter.insert_message(
        {"id": message_id, "chat_id": CHAT, "text": text, "date": when, "sender_name": "Fixture Sender"},
        account_id=1,
    )


async def _transcribe(adapter, media_id: str, text: str) -> None:
    row = await adapter.enqueue_media_transcript(media_id, account_id=1, preset="auto", force=True)
    await adapter.fill_media_transcript(row["id"], status="done", text=text, language="en")


async def _voice_replaced_on_edit(adapter) -> tuple[str, str]:
    """Message 1270: a voice note whose audio an edit replaced, with a transcript of each audio.

    Returns the earlier media's id and the current one's.
    """
    await _message(adapter, 1270)
    old_id = f"{CHAT}_1270_voice"
    await adapter.insert_media(
        {
            "id": old_id,
            "message_id": 1270,
            "chat_id": CHAT,
            "type": "voice",
            "file_path": "fixture/1270_first.ogg",
            "file_name": "1270_first.ogg",
            "file_size": 7000,
            "mime_type": "audio/ogg",
            "duration": 7,
            "downloaded": True,
            "telegram_file_id": OLD_FILE_ID,
        },
        account_id=1,
    )
    await _transcribe(adapter, old_id, "Meet at eight at the south lot.")
    replaced = await adapter.reconcile_media_row(
        CHAT, 1270, "voice", account_id=1, telegram_file_id=NEW_FILE_ID, edit_date=EDITED
    )
    assert replaced["replaced"] is True
    new_id = replaced["id"]
    await adapter.insert_media(
        {
            "id": new_id,
            "message_id": 1270,
            "chat_id": CHAT,
            "type": "voice",
            "file_path": "fixture/1270_second.ogg",
            "file_name": "1270_second.ogg",
            "file_size": 9000,
            "mime_type": "audio/ogg",
            "duration": 9,
            "downloaded": True,
            "telegram_file_id": NEW_FILE_ID,
        },
        account_id=1,
    )
    await _transcribe(adapter, new_id, "Meet at seven thirty at the north lot.")
    return old_id, new_id


def _voice(media_id: str, name: str, size: int, duration: int) -> dict:
    return {
        "media_id": media_id,
        "type": "voice",
        "file_name": name,
        "file_size": size,
        "mime_type": "audio/ogg",
        "width": None,
        "height": None,
        "duration": duration,
    }


def _listed_media_ids(message: dict) -> set[str]:
    ids = {media["media_id"] for media in message["media"]}
    for version in message["versions"]:
        ids |= {media["media_id"] for media in version["media"]}
    return ids


async def _cli_export(adapter, tmp_path) -> dict:
    output = tmp_path / "export.json"
    await BackupExporter(adapter).export_to_json(str(output), chat_id=CHAT)
    return json.loads(output.read_text(encoding="utf-8"))


async def _store(adapter, *rows) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        session.add_all(rows)
        await session.commit()


def _media_version(message_id: int, number: int, when: datetime, **extra) -> MediaVersion:
    return MediaVersion(
        account_id=1,
        chat_id=CHAT,
        message_id=message_id,
        media_id=f"{CHAT}_{message_id}_photo_v{number}",
        type="photo",
        file_path=f"fixture/{message_id}_v{number}.jpg",
        file_name=f"{message_id}_v{number}.jpg",
        file_size=4000 + number,
        mime_type="image/jpeg",
        width=320,
        height=240,
        downloaded=1,
        date=when,
        captured_at=when + timedelta(minutes=1),
        source="listener",
        **extra,
    )


def _text_version(message_id: int, text: str, when: datetime, **extra) -> MessageVersion:
    return MessageVersion(
        account_id=1,
        chat_id=CHAT,
        message_id=message_id,
        text=text,
        date=when,
        captured_at=when + timedelta(minutes=1),
        change_hash=f"fixture-{message_id}-{text}",
        **extra,
    )


class TestViewerExport:
    async def test_lists_the_current_and_the_earlier_media_every_transcript_names(self, real_adapter):
        old_id, new_id = await _voice_replaced_on_edit(real_adapter)
        (message,) = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]

        assert message["media"] == [_voice(new_id, "1270_second.ogg", 9000, 9)]
        (version,) = message["versions"]
        assert list(version) == VERSION_FIELDS
        assert version["media"] == [_voice(old_id, "1270_first.ogg", 7000, 7)]
        assert version["date"] == SENT.isoformat()
        assert {row["media_id"] for row in message["transcripts"]} == {old_id, new_id}
        assert {row["media_id"] for row in message["transcripts"]} <= _listed_media_ids(message)
        # Where the files lie on disk never leaves the archive.
        assert "fixture/" not in json.dumps(message)

    async def test_only_the_restore_scripts_flag_reads_the_file_path(self, real_adapter):
        """``scripts/restore_chat.py`` uploads the files again; the export route never asks for the path."""
        await _voice_replaced_on_edit(real_adapter)
        await _message(real_adapter, 1271, text="plain", when=SENT + timedelta(minutes=5))
        plain = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert all("media_path" not in m and "media_type" not in m for m in plain)
        restore = [m async for m in real_adapter.get_messages_for_export(CHAT, include_media=True, account_id=1)]
        assert [(m["id"], m["media_type"], m["media_path"]) for m in restore] == [
            (1270, "voice", "fixture/1270_second.ogg"),
            (1271, None, None),
        ]

    async def test_a_message_without_media_lists_none(self, real_adapter):
        await _message(real_adapter, 1, text="plain")
        (message,) = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert (message["media"], message["versions"]) == ([], [])

    async def test_a_message_with_two_media_rows_lists_both_once(self, real_adapter):
        """Downloaded first, then the lowest id: the first entry is the one the viewer shows."""
        await _message(real_adapter, 1)
        for media_id, downloaded in (("fixture-a", False), ("fixture-b", True), ("fixture-c", True)):
            await real_adapter.insert_media(
                {"id": media_id, "message_id": 1, "chat_id": CHAT, "type": "photo", "downloaded": downloaded},
                account_id=1,
            )
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert [(m["id"], [media["media_id"] for media in m["media"]]) for m in exported] == [
            (1, ["fixture-b", "fixture-c", "fixture-a"])
        ]

    async def test_every_version_carries_its_source_formatting_and_block_tree(self, real_adapter):
        await _message(real_adapter, 1, text="final")
        entities = [{"type": "bold", "offset": 0, "length": 4}]
        rich = {"blocks": [{"type": "heading", "text": "Bold"}]}
        await _store(
            real_adapter,
            _text_version(
                1, "Bold text", SENT, source="listener", entities=json.dumps(entities), rich_message=json.dumps(rich)
            ),
            _text_version(1, "plain draft", SENT + timedelta(minutes=1), source="sync"),
        )
        (message,) = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert [
            (v["text"], v["source"], v["entities"], v["rich_message"], v["media"]) for v in message["versions"]
        ] == [
            ("Bold text", "listener", entities, rich, []),
            ("plain draft", "sync", None, None, []),
        ]

    async def test_earlier_media_with_no_text_version_of_its_date_is_its_own_version(self, real_adapter):
        """No kept media goes unlisted; the pairing is the one the edit history shows."""
        await _message(real_adapter, 1, text="final")
        await _store(
            real_adapter,
            _text_version(1, "caption one", SENT),
            _media_version(1, 1, SENT),
            _media_version(1, 2, SENT + timedelta(minutes=3)),
            _text_version(1, "caption two", SENT + timedelta(minutes=5)),
        )
        (message,) = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        got = [
            (v["text"], v["date"], v.get("media_only", False), [media["media_id"] for media in v["media"]])
            for v in message["versions"]
        ]
        assert got == [
            ("caption one", SENT.isoformat(), False, [f"{CHAT}_1_photo_v1"]),
            (None, (SENT + timedelta(minutes=3)).isoformat(), True, [f"{CHAT}_1_photo_v2"]),
            ("caption two", (SENT + timedelta(minutes=5)).isoformat(), False, []),
        ]
        media_only = message["versions"][1]
        assert (media_only["source"], media_only["entities"], media_only["rich_message"]) == ("listener", None, None)
        assert media_only["captured_at"] == (SENT + timedelta(minutes=4)).isoformat()

        # The edit history pairs them the same way (newest first there).
        history = await real_adapter.get_message_versions(CHAT, 1, account_id=1)

        def pairing(versions, date):
            return [(v["text"], date(v), [m["file_name"] for m in v.get("media", [])]) for v in versions]

        assert pairing(reversed(history), lambda v: v["date"].isoformat()) == pairing(
            message["versions"], lambda v: v["date"]
        )

    async def test_of_two_text_versions_with_one_date_the_last_stored_holds_the_media(self, real_adapter):
        await _message(real_adapter, 1, text="final")
        await _store(real_adapter, _text_version(1, "stored first", SENT, id=900001))
        await _store(real_adapter, _text_version(1, "stored second", SENT, id=900002), _media_version(1, 1, SENT))
        (message,) = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert [(v["text"], len(v["media"])) for v in message["versions"]] == [
            ("stored first", 0),
            ("stored second", 1),
        ]
        history = await real_adapter.get_message_versions(CHAT, 1, account_id=1)
        assert [(v["text"], len(v.get("media", []))) for v in history] == [("stored second", 1), ("stored first", 0)]

    async def test_only_the_current_messages_rows_are_read_from_the_database(self, real_adapter):
        """Media and earlier media stream beside the messages: a message with many is never read ahead."""
        for message_id in (1, 2, 3):
            await _message(real_adapter, message_id, when=SENT + timedelta(minutes=message_id))
        await _store(
            real_adapter,
            *(
                Media(account_id=1, id=f"fixture-{message_id}-{n}", chat_id=CHAT, message_id=message_id, type="photo")
                for message_id, count in ((1, 1), (2, 40), (3, 1))
                for n in range(count)
            ),
            *(_media_version(2, n, SENT + timedelta(hours=1, minutes=n)) for n in range(1, 41)),
        )
        original = AsyncResult.__anext__
        fetched: list = []

        async def counting(result):
            row = await original(result)
            fetched.append(row)
            return row

        with patch.object(AsyncResult, "__anext__", counting):
            stream = real_adapter.get_messages_for_export(CHAT, account_id=1)
            exported = [await anext(stream)]
            # Message 1 and its media, plus at most one row ahead in each statement.
            assert len(fetched) <= 6
            exported += [m async for m in stream]
        assert len(fetched) > 80
        assert [(m["id"], len(m["media"]), len(m["versions"])) for m in exported] == [(1, 1, 0), (2, 40, 40), (3, 1, 0)]

    @pytest.mark.parametrize(
        ("query", "table_id"),
        [
            ("_media_of_messages_query", Media.id),
            ("_versions_of_messages_query", MessageVersion.id),
            ("_media_versions_of_messages_query", MediaVersion.id),
        ],
    )
    async def test_rows_out_of_step_with_the_messages_fail_the_export(self, real_adapter, query, table_id):
        """Message 5 was sent before message 4 but has the higher ids: sorting by id walks them apart."""
        for message_id, sent in ((4, SENT + timedelta(hours=2)), (5, SENT + timedelta(hours=1))):
            await _message(real_adapter, message_id, when=sent)
        for message_id in (4, 5):
            await real_adapter.insert_media(
                {"id": f"{CHAT}_{message_id}_photo", "message_id": message_id, "chat_id": CHAT, "type": "photo"},
                account_id=1,
            )
            await _store(
                real_adapter,
                _text_version(message_id, f"draft {message_id}", SENT + timedelta(hours=3)),
                _media_version(message_id, 1, SENT + timedelta(hours=3)),
            )
        original = getattr(real_adapter, query)

        def by_table_id(conditions):
            return original(conditions).order_by(None).order_by(table_id.asc())

        with patch.object(real_adapter, query, by_table_id), pytest.raises(RuntimeError, match="out of step"):
            _ = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]


class TestCliExport:
    async def test_lists_the_current_and_the_earlier_media_every_transcript_names(self, real_adapter, tmp_path):
        old_id, new_id = await _voice_replaced_on_edit(real_adapter)
        data = await _cli_export(real_adapter, tmp_path)

        assert "message_versions" not in data
        (message,) = data["messages"]
        assert message["media"] == [_voice(new_id, "1270_second.ogg", 9000, 9)]
        (version,) = message["versions"]
        assert list(version) == VERSION_FIELDS
        assert version["media"] == [_voice(old_id, "1270_first.ogg", 7000, 7)]
        # Dates keep the file's own format, like the message's.
        assert version["date"] == str(SENT)
        assert {row["media_id"] for row in message["transcripts"]} == {old_id, new_id}
        assert {row["media_id"] for row in message["transcripts"]} <= _listed_media_ids(message)
        assert "fixture/" not in json.dumps(data)
        assert data["statistics"]["total_message_versions"] == 1
        assert data["statistics"]["total_transcripts"] == 2

    async def test_every_version_carries_its_source_formatting_and_media_only_entries(self, real_adapter, tmp_path):
        await _message(real_adapter, 1, text="final")
        entities = [{"type": "italic", "offset": 0, "length": 5}]
        await _store(
            real_adapter,
            _text_version(1, "draft", SENT, source="backup", entities=json.dumps(entities)),
            _media_version(1, 1, SENT + timedelta(minutes=2)),
        )
        data = await _cli_export(real_adapter, tmp_path)
        (message,) = data["messages"]
        assert [(v["text"], v["source"], v["entities"], v.get("media_only", False)) for v in message["versions"]] == [
            ("draft", "backup", entities, False),
            (None, "listener", None, True),
        ]
        assert data["statistics"]["total_message_versions"] == 2

    async def test_the_same_message_id_in_two_chats_keeps_its_own_media(self, real_adapter, tmp_path):
        """Unscoped by chat, the walk keys rows by chat too: message 1 of each chat keeps its own media."""
        other = CHAT - 1
        for chat_id in (CHAT, other):
            await real_adapter.upsert_chat({"id": chat_id, "type": "group", "title": "Fixture"}, account_id=1)
            await real_adapter.insert_message(
                {"id": 1, "chat_id": chat_id, "text": "", "date": SENT, "sender_name": "Fixture Sender"},
                account_id=1,
            )
            await real_adapter.insert_media(
                {"id": f"fixture-{chat_id}", "message_id": 1, "chat_id": chat_id, "type": "photo"}, account_id=1
            )
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        assert sorted((m["chat_id"], [x["media_id"] for x in m["media"]]) for m in data["messages"]) == [
            (other, [f"fixture-{other}"]),
            (CHAT, [f"fixture-{CHAT}"]),
        ]


async def _voice_with_audio_twin(adapter) -> tuple[str, str]:
    """Message 1: a voice row and an older ``audio`` twin of the same file, the twin transcribed."""
    await _message(adapter, 1)
    for media_type in ("voice", "audio"):
        await adapter.insert_media(
            {
                "id": f"{CHAT}_1_{media_type}",
                "message_id": 1,
                "chat_id": CHAT,
                "type": media_type,
                "file_path": "fixture/1.ogg",
                "downloaded": True,
            },
            account_id=1,
        )
    await _transcribe(adapter, f"{CHAT}_1_audio", "Meet at eight.")
    return f"{CHAT}_1_voice", f"{CHAT}_1_audio"


def _twin_cleanup_after_first_read(adapter):
    """Run the backup's voice/audio twin cleanup right after the export's first read."""
    original = AsyncSession.execute
    done: list[bool] = []

    async def read_then_clean(session, statement, *args, **kwargs):
        result = await original(session, statement, *args, **kwargs)
        if isinstance(statement, Select) and not done:
            done.append(True)
            assert await adapter.delete_voice_note_audio_twins(account_id=1) == 1
        return result

    return patch.object(AsyncSession, "execute", read_then_clean)


class TestOneSnapshot:
    """Media and transcripts come from one snapshot: a media row removed during the export leaves no dangling transcript."""

    async def test_viewer_export(self, real_adapter):
        voice, audio = await _voice_with_audio_twin(real_adapter)
        with _twin_cleanup_after_first_read(real_adapter):
            (message,) = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert [media["media_id"] for media in message["media"]] == [audio, voice]
        assert [row["media_id"] for row in message["transcripts"]] == [audio]

    async def test_cli_export(self, real_adapter, tmp_path):
        voice, audio = await _voice_with_audio_twin(real_adapter)
        with _twin_cleanup_after_first_read(real_adapter):
            data = await _cli_export(real_adapter, tmp_path)
        (message,) = data["messages"]
        assert [media["media_id"] for media in message["media"]] == [audio, voice]
        assert [row["media_id"] for row in message["transcripts"]] == [audio]
