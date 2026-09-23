"""Record which profile photo each account sees for a chat.

Avatar files live in one folder shared by every account, named
``{chat_id}_{photo_id}.jpg``, and the viewer served whichever was modified
last. Two accounts can see different photos for the same user — a photo one
account set for a contact is visible to that account only — so adding a
second account made the viewer flip to the other account's photo as soon as
it downloaded its own. ``chats`` is already keyed per account, so the photo
id each account sees goes on its own row and the viewer prefers that file.

Nullable, no backfill: the next backup run fills it, and until then the
viewer keeps the newest-file behaviour. Idempotent — the entrypoint stamping
ladder tops out at 018 and a create_all() database already has the column.

Revision ID: 029
Revises: 028
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "chats"
COLUMN_NAME = "avatar_photo_id"


def _has_column(inspector: sa.Inspector) -> bool | None:
    """Whether chats has the column; None when there is no chats table at all."""
    if TABLE_NAME not in inspector.get_table_names():
        return None
    return COLUMN_NAME in {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())) is False:
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.BigInteger(), nullable=True))


def downgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
