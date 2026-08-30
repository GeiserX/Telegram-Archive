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

    async def test_cursor_never_moves_backwards(self, real_adapter):
        """last_message_id is a high-water mark: the backup reads it as min_id for
        the next incremental pass, so importing an older export — which calls
        update_sync_status with that export's smaller max id — must not drag the
        checkpoint down and re-fetch everything above it from Telegram."""
        await _seed_chat(real_adapter, 900009)

        await real_adapter.update_sync_status(900009, 500, 10, account_id=1)
        await real_adapter.update_sync_status(900009, 400, 5, account_id=1)
        assert await real_adapter.get_last_message_id(900009, account_id=1) == 500

        # Forward motion still moves the mark, and the count kept accumulating.
        await real_adapter.update_sync_status(900009, 600, 5, account_id=1)
        assert await real_adapter.get_last_message_id(900009, account_id=1) == 600
        async with real_adapter.db_manager.async_session_factory() as session:
            row = (
                (await session.execute(SyncStatus.__table__.select().where(SyncStatus.chat_id == 900009)))
                .mappings()
                .one()
            )
        assert row["message_count"] == 20


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

        #301: this is what lets the planner satisfy a leading-wildcard ILIKE
        from a bitmap index scan instead of reading every row, so the search
        stops scaling linearly with the table. Checked
        against the catalog (not EXPLAIN) deliberately - on a near-empty test
        table the planner can rightfully prefer a seq scan over any index
        regardless of what exists, so asserting on the *chosen plan* here
        would be a table-size-dependent flake, not a check of the fix.
        SQLite has no gin_trgm_ops equivalent (see migration 023 / models.py's
        Index() dialect kwargs), so this only runs on PostgreSQL.

        The catalog lookup is bound to ``messages`` by oid: a name alone is
        unique per schema, not per database, so an index of the same name on
        another table would otherwise answer for the one being asserted.
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
                        "AND ix.indrelid = 'messages'::regclass "
                        "GROUP BY am.amname"
                    )
                )
            ).first()

        assert row is not None, "idx_messages_text_trgm does not exist"
        index_method, opclasses = row
        assert index_method == "gin", f"expected a GIN index, got {index_method!r}"
        assert "gin_trgm_ops" in opclasses, f"expected gin_trgm_ops, got {opclasses!r}"

    async def test_trgm_index_absent_on_sqlite(self, real_adapter):
        """SQLite must not carry the trigram index at all.

        A same-name B-tree would duplicate the entire text column into an
        index SQLite's ILIKE plan can never use — permanent file growth for
        nothing — so both provisioning paths skip it there (models.py's
        ddl_if(dialect="postgresql") and migration 023's dialect guard).
        """
        if real_adapter.db_manager.engine.dialect.name == "postgresql":
            import pytest

            pytest.skip("absence pin is the SQLite half; the GIN pin covers PostgreSQL")

        import sqlalchemy as sa

        async with real_adapter.db_manager.engine.connect() as conn:
            names = await conn.run_sync(lambda c: {ix["name"] for ix in sa.inspect(c).get_indexes("messages")})
        assert "idx_messages_text_trgm" not in names


class TestPageLimitCountsMessagesRealEngine:
    """LIMIT must deliver LIMIT distinct messages.

    A message can carry several media rows (a JSON import writes
    import_{chat}_{msg} beside the live {chat}_{msg}_{type} row), and the old
    join-then-LIMIT shape returned 50 JOIN rows holding far fewer distinct
    messages — the measured case: 10 three-media heads turned a page of 50
    into 30 messages."""

    async def _seed(self, real_adapter, chat_id):
        await _seed_chat(real_adapter, chat_id)
        for n in range(1, 61):
            await real_adapter.insert_message(_message(chat_id, n, offset_minutes=n), account_id=1)
        for n in range(51, 61):
            for media_id, downloaded in (
                # msg 60 isolates the downloaded-preference: both live rows
                # pending, only the import row downloaded.
                (f"{chat_id}_{n}_photo", 0 if n == 60 else 1),
                (f"import_{chat_id}_{n}", 1),
                (f"{chat_id}_{n}_video", 0 if n == 60 else 1),
            ):
                await real_adapter.insert_media(
                    {
                        "id": media_id,
                        "message_id": n,
                        "chat_id": chat_id,
                        "type": "photo",
                        "file_path": f"{chat_id}/{media_id}.jpg",
                        "file_name": f"{media_id}.jpg",
                        "file_size": 1,
                        "mime_type": "image/jpeg",
                        "downloaded": downloaded,
                    },
                    account_id=1,
                )

    async def test_page_of_50_returns_50_distinct_messages(self, real_adapter):
        await self._seed(real_adapter, 900100)

        page = await real_adapter.get_messages_paginated(900100, limit=50)

        ids = [m["id"] for m in page]
        assert len(ids) == 50, f"page shortfall: {len(ids)}"
        assert len(set(ids)) == 50
        assert ids == list(range(60, 10, -1)), "newest-first contract broken"

    async def test_multi_row_media_attaches_one_deterministic_row(self, real_adapter):
        await self._seed(real_adapter, 900101)

        page = await real_adapter.get_messages_paginated(900101, limit=50)
        by_id = {m["id"]: m for m in page}

        # msg 60: its plain photo row is NOT downloaded — the downloaded
        # import row must win.
        assert by_id[60]["media"]["id"] == "import_900101_60"
        # msg 59: all three downloaded — lowest media id wins (digits < letters).
        assert by_id[59]["media"]["id"] == "900101_59_photo"
        # A message without media stays None.
        assert by_id[20]["media"] is None


