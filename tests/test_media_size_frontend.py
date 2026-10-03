"""The viewer sizes a photo, a video and a GIF the way Telegram Desktop does.

Before this, a single photo had no width of its own: its only width rule was a
percentage, which adds nothing to a bubble that shrinks to fit, so the bubble
took the width of the sender's name and a 720x1280 photo was drawn about 140px
wide. The box now comes from the stored size, before the file loads.

The helpers are EXECUTED under node, lifted verbatim from the template.
Template checks pin the parts Vue renders. Demo data only.
"""

import json
import re
import unittest

from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

HTML = INDEX_HTML.read_text(encoding="utf-8")

_BOX = (
    "const MEDIA_MAX_SIZE = ",
    "const GIF_MAX_SIZE = ",
    "const MEDIA_MIN_SIZE = ",
    "const MEDIA_BUBBLE_MIN_WIDTH = ",
    "const mediaBox = (width, height,",
    "const mediaCapFor = (viewport) =>",
)


def _boxes(calls: dict[str, str]):
    program = "const out = {" + ",".join(f"{json.dumps(k)}: {v}" for k, v in calls.items()) + "};\n"
    program += "console.log(JSON.stringify(out))"
    return _run_setup_program(HTML, _BOX, "", program)


def _wh(box: dict) -> tuple:
    return (box["width"], box["height"], box["fit"], box["known"])


@unittest.skipUnless(NODE, "node is required to run the media size helpers")
class TestMediaBox(unittest.TestCase):
    def test_a_picture_is_scaled_down_to_430_and_never_up(self) -> None:
        out = _boxes(
            {
                "portrait": "mediaBox(720, 1280)",
                "landscape": "mediaBox(1280, 853)",
                "square100": "mediaBox(100, 100)",
                "small": "mediaBox(300, 200)",
                "video": "mediaBox(640, 360)",
            }
        )
        self.assertEqual(_wh(out["portrait"]), (242, 430, "cover", True))
        self.assertEqual(_wh(out["landscape"]), (430, 287, "cover", True))
        self.assertEqual(_wh(out["square100"]), (100, 100, "cover", True))
        # Smaller than the cap: drawn at its own size, not stretched.
        self.assertEqual(_wh(out["small"]), (300, 200, "cover", True))
        self.assertEqual(_wh(out["video"]), (430, 242, "cover", True))

    def test_a_tiny_picture_gets_the_100px_minimum(self) -> None:
        out = _boxes({"tiny": "mediaBox(50, 50)", "thin": "mediaBox(30, 600)"})
        self.assertEqual(_wh(out["tiny"]), (100, 100, "cover", True))
        # 30x600 scales to 22x430: the box is 100 wide, and covering it would
        # cut far more than a quarter, so the picture is drawn whole.
        self.assertEqual(_wh(out["thin"]), (100, 430, "contain", True))

    def test_a_panorama_is_drawn_whole_in_a_430_by_100_box(self) -> None:
        out = _boxes({"panorama": "mediaBox(4000, 200)"})
        self.assertEqual(_wh(out["panorama"]), (430, 100, "contain", True))

    def test_a_picture_in_a_bubble_is_at_least_200_wide(self) -> None:
        out = _boxes(
            {
                "square": "mediaBox(100, 100, { framed: true })",
                "portrait": "mediaBox(720, 1280, { framed: true })",
                "alone": "mediaBox(100, 100, { framed: false })",
            }
        )
        self.assertEqual(_wh(out["square"]), (200, 100, "contain", True))
        self.assertEqual(_wh(out["portrait"]), (242, 430, "cover", True))
        self.assertEqual(_wh(out["alone"]), (100, 100, "cover", True))

    def test_the_3_4_rule_cuts_a_little_and_draws_the_rest_whole(self) -> None:
        # In a bubble a picture narrower than 200px is widened to 200. At 160
        # wide, covering scales it by 1.25 and keeps 80% of its height: cut.
        # At 150 the scale is 4/3 and exactly three quarters stays: still cut.
        # At 140 the scale is 1.43 and more than a quarter would go: whole.
        out = _boxes(
            {
                "cut": "mediaBox(160, 180, { framed: true })",
                "edge": "mediaBox(150, 180, { framed: true })",
                "whole": "mediaBox(140, 180, { framed: true })",
            }
        )
        self.assertEqual(_wh(out["cut"]), (200, 180, "cover", True))
        self.assertEqual(_wh(out["edge"]), (200, 180, "cover", True))
        self.assertEqual(_wh(out["whole"]), (200, 180, "contain", True))

    def test_no_size_is_a_4_by_3_box_at_the_cap(self) -> None:
        out = _boxes(
            {
                "none": "mediaBox(undefined, undefined)",
                "nulls": "mediaBox(null, null)",
                "zero": "mediaBox(0, 0)",
                "text": "mediaBox('abc', 'abc')",
                "negative": "mediaBox(-5, 10)",
                "phone": "mediaBox(null, null, { cap: 292 })",
            }
        )
        for key in ("none", "nulls", "zero", "text", "negative"):
            with self.subTest(key=key):
                self.assertEqual(_wh(out[key]), (430, 323, "cover", False))
        self.assertEqual(_wh(out["phone"]), (292, 219, "cover", False))

    def test_the_phone_cap_and_the_gif_cap(self) -> None:
        out = _boxes(
            {
                "phonePortrait": "mediaBox(720, 1280, { cap: 292 })",
                "gif": "mediaBox(480, 270, { cap: 320 })",
                "gifSmall": "mediaBox(200, 150, { cap: 320 })",
            }
        )
        self.assertEqual(_wh(out["phonePortrait"]), (164, 292, "cover", True))
        self.assertEqual(_wh(out["gif"]), (320, 180, "cover", True))
        self.assertEqual(_wh(out["gifSmall"]), (200, 150, "cover", True))

    def test_the_cap_follows_the_viewport(self) -> None:
        out = _boxes(
            {
                "desktop": "mediaCapFor(1280)",
                "edge": "mediaCapFor(768)",
                "tablet": "mediaCapFor(767)",
                "phone": "mediaCapFor(390)",
                "small": "mediaCapFor(320)",
            }
        )
        self.assertEqual(out, {"desktop": 430, "edge": 430, "tablet": 430, "phone": 292, "small": 240})


