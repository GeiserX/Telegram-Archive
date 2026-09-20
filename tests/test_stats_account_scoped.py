"""A restricted login's statistics, computed from its own account's rows.

The cached statistics blob's per-chat map was grouped by ``chat_id`` alone, so
one entry held the SUM across every account's copy of that id. ``/api/stats``
filtered that map by bare chat id and recomputed ``chats`` and ``messages``
from it, so a viewer entitled to one account read totals that included the
other account's messages — and for a one-to-one chat, whose id is the other
party's user id, it summed two entirely unrelated conversations.

What this pins:

* The map is keyed by ``(account, chat)``, and a restricted principal's totals
  come from its own keys only.
* Fail closed, twice over. An absent or unreadable map scopes to zeros rather
  than to the archive-wide numbers, and so does a blob written before this
  change: its bare-id keys cannot say which account a count belongs to, so
  they are refused rather than guessed at.
* The archive-wide totals are untouched. They count rows, across every
  account, because they are storage figures rather than a reading view.
* Folder counts take the same key, so a folder is not credited with the other
  account's membership rows.

The adapter half runs on ``real_adapter``, so the grouping and the tuple
predicate are compiled and executed by SQLite and PostgreSQL.
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_stats_"))

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from src.db.adapter import (
    LEGACY_CHAT_COUNTS_KEY,
    PER_ACCOUNT_CHAT_COUNTS_KEY,
    ChatScope,
    account_chat_stats_key,
    parse_account_chat_stats_key,
)
from src.web import main as web_main

BASE = datetime(2026, 5, 1, 9, 0, 0)

# A channel both accounts hold, and a one-to-one chat whose id collides across
# them. Message counts differ per account so a leak is unmistakable.
SHARED_CHANNEL = -1005100001
COLLIDING_PRIVATE = 510000002

# (account, chat) -> how many messages that account archived there.
SEEDED = {
    (1, SHARED_CHANNEL): 2,
    (1, COLLIDING_PRIVATE): 1,
    (2, SHARED_CHANNEL): 5,
    (2, COLLIDING_PRIVATE): 4,
}
ACCOUNT_ONE_MESSAGES = 3  # 2 + 1
ACCOUNT_TWO_MESSAGES = 9  # 5 + 4
ALL_MESSAGES = 12

REF_OF = {
    (1, SHARED_CHANNEL): "statsRefA1chan000001",
    (1, COLLIDING_PRIVATE): "statsRefA1priv000002",
    (2, SHARED_CHANNEL): "statsRefA2chan000001",
    (2, COLLIDING_PRIVATE): "statsRefA2priv000002",
}


async def seed_two_accounts(adapter) -> None:
    """Both accounts holding the same two chat ids, with different histories."""
    for (account_id, chat_id), count in SEEDED.items():
        await adapter.upsert_chat(
            {
                "id": chat_id,
                "type": "channel" if chat_id < 0 else "private",
                "title": "stats fixture",
            },
            account_id=account_id,
        )
        for index in range(count):
            await adapter.insert_message(
                {
                    "id": 1000 * account_id + index,
                    "chat_id": chat_id,
                    "sender_id": 510009001,
                    "date": BASE + timedelta(minutes=index),
                    "text": "stats fixture message",
                    "raw_data": {},
                },
                account_id=account_id,
            )
    async with adapter.db_manager.async_session_factory() as session:
        for (account_id, chat_id), ref in REF_OF.items():
            await session.execute(
                text("UPDATE chats SET ref = :r WHERE account_id = :a AND id = :c"),
                {"r": ref, "a": account_id, "c": chat_id},
            )
        await session.commit()


# ============================================================================
# The cached blob and the key it is addressed by
# ============================================================================


class TestTheKeyCarriesItsAccount:
    def test_a_key_round_trips(self):
        assert parse_account_chat_stats_key(account_chat_stats_key(2, SHARED_CHANNEL)) == (2, SHARED_CHANNEL)

    @pytest.mark.parametrize(
        "key",
        ["-1005100001", "", "notakey", "1:", ":5", "1:2:3", None, 7, "a:b"],
        ids=["bare-id", "empty", "no-separator", "no-chat", "no-account", "too-many", "none", "int", "letters"],
    )
    def test_anything_that_is_not_one_of_ours_reads_as_none(self, key):
        """Every key of a pre-change blob is a bare id, so this is the upgrade path."""
        assert parse_account_chat_stats_key(key) is None

    async def test_the_map_is_grouped_by_account_and_chat(self, real_adapter):
        await seed_two_accounts(real_adapter)

        stats = await real_adapter.calculate_and_store_statistics()

        assert stats[PER_ACCOUNT_CHAT_COUNTS_KEY] == {
            account_chat_stats_key(account, chat): count for (account, chat), count in SEEDED.items()
        }
        assert LEGACY_CHAT_COUNTS_KEY not in stats

    async def test_the_archive_wide_totals_still_count_rows(self, real_adapter):
        """Storage figures, deliberately not folded: four chat rows, twelve messages."""
        await seed_two_accounts(real_adapter)

        stats = await real_adapter.calculate_and_store_statistics()

        assert stats["chats"] == len(SEEDED)
        assert stats["messages"] == ALL_MESSAGES


class TestVisibleChatPairs:
    async def test_the_pairs_are_account_qualified(self, real_adapter):
        await seed_two_accounts(real_adapter)

        pairs = await real_adapter.get_visible_chat_pairs(ChatScope.build(accounts={1}))

        assert pairs == {(1, SHARED_CHANNEL), (1, COLLIDING_PRIVATE)}

    async def test_a_ref_grant_selects_exactly_its_chats(self, real_adapter):
        await seed_two_accounts(real_adapter)

        pairs = await real_adapter.get_visible_chat_pairs(ChatScope.build(refs={REF_OF[(2, SHARED_CHANNEL)]}))

        assert pairs == {(2, SHARED_CHANNEL)}


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
    user = web_main.UserContext(username="stats-test", role=fields.pop("role", "viewer"), **fields)
    web_main.app.dependency_overrides[web_main.require_auth] = lambda: user
    web_main.app.dependency_overrides[web_main.require_master] = lambda: user


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


class TestRestrictedTotalsAreItsOwn:
    async def test_a_viewer_entitled_to_one_account_reads_that_account_s_numbers(self, app_on):
        """The defect: these used to be the summed totals of both accounts."""
        await seed_two_accounts(app_on)
        await app_on.calculate_and_store_statistics()
        as_principal(allowed_accounts={1})

        async with client() as http:
            body = (await http.get("/api/stats")).json()

        assert body["chats"] == 2
        assert body["messages"] == ACCOUNT_ONE_MESSAGES
        assert body["messages"] != ALL_MESSAGES

    async def test_the_other_account_reads_its_own(self, app_on):
        await seed_two_accounts(app_on)
        await app_on.calculate_and_store_statistics()
        as_principal(allowed_accounts={2})

        async with client() as http:
            body = (await http.get("/api/stats")).json()

        assert body["chats"] == 2
        assert body["messages"] == ACCOUNT_TWO_MESSAGES

    async def test_a_share_token_reads_only_its_granted_chat(self, app_on):
        """A ref grant names one copy, so only that copy's messages count."""
        await seed_two_accounts(app_on)
        await app_on.calculate_and_store_statistics()
        as_principal(role="token", allowed_chat_refs={REF_OF[(2, SHARED_CHANNEL)]})

        async with client() as http:
            body = (await http.get("/api/stats")).json()

        assert body["chats"] == 1
        assert body["messages"] == SEEDED[(2, SHARED_CHANNEL)]

    async def test_a_ref_grant_on_one_conversation_excludes_the_colliding_one(self, app_on):
        """Two accounts' conversations with the same person are two chats.

        They share a ``chats.id``, so a map keyed by id alone gave this viewer
        the other conversation's messages as well as its own.
        """
        await seed_two_accounts(app_on)
        await app_on.calculate_and_store_statistics()
        as_principal(role="token", allowed_chat_refs={REF_OF[(1, COLLIDING_PRIVATE)]})

        async with client() as http:
            body = (await http.get("/api/stats")).json()

        assert body["chats"] == 1
        assert body["messages"] == SEEDED[(1, COLLIDING_PRIVATE)]

    async def test_an_unrestricted_principal_keeps_the_archive_wide_totals(self, app_on):
        await seed_two_accounts(app_on)
        await app_on.calculate_and_store_statistics()
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            body = (await http.get("/api/stats")).json()

        assert body["chats"] == len(SEEDED)
        assert body["messages"] == ALL_MESSAGES
        # Storage figures survive for a principal that may see everything.
        assert "media_files" in body
        assert "total_size_mb" in body


