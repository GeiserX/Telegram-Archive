"""Fix stale positive-ID folder prefixes in media.file_path.

Before v4.0.5, the backup stored media in directories named with raw (positive)
entity IDs. After v4.0.5, directories use Telethon's marked IDs (negative for
groups/channels). The v4.0.6 migration fixed chat_id columns but never updated
file_path strings, leaving ~37% of media rows with paths pointing to the wrong
folder name.

This migration rewrites file_path values so the folder component matches the
marked chat_id already stored in media.chat_id.

Revision ID: 013
Revises: 012
Create Date: 2026-05-24
"""

import sqlalchemy as sa

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

_BATCH_SIZE = 5000


def _derive_stale_folder(chat_id: int) -> str | None:
    """Derive the old positive folder name from a marked chat_id.

    Basic groups: chat_id = -X  →  old folder = "X"
    Channels:     chat_id = -(1000000000000 + X)  →  old folder = "X"
    Users:        chat_id > 0  →  no mismatch possible, return None
    """
    if chat_id >= 0:
        return None
    raw = -chat_id
    if raw > 1000000000000:
        return str(raw - 1000000000000)
    return str(raw)


def upgrade():
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "postgresql":
        # Get all distinct negative chat_ids that have media
        result = conn.execute(sa.text("SELECT DISTINCT chat_id FROM media WHERE chat_id < 0 AND file_path IS NOT NULL"))
        chat_ids = [row[0] for row in result]

        for chat_id in chat_ids:
            stale_folder = _derive_stale_folder(chat_id)
            if stale_folder is None:
                continue
            correct_folder = str(chat_id)

            # Only update rows where file_path contains the stale folder
            # Use pattern: .../<stale_folder>/... → .../<correct_folder>/...
            stale_pattern = f"%/{stale_folder}/%"
            conn.execute(
                sa.text(
                    "UPDATE media SET file_path = REPLACE(file_path, :old_seg, :new_seg) "
                    "WHERE chat_id = :cid AND file_path LIKE :pattern"
                ),
                {
                    "old_seg": f"/{stale_folder}/",
                    "new_seg": f"/{correct_folder}/",
                    "cid": chat_id,
                    "pattern": stale_pattern,
                },
            )

    elif dialect == "sqlite":
        result = conn.execute(sa.text("SELECT DISTINCT chat_id FROM media WHERE chat_id < 0 AND file_path IS NOT NULL"))
        chat_ids = [row[0] for row in result]

        for chat_id in chat_ids:
            stale_folder = _derive_stale_folder(chat_id)
            if stale_folder is None:
                continue
            correct_folder = str(chat_id)

            stale_pattern = f"%/{stale_folder}/%"
            conn.execute(
                sa.text(
                    "UPDATE media SET file_path = REPLACE(file_path, :old_seg, :new_seg) "
                    "WHERE chat_id = :cid AND file_path LIKE :pattern"
                ),
                {
                    "old_seg": f"/{stale_folder}/",
                    "new_seg": f"/{correct_folder}/",
                    "cid": chat_id,
                    "pattern": stale_pattern,
                },
            )


def downgrade():
    # Reversible: swap the folder components back
    conn = op.get_bind()
    dialect = conn.dialect.name

    result = conn.execute(sa.text("SELECT DISTINCT chat_id FROM media WHERE chat_id < 0 AND file_path IS NOT NULL"))
    chat_ids = [row[0] for row in result]

    for chat_id in chat_ids:
        stale_folder = _derive_stale_folder(chat_id)
        if stale_folder is None:
            continue
        correct_folder = str(chat_id)
        pattern = f"%/{correct_folder}/%"

        conn.execute(
            sa.text(
                "UPDATE media SET file_path = REPLACE(file_path, :new_seg, :old_seg) "
                "WHERE chat_id = :cid AND file_path LIKE :pattern"
            ),
            {
                "new_seg": f"/{correct_folder}/",
                "old_seg": f"/{stale_folder}/",
                "cid": chat_id,
                "pattern": pattern,
            },
        )
