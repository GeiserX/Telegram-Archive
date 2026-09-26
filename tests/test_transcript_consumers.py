"""Transcript consumers (slice 7 of docs/TRANSCRIPTION.md) on real engines.

The four flag-gated delete paths take the transcript rows of the media they
remove and nothing else, the voice/audio twin cleanup keeps them, the
changes feed reports a ``transcript`` kind, both exports carry every row,
the gallery items carry them, and the Voice tab shows and filters by them.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from src.db.adapter import ChatScope
from src.db.models import MediaTranscript
from src.export_backup import BackupExporter

sys.path.insert(0, os.path.dirname(__file__))

from test_transcription_bubble import _html, _run_node, _script  # noqa: E402

CHAT = -420700001
OTHER_CHAT = -420700002
WHEN = datetime(2026, 2, 3, 4, 5, 6)


async def _voice(
    adapter,
    message_id: int,
    text: str | None = "fixture words",
    *,
    chat_id: int = CHAT,
    account_id: int = 1,
    media_type: str = "voice",
    file_path: str | None = None,
    chat_type: str = "group",
) -> str:
    """A chat, a message, one downloaded media row and one finished transcript of it."""
    await adapter.upsert_chat({"id": chat_id, "type": chat_type, "title": "fixture chat"}, account_id=account_id)
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "text": "",
            "date": WHEN,
            "sender_name": "Fixture Sender",
            "raw_data": {},
        },
        account_id=account_id,
    )
    media_id = f"{chat_id}_{message_id}_{media_type}"
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "type": media_type,
            "file_path": file_path or f"fixture/{message_id}.ogg",
            "downloaded": True,
            "duration": 4,
        },
        account_id=account_id,
    )
    row = await adapter.enqueue_media_transcript(media_id, account_id=account_id, preset="auto")
    if text is not None:
        await adapter.fill_media_transcript(row["id"], status="done", text=text, language="es")
    return media_id


async def _transcript_keys(adapter) -> set[tuple[int, str]]:
    async with adapter.db_manager.async_session_factory() as session:
        rows = await session.execute(select(MediaTranscript.account_id, MediaTranscript.media_id))
        return {(row.account_id, row.media_id) for row in rows}


async def _seed_for_deletes(adapter) -> dict[str, str]:
    return {
        "a": await _voice(adapter, 1),
        "b": await _voice(adapter, 2),
        "other_chat": await _voice(adapter, 3, chat_id=OTHER_CHAT),
        "other_account": await _voice(adapter, 1, account_id=2),
    }


class TestDeletePaths:
    async def test_delete_message_takes_the_transcripts_of_its_media_only(self, real_adapter):
        ids = await _seed_for_deletes(real_adapter)
        await real_adapter.delete_message(CHAT, 1, account_id=1)
        assert await _transcript_keys(real_adapter) == {
            (1, ids["b"]),
            (1, ids["other_chat"]),
            (2, ids["other_account"]),
        }

    async def test_delete_media_for_chat_takes_the_chats_transcripts_only(self, real_adapter):
        ids = await _seed_for_deletes(real_adapter)
        assert await real_adapter.delete_media_for_chat(CHAT, account_id=1) == 2
        assert await _transcript_keys(real_adapter) == {(1, ids["other_chat"]), (2, ids["other_account"])}

    async def test_delete_media_records_keeps_the_transcripts_unless_asked(self, real_adapter):
        """The pending-twin cleanup runs on every backup with no flag: the media row goes, its transcript stays."""
        ids = await _seed_for_deletes(real_adapter)
        assert await real_adapter.delete_media_records([ids["a"]], account_id=1) == 1
        assert (1, ids["a"]) in await _transcript_keys(real_adapter)

    async def test_delete_media_records_with_transcripts_takes_the_named_media_transcripts_only(self, real_adapter):
        ids = await _seed_for_deletes(real_adapter)
        assert await real_adapter.delete_media_records([ids["a"]], account_id=1, with_transcripts=True) == 1
        assert await _transcript_keys(real_adapter) == {
            (1, ids["b"]),
            (1, ids["other_chat"]),
            (2, ids["other_account"]),
        }

    async def test_delete_chat_and_related_data_takes_the_chats_transcripts_only(self, real_adapter):
        ids = await _seed_for_deletes(real_adapter)
        await real_adapter.delete_chat_and_related_data(CHAT, account_id=1)
        assert await _transcript_keys(real_adapter) == {(1, ids["other_chat"]), (2, ids["other_account"])}

    async def test_the_twin_cleanup_keeps_the_transcripts(self, real_adapter):
        """The audio twin goes; its transcript row stays in the table."""
        audio = await _voice(real_adapter, 5, media_type="audio", file_path="fixture/5.ogg")
        await real_adapter.insert_media(
            {
                "id": f"{CHAT}_5_voice",
                "message_id": 5,
                "chat_id": CHAT,
                "type": "voice",
                "file_path": "fixture/5.ogg",
                "downloaded": True,
            },
            account_id=1,
        )
        assert await real_adapter.delete_voice_note_audio_twins(account_id=1) == 1
        assert await _transcript_keys(real_adapter) == {(1, audio)}


class TestChangesFeed:
    async def _complete_at(self, adapter, when: datetime) -> None:
        async with adapter.db_manager.async_session_factory() as session:
            await session.execute(update(MediaTranscript).values(completed_at=when))
            await session.commit()

    async def test_a_finished_transcript_is_a_transcript_change(self, real_adapter):
        await _voice(real_adapter, 1, "the ferry leaves at nine")
        await _voice(real_adapter, 2, None)  # queued: not a change yet
        await self._complete_at(real_adapter, WHEN)
        changes = await real_adapter.get_recent_changes()
        assert [(c["kind"], c["message_id"], c["text"], c["language"]) for c in changes] == [
            ("transcript", 1, "the ferry leaves at nine", "es")
        ]
        assert changes[0]["date"] == WHEN.isoformat()
        assert changes[0]["sender_name"] == "Fixture Sender"

    async def test_the_window_and_the_scope_apply(self, real_adapter):
        await _voice(real_adapter, 1)
        await _voice(real_adapter, 2, chat_id=OTHER_CHAT)
        await self._complete_at(real_adapter, WHEN)
        assert await real_adapter.get_recent_changes(since=WHEN + timedelta(seconds=1)) == []
        assert await real_adapter.get_recent_changes(before=WHEN) == []
        scoped = await real_adapter.get_recent_changes(scope=ChatScope.build(ids={OTHER_CHAT}))
        assert [(c["kind"], c["message_id"]) for c in scoped] == [("transcript", 2)]

    async def test_one_row_per_event_when_two_accounts_hold_the_chat(self, real_adapter):
        await _voice(real_adapter, 1, "shared words")
        await _voice(real_adapter, 1, "shared words", account_id=2)
        await self._complete_at(real_adapter, WHEN)
        assert [c["kind"] for c in await real_adapter.get_recent_changes()] == ["transcript"]
        # A private chat never merges: the same ids name two conversations.
        await _voice(real_adapter, 7, "private words", chat_id=555, chat_type="private")
        await _voice(real_adapter, 7, "private words", chat_id=555, account_id=2, chat_type="private")
        await self._complete_at(real_adapter, WHEN)
        assert len(await real_adapter.get_recent_changes()) == 3

    async def test_different_transcripts_of_one_shared_message_are_two_changes(self, real_adapter):
        """The text names the event: two accounts' different results are two events, not one."""
        await _voice(real_adapter, 1, "first account words")
        await _voice(real_adapter, 1, "second account words", account_id=2)
        await self._complete_at(real_adapter, WHEN)
        changes = await real_adapter.get_recent_changes()
        assert sorted((c["kind"], c["text"]) for c in changes) == [
            ("transcript", "first account words"),
            ("transcript", "second account words"),
        ]


