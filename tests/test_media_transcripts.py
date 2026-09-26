"""The ``media_transcripts`` adapter methods on real engines (docs/TRANSCRIPTION.md).

Insert-if-absent of a queued row, the fill rule (status advances, every
other column is written once), the drain query with its retry rules, the
skipped row, and the app_settings helpers. Runs on SQLite and, when a server
is reachable, on PostgreSQL.
"""

from datetime import datetime, timedelta

from sqlalchemy import text, update

from src.db.adapter import TRANSCRIPTION_EVENTS_CURSOR_KEY, TRANSCRIPTION_SERVER_KEY
from src.db.models import MediaTranscript
from src.message_utils import utcnow_naive

CHAT = -420300001
TYPES = ("voice", "video_note")


async def _media(adapter, media_id: str, *, account_id: int = 1, media_type: str = "voice", **extra) -> dict:
    """A downloaded media row with its parent chat and message.

    PostgreSQL enforces ``fk_media_message``; SQLite does not, so a media
    row without parents would pass there and fail on the other engine.
    """
    row = {
        "id": media_id,
        "message_id": int(media_id.split("_")[1]),
        "chat_id": CHAT,
        "type": media_type,
        "file_path": f"{CHAT}/{media_id}.ogg",
        "downloaded": True,
        "duration": 12,
        "download_date": datetime(2026, 1, 2, 3, 4, 5),
    } | extra
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "fixture chat"}, account_id=account_id)
    await adapter.insert_message(
        {"id": row["message_id"], "chat_id": CHAT, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
        account_id=account_id,
    )
    await adapter.insert_media(row, account_id=account_id)
    return row


async def _drain(adapter, *, account_id: int = 1, per_run: int = 50, types=TYPES, stale_minutes: int = 10):
    stale_before = utcnow_naive() - timedelta(minutes=stale_minutes)
    rows = await adapter.get_media_awaiting_transcription(
        account_id=account_id, types=types, per_run=per_run, stale_before=stale_before
    )
    return [row["id"] for row in rows]


async def _age(adapter, transcript_id: int, minutes: int) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            update(MediaTranscript)
            .where(MediaTranscript.id == transcript_id)
            .values(requested_at=utcnow_naive() - timedelta(minutes=minutes))
        )
        await session.commit()


class TestEnqueue:
    async def test_first_enqueue_inserts_a_queued_row(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        row = await real_adapter.enqueue_media_transcript(
            "m_1_voice", account_id=1, content_hash="a" * 64, idempotency_key="a" * 64, preset="auto", source="openai"
        )
        assert row["status"] == "queued"
        assert row["job_id"] is None
        assert row["idempotency_key"] == "a" * 64
        assert row["preset"] == "auto"
        assert isinstance(row["requested_at"], datetime)

    async def test_second_enqueue_is_a_noop_while_a_row_is_open(self, real_adapter):
        first = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        second = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        assert second["id"] == first["id"]
        await real_adapter.fill_media_transcript(first["id"], status="running")
        third = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        assert third["id"] == first["id"]
        assert len(await real_adapter.list_media_transcripts("m_1_voice", account_id=1)) == 1

    async def test_no_new_row_after_done_unless_forced(self, real_adapter):
        first = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(first["id"], status="done", text="hola")
        assert await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1) is None
        forced = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True)
        assert forced["id"] != first["id"]
        assert forced["status"] == "queued"
        rows = await real_adapter.list_media_transcripts("m_1_voice", account_id=1)
        assert [r["id"] for r in rows] == [forced["id"], first["id"]]  # newest first

    async def test_no_new_row_after_skipped_unless_forced(self, real_adapter):
        await real_adapter.mark_media_transcript_skipped("m_1_voice", account_id=1, reason="too long")
        assert await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1) is None
        assert (await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True))[
            "status"
        ] == "queued"

    async def test_a_failed_newest_row_allows_a_new_row(self, real_adapter):
        first = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(first["id"], status="failed", error="submit_failed")
        second = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        assert second["id"] != first["id"]

    async def test_accounts_do_not_share_rows(self, real_adapter):
        a = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        b = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=2)
        assert a["id"] != b["id"]
        assert [r["id"] for r in await real_adapter.list_media_transcripts("m_1_voice", account_id=2)] == [b["id"]]

    async def test_a_second_open_row_planted_underneath_is_refused_and_reported_as_the_winner(self, real_adapter):
        """The partial unique index, as the cross-process race would hit it."""
        first = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(first["id"], status="failed", error="x")
        # Plant an open row directly, then race a second enqueue against it by
        # making the newest-row read see the failed row: the index must refuse
        # the insert and the loser must return the existing open row.
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO media_transcripts (account_id, media_id, status, requested_at, created_at) "
                    "VALUES (1, 'm_1_voice', 'queued', :t, :t)"
                ),
                {"t": datetime(2026, 1, 1)},
            )
            await session.commit()
        again = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        assert again["status"] == "queued"
        rows = await real_adapter.list_media_transcripts("m_1_voice", account_id=1)
        assert [r["status"] for r in rows] == ["queued", "failed"]


