"""The edited message: the pencil and its count, the peek, the history panel, and
the "Edited only" list.

The meta row marks an edit with a pencil and the number of earlier versions the
archive kept; its name says when the last edit was ("Edited at 08:57, 2 earlier
versions kept"). Hover, focus or a long press peeks at the text before the last
edit; a click opens the edit history, a panel beside the chat on a wide screen
and a bottom sheet on a phone, as a timeline. The chat menu opens the chat
search in an "Edited only" mode.

The helpers are EXECUTED under node, lifted verbatim from the template with the
real vendored moment, so a broken rule fails here and not only in a browser.
"""

import json
import unittest

from test_deleted_messages_frontend import _MOMENT
from test_entity_rendering_frontend import _renderer_bundle
from test_frontend_bootstrap import (
    _PRODUCER_PRELUDE,
    INDEX_HTML,
    NODE,
    _run_setup_program,
    _setup_slice,
)

HTML = INDEX_HTML.read_text(encoding="utf-8")

# What the marker and timeline helpers close over.
_PRELUDE = (
    _MOMENT
    + """
const viewerTimezone = { value: 'UTC' }
const selectedChat = { value: { id: 7, ref: 'r7' } }
const ref = (value) => ({ value })
const computed = (fn) => ({ get value() { return fn() } })
const versionsMessage = { value: null }
const messageVersionsByMessage = { value: {} }
const messageVersionsLoading = { value: {} }
const messageVersionsErrors = { value: {} }
"""
)

_MARK_DECLARATIONS = (
    "const editedCount = (msg) =>",
    "const shownEditDate = (msg) =>",
    "const isEditedMessage = (msg) =>",
    "const editedMoment = (msg) =>",
    "const editedWhen = (msg) =>",
    "const editedLabel = (msg) =>",
    "const editedHeadText = (msg) =>",
)

_TIMELINE_DECLARATIONS = (
    "const WORD_DIFF_CELLS = 40000",
    "const diffTokens = (text, byChar) =>",
    "const diffWords = (before, after) =>",
    "const versionWhen = (msg, dateStr) =>",
    "const versionGap = (fromStr, toStr) =>",
    "const formattedSlice = (text, entityList, start, end) =>",
    "const versionHtml = (entry, before) =>",
    "const VERSIONS_LIMIT = 100",
    "const versionHistoryShape = (msg, kept) =>",
    "const versionEntries = computed(() =>",
    "const versionMediaKind = (type) =>",
    "const versionMediaItems = (list, keyBase, prefix) =>",
    "const getMediaDisplayName = (media) =>",
    "const messageVersionsKey = (msg) =>",
    "const getMessageVersions = (msg) =>",
    "const isMessageVersionsLoading = (msg) =>",
)

# The demo's edited message: sent 08:52, edited 08:54 and 08:57, two versions kept.
_SENT = "2026-09-30T08:52:00"
_MSG = {
    "id": 5,
    "chat_id": 7,
    "date": _SENT,
    "edit_date": "2026-09-30T08:57:00",
    "version_count": 2,
    "text": "The north lot. It fills up by 8, so get there early.",
}
# Newest first, as the versions endpoint returns them. The listener saw both edits.
_KEPT = [
    {"text": "The north lot. It fills up by 9.", "date": "2026-09-30T08:54:00", "source": "listener"},
    {"text": "The north lot.", "date": _SENT, "source": "listener"},
]


def _run(expression: str, declarations: tuple[str, ...], prelude: str = "") -> object:
    return _run_setup_program(HTML, declarations, _PRELUDE + prelude, f"console.log(JSON.stringify({expression}))")


def _block(start: str, end: str) -> str:
    """The template's code from ``start`` up to (not including) ``end``, verbatim."""
    first = HTML.index(start)
    return HTML[first : HTML.index(end, first)]


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheCountAndItsName(unittest.TestCase):
    """A: the pencil, the number of earlier versions, and a name with the edit time."""

    def _labels(self, rows: list[dict], prelude: str = "") -> dict:
        return _run(
            f"(() => {{ const rows = {json.dumps(rows)}; return {{"
            " counts: rows.map(editedCount), edited: rows.map(isEditedMessage),"
            " labels: rows.map(editedLabel), heads: rows.map(editedHeadText) }})()",
            _MARK_DECLARATIONS,
            prelude,
        )

    def test_the_name_gives_the_edit_time_and_the_versions_kept(self) -> None:
        rows = [
            _MSG,
            {**_MSG, "version_count": 1},
            # Marked edited by Telegram, no earlier text kept: the pencil alone.
            {**_MSG, "version_count": 0},
            # A kept version and no mark (a late-hydrated empty text).
            {**_MSG, "edit_date": None, "version_count": 1},
            {**_MSG, "edit_date": None, "version_count": 0},
        ]
        out = self._labels(rows)
        self.assertEqual(out["counts"], [2, 1, 0, 1, 0])
        self.assertEqual(out["edited"], [True, True, True, True, False])
        self.assertEqual(
            out["labels"],
            [
                "Edited at 08:57, 2 earlier versions kept",
                "Edited at 08:57, 1 earlier version kept",
                "Edited at 08:57. The archive did not see the earlier text",
                "Edited, 1 earlier version kept",
                "Edited. The archive did not see the earlier text",
            ],
        )
        self.assertEqual(out["heads"][:3], ["Edited · 08:57"] * 3)
        self.assertEqual(out["heads"][3], "Edited")

    def test_an_edit_telegram_hides_is_not_an_edit(self) -> None:
        # Telegram bumps edit_date when only the reactions change and sets
        # edit_hide: with no earlier text kept, no pencil. A kept version still
        # counts, without the hidden time.
        rows = [
            {**_MSG, "version_count": 0, "edit_hide": 1},
            {**_MSG, "version_count": 1, "edit_hide": 1},
            {**_MSG, "version_count": 0, "edit_hide": 0},
            {**_MSG, "version_count": 0, "edit_hide": None},
        ]
        out = self._labels(rows)
        self.assertEqual(out["edited"], [False, True, True, True])
        self.assertEqual(out["labels"][1], "Edited, 1 earlier version kept")
        self.assertEqual(out["heads"][1], "Edited")
        self.assertEqual(out["labels"][2], "Edited at 08:57. The archive did not see the earlier text")

    def test_an_edit_on_a_later_day_names_the_day(self) -> None:
        rows = [
            {**_MSG, "edit_date": "2026-10-01T09:05:00"},
            {**_MSG, "date": "2025-12-31T23:00:00", "edit_date": "2026-01-02T09:05:00"},
        ]
        out = self._labels(rows)
        self.assertEqual(
            out["labels"],
            [
                "Edited on Oct 1 at 09:05, 2 earlier versions kept",
                "Edited on Jan 2, 2026 at 09:05, 2 earlier versions kept",
            ],
        )
        self.assertEqual(out["heads"], ["Edited · Oct 1, 09:05", "Edited · Jan 2, 2026, 09:05"])

    def test_the_time_follows_the_viewer_timezone(self) -> None:
        # 22:30 UTC is already the next day in Madrid; the send at 08:52 UTC is not.
        row = {**_MSG, "edit_date": "2026-09-30T22:30:00"}
        out = _run(
            f"[editedLabel({json.dumps(row)}), (viewerTimezone.value = 'Europe/Madrid', editedLabel({json.dumps(row)}))]",
            _MARK_DECLARATIONS,
        )
        self.assertEqual(
            out,
            [
                "Edited at 22:30, 2 earlier versions kept",
                "Edited on Oct 1 at 00:30, 2 earlier versions kept",
            ],
        )


