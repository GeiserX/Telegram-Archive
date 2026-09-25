"""Automatic voice transcription: the backup side of docs/TRANSCRIPTION.md.

Only the backup process talks to the transcription server. The drain runs
at the end of every backup and the listener calls the same per-media
function the moment it downloads a voice message. Every result is a new row
in ``media_transcripts``; nothing here changes or removes a media row.

This slice implements the synchronous path: the server is asked once per
run what it is (``GET /v1/server``), and the audio goes to the OpenAI
transcription endpoint (``POST /v1/audio/transcriptions``) whose answer is
stored at once. The akou job path (``POST /v1/jobs``, the event feed and
the straggler poll) is slice 4; until it lands the drain takes the
synchronous path whatever the server offers.

A server that cannot be reached is transient: the row stays ``queued`` and
the ten-minute branch of the drain query resubmits on it, so an outage
never spends the cap of three failed rows. An HTTP error is an answer and
is stored as a ``failed`` row.

PII rule: this module never logs a URL (httpx exception strings embed it,
so exceptions log as class names), the bearer key, a media id (it carries
the chat id), a file name or transcript text. Counts and reasons only.
"""

import asyncio
import hashlib
import logging
import os
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from .message_utils import describe_exception, utcnow_naive
from .realtime import NotificationType, RealtimeNotifier
from .web.media_utils import resolve_stored_media_path

logger = logging.getLogger(__name__)

# A queued row with no job_id older than this was left behind by a process
# that died between the insert and the submit; the drain resubmits on it.
STALE_QUEUED = timedelta(minutes=10)

# ``media_transcripts.source`` for the two protocols this module speaks.
SOURCE_SYNC = "openai"
SOURCE_AKOU = "akou"

# The OpenAI endpoint's ``model`` for any server that is not akou. Only akou
# reads the preset there (a preset name or an engine id); an OpenAI-compatible
# server that validates the field would refuse "auto" for good.
DEFAULT_SYNC_MODEL = "whisper-1"


