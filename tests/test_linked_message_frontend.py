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
        out = self._run(
            """
TARGET = async (ref) => { selectedChat.value = { ref }; return false }
PROBE = async () => ({ ok: true, json: async () => [{ id: 43 }] })
""",
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
            ("const openNotificationTarget = async (chatRef, messageId) =>",),
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
