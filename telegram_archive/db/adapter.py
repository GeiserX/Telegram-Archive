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
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from typing import Any

from sqlalchemy import (
    and_,
    case,
    delete,
    desc,
    exists,
    false,
    func,
    insert,
    literal,
    literal_column,
    not_,
    nulls_last,
    or_,
    select,
    text,
    true,
    tuple_,
    union,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import aliased

from ..message_utils import (
    MEDIA_PAYLOAD_KEYS,
    METADATA_ONLY_MEDIA_TYPES,
    PAYLOAD_BACKFILL_TYPES,
    compute_directory_size,
    merge_geo_live,
    resolve_sender_display_name,
    stored_media_file_id,
    utcnow_naive,
)
from ..transcription_contract import TRANSCRIBABLE_DOCUMENT_MIME_PREFIXES, TRANSCRIBABLE_TYPES
from .base import DatabaseManager
from .fts import (
    PG_TRANSCRIPT_TSQUERY_FROM_SEARCH,
    PG_TSQUERY_FROM_SEARCH,
    PG_TSVECTOR_COLUMN,
    SQLITE_FTS_TABLE,
    SQLITE_TRANSCRIPT_FTS_TABLE,
    fts_match_query,
    search_has_words,
)
from .models import (
    DEFAULT_ACCOUNT_ID,
    PRIVATE_CHAT_TYPE,
    TRANSCRIPT_OPEN_STATUSES,
    Account,
    AppSettings,
    AvatarHistory,
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    MediaTranscript,
    MediaVersion,
    Message,
    MessageVersion,
    Metadata,
    PushSubscription,
    Reaction,
    ReactionHistory,
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

# The order both exports list messages in, unique per message across chats
# and accounts. The messages query and the queries of their versions, media
# and earlier media all sort by it, so those rows can be walked beside the
# messages (``_ExportWalk``); written once so they cannot drift.
EXPORT_MESSAGE_ORDER = (Message.date.asc(), Message.account_id.asc(), Message.chat_id.asc(), Message.id.asc())


class _ExportWalk:
    """The rows of one statement, read beside the export's messages.

    The statement sorts by ``EXPORT_MESSAGE_ORDER`` and names each row's
    message by ``account_id``, ``chat_id`` and ``message_id``. ``take`` hands
    over the rows of one message and reads no further than the next
    message's first row, so only one message's rows are in memory at a time.
    """

    def __init__(self, result) -> None:
        self._result = result
        self.pending = None

    @classmethod
    async def open(cls, session, stmt) -> _ExportWalk:
        walk = cls(await session.stream(stmt))
        walk.pending = await anext(walk._result, None)
        return walk

    @staticmethod
    def _key(row) -> tuple[int, int, int]:
        return (row.account_id, row.chat_id, row.message_id)

    async def take(self, key: tuple[int, int, int]) -> list:
        rows = []
        while self.pending is not None and self._key(self.pending) == key:
            rows.append(self.pending)
            self.pending = await anext(self._result, None)
        return rows


# Two accounts that saw one reaction drop record it a little apart: each one's
# listener or backup notices it on its own clock. Within this many seconds, the
# same emoji going from the same count to the same count is one event in What
# changed; further apart, it is two.
REACTION_EVENT_TOLERANCE_SECONDS = 900

# Media transcripts (032). ``status`` only advances along this rank; a row at
# a terminal status is never written again.
TRANSCRIPT_STATUS_RANK = {"queued": 0, "running": 1, "done": 2, "failed": 2, "skipped": 2}
TRANSCRIPT_TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})
# Failure reasons about the archive's copy of the file, and about the server
# rather than the file: a repair of the disk or of the server makes them go
# away, so they say nothing about whether the audio can be transcribed.
# akou's own rule agrees: only ``decode_failed`` and ``too_long`` are the
# caller's file, the rest is the server's.
TRANSCRIPT_FILE_ERRORS = frozenset({"file_missing", "file_unreadable"})
TRANSCRIPT_SERVER_ERRORS = frozenset(
    {
        "engine_unavailable",
        "models_missing",
        "model_download_failed",
        "not_found",
        "expired",
        "invalid_job",
        "invalid_json",
    }
)
TRANSCRIPT_ENVIRONMENT_ERRORS = TRANSCRIPT_FILE_ERRORS | TRANSCRIPT_SERVER_ERRORS
# The drain retries a media whose newest row failed while it has fewer than
# this many failed rows for any other reason (about the file's content or the
# request)...
TRANSCRIPT_MAX_FAILED_ROWS = 3
# ...and fewer than this many failed rows in all, which ends a file the server
# keeps failing on while it finishes others.
TRANSCRIPT_MAX_ANY_FAILED_ROWS = 10
# A server failure with no transcript finished since is retried by one probe a
# drain: at once after the media's first failed row, then once the newest is
# older than this, doubled for each failed row after the second.
TRANSCRIPT_PROBE_WAIT = timedelta(hours=1)
TRANSCRIPT_JSON_COLUMNS = frozenset({"models", "words", "segments"})
TRANSCRIPT_FILL_COLUMNS = frozenset(
    {
        "content_hash",
        "idempotency_key",
        "source",
        "engine_name",
        "engine_version",
        "preset",
        "models",
        "language",
        "language_confidence",
        "text",
        "words",
        "segments",
        "confidence",
        "duration_s",
        "job_id",
        "error",
        "completed_at",
        "copied_from_id",
        "diarize",
        "options_tag",
    }
)
# app_settings keys the backup writes for the viewer's settings row and for
# the akou event feed; both appear in the master-only settings dump, neither
# is a secret.
TRANSCRIPTION_EVENTS_CURSOR_KEY = "transcription.events_cursor"
TRANSCRIPTION_SERVER_KEY = "transcription.server"


def _json_list(value: str | None) -> list:
    """A JSON-text column as a list; anything unreadable is an empty list."""
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except ValueError, TypeError:
        return []
    return loaded if isinstance(loaded, list) else []


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
    """True when a serialised raw_data blob carries anything worth keeping.

    An empty formatting list alone (``{"entities": []}``, a read of a message
    with no formatting and no other extras, since 9.0) is no payload either:
    it must not replace extras another writer archived, just as ``"{}"`` does not.
    """
    if not value or value == "{}":
        return False
    raw = _raw_data_dict(value)
    return raw is None or any(key != "entities" or entity_list != [] for key, entity_list in raw.items())


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


# The cached statistics blob's per-chat map, keyed by account AND chat since
# 8.12.2. A NEW name rather than a new shape under the old one: a blob written
# before this is keyed by bare chat id and cannot say which account a count
# belongs to, so a reader must be able to tell the two apart and refuse the old
# one rather than guess. Nothing in the viewer reads the map; it exists so a
# restricted principal's totals can be computed from its own chats.
PER_ACCOUNT_CHAT_COUNTS_KEY = "per_account_chat_message_counts"

# The pre-8.12.2 name, still arriving from a cached blob on upgrade.
LEGACY_CHAT_COUNTS_KEY = "per_chat_message_counts"

# Downloaded media per chat, count and bytes, under the same "<account>:<chat>"
# keys, so a restricted principal's media totals come from its own chats too.
# A blob written before these existed lacks them, and the viewer then omits the
# media figures for that principal rather than guessing.
PER_ACCOUNT_CHAT_MEDIA_COUNTS_KEY = "per_account_chat_media_counts"
PER_ACCOUNT_CHAT_MEDIA_BYTES_KEY = "per_account_chat_media_bytes"


def account_chat_stats_key(account_id: int, chat_id: int) -> str:
    """``(account, chat)`` as one JSON object key.

    JSON has no tuple keys, and a chat id is frequently negative, so the
    separator has to be one that cannot appear inside either number.
    """
    return f"{int(account_id)}:{int(chat_id)}"


def parse_account_chat_stats_key(key: Any) -> tuple[int, int] | None:
    """Read one key back, or None when it is not one of ours.

    None is what makes the upgrade safe: every key of a pre-8.12.2 blob parses
    to None, so a restricted principal scopes to nothing instead of reading
    counts it cannot attribute to an account.
    """
    if not isinstance(key, str):
        return None
    account, separator, chat = key.partition(":")
    if not separator:
        return None
    try:
        return int(account), int(chat)
    except ValueError:
        return None


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

    def sql_predicates(self, entity=Chat) -> list[Any]:
        """The same three rules as WHERE-clause fragments against ``chats``.

        ``entity`` is the chats table the predicates address — ``Chat`` for the
        query's own row, or an ``aliased(Chat)`` when the rules have to be
        re-asked about a DIFFERENT copy of the same chat (see
        :meth:`displayed_copy_predicate`). Same three rules either way.
        """
        predicates: list[Any] = []
        for column, grant in ((entity.id, self.ids), (entity.account_id, self.accounts), (entity.ref, self.refs)):
            if grant is None:
                continue
            # An empty grant is "nothing", never "no filter".
            predicates.append(column.in_(grant) if grant else false())
        return predicates

    def displayed_copy_predicate(self, entity=Chat) -> Any:
        """True for the ONE copy of a chat this scope shows (8.12 chat folding).

        Since 8.0 several accounts archive into one database, so the same
        Telegram chat can exist once per account — same ``chats.id``, one row
        each, each with its own ``ref``. For a channel, a supergroup or a group
        that is genuinely the SAME conversation seen twice, listing both copies
        shows the operator a duplicate; the viewer folds them into one row and
        names the accounts instead.

        A private chat is NEVER folded. Its ``chats.id`` is the other person's
        user id, so "account A's conversation with X" and "account B's
        conversation with X" share an id while being two different
        conversations with different messages. Merging them would invent a
        thread that never existed.

        The surviving copy is the one belonging to the LOWEST account id this
        scope is entitled to. Lowest rather than newest/busiest because it must
        not change as messages arrive: the copy that survives owns the ``ref``
        the viewer deep-links, subscribes and addresses media with, and a ref
        that flips under the user breaks every one of those.

        The grant is re-applied to the lower copy on purpose. Folding may only
        consider accounts the principal may actually see, so a viewer entitled
        to account 2 alone keeps seeing account 2's copy — nothing is hidden
        behind a row it has no right to.
        """
        lower_copy = aliased(Chat, name="lower_account_copy")
        shared = (
            select(literal(1))
            .select_from(lower_copy)
            .where(
                lower_copy.id == entity.id,
                lower_copy.account_id < entity.account_id,
                lower_copy.type != PRIVATE_CHAT_TYPE,
            )
        )
        for predicate in self.sql_predicates(lower_copy):
            shared = shared.where(predicate)
        return or_(entity.type == PRIVATE_CHAT_TYPE, ~shared.correlate(entity).exists())


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


def _formatted_message_version_hash(
    chat_id: int,
    message_id: int,
    text: str | None,
    date: datetime,
    entities: list | None,
) -> str:
    """The dedup identity of a version whose frozen hash is already taken by other formatting.

    Two edits in one second that change only the formatting leave versions
    with the same text and the same date, so ``_message_version_hash`` gives
    both one identity. Only then is the version written under this hash,
    which adds its entities. Rows written under the frozen hash keep it, so
    its meaning for every stored row stays the same.
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "date": _datetime_hash_value(date),
        "entities": entities or None,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_preview_refresh(row_type: str | None, new_type: str | None) -> bool:
    """True when a message's archived media and the media read now are both link previews.

    A preview's photo or document is Telegram's picture of the linked page.
    Telegram crawls the page again and can serve another one for a message
    nobody edited, so its id says nothing about an edit. A new preview comes
    with a new link, which is an edit of the text and is kept as one.
    """
    return row_type == "webpage" and new_type == "webpage"


def _raw_data_dict(raw_data: Any) -> dict | None:
    """``raw_data`` as a dict, from its JSON string or a dict; None when it is not one."""
    if isinstance(raw_data, dict):
        return raw_data
    if not raw_data:
        return {}
    try:
        parsed = json.loads(raw_data)
    except ValueError, TypeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# The payload keys a reply quote names when the target has no media row, in
# the order the viewer's cards pick them.
_REPLY_CARD_KINDS = ("geo_live", "venue", "geo", "contact")


def _reply_card_kind(raw_data: Any) -> tuple[str | None, str | None]:
    """The card kind a reply target's ``raw_data`` holds, and a venue's title."""
    raw = _raw_data_dict(raw_data)
    if not raw:
        return None, None
    for kind in _REPLY_CARD_KINDS:
        payload = raw.get(kind)
        if isinstance(payload, dict):
            title = payload.get("title") if kind == "venue" else None
            return kind, title if isinstance(title, str) and title else None
    return None, None


def _media_payloads_of(raw_data: Any) -> dict[str, Any]:
    """The media payloads in ``raw_data`` (``MEDIA_PAYLOAD_KEYS``), keyed as stored."""
    raw = _raw_data_dict(raw_data)
    if not raw:
        return {}
    return {key: raw[key] for key in MEDIA_PAYLOAD_KEYS if key in raw}


# How much of a message the chat list's second line carries. The line shows
# about forty characters on a phone; the rest is room for a wide sidebar.
CHAT_PREVIEW_TEXT_LENGTH = 100


def _preview_text(text: str | None) -> str | None:
    """``text`` on one line, cut to ``CHAT_PREVIEW_TEXT_LENGTH`` with an ellipsis."""
    if not isinstance(text, str):
        return None
    folded = " ".join(text.split())
    if not folded:
        return None
    if len(folded) > CHAT_PREVIEW_TEXT_LENGTH:
        return folded[:CHAT_PREVIEW_TEXT_LENGTH].rstrip() + "\u2026"
    return folded


def _chat_preview(row: Any, chat_type: str | None) -> dict[str, Any]:
    """One chat-list preview from a row of ``_attach_chat_previews``' query."""
    raw = _raw_data_dict(row.raw_data) or {}
    text = _preview_text(row.text)
    outgoing = bool(row.is_outgoing)
    action = None
    action_title = None
    if raw.get("service_type") == "service":
        kind = "service"
        if text is None:
            action = raw.get("action_type") if isinstance(raw.get("action_type"), str) else None
            action_title = raw.get("new_title") if isinstance(raw.get("new_title"), str) else None
    elif isinstance(raw.get("poll"), dict):
        kind = "poll"
        if text is None:
            text = _preview_text(raw["poll"].get("question"))
    elif row.media_type:
        kind = row.media_type
    elif _reply_card_kind(raw)[0] is not None:
        # The listener writes no media row for a location or a contact; its
        # payload names the kind, as it does for a reply quote.
        kind = _reply_card_kind(raw)[0]
    elif text is not None:
        kind = "text"
    else:
        kind = "message"

    sender = None
    if kind != "service" and chat_type != "channel":
        if outgoing:
            sender = "You"
        elif chat_type != PRIVATE_CHAT_TYPE:
            first_name = row.first_name.strip() if isinstance(row.first_name, str) else ""
            sender = first_name or resolve_sender_display_name(row.sender_name, None, row.last_name, row.username)
    return {
        "message_id": row.id,
        "date": row.date,
        "text": text,
        "sender": sender,
        "kind": kind,
        "outgoing": outgoing,
        "action": action,
        "action_title": action_title,
    }


def _json_or_none(value: str | None) -> Any:
    """A JSON column's value, or None when it is empty or unreadable."""
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError, TypeError:
        return None


def _formatting_of(raw_data: Any) -> list | None:
    """The formatting entities in ``raw_data``, or None when it has none."""
    raw = _raw_data_dict(raw_data)
    entities = raw.get("entities") if raw else None
    return entities if isinstance(entities, list) and entities else None


def _rich_message_of(raw_data: Any) -> dict | None:
    """The Rich Text Editor block tree in ``raw_data`` (#470), or None when it has none."""
    raw = _raw_data_dict(raw_data)
    rich = raw.get("rich_message") if raw else None
    return rich if isinstance(rich, dict) and rich else None


def _formatting_state(raw_data: Any) -> tuple[list | None, dict | None]:
    """Everything in ``raw_data`` that describes the text's formatting: entities and block tree."""
    return _formatting_of(raw_data), _rich_message_of(raw_data)


def _known_formatting_changes(
    archived_raw_data: Any, entities: list | None, rich_message: dict | None
) -> tuple[bool, bool]:
    """Whether a read's entities and block tree differ from what the archive knows.

    Only a key the archived ``raw_data`` has is compared. An absent key is
    unknown, not "no formatting": messages archived before formatting was
    captured (``entities`` since 8.3.0, ``rich_message`` since #470) have
    none, and a message without formatting has none either. A difference
    against an unknown key is no edit; the key is filled instead.
    """
    raw = _raw_data_dict(archived_raw_data)
    if raw is None:
        return False, False
    entities_changed = "entities" in raw and (entities or None) != _formatting_of(raw)
    rich_changed = "rich_message" in raw and (rich_message or None) != _rich_message_of(raw)
    return entities_changed, rich_changed


def _version_date(date: datetime | None, edit_date: datetime | None, edit_hide: Any) -> datetime | None:
    """When a text became current: its visible edit, or the send time.

    An ``edit_date`` Telegram hides (``edit_hide``, a reaction, #219) is not
    an edit of the text, so a text carrying one has been current since it
    was sent.
    """
    if edit_hide:
        return _strip_tz(date)
    return _strip_tz(edit_date) or _strip_tz(date)


# The keys of raw_data that describe a text's formatting. An edit moves them
# into the version it supersedes; nothing else may replace them.
_FORMATTING_KEYS = ("entities", "rich_message")


def _keep_archived_formatting(archived_raw_data: Any, incoming_raw_data: str) -> str:
    """``incoming_raw_data`` with the archived formatting kept in place.

    For a write that is not an edit (an import that renders text its own way,
    an older read, an edit Telegram hides). The archived formatting keys win;
    a key the archive does not have is filled from the incoming payload.
    An empty formatting list never fills one: a row archived before 9.0 has
    its formatting unknown, and stays so until a read brings some.
    """
    archived = _raw_data_dict(archived_raw_data) or {}
    incoming = _raw_data_dict(incoming_raw_data)
    if incoming is None:
        return incoming_raw_data
    merged = dict(incoming)
    for key in _FORMATTING_KEYS:
        if key in archived:
            merged[key] = archived[key]
        elif key == "entities" and merged.get(key) == []:
            merged.pop(key)
    if merged == incoming:
        return incoming_raw_data
    return json.dumps(merged) if merged else "{}"


def _with_formatting_of(archived_raw_data: Any, incoming_raw_data: str) -> str:
    """The archived ``raw_data`` with its formatting keys taken from the incoming one.

    For an edit read with no other extras (``"{}"``), of the text or of the
    formatting only: the old formatting has gone into the version it
    supersedes, and the rest of the archived payload stays as it was.
    """
    archived = _raw_data_dict(archived_raw_data)
    incoming = _raw_data_dict(incoming_raw_data)
    if archived is None or incoming is None:
        return incoming_raw_data
    merged = dict(archived)
    for key in _FORMATTING_KEYS:
        if key in incoming:
            merged[key] = incoming[key]
        else:
            merged.pop(key, None)
    return json.dumps(merged) if merged else "{}"


def _keep_archived_payloads(archived_raw_data: Any, incoming_raw_data: str, *, incoming_wins: bool) -> str:
    """``incoming_raw_data`` with every media payload the archive holds kept.

    A payload key (a poll, a location, a contact, a venue, a live location,
    ...; ``MEDIA_PAYLOAD_KEYS``) the archive holds and the incoming read lacks
    stays: an ``import --merge`` read that carries only ``forward_from_name``
    must not drop a poll. When both hold a key, ``incoming_wins`` decides.
    A read from Telegram (``incoming_wins`` true) wins, so a newer poll tally
    replaces the old one, and a live location in both goes through
    ``merge_geo_live``, which keeps every position either one saw. Any other
    writer (an import renders a thinner stand-in: no poll option bytes, no
    vCard, no venue provider) only fills keys the archive lacks; the archived
    value stays, a live location included. When the result is the archived
    payload itself, the archived string comes back unchanged, so the upsert
    sees no change to write.
    """
    archived = _raw_data_dict(archived_raw_data)
    incoming = _raw_data_dict(incoming_raw_data)
    if not archived or incoming is None:
        return incoming_raw_data
    merged = dict(incoming)
    for key in MEDIA_PAYLOAD_KEYS:
        if key not in archived:
            continue
        if key not in merged or not incoming_wins:
            merged[key] = archived[key]
        elif key == "geo_live":
            merged[key] = merge_geo_live(archived[key], merged[key])
    if merged == incoming:
        return incoming_raw_data
    if merged == archived and isinstance(archived_raw_data, str):
        return archived_raw_data
    return json.dumps(merged)