class TranscriptionError(Exception):
    """A request that failed. ``reason`` is safe to store and to log.

    ``transient`` is True when the server could not be reached at all, so
    the caller leaves the row queued instead of storing a failed one.
    """

    def __init__(self, reason: str, *, transient: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient


@dataclass(frozen=True)
class ServerInfo:
    """What ``GET /v1/server`` said. Empty name means "not akou, synchronous path"."""

    name: str = ""
    version: str = ""
    jobs: bool = False


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
    5xx; any other status is a permanent answer. Redirects are never
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

    def __init__(self, config, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Defensive getattr + type checks, as the webhook sender: tests build
        # configs as bare MagicMock whose attributes are truthy.
        self._base_url = _base_url(getattr(config, "transcription_url", None))
        key = getattr(config, "transcription_api_key", None)
        self._headers = {"Authorization": f"Bearer {key}"} if isinstance(key, str) and key else {}
        preset = getattr(config, "transcription_preset", None)
        self.preset = preset if isinstance(preset, str) else "auto"
        language = getattr(config, "transcription_language", None)
        self.language = language if isinstance(language, str) else ""
        self.backoffs = self.BACKOFFS
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def sync_model(self, server: ServerInfo) -> str:
        """The synchronous endpoint's ``model``: the preset for akou, whisper-1 for anyone else."""
        if server.name == SOURCE_AKOU and self.preset:
            return self.preset
        return DEFAULT_SYNC_MODEL

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
        """
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
        return ServerInfo(
            name=name if isinstance(name, str) else "",
            version=str(version) if isinstance(version, (str, int, float)) and not isinstance(version, bool) else "",
            jobs=isinstance(capabilities, dict) and capabilities.get("jobs") is True,
        )

    async def transcribe(self, audio: bytes, filename: str, *, model: str, prompt: str | None = None) -> dict[str, Any]:
        """``POST /v1/audio/transcriptions`` and return the ``verbose_json`` answer.

        Multipart with ``model`` (see ``sync_model``), ``response_format=verbose_json``
        and ``timestamp_granularities[]=word``, plus the configured language
        and the hotword prompt when any. Raises ``TranscriptionError`` with
        a reason that names no URL; it is transient when every attempt was
        a transport failure, permanent when the server answered.
        """
        data = {"model": model, "response_format": "verbose_json", "timestamp_granularities[]": "word"}
        if self.language:
            data["language"] = self.language
        if prompt:
            data["prompt"] = prompt
        files = {"file": (filename, audio, "application/octet-stream")}
        timeout = httpx.Timeout(
            self.SYNC_RESPONSE_TIMEOUT_SECONDS,
            connect=self.CONNECT_TIMEOUT_SECONDS,
            write=self.UPLOAD_TIMEOUT_SECONDS,
        )
        reason = "unknown"
        transient = False
        async with self._client(timeout) as client:
            for attempt in range(self.ATTEMPTS):
                try:
                    response = await client.post(self._url("/v1/audio/transcriptions"), data=data, files=files)
                except httpx.TransportError as e:
                    reason = type(e).__name__
                    transient = True
                else:
                    transient = False
                    if response.status_code < 300:
                        try:
                            payload = response.json()
                        except ValueError:
                            raise TranscriptionError("invalid_json") from None
                        if not isinstance(payload, dict):
                            raise TranscriptionError("invalid_json")
                        return payload
                    reason = f"HTTP {response.status_code}"
                    if response.status_code != 429 and response.status_code < 500:
                        # Permanent (4xx) - a redirect (3xx) lands here too.
                        break
                if attempt < self.ATTEMPTS - 1:
                    await asyncio.sleep(self.backoffs[attempt])
        raise TranscriptionError(reason, transient=transient)


def _prompt_for(config) -> str | None:
    """The hotword prompt: an optional list of words a later slice may configure."""
    hotwords = getattr(config, "transcription_hotwords", None)
    if not isinstance(hotwords, (list, tuple)):
        return None
    words = [w.strip() for w in hotwords if isinstance(w, str) and w.strip()]
    return ", ".join(words) or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def result_columns(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Map a ``verbose_json`` answer onto the transcript row's columns."""
    words = [
        {
            "w": w.get("word"),
            "s": _number(w.get("start")),
            "e": _number(w.get("end")),
            "c": _number(w.get("probability")),
        }
        for w in payload.get("words") or []
        if isinstance(w, dict)
    ]
    segments = [
        {
            "s": _number(seg.get("start")),
            "e": _number(seg.get("end")),
            "text": seg.get("text") if isinstance(seg.get("text"), str) else "",
            "speaker": seg.get("speaker") if isinstance(seg.get("speaker"), str) else None,
        }
        for seg in payload.get("segments") or []
        if isinstance(seg, dict)
    ]
    text = payload.get("text")
    language = payload.get("language")
    return {
        "text": text if isinstance(text, str) else "",
        "language": language if isinstance(language, str) and language else None,
        "duration_s": _number(payload.get("duration")),
        "words": words,
        "segments": segments,
        "models": [model],
    }


def _read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


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
    """One media through the synchronous path; the drain and the listener both call this.

    Returns ``done``, ``failed``, ``skipped``, ``unreachable`` or ``noop``.
    The queued row is inserted first (insert-if-absent, so a second call
    while one is open reuses it), the audio is hashed when the media row
    carries no hash, and the answer fills the same row. The row stays
    ``queued`` during the request on purpose: a process that dies
    mid-request, and a server that cannot be reached (``unreachable``),
    both leave a row the next drain resubmits after ten minutes, with no
    failed row added.
    """
    client = client or TranscriptionClient(config)
    if not client.configured:
        return "noop"
    media_id = media["id"]
    max_seconds = getattr(config, "transcription_max_seconds", 1800)
    duration = _number(media.get("duration"))
    if isinstance(max_seconds, int) and not isinstance(max_seconds, bool) and duration is not None:
        if duration > max_seconds:
            row = await db.mark_media_transcript_skipped(
                media_id,
                account_id=account_id,
                reason=f"longer than the {max_seconds} second limit",
                content_hash=media.get("content_hash"),
                duration_s=duration,
            )
            if row is not None:
                await _notify(notifier, media, row["id"], "skipped", account_id)
            return "skipped"

    content_hash = media.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        content_hash = None
    row = await db.enqueue_media_transcript(
        media_id,
        account_id=account_id,
        content_hash=content_hash,
        idempotency_key=content_hash,
        preset=client.preset,
        source=SOURCE_SYNC,
    )
    if row is None:
        return "noop"  # the newest row is done or skipped; only a user click adds another
    if row["status"] != "queued" or row.get("job_id"):
        return "noop"  # in flight on the job path, or being written by another process

    path = resolve_stored_media_path(media.get("file_path"), getattr(config, "media_path", ""))
    if not path or not os.path.isfile(path):
        await db.fill_media_transcript(row["id"], status="failed", error="file_missing", source=SOURCE_SYNC)
        await _notify(notifier, media, row["id"], "failed", account_id)
        return "failed"
    audio = await asyncio.to_thread(_read_file, path)
    idempotency_key = content_hash
    if idempotency_key is None:
        # Imported rows carry no hash: computed here, stored on the transcript
        # row only, never written back to media.
        idempotency_key = hashlib.sha256(audio).hexdigest()
    if not row.get("idempotency_key"):
        await db.fill_media_transcript(row["id"], status="queued", idempotency_key=idempotency_key)

    if server is None:
        try:
            server = await client.detect_server()
        except TranscriptionError as e:
            # The row stays queued; the next drain resubmits it after ten minutes.
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"

    model = client.sync_model(server)
    try:
        payload = await client.transcribe(audio, os.path.basename(path), model=model, prompt=_prompt_for(config))
    except TranscriptionError as e:
        if e.transient:
            # Same as above: an outage is not an answer and spends no failed row.
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"
        await db.fill_media_transcript(
            row["id"],
            status="failed",
            error=e.reason,
            source=SOURCE_SYNC,
            engine_name=server.name or None,
            engine_version=server.version or None,
        )
        await _notify(notifier, media, row["id"], "failed", account_id)
        logger.warning(f"Transcription failed ({e.reason})")
        return "failed"

    await db.fill_media_transcript(
        row["id"],
        status="done",
        source=SOURCE_SYNC,
        engine_name=server.name or SOURCE_SYNC,
        engine_version=server.version or None,
        preset=client.preset,
        **result_columns(payload, model=model),
    )
    await _notify(notifier, media, row["id"], "done", account_id)
    return "done"


async def drain_transcriptions(
    config,
    db,
    *,
    account_id: int,
    notifier: RealtimeNotifier | None = None,
    client: TranscriptionClient | None = None,
) -> dict[str, int]:
    """One drain: detect the server, reconcile, poll stragglers, submit.

    The middle two steps belong to the akou job path (slice 4); until it
    lands, and whenever the server offers no jobs, every media takes the
    synchronous path. Returns the counts of what this run did. Never raises
    for a server problem: an unreachable server is one warning and no rows,
    whether it is down at detection or goes down mid-run, in which case the
    run ends there and the rest waits for the next one.
    """
    stats = {"done": 0, "failed": 0, "skipped": 0, "unreachable": 0, "noop": 0}
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
    if server.name:
        try:
            await db.set_transcription_server(server.name, server.version)
        except Exception as e:
            logger.debug(f"Could not record the transcription server: {describe_exception(e)}")

    # 2. Reconcile and 3. poll stragglers: the job path, slice 4.

    # 4. Submit.
    types = getattr(config, "transcription_types", None)
    if not isinstance(types, (set, frozenset, list, tuple)):
        types = ("voice", "video_note")
    per_run = getattr(config, "transcription_backfill_per_run", 50)
    if not isinstance(per_run, int) or isinstance(per_run, bool) or per_run < 1:
        per_run = 50
    media_rows = await db.get_media_awaiting_transcription(
        account_id=account_id, types=types, per_run=per_run, stale_before=utcnow_naive() - STALE_QUEUED
    )
    if not media_rows:
        logger.debug("Transcription: nothing to send")
        return stats
    if notifier is None:
        notifier = RealtimeNotifier(getattr(db, "db_manager", None))
    for media in media_rows:
        outcome = await transcribe_media(
            config, db, media, account_id=account_id, client=client, server=server, notifier=notifier
        )
        stats[outcome] = stats.get(outcome, 0) + 1
        if outcome == "unreachable":
            # transcribe_media warned once; every media after this one would
            # wait out the same connect timeouts for the same answer.
            break
    logger.info(
        "Transcription drain: %d done, %d failed, %d skipped, %d unreachable of %d media",
        stats["done"],
        stats["failed"],
        stats["skipped"],
        stats["unreachable"],
        len(media_rows),
    )
    return stats
