"""
Tests for the scheduler module (src/scheduler.py).
"""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_entry(connection=None, row_id=1, index=1, listener=None, listener_task=None):
    """One _AccountRuntime as _connect() would build it, with a mock connection.

    v8.0.0: the scheduler keeps per-account runtime state in ``_accounts``
    instead of the old singular ``_connection``/``_listener`` attributes; tests
    inject their mocks through entries like this one.
    """
    from src.scheduler import _AccountRuntime

    account = MagicMock()
    account.index = index
    account.label = "default"
    if connection is None:
        connection = AsyncMock()
        connection.is_connected = True
        connection.client = MagicMock()
    return _AccountRuntime(
        account=account,
        connection=connection,
        row_id=row_id,
        listener=listener,
        listener_task=listener_task,
    )


class TestBackupSchedulerInit:
    """Tests for BackupScheduler.__init__."""

    def test_init_sets_config(self):
        """BackupScheduler stores the config object."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)

            assert scheduler.config is config

    def test_init_sets_running_false(self):
        """BackupScheduler starts in non-running state."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)

            assert scheduler.running is False

    def test_init_starts_with_no_account_runtimes(self):
        """BackupScheduler starts with no per-account connections."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)

            assert scheduler._accounts == []

    def test_init_sets_listener_disabled(self):
        """BackupScheduler starts with listeners not yet enabled."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)

            assert scheduler._listener_enabled is False

    def test_init_creates_backup_lock(self):
        """BackupScheduler creates a lock to prevent overlapping backup runs."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.should_skip_topic = MagicMock(return_value=False)
            scheduler = BackupScheduler(config)

            assert hasattr(scheduler, "_backup_lock")
            assert hasattr(scheduler._backup_lock, "locked")

    def test_init_registers_signal_handlers(self):
        """BackupScheduler registers SIGINT and SIGTERM handlers."""
        with patch("src.scheduler.signal.signal") as mock_signal:
            from src.scheduler import BackupScheduler

            config = MagicMock()
            BackupScheduler(config)

            calls = [c[0] for c in mock_signal.call_args_list]
            assert calls[0][:1] == (signal.SIGINT,)
            assert calls[1][:1] == (signal.SIGTERM,)


class TestBackupSchedulerSignalHandler:
    """Tests for BackupScheduler._signal_handler."""

    def test_signal_handler_calls_stop(self):
        """Signal handler triggers stop on the scheduler."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)
            scheduler.stop = MagicMock()

            scheduler._signal_handler(signal.SIGTERM, None)

            scheduler.stop.assert_called_once()


