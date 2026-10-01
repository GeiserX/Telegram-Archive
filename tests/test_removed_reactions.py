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


class TestThePageCapsTheHistory:
    """A busy post can hold hundreds of reaction states. The page returns its
    newest PAGE_REACTION_HISTORY_LIMIT, plus each emoji's newest state, latest
    drop and return, says how many it left out, and the reactions taken back
    read exactly as from the whole history. Both exports keep every state."""

    async def _busy_post(self, adapter) -> None:
        """❤️ drops 7 to 5, 👍 is taken back and given again, and 🎉 climbs 1 to 2,
        all long ago; then 🔥 climbs through 30 states, so they fall outside the cap."""
        await _seed(adapter, 1)
        snapshots = [
            [("❤️", 7), ("👍", 4), ("🎉", 1)],
            [("❤️", 5), ("👍", 4), ("🎉", 1)],
            [("❤️", 5), ("🎉", 1)],
            [("❤️", 5), ("👍", 2), ("🎉", 1)],
            [("❤️", 5), ("👍", 2), ("🎉", 2)],
        ]
        snapshots += [[("❤️", 5), ("👍", 2), ("🎉", 2), ("🔥", n)] for n in range(1, 31)]
        for observed in snapshots:
            await adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": e, "count": n} for e, n in observed], account_id=1)
        await adapter.reconcile_reactions(2, CHAT_ID, [{"emoji": "😮", "count": 1}], account_id=1)

    async def test_more_states_than_the_cap_are_cut_and_the_page_says_how_many(self, real_adapter):
        from telegram_archive.db.adapter import PAGE_REACTION_HISTORY_LIMIT

        await self._busy_post(real_adapter)
        page = await _page(real_adapter)
        history = page[1]["reaction_history"]

        # 2 ❤️ + 3 👍 + 2 🎉 + 30 🔥 kept; the newest 20 are 🔥 11 to 30, and
        # the viewer's states ride along: ❤️'s newest (its drop), 👍's drop and
        # return, and 🎉's newest, so an emoji with history is never left out.
        assert PAGE_REACTION_HISTORY_LIMIT == 20
        assert [(h["emoji"], h["count"], h["previous_count"]) for h in history] == [
            ("❤️", 5, 7),
            ("👍", 0, 4),
            ("👍", 2, 0),
            ("🎉", 2, 1),
        ] + [("🔥", n, n - 1) for n in range(11, 31)]
        assert page[1]["reaction_history_omitted"] == 37 - 24
        assert page[2]["reaction_history_omitted"] == 0
        assert [h["count"] for h in page[2]["reaction_history"]] == [1]

    async def test_the_reactions_taken_back_read_as_from_the_whole_history(self, real_adapter):
        await self._busy_post(real_adapter)
        page = await _page(real_adapter)
        exported = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT_ID, account_id=1)}
        whole = [
            {**state, "observed_at": datetime.fromisoformat(state["observed_at"])}
            for state in exported[1]["reaction_history"]
        ]

        removed = page[1]["removed_reactions"]
        assert [(r["emoji"], r["count"], r["count_before"], r["back_at"] is not None) for r in removed] == [
            ("👍", 4, 4, True),
            ("❤️", 2, 7, False),
        ]
        live = {r["emoji"] for r in page[1]["reactions"]}
        assert removed == real_adapter._removed_reactions(whole, {}, live)

    async def test_both_exports_keep_every_state(self, real_adapter):
        await self._busy_post(real_adapter)
        viewer = {m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT_ID, account_id=1)}
        command = {m["id"]: m for m in await real_adapter.get_messages_for_backup_export(CHAT_ID, account_id=1)}

        assert len(viewer[1]["reaction_history"]) == 37
        assert len(command[1]["reaction_history"]) == 37
        assert "reaction_history_omitted" not in viewer[1]
        assert "reaction_history_omitted" not in command[1]
