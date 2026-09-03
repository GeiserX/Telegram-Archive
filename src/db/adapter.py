"""
Async database adapter for Telegram Backup.

Provides all database operations using SQLAlchemy async.
This is a drop-in replacement for the old Database class.
"""

import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any

from sqlalchemy import (
    and_,
    delete,
    desc,
    exists,
    false,
    func,
    literal,
    nulls_last,
    or_,
    select,
    text,
    tuple_,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError

from ..message_utils import (
    METADATA_ONLY_MEDIA_TYPES,
    compute_directory_size,
    resolve_sender_display_name,
    utcnow_naive,
)
from .base import DatabaseManager
from .fts import PG_TSQUERY_FROM_SEARCH, PG_TSVECTOR_COLUMN, SQLITE_FTS_TABLE, fts_match_query, search_has_words
from .models import (
    DEFAULT_ACCOUNT_ID,
    Account,
    AppSettings,
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    Message,
    MessageVersion,
    Metadata,
    PushSubscription,
    Reaction,
    SyncStatus,
    User,
    ViewerAccount,
    ViewerAuditLog,
    ViewerSession,
    ViewerToken,
)

logger = logging.getLogger(__name__)

# Marked ids for channels and supergroups live below this ceiling (-100…).
# Bare (peerless) events can only ever refer to the common message box, whose
# ids sit above it — the same constant migration 022 types placeholders with.
SUPERGROUP_ID_CEILING = -(10**12)


def _strip_tz(dt: datetime | None) -> datetime | None:
    """Strip timezone info from datetime for PostgreSQL compatibility."""
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _is_nonblank_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strip_nul(value: str | None) -> str | None:
    """Replace NUL bytes so PostgreSQL never rejects the row (None passes through).

    PostgreSQL text columns reject \\x00 outright while SQLite stores it, and
    every ``create_audit_log`` caller swallows the resulting exception — so a
    NUL smuggled into a login field left NO audit row on PostgreSQL. Replacement
    (not deletion) keeps a NUL-suffixed impersonation of an existing username
    distinguishable from the genuine one in the audit trail. Other C0 control
    characters are accepted by both backends and pass through untouched.
    """
    if value is None:
        return None
    return value.replace("\x00", "�")


def _clamp(value: str | None, max_length: int) -> str | None:
    """Truncate a value to its column width, NUL-scrubbed (None passes through)."""
    if value is None:
        return None
    return _strip_nul(value)[:max_length]


def _has_raw_payload(value: Any) -> bool:
    """True when a serialised raw_data blob carries anything worth keeping."""
    return bool(value) and value != "{}"


def parse_entitlement_column(raw: str | None, element_type: type) -> set | None:
    """Read one v8.0.0 entitlement column (allowed_accounts / allowed_chat_refs) fail-closed.

    The reader half of migration 022's converter, shared by the viewer and the
    push filter so the two can never diverge. NULL means "no restriction" and
    returns None. A well-formed JSON list whose every element is exactly
    ``element_type`` returns that set — including the empty set, which denies
    everything. ANY other payload (unparseable JSON, a non-list, a list with a
    foreign element) also returns the empty set: a grant that cannot be read
    must deny, never widen. bool is excluded from int on purpose — True would
    otherwise read as account 1.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except TypeError, ValueError:
        return set()
    if not isinstance(parsed, list):
        return set()
    values = set()
    for element in parsed:
        if not isinstance(element, element_type) or isinstance(element, bool):
            return set()
        values.add(element)
    return values


@dataclass(frozen=True)
class ChatScope:
    """The set of chat rows one principal may see, as data both Python and SQL can read.

    Chat visibility is decided by exactly three rules, and this object is the
    ONE place they are written down:

    * ``ids``      - the operator's DISPLAY_CHAT_IDS filter (``chats.id``)
    * ``accounts`` - the viewer's account grant (``chats.account_id``)
    * ``refs``     - the viewer's chat-ref grant (``chats.ref``)

    Each field is ``None`` (that rule restricts nothing) or a collection (the
    grant). ``None`` and the EMPTY collection are NOT the same thing and the
    difference is the whole security story: an empty grant means "entitled to
    nothing" and MUST match zero rows. Rendering it as a skipped filter — the
    classic falsy-empty-list bug — is a total entitlement bypass, so
    :meth:`sql_predicates` maps it to ``false()`` explicitly rather than
    trusting any dialect's empty-``IN`` rendering.

    :meth:`allows` and :meth:`sql_predicates` are twins: the same three rules,
    in the same order, one evaluated in Python (websocket delivery, the ref
    resolver) and one pushed into the WHERE clause (the chat list). They are
    written next to each other so they cannot drift, and
    ``tests/test_chat_scope_equivalence.py`` runs the whole rule space through
    both and asserts the two answers are identical.
    """

    ids: frozenset[int] | None = None
    accounts: frozenset[int] | None = None
    refs: frozenset[str] | None = None

    @classmethod
    def build(
        cls,
        *,
        ids: Collection[int] | None = None,
        accounts: Collection[int] | None = None,
        refs: Collection[str] | None = None,
    ) -> ChatScope:
        """Freeze caller-supplied grants, preserving None-vs-empty exactly."""
        return cls(
            ids=None if ids is None else frozenset(ids),
            accounts=None if accounts is None else frozenset(accounts),
            refs=None if refs is None else frozenset(refs),
        )

    @property
    def unrestricted(self) -> bool:
        """True when no rule restricts anything, so the scope can be skipped entirely."""
        return self.ids is None and self.accounts is None and self.refs is None

    def allows(self, chat: Mapping[str, Any]) -> bool:
        """Whether ``chat`` (a row dict carrying id/account_id/ref) is in scope.

        Each key is read ONLY when its rule is active, so a partial row dict is
        as acceptable here as it was to the hand-written check this replaces.
        """
        if self.ids is not None and chat["id"] not in self.ids:
            return False
        if self.accounts is not None and chat["account_id"] not in self.accounts:
            return False
        if self.refs is not None and chat["ref"] not in self.refs:
            return False
        return True

    def sql_predicates(self) -> list[Any]:
        """The same three rules as WHERE-clause fragments against ``chats``."""
        predicates: list[Any] = []
        for column, grant in ((Chat.id, self.ids), (Chat.account_id, self.accounts), (Chat.ref, self.refs)):
            if grant is None:
                continue
            # An empty grant is "nothing", never "no filter".
            predicates.append(column.in_(grant) if grant else false())
        return predicates


# Message columns an upsert may refresh ONLY when the writer actually supplied
# the key. ``_message_values`` materialises every column with a ``.get()``
# default, so an absent key is indistinguishable from an explicit NULL by the
# time the ON CONFLICT path runs — and writing that NULL erases data a previous
# writer did capture. ``import --merge`` omits ``reply_to_top_id`` entirely, so
# merging an export into an already-backed-up forum chat un-assigned every
# overlapping message from its topic. Chats (``upsert_chat``) and media
# (``insert_media``) already build their update sets this way; messages did not.
# ``id``/``chat_id``/``date`` are required keys and are never optional.
_MESSAGE_OPTIONAL_UPDATE_KEYS = (
    "sender_id",
    "sender_name",
    "text",
    "reply_to_msg_id",
    "reply_to_top_id",
    "reply_to_text",
    "forward_from_id",
    "edit_date",
    "raw_data",
    "is_outgoing",
    "is_pinned",
    "is_deleted",
    "deleted_at",
)


def _message_conflict_update_values(message_data: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Build update values for message upserts without undoing soft deletes.

    Drops every column the caller did not supply, so a partial writer refreshes
    what it observed and leaves the rest of the archived row alone.
    """
    update_values = dict(values)

    for key in _MESSAGE_OPTIONAL_UPDATE_KEYS:
        if key not in message_data:
            update_values.pop(key, None)

    if not message_data.get("is_deleted"):
        update_values.pop("is_deleted", None)
        update_values.pop("deleted_at", None)

    return update_values


def _datetime_hash_value(dt: datetime | None) -> str | None:
    dt = _strip_tz(dt)
    if dt is None:
        return None
    return dt.isoformat(timespec="microseconds")


def _message_version_hash(
    chat_id: int,
    message_id: int,
    text: str | None,
    date: datetime,
) -> str:
    # FROZEN CONTRACT: this exact encoding (key set, sort_keys, separators,
    # microsecond timespec) IS the dedup identity for message_versions rows via
    # the unique change_hash column. Changing any detail silently re-admits
    # duplicates of already-stored versions. Known accepted limit: repeated
    # no-edit_date edits that oscillate back to the same text reuse the same
    # fallback date and dedup into one row.
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "date": _datetime_hash_value(date),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def retry_on_locked(
    max_retries: int = 5, initial_delay: float = 0.1, max_delay: float = 2.0, backoff_factor: float = 2.0
):
    """
    Decorator to retry async database operations on operational errors.

    Works for both SQLite (database locked) and PostgreSQL (connection issues).
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    error_str = str(e).lower()
                    if "locked" not in error_str and "connection" not in error_str:
                        raise

                    last_exception = e
                    if attempt < max_retries:
                        # Type name only: the raw exception text can carry the SQL
                        # statement, bound values, or a connection DSN, and this
                        # wraps writers whose payloads identify chats.
                        logger.warning(
                            f"Database error on {func.__name__}, attempt {attempt + 1}/{max_retries + 1}. "
                            f"Retrying in {delay:.2f}s... Error type: {type(e).__name__}"
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                    else:
                        logger.error(f"Database error on {func.__name__} after {max_retries + 1} attempts. Giving up.")
                        raise

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


def _is_statement_timeout(exc: BaseException) -> bool:
    """PostgreSQL's statement_timeout firing (SQLSTATE 57014), as SQLAlchemy wraps it."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "57014" or type(orig).__name__ == "QueryCanceledError"


class DatabaseAdapter:
    """
    Async database adapter compatible with the old Database class interface.

    All methods are async and should be awaited.

    v8.0.0 account contract. Every table holding Telegram data is keyed by
    ``account_id`` (see models.py), and the adapter names the account explicitly:

    - Capture-side methods (writes, and reads that feed capture decisions such
      as gap detection or sync state) take keyword-only ``account_id: int`` with
      NO default — a caller that forgets the account is a TypeError at call
      time, never a row written under the server default.
    - Viewer/MCP-facing reads take ``account_id: int | None = None``; ``None``
      means unscoped, which is correct while the archive holds one account.
      Phase 4 (viewer entitlements) closes that hole by passing the account.
    - Tables that are global by design (metadata, users, app_settings, the
      viewer_* tables) keep their pre-8.0 signatures.
    """

    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize adapter with a DatabaseManager.

        Args:
            db_manager: Initialized DatabaseManager instance
        """
        self.db_manager = db_manager
        self._is_sqlite = db_manager._is_sqlite
        # Full-text capability, probed once on first search: None = unknown.
        self._fts_ready_cache: bool | None = None

    def _serialize_raw_data(self, raw_data: Any) -> str:
        """
        Safely serialize raw_data to JSON.

        Args:
            raw_data: Data to serialize

        Returns:
            JSON string representation
        """
        if not raw_data:
            return "{}"

        try:
            return json.dumps(raw_data)
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to serialize raw_data directly: {e}")
            try:

                def convert_to_serializable(obj):
                    if isinstance(obj, dict):
                        return {k: convert_to_serializable(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [convert_to_serializable(item) for item in obj]
                    elif isinstance(obj, (str, int, float, bool, type(None))):
                        return obj
                    else:
                        return str(obj)

                serializable_data = convert_to_serializable(raw_data)
                return json.dumps(serializable_data)
            except Exception as e2:
                logger.error(f"Failed to serialize raw_data even after conversion: {e2}")
                return "{}"

    def _message_values(self, message_data: dict[str, Any], account_id: int) -> dict[str, Any]:
        sender_name = message_data.get("sender_name")
        sender_name = sender_name.strip() if _is_nonblank_text(sender_name) else None
        return {
            # Always explicit, never the column's server default: an INSERT
            # missing a server-defaulted PK column makes SQLAlchemy append
            # RETURNING for it, and on the ON CONFLICT DO NOTHING path that
            # changes what rowcount means — the caller then skips the update
            # branch and an edited message's new text is silently dropped.
            "account_id": account_id,
            "id": message_data["id"],
            "chat_id": message_data["chat_id"],
            "sender_id": message_data.get("sender_id"),
            "sender_name": sender_name,
            "date": _strip_tz(message_data["date"]),
            "text": message_data.get("text"),
            "reply_to_msg_id": message_data.get("reply_to_msg_id"),
            "reply_to_top_id": message_data.get("reply_to_top_id"),
            "reply_to_text": message_data.get("reply_to_text"),
            "forward_from_id": message_data.get("forward_from_id"),
            "edit_date": _strip_tz(message_data.get("edit_date")),
            "raw_data": self._serialize_raw_data(message_data.get("raw_data", {})),
            "is_outgoing": message_data.get("is_outgoing", 0),
            "is_pinned": message_data.get("is_pinned", 0),
            "is_deleted": message_data.get("is_deleted", 0),
            "deleted_at": _strip_tz(message_data.get("deleted_at")),
        }

    def _insert_message_stmt(self, values: dict[str, Any]):
        if self._is_sqlite:
            return (
                sqlite_insert(Message)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["account_id", "chat_id", "id"])
            )
        return (
            pg_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["account_id", "chat_id", "id"])
        )

    def _insert_message_version_stmt(self, values: dict[str, Any]):
        if self._is_sqlite:
            return (
                sqlite_insert(MessageVersion)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["account_id", "change_hash"])
            )
        return (
            pg_insert(MessageVersion)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["account_id", "change_hash"])
        )

    async def _record_message_version(
        self,
        session,
        account_id: int,
        chat_id: int,
        message_id: int,
        text: str | None,
        date: datetime,
    ) -> bool:
        """Best-effort capture of a superseded text into message_versions.

        Versioning is plain text only — formatting/entity-only edits produce the
        same text and are intentionally not versioned. Runs inside a SAVEPOINT so
        an unexpected failure here can never poison the transaction or abort the
        message upsert/batch it belongs to (the expected duplicate case is already
        silenced by ON CONFLICT DO NOTHING on change_hash).
        """
        date = _strip_tz(date)
        if date is None:
            return False

        change_hash = _message_version_hash(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            date=date,
        )
        values = {
            # The hash payload is a frozen contract and does NOT carry the
            # account; the (account_id, change_hash) constraint does instead.
            "account_id": account_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "date": date,
            "change_hash": change_hash,
            "captured_at": utcnow_naive(),
        }
        try:
            async with session.begin_nested():
                result = await session.execute(self._insert_message_version_stmt(values))
        except Exception as e:
            logger.warning("Could not record a message version (%s); message update continues", type(e).__name__)
            return False
        return bool(result.rowcount)

    def _message_version_date(self, message: Message) -> datetime:
        return _strip_tz(message.edit_date) or _strip_tz(message.date)

    def _should_apply_upsert_text(self, existing: Message, values: dict[str, Any]) -> bool:
        """Decide whether a re-scanned/imported message may replace archived text.

        Truth table (upsert sources: backup re-scan, gap-fill, import):
        - same text            -> never bump edit_date (#219): Telegram bumps
                                  edit_date server-side for reaction-only changes,
                                  so treating an unchanged-text re-scan as an edit
                                  produced a phantom "edited" marker. Non-text
                                  metadata still refreshes via _pending_update_values.
        - empty -> non-empty   -> always fill (late hydration), even without an
                                  edit_date; caller preserves the existing edit_date.
        - differing text, no incoming edit_date -> refuse: an upsert source with no
                                  edit evidence must never clobber archived text.
        - differing text, incoming edit_date >= archived (or archived None) -> apply.
          ``>=`` (not ``>``) is deliberate: listener and backup can deliver the same
          edit with equal timestamps but the text seen later is the fresher fetch.
        """
        new_text = values.get("text")
        new_edit_date = _strip_tz(values.get("edit_date"))
        old_text = existing.text
        old_edit_date = _strip_tz(existing.edit_date)

        if old_text == new_text:
            # Same text -> never bump edit_date. Telegram bumps a message's
            # edit_date server-side when only reactions change (#219), so bumping
            # here would set edit_date with no version and surface a phantom
            # "edited" marker on re-scan/gap-fill/import too. Non-text metadata
            # still refreshes via _pending_update_values regardless of this gate;
            # reactions are reconciled by reconcile_reactions.
            return False
        if (old_text is None or old_text == "") and new_text not in (None, ""):
            return True
        if new_edit_date is None:
            return False
        if old_edit_date is None:
            return True
        if new_edit_date >= old_edit_date:
            return True
        return False

    def _should_apply_edit_text(self, existing: Message, new_text: str, edit_date: datetime | None) -> bool:
        """Decide whether a live edit event (listener/sync) may replace archived text.

        Differs from the upsert policy on the no-edit_date case: a live event with
        ``edit_date=None`` is applied only when the archived row was never edited —
        an already-edited row is never rolled over on date-less evidence (rare
        bot-API edits may hit this; conservative by design, covered by tests).
        """
        old_edit_date = _strip_tz(existing.edit_date)
        edit_date = _strip_tz(edit_date)

        if existing.text == new_text:
            # Text unchanged -> not a real text edit. Telegram bumps edit_date for
            # reaction-only changes (server-side; message.edit_hide is documented as
            # unreliable), so applying here would set edit_date with no version and
            # surface a phantom "edited" marker (#219). Reactions are captured by the
            # dedicated reaction path instead.
            return False
        if edit_date is None:
            return old_edit_date is None
        if old_edit_date is None:
            return True
        return edit_date >= old_edit_date

    async def _load_message_for_update(self, session, account_id: int, chat_id: int, message_id: int) -> Message | None:
        pk = and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id)
        if self._is_sqlite:
            # SQLite has no row-level SELECT FOR UPDATE. A no-op write acquires the
            # transaction's write lock before we re-read and decide whether to update.
            await session.execute(update(Message).where(pk).values(id=Message.id))
            stmt = select(Message).where(pk)
        else:
            stmt = select(Message).where(pk).with_for_update()

        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def _load_message_snapshot(self, session, account_id: int, chat_id: int, message_id: int) -> Message | None:
        """Plain lock-free read, used only for the fast-path no-change check."""
        stmt = select(Message).where(
            and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id)
        )
        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    def _pending_update_values(
        self, existing: Message, message_data: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        """Columns an upsert would actually change on ``existing`` (may be empty).

        Applies the text/edit_date gating policy, then drops every key whose value
        already matches the row, so re-scanning an unchanged message performs no
        write at all. Deliberate scope note: when text is withheld (older or
        no-evidence source), remaining metadata still refreshes from the incoming
        payload. The exception is sender_name: once nonblank, that capture-time
        snapshot is immutable.
        """
        update_values = _message_conflict_update_values(message_data, values)
        if self._should_apply_upsert_text(existing, values):
            if values.get("edit_date") is None and existing.edit_date is not None:
                # Text change arrived without edit evidence (e.g. late hydration):
                # keep the existing edit_date rather than nulling it.
                update_values.pop("edit_date", None)
        else:
            update_values.pop("text", None)
            update_values.pop("edit_date", None)

        # Sender names are capture-time snapshots. A missing/blank snapshot may
        # be hydrated once, but a nonblank archived value is immutable.
        if _is_nonblank_text(getattr(existing, "sender_name", None)) or not _is_nonblank_text(
            values.get("sender_name")
        ):
            update_values.pop("sender_name", None)

        # raw_data carries capture-time extras: the album grouped_id, service
        # action payloads, and the #228 group->supergroup migration pointers
        # get_migration_markers reads back. A source with no extras serialises to
        # the literal "{}", which is not evidence that the archived blob should
        # be empty. Same rule as sender_name: no information never overwrites
        # information.
        if not _has_raw_payload(values.get("raw_data")) and _has_raw_payload(getattr(existing, "raw_data", None)):
            update_values.pop("raw_data", None)

        changed = {}
        for key, value in update_values.items():
            if key in ("account_id", "id", "chat_id"):
                continue
            if getattr(existing, key) != value:
                changed[key] = value
        return changed

    async def _apply_existing_message_update(
        self,
        session,
        message_data: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        # Fast path: a lock-free read to detect the common re-scan case of a fully
        # unchanged message, so full re-backups don't pay the write lock + extra
        # statements per row. The definitive decision is re-made under the lock
        # below; skipping here is safe because a concurrent writer that changes the
        # row after our snapshot has, by definition, applied data at least as new
        # as ours.
        snapshot = await self._load_message_snapshot(session, values["account_id"], values["chat_id"], values["id"])
        if snapshot is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return
        if not self._pending_update_values(snapshot, message_data, values):
            return

        existing = await self._load_message_for_update(session, values["account_id"], values["chat_id"], values["id"])
        if existing is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return

        update_values = self._pending_update_values(existing, message_data, values)
        if not update_values:
            return
        if "text" in update_values:
            await self._record_message_version(
                session=session,
                account_id=existing.account_id,
                chat_id=existing.chat_id,
                message_id=existing.id,
                text=existing.text,
                date=self._message_version_date(existing),
            )
        await session.execute(
            update(Message)
            .where(
                and_(
                    Message.account_id == values["account_id"],
                    Message.chat_id == values["chat_id"],
                    Message.id == values["id"],
                )
            )
            .values(**update_values)
        )

    async def _insert_or_update_message(self, session, message_data: dict[str, Any], *, account_id: int) -> None:
        values = self._message_values(message_data, account_id)
        result = await session.execute(self._insert_message_stmt(values))
        if result.rowcount:
            return

        await self._apply_existing_message_update(session, message_data, values)

    # ========== Metadata Operations ==========

    @retry_on_locked()
    async def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        async with self.db_manager.async_session_factory() as session:
            # Use upsert
            if self._is_sqlite:
                stmt = sqlite_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            else:
                stmt = pg_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            await session.execute(stmt)
            await session.commit()

    @retry_on_locked()
    async def get_operator_status_counts(self, *, max_attempts: int) -> dict[str, Any]:
        """Aggregate media-pipeline counts for the operator status panel.

        Counts only (PII rule). ``pending`` rows are still retried by the
        scheduled pass; ``exhausted`` rows hit the retry cap and stay pending
        until the operator intervenes (the honesty split #212/#360 built).
        """
        async with self.db_manager.async_session_factory() as session:
            downloaded = (
                await session.execute(select(func.count()).select_from(Media).where(Media.downloaded == 1))
            ).scalar() or 0
            # Metadata-only rows (polls, dice, venues, ...) sit at
            # downloaded=0 by design — counting them as pending would show a
            # permanently-red pipeline for archives full of polls.
            not_metadata = Media.type.notin_(sorted(METADATA_ONLY_MEDIA_TYPES))
            pending = (
                await session.execute(
                    select(func.count())
                    .select_from(Media)
                    .where(Media.downloaded == 0, Media.download_attempts < max_attempts, not_metadata)
                )
            ).scalar() or 0
            exhausted = (
                await session.execute(
                    select(func.count())
                    .select_from(Media)
                    .where(Media.downloaded == 0, Media.download_attempts >= max_attempts, not_metadata)
                )
            ).scalar() or 0
        return {"downloaded": downloaded, "pending": pending, "exhausted": exhausted}

    async def get_database_size_bytes(self) -> int | None:
        """Best-effort on-disk size of the archive database.

        SQLite: the database file's size. PostgreSQL: pg_database_size().
        None when it cannot be determined — the status panel shows "unknown"
        rather than failing.
        """
        try:
            if self.db_manager._is_sqlite:
                url = self.db_manager.database_url
                _, sep, path = url.partition(":///")
                if not sep or not path:
                    return None
                return os.path.getsize(path)
            async with self.db_manager.async_session_factory() as session:
                result = await session.execute(text("SELECT pg_database_size(current_database())"))
                return int(result.scalar())
        except Exception:
            return None

    async def get_account_ids(self) -> list[int]:
        """All accounts.id values, ascending (viewer status aggregation, 8.1)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Account.id).order_by(Account.id))
            return [row[0] for row in result]

    async def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Metadata.value).where(Metadata.key == key))
            row = result.scalar_one_or_none()
            return row

    async def get_migration_markers(self, *, account_id: int) -> list[tuple[int, int]]:
        """Return stored group→supergroup migration pointers (#228).

        Selects service messages whose ``raw_data.action_type`` is
        ``chat_migrate_to`` and returns ``(old_chat_id, new_marked_id)`` pairs,
        where ``new_marked_id`` is ``raw_data.migrate_to_id`` (already in marked
        ``-100…`` form, written by ``_process_message``). SELECT-only; used to
        reconcile scope for migrations that occurred while the archiver was
        offline (the dead basic group may no longer surface as a dialog).

        The ``LIKE`` clause is only a cheap prefilter — the authoritative match
        is the Python-side ``json.loads`` — so the result is portable across the
        SQLite and PostgreSQL backends without dialect-specific JSON operators.
        PII: ids are returned to the caller for scope reconciliation only.
        """
        markers: list[tuple[int, int]] = []
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Message.chat_id, Message.raw_data).where(
                    and_(Message.account_id == account_id, Message.raw_data.like('%"chat_migrate_to"%'))
                )
            )
            for chat_id, raw in result.all():
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except ValueError, TypeError:
                    continue
                if data.get("action_type") != "chat_migrate_to":
                    continue
                new_id = data.get("migrate_to_id")
                if isinstance(new_id, int):
                    markers.append((chat_id, new_id))
        return markers

    # ========== Account Operations (v8.0.0) ==========

    @retry_on_locked()
    async def ensure_account(self, *, telegram_user_id: int, env_index: int, label: str) -> int:
        """Resolve the ``accounts`` row a logged-in account writes under.

        Called once per configured account per process start, after its client
        authenticates and ``get_me()`` yields the Telegram user id. Returns the
        ``accounts.id`` every capture-side call then passes as ``account_id``.

        Resolution order — the user id owns the row, the env index owns nothing:

        1. A row already carrying ``telegram_user_id`` wins outright (re-runs and
           re-ordered ``TG_ACCOUNT_<N>_*`` indexes always land here). The label is
           rewritten when it differs: the env is the display-name source of truth
           on every start.
        2. Only the account at env index 1 may claim the migrated row — pre-8.0
           rows carry no user id, and index 1 is defined as their continuation.
           The ``telegram_user_id IS NULL`` guard inside the UPDATE's WHERE makes
           the claim atomic and once-only; a row 1 already owned by a different
           user makes the guard miss, so reshuffled indexes never steal data.
        3. Anything else is a new identity: INSERT and return the generated id
           (migration 022 re-synced PostgreSQL's sequence past the seeded row).

        PII: the Telegram user id and the label never reach the log — the debug
        line names the env index and the resolved row id only (#272).
        """
        async with self.db_manager.async_session_factory() as session:
            # No unique constraint backs telegram_user_id, so read defensively:
            # if a corrupted archive ever held duplicates, the oldest row wins
            # deterministically instead of MultipleResultsFound killing every
            # backup run forever.
            row = (
                (
                    await session.execute(
                        select(Account).where(Account.telegram_user_id == telegram_user_id).order_by(Account.id)
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                if row.label != label:
                    row.label = label
                    await session.commit()
                logger.debug(f"account {env_index} -> row {row.id}")
                return row.id

            if env_index == 1:
                result = await session.execute(
                    update(Account)
                    .where(and_(Account.id == DEFAULT_ACCOUNT_ID, Account.telegram_user_id.is_(None)))
                    .values(telegram_user_id=telegram_user_id, label=label)
                )
                if result.rowcount == 1:
                    await session.commit()
                    logger.debug(f"account {env_index} -> row {DEFAULT_ACCOUNT_ID} (claimed migrated row)")
                    return DEFAULT_ACCOUNT_ID

            account = Account(label=label, telegram_user_id=telegram_user_id)
            session.add(account)
            await session.commit()
            logger.debug(f"account {env_index} -> row {account.id} (new)")
            return account.id

    # ========== Chat Operations ==========

    @retry_on_locked()
    async def upsert_chat(self, chat_data: dict[str, Any], *, account_id: int) -> int:
        """Insert or update a chat record.

        Only fields present in chat_data will be updated on conflict.
        This prevents the listener (which only provides basic fields)
        from overwriting is_forum/is_archived set by the backup.

        ``ref`` is deliberately absent from both the values and the update set:
        the model's Python-side default mints one on the INSERT branch, and the
        DO UPDATE branch never touches the column, so a ref is stable for the
        life of the row.
        """
        async with self.db_manager.async_session_factory() as session:
            values = {
                "account_id": account_id,
                "id": chat_data["id"],
                "type": chat_data.get("type", "unknown"),
                "title": chat_data.get("title"),
                "username": chat_data.get("username"),
                "first_name": chat_data.get("first_name"),
                "last_name": chat_data.get("last_name"),
                "phone": chat_data.get("phone"),
                "description": chat_data.get("description"),
                "participants_count": chat_data.get("participants_count"),
                "is_forum": chat_data.get("is_forum", 0),
                "is_archived": chat_data.get("is_archived", 0),
                "updated_at": utcnow_naive(),
            }

            # Build update set from only the fields explicitly provided in chat_data.
            # This prevents partial upserts (e.g. from the listener) from resetting
            # is_forum/is_archived to their defaults.
            update_set = {
                "updated_at": utcnow_naive(),
            }
            # Always update these basic metadata fields
            for field in (
                "type",
                "title",
                "username",
                "first_name",
                "last_name",
                "phone",
                "description",
                "participants_count",
            ):
                if field in chat_data:
                    update_set[field] = values[field]
            # Only update is_forum/is_archived if explicitly provided
            if "is_forum" in chat_data:
                update_set["is_forum"] = values["is_forum"]
            if "is_archived" in chat_data:
                update_set["is_archived"] = values["is_archived"]

            if self._is_sqlite:
                stmt = sqlite_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_set)
            else:
                stmt = pg_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()
            return chat_data["id"]

    async def get_all_chats(
        self,
        limit: int = None,
        offset: int = 0,
        search: str = None,
        archived: bool | None = None,
        folder_id: int | None = None,
        *,
        account_id: int | None = None,
        scope: ChatScope | None = None,
    ) -> list[dict[str, Any]]:
        """Get chats with their last message date, with optional pagination and search.

        Args:
            limit: Maximum number of chats to return
            offset: Offset for pagination
            search: Optional search query (case-insensitive, matches title/first_name/last_name/username)
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
            account_id: If set, only this account's chats (None = unscoped until phase 4)
            scope: Viewer entitlement, applied as WHERE predicates so a restricted
                viewer reads only the rows it may see. The caller must NOT
                post-filter: pushing the grant down here is what keeps limit /
                offset / COUNT honest, and what stops a one-chat viewer from
                paying for every chat in the archive.
        """
        async with self.db_manager.async_session_factory() as session:
            # Last message date, as a CORRELATED scalar subquery — one
            # idx_messages_chat_date_desc seek per chat row returned.
            #
            # It used to be `SELECT chat_id, max(date) FROM messages GROUP BY
            # chat_id` joined to chats: with no chat_id predicate the aggregate
            # could not be pruned by the LIMIT, so listing 50 chats aggregated
            # every message in the archive on every /api/chats call. Measured on
            # 1,000 chats / 1,000,000 messages: 63 ms -> 1.3 ms, and flat in
            # archive size instead of linear.
            # The account equality rides in the correlation (not only in an
            # optional filter): a chat id repeats across accounts, so without it
            # the max() would read BOTH accounts' copies of the chat.
            last_message_date = (
                select(func.max(Message.date))
                .where(and_(Message.account_id == Chat.account_id, Message.chat_id == Chat.id))
                .correlate(Chat)
                .scalar_subquery()
                .label("last_message_date")
            )

            stmt = select(Chat, last_message_date)

            # Filter by folder membership
            if folder_id is not None:
                stmt = stmt.join(
                    ChatFolderMember,
                    and_(
                        ChatFolderMember.account_id == Chat.account_id,
                        ChatFolderMember.chat_id == Chat.id,
                        ChatFolderMember.folder_id == folder_id,
                    ),
                )

            if account_id is not None:
                stmt = stmt.where(Chat.account_id == account_id)

            # Viewer entitlement, in SQL. Applied before ORDER BY/LIMIT so the
            # page, the ordering and the count all describe the same row set.
            if scope is not None:
                for predicate in scope.sql_predicates():
                    stmt = stmt.where(predicate)

            # Filter by archived status
            if archived is True:
                stmt = stmt.where(Chat.is_archived == 1)
            elif archived is False:
                stmt = stmt.where(or_(Chat.is_archived == 0, Chat.is_archived.is_(None)))

            # Apply search filter if provided
            if search:
                escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                search_pattern = f"%{escaped}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern, escape="\\"),
                        Chat.first_name.ilike(search_pattern, escape="\\"),
                        Chat.last_name.ilike(search_pattern, escape="\\"),
                        Chat.username.ilike(search_pattern, escape="\\"),
                    )
                )

            # Order by last message date, referencing the SELECT label so the
            # correlated subquery is evaluated once per row rather than twice.
            # `DESC NULLS LAST` is the message-less-chats-last rule the previous
            # `is_(None), desc()` pair spelled out. Chat.id is the tiebreaker
            # that makes the ordering TOTAL: without it every message-less chat
            # ties on NULL, and LIMIT/OFFSET may then split that tie group
            # differently on each page, so a chat could appear twice or vanish.
            stmt = stmt.order_by(nulls_last(desc("last_message_date")), Chat.id.desc())

            # Apply pagination if limit is specified
            if limit is not None:
                stmt = stmt.limit(limit).offset(offset)

            result = await session.execute(stmt)
            chats = []
            for row in result:
                chat_dict = {
                    "id": row.Chat.id,
                    "account_id": row.Chat.account_id,
                    "ref": row.Chat.ref,
                    "type": row.Chat.type,
                    "title": row.Chat.title,
                    "username": row.Chat.username,
                    "first_name": row.Chat.first_name,
                    "last_name": row.Chat.last_name,
                    "phone": row.Chat.phone,
                    "description": row.Chat.description,
                    "participants_count": row.Chat.participants_count,
                    "is_forum": row.Chat.is_forum,
                    "is_archived": row.Chat.is_archived,
                    "last_synced_message_id": row.Chat.last_synced_message_id,
                    "created_at": row.Chat.created_at,
                    "updated_at": row.Chat.updated_at,
                    "last_message_date": row.last_message_date,
                }
                chats.append(chat_dict)
            return chats

    async def get_visible_chat_ids(self, scope: ChatScope) -> set[int]:
        """Just the chat ids a scope selects — no row build, no date subquery.

        ``get_all_chats`` attaches a correlated ``MAX(messages.date)`` per row,
        which is exactly what the callers of this (folder counts, cached stats)
        throw away. A grant can be as wide as a whole account, so paying that
        subquery per chat to collect ids is waste that grows with the archive.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat.id)
            for predicate in scope.sql_predicates():
                stmt = stmt.where(predicate)
            result = await session.execute(stmt)
            return {row[0] for row in result}

    async def get_chat_count(
        self,
        search: str = None,
        archived: bool | None = None,
        folder_id: int | None = None,
        *,
        account_id: int | None = None,
        scope: ChatScope | None = None,
    ) -> int:
        """Get total number of chats (fast count for pagination).

        Args:
            search: Optional search query to filter count
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
            account_id: If set, only this account's chats (None = unscoped until phase 4)
            scope: Viewer entitlement (see get_all_chats). Must be the SAME scope the
                matching get_all_chats call used, or ``total`` and the page disagree.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.count(Chat.id))

            if folder_id is not None:
                stmt = stmt.join(
                    ChatFolderMember,
                    and_(
                        ChatFolderMember.account_id == Chat.account_id,
                        ChatFolderMember.chat_id == Chat.id,
                        ChatFolderMember.folder_id == folder_id,
                    ),
                )

            if account_id is not None:
                stmt = stmt.where(Chat.account_id == account_id)

            if scope is not None:
                for predicate in scope.sql_predicates():
                    stmt = stmt.where(predicate)

            if archived is True:
                stmt = stmt.where(Chat.is_archived == 1)
            elif archived is False:
                stmt = stmt.where(or_(Chat.is_archived == 0, Chat.is_archived.is_(None)))

            if search:
                escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                search_pattern = f"%{escaped}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern, escape="\\"),
                        Chat.first_name.ilike(search_pattern, escape="\\"),
                        Chat.last_name.ilike(search_pattern, escape="\\"),
                        Chat.username.ilike(search_pattern, escape="\\"),
                    )
                )

            result = await session.execute(stmt)
            return result.scalar() or 0

    # ========== User Operations ==========

    @retry_on_locked()
    async def upsert_user(self, user_data: dict[str, Any]) -> None:
        """Insert or update a user record.

        Only keys PRESENT in ``user_data`` reach the conflict update — the
        importer knows only {id, first_name}, and letting its absent keys
        write NULLs erased the username/last_name/phone the live capture had
        recorded (the same present-keys contract upsert_chat already keeps).
        Callers that observe a removal (backup/listener build every key
        explicitly) still clear a column by passing the key with None.
        """
        async with self.db_manager.async_session_factory() as session:
            values: dict[str, Any] = {"id": user_data["id"], "updated_at": utcnow_naive()}
            for key in ("username", "first_name", "last_name", "phone"):
                if key in user_data:
                    values[key] = user_data.get(key)
            if "is_bot" in user_data:
                values["is_bot"] = 1 if user_data.get("is_bot") else 0

            insert_fn = sqlite_insert if self._is_sqlite else pg_insert
            stmt = insert_fn(User).values(**values)
            update_set = {key: getattr(stmt.excluded, key) for key in values if key != "id"}
            stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    # ========== Message Operations ==========

    @retry_on_locked()
    async def insert_message(self, message_data: dict[str, Any], *, account_id: int) -> None:
        """Insert a message record.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        async with self.db_manager.async_session_factory() as session:
            await self._insert_or_update_message(session, message_data, account_id=account_id)
            await session.commit()

    @retry_on_locked()
    async def insert_messages_batch(self, messages_data: list[dict[str, Any]], *, account_id: int) -> None:
        """Insert multiple message records in a single transaction.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        if not messages_data:
            return

        async with self.db_manager.async_session_factory() as session:
            for m in messages_data:
                await self._insert_or_update_message(session, m, account_id=account_id)

            await session.commit()

    async def get_messages_by_date_range(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        *,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get messages within a date range (None account_id = unscoped until phase 4)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message)

            conditions = []
            if account_id is not None:
                conditions.append(Message.account_id == account_id)
            if chat_id:
                conditions.append(Message.chat_id == chat_id)
            if start_date:
                conditions.append(Message.date >= start_date)
            if end_date:
                conditions.append(Message.date <= end_date)

            if conditions:
                stmt = stmt.where(and_(*conditions))

            stmt = stmt.order_by(Message.date.asc())

            result = await session.execute(stmt)
            return [self._message_to_dict(m) for m in result.scalars()]

    async def find_message_by_date(
        self, chat_id: int, target_date: datetime, *, account_id: int | None = None
    ) -> dict[str, Any] | None:
        """Find the first message on or after a specific date (None account_id = unscoped until phase 4)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message)
                .where(and_(Message.chat_id == chat_id, Message.date >= target_date))
                .order_by(Message.date.asc())
                .limit(1)
            )
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)
            result = await session.execute(stmt)
            message = result.scalar_one_or_none()
            return self._message_to_dict(message) if message else None

    async def get_messages_sync_data(self, chat_id: int, *, account_id: int) -> dict[int, str | None]:
        """Get message IDs and their edit dates for sync checking."""
        async with self.db_manager.async_session_factory() as session:
            # Exclude soft-deleted rows so sync doesn't re-check them. The is_(None) arm is
            # defensive (is_deleted is NOT NULL with server_default 0) and mirrors is_archived.
            stmt = select(Message.id, Message.edit_date).where(
                and_(
                    Message.account_id == account_id,
                    Message.chat_id == chat_id,
                    or_(Message.is_deleted == 0, Message.is_deleted.is_(None)),
                )
            )
            result = await session.execute(stmt)
            return {row.id: row.edit_date for row in result}

    async def get_message_ids_since(self, chat_id: int, cutoff: datetime, limit: int, *, account_id: int) -> list[int]:
        """Return the newest message IDs in a chat dated at or after ``cutoff`` (#221).

        Used by the bounded reaction re-sweep to recover self-reactions Telegram
        never pushed to this session. Newest-first (highest id) and capped at
        ``limit`` so the caller re-checks the most recent window at a fixed cost.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id)
                .where(and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.date >= cutoff))
                .order_by(Message.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [row.id for row in result]

    async def get_chat_id_for_message(self, message_id: int, *, account_id: int) -> int | None:
        """
        Look up the chat_id for a message by its ID, within one account.

        Used when Telegram sends deletion events without chat_id — the event
        arrived on one account's session, so only that account's rows are
        candidates (idx_messages_account_msgid serves this seek).
        Note: Message IDs are only unique within a chat, so this may return
        multiple results. Returns the first match.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.chat_id).where(and_(Message.account_id == account_id, Message.id == message_id)).limit(1)
            )
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    async def _deletion_snapshot(self, session, account_id: int, chat_id: int, message_id: int) -> dict | None:
        """Snapshot the fields the event webhook needs, inside the deleting transaction.

        Runs before the row is destroyed (hard delete) or tombstoned (soft
        delete), so deleted text is available in BOTH deletion modes. Returns
        None when the message was never archived. media_type comes from the
        media table because Message lost its media columns in v6.0.0.

        The row is locked (FOR UPDATE; a no-op on SQLite, whose writers
        serialize anyway) so concurrent deletions of the same message
        serialize against this snapshot: the loser re-reads the committed
        state (tombstoned, or gone) instead of also seeing is_deleted=0 and
        firing a duplicate message_deleted webhook.
        """
        result = await session.execute(
            select(Message)
            .where(and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id))
            .with_for_update()
        )
        message = result.scalar_one_or_none()
        if message is None:
            return None
        media_result = await session.execute(
            select(Media.type)
            .where(and_(Media.account_id == account_id, Media.chat_id == chat_id, Media.message_id == message_id))
            .order_by(Media.id)
            .limit(1)
        )
        media_row = media_result.first()
        return {
            "text": message.text,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "date": message.date,
            "is_deleted": message.is_deleted,
            "media_type": media_row[0] if media_row else None,
        }

    @retry_on_locked()
    async def delete_message(self, chat_id: int, message_id: int, *, account_id: int) -> dict | None:
        """Delete a specific message and its media.

        Returns a pre-deletion snapshot of the row (see _deletion_snapshot) so
        the listener can fire the event webhook with the destroyed content, or
        None when the message was never archived. The four DELETEs still run
        unconditionally — orphan-cleanup behavior is unchanged.
        """
        async with self.db_manager.async_session_factory() as session:
            snapshot = await self._deletion_snapshot(session, account_id, chat_id, message_id)
            # Delete previous versions
            await session.execute(
                delete(MessageVersion).where(
                    and_(
                        MessageVersion.account_id == account_id,
                        MessageVersion.chat_id == chat_id,
                        MessageVersion.message_id == message_id,
                    )
                )
            )
            # Delete associated media
            await session.execute(
                delete(Media).where(
                    and_(Media.account_id == account_id, Media.chat_id == chat_id, Media.message_id == message_id)
                )
            )
            # Delete reactions
            await session.execute(
                delete(Reaction).where(
                    and_(
                        Reaction.account_id == account_id,
                        Reaction.chat_id == chat_id,
                        Reaction.message_id == message_id,
                    )
                )
            )
            # Delete the message
            await session.execute(
                delete(Message).where(
                    and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id)
                )
            )
            await session.commit()
            logger.debug(f"Deleted message {message_id}")
            return snapshot

    @retry_on_locked()
    async def mark_message_deleted(
        self, chat_id: int, message_id: int, deleted_at: datetime | None = None, *, account_id: int
    ) -> dict | None:
        """Mark a message as deleted on Telegram while keeping archive content.

        Returns a pre-tombstone snapshot of the row (see _deletion_snapshot),
        or None when the message was never archived. The snapshot's is_deleted
        reflects the state BEFORE this call, so callers can detect a re-mark
        and keep webhook delivery exactly-once; the idempotent UPDATE and
        deleted_at coalesce semantics are unchanged.
        """
        deleted_at = _strip_tz(deleted_at) or utcnow_naive()
        async with self.db_manager.async_session_factory() as session:
            snapshot = await self._deletion_snapshot(session, account_id, chat_id, message_id)
            result = await session.execute(
                update(Message)
                .where(and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id))
                .values(
                    is_deleted=1,
                    deleted_at=func.coalesce(Message.deleted_at, deleted_at),
                )
            )
            await session.commit()
            if result.rowcount:
                logger.debug(f"Marked message {message_id} as deleted")
            else:
                logger.debug(f"Soft-delete no-op: message {message_id} not in archive")
            return snapshot

    async def resolve_message_chat_id(self, message_id: int, *, account_id: int) -> int | None:
        """
        Find which chat a peerless event's message belongs to, within one account.

        Returns the chat_id if found in exactly one of the account's chats.
        Returns None if not found or ambiguous (same ID in multiple chats).
        Telegram message IDs are only unique within a chat — and another
        account's rows must never make this account's lookup ambiguous, nor
        resolve a deletion onto a chat this account never archived.

        Channels and supergroups are excluded outright: Telegram omits the
        peer exactly and only for the common message box (private chats and
        basic groups); channel deletions always arrive with the channel id.
        The two id spaces are disjoint, so a peerless event can never
        legitimately name a -100… chat — and matching one there tombstoned a
        message that was never deleted (9t6.5.4).
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Message.chat_id).where(
                    and_(
                        Message.account_id == account_id,
                        Message.id == message_id,
                        # Marked channel/supergroup ids live below this ceiling.
                        Message.chat_id > SUPERGROUP_ID_CEILING,
                    )
                )
            )
            chat_ids = [row[0] for row in result.fetchall()]

            if len(chat_ids) == 1:
                return chat_ids[0]
            if len(chat_ids) > 1:
                logger.warning(f"Message {message_id} found in {len(chat_ids)} chats, skipping ambiguous deletion")
            return None

    async def get_message_sender_id(
        self, chat_id: int, message_id: int, *, account_id: int | None = None
    ) -> int | None:
        """Sender of one message, or None when the message is absent or senderless.

        Phase 4: the ref-addressed sender-avatar route
        (``/media/avatar/{chat_ref}/{message_id}``) resolves the sender through
        the message so no user id has to appear in the URL — for a private chat
        the peer's user id IS the chat id, which must stay out of access logs.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.sender_id).where(and_(Message.chat_id == chat_id, Message.id == message_id))
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    @retry_on_locked()
    async def update_message_text(
        self,
        chat_id: int,
        message_id: int,
        new_text: str,
        edit_date: datetime | None,
        *,
        account_id: int,
        entities: list | None = None,
        update_entities: bool = False,
    ) -> tuple[str, dict | None]:
        """Update a message's text and edit_date.

        Returns ``(outcome, prior)`` so callers can keep honest counters and
        only broadcast edits that actually changed the archive. ``outcome`` is
        ``"applied"`` | ``"noop"`` (already current / older evidence) |
        ``"not_found"`` (message not archived). ``prior`` is a snapshot of the
        superseded row ({text, sender_id, sender_name}) on "applied", captured
        in the same transaction so the event webhook gets race-free old text;
        None otherwise.
        """
        edit_date = _strip_tz(edit_date)
        async with self.db_manager.async_session_factory() as session:
            message = await self._load_message_for_update(session, account_id, chat_id, message_id)
            if message is None:
                logger.debug("Edit no-op: message not found in archive")
                return "not_found", None

            if not self._should_apply_edit_text(message, new_text, edit_date):
                # Formatting-only edits arrive with UNCHANGED text but different
                # entities. Merge them silently — no edit_date bump, no version,
                # no webhook — so formatting stays current without the phantom
                # "edited" marker #219 removed.
                if update_entities and self._merge_raw_data_entities(message, entities):
                    await session.execute(
                        update(Message)
                        .where(
                            and_(
                                Message.account_id == account_id,
                                Message.chat_id == chat_id,
                                Message.id == message_id,
                            )
                        )
                        .values(raw_data=message.raw_data)
                    )
                    await session.commit()
                    logger.debug("Edit no-op text, entities refreshed")
                else:
                    logger.debug("Edit no-op: message already current")
                return "noop", None

            prior = {"text": message.text, "sender_id": message.sender_id, "sender_name": message.sender_name}
            if message.text != new_text:
                await self._record_message_version(
                    session=session,
                    account_id=account_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=message.text,
                    date=self._message_version_date(message),
                )
            await session.execute(
                update(Message)
                .where(and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id))
                .values(text=new_text, edit_date=edit_date)
            )
            if update_entities and self._merge_raw_data_entities(message, entities):
                await session.execute(
                    update(Message)
                    .where(
                        and_(
                            Message.account_id == account_id,
                            Message.chat_id == chat_id,
                            Message.id == message_id,
                        )
                    )
                    .values(raw_data=message.raw_data)
                )
            await session.commit()
            logger.debug("Updated archived message text")
            return "applied", prior

    def _merge_raw_data_entities(self, message: Message, entities: list | None) -> bool:
        """Set or drop raw_data["entities"] on the loaded row; True if it changed.

        raw_data is a JSON string column, so the merge round-trips through
        json; a row whose raw_data is unparseable is left untouched (never
        destroy unrelated capture payloads for a formatting refresh).
        """
        try:
            raw = json.loads(message.raw_data) if message.raw_data else {}
        except ValueError, TypeError:
            return False
        if not isinstance(raw, dict):
            return False
        if raw.get("entities") == entities and (entities is not None or "entities" not in raw):
            return False
        if entities is None:
            if "entities" not in raw:
                return False
            raw.pop("entities")
        else:
            raw["entities"] = entities
        message.raw_data = json.dumps(raw)
        return True

    async def sender_has_message_in_chats(
        self, sender_id: int, chat_ids: Iterable[int], *, account_id: int | None = None
    ) -> bool:
        """Return True if sender_id authored at least one message in any of chat_ids.

        SELECT-only membership probe used by the media ACL to decide whether a
        viewer may fetch a member's avatar: a user avatar is served iff the
        viewer can see a chat in which that user has spoken. Never logs ids.
        """
        chat_id_list = list(chat_ids)
        if not chat_id_list:
            return False
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id)
                .where(and_(Message.sender_id == sender_id, Message.chat_id.in_(chat_id_list)))
                .limit(1)
            )
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)
            result = await session.execute(stmt)
            return result.first() is not None

    async def backfill_is_outgoing(self, owner_id: int, *, account_id: int) -> None:
        """Backfill is_outgoing flag for messages sent by the owner.

        Scoped to the account whose owner ``owner_id`` is: the same person can
        be a mere participant in the other account's copy of a shared chat, and
        those rows are genuinely not outgoing there.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(
                    and_(
                        Message.account_id == account_id,
                        Message.sender_id == owner_id,
                        or_(Message.is_outgoing == 0, Message.is_outgoing.is_(None)),
                    )
                )
                .values(is_outgoing=1)
            )
            await session.commit()
            if result.rowcount > 0:
                logger.info(f"Backfilled is_outgoing=1 for {result.rowcount} messages from owner {owner_id}")

    def _message_to_dict(self, message: Message) -> dict[str, Any]:
        """Convert Message model to dictionary.

        v6.0.0: media_type, media_id, media_path removed - use media_items relationship.
        """
        is_deleted = getattr(message, "is_deleted", 0)
        if not isinstance(is_deleted, int):
            is_deleted = 0
        deleted_at = getattr(message, "deleted_at", None)
        if not isinstance(deleted_at, datetime):
            deleted_at = None
        sender_name = getattr(message, "sender_name", None)
        sender_name = sender_name.strip() if _is_nonblank_text(sender_name) else None

        return {
            "id": message.id,
            "chat_id": message.chat_id,
            "sender_id": message.sender_id,
            "sender_name": sender_name,
            "date": message.date,
            "text": message.text,
            "reply_to_msg_id": message.reply_to_msg_id,
            "reply_to_top_id": message.reply_to_top_id,
            "reply_to_text": message.reply_to_text,
            "forward_from_id": message.forward_from_id,
            "edit_date": message.edit_date,
            "raw_data": message.raw_data,
            "created_at": message.created_at,
            "is_outgoing": message.is_outgoing,
            "is_pinned": message.is_pinned,
            "is_deleted": int(is_deleted),
            "deleted_at": deleted_at,
        }

    def _message_version_to_dict(self, row: MessageVersion) -> dict[str, Any]:
        return {
            "chat_id": row.chat_id,
            "message_id": row.message_id,
            "text": row.text,
            "date": row.date,
        }

    async def get_message_versions(
        self, chat_id: int, message_id: int, limit: int = 100, *, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Get preserved previous text versions for a message (None account_id = unscoped until phase 4)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(MessageVersion)
                .where(and_(MessageVersion.chat_id == chat_id, MessageVersion.message_id == message_id))
                .order_by(MessageVersion.date.desc(), MessageVersion.id.desc())
                .limit(limit)
            )
            if account_id is not None:
                stmt = stmt.where(MessageVersion.account_id == account_id)
            result = await session.execute(stmt)
            return [self._message_version_to_dict(row) for row in result.scalars()]

    def _message_versions_query(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        account_id: int | None = None,
    ):
        # No join to messages: versions already carry (chat_id, message_id), and
        # referential integrity is owned by the explicit deletes in
        # delete_message / delete_chat_and_related_data.
        stmt = select(MessageVersion)

        conditions = []
        if account_id is not None:
            conditions.append(MessageVersion.account_id == account_id)
        if chat_id is not None:
            conditions.append(MessageVersion.chat_id == chat_id)
        if start_date:
            conditions.append(MessageVersion.date >= start_date)
        if end_date:
            conditions.append(MessageVersion.date <= end_date)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        return stmt.order_by(
            MessageVersion.chat_id.asc(),
            MessageVersion.message_id.asc(),
            MessageVersion.date.asc(),
            MessageVersion.id.asc(),
        )

    async def get_recent_changes(
        self,
        *,
        since: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
        scope: ChatScope | None = None,
    ) -> list[dict[str, Any]]:
        """The what-changed feed: deletions and edits the archive captured.

        The archive's differentiator is that it KEEPS what disappeared; this
        is the query that finally lists it. Two streams share one shape:

        * ``deleted`` — soft-deleted messages (``is_deleted=1``), dated by
          ``deleted_at``, carrying the text the archive kept.
        * ``edited`` — ``message_versions`` rows, dated by ``captured_at``
          (when the archive observed the supersession), carrying the old text
          plus the message's CURRENT text.

        Newest first. ``before`` is an exclusive keyset cursor over the
        per-row date: pass the last row's ``date`` back to page. Rows sharing
        that exact microsecond with the cursor are skipped — this is a review
        feed, not an export, and the export path is the lossless one.
        Entitlements ride ``scope.sql_predicates()`` against the joined chat
        row, the same compiled rules as the chat list — a restricted viewer's
        feed touches only their rows. Hard deletions cannot appear: their
        content no longer exists (DELETION_MODE=soft is what feeds this).
        """
        per_stream = max(1, min(int(limit), 200))

        def _chat_fields(row) -> dict[str, Any]:
            name = row.title or " ".join(p for p in (row.first_name, row.last_name) if p) or row.username or ""
            return {"ref": row.ref, "title": name, "type": row.chat_type}

        async with self.db_manager.async_session_factory() as session:
            deleted_stmt = (
                select(
                    Message.id.label("message_id"),
                    Message.deleted_at.label("date"),
                    Message.text,
                    Message.sender_name,
                    Chat.ref,
                    Chat.title,
                    Chat.first_name,
                    Chat.last_name,
                    Chat.username,
                    Chat.type.label("chat_type"),
                )
                .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
                .where(Message.is_deleted == 1, Message.deleted_at.isnot(None))
            )
            edited_stmt = (
                select(
                    MessageVersion.message_id,
                    MessageVersion.captured_at.label("date"),
                    MessageVersion.text.label("old_text"),
                    Message.text.label("new_text"),
                    Message.sender_name,
                    Chat.ref,
                    Chat.title,
                    Chat.first_name,
                    Chat.last_name,
                    Chat.username,
                    Chat.type.label("chat_type"),
                )
                .join(
                    Message,
                    and_(
                        Message.account_id == MessageVersion.account_id,
                        Message.chat_id == MessageVersion.chat_id,
                        Message.id == MessageVersion.message_id,
                    ),
                )
                .join(Chat, and_(Chat.account_id == MessageVersion.account_id, Chat.id == MessageVersion.chat_id))
            )
            if since is not None:
                deleted_stmt = deleted_stmt.where(Message.deleted_at >= since)
                edited_stmt = edited_stmt.where(MessageVersion.captured_at >= since)
            if before is not None:
                deleted_stmt = deleted_stmt.where(Message.deleted_at < before)
                edited_stmt = edited_stmt.where(MessageVersion.captured_at < before)
            if scope is not None:
                for predicate in scope.sql_predicates():
                    deleted_stmt = deleted_stmt.where(predicate)
                    edited_stmt = edited_stmt.where(predicate)
            deleted_stmt = deleted_stmt.order_by(Message.deleted_at.desc()).limit(per_stream)
            edited_stmt = edited_stmt.order_by(MessageVersion.captured_at.desc()).limit(per_stream)

            changes: list[dict[str, Any]] = []
            for row in (await session.execute(deleted_stmt)).all():
                changes.append(
                    {
                        "kind": "deleted",
                        "date": row.date.isoformat() if row.date else None,
                        "chat": _chat_fields(row),
                        "message_id": row.message_id,
                        "sender_name": row.sender_name,
                        "text": row.text,
                    }
                )
            for row in (await session.execute(edited_stmt)).all():
                changes.append(
                    {
                        "kind": "edited",
                        "date": row.date.isoformat() if row.date else None,
                        "chat": _chat_fields(row),
                        "message_id": row.message_id,
                        "sender_name": row.sender_name,
                        "old_text": row.old_text,
                        "new_text": row.new_text,
                    }
                )
            changes.sort(key=lambda c: c["date"] or "", reverse=True)
            return changes[:per_stream]

    async def get_message_versions_by_date_range(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        *,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get previous message versions by version date/chat filter (None account_id = unscoped until phase 4)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(self._message_versions_query(chat_id, start_date, end_date, account_id))
            return [self._message_version_to_dict(row) for row in result.scalars()]

    async def iter_message_versions_for_export(
        self,
        chat_id: int,
        *,
        account_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ):
        """Stream a chat's message versions one by one (async generator).

        Mirrors get_messages_for_export so the export endpoint never
        materializes an entire edit history in memory. The optional window
        uses the export contract (>= from, < to) — deliberately NOT the shared
        query's inclusive end_date, whose contract other callers own.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = self._message_versions_query(chat_id, account_id=account_id)
            if from_date is not None:
                stmt = stmt.where(MessageVersion.date >= from_date)
            if to_date is not None:
                stmt = stmt.where(MessageVersion.date < to_date)
            result = await session.stream(stmt)
            async for row in result.scalars():
                yield self._message_version_to_dict(row)

    async def get_chat_stats(self, chat_id: int, *, account_id: int | None = None) -> dict[str, Any]:
        """Get statistics for a specific chat (message count, media count, total size).

        None account_id = unscoped until phase 4.

        Returns:
            Dict with keys: messages, media_files, total_size_bytes, first_message_date, last_message_date
        """
        msg_where = [Message.chat_id == chat_id]
        media_where = [Media.chat_id == chat_id]
        if account_id is not None:
            msg_where.append(Message.account_id == account_id)
            media_where.append(Media.account_id == account_id)
        async with self.db_manager.async_session_factory() as session:
            # Message count
            msg_result = await session.execute(select(func.count(Message.id)).where(and_(*msg_where)))
            message_count = msg_result.scalar() or 0

            # Media count and total size
            media_result = await session.execute(
                select(func.count(Media.id), func.coalesce(func.sum(Media.file_size), 0)).where(and_(*media_where))
            )
            media_row = media_result.one()
            media_count = media_row[0] or 0
            total_size = media_row[1] or 0

            # First and last message dates
            date_result = await session.execute(
                select(func.min(Message.date), func.max(Message.date)).where(and_(*msg_where))
            )
            date_row = date_result.one()
            first_message = date_row[0]
            last_message = date_row[1]

            return {
                "chat_id": chat_id,
                "messages": int(message_count),
                "media_files": int(media_count),
                "total_size_bytes": int(total_size),
                "total_size_mb": round(total_size / (1024 * 1024), 2) if total_size else 0,
                "first_message_date": first_message.isoformat() if first_message else None,
                "last_message_date": last_message.isoformat() if last_message else None,
            }

    # ========== Media Operations ==========

    @retry_on_locked()
    async def insert_media(self, media_data: dict[str, Any], *, account_id: int) -> None:
        """Insert (or upsert) a media file record.

        Contract for the ``downloaded`` key: include it whenever the caller
        actually observed the download outcome (True after a successful write,
        False after a skip/failure it is willing to have retried), and OMIT it
        when the caller cannot know whether a file is on disk. An omitted key
        means "leave the stored flag alone" on conflict and 0 on a fresh insert —
        see the comment on the conflict clause below.
        """
        async with self.db_manager.async_session_factory() as session:
            values = {
                "account_id": account_id,
                "id": media_data["id"],
                "message_id": media_data.get("message_id"),
                "chat_id": media_data.get("chat_id"),
                "type": media_data["type"],
                "file_name": media_data.get("file_name"),
                "file_path": media_data.get("file_path"),
                "file_size": media_data.get("file_size"),
                "mime_type": media_data.get("mime_type"),
                "width": media_data.get("width"),
                "height": media_data.get("height"),
                "duration": media_data.get("duration"),
                "content_hash": media_data.get("content_hash"),
                "downloaded": 1 if media_data.get("downloaded") else 0,
                "download_date": media_data.get("download_date"),
            }

            stmt = sqlite_insert(Media).values(**values) if self._is_sqlite else pg_insert(Media).values(**values)

            # On conflict, a writer that has NO value for a column must not blank out
            # what an earlier writer already stored (#263). Both halves of the row
            # are affected, so both are COALESCEd:
            #   - the metadata columns, when an ingest path could not read the
            #     attributes off the Telethon object;
            #   - the file-identity columns, because ``_process_media`` returns a
            #     value-less row for an over-size skip and for a download error —
            #     that row used to null the file_path/file_name/content_hash/
            #     download_date of a file that is still on disk.
            # COALESCE only falls back on NULL, so a real value still overwrites a
            # real value: a re-download to a new path DOES update file_path.
            # (``mark_media_for_redownload`` is a separate UPDATE that clears these
            # deliberately; it currently has no production caller, only tests.)
            update_values = dict(values)
            for column in (
                "file_name",
                "file_path",
                "file_size",
                "mime_type",
                "width",
                "height",
                "duration",
                "content_hash",
                "download_date",
            ):
                update_values[column] = func.coalesce(getattr(stmt.excluded, column), getattr(Media, column))
            # ``downloaded`` is a flag, not a value: 0 is a real value, so COALESCE
            # cannot express "this writer has no opinion" for it. The KEY'S PRESENCE
            # in ``media_data`` does instead:
            #   - present -> the writer observed the outcome, so write it. A failed
            #     download therefore sets 0 again and the row returns to
            #     ``get_pending_media_downloads``, which ``_retry_pending_media_downloads``
            #     drains on EVERY backup cycle. That is the only always-on recovery
            #     path: ``TelegramBackup._verify_and_redownload_media`` (the disk-stat
            #     scan) runs only when VERIFY_MEDIA is on, and it defaults to false.
            #     Pinning the flag at 1 stranded such a row forever, pointing at a
            #     file that is gone.
            #   - absent -> the writer knows nothing about what is on disk, so keep
            #     the stored flag. ``_process_media``'s over-size skip is the one
            #     such writer: the file may already be on disk from a run with a
            #     higher MAX_MEDIA_SIZE, and flipping it to 0 would hide it from the
            #     gallery (``get_media_paginated`` filters ``downloaded == 1``)
            #     without ever retrying it (``get_pending_media_downloads`` excludes
            #     its over-limit file_size).
            # A fresh INSERT still lands 0 for an absent key — nothing is downloaded.
            if "downloaded" not in media_data:
                update_values["downloaded"] = Media.downloaded
            stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_values)

            await session.execute(stmt)
            await session.commit()

    async def find_media_by_content_hash(self, content_hash: str, *, account_id: int) -> dict[str, Any] | None:
        """Find an existing downloaded media record with the given SHA-256 content hash.

        Account-scoped on purpose: shared-store dedup must only reuse a blob the
        SAME account's rows reference, so no account's media ever points at
        content that exists solely under another account's lifecycle.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media)
                .where(and_(Media.account_id == account_id, Media.content_hash == content_hash, Media.downloaded == 1))
                .limit(1)
            )
            result = await session.execute(stmt)
            media = result.scalar_one_or_none()
            if media is None:
                return None
            return {
                "file_path": media.file_path,
                "file_name": media.file_name,
                "content_hash": media.content_hash,
            }

    async def get_media_for_chat(self, chat_id: int, *, account_id: int) -> list[dict[str, Any]]:
        """
        Get all media records for one account's copy of a chat.

        Feeds the chat-cleanup path that deletes files from disk, so it must
        never surface another account's rows.

        Args:
            chat_id: Chat identifier

        Returns:
            List of media records with file paths and metadata
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(and_(Media.account_id == account_id, Media.chat_id == chat_id))
            result = await session.execute(stmt)
            media_records = result.scalars().all()

            return [
                {
                    "id": m.id,
                    "message_id": m.message_id,
                    "chat_id": m.chat_id,
                    "type": m.type,
                    "file_path": m.file_path,
                    "file_size": m.file_size,
                    "downloaded": m.downloaded,
                }
                for m in media_records
            ]

    async def get_media_paginated(
        self,
        chat_id: int,
        media_types: list[str] | None = None,
        limit: int = 50,
        before_id: str | None = None,
        after_id: str | None = None,
        *,
        before_key: tuple[int, str] | None = None,
        after_key: tuple[int, str] | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Get paginated media records for a chat with cursor-based pagination.

        A cursor may be given either as a storage id (``before_id``/``after_id``)
        or as the natural key ``(message_id, type)`` (``before_key``/``after_key``).
        The gallery URL carries the natural key, because an imported row's
        storage id is not derivable from it (#423) — resolving the cursor by
        column keeps 'load more' working on archives built from an import
        instead of dead-ending at the first imported item.

        ``account_id=None`` is unscoped until phase 4. The media↔message joins
        below carry the account equality UNCONDITIONALLY: a ``{chat}_{msg}_{type}``
        media id repeats across accounts, so joining without it would multiply
        rows even for a caller that asked for no scoping.

        ``before_id``/``after_id`` are opaque ``Media.id`` tokens (the gallery
        round-trips the composite ``{chat}_{msg}_{type}`` string), but each is resolved
        to the pair (Media.message_id, Media.id) before use. ``Media.id`` alone sorts
        lexically, so rows sharing a message came back as 9, 99, 98, ..., 8, 89 —
        numerically meaningless; ``Media.message_id`` is an integer and orders
        correctly. Ordering on the media pair rather than on ``Message.date`` is what
        lets one covering index (``idx_media_gallery``) serve filter, cursor predicate
        and ORDER BY in a single seek: per-chat Telegram message ids are assigned
        monotonically in time (the same fact the message ``before_id`` cursor relies
        on), so the pair yields the identical chronological order without dragging
        the messages join into the sort.

        Two directions, one cursor shape:

        - ``before_id`` walks BACKWARD (older): predicate ``<`` on the triple, ordered
          DESC, page returned newest-first. ``has_more`` means "more OLDER rows exist".
        - ``after_id`` walks FORWARD (newer): predicate ``>`` on the triple, ordered
          ASC, page returned oldest-first. ``has_more`` means "more NEWER rows exist".
          The audio queue uses this to extend forward on demand instead of
          pre-collecting every item newer than the playing track (#266).

        The two are MUTUALLY EXCLUSIVE: supplying both is a caller bug (there is no
        coherent page "before X and after Y" in this API) and raises ``ValueError``.

        The ORDER BY and the cursor predicate MUST stay the same pair, in the same
        direction: that identity is what guarantees a full walk yields every row
        exactly once (no skips, no duplicates). Change one and you must change the
        other — in both directions.

        The cursor resolution is scoped to ``chat_id``, forward as well as backward.
        Unscoped, a caller could pass ANOTHER chat's media id and have that row's
        timestamp shape this chat's result window — a cross-chat existence/timestamp
        oracle for a chat the caller cannot read. A token that does not belong to
        ``chat_id`` is indistinguishable from a deleted one and returns an EMPTY page
        (never a full first page) in either direction, so neither the existence nor the
        date of a foreign row can be inferred from the response, and a client can treat
        both directions alike.
        """
        if before_id and after_id:
            raise ValueError("before_id and after_id are mutually exclusive")

        forward = bool(after_id or after_key)
        cursor_token = after_id if forward else before_id
        cursor_key = after_key if forward else before_key

        async with self.db_manager.async_session_factory() as session:
            # Two-step page: pick the page's Media.ids from a NARROW statement
            # (the two sort keys, nothing else), then hydrate only those rows.
            # The key statement touches only media columns — filter (chat_id,
            # downloaded), cursor predicate and ORDER BY are all on the media
            # pair — so idx_media_gallery (chat_id, downloaded, message_id, id)
            # serves the whole page as one index seek: O(page size) after the
            # cursor, no temp sort, no messages join until hydration.
            key_stmt = select(Media.id.label("page_media_id"))
            key_stmt = key_stmt.where(and_(Media.chat_id == chat_id, Media.downloaded == 1))
            if account_id is not None:
                key_stmt = key_stmt.where(Media.account_id == account_id)

            if media_types:
                key_stmt = key_stmt.where(Media.type.in_(media_types))

            if cursor_token or cursor_key:
                cursor_match = (
                    Media.id == cursor_token
                    if cursor_token
                    else and_(Media.message_id == cursor_key[0], Media.type == cursor_key[1])
                )
                cursor_stmt = select(Media.id, Media.message_id).where(and_(cursor_match, Media.chat_id == chat_id))
                if account_id is not None:
                    cursor_stmt = cursor_stmt.where(Media.account_id == account_id)
                # A natural key names ONE row for every archive except those
                # holding the duplicate class #310 could leave behind: an import
                # row and a sweep row sharing a message and a type (documented at
                # :3459). There it names two, so it identifies a GROUP, and the
                # cursor has to clear the whole group -- resolve it to the twin the
                # walk reaches LAST, so the keyset predicate steps past both.
                #
                # Resolving to the first twin instead makes the page end on a
                # cursor that resolves back to a row it already passed, and the
                # walk stalls on that item forever instead of reaching older media.
                #
                # Skipping the second twin loses nothing a viewer can reach: both
                # carry the same {message_id}_{type} item id and the same media
                # URL, and that URL resolves through get_media_for_message to one
                # canonical row. They are one item in the gallery, twice in the
                # table.
                cursor_stmt = cursor_stmt.order_by(Media.id.desc() if forward else Media.id.asc())
                cursor_result = await session.execute(cursor_stmt)
                # first(), not one_or_none(): unscoped (account_id=None) calls
                # can match BOTH accounts' copies of the same media id, and the
                # copies share the same (message_id, id) pair — any matching
                # row resolves the cursor identically, while one_or_none()
                # would raise MultipleResultsFound on exactly that duplicate.
                cursor_row = cursor_result.first()
                if cursor_row is None:
                    return {"items": [], "has_more": False}
                cursor_media_id, cursor_message_id = cursor_row
                if forward:
                    key_stmt = key_stmt.where(
                        or_(
                            Media.message_id > cursor_message_id,
                            and_(
                                Media.message_id == cursor_message_id,
                                Media.id > cursor_media_id,
                            ),
                        )
                    )
                else:
                    key_stmt = key_stmt.where(
                        or_(
                            Media.message_id < cursor_message_id,
                            and_(
                                Media.message_id == cursor_message_id,
                                Media.id < cursor_media_id,
                            ),
                        )
                    )

            if forward:
                order_by = (Media.message_id.asc(), Media.id.asc())
            else:
                order_by = (Media.message_id.desc(), Media.id.desc())

            page_keys = key_stmt.add_columns(Media.account_id.label("page_account_id")).order_by(*order_by)
            page_keys = page_keys.limit(limit + 1).subquery()
            stmt = (
                select(
                    Media,
                    Message.date,
                    Message.sender_name,
                    User.first_name,
                    User.last_name,
                    User.username,
                )
                .join(
                    page_keys,
                    and_(Media.account_id == page_keys.c.page_account_id, Media.id == page_keys.c.page_media_id),
                )
                .join(
                    Message,
                    and_(
                        Media.account_id == Message.account_id,
                        Media.message_id == Message.id,
                        Media.chat_id == Message.chat_id,
                    ),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .order_by(*order_by)
            )
            result = await session.execute(stmt)
            rows = result.all()

            has_more = len(rows) > limit
            items = [
                {
                    "id": media.id,
                    "message_id": media.message_id,
                    "chat_id": media.chat_id,
                    "type": media.type,
                    "file_path": media.file_path,
                    "file_name": media.file_name,
                    "file_size": media.file_size,
                    "mime_type": media.mime_type,
                    "width": media.width,
                    "height": media.height,
                    "duration": media.duration,
                    "message_date": msg_date.isoformat() if msg_date else None,
                    "sender_name": resolve_sender_display_name(sender_name, first_name, last_name, username),
                }
                for media, msg_date, sender_name, first_name, last_name, username in rows[:limit]
            ]

            return {"items": items, "has_more": has_more}

    async def get_media_counts(self, chat_id: int, *, account_id: int | None = None) -> dict[str, int]:
        """
        Get count of downloaded media grouped by type for a chat.

        Args:
            chat_id: Chat identifier
            account_id: If set, only this account's media (None = unscoped until phase 4)

        Returns:
            Dict mapping media type to count (only types with count > 0)
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media.type, func.count())
                .where(and_(Media.chat_id == chat_id, Media.downloaded == 1))
                .group_by(Media.type)
            )
            if account_id is not None:
                stmt = stmt.where(Media.account_id == account_id)
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}

    async def get_media_for_message(
        self, chat_id: int, message_id: int, media_type: str, *, account_id: int
    ) -> dict[str, Any] | None:
        """One chat's media row for a (message, type), whatever its storage id.

        ``Media.id`` is a DERIVED key, and two ingest paths spell it
        differently: the API sweep and the listener mint
        ``{chat}_{msg}_{type}``, while the Telegram Desktop importer mints
        ``import_{chat}_{msg}`` — deliberately type-free, so adoption can
        re-key the row whichever type each side computed. Reconstructing the
        sweep spelling and querying by it therefore finds nothing for an
        imported row, which is #423.

        So this asks for what the caller actually means, using the columns
        that hold it. Being predicate-scoped rather than string-scoped also
        makes the chat bound explicit: ``get_media_by_id`` is account-scoped
        only, so a chat bound smuggled inside an id string is a bound only for
        as long as every caller keeps minting that string itself.

        Ordered like ``get_messages`` attaches media (downloaded first, then
        lowest id) so the bytes this serves are the bytes the message payload
        described, even where a re-download left a duplicate row behind.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media)
                .where(
                    and_(
                        Media.account_id == account_id,
                        Media.chat_id == chat_id,
                        Media.message_id == message_id,
                        Media.type == media_type,
                    )
                )
                .order_by(Media.downloaded.desc(), Media.id)
                .limit(1)
            )
            media = (await session.execute(stmt)).scalars().first()
            if not media:
                return None
            return {
                "id": media.id,
                "account_id": media.account_id,
                "message_id": media.message_id,
                "chat_id": media.chat_id,
                "type": media.type,
                "file_path": media.file_path,
                "file_name": media.file_name,
                "file_size": media.file_size,
                "mime_type": media.mime_type,
                "downloaded": media.downloaded,
            }

    async def get_media_by_id(self, media_id: str, *, account_id: int) -> dict[str, Any] | None:
        """Get one media row by its ``{chat_id}_{message_id}_{type}`` storage key.

        Phase 4: the ref-addressed media routes reconstruct this key from a
        resolved chat plus the URL's ``{message_id}_{type}`` suffix, then serve
        the row's ``file_path`` — the URL itself never carries the chat id.
        The account is required: the storage key is only unique per account,
        so an unscoped lookup could raise on — or leak — another account's row.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(and_(Media.account_id == account_id, Media.id == media_id))
            result = await session.execute(stmt)
            media = result.scalar_one_or_none()
            if not media:
                return None
            return {
                "id": media.id,
                "account_id": media.account_id,
                "message_id": media.message_id,
                "chat_id": media.chat_id,
                "type": media.type,
                "file_path": media.file_path,
                "file_name": media.file_name,
                "file_size": media.file_size,
                "mime_type": media.mime_type,
                "downloaded": media.downloaded,
            }

    async def delete_media_for_chat(self, chat_id: int, *, account_id: int) -> int:
        """
        Delete one account's media records for a specific chat.
        Does not delete message records or the chat itself.

        Args:
            chat_id: Chat identifier

        Returns:
            Number of media records deleted
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = delete(Media).where(and_(Media.account_id == account_id, Media.chat_id == chat_id))
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    async def iter_media_for_verification(self, *, account_id: int, batch_size: int = 500):
        """Yield batches of one account's media records that should have files
        on disk (``downloaded=1`` OR ``file_path`` set). Used by VERIFY_MEDIA —
        the caller re-downloads what is missing, and only this account's
        session can.

        Keyset-paginated on ``id`` (a string, unique within one account),
        projecting only the columns verification consumes, so memory stays
        bounded by ``batch_size`` regardless of archive size — materializing
        this set as ORM rows OOM-killed the 256m backup container on large
        archives, the same failure ``iter_media_paths_for_repair`` streams
        around.
        """
        last_id: str | None = None
        while True:
            async with self.db_manager.async_session_factory() as session:
                stmt = (
                    select(
                        Media.id,
                        Media.message_id,
                        Media.chat_id,
                        Media.type,
                        Media.file_path,
                        Media.file_name,
                        Media.file_size,
                        Media.downloaded,
                    )
                    .where(
                        and_(Media.account_id == account_id, or_(Media.downloaded == 1, Media.file_path.isnot(None)))
                    )
                    .order_by(Media.id)
                    .limit(batch_size)
                )
                if last_id is not None:
                    stmt = stmt.where(Media.id > last_id)
                rows = (await session.execute(stmt)).all()
            if not rows:
                return
            yield [
                {
                    "id": r[0],
                    "message_id": r[1],
                    "chat_id": r[2],
                    "type": r[3],
                    "file_path": r[4],
                    "file_name": r[5],
                    "file_size": r[6],
                    "downloaded": r[7],
                }
                for r in rows
            ]
            last_id = rows[-1][0]
            if len(rows) < batch_size:
                return

    async def iter_media_paths_for_repair(self, batch_size: int = 500):
        """Yield ``(account_id, id, file_path, file_name)`` batches for the #175 repair pass.

        Deliberately account-blind: extension repair fixes the file each row
        points at, whatever account owns the row, so the sweep walks the whole
        archive once. Keyset-paginated on the FULL primary key (account_id, id)
        — ``id`` alone stopped being unique in v8.0.0, and a strict ``>`` on a
        non-unique key silently skips the second account's copy of an id.
        Projects only the columns the repair needs, so memory stays bounded
        regardless of table size. A full-table materialization of this table
        once OOM-killed the 256m backup container on large archives; both this
        repair pass and ``iter_media_for_verification`` stream instead.
        """
        last_key: tuple[int, str] | None = None
        while True:
            async with self.db_manager.async_session_factory() as session:
                stmt = (
                    select(Media.account_id, Media.id, Media.file_path, Media.file_name)
                    .where(or_(Media.downloaded == 1, Media.file_path.isnot(None)))
                    .order_by(Media.account_id, Media.id)
                    .limit(batch_size)
                )
                if last_key is not None:
                    last_account, last_id = last_key
                    stmt = stmt.where(
                        or_(
                            Media.account_id > last_account,
                            and_(Media.account_id == last_account, Media.id > last_id),
                        )
                    )
                rows = (await session.execute(stmt)).all()
            if not rows:
                return
            yield [{"account_id": r[0], "id": r[1], "file_path": r[2], "file_name": r[3]} for r in rows]
            last_key = (rows[-1][0], rows[-1][1])
            if len(rows) < batch_size:
                return

    async def reset_chat_sync_cursor(self, chat_id: int) -> int:
        """Zero every account's sync cursor for one chat; chat rows changed.

        The backfill-topics resweep needs the next backup pass to walk the
        chat from the beginning so its upserts can refresh reply_to_top_id
        on rows an HTML import created without topic metadata. All accounts
        on purpose: the backfill is per-chat, and any account archiving the
        chat wants the same refresh.

        BOTH cursors must reset: the sweep's min_id comes from
        sync_status.last_message_id (get_last_message_id), while
        chats.last_synced_message_id mirrors it for display — zeroing only
        the chat column leaves the resweep resuming where it left off. The
        return value counts CHAT rows, so an archived chat whose sync_status
        row does not exist yet (import-only history) still reads as known.
        """
        async with self.db_manager.async_session_factory() as session:
            await session.execute(update(SyncStatus).where(SyncStatus.chat_id == chat_id).values(last_message_id=0))
            result = await session.execute(update(Chat).where(Chat.id == chat_id).values(last_synced_message_id=0))
            await session.commit()
            return result.rowcount or 0

    async def has_media_for_message(self, chat_id: int, message_id: int, *, exclude_id: str, account_id: int) -> bool:
        """True when any media row other than ``exclude_id`` covers the message.

        The importer asks this before (re)creating an ``import_*`` row: when
        the sweep already archived the message's media — including by
        ADOPTING an earlier import run's row, which re-keys it — writing the
        import row again would resurrect exactly the duplicate #405 removed.
        """
        async with self.db_manager.async_session_factory() as session:
            row = (
                await session.execute(
                    select(Media.id)
                    .where(
                        and_(
                            Media.account_id == account_id,
                            Media.chat_id == chat_id,
                            Media.message_id == message_id,
                            Media.id != exclude_id,
                        )
                    )
                    .limit(1)
                )
            ).first()
            return row is not None

    async def get_chats_with_media_type(self, media_type: str, *, account_id: int) -> list[int]:
        """Chat ids holding at least one media row of this type."""
        async with self.db_manager.async_session_factory() as session:
            rows = await session.execute(
                select(Media.chat_id).where(and_(Media.account_id == account_id, Media.type == media_type)).distinct()
            )
            return [c for (c,) in rows if c is not None]

    async def retype_media_for_messages(
        self, chat_id: int, message_ids: Sequence[int], media_type: str, *, account_id: int
    ) -> int:
        """Set the media type for these messages, returning how many rows moved.

        Rows are corrected in place. Nothing is re-keyed and nothing is deleted:
        ``Media.id`` is an opaque token (see reconcile_media_row) and every
        reader resolves a row by its (chat, message, type) columns, so changing
        the type is the whole of the change.
        """
        if not message_ids:
            return 0
        moved = 0
        async with self.db_manager.async_session_factory() as session:
            # Chunked so a chat with thousands of matches cannot build an IN ()
            # list past SQLite's variable limit.
            for start in range(0, len(message_ids), 500):
                chunk = list(message_ids[start : start + 500])
                result = await session.execute(
                    update(Media)
                    .where(
                        and_(
                            Media.account_id == account_id,
                            Media.chat_id == chat_id,
                            Media.message_id.in_(chunk),
                            Media.type != media_type,
                        )
                    )
                    .values(type=media_type)
                )
                moved += result.rowcount or 0
            await session.commit()
        return moved

    async def reconcile_media_row(
        self, chat_id: int, message_id: int, media_type: str, *, account_id: int
    ) -> dict[str, Any] | None:
        """The media row this message already has, re-typed to the current
        judgement, or None when the message has no media row yet.

        ``Media.id`` used to be minted fresh on every capture from
        ``{chat}_{msg}_{type}`` -- so it cached a JUDGEMENT (what kind of thing
        this media is) and then that string was used as the row's identity. The
        moment the judgement changed, the writer stopped talking about the row
        it already had: a re-processed round video reclassified from ``video``
        to ``video_note`` became a SECOND row, the original stayed
        ``downloaded=0`` with its attempt counter untouched, and the pending
        retry re-requested it from Telegram every cycle without ever reaching
        the attempt cap.

        So the id stops being identity. A message's media row is found by its
        ``(account_id, chat_id, message_id)`` COLUMNS and keeps whatever id it
        was first filed under -- opaque, stable, and never re-keyed. Nothing
        reads its shape any more: the viewer builds URL keys from the message
        and type columns, and ``get_media_for_message`` looks rows up the same
        way. Only ``type`` is corrected, which is the value every reader
        actually consults.

        Ordered exactly like ``get_media_for_message`` (downloaded first, then
        lowest id) so the writer and the reader always agree on which row is
        canonical when an archive holds more than one for a message.
        """
        async with self.db_manager.async_session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(Media)
                        .where(
                            and_(
                                Media.account_id == account_id,
                                Media.chat_id == chat_id,
                                Media.message_id == message_id,
                            )
                        )
                        .order_by(Media.downloaded.desc(), Media.id)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            if media_type and row.type != media_type:
                await session.execute(
                    update(Media)
                    .where(and_(Media.account_id == account_id, Media.id == row.id))
                    .values(type=media_type)
                )
                await session.commit()
            return {
                "id": row.id,
                "type": media_type or row.type,
                "message_id": row.message_id,
                "chat_id": row.chat_id,
                "file_name": row.file_name,
                "file_path": row.file_path,
                "file_size": row.file_size,
                "mime_type": row.mime_type,
                "width": row.width,
                "height": row.height,
                "duration": row.duration,
                "content_hash": row.content_hash,
                "downloaded": bool(row.downloaded),
                "download_date": row.download_date,
            }

    async def get_pending_media_downloads(
        self,
        max_media_size_bytes: int | None = None,
        max_attempts: int | None = None,
        limit: int | None = 1000,
        *,
        account_id: int,
    ) -> list[dict[str, Any]]:
        """Get media records that failed to download and need retry.

        Returns records where downloaded=0 for downloadable media types
        (excludes contact/geo/poll which are metadata-only).
        Files exceeding max_media_size_bytes are excluded to prevent
        infinite retry of over-limit media. Records whose download_attempts have
        reached max_attempts are also excluded, so a permanently-failing file
        (e.g. an unwritable filename) can't be re-fetched every run forever (#212).

        ``limit`` bounds a single retry pass so this can't materialize the whole
        pending-media table in memory (the same OOM class fixed in
        ``iter_media_paths_for_repair``); pass ``None`` to restore the old
        unbounded behavior. Ordered by (download_attempts, id) so a bounded pass
        makes progress on the least-retried rows first.
        """
        async with self.db_manager.async_session_factory() as session:
            conditions = [
                # Only this account's failures: retrying them needs this
                # account's client, and only its session can fetch them.
                Media.account_id == account_id,
                Media.downloaded == 0,
                Media.type.notin_(sorted(METADATA_ONLY_MEDIA_TYPES)),
            ]
            if max_media_size_bytes is not None:
                conditions.append(or_(Media.file_size.is_(None), Media.file_size <= max_media_size_bytes))
            if max_attempts is not None:
                conditions.append(Media.download_attempts < max_attempts)
            where_clause = and_(*conditions)
            stmt = select(Media).where(where_clause).order_by(Media.download_attempts.asc(), Media.id.asc())
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()

            if limit is not None and len(rows) == limit:
                total_stmt = select(func.count(Media.id)).where(where_clause)
                total = (await session.execute(total_stmt)).scalar() or 0
                if total > limit:
                    logger.info("media retry: processing %d of %d pending", limit, total)

            return [
                {
                    "id": m.id,
                    "message_id": m.message_id,
                    "chat_id": m.chat_id,
                    "type": m.type,
                    "file_path": m.file_path,
                    "file_name": m.file_name,
                    "file_size": m.file_size,
                    "downloaded": m.downloaded,
                    "download_attempts": m.download_attempts,
                }
                for m in rows
            ]

    @retry_on_locked()
    async def increment_media_download_attempts(self, media_id: str, *, account_id: int) -> None:
        """Bump the failed-download attempt counter for a media record (#212).

        A media id repeats across accounts (it is ``{chat}_{msg}_{type}``), so
        an id-only UPDATE here would charge one account's failure to both.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(Media)
                .where(and_(Media.account_id == account_id, Media.id == media_id))
                .values(download_attempts=func.coalesce(Media.download_attempts, 0) + 1)
            )
            await session.execute(stmt)
            await session.commit()

    async def mark_media_for_redownload(self, media_id: str, *, account_id: int) -> None:
        """Mark a media record as needing re-download.

        Also resets download_attempts so a row that previously hit the retry
        cap (#212) becomes eligible for the pending-download retry again.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(Media)
                .where(and_(Media.account_id == account_id, Media.id == media_id))
                .values(downloaded=0, file_path=None, download_date=None, download_attempts=0)
            )
            await session.execute(stmt)
            await session.commit()

    async def count_capped_media_downloads(self, max_attempts: int, *, account_id: int) -> int:
        """Count downloadable media permanently skipped after hitting the retry cap (#212).

        Lets the caller surface an aggregate signal instead of silently abandoning
        files — the very failure mode #212 was about.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.count(Media.id)).where(
                and_(
                    Media.account_id == account_id,
                    Media.downloaded == 0,
                    Media.type.notin_(sorted(METADATA_ONLY_MEDIA_TYPES)),
                    Media.download_attempts >= max_attempts,
                )
            )
            return (await session.execute(stmt)).scalar() or 0

    async def update_media_file_path(self, media_id: str, file_path: str, *, account_id: int) -> None:
        """Update the stored file_path for a single media record."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(Media)
                .where(and_(Media.account_id == account_id, Media.id == media_id))
                .values(file_path=file_path)
            )
            await session.execute(stmt)
            await session.commit()

    # ========== Reaction Operations ==========

    async def _reset_reactions_sequence(self) -> None:
        """Reset the reactions table sequence to max(id) + 1."""
        async with self.db_manager.async_session_factory() as session:
            if not self.db_manager._is_sqlite:
                await session.execute(
                    text("SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false)")
                )
                await session.commit()
                logger.info("Reset reactions_id_seq sequence")

    async def get_reactions(
        self, message_id: int, chat_id: int, *, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Get all currently-active reactions for a message (excludes tombstoned).

        None account_id = unscoped until phase 4.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Reaction)
                .where(
                    and_(
                        Reaction.message_id == message_id,
                        Reaction.chat_id == chat_id,
                        Reaction.removed_at.is_(None),
                    )
                )
                .order_by(Reaction.emoji)
            )
            if account_id is not None:
                stmt = stmt.where(Reaction.account_id == account_id)
            result = await session.execute(stmt)
            return [{"emoji": r.emoji, "user_id": r.user_id, "count": r.count} for r in result.scalars()]

    @retry_on_locked()
    async def get_message_ids_with_reaction_rows(
        self, chat_id: int, message_ids: list[int], *, account_id: int
    ) -> set[int]:
        """Return the subset of ``message_ids`` holding ANY reaction row (live
        or tombstoned) in this chat.

        One indexed probe per commit batch (idx_reactions_chat_message) lets the
        sweep skip empty-snapshot reconciles for messages with no stored rows —
        the overwhelming majority — where reconcile_reactions would take the
        per-message row lock only to no-op. Messages that DO hold rows still
        reconcile, so removals-to-zero keep tombstoning (#219).
        """
        if not message_ids:
            return set()
        found: set[int] = set()
        async with self.db_manager.async_session_factory() as session:
            # 500-id chunks keep the IN list under every backend's bind-parameter
            # ceiling regardless of the caller's configured batch size.
            for i in range(0, len(message_ids), 500):
                result = await session.execute(
                    select(Reaction.message_id)
                    .where(
                        and_(
                            Reaction.account_id == account_id,
                            Reaction.chat_id == chat_id,
                            Reaction.message_id.in_(message_ids[i : i + 500]),
                        )
                    )
                    .distinct()
                )
                found.update(result.scalars().all())
        return found

    @retry_on_locked()
    async def reconcile_reactions(
        self,
        message_id: int,
        chat_id: int,
        observed: list[dict[str, Any]],
        *,
        account_id: int,
        mark_removed: bool = True,
        _after_seq_reset: bool = False,
    ) -> str:
        """Reconcile a message's reactions against a fresh FULL snapshot (#219).

        ``observed`` is the complete current per-emoji aggregate
        (``[{"emoji", "count"}]``, ``count`` authoritative) — the same shape the
        scheduled backup and the live UpdateMessageReactions handler both produce.
        Storage is intentionally EMOJI-AGGREGATE ONLY (one row per (message, chat,
        emoji), ``user_id`` NULL): per-user attribution is unsound on a user client
        (Telegram exposes only a tiny ``recent_reactions`` preview, so a reactor
        rolling off it is indistinguishable from a removal), so we never persist or
        rely on it. Unlike the legacy full delete-then-reinsert, this:

        - preserves ``created_at`` on the surviving row (first-seen survives
          re-scans — the reporter's histogram measured backup cadence precisely
          because the old path reset it every run);
        - keeps exactly one row per emoji via UPDATE, never ON CONFLICT (the
          ``uq_reaction`` constraint's nullable ``user_id`` is non-colliding in SQL,
          so an upsert on it would grow unbounded) — and collapses any legacy
          multi-row-per-emoji rows into that single aggregate;
        - reconciles removals INCLUDING to zero: an emoji absent from ``observed``
          is tombstoned (``removed_at``) when ``mark_removed`` (default), else
          deleted — this branch runs even when ``observed`` is empty;
        - is a no-op when the message is not archived (best-effort; never stubs a
          synthetic message row, which would render blank in the viewer and, with
          the FK having no CASCADE, raise on PostgreSQL).

        Returns ``"reconciled"`` | ``"noop"`` | ``"no_message"``.
        """
        async with self.db_manager.async_session_factory() as session:
            # Lock the parent message row for the whole reconcile so the live
            # listener and the scheduled backup can't both read "no rows" and insert
            # duplicate aggregate rows for the same emoji (uq_reaction is inert for
            # NULL user_id, so nothing else dedups them → inflated viewer counts).
            # This also guards the FK: reactions.fk_reaction_message has no CASCADE,
            # so a reaction for an unarchived message would raise on PostgreSQL.
            if await self._load_message_for_update(session, account_id, chat_id, message_id) is None:
                return "no_message"

            existing_rows = (
                (
                    await session.execute(
                        select(Reaction).where(
                            and_(
                                Reaction.account_id == account_id,
                                Reaction.message_id == message_id,
                                Reaction.chat_id == chat_id,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_emoji: dict[str, list[Reaction]] = {}
            for r in existing_rows:
                by_emoji.setdefault(r.emoji, []).append(r)

            # Authoritative per-emoji counts from the snapshot (later duplicates of an
            # emoji are summed defensively; the extractor yields one entry per emoji).
            desired: dict[str, int] = {}
            for entry in observed:
                emoji = entry.get("emoji")
                if not emoji:
                    continue
                count = int(entry.get("count", 0) or 0)
                if count <= 0:
                    # A zero/negative count is "absent", not a live reaction — leave it
                    # out of `desired` so the emoji is tombstoned/removed below.
                    continue
                desired[emoji] = desired.get(emoji, 0) + count

            now = utcnow_naive()
            changed = False

            for emoji, count in desired.items():
                rows = by_emoji.get(emoji)
                if rows:
                    # Keep the earliest-seen row as the aggregate (preserves
                    # created_at); collapse any others (legacy per-user/dup rows).
                    rows_sorted = sorted(rows, key=lambda r: (r.created_at or now, r.id))
                    keep = rows_sorted[0]
                    if keep.count != count or keep.user_id is not None or keep.removed_at is not None:
                        keep.count = count
                        keep.user_id = None
                        keep.removed_at = None
                        changed = True
                    for extra in rows_sorted[1:]:
                        await session.delete(extra)
                        changed = True
                else:
                    session.add(
                        Reaction(
                            account_id=account_id,
                            message_id=message_id,
                            chat_id=chat_id,
                            emoji=emoji,
                            user_id=None,
                            count=count,
                            created_at=now,
                            removed_at=None,
                        )
                    )
                    changed = True

            # Emojis no longer present: tombstone (retain) or delete. Collapse any
            # legacy multi-row group into one retained row so counts don't inflate.
            for emoji, rows in by_emoji.items():
                if emoji in desired:
                    continue
                if mark_removed:
                    rows_sorted = sorted(rows, key=lambda r: (r.created_at or now, r.id))
                    keep = rows_sorted[0]
                    total = sum(r.count or 0 for r in rows)
                    if keep.removed_at is None or keep.count != total or keep.user_id is not None:
                        keep.removed_at = keep.removed_at or now
                        keep.count = total
                        keep.user_id = None
                        changed = True
                    for extra in rows_sorted[1:]:
                        await session.delete(extra)
                        changed = True
                else:
                    for row in rows:
                        await session.delete(row)
                        changed = True

            if not changed:
                return "noop"

            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                # A brand-new reaction row can collide with a stale PG serial (the
                # long-standing reactions_id_seq drift). Reset the sequence and retry
                # the reconcile ONCE with the same snapshot so the authoritative state
                # is actually applied (returning early would drop it until the next
                # event). Log the error class only (never ids/emoji — PII).
                if not _after_seq_reset and ("duplicate key" in str(e).lower() or "unique" in str(e).lower()):
                    logger.warning("Reactions sequence out of sync during reconcile, resetting and retrying")
                    await self._reset_reactions_sequence()
                    return await self.reconcile_reactions(
                        message_id,
                        chat_id,
                        observed,
                        account_id=account_id,
                        mark_removed=mark_removed,
                        _after_seq_reset=True,
                    )
                raise
            return "reconciled"

    # ========== Sync Status Operations ==========

    async def get_last_message_id(self, chat_id: int, *, account_id: int) -> int:
        """Get the last synced message ID for one account's copy of a chat."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(SyncStatus.last_message_id).where(
                and_(SyncStatus.account_id == account_id, SyncStatus.chat_id == chat_id)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row if row else 0

    async def get_earliest_message_id(self, chat_id: int, *, account_id: int) -> int:
        """Smallest archived message id for one account's copy of a chat (0 when empty).

        One indexed MIN() probe — the leading-hole check in gap-fill calls it
        once per scanned chat.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.min(Message.id)).where(
                and_(Message.account_id == account_id, Message.chat_id == chat_id)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row if row else 0

    @retry_on_locked()
    async def update_sync_status(
        self, chat_id: int, last_message_id: int, message_count: int, *, account_id: int
    ) -> None:
        """Update sync status for a chat using atomic upsert."""
        async with self.db_manager.async_session_factory() as session:
            now = utcnow_naive()
            values = {
                "account_id": account_id,
                "chat_id": chat_id,
                "last_message_id": last_message_id,
                "last_sync_date": now,
                "message_count": message_count,
            }

            if self._is_sqlite:
                stmt = sqlite_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["account_id", "chat_id"],
                    set_={
                        # High-water mark: the backup reads this as min_id for the
                        # next incremental pass, so it must never move backwards
                        # (an older export import supplies a smaller max id).
                        "last_message_id": func.max(SyncStatus.last_message_id, stmt.excluded.last_message_id),
                        "last_sync_date": stmt.excluded.last_sync_date,
                        "message_count": SyncStatus.message_count + stmt.excluded.message_count,
                    },
                )
            else:
                stmt = pg_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["account_id", "chat_id"],
                    set_={
                        # Same high-water clamp; PostgreSQL spells two-arg max GREATEST.
                        "last_message_id": func.greatest(SyncStatus.last_message_id, stmt.excluded.last_message_id),
                        "last_sync_date": stmt.excluded.last_sync_date,
                        "message_count": SyncStatus.message_count + stmt.excluded.message_count,
                    },
                )

            await session.execute(stmt)
            await session.commit()

    # ========== Gap Detection ==========

    async def detect_message_gaps(
        self, chat_id: int, threshold: int = 50, *, account_id: int
    ) -> list[tuple[int, int, int]]:
        """Detect gaps in message ID sequences for one account's copy of a chat.

        Uses a SQL LAG() window function to find gaps larger than threshold.
        The window MUST name the account: two accounts' id sequences for the
        same chat interleave, so an account-blind LAG() reads them as one
        sequence and a real gap disappears whenever the other account's ids
        happen to fall inside it (measured on both backends).

        Returns:
            List of (gap_start_id, gap_end_id, gap_size) tuples where
            gap_start is the last message ID before the gap and
            gap_end is the first message ID after the gap.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT gap_start, gap_end, gap_size FROM (
                        SELECT
                            LAG(id) OVER (ORDER BY id) AS gap_start,
                            id AS gap_end,
                            id - LAG(id) OVER (ORDER BY id) AS gap_size
                        FROM messages
                        WHERE chat_id = :chat_id AND account_id = :account_id
                    ) gaps
                    WHERE gap_size > :threshold
                    ORDER BY gap_start
                    """
                ),
                {"chat_id": chat_id, "account_id": account_id, "threshold": threshold},
            )
            return [(row[0], row[1], row[2]) for row in result.fetchall()]

    async def get_chats_with_messages(self, *, account_id: int) -> list[int]:
        """One account's chat ids that have at least one stored message.

        The chats table drives the scan (never a wholesale messages sweep —
        that is extremely slow on large databases); a correlated EXISTS probe
        per chat row, served by the chat-leading messages index, keeps the
        name honest. _backup_dialog upserts the chat row before any message
        lands, so a bare chats query let message-less rows through — and in
        gap-fill each of those cost a get_entity call and FloodWait exposure
        for a chat that cannot have gaps.
        """
        async with self.db_manager.async_session_factory() as session:
            has_rows = (
                select(Message.id)
                .where(and_(Message.account_id == Chat.account_id, Message.chat_id == Chat.id))
                .exists()
            )
            stmt = select(Chat.id).where(and_(Chat.account_id == account_id, has_rows))
            result = await session.execute(stmt)
            return [row[0] for row in result.fetchall()]

    # ========== Statistics ==========

    async def get_statistics(self) -> dict[str, Any]:
        """Get statistics - alias for get_cached_statistics for backwards compatibility."""
        return await self.get_cached_statistics()

    async def get_cached_statistics(self) -> dict[str, Any]:
        """Get cached statistics (fast, no expensive queries)."""
        # Get cached stats from metadata
        cached_stats = await self.get_metadata("cached_stats")
        stats_calculated_at = await self.get_metadata("stats_calculated_at")
        last_backup_time = await self.get_metadata("last_backup_time")

        result = {
            "chats": 0,
            "messages": 0,
            "media_files": 0,
            "total_size_mb": 0,
            "stats_calculated_at": stats_calculated_at,
        }

        if cached_stats:
            import json

            try:
                result.update(json.loads(cached_stats))
            except json.JSONDecodeError, TypeError:
                pass

        if last_backup_time:
            result["last_backup_time"] = last_backup_time
            result["last_backup_time_source"] = "metadata"

        return result

    async def calculate_and_store_statistics(self, storage_path: str | None = None) -> dict[str, Any]:
        """Calculate statistics and store in metadata (expensive, run daily).

        When ``storage_path`` is given, total media size reflects actual on-disk
        usage (``du`` semantics) via ``compute_directory_size`` so the figure
        tracks real disk consumption. The filesystem walk is a blocking scan, so
        it runs off the event loop (``asyncio.to_thread``) and outside the DB
        session. If the path is missing/unmounted (``du`` is 0 while media rows
        exist), or no path is given, it falls back to the DB snapshot
        ``SUM(media.file_size WHERE downloaded=1)``.
        """
        import asyncio
        import json

        async with self.db_manager.async_session_factory() as session:
            logger.info("Calculating statistics (this may take a while)...")

            # Chat count
            chat_count = await session.execute(select(func.count(Chat.id)))
            chat_count = chat_count.scalar() or 0

            # Message count
            msg_count = await session.execute(select(func.count()).select_from(Message))
            msg_count = msg_count.scalar() or 0

            # Media count
            media_count = await session.execute(select(func.count(Media.id)).where(Media.downloaded == 1))
            media_count = media_count.scalar() or 0

            # DB snapshot of downloaded media sizes — the fallback when on-disk
            # usage is unavailable (e.g. the backup volume is not mounted yet).
            db_total_size = (
                await session.execute(select(func.sum(Media.file_size)).where(Media.downloaded == 1))
            ).scalar() or 0

            # Per-chat statistics
            chat_stats_query = select(Message.chat_id, func.count(Message.id).label("message_count")).group_by(
                Message.chat_id
            )
            chat_stats_result = await session.execute(chat_stats_query)
            per_chat_stats = {row.chat_id: row.message_count for row in chat_stats_result}

        # Total media size: prefer actual on-disk usage. Run the blocking walk off
        # the event loop and after the session is closed so it never stalls other
        # requests or pins a DB connection.
        if storage_path is not None:
            total_size = await asyncio.to_thread(compute_directory_size, storage_path)
            if total_size == 0 and media_count > 0:
                # Path missing/unmounted: don't cache a spurious 0 over the last good value.
                logger.warning("On-disk storage size is 0 while media exists; using DB snapshot for storage stat")
                total_size = db_total_size
        else:
            total_size = db_total_size

        stats = {
            "chats": int(chat_count),
            "messages": int(msg_count),
            "media_files": int(media_count),
            "total_size_mb": float(round(total_size / (1024 * 1024), 2)),
            "per_chat_message_counts": {int(k): int(v) for k, v in per_chat_stats.items()},
        }

        logger.info(f"Statistics calculated: {chat_count} chats, {msg_count} messages, {media_count} media files")

        # Store in metadata
        await self.set_metadata("cached_stats", json.dumps(stats))
        await self.set_metadata("stats_calculated_at", utcnow_naive().isoformat())

        return stats

    # ========== Delete Operations ==========

    async def delete_chat_and_related_data(self, chat_id: int, media_base_path: str = None, *, account_id: int) -> None:
        """Delete one account's copy of a chat and all related data.

        The on-disk media directory below is chat-scoped, not account-scoped:
        while the media layout stays ``<base>/<chat_id>`` this also removes any
        files another account's copy of the chat still references. Single-account
        (this stage) that set is empty; phase 5 owns the layout decision.
        """
        async with self.db_manager.async_session_factory() as session:
            # Serialize concurrent deletions of the same chat: on PostgreSQL two
            # READ COMMITTED transactions deleting different accounts' copies
            # could each still see the other's not-yet-committed Chat row in the
            # final-copy probe below and BOTH skip the push-subscription purge.
            # Locking every account's row first makes the second deleter wait,
            # so its probe sees the truth. SQLite ignores FOR UPDATE (it has a
            # single writer, which serializes the same race by construction).
            await session.execute(select(Chat.id).where(Chat.id == chat_id).with_for_update())
            # Delete previous versions
            await session.execute(
                delete(MessageVersion).where(
                    and_(MessageVersion.account_id == account_id, MessageVersion.chat_id == chat_id)
                )
            )
            # Delete media records
            await session.execute(delete(Media).where(and_(Media.account_id == account_id, Media.chat_id == chat_id)))
            # Delete reactions
            await session.execute(
                delete(Reaction).where(and_(Reaction.account_id == account_id, Reaction.chat_id == chat_id))
            )
            # Delete messages
            await session.execute(
                delete(Message).where(and_(Message.account_id == account_id, Message.chat_id == chat_id))
            )
            # Delete sync status
            await session.execute(
                delete(SyncStatus).where(and_(SyncStatus.account_id == account_id, SyncStatus.chat_id == chat_id))
            )
            # Delete forum topics and folder memberships explicitly: their FKs
            # declare ondelete CASCADE, but SQLite ships with foreign_keys off,
            # so the cascade never fires there - same reason as every delete above.
            await session.execute(
                delete(ForumTopic).where(and_(ForumTopic.account_id == account_id, ForumTopic.chat_id == chat_id))
            )
            await session.execute(
                delete(ChatFolderMember).where(
                    and_(ChatFolderMember.account_id == account_id, ChatFolderMember.chat_id == chat_id)
                )
            )
            # Delete chat
            await session.execute(delete(Chat).where(and_(Chat.account_id == account_id, Chat.id == chat_id)))

            # Push subscriptions are viewer-side and carry no account column,
            # so a chat-scoped subscription is orphaned only when NO account
            # still has this chat. Checked after the Chat delete above, inside
            # the same transaction; global subscriptions (chat_id NULL) are
            # untouched by construction.
            remaining = await session.execute(select(Chat.id).where(Chat.id == chat_id).limit(1))
            if remaining.first() is None:
                await session.execute(delete(PushSubscription).where(PushSubscription.chat_id == chat_id))

            await session.commit()
            logger.info("Deleted chat and all related data from database")

        # Delete physical files
        if media_base_path and os.path.exists(media_base_path):
            chat_media_dir = os.path.join(media_base_path, str(chat_id))
            if os.path.exists(chat_media_dir):
                try:
                    shutil.rmtree(chat_media_dir)
                    logger.info("Deleted media folder for chat")
                except Exception as e:
                    # Type only, never str(e): OSError stringifies as
                    # "[Errno 66] Directory not empty: '/media/-1001234'", so the
                    # message carries the path — and the path is str(chat_id).
                    # Logging the exception text would undo the redaction above.
                    logger.error(f"Failed to delete media folder for chat: {type(e).__name__}")

            for avatar_type in ["chats", "users"]:
                avatar_pattern = os.path.join(media_base_path, "avatars", avatar_type, f"{chat_id}_*.jpg")
                avatar_files = glob.glob(avatar_pattern)

                # Legacy fallback: remove old <chat_id>.jpg files as well
                legacy_avatar = os.path.join(media_base_path, "avatars", avatar_type, f"{chat_id}.jpg")
                if os.path.exists(legacy_avatar):
                    avatar_files.append(legacy_avatar)
                for avatar_file in avatar_files:
                    try:
                        os.remove(avatar_file)
                        logger.info("Deleted avatar file for chat")
                    except Exception as e:
                        # Type only — the avatar path embeds the chat id too.
                        logger.error(f"Failed to delete avatar for chat: {type(e).__name__}")

    # ========== Web Viewer Operations ==========

    async def _attach_reply_metadata(
        self, session, chat_id: int, messages: list[dict[str, Any]], account_id: int | None = None
    ) -> None:
        """Resolve the reply targets of a whole page in ONE query (#268).

        THE single rule for what a reply quote block shows. Every read path that
        returns rendered messages calls this — the normal list, the pinned list
        and the by-date lookup — so the same message can never render one way in
        one list and another way in the next (#259 was exactly that drift).

        Null contract: a message that IS a reply always carries both
        ``reply_to_sender_name`` and ``reply_to_media_type``. They are None when
        the target is not in the archive (never captured, hard-deleted, or in
        another chat) or when the target's sender cannot be named at all — the
        viewer renders its own fallback. A soft-deleted target is still archived
        history and resolves normally. A message that is not a reply carries
        neither key. ``reply_to_text`` is only backfilled when the row did not
        capture one.

        Cost: one statement per page regardless of how many replies it holds.
        The media kind rides along as a correlated scalar subquery (first media
        row per message) rather than a join, which would multiply rows for
        albums, and never as a per-row lookup.
        """
        reply_ids_needed = {msg["reply_to_msg_id"] for msg in messages if msg.get("reply_to_msg_id")}
        if not reply_ids_needed:
            return

        reply_media_type = (
            select(Media.type)
            .where(
                and_(Media.account_id == Message.account_id, Media.chat_id == chat_id, Media.message_id == Message.id)
            )
            .order_by(Media.id)
            .limit(1)
            .scalar_subquery()
            .label("reply_media_type")
        )
        reply_stmt = (
            select(
                Message.id,
                Message.text,
                Message.sender_name,
                User.first_name,
                User.last_name,
                User.username,
                reply_media_type,
            )
            .outerjoin(User, Message.sender_id == User.id)
            .where(and_(Message.chat_id == chat_id, Message.id.in_(reply_ids_needed)))
        )
        if account_id is not None:
            reply_stmt = reply_stmt.where(Message.account_id == account_id)
        reply_result = await session.execute(reply_stmt)
        reply_rows: dict[int, dict[str, Any]] = {
            row.id: {
                "text": row.text,
                "sender_name": resolve_sender_display_name(
                    row.sender_name, row.first_name, row.last_name, row.username
                ),
                "media_type": row.reply_media_type,
            }
            for row in reply_result
        }

        for msg in messages:
            if not msg.get("reply_to_msg_id"):
                continue
            reply_row = reply_rows.get(msg["reply_to_msg_id"])
            msg["reply_to_sender_name"] = reply_row["sender_name"] if reply_row else None
            msg["reply_to_media_type"] = reply_row["media_type"] if reply_row else None
            if reply_row and not msg.get("reply_to_text") and reply_row["text"]:
                msg["reply_to_text"] = reply_row["text"][:100]

    async def search_messages_by_tag(
        self,
        tag: str,
        *,
        scope: ChatScope,
        chat_id: int | None = None,
        account_id: int | None = None,
        outgoing_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        scan_cap: int = 3000,
    ) -> dict[str, Any]:
        """Messages carrying ``tag`` (#hashtag / $CASHTAG) as a whole token, newest first.

        The tag view's data source: the SQL side prefilters with ILIKE — served
        by ``idx_messages_text_trgm`` on PostgreSQL — and a word-boundary
        post-filter drops substring hits ('#tag' inside '#taglonger').
        Entitlements arrive as ``scope`` and apply in the WHERE clause exactly
        like the chat list, so a restricted viewer's tag search can only ever
        touch entitled chats. ``chat_id``+``account_id`` narrow to one chat
        (the This Chat tab); ``outgoing_only`` is My Messages (the archive
        owner's side of every conversation). Offset paging re-scans from the
        top by design — tag result sets are small, and each request bounds its
        own scan (``scan_cap`` prefilter rows) so no single call can walk the
        table. When the cap truncates the scan, ``has_more`` stays False —
        pages past the cap are unreachable through an offset API, and
        advertising them would loop the client forever — and ``truncated``
        turns True so the UI can say the search was cut short.

        Returns ``{"results": [...], "has_more": bool, "truncated": bool}``;
        rows carry message id/date/text/is_outgoing/sender_name plus
        chat_ref/chat_title/chat_type so the viewer addresses the jump by
        ref, never by id.
        """
        # Hashtags search case-insensitively (official behavior); cashtags are
        # uppercase-only entities, so '$TSLA' must not match '$tsla' in text.
        flags = re.IGNORECASE if tag.startswith("#") else 0
        boundary = re.compile(rf"(?<![\w#$]){re.escape(tag)}(?!\w)", flags)
        escaped = tag.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        needed = offset + limit + 1  # one extra row proves has_more
        matched: list[dict[str, Any]] = []
        scanned = 0
        cursor: tuple[Any, ...] | None = None
        chunk = max(limit * 3, 60)
        exhausted = False

        async with self.db_manager.async_session_factory() as session:
            while len(matched) < needed and scanned < scan_cap:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.sender_name,
                        Message.account_id,
                        Message.chat_id,
                        Chat.ref.label("chat_ref"),
                        Chat.title.label("chat_title"),
                        Chat.first_name.label("chat_first_name"),
                        Chat.last_name.label("chat_last_name"),
                        Chat.type.label("chat_type"),
                    )
                    .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
                    .where(Message.text.isnot(None))
                    .where(Message.text.ilike(f"%{escaped}%", escape="\\"))
                )
                if chat_id is not None:
                    stmt = stmt.where(Message.chat_id == chat_id)
                if account_id is not None:
                    stmt = stmt.where(Message.account_id == account_id)
                if outgoing_only:
                    stmt = stmt.where(Message.is_outgoing == 1)
                for predicate in scope.sql_predicates():
                    stmt = stmt.where(predicate)
                order_cols = (Message.date, Message.account_id, Message.chat_id, Message.id)
                if cursor is not None:
                    stmt = stmt.where(tuple_(*order_cols) < cursor)
                stmt = stmt.order_by(*(col.desc() for col in order_cols)).limit(chunk)

                rows = (await session.execute(stmt)).mappings().all()
                scanned += len(rows)
                for row in rows:
                    if not boundary.search(row["text"] or ""):
                        continue
                    title = (
                        row["chat_title"]
                        or " ".join(part for part in (row["chat_first_name"], row["chat_last_name"]) if part)
                        or "Unknown"
                    )
                    matched.append(
                        {
                            "id": row["id"],
                            "date": row["date"],
                            "text": row["text"],
                            "is_outgoing": row["is_outgoing"],
                            "sender_name": row["sender_name"],
                            "chat_ref": row["chat_ref"],
                            "chat_title": title,
                            "chat_type": row["chat_type"],
                        }
                    )
                    if len(matched) >= needed:
                        break
                if len(rows) < chunk:
                    exhausted = True
                    break
                cursor = tuple(rows[-1][key] for key in ("date", "account_id", "chat_id", "id"))

        truncated = not exhausted and scanned >= scan_cap and len(matched) < needed
        return {
            "results": matched[offset : offset + limit],
            "has_more": len(matched) > offset + limit,
            "truncated": truncated,
        }

    # --- Global (cross-chat) text search: the sidebar's Messages section ------
    #
    # PostgreSQL has two ways to answer "the newest N messages matching q", and
    # its planner cannot tell which one is right: the tsquery is built inside
    # the database (PG_TSQUERY_FROM_SEARCH) and prefix terms get a flat row
    # estimate either way, so it always walks idx_messages_date backwards and
    # filters. That is 1 ms for a dense term and a full-table walk for a rare
    # or absent one — 2 to 9 s on a 2.9M-row archive. The GIN index is the
    # opposite: its cost is the number of hits, so a rare term is milliseconds
    # and a single letter is over a second. So the search first asks the index
    # how many hits there are, capped, and takes the path that is bounded for
    # that answer: under GLOBAL_SEARCH_DENSE_HITS the hit set is materialised
    # and sorted; at or above it the walk fills a page within a few thousand
    # rows. The walk keeps a statement timeout for the one shape neither path
    # bounds — a dense term whose newest hit is millions of rows back — and
    # the sorted hit set answers when it fires. SQLite's FTS5 always drives
    # from the hit set, and sorting the keys before the joins is what keeps a
    # common word cheap there (measured on a 155k-row archive: 87 ms against
    # 667 ms for the joined walk), so SQLite takes that one path
    # unconditionally.
    GLOBAL_SEARCH_DENSE_HITS = 10_000
    GLOBAL_SEARCH_WALK_TIMEOUT_MS = 2_000

    _GLOBAL_SEARCH_ORDER = (Message.date, Message.account_id, Message.chat_id, Message.id)
    _GLOBAL_SEARCH_COLUMNS = (
        Message.id,
        Message.date,
        Message.text,
        Message.sender_name,
        Message.is_deleted,
        Message.account_id,
        Message.chat_id,
        Chat.ref.label("chat_ref"),
        Chat.title.label("chat_title"),
        Chat.first_name.label("chat_first_name"),
        Chat.last_name.label("chat_last_name"),
        Chat.username.label("chat_username"),
        Chat.type.label("chat_type"),
        Chat.is_forum.label("chat_is_forum"),
        ForumTopic.title.label("topic_title"),
    )

    async def search_messages_global(
        self,
        search: str,
        *,
        scope: ChatScope,
        limit: int = 50,
        offset: int = 0,
        dense_hits: int | None = None,
        walk_timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Messages whose text matches ``search`` in ANY entitled chat, newest first.

        Entitlements arrive as ``scope`` and apply in SQL exactly like the chat
        list and the tag view, so a restricted viewer's search only ever
        touches entitled chats. Matching is word-prefix through the same
        predicate as the per-chat search (``_text_search_predicate``). Unlike
        the per-chat search there is no ILIKE fallback: without the full-text
        layer a cross-chat substring scan reads the whole archive per
        keystroke, so the answer is empty with ``indexed`` False and the
        viewer says the index is missing. A search carrying no word at all
        (punctuation, emoji) answers empty too.

        Rows carry ``chat_ref``/``chat_title``/``chat_first_name``/
        ``chat_last_name``/``chat_username``/``chat_type``/``chat_is_forum``
        so the viewer names the chat the way the chat list does (a private
        chat has no title), ``topic_title`` for forum hits, and ``is_deleted``
        so a soft-deleted hit can be dimmed like it is in the chat.

        ``dense_hits`` and ``walk_timeout_ms`` exist for tests that want to
        drive each PostgreSQL path with a handful of rows.

        Returns ``{"results": [...], "has_more": bool, "indexed": bool}``; one
        extra row is fetched to answer ``has_more``.
        """
        if not search_has_words(search):
            return {"results": [], "has_more": False, "indexed": True}
        if dense_hits is None:
            dense_hits = self.GLOBAL_SEARCH_DENSE_HITS
        if walk_timeout_ms is None:
            walk_timeout_ms = self.GLOBAL_SEARCH_WALK_TIMEOUT_MS

        async with self.db_manager.async_session_factory() as session:
            predicate = await self._text_search_predicate(session, search)
            if predicate is None:
                return {"results": [], "has_more": False, "indexed": False}

            if (
                self._is_sqlite
                or await self._global_search_hit_count(session, predicate, scope, dense_hits) < dense_hits
            ):
                rows = await self._global_search_sorted_hits(session, predicate, scope, limit, offset)
            else:
                try:
                    rows = await self._global_search_walk(
                        session, predicate, scope, limit, offset, timeout_ms=walk_timeout_ms
                    )
                except DBAPIError as exc:
                    if not _is_statement_timeout(exc):
                        raise
                    await session.rollback()
                    rows = await self._global_search_sorted_hits(session, predicate, scope, limit, offset)

        results = [
            {
                "id": row["id"],
                "date": row["date"],
                "text": row["text"],
                "sender_name": row["sender_name"],
                "is_deleted": bool(row["is_deleted"]),
                "account_id": row["account_id"],
                "chat_id": row["chat_id"],
                "chat_ref": row["chat_ref"],
                "chat_title": row["chat_title"],
                "chat_first_name": row["chat_first_name"],
                "chat_last_name": row["chat_last_name"],
                "chat_username": row["chat_username"],
                "chat_type": row["chat_type"],
                "chat_is_forum": bool(row["chat_is_forum"]),
                "topic_title": row["topic_title"],
            }
            for row in rows[:limit]
        ]
        return {"results": results, "has_more": len(rows) > limit, "indexed": True}

    @staticmethod
    def _global_search_scoped(stmt, scope: ChatScope):
        """Restrict a hit-set SELECT over ``messages`` to the entitled chats.

        Applied INSIDE the hit set so a viewer entitled to one chat never pays
        for the whole archive's hits; skipped entirely for an unrestricted
        scope, where the join would only add work.
        """
        if scope.unrestricted:
            return stmt
        stmt = stmt.join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
        for predicate in scope.sql_predicates():
            stmt = stmt.where(predicate)
        return stmt

    @classmethod
    def _global_search_joins(cls, stmt):
        """The chat row and (for forum hits) the topic row behind each result."""
        return stmt.join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id)).outerjoin(
            ForumTopic,
            and_(
                ForumTopic.account_id == Message.account_id,
                ForumTopic.chat_id == Message.chat_id,
                ForumTopic.id == func.coalesce(Message.reply_to_top_id, 1),
            ),
        )

    async def _global_search_hit_count(self, session, predicate, scope: ChatScope, cap: int) -> int:
        """How many rows match, counted through the index and stopped at ``cap``."""
        hits = self._global_search_scoped(select(literal(1)).select_from(Message).where(predicate), scope)
        capped = hits.limit(cap).subquery("search_hits")
        return int((await session.execute(select(func.count()).select_from(capped))).scalar_one())

    async def _global_search_walk(
        self, session, predicate, scope: ChatScope, limit: int, offset: int, *, timeout_ms: int | None = None
    ):
        """Newest-first walk that filters as it goes — the shape for dense terms."""
        stmt = self._global_search_joins(select(*self._GLOBAL_SEARCH_COLUMNS).select_from(Message)).where(predicate)
        for scope_predicate in scope.sql_predicates():
            stmt = stmt.where(scope_predicate)
        stmt = stmt.order_by(*(column.desc() for column in self._GLOBAL_SEARCH_ORDER)).limit(limit + 1).offset(offset)
        if timeout_ms is not None:
            # SET LOCAL: scoped to this transaction, so the pooled connection
            # never carries it into another request.
            await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
        return (await session.execute(stmt)).mappings().all()

    async def _global_search_sorted_hits(self, session, predicate, scope: ChatScope, limit: int, offset: int):
        """Materialise the hit keys through the index, sort them, then fetch one page.

        The keys are MATERIALIZED so the planner cannot flatten the CTE back
        into the date walk it prefers, and the page is cut BEFORE the joins so
        a dense term never joins every hit to fetch twenty rows.
        """
        keys = select(Message.account_id, Message.chat_id, Message.id, Message.date).select_from(Message)
        hits = self._global_search_scoped(keys.where(predicate), scope).cte("search_hits").prefix_with("MATERIALIZED")
        page = (
            select(hits.c.account_id, hits.c.chat_id, hits.c.id, hits.c.date)
            .order_by(hits.c.date.desc(), hits.c.account_id.desc(), hits.c.chat_id.desc(), hits.c.id.desc())
            .limit(limit + 1)
            .offset(offset)
            .subquery("search_page")
        )
        stmt = self._global_search_joins(
            select(*self._GLOBAL_SEARCH_COLUMNS)
            .select_from(page)
            .join(
                Message,
                and_(
                    Message.account_id == page.c.account_id,
                    Message.chat_id == page.c.chat_id,
                    Message.id == page.c.id,
                ),
            )
        ).order_by(page.c.date.desc(), page.c.account_id.desc(), page.c.chat_id.desc(), page.c.id.desc())
        return (await session.execute(stmt)).mappings().all()

    async def _fts_ready(self, session) -> bool:
        """Whether migration 028's full-text layer exists in THIS database.

        Probed once per adapter (databases do not gain or lose the index
        mid-process except during the migration itself, which restarts the
        app). A create_all() database that has not run migrations yet keeps
        ILIKE until its first upgrade pass.
        """
        if self._fts_ready_cache is None:
            if self._is_sqlite:
                row = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t").bindparams(t=SQLITE_FTS_TABLE)
                )
            else:
                # to_regclass resolves 'messages' through search_path exactly
                # like the unqualified queries below do, so the probe answers
                # for the table they will actually hit — a same-named table in
                # another schema can neither fake the column nor hide it.
                row = await session.execute(
                    text(
                        "SELECT 1 FROM pg_attribute "
                        "WHERE attrelid = to_regclass('messages') "
                        "AND attname = :c AND NOT attisdropped"
                    ).bindparams(c=PG_TSVECTOR_COLUMN)
                )
            self._fts_ready_cache = row.first() is not None
        return self._fts_ready_cache

    async def _text_search_predicate(self, session, search: str):
        """An indexed word-prefix predicate for ``search``, or None for ILIKE.

        None means: no index in this database, or the search reduced to no
        words (punctuation-only) — the caller keeps the substring ILIKE that
        has always answered those.
        """
        if not await self._fts_ready(session):
            return None
        if self._is_sqlite:
            match = fts_match_query(search)
            if match is None:
                return None
            return text(
                "messages.rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH :fts_match)"
            ).bindparams(fts_match=match)
        if not search_has_words(search):
            return None
        # The tsquery is built inside PostgreSQL from the same parser that
        # built the index (see PG_TSQUERY_FROM_SEARCH) — the raw search
        # string only ever travels as a bind parameter.
        return text(f"messages.text_search @@ {PG_TSQUERY_FROM_SEARCH}").bindparams(fts_search=search)

    async def get_messages_paginated(
        self,
        chat_id: int,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        before_date: datetime | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
        topic_id: int | None = None,
        *,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get messages with user info and media info for web viewer.

        ``account_id=None`` is unscoped until phase 4 (viewer entitlements).

        v6.0.0: Media is now returned as a nested object from the media table.
        v6.2.0: Added topic_id filter for forum topic messages.

        Supports two pagination modes:
        1. Offset-based (legacy): Uses offset parameter - slower for large offsets
        2. Cursor-based (preferred): Uses before_date/before_id - O(1) regardless of position

        Args:
            chat_id: Chat ID
            limit: Maximum messages to return
            offset: Pagination offset (used only if before_date/before_id not provided)
            search: Optional text search filter
            before_date: Cursor - get messages before this date (faster than offset)
            before_id: Cursor - message ID to use as tiebreaker for same-date messages
                (or, without before_date, an id-only bound: rows with id < before_id)
            after_id: Cursor - get messages newer than this message ID (takes
                precedence over the other cursors; used for jump-to-message
                after-context). Response stays newest-first like every other mode.
            topic_id: Optional forum topic ID to filter messages by thread

        Returns:
            List of message dictionaries with user and media info. A row that is a
            reply also carries ``reply_to_sender_name`` and ``reply_to_media_type``
            (both nullable) so the viewer can render "Reply to <name>" (#268).
        """
        async with self.db_manager.async_session_factory() as session:
            # Build query with joins - v6.0.0: join on composite key
            # No Media join here: a message can carry SEVERAL media rows (a
            # JSON import writes import_{chat}_{msg} beside the live
            # {chat}_{msg}_{type} row), and LIMIT applied to the multiplied
            # join meant a page of 50 could deliver far fewer distinct
            # messages — measured 50 rows / 30 messages with 10 three-media
            # heads. Media is batch-attached below from the page's id set,
            # the same shape versions and reactions already use. The User
            # join stays: users.id is unique, so it cannot multiply.
            stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                )
                .outerjoin(User, Message.sender_id == User.id)
                .where(Message.chat_id == chat_id)
            )

            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)

            # v6.2.0: Filter by forum topic. NULL reply_to_top_id == General (id=1),
            # matching the coalesce in get_forum_topics counts.
            # Mirrored by messageBelongsToCurrentTopic in the viewer (GENERAL_TOPIC_ID).
            if topic_id is not None:
                stmt = stmt.where(func.coalesce(Message.reply_to_top_id, 1) == topic_id)

            if search:
                fts_predicate = await self._text_search_predicate(session, search)
                if fts_predicate is not None:
                    stmt = stmt.where(fts_predicate)
                else:
                    escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                    stmt = stmt.where(Message.text.ilike(f"%{escaped}%", escape="\\"))

            # Cursor-based pagination (preferred - O(1) performance)
            # Mirrored by the viewer (src/web/templates/index.html: compareMessagesDesc/messageCursor) — keep in sync.
            if after_id is not None:
                # Forward window (#213): the LIMIT must take the rows closest to the
                # target, so select oldest-first and reverse to newest-first below to
                # keep the response contract identical to every other mode. Ordering
                # by id (monotonic per chat) instead of date lets the (chat_id, id)
                # index satisfy both the bound and the sort — ordering by date here
                # forced a full per-chat scan plus a temp sort.
                stmt = stmt.where(Message.id > after_id)
                stmt = stmt.order_by(Message.id.asc()).limit(limit)
            elif before_date is not None:
                # Use composite cursor: (date, id) for deterministic ordering
                # Messages with same date are ordered by id DESC
                if before_id is not None:
                    stmt = stmt.where(
                        or_(Message.date < before_date, and_(Message.date == before_date, Message.id < before_id))
                    )
                else:
                    stmt = stmt.where(Message.date < before_date)
                stmt = stmt.order_by(Message.date.desc(), Message.id.desc()).limit(limit)
            elif before_id is not None:
                # Lone before_id cursor (#213): per-chat Telegram message ids increase
                # monotonically over time, so an id-only bound is chronologically
                # correct within the chat scope. Without this branch a lone before_id
                # silently fell through to the offset path and returned the latest
                # page — the jump-to-message window was never fetched. Ordering by id
                # (not date) lets the (chat_id, id) index seek directly to the bound.
                stmt = stmt.where(Message.id < before_id)
                stmt = stmt.order_by(Message.id.desc()).limit(limit)
            else:
                # Offset-based pagination (legacy fallback)
                stmt = stmt.order_by(Message.date.desc(), Message.id.desc()).limit(limit).offset(offset)

            result = await session.execute(stmt)
            messages = []

            # account_id is carried beside each row (not in the API dict — the
            # response stays ref-addressed) so the media attach below can match
            # per (account, message) even in unscoped mode.
            row_accounts: list[int] = []
            for row in result:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username
                msg["media"] = None

                # Parse raw_data JSON
                if msg.get("raw_data"):
                    try:
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except ValueError, TypeError:
                        logger.debug("Malformed raw_data JSON for a message row; substituting empty dict")
                        msg["raw_data"] = {}

                row_accounts.append(row.Message.account_id)
                messages.append(msg)

            if after_id is not None:
                # Selected oldest-first for the LIMIT; restore the newest-first contract.
                messages.reverse()
                row_accounts.reverse()

            version_counts = {msg["id"]: 0 for msg in messages}
            page_message_ids = [msg["id"] for msg in messages]

            # v6.0.0 media as a nested object — batched for the page. When a
            # message carries several media rows, ONE is attached
            # deterministically: a downloaded row beats a pending one, then
            # the lowest media id wins (the old join order was arbitrary).
            if page_message_ids:
                media_stmt = (
                    select(Media)
                    .where(
                        and_(
                            Media.chat_id == chat_id,
                            Media.message_id.in_(page_message_ids),
                        )
                    )
                    .order_by(Media.message_id, Media.downloaded.desc(), Media.id)
                )
                if account_id is not None:
                    media_stmt = media_stmt.where(Media.account_id == account_id)
                media_result = await session.execute(media_stmt)
                media_by_key: dict[tuple[int, int], dict[str, Any]] = {}
                for media_row in media_result.scalars():
                    key = (media_row.account_id, media_row.message_id)
                    if key in media_by_key:
                        continue
                    media_by_key[key] = {
                        "id": media_row.id,
                        "type": media_row.type,
                        "file_path": media_row.file_path,
                        "file_name": media_row.file_name,
                        "file_size": media_row.file_size,
                        "mime_type": media_row.mime_type,
                        "width": media_row.width,
                        "height": media_row.height,
                        "duration": media_row.duration,
                    }
                for account, msg in zip(row_accounts, messages, strict=True):
                    msg["media"] = media_by_key.get((account, msg["id"]))
            if page_message_ids:
                count_stmt = (
                    select(MessageVersion.message_id, func.count(MessageVersion.id).label("version_count"))
                    .where(
                        and_(
                            MessageVersion.chat_id == chat_id,
                            MessageVersion.message_id.in_(page_message_ids),
                        )
                    )
                    .group_by(MessageVersion.message_id)
                )
                if account_id is not None:
                    count_stmt = count_stmt.where(MessageVersion.account_id == account_id)
                count_result = await session.execute(count_stmt)
                version_counts.update({row.message_id: int(row.version_count or 0) for row in count_result})

            await self._attach_reply_metadata(session, chat_id, messages, account_id)

            # Batch reactions: one query for the whole page instead of one
            # get_reactions() call per message. Ties within the same emoji are
            # broken by Reaction.id to match get_reactions' de-facto row order.
            reactions_by_message: dict[int, list[dict[str, Any]]] = {mid: [] for mid in page_message_ids}
            if page_message_ids:
                reactions_stmt = (
                    select(Reaction)
                    .where(
                        and_(
                            Reaction.chat_id == chat_id,
                            Reaction.message_id.in_(page_message_ids),
                            # Tombstoned (retain-on-removal) reactions are archived
                            # history, not part of the live displayed count (#219).
                            Reaction.removed_at.is_(None),
                        )
                    )
                    .order_by(Reaction.message_id, Reaction.emoji, Reaction.id)
                )
                if account_id is not None:
                    reactions_stmt = reactions_stmt.where(Reaction.account_id == account_id)
                reactions_result = await session.execute(reactions_stmt)
                for r in reactions_result.scalars():
                    reactions_by_message[r.message_id].append(
                        {"emoji": r.emoji, "user_id": r.user_id, "count": r.count}
                    )

            for msg in messages:
                msg["version_count"] = version_counts.get(msg["id"], 0)

                reactions_by_emoji = {}
                for reaction in reactions_by_message.get(msg["id"], []):
                    emoji = reaction["emoji"]
                    if emoji not in reactions_by_emoji:
                        reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                    reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                    if reaction.get("user_id"):
                        reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
                msg["reactions"] = list(reactions_by_emoji.values())

            return messages

    async def get_message_dates(
        self,
        chat_id: int,
        day_ranges: list[tuple[str, datetime, datetime]],
        topic_id: int | None = None,
        *,
        account_id: int | None = None,
    ) -> list[str]:
        """Return the requested local-calendar dates that contain messages (None account_id = unscoped until phase 4)."""
        if not day_ranges:
            return []

        branches = []
        for day, utc_start, utc_end in day_ranges:
            conditions = [
                Message.chat_id == chat_id,
                Message.date >= utc_start,
                Message.date < utc_end,
            ]
            if account_id is not None:
                conditions.append(Message.account_id == account_id)
            if topic_id is not None:
                conditions.append(func.coalesce(Message.reply_to_top_id, 1) == topic_id)
            branches.append(
                select(literal(day).label("day")).where(
                    exists(select(1).where(*conditions)),
                )
            )

        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(union_all(*branches))
            return sorted(set(result.scalars().all()))

    async def find_message_by_date_with_joins(
        self,
        chat_id: int,
        target_date: datetime,
        topic_id: int | None = None,
        *,
        account_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Find message by date with full user/media joins for web viewer.

        v6.0.0: Media is now returned as a nested object from the media table.

        Args:
            chat_id: Chat ID
            target_date: Target date to find message for
            topic_id: Optional forum topic ID to filter messages by thread
            account_id: If set, only this account's messages (None = unscoped until phase 4)

        Returns:
            Message dictionary with user and media info, or None
        """
        async with self.db_manager.async_session_factory() as session:
            base_stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(
                    Media,
                    and_(
                        Media.account_id == Message.account_id,
                        Media.message_id == Message.id,
                        Media.chat_id == Message.chat_id,
                    ),
                )
                .where(Message.chat_id == chat_id)
            )
            if account_id is not None:
                base_stmt = base_stmt.where(Message.account_id == account_id)
            if topic_id is not None:
                base_stmt = base_stmt.where(func.coalesce(Message.reply_to_top_id, 1) == topic_id)

            # Try on or after target date
            stmt = base_stmt.where(Message.date >= target_date).order_by(Message.date.asc()).limit(1)
            result = await session.execute(stmt)
            row = result.first()

            if not row:
                # Try before target date
                stmt = base_stmt.where(Message.date < target_date).order_by(Message.date.desc()).limit(1)
                result = await session.execute(stmt)
                row = result.first()

            if not row:
                # Try first message in chat
                stmt = base_stmt.order_by(Message.date.asc()).limit(1)
                result = await session.execute(stmt)
                row = result.first()

            if not row:
                return None

            msg = self._message_to_dict(row.Message)
            msg["first_name"] = row.first_name
            msg["last_name"] = row.last_name
            msg["username"] = row.username

            # v6.0.0: Media as nested object
            if row.media_type:
                msg["media"] = {
                    "id": row.media_id,
                    "type": row.media_type,
                    "file_path": row.media_file_path,
                    "file_name": row.media_file_name,
                    "file_size": row.media_file_size,
                    "mime_type": row.media_mime_type,
                    "width": row.media_width,
                    "height": row.media_height,
                    "duration": row.media_duration,
                }
            else:
                msg["media"] = None

            # Parse raw_data
            if msg.get("raw_data"):
                try:
                    msg["raw_data"] = json.loads(msg["raw_data"])
                except ValueError, TypeError:
                    logger.debug("Malformed raw_data JSON for a message row; substituting empty dict")
                    msg["raw_data"] = {}

            # Reply quote metadata — same helper, same rule as every other read
            # path. It replaces a text-only lookup that resolved less than the
            # message list did for the very same message.
            await self._attach_reply_metadata(session, chat_id, [msg], account_id)

            # Get reactions
            reactions = await self.get_reactions(msg["id"], chat_id, account_id=account_id)
            reactions_by_emoji = {}
            for reaction in reactions:
                emoji = reaction["emoji"]
                if emoji not in reactions_by_emoji:
                    reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                if reaction.get("user_id"):
                    reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
            msg["reactions"] = list(reactions_by_emoji.values())

            return msg

    @staticmethod
    def _chat_row_to_dict(chat: Chat) -> dict[str, Any]:
        return {
            "id": chat.id,
            "account_id": chat.account_id,
            "ref": chat.ref,
            "type": chat.type,
            "title": chat.title,
            "username": chat.username,
            "first_name": chat.first_name,
            "last_name": chat.last_name,
            "phone": chat.phone,
            "description": chat.description,
            "participants_count": chat.participants_count,
            "is_forum": chat.is_forum,
            "is_archived": chat.is_archived,
        }

    async def get_chat_by_id(self, chat_id: int, *, account_id: int | None = None) -> dict[str, Any] | None:
        """Get a single chat by ID (None account_id = unscoped until phase 4)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat).where(Chat.id == chat_id)
            if account_id is not None:
                stmt = stmt.where(Chat.account_id == account_id)
            result = await session.execute(stmt)
            chat = result.scalar_one_or_none()
            if not chat:
                return None
            return self._chat_row_to_dict(chat)

    async def get_chat_by_ref(self, ref: str, *, account_id: int | None = None) -> dict[str, Any] | None:
        """Resolve an opaque chat ref to its chat row, or None when no chat carries it.

        The viewer's phase-4 resolver: ``chats.ref`` is globally UNIQUE
        (uq_chats_ref spans accounts), so a bare ref names exactly one
        (account_id, chat_id) pair. The parameterised equality on a VARCHAR(22)
        makes any malformed or oversized candidate a plain index miss — one code
        path for well-formed-unknown and garbage alike, so response timing does
        not classify the input.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat).where(Chat.ref == ref)
            if account_id is not None:
                stmt = stmt.where(Chat.account_id == account_id)
            result = await session.execute(stmt)
            chat = result.scalar_one_or_none()
            if not chat:
                return None
            return self._chat_row_to_dict(chat)

    async def get_pinned_messages(self, chat_id: int, *, account_id: int | None = None) -> list[dict[str, Any]]:
        """Get all pinned messages for a chat, ordered by date descending (newest first).

        v6.0.0: Media is now returned as a nested object from the media table.
        None account_id = unscoped until phase 4.

        The pinned-only view swaps this list into the SAME message renderer the
        normal list uses, so a pinned reply must arrive with the same reply
        metadata (#268) — otherwise one message renders "Reply to <name>" in the
        list and a bare "Reply to / Message" in the pinned view.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(
                    Media,
                    and_(
                        Media.account_id == Message.account_id,
                        Media.message_id == Message.id,
                        Media.chat_id == Message.chat_id,
                    ),
                )
                .where(Message.chat_id == chat_id)
                .where(Message.is_pinned == 1)
                .order_by(Message.date.desc())
            )
            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)

            result = await session.execute(stmt)
            rows = result.all()

            messages = []
            for row in rows:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username

                # v6.0.0: Media as nested object
                if row.media_type:
                    msg["media"] = {
                        "id": row.media_id,
                        "type": row.media_type,
                        "file_path": row.media_file_path,
                        "file_name": row.media_file_name,
                        "file_size": row.media_file_size,
                        "mime_type": row.media_mime_type,
                        "width": row.media_width,
                        "height": row.media_height,
                        "duration": row.media_duration,
                    }
                else:
                    msg["media"] = None

                # Parse raw_data JSON
                if msg.get("raw_data"):
                    try:
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except ValueError, TypeError:
                        logger.debug("Malformed raw_data JSON for a message row; substituting empty dict")
                        msg["raw_data"] = {}

                messages.append(msg)

            # One query for the whole pinned list, not one per pinned reply.
            await self._attach_reply_metadata(session, chat_id, messages, account_id)

            return messages

    async def sync_pinned_messages(self, chat_id: int, pinned_message_ids: list[int], *, account_id: int) -> None:
        """
        Sync pinned messages for one account's copy of a chat.

        Sets is_pinned=1 for messages in the list and is_pinned=0 for all others.
        This ensures the database reflects the current state of pinned messages.
        The unpin sweep is the dangerous half: unscoped it would strip the other
        account's pins for the same chat id.

        Args:
            chat_id: Chat ID
            pinned_message_ids: List of message IDs that are currently pinned
        """
        async with self.db_manager.async_session_factory() as session:
            # First, unpin all messages in this chat
            await session.execute(
                update(Message)
                .where(Message.account_id == account_id)
                .where(Message.chat_id == chat_id)
                .where(Message.is_pinned == 1)
                .values(is_pinned=0)
            )

            # Then, pin the specified messages (if any exist in our database)
            if pinned_message_ids:
                await session.execute(
                    update(Message)
                    .where(Message.account_id == account_id)
                    .where(Message.chat_id == chat_id)
                    .where(Message.id.in_(pinned_message_ids))
                    .values(is_pinned=1)
                )

            await session.commit()

    async def update_message_pinned(self, chat_id: int, message_id: int, is_pinned: bool, *, account_id: int) -> None:
        """
        Update the pinned status of a single message.

        Used by the real-time listener when pin/unpin events are received.

        Args:
            chat_id: Chat ID
            message_id: Message ID
            is_pinned: Whether the message is pinned
        """
        async with self.db_manager.async_session_factory() as session:
            await session.execute(
                update(Message)
                .where(Message.account_id == account_id)
                .where(Message.chat_id == chat_id)
                .where(Message.id == message_id)
                .values(is_pinned=1 if is_pinned else 0)
            )
            await session.commit()

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Get a user by ID."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return None
            return {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone,
                "is_bot": user.is_bot,
            }

    async def get_messages_for_export(
        self,
        chat_id: int,
        include_media: bool = False,
        *,
        account_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ):
        """
        Get messages for export with user info.
        Returns an async generator for streaming.
        None account_id = unscoped until phase 4.

        v6.0.0: Media info now comes from the media table via JOIN.

        Args:
            chat_id: Chat ID to export
            include_media: If True, include media info from media table
            from_date: naive-UTC inclusive lower bound on Message.date
            to_date: naive-UTC EXCLUSIVE upper bound on Message.date

        Yields:
            Message dictionaries with user info
        """
        async with self.db_manager.async_session_factory() as session:
            if include_media:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.reply_to_msg_id,
                        Message.sender_name,
                        Media.type.label("media_type"),
                        Media.file_path.label("media_file_path"),
                        User.first_name,
                        User.last_name,
                        User.username,
                    )
                    .outerjoin(User, Message.sender_id == User.id)
                    .outerjoin(
                        Media,
                        and_(
                            Media.account_id == Message.account_id,
                            Media.message_id == Message.id,
                            Media.chat_id == Message.chat_id,
                        ),
                    )
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.date.asc())
                )
            else:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.reply_to_msg_id,
                        Message.sender_name,
                        User.first_name,
                        User.last_name,
                        User.username,
                    )
                    .outerjoin(User, Message.sender_id == User.id)
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.date.asc())
                )

            if account_id is not None:
                stmt = stmt.where(Message.account_id == account_id)
            if from_date is not None:
                stmt = stmt.where(Message.date >= from_date)
            if to_date is not None:
                stmt = stmt.where(Message.date < to_date)

            result = await session.stream(stmt)
            async for row in result:
                msg = {
                    "id": row.id,
                    "date": row.date.isoformat() if row.date else None,
                    "sender": {
                        "name": resolve_sender_display_name(
                            row.sender_name, row.first_name, row.last_name, row.username
                        )
                        or "Unknown",
                        "username": row.username,
                    },
                    "text": row.text,
                    "is_outgoing": bool(row.is_outgoing),
                    "reply_to": row.reply_to_msg_id,
                }
                if include_media:
                    msg["media_type"] = row.media_type
                    msg["media_path"] = row.media_file_path
                yield msg

    # ========== Forum Topic Operations (v6.2.0) ==========

    @retry_on_locked()
    async def upsert_forum_topic(self, topic_data: dict[str, Any], *, account_id: int) -> None:
        """Insert or update a forum topic record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "account_id": account_id,
                "id": topic_data["id"],
                "chat_id": topic_data["chat_id"],
                "title": topic_data["title"],
                "icon_color": topic_data.get("icon_color"),
                "icon_emoji_id": topic_data.get("icon_emoji_id"),
                "icon_emoji": topic_data.get("icon_emoji"),
                "is_closed": topic_data.get("is_closed", 0),
                "is_pinned": topic_data.get("is_pinned", 0),
                "is_hidden": topic_data.get("is_hidden", 0),
                "date": _strip_tz(topic_data.get("date")),
                "updated_at": utcnow_naive(),
            }

            update_set = {
                "title": values["title"],
                "icon_color": values["icon_color"],
                "icon_emoji_id": values["icon_emoji_id"],
                "icon_emoji": values["icon_emoji"],
                "is_closed": values["is_closed"],
                "is_pinned": values["is_pinned"],
                "is_hidden": values["is_hidden"],
                "date": values["date"],
                "updated_at": utcnow_naive(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ForumTopic).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "chat_id", "id"], set_=update_set)
            else:
                stmt = pg_insert(ForumTopic).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "chat_id", "id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    async def get_forum_topics(self, chat_id: int, *, account_id: int | None = None) -> list[dict[str, Any]]:
        """Get all forum topics for a chat, with message count per topic.

        None account_id = unscoped until phase 4.
        """
        async with self.db_manager.async_session_factory() as session:
            # Aggregate on the RAW topic column so idx_messages_topic
            # (chat_id, reply_to_top_id, date) can drive a covering scan —
            # grouping on coalesce(reply_to_top_id, 1) forced a temp b-tree
            # and dragged every message row (raw_data included) off the heap:
            # 17.4ms -> 2.4ms at 60k rows, and O(index slice) memory. The
            # NULL bucket (pre-v6.2.0 and pre-forum messages, which Telegram
            # shows under General) is folded into topic 1 in Python below.
            msg_where = [Message.chat_id == chat_id]
            if account_id is not None:
                msg_where.append(Message.account_id == account_id)
            agg_stmt = (
                select(
                    Message.reply_to_top_id,
                    func.count().label("message_count"),
                    func.max(Message.date).label("last_message_date"),
                )
                .where(and_(*msg_where))
                .group_by(Message.reply_to_top_id)
            )
            message_counts: dict[int, int] = {}
            last_dates: dict[int, Any] = {}
            for topic_id, message_count, last_date in await session.execute(agg_stmt):
                key = 1 if topic_id is None else topic_id
                message_counts[key] = message_counts.get(key, 0) + message_count
                if last_date is not None and (key not in last_dates or last_date > last_dates[key]):
                    last_dates[key] = last_date

            topic_stmt = select(ForumTopic).where(ForumTopic.chat_id == chat_id)
            if account_id is not None:
                topic_stmt = topic_stmt.where(ForumTopic.account_id == account_id)

            result = await session.execute(topic_stmt)
            topics = []
            for topic in result.scalars():
                topics.append(
                    {
                        "id": topic.id,
                        "chat_id": topic.chat_id,
                        "title": topic.title,
                        "icon_color": topic.icon_color,
                        "icon_emoji_id": topic.icon_emoji_id,
                        "icon_emoji": topic.icon_emoji,
                        "is_closed": topic.is_closed,
                        "is_pinned": topic.is_pinned,
                        "is_hidden": topic.is_hidden,
                        "date": topic.date,
                        "message_count": message_counts.get(topic.id, 0),
                        "last_message_date": last_dates.get(topic.id),
                    }
                )
            # Same order the SQL used to produce: pinned first, then newest
            # last-message first with never-posted topics at the end.
            topics.sort(
                key=lambda entry: (
                    bool(entry["is_pinned"]),
                    entry["last_message_date"] is not None,
                    entry["last_message_date"] or datetime.min,
                ),
                reverse=True,
            )
            return topics

    # ========== Chat Folder Operations (v6.2.0) ==========

    @retry_on_locked()
    async def upsert_chat_folder(self, folder_data: dict[str, Any], *, account_id: int) -> None:
        """Insert or update a chat folder."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "account_id": account_id,
                "id": folder_data["id"],
                "title": folder_data["title"],
                "emoticon": folder_data.get("emoticon"),
                "sort_order": folder_data.get("sort_order", 0),
                "updated_at": utcnow_naive(),
            }

            update_set = {
                "title": values["title"],
                "emoticon": values["emoticon"],
                "sort_order": values["sort_order"],
                "updated_at": utcnow_naive(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_set)
            else:
                stmt = pg_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    # Flag-based folders can now resolve to very large member sets, so the
    # existence check is chunked to stay well under driver bind-parameter caps
    # (SQLite ~32766, PostgreSQL 65535).
    _FOLDER_MEMBER_CHUNK = 500

    @retry_on_locked()
    async def sync_folder_members(self, folder_id: int, chat_ids: list[int], *, account_id: int) -> None:
        """Sync folder membership: replace all members for one account's folder.

        Folder ids start at 2 for every account, so the replace-all delete must
        name the account or it wipes the other account's identically-numbered
        folder.
        """
        async with self.db_manager.async_session_factory() as session:
            # Delete existing members
            await session.execute(
                delete(ChatFolderMember).where(
                    and_(ChatFolderMember.account_id == account_id, ChatFolderMember.folder_id == folder_id)
                )
            )

            # Insert new members (only for chats that exist in our DB)
            if chat_ids:
                # Dedup while preserving order; verify existence in bounded chunks.
                unique_ids = list(dict.fromkeys(chat_ids))
                existing_ids: set[int] = set()
                for i in range(0, len(unique_ids), self._FOLDER_MEMBER_CHUNK):
                    chunk = unique_ids[i : i + self._FOLDER_MEMBER_CHUNK]
                    result = await session.execute(
                        select(Chat.id).where(and_(Chat.account_id == account_id, Chat.id.in_(chunk)))
                    )
                    existing_ids.update(row[0] for row in result)

                for cid in unique_ids:
                    if cid in existing_ids:
                        session.add(ChatFolderMember(account_id=account_id, folder_id=folder_id, chat_id=cid))

            await session.commit()

    async def get_all_folders(
        self, allowed_chat_ids: set[int] | None = None, *, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Get all chat folders with their chat counts.

        Only folders that contain at least one backed-up (and, for restricted
        viewers, accessible) chat are returned. The viewer reflects the archive,
        not the full Telegram account: a folder whose chats were all excluded
        from backup — or that is empty on Telegram — would otherwise show as an
        empty filter tab that returns nothing when clicked (#208). Folder
        membership is already limited to chats present in our DB by
        sync_folder_members, so a zero count means "nothing archived here".

        Args:
            allowed_chat_ids: If set, only count chats the user can access.
            account_id: If set, only this account's folders (None = unscoped until phase 4).
        """
        async with self.db_manager.async_session_factory() as session:
            count_q = select(ChatFolderMember.folder_id, func.count(ChatFolderMember.chat_id).label("chat_count"))
            if allowed_chat_ids is not None:
                # Same rule as ChatScope.sql_predicates: an empty grant is
                # "nothing", never "no filter". SQLAlchemy 2.0 does render an
                # empty IN as an always-false expression, but an access-control
                # filter must not rest on how the ORM renders an edge case.
                count_q = count_q.where(ChatFolderMember.chat_id.in_(allowed_chat_ids) if allowed_chat_ids else false())
            if account_id is not None:
                count_q = count_q.where(ChatFolderMember.account_id == account_id)
            count_subq = count_q.group_by(ChatFolderMember.folder_id).subquery()

            stmt = (
                select(ChatFolder, count_subq.c.chat_count)
                .outerjoin(count_subq, ChatFolder.id == count_subq.c.folder_id)
                .order_by(ChatFolder.sort_order, ChatFolder.title)
            )
            if account_id is not None:
                stmt = stmt.where(ChatFolder.account_id == account_id)

            result = await session.execute(stmt)
            folders = []
            for row in result:
                folder = row.ChatFolder
                count = row.chat_count or 0
                # Hide folders with no backed-up chats (empty tabs help no one)
                if count == 0:
                    continue
                folders.append(
                    {
                        "id": folder.id,
                        "title": folder.title,
                        "emoticon": folder.emoticon,
                        "sort_order": folder.sort_order,
                        "chat_count": count,
                    }
                )
            return folders

    async def get_chats_for_folder_resolution(self, *, account_id: int) -> list[dict[str, Any]]:
        """Return one account's archived chats with the facts needed to evaluate a
        folder's category flags: id, type, whether it is a bot, and archived state.

        Account-scoped because the result becomes folder membership WRITES for
        this account's folders — another account's chats must never be swept in.

        Bot-ness is only meaningful for private chats and is read from the users
        table (chats store bots as type ``private``). The join is on ``User.id ==
        Chat.id`` — a private chat's id is the positive user id, while group and
        channel ids are negative/marked and can never collide with a user id, so
        they always resolve to ``is_bot = 0``.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Chat.id,
                    Chat.type,
                    Chat.is_archived,
                    func.coalesce(User.is_bot, 0).label("is_bot"),
                )
                .outerjoin(User, User.id == Chat.id)
                .where(Chat.account_id == account_id)
            )
            result = await session.execute(stmt)
            return [
                {
                    "id": row.id,
                    "type": row.type,
                    "is_bot": bool(row.is_bot),
                    "is_archived": bool(row.is_archived),
                }
                for row in result
            ]

    @retry_on_locked()
    async def cleanup_stale_folders(self, active_folder_ids: list[int], *, account_id: int) -> None:
        """Remove one account's folders that no longer exist in Telegram.

        ``active_folder_ids`` comes from ONE account's dialog filters, so the
        NOT IN sweep must stay inside that account — unscoped it deletes every
        other account's folders wholesale (their ids are never in this list).
        """
        async with self.db_manager.async_session_factory() as session:
            if active_folder_ids:
                await session.execute(
                    delete(ChatFolder).where(
                        and_(ChatFolder.account_id == account_id, ChatFolder.id.notin_(active_folder_ids))
                    )
                )
            else:
                await session.execute(delete(ChatFolder).where(ChatFolder.account_id == account_id))
            await session.commit()

    async def get_archived_chat_count(self, *, account_id: int | None = None) -> int:
        """Get the count of archived chats (None account_id = unscoped until phase 4)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.count(Chat.id)).where(Chat.is_archived == 1)
            if account_id is not None:
                stmt = stmt.where(Chat.account_id == account_id)
            result = await session.execute(stmt)
            return result.scalar() or 0

    # ========================================================================
    # Viewer Account Management (v7.0.0)
    # ========================================================================

    @retry_on_locked()
    async def create_viewer_account(
        self,
        username: str,
        password_hash: str,
        salt: str,
        allowed_chat_ids: str | None = None,
        created_by: str | None = None,
        is_active: int = 1,
        no_download: int = 0,
        allowed_accounts: str | None = None,
        allowed_chat_refs: str | None = None,
    ) -> dict[str, Any]:
        """Create a new viewer account. Returns the created account dict.

        ``allowed_accounts``/``allowed_chat_refs`` are the v8.0.0 grant columns
        (JSON lists, NULL = unrestricted). ``allowed_chat_ids`` survives as
        rollback data only: 8.0 code never reads it, and restricted callers
        write ``"[]"`` there so a 7.x rollback denies instead of failing open.
        """
        async with self.db_manager.async_session_factory() as session:
            account = ViewerAccount(
                username=username,
                password_hash=password_hash,
                salt=salt,
                allowed_chat_ids=allowed_chat_ids,
                allowed_accounts=allowed_accounts,
                allowed_chat_refs=allowed_chat_refs,
                created_by=created_by,
                is_active=is_active,
                no_download=no_download,
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return self._viewer_account_to_dict(account)

    async def get_viewer_account(self, account_id: int) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            return self._viewer_account_to_dict(account) if account else None

    async def get_viewer_by_username(self, username: str) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.username == username))
            account = result.scalar_one_or_none()
            return self._viewer_account_to_dict(account) if account else None

    async def get_all_viewer_accounts(self) -> list[dict[str, Any]]:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).order_by(ViewerAccount.created_at.desc()))
            return [self._viewer_account_to_dict(a) for a in result.scalars().all()]

    @retry_on_locked()
    async def update_viewer_account(self, account_id: int, **kwargs) -> dict[str, Any] | None:
        """Update viewer account fields. Returns updated account or None if not found."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            if not account:
                return None
            for key, value in kwargs.items():
                if hasattr(account, key):
                    setattr(account, key, value)
            account.updated_at = utcnow_naive()
            await session.commit()
            await session.refresh(account)
            return self._viewer_account_to_dict(account)

    @retry_on_locked()
    async def delete_viewer_account(self, account_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerAccount).where(ViewerAccount.id == account_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _viewer_account_to_dict(account: ViewerAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "username": account.username,
            "password_hash": account.password_hash,
            "salt": account.salt,
            "allowed_chat_ids": account.allowed_chat_ids,
            "allowed_accounts": account.allowed_accounts,
            "allowed_chat_refs": account.allowed_chat_refs,
            "is_active": account.is_active,
            "no_download": account.no_download,
            "created_by": account.created_by,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        }

    # ========================================================================
    # Viewer Audit Log (v7.0.0)
    # ========================================================================

    @retry_on_locked()
    async def create_audit_log(
        self,
        username: str,
        role: str,
        action: str,
        endpoint: str | None = None,
        chat_id: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        # Clamp to the declared column widths. The two backends disagree about
        # over-long values: SQLite ignores VARCHAR lengths, PostgreSQL raises
        # SQLSTATE 22001 and the row is never written. Every caller wraps this in
        # a bare `except Exception: logger.warning(...)`, so on PostgreSQL a
        # failed login with a 300-character username left NO audit record at all
        # while the same attack was fully logged on SQLite. NUL bytes kill the
        # insert the same way (_strip_nul), including in the width-less
        # user_agent Text column. A scrubbed audit row beats a missing one.
        async with self.db_manager.async_session_factory() as session:
            entry = ViewerAuditLog(
                username=_clamp(username, 255),
                role=_clamp(role, 20),
                action=_clamp(action, 100),
                endpoint=_clamp(endpoint, 255),
                chat_id=chat_id,
                ip_address=_clamp(ip_address, 45),
                user_agent=_strip_nul(user_agent),
            )
            session.add(entry)
            await session.commit()

    async def get_audit_logs(
        self, limit: int = 100, offset: int = 0, username: str | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.db_manager.async_session_factory() as session:
            stmt = select(ViewerAuditLog).order_by(ViewerAuditLog.created_at.desc())
            if username:
                stmt = stmt.where(ViewerAuditLog.username == username)
            if action:
                stmt = stmt.where(ViewerAuditLog.action.startswith(action))
            stmt = stmt.limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [
                {
                    "id": log.id,
                    "username": log.username,
                    "role": log.role,
                    "action": log.action,
                    "endpoint": log.endpoint,
                    "chat_id": log.chat_id,
                    "ip_address": log.ip_address,
                    "user_agent": log.user_agent,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in result.scalars().all()
            ]

    # ========================================================================
    # Viewer Sessions (v7.1.0 - persistent sessions)
    # ========================================================================

    @retry_on_locked()
    async def save_session(
        self,
        token: str,
        username: str,
        role: str,
        allowed_chat_ids: str | None,
        created_at: float,
        last_accessed: float,
        no_download: int = 0,
        source_token_id: int | None = None,
        allowed_accounts: str | None = None,
        allowed_chat_refs: str | None = None,
    ) -> None:
        """Save or update a session in the database.

        ``allowed_accounts``/``allowed_chat_refs`` carry the v8.0.0 grant;
        ``allowed_chat_ids`` is the 7.x rollback tombstone ("[]" for restricted
        sessions, NULL for unrestricted) and is never read back by 8.0 code.
        """
        async with self.db_manager.async_session_factory() as session:
            values = {
                "token": token,
                "username": username,
                "role": role,
                "allowed_chat_ids": allowed_chat_ids,
                "allowed_accounts": allowed_accounts,
                "allowed_chat_refs": allowed_chat_refs,
                "no_download": no_download,
                "source_token_id": source_token_id,
                "created_at": created_at,
                "last_accessed": last_accessed,
            }
            if self._is_sqlite:
                stmt = sqlite_insert(ViewerSession).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["token"],
                    set_={"last_accessed": last_accessed},
                )
            else:
                stmt = pg_insert(ViewerSession).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["token"],
                    set_={"last_accessed": last_accessed},
                )
            await session.execute(stmt)
            await session.commit()

    async def get_session(self, token: str) -> dict[str, Any] | None:
        """Get a session by token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerSession).where(ViewerSession.token == token))
            row = result.scalar_one_or_none()
            return self._viewer_session_to_dict(row) if row else None

    async def load_all_sessions(self) -> list[dict[str, Any]]:
        """Load all sessions from the database (used on startup)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerSession))
            return [self._viewer_session_to_dict(s) for s in result.scalars().all()]

    @retry_on_locked()
    async def delete_session(self, token: str) -> bool:
        """Delete a single session by token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.token == token))
            await session.commit()
            return result.rowcount > 0

    @retry_on_locked()
    async def delete_user_sessions(self, username: str) -> int:
        """Delete all sessions for a given username. Returns count deleted."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.username == username))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def cleanup_expired_sessions(self, max_age_seconds: float) -> int:
        """Delete all expired sessions. Returns count deleted."""
        import time

        cutoff = time.time() - max_age_seconds
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.created_at < cutoff))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def delete_sessions_by_source_token_id(self, token_id: int) -> int:
        """Delete all sessions created from a specific share token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.source_token_id == token_id))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def delete_push_subscriptions_for_username(self, *, username: str) -> int:
        """Delete every push subscription owned by ``username``. Returns count deleted.

        The revocation half of push ownership: a subscription is a delivery
        channel that survives the session that created it, so every path that
        invalidates a principal's sessions deletes its push rows through here.
        Writes push_subscriptions and nothing else.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(PushSubscription).where(PushSubscription.username == username))
            await session.commit()
            return result.rowcount

    @staticmethod
    def _viewer_session_to_dict(row: ViewerSession) -> dict[str, Any]:
        return {
            "token": row.token,
            "username": row.username,
            "role": row.role,
            "allowed_chat_ids": row.allowed_chat_ids,
            "allowed_accounts": row.allowed_accounts,
            "allowed_chat_refs": row.allowed_chat_refs,
            "no_download": row.no_download,
            "source_token_id": row.source_token_id,
            "created_at": row.created_at,
            "last_accessed": row.last_accessed,
        }

    # ========================================================================
    # Viewer Tokens (v7.2.0 - share tokens)
    # ========================================================================

    @retry_on_locked()
    async def create_viewer_token(
        self,
        label: str | None,
        token_hash: str,
        token_salt: str,
        created_by: str,
        allowed_chat_ids: str,
        no_download: int = 0,
        expires_at: datetime | None = None,
        allowed_accounts: str | None = None,
        allowed_chat_refs: str | None = None,
    ) -> dict[str, Any]:
        """Create a new share token. Returns the created token dict.

        ``allowed_chat_refs`` is the v8.0.0 grant; ``allowed_chat_ids`` (a NOT
        NULL column) takes the "[]" rollback tombstone so a 7.x binary reading
        this row denies rather than fails open. 8.0 code never reads it.
        """
        async with self.db_manager.async_session_factory() as session:
            token = ViewerToken(
                label=label,
                token_hash=token_hash,
                token_salt=token_salt,
                created_by=created_by,
                allowed_chat_ids=allowed_chat_ids,
                allowed_accounts=allowed_accounts,
                allowed_chat_refs=allowed_chat_refs,
                no_download=no_download,
                expires_at=expires_at,
            )
            session.add(token)
            await session.commit()
            await session.refresh(token)
            return self._viewer_token_to_dict(token)

    async def get_all_viewer_tokens(self) -> list[dict[str, Any]]:
        """Get all tokens (for admin panel)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).order_by(ViewerToken.created_at.desc()))
            return [self._viewer_token_to_dict(t) for t in result.scalars().all()]

    async def verify_viewer_token(self, plaintext_token: str) -> dict[str, Any] | None:
        """Verify a plaintext token against stored hashes. Returns token dict or None.

        The PBKDF2 derivations (600k rounds, ~50ms per stored token) run in ONE
        worker thread: derived inline they stalled the shared event loop for
        the whole scan on every auth attempt, freezing every concurrent viewer
        request — the same rule _hash_token's docstring states for its callers.
        Only the pure-CPU scan moves off the loop; the material is snapshotted
        first, so no ORM object is ever touched from the thread, and the
        session (row update + commit) stays on the loop.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).where(ViewerToken.is_revoked == 0))
            now = utcnow_naive()
            candidates = [
                record for record in result.scalars().all() if not (record.expires_at and record.expires_at < now)
            ]
            material = [(bytes.fromhex(r.token_salt), r.token_hash) for r in candidates]

            def derive_match() -> int | None:
                encoded = plaintext_token.encode()
                for index, (salt, expected) in enumerate(material):
                    computed = hashlib.pbkdf2_hmac("sha256", encoded, salt, 600_000).hex()
                    if secrets.compare_digest(computed, expected):
                        return index
                return None

            match_index = await asyncio.to_thread(derive_match)
            if match_index is None:
                return None
            # The worker-thread yield is wide (~50ms per stored token), so the
            # matched row may have been revoked or expired meanwhile. The
            # update re-checks both conditions in SQL and increments use_count
            # atomically; zero rows updated means the token died mid-scan and
            # must not authenticate.
            record = candidates[match_index]
            now = utcnow_naive()
            result = await session.execute(
                update(ViewerToken)
                .where(
                    ViewerToken.id == record.id,
                    ViewerToken.is_revoked == 0,
                    or_(ViewerToken.expires_at.is_(None), ViewerToken.expires_at >= now),
                )
                .values(last_used_at=now, use_count=func.coalesce(ViewerToken.use_count, 0) + 1)
            )
            if result.rowcount == 0:
                await session.rollback()
                return None
            await session.commit()
            await session.refresh(record)
            return self._viewer_token_to_dict(record)

    @retry_on_locked()
    async def update_viewer_token(self, token_id: int, **kwargs) -> dict[str, Any] | None:
        """Update token fields. Supports: label, allowed_chat_ids, is_revoked, no_download."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).where(ViewerToken.id == token_id))
            token = result.scalar_one_or_none()
            if not token:
                return None
            allowed_fields = {
                "label",
                "allowed_chat_ids",
                "allowed_accounts",
                "allowed_chat_refs",
                "is_revoked",
                "no_download",
            }
            for key, value in kwargs.items():
                if key in allowed_fields:
                    setattr(token, key, value)
            await session.commit()
            await session.refresh(token)
            return self._viewer_token_to_dict(token)

    @retry_on_locked()
    async def delete_viewer_token(self, token_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerToken).where(ViewerToken.id == token_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _viewer_token_to_dict(token: ViewerToken) -> dict[str, Any]:
        return {
            "id": token.id,
            "label": token.label,
            "token_hash": token.token_hash,
            "token_salt": token.token_salt,
            "created_by": token.created_by,
            "allowed_chat_ids": token.allowed_chat_ids,
            "allowed_accounts": token.allowed_accounts,
            "allowed_chat_refs": token.allowed_chat_refs,
            "is_revoked": token.is_revoked,
            "no_download": token.no_download,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "use_count": token.use_count,
            "created_at": token.created_at.isoformat() if token.created_at else None,
        }

    # ========================================================================
    # App Settings (v7.2.0 - key-value store)
    # ========================================================================

    @retry_on_locked()
    async def set_setting(self, key: str, value: str) -> None:
        """Set a key-value setting (upsert)."""
        async with self.db_manager.async_session_factory() as session:
            if self._is_sqlite:
                stmt = sqlite_insert(AppSettings).values(key=key, value=value, updated_at=utcnow_naive())
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_at": utcnow_naive()},
                )
            else:
                stmt = pg_insert(AppSettings).values(key=key, value=value, updated_at=utcnow_naive())
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_at": utcnow_naive()},
                )
            await session.execute(stmt)
            await session.commit()

    async def get_setting(self, key: str) -> str | None:
        """Get a setting value by key. Returns None if not found."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(AppSettings).where(AppSettings.key == key))
            row = result.scalar_one_or_none()
            return row.value if row else None

    async def get_all_settings(self) -> dict[str, str]:
        """Get all settings as a dict."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(AppSettings))
            return {row.key: row.value for row in result.scalars().all()}

    async def close(self) -> None:
        """Close database connections."""
        await self.db_manager.close()
