"""Adapter tests that run against a real engine on both supported backends.

``tests/test_db_adapter.py`` covers the adapter with a mocked DatabaseManager:
fast, broad, and blind to anything the database itself decides. These tests are
the counterweight. They use the ``real_adapter`` fixture from ``conftest.py``,
so every one of them runs twice — once on SQLite, once on PostgreSQL — and the
SQL is compiled and executed for real.

Only the paths where the two backends genuinely diverge live here:

* ``update_sync_status``  — ``sqlite_insert`` vs ``pg_insert`` upsert, and the
  ``message_count + excluded.message_count`` accumulate on conflict.
* ``insert_message``      — ``on_conflict_do_nothing`` plus, on the conflict
  branch, ``SELECT ... FOR UPDATE`` (PostgreSQL) vs the no-op-write lock
  (SQLite), and the message-version capture that hangs off it.
* ``get_messages_paginated`` — the composite ``(date, id)`` cursor and the
  ``ilike`` search with its ``\\`` escape, which PostgreSQL and SQLite treat
  differently.

The PostgreSQL leg skips when no server is reachable; see conftest.
"""

from datetime import datetime, timedelta

from src.db.models import SyncStatus

BASE_DATE = datetime(2026, 3, 1, 12, 0, 0)


async def _seed_chat(adapter, chat_id: int) -> None:
    """Insert the parent chat row messages and sync_status point at."""
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "fixture chat"}, account_id=1)


def _message(chat_id: int, message_id: int, *, text: str | None = None, offset_minutes: int = 0) -> dict:
    return {
        "id": message_id,
        "chat_id": chat_id,
        "sender_id": 4242,
        "date": BASE_DATE + timedelta(minutes=offset_minutes),
        "text": text,
        "raw_data": {},
    }


