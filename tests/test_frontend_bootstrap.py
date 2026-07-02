"""Regression tests for frontend boot-time failures."""

from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


def test_media_gallery_refs_are_initialized_before_watcher():
    """The root Vue setup must not touch media gallery refs before their const declarations."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    state_index = html.index("const showMediaGallery = ref(false)")
    watcher_index = html.index("watch(showMediaGallery")

    assert state_index < watcher_index


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
    assert html.count("messageVersionsRequestSeq.value[key] !== requestSeq") == 2
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
