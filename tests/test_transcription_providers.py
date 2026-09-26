"""TRANSCRIPTION_PROVIDER and the providers of the synchronous path (docs/TRANSCRIPTION.md, "Providers").

``auto`` still picks akou's job path when the server says so; ``akou``
insists on it; ``openai`` skips the question and sends TRANSCRIPTION_MODEL.
A refusal about the server's setup (a wrong key, no credit, an unknown
model, a rate limit) or a 413 from a limit on the way keeps the rows queued
and ends the run; any other 4xx is that file's failed row. Each
native adapter (Deepgram, AssemblyAI, ElevenLabs) runs against a fake built
from its official request and response examples, with its auth header and
its failure shapes. No log line carries the key or the URL.
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.config import Config
from src.transcription import TranscriptionClient, drain_transcriptions, result_columns
from src.transcription_providers import segments_from_words

sys.path.insert(0, os.path.dirname(__file__))

from test_transcription import (  # noqa: E402
    AUDIO,
    KEY,
    URL,
    VERBOSE_JSON,
    AkouServer,
    FakeServer,
    _client,
    _config,
    _make_stale,
    _media,
    _rows,
    _stats,
)


def _provider_client(config, handler) -> tuple[TranscriptionClient, list[httpx.Request]]:
    """A client whose transport records every request and answers with ``handler``."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = TranscriptionClient(config, transport=httpx.MockTransport(record))
    client.backoffs = (0.0, 0.0)
    client.poll_interval = 0.0
    return client, requests


async def _drain(config, adapter, client) -> dict:
    return await drain_transcriptions(config, adapter, account_id=1, notifier=AsyncMock(), client=client)


def _field(request: httpx.Request, name: str) -> list[str]:
    """Every value of one multipart field."""
    body = request.content.decode("latin-1")
    marker = f'name="{name}"\r\n\r\n'
    values = []
    for part in body.split(marker)[1:]:
        values.append(part.split("\r\n", 1)[0])
    return values


# ============================================================================
# Configuration
# ============================================================================


def _base_env(temp_dir: str) -> dict:
    return {
        "CHAT_TYPES": "private",
        "BACKUP_PATH": temp_dir,
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "abcdef",
        "TELEGRAM_PHONE": "+1234567890",
    }


class TestProviderConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def _config(self, **extra):
        with patch.dict(os.environ, _base_env(self.temp_dir) | extra, clear=True):
            return Config()

    def test_defaults_keep_todays_behaviour(self):
        config = self._config()
        self.assertEqual(config.transcription_provider, "auto")
        self.assertEqual(config.transcription_model, "")
        self.assertEqual(config.transcription_hotwords, [])

    def test_every_provider_name_is_accepted_and_the_values_are_read(self):
        for name in ("auto", "akou", "openai", "deepgram", "assemblyai", "elevenlabs"):
            config = self._config(TRANSCRIPTION_PROVIDER=f" {name.upper()} ")
            self.assertEqual(config.transcription_provider, name)
        config = self._config(TRANSCRIPTION_MODEL=" whisper-large-v3 ", TRANSCRIPTION_HOTWORDS="Acme, , Zork ")
        self.assertEqual(config.transcription_model, "whisper-large-v3")
        self.assertEqual(config.transcription_hotwords, ["Acme", "Zork"])

    def test_an_unknown_provider_warns_without_the_value_and_falls_back_to_auto(self):
        with self.assertLogs("src.config", level=logging.WARNING) as logs:
            config = self._config(TRANSCRIPTION_PROVIDER="nosuchvendor")
        self.assertEqual(config.transcription_provider, "auto")
        self.assertTrue(any("TRANSCRIPTION_PROVIDER" in line for line in logs.output))
        self.assertFalse(any("nosuchvendor" in line for line in logs.output))


# ============================================================================
# Choosing the path
# ============================================================================


