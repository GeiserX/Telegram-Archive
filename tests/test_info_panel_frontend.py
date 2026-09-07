"""The info panel (#433), executed: the real setup-scope block lifted out of the
template and run under node with a stubbed DOM, so these pin behaviour rather
than source text: selection only while the panel is open and never from a
nested control, the live row winning over the clicked object, keyboard walking
in the col-reverse pane, pane widths clamped to the viewport and persisted, and
the Open buttons posting only where the operator configured a command.
"""

import json
import re

from test_frontend_audit_fixes import INDEX_HTML, _extract_const_arrow_function, _run_node

BLOCK_START = "                const selectedMessage = ref(null)\n"
BLOCK_END = "                const selectedPaneTopic = ref(null)\n"

PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const computed = getter => ({ get value() { return getter(); } });
const nextTick = fn => { if (fn) fn(); };
const toasts = [];
const showToast = (message, ms) => toasts.push(message);
const listeners = { keydown: [], pointermove: [], pointerup: [], pointercancel: [] };
let dialogOpen = false;
const bodyClasses = new Set();
const document = {
    addEventListener: (type, fn) => listeners[type].push(fn),
    removeEventListener: (type, fn) => { listeners[type] = listeners[type].filter(f => f !== fn); },
    querySelector: () => dialogOpen ? {} : null,
    body: { classList: { add: c => bodyClasses.add(c), remove: c => bodyClasses.delete(c), contains: c => bodyClasses.has(c) } },
};
const window = { innerWidth: 1400 };
const stored = new Map();
const localStorage = { getItem: k => stored.has(k) ? stored.get(k) : null, setItem: (k, v) => stored.set(k, String(v)) };
const messages = ref([]);
const messagesContainer = ref(null);
const selectedChat = ref({ ref: 'c1', type: 'group', id: -100123 });
const userRole = ref('master');
const albums = new Map();
const getAlbumForMessage = msg => albums.get(msg.id) || null;
const formatFileSize = bytes => `${bytes} B`;
const isDeletedChat = chat => !!chat.deleted;
const getMediaUrl = msg => msg.media?.url || '';
const formatDuration = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const galleryCalls = [];
const switchMediaTab = id => galleryCalls.push(id);
const showMediaGallery = ref(false);
const watchers = [];
const watch = (source, fn) => watchers.push({ source, fn });
const requests = [];
let fetchOk = true;
const fetch = async (url, init) => { requests.push([url, init && init.method]); return { ok: fetchOk, status: fetchOk ? 200 : 500 }; };
let clipboardOk = true;
const navigator = { clipboard: { writeText: async text => { if (!clipboardOk) throw new Error('denied'); navigator.written = text; } } };
const noEvent = { target: { closest: () => null } };
const rowsIn = ids => ({ querySelectorAll: () => ids.map(id => ({ dataset: { msgId: String(id) }, scrollIntoView() { scrolled.push(id); } })) });
const scrolled = [];
"""


def _panel_block(html: str, capabilities: dict) -> str:
    start = html.index(BLOCK_START)
    end = html.index(BLOCK_END, start)
    return html[start:end].replace("__VIEWER_MEDIA_OPEN__", json.dumps(capabilities))


def _script(body: str, capabilities: dict | None = None) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    parts = [PRELUDE, _panel_block(html, capabilities or {"file": False, "path": False})]
    parts.append(_extract_const_arrow_function(html, "isVideoMedia", asynchronous=False))
    parts.append(body)
    return "\n".join(parts)


def test_labels_read_like_the_apps() -> None:
    _run_node(
        _script("""
