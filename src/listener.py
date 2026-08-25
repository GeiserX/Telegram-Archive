"""
Real-time event listener for Telegram message edits and deletions.
Catches events as they happen and updates the local database immediately.

Safety features:
- LISTEN_EDITS: Apply text edits (default: true, safe)
- LISTEN_DELETIONS: Delete messages (default: false, opt-in mirror mode)
- Mass operation detection: Blocks bulk edits/deletions to protect data

Mass operation protection is rate limiting, not buffering. Operations under
the threshold are applied immediately; disable LISTEN_DELETIONS to guarantee
Telegram deletions never remove archived messages.
"""

import asyncio
import json
import logging
import os
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    UpdateMessageReactions,
    UpdatePinnedChannelMessages,
    UpdatePinnedMessages,
    User,
)
from telethon.utils import get_peer_id

from .avatar_utils import get_avatar_paths
from .config import AccountConfig, Config
from .db import DatabaseAdapter, create_adapter
from .db.models import account_metadata_key
from .event_webhook import EventWebhookSender
from .message_utils import (
    METADATA_ONLY_MEDIA_TYPES,
    _photo_size_bytes,
    build_media_filename,
    classify_extended_media,
    compute_file_hash_async,
    describe_exception,
    download_and_shard_media,
    downloadable_media_payload,
    extract_extended_media_details,
    extract_forward_origin,
    extract_media_attributes,
    extract_reactions,
    extract_topic_id,
    extract_webpage_preview,
    fallback_media_filename,
    finalize_atomic_download,
    message_plain_text,
    sanitize_media_filename,
    sender_display_name,
    serialize_message_entities,
    service_action_type,
    service_message_text,
    utcnow_naive,
)
from .realtime import NotificationType, RealtimeNotifier
from .telegram_backup import absorb_media_floods, call_with_flood_retry

logger = logging.getLogger(__name__)


class MassOperationProtector:
    """
    Rate-limiting protection against mass deletions/edits.

    HOW IT WORKS:
    - Uses a sliding time window to count operations per chat
    - Operations are applied IMMEDIATELY if under threshold
    - Once threshold exceeded, chat is blocked for remainder of window

    PARAMETERS:
    - THRESHOLD (default 10): Max operations allowed in the time window
    - WINDOW_SECONDS (default 30): Sliding time window for counting operations

    EXAMPLE:
    - User deletes 2 messages → both applied immediately ✓
    - User deletes 10 messages over 30s → all applied ✓
    - Attacker deletes 50 messages in 10s → first 10 applied, remaining 40 blocked ✓

    This provides RATE LIMITING - normal usage works, mass attacks are capped.
    For zero deletions from backup, disable LISTEN_DELETIONS entirely.
    """

    def __init__(
        self,
        threshold: int = 10,
        window_seconds: int = 30,
    ):
        """
        Args:
            threshold: Max operations allowed per chat in the time window
            window_seconds: Sliding window for counting operations
        """
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.window = timedelta(seconds=window_seconds)

        # Operation history for sliding window: {chat_id: deque of timestamps}
        self._operation_history: dict[int, deque[datetime]] = {}

        # Blocked chats: {chat_id: (blocked_until, reason, blocked_count)}
        self._blocked: dict[int, tuple[datetime, str, int]] = {}

        self._running = False

        # Statistics
        self.stats = {
            "operations_applied": 0,
            "operations_blocked": 0,
            "rate_limits_triggered": 0,
            "chats_rate_limited": set(),
        }

    def start(self):
        """Start the protector."""
        self._running = True
        logger.info(f"🛡️ Rate limiter active: max {self.threshold} ops per {self.window_seconds}s per chat")

    async def stop(self):
        """Stop the protector."""
        self._running = False

    def is_blocked(self, chat_id: int) -> tuple[bool, str]:
        """Check if a chat is currently rate-limited."""
        if chat_id in self._blocked:
            blocked_until, reason, _ = self._blocked[chat_id]
            if datetime.now() < blocked_until:
                return True, reason
            else:
                # Block expired
                del self._blocked[chat_id]
                logger.info("🔓 Rate limit expired for chat")
        return False, ""

    def _count_ops_in_window(self, chat_id: int) -> int:
        """Count operations in the sliding time window for a chat."""
        if chat_id not in self._operation_history:
            return 0

        now = datetime.now()
        cutoff = now - self.window

        # Clean old entries and count
        history = self._operation_history[chat_id]
        while history and history[0] < cutoff:
            history.popleft()

        return len(history)

    def _record_operation(self, chat_id: int):
        """Record an operation timestamp for sliding window tracking."""
        if chat_id not in self._operation_history:
            self._operation_history[chat_id] = deque()
        self._operation_history[chat_id].append(datetime.now())

    def check_operation(self, chat_id: int, operation_type: str) -> tuple[bool, str]:
        """
        Check if an operation should be allowed.

        Returns (allowed, reason):
            - (True, "allowed") if operation can proceed
            - (False, reason) if chat is rate-limited
        """
        # Check if already blocked
        blocked, reason = self.is_blocked(chat_id)
        if blocked:
            self.stats["operations_blocked"] += 1
            return False, f"RATE LIMITED: {reason}"

        # Record this operation
        self._record_operation(chat_id)

        # Check sliding window
        ops_in_window = self._count_ops_in_window(chat_id)

        if ops_in_window > self.threshold:
            # Rate limit triggered - block further operations
            block_until = datetime.now() + self.window
            reason = f"Rate limit: {ops_in_window} {operation_type}s in {self.window_seconds}s (max: {self.threshold})"
            self._blocked[chat_id] = (block_until, reason, ops_in_window - self.threshold)

            # Update stats
            self.stats["rate_limits_triggered"] += 1
            self.stats["operations_blocked"] += 1
            self.stats["chats_rate_limited"].add(chat_id)

            logger.warning("=" * 70)
            logger.warning("🛡️ RATE LIMIT TRIGGERED")
            logger.warning(f"   Operation type: {operation_type}")
            logger.warning(f"   Operations in {self.window_seconds}s: {ops_in_window} (max: {self.threshold})")
            logger.warning(f"   First {self.threshold} were applied, remaining blocked")
            logger.warning(f"   Chat blocked until: {block_until}")
            logger.warning("=" * 70)

            return False, reason

        # Operation allowed
        self.stats["operations_applied"] += 1
        return True, "allowed"

    def get_stats(self) -> dict[str, Any]:
        """Get rate limiter statistics."""
        return {
            "operations_applied": self.stats["operations_applied"],
            "operations_blocked": self.stats["operations_blocked"],
            "rate_limits_triggered": self.stats["rate_limits_triggered"],
            "chats_rate_limited": len(self.stats["chats_rate_limited"]),
            "currently_blocked": len([c for c in self._blocked if datetime.now() < self._blocked[c][0]]),
        }

    def get_blocked_chats(self) -> dict[int, tuple[str, int]]:
        """Get currently rate-limited chats."""
        now = datetime.now()
        return {
            chat_id: (reason, blocked_count)
            for chat_id, (blocked_until, reason, blocked_count) in self._blocked.items()
            if now < blocked_until
        }