_FRAME_PRELUDE = """
const computed = (fn) => ({ get value() { return fn() } })
const ref = (value) => ({ value })
const window = { innerWidth: 1280, addEventListener() {} }
const requestAnimationFrame = (fn) => fn()
let mediaOnly = false
const isMediaOnlyMessage = () => mediaOnly
const isImageDocument = (msg) => (msg.media?.mime_type || '').startsWith('image/')
const getMediaUrl = (msg) => msg.media?.url || ''
"""

_CAP = (
    "const viewportWidth = ref(",
    "const bubbleRoom = ref(",
    "const BUBBLE_PADDING_X = ",
    "const mediaCap = computed(",
    "const mediaNaturalSizes = ref(",
)

_FRAME = (
    *_BOX,
    *_CAP,
    "const mediaFrame = (msg, index) =>",
    "const mediaFrameStyle = (msg, index) =>",
    "const MEDIA_COMPACT_HEIGHT = ",
    "const mediaFrameCompact = (msg, index) =>",
    "const bubbleMediaStyle = (msg, index) =>",
    "const noteNaturalSize = (msg, width, height) =>",
)


def _frame(epilogue: str):
    return _run_setup_program(HTML, _FRAME, _FRAME_PRELUDE, epilogue)


@unittest.skipUnless(NODE, "node is required to run the media size helpers")
class TestMediaFrame(unittest.TestCase):
    def test_the_style_carries_the_width_the_shape_and_the_fit(self) -> None:
        out = _frame(
            "console.log(JSON.stringify(mediaFrameStyle({ media: { type: 'photo', width: 720, height: 1280, file_path: 'a.jpg' } }, 0)))"
        )
        self.assertEqual(out, {"width": "242px", "aspectRatio": "242 / 430", "--media-fit": "cover"})

    def test_a_picture_with_a_name_or_caption_is_framed_and_one_alone_is_not(self) -> None:
        out = _frame(
            """
const msg = { media: { type: 'photo', width: 100, height: 100, file_path: 'a.jpg' } }
mediaOnly = true
const alone = mediaFrame(msg, 0)
mediaOnly = false
const framed = mediaFrame(msg, 0)
console.log(JSON.stringify({ alone: alone.width, framed: framed.width }))
"""
        )
        self.assertEqual(out, {"alone": 100, "framed": 200})

    def test_an_image_sent_as_a_file_is_always_framed(self) -> None:
        out = _frame(
            """
mediaOnly = true
const box = mediaFrame({ media: { type: 'document', mime_type: 'image/png', width: 120, height: 90, file_path: 'a.png' } }, 0)
console.log(JSON.stringify(box.width))
"""
        )
        self.assertEqual(out, 200)

    def test_a_gif_takes_the_320_cap_and_a_video_the_430_cap(self) -> None:
        out = _frame(
            """
const gif = mediaFrame({ media: { type: 'animation', width: 480, height: 270, file_path: 'a.mp4' } }, 0)
const video = mediaFrame({ media: { type: 'video', width: 640, height: 360, file_path: 'b.mp4' } }, 0)
console.log(JSON.stringify({ gif: [gif.width, gif.height], video: [video.width, video.height] }))
"""
        )
        self.assertEqual(out, {"gif": [320, 180], "video": [430, 242]})

    def test_a_phone_viewport_lowers_the_cap(self) -> None:
        out = _frame(
            """
mediaOnly = true
viewportWidth.value = 390
const box = mediaFrame({ media: { type: 'photo', width: 720, height: 1280, file_path: 'a.jpg' } }, 0)
viewportWidth.value = 1280
console.log(JSON.stringify([box.width, box.height]))
"""
        )
        self.assertEqual(out, [164, 292])

    def test_a_stand_in_too_short_for_the_ring_above_its_words_is_compact(self) -> None:
        out = _frame(
            """
const short = mediaFrameCompact({ media: { type: 'photo', width: 1280, height: 300 } }, 0)
const tall = mediaFrameCompact({ media: { type: 'photo', width: 1280, height: 853 } }, 0)
console.log(JSON.stringify({ short, tall }))
"""
        )
        self.assertEqual(out, {"short": True, "tall": False})

    def test_a_row_with_no_size_learns_it_from_the_file_once(self) -> None:
        out = _frame(
            """
const msg = { id: 7, media: { type: 'photo', width: null, height: null, file_path: 'a.jpg', url: '/media/c1/7_photo' } }
const before = mediaFrame(msg, 0)
noteNaturalSize(msg, 900, 1200)
const after = mediaFrame(msg, 0)
const stored = { id: 8, media: { type: 'photo', width: 1280, height: 853, file_path: 'b.jpg', url: '/media/c1/8_photo' } }
noteNaturalSize(stored, 10, 10)
noteNaturalSize(msg, 0, 0)
console.log(JSON.stringify({
    before: [before.width, before.height, before.known],
    after: [after.width, after.height, after.known],
    storedKept: !mediaNaturalSizes.value.has('/media/c1/8_photo') && mediaFrame(stored, 0).width === 430,
    natural: mediaNaturalSizes.value.get('/media/c1/7_photo'),
    onRow: 'mediaNaturalSize' in msg,
}))
"""
        )
        self.assertEqual(out["before"], [430, 323, False])
        self.assertEqual(out["after"], [323, 430, True])
        self.assertTrue(out["storedKept"])
        self.assertEqual(out["natural"], {"w": 900, "h": 1200})
        self.assertFalse(out["onRow"])

    def test_a_jump_that_replaces_the_row_keeps_the_learned_size(self) -> None:
        # A jump replaces messages.value with fresh objects from the API. Vue
        # keeps the <img> of a row in both windows (same key, same src), so it
        # loads nothing and reports nothing again: the new object must still
        # find the size the old one learned.
        out = _frame(
            """
const media = () => ({ type: 'photo', width: null, height: null, file_path: 'a.jpg', url: '/media/c1/7_photo' })
const old = { id: 7, media: media() }
noteNaturalSize(old, 720, 1280)
const fresh = { id: 7, media: media() }
const other = { id: 9, media: { ...media(), url: '/media/c1/9_photo' } }
const a = mediaFrame(old, 0)
const b = mediaFrame(fresh, 0)
const c = mediaFrame(other, 0)
console.log(JSON.stringify({ old: [a.width, a.height], fresh: [b.width, b.height], other: [c.width, c.height] }))
"""
        )
        self.assertEqual(out["old"], [242, 430])
        self.assertEqual(out["fresh"], out["old"])
        # Another file is not given that size.
        self.assertEqual(out["other"], [430, 323])

    def test_a_narrow_column_lowers_the_cap_to_the_bubble_room(self) -> None:
        # At an 800px window the message column lets a bubble be 298px wide.
        # The box is computed at that width, so its 100px minimum holds and
        # the compact stand-in is decided on the height it is drawn at. Set
        # from the viewport alone, the 430x100 panorama box was shrunk by
        # max-width to 298x69.
        out = _frame(
            """
mediaOnly = true
bubbleRoom.value = 298
const panorama = mediaFrame({ media: { type: 'photo', width: 4000, height: 200 } }, 0)
const portrait = mediaFrame({ media: { type: 'photo', width: 720, height: 1280 } }, 0)
const doc = mediaFrame({ media: { type: 'document', mime_type: 'image/png', width: 800, height: 600 } }, 0)
const compact = mediaFrameCompact({ media: { type: 'photo', width: 1280, height: 853 } }, 0)
bubbleRoom.value = 600
const wide = mediaFrame({ media: { type: 'photo', width: 720, height: 1280 } }, 0)
bubbleRoom.value = 0
const unmeasured = mediaFrame({ media: { type: 'photo', width: 4000, height: 200 } }, 0)
console.log(JSON.stringify({
    panorama: [panorama.width, panorama.height, panorama.fit],
    portrait: [portrait.width, portrait.height],
    doc: [doc.width, doc.height],
    compact,
    wide: [wide.width, wide.height],
    unmeasured: [unmeasured.width, unmeasured.height],
}))
"""
        )
        self.assertEqual(out["panorama"], [298, 100, "contain"])
        self.assertEqual(out["portrait"], [168, 298])
        # An image sent as a file keeps the bubble's padding: 298 - 2 * 11.
        self.assertEqual(out["doc"], [276, 207])
        # 1280x853 at 298 wide is 199 tall: tall enough for the ring above.
        self.assertFalse(out["compact"])
        # More room than the cap: the cap still holds.
        self.assertEqual(out["wide"], [242, 430])
        self.assertEqual(out["unmeasured"], [430, 100])

    def test_the_bubble_carries_the_picture_width_and_an_album_does_not(self) -> None:
        out = _frame(
            """
console.log(JSON.stringify({
    photo: bubbleMediaStyle({ media: { type: 'photo', width: 720, height: 1280, file_path: 'a.jpg' } }, 0),
    gif: bubbleMediaStyle({ media: { type: 'animation', width: 480, height: 270, file_path: 'a.mp4' } }, 0),
    doc: bubbleMediaStyle({ media: { type: 'document', mime_type: 'image/png', width: 800, height: 600, file_path: 'a.png' } }, 0),
    album: bubbleMediaStyle({ media: { type: 'photo', width: 720, height: 1280, file_path: 'a.jpg' }, raw_data: { grouped_id: 5 } }, 0),
    file: bubbleMediaStyle({ media: { type: 'document', mime_type: 'application/pdf', file_path: 'a.pdf' } }, 0),
    text: bubbleMediaStyle({ text: 'hello' }, 0),
}))
"""
        )
        self.assertEqual(out["photo"], {"--media-w": "242px"})
        self.assertEqual(out["gif"], {"--media-w": "320px"})
        self.assertEqual(out["doc"], {"--media-w": "430px"})
        self.assertIsNone(out["album"])
        self.assertIsNone(out["file"])
        self.assertIsNone(out["text"])


