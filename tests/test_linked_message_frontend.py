"""A shared message link (/?chat=<ref>&msg=<id>) and the jump it ends in.

The link opens after the first session check or a sign-in, beside the sidebar
load, never behind a lock on the whole app. Every jump (a link, a reply quote, a
search hit, the gallery) outlines its target the same way, and the outline lives
in reactive state, so it survives Vue drawing the message again.

These tests EXECUTE the template's own code under node. The template tests
compile the shipped markup with the vendored Vue and mount it on a small
in-memory renderer, so "the form stays mounted" is checked on real vnodes.
"""

import json
import re
import unittest
from pathlib import Path

from test_frontend_bootstrap import (
    _PRODUCER_PRELUDE,
    INDEX_HTML,
    NODE,
    _run_setup_program,
    _setup_function,
    _setup_slice,
)

VUE = (
    Path(__file__).resolve().parents[1] / "telegram_archive" / "web" / "static" / "vendor" / "vue-3.5.41.global.prod.js"
)

# The vendored Vue on a renderer whose nodes are plain objects. Each node keeps
# its props, so a test reads the class Vue computed. REMOVED lists every node
# Vue took out of the tree.
_VUE_RUNTIME = f"""
const vm = require('node:vm')
const fs = require('node:fs')
vm.runInThisContext(fs.readFileSync({json.dumps(str(VUE))}, 'utf8'))
const REMOVED = []
const node = (tag, text = '') => ({{ tag, text, props: {{}}, children: [], parent: null }})
const detach = (child) => {{
    if (!child.parent) return
    const list = child.parent.children
    list.splice(list.indexOf(child), 1)
    child.parent = null
}}
const renderer = Vue.createRenderer({{
    createElement: (tag) => node(tag),
    createText: (text) => node('#text', text),
    createComment: (text) => node('#comment', text),
    setText: (n, text) => {{ n.text = text }},
    setElementText: (n, text) => {{ n.children = []; n.text = text }},
    insert: (child, parent, anchor) => {{
        detach(child)
        const at = anchor ? parent.children.indexOf(anchor) : -1
        if (at === -1) parent.children.push(child)
        else parent.children.splice(at, 0, child)
        child.parent = parent
    }},
    remove: (child) => {{ REMOVED.push(child); detach(child) }},
    parentNode: (n) => n.parent,
    nextSibling: (n) => {{
        if (!n.parent) return null
        const list = n.parent.children
        return list[list.indexOf(n) + 1] || null
    }},
    patchProp: (el, key, prev, next) => {{ el.props[key] = next }},
}})
// The browser build decodes entities through the DOM; the snippets hold none.
const compile = (template) => Vue.compile(template, {{ decodeEntities: (s) => s }})
const walk = (n, out = []) => {{ out.push(n); for (const c of n.children) walk(c, out); return out }}
const mount = (template, setup) => {{
    const root = node('#root')
    renderer.createApp({{ setup, render: compile(template) }}).mount(root)
    return root
}}
"""


# The open behind every notification, search hit and link. It takes the link's
# guard since the link stops when the reader leaves.
_ONT_DECLARATION = next(
    d
    for d in (
        "const openNotificationTarget = async (chatRef, messageId, externalGuard = null) =>",
        "const openNotificationTarget = async (chatRef, messageId) =>",
    )
    if d in INDEX_HTML.read_text(encoding="utf-8")
)


def _optional(html: str, declaration: str, fallback: str) -> str:
    """The declaration when the template has it, else a stand-in.

    The stand-in lets the same test run on an older template and fail on its
    assertion, not on a missing name.
    """
    return _setup_slice(html, declaration) if declaration in html else fallback


def _opening_tag(html: str, marker: str) -> str:
    """The opening ``<div ...>`` tag that contains ``marker``."""
    at = html.index(marker)
    start = html.rindex("<div", 0, at)
    return html[start : html.index(">", at) + 1]


def _directive(tag: str) -> str:
    match = re.search(r'\b(v-if|v-else-if)="([^"]*)"', tag)
    if match:
        return f'{match.group(1)}="{match.group(2)}"'
    assert re.search(r"\bv-else\b", tag), tag
    return "v-else"


@unittest.skipUnless(NODE, "node is required to execute the template")
class TestTheSignInFormStaysMounted(unittest.TestCase):
    """A sign-in attempt keeps the form. Only the first session check shows the loading screen."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _gate(self) -> str:
        # The three screens of #app, with their real conditions and nothing inside.
        app = self.html[self.html.index('<div id="app"') :]
        tags = {
            "loading": app[app.index("<div", 1) : app.index(">", app.index("<div", 1)) + 1],
            "login": _opening_tag(app, 'class="login-page'),
            "main": app[app.index("<div", app.index("<!-- Main App -->")) :].split(">", 1)[0] + ">",
        }
        return "<div>" + "".join(f'<div {_directive(tag)} id="{name}"></div>' for name, tag in tags.items()) + "</div>"

    def test_a_wrong_password_keeps_the_same_form_on_screen(self) -> None:
        program = (
            _VUE_RUNTIME
            + """
const { ref } = Vue
const isAuthenticated = ref(false)
const loadingAuth = ref(true)
const checkingSession = ref(true)
const openingLink = ref(false)
const loginMode = ref('password')
const loginForm = ref({ username: 'reader', password: 'wrong' })
const tokenInput = ref('')
const loginError = ref('')
const userRole = ref('')
const currentUsername = ref('')
const noDownload = ref(false)
const openInitialView = async () => {}
const initNotifications = async () => {}
let answer
const fetch = () => new Promise((resolve) => { answer = resolve })
"""
            + _setup_slice(self.html, "const performLogin = async () =>")
            + f"""