class TestTheMarkInTheMetaRow(unittest.TestCase):
    """The marker is a real button beside the send time, with no second tooltip."""

    def test_the_button_is_wired_for_pointer_keyboard_and_touch(self) -> None:
        start = HTML.index('<button v-if="isEditedMessage(msg)"')
        button = HTML[start : HTML.index("</button>", start)]
        for wiring in (
            '@click.stop="onEditMarkClick(msg)"',
            '@pointerenter="onEditMarkPointerEnter(msg, $event)"',
            '@pointerleave="onEditMarkPointerLeave($event)"',
            '@pointerdown="onEditMarkPointerDown(msg, $event)"',
            '@pointerup="cancelEditLongPress"',
            '@pointercancel="cancelEditLongPress"',
            '@contextmenu="onEditMarkContextMenu($event)"',
            '@focus="onEditMarkFocus(msg, $event)"',
            '@blur="onEditMarkBlur($event)"',
            ":aria-describedby=\"isEditPeekFor(msg) ? 'edit-peek-desc' : null\"",
        ):
            self.assertIn(wiring, button)
        # It comes before the time, which stays the send time.
        meta = HTML[start : HTML.index("</span>\n                                    </div>", start)]
        self.assertLess(meta.index("meta-edited"), meta.index("formatTime(msg.date)"))

    def test_every_edited_check_goes_through_the_one_rule(self) -> None:
        # A bare edit_date test would mark a message Telegram hid the edit of
        # (a reaction), so the bubble frame, the time tooltip and the info panel
        # all ask isEditedMessage, and the frame follows the realtime flag.
        self.assertNotIn("edit_date || Number(", HTML)
        self.assertNotIn("msg.edit_date || versions", HTML)
        self.assertIn('<component v-if="isEditedMessage(infoPanelMessage)"', HTML)
        self.assertIn("editMsg.edit_hide = data.edit_hide ? 1 : 0", HTML)

    def test_a_long_press_does_not_select_or_open_the_system_menu(self) -> None:
        rule = HTML[HTML.index("        .message-meta .meta-edited {") :]
        rule = rule[: rule.index("}")]
        self.assertIn("-webkit-touch-callout: none;", rule)
        self.assertIn("user-select: none;", rule)
        # The hover underline only where a pointer can hover: a tap leaves no sticky state.
        self.assertIn("@media (hover: hover) {\n            .message-meta .meta-edited:hover", HTML)


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheTimeline(unittest.TestCase):
    """E: Sent, Edit 1, Edit 2 current, each with its time and the gap before it."""

    def _entries(self, msg: dict, kept: list[dict]) -> list[dict]:
        prelude = f"""{_renderer_bundle(HTML)}
versionsMessage.value = {json.dumps(msg)}
messageVersionsByMessage.value = {{ '7:{msg["id"]}': {json.dumps(kept)} }}
"""
        return _run(
            "versionEntries.value.map(e => ({ what: e.what, when: e.when, gap: e.gap, current: e.current,"
            " parts: e.parts === undefined ? 'none' : e.parts, html: e.html, formattingOnly: !!e.formattingOnly }))",
            _TIMELINE_DECLARATIONS,
            prelude,
        )

    def test_the_versions_run_down_a_timeline_with_their_changes(self) -> None:
        out = self._entries(_MSG, _KEPT)
        self.assertEqual(
            [(e["when"], e["what"], e["gap"], e["current"]) for e in out],
            [
                ("08:52", "Sent", "", False),
                ("08:54", "Edit 1", "2 min later", False),
                ("08:57", "Edit 2, current", "3 min later", True),
            ],
        )
        self.assertEqual(out[0]["parts"], "none")
        self.assertIn({"kind": "ins", "text": " It fills up by 9."}, out[1]["parts"])
        self.assertIn({"kind": "del", "text": "9."}, out[2]["parts"])
        self.assertIn({"kind": "ins", "text": "8, so get there early."}, out[2]["parts"])

    def test_gaps_read_in_minutes_hours_and_days_and_a_later_day_shows_its_date(self) -> None:
        msg = {**_MSG, "edit_date": "2026-10-03T12:00:00"}
        kept = [
            {"text": "b", "date": "2026-09-30T11:52:00"},
            {"text": "a", "date": _SENT},
        ]
        out = self._entries(msg, kept)
        self.assertEqual(
            [(e["when"], e["gap"]) for e in out],
            [("08:52", ""), ("11:52", "3 h later"), ("Oct 3, 12:00", "3 days later")],
        )

    def test_no_numbers_when_the_archive_returned_its_limit(self) -> None:
        kept = [{"text": f"v{i}", "date": _SENT} for i in range(100)]
        out = self._entries(_MSG, kept)
        self.assertEqual({e["what"] for e in out[:-1]}, {"Earlier version"})
        self.assertEqual(out[-1]["what"], "Current")
        # Equal times make no gap.
        self.assertEqual(out[1]["gap"], "")

    def test_the_template_draws_the_label_and_the_card(self) -> None:
        start = HTML.index('<li v-for="entry in versionEntries" :key="entry.key"')
        item = HTML[start : HTML.index("</li>", start)]
        self.assertIn(":class=\"{ 'is-current': entry.current }\"", item)
        self.assertIn('<span v-if="entry.when" class="version-when">{{ entry.when }}</span>', item)
        self.assertIn(
            '<span class="version-what">{{ entry.what }}<template v-if="entry.gap"> · {{ entry.gap }}</template>', item
        )
        self.assertIn('<template v-if="entry.formattingOnly"> · formatting only</template>', item)
        # The card is the bubble's own renderer's output, the only v-html here.
        self.assertIn('<span v-if="entry.html" v-html="entry.html"></span>', item)
        self.assertEqual(item.count("v-html"), 1)

    def test_the_marks_sit_on_the_formatted_words(self) -> None:
        out = self._entries(_MSG, _KEPT)
        self.assertEqual(out[0]["html"], "The north lot.")
        self.assertIn('<span class="diff-ins"> It fills up by 9.</span>', out[1]["html"])
        self.assertIn('<span class="diff-del">9.</span>', out[2]["html"])

    def test_each_version_keeps_its_own_formatting(self) -> None:
        """t2h: a version is drawn with the formatting it had, the way the bubble
        draws it; a removed word keeps the formatting of the version it left."""
        msg = {**_MSG, "text": "Meet at 8", "raw_data": {"entities": [{"type": "italic", "offset": 8, "length": 1}]}}
        kept = [
            {
                "text": "Meet at 9",
                "date": "2026-09-30T08:54:00",
                "entities": [{"type": "bold", "offset": 8, "length": 1}],
            },
            {"text": "Meet at 9", "date": _SENT, "entities": None},
        ]
        out = self._entries(msg, kept)
        self.assertEqual(out[0]["html"], "Meet at 9")
        self.assertEqual(out[1]["html"], "Meet at <strong>9</strong>")
        self.assertTrue(out[1]["formattingOnly"])
        self.assertFalse(out[2]["formattingOnly"])
        self.assertIn('<span class="diff-del"><strong>9</strong></span>', out[2]["html"])
        self.assertIn('<span class="diff-ins"><em>8</em></span>', out[2]["html"])

    def test_the_card_escapes_the_text(self) -> None:
        kept = [{"text": "<img src=x onerror=alert(1)>", "date": _SENT, "entities": None}]
        out = self._entries({**_MSG, "text": "<b>now</b> and then"}, kept)
        self.assertNotIn("<img", out[0]["html"])
        self.assertNotIn("<b>", out[1]["html"])
        self.assertIn("&lt;b&gt;now&lt;/b&gt;", out[1]["html"])

    def test_versions_from_the_sync_make_the_count_a_lower_bound(self) -> None:
        """5kr: the sync, a backup and an import read only the current text, so
        edits between two reads leave no version; the labels drop their numbers.
        A version from before the archive named its paths (no source) is
        unknown and counts the same way."""
        for source in ("sync", "backup", "import", None):
            with self.subTest(source=source):
                # The oldest version is the sent text, so only the source says edits may be missing.
                kept = [{**_KEPT[0], "source": source}, {**_KEPT[1], "source": "listener"}]
                out = self._entries(_MSG, kept)
                self.assertEqual([e["what"] for e in out], ["Sent", "Edit", "Current"])
        # Versions all from the listener keep the numbers.
        out = self._entries(_MSG, _KEPT)
        self.assertEqual([e["what"] for e in out], ["Sent", "Edit 1", "Edit 2, current"])

    def test_one_edit_seen_by_the_listener_is_edit_1(self) -> None:
        """A message first archived with a reaction's hidden edit date, then edited
        once: its version is dated at the send time, so it reads Sent / Edit 1."""
        msg = {**_MSG, "version_count": 1}
        kept = [{"text": "The north lot.", "date": _SENT, "source": "listener"}]
        out = self._entries(msg, kept)
        self.assertEqual([e["what"] for e in out], ["Sent", "Edit 1, current"])

    def test_a_message_first_seen_already_edited_says_so(self) -> None:
        kept = [{"text": "The north lot. It fills up by 9.", "date": "2026-09-30T08:54:00", "source": "backup"}]
        out = self._entries(_MSG, kept)
        self.assertEqual([e["what"] for e in out], ["First seen, already edited", "Current"])

    def test_the_subtitle_says_at_least_when_edits_may_be_missing(self) -> None:
        def subtitle(kept: list[dict]) -> str:
            prelude = f"""
const formatDatePill = () => 'September 30'
const formatTime = () => '08:52'
versionsMessage.value = {json.dumps(_MSG)}
messageVersionsByMessage.value = {{ '7:5': {json.dumps(kept)} }}
"""
            return _run(
                "versionsSubtitle.value", (*_TIMELINE_DECLARATIONS, "const versionsSubtitle = computed("), prelude
            )

        self.assertEqual(subtitle(_KEPT), "Sent September 30 at 08:52 · 2 earlier versions")
        for source in ("sync", "backup", "import", None):
            self.assertEqual(
                subtitle([{**_KEPT[0], "source": source}, _KEPT[1]]), "Sent September 30 at 08:52 · at least 2 edits"
            )
        # First seen already edited: the oldest kept text was itself an edit.
        self.assertEqual(
            subtitle([{**_KEPT[0], "source": "listener"}]), "Sent September 30 at 08:52 · at least 2 edits"
        )


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestEarlierMedia(unittest.TestCase):
    """b2v: a version whose photo or file an edit replaced shows it, and the current
    entry shows the current media beside it; a version kept only as media has no
    card and the diff runs past it."""

    def _entries(self, msg: dict, kept: list[dict]) -> list[dict]:
        prelude = f"""{_renderer_bundle(HTML)}
versionsMessage.value = {json.dumps(msg)}
messageVersionsByMessage.value = {{ '7:{msg["id"]}': {json.dumps(kept)} }}
"""
        return _run(
            "versionEntries.value.map(e => ({ what: e.what, media: e.media, mediaOnly: e.mediaOnly, html: e.html,"
            " parts: e.parts === undefined ? 'none' : e.parts }))",
            _TIMELINE_DECLARATIONS,
            prelude,
        )

    def test_an_earlier_photo_sits_on_its_version_and_the_current_one_beside_it(self) -> None:
        msg = {**_MSG, "text": "Look", "media": {"type": "photo", "url": "/media/r7/5_photo"}}
        kept = [
            {
                "text": "Look",
                "date": _SENT,
                "source": "listener",
                "media": [{"type": "photo", "url": "/media/r7/5_v3", "file_name": "111.jpg", "downloaded": True}],
            }
        ]
        out = self._entries(msg, kept)
        self.assertEqual(
            out[0]["media"],
            [
                {
                    "key": f"{_SENT}:0:m0",
                    "url": "/media/r7/5_v3",
                    "thumbUrl": "/media/thumb/200/r7/5_v3",
                    "label": "Earlier photo",
                }
            ],
        )
        self.assertEqual(
            out[1]["media"],
            [
                {
                    "key": "current:m0",
                    "url": "/media/r7/5_photo",
                    "thumbUrl": "/media/thumb/200/r7/5_photo",
                    "label": "Current photo",
                }
            ],
        )

    def test_without_earlier_media_the_current_entry_shows_none(self) -> None:
        msg = {**_MSG, "media": {"type": "photo", "url": "/media/r7/5_photo"}}
        out = self._entries(msg, _KEPT)
        self.assertEqual([e["media"] for e in out], [[], [], []])

    def test_a_file_is_named_and_a_file_not_downloaded_says_so(self) -> None:
        kept = [
            {
                "text": "The north lot.",
                "date": _SENT,
                "media": [
                    {"type": "document", "url": None, "file_name": "2222_report.pdf", "downloaded": False},
                    {"type": "video", "url": None, "no_download": True},
                ],
            }
        ]
        out = self._entries(_MSG, kept)
        self.assertEqual(
            [(m["label"], m["url"], m["thumbUrl"]) for m in out[0]["media"]],
            [("Earlier file · report.pdf · not downloaded", "", ""), ("Earlier video", "", "")],
        )

    def test_a_version_kept_only_as_media_has_no_card_and_the_diff_runs_past_it(self) -> None:
        kept = [
            {
                "text": None,
                "media_only": True,
                "date": "2026-09-30T08:54:00",
                "media": [{"type": "photo", "url": "/media/r7/5_v1"}],
            },
            {"text": "The north lot.", "date": _SENT, "source": "listener"},
        ]
        out = self._entries(_MSG, kept)
        self.assertEqual([(e["what"], e["mediaOnly"]) for e in out[:2]], [("Sent", False), ("Earlier media", True)])
        self.assertEqual((out[1]["html"], out[1]["parts"]), ("", "none"))
        self.assertIn({"kind": "ins", "text": " It fills up by 8, so get there early."}, out[2]["parts"])

    def test_the_template_draws_a_thumbnail_a_link_and_no_card_for_media_only(self) -> None:
        start = HTML.index('<li v-for="entry in versionEntries" :key="entry.key"')
        item = HTML[start : HTML.index("</li>", start)]
        self.assertIn('<div v-for="media in entry.media" :key="media.key" class="version-media">', item)
        self.assertIn('<a v-if="media.thumbUrl" :href="media.url" target="_blank" rel="noopener"', item)
        self.assertIn('<img :src="media.thumbUrl" :alt="media.label" loading="lazy">', item)
        self.assertIn('<span v-else class="version-media-caption">{{ media.label }}</span>', item)
        self.assertIn('<div v-if="!entry.mediaOnly" class="version-bubble"', item)
        # The label is text, never markup: the card stays the only v-html.
        self.assertEqual(item.count("v-html"), 1)


