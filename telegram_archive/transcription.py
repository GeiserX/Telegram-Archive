"""Automatic voice transcription: the backup side of docs/TRANSCRIPTION.md.

Only the backup process talks to the transcription server. The drain runs
at the end of every backup and the listener calls the same per-media
function the moment it downloads a voice message. Every result is a new row
in ``media_transcripts``; nothing here changes or removes a media row.

``TRANSCRIPTION_PROVIDER`` picks the path. With ``auto``, the default, the
server is asked once per run what it is (``GET /v1/server``). akou
with ``capabilities.jobs`` takes the job path: ``POST /v1/jobs`` with the
audio's SHA-256 as the ``Idempotency-Key`` (``<sha256>.<n>`` on a retry),
then the result arrives by the signed callback into the viewer, by the
event feed the next drain reads, or by the straggler poll. Every other server takes the synchronous path:
the OpenAI transcription endpoint (``POST /v1/audio/transcriptions``),
whose answer is stored at once. ``akou`` insists on the job path,
``openai`` skips the question, and a provider with an adapter of its own
(``telegram_archive/transcription_providers``) replaces the OpenAI request with its own.

Where akou's documents leave a shape open, the shape assumed is written
once: ``ServerInfo``'s ``retain_days`` and ``_error_code`` here, the event
page, a job's outcome and the per-attempt ``Idempotency-Key`` in
``transcription_contract``, which the viewer imports without this module's
HTTP client.

A server that cannot be reached is transient: the row stays ``queued`` and
the ten-minute branch of the drain query resubmits on it, so an outage
never spends the cap of three failed rows. A refusal about the server or
its configuration, not about the file (401, 403, 429, akou's
``preset_unavailable`` and ``callback_not_allowed``, and a 5xx on the job
path), ends the run the same way and also leaves the row ``queued``. Any
other HTTP error is an answer about the file and is stored as a ``failed``
row, and so is a synchronous request the server took and never answered
(a read or write timeout, or a 5xx after every attempt), which also ends
the run. On the synchronous path a 404 is about the server too (an unknown
model or a wrong URL). A 413 is a limit on the way lower than the upload
cap: a file sent as stored is sent again as its Opus track, and a track
still refused is skipped as ``too_large``, so only that file is affected.
Two refused files with none answered end the run: a model or language
setting the server refuses would otherwise fail every file of the backlog.

PII rule: this module never logs a URL (httpx exception strings embed it,
so exceptions log as class names), the bearer key, a media id (it carries
the chat id), a file name or transcript text. Counts and reasons only.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import urllib.parse
import weakref
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, BinaryIO

import httpx

from .media_integrity import REFETCH, repair_media_row
from .message_utils import describe_exception, utcnow_naive
from .realtime import NotificationType, RealtimeNotifier
from .transcription_contract import (
    _SAFE_CODE,
    SOURCE_AKOU,
    _dicts,
    _number,
    apply_job_outcome,
    attempt_key,
    event_data,
    flat_job,
    job_outcome,
    language_tag,
    options_tag,
    parse_events_page,
    real_speaker_label,
)
from .transcription_providers import NATIVE_PROVIDERS
from .transcription_providers import load as load_provider
from .web.media_utils import resolve_stored_media_path

logger = logging.getLogger(__name__)

# A queued row with no job_id older than this was left behind by a process
# that died between the insert and the submit; the drain resubmits on it.
STALE_QUEUED = timedelta(minutes=10)

# ``media_transcripts.source`` of the synchronous path; ``SOURCE_AKOU`` is
# the job path's, in transcription_contract.
SOURCE_SYNC = "openai"

# The OpenAI endpoint's ``model`` for any server that is not akou, unless
# TRANSCRIPTION_MODEL names another. Only akou reads the preset there (a
# preset name or an engine id); an OpenAI-compatible server that validates
# the field would refuse "auto" for good.
DEFAULT_SYNC_MODEL = "whisper-1"

# The seconds between two polls of a provider that transcribes on its side
# (AssemblyAI) inside one synchronous call.
PROVIDER_POLL_SECONDS = 3.0

# akou keeps a job's audio and result this long unless ``GET /v1/server``
# says otherwise (SERVER.md SV-J6, ``server.retain_days``). A row still open
# after it is marked failed with reason ``expired`` and the drain retries it.
DEFAULT_RETAIN_DAYS = 7

# At most this many pages of the event feed per drain; the rest waits for
# the next run, the cursor keeps the place.
MAX_EVENT_PAGES = 20

# ffprobe reads a file's stream headers, not its samples; a file it cannot
# read in this long is sent as if there were no ffprobe.
FFPROBE_TIMEOUT_SECONDS = 30

# ``media_transcripts.error`` of a job akou answered with other options than
# the request asked for (another preset or diarize value).
OPTIONS_MISMATCH = "options_mismatch"

# ``media_transcripts.error`` of a file ffprobe found no audio stream in.
NO_AUDIO_TRACK = "no_audio_track"

# ``media_transcripts.error`` of a file bigger than TRANSCRIPTION_MAX_UPLOAD_MB
# as it would be sent, after its audio track is extracted.
TOO_LARGE = "too_large"

# Voice messages and music are sent as stored: they are audio already and
# small. Everything else (a video, a round video, a file sent as a document)
# is sent as its audio track alone, extracted by ffmpeg, which shrinks a
# 4 GB video to tens of megabytes. Extracting reads the whole file, so it
# gets far longer than ffprobe.
SENT_AS_STORED = frozenset({"voice", "audio"})

# What ``_send`` answers for a 413; ``transcribe_media`` turns it into a retry
# with the audio track, or a ``too_large`` skip. Never a drain outcome.
UPLOAD_TOO_LARGE = "upload_too_large"

# The upload cap of TRANSCRIPTION_PROVIDER=openai when TRANSCRIPTION_MAX_UPLOAD_MB
# is not set: OpenAI and Groq's free tier refuse more than 25 MB.
OPENAI_UPLOAD_MB = 25
EXTRACT_TIMEOUT_SECONDS = 900

# One warning per process and tool when ffprobe or ffmpeg is missing or
# fails; debug after that.
_warned: set[str] = set()

# ffprobe and ffmpeg run in the default executor. At most this many run at
# once, shared by the drain and the listener's immediate path, so a burst of
# videos cannot take every executor thread. One semaphore per event loop.
MEDIA_TOOL_SLOTS = 2
_tool_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _media_tool_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _tool_slots.get(loop)
    if slot is None:
        slot = _tool_slots[loop] = asyncio.Semaphore(MEDIA_TOOL_SLOTS)
    return slot


def _warn_once(tool: str, message: str) -> None:
    if tool in _warned:
        logger.debug(message)
    else:
        _warned.add(tool)
        logger.warning(message)


class TranscriptionError(Exception):
    """A request that failed. ``reason`` is safe to store and to log.

    ``transient`` is True when no HTTP answer came, so the caller leaves
    the row queued instead of storing a failed one. ``stalled`` narrows it:
    the connection was made and the request taken, then it timed out or
    broke. The synchronous path stores that as a failed row, since a
    message the server always times out on would otherwise be resent on
    every run with no cap; the job path keeps it queued, because its
    resubmit with the same ``Idempotency-Key`` finds any job akou made.
    """

    def __init__(
        self, reason: str, *, transient: bool = False, stalled: bool = False, status: int | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient
        self.stalled = stalled
        self.status = status  # the HTTP status of the answer, None when none came


@dataclass(frozen=True)
class ServerInfo:
    """What ``GET /v1/server`` said. Empty name means "not akou, synchronous path"."""

    name: str = ""
    version: str = ""
    jobs: bool = False
    retain_days: int = DEFAULT_RETAIN_DAYS

    @property
    def job_path(self) -> bool:
        return self.name == SOURCE_AKOU and self.jobs


# The transport errors that mean the server was never reached. Any other
# httpx.TransportError came after the connection was made: ``stalled``.
_UNREACHABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# akou's refusals about its configuration, never about one file: a preset it
# cannot run yet (also while it downloads its models) and a callback host off
# the key's allowlist.
_CONFIG_REFUSALS = frozenset({"preset_unavailable", "callback_not_allowed"})


def _server_refused(e: TranscriptionError) -> bool:
    """A refusal about the server or its configuration: a wrong key, no credit, a rate limit, a preset, a callback host.

    Every media of the run would get the same answer, so none of them
    spends a failed row: the row stays queued and the run ends.
    """
    return e.status in (401, 402, 403, 429) or e.reason in _CONFIG_REFUSALS


def _server_failed(e: TranscriptionError) -> bool:
    """A 5xx after every attempt: the server, or a proxy in front of it, is failing."""
    return e.status is not None and e.status >= 500


def _base_url(raw: Any) -> str:
    """Scheme, host and path of the configured URL; query and fragment dropped."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    parts = urllib.parse.urlsplit(raw.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


