"""The messages route and the chat export pass poll and preview snapshots through (038).

Both read through the chat and account the route resolved, so a viewer bound
to one account never sees the states another account's listener kept. A
no-download login still gets the snapshots in the messages route (they hold
no file) and is refused the export, as before. Runs against a real engine on
both backends.
"""

import importlib
import json
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_message_snapshots_web_"))

pytest.importorskip("fastapi")

CHAT_ID = -1009990401
MESSAGE_ID = 9
CHAT_REF = "messageSnapshotsRef01"
ANON_ENV = {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}
POLL = {"id": 5550000000000000401, "question": "Demo?", "answers": [{"text": "A", "option": "AA=="}], "closed": False}


@pytest.fixture
def main_mod(monkeypatch):
    for key, value in ANON_ENV.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module.db = AsyncMock()
    return module


async def _seed(adapter) -> None:
    """The same poll message in two accounts' copies of a chat; only account 2 saw it close."""
    for account in (1, 2):
        await adapter.upsert_chat({"id": CHAT_ID, "type": "group", "title": "Test Group C"}, account_id=account)
        await adapter.insert_message(
            {
                "id": MESSAGE_ID,
                "chat_id": CHAT_ID,
                "date": datetime(2026, 3, 1, 9),
                "text": "",
                "raw_data": {"poll": POLL},
            },
            account_id=account,
        )
    closed = {**POLL, "closed": True}
    await adapter.record_message_snapshots(CHAT_ID, MESSAGE_ID, {"poll": closed}, account_id=2, source="listener")


async def _messages(main_mod, chat, user) -> list:
    return await main_mod.get_messages(
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


async def test_the_messages_route_returns_the_snapshots_of_its_account_only(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer")

    one = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    (msg,) = await _messages(main_mod, one, user)
    assert msg["snapshots"] == {}

    two = main_mod.ChatContext(account_id=2, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    (msg,) = await _messages(main_mod, two, user)
    assert msg["snapshots"]["poll"]["payload"]["closed"] is True
    assert msg["raw_data"]["poll"]["closed"] is False


async def test_a_no_download_login_keeps_the_snapshots_and_is_refused_the_export(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer", no_download=True)
    two = main_mod.ChatContext(account_id=2, chat_id=CHAT_ID, ref=CHAT_REF, type="group")

    (msg,) = await _messages(main_mod, two, user)
    assert msg["snapshots"]["poll"]["count"] == 1
    with pytest.raises(main_mod.HTTPException) as exc:
        await main_mod.export_chat(chat=two, user=user, from_date=None, to_date=None)
    assert exc.value.status_code == 403


async def test_the_export_lists_the_states_of_its_account_only(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer")

    for account, expected in ((1, []), (2, [True])):
        chat = main_mod.ChatContext(account_id=account, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
        response = await main_mod.export_chat(chat=chat, user=user, from_date=None, to_date=None)
        body = "".join([part async for part in response.body_iterator])
        (msg,) = json.loads(body)["messages"]
        assert [snapshot["payload"]["closed"] for snapshot in msg["snapshots"]] == expected
