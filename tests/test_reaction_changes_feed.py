"""Reactions taken back in What changed, and the period of one chat's feed.

``/api/changes?reactions=true`` adds a ``reaction`` row for every drop the
archive kept in ``reaction_history``: a state whose count is below the one
before it, so a partial drop, a removal and a removal seeded from an old
tombstone all list. Left out unless asked for. The rows ride the feed's own
scope, chat narrowing and cross-account deduplication.

The viewer keeps the kind unticked until the reader asks, and asks the
server for it only then. One chat's feed opens on All time, and a period
picked there is neither remembered nor carried back to every chat.

The adapter and route halves run on ``real_adapter`` (SQLite and PostgreSQL);
the viewer half runs the real setup code under node.
"""

import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_reaction_feed_"))

from test_account_fold_followups import BASE, COLLIDING_PRIVATE, SHARED_CHANNEL, seed_shared_chats
from test_changes_chat_filter import app_on as app_on  # noqa: F401  (pytest fixture)
from test_changes_chat_filter import as_principal, chat_ref, client, seed_two_groups
from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

from telegram_archive.db.adapter import ChatScope
from telegram_archive.db.models import Reaction

UNRESTRICTED = ChatScope.build()
ONLY_ACCOUNT_1 = ChatScope.build(accounts={1})
GROUP_A = -1004300001
GROUP_B = -1004300002


async def _states(adapter, chat_id: int, message_id: int, counts: list[dict], *, account_id: int = 1, start: int = 0):
    """One reconcile per snapshot, a minute apart, as the listener would see them."""
    for minute, observed in enumerate(counts, start=start):
        with patch("telegram_archive.db.adapter.utcnow_naive", return_value=BASE + timedelta(hours=1, minutes=minute)):
            await adapter.reconcile_reactions(
                message_id,
                chat_id,
                [{"emoji": e, "count": n} for e, n in observed.items()],
                account_id=account_id,
                source="listener",
            )


def _reactions(changes) -> list[tuple]:
    return [
        (c["chat"]["title"], c["message_id"], c["emoji"], c["count"], c["count_before"], c["count_after"])
        for c in changes
        if c["kind"] == "reaction"
    ]


