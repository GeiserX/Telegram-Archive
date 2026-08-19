"""Drop idx_messages_chat_id - a strict prefix of two later composite indexes.

``idx_messages_chat_id`` (chat_id) has existed since 001 and, unlike every
other index on this table, carries no comment explaining what it serves.
Every access path that filters ``Message.chat_id`` on its own also orders or
bounds the result (by date, by id, or by is_pinned) and so already seeks
through ``idx_messages_chat_id_id`` (chat_id, id, added 017) or
``idx_messages_chat_date_desc`` (chat_id, date DESC, added 005) - both are a
superset of this index's leading column, so the planner can use either for a
bare chat_id predicate too. Checked every ``Message.chat_id ==`` /
``Message.chat_id.__eq__`` site in src/db/adapter.py (get_messages_paginated,
get_pinned_messages, toggle_pinned, delete_chat_messages, the per-day
export branches): none of them stop at chat_id alone without also ordering
or bounding by date/id/is_pinned.

Confirmed via ``pg_stat_user_indexes`` on a live ~385k-row archive:
``idx_messages_chat_id`` sat at 0 scans while ``idx_messages_chat_id_id`` and
``idx_messages_chat_date_desc`` carried hundreds of thousands between them -
the planner was already skipping the narrower index in favor of the
composites. Dropping it removes 3.2MB of on-disk index and one write on
every message insert, with no query plan depending on it.

Named explicitly rather than reflecting models.py, matching 024's rationale:
a migration has to keep meaning what it meant when it was written.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "messages"
INDEX_NAME = "idx_messages_chat_id"


def _index_exists(inspector: sa.Inspector) -> bool:
    if TABLE_NAME not in inspector.get_table_names():
        return False
    return INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    if _index_exists(sa.inspect(conn)):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME in inspector.get_table_names() and not _index_exists(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, ["chat_id"])
