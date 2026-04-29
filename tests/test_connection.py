"""Tests for TelegramConnection shared client connection manager."""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError

from src import connection
from src.connection import TelegramConnection


@pytest.mark.asyncio
async def test_connection_call_with_flood_retry_aborts_excessive_wait():
    """Shared connection retry helper must fail fast on excessive FloodWaits."""
    sleeps: list[float] = []

    async def huge_wait():
        raise FloodWaitError(request=None, capture=86400)

    async def record_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch.object(connection, "MAX_FLOOD_WAIT_SECONDS", 30),
        patch.object(connection.asyncio, "sleep", record_sleep),
        pytest.raises(FloodWaitError),
    ):
        await connection._call_with_flood_retry(huge_wait)

    assert sleeps == []


class TestTelegramConnectionInit(unittest.TestCase):
    """Test TelegramConnection initialization."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_sets_config_and_defaults(self):
        """Initialization stores config and sets connected state to False."""
        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            from src.config import Config

            config = Config()
            conn = TelegramConnection(config)

            assert conn.config is config
            assert conn._client is None
            assert conn._connected is False
            assert conn._me is None

    def test_init_calls_validate_credentials(self):
        """Initialization calls config.validate_credentials()."""
        config = MagicMock()
        config.validate_credentials = MagicMock()
        TelegramConnection(config)
        config.validate_credentials.assert_called_once()


class TestTelegramConnectionProperties(unittest.TestCase):
    """Test TelegramConnection property accessors."""

    def test_client_property_returns_none_initially(self):
        """client property returns None before connection."""
        config = MagicMock()
        conn = TelegramConnection(config)
        assert conn.client is None

    def test_client_property_returns_client_when_set(self):
        """client property returns the client instance after assignment."""
        config = MagicMock()
        conn = TelegramConnection(config)
        mock_client = MagicMock()
        conn._client = mock_client
        assert conn.client is mock_client

    def test_is_connected_returns_false_initially(self):
        """is_connected returns False before connection."""
        config = MagicMock()
        conn = TelegramConnection(config)
        assert conn.is_connected is False

    def test_is_connected_returns_true_when_connected(self):
        """is_connected returns True when _connected is True and client exists."""
        config = MagicMock()
        conn = TelegramConnection(config)
        conn._connected = True
        conn._client = MagicMock()
        assert conn.is_connected is True

    def test_is_connected_returns_false_when_no_client(self):
        """is_connected returns False when _connected is True but client is None."""
        config = MagicMock()
        conn = TelegramConnection(config)
        conn._connected = True
        conn._client = None
        assert conn.is_connected is False

    def test_me_property_returns_none_initially(self):
        """me property returns None before connection."""
        config = MagicMock()
        conn = TelegramConnection(config)
        assert conn.me is None


class TestSessionHasAuth(unittest.TestCase):
    """Test _session_has_auth static method."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_returns_false_for_nonexistent_file(self):
        """Returns False when session file does not exist."""
        result = TelegramConnection._session_has_auth("/nonexistent/path.session")
        assert result is False

    def test_returns_false_for_empty_file(self):
        """Returns False when session file is empty (0 bytes)."""
        empty_file = os.path.join(self.temp_dir, "empty.session")
        with open(empty_file, "w"):
            pass
        result = TelegramConnection._session_has_auth(empty_file)
        assert result is False

    def test_returns_true_for_valid_session(self):
        """Returns True when session DB has a non-empty auth_key."""
        session_file = os.path.join(self.temp_dir, "valid.session")
        conn = sqlite3.connect(session_file)
        conn.execute("CREATE TABLE sessions (auth_key BLOB)")
        conn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (b"\x01\x02\x03",))
        conn.commit()
        conn.close()

        result = TelegramConnection._session_has_auth(session_file)
        assert result is True

    def test_returns_false_for_null_auth_key(self):
        """Returns False when auth_key is NULL."""
        session_file = os.path.join(self.temp_dir, "null_auth.session")
        conn = sqlite3.connect(session_file)
        conn.execute("CREATE TABLE sessions (auth_key BLOB)")
        conn.execute("INSERT INTO sessions (auth_key) VALUES (NULL)")
        conn.commit()
        conn.close()

        result = TelegramConnection._session_has_auth(session_file)
        assert not result

    def test_returns_false_for_empty_auth_key(self):
        """Returns False when auth_key is an empty blob."""
        session_file = os.path.join(self.temp_dir, "empty_auth.session")
        conn = sqlite3.connect(session_file)
        conn.execute("CREATE TABLE sessions (auth_key BLOB)")
        conn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (b"",))
        conn.commit()
        conn.close()

        result = TelegramConnection._session_has_auth(session_file)
        assert not result

    def test_returns_false_for_corrupt_db(self):
        """Returns False when session file is not a valid SQLite database."""
        corrupt_file = os.path.join(self.temp_dir, "corrupt.session")
        with open(corrupt_file, "w") as f:
            f.write("not a database")

        result = TelegramConnection._session_has_auth(corrupt_file)
        assert result is False

    def test_returns_false_for_missing_table(self):
        """Returns False when DB exists but has no sessions table."""
        session_file = os.path.join(self.temp_dir, "notable.session")
        conn = sqlite3.connect(session_file)
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()

        result = TelegramConnection._session_has_auth(session_file)
        assert result is False


