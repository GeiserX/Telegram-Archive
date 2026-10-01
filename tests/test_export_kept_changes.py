"""Both chat exports say which messages were deleted or edited, with every kept version.

An export of an archive has to carry what the viewer shows beside the text:
``is_deleted`` and ``deleted_at`` on a message deleted in Telegram (and kept
here), ``edit_date``, and ``versions``, the earlier texts the archive kept, on
the message they belong to. Without them an exported chat reads as a live
Telegram chat. Runs on SQLite and PostgreSQL (``real_adapter``).
"""

import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_archive.db.adapter import DatabaseAdapter
from telegram_archive.db.models import MessageVersion
from telegram_archive.export_backup import BackupExporter

CHAT = -420800001
SENT = datetime(2026, 2, 3, 10, 0, 0)
FIRST_EDIT = SENT + timedelta(minutes=2)
SECOND_EDIT = SENT + timedelta(minutes=5)
DELETED = SENT + timedelta(hours=1)


async def _message(adapter, message_id: int, text: str, *, when: datetime = SENT, account_id: int = 1) -> None:
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Group"}, account_id=account_id)
    await adapter.insert_message(
        {"id": message_id, "chat_id": CHAT, "text": text, "date": when, "sender_name": "Fixture Sender"},
        account_id=account_id,
    )


async def _seed(adapter) -> None:
    """Message 1 edited twice, 2 deleted in Telegram, 3 untouched; account 2 edits its own message 1."""
    await _message(adapter, 1, "first draft")
    await adapter.update_message_text(CHAT, 1, "second draft", FIRST_EDIT, account_id=1)
    await adapter.update_message_text(CHAT, 1, "final text", SECOND_EDIT, account_id=1)
    await _message(adapter, 2, "gone from Telegram")
    await adapter.mark_message_deleted(CHAT, 2, DELETED, account_id=1)
    await _message(adapter, 3, "never changed")
    await _message(adapter, 1, "other account draft", account_id=2)
    await adapter.update_message_text(CHAT, 1, "other account final", FIRST_EDIT, account_id=2)