const root = mount({json.dumps(self._gate())}, () => ({{ isAuthenticated, loadingAuth, checkingSession, openingLink }}))
const shown = () => walk(root).filter((n) => n.props.id).map((n) => n.props.id)
const findLogin = () => walk(root).find((n) => n.props.id === 'login')
const seen = {{}}
;(async () => {{
    seen.checking = shown()
    // The first session check ends: no session.
    loadingAuth.value = false
    checkingSession.value = false
    await Vue.nextTick()
    seen.signedOut = shown()
    const form = findLogin()
    const attempt = performLogin()
    await Vue.nextTick()
    seen.during = shown()
    seen.sameFormDuring = findLogin() === form
    answer({{ ok: false, status: 401 }})
    await attempt
    await Vue.nextTick()
    seen.after = shown()
    seen.sameFormAfter = findLogin() === form
    seen.formRemoved = REMOVED.includes(form)
    seen.error = loginError.value
    console.log(JSON.stringify(seen))
}})()
"""
        )
        out = _run_setup_program(self.html, (), program, "")
        self.assertEqual(out["checking"], ["loading"])
        self.assertEqual(out["signedOut"], ["login"])
        self.assertEqual(out["during"], ["login"])
        self.assertEqual(out["after"], ["login"])
        self.assertTrue(out["sameFormDuring"])
        self.assertTrue(out["sameFormAfter"])
        self.assertFalse(out["formRemoved"])
        self.assertEqual(out["error"], "Invalid username or password")

    def test_a_successful_sign_in_opens_the_initial_view(self) -> None:
        out = _run_setup_program(
            self.html,
            ("const performLogin = async () =>",),
            """
const ref = (value) => ({ value })
const isAuthenticated = ref(false)
const loadingAuth = ref(false)
const loginMode = ref('password')
const loginForm = ref({ username: 'reader', password: 'right' })
const tokenInput = ref('')
const loginError = ref('')
const userRole = ref('')
const currentUsername = ref('')
const noDownload = ref(false)
const CALLS = []
const openInitialView = async () => { CALLS.push(['view', isAuthenticated.value]) }
const initNotifications = async () => { CALLS.push(['notifications']) }
const fetch = async () => ({ ok: true, json: async () => ({ success: true, role: 'viewer' }) })
""",
            "performLogin().then(() => console.log(JSON.stringify(CALLS)))",
        )
        self.assertEqual(out, [["view", True], ["notifications"]])


# What openInitialView and the link entry close over. EVENTS records the order.
_INITIAL_VIEW_PRELUDE = """
const print = console.log
console.log = () => {}
let location = new URL('https://archive.example/?chat=target&msg=42&keep=1')
const history = { state: { tab: 1 }, replaceState(state, _, url) { location = new URL(url) } }
const EVENTS = []
const TOASTS = []
const showToast = (message) => { TOASTS.push(message) }
const selectedChat = { value: null }
const currentNav = { value: { type: 'chatList' } }
const later = (name) => new Promise((resolve) => setTimeout(() => { EVENTS.push(name); resolve() }, 5))
const loadAccounts = () => later('accounts')
const loadTranscriptionState = () => later('transcription')
const loadStats = () => later('stats')
const loadChats = async () => { EVENTS.push('chats') }
const loadFolders = async () => { EVENTS.push('folders') }
const loadArchivedCount = async () => { EVENTS.push('archived') }
const checkNewChanges = () => { EVENTS.push('changes') }
const setupInfiniteScroll = () => { EVENTS.push('scroll') }
let TARGET = async () => true
const openNotificationTarget = (ref, id) => { EVENTS.push(['target', ref, id]); return TARGET(ref, id) }
let PROBE = async () => ({ ok: true, json: async () => [] })
const fetch = (url) => { EVENTS.push(['probe', url]); return PROBE(url) }
"""

# An older template reads the link from these instead.
_LEGACY_LINK_STATE = """
const linkParams = location.searchParams
const openingLink = { value: Boolean(linkParams.get('chat')) }
"""


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheLinkOpensBesideTheSidebar(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, setup: str, drive: str) -> dict:
        declarations = [
            decl
            for decl in (
                "const dropLinkParams = (...names) =>",
                "const linkedMessageMissing = async (chatRef, messageId) =>",
                "const openLinkedMessage = async () =>",
            )
            if decl in self.html
        ]
        legacy = "" if "const openLinkedMessage" in self.html else _LEGACY_LINK_STATE
        prelude = "\n".join(
            (_INITIAL_VIEW_PRELUDE, setup, legacy, _setup_function(self.html, "const openInitialView = async () =>"))
        )
        epilogue = (
            "(async () => {\n"
            + drive
            + "\nprint(JSON.stringify({ events: EVENTS, toasts: TOASTS, url: location.search }))\n"
            + "})().catch((e) => { console.error(e); process.exit(1) })"
        )
        return _run_setup_program(self.html, tuple(declarations), prelude, epilogue)

    def test_a_stalled_link_does_not_hold_the_sidebar(self) -> None:
        out = self._run(
            "TARGET = () => new Promise(() => {})  // the message window never answers",
            "openInitialView(); await new Promise((r) => setTimeout(r, 50))",
        )
        names = [e if isinstance(e, str) else e[0] for e in out["events"]]
        for name in ("chats", "folders", "archived", "changes", "scroll"):
            self.assertIn(name, names)

    def test_viewer_settings_load_before_the_link_and_the_sidebar(self) -> None:
        out = self._run("", "await openInitialView()")
        names = [e if isinstance(e, str) else e[0] for e in out["events"]]
        first_view = min(names.index("target"), names.index("chats"))
        for name in ("accounts", "transcription", "stats"):
            self.assertLess(names.index(name), first_view, names)

    def test_an_opened_link_leaves_the_url_and_keeps_the_rest(self) -> None:
        out = self._run("", "await openInitialView()")
        self.assertIn(["target", "target", 42], out["events"])
        self.assertEqual(out["url"], "?keep=1")
        self.assertEqual(out["toasts"], [])

    def test_a_malformed_message_id_opens_the_chat_at_its_newest_page(self) -> None:
        for raw in ("abc", "0", "-3", "1e3"):
            with self.subTest(msg=raw):
                out = self._run(
                    f"location = new URL('https://archive.example/?chat=target&msg={raw}&keep=1')",
                    "await openInitialView()",
                )
                self.assertIn(["target", "target", None], out["events"])
                self.assertEqual(out["url"], "?keep=1")

    def test_a_malformed_id_is_dropped_even_when_the_chat_fails_to_open(self) -> None:
        out = self._run(
            "location = new URL('https://archive.example/?chat=target&msg=abc')\nTARGET = async () => false",
            "await openInitialView()",
        )
        self.assertEqual(out["url"], "?chat=target")

    def test_a_message_the_archive_lacks_says_so_once_and_cleans_the_url(self) -> None:
        # Past the newest message the archive answers an empty page. A gap in the
        # ids answers the next message instead.
        cases = {"past the newest message": "[]", "inside a gap": "[{ id: 43 }]"}
        for name, rows in cases.items():
            with self.subTest(name):
                out = self._run(
                    "TARGET = async (ref) => { selectedChat.value = { ref }; return false }\n"
                    + f"PROBE = async () => ({{ ok: true, json: async () => {rows} }})",
                    "await openInitialView()",
                )
                self.assertEqual(out["toasts"], ["Linked message unavailable"])
                self.assertEqual(out["url"], "?keep=1")
                probes = [e for e in out["events"] if isinstance(e, list) and e[0] == "probe"]
                self.assertEqual(probes, [["probe", "/api/chats/target/messages?after_id=41&limit=1"]])

    def test_a_failure_on_the_way_keeps_the_link_for_a_refresh(self) -> None:
        cases = {
            "server error": "PROBE = async () => ({ ok: false, status: 502 })",
            "network error": "PROBE = async () => { throw new TypeError('offline') }",
            "message is there": "PROBE = async () => ({ ok: true, json: async () => [{ id: 42 }] })",
        }
        for name, probe in cases.items():
            with self.subTest(name):
                out = self._run(
                    "TARGET = async (ref) => { selectedChat.value = { ref }; return false }\n" + probe,
                    "await openInitialView()",
                )
                self.assertEqual(out["url"], "?chat=target&msg=42&keep=1")
                self.assertEqual(out["toasts"], [])

    def test_a_chat_that_is_not_visible_keeps_the_link(self) -> None:
        out = self._run("TARGET = async () => false", "await openInitialView()")
        self.assertEqual(out["url"], "?chat=target&msg=42&keep=1")
        self.assertFalse([e for e in out["events"] if isinstance(e, list) and e[0] == "probe"])

    def test_no_link_opens_nothing(self) -> None:
        out = self._run(
            "location = new URL('https://archive.example/?keep=1')",
            "await openInitialView()",
        )
        self.assertFalse([e for e in out["events"] if isinstance(e, list)])
        self.assertIn("chats", out["events"])


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestOneOutlineForEveryJump(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _jump_state(self, ref_impl: str) -> str:
        return (
            f"const ref = {ref_impl}\n"
            + _optional(self.html, "const JUMP_OUTLINE_MS = ", "")
            + "\n"
            + _optional(self.html, "const jumpTarget = ref(null)", "const jumpTarget = ref(null)")
            + "\n"
            + _optional(self.html, "const markJumpTarget = (msgId) =>", "const markJumpTarget = () => {}")
            + "\n"
            + _optional(self.html, "const isJumpTarget = (msgId) =>", "const isJumpTarget = () => false")
            + "\n"
        )

    def test_a_reply_jump_to_an_album_member_outlines_and_centers_that_picture(self) -> None:
        """Not only a cold link: a reply quote, a search hit and the gallery take the same path."""
        prelude = """
