"""The folded deleted message, its opened header, and the "Deleted only" list.

A deleted message folds to one line ("Deleted photo · 21:59 · Show") until the
reader opens it, a jump lands on it or a search finds it. Opened, its header
says "Deleted · 21:59" with the time the archive noticed. The chat menu opens
the chat search in a "Deleted only" mode that lists the chat's deletions.

The helpers are EXECUTED under node, lifted verbatim from the template with the
real vendored moment, so a broken rule fails here and not only in a browser.
"""

import json
import unittest
from pathlib import Path

from test_frontend_bootstrap import (
    _PRODUCER_PRELUDE,
    INDEX_HTML,
    NODE,
    _run_setup_program,
    _setup_slice,
)

VENDOR = Path(__file__).resolve().parents[1] / "telegram_archive" / "web" / "static" / "vendor"

# The real moment and moment-timezone, loaded in a sandbox: their UMD wrappers
# would otherwise ask node for a "moment" package.
_MOMENT = f"""
const vm = require('node:vm')
const fs = require('node:fs')
const momentBox = vm.createContext({{}})
vm.runInContext(fs.readFileSync({json.dumps(str(VENDOR / "moment-2.29.4.min.js"))}, 'utf8'), momentBox)
vm.runInContext(fs.readFileSync({json.dumps(str(VENDOR / "moment-timezone-with-data-1970-2030-0.5.43.min.js"))}, 'utf8'), momentBox)
const moment = momentBox.moment
"""

# What the folding helpers close over. ALBUMS maps a grouped_id to its rows.
_FOLD_PRELUDE = (
    _MOMENT
    + """
const viewerTimezone = { value: 'UTC' }
const selectedChat = { value: { id: 7, ref: 'r7' } }
const messageSearchQuery = { value: '' }
const deletedOnly = { value: false }
const editedOnly = { value: false }
const openDeletedMessages = { value: {} }
let ALBUMS = {}
const getAlbumForMessage = (msg) => ALBUMS[msg.raw_data?.grouped_id] || null
"""
)

_FOLD_DECLARATIONS = (
    "const messageFilterOn = () =>",
    "const formatStamp = (iso, withZone = false) =>",
    "const isAlbumPicture = (m) =>",
    "const deletedAlbumMember = (msg) =>",
    "const isBubbleDeleted = (msg) =>",
    "const deletedFoldable = (msg) =>",
    "const deletedKey = (msg) =>",
    "const isDeletedOpen = (msg) =>",
    "const isDeletedFolded = (msg) =>",
    "const DELETED_KIND = {",
    "const deletedKindLabel = (msg) =>",
    "const deletedStamp = (msg) =>",
    "const deletedHeadText = (msg) =>",
    "const deletedNoticedTitle = (msg) =>",
)


def _row(msg_id: int, *, date: str = "2026-09-29T08:34:00", deleted_at: str | None = None, **extra) -> dict:
    row = {"id": msg_id, "chat_id": 7, "date": date, "is_deleted": 0 if deleted_at is None else 1}
    row["deleted_at"] = deleted_at
    row.update(extra)
    return row


