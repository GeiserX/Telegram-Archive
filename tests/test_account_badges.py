"""Account badges and chat folding: one row per shared chat, named by account.

Since 8.0 several Telegram accounts archive into one database, so the same
channel subscribed from two accounts is two ``chats`` rows with the same id and
different refs — and the chat list showed it twice with nothing to say which
account either copy came from.

8.12 folds those copies and labels them. This file pins the rule, because every
part of it is a decision that can silently go the wrong way:

* A non-private chat several ENTITLED accounts hold is listed once, through its
  LOWEST entitled account. Lowest because the surviving copy owns the ref the
  viewer deep-links and subscribes with, and that ref must not move as messages
  arrive.
* A private chat is NEVER folded. ``chats.id`` is the other party's user id
  there, so the two rows are two different conversations that merely collide on
  an id — merging them would invent a thread.
* Folding sees only what the principal may see. A viewer entitled to account 2
  alone keeps account 2's copy and a single-element ``accounts`` list: nothing
  of its view may depend on a row it has no right to.
* ``total`` and ``has_more`` count folded rows. Counting the unfolded ones
  promises pages that do not exist.
* ``/api/accounts`` answers id and label and nothing else. The third column on
  that table, ``telegram_user_id``, is PII.

The adapter half runs on ``real_adapter``, so the fold predicate is compiled and
executed by SQLite AND PostgreSQL rather than asserted against a mock.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_badges_"))

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from src.db.adapter import ChatScope
from src.web import main as web_main

BASE = datetime(2026, 3, 1, 10, 0, 0)

UNRESTRICTED = ChatScope.build()
ONLY_ACCOUNT_1 = ChatScope.build(accounts={1})
ONLY_ACCOUNT_2 = ChatScope.build(accounts={2})
ACCOUNTS_1_AND_2 = ChatScope.build(accounts={1, 2})

# A channel and a legacy group, each archived by BOTH accounts, plus a private
# chat whose id collides across accounts and a channel only account 2 has. The
# ids are distinctive so an assertion failure is unambiguous.
SHARED_CHANNEL = -1008100001
SHARED_GROUP = -8100002
COLLIDING_PRIVATE = 810000003
ACCOUNT_2_ONLY = -1008100004

UNIVERSE = [
    (1, SHARED_CHANNEL, "channel", "badgeRefA1chan0000001"),
    (2, SHARED_CHANNEL, "channel", "badgeRefA2chan0000001"),
    (1, SHARED_GROUP, "group", "badgeRefA1grp00000002"),
    (2, SHARED_GROUP, "group", "badgeRefA2grp00000002"),
    (1, COLLIDING_PRIVATE, "private", "badgeRefA1priv0000003"),
    (2, COLLIDING_PRIVATE, "private", "badgeRefA2priv0000003"),
    (2, ACCOUNT_2_ONLY, "channel", "badgeRefA2chan0000004"),
]


async def seed_universe(adapter) -> None:
    """Seed the chat rows above with pinned refs and one message each."""
    for index, (account_id, chat_id, chat_type, _ref) in enumerate(UNIVERSE):
        await adapter.upsert_chat(
            {"id": chat_id, "type": chat_type, "title": f"badge fixture {chat_id}"}, account_id=account_id
        )
        await adapter.insert_message(
            {
                "id": 5000 + index,
                "chat_id": chat_id,
                "sender_id": 4242,
                "date": BASE + timedelta(minutes=index),
                "text": "badge fixture message",
                "raw_data": {},
            },
            account_id=account_id,
        )
    # upsert_chat mints a random ref; pin them so the assertions can name refs.
    async with adapter.db_manager.async_session_factory() as session:
        for account_id, chat_id, _type, ref in UNIVERSE:
            await session.execute(
                text("UPDATE chats SET ref = :ref WHERE account_id = :a AND id = :c"),
                {"ref": ref, "a": account_id, "c": chat_id},
            )
        await session.commit()


@pytest.fixture
async def seeded_adapter(real_adapter):
    await seed_universe(real_adapter)
    return real_adapter


def keyed(rows) -> dict[int, dict]:
    """Rows by chat id — only valid where no id appears twice."""
    return {row["id"]: row for row in rows}


# ============================================================================
# The fold rule, compiled and executed by both backends
# ============================================================================


class TestFolding:
    async def test_shared_channel_and_group_fold_to_the_lowest_entitled_account(self, seeded_adapter):
        """Both non-private types fold, and the copy that survives is account 1's."""
        rows = await seeded_adapter.get_all_chats(scope=ACCOUNTS_1_AND_2, fold_shared=True)
        by_id = keyed([row for row in rows if row["type"] != "private"])

        assert by_id[SHARED_CHANNEL]["account_id"] == 1
        assert by_id[SHARED_CHANNEL]["ref"] == "badgeRefA1chan0000001"
        assert by_id[SHARED_CHANNEL]["accounts"] == [1, 2]

        assert by_id[SHARED_GROUP]["account_id"] == 1
        assert by_id[SHARED_GROUP]["ref"] == "badgeRefA1grp00000002"
        assert by_id[SHARED_GROUP]["accounts"] == [1, 2]

        # The chat only one account holds is untouched and says so.
        assert by_id[ACCOUNT_2_ONLY]["account_id"] == 2
        assert by_id[ACCOUNT_2_ONLY]["accounts"] == [2]

    async def test_private_chats_with_the_same_id_are_never_folded(self, seeded_adapter):
        """Two accounts' conversations with the same person stay two conversations."""
        rows = await seeded_adapter.get_all_chats(scope=ACCOUNTS_1_AND_2, fold_shared=True)
        privates = [row for row in rows if row["type"] == "private"]

        assert len(privates) == 2
        assert sorted(row["account_id"] for row in privates) == [1, 2]
        assert sorted(row["ref"] for row in privates) == ["badgeRefA1priv0000003", "badgeRefA2priv0000003"]
        # Each names ONLY its own account: the other copy is a different chat.
        assert {row["account_id"]: row["accounts"] for row in privates} == {1: [1], 2: [2]}

    async def test_a_private_chat_never_folds_behind_a_non_private_one(self, real_adapter):
        """The rule is "this row is private", not "the other row is private".

        The inner half of the predicate already refuses to fold behind a
        private copy. The outer half is what protects a private chat from
        being folded away by a NON-private chat that happens to carry the same
        id in a lower account — the one arrangement where the two halves
        disagree, and the reason the outer clause exists.
        """
        collision = 810009999
        await real_adapter.upsert_chat({"id": collision, "type": "group", "title": "group copy"}, account_id=1)
        await real_adapter.upsert_chat({"id": collision, "type": "private", "title": "dm copy"}, account_id=2)

        rows = await real_adapter.get_all_chats(scope=ACCOUNTS_1_AND_2, fold_shared=True)

        assert sorted((row["account_id"], row["type"]) for row in rows) == [(1, "group"), (2, "private")]
        # The private row names its own account, never the group's.
        assert next(row for row in rows if row["type"] == "private")["accounts"] == [2]

    async def test_an_account_2_viewer_sees_its_own_copies_and_one_badge(self, seeded_adapter):
        """Nothing a restricted viewer sees may depend on a row it may not see."""
        rows = await seeded_adapter.get_all_chats(scope=ONLY_ACCOUNT_2, fold_shared=True)

        assert {row["account_id"] for row in rows} == {2}
        assert all(row["accounts"] == [2] for row in rows)
        # Same rows it saw before folding existed, and the same count.
        unfolded = await seeded_adapter.get_all_chats(scope=ONLY_ACCOUNT_2)
        assert sorted(row["ref"] for row in rows) == sorted(row["ref"] for row in unfolded)

    async def test_an_account_1_viewer_sees_only_its_own_accounts_badge(self, seeded_adapter):
        rows = await seeded_adapter.get_all_chats(scope=ONLY_ACCOUNT_1, fold_shared=True)
        assert all(row["accounts"] == [1] for row in rows)
        assert ACCOUNT_2_ONLY not in {row["id"] for row in rows}

    async def test_a_ref_grant_naming_only_the_higher_copy_keeps_it(self, seeded_adapter):
        """The fold may only hide behind a copy the grant actually reaches.

        A viewer granted account 2's copy of the shared channel and nothing
        else must still see it. Account 1's copy is lower, but it is not in
        this viewer's scope, so it cannot be what the chat folds into.
        """
        scope = ChatScope.build(refs={"badgeRefA2chan0000001"})
        rows = await seeded_adapter.get_all_chats(scope=scope, fold_shared=True)

        assert len(rows) == 1
        assert rows[0]["ref"] == "badgeRefA2chan0000001"
        assert rows[0]["accounts"] == [2]

    async def test_unfolded_reads_are_untouched(self, seeded_adapter):
        """The admin chat picker still gets every copy, with no accounts field."""
        rows = await seeded_adapter.get_all_chats()
        assert len(rows) == len(UNIVERSE)
        assert all("accounts" not in row for row in rows)


