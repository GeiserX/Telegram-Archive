"""Ending every viewer session at once, and a second viewer noticing it.

Three layers, each proven where it can fail:

* ``DatabaseAdapter.delete_all_sessions`` runs against a real engine on both
  backends (``real_adapter``), because it uses DELETE ... RETURNING.
* ``POST /api/admin/sessions/end-all`` runs through the real app on the real
  adapter: who may call it, what it ends, what ``keep_current`` keeps.
* The session cache: a cached session whose row another process deleted must
  stop working within ``_SESSION_REVALIDATE_SECONDS``, on a request and in the
  sweep.

The button in Admin Settings is lifted out of the template and run under node.
"""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_frontend_audit_fixes import INDEX_HTML, _extract_const_arrow_function, _run_node

from telegram_archive.db.models import PushSubscription, ViewerAuditLog
from telegram_archive.web import main as web_main

MASTER = "test-master"
VIEWER = "test-viewer"
TOKEN_USER = "token:Fake label"
PROXY_ADMIN = "proxy-admin"
PROXY_HEADER = "X-Test-User"
ROUTE = "/api/admin/sessions/end-all"


# ============================================================================
# Adapter, real engine on both backends
# ============================================================================


async def _save(adapter, token: str, username: str, role: str = "viewer") -> None:
    now = time.time()
    await adapter.save_session(
        token=token, username=username, role=role, allowed_chat_ids=None, created_at=now, last_accessed=now
    )


async def _stored_tokens(adapter) -> set[str]:
    return {row["token"] for row in await adapter.load_all_sessions()}


class TestDeleteAllSessionsRealEngine:
    async def test_deletes_every_row_and_reports_each(self, real_adapter):
        await _save(real_adapter, "tok-a", "user-a")
        await _save(real_adapter, "tok-b", "user-b")
        await _save(real_adapter, "tok-c", "user-a")

        deleted = await real_adapter.delete_all_sessions()

        assert sorted(deleted) == [("tok-a", "user-a"), ("tok-b", "user-b"), ("tok-c", "user-a")]
        assert await _stored_tokens(real_adapter) == set()

    async def test_keep_token_survives(self, real_adapter):
        await _save(real_adapter, "tok-a", "user-a")
        await _save(real_adapter, "tok-b", "user-b")

        deleted = await real_adapter.delete_all_sessions(keep_token="tok-b")

        assert deleted == [("tok-a", "user-a")]
        assert await _stored_tokens(real_adapter) == {"tok-b"}

    async def test_empty_table_reports_nothing(self, real_adapter):
        assert await real_adapter.delete_all_sessions() == []

    async def test_without_returning_reads_then_deletes(self, real_adapter, monkeypatch):
        """SQLite before 3.35 has no RETURNING; the same answer comes from a read and a delete."""
        monkeypatch.setattr(real_adapter.db_manager.engine.dialect, "delete_returning", False)
        await _save(real_adapter, "tok-a", "user-a")
        await _save(real_adapter, "tok-b", "user-b")
        await _save(real_adapter, "tok-c", "user-a")

        deleted = await real_adapter.delete_all_sessions(keep_token="tok-b")

        assert sorted(deleted) == [("tok-a", "user-a"), ("tok-c", "user-a")]
        assert await _stored_tokens(real_adapter) == {"tok-b"}

    async def test_without_returning_deletes_in_chunks(self, real_adapter, monkeypatch):
        """Old SQLite caps bound variables, so the tokens go in chunks and every chunk is deleted."""
        monkeypatch.setattr(real_adapter.db_manager.engine.dialect, "delete_returning", False)
        monkeypatch.setattr(real_adapter, "_SESSION_DELETE_CHUNK", 2)
        for index in range(5):
            await _save(real_adapter, f"tok-{index}", "user-a")
        await _save(real_adapter, "tok-keep", "user-b")

        deleted = await real_adapter.delete_all_sessions(keep_token="tok-keep")

        assert sorted(deleted) == [(f"tok-{index}", "user-a") for index in range(5)]
        assert await _stored_tokens(real_adapter) == {"tok-keep"}


