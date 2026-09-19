"""Two things 8.12's chat folding left behind, on real engines and under node.

Folding shows a chat several accounts hold once, through one account's copy.
Two consequences were missed:

* **The what-changed feed listed an event twice.** Both accounts' listeners see
  the same deletion in a channel they both hold, so both archive it, a second
  apart, and the feed showed both. Folding it by CHAT copy would be wrong: an
  event only exists in the copy whose listener was up when it happened, so
  dropping the other copy's rows loses the event outright whenever the
  displayed account missed it. The identity that matters is the event.

* **The reader's own message looked like a stranger's.** ``is_outgoing`` is
  written per copy, from the point of view of the account that captured it, so
  in a chat displayed through one account the other account's messages arrive
  with ``is_outgoing=0`` and rendered on the left, with an avatar and a full
  name. They are the reader's own messages.

The adapter half runs on ``real_adapter`` so the deduplication predicate is
compiled and executed by SQLite and PostgreSQL. The viewer half runs the real
``isOwnMessage`` under node, lifted out of the shipped template, so it pins
behaviour rather than source text.
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_followups_"))

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_frontend_audit_fixes import INDEX_HTML, _extract_const_arrow_function, _run_node

from src.db.adapter import ChatScope
from src.db.models import MessageVersion
from src.web import main as web_main

BASE = datetime(2026, 4, 1, 12, 0, 0)

UNRESTRICTED = ChatScope.build()
ONLY_ACCOUNT_1 = ChatScope.build(accounts={1})
ONLY_ACCOUNT_2 = ChatScope.build(accounts={2})

# A channel both accounts hold, and a one-to-one chat whose id collides across
# them. Distinctive on purpose so a failure names itself.
SHARED_CHANNEL = -1004200001
COLLIDING_PRIVATE = 420000002

# Two logged-in accounts. Obviously fake ids and labels.
OWNER_A = 420009001
OWNER_B = 420009002
OUTSIDER = 420009003


async def seed_accounts(adapter, rows: list[tuple[int, str | None, int | None]]) -> None:
    """Insert ``accounts`` rows directly: id, label, telegram user id."""
    async with adapter.db_manager.async_session_factory() as session:
        for account_id, label, telegram_user_id in rows:
            await session.execute(
                text("INSERT INTO accounts (id, label, telegram_user_id) VALUES (:id, :label, :owner)"),
                {"id": account_id, "label": label, "owner": telegram_user_id},
            )
        await session.commit()


async def seed_shared_chats(adapter) -> None:
    """Both accounts holding one channel, and each its own conversation."""
    for account_id in (1, 2):
        await adapter.upsert_chat(
            {"id": SHARED_CHANNEL, "type": "channel", "title": "shared channel"}, account_id=account_id
        )
        await adapter.upsert_chat(
            {"id": COLLIDING_PRIVATE, "type": "private", "title": "a conversation"}, account_id=account_id
        )
        for chat_id, message_id in ((SHARED_CHANNEL, 11), (COLLIDING_PRIVATE, 22)):
            await adapter.insert_message(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "sender_id": OUTSIDER,
                    "date": BASE,
                    "text": "original text",
                    "raw_data": {},
                },
                account_id=account_id,
            )


async def mark_deleted(adapter, *, account_id: int, chat_id: int, message_id: int, at: datetime) -> None:
    """Soft-delete one account's copy, the way the listener records it."""
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            text(
                "UPDATE messages SET is_deleted = 1, deleted_at = :at "
                "WHERE account_id = :a AND chat_id = :c AND id = :m"
            ),
            {"at": at, "a": account_id, "c": chat_id, "m": message_id},
        )
        await session.commit()


