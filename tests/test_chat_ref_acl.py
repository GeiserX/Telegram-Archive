"""The phase-4 fail-closed entitlement matrix, on a real migrated 8.0 database.

Every chat-scoped viewer route resolves its ``{chat_ref}`` through ONE shared
dependency, and that dependency is the whole access-control story — so this
file pins its behaviour end to end, on a database built by ``alembic upgrade
head`` (never ``create_all``) and driven through the real FastAPI app, on both
supported backends:

- NULL grants see everything; a set grant sees exactly the set; the EMPTY set
  denies everything.
- A grant that cannot be read (bad JSON, wrong shape, wrong element type)
  parses to the empty set: authentication still succeeds, the session just
  sees nothing — fail-closed without a login oracle.
- The legacy 7.x ``allowed_chat_ids`` column is NEVER read as a grant. A row
  where it is populated while both 8.0 columns are NULL is an unconverted
  restriction and gets the empty grant; a populated legacy column can never
  override an 8.0 deny.
- Unknown, malformed and forbidden refs answer with the SAME status and body:
  nothing distinguishes "exists but not yours" from "does not exist".
- Media bytes ride the same resolver; a forbidden ref is indistinguishable
  from a nonexistent one there too, and cursors/ids are chat-free (#266).
- The WebSocket subscribe path uses the same resolver, and delivery re-checks
  the grant per socket — parity with HTTP is a hard requirement.
- No stored audit row, no server log line, and no URL the server mints or
  serves carries a chat id; the ref is the only chat identity that travels.

The chat ids seeded here (900777001 / 900777002) are deliberately distinctive
so the "never appears" assertions are a grep, not a guess.
"""

import json
import logging
import os
import secrets
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from conftest import NO_POSTGRES_REASON
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from alembic import command

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_chat_ref_"))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Chat, Media, Message
from src.web import main as web_main

REPO_ROOT = Path(__file__).resolve().parent.parent

# Distinctive on purpose: the PII assertions grep every stored/logged string
# for these digits, so they must be unmistakable for anything else.
CHAT_A_ID = 900777001
CHAT_B_ID = 900777002
SENDER_ID = 900777801

MASTER_USERNAME = "matrix-master"
MASTER_PASSWORD = "master-pass@test/value"  # obvious fake
VIEWER_PASSWORD = "matrix-pass@test/value"  # obvious fake

UNIFORM_404 = {"detail": "Chat not found"}


