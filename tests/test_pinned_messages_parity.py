"""The pinned list carries what the messages page gives a row.

The pinned-only view draws ``GET /api/chats/{ref}/pinned`` with the chat's own
renderer. Since 9.0 ``raw_data.poll`` and ``raw_data.webpage`` keep the first
capture and later states live in ``message_snapshots``, so a pinned list
without ``snapshots`` froze a pinned poll at its first tally. It also lacked
the reactions and the reactions taken back, so the same message showed chips
in the chat and none in the pinned view. Runs against a real engine on both
backends.
"""

import importlib
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_pinned_parity_"))

pytest.importorskip("fastapi")

CHAT_ID = -1009990402
POLL_ID = 9
CHAT_REF = "pinnedParityRef00001"
ANON_ENV = {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}
POLL = {
    "id": 5550000000000000402,
    "question": "Demo question?",
    "answers": [{"text": "Option A", "option": "AA=="}, {"text": "Option B", "option": "AQ=="}],
    "closed": False,
    "results": {"total_voters": 0, "results": []},
}


@pytest.fixture
def main_mod(monkeypatch):
    for key, value in ANON_ENV.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module.db = AsyncMock()
    return module


async def _seed(adapter, pinned_ids=(POLL_ID,)) -> None:
    """A pinned poll in two accounts' copies of a group; only account 2 saw it close and its reactions drop."""
    for account in (1, 2):
        await adapter.upsert_chat({"id": CHAT_ID, "type": "group", "title": "Test Group P"}, account_id=account)
        for message_id in sorted({POLL_ID, *pinned_ids}):
            await adapter.insert_message(
                {
                    "id": message_id,
                    "chat_id": CHAT_ID,
                    "date": datetime(2026, 3, 1, 9, message_id % 60),
                    "text": "" if message_id == POLL_ID else f"demo text {message_id}",
                    "raw_data": {"poll": POLL} if message_id == POLL_ID else {},
                },
                account_id=account,
            )
        await adapter.sync_pinned_messages(CHAT_ID, list(pinned_ids), account_id=account)
    closed = {**POLL, "closed": True, "results": {"total_voters": 7, "results": []}}
    await adapter.record_message_snapshots(CHAT_ID, POLL_ID, {"poll": closed}, account_id=2, source="listener")
    await adapter.reconcile_reactions(
        POLL_ID, CHAT_ID, [{"emoji": "👍", "count": 3}, {"emoji": "❤", "count": 1}], account_id=2, source="listener"
    )
    await adapter.reconcile_reactions(POLL_ID, CHAT_ID, [{"emoji": "👍", "count": 1}], account_id=2, source="listener")


def _chat(main_mod, account: int):
    return main_mod.ChatContext(account_id=account, chat_id=CHAT_ID, ref=CHAT_REF, type="group")


async def _page_row(main_mod, chat, user) -> dict:
    rows = await main_mod.get_messages(
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
    return next(row for row in rows if row["id"] == POLL_ID)


async def _pinned_row(main_mod, chat, user) -> dict:
    rows = await main_mod.get_pinned_messages(chat=chat, user=user)
    return next(row for row in rows if row["id"] == POLL_ID)


async def test_a_pinned_poll_shows_its_newest_state_like_the_chat(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer")

    pinned = await _pinned_row(main_mod, _chat(main_mod, 2), user)
    page = await _page_row(main_mod, _chat(main_mod, 2), user)

    assert pinned["snapshots"]["poll"]["payload"]["closed"] is True
    assert pinned["snapshots"]["poll"]["payload"]["results"]["total_voters"] == 7
    assert pinned["snapshots"]["poll"]["differs_from_first"] is True
    assert pinned["raw_data"]["poll"]["closed"] is False
    assert pinned["snapshots"] == page["snapshots"]


async def test_a_pinned_message_shows_the_chat_s_reactions_and_the_ones_taken_back(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer")

    pinned = await _pinned_row(main_mod, _chat(main_mod, 2), user)
    page = await _page_row(main_mod, _chat(main_mod, 2), user)

    assert [(r["emoji"], r["count"]) for r in pinned["reactions"]] == [("👍", 1)]
    taken_back = {r["emoji"]: (r["count"], r["count_before"]) for r in pinned["removed_reactions"]}
    assert taken_back == {"👍": (2, 3), "❤": (1, 1)}
    for key in ("reactions", "removed_reactions", "reaction_history"):
        assert pinned[key] == page[key], key


async def test_the_pinned_list_reads_only_its_own_account(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer")

    pinned = await _pinned_row(main_mod, _chat(main_mod, 1), user)

    assert pinned["snapshots"] == {}
    assert pinned["reactions"] == []
    assert pinned["removed_reactions"] == []
    assert pinned["reaction_history"] == []


async def _select_count(adapter, account_id: int) -> int:
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sync_engine = adapter.db_manager.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        await adapter.get_pinned_messages(CHAT_ID, account_id=account_id)
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    return sum(1 for statement in statements if statement.lstrip().upper().startswith("SELECT"))


async def test_the_pinned_list_costs_the_same_statements_for_one_pin_or_many(real_adapter):
    await _seed(real_adapter, pinned_ids=(POLL_ID, 11, 12, 13, 14, 15))
    await real_adapter._account_owner_ids()  # warm the owner cache, so both reads count alike

    many = await _select_count(real_adapter, account_id=2)
    await real_adapter.sync_pinned_messages(CHAT_ID, [POLL_ID], account_id=2)
    one = await _select_count(real_adapter, account_id=2)

    # Pinned rows, snapshot groups, snapshot rows, reactions, reaction history:
    # five whatever the number of pins (no pinned reply here, so no reply read).
    assert many == one == 5
