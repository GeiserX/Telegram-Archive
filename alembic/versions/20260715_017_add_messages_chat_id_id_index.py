"""Add (chat_id, id) index for jump-window cursors (#213).

The lone before_id / after_id cursor paths bound Message.id within a chat;
without this index the planner post-filters every row of the chat and sorts
in a temp B-tree (the composite PK leads with id, so it can't serve the seek).

Revision ID: 017
Revises: 016
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "messages"
INDEX_NAME = "idx_messages_chat_id_id"


def _index_exists(inspector: sa.Inspector) -> bool:
    return INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Idempotent: create_all()-provisioned databases may already have the index.
    if TABLE_NAME in inspector.get_table_names() and not _index_exists(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, ["chat_id", "id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME in inspector.get_table_names() and _index_exists(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
