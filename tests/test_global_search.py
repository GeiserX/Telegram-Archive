"""Global message search (the sidebar's Messages section): adapter semantics on real engines.

Cross-chat ordering, entitlement scoping, chat naming, topic titles, paging
and the two PostgreSQL access paths of ``search_messages_global`` — executed
against real SQLite (and PostgreSQL when reachable), because the full-text
predicate, the MATERIALIZED hit set and the statement timeout are dialect
behaviour a mock cannot vouch for.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from src.db.adapter import ChatScope

BASE = datetime(2026, 1, 1, 12, 0, 0)
UNRESTRICTED = ChatScope.build()


async def _seed_chat(adapter, chat_id: int, *, account_id: int = 1, **fields) -> None:
    await adapter.upsert_chat(
        {"id": chat_id, "type": "group", "title": f"chat {chat_id}", **fields}, account_id=account_id
    )


async def _seed_message(
    adapter,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    minutes: int = 0,
    account_id: int = 1,
    reply_to_top_id: int | None = None,
    is_deleted: int = 0,
) -> None:
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": BASE + timedelta(minutes=minutes),
            "text": text,
            "sender_name": "Fixture Sender",
            "reply_to_top_id": reply_to_top_id,
            "is_deleted": is_deleted,
            "raw_data": {},
        },
        account_id=account_id,
    )


async def _search(adapter, query: str, *, scope=UNRESTRICTED, **kwargs):
    return await adapter.search_messages_global(query, scope=scope, **kwargs)


def _keys(payload) -> list[tuple[int, int]]:
    return [(row["chat_id"], row["id"]) for row in payload["results"]]


class TestOrderingAndPaging:
    async def test_newest_first_across_chats_and_every_page_is_reachable(self, real_adapter):
        """Ties on date are ordered by (account, chat, id) so offset pages never skip or repeat."""
        await _seed_chat(real_adapter, 920001)
        await _seed_chat(real_adapter, 920002)
        # Two chats, identical timestamps per pair: the tie the plain (date, id)
        # order left undefined.
        await _seed_message(real_adapter, 920001, 1, "haarp research one", minutes=0)
        await _seed_message(real_adapter, 920002, 1, "haarp research two", minutes=0)
        await _seed_message(real_adapter, 920001, 2, "haarp research three", minutes=5)
        await _seed_message(real_adapter, 920002, 2, "haarp research four", minutes=5)
        await _seed_message(real_adapter, 920001, 3, "nothing to see here", minutes=9)

        full = await _search(real_adapter, "haarp", limit=10)
        assert _keys(full) == [(920002, 2), (920001, 2), (920002, 1), (920001, 1)]
        assert full["has_more"] is False

        walked, offset = [], 0
        while True:
            page = await _search(real_adapter, "haarp", limit=1, offset=offset)
            walked.extend(_keys(page))
            if not page["has_more"]:
                break
            offset += 1
        assert walked == _keys(full)

    async def test_has_more_is_answered_by_the_extra_row_and_offset_past_the_end_is_empty(self, real_adapter):
        await _seed_chat(real_adapter, 920003)
        for message_id in range(1, 5):
            await _seed_message(real_adapter, 920003, message_id, "budget meeting", minutes=message_id)

        page = await _search(real_adapter, "budget", limit=3)
        assert [row["id"] for row in page["results"]] == [4, 3, 2]
        assert page["has_more"] is True

        last = await _search(real_adapter, "budget", limit=3, offset=3)
        assert [row["id"] for row in last["results"]] == [1]
        assert last["has_more"] is False

        assert (await _search(real_adapter, "budget", limit=3, offset=40)) == {
            "results": [],
            "has_more": False,
            "indexed": True,
        }

    async def test_matches_the_per_chat_search_for_the_same_words(self, real_adapter):
        """The sidebar and the in-chat box must agree: same predicate, same prefix semantics."""
        await _seed_chat(real_adapter, 920004)
        await _seed_message(real_adapter, 920004, 1, "covid-19 numbers are in", minutes=0)
        await _seed_message(real_adapter, 920004, 2, "the covid numbers again", minutes=1)
        await _seed_message(real_adapter, 920004, 3, "numbers without the word", minutes=2)

        per_chat = await real_adapter.get_messages_paginated(chat_id=920004, search="covid num", limit=50, account_id=1)
        everywhere = await _search(real_adapter, "covid num")
        assert [row["id"] for row in everywhere["results"]] == [row["id"] for row in per_chat] == [2, 1]

    async def test_a_search_without_words_answers_empty(self, real_adapter):
        await _seed_chat(real_adapter, 920005)
        await _seed_message(real_adapter, 920005, 1, "+++ signal +++")

        assert (await _search(real_adapter, "+++")) == {"results": [], "has_more": False, "indexed": True}
        # Control: the same row IS found through a word.
        assert [row["id"] for row in (await _search(real_adapter, "signal"))["results"]] == [1]


class TestNoIndex:
    async def test_without_the_full_text_layer_the_search_declines_instead_of_scanning(self, real_adapter, monkeypatch):
        """The per-chat search falls back to ILIKE; across every chat that is a whole-archive scan per keystroke."""
        await _seed_chat(real_adapter, 960001)
        await _seed_message(real_adapter, 960001, 1, "findable text")

        async def no_layer(session, search):
            return None

        monkeypatch.setattr(real_adapter, "_text_search_predicate", no_layer)
        assert (await _search(real_adapter, "findable")) == {"results": [], "has_more": False, "indexed": False}
        # Control: with the layer, the same row is found and the flag says so.
        monkeypatch.undo()
        found = await _search(real_adapter, "findable")
        assert [row["id"] for row in found["results"]] == [1]
        assert found["indexed"] is True


class TestScope:
    async def _seed_two_accounts(self, real_adapter):
        await _seed_chat(real_adapter, 930001, account_id=1)
        await _seed_chat(real_adapter, 930002, account_id=1)
        await _seed_chat(real_adapter, 930003, account_id=2)
        await _seed_message(real_adapter, 930001, 1, "shared keyword", minutes=0, account_id=1)
        await _seed_message(real_adapter, 930002, 1, "shared keyword", minutes=1, account_id=1)
        await _seed_message(real_adapter, 930003, 1, "shared keyword", minutes=2, account_id=2)

    async def _ref(self, real_adapter, chat_id: int, account_id: int = 1) -> str:
        chat = await real_adapter.get_chat_by_id(chat_id, account_id=account_id)
        return chat["ref"]

    async def test_unrestricted_sees_every_account(self, real_adapter):
        await self._seed_two_accounts(real_adapter)
        assert _keys(await _search(real_adapter, "keyword")) == [(930003, 1), (930002, 1), (930001, 1)]

    async def test_each_rule_restricts_and_an_empty_grant_denies_everything(self, real_adapter):
        await self._seed_two_accounts(real_adapter)
        ref = await self._ref(real_adapter, 930002)

        assert _keys(await _search(real_adapter, "keyword", scope=ChatScope.build(ids={930001}))) == [(930001, 1)]
        assert _keys(await _search(real_adapter, "keyword", scope=ChatScope.build(accounts={2}))) == [(930003, 1)]
        assert _keys(await _search(real_adapter, "keyword", scope=ChatScope.build(refs={ref}))) == [(930002, 1)]
        assert _keys(await _search(real_adapter, "keyword", scope=ChatScope.build(ids={930001}, accounts={2}))) == []

        for empty in (ChatScope.build(ids=set()), ChatScope.build(accounts=set()), ChatScope.build(refs=set())):
            assert (await _search(real_adapter, "keyword", scope=empty)) == {
                "results": [],
                "has_more": False,
                "indexed": True,
            }


class TestRowShape:
    async def test_private_chat_rows_carry_the_name_fields_the_chat_list_uses(self, real_adapter):
        await real_adapter.upsert_chat(
            {
                "id": 940001,
                "type": "private",
                "title": None,
                "first_name": "Ana",
                "last_name": "Pérez",
                "username": "ana",
            },
            account_id=1,
        )
        await _seed_message(real_adapter, 940001, 1, "lunch tomorrow?")

        row = (await _search(real_adapter, "lunch"))["results"][0]
        assert row["chat_title"] is None
        assert (row["chat_first_name"], row["chat_last_name"], row["chat_username"]) == ("Ana", "Pérez", "ana")
        assert row["chat_type"] == "private"
        assert row["chat_is_forum"] is False
        assert row["topic_title"] is None
        assert row["is_deleted"] is False
        assert row["sender_name"] == "Fixture Sender"
        assert row["date"] == BASE
        assert row["text"] == "lunch tomorrow?"
        assert row["chat_ref"] == (await real_adapter.get_chat_by_id(940001, account_id=1))["ref"]

    async def test_forum_hits_name_their_topic_and_general_is_the_default(self, real_adapter):
        await _seed_chat(real_adapter, 940002, is_forum=1)
        await real_adapter.upsert_forum_topic({"id": 7, "chat_id": 940002, "title": "Recipes"}, account_id=1)
        await real_adapter.upsert_forum_topic({"id": 1, "chat_id": 940002, "title": "General"}, account_id=1)
        await _seed_chat(real_adapter, 940003, is_forum=1)  # no General row captured
        await _seed_message(real_adapter, 940002, 1, "paella recipe", minutes=0, reply_to_top_id=7)
        await _seed_message(real_adapter, 940002, 2, "paella tonight", minutes=1)
        await _seed_message(real_adapter, 940003, 1, "paella again", minutes=2)

        rows = (await _search(real_adapter, "paella"))["results"]
        assert [(row["chat_id"], row["topic_title"], row["chat_is_forum"]) for row in rows] == [
            (940003, None, True),
            (940002, "General", True),
            (940002, "Recipes", True),
        ]

    async def test_deleted_hits_are_flagged_not_hidden(self, real_adapter):
        await _seed_chat(real_adapter, 940004)
        await _seed_message(real_adapter, 940004, 1, "retracted statement", minutes=0, is_deleted=1)
        await _seed_message(real_adapter, 940004, 2, "statement stands", minutes=1)

        rows = (await _search(real_adapter, "statement"))["results"]
        assert [(row["id"], row["is_deleted"]) for row in rows] == [(2, False), (1, True)]


class TestPostgresPaths:
    """The dense/sparse split and the timeout fallback, driven with a handful of rows."""

    @pytest.fixture(autouse=True)
    def _postgres_only(self, real_adapter):
        if real_adapter._is_sqlite:
            pytest.skip("PostgreSQL access paths")

    async def _seed(self, real_adapter):
        await _seed_chat(real_adapter, 950001)
        await _seed_chat(real_adapter, 950002)
        for message_id in range(1, 4):
            await _seed_message(real_adapter, 950001, message_id, "dense term", minutes=message_id)
            await _seed_message(real_adapter, 950002, message_id, "dense term", minutes=message_id)
        await _seed_message(real_adapter, 950001, 9, "unrelated", minutes=30)

    async def test_hit_count_stops_at_the_cap(self, real_adapter):
        await self._seed(real_adapter)
        async with real_adapter.db_manager.async_session_factory() as session:
            predicate = await real_adapter._text_search_predicate(session, "dense")
            assert predicate is not None  # the FTS layer is what these paths are about
            assert await real_adapter._global_search_hit_count(session, predicate, UNRESTRICTED, 100) == 6
            assert await real_adapter._global_search_hit_count(session, predicate, UNRESTRICTED, 4) == 4
            scoped = ChatScope.build(ids={950002})
            assert await real_adapter._global_search_hit_count(session, predicate, scoped, 100) == 3

    async def test_walk_and_sorted_hits_answer_identically(self, real_adapter):
        await self._seed(real_adapter)
        expected = [(950002, 3), (950001, 3), (950002, 2), (950001, 2), (950002, 1), (950001, 1)]
        scoped = ChatScope.build(ids={950002})

        # dense_hits=1: six hits is "dense", so the date walk answers.
        walk = await _search(real_adapter, "dense", limit=4, dense_hits=1)
        walk_rest = await _search(real_adapter, "dense", limit=4, offset=4, dense_hits=1)
        # dense_hits=1000: six hits is "sparse", so the MATERIALIZED hit set answers.
        sorted_hits = await _search(real_adapter, "dense", limit=4, dense_hits=1000)
        sorted_rest = await _search(real_adapter, "dense", limit=4, offset=4, dense_hits=1000)

        assert _keys(walk) == _keys(sorted_hits) == expected[:4]
        assert walk["has_more"] is sorted_hits["has_more"] is True
        assert _keys(walk_rest) == _keys(sorted_rest) == expected[4:]
        assert walk_rest["has_more"] is sorted_rest["has_more"] is False
        assert _keys(await _search(real_adapter, "dense", scope=scoped, dense_hits=1)) == [
            (950002, 3),
            (950002, 2),
            (950002, 1),
        ]
        assert _keys(await _search(real_adapter, "dense", scope=scoped, dense_hits=1000)) == [
            (950002, 3),
            (950002, 2),
            (950002, 1),
        ]

    async def test_a_walk_timeout_falls_back_to_the_sorted_hits(self, real_adapter, monkeypatch):
        await self._seed(real_adapter)
        cancelled = DBAPIError("SELECT ...", {}, type("QueryCanceledError", (Exception,), {"sqlstate": "57014"})())
        monkeypatch.setattr(real_adapter, "_global_search_walk", AsyncMock(side_effect=cancelled))

        page = await _search(real_adapter, "dense", limit=4, dense_hits=1)
        assert _keys(page) == [(950002, 3), (950001, 3), (950002, 2), (950001, 2)]
        assert page["has_more"] is True
        real_adapter._global_search_walk.assert_awaited_once()

    async def test_any_other_database_error_still_propagates(self, real_adapter, monkeypatch):
        await self._seed(real_adapter)
        broken = DBAPIError("SELECT ...", {}, type("UndefinedTableError", (Exception,), {"sqlstate": "42P01"})())
        monkeypatch.setattr(real_adapter, "_global_search_walk", AsyncMock(side_effect=broken))

        with pytest.raises(DBAPIError):
            await _search(real_adapter, "dense", dense_hits=1)