@unittest.skipUnless(NODE, "node is required to execute the handler")
class TestTheLiveEditFrame(unittest.TestCase):
    """An "edit" frame updates the open chat at once, a formatting-only edit too."""

    def _handle(self, msg: dict, frame: dict, *, pinned: list | None = None, expression: str = "messages.value[0]"):
        prelude = f"""
const messages = {{ value: [{json.dumps(msg)}] }}
const pinnedMessages = {{ value: {json.dumps(pinned or [])} }}
const clearMessageVersionsCache = () => {{}}
const isVersionsPanelOpenFor = () => false
const isEditPeekFor = () => false
const loadMessageVersions = () => {{}}
const mediaRevision = {{ value: 0 }}
const handle = (data) => {{
    switch (data.type) {{
        {_block("case 'edit':", "case 'reaction':")}
    }}
}}
handle({json.dumps(frame)})
"""
        return _run(expression, (), prelude)

    # 2A (9.0): the frame of an edit that replaced the photo carries the new media.
    _OLD_MEDIA = {"id": "5_photo", "type": "photo", "url": "/media/r7/5_photo", "file_name": "old.jpg"}
    _NEW_MEDIA = {"id": "5_photo", "type": "photo", "url": "/media/r7/5_photo?v=1", "file_name": "new.jpg"}
    _MEDIA_FRAME = {
        "type": "edit",
        "chat_ref": "r7",
        "message_id": 5,
        "new_text": "Look at this",
        "edit_date": "2026-09-30T08:54:00",
        "edit_hide": 0,
        "media": _NEW_MEDIA,
    }

    def test_a_replacing_edit_swaps_in_the_new_media_at_once(self) -> None:
        msg = {
            "id": 5,
            "text": "Look at this",
            "version_count": 0,
            "media": self._OLD_MEDIA,
            "mediaLoadFailed": True,
            "mapLoadFailed": True,
        }
        out = self._handle(msg, self._MEDIA_FRAME)
        self.assertEqual(out["media"], self._NEW_MEDIA)
        self.assertFalse(out["mediaLoadFailed"])
        self.assertFalse(out["mapLoadFailed"])
        self.assertEqual(out["version_count"], 1)

        # A frame without media (the media did not change, or did not fit) keeps it.
        frame_without = {key: value for key, value in self._MEDIA_FRAME.items() if key != "media"}
        out = self._handle(msg, frame_without)
        self.assertEqual(out["media"], self._OLD_MEDIA)

    def test_a_replacing_edit_tells_the_gif_watcher_once_and_a_text_edit_does_not(self) -> None:
        """A GIF or round video loads only once the watcher observes its element,
        and the watcher runs on the list or on mediaRevision: the swap bumps it."""
        msg = {"id": 5, "text": "Look", "version_count": 0, "media": self._OLD_MEDIA}
        pinned = [{"id": 5, "text": "Look", "version_count": 0, "media": self._OLD_MEDIA}]
        revision = self._handle(msg, self._MEDIA_FRAME, pinned=pinned, expression="mediaRevision.value")
        self.assertGreaterEqual(revision, 1)
        frame_without = {key: value for key, value in self._MEDIA_FRAME.items() if key != "media"}
        self.assertEqual(self._handle(msg, frame_without, expression="mediaRevision.value"), 0)

    def test_every_video_element_is_keyed_by_its_media_url(self) -> None:
        """A replaced clip keeps its element type, so Vue would patch the same
        <video>: a <source> src change never reloads it, and the lazy loader sets
        a GIF's src only once. Keyed by the URL (the replacement adds ?v=), the new
        clip gets a new element."""
        videos = [chunk.split(">", 1)[0] for chunk in HTML.split("<video")[1:]]
        # A <video :src> (the info panel, the lightbox) reloads when its src changes;
        # the bubble's five (an album tile, a GIF, a round video, a video and a
        # video sticker) load through a <source> or data-src and need the key.
        bubble = [tag for tag in videos if "getMediaUrl(" in tag and ':src="' not in tag]
        self.assertEqual(len(bubble), 5, bubble)
        for tag in bubble:
            self.assertRegex(tag, r':key="getMediaUrl\((msg|albumMsg)\)"', tag)

    def test_a_pinned_row_takes_the_edit_too(self) -> None:
        """The pinned-only list holds its own copy of the row, which no refresh of
        the newest 50 reaches: it takes the text and the media as well."""
        msg = {"id": 5, "text": "Look", "version_count": 0, "media": self._OLD_MEDIA}
        pinned = [
            {"id": 5, "text": "Look", "version_count": 0, "media": self._OLD_MEDIA},
            {"id": 6, "text": "Other", "version_count": 0, "media": self._OLD_MEDIA},
        ]
        out = self._handle(msg, self._MEDIA_FRAME, pinned=pinned, expression="[messages.value, pinnedMessages.value]")
        loaded, pinned_out = out
        self.assertEqual(loaded[0]["media"], self._NEW_MEDIA)
        self.assertEqual((pinned_out[0]["text"], pinned_out[0]["media"]), ("Look at this", self._NEW_MEDIA))
        self.assertEqual(pinned_out[0]["version_count"], 1)
        # Another pinned message is left alone.
        self.assertEqual((pinned_out[1]["text"], pinned_out[1]["media"]), ("Other", self._OLD_MEDIA))

    def test_a_formatting_only_edit_takes_the_new_entities_and_counts_a_version(self) -> None:
        bold = [{"type": "bold", "offset": 0, "length": 4}]
        italic = [{"type": "italic", "offset": 8, "length": 4}]
        msg = {
            "id": 5,
            "text": "Meet at nine",
            "edit_date": None,
            "version_count": 0,
            "raw_data": {"entities": bold, "webpage": {"url": "https://keep.example"}},
        }
        frame = {
            "type": "edit",
            "chat_ref": "r7",
            "message_id": 5,
            "new_text": "Meet at nine",
            "edit_date": "2026-09-30T08:54:00",
            "edit_hide": 0,
            "entities": italic,
        }
        out = self._handle(msg, frame)
        self.assertEqual(out["raw_data"], {"entities": italic, "webpage": {"url": "https://keep.example"}})
        self.assertEqual(out["version_count"], 1)
        self.assertEqual(out["edit_date"], "2026-09-30T08:54:00")

        # Formatting removed: the entities go, the rest stays.
        out = self._handle(msg, {**frame, "entities": None})
        self.assertEqual(out["raw_data"], {"webpage": {"url": "https://keep.example"}})
        # A frame without entities (the text was cut to fit) keeps the formatting it had.
        frame_without = {key: value for key, value in frame.items() if key != "entities"}
        out = self._handle(msg, frame_without)
        self.assertEqual(out["raw_data"]["entities"], bold)
        self.assertEqual(out["version_count"], 1)


