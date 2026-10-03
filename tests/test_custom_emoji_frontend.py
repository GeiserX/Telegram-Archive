"""The viewer draws custom emoji over their fallback character.

The helpers are EXECUTED under node, lifted verbatim from the template: the
HTML builder of one custom emoji, the batcher that asks the server for ids,
the reaction chip's HTML, and the routing of moving emoji to their own play
budget. Template checks pin the parts Vue renders. Demo data only.
"""

import json
import re
import unittest

from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

HTML = INDEX_HTML.read_text(encoding="utf-8")

SUN = "5000000000000000001"  # above 2**53: Number() would round it
_BUILDER = (
    "const CUSTOM_EMOJI_ID_RE = /^",
    "const escapeEmojiText = (text) =>",
    "const customEmojiHtml = (id, fallbackText, options = {}) =>",
)
_STRIP = (
    "const strip = (html) => html.replace(/<[^>]*>/g, '')"
    ".replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '\"').replace(/&#39;/g, \"'\").replace(/&amp;/g, '&')\n"
)


def _build(info: dict, calls: str, support: dict | None = None):
    prelude = (
        "const ref = (value) => ({ value })\n"
        f"const customEmojiInfo = ref({json.dumps(info)})\n"
        f"const stickerSupport = {json.dumps(support or {'tgs': True, 'webm': True})}\n"
    )
    return _run_setup_program(HTML, _BUILDER, prelude, _STRIP + f"console.log(JSON.stringify({calls}))")


@unittest.skipUnless(NODE, "node is required to run the custom emoji helpers")
class TestCustomEmojiHtml(unittest.TestCase):
    INFO = {
        SUN: {"kind": "image", "alt": "☀️", "text_color": False, "ready": True},
        "5000000000000000002": {"kind": "tgs", "alt": "🌞", "text_color": False, "ready": True},
        "5000000000000000003": {"kind": "webm", "alt": "⭐", "text_color": False, "ready": True},
        "5000000000000000004": {"kind": None, "alt": "🏔️", "text_color": False, "ready": False},
        "5000000000000000005": {"kind": "image", "alt": "🎨", "text_color": True, "ready": True},
    }

    def test_each_kind_draws_its_picture_over_the_character(self) -> None:
        result = _build(
            self.INFO,
            "[customEmojiHtml('5000000000000000001', '☀️'), customEmojiHtml('5000000000000000002', '🌞'),"
            " customEmojiHtml('5000000000000000003', '⭐')]",
        )
        image, tgs, webm = result
        self.assertEqual(
            image,
            f'<span class="custom-emoji" data-emoji-id="{SUN}"><span class="custom-emoji-text">☀️</span>'
            f'<img class="custom-emoji-picture" src="/media/emoji/{SUN}" alt="" aria-hidden="true" draggable="false" decoding="async"></span>',
        )
        self.assertIn(
            '<span class="custom-emoji-picture tgs-sticker tgs-emoji" data-src="/media/emoji/5000000000000000002" aria-hidden="true"></span>',
            tgs,
        )
        self.assertIn('<span class="custom-emoji-text">🌞</span>', tgs)
        self.assertIn('class="custom-emoji-picture emoji-video" data-src="/media/emoji/5000000000000000003"', webm)
        self.assertIn(" muted loop playsinline ", webm)
        self.assertNotIn("data-still", image + tgs + webm)

    def test_the_character_alone_when_the_picture_cannot_be_drawn(self) -> None:
        result = _build(
            self.INFO,
            "[customEmojiHtml('5000000000000000004', '🏔️'), customEmojiHtml('5000000000000000005', '🎨'),"
            " customEmojiHtml('5000000000000000099', '🎭'), customEmojiHtml('5e18', '🎭'), customEmojiHtml(5, '🎭')]",
        )
        # Not fetched yet, tinted with the text colour, unknown, not an id.
        self.assertEqual(result, ["🏔️", "🎨", "🎭", "🎭", "🎭"])

    def test_an_id_that_is_not_digits_never_reaches_the_markup(self) -> None:
        hostile = '1" onmouseover="alert(1)'
        result = _build(
            {hostile: {"kind": "image", "alt": "x", "text_color": False, "ready": True}},
            f"customEmojiHtml({json.dumps(hostile)}, '🎭')",
        )
        self.assertEqual(result, "🎭")

    def test_a_kind_this_browser_cannot_play_keeps_the_character(self) -> None:
        result = _build(
            self.INFO,
            "[customEmojiHtml('5000000000000000002', '🌞'), customEmojiHtml('5000000000000000003', '⭐'),"
            " customEmojiHtml('5000000000000000001', '☀️').includes('<img')]",
            support={"tgs": False, "webm": False},
        )
        # Safari and iOS have no VP9 alpha, a browser without DecompressionStream no .tgs.
        self.assertEqual(result, ["🌞", "⭐", True])

    def test_still_marks_the_moving_kinds_for_the_first_frame_only(self) -> None:
        result = _build(
            self.INFO,
            "[customEmojiHtml('5000000000000000002', '🌞', { still: true }), customEmojiHtml('5000000000000000003', '⭐', { still: true })]",
        )
        self.assertTrue(all(' data-still="1"' in html for html in result))

    def test_the_fallback_is_escaped_and_the_text_is_the_fallback(self) -> None:
        hostile = '<img src=x onerror="alert(1)">&'
        result = _build(
            {SUN: {"kind": "image", "alt": hostile, "text_color": False, "ready": True}},
            f"[customEmojiHtml('{SUN}', {json.dumps(hostile)}), strip(customEmojiHtml('{SUN}', {json.dumps(hostile)})),"
            f" customEmojiHtml('9', {json.dumps(hostile)})]",
        )
        drawn, text, alone = result
        self.assertNotIn("<img src=x", drawn)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;", drawn)
        # What selection and copy read: the character, nothing of the picture.
        self.assertEqual(text, hostile)
        self.assertEqual(alone, "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;")