async def add_version(adapter, *, account_id: int, chat_id: int, message_id: int, old_text: str, at: datetime) -> None:
    """One superseded revision, dated by when the archive observed it."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            MessageVersion(
                account_id=account_id,
                chat_id=chat_id,
                message_id=message_id,
                text=old_text,
                date=BASE,
                captured_at=at,
                change_hash=f"fake-hash-{account_id}-{chat_id}-{message_id}-{old_text}",
            )
        )
        await session.commit()


def kinds(changes) -> list[tuple[str, int, str]]:
    """(kind, message id, chat type) for each row, order preserved."""
    return [(c["kind"], c["message_id"], c["chat"]["type"]) for c in changes]


# ============================================================================
# The what-changed feed
# ============================================================================


class TestChangesFeedDeduplication:
    async def test_one_deletion_captured_by_both_accounts_is_listed_once(self, real_adapter):
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=1, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE + timedelta(seconds=1)
        )

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)

        assert kinds(changes) == [("deleted", 11, "channel")]

    async def test_the_surviving_row_is_the_lowest_entitled_account_s(self, real_adapter):
        """Stable: the same copy the chat list shows, so the feed links where the list does."""
        await seed_shared_chats(real_adapter)
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                text("UPDATE chats SET ref = :r WHERE account_id = 1 AND id = :c"),
                {"r": "followRefA1chan000001", "c": SHARED_CHANNEL},
            )
            await session.execute(
                text("UPDATE chats SET ref = :r WHERE account_id = 2 AND id = :c"),
                {"r": "followRefA2chan000001", "c": SHARED_CHANNEL},
            )
            await session.commit()
        await mark_deleted(real_adapter, account_id=1, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE + timedelta(seconds=1)
        )

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)

        assert [c["chat"]["ref"] for c in changes] == ["followRefA1chan000001"]

    async def test_two_conversations_sharing_an_id_are_two_events(self, real_adapter):
        """A one-to-one chat's id is the other party's user id, not the chat's.

        Two accounts talking to the same person hold two different messages
        under the same (chat id, message id). Deduplicating them would delete
        one account's history from the feed.
        """
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=1, chat_id=COLLIDING_PRIVATE, message_id=22, at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=COLLIDING_PRIVATE, message_id=22, at=BASE + timedelta(seconds=1)
        )

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)

        assert kinds(changes) == [("deleted", 22, "private"), ("deleted", 22, "private")]

    async def test_an_event_only_the_higher_account_captured_survives(self, real_adapter):
        """The reason this deduplicates events instead of folding chat copies.

        The displayed account's listener was down, so only the other account
        holds the deletion. Folding by chat copy would drop it entirely.
        """
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)

        assert kinds(changes) == [("deleted", 11, "channel")]

    async def test_one_edit_captured_by_both_accounts_is_listed_once(self, real_adapter):
        await seed_shared_chats(real_adapter)
        await add_version(
            real_adapter, account_id=1, chat_id=SHARED_CHANNEL, message_id=11, old_text="was this", at=BASE
        )
        await add_version(
            real_adapter,
            account_id=2,
            chat_id=SHARED_CHANNEL,
            message_id=11,
            old_text="was this",
            at=BASE + timedelta(seconds=1),
        )

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)

        assert kinds(changes) == [("edited", 11, "channel")]
        assert changes[0]["old_text"] == "was this"

    async def test_two_edits_of_one_message_stay_two_events(self, real_adapter):
        """The superseded text is the revision's identity, so edits do not collapse."""
        await seed_shared_chats(real_adapter)
        for account_id, offset in ((1, 0), (2, 1)):
            for old_text, extra in (("first draft", 0), ("second draft", 10)):
                await add_version(
                    real_adapter,
                    account_id=account_id,
                    chat_id=SHARED_CHANNEL,
                    message_id=11,
                    old_text=old_text,
                    at=BASE + timedelta(seconds=offset + extra),
                )

        changes = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=50)

        assert kinds(changes) == [("edited", 11, "channel"), ("edited", 11, "channel")]
        assert sorted(c["old_text"] for c in changes) == ["first draft", "second draft"]

    async def test_a_viewer_entitled_to_one_account_sees_its_own_copy(self, real_adapter):
        """Deduplication may never hide a row behind one the reader cannot see."""
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=1, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE + timedelta(seconds=1)
        )

        for scope in (ONLY_ACCOUNT_1, ONLY_ACCOUNT_2):
            changes = await real_adapter.get_recent_changes(scope=scope, limit=50)
            assert kinds(changes) == [("deleted", 11, "channel")], scope

    async def test_the_page_stays_full_so_the_cursor_does_not_stop_early(self, real_adapter):
        """Deduplication happens in SQL, before LIMIT.

        The route stops paging when a page comes back shorter than the limit,
        so halving a page in Python afterwards would strand the rest of the
        feed. Twelve shared deletions both accounts captured must still fill a
        page of ten.
        """
        for account_id in (1, 2):
            await real_adapter.upsert_chat(
                {"id": SHARED_CHANNEL, "type": "channel", "title": "shared channel"}, account_id=account_id
            )
        for index in range(12):
            for account_id in (1, 2):
                await real_adapter.insert_message(
                    {
                        "id": 100 + index,
                        "chat_id": SHARED_CHANNEL,
                        "sender_id": OUTSIDER,
                        "date": BASE,
                        "text": "original text",
                        "raw_data": {},
                    },
                    account_id=account_id,
                )
                await mark_deleted(
                    real_adapter,
                    account_id=account_id,
                    chat_id=SHARED_CHANNEL,
                    message_id=100 + index,
                    at=BASE + timedelta(minutes=index, seconds=account_id),
                )

        page = await real_adapter.get_recent_changes(scope=UNRESTRICTED, limit=10)

        assert len(page) == 10
        assert len({c["message_id"] for c in page}) == 10

    async def test_a_copy_outside_the_window_does_not_suppress_one_inside_it(self, real_adapter):
        """A row is hidden only behind one this same query would list.

        Without the window bounds in the duplicate check, an event whose other
        copy was captured just outside the page would vanish from the page
        rather than be deduplicated.
        """
        await seed_shared_chats(real_adapter)
        await mark_deleted(real_adapter, account_id=1, chat_id=SHARED_CHANNEL, message_id=11, at=BASE)
        await mark_deleted(
            real_adapter, account_id=2, chat_id=SHARED_CHANNEL, message_id=11, at=BASE + timedelta(hours=2)
        )

        changes = await real_adapter.get_recent_changes(since=BASE + timedelta(hours=1), scope=UNRESTRICTED, limit=50)

        assert kinds(changes) == [("deleted", 11, "channel")]


