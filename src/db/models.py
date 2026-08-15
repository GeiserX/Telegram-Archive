"""
SQLAlchemy ORM models for Telegram Backup.

v6.0.0 - Normalized schema with proper foreign key constraints.
Media data is now stored only in the media table, not duplicated in messages.

v8.0.0 - Multi-account. ``account_id`` leads the primary key of every table
holding Telegram data, because a Telegram chat id, message id, topic id and
dialog-filter id are only unique *within one account*. Without it a second
account silently overwrites the first: measured on scratch schemas, a decorative
``account_id`` column left ``message_versions``' UNIQUE(change_hash) discarding
the second account's entire edit history, ``chat_folders``' PK(id) destroying the
first account's folder title and every membership row, and ``messages``' PK
(id, chat_id) dropping the second account's copy so it read the first account's
``is_outgoing``.

``users`` stays global on purpose: a Telegram user id is the same person for
both accounts, and per-account users turns
``get_chats_for_folder_resolution``'s ``outerjoin(User, User.id == Chat.id)``
into duplicate rows. The cost is last-writer-wins on a contact's display name.
"""

import secrets
from datetime import datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..message_utils import utcnow_naive

# The account every row written before v8.0.0 belongs to. Migration 022 seeds it
# and backfills every existing row to it, and it is the server-side default of
# every account_id column so a writer that does not name an account still lands
# somewhere real instead of failing the NOT NULL.
DEFAULT_ACCOUNT_ID = 1

# secrets.token_urlsafe(16) is always exactly 22 characters of URL-safe base64
# over 128 bits. Chat refs are minted once, on INSERT, and never re-rolled.
CHAT_REF_BYTES = 16
CHAT_REF_LENGTH = 22