def _upgrade_to_head(url: str) -> None:
    """Run this tree's real Alembic environment against ``url`` (sync or async)."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _seed(sync_url: str, media_root: Path) -> dict[int, str]:
    """Two chats under account 1 (the row migration 022 seeds), messages, media.

    Returns {chat_id: ref} — the refs the ORM minted on INSERT, which are the
    only chat identity the tests put in URLs from here on.
    """
    (media_root / str(CHAT_A_ID)).mkdir(parents=True, exist_ok=True)
    (media_root / str(CHAT_A_ID) / "2_photo.jpg").write_bytes(b"ref-jpg")
    (media_root / str(CHAT_A_ID) / "3_clip.mp4").write_bytes(b"ref-mp4")

    engine = sa.create_engine(sync_url)
    try:
        with Session(engine) as session:
            session.add(Chat(account_id=1, id=CHAT_A_ID, type="private", first_name="Alpha", username="matrix_alpha"))
            session.add(Chat(account_id=1, id=CHAT_B_ID, type="private", first_name="Beta", username="matrix_beta"))
            start = datetime(2026, 4, 1, 9, 0, 0)
            session.add(Message(account_id=1, id=1, chat_id=CHAT_A_ID, date=start, text="alpha hello"))
            session.add(
                Message(
                    account_id=1,
                    id=2,
                    chat_id=CHAT_A_ID,
                    date=start + timedelta(minutes=1),
                    text="alpha photo",
                    sender_id=SENDER_ID,
                )
            )
            session.add(
                Message(account_id=1, id=3, chat_id=CHAT_A_ID, date=start + timedelta(minutes=2), text="alpha clip")
            )
            session.add(Message(account_id=1, id=1, chat_id=CHAT_B_ID, date=start + timedelta(hours=1), text="beta"))
            session.add(
                Media(
                    account_id=1,
                    id=f"{CHAT_A_ID}_2_photo",
                    message_id=2,
                    chat_id=CHAT_A_ID,
                    type="photo",
                    file_path=f"{CHAT_A_ID}/2_photo.jpg",
                    file_name="2_photo.jpg",
                    file_size=7,
                    mime_type="image/jpeg",
                    downloaded=1,
                )
            )
            session.add(
                Media(
                    account_id=1,
                    id=f"{CHAT_A_ID}_3_video",
                    message_id=3,
                    chat_id=CHAT_A_ID,
                    type="video",
                    file_path=f"{CHAT_A_ID}/3_clip.mp4",
                    file_name="3_clip.mp4",
                    file_size=7,
                    mime_type="video/mp4",
                    downloaded=1,
                )
            )
            session.commit()
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT id, ref FROM chats")).fetchall()
    finally:
        engine.dispose()
    return dict(rows)


@pytest.fixture(scope="module", params=("sqlite", "postgresql"))
def ref_archive(request, tmp_path_factory, postgres_server_url, make_postgres_database):
    """A migration-built, seeded 8.0 database per backend, plus its media root."""
    media_root = tmp_path_factory.mktemp(f"chatref-media-{request.param}").resolve()
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        async_url, sync_url = make_postgres_database("telegram_archive_pytest_chatref")
    else:
        db_path = tmp_path_factory.mktemp("chatref-db") / "archive.db"
        sync_url = f"sqlite:///{db_path}"
        async_url = f"sqlite+aiosqlite:///{db_path}"
    # alembic/env.py builds an ASYNC engine from the URL it is given, so the
    # upgrade takes the async URL; seeding uses a plain sync engine.
    _upgrade_to_head(async_url)
    refs = _seed(sync_url, media_root)
    return SimpleNamespace(
        backend=request.param,
        async_url=async_url,
        media_root=media_root,
        ref_a=refs[CHAT_A_ID],
        ref_b=refs[CHAT_B_ID],
    )


def _null_pool_manager(async_url: str) -> DatabaseManager:
    """A DatabaseManager whose engine never pools connections.

    The WebSocket tests drive the app through Starlette's TestClient, whose
    portal runs a DIFFERENT event loop from pytest's. A pooled asyncpg
    connection binds to the loop it was created on, so pooling across the two
    breaks; NullPool gives every operation a fresh connection on whatever loop
    is current, which also makes disposal loop-safe. ``hide_parameters`` stays
    on exactly as in production (the PII rule for engine error text).
    """
    manager = DatabaseManager(async_url)
    manager.engine = create_async_engine(manager.database_url, poolclass=NullPool, hide_parameters=True)
    manager.async_session_factory = async_sessionmaker(manager.engine, class_=AsyncSession, expire_on_commit=False)
    return manager


@pytest.fixture
async def viewer_app(ref_archive):
    """The real app wired to the migrated database, auth on, state restored after."""
    manager = _null_pool_manager(ref_archive.async_url)
    adapter = DatabaseAdapter(manager)

    saved = {
        "db": web_main.db,
        "auth_enabled": web_main.AUTH_ENABLED,
        "allow_anonymous": web_main.ALLOW_ANONYMOUS_VIEWER,
        "viewer_username": web_main.VIEWER_USERNAME,
        "viewer_password": web_main.VIEWER_PASSWORD,
        "sessions": dict(web_main._sessions),
        "login_attempts": dict(web_main._login_attempts),
        "display_chat_ids": web_main.config.display_chat_ids,
        "media_path": web_main.config.media_path,
        "media_root": web_main._media_root,
        "avatar_cache": dict(web_main._avatar_cache),
        "avatar_cache_time": web_main._avatar_cache_time,
        "chat_stats_cache": dict(web_main._chat_stats_cache),
        "broadcast_chat_cache": dict(web_main._broadcast_chat_cache),
        "sender_lookup_cache": dict(web_main._sender_lookup_cache),
    }
    web_main.db = adapter
    web_main.AUTH_ENABLED = True
    web_main.ALLOW_ANONYMOUS_VIEWER = False
    web_main.VIEWER_USERNAME = MASTER_USERNAME
    web_main.VIEWER_PASSWORD = MASTER_PASSWORD
    web_main._sessions.clear()
    web_main._login_attempts.clear()
    web_main.config.display_chat_ids = set()
    web_main.config.media_path = str(ref_archive.media_root)
    web_main._media_root = ref_archive.media_root
    web_main._avatar_cache.clear()
    web_main._avatar_cache_time = None
    web_main._chat_stats_cache.clear()
    web_main._broadcast_chat_cache.clear()
    web_main._sender_lookup_cache.clear()

    try:
        yield SimpleNamespace(adapter=adapter, archive=ref_archive)
    finally:
        web_main.db = saved["db"]
        web_main.AUTH_ENABLED = saved["auth_enabled"]
        web_main.ALLOW_ANONYMOUS_VIEWER = saved["allow_anonymous"]
        web_main.VIEWER_USERNAME = saved["viewer_username"]
        web_main.VIEWER_PASSWORD = saved["viewer_password"]
        web_main._sessions.clear()
        web_main._sessions.update(saved["sessions"])
        web_main._login_attempts.clear()
        web_main._login_attempts.update(saved["login_attempts"])
        web_main.config.display_chat_ids = saved["display_chat_ids"]
        web_main.config.media_path = saved["media_path"]
        web_main._media_root = saved["media_root"]
        web_main._avatar_cache.clear()
        web_main._avatar_cache.update(saved["avatar_cache"])
        web_main._avatar_cache_time = saved["avatar_cache_time"]
        web_main._chat_stats_cache.clear()
        web_main._chat_stats_cache.update(saved["chat_stats_cache"])
        web_main._broadcast_chat_cache.clear()
        web_main._broadcast_chat_cache.update(saved["broadcast_chat_cache"])
        web_main._sender_lookup_cache.clear()
        web_main._sender_lookup_cache.update(saved["sender_lookup_cache"])
        await manager.close()


def _client(app=None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app or web_main.app), base_url="http://test")


async def _login_viewer(
    client: AsyncClient,
    adapter: DatabaseAdapter,
    *,
    allowed_chat_ids: str | None = None,
    allowed_accounts: str | None = None,
    allowed_chat_refs: str | None = None,
) -> str:
    """Create a viewer account with EXACTLY these stored grant columns, log in.

    The grants deliberately go through the database and the real login flow, so
    what is enforced is what a real deployment enforces — a row shaped by 8.0
    writers, by migration 022, or by a bypassed migration (the legacy cases).
    """
    username = f"matrix-viewer-{secrets.token_hex(4)}"
    salt = secrets.token_hex(8)
    password_hash = web_main._hash_password(VIEWER_PASSWORD, salt)
    await adapter.create_viewer_account(
        username=username,
        password_hash=password_hash,
        salt=salt,
        allowed_chat_ids=allowed_chat_ids,
        allowed_accounts=allowed_accounts,
        allowed_chat_refs=allowed_chat_refs,
        created_by="test",
        is_active=1,
    )
    resp = await client.post("/api/login", json={"username": username, "password": VIEWER_PASSWORD})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "viewer"
    return username


async def _visible_chat_ids(client: AsyncClient) -> list[int]:
    resp = await client.get("/api/chats")
    assert resp.status_code == 200, resp.text
    return sorted(chat["id"] for chat in resp.json()["chats"])


def _fresh_unknown_ref() -> str:
    """Well-formed (22-char urlsafe) but guaranteed absent from the database."""
    return secrets.token_urlsafe(16)


# ============================================================================
# (a) NULL grants: unrestricted
# ============================================================================


async def test_null_grants_see_every_chat(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter)
        assert await _visible_chat_ids(client) == [CHAT_A_ID, CHAT_B_ID]
        for ref in (archive.ref_a, archive.ref_b):
            resp = await client.get(f"/api/chats/{ref}/messages")
            assert resp.status_code == 200, resp.text
        # The chat payload carries ref + account_id, and its avatar URL — the
        # only chat identity handed to the frontend for URLs is the ref.
        chats = (await client.get("/api/chats")).json()["chats"]
        by_id = {chat["id"]: chat for chat in chats}
        assert by_id[CHAT_A_ID]["ref"] == archive.ref_a
        assert by_id[CHAT_A_ID]["account_id"] == 1


# ============================================================================
# (b) A ref grant, and the no-oracle rule
# ============================================================================


async def test_ref_grant_sees_only_that_ref_and_denials_are_uniform(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs=json.dumps([archive.ref_a]))
        assert await _visible_chat_ids(client) == [CHAT_A_ID]

        allowed = await client.get(f"/api/chats/{archive.ref_a}/messages")
        assert allowed.status_code == 200, allowed.text
        assert [message["id"] for message in allowed.json()] == [3, 2, 1]

        # Forbidden-but-existing, well-formed-unknown, and garbage must be
        # indistinguishable: same status, same body, for every candidate.
        outcomes = set()
        for candidate in (archive.ref_b, _fresh_unknown_ref(), "AAAA", "!" * 22, "0"):
            resp = await client.get(f"/api/chats/{candidate}/messages")
            outcomes.add((resp.status_code, resp.text))
        assert len(outcomes) == 1, outcomes
        status, body = outcomes.pop()
        assert status == 404
        assert json.loads(body) == UNIFORM_404


# ============================================================================
# (c) An account grant that matches no chats
# ============================================================================


async def test_account_grant_for_an_absent_account_sees_nothing(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_accounts=json.dumps([2]))
        assert await _visible_chat_ids(client) == []
        for ref in (archive.ref_a, archive.ref_b):
            resp = await client.get(f"/api/chats/{ref}/messages")
            assert (resp.status_code, resp.json()) == (404, UNIFORM_404)


# ============================================================================
# (d) Unparseable grants deny — after a SUCCESSFUL login
# ============================================================================


@pytest.mark.parametrize(
    "grant_columns",
    [
        {"allowed_chat_refs": "{not json"},
        {"allowed_chat_refs": '"a-string-not-a-list"'},
        {"allowed_chat_refs": "[1, 2]"},  # wrong element type: refs are strings
        {"allowed_accounts": "{not json"},
        {"allowed_accounts": '["one"]'},  # wrong element type: accounts are ints
        {"allowed_accounts": "[true]"},  # bool is NOT an account id
    ],
)
async def test_unparseable_grant_authenticates_into_the_empty_grant(viewer_app, grant_columns):
    archive = viewer_app.archive
    async with _client() as client:
        # Login must SUCCEED (a corrupt grant is not a login oracle)…
        await _login_viewer(client, viewer_app.adapter, **grant_columns)
        # …into a session that sees nothing at all.
        assert await _visible_chat_ids(client) == []
        resp = await client.get(f"/api/chats/{archive.ref_a}/messages")
        assert (resp.status_code, resp.json()) == (404, UNIFORM_404)


# ============================================================================
# (e) The EMPTY grant denies everything
# ============================================================================


async def test_empty_list_grant_denies_everything(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs="[]")
        assert await _visible_chat_ids(client) == []
        resp = await client.get(f"/api/chats/{archive.ref_a}/messages")
        assert (resp.status_code, resp.json()) == (404, UNIFORM_404)


# ============================================================================
# (e2) Paging and `total` describe the VISIBLE list, not the archive
# ============================================================================


async def test_restricted_paging_and_total_describe_only_visible_chats(viewer_app):
    """A restricted viewer pages the same way an unrestricted one does.

    The grant is applied in SQL now, so limit/offset/COUNT all run against the
    visible row set. Three things have to hold together, and each of them was a
    way the old load-everything-then-slice code could have gone wrong:

    * ``total`` counts VISIBLE chats — leaking the archive's chat count would
      tell a one-chat viewer how many chats exist.
    * paging is stable: walking the list one page at a time yields every
      visible chat exactly once, in the same order as one unpaged request.
    * the order is the unrestricted order with the invisible rows removed, so a
      restricted viewer sees the same "most recent first" list everyone else
      does.
    """
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs=json.dumps([archive.ref_a, archive.ref_b]))
        whole = (await client.get("/api/chats")).json()
        assert whole["total"] == 2
        assert whole["has_more"] is False
        order = [chat["ref"] for chat in whole["chats"]]

        paged = []
        for offset in (0, 1):
            page = (await client.get(f"/api/chats?limit=1&offset={offset}")).json()
            assert page["total"] == 2
            assert page["limit"] == 1
            assert page["offset"] == offset
            assert page["has_more"] is (offset == 0)
            paged += [chat["ref"] for chat in page["chats"]]
        assert paged == order

    # The same list, seen by a principal with no grant at all: the restricted
    # order must be that order, not a differently-sorted subset.
    async with _client() as client:
        resp = await client.post("/api/login", json={"username": MASTER_USERNAME, "password": MASTER_PASSWORD})
        assert resp.status_code == 200, resp.text
        unrestricted = (await client.get("/api/chats")).json()
        assert unrestricted["total"] == 2
        assert [chat["ref"] for chat in unrestricted["chats"]] == order


async def test_total_for_a_one_chat_grant_never_reveals_the_archive_size(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs=json.dumps([archive.ref_a]))
        body = (await client.get("/api/chats")).json()
        assert body["total"] == 1
        assert [chat["ref"] for chat in body["chats"]] == [archive.ref_a]
        assert body["has_more"] is False


async def test_empty_grant_totals_zero_rather_than_everything(viewer_app):
    """The bypass shape: an empty grant must not degrade to "no filter"."""
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs="[]")
        body = (await client.get("/api/chats")).json()
        assert body == {"chats": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
        assert (await client.get("/api/archived/count")).json() == {"count": 0}


# ============================================================================
# (f) The legacy column is rollback data, never a grant
# ============================================================================


async def test_legacy_column_alone_is_an_unconverted_restriction_and_denies(viewer_app):
    """Both 8.0 columns NULL + a populated legacy grant = migration bypassed.

    Under 7.x this row could read chat A; reading it that way today would be
    the fail-open this design exists to prevent. It must see NOTHING.
    """
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_ids=json.dumps([CHAT_A_ID, CHAT_B_ID]))
        assert await _visible_chat_ids(client) == []
        for candidate in (archive.ref_a, archive.ref_b):
            resp = await client.get(f"/api/chats/{candidate}/messages")
            assert (resp.status_code, resp.json()) == (404, UNIFORM_404)


async def test_legacy_column_cannot_override_an_explicit_deny(viewer_app):
    """New columns say deny (empty), legacy says allow-everything: still deny."""
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(
            client,
            viewer_app.adapter,
            allowed_chat_ids=json.dumps([CHAT_A_ID, CHAT_B_ID]),
            allowed_chat_refs="[]",
        )
        assert await _visible_chat_ids(client) == []
        resp = await client.get(f"/api/chats/{archive.ref_a}/messages")
        assert (resp.status_code, resp.json()) == (404, UNIFORM_404)


# ============================================================================
# (g) Media through a forbidden ref is indistinguishable from nonexistent
# ============================================================================


async def test_media_via_forbidden_ref_is_indistinguishable(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        # Granted chat B only; the media lives in chat A.
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs=json.dumps([archive.ref_b]))
        outcomes = set()
        for candidate in (archive.ref_a, _fresh_unknown_ref()):
            resp = await client.get(f"/media/{candidate}/2_photo")
            outcomes.add((resp.status_code, resp.text))
            thumb = await client.get(f"/media/thumb/200/{candidate}/2_photo")
            outcomes.add((thumb.status_code, thumb.text))
        assert len(outcomes) == 1, outcomes
        status, body = outcomes.pop()
        assert status == 404
        assert json.loads(body) == UNIFORM_404

    async with _client() as control:
        # Control: the same URL with the grant serves the actual bytes — the
        # 404s above are entitlement, not brokenness.
        await _login_viewer(control, viewer_app.adapter, allowed_chat_refs=json.dumps([archive.ref_a]))
        resp = await control.get(f"/media/{archive.ref_a}/2_photo")
        assert resp.status_code == 200
        assert resp.content == b"ref-jpg"
        assert resp.headers["cache-control"] == "private"


async def test_media_ids_and_cursors_are_chat_free(viewer_app):
    """Gallery ids round-trip as cursors; a 7.x full-composite token yields an
    empty page (#266 semantics) instead of resolving anything."""
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter, allowed_chat_refs=json.dumps([archive.ref_a]))
        page = await client.get(f"/api/chats/{archive.ref_a}/media")
        assert page.status_code == 200, page.text
        items = page.json()["items"]
        assert [item["id"] for item in items] == ["3_video", "2_photo"]
        assert items[1]["media_url"] == f"/media/{archive.ref_a}/2_photo"

        older = await client.get(f"/api/chats/{archive.ref_a}/media", params={"before_id": "3_video"})
        assert [item["id"] for item in older.json()["items"]] == ["2_photo"]

        legacy = await client.get(f"/api/chats/{archive.ref_a}/media", params={"before_id": f"{CHAT_A_ID}_3_video"})
        assert legacy.status_code == 200
        assert legacy.json()["items"] == []