class TestTheFoldedPill(unittest.TestCase):
    """The pill is a real button in the bubble's place, on the bubble's side."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_the_pill_replaces_the_bubble_and_says_what_and_when(self) -> None:
        start = self.html.index('<button v-if="isDeletedFolded(msg)" type="button"')
        pill = self.html[start : self.html.index("</button>", start)]
        self.assertIn(":class=\"isOwnMessage(msg) ? 'bubble-out' : 'bubble-in'\"", pill)
        self.assertIn('aria-expanded="false"', pill)
        self.assertIn('@click.stop="openDeletedMessage(msg)"', pill)
        self.assertIn(':title="deletedNoticedTitle(msg)"', pill)
        self.assertIn("{{ deletedKindLabel(msg) }}", pill)
        self.assertIn("{{ deletedStamp(msg) }}", pill)
        self.assertIn('<span class="deleted-pill-show">Show</span>', pill)
        self.assertIn('<path d="M3 6h18"/>', pill)  # the trash
        # The bubble renders only when the pill does not: the same row, never both.
        after = self.html[self.html.index("</button>", start) + len("</button>") :]
        self.assertTrue(after.lstrip().startswith("<!-- bubble-in / bubble-out"))
        self.assertIn('<div v-else class="message-bubble"', after[:400])

    def test_the_pill_is_tinted_and_shows_its_focus(self) -> None:
        rule = self.html[self.html.index("        .deleted-pill {") :]
        rule = rule[: rule.index("}")]
        self.assertIn("background-image: linear-gradient(var(--tg-deleted-wash), var(--tg-deleted-wash));", rule)
        self.assertIn("rgb(var(--tg-deleted-fg) / 0.45)", rule)
        focus = self.html[self.html.index(".deleted-pill:focus-visible {") :]
        focus = focus[: focus.index("}")]
        self.assertIn("outline: 2px solid rgb(var(--tg-focus-pane));", focus)
        for side, fill in (("in", "--tg-other"), ("out", "--tg-own")):
            block = self.html[self.html.index(f".deleted-pill.bubble-{side} {{") :]
            block = block[: block.index("}")]
            self.assertIn(f"--tg-bubble-fill: var({fill});", block)
            self.assertIn(f"--tg-deleted-fg: var(--tg-deleted-fg-{side});", block)
            self.assertIn(f"--tg-meta: var(--tg-meta-{side});", block)
            self.assertIn(f"--tg-quote: var(--tg-quote-{side});", block)

    def test_the_opened_bubble_carries_its_header_and_hide(self) -> None:
        # On the name line, and on a line of its own when there is none.
        self.assertEqual(self.html.count('<span class="deleted-head" :title="deletedNoticedTitle(msg)">'), 2)
        self.assertEqual(
            self.html.count(
                '<button type="button" class="deleted-hide" aria-expanded="true" '
                '@click.stop="foldDeletedMessage(msg)">Hide</button>'
            ),
            2,
        )
        self.assertIn('<div v-else-if="isBubbleDeleted(msg)" class="deleted-head-row">', self.html)
        # In the "Deleted only" list the same place offers the way to the chat.
        self.assertEqual(
            self.html.count(
                '<button type="button" class="deleted-hide" @click.stop="showDeletedInChat(msg)">Show in chat</button>'
            ),
            2,
        )
        # The time in the meta row is the send time: no deleted mark sits beside it.
        meta = self.html[self.html.index("<!-- Marks first, then the time, in Telegram's order:") :]
        meta = meta[: meta.index("</span>\n                                    </div>")]
        self.assertIn("formatTime(msg.date)", meta)
        self.assertNotIn("meta-deleted", meta)
        self.assertNotIn("isBubbleDeleted", meta)


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheFoldingRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, expression: str, prelude: str = "") -> dict:
        return _run_setup_program(
            self.html, _FOLD_DECLARATIONS, _FOLD_PRELUDE + prelude, f"console.log(JSON.stringify({expression}))"
        )

    def test_the_kind_names_what_was_deleted(self) -> None:
        kinds = {
            "photo": "Deleted photo",
            "video": "Deleted video",
            "video_note": "Deleted video",
            "voice": "Deleted voice message",
            "sticker": "Deleted sticker",
            "document": "Deleted file",
            "audio": "Deleted file",
            "poll": "Deleted message",
        }
        rows = [_row(i, deleted_at="2026-09-29T08:38:00", media={"type": kind}) for i, kind in enumerate(kinds)]
        rows.append(_row(99, deleted_at="2026-09-29T08:38:00"))
        out = self._run(f"{json.dumps(rows)}.map(deletedKindLabel)")
        self.assertEqual(out, [*kinds.values(), "Deleted message"])

    def test_the_time_is_when_the_archive_noticed_with_the_day_when_it_differs(self) -> None:
        same_day = _row(1, deleted_at="2026-09-29T08:38:00")
        later_day = _row(2, deleted_at="2026-09-30T21:59:00")
        later_year = _row(3, date="2025-12-31T23:00:00", deleted_at="2026-01-02T09:05:00")
        unknown = _row(4, deleted_at=None)
        unknown["is_deleted"] = 1
        rows = json.dumps([same_day, later_day, later_year, unknown])
        out = self._run(
            f"{{ stamps: {rows}.map(deletedStamp), heads: {rows}.map(deletedHeadText),"
            f" titles: {rows}.map(deletedNoticedTitle) }}"
        )
        self.assertEqual(out["stamps"], ["08:38", "Sep 30, 21:59", "Jan 2, 2026, 09:05", ""])
        self.assertEqual(
            out["heads"], ["Deleted · 08:38", "Deleted · Sep 30, 21:59", "Deleted · Jan 2, 2026, 09:05", "Deleted"]
        )
        self.assertEqual(
            out["titles"][1],
            "Deleted in Telegram. The archive noticed on September 30, 2026 at 21:59. "
            "Telegram does not say when a message was deleted.",
        )
        self.assertIn("The archive kept it.", out["titles"][3])

    def test_the_day_follows_the_viewer_timezone(self) -> None:
        # 22:30 UTC on the 29th is already the 30th in Madrid; the send at 08:34
        # UTC is still the 29th there, so the date shows.
        row = _row(1, deleted_at="2026-09-29T22:30:00")
        out = self._run(
            f"[deletedStamp({json.dumps(row)}), (viewerTimezone.value = 'Europe/Madrid', deletedStamp({json.dumps(row)}))]"
        )
        self.assertEqual(out, ["22:30", "Sep 30, 00:30"])

    def test_folded_until_opened_and_open_while_the_list_is_filtered(self) -> None:
        deleted = json.dumps(_row(5, deleted_at="2026-09-29T08:38:00"))
        live = json.dumps(_row(6))
        out = self._run(
            f"""(() => {{
    const msg = {deleted}
    const res = {{ live: isDeletedFolded({live}), folded: isDeletedFolded(msg) }}
    openDeletedMessages.value = {{ [deletedKey(msg)]: true }}
    res.opened = isDeletedFolded(msg)
    openDeletedMessages.value = {{}}
    messageSearchQuery.value = 'north'
    res.searching = isDeletedFolded(msg)
    messageSearchQuery.value = ''
    deletedOnly.value = true
    res.deletedOnly = isDeletedFolded(msg)
    deletedOnly.value = false
    res.back = isDeletedFolded(msg)
    // The key names the chat too: the same id in another chat is another message.
    openDeletedMessages.value = {{ '8:5': true }}
    res.otherChat = isDeletedFolded(msg)
    return res
}})()"""
        )
        self.assertEqual(
            out,
            {
                "live": False,
                "folded": True,
                "opened": False,
                "searching": False,
                "deletedOnly": False,
                "back": True,
                "otherChat": True,
            },
        )

    def test_an_album_folds_only_when_all_of_it_was_deleted(self) -> None:
        def picture(msg_id: int, deleted: bool) -> dict:
            row = _row(msg_id, deleted_at="2026-09-29T08:38:00" if deleted else None)
            row.update(raw_data={"grouped_id": 42}, media={"type": "photo"})
            return row

        partial = [picture(1, True), picture(2, False)]
        whole = [picture(3, True), picture(4, True)]
        out = self._run(
            f"""(() => {{
    const partial = {json.dumps(partial)}, whole = {json.dumps(whole)}
    ALBUMS = {{ 42: partial }}
    const p = {{ marked: isBubbleDeleted(partial[1]), folds: deletedFoldable(partial[1]), head: deletedHeadText(partial[1]) }}
    ALBUMS = {{ 42: whole }}
    const w = {{ folds: deletedFoldable(whole[0]), kind: deletedKindLabel(whole[0]), head: deletedHeadText(whole[0]) }}
    return {{ p, w }}
}})()"""
        )
        # One picture gone: the live pictures stay in view, and the header says which.
        self.assertEqual(out["p"], {"marked": True, "folds": False, "head": "Picture deleted · 08:38"})
        self.assertEqual(out["w"], {"folds": True, "kind": "Deleted album", "head": "Deleted · 08:38"})

    def test_show_and_hide_keep_the_state_for_the_session(self) -> None:
        prelude = """
