"""The synchronous transcription path (slice 2 of docs/TRANSCRIPTION.md).

A fake server behind ``httpx.MockTransport`` answers ``/v1/server`` with a
404 (any non-akou server) and ``/v1/audio/transcriptions`` with a fixed
``verbose_json``. The drain stores one done row and never resends for the
same media; a failing server adds one failed row per drain and the drain
stops after three; a server that goes down mid-run leaves the row queued,
ends the run and spends no failed row; media over the limit gets a skipped
row and no request; the listener enqueues a just-downloaded voice message;
the viewer route returns the rows; and no log line carries the key or the
URL's query.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
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
from src.transcription_contract import parse_events_page

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

    def __init__(
        self,
        *,
        server_status: int = 404,
        server_body: dict | None = None,
        transcribe_status: int = 200,
        transcribe_down: bool = False,
        transcribe_timeout: bool = False,
    ):
        self.server_status = server_status
        self.server_body = server_body
        self.transcribe_status = transcribe_status
        self.transcribe_down = transcribe_down  # /v1/server answers, the upload cannot connect
        self.transcribe_timeout = transcribe_timeout  # the upload connects, the answer never comes
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/v1/server"):
            if self.server_status == 200:
                return httpx.Response(200, json=self.server_body or {})
            return httpx.Response(self.server_status)
        if request.url.path.endswith("/v1/audio/transcriptions"):
            if self.transcribe_down:
                raise httpx.ConnectError(f"cannot reach {URL}")
            if self.transcribe_timeout:
                raise httpx.ReadTimeout(f"no answer from {URL}")
            if self.transcribe_status == 200:
                return httpx.Response(200, json=VERBOSE_JSON)
            if self.transcribe_status == 302:
                return httpx.Response(302, headers={"Location": "http://elsewhere.example.test/"})
            # OpenAI's error shape; ``code`` is null, so the reason stays ``HTTP <code>``.
            error = {"message": "nope", "type": "invalid_request_error", "code": None}
            return httpx.Response(self.transcribe_status, json={"error": error})
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


async def _media(
    adapter,
    tmp_path,
    media_id: str,
    *,
    duration: int = 12,
    content_hash=None,
    on_disk=True,
    download_date: datetime = datetime(2026, 1, 2, 3, 4, 5),
) -> dict:
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
        "download_date": download_date,
    }
    # The parents first: PostgreSQL enforces fk_media_message, SQLite does not.
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "fixture chat"}, account_id=1)
    await adapter.insert_message(
        {"id": row["message_id"], "chat_id": CHAT, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
        account_id=1,
    )
    await adapter.insert_media(row, account_id=1)
    return row


def _stats(**counts: int) -> dict[str, int]:
    """A drain's counts: every key at zero except the ones named."""
    stats = dict.fromkeys(
        (
            "done",
            "failed",
            "skipped",
            "submitted",
            "refused",
            "unreachable",
            "stalled",
            "noop",
            "reconciled",
            "polled",
            "copied",
        ),
        0,
    )
    stats.update(counts)
    return stats


async def _rows(adapter, media_id: str) -> list[dict]:
    return await adapter.list_media_transcripts(media_id, account_id=1)


async def _make_stale(adapter) -> None:
    """Move every queued row without a job past the ten-minute window, as the next backup would find it."""
    from datetime import timedelta

    from sqlalchemy import update

    from src.db.models import MediaTranscript
    from src.message_utils import utcnow_naive

    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            update(MediaTranscript)
            .where(MediaTranscript.status == "queued", MediaTranscript.job_id.is_(None))
            .values(requested_at=utcnow_naive() - STALE_QUEUED - timedelta(minutes=1))
        )
        await session.commit()


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
        assert excinfo.value.transient is True
        assert KEY not in str(excinfo.value)

    async def test_transcribe_sends_the_openai_multipart_with_the_bearer_key(self, tmp_path):
        server = FakeServer()
        config = _config(str(tmp_path), transcription_language="es", transcription_hotwords=["Neutral", "akou"])
        client = _client(config, server)
        payload = await client.transcribe(AUDIO, "note.ogg", model="auto", prompt="Neutral, akou")
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
            # OpenAI's rule, which akou follows: segments come back only when asked for.
            ("timestamp_granularities[]", "segment"),
            ("language", "es"),
            ("prompt", "Neutral, akou"),
        ):
            assert f'name="{field}"\r\n\r\n{value}\r\n' in body, field
        assert 'name="file"; filename="note.ogg"' in body
        assert AUDIO.decode("latin-1") in body

    async def test_no_key_means_no_authorization_header(self, tmp_path):
        server = FakeServer()
        client = _client(_config(str(tmp_path), transcription_api_key=""), server)
        await client.transcribe(AUDIO, "a.ogg", model="auto")
        assert "authorization" not in server.transcribe_requests[0].headers

    def test_the_preset_is_the_model_for_akou_only(self, tmp_path):
        """Only akou reads a preset in ``model``; speaches or LocalAI would refuse "auto" for good."""
        client = _client(_config(str(tmp_path), transcription_preset="best"), FakeServer())
        assert client.sync_model(ServerInfo(name="akou", version="0.2.0", jobs=True)) == "best"
        assert client.sync_model(ServerInfo(name="akou")) == "best"
        assert client.sync_model(ServerInfo()) == "whisper-1"
        assert client.sync_model(ServerInfo(name="speaches", version="1")) == "whisper-1"
        assert (
            _client(_config(str(tmp_path), transcription_preset=""), FakeServer()).sync_model(ServerInfo(name="akou"))
            == "whisper-1"
        )

    async def test_5xx_is_retried_up_to_three_attempts_then_fails(self, tmp_path):
        server = FakeServer(transcribe_status=503)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg", model="auto")
        assert excinfo.value.reason == "HTTP 503"
        assert excinfo.value.transient is False  # the server answered: a failed row is right
        assert len(server.transcribe_requests) == 3

    async def test_4xx_is_permanent_after_one_attempt(self, tmp_path):
        server = FakeServer(transcribe_status=422)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg", model="auto")
        assert excinfo.value.reason == "HTTP 422"
        assert excinfo.value.transient is False
        assert len(server.transcribe_requests) == 1

    async def test_redirects_are_not_followed(self, tmp_path):
        server = FakeServer(transcribe_status=302)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg", model="auto")
        assert excinfo.value.reason == "HTTP 302"
        assert len(server.requests) == 1

    async def test_a_transport_failure_on_every_attempt_is_transient(self, tmp_path):
        server = FakeServer(transcribe_down=True)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg", model="auto")
        assert excinfo.value.reason == "ConnectError"
        assert excinfo.value.transient is True
        assert len(server.transcribe_requests) == 3
        assert KEY not in str(excinfo.value)

    async def test_a_timeout_after_connecting_is_stalled_and_a_connect_timeout_is_not(self, tmp_path):
        server = FakeServer(transcribe_timeout=True)
        with pytest.raises(TranscriptionError) as excinfo:
            await _client(_config(str(tmp_path)), server).transcribe(AUDIO, "a.ogg", model="auto")
        assert (excinfo.value.reason, excinfo.value.transient, excinfo.value.stalled) == ("ReadTimeout", True, True)
        # Sent once: a second attempt would wait out the same timeout and hand
        # the server the same work again.
        assert len(server.transcribe_requests) == 1

        def connect_timeout(request):
            raise httpx.ConnectTimeout(f"cannot reach {URL}")

        client = TranscriptionClient(_config(str(tmp_path)), transport=httpx.MockTransport(connect_timeout))
        client.backoffs = (0.0, 0.0)
        with pytest.raises(TranscriptionError) as excinfo:
            await client.transcribe(AUDIO, "a.ogg", model="auto")
        assert (excinfo.value.reason, excinfo.value.transient, excinfo.value.stalled) == ("ConnectTimeout", True, False)

    def test_bad_or_missing_url_means_not_configured(self, tmp_path):
        assert not TranscriptionClient(_config(str(tmp_path), transcription_url="")).configured
        assert not TranscriptionClient(_config(str(tmp_path), transcription_url="ftp://x")).configured
        assert not TranscriptionClient(MagicMock()).configured  # a bare mock reads truthy; the type check holds

    def test_a_language_is_stored_only_as_a_bcp47_tag(self):
        """Tags are kept, Whisper's English names map to their codes, "unknown" and the rest become NULL."""
        from src.transcription_contract import job_outcome

        cases = {
            "es": "es",
            "pt-BR": "pt-BR",
            "yue": "yue",
            "zh-Hant-TW": "zh-Hant-TW",
            "spanish": "es",
            "Haitian Creole": "ht",
            "javanese": "jv",
            "lao": "lo",
            "castilian": "es",
            "unknown": None,
            "klingon": None,
            "": None,
            "e": None,
            "es_ES": None,
            "1a": None,
            # Longer than media_transcripts.language (String(16)) holds.
            "en-abcdefgh-ijklmnop": None,
            None: None,
            7: None,
        }
        for value, want in cases.items():
            assert result_columns({"text": "x", "language": value}, model="auto")["language"] == want, value
            _status, columns = job_outcome({"status": "done", "text": "x", "language": value})
            assert columns["language"] == want, value

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
        # A list field that is not a list reads as empty instead of raising.
        hostile = result_columns({"text": "x", "words": 5, "segments": 7}, model="m")
        assert (hostile["words"], hostile["segments"]) == ([], [])

    async def test_a_stalled_get_is_still_retried(self, tmp_path):
        """Only a stalled POST is sent once; a GET carries no work and is retried."""

        calls = []

        def stall(request):
            calls.append(request)
            raise httpx.ReadTimeout(f"no answer from {URL}")

        client = TranscriptionClient(_config(str(tmp_path)), transport=httpx.MockTransport(stall))
        client.backoffs = (0.0, 0.0)
        with pytest.raises(TranscriptionError) as excinfo:
            await client.get_job("job_0001")
        assert (excinfo.value.transient, excinfo.value.stalled) == (True, True)
        assert len(calls) == 3


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
        # A 404 on /v1/server is not akou: the preset stays home and the
        # OpenAI model name goes out, or a server that validates it (speaches,
        # LocalAI) would answer 4xx for good.
        body = server.transcribe_requests[0].content.decode("latin-1")
        assert 'name="model"\r\n\r\nwhisper-1\r\n' in body
        assert 'name="model"\r\n\r\nauto\r\n' not in body
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
        assert row["models"] == ["whisper-1"]
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
        assert stats == _stats()
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
            # The server decodes the file inside the request, so a 5xx may be about
            # this file: a failed row, and the run ends (``stalled``).
            assert stats["stalled"] == 1, f"drain {drain}"
            rows = await _rows(real_adapter, "m_1_voice")
            assert [r["status"] for r in rows] == ["failed"] * drain
            assert rows[0]["error"] == "HTTP 500"
        sent_before = len(server.transcribe_requests)
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )
        assert stats == _stats()
        assert len(server.transcribe_requests) == sent_before
        assert len(await _rows(real_adapter, "m_1_voice")) == 3
        assert all(call.args[2]["status"] == "failed" for call in notifier.notify.await_args_list)

    async def test_the_drain_sends_the_priority_chats_first(self, real_adapter, tmp_path):
        """With room for one media, the one from TRANSCRIPTION_PRIORITY_CHAT_IDS goes, not the newest."""
        other = -100500600007
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 1))
        path = tmp_path / str(other) / "m_2_voice.ogg"
        path.parent.mkdir(parents=True)
        path.write_bytes(AUDIO)
        await real_adapter.upsert_chat({"id": other, "type": "group", "title": "fixture chat"}, account_id=1)
        await real_adapter.insert_message(
            {"id": 2, "chat_id": other, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}}, account_id=1
        )
        await real_adapter.insert_media(
            {
                "id": "m_2_voice",
                "message_id": 2,
                "chat_id": other,
                "type": "voice",
                "file_path": str(path),
                "downloaded": True,
                "duration": 12,
                "download_date": datetime(2026, 1, 9),
            },
            account_id=1,
        )
        server = FakeServer()
        config = _config(str(tmp_path), transcription_backfill_per_run=1, transcription_priority_chat_ids=[CHAT])

        assert (await _drain_all(config, real_adapter, server))["done"] == 1
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["done"]
        assert await _rows(real_adapter, "m_2_voice") == []

    async def test_media_over_the_limit_gets_a_skipped_row_and_no_request(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", duration=1801)
        await _media(real_adapter, tmp_path, "m_2_voice", duration=1800)
        server = FakeServer()
        config = _config(str(tmp_path))
        notifier = AsyncMock()

        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, server)
        )

        assert stats == _stats(done=1, skipped=1)
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

    async def test_an_unreadable_file_is_a_failed_row_and_the_run_goes_on(self, real_adapter, tmp_path):
        """A read error spends a failed row instead of aborting the drain, and an
        ask-now row is marked picked up (its preset) before the read is tried."""
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 2))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        asked = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True)
        assert asked["preset"] is None
        real_hash = __import__("src.transcription", fromlist=["_file_sha256"])._file_sha256

        def read(path):
            if "m_1_voice" in path:
                raise PermissionError("denied")
            return real_hash(path)

        server = FakeServer()
        config = _config(str(tmp_path))
        with patch("src.transcription._file_sha256", side_effect=read):
            stats = await drain_transcriptions(
                config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
            )
        assert (stats["failed"], stats["done"]) == (1, 1)
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["id"], row["status"], row["error"], row["preset"]) == (
            asked["id"],
            "failed",
            "file_unreadable",
            "auto",
        )
        assert [r["status"] for r in await _rows(real_adapter, "m_2_voice")] == ["done"]

    async def test_an_unreachable_server_writes_no_rows(self, real_adapter, tmp_path, caplog):
        await _media(real_adapter, tmp_path, "m_1_voice")

        def boom(request):
            raise httpx.ConnectError(f"cannot reach {URL}")

        config = _config(str(tmp_path))
        client = TranscriptionClient(config, transport=httpx.MockTransport(boom))
        with caplog.at_level(logging.DEBUG, logger="src.transcription"):
            stats = await drain_transcriptions(config, real_adapter, account_id=1, notifier=AsyncMock(), client=client)
        assert stats == _stats()
        assert await _rows(real_adapter, "m_1_voice") == []
        assert any("unreachable" in record.getMessage() for record in caplog.records)

    async def test_a_server_that_goes_down_mid_run_leaves_the_row_queued_and_ends_the_run(
        self, real_adapter, tmp_path, caplog
    ):
        """An outage is not an answer: no failed row, no retry budget spent, the rest waits."""
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 2))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        down = FakeServer(transcribe_down=True)
        config = _config(str(tmp_path))
        notifier = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="src.transcription"):
            stats = await drain_transcriptions(
                config, real_adapter, account_id=1, notifier=notifier, client=_client(config, down)
            )

        assert stats == _stats(unreachable=1)
        # Three attempts on the first media, then the run ends: the second
        # media was never tried and has no row at all.
        assert len(down.transcribe_requests) == 3
        rows = await _rows(real_adapter, "m_1_voice")
        assert [r["status"] for r in rows] == ["queued"]
        assert rows[0]["error"] is None
        assert await _rows(real_adapter, "m_2_voice") == []
        notifier.notify.assert_not_awaited()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "unreachable" in warnings[0].getMessage()

        # Three outages in a row spend nothing. The first media's row is
        # fresh, so those runs skip it and try the second media, which gets
        # its own queued row the same way; neither ever gets a failed row.
        for _ in range(3):
            await drain_transcriptions(
                config, real_adapter, account_id=1, notifier=notifier, client=_client(config, down)
            )
        first_row = rows[0]
        second_row = (await _rows(real_adapter, "m_2_voice"))[0]
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["queued"]
        assert [r["status"] for r in await _rows(real_adapter, "m_2_voice")] == ["queued"]

        # Once the rows are older than the stale window and the server is
        # back, the same rows are filled; no second row appears for either.
        from datetime import timedelta

        from sqlalchemy import update

        from src.db.models import MediaTranscript
        from src.message_utils import utcnow_naive

        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                update(MediaTranscript)
                .where(MediaTranscript.status == "queued")
                .values(requested_at=utcnow_naive() - STALE_QUEUED - timedelta(minutes=1))
            )
            await session.commit()
        up = FakeServer()
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=notifier, client=_client(config, up)
        )
        assert stats["done"] == 2
        assert [(r["id"], r["status"]) for r in await _rows(real_adapter, "m_1_voice")] == [(first_row["id"], "done")]
        assert [(r["id"], r["status"]) for r in await _rows(real_adapter, "m_2_voice")] == [(second_row["id"], "done")]

    async def test_a_server_that_never_answers_spends_a_failed_row_and_ends_the_run(self, real_adapter, tmp_path):
        """A read timeout reached the server: a failed row, so the cap of three ends a message it always times out on."""
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 2))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        slow = FakeServer(transcribe_timeout=True)
        config = _config(str(tmp_path))
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, slow)
        )
        assert stats == _stats(stalled=1)
        assert len(slow.transcribe_requests) == 1  # one media, one attempt, then the run ends
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"]) == ("failed", "ReadTimeout")
        assert await _rows(real_adapter, "m_2_voice") == []

        for _ in range(6):
            await drain_transcriptions(
                config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, slow)
            )
        for media_id in ("m_1_voice", "m_2_voice"):
            assert [r["status"] for r in await _rows(real_adapter, media_id)] == ["failed"] * 3
        sent = len(slow.transcribe_requests)
        await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, slow)
        )
        assert len(slow.transcribe_requests) == sent  # the cap holds: nothing is resent

    @pytest.mark.parametrize("status", [401, 403, 429])
    async def test_a_refusal_about_the_key_keeps_the_row_queued_and_ends_the_run(self, real_adapter, tmp_path, status):
        """A wrong key or a rate limit is not about the file: no failed row, and the fixed server finishes it."""
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 2))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        config = _config(str(tmp_path))
        for _ in range(4):  # more runs than the cap of three failed rows
            stats = await drain_transcriptions(
                config,
                real_adapter,
                account_id=1,
                notifier=AsyncMock(),
                client=_client(config, FakeServer(transcribe_status=status)),
            )
            assert stats == _stats(refused=1)
            await _make_stale(real_adapter)
        assert [(r["status"], r["error"]) for r in await _rows(real_adapter, "m_1_voice")] == [("queued", None)]
        assert await _rows(real_adapter, "m_2_voice") == []

        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )
        assert stats["done"] == 2
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["done"]

    async def test_akou_without_jobs_refusing_the_preset_keeps_the_row_queued(self, real_adapter, tmp_path):
        """akou in app mode answers the OpenAI route too, and 409 preset_unavailable is its configuration."""
        await _media(real_adapter, tmp_path, "m_1_voice")

        def akou_app(request):
            if request.url.path.endswith("/v1/server"):
                return httpx.Response(200, json={"name": "akou", "version": "0.3.0", "capabilities": {"jobs": False}})
            return httpx.Response(409, json={"error": "preset_unavailable", "message": "not built"})

        config = _config(str(tmp_path), transcription_preset="best")
        client = TranscriptionClient(config, transport=httpx.MockTransport(akou_app))
        stats = await drain_transcriptions(config, real_adapter, account_id=1, notifier=AsyncMock(), client=client)
        assert stats == _stats(refused=1)
        assert [(r["status"], r["error"]) for r in await _rows(real_adapter, "m_1_voice")] == [("queued", None)]

    async def test_a_5xx_spends_a_failed_row_and_ends_the_run(self, real_adapter, tmp_path):
        """Every later media would get the same 5xx and the same backoffs: one failed row, then the run ends."""
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 2))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        server = FakeServer(transcribe_status=502)
        config = _config(str(tmp_path))
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )
        assert stats == _stats(stalled=1)
        assert len(server.transcribe_requests) == 3  # a 5xx is retried, then the run ends
        assert [(r["status"], r["error"]) for r in await _rows(real_adapter, "m_1_voice")] == [("failed", "HTTP 502")]
        assert await _rows(real_adapter, "m_2_voice") == []

    async def test_the_server_row_is_written_only_when_the_server_names_itself(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path))
        await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )
        assert await real_adapter.get_transcription_server() is None

        akou = FakeServer(
            server_status=200, server_body={"name": "akou", "version": "0.2.0", "capabilities": {"jobs": False}}
        )
        await _media(real_adapter, tmp_path, "m_2_voice")
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, akou)
        )
        assert await real_adapter.get_transcription_server() == {"name": "akou", "version": "0.2.0"}
        # akou without jobs (app mode): the synchronous path is taken.
        assert stats["done"] == 1
        assert len(akou.transcribe_requests) == 1
        # akou reads the preset in ``model``; nobody else does.
        assert 'name="model"\r\n\r\nauto\r\n' in akou.transcribe_requests[0].content.decode("latin-1")
        row = (await _rows(real_adapter, "m_2_voice"))[0]
        assert row["engine_name"] == "akou"
        assert row["engine_version"] == "0.2.0"
        assert row["source"] == "openai"
        assert row["models"] == ["auto"]

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
            assert stats == _stats()
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
# The audio check before the upload (ffprobe)
# ============================================================================

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe not installed")