_PLACEHOLDER_PRELUDE = """
const computed = (fn) => ({ get value() { return fn() } })
const ref = (value) => ({ value })
const window = { innerWidth: 1280, addEventListener() {} }
const requestAnimationFrame = (fn) => fn()
const isImageDocument = (msg) => (msg.media?.mime_type || '').startsWith('image/')
const getMediaUrl = (msg) => msg.media?.url || ''
const isAlbumPicture = (m) => m.media?.type === 'photo' || m.media?.type === 'video'
const canPlaySticker = () => true
const hasTranscriptButton = () => false
const isTranscriptExpanded = () => false
const transcriptStatus = () => null
const currentPreview = () => null
const hasReactionRow = () => false
const isEditedMessage = () => false
const hasForwardHeader = () => false
const isGroup = ref(false)
const isOwnMessage = () => false
const showNameAt = () => false
const showSenderName = () => false
const bubbleAccountLabel = () => ''
const getAlbumCaptionMessage = () => null
const getExtendedMediaChip = () => null
const isLockedAudio = () => false
const formatBytes = (n) => `${n} B`
const getMediaDisplayName = (m) => m.file_name
const sessionWord = { value: 'login' }
"""

_PLACEHOLDER = (
    "const METADATA_ONLY_TYPES = new Set([",
    "const MEDIA_TYPE_WORDS = {",
    "const VISUAL_MEDIA_TYPES = ",
    "const FRAMELESS_MEDIA_TYPES = ",
    "const rendersAsPicture = (msg) =>",
    "const isMediaOnlyMessage = (msg, index) =>",
    "const mediaUnavailable = (msg) =>",
    "const STICKER_REASONS = {",
    "const MISSING_REASONS = {",
    "const mediaMissingReason = (msg) =>",
    *_BOX,
    *_CAP,
    "const mediaFrame = (msg, index) =>",
    "const mediaFrameStyle = (msg, index) =>",
    "const MEDIA_COMPACT_HEIGHT = ",
    "const mediaFrameCompact = (msg, index) =>",
    "const mediaPlaceholder = (msg) =>",
)


