"""The transcript button in the bubble (slice 3 of docs/TRANSCRIPTION.md).

Three layers. The markup: a real button with the accessibility attributes
the design asks for, the text at full width with ``dir="auto"``, the round
video overlay, the nudge, the header toggle and the settings row. The
behaviour: the transcript block of the template, lifted verbatim and run
under node with a stubbed ``localStorage`` and ``fetch``, so the state
machine, the remembered open state, expand-all, the 350 ms loading floor,
the picker and the nudge are executed rather than grepped. The routes: the
insert-only ask-now on a real database, the status read, the page payload,
and the drain picking the ask-now row up first.
"""

import json
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_frontend_audit_fixes import INDEX_HTML, _run_node  # noqa: E402

BLOCK_START = "                // Voice transcripts (docs/TRANSCRIPTION.md"
BLOCK_END = "                // End of the voice transcript block.\n"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _between(html: str, start: str, end: str) -> str:
    begin = html.index(start)
    return html[begin : html.index(end, begin)]


def _audio_bubble(html: str) -> str:
    return _between(html, '<div v-else-if="isAudioFile(msg)"', "<!-- GIFs / Animations")


def _round_video(html: str) -> str:
    return _between(html, "<div v-else-if=\"msg.media?.type === 'video_note'\">", "<!-- Videos - click to open")


# ============================================================================
# Markup
# ============================================================================


class TestBubbleMarkup(unittest.TestCase):
    def test_the_audio_bubble_button_is_an_accessible_toggle(self):
        bubble = _audio_bubble(_html())
        button = _between(bubble, '<button v-if="hasTranscriptButton(msg)"', "</button>")
        self.assertIn('type="button"', button)
        self.assertIn('class="transcript-btn"', button)
        self.assertIn(":aria-expanded=\"isTranscriptExpanded(msg) ? 'true' : 'false'\"", button)
        self.assertIn(':aria-controls="transcriptRegionId(msg)"', button)
        self.assertIn(":aria-busy=\"transcriptStatus(msg) === 'loading' ? 'true' : 'false'\"", button)
        self.assertIn("'Hide transcript' : 'Show transcript'", button)
        self.assertIn("isTranscriptExpanded(msg) ? 'A→' : '→A'", button)
        self.assertIn('class="transcript-loop"', button)
        self.assertIn('pathLength="100"', button)

    def test_the_button_sits_on_the_waveform_row_and_the_text_under_it(self):
        bubble = _audio_bubble(_html())
        row_end = bubble.index("<!-- The transcript under the row")
        self.assertLess(bubble.index('<button v-if="hasTranscriptButton(msg)"'), row_end)
        region = bubble[row_end:]
        self.assertIn(':id="transcriptRegionId(msg)"', region)
        self.assertIn('<p dir="auto" class="transcript-text', region)
        self.assertIn("whitespace-pre-wrap", region)
        # No line cap and no "show more": a long transcript makes the bubble taller.
        for cap in ("line-clamp", "truncate", "max-h-", "Show more"):
            self.assertNotIn(cap, _between(region, '<p dir="auto"', "</p>"))
        self.assertIn("transcriptErrorText(msg)", region)
        self.assertIn('<select v-if="doneTranscripts(msg).length > 1"', region)
        self.assertIn('aria-label="Transcript version"', region)

    def test_the_round_video_keeps_its_circle_and_the_button_sits_on_the_corner(self):
        block = _round_video(_html())
        self.assertIn('v-show="!isTranscriptExpanded(msg)"', block)
        self.assertIn('class="round-video gif-video cursor-pointer"', block)
        overlay = _between(block, "transcript-btn--overlay", "</button>")
        self.assertIn(":class=\"isOwnMessage(msg) ? 'left-1' : 'right-1'\"", overlay)
        self.assertIn(':aria-controls="transcriptRegionId(msg)"', overlay)
        # Expanded, it becomes a voice bubble with a placeholder waveform.
        self.assertIn('v-for="(h, i) in transcriptPlaceholderBars"', block)
        self.assertIn('<p dir="auto" class="transcript-text', block)
        self.assertEqual(block.count(':id="transcriptRegionId(msg)"'), 1)

    def test_a_polite_live_region_announces_the_result(self):
        html = _html()
        self.assertIn(
            '<div class="sr-only" role="status" aria-live="polite" aria-atomic="true">{{ transcriptAnnouncement }}</div>',
            html,
        )

    def test_the_nudge_names_the_variable_and_links_akou(self):
        banner = _between(_html(), '<div v-if="transcriptNudgeVisible"', "<!-- Pinned Message Banner")
        self.assertIn("Voice messages can be transcribed automatically.", banner)
        self.assertIn("TRANSCRIPTION_URL", banner)
        self.assertIn('href="https://github.com/GeiserX/akou"', banner)
        self.assertIn('@click="dismissTranscriptNudge"', banner)

    def test_the_header_toggle_and_the_settings_row(self):
        html = _html()
        toggle = _between(html, '<button v-if="transcriptionState.enabled"', "</button>")
        self.assertIn('@click="toggleExpandAllTranscripts"', toggle)
        self.assertIn(":aria-pressed=", toggle)
        self.assertIn("'Collapse all transcripts' : 'Expand all transcripts'", toggle)
        self.assertIn('<dd class="text-tg-ink text-right">{{ transcriptionSettingText }}</dd>', html)

    def test_the_button_styles_read_theme_tokens(self):
        html = _html()
        css = _between(html, ".transcript-btn {", "@media (prefers-reduced-motion: reduce)")
        self.assertIn("rgb(var(--tg-n300))", css)
        self.assertIn("rgb(var(--tg-accent))", css)
        # Only the overlay's white-on-scrim is a literal, like the other media overlays.
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,6}\b", css), ["#fff"])

    def test_the_setup_returns_what_the_markup_uses(self):
        html = _html()
        returned = html[html.rindex("return {") :]
        for name in (
            "transcriptionState",
            "transcriptionSettingText",
            "transcriptPlaceholderBars",
            "transcriptAnnouncement",
            "transcriptRegionId",
            "hasTranscriptButton",
            "transcriptStatus",
            "isTranscriptExpanded",
            "showTranscriptText",
            "selectedTranscript",
            "doneTranscripts",
            "pickTranscript",
            "transcriptCaption",
            "transcriptErrorText",
            "pressTranscript",
            "transcriptsExpandedForChat",
            "toggleExpandAllTranscripts",
            "transcriptNudgeVisible",
            "dismissTranscriptNudge",
        ):
            self.assertIn(f"                    {name},\n", returned, name)


