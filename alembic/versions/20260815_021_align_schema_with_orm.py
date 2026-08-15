"""Make ``alembic upgrade head`` produce exactly the schema in models.py.

Until now this project had two schema authors that nothing compared:
``Base.metadata.create_all`` (SQLite only, from ``src/db/base.py``) and this
migration chain (the only author PostgreSQL ever had). They had drifted to 54
structural differences on SQLite and 50 on PostgreSQL. This migration removes
every one of them, and ``tests/test_schema_parity.py`` keeps them removed: its
allow-list is empty, so any new divergence between the two authors fails CI on
both backends.

What it changes, and why each is safe:

* **NOT NULL on 35 columns.** The ORM has always marked them non-optional; the
  migrations never said so, so a PostgreSQL install accepted NULLs that the
  SQLite install of the same version rejected. Every column is backfilled with
  the ORM's own default *before* it is tightened, so a database holding NULLs
  converges instead of crash-looping on ``SET NOT NULL``.
* **Server defaults.** ``created_at``/``updated_at`` gain the ``func.now()``
  default the ORM declares. ``messages.is_pinned``/``is_outgoing`` and
  ``media.downloaded`` lose the literal ``0`` the ORM does not declare (their
  value is always supplied Python-side). ``chats.id``/``users.id`` lose the
  ``BIGSERIAL`` sequence migration 001 gave them by accident — those ids come
  from Telegram and were never generated.
* **``media.file_path`` VARCHAR(500) -> TEXT on PostgreSQL.** This one is a live
  bug: models.py widened it to Text in v6.0.0 for long paths, no migration
  followed, so a path over 500 characters raises StringDataRightTruncation on
  PostgreSQL and inserts fine on SQLite.
* **INTEGER -> BIGINT on eight SQLite columns** that migration 005's
  hand-written rebuild declared narrower than the ORM. Cosmetic on SQLite
  (both are 8-byte), but it is the same declaration that produced the
  VARCHAR(500) above, and leaving it makes the gate impossible to satisfy.
* **``fk_reaction_message``.** The ORM constrains a reaction to its message; no
  migration ever created it, so an Alembic-built install has orphanable
  reaction rows. Orphans are deleted first or the constraint cannot be added.
* **``_messages_media_backup``.** Migration 005 creates this rollback table and
  drops it only in its downgrade, so every Alembic-built install carries a copy
  forever. It is dropped ONLY when empty: on a database that really did upgrade
  through v6.0.0 with legacy media rows it still holds the pre-normalization
  pointers, and this migration will not delete those.

Idempotency: every step reads the live schema first and does nothing when the
object is already in its target shape, so this runs clean against a database
provisioned by ``create_all`` (where most of it is already true), against one
built by this chain, and against a re-run of itself.

One difference is deliberately left alone. A SQLite database created by the
``create_all`` of a release before this one carries the reactions -> users
foreign key unnamed, because models.py only started naming it
``fk_reactions_user`` here. SQLite never enforces that constraint (nothing in
this project sets ``PRAGMA foreign_keys=ON``) and never exposes its name, so
relabelling it would mean rebuilding a table of the user's data for a label
nothing can observe. Every freshly built schema on either backend agrees, which
is what the parity gate checks.

Revision ID: 021
Revises: 020
Create Date: 2026-08-15
"""

import logging
import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

BACKUP_TABLE = "_messages_media_backup"

