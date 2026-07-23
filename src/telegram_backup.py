"""
Main Telegram backup module.
Handles Telegram client connection, message fetching, and incremental backup logic.
"""

import asyncio
import base64
import json
import logging
import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatForbiddenError,
    FileReferenceExpiredError,
    FloodWaitError,
    RPCError,
    UserBannedInChannelError,
)
from telethon.tl.types import (
    Channel,
    Chat,
    InputPeerSelf,
    Message,
    MessageActionChannelMigrateFrom,
    MessageActionChatMigrateTo,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    PeerChannel,
    PeerChat,
    TextWithEntities,
    User,
)
from telethon.utils import get_peer_id

from .avatar_utils import get_avatar_paths
from .config import Config
from .db import DatabaseAdapter, create_adapter
from .folder_utils import FolderChat, FolderRules, resolve_folder_member_ids
from .media_errors import is_media_location_error
from .message_utils import (
    build_media_filename,
    compute_file_hash,
    download_and_shard_media,
    extract_reactions,
    extract_topic_id,
    fallback_media_filename,
    finalize_atomic_download,
    resolve_shared_file_path,
    service_action_type,
    service_message_text,
    utcnow_naive,
)
from .parallel_download import (
    ParallelDownloader,
    ParallelDownloadUnavailable,
    supports_parallel_download,
)

logger = logging.getLogger(__name__)


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%d", name, raw, default)
        return default


def _get_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default=%.1f", name, raw, default)
        return default


MAX_FLOOD_RETRIES = _get_int_env("MAX_FLOOD_RETRIES", 5)
MAX_FLOOD_WAIT_SECONDS = _get_int_env("MAX_FLOOD_WAIT_SECONDS", 3600)
BACKOFF_MIN_SECONDS = _get_float_env("BACKOFF_MIN_SECONDS", 2.0)
BACKOFF_MAX_SECONDS = _get_float_env("BACKOFF_MAX_SECONDS", 300.0)

# Re-sweep flood handling (#224): after a FloodWait the re-sweep pauses (nothing
# sleeps, nothing retries) until the server-requested window plus this margin has
# elapsed, then resumes within the same run. After this many floods in a single
# run the remainder defers outright — repeated floods signal a degraded bucket
# that should be left alone until the next scheduled run.
RESWEEP_FLOOD_RESUME_MARGIN_SECONDS = 2.0
RESWEEP_MAX_FLOODS_PER_RUN = 3
FLOOD_WAIT_LOG_THRESHOLD = _get_int_env("FLOOD_WAIT_LOG_THRESHOLD", 10)
# Bounded re-fetch+retry for transient media errors (expired reference / location
# unavailable). After this many download attempts the item is left for the next
# scheduled backup run instead of being retried indefinitely.
MEDIA_REFRESH_MAX_ATTEMPTS = _get_int_env("MEDIA_REFRESH_MAX_ATTEMPTS", 3)
# Upper bound on a single message-refresh round-trip so it can never hang.
MEDIA_REFRESH_TIMEOUT_SECONDS = _get_int_env("MEDIA_REFRESH_TIMEOUT_SECONDS", 120)