assert.equal(getChatTypeLabel({ type: 'private' }), 'Private chat');
assert.equal(getChatTypeLabel({ type: 'private', deleted: true }), 'Deleted account');
assert.equal(getChatTypeLabel({ type: 'group' }), 'Private group');
assert.equal(getChatTypeLabel({ type: 'group', username: 'g', is_forum: 1 }), 'Public group with topics');
assert.equal(getChatTypeLabel({ type: 'channel', username: 'c' }), 'Public channel');
assert.equal(getChatTypeLabel({ type: 'channel' }), 'Private channel');
assert.equal(getChatTypeLabel({ type: 'supergroup', username: 's' }), 'Public group', 'an imported supergroup is a group');
assert.equal(getChatTypeLabel(null), '');
assert.equal(chatStatusLine({ type: 'group', participants_count: 1234 }), '1,234 members');
assert.equal(chatStatusLine({ type: 'group', participants_count: 1 }), '1 member');
assert.equal(chatStatusLine({ type: 'channel', participants_count: 20000 }), '20,000 subscribers');
assert.equal(chatStatusLine({ type: 'channel', participants_count: null }), 'Private channel');
assert.equal(chatStatusLine({ type: 'private', username: 'x' }), 'Private chat');
selectedChat.value = { type: 'private' }; assert.equal(infoPanelTitle.value, 'User Info');
selectedChat.value = { type: 'group' }; assert.equal(infoPanelTitle.value, 'Group Info');
selectedChat.value = { type: 'supergroup' }; assert.equal(infoPanelTitle.value, 'Group Info');
selectedChat.value = { type: 'channel' }; assert.equal(infoPanelTitle.value, 'Channel Info');
selectedChat.value = { type: 'weird' }; assert.equal(infoPanelTitle.value, 'Chat Info');
assert.equal(mediaSummaryLine({ mime_type: 'image/jpeg', file_size: 10, width: 4, height: 3 }), 'image/jpeg · 10 B · 4 × 3');
assert.equal(mediaSummaryLine({ type: 'photo' }), 'photo');
assert.equal(mediaSummaryLine({ type: 'video', file_size: 0 }), 'video · 0 B');
assert.equal(mediaSummaryLine({ type: 'video', duration: 754, file_size: 9 }), 'video · 12:34 · 9 B', 'duration leads, as the apps show it');

// Long descriptions fold; the fold resets with the chat.
selectedChat.value = { ref: 'a', type: 'group', description: 'short' };
assert.equal(descriptionIsLong.value, false);
selectedChat.value = { ref: 'a', type: 'group', description: 'x'.repeat(241) };
assert.equal(descriptionIsLong.value, true);
selectedChat.value = { ref: 'a', type: 'group', description: 'a\\nb\\nc\\nd\\ne' };
assert.equal(descriptionIsLong.value, true);
descriptionExpanded.value = true;
watchers[0].fn();
assert.equal(descriptionExpanded.value, false);

openSharedMedia('files');
assert.deepEqual(galleryCalls, ['files']);
assert.equal(showMediaGallery.value, true);

markPreviewFailed(4); markPreviewFailed(5);
assert.deepEqual([...previewFailed.value], [4, 5]);
userRole.value = 'viewer'; assert.equal(showArchivePath.value, false, 'the disk path belongs to the operator');
userRole.value = 'master'; assert.equal(showArchivePath.value, true);
""")
    )


def test_selection_needs_the_panel_and_ignores_the_rows_own_controls() -> None:
    _run_node(
        _script("""
const msg = { id: 7, chat_id: -100123 };
selectMessage(msg, noEvent);
assert.equal(selectedMessage.value, null, 'closed panel: a click selects nothing');
assert.equal(isSelectedMessage(msg), false);

