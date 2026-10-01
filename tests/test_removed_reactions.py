"""Reactions taken back come back to the viewer beside the live ones.

``reconcile_reactions`` keeps a reaction that left Telegram as a tombstone
(``removed_at``, #219). The page read used to filter those rows out, so the
viewer could never show what the archive kept. It now returns them as
``removed_reactions``: one entry per emoji, newest first, never inside the live
count. Since 037 the entry is the emoji's latest drop in ``reaction_history``:
how many went, the count before (a partial drop keeps it), when the archive
noticed, and when an emoji taken back to zero came back. The page also returns
the history itself as ``reaction_history``.

These run on a real engine, SQLite and PostgreSQL (``real_adapter``), through
the same writer the backup and the listener use.
"""

from datetime import datetime, timedelta

from sqlalchemy import update

from telegram_archive.db.models import Account, Reaction, ReactionHistory

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
    """Move a removal in time: the tombstone and the history row that recorded it."""
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(
            update(Reaction)
            .where(Reaction.chat_id == CHAT_ID, Reaction.message_id == message_id, Reaction.emoji == emoji)
            .values(removed_at=when)
        )
        await session.execute(
            update(ReactionHistory)
            .where(
                ReactionHistory.chat_id == CHAT_ID,
                ReactionHistory.message_id == message_id,
                ReactionHistory.emoji == emoji,
                ReactionHistory.count == 0,
            )
            .values(observed_at=when)
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

    async def test_an_emoji_that_comes_back_is_live_again_and_keeps_its_removal(self, real_adapter):
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "🔥", "count": 1}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "🔥", "count": 4}], account_id=1)

        page = await _page(real_adapter)
        assert [(r["emoji"], r["count"]) for r in page[1]["reactions"]] == [("🔥", 4)]
        [removed] = page[1]["removed_reactions"]
        assert (removed["emoji"], removed["count"], removed["count_before"]) == ("🔥", 1, 1)
        assert removed["removed_at"] < removed["back_at"]

    async def test_a_partial_drop_is_listed_with_the_count_before_it(self, real_adapter):
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "❤️", "count": 7}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "❤️", "count": 5}], account_id=1)

        page = await _page(real_adapter)
        assert page[1]["reactions"] == [{"emoji": "❤️", "count": 5, "user_ids": []}]
        [removed] = page[1]["removed_reactions"]
        assert (removed["emoji"], removed["count"], removed["count_before"], removed["back_at"]) == ("❤️", 2, 7, None)

    async def test_only_the_latest_drop_of_an_emoji_is_listed(self, real_adapter):
        """7 to 5, back up to 6, then 6 to 3: the list says "3 of 6"; the whole
        story stays in reaction_history."""
        await _seed(real_adapter, 1)
        for n in (7, 5, 6, 3):
            await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "❤️", "count": n}], account_id=1)

        page = await _page(real_adapter)
        assert [(r["count"], r["count_before"]) for r in page[1]["removed_reactions"]] == [(3, 6)]
        assert [(h["count"], h["previous_count"]) for h in page[1]["reaction_history"]] == [
            (7, None),
            (5, 7),
            (6, 5),
            (3, 6),
        ]

    async def test_the_page_returns_every_kept_state_oldest_first(self, real_adapter):
        await _seed(real_adapter, 1)
        await real_adapter.reconcile_reactions(2, CHAT_ID, [{"emoji": "😮", "count": 1}], account_id=1, source="backup")
        await real_adapter.reconcile_reactions(2, CHAT_ID, [], account_id=1, source="listener")

        page = await _page(real_adapter)
        history = page[2]["reaction_history"]
        assert [(h["emoji"], h["count"], h["previous_count"], h["source"]) for h in history] == [
            ("😮", 1, None, "backup"),
            ("😮", 0, 1, "listener"),
        ]
        assert all(isinstance(h["observed_at"], datetime) for h in history)
        assert set(history[0]) == {"emoji", "count", "previous_count", "observed_at", "source"}
        assert page[1]["reaction_history"] == []

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
        assert {h["emoji"] for h in first[1]["reaction_history"]} == {"👍"}
        assert {h["emoji"] for h in second[1]["reaction_history"]} == {"🎉"}

    async def test_rows_reconcile_never_writes_still_read_right(self, real_adapter):
        """reconcile_reactions keeps one row per emoji, but the read must not
        depend on it: a tombstone beside a live row of the same emoji stays out
        of the removed list, and two tombstones of one emoji merge into one
        entry with the summed count and the later time."""
        await _seed(real_adapter, 1)
        earlier = BASE_DATE + timedelta(hours=1)
        later = BASE_DATE + timedelta(hours=2)
        async with real_adapter.db_manager.async_session_factory() as session:
            for emoji, count, removed_at in (
                ("👍", 2, None),
                ("👍", 1, earlier),
                ("🔥", 1, earlier),
                ("🔥", 2, later),
            ):
                session.add(
                    Reaction(
                        account_id=1,
                        message_id=1,
                        chat_id=CHAT_ID,
                        emoji=emoji,
                        user_id=None,
                        count=count,
                        created_at=BASE_DATE,
                        removed_at=removed_at,
                    )
                )
            await session.commit()

        page = await _page(real_adapter)
        assert page[1]["reactions"] == [{"emoji": "👍", "count": 2, "user_ids": []}]
        removed = page[1]["removed_reactions"]
        assert [(r["emoji"], r["count"], r["removed_at"]) for r in removed] == [("🔥", 3, later)]

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

    async def test_a_tombstone_without_a_count_reads_as_one(self, real_adapter):
        """reconcile_reactions stores the summed count on a tombstone, so a
        legacy group whose counts were NULL leaves count 0. The entry and the
        toggle total must still say one reaction went, never zero."""
        await _seed(real_adapter, 1)
        async with real_adapter.db_manager.async_session_factory() as session:
            session.add(
                Reaction(
                    account_id=1,
                    message_id=1,
                    chat_id=CHAT_ID,
                    emoji="👍",
                    user_id=None,
                    count=0,
                    created_at=BASE_DATE,
                    removed_at=BASE_DATE + timedelta(hours=1),
                )
            )
            await session.commit()

        page = await _page(real_adapter)
        assert [(r["emoji"], r["count"]) for r in page[1]["removed_reactions"]] == [("👍", 1)]