def _media_retry_backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff (+jitter) between media-refresh retries.

    Location errors are transient server-side conditions, so we pause before
    retrying rather than hammering ``upload.GetFile`` (which risks a FloodWait).
    """
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * (2.0**attempt))
    return base + random.uniform(0.5, 1.5)


def _is_non_retryable_media_op(exc: BaseException) -> bool:
    """Errors the media-download loop handles itself, so ``call_with_flood_retry``
    must re-raise them rather than retry: location errors (the outer loop refreshes
    and backs off) and a per-operation ``TimeoutError`` (the outer loop decides).

    Keeping these out of ``call_with_flood_retry`` also means the per-operation
    timeout never wraps — and so never cancels — its FloodWait sleeps.
    """
    return is_media_location_error(exc) or isinstance(exc, TimeoutError)


def _pre_generate_thumbnail(source_path: str, media_root: str) -> None:
    """Pre-generate 200px WebP thumbnail for gallery grid view."""
    try:
        from pathlib import Path

        from PIL import Image

        from src.web.thumbnails import (
            _IMAGE_EXTENSIONS,
            _MAX_SOURCE_BYTES,
            WEBP_QUALITY,
            _thumb_path,
        )

        Image.MAX_IMAGE_PIXELS = 50_000_000

        source = Path(source_path)
        if not source.exists():
            return

        if source.suffix.lower() not in _IMAGE_EXTENSIONS:
            return

        if source.stat().st_size > _MAX_SOURCE_BYTES:
            return

        media_root_path = Path(media_root)
        if not source.is_relative_to(media_root_path):
            return

        rel = source.relative_to(media_root_path)
        folder = str(rel.parent)
        dest = _thumb_path(media_root_path, 200, folder, source.name)

        if dest.exists():
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as img:
            img.thumbnail((200, 200), Image.LANCZOS)
            img.save(dest, "WEBP", quality=WEBP_QUALITY)
    except Exception as e:
        logger.debug("Thumbnail pre-generation failed: %s", e)


async def call_with_flood_retry(
    coro_fn,
    *args,
    max_retries=MAX_FLOOD_RETRIES,
    non_retryable: Callable[[BaseException], bool] | None = None,
    **kwargs,
):
    """Retry a single async call on FloodWaitError with bounded sleep and
    general transient errors with configurable exponential backoff and jitter.

    Use this for one-shot Telegram API calls (``get_dialogs``, ``get_me``, etc.)
    that are not async iterators.  For ``iter_messages`` use
    ``iter_messages_with_flood_retry`` instead.

    ``non_retryable`` is an optional predicate; when it returns ``True`` for a
    raised exception, that exception is re-raised immediately instead of being
    retried here, letting the caller handle it (e.g. refresh a stale media
    reference and retry with its own backoff).
    """
    retries = 0
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except FloodWaitError as e:
            retries += 1
            if retries > max_retries:
                logger.error(
                    "FloodWait: exceeded %d retries on %s, giving up",
                    max_retries,
                    getattr(coro_fn, "__name__", coro_fn),
                )
                raise
            if e.seconds > MAX_FLOOD_WAIT_SECONDS:
                logger.error(
                    "FloodWait: required wait %ss exceeds MAX_FLOOD_WAIT_SECONDS=%s on %s",
                    e.seconds,
                    MAX_FLOOD_WAIT_SECONDS,
                    getattr(coro_fn, "__name__", coro_fn),
                )
                raise
            wait_seconds = max(0, e.seconds)
            # Exponential backoff: use at least the Telegram-required wait,
            # but escalate on repeated hits so we don't hammer the server.
            backoff = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * (2.0 ** (retries - 1)))
            effective_wait = max(wait_seconds, backoff)
            jitter = random.uniform(0.5, 2.0)
            sleep_duration = effective_wait + jitter
            logger.warning(
                "FloodWait: sleeping %.2fs (wait=%ss, backoff=%.0fs, jitter=%.2fs) before retrying %s (retry=%d/%d)",
                sleep_duration,
                wait_seconds,
                backoff,
                jitter,
                getattr(coro_fn, "__name__", coro_fn),
                retries,
                max_retries,
            )
            await asyncio.sleep(sleep_duration)
        except (TimeoutError, ConnectionError, OSError, RPCError) as exc:
            # If it is a FloodWaitError, FileReferenceExpiredError, or terminal RPC error,
            # raise it to let the prior except block or the calling scope catch it specifically
            # without wasting retries.
            if isinstance(
                exc,
                (
                    FloodWaitError,
                    FileReferenceExpiredError,
                    ChannelPrivateError,
                    ChatForbiddenError,
                    UserBannedInChannelError,
                ),
            ):
                raise exc
            if non_retryable is not None and non_retryable(exc):
                # Caller handles this error itself (e.g. refresh the message and
                # retry), so don't burn this retry budget on it here.
                raise exc

            retries += 1
            if retries > max_retries:
                logger.error(
                    "Transient Error: exceeded %d retries on %s, giving up: %s",
                    max_retries,
                    getattr(coro_fn, "__name__", coro_fn),
                    exc,
                )
                raise

            # Exponential backoff: backoff = min(backoff_max, backoff_min * (2 ** (retries - 1)))
            backoff = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * (2.0 ** (retries - 1)))
            jitter = random.uniform(0.5, 1.5)
            sleep_duration = backoff + jitter

            logger.warning(
                "Transient Error (%s): sleeping %.2fs before retrying %s (retry=%d/%d): %s",
                exc.__class__.__name__,
                sleep_duration,
                getattr(coro_fn, "__name__", coro_fn),
                retries,
                max_retries,
                exc,
            )
            await asyncio.sleep(sleep_duration)


async def iter_messages_with_flood_retry(client, entity, *, min_id=0, **kwargs):
    """Wrap ``client.iter_messages`` so FloodWaitError is logged and retried.

    With ``flood_sleep_threshold=0`` on the client, every flood-wait bubbles up
    as an exception. We log the wait and resume iteration from the last yielded
    message id so progress isn't lost.

    Bounded retries: the inner ``while`` is capped at ``MAX_FLOOD_RETRIES``
    *consecutive* flood-waits without progress, and the counter resets every
    time iteration yields a message. Without the cap, an account-restricted
    Telegram session would loop forever on one chat and block every later one.

    Bounded sleep: waits above ``MAX_FLOOD_WAIT_SECONDS`` abort the current
    operation instead of retrying before Telegram's required wait has elapsed.

    The ``FLOOD_WAIT_LOG_THRESHOLD`` env var (default 10) suppresses log
    output for short waits — those are routine and noisy in healthy backfills.
    Set to 0 to log every wait.

    Note: resume tracking uses ``max(resume_from, msg.id)`` which is only
    correct for ascending iteration (``reverse=True``).
    """
    if not kwargs.get("reverse", False):
        raise ValueError("iter_messages_with_flood_retry only supports reverse=True (ascending) iteration")
    resume_from = min_id
    retries = 0
    while True:
        try:
            async for msg in client.iter_messages(entity, min_id=resume_from, **kwargs):
                yield msg
                if getattr(msg, "id", None) is not None:
                    resume_from = max(resume_from, msg.id)
                retries = 0
            return
        except FloodWaitError as e:
            retries += 1
            if retries > MAX_FLOOD_RETRIES:
                logger.error(
                    "FloodWait: exceeded %d retries without progress, giving up (last_msg_id=%s)",
                    MAX_FLOOD_RETRIES,
                    resume_from,
                )
                raise
            if e.seconds > MAX_FLOOD_WAIT_SECONDS:
                logger.error(
                    "FloodWait: required wait %ss exceeds MAX_FLOOD_WAIT_SECONDS=%s; aborting (last_msg_id=%s)",
                    e.seconds,
                    MAX_FLOOD_WAIT_SECONDS,
                    resume_from,
                )
                raise
            wait_seconds = max(0, e.seconds)
            # Exponential backoff: use at least the Telegram-required wait,
            # but escalate on repeated hits so we don't hammer the server.
            backoff = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * (2.0 ** (retries - 1)))
            effective_wait = max(wait_seconds, backoff)
            jitter = random.uniform(0.5, 2.0)
            sleep_duration = effective_wait + jitter
            if e.seconds >= FLOOD_WAIT_LOG_THRESHOLD or retries > 1:
                logger.warning(
                    "FloodWait: sleeping %.2fs (wait=%ss, backoff=%.0fs, jitter=%.2fs) before resuming (last_msg_id=%s, retry=%d/%d)",
                    sleep_duration,
                    wait_seconds,
                    backoff,
                    jitter,
                    resume_from,
                    retries,
                    MAX_FLOOD_RETRIES,
                )
            await asyncio.sleep(sleep_duration)


class TelegramBackup:
    """Main class for managing Telegram backups."""

    def __init__(self, config: Config, db: DatabaseAdapter, client: TelegramClient | None = None):
        """
        Initialize Telegram backup manager.

        Args:
            config: Configuration object
            db: Async database adapter (must be initialized before passing)
            client: Optional existing TelegramClient to use (for shared connection).
                   If not provided, will create a new client in connect().
        """
        self.config = config
        self.config.validate_credentials()
        self.db = db
        self.client: TelegramClient | None = client
        self._owns_client = client is None  # Track if we created the client
        self._cleaned_media_chats: set[int] = set()  # Track chats already cleaned this session
        # Lazily-built parallel downloader (issue #183). Stays None until the
        # first large file when the feature is enabled; disabled for the rest of
        # the run if the client lacks the required Telethon internals.
        self._parallel_downloader: ParallelDownloader | None = None
        self._parallel_download_disabled = False
        # Marked ids of supergroups adopted after a group→supergroup migration
        # (#228). Loaded from the metadata KV at the start of each backup_all run
        # when FOLLOW_CHAT_MIGRATIONS is on; merged into the effective sweep scope.
        self._followed_migration_ids: set[int] = set()

        logger.info("TelegramBackup initialized")

    def _get_marked_id(self, entity) -> int:
        """
        Get the marked ID for an entity (with -100 prefix for channels/supergroups).

        Telegram uses different ID formats:
        - Users: positive ID (e.g., 123456789)
        - Basic groups (Chat): negative ID (e.g., -123456789)
        - Supergroups/Channels: marked with -100 prefix (e.g., -1001234567890)

        This ensures IDs match what users see in Telegram and configure in env vars.
        """
        return get_peer_id(entity)

    async def _load_followed_migrations(self) -> None:
        """Load adopted-supergroup ids from the metadata KV (#228).

        Populates ``self._followed_migration_ids`` from the ``followed_migrations``
        metadata key (a JSON list of marked ids). Only consulted when
        FOLLOW_CHAT_MIGRATIONS is on; when off the set stays empty so nothing is
        treated as followed and the sweep only warns. Never raises — a missing or
        malformed value degrades to "nothing followed yet".
        """
        self._followed_migration_ids = set()
        if not self.config.follow_chat_migrations:
            return
        try:
            raw = await self.db.get_metadata("followed_migrations")
        except Exception as e:
            logger.warning("Could not load followed migrations: %s", type(e).__name__)
            return
        if not raw:
            return
        try:
            loaded = json.loads(raw)
        except ValueError, TypeError:
            logger.warning("Malformed followed_migrations metadata; ignoring")
            return
        if isinstance(loaded, list):
            self._followed_migration_ids = {x for x in loaded if isinstance(x, int)}

    def _is_followed_migration(self, chat_id: int) -> bool:
        """True if ``chat_id`` was adopted via FOLLOW_CHAT_MIGRATIONS (#228)."""
        return self.config.follow_chat_migrations and chat_id in self._followed_migration_ids

    async def _reconcile_migrations(self, dialogs: list, backed_up_chat_ids: set[int]) -> None:
        """Detect group→supergroup migrations and warn or follow them (#228).

        Migration is invisible to the live handlers (Telethon surfaces the
        ``MessageActionChatMigrateTo``/``ChannelMigrateFrom`` service message to
        neither NewMessage nor ChatAction), so the scheduled sweep is the only
        sound detection point. Two sources are combined:

        * PRIMARY — the migrated basic group's ``Chat`` entity is still returned
          in the dialog list and carries ``.migrated_to`` (an InputChannel).
        * SECONDARY — a stored ``chat_migrate_to`` service marker, which covers
          migrations that happened while the archiver was offline (the dead
          basic group may no longer surface as a dialog).

        For each new supergroup id NOT already captured this run / configured /
        followed: when FOLLOW_CHAT_MIGRATIONS is on it is adopted (persisted to
        the metadata KV and backed up immediately this run); otherwise a
        count-only warning fires — re-emitted every run until acted on, so the
        silent capture-stop can never go unnoticed. PII: counts only, never ids.
        """
        try:
            migrations: dict[int, int] = {}

            # PRIMARY: entities the sweep already fetched this run.
            for dialog in dialogs:
                entity = getattr(dialog, "entity", None)
                migrated_to = getattr(entity, "migrated_to", None)
                channel_id = getattr(migrated_to, "channel_id", None) if migrated_to is not None else None
                if channel_id is None:
                    continue
                old_id = self._get_marked_id(entity)
                migrations[old_id] = get_peer_id(PeerChannel(channel_id))

            # SECONDARY: stored markers (migrations that happened while offline).
            try:
                for old_id, new_id in await self.db.get_migration_markers():
                    migrations.setdefault(old_id, new_id)
            except Exception as e:
                logger.warning("Migration marker lookup failed: %s", type(e).__name__)

            if not migrations:
                return

            # Ids the user already arranged to capture (explicit config) or
            # explicitly opted out of (exclude lists take priority — no nag).
            configured = (
                self.config.chat_ids
                | self.config.global_include_ids
                | self.config.groups_include_ids
                | self.config.channels_include_ids
            )
            excluded = (
                self.config.global_exclude_ids | self.config.groups_exclude_ids | self.config.channels_exclude_ids
            )

            out_of_scope: set[int] = set()
            for new_id in migrations.values():
                if new_id in excluded:
                    continue  # user opted the new supergroup out
                if new_id in backed_up_chat_ids or new_id in configured:
                    continue  # already in scope and captured
                # A migrated supergroup is always a megagroup, so ask the
                # type-based filter directly: in all-groups mode (no include
                # list) the new supergroup is naturally in scope and will be
                # captured on its own, so warning about it would be spurious.
                if self.config.should_backup_chat(new_id, is_user=False, is_group=True, is_channel=False, is_bot=False):
                    continue
                if self.config.follow_chat_migrations and new_id in self._followed_migration_ids:
                    continue  # already adopted on a previous run
                out_of_scope.add(new_id)

            if not out_of_scope:
                return

            if self.config.follow_chat_migrations:
                # Adopt: persist first (durable), then capture this run.
                self._followed_migration_ids |= out_of_scope
                try:
                    await self.db.set_metadata("followed_migrations", json.dumps(sorted(self._followed_migration_ids)))
                except Exception as e:
                    logger.warning("Could not persist followed migrations: %s", type(e).__name__)
                captured = 0
                for new_id in out_of_scope:
                    try:
                        if await self._backup_followed_migration(new_id):
                            backed_up_chat_ids.add(new_id)
                            captured += 1
                    except Exception as e:
                        logger.warning("Could not capture a newly-followed supergroup: %s", type(e).__name__)
                logger.info(
                    "FOLLOW_CHAT_MIGRATIONS: adopted %d migrated supergroup(s), captured %d this run",
                    len(out_of_scope),
                    captured,
                )
            else:
                logger.warning(
                    "%d tracked group(s) migrated to a supergroup not in scope; capture stops for them "
                    "until you add the new id to GROUPS_INCLUDE_CHAT_IDS or enable FOLLOW_CHAT_MIGRATIONS",
                    len(out_of_scope),
                )
        except Exception as e:
            logger.warning("Migration reconciliation failed: %s", type(e).__name__)

    async def _backup_followed_migration(self, new_id: int) -> bool:
        """Fetch and back up a newly-adopted supergroup this run (#228).

        Returns True when the supergroup was fetched and backed up, False when it
        is inaccessible (caught and count-only-logged so the sweep never crashes).
        """
        try:
            entity = await call_with_flood_retry(self.client.get_entity, new_id)
        except Exception as e:
            logger.warning("Followed supergroup is inaccessible this run: %s", type(e).__name__)
            return False

        class _FollowedDialog:
            def __init__(self, followed_entity):
                self.entity = followed_entity
                self.date = datetime.now()

        await self._backup_dialog(_FollowedDialog(entity), is_archived=False)
        return True

    @classmethod
    async def create(cls, config: Config, client: TelegramClient | None = None) -> TelegramBackup:
        """
        Factory method to create TelegramBackup with initialized database.

        Args:
            config: Configuration object
            client: Optional existing TelegramClient to use (for shared connection)

        Returns:
            Initialized TelegramBackup instance
        """
        db = await create_adapter()
        return cls(config, db, client=client)

    async def connect(self):
        """
        Connect to Telegram and authenticate.

        If a client was provided in __init__, verifies it's connected.
        Otherwise, creates a new client and connects.
        """
        # If using shared client, just verify it's connected
        if self.client is not None and not self._owns_client:
            if not self.client.is_connected():
                raise RuntimeError("Shared client is not connected")
            logger.debug("Using shared Telegram client")
            return

        # Create new client
        logger.info(f"Using Telethon session database: {self.config.session_path}.session")
        self.client = TelegramClient(
            self.config.session_path,
            self.config.api_id,
            self.config.api_hash,
            **self.config.get_telegram_client_kwargs(),
        )
        self._owns_client = True

        # Fix for database locked errors: Enable WAL mode for session DB
        # This is critical for concurrency when the viewer is also running
        try:
            if hasattr(self.client.session, "_conn"):
                # Ensure connection is open
                if self.client.session._conn is None:
                    # Trigger connection if lazy loaded (though usually it's open)
                    pass

                if self.client.session._conn:
                    self.client.session._conn.execute("PRAGMA journal_mode=WAL")
                    self.client.session._conn.execute("PRAGMA busy_timeout=30000")
                    logger.info("Enabled WAL mode for Telethon session database")
        except Exception as e:
            logger.warning(f"Could not enable WAL mode for session DB: {e}")

        # Connect without starting interactive flow
        await self.client.connect()

        # Check authorization status
        if not await self.client.is_user_authorized():
            logger.error("❌ Session not authorized!")
            logger.error("Please run the authentication setup first:")
            logger.error("  Docker: ./init_auth.bat (Windows) or ./init_auth.sh (Linux/Mac)")
            logger.error("  Local:  python -m src.setup_auth")
            raise RuntimeError("Session not authorized. Please run authentication setup.")

        me = await self.client.get_me()
        logger.info(f"Connected as {me.first_name} ({me.phone})")

    async def disconnect(self):
        """
        Disconnect from Telegram.

        Only disconnects if we own the client (created it ourselves).
        Shared clients are managed by the connection owner.
        """
        if self.client and self._owns_client:
            await self.client.disconnect()
            logger.info("Disconnected from Telegram")

    async def backup_all(self):
        """
        Perform backup of all configured chats.
        This is the main entry point for scheduled backups.
        """
        try:
            logger.info("Starting backup process...")

            # Connect to Telegram
            logger.info("Connecting to Telegram...")
            await self.client.start(phone=self.config.phone)

            # Get current user info
            me = await self.client.get_me()
            logger.info(f"Logged in as {me.first_name} ({me.id})")

            # Store owner ID and backfill is_outgoing for existing messages
            await self.db.set_metadata("owner_id", str(me.id))
            await self.db.backfill_is_outgoing(me.id)

            start_time = datetime.now()

            # Store last backup time in UTC at the START of backup (not when it finishes)
            last_backup_time = utcnow_naive().isoformat() + "Z"
            await self.db.set_metadata("last_backup_time", last_backup_time)

            # Mark a backup as in progress so the viewer can show a "backing up"
            # indicator and treat partial stats as expected (issue #200). Cleared
            # in the finally block below, even if the backup raises.
            await self.db.set_metadata("backup_in_progress", "1")

            # Reset the reaction re-sweep pacing state and load the cycle cursor
            # (which chats already completed after a deferred run — #224).
            await self._load_resweep_cycle()

            # Load the set of supergroups we already adopted after a group→
            # supergroup migration (#228) so they are treated as in-scope this
            # run. Only when FOLLOW_CHAT_MIGRATIONS is on — when off nothing is
            # ever persisted and this stays empty (warning-only behaviour).
            await self._load_followed_migrations()

            # Whitelist mode: skip expensive get_dialogs() and fetch only the
            # specified chats directly.  For accounts with many dialogs the full
            # dialog fetch can hang indefinitely (see #95).
            if self.config.whitelist_mode:
                logger.info(f"Whitelist mode: fetching {len(self.config.chat_ids)} chat(s) directly")
                filtered_dialogs = []
                archived_chat_ids = set()
                archived_dialogs = []
                explicitly_excluded_chat_ids = set()
                seen_chat_ids = set()
                # Adopted-migration supergroups (#228) are captured even in
                # whitelist mode so a followed group keeps flowing after upgrade.
                # Honor the exclude lists for the followed additions (the explicit
                # CHAT_IDS whitelist itself is never exclude-filtered). Only touch
                # the exclude sets when there is actually something followed.
                followed_to_fetch = self._followed_migration_ids
                if followed_to_fetch:
                    followed_to_fetch = followed_to_fetch - (
                        self.config.global_exclude_ids
                        | self.config.groups_exclude_ids
                        | self.config.channels_exclude_ids
                    )
                for cid in self.config.chat_ids | followed_to_fetch:
                    try:
                        entity = await call_with_flood_retry(self.client.get_entity, cid)

                        class SimpleDialog:
                            def __init__(self, entity):
                                self.entity = entity
                                self.date = datetime.now()

                        filtered_dialogs.append(SimpleDialog(entity))
                        seen_chat_ids.add(cid)
                        logger.info("  → Fetched chat")
                    except Exception as e:
                        logger.warning(f"  → Could not fetch chat: {e}")

            else:
                # Type-based mode: fetch full dialog list and filter
                logger.info("Fetching dialog list...")
                dialogs = await self._get_dialogs()
                logger.info(f"Found {len(dialogs)} total dialogs")

                # v6.2.0: Fetch archived dialogs
                logger.info("Fetching archived dialogs...")
                archived_dialogs = await self._get_dialogs(archived=True)
                logger.info(f"Found {len(archived_dialogs)} archived dialogs")

                # Build set of archived chat IDs for fast lookup.
                # Only trust this for chats NOT found in the regular dialog list,
                # since Telegram's API may occasionally return a chat in both lists.
                archived_chat_ids = set()
                for dialog in archived_dialogs:
                    archived_chat_ids.add(self._get_marked_id(dialog.entity))
                archived_matching_includes = archived_chat_ids & (
                    self.config.global_include_ids
                    | self.config.private_include_ids
                    | self.config.groups_include_ids
                    | self.config.channels_include_ids
                )
                logger.info(f"Archived chats matching includes: {len(archived_matching_includes)}")

                # Filter dialogs based on chat type and ID filters
                # Also delete explicitly excluded chats from database
                filtered_dialogs = []
                explicitly_excluded_chat_ids = set()
                seen_chat_ids = set()  # Track which IDs we've processed from dialogs

                for dialog in dialogs:
                    entity = dialog.entity
                    # Use marked ID (with -100 prefix for channels/supergroups) to match user config
                    chat_id = self._get_marked_id(entity)
                    seen_chat_ids.add(chat_id)

                    is_bot = isinstance(entity, User) and entity.bot
                    is_user = isinstance(entity, User) and not entity.bot
                    is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and entity.megagroup)
                    is_channel = isinstance(entity, Channel) and not entity.megagroup

                    # Check if chat is explicitly in an exclude list (not just filtered out)
                    is_explicitly_excluded = (
                        chat_id in self.config.global_exclude_ids
                        or ((is_user or is_bot) and chat_id in self.config.private_exclude_ids)
                        or (is_group and chat_id in self.config.groups_exclude_ids)
                        or (is_channel and chat_id in self.config.channels_exclude_ids)
                    )

                    if is_explicitly_excluded:
                        # Chat is explicitly excluded - mark for deletion
                        explicitly_excluded_chat_ids.add(chat_id)
                    elif self.config.should_backup_chat(chat_id, is_user, is_group, is_channel, is_bot):
                        # Chat should be backed up
                        filtered_dialogs.append(dialog)
                    elif self._is_followed_migration(chat_id):
                        # Adopted after a group→supergroup migration (#228): in
                        # scope even though it is not in any user include list.
                        filtered_dialogs.append(dialog)

                # Fetch explicitly included chats that weren't in dialogs
                # This handles cases where chats don't appear in the dialog list
                # (newly created, archived, or not recently messaged)
                all_include_ids = (
                    self.config.global_include_ids
                    | self.config.private_include_ids
                    | self.config.groups_include_ids
                    | self.config.channels_include_ids
                )
                # Followed migrations (#228) are fetched explicitly too, so an
                # adopted supergroup that no longer surfaces in the dialog list
                # (e.g. not recently active) is still captured.
                missing_include_ids = (
                    (all_include_ids | self._followed_migration_ids) - seen_chat_ids - explicitly_excluded_chat_ids
                )

                if missing_include_ids:
                    logger.info(f"Fetching {len(missing_include_ids)} explicitly included chats not in regular dialogs")
                    for include_id in missing_include_ids:
                        is_in_archive = include_id in archived_chat_ids
                        try:
                            entity = await call_with_flood_retry(self.client.get_entity, include_id)

                            class SimpleDialog:
                                def __init__(self, entity):
                                    self.entity = entity
                                    self.date = datetime.now()

                            filtered_dialogs.append(SimpleDialog(entity))
                            logger.info(
                                f"  → Added chat{' [in archive]' if is_in_archive else ' [not in any dialog list]'}"
                            )
                        except Exception as e:
                            logger.warning(f"  → Could not fetch included chat: {e}")

                # Delete only explicitly excluded chats from database
                if explicitly_excluded_chat_ids:
                    logger.info(
                        f"Deleting {len(explicitly_excluded_chat_ids)} explicitly excluded chats from database..."
                    )
                    for chat_id in explicitly_excluded_chat_ids:
                        try:
                            await self.db.delete_chat_and_related_data(chat_id, self.config.media_path)
                        except Exception as e:
                            logger.error(f"Error deleting chat: {e}", exc_info=True)

            logger.info(f"Backing up {len(filtered_dialogs)} dialogs after filtering")

            if not filtered_dialogs:
                logger.info("No dialogs to back up after filtering")
                return

            # Sort dialogs: priority chats first, then by most recently active
            # Priority chats (PRIORITY_CHAT_IDS) are always processed first
            # Use .timestamp() to avoid comparing timezone-aware vs naive datetimes
            # (Saved Messages chat has UTC timezone, others may be naive)
            # Fixes: https://github.com/GeiserX/Telegram-Archive/issues/12
            priority_ids = self.config.priority_chat_ids

            def dialog_sort_key(d):
                chat_id = self._get_marked_id(d.entity)
                is_priority = chat_id in priority_ids
                timestamp = (getattr(d, "date", None) or datetime.min.replace(tzinfo=UTC)).timestamp()
                # Sort by: (not is_priority, -timestamp) so priority=True sorts first, then by recency
                return (not is_priority, -timestamp)

            filtered_dialogs.sort(key=dialog_sort_key)

            # Log priority chats if any
            if priority_ids:
                priority_count = sum(1 for d in filtered_dialogs if self._get_marked_id(d.entity) in priority_ids)
                if priority_count > 0:
                    logger.info(f"📌 {priority_count} priority chat(s) will be processed first")

            # Detect whether we've already completed at least one full backup run
            # (i.e. some chats have a non-zero last_message_id recorded)
            has_synced_before = False
            for dialog in filtered_dialogs:
                if await self.db.get_last_message_id(self._get_marked_id(dialog.entity)) > 0:
                    has_synced_before = True
                    break

            # Backup each dialog
            # v6.2.0: Check archived_chat_ids so chats in both INCLUDE_CHAT_IDS
            # and the archived folder get the correct is_archived flag immediately.
            # A chat found in the regular dialog list (seen_chat_ids) is NEVER
            # archived, even if Telegram's API also returns it in folder=1.
            total_messages = 0
            backed_up_chat_ids = set()
            for i, dialog in enumerate(filtered_dialogs, 1):
                entity = dialog.entity
                chat_id = self._get_marked_id(entity)
                is_archived = chat_id in archived_chat_ids and chat_id not in seen_chat_ids
                if chat_id in archived_chat_ids and chat_id in seen_chat_ids:
                    logger.warning(
                        "  Chat appears in both regular and archived dialog lists - treating as NOT archived"
                    )
                logger.info(f"[{i}/{len(filtered_dialogs)}] Backing up{' (archived)' if is_archived else ''}")

                try:
                    message_count = await self._backup_dialog(dialog, is_archived=is_archived)
                    total_messages += message_count
                    backed_up_chat_ids.add(chat_id)
                    logger.info(f"  → Backed up {message_count} new messages")

                    # Optimization: after initial full run, if the most recently
                    # active chat has no new messages, we assume the rest don't either.

                except (ChannelPrivateError, ChatForbiddenError, UserBannedInChannelError) as e:
                    logger.warning(f"  → Skipped (no access): {e.__class__.__name__}")
                except Exception as e:
                    logger.error(f"  → Error backing up chat: {e}", exc_info=True)

            # v6.2.0: Backup archived dialogs that weren't already processed above.
            # Apply the same chat type/ID filters so we don't back up unintended chats.
            archived_to_backup = []
            for dialog in archived_dialogs:
                entity = dialog.entity
                chat_id = self._get_marked_id(entity)
                if chat_id in backed_up_chat_ids:
                    continue  # Already backed up with correct is_archived flag
                if chat_id in explicitly_excluded_chat_ids:
                    continue

                is_bot = isinstance(entity, User) and entity.bot
                is_user = isinstance(entity, User) and not entity.bot
                is_group = isinstance(entity, Chat) or (isinstance(entity, Channel) and entity.megagroup)
                is_channel = isinstance(entity, Channel) and not entity.megagroup

                if self.config.should_backup_chat(
                    chat_id, is_user, is_group, is_channel, is_bot
                ) or self._is_followed_migration(chat_id):
                    archived_to_backup.append(dialog)

            if archived_to_backup:
                logger.info(f"Backing up {len(archived_to_backup)} additional archived dialogs...")
                for i, dialog in enumerate(archived_to_backup, 1):
                    entity = dialog.entity
                    chat_id = self._get_marked_id(entity)
                    logger.info(f"  [Archived {i}/{len(archived_to_backup)}]")

                    try:
                        message_count = await self._backup_dialog(dialog, is_archived=True)
                        total_messages += message_count
                        backed_up_chat_ids.add(chat_id)
                        if message_count > 0:
                            logger.info(f"    → Backed up {message_count} new messages")
                    except (ChannelPrivateError, ChatForbiddenError, UserBannedInChannelError) as e:
                        logger.warning(f"    → Skipped (no access): {e.__class__.__name__}")
                    except Exception as e:
                        logger.error(f"    → Error: {e}", exc_info=True)
            else:
                logger.info("No additional archived dialogs to back up")

            # Persist (deferred run) or complete (clean run) the re-sweep cycle
            # (#224) — directly after the dialog loops, so a later failure in
            # topics/folders/statistics cannot drop the cursor update.
            await self._finalize_resweep_cycle()

            # Reconcile group→supergroup migrations (#228): warn (count-only)
            # about tracked groups that migrated out of scope, and — when
            # FOLLOW_CHAT_MIGRATIONS is on — adopt + capture the new supergroup.
            # Guarded internally so it can never abort folders/stats below.
            await self._reconcile_migrations(list(filtered_dialogs) + list(archived_to_backup), backed_up_chat_ids)

            # v6.2.0: Backup forum topics for forum-enabled chats.
            # Idempotent backstop to the early per-dialog fetch in _backup_dialog
            # (issue #200): re-runs after messages exist so the message-inference
            # fallback can fill in any topics the API path missed.
            logger.info("Checking for forum topics...")
            all_backed_up_dialogs = list(filtered_dialogs) + list(archived_to_backup)
            for dialog in all_backed_up_dialogs:
                entity = dialog.entity
                if isinstance(entity, Channel) and getattr(entity, "forum", False):
                    chat_id = self._get_marked_id(entity)
                    try:
                        await self._backup_forum_topics(chat_id, entity)
                    except Exception as e:
                        # Don't let a topic-fetch failure abort folders/stats below.
                        logger.warning(f"End-of-run forum-topic fetch failed (will retry next run): {e}")

            # v6.2.0: Backup user's chat folders
            logger.info("Backing up chat folders...")
            await self._backup_folders()

            # Calculate and cache statistics (also updates metadata for the viewer)
            duration = (datetime.now() - start_time).total_seconds()
            stats = await self.db.calculate_and_store_statistics(storage_path=self.config.backup_path)

            # Note: last_backup_time is stored at the START of backup (see beginning of backup_all)

            logger.info("=" * 60)
            logger.info("Backup completed successfully!")
            logger.info(f"Duration: {duration:.2f} seconds")
            logger.info(f"New messages: {total_messages}")
            logger.info(f"Total chats: {stats['chats']}")
            logger.info(f"Total messages: {stats['messages']}")
            logger.info(f"Total media files: {stats['media_files']}")
            logger.info(f"Total storage: {stats['total_size_mb']} MB")
            logger.info("=" * 60)

            # Retry previously failed media downloads
            await self._retry_pending_media_downloads()

            # Run media verification if enabled
            if self.config.verify_media:
                await self._verify_and_redownload_media()

        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            raise
        finally:
            # Always clear the in-progress flag, even on failure, so the viewer
            # doesn't show a stuck "backing up" indicator after a crash (#200).
            try:
                await self.db.set_metadata("backup_in_progress", "0")
            except Exception as e:
                logger.warning(f"Failed to clear backup_in_progress flag: {e}")

    async def _get_dialogs(self, archived: bool = False) -> list:
        """
        Get all dialogs (chats) from Telegram.

        Args:
            archived: If True, fetch archived dialogs (folder=1)

        Returns:
            List of dialog objects

        Note: folder=0 explicitly fetches non-archived dialogs only.
        Without folder parameter, Telethon returns ALL dialogs including
        archived ones, which causes overlap with the folder=1 results.
        """
        if archived:
            dialogs = await call_with_flood_retry(self.client.get_dialogs, folder=1)
        else:
            dialogs = await call_with_flood_retry(self.client.get_dialogs, folder=0)
        return dialogs

    async def _verify_and_redownload_media(self) -> None:
        """
        Verify all media files on disk and re-download missing/corrupted ones.

        This method:
        1. Queries all media records marked as downloaded
        2. Checks if files exist on disk
        3. Optionally verifies file size matches DB record
        4. Re-downloads missing/corrupted files from Telegram

        Edge cases handled:
        - File missing on disk: re-download
        - File is 0 bytes: re-download (interrupted download)
        - File size mismatch: re-download (corrupted)
        - Message deleted on Telegram: log warning, skip
        - Chat inaccessible: log warning, skip chat
        - Media expired: log warning, skip
        """
        logger.info("=" * 60)
        logger.info("Starting media verification...")

        media_records = await self.db.get_media_for_verification()
        logger.info(f"Found {len(media_records)} media records to verify")

        missing_files = []
        corrupted_files = []
        skipped_symlinks = 0

        # Phase 1: Check which files need re-downloading
        for record in media_records:
            file_path = record.get("file_path")
            if not file_path:
                continue

            # Detect "truly missing" via lexists so an existing symlink
            # whose ultimate target is unreachable (e.g. git-annex object
            # outside the bind mount) is not flagged for re-download.
            # Re-downloading it would atomic-rename a regular file on top
            # of the symlink, mutating an archived working tree (issue #143).
            if not os.path.lexists(file_path):
                missing_files.append(record)
                continue

            # Trust symlinks: their content is managed externally and may
            # be unreachable from this process. We cannot meaningfully
            # check size or emptiness without following the link.
            if os.path.islink(file_path):
                skipped_symlinks += 1
                continue

            # Check if file is empty (interrupted download)
            if os.path.getsize(file_path) == 0:
                corrupted_files.append(record)
                continue

            # Check file size matches (if we have the expected size)
            expected_size = record.get("file_size")
            if expected_size and expected_size > 0:
                actual_size = os.path.getsize(file_path)
                # Allow 1% tolerance for size differences (encoding variations)
                if abs(actual_size - expected_size) > expected_size * 0.01:
                    corrupted_files.append(record)

        total_issues = len(missing_files) + len(corrupted_files)
        if total_issues == 0:
            msg = "✓ All media files verified - no issues found"
            if skipped_symlinks:
                msg += f" ({skipped_symlinks} symlink entries skipped)"
            logger.info(msg)
            logger.info("=" * 60)
            return

        logger.info(f"Found {len(missing_files)} missing files, {len(corrupted_files)} corrupted files")
        logger.info("Starting re-download process...")

        # Phase 2: Re-download missing/corrupted files
        files_to_redownload = missing_files + corrupted_files

        # Group by chat_id for efficient fetching
        by_chat: dict[int, list[dict]] = {}
        for record in files_to_redownload:
            chat_id = record.get("chat_id")
            if chat_id:
                by_chat.setdefault(chat_id, []).append(record)

        redownloaded = 0
        failed = 0

        for chat_id, records in by_chat.items():
            # Skip media verification for chats in skip list
            if chat_id in self.config.skip_media_chat_ids:
                logger.debug("Skipping media verification for chat (in SKIP_MEDIA_CHAT_IDS)")
                continue

            try:
                # Get message IDs to fetch
                message_ids = [r["message_id"] for r in records if r.get("message_id")]
                if not message_ids:
                    continue

                # Fetch messages from Telegram in batch
                try:
                    messages = await call_with_flood_retry(self.client.get_messages, chat_id, ids=message_ids)
                except Exception as e:
                    logger.warning(f"Cannot access chat for media verification: {e}")
                    failed += len(records)
                    continue

                # Create a map of message_id -> message
                msg_map = {}
                for msg in messages:
                    if msg:  # msg can be None if message was deleted
                        msg_map[msg.id] = msg

                # Re-download each file
                for record in records:
                    msg_id = record.get("message_id")
                    msg = msg_map.get(msg_id)

                    if not msg:
                        logger.warning("Message was deleted - cannot recover media")
                        failed += 1
                        continue

                    if not msg.media:
                        logger.warning("Message no longer has media - cannot recover")
                        failed += 1
                        continue

                    try:
                        # Delete corrupted file if exists (lexists catches dangling symlinks)
                        file_path = record.get("file_path")
                        if file_path and os.path.lexists(file_path):
                            os.remove(file_path)

                        # Re-download using existing method
                        result = await self._process_media(msg, chat_id)
                        if result and result.get("downloaded"):
                            # Insert media record (message already exists for re-downloads)
                            await self.db.insert_media(result)
                            redownloaded += 1
                            logger.debug("Re-downloaded media for message")
                        else:
                            failed += 1
                            logger.warning("Failed to re-download media for message")
                    except Exception as e:
                        failed += 1
                        logger.error(f"Error re-downloading media for message: {e}")

            except Exception as e:
                logger.error(f"Error processing chat for media verification: {e}")
                failed += len(records)

        logger.info("=" * 60)
        logger.info("Media verification completed!")
        logger.info(f"Re-downloaded: {redownloaded} files")
        logger.info(f"Failed/Unrecoverable: {failed} files")
        logger.info("=" * 60)

    async def _retry_pending_media_downloads(self) -> None:
        """Retry downloading media that previously failed.

        Picks up records with downloaded=0 (excluding metadata-only types
        like contact/geo/poll) and re-attempts the download from Telegram.
        Respects MAX_MEDIA_SIZE_BYTES — files that still exceed the limit
        are skipped silently.
        """
        pending = await self.db.get_pending_media_downloads(
            self.config.get_max_media_size_bytes(), self.config.max_media_download_attempts
        )
        # Surface (don't silently swallow) files given up after hitting the retry cap —
        # the silent-loss failure mode #212 was about. Count only (no chat/file names, PII).
        capped = await self.db.count_capped_media_downloads(self.config.max_media_download_attempts)
        if capped:
            logger.warning(
                f"{capped} media file(s) permanently skipped after "
                f"{self.config.max_media_download_attempts} failed download attempts "
                f"(raise MEDIA_MAX_DOWNLOAD_ATTEMPTS to retry them)"
            )
        if not pending:
            return

        logger.info("=" * 60)
        logger.info(f"Retrying {len(pending)} pending media downloads...")

        # Group by chat_id for efficient batch fetching
        by_chat: dict[int, list[dict]] = {}
        for record in pending:
            chat_id = record.get("chat_id")
            if chat_id:
                by_chat.setdefault(chat_id, []).append(record)

        downloaded = 0
        skipped = 0
        failed = 0

        for chat_id, records in by_chat.items():
            if chat_id in self.config.skip_media_chat_ids:
                skipped += len(records)
                continue

            try:
                message_ids = [r["message_id"] for r in records if r.get("message_id")]
                if not message_ids:
                    continue

                try:
                    messages = await call_with_flood_retry(self.client.get_messages, chat_id, ids=message_ids)
                except Exception as e:
                    logger.warning(f"Cannot access chat for pending media retry: {e}")
                    failed += len(records)
                    continue

                msg_map = {}
                for msg in messages:
                    if msg:
                        msg_map[msg.id] = msg

                for record in records:
                    msg_id = record.get("message_id")
                    msg = msg_map.get(msg_id)

                    if not msg:
                        skipped += 1
                        continue

                    if not msg.media:
                        skipped += 1
                        continue

                    # Re-attempt _process_media (which handles size checks internally).
                    # Count each unsuccessful re-attempt so a permanently-failing file
                    # (e.g. a filename too long for the target filesystem, #212) stops
                    # being re-fetched once it hits MEDIA_MAX_DOWNLOAD_ATTEMPTS.
                    try:
                        result = await self._process_media(msg, chat_id)
                        if result and result.get("downloaded"):
                            await self.db.insert_media(result)
                            downloaded += 1
                        else:
                            await self.db.increment_media_download_attempts(record["id"])
                            skipped += 1
                    except Exception as e:
                        logger.debug(f"Retry failed for pending media: {e}")
                        await self.db.increment_media_download_attempts(record["id"])
                        failed += 1

            except Exception as e:
                logger.error(f"Error retrying pending media for chat: {e}")
                failed += len(records)

        if downloaded > 0 or failed > 0:
            logger.info(f"Pending media retry: {downloaded} downloaded, {skipped} skipped, {failed} failed")
        else:
            logger.info("Pending media retry: no actionable items")
        logger.info("=" * 60)

    async def _backup_dialog(self, dialog, is_archived: bool = False) -> int:
        """
        Backup a single dialog (chat).

        Args:
            dialog: Dialog object from Telegram
            is_archived: Whether this dialog is from the archived folder

        Returns:
            Number of new messages backed up
        """
        entity = dialog.entity
        # Use marked ID (with -100 prefix for channels/supergroups) for consistency
        chat_id = self._get_marked_id(entity)

        # Save chat information
        chat_data = self._extract_chat_data(entity, is_archived=is_archived)
        await self.db.upsert_chat(chat_data)

        # Fetch forum topics early (cheap, message-independent API call) so the viewer
        # shows the topic list immediately, before the slow media backfill (issue #200).
        # Same forum-detection guard as the end-of-run backstop loop in backup_all.
        if isinstance(entity, Channel) and getattr(entity, "forum", False):
            try:
                await self._backup_forum_topics(chat_id, entity)
            except Exception as e:
                logger.warning(f"Early forum-topic fetch failed for chat (will retry at end of run): {e}")

        # Clean up existing media if this chat is in the skip list (once per session)
        if (
            chat_id in self.config.skip_media_chat_ids
            and self.config.skip_media_delete_existing
            and chat_id not in self._cleaned_media_chats
        ):
            await self._cleanup_existing_media(chat_id)
            self._cleaned_media_chats.add(chat_id)

        # Ensure profile photos for users and groups/channels are backed up.
        # This runs on every dialog backup but only downloads new files when
        # Telegram reports a different profile photo.
        try:
            await self._ensure_profile_photo(entity, chat_id)
        except Exception as e:
            logger.error(f"Error downloading profile photo: {e}", exc_info=True)

        # Get last synced message ID for incremental backup
        last_message_id = await self.db.get_last_message_id(chat_id)

        # Fetch and process messages in batches with periodic checkpointing.
        # sync_status is updated every checkpoint_interval batches so that
        # a crash/restart only re-fetches messages since the last checkpoint
        # instead of restarting the entire chat from scratch.
        batch_data: list[dict] = []
        batch_size = self.config.batch_size
        checkpoint_interval = self.config.checkpoint_interval
        grand_total = 0
        uncheckpointed_count = 0
        batches_since_checkpoint = 0
        running_max_id = last_message_id

        async for message in iter_messages_with_flood_retry(self.client, entity, min_id=last_message_id, reverse=True):
            running_max_id = max(running_max_id, message.id)

            # Skip messages belonging to excluded forum topics
            if self.config.should_skip_topic(chat_id, extract_topic_id(message)):
                continue

            msg_data = await self._process_message(message, chat_id)
            batch_data.append(msg_data)

            if len(batch_data) >= batch_size:
                await self._commit_batch(batch_data, chat_id)
                count = len(batch_data)
                grand_total += count
                uncheckpointed_count += count
                batches_since_checkpoint += 1
                logger.info(f"  → Processed {grand_total} messages...")

                if batches_since_checkpoint >= checkpoint_interval:
                    await self.db.update_sync_status(chat_id, running_max_id, uncheckpointed_count)
                    uncheckpointed_count = 0
                    batches_since_checkpoint = 0

                batch_data = []

        # Flush remaining messages
        if batch_data:
            await self._commit_batch(batch_data, chat_id)
            count = len(batch_data)
            grand_total += count
            uncheckpointed_count += count

        # Final checkpoint: persist when there are un-checkpointed messages OR
        # when the cursor advanced purely from skipped (topic-filtered) messages
        # that were never counted in uncheckpointed_count.
        if uncheckpointed_count > 0 or (grand_total == 0 and running_max_id > last_message_id):
            await self.db.update_sync_status(chat_id, running_max_id, uncheckpointed_count)

        # Sync deletions and edits if enabled (expensive!)
        if self.config.sync_deletions_edits:
            await self._sync_deletions_and_edits(chat_id, entity)

        # Always sync pinned messages to keep them up-to-date
        await self._sync_pinned_messages(chat_id, entity)

        # Bounded reaction re-sweep (opt-in): recover self-reactions Telegram never
        # pushed to this session by re-checking the last N days of messages (#221).
        if self.config.reaction_resweep_days > 0:
            await self._resweep_reactions(entity, chat_id)

        return grand_total

    async def _commit_batch(self, batch_data: list[dict], chat_id: int) -> None:
        """Persist a batch of processed messages, their media and reactions to the DB."""
        await self.db.insert_messages_batch(batch_data)

        for msg in batch_data:
            if msg.get("_media_data"):
                await self.db.insert_media(msg["_media_data"])

        for msg in batch_data:
            # Reconcile reactions for every processed message, including those whose
            # snapshot is empty ([]), so removals-to-zero on re-fetched messages
            # persist instead of leaving stale rows (#219). reconcile_reactions is
            # idempotent (a stable message re-scans to a no-op) and preserves
            # created_at. A None snapshot means extraction FAILED (shape drift) —
            # skip rather than tombstone valid rows.
            observed = msg.get("reactions")
            if observed is not None:
                await self.db.reconcile_reactions(msg["id"], chat_id, observed, mark_removed=True)

    async def _fill_gap_range(self, entity, chat_id: int, gap_start: int, gap_end: int) -> int:
        """
        Fetch and store messages for a single gap range.

        Args:
            entity: Telegram entity for the chat
            chat_id: Chat identifier
            gap_start: Last message ID before the gap
            gap_end: First message ID after the gap

        Returns:
            Number of recovered messages
        """
        batch_data: list[dict] = []
        batch_size = self.config.batch_size
        recovered = 0

        async for message in iter_messages_with_flood_retry(
            self.client, entity, min_id=gap_start, max_id=gap_end, reverse=True
        ):
            # Skip messages belonging to excluded forum topics
            if self.config.should_skip_topic(chat_id, extract_topic_id(message)):
                continue

            msg_data = await self._process_message(message, chat_id)
            batch_data.append(msg_data)

            if len(batch_data) >= batch_size:
                await self._commit_batch(batch_data, chat_id)
                recovered += len(batch_data)
                batch_data = []

        # Flush remaining messages
        if batch_data:
            await self._commit_batch(batch_data, chat_id)
            recovered += len(batch_data)

        return recovered

    async def _fill_gaps(self, chat_id: int | None = None) -> dict:
        """
        Detect and fill gaps in message ID sequences.

        Scans chats for missing message ID ranges and fetches them from Telegram.

        Args:
            chat_id: If provided, scan only this chat. Otherwise scan all chats.

        Returns:
            Summary dict with gap-fill statistics.
        """
        threshold = self.config.gap_threshold
        summary = {
            "chats_scanned": 0,
            "chats_with_gaps": 0,
            "total_gaps": 0,
            "total_recovered": 0,
            "errors": 0,
            "details": [],
        }

        if chat_id is not None:
            chat_ids = [chat_id]
        else:
            # Only scan chats that current config would back up (respects
            # CHAT_IDS whitelist, CHAT_TYPES, and all exclude lists)
            all_chat_ids = await self.db.get_chats_with_messages()
            chat_ids = []
            for cid in all_chat_ids:
                chat_info = await self.db.get_chat_by_id(cid)
                if not chat_info:
                    continue
                ctype = chat_info.get("type", "")
                is_user = ctype == "private"
                is_group = ctype in ("group", "supergroup")
                is_channel = ctype == "channel"
                is_bot = ctype == "bot"
                if self.config.should_backup_chat(cid, is_user, is_group, is_channel, is_bot):
                    chat_ids.append(cid)

        logger.info(f"Gap-fill: scanning {len(chat_ids)} chat(s) with threshold={threshold}")

        for cid in chat_ids:
            summary["chats_scanned"] += 1

            try:
                entity = await call_with_flood_retry(self.client.get_entity, cid)
            except (ChannelPrivateError, ChatForbiddenError, UserBannedInChannelError) as e:
                logger.warning(f"Gap-fill: skipping chat (no access): {e.__class__.__name__}")
                continue
            except Exception as e:
                logger.error(f"Gap-fill: failed to get entity for chat: {e}")
                summary["errors"] += 1
                continue

            chat_name = self._get_chat_name(entity)

            try:
                gaps = await self.db.detect_message_gaps(cid, threshold)
            except Exception as e:
                logger.error(f"Gap-fill: failed to detect gaps for chat: {e}")
                summary["errors"] += 1
                continue

            if not gaps:
                continue

            summary["chats_with_gaps"] += 1
            chat_recovered = 0

            logger.info(f"Gap-fill: chat has {len(gaps)} gap(s)")

            for gap_start, gap_end, gap_size in gaps:
                logger.info(f"  → Filling gap (size {gap_size})")
                try:
                    recovered = await self._fill_gap_range(entity, cid, gap_start, gap_end)
                    chat_recovered += recovered
                    logger.info(f"    Recovered {recovered} messages")
                except Exception as e:
                    logger.error(f"    Error filling gap (size {gap_size}): {e}")
                    summary["errors"] += 1

            summary["total_gaps"] += len(gaps)
            summary["total_recovered"] += chat_recovered
            summary["details"].append(
                {
                    "chat_id": cid,
                    "chat_name": chat_name,
                    "gaps": len(gaps),
                    "recovered": chat_recovered,
                }
            )

        status = "complete" if summary["errors"] == 0 else "complete with errors"
        logger.info(
            f"Gap-fill {status}: {summary['chats_scanned']} chats scanned, "
            f"{summary['total_gaps']} gaps found, {summary['total_recovered']} messages recovered"
            + (f", {summary['errors']} error(s)" if summary["errors"] else "")
        )

        return summary

    async def _sync_deletions_and_edits(self, chat_id: int, entity):
        """
        Sync deletions and edits for existing messages in the database.

        Args:
            chat_id: Chat ID to sync
            entity: Telegram entity
        """
        logger.info("  → Syncing deletions and edits for chat...")

        # Get all local message IDs and their edit dates
        local_messages = await self.db.get_messages_sync_data(chat_id)
        if not local_messages:
            return

        local_ids = list(local_messages.keys())
        total_checked = 0
        total_deleted = 0
        total_updated = 0

        # Process in batches
        batch_size = 100
        for i in range(0, len(local_ids), batch_size):
            batch_ids = local_ids[i : i + batch_size]

            try:
                # Fetch current state from Telegram
                remote_messages = await call_with_flood_retry(self.client.get_messages, entity, ids=batch_ids)

                for msg_id, remote_msg in zip(batch_ids, remote_messages):
                    # Check for deletion
                    if remote_msg is None:
                        if getattr(self.config, "deletion_mode", "hard") == "soft":
                            # mark_message_deleted defaults deleted_at to now(UTC); this path
                            # doesn't broadcast, so no need to pass an explicit timestamp.
                            await self.db.mark_message_deleted(chat_id, msg_id)
                        else:
                            await self.db.delete_message(chat_id, msg_id)
                        total_deleted += 1
                        continue

                    # Check for edits. Telethon delivers tz-aware UTC datetimes
                    # while the archive stores naive UTC — normalize before
                    # comparing, otherwise every previously-edited message looks
                    # changed on every sync pass and pays a pointless locked
                    # update round-trip.
                    remote_edit_date = remote_msg.edit_date
                    if remote_edit_date is not None and remote_edit_date.tzinfo is not None:
                        remote_edit_date = remote_edit_date.replace(tzinfo=None)
                    local_edit_date = local_messages[msg_id]

                    if remote_edit_date and remote_edit_date != local_edit_date:
                        # Update text and edit_date; count only edits the archive
                        # actually accepted (the adapter re-checks under lock).
                        outcome = await self.db.update_message_text(
                            chat_id, msg_id, remote_msg.message, remote_msg.edit_date
                        )
                        if outcome == "applied":
                            total_updated += 1

                    # Piggyback reaction reconcile (#221): the full message is already
                    # in hand, so harvest its reactions at zero extra API cost. Skip
                    # None (extraction failure) and min payloads (partial; may omit the
                    # account's own reaction → false tombstone). PII: aggregate only.
                    reactions_obj = getattr(remote_msg, "reactions", None)
                    if not getattr(reactions_obj, "min", False):
                        observed = extract_reactions(reactions_obj)
                        if observed is not None:
                            await self.db.reconcile_reactions(msg_id, chat_id, observed, mark_removed=True)

            except Exception as e:
                logger.error(f"Error syncing batch for chat: {e}")

            total_checked += len(batch_ids)
            if total_checked % 1000 == 0:
                logger.info(f"  → Checked {total_checked}/{len(local_ids)} messages for sync...")

        if total_deleted > 0 or total_updated > 0:
            logger.info(f"  → Sync result: {total_deleted} deleted, {total_updated} updated")

    def _ensure_resweep_state(self) -> None:
        """Lazy-init the per-run re-sweep pacing/deferral state (#224).

        ``backup_all`` resets this at the start of every run; the lazy init keeps
        direct ``_backup_dialog`` callers (and tests built via ``__new__``) safe.
        """
        if not hasattr(self, "_resweep_flood_until"):
            self._resweep_flood_until: float | None = None
            self._resweep_flood_count = 0
            self._resweep_hard_deferred = False
            self._resweep_deferred_any = False
            self._resweep_last_request_ts: float | None = None
            self._resweep_dialogs_deferred = 0
            self._resweep_cycle_done: set[int] = set()
            self._resweep_partial: dict[int, int] = {}

    async def _resweep_pace(self) -> None:
        """Global inter-request spacing for the re-sweep, spanning chats (#224).

        getMessagesReactions has a burst-rate flood limit accumulated per
        account+method (not per chat), so the spacing must survive chat
        boundaries: one timestamp on the instance, checked before EVERY re-sweep
        API request (raw or fallback).
        """
        delay = self.config.reaction_resweep_batch_delay_seconds
        if delay > 0 and self._resweep_last_request_ts is not None:
            elapsed = time.monotonic() - self._resweep_last_request_ts
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
        self._resweep_last_request_ts = time.monotonic()

    def _register_resweep_flood(self, seconds: int, chat_id: int, covered: int, source: str) -> None:
        """Record a re-sweep FloodWait: pause, then resume within the run (#224).

        Nothing sleeps and nothing retries. The re-sweep goes quiet until the
        server-requested window (plus ``RESWEEP_FLOOD_RESUME_MARGIN_SECONDS``)
        has elapsed and then resumes with later chats in the same run — chats
        reached while still cooling down skip to the next-run cursor, and the
        flooded chat parks its mid-chat progress there too. Deferring entire
        runs on the first flood over-corrected on small-bucket accounts (a
        ~1-minute window turned into hours of cycle latency). After
        ``RESWEEP_MAX_FLOODS_PER_RUN`` floods in one run the remainder defers
        outright: repeated floods signal a degraded bucket that should be left
        alone until the next scheduled run.
        """
        self._resweep_flood_count += 1
        self._resweep_dialogs_deferred += 1
        self._resweep_deferred_any = True
        self._resweep_partial[chat_id] = covered
        wait_s = max(0, seconds or 0)
        if self._resweep_flood_count >= RESWEEP_MAX_FLOODS_PER_RUN:
            self._resweep_hard_deferred = True
            self._resweep_flood_until = None
            logger.warning(
                "Reaction resweep hit a %s FloodWait (%ss) — flood #%d this run; "
                "deferring the rest of this run's resweep",
                source,
                wait_s,
                self._resweep_flood_count,
            )
            return
        self._resweep_flood_until = time.monotonic() + wait_s + RESWEEP_FLOOD_RESUME_MARGIN_SECONDS
        logger.warning(
            "Reaction resweep hit a %s FloodWait (%ss); pausing, will resume once it expires "
            "(within this run if it ends sooner)",
            source,
            wait_s,
        )

    async def _load_resweep_cycle(self) -> None:
        """Load the re-sweep cycle cursor for this run (#224).

        When a run defers its re-sweep after a FloodWait, the completed chats —
        and the mid-chat progress of the chat that flooded — are persisted so the
        NEXT run resumes where this one stopped instead of re-sweeping the same
        recency-sorted head forever (which would permanently starve the tail, and
        a chat larger than the flood bucket would never finish at all).

        The cursor is discarded when its window setting no longer matches or when
        it is older than 48h (e.g. the feature was disabled and re-enabled weeks
        later): a stale "done" set would silently skip chats for a whole cycle.
        """
        self._resweep_flood_until = None
        self._resweep_flood_count = 0
        self._resweep_hard_deferred = False
        self._resweep_deferred_any = False
        self._resweep_last_request_ts = None
        self._resweep_dialogs_deferred = 0
        self._resweep_cycle_done = set()
        self._resweep_partial = {}
        if self.config.reaction_resweep_days <= 0:
            return
        try:
            raw = await self.db.get_metadata("reaction_resweep_cycle_done")
            if not raw:
                return
            state = json.loads(raw)
            if not isinstance(state, dict):
                return  # legacy/unknown shape: start a fresh cycle
            if state.get("days") != self.config.reaction_resweep_days:
                return  # window changed: the old cycle's coverage is meaningless
            saved_at = datetime.fromisoformat(state.get("saved_at", ""))
            if utcnow_naive() - saved_at > timedelta(hours=48):
                return  # stale (e.g. disabled-then-re-enabled): start fresh
            self._resweep_cycle_done = {int(c) for c in state.get("done", [])}
            self._resweep_partial = {int(c): int(n) for c, n in (state.get("partial") or {}).items()}
        except Exception as e:
            logger.warning("Could not load reaction resweep cycle state: %s", type(e).__name__)
            self._resweep_cycle_done = set()
            self._resweep_partial = {}

    async def _finalize_resweep_cycle(self) -> None:
        """Persist or complete the re-sweep cycle after the dialog loops (#224).

        Called directly after the dialog iteration (not at the very end of
        ``backup_all``) so a later failure in topics/folders/statistics cannot
        drop a deferral or a completed cycle on the floor.
        """
        if self.config.reaction_resweep_days <= 0:
            return
        self._ensure_resweep_state()
        try:
            if self._resweep_deferred_any:
                state = {
                    "saved_at": utcnow_naive().isoformat(),
                    "days": self.config.reaction_resweep_days,
                    "done": sorted(self._resweep_cycle_done),
                    "partial": {str(c): n for c, n in self._resweep_partial.items()},
                }
                await self.db.set_metadata("reaction_resweep_cycle_done", json.dumps(state))
                logger.warning(
                    "Reaction resweep deferred %d dialogs to the next run after FloodWaits; "
                    "%d dialogs are done this cycle",
                    self._resweep_dialogs_deferred,
                    len(self._resweep_cycle_done),
                )
            else:
                # Clean run: the cycle is complete, next run starts fresh.
                await self.db.set_metadata("reaction_resweep_cycle_done", "{}")
        except Exception as e:
            logger.warning("Could not persist reaction resweep cycle state: %s", type(e).__name__)

    async def _resweep_reactions(self, entity, chat_id: int) -> None:
        """Re-check reactions on recent messages to recover self-reactions (#221).

        Telegram does not reliably push ``UpdateMessageReactions`` for reactions the
        archive account makes from ANOTHER device, and the scheduled sweep only
        revisits messages inside its incremental window — so self-reactions on older
        messages are otherwise missed. This opt-in pass (``REACTION_RESWEEP_DAYS`` > 0)
        re-reads the last N days of messages for this chat and reconciles their current
        aggregate, capped at ``REACTION_RESWEEP_MAX_PER_CHAT`` (default 500).

        Pacing (#224): getMessagesReactions has a burst-rate flood limit accumulated
        ACROSS chats (bucket size varies wildly by account), so requests are spaced
        globally by ``REACTION_RESWEEP_BATCH_DELAY_SECONDS``. On a FloodWait the
        re-sweep pauses — nothing sleeps, nothing retries, no fallback onto a
        second rate bucket — and resumes within the same run once the
        server-requested window has elapsed; it defers the remainder to the next
        scheduled run only when the window outlives the run or after
        ``RESWEEP_MAX_FLOODS_PER_RUN`` floods. A chat-keyed cycle cursor persists
        which chats completed (plus mid-chat progress), so deferred chats are
        picked up next run instead of being starved by the recency sort order.
        PII: aggregate counts only, never ids/emoji.
        """
        from telethon.tl.functions.messages import GetMessagesReactionsRequest
        from telethon.tl.types import UpdateMessageReactions

        self._ensure_resweep_state()
        if chat_id in self._resweep_cycle_done:
            return  # covered earlier this cycle (checked first: done ≠ deferred)
        if self._resweep_hard_deferred:
            self._resweep_dialogs_deferred += 1
            self._resweep_deferred_any = True
            return
        if self._resweep_flood_until is not None:
            if time.monotonic() < self._resweep_flood_until:
                # Cooling down after a FloodWait: this chat's re-sweep skips to
                # the next-run cursor; the rest of the backup is unaffected.
                self._resweep_dialogs_deferred += 1
                self._resweep_deferred_any = True
                return
            # The server-requested window has fully elapsed: resume within this
            # run (#224 follow-up — deferring whole runs over-corrected on
            # small-bucket accounts, costing more coverage than the floods did).
            self._resweep_flood_until = None
            logger.info("Reaction resweep cooldown elapsed; resuming within this run")

        cutoff = utcnow_naive() - timedelta(days=self.config.reaction_resweep_days)
        ids = await self.db.get_message_ids_since(chat_id, cutoff, self.config.reaction_resweep_max_per_chat)
        # Resume mid-chat after an earlier deferred run: the first ``skip_n``
        # (newest) ids were already covered this cycle. The window shifts between
        # runs so the offset is approximate — reconcile is idempotent, so a few
        # re-covered or missed ids are harmless; what matters is guaranteed
        # forward progress on chats larger than the flood bucket, which would
        # otherwise flood at the same chunk every run and never finish.
        skip_n = self._resweep_partial.get(chat_id, 0)
        if skip_n:
            ids = ids[skip_n:]
        if not ids:
            if skip_n:
                self._resweep_partial.pop(chat_id, None)
                self._resweep_cycle_done.add(chat_id)
            return

        checked = 0
        reconciled = 0
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            checked += len(chunk)

            # PRIMARY: one raw request returns just the reaction aggregates for the
            # requested ids. Updates inside an RPC result are tagged _self_outgoing by
            # Telethon and never reach the listener's dispatch loop, so parsing the
            # result directly cannot double-process with the live handler. ``updates``
            # stays None only if the request itself failed (→ fallback below).
            #
            # No retry wrapper here: a FloodWait on this bucket means the whole
            # account+method budget is exhausted, so retrying (or falling back to
            # get_messages, a DIFFERENT bucket under the same pressure pattern)
            # compounds the penalty — pause and resume within the run once the
            # server-requested window elapses instead (#224).
            updates = None
            await self._resweep_pace()
            try:
                result = await self.client(GetMessagesReactionsRequest(peer=entity, id=chunk))
                updates = getattr(result, "updates", []) or []
            except FloodWaitError as e:
                self._register_resweep_flood(e.seconds, chat_id, skip_n + i, "getMessagesReactions")
                return
            except Exception as e:
                # Raw request unsupported for this peer, or ids rejected (e.g. deleted
                # on Telegram → MSG_ID_INVALID). Fall back to a full-message fetch.
                logger.debug("Reaction resweep raw request failed, falling back: %s", type(e).__name__)

            if updates is not None:
                for u in updates:
                    # Only ids ECHOED BACK are reconciled; ids absent from the response
                    # are left untouched (absence never means "reacted to zero").
                    if not isinstance(u, UpdateMessageReactions):
                        continue
                    reactions_obj = getattr(u, "reactions", None)
                    if getattr(reactions_obj, "min", False):
                        continue
                    observed = extract_reactions(reactions_obj)
                    if observed is None:
                        continue
                    if (
                        await self.db.reconcile_reactions(u.msg_id, chat_id, observed, mark_removed=True)
                        == "reconciled"
                    ):
                        reconciled += 1
                continue

            # FALLBACK: full-message fetch, only for genuine non-flood raw errors
            # (unsupported peer, rejected ids). get_messages returns None placeholders
            # for missing ids (skip); a returned message with reactions=None is a
            # definitive empty snapshot (extract_reactions(None) == []) → reconcile to
            # zero. It draws on its own rate bucket, so it is paced identically and,
            # exactly like the raw path, a FloodWait pauses the re-sweep — no
            # sleeping into the live flood window, no retry. Other errors skip the
            # chunk (the next cycle retries it).
            await self._resweep_pace()
            try:
                msgs = await self.client.get_messages(entity, ids=chunk)
            except FloodWaitError as e:
                self._register_resweep_flood(e.seconds, chat_id, skip_n + i, "get_messages")
                return
            except Exception as e:
                logger.debug("Reaction resweep fallback fetch failed: %s", type(e).__name__)
                continue
            for msg in msgs or []:
                if msg is None:
                    continue
                reactions_obj = getattr(msg, "reactions", None)
                if getattr(reactions_obj, "min", False):
                    continue
                observed = extract_reactions(reactions_obj)
                if observed is None:
                    continue
                if await self.db.reconcile_reactions(msg.id, chat_id, observed, mark_removed=True) == "reconciled":
                    reconciled += 1

        # Every chunk completed: mark this chat covered for the current cycle so a
        # later deferred run resumes with the chats that were skipped, not this one.
        self._resweep_partial.pop(chat_id, None)
        self._resweep_cycle_done.add(chat_id)
        logger.info("  → Reaction resweep: checked %d ids, reconciled %d", checked, reconciled)

    async def _sync_pinned_messages(self, chat_id: int, entity) -> None:
        """
        Sync pinned messages for a chat.

        Fetches all currently pinned messages from Telegram using the
        InputMessagesFilterPinned filter and updates the is_pinned field
        in the database.

        This ensures pinned status is always up-to-date after each backup,
        catching both newly pinned and unpinned messages.

        Args:
            chat_id: Chat ID (marked format)
            entity: Telegram entity
        """
        try:
            from telethon.tl.types import InputMessagesFilterPinned

            # Fetch all pinned messages from Telegram (up to 100)
            pinned_messages = await call_with_flood_retry(
                self.client.get_messages, entity, filter=InputMessagesFilterPinned(), limit=100
            )

            if pinned_messages:
                pinned_ids = [msg.id for msg in pinned_messages]
                await self.db.sync_pinned_messages(chat_id, pinned_ids)
                logger.debug(f"  → Synced {len(pinned_ids)} pinned messages")
            else:
                # No pinned messages - clear any existing
                await self.db.sync_pinned_messages(chat_id, [])

        except Exception as e:
            # Don't fail the backup if pinned sync fails
            logger.debug(f"  → Could not sync pinned messages: {e}")

    def _extract_forward_from_id(self, message: Message) -> int | None:
        """
        Extract forward sender ID safely handling different Peer types.

        Args:
            message: Message object

        Returns:
            ID of the forward sender or None
        """
        if not message.fwd_from or not message.fwd_from.from_id:
            return None

        peer = message.fwd_from.from_id

        # Handle different Peer types
        if hasattr(peer, "user_id"):
            return peer.user_id
        if hasattr(peer, "channel_id"):
            return peer.channel_id
        if hasattr(peer, "chat_id"):
            return peer.chat_id

        return None

    def _text_with_entities_to_string(self, text_obj) -> str:
        """
        Convert TextWithEntities or string to a plain string.

        Args:
            text_obj: TextWithEntities object or string

        Returns:
            Plain string representation
        """
        if text_obj is None:
            return ""
        if isinstance(text_obj, str):
            return text_obj
        if isinstance(text_obj, TextWithEntities):
            # Extract the text from TextWithEntities
            return text_obj.text if hasattr(text_obj, "text") else str(text_obj)
        # Fallback for any other type
        return str(text_obj)

    async def _resolve_display_name(self, user_id: int) -> str | None:
        """Display name for a user id: local users table first, then the API.

        Used to name the AFFECTED user in add/kick service texts (#222 review).
        Returns None when the user is unknown everywhere; the caller then renders
        "Someone ..." rather than attributing the action to the wrong person.
        """
        try:
            row = await self.db.get_user_by_id(user_id)
        except Exception:
            row = None
        if row:
            name = (row.get("first_name") or "").strip()
            if row.get("last_name"):
                name = f"{name} {row['last_name']}".strip()
            if name:
                return name
        try:
            entity = await call_with_flood_retry(self.client.get_entity, user_id)
        except Exception:
            return None
        name = getattr(entity, "first_name", "") or getattr(entity, "title", "")
        if name and getattr(entity, "last_name", None):
            name += f" {entity.last_name}"
        return name or None

    async def _process_message(self, message: Message, chat_id: int) -> dict:
        """
        Process and save a single message.

        Args:
            message: Message object from Telegram
            chat_id: Chat identifier
        """
        # Save sender information if available
        if message.sender:
            sender_data = self._extract_user_data(message.sender)
            if sender_data:
                await self.db.upsert_user(sender_data)

        # Extract message data
        # v6.0.0: media_type, media_id, media_path removed - media stored in separate table
        # v6.2.0: reply_to_top_id added for forum topic threading
        reply_to_top_id = extract_topic_id(message)

        message_data = {
            "id": message.id,
            "chat_id": chat_id,
            "sender_id": message.sender_id,
            "date": message.date,
            "text": message.text or "",
            "reply_to_msg_id": message.reply_to_msg_id,
            "reply_to_top_id": reply_to_top_id,
            "reply_to_text": None,
            "forward_from_id": self._extract_forward_from_id(message),
            "edit_date": message.edit_date,
            "raw_data": {},
            "is_outgoing": 1 if message.out else 0,
            "is_pinned": 1 if getattr(message, "pinned", False) else 0,
        }

        # Preserve service-action metadata (e.g. forum topic creations and
        # renames) so historical backfills carry the same raw_data *shape* AND
        # *vocabulary* as the live listener: since the #222 fix both derive
        # action_type from the MessageAction class name via service_action_type
        # (chat_edit_title, chat_joined_by_link, ...). Without this, service
        # events are stored without their payload and are irrecoverable once
        # archived.
        action = getattr(message, "action", None)
        if action is not None:
            message_data["raw_data"]["service_type"] = "service"
            message_data["raw_data"]["action_type"] = service_action_type(action)
            action_title = getattr(action, "title", None)
            if action_title is not None:
                message_data["raw_data"]["new_title"] = self._text_with_entities_to_string(action_title)

            # Group ↔ supergroup migration pointers (#228). MessageActionChatMigrateTo
            # carries only ``.channel_id`` (no ``.title``), so the new supergroup id
            # would otherwise be silently dropped; persist it in marked form so a
            # later sweep can reconcile scope even if the migration happened while
            # the archiver was offline. The reverse marker records the old group id.
            if isinstance(action, MessageActionChatMigrateTo):
                message_data["raw_data"]["migrate_to_id"] = get_peer_id(PeerChannel(action.channel_id))
            elif isinstance(action, MessageActionChannelMigrateFrom):
                message_data["raw_data"]["migrate_from_id"] = get_peer_id(PeerChat(action.chat_id))

            # Service messages carry no user-authored text, so synthesize the same
            # human-readable line the live listener stores. Only fill an empty text
            # (a service message with real text is left untouched).
            #
            # The sentence SUBJECT is the affected user for add/kick actions — the
            # person added or removed (mirroring the listener, which resolves
            # event.user_id) — never the admin who performed it. For every other
            # action the sender IS the subject. When the affected user cannot be
            # resolved the text falls back to "Someone ...", never to the wrong name.
            if not message.text:
                action_cls = type(action).__name__
                subject_id = None
                joined_self = False
                if action_cls == "MessageActionChatAddUser":
                    added_users = list(getattr(action, "users", None) or [])
                    joined_self = added_users == [message.sender_id]
                    if added_users and not joined_self:
                        subject_id = added_users[0]
                elif action_cls == "MessageActionChatDeleteUser":
                    affected_id = getattr(action, "user_id", None)
                    if affected_id is not None and affected_id != message.sender_id:
                        subject_id = affected_id

                if subject_id is not None:
                    actor_name = await self._resolve_display_name(subject_id)
                else:
                    sender = message.sender
                    actor_name = None
                    if sender is not None:
                        actor_name = getattr(sender, "first_name", "") or getattr(sender, "title", "")
                        if actor_name and getattr(sender, "last_name", None):
                            actor_name += f" {sender.last_name}"
                affected_left = getattr(action, "user_id", None) == message.sender_id
                message_data["text"] = (
                    service_message_text(
                        action,
                        actor_name=actor_name,
                        affected_left=affected_left,
                        affected_joined_self=joined_self,
                    )
                    or ""
                )

        # Capture grouped_id for album detection (multiple photos/videos sent together)
        if message.grouped_id:
            message_data["raw_data"]["grouped_id"] = str(message.grouped_id)

        # Capture forwarded message info (name of original sender)
        if message.fwd_from:
            fwd = message.fwd_from
            # fwd_from.from_name is set when forwarding from hidden users or deleted accounts
            if fwd.from_name:
                message_data["raw_data"]["forward_from_name"] = fwd.from_name
            elif fwd.from_id:
                # Try to resolve the name from the entity
                try:
                    fwd_entity = await call_with_flood_retry(self.client.get_entity, fwd.from_id)
                    if hasattr(fwd_entity, "title"):
                        message_data["raw_data"]["forward_from_name"] = fwd_entity.title
                    elif hasattr(fwd_entity, "first_name"):
                        name = fwd_entity.first_name or ""
                        if fwd_entity.last_name:
                            name += " " + fwd_entity.last_name
                        message_data["raw_data"]["forward_from_name"] = name.strip()
                except Exception:
                    # Can't resolve - will fall back to ID in viewer
                    pass

        # Capture channel post author (signature) if available
        if hasattr(message, "post_author") and message.post_author:
            message_data["raw_data"]["post_author"] = message.post_author

        # Get reply text if this is a reply
        if message.reply_to_msg_id and message.reply_to:
            reply_msg = message.reply_to
            if hasattr(reply_msg, "message"):
                # Truncate to first 100 chars like Telegram does
                reply_text = (reply_msg.message or "")[:100]
                message_data["reply_to_text"] = reply_text

        # Handle media
        if message.media:
            # Handle Polls specially (store structure in raw_data, do not download)
            # v6.0.0: Poll type is detected by presence of raw_data['poll']
            if isinstance(message.media, MessageMediaPoll):
                poll = message.media.poll
                results = message.media.results

                # Parse results if available
                results_data = None
                if results:
                    try:
                        results_list = []
                        if results.results:
                            for r in results.results:
                                results_list.append(
                                    {
                                        "option": base64.b64encode(r.option).decode("ascii"),
                                        "voters": r.voters,
                                        "correct": r.correct,
                                    }
                                )
                        results_data = {"total_voters": results.total_voters, "results": results_list}
                    except Exception as e:
                        logger.warning(f"Error parsing poll results: {e}")

                # Store poll structure
                # Convert TextWithEntities to strings for JSON serialization
                question_text = self._text_with_entities_to_string(getattr(poll, "question", ""))
                message_data["raw_data"]["poll"] = {
                    "id": getattr(poll, "id", None),
                    "question": question_text,
                    "answers": [
                        {
                            "text": self._text_with_entities_to_string(getattr(a, "text", "")),
                            "option": base64.b64encode(a.option).decode("ascii"),
                        }
                        for a in poll.answers
                    ],
                    "closed": poll.closed,
                    "public_voters": poll.public_voters,
                    "multiple_choice": poll.multiple_choice,
                    "quiz": poll.quiz,
                    "results": results_data,
                }

            elif self.config.should_download_media_for_chat(chat_id):
                # v6.0.0: Download media and store data for later insertion
                # (media is inserted AFTER message to satisfy FK constraint)
                media_result = await self._process_media(message, chat_id)
                if media_result:
                    message_data["_media_data"] = media_result

        # Extract reactions (per-emoji aggregate snapshot). Reconciled after the
        # message is inserted; see DatabaseAdapter.reconcile_reactions (#219).
        message_data["reactions"] = extract_reactions(getattr(message, "reactions", None))

        # Return message data for batch processing
        return message_data

    async def _ensure_profile_photo(self, entity, marked_id: int = None) -> None:
        """
        Download the current profile photo for users and chats.

        Downloads the profile photo on every backup run to ensure avatars
        stay up-to-date. Files are named `<chat_id>_<photo_id>.jpg` so the
        viewer can pick the freshest version.

        Args:
            entity: Telegram entity (User, Chat, Channel)
            marked_id: The marked chat ID (negative for groups/channels) for consistent file naming
        """
        file_id = marked_id if marked_id is not None else self._get_marked_id(entity)
        avatar_path, _legacy_path = get_avatar_paths(self.config.media_path, entity, file_id)

        # Nothing to download (no avatar set)
        if avatar_path is None:
            logger.debug("No avatar available")
            return

        try:
            # Avoid redundant downloads when we already have the current photo.
            # lexists treats an existing symlink (even one pointing into an
            # archive store like git-annex whose target may be unreachable
            # from this process) as "we have it". Without this guard, a
            # broken-but-intentional symlink at avatar_path made
            # download_profile_photo follow the symlink into a missing
            # parent directory and surface as ENOENT (issue #143).
            if os.path.lexists(avatar_path):
                # Symlink-or-file already in place: skip unless it is a
                # zero-byte regular file from a prior interrupted download.
                if os.path.islink(avatar_path) or os.path.getsize(avatar_path) > 0:
                    return

            result = await self.client.download_profile_photo(
                entity,
                file=avatar_path,
                download_big=False,  # Small size is usually sufficient
            )
            if result:
                logger.info("📷 Avatar downloaded")
        except Exception as e:
            logger.warning(f"Failed to download avatar: {e}")

    async def _cleanup_existing_media(self, chat_id: int) -> None:
        """
        Delete existing media files and database records for a chat.
        Used when a chat is added to SKIP_MEDIA_CHAT_IDS to reclaim storage.

        Handles deduplicated media safely: symlinks are removed without
        affecting the shared original in _shared/. Only real files
        (non-symlinks) count toward freed storage.

        Args:
            chat_id: Chat identifier
        """
        try:
            media_records = await self.db.get_media_for_chat(chat_id)
            if not media_records:
                logger.debug("No existing media found for chat")
                return

            deleted_files = 0
            deleted_symlinks = 0
            deleted_records = 0
            freed_bytes = 0

            for record in media_records:
                file_path = record.get("file_path")
                if file_path and os.path.exists(file_path):
                    try:
                        if os.path.islink(file_path):
                            os.unlink(file_path)
                            deleted_symlinks += 1
                        else:
                            freed_bytes += os.path.getsize(file_path)
                            os.remove(file_path)
                            deleted_files += 1
                    except Exception as e:
                        logger.warning(f"Failed to delete media file: {e}")

            # Delete all media records from database for this chat
            deleted_records = await self.db.delete_media_for_chat(chat_id)

            # Clean up empty chat media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            if os.path.isdir(chat_media_dir):
                try:
                    remaining = os.listdir(chat_media_dir)
                    if not remaining:
                        os.rmdir(chat_media_dir)
                        logger.debug("Removed empty media directory for chat")
                except Exception as e:
                    logger.debug(f"Could not remove media directory for chat: {e}")

            if deleted_files > 0 or deleted_symlinks > 0 or deleted_records > 0:
                freed_mb = freed_bytes / (1024 * 1024)
                parts = []
                if deleted_files > 0:
                    parts.append(f"{deleted_files} files ({freed_mb:.1f} MB freed)")
                if deleted_symlinks > 0:
                    parts.append(f"{deleted_symlinks} symlinks removed")
                logger.info(
                    f"Cleaned up existing media for chat: {', '.join(parts)}, {deleted_records} DB records deleted"
                )

        except Exception as e:
            logger.error(f"Error cleaning up existing media for chat: {e}", exc_info=True)

    async def _refresh_message_for_media(self, chat_id: int, message: Message) -> Message | None:
        """Best-effort re-fetch so Telegram issues an updated media reference/location.

        Bounded by ``MEDIA_REFRESH_TIMEOUT_SECONDS`` so it can never hang, and
        swallows transient errors (returning ``None``) so a failed refresh never
        blows up the surrounding retry loop. Handles a deleted/unavailable
        message (``[]`` or ``[None]``) by returning ``None``.
        """

        async def _get_messages_once():
            # Time only the single Telegram call, so call_with_flood_retry still
            # owns (and is never cancelled mid-) any FloodWait sleep.
            return await asyncio.wait_for(
                self.client.get_messages(chat_id, ids=[message.id]),
                timeout=MEDIA_REFRESH_TIMEOUT_SECONDS,
            )

        try:
            fresh_messages = await call_with_flood_retry(
                _get_messages_once,
                non_retryable=lambda exc: isinstance(exc, TimeoutError),
            )
        except (TimeoutError, RPCError, ConnectionError, OSError) as e:
            logger.debug("Could not refresh media reference (%s)", type(e).__name__)
            return None
        if fresh_messages and fresh_messages[0]:
            return fresh_messages[0]
        return None

    async def _fetch_media_bytes_bounded(self, message: Message, tmp_path: str, file_size: int, timeout_val):
        """``_fetch_media_bytes`` bounded by a per-operation timeout.

        Timing only the single download operation (rather than the whole
        ``call_with_flood_retry`` wrapper) ensures a Telegram FloodWait sleep is
        never cancelled by the download timeout. A timed-out operation raises
        ``TimeoutError``, which ``_is_non_retryable_media_op`` lets propagate to
        the outer retry loop.
        """
        coro = self._fetch_media_bytes(message, tmp_path, file_size)
        if timeout_val is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_val)

    async def _download_media_to_path(self, message: Message, tmp_path: str, file_size: int, chat_id: int):
        """Download a message's media to ``tmp_path`` with bounded refresh + retry.

        Transient Telegram errors that a fresh message can fix — an expired file
        reference, or an unavailable/invalid media *location* — trigger a
        re-fetch of the message (for a new reference/location). A location error
        is a transient server-side condition, so we also pause with exponential
        backoff before retrying; an expired reference is fixed by the refresh
        itself and is retried immediately. After ``MEDIA_REFRESH_MAX_ATTEMPTS``
        the last real error is raised so the caller records the item as
        not-downloaded; the next scheduled backup run re-attempts it.

        Returns the downloaded path on success.
        """
        timeout = getattr(self.config, "download_timeout_seconds", 3600)
        timeout_val = timeout if isinstance(timeout, int) and timeout > 0 else None
        last = MEDIA_REFRESH_MAX_ATTEMPTS - 1
        try:
            for attempt in range(MEDIA_REFRESH_MAX_ATTEMPTS):
                # Start each attempt clean so a prior partial never corrupts it.
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                try:
                    return await call_with_flood_retry(
                        self._fetch_media_bytes_bounded,
                        message,
                        tmp_path,
                        file_size,
                        timeout_val,
                        non_retryable=_is_non_retryable_media_op,
                    )
                except (FileReferenceExpiredError, RPCError) as e:
                    is_expired_ref = isinstance(e, FileReferenceExpiredError)
                    if not is_expired_ref and not is_media_location_error(e):
                        raise  # not refreshable — let the outer handler record it
                    if attempt >= last:
                        logger.warning(
                            "Media still unavailable after %d attempt(s) (%s); leaving it for a future backup run",
                            attempt + 1,
                            type(e).__name__,
                        )
                        raise
                    refreshed = await self._refresh_message_for_media(chat_id, message)
                    if refreshed is not None:
                        message = refreshed
                        logger.info(
                            "Refreshed media reference after a transient error (attempt %d/%d); retrying",
                            attempt + 1,
                            MEDIA_REFRESH_MAX_ATTEMPTS,
                        )
                    else:
                        logger.info(
                            "Could not refresh media reference (attempt %d/%d); retrying anyway",
                            attempt + 1,
                            MEDIA_REFRESH_MAX_ATTEMPTS,
                        )
                    if not is_expired_ref:
                        await asyncio.sleep(_media_retry_backoff_seconds(attempt))
                except TimeoutError:
                    if attempt >= last:
                        logger.error(
                            "Media download timed out after %ss on attempt %d/%d; giving up for this run",
                            timeout,
                            attempt + 1,
                            MEDIA_REFRESH_MAX_ATTEMPTS,
                        )
                        raise
                    logger.warning(
                        "Media download timed out after %ss (attempt %d/%d); retrying",
                        timeout,
                        attempt + 1,
                        MEDIA_REFRESH_MAX_ATTEMPTS,
                    )
            # Defensive: the loop returns on success or raises on the final attempt.
            raise FileReferenceExpiredError(request=None)
        except BaseException:
            # Never leave a partial .part behind on failure or cancellation.
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    async def _process_media(self, message: Message, chat_id: int) -> dict | None:
        """
        Process and download media from a message.

        Args:
            message: Message object with media
            chat_id: Chat identifier

        Returns:
            Dictionary with media information, or None if skipped
        """
        media = message.media
        media_type = self._get_media_type(media)

        if not media_type:
            return None

        # Generate unique media ID
        media_id = f"{chat_id}_{message.id}_{media_type}"

        # Contacts, locations, and polls are Telegram message payloads rather
        # than downloadable files. Store them as metadata-only records when the
        # caller asks for media processing.
        if media_type in {"contact", "geo", "poll"}:
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_size": 0,
                "downloaded": False,
            }

        # Get Telegram's file unique ID for deduplication
        telegram_file_id = None
        if hasattr(media, "photo"):
            telegram_file_id = str(getattr(media.photo, "id", None))
        elif hasattr(media, "document"):
            telegram_file_id = str(getattr(media.document, "id", None))

        # Guard against inaccessible media producing "None" string IDs
        if telegram_file_id == "None":
            telegram_file_id = None

        # Check file size (estimated)
        file_size = self._get_media_size(media)
        max_size = self.config.get_max_media_size_bytes()

        if file_size > max_size:
            logger.debug(f"Skipping large media file: {file_size / 1024 / 1024:.2f} MB")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_size": file_size,
                "downloaded": False,
            }

        # Download media (with optional global deduplication)
        try:
            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)

            # Generate filename using file_id for automatic deduplication
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
            file_path = os.path.join(chat_media_dir, file_name)

            # Check if deduplication is enabled
            content_hash = None
            if getattr(self.config, "deduplicate_media", True):
                # Global deduplication: use _shared directory for actual files
                shared_dir = os.path.join(self.config.media_path, "_shared")
                os.makedirs(shared_dir, exist_ok=True)

                async def _download_fn(tmp_path):
                    return await self._download_media_to_path(message, tmp_path, file_size, chat_id)

                shared_file_path, content_hash = await download_and_shard_media(
                    db=self.db,
                    download_coro=_download_fn,
                    shared_dir=shared_dir,
                    chat_media_dir=chat_media_dir,
                    file_name=file_name,
                    file_path=file_path,
                    logger=logger,
                )
                if not shared_file_path and not os.path.lexists(file_path):
                    return None

                # Backup-specific post-processing: update file_size from disk
                if not shared_file_path:
                    shared_file_path = resolve_shared_file_path(shared_dir, file_name, content_hash)
                actual_path = shared_file_path if shared_file_path and os.path.exists(shared_file_path) else file_path
                if os.path.exists(actual_path):
                    file_size = os.path.getsize(actual_path)
                    if not content_hash:
                        content_hash = compute_file_hash(actual_path)
            else:
                # No deduplication - download directly to chat directory.
                # lexists short-circuits the download when a symlink is
                # already recorded, even if its target is unreachable.
                if not os.path.lexists(file_path):
                    task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
                    tmp_file_path = f"{file_path}.{os.getpid()}.{task_id}.part"
                    actual_path = await self._download_media_to_path(message, tmp_file_path, file_size, chat_id)
                    file_path = finalize_atomic_download(
                        actual_path if isinstance(actual_path, str) else None,
                        tmp_file_path,
                        file_path,
                    )
                    if not file_path or not os.path.exists(file_path):
                        logger.warning("Media download did not produce a file")
                        return None
                    logger.debug(f"Downloaded media: {file_name}")

                # Update file_size and compute hash from disk
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    content_hash = compute_file_hash(file_path)

            # Extract media metadata
            media_data = {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "file_name": file_name,
                "file_path": file_path,
                "file_size": file_size,
                "mime_type": getattr(media, "mime_type", None),
                "content_hash": content_hash,
                "downloaded": True,
                "download_date": utcnow_naive(),
            }

            # Add type-specific metadata
            if hasattr(media, "photo"):
                photo = media.photo
                media_data["width"] = getattr(photo, "w", None)
                media_data["height"] = getattr(photo, "h", None)
            elif hasattr(media, "document"):
                doc = media.document
                for attr in doc.attributes:
                    if hasattr(attr, "w") and hasattr(attr, "h"):
                        media_data["width"] = attr.w
                        media_data["height"] = attr.h
                    if hasattr(attr, "duration"):
                        media_data["duration"] = attr.duration

            # Pre-generate thumbnail for instant gallery loading
            try:
                _pre_generate_thumbnail(file_path, self.config.media_path)
            except Exception:
                pass  # Non-critical, viewer generates on-demand as fallback

            # Return media data - caller is responsible for inserting to database
            # (to ensure message exists before media FK constraint)
            return media_data

        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return {
                "id": media_id,
                "type": media_type,
                "message_id": message.id,
                "chat_id": chat_id,
                "downloaded": False,
            }

    def _should_parallelize(self, message, file_size: int) -> bool:
        """Decide whether this file should use the parallel chunked path.

        Gated by config (default OFF), a size threshold, and a one-time client
        capability probe. Returns False for anything that should stay on the
        proven single-stream ``download_media`` path.
        """
        # Strict ``is True`` (not truthiness): a real Config sets a bool, while a
        # MagicMock config returns a truthy mock — this keeps the feature off in
        # tests/callers that never opted in, and off by default in production.
        if getattr(self.config, "parallel_download_enabled", False) is not True:
            return False
        if getattr(self, "_parallel_download_disabled", False):
            return False
        if file_size < self.config.get_parallel_download_min_size_bytes():
            return False
        if not supports_parallel_download(self.client):
            # Probe once; if the installed Telethon lacks the internals we need,
            # stop trying for the whole run instead of re-probing every file.
            logger.warning("Parallel download unavailable (Telethon internals missing); using single-stream")
            self._parallel_download_disabled = True
            return False
        return True

    async def _fetch_media_bytes(self, message, tmp_path, file_size: int):
        """Fetch a message's media to ``tmp_path`` (the bytes-fetch primitive).

        Swaps only the transport: callers keep ``call_with_flood_retry``, the
        timeout, the ``FileReferenceExpired`` refresh loop, and dedup/sharding.
        Uses the parallel transferrer for large files when enabled, otherwise
        the single-stream ``client.download_media``. A parallel attempt that
        reports itself unavailable transparently falls back to single-stream for
        that file; FloodWait and other real errors propagate unchanged so the
        caller's single retry budget governs them.
        """
        if self._should_parallelize(message, file_size):
            if self._parallel_downloader is None:
                self._parallel_downloader = ParallelDownloader(
                    self.client,
                    connections=self.config.parallel_download_connections,
                    part_size=self.config.get_parallel_download_part_size_bytes(),
                    max_file_size=self.config.get_max_media_size_bytes(),
                )
            try:
                return await self._parallel_downloader.download_media(message, tmp_path)
            except ParallelDownloadUnavailable as exc:
                logger.info("Parallel download not applicable (%s); falling back to single-stream", exc)
        return await self.client.download_media(message, tmp_path)

    def _get_media_size(self, media) -> int:
        """Get estimated size of media object in bytes."""
        # Document (Video, Audio, File)
        if hasattr(media, "document") and media.document:
            return getattr(media.document, "size", 0)

        # Photo (find largest size)
        if hasattr(media, "photo") and media.photo:
            sizes = getattr(media.photo, "sizes", [])
            if sizes:
                # Return size of the last one (usually the largest)
                # Some Size types have 'size' field, others don't (like PhotoCachedSize)
                largest = sizes[-1]
                return getattr(largest, "size", 0)

        # Fallback to direct attribute
        return getattr(media, "size", 0)

    def _get_media_type(self, media) -> str | None:
        """Get media type as string."""
        if isinstance(media, MessageMediaPhoto):
            return "photo"
        elif isinstance(media, MessageMediaDocument):
            # Check document attributes to determine specific type
            if hasattr(media, "document") and media.document:
                is_animated = False
                for attr in media.document.attributes:
                    attr_type = type(attr).__name__
                    if "Animated" in attr_type:
                        is_animated = True
                    if "Video" in attr_type:
                        # If animated, it's a GIF
                        return "animation" if is_animated else "video"
                    elif "Audio" in attr_type:
                        # Voice notes have .voice=True on DocumentAttributeAudio
                        if hasattr(attr, "voice") and attr.voice:
                            return "voice"
                        return "audio"
                    elif "Sticker" in attr_type:
                        return "sticker"
                # If animated but no video attribute, still an animation
                if is_animated:
                    return "animation"
                return "document"
            return None  # document reference unavailable (e.g., forwarded from private channel)
        elif isinstance(media, MessageMediaContact):
            return "contact"
        elif isinstance(media, MessageMediaGeo):
            return "geo"
        elif isinstance(media, MessageMediaPoll):
            return "poll"
        return None

    def _get_media_filename(self, message: Message, media_type: str, telegram_file_id: str = None) -> str:
        """
        Generate a unique filename using Telegram's file_id.
        Properly handles files sent "as documents" by checking mime_type and original filename.
        """
        # First, try to get original filename from document attributes
        original_name = None
        mime_type = None

        if hasattr(message.media, "document") and message.media.document:
            doc = message.media.document
            mime_type = getattr(doc, "mime_type", None)

            for attr in doc.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    original_name = attr.file_name
                    break

        # If we have original filename, use it (with file_id prefix for uniqueness).
        # Length-budget the decorative name so it stays writable on constrained
        # filesystems (Synology/eCryptfs ~143 bytes); the file_id prefix + extension
        # are always preserved. (#212)
        if original_name and telegram_file_id:
            return build_media_filename(telegram_file_id, original_name, self.config.max_filename_bytes)

        # No usable original name — shared fallback (message_utils) keeps this
        # identical to the listener's ingest path for the same inputs.
        return fallback_media_filename(telegram_file_id, media_type, mime_type, message.id)

    def _get_media_extension(self, media_type: str) -> str:
        """Get file extension for media type (fallback only)."""
        extensions = {
            "photo": "jpg",
            "video": "mp4",
            "audio": "mp3",
            "voice": "ogg",
            "document": "bin",  # Only used if mime_type detection fails
        }
        return extensions.get(media_type, "bin")

    def _extract_chat_data(self, entity, is_archived: bool = False) -> dict:
        """Extract chat data from entity.

        Args:
            entity: Telegram entity (User, Chat, Channel)
            is_archived: Whether this chat is from the archived folder
        """
        # Use marked ID (with -100 prefix for channels/supergroups) for consistency
        chat_data = {"id": self._get_marked_id(entity)}

        if isinstance(entity, User):
            chat_data["type"] = "private"
            chat_data["first_name"] = entity.first_name
            chat_data["last_name"] = entity.last_name
            chat_data["username"] = entity.username
            chat_data["phone"] = entity.phone
        elif isinstance(entity, Chat):
            chat_data["type"] = "group"
            chat_data["title"] = entity.title
            chat_data["participants_count"] = entity.participants_count
        elif isinstance(entity, Channel):
            chat_data["type"] = "channel" if not entity.megagroup else "group"
            chat_data["title"] = entity.title
            chat_data["username"] = entity.username
            # v6.2.0: Detect forum-enabled chats
            if getattr(entity, "forum", False):
                chat_data["is_forum"] = 1

        # v6.2.0: Track archived status (always set explicitly)
        chat_data["is_archived"] = 1 if is_archived else 0

        return chat_data

    def _extract_user_data(self, user) -> dict | None:
        """Extract user data from user entity."""
        if not isinstance(user, User):
            return None

        return {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "is_bot": user.bot,
        }

    def _get_chat_name(self, entity) -> str:
        """Get a readable name for a chat."""
        if isinstance(entity, User):
            name = entity.first_name or ""
            if entity.last_name:
                name += f" {entity.last_name}"
            if entity.username:
                name += f" (@{entity.username})"
            return name or f"User {entity.id}"
        elif isinstance(entity, (Chat, Channel)):
            return entity.title or f"Chat {entity.id}"
        return f"Unknown {entity.id}"

    async def _backup_forum_topics(self, chat_id: int, entity) -> int:
        """
        Fetch and store forum topics for a forum-enabled chat.

        Uses message metadata to infer topics when GetForumTopicsRequest
        is not available in the current Telethon version.

        Args:
            chat_id: Chat ID (marked format)
            entity: Telegram entity

        Returns:
            Number of topics found
        """
        try:
            # Try using GetForumTopicsRequest via raw API
            # Note: In Telethon 1.42+, this is in messages, not channels
            from telethon.tl.functions.messages import GetForumTopicsRequest

            # Defined before the try so a partial result survives a mid-pagination failure.
            topics_count = 0

            try:
                input_channel = await self.client.get_input_entity(entity)
                # offset_date must be a proper date object, not int 0
                from datetime import datetime as dt

                # Paginate through all topics. Each page is wrapped in
                # call_with_flood_retry so a FloodWait on a large forum doesn't
                # abort the fetch (issue #200). Telethon invokes a request via
                # ``self.client(request)``, so pass self.client as the func and
                # the request object as its sole positional argument.
                seen_count = 0  # every topic the server returned (pre-skip), for pagination
                offset_date = dt(1970, 1, 1)
                offset_id = 0
                offset_topic = 0
                total_count = None
                max_pages = 50  # defensive cap to avoid an unbounded loop
                for _ in range(max_pages):
                    result = await call_with_flood_retry(
                        self.client,
                        GetForumTopicsRequest(
                            peer=input_channel,
                            offset_date=offset_date,
                            offset_id=offset_id,
                            offset_topic=offset_topic,
                            limit=100,
                        ),
                    )

                    if total_count is None:
                        raw_count = getattr(result, "count", 0)
                        total_count = raw_count if isinstance(raw_count, int) else 0

                    page_topics = result.topics
                    if not page_topics:
                        break
                    seen_count += len(page_topics)

                    # Build a message-id → date map for this page so we can
                    # advance offset_date from the last topic's top message.
                    msg_dates = {m.id: m.date for m in getattr(result, "messages", []) if getattr(m, "date", None)}

                    # Resolve custom emoji IDs to unicode emojis for this page
                    emoji_map = {}
                    emoji_ids = [t.icon_emoji_id for t in page_topics if getattr(t, "icon_emoji_id", None)]
                    if emoji_ids:
                        try:
                            from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest

                            docs = await call_with_flood_retry(
                                self.client, GetCustomEmojiDocumentsRequest(document_id=emoji_ids)
                            )
                            for doc in docs:
                                for attr in doc.attributes:
                                    if hasattr(attr, "alt") and attr.alt:
                                        emoji_map[doc.id] = attr.alt
                                        break
                            logger.info(f"  → Resolved {len(emoji_map)} topic emojis")
                        except Exception as e:
                            logger.warning(f"  → Could not resolve topic emojis: {e}")

                    for topic in page_topics:
                        # Deleted topics (forumTopicDeleted) only carry an id, no
                        # title — skip them so we don't store empty placeholders.
                        topic_title = getattr(topic, "title", None)
                        if not topic_title:
                            continue
                        emoji_id = getattr(topic, "icon_emoji_id", None)
                        topic_data = {
                            "id": topic.id,
                            "chat_id": chat_id,
                            "title": topic_title,
                            "icon_color": getattr(topic, "icon_color", None),
                            "icon_emoji_id": emoji_id,
                            "icon_emoji": emoji_map.get(emoji_id) if emoji_id else None,
                            "is_closed": 1 if getattr(topic, "closed", False) else 0,
                            "is_pinned": 1 if getattr(topic, "pinned", False) else 0,
                            "is_hidden": 1 if getattr(topic, "hidden", False) else 0,
                            "date": getattr(topic, "date", None),
                        }
                        if self.config.should_skip_topic(chat_id, topic.id):
                            logger.debug("  → Skipping excluded topic")
                            continue
                        await self.db.upsert_forum_topic(topic_data)
                        topics_count += 1

                    # Advance offsets from the LAST topic of this page. offset_topic is
                    # the load-bearing cursor (always advances monotonically); anchor the
                    # message-based offsets on the last topic that actually has a
                    # top_message, since a trailing forumTopicDeleted carries only an id.
                    last_topic = page_topics[-1]
                    offset_topic = last_topic.id
                    anchor = next((t for t in reversed(page_topics) if getattr(t, "top_message", 0)), last_topic)
                    offset_id = getattr(anchor, "top_message", 0) or 0
                    offset_date = msg_dates.get(offset_id) or getattr(anchor, "date", None) or offset_date

                    # Stop once we've seen every topic the server reported.
                    # seen_count is pre-skip so it matches result.count even when
                    # some topics are excluded or deleted.
                    if total_count and seen_count >= total_count:
                        break

                if total_count and seen_count < total_count:
                    logger.warning(
                        f"  → Forum topic pagination hit the {max_pages}-page cap; "
                        f"fetched {seen_count} of {total_count} topics"
                    )
                logger.info(f"  → Backed up {topics_count} forum topics via API")
                return topics_count

            except Exception as e:
                # If earlier pages already succeeded, keep them rather than falling
                # through to per-topic message inference (which issues many more API
                # calls — bad right after a FloodWait). The end-of-run backstop / next
                # run continues from here.
                if topics_count > 0:
                    logger.warning(
                        f"GetForumTopicsRequest failed mid-pagination ({e.__class__.__name__}: {e}); "
                        f"keeping {topics_count} topics fetched so far"
                    )
                    return topics_count
                logger.warning(
                    f"GetForumTopicsRequest failed ({e.__class__.__name__}: {e}), falling back to message inference"
                )
                # Fall through to inference method
        except ImportError:
            logger.warning("GetForumTopicsRequest not available in this Telethon version, using message inference")

        # Fallback: Infer topics from message reply_to_top_id values
        # This finds unique topic IDs and uses the topic's first message as metadata
        try:
            from sqlalchemy import distinct, select

            from .db.models import Message as MessageModel

            async with self.db.db_manager.async_session_factory() as session:
                # Get unique reply_to_top_id values for this chat
                stmt = (
                    select(distinct(MessageModel.reply_to_top_id))
                    .where(MessageModel.chat_id == chat_id)
                    .where(MessageModel.reply_to_top_id.isnot(None))
                )
                result = await session.execute(stmt)
                topic_ids = [row[0] for row in result]

            topics_count = 0
            for topic_id in topic_ids:
                if self.config.should_skip_topic(chat_id, topic_id):
                    logger.debug("  → Skipping excluded topic")
                    continue
                # Try to get the topic's first message for metadata
                try:
                    msgs = await call_with_flood_retry(self.client.get_messages, entity, ids=[topic_id])
                    if msgs and msgs[0]:
                        msg = msgs[0]
                        topic_data = {
                            "id": topic_id,
                            "chat_id": chat_id,
                            "title": msg.text[:100] if msg.text else f"Topic {topic_id}",
                            "date": msg.date,
                        }
                        await self.db.upsert_forum_topic(topic_data)
                        topics_count += 1
                except Exception as e:
                    logger.debug(f"Could not fetch topic metadata: {e}")

            if topics_count > 0:
                logger.info(f"  → Inferred {topics_count} forum topics from messages")
            return topics_count

        except Exception as e:
            logger.warning(f"  → Failed to infer forum topics: {e}")
            return 0

    def _resolve_peer_ids(self, peers, own_id: int | None = None) -> set[int]:
        """Resolve a DialogFilter peer list (InputPeer objects) to marked chat ids.

        ``own_id`` maps ``InputPeerSelf`` (how a pinned Saved Messages chat is
        stored) to the account's own user id, which get_peer_id cannot resolve.
        """
        ids: set[int] = set()
        for peer in peers or []:
            if own_id is not None and isinstance(peer, InputPeerSelf):
                ids.add(own_id)
                continue
            try:
                ids.add(self._get_marked_id(peer))
            except Exception:
                # Some peers might not be resolvable via get_peer_id; fall back to
                # the raw id fields with the standard marked-id conventions.
                if hasattr(peer, "user_id"):
                    ids.add(peer.user_id)
                elif hasattr(peer, "chat_id"):
                    ids.add(-peer.chat_id)
                elif hasattr(peer, "channel_id"):
                    ids.add(-1000000000000 - peer.channel_id)
        return ids

    def _folder_rules_from_filter(self, f, own_id: int | None = None) -> FolderRules:
        """Build resolver rules from a DialogFilter / DialogFilterChatlist.

        Chatlist (shareable) folders carry no flags or exclude_peers; getattr
        defaults keep them as a pure pinned+include allowlist.
        """
        return FolderRules(
            pinned_ids=frozenset(self._resolve_peer_ids(getattr(f, "pinned_peers", []), own_id)),
            include_ids=frozenset(self._resolve_peer_ids(getattr(f, "include_peers", []), own_id)),
            exclude_ids=frozenset(self._resolve_peer_ids(getattr(f, "exclude_peers", []), own_id)),
            contacts=bool(getattr(f, "contacts", False)),
            non_contacts=bool(getattr(f, "non_contacts", False)),
            groups=bool(getattr(f, "groups", False)),
            broadcasts=bool(getattr(f, "broadcasts", False)),
            bots=bool(getattr(f, "bots", False)),
            exclude_muted=bool(getattr(f, "exclude_muted", False)),
            exclude_read=bool(getattr(f, "exclude_read", False)),
            exclude_archived=bool(getattr(f, "exclude_archived", False)),
        )

    async def _get_contact_ids(self) -> set[int]:
        """Fetch the account's contact user ids (for contacts/non_contacts flags).

        Returns an empty set on failure — folders relying on those flags simply
        fall back to their explicit peers rather than aborting the backup.
        """
        try:
            from telethon.tl.functions.contacts import GetContactsRequest

            result = await call_with_flood_retry(self.client, GetContactsRequest(hash=0))
            return {u.id for u in getattr(result, "users", [])}
        except Exception as e:
            logger.warning(f"Could not fetch contacts for folder resolution: {e}")
            return set()

    async def _get_own_id(self) -> int | None:
        """Return the account's own user id (for resolving self/Saved Messages)."""
        try:
            me = await call_with_flood_retry(self.client.get_me)
            return me.id if me is not None else None
        except Exception as e:
            logger.warning(f"Could not resolve own id for folder resolution: {e}")
            return None

    async def _backup_folders(self) -> int:
        """
        Fetch and store user's Telegram chat folders (dialog filters).

        Resolves each folder's FULL effective membership against the chats we've
        archived — explicit pinned/include peers minus exclude peers, plus the
        category flags (contacts/non_contacts/groups/broadcasts/bots), not only
        include_peers — so folders defined by pins or flags aren't left empty.

        Returns:
            Number of folders backed up
        """
        try:
            from telethon.tl.functions.messages import GetDialogFiltersRequest

            result = await self.client(GetDialogFiltersRequest())

            # result might be a list directly or have a .filters attribute
            filters = result.filters if hasattr(result, "filters") else result

            # The archived-chat snapshot and contacts are fetched at most once per
            # run, lazily, and reused across folders — an account with only the
            # default "All" filter pays for neither.
            resolution_chats: list[FolderChat] | None = None
            contact_ids: set[int] | None = None
            own_id = await self._get_own_id()

            folder_count = 0
            active_folder_ids = []

            for idx, f in enumerate(filters):
                # Skip the default "All" filter
                if not hasattr(f, "id") or not hasattr(f, "title"):
                    continue

                folder_id = f.id
                # Handle title - might be string or TextWithEntities
                title = f.title
                if hasattr(title, "text"):
                    title = title.text
                title = str(title)

                active_folder_ids.append(folder_id)

                folder_data = {
                    "id": folder_id,
                    "title": title,
                    "emoticon": getattr(f, "emoticon", None),
                    "sort_order": idx,
                }
                await self.db.upsert_chat_folder(folder_data)

                if resolution_chats is None:
                    resolution_chats = [
                        FolderChat(id=r["id"], type=r["type"], is_bot=r["is_bot"], is_archived=r["is_archived"])
                        for r in await self.db.get_chats_for_folder_resolution()
                    ]

                rules = self._folder_rules_from_filter(f, own_id)
                if (rules.contacts or rules.non_contacts) and contact_ids is None:
                    contact_ids = await self._get_contact_ids()
                    # Saved Messages (self) counts as a contact, matching Telegram.
                    if own_id is not None:
                        contact_ids.add(own_id)

                member_ids = resolve_folder_member_ids(rules, resolution_chats, contact_ids or set())
                # Always sync (even to an empty set) so a folder that lost all its
                # archived chats is emptied rather than keeping stale members.
                await self.db.sync_folder_members(folder_id, list(member_ids))

                folder_count += 1
                logger.debug(f"  → Folder: {len(member_ids)} chats")

            # Remove folders that no longer exist
            await self.db.cleanup_stale_folders(active_folder_ids)

            if folder_count > 0:
                logger.info(f"Backed up {folder_count} chat folders")
            return folder_count

        except Exception as e:
            logger.warning(f"Failed to backup chat folders: {e}")
            return 0


