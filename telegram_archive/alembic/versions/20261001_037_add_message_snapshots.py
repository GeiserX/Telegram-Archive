"""Follow a poll's votes and closing, and a link preview's later state.

A poll and a link preview live in ``raw_data`` as first captured. Later votes,
a poll's closing and a preview Telegram changes were never kept, and a backup
that read the message again replaced ``raw_data`` with what it saw.

``message_snapshots`` is append-only. When a read shows a poll or a preview in
another state than the newest one kept, a row is added with the whole state as
JSON, when the archive saw it and the path that saw it. ``raw_data`` keeps the
first capture. Live locations are not followed.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the table, and every step is guarded by the inspector.

Revision ID: 037
Revises: 036
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "message_snapshots"
INDEX_NAME = "ix_message_snapshots_message"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "messages" not in tables:
        return

    if TABLE_NAME not in tables:
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["account_id", "message_id", "chat_id"],
                ["messages.account_id", "messages.id", "messages.chat_id"],
                name="fk_message_snapshots_message",
                ondelete="CASCADE",
            ),
        )
        inspector = sa.inspect(conn)

    if INDEX_NAME not in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}:
        op.create_index(INDEX_NAME, TABLE_NAME, ["account_id", "chat_id", "message_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME in inspector.get_table_names():
        if INDEX_NAME in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}:
            op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
        op.drop_table(TABLE_NAME)
