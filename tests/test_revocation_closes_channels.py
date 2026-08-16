"""Revoking a principal must close its LIVE channels, not just its session row.

A principal reaches the archive through three things, not one: the session
cookie, any open WebSocket, and any stored Web Push subscription. Only the
first was ever revoked -- logout, viewer update/deactivate/delete, share-token
revoke/update/delete and session expiry each edited ``_sessions`` and the
``viewer_sessions`` table and stopped there. The socket kept delivering frames
from the ``UserContext`` frozen into it at upgrade time, and the push row kept
delivering notifications with no session involved at all.

Every test here drives the real routes against a database built by ``alembic
upgrade head`` on both supported backends, and proves a channel is gone by
trying to USE it: a socket is proven closed by broadcasting into the very chat
it subscribed to and watching the close frame arrive instead of the message.

The last defence is in ``PushNotificationManager.get_subscriptions``: even if
the purge on a revoking path never ran, a subscription whose owner no longer
resolves to a live principal must receive nothing.
"""

import asyncio
import contextlib
import json
import os
import secrets
import tempfile
import time
from datetime import datetime
from functools import partial
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
from starlette.websockets import WebSocketDisconnect

from alembic import command

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_revocation_"))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Chat, PushSubscription
from src.web import main as web_main

REPO_ROOT = Path(__file__).resolve().parent.parent

CHAT_ID = 900888001

MASTER_USERNAME = "revoc-master"
MASTER_PASSWORD = "master-pass@test/value"  # obvious fake
VIEWER_PASSWORD = "revoc-pass@test/value"  # obvious fake


def _upgrade_to_head(url: str) -> None:
    """Run this tree's real Alembic environment against ``url``."""
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


def _seed(sync_url: str) -> str:
    """One chat under account 1. Returns the ref the ORM minted on INSERT."""
    engine = sa.create_engine(sync_url)
    try:
        with Session(engine) as session:
            session.add(Chat(account_id=1, id=CHAT_ID, type="private", first_name="Revoc", username="revoc_chat"))
            session.commit()
        with engine.connect() as conn:
            ref = conn.execute(sa.text("SELECT ref FROM chats WHERE id = :id"), {"id": CHAT_ID}).scalar_one()
    finally:
        engine.dispose()
    return ref