# ============================================================================
# Behaviour, executed under node
# ============================================================================

PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const computed = getter => ({ get value() { return getter(); } });
const stored = new Map(Object.entries(__STORED__));
const localStorage = { getItem: k => stored.has(k) ? stored.get(k) : null, setItem: (k, v) => stored.set(k, String(v)) };
const messages = ref([]);
const messageHighlight = ref(null);
const selectedChat = ref({ ref: 'refA' });
const toasts = [];
const showToast = message => toasts.push(message);
const requests = [];
let respond = () => ({ ok: true, json: async () => ({}) });
const fetch = async (url, init) => { requests.push([url, (init && init.method) || 'GET']); return respond(url, init); };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const done = (id, extra = {}) => ({ id, status: 'done', text: `text ${id}`, source: 'akou', engine_name: 'akou',
    models: ['parakeet-v3'], preset: 'auto', language: 'es', completed_at: '2026-01-02T03:04:05', ...extra });
const voice = (id, transcripts) => ({ id, media: { id: `${id}_voice`, type: 'voice', transcripts } });
"""


def _script(body: str, stored: dict | None = None) -> str:
    block = _between(_html(), BLOCK_START, BLOCK_END)
    prelude = PRELUDE.replace("__STORED__", json.dumps(stored or {}))
    wrapped = ";(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    return "\n".join([prelude, block, wrapped])


def test_the_states_follow_the_design_table() -> None:
    _run_node(
        _script(
            """
            const none = voice(1, [])
            assert.equal(hasTranscriptButton(none), false, 'feature off: no button')
            transcriptionState.value = { enabled: true, configured: false }
            assert.equal(hasTranscriptButton(none), true)
            assert.equal(transcriptStatus(none), 'unconfigured')
            transcriptionState.value = { enabled: true, configured: true }
            assert.equal(transcriptStatus(none), 'none')
            transcriptionState.value = { enabled: true, configured: false }
            const stuck = voice(11, [{ id: 12, status: 'queued' }])
            assert.equal(transcriptStatus(stuck), 'unconfigured', 'no stroke on a row that cannot move')
            assert.equal(transcriptStatus(voice(12, [{ id: 13, status: 'queued' }, done(4)])), 'done')
            transcriptionState.value = { enabled: true, configured: true }
            assert.equal(transcriptStatus(stuck), 'loading')
            assert.equal(transcriptStatus(voice(2, [{ id: 5, status: 'queued' }])), 'loading')
            assert.equal(transcriptStatus(voice(3, [{ id: 5, status: 'running' }, done(4)])), 'loading')
            assert.equal(transcriptStatus(voice(4, [done(4)])), 'done')
            assert.equal(transcriptStatus(voice(5, [{ id: 6, status: 'failed', error: 'submit_failed' }, done(4)])), 'done')
            const skipped = voice(6, [{ id: 7, status: 'skipped', error: 'longer than the 1800 second limit' }])
            assert.equal(transcriptStatus(skipped), 'error')
            assert.equal(transcriptErrorText(skipped), 'Longer than the 30 minute limit')
            assert.equal(transcriptErrorText(voice(7, [{ id: 8, status: 'failed', error: 'file_missing' }])), 'The audio file is missing')
            // Music is opt-in: an audio bubble shows the button only once a row exists.
            assert.equal(hasTranscriptButton({ id: 8, media: { id: '8_audio', type: 'audio' } }), false)
            assert.equal(hasTranscriptButton({ id: 9, media: { id: '9_audio', type: 'audio', transcripts: [done(1)] } }), true)
            assert.equal(hasTranscriptButton({ id: 10, media: { id: '10_video_note', type: 'video_note' } }), true)
            """
        )
    )


def test_open_state_is_remembered_per_message_and_survives_a_reload() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const a = voice(1, [done(4)]), b = voice(2, [done(5)])
            assert.equal(isTranscriptExpanded(a), false)
            await pressTranscript(a)
            assert.equal(isTranscriptExpanded(a), true)
            assert.equal(isTranscriptExpanded(b), false)
            assert.equal(showTranscriptText(a), true)
            assert.deepEqual(JSON.parse(stored.get('transcriptOpen')), { 'refA:1': true })
            await pressTranscript(a)
            assert.equal(isTranscriptExpanded(a), false)
            assert.equal(requests.length, 0, 'a done bubble toggles without a request')
            // The same message id in another chat is another message.
            await pressTranscript(a)
            selectedChat.value = { ref: 'refB' }
            assert.equal(isTranscriptExpanded(a), false)
            """
        )
    )
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            assert.equal(isTranscriptExpanded(voice(1, [done(4)])), true, 'restored from localStorage')
            assert.equal(isTranscriptExpanded(voice(1, [])), false, 'nothing to show without a done row')
            """,
            stored={"transcriptOpen": json.dumps({"refA:1": True})},
        )
    )


def test_expand_all_is_per_chat_remembered_and_wins_over_hand_set_states() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const a = voice(1, [done(4)]), b = voice(2, [done(5)])
            await pressTranscript(a)
            await pressTranscript(a)
            assert.equal(isTranscriptExpanded(a), false)
            toggleExpandAllTranscripts()
            assert.equal(transcriptsExpandedForChat(), true)
            assert.equal(isTranscriptExpanded(a), true, 'the toggle clears the hand-set close')
            assert.equal(isTranscriptExpanded(b), true)
            assert.deepEqual(JSON.parse(stored.get('transcriptExpandAll')), { refA: true })
            await pressTranscript(b)
            assert.equal(isTranscriptExpanded(b), false, 'one message can still be closed by hand')
            selectedChat.value = { ref: 'refB' }
            assert.equal(transcriptsExpandedForChat(), false)
            assert.equal(isTranscriptExpanded(a), false)
            """
        )
    )


