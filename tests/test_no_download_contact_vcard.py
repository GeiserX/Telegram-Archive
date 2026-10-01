"""A login whose downloads are off never receives a shared contact's vCard text.

Releases before 9.0 kept a contact's vCard as a file, which such a login could
not download. 9.0 keeps it under ``raw_data.contact.vcard`` instead. The viewer
draws only the name and the phone, so the vCard text keeps the file rule: the
messages route, the pinned list and the date jump drop it for a no-download
login and keep it for everyone else. Runs against a real engine on both
backends.
"""

import importlib
import json
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_no_download_vcard_"))

pytest.importorskip("fastapi")

CHAT_ID = -1009990403
MESSAGE_ID = 21
CHAT_REF = "noDownloadVcardRef01"
ANON_ENV = {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}
VCARD = "BEGIN:VCARD\nVERSION:3.0\nFN:Demo Contact\nEMAIL:test@value/here\nEND:VCARD"
CONTACT = {"first_name": "Demo", "last_name": "Contact", "phone_number": "15550100", "vcard": VCARD, "user_id": 0}


@pytest.fixture
def main_mod(monkeypatch):
    for key, value in ANON_ENV.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module.db = AsyncMock()
    return module


async def _seed(adapter) -> None:
    await adapter.upsert_chat({"id": CHAT_ID, "type": "group", "title": "Test Group V"}, account_id=1)
    await adapter.insert_message(
        {
            "id": MESSAGE_ID,
            "chat_id": CHAT_ID,
            "date": datetime(2026, 3, 2, 9),
            "text": "",
            "raw_data": {"contact": CONTACT},
        },
        account_id=1,
    )
    await adapter.sync_pinned_messages(CHAT_ID, [MESSAGE_ID], account_id=1)


def _chat(main_mod):
    return main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")


async def _rows(main_mod, user) -> dict[str, dict]:
    """The contact message as each route that returns raw_data gives it to ``user``."""
    chat = _chat(main_mod)
    (page,) = await main_mod.get_messages(
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
    (pinned,) = await main_mod.get_pinned_messages(chat=chat, user=user)
    by_date = await main_mod.get_message_by_date(chat=chat, user=user, date="2026-03-02", timezone="UTC", topic_id=None)
    return {"page": page, "pinned": pinned, "by_date": by_date}


async def test_a_no_download_login_gets_the_card_without_the_vcard(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer", no_download=True)

    for route, row in (await _rows(main_mod, user)).items():
        contact = row["raw_data"]["contact"]
        assert "vcard" not in contact, route
        assert (contact["first_name"], contact["last_name"], contact["phone_number"]) == (
            "Demo",
            "Contact",
            "15550100",
        ), route
        assert "test@value/here" not in json.dumps(row, default=str), route


async def test_every_other_login_keeps_the_vcard(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer")

    for route, row in (await _rows(main_mod, user)).items():
        assert row["raw_data"]["contact"]["vcard"] == VCARD, route


async def test_the_chat_export_stays_refused_to_a_no_download_login(main_mod, real_adapter):
    await _seed(real_adapter)
    main_mod.db = real_adapter
    user = main_mod.UserContext(username="viewer", role="viewer", no_download=True)

    with pytest.raises(main_mod.HTTPException) as exc:
        await main_mod.export_chat(chat=_chat(main_mod), user=user, from_date=None, to_date=None)
    assert exc.value.status_code == 403


def test_the_strip_leaves_other_payloads_and_non_dicts_alone(main_mod):
    venue = {"venue": {"title": "Demo Cafe"}}
    assert main_mod._without_contact_vcard(venue) is venue
    assert main_mod._without_contact_vcard("{}") == "{}"
    assert main_mod._without_contact_vcard(None) is None
    raw = {"contact": dict(CONTACT), "entities": []}
    stripped = main_mod._without_contact_vcard(raw)
    assert stripped == {"contact": {k: v for k, v in CONTACT.items() if k != "vcard"}, "entities": []}
    # The caller's dict is not changed.
    assert raw["contact"]["vcard"] == VCARD
