"""Outbound event webhook for listener-applied message edits and deletions (#336).

Fires a user-templated HTTP request when the real-time listener commits a
message edit or deletion to the archive. Opt-in via EVENT_WEBHOOK_ENABLED;
the sweep (SYNC_DELETIONS_EDITS) deliberately does not fire it — bulk
discovery has no event time and would flood stale pings.

Template language: ``{placeholder}`` and ``{placeholder|filter}`` only, where
both identifier and filter match ``[A-Za-z_][A-Za-z0-9_]*``. Everything else —
including every brace of a literal JSON template — passes through untouched.
There is no attribute access, no expression evaluation and no re-scanning of
substituted values, so template injection is impossible by construction.

Substitution rules:
- Unknown placeholder or ``None`` value renders as an empty string.
- ``datetime`` values render as ISO-8601.
- Filters: ``jsonescape`` (JSON string-escape, no surrounding quotes),
  ``urlencode`` (percent-encode, no safe characters), ``raw`` (identity).
- An unknown filter falls back to the auto filter.
- The auto filter is keyed to the declared Content-Type header:
  ``application/json``/``*+json`` → jsonescape,
  ``application/x-www-form-urlencoded`` → urlencode, anything else → raw.

PII rule: this module never logs the URL (a capability secret — and httpx
exception strings embed it, so exceptions log as class names only), headers,
rendered bodies, response bodies, chat ids or message content.
"""

import asyncio
import json
import logging
import re
import urllib.parse
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

# Always-present ids are unquoted (real JSON numbers); nullable fields are
# quoted so a missing value renders as "" and the body stays valid JSON.
DEFAULT_BODY_TEMPLATE = (
    '{"event":"{event}","account_id":{account_id},"chat_id":{chat_id},'
    '"chat_title":"{chat_title}","message_id":{message_id},'
    '"sender_id":"{sender_id}","sender_name":"{sender_name}","date":"{date}",'
    '"text":"{text}","old_text":"{old_text}","new_text":"{new_text}",'
    '"media_type":"{media_type}"}'
)

VALID_EVENTS = frozenset({"message_edited", "message_deleted"})

_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\|([A-Za-z_][A-Za-z0-9_]*))?\}")

_FILTERS = {
    # json.dumps of a str is always quote-delimited, so the [1:-1] slice is safe.
    # ensure_ascii=False keeps emoji/UTF-8 literal; the body is sent as UTF-8.
    "jsonescape": lambda s: json.dumps(s, ensure_ascii=False)[1:-1],
    "urlencode": lambda s: urllib.parse.quote(s, safe=""),
    "raw": lambda s: s,
}