class TestThePanelAndTheSheet(unittest.TestCase):
    """E: beside the chat on a wide screen, a bottom sheet on a phone."""

    def _rule(self, selector: str, within: str = HTML) -> str:
        rule = within[within.index(selector) :]
        return rule[: rule.index("}")]

    def test_it_sits_beside_the_chat_in_the_main_row(self) -> None:
        panel = HTML.index('<aside v-if="versionsMessage" ref="versionsDialog" id="versions-panel"')
        chat = HTML.index("<!-- Main Chat Area - full width on mobile -->")
        info = HTML.index('<aside v-if="showInfoPanel && selectedChat" id="info-panel"')
        self.assertLess(chat, panel)
        self.assertLess(panel, info)
        # No scrim beside the chat: only the phone's sheet dims it.
        self.assertIn('<div v-if="versionsMessage && versionsSheet" class="versions-scrim', HTML)
        self.assertNotIn('@click.self="closeVersionsPanel"', HTML)

    def test_the_layout_is_a_sheet_then_an_overlay_then_a_column(self) -> None:
        sheet = self._rule("        .versions-panel {")
        for decl in ("position: fixed;", "bottom: 0;", "max-height: 85%;", "border-radius: 16px 16px 0 0;"):
            self.assertIn(decl, sheet)
        wide = HTML[HTML.index("        @media (min-width: 768px) {\n            .versions-panel {") :]
        self.assertIn("width: clamp(300px, 34vw, 380px);", self._rule(".versions-panel {", wide))
        column = HTML[HTML.index("        @media (min-width: 1024px) {\n            .versions-panel {") :]
        column_rule = self._rule(".versions-panel {", column)
        self.assertIn("position: relative;", column_rule)
        self.assertIn("flex-shrink: 0;", column_rule)
        # The handle on the sheet, gone beside the chat.
        self.assertIn(".versions-panel .versions-header::before {", HTML)

    def test_the_message_is_highlighted_in_the_chat_while_open(self) -> None:
        self.assertIn(
            "isSelectedMessage(msg) || isVersionsPanelOpenFor(msg) ? 'message-info-selected' : ''",
            HTML,
        )

    def test_the_sheet_is_modal_and_the_panel_is_not(self) -> None:
        query = _setup_slice(HTML, "const versionsSheetQuery = window.matchMedia")
        self.assertIn("window.matchMedia('(max-width: 767px)')", query)
        handler = _setup_slice(HTML, "const handleVersionsKeydown = (e) =>")
        self.assertIn("if (versionsSheet.value) cycleTabWithin(versionsDialog.value, e)", handler)

    def test_another_chat_closes_the_panel_and_the_peek(self) -> None:
        watcher = _block("watch(() => selectedChat.value?.ref || null, () => {", "// ---- The peek on the pencil")
        self.assertIn("closeVersionsPanel(false)", watcher)
        self.assertIn("closeEditPeek()", watcher)


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestThePanelKeyboard(unittest.TestCase):
    """Focus moves into the panel; Escape closes it and returns focus to the pencil."""

    _PRELUDE = """
const FOCUSED = []
const LISTENERS = {}
const document = {
    activeElement: null,
    addEventListener: (type, fn) => { LISTENERS[type] = fn },
    removeEventListener: (type) => { delete LISTENERS[type] },
}
globalThis.HTMLElement = class {}
const mark = Object.assign(new HTMLElement(), { isConnected: true, focus: () => FOCUSED.push('mark') })
const nextTick = (fn) => { if (fn) fn(); return Promise.resolve() }
const versionsCloseBtn = { value: { focus: () => FOCUSED.push('close') } }
const versionsDialog = { value: { contains: (el) => el === 'inside' } }
const versionsFromFeed = { value: false }
const versionsSheet = { value: false }
const messagesContainer = { value: null }
let versionsTrigger = null
const CYCLED = []
const cycleTabWithin = () => CYCLED.push('tab')
const closeEditPeek = () => {}
const loadMessageVersions = async () => {}
const watch = () => {}
"""

    def test_open_escape_and_back_to_the_pencil(self) -> None:
        out = _run(
            """(() => {
    document.activeElement = mark
    toggleMessageVersions({ id: 5, chat_id: 7 })
    const opened = { focused: FOCUSED.slice(), open: !!versionsMessage.value, listening: !!LISTENERS.keydown }
    // Escape typed in the chat's search field is the field's own.
    LISTENERS.keydown({ key: 'Escape', target: { closest: () => ({}) } })
    const afterFieldEscape = !!versionsMessage.value
    LISTENERS.keydown({ key: 'Tab', target: {} })
    const cycledBeside = CYCLED.length
    versionsSheet.value = true
    LISTENERS.keydown({ key: 'Tab', target: {} })
    const cycledOnSheet = CYCLED.length
    versionsSheet.value = false
    LISTENERS.keydown({ key: 'Escape', target: { closest: () => null } })
    return { opened, afterFieldEscape, cycledBeside, cycledOnSheet, closed: versionsMessage.value, focused: FOCUSED, listening: !!LISTENERS.keydown }
})()""",
            (
                "const messageVersionsKey = (msg) =>",
                "const toggleMessageVersions = async (msg) =>",
                "const closeVersionsPanel = (restoreFocus = true) =>",
                "const handleVersionsKeydown = (e) =>",
            ),
            self._PRELUDE,
        )
        self.assertEqual(out["opened"], {"focused": ["close"], "open": True, "listening": True})
        self.assertTrue(out["afterFieldEscape"])
        # Tab stays inside only on the phone's sheet.
        self.assertEqual((out["cycledBeside"], out["cycledOnSheet"]), (0, 1))
        self.assertIsNone(out["closed"])
        self.assertEqual(out["focused"], ["close", "mark"])
        self.assertFalse(out["listening"])