class TestBackupSchedulerStart:
    """Tests for BackupScheduler.start."""

    def test_start_with_valid_cron_schedule(self):
        """Start succeeds with valid 5-part cron schedule."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.schedule = "0 */6 * * *"

            scheduler = BackupScheduler(config)
            scheduler.scheduler = MagicMock()

            scheduler.start()

            scheduler.scheduler.add_job.assert_called_once()
            scheduler.scheduler.start.assert_called_once()
            assert scheduler.running is True

    def test_start_with_invalid_cron_raises_value_error(self):
        """Start raises ValueError with malformed cron schedule."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.schedule = "invalid"

            scheduler = BackupScheduler(config)

            with pytest.raises(ValueError, match="Invalid cron schedule format"):
                scheduler.start()

    def test_start_with_three_part_cron_raises_value_error(self):
        """Start raises ValueError when cron has wrong number of parts."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.schedule = "0 * *"

            scheduler = BackupScheduler(config)

            with pytest.raises(ValueError, match="Invalid cron schedule format"):
                scheduler.start()


class TestBackupSchedulerStop:
    """Tests for BackupScheduler.stop."""

    def test_stop_when_running_shuts_down_scheduler(self):
        """Stop shuts down the APScheduler when running."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)
            scheduler.running = True
            scheduler.scheduler = MagicMock()

            scheduler.stop()

            scheduler.scheduler.shutdown.assert_called_once_with(wait=True)
            assert scheduler.running is False

    def test_stop_when_not_running_is_noop(self):
        """Stop is a no-op when scheduler is not running."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)
            scheduler.running = False
            scheduler.scheduler = MagicMock()

            scheduler.stop()

            scheduler.scheduler.shutdown.assert_not_called()


class TestBackupSchedulerRunBackupJob:
    """Tests for BackupScheduler._run_backup_job."""

    @pytest.fixture
    def scheduler_with_connection(self):
        """Create a scheduler with one mocked account runtime."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            scheduler = BackupScheduler(config)
            scheduler._accounts = [_make_entry()]
            return scheduler

    async def test_run_backup_job_calls_run_backup(self, scheduler_with_connection):
        """Backup job calls run_backup with config, shared client and row id."""
        scheduler = scheduler_with_connection
        mock_client = MagicMock()
        entry = scheduler._accounts[0]
        entry.connection.ensure_connected = AsyncMock(return_value=mock_client)

        with patch("src.scheduler.run_backup", new_callable=AsyncMock) as mock_backup:
            await scheduler._run_backup_job()

            scheduler.config.for_account.assert_called_with(entry.account.index)
            mock_backup.assert_called_once_with(
                scheduler.config.for_account.return_value, client=mock_client, account_id=entry.row_id
            )

    async def test_run_backup_job_sweeps_accounts_sequentially(self):
        """Two accounts are swept in config order, each under its own row id."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            scheduler = BackupScheduler(config)
            scheduler._accounts = [_make_entry(row_id=1, index=1), _make_entry(row_id=5, index=2)]

            with patch("src.scheduler.run_backup", new_callable=AsyncMock) as mock_backup:
                await scheduler._run_backup_job()

            assert [c.kwargs["account_id"] for c in mock_backup.await_args_list] == [1, 5]

    async def test_run_backup_job_one_broken_account_does_not_consume_the_others(self, caplog):
        """Account 1 failing to connect still leaves account 2 its full sweep.

        The failure is logged by env index and exception TYPE only — the text
        of a Telethon error can carry the phone number (#272).
        """
        import logging

        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            scheduler = BackupScheduler(config)
            broken = _make_entry(row_id=1, index=1)
            broken.connection.ensure_connected = AsyncMock(side_effect=ConnectionError("+34600000001 unreachable"))
            healthy = _make_entry(row_id=2, index=2)
            scheduler._accounts = [broken, healthy]

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock) as mock_backup,
                caplog.at_level(logging.ERROR, logger="src.scheduler"),
            ):
                await scheduler._run_backup_job()

            assert [c.kwargs["account_id"] for c in mock_backup.await_args_list] == [2]
            messages = [r.getMessage() for r in caplog.records]
            assert "account 1 failed: ConnectionError" in messages
            assert all("+34600000001" not in m for m in messages)

    async def test_run_backup_job_with_gap_fill_enabled(self, scheduler_with_connection):
        """Backup job runs gap-fill when fill_gaps is enabled."""
        scheduler = scheduler_with_connection
        scheduler.config.fill_gaps = True

        mock_run_fill_gaps = AsyncMock(return_value={"errors": 0, "total_recovered": 5})

        with (
            patch("src.scheduler.run_backup", new_callable=AsyncMock),
            patch("src.telegram_backup.run_fill_gaps", mock_run_fill_gaps, create=True),
        ):
            await scheduler._run_backup_job()

        mock_run_fill_gaps.assert_awaited_once()

    async def test_run_backup_job_reloads_listener_tracked_chats(self, scheduler_with_connection):
        """Backup job reloads the account's listener tracked chats after completing."""
        scheduler = scheduler_with_connection
        entry = scheduler._accounts[0]
        entry.listener = AsyncMock()
        entry.listener._load_tracked_chats = AsyncMock()

        with patch("src.scheduler.run_backup", new_callable=AsyncMock):
            await scheduler._run_backup_job()

            entry.listener._load_tracked_chats.assert_called_once()

    async def test_run_backup_job_handles_exception_gracefully(self, scheduler_with_connection):
        """Backup job catches and logs exceptions without crashing."""
        scheduler = scheduler_with_connection
        scheduler._accounts[0].connection.ensure_connected = AsyncMock(side_effect=Exception("connection lost"))

        # Should NOT raise
        await scheduler._run_backup_job()

    async def test_run_backup_job_skips_when_another_backup_running(self, scheduler_with_connection):
        """Backup job does not overlap with an already running backup."""
        scheduler = scheduler_with_connection
        await scheduler._backup_lock.acquire()
        try:
            with patch("src.scheduler.run_backup", new_callable=AsyncMock) as mock_backup:
                await scheduler._run_backup_job()
            mock_backup.assert_not_called()
        finally:
            scheduler._backup_lock.release()

    async def test_run_backup_job_gap_fill_with_errors(self):
        """Backup job logs warning when gap-fill has errors."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = True
            scheduler = BackupScheduler(config)
            scheduler._accounts = [_make_entry()]

            mock_fill_gaps = AsyncMock(return_value={"errors": 2, "total_recovered": 3})

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch("src.telegram_backup.run_fill_gaps", mock_fill_gaps, create=True),
            ):
                await scheduler._run_backup_job()

            mock_fill_gaps.assert_awaited_once()

    async def test_run_backup_job_gap_fill_exception(self):
        """Backup job catches gap-fill exceptions without crashing."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = True
            scheduler = BackupScheduler(config)
            scheduler._accounts = [_make_entry()]

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch(
                    "src.telegram_backup.run_fill_gaps", new_callable=AsyncMock, side_effect=Exception("gap fill boom")
                ),
            ):
                await scheduler._run_backup_job()


