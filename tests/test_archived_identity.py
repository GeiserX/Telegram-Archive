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


async def _user_row(adapter, user_id):
    async with adapter.db_manager.async_session_factory() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        return (user.first_name, user.last_name, user.username, user.phone)


async def test_deleted_snapshot_keeps_archived_phone(real_adapter):
    named = {"id": 140, "first_name": "Sample", "last_name": "Person", "username": "sample_user", "phone": "15550100"}
    deleted = {"id": 140, "first_name": None, "last_name": None, "username": None, "phone": None}
    await real_adapter.upsert_chat({**named, "type": "private"}, account_id=1)
    await real_adapter.upsert_chat({**deleted, "type": "private"}, account_id=1)
    assert (await real_adapter.get_all_chats(account_id=1))[0]["phone"] == "15550100"
    await real_adapter.upsert_user(named)
    await real_adapter.upsert_user(deleted)
    assert await _user_row(real_adapter, 140) == ("Sample", "Person", "sample_user", "15550100")


async def test_unnamed_snapshot_with_phone_still_updates_phone(real_adapter):
    named = {"id": 141, "first_name": "Sample", "last_name": "Person", "username": "sample_user", "phone": "15550100"}
    unnamed = {"id": 141, "first_name": "", "last_name": "", "username": "", "phone": "15550199"}
    await real_adapter.upsert_chat({**named, "type": "private"}, account_id=1)
    await real_adapter.upsert_chat({**unnamed, "type": "private"}, account_id=1)
    row = (await real_adapter.get_all_chats(account_id=1))[0]
    assert (row["first_name"], row["phone"]) == ("Sample", "15550199")
    await real_adapter.upsert_user(named)
    await real_adapter.upsert_user(unnamed)
    assert await _user_row(real_adapter, 141) == ("Sample", "Person", "sample_user", "15550199")


async def test_first_name_only_snapshot_still_overwrites(real_adapter):
    named = {"id": 150, "first_name": "Sample", "last_name": "Person", "username": "sample_user"}
    first_only = {"id": 150, "first_name": "Solo", "last_name": None, "username": None}
    await real_adapter.upsert_chat({**named, "type": "private"}, account_id=1)
    await real_adapter.upsert_chat({**first_only, "type": "private"}, account_id=1)
    row = (await real_adapter.get_all_chats(account_id=1))[0]
    assert (row["first_name"], row["last_name"], row["username"]) == ("Solo", None, None)
    await real_adapter.upsert_user(named)
    await real_adapter.upsert_user(first_only)
    assert (await _user_row(real_adapter, 150))[:3] == ("Solo", None, None)


async def test_partial_update_without_names_clears_username(real_adapter):
    await real_adapter.upsert_chat(
        {"id": 151, "type": "private", "first_name": "Sample", "last_name": "Person", "username": "sample_user"},
        account_id=1,
    )
    await real_adapter.upsert_chat({"id": 151, "type": "private", "username": None}, account_id=1)
    row = (await real_adapter.get_all_chats(account_id=1))[0]
    assert (row["first_name"], row["last_name"], row["username"]) == ("Sample", "Person", None)
    await real_adapter.upsert_user(
        {"id": 151, "first_name": "Sample", "last_name": "Person", "username": "sample_user"}
    )
    await real_adapter.upsert_user({"id": 151, "username": None})
    assert (await _user_row(real_adapter, 151))[:3] == ("Sample", "Person", None)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"first_name": None, "last_name": "Person", "username": None},
        {"first_name": None, "last_name": None, "username": "new_handle"},
    ],
)
async def test_snapshot_with_one_identity_field_still_overwrites(real_adapter, snapshot):
    named = {"id": 160, "first_name": "Sample", "last_name": "Other", "username": "sample_user"}
    await real_adapter.upsert_chat({**named, "type": "private"}, account_id=1)
    await real_adapter.upsert_chat({"id": 160, "type": "private", **snapshot}, account_id=1)
    row = (await real_adapter.get_all_chats(account_id=1))[0]
    expected = (snapshot["first_name"], snapshot["last_name"], snapshot["username"])
    assert (row["first_name"], row["last_name"], row["username"]) == expected
    await real_adapter.upsert_user(named)
    await real_adapter.upsert_user({"id": 160, **snapshot})
    assert (await _user_row(real_adapter, 160))[:3] == expected


async def test_channel_snapshot_without_names_clears_username(real_adapter):
    # The listener sends first_name/last_name keys for every chat type, so a
    # channel that drops its public username must not count as a deleted user.
    await real_adapter.upsert_chat({"id": -1001, "type": "channel", "title": "News", "username": "news"}, account_id=1)
    await real_adapter.upsert_chat(
        {"id": -1001, "type": "channel", "title": "News", "username": None, "first_name": None, "last_name": None},
        account_id=1,
    )
    row = (await real_adapter.get_all_chats(account_id=1))[0]
    assert (row["title"], row["username"]) == ("News", None)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # A lone "@" is not an empty handle search that lists every chat with a username.
        ("@", set()),
        # The full name is the trimmed join, so a first-name-only chat reads "Solo", not "Solo ".
        ("Solo ", set()),
        ("100%", {172}),
        ("a_b", {173}),
        ("c\\d", {175}),
    ],
)
async def test_chat_search_edge_queries(real_adapter, query, expected):
    chats = [
        {"id": 170, "type": "private", "first_name": "Solo", "username": "solo_user"},
        {"id": 171, "type": "group", "title": "1000 members"},
        {"id": 172, "type": "group", "title": "100% match"},
        {"id": 173, "type": "group", "title": "a_b"},
        {"id": 174, "type": "group", "title": "axb cd"},
        {"id": 175, "type": "group", "title": "c\\d"},
    ]
    for chat in chats:
        await real_adapter.upsert_chat(chat, account_id=1)
    rows = await real_adapter.get_all_chats(search=query, account_id=1)
    assert {row["id"] for row in rows} == expected
    assert await real_adapter.get_chat_count(search=query, account_id=1) == len(expected)
