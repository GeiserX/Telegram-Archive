"""What changed for one chat: ``/api/changes?chat_ref=`` and the viewer's ways in.

The feed lists deletions, edits and transcripts across the archive. A chat's
menu and info panel open it narrowed to that chat. The narrowing is read only
and never widens what a viewer may see: the ref resolves through the same
resolver as every {chat_ref} route, so a chat outside the viewer's scope
answers exactly like an unknown one.

A ref names one account's copy of a chat. For a channel or a group the feed
narrows to the chat, every copy the viewer may see: an event exists only in
the copy whose listener was up, so narrowing to the ref's own copy would drop
what only another account captured, which the feed of every chat lists. The
cross-account deduplication still lists each event once. A private chat
narrows to the ref's own account, because its id is the other party's user id
and names a different conversation in each account.

The adapter and route halves run on ``real_adapter``, so the SQL is compiled
and executed by SQLite and PostgreSQL. The viewer half runs the real setup
code under node.
"""

import os
import tempfile
import unittest
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_changes_chat_"))

from httpx import ASGITransport, AsyncClient
from test_account_fold_followups import (
    BASE,
    COLLIDING_PRIVATE,
    OUTSIDER,
    SHARED_CHANNEL,
    add_version,
    mark_deleted,
    seed_shared_chats,
)
from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program
from test_transcript_consumers import _voice

from telegram_archive.db.adapter import ChatScope
from telegram_archive.web import main as web_main

UNRESTRICTED = ChatScope.build()
ONLY_ACCOUNT_1 = ChatScope.build(accounts={1})

# Two groups of account 1, distinctive so a failure names itself.
GROUP_A = -1004300001
GROUP_B = -1004300002
# A private chat of account 1 whose id account 2 holds as an imported chat.
IMPORTED_PRIVATE = 420000003


async def seed_two_groups(adapter, *, messages: int = 3) -> None:
    for chat_id, title in ((GROUP_A, "group a"), (GROUP_B, "group b")):
        await adapter.upsert_chat({"id": chat_id, "type": "group", "title": title}, account_id=1)
        for message_id in range(1, messages + 1):
            await adapter.insert_message(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "sender_id": OUTSIDER,
                    "date": BASE,
                    "text": "original text",
                    "raw_data": {},
                },
                account_id=1,
            )


async def delete_through_the_archive(adapter, *, chat_id: int, message_id: int, at) -> None:
    """Soft-delete the way the listener does, so the stored date has the adapter's format.

    The paging tests compare dates in SQL; a raw UPDATE would store SQLite's
    own text form, which the cursor was never written against.
    """
    await adapter.mark_message_deleted(chat_id, message_id, at, account_id=1)


async def chat_ref(adapter, chat_id: int, account_id: int = 1) -> str:
    return (await adapter.get_chat_by_id(chat_id, account_id=account_id))["ref"]


def events(changes) -> list[tuple[str, str, int]]:
    """(kind, chat title, message id) per row, order preserved."""
    return [(c["kind"], c["chat"]["title"], c["message_id"]) for c in changes]


# ============================================================================
# The adapter
# ============================================================================


