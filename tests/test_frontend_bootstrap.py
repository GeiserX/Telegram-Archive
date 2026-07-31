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

from src.message_utils import service_action_type, service_message_text

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"

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
        self.assertGreaterEqual(self.html.count("getSenderRunKey("), 4)
        self.assertIn("return index > 0 && isRunEnd(index)", self.html)
        self.assertIn("isSenderBreak,", self.html)

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
        self.assertIn("senderInfoMessage.sender_name ? 'Archived name'", self.html)
        self.assertIn("getCurrentSenderName(senderInfoMessage) ? 'Latest known name' : 'Name'", self.html)
        self.assertIn("hasDifferentCurrentSenderName(senderInfoMessage)", self.html)
        self.assertIn("senderInfoMessage.sender_id ?? 'Unknown'", self.html)
        self.assertIn("event.key === 'Escape'", self.html)
        self.assertIn("event.key !== 'Tab'", self.html)
        self.assertIn("senderInfoDialog.value.querySelectorAll", self.html)
        self.assertIn("senderInfoCloseBtn.value?.focus()", self.html)
        self.assertIn("trigger?.focus()", self.html)

    def test_imported_document_display_name_hides_storage_prefix(self) -> None:
        """Imported media IDs are storage details and should not appear in the gallery."""
        start = self.html.index("const getMediaDisplayName = (media) =>")
        end = self.html.index("const getDocumentDisplayName = (msg) =>", start)
        body = self.html[start:end]
        self.assertIn("media?.id", body)
        self.assertIn("name.startsWith(storagePrefix)", body)
        self.assertIn("name = name.slice(storagePrefix.length)", body)
        self.assertIn("{{ getMediaDisplayName(item) }}", self.html)


def test_message_versions_are_loaded_only_from_click_handler():
    """Viewer message versions should be fetched lazily from the edited button."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '@click.stop="toggleMessageVersions(msg)"' in html
    assert 'v-if="versionsMessage"' in html
    assert '@click.self="closeVersionsPanel"' in html
    assert "const loadMessageVersions = async (msg) =>" in html
    assert "const toggleMessageVersions = async (msg) =>" in html
    assert "const versionsMessage = ref(null)" in html

    load_start = html.index("const loadMessageVersions = async (msg) =>")
    toggle_start = html.index("const toggleMessageVersions = async (msg) =>")
    versions_fetch = html.index("/versions?limit=100")

    assert load_start < versions_fetch < toggle_start
    assert html.count("/versions?limit=100") == 1
    assert "/edits?limit=100" not in html


def test_message_versions_trigger_is_plain_text():
    """The edited trigger should stay visually quiet in message metadata."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "fa-solid fa-pen" not in html
    assert "decoration-dotted" not in html
    assert "underline-offset-2" not in html
    assert "edited({{ msg.version_count }})" in html


def test_edited_without_versions_is_not_clickable():
    """Edited messages should open versions only when retained versions exist."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    clickable = 'v-if="Number(msg.version_count) > 0"'
    fallback = 'v-else-if="msg.edit_date"'
    click_handler = '@click.stop="toggleMessageVersions(msg)"'

    assert clickable in html
    assert fallback in html
    assert html.index(clickable) < html.index(click_handler) < html.index(fallback)
    assert '<span v-else-if="msg.edit_date"' in html
    assert ">edited</span>" in html


def test_versions_can_open_without_edit_date_when_count_exists():
    """Retained versions should be clickable even when the current edit marker is absent."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'v-if="Number(msg.version_count) > 0"' in html
    assert 'v-if="msg.edit_date && Number(msg.version_count) > 0"' not in html
    assert ":title=\"formatMetadataTimestampTitle('Edited', msg.edit_date)\"" in html


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
    """Realtime text edits should keep the edited count in sync without loading versions."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const previousText = editMsg.text" in html
    assert "if (previousText !== data.new_text)" in html
    assert "editMsg.version_count = (Number(editMsg.version_count) || 0) + 1" in html


def test_message_status_badges_show_timestamps_on_hover():
    """Edited/deleted status badges should expose their event timestamps on hover."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    edited_title = ":title=\"formatMetadataTimestampTitle('Edited', msg.edit_date)\""
    deleted_title = ":title=\"formatMetadataTimestampTitle('Deleted', msg.deleted_at)\""
    assert edited_title in html
    assert deleted_title in html
    assert html.index(deleted_title) < html.index(edited_title)
    assert '<span v-if="msg.is_deleted" class="order-1"' in html
    assert '<span class="order-3">{{ formatTime(msg.date) }}</span>' in html
    assert "const formatMetadataTimestampTitle = (label, dateStr) =>" in html
    assert "`${label} ${formatDateFull(dateStr)} ${formatTime(dateStr)}`" in html


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

    drawer_index = html.index("<!-- Message Versions Drawer -->")
    lightbox_index = html.index("<!-- Lightbox Modal for Images -->")
    drawer_html = html[drawer_index:lightbox_index]

    assert 'role="dialog"' in drawer_html
    assert 'aria-modal="true"' in drawer_html


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
    sorted_start = html.index("const sortedMessages = computed(() =>")
    sorted_body = html[sorted_start : html.index("// Group consecutive messages", sorted_start)]

    assert "moment.utc(msg.date)" in helper_body
    assert "sortTimeCache" in helper_body
    assert "messageSortTime(b) - messageSortTime(a)" in helper_body
    assert "(Number(b?.id) || 0) - (Number(a?.id) || 0)" in helper_body
    assert "return sortedLoadedMessages()" in sorted_body


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
    assert "if (chatVersion !== myVersion || messageSearchQuery.value) return" in refresh_body
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
    assert "scrollIntoView({ behavior: 'smooth', block: 'center' })" in scroll_body


