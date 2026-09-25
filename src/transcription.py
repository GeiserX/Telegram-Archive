"""Automatic voice transcription: the backup side of docs/TRANSCRIPTION.md.

Only the backup process talks to the transcription server. The drain runs
at the end of every backup and the listener calls the same per-media
function the moment it downloads a voice message. Every result is a new row
in ``media_transcripts``; nothing here changes or removes a media row.

The server is asked once per run what it is (``GET /v1/server``). akou
with ``capabilities.jobs`` takes the job path: ``POST /v1/jobs`` with the
audio's SHA-256 as the ``Idempotency-Key`` (``<sha256>.<n>`` on a retry),
then the result arrives by the signed callback into the viewer, by the
event feed the next drain reads, or by the straggler poll. Every other server takes the synchronous path:
the OpenAI transcription endpoint (``POST /v1/audio/transcriptions``),
whose answer is stored at once.

Where akou's documents leave a shape open, the shape assumed is written
once: ``ServerInfo``'s ``retain_days`` and ``_error_code`` here, the event
page, a job's outcome and the per-attempt ``Idempotency-Key`` in
``transcription_contract``, which the viewer imports without this module's
HTTP client.

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
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from .message_utils import describe_exception, utcnow_naive
from .realtime import NotificationType, RealtimeNotifier
from .transcription_contract import (
    _SAFE_CODE,
    SOURCE_AKOU,
    _number,
    apply_job_outcome,
    attempt_key,
    event_data,
    flat_job,
    job_outcome,
    parse_events_page,
)
from .web.media_utils import resolve_stored_media_path

logger = logging.getLogger(__name__)

# A queued row with no job_id older than this was left behind by a process
# that died between the insert and the submit; the drain resubmits on it.
STALE_QUEUED = timedelta(minutes=10)

# ``media_transcripts.source`` of the synchronous path; ``SOURCE_AKOU`` is
# the job path's, in transcription_contract.
SOURCE_SYNC = "openai"

# The OpenAI endpoint's ``model`` for any server that is not akou. Only akou
# reads the preset there (a preset name or an engine id); an OpenAI-compatible
# server that validates the field would refuse "auto" for good.
DEFAULT_SYNC_MODEL = "whisper-1"

# akou keeps a job's audio and result this long unless ``GET /v1/server``
# says otherwise (SERVER.md SV-J6, ``server.retain_days``). A row still open
# after it is marked failed with reason ``expired`` and the drain retries it.
DEFAULT_RETAIN_DAYS = 7

# At most this many pages of the event feed per drain; the rest waits for
# the next run, the cursor keeps the place.
MAX_EVENT_PAGES = 20


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
    retain_days: int = DEFAULT_RETAIN_DAYS

    @property
    def job_path(self) -> bool:
        return self.name == SOURCE_AKOU and self.jobs


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
    POLL_TIMEOUT_SECONDS = 60.0

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
        response = await self._send("POST", "/v1/audio/transcriptions", timeout=timeout, data=data, files=files)
        if response.status_code >= 300:
            # Permanent (4xx) - a redirect (3xx) lands here too.
            raise TranscriptionError(f"HTTP {response.status_code}")
        return _json_object(response)

    async def _send(self, method: str, path: str, *, timeout: float | httpx.Timeout, **kwargs) -> httpx.Response:
        """One request with bounded attempts; the first answer that is not 429 or 5xx.

        Transport errors, 429 and 5xx are retried. Raises a transient
        ``TranscriptionError`` when every attempt failed to connect, and a
        permanent one (``HTTP <code>``) when the last answer was 429 or 5xx.
        Any other answer, a 2xx, a 3xx or a 4xx, is returned for the caller
        to read.
        """
        reason = "unknown"
        transient = False
        async with self._client(timeout) as client:
            for attempt in range(self.ATTEMPTS):
                try:
                    response = await client.request(method, self._url(path), **kwargs)
                except httpx.TransportError as e:
                    reason = type(e).__name__
                    transient = True
                else:
                    transient = False
                    if response.status_code != 429 and response.status_code < 500:
                        return response
                    reason = f"HTTP {response.status_code}"
                if attempt < self.ATTEMPTS - 1:
                    await asyncio.sleep(self.backoffs[attempt])
        raise TranscriptionError(reason, transient=transient)

    # ------------------------------------------------------------------
    # The akou job path (SERVER.md sections 5 and 6)
    # ------------------------------------------------------------------

    async def submit_job(
        self,
        audio: bytes,
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
        data = {
            "preset": self.preset or "auto",
            "language": self.language or "auto",
            "metadata": json.dumps({"content_hash": content_hash}),
        }
        if callback_url:
            data["callback_url"] = callback_url
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
            raise TranscriptionError(_error_code(response))
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
            raise TranscriptionError("not_found")
        if response.status_code >= 300:
            raise TranscriptionError(_error_code(response))
        return _json_object(response)


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
    """The hotword prompt: an optional list of words a later slice may configure."""
    hotwords = getattr(config, "transcription_hotwords", None)
    if not isinstance(hotwords, (list, tuple)):
        return None
    words = [w.strip() for w in hotwords if isinstance(w, str) and w.strip()]
    return ", ".join(words) or None


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


async def _notify_rows(db, notifier, rows: list[dict[str, Any]]) -> None:
    """Push each filled row; the media row gives the chat and message ids."""
    if notifier is None:
        return
    for row in rows:
        media = await db.get_media_by_id(row["media_id"], account_id=row["account_id"])
        if media is not None:
            await _notify(notifier, media, row["id"], row["status"], row["account_id"])


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
    job_id = data.get("job_id") if isinstance(data.get("job_id"), str) else None
    siblings = await db.list_media_transcripts(row["media_id"], account_id=row["account_id"])
    if job_id and any(s["job_id"] == job_id and s["id"] != row["id"] for s in siblings):
        job_id = None
    if await db.fill_media_transcript(row["id"], status=status, job_id=job_id, **columns):
        filled.append({"id": row["id"], "account_id": row["account_id"], "media_id": row["media_id"], "status": status})
    return filled


async def _submit_job(
    config,
    db,
    media: dict[str, Any],
    row: dict[str, Any],
    audio: bytes,
    filename: str,
    idempotency_key: str,
    *,
    account_id: int,
    client: TranscriptionClient,
    server: ServerInfo,
    notifier,
) -> str:
    """``POST /v1/jobs`` for one queued row and branch on the answer's ``status``.

    ``queued`` or ``running``: the row stores the job id and that status,
    and the result comes later by the callback, the event feed or the
    straggler poll (``submitted``). ``done``: the completed event is
    behind the cursor and no callback will come, so the row is stored now
    from the nested result, or from the result route when the answer
    carries none. ``failed`` or ``cancelled``: a failed row with the reason.
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
            idempotency_key=attempt_key(idempotency_key, earlier),
        )
    except TranscriptionError as e:
        if e.transient:
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"
        await db.fill_media_transcript(row["id"], status="failed", error=e.reason, source=SOURCE_AKOU)
        await _notify(notifier, media, row["id"], "failed", account_id)
        if e.reason == "callback_not_allowed":
            # Every other submit of this run would be refused the same way.
            logger.warning(
                "Transcription: akou refused TRANSCRIPTION_CALLBACK_URL (callback_not_allowed); "
                "add its host to the key's callback-host allowlist or unset it. Ending this run"
            )
            return "refused"
        logger.warning(f"Transcription job refused ({e.reason})")
        return "failed"

    data = flat_job(job)
    job_id = data.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        await db.fill_media_transcript(row["id"], status="failed", error="invalid_job", source=SOURCE_AKOU)
        await _notify(notifier, media, row["id"], "failed", account_id)
        return "failed"
    status = data.get("status")
    if status == "done" and not isinstance(data.get("text"), str):
        try:
            data = await client.get_result(job_id)
        except TranscriptionError as e:
            # The straggler poll fetches it on a later drain.
            logger.warning(f"Transcription result not read yet ({e.reason})")
            await db.fill_media_transcript(row["id"], status="running", job_id=job_id, source=SOURCE_AKOU)
            return "submitted"
    outcome = job_outcome(data, engine_version=server.version)
    if outcome is None:
        # Still open, or a status this client does not know: keep the job id
        # and let the callback, the event feed or the straggler poll finish it.
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
                    if e.transient:
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
    """Ask akou about every open job older than ten minutes; the rows finished.

    A row older than the server's retention is marked failed with reason
    ``expired`` without a request, since akou has deleted the job by then,
    and the drain query retries it. A job akou no longer knows is failed
    with reason ``not_found`` and retried the same way. A ``done`` job
    whose answer carries no result is read from the result route.
    """
    now = utcnow_naive()
    expired_before = now - timedelta(days=server.retain_days)
    finished = 0
    for row in await db.get_open_job_transcripts(account_id=account_id, requested_before=now - STALE_QUEUED):
        if row["requested_at"] < expired_before:
            if await db.fill_media_transcript(row["id"], status="failed", error="expired"):
                finished += 1
                await _notify_rows(db, notifier, [{**row, "status": "failed"}])
            continue
        try:
            data = flat_job(await client.get_job(row["job_id"]))
            if data.get("status") == "done" and not isinstance(data.get("text"), str):
                data = await client.get_result(row["job_id"])
        except TranscriptionError as e:
            if e.transient:
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
    ``unreachable`` or ``noop``. The queued row is inserted first
    (insert-if-absent, so a second call while one is open reuses it), the
    audio is hashed when the media row carries no hash, and the answer
    fills the same row: at once on the synchronous path, or with the job
    id on the akou job path (``submitted``), where the result arrives
    later. The row stays ``queued`` during the request on purpose: a
    process that dies mid-request, and a server that cannot be reached
    (``unreachable``), both leave a row the next drain resubmits after ten
    minutes, with no failed row added. ``refused`` is akou's
    ``callback_not_allowed``: the row is failed and the drain ends the run.
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
    # The source is written once; before the server is known (the
    # listener's call) it is left for the answer to fill.
    source = None if server is None else (SOURCE_AKOU if server.job_path else SOURCE_SYNC)
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

    path = resolve_stored_media_path(media.get("file_path"), getattr(config, "media_path", ""))
    if not path or not os.path.isfile(path):
        await db.fill_media_transcript(row["id"], status="failed", error="file_missing", source=source or SOURCE_SYNC)
        await _notify(notifier, media, row["id"], "failed", account_id)
        return "failed"
    audio = await asyncio.to_thread(_read_file, path)
    idempotency_key = content_hash
    if idempotency_key is None:
        # Imported rows carry no hash: computed here, stored on the transcript
        # row only, never written back to media.
        idempotency_key = hashlib.sha256(audio).hexdigest()
    if not row.get("idempotency_key") or not row.get("preset"):
        # A viewer ask-now row carries neither a key nor a preset. Filling the
        # preset marks it picked up: from here the ten-minute rule of the drain
        # query applies to it like to any other row.
        await db.fill_media_transcript(
            row["id"],
            status="queued",
            idempotency_key=idempotency_key,
            preset=client.preset,
            content_hash=content_hash,
        )

    if server is None:
        try:
            server = await client.detect_server()
        except TranscriptionError as e:
            # The row stays queued; the next drain resubmits it after ten minutes.
            logger.warning(f"Transcription server unreachable ({e.reason}); the media stays queued")
            return "unreachable"

    if server.job_path:
        return await _submit_job(
            config,
            db,
            media,
            row,
            audio,
            os.path.basename(path),
            idempotency_key,
            account_id=account_id,
            client=client,
            server=server,
            notifier=notifier,
        )

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

    The middle two steps are the akou job path; a server that offers no
    jobs skips them and every media takes the synchronous path. Returns the
    counts of what this run did. Never raises for a server problem: an
    unreachable server is one warning and no rows, whether it is down at
    detection or goes down mid-run, in which case the run ends there and
    the rest waits for the next one. akou refusing the callback URL ends
    the run the same way, after one failed row and one warning.
    """
    stats = {
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "submitted": 0,
        "refused": 0,
        "unreachable": 0,
        "noop": 0,
        "reconciled": 0,
        "polled": 0,
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
    if server.name:
        try:
            await db.set_transcription_server(server.name, server.version)
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
            # An answer this client cannot use; the submit step still runs.
            logger.warning(f"Transcription reconcile stopped ({e.reason})")

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
    for media in media_rows:
        outcome = await transcribe_media(
            config, db, media, account_id=account_id, client=client, server=server, notifier=notifier
        )
        stats[outcome] = stats.get(outcome, 0) + 1
        if outcome in ("unreachable", "refused"):
            # transcribe_media warned once; every media after this one would
            # wait out the same connect timeouts, or get the same refusal.
            break
    logger.info(
        "Transcription drain: %d done, %d failed, %d skipped, %d submitted, %d unreachable of %d media; "
        "%d filled from the event feed, %d from the poll",
        stats["done"],
        stats["failed"] + stats["refused"],
        stats["skipped"],
        stats["submitted"],
        stats["unreachable"],
        len(media_rows),
        stats["reconciled"],
        stats["polled"],
    )
    return stats