toggleInfoPanel();
assert.equal(showInfoPanel.value, true);
assert.equal(listeners.keydown.length, 1, 'the panel listens for keys while open');
selectMessage(msg, { target: { closest: sel => sel.includes('button') ? {} : null } });
assert.equal(selectedMessage.value, null, 'a click on a control inside the row is not a selection');
window.getSelection = () => ({ toString: () => 'some copied words' });
selectMessage(msg, noEvent);
assert.equal(selectedMessage.value, null, 'the click that ends a text selection is not a choice');
window.getSelection = () => ({ toString: () => '' });
selectMessage(msg, noEvent);
assert.equal(selectedMessage.value, msg);
assert.equal(isSelectedMessage({ id: 7, chat_id: -100123 }), true);
assert.equal(isSelectedMessage({ id: 7, chat_id: -100999 }), false, 'same id in another chat is another message');

// The panel reads the live row with that id once the list is refreshed.
messages.value = [{ id: 7, chat_id: -100123, edit_date: '2026-09-07 10:00:00' }];
assert.equal(infoPanelMessage.value.edit_date, '2026-09-07 10:00:00');
messages.value = [];
assert.equal(infoPanelMessage.value, msg, 'no live row: the clicked object still describes the message');

toggleInfoPanel();
assert.equal(showInfoPanel.value, false);
assert.equal(selectedMessage.value, null, 'closing the panel drops the selection');
assert.equal(listeners.keydown.length, 0, 'and its key listener');
assert.equal(isSelectedMessage(msg), false);
""")
    )


def test_arrow_keys_walk_the_rendered_rows_and_escape_closes() -> None:
    _run_node(
        _script("""
// DOM order is newest first (flex-col-reverse), so ids 30, 20, 10 read bottom to top.
messagesContainer.value = rowsIn([30, 20, 10]);
messages.value = [{ id: 30 }, { id: 20 }, { id: 10 }];
toggleInfoPanel();
const press = (key, extra = {}) => { const e = { key, target: { closest: () => null }, prevented: false, preventDefault() { this.prevented = true; }, ...extra }; listeners.keydown[0](e); return e; };

assert.equal(press('ArrowUp').prevented, true);
assert.equal(selectedMessage.value.id, 10, 'nothing selected: Up starts from the top row');
assert.equal(press('ArrowUp').prevented, false, 'no row above the top: the key is left alone');
assert.equal(selectedMessage.value.id, 10);
press('ArrowDown');
assert.equal(selectedMessage.value.id, 20);
press('ArrowDown');
assert.equal(selectedMessage.value.id, 30);
assert.equal(press('ArrowDown').prevented, false);
assert.deepEqual(scrolled, [10, 20, 30]);

press('ArrowUp', { target: { closest: () => ({}) } });
assert.equal(selectedMessage.value.id, 30, 'keys typed into a field are not navigation');

dialogOpen = true;
press('Escape');
assert.equal(showInfoPanel.value, true, 'a dialog on top owns Escape');
dialogOpen = false;
assert.equal(press('Escape').prevented, true);
assert.equal(showInfoPanel.value, false);

selectedMessage.value = null; showInfoPanel.value = true;
messagesContainer.value = null;
assert.equal(moveMessageSelection(1), false, 'no pane, no move');
""")
    )


def test_files_come_from_the_album_and_the_row_alike() -> None:
    _run_node(
        _script("""
showInfoPanel.value = true;
assert.deepEqual(infoPanelMedia.value, []);
const single = { id: 1, media: { type: 'photo', url: '/media/c1/1_photo' } };
selectedMessage.value = single;
assert.deepEqual(infoPanelMedia.value.map(m => m.id), [1]);
selectedMessage.value = { id: 2, text: 'no media' };
assert.deepEqual(infoPanelMedia.value, []);
const a = { id: 3, media: { type: 'photo' } }, b = { id: 4, media: { type: 'video' } }, c = { id: 5 };
albums.set(3, [a, b, c]);
selectedMessage.value = a;
assert.deepEqual(infoPanelMedia.value.map(m => m.id), [3, 4], 'every album item that has media, in album order');

