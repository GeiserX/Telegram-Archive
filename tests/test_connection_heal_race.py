"""Connection healing must be safe while a backup is mid-run — #265 follow-up.

The #265 fix made ``_start_listener`` call ``ensure_connected()``. That method
runs on the listener watchdog loop, which shares an event loop with the
APScheduler backup job, so it can now execute while a backup is suspended at an
await. Before this change ``ensure_connected`` could REPLACE
``TelegramConnection._client`` (its failure path built a brand new
``TelegramClient``) — and the backup holds the client object it was handed for
the whole run. The backup would then keep talking to a client that had been
disconnected and abandoned, while the healthy one lived somewhere else, with two
``TelegramClient`` instances on one session database.

These tests pin the two properties that make healing safe:

- IN PLACE: healing never swaps the client object out from under a holder.
- SERIALISED: two coroutines cannot be inside connect() at the same time.

plus the one case where rebuilding is mandatory (a restored session file), and
the #265 failure itself — a sender that gave up and only ``connect()`` revives.
"""

import asyncio
import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.connection import TelegramConnection


def _wire_default_account(config):
    """Mirror Config on a MagicMock: ``accounts[0]`` carries the credentials.

    v8.0.0: TelegramConnection reads the session path and API credentials from
    its AccountConfig (defaulting to ``config.accounts[0]``), never from the
    legacy config attributes — a real Config always provides that list.
    """
    account = MagicMock()
    account.index = 1
    account.label = "default"
    account.session_path = config.session_path
    account.api_id = config.api_id
    account.api_hash = config.api_hash
    config.accounts = [account]
    return config


def _shared_client():
    """A Telethon-shaped shared client that can die and be revived in place."""
    state = {"alive": True, "connects": 0, "disconnects": 0, "fail_next_connect": False, "slow_connect": False}

    client = MagicMock()

    def is_connected():
        return state["alive"]

    async def connect():
        state["connects"] += 1
        if state["slow_connect"]:
            # Yield control so a second healer can interleave if nothing stops it.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        if state["fail_next_connect"]:
            state["fail_next_connect"] = False
            raise OSError("Network is unreachable")
        state["alive"] = True

    async def disconnect():
        state["disconnects"] += 1
        state["alive"] = False

    client.is_connected = MagicMock(side_effect=is_connected)
    client.connect = AsyncMock(side_effect=connect)
    client.disconnect = AsyncMock(side_effect=disconnect)
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", phone="+10000000000"))
    client.session = MagicMock()
    client.session._conn = None
    return client, state


def _connection(tmp_path, client):
    """A real TelegramConnection that already owns ``client`` and believes it is up."""
    session_path = os.path.join(str(tmp_path), "test_session")
    # connect() copies the live session file to the golden backup on success.
    with open(session_path + ".session", "wb") as handle:
        handle.write(b"placeholder")

    config = MagicMock()
    config.session_path = session_path
    config.api_id = 12345
    config.api_hash = "test-api-hash"
    config.get_telegram_client_kwargs.return_value = {}
    _wire_default_account(config)

    conn = TelegramConnection(config)
    conn._client = client
    conn._connected = True
    return conn


def _never_built():
    """Patch target that fails loudly if a second TelegramClient is constructed."""
    return patch(
        "src.connection.TelegramClient",
        side_effect=AssertionError("healing built a new TelegramClient instead of reviving the existing one"),
    )


class TestHealingIsInPlace:
    async def test_dead_sender_is_revived_on_the_same_object(self, tmp_path):
        """The #265 state: Telethon gave up, the app-level flag still says up."""
        client, state = _shared_client()
        state["alive"] = False
        conn = _connection(tmp_path, client)

        with _never_built():
            healed = await conn.ensure_connected()

        assert healed is client
        assert conn.client is client
        assert client.is_connected()
        assert state["connects"] == 1

    async def test_failed_health_check_reconnects_the_same_object(self, tmp_path):
        """The old failure path built a new client here; now it revives this one."""
        client, state = _shared_client()
        client.is_connected = MagicMock(side_effect=Exception("socket gone"))
        conn = _connection(tmp_path, client)

        with _never_built():
            healed = await conn.ensure_connected()

        assert healed is client
        assert conn.client is client
        assert conn.is_connected is True
        assert state["connects"] == 1

    async def test_a_failed_reconnect_still_ends_on_the_same_object(self, tmp_path):
        """First connect() attempt fails, the retry inside connect() succeeds."""
        client, state = _shared_client()
        state["alive"] = False
        state["fail_next_connect"] = True
        conn = _connection(tmp_path, client)

        with _never_built():
            healed = await conn.ensure_connected()

        assert healed is client
        assert conn.client is client
        assert client.is_connected()
        assert state["connects"] == 2  # the failed attempt, then the one inside connect()

    async def test_client_identity_survives_a_disconnect_reconnect_cycle(self, tmp_path):
        """disconnect() then connect() must not mint a new object either."""
        client, _ = _shared_client()
        conn = _connection(tmp_path, client)

        with patch("src.connection.asyncio.sleep", new_callable=AsyncMock):
            await conn.disconnect()
        assert conn.client is client

        with _never_built():
            await conn.connect()

        assert conn.client is client
        assert conn.is_connected is True