const selectedChat = { value: { id: 7, ref: 'r7' } }
const openingLink = { value: false }
const sortedMessages = { value: [{ id: 11 }, { id: 12 }] }
const openDeletedMessages = { value: {} }
const isDeletedFolded = () => false
const deletedKey = () => ''
const SCROLLED = []
const picture = { name: 'picture 12', scrollIntoView: (o) => SCROLLED.push(['picture', o.block]) }
const row = {
    name: 'row 11',
    dataset: { msgId: '11' },
    querySelector: (selector) => (selector === '[data-album-message-id="12"]' ? picture : null),
    scrollIntoView: (o) => SCROLLED.push(['row', o.block]),
    classList: { add: () => {}, remove: () => {} },
    offsetWidth: 0,
}
const findMessageElement = () => row
const nextTick = (fn) => Promise.resolve().then(fn)
const window = { matchMedia: () => ({ matches: true }) }
const pendingLink = null
""" + self._jump_state("(value) => ({ value })")
        out = _run_setup_program(
            self.html,
            ("const scrollToMessage = (msgId) =>",),
            prelude,
            """scrollToMessage(12).then((found) => {
    console.log(JSON.stringify({ found, scrolled: SCROLLED, marked: [isJumpTarget(11), isJumpTarget(12)] }))
    process.exit(0)
})""",
        )
        self.assertEqual(out, {"found": True, "scrolled": [["picture", "center"]], "marked": [False, True]})

    def test_the_outline_ends_and_belongs_to_one_chat(self) -> None:
        out = _run_setup_program(
            self.html,
            (),
            "const selectedChat = { value: { ref: 'r7' } }\n"
            + "const TIMERS = []\nconst setTimeout = (fn, ms) => { TIMERS.push([fn, ms]); return TIMERS.length }\n"
            + "const clearTimeout = () => {}\n"
            + self._jump_state("(value) => ({ value })"),
            """markJumpTarget(5)
const seen = { marked: isJumpTarget(5), other: isJumpTarget(6) }
selectedChat.value = { ref: 'r8' }
seen.otherChat = isJumpTarget(5)
selectedChat.value = { ref: 'r7' }
TIMERS.at(-1)[0]()
seen.afterTimer = isJumpTarget(5)
seen.ms = TIMERS.at(-1)[1]
console.log(JSON.stringify(seen))""",
        )
        self.assertEqual(out, {"marked": True, "other": False, "otherChat": False, "afterTimer": False, "ms": 5000})

    def test_the_outline_survives_an_album_picture_that_fails_to_load(self) -> None:
        """A failing picture swaps its button for the missing placeholder; the outline follows."""
        start = self.html.index('<template v-for="albumMsg in getAlbumForMessage(msg)"')
        end = self.html.index("</template>", start) + len("</template>")
        block = self.html[start:end]
        self.assertNotIn("<template", block[1:])
        program = (
            _VUE_RUNTIME
            + "const selectedChat = Vue.ref({ ref: 'r7' })\n"
            + self._jump_state("Vue.ref")
            + """
