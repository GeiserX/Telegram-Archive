"""The viewer plays animated (.tgs) and video (.webm) stickers.

The helpers are EXECUTED under node, lifted verbatim from the template: the
sticker kind, which kinds a browser can play, the box size, the .tgs reader
with its size caps, and the play budget. Template checks pin the parts Vue
renders. Demo data only.
"""

import json
import re
import unittest

from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

HTML = INDEX_HTML.read_text(encoding="utf-8")

_NAMES = ("const getMediaDisplayName = (media) =>", "const getDocumentDisplayName = (msg) =>")


def _sticker(**media) -> str:
    return json.dumps({"media": {"type": "sticker", **media}})


@unittest.skipUnless(NODE, "node is required to run the sticker helpers")
class TestStickerKind(unittest.TestCase):
    def test_the_kind_follows_the_mime_type_and_then_the_name(self) -> None:
        cases = {
            "tgsByMime": _sticker(mime_type="application/x-tgsticker", file_name="5550001_AnimatedSticker.tgs"),
            "tgsNullMime": _sticker(mime_type=None, file_name="5550001_AnimatedSticker.tgs"),
            "tgsPathOnly": _sticker(file_path="-1001/5550001_AnimatedSticker.tgs"),
            "webmByMime": _sticker(mime_type="video/webm", file_name="5550002_sticker.webm"),
            "webmNullMime": _sticker(mime_type=None, file_name="5550002_sticker.webm"),
            "webpByMime": _sticker(mime_type="image/webp", file_name="5550003_sticker.webp"),
            "webpNullMime": _sticker(file_name="5550003_sticker.webp"),
            "notSticker": json.dumps({"media": {"type": "video", "file_name": "5550004_sticker.webm"}}),
        }
        program = "const cases = {" + ",".join(f"{k}: {v}" for k, v in cases.items()) + "};\n"
        program += "console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, m]) => [k, stickerKind(m)]))))"
        result = _run_setup_program(HTML, (*_NAMES, "const stickerKind = (msg) =>"), "", program)
        self.assertEqual(
            result,
            {
                "tgsByMime": "tgs",
                "tgsNullMime": "tgs",
                "tgsPathOnly": "tgs",
                "webmByMime": "webm",
                "webmNullMime": "webm",
                "webpByMime": "image",
                "webpNullMime": "image",
                "notSticker": None,
            },
        )

    def test_a_kind_the_browser_cannot_play_keeps_the_label(self) -> None:
        program = (
            "const stickerSupport = { tgs: false, webm: false };\n"
            "const tgs = { media: { type: 'sticker', file_name: '1_AnimatedSticker.tgs' } };\n"
            "const webm = { media: { type: 'sticker', file_name: '1_sticker.webm' } };\n"
            "const webp = { media: { type: 'sticker', file_name: '1_sticker.webp' } };\n"
            "const before = [canPlaySticker(tgs), canPlaySticker(webm), canPlaySticker(webp)];\n"
            "stickerSupport.tgs = true; stickerSupport.webm = true;\n"
            "console.log(JSON.stringify({ before, after: [canPlaySticker(tgs), canPlaySticker(webm), canPlaySticker(webp)] }))"
        )
        result = _run_setup_program(
            HTML, (*_NAMES, "const stickerKind = (msg) =>", "const canPlaySticker = (msg) =>"), "", program
        )
        self.assertEqual(result, {"before": [False, False, True], "after": [True, True, True]})


@unittest.skipUnless(NODE, "node is required to run the sticker helpers")
class TestStickerPlayback(unittest.TestCase):
    CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    SAFARI_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
    CHROME_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0 Mobile/15E148 Safari/604.1"
    FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0"
    CHROME_ANDROID = (
        "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"
    )

    def _playback(self, **env) -> dict:
        base = {"platform": "", "maxTouchPoints": 0, "decompression": True, "webm": True}
        program = f"console.log(JSON.stringify(stickerPlayback({json.dumps({**base, **env})})))"
        return _run_setup_program(HTML, ("const stickerPlayback = (env) =>",), "", program)

    def test_chrome_firefox_and_android_play_both_kinds(self) -> None:
        for ua in (self.CHROME_MAC, self.FIREFOX_LINUX, self.CHROME_ANDROID):
            self.assertEqual(self._playback(userAgent=ua), {"tgs": True, "webm": True}, ua)

    def test_safari_and_ios_keep_video_stickers_as_a_label(self) -> None:
        """No VP9 alpha there: a video sticker would sit on a black box."""
        self.assertEqual(self._playback(userAgent=self.SAFARI_MAC), {"tgs": True, "webm": False})
        self.assertEqual(self._playback(userAgent=self.CHROME_IPHONE), {"tgs": True, "webm": False})
        ipad = self._playback(
            userAgent=self.SAFARI_MAC.replace("Version/26.0 ", ""), platform="MacIntel", maxTouchPoints=5
        )
        self.assertEqual(ipad, {"tgs": True, "webm": False})

    def test_no_decompression_stream_means_no_tgs(self) -> None:
        self.assertEqual(self._playback(userAgent=self.CHROME_MAC, decompression=False), {"tgs": False, "webm": True})

    def test_no_webm_decoder_means_no_video_stickers(self) -> None:
        self.assertEqual(self._playback(userAgent=self.FIREFOX_LINUX, webm=False), {"tgs": True, "webm": False})