# ============================================================================
# DISPLAY_CHAT_IDS binds every role, master included
# ============================================================================


async def test_display_chat_ids_binds_master_too(viewer_app):
    archive = viewer_app.archive
    web_main.config.display_chat_ids = {CHAT_B_ID}
    async with _client() as client:
        resp = await client.post("/api/login", json={"username": MASTER_USERNAME, "password": MASTER_PASSWORD})
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "master"
        assert await _visible_chat_ids(client) == [CHAT_B_ID]
        hidden = await client.get(f"/api/chats/{archive.ref_a}/messages")
        assert (hidden.status_code, hidden.json()) == (404, UNIFORM_404)
        shown = await client.get(f"/api/chats/{archive.ref_b}/messages")
        assert shown.status_code == 200, shown.text


# ============================================================================
# Database down: 503, with no per-chat signal
# ============================================================================


async def test_db_down_answers_503_identically_for_any_ref(viewer_app):
    archive = viewer_app.archive
    async with _client() as client:
        await _login_viewer(client, viewer_app.adapter)
        web_main.db = None
        try:
            outcomes = set()
            for candidate in (archive.ref_a, _fresh_unknown_ref()):
                resp = await client.get(f"/api/chats/{candidate}/messages")
                outcomes.add((resp.status_code, resp.text))
        finally:
            web_main.db = viewer_app.adapter
        assert len(outcomes) == 1, outcomes
        assert outcomes.pop()[0] == 503


