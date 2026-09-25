"""The synchronous transcription path (slice 2 of docs/TRANSCRIPTION.md).

A fake server behind ``httpx.MockTransport`` answers ``/v1/server`` with a
404 (any non-akou server) and ``/v1/audio/transcriptions`` with a fixed
``verbose_json``. The drain stores one done row and never resends for the
same media; a failing server adds one failed row per drain and the drain
stops after three; media over the limit gets a skipped row and no request;
the listener enqueues a just-downloaded voice message; the viewer route
returns the rows; and no log line carries the key or the URL's query.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.realtime import NotificationType
from src.transcription import (
    STALE_QUEUED,
    ServerInfo,
    TranscriptionClient,
    TranscriptionError,
    drain_transcriptions,
    result_columns,
    transcribe_media,
)

sys.path.insert(0, os.path.dirname(__file__))

KEY = "test@value/here"
URL = "http://akou.example.test:9000/base?token=" + KEY
CHAT = -420300001
AUDIO = b"OggS fake voice note bytes"

VERBOSE_JSON = {
    "task": "transcribe",
    "language": "es",
    "duration": 2.5,
    "text": "hola, te llamo luego",
    "words": [
        {"word": "hola", "start": 0.0, "end": 0.4, "probability": 0.98},
        {"word": "te", "start": 0.5, "end": 0.6},
    ],
    "segments": [{"id": 0, "start": 0.0, "end": 2.5, "text": "hola, te llamo luego"}],
}


class FakeServer:
    """Scripted transcription server: counts requests, answers by path."""

    def __init__(self, *, server_status: int = 404, server_body: dict | None = None, transcribe_status: int = 200):
        self.server_status = server_status
        self.server_body = server_body
        self.transcribe_status = transcribe_status
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/v1/server"):
            if self.server_status == 200:
                return httpx.Response(200, json=self.server_body or {})
            return httpx.Response(self.server_status)
        if request.url.path.endswith("/v1/audio/transcriptions"):
            if self.transcribe_status == 200:
                return httpx.Response(200, json=VERBOSE_JSON)
            if self.transcribe_status == 302:
                return httpx.Response(302, headers={"Location": "http://elsewhere.example.test/"})
            return httpx.Response(self.transcribe_status, json={"error": "nope"})
        return httpx.Response(404)

    @property
    def transcribe_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/v1/audio/transcriptions")]


def _config(media_root: str, **overrides) -> SimpleNamespace:
    values = {
        "transcription_enabled": True,
        "transcription_url": URL,
        "transcription_api_key": KEY,
        "transcription_preset": "auto",
        "transcription_types": {"voice", "video_note"},
        "transcription_max_seconds": 1800,
        "transcription_language": "",
        "transcription_backfill_per_run": 50,
        "media_path": media_root,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(config, server: FakeServer) -> TranscriptionClient:
    client = TranscriptionClient(config, transport=server.transport)
    client.backoffs = (0.0, 0.0)
    return client


async def _media(adapter, tmp_path, media_id: str, *, duration: int = 12, content_hash=None, on_disk=True) -> dict:
    path = tmp_path / str(CHAT) / f"{media_id}.ogg"
    if on_disk:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(AUDIO)
    row = {
        "id": media_id,
        "message_id": int(media_id.split("_")[1]),
        "chat_id": CHAT,
        "type": "voice",
        "file_path": str(path),
        "downloaded": True,
        "duration": duration,
        "content_hash": content_hash,
        "download_date": datetime(2026, 1, 2, 3, 4, 5),
    }
    await adapter.insert_media(row, account_id=1)
    return row


async def _rows(adapter, media_id: str) -> list[dict]:
    return await adapter.list_media_transcripts(media_id, account_id=1)


# ============================================================================
# The client
# ============================================================================


class TestClient:
    async def test_404_on_server_selects_the_synchronous_path(self, tmp_path):
        server = FakeServer(server_status=404)
        info = await _client(_config(str(tmp_path)), server).detect_server()
        assert info == ServerInfo()

    async def test_akou_answer_is_read_and_unknown_fields_are_ignored(self, tmp_path):
        server = FakeServer(
            server_status=200,
            server_body={"name": "akou", "version": "0.2.0", "capabilities": {"jobs": True, "quantum": 1}, "extra": 9},
        )
        info = await _client(_config(str(tmp_path)), server).detect_server()
        assert info == ServerInfo(name="akou", version="0.2.0", jobs=True)

    async def test_a_200_that_is_not_akou_shaped_selects_the_synchronous_path(self, tmp_path):
        server = FakeServer(server_status=200, server_body={"status": "ok"})
        info = await _client(_config(str(tmp_path)), server).detect_server()
        assert info == ServerInfo()

    async def test_unreachable_server_raises_with_the_exception_class_only(self, tmp_path):
        def boom(request):
            raise httpx.ConnectError(f"cannot reach {URL}")

        client = TranscriptionClient(_config(str(tmp_path)), transport=httpx.MockTransport(boom))
        with pytest.raises(TranscriptionError) as excinfo:
            await client.detect_server()
        assert excinfo.value.reason == "ConnectError"
        assert KEY not in str(excinfo.value)

    async def test_transcribe_sends_the_openai_multipart_with_the_bearer_key(self, tmp_path):
        server = FakeServer()
        config = _config(str(tmp_path), transcription_language="es", transcription_hotwords=["Neutral", "akou"])
        client = _client(config, server)
        payload = await client.transcribe(AUDIO, "note.ogg", prompt="Neutral, akou")
        assert payload == VERBOSE_JSON
        request = server.transcribe_requests[0]
        assert request.url.scheme == "http"
        assert request.url.host == "akou.example.test"
        assert request.url.port == 9000
        assert request.url.path == "/base/v1/audio/transcriptions"
        assert request.url.query == b""  # the configured query is not forwarded
        assert request.headers["authorization"] == f"Bearer {KEY}"
        body = request.content.decode("latin-1")
        for field, value in (
            ("model", "auto"),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("language", "es"),
            ("prompt", "Neutral, akou"),
        ):
            assert f'name="{field}"\r\n\r\n{value}\r\n' in body, field
        assert 'name="file"; filename="note.ogg"' in body
        assert AUDIO.decode("latin-1") in body

    async def test_no_key_means_no_authorization_header(self, tmp_path):
        server = FakeServer()
        await _client(_config(str(tmp_path), transcription_api_key=""), server).transcribe(AUDIO, "a.ogg")
        assert "authorization" not in server.transcribe_requests[0].headers

    async def test_empty_preset_falls_back_to_whisper_1(self, tmp_path):
        server = FakeServer()
        await _client(_config(str(tmp_path), transcription_preset=""), server).transcribe(AUDIO, "a.ogg")
        assert 'name="model"\r\n\r\nwhisper-1\r\n' in server.transcribe_requests[0].content.decode("latin-1")

    async def test_5xx_is_retried_up_to_three_attempts_then_fails(self, tmp_path):
        server = FakeServer(transcribe_status=503)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg")
        assert excinfo.value.reason == "HTTP 503"
        assert len(server.transcribe_requests) == 3

    async def test_4xx_is_permanent_after_one_attempt(self, tmp_path):
        server = FakeServer(transcribe_status=422)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg")
        assert excinfo.value.reason == "HTTP 422"
        assert len(server.transcribe_requests) == 1

    async def test_redirects_are_not_followed(self, tmp_path):
        server = FakeServer(transcribe_status=302)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg")
        assert excinfo.value.reason == "HTTP 302"
        assert len(server.requests) == 1

    async def test_the_job_path_is_a_named_stub_for_slice_4(self, tmp_path):
        client = _client(_config(str(tmp_path)), FakeServer())
        for call in (
            client.submit_job(AUDIO, "a.ogg", idempotency_key="x"),
            client.fetch_events(None),
            client.poll_job("job-1"),
        ):
            with pytest.raises(NotImplementedError):
                await call

    def test_bad_or_missing_url_means_not_configured(self, tmp_path):
        assert not TranscriptionClient(_config(str(tmp_path), transcription_url="")).configured
        assert not TranscriptionClient(_config(str(tmp_path), transcription_url="ftp://x")).configured
        assert not TranscriptionClient(MagicMock()).configured  # a bare mock reads truthy; the type check holds

    def test_verbose_json_maps_onto_the_columns(self):
        columns = result_columns(VERBOSE_JSON, model="auto")
        assert columns["text"] == "hola, te llamo luego"
        assert columns["language"] == "es"
        assert columns["duration_s"] == 2.5
        assert columns["words"] == [
            {"w": "hola", "s": 0.0, "e": 0.4, "c": 0.98},
            {"w": "te", "s": 0.5, "e": 0.6, "c": None},
        ]
        assert columns["segments"] == [{"s": 0.0, "e": 2.5, "text": "hola, te llamo luego", "speaker": None}]
        assert columns["models"] == ["auto"]
        assert result_columns({"text": 7, "words": "nope"}, model="m")["text"] == ""


# ============================================================================
# The drain, on a real database
# ============================================================================


class TestDrain:
    async def test_stores_one_done_row_and_never_resends_for_the_same_media(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = FakeServer()
        config = _config(str(tmp_path))
        notifier = AsyncMock()

        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )

        assert stats["done"] == 1
        assert len(server.transcribe_requests) == 1
        rows = await _rows(real_adapter, "m_1_voice")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "done"
        assert row["source"] == "openai"
        assert row["engine_name"] == "openai"  # a 404 on /v1/server names no engine
        assert row["preset"] == "auto"
        assert row["job_id"] is None
        assert row["text"] == "hola, te llamo luego"
        assert row["language"] == "es"
        assert row["duration_s"] == 2.5
        assert row["words"][0] == {"w": "hola", "s": 0.0, "e": 0.4, "c": 0.98}
        assert row["models"] == ["auto"]
        assert isinstance(row["completed_at"], datetime)
        # The media row carries no hash, so the audio was hashed at drain
        # time and stored on the transcript row only.
        import hashlib

        assert row["idempotency_key"] == hashlib.sha256(AUDIO).hexdigest()
        assert row["content_hash"] is None
        media = await real_adapter.get_media_by_id("m_1_voice", account_id=1)
        assert media is not None

        notifier.notify.assert_awaited_once()
        kind, chat_id, data = notifier.notify.await_args.args
        assert kind == NotificationType.TRANSCRIPT
        assert chat_id == CHAT
        assert data == {
            "account_id": 1,
            "chat_id": CHAT,
            "message_id": 1,
            "media_id": "m_1_voice",
            "transcript_id": row["id"],
            "status": "done",
        }
        assert notifier.notify.await_args.kwargs == {"account_id": 1}

        # A second drain finds nothing to send.
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )
        assert stats == {"done": 0, "failed": 0, "skipped": 0, "noop": 0}
        assert len(server.transcribe_requests) == 1
        assert len(await _rows(real_adapter, "m_1_voice")) == 1

    async def test_a_media_hash_becomes_the_idempotency_key(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="c" * 64)
        config = _config(str(tmp_path))
        await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )
        row = (await _rows(real_adapter, "m_1_voice"))[0]
        assert row["idempotency_key"] == "c" * 64
        assert row["content_hash"] == "c" * 64

    async def test_a_failing_server_adds_one_failed_row_per_drain_and_stops_after_three(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = FakeServer(transcribe_status=500)
        config = _config(str(tmp_path))
        notifier = AsyncMock()
        for drain in range(1, 4):
            stats = await drain_transcriptions(
                config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
            )
            assert stats["failed"] == 1, f"drain {drain}"
            rows = await _rows(real_adapter, "m_1_voice")
            assert [r["status"] for r in rows] == ["failed"] * drain
            assert rows[0]["error"] == "HTTP 500"
        sent_before = len(server.transcribe_requests)
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )
        assert stats["failed"] == 0
        assert len(server.transcribe_requests) == sent_before
        assert len(await _rows(real_adapter, "m_1_voice")) == 3
        assert all(call.args[2]["status"] == "failed" for call in notifier.notify.await_args_list)

    async def test_media_over_the_limit_gets_a_skipped_row_and_no_request(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", duration=1801)
        await _media(real_adapter, tmp_path, "m_2_voice", duration=1800)
        server = FakeServer()
        config = _config(str(tmp_path))
        notifier = AsyncMock()

        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )

        assert stats == {"done": 1, "failed": 0, "skipped": 1, "noop": 0}
        assert len(server.transcribe_requests) == 1
        skipped = (await _rows(real_adapter, "m_1_voice"))[0]
        assert skipped["status"] == "skipped"
        assert skipped["error"] == "longer than the 1800 second limit"
        assert skipped["duration_s"] == 1801.0
        assert skipped["job_id"] is None
        assert (await _rows(real_adapter, "m_2_voice"))[0]["status"] == "done"
        statuses = sorted(call.args[2]["status"] for call in notifier.notify.await_args_list)
        assert statuses == ["done", "skipped"]

        # The skipped row ends the loop: a second drain sends nothing more.
        await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )
        assert len(server.transcribe_requests) == 1

    async def test_a_missing_file_is_a_failed_row_without_a_request(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", on_disk=False)
        server = FakeServer()
        config = _config(str(tmp_path))
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )
        assert stats["failed"] == 1
        assert server.transcribe_requests == []
        assert (await _rows(real_adapter, "m_1_voice"))[0]["error"] == "file_missing"

    async def test_an_unreachable_server_writes_no_rows(self, real_adapter, tmp_path, caplog):
        await _media(real_adapter, tmp_path, "m_1_voice")

        def boom(request):
            raise httpx.ConnectError(f"cannot reach {URL}")

        config = _config(str(tmp_path))
        client = TranscriptionClient(config, transport=httpx.MockTransport(boom))
        with caplog.at_level(logging.DEBUG, logger="src.transcription"):
            stats = await drain_transcriptions(config, real_adapter, account_id=1, notifier=AsyncMock(), client=client)
        assert stats == {"done": 0, "failed": 0, "skipped": 0, "noop": 0}
        assert await _rows(real_adapter, "m_1_voice") == []
        assert any("unreachable" in record.getMessage() for record in caplog.records)

    async def test_the_server_row_is_written_only_when_the_server_names_itself(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path))
        await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )
        assert await real_adapter.get_transcription_server() is None

        akou = FakeServer(
            server_status=200, server_body={"name": "akou", "version": "0.2.0", "capabilities": {"jobs": True}}
        )
        await _media(real_adapter, tmp_path, "m_2_voice")
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, akou)
        )
        assert await real_adapter.get_transcription_server() == {"name": "akou", "version": "0.2.0"}
        # jobs offered but the job path is slice 4: the synchronous path is taken.
        assert stats["done"] == 1
        assert len(akou.transcribe_requests) == 1
        row = (await _rows(real_adapter, "m_2_voice"))[0]
        assert row["engine_name"] == "akou"
        assert row["engine_version"] == "0.2.0"
        assert row["source"] == "openai"

    async def test_per_run_and_types_are_honoured(self, real_adapter, tmp_path):
        for n in range(3):
            await _media(real_adapter, tmp_path, f"m_{n}_voice")
        config = _config(str(tmp_path), transcription_backfill_per_run=2)
        server = FakeServer()
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )
        assert stats["done"] == 2
        config = _config(str(tmp_path), transcription_types={"video_note"})
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )
        assert stats["done"] == 0
        assert len(server.transcribe_requests) == 2

    async def test_disabled_or_unconfigured_does_nothing(self, real_adapter, tmp_path, caplog):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = FakeServer()
        for config in (
            _config(str(tmp_path), transcription_enabled=False),
            _config(str(tmp_path), transcription_url=""),
            MagicMock(),  # a bare mock config must never enable the feature
        ):
            with caplog.at_level(logging.DEBUG, logger="src.transcription"):
                stats = await drain_transcriptions(config, real_adapter, account_id=1, client=_client(config, server))
            assert stats == {"done": 0, "failed": 0, "skipped": 0, "noop": 0}
        assert server.requests == []
        assert not [r for r in caplog.records if r.levelno >= logging.INFO]

    async def test_a_stale_queued_row_is_resubmitted_on_the_same_row(self, real_adapter, tmp_path):
        """A process died between the insert and the submit: no new row, no failed row."""
        from datetime import timedelta

        from sqlalchemy import update

        from src.db.models import MediaTranscript
        from src.message_utils import utcnow_naive

        await _media(real_adapter, tmp_path, "m_1_voice")
        stale = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                update(MediaTranscript)
                .where(MediaTranscript.id == stale["id"])
                .values(requested_at=utcnow_naive() - STALE_QUEUED - timedelta(minutes=1))
            )
            await session.commit()
        config = _config(str(tmp_path))
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )
        assert stats["done"] == 1
        rows = await _rows(real_adapter, "m_1_voice")
        assert [r["id"] for r in rows] == [stale["id"]]
        assert rows[0]["status"] == "done"

    async def test_log_lines_carry_neither_the_key_nor_the_url_query(self, real_adapter, tmp_path, caplog):
        await _media(real_adapter, tmp_path, "m_1_voice")
        await _media(real_adapter, tmp_path, "m_2_voice", duration=5000)
        config = _config(str(tmp_path))
        with caplog.at_level(logging.DEBUG):
            await drain_transcriptions(
                config,
                real_adapter,
                account_id=1,
                notifier=AsyncMock(),
                client=_client(config, FakeServer(transcribe_status=500)),
            )

            def boom(request):
                raise httpx.ConnectError(f"cannot reach {URL}")

            client = TranscriptionClient(config, transport=httpx.MockTransport(boom))
            await drain_transcriptions(config, real_adapter, account_id=1, notifier=AsyncMock(), client=client)
            await transcribe_media(
                config,
                real_adapter,
                {
                    "id": "m_9_voice",
                    "chat_id": CHAT,
                    "message_id": 9,
                    "type": "voice",
                    "file_path": "/nowhere.ogg",
                    "duration": 3,
                },
                account_id=1,
                client=client,
            )
        # Our own lines. httpx's own request log names the URL at INFO, which
        # is why setup_logging raises that logger to WARNING (test_transcription_config).
        joined = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("src."))
        assert joined  # the control: something was logged
        assert KEY not in joined
        assert "token=" not in joined
        assert "akou.example.test" not in joined
        assert str(CHAT) not in joined


# ============================================================================
# The listener's immediate enqueue
# ============================================================================


class TestListenerEnqueue:
    async def test_a_downloaded_voice_message_is_transcribed_at_once(self):
        from telethon import events
        from test_listener_extended import _make_listener_with_handlers, _media_event, _voice_media

        listener, handlers, db, config = _make_listener_with_handlers(
            listen_new_messages_media=True,
            transcription_enabled=True,
            transcription_url=URL,
            transcription_types={"voice", "video_note"},
        )
        listener._download_media = AsyncMock(return_value=("/tmp/media/-100/voice.ogg", "voice.ogg", "hash123"))
        with patch("src.listener.transcribe_media", new=AsyncMock(return_value="done")) as transcribe:
            await handlers[events.NewMessage](_media_event(_voice_media(duration=7, size=4321)))
            await asyncio.gather(*listener._transcription_tasks)
        transcribe.assert_awaited_once()
        args, kwargs = transcribe.await_args
        assert args[0] is config
        assert args[1] is db
        assert args[2] == db.insert_media.call_args[0][0]
        assert args[2]["type"] == "voice"
        assert kwargs == {"account_id": 1, "notifier": None}
        assert listener._transcription_tasks == set()

    async def test_other_types_a_missing_server_and_a_mock_config_enqueue_nothing(self):
        from telethon import events
        from test_listener_extended import _make_listener_with_handlers, _media_event, _video_media, _voice_media

        cases = [
            (
                {"transcription_enabled": True, "transcription_url": URL, "transcription_types": {"voice"}},
                _video_media(),
            ),
            (
                {"transcription_enabled": True, "transcription_url": "", "transcription_types": {"voice"}},
                _voice_media(),
            ),
            (
                {"transcription_enabled": False, "transcription_url": URL, "transcription_types": {"voice"}},
                _voice_media(),
            ),
            ({}, _voice_media()),  # a bare MagicMock config: every attribute is truthy, none is True
        ]
        for overrides, media in cases:
            listener, handlers, db, config = _make_listener_with_handlers(listen_new_messages_media=True, **overrides)
            listener._download_media = AsyncMock(return_value=("/tmp/media/-100/x.bin", "x.bin", "h"))
            with patch("src.listener.transcribe_media", new=AsyncMock()) as transcribe:
                await handlers[events.NewMessage](_media_event(media))
                await asyncio.gather(*listener._transcription_tasks)
            db.insert_media.assert_called_once()
            transcribe.assert_not_awaited()

    async def test_a_transcription_error_never_reaches_the_handler(self):
        from telethon import events
        from test_listener_extended import _make_listener_with_handlers, _media_event, _voice_media

        listener, handlers, db, config = _make_listener_with_handlers(
            listen_new_messages_media=True,
            transcription_enabled=True,
            transcription_url=URL,
            transcription_types={"voice"},
        )
        listener._download_media = AsyncMock(return_value=("/tmp/media/-100/voice.ogg", "voice.ogg", "hash123"))
        with patch("src.listener.transcribe_media", new=AsyncMock(side_effect=RuntimeError("boom"))):
            await handlers[events.NewMessage](_media_event(_voice_media()))
            await asyncio.gather(*listener._transcription_tasks)
        assert listener.stats["new_messages_received"] == 1


# ============================================================================
# The backup's drain call
# ============================================================================


class TestBackupDrain:
    def test_backup_all_drains_after_the_media_sweeps(self):
        """The drain is the last step of a run, right after the pending-media retry."""
        from telethon.tl.types import User
        from test_telegram_backup_extended import TestBackupAllNonWhitelistMode, _run

        # Borrow the one fixture that drives backup_all through a real dialog;
        # with no dialogs the run returns before the sweeps.
        case = TestBackupAllNonWhitelistMode("test_non_whitelist_fetches_dialogs_and_backs_up")
        case.setUp()
        try:
            backup = case.backup
            dialog = case._make_dialog(case._make_entity(User, 100, bot=False))
            backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
            order: list[str] = []
            backup._retry_pending_media_downloads = AsyncMock(side_effect=lambda: order.append("retry"))
            backup._verify_and_redownload_media = AsyncMock(side_effect=lambda: order.append("verify"))
            backup._drain_transcriptions = AsyncMock(side_effect=lambda: order.append("drain"))

            _run(backup.backup_all())

            assert order == ["retry", "drain"]
            backup.config.verify_media = True
            order.clear()
            backup._get_dialogs = AsyncMock(side_effect=[[dialog], []])
            _run(backup.backup_all())
            assert order == ["retry", "verify", "drain"]
        finally:
            case.tearDown()

    def test_the_drain_never_fails_the_backup_and_a_mock_config_never_enables_it(self):
        from src.telegram_backup import TelegramBackup

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.db = AsyncMock()
        backup.config = SimpleNamespace(transcription_enabled=True)
        with patch(
            "src.telegram_backup.drain_transcriptions", new=AsyncMock(side_effect=RuntimeError("boom"))
        ) as drain:
            asyncio.run(backup._drain_transcriptions())
        drain.assert_awaited_once_with(backup.config, backup.db, account_id=1)

        backup.config = MagicMock()
        with patch("src.telegram_backup.drain_transcriptions", new=AsyncMock()) as drain:
            asyncio.run(backup._drain_transcriptions())
        drain.assert_not_awaited()


# ============================================================================
# Realtime type and the viewer
# ============================================================================


def test_transcript_notification_type():
    assert NotificationType.TRANSCRIPT.value == "transcript"


def test_the_template_fetches_the_rows_on_a_transcript_frame():
    template = os.path.join(os.path.dirname(__file__), "..", "src", "web", "templates", "index.html")
    with open(template, encoding="utf-8") as handle:
        source = handle.read()
    case = source.index("case 'transcript':")
    assert case > source.index("case 'reaction':")
    block = source[case : source.index("case 'delete':", case)]
    assert "/api/media/${encodeURIComponent(data.media_id)}/transcripts" in block
    assert "target.media.transcripts = rows" in block


pytest.importorskip("fastapi")

from test_web_routes import _skip_unless_web, _WebTestBase, web_main  # noqa: E402


def _transcript_row(row_id: int, status: str = "done") -> dict:
    return {
        "id": row_id,
        "account_id": 1,
        "media_id": "m_1_voice",
        "content_hash": None,
        "idempotency_key": "a" * 64,
        "source": "openai",
        "engine_name": "akou",
        "engine_version": "0.2.0",
        "preset": "auto",
        "models": ["auto"],
        "language": "es",
        "language_confidence": None,
        "text": "hola" if status == "done" else None,
        "words": [],
        "segments": [],
        "confidence": None,
        "duration_s": 2.5,
        "job_id": None,
        "status": status,
        "error": None,
        "requested_at": datetime(2026, 1, 2, 3, 4, 5),
        "completed_at": datetime(2026, 1, 2, 3, 4, 9) if status == "done" else None,
        "created_at": datetime(2026, 1, 2, 3, 4, 5),
    }


@_skip_unless_web
class TestTranscriptsRoute(_WebTestBase):
    def setUp(self):
        super().setUp()
        self.mock_db.get_media_chat_pairs = AsyncMock(
            return_value=[{"account_id": 1, "chat_id": CHAT, "message_id": 1}]
        )
        self.mock_db.get_chat_by_id = AsyncMock(
            return_value={"id": CHAT, "account_id": 1, "ref": "refA", "type": "group"}
        )
        self.mock_db.list_media_transcripts = AsyncMock(
            side_effect=lambda media_id, *, account_id: [_transcript_row(2, "failed"), _transcript_row(1)]
        )

    async def test_returns_the_rows_newest_first_with_iso_dates(self):
        async with self._client() as client:
            resp = await client.get("/api/media/m_1_voice/transcripts")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual([r["id"] for r in rows], [2, 1])
        self.assertEqual(rows[1]["text"], "hola")
        self.assertEqual(rows[1]["completed_at"], "2026-01-02T03:04:09")
        self.assertIsNone(rows[0]["completed_at"])
        self.mock_db.list_media_transcripts.assert_awaited_once_with("m_1_voice", account_id=1)

    async def test_unknown_media_is_a_404(self):
        self.mock_db.get_media_chat_pairs = AsyncMock(return_value=[])
        async with self._client() as client:
            resp = await client.get("/api/media/nope/transcripts")
        self.assertEqual(resp.status_code, 404)
        self.mock_db.list_media_transcripts.assert_not_awaited()

    async def test_a_chat_the_caller_may_not_see_is_a_404(self):
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: web_main.UserContext(
            username="viewer-test", role="viewer", allowed_chat_refs={"refB"}
        )
        try:
            async with self._client() as client:
                resp = await client.get("/api/media/m_1_voice/transcripts")
        finally:
            web_main.app.dependency_overrides.pop(web_main.require_auth, None)
        self.assertEqual(resp.status_code, 404)
        self.mock_db.list_media_transcripts.assert_not_awaited()

    async def test_a_visible_media_without_rows_is_an_empty_list(self):
        self.mock_db.list_media_transcripts = AsyncMock(return_value=[])
        async with self._client() as client:
            resp = await client.get("/api/media/m_1_voice/transcripts")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    async def test_the_realtime_handler_broadcasts_ids_and_status_only(self):
        broadcast = AsyncMock()
        with patch.object(web_main.ws_manager, "broadcast_to_chat", broadcast):
            await web_main.handle_realtime_notification(
                {
                    "type": "transcript",
                    "chat_id": CHAT,
                    "account_id": 1,
                    "data": {
                        "account_id": 1,
                        "chat_id": CHAT,
                        "message_id": 1,
                        "media_id": "m_1_voice",
                        "transcript_id": 7,
                        "status": "done",
                        "text": "must not be forwarded",
                    },
                }
            )
        broadcast.assert_awaited_once()
        chat, frame = broadcast.await_args.args
        self.assertEqual(chat["ref"], "refA")
        self.assertEqual(
            frame,
            {
                "type": "transcript",
                "chat_ref": "refA",
                "message_id": 1,
                "media_id": "m_1_voice",
                "transcript_id": 7,
                "status": "done",
            },
        )
        self.assertNotIn("text", json.dumps(frame))
