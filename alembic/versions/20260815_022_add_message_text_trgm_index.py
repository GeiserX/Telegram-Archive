"""Add a pg_trgm GIN index on messages.text for the viewer's search (PostgreSQL).

get_messages_paginated / get_all_chats' search filters both use
``Message.text.ILIKE('%term%')`` (leading wildcard - substring search, not a
prefix match). A plain B-tree index can't serve that pattern at all, so every
search was a sequential scan of the whole messages table, with cost growing
linearly as the archive grows. Verified on a live ~100k-row instance:

    EXPLAIN ANALYZE SELECT id FROM messages WHERE text ILIKE '%test%' LIMIT 20;

    Before: Seq Scan on messages (actual time=4.556..36.063, Rows Removed by
            Filter: 6080) - Execution Time: 41.764 ms
    After:  Bitmap Heap Scan on messages using idx_messages_text_trgm
            (actual time=0.147..0.368) - Execution Time: 0.438 ms

~95x faster on this dataset, and critically the scan cost moves from O(table
size) to roughly O(matching rows) - a sequential scan degrades linearly as
more messages are backed up, while the trigram index does not.

The index is created on every dialect (same name/column, so ORM- and
Alembic-built schemas agree per tests/test_schema_parity.py), but only
PostgreSQL gets the GIN/pg_trgm treatment that actually makes it useful.
SQLite has no pg_trgm equivalent, so it gets a plain B-tree there instead -
harmless, if unused for this particular query pattern, and matches what
``postgresql_using``/``postgresql_ops`` (dialect-scoped kwargs on the
models.py Index()) already produce via ``Base.metadata.create_all()``:
SQLAlchemy silently drops PostgreSQL-only kwargs on every other dialect
rather than erroring.

No CONCURRENTLY: this project's existing index migrations run inside
Alembic's normal transactional DDL (see #213/017), so this one does too for
consistency. On an already-large `messages` table this will briefly hold a
lock for the duration of the index build - acceptable for the same reason
prior structural changes to this table were: migrations run once, and this
one is asymptotically the same cost class as REINDEXing an existing index.

Revision ID: 022
Revises: 021
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "messages"
INDEX_NAME = "idx_messages_text_trgm"


def _index_exists(inspector: sa.Inspector) -> bool:
    return INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    is_postgresql = dialect == "postgresql"

    if is_postgresql:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    inspector = sa.inspect(conn)
    # Idempotent: create_all()-provisioned databases may already have the index.
    if TABLE_NAME in inspector.get_table_names() and not _index_exists(inspector):
        # Same index name/column on every dialect (matches what models.py's
        # Index() produces via create_all(), so ORM- and Alembic-built
        # schemas agree - see tests/test_schema_parity.py). Only PostgreSQL
        # gets the GIN/pg_trgm treatment that makes it actually fast;
        # postgresql_using/postgresql_ops are dialect-scoped kwargs that
        # SQLAlchemy silently drops on every other dialect, producing a
        # harmless, unused plain B-tree there instead.
        kwargs = {"postgresql_using": "gin", "postgresql_ops": {"text": "gin_trgm_ops"}} if is_postgresql else {}
        op.create_index(INDEX_NAME, TABLE_NAME, ["text"], **kwargs)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME in inspector.get_table_names() and _index_exists(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    # pg_trgm extension is left installed on PostgreSQL - it's harmless and
    # other indexes (e.g. a future chats.title/first_name search index) may
    # want it too.