# Columns the ORM declares non-optional, with the value used to backfill any
# existing NULL. "now" is the dialect's current timestamp; anything else is a
# numeric literal. Ordering is irrelevant - each table is handled independently.
NOT_NULL_COLUMNS: dict[str, dict[str, str]] = {
    "app_settings": {"updated_at": "now"},
    "chat_folders": {"created_at": "now", "sort_order": "0", "updated_at": "now"},
    "chats": {
        "created_at": "now",
        "is_archived": "0",
        "is_forum": "0",
        "last_synced_message_id": "0",
        "updated_at": "now",
    },
    "forum_topics": {
        "created_at": "now",
        "is_closed": "0",
        "is_hidden": "0",
        "is_pinned": "0",
        "updated_at": "now",
    },
    "media": {"created_at": "now", "downloaded": "0"},
    "messages": {"created_at": "now", "is_outgoing": "0"},
    "reactions": {"count": "1", "created_at": "now"},
    "sync_status": {"last_message_id": "0", "last_sync_date": "now", "message_count": "0"},
    "users": {"created_at": "now", "is_bot": "0", "updated_at": "now"},
    "viewer_accounts": {"created_at": "now", "no_download": "0", "updated_at": "now"},
    "viewer_audit_log": {"created_at": "now"},
    "viewer_sessions": {"no_download": "0"},
    "viewer_tokens": {
        "created_at": "now",
        "is_revoked": "0",
        "no_download": "0",
        "use_count": "0",
    },
}

# Columns whose ORM definition carries server_default=func.now().
TIMESTAMP_DEFAULT_COLUMNS: dict[str, tuple[str, ...]] = {
    "chats": ("created_at", "updated_at"),
    "media": ("created_at",),
    "messages": ("created_at",),
    "reactions": ("created_at",),
    "sync_status": ("last_sync_date",),
    "users": ("created_at", "updated_at"),
}

# Columns the migrations gave a literal default the ORM does not declare. The
# value is always supplied by the ORM's Python-side default on insert.
DROP_DEFAULT_COLUMNS: dict[str, tuple[str, ...]] = {
    "media": ("downloaded",),
    "messages": ("is_outgoing", "is_pinned"),
}

# BigInteger in the ORM, INTEGER in migration 005's hand-written SQLite DDL.
SQLITE_BIGINT_COLUMNS: dict[str, tuple[str, ...]] = {
    "media": ("chat_id", "file_size", "message_id"),
    "messages": ("chat_id", "forward_from_id", "id", "reply_to_msg_id", "sender_id"),
}

# PostgreSQL BIGSERIAL primary keys the ORM declares as autoincrement=False.
PG_UNWANTED_SEQUENCES: tuple[tuple[str, str], ...] = (("chats", "id"), ("users", "id"))


def _now_sql(dialect: str) -> str:
    return "now()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"


def _columns(inspector: sa.Inspector, table: str) -> dict[str, dict]:
    """Reflected columns of ``table``, or ``{}`` when the table is absent."""
    if table not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table)}


def _fk_to_table_exists(inspector: sa.Inspector, table: str, referred_table: str) -> bool:
    """True if ``table`` already has any FK referencing ``referred_table``.

    Name-agnostic on purpose, exactly as migration 005 does it: a
    create_all()-provisioned database names its constraints differently, and
    "already constrained" is the question that matters, not "named like this".
    """
    if table not in inspector.get_table_names():
        return False
    return any(fk.get("referred_table") == referred_table for fk in inspector.get_foreign_keys(table))


def _backfill_nulls(conn: sa.Connection, dialect: str, table: str, column: str, fill: str) -> None:
    """Replace NULLs so the column can be tightened without failing."""
    value = _now_sql(dialect) if fill == "now" else fill
    # Identifiers come from the module-level constants above, never from input.
    conn.execute(sa.text(f'UPDATE "{table}" SET "{column}" = {value} WHERE "{column}" IS NULL'))


def _pending_not_null(inspector: sa.Inspector, table: str) -> dict[str, str]:
    """Columns of ``table`` that are still nullable and should not be."""
    present = _columns(inspector, table)
    return {
        column: fill
        for column, fill in NOT_NULL_COLUMNS.get(table, {}).items()
        if column in present and present[column]["nullable"]
    }


def _pending_default_changes(inspector: sa.Inspector, table: str) -> tuple[set[str], set[str]]:
    """(columns needing a now() default, columns needing their default dropped)."""
    present = _columns(inspector, table)
    add = {
        column
        for column in TIMESTAMP_DEFAULT_COLUMNS.get(table, ())
        if column in present and present[column].get("default") is None
    }
    drop = {
        column
        for column in DROP_DEFAULT_COLUMNS.get(table, ())
        if column in present and present[column].get("default") is not None
    }
    return add, drop


