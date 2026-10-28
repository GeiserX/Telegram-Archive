"""Both chat exports can pick only the messages whose archive record changed since a time.

An operator who exports on a schedule needs each run to carry what the
archive learned since the last one: a message stored for the first time, one
seen deleted, and an old message that gained a text version, an earlier
media, a poll or preview state or a reaction state. Selecting by
``messages.date`` alone misses every change to an old message. Runs on SQLite
and PostgreSQL (``real_adapter``).
"""

import json
import re
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient

from telegram_archive.db.models import (
    MediaVersion,
    Message,
    MessageSnapshot,
    MessageVersion,
    ReactionHistory,
)
from telegram_archive.export_backup import BackupExporter
from telegram_archive.message_utils import parse_utc_timestamp
from telegram_archive.web import main as web_main

CHAT = -420800077
OTHER_CHAT = -420800078
SENT = datetime(2025, 12, 1, 9, 0, 0)  # Telegram date of every old message
STORED = datetime(2026, 1, 1, 0, 0, 0)  # when the archive first stored the old messages
SINCE = datetime(2026, 2, 1, 10, 0, 0, 250000)
# Same day as SINCE: stored as text with a space on SQLite, these are the rows
# a string bound with a "T" would misorder.
BEFORE = datetime(2026, 2, 1, 8, 0, 0)
AFTER = datetime(2026, 2, 1, 18, 0, 0)

EDITED, DELETED, NEW, UNTOUCHED, MEDIA_REPLACED, POLL_MOVED, REACTED, AT_SINCE = range(1, 9)
CHANGED = [EDITED, DELETED, NEW, MEDIA_REPLACED, POLL_MOVED, REACTED, AT_SINCE]


def _message(
    message_id: int, *, chat_id: int = CHAT, created_at: datetime = STORED, account_id: int = 1, **extra
) -> Message:
    return Message(
        account_id=account_id,
        id=message_id,
        chat_id=chat_id,
        date=SENT + timedelta(minutes=message_id),
        text=f"fixture text {message_id}",
        created_at=created_at,
        **extra,
    )


def _version(message_id: int, text: str, captured_at: datetime, *, chat_id: int = CHAT) -> MessageVersion:
    return MessageVersion(
        account_id=1,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        date=SENT,
        change_hash=f"{chat_id}-{message_id}-{text}",
        captured_at=captured_at,
    )


async def _seed(adapter) -> None:
    """One message per kind of change after SINCE, one untouched, one changed only before SINCE."""
    for chat_id in (CHAT, OTHER_CHAT):
        await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "Fixture Group"}, account_id=1)
    async with adapter.db_manager.async_session_factory() as session:
        session.add_all(
            [
                _message(EDITED),
                _message(DELETED, is_deleted=1, deleted_at=AFTER),
                _message(NEW, created_at=AFTER),
                _message(UNTOUCHED),
                _message(MEDIA_REPLACED),
                _message(POLL_MOVED),
                _message(REACTED),
                _message(AT_SINCE, created_at=SINCE),
                _message(1, chat_id=OTHER_CHAT),
            ]
        )
        await session.flush()
        session.add_all(
            [
                # The edited message: one version kept before SINCE, one after.
                _version(EDITED, "first draft", BEFORE),
                _version(EDITED, "second draft", AFTER),
                # The untouched message changed too, but only before SINCE.
                _version(UNTOUCHED, "older draft", BEFORE),
                ReactionHistory(
                    account_id=1, chat_id=CHAT, message_id=UNTOUCHED, emoji="👍", count=1, observed_at=BEFORE
                ),
                MediaVersion(
                    account_id=1,
                    chat_id=CHAT,
                    message_id=MEDIA_REPLACED,
                    media_id=f"{CHAT}_{MEDIA_REPLACED}_photo",
                    type="photo",
                    downloaded=0,
                    date=SENT,
                    captured_at=AFTER,
                ),
                MessageSnapshot(
                    account_id=1,
                    chat_id=CHAT,
                    message_id=POLL_MOVED,
                    kind="poll",
                    payload=json.dumps({"question": "fixture?"}),
                    observed_at=AFTER,
                ),
                ReactionHistory(account_id=1, chat_id=CHAT, message_id=REACTED, emoji="👍", count=2, observed_at=AFTER),
                # Another chat's message edited after SINCE: a chat filter keeps it out.
                _version(1, "other chat draft", AFTER, chat_id=OTHER_CHAT),
            ]
        )
        await session.commit()


