"""Regression tests for frontend boot-time failures."""

import inspect
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from telegram_archive.message_utils import service_action_type, service_message_text

INDEX_HTML = Path(__file__).resolve().parents[1] / "telegram_archive" / "web" / "templates" / "index.html"

NODE = shutil.which("node")

try:  # Telethon is archived; keep the cross-check optional rather than a hard dep.
    from telethon.tl import types as telethon_types
except Exception:  # pragma: no cover - exercised only where telethon is absent
    telethon_types = None


def _setup_slice(html: str, declaration: str) -> str:
    """Return one top-level ``const`` body from the root Vue ``setup()``.

    Setup-scope declarations are indented 16 spaces, so the next such line is
    the end of the current one; nested declarations are indented deeper and do
    not terminate the slice.
    """
    start = html.index(declaration)
    return html[start : html.index("\n                const ", start + len(declaration))]


def _setup_function(html: str, declaration: str) -> str:
    """Return exactly one setup-scope function, ended by its matching brace.

    ``_setup_slice`` stops at the next 16-space ``const``, which OVERSHOOTS when
    what follows is a ``let`` or a bare ``watch(...)`` call — the extra code then
    runs inside the test program and fails on identifiers the test never stubbed.
    Brace matching is exact here because the template's function bodies keep
    their braces balanced (template literals included).
    """
    start = html.index(declaration)
    index = html.index("{", start)
    depth = 0
    while True:
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : index + 1]
        index += 1


def _run_setup_program(html: str, declarations: tuple[str, ...], prelude: str, epilogue: str) -> Any:
    """EXECUTE real setup-scope declarations under a stubbed environment.

    String assertions cannot tell a working helper from a broken one, and this
    repo has shipped green-CI regressions on exactly that. The declarations are
    lifted VERBATIM out of the template — no DOM, no Vue, no browser — with
    ``prelude`` supplying whatever they close over (refs, module-level
    counters, fetch) and ``epilogue`` driving them and printing one JSON line.
    """
    parts = [prelude]
    for declaration in declarations:
        parts.append(_setup_slice(html, declaration))
    program = "\n".join(parts) + "\n" + epilogue + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "helpers.js"
        script.write_text(program, encoding="utf-8")
        result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + "\n----\n" + program
    return json.loads(result.stdout)


def _run_setup_helpers(html: str, declarations: tuple[str, ...], expression: str, prelude: str = "") -> Any:
    """EXECUTE a few setup-scope helpers and return the JSON value of ``expression``."""
    return _run_setup_program(html, declarations, prelude, f"console.log(JSON.stringify({expression}))")


def test_media_gallery_refs_are_initialized_before_watcher():
    """The root Vue setup must not touch media gallery refs before their const declarations."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    state_index = html.index("const showMediaGallery = ref(false)")
    watcher_index = html.index("watch(showMediaGallery")

    assert state_index < watcher_index


def test_media_gallery_close_reconnects_message_observer():
    """Closing the gallery rebuilds message DOM and must reconnect infinite scroll."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    watcher_start = html.index("watch(showMediaGallery")
    watcher_body = html[watcher_start : html.index("const filteredChats = computed", watcher_start)]

    assert "watch(showMediaGallery, async (val) =>" in watcher_body
    assert "} else {" in watcher_body
    assert "await nextTick()" in watcher_body
    assert "setupMessagesScrollObserver()" in watcher_body
    assert watcher_body.index("await nextTick()") < watcher_body.rindex("setupMessagesScrollObserver()")


class TestSenderPresentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_message_spacing_changes_at_sender_boundaries(self) -> None:
        """Consecutive messages should be tighter than transitions between senders."""
        self.assertNotIn("gap-1 messages-scroll", self.html)
        self.assertIn("message-run-continue", self.html)
        self.assertIn("message-run-break", self.html)
        self.assertIn("const getSenderRunKey = (msg) =>", self.html)
        self.assertIn("const isSenderBreak = (index) =>", self.html)
        # Run positions are keyed once per list change, not per row per render.
        self.assertIn("const runKeys = computed(() => sortedMessages.value.map(getRunKey))", self.html)
        # A pause of more than 15 minutes splits a run before its positions are read.
        self.assertIn("computeRunPositions(runSegmentKeys.value)", self.html)
        self.assertIn("runSegmentKeys.value,", self.html)
        self.assertIn("const showSenderName = (index) => runPositions.value[index]?.start ?? true", self.html)
        self.assertIn("const isRunEnd = (index) => runPositions.value[index]?.end ?? true", self.html)
        self.assertIn("return index > 0 && isRunEnd(index)", self.html)
        self.assertIn("isSenderBreak,", self.html)

    @unittest.skipUnless(NODE, "node is required to run the run-position helper")
    def test_runs_skip_unpainted_rows_and_end_at_a_break(self) -> None:
        """Index 0 is the newest row. A null key (an album duplicate) is skipped,
        so the album row keeps the run's top and its sender's avatar at the bottom;
        a different key (another sender, another day, a service pill) ends a run."""
        positions = _run_setup_program(
            self.html,
            ("const computeRunPositions = (keys) =>",),
            "",
            "console.log(JSON.stringify(computeRunPositions(['a', 'a', null, 'a', 'b', 'svc', 'b'])))",
        )
        self.assertEqual(
            [(p["start"], p["end"]) for p in positions],
            [
                (False, True),  # newest 'a': bottom of the run, where the tail and avatar go
                (False, False),
                (True, True),  # unpainted: no run
                (True, False),  # oldest 'a': top of the run, past the skipped row
                (True, True),
                (True, True),  # the service pill
                (True, True),  # 'b' again, but the pill ended its run
            ],
        )

    @unittest.skipUnless(NODE, "node is required to run the run helpers")
    def test_a_pause_of_more_than_15_minutes_starts_a_new_run(self) -> None:
        """Index 0 is the newest row. The same sender after a pause of more than
        15 minutes starts a new run, so its name, avatar and tail come back;
        exactly 15 minutes keeps the run, and an unpainted row is skipped."""
        positions = _run_setup_program(
            self.html,
            ("const computeRunPositions = (keys) =>", "const splitRunsAtPauses = (keys, times) =>"),
            "",
            "const minute = 60 * 1000;\n"
            "const keys = ['a', 'a', null, 'a', 'a', 'b'];\n"
            "const times = [61, 60, 45, 45, 29, 0].map((m) => m * minute);\n"
            "console.log(JSON.stringify(computeRunPositions(splitRunsAtPauses(keys, times))))",
        )
        self.assertEqual(
            [(p["start"], p["end"]) for p in positions],
            [
                (False, True),  # 61: one minute on, the bottom of the run
                (False, False),  # 60: exactly 15 minutes after 45 keeps the run
                (True, True),  # unpainted: skipped
                (True, False),  # 45: 16 minutes after 29, a new run starts
                (True, True),  # 29: a run of one
                (True, True),  # another sender
            ],
        )

    @unittest.skipUnless(NODE, "node is required to run the run-position helpers")
    def test_a_run_that_opens_frameless_names_its_sender_on_the_next_bubble(self) -> None:
        """A sticker or a round video stands with no bubble to hold a name, so the
        name moves to the run's first framed bubble; a run of only frameless rows
        shows none, and an unpainted row never takes it."""
        names = _run_setup_program(
            self.html,
            ("const computeRunPositions = (keys) =>", "const computeNameRows = (keys, positions, isFramelessAt) =>"),
            "",
            "const keys = ['a', 'a', null, 'a', 'b', 'c', 'c'];\n"
            "const frameless = new Set([3, 4, 6]);\n"
            "console.log(JSON.stringify(computeNameRows(keys, computeRunPositions(keys), (i) => frameless.has(i))))",
        )
        # Index 0 is the newest row. Run 'a' opens with a sticker (3), skips the
        # unpainted row (2), and names its sender on row 1. Run 'b' is one
        # sticker: no name. Run 'c' opens frameless (6): the name goes on 5.
        self.assertEqual(names, [False, True, False, False, False, True, False])

    def test_the_name_template_reads_the_name_rows(self) -> None:
        self.assertIn("const showNameAt = (index) => nameRows.value[index] ?? true", self.html)
        self.assertIn('<span v-if="!isOwnMessage(msg) && isGroup && showNameAt(index)" dir="auto"', self.html)
        self.assertNotIn('isFramelessMedia(msg, index)"', self.html)

    def test_sender_snapshot_precedes_current_profile_name(self) -> None:
        """Archived names must not be rewritten in the UI by mutable user profiles."""
        start = self.html.index("const getSenderName = (msg) =>")
        end = self.html.index("const getCurrentSenderName = (msg) =>", start)
        body = self.html[start:end]

        self.assertIn("msg.raw_data.post_author", body)
        self.assertIn("if (msg.sender_name) return msg.sender_name", body)
        self.assertLess(body.index("msg.raw_data.post_author"), body.index("msg.sender_name"))
        self.assertLess(body.index("msg.sender_name"), body.index("getCurrentSenderName(msg)"))

    def test_sender_avatar_opens_accessible_details_dialog(self) -> None:
        """The run-start avatar exposes archived/current names and the numeric ID."""
        self.assertIn('@click="openSenderInfo(msg, $event)"', self.html)
        self.assertIn('role="dialog" aria-modal="true" aria-labelledby="sender-info-title"', self.html)
        self.assertIn("senderInfoMessage.sender_name ? 'Name when this message was sent'", self.html)
        self.assertIn("getCurrentSenderName(senderInfoMessage) ? 'Latest known name' : 'Name'", self.html)
        self.assertIn("hasDifferentCurrentSenderName(senderInfoMessage)", self.html)
        self.assertIn("senderInfoMessage.sender_id ?? 'Unknown'", self.html)
        self.assertIn("event.key === 'Escape'", self.html)
        self.assertIn("event.key !== 'Tab'", self.html)
        self.assertIn("senderInfoDialog.value.querySelectorAll", self.html)
        self.assertIn("senderInfoCloseBtn.value?.focus()", self.html)
        self.assertIn("trigger?.focus()", self.html)

    def test_imported_document_display_name_hides_storage_prefix(self) -> None:
        """Imported media IDs are storage details and should not appear in the gallery.

        The label used to be produced by stripping ``media.id + '_'`` off the
        stored filename. That only ever matched imported files, and only while
        media.id still WAS the storage id — i.e. only while #423 was unfixed.
        media.id is now the chat-free URL key, so the prefix is matched on its
        own shape, mirroring _MEDIA_STORAGE_PREFIX_RE in telegram_archive/message_utils.py so
        the visible label and the saved download name agree.
        """
        start = self.html.index("const getMediaDisplayName = (media) =>")
        end = self.html.index("const getDocumentDisplayName = (msg) =>", start)
        body = self.html[start:end]
        # The storage prefix is recognised by shape, not by the id we happen to ship.
        self.assertIn("/^(?:import_-?[0-9]+_[0-9]+_|[0-9]+_)/", body)
        self.assertNotIn("storagePrefix", body)
        self.assertIn("{{ getMediaDisplayName(item) }}", self.html)

    def test_media_display_name_matches_the_server_download_name(self) -> None:
        """One rule, two places: the viewer's label and the server's saved
        filename must strip the same prefixes, or a download lands under a name
        the gallery never showed. Asserted against the real Python function."""
        import re as _re

        from telegram_archive.message_utils import media_display_filename

        start = self.html.index("const getMediaDisplayName = (media) =>")
        end = self.html.index("const getDocumentDisplayName = (msg) =>", start)
        js_pattern = _re.search(r"name\.replace\(/\^(.+?)/, ''\)", self.html[start:end]).group(1)
        for stored in (
            "import_-1002176572213_1453_holiday.mp4",  # imported, negative chat
            "import_7654321_7_holiday.mp4",  # imported, positive chat
            "5551234_holiday.jpg",  # swept
            "holiday.jpg",  # no prefix at all
            "5551234_",  # prefix and nothing else: both sides keep the original
        ):
            with self.subTest(stored=stored):
                self.assertEqual(
                    media_display_filename(stored),
                    _re.sub("^" + js_pattern, "", stored, count=1) or stored,
                )


def test_message_versions_are_loaded_only_from_click_handler():
    """Viewer message versions are fetched lazily: from the pencil's click (or its
    peek), never with the message list."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '@click.stop="onEditMarkClick(msg)"' in html
    assert 'v-if="versionsMessage"' in html
    # The phone's sheet closes from its scrim; the side panel has none.
    assert '<div v-if="versionsMessage && versionsSheet" class="versions-scrim' in html
    assert '@click="closeVersionsPanel()" aria-hidden="true"' in html
    assert "const loadMessageVersions = async (msg) =>" in html
    assert "const toggleMessageVersions = async (msg) =>" in html
    assert "const versionsMessage = ref(null)" in html
    click = _setup_slice(html, "const onEditMarkClick = (msg) =>")
    assert "toggleMessageVersions(msg)" in click

    load_start = html.index("const loadMessageVersions = async (msg) =>")
    toggle_start = html.index("const toggleMessageVersions = async (msg) =>")
    versions_fetch = html.index("/versions?limit=100")

    assert load_start < versions_fetch < toggle_start
    assert html.count("/versions?limit=100") == 1
    assert "/edits?limit=100" not in html


def test_the_edit_mark_is_a_pencil_with_the_count():
    """The meta row marks an edit with a pencil and the number of earlier versions
    the archive kept, not the word "edited". Its accessible name says when the last
    edit was and how many versions are kept; it is a button with a 40px hit area
    that opens a dialog (the edit history)."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "fa-solid fa-pen" not in html
    assert "edited({{ msg.version_count }})" not in html
    start = html.index('<button v-if="isEditedMessage(msg)"')
    button = html[start : html.index("</button>", start)]
    assert '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>' in button  # the pencil
    assert (
        '<span v-if="editedCount(msg) > 0" class="meta-edited-count">{{ formatCount(editedCount(msg)) }}</span>'
        in button
    )
    assert "</svg>edited" not in button
    assert 'class="meta-edited hit-40 order-2' in button
    assert ':aria-label="editedLabel(msg)"' in button
    assert 'aria-haspopup="dialog"' in button
    assert ":aria-expanded=\"isVersionsPanelOpenFor(msg) ? 'true' : 'false'\"" in button
    # No native tooltip: the peek is the tooltip, and a title would draw a second one.
    assert ":title=" not in button


def test_edited_without_versions_is_still_a_button():
    """A message Telegram marks edited with no earlier text kept shows the pencil
    alone. It is still a button, so a keyboard and a touch screen reach what its
    tooltip says: the history opens on "The archive did not see an earlier version"."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<span v-else-if="msg.edit_date"' not in html
    assert "const isEditedMessage = (msg) => !!shownEditDate(msg) || editedCount(msg) > 0" in html
    assert "The archive did not see an earlier version." in html


def test_versions_can_open_without_edit_date_when_count_exists():
    """Retained versions should be clickable even when the current edit marker is absent."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<button v-if="isEditedMessage(msg)"' in html
    assert 'v-if="msg.edit_date && Number(msg.version_count) > 0"' not in html
    assert "const editedCount = (msg) => Number(msg?.version_count) || 0" in html


def test_message_versions_ignore_stale_load_responses():
    """Concurrent versions loads should not let older responses overwrite newer state."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const messageVersionsRequestSeq = ref({})" in html
    assert "const requestSeq = (messageVersionsRequestSeq.value[key] || 0) + 1" in html
    assert "setMessageVersionsRecord(messageVersionsRequestSeq, key, requestSeq)" in html
    # success, catch, AND the 503 branch must all discard stale responses
    assert html.count("messageVersionsRequestSeq.value[key] !== requestSeq") == 3
    assert "if (messageVersionsRequestSeq.value[key] === requestSeq)" in html


def test_realtime_edits_increment_visible_version_count():
    """Realtime edits keep the edited count in sync without loading versions.

    Every edit frame is an applied edit that kept a version, a formatting-only
    edit with the same text too, so the count moves on every frame (executed in
    test_edited_messages_frontend.TestTheLiveEditFrame).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    edit_case = html[html.index("case 'edit':") : html.index("case 'reaction':")]
    assert "previousText" not in edit_case
    assert "editMsg.version_count = (Number(editMsg.version_count) || 0) + 1" in edit_case


def test_message_status_badges_show_timestamps_on_hover():
    """Edited/deleted status badges should expose their event timestamps on hover."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # An edit keeps its mark beside the time, named with the edit's own time.
    # A deletion moved to the head of the bubble, with its own time: the meta
    # row carries the send time only.
    assert ':aria-label="editedLabel(msg)"' in html
    assert "isBubbleDeleted(msg) && metaOpensInfo" not in html
    assert '<span v-else-if="isBubbleDeleted(msg)" class="meta-deleted' not in html
    assert '<span class="deleted-head" :title="deletedNoticedTitle(msg)">' in html
    deleted = html[html.index("const deletedTitle = (msg) =>") :]
    deleted = deleted[: deleted.index("\n                }\n")]
    assert "formatStamp(member.deleted_at)" in deleted
    assert "The archive kept it." in deleted
    edited = _setup_slice(html, "const editedLabel = (msg) =>")
    assert "earlier ${kept === 1 ? 'version' : 'versions'} kept" in edited
    assert "The archive did not see the earlier text" in edited
    # The time's own tooltip carries the full date, the edit and the deletion.
    assert '<span v-else class="order-3" :title="messageTimeTitle(msg)">{{ formatTime(msg.date) }}</span>' in html
    start = html.index("const messageTimeTitle = (msg) =>")
    body = html[start : html.index("\n                }\n", start)]
    assert "formatMetadataTimestampTitle('Edited', shownEditDate(msg))" in body
    assert "formatMetadataTimestampTitle('Deletion noticed', msg.deleted_at)" in body
    assert "earlier ${versions === 1 ? 'version' : 'versions'} kept" in body
    assert "const formatMetadataTimestampTitle = (label, dateStr) =>" in html
    assert "`${label} ${formatStamp(dateStr)}`" in html


def test_message_versions_use_drawer_not_inline_panel():
    """Previous versions should render in the drawer so chat flow stays compact."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    drawer_index = html.index("<!-- Message Versions Drawer -->")
    lightbox_index = html.index("<!-- Lightbox Modal for Images -->")
    metadata_index = html.index("<!-- Metadata -->")

    assert metadata_index < drawer_index < lightbox_index


def test_message_versions_no_client_resort():
    """The drawer must not re-sort versions client-side; the server returns them ordered."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "messageVersionSortTime" not in html
    assert "const getMessageVersions = (msg) =>" in html

    get_start = html.index("const getMessageVersions = (msg) =>")
    next_fn = html.index("const isMessageVersionsLoading", get_start)
    get_body = html[get_start:next_fn]
    assert ".sort(" not in get_body
    assert "entry.change_hash" not in html


def test_versions_escape_closes_panel():
    """The Escape key must be wired to closeVersionsPanel via a keydown handler."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const handleVersionsKeydown = (e) =>" in html
    assert "document.addEventListener('keydown', handleVersionsKeydown)" in html
    assert "document.removeEventListener('keydown', handleVersionsKeydown)" in html

    handler_start = html.index("const handleVersionsKeydown = (e) =>")
    next_fn = html.index("const formatReactionEmoji", handler_start)
    handler_body = html[handler_start:next_fn]
    assert "Escape" in handler_body
    assert "closeVersionsPanel()" in handler_body


def test_versions_drawer_dialog_semantics():
    """The versions drawer aside must carry ARIA dialog attributes."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index('<aside v-if="versionsMessage" ref="versionsDialog"')
    aside = html[start : html.index(">", start)]

    assert 'role="dialog"' in aside
    assert 'aria-labelledby="versions-title"' in aside
    # Modal only as the phone's sheet; beside the chat it is not.
    assert ":aria-modal=\"versionsSheet ? 'true' : null\"" in aside


def test_versions_401_sets_unauthenticated():
    """A 401 from the versions endpoint must flip isAuthenticated to false."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    load_start = html.index("const loadMessageVersions = async (msg) =>")
    toggle_start = html.index("const toggleMessageVersions = async (msg) =>")
    load_body = html[load_start:toggle_start]

    assert "res.status === 401" in load_body
    assert "isAuthenticated.value = false" in load_body


def test_realtime_display_uses_api_message_order():
    """Local viewer ordering should match the API's date DESC, id DESC cursor contract."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    helper_start = html.index("const messageSortTime = (msg) =>")
    helper_body = html[helper_start : html.index("// v6.2.0: Find the topics nav entry", helper_start)]
    # #268: the ordinary view's rows are produced by messageView, alongside the
    # contiguity answer that goes with them; sortedMessages is its projection.
    sorted_start = html.index("const messageView = computed(() =>")
    sorted_body = html[sorted_start : html.index("// Group consecutive messages", sorted_start)]

    assert "moment.utc(msg.date)" in helper_body
    assert "sortTimeCache" in helper_body
    assert "messageSortTime(b) - messageSortTime(a)" in helper_body
    assert "(Number(b?.id) || 0) - (Number(a?.id) || 0)" in helper_body
    assert "rows: sortedLoadedMessages()" in sorted_body
    assert "const sortedMessages = computed(() => messageView.value.rows)" in sorted_body


def test_history_cursor_is_not_advanced_by_realtime_refresh():
    """Realtime/latest polling rows must not reset the older-history pagination cursor."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    helper_start = html.index("let oldestMessageCursor = null")
    helper_body = html[helper_start : html.index("// v6.2.0: Find the topics nav entry", helper_start)]
    refresh_start = html.index("const checkForNewMessages = async () =>")
    load_start = html.index("const loadMessages = async () =>")
    refresh_body = html[refresh_start:load_start]
    load_body = html[load_start : html.index("const searchMessages = async () =>", load_start)]

    assert "const updateOldestMessageCursor = (loadedMessages) =>" in helper_body
    assert "const cursor = oldestMessageCursor || messageCursor(oldestMessageFrom(messages.value))" in load_body
    assert "before_date=${encodeURIComponent(cursor.date)}" in load_body
    assert "before_id=${cursor.id}" in load_body
    assert "updateOldestMessageCursor(newMessages)" in load_body
    assert "updateOldestMessageCursor" not in refresh_body
    assert "reduce((oldest, msg)" not in load_body
    assert "if (chatVersion !== myVersion || messageFilterOn()) return" in refresh_body
    assert load_body.count("chatVersion !== myVersion") >= 2


def test_jump_to_message_resets_history_pagination():
    """Replacing the message window should rebuild history pagination from that window."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    jump_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    jump_body = html[jump_start : html.index("watch(showMediaGallery", jump_start)]

    assert "const myVersion = ++chatVersion" in jump_body
    assert "loading.value = true" in jump_body
    assert "messages.value = [...afterRows, ...windowRows]" in jump_body
    assert "resetMessagePagination()" in jump_body
    assert "setupMessagesScrollObserver()" in jump_body
    assert jump_body.index("messages.value = [...afterRows, ...windowRows]") < jump_body.index(
        "resetMessagePagination()"
    )
    assert jump_body.index("resetMessagePagination()") < jump_body.index("setupMessagesScrollObserver()")


def test_jump_window_suppresses_realtime_poll():
    """A jump-to-message window pauses the offset=0 poll so it can't snap to newest (#213)."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    jump_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    jump_body = html[jump_start : html.index("watch(showMediaGallery", jump_start)]
    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]
    reset_start = html.index("const resetMessagePagination = () =>")
    reset_body = html[reset_start : html.index("// Mirrors backend coalesce", reset_start)]

    assert "const viewingPinnedWindow = ref(false)" in html
    # The poll bails while a detached window is shown...
    assert "|| viewingPinnedWindow.value) return" in refresh_body
    # ...the jump sets the flag AFTER its own resetMessagePagination() — pinned
    # unless a short after-context page proved the window already reaches the tail...
    assert "viewingPinnedWindow.value = !(afterFetchComplete && afterRows.length < windowLimit)" in jump_body
    assert jump_body.index("resetMessagePagination()") < jump_body.index("viewingPinnedWindow.value = !(")
    # ...and every tail-inclusive view entry clears it via resetMessagePagination.
    assert "viewingPinnedWindow.value = false" in reset_body

    # The "scroll to latest" button must genuinely return to live from a pinned
    # window (reload the tail), not just scroll the stale window (#214 review).
    latest_start = html.index("const scrollToLatest = async () =>")
    latest_body = html[latest_start : html.index("const isOwnMessage = (msg) =>", latest_start)]
    assert "if (viewingPinnedWindow.value)" in latest_body
    assert "resetMessagePagination()" in latest_body
    assert "await loadMessages()" in latest_body
    # While pinned, scrollTop sits at 0 so the scroll-position heuristic alone
    # would hide the button — the flag must keep the exit affordance rendered.
    assert 'v-if="showScrollToBottom || unseenMessageCount > 0 || viewingPinnedWindow"' in html


def test_jump_window_fetches_context_and_scrolls_to_target():
    """The jump loads history + after-context scoped to the topic and scrolls to the target (#213)."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    jump_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    jump_body = html[jump_start : html.index("watch(showMediaGallery", jump_start)]

    # Exclusive bound keeps the target as the newest row of the history half.
    assert "before_id=${messageId + 1}" in jump_body
    assert "after_id=${messageId}" in jump_body
    # Both window fetches must carry the forum-topic scope.
    assert jump_body.count("${topicParam}") == 2
    # Target scroll goes through the shared id-anchored helper.
    assert "scrollToMessage(messageId)" in jump_body


def test_message_rows_bind_the_msg_id_anchor():
    """Both rendered row variants carry data-msg-id, and JS data-* selectors resolve.

    Guards the #213 bug class: v7.21.0 shipped a querySelector for
    [data-msg-id=...] while no element rendered the attribute, so the jump's
    scroll/highlight was dead code.
    """
    import re

    html = INDEX_HTML.read_text(encoding="utf-8")

    # service row + regular row
    assert html.count(':data-msg-id="msg.id"') == 2

    # Generic drift guard: every data-* attribute queried from JS must be
    # rendered somewhere in the template (as a static or bound attribute).
    queried = set(re.findall(r"querySelector(?:All)?\([`'\"]\[(data-[a-z-]+)", html))
    assert "data-msg-id" in queried
    for attr in queried:
        assert f":{attr}=" in html or f" {attr}=" in html, f"JS queries [{attr}] but the template never renders it"


def test_scroll_to_message_uses_id_anchor_not_positional_index():
    """scrollToMessage must resolve rows by data-msg-id, not by .message-bubble index.

    Service rows and hidden album rows make the bubble NodeList shorter than
    sortedMessages, so positional lookups scrolled to the wrong message.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "querySelectorAll('.message-bubble')" not in html

    helper_start = html.index("const findMessageElement = (msgId) =>")
    helper_body = html[helper_start : html.index("const scrollToMessage = (msgId) =>", helper_start)]
    assert '[data-msg-id="${msgId}"]' in helper_body
    # Album-hidden targets resolve to their visible first-in-album sibling.
    assert "getGroupedId" in helper_body

    scroll_start = html.index("const scrollToMessage = (msgId) =>")
    scroll_body = html[scroll_start : html.index("const openDatePicker", scroll_start)]
    assert "findMessageElement(msgId)" in scroll_body
    # Smooth unless the reader asked for reduced motion.
    assert "scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' })" in scroll_body


def test_websocket_new_message_respects_pinned_window_and_search():
    """The WS path must honor the same guards as the poll — it was the ungated snap-back writer (#213)."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    ws_start = html.index("case 'new_message':")
    ws_body = html[ws_start : html.index("case 'edit':", ws_start)]

    assert "if (viewingPinnedWindow.value || messageFilterOn())" in ws_body
    # The guard must run before the upsert/autoscroll path.
    assert ws_body.index("viewingPinnedWindow.value") < ws_body.index("upsertMessages([data.message]")
    # Desktop notifications still fire while pinned (the guard must not break out early).
    assert "showNotification(data)" in ws_body


def test_jump_to_date_routes_through_window_loader():
    """Date jumps reuse the jump-window path instead of the capped push+fill-gap machinery."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    date_start = html.index("const jumpToDate = async () =>")
    date_body = html[date_start : html.index("// Admin panel", date_start)]

    assert "await loadMessagesAroundId(" in date_body
    assert "message.id," in date_body
    # The 20-page fill-gap loop (failed for targets >1000 messages back) is gone.
    assert "fillGap" not in html
    assert "maxIterations" not in date_body


def test_realtime_polling_skips_search_results():
    """Latest-message polling should not mix unfiltered rows into search results."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]
    search_start = html.index("const searchMessages = async () =>")
    search_body = html[search_start : html.index("const handleScroll = (e) =>", search_start)]

    assert "isRefreshing || messageFilterOn()" in refresh_body
    # The "Deleted only" and "Edited only" lists are filtered views too: the same guards cover them.
    assert "const messageFilterOn = () => !!messageSearchQuery.value || deletedOnly.value || editedOnly.value" in html
    assert "chatVersion++" in search_body
    # The version bump makes an invalidated in-flight load skip its own loading=false
    # (finally sees a version mismatch), so search must reset the gate itself or a
    # second fast keystroke finds loading stuck true and bails.
    assert "loading.value = false" in search_body
    assert search_body.index("chatVersion++") < search_body.index("loading.value = false")
    assert search_body.index("loading.value = false") < search_body.index("await loadMessages()")


def test_realtime_rows_are_filtered_deduped_and_stick_to_bottom():
    """Realtime rows should match the active topic, canonicalize through polling, and keep latest view visible."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    ws_start = html.index("case 'new_message':")
    ws_body = html[ws_start : html.index("case 'edit':", ws_start)]
    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]

    assert "messageBelongsToCurrentTopic(data.message)" in ws_body
    assert "isNearMessageBottom(messagesContainer.value)" in ws_body
    assert "upsertMessages([data.message], { updateExisting: false })" in ws_body
    assert ws_body.index("const shouldStickToBottom") < ws_body.index("upsertMessages([data.message]")
    assert "upsertMessages(latestMessages)" in refresh_body
    assert "const shouldStickToBottom = isNearMessageBottom(messagesContainer.value)" in refresh_body
    assert "return !!container && container.scrollTop > -STICK_TO_BOTTOM_PX" in html
    assert "messages.value.push(data.message)" not in ws_body
    assert "messages.value.push(...newMessages)" not in refresh_body


def test_pagination_reset_called_at_all_entry_points():
    """Every view-switching entry point must reset history pagination before loading."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    topic_start = html.index("const selectTopic = async (chat, topic, options = {}) =>")
    topic_body = html[topic_start : html.index("const activeTab = computed", topic_start)]
    chat_start = html.index("const selectChat = async (chat, options = {}) =>")
    chat_body = html[chat_start : html.index("const startMessageRefresh = () =>", chat_start)]
    search_start = html.index("const searchMessages = async () =>")
    search_body = html[search_start : html.index("const handleScroll = (e) =>", search_start)]

    assert "resetMessagePagination()" in topic_body
    assert "resetMessagePagination()" in chat_body
    assert "resetMessagePagination()" in search_body


def test_chat_wallpaper_is_a_theme_token_not_a_per_render_style_read():
    """The bubble fill is a token, so no bubble reads computed style to draw itself.

    getMessageBackground runs once per bubble per render, on every poll tick.
    Reading a custom property off documentElement there forces a style
    recalculation for a value the palette fixes.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index("const getMessageBackground = (msg) =>")
    body = html[start : html.index("\n                const ", start + 10)]
    assert "getComputedStyle" not in body
    # Bubbles are opaque in every palette: a pattern or a picture never shows through text.
    assert "'rgb(var(--tg-own))'" in body
    assert "'rgb(var(--tg-other))'" in body
    assert "bubble-alpha" not in html
    # A gradient outgoing bubble (iOS Night) lays its image over the flat fill,
    # through the side alias the deleted wash also layers over.
    assert "--tg-bubble-image: var(--tg-own-image);" in html
    assert "background-image: var(--tg-bubble-image, none);" in html

    root = html[html.index("        :root {\n            --tg-font:") :]
    root = root[: root.index("\n        }")]
    assert "--viewer-chat-background: none;" in root
    assert "--viewer-chat-tint: none;" in root
    # Everything drawn over the pane sits on the service pill: dates, service
    # messages and the notes (loading, empty, start of history).
    assert "--tg-chip-bg" not in html
    assert "--tg-pane-note-opacity" not in html
    assert "background-color: var(--tg-service-bg);" in html
    assert "color: var(--tg-service-fg);" in html
    assert 'style="background: var(--tg-service-bg); color: var(--tg-service-fg);"' in html
    assert '<span class="pane-note">Nothing older in the archive</span>' in html
    # Nothing may keep the hardcoded fills those tokens replaced.
    assert "background: rgba(0,0,0,0.3)" not in html
    assert "background-color: rgb(var(--tg-sidebar) / 0.85)" not in html
    # The server's block is its own rule after every palette. `:root[data-theme]`
    # ties with a palette block on specificity and wins by order, so the
    # operator's wallpaper replaces the palette's own under every theme.
    assert html.count("__VIEWER_CHAT_BACKGROUND__") == 1
    injection = html.index(
        "        :root,\n        :root[data-theme] {\n            __VIEWER_CHAT_BACKGROUND__\n        }"
    )
    assert html.rindex(':root[data-theme="') < injection

    # The pane paints the operator's tint and picture over the palette's pattern
    # and gradient; the first two are none until the server says otherwise.
    pane_start = html.index("        .messages-scroll,\n        .chat-wallpaper {")
    pane = html[pane_start : html.index("\n        }", pane_start)]
    assert (
        "background-image: var(--viewer-chat-tint), var(--viewer-chat-background), var(--tg-wall-pattern), var(--tg-wall-gradient);"
        in pane
    )
    # The gradient token holds several layers: a short size or repeat list would
    # repeat itself and tile the gradient like the pattern.
    for prop in ("background-size", "background-repeat", "background-position"):
        values = re.search(prop + r": ([^;]+);", pane).group(1).split(", ")
        assert len(values) >= 7, prop
    # `fixed` repaints the whole layer per scroll frame in mobile Safari, and this
    # pane is the viewport for its own scrolling, so the default already holds it still.
    assert "background-attachment" not in pane