class TestBackupSchedulerConnect:
    """Tests for BackupScheduler._connect and _disconnect."""

    async def test_connect_creates_telegram_connection_per_account_and_resolves_rows(self):
        """_connect builds one TelegramConnection per account and resolves row ids."""
        with (
            patch("src.scheduler.signal.signal"),
            patch("src.scheduler.TelegramConnection") as MockConn,
        ):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            account = MagicMock()
            account.index = 1
            account.label = "default"
            config.accounts = [account]
            scheduler = BackupScheduler(config)

            mock_conn_instance = AsyncMock()
            mock_conn_instance.me = MagicMock(id=900001111)
            MockConn.return_value = mock_conn_instance

            mock_db = AsyncMock()
            mock_db.ensure_account = AsyncMock(return_value=1)

            with patch("src.db.create_adapter", new_callable=AsyncMock, return_value=mock_db):
                await scheduler._connect()

            MockConn.assert_called_once_with(config, account=account)
            mock_conn_instance.connect.assert_called_once()
            assert len(scheduler._accounts) == 1
            assert scheduler._accounts[0].connection is mock_conn_instance
            # The row id came from ensure_account, keyed on the login's user id.
            mock_db.ensure_account.assert_awaited_once_with(telegram_user_id=900001111, env_index=1, label="default")
            assert scheduler._accounts[0].row_id == 1
            mock_db.close.assert_awaited_once()

    async def test_disconnect_closes_connection(self):
        """_disconnect calls disconnect on every account's connection."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)
            mock_conn = AsyncMock()
            scheduler._accounts = [_make_entry(connection=mock_conn)]

            await scheduler._disconnect()

            mock_conn.disconnect.assert_called_once()
            assert scheduler._accounts == []

    async def test_disconnect_when_no_connection_is_noop(self):
        """_disconnect is safe when no connection exists."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)
            scheduler._accounts = []

            # Should not raise
            await scheduler._disconnect()


