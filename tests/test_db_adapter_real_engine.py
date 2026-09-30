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

import json
from datetime import datetime, timedelta

from telegram_archive.db.models import SyncStatus

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


class TestEditHideRealEngine:
    """Telegram's edit_hide flag: kept beside edit_date, and a hidden edit with no
    kept version is not an edit in the count, the "Edited only" list or the rows."""

    async def test_a_hidden_edit_with_no_kept_version_is_not_counted(self, real_adapter):
        chat = 900050
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)

        def captured(message_id: int, text: str, *, edit_hide: int | None) -> dict:
            row = _message(chat, message_id, text=text, offset_minutes=message_id)
            row["edit_date"] = edited_at
            if edit_hide is not None:
                row["edit_hide"] = edit_hide
            return row

        # 1: first captured after a reaction; Telegram bumped edit_date and hid it.
        await real_adapter.insert_message(captured(1, "reacted to", edit_hide=1), account_id=1)
        # 2: first captured after a real edit; Telegram shows it.
        await real_adapter.insert_message(captured(2, "edited before capture", edit_hide=0), account_id=1)
        # 3: a row with no flag (from before the column, or an import): shown, as before.
        await real_adapter.insert_message(captured(3, "no flag", edit_hide=None), account_id=1)
        # 4: hidden at first, then a real text edit arrives with the flag clear.
        await real_adapter.insert_message(captured(4, "before", edit_hide=1), account_id=1)
        outcome, _ = await real_adapter.update_message_text(
            chat, 4, "after", edited_at + timedelta(minutes=1), account_id=1, edit_hide=0
        )
        assert outcome == "applied"
        # 5: hidden, and an empty text later filled in: a kept version, so edited.
        await real_adapter.insert_message(captured(5, "", edit_hide=1), account_id=1)
        await real_adapter.insert_message(_message(chat, 5, text="filled", offset_minutes=5), account_id=1)
        # 6: hidden; a re-scan with the same text and another flag writes nothing.
        await real_adapter.insert_message(captured(6, "same", edit_hide=1), account_id=1)
        rescan = captured(6, "same", edit_hide=0)
        rescan["edit_date"] = edited_at + timedelta(minutes=9)
        await real_adapter.insert_message(rescan, account_id=1)
        # 7: never edited.
        await real_adapter.insert_message(_message(chat, 7, text="plain", offset_minutes=7), account_id=1)

        stats = await real_adapter.get_chat_stats(chat, account_id=1, with_kept_changes=True)
        assert stats["edited_messages"] == 4
        listed = await real_adapter.get_messages_paginated(chat, account_id=1, edited_only=True)
        assert sorted(row["id"] for row in listed) == [2, 3, 4, 5]

        rows = {row["id"]: row for row in await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)}
        # The raw fields stay as captured: the date is kept, the flag beside it.
        assert rows[1]["edit_date"] == edited_at
        assert rows[1]["edit_hide"] == 1
        assert rows[3]["edit_hide"] is None
        assert rows[4]["edit_hide"] == 0
        assert rows[5]["edit_hide"] == 1
        assert rows[6]["edit_hide"] == 1
        assert rows[6]["edit_date"] == edited_at

    async def test_a_later_read_of_the_same_edit_fills_an_unknown_flag(self, real_adapter):
        """A row archived before 034 has edit_date and no flag, so a reaction
        Telegram hid still counts as an edit. A backup read of the same edit
        (same edit_date) fills the flag; it replaces no known flag and no text."""
        chat = 900051
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)

        def legacy(message_id: int, text: str) -> dict:
            row = _message(chat, message_id, text=text, offset_minutes=message_id)
            row["edit_date"] = edited_at
            return row

        # 1: legacy row; a re-scan or gap fill reads the same edit with the flag.
        await real_adapter.insert_message(legacy(1, "reacted"), account_id=1)
        await real_adapter.insert_message({**legacy(1, "reacted"), "edit_hide": 1}, account_id=1)
        # 2: legacy row; the sync pass fills it through fill_edit_hide.
        await real_adapter.insert_message(legacy(2, "reacted too"), account_id=1)
        assert await real_adapter.get_unflagged_edit_ids(chat, account_id=1) == {2}
        assert await real_adapter.fill_edit_hide(chat, 2, edited_at, 1, account_id=1) is True
        # 3: known flag; neither path replaces it beside the same date, and an
        # import (no flag) that fills an empty text does not blank it either.
        await real_adapter.insert_message({**legacy(3, ""), "edit_hide": 1}, account_id=1)
        await real_adapter.insert_message({**legacy(3, "filled")}, account_id=1)
        await real_adapter.insert_message({**legacy(3, "filled"), "edit_hide": 0}, account_id=1)
        assert await real_adapter.fill_edit_hide(chat, 3, edited_at, 0, account_id=1) is False
        # 4: legacy row; a read of ANOTHER edit_date is not the same edit: no fill.
        await real_adapter.insert_message(legacy(4, "kept"), account_id=1)
        assert await real_adapter.fill_edit_hide(chat, 4, edited_at + timedelta(minutes=1), 1, account_id=1) is False

        assert await real_adapter.get_unflagged_edit_ids(chat, account_id=1) == {4}
        rows = {row["id"]: row for row in await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)}
        assert (rows[1]["edit_hide"], rows[1]["text"], rows[1]["edit_date"]) == (1, "reacted", edited_at)
        assert (rows[2]["edit_hide"], rows[2]["text"], rows[2]["edit_date"]) == (1, "reacted too", edited_at)
        assert (rows[3]["edit_hide"], rows[3]["text"]) == (1, "filled")
        assert rows[4]["edit_hide"] is None
        stats = await real_adapter.get_chat_stats(chat, account_id=1, with_kept_changes=True)
        # 3 (a filled empty text keeps a version) and 4 (still unknown) are edited.
        assert stats["edited_messages"] == 2
        assert await real_adapter.get_message_versions(chat, 1, account_id=1) == []
        assert await real_adapter.get_message_versions(chat, 2, account_id=1) == []


