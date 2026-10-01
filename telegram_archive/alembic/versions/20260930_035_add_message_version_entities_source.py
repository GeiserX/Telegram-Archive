"""Keep each version's formatting and the path that saw it.

``message_versions`` kept only the plain text of an earlier version, so an
edit replaced the old formatting in ``raw_data`` and an edit that changed only
the formatting left nothing behind. ``entities`` keeps the version's
formatting as a JSON list, in the shape of ``raw_data["entities"]``.
``rich_message`` keeps a Rich Text Editor message's block tree (#470) as JSON,
in the shape of ``raw_data["rich_message"]``: the rest of the version's
formatting, which ``entities`` flattens.

``source`` names the path that wrote the row: ``listener``, ``sync``,
``backup`` or ``import``. The listener sees each edit; the other paths read
the text current at that moment, so several edits between two reads arrive as
one. The viewer uses it to say when the count of edits is a lower bound.

All three columns are nullable and rows from before this migration keep NULL:
unknown, never a guess.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the columns, and every step is guarded by the inspector.

Revision ID: 035
Revises: 034
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "message_versions"


def _columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("entities", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("rich_message", sa.Text(), nullable=True),
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns(TABLE_NAME)}
    for column in _columns():
        if column.name not in existing:
            op.add_column(TABLE_NAME, column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns(TABLE_NAME)}
    for column in reversed(_columns()):
        if column.name in existing:
            op.drop_column(TABLE_NAME, column.name)