const album = [
    { id: 11, media: { type: 'photo', file_path: 'a.jpg' } },
    { id: 12, media: { type: 'photo', file_path: 'b.jpg' } },
]
const broken = Vue.reactive({})
const ctx = {
    msg: album[0],
    getAlbumForMessage: () => album,
    mediaUnavailable: (m) => Boolean(broken[m.id]),
    mediaMissingReason: () => 'missing',
    mediaMissingWords: () => 'Not downloaded',
    MISSING_GLYPHS: { missing: [] },
    getMediaUrl: (m) => `/media/${m.id}`,
    handleImageError: () => {},
    videoStillFailed: () => false,
    videoStillUrl: () => '',
    failVideoStill: () => {},
    openMedia: () => {},
    isJumpTarget,
}
"""
            + f"const root = mount({json.dumps('<div>' + block + '</div>')}, () => ctx)\n"
            + """
const item = (id) => walk(root).find((n) => String(n.props['data-album-message-id']) === String(id))
const describe = (id) => ({ tag: item(id).tag, cls: String(item(id).props.class || '') })
;(async () => {
    markJumpTarget(12)
    await Vue.nextTick()
    const before = describe(12)
    broken[12] = true
    await Vue.nextTick()
    const after = describe(12)
    console.log(JSON.stringify({ before, after, neighbour: describe(11) }))
    process.exit(0)
})()
"""
        )
        out = _run_setup_program(self.html, (), program, "")
        self.assertEqual(out["before"]["tag"], "button")
        self.assertIn("jump-target", out["before"]["cls"].split())
        self.assertEqual(out["after"]["tag"], "div")
        self.assertIn("album-missing", out["after"]["cls"].split())
        self.assertIn("jump-target", out["after"]["cls"].split())
        self.assertNotIn("jump-target", out["neighbour"]["cls"].split())

    def test_a_row_outlines_its_bubble_unless_its_album_holds_the_target(self) -> None:
        row = _opening_tag(self.html, 'class="message-row flex items-end"')
        service = _opening_tag(self.html, ':data-msg-id="msg.id" class="flex justify-center my-2"')
        self.assertIn("isJumpTarget(msg.id) && !isFirstInAlbum(msg, index) ? 'jump-target' : ''", row)
        self.assertIn("'jump-target': isJumpTarget(msg.id)", service)
        css = self.html[self.html.index(".jump-target > .message-bubble,") :]
        css = css[: css.index("}")]
        self.assertIn(".album-item.jump-target", css)
        self.assertIn("outline: 3px solid rgb(var(--tg-accent));", css)
        self.assertNotIn("message-flash", self.html)


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestOnlyTheLinkSpeaksForTheLink(unittest.TestCase):
    """A gallery, date, Show in chat or audio jump never says "Linked message unavailable"."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_a_window_without_its_target_returns_false_quietly(self) -> None:
        prelude = (
            _PRODUCER_PRELUDE.replace(
                "const showToast = () => {}", "const TOASTS = []\nconst showToast = (m) => { TOASTS.push(m) }"
            )
            + """
const newestLoadedMessageId = () => 9
const setupMessagesScrollObserver = () => {}
const scrollToMessage = async () => false
const startMessageRefresh = () => {}
let messagesNewerObserver = null
const loadNewerSentinel = { value: null }
PANE_ROWS = [messageRow(4)]
"""
            + _setup_function(self.html, "const loadMessagesAroundId = async (messageId, externalGuard = null) =>")
        )
        out = _run_setup_program(
            self.html,
            (
                "const messageIdKey = (msg) =>",
                "const upsertMessages = (incomingMessages, ",
                "const resetMessagePagination = () =>",
            ),
            prelude,
            "loadMessagesAroundId(5).then((found) => console.log(JSON.stringify({ found, toasts: TOASTS })))",
        )
        self.assertEqual(out, {"found": False, "toasts": []})

    def test_a_forum_jump_names_a_failure_and_stays_quiet_on_a_missing_message(self) -> None:
        prelude = """
const ref = (value) => ({ value })
const chats = ref([{ id: 2, ref: 'forumRef', is_forum: true }])
const resolveChatById = async (chatRef) => chats.value.find((c) => c.ref === chatRef) || null
const selectChat = async () => {}
const selectTopic = async () => {}
const loadMessagesAroundId = async () => true
const reloadViewAfterFailedJump = async () => {}
const GENERAL_TOPIC_ID = 1
const topics = { value: [{ id: 1, title: 'General' }] }
const nextTick = async () => {}
const TOASTS = []
const showToast = (m) => { TOASTS.push(m) }
let ANSWER = null
const fetch = async () => ANSWER
"""
        out = _run_setup_program(
            self.html,
            (_ONT_DECLARATION,),
            prelude,
            """(async () => {
    const seen = {}
    ANSWER = { ok: false, status: 502, json: async () => ({ detail: 'busy' }) }
    seen.serverError = [await openNotificationTarget('forumRef', 9), TOASTS.splice(0)]
    ANSWER = { ok: true, json: async () => [{ id: 10 }] }
    seen.missing = [await openNotificationTarget('forumRef', 9), TOASTS.splice(0)]
    ANSWER = { ok: true, json: async () => [{ id: 9 }] }
    seen.found = [await openNotificationTarget('forumRef', 9), TOASTS.splice(0)]
    console.log(JSON.stringify(seen))
})()""",
        )
        self.assertEqual(out["serverError"], [False, ["Could not locate that topic"]])
        self.assertEqual(out["missing"], [False, []])
        self.assertEqual(out["found"], [True, []])


def _auth_check_on_mount(html: str) -> str:
    """The ``onMounted`` call that runs the first session check, as one statement."""
    call = html.index("onMounted(async () =>", html.index("// Auth check on mount"))
    return _setup_function(html[call:], "onMounted(async () =>") + ");"