async def run_backup(config: Config, client: TelegramClient | None = None):
    """
    Run a single backup operation.

    Args:
        config: Configuration object
        client: Optional existing TelegramClient to use (for shared connection).
               If provided, the backup will use this client instead of creating
               its own, avoiding session file lock conflicts.
    """
    backup = await TelegramBackup.create(config, client=client)
    try:
        await backup.connect()
        # One-time repair of media files corrupted by the pre-7.11.3 finalize bug (#175).
        from .repair_media_extensions import repair_media_extensions

        await repair_media_extensions(config.media_path, backup.db)
        await backup.backup_all()
    finally:
        await backup.disconnect()
        await backup.db.close()


async def run_fill_gaps(config: Config, client: TelegramClient | None = None, chat_id: int | None = None) -> dict:
    """
    Run gap-fill to recover missing messages in backed-up chats.

    Args:
        config: Configuration object
        client: Optional existing TelegramClient to use (for shared connection).
               If provided, the operation will use this client instead of creating
               its own, avoiding session file lock conflicts.
        chat_id: If provided, scan only this chat. Otherwise scan all chats.

    Returns:
        Summary dict with gap-fill statistics.
    """
    backup = await TelegramBackup.create(config, client=client)
    try:
        await backup.connect()
        summary = await backup._fill_gaps(chat_id=chat_id)

        # Refresh cached stats if messages were recovered so the viewer
        # doesn't show stale totals until the next scheduled recalculation
        if summary["total_recovered"] > 0:
            try:
                await backup.db.calculate_and_store_statistics(storage_path=config.backup_path)
                logger.info("Stats recalculated after gap-fill recovery")
            except Exception as e:
                logger.warning(f"Failed to recalculate stats after gap-fill: {e}")

        return summary
    finally:
        await backup.disconnect()
        await backup.db.close()


def main():
    """Main entry point for CLI."""
    import asyncio

    from .config import Config, setup_logging
    from .migrate_shared_media import migrate_shared_media

    config = Config()
    setup_logging(config)

    migrate_shared_media(config.media_path)

    return asyncio.run(run_backup(config))


if __name__ == "__main__":
    # Test backup
    main()