@unittest.skipUnless(NODE, "node is required to run the custom emoji helpers")
class TestTheBatcher(unittest.TestCase):
    def test_each_id_is_asked_once_and_a_hundred_per_request(self) -> None:
        program = (
            "const sent = []\n"
            "let pending = null\n"
            "const batcher = createCustomEmojiBatcher({ request: (ids) => sent.push(ids), schedule: (fn) => { pending = fn } })\n"
            "for (let i = 0; i < 250; i++) batcher.note(String(5000000000000000000n + BigInt(i)))\n"
            "batcher.note('5000000000000000001'); batcher.note('abc'); batcher.note('12345678901234567890'); batcher.note(7)\n"
            "const before = sent.length\n"
            "pending()\n"
            "batcher.note('5000000000000000001')\n"
            "batcher.forget('5000000000000000002'); batcher.note('5000000000000000002'); pending()\n"
            "console.log(JSON.stringify({ before, sizes: sent.map(ids => ids.length), first: sent[0][1], last: sent[3] }))"
        )
        result = _run_setup_program(
            HTML,
            ("const CUSTOM_EMOJI_BATCH = 100", "const CUSTOM_EMOJI_ID_RE = /^", "const createCustomEmojiBatcher = ("),
            "",
            program,
        )
        # Nothing is sent before the render ends; then 250 ids in three requests,
        # every digit kept. A forgotten id is asked again.
        self.assertEqual(result["before"], 0)
        self.assertEqual(result["sizes"], [100, 100, 50, 1])
        self.assertEqual(result["first"], "5000000000000000001")
        self.assertEqual(result["last"], ["5000000000000000002"])


