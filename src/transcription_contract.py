"""The akou contract in one place: the event page, a job's outcome, the webhook signature.

The backup (``src/transcription.py``) and the viewer's callback route
(``src/web/main.py``) both read these. Standard library only, on purpose:
the viewer image installs the ``viewer-runtime`` dependency group, which
has no HTTP client, and copies this file but not ``transcription.py``.

Where akou's documents leave a shape open, the shape assumed here is
written once: the event page in ``parse_events_page``, a job's outcome in
``job_outcome`` and ``_failure_reason``, and the per-attempt
``Idempotency-Key`` in ``attempt_key``. ``ServerInfo.retain_days`` and
``_error_code`` in ``transcription.py`` are the other two.
"""

import base64
import binascii
import hashlib
import hmac
import re
import time
from typing import Any

# ``media_transcripts.source`` of the akou job path.
SOURCE_AKOU = "akou"

# What can be transcribed: every media that carries sound. The bubble shows
# its button on all of it and the ask-now routes accept all of it; the drain
# and the listener pick up on their own only the TRANSCRIPTION_TYPES subset.
# ``document`` counts only when its stored mime_type is audio or video: a
# .wav, .flac or .mkv sent as a file. ``animation`` never does: Telegram's
# GIF-style clips have no sound.
TRANSCRIBABLE_TYPES = frozenset({"voice", "video_note", "audio", "video", "document"})
TRANSCRIBABLE_DOCUMENT_MIME_PREFIXES = ("audio/", "video/")


def is_transcribable(media_type: Any, mime_type: Any, types: Any = TRANSCRIBABLE_TYPES) -> bool:
    """True when a media of ``media_type`` and ``mime_type`` is in ``types`` and carries sound."""
    if media_type not in TRANSCRIBABLE_TYPES or media_type not in types:
        return False
    if media_type == "document":
        return isinstance(mime_type, str) and mime_type.lower().startswith(TRANSCRIBABLE_DOCUMENT_MIME_PREFIXES)
    return True


# The event types of akou's feed and callback (SERVER.md SV-E1). Any other
# type is skipped and the cursor still moves past it.
EVENT_COMPLETED = "transcription.completed"
EVENT_FAILED = "transcription.failed"
EVENT_CANCELLED = "transcription.cancelled"

# A refused request's error code is stored and logged only when it looks
# like one of akou's snake_case codes; anything else becomes ``HTTP <code>``.
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

# The Standard Webhooks rules of the callback (SERVER.md SV-E2).
WEBHOOK_TOLERANCE_SECONDS = 5 * 60
WEBHOOK_SECRET_PREFIX = "whsec_"


# A language is stored only when it looks like a BCP-47 tag ("es", "pt-BR",
# "yue"). Anything else, akou's OpenAI route answering "unknown" or OpenAI's
# own English names ("spanish"), is stored as NULL rather than as a code the
# viewer would show and nothing could filter on.
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")


def language_tag(value: Any) -> str | None:
    """``value`` when it looks like a BCP-47 tag, otherwise None."""
    return value if isinstance(value, str) and _LANGUAGE_TAG.fullmatch(value) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dicts(value: Any) -> list[dict[str, Any]]:
    """The objects of a list field; anything that is not a list reads as empty.

    One event with ``words: 5`` must not raise: the feed's cursor is saved
    only after a whole page, so an exception here would stop it for good.
    """
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def parse_events_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """One page of ``GET /v1/events``: the events and the cursor to read after them.

    akou answers ``{"events": [{"id", "cursor", "type", "timestamp", "job_id",
    "data"}, ...], "cursor": <int>, "has_more": <bool>}``. The cursor is an
    integer sequence number, and ``after`` must be that integer: an event's
    ``id`` is a ``msg_`` string that akou refuses as a cursor. The top-level
    ``cursor`` wins; without it, the last event's own ``cursor``; with
    neither, None, and the stored cursor stays where it is. The cursor is
    stored as a decimal string.
    """
    events = payload.get("events")
    events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    cursor = _int_cursor(payload.get("cursor"))
    if cursor is None and events:
        cursor = _int_cursor(events[-1].get("cursor"))
    return events, cursor