class TestCountAgreesWithTheRows:
    @pytest.mark.parametrize(
        "scope",
        [UNRESTRICTED, ONLY_ACCOUNT_1, ONLY_ACCOUNT_2, ACCOUNTS_1_AND_2],
        ids=["unrestricted", "account-1", "account-2", "accounts-1-2"],
    )
    async def test_count_matches_the_folded_page(self, seeded_adapter, scope):
        rows = await seeded_adapter.get_all_chats(scope=scope, fold_shared=True)
        total = await seeded_adapter.get_chat_count(scope=scope, fold_shared=True)
        assert total == len(rows)

    async def test_the_unfolded_count_is_larger(self, seeded_adapter):
        """Positive control: the count really does change when folding is on."""
        folded = await seeded_adapter.get_chat_count(scope=ACCOUNTS_1_AND_2, fold_shared=True)
        unfolded = await seeded_adapter.get_chat_count(scope=ACCOUNTS_1_AND_2)
        assert unfolded == len(UNIVERSE)
        assert folded == unfolded - 2  # the channel copy and the group copy


class TestGlobalSearchDedupe:
    async def test_a_shared_channel_answers_once_through_the_displayed_copy(self, seeded_adapter):
        """Both copies hold the same channel message; a search must return one.

        And it must be the copy the chat list shows, because that is the only
        ``chat_ref`` whose row the sidebar has.
        """
        for account_id in (1, 2):
            await seeded_adapter.insert_message(
                {
                    "id": 6001,
                    "chat_id": SHARED_CHANNEL,
                    "sender_id": 4242,
                    "date": BASE + timedelta(hours=1),
                    "text": "quokka announcement",
                    "raw_data": {},
                },
                account_id=account_id,
            )

        both = await seeded_adapter.search_messages_global("quokka", scope=UNRESTRICTED)
        assert len(both["results"]) == 2  # positive control: the duplicate is real

        folded = await seeded_adapter.search_messages_global("quokka", scope=UNRESTRICTED, fold_shared=True)
        assert len(folded["results"]) == 1
        assert folded["results"][0]["chat_ref"] == "badgeRefA1chan0000001"

    async def test_private_chats_are_not_deduped_by_search(self, seeded_adapter):
        """Two accounts' conversations with the same person are two results."""
        for account_id in (1, 2):
            await seeded_adapter.insert_message(
                {
                    "id": 6002,
                    "chat_id": COLLIDING_PRIVATE,
                    "sender_id": 4242,
                    "date": BASE + timedelta(hours=2),
                    "text": "wombat plans",
                    "raw_data": {},
                },
                account_id=account_id,
            )

        folded = await seeded_adapter.search_messages_global("wombat", scope=UNRESTRICTED, fold_shared=True)
        assert len(folded["results"]) == 2
        assert sorted(row["chat_ref"] for row in folded["results"]) == [
            "badgeRefA1priv0000003",
            "badgeRefA2priv0000003",
        ]