class TestChoosingThePath:
    @pytest.mark.parametrize("provider", [None, "auto"])
    async def test_auto_still_picks_akous_job_path(self, real_adapter, tmp_path, provider):
        await _media(real_adapter, tmp_path, "m_1_voice")
        overrides = {"transcription_callback_url": ""}
        if provider:
            overrides["transcription_provider"] = provider
        config = _config(str(tmp_path), **overrides)
        server = AkouServer()
        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )
        assert stats["submitted"] == 1
        assert [r.url.path for r in server.requests if r.method == "POST"] == ["/base/v1/jobs"]
        assert server.transcribe_requests == []

    async def test_akou_insists_on_the_job_path_and_spends_nothing_on_another_server(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="akou", transcription_callback_url="")
        server = FakeServer()  # 404 on /v1/server: not akou
        stats = await _drain(config, real_adapter, _client(config, server))
        assert stats == _stats()
        assert [r.method for r in server.requests] == ["GET"]
        assert await _rows(real_adapter, "m_1_voice") == []

        akou = AkouServer()
        stats = await _drain(config, real_adapter, _client(config, akou))
        assert stats["submitted"] == 1

    async def test_akou_on_the_listener_path_sends_nothing_to_another_server(self, real_adapter, tmp_path):
        from src.transcription import transcribe_media

        media = await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="akou")
        server = FakeServer()
        outcome = await transcribe_media(config, real_adapter, media, account_id=1, client=_client(config, server))
        assert outcome == "refused"
        assert server.transcribe_requests == []
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["queued"]

    async def test_openai_skips_the_question_and_sends_the_configured_model(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="openai", transcription_model="whisper-large-v3-turbo")
        server = FakeServer()
        stats = await _drain(config, real_adapter, _client(config, server))
        assert stats["done"] == 1
        assert [r.url.path for r in server.requests] == ["/base/v1/audio/transcriptions"]
        assert _field(server.transcribe_requests[0], "model") == ["whisper-large-v3-turbo"]
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["source"], row["engine_name"], row["models"]) == ("openai", "openai", ["whisper-large-v3-turbo"])

    async def test_the_model_reaches_an_auto_detected_server_and_akou_still_gets_the_preset(
        self, real_adapter, tmp_path
    ):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_model="voxtral-mini-latest", transcription_preset="best")
        server = FakeServer()
        await _drain(config, real_adapter, _client(config, server))
        assert _field(server.transcribe_requests[0], "model") == ["voxtral-mini-latest"]

        from src.transcription import ServerInfo

        client = _client(config, server)
        assert client.sync_model(ServerInfo(name="akou")) == "best"
        assert _client(_config(str(tmp_path)), server).sync_model(ServerInfo()) == "whisper-1"

    async def test_the_hotwords_go_out_as_the_prompt(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_hotwords=["Acme", "Zork"])
        server = FakeServer()
        await _drain(config, real_adapter, _client(config, server))
        assert _field(server.transcribe_requests[0], "prompt") == ["Acme, Zork"]


# ============================================================================
# A server that refuses every file is set up wrong
# ============================================================================


def _refusing(status: int, body: dict | None = None, *, good: set[int] | None = None):
    """An OpenAI-shaped server: ``good`` upload numbers (1-based) succeed, every other upload is refused."""
    count = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/server"):
            return httpx.Response(404)
        count["n"] += 1
        if good and count["n"] in good:
            return httpx.Response(200, json=VERBOSE_JSON)
        return httpx.Response(status, json=body or {"detail": "Model 'whisper-1' is not installed locally"})

    return handle