async def _seed_second_account(adapter) -> None:
    """Account 2 holds the same chat id and message id as account 1's untouched message.

    Only account 2's copy changes after SINCE: an edit, a replaced media, a
    poll state and a reaction state. Account 1's copy must stay out.
    """
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Group"}, account_id=2)
    async with adapter.db_manager.async_session_factory() as session:
        session.add(_message(UNTOUCHED, account_id=2))
        await session.flush()
        session.add_all(
            [
                MessageVersion(
                    account_id=2,
                    chat_id=CHAT,
                    message_id=UNTOUCHED,
                    text="account 2 draft",
                    date=SENT,
                    change_hash=f"2-{CHAT}-{UNTOUCHED}",
                    captured_at=AFTER,
                ),
                MediaVersion(
                    account_id=2,
                    chat_id=CHAT,
                    message_id=UNTOUCHED,
                    media_id=f"2_{CHAT}_{UNTOUCHED}_photo",
                    type="photo",
                    downloaded=0,
                    date=SENT,
                    captured_at=AFTER,
                ),
                MessageSnapshot(
                    account_id=2,
                    chat_id=CHAT,
                    message_id=UNTOUCHED,
                    kind="poll",
                    payload=json.dumps({"question": "fixture?"}),
                    observed_at=AFTER,
                ),
                ReactionHistory(
                    account_id=2, chat_id=CHAT, message_id=UNTOUCHED, emoji="👍", count=3, observed_at=AFTER
                ),
            ]
        )
        await session.commit()


class TestParseUtcTimestamp:
    def test_every_iso_form_lands_on_the_same_naive_utc_instant(self):
        expected = datetime(2026, 2, 1, 10, 0, 0)
        for value in (
            "2026-02-01T10:00:00",
            "2026-02-01 10:00:00",
            "2026-02-01T10:00:00Z",
            "2026-02-01 10:00:00+00:00",
            "2026-02-01T12:00:00+02:00",
            "2026-02-01T05:00:00-05:00",
            " 2026-02-01T10:00:00Z ",
        ):
            assert parse_utc_timestamp(value) == expected, value
            assert parse_utc_timestamp(value).tzinfo is None

    def test_a_bare_date_is_its_midnight(self):
        assert parse_utc_timestamp("2026-02-01") == datetime(2026, 2, 1)

    def test_anything_else_is_refused(self):
        # The last one is a valid offset whose UTC instant is before year 1.
        for value in ("not-a-time", "", "  ", "2026-13-01", "01/02/2026", "0001-01-01T00:00:00+01:00"):
            with pytest.raises(ValueError):
                parse_utc_timestamp(value)