class TestWatchdogHealsWhileABackupIsSuspended:
    """The interleaving itself, driven through the real scheduler."""

    def _scheduler(self, connection):
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler, _AccountRuntime

            config = MagicMock()
            config.enable_listener = True
            config.fill_gaps = False
            scheduler = BackupScheduler(config)
        account = MagicMock()
        account.index = 1
        account.label = "default"
        scheduler._accounts = [_AccountRuntime(account=account, connection=connection, row_id=1)]
        scheduler._listener_enabled = True
        return scheduler

    class _StubListener:
        @classmethod
        async def create(cls, config, client=None, *, account_id):
            listener = cls()
            listener.client = client
            return listener

        async def connect(self):
            # Mirrors the real guard in src/listener.py.
            if not self.client.is_connected():
                raise RuntimeError("Shared client is not connected")

        async def run(self):
            await asyncio.sleep(3600)

        async def _load_tracked_chats(self):
            return None

    async def test_backup_still_holds_a_live_client_after_the_watchdog_heals(self, tmp_path):
        client, state = _shared_client()
        conn = _connection(tmp_path, client)
        scheduler = self._scheduler(conn)

        # A replacement is made available on purpose: the assertions below prove
        # it is never reached. Before the fix the watchdog installed it as
        # `connection.client` and left the parked backup on the old, now
        # disconnected object.
        replacement, _ = _shared_client()
        builder = MagicMock(return_value=replacement)

        backup_started = asyncio.Event()
        watchdog_done = asyncio.Event()
        seen = {}

        async def fake_run_backup(config, client=None, account_id=None):
            seen["at_start"] = client
            backup_started.set()
            # Suspended mid-run: every await inside a real backup is a point
            # where the watchdog gets to run.
            await watchdog_done.wait()
            seen["at_resume"] = client
            seen["still_the_connections_client"] = client is conn.client
            seen["usable_at_resume"] = client.is_connected()

        with patch("src.scheduler.run_backup", fake_run_backup):
            backup_task = asyncio.create_task(scheduler._run_backup_job())
            await backup_started.wait()

            # The shared sender dies while the backup is parked, and the first
            # reconnect attempt loses too — the exact path that used to abandon
            # the client and build a new one. The listener task is dead, so the
            # watchdog restarts it: the #265 path that calls ensure_connected().
            state["alive"] = False
            state["fail_next_connect"] = True
            with (
                patch("src.listener.TelegramListener", self._StubListener),
                patch("src.connection.TelegramClient", builder),
            ):
                await scheduler._start_listener()

            watchdog_done.set()
            await backup_task

        entry = scheduler._accounts[0]
        assert entry.listener is not None
        entry.listener_task.cancel()

        assert builder.call_count == 0, "healing built a new client instead of reviving the shared one"
        assert seen["at_start"] is client
        assert seen["at_resume"] is client
        assert seen["still_the_connections_client"] is True, "the watchdog replaced the client under the backup"
        assert seen["usable_at_resume"] is True, "the backup resumed on a disconnected client"
        # The listener was started on the very same client the backup is using.
        assert entry.listener.client is client

    async def test_the_backup_never_overlaps_the_watchdogs_connect(self, tmp_path):
        """Both reach ensure_connected(); the lock keeps them out of each other."""
        client, state = _shared_client()
        state["alive"] = False
        state["slow_connect"] = True
        conn = _connection(tmp_path, client)
        scheduler = self._scheduler(conn)

        inside = {"now": 0, "max": 0}
        original_connect = client.connect.side_effect

        async def counting_connect():
            inside["now"] += 1
            inside["max"] = max(inside["max"], inside["now"])
            try:
                await original_connect()
            finally:
                inside["now"] -= 1

        client.connect = AsyncMock(side_effect=counting_connect)

        async def fake_run_backup(config, client=None, account_id=None):
            return None

        with (
            patch("src.scheduler.run_backup", fake_run_backup),
            patch("src.listener.TelegramListener", self._StubListener),
            _never_built(),
        ):
            await asyncio.gather(scheduler._run_backup_job(), scheduler._start_listener())

        if scheduler._accounts[0].listener_task:
            scheduler._accounts[0].listener_task.cancel()

        assert inside["max"] == 1, "a backup and the watchdog were inside connect() at the same time"
        # The loser of the race finds a healthy client and does not connect again.
        assert state["connects"] == 1