assert.equal(isVideoMedia({ media: { type: 'video_note' } }), true, 'round videos are videos here too');
assert.equal(isVideoMedia({ media: { type: 'animation' } }), true);
assert.equal(isVideoMedia({ media: { type: 'document', mime_type: 'video/mp4' } }), true);
assert.equal(isVideoMedia({ media: { type: 'document', file_name: 'clip.mp4' } }), false, 'a name is not a type');
assert.equal(isVideoMedia({ media: { type: 'photo' } }), false);
""")
    )


def test_pane_widths_are_validated_clamped_to_the_viewport_and_persisted() -> None:
    _run_node(
        _script("""
assert.equal(chatListWidth.value, 350, 'nothing stored: a quarter of the 1400px window, as the layout always was');
assert.equal(infoPanelWidth.value, 320);
stored.set('chatListWidth', 'garbage'); stored.set('infoPanelWidth', '9999');
assert.equal(readStoredPaneWidth('chatList'), 350, 'garbage is not a width');
window.innerWidth = 3000; assert.equal(defaultPaneWidth('chatList'), 600, 'the quarter is capped'); window.innerWidth = 1400;
assert.equal(readStoredPaneWidth('info'), 320, 'out of range is not a width');
stored.set('chatListWidth', '450');
assert.equal(readStoredPaneWidth('chatList'), 450);

setPaneWidth('chatList', 5000);
assert.equal(chatListWidth.value, 600, 'the pane never exceeds its own maximum');
setPaneWidth('chatList', 10);
assert.equal(chatListWidth.value, 300, 'nor shrinks below its minimum');

// 1400px viewport: chat list 600 + messages 360 leaves 440 for the info panel.
showInfoPanel.value = true;
chatListWidth.value = 600;
setPaneWidth('info', 640);
assert.equal(infoPanelWidth.value, 440, 'the messages pane keeps its minimum width');
window.innerWidth = 700;
setPaneWidth('info', 640);
assert.equal(infoPanelWidth.value, 260, 'a tiny viewport still honours the pane minimum');
window.innerWidth = 1400;

// Drag: the info panel grows to the left; the chat list to the right.
infoPanelWidth.value = 320;
startPaneResize('info', { button: 0, clientX: 1000, preventDefault() {} });
assert.equal(bodyClasses.has('pane-resizing'), true);
assert.deepEqual([listeners.pointermove.length, listeners.pointerup.length, listeners.pointercancel.length], [1, 1, 1]);
listeners.pointermove[0]({ clientX: 900 });
assert.equal(infoPanelWidth.value, 420);
listeners.pointercancel[0]();
assert.equal(bodyClasses.has('pane-resizing'), false);
assert.deepEqual([listeners.pointermove.length, listeners.pointerup.length, listeners.pointercancel.length], [0, 0, 0], 'every listener is gone after a cancel too');
assert.equal(stored.get('infoPanelWidth'), '420', 'the width the drag ended on is what the next visit restores');

startPaneResize('chatList', { button: 2, clientX: 0, preventDefault() {} });
assert.equal(listeners.pointermove.length, 0, 'a right-button press is not a drag');
chatListWidth.value = 320;
startPaneResize('chatList', { button: 0, clientX: 300, preventDefault() {} });
listeners.pointermove[0]({ clientX: 350 });
listeners.pointerup[0]();
assert.equal(chatListWidth.value, 370);
assert.equal(stored.get('chatListWidth'), '370');

const key = k => ({ key: k, preventDefault() {} });
resizePaneByKey('chatList', key('ArrowRight'));
assert.equal(chatListWidth.value, 386);
resizePaneByKey('info', key('ArrowRight'));
assert.equal(infoPanelWidth.value, 404, 'for the info panel, right means narrower');
resizePaneByKey('info', key('Enter'));
assert.equal(infoPanelWidth.value, 404);
assert.equal(stored.get('infoPanelWidth'), '404');
""")
    )


def test_open_buttons_post_to_the_command_routes_and_report_failure() -> None:
    _run_node(
        _script(
            """
