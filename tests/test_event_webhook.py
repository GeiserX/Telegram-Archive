"""Unit tests for the outbound event webhook engine and sender (#336).

The template engine's contract is exact-output: rendered JSON bodies must
json.loads round-trip for hostile values, form bodies must parse_qs
round-trip, and substituted values must never be re-expanded. The sender's
contract is bounded fire-and-forget delivery with PII-free logging.
"""

import asyncio
import json
import logging
import os
import sys
import urllib.parse
from datetime import datetime
from unittest.mock import MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.event_webhook import (
    DEFAULT_BODY_TEMPLATE,
    EventWebhookSender,
    auto_filter_for,
    render_template,
)

SECRET_URL = "https://hooks.example.test/T000/B000/secret-path-token"


class TestAutoFilterFor:
    def test_json_with_parameters(self):
        assert auto_filter_for("application/json; charset=utf-8") == "jsonescape"

    def test_json_case_insensitive(self):
        assert auto_filter_for("APPLICATION/JSON") == "jsonescape"

    def test_structured_json_suffix(self):
        assert auto_filter_for("application/problem+json") == "jsonescape"

    def test_form(self):
        assert auto_filter_for("application/x-www-form-urlencoded") == "urlencode"

    def test_plain_and_unknown_and_missing(self):
        assert auto_filter_for("text/plain") == "raw"
        assert auto_filter_for("application/xml") == "raw"
        assert auto_filter_for(None) == "raw"
        assert auto_filter_for("") == "raw"


class TestRenderTemplate:
    def test_default_template_round_trips_hostile_text(self):
        context = {
            "event": "message_edited",
            "account_id": 1,
            "chat_id": -100123,
            "chat_title": 'Group "A"\nline2',
            "message_id": 42,
            "sender_id": 555,
            "sender_name": "Ana \\ O'Hara",
            "date": datetime(2026, 8, 22, 11, 0, 5),
            "text": 'new "quoted"\ntext',
            "old_text": "old\ttext",
            "new_text": 'new "quoted"\ntext',
            "media_type": None,
        }
        body = json.loads(render_template(DEFAULT_BODY_TEMPLATE, context))
        assert body["event"] == "message_edited"
        assert body["chat_id"] == -100123  # unquoted placeholder stays a JSON number
        assert body["message_id"] == 42
        assert body["chat_title"] == 'Group "A"\nline2'
        assert body["text"] == 'new "quoted"\ntext'
        assert body["date"] == "2026-08-22T11:00:05"
        assert body["media_type"] == ""  # None renders blank, body stays valid

    def test_values_are_never_re_expanded(self):
        context = {"event": "message_deleted", "text": 'has {event} and {"a":1} inside'}
        body = json.loads(render_template('{"e":"{event}","t":"{text}"}', context))
        assert body["t"] == 'has {event} and {"a":1} inside'

    def test_unknown_placeholder_renders_blank(self):
        body = json.loads(render_template('{"x":"{nonexistent}"}', {}))
        assert body["x"] == ""

    def test_non_tokens_pass_through_byte_identical(self):
        template = '{ "x": 1 } {123} {} { event } {a-b} {event|} {|raw}'
        assert render_template(template, {"event": "e"}) == template

    def test_double_braces_render_brace_value_brace(self):
        assert render_template("{{event}}", {"event": "X"}) == "{X}"

    def test_form_body_round_trips(self):
        context = {"text": "a=b&c d\nnewline", "chat_id": -1}
        rendered = render_template("text={text}&chat={chat_id}", context, auto="urlencode")
        parsed = urllib.parse.parse_qs(rendered, keep_blank_values=True)
        assert parsed["text"] == ["a=b&c d\nnewline"]
        assert parsed["chat"] == ["-1"]

    def test_urlencode_filter_inside_json_template(self):
        context = {"text": "hola & adiós"}
        body = json.loads(render_template('{"u":"{text|urlencode}"}', context))
        assert urllib.parse.unquote(body["u"]) == "hola & adiós"

    def test_raw_filter_is_the_documented_footgun(self):
        # Positive control: prove the validity checks in this file CAN fail —
        # |raw injects the newline+quote unescaped and breaks the JSON body.
        context = {"text": 'break"me\nnow'}
        rendered = render_template('{"t":"{text|raw}"}', context)
        with pytest.raises(json.JSONDecodeError):
            json.loads(rendered)

    def test_unknown_filter_falls_back_to_auto(self):
        body = json.loads(render_template('{"t":"{text|bogus}"}', {"text": 'q"q'}))
        assert body["t"] == 'q"q'

    def test_emoji_stays_literal(self):
        body = json.loads(render_template('{"t":"{text}"}', {"text": "café 🎉"}))
        assert body["t"] == "café 🎉"

    def test_numeric_escaping_is_identity(self):
        assert render_template('{"n":{chat_id}}', {"chat_id": -100987}) == '{"n":-100987}'