@pytest.fixture(scope="module", params=("sqlite", "postgresql"))
def revocation_archive(request, tmp_path_factory, postgres_server_url, make_postgres_database):
    """A migration-built, seeded 8.0 database per backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        async_url, sync_url = make_postgres_database("telegram_archive_pytest_revocation")
    else:
        db_path = tmp_path_factory.mktemp("revocation-db") / "archive.db"
        sync_url = f"sqlite:///{db_path}"
        async_url = f"sqlite+aiosqlite:///{db_path}"
    _upgrade_to_head(async_url)
    ref = _seed(sync_url)
    return SimpleNamespace(backend=request.param, async_url=async_url, ref=ref)


def _null_pool_manager(async_url: str) -> DatabaseManager:
    """A DatabaseManager whose engine never pools connections.

    TestClient runs the app on its own portal loop, a different one from
    pytest's; a pooled asyncpg connection binds to the loop that created it, so
    only NullPool lets the same test touch the database from both.
    """
    manager = DatabaseManager(async_url)
    manager.engine = create_async_engine(manager.database_url, poolclass=NullPool, hide_parameters=True)
    manager.async_session_factory = async_sessionmaker(manager.engine, class_=AsyncSession, expire_on_commit=False)
    return manager


@pytest.fixture
async def viewer_app(revocation_archive):
    """The real app wired to the migrated database, auth on, state restored after."""
    manager = _null_pool_manager(revocation_archive.async_url)
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
        "broadcast_chat_cache": dict(web_main._broadcast_chat_cache),
    }
    web_main.db = adapter
    web_main.AUTH_ENABLED = True
    web_main.ALLOW_ANONYMOUS_VIEWER = False
    web_main.VIEWER_USERNAME = MASTER_USERNAME
    web_main.VIEWER_PASSWORD = MASTER_PASSWORD
    web_main._sessions.clear()
    web_main._login_attempts.clear()
    web_main.config.display_chat_ids = set()
    web_main._broadcast_chat_cache.clear()

    try:
        yield SimpleNamespace(adapter=adapter, archive=revocation_archive, ref=revocation_archive.ref)
    finally:
        await _delete_all_push_subscriptions(adapter)
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
        web_main._broadcast_chat_cache.clear()
        web_main._broadcast_chat_cache.update(saved["broadcast_chat_cache"])
        await manager.close()


# ============================================================================
# Helpers
# ============================================================================


async def _request(method: str, url: str, *, cookie: str | None = None, body: dict | None = None):
    """One request through the real ASGI app, on whatever loop is current."""
    cookies = {web_main.AUTH_COOKIE_NAME: cookie} if cookie else None
    async with AsyncClient(
        transport=ASGITransport(app=web_main.app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.request(method, url, json=body)


async def _viewer_with_session(adapter: DatabaseAdapter, *, username: str | None = None) -> tuple[str, str]:
    """A real viewer account plus a live session cookie minted the real way."""
    username = username or f"revoc-viewer-{secrets.token_hex(4)}"
    salt = secrets.token_hex(8)
    await adapter.create_viewer_account(
        username=username,
        password_hash=web_main._hash_password(VIEWER_PASSWORD, salt),
        salt=salt,
        created_by="test",
        is_active=1,
    )
    return username, await web_main._create_session(username, "viewer")


async def _master_cookie() -> str:
    return await web_main._create_session(MASTER_USERNAME, "master")


async def _viewer_id(adapter: DatabaseAdapter, username: str) -> int:
    account = await adapter.get_viewer_by_username(username)
    assert account is not None
    return account["id"]


async def _add_push_subscription(
    adapter: DatabaseAdapter,
    *,
    endpoint: str,
    username: str | None,
    chat_id: int | None = None,
) -> None:
    """Insert a push subscription row exactly as PushNotificationManager.subscribe does."""
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            PushSubscription(
                endpoint=endpoint,
                p256dh="p256dh-not-a-real-key",
                auth="auth-not-a-real-secret",
                chat_id=chat_id,
                username=username,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
            )
        )
        await session.commit()


async def _push_endpoints(adapter: DatabaseAdapter) -> set[str]:
    async with adapter.db_manager.async_session_factory() as session:
        result = await session.execute(sa.select(PushSubscription.endpoint))
        return set(result.scalars().all())


async def _delete_all_push_subscriptions(adapter: DatabaseAdapter) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(sa.delete(PushSubscription))
        await session.commit()


@contextlib.contextmanager
def _revocable_socket(cookie: str):
    """A live socket whose SERVER may close it under us.

    Teardown sends a client-side close, which errors once the server has
    already torn the connection down -- that is the outcome under test, not a
    failure, so only the teardown is suppressed (the body's own exceptions
    still propagate).
    """
    client = TestClient(web_main.app, raise_server_exceptions=False)
    client.cookies.set(web_main.AUTH_COOKIE_NAME, cookie)
    session = client.websocket_connect("/ws/updates")
    socket = session.__enter__()
    try:
        yield socket
    finally:
        with contextlib.suppress(Exception):
            session.__exit__(None, None, None)


def _subscribe(socket, ref: str) -> None:
    socket.send_json({"action": "subscribe", "chat_ref": ref})
    assert socket.receive_json() == {"type": "subscribed", "chat_ref": ref}


def _assert_socket_revoked(socket, ref: str) -> None:
    """Broadcast into the socket's own subscription; a revoked socket is closed, not fed.

    Sending the frame is what makes the assertion falsifiable: a socket that
    outlived its revocation answers with the message, so the failure is a
    delivered frame rather than a hang.
    """
    socket.portal.call(web_main.broadcast_new_message, CHAT_ID, {"id": 9, "text": "after-revocation"})
    with pytest.raises(WebSocketDisconnect) as disconnect:
        socket.receive_json()
    assert disconnect.value.code == 4001


# ============================================================================
# (1) Logout closes THAT browser's socket, and only that one
# ============================================================================


async def test_logout_closes_that_sessions_socket_and_leaves_other_sessions_alone(viewer_app):
    ref = viewer_app.ref
    username, cookie_a = await _viewer_with_session(viewer_app.adapter)
    cookie_b = await web_main._create_session(username, "viewer")

    with _revocable_socket(cookie_a) as socket_a:
        _subscribe(socket_a, ref)

        with _revocable_socket(cookie_b) as socket_b:
            _subscribe(socket_b, ref)

            response = socket_a.portal.call(partial(_request, "POST", "/api/logout", cookie=cookie_a))
            assert response.status_code == 200, response.text

            # The other browser is a different session: it must survive intact.
            socket_b.send_json({"action": "ping"})
            assert socket_b.receive_json() == {"type": "pong"}
            assert cookie_b in web_main._sessions

        _assert_socket_revoked(socket_a, ref)


# ============================================================================
# (2) Viewer deactivate / delete, and share-token revoke, close their sockets
# ============================================================================


async def test_deactivating_a_viewer_closes_its_socket(viewer_app):
    ref = viewer_app.ref
    username, cookie = await _viewer_with_session(viewer_app.adapter)
    viewer_id = await _viewer_id(viewer_app.adapter, username)
    master = await _master_cookie()

    with _revocable_socket(cookie) as socket:
        _subscribe(socket, ref)
        response = socket.portal.call(
            partial(_request, "PUT", f"/api/admin/viewers/{viewer_id}", cookie=master, body={"is_active": False})
        )
        assert response.status_code == 200, response.text
        _assert_socket_revoked(socket, ref)


async def test_deleting_a_viewer_closes_its_socket(viewer_app):
    ref = viewer_app.ref
    username, cookie = await _viewer_with_session(viewer_app.adapter)
    viewer_id = await _viewer_id(viewer_app.adapter, username)
    master = await _master_cookie()

    with _revocable_socket(cookie) as socket:
        _subscribe(socket, ref)
        response = socket.portal.call(partial(_request, "DELETE", f"/api/admin/viewers/{viewer_id}", cookie=master))
        assert response.status_code == 200, response.text
        _assert_socket_revoked(socket, ref)


async def test_revoking_a_share_token_closes_its_socket(viewer_app):
    ref = viewer_app.ref
    master = await _master_cookie()

    created = await _request(
        "POST", "/api/admin/tokens", cookie=master, body={"label": "revoc-share", "allowed_chat_refs": [ref]}
    )
    assert created.status_code == 200, created.text
    token_id = created.json()["id"]

    authenticated = await _request("POST", "/auth/token", body={"token": created.json()["token"]})
    assert authenticated.status_code == 200, authenticated.text
    cookie = authenticated.cookies[web_main.AUTH_COOKIE_NAME]

    with _revocable_socket(cookie) as socket:
        _subscribe(socket, ref)
        response = socket.portal.call(
            partial(_request, "PUT", f"/api/admin/tokens/{token_id}", cookie=master, body={"is_revoked": True})
        )
        assert response.status_code == 200, response.text
        _assert_socket_revoked(socket, ref)


# ============================================================================
# (3) An expired session loses its socket too
# ============================================================================


async def _sweep_expired_sessions() -> None:
    """Run the real background sweep once, then stop it."""
    saved_interval = web_main._SESSION_CLEANUP_INTERVAL
    web_main._SESSION_CLEANUP_INTERVAL = 0.01
    task = asyncio.create_task(web_main.session_cleanup_task())
    try:
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        web_main._SESSION_CLEANUP_INTERVAL = saved_interval


async def test_expired_session_loses_its_socket_on_the_cleanup_sweep(viewer_app):
    ref = viewer_app.ref
    _username, cookie = await _viewer_with_session(viewer_app.adapter)

    with _revocable_socket(cookie) as socket:
        _subscribe(socket, ref)

        # The socket was admitted by a live session; the session then ages out
        # underneath it, which is exactly the case a sweep of _sessions alone
        # cannot reach.
        web_main._sessions[cookie].created_at = time.time() - web_main.AUTH_SESSION_SECONDS - 60
        socket.portal.call(_sweep_expired_sessions)
        assert cookie not in web_main._sessions

        _assert_socket_revoked(socket, ref)


async def test_a_request_that_expires_a_session_closes_its_socket_too(viewer_app):
    """The sweep is not the only path that expires a session.

    A request carrying an aged cookie expires it on the spot, which takes it
    out of the sweep's reach: whoever expires a session owns closing the
    sockets it admitted, or this one outlives every path that could close it.
    """
    ref = viewer_app.ref
    _username, cookie = await _viewer_with_session(viewer_app.adapter)

    with _revocable_socket(cookie) as socket:
        _subscribe(socket, ref)

        web_main._sessions[cookie].created_at = time.time() - web_main.AUTH_SESSION_SECONDS - 60
        response = socket.portal.call(partial(_request, "GET", "/api/chats", cookie=cookie))
        assert response.status_code == 401, response.text
        assert cookie not in web_main._sessions

        _assert_socket_revoked(socket, ref)


# ============================================================================
# (4) Every revocation trigger deletes the principal's push subscriptions
# ============================================================================


async def test_logout_purges_that_users_push_subscriptions(viewer_app):
    adapter = viewer_app.adapter
    username, cookie = await _viewer_with_session(adapter)
    bystander, _ = await _viewer_with_session(adapter)
    await _add_push_subscription(adapter, endpoint="https://push.example.com/logout-owner", username=username)
    await _add_push_subscription(adapter, endpoint="https://push.example.com/bystander", username=bystander)

    response = await _request("POST", "/api/logout", cookie=cookie)
    assert response.status_code == 200, response.text

    assert await _push_endpoints(adapter) == {"https://push.example.com/bystander"}


async def test_deactivating_and_deleting_a_viewer_purge_its_push_subscriptions(viewer_app):
    adapter = viewer_app.adapter
    master = await _master_cookie()

    deactivated, _ = await _viewer_with_session(adapter)
    deleted, _ = await _viewer_with_session(adapter)
    bystander, _ = await _viewer_with_session(adapter)
    await _add_push_subscription(adapter, endpoint="https://push.example.com/deactivated", username=deactivated)
    await _add_push_subscription(adapter, endpoint="https://push.example.com/deleted", username=deleted)
    await _add_push_subscription(adapter, endpoint="https://push.example.com/bystander", username=bystander)

    disabled = await _request(
        "PUT",
        f"/api/admin/viewers/{await _viewer_id(adapter, deactivated)}",
        cookie=master,
        body={"is_active": False},
    )
    assert disabled.status_code == 200, disabled.text
    removed = await _request("DELETE", f"/api/admin/viewers/{await _viewer_id(adapter, deleted)}", cookie=master)
    assert removed.status_code == 200, removed.text

    assert await _push_endpoints(adapter) == {"https://push.example.com/bystander"}


async def test_revoking_a_share_token_purges_its_push_subscriptions(viewer_app):
    adapter = viewer_app.adapter
    master = await _master_cookie()

    created = await _request(
        "POST",
        "/api/admin/tokens",
        cookie=master,
        body={"label": "revoc-push-share", "allowed_chat_refs": [viewer_app.ref]},
    )
    assert created.status_code == 200, created.text
    token_id = created.json()["id"]

    authenticated = await _request("POST", "/auth/token", body={"token": created.json()["token"]})
    assert authenticated.status_code == 200, authenticated.text
    # The username the token principal really subscribes under, read from the
    # mint site itself rather than reconstructed here.
    principal = authenticated.json()["username"]
    assert principal.startswith("token:")
    await _add_push_subscription(adapter, endpoint="https://push.example.com/token-owner", username=principal)

    response = await _request("PUT", f"/api/admin/tokens/{token_id}", cookie=master, body={"is_revoked": True})
    assert response.status_code == 200, response.text

    assert await _push_endpoints(adapter) == set()


# ============================================================================
# (5) The backstop: a subscription whose owner is gone receives nothing
# ============================================================================


async def test_get_subscriptions_drops_rows_whose_owner_no_longer_lives(viewer_app):
    """Even with every purge missed, a dead owner's channel must go quiet.

    The rows here are written straight to the table, which is what a missed
    purge leaves behind: the revoking path already ran and deleted nothing.
    """
    pytest.importorskip("pywebpush")
    from src.web.push import PushNotificationManager

    adapter = viewer_app.adapter
    master = await _master_cookie()

    live_viewer, _ = await _viewer_with_session(adapter)
    inactive_viewer, _ = await _viewer_with_session(adapter)
    await adapter.update_viewer_account(await _viewer_id(adapter, inactive_viewer), is_active=0)

    live_token = await _request(
        "POST",
        "/api/admin/tokens",
        cookie=master,
        body={"label": "backstop-live", "allowed_chat_refs": [viewer_app.ref]},
    )
    revoked_token = await _request(
        "POST",
        "/api/admin/tokens",
        cookie=master,
        body={"label": "backstop-revoked", "allowed_chat_refs": [viewer_app.ref]},
    )
    live_principal = (await _request("POST", "/auth/token", body={"token": live_token.json()["token"]})).json()[
        "username"
    ]
    revoked_principal = (await _request("POST", "/auth/token", body={"token": revoked_token.json()["token"]})).json()[
        "username"
    ]
    revoke = await _request(
        "PUT", f"/api/admin/tokens/{revoked_token.json()['id']}", cookie=master, body={"is_revoked": True}
    )
    assert revoke.status_code == 200, revoke.text

    rows = {
        "https://push.example.com/live-viewer": live_viewer,
        "https://push.example.com/inactive-viewer": inactive_viewer,
        "https://push.example.com/deleted-viewer": "revoc-viewer-never-existed",
        "https://push.example.com/live-token": live_principal,
        "https://push.example.com/revoked-token": revoked_principal,
        "https://push.example.com/master": MASTER_USERNAME,
        "https://push.example.com/legacy-row": None,
    }
    for endpoint, username in rows.items():
        await _add_push_subscription(adapter, endpoint=endpoint, username=username)

    manager = PushNotificationManager(adapter, web_main.config, configured_principals=web_main._configured_principals())
    delivered = await manager.get_subscriptions(CHAT_ID, account_id=1, chat_ref=viewer_app.ref)

    assert {sub["endpoint"] for sub in delivered} == {
        "https://push.example.com/live-viewer",
        "https://push.example.com/live-token",
        "https://push.example.com/master",
        "https://push.example.com/legacy-row",
    }


async def test_a_live_owner_still_receives_after_an_unrelated_revocation(viewer_app):
    """The backstop must not become a silent global mute.

    Deleting one viewer account leaves everyone else's channel working -- the
    check that proves the filter can go red also has to prove it can stay green.
    """
    pytest.importorskip("pywebpush")
    from src.web.push import PushNotificationManager

    adapter = viewer_app.adapter
    master = await _master_cookie()
    keeper, _ = await _viewer_with_session(adapter)
    doomed, _ = await _viewer_with_session(adapter)

    await _add_push_subscription(adapter, endpoint="https://push.example.com/keeper", username=keeper)
    removed = await _request("DELETE", f"/api/admin/viewers/{await _viewer_id(adapter, doomed)}", cookie=master)
    assert removed.status_code == 200, removed.text

    manager = PushNotificationManager(adapter, web_main.config, configured_principals=web_main._configured_principals())
    delivered = await manager.get_subscriptions(CHAT_ID, account_id=1, chat_ref=viewer_app.ref)
    assert [sub["endpoint"] for sub in delivered] == ["https://push.example.com/keeper"]


# ============================================================================
# The entitlement snapshot a subscription carries is still enforced
# ============================================================================


async def test_a_live_owners_grant_still_bounds_delivery(viewer_app):
    """Liveness is an ADDITIONAL gate, never a replacement for the ref grant."""
    pytest.importorskip("pywebpush")
    from src.web.push import PushNotificationManager

    adapter = viewer_app.adapter
    username, _ = await _viewer_with_session(adapter)
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            PushSubscription(
                endpoint="https://push.example.com/other-chat-only",
                p256dh="p256dh-not-a-real-key",
                auth="auth-not-a-real-secret",
                username=username,
                allowed_chat_ids="[]",
                allowed_chat_refs=json.dumps(["someOtherRefAAAABB0001"]),
                created_at=datetime(2026, 4, 1, 9, 0, 0),
            )
        )
        await session.commit()

    manager = PushNotificationManager(adapter, web_main.config, configured_principals=web_main._configured_principals())
    assert await manager.get_subscriptions(CHAT_ID, account_id=1, chat_ref=viewer_app.ref) == []