const FOCUSED = []
const messagesContainer = { value: { querySelector: () => ({ querySelector: (sel) => ({ focus: () => FOCUSED.push(sel) }) }) } }
const nextTick = (fn) => { if (fn) fn(); return Promise.resolve() }
"""
        declarations = (
            *_FOLD_DECLARATIONS,
            "const focusDeletedControl = (msg, selector) =>",
            "const openDeletedMessage = (msg) =>",
            "const foldDeletedMessage = (msg) =>",
        )
        deleted = json.dumps(_row(5, deleted_at="2026-09-29T08:38:00"))
        other = json.dumps(_row(9, deleted_at="2026-09-29T08:40:00"))
        out = _run_setup_program(
            self.html,
            declarations,
            _FOLD_PRELUDE + prelude,
            f"""const msg = {deleted}, other = {other}
openDeletedMessage(msg)
openDeletedMessage(other)
const afterShow = [isDeletedFolded(msg), isDeletedFolded(other)]
foldDeletedMessage(msg)
console.log(JSON.stringify({{ afterShow, afterHide: [isDeletedFolded(msg), isDeletedFolded(other)], focused: FOCUSED }}))""",
        )
        self.assertEqual(out["afterShow"], [False, False])
        self.assertEqual(out["afterHide"], [True, False])
        # Focus moves to the control that replaced the pressed one.
        self.assertEqual(out["focused"], [".deleted-hide", ".deleted-hide", ".deleted-pill"])


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestAJumpOpensItsTarget(unittest.TestCase):
    """Every jump (a reply quote, a search hit, a link, Show in chat) ends in
    scrollToMessage, so the target opens there, before the scroll measures it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _jump(self, rows: list[dict], target: int, rendered: int) -> dict:
        prelude = f"""
const ROWS = {json.dumps(rows)}
const sortedMessages = {{ value: ROWS }}
const SCROLLED = []
const el = {{
    dataset: {{ msgId: '{rendered}' }},
    scrollIntoView: () => SCROLLED.push(Object.keys(openDeletedMessages.value)),
    classList: {{ add: () => {{}}, remove: () => {{}} }},
    offsetWidth: 0,
}}
const findMessageElement = () => el
const nextTick = (fn) => Promise.resolve().then(fn)
const window = {{ matchMedia: () => ({{ matches: true }}) }}
"""
        return _run_setup_program(
            self.html,
            (*_FOLD_DECLARATIONS, "const scrollToMessage = (msgId) =>"),
            _FOLD_PRELUDE + prelude,
            f"""scrollToMessage({target})
setTimeout(() => console.log(JSON.stringify({{ open: Object.keys(openDeletedMessages.value), scrolled: SCROLLED }})), 20)""",
        )

    def test_a_folded_target_opens_before_the_scroll(self) -> None:
        out = self._jump([_row(5, deleted_at="2026-09-29T08:38:00")], 5, 5)
        self.assertEqual(out, {"open": ["7:5"], "scrolled": [["7:5"]]})

    def test_a_live_target_changes_nothing(self) -> None:
        out = self._jump([_row(6)], 6, 6)
        self.assertEqual(out, {"open": [], "scrolled": [[]]})

    def test_an_album_member_opens_the_row_that_draws_the_album(self) -> None:
        rows = []
        for msg_id in (11, 12):
            row = _row(msg_id, deleted_at="2026-09-29T08:38:00")
            row.update(raw_data={"grouped_id": 77}, media={"type": "photo"})
            rows.append(row)
        out = _run_setup_program(
            self.html,
            (*_FOLD_DECLARATIONS, "const scrollToMessage = (msgId) =>"),
            _FOLD_PRELUDE
            + f"""
ALBUMS = {{ 77: {json.dumps(rows)} }}
const sortedMessages = {{ value: ALBUMS[77] }}
const el = {{ dataset: {{ msgId: '11' }}, scrollIntoView: () => {{}}, classList: {{ add() {{}}, remove() {{}} }} }}
const findMessageElement = () => el
const nextTick = (fn) => Promise.resolve().then(fn)
const window = {{ matchMedia: () => ({{ matches: false }}) }}
""",
            """scrollToMessage(12)
setTimeout(() => console.log(JSON.stringify(Object.keys(openDeletedMessages.value))), 20)""",
        )
        self.assertEqual(out, ["7:11"])


