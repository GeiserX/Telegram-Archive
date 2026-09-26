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


def _video(html: str) -> str:
    return _between(html, "<!-- Videos - click to open in lightbox -->", "<!-- Stickers")


def _file(html: str) -> str:
    return _between(html, "<!-- Other documents / files -->", "<!-- Text with clickable links")


def _assert_accessible_toggle(test: unittest.TestCase, block: str) -> None:
    """The bubble's button is the audio bubble's: named, tied to its region, busy while loading."""
    button = block[block.index("<button v-if=") :]
    button = button[: button.index("</button>")]
    test.assertIn("hasTranscriptButton(msg)", button)
    test.assertIn('type="button"', button)
    test.assertIn('@click.stop="pressTranscript(msg)"', button)
    test.assertIn(":aria-expanded=\"isTranscriptExpanded(msg) ? 'true' : 'false'\"", button)
    test.assertIn(':aria-controls="transcriptRegionId(msg)"', button)
    test.assertIn(":aria-busy=\"transcriptStatus(msg) === 'loading' ? 'true' : 'false'\"", button)
    test.assertIn("'Hide transcript' : 'Show transcript'", button)
    test.assertIn('class="transcript-loop"', button)
    region = block[block.index(':id="transcriptRegionId(msg)"') :]
    test.assertEqual(block.count(':id="transcriptRegionId(msg)"'), 1)
    test.assertIn('<p dir="auto" class="transcript-text', region)
    test.assertIn("transcriptErrorText(msg)", region)
    test.assertIn('aria-label="Transcript version"', region)


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

    def test_a_video_carries_the_button_over_the_corner_and_the_text_under_the_player(self):
        block = _video(_html())
        _assert_accessible_toggle(self, block)
        overlay = _between(block, "<button v-if=", "</button>")
        self.assertIn('class="transcript-btn transcript-btn--overlay"', overlay)
        self.assertIn(":class=\"isOwnMessage(msg) ? 'left-1' : 'right-1'\"", overlay)
        # Inside the player that opens the lightbox, so the press must not open it too.
        self.assertLess(block.index('@click="msg.mediaLoadFailed || openMedia(msg)"'), block.index("<button v-if="))
        self.assertLess(block.index("</button>"), block.index(':id="transcriptRegionId(msg)"'))

    def test_a_file_carries_the_button_beside_its_name_and_the_text_under_it(self):
        block = _file(_html())
        _assert_accessible_toggle(self, block)
        self.assertIn('<button v-if="hasTranscriptButton(msg)" type="button"', block)
        # A button inside the download link would be invalid markup and a nested control.
        link = _between(block, "<a :href=", "</a>")
        self.assertNotIn("<button", link)
        self.assertLess(block.index("</a>"), block.index("<button v-if="))

    def test_a_done_transcript_with_no_text_says_no_speech_detected(self):
        """Silence comes back as an empty text; the bubble says so in the error states' grey, as the official apps do."""
        html = _html()
        pairs = re.findall(
            r'<p dir="auto" class="transcript-text[^\n]*v-if="selectedTranscript\(msg\)\.text"></p>\n'
            r' *<p v-else class="text-\[11px\] text-tg-n400">No speech detected</p>',
            html,
        )
        self.assertEqual(len(pairs), 4)
        self.assertEqual(len(pairs), html.count('class="transcript-text text-sm'))
        # The same small grey style as the failed and skipped reasons.
        self.assertIn('class="text-[11px] text-tg-n400">{{ transcriptErrorText(msg) }}</p>', html)

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
const mediaGalleryItems = ref([]);
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
            // Every media with sound shows the button before any row exists.
            assert.equal(hasTranscriptButton({ id: 8, media: { id: '8_audio', type: 'audio' } }), true)
            assert.equal(hasTranscriptButton({ id: 9, media: { id: '9_video', type: 'video' } }), true)
            assert.equal(hasTranscriptButton({ id: 14, media: { id: '14_document', type: 'document', mime_type: 'audio/flac' } }), true)
            assert.equal(hasTranscriptButton({ id: 15, media: { id: '15_document', type: 'document', mime_type: 'Video/x-matroska' } }), true)
            // No sound, no button: a PDF, a document with no mime type, a GIF-style clip.
            assert.equal(hasTranscriptButton({ id: 16, media: { id: '16_document', type: 'document', mime_type: 'application/pdf' } }), false)
            assert.equal(hasTranscriptButton({ id: 17, media: { id: '17_document', type: 'document' } }), false)
            assert.equal(hasTranscriptButton({ id: 18, media: { id: '18_animation', type: 'animation' } }), false)
            // A row found another way still shows, whatever the type.
            assert.equal(hasTranscriptButton({ id: 19, media: { id: '19_photo', type: 'photo', transcripts: [done(1)] } }), true)
            assert.equal(hasTranscriptButton({ id: 10, media: { id: '10_video_note', type: 'video_note' } }), true)
            // A no-download login reads no transcript; the routes would answer 403.
            assert.equal(hasTranscriptButton({ id: 13, media: { id: '13_voice', type: 'voice', no_download: true } }), false)
            """
        )
    )


def test_the_bubble_rule_is_the_drains_rule() -> None:
    """The template's isTranscribable mirrors is_transcribable in transcription_contract."""
    from src.transcription_contract import is_transcribable

    cases = [
        (media_type, mime)
        for media_type in ("voice", "video_note", "audio", "video", "document", "animation", "photo", "sticker")
        for mime in (None, "audio/ogg", "Audio/FLAC", "video/x-matroska", "application/pdf", "image/png", "")
    ]
    expected = [is_transcribable(media_type, mime) for media_type, mime in cases]
    assert any(expected) and not all(expected)
    media = [{"id": i, "media": {"id": f"{i}_x", "type": t, "mime_type": m}} for i, (t, m) in enumerate(cases)]
    _run_node(
        _script(
            f"""
            transcriptionState.value = {{ enabled: true, configured: true }}
            const media = {json.dumps(media)}
            assert.deepEqual(media.map(hasTranscriptButton), {json.dumps(expected)})
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


def test_the_open_state_map_keeps_the_most_recent_500_entries() -> None:
    old = {f"refA:{n}": True for n in range(600)}
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            // Pressing an old entry again makes it the newest one.
            await pressTranscript(voice(5, [done(4)]))
            const kept = JSON.parse(stored.get('transcriptOpen'))
            const keys = Object.keys(kept)
            assert.equal(keys.length, 500)
            assert.equal(keys[keys.length - 1], 'refA:5')
            assert.equal(kept['refA:5'], false)
            assert.equal(keys[0], 'refA:101', 'the oldest entries went first')
            assert.equal('refA:100' in kept, false)
            assert.equal(Object.keys(transcriptOpen.value).length, 500)
            """,
            stored={"transcriptOpen": json.dumps(old)},
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
            messages.value = [{ id: 7, media: { id: '7_video_note', type: 'video_note' } }, { id: 8, media: { id: '8_video', type: 'video' } }]
            assert.equal(transcriptNudgeVisible.value, false, 'only a voice message, the default type, shows it')
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


def test_a_chat_switch_during_a_request_never_writes_into_the_other_chat() -> None:
    """Message 7 of chat A and message 7 of chat B are two messages; a late answer for A stays out of B."""
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const inA = voice(7, []), inB = voice(7, [])
            let release
            const held = body => new Promise(resolve => { release = () => resolve({ ok: true, json: async () => body }) })
            const switchChat = () => { selectedChat.value = { ref: 'refB' }; messages.value = [inB] }

            messages.value = [inA]
            respond = () => held([done(21, { text: 'chat A words' })])
            const refreshing = refreshTranscripts(inA)
            switchChat()
            release()
            await refreshing
            assert.deepEqual(inB.media.transcripts, [])
            assert.equal(inB.media.transcript, undefined)

            selectedChat.value = { ref: 'refA' }
            messages.value = [inA]
            respond = () => held({ id: 22, status: 'queued' })
            const pressing = pressTranscript(inA)
            switchChat()
            release()
            await pressing
            assert.deepEqual(inB.media.transcripts, [])
            assert.equal(transcriptStatus(inB), 'none')
            assert.deepEqual(toasts, [])
            """
        )
    )


def test_a_refused_ask_shows_the_servers_reason() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const msg = voice(3, [])
            messages.value = [msg]
            respond = () => ({ ok: false, status: 409, json: async () => ({ detail: 'Not downloaded yet' }) })
            await pressTranscript(msg)
            assert.deepEqual(toasts, ['Not downloaded yet'])
            assert.equal(transcriptStatus(msg), 'none', 'no row, no stroke')
            respond = () => ({ ok: false, status: 500, json: async () => ({ detail: 'Internal server error' }) })
            await pressTranscript(msg)
            assert.deepEqual(toasts, ['Not downloaded yet', 'Could not ask for a transcript'])
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

    async def test_a_media_the_drain_would_never_send_is_a_409_and_no_row(self, real_adapter, viewer):
        """Not downloaded yet, or a type no server transcribes: its queued row would never move."""
        await _media(real_adapter, "m_1_voice", downloaded=False)
        await _media(real_adapter, "m_2_photo", media_type="photo")
        ref = await _chat_ref(real_adapter)
        async with _client() as client:
            pending = [
                await client.post("/api/media/m_1_voice/transcripts"),
                await client.post(f"/api/chats/{ref}/media/1_voice/transcripts"),
            ]
            photo = [
                await client.post("/api/media/m_2_photo/transcripts"),
                await client.post(f"/api/chats/{ref}/media/2_photo/transcripts"),
            ]
        assert [resp.status_code for resp in pending + photo] == [409, 409, 409, 409]
        assert {resp.json()["detail"] for resp in pending} == {"Not downloaded yet"}
        for media_id in ("m_1_voice", "m_2_photo"):
            assert await real_adapter.list_media_transcripts(media_id, account_id=1) == []

    async def test_a_video_and_an_audio_document_can_be_asked_and_a_pdf_cannot(self, real_adapter, viewer):
        await _media(real_adapter, "m_1_video", media_type="video", mime_type="video/mp4")
        await _media(real_adapter, "m_2_document", media_type="document", mime_type="audio/x-wav")
        await _media(real_adapter, "m_3_document", media_type="document", mime_type="video/x-matroska")
        await _media(real_adapter, "m_4_document", media_type="document", mime_type="application/pdf")
        await _media(real_adapter, "m_5_animation", media_type="animation", mime_type="video/mp4")
        ref = await _chat_ref(real_adapter)
        async with _client() as client:
            by_id = [await client.post(f"/api/media/{m}/transcripts") for m in ("m_1_video", "m_2_document")]
            by_chat = await client.post(f"/api/chats/{ref}/media/3_document/transcripts")
            refused = [
                await client.post("/api/media/m_4_document/transcripts"),
                await client.post(f"/api/chats/{ref}/media/4_document/transcripts"),
                await client.post("/api/media/m_5_animation/transcripts"),
            ]
        assert [resp.status_code for resp in [*by_id, by_chat]] == [200, 200, 200], [r.text for r in by_id]
        assert [resp.status_code for resp in refused] == [409, 409, 409]
        for media_id in ("m_1_video", "m_2_document", "m_3_document"):
            [row] = await real_adapter.list_media_transcripts(media_id, account_id=1)
            assert row["status"] == "queued"
        for media_id in ("m_4_document", "m_5_animation"):
            assert await real_adapter.list_media_transcripts(media_id, account_id=1) == []
        # The drain sends what was asked, even with documents and videos outside TRANSCRIPTION_TYPES.
        assert sorted(await _drain(real_adapter, types=("voice",))) == ["m_1_video", "m_2_document", "m_3_document"]
        assert viewer == []

    async def test_an_asked_type_outside_transcription_types_is_still_sent(self, real_adapter, viewer):
        """The viewer does not know TRANSCRIPTION_TYPES, so the drain honours the click."""
        await _media(real_adapter, "m_1_audio", media_type="audio")
        await _media(real_adapter, "m_2_audio", media_type="audio")
        async with _client() as client:
            resp = await client.post("/api/media/m_1_audio/transcripts")
        assert resp.status_code == 200, resp.text
        assert await _drain(real_adapter, types=("voice",)) == ["m_1_audio"]

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

    async def test_media_with_sound_carries_an_empty_list_and_a_pdf_carries_none(self, real_adapter, viewer):
        """The empty list is what a bubble with no row yet renders from; a PDF has nothing to transcribe."""
        await _media(real_adapter, "m_1_video", media_type="video", mime_type="video/mp4")
        await _media(real_adapter, "m_2_document", media_type="document", mime_type="audio/flac")
        await _media(real_adapter, "m_3_document", media_type="document", mime_type="application/pdf")
        async with _client() as client:
            resp = await client.get(f"/api/chats/{await _chat_ref(real_adapter)}/messages")
        assert resp.status_code == 200, resp.text
        by_id = {m["id"]: m["media"] for m in resp.json()}
        assert by_id[1]["transcripts"] == [] and by_id[1]["transcript"] is None
        assert by_id[2]["transcripts"] == [] and by_id[2]["transcript"] is None
        assert "transcripts" not in by_id[3]


class TestDrainPicksUpTheAsk:
    async def test_a_video_outside_the_types_waits_for_its_click_then_the_next_drain_sends_it(
        self, real_adapter, viewer, tmp_path, monkeypatch
    ):
        """TRANSCRIPTION_TYPES is what goes ahead of time; any other file with sound goes when its button is pressed."""
        import test_transcription as sync

        sync._fake_ffprobe(
            tmp_path,
            monkeypatch,
            'echo \'{"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "4.0"}}\'\n',
        )
        path = tmp_path / str(CHAT) / "clip.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(sync.AUDIO)
        await sync._file_media(real_adapter, path, "m_1_video", media_type="video", mime_type="video/mp4")
        server = sync.FakeServer()
        config = sync._config(str(tmp_path), transcription_types={"voice"})

        async def drain() -> dict:
            return await sync.drain_transcriptions(
                config, real_adapter, account_id=1, notifier=AsyncMock(), client=sync._client(config, server)
            )

        assert (await drain())["done"] == 0
        assert server.transcribe_requests == []
        assert await real_adapter.list_media_transcripts("m_1_video", account_id=1) == []

        async with _client() as client:
            asked = await client.post(f"/api/chats/{await _chat_ref(real_adapter)}/media/1_video/transcripts")
        assert asked.status_code == 200, asked.text

        assert (await drain())["done"] == 1
        assert len(server.transcribe_requests) == 1
        [row] = await real_adapter.list_media_transcripts("m_1_video", account_id=1)
        assert (row["id"], row["status"], row["text"]) == (asked.json()["id"], "done", "hola, te llamo luego")
        assert viewer == []

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