class TelegramListener:
    """
    Real-time event listener for Telegram.

    Catches message edits and deletions as they happen and updates the database.
    Designed to run alongside the scheduled backup process.

    RATE LIMITING PROTECTION:
    Uses a sliding window to limit operations per chat. Normal usage (deleting
    a few messages) works instantly. Mass operations (deleting 50+ messages)
    are blocked after the threshold, protecting most of your backup.

    Example: threshold=10, window=30s
    - Delete 2 messages → both applied ✓
    - Delete 50 messages in 10s → first 10 applied, remaining 40 blocked ✓

    Safety features:
    - LISTEN_EDITS: Only sync edits if enabled (default: true)
    - LISTEN_DELETIONS: Sync deletions with rate limiting (default: true)
    - For zero deletions from backup, set LISTEN_DELETIONS=false
    """

    def __init__(
        self,
        config: Config,
        db: DatabaseAdapter,
        client: TelegramClient | None = None,
        *,
        account_id: int | None = None,
        account: AccountConfig | None = None,
        account_resolver=None,
    ):
        """
        Initialize the listener.

        Args:
            config: Configuration object
            db: Database adapter (must be initialized)
            client: Optional existing TelegramClient to use (for shared connection).
                   If not provided, will create a new client in connect().
            account_id: accounts.id every row written by this listener belongs to.
                May be None only when ``account_resolver`` is given.
            account: The configured account this listener captures for (session
                file and API credentials for the own-client path). Defaults to
                ``config.accounts[0]`` — the synthesized legacy account in a
                zero-config deployment.
            account_resolver: Optional ``async (client, db) -> int`` awaited by
                connect() once the client is proven authorized — and before any
                event handler is registered — yielding the accounts.id. Needed
                by the own-client path, where no client exists before connect()
                and the row is keyed on the Telegram user id.
        """
        if account_id is None and account_resolver is None:
            raise ValueError("account_id or account_resolver is required")
        self.config = config
        self.config.validate_credentials()
        self.db = db
        self.account_id = account_id
        self.account = account if account is not None else config.accounts[0]
        self._account_resolver = account_resolver
        self.client: TelegramClient | None = client
        self._owns_client = client is None  # Track if we created the client
        self._running = False
        self._tracked_chat_ids: set[int] = set()
        # Supergroups adopted via FOLLOW_CHAT_MIGRATIONS (#228). Kept as a
        # dedicated set (not just folded into _tracked_chat_ids) so whitelist
        # mode — which ignores _tracked_chat_ids — can still process them.
        self._followed_live: set[int] = set()

        # Zero-footprint mass operation protection
        self._protector = MassOperationProtector(
            threshold=config.mass_operation_threshold,
            window_seconds=config.mass_operation_window_seconds,
        )

        # Reaction debounce buffer (#219): a hot message fires many reaction updates,
        # each a full snapshot, so we coalesce per (chat_id, message_id) keeping only
        # the latest and flush on a timer — one reconcile + one broadcast per window.
        # Kept separate from the MassOperationProtector so a reaction storm can never
        # rate-limit edits/deletions in the same chat.
        self._reaction_pending: dict[tuple[int, int], list[dict]] = {}
        self._reaction_flush_task: asyncio.Task | None = None

        # Real-time notifier for viewer WebSocket updates
        self._notifier: RealtimeNotifier | None = None

        # Callbacks handed to client.add_event_handler, kept so stop() can detach
        # them again. Telethon only ever appends, and the scheduler builds a NEW
        # listener on the SAME shared client after every network blip, so without
        # this every restart left another live instance dispatching the same
        # updates: duplicate writes, duplicate broadcasts, duplicate downloads.
        self._registered_handlers: list = []

        # Statistics
        self.stats = {
            "edits_received": 0,
            "edits_applied": 0,
            "edits_skipped": 0,  # No-op edits (already current / not archived)
            "deletions_received": 0,
            "deletions_applied": 0,
            "deletions_skipped": 0,  # Skipped due to LISTEN_DELETIONS=false
            "new_messages_received": 0,
            "new_messages_saved": 0,
            "reactions_received": 0,
            "reactions_applied": 0,
            "bursts_intercepted": 0,
            "operations_discarded": 0,
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_dropped": 0,
            "errors": 0,
            "start_time": None,
        }

        # Outbound event webhook (#336): fires on listener-applied edits and
        # deletions. Inert unless EVENT_WEBHOOK_ENABLED with a valid URL.
        self._event_webhook = EventWebhookSender(self.config, self.stats)

        # Log safety settings
        logger.info("=" * 70)
        logger.info("🛡️ TelegramListener initialized with ZERO-FOOTPRINT PROTECTION")
        logger.info("=" * 70)
        logger.info(f"  LISTEN_EDITS: {config.listen_edits}")
        if config.listen_deletions:
            logger.warning("  ⚠️ LISTEN_DELETIONS: true - Deletions will be processed (with protection)")
            logger.info(f"  DELETION_MODE: {getattr(config, 'deletion_mode', 'hard')}")
        else:
            logger.info("  LISTEN_DELETIONS: false (backup fully protected)")
        if config.listen_new_messages:
            logger.info("  LISTEN_NEW_MESSAGES: true - New messages saved in real-time!")
            if config.listen_new_messages_media:
                logger.info("  LISTEN_NEW_MESSAGES_MEDIA: true - Media downloaded immediately!")
            else:
                logger.info("  LISTEN_NEW_MESSAGES_MEDIA: false (media on scheduled backup)")
        else:
            logger.info("  LISTEN_NEW_MESSAGES: false (saved on scheduled backup)")
        if config.listen_reactions:
            logger.info("  LISTEN_REACTIONS: true - Reactions captured in real-time (aggregate, best-effort)")
        else:
            logger.info("  LISTEN_REACTIONS: false (reactions on scheduled backup)")
        if config.skip_topic_ids:
            total = sum(len(t) for t in config.skip_topic_ids.values())
            logger.info(f"  SKIP_TOPIC_IDS: {total} topic(s) excluded across {len(config.skip_topic_ids)} chat(s)")
        logger.info(
            f"  Mass-op rate limit: first {config.mass_operation_threshold} ops per chat per "
            f"{config.mass_operation_window_seconds}s window are applied, the rest blocked"
        )
        logger.info("=" * 70)

    @classmethod
    async def create(
        cls,
        config: Config,
        client: TelegramClient | None = None,
        *,
        account_id: int | None = None,
        account: AccountConfig | None = None,
        account_resolver=None,
    ) -> TelegramListener:
        """
        Factory method to create TelegramListener with initialized database.

        Args:
            config: Configuration object
            client: Optional existing TelegramClient to use (for shared connection)
            account_id: accounts.id every row written by this listener belongs to
                (omit only when ``account_resolver`` is given)
            account: The configured account to listen for (see ``__init__``)
            account_resolver: Deferred accounts.id resolution (see ``__init__``)

        Returns:
            Initialized TelegramListener instance
        """
        db = await create_adapter()
        return cls(config, db, client=client, account_id=account_id, account=account, account_resolver=account_resolver)

    async def connect(self) -> None:
        """
        Connect to Telegram and set up event handlers.

        If a client was provided in __init__, verifies it's connected.
        Otherwise, creates a new client and connects.
        """
        # If using shared client, just verify it's connected
        if self.client is not None and not self._owns_client:
            if not self.client.is_connected():
                raise RuntimeError("Shared client is not connected")
            # This branch has no authorization check of its own. It used to get
            # one by accident: the old log line read me.first_name, and get_me()
            # returns None on an unauthorized session, so an AttributeError blew
            # up connect(). Removing the account name from the log (#272) would
            # have removed that signal too and left the listener attaching
            # handlers to a revoked session in silence, so ask the question
            # directly — is_user_authorized() actually raises, get_me() does not.
            if not await self.client.is_user_authorized():
                raise RuntimeError("Shared client session is not authorized")
            logger.info("Connected")
        else:
            # Create new client
            logger.info(f"Using Telethon session database: {self.account.session_path}.session")
            self.client = TelegramClient(
                self.account.session_path,
                self.account.api_id,
                self.account.api_hash,
                **self.config.get_telegram_client_kwargs(),
            )
            self._owns_client = True

            # Connect and authenticate
            await self.client.connect()

            if not await self.client.is_user_authorized():
                logger.error("❌ Session not authorized!")
                logger.error("Please run the authentication setup first.")
                raise RuntimeError("Session not authorized. Please run authentication setup.")

            # Authorization already proven by the check above; no get_me() needed.
            logger.info("Connected")

        # Resolve which accounts row this login writes under, now that the
        # client is proven authorized. This MUST happen before handlers are
        # registered below: a handler firing with an unresolved account_id
        # would file its rows under the wrong account. Logs nothing at INFO,
        # so the single-account startup output is unchanged.
        if self._account_resolver is not None and self.account_id is None:
            self.account_id = await self._account_resolver(self.client, self.db)

        # Load tracked chat IDs from database
        await self._load_tracked_chats()

        # Initialize real-time notifier (auto-detects PostgreSQL vs SQLite).
        # Bound to THIS listener's own manager — never re-resolved from the
        # process global: a cron backup starting inside connect()'s await
        # window reassigns that global with a fresh engine, and its run-end
        # dispose() would then tear the pool down under the notifier.
        self._notifier = RealtimeNotifier(self.db.db_manager)
        await self._notifier.init()
        logger.info("Real-time notifier initialized")

        # Register event handlers
        self._register_handlers()

        logger.info("Event handlers registered")

    async def _load_tracked_chats(self) -> None:
        """Load list of chat IDs we're backing up (to filter events)."""
        try:
            chats = await self.db.get_all_chats(account_id=self.account_id)
            tracked = {chat["id"] for chat in chats}
            # Supergroups adopted via FOLLOW_CHAT_MIGRATIONS (#228): keep them in
            # a dedicated set AND fold into the type-based tracked set, so the new
            # supergroup is captured live from listener start (even before its chat
            # row is persisted) in BOTH whitelist and type-based modes.
            followed = await self._load_followed_migration_ids()
            # Assigned only after BOTH loads succeed, so a failure part-way
            # through cannot leave the two sets inconsistent.
            self._followed_live = followed
            self._tracked_chat_ids = tracked | followed
            logger.info(f"Tracking {len(self._tracked_chat_ids)} chats for real-time updates")
        except Exception as e:
            # A transient refresh failure must not shrink the live capture
            # scope: every handler gates on _tracked_chat_ids, so replacing a
            # serving set with an empty one silently stops ALL real-time
            # capture until the next scheduled backup reloads it (hours by
            # default) — and edits/deletions in that window are exactly what
            # the listener exists to catch. Keep the previous known-good sets.
            if self._tracked_chat_ids:
                logger.warning(
                    f"Could not refresh tracked chats ({e.__class__.__name__}); "
                    f"keeping the previous set of {len(self._tracked_chat_ids)} chats"
                )
            else:
                logger.warning(
                    f"Could not load tracked chats ({e.__class__.__name__}); "
                    "real-time capture stays disabled until a load succeeds"
                )

    async def _load_followed_migration_ids(self) -> set[int]:
        """Read adopted-supergroup ids from the metadata KV (#228).

        Empty unless FOLLOW_CHAT_MIGRATIONS is on. A missing or malformed
        value degrades to an empty set — that is a real observation. A
        DATABASE failure raises instead: swallowing it here would hand
        _load_tracked_chats an empty set that looks like success, silently
        dropping every followed supergroup from the live scope — the exact
        failure mode its keep-previous-scope guard exists to stop.
        """
        if not getattr(self.config, "follow_chat_migrations", False):
            return set()
        raw = await self.db.get_metadata(account_metadata_key("followed_migrations", self.account_id))
        if not raw:
            return set()
        try:
            loaded = json.loads(raw)
        except ValueError, TypeError:
            logger.warning("Malformed followed_migrations metadata; ignoring")
            return set()
        if isinstance(loaded, list):
            return {x for x in loaded if isinstance(x, int)}
        return set()

    def _get_marked_id(self, entity_or_peer) -> int:
        """
        Get the marked ID for an entity (with -100 prefix for channels/supergroups).
        """
        try:
            return get_peer_id(entity_or_peer)
        except Exception:
            # Fallback for raw IDs
            if hasattr(entity_or_peer, "id"):
                return entity_or_peer.id
            return entity_or_peer

    async def _notify_update(self, notification_type: str, data: dict) -> None:
        """
        Send a real-time notification to the viewer.

        Args:
            notification_type: Type of notification ('edit', 'delete', 'new_message')
            data: Notification data (must include 'chat_id')
        """
        if self._notifier is None:
            return

        try:
            from .realtime import NotificationType

            # Map string types to enum
            type_map = {
                "edit": NotificationType.EDIT,
                "delete": NotificationType.DELETE,
                "new_message": NotificationType.NEW_MESSAGE,
                "pin": NotificationType.PIN,
                "reaction": NotificationType.REACTION,
            }

            nt = type_map.get(notification_type)
            if nt is None:
                logger.warning(f"Unknown notification type: {notification_type}")
                return

            chat_id = data.get("chat_id", 0)
            await self._notifier.notify(nt, chat_id, data, account_id=self.account_id)
        except Exception as e:
            logger.debug(f"Failed to send notification: {e}")

    def _buffer_reaction_snapshot(self, chat_id: int, message: object, *, overwrite: bool = True) -> None:
        """Feed a message's reaction snapshot into the debounce buffer (#221).

        Skips when the reactions feature is off, when the message carries no
        reactions object at all (an edit or new message without one conveys NO
        information — treating it as an authoritative empty snapshot could
        tombstone reactions observed moments earlier), and when Telegram marks
        the object ``min`` (partial; documented to omit the current user's own
        reactions, so reconciling it could falsely remove a self-reaction).

        ``overwrite=False`` is for producers whose capture-to-write path crosses
        an await (the new-message handler): a snapshot buffered meanwhile by the
        live reaction handler is strictly fresher and must win. Synchronous
        capture-and-write producers keep arrival order and may overwrite.
        """
        if not self.config.listen_reactions:
            return
        reactions_obj = getattr(message, "reactions", None)
        if reactions_obj is None or getattr(reactions_obj, "min", False):
            return
        observed = extract_reactions(reactions_obj)
        if observed is None:
            return
        key = (chat_id, message.id)
        if overwrite:
            self._reaction_pending[key] = observed
        else:
            self._reaction_pending.setdefault(key, observed)
        self.stats["reactions_received"] += 1

    async def _flush_reactions(self) -> None:
        """Reconcile and broadcast the coalesced reaction snapshots (#219).

        Drains the debounce buffer atomically (swap-then-process) so handlers can
        keep buffering while this runs. Each entry is a full snapshot, so a single
        reconcile per message is loss-free. PII: log error classes only.
        """
        if not self._reaction_pending:
            return
        pending = self._reaction_pending
        self._reaction_pending = {}
        for (chat_id, message_id), observed in pending.items():
            try:
                outcome = await self.db.reconcile_reactions(
                    message_id, chat_id, observed, account_id=self.account_id, mark_removed=True
                )
                if outcome == "reconciled":
                    self.stats["reactions_applied"] += 1
                    await self._notify_update(
                        "reaction",
                        {"chat_id": chat_id, "message_id": message_id, "reactions": observed},
                    )
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Error reconciling reactions: {type(e).__name__}")

    async def _reaction_flush_loop(self) -> None:
        """Periodically flush the reaction debounce buffer while the listener runs."""
        try:
            while self._running:
                await asyncio.sleep(self.config.reaction_debounce_seconds)
                await self._flush_reactions()
        except asyncio.CancelledError:
            raise

    def _get_deletion_mode(self) -> str:
        """Return configured deletion mode, defaulting to legacy hard delete."""
        mode = getattr(self.config, "deletion_mode", "hard")
        return "soft" if mode == "soft" else "hard"

    async def _webhook_chat_title(self, chat_id: int) -> str:
        """Archive-sourced chat title for webhook contexts.

        Sourced from the archive, not the event, so it works for peerless
        deletions where event.get_chat() is impossible. Private chats compose
        a display name from first/last/username; "" = blank-per-missing rule.
        """
        chat = await self.db.get_chat_by_id(chat_id, account_id=self.account_id)
        if not chat:
            return ""
        if chat.get("title"):
            return chat["title"]
        name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
        if chat.get("username"):
            name = f"{name} (@{chat['username']})" if name else chat["username"]
        return name

    async def _fire_event_webhook(self, event: str, chat_id: int, **fields) -> None:
        """Build the context and hand off to the sender; never raises into a handler."""
        try:
            if not self._event_webhook.wants(event, chat_id):
                return
            context = {
                "event": event,
                "chat_id": chat_id,
                "account_id": self.account_id,
                "chat_title": await self._webhook_chat_title(chat_id),
                **fields,
            }
            self._event_webhook.fire(event, context)
        except Exception as e:
            # PII rule: exception class name only — no chat ids, no content.
            logger.debug("Event webhook fire skipped: %s", type(e).__name__)

    async def _apply_message_deletion(self, chat_id: int, message_id: int) -> None:
        """Apply a Telegram deletion event according to DELETION_MODE."""
        deletion_mode = self._get_deletion_mode()

        if deletion_mode == "soft":
            deleted_at = utcnow_naive()
            prior = await self.db.mark_message_deleted(
                chat_id, message_id, deleted_at=deleted_at, account_id=self.account_id
            )
            event_date = deleted_at
            logger.debug("🗑️ Deletion marked")
            await self._notify_update(
                "delete",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "deletion_mode": "soft",
                    "deleted_at": deleted_at.isoformat(),
                },
            )
        else:
            prior = await self.db.delete_message(chat_id, message_id, account_id=self.account_id)
            # Telegram delete events carry no timestamp; observation time it is.
            event_date = utcnow_naive()
            logger.debug("🗑️ Deletion applied")
            await self._notify_update(
                "delete",
                {"chat_id": chat_id, "message_id": message_id, "deletion_mode": "hard"},
            )

        # Fire the event webhook post-commit. prior=None means never archived;
        # a truthy prior is_deleted means an already-tombstoned re-mark — both
        # skipped so delivery stays exactly-once per archived message.
        if prior is not None and not prior.get("is_deleted"):
            await self._fire_event_webhook(
                "message_deleted",
                chat_id,
                message_id=message_id,
                sender_id=prior.get("sender_id"),
                sender_name=prior.get("sender_name"),
                date=event_date,
                text=prior.get("text"),
                media_type=prior.get("media_type"),
            )

    def _should_process_chat(
        self,
        chat_id: int,
        *,
        is_user: bool | None = None,
        is_group: bool | None = None,
        is_channel: bool | None = None,
    ) -> bool:
        """
        Check if we should process events for this chat.

        Two modes:

        MODE 1 - Whitelist Mode (CHAT_IDS is set):
            Only process events for chats explicitly listed in CHAT_IDS, plus
            any supergroup adopted via FOLLOW_CHAT_MIGRATIONS (#228).

        MODE 2 - Type-based Mode:
            Process if:
            - Chat is in our tracked list (backed up at least once), OR
            - Chat matches our backup filters (include lists), OR
            - The caller already knows the chat's type (is_user/is_group/
              is_channel) and it matches CHAT_TYPES

        The is_user/is_group/is_channel kwargs let a caller that already has
        cheap, no-network type info (e.g. NewMessage.Event.is_private/
        is_group/is_channel, derived from the update's peer) evaluate a
        brand-new, never-tracked chat against CHAT_TYPES immediately -
        without them, a chat we've never backed up falls through to
        "conservative: only explicit include lists match", i.e. first
        contact from someone we've never chatted with is invisible to the
        listener until the next scheduled backup discovers it. Bots can't
        be distinguished from regular users this way (that needs the sender
        entity, which isn't synchronously available), so is_user=True is
        passed for both - a first DM from a bot is evaluated against
        CHAT_TYPES' "private" bucket rather than "bots" until the next
        scheduled backup reclassifies it.
        """
        # MODE 1: Whitelist Mode - CHAT_IDS takes absolute priority. Followed
        # migrations are added explicitly (not via _tracked_chat_ids, which
        # whitelist mode ignores) so a migrated supergroup keeps flowing live.
        if self.config.whitelist_mode:
            return chat_id in self.config.chat_ids or chat_id in self._followed_live

        # MODE 2: Type-based Mode
        # First, check if it's in our tracked chats
        if chat_id in self._tracked_chat_ids:
            return True

        # If not tracked yet, check if it would be backed up based on config
        if chat_id in self.config.global_include_ids:
            return True
        if chat_id in self.config.private_include_ids:
            return True
        if chat_id in self.config.groups_include_ids:
            return True
        if chat_id in self.config.channels_include_ids:
            return True

        # First-contact fallback: only take this path when the caller
        # supplied real type info; otherwise stay conservative and return
        # False rather than guess.
        if is_user is not None or is_group is not None or is_channel is not None:
            return self.config.should_backup_chat_type(bool(is_user), bool(is_group), bool(is_channel))

        return False

    def _get_chat_type(self, entity) -> str:
        """Determine chat type from Telethon entity."""
        from telethon.tl.types import Channel
        from telethon.tl.types import Chat as TelethonChat
        from telethon.tl.types import User as TelethonUser

        if isinstance(entity, TelethonUser):
            return "private"
        elif isinstance(entity, TelethonChat):
            return "group"
        elif isinstance(entity, Channel):
            return "channel" if not entity.megagroup else "group"
        return "unknown"

    async def _download_avatar(self, entity, chat_id: int) -> None:
        """
        Download the current profile photo/avatar for a chat or user.

        Called when a photo_changed event is detected to immediately
        update the avatar without waiting for the next scheduled backup.
        """
        try:
            avatar_path, _legacy_path = get_avatar_paths(self.config.media_path, entity, chat_id)

            if avatar_path is None:
                logger.debug("No avatar set")
                return

            # lexists short-circuits when an avatar (even a symlink whose
            # target is unreachable from this process) is already on disk,
            # so we don't try to overwrite an archive entry. Mirrors the
            # backup-flow guard in src/telegram_backup.py (issue #143).
            if os.path.lexists(avatar_path):
                if os.path.islink(avatar_path) or os.path.getsize(avatar_path) > 0:
                    return

            result = await self.client.download_profile_photo(entity, file=avatar_path, download_big=False)
            if result:
                logger.info("📷 Avatar downloaded")
            else:
                logger.debug("No avatar available")
        except Exception as e:
            logger.warning(f"Failed to download avatar: {describe_exception(e)}")

    def _get_media_type(self, media) -> str | None:
        """Get media type as string."""
        if isinstance(media, MessageMediaPhoto):
            return "photo"
        elif isinstance(media, MessageMediaDocument):
            # Check document attributes to determine specific type
            if hasattr(media, "document") and media.document:
                # DocumentEmpty is truthy but carries no .attributes at all. Its reference
                # is unusable, so treat it exactly like a missing document rather than
                # classifying it as a real one and sending it down the download path.
                attributes = getattr(media.document, "attributes", None)
                if attributes is None:
                    return None
                is_animated = False
                for attr in attributes:
                    attr_type = type(attr).__name__
                    if "Animated" in attr_type:
                        is_animated = True
                    if "Video" in attr_type:
                        return "animation" if is_animated else "video"
                    elif "Audio" in attr_type:
                        if hasattr(attr, "voice") and attr.voice:
                            return "voice"
                        return "audio"
                    elif "Sticker" in attr_type:
                        return "sticker"
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
        # The nine kinds this ladder used to flatten to None (venue, dice,
        # invoice, story, giveaways, live location, game, unsupported):
        # metadata-only types with a typed viewer chip, never downloaded.
        if type(media).__name__ == "MessageMediaWebPage":
            webpage = getattr(media, "webpage", None)
            if type(webpage).__name__ == "WebPage" and (
                getattr(webpage, "photo", None) is not None or getattr(webpage, "document", None) is not None
            ):
                return "webpage"
            return None  # card-only preview (raw_data.webpage): nothing to download
        return classify_extended_media(media)

    def _get_media_filename(self, message, media_type: str, telegram_file_id: str | None = None) -> str:
        """Generate a filename for media."""
        # Try to get original filename from document
        original_name = None
        mime_type = None

        media_payload = downloadable_media_payload(message.media)
        if hasattr(media_payload, "document") and media_payload.document:
            doc = media_payload.document
            mime_type = getattr(doc, "mime_type", None)
            for attr in getattr(doc, "attributes", None) or ():
                if hasattr(attr, "file_name") and attr.file_name:
                    original_name = attr.file_name
                    break

        # Use Telegram file ID + original name for deduplication.
        # Length-budget the decorative name for constrained filesystems (#212).
        if original_name and telegram_file_id:
            return build_media_filename(telegram_file_id, original_name, self.config.max_filename_bytes)
        if original_name:
            return sanitize_media_filename(original_name)

        # No usable original name — shared fallback (message_utils) keeps this
        # identical to the backup module's ingest path for the same inputs.
        return fallback_media_filename(telegram_file_id, media_type, mime_type, message.id)

    async def _download_media(self, message, chat_id: int) -> tuple[str, str, str | None] | None:
        """
        Download media from a message.

        Returns (file_path, file_name, content_hash) if successful, None otherwise.
        """
        media = message.media
        media_type = self._get_media_type(media)

        if not media_type or media_type in METADATA_ONLY_MEDIA_TYPES:
            return None  # These don't have downloadable files

        try:
            # Get Telegram's file unique ID for deduplication. Webpage previews
            # keep their photo/document one level down — unwrap once.
            payload = downloadable_media_payload(media)
            # Truthy guards, not hasattr: a WebPage carries BOTH .photo and
            # .document (one None), so hasattr would pick the empty photo
            # branch for document-backed previews and lose the file id.
            telegram_file_id = None
            if getattr(payload, "photo", None):
                telegram_file_id = str(getattr(payload.photo, "id", None))
            elif getattr(payload, "document", None):
                telegram_file_id = str(getattr(payload.document, "id", None))

            # Guard against inaccessible media producing "None" string IDs
            if telegram_file_id == "None":
                telegram_file_id = None

            # Check file size
            file_size = 0
            if hasattr(payload, "document") and payload.document:
                file_size = getattr(payload.document, "size", 0)
            elif hasattr(payload, "photo") and payload.photo:
                if hasattr(payload.photo, "sizes") and payload.photo.sizes:
                    # PhotoSizeProgressive (the full rendition) has no scalar
                    # .size, so max-by-getattr scored it 0 and a thumbnail won
                    # — the size gate then measured kilobytes for a photo of
                    # megabytes. _photo_size_bytes reads both shapes.
                    file_size = max(_photo_size_bytes(s) for s in payload.photo.sizes)

            max_size = self.config.get_max_media_size_bytes()
            if file_size > max_size:
                logger.debug(f"Skipping large media file: {file_size / 1024 / 1024:.2f} MB")
                return None

            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)

            # Generate filename
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
            file_path = os.path.join(chat_media_dir, file_name)

            # Download with deduplication if enabled
            content_hash = None
            if getattr(self.config, "deduplicate_media", True):
                # Global deduplication: use _shared directory for actual files
                shared_dir = os.path.join(self.config.media_path, "_shared")
                os.makedirs(shared_dir, exist_ok=True)

                async def _download_fn(tmp_path):
                    # absorb_media_floods raises a CLIENT-WIDE attribute, so it may only be
                    # held for one transfer attempt — never across the retry wrapper's flood
                    # sleeps, which would leave every other coroutine (the scheduled backup
                    # shares this client) silently sleeping inside Telethon for hours. Same
                    # ordering as the backup path's _fetch_media_bytes.
                    async def _attempt():
                        async with absorb_media_floods(
                            self.client, getattr(self.config, "media_flood_sleep_threshold", 0)
                        ):
                            return await self.client.download_media(message, tmp_path)

                    try:
                        return await call_with_flood_retry(_attempt, client=self.client)
                    except BaseException:
                        # Never leave a partial .part behind on failure or cancellation.
                        if os.path.exists(tmp_path):
                            try:
                                os.remove(tmp_path)
                            except OSError:
                                pass
                        raise

                shared_file_path, content_hash = await download_and_shard_media(
                    db=self.db,
                    download_coro=_download_fn,
                    shared_dir=shared_dir,
                    chat_media_dir=chat_media_dir,
                    file_name=file_name,
                    file_path=file_path,
                    logger=logger,
                    account_id=self.account_id,
                )
                if not shared_file_path and not os.path.lexists(file_path):
                    return None
            else:
                # No deduplication - download directly. lexists short-circuits
                # the download when a symlink is already recorded, even if its
                # target is unreachable.
                if not os.path.lexists(file_path):
                    task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
                    tmp_file_path = f"{file_path}.{os.getpid()}.{task_id}.part"
                    if os.path.exists(tmp_file_path):
                        os.remove(tmp_file_path)

                    async def _attempt():
                        async with absorb_media_floods(
                            self.client, getattr(self.config, "media_flood_sleep_threshold", 0)
                        ):
                            return await self.client.download_media(message, tmp_file_path)

                    try:
                        actual_path = await call_with_flood_retry(_attempt, client=self.client)
                    except BaseException:
                        # Never leave a partial .part behind on failure or cancellation.
                        if os.path.exists(tmp_file_path):
                            try:
                                os.remove(tmp_file_path)
                            except OSError:
                                pass
                        raise
                    file_path = finalize_atomic_download(
                        actual_path if isinstance(actual_path, str) else None,
                        tmp_file_path,
                        file_path,
                    )
                    if not file_path or not os.path.exists(file_path):
                        logger.warning("Media download did not produce a file")
                        return None

            # Compute content hash only if not already obtained during dedup
            if not content_hash:
                resolved = file_path
                if os.path.islink(file_path):
                    resolved = os.path.realpath(file_path)
                content_hash = await compute_file_hash_async(resolved) if os.path.exists(resolved) else None

            # Return the path as stored in DB (relative to media root)
            return f"{self.config.media_path}/{chat_id}/{file_name}", file_name, content_hash

        except Exception as e:
            logger.error(f"Error downloading media: {describe_exception(e)}")
            return None

    def _register_handlers(self) -> None:
        """Register Telethon event handlers."""

        @self.client.on(events.MessageEdited)
        async def on_message_edited(event: events.MessageEdited.Event) -> None:
            """
            Handle message edit events.

            Operations are QUEUED, not applied immediately.
            The background processor applies them after the buffer delay,
            allowing burst detection BEFORE any data is modified.
            """
            # Check if edits are enabled
            if not self.config.listen_edits:
                return

            try:
                chat_id = self._get_marked_id(event.chat_id)

                if not self._should_process_chat(chat_id):
                    return

                # Skip edits in excluded forum topics
                message = event.message
                if self.config.should_skip_topic(chat_id, extract_topic_id(message)):
                    return

                # Reaction-carrying edits (#221): Telegram delivers some reaction
                # changes as genuine UpdateEditMessage events (Telethon #4635), which
                # our text-outcome early return below would otherwise discard. Harvest
                # them into the same debounce buffer as the live reaction handler,
                # BEFORE the rate-limit check and the text early return, so a
                # reaction-only edit still reconciles and an edit rate limit can't
                # suppress it. Capture and write are synchronous here, so arrival
                # order is preserved and overwriting is correct.
                self._buffer_reaction_snapshot(chat_id, message)

                self.stats["edits_received"] += 1
                new_text = message_plain_text(message)
                edit_date = message.edit_date

                # Check rate limit before applying
                allowed, reason = self._protector.check_operation(chat_id, "edit")

                if not allowed:
                    self.stats["operations_discarded"] += 1
                    return

                # Apply the edit immediately; count and broadcast only when the
                # archive actually changed, so stats stay honest and the viewer
                # never displays text the archive rejected as stale.
                outcome, prior = await self.db.update_message_text(
                    chat_id=chat_id,
                    message_id=message.id,
                    new_text=new_text,
                    edit_date=edit_date,
                    account_id=self.account_id,
                    entities=serialize_message_entities(getattr(message, "entities", None)),
                    update_entities=True,
                )
                if outcome != "applied":
                    self.stats["edits_skipped"] += 1
                    logger.debug("📝 Edit skipped (%s)", outcome)
                    return

                self.stats["edits_applied"] += 1
                logger.debug("📝 Edit applied")

                # Notify viewer of the update
                await self._notify_update(
                    "edit",
                    {
                        "chat_id": chat_id,
                        "message_id": message.id,
                        "new_text": new_text,
                        "edit_date": edit_date.isoformat() if edit_date else None,
                    },
                )

                await self._fire_event_webhook(
                    "message_edited",
                    chat_id,
                    message_id=message.id,
                    sender_id=(prior or {}).get("sender_id") or message.sender_id,
                    sender_name=(prior or {}).get("sender_name"),
                    date=edit_date,
                    text=new_text,
                    new_text=new_text,
                    old_text=(prior or {}).get("text"),
                    media_type=self._get_media_type(message.media) if message.media else None,
                )

            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Error processing edit event: {e}", exc_info=True)

        @self.client.on(events.MessageDeleted)
        async def on_message_deleted(event: events.MessageDeleted.Event) -> None:
            """
            Handle message deletion events.

            Rate-limited: if too many deletions occur in a short time,
            further deletions are blocked to protect the backup.
            """
            # Check if deletions are enabled (DEFAULT: FALSE; opt-in mirror mode)
            if not self.config.listen_deletions:
                if event.deleted_ids:
                    self.stats["deletions_skipped"] += len(event.deleted_ids)
                    logger.debug(f"⏭️ Deletion skipped (LISTEN_DELETIONS=false): {len(event.deleted_ids)} messages")
                return

            try:
                # Note: event.chat_id might be None for some deletion events
                chat_id = event.chat_id
                if chat_id is not None:
                    chat_id = self._get_marked_id(chat_id)

                    if not self._should_process_chat(chat_id):
                        return

                # Process each deletion
                for msg_id in event.deleted_ids:
                    self.stats["deletions_received"] += 1

                    # If chat_id is unknown, resolve it from DB first
                    # Message IDs are only unique within a chat — skip ambiguous cases
                    effective_chat_id = chat_id
                    if effective_chat_id is None:
                        try:
                            resolved = await self.db.resolve_message_chat_id(msg_id, account_id=self.account_id)
                            if resolved is None:
                                logger.debug("⚠️ Deletion skipped (not found or ambiguous)")
                                continue

                            if not self._should_process_chat(resolved):
                                continue

                            # Apply rate limit like the normal path
                            allowed, reason = self._protector.check_operation(resolved, "deletion")
                            if not allowed:
                                self.stats["operations_discarded"] += 1
                                continue

                            await self._apply_message_deletion(resolved, msg_id)
                            self.stats["deletions_applied"] += 1
                        except Exception as e:
                            self.stats["errors"] += 1
                            logger.warning(f"Could not apply deletion: {e}")
                        continue

                    if not self._should_process_chat(effective_chat_id):
                        continue

                    # Check rate limit before applying
                    allowed, reason = self._protector.check_operation(effective_chat_id, "deletion")

                    if not allowed:
                        self.stats["operations_discarded"] += 1
                        continue

                    # Apply the deletion immediately
                    await self._apply_message_deletion(effective_chat_id, msg_id)
                    self.stats["deletions_applied"] += 1

            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Error processing deletion event: {e}", exc_info=True)

        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            """
            Handle new messages.

            If LISTEN_NEW_MESSAGES is enabled, saves messages to database in real-time.
            Otherwise, just tracks chat IDs for edits/deletions.
            """
            try:
                chat_id = self._get_marked_id(event.chat_id)

                # NewMessage carries its peer type (PeerUser/PeerChat/PeerChannel)
                # synchronously via _chat_peer - no entity fetch needed - so a chat
                # we've never backed up before can be matched against CHAT_TYPES
                # right now instead of being dropped until the next scheduled
                # backup adds it to _tracked_chat_ids. Otherwise a first message
                # from someone we've never chatted with is invisible to the
                # listener (see _should_process_chat).
                #
                # Telethon's event.is_channel is True for BOTH broadcast channels
                # and megagroups (any PeerChannel), while event.is_group is True
                # for megagroups too - so a megagroup would otherwise set both
                # flags and could match a channels-only CHAT_TYPES filter it
                # should be excluded from. _get_chat_type() already treats a
                # megagroup as "group", never "channel"; mirror that here.
                is_user, is_group, is_channel = event.is_private, event.is_group, event.is_channel
                if is_channel and is_group:
                    is_channel = False

                # Add to tracked chats if we should be backing up this chat
                if chat_id not in self._tracked_chat_ids:
                    if self._should_process_chat(chat_id, is_user=is_user, is_group=is_group, is_channel=is_channel):
                        self._tracked_chat_ids.add(chat_id)
                        logger.debug("Added chat to tracking list")

                # Skip if not in tracked chats
                if not self._should_process_chat(chat_id, is_user=is_user, is_group=is_group, is_channel=is_channel):
                    return

                # Save the message to database
                message = event.message

                # Extract topic ID early for filtering and message_data
                # v6.2.0: reply_to_top_id added for forum topic threading
                reply_to_top_id = extract_topic_id(message)

                # Skip messages in excluded forum topics
                if self.config.should_skip_topic(chat_id, reply_to_top_id):
                    logger.debug("⏭️ Skipping message in excluded topic")
                    return

                self.stats["new_messages_received"] += 1

                # If LISTEN_NEW_MESSAGES is disabled, just track for edits/deletions
                if not self.config.listen_new_messages:
                    return

                # Ensure chat exists in database (prevents FK violation for new chats)
                chat_entity = await event.get_chat()
                if chat_entity:
                    chat_data = {
                        "id": chat_id,
                        "type": self._get_chat_type(chat_entity),
                        "title": getattr(chat_entity, "title", None),
                        "username": getattr(chat_entity, "username", None),
                        "first_name": getattr(chat_entity, "first_name", None),
                        "last_name": getattr(chat_entity, "last_name", None),
                    }
                    await self.db.upsert_chat(chat_data, account_id=self.account_id)

                sender = message.sender
                if sender is None:
                    try:
                        sender = await call_with_flood_retry(
                            event.get_sender,
                            non_retryable=lambda _exc: True,
                        )
                    except Exception:
                        sender = None

                # Save sender information if available
                # sender_user is kept for the WS notify payload below (mirrors the API row's
                # flat first_name/last_name/username fields).
                sender_user = None
                if sender and isinstance(sender, User):
                    user_data = {
                        "id": sender.id,
                        "username": sender.username,
                        "first_name": sender.first_name,
                        "last_name": sender.last_name,
                        "phone": sender.phone,
                        "is_bot": sender.bot,
                    }
                    await self.db.upsert_user(user_data)
                    sender_user = user_data

                message_data = {
                    "id": message.id,
                    "chat_id": chat_id,
                    "sender_id": message.sender_id,
                    "sender_name": sender_display_name(sender),
                    "date": message.date,
                    "text": message_plain_text(message),
                    "reply_to_msg_id": message.reply_to_msg_id if hasattr(message, "reply_to_msg_id") else None,
                    "reply_to_top_id": reply_to_top_id,
                    "reply_to_text": None,
                    "forward_from_id": None,  # Will be filled by next backup if needed
                    "edit_date": message.edit_date,
                    "raw_data": {},
                    "is_outgoing": 1 if message.out else 0,
                }

                # Capture grouped_id for album detection (multiple photos/videos sent together)
                if message.grouped_id:
                    message_data["raw_data"]["grouped_id"] = str(message.grouped_id)

                # Capture-time web preview (mf7) — same shape as the sweep writes.
                webpage_preview = extract_webpage_preview(message.media)
                if webpage_preview is not None:
                    message_data["raw_data"]["webpage"] = webpage_preview

                # Extended media kinds: same raw_data shape the sweep writes.
                extended_media = extract_extended_media_details(message.media)
                if extended_media is not None:
                    extended_kind, extended_details = extended_media
                    message_data["raw_data"][extended_kind] = extended_details
                # Forward origin pointer: pure metadata off the event, no API
                # cost — the sweep writer captures the same key.
                forward_origin = extract_forward_origin(message)
                if forward_origin:
                    message_data["raw_data"]["forward_origin"] = forward_origin

                # Formatting entities — same contract as the sweep writer.
                message_entities = serialize_message_entities(getattr(message, "entities", None))
                if message_entities:
                    message_data["raw_data"]["entities"] = message_entities

                # v6.0.0: Detect media type for logging (download happens after message insert)
                media_type = None
                if message.media:
                    media_type = self._get_media_type(message.media)

                # Insert the message FIRST (required for FK constraint on media table)
                await self.db.insert_message(message_data, account_id=self.account_id)
                self.stats["new_messages_saved"] += 1

                # New messages can arrive already carrying reactions (fast reactors,
                # forwarded content). Buffer them now that the row exists (#221).
                # overwrite=False: the awaits above (insert_message etc.) opened a
                # window in which the live reaction handler may have buffered a
                # FRESHER snapshot for this message — our event-time capture must
                # not clobber it (review finding, reproduced).
                self._buffer_reaction_snapshot(chat_id, message, overwrite=False)

                # v6.0.0: Handle media - create Media record AFTER message exists
                # ws_media mirrors the API row's nested media dict for the WS notify payload
                # below; stays None when media wasn't downloaded/inserted (DB has no record then either).
                ws_media = None
                if media_type:
                    # Download media immediately if enabled
                    if self.config.listen_new_messages_media and self.config.should_download_media_for_chat(chat_id):
                        try:
                            download_result = await self._download_media(message, chat_id)
                            if download_result:
                                media_path, media_file_name, content_hash = download_result
                                # Create media record (FK to messages now satisfied)
                                media_id = f"{chat_id}_{message.id}_{media_type}"
                                # Same metadata the scheduled sweep records (#263) — without it
                                # live-captured voice notes had a NULL duration and rendered
                                # without it while sweep-captured ones showed it.
                                media_attributes = extract_media_attributes(downloadable_media_payload(message.media))
                                try:
                                    media_attributes["file_size"] = os.path.getsize(media_path)
                                except OSError:
                                    pass  # Keep Telegram's reported size when the path isn't stat-able
                                media_row = {
                                    "id": media_id,
                                    "message_id": message.id,
                                    "chat_id": chat_id,
                                    "type": media_type,
                                    "file_path": media_path,
                                    "file_name": media_file_name,
                                    "content_hash": content_hash,
                                    "downloaded": True,
                                    "download_date": utcnow_naive(),
                                    **media_attributes,
                                }
                                await self.db.insert_media(media_row, account_id=self.account_id)
                                logger.debug("📎 Downloaded media")
                                # Mirror the DB row so the WS row matches what the next poll returns.
                                ws_media = {
                                    "id": media_id,
                                    "type": media_type,
                                    "file_path": media_path,
                                    "file_name": media_file_name,
                                    "file_size": media_row["file_size"],
                                    "mime_type": media_row["mime_type"],
                                    "width": media_row["width"],
                                    "height": media_row["height"],
                                    "duration": media_row["duration"],
                                }
                        except Exception as e:
                            logger.warning(
                                f"Failed to download media for message {message.id}: {describe_exception(e)}"
                            )

                # Send real-time notification (enriched to mirror the API row shape so the
                # viewer can render sender name + media immediately instead of a bare row
                # until the next poll: flat user fields + nested media dict). message_data
                # itself is left untouched since it was already passed to db.insert_message.
                if self._notifier:
                    ws_message = {
                        **message_data,
                        "first_name": sender_user["first_name"] if sender_user else None,
                        "last_name": sender_user["last_name"] if sender_user else None,
                        "username": sender_user["username"] if sender_user else None,
                        "media": ws_media,
                    }
                    await self._notifier.notify(
                        NotificationType.NEW_MESSAGE, chat_id, {"message": ws_message}, account_id=self.account_id
                    )

                # Log the new message (no chat_id/msg_id/text — PII)
                media_indicator = f" [{media_type}]" if media_type else ""
                logger.info(f"📩 New message saved{media_indicator}")

            except Exception as e:
                self.stats["errors"] += 1
                # No exc_info: the traceback ends with the raw exception repr, so an
                # OSError would print the media path that describe_exception
                # just removed. Type and (where safe) message are kept.
                logger.error(f"Error in new message handler: {describe_exception(e)}")

        # ChatAction handler - tracks chat metadata changes
        @self.client.on(events.ChatAction)
        async def on_chat_action(event: events.ChatAction.Event) -> None:
            """
            Handle chat action events (photo changes, member joins/leaves, title changes).

            Only active if LISTEN_CHAT_ACTIONS is enabled.
            """
            if not self.config.listen_chat_actions:
                return

            try:
                chat_id = self._get_marked_id(event.chat_id)

                if not self._should_process_chat(chat_id):
                    return

                # Track stats
                if "chat_actions" not in self.stats:
                    self.stats["chat_actions"] = 0
                self.stats["chat_actions"] += 1

                # Only events built from a real service message carry a row we can
                # archive. Participant-sync builds (UpdateChatParticipantAdd/Delete,
                # UpdateChannelParticipant) and unpins pass a peer only, so
                # action_message is None. Writing a row here used to fabricate a
                # wall-clock-derived id that could collide with real message ids and
                # left ~2 phantom rows per join (#222); now we simply skip.
                msg = event.action_message
                if msg is None:
                    logger.debug("Chat action without service message - skipped")
                    return

                # Honor forum topic exclusions exactly like the new-message handler.
                topic_id = extract_topic_id(msg)
                if self.config.should_skip_topic(chat_id, topic_id):
                    logger.debug("⏭️ Skipping chat action in excluded topic")
                    return

                # Storage tag shared with the backfill sweep (chat_joined_by_link,
                # chat_edit_title, ...), derived from the MessageAction class name.
                action_type = service_action_type(msg.action)

                sender = getattr(msg, "sender", None)
                if sender is None:
                    get_sender = getattr(msg, "get_sender", None)
                    if callable(get_sender):
                        try:
                            sender = await call_with_flood_retry(
                                get_sender,
                                non_retryable=lambda _exc: True,
                            )
                        except Exception:
                            sender = None

                # Metadata classification (Telethon 1.43 ChatAction semantics):
                #   photo changed -> new_photo set AND photo is a Photo
                #   photo removed -> new_photo set AND photo is None
                #   title changed -> new_title set AND not a chat/channel creation
                photo_changed = bool(event.new_photo) and event.photo is not None
                photo_removed = bool(event.new_photo) and event.photo is None
                title_changed = event.new_title is not None and not event.created

                if photo_changed:
                    logger.info("📷 Chat photo changed")
                elif photo_removed:
                    logger.info("📷 Chat photo removed")
                elif title_changed:
                    logger.info("📝 Chat title changed")
                elif event.user_joined:
                    logger.debug("👤 User joined")
                elif event.user_added:
                    logger.debug("👤 User added")
                elif event.user_left:
                    logger.debug("👤 User left")
                elif event.user_kicked:
                    logger.debug("👤 User kicked")

                # Resolve the display name of the affected user (the subject of the
                # service sentence) when the event names one.
                actor_name = None
                if hasattr(event, "user_id") and event.user_id:
                    try:
                        actor = await call_with_flood_retry(self.client.get_entity, event.user_id)
                        actor_name = getattr(actor, "first_name", "") or getattr(actor, "title", "")
                        if hasattr(actor, "last_name") and actor.last_name:
                            actor_name += f" {actor.last_name}"
                    except Exception as e:
                        actor_name = None
                        logger.debug("Actor name lookup failed: %s", type(e).__name__)

                service_text = service_message_text(
                    msg.action,
                    actor_name=actor_name,
                    affected_left=bool(event.user_left),
                    affected_joined_self=bool(event.user_joined),
                )

                # Persist with the REAL Telegram id/date so this row upserts cleanly
                # against the same id a later sweep re-scans. events.NewMessage skips
                # MessageService, so this handler is the only real-time source of
                # service rows. raw_data mirrors the sweep shape (service_type /
                # action_type / new_title).
                message_data = {
                    "id": msg.id,
                    "chat_id": chat_id,
                    "sender_id": msg.sender_id,
                    "sender_name": sender_display_name(sender),
                    "date": msg.date,
                    "text": service_text or "",
                    "reply_to_msg_id": msg.reply_to_msg_id,
                    "reply_to_top_id": topic_id,
                    "reply_to_text": None,
                    "forward_from_id": None,
                    "edit_date": None,
                    "raw_data": {
                        "service_type": "service",
                        "action_type": action_type,
                    },
                    "is_outgoing": 1 if msg.out else 0,
                }
                if event.new_title is not None:
                    message_data["raw_data"]["new_title"] = event.new_title

                await self.db.insert_message(message_data, account_id=self.account_id)
                logger.info("📌 Service message saved")

                # Refresh cached chat metadata on a photo or title change. A photo
                # add/swap also re-downloads the avatar; a removal only updates the
                # row (there is no new avatar to fetch).
                if photo_changed or photo_removed or title_changed:
                    try:
                        entity = await call_with_flood_retry(self.client.get_entity, chat_id)
                        if entity:
                            chat_data = {
                                "id": chat_id,
                                # _get_chat_type, not hasattr(entity, "broadcast"):
                                # Telethon's Channel always CARRIES broadcast (False
                                # on a megagroup), so the hasattr test relabelled
                                # every supergroup as a broadcast channel and folder
                                # sync then filed it under the wrong folder flag.
                                "type": self._get_chat_type(entity),
                                "title": getattr(entity, "title", None),
                                "username": getattr(entity, "username", None),
                            }
                            await self.db.upsert_chat(chat_data, account_id=self.account_id)
                            logger.info("✅ Chat metadata updated")

                            if photo_changed:
                                await self._download_avatar(entity, chat_id)
                    except Exception as e:
                        logger.warning(f"Failed to update chat metadata: {type(e).__name__}")

            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Error in chat action handler: {e}", exc_info=True)

        # Note: Album handling removed - NewMessage handler captures grouped_id for album grouping
        # The viewer groups messages by grouped_id automatically

        # Pin/Unpin handler - tracks pinned message changes
        @self.client.on(events.Raw(types=[UpdatePinnedMessages, UpdatePinnedChannelMessages]))
        async def on_pinned_messages(event) -> None:
            """
            Handle pin/unpin events for messages.

            This catches when messages are pinned or unpinned in real-time
            and updates the is_pinned field in the database.
            """
            try:
                # Get chat ID based on the update type
                if isinstance(event, UpdatePinnedChannelMessages):
                    # For channels: channel_id needs -100 prefix
                    chat_id = -1000000000000 - event.channel_id
                    pinned_messages = event.messages  # List of message IDs that are pinned
                    is_pinning = event.pinned  # True if pinning, False if unpinning
                elif isinstance(event, UpdatePinnedMessages):
                    # For groups/private chats: get peer ID
                    peer = event.peer
                    if hasattr(peer, "user_id"):
                        chat_id = peer.user_id
                    elif hasattr(peer, "chat_id"):
                        chat_id = -peer.chat_id
                    elif hasattr(peer, "channel_id"):
                        chat_id = -1000000000000 - peer.channel_id
                    else:
                        return
                    pinned_messages = event.messages
                    is_pinning = event.pinned
                else:
                    return

                if not self._should_process_chat(chat_id):
                    return

                # Track stats
                if "pins" not in self.stats:
                    self.stats["pins"] = 0
                self.stats["pins"] += len(pinned_messages)

                # Update each message's pinned status
                for msg_id in pinned_messages:
                    await self.db.update_message_pinned(chat_id, msg_id, is_pinning, account_id=self.account_id)

                action = "📌 Pinned" if is_pinning else "📌 Unpinned"
                logger.info(f"{action}: {len(pinned_messages)} message(s)")

                # Notify viewer of the update
                await self._notify_update(
                    "pin", {"chat_id": chat_id, "message_ids": pinned_messages, "pinned": is_pinning}
                )

            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Error in pin handler: {e}", exc_info=True)

        @self.client.on(events.Raw(types=[UpdateMessageReactions]))
        async def on_message_reactions(event) -> None:
            """Handle real-time reaction changes (#219, opt-in via LISTEN_REACTIONS).

            UpdateMessageReactions carries the FULL current aggregate snapshot with
            no gap recovery (best-effort). We coalesce bursts per message via the
            debounce buffer and reconcile the latest snapshot; the scheduled sweep
            remains the backstop. PII: never log chat/message/user ids or emoji.
            """
            if not self.config.listen_reactions:
                return
            try:
                chat_id = self._get_marked_id(event.peer)
                if not self._should_process_chat(chat_id):
                    return
                # Forum topic filtering: reactions carry top_msg_id (the topic id)
                # rather than a full message, so use it directly.
                if self.config.should_skip_topic(chat_id, getattr(event, "top_msg_id", None)):
                    return

                observed = extract_reactions(getattr(event, "reactions", None))
                if observed is None:
                    # Extraction failed (unexpected shape) — skip rather than buffer
                    # an empty snapshot that would tombstone valid reactions.
                    return
                self.stats["reactions_received"] += 1
                # Latest full snapshot wins; the timed flush reconciles + broadcasts.
                self._reaction_pending[(chat_id, event.msg_id)] = observed
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Error in reaction handler: {type(e).__name__}")

        # Paired with _remove_handlers() in stop(); keep both lists in step.
        self._registered_handlers = [
            on_message_edited,
            on_message_deleted,
            on_new_message,
            on_chat_action,
            on_pinned_messages,
            on_message_reactions,
        ]

    def _remove_handlers(self) -> None:
        """Detach this listener's handlers from the (possibly shared) client."""
        if not self.client:
            self._registered_handlers = []
            return
        for callback in self._registered_handlers:
            try:
                self.client.remove_event_handler(callback)
            except Exception as e:
                logger.debug(f"Could not remove event handler: {type(e).__name__}")
        self._registered_handlers = []

    async def run(self) -> None:
        """
        Run the listener until stopped.

        Operations are applied immediately with rate limiting:
        - Normal usage (few deletions) → applied instantly
        - Mass operations → blocked after threshold
        """
        self._running = True
        self.stats["start_time"] = datetime.now()

        # Start the rate limiter
        self._protector.start()

        # Start the reaction debounce flusher (#219) when reactions are enabled.
        if self.config.listen_reactions:
            self._reaction_flush_task = asyncio.create_task(self._reaction_flush_loop())

        # Write listener status to database (for viewer to display)
        try:
            await self.db.set_metadata(
                account_metadata_key("listener_active_since", self.account_id), datetime.now().isoformat()
            )
        except Exception as e:
            logger.warning(f"Could not write listener status to DB: {e}")

        logger.info("=" * 70)
        logger.info("🎧 Real-time listener started with RATE LIMITING")
        logger.info(f"   Max {self._protector.threshold} ops per {self._protector.window_seconds}s per chat")
        logger.info("   Normal usage works instantly, mass operations blocked")
        logger.info("=" * 70)

        try:
            # Keep running until disconnected or stopped
            await self.client.run_until_disconnected()
        except asyncio.CancelledError:
            logger.info("Listener cancelled")
        finally:
            self._running = False
            # Clear listener status when stopped
            try:
                await self.db.set_metadata(account_metadata_key("listener_active_since", self.account_id), "")
            except Exception:
                pass

            # Stop the reaction flusher and drain any buffered snapshots so a
            # reaction observed just before shutdown is not lost (#219).
            if self._reaction_flush_task:
                self._reaction_flush_task.cancel()
                try:
                    await self._reaction_flush_task
                except asyncio.CancelledError:
                    pass
            try:
                await self._flush_reactions()
            except Exception as e:
                logger.debug(f"Final reaction flush failed: {type(e).__name__}")

            # Cancel in-flight event webhooks: fire-and-forget semantics mean
            # shutdown never waits out delivery retries (#336).
            try:
                await self._event_webhook.aclose()
            except Exception as e:
                logger.debug(f"Event webhook close failed: {type(e).__name__}")

            # Stop the protector
            await self._protector.stop()

            await self._log_stats()

    async def stop(self) -> None:
        """
        Stop the listener gracefully.

        Only disconnects if we own the client (created it ourselves).
        Shared clients are managed by the connection owner.

        Note: Telethon has a known issue (LonamiWebs/Telethon#782) where internal
        tasks may not be cancelled cleanly, causing asyncio warnings. These are
        harmless and don't affect functionality.
        """
        logger.info("Stopping listener...")
        self._running = False

        # Detach handlers first: a shared client outlives this listener, and a
        # stopped listener must stop receiving events.
        self._remove_handlers()

        # Only disconnect if we own the client
        if self.client and self._owns_client and self.client.is_connected():
            try:
                await self.client.disconnect()
                # Small delay to allow internal task cleanup
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"Listener disconnect cleanup: {e}")

        await self._log_stats()
        logger.info("Listener stopped")

    async def _log_stats(self) -> None:
        """Log listener and protection statistics."""
        if self.stats["start_time"]:
            uptime = datetime.now() - self.stats["start_time"]
            protector_stats = self._protector.get_stats()

            logger.info("=" * 70)
            logger.info("📊 Listener Statistics")
            logger.info(f"   Uptime: {uptime}")
            logger.info("")
            logger.info("   📝 Edits:")
            logger.info(f"      Received: {self.stats['edits_received']}")
            logger.info(f"      Applied:  {self.stats['edits_applied']}")
            logger.info(f"      Skipped:  {self.stats['edits_skipped']}")
            logger.info("")
            logger.info("   🗑️ Deletions:")
            logger.info(f"      Received: {self.stats['deletions_received']}")
            logger.info(f"      Applied:  {self.stats['deletions_applied']}")
            if self.stats["deletions_skipped"]:
                logger.info(f"      Skipped (LISTEN_DELETIONS=false): {self.stats['deletions_skipped']}")
            logger.info("")
            logger.info("   📩 New Messages:")
            logger.info(f"      Received: {self.stats['new_messages_received']}")
            logger.info(f"      Saved:    {self.stats['new_messages_saved']}")
            logger.info("")
            logger.info("   🛡️ Protection:")
            logger.info(f"      Rate limits triggered: {protector_stats['rate_limits_triggered']}")
            logger.info(f"      Operations blocked: {protector_stats['operations_blocked']}")
            logger.info(f"      Chats rate-limited: {protector_stats['chats_rate_limited']}")

            if self.stats["errors"]:
                logger.warning(f"   ⚠️ Errors: {self.stats['errors']}")

            # Show currently blocked chats
            blocked = self._protector.get_blocked_chats()
            if blocked:
                logger.warning("")
                logger.warning(f"   🚫 Currently blocked chats: {len(blocked)}")
                for _chat_id, (reason, discarded) in blocked.items():
                    logger.warning(f"      {discarded} ops discarded - {reason}")

            logger.info("=" * 70)

    async def close(self) -> None:
        """Clean up resources."""
        await self.stop()
        if self.db:
            await self.db.close()