def test_topic_filter_mirrors_backend_default():
    """The viewer's topic filter must mirror the backend's General-topic coalesce default."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    belongs_start = html.index("const messageBelongsToCurrentTopic = (msg) =>")
    belongs_body = html[belongs_start : html.index("const upsertMessages", belongs_start)]

    assert "reply_to_top_id ?? GENERAL_TOPIC_ID" in belongs_body
    assert "const GENERAL_TOPIC_ID = 1" in html
    assert "const topicId = activeTopicId()" in belongs_body
    assert "currentNav.value" not in belongs_body


def test_load_messages_handles_auth_expiry():
    """A 401 from the messages endpoint must surface the login screen, and history retries must be capped."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    load_start = html.index("const loadMessages = async () =>")
    load_body = html[load_start : html.index("const searchMessages = async () =>", load_start)]
    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]

    assert "res.status === 401" in load_body
    assert "isAuthenticated.value = false" in load_body
    assert "loadFailureStreak" in load_body
    assert "res.status === 401" in refresh_body
    assert "isAuthenticated.value = false" in refresh_body


def test_poll_deletion_pass_is_range_bounded():
    """Polling must not treat rows outside the server's returned window as deleted."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]

    assert "const serverOldest = oldestMessageFrom(latestMessages)" in refresh_body
    assert "compareMessagesDesc(m, serverOldest) <= 0" in refresh_body


def test_gallery_close_restores_reading_position_and_focus():
    """A plain gallery close must return the user to their scroll position and focus."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    watcher_start = html.index("watch(showMediaGallery")
    watcher_body = html[watcher_start : html.index("const filteredChats = computed", watcher_start)]
    jump_start = html.index("const jumpToMessage = async (item) =>")
    jump_body = html[jump_start : html.index("const formatMediaDate = (dateStr) =>", jump_start)]

    assert "let galleryReturnState = null" in html
    assert "scrollTop: messagesContainer.value ? messagesContainer.value.scrollTop : 0" in watcher_body
    assert "document.activeElement instanceof HTMLElement" in watcher_body
    assert "returnState.focusElement.isConnected" in watcher_body
    # Programmatic exits reposition the view themselves and must not restore.
    assert "galleryReturnState = null" in jump_body
    # Restore happens after the observer reconnect, inside the same guarded block.
    assert watcher_body.index("setupMessagesScrollObserver()") < watcher_body.index(
        "returnState.chatRef === (selectedChat.value?.ref ?? null)"
    )


def test_toast_exists_and_is_wired_into_jump_failure_path():
    """A minimal toast must surface the jump-window failure instead of failing silently."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const toastMessage = ref(null)" in html
    assert "const showToast = (message, ms = 4000) =>" in html
    assert 'v-if="toastMessage"' in html
    assert "toastMessage," in html
    assert "showToast," in html

    jump_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    jump_body = html[jump_start : html.index("watch(showMediaGallery", jump_start)]
    assert "showToast('Could not load messages around that message')" in jump_body
    # Both the primary-fetch !res.ok branch and a thrown network error must toast.
    assert jump_body.count("showToast('Could not load messages around that message')") == 2

    chats_start = html.index("const loadChats = async (append = false) =>")
    chats_body = html[chats_start : html.index("const loadMessages = async () =>", chats_start)]
    assert "showToast('Failed to load chats')" in chats_body

    load_start = html.index("const loadMessages = async () =>")
    load_body = html[load_start : html.index("const searchMessages = async () =>", load_start)]
    assert "showToast('Failed to load messages')" in load_body

    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]
    assert "showToast(" not in refresh_body

    date_start = html.index("const jumpToDate = async () =>")
    date_body = html[date_start : html.index("// Admin panel", date_start)]
    assert "showToast('No messages found for this date')" in date_body
    assert "showToast('Failed to jump to date. Please try again.')" in date_body
    assert "alert(" not in date_body


def test_shipped_debug_logs_are_absent():
    """Debug instrumentation left over from troubleshooting must not ship."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "'>>> Loading more messages" not in html
    assert "console.log('Stats loaded:'" not in html
    assert "console.log('[DEBUG] onMounted started')" not in html
    assert "console.log('[DEBUG] Fetching /api/auth/check...')" not in html
    assert "console.log('[DEBUG] Fetch response:'" not in html
    assert "console.log('[DEBUG] Auth response data:'" not in html
    assert "console.log('[DEBUG] authRequired:'" not in html


def test_unseen_message_badge_tracks_background_arrivals():
    """Messages arriving while scrolled up must surface a count on the jump button."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    ws_start = html.index("case 'new_message':")
    ws_body = html[ws_start : html.index("case 'edit':", ws_start)]
    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]
    scroll_start = html.index("const handleScroll = (e) =>")
    scroll_body = html[scroll_start : html.index("const loadMoreMessages = () =>", scroll_start)]

    assert "unseenMessageCount.value += 1" in ws_body
    assert "unseenMessageCount.value += newMessages.length" in refresh_body
    # Cleared when the user is back near the bottom, on view entry, and on manual jump.
    assert "unseenMessageCount.value = 0" in scroll_body
    reset_start = html.index("const resetMessagePagination = () =>")
    reset_body = html[reset_start : html.index("// Mirrors backend coalesce", reset_start)]
    assert "unseenMessageCount.value = 0" in reset_body
    latest_start = html.index("const scrollToLatest = async () =>")
    latest_body = html[latest_start : html.index("const isOwnMessage = (msg) =>", latest_start)]
    assert "unseenMessageCount.value = 0" in latest_body
    # Button shows for the badge even before the distance threshold (and always
    # while a detached jump window is pinned), with an aria-label.
    assert 'v-if="showScrollToBottom || unseenMessageCount > 0 || viewingPinnedWindow"' in html
    assert "new ${unseenMessageCount === 1 ? 'message' : 'messages'}. Scroll to the latest`" in html


def test_reaction_ws_case_patches_message_reactions():
    """#219: the WS 'reaction' event replaces a loaded message's reactions in place.

    The reactions block already renders msg.reactions generically, and the 3s poll
    merges reactions via upsertMessages, so this case is the instant-update path.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    ws_start = html.index("const handleWebSocketMessage = (data) =>")
    ws_body = html[ws_start:]

    assert "case 'reaction':" in ws_body
    reaction_start = ws_body.index("case 'reaction':")
    reaction_body = ws_body[reaction_start : ws_body.index("case 'delete':", reaction_start)]
    # Same chat-scope guard as the 'edit' case (ref-addressed frames since v8.0),
    # then the frame's live set replaces the reactions array; an emoji that left
    # it moves to the reactions taken back (tests/test_removed_reactions_frontend.py).
    assert "selectedChat.value?.ref !== data.chat_ref" in reaction_body
    assert "applyLiveReactions(reactionMsg, data.reactions)" in reaction_body
    apply_start = html.index("const applyLiveReactions = (msg, reactions) =>")
    assert "msg.reactions = live" in html[apply_start : html.index("const formatReactionEmoji", apply_start)]
    # The reactions block renders the aggregate shape the server sends.
    assert 'v-for="reaction in (msg.reactions || [])"' in html


def test_detached_window_loads_newer_pages_with_independent_state():
    """Detached windows must paginate toward the live tail without touching older pagination."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'ref="loadNewerSentinel"' in html
    assert "const loadNewerSentinel = ref(null)" in html
    assert "const hasMoreNewer = ref(false)" in html
    assert "const loadingNewer = ref(false)" in html
    assert "let newestMessageId = null" in html
    assert "let messagesNewerObserver = null" in html

    loader_start = html.index("const loadNewerMessages = async () =>")
    loader_body = html[loader_start : html.index("const searchMessages = async () =>", loader_start)]
    assert "loadingNewer.value || newerLoadError.value || !hasMoreNewer.value" in loader_body
    assert "after_id=${newestMessageId}" in loader_body
    assert "${topicParam}" in loader_body
    assert loader_body.count("chatVersion !== myVersion") >= 2
    assert "upsertMessages(newMessages)" in loader_body
    assert "newestMessageId = newest.id" in loader_body

    jump_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    jump_body = html[jump_start : html.index("watch(showMediaGallery", jump_start)]
    assert "newestMessageId = newestLoadedMessageId()" in jump_body
    assert "hasMoreNewer.value = !afterFetchComplete || afterRows.length === windowLimit" in jump_body

    assert '@click="jumpToReply(msg.reply_to_msg_id)"' in html
    reply_start = html.index("const jumpToReply = async (msgId) =>")
    reply_body = html[reply_start : html.index("const calendarAvailabilityKey", reply_start)]
    assert "findMessageElement(msgId)" in reply_body
    assert "await loadMessagesAroundId(msgId)" in reply_body


def test_newer_sentinel_and_live_tail_transition_are_independent():
    """The visual-bottom observer should page newer rows, then resume realtime at the tail."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    observer_start = html.index("const setupMessagesScrollObserver = () =>")
    observer_body = html[observer_start : html.index("// Stats data", observer_start)]
    assert "messagesScrollObserver = new IntersectionObserver" in observer_body
    assert "messagesNewerObserver = new IntersectionObserver" in observer_body
    assert "loadMessages()" in observer_body
    assert "loadNewerMessages()" in observer_body
    assert "loadMoreSentinel.value" in observer_body
    assert "loadNewerSentinel.value" in observer_body

    loader_start = html.index("const loadNewerMessages = async () =>")
    loader_body = html[loader_start : html.index("const searchMessages = async () =>", loader_start)]
    assert "if (newMessages.length < limit)" in loader_body
    assert "hasMoreNewer.value = false" in loader_body
    assert "viewingPinnedWindow.value = false" in loader_body
    assert "startMessageRefresh()" in loader_body

    reset_start = html.index("const resetMessagePagination = () =>")
    reset_body = html[reset_start : html.index("// Mirrors backend coalesce", reset_start)]
    assert "hasMoreNewer.value = false" in reset_body
    assert "loadingNewer.value = false" in reset_body
    assert "newestMessageId = null" in reset_body


def test_flatpickr_month_is_static_text_not_a_native_select():
    """The calendar draws its own month header: Flatpickr's month select is off
    (monthSelectorType static), so no style for that select is left behind."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "monthSelectorType: 'static'," in html
    assert ".flatpickr-monthDropdown-months" not in html
    assert ".flatpickr-monthDropdown-month" not in html
    assert "color-scheme: dark;" in html


def test_date_picker_fetches_month_availability_and_marks_days():
    """Calendar open/month/year changes fetch availability and decorate, never disable, dates.

    A day with messages is marked in ink and a day without is greyed, but every
    day up to today stays selectable: a jump to an empty day lands on the next
    message, and a failed check leaves every day open.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const calendarAvailabilityCache = new Map()" in html
    assert "let calendarAvailabilityRequestSeq = 0" in html
    assert "const loadCalendarAvailability = async (year, month) =>" in html
    assert "/messages/dates?month=${monthKey}&timezone=${encodeURIComponent(timezone)}" in html
    assert "onOpen:" in html
    assert "onMonthChange:" in html
    assert "onYearChange:" in html
    assert "loadCalendarAvailability(instance.currentYear, instance.currentMonth)" in html
    assert "onDayCreate:" in html
    assert "calendar-available-date" in html
    # Ink for a day with messages, muted for one without: no dots, and no day
    # closed by the availability check.
    assert "calendar-availability-dot" not in html
    assert "dayElem.classList.add('calendar-empty-date')" in html
    assert "dayElem.classList.add('flatpickr-disabled'" not in html
    assert "dayElem.setAttribute('aria-label'" in html
    assert "dayElem.title =" in html
    assert "disable:" not in html[html.index("flatpickr(datePickerInput.value") : html.index("const closeDatePicker")]


def test_calendar_availability_is_topic_scoped_stale_safe_and_fail_open():
    """Availability cache writes must be scoped and stale responses ignored; failures leave days enabled."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    availability_start = html.index("const loadCalendarAvailability = async (year, month) =>")
    availability_body = html[availability_start : html.index("const openDatePicker", availability_start)]
    assert "calendarAvailabilityKey(chatRef, topicId, timezone, monthKey)" in availability_body
    assert "url += `&topic_id=${topicId}`" in availability_body
    assert availability_body.count("requestSeq !== calendarAvailabilityRequestSeq") >= 2
    assert "calendarAvailableDates.value = null" in availability_body
    assert "catch (e)" in availability_body
    assert "flatpickrInstance.redraw()" in availability_body

    assert "calendarAvailabilityCache.clear()" in html
    assert "calendarAvailabilityRequestSeq++" in html


def test_date_picker_uses_viewer_timezone_and_topic_for_date_jump():
    """Today and both date endpoints must use the viewer timezone and active topic."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    picker_start = html.index("const openDatePicker = (initialDate) =>")
    picker_body = html[picker_start : html.index("const closeDatePicker", picker_start)]
    assert "moment.tz(viewerTimezone.value).format('YYYY-MM-DD')" in picker_body
    assert "maxDate: viewerToday" in picker_body
    assert "maxDate: 'today'" not in picker_body

    jump_start = html.index("const jumpToDate = async () =>")
    jump_body = html[jump_start : html.index("// Admin panel", jump_start)]
    assert "let dateUrl =" in jump_body
    assert "dateUrl += `&topic_id=${topicIdAtStart}`" in jump_body
    assert "const topicIdAtStart = activeTopicId()" in jump_body


def test_pane_topic_scope_survives_sidebar_navigation():
    """Sidebar navigation must not silently change the topic still displayed in the message pane."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const selectedPaneTopic = ref(null)" in html
    active_start = html.index("const activeTopicId = () =>")
    active_body = html[active_start : html.index("// Contract mirror", active_start)]
    assert "selectedPaneTopic.value?.id" in active_body
    assert "currentNav.value" not in active_body

    topic_start = html.index("const selectTopic = async (chat, topic, options = {}) =>")
    topic_body = html[topic_start : html.index("const activeTab = computed", topic_start)]
    assert "selectedPaneTopic.value = topic" in topic_body
    assert topic_body.index("selectedPaneTopic.value = topic") < topic_body.index("await loadMessages()")

    chat_start = html.index("const selectChat = async (chat, options = {}) =>")
    chat_body = html[chat_start : html.index("const startMessageRefresh", chat_start)]
    assert "selectedPaneTopic.value = null" in chat_body

    back_start = html.index("const navigateBack = () =>")
    back_body = html[back_start : html.index("const loadFolders", back_start)]
    assert "selectedPaneTopic.value" not in back_body
    assert "Main panel keeps showing current topic messages" in back_body

    assert ":class=\"{'is-active': selectedPaneTopic?.id === topic.id}\"" in html
    assert '<template v-if="selectedPaneTopic?.title">' in html


def test_all_pane_requests_use_retained_topic_scope():
    """Forward/history/poll/calendar/date requests must all read the pane topic, not sidebar state."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    jump_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    jump_body = html[jump_start : html.index("watch(showMediaGallery", jump_start)]
    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]
    load_start = html.index("const loadMessages = async () =>")
    load_body = html[load_start : html.index("const loadNewerMessages", load_start)]
    newer_start = html.index("const loadNewerMessages = async () =>")
    newer_body = html[newer_start : html.index("const retryNewerMessages", newer_start)]

    assert "const topicId = activeTopicId()" in jump_body
    assert "const topicId = activeTopicId()" in refresh_body
    assert "const topicId = activeTopicId()" in load_body
    assert "const topicId = activeTopicId()" in newer_body
    assert "currentNav.value" not in jump_body
    assert "currentNav.value" not in refresh_body
    assert "currentNav.value" not in load_body

    availability_start = html.index("const loadCalendarAvailability = async (year, month) =>")
    availability_body = html[availability_start : html.index("const handleDatePickerKeydown", availability_start)]
    assert "const topicId = activeTopicId()" in availability_body
    assert "url += `&topic_id=${topicId}`" in availability_body

    by_date_start = html.index("const jumpToDate = async () =>")
    by_date_body = html[by_date_start : html.index("// Admin panel", by_date_start)]
    assert "const topicIdAtStart = activeTopicId()" in by_date_body
    assert "dateUrl += `&topic_id=${topicIdAtStart}`" in by_date_body


def test_newer_failure_pauses_observer_and_exposes_retry():
    """Forward failures must preserve the cursor/page and require an explicit retry."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const newerLoadError = ref('')" in html
    assert "let newerLoadRequestSeq = 0" in html
    assert 'v-else-if="newerLoadError"' in html
    assert '@click="retryNewerMessages"' in html
    assert "Could not load newer messages." in html

    observer_start = html.index("messagesNewerObserver = new IntersectionObserver")
    observer_body = html[observer_start : html.index("// Observe each independent edge", observer_start)]
    assert "!newerLoadError.value" in observer_body

    loader_start = html.index("const loadNewerMessages = async () =>")
    loader_body = html[loader_start : html.index("const retryNewerMessages", loader_start)]
    catch_body = loader_body[loader_body.index("} catch (e) {") : loader_body.index("} finally {")]
    assert "newerLoadError.value = 'Could not load newer messages.'" in catch_body
    assert "hasMoreNewer.value = false" not in catch_body
    assert "requestSeq === newerLoadRequestSeq" in loader_body
    assert "newerLoadError.value = ''" in loader_body

    retry_start = html.index("const retryNewerMessages = () =>")
    retry_body = html[retry_start : html.index("const searchMessages", retry_start)]
    assert "newerLoadError.value = ''" in retry_body
    assert "loadNewerMessages()" in retry_body

    reset_start = html.index("const resetMessagePagination = () =>")
    reset_body = html[reset_start : html.index("// Mirrors backend coalesce", reset_start)]
    assert "newerLoadError.value = ''" in reset_body
    assert "newerLoadRequestSeq++" in reset_body
    assert "loadingNewer.value = false" in reset_body


def test_date_picker_dialog_accessibility_and_mobile_calendar():
    """The custom calendar must be keyboard-accessible and used consistently on mobile."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert ".date-separator button {" in html
    assert '<button type="button" @click="openDatePicker(msg.date)"' in html
    assert 'role="dialog" aria-modal="true" aria-labelledby="date-picker-title"' in html
    assert 'id="date-picker-title"' in html
    # No text field and no Jump button: one click on a day jumps; Cancel closes.
    assert '<button type="button" @click="closeDatePicker()" class="tg-text-btn">Cancel</button>' in html
    assert 'aria-label="Date to jump to"' in html
    assert "disableMobile: true" in html
    # Open in the dialog under the date field, never a popup over its buttons.
    assert "inline: true," in html
    assert "appendTo: datePickerCalendarHost.value" in html
    assert '<div v-show="!calendarMonthView" ref="datePickerCalendarHost" class="date-picker-calendar"></div>' in html
    # The month's name opens the year's months, so a far year is a few clicks away.
    assert '@click="toggleCalendarMonthView()"' in html
    assert '@click="pickCalendarMonth(cell.index)"' in html

    handler_start = html.index("const handleDatePickerKeydown = (event) =>")
    handler_body = html[handler_start : html.index("const openDatePicker", handler_start)]
    assert "event.key === 'Escape'" in handler_body
    # Shift+Page Up/Down steps a year; the Tab trap is the shared one.
    assert "if (event.shiftKey) stepCalendarYear(delta)" in handler_body
    assert "cycleTabWithin(datePickerDialog.value, event)" in handler_body
    assert "event.preventDefault()" in handler_body

    open_start = html.index("const openDatePicker = (initialDate) =>")
    open_body = html[open_start : html.index("const closeDatePicker", open_start)]
    assert "document.activeElement instanceof HTMLElement" in open_body
    assert "document.addEventListener('keydown', handleDatePickerKeydown)" in open_body
    # Focus lands on the day itself, where the arrow keys move.
    assert "focusCalendarDay()" in open_body

    close_start = html.index("const closeDatePicker = (invalidateJump = true) =>")
    close_body = html[close_start : html.index("const jumpToDate", close_start)]
    assert "if (invalidateJump) dateJumpRequestSeq++" in close_body
    assert "document.removeEventListener('keydown', handleDatePickerKeydown)" in close_body
    assert "trigger?.isConnected" in close_body
    assert "trigger.focus()" in close_body
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html
    assert "@media (max-height: 700px)" in html
    assert "max-height: calc(100dvh - 2rem)" in html
    assert "overflow-y-auto" in html


def test_calendar_status_deduplicates_requests_and_fails_open_visibly():
    """Month hooks share one request and expose loading/failure without disabling dates."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const calendarAvailabilityLoading = ref(false)" in html
    assert "const calendarAvailabilityError = ref('')" in html
    assert "const calendarAvailabilityInFlight = new Set()" in html
    assert "let calendarAvailabilityActiveKey = null" in html
    assert 'v-if="calendarAvailabilityLoading" role="status" aria-live="polite"' in html
    assert 'v-else-if="calendarAvailabilityError" role="status" aria-live="polite"' in html
    assert "Could not check which days have messages" in html

    availability_start = html.index("const loadCalendarAvailability = async (year, month) =>")
    availability_body = html[availability_start : html.index("const handleDatePickerKeydown", availability_start)]
    assert "calendarAvailabilityInFlight.has(cacheKey)" in availability_body
    assert "calendarAvailabilityInFlight.add(cacheKey)" in availability_body
    assert "calendarAvailabilityInFlight.delete(cacheKey)" in availability_body
    assert "calendarAvailabilityActiveKey = cacheKey" in availability_body
    assert "calendarAvailabilityActiveKey !== cacheKey" in availability_body
    assert "calendarAvailabilityLoading.value = true" in availability_body
    assert "calendarAvailabilityLoading.value = false" in availability_body
    assert "calendarAvailabilityError.value = 'Could not check which days have messages'" in availability_body
    assert "disable:" not in html[html.index("flatpickr(datePickerInput.value") : html.index("const closeDatePicker")]


def test_empty_date_nearest_result_warns_before_navigation():
    """An undotted date resolving to another local day should explain the nearest-date jump."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    jump_start = html.index("const jumpToDate = async () =>")
    jump_body = html[jump_start : html.index("// Admin panel", jump_start)]
    assert "const selectedDateHadAvailability =" in jump_body
    assert "calendarAvailableDates.value?.has(selectedDateAtStart) === true" in jump_body
    assert (
        "const messageLocalDate = moment.utc(message.date).tz(viewerTimezone.value).format('YYYY-MM-DD')" in jump_body
    )
    assert "if (!selectedDateHadAvailability && messageLocalDate !== selectedDateAtStart)" in jump_body
    toast = "showToast(`No messages on ${selectedDateAtStart}; showing nearest message on ${messageLocalDate}.`)"
    assert toast in jump_body
    response_body = jump_body[jump_body.index("const message = await res.json()") :]
    assert response_body.index(toast) < response_body.index("closeDatePicker(false)")


def test_date_jump_latest_intent_wins_and_cancellation_propagates_to_window_load():
    """Closing or replacing a date jump must invalidate every later async stage."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "let dateJumpRequestSeq = 0" in html
    open_start = html.index("const openDatePicker = (initialDate) =>")
    open_body = html[open_start : html.index("const closeDatePicker", open_start)]
    assert "dateJumpRequestSeq++" in open_body

    jump_start = html.index("const jumpToDate = async () =>")
    jump_body = html[jump_start : html.index("// Admin panel", jump_start)]
    assert "const jumpRequestSeq = ++dateJumpRequestSeq" in jump_body
    assert jump_body.count("jumpRequestSeq !== dateJumpRequestSeq") >= 4
    assert "closeDatePicker(false)" in jump_body
    assert "() => jumpRequestSeq === dateJumpRequestSeq" in jump_body

    window_start = html.index("const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
    window_body = html[window_start : html.index("watch(showMediaGallery", window_start)]
    assert "const isCurrentIntent = () =>" in window_body
    assert "!externalGuard || externalGuard()" in window_body
    assert window_body.count("if (!isCurrentIntent()) return") >= 6


def test_date_separators_are_not_individually_sticky():
    """Regression for #249.

    Every ``.date-separator`` is a direct child of the one scroll container, so
    making each one ``position: sticky`` pinned them all at the same offset —
    CSS Position L3 §3.4: "Multiple sticky positioned boxes in the same
    container are offset independently, and therefore might overlap". That
    stacked several pills showing contradictory dates, and whichever painted
    last looked "frozen". The day indicator must instead be a single element.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    separator_css_start = html.index(".date-separator {")
    separator_css = html[separator_css_start : html.index("}", separator_css_start)]
    assert "position: sticky" not in separator_css

    # ...and no other rule may reintroduce per-day stickiness in the message
    # list. The sticky boxes in the page are the phone sheet's grab bar, the
    # What changed day pill (inside its own day's section of the feed), the
    # sidebar's back row over Archived Chats and topics, and the filter row
    # over the admin chat checklist. None is in the message list.
    assert html.count("position: sticky") == 4
    for selector in (
        "html .popover-sheet::before {",
        ".change-day-pill {",
        ".list-back-row {",
        ".admin-checklist-head {",
    ):
        rule = html[html.index(selector) :]
        rule = rule[: rule.index("}")]
        assert "position: sticky" in rule, selector


def test_floating_date_pill_is_a_single_element_outside_the_scroller():
    """The pill must live outside the message list, so only one can ever exist."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert html.count('class="floating-date-pill"') == 1
    # Rendered after the scroll container closes (the container ends with the scrollAnchor).
    assert html.index('class="floating-date-pill"') > html.index('<div ref="scrollAnchor">')
    # Accessible as a heading rather than a live region: it changes on every
    # scroll, and announcing that would flood screen readers.
    assert 'role="heading" aria-level="5"' in html
    assert "aria-live" not in html.split('class="floating-date-pill"')[1][:400]


def test_floating_date_pill_is_guarded_and_clickable():
    """Empty lists and the pinned-only view must not render a day indicator."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    pill_start = html.index('v-if="floatingDateLabel')
    pill_block = html[pill_start : html.index("</div>", pill_start)]
    assert "!showPinnedOnly" in pill_block
    assert "sortedMessages.length > 0" in pill_block
    # Clicking still opens the date picker for the day being viewed.
    assert "openDatePicker(floatingDateIso)" in pill_block


def test_floating_date_recomputes_on_scroll_and_on_list_changes():
    """#249: scroll alone is not enough.

    Appending an older page to a ``flex-col-reverse`` list changes the content
    above the viewport WITHOUT firing a scroll event, and jump windows replace
    the array outright — so a scroll-only pill goes stale exactly after
    pagination. Both triggers must stay wired.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    scroll_start = html.index("const handleScroll = (e) =>")
    # End at the next sibling declaration (same indentation), not the first
    # nested `const` inside the handler body.
    scroll_body = html[scroll_start : html.index("\n                const ", scroll_start + 10)]
    assert "queueFloatingDateUpdate()" in scroll_body

    watch_start = html.index("watch(sortedMessages,")
    watch_body = html[watch_start : html.index("})", watch_start)]
    assert "updateFloatingDate" in watch_body

    # The scroll path must be coalesced to one recompute per frame.
    queue_start = html.index("const queueFloatingDateUpdate = () =>")
    queue_body = html[queue_start : html.index("// Stats data", queue_start)]
    assert "requestAnimationFrame" in queue_body
    assert "floatingDateFramePending" in queue_body

    # The day is derived from the separators (O(days)), never from every message row.
    update_start = html.index("const updateFloatingDate = () =>")
    update_body = html[update_start : html.index("const queueFloatingDateUpdate", update_start)]
    assert "querySelectorAll('.date-separator')" in update_body
    assert "[data-msg-id]" not in update_body


def test_floating_date_handles_the_top_of_history():
    """#249: being above every separator is not the same as being at the newest end.

    A separator sits above its own day's messages, so normally the current day is
    the separator closest ABOVE the trip line. At the very start of history the
    viewport is above every separator; resolving that to the newest message would
    print today's date while the oldest day is on screen — the same wrong-date
    symptom, moved to the top boundary. It must resolve to the first separator
    BELOW the line instead.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    update_start = html.index("const updateFloatingDate = () =>")
    update_body = html[update_start : html.index("const queueFloatingDateUpdate", update_start)]

    assert "firstBelow" in update_body
    assert "best = best || firstBelow" in update_body
    # The newest loaded message must NOT be used to resolve that case.
    assert "sortedMessages.value[0]" not in update_body


def test_sender_details_dialog_shows_a_large_avatar():
    """#240: the popup renders the already-resolved photo at a readable size."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index('<section ref="senderInfoDialog"')
    body = html[start : html.index("</section>", start)]

    # Big circle, above the definition list.
    assert "w-20 h-20 rounded-full" in body
    assert body.index("w-20 h-20 rounded-full") < body.index('<div class="tg-dialog-body mt-2">')

    # Same photo the message row resolved, with a fallback on load failure.
    assert 'v-if="senderInfoMessage.sender_avatar_url"' in body
    assert '@error="senderInfoMessage.sender_avatar_url = null"' in body

    # Fallback reuses the existing initials + deterministic gradient helpers.
    assert "getSenderInitials(senderInfoMessage)" in body
    assert "getAvatarFill(senderInfoMessage)" in body

    # Decorative only: it must not become a focusable child of the dialog's Tab trap.
    avatar = body[body.index("w-20 h-20 rounded-full") : body.index('<div class="tg-dialog-body mt-2">')]
    assert "<button" not in avatar
    assert "<a " not in avatar


def _chat_header_info_button(html: str) -> str:
    """The header button that holds the photo, the name and the status line (#538)."""
    header = html[html.index("<!-- Chat Header -->") : html.index('<div class="flex items-center gap-0.5 min-w-0">')]
    start = header.index('<button type="button" @click="openChatInfo($event)"')
    return header[start : header.index("</button>", start)]


def test_chat_header_avatar_is_decoration_inside_the_info_button():
    """#538: the phone's header photo sits inside the name's button, which opens the info panel."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    body = _chat_header_info_button(html)

    avatar = body[body.index('<span aria-hidden="true"') : body.index("</span>")]
    assert "md:hidden avatar-initials" in avatar
    assert 'alt=""' in avatar, "the name follows the photo, so the photo says nothing"
    assert "getChatAvatarFill(selectedChat)" in avatar
    # One photo for every kind of chat: no private-only button beside a plain circle.
    assert "<button" not in body[1:]
    assert "Show details for" not in html

    # The message rows keep their own trigger of the sender popup.
    assert '@click="openSenderInfo(msg, $event)"' in html


def test_chat_header_avatar_button_is_not_a_tap_target():
    """.tap-target forces 44px minimums on mobile and would deform the 40px circle."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    body = _chat_header_info_button(html)
    avatar = body[body.index('<span aria-hidden="true"') : body.index("</span>")]

    assert "tap-target" not in avatar
    assert "aspect-square" in avatar


# --- Global audio player (#250) -------------------------------------------------


def _code_only(body: str) -> str:
    """Drop whole-line ``//`` comments: some assertions are about code, not prose."""
    return "\n".join(line for line in body.splitlines() if not line.strip().startswith("//"))


def test_audio_playback_uses_a_single_shared_element():
    """#250: no per-message player may exist.

    ``loadMessagesAroundId`` replaces ``messages.value`` wholesale, so a player
    rendered inside the message ``v-for`` is destroyed mid-track by an ordinary
    jump. The bubble must only delegate to the app-wide engine.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Zero markup players: the engine is a JS-owned HTMLAudioElement.
    assert html.count("<audio") == 0
    assert "<audio controls" not in html
    assert html.count("new Audio()") == 1

    # The bubble branch now delegates.
    audio_branch_start = html.index('v-else-if="isAudioFile(msg)"')
    audio_branch = html[audio_branch_start : audio_branch_start + 2500]
    assert 'type="button"' in audio_branch
    assert "playAudioMessage(msg)" in audio_branch
    assert ":aria-label=" in audio_branch


def test_audio_engine_and_playbar_live_outside_the_message_loop():
    """The engine and its bar must survive chat switches and list rebuilds."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # The element is created in setup(), not in the template.
    engine_index = html.index("const audioEngine = new Audio()")
    assert engine_index > html.index("createApp({")

    # A single playbar, rendered after the message scroll container closes.
    assert html.count('class="audio-playbar') == 1
    assert html.index('class="audio-playbar') > html.index('<div ref="scrollAnchor">')

    # z-index band: above the message list / scroll FAB (10), below modals (50).
    playbar_css = html[html.index(".audio-playbar {") : html.index(".audio-playbar input")]
    z_index = int(playbar_css.split("z-index:")[1].split(";")[0].strip())
    assert 41 <= z_index <= 49

    # The layout flag must NOT be a :class on #app — that div is the mount
    # CONTAINER, so bindings written on it are never part of the template and are
    # silently dropped (verified in a browser: the class never appeared).
    assert ":class=\"{ 'audio-player-open'" not in html
    assert "document.body.classList.toggle('audio-player-open'" in html


def test_audio_playback_rate_survives_a_track_change():
    """#250: the media load algorithm resets ``playbackRate`` on every ``src`` change.

    Without planting the rate in ``defaultPlaybackRate`` before the ``src``
    assignment AND re-applying it once metadata arrives, every new track
    silently falls back to 1x while the UI still shows the chosen speed.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    load_body = _setup_slice(html, "const loadAudioTrack = (track) =>")
    assert load_body.index("audioEngine.defaultPlaybackRate = rate") < load_body.index("audioEngine.src = track.url")

    meta_start = html.index("audioEngine.addEventListener('loadedmetadata'")
    meta_body = html[meta_start : html.index("audioEngine.addEventListener('timeupdate'", meta_start)]
    assert "audioEngine.playbackRate = audioTrack.value" in meta_body


def test_audio_ended_advances_and_errors_halt_after_repeated_failures():
    """Auto-advance must chain tracks, but a broken media path must not be walked."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    ended_start = html.index("audioEngine.addEventListener('ended'")
    ended_body = html[ended_start : html.index("audioEngine.addEventListener('error'", ended_start)]
    assert "playNextAudio()" in ended_body
    assert "audioAutoAdvanceHalted.value" in ended_body

    error_start = html.index("audioEngine.addEventListener('error'")
    error_body = html[error_start : html.index("const audioMediaSessionActions", error_start)]
    assert "handleAudioLoadFailure()" in error_body
    assert "if (!audioAutoAdvanceHalted.value) playNextAudio()" in error_body

    assert "const AUDIO_MAX_FAILURES = 2" in html
    failure_body = _setup_slice(html, "const handleAudioLoadFailure = () =>")
    assert "audioConsecutiveFailures += 1" in failure_body
    assert "audioConsecutiveFailures >= AUDIO_MAX_FAILURES" in failure_body
    assert "audioAutoAdvanceHalted.value = true" in failure_body


def test_audio_play_rejection_distinguishes_blocked_from_aborted():
    """``play()`` rejects for two very different reasons and must not be swallowed."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    body = _setup_slice(html, "const startAudioPlayback = () =>")
    # Fast skipping aborts the pending play — benign, ignored.
    assert "if (name === 'AbortError') return" in body
    # Autoplay policy — surfaced as a tap-to-play state, never a silent stall.
    assert "name === 'NotAllowedError'" in body
    assert "audioBlocked.value = true" in body
    # Anything else is a real load failure.
    assert "handleAudioLoadFailure()" in body
    assert "audioStatusMessage" in html


def test_audio_speed_is_persisted_per_media_type():
    """Voice speed and music speed are independent and survive a reload."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const audioSpeeds = [0.5, 1, 1.5, 2]" in html
    key_body = _setup_slice(html, "const audioSpeedStorageKey = (kind) =>")
    assert "'audio_speed_voice'" in key_body
    assert "'audio_speed_music'" in key_body

    restore_body = _setup_slice(html, "const readStoredAudioSpeed = (kind) =>")
    assert "localStorage.getItem(audioSpeedStorageKey(kind))" in restore_body

    set_body = _setup_slice(html, "const setAudioSpeed = (rate) =>")
    assert "localStorage.setItem(audioSpeedStorageKey(kind), String(rate))" in set_body
    # The kind comes from the playing track, so music never overwrites voice.
    assert "audioTrack.value ? audioTrack.value.kind : 'voice'" in set_body


def test_audio_player_is_gated_on_no_download():
    """no_download viewers 403 on every /media GET — never offer or queue playback."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'v-if="audioTrack && !noDownload"' in html

    play_body = _setup_slice(html, "const playAudioMessage = (msg) =>")
    assert "if (noDownload.value) return" in play_body

    load_body = _setup_slice(html, "const loadAudioTrack = (track) =>")
    assert "noDownload.value" in load_body

    audio_branch_start = html.index('v-else-if="isAudioFile(msg)"')
    audio_branch = html[audio_branch_start : audio_branch_start + 2500]
    assert ':disabled="noDownload"' in audio_branch


def test_audio_media_session_is_feature_detected():
    """OS / lock-screen controls must be optional, never a hard dependency."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "'mediaSession' in navigator" in html
    assert "typeof MediaMetadata !== 'function'" in html
    assert "new MediaMetadata(" in html

    actions_start = html.index("const audioMediaSessionActions = {")
    actions_body = html[actions_start : html.index("if ('mediaSession' in navigator) {", actions_start)]
    for action in ("play:", "pause:", "previoustrack:", "nexttrack:"):
        assert action in actions_body
    # Unsupported actions throw, so each registration is independent.
    assert "navigator.mediaSession.setActionHandler(action, handler)" in html
    assert "navigator.mediaSession.playbackState" in html


def test_audio_auto_advance_does_not_drive_pagination():
    """Deferred by design (#250).

    Two IntersectionObservers already auto-fire the older/newer page loads. A
    player that also drove them would double-fetch and race ``loadingNewer`` /
    ``newerLoadError`` / ``chatVersion``, so advancing stops at the edge of the
    already-loaded window.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    advance_body = _setup_slice(html, "const playAdjacentAudio = (step) =>")
    assert "loadMessages" not in advance_body
    assert "loadNewerMessages" not in advance_body
    assert "audioQueue.value[index + step]" in advance_body

    ended_start = html.index("audioEngine.addEventListener('ended'")
    ended_body = html[ended_start : html.index("audioEngine.addEventListener('error'", ended_start)]
    assert "loadMessages" not in ended_body
    assert "loadNewerMessages" not in ended_body

    # The queue is a snapshot of copied metadata, not references into messages.value.
    queue_body = _setup_slice(html, "const buildAudioQueue = (msg) =>")
    assert "audioTrackFromMessage(m)" in queue_body
    # Voice and music never share a queue.
    assert "audioMediaKind(m) === kind" in queue_body


# --- Pagination-aware audio queue (#254) ---------------------------------------

_AUDIO_QUEUE_DECLARATIONS = (
    "const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') =>",
    "const seedAudioQueueAroundTrack = async (track) =>",
    "const extendAudioQueueFromMedia = async (track) =>",
    "const extendAudioQueueNewer = async () =>",
    "const extendAudioQueueOlder = async () =>",
)


def test_audio_queue_pages_the_media_endpoint_with_a_capped_cursor_walk():
    """#254: the queue grows from the media endpoint's own cursor, not the pane's.

    The unanchored backward walk is now the FALLBACK path (#266 anchors on the
    playing track's own media id instead), but it is still what runs for an
    archive whose media ids the client cannot rebuild, and it is still capped so
    a huge chat cannot spin forever.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    fetch_body = _setup_slice(html, "const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') =>")
    assert "`/api/chats/${encodeURIComponent(chatRef)}/media?${params}`" in fetch_body
    assert "types: audioQueueTypes(kind)," in fetch_body
    assert "limit: String(AUDIO_QUEUE_PAGE_SIZE)," in fetch_body
    # One cursor shape, two directions: before_id walks older, after_id newer.
    assert "params.set(direction === 'newer' ? 'after_id' : 'before_id', cursor)" in fetch_body
    assert "credentials: 'include'" in fetch_body

    assert "const AUDIO_QUEUE_PAGE_SIZE = 50" in html
    assert "const AUDIO_QUEUE_MAX_PAGES = 10" in html

    extend_body = _setup_slice(html, "const extendAudioQueueFromMedia = async (track) =>")
    assert "for (let page = 0; page < AUDIO_QUEUE_MAX_PAGES; page++)" in extend_body
    assert "await fetchAudioQueuePage(track.chatRef, track.kind, cursor, 'older')" in extend_body
    assert "cursor = items[items.length - 1].id" in extend_body
    # Stop as soon as the playing track is in hand, or nothing older is left.
    assert "items.some(item => item.message_id === track.id)" in extend_body
    assert "if (!hasOlder) break" in extend_body
    # Hitting the cap keeps whatever was fetched instead of failing — but only
    # when the walk actually reached the playing track (see #257 hole guard).
    assert "audioQueue.value = mergeAudioQueue(audioTracksFromMediaItems(collected, track))" in extend_body

    # "Previous" at the head of the queue pages one more time from the same cursor.
    older_body = _setup_slice(html, "const extendAudioQueueOlder = async () =>")
    assert "await fetchAudioQueuePage(track.chatRef, track.kind, audioQueueCursor, 'older')" in older_body
    prev_body = _setup_slice(html, "const playPrevAudio = async () =>")
    assert "await extendAudioQueueOlder()" in prev_body
    assert "seekAudioTo(0)" in prev_body


def test_audio_queue_extension_never_drives_message_pagination():
    """#254 is only safe because the player owns a SEPARATE cursor.

    The pane's older/newer pages are fetched by two IntersectionObservers. A
    player that also called those loaders would double-fetch and race
    ``loading`` / ``loadingNewer`` / ``newerLoadError`` / ``chatVersion``.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    advance_declarations = _AUDIO_QUEUE_DECLARATIONS + (
        "const playAdjacentAudio = (step) =>",
        "const playNextAudio = async () =>",
        "const playPrevAudio = async () =>",
        "const playAudioMessage = (msg) =>",
    )
    for declaration in advance_declarations:
        body = _setup_slice(html, declaration)
        assert "loadMessages" not in body, declaration
        assert "loadNewerMessages" not in body, declaration
        assert "messagesScrollObserver" not in body, declaration
        assert "messagesNewerObserver" not in body, declaration

    ended_start = html.index("audioEngine.addEventListener('ended'")
    ended_body = html[ended_start : html.index("audioEngine.addEventListener('error'", ended_start)]
    assert "loadMessages" not in ended_body
    assert "loadNewerMessages" not in ended_body

    # The step over the queue itself stays synchronous. playNextAudio wraps it
    # with the on-demand forward page (#266) and is therefore async, so the
    # 'ended' handler has to AWAIT its boolean — `!promise` is always false and
    # would skip the end-of-queue reset.
    assert "const playNextAudio = async () =>" in html
    next_body = _setup_slice(html, "const playNextAudio = async () =>")
    assert "if (playAdjacentAudio(1)) return true" in next_body
    assert "const outcome = await extendAudioQueueNewer()" in next_body
    assert "if (outcome === 'extended' && playAdjacentAudio(1)) return true" in next_body
    assert "if (audioAutoAdvanceHalted.value || !(await playNextAudio())) {" in ended_body

    advance_body = _setup_slice(html, "const playAdjacentAudio = (step) =>")
    assert "audioQueue.value[index + step]" in advance_body

    # The queue holds copied descriptors, so emptying messages.value on a chat
    # switch cannot invalidate it.
    item_body = _setup_slice(html, "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>")
    assert "id: item.message_id," in item_body
    assert "chatId: item.chat_id," in item_body
    assert "url: item.media_url || ''," in item_body


def test_audio_queue_discards_results_for_a_superseded_track():
    """A page that lands after the user started something else belongs to nobody.

    Two guards, because they catch different things: the request id is bumped by
    each new playback session (and by closing the player), while the (chat, kind)
    check catches a chat switch. Auto-advance within the same queue is NOT stale
    — voice notes are short enough to advance mid-fetch.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    guard_body = _setup_slice(html, "const audioQueueBelongsToTrack = (track) =>")
    assert "current.chatId === track.chatId" in guard_body
    assert "current.kind === track.kind" in guard_body

    extend_body = _setup_slice(html, "const extendAudioQueueFromMedia = async (track) =>")
    assert "const requestId = ++audioQueueRequestId" in extend_body
    assert "if (requestId !== audioQueueRequestId || !audioQueueBelongsToTrack(track)) return" in extend_body
    # The guard runs before anything is written back to the queue.
    assert extend_body.index("!audioQueueBelongsToTrack(track)") < extend_body.index(
        "audioQueue.value = mergeAudioQueue"
    )

    older_body = _setup_slice(html, "const extendAudioQueueOlder = async () =>")
    assert "const requestId = ++audioQueueRequestId" in older_body
    # Tri-state outcome since the follow-up: the stale guard yields 'aborted',
    # which playPrevAudio must not treat as "reached the oldest message".
    assert "if (requestId !== audioQueueRequestId || !audioQueueBelongsToTrack(track)) return 'aborted'" in older_body
    assert older_body.index("!audioQueueBelongsToTrack(track)") < older_body.index("audioQueue.value = mergeAudioQueue")

    # Closing the player invalidates whatever is still in flight.
    close_body = _setup_slice(html, "const closeAudioPlayer = () =>")
    assert "audioQueueRequestId += 1" in close_body
    assert "audioQueueCursor = null" in close_body


def test_audio_queue_fetch_failure_does_not_halt_auto_advance():
    """A failed queue page and a failed MEDIA load are different failure modes.

    Only the latter may count towards ``AUDIO_MAX_FAILURES``; a queue fetch that
    fails must degrade to the already-loaded window, silently.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    for declaration in _AUDIO_QUEUE_DECLARATIONS:
        body = _code_only(_setup_slice(html, declaration))
        assert "audioConsecutiveFailures" not in body, declaration
        assert "audioAutoAdvanceHalted" not in body, declaration
        assert "handleAudioLoadFailure" not in body, declaration

    extend_body = _setup_slice(html, "const extendAudioQueueFromMedia = async (track) =>")
    assert "} catch (e) {" in extend_body
    older_body = _setup_slice(html, "const extendAudioQueueOlder = async () =>")
    assert "} catch (e) {" in older_body
    assert "return 'error'" in older_body  # paging failure, explicitly not end-of-queue

    # The window-derived queue is seeded before the fetch is even started, so a
    # failure leaves playback exactly where it is today.
    play_body = _setup_slice(html, "const playAudioMessage = (msg) =>")
    assert play_body.index("audioQueue.value = buildAudioQueue(msg)") < play_body.index(
        "extendAudioQueueFromMedia(track)"
    )
    # ...and the fetch is not awaited, so it cannot spend the user gesture that
    # authorises playback on iOS.
    assert "await extendAudioQueueFromMedia(track)" not in play_body


def test_audio_queue_keeps_voice_and_music_on_separate_types():
    """Voice notes and music are separate queues, so they are separate queries."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    types_body = _setup_slice(html, "const audioQueueTypes = (kind) =>")
    assert "kind === 'voice' ? 'voice' : 'audio'" in types_body
    # Never the combined filter the media gallery uses.
    assert "'voice,audio'" not in types_body

    # Every page request is keyed on the playing track's own kind, in EVERY
    # direction — a forward page that dropped the kind would roll a voice run
    # into music the moment auto-advance left the seeded window.
    extend_body = _setup_slice(html, "const extendAudioQueueFromMedia = async (track) =>")
    assert "fetchAudioQueuePage(track.chatRef, track.kind, cursor, 'older')" in extend_body
    older_body = _setup_slice(html, "const extendAudioQueueOlder = async () =>")
    assert "fetchAudioQueuePage(track.chatRef, track.kind, audioQueueCursor, 'older')" in older_body
    seed_body = _setup_slice(html, "const seedAudioQueueAroundTrack = async (track) =>")
    assert "fetchAudioQueuePage(track.chatRef, track.kind, track.mediaId, 'older')" in seed_body
    newer_body = _setup_slice(html, "const extendAudioQueueNewer = async () =>")
    assert "track.chatRef, track.kind, audioQueueCursorNewer, 'newer')" in newer_body

    # Fetched descriptors inherit that kind, so a merged queue stays single-class.
    tracks_body = _setup_slice(html, "const audioTracksFromMediaItems = (items, track) =>")
    assert "audioTrackFromMediaItem(item, track.kind, track.chatName, track.chatRef)" in tracks_body


def test_audio_queue_in_flight_flag_cannot_leak():
    """#254: closing the player mid-fetch must not disable paging forever.

    The in-flight flag is owned by its own sequence, NOT by the staleness id:
    teardown bumps the staleness id without starting a fetch, so a `finally`
    keyed on that id never matches and the flag stays set — after which
    extendAudioQueueOlder bails on every later call and head-of-queue
    "previous" silently stops paging for the rest of the session.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "let audioQueueFetchSeq = 0" in html
    # EVERY fetch path releases the flag by ownership token, not by request id:
    # the anchored seed and the forward extension (#266) as well as the original
    # walk and the head-of-queue page.
    assert html.count("if (fetchToken === audioQueueFetchSeq) audioQueueFetching = false") == 4
    assert "if (requestId === audioQueueRequestId) audioQueueFetching = false" not in html

    # Teardown releases it outright.
    close_start = html.index("const closeAudioPlayer = () =>")
    close_body = html[close_start : html.index("\n                const ", close_start + 10)]
    assert "audioQueueFetching = false" in close_body


class TestAudioQueuePagingOutcomes(unittest.TestCase):
    """#254 follow-up: paging outcomes must stay distinguishable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _slice(self, declaration: str) -> str:
        start = self.html.index(declaration)
        return self.html[start : self.html.index("\n                const ", start + 10)]

    def test_transport_failure_is_not_end_of_queue(self) -> None:
        """401/403/429/5xx must not read as "no older audio".

        fetchAudioQueuePage only checked res.ok, so any error collapsed into the
        same falsy result as an exhausted cursor and "previous" restarted the
        current track as though the oldest message had been reached.
        """
        fetch_body = self._slice("const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') =>")
        self.assertIn("error.status = res.status", fetch_body)
        # Stale pages are aborted rather than landing on a closed player.
        self.assertIn("new AbortController()", fetch_body)
        self.assertIn("signal: controller.signal", fetch_body)

        older_body = self._slice("const extendAudioQueueOlder = async () =>")
        self.assertIn("return 'error'", older_body)
        self.assertIn("return 'exhausted'", older_body)
        self.assertIn("if (e?.name === 'AbortError') return 'aborted'", older_body)
        # Paging failure must never halt playback.
        self.assertNotIn("audioConsecutiveFailures", older_body)

    def test_in_flight_page_is_not_reported_as_exhausted(self) -> None:
        """A page already on its way is not the end of the queue.

        Returning 'exhausted' while a fetch is in flight makes a second
        "previous" press restart the track before that page lands.
        """
        older_body = self._slice("const extendAudioQueueOlder = async () =>")
        self.assertIn("if (audioQueueFetching) return 'pending'", older_body)
        # The three guard states stay separate rather than collapsing into one.
        self.assertIn("if (!track) return 'aborted'", older_body)
        self.assertIn("if (!audioQueueHasOlder || !audioQueueCursor) return 'exhausted'", older_body)

        prev_body = self._slice("const playPrevAudio = async () =>")
        # Restart only on a known-exhausted queue — never on error, abort or pending.
        self.assertIn("if (outcome === 'exhausted') seekAudioTo(0)", prev_body)

    def test_teardown_aborts_the_in_flight_page(self) -> None:
        close_body = self._slice("const closeAudioPlayer = () =>")
        self.assertIn("audioQueueAbort?.abort()", close_body)


class TestAudioQueueHoleGuard(unittest.TestCase):
    """#257: a capped backward walk must not splice two disjoint time blocks."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_paged_result_is_adopted_only_when_the_playing_track_was_found(self) -> None:
        """The walk stops at AUDIO_QUEUE_PAGE_SIZE * AUDIO_QUEUE_MAX_PAGES items.

        With more audio newer than the playing track than that cap allows, the
        loop ends without ever reaching it, and the newest block plus the seed
        window are two blocks with a chronological HOLE between them. The old
        code merged and date-sorted them unconditionally, so "next" walked to
        the seed edge and then jumped days ahead.
        """
        body = _setup_slice(self.html, "const extendAudioQueueFromMedia = async (track) =>")

        # The loop records whether the playing track actually showed up.
        self.assertIn("let foundPlaying = false", body)
        self.assertIn("foundPlaying = true", body)

        # The guard runs BEFORE the merge, and bails out of it.
        self.assertIn("if (!foundPlaying && hasOlder) return", body)
        guard = body.index("if (!foundPlaying && hasOlder) return")
        merge = body.index("audioQueue.value = mergeAudioQueue(")
        self.assertLess(guard, merge)

        # Not found -> keep the seed queue exactly as it is.
        bail = body[guard:merge]
        self.assertNotIn("audioQueue.value =", bail)

    def test_hole_guard_does_not_latch_paging_off_for_the_session(self) -> None:
        """The guard must abandon ONE extension, not disable 'previous' forever.

        Setting ``audioQueueHasOlder = false`` on the not-found path permanently
        killed head-of-queue paging past the seed window for the rest of the
        session, even though a later, correctly seeded walk could succeed. Only
        a CONTIGUOUS result may write the cursor / has-older pair, and the reset
        on the next track or chat is what re-enables paging.
        """
        body = _code_only(_setup_slice(self.html, "const extendAudioQueueFromMedia = async (track) =>"))
        guard = body.index("if (!foundPlaying && hasOlder) return")
        # The guard is a bare early return: it writes NOTHING back. The only
        # assignment to the session flag in this whole helper is the contiguous
        # one, so the not-found path cannot latch paging off.
        self.assertNotIn("audioQueueHasOlder = false", body)
        self.assertEqual(body.count("audioQueueHasOlder"), 1)
        # The pair is still written on the contiguous path, after the guard.
        self.assertLess(guard, body.index("audioQueueCursor = cursor"))
        self.assertLess(guard, body.index("audioQueueHasOlder = hasOlder"))
        # ...and reset on the next track / on close, which is the re-enable point.
        self.assertIn("audioQueueHasOlder = false", _setup_slice(self.html, "const playAudioMessage = (msg) =>"))
        self.assertIn("audioQueueHasOlder = false", _setup_slice(self.html, "const closeAudioPlayer = () =>"))

    def test_an_empty_page_never_forces_exhaustion_and_always_terminates(self) -> None:
        """An empty page is not proof the walk reached the end of the chat.

        ``get_media_paginated`` also answers ``{items: [], has_more: False}``
        for a ``before_id`` it cannot resolve (a foreign or since-deleted cursor
        row). Forcing ``hasOlder = false`` there made an unresolvable cursor
        look exactly like exhaustion, so the #257 guard adopted or truncated a
        walk that never reached the end.

        ``hasOlder`` already carries the right answer on both paths — its
        initial ``false`` on the first page, the previous page's ``has_more``
        mid-walk — so the branch must write NOTHING. What it must still do is
        ``break``, which is what keeps the walk finite.
        """
        body = _code_only(_setup_slice(self.html, "const extendAudioQueueFromMedia = async (track) =>"))
        # The initial value is what makes an empty FIRST page read as exhausted.
        self.assertIn("let hasOlder = false", body)
        empty = body.index("if (!items.length) {")
        branch = body[empty : body.index("collected.push(...items)")]
        self.assertNotIn("hasOlder", branch)
        # ...but the loop still ends here, so F4's infinite-walk risk stays closed.
        self.assertIn("break", branch)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_walk_outcomes_executed_against_both_kinds_of_empty_page(self) -> None:
        """The EXECUTED counterpart: two walks that differ only in has_more.

        Both end on a page the loop cannot continue from, and the string test
        above cannot tell them apart:

        * ``endOfChat`` — the last page carries items and ``has_more: false``.
          The chat really is exhausted, so the paged result is adopted even
          though the playing track was never reached.
        * ``badCursor`` — the first page promises ``has_more: true`` and the
          second comes back empty, which is what an unresolvable ``before_id``
          returns. That walk may have a hole in it, so it must be abandoned and
          the seeded queue left exactly as it was.
        """
        prelude = """
const noDownload = { value: false }
const audioQueue = { value: [] }
const audioTrack = { value: { chatId: 7, chatRef: 'r7', kind: 'voice' } }
const audioTrackFromMediaItem = (item, kind, chatName) => ({
    id: item.message_id, chatId: item.chat_id, kind, chatName,
    date: item.message_date, url: item.media_url,
})
let PAGES = []
const REQUESTED = []
const fetchAudioQueuePage = async (chatId, kind, beforeId) => {
    REQUESTED.push(beforeId ?? null)
    return PAGES.shift() ?? { items: [], has_more: false }
}
const mediaItem = (n) => ({
    id: `m${n}`, message_id: n, chat_id: 7,
    media_url: `/media/${n}.ogg`, message_date: `2026-07-0${n}T00:00:00`,
})
// NOTE: audioQueueCursor and audioQueueHasOlder are NOT declared here on
// purpose. Both are real module-scope `let`s in the template and arrive with
// the sliced declarations below; declaring them again is a duplicate-binding
// SyntaxError, so the epilogue's assignments are not implicit globals.
"""
        epilogue = """
const scenario = async (pages) => {
    PAGES = pages.slice()
    REQUESTED.length = 0
    audioQueueCursor = null
    audioQueueHasOlder = false
    // The seeded window queue around the playing track, which the guard
    // protects when the walk cannot be trusted.
    audioQueue.value = [{ id: 999, chatId: 7, chatRef: 'r7', kind: 'voice', date: '2026-07-09T00:00:00' }]
    await extendAudioQueueFromMedia({ chatId: 7, chatRef: 'r7', kind: 'voice', id: 999, chatName: 'c' })
    return {
        cursor: audioQueueCursor,
        hasOlder: audioQueueHasOlder,
        ids: audioQueue.value.map(t => t.id),
        requested: REQUESTED.slice(),
    }
};
(async () => {
    const endOfChat = await scenario([
        { items: [mediaItem(3)], has_more: true },
        { items: [mediaItem(2)], has_more: false },
    ]);
    const badCursor = await scenario([
        { items: [mediaItem(3)], has_more: true },
        { items: [], has_more: false },
    ]);
    console.log(JSON.stringify({ endOfChat, badCursor }));
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const AUDIO_QUEUE_MAX_PAGES = ",
                "const audioTracksFromMediaItems = (items, track) =>",
                "const audioTrackTime = (track) =>",
                "const mergeAudioQueue = (tracks) =>",
                "const audioQueueBelongsToTrack = (track) =>",
                "const extendAudioQueueFromMedia = async (track) =>",
            ),
            prelude,
            epilogue,
        )

        # Genuine end of chat: both pages were walked, the result is contiguous,
        # and it is merged into the seeded queue in date order.
        self.assertEqual(out["endOfChat"]["requested"], [None, "m3"])
        self.assertFalse(out["endOfChat"]["hasOlder"])
        self.assertEqual(out["endOfChat"]["cursor"], "m2")
        self.assertEqual(out["endOfChat"]["ids"], [2, 3, 999])

        # Unresolvable cursor: the same two fetches, the same "no more items",
        # but the walk is NOT trustworthy, so nothing is adopted.
        self.assertEqual(out["badCursor"]["requested"], [None, "m3"])
        self.assertEqual(out["badCursor"]["ids"], [999])
        self.assertIsNone(out["badCursor"]["cursor"])
        self.assertFalse(out["badCursor"]["hasOlder"])

    def test_the_guard_is_verified_without_shipping_any_observation_hook(self) -> None:
        """The queue is watched by EXECUTING the real helpers, not by exporting it.

        ``test_walk_outcomes_executed_against_both_kinds_of_empty_page`` lifts
        the declarations verbatim and supplies its own ``audioQueue`` ref, so the
        shipped template needs no debug hook AND no ``setup()`` export: nothing
        in the markup consumes ``audioQueue``, and an entry in the returned
        object that no template expression reads is dead surface that reads as
        if the UI depended on it. If a template expression ever does need it,
        export it and drop the second assertion.
        """
        self.assertNotIn("window.__dbg", self.html)
        self.assertNotIn("\n                    audioQueue,\n", self.html)

    def test_track_change_does_not_double_request_the_file(self) -> None:
        """Assigning ``src`` already invokes the media load algorithm.

        The extra ``load()`` aborted that fetch and re-invoked it, so the same
        .ogg was requested 2-3 times per track.
        """
        body = _setup_slice(self.html, "const loadAudioTrack = (track) =>")
        self.assertNotIn("audioEngine.load()", body)
        # The playbackRate trap stays fixed: rate before src, re-applied on metadata.
        self.assertLess(
            body.index("audioEngine.defaultPlaybackRate = rate"),
            body.index("audioEngine.src = track.url"),
        )
        meta_start = self.html.index("audioEngine.addEventListener('loadedmetadata'")
        meta_body = self.html[meta_start : self.html.index("audioEngine.addEventListener('timeupdate'", meta_start)]
        self.assertIn("audioEngine.playbackRate = audioTrack.value", meta_body)

    def test_playbar_jump_loads_the_window_when_the_row_is_absent(self) -> None:
        """The queue reaches far past the loaded window, so the row is usually absent."""
        body = _setup_slice(self.html, "const focusAudioTrackMessage = async () =>")
        self.assertIn("if (!findMessageElement(track.id)) {", body)
        self.assertIn("await loadMessagesAroundId(track.id)", body)
        self.assertLess(
            body.index("findMessageElement(track.id)"),
            body.index("scrollToMessage(track.id)"),
        )

    @unittest.skipUnless(NODE, "node is required to execute the navigation chain")
    def test_playbar_jump_lands_in_the_forum_tracks_own_topic(self) -> None:
        """selectChat short-circuits a forum chat into its topics list without
        touching selectedChat, so the old jump windowed WHATEVER pane was left
        behind — an unrelated slice of another chat positioned by a message id
        that means nothing there. The real navigation chain is executed here:
        the window load must only ever fire with the pane on the track's chat
        and topic, and a vanished topic must bail instead of windowing."""
        prelude = """
const selectedChat = { value: null }
const selectedPaneTopic = { value: null }
const messages = { value: [] }
const messageSearchQuery = { value: '' }
const deletedOnly = { value: false }
const editedOnly = { value: false }
const messageFilterOn = () => !!messageSearchQuery.value || deletedOnly.value || editedOnly.value
const chatStats = { value: null }
const pinnedMessages = { value: [] }
const currentPinnedIndex = { value: 0 }
const showPinnedOnly = { value: false }
const showMediaGallery = { value: false }
const loading = { value: false }
const navStack = { value: [] }
const chats = { value: [] }
const topics = { value: [] }
const audioTrack = { value: null }
const audioError = { value: '' }
let chatVersion = 0
let galleryReturnState = null
let TOPICS_BY_REF = {}
const WINDOW_LOADS = []
const SCROLLS = []
const stopMessageRefresh = () => {}
const startMessageRefresh = () => {}
const resetMessagePagination = () => {}
const setupMessagesScrollObserver = () => {}
const scrollToBottom = () => {}
const nextTick = async () => {}
const loadMessages = async () => {}
const loadChatStats = async () => {}
const loadPinnedMessages = async () => {}
const getChatName = (chat) => chat.title || ''
const navigateTo = (entry) => { navStack.value.push(entry) }
const loadTopics = async (ref) => { topics.value = TOPICS_BY_REF[ref] || [] }
const findMessageElement = () => null
const scrollToMessage = (id) => { SCROLLS.push(id) }
const loadMessagesAroundId = async (messageId) => {
    WINDOW_LOADS.push({
        messageId,
        chatId: selectedChat.value?.id ?? null,
        topicId: activeTopicId(),
    })
}
"""
        epilogue = """
(async () => {
    const forumChat = { id: -1001111, ref: 'refA', title: 'Forum A', is_forum: true }
    const plainChat = { id: -1002222, ref: 'refB', title: 'Plain B' }
    const otherChat = { id: -1003333, ref: 'refC', title: 'Plain C' }
    chats.value = [forumChat, plainChat, otherChat]

    const reset = () => {
        WINDOW_LOADS.length = 0
        navStack.value = []
        topics.value = []
        selectedPaneTopic.value = null
    }

    // 1. The bead's trigger: track from a forum topic, pane on another chat.
    reset()
    TOPICS_BY_REF = { refA: [{ id: 1, title: 'General' }, { id: 7, title: 'Topic 7' }] }
    selectedChat.value = plainChat
    audioTrack.value = { id: 4242, chatId: -1001111, topicId: 7 }
    await focusAudioTrackMessage()
    const forumJump = { loads: [...WINDOW_LOADS], paneTopic: selectedPaneTopic.value?.id ?? null }

    // 2. The track's topic no longer exists: bail on the topics list, and SAY so.
    reset()
    audioError.value = ''
    TOPICS_BY_REF = { refA: [{ id: 1, title: 'General' }] }
    selectedChat.value = plainChat
    audioTrack.value = { id: 4242, chatId: -1001111, topicId: 7 }
    await focusAudioTrackMessage()
    const vanishedTopic = { loads: [...WINDOW_LOADS], error: audioError.value }

    // 2b. A General-topic track whose captured topics lack a General row:
    // General always exists conceptually — the jump synthesizes it.
    reset()
    audioError.value = ''
    TOPICS_BY_REF = { refA: [{ id: 7, title: 'Topic 7' }] }
    selectedChat.value = plainChat
    audioTrack.value = { id: 4243, chatId: -1001111, topicId: null }
    await focusAudioTrackMessage()
    const generalSynthesized = { loads: [...WINDOW_LOADS], paneTopic: selectedPaneTopic.value?.id ?? null }

    // 3. Same forum chat, pane scoped to a different topic.
    reset()
    TOPICS_BY_REF = { refA: [{ id: 1, title: 'General' }, { id: 7, title: 'Topic 7' }] }
    selectedChat.value = forumChat
    selectedPaneTopic.value = { id: 1, title: 'General' }
    audioTrack.value = { id: 4242, chatId: -1001111, topicId: 7 }
    await focusAudioTrackMessage()
    const topicSwitch = { loads: [...WINDOW_LOADS], paneTopic: selectedPaneTopic.value?.id ?? null }

    // 4. A plain chat switch keeps working exactly as before.
    reset()
    selectedChat.value = plainChat
    audioTrack.value = { id: 99, chatId: -1003333, topicId: null }
    await focusAudioTrackMessage()
    const plainSwitch = { loads: [...WINDOW_LOADS] }

    console.log(JSON.stringify({ forumJump, vanishedTopic, generalSynthesized, topicSwitch, plainSwitch }))
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const GENERAL_TOPIC_ID = ",
                "const activeTopicId = () => {",
                "const openForumTopics = async (chat) =>",
                "const selectTopic = async (chat, topic, options = {}) =>",
                "const selectChat = async (chat, options = {}) =>",
            ),
            prelude,
            # focusAudioTrackMessage is followed by audioEngine listener wiring,
            # not a 16-space const, so _setup_slice would swallow it — lift the
            # function by exact brace match and define it just before the driver.
            _setup_function(self.html, "const focusAudioTrackMessage = async () =>") + "\n" + epilogue,
        )

        # The window load fired exactly once, with the pane on the track's chat
        # AND topic — never on the chat the user happened to be reading.
        self.assertEqual(
            out["forumJump"]["loads"],
            [{"messageId": 4242, "chatId": -1001111, "topicId": 7}],
        )
        self.assertEqual(out["forumJump"]["paneTopic"], 7)

        # Vanished topic: no window load at all — the user is left on the
        # topics list with an error surfaced, not on an unrelated pane.
        self.assertEqual(out["vanishedTopic"]["loads"], [])
        self.assertTrue(out["vanishedTopic"]["error"], "the bail must be surfaced, not silent")

        # Missing General row: synthesized, jump lands in topic 1.
        self.assertEqual(
            out["generalSynthesized"]["loads"],
            [{"messageId": 4243, "chatId": -1001111, "topicId": 1}],
        )
        self.assertEqual(out["generalSynthesized"]["paneTopic"], 1)

        # Same chat, wrong topic pane: re-navigates into the track's topic.
        self.assertEqual(
            out["topicSwitch"]["loads"],
            [{"messageId": 4242, "chatId": -1001111, "topicId": 7}],
        )
        self.assertEqual(out["topicSwitch"]["paneTopic"], 7)

        # A non-forum switch is untouched by all of this.
        self.assertEqual(
            out["plainSwitch"]["loads"],
            [{"messageId": 99, "chatId": -1003333, "topicId": None}],
        )


# --- Directional, on-demand audio queue (#266) ---------------------------------

_AUDIO_QUEUE_EXEC_DECLARATIONS = (
    # Brings AUDIO_QUEUE_MAX_PAGES *and* every module-scope `let` the queue owns
    # (request id, fetch seq, both cursors, both has-more flags, fetching), which
    # is why none of them may be re-declared in a prelude.
    "const AUDIO_QUEUE_MAX_PAGES = ",
    "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>",
    "const audioTracksFromMediaItems = (items, track) =>",
    "const audioTrackTime = (track) =>",
    "const mergeAudioQueue = (tracks) =>",
    "const audioQueueBelongsToTrack = (track) =>",
    "const audioQueueEdgeTrack = (newest) =>",
    "const audioQueueEdgeMediaId = (newest) =>",
)

_AUDIO_QUEUE_EXEC_PRELUDE = """
const noDownload = { value: false }
const audioError = { value: '' }
const audioQueue = { value: [] }
const audioTrack = { value: null }
let PAGES = []
let REPEAT = null
const REQUESTED = []
const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') => {
    REQUESTED.push([cursor ?? null, direction])
    return PAGES.shift() ?? REPEAT ?? { items: [], has_more: false }
}
// Ascending ids double as ascending dates, so mergeAudioQueue's sort is stable
// and the queue's first/last entries really are its oldest/newest.
const mediaItem = (n) => ({
    id: `m${n}`, message_id: n, chat_id: 7,
    media_url: `/media/${n}.ogg`, message_date: `2026-07-01T00:00:${String(n).padStart(2, '0')}`,
})
// Downloaded, but with no URL this viewer can play: the queue drops it, so a
// page of these adds NOTHING even though it was a perfectly successful fetch.
const unplayableItem = (n) => ({ ...mediaItem(n), media_url: '' })
const seedTrack = (n) => ({
    id: n, chatId: 7, chatRef: 'r7', kind: 'voice', mediaId: `m${n}`,
    date: `2026-07-01T00:00:${String(n).padStart(2, '0')}`, url: `/media/${n}.ogg`,
})
"""


class TestAudioQueueExtendsInTheDirectionOfTravel(unittest.TestCase):
    """#266: auto-advance died after ~40 tracks in a 1,764-note chat.

    The queue was pre-collected by walking BACKWARD from the newest item until
    the playing track appeared — a cost of one page per 50 tracks of DEPTH,
    capped at ``AUDIO_QUEUE_MAX_PAGES``. Past that cap the walk never reached the
    playing track, #257's hole guard (correctly) refused the disjoint result, and
    the queue stayed at the seeded message window: 9 requests, ~40 tracks, then
    silence with no error and no 'ended'.

    Raising the cap only moves the wall. The fix is to stop pre-collecting: page
    forward, one page at a time, in the direction playback is actually moving.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_forward_paging_uses_the_after_id_cursor(self) -> None:
        """The endpoint pages backward by default; forward needs the #266 cursor."""
        fetch_body = _setup_slice(
            self.html, "const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') =>"
        )
        self.assertIn("params.set(direction === 'newer' ? 'after_id' : 'before_id', cursor)", fetch_body)
        # One cursor per request: before_id and after_id are mutually exclusive
        # server-side, and sending both is a 400.
        self.assertEqual(fetch_body.count("params.set("), 1)

    def test_forward_cursor_is_armed_without_a_request(self) -> None:
        """The seeded window already names the newest track — no walk needed.

        This is what makes the fix depth-independent: the queue can page forward
        from its own edge the instant auto-advance reaches it, whether the
        playing track is the 5th voice note of the chat or the 1,700th.
        """
        play_body = _setup_slice(self.html, "const playAudioMessage = (msg) =>")
        # #268 qualified the edge: it is an anchor only while the window is a
        # contiguous slice of the timeline (see TestAudioQueueForwardCursorContiguity).
        self.assertIn(
            "(audioQueueSeedIsContiguous() && audioQueueEdgeMediaId(true)) || track.mediaId",
            play_body,
        )
        self.assertIn("audioQueueHasNewer = !!audioQueueCursorNewer", play_body)
        # ...and both are released with the player, like the backward pair.
        close_body = _setup_slice(self.html, "const closeAudioPlayer = () =>")
        self.assertIn("audioQueueCursorNewer = null", close_body)
        self.assertIn("audioQueueHasNewer = false", close_body)

    def test_no_page_cap_gates_the_forward_extension(self) -> None:
        """A cap is what put the wall there. Correctness must not need one."""
        newer_body = _code_only(_setup_slice(self.html, "const extendAudioQueueNewer = async () =>"))
        self.assertNotIn("AUDIO_QUEUE_MAX_PAGES", newer_body)
        # No counter of any kind drives termination — Igor's report blamed a
        # consecutive-load counter, and inventing one would be the wrong fix.
        self.assertNotIn("page++", newer_body)
        self.assertNotIn("attempts", newer_body)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_forward_extension_executed(self) -> None:
        """Four walks over the REAL helper, distinguishable only by execution."""
        epilogue = """
const scenario = async (pages, repeat) => {
    PAGES = pages.slice()
    REPEAT = repeat ?? null
    REQUESTED.length = 0
    audioQueueCursor = null
    audioQueueHasOlder = false
    audioQueue.value = [seedTrack(10)]
    audioTrack.value = { chatId: 7, chatRef: 'r7', kind: 'voice', id: 10, chatName: 'c', mediaId: 'm10' }
    audioQueueCursorNewer = audioQueueEdgeMediaId(true)
    audioQueueHasNewer = true
    const outcome = await extendAudioQueueNewer()
    return {
        outcome,
        cursor: audioQueueCursorNewer,
        hasNewer: audioQueueHasNewer,
        ids: audioQueue.value.map(t => t.id),
        requested: REQUESTED.slice(),
    }
};
(async () => {
    // 1. The ordinary case: one page forward, appended in date order.
    const extended = await scenario([
        { items: [mediaItem(11), mediaItem(12)], has_more: true },
    ]);
    // 2. Nothing newer: the forward walk really is finished.
    const empty = await scenario([{ items: [], has_more: false }]);
    // 3. A page that adds nothing usable is NOT the end — keep going. A stop
    //    here would be #266 all over again, just one page further along.
    const skipped = await scenario([
        { items: [unplayableItem(11), unplayableItem(12)], has_more: true },
        { items: [mediaItem(13)], has_more: true },
    ]);
    // 4. Runaway: a server that keeps handing back the SAME page while
    //    promising more. Terminates on "the cursor did not move", never on a
    //    page count, and must not spin.
    const stuck = await scenario([], { items: [unplayableItem(11)], has_more: true });
    console.log(JSON.stringify({ extended, empty, skipped, stuck }));
})();
"""
        out = _run_setup_program(
            self.html,
            _AUDIO_QUEUE_EXEC_DECLARATIONS + ("const extendAudioQueueNewer = async () =>",),
            _AUDIO_QUEUE_EXEC_PRELUDE,
            epilogue,
        )

        # 1. Travelling forward: one request, after_id on the queue's own edge.
        self.assertEqual(out["extended"]["requested"], [["m10", "newer"]])
        self.assertEqual(out["extended"]["outcome"], "extended")
        self.assertEqual(out["extended"]["ids"], [10, 11, 12])
        self.assertEqual(out["extended"]["cursor"], "m12")
        self.assertTrue(out["extended"]["hasNewer"])

        # 2. Genuine end of the chat.
        self.assertEqual(out["empty"]["outcome"], "exhausted")
        self.assertEqual(out["empty"]["ids"], [10])
        self.assertFalse(out["empty"]["hasNewer"])

        # 3. Progress, not page count, decides: the unusable page advanced the
        #    cursor, so the walk continued and the NEXT page delivered a track.
        self.assertEqual(out["skipped"]["outcome"], "extended")
        self.assertEqual(out["skipped"]["ids"], [10, 13])
        self.assertEqual(out["skipped"]["requested"], [["m10", "newer"], ["m12", "newer"]])

        # 4. No progress -> stop. Reaching this assertion at all is the proof
        #    that the runaway guard fired instead of looping forever.
        self.assertEqual(out["stuck"]["outcome"], "exhausted")
        self.assertEqual(out["stuck"]["ids"], [10])
        self.assertFalse(out["stuck"]["hasNewer"])
        self.assertEqual(out["stuck"]["requested"], [["m10", "newer"], ["m11", "newer"]])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_seeding_is_anchored_on_the_playing_track_executed(self) -> None:
        """The backward direction is anchored too, so nothing walks by depth.

        ``before_id`` is an opaque media id and the PLAYING track's own id is a
        valid one, so a single request yields the page immediately older than it
        — contiguous by construction, whatever its depth. Only a track whose id
        this archive does not use falls through to the legacy walk.
        """
        epilogue = """
const scenario = async (track, pages) => {
    PAGES = pages.slice()
    REPEAT = null
    REQUESTED.length = 0
    audioQueueCursor = null
    audioQueueHasOlder = false
    audioQueueCursorNewer = null
    audioQueueHasNewer = false
    audioQueue.value = [seedTrack(500)]
    audioTrack.value = { chatId: 7, chatRef: 'r7', kind: 'voice', id: 500, chatName: 'c' }
    await extendAudioQueueFromMedia(track)
    return {
        cursor: audioQueueCursor,
        hasOlder: audioQueueHasOlder,
        cursorNewer: audioQueueCursorNewer,
        hasNewer: audioQueueHasNewer,
        ids: audioQueue.value.map(t => t.id),
        requested: REQUESTED.slice(),
    }
};
(async () => {
    const anchored = { chatId: 7, chatRef: 'r7', kind: 'voice', id: 500, chatName: 'c', mediaId: 'm500' };
    // The endpoint answers a backward page newest-first.
    const seeded = await scenario(anchored, [
        { items: [mediaItem(499), mediaItem(498)], has_more: true },
    ]);
    // #268: an empty backward page is settled with ONE anchored probe forward.
    // Empty both ways = an id shape this archive does not use, which only the
    // unanchored walk can settle.
    const unresolvable = await scenario(anchored, [
        { items: [], has_more: false },
        { items: [], has_more: false },
        { items: [mediaItem(501), mediaItem(500), mediaItem(499)], has_more: false },
    ]);
    // ...but items forward mean the anchor RESOLVES and the track really is the
    // oldest of its kind. That must stay on the anchored path.
    const oldestOfItsKind = await scenario(anchored, [
        { items: [], has_more: false },
        { items: [mediaItem(501), mediaItem(502)], has_more: true },
    ]);
    // No id to anchor on at all -> straight to the walk.
    const noAnchor = await scenario({ chatId: 7, chatRef: 'r7', kind: 'voice', id: 500, chatName: 'c' }, [
        { items: [mediaItem(501), mediaItem(500)], has_more: false },
    ]);
    console.log(JSON.stringify({ seeded, unresolvable, oldestOfItsKind, noAnchor }));
})();
"""
        out = _run_setup_program(
            self.html,
            _AUDIO_QUEUE_EXEC_DECLARATIONS
            + (
                "const seedAudioQueueAroundTrack = async (track) =>",
                "const extendAudioQueueFromMedia = async (track) =>",
            ),
            _AUDIO_QUEUE_EXEC_PRELUDE,
            epilogue,
        )

        # ONE request, anchored on the playing track — not a walk from the newest
        # item, which is what cost Igor 9 requests and still missed him.
        self.assertEqual(out["seeded"]["requested"], [["m500", "older"]])
        self.assertEqual(out["seeded"]["ids"], [498, 499, 500])
        self.assertEqual(out["seeded"]["cursor"], "m498")
        self.assertTrue(out["seeded"]["hasOlder"])

        # Anchor rejected in BOTH directions -> the walk runs, starting from no
        # cursor as before.
        self.assertEqual(
            out["unresolvable"]["requested"],
            [["m500", "older"], ["m500", "newer"], [None, "older"]],
        )
        self.assertEqual(out["unresolvable"]["ids"], [499, 500, 501])

        # #268: the oldest track of its kind never reaches the walk. Two anchored
        # requests settle it, and the forward page it already paid for becomes the
        # queue AND the forward cursor. Handing this to the walk instead would make
        # it page backwards through the entire chat to find the very last track.
        self.assertEqual(
            out["oldestOfItsKind"]["requested"],
            [["m500", "older"], ["m500", "newer"]],
        )
        self.assertEqual(out["oldestOfItsKind"]["ids"], [500, 501, 502])
        self.assertIsNone(out["oldestOfItsKind"]["cursor"])
        self.assertFalse(out["oldestOfItsKind"]["hasOlder"])
        self.assertEqual(out["oldestOfItsKind"]["cursorNewer"], "m502")
        self.assertTrue(out["oldestOfItsKind"]["hasNewer"])

        # No anchor -> no wasted anchored request at all.
        self.assertEqual(out["noAnchor"]["requested"], [[None, "older"]])
        self.assertEqual(out["noAnchor"]["ids"], [500, 501])

    def test_media_id_is_carried_on_every_descriptor(self) -> None:
        """A cursor the queue cannot name is a queue that cannot page."""
        item_body = _setup_slice(self.html, "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>")
        self.assertIn("mediaId: item.id ?? null,", item_body)

        msg_body = _setup_slice(self.html, "const audioTrackFromMessage = (msg) =>")
        self.assertIn("mediaId: audioMessageMediaId(msg),", msg_body)

        # #268: taken from the payload first — the rebuild is only the fallback.
        # v8.0: the cursor is the CHAT-FREE `{message_id}_{type}` key, so the
        # rebuild no longer needs (or embeds) the chat id.
        built = _setup_slice(self.html, "const audioMessageMediaId = (msg) =>")
        self.assertIn("const delivered = msg?.media?.id", built)
        self.assertLess(built.index("msg?.media?.id"), built.index("`${msg.id}_${type}`"))
        self.assertIn("return `${msg.id}_${type}`", built)
        self.assertIn("if (msg?.id == null || !type) return null", built)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_rebuilt_media_id_executed(self) -> None:
        out = _run_setup_helpers(
            self.html,
            (
                "const audioMessageChatId = (msg) =>",
                "const audioMessageMediaId = (msg) =>",
            ),
            "cases.map(audioMessageMediaId)",
            prelude="""
const selectedChat = { value: null }
const cases = [
    { id: 12, chat_id: -100, media: { type: 'voice' } },
    { id: 12, chat_id: -100, media: {} },
    // v8.0: the rebuilt cursor is chat-free, so a missing chat_id no longer
    // matters — the ref in the request path is what scopes it.
    { id: 12, media: { type: 'voice' } },
]
""",
        )
        self.assertEqual(out, ["12_voice", None, "12_voice"])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_delivered_media_id_wins_over_the_rebuild_executed(self) -> None:
        """#268: the authoritative cursor is already in the payload — use it.

        ``get_messages_paginated`` and ``get_pinned_messages`` both select
        ``Media.id`` into ``media.id`` (telegram_archive/db/adapter.py), and the no_download
        strip only blanks ``file_path``, so the client never has to guess. It
        matters because ``{chat}_{message}_{type}`` is NOT the only shape in
        use: the importer writes ``import_{chat}_{message}``
        (telegram_archive/telegram_import.py). Rebuilding invented a cursor those archives
        have no row for, ``get_media_paginated`` answered the empty page it
        answers for any unresolvable cursor, and #266 stayed unfixed for
        exactly those users — silently, with nothing to show for it.
        """
        out = _run_setup_helpers(
            self.html,
            (
                "const audioMessageChatId = (msg) =>",
                "const audioMessageMediaId = (msg) =>",
            ),
            "cases.map(audioMessageMediaId)",
            prelude="""
const selectedChat = { value: null }
const cases = [
    // Imported archive: the rebuild would have produced "12_voice".
    { id: 12, chat_id: -100, media: { id: 'import_-100_12', type: 'voice' } },
    // A payload predating the v8.0 chat-free rewrite: delivered verbatim, and
    // the server answers the empty page it answers for any unresolvable cursor.
    { id: 12, chat_id: -100, media: { id: '-100_12_voice', type: 'voice' } },
    // Numeric ids survive the trip as cursor strings.
    { id: 12, chat_id: -100, media: { id: 4242, type: 'voice' } },
    // No id delivered -> the documented best-effort (chat-free) rebuild.
    { id: 12, chat_id: -100, media: { type: 'voice' } },
]
""",
        )
        self.assertEqual(out, ["import_-100_12", "-100_12_voice", "4242", "12_voice"])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_forward_walk_stops_on_a_cursor_it_has_already_asked_from(self) -> None:
        """The forward loop is unbounded by design, so it needs a real progress rule.

        "Nothing playable on this page" must not end auto-advance (that is #266
        one page further along), so the loop keeps going. Comparing the new
        cursor only against the PREVIOUS one leaves a response that alternates
        A -> B -> A spinning forever. A forward walk never legitimately revisits
        a cursor, so a repeat — of any age — is proof of no progress.

        The stub throws once the walk exceeds a sane number of requests, so a
        regression surfaces as ``'error'`` instead of hanging the suite.
        """
        prelude = """
const noDownload = { value: false }
const audioError = { value: '' }
const audioQueue = { value: [] }
const audioTrack = { value: { chatId: 7, chatRef: 'r7', kind: 'voice', id: 10 } }
const REQUESTED = []
let CALLS = 0
// Downloaded but unplayable for this viewer, so no page ever adds a track and
// the loop is never allowed to exit on "extended".
const unplayableItem = (n) => ({
    id: `m${n}`, message_id: n, chat_id: 7, media_url: '',
    message_date: `2026-07-01T00:00:${String(n).padStart(2, '0')}`,
})
const fetchAudioQueuePage = async (chatId, kind, cursor, direction = 'newer') => {
    REQUESTED.push([cursor ?? null, direction])
    if (++CALLS > 20) throw new Error('forward walk never terminated')
    // Always promises more, and swings the cursor back and forth between two
    // values it has already handed out.
    return { items: [unplayableItem(cursor === 'm11' ? 12 : 11)], has_more: true }
}
"""
        epilogue = """
(async () => {
    audioQueueCursorNewer = 'm10'
    audioQueueHasNewer = true
    const outcome = await extendAudioQueueNewer()
    console.log(JSON.stringify({ outcome, requested: REQUESTED, hasNewer: audioQueueHasNewer }))
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const AUDIO_QUEUE_MAX_PAGES = ",
                "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>",
                "const audioTracksFromMediaItems = (items, track) =>",
                "const audioTrackTime = (track) =>",
                "const mergeAudioQueue = (tracks) =>",
                "const audioQueueBelongsToTrack = (track) =>",
                "const extendAudioQueueNewer = async () =>",
            ),
            prelude,
            epilogue,
        )
        # m10 -> m11 -> m12 -> m11 (already asked) -> stop. Three requests, and
        # 'exhausted' rather than the stub's runaway error.
        self.assertEqual(out["outcome"], "exhausted")
        self.assertEqual(out["requested"], [["m10", "newer"], ["m11", "newer"], ["m12", "newer"]])
        self.assertFalse(out["hasNewer"])


# Setup-scope stubs for EXECUTING the real message-pane producers
# (resetMessagePagination / loadMessages / loadMessagesAroundId) under node, so
# a test can ask what each one DECLARES about the window it just wrote.
# ``PANE_ROWS`` is what the stubbed endpoint returns, ``TOPIC`` is what
# ``activeTopicId()`` reports, and ``URLS`` records the requests actually built.
_PRODUCER_PRELUDE = """
const messages = { value: [] }
const page = { value: 0 }
const hasMore = { value: true }
const loading = { value: false }
const hasMoreNewer = { value: false }
const loadingNewer = { value: false }
const newerLoadError = { value: '' }
const messagesLoadError = { value: false }
const viewingPinnedWindow = { value: false }
const unseenMessageCount = { value: 0 }
const messageHighlight = { value: null }
const selectedMessage = { value: null }
const previewFallback = { value: {} }
const messageWindowIsContiguous = { value: false }
const isAuthenticated = { value: true }
const messageSearchQuery = { value: '' }
const deletedOnly = { value: false }
const editedOnly = { value: false }
const messageFilterOn = () => !!messageSearchQuery.value || deletedOnly.value || editedOnly.value
const selectedChat = { value: { id: 7, ref: 'r7', title: 'chat' } }
let oldestMessageCursor = null
let loadFailureStreak = 0
let newestMessageId = null
let newerLoadRequestSeq = 0
let chatVersion = 0
let messagesScrollObserver = null
const loadMoreSentinel = { value: null }
let TOPIC = null
const activeTopicId = () => TOPIC
const resetCalendarAvailability = () => {}
const updateOldestMessageCursor = () => {}
const oldestMessageFrom = () => null
const messageCursor = () => null
const showToast = () => {}
const nextTick = async () => {}
const stamp = (n) => `2026-07-01T00:00:${String(n).padStart(2, '0')}`
const messageRow = (n) => ({
    id: n, chat_id: 7, date: stamp(n),
    media: { id: `m${n}`, type: 'voice', duration: 1 },
})
let PANE_ROWS = []
const URLS = []
const fetch = async (url) => {
    URLS.push(url)
    // The after-context half of a jump window comes back SHORT, so the window
    // counts as tail-inclusive instead of a detached jump window.
    return { ok: true, status: 200, json: async () => url.includes('after_id=') ? [] : PANE_ROWS }
}
"""


class TestMessageWindowContiguityIsDeclaredByProducers(unittest.TestCase):
    """#268: contiguity is a POSITIVE property the producer records, not a blacklist.

    Three times in this batch the same defect shipped: an anchor taken from a
    list that is not a contiguous slice of the timeline (#257 backward, then
    pinned-only forward, then search/topic forward). Every one of them came from
    asking "is this one of the sparse views I know about?" — a question that is
    wrong by default and that a newly added filtered view answers incorrectly
    without anyone noticing.

    So the question is inverted here: ``messageWindowIsContiguous`` starts false,
    ``resetMessagePagination`` (the chokepoint every view entry passes through)
    re-clears it, and only a producer that KNOWS it wrote a timeline slice sets
    it. These tests execute the real producers and check what each declares.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_the_flag_defaults_to_sparse(self) -> None:
        """The default is the safety property — a ref that starts true buys nothing."""
        self.assertIn("const messageWindowIsContiguous = ref(false)", self.html)
        reset = _setup_slice(self.html, "const resetMessagePagination = () =>")
        self.assertIn("messageWindowIsContiguous.value = false", reset)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_load_messages_declares_only_the_unfiltered_chat_page(self) -> None:
        """``loadMessages`` builds four different requests; only one is the timeline.

        A search page holds matching rows only. A topic page holds one topic out
        of many, while the audio queue's ``/media`` endpoint is chat-wide — so
        the topic pane's newest row is NOT the newest media before it.
        """
        prelude = _PRODUCER_PRELUDE
        epilogue = """
(async () => {
    const shape = async (search, topic) => {
        TOPIC = topic
        messageSearchQuery.value = search
        messages.value = []
        loading.value = false
        URLS.length = 0
        resetMessagePagination()
        const afterReset = messageWindowIsContiguous.value
        await loadMessages()
        return { afterReset, contiguous: messageWindowIsContiguous.value, url: URLS[0] }
    }
    console.log(JSON.stringify({
        plain: await shape('', null),
        search: await shape('needle', null),
        topic: await shape('', 4),
        searchInTopic: await shape('needle', 4),
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
            prelude,
            epilogue,
        )
        # resetMessagePagination clears it every time, whatever the previous view left.
        for name, result in out.items():
            self.assertFalse(result["afterReset"], name)
        self.assertTrue(out["plain"]["contiguous"])
        self.assertFalse(out["search"]["contiguous"])
        self.assertFalse(out["topic"]["contiguous"])
        self.assertFalse(out["searchInTopic"]["contiguous"])
        # ...and the classification really does track the request that was sent.
        self.assertNotIn("search=", out["plain"]["url"])
        self.assertNotIn("topic_id=", out["plain"]["url"])
        self.assertIn("search=needle", out["search"]["url"])
        self.assertIn("topic_id=4", out["topic"]["url"])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_jump_window_is_contiguous_unless_it_is_topic_scoped(self) -> None:
        """Both halves of a jump window are pages hinged on the same id.

        That makes it a real slice of the timeline — and the whole point of
        keeping the flag rather than banning anchors outright, because a normal
        window must still arm the forward cursor from its own edge with no extra
        request. A topic-scoped window is a slice of the TOPIC, not the chat.
        """
        # loadMessagesAroundId is followed by a `let` and a `watch(...)`, not by
        # another `const`, so it is lifted brace-matched and handed over in the
        # prelude — still VERBATIM template code, just cut at the right place.
        prelude = (
            _PRODUCER_PRELUDE
            + """
const newestLoadedMessageId = () => 9
const setupMessagesScrollObserver = () => {}
const scrollToMessage = () => {}
const startMessageRefresh = () => {}
let messagesNewerObserver = null
const loadNewerSentinel = { value: null }
"""
            + _setup_function(self.html, "const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
        )
        epilogue = """
(async () => {
    const jump = async (topic) => {
        TOPIC = topic
        messageSearchQuery.value = ''
        messages.value = []
        loading.value = false
        URLS.length = 0
        await loadMessagesAroundId(5)
        return { contiguous: messageWindowIsContiguous.value, urls: URLS.slice() }
    }
    console.log(JSON.stringify({ plain: await jump(null), topic: await jump(4) }))
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const messageIdKey = (msg) =>",
                "const upsertMessages = (incomingMessages, ",
                "const resetMessagePagination = () =>",
            ),
            prelude,
            epilogue,
        )
        self.assertTrue(out["plain"]["contiguous"])
        self.assertFalse(out["topic"]["contiguous"])
        # The declaration survives resetMessagePagination, which runs in between and
        # clears the flag by design.
        self.assertEqual(len(out["plain"]["urls"]), 2)
        for url in out["topic"]["urls"]:
            self.assertIn("topic_id=4", url)


class TestAudioQueueForwardCursorContiguity(unittest.TestCase):
    """#268: #257's hole, recreated in the FORWARD direction by a sparse seed.

    ``sortedMessages`` is not always a contiguous slice of the timeline. The
    pinned-only view swaps in ``pinnedMessages`` (entries months apart); an
    in-chat search leaves only matching rows; a forum topic leaves one topic's
    rows while the audio queue's ``/media`` endpoint is chat-wide.
    ``buildAudioQueue`` seeded the queue from any of them, so two tracks that are
    nowhere near each other in the chat ended up ADJACENT in the queue, and
    ``playAudioMessage`` then armed the forward cursor from that sparse queue's
    newest entry — cementing the gap, because the first page forward starts past
    everything in between.

    The rule the whole batch turns on: adjacent queue entries must be adjacent
    in time. An anchor is only usable when it is contiguous with the real
    timeline — the playing track's own media id, or the edge of a window that
    really is a slice of the chat.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_the_gate_asks_a_positive_question_instead_of_listing_views(self) -> None:
        """A predicate that enumerates the sparse views is the defect, not the fix."""
        contiguous = _setup_slice(self.html, "const audioQueueSeedIsContiguous = () =>")
        self.assertIn("messageView.value.contiguous", contiguous)
        # No view names in the gate: adding a filtered view must not require
        # editing it, because whoever adds one will not know it exists.
        for view in ("showPinnedOnly", "messageSearchQuery", "activeTopicId", "selectedPaneTopic"):
            self.assertNotIn(view, contiguous, view)

        build_body = _setup_slice(self.html, "const buildAudioQueue = (msg) =>")
        # A sparse view contributes the clicked track and nothing else...
        self.assertIn("if (!audioQueueSeedIsContiguous()) return [audioTrackFromMessage(msg)]", build_body)
        # ...and that check precedes every read of the sparse list.
        self.assertLess(
            build_body.index("audioQueueSeedIsContiguous()"),
            build_body.index("sortedMessages.value"),
        )

    def test_rows_and_their_contiguity_cannot_diverge(self) -> None:
        """One branch yields both, so a new view cannot return rows and forget the flag."""
        view = _setup_slice(self.html, "const messageView = computed(() =>")
        self.assertIn("rows: pinnedMessages.value, contiguous: false", view)
        self.assertIn("contiguous: messageWindowIsContiguous.value", view)
        # sortedMessages is a projection of that pair — not a second, driftable branch.
        self.assertIn("const sortedMessages = computed(() => messageView.value.rows)", self.html)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_playback_never_skips_a_track_from_any_sparse_view(self) -> None:
        """Auto-advance over a REAL 12-track chat, entered from each sparse view.

        End to end and all real code: the producer (``loadMessages``) fills the
        pane and declares its contiguity, ``messageView`` turns that into the
        gate's answer, and ``playAudioMessage``/``playNextAudio`` walk the chat.
        The stubbed endpoint is a genuine cursor-paged media table, so the walk
        has to be right rather than merely plausible.

        Pre-fix each sparse view played the gap it was shown: pinned-only
        2 -> 11 -> 12, search 2 -> 11 -> 12, topic 2 -> 6 -> 11 -> 12. Tracks
        3-10 were never asked for at all.
        """
        prelude = (
            _PRODUCER_PRELUDE
            + """
const computed = (fn) => ({ get value() { return fn() } })
const pinnedMessages = { value: [] }
const showPinnedOnly = { value: false }
// Stand-in for the date sort: in this fixture message ids ascend with time.
const sortedLoadedMessages = () => [...messages.value].sort((a, b) => Number(b.id) - Number(a.id))

const noDownload = { value: false }
const audioError = { value: '' }
const audioQueue = { value: [] }
const audioTrack = { value: null }
const audioAutoAdvanceHalted = { value: false }
let audioConsecutiveFailures = 0
const getChatName = (chat) => chat.title
const getMediaUrl = (msg) => `/media/${msg.id}.ogg`
const getSenderName = () => 'someone'
const isAudioFile = (msg) => msg.media?.type === 'voice'
const isCurrentAudioMessage = () => false
const toggleAudioPlayback = () => {}
const PLAYED = []
const loadAudioTrack = (track) => { audioTrack.value = track; PLAYED.push(track.id) }
// A small page size so the forward walk really pages instead of getting the
// whole chat in one answer.
const PAGE = 3
const mediaRow = (n) => ({
    id: `m${n}`, message_id: n, chat_id: 7,
    media_url: `/media/${n}.ogg`, message_date: stamp(n),
})
// The chat's real audio timeline, oldest -> newest. Contiguous by definition:
// any gap in what gets PLAYED is a gap the client invented.
const MEDIA = Array.from({ length: 12 }, (_, i) => mediaRow(i + 1))
const REQUESTED = []
// A faithful stand-in for get_media_paginated: an opaque cursor resolved
// against the table, paged away from it in the requested direction, answering
// an unresolvable cursor with an empty page in EITHER direction.
const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') => {
    REQUESTED.push([cursor ?? null, direction])
    const at = cursor ? MEDIA.findIndex(m => m.id === cursor) : -1
    if (cursor && at < 0) return { items: [], has_more: false }
    const rest = direction === 'newer'
        ? MEDIA.slice(at + 1)
        : (at < 0 ? MEDIA.slice() : MEDIA.slice(0, at)).reverse()
    return { items: rest.slice(0, PAGE), has_more: rest.length > PAGE }
}
// playAudioMessage deliberately does NOT await the queue growth (the click
// gesture has to reach play() first), so drain the microtask/timer queues.
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)) }
"""
        )
        epilogue = """