class TestImportedMediaAddressingRealEngine:
    """#423 against real SQL on both backends.

    The rest of the #423 coverage (tests/test_import_media_addressing.py) drives
    the web layer over a hand-rolled table, so it cannot catch a predicate that
    is wrong in SQL or a tie-break the two databases order differently. This is
    the leg that can.
    """

    async def _seed(self, real_adapter, chat_id: int) -> None:
        await _seed_chat(real_adapter, chat_id)
        for n in (10, 11, 12):
            await real_adapter.insert_message(_message(chat_id, n, offset_minutes=n), account_id=1)
        rows = [
            # msg 10: imported only — the #423 case. Note the media-root-RELATIVE
            # file_path, which is the shape src/telegram_import.py really writes;
            # the older adoption fixture uses an absolute path the importer never
            # produces, which is how #310 shipped unnoticed.
            (f"import_{chat_id}_10", 10, "document", 1),
            # msg 11: the duplicate class #310 could leave behind — an import row
            # and a sweep row for one (message, type).
            (f"{chat_id}_11_document", 11, "document", 1),
            (f"import_{chat_id}_11", 11, "document", 1),
            # msg 12: swept only — the control.
            (f"{chat_id}_12_document", 12, "document", 1),
        ]
        for media_id, msg, media_type, downloaded in rows:
            await real_adapter.insert_media(
                {
                    "id": media_id,
                    "message_id": msg,
                    "chat_id": chat_id,
                    "type": media_type,
                    "file_path": f"{chat_id}/{media_id}_report.pdf",
                    "file_name": f"{media_id}_report.pdf",
                    "file_size": 1,
                    "mime_type": "application/pdf",
                    "downloaded": downloaded,
                },
                account_id=1,
            )

    async def test_an_imported_row_is_found_by_its_natural_key(self, real_adapter):
        """#423: reconstructing ``{chat}_{msg}_{type}`` found nothing for an
        imported row, so the viewer said 'Media not found' about a file that was
        on disk. Asking by column finds it whatever it is filed under."""
        await self._seed(real_adapter, 900200)

        row = await real_adapter.get_media_for_message(900200, 10, "document", account_id=1)

        assert row is not None
        assert row["id"] == "import_900200_10"

    async def test_a_swept_row_is_found_the_same_way(self, real_adapter):
        """Control: the majority case must be unaffected."""
        await self._seed(real_adapter, 900201)

        row = await real_adapter.get_media_for_message(900201, 12, "document", account_id=1)

        assert row["id"] == "900201_12_document"

    async def test_the_lookup_cannot_cross_a_chat_boundary(self, real_adapter):
        """The chat bound is a predicate now, not a substring of a key the
        caller happened to mint. get_media_by_id is account-scoped ONLY, so this
        is the property that keeps one chat's ref from naming another's bytes."""
        await self._seed(real_adapter, 900202)
        await self._seed(real_adapter, 900203)

        row = await real_adapter.get_media_for_message(900203, 10, "document", account_id=1)

        assert row["id"] == "import_900203_10"  # never 900202's row

    async def test_a_wrong_type_finds_nothing(self, real_adapter):
        """The import id carries no type, so type must come from the column or
        ``{msg}_anything`` would address it."""
        await self._seed(real_adapter, 900204)

        assert await real_adapter.get_media_for_message(900204, 10, "video", account_id=1) is None

    async def test_a_duplicate_pair_picks_the_row_the_message_list_shows(self, real_adapter):
        """get_messages attaches (downloaded desc, id asc); the byte route must
        agree or the player and the gallery show different files. Both backends
        must order the tie identically — that is why this test is here."""
        await self._seed(real_adapter, 900205)

        row = await real_adapter.get_media_for_message(900205, 11, "document", account_id=1)

        assert row["id"] == "900205_11_document"  # digits sort below letters

    async def test_a_full_gallery_walk_visits_every_row_and_skips_none(self, real_adapter):
        """The gallery cursor is now the natural key, which the duplicate class
        above makes ambiguous. Backward is the direction 'load more' uses: it
        must be exact. Losing a row is the failure that matters — the walk is
        how a viewer reaches media, so a skipped item is invisible."""
        await self._seed(real_adapter, 900206)

        seen: list[str] = []
        key = None
        for _ in range(6):
            page = await real_adapter.get_media_paginated(
                900206, limit=2, account_id=1, **({"before_key": key} if key else {})
            )
            if not page["items"]:
                break
            seen += [i["id"] for i in page["items"]]
            last = page["items"][-1]
            key = (last["message_id"], last["type"])
            if not page["has_more"]:
                break

        assert len(seen) == len(set(seen)), f"the walk re-served a row: {seen}"
        assert set(seen) == {
            "import_900206_10",
            "900206_11_document",
            "import_900206_11",
            "900206_12_document",
        }, f"the walk skipped a row: {seen}"
