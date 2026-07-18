"""Regression tests for frontend boot-time failures."""

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

    jump_start = html.index("const loadMessagesAroundId = async (messageId) =>")
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

    jump_start = html.index("const loadMessagesAroundId = async (messageId) =>")
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

    jump_start = html.index("const loadMessagesAroundId = async (messageId) =>")
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

    assert "await loadMessagesAroundId(message.id)" in date_body
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

    jump_start = html.index("const loadMessagesAroundId = async (messageId) =>")
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