def new_chat_ref() -> str:
    """Mint the opaque handle a chat is addressed by outside the database.

    Applied as a Python-side column default, which is exactly the semantics
    needed: an upsert's INSERT branch mints one, and its ON CONFLICT DO UPDATE
    branch never touches the column, so a ref is stable for the life of the row.
    """
    return secrets.token_urlsafe(CHAT_REF_BYTES)


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Account(Base):
    """One archived Telegram identity.

    ``id`` is a surrogate, never the Telegram user id. The row has to exist
    before authorization completes (SESSION_NAME, PHONE_NUMBER and the chat
    filters all describe an account that has not logged in yet), and copying a
    real Telegram user id into every row of every table and every index would
    spread an identifier this project treats as PII across the whole archive.
    ``telegram_user_id`` is filled in on first login.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(String(255))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)


class Chat(Base):
    """Chats table - users, groups, channels."""

    __tablename__ = "chats"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=str(DEFAULT_ACCOUNT_ID))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # Opaque, URL-safe, globally unique. Minted on INSERT and never rewritten.
    ref: Mapped[str] = mapped_column(String(CHAT_REF_LENGTH), nullable=False, default=new_chat_ref)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    participants_count: Mapped[int | None] = mapped_column(Integer)
    is_forum: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # v6.2.0: forum with topics
    is_archived: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # v6.2.0: archived chat
    last_synced_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, server_default=func.now()
    )

    # Relationships
    messages: Mapped[list[Message]] = relationship("Message", back_populates="chat", lazy="dynamic")
    sync_status: Mapped[SyncStatus | None] = relationship("SyncStatus", back_populates="chat", uselist=False)
    forum_topics: Mapped[list[ForumTopic]] = relationship("ForumTopic", back_populates="chat", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("ref", name="uq_chats_ref"),
        Index("idx_chats_username", "username"),
    )


class Message(Base):
    """Messages table - all messages from all chats.

    v6.0.0: media_type, media_id, media_path removed - use media_items relationship instead.
    """

    __tablename__ = "messages"

    # Composite primary key (account_id, chat_id, id) - message IDs are only
    # unique within a chat, and a chat id is only unique within an account.
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=str(DEFAULT_ACCOUNT_ID))
    id: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NOTE: sender_id has no FK constraint because it can be channel/group IDs (not in users table)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    reply_to_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_to_top_id: Mapped[int | None] = mapped_column(BigInteger)  # v6.2.0: forum topic thread ID
    reply_to_text: Mapped[str | None] = mapped_column(Text)
    forward_from_id: Mapped[int | None] = mapped_column(BigInteger)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime)
    # v6.0.0: media_type, media_id, media_path REMOVED - normalized to media table
    raw_data: Mapped[str | None] = mapped_column(Text)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    is_outgoing: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    is_pinned: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1 - whether this message is pinned
    is_deleted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0 or 1 - deleted on Telegram
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Relationships
    chat: Mapped[Chat] = relationship("Chat", back_populates="messages")
    # NOTE: sender relationship works via ORM join, no DB-level FK (sender_id can be channel/group IDs)
    sender: Mapped[User | None] = relationship(
        "User",
        back_populates="messages",
        primaryjoin="Message.sender_id == User.id",
        foreign_keys="[Message.sender_id]",
    )
    reactions: Mapped[list[Reaction]] = relationship("Reaction", back_populates="message", lazy="dynamic")
    # lazy="select": every read path that needs media (get_messages_paginated,
    # get_pinned_messages, find_message_by_date*, the streaming export) already
    # outer-joins the media table and reads the columns from that join. Under
    # lazy="selectin" SQLAlchemy fired a SECOND full media read per page whose
    # ORM objects were built and thrown away — nothing in src/ reads
    # Message.media_items.
    media_items: Mapped[list[Media]] = relationship("Media", back_populates="message", lazy="select")
    versions: Mapped[list[MessageVersion]] = relationship("MessageVersion", back_populates="message", lazy="dynamic")

    __table_args__ = (
        PrimaryKeyConstraint("account_id", "chat_id", "id"),
        ForeignKeyConstraint(["account_id", "chat_id"], ["chats.account_id", "chats.id"], name="fk_messages_chat"),
        Index("idx_messages_chat_id", "chat_id"),
        Index("idx_messages_date", "date"),
        Index("idx_messages_sender_id", "sender_id"),
        # Composite index for fast pagination: WHERE chat_id = ? ORDER BY date DESC
        Index("idx_messages_chat_date_desc", "chat_id", date.desc()),
        # v7.22.0: id-bounded jump-window cursors (lone before_id / after_id, #213)
        # seek on (chat_id, id).
        Index("idx_messages_chat_id_id", "chat_id", "id"),
        # v8.0.0: repairs a regression this release's key change introduces.
        # resolve_message_chat_id / get_chat_id_for_message look a message up by
        # id alone (the listener's path when Telegram reports a deletion without
        # naming a chat). Until v8.0.0 the PK led with id and served that seek;
        # it now leads with account_id, so the account-qualified lookup needs its
        # own index or it degrades to a full scan of every message archived.
        Index("idx_messages_account_msgid", "account_id", "id"),
        # Index for finding pinned messages in a chat
        Index("idx_messages_chat_pinned", "chat_id", "is_pinned"),
        # Index for reply lookups
        Index("idx_messages_reply_to", "chat_id", "reply_to_msg_id"),
        # v6.2.0: Index for topic message lookups in forum chats
        Index("idx_messages_topic", "chat_id", "reply_to_top_id"),
        # PostgreSQL-only (#295-perf): the viewer's text search is
        # Message.text.ilike('%term%') - a leading wildcard, which a B-tree
        # can't serve at all, so every search was a full sequential scan
        # that gets linearly slower as the archive grows. A GIN trigram
        # index turns it into a bounded-cost lookup regardless of table
        # size (measured ~95x faster on a ~100k-row instance: 41.8ms ->
        # 0.4ms). ``postgresql_using``/``postgresql_ops`` are dialect-
        # scoped kwargs - SQLAlchemy drops them for every dialect other
        # than PostgreSQL, so create_all() on SQLite still gets a same-
        # name/same-column index, just a plain (unused but harmless)
        # B-tree instead. Requires the pg_trgm extension, created by the
        # DDL hook below (create_all() path) and by migration 022 (Alembic
        # path, which is authoritative for PostgreSQL - see 021's
        # docstring).
        Index(
            "idx_messages_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
    )


# idx_messages_text_trgm's gin_trgm_ops needs the pg_trgm extension to exist
# before create_all() reaches that index - fired here rather than relying on
# migration 022 alone, since create_all() (the SQLite-and-test-fixture path,
# not the app's own PostgreSQL runtime path - see 021's docstring) never
# runs Alembic. execute_if scopes this to PostgreSQL only; SQLite's
# create_all() run never touches it.
event.listen(
    Message.__table__,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS pg_trgm").execute_if(dialect="postgresql"),
)


class MessageVersion(Base):
    """Historical text versions for edited messages."""

    __tablename__ = "message_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=str(DEFAULT_ACCOUNT_ID))
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    change_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())

    message: Mapped[Message] = relationship(
        "Message",
        back_populates="versions",
        primaryjoin=(
            "and_(MessageVersion.account_id==Message.account_id,"
            " MessageVersion.message_id==Message.id,"
            " MessageVersion.chat_id==Message.chat_id)"
        ),
        foreign_keys="[MessageVersion.account_id, MessageVersion.message_id, MessageVersion.chat_id]",
    )

    __table_args__ = (
        # NOTE: the CASCADE is inert on SQLite (PRAGMA foreign_keys is off by
        # default) — the explicit MessageVersion deletes in delete_message and
        # delete_chat_and_related_data are the load-bearing cleanup. Keep them
        # in sync with any future message-deletion path.
        ForeignKeyConstraint(
            ["account_id", "message_id", "chat_id"],
            ["messages.account_id", "messages.id", "messages.chat_id"],
            name="fk_message_versions_message",
            ondelete="CASCADE",
        ),
        # v8.0.0: account-qualified. The change_hash payload built by
        # _message_version_hash is a frozen contract and is NOT extended with the
        # account — re-encoding it would re-admit a duplicate of every version
        # already stored. The CONSTRAINT carries the account instead, so two
        # accounts archiving the same edit each keep their own history row
        # rather than the second one being silently discarded.
        UniqueConstraint("account_id", "change_hash", name="uq_message_versions_change_hash"),
        Index("idx_message_versions_message_date", "chat_id", "message_id", "date"),
        Index("idx_message_versions_message_captured", "chat_id", "message_id", "captured_at"),
    )


class User(Base):
    """Users table - message senders."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    is_bot: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, server_default=func.now()
    )

    # Relationships
    # NOTE: Explicit join because sender_id has no DB-level FK (can contain channel/group IDs)
    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="sender",
        primaryjoin="User.id == Message.sender_id",
        foreign_keys="[Message.sender_id]",
        lazy="dynamic",
    )

    __table_args__ = (Index("idx_users_username", "username"),)