class TestRefusedEveryFile:
    @pytest.mark.parametrize("status", [401, 403, 404, 429])
    async def test_a_refusal_about_the_servers_setup_ends_the_run_and_spends_no_file(
        self, real_adapter, tmp_path, status
    ):
        """A wrong key, no access, an unknown model or a wrong URL, a rate limit: the same for every file."""
        for n in range(1, 4):
            await _media(real_adapter, tmp_path, f"m_{n}_voice")
        config = _config(str(tmp_path))
        for _ in range(4):  # more runs than the cap of three failed rows
            client, requests = _provider_client(config, _refusing(status))
            stats = await _drain(config, real_adapter, client)
            assert stats == _stats(refused=1)
            # The client asks three times on a 429 before it gives up on the run.
            assert len([r for r in requests if r.method == "POST"]) == (3 if status == 429 else 1)
            await _make_stale(real_adapter)
        assert not [
            r for n in range(1, 4) for r in await _rows(real_adapter, f"m_{n}_voice") if r["status"] != "queued"
        ]

        client, _ = _provider_client(config, _refusing(status, good={1, 2, 3}))
        assert (await _drain(config, real_adapter, client))["done"] == 3

    @pytest.mark.parametrize("status", [400, 422])
    async def test_a_4xx_about_a_file_fails_that_file_and_the_run_goes_on(self, real_adapter, tmp_path, status):
        """Two files the server refuses first never block the rest (the default auto setting)."""
        from datetime import datetime

        for n, day in ((1, 4), (2, 3), (3, 2)):
            await _media(real_adapter, tmp_path, f"m_{n}_voice", download_date=datetime(2026, 1, day))
        config = _config(str(tmp_path))
        # A 400 is asked once more for plain json before it counts, so the good upload is the fifth.
        good = {5} if status == 400 else {3}
        client, _ = _provider_client(config, _refusing(status, {"error": {"message": "no", "code": None}}, good=good))

        stats = await _drain(config, real_adapter, client)

        assert (stats["done"], stats["failed"], stats["refused"]) == (1, 2, 0)
        for n in (1, 2):
            [bad] = await _rows(real_adapter, f"m_{n}_voice")
            assert (bad["status"], bad["error"], bad["source"]) == ("failed", f"HTTP {status}", "openai")
        assert [r["status"] for r in await _rows(real_adapter, "m_3_voice")] == ["done"]

    async def test_the_listener_path_leaves_a_refused_row_queued_for_the_drain(self, real_adapter, tmp_path):
        from src.transcription import transcribe_media

        media = await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path))
        client, _ = _provider_client(config, _refusing(404))
        assert await transcribe_media(config, real_adapter, media, account_id=1, client=client) == "refused"
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["queued"]

    async def test_a_413_is_a_limit_on_the_way_it_ends_the_run_and_spends_no_file(self, real_adapter, tmp_path, caplog):
        """The archive never sends more than TRANSCRIPTION_MAX_UPLOAD_MB, so a 413 is a lower limit elsewhere."""
        for n in (1, 2):
            await _media(real_adapter, tmp_path, f"m_{n}_voice")
        config = _config(str(tmp_path), transcription_max_upload_mb=500)
        client, requests = _provider_client(config, _refusing(413, {"error": {"message": "too big", "code": None}}))

        with caplog.at_level(logging.WARNING, logger="src.transcription"):
            stats = await _drain(config, real_adapter, client)

        assert stats == _stats(refused=1)
        assert len([r for r in requests if r.method == "POST"]) == 1
        [row] = await _rows(real_adapter, "m_1_voice") or await _rows(real_adapter, "m_2_voice")
        assert (row["status"], row["error"]) == ("queued", None)
        [line] = [r.getMessage() for r in caplog.records if "413" in r.getMessage()]
        assert "TRANSCRIPTION_MAX_UPLOAD_MB (500 MB)" in line and "lower" in line

    async def test_a_402_no_credit_keeps_the_row_queued_even_after_a_transcript(self, real_adapter, tmp_path):
        """No credit is about the account, never the file: the run ends and nothing is spent."""
        from datetime import datetime

        for n, day in ((1, 4), (2, 3), (3, 2)):
            await _media(real_adapter, tmp_path, f"m_{n}_voice", download_date=datetime(2026, 1, day))
        config = _config(str(tmp_path))
        client, requests = _provider_client(config, _refusing(402, good={1}))
        stats = await _drain(config, real_adapter, client)
        assert stats == _stats(done=1, refused=1)
        assert len([r for r in requests if r.method == "POST"]) == 2
        assert [r["status"] for r in await _rows(real_adapter, "m_2_voice")] == ["queued"]
        assert await _rows(real_adapter, "m_3_voice") == []