FIXED_ARGV = ["-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", "-i"]


def _ffmpeg(path, *inputs: str) -> None:
    """A real media file made by ffmpeg from its built-in test sources."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, str(path)], check=True, capture_output=True, timeout=60)


async def _file_media(adapter, path, media_id: str, *, media_type: str, mime_type=None, duration=None) -> dict:
    """A media row for a file already on disk, with the given type, mime type and stored duration."""
    row = {
        "id": media_id,
        "message_id": int(media_id.split("_")[1]),
        "chat_id": CHAT,
        "type": media_type,
        "file_path": str(path),
        "downloaded": True,
        "duration": duration,
        "mime_type": mime_type,
        "download_date": datetime(2026, 1, 2, 3, 4, 5),
    }
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "fixture chat"}, account_id=1)
    await adapter.insert_message(
        {"id": row["message_id"], "chat_id": CHAT, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
        account_id=1,
    )
    await adapter.insert_media(row, account_id=1)
    return row


def _fake_ffprobe(tmp_path, monkeypatch, script: str) -> None:
    """Put an ``ffprobe`` shell script first on PATH; ``script`` is its body."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    probe = bin_dir / "ffprobe"
    probe.write_text("#!/bin/sh\n" + script)
    probe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


async def _drain_all(config, adapter, server: FakeServer) -> dict:
    return await drain_transcriptions(
        config, adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
    )