async def _store_backfilled_version(
    adapter, message_id: int, text: str, when: datetime, *, row_id: int | None = None
) -> None:
    """A version stored after the others whose Telegram date is older, as a late backfill stores it."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            MessageVersion(
                id=row_id,
                account_id=1,
                chat_id=CHAT,
                message_id=message_id,
                text=text,
                date=when,
                change_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
        await session.commit()


async def _seed_date_order_against_id_order(adapter) -> None:
    """Messages 4 and 5, both edited, where 5 was sent before 4: date order and id order disagree."""
    await _message(adapter, 4, "four, first text", when=SENT + timedelta(hours=2))
    await adapter.update_message_text(CHAT, 4, "four, final text", SENT + timedelta(hours=3), account_id=1)
    await _message(adapter, 5, "five, first text", when=SENT + timedelta(hours=1))
    await adapter.update_message_text(CHAT, 5, "five, final text", SENT + timedelta(hours=3), account_id=1)


def _edit_after_first_read(adapter, method: str):
    """Edit message 3 through another session right after the export's first read.

    ``method`` is the AsyncSession method the export reads with: ``stream``
    for the viewer's export, ``execute`` for the command's.
    """
    original = getattr(AsyncSession, method)
    edited: list[bool] = []

    async def read_then_edit(session, statement, *args, **kwargs):
        result = await original(session, statement, *args, **kwargs)
        if isinstance(statement, Select) and not edited:
            edited.append(True)
            await adapter.update_message_text(CHAT, 3, "changed during export", DELETED, account_id=1)
        return result

    return patch.object(AsyncSession, method, read_then_edit)


class TestViewerExport:
    async def test_marks_deleted_and_edited_messages_with_every_kept_version(self, real_adapter):
        await _seed(real_adapter)
        exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}

        edited = exported[1]
        assert edited["text"] == "final text"
        assert edited["edit_date"] == SECOND_EDIT.isoformat()
        assert edited["is_deleted"] is False
        assert edited["deleted_at"] is None
        # Oldest first, as the viewer's timeline runs; the other account's
        # history of its own message 1 stays out.
        assert [(v["text"], v["date"]) for v in edited["versions"]] == [
            ("first draft", SENT.isoformat()),
            ("second draft", FIRST_EDIT.isoformat()),
        ]
        assert all(isinstance(v["captured_at"], str) for v in edited["versions"])

        deleted = exported[2]
        assert deleted["text"] == "gone from Telegram"
        assert deleted["is_deleted"] is True
        assert deleted["deleted_at"] == DELETED.isoformat()
        assert deleted["versions"] == []

        plain = exported[3]
        assert (plain["is_deleted"], plain["deleted_at"], plain["edit_date"], plain["versions"]) == (
            False,
            None,
            None,
            [],
        )
        json.dumps(list(exported.values()))  # serialisable as the route streams it

    async def test_carries_the_hidden_edit_flag_as_the_command_does(self, real_adapter):
        """A reaction moves edit_date and sets edit_hide: without the flag the row reads as edited."""
        await real_adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Group"}, account_id=1)
        for message_id, edit_hide in ((6, 1), (7, 0), (8, None)):
            await real_adapter.insert_message(
                {
                    "id": message_id,
                    "chat_id": CHAT,
                    "text": f"message {message_id}",
                    "date": SENT,
                    "edit_date": FIRST_EDIT,
                    "edit_hide": edit_hide,
                    "sender_name": "Fixture Sender",
                },
                account_id=1,
            )
        viewer = {m["id"]: m["edit_hide"] async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}
        command = {m["id"]: m["edit_hide"] for m in await real_adapter.get_messages_for_backup_export(CHAT)}
        assert viewer == {6: 1, 7: 0, 8: None}
        assert viewer == command

    async def test_unscoped_export_gives_each_account_its_own_versions(self, real_adapter):
        await _seed(real_adapter)
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT) if m["id"] == 1]
        by_text = {m["text"]: [v["text"] for v in m["versions"]] for m in exported}
        assert by_text == {
            "final text": ["first draft", "second draft"],
            "other account final": ["other account draft"],
        }

    async def test_an_account_whose_message_sorts_after_another_accounts_keeps_its_versions(self, real_adapter):
        """Account 1's message 5 and account 2's message 1 share a date; the account decides their order."""
        await _seed(real_adapter)
        await _message(real_adapter, 5, "account one draft")
        await real_adapter.update_message_text(CHAT, 5, "account one final", FIRST_EDIT, account_id=1)
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT)]
        by_text = {m["text"]: [v["text"] for v in m["versions"]] for m in exported}
        assert by_text["account one final"] == ["account one draft"]
        assert by_text["other account final"] == ["other account draft"]
        assert by_text["final text"] == ["first draft", "second draft"]

    async def test_versions_stay_on_their_message_when_date_order_and_id_order_disagree(self, real_adapter):
        """Imported history has old dates on high ids: message 5 is listed before message 4."""
        await _seed_date_order_against_id_order(real_adapter)
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert [(m["id"], [v["text"] for v in m["versions"]]) for m in exported] == [
            (5, ["five, first text"]),
            (4, ["four, first text"]),
        ]

    async def test_versions_out_of_step_with_the_messages_fail_the_export(self, real_adapter):
        """Should the two queries ever sort apart, the export fails instead of dropping versions."""
        await _seed_date_order_against_id_order(real_adapter)
        original = real_adapter._versions_of_messages_query

        def by_version_id(conditions):
            return original(conditions).order_by(None).order_by(MessageVersion.id.asc())

        desynced = patch.object(real_adapter, "_versions_of_messages_query", by_version_id)
        with desynced, pytest.raises(RuntimeError, match="out of step"):
            _ = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]

    async def test_a_windowed_export_keeps_versions_dated_after_the_window(self, real_adapter):
        """The window picks messages by their date; a message in it keeps all its versions."""
        await _seed(real_adapter)
        window = {"from_date": SENT + timedelta(minutes=1), "to_date": SENT + timedelta(days=1)}
        await _message(real_adapter, 4, "inside", when=SENT + timedelta(minutes=30))
        await real_adapter.update_message_text(CHAT, 4, "edited later", SENT + timedelta(days=2), account_id=1)
        await real_adapter.update_message_text(CHAT, 4, "edited again", SENT + timedelta(days=3), account_id=1)
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1, **window)]
        assert [(m["id"], [v["text"] for v in m["versions"]]) for m in exported] == [(4, ["inside", "edited later"])]

    async def test_only_the_current_messages_versions_are_read_into_memory(self, real_adapter):
        """Versions stream beside the messages: a long edit history is never read in one go."""
        edits = {1: 2, 2: 30, 3: 1}
        for message_id, count in edits.items():
            await _message(real_adapter, message_id, f"draft {message_id}.0", when=SENT + timedelta(minutes=message_id))
            for n in range(1, count + 1):
                await real_adapter.update_message_text(
                    CHAT,
                    message_id,
                    f"draft {message_id}.{n}",
                    SENT + timedelta(hours=message_id, seconds=n),
                    account_id=1,
                )
        counted = patch.object(DatabaseAdapter, "_export_version_dict", wraps=DatabaseAdapter._export_version_dict)
        with counted as built:
            stream = real_adapter.get_messages_for_export(CHAT, account_id=1)
            exported = [await anext(stream)]
            assert built.call_count == 2  # message 1's versions, none of message 2's yet
            exported.append(await anext(stream))
            assert built.call_count == 32
            exported += [m async for m in stream]
        assert [(m["id"], len(m["versions"])) for m in exported] == [(1, 2), (2, 30), (3, 1)]
        assert [v["text"] for v in exported[1]["versions"]] == [f"draft 2.{n}" for n in range(30)]

    async def test_versions_come_in_telegram_date_order_not_capture_order(self, real_adapter):
        await _seed(real_adapter)
        await _store_backfilled_version(real_adapter, 1, "backfilled", SENT - timedelta(minutes=1))
        exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}
        assert [v["text"] for v in exported[1]["versions"]] == ["backfilled", "first draft", "second draft"]

    async def test_versions_with_one_date_come_in_the_order_the_archive_stored_them(self, real_adapter):
        """The row id breaks a tie on the Telegram date, whatever order the database returns the rows in."""
        await _seed(real_adapter)
        earlier = SENT - timedelta(minutes=1)
        await _store_backfilled_version(real_adapter, 1, "stored second", earlier, row_id=900002)
        await _store_backfilled_version(real_adapter, 1, "stored first", earlier, row_id=900001)
        exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}
        assert [v["text"] for v in exported[1]["versions"]] == [
            "stored first",
            "stored second",
            "first draft",
            "second draft",
        ]

    async def test_a_message_with_two_media_is_listed_once_with_its_versions(self, real_adapter):
        await _seed(real_adapter)
        for media_id in ("fixture-a", "fixture-b"):
            await real_adapter.insert_media(
                {"id": media_id, "message_id": 1, "chat_id": CHAT, "type": "photo"}, account_id=1
            )
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)]
        assert [(m["id"], len(m["media"]), [v["text"] for v in m["versions"]]) for m in exported] == [
            (1, 2, ["first draft", "second draft"]),
            (2, 0, []),
            (3, 0, []),
        ]

    async def test_an_edit_during_the_export_cannot_make_a_message_disagree_with_its_versions(self, real_adapter):
        """The messages and their versions come from one snapshot, whatever a backup writes meanwhile.

        Only the [postgresql] case proves ``_read_one_snapshot`` here. On
        SQLite the messages statement stays open while the versions are read,
        and that open statement already holds one read snapshot, so the
        [sqlite] case passes without it. The command's test below proves the
        snapshot on SQLite.
        """
        await _seed(real_adapter)
        with _edit_after_first_read(real_adapter, "stream"):
            exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}
        plain = exported[3]
        assert (plain["text"], plain["edit_date"], plain["versions"]) == ("never changed", None, [])


class TestCliExport:
    async def test_marks_deleted_and_edited_messages_with_every_kept_version(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(str(output), chat_id=CHAT)
        data = json.loads(output.read_text(encoding="utf-8"))
        by_key = {(m["account_id"], m["id"]): m for m in data["messages"]}

        edited = by_key[(1, 1)]
        # One date format in the whole file: the versions' dates are written
        # like the message's own, so they match as strings.
        assert edited["edit_date"] == str(SECOND_EDIT)
        assert [(v["text"], v["date"]) for v in edited["versions"]] == [
            ("first draft", str(SENT)),
            ("second draft", str(FIRST_EDIT)),
        ]
        # 9.0 dropped the flat list: every version sits under its message.
        assert "message_versions" not in data
        assert all(str(datetime.fromisoformat(v["captured_at"])) == v["captured_at"] for v in edited["versions"])
        assert [v["text"] for v in by_key[(2, 1)]["versions"]] == ["other account draft"]

        deleted = by_key[(1, 2)]
        assert deleted["is_deleted"] == 1
        assert deleted["deleted_at"] is not None
        assert deleted["versions"] == []
        assert by_key[(1, 3)]["versions"] == []
        # The versions listed under the messages.
        assert data["statistics"]["total_message_versions"] == 3

    async def test_a_windowed_export_keeps_versions_dated_after_the_window(self, real_adapter, tmp_path):
        """The window picks messages by their date; a message in it keeps all its versions."""
        await _message(real_adapter, 4, "inside", when=datetime(2026, 2, 5, 10, 0))
        await real_adapter.update_message_text(CHAT, 4, "edited later", datetime(2026, 3, 1), account_id=1)
        await real_adapter.update_message_text(CHAT, 4, "edited again", datetime(2026, 3, 2), account_id=1)
        await _message(real_adapter, 5, "outside", when=datetime(2026, 2, 9, 10, 0))
        await real_adapter.update_message_text(CHAT, 5, "outside, edited", datetime(2026, 3, 1), account_id=1)
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(
            str(output), chat_id=CHAT, start_date="2026-02-04", end_date="2026-02-08"
        )
        data = json.loads(output.read_text(encoding="utf-8"))
        assert [(m["id"], [v["text"] for v in m["versions"]]) for m in data["messages"]] == [
            (4, ["inside", "edited later"])
        ]

    async def test_versions_come_in_telegram_date_order_not_capture_order(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        await _store_backfilled_version(real_adapter, 1, "backfilled", SENT - timedelta(minutes=1))
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(str(output), chat_id=CHAT)
        data = json.loads(output.read_text(encoding="utf-8"))
        edited = next(m for m in data["messages"] if (m["account_id"], m["id"]) == (1, 1))
        assert [v["text"] for v in edited["versions"]] == ["backfilled", "first draft", "second draft"]

    async def test_an_edit_during_the_export_cannot_make_a_message_disagree_with_its_versions(
        self, real_adapter, tmp_path
    ):
        """Messages, their versions, their media and their transcripts come from one snapshot."""
        await _seed(real_adapter)
        output = tmp_path / "export.json"
        with _edit_after_first_read(real_adapter, "execute"):
            await BackupExporter(real_adapter).export_to_json(str(output), chat_id=CHAT)
        data = json.loads(output.read_text(encoding="utf-8"))
        plain = next(m for m in data["messages"] if (m["account_id"], m["id"]) == (1, 3))
        assert (plain["text"], plain["edit_date"], plain["versions"]) == ("never changed", None, [])
        assert data["statistics"]["total_message_versions"] == 3
