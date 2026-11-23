"""Archived identities survive deleted Telegram snapshots without joining IDs."""

import pytest
from sqlalchemy import select

from telegram_archive.db.models import User


@pytest.mark.parametrize("query", ["Sample Person Example", "sample person", "@sample_user", "@SAMPLE_USER"])
async def test_complete_name_and_handle_search(real_adapter, query):
    await real_adapter.upsert_chat(
        {
            "id": 101,
            "type": "private",
            "first_name": "Sample",
            "last_name": "Person Example",
            "username": "sample_user",
        },
        account_id=1,
    )
    await real_adapter.upsert_chat(
        {"id": 102, "type": "private", "first_name": "Sample", "last_name": "Person Two"}, account_id=1
    )
    rows = await real_adapter.get_all_chats(search=query, account_id=1)
    expected = {101, 102} if query == "sample person" else {101}
    assert {row["id"] for row in rows} == expected
    assert await real_adapter.get_chat_count(search=query, account_id=1) == len(expected)


async def test_deleted_snapshot_preserves_scoped_identity_and_ref(real_adapter):
    old = {
        "id": 101,
        "type": "private",
        "first_name": "Sample",
        "last_name": "Person Example",
        "username": "sample_user",
    }
    await real_adapter.upsert_chat(old, account_id=1)
    initial = (await real_adapter.get_all_chats(account_id=1))[0]
    await real_adapter.upsert_chat({**old, "first_name": "Other account", "username": "different"}, account_id=2)
    empty = {**old, "first_name": None, "last_name": None, "username": None, "is_archived": 1}
    await real_adapter.upsert_chat(empty, account_id=1)
    result = (await real_adapter.get_all_chats(account_id=1))[0]
    assert result["first_name"] == "Sample"
    assert result["last_name"] == "Person Example"
    assert result["username"] == "sample_user"
    assert result["ref"] == initial["ref"]
    assert result["is_archived"] == 1
    assert (await real_adapter.get_all_chats(account_id=2))[0]["first_name"] == "Other account"


async def test_live_name_change_can_remove_username(real_adapter):
    await real_adapter.upsert_chat(
        {"id": 123, "type": "private", "first_name": "Before", "username": "old_handle"}, account_id=1
    )
    await real_adapter.upsert_chat(
        {"id": 123, "type": "private", "first_name": "After", "username": None}, account_id=1
    )
    row = (await real_adapter.get_all_chats(account_id=1))[0]
    assert row["first_name"] == "After"
    assert row["username"] is None


async def test_author_identity_survives_empty_but_accepts_named_updates(real_adapter):
    await real_adapter.upsert_user(
        {"id": 123, "first_name": "Sample", "last_name": "Person", "username": "sample_user"}
    )
    await real_adapter.upsert_user({"id": 123, "first_name": None, "last_name": None, "username": None, "phone": None})
    async with real_adapter.db_manager.async_session_factory() as session:
        user = (await session.execute(select(User).where(User.id == 123))).scalar_one()
        assert (user.first_name, user.last_name, user.username) == ("Sample", "Person", "sample_user")
    await real_adapter.upsert_user({"id": 123, "first_name": "New", "username": None})
    async with real_adapter.db_manager.async_session_factory() as session:
        user = (await session.execute(select(User).where(User.id == 123))).scalar_one()
        assert user.first_name == "New"
        assert user.username is None
