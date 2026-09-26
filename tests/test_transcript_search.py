"""Transcript search (slice 6 of docs/TRANSCRIPTION.md) on real engines.

Chat search (``get_messages_paginated``) and global search
(``search_messages_global``) build their hit set as the UNION of two indexed
key sets: messages through the messages index, and messages reached from
transcript hits through ``media`` on ``(account_id, media_id)``. These tests
run on SQLite and, when a server is reachable, on PostgreSQL, where the plan
of the message side is checked to still read the GIN index.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

from src.db.adapter import ChatScope

sys.path.insert(0, os.path.dirname(__file__))

from test_transcription_bubble import _html, _run_node, _script  # noqa: E402

BASE = datetime(2026, 1, 1, 12, 0, 0)
UNRESTRICTED = ChatScope.build()
CHAT = -420600001
OTHER_CHAT = -420600002


async def _chat(adapter, chat_id: int = CHAT, *, account_id: int = 1) -> None:
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": f"chat {chat_id}"}, account_id=account_id)


async def _message(adapter, message_id: int, text: str, *, chat_id: int = CHAT, minutes: int = 0, account_id: int = 1):
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": BASE + timedelta(minutes=minutes),
            "text": text,
            "sender_name": "Fixture Sender",
            "raw_data": {},
        },
        account_id=account_id,
    )


async def _voice(
    adapter,
    message_id: int,
    transcript: str | None,
    *,
    text: str = "",
    chat_id: int = CHAT,
    minutes: int = 0,
    account_id: int = 1,
    status: str = "done",
) -> str:
    """A voice message with its media row and one transcript row."""
    await _message(adapter, message_id, text, chat_id=chat_id, minutes=minutes, account_id=account_id)
    media_id = f"{chat_id}_{message_id}_voice"
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "type": "voice",
            "file_path": f"fixture/{message_id}.ogg",
            "downloaded": True,
            "duration": 5,
        },
        account_id=account_id,
    )
    row = await adapter.enqueue_media_transcript(media_id, account_id=account_id, preset="auto")
    if status == "done":
        await adapter.fill_media_transcript(row["id"], status="done", text=transcript, language="es")
    elif status != "queued":
        await adapter.fill_media_transcript(row["id"], status=status, error="submit_failed")
    return media_id


async def _global(adapter, query: str, *, scope=UNRESTRICTED, **kwargs):
    return await adapter.search_messages_global(query, scope=scope, **kwargs)


async def _chat_search(adapter, query: str, **kwargs):
    return await adapter.get_messages_paginated(chat_id=CHAT, search=query, limit=50, account_id=1, **kwargs)


def _hits(rows) -> list[tuple[int, str]]:
    return [(row["id"], row["matched_in"]) for row in rows]


async def _seed_three(adapter) -> None:
    """Message 1 matches in its text, 2 only in its transcript, 3 in both; 4 matches nowhere."""
    await _chat(adapter)
    await _message(adapter, 1, "the harbour is closed", minutes=1)
    await _voice(adapter, 2, "meet me at the harbour tonight", minutes=2)
    await _voice(adapter, 3, "harbour again", text="harbour photo attached", minutes=3)
    await _voice(adapter, 4, "nothing relevant", text="nor here", minutes=4)


async def _seed_lighthouse(adapter) -> None:
    """Message 5's media was transcribed twice and both takes match; message 4 once."""
    await _chat(adapter)
    await _voice(adapter, 4, "lighthouse keeper", minutes=4)
    media_id = await _voice(adapter, 5, "lighthouse keeper", minutes=5)
    again = await adapter.enqueue_media_transcript(media_id, account_id=1, force=True)
    await adapter.fill_media_transcript(again["id"], status="done", text="the lighthouse keeper")