# ============================================================================
# The routes
# ============================================================================


@pytest.fixture
async def app_on(real_adapter):
    """Point the real FastAPI app at a real adapter, and put it back afterwards."""
    saved = (
        web_main.db,
        web_main.AUTH_ENABLED,
        web_main.ALLOW_ANONYMOUS_VIEWER,
        web_main.config.display_chat_ids,
    )
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
    """Run the next requests as a specific principal."""
    user = web_main.UserContext(username="badge-test", role=fields.pop("role", "viewer"), **fields)
    web_main.app.dependency_overrides[web_main.require_auth] = lambda: user
    web_main.app.dependency_overrides[web_main.require_master] = lambda: user


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


async def seed_accounts(adapter, rows: list[tuple[int, str | None, int | None]]) -> None:
    """Insert ``accounts`` rows directly — labels and (unused) telegram user ids."""
    async with adapter.db_manager.async_session_factory() as session:
        for account_id, label, telegram_user_id in rows:
            await session.execute(
                text("INSERT INTO accounts (id, label, telegram_user_id) VALUES (:id, :label, :telegram_user_id)"),
                {"id": account_id, "label": label, "telegram_user_id": telegram_user_id},
            )
        await session.commit()


class TestAccountsEndpoint:
    async def test_lists_id_and_label_and_never_the_telegram_user_id(self, app_on):
        await seed_accounts(app_on, [(1, "first", 555000111), (2, "second", 555000222)])
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            resp = await http.get("/api/accounts")

        assert resp.status_code == 200
        assert resp.json() == {"accounts": [{"id": 1, "label": "first"}, {"id": 2, "label": "second"}]}
        # The PII column must not reach the wire under ANY key name.
        assert "555000111" not in resp.text
        assert "telegram_user_id" not in resp.text

    async def test_a_null_label_falls_back_to_the_account_id(self, app_on):
        await seed_accounts(app_on, [(1, None, None)])
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            resp = await http.get("/api/accounts")

        assert resp.json() == {"accounts": [{"id": 1, "label": "account 1"}]}

    async def test_the_account_grant_narrows_the_list(self, app_on):
        await seed_accounts(app_on, [(1, "first", None), (2, "second", None), (3, "third", None)])
        as_principal(allowed_accounts={2})

        async with client() as http:
            resp = await http.get("/api/accounts")

        assert resp.json() == {"accounts": [{"id": 2, "label": "second"}]}

    async def test_an_empty_grant_lists_nothing(self, app_on):
        """The empty grant denies here exactly as it denies in the chat list."""
        await seed_accounts(app_on, [(1, "first", None), (2, "second", None)])
        as_principal(allowed_accounts=set())

        async with client() as http:
            resp = await http.get("/api/accounts")

        assert resp.json() == {"accounts": []}


