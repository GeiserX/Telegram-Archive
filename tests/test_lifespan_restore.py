"""The lifespan's session rehydration, driven for real through TestClient.

The entire lifespan body ran dark in the suite (ASGITransport and bare
TestClient(app) skip it), so a regression in the restore loop — expired rows
restored, or no_download dropped — shipped green. no_download is the sole
gate on original-file downloads for restricted share-token holders, and a
container restart runs exactly this code.
"""

import importlib
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Repo convention: web tests SKIP locally when the fastapi/pydantic versions
# mismatch (they run on CI) — but only a missing dependency may skip; any other
# import-time failure is a real regression and must fail the suite.
try:
    from fastapi.testclient import TestClient

    _CLIENT_AVAILABLE = True
except ImportError:
    _CLIENT_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _CLIENT_AVAILABLE, reason="fastapi TestClient not importable")

_BACKUP_PATH = None


def _backup_path() -> str:
    global _BACKUP_PATH
    if _BACKUP_PATH is None:
        _BACKUP_PATH = tempfile.mkdtemp(prefix="ta_test_lifespan_")
    return _BACKUP_PATH


def _session_rows(now: float, session_seconds: float) -> list[dict]:
    base = {
        "role": "viewer",
        "allowed_accounts": None,
        "allowed_chat_refs": None,
        "allowed_chat_ids": None,
        "source_token_id": None,
        "last_accessed": now,
    }
    # Both ages derive from the effective timeout, so the fixture stays
    # correct whatever AUTH_SESSION_SECONDS is configured to be.
    live_age = session_seconds / 2
    expired_age = session_seconds * 2
    return [
        # Expired: must NOT be restored.
        {**base, "token": "expired-token", "username": "old", "created_at": now - expired_age},
        # Live restricted share-token session: no_download must survive.
        {
            **base,
            "token": "live-restricted",
            "username": "guest",
            "created_at": now - live_age,
            "no_download": 1,
            "source_token_id": 7,
        },
        # Live row with an unconverted legacy grant: restores DENYING, not open.
        {
            **base,
            "token": "legacy-grant",
            "username": "legacy",
            "created_at": now - live_age,
            "allowed_chat_ids": "[1]",
        },
    ]


def _reload_main():
    env = {
        "VIEWER_USERNAME": "admin",
        "VIEWER_PASSWORD": "test@value/here",
        "ALLOW_ANONYMOUS_VIEWER": "false",
        "BACKUP_PATH": _backup_path(),
    }
    with patch.dict(os.environ, env):
        import src.web.main as main_mod

        return importlib.reload(main_mod)


def test_lifespan_restores_only_live_sessions_and_keeps_no_download():
    main_mod = _reload_main()
    now = time.time()

    adapter = AsyncMock()
    adapter.load_all_sessions.return_value = _session_rows(now, main_mod.AUTH_SESSION_SECONDS)
    adapter.get_metadata.return_value = "2026-01-01T00:00:00"  # stats already cached
    adapter.cleanup_expired_sessions.return_value = 0

    listener = MagicMock()
    listener.init = AsyncMock()
    listener.start = AsyncMock()
    listener.stop = AsyncMock()

    saved_sessions = dict(main_mod._sessions)
    try:
        with (
            patch.object(main_mod, "init_database", AsyncMock(return_value=MagicMock())),
            patch.object(main_mod, "DatabaseAdapter", MagicMock(return_value=adapter)),
            patch.object(main_mod, "get_db_manager", AsyncMock(return_value=MagicMock())),
            patch.object(main_mod, "RealtimeListener", MagicMock(return_value=listener)),
            # The real close_database closes the process-wide manager other
            # tests may hold; the restore under test never needs it.
            patch.object(main_mod, "close_database", AsyncMock()),
            patch.object(main_mod, "_normalize_display_chat_ids", AsyncMock()),
            TestClient(main_mod.app),
        ):
            sessions = dict(main_mod._sessions)
    finally:
        main_mod._sessions.clear()
        main_mod._sessions.update(saved_sessions)

    assert "expired-token" not in sessions, "expired session must not be rehydrated"
    assert "live-restricted" in sessions and "legacy-grant" in sessions

    restricted = sessions["live-restricted"]
    assert restricted.no_download is True, "share-token download restriction lost on restart"
    assert restricted.source_token_id == 7

    legacy = sessions["legacy-grant"]
    assert legacy.allowed_chat_refs == set() and legacy.allowed_accounts == set(), (
        "unconverted legacy grant must restore DENYING, never unrestricted"
    )
    assert legacy.no_download is False

    listener.init.assert_awaited_once()
    listener.start.assert_awaited_once()
