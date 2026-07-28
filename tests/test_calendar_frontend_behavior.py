"""Executable regressions for calendar and detached-window frontend behavior."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


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
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"Node behavior test failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_load_newer_messages_pages_to_topic_tail_without_duplicates() -> None:
    """Two real newer-page loads should advance the cursor and resume live refresh only at the tail."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    load_newer_messages = _extract_const_arrow_function(html, "loadNewerMessages", asynchronous=True)

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const loadingNewer = ref(false);
const newerLoadError = ref('');
const hasMoreNewer = ref(true);
const selectedChat = ref({ id: 77 });
const isAuthenticated = ref(true);
const viewingPinnedWindow = ref(true);
const loadNewerSentinel = ref({});
let newestMessageId = 100;
let newerLoadRequestSeq = 0;
let chatVersion = 4;
let refreshStarts = 0;
let observerCalls = 0;
const activeTopicId = () => 9;
const nextTick = async () => {};
const startMessageRefresh = () => { refreshStarts += 1; };
const messagesNewerObserver = { observe: () => { observerCalls += 1; } };
const requestedUrls = [];
const pages = [
    Array.from({ length: 50 }, (_, index) => ({ id: 101 + index })),
    [{ id: 151 }, { id: 152 }],
];
const fetch = async (url, options) => {
    requestedUrls.push(url);
    assert.deepEqual(options, { credentials: 'include' });
    return { ok: true, json: async () => ({ messages: pages.shift() }) };
};
const capturedIds = [];
const seenIds = new Set();
const upsertMessages = rows => {
    for (const row of rows) {
        assert.equal(seenIds.has(row.id), false, `duplicate upsert for message ${row.id}`);
        seenIds.add(row.id);
        capturedIds.push(row.id);
    }
};
const console = { error: () => {} };
""",
            load_newer_messages,
            """