def _int_cursor(value: Any) -> str | None:
    """A feed cursor as a decimal string, or None when it is not a non-negative integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def flat_job(job: dict[str, Any]) -> dict[str, Any]:
    """A job answer in the flat shape of the result route and the webhook ``data``.

    ``POST /v1/jobs`` and ``GET /v1/jobs/{id}`` answer ``{id, status, ...}``
    with any result nested under ``result``; the result route and the
    webhook carry the result fields flat beside ``job_id``, ``status`` and
    ``metadata``. Everything downstream reads the flat shape.
    """
    result = job.get("result")
    flat = dict(result) if isinstance(result, dict) else {}
    flat["job_id"] = job.get("id") if isinstance(job.get("id"), str) else job.get("job_id")
    flat["status"] = job.get("status")
    for name in ("error", "metadata"):
        if name in job and name not in flat:
            flat[name] = job[name]
    return flat


def _failure_reason(data: dict[str, Any]) -> str:
    """The stored reason of a failed job: akou's error code when it looks like one, else ``failed``."""
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return "failed"


def job_outcome(data: dict[str, Any], *, engine_version: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """A job's flat result or event data as ``(row status, columns)``; None while it is still open.

    ``done`` maps the SV-J4 fields onto the row: text, language and its
    confidence, duration, words ``[{w, s, e, c}]``, segments
    ``[{s, e, text, speaker}]``, the mean confidence and ``engine.models``.
    ``engine_name`` is ``akou``, the server, like the synchronous path; the
    ASR model is in ``models``. ``failed`` stores akou's error code as the
    reason and ``cancelled`` stores ``cancelled``; the drain query retries
    both. A ``done`` without inline ``text`` (the callback's ``result_url``
    form) returns None: the caller fetches the result route or leaves the
    row for the backup.
    """
    status = data.get("status")
    if status == "failed":
        return "failed", {"error": _failure_reason(data), "source": SOURCE_AKOU}
    if status == "cancelled":
        return "failed", {"error": "cancelled", "source": SOURCE_AKOU}
    if status != "done" or not isinstance(data.get("text"), str):
        return None
    engine = data.get("engine") if isinstance(data.get("engine"), dict) else {}
    raw_models = engine.get("models")
    models = [m for m in raw_models if isinstance(m, str)] if isinstance(raw_models, list) else []
    words = [
        {"w": w.get("w"), "s": _number(w.get("s")), "e": _number(w.get("e")), "c": _number(w.get("c"))}
        for w in _dicts(data.get("words"))
    ]
    segments = [
        {
            "s": _number(seg.get("s")),
            "e": _number(seg.get("e")),
            "text": seg.get("text") if isinstance(seg.get("text"), str) else "",
            "speaker": seg.get("speaker") if isinstance(seg.get("speaker"), str) else None,
        }
        for seg in _dicts(data.get("segments"))
    ]
    return "done", {
        "text": data["text"],
        "language": language_tag(data.get("language")),
        "language_confidence": _number(data.get("language_confidence")),
        "duration_s": _number(data.get("duration_s")),
        "confidence": _number(data.get("confidence")),
        "words": words,
        "segments": segments,
        "models": models,
        "source": SOURCE_AKOU,
        "engine_name": SOURCE_AKOU,
        "engine_version": engine_version or None,
    }


def event_data(event: dict[str, Any]) -> dict[str, Any] | None:
    """An event's ``data`` with the status its type implies; None for a type this client does not know.

    The same body arrives by the callback and by the event feed:
    ``{type, timestamp, data}`` (SERVER.md SV-E3). The status comes from
    the type, so a ``transcription.cancelled`` event stores ``cancelled``
    whatever its data says. A scrubbed event (``data.deleted`` true, SV-J6:
    akou keeps only the job id and the final state after a delete or its
    retention) is None too: its result is gone, and a completed event
    without text must never be read as an empty transcript.
    """
    event_type = event.get("type")
    statuses = {EVENT_COMPLETED: "done", EVENT_FAILED: "failed", EVENT_CANCELLED: "cancelled"}
    # A list or an object as the type is unhashable: read it as unknown, never raise.
    status = statuses.get(event_type) if isinstance(event_type, str) else None
    data = event.get("data")
    if status is None or not isinstance(data, dict) or data.get("deleted") is True:
        return None
    return {**data, "status": status}


def metadata_hash(data: dict[str, Any]) -> str | None:
    """``data.metadata.content_hash``: the audio's SHA-256 the archive sent with the job."""
    metadata = data.get("metadata")
    value = metadata.get("content_hash") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value else None


def webhook_key(secret: Any) -> bytes | None:
    """The HMAC key of a ``whsec_`` secret: the base64-decoded bytes after the prefix, or None."""
    if not isinstance(secret, str) or not secret.startswith(WEBHOOK_SECRET_PREFIX):
        return None
    try:
        key = base64.b64decode(secret[len(WEBHOOK_SECRET_PREFIX) :], validate=True)
    except binascii.Error, ValueError:
        return None
    return key or None


_TIMESTAMP = re.compile(r"-?\d{1,12}")


def verify_webhook(
    key: bytes, webhook_id: str, timestamp: str, signature: str, body: bytes, *, now: float | None = None
) -> str | None:
    """Standard Webhooks verification; None when the delivery is genuine, else the reason.

    The timestamp must be within five minutes of ``now`` in either
    direction (``stale``). The signature is HMAC-SHA256 over
    ``{webhook-id}.{webhook-timestamp}.{raw body}``; the header is a
    space-separated list of ``v1,<base64>`` values, and any one matching
    accepts, so a rotated secret keeps working during the overlap. Every
    value is compared in constant time (``bad_signature``).
    """
    # Unix seconds fit in 12 digits; a longer number would overflow the float
    # comparison below and turn an unauthenticated request into a 500.
    if not _TIMESTAMP.fullmatch(timestamp):
        return "stale"
    sent_at = int(timestamp)
    if abs((time.time() if now is None else now) - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        return "stale"
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    matched = False
    for entry in signature.split():
        version, _, value = entry.partition(",")
        if version == "v1" and hmac.compare_digest(expected, value.encode("utf-8", "replace")):
            matched = True
    return None if matched else "bad_signature"


async def apply_job_outcome(
    db, data: dict[str, Any], *, content_hash: str | None = None, engine_version: str | None = None
) -> list[dict[str, Any]]:
    """Store one job's outcome in every open row for its audio; the rows filled.

    The callback, the event feed and the straggler poll all land here, and
    the adapter's row rule makes a repeat of an outcome already applied
    write nothing. ``content_hash`` defaults to ``data.metadata.content_hash``.
    """
    outcome = job_outcome(data, engine_version=engine_version)
    key = content_hash or metadata_hash(data)
    if outcome is None or not key:
        return []
    status, columns = outcome
    job_id = data.get("job_id") if isinstance(data.get("job_id"), str) else None
    return await db.fill_open_transcripts_by_key(key, job_id=job_id, status=status, **columns)


def attempt_key(content_hash: str, earlier_attempts: int) -> str:
    """The ``Idempotency-Key`` of one submit: the audio's SHA-256, then ``.<n>`` from the second attempt on.

    akou keeps a key as long as its job and answers the same job for the
    same key in whatever state it is (SV-J2), so a failed or cancelled job
    would come back on every retry. ``earlier_attempts`` counts this
    media's rows that already ended ``done`` or ``failed``: a first attempt
    sends the bare hash, so the same audio under two media rows still
    shares one job, and every later attempt names a new job. Assumed: akou
    accepts any opaque string up to 255 characters as a key, since SV-J2
    names no format.
    """
    return content_hash if earlier_attempts <= 0 else f"{content_hash}.{earlier_attempts}"