@unittest.skipUnless(NODE, "node is required to run the custom emoji helpers")
class TestReactionChip(unittest.TestCase):
    def test_the_chip_draws_a_custom_emoji_and_escapes_any_other(self) -> None:
        prelude = (
            "const ref = (value) => ({ value })\n"
            f"const customEmojiInfo = ref({json.dumps({SUN: {'kind': 'image', 'alt': '☀️', 'text_color': False, 'ready': True}})})\n"
            "const stickerSupport = { tgs: true, webm: true }\n"
            "const noted = []\n"
            "const noteCustomEmoji = (id) => noted.push(id)\n"
        )
        program = (
            f"const chips = [reactionEmojiHtml('custom_{SUN}'), reactionEmojiHtml('custom_5000000000000000009'), reactionEmojiHtml('<b>'), reactionEmojiHtml(null)]\n"
            f"const labels = [formatReactionEmoji('custom_{SUN}'), formatReactionEmoji('custom_5000000000000000009'), formatReactionEmoji('🔥')]\n"
            "console.log(JSON.stringify({ chips, labels, noted }))"
        )
        result = _run_setup_program(
            HTML,
            (
                *_BUILDER,
                "const formatReactionEmoji = (emoji) =>",
                "const reactionEmojiHtml = (emoji, options = {}) =>",
            ),
            prelude,
            program,
        )
        self.assertIn('<span class="custom-emoji-text">☀️</span><img class="custom-emoji-picture"', result["chips"][0])
        self.assertEqual(result["chips"][1:], ["🎭", "&lt;b&gt;", "👍"])
        # The text labels (screen readers, What changed) read the emoji's meaning.
        self.assertEqual(result["labels"], ["☀️", "🎭", "🔥"])
        self.assertEqual(result["noted"], [SUN, "5000000000000000009"])

    def test_the_templates_draw_the_chips_through_the_builder(self) -> None:
        self.assertIn('<span class="reaction-emoji" v-html="reactionEmojiHtml(reaction.emoji)"></span>', HTML)
        # Taken back, in the bubble and in What changed: the first frame only.
        self.assertEqual(HTML.count("reactionEmojiHtml(removed.emoji, { still: true })"), 1)
        self.assertEqual(HTML.count("reactionEmojiHtml(card.emoji, { still: true })"), 1)
        self.assertNotIn("{{ formatReactionEmoji(", HTML)


@unittest.skipUnless(NODE, "node is required to run the custom emoji helpers")
class TestTheEmojiBudget(unittest.TestCase):
    def test_twenty_emoji_play_three_and_leave_the_stickers_their_four(self) -> None:
        prelude = (
            "const el = (kind, n) => ({ n, kind, classList: { contains: (c) => (kind === 'emoji' ? c === 'tgs-emoji' : kind === 'video' ? c === 'emoji-video' : c === 'tgs-sticker') } })\n"
            "const playing = new Set()\n"
            "const tgsState = {}\n"
        )
        program = (
            "const play = (item) => playing.add(item)\n"
            "const hold = (item) => playing.delete(item)\n"
            "tgsState.budget = createStickerBudget({ max: STICKER_MAX_PLAYING, reducedMotion: () => false, play, hold })\n"
            "tgsState.emojiBudget = createStickerBudget({ max: EMOJI_MAX_PLAYING, reducedMotion: () => false, play, hold })\n"
            "const stickers = [0, 1, 2, 3].map(n => el('sticker', n))\n"
            "const emoji = [...Array(20).keys()].map(n => el(n % 2 ? 'video' : 'emoji', n))\n"
            "stickers.forEach(s => budgetFor(s).enter(s))\n"
            "emoji.forEach(e => budgetFor(e).enter(e))\n"
            "const kinds = [...playing].map(p => p.kind)\n"
            "emoji.slice(17).forEach(e => budgetFor(e).leave(e))\n"
            "console.log(JSON.stringify({\n"
            "  stickers: kinds.filter(k => k === 'sticker').length,\n"
            "  emoji: kinds.filter(k => k !== 'sticker').length,\n"
            "  newest: tgsState.emojiBudget.playing().map(p => p.n),\n"
            "  after: tgsState.emojiBudget.playing().map(p => p.n).sort((a, b) => a - b),\n"
            "  stickersAfter: tgsState.budget.playing().length,\n"
            "}))"
        )
        result = _run_setup_program(
            HTML,
            (
                "const STICKER_MAX_PLAYING = 4",
                "const EMOJI_MAX_PLAYING = 3",
                "const createStickerBudget = (",
                "const isMovingEmoji = (el) =>",
                "const budgetFor = (el) =>",
            ),
            prelude,
            program,
        )
        self.assertEqual((result["stickers"], result["emoji"]), (4, 3))
        # The three that left gave their slots to the newest still on screen.
        self.assertEqual(result["after"], [14, 15, 16])
        self.assertEqual(result["stickersAfter"], 4)

    def test_the_sticker_sync_observes_moving_emoji_and_skips_still_ones_in_the_budget(self) -> None:
        sync = HTML[HTML.index("const syncStickers = () =>") :]
        sync = sync[: sync.index("\n                const ", 10)]
        self.assertIn("document.querySelectorAll('.tgs-sticker, .emoji-video')", sync)
        self.assertIn("if (!el.dataset.still) tgsState.view.observe(el)", sync)
        self.assertIn("max: EMOJI_MAX_PLAYING", sync)
        self.assertRegex(
            HTML,
            re.compile(
                r"watch\(\[sortedMessages, mediaRevision, showMediaGallery, customEmojiInfo, openRemovedReactions, showChangesFeed, changeCards\]"
            ),
        )