class TestFailClosed:
    async def test_a_blob_written_before_the_change_scopes_to_zeros(self, app_on):
        """Its keys are bare chat ids, which cannot name an account.

        Reading them anyway is exactly the leak this fixes, so the viewer sees
        zeros until the next calculation. The daily job refreshes it, and
        POST /api/stats/refresh does it on demand.
        """
        await seed_two_accounts(app_on)
        await app_on.set_metadata(
            "cached_stats",
            '{"chats": 4, "messages": 12, "media_files": 7, "total_size_mb": 1.0,'
            ' "per_chat_message_counts": {"-1005100001": 7, "510000002": 5}}',
        )
        as_principal(allowed_accounts={1})

        async with client() as http:
            body = (await http.get("/api/stats")).json()

        assert body["chats"] == 0
        assert body["messages"] == 0

    async def test_the_old_map_never_reaches_the_response(self, app_on):
        """Its keys are chat ids, and a chat id does not travel to a browser."""
        await seed_two_accounts(app_on)
        await app_on.set_metadata(
            "cached_stats",
            '{"chats": 4, "messages": 12, "per_chat_message_counts": {"-1005100001": 7}}',
        )
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            resp = await http.get("/api/stats")

        assert LEGACY_CHAT_COUNTS_KEY not in resp.json()
        assert "1005100001" not in resp.text

    async def test_the_current_map_never_reaches_the_response_either(self, app_on):
        await seed_two_accounts(app_on)
        await app_on.calculate_and_store_statistics()
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            resp = await http.get("/api/stats")

        assert PER_ACCOUNT_CHAT_COUNTS_KEY not in resp.json()
        assert "1005100001" not in resp.text

    async def test_refresh_does_not_hand_back_the_map_either(self, app_on):
        await seed_two_accounts(app_on)
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            resp = await http.post("/api/stats/refresh")

        assert resp.status_code == 200
        assert PER_ACCOUNT_CHAT_COUNTS_KEY not in resp.json()

    @pytest.mark.parametrize(
        "raw",
        [None, [], "text", {}, {"1:2": "many"}, {"1:2": True}],
        ids=["null", "list", "string", "empty", "non-int-count", "boolean-count"],
    )
    def test_an_unreadable_map_scopes_to_nothing(self, raw):
        """Never fail open to the archive-wide numbers this scoping exists to hide."""
        assert web_main._scoped_message_counts(raw, {(1, 2)}) == {}

    def test_a_count_for_a_chat_outside_the_grant_is_dropped(self):
        assert web_main._scoped_message_counts({"1:2": 5, "2:2": 9}, {(1, 2)}) == {(1, 2): 5}