class TestTheFeedListsReactionsTakenBack:
    async def test_drops_list_newest_first_and_rises_never(self, real_adapter):
        await seed_two_groups(real_adapter, messages=2)
        await _states(real_adapter, GROUP_A, 1, [{"❤️": 7}, {"❤️": 5}, {"❤️": 6}, {"❤️": 6, "😮": 1}, {"❤️": 6}])

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, with_reactions=True)
        assert _reactions(changes) == [
            ("group a", 1, "😮", 1, 1, 0),
            ("group a", 1, "❤️", 2, 7, 5),
        ]
        reaction = next(c for c in changes if c["kind"] == "reaction")
        assert reaction["date"] == (BASE + timedelta(hours=1, minutes=4)).isoformat()
        assert reaction["text"] == "original text"
        assert reaction["chat"]["type"] == "group"

    async def test_left_out_unless_asked_for(self, real_adapter):
        await seed_two_groups(real_adapter, messages=1)
        await _states(real_adapter, GROUP_A, 1, [{"❤️": 2}, {}])
        assert await real_adapter.get_recent_changes(scope=UNRESTRICTED) == []
        assert len(await real_adapter.get_recent_changes(scope=UNRESTRICTED, with_reactions=True)) == 1

    async def test_a_removal_from_before_the_history_lists_through_its_baseline(self, real_adapter):
        """A tombstone written before 037 becomes a baseline count and a 0 row;
        the drop between them is a card dated by the tombstone."""
        await seed_two_groups(real_adapter, messages=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            session.add(
                Reaction(
                    account_id=1,
                    message_id=1,
                    chat_id=GROUP_A,
                    emoji="👍",
                    count=3,
                    created_at=BASE,
                    removed_at=BASE + timedelta(minutes=30),
                )
            )
            await session.commit()
        await _states(real_adapter, GROUP_A, 1, [{"🔥": 1}])

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, with_reactions=True)
        assert _reactions(changes) == [("group a", 1, "👍", 3, 3, 0)]
        assert changes[0]["date"] == (BASE + timedelta(minutes=30)).isoformat()

    async def test_the_window_and_the_cursor_bound_the_drops(self, real_adapter):
        await seed_two_groups(real_adapter, messages=1)
        await _states(real_adapter, GROUP_A, 1, [{"❤️": 9}, {"❤️": 8}, {"❤️": 7}, {"❤️": 6}])
        since = BASE + timedelta(hours=1, minutes=2)
        before = BASE + timedelta(hours=1, minutes=3)
        windowed = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, with_reactions=True, since=since, before=before
        )
        assert [(c["count_before"], c["count_after"]) for c in windowed] == [(8, 7)]
        paged = await real_adapter.get_recent_changes(scope=UNRESTRICTED, with_reactions=True, limit=2)
        assert [(c["count_before"], c["count_after"]) for c in paged] == [(7, 6), (8, 7)]

    async def test_the_scope_keeps_another_accounts_drops_out(self, real_adapter):
        await seed_shared_chats(real_adapter)
        await _states(real_adapter, COLLIDING_PRIVATE, 22, [{"👍": 2}, {}], account_id=2)
        assert await real_adapter.get_recent_changes(scope=ONLY_ACCOUNT_1, with_reactions=True) == []
        assert len(await real_adapter.get_recent_changes(scope=UNRESTRICTED, with_reactions=True)) == 1

    async def test_a_drop_both_accounts_saw_in_a_shared_channel_lists_once(self, real_adapter):
        await seed_shared_chats(real_adapter)
        await _states(real_adapter, SHARED_CHANNEL, 11, [{"👍": 4}, {"👍": 1}], account_id=1)
        await _states(real_adapter, SHARED_CHANNEL, 11, [{"👍": 4}, {"👍": 1}], account_id=2, start=10)
        # Account 2 alone saw a later drop.
        await _states(real_adapter, SHARED_CHANNEL, 11, [{}], account_id=2, start=20)

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, with_reactions=True)
        refs = {
            account_id: await chat_ref(real_adapter, SHARED_CHANNEL, account_id=account_id) for account_id in (1, 2)
        }
        assert [(c["chat"]["ref"], c["count_before"], c["count_after"]) for c in changes] == [
            (refs[2], 1, 0),
            (refs[1], 4, 1),
        ]

    async def test_one_chat_narrows_the_drops_and_a_private_chat_its_account(self, real_adapter):
        await seed_two_groups(real_adapter, messages=1)
        await seed_shared_chats(real_adapter)
        await _states(real_adapter, GROUP_A, 1, [{"❤️": 2}, {}])
        await _states(real_adapter, GROUP_B, 1, [{"❤️": 2}, {}])
        for account_id in (1, 2):
            await _states(real_adapter, COLLIDING_PRIVATE, 22, [{"🎉": account_id}, {}], account_id=account_id)

        only_a = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, with_reactions=True, chat_id=GROUP_A, account_id=1
        )
        assert _reactions(only_a) == [("group a", 1, "❤️", 2, 2, 0)]
        private = await real_adapter.get_recent_changes(
            scope=UNRESTRICTED, with_reactions=True, chat_id=COLLIDING_PRIVATE, account_id=2
        )
        assert [(c["emoji"], c["count"]) for c in private] == [("🎉", 2)]


