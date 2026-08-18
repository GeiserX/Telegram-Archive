"""Recreate declared indexes that a real database turned out not to have.

Found on a production archive whose viewer had become unusable: the chat list
took twenty to thirty-five minutes per request, and five of its own queries
were found running concurrently, each stacking on the last. The archive was
missing ``idx_messages_chat_date_desc`` and ``idx_messages_chat_pinned`` —
both declared in models.py since 002 and 004, both present in every other
database anyone had looked at.

``get_all_chats`` reads each chat's last message through a correlated
``MAX(messages.date)`` subquery, one seek per chat row against
``idx_messages_chat_date_desc``. Without that index each of the archive's
4,784 chats scans its own messages across a 2.7M-row table, so the cost of
listing chats becomes the cost of reading the archive. Restoring the two
indexes took that query from >20 minutes to 39 ms, measured on the archive
that reported it.

How a declared index goes missing without anything noticing:
``Base.metadata.create_all(checkfirst=True)`` — which src/db/migrate.py uses
to build the target schema when moving SQLite to PostgreSQL — skips a table
*and every index on it* when the table already exists. ``checkfirst`` is
per-table, not per-index. An index added to models.py after a database was
first created therefore never appears in it, while the migration that would
have created it is skipped too, because entrypoint.sh stamped that database
at a later revision. tests/test_schema_parity.py compares the ORM against a
freshly migrated database, so it cannot see this: the drift only exists in
databases that have lived through history.

This migration names the two indexes explicitly rather than reflecting
models.py, because a migration has to keep meaning what it meant when it was
written; a future edit to models.py must not silently change what this one
does. It creates each index only where it is absent, so it is a no-op on
every database that already has them — which is most of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "messages"

# (index name, the columns as models.py declares them). The DESC on date is
# load-bearing: the query this index serves reads
# ``WHERE chat_id = ? ORDER BY date DESC``, and SQLite reflection cannot see
# sort order, so a rebuild that goes through reflection silently downgrades it
# to ASC (see 021's _repair_sqlite_desc_index).
MISSING_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("idx_messages_chat_date_desc", ("chat_id", "date DESC")),
    ("idx_messages_chat_pinned", ("chat_id", "is_pinned")),
)


def _live_indexes(inspector: sa.Inspector) -> set[str]:
    if TABLE_NAME not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    live = _live_indexes(sa.inspect(conn))
    if TABLE_NAME not in sa.inspect(conn).get_table_names():
        return

    created = 0
    for name, columns in MISSING_INDEXES:
        if name in live:
            continue
        op.create_index(
            name,
            TABLE_NAME,
            [sa.text(column) if " " in column else column for column in columns],
        )
        created += 1

    if created:
        # Names only — an index name is schema, not archive content.
        print(f"024: recreated {created} declared index(es) missing from this database")


def downgrade() -> None:
    """Nothing to undo.

    These indexes are declared in models.py, so a database that has them is
    the correct state at every revision; dropping them on the way down would
    reintroduce the fault this migration exists to repair.
    """