class TestFolderCountsTakeTheSameKey:
    async def test_a_folder_is_not_credited_with_the_other_account_s_rows(self, app_on):
        """Folder membership is keyed by (account, chat) too.

        Counting by bare id gave this folder four members — its own two chats
        plus the other account's rows for the same two ids. The viewer is
        entitled to one account, so two is the honest number.

        Known limit, deliberately not addressed here: a folder id is per
        account, so two accounts that both define folder 3 still produce two
        tabs whose counts are computed from one ``folder_id`` group. Giving
        folders an account-qualified identity reaches the ``folder_id`` query
        parameter and the sidebar, which is a redesign rather than a fix.
        """
        await seed_two_accounts(app_on)
        for account_id in (1, 2):
            await app_on.upsert_chat_folder({"id": 3, "title": "a folder"}, account_id=account_id)
            await app_on.sync_folder_members(3, [SHARED_CHANNEL, COLLIDING_PRIVATE], account_id=account_id)
        as_principal(allowed_accounts={1})

        async with client() as http:
            folders = (await http.get("/api/folders")).json()["folders"]

        assert [folder["chat_count"] for folder in folders] == [2, 2]
        assert all(folder["chat_count"] == 2 for folder in folders), "four would be both accounts' rows"

    async def test_an_empty_grant_counts_no_folder_at_all(self, app_on):
        """The empty grant denies, here as everywhere else."""
        await seed_two_accounts(app_on)
        await app_on.upsert_chat_folder({"id": 3, "title": "a folder"}, account_id=1)
        await app_on.sync_folder_members(3, [SHARED_CHANNEL], account_id=1)
        as_principal(allowed_accounts=set())

        async with client() as http:
            folders = (await http.get("/api/folders")).json()["folders"]

        assert folders == []