class Media(Base):
    """Media table - downloaded media files.

    v6.0.0: Now the single source of truth for media metadata.
    Foreign key constraint to messages table added.
    """

    __tablename__ = "media"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=str(DEFAULT_ACCOUNT_ID))
    # NOT a Telegram file_id: the value is built as f"{chat_id}_{message.id}_{type}"
    # (src/telegram_backup.py, src/listener.py), so two accounts archiving the
    # same message produce the identical string. account_id leads the key.
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str | None] = mapped_column(String(50))
    file_path: Mapped[str | None] = mapped_column(Text)  # v6.0.0: Changed to Text for long paths
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # SHA-256 hex digest
    downloaded: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    # v7.x (#212): failed-download retry counter. The pending-media retry loop skips rows
    # at/above MEDIA_MAX_DOWNLOAD_ATTEMPTS so a permanently-unwritable file stops re-fetching.
    download_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    download_date: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())

    # Relationship to message
    message: Mapped[Message | None] = relationship(
        "Message",
        back_populates="media_items",
        primaryjoin=(
            "and_(Media.account_id==Message.account_id, Media.message_id==Message.id, Media.chat_id==Message.chat_id)"
        ),
        foreign_keys="[Media.account_id, Media.message_id, Media.chat_id]",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "message_id", "chat_id"],
            ["messages.account_id", "messages.id", "messages.chat_id"],
            name="fk_media_message",
            ondelete="CASCADE",
        ),
        Index("idx_media_message", "message_id", "chat_id"),
        Index("idx_media_downloaded", "chat_id", "downloaded"),
        Index("idx_media_type", "type"),
        Index("idx_media_content_hash", "content_hash"),
        Index("idx_media_chat_type", "chat_id", "type"),
    )


class Reaction(Base):
    """Reactions table - message reactions."""

    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=str(DEFAULT_ACCOUNT_ID))
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emoji: Mapped[str] = mapped_column(String(50), nullable=False)
    # Named explicitly: migration 005 created this constraint as fk_reactions_user,
    # and an unnamed ForeignKey() leaves PostgreSQL to invent reactions_user_id_fkey
    # instead — the same constraint under two names on the two schema paths.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL", name="fk_reactions_user")
    )
    count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    # v7.23.0 (#219): retain-on-removal tombstone. When a reaction is no longer
    # present in a fresh snapshot it is stamped here instead of being deleted, so
    # a removed reaction is preserved rather than silently dropped. The live count
    # (get_messages_paginated) excludes rows where removed_at IS NOT NULL.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Relationship to message (composite foreign key)
    message: Mapped[Message] = relationship(
        "Message",
        back_populates="reactions",
        primaryjoin=(
            "and_(Reaction.account_id==Message.account_id,"
            " Reaction.message_id==Message.id,"
            " Reaction.chat_id==Message.chat_id)"
        ),
        foreign_keys="[Reaction.account_id, Reaction.message_id, Reaction.chat_id]",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "message_id", "chat_id"],
            ["messages.account_id", "messages.id", "messages.chat_id"],
            name="fk_reaction_message",
        ),
        UniqueConstraint("account_id", "message_id", "chat_id", "emoji", "user_id", name="uq_reaction"),
        Index("idx_reactions_message", "message_id", "chat_id"),
        # v7.23.0 (#219): chat-first composite matches the schema's convention
        # (every other per-entity index leads with chat_id) and serves the
        # page read `WHERE chat_id = ? AND message_id IN (...)`.
        Index("idx_reactions_chat_message", "chat_id", "message_id"),
        Index("idx_reactions_user", "user_id"),
    )