# What the first session check closes over. SEEN records checkingSession at the
# moment the first view starts to load.
_AUTH_CHECK_PRELUDE = """
const print = console.log
console.log = () => {}
console.warn = () => {}
console.error = () => {}
const ref = (value) => ({ value })
let MOUNT = null
const onMounted = (fn) => { MOUNT = fn }
const navigator = {}
let notificationClickReady = false
let pendingNotificationClick = null
const openNotificationTarget = async () => {}
const authRequired = ref(false)
const oidcEnabled = ref(false)
const isAuthenticated = ref(false)
const userRole = ref('')
const currentUsername = ref('')
const noDownload = ref(false)
const loginError = ref('')
const authCheckFailed = ref(false)
const loadingAuth = ref(true)
const checkingSession = ref(true)
const window = { location: new URL('https://archive.example/?chat=target&msg=42') }
const history = { replaceState() {} }
const SEEN = []
let VIEW = async () => {}
const openInitialView = () => { SEEN.push(checkingSession.value); return VIEW() }
const initNotifications = async () => {}
const initWebSocket = () => {}
const flushPendingNotificationClick = async () => {}
const finishInitialization = () => {}
let AUTH = async () => ({ ok: true, json: async () => ({ authenticated: true }) })
const fetch = (url) => AUTH(url)
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
"""


@unittest.skipUnless(NODE, "node is required to execute the session check")
class TestTheFirstSessionCheckEndsTheLoadingScreen(unittest.TestCase):
    """The loading screen belongs to the first /api/auth/check only, whatever it answers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, setup: str, drive: str) -> dict:
        program = _AUTH_CHECK_PRELUDE + setup + "\n" + _auth_check_on_mount(self.html) + "\n"
        epilogue = (
            "(async () => {\n"
            + drive
            + "\nprint(JSON.stringify({ checking: checkingSession.value, failed: authCheckFailed.value,"
            + " loading: loadingAuth.value, seen: SEEN }))\nprocess.exit(0)\n})()"
        )
        return _run_setup_program(self.html, (), program, epilogue)

    def test_a_failed_session_check_shows_the_sign_in_form(self) -> None:
        cases = {
            "server error": "AUTH = async () => ({ ok: false, status: 502 })",
            "network error": "AUTH = async () => { throw new TypeError('offline') }",
        }
        for name, setup in cases.items():
            with self.subTest(name):
                out = self._run(setup, "await MOUNT()")
                self.assertEqual(out, {"checking": False, "failed": True, "loading": False, "seen": []})

    def test_a_signed_in_boot_drops_the_loading_screen_before_the_first_view_loads(self) -> None:
        # The first view never finishes here: the app must be on screen anyway.
        out = self._run("VIEW = () => new Promise(() => {})", "MOUNT()\nawait sleep(20)")
        self.assertEqual(out["seen"], [False])
        self.assertFalse(out["checking"])

    def test_a_signed_out_answer_shows_the_form_without_loading_a_view(self) -> None:
        out = self._run(
            "AUTH = async () => ({ ok: true, json: async () => ({ authenticated: false, auth_required: true }) })",
            "await MOUNT()",
        )
        self.assertEqual(out, {"checking": False, "failed": False, "loading": False, "seen": []})


@unittest.skipUnless(NODE, "node is required to execute the helper")
class TestSingleSignOnCarriesOnlyTheCurrentLink(unittest.TestCase):
    """A link that already opened left the URL. A later single sign-on must not carry it again."""

    def test_the_carry_reads_the_address_bar_of_now(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        out = _run_setup_program(
            html,
            ("const rememberSsoLink = () =>",),
            """
// The page loaded with a link, and the link opened and left the URL.
let location = new URL('https://archive.example/?keep=1')
const linkUrl = new URL('https://archive.example/?chat=old&msg=7')
const linkParams = linkUrl.searchParams
const ssoLinkKey = 'archive_sso_link'
const STORE = {}
const sessionStorage = {
    setItem: (key, value) => { STORE[key] = value },
    getItem: (key) => STORE[key] ?? null,
    removeItem: (key) => { delete STORE[key] },
}
""",
            """rememberSsoLink()
