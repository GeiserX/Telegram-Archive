"""Executable regressions for the frontend audit findings (S16-S18, S22/S30, S38-S43).

The viewer ships as one 7k-line template, so these tests lift the real functions
out of it and RUN them under node: a string assertion cannot tell a debounce that
works from one that was deleted, and every finding here is about behaviour under
a race, a failure, or a hostile CDN.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"
SERVICE_WORKER = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "sw.js"


def _matching_closing_brace(source: str, opening_brace: int) -> int:
    """Find a JavaScript block's closing brace while ignoring strings and comments."""
    depth = 0
    state = "code"
    index = opening_brace

    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""

        if state == "code":
            if char == "/" and following == "/":
                state = "line_comment"
                index += 2
                continue
            if char == "/" and following == "*":
                state = "block_comment"
                index += 2
                continue
            if char in {"'", '"', "`"}:
                state = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
        elif state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and following == "/":
                state = "code"
                index += 2
                continue
        elif char == "\\":
            index += 2
            continue
        elif char == state:
            state = "code"

        index += 1

    raise ValueError("JavaScript function has no balanced closing brace")


def _extract_const_arrow_function(html: str, name: str, *, asynchronous: bool) -> str:
    """Extract a named block-bodied const arrow function from the shipped template."""
    async_prefix = r"async\s+" if asynchronous else ""
    declaration = re.compile(rf"\bconst\s+{re.escape(name)}\s*=\s*{async_prefix}\([^)]*\)\s*=>\s*\{{")
    match = declaration.search(html)
    if match is None:
        function_kind = "async " if asynchronous else ""
        raise ValueError(f"Could not find const {name} = {function_kind}(...) => {{...}}")

    opening_brace = match.end() - 1
    closing_brace = _matching_closing_brace(html, opening_brace)
    return html[match.start() : closing_brace + 1]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")

    # SECURITY-REVIEW: The executable path is resolved locally and untrusted input is never passed to a shell.
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"Node behavior test failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _without_comments(source: str) -> str:
    """Drop ``//`` comments so an assertion reads the code, not the note explaining it."""
    return re.sub(r"//[^\n]*", "", source)


def _console_call_lines(source: str) -> list[str]:
    """Every line that writes to the browser console."""
    return [line for line in source.splitlines() if re.search(r"\bconsole\.(log|warn|error|info|debug)\(", line)]


# --------------------------------------------------------------------------------------
# S17 — the chat-header search ran an unindexed full-chat LIKE scan on every keystroke
# --------------------------------------------------------------------------------------


def test_chat_search_input_is_debounced_not_fired_per_keystroke() -> None:
    """A 10-character query must cost one search, not ten.

    ``searchMessages`` deliberately defeats ``loadMessages``' in-flight gate (it
    resets ``loading`` and bumps ``chatVersion``), so binding it straight to
    ``@input`` really did issue one full-chat ``ILIKE '%...%'`` per keystroke and
    blank the message pane in between.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # The box must go through the debounce, never straight to the loader.
    assert '@input="onMessageSearchInput"' in html
    assert '@input="searchMessages"' not in html

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const messageSearchQuery = ref('');
const selectedChat = ref({ id: 42 });
let messageSearchDebounceTimer = null;
let searchCalls = 0;
const searchMessages = async () => { searchCalls += 1; };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
""",
            _extract_const_arrow_function(html, "onMessageSearchInput", asynchronous=False),
            """
(async () => {
    for (const char of 'birthday22') {
        messageSearchQuery.value += char;
        onMessageSearchInput();
    }
    assert.equal(searchCalls, 0, 'typing must not search before the pause');
    await sleep(450);
    assert.equal(searchCalls, 1, `10 keystrokes issued ${searchCalls} searches`);

    // A chat switch clears the box and rebuilds the pane; the pending timer must
    // not then run the previous chat's query over the new view.
    messageSearchQuery.value = 'holiday';
    onMessageSearchInput();
    selectedChat.value = { id: 99 };
    messageSearchQuery.value = '';
    await sleep(450);
    assert.equal(searchCalls, 1, 'a superseded query ran after the chat changed');
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# S18 — switching media tabs mid-flight painted the old tab's items under the new tab
# --------------------------------------------------------------------------------------


def test_media_gallery_tab_switch_supersedes_the_in_flight_page() -> None:
    """Clicking Files while the Photos page is in flight must load Files.

    The old gate (``if (mediaGalleryLoading.value) return``) refused to start the
    new tab's request, and the photos response then landed in the cleared list —
    so the Files tab rendered photos, terminally, because the skeleton and the
    empty state only render while the list is empty.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const selectedChat = ref({ id: 7, ref: 'ref-chat-7' });
const mediaGalleryTab = ref('photos');
const mediaGalleryItems = ref([]);
const mediaGalleryLoading = ref(false);
const mediaGalleryHasMore = ref(true);
const mediaGalleryCounts = ref({});
let mediaGalleryRequestSeq = 0;
const toasts = [];
const showToast = message => { toasts.push(message); };
const console = { error: () => {} };
const requests = [];
const fetch = async url => {
    let settle;
    const body = new Promise(resolve => { settle = resolve; });
    requests.push({ url, settle });
    return body;
};
""",
            _extract_const_arrow_function(html, "loadMediaGallery", asynchronous=True),
            _extract_const_arrow_function(html, "switchMediaTab", asynchronous=False),
            """