# ============================================================================
# (h) WebSocket parity: same resolver on subscribe, re-check at delivery
# ============================================================================


async def test_websocket_restricted_socket_sees_only_entitled_events(viewer_app):
    """Subscribe parity AND delivery parity over a real socket.

    The subscribe gate rides the same resolver as HTTP (uniform denial for
    forbidden/unknown/malformed/DB-down), and delivery re-checks the socket's
    grant — a subscription that somehow outlives its grant still delivers
    nothing (a prior audit found exactly this gap once).

    TestClient runs the app on its own portal loop; the fixture's NullPool
    engine is what makes the database usable from there and from pytest's
    loop in the same test.
    """
    archive = viewer_app.archive
    token = f"ws-{secrets.token_hex(8)}"
    web_main._sessions[token] = web_main.SessionData(
        username="ws-viewer", role="viewer", allowed_chat_refs={archive.ref_a}, created_at=time.time()
    )
    client = TestClient(web_main.app, raise_server_exceptions=False)
    client.cookies.set("viewer_auth", token)

    with client.websocket_connect("/ws/updates") as socket:
        socket.send_json({"action": "subscribe", "chat_ref": archive.ref_a})
        assert socket.receive_json() == {"type": "subscribed", "chat_ref": archive.ref_a}

        # Forbidden-but-existing, well-formed-unknown, malformed: one denial.
        for candidate in (archive.ref_b, _fresh_unknown_ref(), "AAAA"):
            socket.send_json({"action": "subscribe", "chat_ref": candidate})
            assert socket.receive_json() == {"type": "subscribe_denied", "chat_ref": candidate}

        # DB down: the same uniform denial, even for the entitled ref.
        web_main.db = None
        try:
            socket.send_json({"action": "subscribe", "chat_ref": archive.ref_a})
            assert socket.receive_json() == {"type": "subscribe_denied", "chat_ref": archive.ref_a}
        finally:
            web_main.db = viewer_app.adapter

        # Delivery re-check: force a subscription the grant does not cover
        # (a stale subscription from before a narrowing) …
        # Exactly one live socket by construction; unpacking fails loudly otherwise.
        [server_socket] = list(web_main.ws_manager.active_connections)
        web_main.ws_manager.active_connections[server_socket].add(archive.ref_b)

        # … broadcast for the forbidden chat FIRST, then the entitled one.
        socket.portal.call(web_main.broadcast_new_message, CHAT_B_ID, {"id": 9, "text": "beta-secret"})
        socket.portal.call(web_main.broadcast_new_message, CHAT_A_ID, {"id": 4, "text": "alpha-public"})

        # The first (and only) frame is the entitled chat's, ref-addressed.
        frame = socket.receive_json()
        assert frame["type"] == "new_message"
        assert frame["chat_ref"] == archive.ref_a
        assert "chat_id" not in frame
        assert frame["message"]["id"] == 4