class TestUpdateSyncStatusRealEngine:
    """The sync cursor upsert, executed rather than mocked."""

    async def test_insert_then_accumulate_on_conflict(self, real_adapter):
        """First call inserts; the second updates the cursor and ADDS the count.

        The mocked twin of this test asserts ``execute`` was awaited once. That
        passes even if ``message_count`` were assigned instead of accumulated —
        which is the one thing this method's ON CONFLICT clause is for.
        """
        await _seed_chat(real_adapter, 900001)

        await real_adapter.update_sync_status(900001, 500, 50, account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            row = (await session.execute(SyncStatus.__table__.select())).mappings().one()
        assert row["last_message_id"] == 500
        assert row["message_count"] == 50

        await real_adapter.update_sync_status(900001, 750, 25, account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            row = (await session.execute(SyncStatus.__table__.select())).mappings().one()
        assert row["last_message_id"] == 750
        assert row["message_count"] == 75

    async def test_last_message_id_round_trips(self, real_adapter):
        """get_last_message_id reads back what the upsert wrote."""
        await _seed_chat(real_adapter, 900002)
        assert await real_adapter.get_last_message_id(900002, account_id=1) == 0

        await real_adapter.update_sync_status(900002, 1234, 7, account_id=1)
        assert await real_adapter.get_last_message_id(900002, account_id=1) == 1234


class TestMessageUpsertConflictRealEngine:
    """insert_message's conflict branch on both dialects."""

    async def test_reinserting_identical_message_is_a_no_op(self, real_adapter):
        """A re-scan of an unchanged message must not duplicate or mutate it."""
        await _seed_chat(real_adapter, 900003)
        await real_adapter.insert_message(_message(900003, 10, text="hello"), account_id=1)
        await real_adapter.insert_message(_message(900003, 10, text="hello"), account_id=1)

        messages = await real_adapter.get_messages_paginated(900003, limit=10)
        assert len(messages) == 1
        assert messages[0]["text"] == "hello"
        assert await real_adapter.get_message_versions(900003, 10) == []

    async def test_edited_text_updates_row_and_records_a_version(self, real_adapter):
        """The conflict path takes the row lock, updates, and snapshots the old text.

        On PostgreSQL that lock is ``SELECT ... FOR UPDATE``; on SQLite it is a
        no-op ``UPDATE`` that acquires the write lock. Both are exercised here.
        """
        await _seed_chat(real_adapter, 900004)
        await real_adapter.insert_message(_message(900004, 11, text="first"), account_id=1)

        edited = _message(900004, 11, text="second")
        edited["edit_date"] = BASE_DATE + timedelta(minutes=5)
        await real_adapter.insert_message(edited, account_id=1)

        messages = await real_adapter.get_messages_paginated(900004, limit=10)
        assert len(messages) == 1
        assert messages[0]["text"] == "second"

        versions = await real_adapter.get_message_versions(900004, 11)
        assert [v["text"] for v in versions] == ["first"]

    async def test_composite_primary_key_separates_chats(self, real_adapter):
        """The same message id in two chats is two rows, not a conflict."""
        await _seed_chat(real_adapter, 900005)
        await _seed_chat(real_adapter, 900006)
        await real_adapter.insert_message(_message(900005, 12, text="in chat A"), account_id=1)
        await real_adapter.insert_message(_message(900006, 12, text="in chat B"), account_id=1)

        assert (await real_adapter.get_messages_paginated(900005, limit=10))[0]["text"] == "in chat A"
        assert (await real_adapter.get_messages_paginated(900006, limit=10))[0]["text"] == "in chat B"


class TestPaginationRealEngine:
    """get_messages_paginated against a real planner and a real collation."""

    async def test_cursor_pagination_walks_the_chat_newest_first(self, real_adapter):
        """The (date, id) cursor returns every row exactly once, in order."""
        await _seed_chat(real_adapter, 900007)
        for index in range(6):
            await real_adapter.insert_message(
                _message(900007, 100 + index, text=f"m{index}", offset_minutes=index), account_id=1
            )

        first = await real_adapter.get_messages_paginated(900007, limit=4)
        assert [m["id"] for m in first] == [105, 104, 103, 102]

        cursor = first[-1]
        second = await real_adapter.get_messages_paginated(
            900007, limit=4, before_date=cursor["date"], before_id=cursor["id"]
        )
        assert [m["id"] for m in second] == [101, 100]

    async def test_search_escapes_sql_wildcards(self, real_adapter):
        """A literal ``%`` in the query must not behave as a wildcard.

        The escape is passed to ``ilike(..., escape="\\\\")``; whether the
        backend honours it can only be settled by running the query.
        """
        await _seed_chat(real_adapter, 900008)
        await real_adapter.insert_message(_message(900008, 200, text="100% done", offset_minutes=0), account_id=1)
        await real_adapter.insert_message(_message(900008, 201, text="nothing here", offset_minutes=1), account_id=1)

        hits = await real_adapter.get_messages_paginated(900008, limit=10, search="100%")
        assert [m["id"] for m in hits] == [200]

        # A bare "%" must match only the row that literally contains one.
        # Unescaped it is the match-everything wildcard and would return both.
        bare = await real_adapter.get_messages_paginated(900008, limit=10, search="%")
        assert [m["id"] for m in bare] == [200]

    async def test_trgm_index_exists_on_postgresql(self, real_adapter):
        """idx_messages_text_trgm must be a real GIN/pg_trgm index, not just present by name.

        #295-perf: this is what makes the leading-wildcard ILIKE search's cost
        independent of table size instead of scaling linearly with it. Checked
        against the catalog (not EXPLAIN) deliberately - on a near-empty test
        table the planner can rightfully prefer a seq scan over any index
        regardless of what exists, so asserting on the *chosen plan* here
        would be a table-size-dependent flake, not a check of the fix.
        SQLite has no gin_trgm_ops equivalent (see migration 022 / models.py's
        Index() dialect kwargs), so this only runs on PostgreSQL.
        """
        if real_adapter.db_manager.engine.dialect.name != "postgresql":
            import pytest

            pytest.skip("trigram index is PostgreSQL-only")

        from sqlalchemy import text as sa_text

        async with real_adapter.db_manager.async_session_factory() as session:
            row = (
                await session.execute(
                    sa_text(
                        "SELECT am.amname, array_agg(opc.opcname) "
                        "FROM pg_index ix "
                        "JOIN pg_class i ON i.oid = ix.indexrelid "
                        "JOIN pg_am am ON am.oid = i.relam "
                        "JOIN pg_opclass opc ON opc.oid = ANY(ix.indclass) "
                        "WHERE i.relname = 'idx_messages_text_trgm' "
                        "GROUP BY am.amname"
                    )
                )
            ).first()

        assert row is not None, "idx_messages_text_trgm does not exist"
        index_method, opclasses = row
        assert index_method == "gin", f"expected a GIN index, got {index_method!r}"
        assert "gin_trgm_ops" in opclasses, f"expected gin_trgm_ops, got {opclasses!r}"