class TestChatsEndpointFolds:
    async def test_total_and_has_more_describe_the_folded_rows(self, app_on):
        await seed_universe(app_on)
        await seed_accounts(app_on, [(1, "one", None), (2, "two", None)])
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            first = (await http.get("/api/chats?limit=3&offset=0")).json()
            second = (await http.get("/api/chats?limit=3&offset=3")).json()

        # 7 rows in the universe, 2 folded away.
        assert first["total"] == 5
        assert first["has_more"] is True
        assert len(first["chats"]) == 3
        assert second["has_more"] is False
        assert len(second["chats"]) == 2

        refs = [row["ref"] for row in first["chats"] + second["chats"]]
        assert len(refs) == len(set(refs)) == 5
        assert "badgeRefA2chan0000001" not in refs  # the hidden channel copy
        assert "badgeRefA2grp00000002" not in refs  # the hidden group copy

    async def test_every_row_carries_its_accounts(self, app_on):
        await seed_universe(app_on)
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            rows = (await http.get("/api/chats?limit=50")).json()["chats"]

        by_ref = {row["ref"]: row["accounts"] for row in rows}
        assert by_ref["badgeRefA1chan0000001"] == [1, 2]
        assert by_ref["badgeRefA1grp00000002"] == [1, 2]
        assert by_ref["badgeRefA2chan0000004"] == [2]
        assert by_ref["badgeRefA1priv0000003"] == [1]
        assert by_ref["badgeRefA2priv0000003"] == [2]

    async def test_a_single_account_viewer_sees_what_it_saw_before(self, app_on):
        await seed_universe(app_on)
        as_principal(allowed_accounts={2})

        async with client() as http:
            payload = (await http.get("/api/chats?limit=50")).json()

        assert payload["total"] == 4
        assert {row["account_id"] for row in payload["chats"]} == {2}
        assert all(row["accounts"] == [row["account_id"]] for row in payload["chats"])

    async def test_the_admin_chat_picker_still_lists_every_copy(self, app_on):
        """It edits per-copy grants, so folding there would hide grantable rows."""
        await seed_universe(app_on)
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            rows = (await http.get("/api/admin/chats")).json()["chats"]

        assert len(rows) == len(UNIVERSE)
        assert "badgeRefA2chan0000001" in {row["ref"] for row in rows}