# The peek's code, from its state to its last handler, and what it closes over.
_PEEK_CODE = _block("                const editPeek = ref(null)", "                // The peek grows when")

_PEEK_PRELUDE = """
let NOW = 0
let TIMERS = []
let SEQ = 0
const setTimeout = (fn, ms) => { const id = ++SEQ; TIMERS.push({ id, at: NOW + ms, fn }); return id }
const clearTimeout = (id) => { TIMERS = TIMERS.filter(t => t.id !== id) }
const advance = (ms) => {
    NOW += ms
    for (;;) {
        const due = TIMERS.filter(t => t.at <= NOW).sort((a, b) => a.at - b.at)[0]
        if (!due) break
        TIMERS = TIMERS.filter(t => t !== due)
        due.fn()
    }
}
Date.now = () => NOW
const requestAnimationFrame = (fn) => { fn(); return 1 }
const LISTENERS = {}
const document = {
    activeElement: null,
    documentElement: { clientWidth: 1440 },
    addEventListener: (type, fn) => { LISTENERS[type] = fn },
    removeEventListener: (type) => { delete LISTENERS[type] },
}
const window = { innerWidth: 1440, innerHeight: 900, addEventListener() {}, removeEventListener() {} }
const nextTick = (fn) => { if (fn) fn(); return Promise.resolve() }
const PEEK = { offsetWidth: 300, offsetHeight: 120, contains: (el) => el === 'in-peek' }
const LOADS = []
const loadMessageVersions = (msg) => { LOADS.push(msg.id) }
const TOGGLED = []
const toggleMessageVersions = (msg) => { TOGGLED.push(msg.id) }
const anchorAt = (rect, focusVisible = false) => ({
    isConnected: true,
    getBoundingClientRect: () => rect,
    matches: () => focusVisible,
    contains: (el) => el === 'in-mark',
    focus() {},
})
const MSG = __MSG__
"""