class TranscriptionClient:
    """The HTTP client: the shape of ``EventWebhookSender`` with its own timeouts.

    A bounded number of attempts, retrying transport errors, HTTP 429 and
    5xx, except a POST the server took and never answered, which is sent
    once; any other status is a permanent answer. Redirects are never
    followed: following one would re-send the bearer key and the audio to a
    host the operator never configured. The upload gets 120 seconds and the
    synchronous answer up to 600, since a long voice message takes a while
    to transcribe.
    """

    ATTEMPTS = 3
    BACKOFFS = (1.0, 4.0)  # seconds before attempts 2 and 3
    DETECT_TIMEOUT_SECONDS = 15.0
    CONNECT_TIMEOUT_SECONDS = 30.0
    UPLOAD_TIMEOUT_SECONDS = 120.0
    SYNC_RESPONSE_TIMEOUT_SECONDS = 600.0
    POLL_TIMEOUT_SECONDS = 60.0

    def __init__(self, config, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Defensive getattr + type checks, as the webhook sender: tests build
        # configs as bare MagicMock whose attributes are truthy.
        self._base_url = _base_url(getattr(config, "transcription_url", None))
        provider_name = getattr(config, "transcription_provider", None)
        self.provider_name = provider_name if isinstance(provider_name, str) and provider_name else "auto"
        self.provider = load_provider(self.provider_name)
        key = getattr(config, "transcription_api_key", None)
        self._headers = self.provider.auth_headers(key) if isinstance(key, str) and key else {}
        model = getattr(config, "transcription_model", None)
        self.model = model if isinstance(model, str) and model else self.provider.default_model
        preset = getattr(config, "transcription_preset", None)
        self.preset = preset if isinstance(preset, str) else "auto"
        language = getattr(config, "transcription_language", None)
        self.language = language if isinstance(language, str) else ""
        self.diarize = getattr(config, "transcription_diarize", None) is True
        self.backoffs = self.BACKOFFS
        self.poll_interval = PROVIDER_POLL_SECONDS
        self._transport = transport
        # Per run: the OpenAI endpoint still takes verbose_json (see the openai
        # provider); the server has answered a file (a transcript, or a job it
        # took); and how many files it refused with a 4xx (413 skips included).
        self.verbose = True
        self.answered = False
        self.file_refusals = 0

    @property
    def sync_source(self) -> str:
        """The ``source`` a synchronous row gets before the server is known."""
        return self.provider.name if self.provider.name in NATIVE_PROVIDERS else SOURCE_SYNC

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def answer_source(self, server: ServerInfo) -> str:
        """``media_transcripts.source`` of an answer: akou's job path, else what this client sends through.

        Read from the configured provider, never from the name a server
        gives itself: under ``auto`` a server calling itself ``deepgram``
        still got an OpenAI-endpoint request.
        """
        return SOURCE_AKOU if server.job_path else self.sync_source

    def diarizes(self, server: ServerInfo) -> bool:
        """Whether TRANSCRIPTION_DIARIZE reaches the server: akou's job path, or an adapter that diarizes."""
        return server.job_path or (self.provider.name in NATIVE_PROVIDERS and self.provider.diarizes)

    def job_options(self) -> dict[str, Any]:
        """The options a job request sends, exactly as sent: the form fields and the key both read this."""
        return {"preset": self.preset or "auto", "language": self.language or "auto", "diarize": self.diarize}

    def request_options(self, server: ServerInfo, config) -> dict[str, Any]:
        """Every option this server's answer depends on, the base of a row's ``options_tag``.

        On akou's job path what the job sends and its key names; on the
        synchronous path and the adapters the provider, model, language
        hint, diarize flag and hotword prompt.
        """
        if server.job_path:
            return self.job_options()
        return {
            "provider": self.provider.name,
            "model": self.sync_model(server),
            "language": self.language or "auto",
            "diarize": self.diarize and self.diarizes(server),
            # Only the OpenAI endpoint sends the hotword prompt; for an adapter a
            # TRANSCRIPTION_HOTWORDS change must not block its copies.
            "prompt": "" if self.provider.name in NATIVE_PROVIDERS else (_prompt_for(config) or ""),
            # The OpenAI endpoint is one source for OpenAI, Groq, Mistral and
            # every self-hosted server: the host tells their answers apart.
            "host": urllib.parse.urlsplit(self._base_url).netloc.lower(),
        }

    def sync_model(self, server: ServerInfo) -> str:
        """The synchronous ``model``: the preset for akou, else TRANSCRIPTION_MODEL or the provider's default."""
        if server.name == SOURCE_AKOU and self.preset:
            return self.preset
        return self.model

    def sync_timeout(self) -> httpx.Timeout:
        """An upload whose answer is the transcript: 600 seconds for the answer."""
        return httpx.Timeout(
            self.SYNC_RESPONSE_TIMEOUT_SECONDS,
            connect=self.CONNECT_TIMEOUT_SECONDS,
            write=self.UPLOAD_TIMEOUT_SECONDS,
        )

    def poll_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.POLL_TIMEOUT_SECONDS, connect=self.CONNECT_TIMEOUT_SECONDS)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _client(self, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers, timeout=timeout, follow_redirects=False, transport=self._transport
        )

    async def detect_server(self) -> ServerInfo:
        """``GET /v1/server`` once per run.

        akou answers ``name``, ``version`` and ``capabilities.jobs``; every
        other field or flag is ignored. A 404, a non-JSON body or any other
        shape selects the synchronous path. Raises a transient
        ``TranscriptionError`` only when the server cannot be reached at all.
        ``TRANSCRIPTION_PROVIDER=openai`` and the native providers ask
        nothing: the answer is known.
        """
        if self.provider_name in NATIVE_PROVIDERS:
            return ServerInfo(name=self.provider_name)
        if self.provider_name == "openai":
            return ServerInfo()
        async with self._client(self.DETECT_TIMEOUT_SECONDS) as client:
            try:
                response = await client.get(self._url("/v1/server"))
            except httpx.TransportError as e:
                raise TranscriptionError(type(e).__name__, transient=True) from None
        if response.status_code != 200:
            return ServerInfo()
        try:
            payload = response.json()
        except ValueError:
            return ServerInfo()
        if not isinstance(payload, dict):
            return ServerInfo()
        name = payload.get("name")
        version = payload.get("version")
        capabilities = payload.get("capabilities")
        # Assumed: a top-level integer ``retain_days`` when akou reports its
        # retention; SV-K1 does not list it, so the default applies without.
        retain_days = payload.get("retain_days")
        if isinstance(retain_days, bool) or not isinstance(retain_days, int) or retain_days < 1:
            retain_days = DEFAULT_RETAIN_DAYS
        return ServerInfo(
            name=name if isinstance(name, str) else "",
            version=str(version) if isinstance(version, (str, int, float)) and not isinstance(version, bool) else "",
            jobs=isinstance(capabilities, dict) and capabilities.get("jobs") is True,
            retain_days=retain_days,
        )

    async def transcribe(
        self,
        audio: bytes | BinaryIO,
        filename: str,
        *,
        model: str,
        prompt: str | None = None,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """``POST /v1/audio/transcriptions`` and return the ``verbose_json`` answer.

        Multipart with ``model`` (see ``sync_model``), ``response_format=verbose_json``
        and ``timestamp_granularities[]`` as both ``word`` and ``segment`` (a
        server that follows OpenAI's rule returns segments only when asked),
        plus the configured language
        and the hotword prompt when any. ``verbose=False`` asks for plain
        ``json`` with no timings instead. Raises ``TranscriptionError`` with
        a reason that names no URL; it is transient when every attempt was
        a transport failure, permanent when the server answered.
        """
        data: dict[str, Any] = {"model": model, "response_format": "json"}
        if verbose:
            data["response_format"] = "verbose_json"
            data["timestamp_granularities[]"] = ["word", "segment"]
        if self.language:
            data["language"] = self.language
        if prompt:
            data["prompt"] = prompt
        files = {"file": (filename, audio, "application/octet-stream")}
        return await self.request_json(
            "POST", "/v1/audio/transcriptions", timeout=self.sync_timeout(), data=data, files=files
        )

    async def request_json(
        self, method: str, path: str, *, timeout: float | httpx.Timeout, body: BinaryIO | None = None, **kwargs
    ) -> dict[str, Any]:
        """One request to ``TRANSCRIPTION_URL`` + ``path`` and its JSON object; what a provider sends through.

        ``body`` is an open file sent as the raw request body, streamed from
        disk and rewound on every attempt. Any answer of 300 or more raises
        ``TranscriptionError`` with its status (a redirect lands here too).
        """
        response = await self._send(method, path, timeout=timeout, body=body, **kwargs)
        if response.status_code >= 300:
            raise TranscriptionError(_error_code(response), status=response.status_code)
        return _json_object(response)

    async def _send(
        self, method: str, path: str, *, timeout: float | httpx.Timeout, body: BinaryIO | None = None, **kwargs
    ) -> httpx.Response:
        """One request with bounded attempts; the first answer that is not 429 or 5xx.

        Transport errors, 429 and 5xx are retried, except a POST the server
        took and never answered: another attempt would wait out the same
        timeout and hand the server the same work again. Raises a transient
        ``TranscriptionError`` when every attempt failed to connect or the
        POST stalled, and a permanent one (``HTTP <code>``, with ``status``)
        when the last answer was 429 or 5xx. Any other answer, a 2xx, a 3xx
        or a 4xx, is returned for the caller to read.
        """
        reason = "unknown"
        transient = stalled = False
        status = None
        async with self._client(timeout) as client:
            for attempt in range(self.ATTEMPTS):
                if body is not None:
                    kwargs["content"] = _file_chunks(body)
                    kwargs["headers"] = {**kwargs.get("headers", {}), "Content-Length": str(_file_size(body))}
                try:
                    response = await client.request(method, self._url(path), **kwargs)
                except httpx.TransportError as e:
                    reason = type(e).__name__
                    transient = True
                    stalled = not isinstance(e, _UNREACHABLE)
                    status = None
                    if stalled and method == "POST":
                        break
                else:
                    transient = stalled = False
                    if response.status_code != 429 and response.status_code < 500:
                        return response
                    status = response.status_code
                    reason = f"HTTP {status}"
                if attempt < self.ATTEMPTS - 1:
                    await asyncio.sleep(self.backoffs[attempt])
        raise TranscriptionError(reason, transient=transient, stalled=stalled, status=status)

    # ------------------------------------------------------------------
    # The akou job path (SERVER.md sections 5 and 6)
    # ------------------------------------------------------------------

    async def submit_job(
        self,
        audio: bytes | BinaryIO,
        filename: str,
        *,
        content_hash: str,
        callback_url: str | None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/jobs`` and return the job akou answered.

        ``202`` for a new job, ``200`` with the existing job, in whatever
        state it is, for the same ``Idempotency-Key`` and the same file; the
        caller branches on ``status`` and never on the code. The key is
        ``idempotency_key`` (see ``attempt_key``), the bare hash by default;
        ``metadata.content_hash`` is always the bare hash. Every /v1 route
        refuses unknown fields with 400, so only the documented fields go
        out and ``wait`` never does. A 4xx raises with akou's error code when
        the body carries one (``callback_not_allowed``,
        ``idempotency_conflict``), otherwise ``HTTP <code>``.
        """
        options = self.job_options()
        data = {
            "preset": options["preset"],
            "language": options["language"],
            "metadata": json.dumps({"content_hash": content_hash}),
        }
        if callback_url:
            data["callback_url"] = callback_url
        # Always stated, false included: the archive's setting is global and
        # must win over any server-side default akou may have.
        data["diarize"] = "true" if options["diarize"] else "false"
        files = {"file": (filename, audio, "application/octet-stream")}
        timeout = httpx.Timeout(self.UPLOAD_TIMEOUT_SECONDS, connect=self.CONNECT_TIMEOUT_SECONDS)
        response = await self._send(
            "POST",
            "/v1/jobs",
            timeout=timeout,
            data=data,
            files=files,
            headers={"Idempotency-Key": idempotency_key or content_hash},
        )
        if response.status_code >= 300:
            raise TranscriptionError(_error_code(response), status=response.status_code)
        return _json_object(response)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """``GET /v1/jobs/{id}`` without ``wait``: the job's current state."""
        return await self._get_json(f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}")

    async def get_result(self, job_id: str) -> dict[str, Any]:
        """``GET /v1/jobs/{id}/result``: the result fields flat, with ``job_id``, ``status`` and ``metadata``."""
        return await self._get_json(f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/result")

    async def get_events(self, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """``GET /v1/events?after=<cursor>``: one page of the key's events, oldest first, and the next cursor.

        Without a cursor the first read starts at the beginning of the feed.
        """
        params = {"after": cursor} if cursor else None
        return parse_events_page(await self._get_json("/v1/events", params=params))

    async def _get_json(self, path: str, **kwargs) -> dict[str, Any]:
        timeout = httpx.Timeout(self.POLL_TIMEOUT_SECONDS, connect=self.CONNECT_TIMEOUT_SECONDS)
        response = await self._send("GET", path, timeout=timeout, **kwargs)
        if response.status_code == 404:
            raise TranscriptionError("not_found", status=404)
        if response.status_code >= 300:
            raise TranscriptionError(_error_code(response), status=response.status_code)
        return _json_object(response)


async def _file_chunks(handle: BinaryIO):
    """An open file from its start, in chunks: a raw request body that never sits in memory."""
    handle.seek(0)
    while chunk := handle.read(1024 * 1024):
        yield chunk


def _file_size(handle: BinaryIO) -> int:
    return os.fstat(handle.fileno()).st_size


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise TranscriptionError("invalid_json") from None
    if not isinstance(payload, dict):
        raise TranscriptionError("invalid_json")
    return payload


def _error_code(response: httpx.Response) -> str:
    """akou's error code for a refused request, or ``HTTP <code>``.

    akou's one error shape is ``{error: <code>, message, ...}``; the code is
    a short snake_case token, safe to store and to log. The message is
    server text and is neither stored nor logged.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    code = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(code, dict):
        code = code.get("code")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return f"HTTP {response.status_code}"


def _prompt_for(config) -> str | None:
    """The hotword prompt: TRANSCRIPTION_HOTWORDS joined, sent as ``prompt`` on the OpenAI endpoint."""
    hotwords = getattr(config, "transcription_hotwords", None)
    if not isinstance(hotwords, (list, tuple)):
        return None
    words = [w.strip() for w in hotwords if isinstance(w, str) and w.strip()]
    return ", ".join(words) or None


def result_columns(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Map a ``verbose_json`` answer onto the transcript row's columns.

    Words come from the top-level ``words``, or from each segment's own
    ``words`` when there is none (whisper.cpp nests them there). A segment's
    speaker is ``speaker``, or ``speaker_id`` (Mistral). whisper.cpp's
    ``detected_language_probability`` is the language confidence.
    """
    raw_words = _dicts(payload.get("words"))
    if not raw_words:
        raw_words = [w for seg in _dicts(payload.get("segments")) for w in _dicts(seg.get("words"))]
    words = [
        {
            "w": w.get("word"),
            "s": _number(w.get("start")),
            "e": _number(w.get("end")),
            "c": _number(w.get("probability")),
        }
        for w in raw_words
    ]
    segments = [
        {
            "s": _number(seg.get("start")),
            "e": _number(seg.get("end")),
            "text": seg.get("text") if isinstance(seg.get("text"), str) else "",
            "speaker": _speaker(seg),
        }
        for seg in _dicts(payload.get("segments"))
    ]
    text = payload.get("text")
    return {
        "text": text if isinstance(text, str) else "",
        "language": language_tag(payload.get("language")),
        "language_confidence": _number(payload.get("detected_language_probability")),
        "duration_s": _number(payload.get("duration")),
        "words": words,
        "segments": segments,
        "models": [model],
    }


def _speaker(segment: dict[str, Any]) -> str | None:
    for name in ("speaker", "speaker_id"):
        if isinstance(segment.get(name), str):
            return segment[name]
    return None


def _file_sha256(path: str) -> str:
    """The SHA-256 of a file, read in chunks: a video can be gigabytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AudioProbe:
    """What ffprobe said about a stored file: an audio stream or not, and its duration if known."""

    has_audio: bool
    duration: float | None = None


def _run_ffprobe(path: str) -> AudioProbe | None:
    """ffprobe on one stored file; None when ffprobe is missing, fails or times out.

    A fixed argument list and no shell: the path is the only variable and
    goes after ``-i``. Blocking, so the caller runs it off the event loop.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            "-i",
            path,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or b"{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("streams"), list):
        return None  # nothing said about the streams: unknown, not "no audio"
    streams = _dicts(payload.get("streams"))
    fmt = payload.get("format")
    duration = None
    if isinstance(fmt, dict):
        try:
            duration = float(fmt.get("duration"))
        except TypeError, ValueError:
            duration = None
        if duration is not None and not duration > 0:
            duration = None
    return AudioProbe(has_audio=any(s.get("codec_type") == "audio" for s in streams), duration=duration)


async def probe_audio(path: str) -> AudioProbe | None:
    """``_run_ffprobe`` off the event loop. None means "unknown": the caller sends the file anyway.

    Logs one warning per process when ffprobe is missing or fails, never
    the path (the exception strings of subprocess carry the full argv).
    """
    try:
        async with _media_tool_slot():
            probe = await asyncio.to_thread(_run_ffprobe, path)
        reason = "failed"
    except FileNotFoundError:
        probe, reason = None, "not installed"
    except (OSError, subprocess.SubprocessError) as e:
        probe, reason = None, type(e).__name__
    if probe is None:
        _warn_once("ffprobe", f"Transcription: ffprobe {reason}; sending the file without the audio check")
    return probe


def _run_extract(path: str, dest: str) -> bool:
    """ffmpeg writes the audio track of ``path`` to ``dest`` as 16 kHz mono Opus; True when it did.

    A fixed argument list and no shell, like ``_run_ffprobe``. The bitexact
    flags make the output the same on every run of the same ffmpeg build:
    the Ogg muxer otherwise picks a random stream serial.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            path,
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-f",
            "ogg",
            dest,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode == 0 and os.path.getsize(dest) > 0


async def extract_audio(path: str) -> str | None:
    """The audio track of ``path`` in a temporary ``.ogg`` file, or None to send the stored file.

    Runs off the event loop; the caller deletes the file. A missing or
    failing ffmpeg logs one warning per process, never the path, and the
    stored file goes out instead, under the same size limit.
    """
    fd, dest = tempfile.mkstemp(prefix="transcription-", suffix=".ogg")
    os.close(fd)
    kept = False
    try:
        try:
            async with _media_tool_slot():
                extracted = await asyncio.to_thread(_run_extract, path, dest)
            if extracted:
                kept = True
                return dest
            reason = "failed"
        except FileNotFoundError:
            reason = "not installed"
        except (OSError, subprocess.SubprocessError) as e:
            reason = type(e).__name__
        _warn_once("ffmpeg", f"Transcription: ffmpeg {reason}; sending the stored file instead of its audio track")
        return None
    finally:
        # Also on cancellation, which none of the handlers above catch.
        if not kept:
            _remove(dest)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _notify(notifier, media: dict[str, Any], transcript_id: int, status: str, account_id: int) -> None:
    """Push ids and status only; the browser fetches the rows itself."""
    if notifier is None or media.get("chat_id") is None:
        return
    data = {
        "account_id": account_id,
        "chat_id": media.get("chat_id"),
        "message_id": media.get("message_id"),
        "media_id": media.get("id"),
        "transcript_id": transcript_id,
        "status": status,
    }
    try:
        await notifier.notify(NotificationType.TRANSCRIPT, media["chat_id"], data, account_id=account_id)
    except Exception as e:
        logger.debug(f"Transcript notification failed: {type(e).__name__}")


async def _notify_rows(db, notifier, rows: list[dict[str, Any]]) -> None:
    """Push each filled row; the media row gives the chat and message ids."""
    if notifier is None:
        return
    for row in rows:
        media = await db.get_media_by_id(row["media_id"], account_id=row["account_id"])
        if media is not None:
            await _notify(notifier, media, row["id"], row["status"], row["account_id"])


async def _free_job_id(db, row: dict[str, Any], job_id: str | None) -> str | None:
    """``job_id``, or None when another row of the same media already holds it.

    The unique index on ``(account_id, media_id, job_id)`` refuses a second
    row with the same job, and a refused write would abort the drain.
    """
    if not job_id:
        return None
    siblings = await db.list_media_transcripts(row["media_id"], account_id=row["account_id"])
    if any(s["job_id"] == job_id and s["id"] != row["id"] for s in siblings):
        return None
    return job_id


async def _store_job_outcome(
    db, row: dict[str, Any], data: dict[str, Any], *, idempotency_key: str, server: ServerInfo
) -> list[dict[str, Any]]:
    """Store a finished job for ``row`` and every open twin; the rows filled.

    Twins go through ``apply_job_outcome`` and its row rule. The row itself
    is filled directly when the row rule held it back: that happens when
    akou answers with a job an earlier row of this archive already
    finished, for example the same failed or cancelled job returned again
    for the same ``Idempotency-Key``. The job id is written only when no
    other row of the same media holds it (the unique index on
    ``(account_id, media_id, job_id)``).
    """
    outcome = job_outcome(data, engine_version=server.version)
    if outcome is None:
        return []
    filled = await apply_job_outcome(db, data, content_hash=idempotency_key, engine_version=server.version)
    if any(r["id"] == row["id"] for r in filled):
        return filled
    status, columns = outcome
    job_id = await _free_job_id(db, row, data.get("job_id") if isinstance(data.get("job_id"), str) else None)
    if await db.fill_media_transcript(row["id"], status=status, job_id=job_id, **columns):
        filled.append({"id": row["id"], "account_id": row["account_id"], "media_id": row["media_id"], "status": status})
    return filled


def _options_disagree(job: dict[str, Any], sent: dict[str, Any]) -> bool:
    """True when akou's job answer names other options than the request sent.

    ``diarize`` is compared when akou reports it as a JSON boolean, which
    it always does; anything else counts as not reported. The preset is
    compared only when the request named one: akou resolves ``auto`` to the
    preset it will run before it stores the job, so an ``auto`` request
    answered with ``fast`` agrees. An answer that carries neither field (an
    older akou) never disagrees.
    """
    answered = job.get("diarize")
    if isinstance(answered, bool) and answered != bool(sent["diarize"]):
        return True
    preset = job.get("preset")
    return sent["preset"] != "auto" and isinstance(preset, str) and preset != sent["preset"]


async def _submit_job(
    config,
    db,
    media: dict[str, Any],
    row: dict[str, Any],
    audio: bytes | BinaryIO,
    filename: str,
    idempotency_key: str,
    *,
    account_id: int,
    client: TranscriptionClient,
    server: ServerInfo,
    notifier,
    upload_key: str | None = None,
) -> str:
    """``POST /v1/jobs`` for one queued row and branch on the answer's ``status``.

    ``idempotency_key`` is the stored file's hash: ``metadata.content_hash``
    and the row are matched on it. ``upload_key`` is the hash of the bytes
    sent when they are an extracted audio track, and the ``Idempotency-Key``
    header derives from it: akou refuses a key it has seen with another
    file, and another ffmpeg build may extract other bytes from the same file.

    ``queued`` or ``running``: the row stores the job id and that status,
    and the result comes later by the callback, the event feed or the
    straggler poll (``submitted``). ``done``: the completed event is
    behind the cursor and no callback will come, so the row is stored now
    from the nested result, or from the result route when the answer
    carries none. ``failed`` or ``cancelled``: a failed row with the reason.
    A refusal about the server or its configuration, or a 5xx, stores
    nothing: the row stays queued and the run ends (``refused``). Only an
    answer about this file spends a failed row.
    """
    row = {**row, "account_id": account_id, "media_id": media["id"]}
    callback_url = getattr(config, "transcription_callback_url", None)
    earlier = sum(
        1
        for other in await db.list_media_transcripts(media["id"], account_id=account_id)
        if other["id"] != row["id"] and other["status"] in ("done", "failed")
    )
    try:
        job = await client.submit_job(
            audio,
            filename,
            content_hash=idempotency_key,
            callback_url=callback_url if isinstance(callback_url, str) and callback_url else None,
            idempotency_key=attempt_key(upload_key or idempotency_key, earlier, client.job_options()),
        )
    except TranscriptionError as e:
        if e.transient:
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"
        if e.status == 413:
            return UPLOAD_TOO_LARGE
        if _server_refused(e) or _server_failed(e):
            # Every other submit of this run would get the same answer, and
            # none of it is about this file: no failed row, the run ends.
            if e.reason == "callback_not_allowed":
                logger.warning(
                    "Transcription: akou refused TRANSCRIPTION_CALLBACK_URL (callback_not_allowed); "
                    "add its host to the key's callback-host allowlist or unset it. Ending this run"
                )
            else:
                logger.warning(f"Transcription server refused the job ({e.reason}); the media stays queued")
            return "refused"
        if e.status is not None and 400 <= e.status < 500:
            client.file_refusals += 1
        await db.fill_media_transcript(row["id"], status="failed", error=e.reason, source=SOURCE_AKOU)
        await _notify(notifier, media, row["id"], "failed", account_id)
        logger.warning(f"Transcription job refused ({e.reason})")
        return "failed"

    client.answered = True
    data = flat_job(job)
    job_id = data.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        await db.fill_media_transcript(row["id"], status="failed", error="invalid_job", source=SOURCE_AKOU)
        await _notify(notifier, media, row["id"], "failed", account_id)
        return "failed"
    if _options_disagree(job, client.job_options()):
        # akou answered a job made with other options, an old one under a
        # reused key: storing it would label an undiarized answer as
        # diarized, or one preset's answer as another's. A failed row, so the
        # retry sends the next key and names a new job.
        await db.fill_media_transcript(row["id"], status="failed", error=OPTIONS_MISMATCH, source=SOURCE_AKOU)
        await _notify(notifier, media, row["id"], "failed", account_id)
        logger.warning("Transcription job answered with other options than asked; stored as failed")
        return "failed"
    status = data.get("status")
    if status == "done" and not isinstance(data.get("text"), str):
        try:
            data = await client.get_result(job_id)
        except TranscriptionError as e:
            # The straggler poll fetches it on a later drain.
            logger.warning(f"Transcription result not read yet ({e.reason})")
            if await _free_job_id(db, row, job_id):
                await db.fill_media_transcript(row["id"], status="running", job_id=job_id, source=SOURCE_AKOU)
            return "submitted"
    outcome = job_outcome(data, engine_version=server.version)
    if outcome is None:
        # Still open, or a status this client does not know: keep the job id
        # and let the callback, the event feed or the straggler poll finish it.
        # A job an earlier row of this media already holds is left to that
        # row; this one stays queued without it and is resubmitted later.
        if await _free_job_id(db, row, job_id):
            await db.fill_media_transcript(
                row["id"], status="running" if status == "running" else "queued", job_id=job_id, source=SOURCE_AKOU
            )
        return "submitted"
    filled = await _store_job_outcome(
        db, row, {**data, "job_id": job_id}, idempotency_key=idempotency_key, server=server
    )
    await _notify_rows(db, notifier, filled)
    return outcome[0]


async def reconcile_events(db, client: TranscriptionClient, server: ServerInfo, *, notifier=None) -> int:
    """Read the event feed after the stored cursor and store every outcome; the rows filled.

    ``transcription.completed`` and ``transcription.failed`` fill every
    open row whose ``idempotency_key`` is ``data.metadata.content_hash``;
    ``transcription.cancelled`` stores ``failed`` with reason ``cancelled``;
    any other type is skipped. The cursor advances after each page, so a
    crash re-reads at most one page, and a re-read writes nothing by the
    row rule. This is what makes an unreachable callback URL harmless.
    """
    cursor = await db.get_transcription_events_cursor()
    filled_total = 0
    for _ in range(MAX_EVENT_PAGES):
        events, next_cursor = await client.get_events(cursor)
        for event in events:
            data = event_data(event)
            if data is None:
                continue
            if (
                data["status"] == "done"
                and not isinstance(data.get("text"), str)
                and isinstance(data.get("job_id"), str)
            ):
                # The large-result form carries ``result_url`` instead of the text.
                try:
                    result = await client.get_result(data["job_id"])
                except TranscriptionError as e:
                    if e.transient or _server_refused(e) or _server_failed(e):
                        raise
                    continue  # gone or unreadable; the straggler poll or expiry ends the row
                data = {**result, "status": "done", "metadata": result.get("metadata") or data.get("metadata")}
            filled = await apply_job_outcome(db, data, engine_version=server.version)
            filled_total += len(filled)
            await _notify_rows(db, notifier, filled)
        if next_cursor is None or next_cursor == cursor:
            break
        await db.set_transcription_events_cursor(next_cursor)
        cursor = next_cursor
        if not events:
            break
    return filled_total


async def poll_stragglers(
    db, client: TranscriptionClient, server: ServerInfo, *, account_id: int, notifier=None
) -> int:
    """Ask akou about every job stored more than ten minutes ago; the rows finished.

    Both ages count from ``job_stored_at``, when the row got its job id, not
    from the insert: a row can wait queued through an outage first. A job
    stored longer ago than the server's retention is marked failed with
    reason ``expired`` without a request, since akou has deleted it by then,
    and the drain query retries it. A job akou no longer knows is failed
    with reason ``not_found`` and retried the same way. A ``done`` job
    whose answer carries no result is read from the result route.
    """
    now = utcnow_naive()
    expired_before = now - timedelta(days=server.retain_days)
    finished = 0
    for row in await db.get_open_job_transcripts(account_id=account_id, stored_before=now - STALE_QUEUED):
        if (row.get("job_stored_at") or row["requested_at"]) < expired_before:
            if await db.fill_media_transcript(row["id"], status="failed", error="expired"):
                finished += 1
                await _notify_rows(db, notifier, [{**row, "status": "failed"}])
            continue
        try:
            data = flat_job(await client.get_job(row["job_id"]))
            if data.get("status") == "done" and not isinstance(data.get("text"), str):
                data = await client.get_result(row["job_id"])
        except TranscriptionError as e:
            if e.transient or _server_refused(e) or _server_failed(e):
                raise
            if e.reason == "not_found" and await db.fill_media_transcript(
                row["id"], status="failed", error="not_found"
            ):
                finished += 1
                await _notify_rows(db, notifier, [{**row, "status": "failed"}])
            continue
        if data.get("status") == "running" and row["status"] == "queued":
            await db.fill_media_transcript(row["id"], status="running")
            continue
        filled = await _store_job_outcome(
            db, row, {**data, "job_id": row["job_id"]}, idempotency_key=row["idempotency_key"], server=server
        )
        finished += len(filled)
        await _notify_rows(db, notifier, filled)
    return finished


# What a copy takes from the row it copies: the answer, never the job.
COPIED_COLUMNS = (
    "source",
    "engine_name",
    "engine_version",
    "models",
    "language",
    "language_confidence",
    "text",
    "words",
    "segments",
    "confidence",
    "duration_s",
    "diarize",
    "options_tag",
)


def _answer_origin(client: TranscriptionClient, server: ServerInfo) -> tuple[str, str]:
    """The ``source`` and ``engine_name`` a row gets from this server, on either path."""
    if server.job_path:
        return SOURCE_AKOU, SOURCE_AKOU
    return client.answer_source(server), server.name or SOURCE_SYNC


async def _copy_transcript(
    config,
    db,
    media: dict[str, Any],
    content_hash: str,
    *,
    account_id: int,
    client: TranscriptionClient,
    server: ServerInfo | None,
    notifier,
) -> str | None:
    """Store this media's transcript as a copy of the same audio's, from any account; None when there is none.

    A ``done`` row for the same stored file that this server would give
    again (same source and engine, same preset, speakers asked for or not
    alike) is copied, and nothing is probed, extracted or sent. The copy is
    this media's own row (the open one when a press is waiting), under its
    own account, so search and exports find it there; ``copied_from_id``
    names the source. A media with a done row of its own is never copied
    into: a press after a done transcript asks the server for a new one,
    even when a twin under another media holds a copy of the old one.
    """
    lookup = {"account_id": account_id, "media_id": media["id"]}
    if server is None:
        # The listener's call, before the server is known: ask it only when a
        # tagged row could be copied at all (openai and the adapters answer
        # without a request).
        loose = await db.find_copyable_transcript(
            content_hash, client.preset, diarize=client.diarize, tagged_only=True, **lookup
        )
        if loose is None:
            return None
        try:
            server = await client.detect_server()
        except TranscriptionError:
            return None  # the send path below meets the same outage and says so
    source, engine_name = _answer_origin(client, server)
    found = await db.find_copyable_transcript(
        content_hash,
        client.preset,
        diarize=client.diarize and client.diarizes(server),
        source=source,
        engine_name=engine_name,
        options_tag=options_tag(client.request_options(server, config)),
        **lookup,
    )
    if found is None:
        return None
    if found.get("diarize") is True and not any(
        real_speaker_label(segment.get("speaker")) for segment in _dicts(found.get("segments"))
    ):
        # Marked diarized with no speaker in it: 8.16.0 stored an undiarized
        # job that way under a reused key. It stays as it is, but is not
        # reused as a diarized answer; the file goes to the server instead.
        return None
    if any(row["status"] == "done" for row in await db.list_media_transcripts(media["id"], account_id=account_id)):
        return None
    row = await db.enqueue_media_transcript(
        media["id"],
        account_id=account_id,
        content_hash=content_hash,
        idempotency_key=content_hash,
        preset=client.preset,
    )
    if row is None or row["status"] != "queued" or row.get("job_id"):
        return "noop"
    await db.fill_media_transcript(
        row["id"],
        status="done",
        preset=client.preset,
        content_hash=content_hash,
        idempotency_key=content_hash,
        copied_from_id=found["id"],
        **{name: found[name] for name in COPIED_COLUMNS},
    )
    await _notify(notifier, media, row["id"], "done", account_id)
    return "copied"


async def transcribe_media(
    config,
    db,
    media: dict[str, Any],
    *,
    account_id: int,
    client: TranscriptionClient | None = None,
    server: ServerInfo | None = None,
    notifier: RealtimeNotifier | None = None,
) -> str:
    """One media to the server; the drain and the listener both call this.

    Returns ``done``, ``failed``, ``skipped``, ``submitted``, ``refused``,
    ``unreachable``, ``stalled``, ``copied``, ``refetch`` or ``noop``. The queued row is inserted first
    (insert-if-absent, so a second call while one is open reuses it), the
    audio is hashed when the media row carries no hash, and the answer
    fills the same row: at once on the synchronous path, or with the job
    id on the akou job path (``submitted``), where the result arrives
    later. The row stays ``queued`` during the request on purpose: a
    process that dies mid-request, and a server that cannot be reached
    (``unreachable``), both leave a row the next drain resubmits after ten
    minutes, with no failed row added. ``stalled`` is a synchronous request
    the server took and gave no usable answer (a timeout, or a 5xx after
    every attempt): the row is failed, so the cap of three applies, and the
    drain ends the run. ``refused`` is a refusal about the server or its
    configuration (see ``_server_refused``; on the synchronous path a 404,
    on the job path a 5xx): the row stays queued, no failed row is spent,
    and the drain ends the run. A 413 is sent again as the Opus audio track
    when the file went out as stored, and ``skipped`` with ``too_large``
    when the track itself is refused: only that file is affected.

    A media whose file is not at its path is repaired first, before any row
    is queued: a copy already on disk is put back (``media_integrity``), and
    a media with no copy anywhere is marked not downloaded and answers
    ``refetch``. The next backup downloads it again and a later drain sends
    it, so no failed row is spent on a file the archive can still get.
    ``failed`` with ``file_missing`` is left for an entry that exists but
    cannot be read from here.
    """
    client = client or TranscriptionClient(config)
    if not client.configured:
        return "noop"
    media_id = media["id"]
    content_hash = media.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        content_hash = None
    # The file is checked and repaired before anything else, the transcript
    # copy included: the copy never reads the file, so answering from a twin
    # first would leave the row downloaded with nothing behind its path.
    media_root = getattr(config, "media_path", "")
    path = resolve_stored_media_path(media.get("file_path"), media_root)
    if not path or not os.path.isfile(path):
        repaired = await repair_media_row(db, media, media_root, account_id=account_id)
        if repaired == REFETCH:
            return "refetch"
    if content_hash is not None:
        copied = await _copy_transcript(
            config, db, media, content_hash, account_id=account_id, client=client, server=server, notifier=notifier
        )
        if copied is not None:
            return copied
    max_seconds = getattr(config, "transcription_max_seconds", 1800)
    if not isinstance(max_seconds, int) or isinstance(max_seconds, bool):
        max_seconds = None
    duration = _number(media.get("duration"))

    async def skip(reason: str) -> str:
        row = await db.mark_media_transcript_skipped(
            media_id,
            account_id=account_id,
            reason=reason,
            content_hash=media.get("content_hash"),
            duration_s=duration,
        )
        if row is not None:
            await _notify(notifier, media, row["id"], "skipped", account_id)
        return "skipped"

    too_long = f"longer than the {max_seconds} second limit"
    if max_seconds is not None and duration is not None and duration > max_seconds:
        return await skip(too_long)
    known_voice = media.get("type") == "voice" and duration is not None
    if path and os.path.isfile(path) and not known_voice:
        # A video with no sound is never sent. Documents and many videos
        # carry no stored duration, so ffprobe's is what the limit reads for
        # them. A voice message with a stored duration needs neither answer.
        probe = await probe_audio(path)
        if probe is not None:
            if duration is None:
                duration = probe.duration
            if not probe.has_audio:
                return await skip(NO_AUDIO_TRACK)
            if max_seconds is not None and duration is not None and duration > max_seconds:
                return await skip(too_long)

    # The source is written once; before the server is known (the
    # listener's call) it is left for the answer to fill.
    source = None if server is None else client.answer_source(server)
    row = await db.enqueue_media_transcript(
        media_id,
        account_id=account_id,
        content_hash=content_hash,
        idempotency_key=content_hash,
        preset=client.preset,
        source=source,
    )
    if row is None:
        return "noop"  # the newest row is done or skipped; only a user click adds another
    if row["status"] != "queued" or row.get("job_id"):
        return "noop"  # in flight on the job path, or being written by another process
    if not row.get("preset"):
        # A viewer ask-now row carries no preset. Filling it marks the row
        # picked up before anything below can fail: from here the ten-minute
        # rule of the drain query applies to it like to any other row.
        await db.fill_media_transcript(row["id"], status="queued", preset=client.preset, content_hash=content_hash)

    if not path or not os.path.isfile(path):
        await db.fill_media_transcript(
            row["id"], status="failed", error="file_missing", source=source or client.sync_source
        )
        await _notify(notifier, media, row["id"], "failed", account_id)
        return "failed"
    try:
        # Imported rows carry no hash: computed here, stored on the transcript
        # row only, never written back to media.
        idempotency_key = content_hash or await asyncio.to_thread(_file_sha256, path)
    except OSError:
        # Unreadable (permissions, a disk error): an answer about this file,
        # not an outage, so it spends a failed row instead of stopping the run.
        await db.fill_media_transcript(
            row["id"], status="failed", error="file_unreadable", source=source or client.sync_source
        )
        await _notify(notifier, media, row["id"], "failed", account_id)
        return "failed"
    if not row.get("idempotency_key"):
        # An ask-now row, or a row for an imported media with no hash.
        await db.fill_media_transcript(row["id"], status="queued", idempotency_key=idempotency_key)

    max_mb = _upload_limit_mb(config, client)
    limit = max_mb * 1024 * 1024 if max_mb > 0 else None  # 0 or less: no limit
    # A voice message or music file over the limit is not skipped yet: its
    # audio extracted to Opus is usually a fraction of it.
    over_limit = limit is not None and os.path.getsize(path) > limit
    extract = media.get("type") not in SENT_AS_STORED or over_limit
    extracted = await extract_audio(path) if extract else None

    async def send(track: str | None) -> str:
        upload_path = track or path
        if limit is not None and os.path.getsize(upload_path) > limit:
            return await skip(TOO_LARGE)
        upload_key = await asyncio.to_thread(_file_sha256, track) if track else None
        stem = os.path.splitext(os.path.basename(path))[0]
        filename = f"{stem}.ogg" if track else os.path.basename(path)
        # Streamed from disk: httpx reads the open file in chunks and rewinds
        # it on a retry, so a large file never sits in memory.
        with open(upload_path, "rb") as upload:
            return await _send(
                config,
                db,
                media,
                row,
                upload,
                filename,
                idempotency_key,
                upload_key,
                account_id=account_id,
                client=client,
                server=server,
                notifier=notifier,
            )

    try:
        outcome = await send(extracted)
        if outcome == UPLOAD_TOO_LARGE and extracted is None:
            # A 413 on the file as stored: its Opus track is a fraction of it, try that once.
            extracted = await extract_audio(path)
            if extracted:
                outcome = await send(extracted)
        if outcome != UPLOAD_TOO_LARGE:
            return outcome
        # A 413 on the audio track itself: this server cannot take this file.
        # Only this file is skipped; the run goes on.
        client.file_refusals += 1
        logger.warning(
            "Transcription server refused a file as too large (HTTP 413) even as its audio track; skipped. "
            f"Its limit, or a proxy's, is below TRANSCRIPTION_MAX_UPLOAD_MB ({_limit_text(max_mb)}): lower the "
            "setting so such files are skipped before they are sent"
        )
        return await skip(TOO_LARGE)
    finally:
        if extracted:
            _remove(extracted)


async def _send(
    config,
    db,
    media: dict[str, Any],
    row: dict[str, Any],
    upload: BinaryIO,
    filename: str,
    idempotency_key: str,
    upload_key: str | None,
    *,
    account_id: int,
    client: TranscriptionClient,
    server: ServerInfo | None,
    notifier,
) -> str:
    """Detect the server if the caller has not, then the job path or the synchronous request.

    On the synchronous path a refusal about the server's setup (401, 402,
    403, 404 for an unknown model or a wrong URL, 429) stores nothing: the
    row stays queued and the run ends (``refused``). A 413 answers
    ``UPLOAD_TOO_LARGE`` for ``transcribe_media`` to retry with the audio
    track or skip. Any other 4xx is an answer about this file and spends a
    failed row, and counts in ``client.file_refusals`` for the drain.
    """
    if server is None:
        try:
            server = await client.detect_server()
        except TranscriptionError as e:
            # The row stays queued; the next drain resubmits it after ten minutes.
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"
    if client.provider_name == SOURCE_AKOU and not server.job_path:
        logger.warning(_NOT_AKOU)
        return "refused"

    # What this request asks for, so a later copy reuses only a like answer.
    await db.fill_media_transcript(
        row["id"],
        status="queued",
        diarize=client.diarize and client.diarizes(server),
        options_tag=options_tag(client.request_options(server, config)),
    )
    if server.job_path:
        return await _submit_job(
            config,
            db,
            media,
            row,
            upload,
            filename,
            idempotency_key,
            account_id=account_id,
            client=client,
            server=server,
            notifier=notifier,
            upload_key=upload_key,
        )

    model = client.sync_model(server)
    try:
        columns = await client.provider.transcribe(client, upload, filename, model=model, prompt=_prompt_for(config))
    except TranscriptionError as e:
        if e.transient and not e.stalled:
            # Same as above: an outage is not an answer and spends no failed row.
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"
        if _server_refused(e) or e.status == 404:
            # A 404 here is an unknown model or a wrong URL: the same for every file.
            logger.warning(f"Transcription server refused the request ({e.reason}); the media stays queued")
            return "refused"
        if e.status == 413:
            return UPLOAD_TOO_LARGE
        if e.status is not None and 400 <= e.status < 500:
            client.file_refusals += 1
        failed = {
            "row_id": row["id"],
            "media": media,
            "account_id": account_id,
            "error": e.reason,
            "source": client.answer_source(server),
            "engine_name": server.name or None,
            "engine_version": server.version or None,
        }
        await _store_failed(db, notifier, failed)
        # A server that took the request and never answered, or failed it with
        # a 5xx after every attempt, would do the same to every later media: the
        # run ends here. A 5xx still spends a failed row on this path, since the
        # server decodes the file inside the request and the file may be the cause.
        return "stalled" if e.stalled or _server_failed(e) else "failed"

    client.answered = True
    await db.fill_media_transcript(
        row["id"],
        status="done",
        source=client.answer_source(server),
        engine_name=server.name or SOURCE_SYNC,
        engine_version=server.version or None,
        preset=client.preset,
        **columns,
    )
    await _notify(notifier, media, row["id"], "done", account_id)
    return "done"


# The warning when TRANSCRIPTION_PROVIDER=akou and the server is not akou's job path.
_NOT_AKOU = (
    "Transcription: TRANSCRIPTION_PROVIDER=akou but the server did not answer as akou with jobs; "
    "nothing sent, the media stays queued"
)


def _upload_limit_mb(config, client: TranscriptionClient) -> int:
    """The upload cap in MB; 0 or less means no limit.

    TRANSCRIPTION_MAX_UPLOAD_MB when it is set. Unset, 500, except for
    TRANSCRIPTION_PROVIDER=openai, whose usual servers (OpenAI, Groq's free
    tier) refuse more than 25 MB: the archive then extracts or skips before
    sending instead of learning it from a 413.
    """
    max_mb = getattr(config, "transcription_max_upload_mb", 500)
    if not isinstance(max_mb, int) or isinstance(max_mb, bool):
        max_mb = 500
    if client.provider_name == "openai" and getattr(config, "transcription_max_upload_mb_set", None) is not True:
        return OPENAI_UPLOAD_MB
    return max_mb


def _limit_text(max_mb: int) -> str:
    return f"{max_mb} MB" if max_mb > 0 else "no limit"


async def _store_failed(db, notifier, failed: dict[str, Any]) -> None:
    """A failed row for a synchronous answer about the file, and its push."""
    await db.fill_media_transcript(
        failed["row_id"],
        status="failed",
        error=failed["error"],
        source=failed["source"],
        engine_name=failed["engine_name"],
        engine_version=failed["engine_version"],
    )
    await _notify(notifier, failed["media"], failed["row_id"], "failed", failed["account_id"])
    logger.warning(f"Transcription failed ({failed['error']})")


async def drain_transcriptions(
    config,
    db,
    *,
    account_id: int,
    notifier: RealtimeNotifier | None = None,
    client: TranscriptionClient | None = None,
) -> dict[str, int]:
    """One drain: detect the server, reconcile, poll stragglers, submit.

    The middle two steps are the akou job path; a server that offers no
    jobs skips them and every media takes the synchronous path. Returns the
    counts of what this run did. Never raises for a server problem: an
    unreachable server is one warning and no rows, whether it is down at
    detection or goes down mid-run, in which case the run ends there and
    the rest waits for the next one. A refusal about the server or its
    configuration (a wrong key, a rate limit, a preset akou cannot run, a
    callback host off the allowlist, a 5xx on the job path) ends the run the
    same way, with one warning and no failed row. On the job path at most
    ``per_run`` jobs are in flight per account: a drain submits only as many
    new media as the open job rows leave room for.
    """
    stats = {
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "submitted": 0,
        "refused": 0,
        "unreachable": 0,
        "stalled": 0,
        "noop": 0,
        "reconciled": 0,
        "polled": 0,
        "copied": 0,
        "refetch": 0,
    }
    if getattr(config, "transcription_enabled", False) is not True:
        return stats
    client = client or TranscriptionClient(config)
    if not client.configured:
        logger.debug("Transcription: no server configured, nothing to drain")
        return stats

    # 1. Detect the server.
    try:
        server = await client.detect_server()
    except TranscriptionError as e:
        logger.warning(f"Transcription server unreachable ({e.reason}); skipping this run")
        return stats
    if client.provider_name == SOURCE_AKOU and not server.job_path:
        logger.warning(_NOT_AKOU)
        return stats
    try:
        # A server that names nothing is recorded as "openai", the endpoint it
        # answers, so the settings row never keeps the name of an earlier server.
        await db.set_transcription_server(server.name or SOURCE_SYNC, server.version)
    except Exception as e:
        logger.debug(f"Could not record the transcription server: {describe_exception(e)}")

    if notifier is None:
        notifier = RealtimeNotifier(getattr(db, "db_manager", None))

    if server.job_path:
        # 2. Reconcile, then 3. poll stragglers.
        try:
            stats["reconciled"] = await reconcile_events(db, client, server, notifier=notifier)
            stats["polled"] = await poll_stragglers(db, client, server, account_id=account_id, notifier=notifier)
        except TranscriptionError as e:
            if e.transient:
                logger.warning(f"Transcription server unreachable ({e.reason}); skipping this run")
                return stats
            if _server_refused(e) or _server_failed(e):
                logger.warning(f"Transcription server refused the feed or the poll ({e.reason}); skipping this run")
                return stats
            # An answer this client cannot use; the submit step still runs.
            logger.warning(f"Transcription reconcile stopped ({e.reason})")

    # 4. Submit.
    types = getattr(config, "transcription_types", None)
    if not isinstance(types, (set, frozenset, list, tuple)):
        types = ("voice",)
    per_run = getattr(config, "transcription_backfill_per_run", 50)
    if not isinstance(per_run, int) or isinstance(per_run, bool) or per_run < 1:
        per_run = 50
    if server.job_path:
        # Jobs still open count against the run: a server slower than per_run
        # per backup would otherwise grow the open rows, and the poll, without end.
        per_run -= len(await db.get_open_job_transcripts(account_id=account_id))
    priority = getattr(config, "transcription_priority_chat_ids", None)
    if not isinstance(priority, (list, tuple)):
        priority = ()
    media_rows = await db.get_media_awaiting_transcription(
        account_id=account_id,
        types=types,
        per_run=per_run,
        stale_before=utcnow_naive() - STALE_QUEUED,
        priority_chat_ids=priority,
    )
    if not media_rows:
        logger.debug("Transcription: nothing to send")
        return stats
    for media in media_rows:
        outcome = await transcribe_media(
            config, db, media, account_id=account_id, client=client, server=server, notifier=notifier
        )
        stats[outcome] = stats.get(outcome, 0) + 1
        if not client.answered and client.file_refusals >= 2:
            # Every file refused, none answered: more likely TRANSCRIPTION_MODEL or
            # TRANSCRIPTION_LANGUAGE than the files. Two rows a run at most, and a
            # failed row retires after three.
            logger.warning(
                f"Transcription server refused {client.file_refusals} files and answered none this run; ending the "
                "run. Check TRANSCRIPTION_MODEL and TRANSCRIPTION_LANGUAGE"
            )
            break
        if outcome in ("unreachable", "stalled", "refused"):
            # transcribe_media warned once; every media after this one would
            # wait out the same timeouts, or get the same refusal.
            break
    logger.info(
        "Transcription drain: %d done, %d copied, %d failed, %d skipped, %d submitted, %d refused, %d unreachable, "
        "%d sent back to download "
        "of %d media; %d filled from the event feed, %d from the poll",
        stats["done"],
        stats["copied"],
        stats["failed"] + stats["stalled"],
        stats["skipped"],
        stats["submitted"],
        stats["refused"],
        stats["unreachable"],
        stats["refetch"],
        len(media_rows),
        stats["reconciled"],
        stats["polled"],
    )
    return stats