class TestTheRoute:
    async def test_reactions_true_adds_them_inside_the_viewers_scope(self, app_on):  # noqa: F811
        await seed_shared_chats(app_on)
        await _states(app_on, COLLIDING_PRIVATE, 22, [{"👍": 2}, {}], account_id=2)
        ref_2 = await chat_ref(app_on, COLLIDING_PRIVATE, account_id=2)

        async with client() as http:
            as_principal(role="master")
            plain = await http.get("/api/changes")
            asked = await http.get("/api/changes", params={"reactions": "true"})
            narrowed = await http.get("/api/changes", params={"reactions": "true", "chat_ref": ref_2})
            as_principal(allowed_accounts={1})
            restricted = await http.get("/api/changes", params={"reactions": "true"})
            hidden_chat = await http.get("/api/changes", params={"reactions": "true", "chat_ref": ref_2})

        assert plain.json()["changes"] == []
        assert [(c["kind"], c["emoji"], c["count"]) for c in asked.json()["changes"]] == [("reaction", "👍", 2)]
        assert [c["kind"] for c in narrowed.json()["changes"]] == ["reaction"]
        assert restricted.json()["changes"] == []
        assert hidden_chat.status_code == 404


# ============================================================================
# The viewer
# ============================================================================

HTML = INDEX_HTML.read_text(encoding="utf-8")

_PRELUDE = """
const ref = (value) => ({ value })
const computed = (fn) => ({ get value() { return fn() } })
const nextTick = (fn) => Promise.resolve().then(fn)
const STORE = {}
const localStorage = {
    getItem: (key) => (key in STORE ? STORE[key] : null),
    setItem: (key, value) => { STORE[key] = String(value) },
}
const URLS = []
const fetch = async (url) => {
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
const shownChangeCount = () => 0
const isAuthenticated = ref(true)
const loadMoreChangesIfInView = () => {}
const markChangesSeen = () => {}
const handleChangesKeydown = () => {}
const mainMenuButton = ref(null)
const selectedChat = ref({ ref: 'fakeRefGroupA0000001', title: 'Fixture Group' })
const selectedPaneTopic = ref(null)
const messagesContainer = ref(null)
const showInfoPanel = ref(false)
const changesTitle = ref({ focus: () => {} })
const chatMenuOpen = ref(false)
const closeInfoPanel = () => {}
const getChatName = (chat) => chat.title
const infoPanelToggleBtn = ref(null)
const chatMenuButton = ref(null)
const noDownload = ref(false)
const formatCount = (n) => String(n)
const formatReactionEmoji = (emoji) => emoji
const settle = () => new Promise(r => setTimeout(r, 0))
const sinceOf = (url) => new URLSearchParams(url.split('?')[1]).get('since')
const params = (url) => Object.fromEntries(new URLSearchParams(url.split('?')[1]))
"""

_DECLARATIONS = (
    "const CHANGE_PERIODS = [",
    "const readStored = (key, fallback) =>",
    "const storedPeriod = readStored(",
    "const changesFeedPeriod = ref(",
    "const changesSince = ref(",
    "const CHANGE_KIND_DEFAULTS = ",
    "const changesKinds = ref(",
    "const changesSinceIso = () =>",
    "const fetchChanges = async (before) =>",
    "let changesFeedChatRef = null",
    "const setChangesChat = (chat) =>",
    "const openChangesFeed = (fromMenu = false, chat = null, returnFocus = null) =>",
    "const openChangesFeedForChat = (from) =>",
    "const widenChangesFeed = () =>",
    "const toggleChangeKind = (kind) =>",
    "const setChangesPeriod = (value) =>",
    "const changeKindOptions = computed(",
    "const changesFilterActive = computed(",
    "const changeReactionText = (card) =>",
)