class TestFill:
    async def test_status_advances_and_other_columns_are_written_once(self, real_adapter):
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, preset="auto")
        assert await real_adapter.fill_media_transcript(row["id"], status="running", job_id="job-1")
        assert await real_adapter.fill_media_transcript(
            row["id"],
            status="done",
            text="hola",
            language="es",
            words=[{"w": "hola", "s": 0.0, "e": 0.4, "c": 0.9}],
            segments=[{"s": 0.0, "e": 0.4, "text": "hola", "speaker": None}],
            models=["parakeet-v3"],
            engine_name="akou",
            duration_s=0.4,
        )
        stored = await real_adapter.get_media_transcript(row["id"])
        assert stored["status"] == "done"
        assert stored["job_id"] == "job-1"
        assert stored["text"] == "hola"
        assert stored["words"] == [{"w": "hola", "s": 0.0, "e": 0.4, "c": 0.9}]
        assert stored["models"] == ["parakeet-v3"]
        assert isinstance(stored["completed_at"], datetime)

        # A row at a final status is left alone, however it is written again.
        assert not await real_adapter.fill_media_transcript(row["id"], status="done", text="otra")
        assert not await real_adapter.fill_media_transcript(row["id"], status="failed", error="late")
        assert not await real_adapter.fill_media_transcript(row["id"], status="running")
        unchanged = await real_adapter.get_media_transcript(row["id"])
        assert unchanged["text"] == "hola"
        assert unchanged["status"] == "done"
        assert unchanged["error"] is None

    async def test_a_filled_column_is_never_overwritten(self, real_adapter):
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="running", job_id="job-1", engine_name="akou")
        await real_adapter.fill_media_transcript(row["id"], status="running", job_id="job-2", engine_version="0.2")
        stored = await real_adapter.get_media_transcript(row["id"])
        assert stored["job_id"] == "job-1"
        assert stored["engine_name"] == "akou"
        assert stored["engine_version"] == "0.2"

    async def test_the_first_job_id_stamps_job_stored_at_once(self, real_adapter):
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="queued", preset="auto")
        assert (await real_adapter.get_media_transcript(row["id"]))["job_stored_at"] is None
        await real_adapter.fill_media_transcript(row["id"], status="queued", job_id="job-1")
        stamped = (await real_adapter.get_media_transcript(row["id"]))["job_stored_at"]
        assert isinstance(stamped, datetime)
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(update(MediaTranscript).values(job_stored_at=stamped - timedelta(days=1)))
            await session.commit()
        # A later fill naming a job, the same or another, never moves it.
        await real_adapter.fill_media_transcript(row["id"], status="running", job_id="job-2")
        await real_adapter.fill_media_transcript(row["id"], status="done", job_id="job-1", text="hola")
        stored = await real_adapter.get_media_transcript(row["id"])
        assert (stored["job_id"], stored["job_stored_at"]) == ("job-1", stamped - timedelta(days=1))

    async def test_status_never_moves_backwards(self, real_adapter):
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="running")
        assert not await real_adapter.fill_media_transcript(row["id"], status="queued")
        assert (await real_adapter.get_media_transcript(row["id"]))["status"] == "running"

    async def test_unknown_status_or_column_is_refused(self, real_adapter):
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        for bad in ({"status": "lost"}, {"status": "done", "media_id": "other"}):
            try:
                await real_adapter.fill_media_transcript(row["id"], **bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad}")

    async def test_account_scope_on_fill(self, real_adapter):
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        assert not await real_adapter.fill_media_transcript(row["id"], status="done", account_id=2)
        assert await real_adapter.fill_media_transcript(row["id"], status="done", account_id=1)