# ============================================================================
# Which account sent it: what the server is willing to say
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
    user = web_main.UserContext(username="followups-test", role=fields.pop("role", "viewer"), **fields)
    web_main.app.dependency_overrides[web_main.require_auth] = lambda: user
    web_main.app.dependency_overrides[web_main.require_master] = lambda: user


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


async def seed_two_speakers(adapter) -> str:
    """One channel both accounts hold, each speaking once. Returns its ref."""
    await seed_accounts(adapter, [(1, "Account A", OWNER_A), (2, "Account B", OWNER_B)])
    for account_id in (1, 2):
        await adapter.upsert_chat(
            {"id": SHARED_CHANNEL, "type": "channel", "title": "shared channel"}, account_id=account_id
        )
        for index, sender_id in enumerate((OWNER_A, OWNER_B, OUTSIDER)):
            await adapter.insert_message(
                {
                    "id": 31 + index,
                    "chat_id": SHARED_CHANNEL,
                    "sender_id": sender_id,
                    "date": BASE + timedelta(minutes=index),
                    "text": "who said this",
                    "raw_data": {},
                },
                account_id=account_id,
            )
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            text("UPDATE chats SET ref = :r WHERE account_id = 1 AND id = :c"),
            {"r": "followRefA1chan000001", "c": SHARED_CHANNEL},
        )
        await session.commit()
    return "followRefA1chan000001"


class TestSenderAccountIsAnEntitlementDecision:
    async def test_a_master_is_told_which_account_spoke(self, app_on):
        chat_ref = await seed_two_speakers(app_on)
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            rows = (await http.get(f"/api/chats/{chat_ref}/messages")).json()

        assert {row["id"]: row["sender_account_id"] for row in rows} == {31: 1, 32: 2, 33: None}

    async def test_a_viewer_granted_one_account_is_not_told_about_the_other(self, app_on):
        """Its own account is named; the other reads as an ordinary participant."""
        chat_ref = await seed_two_speakers(app_on)
        as_principal(allowed_accounts={1})

        async with client() as http:
            resp = await http.get(f"/api/chats/{chat_ref}/messages")

        rows = resp.json()
        assert {row["id"]: row["sender_account_id"] for row in rows} == {31: 1, 32: None, 33: None}
        # Nothing in the payload names the account it may not see.
        assert "Account B" not in resp.text

    def test_the_empty_grant_names_nobody(self):
        """Fail closed, like every other grant in this file.

        An empty account grant cannot reach a chat at all, so this is the rule
        on its own rather than through a route: it must not read as "no
        restriction" the way a falsy collection so often does.
        """
        rows = [{"sender_account_id": 1}, {"sender_account_id": 2}, {"sender_account_id": None}]
        web_main._mask_unentitled_sender_accounts(
            rows, web_main.UserContext(username="v", role="viewer", allowed_accounts=set())
        )

        assert [row["sender_account_id"] for row in rows] == [None, None, None]

    def test_an_unrestricted_principal_is_not_masked_at_all(self):
        rows = [{"sender_account_id": 2}]
        web_main._mask_unentitled_sender_accounts(
            rows, web_main.UserContext(username="m", role="master", allowed_accounts=None)
        )

        assert rows == [{"sender_account_id": 2}]

    async def test_the_mask_reaches_search_hits_too(self, app_on):
        chat_ref = await seed_two_speakers(app_on)
        as_principal(allowed_accounts={1})

        async with client() as http:
            body = (await http.get("/api/search/messages?q=said")).json()

        assert body["results"], "the fixture messages must be findable"
        assert {row["sender_account_id"] for row in body["results"]} <= {1, None}
        assert 2 not in {row["sender_account_id"] for row in body["results"]}
        assert chat_ref  # the ref is the folded copy's; asserted in the 8.12 suite