def auto_filter_for(content_type: str | None) -> str:
    """Map a declared Content-Type to the default per-placeholder filter."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct == "application/json" or ct.endswith("+json"):
        return "jsonescape"
    if ct == "application/x-www-form-urlencoded":
        return "urlencode"
    return "raw"


def render_template(template: str, context: dict, auto: str = "jsonescape") -> str:
    """Single-pass token substitution; see the module docstring for semantics."""

    def repl(match: re.Match) -> str:
        value = context.get(match.group(1))
        if value is None:
            rendered = ""
        elif isinstance(value, datetime):
            rendered = value.isoformat()
        else:
            rendered = str(value)
        return _FILTERS.get(match.group(2) or auto, _FILTERS[auto])(rendered)

    return _TOKEN.sub(repl, template)


class EventWebhookSender:
    """Renders and delivers event webhooks fire-and-forget.

    ``fire()`` is synchronous: it renders the body and spawns a retained
    delivery task (the realtime notifier's task pattern), so a burst of
    deletions never serializes external latency ahead of archive writes.
    Delivery is bounded: 3 attempts, retrying only transport errors, HTTP 429
    and 5xx; anything else is a permanent configuration error. There is no
    persistence or redelivery — the archive DB is the system of record and
    only the ping is lost on final failure.
    """

    MAX_IN_FLIGHT = 100
    ATTEMPTS = 3
    BACKOFFS = (1.0, 4.0)  # seconds before attempts 2 and 3
    TIMEOUT_SECONDS = 5.0  # per attempt

    def __init__(self, config, stats: dict) -> None:
        # Defensive getattr + type checks: listener tests build configs as bare
        # MagicMock, whose auto-attributes are truthy — without normalization
        # every existing listener test would see the webhook enabled.
        url = getattr(config, "event_webhook_url", None)
        self._url = url if isinstance(url, str) else ""
        self._enabled = getattr(config, "event_webhook_enabled", False) is True and bool(self._url)
        method = getattr(config, "event_webhook_method", "POST")
        self._method = method if method in ("POST", "PUT") else "POST"
        headers = getattr(config, "event_webhook_headers", None)
        self._headers = dict(headers) if isinstance(headers, dict) else {}
        events = getattr(config, "event_webhook_events", None)
        self._events = set(events) if isinstance(events, (set, frozenset)) else set(VALID_EVENTS)
        chat_ids = getattr(config, "event_webhook_chat_ids", None)
        self._chat_ids = set(chat_ids) if isinstance(chat_ids, (set, frozenset)) else set()
        template = getattr(config, "event_webhook_body_template", None)
        self._template = template if isinstance(template, str) and template else DEFAULT_BODY_TEMPLATE
        content_type = next((v for k, v in self._headers.items() if k.lower() == "content-type"), None)
        self._auto = auto_filter_for(content_type)
        self._stats = stats
        self._tasks: set[asyncio.Task] = set()

    def wants(self, event: str, chat_id: int) -> bool:
        """Cheap pre-check so callers skip context building when disabled/filtered."""
        return self._enabled and event in self._events and (not self._chat_ids or chat_id in self._chat_ids)

    def fire(self, event: str, context: dict) -> None:
        """Render and dispatch one webhook; never raises, never blocks."""
        if not self.wants(event, context.get("chat_id")):
            return
        if len(self._tasks) >= self.MAX_IN_FLIGHT:
            self._stats["webhook_dropped"] = self._stats.get("webhook_dropped", 0) + 1
            logger.debug("Event webhook dropped: %d deliveries already in flight", len(self._tasks))
            return
        body = render_template(self._template, context, auto=self._auto)
        task = asyncio.create_task(self._deliver(event, body))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def _deliver(self, event: str, body: str) -> None:
        reason = "unknown"
        async with httpx.AsyncClient(timeout=self.TIMEOUT_SECONDS, follow_redirects=False) as client:
            for attempt in range(self.ATTEMPTS):
                try:
                    response = await client.request(
                        self._method, self._url, content=body.encode("utf-8"), headers=self._headers
                    )
                except httpx.TransportError as e:
                    # Includes timeouts and connection failures — retryable.
                    reason = type(e).__name__
                else:
                    if response.status_code < 300:
                        self._stats["webhook_sent"] = self._stats.get("webhook_sent", 0) + 1
                        logger.debug("Event webhook delivered (%s)", event)
                        return
                    reason = f"HTTP {response.status_code}"
                    if response.status_code != 429 and response.status_code < 500:
                        # Permanent config error (4xx) — a redirect (3xx) also lands
                        # here: following one would re-send auth headers and message
                        # content to a host the operator never configured.
                        break
                if attempt < self.ATTEMPTS - 1:
                    await asyncio.sleep(self.BACKOFFS[attempt])
        self._stats["webhook_failed"] = self._stats.get("webhook_failed", 0) + 1
        # PII: never the URL (httpx exception strings embed it), headers or body.
        logger.warning("Event webhook delivery failed (%s: %s)", event, reason)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._stats["webhook_failed"] = self._stats.get("webhook_failed", 0) + 1
            logger.warning("Event webhook delivery task failed: %s", type(exc).__name__)

    async def aclose(self) -> None:
        """Cancel outstanding deliveries; shutdown never waits out retries."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
