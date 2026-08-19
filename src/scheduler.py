"""
Scheduler for automated Telegram backups.
Runs backup tasks on a configurable cron schedule.

Optionally runs a real-time listener that catches message edits and deletions
between scheduled backup runs (when ENABLE_LISTENER=true).

SHARED CONNECTION ARCHITECTURE:
Each configured account has a single TelegramClient shared between its backup
and listener components. This avoids session file lock conflicts and allows
both to run simultaneously. Accounts are swept SEQUENTIALLY: account 1's full
backup completes before account 2's starts — Telegram tolerates N sessions,
not N concurrent full sweeps from one box.
"""

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AccountConfig, Config
from .connection import TelegramConnection
from .telegram_backup import run_backup

if TYPE_CHECKING:
    from .listener import TelegramListener

logger = logging.getLogger(__name__)


@dataclass
class _AccountRuntime:
    """Per-account runtime state: one shared connection, one listener, one row id.

    ``row_id`` is the ``accounts.id`` resolved from the Telegram user id after
    login (see ``DatabaseAdapter.ensure_account``); None until the account has
    connected once. ``log_prefix`` is "" in a single-account deployment — log
    lines stay byte-identical to pre-8.0 — and ``"[account <index>] "``
    otherwise, so multi-account output is attributable by env index, never by
    label or phone (#272).
    """

    account: AccountConfig
    connection: TelegramConnection
    log_prefix: str = ""
    row_id: int | None = None
    listener: TelegramListener | None = None
    listener_task: asyncio.Task | None = None