def test_asking_now_posts_once_holds_loading_and_opens_when_the_row_is_done() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const msg = voice(3, [])
            messages.value = [msg]
            respond = (url, init) => ({ ok: true, json: async () => (init && init.method === 'POST')
                ? { id: 11, status: 'queued' } : [done(11)] })
            const started = Date.now()
            const pressing = pressTranscript(msg)
            assert.equal(transcriptStatus(msg), 'loading', 'loading shows at once')
            await pressing
            assert.ok(Date.now() - started >= 340, 'loading lasts at least 350 ms')
            assert.deepEqual(requests, [['/api/chats/refA/media/3_voice/transcripts', 'POST']])
            assert.equal(transcriptStatus(msg), 'loading', 'the queued row keeps the stroke')
            assert.equal(msg.media.transcripts[0].id, 11)
            await pressTranscript(msg)
            assert.equal(requests.length, 1, 'no second ask while one is open')
            // The realtime frame refetches the rows; the bubble opens and says so.
            await refreshTranscripts(msg)
            assert.equal(transcriptStatus(msg), 'done')
            assert.equal(isTranscriptExpanded(msg), true)
            assert.equal(msg.media.transcript.id, 11)
            await sleep(80)
            assert.equal(transcriptAnnouncement.value, 'Transcript ready')
            """
        )
    )


def test_a_failed_ask_says_so_and_leaves_the_bubble_as_it_was() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const msg = voice(3, [])
            respond = () => ({ ok: false, status: 404, json: async () => ({}) })
            await pressTranscript(msg)
            assert.deepEqual(toasts, ['Could not ask for a transcript'])
            assert.equal(transcriptStatus(msg), 'none')
            """
        )
    )


