"""Reactions taken back come back to the viewer beside the live ones.

``reconcile_reactions`` keeps a reaction that left Telegram as a tombstone
(``removed_at``, #219). The page read used to filter those rows out, so the
viewer could never show what the archive kept. It now returns them as
``removed_reactions``: one entry per emoji, with the count it had and when the
archive noticed it gone, newest first, and never inside the live count.

These run on a real engine, SQLite and PostgreSQL (``real_adapter``), through
the same writer the backup and the listener use.
"""

from datetime import datetime, timedelta

from sqlalchemy import update

from telegram_archive.db.models import Account, Reaction

CHAT_ID = -100900
BASE_DATE = datetime(2026, 9, 1, 12, 0, 0)


async def _seed(adapter, *accounts: int) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        for account_id in accounts:
            await session.merge(Account(id=account_id, label=f"Account {account_id}"))
        await session.commit()
    for account_id in accounts:
        await adapter.upsert_chat({"id": CHAT_ID, "type": "supergroup", "title": "fixture"}, account_id=account_id)
        for message_id in (1, 2):
            await adapter.insert_message(
                {
                    "id": message_id,
                    "chat_id": CHAT_ID,
                    "sender_id": 4242,
                    "date": BASE_DATE + timedelta(minutes=message_id),
                    "text": f"message {message_id}",
                    "raw_data": {},
                },
                account_id=account_id,
            )


async def _set_removed_at(adapter, message_id: int, emoji: str, when: datetime) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            update(Reaction)
            .where(Reaction.chat_id == CHAT_ID, Reaction.message_id == message_id, Reaction.emoji == emoji)
            .values(removed_at=when)
        )
        await session.commit()


async def _page(adapter, account_id: int = 1) -> dict[int, dict]:
    rows = await adapter.get_messages_paginated(chat_id=CHAT_ID, account_id=account_id)
    return {row["id"]: row for row in rows}


class TestRemovedReactionsOnThePage:
    async def test_a_reaction_taken_back_leaves_the_live_count_and_comes_back_beside_it(self, real_adapter):
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(
            2, CHAT_ID, [{"emoji": "👍", "count": 3}, {"emoji": "❤️", "count": 1}], account_id=1
        )
        await real_adapter.reconcile_reactions(2, CHAT_ID, [{"emoji": "❤️", "count": 1}], account_id=1)

        page = await _page(real_adapter)
        assert page[2]["reactions"] == [{"emoji": "❤️", "count": 1, "user_ids": []}]
        removed = page[2]["removed_reactions"]
        assert [(r["emoji"], r["count"]) for r in removed] == [("👍", 3)]
        assert isinstance(removed[0]["removed_at"], datetime)
        # A message with no reactions of either kind still gets both lists.
        assert page[1]["reactions"] == []
        assert page[1]["removed_reactions"] == []

    async def test_all_taken_back_leaves_only_the_removed_list(self, real_adapter):
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "😮", "count": 2}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [], account_id=1)

        page = await _page(real_adapter)
        assert page[1]["reactions"] == []
        assert [(r["emoji"], r["count"]) for r in page[1]["removed_reactions"]] == [("😮", 2)]

    async def test_an_emoji_that_comes_back_is_live_again_and_not_listed_as_removed(self, real_adapter):
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "🔥", "count": 1}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "🔥", "count": 4}], account_id=1)

        page = await _page(real_adapter)
        assert [(r["emoji"], r["count"]) for r in page[1]["reactions"]] == [("🔥", 4)]
        assert page[1]["removed_reactions"] == []

    async def test_newest_removal_first(self, real_adapter):
        await _seed(real_adapter, 1)
        observed = [{"emoji": "👍", "count": 1}, {"emoji": "🎉", "count": 2}, {"emoji": "😮", "count": 1}]
        await real_adapter.reconcile_reactions(2, CHAT_ID, observed, account_id=1)
        await real_adapter.reconcile_reactions(2, CHAT_ID, [], account_id=1)
        await _set_removed_at(real_adapter, 2, "👍", BASE_DATE + timedelta(hours=1))
        await _set_removed_at(real_adapter, 2, "🎉", BASE_DATE + timedelta(hours=3))
        await _set_removed_at(real_adapter, 2, "😮", BASE_DATE + timedelta(hours=2))

        page = await _page(real_adapter)
        assert [(r["emoji"], r["removed_at"]) for r in page[2]["removed_reactions"]] == [
            ("🎉", BASE_DATE + timedelta(hours=3)),
            ("😮", BASE_DATE + timedelta(hours=2)),
            ("👍", BASE_DATE + timedelta(hours=1)),
        ]

    async def test_another_accounts_copy_of_the_chat_stays_out(self, real_adapter):
        """Two accounts hold the same chat id and message ids: each page shows
        only its own account's reactions taken back."""
        await _seed(real_adapter, 1, 2)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "👍", "count": 1}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "🎉", "count": 5}], account_id=2)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [], account_id=2)

        first = await _page(real_adapter, account_id=1)
        second = await _page(real_adapter, account_id=2)
        assert [(r["emoji"], r["count"]) for r in first[1]["removed_reactions"]] == [("👍", 1)]
        assert [(r["emoji"], r["count"]) for r in second[1]["removed_reactions"]] == [("🎉", 5)]

    async def test_the_read_keeps_every_row(self, real_adapter):
        """Reading is read-only: the tombstones are all still there after it."""
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "👍", "count": 1}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [], account_id=1)
        await _page(real_adapter)
        await _page(real_adapter)

        async with real_adapter.db_manager.async_session_factory() as session:
            rows = (await session.execute(Reaction.__table__.select())).mappings().all()
        assert [(row["emoji"], row["count"], row["removed_at"] is not None) for row in rows] == [("👍", 1, True)]