const seen = { opened: STORE[ssoLinkKey] ?? null }
location = new URL('https://archive.example/?chat=new&msg=9&keep=1')
rememberSsoLink()
const carried = JSON.parse(STORE[ssoLinkKey])
seen.carried = { chat: carried.chat, msg: carried.msg }
seen.ttl = carried.expires - Date.now()
console.log(JSON.stringify(seen))""",
        )
        self.assertIsNone(out["opened"])
        self.assertEqual(out["carried"], {"chat": "new", "msg": "9"})
        self.assertGreater(out["ttl"], 590000)
        self.assertLessEqual(out["ttl"], 600000)


# The real view picks (selectChat, openForumTopics, selectTopic), the real open,
# the real link entry and the real message window, scroll and hold, over a
# stubbed network and one pane. LOOKUP_MS, PROBE_MS and WINDOW_MS delay the chat
# lookup, the one-row probe and the message window. WINDOWS lists each window
# asked for: [chat, topic, message id].
_VIEW_PICK_PRELUDE = """
const print = console.log
console.log = () => {}
console.error = () => {}
let location = new URL('https://archive.example/?chat=target&msg=42&keep=1#frag')
const history = { state: null, replaceState(state, _, url) { location = new URL(url) } }
const ref = (value) => ({ value })
const selectedChat = ref(null)
const selectedPaneTopic = ref(null)
const messages = ref([])
const messageSearchQuery = ref('')
const deletedOnly = ref(false)
const editedOnly = ref(false)
const chatStats = ref(null)
const pinnedMessages = ref([])
const currentPinnedIndex = ref(0)
const showPinnedOnly = ref(false)
const showMediaGallery = ref(false)
const loading = ref(false)
const navStack = ref([{ type: 'chatList', filter: 'all' }])
const currentNav = { get value() { return navStack.value[navStack.value.length - 1] } }
const topics = ref([])
let chatVersion = 0
let galleryReturnState = null
const GENERAL_TOPIC_ID = 1
const TOASTS = []
const showToast = (m) => { TOASTS.push(m) }
const stopMessageRefresh = () => {}
const startMessageRefresh = () => {}
const resetMessagePagination = () => {}
const setupMessagesScrollObserver = () => {}
const scrollToBottom = () => {}
const nextTick = (fn) => Promise.resolve().then(fn)
// A newest page. Message ids repeat across chats, so every chat has a 42.
const loadMessages = async () => { messages.value = [{ id: 43 }, { id: 42 }] }
const loadChatStats = async () => {}
const loadPinnedMessages = async () => {}
const getChatName = (chat) => chat.ref
const navigateTo = (entry) => { navStack.value.push(entry) }
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const CHATS = { target: { ref: 'target' }, other: { ref: 'other' }, forum: { ref: 'forum', is_forum: true } }
const TOPICS = { forum: [{ id: 1, title: 'General' }, { id: 7, title: 'Topic 7' }] }
const loadTopics = async (chatRef) => { topics.value = TOPICS[chatRef] || [] }
let LOOKUP_MS = 0
const resolveChatById = async (chatRef) => { await sleep(LOOKUP_MS); return CHATS[chatRef] || null }
let PROBE_MS = 0
let PROBE = () => ({ ok: true, json: async () => [{ id: 42, reply_to_top_id: 7 }] })
const WINDOWS = []
let WINDOW_MS = 0
let WINDOW = () => ({ ok: true, json: async () => [{ id: 42 }, { id: 41 }, { id: 40 }] })
const fetch = async (url) => {
    const before = /before_id=(\\d+)/.exec(url)
    if (before) {
        const topic = /topic_id=(\\d+)/.exec(url)
        WINDOWS.push([selectedChat.value?.ref, topic ? Number(topic[1]) : null, Number(before[1]) - 1])
        await sleep(WINDOW_MS)
        return WINDOW()
    }
    // The newer half of a window: the target is the newest message.
    if (url.includes('&limit=50')) return { ok: true, json: async () => [] }
    await sleep(PROBE_MS)
    return PROBE()
}
// What the message window closes over.
const activeTopicId = () => {
    const topicId = selectedPaneTopic.value?.id
    return !topicId || topicId === 'all' ? null : topicId
}
const messageWindowIsContiguous = ref(false)
let newestMessageId = null
const newestLoadedMessageId = () => null
const hasMoreNewer = ref(false)
const viewingPinnedWindow = ref(false)
let messagesScrollObserver = null
let messagesNewerObserver = null
const loadMoreSentinel = ref(null)
const loadNewerSentinel = ref(null)
// One pane with one row, for message 42 of whichever chat is open. relayout()
// is a late size change: every live observer of the hold sees it.
const SMOOTH = []
const pane = { scrollTop: 0, clientHeight: 400, children: [], getBoundingClientRect: () => ({ top: 0 }), contains: () => true }
const row42 = {
    offset: 1000, height: 100, dataset: { msgId: '42' }, querySelector: () => null,
    getBoundingClientRect() { return { top: this.offset - pane.scrollTop, height: this.height } },
    scrollIntoView: (options) => SMOOTH.push(options),
}
pane.children.push(row42)
const messagesContainer = ref(pane)
const findMessageElement = (msgId) => (messages.value.some((m) => Number(m.id) === Number(msgId)) ? row42 : null)
const sortedMessages = { get value() { return messages.value } }
const openDeletedMessages = ref({})
const isDeletedFolded = () => false
const deletedKey = () => ''
const OBSERVERS = new Set()
class ResizeObserver {
    constructor(cb) { this.cb = cb; OBSERVERS.add(this) }
    observe() {}
    disconnect() { OBSERVERS.delete(this) }
}
class MutationObserver {
    constructor(cb) { this.cb = cb; OBSERVERS.add(this) }
    observe() {}
    disconnect() { OBSERVERS.delete(this) }
}
const relayout = () => { for (const o of [...OBSERVERS]) o.cb([]) }
const window = { matchMedia: () => ({ matches: false }), addEventListener: () => {} }
const SEEN = {}
"""


@unittest.skipUnless(NODE, "node is required to execute the navigation chain")
class TestTheReaderLeavingAbandonsTheLink(unittest.TestCase):
    """A late answer for the link never pulls the reader back to the linked chat."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, setup: str, drive: str) -> dict:
        html = self.html
        prelude = "\n".join(
            (
                _VIEW_PICK_PRELUDE,
                setup,
                _setup_slice(html, "const JUMP_OUTLINE_MS = "),
                _setup_slice(html, "const jumpTarget = ref(null)"),
                _setup_slice(html, "const markJumpTarget = (msgId) =>"),
                _setup_slice(html, "const isJumpTarget = (msgId) =>"),
                _optional(html, "const clearJumpTarget = () =>", ""),
                _optional(html, "const findJumpTarget = (msgId) =>", ""),
                _optional(html, "const centerJumpTarget = (msgId) =>", ""),
                _optional(html, "const JUMP_HOLD_MS = ", "let stopHoldingJumpTarget = () => {}"),
                _optional(html, "const holdJumpTarget = (msgId) =>", "const holdJumpTarget = () => {}"),
                _setup_slice(html, "const scrollToMessage = (msgId) =>"),
                _setup_function(html, "const loadMessagesAroundId = async (messageId, externalGuard = null) =>"),
                _optional(html, "const asLinkPick = (open) =>", "const asLinkPick = (open) => open()"),
                _optional(html, "const noteViewPick = () =>", "const noteViewPick = () => {}"),
                # Before closeChat the Back button only closed the pane.
                _optional(html, "const closeChat = () =>", "const closeChat = () => { selectedChat.value = null }"),
            )
        )
        declarations = (
            "const reloadViewAfterFailedJump = async (chat) =>",
            "const openForumTopics = async (chat) =>",
            "const selectTopic = async (chat, topic, options = {}) =>",
            "const selectChat = async (chat, options = {}) =>",
            _ONT_DECLARATION,
            "const dropLinkParams = (...names) =>",
            "const linkedMessageMissing = async (chatRef, messageId) =>",
            "const openLinkedMessage = async () =>",
        )
        epilogue = (
            "(async () => {\n"
            + drive
            + "\nprint(JSON.stringify({ chat: selectedChat.value?.ref ?? null,"
            + " topic: selectedPaneTopic.value?.id ?? null, windows: WINDOWS, toasts: TOASTS,"
            + " url: location.search + location.hash, outlined: isJumpTarget(42), held: OBSERVERS.size > 0,"
            + " rows: messages.value.length, nav: currentNav.value.type, seen: SEEN }))\n"
            + "process.exit(0)\n})().catch((e) => { console.error(e); process.exit(1) })"
        )
        return _run_setup_program(html, declarations, prelude, epilogue)

    def test_a_chat_the_reader_opens_while_the_lookup_runs_stays_open(self) -> None:
        out = self._run(
            "LOOKUP_MS = 30",
            "const link = openLinkedMessage()\nawait sleep(10)\nawait selectChat(CHATS.other)\nawait link\nawait sleep(40)",
        )
        self.assertEqual(out["chat"], "other")
        self.assertEqual(out["windows"], [])
        self.assertEqual(out["toasts"], [])
        # A refresh must not reopen the link the reader left. The rest stays.
        self.assertEqual(out["url"], "?keep=1#frag")

    def test_a_topic_the_reader_opens_while_the_forum_probe_runs_stays_open(self) -> None:
        out = self._run(
            "location = new URL('https://archive.example/?chat=forum&msg=42&keep=1#frag')\nPROBE_MS = 30",
            "const link = openLinkedMessage()\nawait sleep(10)\n"
            + "await selectTopic(CHATS.forum, TOPICS.forum[0])\nawait link\nawait sleep(40)",
        )
        self.assertEqual([out["chat"], out["topic"]], ["forum", 1])
        self.assertEqual(out["windows"], [])
        self.assertEqual(out["url"], "?keep=1#frag")

    def test_the_links_own_picks_do_not_abandon_it(self) -> None:
        for name, url, window in (
            ("plain chat", "?chat=target&msg=42&keep=1#frag", ["target", None, 42]),
            ("forum topic", "?chat=forum&msg=42&keep=1#frag", ["forum", 7, 42]),
        ):
            with self.subTest(name):
                out = self._run(f"location = new URL('https://archive.example/{url}')", "await openLinkedMessage()")
                self.assertEqual(out["windows"], [window])
                self.assertTrue(out["outlined"])
                self.assertEqual(out["url"], "?keep=1#frag")
                self.assertEqual(out["toasts"], [])

    def test_a_link_kept_for_a_retry_leaves_the_url_when_the_reader_moves_on(self) -> None:
        out = self._run(
            "location = new URL('https://archive.example/?chat=gone&msg=42&keep=1#frag')",
            "await openLinkedMessage()\nconst kept = location.search\nawait selectChat(CHATS.other)\n"
            + "TOASTS.push(kept)",
        )
        self.assertEqual(out["toasts"], ["Could not open that chat", "?chat=gone&msg=42&keep=1"])
        self.assertEqual(out["url"], "?keep=1#frag")

    def test_a_chat_switch_ends_the_outline_and_the_centering(self) -> None:
        # A notification click opens a chat with no wheel, touch or key input.
        # The other chat has a message 42 too: a live hold would center it.
        out = self._run(
            "",
            "await openLinkedMessage()\nSEEN.before = [isJumpTarget(42), OBSERVERS.size > 0, pane.scrollTop]\n"
            + "await selectChat(CHATS.other)\npane.scrollTop = 0\nrow42.offset = 1600\nrelayout()\n"
            + "SEEN.after = [isJumpTarget(42), pane.scrollTop]",
        )
        self.assertEqual(out["seen"], {"before": [True, True, 850], "after": [False, 0]})
        self.assertEqual(out["chat"], "other")
        self.assertFalse(out["held"])

    def test_a_forum_list_the_reader_opens_while_the_window_loads_does_not_empty_the_pane(self) -> None:
        # Opening a forum shows its topic list and keeps the pane on the linked chat.
        # The window still fills that pane.
        out = self._run(
            "WINDOW_MS = 30",
            "const link = openLinkedMessage()\nawait sleep(10)\nawait selectChat(CHATS.forum)\n"
            + "await link\nawait sleep(40)\nSEEN.loading = loading.value",
        )
        self.assertEqual([out["chat"], out["nav"]], ["target", "topics"])
        self.assertEqual(out["rows"], 3)
        self.assertEqual(out["seen"], {"loading": False})
        self.assertEqual(out["url"], "?keep=1#frag")
        self.assertEqual(out["toasts"], [])

    def test_leaving_for_the_chat_list_abandons_the_link(self) -> None:
        # The phone's Back button while the window loads. A refresh must not
        # reopen the chat, and a failed window must not reopen it either.
        for name, window in (
            ("window answers", ""),
            ("window fails", "WINDOW = () => ({ ok: false, status: 500 })"),
        ):
            with self.subTest(name):
                out = self._run(
                    "WINDOW_MS = 30\n" + window,
                    "const link = openLinkedMessage()\nawait sleep(10)\ncloseChat()\nawait link\nawait sleep(40)",
                )
                self.assertIsNone(out["chat"])
                self.assertEqual(out["url"], "?keep=1#frag")
                self.assertFalse(out["held"])

    def test_the_back_button_leaves_through_close_chat(self) -> None:
        # The chat header's Back button (phones), compiled and clicked: in a topic
        # it steps back to the topic list, anywhere else it leaves the chat
        # through closeChat.
        start = self.html.index('aria-label="Back" @click="currentNav.type')
        tag = self.html[self.html.rindex("<button", 0, start) : self.html.index(">", start) + 1]
        program = (
            _VUE_RUNTIME
            + f"""
const CALLS = []
const clicks = {{}}
for (const [name, nav] of [['in a topic', {{ type: 'chat', topicId: 7 }}], ['in a chat', {{ type: 'chat' }}]]) {{
    CALLS.length = 0
    const root = mount({json.dumps("<div>" + tag + "</button></div>")}, () => ({{
        currentNav: nav,
        selectedChat: {{ ref: 'target' }},
        navigateBack: () => CALLS.push('navigateBack'),
        closeChat: () => CALLS.push('closeChat'),
    }}))
    walk(root).find((n) => n.tag === 'button').props.onClick()
    clicks[name] = [...CALLS]
}}
console.log(JSON.stringify(clicks))
"""
        )
        out = _run_setup_program(self.html, (), program, "")
        self.assertEqual(out, {"in a topic": ["navigateBack"], "in a chat": ["closeChat"]})

    def test_a_forum_link_to_a_message_the_archive_lacks_says_so_once(self) -> None:
        out = self._run(
            "location = new URL('https://archive.example/?chat=forum&msg=99999&keep=1#frag')\n"
            + "PROBE = () => ({ ok: true, json: async () => [] })",
            "await openLinkedMessage()",
        )
        self.assertEqual(out["toasts"], ["Linked message unavailable"])
        self.assertEqual(out["url"], "?keep=1#frag")
        self.assertEqual(out["windows"], [])

    def test_a_forum_link_keeps_the_url_after_a_server_error(self) -> None:
        out = self._run(
            "location = new URL('https://archive.example/?chat=forum&msg=42&keep=1#frag')\n"
            + "PROBE = () => ({ ok: false, status: 502 })",
            "await openLinkedMessage()",
        )
        self.assertEqual(out["toasts"], ["Could not locate that topic"])
        self.assertEqual(out["url"], "?chat=forum&msg=42&keep=1#frag")