def test_websocket_new_message_respects_pinned_window_and_search():
    """The WS path must honor the same guards as the poll — it was the ungated snap-back writer (#213)."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    ws_start = html.index("case 'new_message':")
    ws_body = html[ws_start : html.index("case 'edit':", ws_start)]

    assert "if (viewingPinnedWindow.value || messageSearchQuery.value)" in ws_body
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

    assert "isRefreshing || messageSearchQuery.value" in refresh_body
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

    topic_start = html.index("const selectTopic = async (chat, topic) =>")
    topic_body = html[topic_start : html.index("const activeTab = computed", topic_start)]
    chat_start = html.index("const selectChat = async (chat) =>")
    chat_body = html[chat_start : html.index("const startMessageRefresh = () =>", chat_start)]
    search_start = html.index("const searchMessages = async () =>")
    search_body = html[search_start : html.index("const handleScroll = (e) =>", search_start)]

    assert "resetMessagePagination()" in topic_body
    assert "resetMessagePagination()" in chat_body
    assert "resetMessagePagination()" in search_body


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
    jump_body = html[jump_start : html.index("const downloadMedia = (item) =>", jump_start)]

    assert "let galleryReturnState = null" in html
    assert "scrollTop: messagesContainer.value ? messagesContainer.value.scrollTop : 0" in watcher_body
    assert "document.activeElement instanceof HTMLElement" in watcher_body
    assert "returnState.focusElement.isConnected" in watcher_body
    # Programmatic exits reposition the view themselves and must not restore.
    assert "galleryReturnState = null" in jump_body
    # Restore happens after the observer reconnect, inside the same guarded block.
    assert watcher_body.index("setupMessagesScrollObserver()") < watcher_body.index(
        "returnState.chatId === (selectedChat.value?.id ?? null)"
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
    assert "' new message(s) — scroll to latest'" in html


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
    # Same chat-scope guard as the 'edit' case, wholesale-replace the reactions array.
    assert "selectedChat.value?.id !== data.chat_id" in reaction_body
    assert "reactionMsg.reactions = data.reactions" in reaction_body
    # The reactions block renders the aggregate shape the server sends.
    assert 'v-for="reaction in msg.reactions"' in html


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


def test_flatpickr_month_select_has_dark_native_colors():
    """The native Flatpickr month select and its options must remain readable in dark mode."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert ".flatpickr-monthDropdown-months {" in html
    assert "color-scheme: dark;" in html
    assert ".flatpickr-monthDropdown-month {" in html
    assert "background: #334155 !important;" in html
    assert "color: #e2e8f0 !important;" in html


def test_date_picker_fetches_month_availability_and_marks_days():
    """Calendar open/month/year changes fetch availability and decorate, never disable, dates."""
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
    assert "calendar-availability-dot" in html
    assert "dayElem.setAttribute('aria-label'" in html
    assert "dayElem.title =" in html
    assert "disable:" not in html[html.index("flatpickr(datePickerInput.value") : html.index("const closeDatePicker")]


def test_calendar_availability_is_topic_scoped_stale_safe_and_fail_open():
    """Availability cache writes must be scoped and stale responses ignored; failures leave days enabled."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    availability_start = html.index("const loadCalendarAvailability = async (year, month) =>")
    availability_body = html[availability_start : html.index("const openDatePicker", availability_start)]
    assert "calendarAvailabilityKey(chatId, topicId, timezone, monthKey)" in availability_body
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

    topic_start = html.index("const selectTopic = async (chat, topic) =>")
    topic_body = html[topic_start : html.index("const activeTab = computed", topic_start)]
    assert "selectedPaneTopic.value = topic" in topic_body
    assert topic_body.index("selectedPaneTopic.value = topic") < topic_body.index("await loadMessages()")

    chat_start = html.index("const selectChat = async (chat) =>")
    chat_body = html[chat_start : html.index("const startMessageRefresh", chat_start)]
    assert "selectedPaneTopic.value = null" in chat_body

    back_start = html.index("const navigateBack = () =>")
    back_body = html[back_start : html.index("const loadFolders", back_start)]
    assert "selectedPaneTopic.value" not in back_body
    assert "Main panel keeps showing current topic messages" in back_body

    assert ":class=\"{'bg-tg-active': selectedPaneTopic?.id === topic.id}\"" in html
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
    assert 'aria-label="Close date picker"' in html
    assert 'aria-label="Date to jump to"' in html
    assert "disableMobile: true" in html
    assert "appendTo: datePickerDialog.value" in html

    handler_start = html.index("const handleDatePickerKeydown = (event) =>")
    handler_body = html[handler_start : html.index("const openDatePicker", handler_start)]
    assert "event.key === 'Escape'" in handler_body
    assert "event.key !== 'Tab'" in handler_body
    assert "datePickerDialog.value.querySelectorAll" in handler_body
    assert "event.preventDefault()" in handler_body

    open_start = html.index("const openDatePicker = (initialDate) =>")
    open_body = html[open_start : html.index("const closeDatePicker", open_start)]
    assert "document.activeElement instanceof HTMLElement" in open_body
    assert "document.addEventListener('keydown', handleDatePickerKeydown)" in open_body
    assert "datePickerInput.value?.focus()" in open_body

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
    assert "Availability unavailable; all dates remain selectable." in html

    availability_start = html.index("const loadCalendarAvailability = async (year, month) =>")
    availability_body = html[availability_start : html.index("const handleDatePickerKeydown", availability_start)]
    assert "calendarAvailabilityInFlight.has(cacheKey)" in availability_body
    assert "calendarAvailabilityInFlight.add(cacheKey)" in availability_body
    assert "calendarAvailabilityInFlight.delete(cacheKey)" in availability_body
    assert "calendarAvailabilityActiveKey = cacheKey" in availability_body
    assert "calendarAvailabilityActiveKey !== cacheKey" in availability_body
    assert "calendarAvailabilityLoading.value = true" in availability_body
    assert "calendarAvailabilityLoading.value = false" in availability_body
    assert (
        "calendarAvailabilityError.value = 'Availability unavailable; all dates remain selectable.'"
        in availability_body
    )
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

    # ...and no other rule may reintroduce per-day stickiness.
    assert "position: sticky" not in html


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
    assert body.index("w-20 h-20 rounded-full") < body.index('<dl class="mt-4 space-y-3 text-sm">')

    # Same photo the message row resolved, with a fallback on load failure.
    assert 'v-if="senderInfoMessage.sender_avatar_url"' in body
    assert '@error="senderInfoMessage.sender_avatar_url = null"' in body

    # Fallback reuses the existing initials + deterministic gradient helpers.
    assert "getSenderInitials(senderInfoMessage)" in body
    assert "getAvatarFill(senderInfoMessage)" in body

    # Decorative only: it must not become a focusable child of the dialog's Tab trap.
    avatar = body[body.index("w-20 h-20 rounded-full") : body.index('<dl class="mt-4 space-y-3 text-sm">')]
    assert "<button" not in avatar
    assert "<a " not in avatar


def test_private_chat_header_avatar_opens_sender_details():
    """#240: the 1:1 header photo is the counterpart, so it opens the same popup."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index('<button v-if="selectedChat?.type === \'private\'" type="button"')
    body = html[start : html.index("</button>", start)]

    # Real <button> => native Enter/Space activation.
    assert body.startswith('<button v-if="selectedChat?.type === \'private\'" type="button"')
    # $event must be forwarded or openSenderInfo cannot restore focus on close.
    assert '@click="openSenderInfoFromChat(selectedChat, $event)"' in body
    assert ":aria-label=" in body
    assert "getChatName(selectedChat)" in body
    assert "focus:ring-2 focus:ring-blue-400" in body

    # Groups/channels keep the non-interactive circle (that photo is the group, not a sender).
    assert "<div v-else" in html[html.index("</button>", start) :][:400]

    # The original message-row call site stays an independent second trigger.
    assert '@click="openSenderInfo(msg, $event)"' in html


def test_chat_header_sender_trigger_maps_chat_fields_to_message_shape():
    """#240: chats carry id/avatar_url, the dialog reads sender_id/sender_avatar_url."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index("const openSenderInfoFromChat = (chat, event) =>")
    body = html[start : html.index("}, event)", start)]

    assert "sender_id: chat.id" in body
    assert "sender_avatar_url: chat.avatar_url" in body
    assert "sender_name: null" in body
    assert "first_name: chat.first_name" in body
    assert "last_name: chat.last_name" in body
    assert "username: chat.username" in body

    # Must be exposed to the template.
    assert "openSenderInfoFromChat," in html


def test_chat_header_avatar_button_is_not_a_tap_target():
    """.tap-target forces 44px minimums on mobile and would deform the 40px circle."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index('<button v-if="selectedChat?.type === \'private\'" type="button"')
    body = html[start : html.index("</button>", start)]

    assert "tap-target" not in body
    assert "aspect-square" in body


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
    "const fetchAudioQueuePage = async (chatId, kind, beforeId) =>",
    "const extendAudioQueueFromMedia = async (track) =>",
    "const extendAudioQueueOlder = async () =>",
)