(async () => {
    assert.deepEqual(canOpenMedia.value, { file: true, path: false }, 'a button per configured command');
    userRole.value = 'viewer';
    assert.deepEqual(canOpenMedia.value, { file: false, path: false }, 'and only for the master account');
    userRole.value = 'master';

    const msg = { id: 12, media: { url: '/media/c1/12_photo' } };
    await launchMedia(msg, 'open');
    await launchMedia(msg, 'open-path');
    assert.deepEqual(requests, [['/media/open/c1/12_photo', 'POST'], ['/media/open-path/c1/12_photo', 'POST']]);
    assert.deepEqual(toasts, ['Opening the file', 'Showing the folder']);
    fetchOk = false;
    await launchMedia(msg, 'open');
    assert.equal(toasts[toasts.length - 1], 'Could not open the file', 'a failed launch is said, not swallowed');
    await launchMedia({ id: 13, media: {} }, 'open');
    assert.equal(requests.length, 3, 'no URL, no request');

    await copyText('/data/x.jpg', 'Path copied');
    assert.equal(navigator.written, '/data/x.jpg');
    assert.equal(toasts[toasts.length - 1], 'Path copied');
    clipboardOk = false;
    await copyText('again', 'Path copied');
    assert.equal(toasts[toasts.length - 1], 'Clipboard unavailable');
    await copyText('', 'never');
    assert.notEqual(toasts[toasts.length - 1], 'never');
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""",
            {"file": True, "path": False},
        )
    )


def test_the_template_wires_the_panel_the_way_the_functions_expect() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    aside_start = html.index('<aside v-if="showInfoPanel && selectedChat" id="info-panel"')
    aside = html[aside_start : html.index("</aside>", aside_start)]
    assert 'role="complementary"' in aside
    assert 'class="info-panel fixed inset-0 z-40 md:relative' in aside, "a page on phones, a column on desktop"
    assert re.search(r"text-(blue|gray|slate)-\d00|bg-(blue|gray|slate)-\d00", aside) is None, (
        "the panel uses theme tokens only"
    )
    assert aside.count('class="pane-resize-handle hidden md:block"') == 1, "the grab strip is a desktop affordance"
    assert 'ref="infoPanelCloseBtn"' in aside
    assert "formatDateFull(infoPanelMessage.date)" in aside and "formatTime(infoPanelMessage.date)" in aside
    assert "selectedMessage.date" not in aside, "raw timestamps never reach the panel"
    assert 'v-if="canOpenMedia.file' in aside and 'v-if="canOpenMedia.path' in aside
    assert aside.count('@error="markPreviewFailed(mediaMsg.id)"') == 2, "a broken preview gives way to the icon"
    assert 'v-if="showArchivePath && mediaMsg.media.file_path"' in aside, "the disk path is shown to the master only"
    assert "forward_from_name" in aside
    assert "line-clamp-4" in aside and "openSharedMedia(tab.id)" in aside
    assert (
        "@click=\"launchMedia(mediaMsg, 'open')\"" in aside and "@click=\"launchMedia(mediaMsg, 'open-path')\"" in aside
    )

    assert html.count('class="pane-resize-handle hidden md:block"') == 2, "one handle per pane, both desktop only"
    assert 'class="chat-list-pane relative bg-tg-sidebar' in html, "the chat list width is a CSS variable"
    assert "'--chat-list-width': chatListWidth + 'px'" in html
    assert '@click="selectMessage(msg, $event)"' in html
    assert "isSelectedMessage(msg) ? 'message-info-selected' : ''" in html
    assert 'aria-controls="info-panel"' in html

    reset_start = html.index("const resetMessagePagination = () => {")
    reset = html[reset_start : html.index("\n                }\n", reset_start)]
    assert "selectedMessage.value = null" in reset, "every message-list swap drops the selection"
    assert "showCopyToast" not in html and "Ziehen" not in html, "one toast, one language"