class TestAdminAccountGrantRoundTrip:
    async def test_create_and_update_round_trip_allowed_accounts(self, app_on):
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            created = await http.post(
                "/api/admin/viewers",
                json={
                    "username": "badge-viewer",
                    "password": "viewer@test/value",
                    "allowed_accounts": [2, 1],
                },
            )
            assert created.status_code == 200
            # Stored sorted, so the grant reads the same however it was sent.
            assert created.json()["allowed_accounts"] == [1, 2]
            viewer_id = created.json()["id"]

            listed = (await http.get("/api/admin/viewers")).json()["viewers"]
            assert [v["allowed_accounts"] for v in listed if v["id"] == viewer_id] == [[1, 2]]

            narrowed = await http.put(f"/api/admin/viewers/{viewer_id}", json={"allowed_accounts": [2]})
            assert narrowed.status_code == 200
            assert narrowed.json()["allowed_accounts"] == [2]

            widened = await http.put(f"/api/admin/viewers/{viewer_id}", json={"allowed_accounts": None})
            assert widened.status_code == 200
            assert widened.json()["allowed_accounts"] is None

            denied = await http.put(f"/api/admin/viewers/{viewer_id}", json={"allowed_accounts": []})
            assert denied.status_code == 200
            # [] is a real grant meaning "nothing", never "unset".
            assert denied.json()["allowed_accounts"] == []

    async def test_the_partial_put_leaves_the_chat_grant_alone(self, app_on):
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            created = await http.post(
                "/api/admin/viewers",
                json={
                    "username": "badge-viewer-2",
                    "password": "viewer@test/value",
                    "allowed_chat_refs": ["badgeRefA1chan0000001"],
                },
            )
            viewer_id = created.json()["id"]
            updated = await http.put(f"/api/admin/viewers/{viewer_id}", json={"allowed_accounts": [1]})

        assert updated.json()["allowed_chat_refs"] == ["badgeRefA1chan0000001"]
        assert updated.json()["allowed_accounts"] == [1]

    async def test_a_stored_account_grant_reaches_the_session(self, app_on):
        """The round trip is only worth anything if the stored grant is read back."""
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            created = await http.post(
                "/api/admin/viewers",
                json={
                    "username": "badge-viewer-3",
                    "password": "viewer@test/value",
                    "allowed_accounts": [2],
                },
            )
        row = await app_on.get_viewer_account(created.json()["id"])
        assert json.loads(row["allowed_accounts"]) == [2]
        accounts, _refs = web_main._grants_from_row(row)
        assert accounts == {2}