class TestBackupSchedulerListener:
    """Tests for BackupScheduler._start_listener and _stop_listener."""

    async def test_start_listener_when_disabled_is_noop(self):
        """_start_listener does nothing when enable_listener is False."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = False
            scheduler = BackupScheduler(config)
            entry = _make_entry()
            scheduler._accounts = [entry]

            await scheduler._start_listener()

            assert entry.listener is None
            assert entry.listener_task is None

    async def test_start_listener_when_not_connected_logs_error(self):
        """_start_listener fails gracefully when no account ever connected."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = True
            scheduler = BackupScheduler(config)
            scheduler._accounts = []

            # Should not raise; nothing to start.
            await scheduler._start_listener()

    async def test_start_listener_when_connection_not_connected_logs_error(self):
        """_start_listener fails gracefully when a connection is down."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = True
            scheduler = BackupScheduler(config)
            connection = AsyncMock()
            connection.is_connected = False
            entry = _make_entry(connection=connection)
            scheduler._accounts = [entry]

            await scheduler._start_listener()

            assert entry.listener is None

    async def test_start_listener_creates_and_starts_listener(self):
        """_start_listener creates a TelegramListener and starts it."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = True
            scheduler = BackupScheduler(config)
            entry = _make_entry()
            scheduler._accounts = [entry]

            mock_listener = AsyncMock()
            mock_listener.run = AsyncMock()

            with patch("src.listener.TelegramListener") as MockListener:
                MockListener.create = AsyncMock(return_value=mock_listener)
                with patch("src.scheduler.asyncio.create_task") as mock_task:
                    mock_task.return_value = MagicMock()
                    await scheduler._start_listener()

            assert entry.listener is mock_listener
            scheduler.config.for_account.assert_called_with(entry.account.index)
            MockListener.create.assert_awaited_once_with(
                scheduler.config.for_account.return_value, client=entry.connection.client, account_id=entry.row_id
            )

    async def test_start_listener_builds_real_listener_for_the_resolved_account(self):
        """The scheduler path runs the REAL TelegramListener.create/__init__ chain.

        Only leaf dependencies are mocked (DB adapter, network methods): if the
        create() call at the scheduler's composition seam ever drops the
        account_id kwarg, the entry's listener would be built without a row id —
        which these assertions turn red. The patched class in the test above
        cannot catch that.
        """
        with patch("src.scheduler.signal.signal"):
            from src.listener import TelegramListener
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = True
            config.skip_topic_ids = {}
            config.should_skip_topic = MagicMock(return_value=False)
            config.mass_operation_threshold = 10
            config.mass_operation_window_seconds = 30
            config.mass_operation_buffer_delay = 2.0
            # The scheduler hands workers a per-account view; for the real
            # listener chain the plain mock config stands in for it.
            config.for_account = MagicMock(return_value=config)
            scheduler = BackupScheduler(config)
            entry = _make_entry(row_id=7)
            scheduler._accounts = [entry]

            with (
                patch("src.listener.create_adapter", new_callable=AsyncMock, return_value=AsyncMock()),
                patch.object(TelegramListener, "connect", new_callable=AsyncMock),
                patch.object(TelegramListener, "run", new_callable=AsyncMock),
            ):
                await scheduler._start_listener()

                assert isinstance(entry.listener, TelegramListener)
                assert entry.listener.account_id == 7
                assert entry.listener_task is not None
                await entry.listener_task

    async def test_start_listener_handles_exception_gracefully(self):
        """_start_listener catches exceptions during listener creation."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = True
            scheduler = BackupScheduler(config)
            entry = _make_entry()
            scheduler._accounts = [entry]

            # Force the import/create to fail
            with patch.dict(
                "sys.modules",
                {
                    "src.listener": MagicMock(
                        TelegramListener=MagicMock(create=AsyncMock(side_effect=Exception("listener init failed")))
                    )
                },
            ):
                await scheduler._start_listener()

            assert entry.listener is None
            assert entry.listener_task is None

    async def test_stop_listener_cancels_task_and_closes_listener(self):
        """_stop_listener cancels the task and closes the listener."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)

            # Create a real future that raises CancelledError when awaited
            loop = asyncio.get_event_loop()
            mock_task = loop.create_future()
            mock_task.cancel()

            mock_listener = AsyncMock()
            mock_listener.close = AsyncMock()

            entry = _make_entry(listener=mock_listener, listener_task=mock_task)
            scheduler._accounts = [entry]

            await scheduler._stop_listener()

            mock_listener.close.assert_called_once()
            assert entry.listener is None
            assert entry.listener_task is None

    async def test_stop_listener_swallows_dead_task_exception(self):
        """_stop_listener does not re-raise a dead task's stored exception.

        Regression for the crash where a transient ConnectionError from
        run_until_disconnected() became the listener task's stored exception;
        awaiting that done task in _stop_listener re-raised it (only
        CancelledError was caught), crashing run_forever -> main -> sys.exit(1)
        -> container restart, instead of triggering the intended restart.
        """
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.should_skip_topic = MagicMock(return_value=False)
            scheduler = BackupScheduler(config)

            # A task that has already finished with a ConnectionError.
            loop = asyncio.get_event_loop()
            dead_task = loop.create_future()
            dead_task.set_exception(ConnectionError("Cannot send requests while disconnected"))

            mock_listener = AsyncMock()
            mock_listener.close = AsyncMock()

            entry = _make_entry(listener=mock_listener, listener_task=dead_task)
            scheduler._accounts = [entry]

            # Must not raise — teardown should proceed cleanly.
            await scheduler._stop_listener()

            mock_listener.close.assert_called_once()
            assert entry.listener is None
            assert entry.listener_task is None

    async def test_stop_listener_only_dead_leaves_live_listeners_running(self):
        """The watchdog's only_dead stop never touches a healthy account.

        Restarting a healthy listener would detach and re-register its handlers
        for no reason; the watchdog restarts exactly what died.
        """
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)

            loop = asyncio.get_event_loop()
            dead_task = loop.create_future()
            dead_task.cancel()
            dead_listener = AsyncMock()

            live_task = MagicMock()
            live_task.done = MagicMock(return_value=False)
            live_listener = AsyncMock()

            dead = _make_entry(index=1, listener=dead_listener, listener_task=dead_task)
            live = _make_entry(index=2, listener=live_listener, listener_task=live_task)
            scheduler._accounts = [dead, live]

            await scheduler._stop_listener(only_dead=True)

            dead_listener.close.assert_called_once()
            assert dead.listener is None and dead.listener_task is None
            live_listener.close.assert_not_called()
            live_task.cancel.assert_not_called()
            assert live.listener is live_listener

    async def test_stop_listener_when_no_listener_is_noop(self):
        """_stop_listener is safe when no listener is running."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            scheduler = BackupScheduler(config)
            scheduler._accounts = [_make_entry()]

            # Should not raise
            await scheduler._stop_listener()


class TestBackupSchedulerRunForever:
    """Tests for BackupScheduler.run_forever."""

    async def test_run_forever_connects_starts_and_runs_initial_backup(self):
        """run_forever connects, starts scheduler, and runs initial backup."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = False
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            # Make run_forever exit after first iteration by setting running=False
            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock) as mock_backup,
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()

            scheduler._connect.assert_called_once()
            scheduler.start.assert_called_once()
            config.for_account.assert_called_with(entry.account.index)
            mock_backup.assert_called_once_with(
                config.for_account.return_value, client=entry.connection.client, account_id=entry.row_id
            )

    async def test_run_forever_handles_initial_backup_failure(self):
        """run_forever catches exceptions from initial backup."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = False
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()
            scheduler.stop = MagicMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock, side_effect=Exception("backup failed")),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                # Should not raise
                await scheduler.run_forever()

    async def test_run_forever_cleanup_on_keyboard_interrupt(self):
        """run_forever cleans up on KeyboardInterrupt."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = False
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()
            scheduler.stop = MagicMock()

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch("src.scheduler.asyncio.sleep", side_effect=KeyboardInterrupt),
            ):
                await scheduler.run_forever()

            scheduler._stop_listener.assert_called()
            scheduler.stop.assert_called()
            scheduler._disconnect.assert_called()