@pytest.mark.asyncio
async def test_connect_returns_existing_client_when_already_connected():
    """connect() returns existing client without reconnecting when already connected."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = MagicMock()
    conn._connected = True
    conn._client = mock_client

    result = await conn.connect()
    assert result is mock_client


@pytest.mark.asyncio
async def test_connect_creates_client_and_authenticates():
    """connect() creates a TelegramClient, connects, and verifies auth."""
    temp_dir = tempfile.mkdtemp()
    try:
        session_path = os.path.join(temp_dir, "test_session")
        session_file = session_path + ".session"
        # Create session file on disk so shutil.copy2 succeeds after auth
        with open(session_file, "wb") as f:
            f.write(b"placeholder")

        config = MagicMock()
        config.session_path = session_path
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(first_name="Test", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.session = MagicMock()
        mock_client.session._conn = None

        with patch("src.connection.TelegramClient", return_value=mock_client):
            result = await conn.connect()

        assert result is mock_client
        assert conn._connected is True
        assert conn._me is mock_me
        mock_client.connect.assert_awaited_once()
        mock_client.is_user_authorized.assert_awaited_once()
        mock_client.get_me.assert_awaited_once()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_connect_restores_golden_backup_when_session_has_no_auth():
    """connect() restores from golden backup when live session lost auth."""
    temp_dir = tempfile.mkdtemp()
    try:
        session_file = os.path.join(temp_dir, "test_session.session")
        golden_file = os.path.join(temp_dir, "test_session.session.authenticated")

        # Create golden backup with valid auth
        gconn = sqlite3.connect(golden_file)
        gconn.execute("CREATE TABLE sessions (auth_key BLOB)")
        gconn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (b"\x01\x02\x03",))
        gconn.commit()
        gconn.close()

        # Create empty session file (no auth)
        sconn = sqlite3.connect(session_file)
        sconn.execute("CREATE TABLE sessions (auth_key BLOB)")
        sconn.execute("INSERT INTO sessions (auth_key) VALUES (NULL)")
        sconn.commit()
        sconn.close()

        config = MagicMock()
        config.session_path = os.path.join(temp_dir, "test_session")
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(first_name="Test", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.session = MagicMock()
        mock_client.session._conn = None

        with patch("src.connection.TelegramClient", return_value=mock_client):
            await conn.connect()

        # Verify session was restored from golden backup
        rconn = sqlite3.connect(session_file)
        cur = rconn.cursor()
        cur.execute("SELECT auth_key FROM sessions LIMIT 1")
        row = cur.fetchone()
        rconn.close()
        assert row is not None and row[0] == b"\x01\x02\x03"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_connect_creates_snapshot_before_connecting():
    """connect() snapshots the session file before TelegramClient modifies it."""
    temp_dir = tempfile.mkdtemp()
    try:
        session_file = os.path.join(temp_dir, "test_session.session")
        snapshot_file = os.path.join(temp_dir, "test_session.session.bak")

        # Create session with valid auth
        sconn = sqlite3.connect(session_file)
        sconn.execute("CREATE TABLE sessions (auth_key BLOB)")
        sconn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (b"\xaa\xbb",))
        sconn.commit()
        sconn.close()

        config = MagicMock()
        config.session_path = os.path.join(temp_dir, "test_session")
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(first_name="Test", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.session = MagicMock()
        mock_client.session._conn = None

        with patch("src.connection.TelegramClient", return_value=mock_client):
            await conn.connect()

        # Snapshot should exist
        assert os.path.isfile(snapshot_file)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_connect_raises_when_not_authorized():
    """connect() raises RuntimeError when session is not authorized."""
    temp_dir = tempfile.mkdtemp()
    try:
        config = MagicMock()
        config.session_path = os.path.join(temp_dir, "test_session")
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)

        with (
            patch("src.connection.TelegramClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="Session not authorized"),
        ):
            await conn.connect()

        mock_client.disconnect.assert_awaited_once()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_connect_restores_from_backup_on_auth_failure():
    """connect() restores session from golden/snapshot backup when auth fails."""
    temp_dir = tempfile.mkdtemp()
    try:
        session_file = os.path.join(temp_dir, "test_session.session")
        golden_file = os.path.join(temp_dir, "test_session.session.authenticated")

        # Create golden backup
        gconn = sqlite3.connect(golden_file)
        gconn.execute("CREATE TABLE sessions (auth_key BLOB)")
        gconn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (b"\x01\x02",))
        gconn.commit()
        gconn.close()

        # Create live session with auth (so it passes the pre-connect check)
        sconn = sqlite3.connect(session_file)
        sconn.execute("CREATE TABLE sessions (auth_key BLOB)")
        sconn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (b"\xaa",))
        sconn.commit()
        sconn.close()

        config = MagicMock()
        config.session_path = os.path.join(temp_dir, "test_session")
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)

        with (
            patch("src.connection.TelegramClient", return_value=mock_client),
            pytest.raises(RuntimeError),
        ):
            await conn.connect()

        # Session should be restored from golden backup
        rconn = sqlite3.connect(session_file)
        cur = rconn.cursor()
        cur.execute("SELECT auth_key FROM sessions LIMIT 1")
        row = cur.fetchone()
        rconn.close()
        assert row[0] == b"\x01\x02"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_connect_flushes_wal_on_success():
    """connect() flushes WAL checkpoint after successful auth."""
    temp_dir = tempfile.mkdtemp()
    try:
        session_path = os.path.join(temp_dir, "test_session")
        session_file = session_path + ".session"
        with open(session_file, "wb") as f:
            f.write(b"placeholder")

        config = MagicMock()
        config.session_path = session_path
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_session_conn = MagicMock()
        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(first_name="Test", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.session = MagicMock()
        mock_client.session._conn = mock_session_conn

        with patch("src.connection.TelegramClient", return_value=mock_client):
            await conn.connect()

        mock_session_conn.execute.assert_any_call("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_connect_wal_flush_exception_is_suppressed():
    """connect() suppresses exceptions during WAL flush."""
    temp_dir = tempfile.mkdtemp()
    try:
        session_path = os.path.join(temp_dir, "test_session")
        session_file = session_path + ".session"
        with open(session_file, "wb") as f:
            f.write(b"placeholder")

        config = MagicMock()
        config.session_path = session_path
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        conn = TelegramConnection(config)

        mock_session_conn = MagicMock()
        mock_session_conn.execute.side_effect = Exception("WAL error")
        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(first_name="Test", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.session = MagicMock()
        mock_client.session._conn = mock_session_conn

        with patch("src.connection.TelegramClient", return_value=mock_client):
            # Should not raise despite WAL error
            result = await conn.connect()
            assert result is mock_client
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class TestEnableWalMode(unittest.TestCase):
    """Test _enable_wal_mode method."""

    def test_enables_wal_and_busy_timeout(self):
        """Sets WAL journal mode and busy timeout when session._conn exists."""
        config = MagicMock()
        conn = TelegramConnection(config)

        mock_session_conn = MagicMock()
        mock_client = MagicMock()
        mock_client.session._conn = mock_session_conn
        conn._client = mock_client

        conn._enable_wal_mode()

        mock_session_conn.execute.assert_any_call("PRAGMA journal_mode=WAL")
        mock_session_conn.execute.assert_any_call("PRAGMA busy_timeout=30000")

    def test_handles_no_conn_attribute(self):
        """Does not raise when session has no _conn attribute."""
        config = MagicMock()
        conn = TelegramConnection(config)

        mock_client = MagicMock()
        mock_client.session = MagicMock(spec=[])  # No _conn attribute
        conn._client = mock_client

        # Should not raise
        conn._enable_wal_mode()

    def test_handles_none_conn(self):
        """Does not raise when session._conn is None."""
        config = MagicMock()
        conn = TelegramConnection(config)

        mock_client = MagicMock()
        mock_client.session._conn = None
        conn._client = mock_client

        # Should not raise
        conn._enable_wal_mode()

    def test_catches_exception_from_execute(self):
        """Logs warning but does not raise when PRAGMA execution fails."""
        config = MagicMock()
        conn = TelegramConnection(config)

        mock_session_conn = MagicMock()
        mock_session_conn.execute.side_effect = Exception("DB locked")
        mock_client = MagicMock()
        mock_client.session._conn = mock_session_conn
        conn._client = mock_client

        # Should not raise
        conn._enable_wal_mode()


@pytest.mark.asyncio
async def test_disconnect_when_connected():
    """disconnect() calls client.disconnect and resets state."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = AsyncMock()
    conn._client = mock_client
    conn._connected = True

    with patch("src.connection.asyncio.sleep", new_callable=AsyncMock):
        await conn.disconnect()

    mock_client.disconnect.assert_awaited_once()
    assert conn._connected is False