class TestAudioCheck:
    async def test_no_audio_stream_stores_a_skipped_row_and_sends_nothing(self, real_adapter, tmp_path, monkeypatch):
        """Whatever the type says, ffprobe finding no audio stream ends it; the argument list is fixed."""
        argv = tmp_path / "argv.txt"
        _fake_ffprobe(
            tmp_path,
            monkeypatch,
            f'printf "%s\\n" "$@" >> "{argv}"\necho \'{{"streams": [{{"codec_type": "video"}}], "format": {{}}}}\'\n',
        )
        path = tmp_path / str(CHAT) / "clip.mkv"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not decoded by the fake")
        await _file_media(real_adapter, path, "m_1_document", media_type="document", mime_type="video/x-matroska")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"document"})

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(skipped=1)
        assert server.transcribe_requests == []
        [row] = await _rows(real_adapter, "m_1_document")
        assert (row["status"], row["error"], row["job_id"]) == ("skipped", "no_audio_track", None)
        assert argv.read_text().splitlines() == [*FIXED_ARGV, str(path)]
        # The skipped row ends the loop, the archive keeps it.
        await _drain_all(config, real_adapter, server)
        assert server.transcribe_requests == []
        assert len(await _rows(real_adapter, "m_1_document")) == 1

    async def test_an_answer_with_no_streams_list_is_unknown_not_silent(self, real_adapter, tmp_path, monkeypatch):
        """ffprobe saying nothing about the streams is not ffprobe finding no audio: the file is sent."""
        _fake_ffprobe(tmp_path, monkeypatch, 'echo \'{"format": {"duration": "5.0"}}\'\n')
        monkeypatch.setattr("src.transcription._warned", set())
        path = tmp_path / str(CHAT) / "clip.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(AUDIO)
        await _file_media(real_adapter, path, "m_1_video", media_type="video", mime_type="video/mp4")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video"})

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=1)
        assert len(server.transcribe_requests) == 1

    async def test_a_voice_message_with_a_stored_duration_skips_ffprobe(self, real_adapter, tmp_path, monkeypatch):
        calls = tmp_path / "ffprobe-calls"
        _fake_ffprobe(
            tmp_path, monkeypatch, f'echo "$@" >> "{calls}"\n' + 'echo \'{"streams": [{"codec_type": "audio"}]}\'\n'
        )
        await _media(real_adapter, tmp_path, "m_1_voice", duration=12)
        await _media(real_adapter, tmp_path, "m_2_voice", duration=None)
        server = FakeServer()

        stats = await _drain_all(_config(str(tmp_path)), real_adapter, server)

        assert stats == _stats(done=2)
        probed = calls.read_text().splitlines()
        assert len(probed) == 1 and probed[0].endswith("m_2_voice.ogg")

    async def test_ffprobes_duration_feeds_the_limit_when_the_row_has_none(self, real_adapter, tmp_path, monkeypatch):
        _fake_ffprobe(
            tmp_path,
            monkeypatch,
            'echo \'{"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "95.5"}}\'\n',
        )
        for media_id, duration in (("m_1_video", None), ("m_2_video", 60)):
            path = tmp_path / str(CHAT) / f"{media_id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(AUDIO)
            await _file_media(
                real_adapter, path, media_id, media_type="video", mime_type="video/mp4", duration=duration
            )
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video"}, transcription_max_seconds=90)

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=1, skipped=1)
        [skipped] = await _rows(real_adapter, "m_1_video")
        assert (skipped["status"], skipped["error"]) == ("skipped", "longer than the 90 second limit")
        assert skipped["duration_s"] == 95.5
        # A stored duration wins over ffprobe's: 60 s is under the limit and is sent.
        assert (await _rows(real_adapter, "m_2_video"))[0]["status"] == "done"
        assert len(server.transcribe_requests) == 1

    async def test_a_missing_ffprobe_sends_the_file_anyway_and_warns_once(
        self, real_adapter, tmp_path, monkeypatch, caplog
    ):
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.setattr("src.transcription._warned", set())
        # No stored duration, so ffprobe is asked (a voice message with one skips it).
        await _media(real_adapter, tmp_path, "m_1_voice", duration=None)
        await _media(real_adapter, tmp_path, "m_2_voice", duration=None)
        server = FakeServer()
        config = _config(str(tmp_path))

        with caplog.at_level(logging.DEBUG, logger="src.transcription"):
            stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=2)
        assert len(server.transcribe_requests) == 2
        probe_lines = [r for r in caplog.records if "ffprobe" in r.getMessage()]
        assert [r.levelno for r in probe_lines] == [logging.WARNING, logging.DEBUG]
        assert "not installed" in probe_lines[0].getMessage()
        assert not any(str(tmp_path) in r.getMessage() for r in caplog.records), "no path in any log line"

    async def test_an_ffprobe_that_hangs_is_cut_off_and_the_file_is_sent(self, real_adapter, tmp_path, monkeypatch):
        _fake_ffprobe(tmp_path, monkeypatch, "sleep 20\n")
        monkeypatch.setattr("src.transcription.FFPROBE_TIMEOUT_SECONDS", 0.5)
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = FakeServer()
        config = _config(str(tmp_path))

        started = asyncio.get_running_loop().time()
        stats = await _drain_all(config, real_adapter, server)

        assert asyncio.get_running_loop().time() - started < 10
        assert stats == _stats(done=1)
        assert len(server.transcribe_requests) == 1

    @needs_ffmpeg
    async def test_real_files_a_silent_video_is_skipped_and_a_real_audio_file_is_sent(self, real_adapter, tmp_path):
        work = tmp_path / str(CHAT)
        silent = work / "silent.mp4"
        tone = work / "tone.wav"
        _ffmpeg(silent, "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10", "-an", "-c:v", "mpeg4")
        _ffmpeg(tone, "-f", "lavfi", "-i", "sine=frequency=440:duration=1")
        await _file_media(real_adapter, silent, "m_1_video", media_type="video", mime_type="video/mp4", duration=1)
        await _file_media(real_adapter, silent, "m_2_document", media_type="document", mime_type="video/mp4")
        await _file_media(real_adapter, tone, "m_3_document", media_type="document", mime_type="audio/x-wav")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video", "document"})

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=1, skipped=2)
        assert len(server.transcribe_requests) == 1
        # A file sent as a document goes out as its extracted audio track.
        assert 'filename="tone.ogg"' in server.transcribe_requests[0].content.decode("latin-1")
        for media_id in ("m_1_video", "m_2_document"):
            [row] = await _rows(real_adapter, media_id)
            assert (row["status"], row["error"]) == ("skipped", "no_audio_track")
        assert (await _rows(real_adapter, "m_3_document"))[0]["status"] == "done"

    @needs_ffmpeg
    async def test_real_files_ffprobes_duration_skips_a_long_audio_document(self, real_adapter, tmp_path):
        tone = tmp_path / str(CHAT) / "long.wav"
        _ffmpeg(tone, "-f", "lavfi", "-i", "sine=frequency=440:duration=3")
        await _file_media(real_adapter, tone, "m_1_document", media_type="document", mime_type="audio/wav")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"document"}, transcription_max_seconds=2)

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(skipped=1)
        assert server.transcribe_requests == []
        [row] = await _rows(real_adapter, "m_1_document")
        assert (row["status"], row["error"]) == ("skipped", "longer than the 2 second limit")
        assert row["duration_s"] == pytest.approx(3.0, abs=0.1)


# ============================================================================
# The akou job path (slice 4)
# ============================================================================

AKOU_INFO = {"name": "akou", "version": "0.3.0", "capabilities": {"jobs": True, "webhooks": True, "events": True}}
CALLBACK = "https://archive.example.test/api/transcriptions/callback"
# Every field SV-J1 documents; akou refuses anything else with 400.
JOB_FIELDS = {"file", "preset", "language", "keywords[]", "diarize", "callback_url", "metadata"}
_PART = re.compile(r'name="([^"]+)"(?:; filename="[^"]*")?\r\n(?:Content-Type: [^\r]*\r\n)?\r\n(.*?)\r\n--', re.DOTALL)


def _akou_result(job_id: str, content_hash: str, text: str = "hola desde akou") -> dict:
    """The flat result of SV-J4, as the result route and the webhook data carry it."""
    return {
        "job_id": job_id,
        "status": "done",
        "text": text,
        "language": "es",
        "language_confidence": 0.97,
        "duration_s": 4.2,
        "words": [{"w": "hola", "s": 0.31, "e": 0.62, "c": 0.94}],
        "segments": [{"s": 0.31, "e": 3.9, "text": text, "speaker": None}],
        "confidence": 0.93,
        "engine": {"name": "parakeet", "version": "1", "preset": "fast", "models": ["parakeet-test-model"]},
        "metadata": {"content_hash": content_hash},
    }


class AkouServer(FakeServer):
    """``FakeServer`` playing akou: the server route, jobs, the result route and the event feed.

    Jobs are keyed by ``Idempotency-Key`` as SV-J2 says: the same key and
    the same file answer 200 with the existing job in whatever state it is,
    the same key with another file answers 422 ``idempotency_conflict``. A
    ``callback_url`` whose host is not in ``callback_hosts`` answers 422
    ``callback_not_allowed``. Unknown fields and any query parameter on
    ``POST /v1/jobs`` answer 400, as every /v1 route does. Finishing a job
    appends to the event feed; ``page_size`` pages it.
    """

    def __init__(self, *, callback_hosts=("archive.example.test",), page_size: int = 50, retain_days=None):
        info = dict(AKOU_INFO)
        if retain_days is not None:
            info["retain_days"] = retain_days
        super().__init__(server_status=200, server_body=info)
        self.callback_hosts = set(callback_hosts)
        self.page_size = page_size
        self.jobs: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.events: list[dict] = []
        self.issued = 0  # job ids are never reused, even after a job is deleted
        self.nest_result = True  # a done job answer carries its result under "result"
        self.submit_timeout = False  # POST /v1/jobs connects, then the answer never comes
        self.submit_refusal: tuple[int, dict] | None = None  # (status, body) for every POST /v1/jobs
        self.feed_refusal: tuple[int, dict] | None = None  # (status, body) for every GET /v1/events

    # -- what the tests drive -------------------------------------------------

    def add_job(self, content_hash: str, status: str = "running", audio: bytes = AUDIO, key: str | None = None) -> str:
        """A job for ``content_hash``, found again by ``key`` (the hash unless the submit sent another)."""
        self.issued += 1
        job_id = f"job_{self.issued:04d}"
        self.jobs[job_id] = {
            "status": status,
            "hash": content_hash,
            "file_sha": hashlib.sha256(audio).hexdigest(),
            "error": None,
        }
        self.by_key[key or content_hash] = job_id
        return job_id

    def finish(self, job_id: str, *, text: str = "hola desde akou", emit: bool = True) -> None:
        job = self.jobs[job_id]
        job["status"] = "done"
        job["text"] = text
        if emit:
            self.add_event("transcription.completed", _akou_result(job_id, job["hash"], text))

    def fail(self, job_id: str, code: str = "decode_failed") -> None:
        job = self.jobs[job_id]
        job["status"] = "failed"
        job["error"] = {"code": code, "message": "server text that is never stored"}
        self.add_event(
            "transcription.failed",
            {"job_id": job_id, "status": "failed", "error": job["error"], "metadata": {"content_hash": job["hash"]}},
        )

    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job["status"] = "cancelled"
        self.add_event(
            "transcription.cancelled",
            {"job_id": job_id, "status": "cancelled", "metadata": {"content_hash": job["hash"]}},
        )

    def add_event(self, event_type: str, data: dict) -> None:
        self.events.append(
            {
                # akou's feed event (src/main/server/jobs.ts eventView): a string id,
                # the integer cursor, and the job id beside the data.
                "id": f"msg_{len(self.events) + 1:04d}",
                "cursor": len(self.events) + 1,
                "type": event_type,
                "job_id": data.get("job_id") if isinstance(data, dict) else None,
                "timestamp": "2026-01-02T03:04:05Z",
                "data": data,
            }
        )

    # -- requests by kind -----------------------------------------------------

    def _paths(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    @property
    def submits(self) -> list[httpx.Request]:
        return self._paths("POST", "/v1/jobs")

    @property
    def event_reads(self) -> list[httpx.Request]:
        return self._paths("GET", "/v1/events")

    @property
    def job_reads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET" and "/v1/jobs/" in r.url.path]

    # -- the routes -----------------------------------------------------------

    def _job_answer(self, job_id: str) -> dict:
        job = self.jobs[job_id]
        answer = {
            "id": job_id,
            "status": job["status"],
            "created_at": "2026-01-02T03:04:05Z",
            "links": {"self": f"/v1/jobs/{job_id}", "result": f"/v1/jobs/{job_id}/result"},
        }
        if job["status"] == "done" and self.nest_result:
            result = _akou_result(job_id, job["hash"], job["text"])
            answer["result"] = {k: v for k, v in result.items() if k not in ("job_id", "status", "metadata")}
        if job["error"]:
            answer["error"] = job["error"]
        return answer

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/v1/jobs"):
            self.requests.append(request)
            if self.submit_timeout:
                raise httpx.ReadTimeout(f"no answer from {URL}")
            if self.submit_refusal:
                return httpx.Response(self.submit_refusal[0], json=self.submit_refusal[1])
            return self._submit(request)
        if request.method == "GET" and path.endswith("/v1/events"):
            self.requests.append(request)
            if self.feed_refusal:
                return httpx.Response(self.feed_refusal[0], json=self.feed_refusal[1])
            unknown = set(request.url.params) - {"after", "wait"}
            if unknown:
                return httpx.Response(400, json={"error": "unknown_parameter"})
            raw_after = request.url.params.get("after", "0")
            if not raw_after.isdigit():
                # akou reads `after` as a non-negative integer and refuses anything else.
                return httpx.Response(400, json={"error": "bad_param", "message": "after is an integer"})
            after = int(raw_after)
            page = self.events[after : after + self.page_size]
            cursor = page[-1]["cursor"] if page else after
            return httpx.Response(
                200,
                json={"events": page, "cursor": cursor, "has_more": cursor < len(self.events)},
            )
        match = re.search(r"/v1/jobs/([^/]+)(/result)?$", path)
        if request.method == "GET" and match:
            self.requests.append(request)
            job_id = match.group(1)
            if job_id not in self.jobs:
                return httpx.Response(404, json={"error": "not_found", "message": "no such job"})
            job = self.jobs[job_id]
            if match.group(2):
                if job["status"] != "done":
                    return httpx.Response(409, json={"error": "not_done", "message": "not finished"})
                return httpx.Response(200, json=_akou_result(job_id, job["hash"], job["text"]))
            return httpx.Response(200, json=self._job_answer(job_id))
        return super()._handle(request)

    def _submit(self, request: httpx.Request) -> httpx.Response:
        if request.url.query:
            return httpx.Response(400, json={"error": "unknown_parameter", "message": "no query on this route"})
        fields = dict(_PART.findall(request.content.decode("latin-1")))
        if set(fields) - JOB_FIELDS:
            return httpx.Response(400, json={"error": "unknown_field", "message": "refused"})
        callback = fields.get("callback_url")
        if callback and urllib.parse.urlsplit(callback).hostname not in self.callback_hosts:
            return httpx.Response(422, json={"error": "callback_not_allowed", "message": "host not allowed"})
        key = request.headers["idempotency-key"]
        file_sha = hashlib.sha256(fields["file"].encode("latin-1")).hexdigest()
        if key in self.by_key:
            job_id = self.by_key[key]
            if self.jobs[job_id]["file_sha"] != file_sha:
                return httpx.Response(422, json={"error": "idempotency_conflict", "message": "another file"})
            return httpx.Response(200, json=self._job_answer(job_id))
        job_id = self.add_job(json.loads(fields["metadata"])["content_hash"], status="queued", key=key)
        return httpx.Response(202, json=self._job_answer(job_id))


async def _backdate(adapter, **delta) -> None:
    """Move every open job row's request time and job time back by ``delta``."""
    from datetime import timedelta

    from sqlalchemy import update

    from src.db.models import MediaTranscript
    from src.message_utils import utcnow_naive

    when = utcnow_naive() - timedelta(**delta)
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            update(MediaTranscript)
            .where(MediaTranscript.status.in_(("queued", "running")), MediaTranscript.job_id.is_not(None))
            .values(requested_at=when, job_stored_at=when)
        )
        await session.commit()


