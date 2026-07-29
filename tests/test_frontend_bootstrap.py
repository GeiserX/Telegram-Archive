"""Regression tests for frontend boot-time failures."""

import unittest
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


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


def _setup_slice(html: str, declaration: str) -> str:
    """Return one top-level ``const`` body from the root Vue ``setup()``.

    Setup-scope declarations are indented 16 spaces, so the next such line is
    the end of the current one; nested declarations are indented deeper and do
    not terminate the slice.
    """
    start = html.index(declaration)
    return html[start : html.index("\n                const ", start + len(declaration))]


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

    Without planting the rate in ``defaultPlaybackRate`` before ``load()`` AND
    re-applying it once metadata arrives, every new track silently falls back to
    1x while the UI still shows the chosen speed.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    load_body = _setup_slice(html, "const loadAudioTrack = (track) =>")
    assert load_body.index("audioEngine.defaultPlaybackRate = rate") < load_body.index("audioEngine.src = track.url")
    assert load_body.index("audioEngine.src = track.url") < load_body.index("audioEngine.load()")

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
    assert "items.some(item => item.message_id === track.id) || !hasOlder" in extend_body
    # Hitting the cap keeps whatever was fetched instead of failing.
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
    assert "if (requestId !== audioQueueRequestId || !audioQueueBelongsToTrack(track)) return false" in older_body
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
    assert "return false  // paging failure" in older_body

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
