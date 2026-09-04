"""The sidebar search, executed: one field driving the chat and message sections.

The real setup-scope functions are lifted out of the template and run under
node with stubbed fetch/DOM, so these pin behaviour (debounce, stale answers,
Enter, clearing, the deep-link entry) rather than source text.
"""

from test_frontend_audit_fixes import INDEX_HTML, _extract_const_arrow_function, _run_node

PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const computed = getter => ({ get value() { return getter(); } });
const nextTick = async () => {};
const console = { error: () => {}, log: () => {}, warn: () => {} };
const searchQuery = ref('');
const searchActive = computed(() => searchQuery.value.trim().length > 0);
const searchActiveIndex = ref(-1);
const searchInput = ref({ focus() { focused.push('focus'); }, blur() { focused.push('blur'); } });
const focused = [];
const messageSearch = ref({ results: [], loading: false, hasMore: false, error: '', truncated: false, indexed: true });
const MESSAGE_SEARCH_MAX_OFFSET = 5000;
const messageSearchSentinel = ref(null);
let searchGeneration = 0;
let messageSearchObserver = null;
const searchResults = ref([]);
const searchLoading = ref(false);
let searchDebounceTimer = null;
const isAuthenticated = ref(true);
const filteredChats = computed(() => searchQuery.value.trim() ? searchResults.value : []);
const parseTelegramLink = () => null;
const openTelegramLink = async () => {};
const opened = [];
const messageHighlight = ref(null);
const selectedChat = ref(null);
let openOutcome = true;
const selectChat = async chat => { opened.push(['chat', chat.ref]); };
const openNotificationTarget = async (chatRef, messageId) => { opened.push(['message', chatRef, messageId]); await new Promise(r => setTimeout(r, 5)); selectedChat.value = { ref: chatRef }; return openOutcome; };
const requests = [];
// Each request resolves when the test releases it, so answer order is under test control.
const pending = new Map();
const fetch = async url => {
    requests.push(url);
    return await new Promise(resolve => { pending.set(url, resolve); });
};
const answer = (url, body) => {
    const resolve = pending.get(url);
    assert.ok(resolve, `no pending request for ${url}`);
    pending.delete(url);
    resolve({ ok: true, status: 200, json: async () => body });
};
const fail = (url, status) => {
    const resolve = pending.get(url);
    pending.delete(url);
    resolve({ ok: false, status, json: async () => ({}) });
};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const chatUrl = q => `/api/chats?search=${encodeURIComponent(q)}&limit=1000`;
const msgUrl = (q, offset = 0) => `/api/search/messages?q=${encodeURIComponent(q)}&limit=20&offset=${offset}`;
"""

FUNCTIONS = (
    "onSearchInput",
    "dispatchSearch",
    "runChatSearch",
    "runMessageSearch",
    "observeMessageSearchSentinel",
    "resetSearchResults",
    "clearSearch",
    "onSearchEscape",
    "onSearchEnter",
    "moveSearchSelection",
    "openSearchSelection",
    "openMessageSearchResult",
    "dropHitHighlight",
)


def _script(body: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    lifted = [
        _extract_const_arrow_function(
            html,
            name,
            asynchronous=name
            not in (
                "onSearchInput",
                "observeMessageSearchSentinel",
                "resetSearchResults",
                "dispatchSearch",
                "clearSearch",
                "onSearchEscape",
                "onSearchEnter",
                "moveSearchSelection",
                "openSearchSelection",
                "dropHitHighlight",
            ),
        )
        for name in FUNCTIONS
    ]
    return "\n".join([PRELUDE, *lifted, body])


def test_one_keystroke_searches_chats_and_messages_after_the_debounce() -> None:
    _run_node(
        _script("""
