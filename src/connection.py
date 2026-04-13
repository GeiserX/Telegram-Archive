"""
Shared Telegram client connection manager.

This module provides a single TelegramClient instance that can be shared between
the backup and listener components, avoiding session file lock conflicts.

Architecture:
- TelegramConnection owns the single client
- Listener uses it for real-time events
- Backup uses it for fetching message history
- Both work on the same connection without conflicts
"""

import asyncio
import logging
import os
import shutil
import sqlite3

from telethon import TelegramClient

from .config import Config

logger = logging.getLogger(__name__)


class TelegramConnection:
    """
    Manages a single shared Telegram client connection.

    This solves the session lock conflict between listener and backup by
    ensuring only one TelegramClient instance exists and is shared.

    Usage:
        connection = TelegramConnection(config)
        await connection.connect()

        # Pass to backup and listener
        backup = TelegramBackup(config, db, client=connection.client)
        listener = TelegramListener(config, db, client=connection.client)

        # Both use the same connection
        await backup.backup_all()  # Uses shared client
        await listener.run()       # Uses shared client
    """

    def __init__(self, config: Config):
        """
        Initialize the connection manager.

        Args:
            config: Configuration object with Telegram credentials
        """
        self.config = config
        config.validate_credentials()

        self._client: TelegramClient | None = None
        self._connected = False
        self._me = None

    @property
    def client(self) -> TelegramClient | None:
        """Get the TelegramClient instance."""
        return self._client

    @property
    def is_connected(self) -> bool:
        """Check if connected to Telegram."""
        return self._connected and self._client is not None

    @property
    def me(self):
        """Get the current user info (available after connect)."""
        return self._me

    @staticmethod
    def _session_has_auth(session_file: str) -> bool:
        """Check if a session file contains a non-empty auth_key (i.e. was once authenticated)."""
        try:
            if not os.path.isfile(session_file) or os.path.getsize(session_file) == 0:
                return False
            conn = sqlite3.connect(session_file)
            cur = conn.cursor()
            cur.execute("SELECT auth_key FROM sessions LIMIT 1")
            row = cur.fetchone()
            conn.close()
            return row is not None and row[0] and len(row[0]) > 0
        except Exception:
            return False

    async def connect(self) -> TelegramClient:
        """
        Connect to Telegram and authenticate.

        Session protection: Telethon's .connect() silently replaces the auth_key
        in the session DB via DH key exchange when the existing key is invalid
        server-side. This means a single failed connect permanently destroys the
        old auth_key. During crash-loops this is catastrophic — the authenticated
        session is replaced with a useless one on the first attempt.

        We guard against this with two backup tiers:
        - `.session.authenticated` — golden backup, only written after successful auth
        - `.session.bak` — pre-connect snapshot, written before every connect attempt

        On auth failure we restore from the golden backup first (known-good state),
        falling back to the pre-connect snapshot.

        Returns:
            The connected TelegramClient instance

        Raises:
            RuntimeError: If session is not authorized
        """
        if self._connected and self._client:
            logger.debug("Already connected to Telegram")
            return self._client

        logger.info("Connecting to Telegram...")

        session_file = self.config.session_path + ".session"
        snapshot_file = self.config.session_path + ".session.bak"
        golden_file = self.config.session_path + ".session.authenticated"

        # Tier 1: if the golden backup exists and the live session lost its auth,
        # restore BEFORE TelegramClient even touches the file.
        if os.path.isfile(golden_file) and not self._session_has_auth(session_file):
            if self._session_has_auth(golden_file):
                logger.warning("Session file has no auth — restoring from authenticated backup")
                shutil.copy2(golden_file, session_file)

        # Tier 2: snapshot the current state before TelegramClient can modify it.
        if self._session_has_auth(session_file):
            shutil.copy2(session_file, snapshot_file)

        self._client = TelegramClient(
            self.config.session_path,
            self.config.api_id,
            self.config.api_hash,
            **self.config.get_telegram_client_kwargs(),
        )

        # Enable WAL mode for session DB to handle concurrent access
        self._enable_wal_mode()

        # Connect to Telegram
        await self._client.connect()

        # Check authorization
        if not await self._client.is_user_authorized():
            await self._client.disconnect()
            # Restore from the best available backup.
            # Golden backup is preferred (known-good); snapshot is the fallback.
            restored = False
            for backup in (golden_file, snapshot_file):
                if self._session_has_auth(backup):
                    logger.warning(f"Auth failed — restoring session from {os.path.basename(backup)}")
                    shutil.copy2(backup, session_file)
                    restored = True
                    break
            if restored:
                logger.info("Session restored. Will retry on next scheduler cycle.")
            logger.error("❌ Session not authorized!")
            logger.error("Please run the authentication setup first:")
            logger.error("  Docker: ./init_auth.bat (Windows) or ./init_auth.sh (Linux/Mac)")
            logger.error("  Local:  python -m src.setup_auth")
            logger.error("  Non-interactive: python scripts/auth_noninteractive.py send")
            raise RuntimeError("Session not authorized. Please run authentication setup.")

        self._me = await self._client.get_me()
        self._connected = True

        # Auth succeeded — update the golden backup (known-good state).
        # Flush Telethon's WAL to ensure the file is complete before copying.
        try:
            if hasattr(self._client.session, "_conn") and self._client.session._conn:
                self._client.session._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        shutil.copy2(session_file, golden_file)

        logger.info(f"Connected as {self._me.first_name} ({self._me.phone})")

        return self._client

    def _enable_wal_mode(self) -> None:
        """Enable WAL mode on the SQLite session database for better concurrency."""
        try:
            if hasattr(self._client.session, "_conn"):
                if self._client.session._conn:
                    self._client.session._conn.execute("PRAGMA journal_mode=WAL")
                    self._client.session._conn.execute("PRAGMA busy_timeout=30000")
                    logger.info("Enabled WAL mode for Telethon session database")
        except Exception as e:
            logger.warning(f"Could not enable WAL mode for session DB: {e}")

    async def disconnect(self) -> None:
        """
        Disconnect from Telegram gracefully.

        Note: Telethon has a known issue (LonamiWebs/Telethon#782) where internal
        tasks (_send_loop, _recv_loop) aren't properly cancelled on disconnect,
        causing "Task was destroyed but it is pending" warnings. These are harmless
        and don't affect functionality.
        """
        if self._client and self._connected:
            try:
                await self._client.disconnect()
                # Small delay to allow internal task cleanup
                await asyncio.sleep(0.5)
            except Exception as e:
                # Log but don't fail - disconnect errors during shutdown are expected
                logger.debug(f"Disconnect cleanup: {e}")
            finally:
                self._connected = False
                logger.info("Disconnected from Telegram")

    async def ensure_connected(self) -> TelegramClient:
        """
        Ensure the client is connected, reconnecting if necessary.

        Returns:
            The connected TelegramClient instance
        """
        if not self.is_connected:
            return await self.connect()

        # Check if connection is still alive
        try:
            if not self._client.is_connected():
                logger.warning("Connection lost, reconnecting...")
                await self._client.connect()
                self._me = await self._client.get_me()
                logger.info(f"Reconnected as {self._me.first_name}")
        except Exception as e:
            logger.warning(f"Connection check failed: {e}, reconnecting...")
            await self.connect()

        return self._client

    async def __aenter__(self) -> TelegramConnection:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()