# The writers whose upserts are reads of the message from Telegram, so a newer
# edit_date with other formatting is an edit. An import renders text and
# formatting its own way, so a difference there is not evidence of an edit.
_TELEGRAM_READ_SOURCES = ("backup", "listener")


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

    # How long the account owner map is reused (see _account_owner_ids). An
    # account gains its telegram_user_id once, at its first login, so a stale
    # entry means at most this long without a sender label on a brand-new
    # account's messages — never a wrong label.
    ACCOUNT_OWNER_CACHE_TTL_SECONDS = 300

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
        self._transcript_fts_ready_cache: bool | None = None
        # (read_at, {telegram_user_id: account_id}) — see _account_owner_ids.
        self._account_owner_cache: tuple[float, dict[int, int]] | None = None

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
            "edit_hide": message_data.get("edit_hide"),
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
        entities: list | None = None,
        source: str | None = None,
        rich_message: dict | None = None,
    ) -> bool:
        """Best-effort capture of a superseded version into message_versions.

        A version is the text, its formatting (``entities``, the list that was
        in ``raw_data["entities"]``, and ``rich_message``, the block tree of a
        Rich Text Editor message) and the path that saw it (``source``:
        listener, sync, backup or import). The formatting is not part of the
        dedup hash, whose payload is frozen: a version is identified by its
        text and the time it became current, and a formatting-only edit still
        gets its own row because every edit moves the time. Two such edits in
        one second do not: when the frozen hash is taken by a row with other
        entities, the version is written under a hash that adds its entities
        (``_formatting_collision_hash``), so neither formatting is lost. Runs inside a
        SAVEPOINT so an unexpected failure here can never poison the
        transaction or abort the message upsert/batch it belongs to (the
        expected duplicate case is already silenced by ON CONFLICT DO NOTHING
        on change_hash).
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
            "entities": json.dumps(entities) if entities else None,
            "source": source,
            "rich_message": json.dumps(rich_message) if rich_message else None,
        }
        try:
            async with session.begin_nested():
                result = await session.execute(self._insert_message_version_stmt(values))
                if not result.rowcount:
                    salted = await self._formatting_collision_hash(session, values, entities)
                    if salted is not None:
                        values["change_hash"] = salted
                        result = await session.execute(self._insert_message_version_stmt(values))
        except Exception as e:
            logger.warning("Could not record a message version (%s); message update continues", type(e).__name__)
            return False
        return bool(result.rowcount)

    @staticmethod
    async def _formatting_collision_hash(session, values: dict[str, Any], entities: list | None) -> str | None:
        """The hash to keep a version under when its frozen hash holds other formatting, else None.

        None when the stored row is this very version (same entities), and
        for a stored row from before migration 035 (``source`` NULL): it never
        kept its formatting, so a difference against it is not one.
        """
        stored = (
            await session.execute(
                select(MessageVersion.entities, MessageVersion.source).where(
                    and_(
                        MessageVersion.account_id == values["account_id"],
                        MessageVersion.change_hash == values["change_hash"],
                    )
                )
            )
        ).first()
        if stored is None or stored.source is None:
            return None
        if (_json_or_none(stored.entities) or None) == (entities or None):
            return None
        return _formatted_message_version_hash(
            values["chat_id"], values["message_id"], values["text"], values["date"], entities
        )

    def _message_version_date(self, message: Message) -> datetime:
        return _version_date(message.date, message.edit_date, message.edit_hide)

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

    def _should_apply_edit_text(
        self,
        existing: Message,
        new_text: str,
        edit_date: datetime | None,
        formatting_changed: bool = False,
        same_date_applies: bool = False,
    ) -> bool:
        """Decide whether a live edit event (listener/sync) may replace archived text.

        Differs from the upsert policy on the no-edit_date case: a live event with
        ``edit_date=None`` is applied only when the archived row was never edited —
        an already-edited row is never rolled over on date-less evidence (rare
        bot-API edits may hit this; conservative by design, covered by tests).

        ``formatting_changed`` says the event's formatting differs from the
        archived one in an edit Telegram shows. Such an edit with the same text
        applies with a newer ``edit_date``: every edit moves the date.
        ``same_date_applies`` lets it apply with the same ``edit_date`` too:
        the date has one-second resolution and a bot can edit twice within one
        second, so a live event with other entities at the same date is the
        newer edit. For any other caller the same date means the archive
        already holds this edit.
        """
        old_edit_date = _strip_tz(existing.edit_date)
        edit_date = _strip_tz(edit_date)

        if existing.text == new_text:
            if not formatting_changed:
                # Text and formatting unchanged -> not an edit. Telegram bumps
                # edit_date for reaction-only changes (#219), so applying here
                # would set edit_date with no version and surface a phantom
                # "edited" marker. Reactions are captured by the reaction path.
                return False
            if edit_date is None:
                return False
            if old_edit_date is None or edit_date > old_edit_date:
                return True
            return same_date_applies and edit_date == old_edit_date
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

    @staticmethod
    def _fills_unknown_edit_hide(existing: Message, edit_date: datetime | None, edit_hide: int | None) -> bool:
        """True when ``edit_hide`` fills a flag the archive never knew.

        Rows archived before migration 034 have ``edit_date`` and a NULL flag,
        and NULL reads as shown, so a reaction Telegram hid still shows a
        pencil. A later read of the same edit (same ``edit_date``) that carries
        the flag fills it. That writes an unknown value beside the same date
        and overwrites nothing the archive captured.
        """
        if edit_hide is None or existing.edit_hide is not None:
            return False
        edit_date = _strip_tz(edit_date)
        return edit_date is not None and edit_date == _strip_tz(existing.edit_date)

    def _should_write_edit_hide(self, existing: Message, values: dict[str, Any], update_values: dict[str, Any]) -> bool:
        """edit_hide is Telegram's flag for the edit_date it came with.

        It is written when that date is written and differs from the stored
        one (a source with no flag, an import, writes NULL there: unknown, not
        "shown"), or when it fills an unknown flag for the same date. A known
        flag is never replaced beside an unchanged date.
        """
        if "edit_date" in update_values and _strip_tz(update_values["edit_date"]) != _strip_tz(existing.edit_date):
            return True
        return self._fills_unknown_edit_hide(existing, values.get("edit_date"), values.get("edit_hide"))

    def _older_read_text_to_keep(self, existing: Message, message_data: dict[str, Any], values: dict[str, Any]) -> bool:
        """True when a read from Telegram saw text the archive already replaced.

        The backup reads a message, then processes the rest of its batch
        before writing. An edit in that window reaches the listener, which
        stores the newer text with its edit_date first. The batch then brings
        the older text with no edit_date (or an older one), and the upsert
        rightly keeps the newer text. That older text is still something the
        archive read from Telegram, so it is kept as a version.

        Only writers that set ``keeps_older_text`` (the backup's own reads)
        qualify: an import renders text its own way, and a rendering
        difference must never become a version.
        """
        if not message_data.get("keeps_older_text"):
            return False
        new_text = values.get("text")
        if new_text is None or new_text == existing.text:
            return False
        if self._should_apply_upsert_text(existing, values):
            return False
        old_edit_date = _strip_tz(existing.edit_date)
        if old_edit_date is None:
            return False
        new_edit_date = _strip_tz(values.get("edit_date"))
        return new_edit_date is None or new_edit_date < old_edit_date

    def _is_upsert_formatting_edit(
        self, existing: Message, message_data: dict[str, Any], values: dict[str, Any]
    ) -> bool:
        """True when a read from Telegram shows an edit that changed only the formatting.

        Same text, other formatting, and an ``edit_date`` newer than the
        archived one, in an edit Telegram shows (a hidden edit is a reaction).
        Only formatting the archive knows is compared
        (``_known_formatting_changes``): a row archived before formatting was
        captured has its formatting filled, not versioned.
        Only the backup's and the listener's reads qualify: an import renders
        formatting its own way, and a rendering difference is not an edit.
        """
        if message_data.get("version_source") not in _TELEGRAM_READ_SOURCES:
            return False
        if values.get("text") != existing.text or values.get("edit_hide"):
            return False
        new_edit_date = _strip_tz(values.get("edit_date"))
        old_edit_date = _strip_tz(existing.edit_date)
        if new_edit_date is None or (old_edit_date is not None and new_edit_date <= old_edit_date):
            return False
        if _raw_data_dict(values.get("raw_data")) is None:
            return False
        entities, rich_message = _formatting_state(values.get("raw_data"))
        return any(_known_formatting_changes(existing.raw_data, entities, rich_message))

    @staticmethod
    def _is_upsert_media_edit(existing: Message, message_data: dict[str, Any], values: dict[str, Any]) -> bool:
        """True when this read's edit replaced the message's media (``media_replaced``).

        The backup sets the flag when ``reconcile_media_row`` kept the old
        media as a version for this read. The replacement already kept the
        text shown beside the old media; the edit's date moves here, as the
        listener and the sync move it with ``update_message_text(media_changed=True)``.
        """
        if not message_data.get("media_replaced"):
            return False
        new_edit_date = _strip_tz(values.get("edit_date"))
        old_edit_date = _strip_tz(existing.edit_date)
        return new_edit_date is not None and (old_edit_date is None or new_edit_date > old_edit_date)

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
        text_applied = self._should_apply_upsert_text(existing, values)
        formatting_edit = not text_applied and self._is_upsert_formatting_edit(existing, message_data, values)
        media_edit = (
            not text_applied and not formatting_edit and self._is_upsert_media_edit(existing, message_data, values)
        )
        if text_applied:
            if values.get("edit_date") is None and existing.edit_date is not None:
                # Text change arrived without edit evidence (e.g. late hydration):
                # keep the existing edit_date rather than nulling it.
                update_values.pop("edit_date", None)
        elif formatting_edit or media_edit:
            # The text is the same; the edit's date moves, as for a text edit,
            # so the version it supersedes keeps its own date.
            update_values.pop("text", None)
        else:
            update_values.pop("text", None)
            update_values.pop("edit_date", None)
        if not self._should_write_edit_hide(existing, values, update_values):
            update_values.pop("edit_hide", None)

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

        # Formatting belongs to the text it came with. An edit moves the old
        # formatting into the version it supersedes and writes the new one; any
        # other write keeps the archived formatting and only fills a missing key.
        # A read with no extras ("{}") still says the new text has no
        # formatting: the archived extras stay and the old formatting goes.
        if formatting_edit:
            if _has_raw_payload(values.get("raw_data")):
                update_values["raw_data"] = values["raw_data"]
            else:
                update_values["raw_data"] = _with_formatting_of(existing.raw_data, values["raw_data"])
        elif text_applied:
            archived_raw_data = getattr(existing, "raw_data", None)
            if (
                not _has_raw_payload(values.get("raw_data"))
                and _has_raw_payload(archived_raw_data)
                and _raw_data_dict(archived_raw_data) is not None
            ):
                # The read's empty formatting list, when it has one, says the
                # new text has none, and stays known.
                update_values["raw_data"] = _with_formatting_of(archived_raw_data, values.get("raw_data") or "{}")
        elif "raw_data" in update_values:
            update_values["raw_data"] = _keep_archived_formatting(existing.raw_data, update_values["raw_data"])
        # Whatever wrote raw_data above, a media payload the archive holds and
        # this read lacks stays (an import of a forward must not drop a poll).
        # Only a read from Telegram replaces a payload the archive holds; an
        # import's thinner stand-in never does.
        if "raw_data" in update_values:
            update_values["raw_data"] = _keep_archived_payloads(
                existing.raw_data,
                update_values["raw_data"],
                incoming_wins=message_data.get("version_source") in _TELEGRAM_READ_SOURCES,
            )

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
        if not self._pending_update_values(snapshot, message_data, values) and not self._older_read_text_to_keep(
            snapshot, message_data, values
        ):
            return

        existing = await self._load_message_for_update(session, values["account_id"], values["chat_id"], values["id"])
        if existing is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return

        source = message_data.get("version_source")
        if self._older_read_text_to_keep(existing, message_data, values):
            await self._record_message_version(
                session=session,
                account_id=existing.account_id,
                chat_id=existing.chat_id,
                message_id=existing.id,
                text=values["text"],
                date=_version_date(values["date"], values.get("edit_date"), values.get("edit_hide")),
                entities=_formatting_of(values.get("raw_data")),
                rich_message=_rich_message_of(values.get("raw_data")),
                source=source,
            )

        formatting_edit = not self._should_apply_upsert_text(existing, values) and self._is_upsert_formatting_edit(
            existing, message_data, values
        )
        update_values = self._pending_update_values(existing, message_data, values)
        if not update_values:
            return
        if "text" in update_values or formatting_edit:
            await self._record_message_version(
                session=session,
                account_id=existing.account_id,
                chat_id=existing.chat_id,
                message_id=existing.id,
                text=existing.text,
                date=self._message_version_date(existing),
                entities=_formatting_of(existing.raw_data),
                rich_message=_rich_message_of(existing.raw_data),
                source=source,
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
            # Rows the backup declined by configuration (#465: over the size
            # cap, or outside the media-type whitelist) are not pending: no
            # run will ever change them unless the operator relaxes a setting.
            retryable = Media.skip_reason.is_(None)
            pending = (
                await session.execute(
                    select(func.count())
                    .select_from(Media)
                    .where(Media.downloaded == 0, Media.download_attempts < max_attempts, not_metadata, retryable)
                )
            ).scalar() or 0
            exhausted = (
                await session.execute(
                    select(func.count())
                    .select_from(Media)
                    .where(Media.downloaded == 0, Media.download_attempts >= max_attempts, not_metadata, retryable)
                )
            ).scalar() or 0
            skipped = (
                await session.execute(
                    select(func.count())
                    .select_from(Media)
                    .where(Media.downloaded == 0, Media.skip_reason.is_not(None), not_metadata)
                )
            ).scalar() or 0
        return {"downloaded": downloaded, "pending": pending, "exhausted": exhausted, "skipped": skipped}

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

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Every account as ``{"id", "label"}``, ascending — the viewer's badge source.

        ``telegram_user_id`` is deliberately NOT selected. It is the one column
        on this table this project treats as PII, and nothing that renders an
        account to a browser needs it; the label is operator-chosen text from
        ``TG_ACCOUNT_<N>_LABEL``. A NULL label is left NULL here and given its
        fallback at the edge that displays it.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Account.id, Account.label).order_by(Account.id))
            return [{"id": row[0], "label": row[1]} for row in result]

    async def _account_owner_ids(self) -> dict[int, int]:
        """``{telegram_user_id: account_id}`` for accounts that have logged in.

        Server-side only, and the one place this project reads
        ``telegram_user_id`` outside ``ensure_account``. The KEYS are the PII;
        what callers put in a payload is the account id they map to.

        Cached for a short TTL because the answer changes exactly once per
        account, on its first login, while the callers ask on every page of
        messages. A per-message lookup would be a query per row, and a
        per-request one a query per page, for a table with one row per
        configured account.
        """
        cached = self._account_owner_cache
        if cached is not None and time.monotonic() - cached[0] <= self.ACCOUNT_OWNER_CACHE_TTL_SECONDS:
            return cached[1]
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Account.telegram_user_id, Account.id).where(Account.telegram_user_id.isnot(None))
            )
            owners = {row[0]: row[1] for row in result}
        self._account_owner_cache = (time.monotonic(), owners)
        return owners

    async def attach_sender_accounts(self, rows: list[dict[str, Any]], *, sender_key: str = "sender_id") -> None:
        """Stamp every row with ``sender_account_id``, in place.

        With several accounts archiving into one database, a group or channel
        is shown once — through one account's copy — so the other account's
        messages arrive looking like an ordinary participant's, and the
        displayed copy's own messages look like the only outgoing ones. The
        reader cannot tell which of their identities spoke.

        ``sender_account_id`` is the archived account that sent the message, or
        None for everyone else. It is derived here rather than exposed as a raw
        id so ``accounts.telegram_user_id`` never reaches a payload; the field
        is a small integer the viewer turns into a label.

        ``sender_key`` names the column holding the sender on this row shape —
        the search results carry it under a private key they then drop.
        """
        if not rows:
            return
        owners = await self._account_owner_ids()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sender_id = row.get(sender_key)
            row["sender_account_id"] = owners.get(sender_id) if isinstance(sender_id, int) else None

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

    async def _record_avatar_sighting(self, session, account_id: int, chat_id: int, photo_id: int | None) -> bool:
        """Best-effort append of one avatar_history row (031), in the caller's transaction.

        Runs inside a SAVEPOINT, like ``_record_message_version``, so a failure
        here never aborts the chat upsert it belongs to.
        """
        values = {"account_id": account_id, "chat_id": chat_id, "photo_id": photo_id, "seen_at": utcnow_naive()}
        insert_fn = sqlite_insert if self._is_sqlite else pg_insert
        try:
            async with session.begin_nested():
                await session.execute(insert_fn(AvatarHistory).values(**values))
        except Exception as e:
            logger.warning("Could not record an avatar sighting (%s); chat update continues", type(e).__name__)
            return False
        return True

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
                "avatar_photo_id": chat_data.get("avatar_photo_id"),
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
            # Only the backup reads the entity's photo; the listener's partial
            # upserts leave what it recorded (None is a real value: no avatar).
            if "avatar_photo_id" in chat_data:
                update_set["avatar_photo_id"] = values["avatar_photo_id"]

            # Every change of the recorded photo is kept in avatar_history (031),
            # read before the upsert overwrites it. A missing chat row counts as a
            # stored None, so a new chat with no photo records nothing.
            avatar_changed = False
            if "avatar_photo_id" in chat_data:
                stored = await session.execute(
                    select(Chat.avatar_photo_id)
                    .where(and_(Chat.account_id == account_id, Chat.id == chat_data["id"]))
                    .with_for_update()
                )
                stored_row = stored.first()
                stored_photo_id = stored_row[0] if stored_row else None
                avatar_changed = stored_photo_id != values["avatar_photo_id"]

            if self._is_sqlite:
                stmt = sqlite_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_set)
            else:
                stmt = pg_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_set)

            await session.execute(stmt)
            if avatar_changed:
                await self._record_avatar_sighting(session, account_id, chat_data["id"], values["avatar_photo_id"])
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
        fold_shared: bool = False,
        with_preview: bool = False,
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
            fold_shared: Return ONE row per non-private chat shared by several
                entitled accounts (``ChatScope.displayed_copy_predicate``) and
                attach ``accounts`` — the entitled account ids holding that chat,
                ascending — to every row. This is what the viewer's chat list
                asks for; the admin chat picker does not, because it edits
                per-copy grants and needs every copy. The fold happens BEFORE
                the archived/folder/search filters, so a folded chat is
                represented by its displayed copy everywhere, filters included:
                the alternative (fold the filtered set) makes the same chat
                appear and disappear depending on which account archived it,
                and picks a different ref per view.
            with_preview: Attach ``preview`` to every row: the chat's newest
                message not deleted in Telegram, the way Telegram's own chat
                list shows it (see ``_attach_chat_previews``). Only the viewer's
                chat list asks for it.
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

            # One row per shared non-private chat, in SQL for the same reason
            # the grant is: limit/offset/COUNT must all describe the folded set.
            if fold_shared:
                stmt = stmt.where((scope or ChatScope()).displayed_copy_predicate())

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
            # `is_(None), desc()` pair spelled out. The two id columns are the
            # tiebreakers that make the ordering TOTAL: without them every
            # message-less chat ties on NULL, and LIMIT/OFFSET may then split
            # that tie group differently on each page, so a chat could appear
            # twice or vanish. Chat.id alone stopped being total in 8.0 — the
            # primary key is (account_id, id), so two accounts' message-less
            # copies of one private chat tie on BOTH the NULL date and the id.
            stmt = stmt.order_by(nulls_last(desc("last_message_date")), Chat.id.desc(), Chat.account_id.desc())

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
                    "avatar_photo_id": row.Chat.avatar_photo_id,
                    "last_synced_message_id": row.Chat.last_synced_message_id,
                    "created_at": row.Chat.created_at,
                    "updated_at": row.Chat.updated_at,
                    "last_message_date": row.last_message_date,
                }
                chats.append(chat_dict)
            if with_preview:
                await self._attach_chat_previews(session, chats)
        if fold_shared:
            await self._attach_chat_accounts(chats, scope=scope)
        return chats

    async def _attach_chat_previews(self, session, chats: list[dict[str, Any]]) -> None:
        """Give every row in ``chats`` its ``preview``, in place, in ONE query.

        The preview is the chat's newest message NOT deleted in Telegram, which
        is what Telegram's own list shows; the chat itself still shows the
        deletion. It is read from the row's own ``(account_id, id)`` copy, the
        same copy the row's ref opens, so it can never show a message the
        principal could not open in the chat: the page passed in is already cut
        to the principal's scope and folded.

        Cost: one statement per page, never per row. Each row costs one seek of
        ``idx_messages_chat_date_desc`` for the newest kept message (the ORDER BY
        rides the index, so the scan stops at the first row that is neither
        another account's copy nor deleted), one primary-key lookup, one
        ``idx_media_message`` probe for the media kind and primary-key probes of
        ``users`` for the sender's names. A chat whose whole tail
        was deleted walks back through that tail; the archive keeps no index on
        ``is_deleted`` because every other read wants deleted rows too.

        Shape (``None`` for a chat with no kept message)::

            {"message_id", "date", "text", "sender", "kind", "outgoing",
             "action", "action_title"}

        * ``text``: whitespace folded to single spaces, cut to
          ``CHAT_PREVIEW_TEXT_LENGTH`` characters with an ellipsis. A poll with
          no text gives its question. None when there is nothing to quote.
        * ``kind``: ``text``, ``service``, ``poll``, the media type the archive
          stored (``photo``, ``voice``, ``geo``, ``contact`` ...) or ``message``
          when a message has neither text nor a media row (media capture off).
        * ``sender``: ``"You"`` for the account's own message in a private chat
          or a group, the sender's first name for anyone else in a group, and
          None in a channel, in a private chat for the other person, and for a
          service row, whose sentence already names its actor.
        * ``action`` / ``action_title``: a service row's ``action_type`` and
          ``new_title`` when its text is empty (rows from before 7.28), so the
          viewer can word it the way the chat does.
        """
        for chat in chats:
            chat["preview"] = None
        keys = sorted({(chat["account_id"], chat["id"]) for chat in chats})
        if not keys:
            return

        newest_kept = aliased(Message)
        newest_kept_id = (
            select(newest_kept.id)
            .where(
                newest_kept.account_id == Chat.account_id,
                newest_kept.chat_id == Chat.id,
                or_(newest_kept.is_deleted == 0, newest_kept.is_deleted.is_(None)),
            )
            .order_by(newest_kept.date.desc(), newest_kept.id.desc())
            .limit(1)
            .correlate(Chat)
            .scalar_subquery()
        )
        # No ORDER BY: SQLite answered ORDER BY id by walking the media primary
        # key of the whole account, and min(type) by walking the chat's media in
        # type order, instead of probing idx_media_message. A message has one
        # media row in all but rare cases, so any of its rows names its kind.
        media_type = (
            select(Media.type)
            .where(
                Media.account_id == Message.account_id,
                Media.chat_id == Message.chat_id,
                Media.message_id == Message.id,
            )
            .limit(1)
            .correlate(Message)
            .scalar_subquery()
        )

        def sender_column(column):
            # A primary-key probe per row, never a join: PostgreSQL estimates the
            # page as one row and joined users by a sequential scan per preview,
            # which grows with every person the archive has ever seen.
            return select(column).where(User.id == Message.sender_id).correlate(Message).scalar_subquery()

        stmt = (
            select(
                Message.account_id,
                Message.chat_id,
                Message.id,
                Message.date,
                # Enough characters to survive the whitespace fold, never the
                # whole text: a long post must not travel to be cut to a line.
                func.substr(Message.text, 1, CHAT_PREVIEW_TEXT_LENGTH * 4).label("text"),
                Message.sender_name,
                Message.is_outgoing,
                Message.raw_data,
                sender_column(User.first_name).label("first_name"),
                sender_column(User.last_name).label("last_name"),
                sender_column(User.username).label("username"),
                media_type.label("media_type"),
            )
            .select_from(Chat)
            .join(
                Message,
                and_(
                    Message.account_id == Chat.account_id,
                    Message.chat_id == Chat.id,
                    Message.id == newest_kept_id,
                ),
            )
            .where(tuple_(Chat.account_id, Chat.id).in_(keys))
        )
        result = await session.execute(stmt)
        previews = {(row.account_id, row.chat_id): row for row in result}
        for chat in chats:
            row = previews.get((chat["account_id"], chat["id"]))
            if row is not None:
                chat["preview"] = _chat_preview(row, chat.get("type"))

    async def _attach_chat_accounts(self, chats: list[dict[str, Any]], *, scope: ChatScope | None) -> None:
        """Give every row in ``chats`` its ``accounts`` list, in place.

        One extra indexed read per page rather than an aggregate in the list
        query: PostgreSQL would spell it ``array_agg() OVER ()`` and SQLite
        ``group_concat() OVER ()``, and this project ships both backends as
        first-class, so the dialect-free shape is the one that cannot drift.

        Private chats answer with their own account and nothing else — see
        ``ChatScope.displayed_copy_predicate`` for why an id shared by two
        accounts is not the same private conversation.
        """
        shared_ids = [row["id"] for row in chats if row.get("type") != PRIVATE_CHAT_TYPE]
        holders = await self.get_chat_account_ids(shared_ids, scope=scope)
        for row in chats:
            if row.get("type") == PRIVATE_CHAT_TYPE:
                row["accounts"] = [row["account_id"]]
            else:
                row["accounts"] = holders.get(row["id"]) or [row["account_id"]]

    async def get_chat_account_ids(
        self, chat_ids: Collection[int], *, scope: ChatScope | None = None
    ) -> dict[int, list[int]]:
        """Which entitled accounts hold each of these NON-PRIVATE chat ids, ascending.

        The data behind the viewer's account badges. Scoped by the same grant
        as the chat list, so a badge can never name an account the principal is
        not entitled to. Private chats are excluded by the query itself, so an
        id that is only ever a private chat simply has no entry.
        """
        wanted = {int(chat_id) for chat_id in chat_ids}
        if not wanted:
            return {}
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat.id, Chat.account_id).where(Chat.id.in_(wanted), Chat.type != PRIVATE_CHAT_TYPE)
            for predicate in (scope or ChatScope()).sql_predicates():
                stmt = stmt.where(predicate)
            result = await session.execute(stmt)
            holders: dict[int, set[int]] = {}
            for chat_id, account_id in result:
                holders.setdefault(chat_id, set()).add(account_id)
        return {chat_id: sorted(accounts) for chat_id, accounts in holders.items()}

    async def get_visible_account_ids(self, scope: ChatScope) -> set[int]:
        """The accounts that hold at least one chat ``scope`` selects.

        What a ref-scoped principal — a share token, or a viewer granted a
        handful of chats — may be told an account list contains. Reading it off
        the chats the grant already selects means the answer can never name an
        account the principal has no chat in.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat.account_id).distinct()
            for predicate in scope.sql_predicates():
                stmt = stmt.where(predicate)
            result = await session.execute(stmt)
            return {row[0] for row in result}

    async def get_visible_chat_pairs(self, scope: ChatScope) -> set[tuple[int, int]]:
        """The ``(account_id, chat_id)`` pairs a scope selects.

        ``get_all_chats`` attaches a correlated ``MAX(messages.date)`` per row,
        which is exactly what the callers of this (folder counts, cached stats)
        throw away. A grant can be as wide as a whole account, so paying that
        subquery per chat to collect keys is waste that grows with the archive.

        The pair, not the bare id: since 8.0 the primary key is
        ``(account_id, id)``, so two accounts' copies of one channel share an
        id, and two accounts' conversations with the same person share one
        while being different chats holding different messages. Anything that
        filters by id alone counts the other account's rows as if they were
        this principal's.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat.account_id, Chat.id)
            for predicate in scope.sql_predicates():
                stmt = stmt.where(predicate)
            result = await session.execute(stmt)
            return {(row[0], row[1]) for row in result}

    async def get_chat_count(
        self,
        search: str = None,
        archived: bool | None = None,
        folder_id: int | None = None,
        *,
        account_id: int | None = None,
        scope: ChatScope | None = None,
        fold_shared: bool = False,
    ) -> int:
        """Get total number of chats (fast count for pagination).

        Args:
            search: Optional search query to filter count
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
            account_id: If set, only this account's chats (None = unscoped until phase 4)
            scope: Viewer entitlement (see get_all_chats). Must be the SAME scope the
                matching get_all_chats call used, or ``total`` and the page disagree.
            fold_shared: Count folded rows (see get_all_chats). Must match the
                matching get_all_chats call for the same reason the scope must:
                a count of the unfolded rows makes ``has_more`` promise pages
                that do not exist.
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

            if fold_shared:
                stmt = stmt.where((scope or ChatScope()).displayed_copy_predicate())

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
        """Get messages within a date range (None account_id = unscoped until phase 4).

        Each row carries its ``account_id``: unscoped, two accounts' private
        chats with the same peer share a chat id and message ids, and the
        export tells them apart by the account.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message)
                .where(*self._date_range_conditions(chat_id, start_date, end_date, account_id))
                .order_by(Message.date.asc())
            )
            result = await session.execute(stmt)
            return [{**self._message_to_dict(m), "account_id": m.account_id} for m in result.scalars()]

    @staticmethod
    def _date_range_conditions(
        chat_id: int | None,
        start_date: datetime | None,
        end_date: datetime | None,
        account_id: int | None,
    ) -> list:
        """The message filters of ``get_messages_by_date_range``, both ends inclusive."""
        conditions = []
        if account_id is not None:
            conditions.append(Message.account_id == account_id)
        if chat_id:
            conditions.append(Message.chat_id == chat_id)
        if start_date:
            conditions.append(Message.date >= start_date)
        if end_date:
            conditions.append(Message.date <= end_date)
        return conditions

    async def get_messages_for_backup_export(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        *,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """What ``telegram-archive export`` writes, read from one snapshot.

        The messages ``get_messages_by_date_range`` picks, in
        ``EXPORT_MESSAGE_ORDER``, each with ``media``, ``versions``,
        ``reaction_history`` and, when it has any, ``transcripts``
        (``_export_message_parts``). Dates stay
        datetimes, as every other date of that file does. One snapshot, so a
        backup writing meanwhile cannot make a message, its versions, its
        media and its transcripts disagree: every transcript names a media
        listed in the same file.
        """
        conditions = self._date_range_conditions(chat_id, start_date, end_date, account_id)
        async with self.db_manager.async_session_factory() as session:
            await self._read_one_snapshot(session)
            transcripts: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
            for row in await self._read_export_transcripts(session, chat_id, account_id=account_id):
                transcripts.setdefault((row["account_id"], row["chat_id"], row["message_id"]), []).append(row)
            result = await session.stream(select(Message).where(*conditions).order_by(*EXPORT_MESSAGE_ORDER))
            messages = []
            async for m, media, versions, reaction_history in self._export_message_parts(
                session, result.scalars(), conditions, iso=False
            ):
                message = {
                    **self._message_to_dict(m),
                    "account_id": m.account_id,
                    "media": [self._export_media_dict(row) for row in media],
                    "versions": versions,
                    "reaction_history": reaction_history,
                }
                rows = transcripts.get((m.account_id, m.chat_id, m.id))
                if rows:
                    message["transcripts"] = rows
                messages.append(message)
        return messages

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

    async def get_unflagged_edit_ids(self, chat_id: int, *, account_id: int) -> set[int]:
        """IDs of a chat's messages with ``edit_date`` set and no ``edit_hide`` flag.

        These rows were archived before migration 034 kept the flag. The sync
        pass fills the flag for them from the message it already fetched when
        the edit_date matches, so a reaction Telegram hid stops showing as an
        edit. The set shrinks to nothing as the flags are filled.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.id).where(
                and_(
                    Message.account_id == account_id,
                    Message.chat_id == chat_id,
                    Message.edit_date.isnot(None),
                    Message.edit_hide.is_(None),
                    or_(Message.is_deleted == 0, Message.is_deleted.is_(None)),
                )
            )
            result = await session.execute(stmt)
            return {row.id for row in result}

    async def get_edit_hide_backfill_rows(self, chat_id: int, *, account_id: int) -> list[tuple[int, datetime]]:
        """The work list of ``backfill-details`` for edit flags in one chat: ``(message_id, edit_date)``.

        A row is listed when it has an ``edit_date``, no ``edit_hide`` and no
        kept version, and is not deleted. A kept version means a real edit the
        archive saw, whose pencil stays whatever the flag says; Telegram no
        longer serves a deleted message. One chat at a time, so the query is
        a range of the primary key and the list stays the size of one chat.
        Filling the flag takes a row off the list.
        """
        has_version = (
            select(MessageVersion.id)
            .where(
                and_(
                    MessageVersion.account_id == Message.account_id,
                    MessageVersion.chat_id == Message.chat_id,
                    MessageVersion.message_id == Message.id,
                )
            )
            .exists()
        )
        stmt = (
            select(Message.id, Message.edit_date)
            .where(
                and_(
                    Message.account_id == account_id,
                    Message.chat_id == chat_id,
                    Message.edit_date.isnot(None),
                    Message.edit_hide.is_(None),
                    or_(Message.is_deleted == 0, Message.is_deleted.is_(None)),
                    ~has_version,
                )
            )
            .order_by(Message.id)
        )
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(stmt)
            return [(row.id, row.edit_date) for row in result]

    @retry_on_locked()
    async def fill_edit_hide(
        self, chat_id: int, message_id: int, edit_date: datetime, edit_hide: int, *, account_id: int
    ) -> bool:
        """Fill an unknown ``edit_hide`` for the ``edit_date`` the archive holds.

        Writes only when the stored flag is NULL and the stored edit_date is
        this one, so it fills an unknown value beside the same date and
        overwrites nothing. Text, edit_date and versions are left alone.
        Returns True when a row changed.
        """
        edit_date = _strip_tz(edit_date)
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(
                    and_(
                        Message.account_id == account_id,
                        Message.chat_id == chat_id,
                        Message.id == message_id,
                        Message.edit_date == edit_date,
                        Message.edit_hide.is_(None),
                    )
                )
                .values(edit_hide=edit_hide)
            )
            await session.commit()
            return bool(result.rowcount)

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

    async def get_avatar_photo_id(self, chat_id: int, *, account_id: int) -> int | None:
        """The profile photo id ``account_id`` last saw for ``chat_id``, or None.

        None covers no row, no avatar, and a row no backup has refreshed since
        migration 029 — the viewer falls back to the newest file for all three.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat.avatar_photo_id).where(and_(Chat.account_id == account_id, Chat.id == chat_id))
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    async def get_avatar_history(self, chat_id: int, *, account_id: int) -> list[dict[str, Any]]:
        """Every photo id ``account_id`` saw for ``chat_id``, newest first.

        ``photo_id`` None is a sighting of the photo being removed. An empty
        list means nothing was recorded, which is not the same as a removal.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(AvatarHistory.photo_id, AvatarHistory.seen_at)
                .where(and_(AvatarHistory.account_id == account_id, AvatarHistory.chat_id == chat_id))
                .order_by(AvatarHistory.seen_at.desc(), AvatarHistory.id.desc())
            )
            result = await session.execute(stmt)
            return [{"photo_id": row.photo_id, "seen_at": row.seen_at} for row in result]

    async def get_avatar_removals(self, pairs: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
        """The ``(account_id, chat_id)`` pairs whose newest avatar sighting is a removal.

        One query for a page of chats, so the chat list can decline to
        advertise a photo the account saw removed without a history lookup per
        row. A pair with no history at all is not in the result: never
        recorded is not the same as removed.
        """
        wanted = {(int(account_id), int(chat_id)) for account_id, chat_id in pairs}
        if not wanted:
            return set()
        async with self.db_manager.async_session_factory() as session:
            # A row-value IN, not an OR of ANDs: SQLite refuses an OR chain of
            # about a thousand terms (expression depth), and the viewer asks for
            # pages of up to 1000 chats.
            stmt = (
                select(AvatarHistory.account_id, AvatarHistory.chat_id, AvatarHistory.photo_id)
                .where(tuple_(AvatarHistory.account_id, AvatarHistory.chat_id).in_(sorted(wanted)))
                .order_by(
                    AvatarHistory.account_id,
                    AvatarHistory.chat_id,
                    AvatarHistory.seen_at.desc(),
                    AvatarHistory.id.desc(),
                )
            )
            removed: set[tuple[int, int]] = set()
            seen: set[tuple[int, int]] = set()
            for row in await session.execute(stmt):
                key = (row.account_id, row.chat_id)
                if key in seen:
                    continue
                seen.add(key)
                if row.photo_id is None:
                    removed.add(key)
            return removed

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

    @staticmethod
    def _delete_transcripts_of(media_predicate, *, account_id: int):
        """DELETE of the transcript rows of the media ``media_predicate`` selects.

        The flag-gated removal paths take a media row's transcripts with it
        (docs/TRANSCRIPTION.md): run this before deleting the media, in the
        same transaction. There is no foreign key to cascade through.
        """
        return delete(MediaTranscript).where(
            and_(
                MediaTranscript.account_id == account_id,
                MediaTranscript.media_id.in_(select(Media.id).where(media_predicate)),
            )
        )

    @staticmethod
    async def _delete_media_versions_of(session, version_predicate, *, account_id: int) -> None:
        """Delete the ``media_versions`` rows ``version_predicate`` selects, and their transcripts.

        The flag-gated removal paths that delete a message's or a chat's media
        delete its earlier media with it, in the same transaction. A kept
        version's transcripts still point at the id the media row had then.
        """
        await session.execute(
            delete(MediaTranscript).where(
                and_(
                    MediaTranscript.account_id == account_id,
                    MediaTranscript.media_id.in_(select(MediaVersion.media_id).where(version_predicate)),
                )
            )
        )
        await session.execute(delete(MediaVersion).where(version_predicate))

    @retry_on_locked()
    async def delete_message(self, chat_id: int, message_id: int, *, account_id: int) -> dict | None:
        """Delete a specific message and its media.

        Returns a pre-deletion snapshot of the row (see _deletion_snapshot) so
        the listener can fire the event webhook with the destroyed content, or
        None when the message was never archived. The DELETEs still run
        unconditionally — orphan-cleanup behavior is unchanged. The message's
        media take their transcript rows with them.
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
            # Delete the earlier media an edit replaced, with their transcripts
            await self._delete_media_versions_of(
                session,
                and_(
                    MediaVersion.account_id == account_id,
                    MediaVersion.chat_id == chat_id,
                    MediaVersion.message_id == message_id,
                ),
                account_id=account_id,
            )
            # Delete the transcripts of the media below, then the media
            await session.execute(
                self._delete_transcripts_of(
                    and_(Media.account_id == account_id, Media.chat_id == chat_id, Media.message_id == message_id),
                    account_id=account_id,
                )
            )
            await session.execute(
                delete(Media).where(
                    and_(Media.account_id == account_id, Media.chat_id == chat_id, Media.message_id == message_id)
                )
            )
            # Delete reactions and their history
            await session.execute(
                delete(ReactionHistory).where(
                    and_(
                        ReactionHistory.account_id == account_id,
                        ReactionHistory.chat_id == chat_id,
                        ReactionHistory.message_id == message_id,
                    )
                )
            )
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
        edit_hide: int | None = None,
        entities: list | None = None,
        update_entities: bool = False,
        rich_message: dict | None = None,
        source: str | None = None,
        media_changed: bool = False,
    ) -> tuple[str, dict | None]:
        """Update a message's text and edit_date.

        ``edit_hide`` is Telegram's flag for that ``edit_date`` and is written
        beside it (None: the caller does not know it). ``source`` names the
        caller's path (``listener`` or ``sync``) on the version it writes.

        An edit writes the version it supersedes first: the old text, its
        formatting (entities and block tree) and its date. With
        ``update_entities`` an edit that changed only the formatting is an edit
        too, when Telegram shows it and its ``edit_date`` is newer (or, from
        the listener, the same with other entities): it gets a version and
        moves ``edit_date``. Only formatting the archive knows is compared: a
        key the archived ``raw_data`` lacks is filled, with no version.

        Returns ``(outcome, prior)`` so callers can keep honest counters and
        only broadcast edits that actually changed the archive. ``outcome`` is
        ``"applied"`` | ``"noop"`` (already current / older evidence) |
        ``"not_found"`` (message not archived). ``prior`` is a snapshot of the
        superseded row ({text, sender_id, sender_name}) on "applied", captured
        in the same transaction so the event webhook gets race-free old text;
        None otherwise.

        With ``update_entities`` the caller also owns ``raw_data["rich_message"]``
        (#470): ``rich_message`` replaces the stored block tree, None drops it,
        so an edit that rewrote a Rich Text Editor message never leaves the
        old tree beside the new text.

        ``media_changed`` says the same edit replaced the message's photo or
        file (``reconcile_media_row`` kept the old one): it is an edit even when
        the caption stayed the same, so it moves ``edit_date`` like a
        formatting-only edit does.
        """
        edit_date = _strip_tz(edit_date)
        async with self.db_manager.async_session_factory() as session:
            message = await self._load_message_for_update(session, account_id, chat_id, message_id)
            if message is None:
                logger.debug("Edit no-op: message not found in archive")
                return "not_found", None

            archived_entities, archived_rich_message = _formatting_state(message.raw_data)
            entities_changed, rich_changed = (
                _known_formatting_changes(message.raw_data, entities, rich_message)
                if update_entities and not edit_hide
                else (False, False)
            )
            formatting_changed = entities_changed or rich_changed
            # The same edit_date applies only to a live event with other
            # entities. A block tree also carries file references, which
            # Telegram refreshes, so a tree alone never counts at the same date.
            same_date_applies = source == "listener" and entities_changed
            if not self._should_apply_edit_text(
                message, new_text, edit_date, formatting_changed or media_changed, same_date_applies
            ):
                # Not an edit: the same text and formatting (a reaction moves
                # edit_date, #219), an edit Telegram hides, or older evidence.
                # The archived formatting stays; a key the row never had is
                # filled when the text is the same, since it describes that text.
                if (
                    update_entities
                    and message.text == new_text
                    and self._fill_missing_formatting(message, entities, rich_message)
                ):
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
                    logger.debug("Edit no-op text, missing formatting filled")
                else:
                    logger.debug("Edit no-op: message already current")
                return "noop", None

            prior = {"text": message.text, "sender_id": message.sender_id, "sender_name": message.sender_name}
            await self._record_message_version(
                session=session,
                account_id=account_id,
                chat_id=chat_id,
                message_id=message_id,
                text=message.text,
                date=self._message_version_date(message),
                entities=archived_entities,
                rich_message=archived_rich_message,
                source=source,
            )
            await session.execute(
                update(Message)
                .where(and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id))
                .values(text=new_text, edit_date=edit_date, edit_hide=edit_hide)
            )
            if update_entities and self._merge_raw_data_entities(message, entities, rich_message):
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

    def _merge_raw_data_entities(
        self, message: Message, entities: list | None, rich_message: dict | None = None
    ) -> bool:
        """Set or drop raw_data["entities"] and ["rich_message"] on the loaded row; True if anything changed.

        raw_data is a JSON string column, so the merge round-trips through
        json; a row whose raw_data is unparseable is left untouched (never
        destroy unrelated capture payloads for a formatting refresh). None
        drops a key: an edit that no longer carries formatting, or no longer
        is a Rich Text Editor message, must not keep the stale value. Entities
        are the exception since 9.0: None becomes an empty list, so the text is
        known to have no formatting and a later edit that adds some is an edit.
        """
        try:
            raw = json.loads(message.raw_data) if message.raw_data else {}
        except ValueError, TypeError:
            return False
        if not isinstance(raw, dict):
            return False
        changed = False
        for key, value in (("entities", entities or []), ("rich_message", rich_message)):
            if value is None:
                if key in raw:
                    raw.pop(key)
                    changed = True
            elif raw.get(key) != value:
                raw[key] = value
                changed = True
        if not changed:
            return False
        message.raw_data = json.dumps(raw)
        return True

    def _fill_missing_formatting(self, message: Message, entities: list | None, rich_message: dict | None) -> bool:
        """Add formatting keys the loaded row does not have; True if anything was added.

        Rows archived before formatting was captured have no entities. A later
        event that is not an edit may fill them, and never replaces a key the
        archive already holds.
        """
        raw = _raw_data_dict(message.raw_data)
        if raw is None:
            return False
        raw = dict(raw)
        changed = False
        for key, value in (("entities", entities), ("rich_message", rich_message)):
            if value and key not in raw:
                raw[key] = value
                changed = True
        if not changed:
            return False
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
        edit_hide = getattr(message, "edit_hide", None)
        if not isinstance(edit_hide, int):
            edit_hide = None

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
            "edit_hide": edit_hide,
            "raw_data": message.raw_data,
            "created_at": message.created_at,
            "is_outgoing": message.is_outgoing,
            "is_pinned": message.is_pinned,
            "is_deleted": int(is_deleted),
            "deleted_at": deleted_at,
        }

    def _message_version_to_dict(self, row: MessageVersion) -> dict[str, Any]:
        entities = _formatting_of({"entities": _json_or_none(row.entities)})
        rich_message = _rich_message_of({"rich_message": _json_or_none(row.rich_message)})
        return {
            "chat_id": row.chat_id,
            "message_id": row.message_id,
            "text": row.text,
            "date": row.date,
            "captured_at": row.captured_at,
            "source": row.source,
            "entities": entities,
            "rich_message": rich_message,
        }

    @staticmethod
    def _export_media_dict(row) -> dict[str, Any]:
        """One media as both exports list it: what a reader needs to tell it
        apart and match its transcripts, never where the file lies on disk.

        ``media_id`` is the id the transcripts of that media name: the media
        row's id, or for earlier media the id the row had when an edit
        replaced it (``media_versions.media_id``).
        """
        return {
            "media_id": row.media_id,
            "type": row.type,
            "file_name": row.file_name,
            "file_size": row.file_size,
            "mime_type": row.mime_type,
            "width": row.width,
            "height": row.height,
            "duration": row.duration,
        }

    @staticmethod
    def _export_version_dict(row, *, iso: bool = True) -> dict[str, Any]:
        """One earlier text version as both exports list it under its message.

        ``date`` is when that text was current in Telegram (the edit that
        produced it, or the send), ``captured_at`` when the archive saw it
        replaced. Both columns are NOT NULL. ``iso`` writes them as ISO 8601,
        like every other date of the viewer's export; the command's export
        keeps datetimes. ``media`` is filled by ``_export_versions``.
        """
        return {
            "text": row.text,
            "date": row.date.isoformat() if iso else row.date,
            "captured_at": row.captured_at.isoformat() if iso else row.captured_at,
            "source": row.source,
            "entities": _formatting_of({"entities": _json_or_none(row.entities)}),
            "rich_message": _rich_message_of({"rich_message": _json_or_none(row.rich_message)}),
            "media": [],
        }

    @classmethod
    def _export_versions(cls, text_rows: list, media_rows: list, *, iso: bool = True) -> list[dict[str, Any]]:
        """A message's earlier versions with the earlier media each was shown with, oldest first.

        The pairing rule of ``get_message_versions``: an earlier media sits
        under the text version with the same ``date``, the moment that text
        and that media became current together, and of two text versions
        with one date the last one stored. Earlier media with no text version
        of its date is listed as its own version, with ``text`` null and
        ``media_only`` true, so no kept media goes unlisted. ``text_rows``
        and ``media_rows`` come oldest first, the order their queries sort in.
        """
        versions = [cls._export_version_dict(row, iso=iso) for row in text_rows]
        by_date = {row.date: version for row, version in zip(text_rows, versions, strict=True)}
        dated = list(zip((row.date for row in text_rows), versions, strict=True))
        for row in media_rows:
            version = by_date.get(row.date)
            if version is None:
                version = {
                    "text": None,
                    "date": row.date.isoformat() if iso else row.date,
                    "captured_at": row.captured_at.isoformat() if iso else row.captured_at,
                    "source": row.source,
                    "entities": None,
                    "rich_message": None,
                    "media": [],
                    "media_only": True,
                }
                by_date[row.date] = version
                dated.append((row.date, version))
            version["media"].append(cls._export_media_dict(row))
        dated.sort(key=lambda pair: pair[0])
        return [version for _, version in dated]

    @staticmethod
    def _message_keys_of(model):
        """``model``'s account, chat and message columns, labelled as ``_ExportWalk`` reads them."""
        return (
            model.account_id.label("account_id"),
            model.chat_id.label("chat_id"),
            model.message_id.label("message_id"),
        )

    @staticmethod
    def _joined_to_its_message(stmt, model, message_conditions: list):
        """``stmt`` over ``model`` narrowed to the messages ``message_conditions`` pick."""
        return stmt.join(
            Message,
            and_(
                Message.account_id == model.account_id,
                Message.chat_id == model.chat_id,
                Message.id == model.message_id,
            ),
        ).where(*message_conditions)

    @classmethod
    def _versions_of_messages_query(cls, message_conditions: list):
        """Every kept text version of the messages ``message_conditions`` pick, in export order.

        The conditions apply to the message (its chat, account and date), not
        to the version, so a message in a date window keeps all its versions.
        Rows come in ``EXPORT_MESSAGE_ORDER``, each message's versions oldest
        first (the order they were captured in when two share a date), so a
        reader can walk them beside the messages.
        """
        stmt = select(
            *cls._message_keys_of(MessageVersion),
            MessageVersion.text,
            MessageVersion.date,
            MessageVersion.captured_at,
            MessageVersion.source,
            MessageVersion.entities,
            MessageVersion.rich_message,
        )
        return cls._joined_to_its_message(stmt, MessageVersion, message_conditions).order_by(
            *EXPORT_MESSAGE_ORDER, MessageVersion.date.asc(), MessageVersion.id.asc()
        )

    @classmethod
    def _media_of_messages_query(cls, message_conditions: list):
        """The current media rows of the messages ``message_conditions`` pick, in export order.

        A message can hold more than one media row. Its rows come in the order
        the viewer picks the one it shows: a downloaded row before a pending
        one, then the lowest id. ``file_path`` is read for
        ``scripts/restore_chat.py`` only (``include_media``); no export writes it.
        """
        stmt = select(
            *cls._message_keys_of(Media),
            Media.id.label("media_id"),
            Media.type,
            Media.file_path,
            Media.file_name,
            Media.file_size,
            Media.mime_type,
            Media.width,
            Media.height,
            Media.duration,
        )
        return cls._joined_to_its_message(stmt, Media, message_conditions).order_by(
            *EXPORT_MESSAGE_ORDER, func.coalesce(Media.downloaded, 0).desc(), Media.id.asc()
        )

    @classmethod
    def _media_versions_of_messages_query(cls, message_conditions: list):
        """The earlier media (``media_versions``) of the messages ``message_conditions`` pick, in export order.

        Each message's rows oldest first by ``date``, then in the order they were kept.
        """
        stmt = select(
            *cls._message_keys_of(MediaVersion),
            MediaVersion.media_id,
            MediaVersion.type,
            MediaVersion.file_name,
            MediaVersion.file_size,
            MediaVersion.mime_type,
            MediaVersion.width,
            MediaVersion.height,
            MediaVersion.duration,
            MediaVersion.date,
            MediaVersion.captured_at,
            MediaVersion.source,
        )
        return cls._joined_to_its_message(stmt, MediaVersion, message_conditions).order_by(
            *EXPORT_MESSAGE_ORDER, MediaVersion.date.asc(), MediaVersion.id.asc()
        )

    async def _export_message_parts(self, session, messages, message_conditions: list, *, iso: bool):
        """Yield each message of ``messages`` with its media rows, ``versions`` and reaction history.

        ``messages`` is an async iterable of rows or ``Message`` objects in
        ``EXPORT_MESSAGE_ORDER``, picked by ``message_conditions``; each has
        ``account_id``, ``chat_id`` and ``id``. Its current media, text
        versions and earlier media are three more statements in the same
        session, walked beside it, and so is its ``reaction_history`` (one
        dict per kept state, oldest first, ``observed_at`` in ISO 8601 when
        ``iso``), so only the current message's rows are ever in memory,
        however long the chat. Call ``_read_one_snapshot`` first, so all five
        read the same archive state.

        If rows are left over at the end, two statements stopped sorting
        alike and some messages went out without them: the export fails
        rather than write a file that drops them.
        """
        media = await _ExportWalk.open(session, self._media_of_messages_query(message_conditions))
        texts = await _ExportWalk.open(session, self._versions_of_messages_query(message_conditions))
        earlier = await _ExportWalk.open(session, self._media_versions_of_messages_query(message_conditions))
        reactions = await _ExportWalk.open(session, self._reaction_history_of_messages_query(message_conditions))
        async for message in messages:
            key = (message.account_id, message.chat_id, message.id)
            states = [self._reaction_history_to_dict(row) for row in await reactions.take(key)]
            if iso:
                for state in states:
                    state["observed_at"] = state["observed_at"].isoformat()
            yield (
                message,
                await media.take(key),
                self._export_versions(await texts.take(key), await earlier.take(key), iso=iso),
                states,
            )
        if any(walk.pending is not None for walk in (media, texts, earlier, reactions)):
            raise RuntimeError("Export versions fell out of step with the messages")

    @staticmethod
    def _reaction_history_of_messages_query(message_conditions: list):
        """Every kept reaction state of the messages ``message_conditions`` pick, in export order.

        Like ``_versions_of_messages_query``: the conditions pick the message,
        so a message in a date window keeps its whole reaction history, and
        rows come in ``EXPORT_MESSAGE_ORDER``, each message's states oldest
        first, so a reader can walk them beside the messages.
        """
        return (
            select(
                ReactionHistory.account_id,
                ReactionHistory.chat_id,
                ReactionHistory.message_id,
                ReactionHistory.emoji,
                ReactionHistory.count,
                ReactionHistory.previous_count,
                ReactionHistory.observed_at,
                ReactionHistory.source,
            )
            .join(
                Message,
                and_(
                    Message.account_id == ReactionHistory.account_id,
                    Message.chat_id == ReactionHistory.chat_id,
                    Message.id == ReactionHistory.message_id,
                ),
            )
            .where(*message_conditions)
            .order_by(*EXPORT_MESSAGE_ORDER, ReactionHistory.observed_at.asc(), ReactionHistory.id.asc())
        )

    async def _read_one_snapshot(self, session) -> None:
        """Make every read ``session`` runs from here on see the same archive state.

        The exports read a message and its kept versions in separate
        statements. Under PostgreSQL's default READ COMMITTED each statement
        takes its own snapshot, so an edit landing between them would export
        the old text beside a version holding that same text. REPEATABLE READ
        gives the whole transaction one snapshot. SQLite's driver begins a
        transaction only before a write, so an explicit deferred BEGIN does
        the same there. Both exports need it on SQLite too: they read the
        transcripts to the end before they open the messages, versions and
        media statements, and without the BEGIN each read would see its own
        state, so a transcript could name a media a backup removed in
        between. Call it before the session runs anything.
        """
        if self._is_sqlite:
            await session.execute(text("BEGIN"))
        else:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    async def get_message_versions(
        self, chat_id: int, message_id: int, limit: int = 100, *, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Get preserved previous versions for a message (None account_id = unscoped until phase 4).

        A version whose media an edit replaced carries it as ``media``, a list
        of ``_media_version_to_dict`` rows: the media kept in ``media_versions``
        with the same ``date``, which is when that text and that media became
        current together. Earlier media with no text version of its date (the
        text version could not be written) is listed as its own version, with
        ``text`` None and ``media_only`` True, so no kept file goes unlisted.
        """
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
            versions = [self._message_version_to_dict(row) for row in result.scalars()]

            # Every earlier media of the message, in the order it was kept: its
            # position (1, 2, 3...) is the number its URL key names
            # (``get_media_version``). A message has a handful at most.
            media_stmt = (
                select(MediaVersion)
                .where(and_(MediaVersion.chat_id == chat_id, MediaVersion.message_id == message_id))
                .order_by(MediaVersion.id)
            )
            if account_id is not None:
                media_stmt = media_stmt.where(MediaVersion.account_id == account_id)
            media_rows = (await session.execute(media_stmt)).scalars().all()

        if not media_rows:
            return versions
        numbers: dict[int, int] = {}
        seen_per_account: dict[int, int] = {}
        for media_row in media_rows:
            seen_per_account[media_row.account_id] = seen_per_account.get(media_row.account_id, 0) + 1
            numbers[media_row.id] = seen_per_account[media_row.account_id]
        media_rows = sorted(media_rows, key=lambda row: (row.date, row.id), reverse=True)[:limit]
        by_date: dict[datetime, dict[str, Any]] = {}
        for version in versions:
            by_date.setdefault(version["date"], version)
        for media_row in media_rows:
            media = self._media_version_to_dict(media_row)
            media["number"] = numbers[media_row.id]
            version = by_date.get(media_row.date)
            if version is None:
                version = {
                    "chat_id": media_row.chat_id,
                    "message_id": media_row.message_id,
                    "text": None,
                    "date": media_row.date,
                    "captured_at": media_row.captured_at,
                    "source": media_row.source,
                    "entities": None,
                    "rich_message": None,
                    "media_only": True,
                }
                by_date[media_row.date] = version
                versions.append(version)
            version.setdefault("media", []).append(media)
        versions.sort(key=lambda version: version["date"], reverse=True)
        return versions[:limit]

    @staticmethod
    def _media_version_to_dict(row: MediaVersion) -> dict[str, Any]:
        """An earlier media for the edit history. ``file_path`` is for the web
        layer, which turns it into a URL and never sends it."""
        return {
            "id": row.id,
            "type": row.type,
            "file_name": row.file_name,
            "file_path": row.file_path,
            "file_size": row.file_size,
            "mime_type": row.mime_type,
            "width": row.width,
            "height": row.height,
            "duration": row.duration,
            "downloaded": bool(row.downloaded),
            "skip_reason": row.skip_reason,
            "first_seen": row.first_seen,
            "date": row.date,
            "captured_at": row.captured_at,
            "source": row.source,
        }

    async def get_media_version(
        self, chat_id: int, message_id: int, number: int, *, account_id: int
    ) -> dict[str, Any] | None:
        """One earlier media of one message in one chat, for the bytes routes.

        ``number`` is the media's position among the message's earlier media
        in the order they were kept (1 for the first), as
        ``get_message_versions`` numbers them: a URL names no archive-wide id.
        Chat- and account-bound in SQL like ``get_media_for_message``: another
        chat, account or message finds nothing.
        """
        if number < 1:
            return None
        async with self.db_manager.async_session_factory() as session:
            row = (
                await session.execute(
                    select(MediaVersion)
                    .where(
                        and_(
                            MediaVersion.account_id == account_id,
                            MediaVersion.chat_id == chat_id,
                            MediaVersion.message_id == message_id,
                        )
                    )
                    .order_by(MediaVersion.id)
                    .offset(number - 1)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.media_id,
                "account_id": row.account_id,
                "message_id": row.message_id,
                "chat_id": row.chat_id,
                "type": row.type,
                "file_path": row.file_path,
                "file_name": row.file_name,
                "file_size": row.file_size,
                "mime_type": row.mime_type,
                "downloaded": row.downloaded,
            }

    def _event_not_already_listed(
        self,
        scope: ChatScope | None,
        *,
        lower_rows,
        lower_chat,
        event_match: list,
    ):
        """True unless a LOWER entitled account already carries this same event.

        The changes feed's twin of ``ChatScope.displayed_copy_predicate``, keyed
        on the event rather than on the chat: same rule (non-private only,
        lowest entitled account wins, private never merges), different identity.

        A private chat is exempt because its ``chats.id`` is the other party's
        user id, so two accounts' rows with the same (chat id, message id) are
        two different messages in two different conversations — deduplicating
        them would delete one account's history from the feed.

        The grant is re-applied to the lower copy for the same reason the chat
        list re-applies it: a row may only be suppressed behind one the
        principal is actually entitled to see.
        """
        duplicate = (
            select(literal(1))
            .select_from(lower_rows)
            .join(
                lower_chat,
                and_(lower_chat.account_id == lower_rows.account_id, lower_chat.id == lower_rows.chat_id),
            )
            .where(lower_chat.type != PRIVATE_CHAT_TYPE, *event_match)
        )
        for predicate in (scope or ChatScope()).sql_predicates(lower_chat):
            duplicate = duplicate.where(predicate)
        return or_(Chat.type == PRIVATE_CHAT_TYPE, ~duplicate.exists())

    def _seconds_apart(self, first, second):
        """SQL for how many seconds lie between two timestamp columns, on either engine.

        SQLite stores them as text, which ``julianday`` reads (fractional
        seconds included); PostgreSQL subtracts them into an interval.
        """
        if self._is_sqlite:
            return func.abs(func.julianday(first) - func.julianday(second)) * 86400
        return func.abs(func.extract("epoch", first - second))

    async def get_recent_changes(
        self,
        *,
        since: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
        scope: ChatScope | None = None,
        with_transcripts: bool = True,
        with_reactions: bool = False,
        chat_id: int | None = None,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """The what-changed feed: deletions, edits, transcripts and reactions taken back.

        The archive's differentiator is that it KEEPS what disappeared; this
        is the query that finally lists it. Three streams share one shape:

        * ``deleted`` — soft-deleted messages (``is_deleted=1``), dated by
          ``deleted_at``, carrying the text the archive kept.
        * ``edited`` — ``message_versions`` rows, dated by ``captured_at``
          (when the archive observed the supersession), carrying the old text
          plus the message's CURRENT text.
        * ``transcript`` — finished voice transcripts, dated by
          ``completed_at``, carrying the transcript text and its language, so
          a poller sees new transcripts (docs/TRANSCRIPTION.md). Left out
          when ``with_transcripts`` is False, for a no-download login.
        * ``reaction`` — a reaction taken back: a ``reaction_history`` row
          whose count is below the one before it, dated by ``observed_at``,
          carrying the emoji, how many went (``count``), ``count_before``,
          ``count_after`` and the message's current text. Only when
          ``with_reactions`` is True: reactions come and go far more often
          than the rest, and the viewer asks for them only when the reader
          ticks the kind.

        Newest first. ``before`` is an exclusive keyset cursor over the
        per-row date: pass the last row's ``date`` back to page. Rows sharing
        that exact microsecond with the cursor are skipped — this is a review
        feed, not an export, and the export path is the lossless one.
        Entitlements ride ``scope.sql_predicates()`` against the joined chat
        row, the same compiled rules as the chat list — a restricted viewer's
        feed touches only their rows. Hard deletions cannot appear: their
        content no longer exists (DELETION_MODE=soft is what feeds this).

        ``chat_id`` with ``account_id`` narrows the feed to the chat a ref
        names. For a channel or a group that is the chat itself, every copy the
        scope allows: an event exists only in the copy whose listener was up
        when it happened, so narrowing to the ref's own copy would drop the
        events only another account captured, which the feed of every chat
        lists. Each event is still listed once, under the lowest entitled copy
        that holds it. A ref to a private chat narrows to the ref's own
        account, since its id is the other party's user id and names a
        different conversation in each account, whatever type another
        account's copy of that id carries. It narrows, never widens: ``scope``
        still applies, and the caller resolves the ref under the same scope
        first.
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
            transcript_stmt = (
                self._transcript_hit_messages(
                    select(
                        Message.id.label("message_id"),
                        MediaTranscript.completed_at.label("date"),
                        MediaTranscript.text,
                        MediaTranscript.language,
                        Message.sender_name,
                        Chat.ref,
                        Chat.title,
                        Chat.first_name,
                        Chat.last_name,
                        Chat.username,
                        Chat.type.label("chat_type"),
                    )
                )
                .join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
                .where(MediaTranscript.status == "done", MediaTranscript.completed_at.isnot(None))
            )
            reaction_stmt = (
                select(
                    ReactionHistory.message_id,
                    ReactionHistory.observed_at.label("date"),
                    ReactionHistory.emoji,
                    ReactionHistory.count,
                    ReactionHistory.previous_count,
                    Message.text,
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
                        Message.account_id == ReactionHistory.account_id,
                        Message.chat_id == ReactionHistory.chat_id,
                        Message.id == ReactionHistory.message_id,
                    ),
                )
                .join(Chat, and_(Chat.account_id == ReactionHistory.account_id, Chat.id == ReactionHistory.chat_id))
                # The partial index's own predicate, so the feed reads only drops.
                .where(ReactionHistory.count < ReactionHistory.previous_count)
            )
            if since is not None:
                deleted_stmt = deleted_stmt.where(Message.deleted_at >= since)
                edited_stmt = edited_stmt.where(MessageVersion.captured_at >= since)
                transcript_stmt = transcript_stmt.where(MediaTranscript.completed_at >= since)
                reaction_stmt = reaction_stmt.where(ReactionHistory.observed_at >= since)
            if before is not None:
                deleted_stmt = deleted_stmt.where(Message.deleted_at < before)
                edited_stmt = edited_stmt.where(MessageVersion.captured_at < before)
                transcript_stmt = transcript_stmt.where(MediaTranscript.completed_at < before)
                reaction_stmt = reaction_stmt.where(ReactionHistory.observed_at < before)
            if scope is not None:
                for predicate in scope.sql_predicates():
                    deleted_stmt = deleted_stmt.where(predicate)
                    edited_stmt = edited_stmt.where(predicate)
                    transcript_stmt = transcript_stmt.where(predicate)
                    reaction_stmt = reaction_stmt.where(predicate)
            if chat_id is not None:
                deleted_stmt = deleted_stmt.where(Message.chat_id == chat_id)
                edited_stmt = edited_stmt.where(MessageVersion.chat_id == chat_id)
                transcript_stmt = transcript_stmt.where(Message.chat_id == chat_id)
                reaction_stmt = reaction_stmt.where(ReactionHistory.chat_id == chat_id)
            if chat_id is not None and account_id is not None:
                # Only a private chat's id collides across accounts, so only
                # there does the ref's account pick the conversation. The
                # REF's type decides, not each row's: another account's copy
                # of the same id may be typed otherwise (an HTML-export import
                # stores "unknown"), and it is still another conversation.
                ref_type = await session.scalar(
                    select(Chat.type).where(Chat.account_id == account_id, Chat.id == chat_id)
                )
                if ref_type == PRIVATE_CHAT_TYPE:
                    deleted_stmt = deleted_stmt.where(Message.account_id == account_id)
                    edited_stmt = edited_stmt.where(MessageVersion.account_id == account_id)
                    transcript_stmt = transcript_stmt.where(Message.account_id == account_id)
                    reaction_stmt = reaction_stmt.where(ReactionHistory.account_id == account_id)
                else:
                    # Every non-private copy of a channel or group stays in,
                    # and the deduplication below lists each event once.
                    deleted_stmt = deleted_stmt.where(Chat.type != PRIVATE_CHAT_TYPE)
                    edited_stmt = edited_stmt.where(Chat.type != PRIVATE_CHAT_TYPE)
                    transcript_stmt = transcript_stmt.where(Chat.type != PRIVATE_CHAT_TYPE)
                    reaction_stmt = reaction_stmt.where(Chat.type != PRIVATE_CHAT_TYPE)

            # One row per EVENT, not per chat copy. Both accounts' listeners
            # see the same deletion in a channel they both hold, so both
            # archive it, a second apart, and the feed listed it twice.
            #
            # The chat list folds by choosing one COPY of the chat. Doing that
            # here would be wrong: an event only exists in the copy whose
            # listener was up when it happened, so dropping the other copy's
            # rows drops the event outright whenever the displayed account
            # missed it. The identity that matters here is the event — for a
            # non-private chat, (chat id, message id) names the same real
            # message in every account, and for an edit the superseded text
            # names the same revision.
            lower_deleted = aliased(Message, name="lower_deleted_message")
            lower_deleted_chat = aliased(Chat, name="lower_deleted_chat")
            deleted_duplicate = [
                lower_deleted.chat_id == Message.chat_id,
                lower_deleted.id == Message.id,
                lower_deleted.account_id < Message.account_id,
                lower_deleted.is_deleted == 1,
                lower_deleted.deleted_at.isnot(None),
            ]
            lower_edited = aliased(MessageVersion, name="lower_edited_version")
            lower_edited_chat = aliased(Chat, name="lower_edited_chat")
            edited_duplicate = [
                lower_edited.chat_id == MessageVersion.chat_id,
                lower_edited.message_id == MessageVersion.message_id,
                lower_edited.account_id < MessageVersion.account_id,
                # The superseded text IS the revision's identity: a message
                # edited twice is two events, and both accounts captured both.
                lower_edited.text.is_not_distinct_from(MessageVersion.text),
            ]
            # The window bounds ride into the duplicate check too, so a row is
            # suppressed only when a lower account carries one THIS query would
            # otherwise list. Without them an event whose other copy fell just
            # outside the window would vanish from the page instead of being
            # deduplicated.
            # A transcript is an event of the message: two accounts holding one
            # channel each transcribe their own media row of the same audio,
            # and the text names the result the way the superseded text names
            # an edit.
            lower_media = aliased(Media, name="lower_transcribed_media")
            lower_media_chat = aliased(Chat, name="lower_transcribed_chat")
            lower_transcript = aliased(MediaTranscript, name="lower_transcript")
            lower_transcript_match = [
                lower_transcript.account_id == lower_media.account_id,
                lower_transcript.media_id == lower_media.id,
                lower_transcript.status == "done",
                lower_transcript.completed_at.isnot(None),
                lower_transcript.text.is_not_distinct_from(MediaTranscript.text),
            ]
            # A drop is the same event in every account that saw it when the
            # emoji went from the same count to the same count at about the
            # same time. Each account's listener or backup notices it on its
            # own clock, so the times differ a little; the same counts far
            # apart are two drops (taken back, given again, taken back).
            lower_reaction = aliased(ReactionHistory, name="lower_reaction_state")
            lower_reaction_chat = aliased(Chat, name="lower_reaction_chat")
            reaction_duplicate = [
                lower_reaction.chat_id == ReactionHistory.chat_id,
                lower_reaction.message_id == ReactionHistory.message_id,
                lower_reaction.account_id < ReactionHistory.account_id,
                lower_reaction.emoji == ReactionHistory.emoji,
                lower_reaction.count == ReactionHistory.count,
                lower_reaction.previous_count == ReactionHistory.previous_count,
                self._seconds_apart(lower_reaction.observed_at, ReactionHistory.observed_at)
                <= REACTION_EVENT_TOLERANCE_SECONDS,
            ]
            if since is not None:
                deleted_duplicate.append(lower_deleted.deleted_at >= since)
                edited_duplicate.append(lower_edited.captured_at >= since)
                lower_transcript_match.append(lower_transcript.completed_at >= since)
                reaction_duplicate.append(lower_reaction.observed_at >= since)
            if before is not None:
                deleted_duplicate.append(lower_deleted.deleted_at < before)
                edited_duplicate.append(lower_edited.captured_at < before)
                lower_transcript_match.append(lower_transcript.completed_at < before)
                reaction_duplicate.append(lower_reaction.observed_at < before)
            transcript_duplicate = [
                lower_media.chat_id == Message.chat_id,
                lower_media.message_id == Message.id,
                lower_media.account_id < Message.account_id,
                # Correlated by name: auto-correlation reaches one level up only,
                # and this EXISTS sits two levels below the transcript row whose
                # text it compares, so without it the text rule matched any row.
                select(literal(1)).where(*lower_transcript_match).correlate(MediaTranscript, lower_media).exists(),
            ]

            # A feed narrowed to one chat runs the check too: every copy of a
            # shared chat is inside that filter, so the lower copy an event
            # defers to is listed, and a private chat is exempt anyway.
            deleted_stmt = deleted_stmt.where(
                self._event_not_already_listed(
                    scope, lower_rows=lower_deleted, lower_chat=lower_deleted_chat, event_match=deleted_duplicate
                )
            )
            edited_stmt = edited_stmt.where(
                self._event_not_already_listed(
                    scope, lower_rows=lower_edited, lower_chat=lower_edited_chat, event_match=edited_duplicate
                )
            )

            transcript_stmt = transcript_stmt.where(
                self._event_not_already_listed(
                    scope, lower_rows=lower_media, lower_chat=lower_media_chat, event_match=transcript_duplicate
                )
            )

            reaction_stmt = reaction_stmt.where(
                self._event_not_already_listed(
                    scope, lower_rows=lower_reaction, lower_chat=lower_reaction_chat, event_match=reaction_duplicate
                )
            )

            deleted_stmt = deleted_stmt.order_by(Message.deleted_at.desc()).limit(per_stream)
            edited_stmt = edited_stmt.order_by(MessageVersion.captured_at.desc()).limit(per_stream)
            transcript_stmt = transcript_stmt.order_by(MediaTranscript.completed_at.desc()).limit(per_stream)
            reaction_stmt = reaction_stmt.order_by(ReactionHistory.observed_at.desc(), ReactionHistory.id.desc()).limit(
                per_stream
            )

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
            transcript_rows = (await session.execute(transcript_stmt)).all() if with_transcripts else []
            for row in transcript_rows:
                changes.append(
                    {
                        "kind": "transcript",
                        "date": row.date.isoformat() if row.date else None,
                        "chat": _chat_fields(row),
                        "message_id": row.message_id,
                        "sender_name": row.sender_name,
                        "text": row.text,
                        "language": row.language,
                    }
                )
            reaction_rows = (await session.execute(reaction_stmt)).all() if with_reactions else []
            for row in reaction_rows:
                changes.append(
                    {
                        "kind": "reaction",
                        "date": row.date.isoformat() if row.date else None,
                        "chat": _chat_fields(row),
                        "message_id": row.message_id,
                        "sender_name": row.sender_name,
                        "text": row.text,
                        "emoji": row.emoji,
                        "count": row.previous_count - row.count,
                        "count_before": row.previous_count,
                        "count_after": row.count,
                    }
                )
            changes.sort(key=lambda c: c["date"] or "", reverse=True)
            return changes[:per_stream]

    @staticmethod
    def _marked_edited_predicate():
        """Telegram marks a message edited: ``edit_date`` is set and its
        ``edit_hide`` flag is not. Telegram bumps ``edit_date`` for a
        reaction-only change and sets ``edit_hide`` to say the edit must not be
        shown; NULL (a row from before the flag was kept) reads as shown."""
        return and_(Message.edit_date.isnot(None), func.coalesce(Message.edit_hide, 0) == 0)

    @staticmethod
    def _edited_predicate():
        """A message counts as edited when Telegram marks it
        (``_marked_edited_predicate``) or the archive kept an earlier version of
        it. Either alone happens: a message first captured after its edit is
        marked and has no version, and a late-hydrated empty text keeps a
        version with no mark. It is the viewer's own rule for the pencil in a
        bubble, so the chat's count, the "Edited only" list and the marked
        bubbles agree."""
        kept = exists().where(
            MessageVersion.account_id == Message.account_id,
            MessageVersion.chat_id == Message.chat_id,
            MessageVersion.message_id == Message.id,
        )
        return or_(DatabaseAdapter._marked_edited_predicate(), kept)

    async def get_chat_stats(
        self, chat_id: int, *, account_id: int | None = None, with_kept_changes: bool = False
    ) -> dict[str, Any]:
        """Get statistics for a specific chat (message count, media count, total size).

        None account_id = unscoped until phase 4.

        Returns:
            Dict with keys: messages, media_files, total_size_bytes, first_message_date,
            last_message_date. With ``with_kept_changes`` also deleted_messages (deleted
            in Telegram, kept here) and edited_messages (``_edited_predicate``:
            marked edited by Telegram, or with an earlier version kept).
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

            # First and last message dates. Only MIN and MAX here: PostgreSQL
            # answers those from the date index when they are the query's only
            # aggregates, and any other aggregate beside them reads every row.
            date_result = await session.execute(
                select(func.min(Message.date), func.max(Message.date)).where(and_(*msg_where))
            )
            date_row = date_result.one()
            first_message = date_row[0]
            last_message = date_row[1]

            stats = {
                "chat_id": chat_id,
                "messages": int(message_count),
                "media_files": int(media_count),
                "total_size_bytes": int(total_size),
                "total_size_mb": round(total_size / (1024 * 1024), 2) if total_size else 0,
                "first_message_date": first_message.isoformat() if first_message else None,
                "last_message_date": last_message.isoformat() if last_message else None,
            }
            if not with_kept_changes:
                return stats

            # What the archive kept that the chat itself no longer shows, in its
            # own query so the date query above keeps its index shortcut. Only
            # the viewer's chat info asks for it; the import pre-check does not.
            kept_result = await session.execute(
                select(
                    func.coalesce(func.sum(case((Message.is_deleted == 1, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((self._marked_edited_predicate(), 1), else_=0)), 0),
                ).where(and_(*msg_where))
            )
            kept_row = kept_result.one()
            # Edited is _edited_predicate, counted in two cheap parts so this
            # scan needs no per-row lookup: the rows Telegram marks, plus the
            # few unmarked rows that have a kept version (read from the small
            # versions table and its chat index).
            unmarked = (
                select(MessageVersion.account_id, MessageVersion.message_id)
                .join(
                    Message,
                    and_(
                        Message.account_id == MessageVersion.account_id,
                        Message.chat_id == MessageVersion.chat_id,
                        Message.id == MessageVersion.message_id,
                    ),
                )
                .where(MessageVersion.chat_id == chat_id, not_(self._marked_edited_predicate()))
                .distinct()
            )
            if account_id is not None:
                unmarked = unmarked.where(MessageVersion.account_id == account_id)
            unmarked_count = (
                await session.execute(select(func.count()).select_from(unmarked.subquery("unmarked_edits")))
            ).scalar()
            stats["deleted_messages"] = int(kept_row[0] or 0)
            stats["edited_messages"] = int(kept_row[1] or 0) + int(unmarked_count or 0)
            return stats

    # ========== Media Operations ==========

    @retry_on_locked()
    async def insert_media(self, media_data: dict[str, Any], *, account_id: int) -> str | None:
        """Insert (or upsert) a media file record; the id written, or None.

        Contract for the ``downloaded`` key: include it whenever the caller
        actually observed the download outcome (True after a successful write,
        False after a skip/failure it is willing to have retried), and OMIT it
        when the caller cannot know whether a file is on disk. An omitted key
        means "leave the stored flag alone" on conflict and 0 on a fresh insert —
        see the comment on the conflict clause below.

        A row that names its file (``telegram_file_id``) is written only where
        that file belongs, checked in the same transaction as the write
        (``_media_write_id``): a download that was still running when an edit
        replaced the media never lands in the new media's row. None means
        nothing was written to the message's current media.
        """
        async with self.db_manager.async_session_factory() as session:
            media_id = media_data["id"]
            if media_data.get("telegram_file_id") is not None:
                media_id = await self._media_write_id(session, media_data, account_id=account_id)
                if media_id is None:
                    await session.commit()
                    return None
            values = {
                "account_id": account_id,
                "id": media_id,
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
                "skip_reason": media_data.get("skip_reason"),
                "download_date": media_data.get("download_date"),
                "telegram_file_id": media_data.get("telegram_file_id"),
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
                "telegram_file_id",
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
            # ``skip_reason`` (#465) follows the same presence rule. A writer that
            # names it (the over-size and filter skips) sets it; a writer that
            # observed a download outcome without naming it has settled the
            # question, so the reason clears; a writer that knows nothing keeps
            # what is stored.
            if "skip_reason" not in media_data and "downloaded" not in media_data:
                update_values["skip_reason"] = Media.skip_reason
            stmt = stmt.on_conflict_do_update(index_elements=["account_id", "id"], set_=update_values)

            await session.execute(stmt)
            await session.commit()
            return media_id

    async def _media_write_id(self, session, media_data: dict[str, Any], *, account_id: int) -> str | None:
        """The media id a downloaded file may be written under, or None.

        The caller asked ``reconcile_media_row`` for the id before it started
        the download. An edit can replace the media in the meantime: the row
        under that id then moved to ``media_versions`` and the message's
        current media has another id and another file. So the file decides:

        - the row under the id holds this file, or an unknown one, or both
          are link previews (``_is_preview_refresh``): write there;
        - the id was kept as an earlier media of this very file: its file
          values are filled in the ``media_versions`` row, which had none,
          and nothing is written to the current media;
        - the message has another current media: nothing is written to it.
          A downloaded file is kept as an earlier media of the message
          (``_keep_late_download``), so a file the archive read is never
          forgotten when another writer stored other media first;
        - the message has no media row: a new row, under the id unless a kept
          version already holds it.
        """
        media_id = media_data["id"]
        file_id = str(media_data["telegram_file_id"])
        chat_id = media_data.get("chat_id")
        message_id = media_data.get("message_id")
        message = None
        if chat_id is not None and message_id is not None:
            # The lock _replace_media_row takes: the check below and the write
            # after it cannot interleave with a replacement.
            message = await self._load_message_for_update(session, account_id, chat_id, message_id)
        row = (
            await session.execute(
                select(Media.telegram_file_id, Media.file_name, Media.type).where(
                    and_(Media.account_id == account_id, Media.id == media_id)
                )
            )
        ).first()
        if row is not None:
            stored = stored_media_file_id(row.telegram_file_id, row.file_name)
            if stored is None or stored == file_id or _is_preview_refresh(row.type, media_data.get("type")):
                return media_id
            logger.debug("Media changed while it was downloading; the current media is left as it is")
            await self._keep_late_download(session, media_data, message, account_id=account_id)
            return None
        kept = (
            await session.execute(
                select(MediaVersion).where(
                    and_(MediaVersion.account_id == account_id, MediaVersion.media_id == media_id)
                )
            )
        ).scalar_one_or_none()
        if kept is not None and kept.telegram_file_id == file_id:
            if media_data.get("downloaded") and not kept.downloaded:
                await self._fill_media_version_file(session, kept.id, media_data)
            return None
        current = (
            await session.execute(
                select(Media.id)
                .where(
                    and_(
                        Media.account_id == account_id,
                        Media.chat_id == chat_id,
                        Media.message_id == message_id,
                    )
                )
                .limit(1)
            )
        ).first()
        if current is not None:
            logger.debug("Media changed while it was downloading; the current media is left as it is")
            await self._keep_late_download(session, media_data, message, account_id=account_id)
            return None
        if kept is not None:
            return await self._free_media_id(session, account_id, media_id)
        return media_id

    async def _keep_late_download(
        self, session, media_data: dict[str, Any], message: Message | None, *, account_id: int
    ) -> None:
        """Keep a downloaded file as an earlier media when another writer stored other media first.

        A backup batch reads a message and downloads its file; before the
        batch is written, the listener stores the message with other media
        under the same id. The file the batch read is still part of the
        message's history, so it is kept in ``media_versions``: a version
        of this file with no file of its own is filled, and otherwise a new
        version is added. ``version_date`` and ``version_source`` in
        ``media_data`` date it and name the path that read it; without them
        it is dated at the message's send time. A write that downloaded
        nothing keeps nothing.
        """
        if not media_data.get("downloaded") or message is None:
            return
        file_id = str(media_data["telegram_file_id"])
        versions = (
            (
                await session.execute(
                    select(MediaVersion)
                    .where(
                        and_(
                            MediaVersion.account_id == account_id,
                            MediaVersion.chat_id == message.chat_id,
                            MediaVersion.message_id == message.id,
                            MediaVersion.telegram_file_id == file_id,
                        )
                    )
                    .order_by(MediaVersion.id)
                )
            )
            .scalars()
            .all()
        )
        if any(version.downloaded for version in versions):
            return
        if versions:
            await self._fill_media_version_file(session, versions[0].id, media_data)
            return
        now = utcnow_naive()
        await session.execute(
            insert(MediaVersion).values(
                account_id=account_id,
                chat_id=message.chat_id,
                message_id=message.id,
                media_id=await self._free_media_id(session, account_id, media_data["id"]),
                type=media_data.get("type"),
                telegram_file_id=file_id,
                file_path=media_data.get("file_path"),
                file_name=media_data.get("file_name"),
                file_size=media_data.get("file_size"),
                mime_type=media_data.get("mime_type"),
                width=media_data.get("width"),
                height=media_data.get("height"),
                duration=media_data.get("duration"),
                content_hash=media_data.get("content_hash"),
                downloaded=1,
                download_date=media_data.get("download_date"),
                first_seen=now,
                date=_strip_tz(media_data.get("version_date")) or _strip_tz(message.date),
                captured_at=now,
                source=media_data.get("version_source"),
            )
        )
        logger.debug("Kept a late download as an earlier media")

    @staticmethod
    async def _fill_media_version_file(session, version_id: int, media_data: dict[str, Any]) -> None:
        """Give a kept media version the file a late download fetched for it.

        Only a version with no file (kept while its download was still
        running) is filled. Each value falls back to what the version holds,
        as ``insert_media`` does on conflict.
        """
        values: dict[str, Any] = {"downloaded": 1}
        for column in (
            "file_path",
            "file_name",
            "file_size",
            "mime_type",
            "width",
            "height",
            "duration",
            "content_hash",
            "download_date",
        ):
            if media_data.get(column) is not None:
                values[column] = media_data[column]
        await session.execute(
            update(MediaVersion)
            .where(and_(MediaVersion.id == version_id, MediaVersion.downloaded == 0))
            .values(**values)
        )
        logger.debug("Filled the file of an earlier media")

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
        never surface another account's rows. The earlier media edits replaced
        (``media_versions``) are listed too, with ``"version": True``:
        ``delete_media_for_chat`` removes their rows as well.

        Args:
            chat_id: Chat identifier

        Returns:
            List of media records with file paths and metadata
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(and_(Media.account_id == account_id, Media.chat_id == chat_id))
            result = await session.execute(stmt)
            media_records = result.scalars().all()
            version_stmt = select(MediaVersion).where(
                and_(MediaVersion.account_id == account_id, MediaVersion.chat_id == chat_id)
            )
            version_records = (await session.execute(version_stmt)).scalars().all()

            records = [
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
            records += [
                {
                    "id": v.media_id,
                    "message_id": v.message_id,
                    "chat_id": v.chat_id,
                    "type": v.type,
                    "file_path": v.file_path,
                    "file_size": v.file_size,
                    "downloaded": v.downloaded,
                    "version": True,
                }
                for v in version_records
            ]
            return records

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
                    Message.text,
                    Message.is_deleted,
                    Message.deleted_at,
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
                    # The message's own caption and its kept deletion with its date,
                    # so a photo opened from the gallery shows what it shows in the chat.
                    "text": text or "",
                    "is_deleted": bool(is_deleted),
                    "deleted_at": deleted_at.isoformat() if deleted_at else None,
                }
                for (
                    media,
                    msg_date,
                    sender_name,
                    text,
                    is_deleted,
                    deleted_at,
                    first_name,
                    last_name,
                    username,
                ) in rows[:limit]
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
            await self._delete_media_versions_of(
                session,
                and_(MediaVersion.account_id == account_id, MediaVersion.chat_id == chat_id),
                account_id=account_id,
            )
            chat_media = and_(Media.account_id == account_id, Media.chat_id == chat_id)
            await session.execute(self._delete_transcripts_of(chat_media, account_id=account_id))
            result = await session.execute(delete(Media).where(chat_media))
            await session.commit()
            return result.rowcount

    async def get_webpage_preview_documents(self, *, account_id: int) -> list[dict[str, Any]]:
        """One account's link-preview rows whose payload is a FILE, with the
        preview URL the message recorded.

        ``mime_type IS NOT NULL`` is the document-backed discriminator: it is
        read off ``.document`` by ``extract_media_attributes``, so a card whose
        preview was only a thumbnail (``.photo``) has it NULL. That keeps the
        cheap thumbnails — tens of KB, and the card's picture — out of the
        YouTube cleanup, which is only ever after the video files.

        The URL is returned raw rather than matched in SQL: ``raw_data`` is a
        TEXT column holding JSON, so a SQL match would need one dialect-specific
        spelling for PostgreSQL and another for SQLite, and a second URL
        predicate that could drift from ``is_youtube_url``.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Media.id,
                    Media.chat_id,
                    Media.message_id,
                    Media.file_path,
                    Media.file_name,
                    Media.file_size,
                    Media.content_hash,
                    Media.downloaded,
                    Message.raw_data,
                )
                .join(
                    Message,
                    and_(
                        Message.account_id == Media.account_id,
                        Message.chat_id == Media.chat_id,
                        Message.id == Media.message_id,
                    ),
                )
                .where(
                    and_(
                        Media.account_id == account_id,
                        Media.type == "webpage",
                        Media.mime_type.isnot(None),
                    )
                )
            )
            rows = (await session.execute(stmt)).all()

        records: list[dict[str, Any]] = []
        for row in rows:
            url = None
            if row.raw_data:
                try:
                    webpage = json.loads(row.raw_data).get("webpage")
                except ValueError, TypeError:
                    webpage = None
                if isinstance(webpage, dict):
                    url = webpage.get("url") or webpage.get("display_url")
            records.append(
                {
                    "id": row.id,
                    "chat_id": row.chat_id,
                    "message_id": row.message_id,
                    "file_path": row.file_path,
                    "file_name": row.file_name,
                    "file_size": row.file_size,
                    "content_hash": row.content_hash,
                    "downloaded": row.downloaded,
                    "url": url,
                }
            )
        return records

    async def delete_media_records(
        self, media_ids: Collection[str], *, account_id: int, with_transcripts: bool = False
    ) -> int:
        """Delete specific media rows by id. Returns how many were removed.

        A media id repeats across accounts (``{chat}_{msg}_{type}``), so the
        account leads the predicate here exactly as it does in
        ``increment_media_download_attempts``.

        ``with_transcripts`` also deletes the transcript rows of those media.
        Only the flag-gated ``YOUTUBE_VIDEOS_DELETE_EXISTING`` cleanup passes
        it; the pending-twin cleanup, which runs on every backup, removes the
        media row only, the way ``delete_voice_note_audio_twins`` does.
        """
        ids = list(media_ids)
        if not ids:
            return 0
        deleted = 0
        async with self.db_manager.async_session_factory() as session:
            # Chunked: SQLite caps a statement at 999 bound parameters by default,
            # and a large archive can exceed that in one cleanup pass.
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                if with_transcripts:
                    await session.execute(
                        delete(MediaTranscript).where(
                            and_(MediaTranscript.account_id == account_id, MediaTranscript.media_id.in_(chunk))
                        )
                    )
                stmt = delete(Media).where(and_(Media.account_id == account_id, Media.id.in_(chunk)))
                result = await session.execute(stmt)
                deleted += result.rowcount or 0
            await session.commit()
        return deleted

    async def delete_voice_note_audio_twins(self, *, account_id: int) -> int:
        """Delete ``audio`` rows that duplicate a ``voice`` row for the same file.

        The classifier types a document ``voice`` only when Telegram's
        ``DocumentAttributeAudio.voice`` flag is set, so ``voice`` is the more
        specific judgement. An earlier capture that ignored the flag filed the
        same note as ``audio``, and before #426 a second classification minted a
        second row instead of correcting the first. Both rows name the same
        file, so the Voice tab (which lists ``voice,audio``) showed each such
        note twice and its badge added both, while the timeline, which reads the
        lowest id first, played the older ``audio`` twin as music.

        Only rows go, and only when the ``voice`` twin is downloaded and names
        the very same ``file_path``, so the file stays referenced by the row that
        remains. Returns the number of rows deleted.

        Transcripts are left alone: a transcript of the removed row stays in
        its table, and the viewer shows it on the voice twin, which holds the
        same audio, until that twin has rows of its own (docs/TRANSCRIPTION.md).
        """
        twin = aliased(Media)
        async with self.db_manager.async_session_factory() as session:
            stmt = delete(Media).where(
                and_(
                    Media.account_id == account_id,
                    Media.type == "audio",
                    Media.downloaded == 1,
                    select(twin.id)
                    .where(
                        and_(
                            twin.account_id == Media.account_id,
                            twin.chat_id == Media.chat_id,
                            twin.message_id == Media.message_id,
                            twin.type == "voice",
                            twin.downloaded == 1,
                            twin.file_path == Media.file_path,
                        )
                    )
                    .correlate(Media)
                    .exists(),
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    async def count_media_by_content_hash(self, content_hashes: Collection[str]) -> dict[str, int]:
        """How many media rows still reference each content hash, ALL accounts.

        Deliberately not scoped to one account: the shared store is keyed by
        (file_name, content_hash) with no account in the path, so a blob another
        account's row points at must survive this account's cleanup. Earlier
        media an edit replaced (``media_versions``) count too: a kept version
        whose file is the same blob keeps it.
        """
        hashes = [h for h in dict.fromkeys(content_hashes) if h]
        if not hashes:
            return {}
        counts: dict[str, int] = {}
        async with self.db_manager.async_session_factory() as session:
            for start in range(0, len(hashes), 500):
                chunk = hashes[start : start + 500]
                for model in (Media, MediaVersion):
                    stmt = (
                        select(model.content_hash, func.count())
                        .where(model.content_hash.in_(chunk))
                        .group_by(model.content_hash)
                    )
                    for content_hash, count in (await session.execute(stmt)).all():
                        counts[content_hash] = counts.get(content_hash, 0) + count
        return counts

    async def get_media_paths_by_content_hash(self, content_hash: str, *, limit: int = 20) -> list[str]:
        """Stored paths of media rows and kept earlier media with this content hash, ALL accounts.

        The places a copy of the same bytes may already sit, for repairing a row
        whose own file is gone. Paths as stored: absolute, or relative to the
        media root for imported rows.
        """
        if not content_hash:
            return []
        paths: list[str] = []
        async with self.db_manager.async_session_factory() as session:
            for model in (Media, MediaVersion):
                stmt = (
                    select(model.file_path)
                    .where(and_(model.content_hash == content_hash, model.file_path.isnot(None)))
                    .distinct()
                    .limit(limit)
                )
                paths.extend(value for (value,) in (await session.execute(stmt)).all())
        return list(dict.fromkeys(paths))[:limit]

    async def count_shared_blob_references(self, blobs: Collection[tuple[str, str | None]]) -> dict[str, int]:
        """How many rows, ALL accounts, still refer to each ``_shared`` blob, keyed by file name.

        A row refers to a blob when it names the same file (``file_name``) or
        holds the same bytes (``content_hash``). Both count, because neither is
        complete alone: rows written before content hashing existed carry no
        hash, and a blob reused for a duplicate under another name is named by
        its hash only. Earlier media an edit replaced (``media_versions``) count
        too. A blob with any reference must stay.
        """
        wanted = {name: content_hash for name, content_hash in blobs if name}
        if not wanted:
            return {}
        counts: dict[str, int] = {}
        async with self.db_manager.async_session_factory() as session:
            for name, content_hash in wanted.items():
                total = 0
                for model in (Media, MediaVersion):
                    match = model.file_name == name
                    if content_hash:
                        match = or_(match, model.content_hash == content_hash)
                    total += (await session.execute(select(func.count()).select_from(model).where(match))).scalar_one()
                if total:
                    counts[name] = total
        return counts

    async def referenced_file_paths(self, values: Collection[str]) -> set[str]:
        """The subset of ``values`` that some media row or kept earlier media, ALL accounts, names as its file_path."""
        wanted = [value for value in dict.fromkeys(values) if value]
        found: set[str] = set()
        if not wanted:
            return found
        async with self.db_manager.async_session_factory() as session:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                for model in (Media, MediaVersion):
                    stmt = select(model.file_path).where(model.file_path.in_(chunk)).distinct()
                    found.update(value for (value,) in (await session.execute(stmt)).all())
        return found

    async def count_media_rows_in_folder(self, chat_id: int, folder_prefixes: Collection[str]) -> int:
        """Rows of any account whose files sit in a chat's media folder.

        A row belongs when it carries the chat id (every account's copy of a
        chat shares ``<media>/<chat_id>/``) or when its stored path starts with
        one of ``folder_prefixes`` (a legacy row of another id form can still
        point there). Media rows and kept earlier media both count.
        """
        prefixes = [prefix for prefix in folder_prefixes if prefix]
        total = 0
        async with self.db_manager.async_session_factory() as session:
            for model in (Media, MediaVersion):
                match = or_(
                    model.chat_id == chat_id, *(model.file_path.startswith(p, autoescape=True) for p in prefixes)
                )
                total += (await session.execute(select(func.count()).select_from(model).where(match))).scalar_one()
        return total

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
                        Media.content_hash,
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
                    "content_hash": r[8],
                    "account_id": account_id,
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

    async def get_payload_backfill_rows(
        self, *, account_id: int, chat_id: int | None = None
    ) -> dict[int, list[dict[str, Any]]]:
        """The work list of ``backfill-details``, grouped by chat, ordered by message id.

        A media row of a ``PAYLOAD_BACKFILL_TYPES`` kind is listed when its
        message's ``raw_data`` lacks the key of the same name, or when the row
        still carries a ``file_path`` (the leftover of releases up to v7.28.0).
        Each entry is ``{message_id, media_id, type, file_path, has_payload}``.
        A row whose ``raw_data`` does not parse is left out: nothing may be
        added to a payload the archive cannot read without destroying it.
        Filling a key or clearing a path takes a row off the list, so an
        interrupted run resumes by running again.
        """
        stmt = (
            select(Media.chat_id, Media.message_id, Media.id, Media.type, Media.file_path, Message.raw_data)
            .join(
                Message,
                and_(
                    Message.account_id == Media.account_id,
                    Message.chat_id == Media.chat_id,
                    Message.id == Media.message_id,
                ),
            )
            .where(and_(Media.account_id == account_id, Media.type.in_(PAYLOAD_BACKFILL_TYPES)))
            .order_by(Media.chat_id, Media.message_id, Media.id)
        )
        if chat_id is not None:
            stmt = stmt.where(Media.chat_id == chat_id)
        grouped: dict[int, list[dict[str, Any]]] = {}
        async with self.db_manager.async_session_factory() as session:
            result = await session.stream(stmt.execution_options(yield_per=1000))
            async for row_chat, message_id, media_id, media_type, file_path, raw_data in result:
                raw = _raw_data_dict(raw_data)
                if raw is None:
                    continue
                has_payload = media_type in raw
                if has_payload and not file_path:
                    continue
                grouped.setdefault(row_chat, []).append(
                    {
                        "message_id": message_id,
                        "media_id": media_id,
                        "type": media_type,
                        "file_path": file_path or None,
                        "has_payload": has_payload,
                    }
                )
        return grouped

    @retry_on_locked()
    async def add_missing_raw_data_keys(
        self, chat_id: int, message_id: int, payload: dict[str, Any], *, account_id: int
    ) -> bool:
        """Add each key of ``payload`` the message's ``raw_data`` lacks; True if anything was added.

        For ``backfill-details``. The row is locked first, so a writer that
        stores the same key meanwhile wins and this adds nothing. A key the
        row already holds is never replaced, and nothing else on the row
        (text, dates, reactions, other keys) is touched: this is not the
        upsert. A row whose ``raw_data`` does not parse is left as it is.
        """
        if not payload:
            return False
        async with self.db_manager.async_session_factory() as session:
            message = await self._load_message_for_update(session, account_id, chat_id, message_id)
            if message is None:
                await session.rollback()
                return False
            raw = _raw_data_dict(message.raw_data)
            if raw is None:
                await session.rollback()
                return False
            merged = dict(raw)
            for key, value in payload.items():
                if key not in merged:
                    merged[key] = value
            if merged == raw:
                await session.rollback()
                return False
            message.raw_data = json.dumps(merged)
            await session.commit()
            return True

    async def raw_data_has_key(self, chat_id: int, message_id: int, key: str, *, account_id: int) -> bool:
        """True when the message exists and its ``raw_data`` parses and holds ``key``."""
        async with self.db_manager.async_session_factory() as session:
            raw_data = (
                await session.execute(
                    select(Message.raw_data).where(
                        and_(Message.account_id == account_id, Message.chat_id == chat_id, Message.id == message_id)
                    )
                )
            ).scalar_one_or_none()
        raw = _raw_data_dict(raw_data)
        return raw is not None and key in raw

    @retry_on_locked()
    async def clear_metadata_media_path(self, chat_id: int, media_id: str, *, account_id: int) -> bool:
        """Clear the leftover file fields of a metadata-only media row; True if a row changed.

        Sets ``file_path``, ``file_name`` and ``download_date`` to NULL and
        ``downloaded`` to 0, so the row reads as what it is: a location, a
        contact or a poll with no file. The row stays, and nothing on disk is
        touched. Only a metadata-only row that still has a path matches, so a
        second call changes nothing.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Media)
                .where(
                    and_(
                        Media.account_id == account_id,
                        Media.chat_id == chat_id,
                        Media.id == media_id,
                        Media.type.in_(METADATA_ONLY_MEDIA_TYPES),
                        Media.file_path.is_not(None),
                    )
                )
                .values(file_path=None, file_name=None, download_date=None, downloaded=0)
            )
            await session.commit()
            return (result.rowcount or 0) > 0

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
        self,
        chat_id: int,
        message_id: int,
        media_type: str,
        *,
        account_id: int,
        telegram_file_id: str | None = None,
        source: str | None = None,
        edit_date: datetime | None = None,
        edit_hide: int | bool | None = None,
    ) -> dict[str, Any] | None:
        """The media row this message already has, re-typed to the current
        judgement, or None when the message has no media row yet.

        ``telegram_file_id`` is the id of the photo or document the message
        carries now (``media_file_id``). When the row holds a different known
        id and the read shows an edit that can have made the change, the
        media was replaced: the row is kept as a ``media_versions`` row and
        comes back empty under a new id, with ``"replaced": True``, so the
        caller downloads the new file into it (``_replace_media_row``).
        ``source`` names the caller's path on the versions that writes, and
        ``edit_date`` and ``edit_hide`` are the edit date and Telegram's
        hidden-edit flag of the message the caller read.

        When the row holds another file and was not replaced (the read shows
        no such edit, or the row could not be kept), it comes back with
        ``"superseded": True``: it holds other media than the caller's, and
        the caller must not download into it.

        A link preview's photo or document is not compared when the row holds
        a link preview too: Telegram crawls the page again and can serve
        another preview photo for a message nobody edited
        (``_is_preview_refresh``).

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
        was first filed under: opaque and stable. A reclassification never
        re-keys it; only a replacement does, and then the old id stays with
        the old media in ``media_versions``. Nothing reads its shape any more:
        the viewer builds URL keys from the message and type columns, and
        ``get_media_for_message`` looks rows up the same way. Only ``type`` is
        corrected, which is the value every reader actually consults.

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
            if telegram_file_id is not None and not _is_preview_refresh(row.type, media_type):
                stored = stored_media_file_id(row.telegram_file_id, row.file_name)
                if stored is not None and stored != str(telegram_file_id):
                    row_id = row.id
                    await session.rollback()
                    replaced = await self._replace_media_row(
                        chat_id,
                        message_id,
                        row_id,
                        media_type,
                        str(telegram_file_id),
                        account_id=account_id,
                        source=source,
                        edit_date=edit_date,
                        edit_hide=edit_hide,
                    )
                    if replaced is not None:
                        return replaced
                    # Not replaced here. Either another writer replaced it
                    # first (the row now holds this file), or the read shows
                    # no edit that can have replaced the media, or the row
                    # could not be kept. Only the first may take a download.
                    current = await self.reconcile_media_row(chat_id, message_id, None, account_id=account_id)
                    if current is not None:
                        now = stored_media_file_id(current["telegram_file_id"], current["file_name"])
                        if now is not None and now != str(telegram_file_id):
                            current["superseded"] = True
                    return current
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
                "telegram_file_id": row.telegram_file_id,
            }

    @staticmethod
    def _shows_replacing_edit(message: Message, edit_date: datetime | None, edit_hide: int | bool | None) -> bool:
        """True when a read shows an edit that can have replaced the media.

        Telegram moves a message's edit date when its photo or file is
        swapped, and shows that edit. So the read must carry an edit date,
        not hidden (a hidden edit is a reaction, #219), and not older than
        the archived edit. The same date is allowed: the date has one-second
        resolution and a bot can edit twice within one second. A read without
        this evidence replaces nothing, whatever id it carries: a link preview
        crawled again, or a stored id that does not name the file.
        """
        read = _strip_tz(edit_date)
        if read is None or edit_hide:
            return False
        archived = _strip_tz(message.edit_date)
        return archived is None or read >= archived

    @staticmethod
    async def _free_media_id(session, account_id: int, base: str) -> str:
        """``{base}_v{n}`` with the lowest n above every one in use.

        Checked against both tables: a media row and a kept version must never
        share an id, and a row-level cleanup can leave gaps that a count of
        versions would walk back into. A base that is itself re-keyed counts
        from its own stem, so ids never grow a second suffix.
        """
        base = re.sub(r"_v[0-9]+$", "", base)
        pattern = base.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + r"\_v%"
        taken = set(
            (
                await session.execute(
                    select(Media.id).where(and_(Media.account_id == account_id, Media.id.like(pattern, escape="\\")))
                )
            ).scalars()
        )
        taken.update(
            (
                await session.execute(
                    select(MediaVersion.media_id).where(
                        and_(MediaVersion.account_id == account_id, MediaVersion.media_id.like(pattern, escape="\\"))
                    )
                )
            ).scalars()
        )
        highest = 0
        for taken_id in taken:
            suffix = taken_id[len(base) + 2 :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{base}_v{highest + 1}"

    @retry_on_locked()
    async def _replace_media_row(
        self,
        chat_id: int,
        message_id: int,
        media_id: str,
        media_type: str | None,
        telegram_file_id: str,
        *,
        account_id: int,
        source: str | None,
        edit_date: datetime | None = None,
        edit_hide: int | bool | None = None,
    ) -> dict[str, Any] | None:
        """Keep a replaced media row as a version and free the row for the new file.

        One transaction, under the message's row lock, so two paths that see
        the same edit replace the row once:

        1. the message's text as it was is kept as a ``message_versions`` row
           (a no-op when that version is already there), dated like the media;
        2. the media row's file and what is known about it (type, file id,
           path, name, size, MIME type, dimensions, duration, content hash,
           download state and date, skip reason, and its ``created_at`` as
           ``first_seen``) are copied to ``media_versions``, with the id the
           row had, so its file and its transcripts stay findable. Its
           download attempt count is not kept;
        3. the media row takes a new id and the new file's identity, with every
           file value cleared, ``downloaded=0`` and ``created_at`` set to now,
           so no later write can mix the old file into it. The caller
           downloads the new file into it.

        ``edit_date`` and ``edit_hide`` describe the message the caller read.
        Only a read that shows an edit which can have replaced the media
        replaces it (``_shows_replacing_edit``): a read with no edit date, a
        hidden edit, or one older than the archived edit (the listener
        applied a newer edit after the caller fetched the message) replaces
        nothing.

        Returns the row as the caller should use it, or None when nothing was
        replaced: the row is gone or already holds this file (another path
        replaced it first), the read shows no replacing edit, or the row
        could not be kept.
        """
        async with self.db_manager.async_session_factory() as session:
            message = await self._load_message_for_update(session, account_id, chat_id, message_id)
            if message is None:
                return None
            row = (
                await session.execute(
                    select(Media)
                    .where(and_(Media.account_id == account_id, Media.id == media_id))
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            stored = stored_media_file_id(row.telegram_file_id, row.file_name)
            if stored is None or stored == telegram_file_id:
                return None
            if not self._shows_replacing_edit(message, edit_date, edit_hide):
                logger.debug("Media replacement refused: the read shows no edit that replaced it")
                return None

            date = self._message_version_date(message)
            entities, rich_message = _formatting_state(message.raw_data)
            await self._record_message_version(
                session=session,
                account_id=account_id,
                chat_id=chat_id,
                message_id=message_id,
                text=message.text,
                date=date,
                entities=entities,
                rich_message=rich_message,
                source=source,
            )
            new_type = media_type or row.type
            new_id = await self._free_media_id(session, account_id, f"{chat_id}_{message_id}_{new_type}")
            now = utcnow_naive()
            try:
                async with session.begin_nested():
                    await session.execute(
                        insert(MediaVersion).values(
                            account_id=account_id,
                            chat_id=chat_id,
                            message_id=message_id,
                            media_id=row.id,
                            type=row.type,
                            telegram_file_id=stored,
                            file_path=row.file_path,
                            file_name=row.file_name,
                            file_size=row.file_size,
                            mime_type=row.mime_type,
                            width=row.width,
                            height=row.height,
                            duration=row.duration,
                            content_hash=row.content_hash,
                            downloaded=row.downloaded or 0,
                            download_date=row.download_date,
                            skip_reason=row.skip_reason,
                            first_seen=row.created_at,
                            date=date,
                            captured_at=now,
                            source=source,
                        )
                    )
                    await session.execute(
                        update(Media)
                        .where(and_(Media.account_id == account_id, Media.id == row.id))
                        .values(
                            id=new_id,
                            type=new_type,
                            telegram_file_id=telegram_file_id,
                            file_path=None,
                            file_name=None,
                            file_size=None,
                            mime_type=None,
                            width=None,
                            height=None,
                            duration=None,
                            content_hash=None,
                            downloaded=0,
                            download_attempts=0,
                            skip_reason=None,
                            download_date=None,
                            created_at=now,
                        )
                    )
            except IntegrityError:
                # A key already taken: nothing was changed. reconcile_media_row
                # hands the row back as superseded, so no caller downloads the
                # new media into it, and the next read of the message tries again.
                await session.rollback()
                logger.warning("Could not keep replaced media as a version; the row is unchanged")
                return None
            await session.commit()
            logger.debug("Kept replaced media as a version")
            return {
                "id": new_id,
                "type": new_type,
                "message_id": message_id,
                "chat_id": chat_id,
                "file_name": None,
                "file_path": None,
                "file_size": None,
                "mime_type": None,
                "width": None,
                "height": None,
                "duration": None,
                "content_hash": None,
                "downloaded": False,
                "download_date": None,
                "telegram_file_id": telegram_file_id,
                "replaced": True,
            }

    @staticmethod
    def _document_mime_condition(mime_types: set[str], mime_extensions: set[str]):
        """SQL mirror of ``Config.document_mime_allowed``, for document rows only.

        Never stricter than the Python predicate: a row this keeps but the
        predicate declines is re-checked against the live message by the drain
        and costs one re-fetch, while a row this drops is never downloaded at
        all. A bare ``type/subtype`` matches exactly; one carrying
        ``;parameters`` matches on the prefix with anything (whitespace
        included) allowed before the semicolon, so every shape the normalizer
        accepts is kept here and the predicate has the final word.
        """

        def like(value: str) -> str:
            return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

        # COALESCE to '' in the comparing arms: a row with a name but no MIME
        # (or the reverse) would otherwise turn every arm on the missing column
        # NULL, and under three-valued logic the whole OR could come out NULL,
        # so the row was neither drained nor marked filtered. '' matches no
        # configured MIME and no extension, exactly as the predicate's falsy
        # checks treat a missing value.
        mime = func.coalesce(Media.mime_type, "")
        name = func.coalesce(Media.file_name, "")
        allowed = [func.lower(mime).in_(sorted(mime_types))]
        allowed += [mime.ilike(f"{like(m)}%;%", escape="\\") for m in sorted(mime_types)]
        allowed += [name.ilike(f"%{like(ext)}", escape="\\") for ext in sorted(mime_extensions)]
        # A row that stored neither a MIME nor a name cannot be judged here.
        # Keep it: it is indistinguishable from a genuine failed download, and
        # the drain re-checks it against the message before spending anything.
        allowed.append(and_(Media.mime_type.is_(None), Media.file_name.is_(None)))
        return or_(Media.type != "document", *allowed)

    @retry_on_locked()
    async def reconcile_media_skip_reasons(
        self,
        max_media_size_bytes: int | None,
        *,
        account_id: int,
        media_types: set[str] | None = None,
        document_mime_types: set[str] | None = None,
        document_mime_extensions: set[str] | None = None,
        skip_media_chat_ids: set[int] | None = None,
    ) -> dict[str, int]:
        """Re-derive ``skip_reason`` for every not-downloaded row from the live settings (#465).

        Four UPDATEs, all account-scoped and all idempotent: mark rows over
        the size cap ``oversize`` and rows outside the whitelist ``filtered``,
        and clear either reason from rows the current settings would now
        fetch, so the viewer stops calling them skipped the run after the
        operator relaxes a cap or a filter. Rows written before migration 030
        get classified here on the first run. Returns the counts.

        ``skip_media_chat_ids`` carries SKIP_MEDIA_CHAT_IDS: the drain never
        fetches those chats, so their rows are ``filtered`` too, and the clear
        leaves them alone. The clears also reach ``downloaded = 1`` rows: a
        file on disk has no reason to be skipped, and the over-size writer can
        leave one on a file an earlier run with a higher cap already fetched.
        """
        not_metadata = Media.type.notin_(sorted(METADATA_ONLY_MEDIA_TYPES))
        # Marks touch only rows still waiting for a file; clears touch every
        # row, so a stale reason on a downloaded row does not outlive the run.
        scope = and_(Media.account_id == account_id, Media.downloaded == 0, not_metadata)
        clear_scope = and_(Media.account_id == account_id, not_metadata)
        allowed = []
        if media_types:
            allowed.append(Media.type.in_(sorted(media_types)))
        if document_mime_types:
            allowed.append(self._document_mime_condition(document_mime_types, document_mime_extensions or set()))
        # What still earns each reason; a row whose reason no longer holds is cleared.
        still_oversize = []
        if max_media_size_bytes is not None:
            still_oversize.append(Media.file_size > max_media_size_bytes)
        still_filtered = []
        if allowed:
            still_filtered.append(~and_(*allowed))
        if skip_media_chat_ids:
            still_filtered.append(Media.chat_id.in_(sorted(skip_media_chat_ids)))
        on_disk = Media.downloaded == 1
        counts = {"oversize": 0, "filtered": 0, "cleared": 0}
        # Both clears run before both marks: a row whose reason no longer holds
        # (filter removed, cap raised) is re-marked with the reason that now
        # applies in the same pass instead of reading "pending" for one interval.
        clears = []
        for reason, still in (("oversize", still_oversize), ("filtered", still_filtered)):
            gone = [or_(on_disk, ~or_(*still))] if still else []
            clears.append(update(Media).where(clear_scope, Media.skip_reason == reason, *gone))
        async with self.db_manager.async_session_factory() as session:
            for stmt in clears:
                result = await session.execute(stmt.values(skip_reason=None))
                counts["cleared"] += result.rowcount or 0
            if still_oversize:
                result = await session.execute(
                    update(Media)
                    .where(scope, Media.skip_reason.is_(None), *still_oversize)
                    .values(skip_reason="oversize")
                )
                counts["oversize"] = result.rowcount or 0
            if still_filtered:
                result = await session.execute(
                    update(Media)
                    .where(scope, Media.skip_reason.is_(None), or_(*still_filtered))
                    .values(skip_reason="filtered")
                )
                counts["filtered"] = result.rowcount or 0
            await session.commit()
        return counts

    async def get_pending_media_downloads(
        self,
        max_media_size_bytes: int | None = None,
        max_attempts: int | None = None,
        limit: int | None = 1000,
        exclude_chat_ids: set[int] | None = None,
        *,
        account_id: int,
        media_types: set[str] | None = None,
        document_mime_types: set[str] | None = None,
        document_mime_extensions: set[str] | None = None,
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

        ``media_types``/``document_mime_types``/``document_mime_extensions``
        carry DOWNLOAD_MEDIA_TYPES and DOWNLOAD_DOCUMENT_MIME_TYPES, and are
        applied HERE rather than in Python afterwards for the reason #442 moved
        ``exclude_chat_ids`` here: a filtered row is never charged a download
        attempt, so it sits at ``download_attempts = 0`` forever and sorts ahead
        of every genuine failure. On an archive whose filter declines most
        media, filtering after the ``limit`` means the same rows fill all of it
        on every run and no real retry ever gets a slot.
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
            if exclude_chat_ids:
                conditions.append(~Media.chat_id.in_(exclude_chat_ids))
            if media_types:
                conditions.append(Media.type.in_(sorted(media_types)))
            if document_mime_types:
                conditions.append(self._document_mime_condition(document_mime_types, document_mime_extensions or set()))
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

    async def mark_media_for_redownload(self, media_id: str, *, account_id: int, keep_path: bool = False) -> None:
        """Mark a media record as needing re-download.

        Also resets download_attempts so a row that previously hit the retry
        cap (#212) becomes eligible for the pending-download retry again.

        ``keep_path`` keeps ``file_path``: the row still names the link whose
        ``_shared`` file is gone, and the download puts the bytes back under
        that link's own target (``_process_media``), so the link and every
        other link to the same target resolve again even when the current
        Telegram file name differs from the one the link holds.
        """
        values: dict[str, Any] = {"downloaded": 0, "download_date": None, "download_attempts": 0}
        if not keep_path:
            values["file_path"] = None
        async with self.db_manager.async_session_factory() as session:
            stmt = update(Media).where(and_(Media.account_id == account_id, Media.id == media_id)).values(**values)
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
                    # Declined by configuration, not exhausted: raising the retry
                    # cap would not fetch these, so the warning must not count them.
                    Media.skip_reason.is_(None),
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
        source: str | None = None,
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
          the FK having no CASCADE, raise on PostgreSQL);
        - adds a ``reaction_history`` row for every emoji whose count differs
          from the newest row kept for it (0 when it went), tagged with
          ``source``, so a count that drops without reaching zero and an emoji
          that comes back both keep their earlier state. An emoji with a
          ``reactions`` row and no history first gets the baseline that row
          stands for (``_reaction_baseline``), the same one migration 037 seeds.

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
            # What each emoji's rows held before this reconcile changes them, for
            # the baseline of an emoji that has no history yet.
            baseline_rows = {
                emoji: [(r.count, r.created_at, r.removed_at) for r in rows] for emoji, rows in by_emoji.items()
            }
            newest_kept = await self._newest_reaction_history(session, account_id, chat_id, message_id)

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

            # The history: one row per emoji whose count moved.
            history_written = False
            for emoji in sorted(set(by_emoji) | set(desired)):
                new_count = desired.get(emoji, 0)
                kept = newest_kept.get(emoji)
                if kept is None and emoji in by_emoji:
                    for row in self._reaction_baseline(
                        baseline_rows[emoji],
                        account_id=account_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        emoji=emoji,
                        now=now,
                    ):
                        session.add(row)
                        kept = row.count
                    history_written = True
                if kept != new_count:
                    session.add(
                        ReactionHistory(
                            account_id=account_id,
                            chat_id=chat_id,
                            message_id=message_id,
                            emoji=emoji,
                            count=new_count,
                            previous_count=kept,
                            observed_at=now,
                            source=source,
                        )
                    )
                    history_written = True

            if not changed and not history_written:
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
                        source=source,
                        _after_seq_reset=True,
                    )
                raise
            return "reconciled" if changed else "noop"

    @staticmethod
    async def _newest_reaction_history(session, account_id: int, chat_id: int, message_id: int) -> dict[str, int]:
        """The count of the newest ``reaction_history`` row per emoji of one message."""
        ranked = (
            select(
                ReactionHistory.emoji,
                ReactionHistory.count,
                func.row_number()
                .over(
                    partition_by=ReactionHistory.emoji,
                    order_by=(ReactionHistory.observed_at.desc(), ReactionHistory.id.desc()),
                )
                .label("rank"),
            )
            .where(
                ReactionHistory.account_id == account_id,
                ReactionHistory.chat_id == chat_id,
                ReactionHistory.message_id == message_id,
            )
            .subquery()
        )
        result = await session.execute(select(ranked.c.emoji, ranked.c.count).where(ranked.c.rank == 1))
        return {row.emoji: row.count for row in result}

    @staticmethod
    def _reaction_baseline(
        rows: list[tuple[int | None, datetime | None, datetime | None]],
        *,
        account_id: int,
        chat_id: int,
        message_id: int,
        emoji: str,
        now: datetime,
    ) -> list[ReactionHistory]:
        """The history an emoji's ``reactions`` rows stand for, before it had any.

        ``rows`` are (count, created_at, removed_at) as stored before this
        reconcile. One row with the last count they held: the live rows' sum
        when any is live, else every row's (the count it had when it went), a
        tombstone without a positive count read as one, as the page read shows
        it. It is dated when the archive first saw the
        emoji. An emoji taken back gets a second row, count 0, dated by its
        latest tombstone. Migration 037 seeds exactly this, in SQL.
        """
        live = [count or 0 for count, _created, removed in rows if removed is None]
        if live:
            total = sum(live)
        else:
            total = sum(count if count and count > 0 else 1 for count, _created, _removed in rows)
        total = total if total > 0 else 1
        first_seen = min((created or removed or now) for _count, created, removed in rows)
        key = {"account_id": account_id, "chat_id": chat_id, "message_id": message_id, "emoji": emoji}
        baseline = [ReactionHistory(**key, count=total, previous_count=None, observed_at=first_seen, source="baseline")]
        if not live:
            baseline.append(
                ReactionHistory(
                    **key,
                    count=0,
                    previous_count=total,
                    observed_at=max(removed for _count, _created, removed in rows if removed is not None),
                    source="baseline",
                )
            )
        return baseline

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

            # Per-chat statistics, keyed by the account too. Grouped by
            # chat_id alone this summed both accounts' copies into one entry,
            # so a viewer entitled to one account read the other's numbers
            # through it — and for a one-to-one chat, whose id is the other
            # party's user id, it summed two unrelated conversations.
            chat_stats_query = select(
                Message.account_id, Message.chat_id, func.count(Message.id).label("message_count")
            ).group_by(Message.account_id, Message.chat_id)
            chat_stats_result = await session.execute(chat_stats_query)
            per_chat_stats = {
                account_chat_stats_key(row.account_id, row.chat_id): row.message_count for row in chat_stats_result
            }

            # Downloaded media per chat, grouped the same way. Bytes are the DB
            # file sizes (logical), not on-disk usage: a deduplicated blob counts
            # once per row that references it, since per-chat du is not defined.
            # The archive-wide media count and the DB size snapshot (the storage
            # fallback when on-disk usage is unavailable) are summed from these
            # same rows, NULL-chat rows included, so media is scanned once.
            media_stats_query = (
                select(
                    Media.account_id,
                    Media.chat_id,
                    func.count(Media.id).label("media_count"),
                    func.coalesce(func.sum(Media.file_size), 0).label("media_bytes"),
                )
                .where(Media.downloaded == 1)
                .group_by(Media.account_id, Media.chat_id)
            )
            media_stats_result = await session.execute(media_stats_query)
            per_chat_media_counts = {}
            per_chat_media_bytes = {}
            media_count = 0
            db_total_size = 0
            for row in media_stats_result:
                media_count += int(row.media_count)
                db_total_size += int(row.media_bytes)
                if row.chat_id is None:
                    continue
                key = account_chat_stats_key(row.account_id, row.chat_id)
                per_chat_media_counts[key] = int(row.media_count)
                per_chat_media_bytes[key] = int(row.media_bytes)

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
            # Keyed "<account>:<chat>" so the map survives a JSON round trip
            # with its account intact; see account_chat_stats_key.
            PER_ACCOUNT_CHAT_COUNTS_KEY: {str(k): int(v) for k, v in per_chat_stats.items()},
            PER_ACCOUNT_CHAT_MEDIA_COUNTS_KEY: per_chat_media_counts,
            PER_ACCOUNT_CHAT_MEDIA_BYTES_KEY: per_chat_media_bytes,
        }

        logger.info(f"Statistics calculated: {chat_count} chats, {msg_count} messages, {media_count} media files")

        # Store in metadata
        await self.set_metadata("cached_stats", json.dumps(stats))
        await self.set_metadata("stats_calculated_at", utcnow_naive().isoformat())

        return stats

    # ========== Delete Operations ==========

    async def delete_chat_and_related_data(self, chat_id: int, media_base_path: str = None, *, account_id: int) -> None:
        """Delete one account's copy of a chat and all related data.

        The on-disk media folder is chat-scoped, not account-scoped: every
        account's copy of the chat keeps its files in ``<base>/<chat_id>``. The
        folder is removed only when no media row of any account still uses it,
        counted after this account's rows are gone. Otherwise it stays whole,
        because removing it would leave the other account's rows marked
        downloaded with nothing behind them. The folder holds links into
        ``_shared`` and those are removed with it, never the shared files.
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
            # Delete the earlier media an edit replaced, with their transcripts
            await self._delete_media_versions_of(
                session,
                and_(MediaVersion.account_id == account_id, MediaVersion.chat_id == chat_id),
                account_id=account_id,
            )
            # Delete the transcripts of the chat's media, then the media records
            chat_media = and_(Media.account_id == account_id, Media.chat_id == chat_id)
            await session.execute(self._delete_transcripts_of(chat_media, account_id=account_id))
            await session.execute(delete(Media).where(chat_media))
            # Delete reactions and their history
            await session.execute(
                delete(ReactionHistory).where(
                    and_(ReactionHistory.account_id == account_id, ReactionHistory.chat_id == chat_id)
                )
            )
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
            still_used = 0
            if os.path.exists(chat_media_dir):
                folder_prefixes = {
                    os.path.join(media_base_path, str(chat_id)) + os.sep,
                    os.path.join(os.path.abspath(media_base_path), str(chat_id)) + os.sep,
                    f"{chat_id}/",
                }
                try:
                    still_used = await self.count_media_rows_in_folder(chat_id, folder_prefixes)
                except Exception as e:
                    # Unknown means in use: a folder is never removed on doubt.
                    logger.error(f"Could not count the rows still using the chat's media folder: {type(e).__name__}")
                    still_used = 1
                if still_used:
                    logger.info(f"Kept the chat's media folder: {still_used} media row(s) of another account use it")
            if os.path.exists(chat_media_dir) and not still_used:
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
                Message.raw_data,
            )
            .outerjoin(User, Message.sender_id == User.id)
            .where(and_(Message.chat_id == chat_id, Message.id.in_(reply_ids_needed)))
        )
        if account_id is not None:
            reply_stmt = reply_stmt.where(Message.account_id == account_id)
        reply_result = await session.execute(reply_stmt)
        reply_rows: dict[int, dict[str, Any]] = {}
        for row in reply_result:
            payload_kind, venue_title = _reply_card_kind(row.raw_data)
            reply_rows[row.id] = {
                "text": row.text,
                "sender_name": resolve_sender_display_name(
                    row.sender_name, row.first_name, row.last_name, row.username
                ),
                # The listener writes no media row for a location or a contact,
                # so its payload names the kind when there is no row.
                "media_type": row.reply_media_type or payload_kind,
                "venue_title": venue_title,
            }

        for msg in messages:
            if not msg.get("reply_to_msg_id"):
                continue
            reply_row = reply_rows.get(msg["reply_to_msg_id"])
            msg["reply_to_sender_name"] = reply_row["sender_name"] if reply_row else None
            msg["reply_to_media_type"] = reply_row["media_type"] if reply_row else None
            if reply_row and reply_row["venue_title"]:
                # "Location, Demo Cafe" in the quote, as the Telegram apps write it.
                msg["reply_to_media_title"] = reply_row["venue_title"]
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
        fold_shared: bool = False,
    ) -> dict[str, Any]:
        """Messages carrying ``tag`` (#hashtag / $CASHTAG) as a whole token, newest first.

        The tag view's data source: the SQL side prefilters with ILIKE — served
        by ``idx_messages_text_trgm`` on PostgreSQL — and a word-boundary
        post-filter drops substring hits ('#tag' inside '#taglonger').
        Entitlements arrive as ``scope`` and apply in the WHERE clause exactly
        like the chat list, so a restricted viewer's tag search can only ever
        touch entitled chats. ``chat_id``+``account_id`` narrow to one chat
        (the This Chat tab); ``outgoing_only`` is My Messages (the archive
        owner's side of every conversation). ``fold_shared`` applies the chat
        list's folding rule to the cross-chat tabs only — see the call site
        below. Offset paging re-scans from the
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
                        # Derives sender_account_id below; never returned.
                        Message.sender_id,
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
                # Cross-chat tabs fold shared chats like the chat list does, so
                # a channel two accounts archive is not listed twice. The This
                # Chat tab must NOT: it is already narrowed to one copy, and
                # folding there would empty the tab whenever the reader opened
                # the copy the fold hides.
                if fold_shared and chat_id is None:
                    stmt = stmt.where(scope.displayed_copy_predicate())
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
                            "sender_id": row["sender_id"],
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
        page = matched[offset : offset + limit]
        # Same two steps as the global search: derive the account, drop the id.
        await self.attach_sender_accounts(page)
        for result in page:
            result.pop("sender_id", None)
        return {
            "results": page,
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
        # Read to derive sender_account_id below and then dropped: the id of
        # whoever sent a hit is not part of this endpoint's contract.
        Message.sender_id,
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
        Chat.avatar_photo_id.label("chat_avatar_photo_id"),
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
        fold_shared: bool = False,
        with_transcripts: bool = True,
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

        ``fold_shared`` applies the chat list's folding rule, so a channel two
        accounts both archive answers once, through the copy the chat list
        shows — which is also the only ``chat_ref`` whose row the sidebar has,
        so a hit always opens a chat the reader can see. Private chats are
        never folded, there or here.

        ``dense_hits`` and ``walk_timeout_ms`` exist for tests that want to
        drive each PostgreSQL path with a handful of rows.

        Voice transcripts are searched too (docs/TRANSCRIPTION.md): the hit
        set is the UNION of two indexed key sets, message keys from the
        messages index and message keys reached from transcript hits through
        ``media`` on ``(account_id, media_id)``. Never an OR or an EXISTS in
        the message predicate, which would turn every page into a scan. Each
        row's ``matched_in`` is ``transcript`` when only the transcript side
        produced its key, else ``message``. Without the transcript search
        objects (SQLite without FTS5, or a database not yet at 032), or with
        ``with_transcripts`` False (a no-download login), the transcript side
        is absent.

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
            sides = {
                "fold_shared": fold_shared,
                "transcript_predicate": (
                    await self._transcript_search_predicate(session, search) if with_transcripts else None
                ),
            }

            if (
                self._is_sqlite
                or await self._global_search_hit_count(session, predicate, scope, dense_hits, **sides) < dense_hits
            ):
                rows = await self._global_search_sorted_hits(session, predicate, scope, limit, offset, **sides)
            else:
                try:
                    rows = await self._global_search_walk(
                        session, predicate, scope, limit, offset, timeout_ms=walk_timeout_ms, **sides
                    )
                except DBAPIError as exc:
                    if not _is_statement_timeout(exc):
                        raise
                    await session.rollback()
                    rows = await self._global_search_sorted_hits(session, predicate, scope, limit, offset, **sides)

        results = [
            {
                "id": row["id"],
                "date": row["date"],
                "text": row["text"],
                "sender_name": row["sender_name"],
                "sender_id": row["sender_id"],
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
                "chat_avatar_photo_id": row["chat_avatar_photo_id"],
                "topic_title": row["topic_title"],
                "matched_in": "transcript" if row["via_transcript"] else "message",
            }
            for row in rows[:limit]
        ]
        # Derive the archived-account label source, then drop the sender id it
        # was derived from — it entered the SELECT for this and nothing else.
        await self.attach_sender_accounts(results)
        for result in results:
            result.pop("sender_id", None)
        return {"results": results, "has_more": len(rows) > limit, "indexed": True}

    @staticmethod
    def _global_search_scoped(stmt, scope: ChatScope, *, fold_shared: bool = False):
        """Restrict a hit-set SELECT over ``messages`` to the entitled chats.

        Applied INSIDE the hit set so a viewer entitled to one chat never pays
        for the whole archive's hits; skipped entirely for an unrestricted
        scope with nothing to fold, where the join would only add work.

        ``fold_shared`` adds the chat list's folding rule, so a channel two
        accounts both archive answers a cross-chat search once instead of
        twice. It has to be here rather than a post-filter on the page: the
        duplicate is a real row in the hit set, so dropping it afterwards would
        leave ``has_more`` and the offsets describing a set the caller never
        sees. The master is exactly the principal with duplicates to fold, and
        a master's scope is unrestricted — so folding forces the join the
        shortcut above would otherwise skip.
        """
        if scope.unrestricted and not fold_shared:
            return stmt
        stmt = stmt.join(Chat, and_(Chat.account_id == Message.account_id, Chat.id == Message.chat_id))
        for predicate in scope.sql_predicates():
            stmt = stmt.where(predicate)
        if fold_shared:
            stmt = stmt.where(scope.displayed_copy_predicate())
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

    @staticmethod
    def _transcribed_media_owners():
        """Every media id with the message it belongs to: current media and earlier media.

        An edit that replaced a voice message or a video keeps the old media
        in ``media_versions`` under the id its transcripts point at, so a
        transcript reaches its message through either table.
        """
        return union_all(
            select(
                Media.account_id.label("account_id"),
                Media.id.label("media_id"),
                Media.chat_id.label("chat_id"),
                Media.message_id.label("message_id"),
            ),
            select(
                MediaVersion.account_id.label("account_id"),
                MediaVersion.media_id.label("media_id"),
                MediaVersion.chat_id.label("chat_id"),
                MediaVersion.message_id.label("message_id"),
            ),
        ).subquery("transcribed_media")

    @classmethod
    def _transcript_hit_messages(cls, stmt):
        """Put ``media_transcripts`` in FROM and join it through its media to the message that carries it.

        The media is the current one or an earlier one an edit replaced
        (``_transcribed_media_owners``).
        """
        owners = cls._transcribed_media_owners()
        return (
            stmt.select_from(MediaTranscript)
            .join(
                owners,
                and_(owners.c.account_id == MediaTranscript.account_id, owners.c.media_id == MediaTranscript.media_id),
            )
            .join(
                Message,
                and_(
                    Message.account_id == owners.c.account_id,
                    Message.chat_id == owners.c.chat_id,
                    Message.id == owners.c.message_id,
                ),
            )
        )

    def _global_search_side_keys(self, predicate, scope: ChatScope, *, fold_shared: bool = False, via_transcript=False):
        """One side of the hit set: message keys, their date and which side found them.

        The message side reads ``messages`` through its own index. The
        transcript side reads ``media_transcripts`` through its index and
        reaches the message through ``media``. Both are scoped the same way.
        The transcript side is DISTINCT: a media transcribed twice, or a
        message with two transcribed media, is one key, so a side cut to
        ``offset + limit + 1`` rows by the walk holds that many messages.
        """
        stmt = select(
            Message.account_id,
            Message.chat_id,
            Message.id,
            Message.date,
            literal_column("1" if via_transcript else "0").label("via_transcript"),
        )
        if via_transcript:
            stmt = self._transcript_hit_messages(stmt).distinct()
        else:
            stmt = stmt.select_from(Message)
        return self._global_search_scoped(stmt.where(predicate), scope, fold_shared=fold_shared)

    def _global_search_sides(self, predicate, transcript_predicate, scope: ChatScope, *, fold_shared: bool):
        sides = [self._global_search_side_keys(predicate, scope, fold_shared=fold_shared)]
        if transcript_predicate is not None:
            sides.append(
                self._global_search_side_keys(transcript_predicate, scope, fold_shared=fold_shared, via_transcript=True)
            )
        return sides

    async def _global_search_hit_count(
        self, session, predicate, scope: ChatScope, cap: int, *, fold_shared: bool = False, transcript_predicate=None
    ) -> int:
        """How many distinct messages match, counted through the indexes and stopped at ``cap``."""
        sides = [
            side.limit(cap).subquery(f"search_side_{index}")
            for index, side in enumerate(
                self._global_search_sides(predicate, transcript_predicate, scope, fold_shared=fold_shared)
            )
        ]
        if len(sides) == 1:
            capped = select(literal(1)).select_from(sides[0]).subquery("search_hits")
        else:
            keys = union(*(select(side.c.account_id, side.c.chat_id, side.c.id) for side in sides)).subquery("keys")
            capped = select(literal(1)).select_from(keys).limit(cap).subquery("search_hits")
        return int((await session.execute(select(func.count()).select_from(capped))).scalar_one())

    def _global_search_page_rows(self, sides, limit: int, offset: int, *, materialize: bool):
        """One page of the hit set, keyed and sorted before the joins, then its columns.

        Two sides are a UNION ALL grouped by key: a message found by both
        reports the message side (``via_transcript`` 0), and each key is one
        row, so offsets and ``has_more`` count messages, not matches.
        """
        if len(sides) == 1:
            hits = sides[0].cte("search_hits")
        else:
            hits = union_all(*(select(*side.subquery().c) for side in sides)).cte("search_hits")
        if materialize:
            hits = hits.prefix_with("MATERIALIZED")
        via = hits.c.via_transcript
        page = select(hits.c.account_id, hits.c.chat_id, hits.c.id, hits.c.date)
        if len(sides) == 1:
            page = page.add_columns(via.label("via_transcript"))
        else:
            page = page.add_columns(func.min(via).label("via_transcript")).group_by(
                hits.c.account_id, hits.c.chat_id, hits.c.id, hits.c.date
            )
        page = (
            page.order_by(hits.c.date.desc(), hits.c.account_id.desc(), hits.c.chat_id.desc(), hits.c.id.desc())
            .limit(limit + 1)
            .offset(offset)
            .subquery("search_page")
        )
        return self._global_search_joins(
            select(*self._GLOBAL_SEARCH_COLUMNS, page.c.via_transcript)
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

    async def _global_search_walk(
        self,
        session,
        predicate,
        scope: ChatScope,
        limit: int,
        offset: int,
        *,
        timeout_ms: int | None = None,
        fold_shared: bool = False,
        transcript_predicate=None,
    ):
        """Newest-first walk that filters as it goes — the shape for dense terms.

        Each side walks its own newest ``offset + limit + 1`` keys; the page
        of the union is always inside the union of those tops, so paging by
        key stays exact.
        """
        depth = offset + limit + 1
        order = [column.desc() for column in self._GLOBAL_SEARCH_ORDER]
        sides = [
            side.order_by(*order).limit(depth)
            for side in self._global_search_sides(predicate, transcript_predicate, scope, fold_shared=fold_shared)
        ]
        stmt = self._global_search_page_rows(sides, limit, offset, materialize=False)
        if timeout_ms is not None:
            # SET LOCAL: scoped to this transaction, so the pooled connection
            # never carries it into another request.
            await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
        return (await session.execute(stmt)).mappings().all()

    async def _global_search_sorted_hits(
        self,
        session,
        predicate,
        scope: ChatScope,
        limit: int,
        offset: int,
        *,
        fold_shared: bool = False,
        transcript_predicate=None,
    ):
        """Materialise the hit keys through the index, sort them, then fetch one page.

        The keys are MATERIALIZED so the planner cannot flatten the CTE back
        into the date walk it prefers, and the page is cut BEFORE the joins so
        a dense term never joins every hit to fetch twenty rows.
        """
        sides = self._global_search_sides(predicate, transcript_predicate, scope, fold_shared=fold_shared)
        stmt = self._global_search_page_rows(sides, limit, offset, materialize=True)
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

    async def _transcript_fts_ready(self, session) -> bool:
        """Whether migration 032's transcript search objects exist in THIS database.

        Same contract as ``_fts_ready``: probed once per adapter. A database
        migrated to 031 and not yet to 032 keeps searching messages only.
        """
        if self._transcript_fts_ready_cache is None:
            if self._is_sqlite:
                row = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t").bindparams(
                        t=SQLITE_TRANSCRIPT_FTS_TABLE
                    )
                )
            else:
                row = await session.execute(
                    text(
                        "SELECT 1 FROM pg_attribute "
                        "WHERE attrelid = to_regclass('media_transcripts') "
                        "AND attname = :c AND NOT attisdropped"
                    ).bindparams(c=PG_TSVECTOR_COLUMN)
                )
            self._transcript_fts_ready_cache = row.first() is not None
        return self._transcript_fts_ready_cache

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

    async def _transcript_search_predicate(self, session, search: str):
        """The transcript side of a search: an indexed predicate on ``media_transcripts``, or None.

        None when this database lacks migration 032's search objects (SQLite
        built without FTS5, or not yet migrated) or the search has no word.
        Kept apart from ``_text_search_predicate`` on purpose: callers union
        the two sides as key sets and never OR them into one predicate.
        """
        if not await self._transcript_fts_ready(session):
            return None
        if self._is_sqlite:
            match = fts_match_query(search)
            if match is None:
                return None
            return text(
                "media_transcripts.id IN "
                "(SELECT rowid FROM media_transcripts_fts WHERE media_transcripts_fts MATCH :transcript_match)"
            ).bindparams(transcript_match=match)
        if not search_has_words(search):
            return None
        return text(f"media_transcripts.text_search @@ {PG_TRANSCRIPT_TSQUERY_FROM_SEARCH}").bindparams(
            transcript_search=search
        )

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
        with_transcripts: bool = True,
        deleted_only: bool = False,
        edited_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Get messages with user info and media info for web viewer.

        ``with_transcripts`` False keeps a search on message text only, for a
        no-download login that may not learn what a voice message says.

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
            deleted_only: Keep only the rows deleted in Telegram that the
                archive kept (``is_deleted=1``). Combines with every other
                filter and cursor; a read-only narrowing of the same query.
            edited_only: Keep only the edited rows (``_edited_predicate``):
                ``edit_date`` set, or an earlier version kept. Combines like
                ``deleted_only``.

        Returns:
            List of message dictionaries with user and media info. A row that is a
            reply also carries ``reply_to_sender_name`` and ``reply_to_media_type``
            (both nullable) so the viewer can render "Reply to <name>" (#268).
            Every row carries ``reactions`` (live, per emoji) and
            ``removed_reactions``: the emojis taken back, each with the count it
            had and ``removed_at``, when the archive noticed, newest first.
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

            # The viewer's "Deleted only" list: the kept deletions of this chat.
            if deleted_only:
                stmt = stmt.where(Message.is_deleted == 1)

            # The viewer's "Edited only" list: what the bubble marks as edited.
            if edited_only:
                stmt = stmt.where(self._edited_predicate())

            # Chat search, like global search, is the UNION of two indexed key
            # sets: the message index and the transcript index reached through
            # media (docs/TRANSCRIPTION.md). ``transcript_predicate`` stays None
            # on the ILIKE fallback and without the transcript objects.
            fts_predicate = transcript_predicate = None
            if search:
                fts_predicate = await self._text_search_predicate(session, search)
                if fts_predicate is not None and with_transcripts:
                    transcript_predicate = await self._transcript_search_predicate(session, search)
                if transcript_predicate is not None:
                    message_keys = select(Message.account_id, Message.id).where(
                        Message.chat_id == chat_id, fts_predicate
                    )
                    transcript_keys = self._transcript_hit_messages(select(Message.account_id, Message.id)).where(
                        Message.chat_id == chat_id, transcript_predicate
                    )
                    if account_id is not None:
                        message_keys = message_keys.where(Message.account_id == account_id)
                        transcript_keys = transcript_keys.where(Message.account_id == account_id)
                    hit_keys = union(message_keys, transcript_keys).subquery("chat_search_hits")
                    stmt = stmt.where(
                        tuple_(Message.account_id, Message.id).in_(select(hit_keys.c.account_id, hit_keys.c.id))
                    )
                elif fts_predicate is not None:
                    stmt = stmt.where(fts_predicate)
                else:
                    escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                    stmt = stmt.where(Message.text.ilike(f"%{escaped}%", escape="\\"))

            # Cursor-based pagination (preferred - O(1) performance)
            # Mirrored by the viewer (telegram_archive/web/templates/index.html: compareMessagesDesc/messageCursor) — keep in sync.
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

            if search:
                # matched_in: "transcript" when only the transcript side found
                # the row. One indexed read over the page's own ids.
                by_message = set(zip(row_accounts, page_message_ids, strict=True))
                if transcript_predicate is not None and page_message_ids:
                    matched_stmt = select(Message.account_id, Message.id).where(
                        Message.chat_id == chat_id, Message.id.in_(page_message_ids), fts_predicate
                    )
                    if account_id is not None:
                        matched_stmt = matched_stmt.where(Message.account_id == account_id)
                    by_message = {(row.account_id, row.id) for row in await session.execute(matched_stmt)}
                for account, msg in zip(row_accounts, messages, strict=True):
                    msg["matched_in"] = "message" if (account, msg["id"]) in by_message else "transcript"

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
                        "skip_reason": media_row.skip_reason,
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
            # The same read returns the reactions taken back (removed_at set,
            # #219): they stay out of the live count and come back beside it as
            # removed_reactions, so the viewer can show what the archive kept.
            reactions_by_message: dict[int, list[dict[str, Any]]] = {mid: [] for mid in page_message_ids}
            removed_by_message: dict[int, dict[str, dict[str, Any]]] = {mid: {} for mid in page_message_ids}
            if page_message_ids:
                reactions_stmt = (
                    select(Reaction)
                    .where(
                        and_(
                            Reaction.chat_id == chat_id,
                            Reaction.message_id.in_(page_message_ids),
                        )
                    )
                    .order_by(Reaction.message_id, Reaction.emoji, Reaction.id)
                )
                if account_id is not None:
                    reactions_stmt = reactions_stmt.where(Reaction.account_id == account_id)
                reactions_result = await session.execute(reactions_stmt)
                for r in reactions_result.scalars():
                    if r.removed_at is not None:
                        # One entry per emoji: the count it had when it went, and
                        # the latest time the archive noticed it gone.
                        removed = removed_by_message[r.message_id].get(r.emoji)
                        if removed is None:
                            removed_by_message[r.message_id][r.emoji] = {
                                "emoji": r.emoji,
                                "count": r.count or 1,
                                "removed_at": r.removed_at,
                            }
                        else:
                            removed["count"] += r.count or 1
                            removed["removed_at"] = max(removed["removed_at"], r.removed_at)
                        continue
                    reactions_by_message[r.message_id].append(
                        {"emoji": r.emoji, "user_id": r.user_id, "count": r.count}
                    )

            # Every kept state of the page's reactions (reaction_history), oldest
            # first, from the same chat, messages and account as the rows above.
            history_by_message: dict[int, list[dict[str, Any]]] = {mid: [] for mid in page_message_ids}
            if page_message_ids:
                history_stmt = (
                    select(ReactionHistory)
                    .where(
                        ReactionHistory.chat_id == chat_id,
                        ReactionHistory.message_id.in_(page_message_ids),
                    )
                    .order_by(ReactionHistory.message_id, ReactionHistory.observed_at, ReactionHistory.id)
                )
                if account_id is not None:
                    history_stmt = history_stmt.where(ReactionHistory.account_id == account_id)
                for h in (await session.execute(history_stmt)).scalars():
                    history_by_message[h.message_id].append(self._reaction_history_to_dict(h))

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
                history = history_by_message.get(msg["id"], [])
                msg["reaction_history"] = history
                msg["removed_reactions"] = self._removed_reactions(
                    history, removed_by_message.get(msg["id"], {}), set(reactions_by_emoji)
                )

            await self.attach_sender_accounts(messages)
            return messages

    @staticmethod
    def _reaction_history_to_dict(row) -> dict[str, Any]:
        """One kept state of an emoji on a message, as the page read and both exports list it."""
        return {
            "emoji": row.emoji,
            "count": row.count,
            "previous_count": row.previous_count,
            "observed_at": row.observed_at,
            "source": row.source,
        }

    @staticmethod
    def _removed_reactions(
        history: list[dict[str, Any]], tombstones: dict[str, dict[str, Any]], live: set[str]
    ) -> list[dict[str, Any]]:
        """The reactions taken back the viewer lists, newest first, one entry per emoji.

        From ``history`` (one message's ``reaction_history``, oldest first),
        the latest drop of each emoji: a state whose count is below the one
        before it. ``count`` is how many went, ``count_before`` the count
        before the drop, so a partial drop reads "2 of 7", and ``removed_at``
        when the archive saw it. A drop to zero followed by a state with a
        count has ``back_at``, when the archive saw the emoji again.

        An emoji with no history (a row written before 037 and not touched
        since, if the baseline was ever skipped) falls back to its tombstone:
        listed while it is not live, with the count it had when it went.
        """
        by_emoji: dict[str, list[dict[str, Any]]] = {}
        for state in history:
            by_emoji.setdefault(state["emoji"], []).append(state)
        entries: list[dict[str, Any]] = []
        for emoji, states in by_emoji.items():
            drop_index = None
            for index, state in enumerate(states):
                before = state["previous_count"]
                if before is not None and state["count"] < before:
                    drop_index = index
            if drop_index is None:
                continue
            drop = states[drop_index]
            back_at = None
            if drop["count"] == 0:
                back_at = next((later["observed_at"] for later in states[drop_index + 1 :] if later["count"] > 0), None)
            entries.append(
                {
                    "emoji": emoji,
                    "count": drop["previous_count"] - drop["count"],
                    "count_before": drop["previous_count"],
                    "removed_at": drop["observed_at"],
                    "back_at": back_at,
                }
            )
        for emoji, removed in tombstones.items():
            if emoji in by_emoji or emoji in live:
                continue
            entries.append({**removed, "count_before": removed["count"], "back_at": None})
        return sorted(entries, key=lambda removed: (removed["removed_at"], removed["emoji"]), reverse=True)

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
                    Media.skip_reason.label("media_skip_reason"),
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
                    "skip_reason": row.media_skip_reason,
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

            await self.attach_sender_accounts([msg])
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
            "avatar_photo_id": chat.avatar_photo_id,
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
                    Media.skip_reason.label("media_skip_reason"),
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
                        "skip_reason": row.media_skip_reason,
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

            await self.attach_sender_accounts(messages)
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
        """Stream a chat's messages for the viewer's export (async generator).

        None account_id = unscoped until phase 4. ``from_date`` is a naive-UTC
        inclusive lower bound on Message.date, ``to_date`` an EXCLUSIVE upper
        bound.

        Yields message dictionaries with sender info, deleted messages
        included and marked by ``is_deleted``/``deleted_at``. Each carries
        ``edit_date``, ``media`` (its current media, ``_export_media_dict``,
        never a file path) and ``versions``, every earlier text and media the
        archive kept of it (``_export_versions``, any date, oldest first). A
        message whose media has transcripts carries them all under
        ``transcripts``, newest first. Everything is read from one snapshot,
        so every transcript names a media listed in the same file. A
        location, a contact, a poll or another metadata-only kind is under
        ``media_payload``, keyed as in ``raw_data``.

        ``include_media`` is for ``scripts/restore_chat.py``, which uploads
        the files again: each message also gets ``media_type`` and
        ``media_path``, the stored path of its first media row, or None. The
        viewer's export never passes it, so no file path leaves the archive.
        """
        conditions = [Message.chat_id == chat_id]
        if account_id is not None:
            conditions.append(Message.account_id == account_id)
        if from_date is not None:
            conditions.append(Message.date >= from_date)
        if to_date is not None:
            conditions.append(Message.date < to_date)
        stmt = (
            select(
                Message.id,
                Message.account_id,
                Message.chat_id,
                Message.date,
                Message.text,
                Message.is_outgoing,
                Message.reply_to_msg_id,
                Message.sender_name,
                Message.edit_date,
                Message.is_deleted,
                Message.deleted_at,
                Message.raw_data,
                User.first_name,
                User.last_name,
                User.username,
            )
            .outerjoin(User, Message.sender_id == User.id)
            .where(*conditions)
            .order_by(*EXPORT_MESSAGE_ORDER)
        )
        async with self.db_manager.async_session_factory() as session:
            await self._read_one_snapshot(session)
            transcripts: dict[tuple[int, int], list[dict[str, Any]]] = {}
            for row in await self._read_export_transcripts(
                session, chat_id, account_id=account_id, from_date=from_date, to_date=to_date
            ):
                transcripts.setdefault((row["account_id"], row["message_id"]), []).append(row)
            result = await session.stream(stmt)
            async for row, media, versions, reaction_history in self._export_message_parts(
                session, result, conditions, iso=True
            ):
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
                    # What the viewer shows beside the text: deleted in
                    # Telegram and kept here, and when Telegram last marked
                    # it edited.
                    "is_deleted": bool(row.is_deleted),
                    "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
                    "edit_date": row.edit_date.isoformat() if row.edit_date else None,
                    "media": [self._export_media_dict(media_row) for media_row in media],
                }
                if include_media:
                    msg["media_type"] = media[0].type if media else None
                    msg["media_path"] = media[0].file_path if media else None
                # A location, a contact, a poll and the other metadata-only
                # kinds are the message's content: the card the viewer draws.
                media_payload = _media_payloads_of(row.raw_data)
                if media_payload:
                    msg["media_payload"] = media_payload
                if (row.account_id, row.id) in transcripts:
                    msg["transcripts"] = transcripts[(row.account_id, row.id)]
                msg["versions"] = versions
                msg["reaction_history"] = reaction_history
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
        self, allowed_chat_pairs: set[tuple[int, int]] | None = None, *, account_id: int | None = None
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
            allowed_chat_pairs: If set, only count the ``(account_id, chat_id)``
                chats the user can access. The account has to be part of the
                key: two accounts' copies of one chat share an id, so counting
                by id alone credited a folder with the other account's rows.
            account_id: If set, only this account's folders (None = unscoped until phase 4).
        """
        async with self.db_manager.async_session_factory() as session:
            # Counted per (account, folder), because a Telegram dialog-filter id
            # is per account and starts at 2 for everyone: grouped by folder_id
            # alone, one account's folder row joined to a count built from the
            # OTHER account's members. A restricted viewer then saw that other
            # folder's title, with its own count beside it.
            count_q = select(
                ChatFolderMember.account_id,
                ChatFolderMember.folder_id,
                func.count(ChatFolderMember.chat_id).label("chat_count"),
            )
            if allowed_chat_pairs is not None:
                # Same rule as ChatScope.sql_predicates: an empty grant is
                # "nothing", never "no filter". SQLAlchemy 2.0 does render an
                # empty IN as an always-false expression, but an access-control
                # filter must not rest on how the ORM renders an edge case.
                count_q = count_q.where(
                    tuple_(ChatFolderMember.account_id, ChatFolderMember.chat_id).in_(sorted(allowed_chat_pairs))
                    if allowed_chat_pairs
                    else false()
                )
            if account_id is not None:
                count_q = count_q.where(ChatFolderMember.account_id == account_id)
            count_subq = count_q.group_by(ChatFolderMember.account_id, ChatFolderMember.folder_id).subquery()

            stmt = (
                select(ChatFolder, count_subq.c.chat_count)
                .outerjoin(
                    count_subq,
                    and_(
                        ChatFolder.account_id == count_subq.c.account_id,
                        ChatFolder.id == count_subq.c.folder_id,
                    ),
                )
                .order_by(ChatFolder.sort_order, ChatFolder.title)
            )
            if account_id is not None:
                stmt = stmt.where(ChatFolder.account_id == account_id)

            result = await session.execute(stmt)
            folders = []
            for row in result:
                folder = row.ChatFolder
                count = row.chat_count or 0
                # Hide folders with no backed-up chats (empty tabs help no one).
                # With the count now per account, this is also what keeps a
                # restricted viewer from seeing the other account's folders:
                # none of their members are visible, so the count is zero.
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

    _SESSION_DELETE_CHUNK = 500

    @retry_on_locked()
    async def delete_all_sessions(self, *, keep_token: str | None = None) -> list[tuple[str, str]]:
        """Delete every viewer session, or every one but ``keep_token``.

        Returns the ``(token, username)`` of each deleted row, so the caller can
        close the sockets and purge the push channels those sessions held. One
        DELETE ... RETURNING, so a session cannot be deleted without being
        reported. SQLite before 3.35 has no RETURNING; there the rows are read
        first and only those are deleted, so the guarantee holds and a session
        created between the two statements survives. Sessions are credentials,
        not archive state.
        """
        condition = ViewerSession.token != keep_token if keep_token is not None else true()
        async with self.db_manager.async_session_factory() as session:
            if self.db_manager.engine.dialect.delete_returning:
                stmt = delete(ViewerSession).where(condition)
                result = await session.execute(stmt.returning(ViewerSession.token, ViewerSession.username))
                deleted = [(row[0], row[1]) for row in result.all()]
            else:
                result = await session.execute(select(ViewerSession.token, ViewerSession.username).where(condition))
                deleted = [(row[0], row[1]) for row in result.all()]
                tokens = [token for token, _ in deleted]
                # Chunked: those same old SQLite builds cap a statement at 999
                # bound variables, one per token here.
                for start in range(0, len(tokens), self._SESSION_DELETE_CHUNK):
                    chunk = tokens[start : start + self._SESSION_DELETE_CHUNK]
                    await session.execute(delete(ViewerSession).where(ViewerSession.token.in_(chunk)))
            await session.commit()
            return deleted

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
    # Media transcripts (032, docs/TRANSCRIPTION.md)
    # ========================================================================

    @staticmethod
    def _transcript_to_dict(row: MediaTranscript) -> dict[str, Any]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "media_id": row.media_id,
            "content_hash": row.content_hash,
            "idempotency_key": row.idempotency_key,
            "source": row.source,
            "engine_name": row.engine_name,
            "engine_version": row.engine_version,
            "preset": row.preset,
            "models": _json_list(row.models),
            "language": row.language,
            "language_confidence": row.language_confidence,
            "text": row.text,
            "words": _json_list(row.words),
            "segments": _json_list(row.segments),
            "confidence": row.confidence,
            "duration_s": row.duration_s,
            "job_id": row.job_id,
            "status": row.status,
            "error": row.error,
            "requested_at": row.requested_at,
            "completed_at": row.completed_at,
            "created_at": row.created_at,
            "job_stored_at": row.job_stored_at,
            "copied_from_id": row.copied_from_id,
            "diarize": row.diarize,
            "options_tag": row.options_tag,
        }

    @staticmethod
    async def _newest_transcript(session, media_id: str, account_id: int) -> MediaTranscript | None:
        stmt = (
            select(MediaTranscript)
            .where(and_(MediaTranscript.account_id == account_id, MediaTranscript.media_id == media_id))
            .order_by(MediaTranscript.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @retry_on_locked()
    async def enqueue_media_transcript(
        self,
        media_id: str,
        *,
        account_id: int,
        content_hash: str | None = None,
        idempotency_key: str | None = None,
        preset: str | None = None,
        source: str | None = None,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """Insert-if-absent of a ``queued`` row for one media; the open row, or None.

        The newest row decides. Open (``queued`` or ``running``): nothing is
        inserted and that row is returned, so a drain that runs twice and the
        viewer's ask-now route share one row. ``done`` or ``skipped``: None,
        and no row, unless ``force`` says the user asked for another
        transcript. ``failed``: a new row (the drain query decides whether
        a failed media is tried again). Two processes racing past the read
        both try to insert; the partial unique index on open rows stops the
        second, and the loser returns the winner's row as if it were its own.
        """
        async with self.db_manager.async_session_factory() as session:
            newest = await self._newest_transcript(session, media_id, account_id)
            if newest is not None:
                if newest.status in TRANSCRIPT_OPEN_STATUSES:
                    return self._transcript_to_dict(newest)
                if newest.status in ("done", "skipped") and not force:
                    return None
            now = utcnow_naive()
            row = MediaTranscript(
                account_id=account_id,
                media_id=media_id,
                content_hash=content_hash,
                idempotency_key=idempotency_key,
                preset=preset,
                source=source,
                status="queued",
                requested_at=now,
                created_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                newest = await self._newest_transcript(session, media_id, account_id)
                return self._transcript_to_dict(newest) if newest is not None else None
            await session.refresh(row)
            return self._transcript_to_dict(row)

    async def get_newest_media_transcript(self, media_id: str, *, account_id: int) -> dict[str, Any] | None:
        """The newest transcript row of one media, whatever its status, or None when it has none."""
        async with self.db_manager.async_session_factory() as session:
            newest = await self._newest_transcript(session, media_id, account_id)
        return self._transcript_to_dict(newest) if newest is not None else None

    async def count_waiting_transcript_asks(self, *, since: datetime) -> int:
        """How many viewer ask-now rows still wait for the backup, across every account.

        An ask-now row is ``queued`` with no ``job_id`` and no ``preset``: the
        drain fills the preset when it picks the row up, so a row stops
        counting once the backup has taken it. The viewer refuses a new ask
        past TRANSCRIPTION_ASK_MAX_OPEN of these.

        Only rows the drain can still take count: the file must be downloaded
        (the drain query skips the rest), and the ask must be newer than
        ``since``. A row no drain picks up (an account no backup runs for, a
        backup with transcription off) ages out of the count after that
        instead of holding the cap full for good. The row itself stays.
        """
        stmt = (
            select(func.count(MediaTranscript.id))
            .join(Media, and_(Media.account_id == MediaTranscript.account_id, Media.id == MediaTranscript.media_id))
            .where(
                and_(
                    MediaTranscript.status == "queued",
                    MediaTranscript.job_id.is_(None),
                    MediaTranscript.preset.is_(None),
                    MediaTranscript.requested_at >= since,
                    Media.downloaded == 1,
                )
            )
        )
        async with self.db_manager.async_session_factory() as session:
            return int((await session.execute(stmt)).scalar() or 0)

    @retry_on_locked()
    async def fill_media_transcript(
        self, transcript_id: int, *, status: str, account_id: int | None = None, **columns: Any
    ) -> bool:
        """Advance one row's ``status`` and fill its empty columns; True when a row changed.

        ``status`` only moves forward: queued, running, then done, failed or
        skipped, and a row that already reached a final status is left alone,
        so a repeated delivery of the same result changes nothing. Every other
        column is written once: a value lands only where the column is still
        NULL (COALESCE), which is the archive rule that nothing captured is
        overwritten. JSON columns take a list or a ready string. The first
        ``job_id`` written also stamps ``job_stored_at``, the time the
        straggler poll and the retention expiry count from.
        """
        if status not in TRANSCRIPT_STATUS_RANK:
            raise ValueError(f"unknown transcript status: {status}")
        unknown = set(columns) - TRANSCRIPT_FILL_COLUMNS
        if unknown:
            raise ValueError(f"not a fillable transcript column: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {"status": status}
        for name, value in columns.items():
            if value is None:
                continue
            if name in TRANSCRIPT_JSON_COLUMNS and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            values[name] = func.coalesce(getattr(MediaTranscript, name), value)
        if status in TRANSCRIPT_TERMINAL_STATUSES and "completed_at" not in values:
            values["completed_at"] = func.coalesce(MediaTranscript.completed_at, utcnow_naive())
        if "job_id" in values:
            values["job_stored_at"] = case(
                (MediaTranscript.job_id.is_(None), utcnow_naive()), else_=MediaTranscript.job_stored_at
            )
        rank = TRANSCRIPT_STATUS_RANK[status]
        allowed_from = [
            name
            for name, current in TRANSCRIPT_STATUS_RANK.items()
            if current <= rank and name not in TRANSCRIPT_TERMINAL_STATUSES
        ]
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(MediaTranscript)
                .where(and_(MediaTranscript.id == transcript_id, MediaTranscript.status.in_(allowed_from)))
                .values(**values)
            )
            if account_id is not None:
                stmt = stmt.where(MediaTranscript.account_id == account_id)
            result = await session.execute(stmt)
            await session.commit()
            return (result.rowcount or 0) > 0

    @retry_on_locked()
    async def mark_media_transcript_skipped(
        self,
        media_id: str,
        *,
        account_id: int,
        reason: str,
        content_hash: str | None = None,
        duration_s: float | None = None,
    ) -> dict[str, Any] | None:
        """Record that a media is not sent (too long); the ``skipped`` row.

        An open row for the media (a user asked before the drain saw the
        length) is closed as skipped instead of leaving it queued forever; a
        newest row already skipped is left as it is; otherwise a new row.
        """
        async with self.db_manager.async_session_factory() as session:
            newest = await self._newest_transcript(session, media_id, account_id)
        if newest is not None and newest.status in TRANSCRIPT_OPEN_STATUSES:
            await self.fill_media_transcript(
                newest.id, status="skipped", error=reason, content_hash=content_hash, duration_s=duration_s
            )
            return await self.get_media_transcript(newest.id)
        if newest is not None and newest.status == "skipped":
            return self._transcript_to_dict(newest)
        now = utcnow_naive()
        async with self.db_manager.async_session_factory() as session:
            row = MediaTranscript(
                account_id=account_id,
                media_id=media_id,
                content_hash=content_hash,
                duration_s=duration_s,
                status="skipped",
                error=reason,
                requested_at=now,
                completed_at=now,
                created_at=now,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._transcript_to_dict(row)

    async def find_copyable_transcript(
        self,
        content_hash: str,
        preset: str,
        *,
        account_id: int,
        media_id: str,
        diarize: bool,
        source: str | None = None,
        engine_name: str | None = None,
        options_tag: str | None = None,
        tagged_only: bool = False,
    ) -> dict[str, Any] | None:
        """The newest ``done`` row, in any account, of the same stored audio the drain would get again.

        The audio is matched on ``idempotency_key``, the stored file's hash,
        and the answer on ``preset``, on whether speakers were asked for
        (``diarize``; a row from before the column counts as not) and, when
        given, on the server's ``source`` and ``engine_name``. Rows of the
        media asking are left out.
        """
        if not content_hash or not preset:
            return None
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(MediaTranscript)
                .where(
                    and_(
                        MediaTranscript.idempotency_key == content_hash,
                        MediaTranscript.status == "done",
                        MediaTranscript.preset == preset,
                        MediaTranscript.diarize.is_(True)
                        if diarize
                        else or_(MediaTranscript.diarize.is_(None), MediaTranscript.diarize.is_(False)),
                        ~and_(MediaTranscript.account_id == account_id, MediaTranscript.media_id == media_id),
                    )
                )
                .order_by(MediaTranscript.id.desc())
                .limit(1)
            )
            if source is not None:
                stmt = stmt.where(MediaTranscript.source == source)
            if engine_name is not None:
                stmt = stmt.where(MediaTranscript.engine_name == engine_name)
            if options_tag is not None:
                # A row from before the tag (NULL) never matches: its options are unknown.
                stmt = stmt.where(MediaTranscript.options_tag == options_tag)
            elif tagged_only:
                stmt = stmt.where(MediaTranscript.options_tag.is_not(None))
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._transcript_to_dict(row) if row is not None else None

    async def get_media_chat_pairs(self, media_id: str) -> list[dict[str, Any]]:
        """``{account_id, chat_id, message_id}`` of every account's row for one media id.

        The id string is only unique per account, so the viewer resolves each
        copy to its chat and applies the visibility rule per copy.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media.account_id, Media.chat_id, Media.message_id)
                .where(Media.id == media_id)
                .order_by(Media.account_id)
            )
            result = await session.execute(stmt)
            return [{"account_id": row[0], "chat_id": row[1], "message_id": row[2]} for row in result]

    async def list_media_transcripts(self, media_id: str, *, account_id: int) -> list[dict[str, Any]]:
        """Every transcript row for one media, newest first."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(MediaTranscript)
                .where(and_(MediaTranscript.account_id == account_id, MediaTranscript.media_id == media_id))
                .order_by(MediaTranscript.id.desc())
            )
            result = await session.execute(stmt)
            return [self._transcript_to_dict(row) for row in result.scalars()]

    async def list_transcripts_for_media_ids(
        self, media_ids: Collection[str], *, account_id: int, with_twins: bool = False
    ) -> dict[str, list[dict[str, Any]]]:
        """Every transcript row for a page of media ids, newest first per media.

        One query for the whole page, so the message list carries its
        transcripts without a request per bubble. Media with no rows are
        absent from the result.

        ``with_twins`` is the viewer's read path: a media with no rows of its
        own gets the ``done`` rows of the same account whose
        ``idempotency_key`` is its ``content_hash``, the same audio held by
        another media row. That is how a transcript whose media row the
        voice/audio twin cleanup removed is still shown on the row that
        survived. Nothing is copied or moved; the rows stay where they are.
        """
        wanted = sorted({m for m in media_ids if isinstance(m, str) and m})
        if not wanted:
            return {}
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(MediaTranscript)
                .where(and_(MediaTranscript.account_id == account_id, MediaTranscript.media_id.in_(wanted)))
                .order_by(MediaTranscript.id.desc())
            )
            result = await session.execute(stmt)
            by_media: dict[str, list[dict[str, Any]]] = {}
            for row in result.scalars():
                by_media.setdefault(row.media_id, []).append(self._transcript_to_dict(row))
            missing = [media_id for media_id in wanted if media_id not in by_media]
            if not with_twins or not missing:
                return by_media
            hashes = await session.execute(
                select(Media.id, Media.content_hash).where(
                    and_(Media.account_id == account_id, Media.id.in_(missing), Media.content_hash.is_not(None))
                )
            )
            by_hash: dict[str, list[str]] = {}
            for media_id, content_hash in hashes:
                by_hash.setdefault(content_hash, []).append(media_id)
            if not by_hash:
                return by_media
            twins = await session.execute(
                select(MediaTranscript)
                .where(
                    and_(
                        MediaTranscript.account_id == account_id,
                        MediaTranscript.status == "done",
                        MediaTranscript.idempotency_key.in_(sorted(by_hash)),
                    )
                )
                .order_by(MediaTranscript.id.desc())
            )
            for row in twins.scalars():
                for media_id in by_hash[row.idempotency_key]:
                    by_media.setdefault(media_id, []).append(self._transcript_to_dict(row))
            return by_media

    async def get_transcripts_for_export(
        self,
        chat_id: int | None = None,
        *,
        account_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Every transcript row with the ``chat_id`` and ``message_id`` its media belongs to.

        For the two exports: every column, datetimes as ISO strings, newest
        row first. A transcript of an earlier media an edit replaced
        (``media_versions``) sits under its message too. A transcript whose
        media is gone has no message to sit under and is left out. ``None``
        scopes mean every chat or account.
        ``from_date`` (inclusive) and ``to_date`` (exclusive) bound the date
        of the message, as ``get_messages_for_export`` does, so a windowed
        export reads only the rows of the messages it exports.
        """
        async with self.db_manager.async_session_factory() as session:
            return await self._read_export_transcripts(
                session, chat_id, account_id=account_id, from_date=from_date, to_date=to_date
            )

    async def _read_export_transcripts(
        self,
        session,
        chat_id: int | None = None,
        *,
        account_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """``get_transcripts_for_export`` in a session the caller holds.

        The exports call it inside their snapshot, so each transcript they
        write names a media they list from the same archive state.
        """
        owners = self._transcribed_media_owners()
        stmt = (
            select(MediaTranscript, owners.c.chat_id, owners.c.message_id)
            .join(
                owners,
                and_(
                    owners.c.account_id == MediaTranscript.account_id,
                    owners.c.media_id == MediaTranscript.media_id,
                ),
            )
            .order_by(MediaTranscript.id.desc())
        )
        if chat_id is not None:
            stmt = stmt.where(owners.c.chat_id == chat_id)
        if account_id is not None:
            stmt = stmt.where(MediaTranscript.account_id == account_id)
        if from_date is not None or to_date is not None:
            stmt = stmt.join(
                Message,
                and_(
                    Message.account_id == owners.c.account_id,
                    Message.chat_id == owners.c.chat_id,
                    Message.id == owners.c.message_id,
                ),
            )
            if from_date is not None:
                stmt = stmt.where(Message.date >= from_date)
            if to_date is not None:
                stmt = stmt.where(Message.date < to_date)
        rows = []
        for transcript, media_chat_id, message_id in await session.execute(stmt):
            row = self._transcript_to_dict(transcript)
            # The source of a copy may sit in an account the export's
            # reader is not entitled to: its id stays out.
            row.pop("copied_from_id", None)
            for key in ("requested_at", "completed_at", "created_at", "job_stored_at"):
                if isinstance(row[key], datetime):
                    row[key] = row[key].isoformat()
            row["chat_id"] = media_chat_id
            row["message_id"] = message_id
            rows.append(row)
        return rows

    async def get_media_transcript(self, transcript_id: int, *, account_id: int | None = None) -> dict[str, Any] | None:
        """One transcript row by id, or None."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(MediaTranscript).where(MediaTranscript.id == transcript_id)
            if account_id is not None:
                stmt = stmt.where(MediaTranscript.account_id == account_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._transcript_to_dict(row) if row is not None else None

    async def get_media_awaiting_transcription(
        self,
        *,
        account_id: int,
        types: Collection[str],
        per_run: int,
        stale_before: datetime,
        priority_chat_ids: Sequence[int] = (),
    ) -> list[dict[str, Any]]:
        """The drain query: downloaded media of ``types`` that still needs a transcript.

        ``types`` follows the rule of ``is_transcribable`` in
        transcription_contract: a name outside ``TRANSCRIBABLE_TYPES`` is
        ignored, and a ``document`` qualifies only with an audio or video
        ``mime_type``.

        A media qualifies when its newest transcript row is missing, or is
        ``queued`` with no ``job_id`` and older than ``stale_before`` (a
        process died between the insert and the submit; the row is reused),
        or is ``failed`` under the retry rule below. A ``done`` or ``skipped``
        newest row ends the loop for that media.

        The retry rule reads each failed row's reason, so rows written before
        it follow it too. A failure about the file's content or the request
        counts: three of them end the retries. A failure in
        ``TRANSCRIPT_ENVIRONMENT_ERRORS`` does not count, since a repair of
        the disk or the server makes it go away. A file failure (missing,
        unreadable) qualifies on every drain; ``transcribe_media`` writes
        nothing while the file is still missing or unreadable, so such media
        come from a query of their own, at most ``per_run`` of them, each
        after the other media of its priority tier, and take no place in
        the budget: the drain stops once ``per_run`` media did more than
        that check. A server failure qualifies once the
        server finished a transcript of its own (not a copy) after it. With
        none since, the server may still be broken, and one such media per
        drain goes as a probe, from a query of its own: the first in
        priority order, longest waiting first, whose wait is over: none
        after its first failed row, then ``TRANSCRIPT_PROBE_WAIT`` after
        the second, doubled for each failed row after that. Ten
        failed rows of any reason end the retries, which stops a file the
        server keeps failing on while it finishes others.

        A media with no row of its own qualifies even when its account
        already holds a ``done`` transcript of the same audio: the drain's
        copy rule (``find_copyable_transcript``) then copies that answer when
        it matches the current server, preset and diarization, and sends the
        file when it does not. The viewer still shows a twin's row on it
        until then.

        A ``queued`` row with no ``job_id`` and no ``preset`` is a user's
        ask-now from the viewer, which never knows the preset: the backup
        fills it when it picks the row up. Such a row qualifies at once,
        whatever its type (the viewer does not know ``types`` and refuses
        types no server transcribes), and sorts first, so the next drain
        sends it first. Once picked up it carries a preset, and the same
        query takes it back after ten minutes if it is still ``queued`` with
        no ``job_id`` (a refusal, an outage or a crash mid-submit), whatever
        its type: the type filter of the main query would otherwise strand
        a pressed file of a type outside ``types`` for good. Then newest download
        first, at most ``per_run`` rows besides the file checks. Each result
        carries the media columns and ``transcript``: the newest row's id,
        status, job_id and error, or None.

        The ask-now rows are read by a query of their own, from the few
        open transcript rows. OR-ing them into the type filter of the main
        query would keep PostgreSQL off ``idx_media_type`` and read every
        media row of every type on each drain.

        ``priority_chat_ids`` (TRANSCRIPTION_PRIORITY_CHAT_IDS) orders the
        main query only: media of those chats first, in list order, then
        the rest, and the probe. It never widens what qualifies, and the
        ask-now rows still come before all of it.
        """
        wanted = sorted({t for t in types if isinstance(t, str) and t in TRANSCRIBABLE_TYPES})
        if not wanted or per_run <= 0:
            return []
        # The SQL form of is_transcribable: a document needs a mime_type with sound.
        has_sound = or_(
            Media.type != "document",
            *(func.lower(Media.mime_type).like(f"{prefix}%") for prefix in TRANSCRIBABLE_DOCUMENT_MIME_PREFIXES),
        )
        newest = aliased(MediaTranscript, name="newest_transcript")
        newest_id = (
            select(func.max(MediaTranscript.id))
            .where(and_(MediaTranscript.account_id == Media.account_id, MediaTranscript.media_id == Media.id))
            .correlate(Media)
            .scalar_subquery()
        )

        def failed_count(*conditions):
            return (
                select(func.count(MediaTranscript.id))
                .where(
                    and_(
                        MediaTranscript.account_id == Media.account_id,
                        MediaTranscript.media_id == Media.id,
                        MediaTranscript.status == "failed",
                        *conditions,
                    )
                )
                .correlate(Media)
                .scalar_subquery()
            )

        failed_rows = failed_count()
        counted_rows = failed_count(
            or_(MediaTranscript.error.is_(None), MediaTranscript.error.not_in(TRANSCRIPT_ENVIRONMENT_ERRORS))
        )
        failed_at = func.coalesce(newest.completed_at, newest.requested_at)
        # NOT of a comparison with a NULL reason is NULL, never true: the reason is tested for NULL first.
        server_failure = and_(
            newest.status == "failed", newest.error.is_not(None), newest.error.in_(TRANSCRIPT_SERVER_ERRORS)
        )
        retryable = and_(
            newest.status == "failed",
            counted_rows < TRANSCRIPT_MAX_FAILED_ROWS,
            failed_rows < TRANSCRIPT_MAX_ANY_FAILED_ROWS,
        )
        # Newest download first in both queries.
        order = (nulls_last(Media.download_date.desc()), Media.id.desc())
        ranks: dict[int, int] = {}
        for chat_id in priority_chat_ids:
            if isinstance(chat_id, int) and not isinstance(chat_id, bool):
                ranks.setdefault(chat_id, len(ranks))
        rank = (case(ranks, value=Media.chat_id, else_=len(ranks)),) if ranks else ()
        file_failure = and_(
            newest.status == "failed", newest.error.is_not(None), newest.error.in_(TRANSCRIPT_FILE_ERRORS)
        )
        main_order = (*rank, *order)
        columns = (Media, newest.id, newest.status, newest.job_id, newest.error)
        asked_stmt = (
            select(*columns)
            .join(newest, and_(newest.account_id == Media.account_id, newest.media_id == Media.id))
            .where(
                and_(
                    newest.account_id == account_id,
                    newest.status == "queued",
                    newest.job_id.is_(None),
                    or_(newest.preset.is_(None), newest.requested_at < stale_before),
                    newest.id == newest_id,
                    Media.downloaded == 1,
                )
            )
            .order_by(*order)
            .limit(per_run)
        )
        newest_join = and_(newest.account_id == Media.account_id, newest.media_id == Media.id, newest.id == newest_id)
        wanted_media = and_(Media.account_id == account_id, Media.downloaded == 1, Media.type.in_(wanted), has_sound)
        last_done = (
            select(func.max(MediaTranscript.completed_at))
            .where(
                and_(
                    MediaTranscript.account_id == account_id,
                    MediaTranscript.status == "done",
                    MediaTranscript.copied_from_id.is_(None),
                )
            )
            .scalar_subquery()
        )
        async with self.db_manager.async_session_factory() as session:
            # When the server last finished a transcript of its own (a copy is
            # no answer from it): a server failure from before then is retried.
            finished_at = (await session.execute(select(last_done))).scalar()
            server_retry = failed_at < finished_at if finished_at is not None else false()
            stmt = (
                select(*columns)
                .outerjoin(newest, newest_join)
                .where(
                    and_(
                        wanted_media,
                        or_(
                            newest.id.is_(None),
                            and_(
                                newest.status == "queued", newest.job_id.is_(None), newest.requested_at < stale_before
                            ),
                            and_(retryable, not_(file_failure), or_(not_(server_failure), server_retry)),
                        ),
                    )
                )
                .order_by(*main_order)
                .limit(per_run)
            )
            found = list(await session.execute(asked_stmt))
            asked = len(found)
            if len(found) < per_run:
                seen = {media.id for media, *_ in found}
                for match in await session.execute(stmt):
                    if match[0].id not in seen:
                        found.append(match)
                found = found[:per_run]
            if len(found) < per_run:
                # No transcript finished since these failed: the server may still
                # be broken, so one of them goes as a probe instead of all. The
                # first in priority order whose wait is over, longest waiting
                # first; its answer, done or not, tells the next drain. The wait
                # is in the query, so one still waiting never hides one whose
                # wait is over.
                now = utcnow_naive()
                cutoff = case(
                    *(
                        (failed_rows == n, now - TRANSCRIPT_PROBE_WAIT * 2 ** (n - 2))
                        for n in range(2, TRANSCRIPT_MAX_ANY_FAILED_ROWS)
                    ),
                    else_=now,
                )
                probe_stmt = (
                    select(*columns)
                    .join(newest, newest_join)
                    .where(and_(wanted_media, retryable, server_failure, not_(server_retry), failed_at <= cutoff))
                    .order_by(*rank, failed_at, newest.id)
                    .limit(1)
                )
                probe = list(await session.execute(probe_stmt))
            else:
                probe = []
            # A file still missing or unreadable is checked again on every drain
            # and writes nothing while it stays that way, so it takes no place
            # in the budget: at most ``per_run`` such files come from a query of
            # their own, each after the other media of its priority tier, and
            # the drain stops once ``per_run`` media did more than that check.
            file_stmt = (
                select(*columns)
                .join(newest, newest_join)
                .where(and_(wanted_media, retryable, file_failure))
                .order_by(*main_order)
                .limit(per_run)
            )
            waiting = list(await session.execute(file_stmt))

            def tier(match, waits: bool) -> tuple[int, bool]:
                return ranks.get(match[0].chat_id, len(ranks)), waits

            # Both lists are in priority order already; a stable sort by tier
            # keeps each one's order and puts the file checks last in a tier.
            ordered = sorted([(m, False) for m in found[asked:]] + [(m, True) for m in waiting], key=lambda e: tier(*e))
            found = found[:asked] + [m for m, _ in ordered] + probe
            rows = []
            for media, transcript_id, transcript_status, transcript_job_id, transcript_error in found:
                rows.append(
                    {
                        "id": media.id,
                        "account_id": media.account_id,
                        "message_id": media.message_id,
                        "chat_id": media.chat_id,
                        "type": media.type,
                        "file_path": media.file_path,
                        "file_name": media.file_name,
                        "file_size": media.file_size,
                        "mime_type": media.mime_type,
                        "duration": media.duration,
                        "content_hash": media.content_hash,
                        "transcript": (
                            {
                                "id": transcript_id,
                                "status": transcript_status,
                                "job_id": transcript_job_id,
                                "error": transcript_error,
                            }
                            if transcript_id is not None
                            else None
                        ),
                    }
                )
            return rows

    async def fill_open_transcripts_by_key(
        self, idempotency_key: str, *, job_id: str | None, status: str, **columns: Any
    ) -> list[dict[str, Any]]:
        """Write one job's outcome into every open row for the same audio; the rows filled.

        The callback, the event feed and the straggler poll all land here.
        The row rule first: when any row already reached a final status for
        ``job_id``, the outcome was applied before and nothing is written, so
        a replayed delivery or an old event re-read after a user asked for a
        new transcript changes nothing. Otherwise every ``queued`` or
        ``running`` row, in any account, whose ``idempotency_key`` is this
        hash and whose ``job_id`` is empty or this job gets the outcome, so
        the same audio under two media rows shares one job and both fill.
        Each fill goes through ``fill_media_transcript``, so only empty
        columns are written.
        """
        if not idempotency_key:
            return []
        async with self.db_manager.async_session_factory() as session:
            if job_id:
                applied = await session.execute(
                    select(MediaTranscript.id)
                    .where(
                        and_(
                            MediaTranscript.job_id == job_id,
                            MediaTranscript.status.in_(TRANSCRIPT_TERMINAL_STATUSES),
                        )
                    )
                    .limit(1)
                )
                if applied.first() is not None:
                    return []
            job_match = MediaTranscript.job_id.is_(None)
            if job_id:
                job_match = or_(job_match, MediaTranscript.job_id == job_id)
            result = await session.execute(
                select(MediaTranscript.id, MediaTranscript.account_id, MediaTranscript.media_id)
                .where(
                    and_(
                        MediaTranscript.idempotency_key == idempotency_key,
                        MediaTranscript.status.in_(TRANSCRIPT_OPEN_STATUSES),
                        job_match,
                    )
                )
                .order_by(MediaTranscript.id)
            )
            open_rows = list(result)
        filled = []
        for row_id, account_id, media_id in open_rows:
            if await self.fill_media_transcript(row_id, status=status, job_id=job_id, **columns):
                filled.append({"id": row_id, "account_id": account_id, "media_id": media_id, "status": status})
        return filled

    async def get_open_job_transcripts(
        self, *, account_id: int, stored_before: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Open rows of one account that hold a job id; with ``stored_before``, only the stragglers.

        Without ``stored_before`` every job still in flight, which the drain
        counts against its per-run limit. A row from before ``job_stored_at``
        existed counts from its insert time.
        """
        conditions = [
            MediaTranscript.account_id == account_id,
            MediaTranscript.status.in_(TRANSCRIPT_OPEN_STATUSES),
            MediaTranscript.job_id.is_not(None),
        ]
        if stored_before is not None:
            conditions.append(
                func.coalesce(MediaTranscript.job_stored_at, MediaTranscript.requested_at) < stored_before
            )
        async with self.db_manager.async_session_factory() as session:
            stmt = select(MediaTranscript).where(and_(*conditions)).order_by(MediaTranscript.id)
            result = await session.execute(stmt)
            return [self._transcript_to_dict(row) for row in result.scalars()]

    async def get_transcription_events_cursor(self) -> str | None:
        """Where the akou event feed was last read, or None before the first read."""
        return await self.get_setting(TRANSCRIPTION_EVENTS_CURSOR_KEY)

    async def set_transcription_events_cursor(self, cursor: str) -> None:
        await self.set_setting(TRANSCRIPTION_EVENTS_CURSOR_KEY, cursor)

    async def get_transcription_server(self) -> dict[str, str] | None:
        """``{"name", "version"}`` of the server the backup last detected, or None."""
        raw = await self.get_setting(TRANSCRIPTION_SERVER_KEY)
        if not raw:
            return None
        try:
            loaded = json.loads(raw)
        except ValueError, TypeError:
            return None
        if not isinstance(loaded, dict):
            return None
        return {"name": str(loaded.get("name") or ""), "version": str(loaded.get("version") or "")}

    async def set_transcription_server(self, name: str, version: str) -> None:
        await self.set_setting(TRANSCRIPTION_SERVER_KEY, json.dumps({"name": name, "version": version}))

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