class TestAdapterChatFilter:
    async def test_it_lists_only_the_chat_s_deletions_edits_and_transcripts(self, real_adapter):
        # The voice helper names its chat; seeding the groups after it names them back.
        await _voice(real_adapter, 90, "fixture words", chat_id=GROUP_A)
        await _voice(real_adapter, 91, "fixture words", chat_id=GROUP_B)
        await seed_two_groups(real_adapter)
        for chat_id in (GROUP_A, GROUP_B):
            await mark_deleted(real_adapter, account_id=1, chat_id=chat_id, message_id=1, at=BASE)
            await add_version(
                real_adapter, account_id=1, chat_id=chat_id, message_id=2, old_text="was", at=BASE + timedelta(1)
            )

        everything = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)
        narrowed = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50, chat_id=GROUP_A, account_id=1)

        assert len(everything) == 6
        assert sorted(events(narrowed)) == [
            ("deleted", "group a", 1),
            ("edited", "group a", 2),
            ("transcript", "group a", 90),
        ]

    async def test_a_higher_account_s_copy_lists_events_a_lower_account_also_captured(self, real_adapter):
        """Both accounts hold the channel and both captured the deletion and the edit.

        Through either account's ref the feed lists each event exactly once:
        neither hidden behind a row outside the filter nor listed per copy.
        """
        await seed_shared_chats(real_adapter)
        for account_id in (1, 2):
            at = BASE + timedelta(seconds=account_id)
            await mark_deleted(real_adapter, account_id=account_id, chat_id=SHARED_CHANNEL, message_id=11, at=at)
            await add_version(
                real_adapter, account_id=account_id, chat_id=SHARED_CHANNEL, message_id=11, old_text="was", at=at
            )

        copy_2 = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, limit=50, chat_id=SHARED_CHANNEL, account_id=2
        )
        copy_1 = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, limit=50, chat_id=SHARED_CHANNEL, account_id=1
        )

        assert sorted((c["kind"], c["message_id"]) for c in copy_2) == [("deleted", 11), ("edited", 11)]
        assert sorted((c["kind"], c["message_id"]) for c in copy_1) == [("deleted", 11), ("edited", 11)]
        # The feed of every chat still lists each event once.
        assert len(await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)) == 2

    async def test_a_shared_chat_lists_what_only_another_account_captured(self, real_adapter):
        """The regression: the chat list shows account 1's copy of a channel both hold.

        Only account 2's listener saw the deletion and the transcript. The feed
        for every chat lists them under the channel, so the feed narrowed
        through account 1's ref must list them too, each once.
        """
        await _voice(real_adapter, 12, "fixture words", chat_id=SHARED_CHANNEL, account_id=2, chat_type="channel")
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        ref_2 = await chat_ref(real_adapter, SHARED_CHANNEL, account_id=2)

        everything = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)
        for account_id in (1, 2):
            narrowed = await real_adapter.get_recent_changes(
                scope=UNRESTRICTED, limit=50, chat_id=SHARED_CHANNEL, account_id=account_id
            )
            assert sorted(events(narrowed)) == [("deleted", "shared channel", 11), ("transcript", "shared channel", 12)]
            # Each row leads to the copy that holds it, as in the feed of every chat.
            assert {c["chat"]["ref"] for c in narrowed} == {ref_2}
        assert sorted(events(everything)) == [("deleted", "shared channel", 11), ("transcript", "shared channel", 12)]

    async def test_a_private_chat_narrows_to_the_ref_s_own_conversation(self, real_adapter):
        """Two accounts' private chats share the other party's id and are two conversations.

        Account 1's deletion, edit and transcript must not leak into account
        2's conversation, and account 2's deletion not into account 1's.
        """
        await _voice(real_adapter, 30, "fixture words", chat_id=COLLIDING_PRIVATE, account_id=1, chat_type="private")
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=1, chat_id=COLLIDING_PRIVATE, message_id=22, at=BASE)
        await add_version(real_adapter, account_id=1, chat_id=COLLIDING_PRIVATE, message_id=22, old_text="was", at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=COLLIDING_PRIVATE, message_id=22, at=BASE + timedelta(seconds=1)
        )

        copy_1 = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, limit=50, chat_id=COLLIDING_PRIVATE, account_id=1
        )
        copy_2 = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, limit=50, chat_id=COLLIDING_PRIVATE, account_id=2
        )

        assert sorted((c["kind"], c["message_id"]) for c in copy_1) == [
            ("deleted", 22),
            ("edited", 22),
            ("transcript", 30),
        ]
        assert [(c["kind"], c["message_id"]) for c in copy_2] == [("deleted", 22)]

    async def test_a_private_ref_ignores_another_account_s_copy_typed_otherwise(self, real_adapter):
        """The ref's type decides, not each row's.

        An HTML-export import sends no chat type, so account 2's copy of the
        id account 1 holds as a private chat is stored as "unknown". It is
        still another conversation, and the chat list shows it as another
        chat: account 1's private feed must not list its deletion.
        """
        await real_adapter.upsert_chat({"id": IMPORTED_PRIVATE, "type": "private", "title": "a person"}, account_id=1)
        await real_adapter.upsert_chat({"id": IMPORTED_PRIVATE, "title": "an imported chat"}, account_id=2)
        for account_id in (1, 2):
            await real_adapter.insert_message(
                {
                    "id": 33,
                    "chat_id": IMPORTED_PRIVATE,
                    "sender_id": OUTSIDER,
                    "date": BASE,
                    "text": "original text",
                    "raw_data": {},
                },
                account_id=account_id,
            )
        await mark_deleted(real_adapter, account_id=1, chat_id=IMPORTED_PRIVATE, message_id=33, at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=IMPORTED_PRIVATE, message_id=33, at=BASE + timedelta(seconds=1)
        )

        private = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, limit=50, chat_id=IMPORTED_PRIVATE, account_id=1
        )
        imported = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, limit=50, chat_id=IMPORTED_PRIVATE, account_id=2
        )

        assert [(c["kind"], c["chat"]["type"]) for c in private] == [("deleted", "private")]
        assert [(c["kind"], c["chat"]["type"]) for c in imported] == [("deleted", "unknown")]

    async def test_the_filter_never_widens_the_scope(self, real_adapter):
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)

        assert (
            await real_adapter.get_recent_changes(scope=ONLY_ACCOUNT_1, limit=50, chat_id=SHARED_CHANNEL, account_id=2)
            == []
        )

    async def test_the_cursor_pages_through_the_chat_only(self, real_adapter):
        await seed_two_groups(real_adapter, messages=5)
        for message_id in range(1, 6):
            for offset, chat_id in enumerate((GROUP_A, GROUP_B)):
                await delete_through_the_archive(
                    real_adapter,
                    chat_id=chat_id,
                    message_id=message_id,
                    at=BASE + timedelta(minutes=message_id, seconds=offset),
                )

        seen: list[int] = []
        before = None
        for _ in range(5):
            page = await real_adapter.get_recent_changes(
                before=before, scope=UNRESTRICTED, limit=2, chat_id=GROUP_A, account_id=1
            )
            assert {c["chat"]["title"] for c in page} <= {"group a"}
            seen.extend(c["message_id"] for c in page)
            if len(page) < 2:
                break
            before = web_main._parse_changes_bound(page[-1]["date"], "before")

        assert seen == [5, 4, 3, 2, 1]


