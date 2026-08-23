"""The ref-addressed viewer works end to end on the 8.0 multi-account schema.

Phase 4 moved every chat-scoped route onto ``chats.ref``, so this suite drives
the real routes by ref against the real schema: the database is built with
``alembic upgrade head`` from this tree — never ``Base.metadata.create_all`` —
seeded through the ORM models with ``account_id=1`` (the row migration 022
itself seeds, which also mints each chat's ref), and served by the real FastAPI
app over ASGI with a real ``DatabaseAdapter``. Any route that breaks on the 8.0
keys, or any read that starts requiring ``account_id``, surfaces here as a
TypeError-backed 500.

The deployed MCP server (telegram-archive-mcp, a separate repo) fronts these
same HTTP endpoints, so this file is also the MCP-surface baseline: no MCP
module exists under ``src/``.
"""

import hashlib
import inspect
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from alembic import command

if not os.environ.get("BACKUP_PATH"):
    os.environ["BACKUP_PATH"] = tempfile.mkdtemp(prefix="ta_test_v8_viewer_")

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Chat, Media, Message, MessageVersion, Reaction, ViewerAccount
from src.web import main as web_main

REPO_ROOT = Path(__file__).resolve().parent.parent

CHAT_A = -1001000000001
CHAT_B = 77001
BASE_DATE = datetime(2026, 3, 1, 12, 0, 0)

VIEWER_USERNAME = "stage-verify-viewer"
VIEWER_PASSWORD = "viewer-pass@test/value"  # obvious fake
VIEWER_SALT = "stage-verify-salt"

# The 21 OPTIONAL-UNSCOPED reads from the 8.0 contract manifest. Every method
# here must keep ``account_id`` keyword-only WITH default None: the moment one
# grows a required account_id, src/web/ (and the deployed MCP server fronting
# it) breaks without any web code having changed.
OPTIONAL_UNSCOPED_METHODS = (
    "get_all_chats",
    "get_chat_count",
    "get_messages_by_date_range",
    "find_message_by_date",
    "sender_has_message_in_chats",
    "get_message_versions",
    "get_message_versions_by_date_range",
    "iter_message_versions_for_export",
    "get_chat_stats",
    "get_media_paginated",
    "get_media_counts",
    "get_reactions",
    "get_messages_paginated",
    "get_message_dates",
    "find_message_by_date_with_joins",
    "get_chat_by_id",
    "get_pinned_messages",
    "get_messages_for_export",
    "get_forum_topics",
    "get_all_folders",
    "get_archived_chat_count",
)