class TestGlobalSearch:
    async def test_a_transcript_only_match_is_found_and_says_where(self, real_adapter):
        await _seed_three(real_adapter)
        payload = await _global(real_adapter, "harbour")
        assert _hits(payload["results"]) == [(3, "message"), (2, "transcript"), (1, "message")]
        assert payload["has_more"] is False

    async def test_every_page_is_reachable_and_nothing_repeats(self, real_adapter):
        await _seed_three(real_adapter)
        walked, offset = [], 0
        while True:
            page = await _global(real_adapter, "harbour", limit=1, offset=offset)
            walked.extend(_hits(page["results"]))
            if not page["has_more"]:
                break
            offset += 1
        assert walked == [(3, "message"), (2, "transcript"), (1, "message")]
        assert (await _global(real_adapter, "harbour", limit=2, offset=3))["results"] == []

    async def test_two_matching_transcripts_of_one_media_are_one_hit(self, real_adapter):
        await _chat(real_adapter)
        media_id = await _voice(real_adapter, 5, "lighthouse keeper", minutes=5)
        again = await real_adapter.enqueue_media_transcript(media_id, account_id=1, force=True)
        await real_adapter.fill_media_transcript(again["id"], status="done", text="the lighthouse keeper")
        payload = await _global(real_adapter, "lighthouse", limit=1)
        assert _hits(payload["results"]) == [(5, "transcript")]
        assert payload["has_more"] is False

    async def test_both_paths_count_messages_when_one_media_has_two_matching_transcripts(self, real_adapter):
        """The walk cuts each side to ``offset + limit + 1`` keys; a repeated key must not use that depth up."""
        await _seed_lighthouse(real_adapter)
        async with real_adapter.db_manager.async_session_factory() as session:
            predicate = await real_adapter._text_search_predicate(session, "lighthouse")
            transcripts = await real_adapter._transcript_search_predicate(session, "lighthouse")
            assert transcripts is not None
            for path in (real_adapter._global_search_walk, real_adapter._global_search_sorted_hits):
                rows = await path(session, predicate, UNRESTRICTED, 1, 0, transcript_predicate=transcripts)
                assert [(row["id"], row["via_transcript"]) for row in rows] == [(5, 1), (4, 1)], path.__name__

    async def test_an_unfinished_transcript_matches_nothing(self, real_adapter):
        await _chat(real_adapter)
        await _voice(real_adapter, 6, None, status="queued")
        await _voice(real_adapter, 7, None, status="failed")
        assert (await _global(real_adapter, "submit"))["results"] == []

    async def test_the_scope_applies_to_the_transcript_side(self, real_adapter):
        await _chat(real_adapter)
        await _chat(real_adapter, OTHER_CHAT)
        await _voice(real_adapter, 1, "quarantine rules", minutes=1)
        await _voice(real_adapter, 2, "quarantine ends", chat_id=OTHER_CHAT, minutes=2)
        scoped = await _global(real_adapter, "quarantine", scope=ChatScope.build(ids={CHAT}))
        assert [(row["chat_id"], row["id"]) for row in scoped["results"]] == [(CHAT, 1)]
        everything = await _global(real_adapter, "quarantine")
        assert [(row["chat_id"], row["id"]) for row in everything["results"]] == [(OTHER_CHAT, 2), (CHAT, 1)]


class TestChatSearch:
    async def test_a_transcript_only_match_is_found_and_says_where(self, real_adapter):
        await _seed_three(real_adapter)
        assert _hits(await _chat_search(real_adapter, "harbour")) == [(3, "message"), (2, "transcript"), (1, "message")]

    async def test_pages_by_offset_and_by_cursor_stay_correct(self, real_adapter):
        await _seed_three(real_adapter)
        first = await real_adapter.get_messages_paginated(chat_id=CHAT, search="harbour", limit=2, account_id=1)
        assert _hits(first) == [(3, "message"), (2, "transcript")]
        rest = await real_adapter.get_messages_paginated(
            chat_id=CHAT, search="harbour", limit=2, account_id=1, before_date=first[-1]["date"], before_id=2
        )
        assert _hits(rest) == [(1, "message")]
        by_offset = await real_adapter.get_messages_paginated(
            chat_id=CHAT, search="harbour", limit=2, offset=2, account_id=1
        )
        assert _hits(by_offset) == [(1, "message")]

    async def test_another_chat_or_account_never_leaks_in(self, real_adapter):
        await _chat(real_adapter)
        await _chat(real_adapter, OTHER_CHAT)
        await _chat(real_adapter, account_id=2)
        # Each foreign hit shares its message id with a message of this chat
        # that does not match, so a hit key that lost its chat or account
        # bound would name that message and show up here.
        await _voice(real_adapter, 1, "quarantine rules", chat_id=OTHER_CHAT)
        await _voice(real_adapter, 1, "nothing relevant")
        await _voice(real_adapter, 2, "quarantine rules", account_id=2)
        await _voice(real_adapter, 2, "nor here", minutes=2)
        await _voice(real_adapter, 3, "quarantine here", minutes=3)
        assert _hits(await _chat_search(real_adapter, "quarantine")) == [(3, "transcript")]

    async def test_a_plain_page_carries_no_matched_in(self, real_adapter):
        await _seed_three(real_adapter)
        page = await real_adapter.get_messages_paginated(chat_id=CHAT, limit=50, account_id=1)
        assert [row["id"] for row in page] == [4, 3, 2, 1]
        assert not any("matched_in" in row for row in page)