def _program(epilogue: str, stored: dict | None = None) -> object:
    prelude = _PRELUDE + f"Object.assign(STORE, {json.dumps(stored or {})})\n"
    return _run_setup_program(HTML, _DECLARATIONS, prelude, epilogue)


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheReactionKind(unittest.TestCase):
    def test_it_is_unticked_until_the_reader_asks(self) -> None:
        out = _program(
            "console.log(JSON.stringify({ kinds: changesKinds.value, active: changesFilterActive.value,"
            " labels: changeKindOptions.value.map(k => k.label) }))"
        )
        self.assertEqual(out["kinds"], {"deleted": True, "edited": True, "transcript": True, "reaction": False})
        self.assertFalse(out["active"])
        self.assertEqual(out["labels"], ["Deleted", "Edited", "Transcripts", "Reactions taken back"])

    def test_a_saved_choice_is_kept_and_an_older_save_leaves_it_off(self) -> None:
        on = _program(
            "console.log(JSON.stringify([changesKinds.value, changesFilterActive.value]))",
            {"changesKinds": json.dumps({"deleted": True, "edited": False, "transcript": True, "reaction": True})},
        )
        self.assertEqual(on[0], {"deleted": True, "edited": False, "transcript": True, "reaction": True})
        self.assertTrue(on[1])
        older = _program(
            "console.log(JSON.stringify(changesKinds.value))",
            {"changesKinds": json.dumps({"deleted": True, "edited": True, "transcript": False})},
        )
        self.assertEqual(older, {"deleted": True, "edited": True, "transcript": False, "reaction": False})

    def test_ticking_it_asks_the_server_for_them_and_remembers(self) -> None:
        out = _program(
            """(async () => {
    openChangesFeed({ type: 'click' })
    await settle()
    toggleChangeKind('reaction')
    await settle()
    toggleChangeKind('deleted')
    await settle()
    console.log(JSON.stringify({ urls: URLS.map(params), saved: JSON.parse(STORE.changesKinds) }))
})();"""
        )
        self.assertNotIn("reactions", out["urls"][0])
        self.assertEqual(out["urls"][1].get("reactions"), "true")
        # Another kind is filtered on the page: no new request.
        self.assertEqual(len(out["urls"]), 2)
        self.assertTrue(out["saved"]["reaction"])

    def test_the_chip_says_how_many_went_and_of_how_many(self) -> None:
        out = _program(
            "console.log(JSON.stringify([changeReactionText({ count: 2, countBefore: 7 }),"
            " changeReactionText({ count: 1, countBefore: 1 })]))"
        )
        self.assertEqual(out, ["2 of 7", "1"])


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestOneChatsPeriod(unittest.TestCase):
    def test_a_chats_feed_opens_on_all_time_whatever_the_feed_remembers(self) -> None:
        out = _program(
            """(async () => {
    openChangesFeedForChat('menu')
    await settle()
    console.log(JSON.stringify({ since: changesSince.value, url: params(URLS[0]) }))
})();""",
            {"changesPeriod": "1"},
        )
        self.assertEqual(out["since"], "")
        self.assertNotIn("since", out["url"])
        self.assertEqual(out["url"]["chat_ref"], "fakeRefGroupA0000001")

    def test_a_period_picked_in_a_chats_feed_is_not_remembered(self) -> None:
        out = _program(
            """(async () => {
    openChangesFeedForChat('menu')
    await settle()
    setChangesPeriod('30')
    await settle()
    const inChat = { since: changesSince.value, stored: STORE.changesPeriod ?? null, hasSince: sinceOf(URLS[1]) !== null }
    widenChangesFeed()
    await settle()
    const widened = { since: changesSince.value, stored: STORE.changesPeriod ?? null }
    // Opened again for the chat: All time again, not the 30 days picked before.
    openChangesFeedForChat('menu')
    await settle()
    console.log(JSON.stringify({ inChat, widened, reopened: changesSince.value }))
})();""",
            {"changesPeriod": "1"},
        )
        self.assertEqual(out["inChat"], {"since": "30", "stored": "1", "hasSince": True})
        self.assertEqual(out["widened"], {"since": "1", "stored": "1"})
        self.assertEqual(out["reopened"], "")

    def test_the_feed_of_every_chat_still_remembers_its_period(self) -> None:
        out = _program(
            """(async () => {
    openChangesFeed({ type: 'click' })
    await settle()
    const opened = changesSince.value
    setChangesPeriod('30')
    await settle()
    openChangesFeedForChat('menu')
    await settle()
    widenChangesFeed()
    await settle()
    console.log(JSON.stringify({ opened, stored: STORE.changesPeriod, back: changesSince.value }))
})();""",
            {"changesPeriod": "1"},
        )
        self.assertEqual(out, {"opened": "1", "stored": "30", "back": "30"})