class TestChangedSinceSelection:
    async def test_the_command_export_keeps_exactly_the_changed_messages(self, real_adapter):
        await _seed(real_adapter)
        messages = await real_adapter.get_messages_for_backup_export(CHAT, changed_since=SINCE)
        assert sorted(m["id"] for m in messages) == CHANGED

    async def test_the_viewer_export_keeps_exactly_the_changed_messages(self, real_adapter):
        await _seed(real_adapter)
        messages = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1, changed_since=SINCE)]
        assert sorted(m["id"] for m in messages) == CHANGED

    async def test_each_selected_message_carries_its_whole_history(self, real_adapter):
        await _seed(real_adapter)
        by_id = {
            m["id"]: m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1, changed_since=SINCE)
        }
        # The version kept before SINCE is still there beside the later one.
        assert [v["text"] for v in by_id[EDITED]["versions"]] == ["first draft", "second draft"]
        assert by_id[DELETED]["is_deleted"] is True
        assert [v["media"][0]["media_id"] for v in by_id[MEDIA_REPLACED]["versions"]] == [
            f"{CHAT}_{MEDIA_REPLACED}_photo"
        ]
        assert [s["kind"] for s in by_id[POLL_MOVED]["snapshots"]] == ["poll"]
        assert [(h["emoji"], h["count"]) for h in by_id[REACTED]["reaction_history"]] == [("👍", 2)]

    async def test_without_a_chat_it_spans_every_chat(self, real_adapter):
        await _seed(real_adapter)
        messages = await real_adapter.get_messages_for_backup_export(changed_since=SINCE)
        assert sorted((m["chat_id"], m["id"]) for m in messages) == sorted(
            [(CHAT, i) for i in CHANGED] + [(OTHER_CHAT, 1)]
        )

    async def test_it_narrows_the_date_window_rather_than_widening_it(self, real_adapter):
        await _seed(real_adapter)
        end = SENT + timedelta(minutes=REACTED)
        messages = await real_adapter.get_messages_for_backup_export(CHAT, end_date=end, changed_since=SINCE)
        assert sorted(m["id"] for m in messages) == [i for i in CHANGED if i <= REACTED]

    async def test_another_accounts_change_never_selects_this_accounts_copy(self, real_adapter):
        await _seed(real_adapter)
        await _seed_second_account(real_adapter)
        account_1 = [m async for m in real_adapter.get_messages_for_export(CHAT, account_id=1, changed_since=SINCE)]
        assert sorted(m["id"] for m in account_1) == CHANGED
        unscoped = await real_adapter.get_messages_for_backup_export(CHAT, changed_since=SINCE)
        assert sorted((m["account_id"], m["id"]) for m in unscoped) == sorted(
            [(1, i) for i in CHANGED] + [(2, UNTOUCHED)]
        )

    async def test_a_string_bound_is_refused(self, real_adapter):
        with pytest.raises(TypeError):
            await real_adapter.get_messages_for_backup_export(CHAT, changed_since="2026-02-01T10:00:00")


class TestQueryPlan:
    """On SQLite, a whole-archive incremental export reads only what changed.

    Captures every statement the export sends and EXPLAINs the ones carrying
    the changed-since test: none may scan messages or a history table, each
    must start from the timestamp indexes (042). PostgreSQL plans by table
    statistics, so the same check on a tiny fixture would prove nothing there.
    """

    TABLES = ("messages", "media", "message_versions", "media_versions", "reaction_history", "message_snapshots")

    async def test_no_statement_scans_the_archive(self, real_adapter):
        if not real_adapter._is_sqlite:
            pytest.skip("SQLite plans only; see the class docstring")
        await _seed(real_adapter)
        engine = real_adapter.db_manager.engine.sync_engine
        captured: list[tuple[str, object]] = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if "deleted_at >=" in statement:
                captured.append((statement, parameters))

        event.listen(engine, "before_cursor_execute", capture)
        try:
            await real_adapter.get_messages_for_backup_export(changed_since=SINCE)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(captured) == 7  # the messages, the five history walks and the transcripts

        scans = []
        async with real_adapter.db_manager.engine.connect() as conn:
            for statement, parameters in captured:
                raw = await conn.get_raw_connection()
                cursor = await raw.driver_connection.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)
                for row in await cursor.fetchall():
                    # "SCAN messages_1 USING ..." scans messages under an alias.
                    match = re.match(r"SCAN (\w+?)(?:_\d+)?\b", row[3])
                    if match and match.group(1) in self.TABLES:
                        scans.append(row[3])
        assert scans == []


