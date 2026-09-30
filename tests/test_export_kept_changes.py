"""Both chat exports say which messages were deleted or edited, with every kept version.

An export of an archive has to carry what the viewer shows beside the text:
``is_deleted`` and ``deleted_at`` on a message deleted in Telegram (and kept
here), ``edit_date``, and ``versions``, the earlier texts the archive kept, on
the message they belong to. Without them an exported chat reads as a live
Telegram chat. Runs on SQLite and PostgreSQL (``real_adapter``).
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from telegram_archive.db import adapter as adapter_module
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

    async def test_unscoped_export_gives_each_account_its_own_versions(self, real_adapter):
        await _seed(real_adapter)
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT) if m["id"] == 1]
        by_text = {m["text"]: [v["text"] for v in m["versions"]] for m in exported}
        assert by_text == {
            "final text": ["first draft", "second draft"],
            "other account final": ["other account draft"],
        }

    async def test_a_windowed_export_keeps_versions_dated_after_the_window(self, real_adapter):
        """The window picks messages by their date; a message in it keeps all its versions."""
        await _seed(real_adapter)
        window = {"from_date": SENT + timedelta(minutes=1), "to_date": SENT + timedelta(days=1)}
        await _message(real_adapter, 4, "inside", when=SENT + timedelta(minutes=30))
        await real_adapter.update_message_text(CHAT, 4, "edited later", SENT + timedelta(days=2), account_id=1)
        await real_adapter.update_message_text(CHAT, 4, "edited again", SENT + timedelta(days=3), account_id=1)
        exported = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1, **window)]
        assert [(m["id"], [v["text"] for v in m["versions"]]) for m in exported] == [(4, ["inside", "edited later"])]

    async def test_versions_are_read_per_batch_while_the_messages_stream(self, real_adapter):
        """A chat's versions are never read in one go: one query per batch, in message order."""
        for message_id in range(1, 6):
            await _message(real_adapter, message_id, f"draft {message_id}", when=SENT + timedelta(minutes=message_id))
            await real_adapter.update_message_text(
                CHAT, message_id, f"final {message_id}", SENT + timedelta(hours=message_id), account_id=1
            )
        attach = real_adapter._attach_export_versions
        with (
            patch.object(adapter_module, "EXPORT_VERSIONS_BATCH", 2),
            patch.object(real_adapter, "_attach_export_versions", wraps=attach) as spy,
        ):
            stream = real_adapter.get_messages_for_export(CHAT, account_id=1)
            first = await anext(stream)
            assert spy.await_count == 1  # the first batch left before the chat was read
            rest = [m async for m in stream]
        assert spy.await_count == 3  # batches of 2, 2 and 1
        exported = [first, *rest]
        assert [(m["id"], [v["text"] for v in m["versions"]]) for m in exported] == [
            (message_id, [f"draft {message_id}"]) for message_id in range(1, 6)
        ]


class TestCliExport:
    async def test_marks_deleted_and_edited_messages_with_every_kept_version(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(str(output), chat_id=CHAT)
        data = json.loads(output.read_text(encoding="utf-8"))
        by_key = {(m["account_id"], m["id"]): m for m in data["messages"]}

        edited = by_key[(1, 1)]
        assert edited["edit_date"] is not None
        assert [(v["text"], v["date"]) for v in edited["versions"]] == [
            ("first draft", SENT.isoformat()),
            ("second draft", FIRST_EDIT.isoformat()),
        ]
        assert all(isinstance(v["captured_at"], str) for v in edited["versions"])
        assert [v["text"] for v in by_key[(2, 1)]["versions"]] == ["other account draft"]

        deleted = by_key[(1, 2)]
        assert deleted["is_deleted"] == 1
        assert deleted["deleted_at"] is not None
        assert deleted["versions"] == []
        assert by_key[(1, 3)]["versions"] == []
        # The flat list keeps its meaning next to the new per-message lists.
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