class TestWithoutTranscriptIndex:
    async def test_the_transcript_side_is_simply_absent(self, real_adapter, monkeypatch):
        """SQLite built without FTS5, or a database not yet at 032."""
        await _seed_three(real_adapter)

        async def missing(session):
            return False

        monkeypatch.setattr(real_adapter, "_transcript_fts_ready", missing)
        assert _hits((await _global(real_adapter, "harbour"))["results"]) == [(3, "message"), (1, "message")]
        assert _hits(await _chat_search(real_adapter, "harbour")) == [(3, "message"), (1, "message")]

    async def test_the_ilike_fallback_searches_message_text_only(self, real_adapter, monkeypatch):
        await _seed_three(real_adapter)

        async def no_layer(session, search):
            return None

        monkeypatch.setattr(real_adapter, "_text_search_predicate", no_layer)
        assert _hits(await _chat_search(real_adapter, "harbour")) == [(3, "message"), (1, "message")]


class TestPostgres:
    @pytest.fixture(autouse=True)
    def _postgres_only(self, real_adapter):
        if real_adapter._is_sqlite:
            pytest.skip("PostgreSQL access paths")

    async def test_walk_and_sorted_hits_agree_with_transcript_hits(self, real_adapter):
        await _seed_three(real_adapter)
        for message_id in (10, 11, 12):  # newer message hits push the transcript hit to deeper pages
            await _message(real_adapter, message_id, "harbour news", minutes=message_id)
        expected = [(12, "message"), (11, "message"), (10, "message"), (3, "message"), (2, "transcript")]
        expected.append((1, "message"))
        for dense_hits in (1, 1000):  # 1: the date walk; 1000: the MATERIALIZED hit set
            walked, offset = [], 0
            while True:
                page = await _global(real_adapter, "harbour", limit=1, offset=offset, dense_hits=dense_hits)
                walked.extend(_hits(page["results"]))
                if not page["has_more"]:
                    break
                offset += 1
            assert walked == expected, dense_hits
            first = await _global(real_adapter, "harbour", limit=4, dense_hits=dense_hits)
            rest = await _global(real_adapter, "harbour", limit=4, offset=4, dense_hits=dense_hits)
            assert _hits(first["results"]) + _hits(rest["results"]) == expected, dense_hits
            assert (first["has_more"], rest["has_more"]) == (True, False), dense_hits

    async def test_the_walk_reaches_every_message_behind_a_twice_transcribed_one(self, real_adapter):
        await _seed_lighthouse(real_adapter)
        for dense_hits in (1, 1000):
            walked, offset = [], 0
            while True:
                page = await _global(real_adapter, "lighthouse", limit=1, offset=offset, dense_hits=dense_hits)
                walked.extend(_hits(page["results"]))
                if not page["has_more"]:
                    break
                offset += 1
            assert walked == [(5, "transcript"), (4, "transcript")], dense_hits

    async def test_the_hit_count_counts_messages_not_matches(self, real_adapter):
        await _seed_three(real_adapter)
        async with real_adapter.db_manager.async_session_factory() as session:
            predicate = await real_adapter._text_search_predicate(session, "harbour")
            transcripts = await real_adapter._transcript_search_predicate(session, "harbour")
            assert transcripts is not None
            count = real_adapter._global_search_hit_count
            assert await count(session, predicate, UNRESTRICTED, 100, transcript_predicate=transcripts) == 3
            assert await count(session, predicate, UNRESTRICTED, 2, transcript_predicate=transcripts) == 2
            assert await count(session, predicate, UNRESTRICTED, 100) == 2

    async def test_each_side_of_the_global_query_reads_its_gin_index(self, real_adapter):
        """EXPLAIN the two key sets the union is built from.

        Sequential scans are priced out so a near-empty table still shows
        whether the predicate CAN use the index; an OR or an EXISTS folded
        into the message predicate would leave a sequential scan here.
        """
        await _seed_three(real_adapter)
        dialect = real_adapter.db_manager.engine.dialect
        async with real_adapter.db_manager.async_session_factory() as session:
            predicate = await real_adapter._text_search_predicate(session, "harbour")
            transcripts = await real_adapter._transcript_search_predicate(session, "harbour")
            sides = real_adapter._global_search_sides(predicate, transcripts, UNRESTRICTED, fold_shared=False)
            assert len(sides) == 2, "the transcript side is part of the union"
            connection = await session.connection()
            await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            plans = []
            for side in sides:
                sql = str(side.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
                plans.append("\n".join((await connection.exec_driver_sql(f"EXPLAIN {sql}")).scalars().all()))
        assert "idx_messages_text_search" in plans[0], plans[0]
        assert "media_transcripts" not in plans[0], plans[0]
        assert "idx_media_transcripts_text_search" in plans[1], plans[1]


# ============================================================================
# The viewer: a transcript hit opens its bubble, and a press closes it
# ============================================================================


def test_a_transcript_hit_opens_the_bubble_until_pressed() -> None:
    _run_node(
        _script(
            """
            transcriptionState.value = { enabled: true, configured: true }
            const hit = voice(1, [done(4)]), other = voice(2, [done(5)])
            assert.equal(isTranscriptExpanded(hit), false)
            // A global hit found in the transcript opens only the row it landed on.
            messageHighlight.value = { query: 'harbour', messageId: 1, transcript: true }
            assert.equal(isTranscriptExpanded(hit), true)
            assert.equal(isTranscriptExpanded(other), false)
            // A global hit found in the message text opens nothing.
            messageHighlight.value = { query: 'harbour', messageId: 1 }
            assert.equal(isTranscriptExpanded(hit), false)
            // The in-chat filter opens every row the server marked.
            messageHighlight.value = { query: 'harbour', messageId: null }
            hit.matched_in = 'transcript'
            other.matched_in = 'message'
            assert.equal(isTranscriptExpanded(hit), true)
            assert.equal(isTranscriptExpanded(other), false)
            // A press closes it for this search; the next search opens it again.
            await pressTranscript(hit)
            assert.equal(isTranscriptExpanded(hit), false)
            messageHighlight.value = { query: 'harbou', messageId: null }
            assert.equal(isTranscriptExpanded(hit), true)
            // No search: the remembered state rules again.
            messageHighlight.value = null
            assert.equal(isTranscriptExpanded(hit), false)
            assert.equal(requests.length, 0)
            """
        )
    )


def test_the_marks_reach_the_transcript_text() -> None:
    html = _html()
    # Rendered through v-html like message text, so a mark never replaces a node Vue patches.
    # One per bubble that shows a transcript: audio, round video, video, and a video sent as a file.
    assert (
        html.count('class="transcript-text text-sm text-tg-ink whitespace-pre-wrap" v-html="transcriptHtml(msg)"') == 4
    )
    assert "{{ selectedTranscript(msg).text }}" not in html
    assert html.count("querySelectorAll('.message-text, .transcript-text')") == 2
    opener = html[html.index("const openMessageSearchResult") :]
    opener = opener[: opener.index("\n                }\n")]
    assert "row.matched_in === 'transcript'" in opener
    assert "{ query, messageId: row.id, transcript: true }" in opener


def test_a_transcript_hit_says_why_it_matched_in_the_sidebar() -> None:
    """A voice message usually has no text, so its snippet alone would be empty."""
    html = _html()
    row = html[html.index('<span v-html="searchSnippetHtml(row.text)"></span>') :]
    row = row[: row.index("</p>")]
    assert "<span v-if=\"row.matched_in === 'transcript'\"" in row
    assert "Matched in the transcript" in row
