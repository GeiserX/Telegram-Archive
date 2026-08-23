"""Streaming import + resume, proven against a REAL database (9t6.5.48).

Every existing import test drives an ``AsyncMock`` adapter, and SQLite never
enforces the foreign keys PostgreSQL does — so a wrong write order or a
broken resume could stay green everywhere except production. These tests run
the importer end to end through ``conftest.real_adapter`` (SQLite always,
PostgreSQL when a server is available, as on CI) and assert the states the
design promises:

* an interrupted import resumes to EXACTLY the state of an uninterrupted one
  (including ``sync_status.message_count`` — the additive counter that
  ``--merge`` replays used to inflate);
* the chat a crashed run was inside replays without ``--merge`` and without
  tripping the already-imported guard;
* a replay never resurrects an import media row the sweep has adopted (#405);
* a truncated export fails loudly, keeps its progress, and resumes on the
  same file. A REPLACED file (different fingerprint) deliberately does not
  resume — a re-export may hold newer messages for "completed" chats, so
  skipping them would silently drop data; that path stays ``--merge``.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.db.models import Media, Message, SyncStatus, account_metadata_key
from src.telegram_import import TelegramImporter

CHAT_A = 901001  # personal_chat keeps its raw id
CHAT_B_EXPORT = 901002  # what result.json says
CHAT_B = -1000000901002  # derive_chat_id marks supergroups (-100... form)
MARKER_KEY = account_metadata_key("import_progress", 1)


def _msg(msg_id: int, text: str = "hola", **extra) -> dict:
    base = {
        "id": msg_id,
        "type": "message",
        "date": f"2024-01-15T10:{msg_id % 60:02d}:00",
        "from": "Alice",
        "from_id": "user4242",
        "text": text,
    }
    base.update(extra)
    return base


def _write_export(export_dir: Path, chats: list[dict]) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    control = export_dir / "result.json"
    payload = {
        "about": "Telegram Desktop export",
        "personal_information": {"user_id": 4242, "first_name": "Alice"},
        "chats": {"list": chats},
    }
    control.write_text(json.dumps(payload), encoding="utf-8")
    return control


def _two_chat_export(export_dir: Path, *, b_messages: int = 4, with_media: bool = False) -> Path:
    files_dir = export_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    chat_a = {
        "name": "Chat A",
        "type": "personal_chat",
        "id": CHAT_A,
        "messages": [_msg(1), _msg(2, "adios")],
    }
    b_msgs = []
    for i in range(1, b_messages + 1):
        extra = {}
        if with_media and i == 2:
            (files_dir / "doc.bin").write_bytes(b"import-bytes")
            extra = {"file": "files/doc.bin", "file_name": "doc.bin", "media_type": "document"}
        b_msgs.append(_msg(i, f"b-{i}", **extra))
    chat_b = {"name": "Chat B", "type": "private_supergroup", "id": CHAT_B_EXPORT, "messages": b_msgs}
    return _write_export(export_dir, [chat_a, chat_b])


def _importer(real_adapter, tmp_path: Path) -> TelegramImporter:
    return TelegramImporter(real_adapter, str(tmp_path / "media"), account_id=1)


async def _state_snapshot(real_adapter) -> dict:
    """Everything the resume-equality criterion compares."""
    async with real_adapter.db_manager.async_session_factory() as session:
        message_rows = (
            await session.execute(select(Message.chat_id, Message.id, Message.text).where(Message.account_id == 1))
        ).all()
        media_rows = (
            await session.execute(
                select(Media.id, Media.chat_id, Media.message_id, Media.downloaded).where(Media.account_id == 1)
            )
        ).all()
        sync_rows = (
            await session.execute(
                select(SyncStatus.chat_id, SyncStatus.last_message_id, SyncStatus.message_count).where(
                    SyncStatus.account_id == 1
                )
            )
        ).all()
    return {
        "messages": sorted((r[0], r[1], r[2]) for r in message_rows),
        "media": sorted((r[0], r[1], r[2], r[3]) for r in media_rows),
        "sync": sorted((r[0], r[1], r[2]) for r in sync_rows),
    }


# ---------------------------------------------------------------------------
# End-to-end on a real engine
# ---------------------------------------------------------------------------


async def test_end_to_end_import_writes_real_rows(real_adapter, tmp_path):
    _two_chat_export(tmp_path / "export", with_media=True)
    importer = _importer(real_adapter, tmp_path)

    summary = await importer.run(str(tmp_path / "export"))

    assert summary["chats_imported"] == 2
    assert summary["chats_skipped"] == 0
    assert summary["total_messages"] == 6
    assert summary["total_media"] == 1
    state = await _state_snapshot(real_adapter)
    assert len(state["messages"]) == 6
    assert state["media"] == [(f"import_{CHAT_B}_2", CHAT_B, 2, 1)]
    assert state["sync"] == sorted([(CHAT_A, 2, 2), (CHAT_B, 4, 4)])
    media_file = tmp_path / "media" / str(CHAT_B)
    assert any(media_file.iterdir())
    # Clean completion clears the marker.
    assert not await real_adapter.get_metadata(MARKER_KEY)
    # The sweep can adopt what the import created (the #405 contract).
    adopted = await real_adapter.adopt_import_media(CHAT_B, 2, f"{CHAT_B}_2_document", account_id=1)
    assert adopted is not None and adopted["downloaded"] is True


async def test_interrupted_import_resumes_to_identical_state(real_adapter, tmp_path):
    _two_chat_export(tmp_path / "export")
    importer = _importer(real_adapter, tmp_path)

    real_batch = real_adapter.insert_messages_batch
    calls = {"n": 0}

    async def explode_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # chat A batch, chat B batch 1, BOOM on B batch 2
            raise RuntimeError("interrupt: simulated crash mid-import")
        return await real_batch(*args, **kwargs)

    real_adapter.insert_messages_batch = explode_on_second
    try:
        # BATCH_SIZE=2 makes chat B span two batches: the crash lands MID-CHAT,
        # leaving partial chat-B rows — the state whose retry used to require
        # --merge (and corrupt message_count).
        with patch("src.telegram_import.BATCH_SIZE", 2), pytest.raises(RuntimeError, match="simulated crash"):
            await importer.run(str(tmp_path / "export"))
    finally:
        real_adapter.insert_messages_batch = real_batch

    partial = await _state_snapshot(real_adapter)
    assert len(partial["messages"]) == 4  # chat A complete + chat B's first batch

    marker = json.loads(await real_adapter.get_metadata(MARKER_KEY))
    assert marker["completed"] == [CHAT_A]
    assert marker["started"] == CHAT_B

    # Resume with a FRESH importer (new process): chat A skipped, chat B
    # replayed from its start without --merge and without the guard firing.
    resumed = _importer(real_adapter, tmp_path)
    summary = await resumed.run(str(tmp_path / "export"))
    assert summary["chats_skipped"] == 1
    assert summary["chats_imported"] == 1

    state = await _state_snapshot(real_adapter)
    assert len(state["messages"]) == 6
    # THE equality criterion: sync counters identical to an uninterrupted run
    # (message_count is additive on conflict — a --merge replay would have
    # inflated CHAT_A's to 4).
    assert state["sync"] == sorted([(CHAT_A, 2, 2), (CHAT_B, 4, 4)])
    assert not await real_adapter.get_metadata(MARKER_KEY)


async def test_replay_never_resurrects_an_adopted_media_row(real_adapter, tmp_path):
    _two_chat_export(tmp_path / "export", with_media=True)
    importer = _importer(real_adapter, tmp_path)

    real_sync = real_adapter.update_sync_status

    async def explode_on_chat_b(chat_id, *args, **kwargs):
        if chat_id == CHAT_B:
            raise RuntimeError("interrupt: crash after B's rows, before B completes")
        return await real_sync(chat_id, *args, **kwargs)

    real_adapter.update_sync_status = explode_on_chat_b
    try:
        with pytest.raises(RuntimeError, match="before B completes"):
            await importer.run(str(tmp_path / "export"))
    finally:
        real_adapter.update_sync_status = real_sync

    # The sweep reaches the message first and ADOPTS the import row (#405
    # re-keys it to the sweep's own name).
    adopted = await real_adapter.adopt_import_media(CHAT_B, 2, f"{CHAT_B}_2_document", account_id=1)
    assert adopted is not None

    resumed = _importer(real_adapter, tmp_path)
    await resumed.run(str(tmp_path / "export"))

    state = await _state_snapshot(real_adapter)
    # Exactly ONE media row for the message, and it is the sweep-named one:
    # the replay must not re-create import_* next to it.
    assert state["media"] == [(f"{CHAT_B}_2_document", CHAT_B, 2, 1)]


async def test_truncated_export_fails_loudly_and_resumes_on_the_same_file(real_adapter, tmp_path):
    control = _two_chat_export(tmp_path / "export", b_messages=40)
    raw = control.read_bytes()
    cut = raw.rfind(b'"b-20"')  # inside chat B's messages, well after chat A
    assert cut > 0
    control.write_bytes(raw[:cut])

    importer = _importer(real_adapter, tmp_path)
    with pytest.raises(ValueError, match="truncated or invalid"):
        await importer.run(str(tmp_path / "export"))

    marker = json.loads(await real_adapter.get_metadata(MARKER_KEY))
    assert marker["completed"] == [CHAT_A]

    # Re-running on the SAME broken file resumes past chat A and fails in B
    # again — progress is durable, the error is repeatable, nothing doubles.
    resumed = _importer(real_adapter, tmp_path)
    with pytest.raises(ValueError, match="truncated or invalid"):
        await resumed.run(str(tmp_path / "export"))
    marker = json.loads(await real_adapter.get_metadata(MARKER_KEY))
    assert marker["completed"] == [CHAT_A]

    state = await _state_snapshot(real_adapter)
    assert state["sync"] == [(CHAT_A, 2, 2)]  # A once, never doubled; B never completed


async def test_replaced_export_does_not_reuse_the_marker(real_adapter, tmp_path):
    """A different file (fingerprint) must NOT inherit the completed set —
    a re-export can hold newer messages for a 'completed' chat, and skipping
    it would silently drop them. The normal guard applies instead.
    """
    _two_chat_export(tmp_path / "export")
    importer = _importer(real_adapter, tmp_path)
    await importer.run(str(tmp_path / "export"))

    # Plant a stale marker as if an interrupted run had completed chat A of
    # some OTHER export file.
    await real_adapter.set_metadata(
        MARKER_KEY, json.dumps({"fingerprint": "0:deadbeef", "completed": [CHAT_A], "started": CHAT_B})
    )

    fresh = _importer(real_adapter, tmp_path)
    with pytest.raises(ValueError, match="already has"):
        await fresh.run(str(tmp_path / "export"))


async def test_dry_run_reads_and_writes_no_marker(real_adapter, tmp_path):
    _two_chat_export(tmp_path / "export")
    importer = _importer(real_adapter, tmp_path)

    summary = await importer.run(str(tmp_path / "export"), dry_run=True)

    assert summary["chats_imported"] == 2
    assert not await real_adapter.get_metadata(MARKER_KEY)
    state = await _state_snapshot(real_adapter)
    assert state["messages"] == [] and state["media"] == [] and state["sync"] == []


# ---------------------------------------------------------------------------
# Streaming parser hard-fail sentinel (mocked db — no writes may happen)
# ---------------------------------------------------------------------------


async def test_chat_identity_after_messages_hard_fails(tmp_path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    # 'id' and 'type' AFTER the messages array: a forward-only parse derives
    # the chat id before reading them, so rows would land under the wrong
    # chat. The importer must refuse rather than corrupt.
    (export_dir / "result.json").write_text(
        json.dumps(
            {
                "chats": {
                    "list": [
                        {
                            "name": "Sneaky",
                            "messages": [_msg(1)],
                            "id": CHAT_A,
                            "type": "personal_chat",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    db = AsyncMock()
    db.get_metadata.return_value = None
    importer = TelegramImporter(db, str(tmp_path / "media"), account_id=1)

    with pytest.raises(ValueError, match="appeared after"):
        await importer.run(str(export_dir))
    db.insert_messages_batch.assert_not_awaited()


async def test_streaming_keeps_owner_and_outgoing_flags(real_adapter, tmp_path):
    """personal_information precedes chats (the TDesktop order): every
    message carries an honest is_outgoing through the streaming path too.
    """
    export_dir = tmp_path / "export"
    _write_export(
        export_dir,
        [
            {
                "name": "Chat A",
                "type": "personal_chat",
                "id": CHAT_A,
                "messages": [_msg(1, "mine"), _msg(2, "theirs", from_id="user9999")],
            }
        ],
    )
    importer = _importer(real_adapter, tmp_path)
    await importer.run(str(export_dir))

    async with real_adapter.db_manager.async_session_factory() as session:
        rows = (
            await session.execute(
                select(Message.id, Message.is_outgoing).where(Message.account_id == 1, Message.chat_id == CHAT_A)
            )
        ).all()
    assert sorted(rows) == [(1, 1), (2, 0)]  # owner user_id=4242 wrote msg 1