# ============================================================================
# The OpenAI endpoint: plain json for a server that refuses verbose_json
# ============================================================================


class TestPlainJsonFallback:
    async def test_a_400_to_verbose_json_is_asked_once_more_for_json_and_the_run_remembers(
        self, real_adapter, tmp_path
    ):
        """OpenAI's gpt-4o-transcribe family answers only ``json``: ``{text, usage}``."""
        for n in (1, 2):
            await _media(real_adapter, tmp_path, f"m_{n}_voice")

        def gpt4o(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/server"):
                return httpx.Response(404)
            if _field(request, "response_format") != ["json"] or _field(request, "timestamp_granularities[]"):
                error = {
                    "message": "response_format 'verbose_json' is not compatible with model",
                    "type": "invalid_request_error",
                    "param": "response_format",
                    "code": "unsupported_value",
                }
                return httpx.Response(400, json={"error": error})
            return httpx.Response(200, json={"text": "hola", "usage": {"type": "tokens", "total_tokens": 7}})

        config = _config(str(tmp_path), transcription_model="gpt-4o-transcribe")
        client, requests = _provider_client(config, gpt4o)
        stats = await _drain(config, real_adapter, client)
        assert stats["done"] == 2
        posts = [r for r in requests if r.method == "POST"]
        assert [_field(r, "response_format") for r in posts] == [["verbose_json"], ["json"], ["json"]]
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["text"], row["words"], row["models"]) == ("hola", [], ["gpt-4o-transcribe"])

    async def test_a_file_refused_both_ways_is_still_refused(self, tmp_path):
        from src.transcription import TranscriptionError
        from src.transcription_providers.openai import PROVIDER

        config = _config(str(tmp_path))
        client, requests = _provider_client(config, lambda request: httpx.Response(400, json={}))
        path = tmp_path / "a.ogg"
        path.write_bytes(AUDIO)
        with open(path, "rb") as upload, pytest.raises(TranscriptionError) as excinfo:
            await PROVIDER.transcribe(client, upload, "a.ogg", model="whisper-1", prompt=None)
        assert excinfo.value.status == 400
        assert len(requests) == 2
        assert client.verbose is True  # nothing learned from a file refused both ways


# ============================================================================
# The OpenAI result shapes of the servers tested for real
# ============================================================================


class TestResultShapes:
    def test_words_nested_in_segments_are_read_when_there_are_none_on_top(self):
        """whisper.cpp nests words under each segment; the timings were being dropped."""
        payload = {
            "text": " Hola, buenas.",
            "language": "spanish",
            "duration": 17.07,
            "detected_language_probability": 0.97,
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.2,
                    "text": " Hola, buenas.",
                    "words": [
                        {"word": " Hola", "start": 0.0, "end": 0.53, "probability": 0.51},
                        {"word": " buenas.", "start": 0.53, "end": 1.2, "probability": 0.9},
                    ],
                }
            ],
        }
        columns = result_columns(payload, model="whisper-1")
        assert columns["words"] == [
            {"w": " Hola", "s": 0.0, "e": 0.53, "c": 0.51},
            {"w": " buenas.", "s": 0.53, "e": 1.2, "c": 0.9},
        ]
        assert columns["language"] == "es"
        assert columns["language_confidence"] == 0.97
        # Top-level words win when present: never the same word twice.
        both = result_columns({**VERBOSE_JSON, "segments": payload["segments"]}, model="m")
        assert [w["w"] for w in both["words"]] == ["hola", "te"]

    def test_a_speaker_id_is_read_as_the_speaker(self):
        """Mistral's diarized segments name the speaker ``speaker_id``."""
        payload = {"text": "x", "segments": [{"start": 0, "end": 1, "text": "x", "speaker_id": "speaker_1"}]}
        assert result_columns(payload, model="m")["segments"][0]["speaker"] == "speaker_1"

    def test_segments_from_words_follow_the_speaker_runs(self):
        words = [
            {"w": "hi", "s": 0.0, "e": 0.2, "c": None},
            {"w": "there", "s": 0.2, "e": 0.5, "c": None},
            {"w": "yes", "s": 0.6, "e": 0.9, "c": None},
        ]
        assert segments_from_words(words, [None, None, None]) == []
        assert segments_from_words(words, [0, 0, 1]) == [
            {"s": 0.0, "e": 0.5, "text": "hi there", "speaker": "0"},
            {"s": 0.6, "e": 0.9, "text": "yes", "speaker": "1"},
        ]