@pytest.mark.asyncio
async def test_disconnect_when_not_connected():
    """disconnect() does nothing when not connected."""
    config = MagicMock()
    conn = TelegramConnection(config)
    conn._client = None
    conn._connected = False

    await conn.disconnect()
    # No error, nothing to assert except it completed


@pytest.mark.asyncio
async def test_disconnect_handles_exception():
    """disconnect() catches exceptions and still resets connected state."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = AsyncMock()
    mock_client.disconnect.side_effect = Exception("Network error")
    conn._client = mock_client
    conn._connected = True

    with patch("src.connection.asyncio.sleep", new_callable=AsyncMock):
        await conn.disconnect()

    assert conn._connected is False


@pytest.mark.asyncio
async def test_ensure_connected_calls_connect_when_not_connected():
    """ensure_connected() calls connect() when not connected."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = AsyncMock()

    with patch.object(conn, "connect", new_callable=AsyncMock, return_value=mock_client) as mock_connect:
        result = await conn.ensure_connected()

    mock_connect.assert_awaited_once()
    assert result is mock_client


@pytest.mark.asyncio
async def test_ensure_connected_checks_alive_connection():
    """ensure_connected() checks if existing connection is alive."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = MagicMock()
    mock_client.is_connected = MagicMock(return_value=True)
    conn._client = mock_client
    conn._connected = True

    result = await conn.ensure_connected()
    assert result is mock_client
    mock_client.is_connected.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_connected_reconnects_when_connection_lost():
    """ensure_connected() reconnects when client.is_connected() returns False."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=False)
    mock_me = MagicMock(first_name="Test")
    mock_client.get_me = AsyncMock(return_value=mock_me)
    conn._client = mock_client
    conn._connected = True

    result = await conn.ensure_connected()
    assert result is mock_client
    mock_client.connect.assert_awaited_once()
    mock_client.get_me.assert_awaited_once()
    assert conn._me is mock_me


