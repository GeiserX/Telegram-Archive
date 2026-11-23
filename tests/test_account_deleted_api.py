"""The API carries ``account_deleted_at`` wherever the viewer names a chat or a sender.

Runs the real FastAPI app on ``real_adapter`` (SQLite and PostgreSQL), so the
field is checked on the wire, not on a mocked row. The field is additive: a
live chat or sender carries it as null.
"""

from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from test_account_badges import app_on, as_principal, client  # noqa: E402, F401  (app_on is a fixture)

NAMED = {"id": 201, "type": "private", "first_name": "Sample", "last_name": "Person", "username": "sample_user"}
DELETED = {"id": 201, "type": "private", "first_name": None, "last_name": None, "username": None, "phone": None}


async def _seed(adapter) -> None:
    await adapter.upsert_chat(NAMED, account_id=1)
    await adapter.upsert_chat({**DELETED, "account_deleted": True}, account_id=1)
    await adapter.upsert_chat({"id": -5001, "type": "group", "title": "Sample group"}, account_id=1)
    await adapter.upsert_user({"id": 201, "first_name": "Sample", "last_name": "Person"})
    await adapter.upsert_user({"id": 201, "first_name": None, "last_name": None, "username": None})
    await adapter.upsert_user({"id": 201, "account_deleted": True})
    for chat_id in (201, -5001):
        await adapter.insert_message(
            {
                "id": 1,
                "chat_id": chat_id,
                "sender_id": 201,
                "date": datetime(2026, 1, 1, 12),
                "text": "zebracrossing sample",
                "sender_name": "Sample Person",
                "is_pinned": 1,
                "raw_data": {},
            },
            account_id=1,
        )


async def test_chat_and_message_responses_carry_the_field(app_on):  # noqa: F811
    await _seed(app_on)
    as_principal(role="master", allowed_accounts=None)

    async with client() as http:
        chats = {row["id"]: row for row in (await http.get("/api/chats")).json()["chats"]}
        found = (await http.get("/api/chats?search=Sample%20Person")).json()["chats"]
        ref = chats[201]["ref"]
        one = (await http.get(f"/api/chats/{ref}")).json()
        group_ref = chats[-5001]["ref"]
        messages = (await http.get(f"/api/chats/{group_ref}/messages")).json()
        pinned = (await http.get(f"/api/chats/{group_ref}/pinned")).json()
        by_date = (await http.get(f"/api/chats/{group_ref}/messages/by-date?date=2026-01-01&timezone=UTC")).json()
        hits = (await http.get("/api/search/messages?q=zebracrossing")).json()["results"]
        admin = {row["id"]: row for row in (await http.get("/api/admin/chats")).json()["chats"]}

    assert chats[201]["account_deleted_at"] is not None
    assert chats[201]["first_name"] == "Sample"
    assert chats[-5001]["account_deleted_at"] is None
    assert [row["ref"] for row in found] == [ref]
    assert one["account_deleted_at"] == chats[201]["account_deleted_at"]
    assert messages[0]["sender_account_deleted_at"] is not None
    assert pinned[0]["sender_account_deleted_at"] is not None
    assert by_date["sender_account_deleted_at"] is not None
    by_chat = {hit["chat"]["ref"]: hit["chat"]["account_deleted_at"] for hit in hits}
    assert by_chat[ref] is not None
    assert by_chat[group_ref] is None
    assert admin[201]["account_deleted_at"] is not None