(async () => {
    searchQuery.value = 'hel';
    onSearchInput();
    assert.deepEqual(requests, [], 'nothing fires before the debounce');
    assert.equal(searchLoading.value, true);
    await sleep(350);
    assert.deepEqual(requests, [chatUrl('hel'), msgUrl('hel')]);
    assert.equal(messageSearch.value.loading, true);

    answer(chatUrl('hel'), { chats: [{ ref: 'c1', title: 'Helen' }] });
    answer(msgUrl('hel'), { results: [{ id: 5, chat: { ref: 'c2' }, text: 'hello' }], has_more: true });
    await settle();
    assert.deepEqual(searchResults.value.map(c => c.ref), ['c1']);
    assert.deepEqual(messageSearch.value.results.map(r => r.id), [5]);
    assert.equal(messageSearch.value.hasMore, true);
    assert.equal(messageSearch.value.loading, false);
    assert.equal(messageSearch.value.error, '');

    // Loading more appends at the current offset; a second call while loading is a no-op.
    runMessageSearch(true);
    runMessageSearch(true);
    await settle();
    assert.deepEqual(requests.slice(2), [msgUrl('hel', 1)]);
    answer(msgUrl('hel', 1), { results: [{ id: 4, chat: { ref: 'c2' }, text: 'help' }], has_more: false });
    await settle();
    assert.deepEqual(messageSearch.value.results.map(r => r.id), [5, 4]);
    assert.equal(messageSearch.value.hasMore, false);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_a_slow_answer_for_an_older_query_never_overwrites_the_newer_one() -> None:
    _run_node(
        _script("""
(async () => {
    searchQuery.value = 'first';
    onSearchInput();
    await sleep(350);
    searchQuery.value = 'second';
    onSearchInput();
    await sleep(350);
    assert.deepEqual(requests, [chatUrl('first'), msgUrl('first'), chatUrl('second'), msgUrl('second')]);

    // The newer query answers first, then the stale one arrives late.
    answer(msgUrl('second'), { results: [{ id: 2, chat: { ref: 'c' }, text: 'second' }], has_more: false });
    await settle();
    answer(msgUrl('first'), { results: [{ id: 1, chat: { ref: 'c' }, text: 'first' }], has_more: true });
    await settle();
    assert.deepEqual(messageSearch.value.results.map(r => r.id), [2], 'the stale answer was discarded');
    assert.equal(messageSearch.value.hasMore, false);
    assert.equal(messageSearch.value.loading, false);

    // Clearing bumps the generation: an answer that lands afterwards changes nothing.
    searchQuery.value = 'third';
    onSearchInput();
    await sleep(350);
    clearSearch();
    assert.equal(searchQuery.value, '');
    assert.deepEqual(messageSearch.value.results, []);
    assert.deepEqual(focused, ['focus'], 'the field keeps focus after clearing');
    answer(msgUrl('third'), { results: [{ id: 3, chat: { ref: 'c' }, text: 'third' }], has_more: false });
    await settle();
    assert.deepEqual(messageSearch.value.results, []);
    assert.equal(messageSearch.value.loading, false);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_enter_flushes_the_debounce_once_and_opens_the_highlighted_row() -> None:
    _run_node(
        _script("""
(async () => {
    searchQuery.value = 'now';
    onSearchInput();
    onSearchEnter();
    assert.deepEqual(requests, [chatUrl('now'), msgUrl('now')], 'Enter searched immediately');
    await sleep(350);
    assert.equal(requests.length, 2, 'the cancelled debounce did not fire a second pair');

    answer(chatUrl('now'), { chats: [{ ref: 'chatA', title: 'A' }] });
    answer(msgUrl('now'), { results: [{ id: 9, chat: { ref: 'chatB' }, text: 'now' }], has_more: false });
    await settle();

    // Down twice walks past the one chat hit onto the message hit; Enter opens it.
    const document = { getElementById: () => null };
    globalThis.document = document;
    moveSearchSelection(1);
    moveSearchSelection(1);
    assert.equal(searchActiveIndex.value, 1);
    onSearchEnter();
    await settle();
    assert.deepEqual(opened, [['message', 'chatB', 9]]);
    moveSearchSelection(-1);
    onSearchEnter();
    await settle();
    assert.deepEqual(opened[1], ['chat', 'chatA']);

    // Escape: first press clears, second leaves the field.
    onSearchEscape();
    assert.equal(searchQuery.value, '');
    onSearchEscape();
    assert.deepEqual(focused.slice(-2), ['focus', 'blur']);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_failures_are_shown_and_an_expired_session_returns_to_login() -> None:
    _run_node(
        _script("""
(async () => {
    searchQuery.value = 'boom';
    onSearchInput();
    await sleep(350);
    answer(chatUrl('boom'), { chats: [] });
    fail(msgUrl('boom'), 500);
    await settle();
    assert.equal(messageSearch.value.error, 'Search failed. Try again.');
    assert.equal(messageSearch.value.loading, false);

    searchQuery.value = 'expired';
    onSearchInput();
    await sleep(350);
    answer(chatUrl('expired'), { chats: [] });
    fail(msgUrl('expired'), 401);
    await settle();
    assert.equal(isAuthenticated.value, false);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_snippet_windows_on_the_match_and_escapes_the_message() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            "const ref = value => ({ value });",
            "const searchQuery = ref('');",
            "const escapeHtml = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');",
            "const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');",
            "const SEARCH_WORD_RE = /[\\p{L}\\p{N}_]+/gu;",
            _extract_const_arrow_function(html, "foldForMatch", asynchronous=False),
            _extract_const_arrow_function(html, "searchMatchPattern", asynchronous=False),
            _extract_const_arrow_function(html, "searchMatchRanges", asynchronous=False),
            _extract_const_arrow_function(html, "searchSnippetHtml", asynchronous=False),
            """
const MARK = '<mark class="bg-tg-accent/30 text-tg-ink rounded-sm px-0.5">';
searchQuery.value = 'zana';
// Prefix semantics: the whole matched word is emphasised, case-insensitively.
assert.equal(searchSnippetHtml('Compra Zanahorias hoy'), `Compra ${MARK}Zanahorias</mark> hoy`);
// Markup in the message is text, and a query cannot split an escaped entity.
searchQuery.value = 'amp';
assert.equal(searchSnippetHtml('Tom & Jerry <b>amplify</b>'), `Tom &amp; Jerry &lt;b&gt;${MARK}amplify</mark>&lt;/b&gt;`);
// A late match is windowed with a leading ellipsis, and the term is inside the window.
searchQuery.value = 'needle';
const long = 'x'.repeat(300) + ' needle here ' + 'y'.repeat(300);
const snippet = searchSnippetHtml(long);
assert.ok(snippet.startsWith('…'), snippet.slice(0, 20));
assert.ok(snippet.includes(`${MARK}needle</mark>`), 'the match is in the window');
assert.ok(snippet.endsWith('…'));
assert.ok(snippet.length < 260, `bounded window, got ${snippet.length}`);
// No words in the query: plain escaped text, bounded.
searchQuery.value = '+++';
assert.equal(searchSnippetHtml('a <b> plus'), 'a &lt;b&gt; plus');
""",
        ]
    )
    _run_node(script)


def test_paging_stops_at_the_api_offset_ceiling_and_says_so() -> None:
    """The API caps offset at 5000; the client stops asking at 5,000 rows instead of failing on a 422."""
    _run_node(
        _script("""
(async () => {
    const page = (q, offset) => Array.from({ length: 20 }, (_, i) => ({ id: offset + i + 1, chat: { ref: 'c' }, text: q }));
    searchQuery.value = 'common';
    onSearchInput();
    await sleep(350);
    answer(chatUrl('common'), { chats: [] });
    answer(msgUrl('common'), { results: page('common', 0), has_more: true });
    await settle();
    // Page like the API does: twenty rows a time, up to the ceiling.
    for (let offset = 20; offset < 5000; offset += 20) {
        runMessageSearch(true);
        await settle();
        assert.equal(requests[requests.length - 1], msgUrl('common', offset));
        answer(msgUrl('common', offset), { results: page('common', offset), has_more: true });
        await settle();
    }
    assert.equal(messageSearch.value.results.length, 5000);
    assert.equal(messageSearch.value.hasMore, true);
    assert.equal(messageSearch.value.truncated, false);

    // At 5,000 rows the next append stops without a request, flagged as truncated.
    const before = requests.length;
    runMessageSearch(true);
    await settle();
    assert.equal(requests.length, before, 'no request at the ceiling');
    assert.equal(messageSearch.value.results.length, 5000);
    assert.equal(messageSearch.value.hasMore, false);
    assert.equal(messageSearch.value.truncated, true);
    assert.equal(messageSearch.value.error, '');

    // A fresh query clears the flag.
    searchQuery.value = 'rare';
    onSearchInput();
    await sleep(350);
    answer(chatUrl('rare'), { chats: [] });
    answer(msgUrl('rare'), { results: [{ id: 1, chat: { ref: 'c' }, text: 'rare' }], has_more: false });
    await settle();
    assert.equal(messageSearch.value.truncated, false);
    assert.equal(messageSearch.value.indexed, true);

    // An archive without the full-text layer is reported, not shown as "no results".
    searchQuery.value = 'more';
    onSearchInput();
    await sleep(350);
    answer(chatUrl('more'), { chats: [] });
    answer(msgUrl('more'), { results: [], has_more: false, indexed: false });
    await settle();
    assert.equal(messageSearch.value.indexed, false);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_whitespace_is_not_a_search_and_the_sections_read_as_searching_at_once() -> None:
    _run_node(
        _script("""
(async () => {
    // A keystroke marks BOTH sections as searching before the debounce fires,
    // so "No messages found" cannot flash for a query nothing has answered.
    searchQuery.value = 'x';
    onSearchInput();
    assert.equal(searchLoading.value, true);
    assert.equal(messageSearch.value.loading, true);
    await sleep(350);
    answer(chatUrl('x'), { chats: [] });
    answer(msgUrl('x'), { results: [{ id: 1, chat: { ref: 'c' }, text: 'x' }], has_more: false });
    await settle();
    assert.equal(messageSearch.value.results.length, 1);

    // Whitespace only: results reset, but what was typed stays in the field.
    searchQuery.value = '   ';
    onSearchInput();
    assert.equal(searchQuery.value, '   ');
    assert.deepEqual(messageSearch.value.results, []);
    assert.equal(messageSearch.value.loading, false);
    assert.equal(searchLoading.value, false);
    await sleep(350);
    assert.equal(requests.length, 2, 'no request for whitespace');
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_the_chat_half_ignores_stale_answers_and_a_401_clears_its_rows() -> None:
    _run_node(
        _script("""
(async () => {
    searchQuery.value = 'first';
    onSearchInput();
    await sleep(350);
    searchQuery.value = 'second';
    onSearchInput();
    await sleep(350);
    // The newer chat lookup answers first; the older one arrives late and must be ignored.
    answer(chatUrl('second'), { chats: [{ ref: 'two', title: 'Two' }] });
    await settle();
    answer(chatUrl('first'), { chats: [{ ref: 'one', title: 'One' }] });
    await settle();
    assert.deepEqual(searchResults.value.map(c => c.ref), ['two'], 'the stale chat answer was discarded');
    answer(msgUrl('first'), { results: [], has_more: false });
    answer(msgUrl('second'), { results: [], has_more: false });
    await settle();
    assert.equal(searchLoading.value, false);

    // Clearing invalidates an in-flight chat lookup too.
    searchQuery.value = 'third';
    onSearchInput();
    await sleep(350);
    clearSearch();
    answer(chatUrl('third'), { chats: [{ ref: 'three', title: 'Three' }] });
    await settle();
    assert.deepEqual(searchResults.value, []);

    // An expired session clears the rows on show and returns to the login.
    searchQuery.value = 'fourth';
    onSearchInput();
    await sleep(350);
    answer(chatUrl('fourth'), { chats: [{ ref: 'four', title: 'Four' }] });
    answer(msgUrl('fourth'), { results: [], has_more: false });
    await settle();
    assert.deepEqual(searchResults.value.map(c => c.ref), ['four']);
    searchQuery.value = 'fourthagain';
    onSearchInput();
    await sleep(350);
    fail(chatUrl('fourthagain'), 401);
    answer(msgUrl('fourthagain'), { results: [], has_more: false });
    await settle();
    assert.deepEqual(searchResults.value, []);
    assert.equal(isAuthenticated.value, false);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_match_rule_is_word_prefix_any_order_case_insensitive() -> None:
    """The one rule behind the sidebar snippet and the in-message marks: whole words that start with any query word."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            "const SEARCH_WORD_RE = /[\\p{L}\\p{N}_]+/gu;",
            "const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');",
            _extract_const_arrow_function(html, "foldForMatch", asynchronous=False),
            _extract_const_arrow_function(html, "searchMatchPattern", asynchronous=False),
            _extract_const_arrow_function(html, "searchMatchRanges", asynchronous=False),
            """
const words = (text, query) => searchMatchRanges(text, searchMatchPattern(query)).map(([a, b]) => text.slice(a, b));
// A prefix marks the whole word, case-insensitively, and each query word counts on its own, in any order.
assert.deepEqual(words('Compra Zanahorias y numeros', 'zana'), ['Zanahorias']);
assert.deepEqual(words('covid-19 numbers are in', 'num covid'), ['covid', 'numbers']);
// A word boundary is required: nothing inside another word.
assert.deepEqual(words('xzana zana', 'zana'), ['zana']);
// Unicode letters are words; punctuation in the query is ignored; a query with no word matches nothing.
assert.deepEqual(words('Año nuevo, niño', 'niñ'), ['niño']);
assert.deepEqual(words('a+b (c)', '+++'), []);
assert.equal(searchMatchPattern('+++'), null);
// Regex metacharacters in a query word cannot escape into the pattern.
assert.deepEqual(words('c++ and c#', 'c'), ['c', 'c']);
assert.deepEqual(words('1.5 vs 1', '1.5'), ['1', '5', '1']);
// Ranges are half-open offsets into the text.
assert.deepEqual(searchMatchRanges('ab zanahoria', searchMatchPattern('zana')), [[3, 12]]);
// SQLite folds diacritics, so the mark must land on the accented word the server matched —
// both ways round, and on the ORIGINAL offsets whichever normal form the text arrives in.
assert.deepEqual(words('cafe con leche', 'café'), ['cafe']);
assert.deepEqual(words('café con leche', 'cafe'), ['café']);
assert.deepEqual(words('mas o menos, más o menos', 'mas'), ['mas', 'más']);
const decomposed = 'ma\u0301s tarde';   // NFD: m, a, combining acute, s
assert.equal(decomposed.length, 10, 'the fixture must really be decomposed');
assert.equal(decomposed.normalize('NFC').slice(0, 3), 'más');
assert.deepEqual(words(decomposed, 'mas'), ['ma\u0301s']);
assert.deepEqual(searchMatchRanges('un café', searchMatchPattern('cafe')), [[3, 7]]);
// An entity splits a word across DOM text nodes; the applier joins them and matches once,
// so the offsets have to be right across the seam.
assert.deepEqual(searchMatchRanges('hola ' + 'mun' + 'do', searchMatchPattern('mundo')), [[5, 10]]);
""",
        ]
    )
    _run_node(script)


def test_opening_a_hit_marks_that_message_only_for_the_search_that_produced_it() -> None:
    _run_node(
        _script("""
(async () => {
    // The hit opens and the query is unchanged: the intent names the row and the query.
    searchQuery.value = 'zana';
    await openMessageSearchResult({ id: 9, chat: { ref: 'chatB' } });
    assert.deepEqual(opened, [['message', 'chatB', 9]]);
    assert.deepEqual(messageHighlight.value, { query: 'zana', messageId: 9 });

    // The chat could not be opened: nothing to mark.
    messageHighlight.value = null; openOutcome = false;
    await openMessageSearchResult({ id: 10, chat: { ref: 'chatC' } });
    assert.equal(messageHighlight.value, null);
    openOutcome = true;

    // The query changed while the navigation ran: the old search does not mark the new one's pane.
    searchQuery.value = 'first';
    const opening = openMessageSearchResult({ id: 11, chat: { ref: 'chatD' } });
    searchQuery.value = 'second';
    await opening;
    assert.equal(messageHighlight.value, null, 'a superseded search must not mark');

    // An empty field still opens the hit (keyboard path) but marks nothing.
    searchQuery.value = '';
    await openMessageSearchResult({ id: 12, chat: { ref: 'chatE' } });
    assert.equal(opened[opened.length - 1][1], 'chatE');
    assert.equal(messageHighlight.value, null);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_a_hit_mark_belongs_to_the_search_that_opened_it() -> None:
    """Clearing or retyping the field takes the mark away; the in-chat filter's own marks stay."""
    _run_node(
        _script("""
(async () => {
    // The field is watched, and the watcher is this function: any change drops a hit mark.
    searchQuery.value = 'zana';
    await openMessageSearchResult({ id: 9, chat: { ref: 'chatB' } });
    assert.deepEqual(messageHighlight.value, { query: 'zana', messageId: 9 });
    dropHitHighlight();
    assert.equal(messageHighlight.value, null, 'a hit mark must not outlive its search');

    // The in-chat filter marks every row and has no message id: its box owns those.
    messageHighlight.value = { query: 'needle', messageId: null };
    dropHitHighlight();
    assert.deepEqual(messageHighlight.value, { query: 'needle', messageId: null });

    // Nothing marked: nothing to do.
    messageHighlight.value = null;
    dropHitHighlight();
    assert.equal(messageHighlight.value, null);
})().catch(error => { process.stderr.write(`${error.stack}\\n`); process.exitCode = 1; });
""")
    )


def test_the_field_watcher_is_wired_to_the_drop() -> None:
    """The behaviour above only reaches the user if the field actually drives it."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "watch(() => searchQuery.value.trim(), dropHitHighlight)" in html