def _peek(epilogue: str, kept: list[dict] | None = None) -> dict:
    prelude = _PEEK_PRELUDE.replace("__MSG__", json.dumps(_MSG))
    if kept is not None:
        prelude += f"messageVersionsByMessage.value = {{ '7:5': {json.dumps(kept)} }}\n"
    program = _PEEK_CODE + "\neditPeekEl.value = PEEK\n" + epilogue
    return _run_setup_program(
        HTML,
        (
            "const WORD_DIFF_CELLS = 40000",
            "const diffTokens = (text, byChar) =>",
            "const diffWords = (before, after) =>",
            "const versionWhen = (msg, dateStr) =>",
            "const VERSIONS_LIMIT = 100",
            "const messageVersionsKey = (msg) =>",
            "const getMessageVersions = (msg) =>",
            *_MARK_DECLARATIONS,
        ),
        _PRELUDE + prelude,
        program,
    )


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestThePeek(unittest.TestCase):
    """C: the text before the last edit, without flicker, inside the screen."""

    def test_it_shows_the_text_before_the_last_edit_and_how_many_versions(self) -> None:
        out = _peek(
            """openEditPeek(MSG, anchorAt({ top: 500, bottom: 518, left: 880, right: 900 }))
console.log(JSON.stringify({ before: editPeekBefore.value, count: editPeekVersionCount.value, loads: LOADS }))""",
            kept=_KEPT,
        )
        self.assertEqual(out["before"]["label"], "Before the last edit · edited 08:54")
        self.assertEqual(out["before"]["text"], "The north lot. It fills up by 9.")
        # Only the old side: what the edit removed is marked, nothing it added.
        self.assertEqual({part["kind"] for part in out["before"]["parts"]}, {"same", "del"})
        self.assertIn({"kind": "del", "text": "9."}, out["before"]["parts"])
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["loads"], [])  # already loaded: no second fetch

    def test_one_kept_version_is_the_sent_text_and_the_first_peek_loads(self) -> None:
        out = _peek(
            """openEditPeek(MSG, anchorAt({ top: 500, bottom: 518, left: 880, right: 900 }))
const loads = LOADS.slice()
messageVersionsByMessage.value = { '7:5': [{ text: 'The north lot.', date: MSG.date }] }
console.log(JSON.stringify({ loads, label: editPeekBefore.value.label }))"""
        )
        self.assertEqual(out["loads"], [5])
        self.assertEqual(out["label"], "Before the last edit · sent 08:52")

    def test_it_stays_inside_the_screen(self) -> None:
        out = _peek(
            """const at = (rect, width = 1440) => {
    document.documentElement.clientWidth = width
    closeEditPeek()
    openEditPeek(MSG, anchorAt(rect))
    return { ...editPeekPos.value }
}
console.log(JSON.stringify({
    above: at({ top: 500, bottom: 518, left: 880, right: 900 }),
    nearTop: at({ top: 40, bottom: 58, left: 880, right: 900 }),
    nearLeft: at({ top: 500, bottom: 518, left: 60, right: 80 }),
    phone: at({ top: 500, bottom: 518, left: 360, right: 386 }, 390),
}))""",
            kept=_KEPT,
        )
        # Above the pencil, its right edge on the pencil's, 6px apart.
        self.assertEqual(out["above"], {"top": 374, "left": 600, "below": False, "placed": True})
        # No room above: below it.
        self.assertEqual(out["nearTop"], {"top": 64, "left": 600, "below": True, "placed": True})
        # Never past the left edge, nor the right edge of a phone.
        self.assertEqual(out["nearLeft"]["left"], 8)
        self.assertEqual(out["phone"]["left"], 390 - 8 - 300)

    def test_a_pointer_rests_before_it_opens_and_may_move_onto_it(self) -> None:
        out = _peek(
            """const mark = anchorAt({ top: 500, bottom: 518, left: 880, right: 900 })
const ev = (extra = {}) => ({ pointerType: 'mouse', currentTarget: mark, ...extra })
const log = {}
onEditMarkPointerEnter(MSG, ev())
advance(300); log.before = !!editPeek.value
advance(150); log.after = !!editPeek.value
// Out of the pencil and onto the peek within the grace time: it stays.
onEditMarkPointerLeave(ev())
advance(100)
cancelEditPeekClose()
advance(1000); log.onThePeek = !!editPeek.value
// Off the peek: it closes after the grace time, not at once.
onEditPeekPointerLeave({ pointerType: 'mouse' })
advance(100); log.graceful = !!editPeek.value
advance(200); log.closed = !editPeek.value
// A quick pass over the pencil never opens it.
onEditMarkPointerEnter(MSG, ev())
advance(200)
onEditMarkPointerLeave(ev())
advance(1000); log.quickPass = !!editPeek.value
console.log(JSON.stringify(log))""",
            kept=_KEPT,
        )
        self.assertEqual(
            out,
            {
                "before": False,
                "after": True,
                "onThePeek": True,
                "graceful": True,
                "closed": True,
                "quickPass": False,
            },
        )

    def test_escape_closes_only_the_peek_and_focus_stays(self) -> None:
        out = _peek(
            """const mark = anchorAt({ top: 500, bottom: 518, left: 880, right: 900 }, true)
onEditMarkFocus(MSG, { currentTarget: mark })
const opened = !!editPeek.value && !!LISTENERS.keydown
let stopped = false, prevented = false
LISTENERS.keydown({ key: 'Escape', stopPropagation: () => { stopped = true }, preventDefault: () => { prevented = true } })
console.log(JSON.stringify({ opened, closed: !editPeek.value, stopped, prevented, listening: !!LISTENERS.keydown }))""",
            kept=_KEPT,
        )
        self.assertEqual(out, {"opened": True, "closed": True, "stopped": True, "prevented": True, "listening": False})

    def test_a_click_focus_does_not_peek_and_blur_closes(self) -> None:
        out = _peek(
            """const clicked = anchorAt({ top: 500, bottom: 518, left: 880, right: 900 }, false)
onEditMarkFocus(MSG, { currentTarget: clicked })
const byClick = !!editPeek.value
const keyed = anchorAt({ top: 500, bottom: 518, left: 880, right: 900 }, true)
onEditMarkFocus(MSG, { currentTarget: keyed })
const byKey = !!editPeek.value
// Focus moving into the peek keeps it; anywhere else closes it.
onEditMarkBlur({ currentTarget: keyed, relatedTarget: 'in-peek' })
const intoPeek = !!editPeek.value
onEditMarkBlur({ currentTarget: keyed, relatedTarget: null })
console.log(JSON.stringify({ byClick, byKey, intoPeek, blurred: !editPeek.value }))""",
            kept=_KEPT,
        )
        self.assertEqual(out, {"byClick": False, "byKey": True, "intoPeek": True, "blurred": True})

    def test_a_long_press_peeks_and_the_tap_that_ends_it_opens_nothing(self) -> None:
        out = _peek(
            """const mark = anchorAt({ top: 500, bottom: 518, left: 880, right: 900 })
const touch = { pointerType: 'touch', currentTarget: mark, clientX: 10, clientY: 10 }
onEditMarkPointerDown(MSG, touch)
advance(300); const early = !!editPeek.value
advance(250); const pressed = !!editPeek.value
let menuBlocked = false
onEditMarkContextMenu({ preventDefault: () => { menuBlocked = true } })
onEditMarkClick(MSG)
const toggledAfterPress = TOGGLED.slice()
// A tap elsewhere closes it.
LISTENERS.pointerdown({ target: 'elsewhere' })
const tappedAway = !editPeek.value
// A short tap opens the history; a moving finger is a scroll, not a press.
advance(2000)
onEditMarkPointerDown(MSG, touch)
advance(100)
cancelEditLongPress()
onEditMarkClick(MSG)
onEditMarkPointerDown(MSG, touch)
onEditMarkPointerMove({ clientX: 10, clientY: 40 })
advance(1000)
console.log(JSON.stringify({ early, pressed, menuBlocked, toggledAfterPress, tappedAway, toggled: TOGGLED, scrolled: !editPeek.value }))""",
            kept=_KEPT,
        )
        self.assertEqual(
            out,
            {
                "early": False,
                "pressed": True,
                "menuBlocked": True,
                "toggledAfterPress": [],
                "tappedAway": True,
                "toggled": [5],
                "scrolled": True,
            },
        )

    def test_see_all_opens_the_history_and_a_touch_never_hovers(self) -> None:
        out = _peek(
            """const mark = anchorAt({ top: 500, bottom: 518, left: 880, right: 900 })
onEditMarkPointerEnter(MSG, { pointerType: 'touch', currentTarget: mark })
advance(1000)
const hovered = !!editPeek.value
openEditPeek(MSG, mark)
openVersionsFromPeek()
console.log(JSON.stringify({ hovered, toggled: TOGGLED, closed: !editPeek.value }))""",
            kept=_KEPT,
        )
        self.assertEqual(out, {"hovered": False, "toggled": [5], "closed": True})

    def test_the_peek_is_one_element_outside_the_list(self) -> None:
        start = HTML.index('<div v-if="editPeek" ref="editPeekEl" class="edit-peek" role="tooltip" id="edit-peek"')
        peek = HTML[start : HTML.index("<!-- Lightbox Modal for Images -->", start)]
        self.assertIn('<div id="edit-peek-desc">', peek)
        self.assertIn("{{ editedLabel(editPeek.msg) }}", peek)
        self.assertIn("See all {{ editPeekVersionCount }} versions", peek)
        # It never takes focus: the pencil itself opens the history from a keyboard.
        self.assertIn('tabindex="-1" class="edit-peek-all"', peek)
        # Hidden until placed, so it never flashes in a corner.
        self.assertIn("visibility: hidden;", self._rule(".edit-peek {"))
        self.assertIn("visibility: visible;", self._rule(".edit-peek.is-placed {"))

    def _rule(self, selector: str) -> str:
        rule = HTML[HTML.index("        " + selector) :]
        return rule[: rule.index("}")]