class TestExports:
    async def test_the_viewer_export_carries_every_row_on_its_message(self, real_adapter):
        media_id = await _voice(real_adapter, 1, "first take")
        again = await real_adapter.enqueue_media_transcript(media_id, account_id=1, force=True)
        await real_adapter.fill_media_transcript(again["id"], status="done", text="second take", job_id="job_0001")
        await real_adapter.insert_message(
            {"id": 2, "chat_id": CHAT, "text": "plain", "date": WHEN, "raw_data": {}}, account_id=1
        )
        await _voice(real_adapter, 1, "another account", account_id=2)
        exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}
        assert [row["text"] for row in exported[1]["transcripts"]] == ["second take", "first take"]
        assert exported[1]["transcripts"][0]["media_id"] == media_id
        assert isinstance(exported[1]["transcripts"][0]["created_at"], str)
        assert isinstance(exported[1]["transcripts"][0]["job_stored_at"], str)
        assert "transcripts" not in exported[2]
        json.dumps(exported[1])  # serialisable as the route streams it

    async def test_a_windowed_viewer_export_reads_only_the_rows_of_its_messages(self, real_adapter):
        await _voice(real_adapter, 1, "inside the window")
        await _voice(real_adapter, 2, "before the window")
        await _voice(real_adapter, 3, "after the window")
        async with real_adapter.db_manager.async_session_factory() as session:
            from src.db.models import Message

            for message_id, when in ((2, WHEN - timedelta(days=2)), (3, WHEN + timedelta(days=2))):
                await session.execute(update(Message).where(Message.id == message_id).values(date=when))
            await session.commit()
        window = {"from_date": WHEN - timedelta(days=1), "to_date": WHEN + timedelta(days=1)}

        rows = await real_adapter.get_transcripts_for_export(CHAT, account_id=1, **window)
        assert [row["text"] for row in rows] == ["inside the window"]
        # The window's end is exclusive, as it is for the messages.
        edge = await real_adapter.get_transcripts_for_export(CHAT, account_id=1, from_date=WHEN, to_date=WHEN)
        assert edge == []

        read = real_adapter.get_transcripts_for_export
        with patch.object(real_adapter, "get_transcripts_for_export", wraps=read) as spy:
            exported = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1, **window)]
        assert [(m["id"], [r["text"] for r in m["transcripts"]]) for m in exported] == [(1, ["inside the window"])]
        assert spy.await_args.kwargs == {"account_id": 1, **window}

    async def test_the_cli_export_attaches_rows_from_the_database(self, real_adapter, tmp_path):
        await _voice(real_adapter, 1, "cli words")
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(str(output), chat_id=CHAT)
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["statistics"]["total_transcripts"] == 1
        (message,) = data["messages"]
        assert [row["text"] for row in message["transcripts"]] == ["cli words"]

    async def test_the_cli_export_keeps_two_accounts_private_chats_apart(self, real_adapter, tmp_path):
        """Both accounts hold message 7 of a private chat with the same peer: two conversations."""
        await _voice(real_adapter, 7, "account one words", chat_id=555, chat_type="private")
        await _voice(real_adapter, 7, "account two words", chat_id=555, account_id=2, chat_type="private")
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(str(output), chat_id=555)
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["statistics"]["total_transcripts"] == 2
        by_account = {m["account_id"]: [row["text"] for row in m["transcripts"]] for m in data["messages"]}
        assert by_account == {1: ["account one words"], 2: ["account two words"]}