@unittest.skipUnless(NODE, "node is required to run the sticker helpers")
class TestStickerBox(unittest.TestCase):
    def test_the_box_fits_the_file_into_200_by_192(self) -> None:
        program = (
            "console.log(JSON.stringify([\n"
            "  stickerBoxStyle({ media: { type: 'sticker' } }),\n"
            "  stickerBoxStyle({ media: { type: 'sticker', width: 512, height: 512 } }),\n"
            "  stickerBoxStyle({ media: { type: 'sticker', width: 512, height: 256 } }),\n"
            "  stickerBoxStyle({ media: { type: 'sticker', width: 300, height: 512 } }),\n"
            "]))"
        )
        result = _run_setup_program(
            HTML,
            ("const STICKER_BOX_WIDTH = ", "const STICKER_BOX_HEIGHT = ", "const stickerBoxStyle = (msg) =>"),
            "",
            program,
        )
        self.assertEqual(
            result,
            [
                {"width": "192px", "height": "192px"},
                {"width": "192px", "height": "192px"},
                {"width": "200px", "height": "100px"},
                {"width": "113px", "height": "192px"},
            ],
        )


_TGS_DECLARATIONS = ("const TGS_MAX_PACKED = ", "const TGS_MAX_UNPACKED = ", "const readTgsText = async (response) =>")


@unittest.skipUnless(NODE, "node is required to run the sticker helpers")
class TestTgsReader(unittest.TestCase):
    def _read(self, make_bytes: str) -> dict:
        program = (
            "const zlib = require('zlib'); const crypto = require('crypto');\n"
            "(async () => {\n"
            f"  const bytes = {make_bytes};\n"
            "  try {\n"
            "    const text = await readTgsText(new Response(bytes));\n"
            "    console.log(JSON.stringify({ ok: true, value: JSON.parse(text) }));\n"
            "  } catch (error) {\n"
            "    console.log(JSON.stringify({ ok: false, error: String(error.message || error) }));\n"
            "  }\n"
            "})()"
        )
        return _run_setup_program(HTML, _TGS_DECLARATIONS, "", program)

    def test_a_gzipped_lottie_file_is_read(self) -> None:
        result = self._read("zlib.gzipSync(Buffer.from(JSON.stringify({ v: '5.5.2', fr: 60, w: 512, h: 512 })))")
        self.assertEqual(result, {"ok": True, "value": {"v": "5.5.2", "fr": 60, "w": 512, "h": 512}})

    def test_a_file_that_unpacks_past_8_mb_is_refused(self) -> None:
        """A gzip bomb: a few kilobytes on disk, nine megabytes unpacked."""
        result = self._read("zlib.gzipSync(Buffer.alloc(9 * 1024 * 1024, 32))")
        self.assertEqual(result, {"ok": False, "error": "sticker animation too large"})

    def test_a_file_past_2_mb_packed_is_refused(self) -> None:
        result = self._read("zlib.gzipSync(crypto.randomBytes(2 * 1024 * 1024 + 4096))")
        self.assertFalse(result["ok"])
        self.assertIn("too large", result["error"])

    def test_bytes_that_are_not_gzip_are_refused(self) -> None:
        result = self._read("Buffer.from(JSON.stringify({ v: '5.5.2' }))")
        self.assertFalse(result["ok"])


_BUDGET_PRELUDE = """
const log = [];
let reduced = false;
const budget = createStickerBudget({
  max: 4,
  reducedMotion: () => reduced,
  play: (item, once) => log.push((once ? 'once ' : 'play ') + item),
  hold: (item) => log.push('hold ' + item),
});
const step = (fn) => { log.length = 0; fn(); return { log: log.slice(), playing: budget.playing() } };
"""