class TestTheDeletedOnlyMode(unittest.TestCase):
    """The chat menu opens the chat search in a "Deleted only" mode."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_the_menu_item_shows_the_count_and_hides_at_zero(self) -> None:
        menu = self.html[
            self.html.index(
                '<div v-if="!noDownload || chatDeletedCount > 0 || chatEditedCount > 0" class="relative phone-hide">'
            ) :
        ]
        menu = menu[: menu.index("<!-- Info panel toggle")]
        item = menu[menu.index('<button v-if="chatDeletedCount !== 0" type="button" @click="openDeletedOnly"') :]
        item = item[: item.index("</button>")]
        self.assertIn('<span class="flex-1 text-left">Deleted messages</span>', item)
        self.assertIn(
            '<span v-if="chatDeletedCount" class="info-action-count">{{ formatCount(chatDeletedCount) }}</span>', item
        )
        # It comes first, and a login that may not download keeps it without export.
        self.assertLess(menu.index("openDeletedOnly"), menu.index("exportChat()"))
        self.assertIn('<button v-if="!noDownload" type="button" @click="chatMenuOpen = false; exportChat()"', menu)
        self.assertIn('v-if="transcriptionState.enabled && !noDownload"', menu)
        # No chip in the header: the only way in is the menu (and the info panel's row).
        header = self.html[self.html.index('<div class="flex items-center gap-0.5 min-w-0">') :]
        header = header[: header.index("<!-- Media Gallery Button -->")]
        self.assertNotIn("openDeletedOnly", header)
        self.assertNotIn("chatDeletedCount", header)

    def test_the_count_is_the_chat_figure_when_known(self) -> None:
        count = _setup_slice(self.html, "const chatDeletedCount = computed(() =>")
        self.assertIn("chatStats.value?.deleted_messages", count)
        self.assertIn("return count == null ? null : Number(count) || 0", count)

    def test_the_mode_has_a_visible_way_out(self) -> None:
        bar = self.html[self.html.index('<div v-if="deletedOnly && !showMediaGallery" class="deleted-filter-bar"') :]
        bar = bar[: bar.index("</div>")]
        self.assertIn('aria-pressed="true" @click="closeDeletedOnly"', bar)
        self.assertIn('aria-label="Deleted only. Remove this filter"', bar)
        self.assertIn('class="deleted-filter-x"', bar)
        # Closing the search, or opening another chat or topic, leaves the mode too.
        close = _setup_slice(self.html, "const closeChatSearch = async () =>")
        self.assertIn("deletedOnly.value = false", close)
        for declaration in ("const selectChat = async (chat, options = {}) =>", "const selectTopic = async ("):
            body = _setup_slice(self.html, declaration)
            self.assertIn("deletedOnly.value = false", body, declaration)
        self.assertIn("'No deleted messages in this chat'", self.html)

    @unittest.skipUnless(NODE, "node is required to execute the helpers")
    def test_the_list_asks_the_server_for_deletions_only(self) -> None:
        epilogue = """