@unittest.skipUnless(NODE, "node is required to run the media size helpers")
class TestStandInFrame(unittest.TestCase):
    """The real isMediaOnlyMessage and mediaPlaceholder decide the stand-in's box."""

    def _run(self, epilogue: str):
        return _run_setup_program(HTML, _PLACEHOLDER, _PLACEHOLDER_PRELUDE, epilogue)

    def test_a_hidden_photo_gives_its_stand_in_the_shown_photos_box(self) -> None:
        out = self._run(
            """
const photo = (extra) => ({ id: 1, media: { type: 'photo', file_size: 2048, ...extra } })
const big = { width: 1600, height: 1200 }
const small = { width: 120, height: 90 }
const shownBig = photo({ ...big, file_path: '1/a.jpg', url: '/media/c1/1_photo' })
const hiddenBig = photo({ ...big, no_download: true })
const missingBig = photo({ ...big, file_path: '1/a.jpg', url: '/media/c1/1_photo', downloaded: false })
const shownSmall = photo({ ...small, file_path: '1/a.jpg', url: '/media/c1/1_photo' })
const hiddenSmall = photo({ ...small, no_download: true })
const captioned = { ...photo({ ...small, no_download: true }), text: 'A caption' }
console.log(JSON.stringify({
    placeholders: [shownBig, hiddenBig, missingBig].map((m) => mediaPlaceholder(m)?.shape || null),
    shownBig: mediaFrameStyle(shownBig, 0),
    hiddenBig: mediaFrameStyle(hiddenBig, 0),
    missingBig: mediaFrameStyle(missingBig, 0),
    shownSmall: mediaFrameStyle(shownSmall, 0),
    hiddenSmall: mediaFrameStyle(hiddenSmall, 0),
    hiddenAlone: isMediaOnlyMessage(hiddenSmall, 0),
    captioned: mediaFrameStyle(captioned, 0),
}))
"""
        )
        self.assertEqual(out["placeholders"], [None, "picture", "picture"])
        self.assertEqual(out["shownBig"], {"width": "430px", "aspectRatio": "430 / 323", "--media-fit": "cover"})
        self.assertEqual(out["hiddenBig"], out["shownBig"])
        self.assertEqual(out["missingBig"], out["shownBig"])
        # A small picture shows that the real isMediaOnlyMessage runs: alone,
        # the hidden photo's stand-in is the shown photo's 120px box ...
        self.assertTrue(out["hiddenAlone"])
        self.assertEqual(out["hiddenSmall"], out["shownSmall"])
        self.assertEqual(out["shownSmall"]["width"], "120px")
        # ... and with a caption it sits in a bubble, at least 200px wide.
        self.assertEqual(out["captioned"]["width"], "200px")

    def test_the_compact_stand_in_says_the_reason_without_the_size(self) -> None:
        out = self._run(
            """
const ph = mediaPlaceholder({ id: 1, media: { type: 'photo', width: 4000, height: 200, file_size: 1024, file_path: '1/a.jpg', downloaded: false } })
const plain = mediaPlaceholder({ id: 2, media: { type: 'photo', width: 4000, height: 200, no_download: true } })
console.log(JSON.stringify({ line: ph.line, why: ph.why, plainLine: plain.line, plainWhy: plain.why }))
"""
        )
        self.assertEqual(out["line"], "1024 B · not downloaded yet")
        self.assertEqual(out["why"], "Not downloaded yet")
        self.assertEqual(out["plainLine"], "Hidden for this login")
        self.assertEqual(out["plainWhy"], "Hidden for this login")