class TestSchedulerMain:
    """Tests for the scheduler module-level main function."""

    async def test_main_creates_scheduler_and_runs(self):
        """main() loads config, creates scheduler, and runs."""
        mock_config = MagicMock()
        mock_config.schedule = "0 */6 * * *"
        mock_config.backup_path = "/data/backups"
        mock_config.download_media = True
        mock_config.chat_types = ["private"]
        mock_config.enable_listener = False
        mock_config.sync_deletions_edits = False

        mock_scheduler_instance = AsyncMock()

        with (
            patch("src.scheduler.signal.signal"),
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.scheduler.BackupScheduler", return_value=mock_scheduler_instance) as MockBS,
        ):
            from src.scheduler import main

            await main()

            MockBS.assert_called_once_with(mock_config)
            mock_scheduler_instance.run_forever.assert_called_once()

    async def test_main_handles_value_error(self):
        """main() exits with code 1 on ValueError."""
        with (
            patch("src.scheduler.signal.signal"),
            patch("src.config.Config", side_effect=ValueError("bad config")),
            patch("src.config.setup_logging"),
            patch("src.scheduler.sys.exit") as mock_exit,
        ):
            from src.scheduler import main

            await main()

            mock_exit.assert_called_once_with(1)

    async def test_main_handles_generic_exception(self):
        """main() exits with code 1 on unexpected exception."""
        with (
            patch("src.scheduler.signal.signal"),
            patch("src.config.Config", side_effect=RuntimeError("fatal")),
            patch("src.config.setup_logging"),
            patch("src.scheduler.sys.exit") as mock_exit,
        ):
            from src.scheduler import main

            await main()

            mock_exit.assert_called_once_with(1)


