"""Migration 039: stickers filed under another type become stickers.

The upgrade runs from the revision before to head on SQLite and on PostgreSQL.
A video sticker stored as ``video`` and an animated sticker stored as
``document`` are re-typed, in ``media`` and in the earlier media of an edited
message (``media_versions``). An ordinary webm video, a webm named like a sticker
but too wide, one too long, and an ordinary document stay as they were. Ids,
paths and the downloaded flag never change, and a second run changes nothing.

The revision ids are read from the migration module, so renumbering the chain
at merge time changes only the migration file.
"""

import importlib.util
import re
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import NO_POSTGRES_REASON
from test_schema_parity import _build_alembic_schema

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"
SENT = datetime(2026, 1, 1, 9, 0, 0)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("migration_retype_stickers", "20261003_039_retype_archived_stickers.py")

# (media id, message id, type, file name, mime type, width, height, duration, expected type after)
ROWS = [
    ("-1001_1_video", 1, "video", "5550001_sticker.webm", "video/webm", 512, 512, 3, "sticker"),
    ("-1001_2_video", 2, "video", "5550002_sticker.webm", None, 512, 384, 2, "sticker"),
    ("-1001_3_document", 3, "document", "5550003_AnimatedSticker.tgs", None, None, None, None, "sticker"),
    ("-1001_4_video", 4, "video", "5550004_clip.webm", "video/webm", 512, 512, 3, "video"),
    ("-1001_5_video", 5, "video", "5550005_sticker.webm", "video/webm", 1280, 720, 3, "video"),
    ("-1001_6_video", 6, "video", "5550006_sticker.webm", "video/webm", 512, 512, 10, "video"),
    ("-1001_7_document", 7, "document", "5550007_report.pdf", "application/pdf", None, None, None, "document"),
    ("-1001_8_video", 8, "video", "5550008xsticker.webm", "video/webm", 512, 512, 3, "video"),
    ("-1001_9_sticker", 9, "sticker", "5550009_sticker.webp", "image/webp", 512, 512, None, "sticker"),
    # A video with stickers drawn on it, filed as a sticker by an older release: the row cannot tell a
    # video from a GIF, so it stays for a backup that reads the message again.
    ("-1001_10_sticker", 10, "sticker", "5550010_funny.mp4", "video/mp4", 640, 360, 8, "sticker"),
]

# Earlier media of an edited message: (media id, message id, type, file name, mime type, width, height,
# duration, expected type after). The edit history must name a sticker as the current row does.
VERSION_ROWS = [
    ("-1001_1_video_old", 1, "video", "5550101_sticker.webm", "video/webm", 512, 512, 3, "sticker"),
    ("-1001_3_document_old", 3, "document", "5550103_AnimatedSticker.tgs", None, None, None, None, "sticker"),
    ("-1001_4_video_old", 4, "video", "5550104_clip.webm", "video/webm", 512, 512, 3, "video"),
    ("-1001_5_video_old", 5, "video", "5550105_sticker.webm", "video/webm", 1280, 720, 3, "video"),
]


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _snapshot(conn):
    return conn.execute(
        sa.text("SELECT id, type, file_path, file_name, downloaded FROM media ORDER BY message_id")
    ).all()


def _version_snapshot(conn):
    return conn.execute(
        sa.text("SELECT media_id, type, file_path, file_name, downloaded FROM media_versions ORDER BY media_id")
    ).all()


def test_revision_chain_points_at_an_existing_revision_and_is_the_head():
    revisions = {}
    for path in _VERSIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision: str = "([^"]+)"', source, re.M)
        down = re.search(r'^down_revision: str \| None = "([^"]+)"', source, re.M)
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
    assert migration.down_revision in revisions
    assert migration.revision not in revisions.values()


def test_upgrade_without_the_media_table_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        _run(conn, migration.downgrade)
        assert sa.inspect(conn).get_table_names() == []


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_039")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_retypes_only_stickers_and_is_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, migration.down_revision)

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0039', 'group', 0)"
                )
            )
            for row in ROWS:
                conn.execute(
                    sa.text(
                        "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned, "
                        "is_deleted) VALUES (1, :mid, -1001, :sent, '', 0, 0, 0)"
                    ),
                    {"mid": row[1], "sent": str(SENT)},
                )
            for media_id, message_id, media_type, name, mime, width, height, duration, _ in ROWS:
                conn.execute(
                    sa.text(
                        "INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name, "
                        "mime_type, width, height, duration, downloaded) VALUES (1, :id, :mid, -1001, :type, "
                        ":path, :name, :mime, :w, :h, :d, 1)"
                    ),
                    {
                        "id": media_id,
                        "mid": message_id,
                        "type": media_type,
                        "path": f"/data/backups/media/-1001/{name}",
                        "name": name,
                        "mime": mime,
                        "w": width,
                        "h": height,
                        "d": duration,
                    },
                )
            for media_id, message_id, media_type, name, mime, width, height, duration, _ in VERSION_ROWS:
                conn.execute(
                    sa.text(
                        "INSERT INTO media_versions (account_id, chat_id, message_id, media_id, type, file_path, "
                        "file_name, mime_type, width, height, duration, downloaded, date, captured_at) VALUES "
                        "(1, -1001, :mid, :id, :type, :path, :name, :mime, :w, :h, :d, 1, :sent, :sent)"
                    ),
                    {
                        "id": media_id,
                        "mid": message_id,
                        "type": media_type,
                        "path": f"/data/backups/media/-1001/{name}",
                        "name": name,
                        "mime": mime,
                        "w": width,
                        "h": height,
                        "d": duration,
                        "sent": str(SENT),
                    },
                )
            before = _snapshot(conn)
            versions_before = _version_snapshot(conn)

        _build_alembic_schema(async_url, "head")

        with engine.begin() as conn:
            after = _snapshot(conn)
            assert [row.type for row in after] == [row[-1] for row in ROWS]
            # Only the type column moved.
            assert [(r.id, r.file_path, r.file_name, r.downloaded) for r in after] == [
                (r.id, r.file_path, r.file_name, r.downloaded) for r in before
            ]
            versions_after = _version_snapshot(conn)
            expected = {row[0]: row[-1] for row in VERSION_ROWS}
            assert {r.media_id: r.type for r in versions_after} == expected
            assert [(r.media_id, r.file_path, r.file_name, r.downloaded) for r in versions_after] == [
                (r.media_id, r.file_path, r.file_name, r.downloaded) for r in versions_before
            ]
            _run(conn, migration.upgrade)
            assert _snapshot(conn) == after
            assert _version_snapshot(conn) == versions_after
            _run(conn, migration.downgrade)
            assert _snapshot(conn) == after
            assert _version_snapshot(conn) == versions_after
    finally:
        engine.dispose()