# ============================================================================
# Deepgram
# ============================================================================

# The response example of https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded,
# with the detect_language fields and a punctuated_word, as smart_format and detect_language add them.
DEEPGRAM_RESULT = {
    "metadata": {
        "request_id": "a847f427-4ad5-4d67-9b95-db801e58251c",
        "channels": 1,
        "duration": 25.933313,
        "models": ["30089e05-99d1-4376-b32e-c263170674af"],
    },
    "results": {
        "channels": [
            {
                "detected_language": "en",
                "language_confidence": 0.99,
                "alternatives": [
                    {
                        "transcript": "Yeah, as much as, it's worth having a talk to the neighbors.",
                        "confidence": 0.9840088,
                        "words": [
                            {
                                "word": "yeah",
                                "punctuated_word": "Yeah,",
                                "start": 0.08,
                                "end": 0.32,
                                "confidence": 0.9975586,
                                "speaker": 0,
                                "speaker_confidence": 0.98,
                            },
                            {"word": "as", "start": 0.4, "end": 0.5, "confidence": 0.9, "speaker": 1},
                        ],
                    }
                ],
            }
        ]
    },
}


def _deepgram(status: int = 200, body: dict | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if status != 200:
            # Deepgram's error shape (developers.deepgram.com/docs/errors).
            return httpx.Response(status, json=body or {"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials."})
        return httpx.Response(200, json=DEEPGRAM_RESULT)

    return handle


class TestDeepgram:
    async def test_the_request_and_the_row(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="deepgram", transcription_diarize=True)
        client, requests = _provider_client(config, _deepgram())
        stats = await _drain(config, real_adapter, client)
        assert stats["done"] == 1
        [request] = requests  # no GET /v1/server: the provider is known
        assert request.method == "POST"
        assert request.url.path == "/base/v1/listen"
        assert request.headers["authorization"] == f"Token {KEY}"
        assert request.headers["content-type"] == "audio/ogg"
        assert request.headers["content-length"] == str(len(AUDIO))
        assert request.content == AUDIO  # the raw file, not multipart
        params = dict(request.url.params)
        assert "token" not in params  # the configured query is never forwarded
        assert params == {
            "model": "nova-3",
            "smart_format": "true",
            "detect_language": "true",
            "diarize_model": "latest",
        }

        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["source"], row["engine_name"]) == ("done", "deepgram", "deepgram")
        assert row["text"] == "Yeah, as much as, it's worth having a talk to the neighbors."
        assert (row["language"], row["language_confidence"]) == ("en", 0.99)
        assert (row["confidence"], row["duration_s"]) == (0.9840088, 25.933313)
        assert row["words"] == [
            {"w": "Yeah,", "s": 0.08, "e": 0.32, "c": 0.9975586},
            {"w": "as", "s": 0.4, "e": 0.5, "c": 0.9},
        ]
        assert [s["speaker"] for s in row["segments"]] == ["0", "1"]
        assert row["models"] == ["nova-3"]
        assert row["diarize"] is True

    async def test_a_language_hint_replaces_detection_and_the_model_is_configurable(self, tmp_path):
        from src.transcription_providers.deepgram import PROVIDER

        config = _config(str(tmp_path), transcription_provider="deepgram", transcription_language="es")
        client, requests = _provider_client(config, _deepgram())
        path = tmp_path / "a.ogg"
        path.write_bytes(AUDIO)
        with open(path, "rb") as upload:
            await PROVIDER.transcribe(client, upload, "a.ogg", model="nova-2", prompt=None)
        assert dict(requests[0].url.params) == {"model": "nova-2", "smart_format": "true", "language": "es"}

    @pytest.mark.parametrize(
        ("status", "body", "outcome"),
        [
            (401, None, _stats(refused=1)),
            (
                402,
                {"err_code": "ASR_PAYMENT_REQUIRED", "err_msg": "Project does not have enough credits"},
                _stats(refused=1),
            ),
            (403, {"err_code": "FORBIDDEN", "err_msg": "no access to the model"}, _stats(refused=1)),
            (400, {"err_code": "Bad Request", "err_msg": "corrupt or unsupported data"}, _stats(failed=1)),
        ],
    )
    async def test_account_refusals_spend_no_row_and_a_bad_file_spends_its_own(
        self, real_adapter, tmp_path, status, body, outcome
    ):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="deepgram")
        client, _ = _provider_client(config, _deepgram(status, body))
        assert await _drain(config, real_adapter, client) == outcome
        expected = "failed" if status == 400 else "queued"
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == [expected]