def test_the_nudge_shows_for_a_voice_message_and_the_button_reopens_it() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: false }
            assert.equal(transcriptNudgeVisible.value, false, 'no voice message on screen')
            messages.value = [voice(1, [])]
            assert.equal(transcriptNudgeVisible.value, true)
            dismissTranscriptNudge()
            assert.equal(transcriptNudgeVisible.value, false)
            assert.equal(stored.get('transcriptNudgeDismissed'), '1')
            await pressTranscript(messages.value[0])
            assert.equal(transcriptNudgeVisible.value, true, 'the unconfigured button opens the nudge')
            assert.equal(requests.length, 0, 'and asks nothing')
            dismissTranscriptNudge()
            const failed = voice(2, [{ id: 3, status: 'failed', error: 'submit_failed' }, { id: 2, status: 'queued' }])
            await pressTranscript(failed)
            assert.equal(transcriptNudgeVisible.value, true, 'a failed or stuck bubble opens it too')
            assert.equal(requests.length, 0)
            transcriptionState.value = { enabled: true, configured: true }
            assert.equal(transcriptNudgeVisible.value, false)
            transcriptionState.value = { enabled: false, configured: false }
            assert.equal(transcriptNudgeVisible.value, false)
            """
        )
    )
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: false }
            messages.value = [voice(1, [])]
            assert.equal(transcriptNudgeVisible.value, false, 'dismissed stays dismissed')
            """,
            stored={"transcriptNudgeDismissed": "1"},
        )
    )