const flush = () => new Promise(resolve => setImmediate(resolve));
const respond = (index, items) => requests[index].settle({
    ok: true,
    json: async () => ({ items, has_more: false }),
});
(async () => {
    loadMediaGallery();
    await flush();
    assert.equal(requests.length, 1);
    assert.ok(requests[0].url.includes('types=photo%2Cvideo%2Canimation'), requests[0].url);

    // Switch tabs while the photos page is still in flight.
    switchMediaTab('files');
    await flush();
    assert.equal(requests.length, 2, 'the new tab never issued its own request');
    assert.ok(requests[1].url.includes('types=document'), requests[1].url);

    // The superseded photos page lands first and must be discarded.
    respond(0, [{ id: 'photo-1', type: 'photo' }]);
    await flush();
    assert.deepEqual(mediaGalleryItems.value, [], 'the old tab painted into the new tab');

    respond(1, [{ id: 'doc-1', type: 'document' }]);
    await flush();
    assert.deepEqual(mediaGalleryItems.value.map(item => item.id), ['doc-1']);
    assert.equal(mediaGalleryTab.value, 'files');
    assert.equal(mediaGalleryLoading.value, false);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_media_gallery_response_that_outlived_its_chat_is_discarded() -> None:
    """A page requested for one chat must never be painted into another."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
// Chats are URL-addressed by their opaque ref; the id is payload data only.
const selectedChat = ref({ id: 111, ref: 'ref-chat-111' });
const mediaGalleryTab = ref('photos');
const mediaGalleryItems = ref([]);
const mediaGalleryLoading = ref(false);
const mediaGalleryHasMore = ref(true);
const mediaGalleryCounts = ref({});
let mediaGalleryRequestSeq = 0;
const showToast = () => {};
const console = { error: () => {} };
const requests = [];
const fetch = async url => {
    let settle;
    const body = new Promise(resolve => { settle = resolve; });
    requests.push({ url, settle });
    return body;
};
""",
            _extract_const_arrow_function(html, "loadMediaGallery", asynchronous=True),
            """
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    loadMediaGallery();
    await flush();
    assert.ok(requests[0].url.startsWith('/api/chats/ref-chat-111/media?'), requests[0].url);

    selectedChat.value = { id: 222, ref: 'ref-chat-222' };
    requests[0].settle({ ok: true, json: async () => ({ items: [{ id: 'from-111' }], has_more: true }) });
    await flush();
    assert.deepEqual(mediaGalleryItems.value, [], "chat 111's media landed in chat 222");
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# A deep link without a message id must still load the chat; a failed jump must not
# strand an empty pane
# --------------------------------------------------------------------------------------


def test_deep_link_without_a_message_loads_the_chat_and_a_failed_jump_falls_back() -> None:
    """The jump path skips selectChat's own load because loadMessagesAroundId replaces it.

    With no message id there is no jump, so skipping would leave the pane empty
    (``/?chat=<ref>`` opened a blank chat); and when the jump itself fails the
    pane must re-enter the view the ordinary way rather than stay blank.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const chats = ref([{ id: 1, ref: 'plainRef', title: 'plain' }, { id: 2, ref: 'forumRef', title: 'forum', is_forum: true }]);
const opened = [];
const selectChat = async (chat, options = {}) => { opened.push([chat.ref, Boolean(options.skipInitialLoad)]); };
const topicsOpened = [];
const selectTopic = async (chat, topic, options = {}) => { topicsOpened.push([chat.ref, topic.id, Boolean(options.skipInitialLoad)]); };
const messages = ref([]);
const selectedPaneTopic = ref(null);
let jumpResult = true;
const jumps = [];
const loadMessagesAroundId = async id => { jumps.push(id); return jumpResult; };
const GENERAL_TOPIC_ID = 1;
const topics = { value: [{ id: 1, title: 'General' }] };
const nextTick = async () => {};
const toasts = [];
const showToast = message => { toasts.push(message); };
const console = { error: () => {}, log: () => {}, warn: () => {} };
const fetch = async () => ({ ok: true, json: async () => [] });
""",
            _extract_const_arrow_function(html, "resolveChatById", asynchronous=True),
            _extract_const_arrow_function(html, "openNotificationTarget", asynchronous=True),
            _extract_const_arrow_function(html, "reloadViewAfterFailedJump", asynchronous=True),
            """
(async () => {
    // No message id: the ordinary entry, which loads the newest page itself.
    assert.equal(await openNotificationTarget('plainRef', null), true);
    assert.deepEqual(opened, [['plainRef', false]]);
    assert.deepEqual(jumps, []);

    // A message id: the window around it is the only load.
    opened.length = 0;
    assert.equal(await openNotificationTarget('plainRef', 4242), true);
    assert.deepEqual(opened, [['plainRef', true]]);
    assert.deepEqual(jumps, [4242]);

    // The jump fails on an empty pane: fall back to the ordinary entry.
    opened.length = 0; jumps.length = 0; jumpResult = false;
    assert.equal(await openNotificationTarget('plainRef', 4243), true);
    assert.deepEqual(opened, [['plainRef', true], ['plainRef', false]]);

    // A stale-view abort is not a failure (undefined, not false): no fallback.
    opened.length = 0; jumpResult = undefined;
    assert.equal(await openNotificationTarget('plainRef', 4244), true);
    assert.deepEqual(opened, [['plainRef', true]]);

    // A forum jump enters its topic without the topic's own load; the fallback re-enters that topic.
    opened.length = 0; topicsOpened.length = 0; jumpResult = false;
    selectedPaneTopic.value = { id: 1, title: 'General' };
    assert.equal(await openNotificationTarget('forumRef', 99), true);
    assert.deepEqual(topicsOpened, [['forumRef', 1, true], ['forumRef', 1, false]]);
    assert.deepEqual(toasts, []);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# Scroll-to-latest from a detached jump window must re-arm the poll it never had
# --------------------------------------------------------------------------------------


def test_scroll_to_latest_restarts_the_poll_after_a_jump_only_entry() -> None:
    """A jump-only entry (selectChat with skipInitialLoad) never starts the 3-second poll.

    Before the skip existed the timer was always running and merely idled while a
    detached window was pinned, so clearing the flag was enough to resume it. Now the
    button that returns the user to the live tail has to start it, and re-arm the
    older-messages observer, exactly as the ordinary chat entry does.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const showScrollToBottom = ref(true);
const unseenMessageCount = ref(3);
const viewingPinnedWindow = ref(true);
const loading = ref(false);
const messages = ref([{ id: 1 }]);
let chatVersion = 7;
const calls = [];
const resetMessagePagination = () => { calls.push('reset'); viewingPinnedWindow.value = false; };
const loadMessages = async () => { calls.push('load'); messages.value = [{ id: 2 }]; };
const nextTick = async () => {};
const setupMessagesScrollObserver = () => { calls.push('observer'); };
const startMessageRefresh = () => { calls.push('poll'); };
const scrollToBottom = () => { calls.push('scroll'); };
""",
            _extract_const_arrow_function(html, "scrollToLatest", asynchronous=True),
            """
(async () => {
    await scrollToLatest();
    assert.deepEqual(calls, ['reset', 'load', 'observer', 'poll', 'scroll']);
    assert.equal(viewingPinnedWindow.value, false);

    // Not pinned: nothing to reload, nothing to re-arm — just scroll.
    calls.length = 0;
    await scrollToLatest();
    assert.deepEqual(calls, ['scroll']);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# In-chat filter marks: an intent set before the load, cleared by every view entry
# --------------------------------------------------------------------------------------


def test_in_chat_filter_sets_the_mark_intent_before_loading_and_every_view_entry_clears_it() -> None:
    """The marks come from a reactive intent, never from the query read after an await.

    searchMessages sets the intent BEFORE loadMessages, so the rows the load renders
    (and every later page) are marked by the applier; an empty query clears it; and
    resetMessagePagination, the chokepoint every view entry passes through, clears it
    too, which is what keeps a chat switch mid-load from marking the wrong pane.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
let chatVersion = 0;
const loading = ref(false);
const messages = ref([{ id: 1 }]);
const messageSearchQuery = ref('needle');
const messageHighlight = ref({ query: 'stale', messageId: 4 });
const order = [];
const page = ref(3);
const hasMore = ref(false);
let oldestMessageCursor = 'x';
let loadFailureStreak = 2;
const hasMoreNewer = ref(true);
const loadingNewer = ref(true);
const newerLoadError = ref('e');
let newestMessageId = 5;
let newerLoadRequestSeq = 0;
const resetCalendarAvailability = () => {};
const viewingPinnedWindow = ref(true);
const messageWindowIsContiguous = ref(true);
const unseenMessageCount = ref(2);
const loadMessages = async () => { order.push(['load', JSON.stringify(messageHighlight.value)]); };
""",
            _extract_const_arrow_function(html, "resetMessagePagination", asynchronous=False),
            _extract_const_arrow_function(html, "searchMessages", asynchronous=True),
            """
(async () => {
    await searchMessages();
    // The load saw the intent already set for its own query.
    assert.deepEqual(order, [['load', JSON.stringify({ query: 'needle', messageId: null })]]);
    assert.deepEqual(messageHighlight.value, { query: 'needle', messageId: null });

    // Clearing the box clears the intent.
    messageSearchQuery.value = '   ';
    await searchMessages();
    assert.equal(messageHighlight.value, null);

    // Any view entry drops whatever the previous view emphasised.
    messageHighlight.value = { query: 'needle', messageId: 7 };
    resetMessagePagination();
    assert.equal(messageHighlight.value, null);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_search_marks_never_mutate_the_vue_managed_container() -> None:
    """The applier may only rewrite nodes inside .message-text (v-html output).

    Vue 3 delimits its fragments with empty text nodes. A normalize() on the
    messages container removed them, and every later patch of the list failed
    silently: the in-chat filter kept showing the tail on the real instance.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    body = _extract_const_arrow_function(html, "applyMessageHighlight", asynchronous=False)
    assert "container.normalize()" not in body
    assert "block.normalize()" in body
    # Unwrapping and marking both start from a .message-text block, never from the container.
    assert "container.querySelectorAll('.message-text')" in body
    assert "container.querySelectorAll('mark" not in body
    marker = _extract_const_arrow_function(html, "markMatchesIn", asynchronous=False)
    assert "textNode.replaceWith(fragment)" in marker
    assert "normalize" not in marker


# --------------------------------------------------------------------------------------
# S16 — the push deep link silently did nothing outside the first 50 non-archived chats
# --------------------------------------------------------------------------------------


def test_notification_deep_link_resolves_a_chat_outside_the_loaded_page() -> None:
    """Archived chats are never in the sidebar page, and they still fire notifications.

    The old code looked the id up in ``chats.value`` (one page of 50, ``archived=false``)
    and, on a miss, fell through with no error — after scrubbing the URL, so a refresh
    could not retry either.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
// The sidebar page: 50 non-archived chats, none of them the notification's target.
// Notifications address chats by their opaque ref (chat ids stay out of URLs).
const chats = ref(Array.from({ length: 50 }, (_, index) => ({ id: 1000 + index, ref: `ref${1000 + index}`, title: 'chat' })));
const archived = { id: -1009999, ref: 'archivedTargetRef', title: 'archived chat', is_archived: true };
const opened = [];
const scrolled = [];
const toasts = [];
const selectChat = async chat => { opened.push(chat.id); };
// The shared opener rides the #367-hardened jump now (scrollToMessage was a
// silent no-op outside the rendered window); the non-forum path never touches
// the topic helpers, but the identifiers must exist for the forum branch.
const loadMessagesAroundId = async id => { scrolled.push(id); };
const GENERAL_TOPIC_ID = 1;
const topics = { value: [] };
const selectTopic = async () => {};
const nextTick = async () => {};
const showToast = message => { toasts.push(message); };
const console = { error: () => {}, log: () => {} };
const requested = [];
const fetch = async url => {
    requested.push(url);
    // The ref route answers one chat row, or 404; the list is never paged for a deep link.
    if (url === '/api/chats/archivedTargetRef') return { ok: true, json: async () => archived };
    return { ok: false, status: 404, json: async () => ({ detail: 'Chat not found' }) };
};
""",
            _extract_const_arrow_function(html, "resolveChatById", asynchronous=True),
            _extract_const_arrow_function(html, "openNotificationTarget", asynchronous=True),
            """
(async () => {
    const wasOpened = await openNotificationTarget('archivedTargetRef', 4242);
    assert.equal(wasOpened, true, 'the deep link silently failed');
    assert.deepEqual(opened, [-1009999]);
    assert.deepEqual(scrolled, [4242]);
    assert.deepEqual(toasts, []);
    assert.equal(requested.length, 1, 'no wider lookup was issued');
    // Resolved by ref through the chat route: any entitled chat, archived or
    // thousands of rows down the list, never a paged scan of the sidebar.
    assert.equal(requested[0], '/api/chats/archivedTargetRef');

    // A chat in the loaded page needs no request at all.
    requested.length = 0;
    assert.equal(await openNotificationTarget('ref1003', null), true);
    assert.deepEqual(requested, []);
    assert.deepEqual(scrolled, [4242]);

    // Genuinely unresolvable: say so instead of doing nothing.
    const missing = await openNotificationTarget('noSuchRefAnywhere', 7);
    assert.equal(missing, false);
    assert.equal(toasts.length, 1, 'a failed deep link stayed silent');
    assert.deepEqual(opened, [-1009999, 1003]);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_failed_deep_link_keeps_the_url_so_a_refresh_retries() -> None:
    """The URL is scrubbed only once the chat actually opened."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    start = html.index("const chatRefParam = urlParams.get('chat')")
    block = html[start : html.index("window.history.replaceState({}, '', '/')", start) + 80]

    assert "const opened = await openNotificationTarget(chatRefParam, msgId)" in block
    assert "if (opened) {" in block
    assert block.index("if (opened) {") < block.index("window.history.replaceState({}, '', '/')")


def test_notification_click_listener_is_not_gated_on_an_active_controller() -> None:
    """On the first load after registration there is no controller yet.

    The service worker now hands clicks to the open tab with ``postMessage``, so a
    listener that is only attached when ``navigator.serviceWorker.controller`` is
    already set would miss exactly the clicks it exists for.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {" not in html
    listener_start = html.index("// Listen for notification clicks when app is already open")
    listener_body = _without_comments(html[listener_start : html.index("finishInitialization()", listener_start)])
    assert "if ('serviceWorker' in navigator) {" in listener_body
    assert "navigator.serviceWorker.controller" not in listener_body
    assert "await openNotificationTarget(chat_ref, message_id)" in listener_body


# --------------------------------------------------------------------------------------
# S38 — loadTopics kept the previous forum chat's topics on screen
# --------------------------------------------------------------------------------------


def test_load_topics_clears_stale_rows_and_surfaces_a_failure() -> None:
    """A 503 must not leave another chat's topics under this chat's header."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const topics = ref([{ id: 77, title: 'chat A topic' }]);
const loadingTopics = ref(false);
const topicsError = ref('');
let topicsRequestSeq = 0;
const console = { error: () => {} };
const fetch = async () => ({ ok: false, status: 503, json: async () => ({}) });
""",
            _extract_const_arrow_function(html, "loadTopics", asynchronous=True),
            """
(async () => {
    await loadTopics(-100222);
    assert.deepEqual(topics.value, [], "the previous chat's topics stayed on screen");
    assert.notEqual(topicsError.value, '', 'the failure was swallowed silently');
    assert.equal(loadingTopics.value, false);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_load_topics_ignores_a_response_that_lost_the_race() -> None:
    """Open forum A then forum B fast: B's list must win even if A answers last."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const topics = ref([]);
const loadingTopics = ref(false);
const topicsError = ref('');
let topicsRequestSeq = 0;
const console = { error: () => {} };
const pending = [];
const fetch = async url => {
    let settle;
    const body = new Promise(resolve => { settle = resolve; });
    pending.push({ url, settle });
    return body;
};
""",
            _extract_const_arrow_function(html, "loadTopics", asynchronous=True),
            """
const flush = () => new Promise(resolve => setImmediate(resolve));
const rows = (label) => ({ ok: true, json: async () => ({ topics: [{ id: 1, title: label }] }) });
(async () => {
    loadTopics(-100111);   // chat A, slow
    await flush();
    loadTopics(-100222);   // chat B, fast
    await flush();
    assert.equal(pending.length, 2);

    pending[1].settle(rows('chat B topic'));
    await flush();
    pending[0].settle(rows('chat A topic'));
    await flush();

    assert.deepEqual(topics.value.map(topic => topic.title), ['chat B topic']);
    assert.equal(loadingTopics.value, false, 'the spinner never cleared');
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# S39 — a notification click hard-reloaded an open app, and could be a silent no-op:
# the tab was selected with `'postMessage' in client`, which is true of EVERY window
# client per spec, so the openWindow fallback was unreachable dead code
# --------------------------------------------------------------------------------------


def _service_worker_click_script(body: str) -> str:
    """Load the REAL sw.js into a stubbed worker global and drive notificationclick."""
    return "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            "const fs = require('node:fs');",
            "const vm = require('node:vm');",
            f"const SW_PATH = {str(SERVICE_WORKER)!r};",
            """
const handlers = {};
const calls = [];
let windowClients = [];
globalThis.self = {
    addEventListener: (type, handler) => { handlers[type] = handler; },
    location: { origin: 'https://archive.example' },
    skipWaiting: () => {},
    clients: { claim: () => {} },
    registration: { showNotification: async () => {} },
};
globalThis.clients = {
    matchAll: async () => windowClients,
    openWindow: async url => { calls.push(['openWindow', url]); return {}; },
};
globalThis.caches = { keys: async () => [], delete: async () => {} };
const originalConsole = console;
globalThis.console = { log: () => {}, warn: () => {}, error: () => {} };
vm.runInThisContext(fs.readFileSync(SW_PATH, 'utf8'), { filename: 'sw.js' });

const appTab = () => ({
    url: 'https://archive.example/',
    focus: async function () { calls.push(['focus']); return this; },
    postMessage: message => { calls.push(['postMessage', message.type, message.data]); },
    navigate: async url => { calls.push(['navigate', url]); return {}; },
});
const click = async (data) => {
    calls.length = 0;
    const waited = [];
    await handlers.notificationclick({
        notification: { data, close: () => {} },
        waitUntil: promise => { waited.push(promise); },
    });
    await Promise.all(waited);
};
""",
            body,
        ]
    )


def test_notification_click_talks_to_an_open_tab_instead_of_reloading_it() -> None:
    """``client.url`` is absolute and ``url`` was relative, so navigate() always ran.

    A navigate() is a full document load: it throws away the loaded message window,
    the scroll position and whatever the single in-page audio element was playing.
    """
    _run_node(
        _service_worker_click_script(
            """
(async () => {
    windowClients = [appTab()];
    await click({ chat_id: -100123, message_id: 55, url: '/?chat=-100123&msg=55' });

    const kinds = calls.map(entry => entry[0]);
    assert.ok(kinds.includes('focus'), 'the open tab was not focused');
    assert.ok(kinds.includes('postMessage'), 'the in-place path is still unreachable');
    assert.equal(kinds.includes('navigate'), false, 'the click still hard-reloads the app');
    assert.equal(kinds.includes('openWindow'), false);
    const message = calls.find(entry => entry[0] === 'postMessage');
    assert.equal(message[1], 'NOTIFICATION_CLICK');
    assert.deepEqual(message[2], { chat_id: -100123, message_id: 55, url: '/?chat=-100123&msg=55' });
})().catch(error => {
    originalConsole.error(error.stack);
    process.exitCode = 1;
});
"""
        )
    )


def test_notification_click_opens_an_absolute_url_when_no_tab_is_open() -> None:
    """With no window of our own, the relative URL must be resolved against the origin."""
    _run_node(
        _service_worker_click_script(
            """
(async () => {
    windowClients = [];
    await click({ chat_id: -100123, url: '/?chat=-100123' });
    assert.deepEqual(calls, [['openWindow', 'https://archive.example/?chat=-100123']]);

    // A window belonging to some other origin is not ours to focus or message.
    windowClients = [{ url: 'https://elsewhere.example/', focus: async () => {}, postMessage: () => {
        throw new Error('messaged a cross-origin client');
    } }];
    await click({ url: '/' });
    assert.deepEqual(calls, [['openWindow', 'https://archive.example/']]);
})().catch(error => {
    originalConsole.error(error.stack);
    process.exitCode = 1;
});
"""
        )
    )


def test_notification_click_selects_tabs_on_focus_not_postmessage() -> None:
    """The click handler's tab selector must be a predicate that can actually miss.

    ``'postMessage' in client`` is true of every window client the spec can hand
    back, so the old selector matched unconditionally: a tab whose listener was not
    attached yet swallowed the deep link, and openWindow could never run.
    """
    source = _without_comments(SERVICE_WORKER.read_text(encoding="utf-8"))

    assert "'postMessage' in client" not in source, "the always-true selector is back"
    assert '"postMessage" in client' not in source, "the always-true selector is back"

    click_handler = source[source.index("'notificationclick'") :]
    assert "'focus' in client" in click_handler
    # Both misses land the click in a window: no focusable tab, and a focus() that fails.
    assert click_handler.count("clients.openWindow(targetUrl)") >= 2


def test_notification_click_opens_a_window_when_no_tab_is_focusable() -> None:
    """An own tab that cannot be focused must fall through to openWindow.

    This exact shape was the silent no-op: the old ``'postMessage' in client``
    selector matched it, posted a message no listener necessarily received, and
    never reached the fallback.
    """
    _run_node(
        _service_worker_click_script(
            """
(async () => {
    windowClients = [{
        url: 'https://archive.example/',
        postMessage: () => { calls.push(['postMessage']); },
    }];
    await click({ chat_id: -100123, message_id: 55, url: '/?chat=-100123&msg=55' });
    assert.deepEqual(calls, [['openWindow', 'https://archive.example/?chat=-100123&msg=55']],
        'an unfocusable tab still swallowed the click');
})().catch(error => {
    originalConsole.error(error.stack);
    process.exitCode = 1;
});
"""
        )
    )


def test_notification_click_focus_failure_still_lands_the_click() -> None:
    """A tab that refuses focus() must not swallow the click.

    The rejection also must not escape into event.waitUntil unhandled — the click
    promise still resolves, and the deep link lands in a fresh window instead.
    """
    _run_node(
        _service_worker_click_script(
            """
(async () => {
    windowClients = [{
        url: 'https://archive.example/',
        focus: async () => { throw new Error('focus rejected'); },
        postMessage: () => { calls.push(['postMessage']); },
    }];
    await click({ chat_id: -100123, url: '/?chat=-100123' });   // resolves, or this test throws
    assert.deepEqual(calls, [['openWindow', 'https://archive.example/?chat=-100123']],
        'a refused focus() left the click a silent no-op');
})().catch(error => {
    originalConsole.error(error.stack);
    process.exitCode = 1;
});
"""
        )
    )


def test_notification_click_listener_attaches_before_initialization_awaits() -> None:
    """The page half of the same no-op: the listener must exist when the click lands.

    It was attached at the END of onMounted, after auth + chats + stats + folders
    had all been awaited — seconds on a slow link — so the worker focused the tab,
    posted the deep link, and nobody was listening. It must be registered before
    the first await, and delivery must be started explicitly: addEventListener
    alone never drains the browser's queue of messages posted during page load.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    mounted = html[html.index("onMounted(async () => {") :]
    registration = mounted.index("navigator.serviceWorker.addEventListener('message'")
    start_messages = mounted.index("navigator.serviceWorker.startMessages?.()")
    first_await = mounted.index("await fetch('/api/auth/check'")
    assert registration < first_await, "the listener is attached only after initialization awaits"
    assert start_messages < first_await, "queued messages are never released to the listener"


def test_notification_click_during_startup_is_parked_and_replayed_once() -> None:
    """A click that arrives before the chat list exists must be replayed, not dropped."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
let pendingNotificationClick = null;
let notificationClickReady = false;
const opened = [];
const openNotificationTarget = async (chatRef, messageId) => { opened.push([chatRef, messageId]); return true; };
const console = { log: () => {} };
""",
            _extract_const_arrow_function(html, "flushPendingNotificationClick", asynchronous=True),
            _extract_const_arrow_function(html, "onServiceWorkerMessage", asynchronous=True),
            """
(async () => {
    // Before initialization is done: the click is parked, not acted on and not lost.
    // Push payloads carry the chat's opaque ref — never a chat id.
    await onServiceWorkerMessage({ data: { type: 'NOTIFICATION_CLICK', data: { chat_ref: 'parkedRef', message_id: 55 } } });
    assert.deepEqual(opened, [], 'acted on a click before the app could open a chat');

    // Non-clicks and payloads without a chat must not park anything.
    await onServiceWorkerMessage({ data: { type: 'SOMETHING_ELSE' } });
    await onServiceWorkerMessage({ data: { type: 'NOTIFICATION_CLICK', data: {} } });
    await onServiceWorkerMessage({});

    // Initialization finishes: the parked click replays exactly once.
    await flushPendingNotificationClick();
    assert.deepEqual(opened, [['parkedRef', 55]], 'the parked click was dropped');
    await flushPendingNotificationClick();
    assert.deepEqual(opened, [['parkedRef', 55]], 'the parked click replayed twice');

    // From now on clicks are handled directly.
    await onServiceWorkerMessage({ data: { type: 'NOTIFICATION_CLICK', data: { chat_ref: 'directRef' } } });
    assert.deepEqual(opened, [['parkedRef', 55], ['directRef', undefined]]);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# S40 — the floating day pill read one rect per loaded date separator, every frame
# --------------------------------------------------------------------------------------


def test_floating_day_pill_costs_log_rect_reads_not_one_per_day() -> None:
    """Per-frame scroll work must not grow with how much history is loaded.

    Nothing trims the message window, so a separator accumulates for every distinct
    day paged in. The answer is unchanged — it is checked here against a straight
    linear scan at 40 scroll positions, in both layout directions.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const FLOATING_DATE_TRIP_PX = 12;
const floatingDateLabel = ref('');
const floatingDateIso = ref(null);
let rectReads = 0;
let markers = [];
const container = {
    getBoundingClientRect: () => ({ top: 0 }),
    querySelectorAll: selector => {
        assert.equal(selector, '.date-separator');
        return markers;
    },
};
const messagesContainer = ref(container);

const DAYS = 400;
// One separator per loaded day, 800px apart. `reversed` is the shipped layout:
// flex-col-reverse puts the newest day (DOM index 0) at the bottom.
const buildMarkers = (scrollOffset, reversed) => {
    const built = [];
    for (let day = 0; day < DAYS; day++) {
        const top = day * 800 - scrollOffset;
        built.push({
            top,
            dataset: { dateLabel: `day-${day}`, dateIso: `2026-01-${day}` },
            getBoundingClientRect() { rectReads += 1; return { top: this.top }; },
        });
    }
    return reversed ? built.reverse() : built;
};

// The behaviour being preserved, written out plainly.
const linearAnswer = () => {
    let best = null;
    let bestTop = -Infinity;
    let firstBelow = null;
    let firstBelowTop = Infinity;
    for (const marker of markers) {
        const top = marker.top;
        if (top <= FLOATING_DATE_TRIP_PX) {
            if (top > bestTop) { bestTop = top; best = marker; }
        } else if (top < firstBelowTop) { firstBelowTop = top; firstBelow = marker; }
    }
    best = best || firstBelow;
    return best ? best.dataset.dateLabel : '';
};
""",
            _extract_const_arrow_function(html, "updateFloatingDate", asynchronous=False),
            """
let worstReads = 0;
for (const reversed of [true, false]) {
    for (let step = 0; step < 40; step++) {
        const scrollOffset = step * 8000;
        markers = buildMarkers(scrollOffset, reversed);
        const expected = linearAnswer();
        rectReads = 0;
        updateFloatingDate();
        assert.equal(floatingDateLabel.value, expected,
            `wrong day at offset ${scrollOffset} (reversed=${reversed})`);
        worstReads = Math.max(worstReads, rectReads);
    }
}
// A linear scan would read all 400 every frame; a binary search reads ~log2(400).
assert.equal(markers.length, 400);
assert.ok(worstReads <= 20, `read ${worstReads} rects per frame for ${markers.length} separators`);

// Boundaries: an empty list clears the pill, one separator still resolves.
floatingDateLabel.value = 'stale';
markers = [];
updateFloatingDate();
assert.equal(floatingDateLabel.value, '');
// One separator, and it sits below the trip line: the top-of-history branch.
markers = buildMarkers(0, true).slice(0, 1);
updateFloatingDate();
assert.equal(floatingDateLabel.value, 'day-399');
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# S42 — chat ids and full media paths were written to the browser console
# --------------------------------------------------------------------------------------


def test_no_identifiers_are_written_to_the_browser_console() -> None:
    """The project's logging rule covers the client too.

    ``getMediaUrl`` is ``/media/<chat id>/<file id>_<the sender's own filename>``, and
    the console buffer is what a screen share, a devtools screenshot and a browser
    bug report carry off the device.
    """
    forbidden = ("chat_id", "chatId", "message_id", "messageId", "msgId", "topic_id", "topicId", "getMediaUrl")

    for source_file in (INDEX_HTML, SERVICE_WORKER):
        source = source_file.read_text(encoding="utf-8")
        for line in _console_call_lines(source):
            for identifier in forbidden:
                assert identifier not in line, f"{source_file.name} logs {identifier}: {line.strip()}"

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "console.error('Failed to load image:', getMediaUrl(msg))" not in html
    assert "console.error('Failed to load media:', getMediaUrl(msg))" not in html
    assert "console.log('[WS] Subscribed to chat:', data.chat_id)" not in html
    assert "console.warn('[WS] Subscription denied for chat:', data.chat_id)" not in html


def test_console_never_carries_a_whole_message_payload() -> None:
    """``console.log('...', event.data)`` prints the entire payload object.

    For a notification message that is the chat id, the message id and whatever
    else the sender attached — the identifier ban is defeated in one line if the
    object itself is logged instead of a field.
    """
    for source_file in (INDEX_HTML, SERVICE_WORKER):
        source = source_file.read_text(encoding="utf-8")
        for line in _console_call_lines(source):
            assert "event.data" not in line, f"{source_file.name} logs a message payload: {line.strip()}"


def test_media_error_handlers_log_the_event_without_the_media_url() -> None:
    """Run both handlers and read what they actually wrote."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const logged = [];
const console = { error: (...args) => { logged.push(args.join(' ')); } };
const getMediaUrl = () => { throw new Error('the handler built the media URL'); };
const msg = { id: 5, chat_id: -1001234567890, media: { type: 'video' } };
const img = { onerror: () => {}, style: {}, onclick: () => {} };
const video = { parentElement: { set innerHTML(value) { throw new Error('wrote innerHTML'); } } };
""",
            _extract_const_arrow_function(html, "handleImageError", asynchronous=False),
            _extract_const_arrow_function(html, "handleMediaError", asynchronous=False),
            """
handleImageError({ target: img }, msg);
handleMediaError({ target: video }, msg);
assert.equal(logged.length, 2);
for (const line of logged) {
    assert.equal(/\\d/.test(line), false, `console line carries an identifier: ${line}`);
    assert.equal(line.includes('/media/'), false, line);
}
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# S43 — handleMediaError overwrote Vue-owned DOM with innerHTML
# --------------------------------------------------------------------------------------


def test_media_error_placeholder_is_rendered_by_vue_not_written_into_the_dom() -> None:
    """Assigning innerHTML to the parent destroyed nodes Vue still held el pointers to.

    The bubble then stayed detached for the life of the row: Vue kept patching nodes
    that were no longer in the document, so nothing about that message updated again.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    handler = _extract_const_arrow_function(html, "handleMediaError", asynchronous=False)
    assert "innerHTML" not in _without_comments(handler)
    assert "msg.mediaLoadFailed = true" in handler

    # ...and the placeholder is a real branch of the template.
    video_block = html[html.index("<!-- Videos - click to open in lightbox -->") :]
    video_block = video_block[: video_block.index("<!-- Stickers")]
    assert 'v-if="msg.mediaLoadFailed"' in video_block
    assert "<template v-else>" in video_block
    assert video_block.index('v-if="msg.mediaLoadFailed"') < video_block.index("<video")

    # The handler must flag the row and touch nothing else (the stub throws on a write).
    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const console = { error: () => {} };
const msg = { id: 9, chat_id: -100, media: { type: 'video' } };
const parent = { set innerHTML(value) { throw new Error('handleMediaError wrote innerHTML'); } };
""",
            _extract_const_arrow_function(html, "handleMediaError", asynchronous=False),
            """
handleMediaError({ target: { parentElement: parent } }, msg);
assert.equal(msg.mediaLoadFailed, true, 'the row was not flagged, so Vue cannot render the placeholder');
""",
        ]
    )

    _run_node(script)


# --------------------------------------------------------------------------------------
# S22 / S30 — CDN assets had no Subresource Integrity and two were unpinned
# --------------------------------------------------------------------------------------

# Fetched from each CDN and hashed; a wrong value here silently breaks the whole page,
# so these are the bytes actually served for these exact URLs.
EXPECTED_SUBRESOURCES = {
    "https://unpkg.com/vue@3.5.41/dist/vue.global.prod.js": (
        "sha384-arPHRzOKPl8g3Rbe/cQBWYPnq4HcxfPFSFWD3qvI/hc2XQf+4GkVqkOlWgjN5mD3"
    ),
    "https://cdnjs.cloudflare.com/ajax/libs/moment.js/2.29.4/moment.min.js": (
        "sha384-VCGDSwGwLWkVOK5vAWSaY38KZ4oKJ0whHjpJQhjqrMlWadpf2dUVKLgOLBdEaLvZ"
    ),
    "https://cdnjs.cloudflare.com/ajax/libs/moment-timezone/0.5.43/moment-timezone-with-data-1970-2030.min.js": (
        "sha384-l7VdBcaAfGCeXKsn587Z+4Z3m6M5/96OPpQu1zC3wscMtXK9xPY8oQQYUhZBJIC/"
    ),
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css": (
        "sha384-iw3OoTErCYJJB9mCa8LNS2hbsQ7M3C0EpIsO/H5+EGAkPGc6rk+V8i04oW/K5xq0"
    ),
    "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css": (
        "sha384-RkASv+6KfBMW9eknReJIJ6b3UnjKOKC5bOUaNgIY778NFbQ8MtWq9Lr/khUgqtTt"
    ),
    "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js": (
        "sha384-5JqMv4L/Xa0hfvtF06qboNdhvuYXUku9ZrhZh3bSk8VXF0A/RuSLHpLsSV9Zqhl6"
    ),
}

# The two that cannot carry a hash, and why. Tailwind's CDN serves no
# Access-Control-Allow-Origin, and integrity= forces a CORS fetch — adding it would
# block the script and the UI would load unstyled. The Google Fonts stylesheet is
# generated per user agent, so its bytes differ between browsers.
UNHASHABLE_SUBRESOURCES = {
    "https://cdn.tailwindcss.com/3.4.17",
    "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap",
}

_SUBRESOURCE_TAG = re.compile(r"<(?P<tag>script|link)\b(?P<attrs>[^>]*?)>", re.IGNORECASE | re.DOTALL)
_URL_ATTR = re.compile(r'\b(?:src|href)="(?P<url>https://[^"]+)"')


def _external_subresources(html: str) -> dict[str, str]:
    """Map every externally loaded script/stylesheet URL to its tag's attributes."""
    head = html[: html.index("</head>")]
    found = {}
    for match in _SUBRESOURCE_TAG.finditer(head):
        attrs = match.group("attrs")
        url_match = _URL_ATTR.search(attrs)
        if url_match is None:
            continue
        found[url_match.group("url")] = attrs
    return found


_STALE_PANEL_FETCH_STUB = """
const requests = [];
const fetch = url => new Promise((resolve, reject) => { requests.push({ url, resolve, reject }); });
const respond = (index, payload) => requests[index].resolve({ ok: true, json: async () => payload });
const respondError = (index, status) => requests[index].resolve({ ok: false, status, json: async () => ({}) });
const fail = index => requests[index].reject(new TypeError('network down'));
const flush = () => new Promise(resolve => setImmediate(resolve));
"""


def test_chat_stats_response_that_outlived_its_chat_is_discarded() -> None:
    """Stats requested for one chat must never label another chat's header.

    ``chatStats`` is written once per selection and never refreshed, so a stale
    paint was persistent — and the catch branch writes too, so a stale *failure*
    blanked the current chat's header instead.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
// Loaders address and guard by the chat's opaque ref, not its id.
const selectedChat = ref({ ref: 'ref-111' });
const chatStats = ref(null);
const console = { error: () => {} };
""",
            _STALE_PANEL_FETCH_STUB,
            _extract_const_arrow_function(html, "loadChatStats", asynchronous=True),
            """
(async () => {
    loadChatStats('ref-111');
    await flush();
    assert.ok(requests[0].url.startsWith('/api/chats/ref-111/stats'), requests[0].url);

    // The user switches chats while chat 111's stats are still in flight
    // (selectChat resets the panel and issues the new chat's request).
    selectedChat.value = { ref: 'ref-222' };
    chatStats.value = null;
    loadChatStats('ref-222');
    await flush();

    respond(1, { message_count: 5 });
    await flush();
    assert.deepEqual(chatStats.value, { message_count: 5 });

    // The superseded response lands last and must be discarded.
    respond(0, { message_count: 999 });
    await flush();
    assert.deepEqual(chatStats.value, { message_count: 5 },
        "chat 111's stats landed in chat 222's header");

    // A stale network error must not blank the header either.
    loadChatStats('ref-222');
    await flush();
    selectedChat.value = { ref: 'ref-333' };
    chatStats.value = null;
    loadChatStats('ref-333');
    await flush();
    respond(3, { message_count: 7 });
    await flush();
    fail(2);
    await flush();
    assert.deepEqual(chatStats.value, { message_count: 7 },
        "chat 222's network error blanked chat 333's header");

    // The CURRENT chat's failure still clears: the guard must not pin stale numbers.
    loadChatStats('ref-333');
    await flush();
    fail(4);
    await flush();
    assert.equal(chatStats.value, null, 'a live network error no longer clears the header');

    // A stale HTTP failure must not blank the header either.
    loadChatStats('ref-333');
    await flush();
    selectedChat.value = { ref: 'ref-444' };
    chatStats.value = null;
    loadChatStats('ref-444');
    await flush();
    respond(6, { message_count: 9 });
    await flush();
    respondError(5, 500);
    await flush();
    assert.deepEqual(chatStats.value, { message_count: 9 },
        "chat 333's HTTP failure blanked chat 444's header");

    // A CURRENT chat's non-ok response leaves the header as it was: unlike the
    // pinned banner, loadChatStats has no else-branch write — only a network
    // error clears the header.
    loadChatStats('ref-444');
    await flush();
    respondError(7, 500);
    await flush();
    assert.deepEqual(chatStats.value, { message_count: 9 },
        'a live non-ok response started clearing the header');
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_pinned_messages_response_that_outlived_its_chat_is_discarded() -> None:
    """A pinned banner requested for one chat must never hang in another.

    All three branches write ``pinnedMessages``: a stale success painted the old
    chat's pin into the new chat, and a stale else/catch wiped the new chat's
    banner that had just loaded. The success write also resets the banner cycle,
    so a discarded response must leave ``currentPinnedIndex`` alone too.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
// Loaders address and guard by the chat's opaque ref, not its id.
const selectedChat = ref({ ref: 'ref-111' });
const pinnedMessages = ref([]);
const currentPinnedIndex = ref(0);
const console = { error: () => {} };
""",
            _STALE_PANEL_FETCH_STUB,
            _extract_const_arrow_function(html, "loadPinnedMessages", asynchronous=True),
            """
(async () => {
    loadPinnedMessages('ref-111');
    await flush();
    assert.ok(requests[0].url.startsWith('/api/chats/ref-111/pinned'), requests[0].url);

    // Chat switch while 111's request is in flight (selectChat's resets).
    selectedChat.value = { ref: 'ref-222' };
    pinnedMessages.value = [];
    currentPinnedIndex.value = 0;
    loadPinnedMessages('ref-222');
    await flush();

    respond(1, [{ id: 'pin-222-newest' }, { id: 'pin-222-older' }]);
    await flush();
    assert.deepEqual(pinnedMessages.value.map(pin => pin.id), ['pin-222-newest', 'pin-222-older']);
    currentPinnedIndex.value = 1;   // the user cycled the banner

    // The superseded 111 response lands last and must be discarded entirely.
    respond(0, [{ id: 'pin-111' }]);
    await flush();
    assert.deepEqual(pinnedMessages.value.map(pin => pin.id), ['pin-222-newest', 'pin-222-older'],
        "chat 111's pinned messages landed in chat 222");
    assert.equal(currentPinnedIndex.value, 1, 'a stale response reset the banner cycle');

    // A stale HTTP failure must not wipe the current chat's banner.
    loadPinnedMessages('ref-222');
    await flush();
    selectedChat.value = { ref: 'ref-333' };
    pinnedMessages.value = [];
    currentPinnedIndex.value = 0;
    loadPinnedMessages('ref-333');
    await flush();
    respond(3, [{ id: 'pin-333' }]);
    await flush();
    respondError(2, 500);
    await flush();
    assert.deepEqual(pinnedMessages.value.map(pin => pin.id), ['pin-333'],
        "chat 222's failed request wiped chat 333's banner");

    // A stale network error must not wipe it either.
    loadPinnedMessages('ref-333');
    await flush();
    selectedChat.value = { ref: 'ref-444' };
    pinnedMessages.value = [];
    loadPinnedMessages('ref-444');
    await flush();
    respond(5, [{ id: 'pin-444' }]);
    await flush();
    fail(4);
    await flush();
    assert.deepEqual(pinnedMessages.value.map(pin => pin.id), ['pin-444'],
        "chat 333's network error wiped chat 444's banner");

    // The CURRENT chat's failures still clear: the guard must not pin a stale banner.
    loadPinnedMessages('ref-444');
    await flush();
    respondError(6, 500);
    await flush();
    assert.deepEqual(pinnedMessages.value, [], 'a live failed reload no longer clears the banner');

    pinnedMessages.value = [{ id: 'left-behind' }];
    loadPinnedMessages('ref-444');
    await flush();
    fail(7);
    await flush();
    assert.deepEqual(pinnedMessages.value, [], 'a live network error no longer clears the banner');
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_interpolated_fetch_paths_encode_their_segments() -> None:
    """A ref-shaped value can never smuggle a path separator into a fetch URL.

    Every chat-scoped URL builder wraps its interpolated segments in
    encodeURIComponent, so even a hostile deep-link value like ``../admin``
    reaches the server as one opaque path segment (``..%2Fadmin``) instead of
    stepping the request onto another route (CodeQL js/request-forgery).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const selectedChat = ref({ id: 111, ref: '../admin' });
const messageVersionsByMessage = ref({});
const messageVersionsErrors = ref({});
const messageVersionsRequestSeq = ref({});
const messageVersionsLoading = ref({});
const isAuthenticated = ref(true);
const console = { error: () => {} };
const urls = [];
const fetch = async (url) => { urls.push(url); return { ok: false, status: 500, json: async () => ({}) }; };
const messageVersionsKey = msg => String(msg.id);
const setMessageVersionsRecord = (store, key, value) => { store.value = { ...store.value, [key]: value }; };
const clearMessageVersionsRecord = (store, key) => { const next = { ...store.value }; delete next[key]; store.value = next; };
""",
            _extract_const_arrow_function(html, "loadMessageVersions", asynchronous=True),
            """
(async () => {
    await loadMessageVersions({ id: 7 });
    assert.equal(urls.length, 1, 'expected exactly one request');
    assert.ok(urls[0].includes('..%2Fadmin'), `separator not encoded: ${urls[0]}`);
    assert.ok(!/\\/\\.\\.\\//.test(urls[0]), `raw dot-dot segment survived: ${urls[0]}`);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_every_chat_scoped_url_interpolation_is_encoded() -> None:
    """The encoding invariant is enforced by scan, not sampled by one builder.

    Any ``/api/chats/${...}`` interpolation whose first expression is not
    ``encodeURIComponent(`` can smuggle a path separator; a new builder added
    without the wrapper fails here by construction.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    violations = [
        html[max(0, m.start() - 40) : m.start() + 60]
        for m in re.finditer(r"/api/chats/\$\{(?!encodeURIComponent\()", html)
    ]
    assert violations == [], f"unencoded chat-scoped interpolations: {violations}"


# --------------------------------------------------------------------------------------
# Logging out left this browser's push channel armed and re-arming
# --------------------------------------------------------------------------------------


def test_logout_drops_this_browsers_push_channel_before_the_session_dies() -> None:
    """Logout must revoke the browser's half of the push channel, in that order.

    ``unsubscribeFromPush`` POSTs to ``/api/push/unsubscribe``, which is
    session-authenticated -- so it has to run BEFORE the logout request kills
    the cookie, or it silently 401s. Clearing ``push_enabled`` is the other
    half: the service-worker bootstrap re-subscribes on the next load whenever
    that key is still ``'true'`` and permission is granted, so leaving it
    behind re-arms the channel for whoever logs in next on this browser.

    The server purge in ``/api/logout`` is the guarantee; this is hygiene.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const isAuthenticated = ref(true);
const userRole = ref('viewer');
const currentUsername = ref('someone');
const showAdminPanel = ref(true);
const calls = [];
const store = { push_enabled: 'true' };
const localStorage = {
    getItem: key => (key in store ? store[key] : null),
    setItem: (key, value) => { store[key] = String(value); },
    removeItem: key => { delete store[key]; },
};
const unsubscribeFromPush = async () => { calls.push('unsubscribeFromPush'); };
const fetch = async (url) => { calls.push(`fetch:${url}`); return { ok: true }; };
""",
            _extract_const_arrow_function(html, "performLogout", asynchronous=True),
            """
(async () => {
    await performLogout();

    const unsubscribedAt = calls.indexOf('unsubscribeFromPush');
    const loggedOutAt = calls.indexOf('fetch:/api/logout');
    assert.ok(unsubscribedAt >= 0, 'logout never unsubscribed this browser from push');
    assert.ok(loggedOutAt >= 0, 'logout no longer posts /api/logout');
    assert.ok(unsubscribedAt < loggedOutAt,
        'the push unsubscribe ran after the cookie was gone, so it could only 401');
    assert.equal(store.push_enabled, undefined,
        'push_enabled survived logout, so the next load re-subscribes this browser');
    assert.equal(isAuthenticated.value, false, 'logout no longer clears the session state');
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)