@unittest.skipUnless(NODE, "node is required to run the custom emoji helpers")
class TestCustomEmojiInText(unittest.TestCase):
    """renderEntityHtml draws a custom emoji entity, with the bubble's own renderer."""

    def _render(self, body: str) -> object:
        from test_entity_rendering_frontend import _renderer_bundle

        info = {
            "5000000000000000002": {"kind": "tgs", "alt": "🌞", "text_color": False, "ready": True},
            "5000000000000000003": {"kind": None, "alt": "🌙", "text_color": False, "ready": False},
        }
        prelude = (
            "const ref = (value) => ({ value })\n"
            f"const customEmojiInfo = ref({json.dumps(info)})\n"
            "const stickerSupport = { tgs: true, webm: true }\n"
            "const noted = []\n"
            "const noteCustomEmoji = (id) => noted.push(id)\n"
        )
        return _run_setup_program(HTML, _BUILDER, prelude + _renderer_bundle(HTML), _STRIP + body)

    def test_a_known_id_is_drawn_and_an_unknown_keeps_its_character(self) -> None:
        result = self._render(
            "const R = (text, ents) => renderMessageHtml({ text, raw_data: { entities: ents } })\n"
            "const drawn = R('Look 🌞 up', [{ type: 'bold', offset: 0, length: 4 }, { type: 'custom_emoji', offset: 5, length: 2, document_id: '5000000000000000002' }])\n"
            "const pending = R('Look 🌙 up', [{ type: 'custom_emoji', offset: 5, length: 2, document_id: '5000000000000000003' }])\n"
            "const unknown = R('Look ⭐ up', [{ type: 'custom_emoji', offset: 5, length: 1, document_id: '5000000000000000009' }])\n"
            # An integer id from an older API, already rounded by JSON.parse, never addresses a file.
            "const rounded = R('Look 🌞 up', [{ type: 'custom_emoji', offset: 5, length: 2, document_id: 5000000000000000002 }])\n"
            "console.log(JSON.stringify({ drawn, pending, unknown, rounded, text: strip(drawn), noted }))"
        )
        self.assertTrue(
            result["drawn"].startswith(
                '<strong>Look</strong> <span class="custom-emoji" data-emoji-id="5000000000000000002">'
            )
        )
        self.assertIn(
            '<span class="custom-emoji-text">🌞</span><span class="custom-emoji-picture tgs-sticker tgs-emoji"',
            result["drawn"],
        )
        self.assertTrue(result["drawn"].endswith("</span> up"))
        self.assertEqual(result["pending"], "Look 🌙 up")
        self.assertEqual(result["unknown"], "Look ⭐ up")
        self.assertEqual(result["rounded"], "Look 🌞 up")
        self.assertEqual(result["text"], "Look 🌞 up")
        self.assertEqual(result["noted"][:3], ["5000000000000000002", "5000000000000000003", "5000000000000000009"])