# ============================================================================
# (i) Stored rows and logs speak refs, never chat ids
# ============================================================================


class _PathRecorder:
    """Wraps the app and records scope['path'] — exactly what an access log logs."""

    def __init__(self, app):
        self.app = app
        self.paths: list[str] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.paths.append(scope["path"])
        await self.app(scope, receive, send)


class _CaptureHandler(logging.Handler):
    """Captures every formatted record from every logger at INFO and up.

    INFO is production's level (main.py's basicConfig): the claim under test is
    what a real deployment's log stream carries, exc_info tracebacks included
    (the formatter appends them, which is exactly where a leak would hide).
    """

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        self.lines.append(self.format(record))


async def test_stored_rows_and_logs_carry_the_ref_and_never_the_chat_id(viewer_app):
    """Drive a full chat-scoped workload and grep every surface.

    Surfaces: every request path the app saw (the access-log line), every log
    record any logger emitted, every audit row as stored, and the grant/session
    rows the flows wrote. The ref must be present where a chat identity is
    needed; the distinctive chat id digits must appear in NONE of them.
    """
    archive = viewer_app.archive
    adapter = viewer_app.adapter
    recorder = _PathRecorder(web_main.app)
    capture = _CaptureHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.INFO)
    try:
        async with _client(app=recorder) as client:
            # Master: admin flows that persist grants + audit rows.
            resp = await client.post("/api/login", json={"username": MASTER_USERNAME, "password": MASTER_PASSWORD})
            assert resp.status_code == 200, resp.text
            created = await client.post(
                "/api/admin/tokens", json={"label": "matrix-share", "allowed_chat_refs": [archive.ref_a]}
            )
            assert created.status_code == 200, created.text
            share_token = created.json()["token"]
            assert created.json()["allowed_chat_refs"] == [archive.ref_a]

        async with _client(app=recorder) as viewer:
            # Viewer: the chat-scoped read workload, ref-addressed throughout.
            await _login_viewer(viewer, adapter, allowed_chat_refs=json.dumps([archive.ref_a]))
            messages = await viewer.get(f"/api/chats/{archive.ref_a}/messages")
            assert messages.status_code == 200, messages.text
            with_media = next(m for m in messages.json() if m["id"] == 2)
            # The URLs the server mints are the access-log surface of the NEXT
            # request: they must be ref-addressed, chat-id-free.
            assert with_media["media"]["id"] == "2_photo"
            assert with_media["media"]["url"] == f"/media/{archive.ref_a}/2_photo"
            media = await viewer.get(with_media["media"]["url"])
            assert media.status_code == 200
            forbidden = await viewer.get(f"/api/chats/{archive.ref_b}/messages")
            assert forbidden.status_code == 404
            export = await viewer.get(f"/api/chats/{archive.ref_a}/export")
            assert export.status_code == 200

        async with _client(app=recorder) as token_client:
            # Token auth: writes a session row whose grant is ref-based.
            resp = await token_client.post("/auth/token", json={"token": share_token})
            assert resp.status_code == 200, resp.text
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)

    needles = (str(CHAT_A_ID), str(CHAT_B_ID))

    # 1. The access-log surface: the ref addressed the chats; no path carried an id.
    assert any(archive.ref_a in path for path in recorder.paths)
    for path in recorder.paths:
        for needle in needles:
            assert needle not in path, path

    # 2. Every log record from every logger, formatted.
    logged = "\n".join(capture.lines)
    for needle in needles:
        assert needle not in logged

    # 3. Audit rows as STORED: real workload rows exist, and no column of any
    #    row renders the chat id.
    audit_rows = await adapter.get_audit_logs(limit=500)
    actions = {row["action"] for row in audit_rows}
    assert "login_success" in actions
    assert any(action.startswith("token_created") for action in actions)
    rendered = json.dumps(audit_rows, default=str)
    for needle in needles:
        assert needle not in rendered

    # 4. The stored grant rows: refs where a chat identity is needed, the "[]"
    #    tombstone (never ids) in the legacy column.
    token_rows = await adapter.get_all_viewer_tokens()
    share_row = next(row for row in token_rows if row["label"] == "matrix-share")
    assert archive.ref_a in (share_row["allowed_chat_refs"] or "")
    assert share_row["allowed_chat_ids"] == "[]"
    for needle in needles:
        assert needle not in json.dumps(share_row, default=str)

    session_rows = await adapter.load_all_sessions()
    token_sessions = [row for row in session_rows if row["role"] == "token"]
    assert token_sessions, "token auth wrote no session row"
    for row in token_sessions:
        assert archive.ref_a in (row["allowed_chat_refs"] or "")
        assert row["allowed_chat_ids"] == "[]"