def _akou_config(tmp_path, **overrides) -> SimpleNamespace:
    return _config(str(tmp_path), **{"transcription_callback_url": "", **overrides})


async def _akou_drain(config, adapter, server: AkouServer, notifier=None) -> dict:
    return await drain_transcriptions(
        config, adapter, account_id=1, notifier=notifier or AsyncMock(), client=_client(config, server)
    )


SHA = hashlib.sha256(AUDIO).hexdigest()


class TestJobPath:
    async def test_submit_sends_the_documented_fields_and_stores_the_job_id(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        config = _akou_config(tmp_path, transcription_callback_url=CALLBACK, transcription_preset="fast")
        stats = await _akou_drain(config, real_adapter, server)
        assert stats == _stats(submitted=1)
        assert server.transcribe_requests == []  # the job path, never the OpenAI route
        request = server.submits[0]
        assert request.url.query == b""  # never ``wait``: akou refuses unknown parameters
        assert request.headers["idempotency-key"] == SHA
        assert request.headers["authorization"] == f"Bearer {KEY}"
        fields = dict(_PART.findall(request.content.decode("latin-1")))
        assert fields["preset"] == "fast"
        assert fields["language"] == "auto"
        assert fields["callback_url"] == CALLBACK
        assert json.loads(fields["metadata"]) == {"content_hash": SHA}
        assert fields["file"] == AUDIO.decode("latin-1")
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["job_id"], row["source"], row["idempotency_key"]) == (
            "queued",
            "job_0001",
            "akou",
            SHA,
        )

        # No callback URL configured: the field is not sent at all.
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="d" * 64)
        await _akou_drain(_akou_config(tmp_path), real_adapter, server)
        fields = dict(_PART.findall(server.submits[-1].content.decode("latin-1")))
        assert "callback_url" not in fields
        assert server.submits[-1].headers["idempotency-key"] == "d" * 64

    async def test_a_200_with_the_existing_running_job_stores_that_job(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        job_id = server.add_job(SHA, status="running")
        stats = await _akou_drain(_akou_config(tmp_path), real_adapter, server)
        assert stats == _stats(submitted=1)
        assert len(server.jobs) == 1  # no second job for the same audio
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["job_id"]) == ("running", job_id)

    async def test_an_answer_already_done_is_stored_at_once(self, real_adapter, tmp_path):
        """From the nested ``result``, or from the result route when the answer carries none."""
        for nested, media_id in ((True, "m_1_voice"), (False, "m_2_voice")):
            audio_hash = "e" * 64 if nested else "f" * 64
            await _media(real_adapter, tmp_path, media_id, content_hash=audio_hash)
            server = AkouServer()
            server.nest_result = nested
            job_id = server.add_job(audio_hash, status="queued")
            server.finish(job_id, text=f"ya estaba hecho {media_id}", emit=False)
            notifier = AsyncMock()
            stats = await _akou_drain(_akou_config(tmp_path), real_adapter, server, notifier)
            assert stats["done"] == 1
            [row] = await _rows(real_adapter, media_id)
            assert row["status"] == "done"
            assert row["job_id"] == job_id
            assert row["text"] == f"ya estaba hecho {media_id}"
            assert row["language"] == "es"
            assert row["language_confidence"] == 0.97
            assert row["confidence"] == 0.93
            assert row["duration_s"] == 4.2
            assert row["words"] == [{"w": "hola", "s": 0.31, "e": 0.62, "c": 0.94}]
            assert row["segments"][0]["text"] == f"ya estaba hecho {media_id}"
            assert row["models"] == ["parakeet-test-model"]
            assert (row["source"], row["engine_name"], row["engine_version"]) == ("akou", "akou", "0.3.0")
            result_reads = [r for r in server.job_reads if r.url.path.endswith("/result")]
            assert len(result_reads) == (0 if nested else 1)
            notifier.notify.assert_awaited_once()
            assert notifier.notify.await_args.args[2]["status"] == "done"

    async def test_callback_not_allowed_keeps_the_row_queued_warns_once_and_ends_the_run(
        self, real_adapter, tmp_path, caplog
    ):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64, download_date=datetime(2026, 1, 3))
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64, download_date=datetime(2026, 1, 2))
        server = AkouServer(callback_hosts=())
        config = _akou_config(tmp_path, transcription_callback_url=CALLBACK)
        with caplog.at_level(logging.WARNING, logger="src.transcription"):
            stats = await _akou_drain(config, real_adapter, server)
        assert stats["refused"] == 1
        assert len(server.submits) == 1  # the second media was never sent
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"]) == ("queued", None)  # the config, not the file: no failed row
        assert await _rows(real_adapter, "m_2_voice") == []
        assert sum("callback_not_allowed" in r.getMessage() for r in caplog.records) == 1
        assert all(CALLBACK not in r.getMessage() for r in caplog.records)

    async def test_a_submit_that_times_out_stays_queued_for_the_same_key(self, real_adapter, tmp_path):
        """akou may have made the job: the resubmit with the same key finds it, so no failed row."""
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        server.submit_timeout = True
        stats = await _akou_drain(_akou_config(tmp_path), real_adapter, server)
        assert stats == _stats(unreachable=1)
        assert len(server.submits) == 1  # never re-sent in the same run
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["job_id"], row["error"]) == ("queued", None, None)

    async def test_diarize_is_sent_on_the_job_path_only_when_asked(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64)
        server = AkouServer(page_size=1)
        await _akou_drain(_akou_config(tmp_path, transcription_backfill_per_run=1), real_adapter, server)
        await _akou_drain(
            _akou_config(tmp_path, transcription_diarize=True, transcription_backfill_per_run=2), real_adapter, server
        )
        fields = [dict(_PART.findall(r.content.decode("latin-1"))) for r in server.submits]
        assert [f.get("diarize") for f in fields] == [None, "true"]

    async def test_the_synchronous_path_never_diarizes(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_diarize=True)
        assert (await _drain_all(config, real_adapter, server))["done"] == 1
        assert "diarize" not in dict(_PART.findall(server.transcribe_requests[0].content.decode("latin-1")))

    async def test_idempotency_conflict_fails_the_row(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="9" * 64)
        server = AkouServer()
        server.add_job("9" * 64, audio=b"some other file")
        stats = await _akou_drain(_akou_config(tmp_path), real_adapter, server)
        assert stats["failed"] == 1
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"], row["job_id"]) == ("failed", "idempotency_conflict", None)

    async def test_the_event_feed_fills_a_row_no_callback_reached(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        [row] = await _rows(real_adapter, "m_1_voice")
        server.finish(row["job_id"], text="llegó por el feed")

        notifier = AsyncMock()
        stats = await _akou_drain(config, real_adapter, server, notifier)
        assert stats["reconciled"] == 1
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["text"]) == ("done", "llegó por el feed")
        assert server.job_reads == []  # the feed was enough; the row is not a straggler yet
        # The first read ever has no cursor; each later one starts at the stored cursor.
        assert [r.url.params.get("after") for r in server.event_reads] == [None, "0", "1"]
        assert await real_adapter.get_transcription_events_cursor() == "1"
        notifier.notify.assert_awaited_once()
        assert notifier.notify.await_args.args[2]["media_id"] == "m_1_voice"

        # The next run reads after the stored cursor and writes nothing.
        stats = await _akou_drain(config, real_adapter, server)
        assert server.event_reads[-1].url.params["after"] == "1"
        assert stats["reconciled"] == 0

    async def test_failed_and_cancelled_events_store_their_reason(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="a" * 64)
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="b" * 64)
        server = AkouServer()
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        server.fail(server.by_key["a" * 64])
        server.cancel(server.by_key["b" * 64])
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["reconciled"] == 2
        failed = (await _rows(real_adapter, "m_1_voice"))[-1]
        cancelled = (await _rows(real_adapter, "m_2_voice"))[-1]
        assert (failed["status"], failed["error"]) == ("failed", "decode_failed")
        assert (cancelled["status"], cancelled["error"]) == ("failed", "cancelled")

    async def test_unknown_event_types_are_skipped_and_the_cursor_moves_past_them(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer(page_size=2)
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        job_id = server.by_key[SHA]
        # Shaped like a finished result, so only its type keeps it out of the row.
        server.add_event(
            "transcription.previewed",
            {"job_id": job_id, "status": "done", "text": "not a transcript", "metadata": {"content_hash": SHA}},
        )
        server.add_event("transcription.progress", {"job_id": job_id, "metadata": {"content_hash": SHA}})
        server.add_event("transcription.completed", "not an object")
        server.finish(job_id, text="después de tres desconocidos")
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["reconciled"] == 1
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["text"]) == ("done", "después de tres desconocidos")
        assert await real_adapter.get_transcription_events_cursor() == "4"
        assert [r.url.params.get("after") for r in server.event_reads[-3:]] == ["0", "2", "4"]

    async def test_a_scrubbed_event_is_skipped_without_a_fetch_and_the_cursor_moves_past_it(
        self, real_adapter, tmp_path
    ):
        """SV-J6: after a delete or the retention window akou keeps only the job id and the final state."""
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        job_id = server.by_key[SHA]
        server.jobs[job_id]["status"] = "done"
        server.add_event("transcription.completed", {"job_id": job_id, "status": "done", "deleted": True})
        reads = len(server.job_reads)

        stats = await _akou_drain(config, real_adapter, server)

        assert stats["reconciled"] == 0
        assert server.job_reads[reads:] == [], "no result fetch for a scrubbed event"
        assert await real_adapter.get_transcription_events_cursor() == "1"
        [row] = await _rows(real_adapter, "m_1_voice")
        assert row["status"] in ("queued", "running")
        assert row["text"] is None

    async def test_the_straggler_poll_finishes_a_job_the_feed_never_reported(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64)
        server = AkouServer()
        server.nest_result = False
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        server.finish(server.by_key["1" * 64], text="por sondeo", emit=False)
        server.jobs[server.by_key["2" * 64]]["status"] = "running"

        # Younger than ten minutes: not polled yet.
        await _akou_drain(config, real_adapter, server)
        assert server.job_reads == []

        await _backdate(real_adapter, minutes=11)
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["polled"] == 1
        done = (await _rows(real_adapter, "m_1_voice"))[0]
        assert (done["status"], done["text"]) == ("done", "por sondeo")
        running = (await _rows(real_adapter, "m_2_voice"))[0]
        assert running["status"] == "running"
        done_job, running_job = server.by_key["1" * 64], server.by_key["2" * 64]
        paths = sorted(r.url.path.rsplit("/v1/", 1)[1] for r in server.job_reads)
        assert paths == sorted([f"jobs/{done_job}", f"jobs/{done_job}/result", f"jobs/{running_job}"])

    async def test_a_row_past_retention_expires_and_is_resubmitted(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer(retain_days=2)
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        [first] = await _rows(real_adapter, "m_1_voice")
        # akou deleted the job and its key after its retention window.
        del server.jobs[first["job_id"]]
        del server.by_key[SHA]
        await _backdate(real_adapter, days=2, minutes=1)

        stats = await _akou_drain(config, real_adapter, server)
        assert server.job_reads == []  # an expired row costs no request
        newest, expired = await _rows(real_adapter, "m_1_voice")
        assert (expired["id"], expired["status"], expired["error"]) == (first["id"], "failed", "expired")
        assert (newest["status"], newest["job_id"]) == ("queued", "job_0002")
        assert stats["submitted"] == 1

    async def test_expiry_counts_from_the_job_not_from_the_insert(self, real_adapter, tmp_path):
        """A row that waited queued through an outage longer than the retention is not
        expired the moment it gets its job: the poll asks akou and finishes it."""
        from datetime import timedelta

        from sqlalchemy import update

        from src.db.models import MediaTranscript
        from src.message_utils import utcnow_naive

        await _media(real_adapter, tmp_path, "m_1_voice")
        asked_long_ago = utcnow_naive() - timedelta(days=9)
        await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, idempotency_key=SHA, preset="auto")
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(update(MediaTranscript).values(requested_at=asked_long_ago))
            await session.commit()
        server = AkouServer(retain_days=2)
        server.nest_result = False
        config = _akou_config(tmp_path)
        assert (await _akou_drain(config, real_adapter, server))["submitted"] == 1
        [row] = await _rows(real_adapter, "m_1_voice")
        assert row["requested_at"] == asked_long_ago  # the insert time is never rewritten
        assert row["job_stored_at"] > asked_long_ago + timedelta(days=8)
        await _akou_drain(config, real_adapter, server)
        assert server.job_reads == []  # submitted a moment ago: not a straggler yet

        # Eleven minutes after the submit the row is a straggler, nine days after its insert.
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(update(MediaTranscript).values(job_stored_at=utcnow_naive() - timedelta(minutes=11)))
            await session.commit()
        server.finish(row["job_id"], text="llegó tarde", emit=False)
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["polled"] == 1
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"], row["text"]) == ("done", None, "llegó tarde")

    async def test_a_resubmit_after_expiry_never_collides_with_the_expired_row(self, real_adapter, tmp_path):
        """Even an akou that answers the expired row's job again, for the new key, cannot make
        the drain write that job id a second time for the same media."""
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer(retain_days=2)
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        [first] = await _rows(real_adapter, "m_1_voice")
        server.jobs[first["job_id"]]["status"] = "running"
        server.by_key[f"{SHA}.1"] = first["job_id"]  # the worst case: the same live job for the retry key
        await _backdate(real_adapter, days=2, minutes=1)

        stats = await _akou_drain(config, real_adapter, server)  # expires the row, resubmits
        assert stats["submitted"] == 1
        newest, expired = await _rows(real_adapter, "m_1_voice")
        assert (expired["status"], expired["error"], expired["job_id"]) == ("failed", "expired", first["job_id"])
        assert (newest["status"], newest["job_id"]) == ("queued", None)
        assert [r.headers["idempotency-key"] for r in server.submits] == [SHA, f"{SHA}.1"]

    async def test_two_media_with_the_same_audio_share_one_job_and_both_fill(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        await _media(real_adapter, tmp_path, "m_2_voice")
        server = AkouServer()
        config = _akou_config(tmp_path)
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["submitted"] == 2
        assert list(server.jobs) == ["job_0001"]  # the second submit got the first job back
        first = (await _rows(real_adapter, "m_1_voice"))[0]
        second = (await _rows(real_adapter, "m_2_voice"))[0]
        assert first["job_id"] == second["job_id"] == "job_0001"

        server.finish("job_0001", text="un audio, dos mensajes")
        notifier = AsyncMock()
        stats = await _akou_drain(config, real_adapter, server, notifier)
        assert stats["reconciled"] == 2
        for media_id in ("m_1_voice", "m_2_voice"):
            [row] = await _rows(real_adapter, media_id)
            assert (row["status"], row["text"]) == ("done", "un audio, dos mensajes")
        assert sorted(c.args[2]["media_id"] for c in notifier.notify.await_args_list) == ["m_1_voice", "m_2_voice"]

    async def test_a_twin_row_never_submitted_fills_from_the_same_event(self, real_adapter, tmp_path):
        """The per-run cap sent one media; the twin's queued row, with no job id yet, fills from its job."""
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 3))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 2))
        server = AkouServer()
        config = _akou_config(tmp_path, transcription_backfill_per_run=1)
        assert (await _akou_drain(config, real_adapter, server))["submitted"] == 1
        twin = await real_adapter.enqueue_media_transcript(
            "m_2_voice", account_id=1, idempotency_key=SHA, preset="auto"
        )
        assert (twin["status"], twin["job_id"]) == ("queued", None)

        server.finish("job_0001", text="un envío, dos filas")
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["reconciled"] == 2
        for media_id in ("m_1_voice", "m_2_voice"):
            [row] = await _rows(real_adapter, media_id)
            assert (row["status"], row["text"], row["job_id"]) == ("done", "un envío, dos filas", "job_0001")
        assert len(server.submits) == 1

    async def test_a_retry_after_a_cancelled_job_gets_a_new_job(self, real_adapter, tmp_path):
        """akou answers the same job for the same key in any state, so a retry sends ``<sha256>.<n>``."""
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        server.cancel("job_0001")

        stats = await _akou_drain(config, real_adapter, server)
        assert (stats["reconciled"], stats["submitted"]) == (1, 1)
        assert list(server.jobs) == ["job_0001", "job_0002"]
        assert [r.headers["idempotency-key"] for r in server.submits] == [SHA, f"{SHA}.1"]
        fields = dict(_PART.findall(server.submits[-1].content.decode("latin-1")))
        assert json.loads(fields["metadata"]) == {"content_hash": SHA}
        newest, cancelled = await _rows(real_adapter, "m_1_voice")
        assert (cancelled["status"], cancelled["error"], cancelled["job_id"]) == ("failed", "cancelled", "job_0001")
        assert (newest["status"], newest["job_id"], newest["idempotency_key"]) == ("queued", "job_0002", SHA)

        # The new job's event carries the bare hash and fills the new row.
        server.finish("job_0002", text="segundo intento")
        await _akou_drain(config, real_adapter, server)
        newest = (await _rows(real_adapter, "m_1_voice"))[0]
        assert (newest["status"], newest["text"]) == ("done", "segundo intento")

    async def test_the_cursor_moves_only_after_the_page_is_stored(self, real_adapter, tmp_path):
        """A crash between two fills of one page re-reads the page; nothing is skipped or written twice."""
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64)
        server = AkouServer()
        config = _akou_config(tmp_path)
        await _akou_drain(config, real_adapter, server)
        before = await real_adapter.get_transcription_events_cursor()
        server.finish(server.by_key["1" * 64], text="primera")
        server.finish(server.by_key["2" * 64], text="segunda")

        real_fill = real_adapter.fill_open_transcripts_by_key
        calls = 0

        async def second_fill_crashes(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("the database went away")
            return await real_fill(*args, **kwargs)

        with (
            patch.object(real_adapter, "fill_open_transcripts_by_key", new=second_fill_crashes),
            pytest.raises(RuntimeError),
        ):
            await _akou_drain(config, real_adapter, server)
        assert await real_adapter.get_transcription_events_cursor() == before
        assert (await _rows(real_adapter, "m_2_voice"))[0]["status"] == "queued"

        stats = await _akou_drain(config, real_adapter, server)
        assert stats["reconciled"] == 1  # the first event was stored already and writes nothing again
        for media_id, text in (("m_1_voice", "primera"), ("m_2_voice", "segunda")):
            [row] = await _rows(real_adapter, media_id)
            assert (row["status"], row["text"]) == ("done", text)
        assert await real_adapter.get_transcription_events_cursor() == "2"

    @pytest.mark.parametrize(
        ("status", "body", "attempts"),
        [
            (401, {"error": "unauthorized", "message": "a valid bearer token is required"}, 1),
            (403, {"error": "forbidden", "message": "refused"}, 1),
            (409, {"error": "preset_unavailable", "message": "the speech models are not downloaded"}, 1),
            (422, {"error": "callback_not_allowed", "message": "host not allowed"}, 1),
            (429, {"error": "rate_limited", "message": "slow down"}, 3),
            (502, {}, 3),
            (503, {"error": "quitting", "message": "akou is quitting"}, 3),
        ],
    )
    async def test_a_refusal_not_about_the_file_spends_no_failed_row(
        self, real_adapter, tmp_path, status, body, attempts
    ):
        """A wrong key, a preset akou cannot run yet, a callback host, a rate limit or a 5xx: the row
        stays queued however many runs it lasts, the run ends at the first media, and the fixed
        server gets every media."""
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64, download_date=datetime(2026, 1, 3))
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64, download_date=datetime(2026, 1, 2))
        server = AkouServer()
        server.submit_refusal = (status, body)
        config = _akou_config(tmp_path)
        for run in range(4):  # more runs than the cap of three failed rows
            stats = await _akou_drain(config, real_adapter, server)
            assert stats == _stats(refused=1), f"run {run}"
            assert len(server.submits) == attempts * (run + 1)
            await _make_stale(real_adapter)
        assert [(r["status"], r["error"]) for r in await _rows(real_adapter, "m_1_voice")] == [("queued", None)]
        assert await _rows(real_adapter, "m_2_voice") == []

        server.submit_refusal = None
        stats = await _akou_drain(config, real_adapter, server)
        assert stats["submitted"] == 2
        for media_id in ("m_1_voice", "m_2_voice"):
            assert [r["status"] for r in await _rows(real_adapter, media_id)] == ["queued"]
            assert (await _rows(real_adapter, media_id))[0]["job_id"] is not None

    @pytest.mark.parametrize("status", [401, 502])
    async def test_a_refused_event_feed_ends_the_run_before_any_submit(self, real_adapter, tmp_path, status):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        server.feed_refusal = (status, {"error": "unauthorized"} if status == 401 else {})
        stats = await _akou_drain(_akou_config(tmp_path), real_adapter, server)
        assert stats == _stats()
        assert server.submits == []
        assert await _rows(real_adapter, "m_1_voice") == []

    async def test_a_poison_event_neither_raises_nor_stops_the_feed(self, real_adapter, tmp_path):
        """``words`` that is not a list, or a type that is not a string: the cursor still moves and the submit step runs."""
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        config = _akou_config(tmp_path, transcription_backfill_per_run=1)
        await _akou_drain(config, real_adapter, server)
        job_id = server.by_key[SHA]
        server.add_event(
            "transcription.completed", {**_akou_result(job_id, SHA, "palabras raras"), "words": 5, "segments": 7}
        )
        server.add_event(["transcription.completed"], _akou_result(job_id, SHA))
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64)

        stats = await _akou_drain(config, real_adapter, server)
        assert (stats["reconciled"], stats["submitted"]) == (1, 1)
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["text"], row["words"], row["segments"]) == ("done", "palabras raras", [], [])
        assert await real_adapter.get_transcription_events_cursor() == "2"

    async def test_open_jobs_count_against_the_run(self, real_adapter, tmp_path):
        """A server slower than per_run per backup: the open rows stay at per_run instead of growing."""
        for n in range(1, 6):
            await _media(real_adapter, tmp_path, f"m_{n}_voice", content_hash=f"{n}" * 64)
        server = AkouServer()
        config = _akou_config(tmp_path, transcription_backfill_per_run=2)
        assert (await _akou_drain(config, real_adapter, server))["submitted"] == 2
        for _ in range(3):
            await _backdate(real_adapter, hours=1)
            stats = await _akou_drain(config, real_adapter, server)
            assert (stats["submitted"], stats["polled"]) == (0, 0)
        assert len(server.submits) == 2
        assert len(await real_adapter.get_open_job_transcripts(account_id=1)) == 2

        server.finish(server.by_key["5" * 64])  # the newest download went first
        stats = await _akou_drain(config, real_adapter, server)
        assert (stats["reconciled"], stats["submitted"]) == (1, 1)
        assert len(await real_adapter.get_open_job_transcripts(account_id=1)) == 2

    async def test_a_server_without_jobs_keeps_the_synchronous_path(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        server = AkouServer()
        server.server_body = {**AKOU_INFO, "capabilities": {"jobs": False}}
        stats = await _akou_drain(_akou_config(tmp_path), real_adapter, server)
        assert stats["done"] == 1
        assert server.submits == [] and server.event_reads == []
        assert len(server.transcribe_requests) == 1


# ============================================================================
# What is uploaded: the audio track alone, streamed from disk, under a size cap
# ============================================================================


def _fake_tool(tmp_path, monkeypatch, name: str, script: str) -> None:
    """Put a shell script called ``name`` first on PATH; ``script`` is its body."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text("#!/bin/sh\n" + script)
    tool.chmod(0o755)
    path = os.environ.get("PATH", "")
    if not path.startswith(f"{bin_dir}{os.pathsep}"):
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{path}")


# ffmpeg's last argument is the output file; this one writes bytes that differ on every run,
# as a different ffmpeg build extracting the same file might.
FAKE_FFMPEG = 'for a; do last="$a"; done\nprintf "OggS extracted by run %s" "$$" > "$last"\n'
AUDIO_STREAM = 'echo \'{"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {}}\'\n'


def _sent_file(request: httpx.Request) -> bytes:
    return dict(_PART.findall(request.content.decode("latin-1")))["file"].encode("latin-1")


async def _video_on_disk(adapter, tmp_path, media_id: str, content: bytes = AUDIO, media_type: str = "video") -> str:
    path = tmp_path / str(CHAT) / f"{media_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    await _file_media(adapter, path, media_id, media_type=media_type, mime_type="video/mp4")
    return str(path)


HASH = "c" * 64


async def _media_in_account(adapter, tmp_path, account_id: int, media_id: str, *, media_type="video") -> None:
    """A downloaded media row of the shared audio ``HASH`` in ``account_id``."""
    path = tmp_path / f"account{account_id}" / f"{media_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(AUDIO)
    message_id = int(media_id.split("_")[1])
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "fixture chat"}, account_id=account_id)
    await adapter.insert_message(
        {"id": message_id, "chat_id": CHAT, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
        account_id=account_id,
    )
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": CHAT,
            "type": media_type,
            "mime_type": "video/mp4",
            "file_path": str(path),
            "downloaded": True,
            "duration": 12,
            "content_hash": HASH,
            "download_date": datetime(2026, 1, 2),
        },
        account_id=account_id,
    )


async def _done_source(
    adapter,
    account_id: int,
    media_id: str,
    *,
    preset: str = "auto",
    source: str = "openai",
    engine_name: str = "openai",
    diarize: bool | None = None,
) -> dict:
    """A done row of ``HASH``; the defaults are what ``FakeServer``'s synchronous path writes."""
    row = await adapter.enqueue_media_transcript(
        media_id, account_id=account_id, content_hash=HASH, idempotency_key=HASH, preset=preset
    )
    await adapter.fill_media_transcript(
        row["id"],
        status="done",
        source=source,
        engine_name=engine_name,
        diarize=diarize,
        engine_version="0.3.0",
        models=["parakeet-v3"],
        language="es",
        language_confidence=0.97,
        text="hola desde la otra cuenta",
        words=[{"w": "hola", "s": 0.0, "e": 0.4, "c": 0.9}],
        segments=[{"s": 0.0, "e": 2.0, "text": "hola desde la otra cuenta", "speaker": None}],
        confidence=0.91,
        duration_s=2.0,
    )
    return await adapter.get_media_transcript(row["id"])


class TestCopyAcrossAccounts:
    async def test_the_same_audio_in_another_account_is_copied_and_nothing_is_probed_or_sent(
        self, real_adapter, tmp_path, monkeypatch
    ):
        calls = tmp_path / "tool-calls"
        _fake_tool(tmp_path, monkeypatch, "ffprobe", f'echo ffprobe >> "{calls}"\nexit 1\n')
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", f'echo ffmpeg >> "{calls}"\nexit 1\n')
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video")
        source = await _done_source(real_adapter, 1, "m_1_video")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video"})

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert stats == _stats(copied=1)
        assert server.transcribe_requests == []
        assert not calls.exists(), "no ffprobe, no ffmpeg"
        [copy] = await real_adapter.list_media_transcripts("m_7_video", account_id=2)
        assert (copy["status"], copy["copied_from_id"], copy["account_id"]) == ("done", source["id"], 2)
        for name in (
            "text",
            "language",
            "language_confidence",
            "words",
            "segments",
            "models",
            "source",
            "engine_name",
            "engine_version",
            "confidence",
            "duration_s",
            "diarize",
        ):
            assert copy[name] == source[name], name
        assert (copy["preset"], copy["idempotency_key"], copy["job_id"]) == ("auto", HASH, None)
        assert await real_adapter.get_media_transcript(source["id"]) == source, "the source is untouched"
        # Search and the export find the copy under its own account.
        page = await real_adapter.get_messages_paginated(chat_id=CHAT, search="otra cuenta", limit=5, account_id=2)
        assert [m["id"] for m in page] == [7]
        exported = await real_adapter.get_transcripts_for_export(account_id=2)
        assert [(r["id"], r["message_id"]) for r in exported] == [(copy["id"], 7)]
        assert "copied_from_id" not in exported[0], "the source's id may name a row the reader cannot see"

    async def test_another_preset_or_the_same_media_asking_again_is_sent(self, real_adapter, tmp_path):
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video", media_type="voice")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        await _done_source(real_adapter, 1, "m_1_video", preset="best")
        # The same audio is also still in flight in account 1 with this preset: not an answer yet.
        await _media_in_account(real_adapter, tmp_path, 1, "m_2_video", media_type="voice")
        await real_adapter.enqueue_media_transcript(
            "m_2_video", account_id=1, content_hash=HASH, idempotency_key=HASH, preset="auto"
        )
        own = await _done_source(real_adapter, 2, "m_7_video", preset="auto")
        await real_adapter.enqueue_media_transcript("m_7_video", account_id=2, force=True)  # a press after done
        server = FakeServer()
        config = _config(str(tmp_path))

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert stats == _stats(done=1)
        assert len(server.transcribe_requests) == 1
        newest = (await real_adapter.list_media_transcripts("m_7_video", account_id=2))[0]
        assert newest["id"] != own["id"] and newest["copied_from_id"] is None

    async def test_a_press_after_done_asks_the_server_even_when_a_twin_holds_a_copy(self, real_adapter, tmp_path):
        """X transcribed, Y (the same audio forwarded elsewhere) copied from X, then a press on X."""
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video", media_type="voice")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        server = FakeServer()
        config = _config(str(tmp_path))
        assert (
            await drain_transcriptions(
                config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
            )
        )["done"] == 1
        assert (
            await drain_transcriptions(
                config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
            )
        )["copied"] == 1
        await real_adapter.enqueue_media_transcript("m_1_video", account_id=1, force=True)  # the press on X

        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )

        assert stats == _stats(done=1)
        assert len(server.transcribe_requests) == 2
        newest, first = await real_adapter.list_media_transcripts("m_1_video", account_id=1)
        assert (newest["status"], newest["copied_from_id"]) == ("done", None)
        assert newest["id"] != first["id"]

    async def test_only_an_answer_from_the_same_server_is_copied(self, real_adapter, tmp_path):
        """Rows another server wrote are not what the OpenAI-path server here would answer now."""
        for media_id, source, engine in (
            ("m_1_video", "akou", "akou"),  # akou's job path
            ("m_2_video", "openai", "speaches"),  # another OpenAI-compatible server
            ("m_3_video", "akou", "openai"),  # the right engine name, the wrong path
        ):
            await _media_in_account(real_adapter, tmp_path, 1, media_id, media_type="voice")
            await _done_source(real_adapter, 1, media_id, source=source, engine_name=engine)
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        server = FakeServer()
        config = _config(str(tmp_path))

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert stats == _stats(done=1)
        assert len(server.transcribe_requests) == 1

    async def test_with_diarize_on_an_answer_made_without_it_is_not_copied(self, real_adapter, tmp_path):
        """Made before diarization was turned on: sent again, asking for speakers."""
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video", media_type="voice")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        await _done_source(real_adapter, 1, "m_1_video", source="akou", engine_name="akou")
        config = _akou_config(tmp_path, transcription_diarize=True)
        server = AkouServer()

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert (stats["copied"], stats["submitted"]) == (0, 1)
        [submit] = server.submits
        assert dict(_PART.findall(submit.content.decode("latin-1")))["diarize"] == "true"
        [row] = await real_adapter.list_media_transcripts("m_7_video", account_id=2)
        assert (row["diarize"], row["copied_from_id"]) == (True, None), "the row records that it asked for speakers"

    async def test_with_diarize_on_a_diarized_akou_answer_is_copied(self, real_adapter, tmp_path):
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video", media_type="voice")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        source = await _done_source(real_adapter, 1, "m_1_video", source="akou", engine_name="akou", diarize=True)
        config = _akou_config(tmp_path, transcription_diarize=True)
        server = AkouServer()

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert (stats["copied"], stats["submitted"]) == (1, 0)
        assert server.submits == []
        [row] = await real_adapter.list_media_transcripts("m_7_video", account_id=2)
        assert (row["diarize"], row["copied_from_id"]) == (True, source["id"])

    async def test_diarize_on_with_a_server_that_cannot_diarize_copies_a_plain_answer(self, real_adapter, tmp_path):
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video", media_type="voice")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        source = await _done_source(real_adapter, 1, "m_1_video", diarize=False)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_diarize=True)

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert stats == _stats(copied=1)
        [row] = await real_adapter.list_media_transcripts("m_7_video", account_id=2)
        assert row["copied_from_id"] == source["id"]

    async def test_the_listener_path_asks_the_server_before_copying(self, real_adapter, tmp_path):
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video", media_type="voice")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video", media_type="voice")
        source = await _done_source(real_adapter, 1, "m_1_video")
        server = FakeServer()
        config = _config(str(tmp_path))
        # What the listener passes: the row it just inserted, hash included.
        media = {**await real_adapter.get_media_by_id("m_7_video", account_id=2), "content_hash": HASH}

        outcome = await transcribe_media(
            config, real_adapter, media, account_id=2, client=_client(config, server), notifier=AsyncMock()
        )

        assert outcome == "copied"
        assert [r.url.path for r in server.requests] == ["/base/v1/server"]
        [row] = await real_adapter.list_media_transcripts("m_7_video", account_id=2)
        assert row["copied_from_id"] == source["id"]

    async def test_a_waiting_press_is_answered_with_the_copy(self, real_adapter, tmp_path):
        await _media_in_account(real_adapter, tmp_path, 1, "m_1_video")
        await _media_in_account(real_adapter, tmp_path, 2, "m_7_video")
        source = await _done_source(real_adapter, 1, "m_1_video")
        asked = await real_adapter.enqueue_media_transcript("m_7_video", account_id=2, force=True)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"voice"})  # video is not automatic

        stats = await drain_transcriptions(
            config, real_adapter, account_id=2, notifier=AsyncMock(), client=_client(config, server)
        )

        assert stats == _stats(copied=1)
        [row] = await real_adapter.list_media_transcripts("m_7_video", account_id=2)
        assert (row["id"], row["status"], row["copied_from_id"]) == (asked["id"], "done", source["id"])