# A pane over rows with fixed content offsets: a row's top on screen is its
# offset minus the pane's scrollTop. RESIZE is the live observer callback, null
# once disconnected. MUTATE serves an older template that also watched the rows.
_CENTER_PRELUDE = """
const print = console.log
const ref = (value) => ({ value })
const selectedChat = ref({ ref: 'r7' })
let pendingLink = null
const sortedMessages = { value: [{ id: 42 }] }
const openDeletedMessages = { value: {} }
const isDeletedFolded = () => false
const deletedKey = () => ''
const nextTick = (fn) => Promise.resolve().then(fn)
const SMOOTH = []
const makeNode = (offset, height) => ({
    offset, height, dataset: { msgId: '42' }, querySelector: () => null,
    getBoundingClientRect() { return { top: this.offset - pane.scrollTop, height: this.height } },
    scrollIntoView: (options) => SMOOTH.push(options),
})
let node = makeNode(1000, 100)
const below = makeNode(1200, 80)
const rows = [below, node]
const pane = {
    scrollTop: 0, clientHeight: 400, children: rows,
    getBoundingClientRect: () => ({ top: 0 }),
    contains: () => true,
}
const messagesContainer = { value: pane }
const findMessageElement = () => node
const OBSERVED = new Set()
let RESIZE = null
let MUTATE = null
// A size change reaches the hold only for an element it observes.
const resize = (el) => { if (RESIZE && OBSERVED.has(el)) RESIZE([]) }
class ResizeObserver {
    constructor(cb) { RESIZE = cb }
    observe(el) { OBSERVED.add(el) }
    disconnect() { RESIZE = null }
}
class MutationObserver {
    constructor(cb) { MUTATE = cb }
    observe() {}
    disconnect() { MUTATE = null }
}
let REDUCED = false
const LISTENERS = []
const window = {
    matchMedia: () => ({ matches: REDUCED }),
    addEventListener: (name, fn, options) => { LISTENERS.push({ name, fn, options }) },
}
const input = (name) => {
    for (const l of LISTENERS) if (l.name === name && !l.options.signal.aborted) l.fn()
}
const TIMERS = []
const setTimeout = (fn, ms) => { TIMERS.push({ fn, ms }); return TIMERS.length }
const clearTimeout = () => {}
"""


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestALinkHoldsItsTargetCentered(unittest.TestCase):
    """A link lands on its target at once and keeps it centered while the page settles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def _run(self, drive: str) -> dict:
        html = self.html
        prelude = "\n".join(
            (
                _CENTER_PRELUDE,
                _setup_slice(html, "const JUMP_OUTLINE_MS = "),
                _setup_slice(html, "const jumpTarget = ref(null)"),
                _setup_slice(html, "const markJumpTarget = (msgId) =>"),
                _setup_slice(html, "const isJumpTarget = (msgId) =>"),
                _optional(html, "const findJumpTarget = (msgId) =>", ""),
                _optional(html, "const centerJumpTarget = (msgId) =>", ""),
                _optional(html, "const JUMP_HOLD_MS = ", ""),
                _optional(html, "const holdJumpTarget = (msgId) =>", "const holdJumpTarget = () => {}"),
            )
        )
        epilogue = (
            "(async () => {\nconst seen = {}\n"
            + drive
            + "\nprint(JSON.stringify(seen))\nprocess.exit(0)\n})().catch((e) => { console.error(e); process.exit(1) })"
        )
        return _run_setup_program(html, ("const scrollToMessage = (msgId) =>",), prelude, epilogue)

    def test_late_layout_keeps_the_target_centered_until_the_reader_scrolls(self) -> None:
        for reduced in (False, True):
            with self.subTest(reduced_motion=reduced):
                out = self._run(
                    f"""REDUCED = {str(reduced).lower()}