class TestHealingIsSerialised:
    async def test_two_healers_produce_one_connect(self, tmp_path):
        client, state = _shared_client()
        state["alive"] = False
        state["slow_connect"] = True
        conn = _connection(tmp_path, client)

        with _never_built():
            first, second = await asyncio.gather(conn.ensure_connected(), conn.ensure_connected())

        assert first is client
        assert second is client
        assert state["connects"] == 1

    async def test_teardown_cannot_interleave_with_a_heal(self, tmp_path):
        """disconnect() waits for an in-flight heal instead of racing it."""
        client, state = _shared_client()
        state["alive"] = False
        state["slow_connect"] = True
        conn = _connection(tmp_path, client)

        with patch("src.connection.asyncio.sleep", new_callable=AsyncMock), _never_built():
            heal = asyncio.create_task(conn.ensure_connected())
            await asyncio.sleep(0)
            await asyncio.gather(heal, conn.disconnect())

        assert conn.is_connected is False
        assert state["connects"] == 1
        assert state["disconnects"] == 1  # only the teardown's


class TestRebuildingIsReservedForARestoredSession:
    """The one case where reviving in place would be WRONG.

    Telethon reads ``auth_key`` once, when the session object is built, so a
    client that already exists would keep using the dead key after we restore a
    backup file over the session database. That case — and only that case —
    still constructs a fresh client.
    """

    def _session_db(self, path, auth_key):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sessions (auth_key BLOB)")
        conn.execute("INSERT INTO sessions (auth_key) VALUES (?)", (auth_key,))
        conn.commit()
        conn.close()

    async def test_authless_session_is_restored_onto_a_fresh_client(self, tmp_path):
        session_path = os.path.join(str(tmp_path), "test_session")
        self._session_db(session_path + ".session", None)
        self._session_db(session_path + ".session.authenticated", b"\x01\x02\x03")

        old_client, old_state = _shared_client()
        config = MagicMock()
        config.session_path = session_path
        config.api_id = 12345
        config.api_hash = "test-api-hash"
        config.get_telegram_client_kwargs.return_value = {}
        _wire_default_account(config)
        conn = TelegramConnection(config)
        conn._client = old_client
        conn._connected = False

        new_client, _ = _shared_client()
        with patch("src.connection.TelegramClient", return_value=new_client):
            result = await conn.connect()

        assert result is new_client
        assert conn.client is new_client
        assert old_state["disconnects"] == 1, "the stale client must be torn down, not leaked"

        # The restored auth_key is on disk for the fresh client to read.
        probe = sqlite3.connect(session_path + ".session")
        row = probe.execute("SELECT auth_key FROM sessions LIMIT 1").fetchone()
        probe.close()
        assert row[0] == b"\x01\x02\x03"


class TestTheOriginalFailureStillHeals:
    """#265 must still be fixed: only connect() revives a sender that gave up."""

    def _dead_telethon_client(self) -> TelegramClient:
        client = TelegramClient(MemorySession(), 1, "test-api-hash")
        client._sender._user_connected = False
        client._sender._connection = None
        return client

    async def test_start_listener_revives_a_really_dead_sender_in_place(self, tmp_path):
        client = self._dead_telethon_client()
        revived = {"n": 0}

        async def fake_connect():
            revived["n"] += 1
            client._sender._user_connected = True

        client.connect = fake_connect
        client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Test", phone="+10000000000"))

        conn = _connection(tmp_path, client)
        assert conn.is_connected is True  # stale app-level flag
        assert not client.is_connected()  # real state

        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler, _AccountRuntime

            config = MagicMock()
            config.enable_listener = True
            config.fill_gaps = False
            scheduler = BackupScheduler(config)
        account = MagicMock()
        account.index = 1
        account.label = "default"
        scheduler._accounts = [_AccountRuntime(account=account, connection=conn, row_id=1)]

        seen = {}

        class StubListener:
            @classmethod
            async def create(cls, config, client=None, *, account_id):
                listener = cls()
                listener.client = client
                return listener

            async def connect(self):
                seen["connected_at_connect"] = self.client.is_connected()
                if not self.client.is_connected():
                    raise RuntimeError("Shared client is not connected")

            async def run(self):
                await asyncio.sleep(3600)

        with patch("src.listener.TelegramListener", StubListener), _never_built():
            await scheduler._start_listener()

        assert revived["n"] == 1
        assert seen["connected_at_connect"] is True
        entry = scheduler._accounts[0]
        assert entry.listener.client is client, "the listener must share the connection's own client"
        assert conn.client is client
        entry.listener_task.cancel()


@pytest.mark.asyncio
async def test_client_property_is_stable_across_every_heal(tmp_path):
    """Belt and braces: whatever happens, `connection.client` keeps its identity."""
    client, state = _shared_client()
    conn = _connection(tmp_path, client)
    identities = set()

    for scenario in ("healthy", "dead", "connect fails once", "check raises"):
        if scenario == "dead":
            state["alive"] = False
        elif scenario == "connect fails once":
            state["alive"] = False
            state["fail_next_connect"] = True
        elif scenario == "check raises":
            client.is_connected = MagicMock(side_effect=[Exception("boom"), True, True])

        with _never_built():
            identities.add(id(await conn.ensure_connected()))

    assert identities == {id(client)}