class TestUpload:
    async def test_a_video_goes_out_as_its_audio_track_from_disk_and_the_temp_file_is_removed(
        self, real_adapter, tmp_path, monkeypatch
    ):
        _fake_tool(tmp_path, monkeypatch, "ffprobe", AUDIO_STREAM)
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", FAKE_FFMPEG)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        await _video_on_disk(real_adapter, tmp_path, "m_1_video", content=b"\x00" * 4096)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video"})
        sent_types = []
        real_transcribe = TranscriptionClient.transcribe

        async def spy(self, audio, filename, **kwargs):
            sent_types.append(type(audio))
            return await real_transcribe(self, audio, filename, **kwargs)

        with patch.object(TranscriptionClient, "transcribe", spy):
            stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=1)
        [request] = server.transcribe_requests
        assert 'filename="m_1_video.ogg"' in request.content.decode("latin-1")
        assert _sent_file(request).startswith(b"OggS extracted by run ")
        assert sent_types and all(t is not bytes for t in sent_types), "the upload is an open file, not bytes"
        assert list(scratch.iterdir()) == [], "the extracted file is deleted"
        [row] = await _rows(real_adapter, "m_1_video")
        assert row["idempotency_key"] == hashlib.sha256(b"\x00" * 4096).hexdigest(), "the stored file's hash"

    async def test_voice_and_music_go_out_as_stored_without_ffmpeg(self, real_adapter, tmp_path, monkeypatch):
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", "echo called >> " + str(tmp_path / "ffmpeg-calls") + "\nexit 1\n")
        await _media(real_adapter, tmp_path, "m_1_voice")
        path = tmp_path / str(CHAT) / "song.mp3"
        path.write_bytes(AUDIO)
        await _file_media(real_adapter, path, "m_2_audio", media_type="audio", mime_type="audio/mpeg", duration=3)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"voice", "audio"})

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=2)
        assert sorted(_sent_file(r) for r in server.transcribe_requests) == [AUDIO, AUDIO]
        assert not (tmp_path / "ffmpeg-calls").exists()

    async def test_a_missing_ffmpeg_sends_the_stored_file_and_warns_once(
        self, real_adapter, tmp_path, monkeypatch, caplog
    ):
        _fake_tool(tmp_path, monkeypatch, "ffprobe", AUDIO_STREAM)
        # PATH holds the fake ffprobe and nothing else, so there is no ffmpeg.
        monkeypatch.setenv("PATH", str(tmp_path / "fake-bin"))
        monkeypatch.setattr("src.transcription._warned", set())
        await _video_on_disk(real_adapter, tmp_path, "m_1_video")
        await _video_on_disk(real_adapter, tmp_path, "m_2_video")
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video"})

        with caplog.at_level(logging.DEBUG, logger="src.transcription"):
            stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=2)
        assert [_sent_file(r) for r in server.transcribe_requests] == [AUDIO, AUDIO]
        lines = [r for r in caplog.records if "ffmpeg" in r.getMessage()]
        assert [r.levelno for r in lines] == [logging.WARNING, logging.DEBUG]
        assert "not installed" in lines[0].getMessage()

    async def test_the_size_limit_reads_the_bytes_actually_sent(self, real_adapter, tmp_path, monkeypatch):
        """Over the limit as stored, a voice message and a video both go out as a small Opus track."""
        _fake_tool(tmp_path, monkeypatch, "ffprobe", AUDIO_STREAM)
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", FAKE_FFMPEG)
        big = b"\x01" * (2 * 1024 * 1024)
        path = tmp_path / str(CHAT) / "long.ogg"
        path.parent.mkdir(parents=True)
        path.write_bytes(big)
        await _file_media(real_adapter, path, "m_1_voice", media_type="voice", duration=5)
        await _video_on_disk(real_adapter, tmp_path, "m_2_video", content=big)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"voice", "video"}, transcription_max_upload_mb=1)

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=2)
        sent = sorted(_sent_file(r) for r in server.transcribe_requests)
        assert all(body.startswith(b"OggS extracted by run ") for body in sent)

    async def test_an_extracted_track_still_over_the_limit_is_skipped(self, real_adapter, tmp_path, monkeypatch):
        _fake_tool(tmp_path, monkeypatch, "ffprobe", AUDIO_STREAM)
        # This ffmpeg writes 2 MB whatever it is given.
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", 'for a; do last="$a"; done\nhead -c 2097152 /dev/zero > "$last"\n')
        path = tmp_path / str(CHAT) / "song.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x01" * (2 * 1024 * 1024))
        await _file_media(real_adapter, path, "m_1_audio", media_type="audio", mime_type="audio/mpeg", duration=5)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"audio"}, transcription_max_upload_mb=1)

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(skipped=1)
        assert server.transcribe_requests == []
        [row] = await _rows(real_adapter, "m_1_audio")
        assert (row["status"], row["error"]) == ("skipped", "too_large")

    async def test_a_limit_of_zero_means_no_limit(self, real_adapter, tmp_path, monkeypatch):
        calls = tmp_path / "ffmpeg-calls"
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", f'echo called >> "{calls}"\nexit 1\n')
        path = tmp_path / str(CHAT) / "long.ogg"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x01" * (2 * 1024 * 1024))
        await _file_media(real_adapter, path, "m_1_voice", media_type="voice", duration=5)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_max_upload_mb=0)

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=1)
        assert _sent_file(server.transcribe_requests[0]) == path.read_bytes(), "sent as stored"
        assert not calls.exists(), "nothing was over a limit, so nothing was extracted"

    async def test_extract_audio_removes_its_temp_file_when_cancelled(self, tmp_path, monkeypatch):
        import threading

        import src.transcription as transcription

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        started, release = threading.Event(), threading.Event()

        def slow_extract(path, dest):
            started.set()
            release.wait(5)
            return True

        monkeypatch.setattr(transcription, "_run_extract", slow_extract)
        task = asyncio.create_task(transcription.extract_audio(str(tmp_path / "clip.mp4")))
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert len(list(scratch.iterdir())) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert list(scratch.iterdir()) == [], "the temp file is gone after a cancel"

    async def test_at_most_two_ffprobe_or_ffmpeg_runs_at_once(self, monkeypatch):
        import threading
        import time

        import src.transcription as transcription

        lock = threading.Lock()
        running = peak = 0

        def tool(*args):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            return None

        monkeypatch.setattr(transcription, "_run_ffprobe", tool)
        monkeypatch.setattr(transcription, "_run_extract", lambda path, dest: tool() or False)
        monkeypatch.setattr(transcription, "_warned", set())
        await asyncio.gather(
            *(transcription.probe_audio(f"/x/{i}.mp4") for i in range(4)),
            *(transcription.extract_audio(f"/x/{i}.mp4") for i in range(4)),
        )
        assert peak == transcription.MEDIA_TOOL_SLOTS == 2

    async def test_a_reextraction_with_other_bytes_never_conflicts_and_the_outcome_still_matches(
        self, real_adapter, tmp_path, monkeypatch
    ):
        """The header key is the sent bytes' hash; metadata and the row keep the stored file's hash."""
        _fake_tool(tmp_path, monkeypatch, "ffprobe", AUDIO_STREAM)
        _fake_tool(tmp_path, monkeypatch, "ffmpeg", FAKE_FFMPEG)
        stored = b"\x02" * 4096
        stored_hash = hashlib.sha256(stored).hexdigest()
        await _video_on_disk(real_adapter, tmp_path, "m_1_video", content=stored)
        server = AkouServer()
        config = _akou_config(tmp_path, transcription_types={"video"})

        # The first submit reaches akou, which makes the job, and the answer is lost.
        server.submit_timeout = True
        assert (await _akou_drain(config, real_adapter, server))["unreachable"] == 1
        [first] = server.submits
        first_bytes = _sent_file(first)
        assert first.headers["idempotency-key"] == hashlib.sha256(first_bytes).hexdigest()
        assert json.loads(dict(_PART.findall(first.content.decode("latin-1")))["metadata"]) == {
            "content_hash": stored_hash
        }
        server.add_job(stored_hash, status="queued", audio=first_bytes, key=first.headers["idempotency-key"])

        # The retry extracts other bytes: a new key and a new job, never 422.
        server.submit_timeout = False
        await _make_stale(real_adapter)
        stats = await _akou_drain(config, real_adapter, server)
        assert (stats["submitted"], stats["failed"]) == (1, 0)
        second = server.submits[-1]
        assert _sent_file(second) != first_bytes
        assert second.headers["idempotency-key"] == hashlib.sha256(_sent_file(second)).hexdigest()
        [row] = await _rows(real_adapter, "m_1_video")
        assert (row["status"], row["idempotency_key"]) == ("queued", stored_hash)

        # The outcome is matched on the stored file's hash.
        server.finish(row["job_id"], text="del vídeo")
        await _akou_drain(config, real_adapter, server)
        [row] = await _rows(real_adapter, "m_1_video")
        assert (row["status"], row["text"]) == ("done", "del vídeo")

    @needs_ffmpeg
    async def test_real_files_a_video_with_sound_is_sent_as_a_smaller_opus_track(self, real_adapter, tmp_path):
        clip = tmp_path / str(CHAT) / "clip.mp4"
        _ffmpeg(
            clip,
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", "-shortest",
        )  # fmt: skip
        await _file_media(real_adapter, clip, "m_1_video", media_type="video", mime_type="video/mp4", duration=3)
        server = FakeServer()
        config = _config(str(tmp_path), transcription_types={"video"})

        stats = await _drain_all(config, real_adapter, server)

        assert stats == _stats(done=1)
        sent = _sent_file(server.transcribe_requests[0])
        assert sent.startswith(b"OggS") and b"OpusHead" in sent[:200]
        assert len(sent) < clip.stat().st_size / 3
        # The same build extracts the same bytes, so a retry keeps its key.
        from src.transcription import extract_audio

        once, twice = await extract_audio(str(clip)), await extract_audio(str(clip))
        try:
            with open(once, "rb") as first, open(twice, "rb") as second:
                assert first.read() == second.read()
        finally:
            os.remove(once)
            os.remove(twice)


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

    async def test_an_audio_or_video_document_is_enqueued_and_a_pdf_is_not(self):
        from test_listener_extended import _make_listener_with_handlers

        listener, _, _, _ = _make_listener_with_handlers(
            transcription_enabled=True, transcription_url=URL, transcription_types={"voice", "document"}
        )
        rows = [
            {"id": "m_1_document", "type": "document", "mime_type": "audio/flac"},
            {"id": "m_2_document", "type": "document", "mime_type": "video/x-matroska"},
            {"id": "m_3_document", "type": "document", "mime_type": "application/pdf"},
            {"id": "m_4_document", "type": "document", "mime_type": None},
            {"id": "m_5_video", "type": "video", "mime_type": "video/mp4"},  # not in the configured types
        ]
        with patch("src.listener.transcribe_media", new=AsyncMock(return_value="done")) as transcribe:
            for row in rows:
                listener._enqueue_transcription(row)
            await asyncio.gather(*listener._transcription_tasks)
        assert [call.args[2]["id"] for call in transcribe.await_args_list] == ["m_1_document", "m_2_document"]

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
    assert "refreshTranscripts(transcriptMsg)" in block
    assert "data.media_id" not in block  # the frame carries no storage id
    helper = source[source.index("const transcriptsUrl = ") : source.index("const pressTranscript = ")]
    assert "/api/chats/${encodeURIComponent(selectedChat.value?.ref || '')}/media/" in helper
    assert "target.media.transcripts = rows" in source[source.index("const storeTranscriptRows = ") :]


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
                "transcript_id": 7,
                "status": "done",
            },
        )
        self.assertNotIn("text", json.dumps(frame))
        # The storage media id spells the chat id, which never reaches the browser.
        self.assertNotIn("m_1_voice", json.dumps(frame))