const run = async ({ search = '', topic = null, pinnedIds = null, rowIds }, clickedId) => {
    REQUESTED.length = 0
    PLAYED.length = 0
    audioQueue.value = []
    audioTrack.value = null
    audioQueueCursor = null
    audioQueueHasOlder = false
    audioQueueCursorNewer = null
    audioQueueHasNewer = false
    audioQueueFetching = false
    // The REAL producer fills the pane and declares what it wrote. The message
    // pane renders newest-first.
    PANE_ROWS = rowIds.slice().sort((a, b) => b - a).map(messageRow)
    TOPIC = topic
    messageSearchQuery.value = search
    messages.value = []
    loading.value = false
    resetMessagePagination()
    await loadMessages()
    // The pinned-only view is entered on top of whatever the producer wrote —
    // here a genuinely contiguous window, so only the view branch can catch it.
    showPinnedOnly.value = pinnedIds != null
    pinnedMessages.value = (pinnedIds || []).slice().sort((a, b) => b - a).map(messageRow)

    playAudioMessage(messageRow(clickedId))
    await flush()
    const seeded = { queue: audioQueue.value.map(t => t.id), requested: REQUESTED.slice() }
    for (let i = 0; i < 40; i++) {
        if (!await playNextAudio()) break
    }
    return {
        visible: sortedMessages.value.map(m => m.id),
        contiguous: messageView.value.contiguous,
        played: PLAYED.slice(), seeded, requested: REQUESTED.slice(),
    }
};
(async () => {
    // Two pinned voice notes nine tracks apart, over a contiguous window.
    const pinned = await run({ rowIds: [1, 2, 3, 4, 5], pinnedIds: [2, 11] }, 2)
    // An in-chat search: only the matching rows survive.
    const search = await run({ search: 'needle', rowIds: [2, 11] }, 2)
    // A forum topic: this topic's rows, scattered through a chat-wide timeline.
    const topic = await run({ topic: 4, rowIds: [2, 6, 11] }, 2)
    // The ordinary view: a contiguous window of the same chat.
    const window = await run({ rowIds: [1, 2, 3, 4, 5] }, 2)
    console.log(JSON.stringify({ pinned, search, topic, window }))
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const messageIdKey = (msg) =>",
                "const upsertMessages = (incomingMessages, ",
                "const resetMessagePagination = () =>",
                "const loadMessages = async () =>",
                "const messageView = computed(() =>",
                "const sortedMessages = computed(() => messageView.value.rows)",
                "const AUDIO_QUEUE_MAX_PAGES = ",
                "const audioMediaKind = (msg) =>",
                "const audioMessageChatId = (msg) =>",
                "const audioMessageMediaId = (msg) =>",
                "const audioTrackFromMessage = (msg) =>",
                "const audioQueueSeedIsContiguous = () =>",
                "const buildAudioQueue = (msg) =>",
                "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>",
                "const audioTracksFromMediaItems = (items, track) =>",
                "const audioTrackTime = (track) =>",
                "const mergeAudioQueue = (tracks) =>",
                "const audioQueueBelongsToTrack = (track) =>",
                "const audioQueueEdgeTrack = (newest) =>",
                "const audioQueueEdgeMediaId = (newest) =>",
                "const seedAudioQueueAroundTrack = async (track) =>",
                "const extendAudioQueueFromMedia = async (track) =>",
                "const extendAudioQueueNewer = async () =>",
                "const currentAudioQueueIndex = () =>",
                "const playAdjacentAudio = (step) =>",
                "const playNextAudio = async () =>",
                "const playAudioMessage = (msg) =>",
            ),
            prelude,
            epilogue,
        )

        # The fixtures really are the sparse views they claim to be...
        self.assertEqual(out["pinned"]["visible"], [11, 2])
        self.assertEqual(out["search"]["visible"], [11, 2])
        self.assertEqual(out["topic"]["visible"], [11, 6, 2])
        # ...and each is classified sparse, the pinned one DESPITE sitting on a
        # window its producer declared contiguous.
        for name in ("pinned", "search", "topic"):
            with self.subTest(view=name):
                self.assertFalse(out[name]["contiguous"])

        # THE assertion: every track from the clicked one to the end of the chat,
        # in order, with nothing skipped. subTest so a regression names EVERY
        # view it broke, not just the first one checked.
        for name in ("pinned", "search", "topic"):
            with self.subTest(view=name):
                self.assertEqual(out[name]["played"], [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
                # The sparse view contributes no neighbours at all — the seed is
                # the clicked track plus the page anchored on it, both contiguous.
                self.assertEqual(out[name]["seeded"]["queue"], [1, 2])
                self.assertEqual(out[name]["seeded"]["requested"], [["m2", "older"]])
                # ...so the FIRST forward page is anchored on the playing track
                # itself, never on the far-away visible row (that would be "m11").
                forward = [r for r in out[name]["requested"] if r[1] == "newer"]
                self.assertEqual(forward[0], ["m2", "newer"])

        # No regression on the contiguous path: the window is still a valid
        # anchor, so the first forward page starts at its EDGE rather than
        # re-fetching tracks the window already provided — and costs no extra
        # request to arm.
        self.assertTrue(out["window"]["contiguous"])
        self.assertEqual(out["window"]["played"], [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        self.assertEqual(out["window"]["seeded"]["queue"], [1, 2, 3, 4, 5])
        window_forward = [r for r in out["window"]["requested"] if r[1] == "newer"]
        self.assertEqual(window_forward[0], ["m5", "newer"])


# Shared stubs for the two "the one-entry sparse queue must keep making
# progress" programs below. Both drive REAL helpers over a 12-track chat whose
# media endpoint is a faithful stand-in for ``get_media_paginated``: an opaque
# cursor resolved against the table, paged away from it in the requested
# direction. ``PLAYED`` is the only record of what actually reached the element,
# which is what makes these tests behavioural rather than flag-shaped.
_SPARSE_PROGRESS_PRELUDE = """
const noDownload = { value: false }
const audioError = { value: '' }
const audioQueue = { value: [] }
const audioTrack = { value: null }
const audioAutoAdvanceHalted = { value: false }
let audioConsecutiveFailures = 0
const selectedChat = { value: { id: 7, ref: 'r7', title: 'chat' } }
// The clicked row comes from a SPARSE view (search results), which is what
// leaves the queue one entry long until the anchored seed lands.
const messageView = { value: { contiguous: false } }
const sortedMessages = { value: [] }
const getChatName = (chat) => chat.title
const getMediaUrl = (msg) => `/media/${msg.id}.ogg`
const getSenderName = () => 'someone'
const isAudioFile = (msg) => msg.media?.type === 'voice'
const isCurrentAudioMessage = () => false
const toggleAudioPlayback = () => {}
const PLAYED = []
const loadAudioTrack = (track) => { audioTrack.value = track; PLAYED.push(track.id) }
const SEEKS = []
const seekAudioTo = (seconds) => { SEEKS.push(seconds) }
const stamp = (n) => `2026-07-01T00:00:${String(n).padStart(2, '0')}`
const messageRow = (n) => ({
    id: n, chat_id: 7, date: stamp(n),
    media: { id: `m${n}`, type: 'voice', duration: 1 },
})
// Small pages, so the walk really pages instead of getting the whole chat back
// in one answer.
const PAGE = 3
const mediaRow = (n) => ({
    id: `m${n}`, message_id: n, chat_id: 7,
    media_url: `/media/${n}.ogg`, message_date: stamp(n),
})
const REQUESTED = []
const answerPage = (table, cursor, direction) => {
    const at = cursor ? table.findIndex(m => m.id === cursor) : -1
    if (cursor && at < 0) return { items: [], has_more: false }
    const rest = direction === 'newer'
        ? table.slice(at + 1)
        : (at < 0 ? table.slice() : table.slice(0, at)).reverse()
    return { items: rest.slice(0, PAGE), has_more: rest.length > PAGE }
}
// playAudioMessage deliberately does NOT await the queue growth (the click
// gesture has to reach play() first), so drain the microtask/timer queues.
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)) }
"""


class TestSparseQueueKeepsMakingProgress(unittest.TestCase):
    """#268 follow-up: a one-entry seed queue has no slack for "close enough".

    ``buildAudioQueue`` now contributes ONLY the clicked track from a sparse
    view, so the queue really is one entry long at the start of every sparse
    playback and the anchored pages are the only way out of it. Two places
    treated "no answer yet" and "nothing playable here" as "the queue ended",
    which was survivable while the queue arrived pre-filled from the window and
    is not survivable now.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_track_that_fails_while_its_seed_is_in_flight_still_auto_advances(self) -> None:
        """``playNextAudio`` collapsed 'pending' — a page ON ITS WAY — into false.

        Reachable shape: in search results the user clicks a voice note whose
        file is missing on disk. ``playAudioMessage`` seeds ``[clicked]``, starts
        the track and fires the anchored seed fetch; the element's ``error``
        event and the media request race in the same tick, and when the error
        wins, the handler calls ``playNextAudio`` while the queue still holds one
        entry. ``extendAudioQueueNewer`` answers 'pending' (a fetch is already in
        flight), the old code read that as an exhausted queue, and auto-advance
        was over for the session — with the seed page ~50ms away. The same shape
        reaches the 'ended' handler for a sub-second voice note.

        ``playPrevAudio`` has always kept 'pending' and 'exhausted' apart, so
        this was an inconsistency rather than a decision.

        The fetch is HELD here, which is exactly that window; the scenarios
        differ only in what happens inside it.
        """
        prelude = (
            _SPARSE_PROGRESS_PRELUDE
            + """
const MEDIA = Array.from({ length: 12 }, (_, i) => mediaRow(i + 1))
// The seed page is held until RELEASE is called: everything that happens in
// between happens while the fetch is genuinely in flight.
let RELEASE = null
const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') => {
    REQUESTED.push([cursor ?? null, direction])
    if (direction === 'older' && !RELEASE) {
        return new Promise(resolve => { RELEASE = () => resolve(answerPage(MEDIA, cursor, direction)) })
    }
    return answerPage(MEDIA, cursor, direction)
}
"""
        )
        epilogue = """
const scenario = async (duringHold) => {
    REQUESTED.length = 0
    PLAYED.length = 0
    RELEASE = null
    audioQueue.value = []
    audioTrack.value = null
    audioAutoAdvanceHalted.value = false
    audioQueueCursor = null
    audioQueueHasOlder = false
    audioQueueCursorNewer = null
    audioQueueHasNewer = false
    audioQueueFetching = false
    audioQueueSeeding = null

    playAudioMessage(messageRow(2))
    await flush()
    const seeded = audioQueue.value.map(t => t.id)
    // What the real 'error'/'ended' handlers do: call playNextAudio without
    // awaiting it, while the seed is still in flight.
    const advancing = playNextAudio()
    await flush()
    const beforeRelease = PLAYED.slice()
    if (duringHold) duringHold()
    RELEASE()
    const advanced = await advancing
    await flush()
    return { seeded, beforeRelease, advanced, played: PLAYED.slice(), requested: REQUESTED.slice() }
};
(async () => {
    // 1. Nothing else happens: the page lands and the advance goes through.
    const landsLate = await scenario(null)
    // 2. The user tapped ANOTHER voice note while the page was in flight — one
    //    the landing page puts in the queue, so a re-attempt would walk from
    //    THAT track and replay 2.
    const switched = await scenario(() => {
        audioTrack.value = {
            id: 1, chatId: 7, chatRef: 'r7', kind: 'voice', chatName: 'chat',
            mediaId: 'm1', url: '/media/1.ogg', date: stamp(1),
        }
    })
    // 3. Auto-advance was halted while we waited (a second failed file).
    const halted = await scenario(() => { audioAutoAdvanceHalted.value = true })
    console.log(JSON.stringify({ landsLate, switched, halted }))
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const AUDIO_QUEUE_MAX_PAGES = ",
                "const audioMediaKind = (msg) =>",
                "const audioMessageChatId = (msg) =>",
                "const audioMessageMediaId = (msg) =>",
                "const audioTrackFromMessage = (msg) =>",
                "const audioQueueSeedIsContiguous = () =>",
                "const buildAudioQueue = (msg) =>",
                "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>",
                "const audioTracksFromMediaItems = (items, track) =>",
                "const audioTrackTime = (track) =>",
                "const mergeAudioQueue = (tracks) =>",
                "const audioQueueBelongsToTrack = (track) =>",
                "const audioQueueEdgeTrack = (newest) =>",
                "const audioQueueEdgeMediaId = (newest) =>",
                "const seedAudioQueueAroundTrack = async (track) =>",
                "const extendAudioQueueFromMedia = async (track) =>",
                "const extendAudioQueueNewer = async () =>",
                "const currentAudioQueueIndex = () =>",
                "const playAdjacentAudio = (step) =>",
                "const playNextAudio = async () =>",
                "const playAudioMessage = (msg) =>",
            ),
            prelude,
            epilogue,
        )

        # The premise: the sparse seed really is one entry, and the advance is
        # still undecided while the page is in flight (no track played, and no
        # forward request issued off a cursor that is mid-fetch).
        self.assertEqual(out["landsLate"]["seeded"], [2])
        self.assertEqual(out["landsLate"]["beforeRelease"], [2])

        # THE assertion: the page lands and playback moves on, once. The forward
        # page is asked for AFTER the seed settles, anchored on the playing
        # track — the seed itself only pages older, so it cannot supply track 3.
        self.assertTrue(out["landsLate"]["advanced"])
        self.assertEqual(out["landsLate"]["played"], [2, 3])
        self.assertEqual(out["landsLate"]["requested"], [["m2", "older"], ["m2", "newer"]])

        # ...and the re-attempt is owned. A different track means this advance
        # belongs to nobody: no replay of 2 from track 1's position, and no
        # request either.
        self.assertFalse(out["switched"]["advanced"])
        self.assertEqual(out["switched"]["played"], [2])
        self.assertEqual(out["switched"]["requested"], [["m2", "older"]])

        # Halting auto-advance while the page was in flight must survive the
        # wait — the re-attempt may not resurrect it.
        self.assertFalse(out["halted"]["advanced"])
        self.assertEqual(out["halted"]["played"], [2])
        self.assertEqual(out["halted"]["requested"], [["m2", "older"]])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_an_unplayable_seed_page_does_not_park_the_cursor_on_the_playing_track(self) -> None:
        """The older-cursor fixup could move the cursor BACKWARDS.

        ``seedAudioQueueAroundTrack`` sets the cursor to the page's last item and
        then overwrites it with the queue's own oldest edge — right for its
        intended case (the loaded window already holds more than a page of older
        audio), wrong when the page adds nothing. ``audioTracksFromMediaItems``
        drops every item with no ``media_url``, which is what the server returns
        for media whose ``file_path`` falls outside the media root, so a WHOLE
        page can vanish; the edge is then the oldest track already queued, and
        from a sparse view that is the playing track itself — the very anchor the
        page was fetched from. ``audioQueueHasOlder`` was computed before the
        fixup, so it stays true and 'previous' re-asks the question the seed just
        asked, reports 'exhausted' and restarts the track although older playable
        audio is one page further back.
        """
        prelude = (
            _SPARSE_PROGRESS_PRELUDE
            + """
// Tracks 9-11 are downloaded but outside the media root, so the endpoint hands
// back rows with no media_url and the queue can play none of them. With PAGE=3
// they are exactly the page the seed fetches from track 12.
const MEDIA = Array.from({ length: 12 }, (_, i) => {
    const row = mediaRow(i + 1)
    return (i + 1 >= 9 && i + 1 <= 11) ? { ...row, media_url: '' } : row
})
const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') => {
    REQUESTED.push([cursor ?? null, direction])
    return answerPage(MEDIA, cursor, direction)
}
"""
        )
        epilogue = """
const scenario = async ({ contiguous, visible, clicked }) => {
    REQUESTED.length = 0
    PLAYED.length = 0
    SEEKS.length = 0
    audioQueue.value = []
    audioTrack.value = null
    audioQueueCursor = null
    audioQueueHasOlder = false
    audioQueueCursorNewer = null
    audioQueueHasNewer = false
    audioQueueFetching = false
    audioQueueSeeding = null
    // Reset the auto-advance state too, so a later scenario cannot inherit a
    // halt raised by an earlier one and fail in the wrong place.
    audioAutoAdvanceHalted.value = false
    audioConsecutiveFailures = 0
    messageView.value = { contiguous }
    // The pane renders newest-first.
    sortedMessages.value = visible.slice().sort((a, b) => b - a).map(messageRow)

    playAudioMessage(messageRow(clicked))
    await flush()
    const seeded = {
        queue: audioQueue.value.map(t => t.id),
        cursor: audioQueueCursor,
        hasOlder: audioQueueHasOlder,
        requested: REQUESTED.slice(),
    }
    await playPrevAudio()
    return { seeded, played: PLAYED.slice(), seeks: SEEKS.slice(), requested: REQUESTED.slice() }
};
(async () => {
    // Sparse view: the queue is the clicked track alone, and the page anchored
    // on it is wholly unplayable.
    const unplayable = await scenario({ contiguous: false, visible: [12], clicked: 12 })
    // The case the fixup exists for: a contiguous window holding MORE than one
    // page of audio older than the playing track. Its own oldest entry really
    // is older than the page's last item, so the cursor must still follow it.
    const deepWindow = await scenario({ contiguous: true, visible: [1, 2, 3, 4, 5], clicked: 5 })
    console.log(JSON.stringify({ unplayable, deepWindow }));
})();
"""
        out = _run_setup_program(
            self.html,
            (
                "const AUDIO_QUEUE_MAX_PAGES = ",
                "const audioMediaKind = (msg) =>",
                "const audioMessageChatId = (msg) =>",
                "const audioMessageMediaId = (msg) =>",
                "const audioTrackFromMessage = (msg) =>",
                "const audioQueueSeedIsContiguous = () =>",
                "const buildAudioQueue = (msg) =>",
                "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>",
                "const audioTracksFromMediaItems = (items, track) =>",
                "const audioTrackTime = (track) =>",
                "const mergeAudioQueue = (tracks) =>",
                "const audioQueueBelongsToTrack = (track) =>",
                "const audioQueueEdgeTrack = (newest) =>",
                "const audioQueueEdgeMediaId = (newest) =>",
                "const seedAudioQueueAroundTrack = async (track) =>",
                "const extendAudioQueueFromMedia = async (track) =>",
                "const extendAudioQueueOlder = async () =>",
                "const currentAudioQueueIndex = () =>",
                "const playAdjacentAudio = (step) =>",
                "const playPrevAudio = async () =>",
                "const playAudioMessage = (msg) =>",
            ),
            prelude,
            epilogue,
        )

        # The premise: one anchored request, and it contributed nothing playable.
        self.assertEqual(out["unplayable"]["seeded"]["queue"], [12])
        self.assertEqual(out["unplayable"]["seeded"]["requested"], [["m12", "older"]])
        self.assertTrue(out["unplayable"]["seeded"]["hasOlder"])

        # THE assertion: the cursor is the page's own last item, NOT the playing
        # track, so 'previous' walks PAST the unplayable page instead of asking
        # for it again — and lands on real audio rather than restarting track 12.
        self.assertEqual(out["unplayable"]["seeded"]["cursor"], "m9")
        self.assertEqual(out["unplayable"]["requested"], [["m12", "older"], ["m9", "older"]])
        self.assertEqual(out["unplayable"]["played"], [12, 8])
        self.assertEqual(out["unplayable"]["seeks"], [])

        # No regression on what the fixup is for: the window holds tracks 1-4
        # older than the playing one while the page only reaches back to 2, so
        # the cursor follows the QUEUE's edge and paging does not re-fetch rows
        # the queue already has.
        self.assertEqual(out["deepWindow"]["seeded"]["queue"], [1, 2, 3, 4, 5])
        self.assertEqual(out["deepWindow"]["seeded"]["cursor"], "m1")
        # 'previous' is served from the window itself — no request at all.
        self.assertEqual(out["deepWindow"]["requested"], [["m5", "older"]])
        self.assertEqual(out["deepWindow"]["played"], [5, 4])


# Setup-scope stubs for EXECUTING the whole APPEND path end to end: the real
# ``loadMessages`` fills the pane, the real ``checkForNewMessages`` and
# ``handleWebSocketMessage`` bring arrivals in through the real
# ``upsertMessages``, and the real audio queue then plays what that leaves
# behind. ``SERVER`` is the archive's message table (ids ascending with time)
# and ``PANE_PAGE`` bounds every page the message endpoint returns — exactly
# what ``limit=50`` does in production, shrunk so the ids stay readable.
_APPEND_PRELUDE = """
// The real poll logs what it added; keep stdout to the one JSON line the runner
// parses, and hand the tests a private channel for it (``emit``).
const emit = console.log.bind(console)
console.log = () => {}
const ref = (v) => ({ value: v })
const computed = (fn) => ({ get value() { return fn() } })
const moment = { utc: (s) => { const t = Date.parse(/(Z|[+-]\\d\\d:?\\d\\d)$/.test(s) ? s : s + 'Z')
    return { isValid: () => !Number.isNaN(t), valueOf: () => t } } }
const messages = ref([])
const page = ref(0)
const hasMore = ref(true)
const loading = ref(false)
const hasMoreNewer = ref(false)
const loadingNewer = ref(false)
const newerLoadError = ref('')
const messagesLoadError = ref(false)
const viewingPinnedWindow = ref(false)
const unseenMessageCount = ref(0)
const messageHighlight = ref(null)
const selectedMessage = ref(null)
const previewFallback = ref({})
const isAuthenticated = ref(true)
const messageSearchQuery = ref('')
const deletedOnly = ref(false)
const editedOnly = ref(false)
const messageFilterOn = () => !!messageSearchQuery.value || deletedOnly.value || editedOnly.value
const selectedChat = ref({ id: 7, ref: 'r7', title: 'chat' })
const selectedPaneTopic = ref(null)
const pinnedMessages = ref([])
const showPinnedOnly = ref(false)
const messagesContainer = ref({ scrollTop: 0 })
const notificationPermission = ref('denied')
const document = { hidden: false }
let newestMessageId = null
let newerLoadRequestSeq = 0
let chatVersion = 0
let isRefreshing = false
let messagesScrollObserver = null
const loadMoreSentinel = ref(null)
const GENERAL_TOPIC_ID = 1
let TOPIC = null
const activeTopicId = () => TOPIC
const resetCalendarAvailability = () => {}
const showToast = () => {}
const showNotification = () => {}
const clearMessageVersionsCache = () => {}
const isVersionsPanelOpenFor = () => false
const loadMessageVersions = () => {}
const loadPinnedMessages = () => {}
const scrollToBottom = () => {}
const nextTick = async (fn) => { if (fn) fn() }

// The archive: message ids ascending with time, every one a voice note.
const stamp = (n) => `2026-07-01T00:00:${String(n).padStart(2, '0')}`
const messageRow = (n) => ({
    id: n, chat_id: 7, date: stamp(n),
    media: { id: `m${n}`, type: 'voice', duration: 1 },
})
let SERVER = []
// Every page the endpoint returns is BOUNDED. That bound is the whole reason a
// poll can fail to reach back to the loaded window.
const PANE_PAGE = 3
const URLS = []
const fetch = async (url) => {
    URLS.push(url)
    const params = new URL(url, 'http://viewer').searchParams
    const beforeId = params.get('before_id')
    const afterId = params.get('after_id')
    let rows = SERVER.slice()
    if (afterId != null) {
        rows = rows.filter(n => n > Number(afterId)).slice(0, PANE_PAGE)
    } else {
        if (beforeId != null) rows = rows.filter(n => n < Number(beforeId))
        rows = rows.slice(-PANE_PAGE)
    }
    rows = rows.slice().reverse().map(messageRow)   // newest-first response contract
    return { ok: true, status: 200, json: async () => rows }
}

// ---- audio queue: the media endpoint, paged like the real one ----
const noDownload = ref(false)
const audioError = ref('')
const audioQueue = ref([])
const audioTrack = ref(null)
const audioAutoAdvanceHalted = ref(false)
let audioConsecutiveFailures = 0
const getChatName = (chat) => chat.title
const getMediaUrl = (msg) => `/media/${msg.id}.ogg`
const getSenderName = () => 'someone'
const isAudioFile = (msg) => msg.media?.type === 'voice'
const isCurrentAudioMessage = () => false
const toggleAudioPlayback = () => {}
const PLAYED = []
const loadAudioTrack = (track) => { audioTrack.value = track; PLAYED.push(track.id) }
const MEDIA_PAGE = 3
const REQUESTED = []
const fetchAudioQueuePage = async (chatRef, kind, cursor, direction = 'older') => {
    REQUESTED.push([cursor ?? null, direction])
    const table = SERVER.map(n => ({
        id: `m${n}`, message_id: n, chat_id: 7,
        media_url: `/media/${n}.ogg`, message_date: stamp(n),
    }))
    const at = cursor ? table.findIndex(m => m.id === cursor) : -1
    if (cursor && at < 0) return { items: [], has_more: false }
    const rest = direction === 'newer'
        ? table.slice(at + 1)
        : (at < 0 ? table.slice() : table.slice(0, at)).reverse()
    return { items: rest.slice(0, MEDIA_PAGE), has_more: rest.length > MEDIA_PAGE }
}
// playAudioMessage deliberately does NOT await the queue growth (the click
// gesture has to reach play() first), so drain the microtask/timer queues.
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)) }
"""

# Driving helpers shared by every test in the class below: open a chat through
# the REAL producer, then report what the window and the audio queue became.
_APPEND_EPILOGUE_HELPERS = """
const openChat = async (ids) => {
    SERVER = ids.slice()
    messages.value = []
    loading.value = false
    messageSearchQuery.value = ''
    URLS.length = 0
    resetMessagePagination()
    await loadMessages()
}
const windowState = () => ({
    visible: sortedMessages.value.map(m => m.id),
    contiguous: messageView.value.contiguous,
})
const playFrom = async (clickedId) => {
    REQUESTED.length = 0
    PLAYED.length = 0
    audioQueue.value = []
    audioTrack.value = null
    audioQueueCursor = null
    audioQueueHasOlder = false
    audioQueueCursorNewer = null
    audioQueueHasNewer = false
    audioQueueFetching = false
    playAudioMessage(messageRow(clickedId))
    await flush()
    const seeded = { queue: audioQueue.value.map(t => t.id), requested: REQUESTED.slice() }
    const armedFrom = audioQueueCursorNewer
    for (let i = 0; i < 60; i++) {
        if (!await playNextAudio()) break
    }
    return { seeded, armedFrom, played: PLAYED.slice(), requested: REQUESTED.slice() }
}
"""

_APPEND_DECLARATIONS = (
    "const STICK_TO_BOTTOM_PX = ",
    "const sortTimeCache = new WeakMap()",
    "const messageSortTime = (msg) =>",
    "const compareMessagesDesc = (a, b) =>",
    # Overshoots onto `let oldestMessageCursor` / `let loadFailureStreak`, which
    # is what we want: those two are the real pagination state, lifted verbatim.
    "const sortedLoadedMessages = () =>",
    "const messageWindowIsContiguous = ref(false)",
    "const messageIdKey = (msg) =>",
    "const messageCursor = (msg) =>",
    "const oldestMessageFrom = (messageList) =>",
    "const updateOldestMessageCursor = (loadedMessages) =>",
    "const newestLoadedMessageId = () =>",
    "const resetMessagePagination = () =>",
    "const messageBelongsToCurrentTopic = (msg) =>",
    "const appendKeepsWindowContiguous = (added, overlapsWindow) =>",
    "const upsertMessages = (incomingMessages, ",
    "const isNearMessageBottom = (container) =>",
    "const checkForNewMessages = async () =>",
    "const loadMessages = async () =>",
    "const messageView = computed(() =>",
    "const sortedMessages = computed(() => messageView.value.rows)",
    "const AUDIO_QUEUE_MAX_PAGES = ",
    "const audioMediaKind = (msg) =>",
    "const audioMessageChatId = (msg) =>",
    "const audioMessageMediaId = (msg) =>",
    "const audioTrackFromMessage = (msg) =>",
    "const audioQueueSeedIsContiguous = () =>",
    "const buildAudioQueue = (msg) =>",
    "const audioTrackFromMediaItem = (item, kind, chatName, chatRef) =>",
    "const audioTracksFromMediaItems = (items, track) =>",
    "const audioTrackTime = (track) =>",
    "const mergeAudioQueue = (tracks) =>",
    "const audioQueueBelongsToTrack = (track) =>",
    "const audioQueueEdgeTrack = (newest) =>",
    "const audioQueueEdgeMediaId = (newest) =>",
    "const seedAudioQueueAroundTrack = async (track) =>",
    "const extendAudioQueueFromMedia = async (track) =>",
    "const extendAudioQueueNewer = async () =>",
    "const currentAudioQueueIndex = () =>",
    "const playAdjacentAudio = (step) =>",
    "const playNextAudio = async () =>",
    "const playAudioMessage = (msg) =>",
)


class TestAppendsCannotPunchAHoleInTheWindow(unittest.TestCase):
    """#268, fourth sighting: the hole opened by an APPEND, not by a view.

    ``messageWindowIsContiguous`` was written only by the wholesale producers,
    so a window they had declared contiguous stayed "contiguous" while other
    code appended to it. Two appenders can land past the loaded window:

    * the 3s poll fetches the LATEST page. More arrivals than fit in one page
      and that page no longer reaches back to the window — the rows in between
      are never fetched, and nothing heals them (later polls fetch the same
      latest page; "load older" pages the other way).
    * the WebSocket ``new_message`` frame after a reconnect gap appends a single
      row from far past the window's edge.

    Either way the pane held two blocks with a gap between them while still
    claiming to be one slice of the timeline, so the audio queue seeded straight
    across the gap — adjacent queue entries that are not adjacent in time, which
    is the defect this whole batch is about.

    The check therefore lives in ``upsertMessages``, the single append path all
    of them pass through, and it only ever CLEARS: declaring a window contiguous
    stays the privilege of the producer that wrote it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, epilogue: str) -> Any:
        return _run_setup_program(
            self.html,
            _APPEND_DECLARATIONS,
            _APPEND_PRELUDE + _setup_function(self.html, "const handleWebSocketMessage = (data) => {"),
            _APPEND_EPILOGUE_HELPERS + epilogue,
        )

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_burst_bigger_than_one_page_leaves_the_window_sparse(self) -> None:
        """More arrivals than one page fit: the poll cannot reach back, so playback must not either.

        Pre-fix the poll left the pane holding 12, 11, 10, 5, 4, 3 — messages
        6-9 never fetched — and still called it contiguous, so the queue seeded
        [1, 2, 3, 4, 5, 10, 11, 12], armed the forward cursor from m12 (past
        everything missing) and auto-advance PLAYED [3, 4, 5, 10, 11, 12]: four
        voice notes silently skipped.
        """
        out = self._run("""
(async () => {
    await openChat([1, 2, 3, 4, 5])
    const before = windowState()
    // Seven messages arrive between two ticks; the poll's page holds three.
    SERVER = Array.from({ length: 12 }, (_, i) => i + 1)
    await checkForNewMessages()
    const after = windowState()
    emit(JSON.stringify({ before, after, playback: await playFrom(3) }))
})();
""")
        # The window really is the sparse one the poll produced.
        self.assertEqual(out["before"]["visible"], [5, 4, 3])
        self.assertTrue(out["before"]["contiguous"])
        self.assertEqual(out["after"]["visible"], [12, 11, 10, 5, 4, 3])
        # THE flag: the append did not adjoin (lowest new id 10, window newest
        # 5, and no row in common), so the window is no longer a timeline slice.
        self.assertFalse(out["after"]["contiguous"])
        # THE consequence: nothing skipped between the click and the end of the
        # chat, where pre-fix it played [3, 4, 5, 10, 11, 12].
        self.assertEqual(out["playback"]["played"], [3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        # The sparse pane contributes no neighbours at all — the seed is the
        # clicked track plus the page anchored on it.
        self.assertEqual(out["playback"]["seeded"]["queue"], [1, 2, 3])
        self.assertEqual(out["playback"]["armedFrom"], "m3")

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_burst_smaller_than_one_page_keeps_the_optimisation(self) -> None:
        """The ordinary case must stay cheap: the poll's page still overlaps the window.

        A check that cleared on every append would cost the queue an anchored
        request per poll tick forever, which is why "adjoins" is a real
        question and not just "did anything arrive".
        """
        out = self._run("""
(async () => {
    await openChat([1, 2, 3, 4, 5])
    // Two arrivals: the latest page (7, 6, 5) still carries a row we hold.
    SERVER = [1, 2, 3, 4, 5, 6, 7]
    await checkForNewMessages()
    const after = windowState()
    emit(JSON.stringify({ after, playback: await playFrom(3) }))
})();
""")
        self.assertEqual(out["after"]["visible"], [7, 6, 5, 4, 3])
        self.assertTrue(out["after"]["contiguous"])
        # The window is still a usable seed AND a usable anchor: the queue takes
        # its rows, and the forward cursor is armed from the window's own edge
        # with no request at all (the only seeding request is the backward page).
        self.assertEqual(out["playback"]["seeded"]["queue"], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(out["playback"]["seeded"]["requested"], [["m3", "older"]])
        self.assertEqual(out["playback"]["armedFrom"], "m7")
        self.assertEqual(out["playback"]["played"], [3, 4, 5, 6, 7])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_removal_only_tick_leaves_the_flag_alone(self) -> None:
        """A deleted message is not a hole — the timeline no longer has it.

        This is the boundary the check has to respect: the ids in a contiguous
        window are NOT required to be consecutive, so the question can only ever
        be asked about the seam an append lands on. A tick that only removes
        rows appends nothing and must not touch the flag, and the next arrival
        on top of the resulting id gap must not either.
        """
        out = self._run("""
(async () => {
    await openChat([1, 2, 3])
    const before = windowState()
    SERVER = [1, 3]                       // message 2 was hard-deleted upstream
    await checkForNewMessages()
    const afterRemoval = windowState()
    SERVER = [1, 3, 4]                    // ...and now a new message arrives
    await checkForNewMessages()
    emit(JSON.stringify({ before, afterRemoval, afterArrival: windowState() }))
})();
""")
        self.assertEqual(out["before"]["visible"], [3, 2, 1])
        self.assertTrue(out["before"]["contiguous"])
        # The row is gone from the pane, the flag is untouched...
        self.assertEqual(out["afterRemoval"]["visible"], [3, 1])
        self.assertTrue(out["afterRemoval"]["contiguous"])
        # ...and an append over that id gap is still an append that adjoins.
        self.assertEqual(out["afterArrival"]["visible"], [4, 3, 1])
        self.assertTrue(out["afterArrival"]["contiguous"])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_the_socket_frame_is_checked_at_the_same_chokepoint(self) -> None:
        """The WS append is a different caller, not a different rule.

        Pre-fix a frame delivered after a reconnect gap appended one row from
        far past the window's edge and the pane still claimed contiguity, so the
        queue seeded [1, 2, 3, 12] and playback went [3, 12] — nine tracks gone.
        The live case (the very next message) must still be free.
        """
        out = self._run("""
(async () => {
    await openChat([1, 2, 3])
    // The socket was down while 4..12 were archived; on reconnect it delivers 12.
    SERVER = Array.from({ length: 12 }, (_, i) => i + 1)
    handleWebSocketMessage({ type: 'new_message', chat_ref: 'r7', message: messageRow(12) })
    const afterGap = windowState()
    const playback = await playFrom(3)
    // The live case: the pane is the tail and the frame is the next message.
    await openChat([1, 2, 3])
    SERVER = [1, 2, 3, 4]
    handleWebSocketMessage({ type: 'new_message', chat_ref: 'r7', message: messageRow(4) })
    emit(JSON.stringify({ afterGap, playback, live: windowState() }))
})();
""")
        self.assertEqual(out["afterGap"]["visible"], [12, 3, 2, 1])
        self.assertFalse(out["afterGap"]["contiguous"])
        self.assertEqual(out["playback"]["seeded"]["queue"], [1, 2, 3])
        self.assertEqual(out["playback"]["played"], [3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        # ...while the ordinary live frame is the window's own successor.
        self.assertEqual(out["live"]["visible"], [4, 3, 2, 1])
        self.assertTrue(out["live"]["contiguous"])

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_later_page_cannot_re_declare_a_holed_window(self) -> None:
        """Scrolling up after a burst must not undo what the append proved.

        ``loadMessages`` declares contiguity from the REQUEST it built, which is
        only the whole answer for the first page — every later page is appended
        to rows that call never saw. Pre-fix the first scroll-up re-declared the
        holed window contiguous and the queue went back to seeding across the
        gap ([1, 2, 3, 4, 5, 10, 11, 12]), playing [3, 4, 5, 10, 11, 12].
        """
        out = self._run("""
(async () => {
    await openChat([1, 2, 3, 4, 5])
    SERVER = Array.from({ length: 12 }, (_, i) => i + 1)
    await checkForNewMessages()
    const afterBurst = windowState()
    // In production the first page comes back FULL (50 of 50), which is what
    // leaves the scroll-up sentinel armed; this fixture's pages are three rows
    // long, so re-arm it by hand and let the sentinel fire.
    hasMore.value = true
    await loadMessages()
    const afterOlderPage = windowState()
    emit(JSON.stringify({ afterBurst, afterOlderPage, playback: await playFrom(3) }))
})();
""")
        self.assertFalse(out["afterBurst"]["contiguous"])
        # The older page really did land — and the hole really did survive it.
        self.assertEqual(out["afterOlderPage"]["visible"], [12, 11, 10, 5, 4, 3, 2, 1])
        self.assertFalse(out["afterOlderPage"]["contiguous"])
        self.assertEqual(out["playback"]["played"], [3, 4, 5, 6, 7, 8, 9, 10, 11, 12])


class TestAudioBubbleMetadataStaysOnOneLine(unittest.TestCase):
    """#267: "0:12" rendered as 0 / : / 1 / 2, one character per line.

    ``.message-bubble`` sets ``overflow-wrap: anywhere``, which every descendant
    inherits and which drops a text box's MIN-CONTENT width to a single
    character. The duration span is a flex item of a row inside
    ``min-w-0 flex-1``, so once that row is squeezed — the #261 download anchor
    became a sibling in 7.32.0 — it shrinks all the way to that one-character
    minimum. ``whitespace-nowrap`` restores min-content to the whole string,
    which flex ``min-width: auto`` then refuses to shrink below.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        start = cls.html.index('v-else-if="isAudioFile(msg)"')
        cls.bubble = cls.html[start : cls.html.index("<!-- GIFs / Animations", start)]

    def test_the_wrapping_rule_that_makes_nowrap_load_bearing_is_still_there(self) -> None:
        """If this ever goes, the assertions below stop protecting anything."""
        rule_start = self.html.index(".message-bubble {")
        rule = self.html[rule_start : self.html.index("}", rule_start)]
        self.assertIn("overflow-wrap: anywhere;", rule)
        # ...and the squeezable ancestor that lets the row shrink at all.
        self.assertIn('<div class="min-w-0 flex-1">', self.bubble)

    def test_every_span_in_the_metadata_row_is_nowrap(self) -> None:
        """Row-wide, not span-by-span: a NEW status span must not regress it."""
        row_start = self.bubble.index(
            'class="flex flex-wrap items-center gap-x-1.5 h-[18px] overflow-hidden mt-0.5 text-[13px] leading-[18px] tabular-nums text-tg-meta"'
        )
        row = self.bubble[row_start : self.bubble.index("</div>", row_start)]
        spans = re.findall(r"<span\b[^>]*>", row)
        self.assertGreaterEqual(len(spans), 3)
        for span in spans:
            self.assertIn("whitespace-nowrap", span, span)

    def test_the_specific_spans_reported_in_267(self) -> None:
        self.assertIn('<span v-if="msg.media?.duration" class="whitespace-nowrap">', self.bubble)
        self.assertIn('<span v-if="isCurrentAudioMessage(msg)" class="text-tg-quote whitespace-nowrap">', self.bubble)
        self.assertIn('<span v-else-if="noDownload" class="whitespace-nowrap">', self.bubble)
        # "login" for a viewer account, "link" for a share-link session (sessionWord).
        self.assertIn("playback off for this {{ sessionWord }}</span>", self.bubble)

    def test_the_filename_span_keeps_its_own_protection(self) -> None:
        """``truncate`` implies nowrap; the duration span never had either."""
        self.assertIn(
            "<div class=\"voice-title truncate\">{{ msg.media?.type === 'voice' ? 'Voice message' : getDocumentDisplayName(msg) }}</div>",
            self.bubble,
        )


class TestReplyQuoteNamesItsSender(unittest.TestCase):
    """#268: the quote block showed no sender and a bare "Message"."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_markup_delegates_to_the_helpers(self) -> None:
        start = self.html.index('v-if="msg.reply_to_msg_id"')
        block = self.html[start : self.html.index("Forwarded Message Info", start)]
        self.assertIn("{{ replyToLabel(msg) }}", block)
        self.assertIn("{{ replyToSnippet(msg) }}", block)
        # The old inline expression is gone, sender and all.
        self.assertNotIn("msg.reply_to_text || 'Message'", block)
        self.assertNotIn(">Reply to</div>", block)
        # A long sender name must not break per character either (#267's rule
        # applies to this block too — it is inside the same bubble). The colour is
        # the bubble side's quote colour. The quote shows the name alone, as the
        # Telegram apps do; "Reply to" is said to screen readers only.
        self.assertIn('class="font-semibold text-tg-quote truncate"', block)
        self.assertIn('<span class="sr-only">Reply to</span>', block)
        # A solid 3px bar in the quote colour, not a faded 2px one.
        self.assertIn("border-s-[3px] border-tg-quote ", block)
        self.assertNotIn("border-tg-quote/50", block)

    def test_helpers_are_exported_to_the_template(self) -> None:
        self.assertIn("\n                    replyToLabel,\n", self.html)
        self.assertIn("\n                    replyToSnippet,\n", self.html)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_label_and_snippet_executed(self) -> None:
        """Named when the backend names it, silent when it cannot.

        ``reply_to_sender_name`` / ``reply_to_media_type`` are null when the
        replied-to message is not in the archive, and absent entirely on an
        older backend. Both must degrade to the pre-#268 output rather than
        render "undefined" or throw.
        """
        out = _run_setup_helpers(
            self.html,
            (
                "const replyMediaLabels = {",
                "const replyToLabel = (msg) =>",
                "const replyToSnippet = (msg) =>",
            ),
            "cases.map(msg => [replyToLabel(msg), replyToSnippet(msg)])",
            prelude="""
const cases = [
    { reply_to_msg_id: 1, reply_to_sender_name: 'Ada L', reply_to_text: 'see below', reply_to_media_type: null },
    { reply_to_msg_id: 1, reply_to_sender_name: 'Ada L', reply_to_text: null, reply_to_media_type: 'photo' },
    { reply_to_msg_id: 1, reply_to_sender_name: 'Ada L', reply_to_text: null, reply_to_media_type: 'voice' },
    { reply_to_msg_id: 1, reply_to_sender_name: null, reply_to_text: null, reply_to_media_type: null },
    { reply_to_msg_id: 1, reply_to_sender_name: 'Ada L', reply_to_text: null, reply_to_media_type: 'venue' },
    { reply_to_msg_id: 1, reply_to_sender_name: 'Ada L', reply_to_text: null, reply_to_media_type: 'dice' },
    { reply_to_msg_id: 1 },
    {},
    undefined,
]
""",
        )
        self.assertEqual(
            out,
            [
                ["Ada L", "see below"],
                ["Ada L", "Photo"],
                ["Ada L", "Voice message"],
                # Target not in the archive: no name to show.
                ["Reply", "Message"],
                # A venue with no title from the backend: the kind in words.
                ["Ada L", "Location"],
                # Unmapped media kind: the raw type beats the bare word.
                ["Ada L", "dice"],
                # Older backend, neither key present.
                ["Reply", "Message"],
                ["Reply", "Message"],
                ["Reply", "Message"],
            ],
        )


class TestMediaUrlEncoding(unittest.TestCase):
    """#258's successor: the client never assembles a /media/ URL at all.

    v8.0 removed the client-side URL builder — the server hands ref-addressed
    URLs (media.url, media_url, thumb_url, avatar_url, sender_avatar_url) and
    the client uses them verbatim, so a filename can no longer truncate a URL
    and the chat id never enters a request path.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_get_media_url_returns_the_server_url_verbatim(self) -> None:
        body = _setup_slice(self.html, "const getMediaUrl = (msg) =>")
        self.assertIn("return msg.media?.url || ''", body)
        # The old file_path builder must stay deleted: rebuilding from file_path
        # would put the chat id back into requests and access logs.
        self.assertNotIn("file_path", _code_only(body))
        self.assertNotIn("`/media/${", body)

    def test_server_provided_urls_are_not_encoded_again(self) -> None:
        """media_url / thumb_url / avatar_url arrive already encoded server-side."""
        self.assertNotIn("encodeURIComponent(item.media_url", self.html)
        self.assertNotIn("encodeURIComponent(msg.sender_avatar_url", self.html)


class TestServiceMessageFallback(unittest.TestCase):
    """#259: pre-7.28.0 service rows have text='' and rendered as empty pills."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_service_branch_falls_back_to_raw_data(self) -> None:
        self.assertIn("const serviceMessagePredicate = (msg) =>", self.html)
        self.assertIn("const serviceMessageView = (msg) =>", self.html)
        # The pill renders the resolved view, not the bare (possibly empty) text.
        self.assertIn("serviceMessageView(msg)", self.html)
        self.assertNotIn(
            '<div class="service-message px-4 py-1.5 rounded-full text-xs text-center max-w-[80%]"',
            self.html,
        )

    def test_malformed_raw_data_cannot_throw(self) -> None:
        """adapter.py substitutes {} for unparseable raw_data — guard anyway."""
        body = _setup_slice(self.html, "const serviceRawData = (msg) =>")
        self.assertIn("typeof raw === 'object'", body)
        action_body = _setup_slice(self.html, "const serviceActionType = (msg) =>")
        self.assertIn("typeof type === 'string' ? type : ''", action_body)

    def test_add_and_delete_user_never_name_the_sender(self) -> None:
        """REGRESSION GUARD for the correctness trap.

        For chat_add_user / chat_delete_user the subject of the sentence is the
        AFFECTED user, which is never persisted — only service_type and
        action_type are. The row's sender is the ADMIN, so naming them would
        claim the wrong person joined or left. The backend's unknown-actor form
        ("Someone", with the default flags) is the only honest rendering.
        """
        unknown = _setup_slice(self.html, "const SERVICE_UNKNOWN_SUBJECT = {")
        self.assertIn("chat_add_user: 'Someone was added to the group'", unknown)
        self.assertIn("chat_delete_user: 'Someone was removed from the group'", unknown)

        # ...and EXACTLY those two: no other action may use the unknown form,
        # and neither may appear in a sender-named mapping.
        self.assertEqual(unknown.count("Someone"), 2)
        named = _setup_slice(self.html, "const SERVICE_PREDICATES = {")
        titled = _setup_slice(self.html, "const SERVICE_TITLE_PREDICATES = {")
        for action in ("chat_add_user", "chat_delete_user"):
            self.assertNotIn(action, named)
            self.assertNotIn(action, titled)

        # The sender-is-subject group reproduces the backend wording verbatim.
        self.assertIn("chat_joined_by_link: 'joined the group via invite link'", named)
        self.assertIn("chat_joined_by_request: 'joined the group'", named)
        self.assertIn("chat_edit_photo: 'changed the group photo'", named)
        self.assertIn("chat_delete_photo: 'removed the group photo'", named)
        self.assertIn("chat_edit_title: 'changed the group name to'", titled)
        self.assertIn("chat_create: 'created the group'", titled)
        self.assertIn("channel_create: 'created the channel'", titled)

    def test_unmapped_actions_and_blank_rows_render_nothing(self) -> None:
        predicate = _setup_slice(self.html, "const serviceMessagePredicate = (msg) =>")
        # Object.hasOwn, never a bare lookup: action_type 'constructor' would
        # otherwise resolve against Object.prototype.
        self.assertIn("Object.hasOwn(SERVICE_UNKNOWN_SUBJECT, action)", predicate)
        self.assertIn("Object.hasOwn(SERVICE_PREDICATES, action)", predicate)
        self.assertIn("Object.hasOwn(SERVICE_TITLE_PREDICATES, action)", predicate)
        # Falls through to the empty string, matching the backend's None: the
        # LAST statement of the helper is a bare `return ''`, not a fabricated
        # sentence. Asserted on the code, never on the comment above it.
        self.assertTrue(_code_only(predicate).rstrip().rstrip("}").rstrip().endswith("return ''"), predicate)

        # A service row with nothing to show paints an empty pill, and the day
        # divider is resolved through the same condition the pill renders on.
        self.assertIn("const isRenderedMessageRow = (msg, index) =>", self.html)
        self.assertIn('<div v-if="view.tail || view.actor"', self.html)

    def test_a_non_service_row_is_never_suppressed(self) -> None:
        """DATA-HIDING GUARD.

        ``media`` is null on perfectly good rows under DEFAULT configuration:
        ``LISTEN_NEW_MESSAGES_MEDIA`` is false (so the live WS payload carries
        ``"media": None``) and ``DOWNLOAD_MEDIA`` / ``skip_media_chat_ids`` do
        the same, permanently, for the sweep. A caption-less voice note,
        sticker or photo therefore has falsy text AND null media, and
        suppressing it would render the message NOWHERE while the unread badge
        still counted it — and would drop its ``:data-msg-id`` anchor, which
        findMessageElement / scrollToMessage / jumpToReply /
        focusAudioTrackMessage all resolve through.

        So the regular-message branch has exactly ONE reason to drop a row —
        an album duplicate, which is drawn by the album grid instead. There is
        no emptiness term in it: a term that could only ever be false still
        reads as "this branch may hide messages", which is the regression this
        guard exists to prevent.
        """
        self.assertIn('v-else-if="!isHiddenAlbumMessage(msg, index)"', self.html)
        # The two branches split on the same service condition, so a regular row
        # can never reach the service arm of the predicate either.
        self.assertIn("v-if=\"msg.raw_data?.service_type === 'service'\"", self.html)
        rendered = _code_only(_setup_slice(self.html, "const isRenderedMessageRow = (msg, index) =>"))
        statements = [line.strip() for line in rendered.splitlines() if line.strip()]
        # The non-service arm is the LAST statement, and album duplication is
        # the only thing it tests.
        self.assertEqual(statements[-2], "return !isHiddenAlbumMessage(msg, index)")
        # The dead emptiness predicate is gone from the whole template.
        self.assertNotIn("isBlankMessageRow", self.html)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_rendered_row_predicate_executed_against_real_row_shapes(self) -> None:
        """The EXECUTED counterpart of the guard above.

        Row 0 is the failure the string test cannot see: a caption-less voice
        note under default config (``LISTEN_NEW_MESSAGES_MEDIA`` false) —
        ``{text: '', media: null, raw_data: {}}``. It MUST render.

        Rows 7-9 are #T3: a service row whose only content is media, a reply, a
        forward, a reaction or a poll. The service branch renders NONE of those
        — it paints ``view.actor`` and ``view.tail`` and nothing else — so an
        unmapped action_type leaves an empty pill however much other data the
        row carries.
        """
        service = {"service_type": "service", "action_type": "nope"}
        rows = [
            # Not service-shaped: renders, whatever it looks like.
            {"id": 1, "text": "", "media": None, "raw_data": {}},
            {"id": 2, "text": None, "media": None, "raw_data": None},
            {"id": 3, "text": "", "media": None, "raw_data": "unparseable"},
            {"id": 4, "text": "", "media": None, "raw_data": {"grouped_id": None}},
            # Service-shaped with no renderable wording: paints nothing.
            {"id": 5, "text": "", "media": None, "raw_data": service},
            # Service-shaped WITH wording: painted.
            {
                "id": 6,
                "text": "",
                "media": None,
                "raw_data": {"service_type": "service", "action_type": "chat_edit_photo"},
            },
            {"id": 7, "text": "hi", "media": None, "raw_data": service},
            # Service-shaped, no wording, but carrying data the service branch
            # does not render: still paints NOTHING.
            {"id": 8, "text": "", "media": {"type": "photo"}, "raw_data": service},
            {"id": 9, "text": "", "media": None, "reactions": [{"emoji": "x"}], "raw_data": service},
            {
                "id": 10,
                "text": "",
                "media": None,
                "reply_to_msg_id": 5,
                "forward_from_id": 42,
                "raw_data": {**service, "poll": {"question": "?"}},
            },
        ]
        verdicts = _run_setup_helpers(
            self.html,
            (
                "const SERVICE_PREDICATES = {",
                "const SERVICE_TITLE_PREDICATES = {",
                "const SERVICE_UNKNOWN_SUBJECT = {",
                "const getSenderName = (msg) =>",
                "const getCurrentSenderName = (msg) =>",
                "const serviceRawData = (msg) =>",
                "const serviceActionType = (msg) =>",
                "const serviceActorIsSender = (msg) =>",
                "const serviceMessagePredicate = (msg) =>",
                "const serviceMessageView = (msg) =>",
                "const getGroupedId = (msg) =>",
                "const isFirstInAlbum = (msg, index) =>",
                "const isHiddenAlbumMessage = (msg, index) =>",
                "const isRenderedMessageRow = (msg, index) =>",
            ),
            f"{json.dumps(rows)}.map(isRenderedMessageRow)",
            prelude="const sortedMessages = { value: [] }\n",
        )
        self.assertEqual(
            verdicts,
            [True, True, True, True, False, True, True, False, False, False],
        )

    def test_the_day_divider_lands_on_a_row_that_is_actually_painted(self) -> None:
        """A divider must never head a suppressed row.

        Under ``flex-col-reverse`` the divider emitted at ``index`` appears
        ABOVE that row, so it heads the whole day. Emitting it on a suppressed
        row leaves it orphaned with nothing under it; dropping it there instead
        would delete the day header for every remaining row of that day. Both
        neighbours are therefore resolved through the same predicate the row
        branches use, and the search walks past suppressed rows to the next
        painted one.
        """
        self.assertIn('<div v-if="showDateSeparator(index)" class="date-separator"', self.html)
        body = _code_only(_setup_slice(self.html, "const showDateSeparator = (index) =>"))
        self.assertIn("if (!isRenderedMessageRow(currMsg, index)) return false", body)
        self.assertIn("if (!isRenderedMessageRow(olderMsg, older)) continue", body)
        # The bail-out precedes any date comparison, and the walk replaced the
        # bare index + 1 neighbour lookup.
        self.assertLess(body.index("isRenderedMessageRow(currMsg, index)"), body.index("messageDayKey(currMsg)"))
        self.assertNotIn("sortedMessages.value[index + 1]", body)

        rendered = _code_only(_setup_slice(self.html, "const isRenderedMessageRow = (msg, index) =>"))
        self.assertIn("!isHiddenAlbumMessage(msg, index)", rendered)
        # A service row paints its container (and keeps its :data-msg-id anchor)
        # whatever the pill inside resolves to.
        self.assertIn("serviceRawData(msg)?.service_type === 'service'", rendered)
        # #T3: the service arm asks the SERVICE BRANCH'S OWN condition rather
        # than a proxy for it. The branch paints on `view.tail || view.actor`,
        # so the predicate must build that same view.
        self.assertIn("const view = serviceMessageView(msg)", rendered)
        self.assertIn("return !!(view.actor || view.tail)", rendered)
        self.assertIn('<div v-if="view.tail || view.actor"', self.html)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_a_service_row_with_reactions_but_no_wording_does_not_orphan_a_divider(self) -> None:
        """#T3 EXECUTED, end to end through the real ``showDateSeparator``.

        The old predicate answered "painted" for any service row carrying media,
        a reply, a forward, reactions or a poll — none of which the service
        branch renders. Such a row is the OLDEST of its day here, so the divider
        was emitted on it and then had nothing under it: an orphaned "July 29"
        header above an invisible row.

        Row order is newest-first, matching ``sortedMessages`` under
        ``flex-col-reverse``: the divider emitted at ``index`` appears ABOVE
        that row.
        """
        rows = [
            {"id": 3, "date": "2026-07-30 09:00:00", "text": "later", "raw_data": {}},
            # Reactions only, unmapped action_type -> empty pill, paints nothing.
            {
                "id": 2,
                "date": "2026-07-29 12:00:00",
                "text": "",
                "reactions": [{"emoji": "x", "count": 1}],
                "raw_data": {"service_type": "service", "action_type": "nope"},
            },
            {"id": 1, "date": "2026-07-28 08:00:00", "text": "hi", "raw_data": {}},
        ]
        prelude = f"""
const sortedMessages = {{ value: {json.dumps(rows)} }}
const viewerTimezone = {{ value: 'UTC' }}
// The rows are naive UTC and the zone is UTC, so the real
// moment.utc(s).tz(z).format('YYYY-MM-DD') is exactly the date prefix.
const moment = {{ utc: (s) => ({{ tz: () => ({{ format: () => String(s).slice(0, 10) }}) }}) }}
"""
        verdicts = _run_setup_helpers(
            self.html,
            (
                "const SERVICE_PREDICATES = {",
                "const SERVICE_TITLE_PREDICATES = {",
                "const SERVICE_UNKNOWN_SUBJECT = {",
                "const getSenderName = (msg) =>",
                "const getCurrentSenderName = (msg) =>",
                "const serviceRawData = (msg) =>",
                "const serviceActionType = (msg) =>",
                "const serviceActorIsSender = (msg) =>",
                "const serviceMessagePredicate = (msg) =>",
                "const serviceMessageView = (msg) =>",
                "const getGroupedId = (msg) =>",
                "const isFirstInAlbum = (msg, index) =>",
                "const isHiddenAlbumMessage = (msg, index) =>",
                "const isRenderedMessageRow = (msg, index) =>",
                "const dayKeyCache = new WeakMap()",
                "const messageDayKey = (msg) =>",
                "const showDateSeparator = (index) =>",
            ),
            "[showDateSeparator(0), showDateSeparator(1), showDateSeparator(2)]",
            prelude=prelude,
        )
        # index 1 is the empty pill: no divider may hang on it. The July 30 row
        # still heads its own day (the walk skips past the empty pill to July 28),
        # and the oldest row always gets one.
        self.assertEqual(verdicts, [True, False, True])

    def test_quoted_title_is_reproduced_for_title_bearing_actions(self) -> None:
        predicate = _setup_slice(self.html, "const serviceMessagePredicate = (msg) =>")
        self.assertIn("const title = serviceRawData(msg)?.new_title", predicate)
        # Backend wording quotes the title: 'X changed the group name to "Y"'.
        self.assertIn(
            "`${SERVICE_TITLE_PREDICATES[action]} \"${typeof title === 'string' ? title : ''}\"`",
            predicate,
        )


class TestServiceMessageSenderTrigger(unittest.TestCase):
    """#260: the actor name in a service pill opens the sender popup."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_actor_is_a_button_wired_to_the_existing_popup(self) -> None:
        # The ORIGINAL message-row avatar trigger must stay exactly as it was.
        self.assertEqual(self.html.count('@click="openSenderInfo(msg, $event)"'), 2)
        # A real $event is required: the popup stores event.currentTarget and
        # restores focus to it on close.
        self.assertIn(':aria-label="`Show sender details for ${view.actor}`"', self.html)
        self.assertIn('<button v-if="view.clickable" type="button"', self.html)

    def test_trigger_is_gated_on_sender_id_and_on_the_sender_being_the_subject(self) -> None:
        body = _setup_slice(self.html, "const serviceMessageView = (msg) =>")
        self.assertIn("serviceActorIsSender(msg) && msg?.sender_id != null", body)
        # Channel service rows have a NULL sender_id: named, but not clickable.
        self.assertIn("msg?.sender_id != null ? getSenderName(msg) : 'Someone'", body)

    def test_prose_is_never_parsed_for_the_name(self) -> None:
        """Only a literal PREFIX match may be split — a display name can contain
        the words around it, so a substring search would mangle the sentence."""
        body = _setup_slice(self.html, "const serviceMessageView = (msg) =>")
        self.assertIn("stored.startsWith(name)", body)
        self.assertIn("stored.slice(name.length)", body)
        self.assertNotIn(".indexOf(", body)
        self.assertNotIn(".split(", body)
        self.assertNotIn(".replace(", body)


def _stub_action(class_name: str) -> Any:
    """An object whose CLASS NAME is ``class_name``.

    ``service_message_text`` and ``service_action_type`` branch on
    ``type(action).__name__`` and read only ``.title``, so a bare stub drives
    the real backend wording without pinning Telethon constructor signatures.
    ``test_every_mapped_action_names_a_real_telethon_action`` is what proves the
    names are not invented.
    """
    return type(class_name, (), {})()


# The backend's curated wording set, read out of the function itself: every
# ``if name == "MessageAction..."`` branch in ``service_message_text``. Parsed
# rather than hand-listed so ADDING a branch server-side without mirroring it in
# index.html fails this file instead of silently drifting.
_SERVER_ACTION_CLASSES = tuple(
    dict.fromkeys(re.findall(r'name == "(MessageAction[A-Za-z]+)"', inspect.getsource(service_message_text)))
)
_SERVER_ACTION_TYPES = {service_action_type(_stub_action(name)): name for name in _SERVER_ACTION_CLASSES}

_SERVICE_WORDING_DECLARATIONS = (
    "const SERVICE_PREDICATES = {",
    "const SERVICE_TITLE_PREDICATES = {",
    "const SERVICE_UNKNOWN_SUBJECT = {",
    "const getSenderName = (msg) =>",
    "const getCurrentSenderName = (msg) =>",
    "const serviceRawData = (msg) =>",
    "const serviceActionType = (msg) =>",
    "const serviceActorIsSender = (msg) =>",
    "const serviceMessagePredicate = (msg) =>",
    "const serviceMessageView = (msg) =>",
)

# Distinctive on purpose: a sentence-shaped actor name would hide a wording bug
# where one side happens to read the same as the other.
_ACTOR_NAME = "Ada Lovelace"
_NEW_TITLE = "Analytical Engine"


class TestServiceMessageWordingParity(unittest.TestCase):
    """#259 DRIFT GUARD: the client's wording IS the backend's wording.

    #259 happened because the same sentences existed in two places with nothing
    tying them together. The render-time fallback in index.html re-states them a
    THIRD time (as literal JS strings), so this test executes the real client
    helpers and compares every rendered sentence against
    ``message_utils.service_message_text`` — the two copies cannot drift without
    a red test.

    THE ONE DELIBERATE DIVERGENCE is asserted here too rather than excluded:
    ``chat_add_user`` / ``chat_delete_user`` render "Someone ..." client-side.
    The subject of those two sentences is the AFFECTED user, and only
    ``service_type`` / ``action_type`` are persisted — the affected user is not,
    so the sender (the admin who acted) must never be named as the joiner or
    leaver. That is exactly the backend's own unknown-actor form, so parity here
    means "matches ``service_message_text(action, actor_name=None)``" while the
    rest mean "matches it with the row's sender".
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _render_client(self, action_types: tuple[str, ...]) -> dict[str, Any]:
        """EXECUTE the template's own service-pill helpers under node."""
        rows = [
            {
                "id": index + 1,
                "text": "",  # pre-7.28.0 rows: no materialised wording
                "sender_id": 7,
                "sender_name": _ACTOR_NAME,
                "raw_data": {
                    "service_type": "service",
                    "action_type": action_type,
                    "new_title": _NEW_TITLE,
                },
            }
            for index, action_type in enumerate(action_types)
        ]
        epilogue = f"""
const rows = {json.dumps(rows)}
const rendered = {{}}
const clickable = {{}}
for (const row of rows) {{
    const view = serviceMessageView(row)
    rendered[row.raw_data.action_type] = view.actor + view.tail
    clickable[row.raw_data.action_type] = view.clickable
}}
console.log(JSON.stringify({{
    rendered,
    clickable,
    named: Object.keys(SERVICE_PREDICATES),
    titled: Object.keys(SERVICE_TITLE_PREDICATES),
    unknown: Object.keys(SERVICE_UNKNOWN_SUBJECT),
}}))
"""
        return _run_setup_program(self.html, _SERVICE_WORDING_DECLARATIONS, "", epilogue)

    def _render_client_titles(self, cases: tuple[tuple[str, dict[str, Any]], ...]) -> dict[str, Any]:
        """Like ``_render_client``, but each row's ``raw_data`` is built from a
        caller-supplied override instead of the fixed ``_NEW_TITLE`` — needed to
        exercise a missing/null ``new_title`` per action_type, which
        ``_render_client`` cannot express (it keys its result dict by
        action_type, so two rows sharing one action_type would collide)."""
        rows = [
            {
                "id": index + 1,
                "text": "",
                "sender_id": 7,
                "sender_name": _ACTOR_NAME,
                "raw_data": {
                    "service_type": "service",
                    "action_type": action_type,
                    **raw_data_override,
                },
            }
            for index, (action_type, raw_data_override) in enumerate(cases)
        ]
        epilogue = f"""
const rows = {json.dumps(rows)}
const rendered = rows.map(row => {{
    const view = serviceMessageView(row)
    return view.actor + view.tail
}})
console.log(JSON.stringify({{ rendered }}))
"""
        return _run_setup_program(self.html, _SERVICE_WORDING_DECLARATIONS, "", epilogue)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_client_covers_exactly_the_backend_wording_set(self) -> None:
        """Neither side may gain (or lose) an action without the other."""
        result = self._render_client(())
        client_types = set(result["named"]) | set(result["titled"]) | set(result["unknown"])
        self.assertEqual(client_types, set(_SERVER_ACTION_TYPES))
        # The three client maps are disjoint: an action rendered both with and
        # without its actor would resolve by lookup order, not by intent.
        self.assertEqual(
            len(result["named"]) + len(result["titled"]) + len(result["unknown"]),
            len(client_types),
        )

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_every_rendered_sentence_matches_the_backend_verbatim(self) -> None:
        result = self._render_client(tuple(_SERVER_ACTION_TYPES))
        unknown_subject = set(result["unknown"])

        for action_type, class_name in _SERVER_ACTION_TYPES.items():
            with self.subTest(action_type=action_type):
                action = _stub_action(class_name)
                action.title = _NEW_TITLE
                if action_type in unknown_subject:
                    # DELIBERATE DIVERGENCE (see the class docstring): the
                    # affected user is not persisted, so the client renders the
                    # backend's unknown-actor form with the default flags.
                    expected = service_message_text(action, actor_name=None)
                else:
                    expected = service_message_text(action, actor_name=_ACTOR_NAME)
                self.assertEqual(result["rendered"][action_type], expected)
                # Only a sentence whose subject IS the sender may open the
                # sender popup.
                self.assertEqual(result["clickable"][action_type], action_type not in unknown_subject)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_title_bearing_actions_render_empty_quotes_when_new_title_is_missing(self) -> None:
        """The ONE place client and backend are DELIBERATELY allowed to differ.

        Client (``serviceMessagePredicate``, index.html): reads
        ``raw_data.new_title`` and falls back to ``''`` whenever it is absent
        or not a string — ``created the group ""``.

        Backend (``service_message_text``, message_utils.py): does
        ``title = getattr(action, "title", None)`` with no guard against
        ``None`` before interpolating it into the f-string, so a title-less
        action would render the literal Python stringification ``"None"``
        inside the quotes — ``created the group "None"``.

        Verified against the real telethon types (see
        ``test_every_mapped_action_names_a_real_telethon_action``):
        ``MessageActionChatEditTitle`` / ``MessageActionChatCreate`` /
        ``MessageActionChannelCreate`` all declare ``title`` as a REQUIRED
        constructor field, so the backend's ``None``-title branch can never
        actually fire from a live Telethon event — it is unreachable in
        production. The client's branch IS reachable: ``new_title`` is read
        out of the persisted ``raw_data`` JSON blob, and a historical or
        otherwise incomplete row can simply lack that key. The client's
        empty-quotes rendering is therefore the correct behaviour, not a gap
        — a future "fix" that fetches ``"None"``/``"null"``/``"undefined"``
        into the sentence to chase parity would be a regression.
        """
        title_bearing = tuple(self._render_client(())["titled"])
        self.assertTrue(title_bearing)  # sanity: SERVICE_TITLE_PREDICATES must not be empty

        cases: list[tuple[str, dict[str, Any]]] = []
        for action_type in title_bearing:
            cases.append((action_type, {}))  # new_title key absent entirely
            cases.append((action_type, {"new_title": None}))  # new_title explicitly null
        rendered = self._render_client_titles(tuple(cases))["rendered"]

        for (action_type, override), sentence in zip(cases, rendered, strict=True):
            variant = "new_title absent" if not override else "new_title: null"
            with self.subTest(action_type=action_type, variant=variant):
                class_name = _SERVER_ACTION_TYPES[action_type]

                # The client's actual fallback is an empty string, and an empty
                # string IS a str, so the backend's own type-check leaves it
                # untouched too — the two sides genuinely agree here.
                action = _stub_action(class_name)
                action.title = ""
                self.assertEqual(sentence, service_message_text(action, actor_name=_ACTOR_NAME))

                # The forbidden variant: the backend's OWN behaviour when title
                # is actually None (unreachable in production, per the
                # docstring above, but that's exactly what must never leak
                # into the client's rendering).
                action.title = None
                self.assertNotEqual(sentence, service_message_text(action, actor_name=_ACTOR_NAME))
                self.assertNotIn('"None"', sentence)
                self.assertNotIn('"null"', sentence)
                self.assertNotIn('"undefined"', sentence)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_unknown_subject_actions_never_render_the_stored_variant_wording(self) -> None:
        """``affected_left`` / ``affected_joined_self`` are NOT persisted.

        The backend reads them off the live Telethon event; a stored row keeps
        neither, so "left"/"joined" can never be reconstructed at render time.
        The default (``was removed`` / ``was added``) is the only honest choice,
        and this pins that the client did not pick the variant wording instead.
        """
        result = self._render_client(("chat_add_user", "chat_delete_user"))
        added = _stub_action("MessageActionChatAddUser")
        removed = _stub_action("MessageActionChatDeleteUser")

        self.assertEqual(
            result["rendered"]["chat_add_user"],
            service_message_text(added, actor_name=None, affected_joined_self=False),
        )
        self.assertNotEqual(
            result["rendered"]["chat_add_user"],
            service_message_text(added, actor_name=None, affected_joined_self=True),
        )
        self.assertEqual(
            result["rendered"]["chat_delete_user"],
            service_message_text(removed, actor_name=None, affected_left=False),
        )
        self.assertNotEqual(
            result["rendered"]["chat_delete_user"],
            service_message_text(removed, actor_name=None, affected_left=True),
        )
        # And the sender is never named in either sentence.
        for action_type in ("chat_add_user", "chat_delete_user"):
            self.assertNotIn(_ACTOR_NAME, result["rendered"][action_type])

    @unittest.skipUnless(telethon_types, "telethon is required for the class-name cross-check")
    def test_every_mapped_action_names_a_real_telethon_action(self) -> None:
        """The tags are derived from Telethon class names, so they must exist."""
        for class_name in _SERVER_ACTION_CLASSES:
            self.assertTrue(hasattr(telethon_types, class_name), class_name)


class TestPlaybarDate(unittest.TestCase):
    """#262: the playbar showed a time with no date."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_compact_date_helper_reads_the_timestamp_as_utc(self) -> None:
        """The stored timestamp is naive UTC.

        ``Date.parse`` / ``new Date`` read a naive string as LOCAL time, which
        renders the wrong calendar day for anything near midnight, so this must
        use the same moment.utc(...).tz(...) form as ``formatTime``.
        """
        body = _setup_slice(self.html, "const formatShortDate = (dateStr) =>")
        self.assertIn("moment.utc(dateStr).tz(viewerTimezone.value)", body)
        self.assertNotIn("Date.parse", body)
        self.assertNotIn("new Date(", body)
        self.assertNotIn("toLocaleDateString", body)

    def test_helper_is_registered_and_used_in_the_playbar(self) -> None:
        self.assertIn("\n                    formatShortDate,\n", self.html)
        self.assertIn("{{ formatShortDate(audioTrack.date) }} {{ formatTime(audioTrack.date) }}", self.html)


class TestAudioBubbleDownload(unittest.TestCase):
    """#261: per-message download control on audio / voice bubbles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _audio_bubble(self) -> str:
        start = self.html.index('<div v-else-if="isAudioFile(msg)"')
        return self.html[start : self.html.index("<!-- GIFs / Animations", start)]

    def test_download_is_a_real_anchor_gated_on_no_download(self) -> None:
        bubble = self._audio_bubble()
        self.assertIn('v-if="!noDownload && getMediaUrl(msg)"', bubble)
        self.assertIn(':href="mediaDownloadUrl(getMediaUrl(msg))"', bubble)
        self.assertIn(':download="getDocumentDisplayName(msg)"', bubble)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_the_download_flag_joins_a_url_that_has_a_query(self) -> None:
        """The current media's URL carries ``?v=`` once an edit replaced the
        media, so ``download=1`` must join it with ``&``, not start a second query."""
        urls = ["/media/audioChatRef07AB/3_voice", "/media/audioChatRef07AB/3_voice?v=1", ""]
        joined = _run_setup_helpers(
            self.html,
            ("const mediaDownloadUrl = (url) =>",),
            f"{json.dumps(urls)}.map(mediaDownloadUrl)",
        )
        self.assertEqual(
            joined,
            [
                "/media/audioChatRef07AB/3_voice?download=1",
                "/media/audioChatRef07AB/3_voice?v=1&download=1",
                "",
            ],
        )

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_the_url_term_of_the_guard_is_load_bearing(self) -> None:
        """``getMediaUrl(msg)`` in the ``v-if`` is NOT a dead term.

        The bubble renders on ``isAudioFile(msg)``, which is satisfied by
        ``media.type`` alone — and a media row exists with ``type`` set but no
        downloadable file whenever the file was never written: an oversized voice
        note (``_process_media`` returns ``downloaded: False`` with no path once
        it exceeds ``MAX_MEDIA_SIZE``) or a row still queued for the pending
        -media retry loop (``downloaded=0``, ``file_path`` NULL). v8.0: the
        SERVER expresses that as ``media.url: null`` (no local file, or a
        no_download session), and ``getMediaUrl`` returns ``''`` for it.

        Without the term the anchor would render ``href="?download=1"`` — a link
        to the viewer page itself, offered as if the audio were downloadable.
        """
        rows = [
            # Oversized voice note: typed, never written -> the server sends url null.
            {"id": 1, "media": {"id": "1_voice", "type": "voice", "url": None}},
            # Pending download of an audio document, name known, file not there.
            {"id": 2, "media": {"id": "2_audio", "type": "audio", "file_name": "note.ogg", "url": None}},
            # Downloaded: both terms true, anchor renders the server's ref URL.
            {"id": 3, "media": {"id": "3_voice", "type": "voice", "url": "/media/audioChatRef07AB/3_voice"}},
        ]
        verdicts = _run_setup_helpers(
            self.html,
            (
                "const getMediaUrl = (msg) =>",
                "const getMediaDisplayName = (media) =>",
                "const getDocumentDisplayName = (msg) =>",
                "const isAudioFile = (msg) =>",
            ),
            f"{json.dumps(rows)}.map(m => [isAudioFile(m), getMediaUrl(m)])",
        )
        self.assertEqual(
            verdicts,
            [
                [True, ""],
                [True, ""],
                [True, "/media/audioChatRef07AB/3_voice"],
            ],
        )

    def test_download_is_not_inside_the_sm_only_playbar_group(self) -> None:
        """The playbar speed group is ``hidden sm:flex``.

        A download button placed in there vanishes below the sm breakpoint —
        i.e. on mobile, where a per-message download matters most.
        """
        bubble = self._audio_bubble()
        self.assertNotIn("hidden sm:flex", bubble)

        speed_start = self.html.index('role="group" aria-label="Playback speed"')
        speed_body = self.html[speed_start : self.html.index("</div>", self.html.index("</button>", speed_start))]
        self.assertNotIn("download", speed_body)

    def test_duration_is_rendered_once(self) -> None:
        """#263: duration already exists on the bubble — no duplicate element."""
        bubble = self._audio_bubble()
        self.assertEqual(bubble.count("formatDuration(msg.media.duration)"), 1)


class TestAudioBubbleDownloadKeepsFixedSize(unittest.TestCase):
    """#270: the round #261 download anchor was stretched to fill its row.

    ``.message-bubble a`` is (0,1,1) specificity, which OUTRANKS the Tailwind
    ``w-9`` utility at (0,1,0). Without the ``:not(.bubble-icon-action)``
    exclusion, ``width: 100%`` silently wins over the 36px utility, and
    ``shrink-0`` then protects that 100% basis instead of the intended 36px:
    the anchor eats the row and its ``min-w-0 flex-1`` sibling (icon +
    filename + duration) is starved to 0px width — invisible, since
    ``truncate`` paints nothing at 0 width, not merely ugly.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _bubble_media_rule(self) -> str:
        rule_start = self.html.index(".message-bubble img,")
        return self.html[rule_start : self.html.index("}", rule_start)]

    def _download_anchor_tag(self) -> str:
        start = self.html.index('<div v-else-if="isAudioFile(msg)"')
        bubble = self.html[start : self.html.index("<!-- GIFs / Animations", start)]
        anchor_start = bubble.index('<a v-if="!noDownload && getMediaUrl(msg)"')
        return bubble[anchor_start : bubble.index(">", anchor_start) + 1]

    def test_the_anchor_arm_of_the_stretch_rule_excludes_icon_actions(self) -> None:
        """If ``:not(.bubble-icon-action)`` is dropped, every icon anchor in
        ``.message-bubble`` (not just the download button) silently gets
        stretched to 100% width/height:auto again."""
        rule = self._bubble_media_rule()
        self.assertIn(".message-bubble a:not(.bubble-icon-action) {", rule)

    def test_the_download_anchor_opts_out_via_the_icon_action_class(self) -> None:
        """Without ``bubble-icon-action`` on the anchor itself, the CSS
        exclusion above has nothing to match and the anchor is stretched
        exactly as it was pre-fix."""
        anchor = self._download_anchor_tag()
        self.assertIn("bubble-icon-action", anchor)
        # The opt-out is meaningless without the actual 32px sizing utilities
        # it is meant to protect (the transcript button's size, beside it).
        self.assertIn("w-8", anchor)
        self.assertIn("h-8", anchor)
        self.assertIn("shrink-0", anchor)

    def test_the_player_row_has_no_note_emoji_and_names_only_music(self) -> None:
        """The row is a player drawn on the bubble, as Telegram draws it.

        The emoji note is gone, and a voice note shows no file name (every one
        is voice.ogg); a music file keeps its title. The info panel keeps the
        file name for both.
        """
        start = self.html.index('<div v-else-if="isAudioFile(msg)"')
        bubble = self.html[start : self.html.index("<!-- GIFs / Animations", start)]
        self.assertNotIn("🎵", bubble)
        self.assertNotIn("from-tg-n800", bubble)
        self.assertNotIn("shadow-lg", bubble)
        # Telegram's file row: a voice note reads "Voice message", a music
        # file its title, over the duration and the size.
        self.assertIn(
            "<div class=\"voice-title truncate\">{{ msg.media?.type === 'voice' ? 'Voice message' : getDocumentDisplayName(msg) }}</div>",
            bubble,
        )
        self.assertIn("{{ formatBytes(msg.media.file_size) }}", bubble)


def test_gif_observer_watcher_is_shallow_and_ordered():
    """The GIF re-observe watcher rides sortedMessages (shallow): the old
    watch(messages, ..., {deep: true}) re-traversed every loaded message
    object — nested media, raw_data, reactions — on every arriving message,
    and the dependency sets themselves grew with history depth. It must also
    stay declared AFTER sortedMessages (watchers evaluate their source
    eagerly — the same placement rule updateFloatingDate's watcher documents).
    """
    html = INDEX_HTML.read_text()
    assert not re.search(r"deep\s*:\s*true", html), "a deep watcher crept back into the page"
    gif_watch = re.search(
        r"watch\(\[sortedMessages, mediaRevision\], \(\) => \{\s*nextTick\(\(\) => \{\s*"
        r"if \(!gifObserver\) setupGifObserver\(\)\s*"
        r"document\.querySelectorAll\('\.gif-video'\)\.forEach\(video => \{\s*"
        r"gifObserver\.observe\(video\)",
        html,
    )
    assert gif_watch, "gif observer watcher must watch [sortedMessages, mediaRevision] shallowly"
    assert html.index("const sortedMessages") < gif_watch.start(), "watcher declared before its source"
    assert html.index("const mediaRevision") < gif_watch.start(), "watcher declared before its source"


def test_in_place_media_merge_bumps_the_gif_watchers_revision():
    """sortedMessages never reads media, so upsertMessages merging a finished
    download into an EXISTING row is invisible to it — the revision counter is
    the only thing that lets the GIF watcher observe that new <video>. It must
    bump on a real media change and stay put for unrelated or identical
    updates, or the watcher either misses GIFs or fires on every poll."""
    html = INDEX_HTML.read_text()
    prelude = """
const messages = { value: [] }
const mediaRevision = { value: 0 }
const messageWindowIsContiguous = { value: true }
const appendKeepsWindowContiguous = () => true
const messageIdKey = (msg) => (msg && msg.id != null ? String(msg.id) : null)
"""
    epilogue = """
(() => {
    messages.value = [{ id: 1, text: 'clip', media: null, reactions: [] }]
    const after = []

    upsertMessages([{ id: 1, text: 'clip', media: { type: 'animation', url: '/m/1' }, reactions: [] }])
    after.push(mediaRevision.value)   // media arrived -> 1

    upsertMessages([{ id: 1, text: 'clip edited', media: { type: 'animation', url: '/m/1' }, reactions: [] }])
    after.push(mediaRevision.value)   // structurally identical media -> still 1

    upsertMessages([{ id: 2, text: 'new row', media: null, reactions: [] }])
    after.push(mediaRevision.value)   // new row, no merge -> still 1

    upsertMessages([{ id: 1, text: 'clip edited', media: { type: 'animation', url: '/m/1?v=2' }, reactions: [] }])
    after.push(mediaRevision.value)   // media really changed -> 2

    console.log(JSON.stringify(after))
})();
"""
    out = _run_setup_program(
        html,
        ("const upsertMessages = (incomingMessages, { updateExisting = true } = {}) =>",),
        prelude,
        epilogue,
    )
    assert out == [1, 1, 1, 2], out


def test_parse_telegram_link_recognizes_both_forms_and_rejects_noise():
    """t.me/c/<internal>/<msg> maps to the marked id (-100 prefix); public
    t.me/<username>/<msg> maps by username; anything else is a plain search."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    cases = """
(() => {
    const results = [
        parseTelegramLink('https://t.me/c/1234567890/42'),
        parseTelegramLink('t.me/c/1234567890/42?single'),
        parseTelegramLink('https://t.me/durov/100'),
        parseTelegramLink('t.me/Some_Channel/7#comment'),
        parseTelegramLink('hello world'),
        parseTelegramLink('t.me/c/notanumber/42'),
        parseTelegramLink(''),
        parseTelegramLink(null),
    ]
    console.log(JSON.stringify(results))
})();
"""
    out = _run_setup_program(html, ("const parseTelegramLink = ",), "", cases)
    assert out[0] == {"markedId": -1001234567890, "username": None, "messageId": 42}
    assert out[1] == {"markedId": -1001234567890, "username": None, "messageId": 42}
    assert out[2] == {"markedId": None, "username": "durov", "messageId": 100}
    assert out[3] == {"markedId": None, "username": "Some_Channel", "messageId": 7}
    assert out[4] is None and out[5] is None and out[6] is None and out[7] is None


def test_scroll_fab_carries_no_position_utility():
    """The scroll-to-latest FAB must not carry Tailwind's ``relative``.

    ``.scroll-to-bottom-btn`` positions the FAB with ``position: absolute``
    (bottom-right of the message pane); Tailwind's ``.relative`` ties it on
    specificity and the CDN-injected sheet cascades later, so ``relative``
    WON — turning ``right: 20px`` into "20px left of static position" and
    parking the button half off the bottom-LEFT edge (shipped in #207 for
    the unseen badge; the badge needs a positioned parent, which
    ``position: absolute`` already is).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'class="scroll-to-bottom-btn"' in html
    match = re.search(r'class="[^"]*scroll-to-bottom-btn[^"]*"', html)
    assert match is not None
    assert match.group(0) == 'class="scroll-to-bottom-btn"', (
        f"scroll FAB class list grew: {match.group(0)!r} — any utility that sets "
        "position (relative/absolute/fixed/static) breaks its placement; if you "
        "need one, move the positioning into the .scroll-to-bottom-btn rule instead"
    )


def test_body_height_is_not_overridden_with_a_dynamic_viewport_unit():
    """body keeps Tailwind's h-screen (100vh). Do not re-add a dvh override.

    8.4.0 sized body to 100dvh on the theory that 100vh hides bottom-anchored
    content behind iOS Safari's retractable toolbar. On a real iPhone it did
    the opposite: the app rendered SHORT, leaving a large dead band below the
    message list. body also carries the safe-area padding and overflow:hidden,
    and in that combination the dynamic unit resolves to less than the visible
    area. Reverted in 8.4.1.

    The scroll-to-latest button being clipped - the bug the dvh change was
    bundled with - was fully explained and fixed by removing the `relative`
    utility (see test_scroll_fab_carries_no_position_utility), which was
    measured in a browser; the dvh half never was.

    The one legitimate dvh use is a modal's max-height, which predates this
    and is scoped to that element, not the layout root.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    style = html[html.index("<style>") : html.index("</style>")]

    # A layout-root compound selector: html / body / #app plus its own
    # classes, ids, attributes or pseudos - but no descendant, so a rule
    # like `body .modal` or `.date-picker-dialog` is correctly not a root.
    # The qualifier group is optional, never repeated: its trailing class
    # already swallows further qualifiers, so one pass matches the same
    # selectors without the nested quantifier that lets `html####...`
    # backtrack exponentially (CodeQL py/redos).
    root = re.compile(r"^(?:html|body|#app)(?:[.#:\[][^\s>+~,]*)?$")
    offenders = []
    for selector, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
        selector = selector.strip()
        if selector.startswith("@"):
            continue
        parts = [p.strip() for p in selector.split(",")]
        if not any(root.match(p) for p in parts):
            continue
        for prop, value in re.findall(r"([a-z-]*height)\s*:\s*([^;]+)", decls):
            if "dvh" in value or "svh" in value:
                offenders.append(f"{selector} {{ {prop}: {value.strip()} }}")
    assert not offenders, (
        "the layout root is sized with a dynamic viewport unit again; that left "
        f"a dead band at the bottom on iOS (8.4.0 regression): {offenders}"
    )
    assert 'class="bg-tg-header text-tg-ink h-screen overflow-hidden"' in html, (
        "body must keep h-screen: it is what gives the app its height"
    )


def test_theme_boot_precedence_includes_the_server_default():
    """The pre-paint boot script resolves ?theme= > saved choice > server default.

    '__VIEWER_DEFAULT_THEME__' is substituted by read_root at serve time; it
    must appear in BOTH consumers (the boot script and the picker init), the
    boot chain must consult it only after the user's own sources, and every
    source must be validated against the theme allowlist BEFORE precedence -
    an unknown candidate that merely fell through later would flash the
    wrong palette pre-paint.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.count("'__VIEWER_DEFAULT_THEME__'") == 2
    assert (
        "pick(new URLSearchParams(location.search).get('theme')) || pick(localStorage.getItem('viewerTheme')) || pick('__VIEWER_DEFAULT_THEME__')"
        in html
    )


def test_theme_boot_allowlist_matches_the_picker():
    """KNOWN_THEMES in the head script and viewerThemes in the app are two
    copies of one list (the head runs before the app exists); they must not
    drift, or a valid theme would be silently ignored pre-paint."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    boot = re.search(r"const KNOWN_THEMES = \[([^\]]*)\]", html)
    assert boot is not None
    boot_ids = re.findall(r"'([a-z]+)'", boot.group(1))
    picker = re.search(r"const viewerThemes = \[(.*?)\n {16}\]", html, re.S)
    assert picker is not None
    picker_ids = re.findall(r"\{ id: '([a-z]+)', label:", picker.group(1))
    assert boot_ids == picker_ids, f"boot {boot_ids} != picker {picker_ids}"
    assert boot_ids[0] == "system"
    assert len(boot_ids) == 12
    # Ids reach the page through _sanitize_theme_slug, which keeps 3 to 16 letters.
    assert all(re.fullmatch(r"[a-z]{3,16}", theme_id) for theme_id in boot_ids)


def _theme_blocks(html: str) -> dict[str, dict[str, str]]:
    """Every palette's declarations, one data-theme block each (Telegram Day's is also the bare :root)."""
    blocks = {}
    for match in re.finditer(r':root\[data-theme="([a-z]+)"\]\s*\{([^}]*)\}', html):
        blocks[match.group(1)] = match.group(2)
    return {
        name: dict(re.findall(r"^\s*((?:--tg-[a-z0-9-]+)|color-scheme):\s*([^;]+);", body, re.M))
        for name, body in blocks.items()
    }


def test_every_theme_restates_every_colour_token():
    """A palette that misses a token silently falls through to Telegram Day's value.

    That is how a light theme ends up with one dark surface, so every palette
    block declares exactly the set Telegram Day declares: the surfaces, the whole
    neutral scale (the dark palettes no longer inherit it), the bubble sides,
    the seven peer colours and avatar fills, the status colours and the
    colour-scheme the native controls follow.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    themes = _theme_blocks(html)
    boot = re.search(r"const KNOWN_THEMES = \[([^\]]*)\]", html)
    # "system" is a choice, not a palette: it resolves to one of the others.
    assert sorted(themes) == sorted(set(re.findall(r"'([a-z]+)'", boot.group(1))) - {"system"})
    # Telegram Day is the fallback: the bare :root carries it too, for a page
    # whose data-theme is missing or unknown.
    assert ':root,\n        :root[data-theme="telegram"] {' in html
    base = set(themes["telegram"])
    for required in ("color-scheme", "--tg-n950", "--tg-quote-out", "--tg-meta-in", "--tg-peer-6", "--tg-avatar-6"):
        assert required in base
    for name, tokens in themes.items():
        assert set(tokens) == base, f"{name}: missing {base - set(tokens)}, extra {set(tokens) - base}"
        assert tokens["color-scheme"] in ("light", "dark")


# ---------------------------------------------------------------------------
# Stats popup: absent media figures hide their rows; a real zero still renders
# ---------------------------------------------------------------------------


def _load_stats(payload: dict) -> dict:
    """Run the template's real loadStats against a stubbed fetch and return statsData."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    prelude = (
        "const ref = (v) => ({ value: v });\n"
        "const statsData = ref(null), showStatsUI = ref(true), viewerTimezone = ref(null);\n"
        "const lastBackupTime = ref(null), lastBackupTimeSource = ref(null), listenerActive = ref(false);\n"
        "const moment = null;\n"
        f"const fetch = async () => ({{ ok: true, json: async () => ({json.dumps(payload)}) }});\n"
    )
    program = prelude + _setup_function(html, "const loadStats = async () =>")
    program += "\nloadStats().then(() => console.log(JSON.stringify(statsData.value)))\n"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "load_stats.js"
        script.write_text(program, encoding="utf-8")
        result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipIf(NODE is None, "node executable is not installed")
class TestStatsPopupMediaRows(unittest.TestCase):
    def test_absent_media_figures_load_as_null_not_zero(self):
        """A restricted viewer on a pre-change blob gets no media keys: the rows hide, no false 0."""
        got = _load_stats({"chats": 2, "messages": 3})
        self.assertIsNone(got["media_files"])
        self.assertIsNone(got["total_size_mb"])

    def test_a_real_zero_survives_loading(self):
        got = _load_stats({"chats": 0, "messages": 0, "media_files": 0, "total_size_mb": 0.0})
        self.assertEqual(got["media_files"], 0)
        self.assertEqual(got["total_size_mb"], 0)


def test_the_two_media_rows_hide_only_when_the_figure_is_absent():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.count('v-if="statsData.media_files != null"') == 1
    # Archive status's "Media on disk", and the one a viewer reads in Statistics
    # (the owner reads "Disk use" there, which opens Archive status).
    assert html.count('v-if="statsData.total_size_mb != null"') == 1
    assert html.count('v-else-if="statsData.total_size_mb != null"') == 1


def test_disk_use_has_one_home():
    """Statistics holds counts and one Disk use row for the owner; the
    breakdown lives in Archive status, whose media figure is rounded, so no
    row derived from total_size_mb claims an exact byte count."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "@click=\"openStatusPanel('disk')\"" in html
    assert '<span class="tg-row-label">Disk use</span>' in html
    assert 'id="status-disk-title"' in html
    assert html.count('<span class="tg-row-label">Total on disk</span>') == 1
    assert "formatBytesTitle(Math.round(statsData.total_size_mb" not in html
    assert "formatBytesTitle(statusDiskTotal)" not in html
    # The chat's figure sums every media row, downloaded or not: never "on disk".
    assert 'title="Every media file in this chat, downloaded or not">Media size</span>' in html


@unittest.skipIf(NODE is None, "node executable is not installed")
class TestFormatBytes(unittest.TestCase):
    """Every size in the viewer, as Telegram labels storage: B, KB, MB, GB, TB.

    Base 1024. Under 10 of a unit one decimal, from 10 up whole, and the tier
    is chosen on the ROUNDED figure, so a figure never reads "1024 KB". A few
    KB never read as "0 MB". Zero reads "0 B"; an unknown size reads nothing,
    never "0 B", which would be a false fact about the file.
    """

    def test_each_tier_reads_as_measured(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        out = _run_setup_helpers(
            html,
            ("const formatCount = (n) =>", "const formatBytes = (bytes) =>"),
            "[0, null, 812, 1023.6, 1024, 10035, 476774, 1048371, 1677722, 10444000, 25165824, 1073741824 * 1.5,"
            " 3 * 1024 ** 4].map(formatBytes)",
        )
        self.assertEqual(
            out,
            [
                "0 B",
                "",
                "812 B",
                # Rounds up into the next tier instead of reading "1024 B".
                "1.0 KB",
                "1.0 KB",
                "9.8 KB",
                "466 KB",
                "1.0 MB",
                "1.6 MB",
                # 9.96 MB rounds to 10: whole, "10 MB", never "10.0 MB".
                "10 MB",
                "24 MB",
                "1.5 GB",
                "3.0 TB",
            ],
        )

    def test_counts_keep_every_digit(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        out = _run_setup_helpers(
            html,
            ("const formatCount = (n) =>",),
            "[0, 7, 1234, 12345, 1234567, null].map(formatCount)",
        )
        self.assertEqual(out, ["0", "7", "1,234", "12,345", "1,234,567", "0"])
        self.assertNotIn("const formatNumber", html)
        self.assertNotIn("KiB", html)
        self.assertNotIn("MiB", html)

    def test_durations_read_like_the_apps(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        out = _run_setup_helpers(
            html,
            ("const formatDuration = (seconds) =>",),
            "[0, 14, 723, 3765, null, -3].map(formatDuration)",
        )
        self.assertEqual(out, ["0:00", "0:14", "12:03", "1:02:45", "0:00", "0:00"])
        self.assertNotIn("formatAudioTime", html)

    def test_the_chat_figures_carry_the_exact_bytes(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(
            ':title="formatBytesTitle(chatStats.total_size_bytes)">{{ formatBytes(chatStats.total_size_bytes ?? (chatStats.total_size_mb || 0) * 1048576) }}',
            html,
        )
        self.assertIn("`${formatCount(bytes)} bytes`", html)


# ---------------------------------------------------------------------------
# Viewer redesign, phase 1 review: tokens reach the markup that needs them.
# ---------------------------------------------------------------------------


def _lightbox_block(html: str) -> str:
    start = html.index("<!-- Lightbox Modal for Images -->")
    return html[start : html.index("<!-- Admin Panel", start)]


def test_lightbox_controls_are_drawn_for_the_scrim():
    """The scrim is black in every palette, so its controls are on-scrim, never ink.

    Ink is near-black on the light palettes, which made the close and download
    icons disappear on Day and Paper.
    """
    lightbox = _lightbox_block(INDEX_HTML.read_text(encoding="utf-8"))
    assert "text-tg-ink" not in lightbox
    assert lightbox.count("lightbox-control absolute") == 3
    assert 'aria-label="Close"' in lightbox
    assert 'aria-label="Download"' in lightbox
    assert 'aria-label="Show in chat"' in lightbox


def test_bubble_type_follows_the_browser_font_size():
    html = INDEX_HTML.read_text(encoding="utf-8")
    root = html[html.index("        :root {\n            --tg-font:") :]
    root = root[: root.index("\n        }")]
    for token in ("--tg-text-size", "--tg-line-height", "--tg-bubble-pad-y", "--tg-bubble-pad-x"):
        value = re.search(token + r":\s*([^;]+);", root).group(1)
        assert value.endswith("rem"), f"{token} is {value}"


def test_forward_header_has_its_own_colour_token():
    """Two plain lines in the forward colour: no box and no bar, so it never reads as a reply."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('class="forward-header')
    header = html[start : html.index("</div>", start)]
    assert "text-tg-forward" in header
    assert "border-" not in header
    assert "bg-tg-quote-bg" not in header
    assert "--tg-forward: var(--tg-forward-in);" in html
    assert "--tg-forward: var(--tg-forward-out);" in html


def test_selected_chat_row_is_a_class_hover_cannot_override():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "'is-active': selectedChat?.ref === chat.ref" in html
    assert "'bg-tg-active': selectedChat" not in html
    assert ".chat-row.is-active:hover {" in html
    assert "color: rgb(var(--tg-active-muted));" in html


def test_bubble_account_chip_shows_when_several_archived_accounts_speak():
    """The chip shows when the open chat's holders and speakers name more than one account.

    A chat that belongs to one account and hears only that account shows none;
    the header names it. A private chat between two archived accounts belongs to
    one of them, yet both write in it; hiding the chip there would draw both
    sides as the same identity.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "const ids = new Set(selectedChat.value?.accounts || [])" in html
    assert "chatAccountLabels(selectedChat.value).length > 1" not in html
    assert "if (row.sender_account_id != null) ids.add(row.sender_account_id)" in html
    assert "bubbleAccountIds.value.size > 1 ? senderAccountLabel(msg) : ''" in html
    # Incoming: at the name row's far end; outgoing: in the meta row, like a
    # channel signature.
    assert '<span v-if="bubbleAccountLabel(msg)" class="sender-account">' in html
    assert '<span v-if="isOwnMessage(msg) && bubbleAccountLabel(msg)" class="meta-signature order-2">' in html
    assert '<span v-if="senderAccountLabel(msg)" class="account-chip">' not in html


def _bubble_chip_labels(html: str, accounts: list[int], holders: list[int], senders: list[int]) -> list[str]:
    """Run the real chip helpers for one chat and return the chip of each row."""
    return _run_setup_program(
        html,
        (
            "const accountLabels = computed(() =>",
            "const multiAccount = computed(() =>",
            "const accountLabel = (id) =>",
            "const senderAccountLabel = (msg) =>",
            "const bubbleAccountIds = computed(() => {",
            "const bubbleAccountLabel = (msg) =>",
        ),
        "const ref = (value) => ({ value });\n"
        "const computed = (fn) => ({ get value() { return fn() } });\n"
        f"const accountList = ref({json.dumps([{'id': a, 'label': f'Account {a}'} for a in accounts])});\n"
        f"const selectedChat = ref({{ accounts: {json.dumps(holders)} }});\n"
        f"const sortedMessages = ref({json.dumps([{'sender_account_id': s} for s in senders])});",
        "console.log(JSON.stringify(sortedMessages.value.map(bubbleAccountLabel)))",
    )


@unittest.skipUnless(NODE, "node is required to run the chip helpers")
def test_bubble_account_chip_runs_for_holders_and_speakers():
    """Executes the chip rule for the four shapes a chat can take."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # A single-account install never draws a chip.
    assert _bubble_chip_labels(html, [1], [1], [1, 1]) == ["", ""]
    # One holder, and only that account writes: the header already names it.
    assert _bubble_chip_labels(html, [1, 2], [1], [1, 1]) == ["", ""]
    # Two holders, one of them writes: the chip says which one.
    assert _bubble_chip_labels(html, [1, 2], [1, 2], [1, 1]) == ["Account 1", "Account 1"]
    # One holder, two archived accounts write, as in a private chat between them.
    assert _bubble_chip_labels(html, [1, 2], [1], [1, 2]) == ["Account 1", "Account 2"]


def test_reply_quote_is_reachable_from_the_keyboard():
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('<div v-if="msg.reply_to_msg_id" @click="jumpToReply(msg.reply_to_msg_id)"')
    quote = html[start : html.index(">", html.index('class="reply-quote', start))]
    assert 'role="button" tabindex="0"' in quote
    assert '@keydown.enter.prevent="jumpToReply(msg.reply_to_msg_id)"' in quote


def test_deleted_text_stays_readable():
    """An opened deleted message keeps its text colour; a wash of the deleted colour and its header mark it.

    No ring round the whole bubble (it read as an error or a selection) and no
    bar down its start edge (it curled into the corner and cut the tail off).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "{ 'opacity-50': msg.is_deleted }" not in html
    assert "message-text-deleted" not in html
    # An album's caption bubble takes the mark when any of its pictures was deleted.
    assert "isBubbleDeleted(msg) ? 'is-deleted' : ''" in html
    rule = html[html.index(".message-bubble.is-deleted {") :]
    rule = rule[: rule.index("}")]
    assert (
        "background-image: linear-gradient(var(--tg-deleted-wash), var(--tg-deleted-wash)), var(--tg-bubble-image, none);"
        in rule
    )
    # No edge on any palette: a ring cannot follow the tail, and it read as an
    # error or a selection. A dark palette takes a stronger wash instead.
    assert "--tg-deleted-edge" not in html
    assert "box-shadow" not in rule
    # Stronger than the first 6% and 16%, which read as no mark at all. On the
    # light palettes a bright red at 11%: bright enough that the time and links
    # keep 4.5:1 on it (the brick red at 11% would not, see test_sender_avatars).
    # A pale wash lifts a dark bubble, so the dark palettes take a dark,
    # saturated red that keeps the fill's lightness: 22%, 20% on Slate, whose
    # palest name colour sets the limit, and a stronger one over AMOLED's black.
    assert html.count("--tg-deleted-wash: rgb(122 46 46 / 0.22);") == 5
    assert html.count("--tg-deleted-wash: rgb(122 46 46 / 0.2);") == 1
    assert html.count("--tg-deleted-wash: rgb(90 30 30 / 0.38);") == 1
    assert html.count("--tg-deleted-wash: rgb(255 80 72 / 0.11);") == 4
    assert "rgb(163 58 47 / 0.06)" not in html
    assert "rgb(122 46 46 / 0.16)" not in html
    assert "--tg-deleted-wash: rgb(255 154 143" not in html
    assert "inset 0 0 0 1.5px" not in html
    assert "--tg-deleted-bar" not in html
    assert ".message-meta .meta-deleted {" in html
    # The tail takes the wash too.
    assert (
        "background: linear-gradient(var(--tg-deleted-wash), var(--tg-deleted-wash)), rgb(var(--tg-bubble-tail));"
        in html
    )


def test_deleted_boxes_skip_the_wash():
    """The tinted boxes in a deleted bubble paint their tint over the plain fill.

    Their text pairs are measured on the plain fill; over the wash, the quote
    colour on its tint falls under 4.5:1 in Telegram Day (see the positive
    control in test_sender_avatars).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    for selector, token in (
        (".bg-tg-quote-bg", "--tg-quote-bg"),
        (".bg-tg-preview-bg", "--tg-preview-bg"),
        (".tg-blockquote", "--tg-blockquote-bg"),
        (".reaction-chip", "--tg-reaction-bg"),
    ):
        rule = html[html.index(f".message-bubble.is-deleted {selector} {{") :]
        rule = rule[: rule.index("}")]
        assert f"background: linear-gradient(var({token}), var({token})), rgb(var(--tg-bubble-fill));" in rule


def test_the_tail_takes_the_bubble_bottom_colour():
    """iOS Night draws outgoing bubbles with a gradient; the tail takes its last stop, not the top fill."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    tail = html[html.index(".message-row.run-end > .message-bubble::after {") :]
    tail = tail[: tail.index("}")]
    assert "background: rgb(var(--tg-bubble-tail));" in tail
    assert "--tg-bubble-tail: var(--tg-other);" in html
    assert "--tg-bubble-tail: var(--tg-own-tail);" in html
    blocks = _theme_blocks(html)
    assert blocks["iosnight"]["--tg-own-tail"] == "28 77 184"
    assert "#1C4DB8)" in blocks["iosnight"]["--tg-own-image"]
    for name, tokens in blocks.items():
        if tokens["--tg-own-image"] == "none":
            assert tokens["--tg-own-tail"] == tokens["--tg-own"], name


def test_a_failed_load_is_not_an_empty_chat():
    """After failed fetches the pane offers a retry instead of saying the chat is empty."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'v-else-if="!loading && messagesLoadError && sortedMessages.length === 0"' in html
    assert html.index("messagesLoadError && sortedMessages.length === 0") < html.index(
        "No messages to show in this chat"
    )
    assert "if (chatVersion === myVersion) messagesLoadError.value = true" in html
    assert '@click="retryLoadMessages"' in html


def test_an_empty_chat_says_so():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'v-else-if="!loading && !hasMore && sortedMessages.length === 0"' in html
    assert "No messages to show in this chat" in html


def test_search_hits_take_the_chat_rows_peer_colour():
    """A message hit carries the chat's ref, not its id, so its colour comes from the chat list."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert ':style="{ background: getSearchChatAvatarFill(row.chat) }"' in html
    assert "getChatAvatarFill(row.chat)" not in html
    start = html.index("const getSearchChatAvatarFill = (chat) =>")
    body = html[start : html.index("\n                }\n", start)]
    assert "chats.value.find(c => c.ref === chat?.ref)" in body
    assert "return `var(--tg-avatar-${hash % 7})`" in body


def test_header_popovers_are_a_bottom_sheet_on_phones():
    """On a phone the main menu, More actions and the What changed filter span the screen instead of running off its edge."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.count('class="popover-sheet ') == 3
    sheet = html[html.index("        @media (max-width: 767px) {\n            html .popover-sheet {") :]
    sheet = sheet[: sheet.index("\n        }\n")]
    for rule in ("position: fixed;", "left: 0;", "right: 0;", "bottom: 0;", "max-height: 70vh;", "var(--sab)"):
        assert rule in sheet
    assert "min-height: 48px;" in sheet


def test_the_viewer_uses_the_system_font_and_ships_no_webfont():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "inter.css" not in html
    assert "'Inter'" not in html
    assert '--tg-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,' in html


def test_the_meta_row_is_twelve_pixels_with_tabular_figures():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "--tg-meta-size: 0.75rem;" in html
    meta = html[html.index("        .message-meta {") :]
    meta = meta[: meta.index("\n        }")]
    assert "font-size: var(--tg-meta-size);" in meta
    assert "font-variant-numeric: tabular-nums;" in meta
    assert 'class="message-meta text-[10px]' not in html


def test_archive_figures_use_one_icon_set_not_emoji():
    """The stats menu and the info panel's Archive section. The chat header carries
    no figures: it is one bar, and the panel shows them with room to read."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for emoji in ("📊", "💬 Chats", "📬", "🖼️ Media", "💾"):
        assert emoji not in html, emoji
    assert "stats-inline" not in html
    # Statistics and "In the archive" share one row shape (.tg-row): a stroked
    # icon, the label, the figure.
    stats = html[html.index("<template v-else-if=\"mainMenuPage === 'stats'\">") :]
    stats = stats[: stats.index("</template>")]
    assert stats.count('<div class="tg-row"><svg') == 2
    assert stats.count('class="tg-row"><svg') == 4
    assert (
        html.count(
            '<button v-if="chatStats.deleted_messages > 0" type="button" class="tg-row" @click="openDeletedOnlyFromInfo"'
        )
        == 1
    )
    assert ".info-row" not in html
    assert 'aria-labelledby="info-archive-title"' in html
    assert ">In the archive</h4>" in html


# ---------------------------------------------------------------------------
# Viewer redesign, phase 2 review: palettes reach every surface they paint.
# ---------------------------------------------------------------------------


def test_page_chrome_paints_the_header_not_the_wallpaper():
    """--tg-bg is the wallpaper base; the safe-area insets and the bounce show the page.

    With --tg-bg there, a home-screen app on Telegram Day drew a green status bar
    strip above a white header.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    for selector in ("        html {", "        body {"):
        rule = html[html.index(selector) :]
        rule = rule[: rule.index("\n        }")]
        assert "background-color: rgb(var(--tg-header));" in rule, selector
        assert "--tg-bg" not in rule, selector
    assert '<div v-else class="flex w-full h-full bg-tg-header">' in html


def test_no_palette_shadow_is_none():
    """A shadow token is composed into lists (the deleted ring); `none` there drops the rule."""
    themes = _theme_blocks(INDEX_HTML.read_text(encoding="utf-8"))
    for name, tokens in themes.items():
        for token, value in tokens.items():
            if "shadow" in token:
                assert value.strip() != "none", f"{name} {token}"


def test_every_palette_token_is_read():
    """A token nothing reads is a promise the page does not keep (the old --tg-tail)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    base = _theme_blocks(html)["telegram"]
    for token in base:
        if token == "color-scheme" or re.fullmatch(r"--tg-avatar-\d", token):
            continue  # avatars are read as var(--tg-avatar-${index}) by getChatAvatarFill
        assert f"var({token})" in html or f"var({token}," in html, token
    assert "`var(--tg-avatar-${getPeerIndex(" in html


def test_every_static_file_a_palette_names_exists():
    """The wallpaper patterns are files; a missing one silently drops the pattern."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    static = INDEX_HTML.parents[1] / "static"
    urls = set()
    for tokens in _theme_blocks(html).values():
        for value in tokens.values():
            urls.update(re.findall(r"url\('/static/([^']+)'\)", value))
    assert urls, "no palette names a static file: the check would pass on nothing"
    for rel in urls:
        assert (static / rel).is_file(), rel


def test_accent_is_a_fill_never_panel_text():
    """--tg-accent is the fill colour; as text it misses 4.5:1 on the light panels.

    Link-coloured text on a panel is accent-soft, measured in every palette.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert not re.search(r"text-tg-accent(?![-\w])", html)
    assert "hover:bg-tg-accent " not in html
    assert "hover:bg-tg-n500" not in html


def test_bubbles_draw_no_neutral_scale_text():
    """Inside a bubble, text takes the side's colours (text, meta, quote): the neutral
    scale is measured on the panels, not on an outgoing bubble."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("<!-- Regular Message. Album")
    rows = html[start : html.index("<!-- Scroll to Bottom Button -->", start)]
    assert not re.search(r"text-tg-n\d00", rows)
    assert "opacity-80" not in rows
    assert ".message-bubble.bubble-out {\n            --tg-focus: var(--tg-quote-out);" in html


def test_a_jump_marks_its_target_in_the_selection_colour():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "bg-tg-warning/10" not in html
    assert "el.classList.add('message-flash')" in html
    flash = html[html.index("@keyframes message-flash") :]
    assert "var(--tg-selection-bg)" in flash[: flash.index("}")]


def test_a_failed_load_does_not_look_like_loading():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The chat's two, the feed's, the edit history's and Archive status's.
    assert html.count('class="pane-note is-error"') == 5
    assert ".pane-note.is-error {" in html
    start = html.index('@click="retryNewerMessages"')
    assert "min-h-[40px]" in html[start : html.index(">", start)]


def test_the_info_panel_counts_what_the_archive_kept():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The deleted count opens the chat's "Deleted only" list, the edited one its "Edited only" list.
    assert (
        '<button v-if="chatStats.deleted_messages > 0" type="button" class="tg-row" @click="openDeletedOnlyFromInfo"'
        in html
    )
    assert (
        '<button v-if="chatStats.edited_messages > 0" type="button" class="tg-row" @click="openEditedOnlyFromInfo"'
        in html
    )
    # The message's deletion is a fact in the calm deleted colour, never a red pill.
    status = html[html.index('<div v-if="infoPanelMessage.is_deleted" class="tg-fact">') :]
    status = status[: status.index("</div>\n                            </div>")]
    assert "text-tg-deleted" in status
    assert "The archive kept this message." in status
    aside = html[html.index('<aside v-if="showInfoPanel && selectedChat" id="info-panel"') :]
    assert "bg-tg-danger/15" not in aside[: aside.index("</aside>")]


def test_search_fields_keep_16px_and_40px_on_a_phone():
    """iOS Safari zooms the page into any field under 16px and stays zoomed.

    Every search or filter field keeps 16px text and a 40px height below the
    desktop breakpoint; the smaller desktop size rides a breakpoint prefix.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    fields = []
    # Attribute values can hold ">" (a Vue expression), so the tag ends at the
    # first ">" outside quotes.
    for match in re.finditer(r"""<input\b(?:[^>"']|"[^"]*"|'[^']*')*>""", html, re.S):
        tag = match.group(0)
        label = re.search(r'aria-label="([^"]*)"', tag)
        if label and re.match(r"(Search|Filter)", label.group(1)):
            fields.append((label.group(1), re.search(r'\bclass="([^"]*)"', tag).group(1).split()))
    assert len(fields) >= 3, fields
    for label, classes in fields:
        assert "text-base" in classes, label
        assert not {"text-sm", "text-xs"} & set(classes), f"{label}: bare small text"
        assert "h-10" in classes, label
        assert "h-9" not in classes, f"{label}: bare 36px height"


# ---------------------------------------------------------------------------
# Viewer redesign, phase 3 review.
# ---------------------------------------------------------------------------


def test_a_media_only_picture_keeps_its_top_edge():
    """The edge block's first-child rule pulls a picture up by the bubble's top
    padding. A media-only bubble has no padding, so that pull cut the top 6px
    off every caption-less photo. The media-only rule must match the
    first-child selector's weight (0,4,0) and reset the margin."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert ".message-bubble .media-block.is-edge:first-child {" in html
    rule_start = html.index(".message-bubble.bubble-media-only .media-block,\n")
    rule = html[rule_start : html.index("}", rule_start)]
    assert ".message-bubble.bubble-media-only .media-block.is-edge:first-child {" in rule
    assert "margin: 0;" in rule
    # And it comes after the first-child rule, so equal weight resolves its way.
    assert html.index(".message-bubble .media-block.is-edge:first-child {") < rule_start


@unittest.skipUnless(NODE, "node is required to run the frame helpers")
def test_only_a_drawn_picture_stands_without_a_frame():
    """A round video with its transcript open or failed, a clip that failed to
    load, and an animated sticker (a text label) all keep a bubble."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    result = _run_setup_program(
        html,
        ("const isStickerImage = (msg) =>", "const rendersAsPicture = (msg) =>"),
        "const getDocumentDisplayName = (msg) => msg.name || '';\n"
        "const hasTranscriptButton = (msg) => Boolean(msg.transcribable);\n"
        "const isTranscriptExpanded = (msg) => Boolean(msg.open);\n"
        "const transcriptStatus = (msg) => msg.status || 'idle';\n",
        "const cases = {\n"
        "  sticker: { media: { type: 'sticker' }, name: 'a.webp' },\n"
        "  animatedSticker: { media: { type: 'sticker' }, name: 'a.tgs' },\n"
        "  round: { media: { type: 'video_note' }, transcribable: true },\n"
        "  roundOpen: { media: { type: 'video_note' }, transcribable: true, open: true },\n"
        "  roundError: { media: { type: 'video_note' }, transcribable: true, status: 'error' },\n"
        "  roundFailed: { media: { type: 'video_note' }, mediaLoadFailed: true },\n"
        "  video: { media: { type: 'video' } },\n"
        "  videoOpen: { media: { type: 'video' }, transcribable: true, open: true },\n"
        "};\n"
        "console.log(JSON.stringify(Object.fromEntries(Object.entries(cases).map(([k, m]) => [k, rendersAsPicture(m)]))))",
    )
    assert result == {
        "sticker": True,
        "animatedSticker": False,
        "round": True,
        "roundOpen": False,
        "roundError": False,
        "roundFailed": False,
        "video": True,
        "videoOpen": False,
    }
    only = html[html.index("const isMediaOnlyMessage = (msg, index) =>") :]
    only = only[: only.index("\n                }\n")]
    assert "if (!rendersAsPicture(msg)) return false" in only


def test_an_incoming_video_keeps_its_time_off_the_transcript_button():
    """The transcript button sits on the bottom right of an incoming video; the
    time pill takes the bottom left, as on a round video, and a video's open
    transcript takes the bubble's padding back inside the edge block."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    rule_start = html.index(".message-bubble.bubble-media-only.bubble-in:has(.transcript-btn--overlay) .message-meta {")
    rule = html[rule_start : html.index("}", rule_start)]
    assert "left: 6px;" in rule and "right: auto;" in rule
    assert 'class="transcript-region mt-2">' in html
    region = html[html.index(".message-bubble .media-block.is-edge .transcript-region {") :]
    assert "padding: 0 var(--tg-bubble-pad-x);" in region[: region.index("}")]


def test_closing_the_chat_search_returns_focus_to_its_button():
    html = INDEX_HTML.read_text(encoding="utf-8")
    close = html[html.index("const closeChatSearch = async () =>") :]
    close = close[: close.index("\n                }\n")]
    assert close.index("chatSearchOpen.value = false") < close.index("await nextTick()")
    assert close.rstrip().endswith("chatSearchButton.value?.focus()")
    assert 'ref="chatSearchButton"' in html
    # Focus moving between the field and its close button keeps the bar open.
    out = html[html.index("const onChatSearchFocusOut = (event) =>") :]
    assert "event.currentTarget.contains(event.relatedTarget)" in out[: out.index("\n                }\n")]
    assert '@focusout="onChatSearchFocusOut"' in html
    # A topic switch inside a forum closes it as a chat switch does.
    switch = html[html.index("watch(() => [selectedChat.value?.ref, selectedPaneTopic.value?.id], () => {") :]
    assert "chatSearchOpen.value = false" in switch[: switch.index("\n                })")]


def test_pictures_open_from_the_keyboard():
    """Photos, album tiles and gallery tiles are buttons that open the lightbox."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<button type="button" class="album-item media-open' in html
    assert 'class="media-open cursor-pointer hover:opacity-90 transition"' in html
    assert (
        ":aria-label=\"isBubbleDeleted(msg) ? 'Open photo, deleted in Telegram' : 'Open photo'\" @click=\"openMedia(msg)\""
        in html
    )
    assert 'class="media-open aspect-square relative cursor-pointer group overflow-hidden"' in html
    ring = html[html.index(".media-open:focus-visible::after {") :]
    assert "inset 0 0 0 3px rgb(var(--tg-focus))" in ring[: ring.index("}")]


def test_one_pinned_message_is_not_plural():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'View all ${pinnedMessages.length} pinned messages`"' not in html.replace(
        "pinnedMessages.length === 1 ? 'Open pinned message' : `View all ${pinnedMessages.length} pinned messages`", ""
    )


# ---------------------------------------------------------------------------
# Viewer redesign, phase 4 review: the archive's own views.
# ---------------------------------------------------------------------------


def test_poll_vote_count_shares_the_time_line():
    """The vote count is inline text in the message body, so the floated time joins it."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    body = html[html.index('<div class="message-body" dir="auto">') :]
    body = body[: body.index('<span class="message-meta" dir="ltr">')]
    assert '<span v-if="msg.raw_data?.poll?.results" class="poll-votes text-tg-meta">' in body
    poll = html[html.index('<div v-if="msg.raw_data?.poll" class="poll') :]
    poll = poll[: poll.index("<!-- Extended media chip")]
    assert "poll-votes" not in poll


def test_pinned_bar_shows_no_time():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "formatTime(pinnedMessage.date)" not in html


def test_folder_tabs_carry_no_count_that_reads_as_unread():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<span class="folder-tab-count">{{ folder.chat_count }}</span>' not in html
    assert ":title=\"`${folder.chat_count} ${folder.chat_count === 1 ? 'chat' : 'chats'}`\"" in html
    # An empty Shared Media tab is left out, unless it is the open one, and
    # a count shows only when it is not 0.
    assert "mediaTabs.filter(tab => mediaGalleryCounts.value[tab.id] !== 0 || tab.id === mediaGalleryTab.value)" in html
    assert '<button v-for="tab in galleryTabs"' in html
    assert '<span v-if="mediaGalleryCounts[tab.id]" class="folder-tab-count">' in html


def test_what_changed_has_its_own_glyph_and_an_unseen_dot():
    """The clock with an arrow means "edited" on a message; the feed has a list
    with a small clock."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    button = html[html.index('<button @click="openChangesFeed"') :]
    button = button[: button.index("</button>")]
    assert '<circle cx="16.5" cy="16.5" r="5"/><path d="M16.5 14v2.5l1.6 1"/>' in button
    assert "M3 12a9 9 0 1 0 9-9" not in button
    assert '<span v-if="changesUnseen" class="changes-dot" aria-hidden="true"></span>' in button
    opener = html[html.index("const openChangesFeed = (fromMenu = false, chat = null, returnFocus = null) => {") :]
    opener = opener[: opener.index("\n                }")]
    assert opener.count("markChangesSeen()") == 1
    # Narrowed to one chat, opening the feed says nothing about the others.
    assert "if (!changesChat.value) markChangesSeen()" in opener


def test_lightbox_shows_caption_deleted_tag_and_way_back():
    html = INDEX_HTML.read_text(encoding="utf-8")
    lightbox = _lightbox_block(html)
    assert "{{ lightboxCaption(lightboxMedia) }}" in lightbox
    assert '<span v-if="lightboxMedia.is_deleted" class="lightbox-deleted' in lightbox
    assert '@click="lightboxShowInChat"' in lightbox
    assert "-webkit-line-clamp: 3;" in html


# ---------------------------------------------------------------------------
# Viewer redesign, final review: the archive's own marks.
# ---------------------------------------------------------------------------


def test_the_confirm_and_export_dialogs_keep_tab_inside():
    """Both are aria-modal, like the other dialogs: Tab cycles inside them."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '@keydown.tab="cycleTabWithin(confirmDialog, $event)"' in html
    assert '@keydown.tab="cycleTabWithin(exportDialog, $event)"' in html
    assert "                    cycleTabWithin,\n" in html


def test_what_changed_has_a_glyph_of_its_own():
    """A list with a clock in the header and the menu, never the heart pulse
    Archive status uses."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    glyph = '<circle cx="16.5" cy="16.5" r="5"/><path d="M16.5 14v2.5l1.6 1"/>'
    # The sidebar button, the main menu, and the chat's menu and info panel
    # entries that open the feed narrowed to that chat.
    assert html.count(glyph) == 4
    assert "M22 12h-4l-3 9L9 3l-3 9H2" not in html
    menu_row = html[html.index('@click="closeMainMenu(false); openChangesFeed(true)"') :]
    menu_row = menu_row[: menu_row.index("</button>")]
    assert glyph in menu_row


def test_the_transcribed_label_carries_a_glyph():
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('<span v-else class="inline-flex items-center gap-1"><svg')
    label = html[start : html.index("</span>", start)]
    assert label.endswith("transcribed")
    assert '<path d="M14 16h4"/>' in label


def test_hidden_media_names_a_share_link_as_a_link():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "const sessionWord = computed(() => (userRole.value === 'token' ? 'link' : 'login'))" in html
    assert "get hidden() { return `hidden for this ${sessionWord.value}` }," in html
    assert "hidden: 'hidden for this login'" not in html


def test_a_deleted_gallery_tile_uses_the_photo_pill():
    """The mark rides the dark pill a photo's time uses in the chat, bottom
    left, never a white disc; the tile's tooltip gives the deletion's date."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "--tg-media-deleted-fg" not in html
    assert '<span v-if="item.is_deleted" class="media-tile-badge media-tile-deleted">' in html
    assert ':title="item.is_deleted ? deletedTitle(item) : null"' in html
    assert "deleted_at: m.deleted_at || null," in html