# ============================================================================
# isOwnMessage, executed
# ============================================================================

PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const selectedChat = ref({ ref: 'c1', type: 'channel', id: -1004200001 });
"""


def _own_message_harness(*, multi_account: bool) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    return "\n".join(
        [
            PRELUDE,
            f"const multiAccount = {{ value: {str(multi_account).lower()} }};",
            _extract_const_arrow_function(html, "isOwnMessage", asynchronous=False) + ";",
        ]
    )


class TestIsOwnMessageFollowsTheSendingAccount:
    def test_the_other_account_s_message_is_mine_even_with_is_outgoing_zero(self):
        """The bug: is_outgoing is written per copy, so the other account reads 0."""
        _run_node(
            _own_message_harness(multi_account=True)
            + """
            assert.equal(isOwnMessage({ is_outgoing: 0, sender_account_id: 2 }), true,
                'a message sent by another entitled account is the reader\\'s own');
            assert.equal(isOwnMessage({ is_outgoing: 1, sender_account_id: 1 }), true,
                'the displayed account\\'s own messages are unchanged');
            """
        )

    def test_a_stranger_is_still_a_stranger(self):
        _run_node(
            _own_message_harness(multi_account=True)
            + """
            assert.equal(isOwnMessage({ is_outgoing: 0, sender_account_id: null }), false,
                'an ordinary participant stays on the left');
            """
        )

    def test_a_masked_account_reads_as_a_stranger(self):
        """The server nulls the field for an account the reader may not see.

        A viewer granted one account must keep reading the other account's
        messages exactly as it did before: left, with a name.
        """
        _run_node(
            _own_message_harness(multi_account=True)
            + """
            assert.equal(isOwnMessage({ is_outgoing: 0, sender_account_id: undefined }), false);
            """
        )

    def test_a_single_account_install_is_driven_by_is_outgoing_alone(self):
        """Nothing changes where there is only one account to be told about."""
        _run_node(
            _own_message_harness(multi_account=False)
            + """
            assert.equal(isOwnMessage({ is_outgoing: 0, sender_account_id: 1 }), false,
                'one account: the flag decides, exactly as before 8.12.1');
            assert.equal(isOwnMessage({ is_outgoing: 1, sender_account_id: 1 }), true);
            """
        )

    def test_the_legacy_private_chat_fallback_still_works(self):
        """No is_outgoing at all (a pre-2.0 row) keeps its sender-id comparison."""
        _run_node(
            PRELUDE
            + """
            const multiAccount = { value: true };
            selectedChat.value = { ref: 'c2', type: 'private', id: 555 };
            """
            + _extract_const_arrow_function(INDEX_HTML.read_text(encoding="utf-8"), "isOwnMessage", asynchronous=False)
            + """;
            assert.equal(isOwnMessage({ sender_id: 555 }), false, 'the other party');
            assert.equal(isOwnMessage({ sender_id: 999 }), true, 'anyone else in a one-to-one chat is me');
            """
        )


class TestRunsStayHomogeneous:
    def test_a_run_never_mixes_own_and_other(self):
        """Run boundaries key on the sender, so own-ness cannot change mid-run.

        This is what keeps the bubble tail, the avatar gutter and the name row
        consistent now that own-ness has a second source: two accounts are two
        sender ids, so they are always two runs.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        script = "\n".join(
            [
                PRELUDE,
                "const getSenderName = msg => msg.sender_name || 'x';",
                _extract_const_arrow_function(html, "getSenderRunKey", asynchronous=False) + ";",
                """
                const fromA = { sender_id: 420009001, sender_account_id: 1, is_outgoing: 1 };
                const fromB = { sender_id: 420009002, sender_account_id: 2, is_outgoing: 0 };
                assert.notEqual(getSenderRunKey(fromA), getSenderRunKey(fromB),
                    'two accounts must never share a run');
                """,
            ]
        )
        _run_node(script)
