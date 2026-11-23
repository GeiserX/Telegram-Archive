"""Record when a Telegram account was seen deleted, on chats and users.

When a Telegram account is deleted, the next snapshot of it carries no names.
Since #636 the archive keeps the names it had, which is right for an archive,
but nothing recorded that the account was deleted, so the viewer showed the
kept name as a live contact. ``account_deleted_at`` is the time the archive
first saw Telethon report the account deleted. It is set once, never cleared
and never moved.

Nullable, no backfill: a row whose names a deleted snapshot already erased
still reads as deleted in the viewer from its empty names, and the next backup
or listener update sets the column for every deleted account it sees.

Idempotent in both directions: a create_all() database already has the columns
from the model declarations, and the entrypoint stamping ladder stops at 018,
so every later migration may meet a schema that already holds its objects.

Revision ID: 043
Revises: 042
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN_NAME = "account_deleted_at"
TABLES: tuple[str, ...] = ("chats", "users")


def _tables_with_column(inspector: sa.Inspector) -> dict[str, bool]:
    """For each existing table in TABLES, whether it already has the column."""
    present = set(inspector.get_table_names())
    return {
        table: COLUMN_NAME in {c["name"] for c in inspector.get_columns(table)} for table in TABLES if table in present
    }


def upgrade() -> None:
    for table, has_column in _tables_with_column(sa.inspect(op.get_bind())).items():
        if not has_column:
            op.add_column(table, sa.Column(COLUMN_NAME, sa.DateTime(), nullable=True))


def downgrade() -> None:
    for table, has_column in _tables_with_column(sa.inspect(op.get_bind())).items():
        if has_column:
            op.drop_column(table, COLUMN_NAME)
