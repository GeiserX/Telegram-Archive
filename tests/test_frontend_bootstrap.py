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
    assert "messages.value = data.messages || data" in jump_body
    assert "resetMessagePagination()" in jump_body
    assert "setupMessagesScrollObserver()" in jump_body
    assert jump_body.index("messages.value = data.messages || data") < jump_body.index("resetMessagePagination()")
    assert jump_body.index("resetMessagePagination()") < jump_body.index("setupMessagesScrollObserver()")


def test_realtime_polling_skips_search_results():
    """Latest-message polling should not mix unfiltered rows into search results."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]
    search_start = html.index("const searchMessages = async () =>")
    search_body = html[search_start : html.index("const handleScroll = (e) =>", search_start)]

    assert "if (!selectedChat.value || isRefreshing || messageSearchQuery.value) return" in refresh_body
    assert "chatVersion++" in search_body


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


def test_poll_deletion_pass_is_range_bounded():
    """Polling must not treat rows outside the server's returned window as deleted."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    refresh_start = html.index("const checkForNewMessages = async () =>")
    refresh_body = html[refresh_start : html.index("const loadMessages = async () =>", refresh_start)]

    assert "const serverOldest = oldestMessageFrom(latestMessages)" in refresh_body
    assert "compareMessagesDesc(m, serverOldest) <= 0" in refresh_body