class BackupScheduler:
    """
    Scheduler for automated backups with optional real-time listener.

    Uses a shared TelegramClient connection per account for both backup and
    listener, eliminating session file lock conflicts.
    """

    def __init__(self, config: Config):
        """
        Initialize backup scheduler.

        Args:
            config: Configuration object
        """
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.running = False
        self._backup_lock = asyncio.Lock()
        # Serializes account-row resolution: the sweep, a listener start and the
        # initial backup can all call it concurrently, and accounts has no
        # DB-level unique on telegram_user_id, so two interleaved misses would
        # both INSERT a row for the same account.
        self._resolve_rows_lock = asyncio.Lock()

        # Per-account runtime state (one shared connection + listener each),
        # populated by _connect() in config order. Sweeps iterate it in order.
        self._accounts: list[_AccountRuntime] = []

        # Set once (in run_forever) when the listeners are supposed to be running
        # for the life of the process. Unlike checking config directly, this lets
        # the watchdog keep retrying after a failed start -- _start_account_listener
        # resets listener_task to None on failure, but the listener is still "enabled".
        self._listener_enabled = False

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.stop()

    async def _resolve_account_rows(self) -> None:
        """Resolve each connected account's ``accounts`` row id, once.

        The row is keyed on the Telegram user id from the account's own login
        (``DatabaseAdapter.ensure_account`` — including the claim of the
        migrated pre-8.0 row by the account at env index 1). Entries whose
        connection has not come up yet are left unresolved and retried by the
        next sweep or listener start. Uses one short-lived adapter, the same
        engine lifecycle run_backup itself uses per run.
        """
        async with self._resolve_rows_lock:
            # Recomputed under the lock: a caller that queued behind a completed
            # resolution finds nothing pending and does no database work.
            pending = [e for e in self._accounts if e.row_id is None and e.connection.me is not None]
            if not pending:
                return
            from .db import create_adapter

            db = await create_adapter()
            try:
                for entry in pending:
                    entry.row_id = await db.ensure_account(
                        telegram_user_id=entry.connection.me.id,
                        env_index=entry.account.index,
                        label=entry.account.label,
                    )
            finally:
                await db.close()

    async def _sweep_account(self, entry: _AccountRuntime) -> bool:
        """One account's full scheduled sweep: backup, then optional gap-fill.

        Returns False when gap-fill reported errors. Raises on hard failure
        (connection, row resolution, backup) — the caller decides whether that
        kills the run (single account) or moves on to the next account.
        """
        # Ensure connection is still alive
        client = await entry.connection.ensure_connected()

        # Normally resolved at startup; retried here for an account whose
        # connection (or the database) was down back then.
        if entry.row_id is None:
            await self._resolve_account_rows()
        if entry.row_id is None:
            raise RuntimeError(f"account {entry.account.index} row not resolved")

        # Run backup using this account's shared client
        await run_backup(self.config.for_account(entry.account.index), client=client, account_id=entry.row_id)

        # Run gap-fill if enabled
        gap_fill_ok = True
        if self.config.fill_gaps:
            try:
                from .telegram_backup import run_fill_gaps

                logger.info(f"{entry.log_prefix}Running post-backup gap-fill...")
                result = await run_fill_gaps(
                    self.config.for_account(entry.account.index), client=client, account_id=entry.row_id
                )
                if result.get("errors", 0) > 0:
                    gap_fill_ok = False
                    logger.warning(
                        f"{entry.log_prefix}Gap-fill completed with {result['errors']} error(s) "
                        f"({result['total_recovered']} messages recovered)"
                    )
            except Exception as e:
                gap_fill_ok = False
                logger.error(f"{entry.log_prefix}Gap-fill failed: {e}", exc_info=True)

        # Reload tracked chats in this account's listener after its backup
        # (new chats may have been added)
        if entry.listener:
            await entry.listener._load_tracked_chats()

        return gap_fill_ok

    async def _run_backup_job(self):
        """
        Wrapper for backup job that handles errors.

        Uses the shared connections - no need to pause the listeners since each
        account's backup and listener use the same TelegramClient. Accounts are
        swept sequentially, in config order.
        """
        if self._backup_lock.locked():
            logger.warning("Skipping scheduled backup because another backup is already running")
            return

        async with self._backup_lock:
            try:
                logger.info("Scheduled backup starting...")

                gap_fill_ok = True
                failed = 0
                for entry in self._accounts:
                    try:
                        gap_fill_ok = await self._sweep_account(entry) and gap_fill_ok
                    except Exception as e:
                        # One broken account must not consume the other accounts'
                        # sweeps; a single-account deployment keeps the pre-8.0
                        # error path below instead.
                        if len(self._accounts) == 1:
                            raise
                        # Type name only: exception text can carry the phone (#272).
                        failed += 1
                        logger.error(f"account {entry.account.index} failed: {type(e).__name__}")

                if failed:
                    logger.warning(f"Scheduled backup completed, but {failed} account(s) failed")
                elif gap_fill_ok:
                    logger.info("Scheduled backup completed successfully")
                else:
                    logger.warning("Scheduled backup completed, but gap-fill had errors")

            except Exception as e:
                logger.error(f"Scheduled backup failed: {e}", exc_info=True)

    def start(self):
        """Start the scheduler."""
        # Parse cron schedule
        # Format: minute hour day month day_of_week
        # Example: "0 */6 * * *" = every 6 hours
        try:
            parts = self.config.schedule.split()
            if len(parts) != 5:
                raise ValueError(
                    f"Invalid cron schedule format: {self.config.schedule}. "
                    "Expected format: 'minute hour day month day_of_week'"
                )

            minute, hour, day, month, day_of_week = parts

            trigger = CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)

            # Add job to scheduler
            self.scheduler.add_job(
                self._run_backup_job,
                trigger=trigger,
                id="telegram_backup",
                name="Telegram Backup",
                replace_existing=True,
            )

            logger.info(f"Backup scheduled with cron: {self.config.schedule}")

            # Start scheduler
            self.scheduler.start()
            self.running = True

            logger.info("Scheduler started successfully")

        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}", exc_info=True)
            raise

    def stop(self):
        """Stop the scheduler."""
        if self.running:
            logger.info("Stopping scheduler...")
            self.scheduler.shutdown(wait=True)
            self.running = False
            logger.info("Scheduler stopped")

    async def _connect(self) -> None:
        """Establish one shared Telegram connection per configured account."""
        multi = len(self.config.accounts) > 1
        for account in self.config.accounts:
            prefix = f"[account {account.index}] " if multi else ""
            logger.info(f"{prefix}Establishing shared Telegram connection...")
            entry = _AccountRuntime(
                account=account,
                connection=TelegramConnection(self.config, account=account),
                log_prefix=prefix,
            )
            try:
                await entry.connection.connect()
                logger.info(f"{prefix}Shared connection established")
            except Exception as e:
                # One unreachable account must not keep the others from coming
                # up; its connection heals through ensure_connected() on later
                # sweeps and listener starts. A single-account deployment keeps
                # the pre-8.0 fail-fast startup.
                if not multi:
                    raise
                # Type name only: exception text can carry the phone (#272).
                logger.error(f"account {account.index} failed: {type(e).__name__}")
            self._accounts.append(entry)

        try:
            await self._resolve_account_rows()
        except Exception as e:
            # The database being down at startup must not kill the process:
            # pre-8.0 the first DB touch happened inside the initial backup,
            # which is retried every cycle — keep that resilience. Resolution
            # is retried by every sweep and listener start. Type name only
            # (the text can carry a connection DSN).
            logger.warning(f"Could not resolve account rows yet: {type(e).__name__}")

    async def _disconnect(self) -> None:
        """Close all shared Telegram connections."""
        for entry in self._accounts:
            await entry.connection.disconnect()
        self._accounts = []

    async def _start_listener(self) -> None:
        """Start the real-time listener for every account, if enabled.

        Idempotent per account: an entry whose listener task is alive is left
        untouched, so the watchdog can call this to revive only what died —
        healthy accounts' handlers are never re-registered (the duplicate-
        handler trap _remove_handlers exists for).
        """
        if not self.config.enable_listener:
            return

        if not self._accounts:
            logger.error("Cannot start listener: not connected to Telegram")
            return

        for entry in self._accounts:
            if entry.listener_task is not None and not entry.listener_task.done():
                continue
            await self._start_account_listener(entry)

    async def _start_account_listener(self, entry: _AccountRuntime) -> None:
        """Start one account's real-time listener on its shared connection."""
        # ``TelegramConnection.is_connected`` is an app-level flag: it stays True
        # after Telethon's sender exhausts its own reconnect budget (~55s) and
        # marks itself disconnected, so it cannot tell us the socket is dead.
        # Without re-establishing the connection here, the watchdog restarts the
        # listener into "Shared client is not connected" every 5 seconds forever
        # once an outage outlives Telethon's budget (#265). ensure_connected()
        # checks the live client and calls connect() when it is down.
        #
        # This runs on the watchdog loop, which shares an event loop with the
        # scheduled backup job, so it can fire while a backup is suspended at an
        # await. That is safe because TelegramConnection heals the SAME client
        # object in place and serialises healers on its own lock — the backup's
        # client reference heals with it instead of being left behind. Skipping
        # the heal while a backup holds the connection would be the wrong trade:
        # a backup can run for hours, and the listener would stay dead for all
        # of it, which is the outage #265 is about.
        try:
            await entry.connection.ensure_connected()
        except Exception as e:
            logger.warning(
                # Exception text can embed the DSN or host; type name only, like
                # the sibling handlers in this file.
                f"{entry.log_prefix}Could not re-establish the shared connection before starting the listener: "
                f"{type(e).__name__}"
            )

        if not entry.connection.is_connected:
            logger.error(f"{entry.log_prefix}Cannot start listener: not connected to Telegram")
            return

        try:
            from .listener import TelegramListener

            logger.info(f"{entry.log_prefix}Starting real-time listener...")

            # Normally resolved at startup; retried here for an account whose
            # connection (or the database) was down back then.
            if entry.row_id is None:
                await self._resolve_account_rows()
            if entry.row_id is None:
                raise RuntimeError(f"account {entry.account.index} row not resolved")

            # Create listener with this account's shared client.
            entry.listener = await TelegramListener.create(
                self.config.for_account(entry.account.index), client=entry.connection.client, account_id=entry.row_id
            )
            await entry.listener.connect()

            # Run listener in background task
            task_name = (
                "telegram_listener" if len(self._accounts) == 1 else f"telegram_listener_account{entry.account.index}"
            )
            entry.listener_task = asyncio.create_task(entry.listener.run(), name=task_name)
            logger.info(f"{entry.log_prefix}Real-time listener started successfully")

        except Exception as e:
            logger.error(f"{entry.log_prefix}Failed to start listener: {e}", exc_info=True)
            entry.listener = None
            entry.listener_task = None

    async def _stop_listener(self, only_dead: bool = False) -> None:
        """Stop listeners — all of them (shutdown), or only dead ones (watchdog)."""
        for entry in self._accounts:
            if only_dead and entry.listener_task is not None and not entry.listener_task.done():
                continue
            await self._stop_account_listener(entry)

    async def _stop_account_listener(self, entry: _AccountRuntime) -> None:
        """Stop one account's real-time listener if running."""
        if entry.listener_task:
            logger.info(f"{entry.log_prefix}Stopping real-time listener...")
            entry.listener_task.cancel()
            try:
                await entry.listener_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Task already died (e.g. transient ConnectionError); its
                # exception was logged by the restart loop. Awaiting a done
                # task re-raises it, so swallow here to let teardown/restart
                # proceed instead of crashing the process.
                pass
            entry.listener_task = None

        if entry.listener:
            await entry.listener.close()
            entry.listener = None
            logger.info(f"{entry.log_prefix}Real-time listener stopped")

    async def _initial_backup_account(self, entry: _AccountRuntime) -> None:
        """The startup backup for one account, mirroring the scheduled sweep.

        Kept separate from _sweep_account because the startup sequence logs
        'Initial backup completed' between the backup and the gap-fill — the
        pre-8.0 order, which single-account deployments keep byte-for-byte.
        """
        # The connection was just established by _connect(); the ensure below
        # only matters for an account whose startup connect failed (multi-
        # account mode) — it raises into the caller's per-account handler.
        client = entry.connection.client
        if client is None or not entry.connection.is_connected:
            client = await entry.connection.ensure_connected()

        if entry.row_id is None:
            await self._resolve_account_rows()
        if entry.row_id is None:
            raise RuntimeError(f"account {entry.account.index} row not resolved")

        await run_backup(self.config.for_account(entry.account.index), client=client, account_id=entry.row_id)
        logger.info(f"{entry.log_prefix}Initial backup completed")

        # Run gap-fill if enabled
        if self.config.fill_gaps:
            try:
                from .telegram_backup import run_fill_gaps

                logger.info(f"{entry.log_prefix}Running initial gap-fill...")
                result = await run_fill_gaps(
                    self.config.for_account(entry.account.index), client=client, account_id=entry.row_id
                )
                if result.get("errors", 0) > 0:
                    logger.warning(f"{entry.log_prefix}Initial gap-fill completed with {result['errors']} error(s)")
            except Exception as e:
                logger.error(f"{entry.log_prefix}Initial gap-fill failed: {e}", exc_info=True)

        # Reload tracked chats in this account's listener after initial backup
        if entry.listener:
            await entry.listener._load_tracked_chats()

    async def run_forever(self):
        """
        Keep the scheduler running with optional listeners.

        Flow:
        1. Connect to Telegram (one shared connection per account)
        2. Start scheduler
        3. Start listeners if enabled (each on its account's shared connection)
        4. Run initial backup, account by account (shared connections)
        5. Keep running until stopped
        """
        # Establish shared connections
        await self._connect()

        # Start scheduler
        self.start()

        # Start real-time listeners if enabled (each uses its shared connection).
        self._listener_enabled = self.config.enable_listener
        await self._start_listener()

        # Run initial backup immediately on startup (uses shared connections)
        logger.info("Running initial backup on startup...")
        async with self._backup_lock:
            try:
                for entry in self._accounts:
                    try:
                        await self._initial_backup_account(entry)
                    except Exception as e:
                        # Same continuation rule as the scheduled sweep: only a
                        # single-account deployment lets the failure reach the
                        # pre-8.0 catch below.
                        if len(self._accounts) == 1:
                            raise
                        logger.error(f"account {entry.account.index} failed: {type(e).__name__}")
            except Exception as e:
                logger.error(f"Initial backup failed: {e}", exc_info=True)

        # Keep running until stopped
        try:
            while self.running:
                await asyncio.sleep(1)

                # Check if any listener task died, or never came up at all (a failed
                # restart leaves listener_task as None -- see _start_account_listener),
                # and restart it either way. Retrying on None (not just "died") is what
                # keeps a flapping listener from being permanently disabled after a
                # single failed restart attempt. Only dead entries are restarted --
                # healthy accounts' listeners keep running untouched.
                if self._listener_enabled and any(
                    entry.listener_task is None or entry.listener_task.done() for entry in self._accounts
                ):
                    for entry in self._accounts:
                        if entry.listener_task is not None and entry.listener_task.done():
                            # Check if there was an exception
                            try:
                                exc = entry.listener_task.exception()
                                if exc:
                                    logger.error(f"{entry.log_prefix}Listener task died with error: {exc}")
                            except asyncio.CancelledError:
                                pass

                    logger.warning("Listener task not running, attempting restart...")
                    await self._stop_listener(only_dead=True)
                    await asyncio.sleep(5)  # Brief pause before restart
                    await self._start_listener()

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            await self._stop_listener()
            self.stop()
            await self._disconnect()