def _config(**overrides):
    """A config whose webhook attrs are real values (MagicMock elsewhere)."""
    config = MagicMock()
    config.event_webhook_enabled = True
    config.event_webhook_url = SECRET_URL
    config.event_webhook_method = "POST"
    config.event_webhook_headers = {"Content-Type": "application/json; charset=utf-8"}
    config.event_webhook_events = {"message_edited", "message_deleted"}
    config.event_webhook_chat_ids = set()
    config.event_webhook_body_template = ""
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _response(status_code: int):
    response = MagicMock()
    response.status_code = status_code
    return response


class _StubAsyncClient:
    """Stands in for httpx.AsyncClient; scripted per-attempt outcomes."""

    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list = []
        _StubAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _reset_stub(monkeypatch):
    _StubAsyncClient.instances = []
    monkeypatch.setattr("src.event_webhook.httpx.AsyncClient", _StubAsyncClient)
    monkeypatch.setattr(EventWebhookSender, "BACKOFFS", (0.0, 0.0))
    yield


async def _fire_and_wait(sender: EventWebhookSender, event: str, context: dict):
    sender.fire(event, context)
    while sender._tasks:
        await asyncio.sleep(0)


class TestEventWebhookSender:
    def test_bare_magicmock_config_is_inert(self):
        sender = EventWebhookSender(MagicMock(), {})
        assert sender._enabled is False
        assert sender.wants("message_edited", 1) is False

    def test_wants_matrix(self):
        stats: dict = {}
        sender = EventWebhookSender(_config(event_webhook_events={"message_deleted"}), stats)
        assert sender.wants("message_deleted", 1) is True
        assert sender.wants("message_edited", 1) is False
        filtered = EventWebhookSender(_config(event_webhook_chat_ids={-1001}), stats)
        assert filtered.wants("message_edited", -1001) is True
        assert filtered.wants("message_edited", -1002) is False
        disabled = EventWebhookSender(_config(event_webhook_enabled=False), stats)
        assert disabled.wants("message_edited", 1) is False

    def test_content_type_drives_auto_filter(self):
        form = EventWebhookSender(
            _config(event_webhook_headers={"content-type": "application/x-www-form-urlencoded"}), {}
        )
        assert form._auto == "urlencode"
        default = EventWebhookSender(_config(), {})
        assert default._auto == "jsonescape"

    async def test_success_first_attempt(self):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [_response(200)]
        await _fire_and_wait(
            sender, "message_edited", {"event": "message_edited", "account_id": 1, "chat_id": 1, "message_id": 2}
        )
        assert stats.get("webhook_sent") == 1
        client = _StubAsyncClient.instances[0]
        assert client.kwargs == {"timeout": 5.0, "follow_redirects": False}
        method, url, request_kwargs = client.requests[0]
        assert (method, url) == ("POST", SECRET_URL)
        assert json.loads(request_kwargs["content"].decode("utf-8"))["message_id"] == 2

    async def test_5xx_retries_then_succeeds(self):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [_response(500), _response(200)]
        await _fire_and_wait(sender, "message_edited", {"event": "message_edited", "chat_id": 1})
        assert stats.get("webhook_sent") == 1
        assert len(_StubAsyncClient.instances[0].requests) == 2

    async def test_429_and_transport_error_retry(self):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [_response(429), httpx.ConnectError("boom"), _response(200)]
        await _fire_and_wait(sender, "message_deleted", {"event": "message_deleted", "chat_id": 1})
        assert stats.get("webhook_sent") == 1
        assert len(_StubAsyncClient.instances[0].requests) == 3

    async def test_404_is_permanent_single_attempt(self, caplog):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [_response(404)]
        with caplog.at_level(logging.WARNING, logger="src.event_webhook"):
            await _fire_and_wait(sender, "message_edited", {"event": "message_edited", "chat_id": 1})
        assert stats.get("webhook_failed") == 1
        assert stats.get("webhook_sent") is None
        assert len(_StubAsyncClient.instances[0].requests) == 1
        assert "HTTP 404" in caplog.text

    async def test_redirect_is_permanent(self):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [_response(302)]
        await _fire_and_wait(sender, "message_edited", {"event": "message_edited", "chat_id": 1})
        assert stats.get("webhook_failed") == 1
        assert len(_StubAsyncClient.instances[0].requests) == 1

    async def test_three_failures_exhaust(self, caplog):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [httpx.ReadTimeout("t"), _response(503), httpx.ConnectError("c")]
        with caplog.at_level(logging.WARNING, logger="src.event_webhook"):
            await _fire_and_wait(sender, "message_edited", {"event": "message_edited", "chat_id": 1})
        assert stats.get("webhook_failed") == 1
        assert len(_StubAsyncClient.instances[0].requests) == 3
        assert caplog.text.count("Event webhook delivery failed") == 1

    async def test_in_flight_cap_drops(self):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        sender._tasks = {MagicMock() for _ in range(sender.MAX_IN_FLIGHT)}
        sender.fire("message_edited", {"event": "message_edited", "chat_id": 1})
        assert stats.get("webhook_dropped") == 1
        assert len(sender._tasks) == sender.MAX_IN_FLIGHT

    async def test_aclose_cancels_outstanding(self):
        sender = EventWebhookSender(_config(), {})

        async def hang():
            await asyncio.Event().wait()

        task = asyncio.create_task(hang())
        sender._tasks.add(task)
        task.add_done_callback(sender._on_task_done)
        await sender.aclose()
        assert task.cancelled()
        assert not sender._tasks

    async def test_no_pii_in_logs(self, caplog):
        """The URL (a capability secret) and body text never reach any record."""
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)
        _StubAsyncClient.script = [httpx.ConnectError(f"cannot reach {SECRET_URL}"), _response(500), _response(503)]
        with caplog.at_level(logging.DEBUG, logger="src.event_webhook"):
            await _fire_and_wait(
                sender, "message_edited", {"event": "message_edited", "chat_id": -100555, "text": "secret message body"}
            )
        assert stats.get("webhook_failed") == 1
        for record in caplog.records:
            rendered = record.getMessage()
            assert SECRET_URL not in rendered
            assert "secret-path-token" not in rendered
            assert "secret message body" not in rendered
            assert "-100555" not in rendered

    async def test_task_exception_hits_done_callback(self, caplog):
        stats: dict = {}
        sender = EventWebhookSender(_config(), stats)

        async def boom(event, body):
            raise RuntimeError(f"leaky {SECRET_URL}")

        sender._deliver = boom
        with caplog.at_level(logging.WARNING, logger="src.event_webhook"):
            sender.fire("message_edited", {"event": "message_edited", "chat_id": 1})
            while sender._tasks:
                await asyncio.sleep(0)
        assert stats.get("webhook_failed") == 1
        assert "RuntimeError" in caplog.text
        assert SECRET_URL not in caplog.text