(async () => {
    const shape = async (search, deleted) => {
        messageSearchQuery.value = search
        deletedOnly.value = deleted
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
        deleted: await shape('', true),
        narrowed: await shape('north', true),
    }))
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const messageIdKey = (msg) =>",
                "const upsertMessages = (incomingMessages, ",
                "const resetMessagePagination = () =>",
                "const loadMessages = async () =>",
            ),
            _PRODUCER_PRELUDE,
            epilogue,
        )
        self.assertNotIn("deleted_only", out["plain"]["url"])
        self.assertTrue(out["plain"]["contiguous"])
        self.assertIn("offset=0", out["deleted"]["url"])
        self.assertIn("&deleted_only=true", out["deleted"]["url"])
        self.assertNotIn("search=", out["deleted"]["url"])
        self.assertFalse(out["deleted"]["contiguous"])
        self.assertIn("search=north", out["narrowed"]["url"])
        self.assertIn("&deleted_only=true", out["narrowed"]["url"])

    @unittest.skipUnless(NODE, "node is required to execute the helpers")
    def test_open_and_show_in_chat(self) -> None:
        prelude = """
const chatMenuOpen = { value: true }
const deletedOnly = { value: false }
const editedOnly = { value: true }
const messageSearchQuery = { value: 'north' }
const chatSearchOpen = { value: false }
let messageSearchDebounceTimer = null
const CALLS = []
const closePinnedView = () => CALLS.push('closePinned')
const searchMessages = async () => CALLS.push(`search:${deletedOnly.value}`)
const openChatSearch = async () => { chatSearchOpen.value = true; CALLS.push('openSearch') }
const loadMessagesAroundId = async (id) => CALLS.push(`jump:${id}:${deletedOnly.value}:${messageSearchQuery.value}`)
"""
        out = _run_setup_program(
            self.html,
            ("const openDeletedOnly = async () =>", "const showDeletedInChat = async (msg) =>"),
            prelude,
            """(async () => {
    await openDeletedOnly()
    const opened = { deletedOnly: deletedOnly.value, editedOnly: editedOnly.value, menu: chatMenuOpen.value, search: chatSearchOpen.value, calls: CALLS.slice() }
    CALLS.length = 0
    await showDeletedInChat({ id: 5 })
    console.log(JSON.stringify({ opened, shown: { deletedOnly: deletedOnly.value, editedOnly: editedOnly.value, search: chatSearchOpen.value, calls: CALLS } }))
})();""",
        )
        self.assertEqual(
            out["opened"],
            {
                "deletedOnly": True,
                # The two modes never hold at once: opening one leaves the other.
                "editedOnly": False,
                "menu": False,
                "search": True,
                "calls": ["closePinned", "search:true", "openSearch"],
            },
        )
        self.assertEqual(
            out["shown"], {"deletedOnly": False, "editedOnly": False, "search": False, "calls": ["jump:5:false:"]}
        )
