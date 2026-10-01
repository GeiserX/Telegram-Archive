"""Keep Telegram's edit_hide flag beside each message's edit_date.

Telegram bumps a message's ``edit_date`` when only its reactions change, and
sets the message's ``edit_hide`` flag to say the edit must not be shown. The
archive stored the date and dropped the flag, so a message first captured after
a reaction showed as edited with no earlier text. ``messages.edit_hide`` keeps
the flag: 1 hidden, 0 shown, NULL for rows from before this column, which read
as shown, as before.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the column, and the step is guarded by the inspector.

Revision ID: 034
Revises: 033
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "messages"
COLUMN = "edit_hide"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE_NAME)}:
        op.add_column(TABLE_NAME, sa.Column(COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE_NAME)}:
        op.drop_column(TABLE_NAME, COLUMN)