def test_attribution_picker_and_settings_row() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const msg = voice(1, [done(9), { id: 8, status: 'failed' },
                done(7, { source: 'openai', engine_name: 'speaches', models: ['whisper-1'], language: 'en', completed_at: '2026-01-01T00:00:00' })])
            assert.deepEqual(doneTranscripts(msg).map(r => r.id), [9, 7])
            assert.equal(selectedTranscript(msg).id, 9, 'the newest done row is the default')
            assert.deepEqual(transcriptCaption(msg),
                { engine: 'akou', link: 'https://github.com/GeiserX/akou', rest: ['parakeet-v3', 'es', '2026-01-02'] })
            pickTranscript(msg, '7')
            assert.equal(selectedTranscript(msg).id, 7)
            assert.deepEqual(transcriptCaption(msg), { engine: 'speaches', link: null, rest: ['whisper-1', 'en', '2026-01-01'] })

            assert.equal(transcriptionSettingText.value, 'on, server not detected yet')
            transcriptionState.value = { enabled: true, configured: true, server_name: 'akou', server_version: '0.2.0' }
            assert.equal(transcriptionSettingText.value, 'on, akou 0.2.0')
            transcriptionState.value = { enabled: true, configured: false }
            assert.equal(transcriptionSettingText.value, 'on, no server configured')
            transcriptionState.value = { enabled: false, configured: false }
            assert.equal(transcriptionSettingText.value, 'off')
            """
        )
    )


# ============================================================================
# Routes, on a real database
# ============================================================================

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from test_media_transcripts import CHAT, _drain, _media  # noqa: E402
from test_web_routes import web_main  # noqa: E402


@pytest.fixture
async def viewer(real_adapter):
    """The real app on a real adapter: auth off with the anonymous opt-in, transcription on."""
    saved = (web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER, web_main.config.display_chat_ids)
    web_main.db = real_adapter
    web_main.AUTH_ENABLED = False
    web_main.ALLOW_ANONYMOUS_VIEWER = True
    web_main.config.display_chat_ids = set()
    outbound = []

    async def _no_network(self, request):
        outbound.append(request)
        raise AssertionError("the viewer made an outbound request")

    with (
        patch.object(web_main.config, "transcription_enabled", True),
        patch.object(web_main.config, "transcription_url", "http://akou.example.test:9000"),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _no_network),
        patch.object(httpx.HTTPTransport, "handle_request", _no_network),
    ):
        try:
            yield outbound
        finally:
            web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER = saved[:3]
            web_main.config.display_chat_ids = saved[3]
            web_main.app.dependency_overrides.pop(web_main.require_auth, None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


async def _chat_ref(adapter) -> str:
    return (await adapter.get_chat_by_id(CHAT, account_id=1))["ref"]


class TestAskNowRoute:
    async def test_inserts_one_queued_row_and_a_second_call_is_a_noop_while_open(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_voice")
        async with _client() as client:
            first = await client.post("/api/media/m_1_voice/transcripts")
            second = await client.post("/api/media/m_1_voice/transcripts")
        assert first.status_code == 200, first.text
        assert second.status_code == 200
        assert first.json()["status"] == "queued"
        assert first.json()["job_id"] is None
        assert second.json()["id"] == first.json()["id"]
        rows = await real_adapter.list_media_transcripts("m_1_voice", account_id=1)
        assert [(r["status"], r["job_id"], r["preset"]) for r in rows] == [("queued", None, None)]
        assert viewer == [], "no outbound request"

    async def test_a_click_after_a_done_row_adds_another_row(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_voice")
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, preset="auto")
        await real_adapter.fill_media_transcript(row["id"], status="done", text="hola")
        async with _client() as client:
            resp = await client.post("/api/media/m_1_voice/transcripts")
        assert resp.status_code == 200
        rows = await real_adapter.list_media_transcripts("m_1_voice", account_id=1)
        assert [r["status"] for r in rows] == ["queued", "done"]

    async def test_outside_the_callers_grant_is_a_404_and_writes_nothing(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_voice")
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: web_main.UserContext(
            username="viewer-test", role="viewer", allowed_chat_refs={"not-this-chat"}
        )
        async with _client() as client:
            by_id = await client.post("/api/media/m_1_voice/transcripts")
            unknown = await client.post("/api/media/nope/transcripts")
            by_chat = await client.post(f"/api/chats/{await _chat_ref(real_adapter)}/media/1_voice/transcripts")
        assert (by_id.status_code, unknown.status_code, by_chat.status_code) == (404, 404, 404)
        assert await real_adapter.list_media_transcripts("m_1_voice", account_id=1) == []

    async def test_transcription_off_refuses_and_writes_nothing(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_voice")
        with patch.object(web_main.config, "transcription_enabled", False):
            async with _client() as client:
                resp = await client.post("/api/media/m_1_voice/transcripts")
        assert resp.status_code == 409
        assert await real_adapter.list_media_transcripts("m_1_voice", account_id=1) == []

    async def test_the_bubble_routes_are_addressed_by_chat_and_leak_no_storage_id(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_voice")
        url = f"/api/chats/{await _chat_ref(real_adapter)}/media/1_voice/transcripts"
        async with _client() as client:
            asked = await client.post(url)
            again = await client.post(url)
            listed = await client.get(url)
            missing = await client.get(url.replace("1_voice", "9_voice"))
        assert asked.status_code == 200
        assert again.json()["id"] == asked.json()["id"]
        assert [row["id"] for row in listed.json()] == [asked.json()["id"]]
        assert missing.status_code == 404
        for body in (asked.json(), listed.json()[0]):
            assert body["status"] == "queued"
            assert not {"media_id", "idempotency_key", "content_hash", "job_id", "words"} & set(body)
        assert str(CHAT) not in listed.text
        assert len(await real_adapter.list_media_transcripts("m_1_voice", account_id=1)) == 1
        assert viewer == []

    async def test_the_next_drain_sends_the_asked_media_first(self, real_adapter, viewer):
        from datetime import datetime

        await _media(real_adapter, "m_1_voice", download_date=datetime(2026, 1, 1))
        await _media(real_adapter, "m_2_voice", download_date=datetime(2026, 1, 3))
        async with _client() as client:
            resp = await client.post("/api/media/m_1_voice/transcripts")
        assert resp.status_code == 200
        assert await _drain(real_adapter, per_run=1) == ["m_1_voice"]


class TestStatusRoute:
    async def test_returns_only_the_four_fields_and_never_the_url(self, real_adapter, viewer):
        async with _client() as client:
            before = await client.get("/api/transcription/status")
            await real_adapter.set_transcription_server("akou", "0.2.0")
            after = await client.get("/api/transcription/status")
            with patch.object(web_main.config, "transcription_url", ""):
                unconfigured = await client.get("/api/transcription/status")
            with patch.object(web_main.config, "transcription_enabled", False):
                off = await client.get("/api/transcription/status")
        assert before.json() == {"enabled": True, "configured": True, "server_name": None, "server_version": None}
        assert after.json() == {"enabled": True, "configured": True, "server_name": "akou", "server_version": "0.2.0"}
        assert "akou.example.test" not in after.text
        assert unconfigured.json() == {
            "enabled": True,
            "configured": False,
            "server_name": None,
            "server_version": None,
        }
        assert off.json()["enabled"] is False
        assert off.json()["configured"] is False
        assert viewer == []


class TestMessagePagePayload:
    async def test_voice_media_carries_its_rows_and_the_newest_done_one(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_voice")
        await _media(real_adapter, "m_2_voice")
        first = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, preset="auto")
        await real_adapter.fill_media_transcript(first["id"], status="done", text="hola", language="es")
        second = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True)
        async with _client() as client:
            resp = await client.get(f"/api/chats/{await _chat_ref(real_adapter)}/messages")
        assert resp.status_code == 200, resp.text
        by_id = {m["id"]: m for m in resp.json()}
        media = by_id[1]["media"]
        assert [row["id"] for row in media["transcripts"]] == [second["id"], first["id"]]
        assert media["transcript"]["id"] == first["id"]
        assert media["transcript"]["text"] == "hola"
        assert by_id[2]["media"]["transcripts"] == []
        assert by_id[2]["media"]["transcript"] is None
        rows = json.dumps(media["transcripts"])
        assert "m_1_voice" not in rows, "no storage id, which spells the chat id"
        assert not {"media_id", "idempotency_key", "content_hash", "job_id", "words"} & set(media["transcripts"][0])


class TestDrainPicksUpTheAsk:
    async def test_the_backup_fills_the_preset_so_an_outage_does_not_resend_every_run(self, real_adapter, tmp_path):
        """Picked up, the ask-now row falls under the ten-minute rule like any row.

        Without the fill the row would keep no preset, stay first in the drain
        query and be re-sent on every run while the server is down.
        """
        import test_transcription as sync

        await sync._media(real_adapter, tmp_path, "m_1_voice", content_hash="c" * 64)
        asked = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1, force=True)
        server = sync.FakeServer(transcribe_down=True)
        config = sync._config(str(tmp_path))
        stats = await sync.drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=sync._client(config, server)
        )
        assert stats["unreachable"] == 1
        [row] = await real_adapter.list_media_transcripts("m_1_voice", account_id=1)
        assert (row["id"], row["status"], row["job_id"]) == (asked["id"], "queued", None)
        assert (row["preset"], row["idempotency_key"], row["content_hash"]) == ("auto", "c" * 64, "c" * 64)
        assert await _drain(real_adapter) == []
