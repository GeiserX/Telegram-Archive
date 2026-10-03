"""Custom emoji in message text: their ids are recorded and reach the viewer as strings.

A custom emoji in text is a ``custom_emoji`` entity with its ``document_id`` in
``raw_data.entities``. Every message writer notes the id (a pending
``custom_emoji`` row, the same store reactions use), ``backfill-details``
collects the ids of messages and earlier versions archived before, and every
viewer exit sends the id as a string: a document id is above 2**53, where a
JSON number loses digits in a browser.

These run on a real engine, SQLite and PostgreSQL (``real_adapter``). Every
id and text is fake.
"""

import importlib
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from telegram_archive import telegram_backup
from telegram_archive.db.models import Account, Chat, CustomEmoji

CHAT_ID = -100930
SENT = datetime(2026, 9, 1, 12, 0, 0)
SUN = 5000000000000000001
MOON = 5000000000000000002
STAR = 5000000000000000003
TEXT = "Look 🌞 up"


def _entities(document_id: int) -> list[dict]:
    return [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "custom_emoji", "offset": 5, "length": 2, "document_id": document_id},
    ]


async def _seed_chat(adapter) -> str:
    async with adapter.db_manager.async_session_factory() as session:
        await session.merge(Account(id=1, label="Account 1"))
        await session.commit()
    await adapter.upsert_chat({"id": CHAT_ID, "type": "supergroup", "title": "fixture"}, account_id=1)
    async with adapter.db_manager.async_session_factory() as session:
        return (await session.execute(select(Chat.ref).where(Chat.id == CHAT_ID))).scalar_one()


def _message(message_id: int, text: str = TEXT, entities: list | None = None) -> dict:
    return {
        "id": message_id,
        "chat_id": CHAT_ID,
        "date": SENT,
        "text": text,
        "raw_data": {"entities": entities} if entities is not None else {},
    }


async def _ids(adapter) -> list[int]:
    async with adapter.db_manager.async_session_factory() as session:
        return list(
            (await session.execute(select(CustomEmoji.document_id).order_by(CustomEmoji.document_id))).scalars()
        )


class TestRecording:
    async def test_a_message_with_a_custom_emoji_adds_a_pending_row(self, real_adapter):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(_message(1, entities=_entities(SUN)), account_id=1)
        await real_adapter.insert_messages_batch([_message(2, entities=_entities(MOON)), _message(3)], account_id=1)
        assert await _ids(real_adapter) == [SUN, MOON]

    async def test_a_batch_notes_its_ids_once_sorted_after_its_messages(self, real_adapter, monkeypatch):
        # PostgreSQL: ON CONFLICT DO NOTHING waits on another transaction's
        # uncommitted row with the same id. A batch that took MOON then SUN while
        # the listener took {SUN, MOON} in one statement deadlocked. One sorted
        # statement per transaction, after its message rows, cannot.
        await _seed_chat(real_adapter)
        log: list = []
        upsert = real_adapter._insert_or_update_message
        note = real_adapter._note_custom_emoji

        async def spy_upsert(session, message_data, *, account_id):
            log.append(("message", message_data["id"]))
            return await upsert(session, message_data, account_id=account_id)

        async def spy_note(session, document_ids):
            log.append(("note", sorted(set(document_ids))))
            return await note(session, document_ids)

        monkeypatch.setattr(real_adapter, "_insert_or_update_message", spy_upsert)
        monkeypatch.setattr(real_adapter, "_note_custom_emoji", spy_note)
        batch = [_message(1, entities=_entities(MOON)), _message(2), _message(3, entities=_entities(SUN))]
        await real_adapter.insert_messages_batch(batch, account_id=1)
        assert log == [("message", 1), ("message", 2), ("message", 3), ("note", [SUN, MOON])]
        assert await _ids(real_adapter) == [SUN, MOON]

    async def test_plain_text_and_other_entities_add_no_row(self, real_adapter):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(
            _message(1, entities=[{"type": "bold", "offset": 0, "length": 4}]), account_id=1
        )
        # The words in the text are not an entity.
        await real_adapter.insert_message(_message(2, text='say "custom_emoji"'), account_id=1)
        assert await _ids(real_adapter) == []

    async def test_an_edit_that_adds_a_custom_emoji_adds_its_row(self, real_adapter):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(_message(1, text="Look up", entities=[]), account_id=1)
        outcome, _prior = await real_adapter.update_message_text(
            CHAT_ID, 1, TEXT, SENT.replace(minute=5), account_id=1, entities=_entities(STAR), update_entities=True
        )
        assert outcome == "applied"
        assert await _ids(real_adapter) == [STAR]