pendingLink = {{ chatRef: 'r7', messageId: 42 }}
seen.found = await scrollToMessage(42)
seen.smooth = SMOOTH.length
seen.landed = pane.scrollTop
// A picture in a row below the target gets its size and pushes the target.
node.offset = 1300
below.offset = 1500
resize(below)
seen.afterRowResize = pane.scrollTop
// Vue draws the target anew: a new node, found by its id. Then the pane gets
// shorter (a bar opens above it).
node = makeNode(1500, 100)
rows[1] = node
pane.clientHeight = 300
resize(pane)
seen.afterPaneResize = pane.scrollTop
// The reader scrolls: the hold ends at once.
input('wheel')
seen.live = RESIZE !== null
seen.outlined = isJumpTarget(42)""",
                )
                self.assertEqual(
                    out,
                    {
                        "found": True,
                        "smooth": 0,
                        "landed": 850,
                        "afterRowResize": 1150,
                        "afterPaneResize": 1400,
                        "live": False,
                        "outlined": True,
                    },
                )

    def test_every_kind_of_reader_input_and_the_time_limit_end_the_hold(self) -> None:
        for end in ("touchstart", "pointerdown", "keydown", "timer"):
            with self.subTest(end):
                stop = "TIMERS.find((t) => t.ms === 8000).fn()" if end == "timer" else f"input('{end}')"
                out = self._run(
                    "pendingLink = { chatRef: 'r7', messageId: 42 }\nawait scrollToMessage(42)\n"
                    + f"seen.before = RESIZE !== null\n{stop}\n"
                    + "seen.after = RESIZE !== null",
                )
                self.assertEqual(out, {"before": True, "after": False})

    def test_other_jumps_keep_their_smooth_scroll(self) -> None:
        out = self._run(
            "seen.found = await scrollToMessage(42)\nseen.smooth = SMOOTH\n"
            + "seen.scrollTop = pane.scrollTop\nseen.held = RESIZE !== null",
        )
        self.assertEqual(
            out, {"found": True, "smooth": [{"behavior": "smooth", "block": "center"}], "scrollTop": 0, "held": False}
        )