# ============================================================================
# The route
# ============================================================================


@pytest.fixture
async def app_on(real_adapter):
    """Point the real app at a real adapter and put it back afterwards."""
    saved = (web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER, web_main.config.display_chat_ids)
    web_main.db = real_adapter
    web_main.AUTH_ENABLED = False
    web_main.ALLOW_ANONYMOUS_VIEWER = True
    web_main.config.display_chat_ids = set()
    try:
        yield real_adapter
    finally:
        web_main.app.dependency_overrides.clear()
        (
            web_main.db,
            web_main.AUTH_ENABLED,
            web_main.ALLOW_ANONYMOUS_VIEWER,
            web_main.config.display_chat_ids,
        ) = saved


def as_principal(**fields) -> None:
    user = web_main.UserContext(username="changes-chat-test", role=fields.pop("role", "viewer"), **fields)
    web_main.app.dependency_overrides[web_main.require_auth] = lambda: user


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


class TestRouteChatFilter:
    async def test_chat_ref_narrows_the_feed_and_pages_with_the_cursor(self, app_on):
        await seed_two_groups(app_on, messages=3)
        for message_id in range(1, 4):
            for offset, chat_id in enumerate((GROUP_A, GROUP_B)):
                await delete_through_the_archive(
                    app_on,
                    chat_id=chat_id,
                    message_id=message_id,
                    at=BASE + timedelta(minutes=message_id, seconds=offset),
                )
        ref_a = await chat_ref(app_on, GROUP_A)
        as_principal(role="master")

        async with client() as http:
            first = await http.get("/api/changes", params={"chat_ref": ref_a, "limit": 2})
            assert first.status_code == 200, first.text
            body = first.json()
            second = await http.get(
                "/api/changes", params={"chat_ref": ref_a, "limit": 2, "before": body["next_before"]}
            )
            everything = await http.get("/api/changes")

        assert first.headers["cache-control"] == "private, no-store"
        assert events(body["changes"]) == [("deleted", "group a", 3), ("deleted", "group a", 2)]
        assert body["next_before"] == body["changes"][-1]["date"]
        assert events(second.json()["changes"]) == [("deleted", "group a", 1)]
        assert second.json()["next_before"] is None
        assert all(c["chat"]["ref"] == ref_a for c in body["changes"] + second.json()["changes"])
        assert len(everything.json()["changes"]) == 6

    async def test_a_chat_outside_the_scope_answers_like_an_unknown_one(self, app_on):
        await seed_shared_chats(app_on)
        await mark_deleted(app_on, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        ref_1 = await chat_ref(app_on, SHARED_CHANNEL, account_id=1)
        ref_2 = await chat_ref(app_on, SHARED_CHANNEL, account_id=2)

        async with client() as http:
            as_principal(allowed_accounts={1})
            by_account = await http.get("/api/changes", params={"chat_ref": ref_2})
            as_principal(allowed_chat_refs={ref_1})
            by_ref = await http.get("/api/changes", params={"chat_ref": ref_2})
            unknown = await http.get("/api/changes", params={"chat_ref": "noSuchChatRef0000001"})
            empty = await http.get("/api/changes", params={"chat_ref": ""})
            as_principal(role="master")
            allowed = await http.get("/api/changes", params={"chat_ref": ref_2})

        assert unknown.status_code == 404
        for resp in (by_account, by_ref, empty):
            assert (resp.status_code, resp.json()) == (unknown.status_code, unknown.json())
        assert allowed.status_code == 200, allowed.text
        assert [(c["kind"], c["message_id"]) for c in allowed.json()["changes"]] == [("deleted", 11)]

    async def test_a_shared_chat_s_feed_is_the_same_through_either_account_s_ref(self, app_on):
        """The chat list shows one copy of a channel both accounts hold.

        Account 1 alone captured a deletion and account 2 alone an edit. Through
        either ref the feed lists both, each under the copy that holds it.
        """
        await seed_shared_chats(app_on)
        await mark_deleted(app_on, account_id=1, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        await add_version(
            app_on, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, old_text="was", at=BASE + timedelta(1)
        )
        ref_1 = await chat_ref(app_on, SHARED_CHANNEL, account_id=1)
        ref_2 = await chat_ref(app_on, SHARED_CHANNEL, account_id=2)
        as_principal(role="master")

        async with client() as http:
            by_ref_1 = await http.get("/api/changes", params={"chat_ref": ref_1})
            by_ref_2 = await http.get("/api/changes", params={"chat_ref": ref_2})

        for resp in (by_ref_1, by_ref_2):
            assert resp.status_code == 200, resp.text
            assert [(c["kind"], c["chat"]["ref"]) for c in resp.json()["changes"]] == [
                ("edited", ref_2),
                ("deleted", ref_1),
            ]

    async def test_a_private_chat_s_ref_passes_its_account_through(self, app_on):
        """Both accounts deleted message 22 in their own conversation with one person.

        Each ref must list its own account's deletion and not the other's,
        which only the ref's account tells apart.
        """
        await seed_shared_chats(app_on)
        for account_id in (1, 2):
            await mark_deleted(
                app_on,
                account_id=account_id,
                chat_id=COLLIDING_PRIVATE,
                message_id=22,
                at=BASE + timedelta(seconds=account_id),
            )
        refs = {account_id: await chat_ref(app_on, COLLIDING_PRIVATE, account_id=account_id) for account_id in (1, 2)}
        as_principal(role="master")

        async with client() as http:
            answers = {
                account_id: await http.get("/api/changes", params={"chat_ref": ref}) for account_id, ref in refs.items()
            }

        for account_id, resp in answers.items():
            assert resp.status_code == 200, resp.text
            assert [(c["kind"], c["message_id"], c["chat"]["ref"]) for c in resp.json()["changes"]] == [
                ("deleted", 22, refs[account_id])
            ]

    async def test_a_no_download_login_still_gets_no_transcripts(self, app_on):
        await _voice(app_on, 1, "fixture words")
        ref = await chat_ref(app_on, -420700001)

        async with client() as http:
            as_principal(no_download=True)
            hidden = await http.get("/api/changes", params={"chat_ref": ref})
            as_principal()
            shown = await http.get("/api/changes", params={"chat_ref": ref})

        assert hidden.json()["changes"] == []
        assert [c["kind"] for c in shown.json()["changes"]] == ["transcript"]


# ============================================================================
# The viewer
# ============================================================================

HTML = INDEX_HTML.read_text(encoding="utf-8")

_FEED_PRELUDE = """
const ref = (value) => ({ value })
const nextTick = (fn) => Promise.resolve().then(fn)
const URLS = []
const CALLS = []
// What the feed held when each request went out: the cards under the chip.
const FEED_AT_FETCH = []
const fetch = async (url) => {
    FEED_AT_FETCH.push({ cards: changesFeed.value.length, next: changesNextBefore.value })
    URLS.push(url)
    return { ok: true, status: 200, json: async () => ({ changes: [], next_before: null }) }
}
globalThis.HTMLElement = class {}
const document = { activeElement: null, addEventListener: () => {}, removeEventListener: () => {} }
const showChangesFeed = ref(false)
const changesFeed = ref([])
const changesChat = ref(null)
const changesNextBefore = ref(null)
const changesLoading = ref(false)
const changesLoadingOlder = ref(false)
const changesError = ref('')
const changesEmptyPages = ref(0)
const changesFilterOpen = ref(false)
let changesRequestSeq = 0
let changesTrigger = null
let changesReturnFocus = null
let changesReturnState = null
const changesSinceIso = () => null
const changesFeedPeriod = ref('7')
const changesSince = ref('7')
const changesKinds = ref({ deleted: true, edited: true, transcript: true, reaction: false })
const shownChangeCount = () => 0
const isAuthenticated = ref(true)
const loadMoreChangesIfInView = () => {}
const markChangesSeen = () => CALLS.push('seen')
const handleChangesKeydown = () => {}
const mainMenuButton = ref(null)
const selectedChat = ref({ ref: 'fakeRefGroupA0000001', title: 'Fixture Group' })
const selectedPaneTopic = ref(null)
const messagesContainer = ref(null)
const showInfoPanel = ref(true)
const changesTitle = ref({ focus: () => CALLS.push('focusTitle') })
const chatMenuOpen = ref(true)
const closeInfoPanel = () => { showInfoPanel.value = false; CALLS.push('closeInfo') }
const getChatName = (chat) => chat.title
const infoPanelToggleBtn = ref(null)
const chatMenuButton = ref(null)
const setupMessagesScrollObserver = () => {}
// A focusable stand-in for a button, connected to the page or not.
const fakeButton = (name, isConnected = true) => Object.assign(new HTMLElement(), {
    isConnected,
    focus: () => CALLS.push(`focus:${name}`),
})
const settle = () => new Promise(r => setTimeout(r, 0))
"""


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestViewerOpensTheFeedForOneChat(unittest.TestCase):
    def _run(self, epilogue: str) -> dict:
        return _run_setup_program(
            HTML,
            (
                "const fetchChanges = async (before) =>",
                "let changesFeedChatRef = null",
                "const setChangesChat = (chat) =>",
                "const openChangesFeed = (fromMenu = false, chat = null, returnFocus = null) =>",
                "const closeChangesFeed = (restoreFocus = true) =>",
                "const openChangesFeedForChat = (from) =>",
                "const widenChangesFeed = () =>",
            ),
            _FEED_PRELUDE,
            epilogue,
        )

    def test_the_menu_opens_it_narrowed_and_the_chip_widens_it(self) -> None:
        out = self._run(
            """(async () => {
    openChangesFeedForChat('menu')
    await new Promise(r => setTimeout(r, 0))
    const opened = { url: URLS[0], chat: changesChat.value, menu: chatMenuOpen.value, calls: CALLS.splice(0) }
    widenChangesFeed()
    await new Promise(r => setTimeout(r, 0))
    const widened = { url: URLS[1], chat: changesChat.value, calls: CALLS.splice(0) }
    console.log(JSON.stringify({ opened, widened }))
})();"""
        )
        self.assertIn("chat_ref=fakeRefGroupA0000001", out["opened"]["url"])
        self.assertEqual(out["opened"]["chat"], {"ref": "fakeRefGroupA0000001", "title": "Fixture Group"})
        self.assertFalse(out["opened"]["menu"])
        # One chat's newest entry says nothing about the others: nothing is marked seen.
        self.assertNotIn("seen", out["opened"]["calls"])
        self.assertNotIn("chat_ref", out["widened"]["url"])
        self.assertIsNone(out["widened"]["chat"])
        self.assertIn("seen", out["widened"]["calls"])
        self.assertIn("focusTitle", out["widened"]["calls"])

    def test_the_info_panel_closes_and_the_sidebar_button_shows_every_chat(self) -> None:
        out = self._run(
            """(async () => {
    openChangesFeedForChat('info')
    await new Promise(r => setTimeout(r, 0))
    const info = { url: URLS[0], panel: showInfoPanel.value, calls: CALLS.splice(0) }
    openChangesFeed({ type: 'click' })
    await new Promise(r => setTimeout(r, 0))
    console.log(JSON.stringify({ info, sidebar: { url: URLS[1], chat: changesChat.value } }))
})();"""
        )
        self.assertIn("chat_ref=fakeRefGroupA0000001", out["info"]["url"])
        self.assertFalse(out["info"]["panel"])
        self.assertIn("closeInfo", out["info"]["calls"])
        # The sidebar button passes its click event, which is no chat.
        self.assertNotIn("chat_ref", out["sidebar"]["url"])
        self.assertIsNone(out["sidebar"]["chat"])

    def test_another_chat_s_cards_never_sit_under_the_chip_while_it_loads(self) -> None:
        """The kind filter never looks at the chat, so stale cards would show until the answer."""
        out = self._run(
            """(async () => {
    const card = { kind: 'deleted', chat: { ref: 'fakeRefOtherChat0001' }, message_id: 1 }
    // The feed of every chat is loaded; the chat menu narrows it.
    changesFeed.value = [card]
    changesNextBefore.value = '2026-01-01T00:00:00'
    openChangesFeedForChat('menu')
    await settle()
    // Closed and opened again for the same chat, its cards stay while it reloads.
    changesFeed.value = [card]
    closeChangesFeed(false)
    openChangesFeedForChat('menu')
    await settle()
    // The chip's cross widens it back to every chat.
    widenChangesFeed()
    await settle()
    // Narrowed again, closed, then the sidebar button opens every chat.
    openChangesFeedForChat('info')
    await settle()
    changesFeed.value = [card]
    closeChangesFeed(false)
    openChangesFeed({ type: 'click' })
    await settle()
    console.log(JSON.stringify(FEED_AT_FETCH))
})();"""
        )
        self.assertEqual(
            [(at["cards"], at["next"]) for at in out],
            [(0, None), (1, None), (0, None), (0, None), (0, None)],
        )

    def _close_focus(self, opened_from: str, *, connected_trigger: bool = False) -> list[str]:
        out = self._run(
            f"""(async () => {{
    mainMenuButton.value = fakeButton('main menu')
    chatMenuButton.value = fakeButton('chat menu')
    infoPanelToggleBtn.value = fakeButton('info button')
    document.activeElement = fakeButton('trigger', {str(connected_trigger).lower()})
    openChangesFeedForChat('{opened_from}')
    await settle()
    CALLS.splice(0)
    closeChangesFeed()
    await settle()
    console.log(JSON.stringify(CALLS.filter(c => c.startsWith('focus:'))))
}})();"""
        )
        return out

    def test_closing_the_chat_s_feed_puts_focus_back_on_the_button_that_opened_it(self) -> None:
        # The menu's item and the info panel are gone once the feed opens, so
        # focus goes to the button that shows them again, not the main menu.
        self.assertEqual(self._close_focus("menu"), ["focus:chat menu"])
        self.assertEqual(self._close_focus("info"), ["focus:info button"])
        # An element that opened it and is still on the page takes it first.
        self.assertEqual(self._close_focus("menu", connected_trigger=True), ["focus:trigger"])


class TestViewerTemplate(unittest.TestCase):
    def test_the_chat_menu_entry_sits_under_edited_messages(self) -> None:
        start = HTML.index('<div v-if="chatMenuOpen" role="group" aria-label="More actions"')
        menu = HTML[start : HTML.index("toggleExpandAllTranscripts", start)]
        self.assertIn("What changed in this chat", menu)
        self.assertLess(menu.index("openEditedOnly"), menu.index("openChangesFeedForChat('menu')"))
        self.assertLess(menu.index("openChangesFeedForChat('menu')"), menu.index("exportChat()"))

    def test_the_chat_menu_button_is_where_focus_returns(self) -> None:
        start = HTML.index('<button ref="chatMenuButton" type="button" @click="chatMenuOpen = !chatMenuOpen"')
        self.assertLess(start, HTML.index("openChangesFeedForChat('menu')"))

    def test_the_info_panel_row_opens_the_feed_and_the_counts_keep_their_modes(self) -> None:
        # A deliberate choice, written down in the viewer docs: the counts open
        # the chat's own Deleted only and Edited only modes, which show each
        # message in the chat around it; the feed has its own row.
        start = HTML.index('<h4 id="info-archive-title"')
        section = HTML[start : HTML.index("</section>", start)]
        self.assertIn("@click=\"openChangesFeedForChat('info')\"", section)
        self.assertIn('@click="openDeletedOnlyFromInfo"', section)
        self.assertIn('@click="openEditedOnlyFromInfo"', section)

    def test_the_feed_shows_the_chat_as_a_chip_that_widens_it(self) -> None:
        start = HTML.index('<div v-if="changesChat" class="deleted-filter-bar"')
        bar = HTML[start : HTML.index("</div>", start)]
        self.assertIn('@click="widenChangesFeed"', bar)
        self.assertIn('aria-pressed="true"', bar)
        self.assertIn("{{ changesChat.title }}", bar)
        self.assertIn("if (changesChat.value) return `Nothing changed in this chat ${period.words}`", HTML)