class TestSkipped:
    async def test_skipped_row_carries_the_reason(self, real_adapter):
        row = await real_adapter.mark_media_transcript_skipped(
            "m_1_voice", account_id=1, reason="longer than 1800 s", duration_s=3600.0
        )
        assert row["status"] == "skipped"
        assert row["error"] == "longer than 1800 s"
        assert row["job_id"] is None
        assert row["duration_s"] == 3600.0
        again = await real_adapter.mark_media_transcript_skipped("m_1_voice", account_id=1, reason="longer than 1800 s")
        assert again["id"] == row["id"]

    async def test_an_open_row_is_closed_as_skipped_instead_of_a_new_one(self, real_adapter):
        open_row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        skipped = await real_adapter.mark_media_transcript_skipped("m_1_voice", account_id=1, reason="too long")
        assert skipped["id"] == open_row["id"]
        assert skipped["status"] == "skipped"
        assert len(await real_adapter.list_media_transcripts("m_1_voice", account_id=1)) == 1


class TestDrainQuery:
    async def test_media_without_a_row_is_selected_newest_download_first(self, real_adapter):
        await _media(real_adapter, "m_1_voice", download_date=datetime(2026, 1, 1))
        await _media(real_adapter, "m_2_voice", download_date=datetime(2026, 1, 3))
        await _media(real_adapter, "m_3_video_note", media_type="video_note", download_date=datetime(2026, 1, 2))
        assert await _drain(real_adapter) == ["m_2_voice", "m_3_video_note", "m_1_voice"]
        assert await _drain(real_adapter, per_run=1) == ["m_2_voice"]

    async def test_types_not_downloaded_and_other_accounts_are_left_out(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        await _media(real_adapter, "m_2_photo", media_type="photo")
        await _media(real_adapter, "m_3_voice", downloaded=False)
        await _media(real_adapter, "m_4_voice", account_id=2)
        assert await _drain(real_adapter) == ["m_1_voice"]
        assert await _drain(real_adapter, types=("photo",)) == ["m_2_photo"]
        assert await _drain(real_adapter, types=()) == []
        assert await _drain(real_adapter, account_id=2) == ["m_4_voice"]

    async def test_newest_done_row_ends_the_loop(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="done", text="hola")
        assert await _drain(real_adapter) == []

    async def test_newest_skipped_row_ends_the_loop(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        await real_adapter.mark_media_transcript_skipped("m_1_voice", account_id=1, reason="too long")
        assert await _drain(real_adapter) == []

    async def test_a_fresh_queued_row_is_not_selected_but_a_stale_one_is_and_carries_its_row(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        # The backup always writes the preset; a row without one is an ask-now.
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, preset="auto")
        assert await _drain(real_adapter) == []
        await _age(real_adapter, row["id"], minutes=11)
        rows = await real_adapter.get_media_awaiting_transcription(
            account_id=1, types=TYPES, per_run=10, stale_before=utcnow_naive() - timedelta(minutes=10)
        )
        assert [r["id"] for r in rows] == ["m_1_voice"]
        assert rows[0]["transcript"] == {"id": row["id"], "status": "queued", "job_id": None}

    async def test_an_ask_now_row_is_sent_at_once_and_first(self, real_adapter):
        """The viewer's ask-now row (no preset) skips the ten-minute wait and leads the run."""
        await _media(real_adapter, "m_1_voice", download_date=datetime(2026, 1, 1))
        await _media(real_adapter, "m_2_voice", download_date=datetime(2026, 1, 3))
        asked = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True)
        assert asked["preset"] is None
        assert await _drain(real_adapter) == ["m_1_voice", "m_2_voice"]
        assert await _drain(real_adapter, per_run=1) == ["m_1_voice"]

    async def test_an_ask_now_row_picked_up_by_the_backup_waits_like_any_other(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        asked = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True)
        await real_adapter.fill_media_transcript(asked["id"], status="queued", preset="auto")
        assert await _drain(real_adapter) == []

    async def test_a_stale_queued_row_with_a_job_id_is_the_pollers_business(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="queued", job_id="job-1")
        await _age(real_adapter, row["id"], minutes=11)
        assert await _drain(real_adapter) == []

    async def test_failed_rows_retry_until_three(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        for attempt in range(3):
            assert await _drain(real_adapter) == ["m_1_voice"], f"attempt {attempt}"
            row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
            await real_adapter.fill_media_transcript(row["id"], status="failed", error="submit_failed")
        assert await _drain(real_adapter) == []
        assert len(await real_adapter.list_media_transcripts("m_1_voice", account_id=1)) == 3

    async def test_a_done_row_after_failures_still_ends_the_loop(self, real_adapter):
        await _media(real_adapter, "m_1_voice")
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="failed", error="x")
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="done", text="hola")
        assert await _drain(real_adapter) == []

    async def test_the_result_carries_the_media_columns_the_drain_needs(self, real_adapter):
        await _media(real_adapter, "m_1_voice", content_hash="b" * 64, duration=42)
        rows = await real_adapter.get_media_awaiting_transcription(
            account_id=1, types=TYPES, per_run=10, stale_before=utcnow_naive()
        )
        assert rows[0]["file_path"] == f"{CHAT}/m_1_voice.ogg"
        assert rows[0]["content_hash"] == "b" * 64
        assert rows[0]["duration"] == 42
        assert rows[0]["message_id"] == 1
        assert rows[0]["chat_id"] == CHAT
        assert rows[0]["transcript"] is None