def _upgrade_to_head(sync_url: str) -> None:
    """Run this tree's real Alembic environment against ``sync_url``."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", sync_url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _seed(sync_url: str) -> None:
    """Two chats, colliding message ids, two media rows, one reaction, one viewer.

    Message id 1..5 exists in BOTH chats on purpose: on the 7.x schema that
    violated the (id, chat_id) key ordering assumptions nowhere, but on 8.0 it
    exercises PK (account_id, chat_id, id) directly.
    """
    engine = sa.create_engine(sync_url)
    try:
        with Session(engine) as session:
            session.add(Chat(account_id=1, id=CHAT_A, type="channel", title="Synthetic Alpha", username="synth_alpha"))
            session.add(
                Chat(account_id=1, id=CHAT_B, type="private", first_name="Beta", last_name="T", username="synth_beta")
            )
            for i in range(1, 31):
                session.add(
                    Message(
                        account_id=1,
                        id=i,
                        chat_id=CHAT_A,
                        date=BASE_DATE + timedelta(minutes=i),
                        text="the needle hides here" if i == 7 else f"alpha message {i}",
                    )
                )
            for i in range(1, 6):
                session.add(
                    Message(
                        account_id=1,
                        id=i,
                        chat_id=CHAT_B,
                        date=BASE_DATE + timedelta(hours=1, minutes=i),
                        text=f"beta message {i}",
                    )
                )
            session.add(
                Media(
                    account_id=1,
                    id=f"{CHAT_A}_30_photo",
                    message_id=30,
                    chat_id=CHAT_A,
                    type="photo",
                    file_path=f"{CHAT_A}/30_photo.jpg",
                    file_name="30_photo.jpg",
                    file_size=3,
                    mime_type="image/jpeg",
                    downloaded=1,
                )
            )
            # Not yet downloaded: the gallery and the counts endpoint must both
            # keep filtering it out on the 8.0 schema.
            session.add(
                Media(
                    account_id=1,
                    id=f"{CHAT_A}_29_document",
                    message_id=29,
                    chat_id=CHAT_A,
                    type="document",
                    file_name="pending.bin",
                    file_size=9,
                    downloaded=0,
                )
            )
            session.add(Reaction(account_id=1, message_id=30, chat_id=CHAT_A, emoji="\U0001f44d", count=2))
            password_hash = hashlib.pbkdf2_hmac("sha256", VIEWER_PASSWORD.encode(), VIEWER_SALT.encode(), 600_000).hex()
            session.add(
                ViewerAccount(
                    username=VIEWER_USERNAME,
                    password_hash=password_hash,
                    salt=VIEWER_SALT,
                    allowed_chat_ids=None,
                    is_active=1,
                )
            )
            session.commit()
    finally:
        engine.dispose()


class TestViewerOn80Schema(unittest.IsolatedAsyncioTestCase):
    """End-to-end viewer checks against an alembic-built 8.0 SQLite database."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="ta_v8_viewer_"))
        cls.addClassCleanup(shutil.rmtree, cls.root, ignore_errors=True)
        cls.db_path = cls.root / "telegram_backup.db"
        cls.media_root = cls.root / "media"
        (cls.media_root / str(CHAT_A)).mkdir(parents=True)
        (cls.media_root / str(CHAT_A) / "30_photo.jpg").write_bytes(b"jpg")

        sync_url = f"sqlite:///{cls.db_path}"
        _upgrade_to_head(sync_url)
        _seed(sync_url)

        # The refs the ORM minted on INSERT: the only chat identity URLs carry.
        engine = sa.create_engine(sync_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(sa.text("SELECT id, ref FROM chats")).fetchall()
        finally:
            engine.dispose()
        refs = dict(rows)
        cls.ref_a = refs[CHAT_A]
        cls.ref_b = refs[CHAT_B]

    async def asyncSetUp(self):
        self.manager = DatabaseManager(f"sqlite+aiosqlite:///{self.db_path}")
        await self.manager.init()
        self.adapter = DatabaseAdapter(self.manager)

        self._saved = {
            "db": web_main.db,
            "auth_enabled": web_main.AUTH_ENABLED,
            "allow_anonymous": web_main.ALLOW_ANONYMOUS_VIEWER,
            "sessions": dict(web_main._sessions),
            "login_attempts": dict(web_main._login_attempts),
            "display_chat_ids": web_main.config.display_chat_ids,
            "media_path": web_main.config.media_path,
            "media_root": web_main._media_root,
            "avatar_cache": dict(web_main._avatar_cache),
            "avatar_cache_time": web_main._avatar_cache_time,
            "chat_stats_cache": dict(web_main._chat_stats_cache),
        }
        web_main.db = self.adapter
        web_main.AUTH_ENABLED = True
        web_main.ALLOW_ANONYMOUS_VIEWER = False
        web_main._sessions.clear()
        web_main._login_attempts.clear()
        web_main.config.display_chat_ids = set()
        web_main.config.media_path = str(self.media_root)
        web_main._media_root = self.media_root.resolve()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._chat_stats_cache.clear()

    async def asyncTearDown(self):
        web_main.db = self._saved["db"]
        web_main.AUTH_ENABLED = self._saved["auth_enabled"]
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved["allow_anonymous"]
        web_main._sessions.clear()
        web_main._sessions.update(self._saved["sessions"])
        web_main._login_attempts.clear()
        web_main._login_attempts.update(self._saved["login_attempts"])
        web_main.config.display_chat_ids = self._saved["display_chat_ids"]
        web_main.config.media_path = self._saved["media_path"]
        web_main._media_root = self._saved["media_root"]
        web_main._avatar_cache.clear()
        web_main._avatar_cache.update(self._saved["avatar_cache"])
        web_main._avatar_cache_time = self._saved["avatar_cache_time"]
        web_main._chat_stats_cache.clear()
        web_main._chat_stats_cache.update(self._saved["chat_stats_cache"])
        await self.manager.close()

    def _client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def _login(self, client: AsyncClient) -> str:
        resp = await client.post("/api/login", json={"username": VIEWER_USERNAME, "password": VIEWER_PASSWORD})
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.cookies.get("viewer_auth")
        self.assertTrue(token)
        return token

    def test_schema_came_from_alembic_and_seeded_the_default_account(self):
        """The fixture DB is migration-built: version stamped, account (1, 'default') present, refs minted."""
        engine = sa.create_engine(f"sqlite:///{self.db_path}")
        try:
            with engine.connect() as conn:
                version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
                accounts = conn.execute(sa.text("SELECT id, label FROM accounts")).fetchall()
                refs = conn.execute(sa.text("SELECT ref FROM chats")).scalars().all()
        finally:
            engine.dispose()
        self.assertIsNotNone(version)
        self.assertEqual(accounts, [(1, "default")])
        # The ORM minted a distinct opaque ref per chat on INSERT (phase 4 currency).
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(set(refs)), 2)
        self.assertTrue(all(ref for ref in refs))

    async def test_login_persists_a_session_and_auth_check_accepts_it(self):
        async with self._client() as client:
            token = await self._login(client)

            row = await self.adapter.get_session(token)
            self.assertIsNotNone(row)
            self.assertEqual(row["username"], VIEWER_USERNAME)
            self.assertEqual(row["role"], "viewer")

            resp = await client.get("/api/auth/check")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertTrue(data["authenticated"])
            self.assertEqual(data["username"], VIEWER_USERNAME)

    async def test_chat_list_returns_both_chats(self):
        async with self._client() as client:
            await self._login(client)
            resp = await client.get("/api/chats")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(data["total"], 2)
            by_id = {chat["id"]: chat for chat in data["chats"]}
            self.assertEqual(set(by_id), {CHAT_A, CHAT_B})
            self.assertEqual(by_id[CHAT_A]["title"], "Synthetic Alpha")
            self.assertEqual(by_id[CHAT_B]["username"], "synth_beta")
            # Phase 4 payload additions: the ref the frontend must address
            # chats by, and the account id carried for phase 5.
            self.assertEqual(by_id[CHAT_A]["ref"], self.ref_a)
            self.assertEqual(by_id[CHAT_B]["ref"], self.ref_b)
            self.assertEqual(by_id[CHAT_A]["account_id"], 1)
            self.assertFalse(data["has_more"])

    async def test_message_pagination_offset_and_cursor_agree(self):
        async with self._client() as client:
            await self._login(client)

            resp = await client.get(f"/api/chats/{self.ref_a}/messages", params={"limit": 10})
            self.assertEqual(resp.status_code, 200, resp.text)
            page1 = resp.json()
            self.assertEqual([m["id"] for m in page1], list(range(30, 20, -1)))

            resp = await client.get(f"/api/chats/{self.ref_a}/messages", params={"limit": 10, "offset": 10})
            self.assertEqual(resp.status_code, 200, resp.text)
            page2 = resp.json()
            self.assertEqual([m["id"] for m in page2], list(range(20, 10, -1)))

            # Cursor mode (the infinite-scroll path) must land on the same rows.
            oldest = page1[-1]
            resp = await client.get(
                f"/api/chats/{self.ref_a}/messages",
                params={"limit": 10, "before_date": oldest["date"], "before_id": oldest["id"]},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual([m["id"] for m in resp.json()], [m["id"] for m in page2])

    async def test_message_search_returns_only_the_matching_row(self):
        async with self._client() as client:
            await self._login(client)
            resp = await client.get(f"/api/chats/{self.ref_a}/messages", params={"search": "needle"})
            self.assertEqual(resp.status_code, 200, resp.text)
            hits = resp.json()
            self.assertEqual([m["id"] for m in hits], [7])
            self.assertIn("needle", hits[0]["text"])

    async def test_chat_search_matches_username(self):
        async with self._client() as client:
            await self._login(client)
            resp = await client.get("/api/chats", params={"search": "synth_beta"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual([chat["id"] for chat in resp.json()["chats"]], [CHAT_B])

    async def test_media_listing_resolves_and_original_bytes_are_served(self):
        async with self._client() as client:
            await self._login(client)

            resp = await client.get(f"/api/chats/{self.ref_a}/media")
            self.assertEqual(resp.status_code, 200, resp.text)
            items = resp.json()["items"]
            # Exactly one: the seeded downloaded=0 document must stay filtered out.
            self.assertEqual(len(items), 1)
            item = items[0]
            # The gallery id is the CHAT-FREE key (it round-trips as the
            # cursor), and every URL is ref-addressed — no chat id in either.
            self.assertEqual(item["id"], "30_photo")
            self.assertEqual(item["media_url"], f"/media/{self.ref_a}/30_photo")
            self.assertEqual(item["thumb_url"], f"/media/thumb/200/{self.ref_a}/30_photo")

            resp = await client.get(item["media_url"])
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.content, b"jpg")

            # The message payload nests the same media row and its reaction —
            # the joins that now carry Media.account_id == Message.account_id.
            resp = await client.get(f"/api/chats/{self.ref_a}/messages", params={"limit": 1})
            self.assertEqual(resp.status_code, 200, resp.text)
            top = resp.json()[0]
            self.assertEqual(top["id"], 30)
            self.assertEqual(top["media"]["id"], "30_photo")
            self.assertEqual(top["media"]["url"], f"/media/{self.ref_a}/30_photo")
            self.assertEqual(top["reactions"], [{"emoji": "\U0001f44d", "count": 2, "user_ids": []}])

    def test_every_viewer_read_keeps_account_id_optional(self):
        """The OPTIONAL-UNSCOPED contract, pinned at the signature level.

        The route sweeps below only prove the paths they drive; this proves the
        whole classified surface, including reads the CLI export uses.
        """
        for name in OPTIONAL_UNSCOPED_METHODS:
            with self.subTest(method=name):
                params = inspect.signature(getattr(DatabaseAdapter, name)).parameters
                self.assertIn("account_id", params)
                param = params["account_id"]
                self.assertIs(param.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIsNone(param.default)

    async def test_remaining_read_routes_answer_200_with_correct_aggregates(self):
        """The read surface beyond the five headline paths: no hidden 500s.

        Every handler here wraps adapter calls in an except-all that turns a
        TypeError into a 500, so status 200 is exactly the no-missing-kwarg
        check.
        """
        async with self._client() as client:
            await self._login(client)

            resp = await client.get("/api/stats")
            self.assertEqual(resp.status_code, 200, resp.text)

            resp = await client.get("/api/folders")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["folders"], [])

            resp = await client.get(f"/api/chats/{self.ref_a}/topics")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["topics"], [])

            resp = await client.get(f"/api/chats/{self.ref_a}/pinned")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), [])

            resp = await client.get("/api/archived/count")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["count"], 0)

            resp = await client.get(f"/api/chats/{self.ref_a}/media/counts")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), {"photo": 1})

            resp = await client.get(f"/api/chats/{self.ref_a}/messages/30/versions")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), [])

            resp = await client.get(f"/api/chats/{self.ref_a}/stats")
            self.assertEqual(resp.status_code, 200, resp.text)
            stats = resp.json()
            self.assertEqual(stats["messages"], 30)
            # Chat stats count media RECORDS (pending included) — unlike the
            # gallery and /media/counts, which filter to downloaded=1.
            self.assertEqual(stats["media_files"], 2)

    async def test_date_navigation_and_export_stream(self):
        async with self._client() as client:
            await self._login(client)

            resp = await client.get(
                f"/api/chats/{self.ref_a}/messages/by-date",
                params={"date": "2026-03-01", "timezone": "UTC"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["id"], 1)

            resp = await client.get(
                f"/api/chats/{self.ref_a}/messages/dates",
                params={"month": "2026-03", "timezone": "UTC"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["dates"], ["2026-03-01"])

            resp = await client.get(f"/api/chats/{self.ref_a}/export")
            self.assertEqual(resp.status_code, 200, resp.text)
            export = json.loads(resp.text)
            # The export FILE keeps the chat id (the user's own data leaving
            # the system, not a URL) and gains the ref for correlation.
            self.assertEqual(export["chat"]["id"], CHAT_A)
            self.assertEqual(export["chat"]["ref"], self.ref_a)
            self.assertEqual(len(export["messages"]), 30)
            self.assertEqual(export["message_versions"], [])
            # An unwindowed export carries no filters block: the file IS the
            # full history and must not suggest otherwise.
            self.assertNotIn("filters", export)

    async def test_export_date_window(self):
        """?from/?to reach the export stream (the README advertised this).

        Bounds follow the #365 conversion contract: tz-aware instants convert
        to naive UTC (never relabel), a bare "to" date includes its whole day,
        a full "to" timestamp is exclusive.
        """
        async with self._client() as client:
            await self._login(client)

            resp = await client.get(f"/api/chats/{self.ref_a}/export", params={"from": "2026-03-01T12:11:00"})
            self.assertEqual(resp.status_code, 200, resp.text)
            export = json.loads(resp.text)
            self.assertEqual(len(export["messages"]), 20)  # minutes 11..30
            self.assertEqual(export["filters"], {"from": "2026-03-01T12:11:00", "to": None})

            # +02:00 is the same instant as 12:11 UTC — the window must not shift.
            resp = await client.get(f"/api/chats/{self.ref_a}/export", params={"from": "2026-03-01T14:11:00+02:00"})
            self.assertEqual(len(json.loads(resp.text)["messages"]), 20)

            # Full-timestamp "to" is exclusive: < 12:10 keeps minutes 1..9.
            resp = await client.get(f"/api/chats/{self.ref_a}/export", params={"to": "2026-03-01T12:10:00"})
            self.assertEqual(len(json.loads(resp.text)["messages"]), 9)

            # A bare "to" date includes the whole named day.
            resp = await client.get(f"/api/chats/{self.ref_a}/export", params={"to": "2026-03-01"})
            self.assertEqual(len(json.loads(resp.text)["messages"]), 30)

            # The maximum ISO date means "no upper bound", not a 500.
            resp = await client.get(f"/api/chats/{self.ref_a}/export", params={"to": "9999-12-31"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(len(json.loads(resp.text)["messages"]), 30)

            resp = await client.get(f"/api/chats/{self.ref_a}/export", params={"from": "not-a-date"})
            self.assertEqual(resp.status_code, 400)

            resp = await client.get(
                f"/api/chats/{self.ref_a}/export", params={"from": "2026-03-02", "to": "2026-03-01"}
            )

    async def test_changes_feed(self):
        """/api/changes lists captured deletions and edits, newest first,
        under the viewer's compiled scope."""
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import update as sa_update

        async with self.manager.async_session_factory() as session:
            await session.execute(
                sa_update(Message)
                .where(Message.account_id == 1, Message.chat_id == CHAT_A, Message.id == 1)
                .values(is_deleted=1, deleted_at=BASE_DATE + timedelta(days=1))
            )
            session.add(
                MessageVersion(
                    account_id=1,
                    chat_id=CHAT_A,
                    message_id=2,
                    text="the old text",
                    date=BASE_DATE,
                    change_hash="feedtesthash0001",
                    captured_at=BASE_DATE + timedelta(days=1, hours=1),
                )
            )
            await session.commit()

        # The seed is class-scoped: undo these mutations whatever happens, or
        # alphabetically-later tests inherit a tombstoned message 1.
        async def _restore():
            async with self.manager.async_session_factory() as session:
                await session.execute(
                    sa_update(Message)
                    .where(Message.account_id == 1, Message.chat_id == CHAT_A, Message.id == 1)
                    .values(is_deleted=0, deleted_at=None)
                )
                await session.execute(sa_delete(MessageVersion).where(MessageVersion.change_hash == "feedtesthash0001"))
                await session.commit()

        try:
            await self._run_changes_feed_assertions()
        finally:
            await _restore()

    async def _run_changes_feed_assertions(self):
        async with self._client() as client:
            await self._login(client)

            resp = await client.get("/api/changes")
            self.assertEqual(resp.status_code, 200, resp.text)
            payload = resp.json()
            kinds = [c["kind"] for c in payload["changes"]]
            self.assertEqual(kinds, ["edited", "deleted"])  # captured_at is newer
            edited, deleted = payload["changes"]
            self.assertEqual(edited["old_text"], "the old text")
            self.assertEqual(edited["chat"]["ref"], self.ref_a)
            self.assertEqual(deleted["message_id"], 1)
            self.assertIsNone(payload["next_before"])  # short page, no cursor

            # since after both excludes everything.
            resp = await client.get("/api/changes", params={"since": "2026-03-03T00:00:00Z"})
            self.assertEqual(resp.json()["changes"], [])

            # Cursor pages strictly older than the given instant.
            resp = await client.get("/api/changes", params={"before": edited["date"]})
            self.assertEqual([c["kind"] for c in resp.json()["changes"]], ["deleted"])

            resp = await client.get("/api/changes", params={"since": "not-a-date"})
            self.assertEqual(resp.status_code, 400)