class TestBackfill:
    async def test_backfill_collects_text_and_earlier_versions(self, real_adapter, tmp_path):
        await _seed_chat(real_adapter)
        await real_adapter.insert_message(_message(1, entities=_entities(SUN)), account_id=1)
        await real_adapter.insert_message(_message(2, text="Look up", entities=[]), account_id=1)
        # An edit took the custom emoji out: only the earlier version holds it.
        await real_adapter.insert_message(_message(3, entities=_entities(MOON)), account_id=1)
        await real_adapter.update_message_text(
            CHAT_ID, 3, "Look up", SENT.replace(minute=5), account_id=1, entities=[], update_entities=True
        )
        # Rows from before 040: the writers had not recorded them.
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(CustomEmoji.__table__.delete())
            await session.commit()
        assert await real_adapter.get_text_custom_emoji_ids(account_id=1) == {SUN, MOON}
        assert await real_adapter.get_text_custom_emoji_ids(account_id=2) == set()
        assert await real_adapter.get_text_custom_emoji_ids(account_id=1, chat_id=CHAT_ID + 1) == set()

        backup = telegram_backup.TelegramBackup.__new__(telegram_backup.TelegramBackup)
        backup.db = real_adapter
        backup.account_id = 1
        backup.client = None
        summary = telegram_backup._empty_backfill_summary()
        await backup._backfill_custom_emoji(None, False, str(tmp_path), summary)
        assert summary["emoji"]["collected"] == 2
        assert await _ids(real_adapter) == []  # the dry run writes nothing


# ---------------------------------------------------------------------------
# The viewer: string document ids at every exit
# ---------------------------------------------------------------------------


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    for key, value in {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module._media_root = tmp_path.resolve()
    return module


def _custom(entities: list) -> list:
    return [entity for entity in entities if entity.get("type") == "custom_emoji"]


class TestViewerSendsStrings:
    async def test_the_messages_page_pinned_and_by_date(self, main_mod, real_adapter):
        ref = await _seed_chat(real_adapter)
        message = _message(1, entities=_entities(SUN))
        message["is_pinned"] = 1
        await real_adapter.insert_message(message, account_id=1)
        main_mod.db = real_adapter
        chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=ref, type="supergroup")
        user = main_mod.UserContext(username="viewer", role="viewer")
        page = await main_mod.get_messages(
            chat=chat,
            user=user,
            limit=50,
            offset=0,
            search=None,
            before_date=None,
            before_id=None,
            after_id=None,
            topic_id=None,
            deleted_only=False,
            edited_only=False,
        )
        entity = _custom(page[0]["raw_data"]["entities"])[0]
        assert entity["document_id"] == str(SUN)
        # Whole digits survive a JSON round trip read as a browser reads it.
        assert (
            json.loads(json.dumps(page[0], default=str))["raw_data"]["entities"][1]["document_id"]
            == "5000000000000000001"
        )
        pinned = await main_mod.get_pinned_messages(chat=chat, user=user)
        assert _custom(pinned[0]["raw_data"]["entities"])[0]["document_id"] == str(SUN)
        by_date = await main_mod.get_message_by_date(
            date=SENT.strftime("%Y-%m-%d"), chat=chat, user=user, timezone=None, topic_id=None
        )
        assert _custom(by_date["raw_data"]["entities"])[0]["document_id"] == str(SUN)
        # The archive itself keeps the integer.
        stored = await real_adapter.get_messages_paginated(CHAT_ID, limit=5, account_id=1)
        assert _custom(stored[0]["raw_data"]["entities"])[0]["document_id"] == SUN

    async def test_the_versions_route(self, main_mod, real_adapter):
        ref = await _seed_chat(real_adapter)
        await real_adapter.insert_message(_message(1, entities=_entities(MOON)), account_id=1)
        await real_adapter.update_message_text(
            CHAT_ID, 1, "Look up", SENT.replace(minute=5), account_id=1, entities=[], update_entities=True
        )
        main_mod.db = real_adapter
        chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=ref, type="supergroup")
        versions = await main_mod.get_message_versions(
            1, chat=chat, user=main_mod.UserContext(username="v", role="viewer"), limit=100
        )
        assert _custom(versions[0]["entities"])[0]["document_id"] == str(MOON)

    async def test_the_live_frames(self, main_mod, real_adapter):
        await _seed_chat(real_adapter)
        main_mod.db = real_adapter
        raw = {"entities": _entities(STAR)}
        with patch.object(main_mod.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as broadcast:
            await main_mod.handle_realtime_notification(
                {
                    "type": "new_message",
                    "chat_id": CHAT_ID,
                    "account_id": 1,
                    "data": {"message": {"id": 9, "text": TEXT, "raw_data": raw}},
                }
            )
            await main_mod.handle_realtime_notification(
                {
                    "type": "edit",
                    "chat_id": CHAT_ID,
                    "account_id": 1,
                    "data": {"message_id": 9, "new_text": TEXT, "entities": _entities(STAR)},
                }
            )
            await main_mod.broadcast_new_message(CHAT_ID, {"id": 9, "raw_data": raw}, account_id=1)
            await main_mod.broadcast_message_edit(
                CHAT_ID, 9, TEXT, "2026-09-01T12:05:00", account_id=1, entities=_entities(STAR)
            )
        frames = [call.args[1] for call in broadcast.await_args_list]
        assert _custom(frames[0]["message"]["raw_data"]["entities"])[0]["document_id"] == str(STAR)
        assert _custom(frames[1]["entities"])[0]["document_id"] == str(STAR)
        assert _custom(frames[2]["message"]["raw_data"]["entities"])[0]["document_id"] == str(STAR)
        assert _custom(frames[3]["entities"])[0]["document_id"] == str(STAR)
        # The listener's own payload is not changed in place.
        assert raw["entities"][1]["document_id"] == STAR