class TestTheEditedOnlyMode(unittest.TestCase):
    """F: "Edited messages" in the chat menu, under "Deleted messages"."""

    def test_the_menu_item_sits_under_deleted_messages_with_the_count(self) -> None:
        menu = HTML[
            HTML.index(
                '<div v-if="!noDownload || chatDeletedCount > 0 || chatEditedCount > 0" class="relative phone-hide">'
            ) :
        ]
        menu = menu[: menu.index("<!-- Info panel toggle")]
        item = menu[menu.index('<button v-if="chatEditedCount !== 0" type="button" @click="openEditedOnly"') :]
        item = item[: item.index("</button>")]
        self.assertIn('<span class="flex-1 text-left">Edited messages</span>', item)
        self.assertIn(
            '<span v-if="chatEditedCount" class="info-action-count">{{ formatCount(chatEditedCount) }}</span>', item
        )
        self.assertLess(menu.index("openDeletedOnly"), menu.index("openEditedOnly"))
        self.assertLess(menu.index("openEditedOnly"), menu.index("exportChat()"))

    def test_the_count_comes_from_the_chat_figures_like_the_deleted_one(self) -> None:
        count = _setup_slice(HTML, "const chatEditedCount = computed(() =>")
        self.assertIn("chatStats.value?.edited_messages", count)
        self.assertIn("return count == null ? null : Number(count) || 0", count)

    def test_the_info_panel_row_opens_the_same_mode(self) -> None:
        self.assertIn(
            '<button v-if="chatStats.edited_messages > 0" type="button" class="tg-row" @click="openEditedOnlyFromInfo"',
            HTML,
        )
        body = _setup_slice(HTML, "const openEditedOnlyFromInfo = () =>")
        self.assertIn("closeInfoPanel()", body)
        self.assertIn("openEditedOnly()", body)

    def test_the_mode_has_the_same_chip_and_way_out(self) -> None:
        bar = HTML[
            HTML.index('<div v-if="editedOnly && !showMediaGallery" class="deleted-filter-bar edited-filter-bar"') :
        ]
        bar = bar[: bar.index("</div>")]
        self.assertIn('class="deleted-filter-chip" aria-pressed="true" @click="closeEditedOnly"', bar)
        self.assertIn('aria-label="Edited only. Remove this filter"', bar)
        self.assertIn("<span>Edited only</span>", bar)
        self.assertIn('class="deleted-filter-x"', bar)
        self.assertIn("chatEditedCount === 1 ? 'edited message' : 'edited messages'", bar)
        close = _setup_slice(HTML, "const closeChatSearch = async () =>")
        self.assertIn("editedOnly.value = false", close)
        for declaration in ("const selectChat = async (chat, options = {}) =>", "const selectTopic = async ("):
            self.assertIn("editedOnly.value = false", _setup_slice(HTML, declaration), declaration)
        self.assertIn("'No edited messages in this chat'", HTML)
        self.assertIn("editedOnly ? 'Search edited'", HTML)

    def test_each_result_offers_the_way_to_the_chat(self) -> None:
        self.assertEqual(
            HTML.count(
                '<button type="button" class="deleted-hide edited-show" @click.stop="showDeletedInChat(msg)">'
                "Show in chat</button>"
            ),
            2,
        )
        self.assertEqual(HTML.count("<span>{{ editedHeadText(msg) }}</span>"), 2)
        self.assertIn('<template v-else-if="editedOnly"><span class="deleted-head-group">', HTML)
        self.assertIn('<div v-else-if="editedOnly" class="deleted-head-row">', HTML)
        # A deleted and edited message keeps its deleted head, with the way to the chat.
        self.assertEqual(HTML.count('<template v-if="deletedOnly || editedOnly">'), 2)

    @unittest.skipUnless(NODE, "node is required to execute the helpers")
    def test_the_list_asks_the_server_for_edits_only(self) -> None:
        epilogue = """
(async () => {
    const shape = async (search, edited) => {
        messageSearchQuery.value = search
        editedOnly.value = edited
        messages.value = []
        loading.value = false
        hasMore.value = true
        URLS.length = 0
        resetMessagePagination()
        await loadMessages()
        return { url: URLS[0], contiguous: messageWindowIsContiguous.value }
    }
    console.log(JSON.stringify({
        plain: await shape('', false),
        edited: await shape('', true),
        narrowed: await shape('north', true),
    }))
})();
"""
        out = _run_setup_program(
            HTML,
            (
                "const messageIdKey = (msg) =>",
                "const upsertMessages = (incomingMessages, ",
                "const resetMessagePagination = () =>",
                "const loadMessages = async () =>",
            ),
            _PRODUCER_PRELUDE,
            epilogue,
        )
        self.assertNotIn("edited_only", out["plain"]["url"])
        self.assertTrue(out["plain"]["contiguous"])
        self.assertIn("offset=0", out["edited"]["url"])
        self.assertIn("&edited_only=true", out["edited"]["url"])
        self.assertNotIn("deleted_only", out["edited"]["url"])
        self.assertNotIn("search=", out["edited"]["url"])
        self.assertFalse(out["edited"]["contiguous"])
        self.assertIn("search=north", out["narrowed"]["url"])
        self.assertIn("&edited_only=true", out["narrowed"]["url"])

    @unittest.skipUnless(NODE, "node is required to execute the helpers")
    def test_open_close_and_show_in_chat(self) -> None:
        prelude = """
const chatMenuOpen = { value: true }
const deletedOnly = { value: true }
const editedOnly = { value: false }
const messageSearchQuery = { value: 'north' }
const chatSearchOpen = { value: false }
let messageSearchDebounceTimer = null
const CALLS = []
const closePinnedView = () => CALLS.push('closePinned')
const searchMessages = async () => CALLS.push(`search:${deletedOnly.value}:${editedOnly.value}`)
const openChatSearch = async () => { chatSearchOpen.value = true; CALLS.push('openSearch') }
const loadMessagesAroundId = async (id) => CALLS.push(`jump:${id}:${editedOnly.value}:${messageSearchQuery.value}`)
const nextTick = () => Promise.resolve()
const chatSearchInput = { value: { focus: () => CALLS.push('focusSearch') } }
"""
        out = _run_setup_program(
            HTML,
            (
                "const openEditedOnly = async () =>",
                "const closeEditedOnly = async () =>",
                "const showDeletedInChat = async (msg) =>",
            ),
            prelude,
            """(async () => {
    await openEditedOnly()
    const opened = { deletedOnly: deletedOnly.value, editedOnly: editedOnly.value, menu: chatMenuOpen.value, calls: CALLS.splice(0) }
    await closeEditedOnly()
    const closed = { editedOnly: editedOnly.value, calls: CALLS.splice(0) }
    editedOnly.value = true
    await showDeletedInChat({ id: 5 })
    console.log(JSON.stringify({ opened, closed, shown: { editedOnly: editedOnly.value, search: chatSearchOpen.value, calls: CALLS } }))
})();""",
        )
        # The two modes never hold at once.
        self.assertEqual(
            out["opened"],
            {
                "deletedOnly": False,
                "editedOnly": True,
                "menu": False,
                "calls": ["closePinned", "search:false:true", "openSearch"],
            },
        )
        self.assertEqual(out["closed"], {"editedOnly": False, "calls": ["search:false:false", "focusSearch"]})
        self.assertEqual(out["shown"], {"editedOnly": False, "search": False, "calls": ["jump:5:false:"]})