@unittest.skipUnless(NODE, "node is required to run the sticker helpers")
class TestStickerBudget(unittest.TestCase):
    def _run(self, epilogue: str):
        return _run_setup_program(
            HTML,
            ("const createStickerBudget = ({ max, reducedMotion, play, hold }) =>",),
            "",
            _BUDGET_PRELUDE + epilogue,
        )

    def test_at_most_four_play_and_the_newest_take_the_slots(self) -> None:
        result = self._run(
            "const entered = step(() => ['a', 'b', 'c', 'd', 'e', 'f'].forEach(item => budget.enter(item)));\n"
            "const left = step(() => budget.leave('c'));\n"
            "const pressed = step(() => budget.press('a'));\n"
            "console.log(JSON.stringify({ entered, left, pressed }))"
        )
        self.assertEqual(result["entered"]["playing"], ["c", "d", "e", "f"])
        self.assertEqual(result["entered"]["log"].count("hold a"), 1)
        self.assertEqual(result["entered"]["log"].count("hold b"), 1)
        # A playing one leaves: the newest held one still in view takes its slot.
        self.assertEqual(result["left"], {"log": ["hold c", "play b"], "playing": ["d", "e", "f", "b"]})
        # A press on a held one takes the slot of the one playing longest.
        self.assertEqual(result["pressed"], {"log": ["hold d", "play a"], "playing": ["e", "f", "b", "a"]})

    def test_a_hover_plays_a_held_sticker_and_leaves_a_playing_one_alone(self) -> None:
        result = self._run(
            "['a', 'b', 'c', 'd', 'e'].forEach(item => budget.enter(item));\n"
            "const held = step(() => budget.hover('a'));\n"
            "const again = step(() => budget.hover('a'));\n"
            "const away = step(() => budget.hover('zz'));\n"
            "console.log(JSON.stringify({ held, again, away }))"
        )
        self.assertEqual(result["held"], {"log": ["hold b", "play a"], "playing": ["c", "d", "e", "a"]})
        self.assertEqual(result["again"]["log"], [])
        self.assertEqual(result["away"]["log"], [])

    def test_reduced_motion_plays_nothing_and_a_press_plays_once(self) -> None:
        result = self._run(
            "reduced = true;\n"
            "const entered = step(() => ['a', 'b', 'c'].forEach(item => budget.enter(item)));\n"
            "const hovered = step(() => budget.hover('a'));\n"
            "const pressed = step(() => budget.press('b'));\n"
            "const done = step(() => budget.done('b'));\n"
            "const left = step(() => budget.leave('a'));\n"
            "console.log(JSON.stringify({ entered, hovered, pressed, done, left }))"
        )
        self.assertEqual(result["entered"], {"log": [], "playing": []})
        self.assertEqual(result["hovered"], {"log": [], "playing": []})
        self.assertEqual(result["pressed"], {"log": ["once b"], "playing": ["b"]})
        # The slot frees and nothing else starts on its own.
        self.assertEqual(result["done"], {"log": [], "playing": []})
        self.assertEqual(result["left"], {"log": [], "playing": []})


class TestStickerTemplate(unittest.TestCase):
    def _tag(self, marker: str) -> str:
        start = HTML.rindex("<", 0, HTML.index(marker))
        return HTML[start : HTML.index(">", HTML.index(marker)) + 1]

    def test_a_video_sticker_is_a_muted_loop_with_no_controls(self) -> None:
        tag = self._tag('class="gif-video sticker-video sticker-media"')
        self.assertTrue(tag.startswith("<video"), tag)
        for attribute in (" muted", " loop", " playsinline", 'preload="metadata"', ":data-src="):
            self.assertIn(attribute, tag)
        self.assertNotIn("controls", tag)
        self.assertNotIn(":src=", tag)
        self.assertIn('@error="handleMediaError($event, msg)"', tag)

    def test_the_tgs_element_is_empty_and_reports_a_failed_load(self) -> None:
        tag = self._tag('class="tgs-sticker sticker-media"')
        self.assertIn('@stickerfail="handleMediaError($event, msg)"', tag)
        after = HTML[HTML.index(tag) + len(tag) :]
        self.assertTrue(after.startswith("</div>"), "Vue must not render anything inside the .tgs element")

    def test_the_moving_kinds_are_a_named_button_and_a_picture_has_alt_text(self) -> None:
        button = self._tag('class="sticker-box sticker-play"')
        self.assertTrue(button.startswith('<button v-else-if="canPlaySticker(msg)" type="button"'), button)
        self.assertIn('aria-label="Play sticker"', button)
        self.assertIn('@click.stop="pressSticker($event)"', button)
        self.assertIn(".message-bubble .sticker-play:focus-visible {", HTML)
        image = self._tag('alt="Sticker"')
        self.assertTrue(image.startswith("<img"), image)

    def test_the_old_label_only_stays_as_the_fallback(self) -> None:
        branch = HTML[HTML.index("<div v-else-if=\"msg.media?.type === 'sticker'\"") :]
        branch = branch[: branch.index("<!-- Documents that are actually images")]
        self.assertEqual(branch.count("Animated sticker"), 1)
        self.assertLess(branch.index("canPlaySticker(msg)"), branch.index("Animated sticker"))

    def test_reduced_motion_holds_a_video_sticker_in_the_gif_observer(self) -> None:
        start = HTML.index("const setupGifObserver = () => {")
        body = HTML[start : HTML.index("}, { threshold: 0.1 })", start)]
        guard = "if (video.classList.contains('sticker-video') && prefersReducedMotion()) return"
        self.assertIn(guard, body)
        self.assertLess(body.index("video.src = video.dataset.src"), body.index(guard))
        self.assertLess(body.index(guard), body.index("video.play()"))

    def test_the_player_is_wired_to_every_render_and_the_gallery(self) -> None:
        self.assertIn("watch([sortedMessages, mediaRevision, showMediaGallery], () => {\n", HTML)
        self.assertIn("nextTick(syncStickers)", HTML)
        self.assertRegex(HTML, re.compile(r"anim\.setSubframe\(false\)"))
        self.assertIn("anim?.destroy()", HTML)