async def run_listener(config: Config) -> None:
    """
    Run the real-time listener as a standalone process — one listener per
    configured account, each registered on its own client.

    Args:
        config: Configuration object
    """

    def account_row_resolver(account: AccountConfig):
        # Same contract as telegram_backup's resolver: the accounts row is
        # keyed on the Telegram user id, so it can only be resolved once this
        # account's own client is authorized; connect() awaits this before any
        # event handler is registered.
        async def resolve(client: TelegramClient, db: DatabaseAdapter) -> int:
            me = await client.get_me()
            return await db.ensure_account(telegram_user_id=me.id, env_index=account.index, label=account.label)

        return resolve

    listeners: list[TelegramListener] = []
    connected: list[TelegramListener] = []
    failed = 0
    try:
        for account in config.accounts:
            try:
                listener = await TelegramListener.create(
                    config, account=account, account_resolver=account_row_resolver(account)
                )
                listeners.append(listener)
                await listener.connect()
                connected.append(listener)
            except Exception as e:
                # One broken account must not silence the other accounts'
                # listeners — but with a single account there is nothing to
                # shield, so keep the pre-8.0 behavior of letting the failure
                # propagate to main().
                if len(config.accounts) == 1:
                    raise
                # Type name only: exception text can carry the phone (#272).
                failed += 1
                logger.error(f"account {account.index} failed: {type(e).__name__}")
        if failed and not connected:
            raise RuntimeError(f"all {failed} configured accounts failed to start a listener")
        if len(connected) == 1:
            await connected[0].run()
        elif connected:
            await asyncio.gather(*(listener.run() for listener in connected))
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        for listener in listeners:
            await listener.close()


async def main() -> None:
    """Main entry point for standalone listener mode."""
    from .config import Config, setup_logging

    try:
        config = Config()
        setup_logging(config)

        logger.info("=" * 60)
        logger.info("Telegram Archive - Real-time Listener")
        logger.info("=" * 60)
        logger.info("This mode catches message edits and deletions in real-time")
        logger.info("Run alongside the backup scheduler for complete coverage")
        logger.info("=" * 60)

        await run_listener(config)

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
