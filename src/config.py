"""
Configuration management for Telegram Backup Automation.
Loads and validates settings from environment variables.
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

logger = logging.getLogger(__name__)


def _parse_bool(value: str | None, default: bool = False, *, name: str | None = None) -> bool:
    """Parse a boolean-like environment variable value."""
    if value is None or value == "":
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    label = f" for {name}" if name else ""
    raise ValueError(f"Invalid boolean value{label}: {value}")


def _parse_bool_env(name: str, default: bool) -> bool:
    """One boolean vocabulary for EVERY flag: 1/true/yes/on and 0/false/no/off.

    The alternative this replaces — ``.lower() == "true"`` — silently read
    DOWNLOAD_MEDIA=1 or ENABLE_LISTENER=yes as False, degrading capture with
    nothing in the log; and a typo now names the variable instead of making
    the user guess which of the flags carried it.
    """
    return _parse_bool(os.getenv(name), default, name=name)


def build_telegram_proxy_from_env() -> dict | None:
    """Build Telethon proxy configuration from environment variables."""
    proxy_type = os.getenv("TELEGRAM_PROXY_TYPE", "").strip().lower()
    proxy_addr = os.getenv("TELEGRAM_PROXY_ADDR", "").strip()
    proxy_port = os.getenv("TELEGRAM_PROXY_PORT", "").strip()
    proxy_username = os.getenv("TELEGRAM_PROXY_USERNAME", "").strip()
    proxy_password = os.getenv("TELEGRAM_PROXY_PASSWORD", "").strip()
    proxy_rdns = os.getenv("TELEGRAM_PROXY_RDNS", "").strip()

    # ``rdns`` is a modifier of an already-requested proxy, never an enabler on
    # its own — so it is intentionally excluded from this presence gate. The
    # stock docker-compose injects ``TELEGRAM_PROXY_RDNS=false`` by default; if
    # it were part of the gate, that literal "false" string (truthy in Python)
    # would make every default install think a proxy was half-configured and
    # raise the "incomplete proxy configuration" error (issue #193).
    has_proxy_config = any([proxy_type, proxy_addr, proxy_port, proxy_username, proxy_password])
    if not has_proxy_config:
        return None

    missing_fields = []
    if not proxy_type:
        missing_fields.append("TELEGRAM_PROXY_TYPE")
    if not proxy_addr:
        missing_fields.append("TELEGRAM_PROXY_ADDR")
    if not proxy_port:
        missing_fields.append("TELEGRAM_PROXY_PORT")
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise ValueError(f"Telegram proxy configuration is incomplete. Missing required settings: {missing}")

    if proxy_type != "socks5":
        raise ValueError("TELEGRAM_PROXY_TYPE must be 'socks5'")

    try:
        parsed_port = int(proxy_port)
    except ValueError as e:
        raise ValueError(f"TELEGRAM_PROXY_PORT must be a valid integer: {e}") from e

    if not 1 <= parsed_port <= 65535:
        raise ValueError(f"TELEGRAM_PROXY_PORT must be between 1 and 65535, got {parsed_port}")

    try:
        parsed_rdns = _parse_bool(proxy_rdns, default=False, name="TELEGRAM_PROXY_RDNS")
    except ValueError as e:
        raise ValueError(f"TELEGRAM_PROXY_RDNS must be a boolean value: {e}") from e

    if bool(proxy_username) != bool(proxy_password):
        raise ValueError(
            "TELEGRAM_PROXY_USERNAME and TELEGRAM_PROXY_PASSWORD must both be set together for SOCKS5 auth"
        )

    proxy = {
        "proxy_type": proxy_type,
        "addr": proxy_addr,
        "port": parsed_port,
        "rdns": parsed_rdns,
    }
    if proxy_username:
        proxy["username"] = proxy_username
    if proxy_password:
        proxy["password"] = proxy_password

    return proxy


def build_telegram_client_kwargs() -> dict:
    """Build common Telethon client keyword arguments from environment configuration.

    ``flood_sleep_threshold`` stays 0: media downloads and get_dialogs() calls
    temporarily raise the client threshold via ``absorb_media_floods`` (#232
    for media / MEDIA_FLOOD_SLEEP_THRESHOLD, #295 for dialogs /
    DIALOG_FLOOD_SLEEP_THRESHOLD); everything else keeps 0 so floods stay
    visible in app logs (#124).
    """
    kwargs: dict = {"flood_sleep_threshold": 0}
    proxy = build_telegram_proxy_from_env()
    if proxy is not None:
        kwargs["proxy"] = dict(proxy)
    return kwargs


# The full shape of one indexed account variable (v8.0.0 multi-account).
# Parsing scans the environment for the TG_ACCOUNT_ prefix and then requires
# this exact shape, so a typo'd suffix or index (TG_ACCOUNT_2_APIHASH,
# TG_ACCOUNT_02_API_ID) is a loud startup error instead of a credential
# silently not applying.
_TG_ACCOUNT_CREDENTIAL_SUFFIXES = ("API_ID", "API_HASH", "PHONE_NUMBER", "LABEL", "SESSION_NAME")

# Per-account capture-filter overrides (8.1, #313): the indexed variable wins
# for that account, the global one is the fallback. The indexed include/exclude
# names drop the GLOBAL_ prefix (TG_ACCOUNT_2_INCLUDE_CHAT_IDS overrides
# GLOBAL_INCLUDE_CHAT_IDS / legacy INCLUDE_CHAT_IDS).
_TG_ACCOUNT_FILTER_SUFFIXES = (
    "CHAT_IDS",
    "CHAT_TYPES",
    "INCLUDE_CHAT_IDS",
    "EXCLUDE_CHAT_IDS",
    "PRIVATE_INCLUDE_CHAT_IDS",
    "PRIVATE_EXCLUDE_CHAT_IDS",
    "GROUPS_INCLUDE_CHAT_IDS",
    "GROUPS_EXCLUDE_CHAT_IDS",
    "CHANNELS_INCLUDE_CHAT_IDS",
    "CHANNELS_EXCLUDE_CHAT_IDS",
    "PRIORITY_CHAT_IDS",
    "SKIP_MEDIA_CHAT_IDS",
)

_TG_ACCOUNT_ENV_RE = re.compile(
    "^TG_ACCOUNT_([1-9]\\d*)_(" + "|".join(_TG_ACCOUNT_CREDENTIAL_SUFFIXES + _TG_ACCOUNT_FILTER_SUFFIXES) + ")$"
)

# The suffixes every account must declare; LABEL and SESSION_NAME are optional.
_TG_ACCOUNT_REQUIRED_SUFFIXES = ("API_ID", "API_HASH", "PHONE_NUMBER")

# Valid CHAT_TYPES tokens, shared by the global and per-account validators.
_VALID_CHAT_TYPES = {"private", "groups", "channels", "bots"}


@dataclass(frozen=True)
class AccountConfig:
    """One Telegram account the archiver captures with (v8.0.0 multi-account).

    ``index`` is the 1-based TG_ACCOUNT_<N>_* position — an env-file coordinate
    only. The database row an account's data lives under is resolved from the
    Telegram user id after login, never from this number, so re-ordering the
    indexes never moves data between accounts.

    ``api_id``/``api_hash``/``phone``/``label`` are kept out of the dataclass
    repr: reprs travel into logs and exception text, the phone number is PII
    and the hash is a credential — accounts appear in logs by index or database
    row id only. The credential fields are Optional solely for the synthesized
    zero-config account, which the viewer constructs without credentials;
    indexed accounts always carry a complete, validated triple.
    """

    index: int
    api_id: int | None = field(repr=False)
    api_hash: str | None = field(repr=False)
    phone: str | None = field(repr=False)
    label: str = field(repr=False)
    session_name: str
    session_path: str


@dataclass(frozen=True)
class AccountFilters:
    """The effective capture-filter set one account sweeps and listens with (8.1, #313).

    Resolved once at startup from the global filter variables plus any
    TG_ACCOUNT_<N>_<FILTER> overrides: the indexed variable wins for that
    account, the global one is the fallback, so a single-account install with
    no overrides behaves byte-identically to 8.0. An EMPTY indexed value
    inherits (docker-compose's ${VAR:-} idiom injects empty strings, and
    silently clearing a whitelist would widen capture); the literal token
    ``none`` is the explicit-empty override.

    The decision methods mirror Config.should_backup_chat/_type exactly —
    tests assert the two stay equivalent for an override-free account.
    """

    chat_ids: frozenset
    chat_types: tuple
    global_include_ids: frozenset
    global_exclude_ids: frozenset
    private_include_ids: frozenset
    private_exclude_ids: frozenset
    groups_include_ids: frozenset
    groups_exclude_ids: frozenset
    channels_include_ids: frozenset
    channels_exclude_ids: frozenset
    priority_chat_ids: frozenset
    skip_media_chat_ids: frozenset

    @property
    def whitelist_mode(self) -> bool:
        """CHAT_IDS takes absolute priority when non-empty, per-account."""
        return len(self.chat_ids) > 0

    def should_backup_chat_type(self, is_user: bool, is_group: bool, is_channel: bool, is_bot: bool = False) -> bool:
        """Type filter for this account; mirrors Config.should_backup_chat_type."""
        if is_bot and "bots" in self.chat_types:
            return True
        if is_user and "private" in self.chat_types:
            return True
        if is_group and "groups" in self.chat_types:
            return True
        if is_channel and "channels" in self.chat_types:
            return True
        return False

    def should_backup_chat(
        self, chat_id: int, is_user: bool, is_group: bool, is_channel: bool, is_bot: bool = False
    ) -> bool:
        """Two-mode capture decision for this account; mirrors Config.should_backup_chat."""
        if self.whitelist_mode:
            return chat_id in self.chat_ids

        if chat_id in self.global_exclude_ids:
            return False

        if (is_user or is_bot) and chat_id in self.private_exclude_ids:
            return False
        if is_group and chat_id in self.groups_exclude_ids:
            return False
        if is_channel and chat_id in self.channels_exclude_ids:
            return False

        if self.global_include_ids:
            return chat_id in self.global_include_ids

        if (is_user or is_bot) and self.private_include_ids:
            return chat_id in self.private_include_ids
        if is_group and self.groups_include_ids:
            return chat_id in self.groups_include_ids
        if is_channel and self.channels_include_ids:
            return chat_id in self.channels_include_ids

        return self.should_backup_chat_type(is_user, is_group, is_channel, is_bot)


class AccountScopedConfig:
    """The config view one account's capture workers hold (8.1, #313).

    Everything delegates to the shared Config except the capture-filter
    surface — the twelve filter attributes plus their decision methods —
    which is overlaid from one account's resolved AccountFilters. Workers
    keep reading ``config.chat_ids`` / ``config.should_backup_chat`` exactly
    as they always have; the per-account semantics live here instead of at
    every consumer call site. Constructed by ``Config.for_account``.
    """

    def __init__(self, base: Config, account_index: int, filters: AccountFilters):
        self._base = base
        self.account_index = account_index
        self.filters = filters
        # Same concrete types the global attributes carry (sets / list), so
        # set algebra and truthiness behave identically downstream.
        self.chat_ids = set(filters.chat_ids)
        self.whitelist_mode = filters.whitelist_mode
        self.chat_types = list(filters.chat_types)
        self.global_include_ids = set(filters.global_include_ids)
        self.global_exclude_ids = set(filters.global_exclude_ids)
        self.private_include_ids = set(filters.private_include_ids)
        self.private_exclude_ids = set(filters.private_exclude_ids)
        self.groups_include_ids = set(filters.groups_include_ids)
        self.groups_exclude_ids = set(filters.groups_exclude_ids)
        self.channels_include_ids = set(filters.channels_include_ids)
        self.channels_exclude_ids = set(filters.channels_exclude_ids)
        self.priority_chat_ids = set(filters.priority_chat_ids)
        self.skip_media_chat_ids = set(filters.skip_media_chat_ids)

    def __getattr__(self, name: str):
        # Only reached for names not set in __init__: everything that is not
        # a capture filter resolves on the shared Config, methods included.
        try:
            base = object.__getattribute__(self, "_base")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(base, name)

    def should_backup_chat_type(self, is_user: bool, is_group: bool, is_channel: bool, is_bot: bool = False) -> bool:
        return self.filters.should_backup_chat_type(is_user, is_group, is_channel, is_bot)

    def should_backup_chat(
        self, chat_id: int, is_user: bool, is_group: bool, is_channel: bool, is_bot: bool = False
    ) -> bool:
        return self.filters.should_backup_chat(chat_id, is_user, is_group, is_channel, is_bot)

    def should_download_media_for_chat(self, chat_id: int) -> bool:
        """Mirrors Config.should_download_media_for_chat against this account's list."""
        if not self._base.download_media:
            return False
        return chat_id not in self.filters.skip_media_chat_ids


class Config:
    """Configuration settings loaded from environment variables."""

    def __init__(self):
        """Initialize configuration from environment variables."""
        # Telegram API credentials (optional for viewer, required for backup)
        self.api_id = int(os.getenv("TELEGRAM_API_ID")) if os.getenv("TELEGRAM_API_ID") else None
        self.api_hash = os.getenv("TELEGRAM_API_HASH")
        self.phone = os.getenv("TELEGRAM_PHONE")

        # Backup schedule (cron format)
        self.schedule = os.getenv("SCHEDULE", "0 */6 * * *")

        # Backup options
        self.backup_path = os.path.abspath(os.getenv("BACKUP_PATH", "/data/backups"))
        self.download_media = _parse_bool_env("DOWNLOAD_MEDIA", True)
        self.max_media_size_mb = int(os.getenv("MAX_MEDIA_SIZE_MB", "100"))
        # Timeout for media downloads (seconds). 0 disables the timeout.
        self.download_timeout_seconds = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "3600"))
        # Absorb short mid-download FloodWaits (up to this many seconds) so the
        # chunk stream resumes in place instead of restarting from byte 0;
        # 0 = pre-7.27 raise-immediately behavior (#232).
        self.media_flood_sleep_threshold = int(os.getenv("MEDIA_FLOOD_SLEEP_THRESHOLD", "60"))
        # Absorb short FloodWaits (up to this many seconds) during get_dialogs()'s
        # internal pagination so Telethon sleeps and resumes the SAME page instead
        # of the whole call aborting and call_with_flood_retry restarting from
        # page 1. Without this, an account with enough dialogs to reliably trip a
        # page's FloodWait can never finish an initial (non-whitelist) backup: the
        # restart re-walks the same successful early pages and re-trips the same
        # later page every time, regardless of retry count or schedule spacing.
        # 0 = raise-immediately behavior (#295).
        self.dialog_flood_sleep_threshold = int(os.getenv("DIALOG_FLOOD_SLEEP_THRESHOLD", "60"))
        # Max usable filename-component length in BYTES for the media store. Default 143
        # keeps names writable on Synology/eCryptfs encrypted shares (whose filename-
        # encryption overhead caps components at ~143 bytes, not the usual 255); the temp
        # ``.part`` suffix is reserved on top of this. Raise to 255 on plain ext4/xfs/btrfs
        # if you prefer longer decorative names. Only the decorative part is shortened; the
        # file_id prefix (uniqueness) and the extension are always preserved. (#212)
        self.max_filename_bytes = int(os.getenv("MEDIA_MAX_FILENAME_BYTES", "143"))
        # Stop re-fetching a media file that keeps failing to download after this many
        # attempts, so a permanently-unwritable file (e.g. an over-limit name on an
        # exotic filesystem, or a revoked file reference) can't tax every backup run
        # forever. (#212)
        self.max_media_download_attempts = int(os.getenv("MEDIA_MAX_DOWNLOAD_ATTEMPTS", "5"))

        # =====================================================================
        # PARALLEL CHUNKED DOWNLOADS (issue #183)
        # =====================================================================
        # Split a single large file into chunks fetched concurrently over
        # several MTProto senders to one DC, then reassemble by exact offset.
        # Default OFF: opening N senders looks aggressive to Telegram and
        # multiplies FloodWait exposure, so this is opt-in and only kicks in
        # above a size threshold. Small files and photos stay single-stream.
        self.parallel_download_enabled = _parse_bool_env("PARALLEL_DOWNLOAD_ENABLED", False)
        # Only files at/above this size use the parallel path (smaller files
        # gain nothing and pay pure overhead). Clamped to a sane floor.
        self.parallel_download_min_size_mb = max(1, int(os.getenv("PARALLEL_DOWNLOAD_MIN_SIZE_MB", "20")))
        # Concurrent senders per file. Hard-capped well under Telegram's ~20
        # connection cliff to stay safe for an unattended, scheduled tool.
        self.parallel_download_connections = max(2, min(8, int(os.getenv("PARALLEL_DOWNLOAD_CONNECTIONS", "4"))))
        # Per-request chunk size in KiB. Must be a 4 KiB multiple that divides
        # 1 MiB and is <= 512 KiB (Telegram getFile constraints); invalid values
        # fall back to the 512 KiB maximum. Peak memory ~= connections * part.
        self.parallel_download_part_size_kb = self._parse_part_size_kb(
            os.getenv("PARALLEL_DOWNLOAD_PART_SIZE_KB", "512")
        )

        # Batch processing configuration
        self.batch_size = int(os.getenv("BATCH_SIZE", "100"))
        # How often to checkpoint sync progress (every N batch inserts)
        # Lower = better crash recovery, higher = fewer DB writes
        self.checkpoint_interval = max(1, int(os.getenv("CHECKPOINT_INTERVAL", "1")))

        # Database Configuration
        # Timeout for SQLite operations (seconds).
        # Increase this if you experience "database is locked" errors (e.g., on Unraid/slow disks).
        # Default increased to 60s for better resilience with concurrent access (backup + web viewer).
        self.database_timeout = float(os.getenv("DATABASE_TIMEOUT", "60.0"))

        # =====================================================================
        # CHAT FILTERING - Two Modes
        # =====================================================================
        #
        # MODE 1: Whitelist Mode (simple) - set CHAT_IDS
        #   CHAT_IDS=-100id1,-100id2   → Backup ONLY these specific chats
        #   When set, CHAT_TYPES and all INCLUDE/EXCLUDE filters are IGNORED
        #
        # MODE 2: Type-based Mode (default) - use CHAT_TYPES + INCLUDE/EXCLUDE
        #   CHAT_TYPES=private,groups,bots  → Backup all chats of these types
        #   *_INCLUDE_CHAT_IDS         → ALSO include these (additive)
        #   *_EXCLUDE_CHAT_IDS         → Exclude these (takes priority)
        #
        # =====================================================================

        # Whitelist mode: CHAT_IDS takes absolute priority
        # When set, ONLY these chats are backed up - nothing else
        self.chat_ids = self._parse_id_list(os.getenv("CHAT_IDS", ""))
        self.whitelist_mode = len(self.chat_ids) > 0
        # Bounded dialog scan warming the session entity cache when a whitelisted
        # id cannot be resolved (typically a cache-cold DM); 0 disables (#234).
        self.whitelist_resolve_dialog_limit = int(os.getenv("WHITELIST_RESOLVE_DIALOG_LIMIT", "1000"))

        # Type-based mode (only used if CHAT_IDS is not set)
        chat_types_env = os.environ.get("CHAT_TYPES")
        if chat_types_env is None:
            # Not set at all, use default (backup all types)
            chat_types_str = "private,groups,channels"
        else:
            # Explicitly set (even if empty string)
            chat_types_str = chat_types_env
        self.chat_types = [ct.strip().lower() for ct in chat_types_str.split(",") if ct.strip()]
        self._validate_chat_types()

        # Granular chat ID filters (only used in type-based mode)
        # Global filters (backward compatibility with old names)
        self.global_include_ids = self._parse_id_list(
            os.getenv("GLOBAL_INCLUDE_CHAT_IDS") or os.getenv("INCLUDE_CHAT_IDS", "")
        )
        self.global_exclude_ids = self._parse_id_list(
            os.getenv("GLOBAL_EXCLUDE_CHAT_IDS") or os.getenv("EXCLUDE_CHAT_IDS", "")
        )

        # Per-type filters
        self.private_include_ids = self._parse_id_list(os.getenv("PRIVATE_INCLUDE_CHAT_IDS", ""))
        self.private_exclude_ids = self._parse_id_list(os.getenv("PRIVATE_EXCLUDE_CHAT_IDS", ""))

        self.groups_include_ids = self._parse_id_list(os.getenv("GROUPS_INCLUDE_CHAT_IDS", ""))
        self.groups_exclude_ids = self._parse_id_list(os.getenv("GROUPS_EXCLUDE_CHAT_IDS", ""))

        self.channels_include_ids = self._parse_id_list(os.getenv("CHANNELS_INCLUDE_CHAT_IDS", ""))
        self.channels_exclude_ids = self._parse_id_list(os.getenv("CHANNELS_EXCLUDE_CHAT_IDS", ""))

        # Priority chats - these are processed FIRST in all backup/sync operations
        # Useful for ensuring important chats are always backed up first
        self.priority_chat_ids = self._parse_id_list(os.getenv("PRIORITY_CHAT_IDS", ""))

        # Skip media downloads for specific chats (but still backup message text)
        self.skip_media_chat_ids = self._parse_id_list(os.getenv("SKIP_MEDIA_CHAT_IDS", ""))
        # Delete existing media files and records for chats in skip list (reclaim storage)
        self.skip_media_delete_existing = _parse_bool_env("SKIP_MEDIA_DELETE_EXISTING", True)

        # Skip specific topics inside forum supergroups
        # Format: SKIP_TOPIC_IDS=-1001234567890:42,-1001234567890:1337
        # Each entry is chat_id:topic_id — skips that topic but keeps the rest of the chat
        self.skip_topic_ids = self._parse_topic_skip_list(os.getenv("SKIP_TOPIC_IDS", ""))

        # Session configuration
        self.session_name = os.getenv("SESSION_NAME", "telegram_backup")
        self.telegram_proxy = build_telegram_proxy_from_env()

        # Logging
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        # Handle common alias: WARN -> WARNING (Python uses WARNING, not WARN)
        if log_level == "WARN":
            log_level = "WARNING"
        self.log_level = getattr(logging, log_level, logging.INFO)

        # Derived paths
        # Store session in a separate directory from backups
        # If BACKUP_PATH is /data/backups, session goes to /data/session
        backup_parent = os.path.dirname(self.backup_path.rstrip("/\\"))
        self.session_dir = os.path.abspath(os.getenv("SESSION_DIR", os.path.join(backup_parent, "session")))
        self.session_path = os.path.join(self.session_dir, self.session_name)

        # Multi-account declaration (v8.0.0). Parsed after the legacy session
        # settings because account 1 inherits their resolution: with no
        # TG_ACCOUNT_* variable set, exactly one account is synthesized from
        # the legacy TELEGRAM_* variables with the same session file, so a 7.x
        # deployment upgrades with zero env changes and zero re-login. When
        # TG_ACCOUNT_* variables are present, they win over the legacy triple.
        self.accounts, self._indexed_accounts = self._parse_accounts()
        self.account_filters = self._resolve_account_filters()

        # Database path configuration
        # Default: inside backup_path
        # Can be overridden by DATABASE_PATH (full path) or DATABASE_DIR (directory)
        db_path_env = os.getenv("DATABASE_PATH")
        db_dir_env = os.getenv("DATABASE_DIR")

        if db_path_env:
            self.database_path = os.path.abspath(db_path_env)
        elif db_dir_env:
            self.database_path = os.path.abspath(os.path.join(db_dir_env, "telegram_backup.db"))
        else:
            db_path_v3 = os.getenv("DB_PATH")
            if db_path_v3:
                self.database_path = os.path.abspath(db_path_v3)
            else:
                self.database_path = os.path.join(self.backup_path, "telegram_backup.db")

        self.media_path = os.path.join(self.backup_path, "media")

        # Ensure directories exist
        self._ensure_directories()

        # Sync options for exact Telegram mirroring (WARNING: expensive operation)
        # When enabled, checks all backed up messages for deletions/edits on Telegram
        self.sync_deletions_edits = _parse_bool_env("SYNC_DELETIONS_EDITS", False)

        # Media verification mode
        # When enabled, checks all media files on disk and re-downloads missing/corrupted ones
        # Useful for recovering from interrupted backups or deleted media files
        self.verify_media = _parse_bool_env("VERIFY_MEDIA", False)

        # Gap-fill mode: detect and recover skipped messages
        # When enabled, runs after each scheduled backup to find and fill gaps
        # in message ID sequences caused by API errors or interruptions
        self.fill_gaps = _parse_bool_env("FILL_GAPS", False)
        self.gap_threshold = int(os.getenv("GAP_THRESHOLD", "50"))

        # Real-time listener mode
        # When enabled, runs a background listener that catches message edits and deletions
        # in real-time instead of batch-checking on each backup run
        self.enable_listener = _parse_bool_env("ENABLE_LISTENER", False)

        # Listener granular controls (only apply when ENABLE_LISTENER=true)
        # LISTEN_EDITS: Apply text edits to backed up messages (safe, just updates text)
        self.listen_edits = _parse_bool_env("LISTEN_EDITS", True)

        # LISTEN_DELETIONS: Handle deletion events from Telegram.
        # DEFAULT FALSE preserves archive data by ignoring deletion events.
        self.listen_deletions = _parse_bool_env("LISTEN_DELETIONS", False)
        # DELETION_MODE controls what happens when deletion handling is enabled:
        # - hard: legacy mirror behavior; remove archived message records
        # - soft: keep archived messages and mark them deleted
        self.deletion_mode = os.getenv("DELETION_MODE", "hard").strip().lower()
        if self.deletion_mode not in {"hard", "soft"}:
            raise ValueError("DELETION_MODE must be either 'hard' or 'soft'")

        # LISTEN_NEW_MESSAGES: Save new messages to backup in real-time
        # When enabled, new messages are saved immediately instead of waiting for scheduled backup
        # This provides true real-time backup but may increase API usage
        self.listen_new_messages = _parse_bool_env("LISTEN_NEW_MESSAGES", True)

        # LISTEN_NEW_MESSAGES_MEDIA: Also download media in real-time (not just text)
        # When disabled (default), media is marked for download on next scheduled backup
        # When enabled, media is downloaded immediately - more API usage but instant availability
        self.listen_new_messages_media = _parse_bool_env("LISTEN_NEW_MESSAGES_MEDIA", False)

        # LISTEN_CHAT_ACTIONS: Track chat photo changes, member joins/leaves, title changes
        # When enabled, updates to chat metadata are captured in real-time
        self.listen_chat_actions = _parse_bool_env("LISTEN_CHAT_ACTIONS", True)

        # LISTEN_REACTIONS: Capture message reactions in real-time (#219).
        # DEFAULT FALSE (opt-in): reaction push is best-effort on a user client
        # (Telegram gives no gap recovery and recommends polling), it is storm-prone
        # on popular messages, and it captures aggregate per-emoji counts only.
        # When off, reactions are still reconciled by the scheduled backup sweep.
        self.listen_reactions = _parse_bool_env("LISTEN_REACTIONS", False)

        # REACTION_DEBOUNCE_SECONDS: coalesce a burst of reaction updates for the same
        # message into one reconcile/broadcast. Each update carries the full current
        # snapshot, so keeping only the latest within the window is loss-free.
        self.reaction_debounce_seconds = max(0.1, float(os.getenv("REACTION_DEBOUNCE_SECONDS", "1.5")))

        # REACTION_RESWEEP_DAYS: bounded reaction re-sweep window (#221). Telegram does
        # not reliably push reaction updates for the archive account's OWN reactions
        # (made from another device) to this session, and the scheduled sweep only
        # revisits messages inside its incremental window. When >0, each scheduled
        # sweep re-checks the last N days of messages per chat and reconciles their
        # current aggregate. DEFAULT 0 (disabled) — opt-in, small extra API cost.
        self.reaction_resweep_days: float = max(0.0, float(os.getenv("REACTION_RESWEEP_DAYS", "0")))

        # REACTION_RESWEEP_MAX_PER_CHAT: cap on messages re-checked per chat per sweep
        # (fetched newest-first in batches of 100 → ceil(cap/100) requests/chat/sweep).
        self.reaction_resweep_max_per_chat: int = max(1, int(os.getenv("REACTION_RESWEEP_MAX_PER_CHAT", "500")))

        # REACTION_RESWEEP_BATCH_DELAY_SECONDS: minimum spacing between the re-sweep's
        # batched API requests, measured ACROSS chats (#224 — getMessagesReactions has
        # a burst-rate flood limit accumulated per account+method, so per-chat caps
        # cannot express it). Smooths bursts; it is deliberately NOT sized to prevent
        # floods under sustained load (that would need ~15s/request) — the re-sweep
        # additionally defers the rest of a run on its first FloodWait and resumes
        # where it left off on the next scheduled sweep. 0 disables the spacing.
        self.reaction_resweep_batch_delay_seconds: float = max(
            0.0, float(os.getenv("REACTION_RESWEEP_BATCH_DELAY_SECONDS", "2"))
        )

        # =====================================================================
        # OUTBOUND EVENT WEBHOOK (issue #336)
        # =====================================================================
        # Fires an HTTP request when the real-time listener applies a message
        # edit or deletion. Validation rule: the webhook is a side-channel, so
        # a misconfigured EVENT_WEBHOOK_* value logs ONE warning naming the
        # variable and the expected format (never echoing the value — the URL
        # is a capability secret) and force-disables the webhook; the archiver
        # never aborts for it. The sole exception is the master bool, which
        # follows the repo-wide _parse_bool_env vocabulary (raises on garbage).
        self.event_webhook_enabled = _parse_bool_env("EVENT_WEBHOOK_ENABLED", False)
        self.event_webhook_url = os.getenv("EVENT_WEBHOOK_URL", "").strip()
        self.event_webhook_method = os.getenv("EVENT_WEBHOOK_METHOD", "POST").strip().upper()
        self.event_webhook_headers: dict[str, str] = {}
        self.event_webhook_events: set[str] = {"message_edited", "message_deleted"}
        self.event_webhook_chat_ids: set[int] = set()
        self.event_webhook_body_template = os.getenv("EVENT_WEBHOOK_BODY_TEMPLATE", "")
        if self.event_webhook_enabled:
            self._validate_event_webhook()

        # =====================================================================
        # GROUP → SUPERGROUP MIGRATION FOLLOWING (issue #228)
        # =====================================================================
        # When a basic group is upgraded to a supergroup it is assigned a brand
        # new channel id. Neither the live NewMessage handler nor the ChatAction
        # handler sees the migration service message (Telethon exposes it to
        # neither), so capture of the group silently stops at the old id. The
        # scheduled sweep therefore ALWAYS warns (count-only) when a tracked
        # group has migrated to a supergroup that is not in scope.
        # FOLLOW_CHAT_MIGRATIONS (default OFF) additionally makes the archiver
        # adopt the new supergroup id automatically: it is persisted to the
        # metadata KV and merged into the effective backup + listener scope so
        # capture continues seamlessly without editing GROUPS_INCLUDE_CHAT_IDS.
        self.follow_chat_migrations = _parse_bool_env("FOLLOW_CHAT_MIGRATIONS", False)

        # Note: LISTEN_ALBUMS removed - albums are automatically handled via grouped_id
        # in the NewMessage handler. The viewer groups messages by grouped_id.

        # =====================================================================
        # MEDIA DEDUPLICATION
        # =====================================================================
        # DEDUPLICATE_MEDIA: Use symlinks to avoid storing duplicate files
        # When enabled (default), files shared across multiple chats are stored once
        # in a _shared directory and symlinked from chat directories.
        # Saves significant disk space when same media is shared across chats.
        self.deduplicate_media = _parse_bool_env("DEDUPLICATE_MEDIA", True)

        # =====================================================================
        # MASS OPERATION PROTECTION (rate limiter)
        # =====================================================================
        # Deletions/edits are RATE LIMITED per chat: the first THRESHOLD
        # operations inside WINDOW are applied immediately (in hard deletion
        # mode, irreversibly), and only the overflow is blocked. Nothing is
        # buffered and nothing already applied is ever rolled back.
        #
        # THRESHOLD: Max operations applied per chat per window (default: 10)
        # WINDOW: Sliding window for counting operations (default: 30 seconds)
        #
        # Example: If >10 deletions arrive within 30s, the first 10 are
        # applied and the rest are blocked (counted, logged).
        self.mass_operation_threshold = int(os.getenv("MASS_OPERATION_THRESHOLD", "10"))
        self.mass_operation_window_seconds = int(os.getenv("MASS_OPERATION_WINDOW_SECONDS", "30"))
        # Non-positive values do not degrade — they invert the protection: a
        # zero/negative window prunes each operation before it is counted (the
        # limiter never fires again), and a non-positive threshold blocks every
        # operation after the first. This knob guards the archive against mass
        # deletion mirroring, so a typo fails loudly (DELETION_MODE convention)
        # instead of silently disarming it.
        if self.mass_operation_threshold < 1:
            raise ValueError("MASS_OPERATION_THRESHOLD must be >= 1")
        if self.mass_operation_window_seconds < 1:
            raise ValueError("MASS_OPERATION_WINDOW_SECONDS must be >= 1")
        # DEPRECATED: parsed for compatibility, consumed by nothing.
        self.mass_operation_buffer_delay = float(os.getenv("MASS_OPERATION_BUFFER_DELAY", "2.0"))

        # Display chat IDs - restrict viewer to specific chats only
        # Useful for sharing public channel viewers without exposing other chats
        self.display_chat_ids = self._parse_id_list(os.getenv("DISPLAY_CHAT_IDS", ""))

        # Timezone configuration for viewer display
        # Defaults to Europe/Madrid if not specified. Validated HERE: the
        # stats scheduler builds ZoneInfo(viewer_timezone) inside a retry
        # loop whose catch-all would otherwise log-and-sleep every hour,
        # forever, on a misspelled tz name (the request path already falls
        # back to UTC; the scheduler had no such defence).
        viewer_timezone = os.getenv("VIEWER_TIMEZONE", "Europe/Madrid")
        try:
            ZoneInfo(viewer_timezone)
        except ZoneInfoNotFoundError, ValueError, KeyError:
            logger.warning(f"VIEWER_TIMEZONE {viewer_timezone!r} is not a known timezone; falling back to UTC")
            viewer_timezone = "UTC"
        self.viewer_timezone = viewer_timezone

        # Viewer notifications (internal use, prefer PUSH_NOTIFICATIONS)
        self.enable_notifications = _parse_bool_env("ENABLE_NOTIFICATIONS", False)

        # Push notifications mode: 'off', 'basic', 'full'
        # - off: No notifications
        # - basic: In-browser notifications only (tab must be open)
        # - full: Web Push notifications (work even with browser closed, persistent subscriptions)
        push_mode = os.getenv("PUSH_NOTIFICATIONS", "basic").lower()
        self.push_notifications = push_mode if push_mode in ("off", "basic", "full") else "basic"

        # VAPID keys for Web Push (auto-generated if not provided)
        # Generate your own with: npx web-push generate-vapid-keys
        self.vapid_private_key = os.getenv("VAPID_PRIVATE_KEY", "")
        self.vapid_public_key = os.getenv("VAPID_PUBLIC_KEY", "")
        self.vapid_contact = os.getenv("VAPID_CONTACT", "mailto:admin@example.com")

        # Stats calculation schedule
        # Daily calculation of statistics (chat counts, message counts, etc.)
        # Default: 03:00 (3am) in the configured viewer timezone. Documented
        # as 0-23 and validated here for the same reason as the timezone:
        # now.replace(hour=24) raises inside the scheduler's hourly catch-all.
        stats_hour_raw = os.getenv("STATS_CALCULATION_HOUR", "3")
        try:
            stats_hour = int(stats_hour_raw)
        except ValueError:
            stats_hour = -1
        if not 0 <= stats_hour <= 23:
            logger.warning(f"STATS_CALCULATION_HOUR {stats_hour_raw!r} is not an hour in 0-23; using 3")
            stats_hour = 3
        self.stats_calculation_hour = stats_hour

        # Show stats in viewer UI
        # When disabled, hides the stats dropdown next to "Telegram Archive" title
        # Useful for restricted viewers where you don't want to expose total counts
        self.show_stats = _parse_bool_env("SHOW_STATS", True)

        logger.info("Configuration loaded successfully")
        logger.debug(f"Backup path: {self.backup_path}")
        logger.debug(f"Download media: {self.download_media}")

        # Indexed mode announces itself by count only; identifying values
        # (labels, phone numbers) stay out of the log. Zero-config deployments
        # deliberately log nothing new here — their output stays 7.x-identical.
        if self._indexed_accounts:
            logger.info(f"Multi-account: using {len(self.accounts)} configured account(s)")

        # Log filtering mode
        if self.whitelist_mode:
            logger.info(f"Filter mode: WHITELIST - backing up ONLY {len(self.chat_ids)} specific chats")
        else:
            logger.debug("Filter mode: TYPE-BASED")
            logger.debug(f"  Chat types: {self.chat_types}")
        logger.debug(f"Schedule: {self.schedule}")
        if self.sync_deletions_edits:
            logger.warning(
                "SYNC_DELETIONS_EDITS enabled - this will check ALL messages for deletions/edits (expensive!)"
            )
        if self.verify_media:
            logger.info("VERIFY_MEDIA enabled - will check for missing/corrupted media files and re-download them")
        if self.parallel_download_enabled:
            logger.info(
                "PARALLEL_DOWNLOAD enabled - files >=%dMB use %d senders, %dKB chunks (peak mem ~%dKB/file)",
                self.parallel_download_min_size_mb,
                self.parallel_download_connections,
                self.parallel_download_part_size_kb,
                self.parallel_download_connections * self.parallel_download_part_size_kb,
            )
        if self.enable_listener:
            logger.info("ENABLE_LISTENER enabled - will catch message edits/deletions in real-time")
            logger.info(f"  LISTEN_EDITS: {self.listen_edits}")
            if self.listen_deletions:
                if self.deletion_mode == "soft":
                    logger.warning("  LISTEN_DELETIONS: true, DELETION_MODE=soft - Messages will be marked deleted")
                else:
                    logger.warning(
                        "  ⚠️ LISTEN_DELETIONS: true, DELETION_MODE=hard - Messages will be DELETED from backup!"
                    )
            else:
                logger.info("  LISTEN_DELETIONS: false (backup protected)")
            if self.listen_new_messages:
                logger.info("  LISTEN_NEW_MESSAGES: true - New messages saved in real-time!")
            else:
                logger.info("  LISTEN_NEW_MESSAGES: false (messages saved on scheduled backup)")
            if self.listen_chat_actions:
                logger.info("  LISTEN_CHAT_ACTIONS: true - Chat metadata changes tracked!")
            logger.info(
                f"  Mass operation protection: block if >{self.mass_operation_threshold} ops in {self.mass_operation_window_seconds}s"
            )
        if self.event_webhook_enabled:
            # Never log the URL, at any level: Slack/Discord/ntfy URLs are
            # bearer capabilities (deliberate deviation from the proxy
            # DEBUG-endpoint precedent above).
            restriction = (
                f", restricted to {len(self.event_webhook_chat_ids)} chat(s)" if self.event_webhook_chat_ids else ""
            )
            logger.info(
                f"EVENT_WEBHOOK enabled - {self.event_webhook_method} on {', '.join(sorted(self.event_webhook_events))}{restriction}"
            )
            if not self.enable_listener:
                logger.warning("  EVENT_WEBHOOK_ENABLED has no effect: ENABLE_LISTENER=false")
            else:
                if "message_deleted" in self.event_webhook_events and not self.listen_deletions:
                    logger.warning("  message_deleted webhooks will never fire: LISTEN_DELETIONS=false")
                if "message_edited" in self.event_webhook_events and not self.listen_edits:
                    logger.warning("  message_edited webhooks will never fire: LISTEN_EDITS=false")
        if self.follow_chat_migrations:
            logger.info(
                "FOLLOW_CHAT_MIGRATIONS enabled - will adopt the new supergroup id after a group→supergroup migration"
            )
        if self.display_chat_ids:
            logger.info(f"Display mode: Viewer restricted to {len(self.display_chat_ids)} chat(s)")
        if self.skip_media_chat_ids:
            cleanup_status = "will delete existing media" if self.skip_media_delete_existing else "keeps existing media"
            logger.info(f"Media downloads skipped for {len(self.skip_media_chat_ids)} chat(s) ({cleanup_status})")
        if self.skip_topic_ids:
            total_topics = sum(len(t) for t in self.skip_topic_ids.values())
            logger.info(f"Topic filtering: skipping {total_topics} topic(s) across {len(self.skip_topic_ids)} chat(s)")
        if self.telegram_proxy:
            logger.info("Telegram proxy enabled (type=socks5, rdns=%s)", self.telegram_proxy["rdns"])
            logger.debug(
                "Telegram proxy endpoint: %s:%s",
                self.telegram_proxy["addr"],
                self.telegram_proxy["port"],
            )

    def _parse_part_size_kb(self, value: str | None) -> int:
        """Parse PARALLEL_DOWNLOAD_PART_SIZE_KB, clamping to a valid getFile size.

        Telegram requires the per-request limit to be a multiple of 4 KiB that
        divides 1 MiB and is at most 512 KiB. An invalid or out-of-range value
        is clamped to 512 KiB (the maximum, fewest requests) rather than raising,
        so a misconfigured knob never aborts an unattended backup.
        """
        valid = (4, 8, 16, 32, 64, 128, 256, 512)
        try:
            kb = int(value) if value else 512
        except ValueError:
            kb = 512
        if kb in valid:
            return kb
        # Snap down to the largest valid size not exceeding the request.
        for candidate in reversed(valid):
            if kb >= candidate:
                return candidate
        return valid[0]

    def get_parallel_download_min_size_bytes(self) -> int:
        """Minimum file size (bytes) that triggers the parallel download path."""
        return self.parallel_download_min_size_mb * 1024 * 1024

    def get_parallel_download_part_size_bytes(self) -> int:
        """Per-request chunk size in bytes for parallel downloads."""
        return self.parallel_download_part_size_kb * 1024

    def _parse_id_list(self, id_str: str) -> set:
        """Parse comma-separated ID string into a set of integers."""
        if not id_str or not id_str.strip():
            return set()
        return {int(id.strip()) for id in id_str.split(",") if id.strip()}

    def _validate_event_webhook(self) -> None:
        """Validate EVENT_WEBHOOK_* sub-options; on any problem warn + disable (#336).

        Warnings name the variable and the expected format but never echo the
        configured value: the URL is a bearer capability and headers may hold
        auth tokens. Runs only when EVENT_WEBHOOK_ENABLED=true.
        """

        def _disable(message: str) -> None:
            logger.warning(f"{message} - event webhook disabled")
            self.event_webhook_enabled = False

        # LAN targets (ntfy, Gotify, Apprise) are plain http, so both schemes pass.
        if not self.event_webhook_url.lower().startswith(("http://", "https://")):
            _disable("EVENT_WEBHOOK_URL must start with http:// or https://")
            return
        if self.event_webhook_method not in {"POST", "PUT"}:
            _disable("EVENT_WEBHOOK_METHOD must be POST or PUT")
            return
        headers_raw = os.getenv("EVENT_WEBHOOK_HEADERS", "").strip()
        if headers_raw:
            try:
                parsed = json.loads(headers_raw)
            except json.JSONDecodeError:
                _disable("EVENT_WEBHOOK_HEADERS must be valid JSON")
                return
            if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
            ):
                _disable("EVENT_WEBHOOK_HEADERS must be a JSON object of string values")
                return
            self.event_webhook_headers = parsed
        # The declared Content-Type drives per-placeholder auto-escaping; when
        # absent, declare the default template's actual content type.
        if not any(k.lower() == "content-type" for k in self.event_webhook_headers):
            self.event_webhook_headers["Content-Type"] = "application/json; charset=utf-8"
        events_raw = os.getenv("EVENT_WEBHOOK_EVENTS", "").strip()
        if events_raw:
            requested = {part.strip().lower() for part in events_raw.split(",") if part.strip()}
            if not requested or not requested <= {"message_edited", "message_deleted"}:
                _disable("EVENT_WEBHOOK_EVENTS entries must be message_edited and/or message_deleted")
                return
            self.event_webhook_events = requested
        chat_ids_raw = os.getenv("EVENT_WEBHOOK_CHAT_IDS", "")
        try:
            self.event_webhook_chat_ids = self._parse_id_list(chat_ids_raw)
        except ValueError:
            _disable("EVENT_WEBHOOK_CHAT_IDS must be comma-separated integer chat ids")
            return

    def _parse_topic_skip_list(self, skip_str: str) -> dict[int, set[int]]:
        """Parse SKIP_TOPIC_IDS into {chat_id: {topic_id, ...}}.

        Format: chat_id:topic_id,chat_id:topic_id,...
        Example: -1001234567890:42,-1001234567890:1337,-1009876543210:7
        """
        result: dict[int, set[int]] = {}
        if not skip_str or not skip_str.strip():
            return result
        for entry in skip_str.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                raise ValueError(f"Invalid SKIP_TOPIC_IDS entry '{entry}': expected format chat_id:topic_id")
            chat_part, topic_part = entry.split(":", 1)
            try:
                chat_id = int(chat_part.strip())
                topic_id = int(topic_part.strip())
            except ValueError as e:
                raise ValueError(
                    f"Invalid SKIP_TOPIC_IDS entry '{entry}': chat_id and topic_id must be integers"
                ) from e
            result.setdefault(chat_id, set()).add(topic_id)
        return result

    def _parse_accounts(self) -> tuple[list[AccountConfig], bool]:
        """Parse TG_ACCOUNT_<N>_* into an ordered account list.

        Returns ``(accounts, indexed)`` where ``indexed`` is True when any
        TG_ACCOUNT_* variable declared an account and False when the single
        account was synthesized from the legacy TELEGRAM_* variables.

        Session-name resolution: TG_ACCOUNT_<N>_SESSION_NAME wins when set;
        account 1 then falls back to the legacy chain (SESSION_NAME env →
        'telegram_backup') so an existing deployment keeps its session file,
        and accounts 2+ fall back to 'telegram_backup_account<N>'.

        Error messages name the offending VARIABLE, never its value: the
        values are credentials and phone numbers (PII).
        """
        declared: dict[int, dict[str, str]] = {}
        filter_overrides: dict[int, dict[str, str]] = {}
        for key, value in os.environ.items():
            if not key.startswith("TG_ACCOUNT_"):
                continue
            match = _TG_ACCOUNT_ENV_RE.match(key)
            if match is None:
                raise ValueError(
                    f"Unrecognized account variable '{key}'. Expected TG_ACCOUNT_<N>_API_ID / _API_HASH / "
                    "_PHONE_NUMBER / _LABEL / _SESSION_NAME, or a per-account filter override "
                    "(_CHAT_IDS, _CHAT_TYPES, _INCLUDE/_EXCLUDE_CHAT_IDS, the PRIVATE_/GROUPS_/CHANNELS_ "
                    "variants, _PRIORITY_CHAT_IDS, _SKIP_MEDIA_CHAT_IDS), with N starting at 1 (no leading zeros)."
                )
            # docker-compose's ${VAR:-} idiom injects empty strings for unset
            # host variables; treat them exactly like absent variables.
            if not value.strip():
                continue
            suffix = match.group(2)
            # Filter overrides never make an account "declared": a legacy
            # zero-config install may override account 1's filters without
            # being forced into indexed mode (and its credential triple).
            target = filter_overrides if suffix in _TG_ACCOUNT_FILTER_SUFFIXES else declared
            target.setdefault(int(match.group(1)), {})[suffix] = value.strip()
        self._account_filter_overrides = filter_overrides

        if not declared:
            # Zero-config upgrade: byte-identical single-account behavior,
            # including the credentials-optional viewer case (the Nones).
            # 'default' matches the label migration 022 seeds on row 1, so the
            # runtime's claim of that row rewrites nothing.
            return [
                AccountConfig(
                    index=1,
                    api_id=self.api_id,
                    api_hash=self.api_hash,
                    phone=self.phone,
                    label="default",
                    session_name=self.session_name,
                    session_path=self.session_path,
                )
            ], False

        indexes = sorted(declared)
        if indexes != list(range(1, len(indexes) + 1)):
            missing = next(i for i in range(1, max(indexes) + 1) if i not in declared)
            raise ValueError(
                f"TG_ACCOUNT_* indexes must be contiguous starting at 1: "
                f"account {max(indexes)} is declared but TG_ACCOUNT_{missing}_API_ID is missing"
            )

        accounts: list[AccountConfig] = []
        phone_owner: dict[str, int] = {}
        session_owner: dict[str, int] = {}
        for index in indexes:
            variables = declared[index]
            missing_suffixes = [s for s in _TG_ACCOUNT_REQUIRED_SUFFIXES if s not in variables]
            if missing_suffixes:
                names = ", ".join(f"TG_ACCOUNT_{index}_{suffix}" for suffix in missing_suffixes)
                raise ValueError(f"Account {index} is incomplete: {names} missing")
            try:
                api_id = int(variables["API_ID"])
            except ValueError:
                # ``from None`` on purpose: the chained int() error would echo
                # the raw value, and these messages never carry values.
                raise ValueError(f"TG_ACCOUNT_{index}_API_ID must be an integer") from None

            phone = variables["PHONE_NUMBER"]
            if phone in phone_owner:
                raise ValueError(
                    f"TG_ACCOUNT_{index}_PHONE_NUMBER duplicates TG_ACCOUNT_{phone_owner[phone]}_PHONE_NUMBER: "
                    "each account must be a distinct Telegram identity"
                )
            phone_owner[phone] = index

            if index == 1:
                # Account 1 keeps the legacy resolution so no existing
                # deployment ever re-logins after declaring indexed accounts.
                session_name = variables.get("SESSION_NAME") or self.session_name
            else:
                session_name = variables.get("SESSION_NAME") or f"telegram_backup_account{index}"
            if session_name in session_owner:
                raise ValueError(
                    f"Accounts {session_owner[session_name]} and {index} resolve to the same session file: "
                    f"set a distinct TG_ACCOUNT_{index}_SESSION_NAME (two clients sharing one Telethon "
                    "session corrupt it)"
                )
            session_owner[session_name] = index

            accounts.append(
                AccountConfig(
                    index=index,
                    api_id=api_id,
                    api_hash=variables["API_HASH"],
                    phone=phone,
                    label=variables.get("LABEL") or ("default" if index == 1 else f"account{index}"),
                    session_name=session_name,
                    session_path=os.path.join(self.session_dir, session_name),
                )
            )
        return accounts, True

    def _resolve_account_filters(self) -> dict[int, AccountFilters]:
        """Effective capture filters per account (8.1, #313).

        A TG_ACCOUNT_<N>_<FILTER> variable wins for that account; the global
        variable is the fallback, mirroring how sessions resolve. An empty
        indexed value inherits — the compose ${VAR:-} idiom injects empty
        strings, and silently clearing a whitelist would widen capture — so
        explicit-empty is spelled with the literal token ``none`` ("no
        whitelist for this account" / "no entries in this list").
        """
        overrides = self._account_filter_overrides
        known = {account.index for account in self.accounts}
        orphaned = sorted(set(overrides) - known)
        if orphaned:
            raise ValueError(
                f"TG_ACCOUNT_{orphaned[0]}_* filter variables are declared but no account {orphaned[0]} "
                f"exists ({len(known)} account(s) configured)"
            )

        resolved: dict[int, AccountFilters] = {}
        for account in self.accounts:
            raw = overrides.get(account.index, {})

            def ids(suffix: str, global_value: set, raw: dict = raw) -> frozenset:
                value = raw.get(suffix)
                if value is None:
                    return frozenset(global_value)
                if value.lower() == "none":
                    return frozenset()
                return frozenset(self._parse_id_list(value))

            types_value = raw.get("CHAT_TYPES")
            if types_value is None:
                chat_types = tuple(self.chat_types)
            elif types_value.lower() == "none":
                chat_types = ()
            else:
                chat_types = tuple(ct.strip().lower() for ct in types_value.split(",") if ct.strip())
                invalid = set(chat_types) - _VALID_CHAT_TYPES
                if invalid:
                    raise ValueError(
                        f"TG_ACCOUNT_{account.index}_CHAT_TYPES has invalid chat types: {sorted(invalid)}. "
                        f"Valid options are: {sorted(_VALID_CHAT_TYPES)}"
                    )

            resolved[account.index] = AccountFilters(
                chat_ids=ids("CHAT_IDS", self.chat_ids),
                chat_types=chat_types,
                global_include_ids=ids("INCLUDE_CHAT_IDS", self.global_include_ids),
                global_exclude_ids=ids("EXCLUDE_CHAT_IDS", self.global_exclude_ids),
                private_include_ids=ids("PRIVATE_INCLUDE_CHAT_IDS", self.private_include_ids),
                private_exclude_ids=ids("PRIVATE_EXCLUDE_CHAT_IDS", self.private_exclude_ids),
                groups_include_ids=ids("GROUPS_INCLUDE_CHAT_IDS", self.groups_include_ids),
                groups_exclude_ids=ids("GROUPS_EXCLUDE_CHAT_IDS", self.groups_exclude_ids),
                channels_include_ids=ids("CHANNELS_INCLUDE_CHAT_IDS", self.channels_include_ids),
                channels_exclude_ids=ids("CHANNELS_EXCLUDE_CHAT_IDS", self.channels_exclude_ids),
                priority_chat_ids=ids("PRIORITY_CHAT_IDS", self.priority_chat_ids),
                skip_media_chat_ids=ids("SKIP_MEDIA_CHAT_IDS", self.skip_media_chat_ids),
            )
        return resolved

    def filters_for(self, index: int) -> AccountFilters:
        """The effective capture filters of the account at env index ``index``."""
        return self.account_filters[index]

    def for_account(self, index: int) -> AccountScopedConfig:
        """The config view the capture workers of account ``index`` receive."""
        return AccountScopedConfig(self, index, self.filters_for(index))

    def should_skip_topic(self, chat_id: int, topic_id: int | None) -> bool:
        """Check if a specific topic in a chat should be skipped.

        Args:
            chat_id: Telegram chat ID (marked format)
            topic_id: Forum topic ID (reply_to_top_id), or None for non-topic messages

        Returns:
            True if this topic should be skipped, False otherwise
        """
        if topic_id is None or not self.skip_topic_ids:
            return False
        skip_set = self.skip_topic_ids.get(chat_id)
        if skip_set is None:
            return False
        return topic_id in skip_set

    def _get_required_env(self, key: str, value_type: type):
        """
        Get a required environment variable and convert to specified type.

        Args:
            key: Environment variable name
            value_type: Type to convert the value to (int or str)

        Returns:
            Converted environment variable value

        Raises:
            ValueError: If environment variable is not set
        """
        value = os.getenv(key)
        if value is None or value == "":
            raise ValueError(
                f"Required environment variable '{key}' is not set. Please set it in your .env file or environment."
            )

        try:
            if value_type == int:
                return int(value)
            return value
        except ValueError as e:
            raise ValueError(f"Environment variable '{key}' must be a valid {value_type.__name__}: {e}")

    def _validate_chat_types(self):
        """Validate that chat types are valid options.

        Empty chat_types list is allowed - this enables "whitelist-only" mode
        where only explicitly included chat IDs are backed up.
        """
        valid_types = _VALID_CHAT_TYPES
        invalid_types = set(self.chat_types) - valid_types

        if invalid_types:
            raise ValueError(f"Invalid chat types: {invalid_types}. Valid options are: {valid_types}")

    def _ensure_directories(self):
        """Create necessary directories if they don't exist."""
        os.makedirs(self.backup_path, exist_ok=True)
        os.makedirs(self.session_dir, exist_ok=True)

        # Ensure database directory exists
        db_dir = os.path.dirname(self.database_path)
        os.makedirs(db_dir, exist_ok=True)

        if self.download_media:
            os.makedirs(self.media_path, exist_ok=True)

    def should_backup_chat_type(self, is_user: bool, is_group: bool, is_channel: bool, is_bot: bool = False) -> bool:
        """
        Determine if a chat should be backed up based on its type.

        Args:
            is_user: True if chat is a private conversation (non-bot)
            is_group: True if chat is a group
            is_channel: True if chat is a channel
            is_bot: True if chat is a bot conversation

        Returns:
            True if chat should be backed up, False otherwise
        """
        if is_bot and "bots" in self.chat_types:
            return True
        if is_user and "private" in self.chat_types:
            return True
        if is_group and "groups" in self.chat_types:
            return True
        if is_channel and "channels" in self.chat_types:
            return True
        return False

    def should_backup_chat(
        self, chat_id: int, is_user: bool, is_group: bool, is_channel: bool, is_bot: bool = False
    ) -> bool:
        """
        Determine if a chat should be backed up based on its ID and type.

        Two modes:

        MODE 1 - Whitelist Mode (CHAT_IDS is set):
            Backup ONLY the chats in CHAT_IDS. Everything else is ignored.
            Simple, explicit, no ambiguity.

        MODE 2 - Type-based Mode (CHAT_IDS not set):
            Filtering logic (Priority Order):
            1. Global Exclude (Blacklist) -> Skip
            2. Type-Specific Exclude -> Skip
            3. Global Include -> Backup (additive)
            4. Type-Specific Include -> Backup (additive for that type)
            5. Chat Type Filter (CHAT_TYPES) -> Backup if matches

        Args:
            chat_id: Telegram chat ID
            is_user: True if chat is a private conversation (non-bot)
            is_group: True if chat is a group
            is_channel: True if chat is a channel
            is_bot: True if chat is a bot conversation

        Returns:
            True if chat should be backed up, False otherwise
        """
        # =====================================================================
        # MODE 1: Whitelist Mode - CHAT_IDS takes absolute priority
        # =====================================================================
        if self.whitelist_mode:
            return chat_id in self.chat_ids

        # =====================================================================
        # MODE 2: Type-based Mode
        # =====================================================================

        # 1. Global Exclude
        if chat_id in self.global_exclude_ids:
            return False

        # 2. Type-Specific Exclude (bots use private exclude lists)
        if (is_user or is_bot) and chat_id in self.private_exclude_ids:
            return False
        if is_group and chat_id in self.groups_exclude_ids:
            return False
        if is_channel and chat_id in self.channels_exclude_ids:
            return False

        # 3. Global Include (acts as whitelist - if set, ONLY these are backed up)
        if self.global_include_ids:
            return chat_id in self.global_include_ids

        # 4. Type-Specific Include (bots use private include lists)
        if (is_user or is_bot) and self.private_include_ids:
            return chat_id in self.private_include_ids
        if is_group and self.groups_include_ids:
            return chat_id in self.groups_include_ids
        if is_channel and self.channels_include_ids:
            return chat_id in self.channels_include_ids

        # 5. Chat Type Filter (only if no include lists are set)
        return self.should_backup_chat_type(is_user, is_group, is_channel, is_bot)

    def get_max_media_size_bytes(self) -> int:
        """Maximum media file size in bytes; 0 (or negative) means no limit.

        0 disabling the cap is the meaning 0 already carries across this
        config surface (DOWNLOAD_TIMEOUT_SECONDS, MEDIA_FLOOD_SLEEP_THRESHOLD,
        WHITELIST_RESOLVE_DIALOG_LIMIT). It used to mean "skip every file
        with a nonzero size" — silently, at DEBUG level.
        """
        if self.max_media_size_mb <= 0:
            return sys.maxsize
        return self.max_media_size_mb * 1024 * 1024

    def should_download_media_for_chat(self, chat_id: int) -> bool:
        """
        Determine if media should be downloaded for a specific chat.

        Args:
            chat_id: Telegram chat ID (marked format)

        Returns:
            True if media should be downloaded, False if skipped
        """
        # If global media download is disabled, return False
        if not self.download_media:
            return False

        # Check if chat is in skip list
        if chat_id in self.skip_media_chat_ids:
            return False

        return True

    def validate_credentials(self):
        """Ensure Telegram credentials are present for every configured account."""
        if self._indexed_accounts:
            # Indexed TG_ACCOUNT_<N>_* accounts were validated for complete
            # triples (and an integer API_ID) when they were parsed, so being
            # here means every declared account is whole.
            return
        if not all([self.api_id, self.api_hash, self.phone]):
            raise ValueError(
                "Missing required Telegram credentials (TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE). "
                "Please set them in your .env file."
            )

    def get_telegram_client_kwargs(self) -> dict:
        """Get shared TelegramClient keyword arguments.

        ``flood_sleep_threshold=0`` forces Telethon to raise FloodWaitError
        instead of silently sleeping, so long waits become visible in the log
        via ``iter_messages_with_flood_retry``. Media downloads and
        get_dialogs() calls temporarily raise the client threshold via
        ``absorb_media_floods`` (#232 for media / MEDIA_FLOOD_SLEEP_THRESHOLD,
        #295 for dialogs / DIALOG_FLOOD_SLEEP_THRESHOLD); everything else
        keeps 0 so floods stay visible in app logs (#124).
        """
        kwargs: dict = {"flood_sleep_threshold": 0}
        if self.telegram_proxy is not None:
            kwargs["proxy"] = dict(self.telegram_proxy)
        return kwargs


def setup_logging(config: Config):
    """
    Configure logging for the application.

    Args:
        config: Configuration object with log level
    """
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Set Telethon logging to WARNING to reduce noise
    logging.getLogger("telethon").setLevel(logging.WARNING)


if __name__ == "__main__":
    # Test configuration loading
    try:
        config = Config()
        setup_logging(config)
        logger.info("Configuration test successful")
        logger.info(f"API ID: {config.api_id}")
        # This self-test needs to report that a phone number parsed, not what it
        # is: the number is PII and setup_logging sends this to stderr, which is
        # captured wherever the check runs. bool() is applied before the logging
        # call so the statement itself never reads the attribute.
        phone_configured = bool(config.phone)
        logger.info(f"Phone configured: {phone_configured}")
        logger.info(f"Schedule: {config.schedule}")
        logger.info(f"Chat types: {config.chat_types}")
    except ValueError as e:
        print(f"Configuration error: {e}")