class SyncStatus(Base):
    """Sync status table - tracks backup progress per chat."""

    __tablename__ = "sync_status"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=str(DEFAULT_ACCOUNT_ID))
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    # Relationship
    chat: Mapped[Chat] = relationship("Chat", back_populates="sync_status")

    __table_args__ = (
        # message_count is accumulated by update_sync_status' ON CONFLICT clause
        # (`message_count + excluded.message_count`), so two accounts collapsing
        # into one row would not overwrite it, they would SUM it and report a
        # message count that never existed.
        ForeignKeyConstraint(["account_id", "chat_id"], ["chats.account_id", "chats.id"], name="fk_sync_status_chat"),
    )


class Metadata(Base):
    """Metadata table - key-value store for app settings."""

    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class PushSubscription(Base):
    """Push notification subscriptions for Web Push API."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # Push service URL
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)  # Public key
    auth: Mapped[str] = mapped_column(String(255), nullable=False)  # Auth secret
    chat_id: Mapped[int | None] = mapped_column(BigInteger)  # Optional: subscribe to specific chat only
    username: Mapped[str | None] = mapped_column(String(255))  # User who created this subscription
    allowed_chat_ids: Mapped[str | None] = mapped_column(
        Text
    )  # JSON snapshot of user's allowed chats at subscribe time
    # v8.0.0 entitlement. NEW columns, never a reinterpreted allowed_chat_ids:
    # reading an old [123, 456] payload as (account 123, chat 456) would GRANT
    # rather than deny, so an unconverted 7.x row must never be readable as an
    # account-qualified one. See ViewerAccount for the full reasoning.
    allowed_accounts: Mapped[str | None] = mapped_column(Text)
    allowed_chat_refs: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(String(500))  # Browser info for debugging
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)  # Track activity

    __table_args__ = (Index("idx_push_sub_chat", "chat_id"), Index("idx_push_sub_username", "username"))


class ForumTopic(Base):
    """Forum topics table - topics within forum-enabled chats.

    v6.2.0: Stores topic metadata for forum groups/channels.
    """

    __tablename__ = "forum_topics"

    account_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=str(DEFAULT_ACCOUNT_ID))
    id: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    icon_color: Mapped[int | None] = mapped_column(Integer)
    icon_emoji_id: Mapped[int | None] = mapped_column(BigInteger)
    icon_emoji: Mapped[str | None] = mapped_column(String(32))  # Unicode emoji resolved from icon_emoji_id
    is_closed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_pinned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_hidden: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    date: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())

    # Relationships
    chat: Mapped[Chat] = relationship("Chat", back_populates="forum_topics")

    __table_args__ = (
        PrimaryKeyConstraint("account_id", "chat_id", "id"),
        ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name="fk_forum_topics_chat",
            ondelete="CASCADE",
        ),
        Index("idx_forum_topics_chat", "chat_id"),
    )


class ChatFolder(Base):
    """Chat folders table - user-created Telegram folders.

    v6.2.0: Stores folder metadata from Telegram dialog filters.
    """

    __tablename__ = "chat_folders"

    # Telegram dialog-filter ids are per account and start at 2 for everyone, so
    # without account_id in the key a second account's folder overwrites the
    # first account's title and takes over its membership rows.
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=str(DEFAULT_ACCOUNT_ID))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    emoticon: Mapped[str | None] = mapped_column(String(50))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())

    # Relationships
    members: Mapped[list[ChatFolderMember]] = relationship(
        "ChatFolderMember", back_populates="folder", cascade="all, delete-orphan"
    )


class ChatFolderMember(Base):
    """Chat folder members table - maps chats to folders.

    v6.2.0: Junction table for many-to-many relationship between folders and chats.
    """

    __tablename__ = "chat_folder_members"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=str(DEFAULT_ACCOUNT_ID))
    folder_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Relationships
    folder: Mapped[ChatFolder] = relationship("ChatFolder", back_populates="members")
    # viewonly: account_id belongs to BOTH composite foreign keys, so without
    # this SQLAlchemy warns that persisting through `chat` and through `folder`
    # would both write it. Nothing does: membership rows are built with explicit
    # folder_id/chat_id values, and this attribute exists only to read the chat.
    chat: Mapped[Chat] = relationship("Chat", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "folder_id"],
            ["chat_folders.account_id", "chat_folders.id"],
            name="fk_folder_members_folder",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name="fk_folder_members_chat",
            ondelete="CASCADE",
        ),
        Index("idx_folder_members_chat", "chat_id"),
        Index("idx_folder_members_folder", "folder_id"),
    )


class ViewerAccount(Base):
    """Viewer accounts for multi-user web viewer access control.

    v7.0.0: DB-backed viewer accounts with per-user chat whitelists.
    Master account uses env vars; these are additional restricted viewers.
    """

    __tablename__ = "viewer_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_chat_ids: Mapped[str | None] = mapped_column(Text)  # JSON array of chat IDs, NULL = all
    # v8.0.0 entitlement, as two NEW nullable columns rather than a
    # reinterpretation of allowed_chat_ids. Reinterpreting fails OPEN: an
    # unconverted [123, 456] read as (account 123, chat 456) grants access
    # instead of denying it. With new columns a 7.x row is unmistakably
    # unconverted. allowed_accounts: JSON array of account ids. allowed_chat_refs:
    # JSON array of chats.ref values. Migration 022 converts every restricted row
    # rather than leaving it NULL, because NULL means "no restriction".
    allowed_accounts: Mapped[str | None] = mapped_column(Text)
    allowed_chat_refs: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    no_download: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # v7.2.0
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, server_default=func.now()
    )


class ViewerAuditLog(Base):
    """Audit log for viewer access and admin operations.

    v7.0.0: Tracks login attempts, admin CRUD, and API access.
    Username is denormalized so audit records survive viewer deletion.
    """

    __tablename__ = "viewer_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "master" or "viewer"
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(255))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())

    # These two indexes are also created by migration 007. A fresh SQLite install
    # is provisioned by create_all() and then stamped straight at the head
    # revision, so 007 never runs there and the table was left unindexed forever
    # — the lone index-level drift between a create_all schema and a migrated one.
    # They serve get_audit_logs: `WHERE username = ? ORDER BY created_at DESC`.
    __table_args__ = (
        Index("idx_audit_log_username", "username"),
        Index("idx_audit_log_created", "created_at"),
    )


class ViewerSession(Base):
    """Persistent viewer sessions for web authentication.

    v7.1.0: DB-backed sessions survive container restarts.
    In-memory cache in main.py provides fast lookups; DB is the persistence layer.
    """

    __tablename__ = "viewer_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "master", "viewer", or "token"
    allowed_chat_ids: Mapped[str | None] = mapped_column(Text)  # JSON array or NULL = all chats
    # v8.0.0 entitlement — see ViewerAccount for why these are new columns.
    allowed_accounts: Mapped[str | None] = mapped_column(Text)
    allowed_chat_refs: Mapped[str | None] = mapped_column(Text)
    no_download: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # v7.2.0
    source_token_id: Mapped[int | None] = mapped_column(Integer)  # v7.2.0: FK to viewer_tokens.id for revocation
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_accessed: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_viewer_sessions_username", "username"),
        Index("idx_viewer_sessions_created_at", "created_at"),
        Index("idx_viewer_sessions_source_token", "source_token_id"),
    )


class ViewerToken(Base):
    """Share tokens for scoped chat access without username/password.

    v7.2.0: Admins create tokens granting time-bound access to specific chats.
    Tokens are hashed (PBKDF2-SHA256) - plaintext shown only at creation.
    """

    __tablename__ = "viewer_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_chat_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array of chat IDs
    # v8.0.0 entitlement — see ViewerAccount for why these are new columns.
    allowed_accounts: Mapped[str | None] = mapped_column(Text)
    allowed_chat_refs: Mapped[str | None] = mapped_column(Text)
    is_revoked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    no_download: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # NULL = no expiry
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    use_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, server_default=func.now())

    __table_args__ = (
        Index("idx_viewer_tokens_created_by", "created_by"),
        Index("idx_viewer_tokens_is_revoked", "is_revoked"),
    )


class AppSettings(Base):
    """Key-value settings shared between backup and viewer containers.

    v7.2.0: Used for backup schedule override, viewer tracking, and status.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, server_default=func.now()
    )