# ============================================================================
# AssemblyAI
# ============================================================================

UPLOAD_URL = "https://cdn.assemblyai.example/upload/f756988d-47e2-4ca3-96ce-04bb168f8f2a"
TRANSCRIPT_ID = "9ea68fd3-f953-42c1-9742-976c447fb463"

# The response example of https://www.assemblyai.com/docs/api-reference/transcripts/submit.
ASSEMBLYAI_DONE = {
    "id": TRANSCRIPT_ID,
    "status": "completed",
    "text": "Smoke from hundreds of wildfires.",
    "language_code": "en_us",
    "language_confidence": 0.9959,
    "audio_duration": 281,
    "confidence": 0.9404651451800253,
    "words": [
        {"text": "Smoke", "start": 250, "end": 650, "confidence": 0.97465, "speaker": "A"},
        {"text": "from", "start": 730, "end": 880, "confidence": 0.99, "speaker": "A"},
    ],
    "utterances": [
        {"confidence": 0.9359, "end": 26950, "speaker": "A", "start": 250, "text": "Smoke from hundreds of wildfires."}
    ],
}


class AssemblyAIServer:
    """Upload, submit, then ``pending`` polls answering ``processing`` before the final state."""

    def __init__(self, *, pending: int = 2, final: dict | None = None, upload_status: int = 200):
        self.pending = pending
        self.final = final or ASSEMBLYAI_DONE
        self.upload_status = upload_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/v2/upload"):
            if self.upload_status != 200:
                return httpx.Response(
                    self.upload_status, json={"error": "Authentication error, API token missing/invalid"}
                )
            return httpx.Response(200, json={"upload_url": UPLOAD_URL})
        if path.endswith("/v2/transcript") and request.method == "POST":
            return httpx.Response(200, json={"id": TRANSCRIPT_ID, "status": "queued"})
        if path.endswith(f"/v2/transcript/{TRANSCRIPT_ID}"):
            if self.pending > 0:
                self.pending -= 1
                return httpx.Response(200, json={"id": TRANSCRIPT_ID, "status": "processing"})
            return httpx.Response(200, json=self.final)
        return httpx.Response(404)


