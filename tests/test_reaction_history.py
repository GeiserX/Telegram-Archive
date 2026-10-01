"""Every observed state of a message's reactions is kept in ``reaction_history``.

``reconcile_reactions`` keeps one ``reactions`` row per emoji with the current
count. Before 037 a count that dropped without reaching zero overwrote the
earlier count, and an emoji that came back cleared its tombstone. Now each
reconcile that moves an emoji's count adds a history row (count, the count
before it, when, and which path saw it), and nothing in the history is ever
updated or removed by a reconcile.

These run on a real engine, SQLite and PostgreSQL (``real_adapter``), through
the same writer the backup and the listener use.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from telegram_archive.db.models import Account, Reaction, ReactionHistory

CHAT_ID = -100910
BASE_DATE = datetime(2026, 9, 1, 12, 0, 0)


async def _seed(adapter, *accounts: int, messages: tuple[int, ...] = (1, 2)) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        for account_id in accounts:
            await session.merge(Account(id=account_id, label=f"Account {account_id}"))
        await session.commit()
    for account_id in accounts:
        await adapter.upsert_chat({"id": CHAT_ID, "type": "supergroup", "title": "fixture"}, account_id=account_id)
        for message_id in messages:
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


_CLOCK = {"minute": 0}


@pytest.fixture
def clock():
    """Each reconcile sees the next minute, so the order of states is plain."""
    _CLOCK["minute"] = 0
    yield


def _at(minute: int) -> datetime:
    return BASE_DATE + timedelta(hours=1, minutes=minute)


async def _history(adapter, message_id: int = 1, account_id: int = 1) -> list[tuple]:
    async with adapter.db_manager.async_session_factory() as session:
        rows = (
            await session.execute(
                select(ReactionHistory)
                .where(
                    ReactionHistory.account_id == account_id,
                    ReactionHistory.chat_id == CHAT_ID,
                    ReactionHistory.message_id == message_id,
                )
                .order_by(ReactionHistory.observed_at, ReactionHistory.id)
            )
        ).scalars()
        return [(r.emoji, r.count, r.previous_count, r.observed_at, r.source) for r in rows]


async def _reconcile(adapter, observed: list[tuple[str, int]], message_id: int = 1, account_id: int = 1, **kwargs):
    _CLOCK["minute"] += 1
    with patch("telegram_archive.db.adapter.utcnow_naive", return_value=_at(_CLOCK["minute"])):
        return await adapter.reconcile_reactions(
            message_id,
            CHAT_ID,
            [{"emoji": emoji, "count": n} for emoji, n in observed],
            account_id=account_id,
            **kwargs,
        )


class TestTheHistoryKeepsEveryState:
    async def test_a_count_that_drops_without_reaching_zero_keeps_the_earlier_count(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await _reconcile(real_adapter, [("❤️", 7)], source="backup")
        await _reconcile(real_adapter, [("❤️", 5)], source="listener")

        assert await _history(real_adapter) == [
            ("❤️", 7, None, _at(1), "backup"),
            ("❤️", 5, 7, _at(2), "listener"),
        ]

    async def test_an_emoji_that_comes_back_keeps_its_removal(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await _reconcile(real_adapter, [("😮", 1)], source="listener")
        await _reconcile(real_adapter, [], source="listener")
        await _reconcile(real_adapter, [("😮", 1)], source="listener")

        assert [(e, n, p, t) for e, n, p, t, _s in await _history(real_adapter)] == [
            ("😮", 1, None, _at(1)),
            ("😮", 0, 1, _at(2)),
            ("😮", 1, 0, _at(3)),
        ]

    async def test_an_unchanged_snapshot_adds_nothing(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        assert await _reconcile(real_adapter, [("👍", 2), ("🔥", 1)]) == "reconciled"
        assert await _reconcile(real_adapter, [("🔥", 1), ("👍", 2)]) == "noop"
        await _reconcile(real_adapter, [])
        assert await _reconcile(real_adapter, []) == "noop"

        assert [(e, n) for e, n, *_ in await _history(real_adapter)] == [("👍", 2), ("🔥", 1), ("👍", 0), ("🔥", 0)]

    async def test_only_the_emoji_that_moved_gets_a_row(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await _reconcile(real_adapter, [("👍", 2), ("🔥", 1)])
        await _reconcile(real_adapter, [("👍", 3), ("🔥", 1)])

        assert [(e, n, p) for e, n, p, *_ in await _history(real_adapter)] == [
            ("👍", 2, None),
            ("🔥", 1, None),
            ("👍", 3, 2),
        ]

    async def test_a_reconcile_never_changes_or_removes_a_kept_row(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        snapshots = [[("👍", 1)], [("👍", 4)], [("👍", 2)], [], [("👍", 2)], [("👍", 9), ("🎉", 1)], []]
        seen: list[tuple] = []
        for snapshot in snapshots:
            await _reconcile(real_adapter, snapshot)
            now = await _history(real_adapter)
            assert now[: len(seen)] == seen
            seen = now
        assert len(seen) == 9

    async def test_each_account_keeps_its_own_history(self, real_adapter, clock):
        await _seed(real_adapter, 1, 2)
        await _reconcile(real_adapter, [("👍", 3)], account_id=1)
        await _reconcile(real_adapter, [("👍", 1)], account_id=2)
        await _reconcile(real_adapter, [("👍", 2)], account_id=1)

        assert [(e, n, p) for e, n, p, *_ in await _history(real_adapter, account_id=1)] == [
            ("👍", 3, None),
            ("👍", 2, 3),
        ]
        assert [(e, n, p) for e, n, p, *_ in await _history(real_adapter, account_id=2)] == [("👍", 1, None)]

    async def test_a_message_the_archive_does_not_hold_writes_no_history(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        assert await _reconcile(real_adapter, [("👍", 1)], message_id=99) == "no_message"
        assert await _history(real_adapter, message_id=99) == []


class TestTheBaseline:
    """A ``reactions`` row with no history (written before 037, or by a path
    that wrote the row directly) gets the baseline it stands for before the
    first change, so the earlier count is kept too."""

    async def _add_rows(self, adapter, rows: list[tuple[str, int | None, datetime | None]]) -> None:
        async with adapter.db_manager.async_session_factory() as session:
            for emoji, n, removed_at in rows:
                session.add(
                    Reaction(
                        account_id=1,
                        message_id=1,
                        chat_id=CHAT_ID,
                        emoji=emoji,
                        user_id=None,
                        count=n,
                        created_at=BASE_DATE,
                        removed_at=removed_at,
                    )
                )
            await session.commit()

    async def test_a_live_row_is_kept_before_its_count_drops(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await self._add_rows(real_adapter, [("❤️", 7, None)])
        await _reconcile(real_adapter, [("❤️", 5)], source="listener")

        assert await _history(real_adapter) == [
            ("❤️", 7, None, BASE_DATE, "baseline"),
            ("❤️", 5, 7, _at(1), "listener"),
        ]

    async def test_a_tombstone_is_kept_before_the_emoji_comes_back(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        gone = BASE_DATE + timedelta(minutes=30)
        await self._add_rows(real_adapter, [("😮", 2, gone)])
        await _reconcile(real_adapter, [("😮", 1)], source="backup")

        assert await _history(real_adapter) == [
            ("😮", 2, None, BASE_DATE, "baseline"),
            ("😮", 0, 2, gone, "baseline"),
            ("😮", 1, 0, _at(1), "backup"),
        ]

    async def test_an_unchanged_row_gets_its_baseline_once(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await self._add_rows(real_adapter, [("👍", 3, None)])
        assert await _reconcile(real_adapter, [("👍", 3)]) == "noop"
        assert await _reconcile(real_adapter, [("👍", 3)]) == "noop"

        assert await _history(real_adapter) == [("👍", 3, None, BASE_DATE, "baseline")]

    async def test_legacy_live_rows_of_one_emoji_add_up(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await self._add_rows(real_adapter, [("👍", 1, None), ("👍", 2, None), ("👍", 4, BASE_DATE)])
        await _reconcile(real_adapter, [("👍", 1)])

        assert [(e, n, p) for e, n, p, *_ in await _history(real_adapter)] == [("👍", 3, None), ("👍", 1, 3)]

    async def test_legacy_tombstones_add_up_and_a_zero_count_reads_as_one(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await self._add_rows(real_adapter, [("👍", 0, BASE_DATE), ("👍", 2, BASE_DATE + timedelta(minutes=5))])
        await _reconcile(real_adapter, [("👍", 1)])

        assert [(e, n, p) for e, n, p, *_ in await _history(real_adapter)] == [
            ("👍", 3, None),
            ("👍", 0, 3),
            ("👍", 1, 0),
        ]


class TestRemovalsTheOperatorAskedFor:
    """Hard deletion and an excluded chat remove a message's reactions; its
    history goes with them, on SQLite too, where foreign keys do not cascade."""

    async def test_deleting_a_message_removes_its_history(self, real_adapter, clock):
        await _seed(real_adapter, 1)
        await _reconcile(real_adapter, [("👍", 2)], message_id=1)
        await _reconcile(real_adapter, [("👍", 2)], message_id=2)
        await real_adapter.delete_message(CHAT_ID, 1, account_id=1)

        assert await _history(real_adapter, message_id=1) == []
        assert [(e, n) for e, n, *_ in await _history(real_adapter, message_id=2)] == [("👍", 2)]

    async def test_deleting_a_chat_removes_its_history_and_leaves_other_accounts(self, real_adapter, clock):
        await _seed(real_adapter, 1, 2)
        await _reconcile(real_adapter, [("👍", 2)], account_id=1)
        await _reconcile(real_adapter, [("👍", 5)], account_id=2)
        await real_adapter.delete_chat_and_related_data(CHAT_ID, account_id=1)

        assert await _history(real_adapter, account_id=1) == []
        assert [(e, n) for e, n, *_ in await _history(real_adapter, account_id=2)] == [("👍", 5)]
