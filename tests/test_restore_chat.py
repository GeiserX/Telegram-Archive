"""``scripts/restore_chat.py`` sends a message once, with every one of its media.

Since 9.0 the export lists a message once, with all its media rows. The
restore used to read only the first row's path, so a message with several
files went back with one. It now sends the first file with the text as its
caption and every other file after it without text, in the order both exports
list media: downloaded first, then the lowest id. Runs on SQLite and
PostgreSQL (``real_adapter``).
"""

import importlib.util
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

CHAT = -1001900000002
SENT = datetime(2026, 10, 1, 10, 38, 0)
REPO = Path(__file__).resolve().parents[1]


def _load_restore():
    spec = importlib.util.spec_from_file_location("restore_chat", REPO / "scripts" / "restore_chat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _album(adapter, media_root: Path) -> None:
    """Message 1 holds four media rows; message 2 is text only.

    ``fixture-0`` is downloaded but its file is gone, ``fixture-a`` was never
    downloaded, and ``fixture-c`` and ``fixture-b`` are on disk (inserted in
    that order, so the order is the id's, not the insert's).
    """
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Group"}, account_id=1)
    for message_id, text in ((1, "Three photos from the trip"), (2, "Only text")):
        await adapter.insert_message(
            {"id": message_id, "chat_id": CHAT, "text": text, "date": SENT, "sender_name": "Fixture Sender"},
            account_id=1,
        )
    folder = media_root / str(CHAT)
    folder.mkdir(parents=True)
    for media_id, downloaded, on_disk in (
        ("fixture-c", True, True),
        ("fixture-b", True, True),
        ("fixture-0", True, False),
        ("fixture-a", False, False),
    ):
        path = f"{CHAT}/{media_id}.jpg" if downloaded else None
        if on_disk:
            (folder / f"{media_id}.jpg").write_bytes(b"demo photo " + media_id.encode())
        await adapter.insert_media(
            {
                "id": media_id,
                "message_id": 1,
                "chat_id": CHAT,
                "type": "photo",
                "file_path": path,
                "downloaded": downloaded,
            },
            account_id=1,
        )


async def test_the_adapter_hands_the_restore_every_media_row_in_export_order(real_adapter, tmp_path):
    await _album(real_adapter, tmp_path / "media")

    restore = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, include_media=True, account_id=1)}
    plain = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1)}

    assert [(f["type"], f["path"]) for f in restore[1]["media_files"]] == [
        ("photo", f"{CHAT}/fixture-0.jpg"),
        ("photo", f"{CHAT}/fixture-b.jpg"),
        ("photo", f"{CHAT}/fixture-c.jpg"),
        ("photo", None),
    ]
    assert [m["media_id"] for m in restore[1]["media"]] == ["fixture-0", "fixture-b", "fixture-c", "fixture-a"]
    assert restore[2]["media_files"] == []
    # The viewer's export never carries a path.
    assert all("media_files" not in m and "media_path" not in m for m in plain.values())


async def test_a_message_is_sent_once_with_every_file_it_has_on_disk(real_adapter, tmp_path):
    media_root = tmp_path / "media"
    await _album(real_adapter, media_root)
    module = _load_restore()
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=SimpleNamespace(title="Fixture Destination")),
        send_file=AsyncMock(),
        send_message=AsyncMock(),
        disconnect=AsyncMock(),
    )

    with (
        patch.object(module, "get_db_adapter", AsyncMock(return_value=real_adapter)),
        patch.object(module, "get_telegram_client", AsyncMock(return_value=client)),
        patch("builtins.input", return_value="YES"),
        patch.dict(os.environ, {"BACKUP_PATH": str(tmp_path)}),
    ):
        await module.restore_chat(CHAT, CHAT, delay=0)

    sent = [(call.args[1], call.kwargs.get("caption")) for call in client.send_file.await_args_list]
    assert sent == [
        (
            str(media_root / str(CHAT) / "fixture-b.jpg"),
            "[Fixture Sender - 2026-10-01 10:38]\nThree photos from the trip",
        ),
        (str(media_root / str(CHAT) / "fixture-c.jpg"), None),
    ]
    assert [call.args[1] for call in client.send_message.await_args_list] == [
        "[Fixture Sender - 2026-10-01 10:38]\nOnly text"
    ]