class TestAssemblyAI:
    async def test_upload_submit_poll_and_the_row(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="assemblyai", transcription_diarize=True)
        server = AssemblyAIServer()
        client, _ = _provider_client(config, server)
        stats = await _drain(config, real_adapter, client)
        assert stats["done"] == 1
        assert [(r.method, r.url.path) for r in server.requests] == [
            ("POST", "/base/v2/upload"),
            ("POST", "/base/v2/transcript"),
            ("GET", f"/base/v2/transcript/{TRANSCRIPT_ID}"),
            ("GET", f"/base/v2/transcript/{TRANSCRIPT_ID}"),
            ("GET", f"/base/v2/transcript/{TRANSCRIPT_ID}"),
        ]
        upload, submit = server.requests[:2]
        for request in server.requests:
            assert request.headers["authorization"] == KEY  # the key itself, no Bearer
        assert upload.content == AUDIO
        assert upload.headers["content-type"] == "application/octet-stream"
        assert json.loads(submit.content) == {
            "audio_url": UPLOAD_URL,
            "speaker_labels": True,
            "language_detection": True,
        }

        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["source"], row["engine_name"]) == ("done", "assemblyai", "assemblyai")
        assert row["text"] == "Smoke from hundreds of wildfires."
        assert (row["language"], row["language_confidence"]) == ("en-us", 0.9959)
        assert row["duration_s"] == 281
        assert row["words"][0] == {"w": "Smoke", "s": 0.25, "e": 0.65, "c": 0.97465}
        assert row["segments"] == [{"s": 0.25, "e": 26.95, "text": "Smoke from hundreds of wildfires.", "speaker": "A"}]
        assert row["models"] == []  # AssemblyAI chose its own

    async def test_a_language_hint_and_a_model_are_sent(self, tmp_path):
        from src.transcription_providers.assemblyai import PROVIDER

        config = _config(str(tmp_path), transcription_language="es")
        server = AssemblyAIServer(pending=0)
        client, _ = _provider_client(config, server)
        path = tmp_path / "a.ogg"
        path.write_bytes(AUDIO)
        with open(path, "rb") as upload:
            columns = await PROVIDER.transcribe(client, upload, "a.ogg", model="universal-2", prompt=None)
        assert json.loads(server.requests[1].content) == {
            "audio_url": UPLOAD_URL,
            "speaker_labels": False,
            "language_code": "es",
            "speech_models": ["universal-2"],
        }
        assert columns["models"] == ["universal-2"]

    async def test_a_transcript_in_error_is_a_failed_row_without_the_message(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="assemblyai")
        final = {"id": TRANSCRIPT_ID, "status": "error", "error": "File does not appear to contain audio."}
        client, _ = _provider_client(config, AssemblyAIServer(final=final))
        stats = await _drain(config, real_adapter, client)
        assert stats["failed"] == 1
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"], row["source"]) == ("failed", "failed", "assemblyai")

    async def test_a_transcript_never_finished_ends_the_run_as_a_stall(self, real_adapter, tmp_path, monkeypatch):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="assemblyai")
        client, _ = _provider_client(config, AssemblyAIServer(pending=10**6))
        monkeypatch.setattr(client, "SYNC_RESPONSE_TIMEOUT_SECONDS", 0.05)
        stats = await _drain(config, real_adapter, client)
        assert stats == _stats(stalled=1)
        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["error"]) == ("failed", "timeout")

    async def test_a_wrong_key_keeps_the_row_queued(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="assemblyai")
        server = AssemblyAIServer(upload_status=401)
        client, _ = _provider_client(config, server)
        assert await _drain(config, real_adapter, client) == _stats(refused=1)
        assert len(server.requests) == 1
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["queued"]


# ============================================================================
# ElevenLabs Scribe
# ============================================================================

# The response example of https://elevenlabs.io/docs/api-reference/speech-to-text/convert,
# with a spacing entry and a second speaker as a diarized answer has them.
ELEVENLABS_RESULT = {
    "language_code": "en",
    "language_probability": 0.98,
    "text": "Hello world!",
    "words": [
        {"text": "Hello", "start": 0, "end": 0.5, "type": "word", "speaker_id": "speaker_1", "logprob": -0.124},
        {"text": " ", "start": 0.5, "end": 0.55, "type": "spacing", "speaker_id": "speaker_1"},
        {"text": "world!", "start": 0.55, "end": 1.0, "type": "word", "speaker_id": "speaker_2", "logprob": -0.2},
    ],
    "transcription_id": "transcript-123",
}