# ============================================================================
# The signed callback route (slice 5)
# ============================================================================

import base64  # noqa: E402
import hmac  # noqa: E402
import time  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

CALLBACK_PATH = "/api/transcriptions/callback"
WEBHOOK_KEY = b"test@value/here-webhook-key"
WEBHOOK_SECRET = "whsec_" + base64.b64encode(WEBHOOK_KEY).decode()


def _sign(webhook_id: str, timestamp: str, body: bytes, key: bytes = WEBHOOK_KEY) -> str:
    """The Standard Webhooks signature, computed here independently of the route."""
    digest = hmac.new(key, f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _delivery(data: dict, *, event_type: str = "transcription.completed", webhook_id: str = "msg_0001", **sign):
    body = json.dumps({"type": event_type, "timestamp": "2026-01-02T03:04:05Z", "data": data}).encode()
    timestamp = str(int(time.time()))
    headers = {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": _sign(webhook_id, timestamp, body, **sign),
        "content-type": "application/json",
    }
    return body, headers


async def _post(content, headers: dict) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
        return await client.post(CALLBACK_PATH, content=content, headers=headers)


@pytest.fixture
async def callback_route(real_adapter):
    """The route installed with a test secret on the real app, the database a real adapter."""
    saved_db, saved_key = web_main.db, web_main._transcription_webhook_key
    web_main.db = real_adapter
    assert web_main.install_transcription_callback(web_main.app, WEBHOOK_SECRET) is True
    try:
        with patch.object(web_main, "handle_realtime_notification", new=AsyncMock()) as push:
            yield push
    finally:
        web_main.app.router.routes[:] = [
            r for r in web_main.app.router.routes if getattr(r, "path", None) != CALLBACK_PATH
        ]
        web_main.db = saved_db
        web_main._transcription_webhook_key = saved_key


async def _running_row(adapter, tmp_path, media_id: str = "m_1_voice", job_id: str = "job_0001") -> dict:
    await _media(adapter, tmp_path, media_id)
    row = await adapter.enqueue_media_transcript(media_id, account_id=1, idempotency_key=SHA, source="akou")
    await adapter.fill_media_transcript(row["id"], status="running", job_id=job_id)
    return row


class TestCallbackRoute:
    async def test_a_valid_delivery_fills_the_row_and_broadcasts_ids_and_status(
        self, real_adapter, tmp_path, callback_route
    ):
        row = await _running_row(real_adapter, tmp_path)
        await real_adapter.set_transcription_server("akou", "0.3.0")
        body, headers = _delivery(_akou_result("job_0001", SHA, "llegó por el callback"))
        resp = await _post(body, headers)
        assert resp.status_code == 204
        [stored] = await _rows(real_adapter, "m_1_voice")
        assert (stored["id"], stored["status"], stored["text"]) == (row["id"], "done", "llegó por el callback")
        assert (stored["engine_name"], stored["engine_version"]) == ("akou", "0.3.0")
        callback_route.assert_awaited_once()
        payload = callback_route.await_args.args[0]
        assert payload == {
            "type": "transcript",
            "chat_id": CHAT,
            "account_id": 1,
            "data": {
                "account_id": 1,
                "chat_id": CHAT,
                "message_id": 1,
                "media_id": "m_1_voice",
                "transcript_id": row["id"],
                "status": "done",
            },
        }
        assert "llegó" not in json.dumps(payload)

    async def test_a_twin_row_never_submitted_fills_from_the_callback(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        await _media(real_adapter, tmp_path, "m_2_voice")
        twin = await real_adapter.enqueue_media_transcript(
            "m_2_voice", account_id=1, idempotency_key=SHA, source="akou"
        )
        assert twin["job_id"] is None
        body, headers = _delivery(_akou_result("job_0001", SHA, "dos filas, un callback"))
        assert (await _post(body, headers)).status_code == 204
        for media_id in ("m_1_voice", "m_2_voice"):
            [row] = await _rows(real_adapter, media_id)
            assert (row["status"], row["text"], row["job_id"]) == ("done", "dos filas, un callback", "job_0001")
        assert callback_route.await_count == 2

    async def test_hostile_list_fields_and_types_are_a_204_not_a_500(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        body = json.dumps({"type": ["transcription.completed"], "data": _akou_result("job_0001", SHA)}).encode()
        timestamp = str(int(time.time()))
        headers = {
            "webhook-id": "msg_0002",
            "webhook-timestamp": timestamp,
            "webhook-signature": _sign("msg_0002", timestamp, body),
            "content-type": "application/json",
        }
        assert (await _post(body, headers)).status_code == 204
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "running"  # an unknown type writes nothing

        body, headers = _delivery({**_akou_result("job_0001", SHA, "con palabras rotas"), "words": 5, "segments": 7})
        assert (await _post(body, headers)).status_code == 204
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["text"], row["words"], row["segments"]) == ("done", "con palabras rotas", [], [])

    async def test_a_stale_timestamp_is_refused(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0001", SHA))
        for offset in (-301, 301):
            stale = str(int(time.time()) + offset)
            resp = await _post(
                body, {**headers, "webhook-timestamp": stale, "webhook-signature": _sign("msg_0001", stale, body)}
            )
            assert resp.status_code == 401
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "running"
        callback_route.assert_not_awaited()

    async def test_an_absurd_timestamp_is_a_401_not_a_500(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0001", SHA))
        for absurd in ("9" * 400, "12.5", "1e9"):
            resp = await _post(
                body, {**headers, "webhook-timestamp": absurd, "webhook-signature": _sign("msg_0001", absurd, body)}
            )
            assert resp.status_code == 401
        callback_route.assert_not_awaited()

    async def test_a_wrong_secret_or_a_changed_body_is_refused(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0001", SHA), key=b"another test@value/here key")
        assert (await _post(body, headers)).status_code == 401
        body, headers = _delivery(_akou_result("job_0001", SHA))
        assert (await _post(body.replace(b"hola", b"hol4"), headers)).status_code == 401
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "running"
        callback_route.assert_not_awaited()

    async def test_a_signature_list_is_accepted_when_only_the_second_value_matches(
        self, real_adapter, tmp_path, callback_route
    ):
        await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0001", SHA))
        old = _sign("msg_0001", headers["webhook-timestamp"], body, key=b"the retired test@value/here key")
        headers["webhook-signature"] = f"{old} v2,ignored {headers['webhook-signature']}"
        assert (await _post(body, headers)).status_code == 204
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "done"

    async def test_missing_headers_are_a_400(self, real_adapter, tmp_path, callback_route):
        body, headers = _delivery(_akou_result("job_0001", SHA))
        for name in ("webhook-id", "webhook-timestamp", "webhook-signature"):
            partial = {k: v for k, v in headers.items() if k != name}
            assert (await _post(body, partial)).status_code == 400, name

    async def test_a_replayed_delivery_writes_nothing(self, real_adapter, tmp_path, callback_route):
        """Not even into a newer open row for the same audio a user asked for since."""
        first = await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0001", SHA, "primera entrega"))
        assert (await _post(body, headers)).status_code == 204
        again = await real_adapter.enqueue_media_transcript(
            "m_1_voice", account_id=1, idempotency_key=SHA, source="akou", force=True
        )
        assert (await _post(body, headers)).status_code == 204
        newest, done = await _rows(real_adapter, "m_1_voice")
        assert (done["id"], done["text"]) == (first["id"], "primera entrega")
        assert (newest["id"], newest["status"], newest["text"]) == (again["id"], "queued", None)
        callback_route.assert_awaited_once()

    async def test_an_oversized_body_with_a_length_is_refused_before_it_is_read(
        self, real_adapter, tmp_path, callback_route
    ):
        await _running_row(real_adapter, tmp_path)
        data = _akou_result("job_0001", SHA, "x" * (256 * 1024))
        body, headers = _delivery(data)  # correctly signed: only the size refuses it
        resp = await _post(body, headers)
        assert resp.status_code == 413
        # A small, correctly signed body that declares a length over the cap is
        # refused on the header alone, before a byte of it is read.
        small, small_headers = _delivery(_akou_result("job_0001", SHA, "pequeño"))
        resp = await _post(small, {**small_headers, "content-length": str(256 * 1024 + 1)})
        assert resp.status_code == 413
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "running"

    async def test_an_oversized_chunked_body_is_refused(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0001", SHA, "x" * (256 * 1024)))

        async def chunks():
            for start in range(0, len(body), 16 * 1024):
                yield body[start : start + 16 * 1024]

        resp = await _post(chunks(), headers)
        assert resp.status_code == 413
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "running"

        # The same streamed shape under the cap goes through.
        small, small_headers = _delivery(_akou_result("job_0001", SHA, "cabe"), webhook_id="msg_0002")

        async def small_chunks():
            yield small[:10]
            yield small[10:]

        assert (await _post(small_chunks(), small_headers)).status_code == 204
        assert (await _rows(real_adapter, "m_1_voice"))[0]["text"] == "cabe"

    async def test_result_url_instead_of_text_writes_nothing(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        data = _akou_result("job_0001", SHA)
        for name in ("text", "words", "segments"):
            data.pop(name)
        data["result_url"] = "/v1/jobs/job_0001/result"
        body, headers = _delivery(data)
        assert (await _post(body, headers)).status_code == 204
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["text"]) == ("running", None)
        callback_route.assert_not_awaited()

    async def test_a_hash_no_open_row_carries_and_an_unknown_type_write_nothing(
        self, real_adapter, tmp_path, callback_route
    ):
        await _running_row(real_adapter, tmp_path)
        body, headers = _delivery(_akou_result("job_0009", "0" * 64))
        assert (await _post(body, headers)).status_code == 204
        body, headers = _delivery(_akou_result("job_0001", SHA), event_type="transcription.previewed")
        assert (await _post(body, headers)).status_code == 204
        assert (await _rows(real_adapter, "m_1_voice"))[0]["status"] == "running"
        callback_route.assert_not_awaited()

    async def test_failed_and_cancelled_deliveries_store_their_reason(self, real_adapter, tmp_path, callback_route):
        await _running_row(real_adapter, tmp_path)
        data = {"job_id": "job_0001", "status": "failed", "error": {"code": "decode_failed", "message": "text"}}
        body, headers = _delivery({**data, "metadata": {"content_hash": SHA}}, event_type="transcription.failed")
        assert (await _post(body, headers)).status_code == 204
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"]) == ("failed", "decode_failed")


def test_the_route_is_absent_without_a_usable_secret():
    # The test environment sets no TRANSCRIPTION_WEBHOOK_SECRET.
    assert CALLBACK_PATH not in [getattr(r, "path", None) for r in web_main.app.routes]
    bare = FastAPI()
    for secret in ("", None, "not-a-whsec-secret", "whsec_", "whsec_!!not base64!!"):
        assert web_main.install_transcription_callback(bare, secret) is False, secret
    assert CALLBACK_PATH not in [getattr(r, "path", None) for r in bare.routes]
    assert web_main.install_transcription_callback(bare, WEBHOOK_SECRET) is True
    assert CALLBACK_PATH in [getattr(r, "path", None) for r in bare.routes]


def test_the_viewer_config_reads_the_secret_without_telegram_credentials(tmp_path):
    from src.config import Config

    env = {
        "BACKUP_PATH": str(tmp_path),
        "DATABASE_PATH": str(tmp_path / "viewer.db"),
        "TRANSCRIPTION_WEBHOOK_SECRET": WEBHOOK_SECRET,
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config()
    assert config.api_id is None
    assert config.transcription_webhook_secret == WEBHOOK_SECRET


class TestParseEventsPage:
    """The event page as akou's feed answers it (akou src/main/api/routes/jobs.ts GET /events)."""

    def test_the_top_level_integer_cursor_is_the_next_after(self):
        page = {
            "events": [{"id": "msg_0007", "cursor": 7, "type": "transcription.completed", "data": {}}],
            "cursor": 7,
            "has_more": False,
        }
        events, cursor = parse_events_page(page)
        assert len(events) == 1
        assert cursor == "7"

    def test_the_event_id_is_never_used_as_a_cursor(self):
        page = {"events": [{"id": "msg_0009", "type": "transcription.completed", "data": {}}]}
        assert parse_events_page(page)[1] is None

    def test_the_last_event_cursor_is_used_when_the_page_has_none(self):
        page = {"events": [{"id": "msg_0001", "cursor": 1}, {"id": "msg_0002", "cursor": 2}]}
        assert parse_events_page(page)[1] == "2"

    def test_an_empty_page_keeps_the_stored_cursor(self):
        assert parse_events_page({"events": [], "cursor": 12, "has_more": False}) == ([], "12")
        assert parse_events_page({}) == ([], None)
