"""Paging through ``/api/changes`` lists every row exactly once.

The feed pages newest first. Its old cursor was the last row's date alone, so
the rows that shared that exact date with the page's last row were skipped:
a burst of deletions or edits stored at one instant lost all but the first
page's share. The cursor now names the row itself (its date, its kind and the
row's own keys), so a walk that follows ``next_cursor`` sees each row once,
whatever the page size and however the streams interleave.

Runs on ``real_adapter``, so the SQL is compiled and executed by SQLite and
PostgreSQL.
"""

import os
import tempfile
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_changes_paging_"))

from test_account_fold_followups import BASE, add_version
from test_changes_chat_filter import GROUP_A, GROUP_B, as_principal, client, events, seed_two_groups
from test_changes_chat_filter import app_on as app_on  # noqa: F401  (pytest fixture)

from telegram_archive.db.adapter import ChatScope

UNRESTRICTED = ChatScope.build()
SAME_INSTANT = BASE + timedelta(hours=2)


async def seed_a_burst(adapter) -> list[tuple[str, str, int]]:
    """Five deletions and four edits stored at one instant, plus one row either side.

    Returns every row the feed must list, as ``events`` names them.
    """
    await seed_two_groups(adapter, messages=6)
    expected = []
    for message_id in range(1, 6):
        await adapter.mark_message_deleted(GROUP_A, message_id, SAME_INSTANT, account_id=1)
        expected.append(("deleted", "group a", message_id))
    for message_id in range(1, 5):
        await add_version(
            adapter, account_id=1, chat_id=GROUP_B, message_id=message_id, old_text="was", at=SAME_INSTANT
        )
        expected.append(("edited", "group b", message_id))
    await adapter.mark_message_deleted(GROUP_B, 6, SAME_INSTANT + timedelta(minutes=1), account_id=1)
    expected.append(("deleted", "group b", 6))
    await add_version(
        adapter, account_id=1, chat_id=GROUP_A, message_id=6, old_text="was", at=SAME_INSTANT - timedelta(minutes=1)
    )
    expected.append(("edited", "group a", 6))
    return expected


async def walk(http, *, limit: int) -> list[tuple[str, str, int]]:
    """Follow the feed's own cursor to the end and return every row seen."""
    seen = []
    params = {"limit": limit}
    for _ in range(50):
        resp = await http.get("/api/changes", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(events(body["changes"]))
        if body.get("next_cursor"):
            params = {"limit": limit, "cursor": body["next_cursor"]}
        elif body["next_before"]:
            params = {"limit": limit, "before": body["next_before"]}
        else:
            return seen
    raise AssertionError("the feed never ended")


class TestPagingIsLossless:
    @pytest.mark.parametrize("limit", [1, 2, 3, 4, 50])
    async def test_rows_sharing_a_date_across_a_page_boundary_are_all_listed(self, app_on, limit):
        expected = await seed_a_burst(app_on)
        as_principal(role="master")

        async with client() as http:
            seen = await walk(http, limit=limit)

        assert len(seen) == len(set(seen)), f"listed twice: {seen}"
        assert sorted(seen) == sorted(expected)
        # Newest first: the later deletion leads, the earlier edit closes.
        assert seen[0] == ("deleted", "group b", 6)
        assert seen[-1] == ("edited", "group a", 6)

    async def test_one_page_and_many_pages_list_the_same_order(self, app_on):
        await seed_a_burst(app_on)
        as_principal(role="master")

        async with client() as http:
            whole = await walk(http, limit=50)
            paged = await walk(http, limit=2)

        assert paged == whole

    async def test_the_adapter_pages_with_the_row_cursor(self, real_adapter):
        expected = await seed_a_burst(real_adapter)

        seen = []
        cursor = None
        for _ in range(20):
            page = await real_adapter.get_recent_changes(cursor=cursor, scope=UNRESTRICTED, limit=3)
            seen.extend(events(page))
            if len(page) < 3:
                break
            cursor = page[-1]["cursor"]

        assert sorted(seen) == sorted(expected)
        assert len(seen) == len(set(seen))


class TestCursorInput:
    async def test_a_malformed_cursor_is_a_400(self, app_on):
        as_principal(role="master")

        async with client() as http:
            for bad in ("not-a-cursor", "W10", "WyIyMDI2LTA0LTAxVDEyOjAwOjAwIiwgIm5vcGUiLCBbMV1d"):
                resp = await http.get("/api/changes", params={"cursor": bad})
                assert resp.status_code == 400, (bad, resp.text)

    async def test_the_old_date_cursor_still_pages(self, app_on):
        """``before`` keeps working for a client that sends it: an exclusive date bound."""
        await seed_a_burst(app_on)
        as_principal(role="master")

        async with client() as http:
            first = (await http.get("/api/changes", params={"limit": 1})).json()
            older = (await http.get("/api/changes", params={"before": first["next_before"]})).json()

        assert first["next_before"] == first["changes"][-1]["date"]
        assert ("deleted", "group b", 6) not in events(older["changes"])
        assert len(older["changes"]) == 10