class TestCommandExport:
    async def test_the_flag_reaches_the_file_and_is_recorded_in_its_filters(self, real_adapter, tmp_path):
        await _seed(real_adapter)
        output = tmp_path / "export.json"
        await BackupExporter(real_adapter).export_to_json(
            str(output), chat_id=CHAT, changed_since="2026-02-01T12:00:00.25+02:00"
        )
        data = json.loads(output.read_text(encoding="utf-8"))
        assert sorted(m["id"] for m in data["messages"]) == CHANGED
        assert data["filters"]["changed_since"] == SINCE.isoformat()

    async def test_a_bad_timestamp_fails_before_reading(self, real_adapter, tmp_path):
        output = tmp_path / "export.json"
        # An empty value is refused too, never read as "no filter" (a full export).
        for value in ("yesterday", ""):
            with pytest.raises(ValueError, match="--changed-since"):
                await BackupExporter(real_adapter).export_to_json(str(output), changed_since=value)
            assert not output.exists()


class TestViewerEndpoint:
    @pytest.fixture
    async def viewer(self, real_adapter):
        """The real app on the seeded archive, auth on, one viewer session per grant."""
        await _seed(real_adapter)
        refs = {
            chat_id: (await real_adapter.get_chat_by_id(chat_id, account_id=1))["ref"] for chat_id in (CHAT, OTHER_CHAT)
        }
        saved = (web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER, dict(web_main._sessions))
        web_main.db = real_adapter
        web_main.AUTH_ENABLED = True
        web_main.ALLOW_ANONYMOUS_VIEWER = False
        web_main._sessions.clear()
        for token, grant, no_download in (
            ("changed-since-all", None, False),
            ("changed-since-other", {refs[OTHER_CHAT]}, False),
            ("changed-since-nodl", None, True),
        ):
            web_main._sessions[token] = web_main.SessionData(
                username=token,
                role="viewer",
                allowed_chat_refs=grant,
                no_download=no_download,
                created_at=time.time(),
            )
        try:
            yield refs
        finally:
            web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER, sessions = saved
            web_main._sessions.clear()
            web_main._sessions.update(sessions)

    @staticmethod
    def _client(token: str) -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")
        client.cookies.set("viewer_auth", token)
        return client

    async def test_changed_since_selects_the_changed_messages(self, viewer):
        async with self._client("changed-since-all") as client:
            # A space, no offset: the same instant as SINCE in UTC.
            resp = await client.get(
                f"/api/chats/{viewer[CHAT]}/export", params={"changed_since": "2026-02-01 10:00:00.25"}
            )
        assert resp.status_code == 200, resp.text
        export = json.loads(resp.text)
        assert sorted(m["id"] for m in export["messages"]) == CHANGED
        assert export["filters"] == {"from": None, "to": None, "changed_since": SINCE.isoformat()}

    async def test_it_combines_with_the_date_window(self, viewer):
        async with self._client("changed-since-all") as client:
            resp = await client.get(
                f"/api/chats/{viewer[CHAT]}/export",
                params={"changed_since": "2026-02-01T10:00:00.25Z", "to": (SENT + timedelta(minutes=4)).isoformat()},
            )
        assert resp.status_code == 200, resp.text
        assert sorted(m["id"] for m in json.loads(resp.text)["messages"]) == [EDITED, DELETED, NEW]

    async def test_a_bad_timestamp_is_a_400(self, viewer):
        async with self._client("changed-since-all") as client:
            for value in ("yesterday", "", "0001-01-01T00:00:00+01:00"):
                resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params={"changed_since": value})
                assert resp.status_code == 400, value

    async def test_the_acl_is_unchanged(self, viewer):
        params = {"changed_since": "2026-02-01T10:00:00Z"}
        async with self._client("changed-since-other") as client:
            denied = await client.get(f"/api/chats/{viewer[CHAT]}/export", params=params)
            allowed = await client.get(f"/api/chats/{viewer[OTHER_CHAT]}/export", params=params)
        assert denied.status_code == 404
        assert allowed.status_code == 200, allowed.text
        assert [m["id"] for m in json.loads(allowed.text)["messages"]] == [1]
        async with self._client("changed-since-nodl") as client:
            resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params=params)
        assert resp.status_code == 403