async def main():
    """Main entry point for the scheduler."""
    try:
        # Load configuration
        from .config import Config, setup_logging

        config = Config()
        setup_logging(config)

        logger.info("=" * 60)
        logger.info("Telegram Backup Automation")
        logger.info("=" * 60)
        logger.info(f"Schedule: {config.schedule}")
        logger.info(f"Backup path: {config.backup_path}")
        logger.info(f"Download media: {config.download_media}")
        logger.info(f"Chat types: {', '.join(config.chat_types) or '(whitelist-only mode)'}")
        # Effective per-account capture scope (8.1, #313) — counts only, never ids.
        for account in config.accounts:
            filters = config.filters_for(account.index)
            if filters.whitelist_mode:
                scope = f"whitelist, {len(filters.chat_ids)} chat(s)"
            else:
                include_count = (
                    len(filters.global_include_ids)
                    + len(filters.private_include_ids)
                    + len(filters.groups_include_ids)
                    + len(filters.channels_include_ids)
                )
                exclude_count = (
                    len(filters.global_exclude_ids)
                    + len(filters.private_exclude_ids)
                    + len(filters.groups_exclude_ids)
                    + len(filters.channels_exclude_ids)
                )
                scope = (
                    f"type-based ({', '.join(filters.chat_types) or 'no types'}), "
                    f"+{include_count} include / -{exclude_count} exclude"
                )
            logger.info(f"Capture scope [account {account.index}]: {scope}")
        logger.info(f"Real-time listener: {'ENABLED' if config.enable_listener else 'disabled'}")
        if config.sync_deletions_edits:
            logger.warning("⚠️  SYNC_DELETIONS_EDITS: ENABLED")
            logger.warning("   → Will re-check ALL messages for edits/deletions each run")
            logger.warning("   → This is expensive but catches changes made while offline")
        logger.info("=" * 60)

        # Migrate flat _shared/ to sharded layout (idempotent, runs once)
        from .migrate_shared_media import migrate_shared_media

        migrate_shared_media(config.media_path)

        # Create and run scheduler
        scheduler = BackupScheduler(config)
        await scheduler.run_forever()

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