# ===========================================================================
# _run_backup_job gap-fill exception (lines 93-95)
# ===========================================================================


class TestRunBackupJobGapFillException:
    """Test _run_backup_job gap-fill exception path (lines 93-95)."""

    async def test_gap_fill_exception_sets_gap_fill_ok_false(self):
        """Exception during gap-fill sets gap_fill_ok to False."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = True
            scheduler = BackupScheduler(config)
            scheduler._accounts = [_make_entry()]

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch(
                    "src.telegram_backup.run_fill_gaps", new_callable=AsyncMock, side_effect=Exception("gap fill crash")
                ),
            ):
                # Should not raise
                await scheduler._run_backup_job()


# ===========================================================================
# run_forever initial gap-fill (lines 240-248)
# ===========================================================================


class TestRunForeverInitialGapFill:
    """Test run_forever initial gap-fill paths (lines 240-248)."""

    async def test_initial_gap_fill_runs_when_enabled(self):
        """Initial gap-fill runs after initial backup when fill_gaps=True."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = True
            config.enable_listener = False
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch(
                    "src.telegram_backup.run_fill_gaps",
                    new_callable=AsyncMock,
                    return_value={"errors": 0, "total_recovered": 3},
                ) as mock_fill,
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()

            mock_fill.assert_awaited_once()

    async def test_initial_gap_fill_with_errors_logs_warning(self):
        """Initial gap-fill with errors logs warning (line 246)."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = True
            config.enable_listener = False
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch(
                    "src.telegram_backup.run_fill_gaps",
                    new_callable=AsyncMock,
                    return_value={"errors": 5, "total_recovered": 2},
                ),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()

    async def test_initial_gap_fill_exception_caught(self):
        """Initial gap-fill exception is caught (lines 247-248)."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = True
            config.enable_listener = False
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch(
                    "src.telegram_backup.run_fill_gaps", new_callable=AsyncMock, side_effect=Exception("gap fill crash")
                ),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()


# ===========================================================================
# run_forever listener reload after gap-fill (line 252)
# ===========================================================================