def test_audio_queue_pages_the_media_endpoint_with_a_capped_cursor_walk():
    """#254: the queue grows from the media endpoint's own cursor, not the pane's.

    Playback runs oldest -> newest while ``/media`` pages newest -> oldest, so
    paging back until the playing track appears puts every possible next track in
    hand. The walk must be capped so a huge chat cannot spin forever.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    fetch_body = _setup_slice(html, "const fetchAudioQueuePage = async (chatId, kind, beforeId) =>")
    assert "`/api/chats/${chatId}/media?${params}`" in fetch_body
    assert "types: audioQueueTypes(kind)," in fetch_body
    assert "limit: String(AUDIO_QUEUE_PAGE_SIZE)," in fetch_body
    # The cursor is the composite media id of the last item seen.
    assert "params.set('before_id', beforeId)" in fetch_body
    assert "credentials: 'include'" in fetch_body

    assert "const AUDIO_QUEUE_PAGE_SIZE = 50" in html
    assert "const AUDIO_QUEUE_MAX_PAGES = 10" in html

    extend_body = _setup_slice(html, "const extendAudioQueueFromMedia = async (track) =>")
    assert "for (let page = 0; page < AUDIO_QUEUE_MAX_PAGES; page++)" in extend_body
    assert "await fetchAudioQueuePage(track.chatId, track.kind, cursor)" in extend_body
    assert "cursor = items[items.length - 1].id" in extend_body
    # Stop as soon as the playing track is in hand, or nothing older is left.
    assert "items.some(item => item.message_id === track.id)" in extend_body
    assert "if (!hasOlder) break" in extend_body
    # Hitting the cap keeps whatever was fetched instead of failing — but only
    # when the walk actually reached the playing track (see #257 hole guard).
    assert "audioQueue.value = mergeAudioQueue(audioTracksFromMediaItems(collected, track))" in extend_body

    # "Previous" at the head of the queue pages one more time from the same cursor.
    older_body = _setup_slice(html, "const extendAudioQueueOlder = async () =>")
    assert "await fetchAudioQueuePage(track.chatId, track.kind, audioQueueCursor)" in older_body
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
        "const playNextAudio = () => playAdjacentAudio(1)",
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

    # Auto-advance itself stays a synchronous walk over the player's own queue,
    # so the 'ended' handler can still branch on its boolean result.
    assert "const playNextAudio = () => playAdjacentAudio(1)" in html
    advance_body = _setup_slice(html, "const playAdjacentAudio = (step) =>")
    assert "audioQueue.value[index + step]" in advance_body

    # The queue holds copied descriptors, so emptying messages.value on a chat
    # switch cannot invalidate it.
    item_body = _setup_slice(html, "const audioTrackFromMediaItem = (item, kind, chatName) =>")
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

    # Every page request is keyed on the playing track's own kind.
    extend_body = _setup_slice(html, "const extendAudioQueueFromMedia = async (track) =>")
    assert "fetchAudioQueuePage(track.chatId, track.kind, cursor)" in extend_body
    older_body = _setup_slice(html, "const extendAudioQueueOlder = async () =>")
    assert "fetchAudioQueuePage(track.chatId, track.kind, audioQueueCursor)" in older_body

    # Fetched descriptors inherit that kind, so a merged queue stays single-class.
    tracks_body = _setup_slice(html, "const audioTracksFromMediaItems = (items, track) =>")
    assert "audioTrackFromMediaItem(item, track.kind, track.chatName)" in tracks_body


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
    # Both fetch paths release the flag by ownership token, not by request id.
    assert html.count("if (fetchToken === audioQueueFetchSeq) audioQueueFetching = false") == 2
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
        fetch_body = self._slice("const fetchAudioQueuePage = async (chatId, kind, beforeId) =>")
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
const audioTrack = { value: { chatId: 7, kind: 'voice' } }
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
    audioQueue.value = [{ id: 999, chatId: 7, kind: 'voice', date: '2026-07-09T00:00:00' }]
    await extendAudioQueueFromMedia({ chatId: 7, kind: 'voice', id: 999, chatName: 'c' })
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


class TestMediaUrlEncoding(unittest.TestCase):
    """#258: '#' or '?' in a filename truncated the URL inside the browser."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_get_media_url_encodes_each_segment(self) -> None:
        body = _setup_slice(self.html, "const getMediaUrl = (msg) =>")
        self.assertIn("encodeURIComponent(folder)", body)
        self.assertIn("encodeURIComponent(filename)", body)
        # Per-segment only: encoding the assembled path would escape the '/'.
        self.assertNotIn("encodeURIComponent(`/media/", body)
        self.assertNotIn("encodeURI(`/media/", body)

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
        self.assertLess(body.index("isRenderedMessageRow(currMsg, index)"), body.index("moment.utc(currMsg.date)"))
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
        self.assertIn(":href=\"getMediaUrl(msg) + '?download=1'\"", bubble)
        self.assertIn(':download="getDocumentDisplayName(msg)"', bubble)

    @unittest.skipUnless(NODE, "node is required to execute the helper")
    def test_the_url_term_of_the_guard_is_load_bearing(self) -> None:
        """``getMediaUrl(msg)`` in the ``v-if`` is NOT a dead term.

        The bubble renders on ``isAudioFile(msg)``, which is satisfied by
        ``media.type`` alone — and a media row exists with ``type`` set but NO
        ``file_path`` whenever the file was never written: an oversized voice
        note (``_process_media`` returns ``downloaded: False`` with no path once
        it exceeds ``MAX_MEDIA_SIZE``) or a row still queued for the pending
        -media retry loop (``downloaded=0``, ``file_path`` NULL). The adapter
        emits ``msg.media`` for every row whose ``media_type`` is set, so those
        rows reach the client verbatim and ``getMediaUrl`` returns ``''``.

        Without the term the anchor would render ``href="?download=1"`` — a link
        to the viewer page itself, offered as if the audio were downloadable.
        """
        rows = [
            # Oversized voice note: typed, never written.
            {"id": 1, "media": {"id": "7_1_voice", "type": "voice", "file_path": None}},
            # Pending download of an audio document, name known, file not there.
            {"id": 2, "media": {"id": "7_2_audio", "type": "audio", "file_name": "note.ogg", "file_path": None}},
            # Downloaded: both terms true, anchor renders.
            {"id": 3, "media": {"id": "7_3_voice", "type": "voice", "file_path": "/data/media/7/7_3_voice.ogg"}},
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
                [True, "/media/7/7_3_voice.ogg"],
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
        self.assertEqual(bubble.count("formatAudioTime(msg.media.duration)"), 1)
