"""Name every custom emoji the archive saw, so the viewer can draw it.

A reaction made with a custom (premium) emoji is stored as
``custom_<document_id>``, and a custom emoji in message text keeps its
``document_id`` in the entities. Nothing fetched the emoji itself, so the
viewer drew a placeholder for the reaction and the fallback character in text.

``custom_emoji`` holds one row per document id, for every chat and account:
the file is the same public sticker-set file for everyone. The backup fetches
the pending rows and writes each file once under ``media/_emoji``.

The upgrade adds a pending row for every custom emoji already stored as a
reaction (``reactions`` and ``reaction_history``), dated when the archive first
saw it, so reactions kept before this release are drawn after the next backup.
The ids are read in Python, so a stored value that is not ``custom_`` and
digits adds nothing. No other table is changed.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the table, every step is guarded by the inspector, and
the seed inserts with ON CONFLICT DO NOTHING.

Revision ID: 040
Revises: 039
Create Date: 2026-10-04
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "custom_emoji"

# The value normalize_reaction_emoji stores for a custom emoji. A document id is
# a signed 64-bit integer, so at most 19 digits.
CUSTOM_REACTION = re.compile(r"^custom_(\d{1,19})$")
MAX_DOCUMENT_ID = 2**63 - 1

# (table, the column that dates the row)
SEED_SOURCES = (("reactions", "created_at"), ("reaction_history", "observed_at"))

SEED_INSERT = sa.text(
    "INSERT INTO custom_emoji (document_id, first_seen, downloaded, attempts, text_color) "
    "VALUES (:document_id, :first_seen, 0, 0, 0) ON CONFLICT (document_id) DO NOTHING"
).bindparams(sa.bindparam("first_seen", type_=sa.DateTime()))


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def seen_custom_emoji(conn, tables: set[str]) -> dict[int, datetime]:
    """{document id: when first seen} of every custom emoji stored as a reaction."""
    now = datetime.now(UTC).replace(tzinfo=None)
    seen: dict[int, datetime] = {}
    for table, column in SEED_SOURCES:
        if table not in tables:
            continue
        rows = conn.execute(
            sa.text(f"SELECT emoji, MIN({column}) FROM {table} WHERE emoji LIKE 'custom_%' GROUP BY emoji")
        ).all()
        for emoji, first in rows:
            match = CUSTOM_REACTION.match(emoji or "")
            if match is None:
                continue
            document_id = int(match[1])
            if document_id > MAX_DOCUMENT_ID:
                continue
            when = _as_datetime(first) or now
            if document_id not in seen or when < seen[document_id]:
                seen[document_id] = when
    return seen


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    if TABLE_NAME not in tables:
        op.create_table(
            TABLE_NAME,
            sa.Column("document_id", sa.BigInteger(), autoincrement=False, nullable=False),
            sa.Column("file_name", sa.String(length=64), nullable=True),
            sa.Column("mime_type", sa.String(length=100), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("alt", sa.String(length=64), nullable=True),
            sa.Column("text_color", sa.Integer(), server_default="0", nullable=False),
            sa.Column("downloaded", sa.Integer(), server_default="0", nullable=False),
            sa.Column("skip_reason", sa.String(length=16), nullable=True),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("first_seen", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("download_date", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("document_id"),
        )

    seen = seen_custom_emoji(conn, tables)
    if seen:
        conn.execute(SEED_INSERT, [{"document_id": key, "first_seen": when} for key, when in sorted(seen.items())])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME in inspector.get_table_names():
        op.drop_table(TABLE_NAME)