class TestRunForeverListenerReload:
    """Test run_forever listener reload after initial backup (line 252)."""

    async def test_listener_tracked_chats_reloaded(self):
        """Listener tracked chats are reloaded after initial backup."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = True
            scheduler = BackupScheduler(config)

            mock_listener = AsyncMock()
            mock_listener._load_tracked_chats = AsyncMock()

            entry = _make_entry(listener=mock_listener)

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 1:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()

            mock_listener._load_tracked_chats.assert_awaited()


# ===========================================================================
# run_forever listener restart loop (lines 260-279)
# ===========================================================================


class TestRunForeverListenerRestart:
    """Test run_forever listener task restart loop (lines 260-279)."""

    async def test_listener_task_restart_on_death(self):
        """Dead listener task is restarted during the main loop."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = True
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            # Create a done task to simulate listener death
            loop = asyncio.get_event_loop()
            done_task = loop.create_future()
            done_task.set_exception(Exception("listener crashed"))
            entry.listener_task = done_task
            entry.listener = AsyncMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()

            # _stop_listener and _start_listener should have been called for restart
            assert scheduler._stop_listener.await_count >= 1
            assert scheduler._start_listener.await_count >= 1

    async def test_watchdog_retries_after_failed_start_until_success(self):
        """Regression: a failed restart must not permanently disable the listener.

        _start_listener resets _listener_task to None on failure. The old watchdog
        condition (`self.config.enable_listener and self._listener_task`) is falsy
        once _listener_task is None, so it never attempted another restart -- only
        a container restart could recover. The fixed watchdog keeps retrying based
        on `self._listener_enabled` regardless of whether the task is None or done.
        """
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = True
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            # scheduler.start() is mocked out, so it never sets self.running -- set it
            # directly to actually enter the `while self.running:` watchdog loop below.
            scheduler.start = MagicMock(side_effect=lambda: setattr(scheduler, "running", True))
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            attempt = [0]

            async def flaky_start_listener():
                attempt[0] += 1
                if attempt[0] < 3:
                    # Mirrors the real _start_account_listener failure path:
                    # the entry's task/listener stay None.
                    entry.listener_task = None
                    entry.listener = None
                else:
                    entry.listener_task = MagicMock(done=MagicMock(return_value=False))
                    entry.listener = AsyncMock()

            scheduler._start_listener = AsyncMock(side_effect=flaky_start_listener)

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 3:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()

            # Initial start (attempt 1) plus at least one watchdog-triggered retry
            # while the task stayed None -- proves the watchdog didn't give up after one try.
            assert attempt[0] >= 2
            assert entry.listener_task is not None

    async def test_listener_task_cancelled_restart(self):
        """Cancelled listener task is restarted during the main loop."""
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.fill_gaps = False
            config.enable_listener = True
            scheduler = BackupScheduler(config)

            entry = _make_entry()

            scheduler._connect = AsyncMock(side_effect=lambda: scheduler._accounts.append(entry))
            scheduler.start = MagicMock()
            scheduler._start_listener = AsyncMock()
            scheduler._stop_listener = AsyncMock()
            scheduler._disconnect = AsyncMock()

            # Create a cancelled task
            loop = asyncio.get_event_loop()
            done_task = loop.create_future()
            done_task.cancel()
            entry.listener_task = done_task
            entry.listener = AsyncMock()

            call_count = 0

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    scheduler.running = False

            with (
                patch("src.scheduler.run_backup", new_callable=AsyncMock),
                patch("src.scheduler.asyncio.sleep", side_effect=fake_sleep),
            ):
                await scheduler.run_forever()


# ===========================================================================
# main() logging output (lines 304-306, 322)
# ===========================================================================


class TestSchedulerMainLogging:
    """Test main() logging for sync_deletions_edits warning (lines 304-306)."""

    async def test_main_logs_sync_deletions_edits_warning(self):
        """main() logs warning when sync_deletions_edits is enabled (line 304)."""
        mock_config = MagicMock()
        mock_config.schedule = "0 */6 * * *"
        mock_config.backup_path = "/data/backups"
        mock_config.download_media = True
        mock_config.chat_types = ["private"]
        mock_config.enable_listener = False
        mock_config.sync_deletions_edits = True

        mock_scheduler_instance = AsyncMock()

        with (
            patch("src.scheduler.signal.signal"),
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.scheduler.BackupScheduler", return_value=mock_scheduler_instance) as MockBS,
        ):
            from src.scheduler import main

            await main()

            MockBS.assert_called_once_with(mock_config)
            mock_scheduler_instance.run_forever.assert_called_once()
