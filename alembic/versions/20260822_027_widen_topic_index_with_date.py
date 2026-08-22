"""Widen idx_messages_topic with date so the topic sidebar stops touching the heap.

GET /api/chats/{chat_id}/topics aggregates count and max(date) per topic over
every message row of the chat. Grouped on coalesce(reply_to_top_id, 1) the
planner needed a temp b-tree and a heap lookup per row (raw_data included);
grouped on the raw column with this index carrying date, the whole aggregate
is a covering index scan: measured 17.4 ms -> 2.4 ms at 60k rows on SQLite,
with the NULL bucket folded into the General topic in Python instead of SQL.

The index keeps its name across the widening, so idempotency is keyed on the
COLUMN LIST, not existence: a create_all() database already has the 3-column
shape from the model declaration, an upgraded database has the 2-column one.
Guarded both directions — the entrypoint stamping ladder tops out at 018 and
relies on every later migration being idempotent.

Revision ID: 027
Revises: 026
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "messages"
INDEX_NAME = "idx_messages_topic"
NEW_COLUMNS = ["chat_id", "reply_to_top_id", "date"]
OLD_COLUMNS = ["chat_id", "reply_to_top_id"]


def _index_columns(inspector: sa.Inspector) -> list[str] | None:
    """The live column list of idx_messages_topic, [] if absent, None if no table."""
    if TABLE_NAME not in inspector.get_table_names():
        return None
    for index in inspector.get_indexes(TABLE_NAME):
        if index["name"] == INDEX_NAME:
            return list(index["column_names"])
    return []


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = _index_columns(inspector)
    if columns is None or columns == NEW_COLUMNS:
        return
    if columns:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(INDEX_NAME, TABLE_NAME, NEW_COLUMNS)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = _index_columns(inspector)
    if columns is None or columns == OLD_COLUMNS:
        return
    if columns:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_index(INDEX_NAME, TABLE_NAME, OLD_COLUMNS)