@pytest.mark.asyncio
async def test_ensure_connected_reconnects_on_check_failure():
    """ensure_connected() calls connect() when connection check raises."""
    config = MagicMock()
    conn = TelegramConnection(config)
    mock_client = MagicMock()
    mock_client.is_connected.side_effect = Exception("Connection error")
    conn._client = mock_client
    conn._connected = True

    mock_new_client = AsyncMock()
    with patch.object(conn, "connect", new_callable=AsyncMock, return_value=mock_new_client):
        result = await conn.ensure_connected()

    assert result is mock_client  # Returns self._client at end


@pytest.mark.asyncio
async def test_async_context_manager_enter():
    """__aenter__ calls connect and returns self."""
    config = MagicMock()
    conn = TelegramConnection(config)

    with patch.object(conn, "connect", new_callable=AsyncMock) as mock_connect:
        result = await conn.__aenter__()

    mock_connect.assert_awaited_once()
    assert result is conn


@pytest.mark.asyncio
async def test_async_context_manager_exit():
    """__aexit__ calls disconnect."""
    config = MagicMock()
    conn = TelegramConnection(config)

    with patch.object(conn, "disconnect", new_callable=AsyncMock) as mock_disconnect:
        await conn.__aexit__(None, None, None)

    mock_disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
