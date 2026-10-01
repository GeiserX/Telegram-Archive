"""Keep every observed state of a message's reactions.

``reactions`` holds one row per emoji with its current count. When a count
dropped without reaching zero, say from 7 to 5, the new count was written over
the old one, and when an emoji taken back was given again its ``removed_at``
tombstone was cleared. Both earlier states were lost.

``reaction_history`` is append-only: one row per state the archive observed of
one emoji on one message (count, the count before it, when, and which path saw
it). ``count`` 0 means taken back completely. ``reconcile_reactions`` adds a row
whenever the count differs from the newest row kept for that emoji.

The upgrade seeds the history from ``reactions`` with source "baseline": one row
per emoji with the last count that table held, dated when the archive first saw
the emoji, and for an emoji taken back a second row with count 0, dated by its
tombstone. A message whose emoji already has history rows is left alone, so a
re-run adds nothing.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the table, and every step is guarded by the inspector.

Revision ID: 037
Revises: 036
Create Date: 2026-10-01
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# The only place the chain is named: renumbering this migration is these two lines.
revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "reaction_history"
MESSAGE_INDEX = "ix_reaction_history_message"
TAKEN_BACK_INDEX = "ix_reaction_history_taken_back"
TAKEN_BACK_WHERE = sa.text("count < previous_count")

# The count an emoji's rows add up to: the live rows when any is live, else every
# row (the count it had when it went). A tombstone without a positive count reads
# as one, as the page read has always shown it.
_GROUP_COUNT = """
    CASE WHEN SUM(CASE WHEN r.removed_at IS NULL THEN 1 ELSE 0 END) > 0
         THEN SUM(CASE WHEN r.removed_at IS NULL THEN r.count ELSE 0 END)
         ELSE SUM(CASE WHEN r.count > 0 THEN r.count ELSE 1 END)
    END
"""
_SAME_EMOJI = (
    "h.account_id = r.account_id AND h.chat_id = r.chat_id AND h.message_id = r.message_id AND h.emoji = r.emoji"
)

# One baseline row per emoji that has no history yet.
SEED_BASELINE = f"""
INSERT INTO reaction_history (account_id, chat_id, message_id, emoji, count, previous_count, observed_at, source)
SELECT r.account_id, r.chat_id, r.message_id, r.emoji,
       CASE WHEN {_GROUP_COUNT} > 0 THEN {_GROUP_COUNT} ELSE 1 END,
       NULL,
       MIN(COALESCE(r.created_at, r.removed_at, :now)),
       'baseline'
FROM reactions r
WHERE NOT EXISTS (SELECT 1 FROM reaction_history h WHERE {_SAME_EMOJI})
GROUP BY r.account_id, r.chat_id, r.message_id, r.emoji
"""

# Then, for an emoji every row of which is a tombstone, the removal: count 0
# after the baseline count, dated by the latest tombstone. Only where the
# emoji's history is that lone baseline row, so a re-run adds nothing.
SEED_REMOVAL = f"""
INSERT INTO reaction_history (account_id, chat_id, message_id, emoji, count, previous_count, observed_at, source)
SELECT r.account_id, r.chat_id, r.message_id, r.emoji,
       0,
       CASE WHEN {_GROUP_COUNT} > 0 THEN {_GROUP_COUNT} ELSE 1 END,
       MAX(r.removed_at),
       'baseline'
FROM reactions r
WHERE NOT EXISTS (
    SELECT 1 FROM reaction_history h
    WHERE {_SAME_EMOJI} AND (h.source IS NULL OR h.source <> 'baseline' OR h.count = 0)
)
GROUP BY r.account_id, r.chat_id, r.message_id, r.emoji
HAVING SUM(CASE WHEN r.removed_at IS NULL THEN 1 ELSE 0 END) = 0
"""


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
            sa.Column("emoji", sa.String(length=50), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False),
            sa.Column("previous_count", sa.Integer(), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["account_id", "message_id", "chat_id"],
                ["messages.account_id", "messages.id", "messages.chat_id"],
                name="fk_reaction_history_message",
                ondelete="CASCADE",
            ),
        )
        inspector = sa.inspect(conn)

    index_names = {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    if MESSAGE_INDEX not in index_names:
        op.create_index(MESSAGE_INDEX, TABLE_NAME, ["account_id", "chat_id", "message_id"])
    if TAKEN_BACK_INDEX not in index_names:
        op.create_index(
            TAKEN_BACK_INDEX,
            TABLE_NAME,
            ["observed_at"],
            sqlite_where=TAKEN_BACK_WHERE,
            postgresql_where=TAKEN_BACK_WHERE,
        )

    if "reactions" in tables:
        now = sa.bindparam("now", value=datetime.now(UTC).replace(tzinfo=None), type_=sa.DateTime())
        conn.execute(sa.text(SEED_BASELINE).bindparams(now))
        conn.execute(sa.text(SEED_REMOVAL))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    index_names = {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    for name in (TAKEN_BACK_INDEX, MESSAGE_INDEX):
        if name in index_names:
            op.drop_index(name, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