class TestOlderReadTextRealEngine:
    """The backup reads a message, and before its batch is written an edit
    reaches the listener, which stores the newer text first. The batch's older
    text must not be thrown away: it is kept as a version."""

    async def test_backup_batch_after_a_listener_edit_keeps_the_older_text(self, real_adapter):
        chat = 900052
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)

        listener_row = _message(chat, 1, text="second text")
        listener_row["edit_date"] = edited_at
        await real_adapter.insert_message(listener_row, account_id=1)
        backup_row = {**_message(chat, 1, text="first text"), "edit_date": None, "keeps_older_text": True}
        await real_adapter.insert_messages_batch([backup_row], account_id=1)
        # The same read written twice records one version (change_hash).
        await real_adapter.insert_messages_batch([backup_row], account_id=1)

        rows = await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)
        assert (rows[0]["text"], rows[0]["edit_date"]) == ("second text", edited_at)
        versions = await real_adapter.get_message_versions(chat, 1, account_id=1)
        assert [(v["text"], v["date"]) for v in versions] == [("first text", BASE_DATE)]

    async def test_an_import_does_not_turn_its_older_text_into_a_version(self, real_adapter):
        """An import renders text its own way, so a differing older text from it
        is refused and recorded nowhere, as before."""
        chat = 900053
        await _seed_chat(real_adapter, chat)

        stored = _message(chat, 1, text="second text")
        stored["edit_date"] = BASE_DATE + timedelta(minutes=5)
        await real_adapter.insert_message(stored, account_id=1)
        await real_adapter.insert_message(_message(chat, 1, text="first **text**"), account_id=1)

        rows = await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)
        assert rows[0]["text"] == "second text"
        assert await real_adapter.get_message_versions(chat, 1, account_id=1) == []

    async def test_a_read_as_new_as_the_archive_keeps_no_extra_version(self, real_adapter):
        """Never-edited rows re-read with a differing text (both without an edit
        date) are not a race: the archive has no evidence of a newer edit."""
        chat = 900054
        await _seed_chat(real_adapter, chat)

        await real_adapter.insert_message(_message(chat, 1, text="as stored"), account_id=1)
        await real_adapter.insert_messages_batch(
            [{**_message(chat, 1, text="as rendered now"), "keeps_older_text": True}], account_id=1
        )

        assert await real_adapter.get_message_versions(chat, 1, account_id=1) == []