class TestElevenLabs:
    async def test_the_request_and_the_row(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        config = _config(str(tmp_path), transcription_provider="elevenlabs", transcription_diarize=True)
        client, requests = _provider_client(config, lambda request: httpx.Response(200, json=ELEVENLABS_RESULT))
        stats = await _drain(config, real_adapter, client)
        assert stats["done"] == 1
        [request] = requests
        assert request.url.path == "/base/v1/speech-to-text"
        assert request.headers["xi-api-key"] == KEY
        assert "authorization" not in request.headers
        assert _field(request, "model_id") == ["scribe_v2"]
        assert _field(request, "diarize") == ["true"]
        assert _field(request, "timestamps_granularity") == ["word"]
        assert _field(request, "language_code") == []
        assert AUDIO.decode("latin-1") in request.content.decode("latin-1")

        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["source"], row["engine_name"]) == ("done", "elevenlabs", "elevenlabs")
        assert row["text"] == "Hello world!"
        assert (row["language"], row["language_confidence"]) == ("en", 0.98)
        assert row["words"] == [
            {"w": "Hello", "s": 0.0, "e": 0.5, "c": None},
            {"w": "world!", "s": 0.55, "e": 1.0, "c": None},
        ]
        assert [s["speaker"] for s in row["segments"]] == ["speaker_1", "speaker_2"]
        assert row["models"] == ["scribe_v2"]

    async def test_diarize_off_and_a_language_are_stated(self, tmp_path):
        from src.transcription_providers.elevenlabs import PROVIDER

        config = _config(str(tmp_path), transcription_language="es")
        client, requests = _provider_client(config, lambda request: httpx.Response(200, json=ELEVENLABS_RESULT))
        path = tmp_path / "a.ogg"
        path.write_bytes(AUDIO)
        with open(path, "rb") as upload:
            await PROVIDER.transcribe(client, upload, "a.ogg", model="scribe_v1", prompt=None)
        assert _field(requests[0], "diarize") == ["false"]
        assert _field(requests[0], "language_code") == ["es"]
        assert _field(requests[0], "model_id") == ["scribe_v1"]

    async def test_a_validation_error_after_a_transcript_is_a_failed_row(self, real_adapter, tmp_path):
        from datetime import datetime

        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 2))
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        calls = {"n": 0}

        def handle(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=ELEVENLABS_RESULT)
            detail = [{"loc": ["file"], "msg": "error message", "type": "error_type"}]
            return httpx.Response(422, json={"detail": detail})

        config = _config(str(tmp_path), transcription_provider="elevenlabs")
        client, _ = _provider_client(config, handle)
        stats = await _drain(config, real_adapter, client)
        assert (stats["done"], stats["failed"]) == (1, 1)
        [row] = await _rows(real_adapter, "m_2_voice")
        assert (row["status"], row["error"]) == ("failed", "HTTP 422")


# ============================================================================
# Logs
# ============================================================================


@pytest.mark.parametrize("provider", ["deepgram", "assemblyai", "elevenlabs"])
async def test_no_log_line_carries_the_key_or_the_url(real_adapter, tmp_path, caplog, provider):
    await _media(real_adapter, tmp_path, "m_1_voice")
    config = _config(str(tmp_path), transcription_provider=provider)

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {URL}")

    with caplog.at_level(logging.DEBUG):
        client, _ = _provider_client(config, boom)
        await _drain(config, real_adapter, client)
        await _make_stale(real_adapter)
        client, _ = _provider_client(config, lambda request: httpx.Response(401, json={"error": KEY}))
        await _drain(config, real_adapter, client)
    joined = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("src."))
    # The control: both failures were logged.
    assert "Transcription server unreachable" in joined
    assert "Transcription server refused the request" in joined
    assert KEY not in joined
    assert "akou.example.test" not in joined
    assert "token=" not in joined
