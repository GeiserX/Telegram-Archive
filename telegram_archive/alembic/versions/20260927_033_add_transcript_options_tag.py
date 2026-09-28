"""Record the options each transcript was asked for (docs/TRANSCRIPTION.md).

``media_transcripts.options_tag`` holds 12 hex characters naming the options
a row's request carried: on akou's job path the preset, language hint and
diarize flag that also name its Idempotency-Key; on the synchronous path and
the provider adapters the provider, model, language hint, diarize flag and
hotword prompt. The copy rule reuses a done row only when its tag equals the
one the drain would send, so an answer made with other options is never
copied. Rows from before the column keep NULL and are never copied.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the column, and the step is guarded by the inspector.

Revision ID: 033
Revises: 032
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media_transcripts"
COLUMN = "options_tag"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE_NAME)}:
        op.add_column(TABLE_NAME, sa.Column(COLUMN, sa.String(16), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE_NAME)}:
        op.drop_column(TABLE_NAME, COLUMN)