def _pending_sqlite_bigint(inspector: sa.Inspector, table: str) -> set[str]:
    present = _columns(inspector, table)
    return {
        column
        for column in SQLITE_BIGINT_COLUMNS.get(table, ())
        if column in present and str(present[column]["type"]).upper() != "BIGINT"
    }


def _delete_orphan_reactions(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Remove reactions whose message is gone, so the FK can be added.

    Migration 019 already deletes child reactions before deleting a phantom
    message, and both delete paths in the adapter do the same, so this only
    finds rows left behind by the eras before those fixes. Counts only in the
    log - never a chat id or a message id.
    """
    tables = inspector.get_table_names()
    if "reactions" not in tables or "messages" not in tables:
        return
    result = conn.execute(
        sa.text(
            "DELETE FROM reactions WHERE NOT EXISTS ("
            " SELECT 1 FROM messages m"
            " WHERE m.id = reactions.message_id AND m.chat_id = reactions.chat_id)"
        )
    )
    if result.rowcount:
        logger.info("021: deleted %d orphan reaction row(s) with no surviving message", result.rowcount)


def _drop_empty_backup_table(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Drop migration 005's rollback table, but only when it holds no rows."""
    if BACKUP_TABLE not in inspector.get_table_names():
        return
    rows = conn.execute(sa.text(f"SELECT COUNT(*) FROM {BACKUP_TABLE}")).scalar() or 0
    if rows:
        logger.info(
            "021: keeping %s - it still holds %d pre-normalization row(s) and is this database's only copy of them",
            BACKUP_TABLE,
            rows,
        )
        return
    op.drop_table(BACKUP_TABLE)


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def _upgrade_postgresql(conn: sa.Connection) -> None:
    inspector = sa.inspect(conn)

    for table in sorted(set(NOT_NULL_COLUMNS) | set(TIMESTAMP_DEFAULT_COLUMNS) | set(DROP_DEFAULT_COLUMNS)):
        present = _columns(inspector, table)
        if not present:
            continue

        for column, fill in _pending_not_null(inspector, table).items():
            _backfill_nulls(conn, "postgresql", table, column, fill)
            op.alter_column(table, column, existing_type=present[column]["type"], nullable=False)

        add_default, drop_default = _pending_default_changes(inspector, table)
        for column in sorted(add_default):
            op.alter_column(
                table,
                column,
                existing_type=present[column]["type"],
                server_default=sa.text("now()"),
            )
        for column in sorted(drop_default):
            op.alter_column(table, column, existing_type=present[column]["type"], server_default=None)

    # media.file_path: VARCHAR(500) -> TEXT. Binary-coercible, so PostgreSQL
    # does not rewrite the table.
    media_columns = _columns(inspector, "media")
    if "file_path" in media_columns and str(media_columns["file_path"]["type"]).upper() != "TEXT":
        op.alter_column("media", "file_path", existing_type=media_columns["file_path"]["type"], type_=sa.Text())

    # Retire the BIGSERIAL sequences migration 001 created for Telegram-supplied
    # primary keys. DROP DEFAULT must come first or the sequence is still
    # depended upon. The sequence name is read out of the column's own default
    # rather than assumed, and left in place if it does not parse.
    for table, column in PG_UNWANTED_SEQUENCES:
        present = _columns(inspector, table)
        if column not in present or present[column].get("default") is None:
            continue
        sequence = re.search(r"nextval\('([A-Za-z_][A-Za-z0-9_.]*)'", str(present[column]["default"]))
        op.alter_column(table, column, existing_type=present[column]["type"], server_default=None)
        if sequence:
            op.execute(f"DROP SEQUENCE IF EXISTS {sequence.group(1)}")

    inspector = sa.inspect(conn)
    if "reactions" in inspector.get_table_names() and not _fk_to_table_exists(inspector, "reactions", "messages"):
        _delete_orphan_reactions(conn, inspector)
        op.create_foreign_key(
            "fk_reaction_message",
            "reactions",
            "messages",
            ["message_id", "chat_id"],
            ["id", "chat_id"],
        )

    _drop_empty_backup_table(conn, sa.inspect(conn))


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def _named_fk_exists(inspector: sa.Inspector, table: str, referred_table: str, name: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(
        fk.get("referred_table") == referred_table and fk.get("name") == name
        for fk in inspector.get_foreign_keys(table)
    )


def _sqlite_table_needs_rebuild(inspector: sa.Inspector, table: str) -> bool:
    """True when this table's shape still differs from what the ORM builds.

    SQLite cannot alter a column in place, so every change here costs a full
    table copy. Asking per table keeps that cost off the databases that do not
    need it — a schema provisioned by ``create_all`` is already in the target
    shape for the big tables, so ``messages`` and ``media`` are not touched.
    """
    add_default, drop_default = _pending_default_changes(inspector, table)
    if _pending_not_null(inspector, table) or add_default or drop_default:
        return True
    if _pending_sqlite_bigint(inspector, table):
        return True
    if table == "media" and not _named_fk_exists(inspector, "media", "messages", "fk_media_message"):
        return True
    if table == "reactions" and not (
        _fk_to_table_exists(inspector, "reactions", "messages") and _fk_to_table_exists(inspector, "reactions", "users")
    ):
        return True
    return False


def _sqlite_media_copy_from(conn: sa.Connection) -> sa.Table:
    """Reflect ``media`` with its messages FK given the name the ORM uses.

    Migration 005's raw ``CREATE TABLE`` left that constraint unnamed on SQLite,
    and an unnamed constraint cannot be dropped by name. Handing the rebuild a
    reflected definition with the name filled in renames it without a line of
    hand-written DDL that could drift from models.py again.
    """
    reflected = sa.Table("media", sa.MetaData(), autoload_with=conn)
    for constraint in reflected.foreign_key_constraints:
        if constraint.name is None and constraint.elements[0].target_fullname.startswith("messages."):
            constraint.name = "fk_media_message"
    return reflected


def _repair_sqlite_desc_index(conn: sa.Connection) -> None:
    """Restore ``idx_messages_chat_date_desc``'s DESC ordering after a rebuild.

    SQLite reflection reports an index's columns but not their sort order, so
    any table rebuild that goes through reflection recreates this one as plain
    ASC. models.py declares it ``(chat_id, date DESC)`` for the page read
    ``WHERE chat_id = ? ORDER BY date DESC``, and the parity gate compares
    column names only - it would not catch the loss.
    """
    ddl = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_messages_chat_date_desc'")
    ).scalar()
    # Only the column list may be inspected for DESC: the index's own name ends
    # in "_desc", so testing the whole statement always reports a match.
    columns_clause = ddl[ddl.index("(") :].upper() if ddl and "(" in ddl else ""
    if not columns_clause or "DESC" in columns_clause:
        return
    op.drop_index("idx_messages_chat_date_desc", table_name="messages")
    op.create_index("idx_messages_chat_date_desc", "messages", ["chat_id", sa.text("date DESC")])


def _upgrade_sqlite(conn: sa.Connection) -> None:
    inspector = sa.inspect(conn)
    candidates = sorted(
        set(NOT_NULL_COLUMNS) | set(TIMESTAMP_DEFAULT_COLUMNS) | set(DROP_DEFAULT_COLUMNS) | set(SQLITE_BIGINT_COLUMNS)
    )

    # A reaction whose message is gone would survive the rebuild and then break
    # the FK the rebuild adds, so clean first.
    if "reactions" in inspector.get_table_names() and not _fk_to_table_exists(inspector, "reactions", "messages"):
        _delete_orphan_reactions(conn, inspector)

    for table in candidates:
        present = _columns(inspector, table)
        if not present or not _sqlite_table_needs_rebuild(inspector, table):
            continue

        pending_null = _pending_not_null(inspector, table)
        for column, fill in pending_null.items():
            _backfill_nulls(conn, "sqlite", table, column, fill)

        add_default, drop_default = _pending_default_changes(inspector, table)
        widen = _pending_sqlite_bigint(inspector, table)

        # SQLite cannot alter a column in place; batch_alter_table copies the
        # table through a temporary one. Renaming over a table that other
        # tables reference makes modern SQLite re-validate and rewrite those
        # references, so the legacy rename is used for the duration - exactly
        # the shape migration 005 already ships for this table set.
        # recreate="always": this block is only entered when the table really
        # does need rebuilding, and for `media` the only change can be a
        # constraint rename, which produces no alter_column op for batch mode to
        # trigger on.
        batch_kwargs: dict = {"recreate": "always"}
        if table == "media":
            batch_kwargs["copy_from"] = _sqlite_media_copy_from(conn)

        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        try:
            with op.batch_alter_table(table, **batch_kwargs) as batch_op:
                for column in sorted(set(pending_null) | add_default | drop_default | widen):
                    kwargs: dict = {"existing_type": present[column]["type"]}
                    if column in pending_null:
                        kwargs["nullable"] = False
                    if column in widen:
                        kwargs["type_"] = sa.BigInteger()
                    if column in add_default:
                        kwargs["server_default"] = sa.text("CURRENT_TIMESTAMP")
                    elif column in drop_default:
                        kwargs["server_default"] = None
                    batch_op.alter_column(column, **kwargs)

                if table == "media" and not _fk_to_table_exists(inspector, "media", "messages"):
                    batch_op.create_foreign_key(
                        "fk_media_message", "messages", ["message_id", "chat_id"], ["id", "chat_id"], ondelete="CASCADE"
                    )
                if table == "reactions":
                    if not _fk_to_table_exists(inspector, "reactions", "messages"):
                        batch_op.create_foreign_key(
                            "fk_reaction_message", "messages", ["message_id", "chat_id"], ["id", "chat_id"]
                        )
                    if not _fk_to_table_exists(inspector, "reactions", "users"):
                        batch_op.create_foreign_key(
                            "fk_reactions_user", "users", ["user_id"], ["id"], ondelete="SET NULL"
                        )
        finally:
            conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")

        if table == "messages":
            _repair_sqlite_desc_index(conn)

        inspector = sa.inspect(conn)

    _drop_empty_backup_table(conn, sa.inspect(conn))


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        _upgrade_postgresql(conn)
    else:
        _upgrade_sqlite(conn)


def downgrade() -> None:
    """Loosen the tightened columns again.

    The dropped ``_messages_media_backup`` table is not recreated: it only ever
    held a copy of columns migration 005 had already moved into ``media``, and
    it is dropped above only when empty.
    """
    conn = op.get_bind()
    dialect = conn.dialect.name
    inspector = sa.inspect(conn)

    if dialect == "postgresql":
        for table, columns in NOT_NULL_COLUMNS.items():
            present = _columns(inspector, table)
            for column in sorted(columns):
                if column in present and not present[column]["nullable"]:
                    op.alter_column(table, column, existing_type=present[column]["type"], nullable=True)
        # Name-based, like migration 005's downgrade: only undo the constraint
        # this migration created, never one that arrived some other way.
        if _named_fk_exists(inspector, "reactions", "messages", "fk_reaction_message"):
            op.drop_constraint("fk_reaction_message", "reactions", type_="foreignkey")
        return

    for table, columns in NOT_NULL_COLUMNS.items():
        present = _columns(inspector, table)
        targets = [column for column in sorted(columns) if column in present and not present[column]["nullable"]]
        if not targets:
            continue
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        try:
            with op.batch_alter_table(table) as batch_op:
                for column in targets:
                    batch_op.alter_column(column, existing_type=present[column]["type"], nullable=True)
        finally:
            conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        inspector = sa.inspect(conn)
