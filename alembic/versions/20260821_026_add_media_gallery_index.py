"""Add idx_media_gallery so a gallery page stops sorting the whole chat.

GET /api/chats/{chat_id}/media orders by the media keyset pair and filters on
(chat_id, downloaded), but no index carried both the filter and an ordering
column: measured on a chat holding 120,000 downloaded media rows, every page
fetched, joined and sorted all of them to return 51 (43 ms first page, 494 ms
near the oldest row, linear in chat media count). This composite covers the
filter, the cursor predicate and the ORDER BY in one seek, so a page costs
O(page size) after the cursor.

Named explicitly rather than reflecting models.py, matching 024/025's
rationale: a migration has to keep meaning what it meant when it was written.
Guarded both directions — the entrypoint stamping ladder tops out at 018 and
relies on every later migration being idempotent, and a create_all() database
already has this index from the model declaration.

Revision ID: 026
Revises: 025
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "026"
down_revision: str | None = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media"
INDEX_NAME = "idx_media_gallery"
COLUMNS = ("chat_id", "downloaded", "message_id", "id")


def _live_indexes(inspector: sa.Inspector) -> set[str]:
    if TABLE_NAME not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    if INDEX_NAME in _live_indexes(inspector):
        return
    op.create_index(INDEX_NAME, TABLE_NAME, list(COLUMNS))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if INDEX_NAME not in _live_indexes(inspector):
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
