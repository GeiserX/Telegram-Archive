"""Tag search (#hashtag / $cashtag view): adapter semantics on real engines.

Boundary matching, entitlement scoping, the three tabs (chat / mine / all),
ordering and paging for ``search_messages_by_tag`` — executed against real
SQLite (and PostgreSQL when reachable), because ILIKE escaping and tuple
cursors are dialect behavior a mock cannot vouch for.
"""

from datetime import datetime, timedelta

from src.db.adapter import ChatScope

BASE = datetime(2026, 1, 1, 12, 0, 0)

UNRESTRICTED = ChatScope(ids=None, accounts=None, refs=None)


async def _seed_chat(adapter, chat_id: int, title: str = "fixture chat") -> None:
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": title}, account_id=1)


async def _seed_message(
    adapter,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    minutes: int = 0,
    is_outgoing: int = 0,
) -> None:
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": BASE + timedelta(minutes=minutes),
            "text": text,
            "is_outgoing": is_outgoing,
            "sender_name": "Fixture Sender",
            "raw_data": {},
        },
        account_id=1,
    )


class TestTagBoundaries:
    async def test_whole_token_only_and_case_insensitive(self, real_adapter):
        await _seed_chat(real_adapter, 910001)
        await _seed_message(real_adapter, 910001, 1, "launch day #news")
        await _seed_message(real_adapter, 910001, 2, "not this one: #newsletter", minutes=1)
        await _seed_message(real_adapter, 910001, 3, "shouting #NEWS!", minutes=2)
        await _seed_message(real_adapter, 910001, 4, "mid-word x#news stays out", minutes=3)

        payload = await real_adapter.search_messages_by_tag("#news", scope=UNRESTRICTED)
        ids = [row["id"] for row in payload["results"]]
        assert ids == [3, 1]  # newest first; #newsletter and x#news excluded
        assert payload["has_more"] is False

    async def test_cashtag_and_like_metachars_are_literal(self, real_adapter):
        await _seed_chat(real_adapter, 910002)
        await _seed_message(real_adapter, 910002, 1, "buy $TSLA now")
        await _seed_message(real_adapter, 910002, 2, "percent %TSLA is not a tag", minutes=1)
        await _seed_message(real_adapter, 910002, 3, "lowercase $tsla is plain text, not an entity", minutes=2)

        payload = await real_adapter.search_messages_by_tag("$TSLA", scope=UNRESTRICTED)
        assert [row["id"] for row in payload["results"]] == [1]

    async def test_rows_carry_ref_and_title_for_the_jump(self, real_adapter):
        await _seed_chat(real_adapter, 910003, title="Tagged Group")
        await _seed_message(real_adapter, 910003, 1, "#anchor here")

        row = (await real_adapter.search_messages_by_tag("#anchor", scope=UNRESTRICTED))["results"][0]
        assert row["chat_title"] == "Tagged Group"
        assert isinstance(row["chat_ref"], str) and len(row["chat_ref"]) > 10
        assert row["sender_name"] == "Fixture Sender"


class TestTagScoping:
    async def test_chat_scope_restricts_in_sql(self, real_adapter):
        """A restricted viewer's tag search can only ever touch entitled chats."""
        await _seed_chat(real_adapter, 910010, title="entitled")
        await _seed_chat(real_adapter, 910011, title="forbidden")
        await _seed_message(real_adapter, 910010, 1, "#shared in the entitled chat")
        await _seed_message(real_adapter, 910011, 1, "#shared in the forbidden chat", minutes=1)

        restricted = ChatScope(ids=frozenset({910010}), accounts=None, refs=None)
        payload = await real_adapter.search_messages_by_tag("#shared", scope=restricted)
        assert [row["chat_ref"] for row in payload["results"]] == [
            (await real_adapter.get_chat_by_id(910010, account_id=1))["ref"]
        ]

        empty = ChatScope(ids=frozenset(), accounts=None, refs=None)
        assert (await real_adapter.search_messages_by_tag("#shared", scope=empty))["results"] == []

    async def test_this_chat_and_mine_tabs(self, real_adapter):
        await _seed_chat(real_adapter, 910020)
        await _seed_chat(real_adapter, 910021)
        await _seed_message(real_adapter, 910020, 1, "#tab here", is_outgoing=1)
        await _seed_message(real_adapter, 910020, 2, "#tab theirs", minutes=1)
        await _seed_message(real_adapter, 910021, 3, "#tab elsewhere", minutes=2)

        one_chat = await real_adapter.search_messages_by_tag("#tab", scope=UNRESTRICTED, chat_id=910020, account_id=1)
        assert [row["id"] for row in one_chat["results"]] == [2, 1]

        mine = await real_adapter.search_messages_by_tag("#tab", scope=UNRESTRICTED, outgoing_only=True)
        assert [row["id"] for row in mine["results"]] == [1]


class TestTagPaging:
    async def test_offset_paging_and_has_more(self, real_adapter):
        await _seed_chat(real_adapter, 910030)
        for i in range(7):
            await _seed_message(real_adapter, 910030, i + 1, f"#page hit {i}", minutes=i)

        first = await real_adapter.search_messages_by_tag("#page", scope=UNRESTRICTED, limit=3)
        assert [row["id"] for row in first["results"]] == [7, 6, 5]
        assert first["has_more"] is True

        second = await real_adapter.search_messages_by_tag("#page", scope=UNRESTRICTED, limit=3, offset=3)
        assert [row["id"] for row in second["results"]] == [4, 3, 2]
        assert second["has_more"] is True

        last = await real_adapter.search_messages_by_tag("#page", scope=UNRESTRICTED, limit=3, offset=6)
        assert [row["id"] for row in last["results"]] == [1]
        assert last["has_more"] is False

    async def test_prefilter_chunking_survives_substring_noise(self, real_adapter):
        """The internal cursor loop keeps scanning past ILIKE hits the boundary filter rejects."""
        await _seed_chat(real_adapter, 910031)
        for i in range(80):
            await _seed_message(real_adapter, 910031, i + 1, f"#tagnoise{i} filler", minutes=i)
        await _seed_message(real_adapter, 910031, 999, "the real #tag", minutes=200)

        payload = await real_adapter.search_messages_by_tag("#tag", scope=UNRESTRICTED, limit=5)
        assert [row["id"] for row in payload["results"]] == [999]
        assert payload["has_more"] is False

    async def test_scan_cap_reports_truncation_not_phantom_pages(self, real_adapter):
        """At the cap: has_more must be False (offset paging cannot reach past it)."""
        await _seed_chat(real_adapter, 910032)
        # The match is the OLDEST row, behind 65 newer prefilter hits: one
        # 60-row chunk exhausts the cap before the scan can reach it.
        await _seed_message(real_adapter, 910032, 999, "beyond the cap #cap", minutes=0)
        for i in range(65):
            await _seed_message(real_adapter, 910032, i + 1, f"#capnoise{i} filler", minutes=i + 10)

        payload = await real_adapter.search_messages_by_tag("#cap", scope=UNRESTRICTED, limit=5, scan_cap=10)
        assert payload["results"] == []
        assert payload["has_more"] is False
        assert payload["truncated"] is True

        # With the cap lifted the same search finds the match and is complete.
        full = await real_adapter.search_messages_by_tag("#cap", scope=UNRESTRICTED, limit=5)
        assert [row["id"] for row in full["results"]] == [999]
        assert full["truncated"] is False