class TestMediaFrameTemplate(unittest.TestCase):
    def _tag(self, marker: str) -> str:
        start = HTML.rindex("<", 0, HTML.index(marker))
        return HTML[start : HTML.index(">", HTML.index(marker)) + 1]

    def test_every_picture_box_binds_the_frame_style(self) -> None:
        markers = {
            "photo": "<button v-else-if=\"msg.media?.type === 'photo' && !msg.raw_data?.grouped_id\"",
            "gif": "<button v-else-if=\"msg.media?.type === 'animation'\"",
            "video": '<div class="relative group media-frame"',
            "document": 'aria-label="Open image"',
            "placeholder": '<div v-if="ph.shape !== \'file\'" class="media-placeholder"',
        }
        for name, marker in markers.items():
            with self.subTest(name=name):
                self.assertEqual(HTML.count(marker), 1, marker)
                tag = self._tag(marker)
                self.assertIn("mediaFrameStyle(msg, index)", tag)
                self.assertIn("media-frame", tag)

    def test_the_old_fixed_heights_are_gone_from_photo_video_gif_and_document(self) -> None:
        for name, marker in {
            "video": 'class="pointer-events-none"',
            "gif": 'class="gif-video" loop muted playsinline',
        }.items():
            with self.subTest(name=name):
                self.assertNotRegex(self._tag(marker), r"max-h-|rounded-lg|w-full")
        self.assertNotIn("max-h-64", HTML)
        # Only the link preview's picture keeps max-h-48: it is not a media box.
        self.assertEqual(HTML.count("max-h-48"), 1)
        self.assertIn('class="mt-1 rounded max-h-48 max-w-full object-cover"', HTML)
        self.assertNotIn("max-height: 320px;", HTML)

    def test_a_file_without_a_size_reports_its_own(self) -> None:
        self.assertEqual(
            HTML.count('@load="noteNaturalSize(msg, $event.target.naturalWidth, $event.target.naturalHeight)"'), 2
        )
        self.assertEqual(
            HTML.count('@loadedmetadata="noteNaturalSize(msg, $event.target.videoWidth, $event.target.videoHeight)"'),
            2,
        )

    def test_the_bubble_carries_the_picture_width(self) -> None:
        self.assertIn(':style="[getSenderStyle(msg), bubbleMediaStyle(msg, index)]"', HTML)
        self.assertRegex(
            HTML,
            r"\.message-bubble:has\(> \.media-block\.is-edge \.media-frame\) \{\s*max-width: min\(var\(--tg-bubble-max\), var\(--media-w, 100%\)\);",
        )
        self.assertRegex(
            HTML,
            r"\.message-bubble:has\(> \.media-block:not\(\.is-edge\) \.media-frame\) \{\s*max-width: min\(var\(--tg-bubble-max\), calc\(var\(--media-w, 100%\) \+ 2 \* var\(--tg-bubble-pad-x\)\)\);",
        )

    def test_the_file_fills_its_box(self) -> None:
        rule = re.search(
            r"\.message-bubble \.media-frame > img,\s*\.message-bubble \.media-frame > video \{([^}]*)\}", HTML
        )
        self.assertIsNotNone(rule)
        for declaration in (
            "position: absolute;",
            "inset: 0;",
            "width: 100%;",
            "height: 100%;",
            "object-fit: var(--media-fit, cover);",
        ):
            self.assertIn(declaration, rule.group(1))
        frame = re.search(r"\n        \.media-frame \{([^}]*)\}", HTML)
        self.assertIsNotNone(frame)
        self.assertIn("max-width: 100%;", frame.group(1))

    def test_an_album_has_its_own_width(self) -> None:
        rules = re.findall(r"\n        \.album-grid \{([^}]*)\}", HTML)
        self.assertEqual(len(rules), 1)
        self.assertIn("width: 400px;", rules[0])
        self.assertIn("max-width: 100%;", rules[0])

    def test_the_stand_in_no_longer_sizes_itself(self) -> None:
        rule = re.search(r"\n        \.media-placeholder \{([^}]*)\}", HTML)
        self.assertIsNotNone(rule)
        for gone in ("min-width: 100%", "max-height: 260px", "min-height: 132px"):
            self.assertNotIn(gone, rule.group(1))
        self.assertNotIn("Math.min(320, 260 * ratio)", HTML)
        compact = re.search(r"\n        \.media-placeholder\.is-compact \{([^}]*)\}", HTML)
        self.assertIsNotNone(compact)
        # The words go beside the ring, never away: a phone shows no tooltip.
        self.assertIn("flex-direction: row;", compact.group(1))
        self.assertNotRegex(HTML, r"\.is-compact[^{]*\{[^}]*display: none")

    def test_a_gif_that_fails_to_load_shows_the_stand_in(self) -> None:
        tag = self._tag('class="gif-video" loop muted playsinline')
        self.assertIn('@error="handleMediaError($event, msg)"', tag)

    def test_the_bubble_room_probe_sits_in_the_message_list(self) -> None:
        list_start = HTML.index('<div ref="messagesContainer"')
        probe = HTML.index('<div ref="bubbleRoomProbe" class="bubble-room-probe" aria-hidden="true"></div>')
        first_row = HTML.index('<template v-for="(msg, index) in sortedMessages" :key="msg.id">')
        self.assertLess(list_start, probe)
        self.assertLess(probe, first_row)
        rule = re.search(r"\n        \.bubble-room-probe \{([^}]*)\}", HTML)
        self.assertIsNotNone(rule)
        for declaration in ("width: 100%;", "max-width: var(--tg-bubble-max);", "height: 0;"):
            self.assertIn(declaration, rule.group(1))
        self.assertIn("bubbleRoomProbe,", HTML[HTML.index("mediaFrameCompact,\n") :][:400])

    def test_the_compact_stand_in_wraps_the_reason_instead_of_cutting_it(self) -> None:
        self.assertIn(
            "{{ ph.shape === 'picture' && mediaFrameCompact(msg, index) ? ph.why : ph.line }}",
            HTML,
        )
        rule = re.search(r"\n        \.media-placeholder\.is-compact \.media-placeholder-text > span \{([^}]*)\}", HTML)
        self.assertIsNotNone(rule)
        self.assertNotIn("nowrap", rule.group(1))
        self.assertIn("-webkit-line-clamp: 2;", rule.group(1))

    def test_the_frame_is_never_computed_inside_the_placeholder(self) -> None:
        # isMediaOnlyMessage calls mediaPlaceholder; mediaFrame calls
        # isMediaOnlyMessage. A call the other way would never end.
        start = HTML.index("const mediaPlaceholder = (msg) => {")
        end = HTML.index("\n                const ", start + 10)
        code = re.sub(r"//[^\n]*", "", HTML[start:end])
        self.assertNotRegex(code, r"mediaFrame|mediaBox|isMediaOnlyMessage")