BOLD = [{"type": "bold", "offset": 0, "length": 4}]
ITALIC = [{"type": "italic", "offset": 8, "length": 4}]


def _formatted(chat_id: int, message_id: int, text: str, entities: list | None, **extra) -> dict:
    row = _message(chat_id, message_id, text=text)
    row["raw_data"] = {"entities": entities} if entities else {}
    row.update(extra)
    return row


async def _raw(adapter, chat_id: int, message_id: int) -> dict:
    rows = await adapter.get_messages_paginated(chat_id, account_id=1, limit=10)
    raw = next(row for row in rows if row["id"] == message_id)["raw_data"]
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


async def _raw_entities(adapter, chat_id: int, message_id: int) -> list | None:
    return (await _raw(adapter, chat_id, message_id)).get("entities")


# Rich Text Editor block trees (#470): a heading at level 1, then at level 2.
# Both render as the same bold text, so only the tree tells them apart.
TREE_1 = {"blocks": [{"_": "PageBlockHeader", "text": {"_": "TextPlain", "text": "Meet at nine"}, "level": 1}]}
TREE_2 = {"blocks": [{"_": "PageBlockHeader", "text": {"_": "TextPlain", "text": "Meet at nine"}, "level": 2}]}


class TestVersionFormattingRealEngine:
    """Every version keeps its own formatting and names the path that saw it.

    An edit that changes only the formatting is an edit: it writes a version
    and moves edit_date. A reaction (an edit Telegram hides) is not. Nothing
    replaces archived formatting except an edit, which first moves it into the
    version it supersedes."""

    async def test_a_formatting_only_edit_writes_a_version_with_the_old_formatting(self, real_adapter):
        chat = 900060
        await _seed_chat(real_adapter, chat)
        await real_adapter.insert_message(_formatted(chat, 1, "Meet at nine", BOLD), account_id=1)

        edited_at = BASE_DATE + timedelta(minutes=5)
        outcome, prior = await real_adapter.update_message_text(
            chat,
            1,
            "Meet at nine",
            edited_at,
            account_id=1,
            edit_hide=0,
            entities=ITALIC,
            update_entities=True,
            source="listener",
        )

        assert outcome == "applied"
        assert prior["text"] == "Meet at nine"
        versions = await real_adapter.get_message_versions(chat, 1, account_id=1)
        assert [(v["text"], v["date"], v["entities"], v["source"]) for v in versions] == [
            ("Meet at nine", BASE_DATE, BOLD, "listener")
        ]
        assert isinstance(versions[0]["captured_at"], datetime)
        rows = await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)
        assert rows[0]["edit_date"] == edited_at
        assert await _raw_entities(real_adapter, chat, 1) == ITALIC
        stats = await real_adapter.get_chat_stats(chat, account_id=1, with_kept_changes=True)
        assert stats["edited_messages"] == 1

    async def test_a_text_edit_keeps_the_old_formatting_in_its_version(self, real_adapter):
        chat = 900061
        await _seed_chat(real_adapter, chat)
        await real_adapter.insert_message(_formatted(chat, 1, "Meet at nine", BOLD), account_id=1)

        outcome, _ = await real_adapter.update_message_text(
            chat,
            1,
            "Meet at ten",
            BASE_DATE + timedelta(minutes=5),
            account_id=1,
            edit_hide=0,
            entities=None,
            update_entities=True,
            source="sync",
        )

        assert outcome == "applied"
        versions = await real_adapter.get_message_versions(chat, 1, account_id=1)
        assert [(v["text"], v["entities"], v["source"]) for v in versions] == [("Meet at nine", BOLD, "sync")]
        assert await _raw_entities(real_adapter, chat, 1) is None

    async def test_a_hidden_or_repeated_formatting_change_replaces_nothing(self, real_adapter):
        chat = 900062
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)
        # 1: formatted; an edit Telegram hides (a reaction) carries other entities.
        await real_adapter.insert_message(_formatted(chat, 1, "Meet at nine", BOLD), account_id=1)
        # 2: formatted and edited at edited_at; the sync reading the same edit_date
        # again reads the same edit (a live event at the same second is covered below).
        await real_adapter.insert_message(
            _formatted(chat, 2, "Meet at nine", BOLD, edit_date=edited_at, edit_hide=0), account_id=1
        )
        # 3: archived before formatting was kept; a hidden edit fills the missing key.
        await real_adapter.insert_message(_formatted(chat, 3, "Meet at nine", None), account_id=1)

        for message_id, edit_hide, source in ((1, 1, "listener"), (2, 0, "sync"), (3, 1, "listener")):
            outcome, _ = await real_adapter.update_message_text(
                chat,
                message_id,
                "Meet at nine",
                edited_at,
                account_id=1,
                edit_hide=edit_hide,
                entities=ITALIC,
                update_entities=True,
                source=source,
            )
            assert outcome == "noop"

        assert await _raw_entities(real_adapter, chat, 1) == BOLD
        assert await _raw_entities(real_adapter, chat, 2) == BOLD
        assert await _raw_entities(real_adapter, chat, 3) == ITALIC
        for message_id in (1, 2, 3):
            assert await real_adapter.get_message_versions(chat, message_id, account_id=1) == []

    async def test_a_backup_read_of_a_formatting_edit_writes_a_version(self, real_adapter):
        chat = 900063
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)
        await real_adapter.insert_message(_formatted(chat, 1, "Meet at nine", BOLD), account_id=1)
        # 2 carries other extras (a link preview): they stay when the formatting goes.
        seeded = _formatted(chat, 2, "Meet at nine", BOLD)
        seeded["raw_data"]["webpage"] = {"url": "https://keep.example"}
        await real_adapter.insert_message(seeded, account_id=1)

        # 1: a gap fill or re-scan reads the message edited, formatting only.
        read = _formatted(chat, 1, "Meet at nine", ITALIC, edit_date=edited_at, edit_hide=0, version_source="backup")
        await real_adapter.insert_messages_batch([read], account_id=1)
        # 2: a read with no extras at all ("{}") is still the whole message: bold removed.
        plain = _formatted(chat, 2, "Meet at nine", None, edit_date=edited_at, edit_hide=0, version_source="backup")
        await real_adapter.insert_messages_batch([plain], account_id=1)

        for message_id, now in ((1, ITALIC), (2, None)):
            versions = await real_adapter.get_message_versions(chat, message_id, account_id=1)
            assert [(v["text"], v["date"], v["entities"], v["source"]) for v in versions] == [
                ("Meet at nine", BASE_DATE, BOLD, "backup")
            ]
            assert await _raw_entities(real_adapter, chat, message_id) == now
        rows = {row["id"]: row for row in await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)}
        assert rows[1]["edit_date"] == edited_at
        assert rows[2]["edit_date"] == edited_at
        assert await _raw(real_adapter, chat, 2) == {"webpage": {"url": "https://keep.example"}}

    async def test_a_backup_read_of_a_text_edit_with_no_extras_drops_the_old_formatting(self, real_adapter):
        """A read of an edit that removed every formatting mark serialises to
        "{}". The new text must not keep the old entities; they move into the
        version, and the other archived extras stay."""
        chat = 900065
        await _seed_chat(real_adapter, chat)
        seeded = _formatted(chat, 1, "Meet at nine", BOLD)
        seeded["raw_data"]["webpage"] = {"url": "https://keep.example"}
        await real_adapter.insert_message(seeded, account_id=1)

        read = _formatted(
            chat,
            1,
            "Plan at ten!",
            None,
            edit_date=BASE_DATE + timedelta(minutes=5),
            edit_hide=0,
            version_source="backup",
        )
        await real_adapter.insert_messages_batch([read], account_id=1)

        rows = await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)
        assert rows[0]["text"] == "Plan at ten!"
        assert await _raw(real_adapter, chat, 1) == {"webpage": {"url": "https://keep.example"}}
        versions = await real_adapter.get_message_versions(chat, 1, account_id=1)
        assert [(v["text"], v["entities"], v["source"]) for v in versions] == [("Meet at nine", BOLD, "backup")]

    async def test_a_read_that_is_not_a_formatting_edit_keeps_the_archived_formatting(self, real_adapter):
        """A hidden edit (a reaction), a read at the same edit_date, and an
        import with another block tree are not edits: no version, and the
        archived formatting stays."""
        chat = 900066
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)
        await real_adapter.insert_message(_formatted(chat, 1, "Meet at nine", BOLD), account_id=1)
        await real_adapter.insert_message(
            _formatted(chat, 2, "Meet at nine", BOLD, edit_date=edited_at, edit_hide=0), account_id=1
        )
        tree_row = _formatted(chat, 3, "Meet at nine", BOLD)
        tree_row["raw_data"]["rich_message"] = TREE_1
        await real_adapter.insert_message(tree_row, account_id=1)

        # 1: a newer edit_date Telegram hides, with other entities.
        hidden = _formatted(chat, 1, "Meet at nine", ITALIC, edit_date=edited_at, edit_hide=1, version_source="backup")
        # 2: the archived edit_date again, with other entities.
        same_date = _formatted(
            chat, 2, "Meet at nine", ITALIC, edit_date=edited_at, edit_hide=0, version_source="backup"
        )
        # 3: an import with another block tree and a newer date.
        imported = _formatted(chat, 3, "Meet at nine", BOLD, edit_date=edited_at, version_source="import")
        imported["raw_data"]["rich_message"] = TREE_2
        await real_adapter.insert_messages_batch([hidden, same_date, imported], account_id=1)

        for message_id in (1, 2, 3):
            assert await _raw_entities(real_adapter, chat, message_id) == BOLD
            assert await real_adapter.get_message_versions(chat, message_id, account_id=1) == []
        assert (await _raw(real_adapter, chat, 3))["rich_message"] == TREE_1

    async def test_an_edit_keeps_the_old_block_tree_in_its_version(self, real_adapter):
        """A Rich Text Editor message's block tree is formatting too: a text
        edit and an edit of the tree alone both keep the old tree in the
        version, and the tree alone is an edit, not a silent drop."""
        chat = 900067
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)
        for message_id in (1, 2, 3):
            row = _formatted(chat, message_id, "Meet at nine", BOLD)
            row["raw_data"]["rich_message"] = TREE_1
            await real_adapter.insert_message(row, account_id=1)
        row = _formatted(chat, 4, "Meet at nine", BOLD, edit_date=edited_at, edit_hide=0)
        row["raw_data"]["rich_message"] = TREE_1
        await real_adapter.insert_message(row, account_id=1)

        # 1: the listener sees a text edit with a new tree.
        outcome, _ = await real_adapter.update_message_text(
            chat,
            1,
            "Meet at ten",
            edited_at,
            account_id=1,
            edit_hide=0,
            entities=BOLD,
            update_entities=True,
            rich_message=TREE_2,
            source="listener",
        )
        assert outcome == "applied"
        # 2: the listener sees an edit of the tree alone: same text, same entities.
        outcome, _ = await real_adapter.update_message_text(
            chat,
            2,
            "Meet at nine",
            edited_at,
            account_id=1,
            edit_hide=0,
            entities=BOLD,
            update_entities=True,
            rich_message=TREE_2,
            source="listener",
        )
        assert outcome == "applied"
        # 3: a backup read of an edit of the tree alone.
        read = _formatted(chat, 3, "Meet at nine", BOLD, edit_date=edited_at, edit_hide=0, version_source="backup")
        read["raw_data"]["rich_message"] = TREE_2
        await real_adapter.insert_messages_batch([read], account_id=1)
        # 4: a tree alone at the archived edit_date is not an edit, even from the
        # listener: a tree carries file references Telegram refreshes.
        outcome, _ = await real_adapter.update_message_text(
            chat,
            4,
            "Meet at nine",
            edited_at,
            account_id=1,
            edit_hide=0,
            entities=BOLD,
            update_entities=True,
            rich_message=TREE_2,
            source="listener",
        )
        assert outcome == "noop"

        for message_id in (1, 2, 3):
            versions = await real_adapter.get_message_versions(chat, message_id, account_id=1)
            assert [(v["text"], v["entities"], v["rich_message"]) for v in versions] == [("Meet at nine", BOLD, TREE_1)]
            assert (await _raw(real_adapter, chat, message_id))["rich_message"] == TREE_2
        assert (await _raw(real_adapter, chat, 4))["rich_message"] == TREE_1

    async def test_a_version_after_a_hidden_edit_is_dated_at_the_send_time(self, real_adapter):
        """A message first archived after a reaction carries the reaction's
        hidden edit_date. The text was current since it was sent, so its
        version is dated at the send time, not at the reaction."""
        chat = 900068
        await _seed_chat(real_adapter, chat)
        row = _formatted(chat, 1, "Meet at nine", None, edit_date=BASE_DATE + timedelta(minutes=1), edit_hide=1)
        await real_adapter.insert_message(row, account_id=1)
        # The backup's older read of a hidden-edit row keeps the same rule.
        await real_adapter.insert_message(
            _formatted(chat, 2, "Meet at ten", None, edit_date=BASE_DATE + timedelta(minutes=9), edit_hide=0),
            account_id=1,
        )
        older = _formatted(
            chat,
            2,
            "Meet at nine",
            None,
            edit_date=BASE_DATE + timedelta(minutes=1),
            edit_hide=1,
            keeps_older_text=True,
            version_source="backup",
        )
        await real_adapter.insert_messages_batch([older], account_id=1)

        outcome, _ = await real_adapter.update_message_text(
            chat, 1, "Meet at ten", BASE_DATE + timedelta(minutes=5), account_id=1, edit_hide=0, source="listener"
        )

        assert outcome == "applied"
        for message_id in (1, 2):
            versions = await real_adapter.get_message_versions(chat, message_id, account_id=1)
            assert [(v["text"], v["date"]) for v in versions] == [("Meet at nine", BASE_DATE)]

    async def test_a_live_formatting_edit_within_the_same_second_applies(self, real_adapter):
        """edit_date has one-second resolution and a bot may edit twice in one
        second. A live listener event at the archived edit_date with other
        entities is the newer edit; the sync at the same date is not."""
        chat = 900069
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)
        for message_id in (1, 2):
            await real_adapter.insert_message(
                _formatted(chat, message_id, "Meet at nine!", ITALIC, edit_date=edited_at, edit_hide=0), account_id=1
            )

        outcomes = []
        for message_id, source in ((1, "listener"), (2, "sync")):
            outcome, _ = await real_adapter.update_message_text(
                chat,
                message_id,
                "Meet at nine!",
                edited_at,
                account_id=1,
                edit_hide=0,
                entities=BOLD,
                update_entities=True,
                source=source,
            )
            outcomes.append(outcome)

        assert outcomes == ["applied", "noop"]
        assert await _raw_entities(real_adapter, chat, 1) == BOLD
        versions = await real_adapter.get_message_versions(chat, 1, account_id=1)
        assert [(v["text"], v["date"], v["entities"], v["source"]) for v in versions] == [
            ("Meet at nine!", edited_at, ITALIC, "listener")
        ]
        assert await _raw_entities(real_adapter, chat, 2) == ITALIC
        assert await real_adapter.get_message_versions(chat, 2, account_id=1) == []

    async def test_an_import_or_an_older_read_keeps_the_archived_formatting(self, real_adapter):
        chat = 900064
        edited_at = BASE_DATE + timedelta(minutes=5)
        await _seed_chat(real_adapter, chat)

        # 1: an import with other extras and no entities, same text, newer date.
        await real_adapter.insert_message(_formatted(chat, 1, "Meet at nine", BOLD), account_id=1)
        imported = _formatted(chat, 1, "Meet at nine", None, edit_date=edited_at, version_source="import")
        imported["raw_data"] = {"forward_from_name": "Channel A"}
        await real_adapter.insert_messages_batch([imported], account_id=1)
        # 2: the listener stored a newer edit; the backup batch read before it arrives after.
        newer = _formatted(chat, 2, "Meet at ten", ITALIC, edit_date=edited_at, edit_hide=0)
        await real_adapter.insert_message(newer, account_id=1)
        older = _formatted(chat, 2, "Meet at nine", BOLD, keeps_older_text=True, version_source="backup")
        await real_adapter.insert_messages_batch([older], account_id=1)

        rows = {row["id"]: row for row in await real_adapter.get_messages_paginated(chat, account_id=1, limit=10)}
        assert await _raw_entities(real_adapter, chat, 1) == BOLD
        raw_1 = rows[1]["raw_data"] if isinstance(rows[1]["raw_data"], dict) else json.loads(rows[1]["raw_data"])
        assert raw_1["forward_from_name"] == "Channel A"
        assert rows[1]["edit_date"] is None
        assert await real_adapter.get_message_versions(chat, 1, account_id=1) == []

        assert (rows[2]["text"], await _raw_entities(real_adapter, chat, 2)) == ("Meet at ten", ITALIC)
        versions = await real_adapter.get_message_versions(chat, 2, account_id=1)
        assert [(v["text"], v["entities"], v["source"]) for v in versions] == [("Meet at nine", BOLD, "backup")]


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
            # file_path, which is the shape telegram_archive/telegram_import.py really writes;
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

    async def _walk(self, real_adapter, chat_id: int, limit: int) -> list[tuple[int, str]]:
        """Page the gallery the way the viewer does: send the last item's key back."""
        seen: list[tuple[int, str]] = []
        key = None
        for _ in range(10):  # a stalled cursor would spin here forever
            page = await real_adapter.get_media_paginated(
                chat_id, limit=limit, account_id=1, **({"before_key": key} if key else {})
            )
            if not page["items"]:
                break
            seen += [(i["message_id"], i["type"]) for i in page["items"]]
            last = page["items"][-1]
            key = (last["message_id"], last["type"])
            if not page["has_more"]:
                break
        return seen

    async def test_a_full_gallery_walk_visits_every_item_exactly_once(self, real_adapter):
        """The cursor is the natural key, which the duplicate class above turns
        into the name of a GROUP rather than a row. The walk must clear the whole
        group: the two twins carry the same item id and the same media URL, so
        they are one item to a viewer, and emitting both means the next cursor
        points back at a row already passed.

        Run at limit=1, which is where that goes wrong most sharply: the page
        boundary lands inside the group every time."""
        await self._seed(real_adapter, 900206)

        seen = await self._walk(real_adapter, 900206, limit=1)

        assert len(seen) == len(set(seen)), f"the walk stalled or repeated an item: {seen}"
        assert set(seen) == {(10, "document"), (11, "document"), (12, "document")}, f"the walk skipped an item: {seen}"

    async def test_the_walk_is_stable_across_page_sizes(self, real_adapter):
        """Control: a limit that never splits the group must reach the same set,
        so the test above is measuring the cursor and not the page size."""
        await self._seed(real_adapter, 900207)

        assert await self._walk(real_adapter, 900207, limit=1) == await self._walk(real_adapter, 900207, limit=2)