class TestAppSettingsHelpers:
    async def test_events_cursor_round_trip(self, real_adapter):
        assert await real_adapter.get_transcription_events_cursor() is None
        await real_adapter.set_transcription_events_cursor("evt_00042")
        assert await real_adapter.get_transcription_events_cursor() == "evt_00042"
        assert await real_adapter.get_setting(TRANSCRIPTION_EVENTS_CURSOR_KEY) == "evt_00042"

    async def test_server_round_trip_and_unreadable_value(self, real_adapter):
        assert await real_adapter.get_transcription_server() is None
        await real_adapter.set_transcription_server("akou", "0.2.0")
        assert await real_adapter.get_transcription_server() == {"name": "akou", "version": "0.2.0"}
        await real_adapter.set_setting(TRANSCRIPTION_SERVER_KEY, "{not json")
        assert await real_adapter.get_transcription_server() is None


class TestTranscriptFtsProbe:
    async def test_probe_reports_the_search_objects(self, real_adapter):
        async with real_adapter.db_manager.async_session_factory() as session:
            assert await real_adapter._transcript_fts_ready(session) is True

    async def test_probe_is_false_when_the_objects_are_missing(self, real_adapter):
        if not real_adapter._is_sqlite:
            return
        async with real_adapter.db_manager.async_session_factory() as session:
            for trigger in ("media_transcripts_fts_ai", "media_transcripts_fts_ad", "media_transcripts_fts_au"):
                await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
            await session.execute(text("DROP TABLE IF EXISTS media_transcripts_fts"))
            await session.commit()
        async with real_adapter.db_manager.async_session_factory() as session:
            assert await real_adapter._transcript_fts_ready(session) is False