# ============================================================================
# The gallery
# ============================================================================


def test_the_voice_tab_shows_the_first_line_and_filters_by_transcript() -> None:
    _run_node(
        _script(
            """
            const item = (id, file_name, transcripts) => ({ id, file_name, transcripts,
                transcript: transcripts.find(row => row.status === 'done') || null })
            const a = item('1_voice', 'a.ogg', [done(3, { text: '\\n  Harbour at nine  \\nsecond line' })])
            const b = item('2_voice', 'Harbour.ogg', [])
            const c = item('3_voice', 'c.ogg', [{ id: 4, status: 'failed', text: 'harbour' }, done(2, { text: 'older NINE take' })])
            const d = item('4_voice', 'd.ogg', [done(5, { text: 'unrelated' })])
            assert.equal(galleryTranscriptLine(a), 'Harbour at nine')
            assert.equal(galleryTranscriptLine(b), '')
            mediaGalleryItems.value = [a, b, c, d]
            assert.deepEqual(galleryVoiceItems.value.map(i => i.id), ['1_voice', '2_voice', '3_voice', '4_voice'])
            // File name or a finished transcript; a failed row's text is not a transcript.
            mediaGalleryFilter.value = '  harbour '
            assert.deepEqual(galleryVoiceItems.value.map(i => i.id), ['1_voice', '2_voice'])
            // Any finished row counts, not only the newest.
            mediaGalleryFilter.value = 'nine'
            assert.deepEqual(galleryVoiceItems.value.map(i => i.id), ['1_voice', '3_voice'])
            """
        )
    )


def test_the_markup_uses_what_the_setup_returns() -> None:
    html = _html()
    voice_tab = html[html.index("<template v-else-if=\"mediaGalleryTab === 'voice'\">") :]
    voice_tab = voice_tab[: voice_tab.index("</template>")]
    assert 'v-for="item in galleryVoiceItems"' in voice_tab
    assert 'v-model="mediaGalleryFilter"' in voice_tab
    assert "galleryTranscriptLine(item)" in voice_tab
    returned = html[html.rindex("return {") :]
    for name in ("mediaGalleryFilter", "galleryVoiceItems", "galleryTranscriptLine"):
        assert f"                    {name},\n" in returned, name
    # The changes panel renders the transcript kind with its own body, not as an edit.
    assert "v-else-if=\"change.kind === 'transcript'\"" in html


pytest.importorskip("fastapi")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from test_transcription_bubble import viewer  # noqa: E402, F401
from test_web_routes import web_main  # noqa: E402


@pytest.mark.usefixtures("viewer")
class TestRoutes:
    async def test_gallery_items_carry_their_rows_without_the_storage_id(self, real_adapter):
        media_id = await _voice(real_adapter, 1, "gallery words")
        await _voice(real_adapter, 2, None)
        ref = (await real_adapter.get_chat_by_id(CHAT, account_id=1))["ref"]
        async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
            resp = await client.get(f"/api/chats/{ref}/media?types=voice,audio")
        assert resp.status_code == 200, resp.text
        items = {item["id"]: item for item in resp.json()["items"]}
        assert items["1_voice"]["transcript"]["text"] == "gallery words"
        assert [row["status"] for row in items["1_voice"]["transcripts"]] == ["done"]
        assert items["2_voice"]["transcript"] is None
        assert [row["status"] for row in items["2_voice"]["transcripts"]] == ["queued"]
        assert media_id not in resp.text, "the rows carry no storage media id"

    async def test_the_changes_route_reports_the_transcript_kind(self, real_adapter):
        await _voice(real_adapter, 1, "feed words")
        async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
            resp = await client.get("/api/changes")
        assert resp.status_code == 200, resp.text
        assert [(c["kind"], c["text"]) for c in resp.json()["changes"]] == [("transcript", "feed words")]