(async () => {
    await loadNewerMessages();

    assert.equal(newestMessageId, 150);
    assert.equal(hasMoreNewer.value, true);
    assert.equal(viewingPinnedWindow.value, true);
    assert.equal(refreshStarts, 0);

    await loadNewerMessages();

    assert.deepEqual(requestedUrls, [
        '/api/chats/77/messages?after_id=100&limit=50&topic_id=9',
        '/api/chats/77/messages?after_id=150&limit=50&topic_id=9',
    ]);
    assert.equal(newestMessageId, 152);
    assert.equal(hasMoreNewer.value, false);
    assert.equal(viewingPinnedWindow.value, false);
    assert.equal(refreshStarts, 1);
    assert.equal(capturedIds.length, 52);
    assert.equal(seenIds.size, 52);
    assert.equal(observerCalls, 2);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_newer_failure_requires_manual_retry_and_preserves_forward_paging() -> None:
    """A failed real newer load should pause observation until the real retry handler clears the error."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    load_newer_messages = _extract_const_arrow_function(html, "loadNewerMessages", asynchronous=True)
    retry_newer_messages = _extract_const_arrow_function(html, "retryNewerMessages", asynchronous=False)

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const loadingNewer = ref(false);
const newerLoadError = ref('');
const hasMoreNewer = ref(true);
const selectedChat = ref({ id: 88 });
const isAuthenticated = ref(true);
const viewingPinnedWindow = ref(true);
const loadNewerSentinel = ref({});
let newestMessageId = 200;
let newerLoadRequestSeq = 0;
let chatVersion = 5;
let fetchCalls = 0;
let observerCalls = 0;
let upsertCalls = 0;
const activeTopicId = () => 12;
const nextTick = async () => {};
const startMessageRefresh = () => {};
const messagesNewerObserver = { observe: () => { observerCalls += 1; } };
const fetch = async url => {
    fetchCalls += 1;
    assert.match(url, /topic_id=12$/);
    if (fetchCalls === 1) {
        throw new Error('simulated network failure');
    }
    return { ok: true, json: async () => ({ messages: [{ id: 201 }] }) };
};
const upsertMessages = () => { upsertCalls += 1; };
const console = { error: () => {} };
""",
            load_newer_messages,
            retry_newer_messages,
            """
(async () => {
    await loadNewerMessages();

    assert.equal(hasMoreNewer.value, true);
    assert.equal(newerLoadError.value, 'Could not load newer messages.');
    assert.equal(observerCalls, 0);
    assert.equal(fetchCalls, 1);

    // A stale observer callback cannot loop while the visible error gate is active.
    await loadNewerMessages();
    assert.equal(fetchCalls, 1);

    retryNewerMessages();
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(fetchCalls, 2);
    assert.equal(upsertCalls, 1);
    assert.equal(newerLoadError.value, '');
    assert.equal(hasMoreNewer.value, false);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)


def test_jump_to_date_cancellation_and_latest_intent_win() -> None:
    """Real date jumps should ignore closed and superseded deferred responses."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    close_date_picker = _extract_const_arrow_function(html, "closeDatePicker", asynchronous=False)
    jump_to_date = _extract_const_arrow_function(html, "jumpToDate", asynchronous=True)

    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            """
const ref = value => ({ value });
const selectedChat = ref({ id: 99 });
const selectedDate = ref('2026-01-10');
const calendarAvailableDates = ref(new Set(['2026-01-10', '2026-01-11', '2026-01-12']));
const viewerTimezone = ref('Europe/Madrid');
const sortedMessages = ref([]);
const showDatePickerModal = ref(true);
let dateJumpRequestSeq = 0;
let chatVersion = 8;
let flatpickrInstance = null;
let datePickerTrigger = null;
const activeTopicId = () => 44;
const handleDatePickerKeydown = () => {};
const document = { removeEventListener: () => {} };
const resetCalendarAvailability = () => {};
const nextTick = async callback => { if (callback) callback(); };
const moment = {
    utc: value => ({
        tz: () => ({ format: () => value.slice(0, 10) }),
    }),
};
const deferredFetches = [];
const fetch = (url, options) => {
    let resolve;
    const promise = new Promise(resolver => { resolve = resolver; });
    deferredFetches.push({ url, options, resolve });
    return promise;
};
const loaderCalls = [];
const loadMessagesAroundId = async (messageId, externalGuard) => {
    assert.equal(typeof externalGuard, 'function');
    assert.equal(externalGuard(), true);
    loaderCalls.push({ messageId, externalGuard });
};
const scrollCalls = [];
const scrollToMessage = messageId => { scrollCalls.push(messageId); };
const toastCalls = [];
const showToast = message => { toastCalls.push(message); };
const console = { error: () => {} };
""",
            close_date_picker,
            jump_to_date,
            """
const response = (id, date) => ({
    ok: true,
    json: async () => ({ id, date: `${date}T10:00:00Z` }),
});

(async () => {
    const cancelledJump = jumpToDate();
    assert.equal(deferredFetches.length, 1);
    closeDatePicker();
    deferredFetches[0].resolve(response(300, '2026-01-10'));
    await cancelledJump;

    assert.equal(loaderCalls.length, 0);
    assert.equal(scrollCalls.length, 0);

    selectedDate.value = '2026-01-11';
    const olderJump = jumpToDate();
    selectedDate.value = '2026-01-12';
    const latestJump = jumpToDate();
    assert.equal(deferredFetches.length, 3);

    deferredFetches[2].resolve(response(302, '2026-01-12'));
    await latestJump;
    deferredFetches[1].resolve(response(301, '2026-01-11'));
    await olderJump;

    assert.deepEqual(
        deferredFetches.map(request => request.url),
        [
            '/api/chats/99/messages/by-date?date=2026-01-10&timezone=Europe%2FMadrid&topic_id=44',
            '/api/chats/99/messages/by-date?date=2026-01-11&timezone=Europe%2FMadrid&topic_id=44',
            '/api/chats/99/messages/by-date?date=2026-01-12&timezone=Europe%2FMadrid&topic_id=44',
        ],
    );
    for (const request of deferredFetches) {
        assert.deepEqual(request.options, { credentials: 'include' });
    }
    assert.deepEqual(loaderCalls.map(call => call.messageId), [302]);
    assert.equal(typeof loaderCalls[0].externalGuard, 'function');
    assert.equal(loaderCalls[0].externalGuard(), true);
    assert.equal(scrollCalls.length, 0);
    assert.equal(toastCalls.length, 0);
})().catch(error => {
    process.stderr.write(`${error.stack}\\n`);
    process.exitCode = 1;
});
""",
        ]
    )

    _run_node(script)