# ============================================================================
# Route, real app on the real adapter
# ============================================================================


@pytest.fixture
def viewer_app(real_adapter, monkeypatch):
    """The app wired to a real database with the master login on and an empty cache."""
    close_for = AsyncMock(return_value=0)
    monkeypatch.setattr(web_main, "db", real_adapter)
    monkeypatch.setattr(web_main, "AUTH_ENABLED", True)
    monkeypatch.setattr(web_main, "ALLOW_ANONYMOUS_VIEWER", False)
    monkeypatch.setattr(web_main, "VIEWER_USERNAME", MASTER)
    monkeypatch.setattr(web_main, "VIEWER_PASSWORD", "fake-pass@test/value")
    monkeypatch.setattr(web_main, "_PROXY_AUTH_ENABLED", False)
    monkeypatch.setattr(web_main, "_sessions", {})
    monkeypatch.setattr(web_main.ws_manager, "close_for", close_for)
    return real_adapter, close_for


async def _post(url: str, *, cookie: str | None = None, headers: dict | None = None, content: bytes | None = None):
    cookies = {web_main.AUTH_COOKIE_NAME: cookie} if cookie else None
    async with AsyncClient(
        transport=ASGITransport(app=web_main.app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(url, headers=headers, content=content)


async def _get(url: str, *, cookie: str):
    async with AsyncClient(
        transport=ASGITransport(app=web_main.app),
        base_url="http://test",
        cookies={web_main.AUTH_COOKIE_NAME: cookie},
    ) as client:
        return await client.get(url)


async def _three_sessions() -> tuple[str, str, str]:
    """A master, a viewer account and a share-link session, minted the real way."""
    master = await web_main._create_session(MASTER, "master")
    viewer = await web_main._create_session(VIEWER, "viewer", allowed_chat_refs={"fakeRef0001"})
    token = await web_main._create_session(TOKEN_USER, "token", allowed_chat_refs={"fakeRef0001"}, source_token_id=1)
    return master, viewer, token


async def _add_push(adapter, username: str) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            PushSubscription(endpoint=f"https://push.invalid/{username}", p256dh="fake", auth="fake", username=username)
        )
        await session.commit()


async def _push_owners(adapter) -> set[str]:
    async with adapter.db_manager.async_session_factory() as session:
        return set((await session.execute(select(PushSubscription.username))).scalars().all())


async def _audit_actions(adapter) -> list[str]:
    async with adapter.db_manager.async_session_factory() as session:
        return list((await session.execute(select(ViewerAuditLog.action))).scalars().all())


def _closed_keys(close_for: AsyncMock) -> set[str]:
    keys: set[str] = set()
    for call in close_for.await_args_list:
        keys.update(call.kwargs.get("session_keys") or ())
    return keys


def _clears_cookie(response) -> bool:
    return any(
        header.startswith(f"{web_main.AUTH_COOKIE_NAME}=") and "Max-Age=0" in header
        for header in response.headers.get_list("set-cookie")
    )


class TestEndAllSessionsRoute:
    async def test_master_ends_every_session_including_its_own(self, viewer_app):
        adapter, close_for = viewer_app
        master, viewer, token = await _three_sessions()
        for username in (MASTER, VIEWER, TOKEN_USER, "no-session-user"):
            await _add_push(adapter, username)

        response = await _post(ROUTE, cookie=master)

        assert response.status_code == 200
        assert response.json() == {"success": True, "ended": 3, "current_session_ended": True}
        assert _clears_cookie(response)
        assert await _stored_tokens(adapter) == set()
        assert web_main._sessions == {}
        assert _closed_keys(close_for) == {master, viewer, token}
        assert await _push_owners(adapter) == {"no-session-user"}
        assert await _audit_actions(adapter) == ["sessions_ended_all"]
        assert (await _get("/api/admin/settings", cookie=master)).status_code == 401

    async def test_ends_rows_this_process_never_cached(self, viewer_app):
        """A session another viewer created is in the table only; it ends too."""
        adapter, close_for = viewer_app
        master = await web_main._create_session(MASTER, "master")
        await _save(adapter, "other-process-token", VIEWER)

        response = await _post(ROUTE, cookie=master)

        assert response.json()["ended"] == 2
        assert await _stored_tokens(adapter) == set()
        assert "other-process-token" in _closed_keys(close_for)

    async def test_keep_current_query_keeps_the_callers_session(self, viewer_app):
        adapter, close_for = viewer_app
        master, viewer, token = await _three_sessions()
        other_master = await web_main._create_session(MASTER, "master")
        for username in (MASTER, VIEWER):
            await _add_push(adapter, username)

        response = await _post(f"{ROUTE}?keep_current=true", cookie=master)

        assert response.status_code == 200
        assert response.json() == {"success": True, "ended": 3, "current_session_ended": False}
        assert not _clears_cookie(response)
        assert await _stored_tokens(adapter) == {master}
        assert set(web_main._sessions) == {master}
        assert _closed_keys(close_for) == {viewer, token, other_master}
        # The master's other session ended, and push is purged per user, so the
        # master's subscriptions go too; the kept browser subscribes again on load.
        assert await _push_owners(adapter) == set()
        assert await _audit_actions(adapter) == ["sessions_ended_all:kept_current"]
        assert (await _get("/api/admin/settings", cookie=master)).status_code == 200

    async def test_keep_current_leaves_the_kept_users_push_alone(self, viewer_app):
        """With no other master session ended, the kept browser's push channel stays."""
        adapter, _ = viewer_app
        master, _, _ = await _three_sessions()
        for username in (MASTER, VIEWER):
            await _add_push(adapter, username)

        response = await _post(f"{ROUTE}?keep_current=true", cookie=master)

        assert response.json() == {"success": True, "ended": 2, "current_session_ended": False}
        assert await _push_owners(adapter) == {MASTER}

    async def test_failed_audit_write_still_answers_and_clears_the_cookie(self, viewer_app, monkeypatch):
        adapter, _ = viewer_app
        master, _, _ = await _three_sessions()
        monkeypatch.setattr(adapter, "create_audit_log", AsyncMock(side_effect=RuntimeError("database is locked")))

        response = await _post(ROUTE, cookie=master)

        assert response.status_code == 200
        assert response.json() == {"success": True, "ended": 3, "current_session_ended": True}
        assert _clears_cookie(response)
        assert await _stored_tokens(adapter) == set()

    async def test_keep_current_json_body(self, viewer_app):
        adapter, _ = viewer_app
        master, _, _ = await _three_sessions()

        response = await _post(
            ROUTE,
            cookie=master,
            headers={"Content-Type": "application/json"},
            content=json.dumps({"keep_current": True}).encode(),
        )

        assert response.json()["current_session_ended"] is False
        assert await _stored_tokens(adapter) == {master}

    async def test_query_wins_over_body(self, viewer_app):
        adapter, _ = viewer_app
        master, _, _ = await _three_sessions()

        response = await _post(
            f"{ROUTE}?keep_current=false", cookie=master, content=json.dumps({"keep_current": True}).encode()
        )

        assert response.json()["current_session_ended"] is True
        assert await _stored_tokens(adapter) == set()

    @pytest.mark.parametrize(
        ("content", "detail"),
        [
            (b"not json", "Body must be JSON"),
            (b"[true]", "Body must be a JSON object"),
            (b'{"keep_current": "yes"}', "keep_current must be true or false"),
        ],
    )
    async def test_bad_body_is_refused_and_ends_nothing(self, viewer_app, content, detail):
        adapter, _ = viewer_app
        master, viewer, token = await _three_sessions()

        response = await _post(ROUTE, cookie=master, content=content)

        assert response.status_code == 400
        assert response.json()["detail"] == detail
        assert await _stored_tokens(adapter) == {master, viewer, token}

    @pytest.mark.parametrize("which", ["viewer", "token"])
    async def test_non_master_session_is_denied(self, viewer_app, which):
        adapter, _ = viewer_app
        master, viewer, token = await _three_sessions()
        cookie = viewer if which == "viewer" else token

        response = await _post(ROUTE, cookie=cookie)

        assert response.status_code == 403
        assert await _stored_tokens(adapter) == {master, viewer, token}

    async def test_viewer_only_header_denies_the_master(self, viewer_app):
        adapter, _ = viewer_app
        master, viewer, token = await _three_sessions()

        response = await _post(ROUTE, cookie=master, headers={"X-Viewer-Only": "true"})

        assert response.status_code == 403
        assert await _stored_tokens(adapter) == {master, viewer, token}

    async def test_no_login_is_unauthorized(self, viewer_app):
        adapter, _ = viewer_app
        master, viewer, token = await _three_sessions()

        response = await _post(ROUTE)

        assert response.status_code == 401
        assert await _stored_tokens(adapter) == {master, viewer, token}

    async def test_proxy_admin_has_no_session_of_its_own(self, viewer_app, monkeypatch):
        """A proxy-header master may call it; a cookie it carries for another user is not its session."""
        adapter, _ = viewer_app
        monkeypatch.setattr(web_main, "_PROXY_AUTH_ENABLED", True)
        monkeypatch.setattr(web_main, "AUTH_PROXY_HEADER", PROXY_HEADER)
        monkeypatch.setattr(web_main, "AUTH_PROXY_ADMIN_USERS", {PROXY_ADMIN})
        _, viewer, _ = await _three_sessions()

        response = await _post(f"{ROUTE}?keep_current=true", cookie=viewer, headers={PROXY_HEADER: PROXY_ADMIN})

        assert response.status_code == 200
        assert response.json() == {"success": True, "ended": 3, "current_session_ended": False}
        assert not _clears_cookie(response)
        assert await _stored_tokens(adapter) == set()

    async def test_proxy_admin_by_default_is_not_told_its_session_ended(self, viewer_app, monkeypatch):
        """Without keep_current a proxy admin still has no session of its own to end or clear."""
        adapter, _ = viewer_app
        monkeypatch.setattr(web_main, "_PROXY_AUTH_ENABLED", True)
        monkeypatch.setattr(web_main, "AUTH_PROXY_HEADER", PROXY_HEADER)
        monkeypatch.setattr(web_main, "AUTH_PROXY_ADMIN_USERS", {PROXY_ADMIN})
        _, viewer, _ = await _three_sessions()

        response = await _post(ROUTE, cookie=viewer, headers={PROXY_HEADER: PROXY_ADMIN})

        assert response.status_code == 200
        assert response.json() == {"success": True, "ended": 3, "current_session_ended": False}
        assert not _clears_cookie(response)
        assert await _stored_tokens(adapter) == set()
        assert await _audit_actions(adapter) == ["sessions_ended_all"]

    async def test_proxy_viewer_is_denied(self, viewer_app, monkeypatch):
        adapter, _ = viewer_app
        monkeypatch.setattr(web_main, "_PROXY_AUTH_ENABLED", True)
        monkeypatch.setattr(web_main, "AUTH_PROXY_HEADER", PROXY_HEADER)
        monkeypatch.setattr(web_main, "AUTH_PROXY_ADMIN_USERS", {PROXY_ADMIN})
        master, viewer, token = await _three_sessions()

        response = await _post(ROUTE, headers={PROXY_HEADER: "proxy-plain-user"})

        assert response.status_code == 403
        assert await _stored_tokens(adapter) == {master, viewer, token}


# ============================================================================
# A second viewer: its cache must follow the table
# ============================================================================


class TestSecondViewerCacheRealEngine:
    async def test_cached_session_stops_once_its_row_is_gone(self, viewer_app):
        """Rows deleted by another process end the session here within one window."""
        adapter, close_for = viewer_app
        master = await web_main._create_session(MASTER, "master")
        await adapter.delete_all_sessions()  # what the other viewer's end-all does

        # Inside the window the cache still answers: that is the bounded lag.
        assert (await _get("/api/admin/settings", cookie=master)).status_code == 200

        web_main._sessions[master].validated_at -= web_main._SESSION_REVALIDATE_SECONDS + 1
        assert (await _get("/api/admin/settings", cookie=master)).status_code == 401
        assert master not in web_main._sessions
        assert master in _closed_keys(close_for)


class _CacheTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.get_session = AsyncMock(return_value={"token": "t"})
        self.db.load_all_sessions = AsyncMock(return_value=[])
        self.close_for = AsyncMock(return_value=0)
        patchers = [
            patch.object(web_main, "db", self.db),
            patch.object(web_main, "_sessions", {}),
            patch.object(web_main.ws_manager, "close_for", self.close_for),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _session(*, stale: bool) -> web_main.SessionData:
        session = web_main.SessionData(username="u", role="viewer")
        if stale:
            session.validated_at = time.time() - web_main._SESSION_REVALIDATE_SECONDS - 1
        return session


class TestRevalidateCachedSession(_CacheTestBase):
    async def test_fresh_session_skips_the_database(self):
        session = self._session(stale=False)
        web_main._sessions["t"] = session

        self.assertIs(await web_main._resolve_session("t"), session)
        self.db.get_session.assert_not_awaited()

    async def test_stale_session_with_a_row_is_kept_and_renewed(self):
        session = self._session(stale=True)
        web_main._sessions["t"] = session
        before = time.time()

        self.assertIs(await web_main._resolve_session("t"), session)
        self.db.get_session.assert_awaited_once_with("t")
        self.assertGreaterEqual(session.validated_at, before)

    async def test_stale_session_without_a_row_is_dropped(self):
        web_main._sessions["t"] = self._session(stale=True)
        self.db.get_session = AsyncMock(return_value=None)

        self.assertIsNone(await web_main._resolve_session("t"))
        self.assertNotIn("t", web_main._sessions)
        self.close_for.assert_awaited_once_with(session_keys=["t"])

    async def test_database_error_keeps_the_session_and_backs_off(self):
        session = self._session(stale=True)
        web_main._sessions["t"] = session
        self.db.get_session = AsyncMock(side_effect=RuntimeError("db down"))

        with self.assertLogs(web_main.logger, level="WARNING"):
            self.assertIs(await web_main._resolve_session("t"), session)
        self.close_for.assert_not_awaited()

        # The next requests inside the back-off answer from the cache.
        self.assertIs(await web_main._resolve_session("t"), session)
        self.assertIs(await web_main._resolve_session("t"), session)
        self.db.get_session.assert_awaited_once()

        # Once the back-off has passed, the database is read again.
        session.validated_at -= web_main._SESSION_REVALIDATE_RETRY_SECONDS + 1
        with self.assertLogs(web_main.logger, level="WARNING"):
            await web_main._resolve_session("t")
        self.assertEqual(self.db.get_session.await_count, 2)

    async def test_login_whose_row_was_not_written_ends_at_the_first_readable_recheck(self):
        """A failed write keeps the login, but the row is never written back later.

        Writing it back would revive a session an end-all on another viewer
        already ended, so a missing row means ended here too.
        """
        self.db.save_session = AsyncMock(side_effect=RuntimeError("database is locked"))
        with self.assertLogs(web_main.logger, level="WARNING"):
            token = await web_main._create_session("u", "viewer")
        session = web_main._sessions[token]

        # The table cannot be read either: the login is kept.
        self.db.get_session = AsyncMock(side_effect=RuntimeError("db down"))
        session.validated_at -= web_main._SESSION_REVALIDATE_SECONDS + 1
        with self.assertLogs(web_main.logger, level="WARNING"):
            self.assertIs(await web_main._resolve_session(token), session)

        # The table reads again and has no row: the login ends, nothing is written.
        self.db.get_session = AsyncMock(return_value=None)
        session.validated_at -= web_main._SESSION_REVALIDATE_SECONDS + 1
        self.assertIsNone(await web_main._resolve_session(token))
        self.assertNotIn(token, web_main._sessions)
        self.assertEqual(self.db.save_session.await_count, 1)
        self.close_for.assert_awaited_once_with(session_keys=[token])

    async def test_without_a_database_the_cache_answers(self):
        session = self._session(stale=True)
        web_main._sessions["t"] = session

        with patch.object(web_main, "db", None):
            self.assertIs(await web_main._resolve_session("t"), session)


class TestRevalidateAllCachedSessions(_CacheTestBase):
    async def test_live_renewed_gone_dropped_fresh_untouched(self):
        live, gone, fresh = self._session(stale=True), self._session(stale=True), self._session(stale=False)
        web_main._sessions.update({"live": live, "gone": gone, "fresh": fresh})
        self.db.load_all_sessions = AsyncMock(return_value=[{"token": "live"}])
        before = time.time()

        with self.assertLogs(web_main.logger, level="INFO"):
            await web_main._revalidate_all_cached_sessions()

        self.assertEqual(set(web_main._sessions), {"live", "fresh"})
        self.assertGreaterEqual(live.validated_at, before)
        self.close_for.assert_awaited_once_with(session_keys=["gone"])

    async def test_nothing_stale_reads_nothing(self):
        web_main._sessions["fresh"] = self._session(stale=False)

        await web_main._revalidate_all_cached_sessions()

        self.db.load_all_sessions.assert_not_awaited()

    async def test_session_replaced_during_the_read_is_left_alone(self):
        web_main._sessions["t"] = self._session(stale=True)
        replacement = self._session(stale=False)

        async def replace_then_read():
            web_main._sessions["t"] = replacement
            return []

        self.db.load_all_sessions = AsyncMock(side_effect=replace_then_read)

        await web_main._revalidate_all_cached_sessions()

        self.assertIs(web_main._sessions["t"], replacement)
        self.close_for.assert_not_awaited()

    async def test_database_error_keeps_every_session(self):
        web_main._sessions["t"] = self._session(stale=True)
        self.db.load_all_sessions = AsyncMock(side_effect=RuntimeError("db down"))

        with self.assertLogs(web_main.logger, level="WARNING"):
            await web_main._revalidate_all_cached_sessions()

        self.assertIn("t", web_main._sessions)

    async def test_without_a_database_nothing_happens(self):
        web_main._sessions["t"] = self._session(stale=True)

        with patch.object(web_main, "db", None):
            await web_main._revalidate_all_cached_sessions()

        self.assertIn("t", web_main._sessions)

    async def test_the_sweep_runs_it(self):
        self.db.cleanup_expired_sessions = AsyncMock(return_value=0)
        web_main._sessions["gone"] = self._session(stale=True)

        with patch.object(web_main, "_SESSION_CLEANUP_INTERVAL", 0):
            task = asyncio.create_task(web_main.session_cleanup_task())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertNotIn("gone", web_main._sessions)


# ============================================================================
# The Admin Settings button, run under node
# ============================================================================

_JS_PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const isAuthenticated = ref(true);
const userRole = ref('master');
const currentUsername = ref('test-master');
const showAdminPanel = ref(true);
let auditLoads = 0;
const loadAdminAudit = async () => { auditLoads += 1; };
let confirmAnswer = true;
const confirm = () => confirmAnswer;
const calls = [];
let nextResponse = null;
const fetch = async (url, options) => { calls.push({ url, options }); return nextResponse; };
const reply = (status, body) => ({ ok: status < 400, status, json: async () => body });
let unsubscribes = 0;
const unsubscribeFromPush = async () => { unsubscribes += 1; };
const removedKeys = [];
const localStorage = { removeItem: key => removedKeys.push(key) };
"""


def _end_all_script(body: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_const_arrow_function(html, "endAllSessions", asynchronous=True)
    refs = "const adminSessionsError = ref('');\nconst adminSessionsMessage = ref('');\n"
    runner = f"(async () => {{\n{body}\n}})().catch(error => {{ process.stderr.write(`${{error.stack}}\\n`); process.exitCode = 1; }});"
    return "\n".join([_JS_PRELUDE, refs, function, runner])


def test_admin_panel_has_the_sessions_tab():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '@click="endAllSessions(false)"' in html
    assert '@click="endAllSessions(true)"' in html
    assert "endAllSessions," in html


def test_ending_every_session_returns_to_the_login_page():
    _run_node(
        _end_all_script(
            """
nextResponse = reply(200, { success: true, ended: 3, current_session_ended: true });
await endAllSessions(false);
assert.equal(unsubscribes, 1);
assert.deepEqual(removedKeys, ['push_enabled']);
assert.equal(calls.length, 1);
assert.equal(calls[0].url, '/api/admin/sessions/end-all?keep_current=false');
assert.equal(calls[0].options.method, 'POST');
assert.equal(isAuthenticated.value, false);
assert.equal(showAdminPanel.value, false);
assert.equal(userRole.value, '');
"""
        )
    )


def test_keeping_this_session_reports_the_count_and_stays():
    _run_node(
        _end_all_script(
            """
nextResponse = reply(200, { success: true, ended: 2, current_session_ended: false });
await endAllSessions(true);
assert.equal(unsubscribes, 0);
assert.deepEqual(removedKeys, []);
assert.equal(calls[0].url, '/api/admin/sessions/end-all?keep_current=true');
assert.equal(isAuthenticated.value, true);
assert.equal(showAdminPanel.value, true);
assert.equal(adminSessionsMessage.value, 'Ended 2 session(s).');
assert.equal(auditLoads, 1);
"""
        )
    )


def test_session_that_did_not_end_keeps_its_push():
    """A proxy admin ends everything but has no session of its own, so its push stays."""
    _run_node(
        _end_all_script(
            """
nextResponse = reply(200, { success: true, ended: 3, current_session_ended: false });
await endAllSessions(false);
assert.equal(unsubscribes, 0);
assert.deepEqual(removedKeys, []);
assert.equal(isAuthenticated.value, true);
assert.equal(adminSessionsMessage.value, 'Ended 3 session(s).');
"""
        )
    )


def test_cancelled_confirm_sends_nothing_and_errors_are_shown():
    _run_node(
        _end_all_script(
            """
confirmAnswer = false;
await endAllSessions(false);
assert.equal(calls.length, 0);
assert.equal(unsubscribes, 0);

confirmAnswer = true;
nextResponse = reply(403, { detail: 'Admin access required' });
await endAllSessions(false);
assert.equal(adminSessionsError.value, 'Admin access required');
assert.equal(isAuthenticated.value, true);

nextResponse = { ok: false, status: 500, json: async () => { throw new Error('not json'); } };
await endAllSessions(false);
assert.equal(adminSessionsError.value, 'Failed to end sessions');

nextResponse = null;
await endAllSessions(false);
assert.equal(adminSessionsError.value, 'Network error');

// None of the failures touched this browser's push channel.
assert.equal(unsubscribes, 0);
assert.deepEqual(removedKeys, []);
"""
        )
    )
