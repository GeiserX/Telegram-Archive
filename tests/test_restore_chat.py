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

from telethon.errors import FloodWaitError, SlowModeWaitError

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


async def _restore(adapter, tmp_path, *, send_file=None, send_message=None):
    """Run the restore into a fake client; returns the client to read its sends."""
    module = _load_restore()
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=SimpleNamespace(title="Fixture Destination")),
        send_file=send_file or AsyncMock(),
        send_message=send_message or AsyncMock(),
        disconnect=AsyncMock(),
    )
    with (
        patch.object(module, "get_db_adapter", AsyncMock(return_value=adapter)),
        patch.object(module, "get_telegram_client", AsyncMock(return_value=client)),
        patch("builtins.input", return_value="YES"),
        patch.dict(os.environ, {"BACKUP_PATH": str(tmp_path)}),
    ):
        await module.restore_chat(CHAT, CHAT, delay=0)
    return client


def _file_sends(client) -> list[tuple[str, str | None]]:
    return [(os.path.basename(call.args[1]), call.kwargs.get("caption")) for call in client.send_file.await_args_list]


CAPTION = "[Fixture Sender - 2026-10-01 10:38]\nThree photos from the trip"


async def test_a_message_is_sent_once_with_every_file_it_has_on_disk(real_adapter, tmp_path):
    await _album(real_adapter, tmp_path / "media")

    client = await _restore(real_adapter, tmp_path)

    assert client.send_file.await_args_list[0].args[1] == str(tmp_path / "media" / str(CHAT) / "fixture-b.jpg")
    assert _file_sends(client) == [("fixture-b.jpg", CAPTION), ("fixture-c.jpg", None)]
    assert [call.args[1] for call in client.send_message.await_args_list] == [
        "[Fixture Sender - 2026-10-01 10:38]\nOnly text"
    ]


async def test_a_wait_on_a_later_file_sends_that_file_again_and_the_rest_after_it(real_adapter, tmp_path):
    """Telegram makes the second upload wait once: the same file goes again
    after the wait, and the message is not left with one of its files."""
    await _album(real_adapter, tmp_path / "media")
    send_file = AsyncMock(side_effect=[None, FloodWaitError(request=None, capture=0), None])

    client = await _restore(real_adapter, tmp_path, send_file=send_file)

    assert _file_sends(client) == [("fixture-b.jpg", CAPTION), ("fixture-c.jpg", None), ("fixture-c.jpg", None)]
    assert client.send_message.await_count == 1


async def test_a_wait_on_the_first_file_or_a_text_sends_it_again_once(real_adapter, tmp_path):
    """The first file carries the text: after a wait it goes again with its
    caption, so the text is still sent exactly once, and so is a text-only message."""
    await _album(real_adapter, tmp_path / "media")
    send_file = AsyncMock(side_effect=[SlowModeWaitError(request=None, capture=0), None, None])
    send_message = AsyncMock(side_effect=[FloodWaitError(request=None, capture=0), None])

    client = await _restore(real_adapter, tmp_path, send_file=send_file, send_message=send_message)

    assert _file_sends(client) == [("fixture-b.jpg", CAPTION), ("fixture-b.jpg", CAPTION), ("fixture-c.jpg", None)]
    assert [call.args[1] for call in client.send_message.await_args_list] == [
        "[Fixture Sender - 2026-10-01 10:38]\nOnly text"
    ] * 2


async def test_a_file_that_keeps_waiting_is_reported_and_the_restore_goes_on(real_adapter, tmp_path, caplog):
    await _album(real_adapter, tmp_path / "media")
    module_retries = _load_restore().SEND_WAIT_RETRIES
    waits = [FloodWaitError(request=None, capture=0)] * (module_retries + 1)
    send_file = AsyncMock(side_effect=[None, *waits])

    with caplog.at_level("WARNING"):
        client = await _restore(real_adapter, tmp_path, send_file=send_file)

    assert send_file.await_count == 1 + module_retries + 1
    assert any("incomplete: 1 of 2 files sent" in r.getMessage() for r in caplog.records)
    # The next message still goes out.
    assert client.send_message.await_count == 1
