"""``telegram-archive merge``: one archive's accounts copied into another.

The archives here are real: the schema comes from this tree's migrations and
the rows from the real adapter, the way the backup writes them. The merge then
runs against the files, and the assertions read the target back with plain SQL.

Every name, number and byte below is fake.
"""

import asyncio
import atexit
import contextlib
import functools
import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig

from telegram_archive import merge
from telegram_archive.__main__ import create_parser, main
from telegram_archive.db.adapter import DatabaseAdapter
from telegram_archive.db.base import DatabaseManager
from telegram_archive.db.models import MediaTranscript, MediaVersion, MessageVersion, Reaction, ReactionHistory

REPO_ROOT = Path(__file__).resolve().parent.parent

CHANNEL = -1001000000001  # a channel both archives hold
FORUM = -1001000000002
PRIVATE = 700000002  # a private chat's id is the other party's user id
SHARED_USER = 700000001
NEW_USER = 700000002

SOURCE_USER_A = 900000001
SOURCE_USER_B = 900000002
TARGET_USER = 900000009

PHOTO_BYTES = b"fake photo bytes, the same in both archives"
DOC_BYTES = b"fake document bytes, only in the source"
PLAIN_BYTES = b"fake plain file bytes"
VOICE_BYTES = b"fake voice note bytes"
EARLIER_PHOTO_BYTES = b"fake photo bytes an edit replaced"
AVATAR_BYTES = b"fake avatar bytes"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upgrade_to_head(url: str) -> None:
    """This tree's real migrations, without the ini file (its fileConfig would reset pytest's logging)."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(REPO_ROOT / "telegram_archive" / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("path_separator", "os")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_blob(media: Path, chat_id: int, name: str, data: bytes, blob_name: str | None = None) -> Path:
    """A ``_shared`` blob and the chat-folder symlink to it, the way the backup lays them out."""
    digest = sha(data)
    blob = media / "_shared" / digest[:2] / (blob_name or name)
    write_file(blob, data)
    link = media / str(chat_id) / name
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(blob, link.parent), link)
    return link


@contextlib.contextmanager
def sqlite(path: Path):
    """A plain sqlite3 connection that commits on success and is always closed."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def tree(root: Path) -> dict[str, str]:
    """Every entry under ``root``: a file's hash or a link's target."""
    entries = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            key = str(path.relative_to(root))
            entries[key] = f"link:{os.readlink(path)}" if path.is_symlink() else sha(path.read_bytes())
    return entries


async def open_adapter(url: str) -> tuple[DatabaseManager, DatabaseAdapter]:
    manager = DatabaseManager(url)
    await manager.init()
    return manager, DatabaseAdapter(manager)


def message(chat_id: int, message_id: int, sender_id: int | None, text: str) -> dict:
    return {
        "id": message_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "date": datetime(2024, 1, message_id, 10, 0),
        "text": text,
    }


async def seed_source(url: str, media: Path) -> None:
    """Two accounts. Account A shares a channel with the target; account B has a private chat."""
    manager, db = await open_adapter(url)
    try:
        account_a = await db.ensure_account(telegram_user_id=SOURCE_USER_A, env_index=1, label="Account A")
        account_b = await db.ensure_account(telegram_user_id=SOURCE_USER_B, env_index=2, label="Account B")
        await db.upsert_user({"id": SHARED_USER, "first_name": "Source Copy"})
        await db.upsert_user({"id": NEW_USER, "first_name": "Only In Source"})

        await db.upsert_chat(
            {"id": CHANNEL, "type": "channel", "title": "Test Channel", "avatar_photo_id": 5550001},
            account_id=account_a,
        )
        await db.upsert_chat(
            {"id": FORUM, "type": "channel", "title": "Test Forum", "is_forum": 1}, account_id=account_a
        )
        await db.upsert_chat({"id": PRIVATE, "type": "private", "first_name": "Only In Source"}, account_id=account_b)

        await db.insert_messages_batch(
            [
                message(CHANNEL, 1, SHARED_USER, "fake message one"),
                message(CHANNEL, 2, NEW_USER, "fake message two"),
                message(CHANNEL, 3, None, "fake message three"),
            ],
            account_id=account_a,
        )
        await db.insert_messages_batch([message(FORUM, 1, None, "fake topic message")], account_id=account_a)
        await db.insert_messages_batch([message(PRIVATE, 1, NEW_USER, "fake private message")], account_id=account_b)

        photo = write_blob(media, CHANNEL, "photo_1.jpg", PHOTO_BYTES)
        doc = write_blob(media, CHANNEL, "doc_2.pdf", DOC_BYTES)
        write_file(media / str(CHANNEL) / "plain_3.bin", PLAIN_BYTES)
        write_file(media / str(PRIVATE) / "voice_1.ogg", VOICE_BYTES)
        media_rows = [
            (account_a, f"{CHANNEL}_1_photo", CHANNEL, 1, "photo", str(photo), "photo_1.jpg", PHOTO_BYTES),
            (account_a, f"{CHANNEL}_2_document", CHANNEL, 2, "document", str(doc), "doc_2.pdf", DOC_BYTES),
            # Relative, the way the Telegram Desktop importer stores it.
            (account_a, f"{CHANNEL}_3_document", CHANNEL, 3, "document", f"{CHANNEL}/plain_3.bin", "plain_3.bin", None),
            (
                account_b,
                f"{PRIVATE}_1_voice",
                PRIVATE,
                1,
                "voice",
                str(media / str(PRIVATE) / "voice_1.ogg"),
                "voice_1.ogg",
                VOICE_BYTES,
            ),
        ]
        for account, media_id, chat_id, message_id, kind, path, name, data in media_rows:
            await db.insert_media(
                {
                    "id": media_id,
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "type": kind,
                    "file_path": path,
                    "file_name": name,
                    "content_hash": sha(data) if data else None,
                    "downloaded": True,
                },
                account_id=account,
            )
        # A row the backup never downloaded: no file, nothing to copy.
        await db.insert_media(
            {"id": f"{FORUM}_1_photo", "message_id": 1, "chat_id": FORUM, "type": "photo"}, account_id=account_a
        )

        async with manager.async_session_factory() as session:
            await session.execute(
                sa.insert(Reaction).values(
                    account_id=account_a, message_id=1, chat_id=CHANNEL, emoji="👍", user_id=NEW_USER
                )
            )
            await session.execute(
                sa.insert(ReactionHistory).values(
                    account_id=account_a,
                    message_id=1,
                    chat_id=CHANNEL,
                    emoji="👍",
                    count=1,
                    observed_at=datetime(2024, 1, 1, 9, 5),
                    source="listener",
                )
            )
            await session.execute(
                sa.insert(MessageVersion).values(
                    account_id=account_a,
                    message_id=2,
                    chat_id=CHANNEL,
                    text="fake message two, first draft",
                    date=datetime(2024, 1, 2, 9, 0),
                    change_hash=sha(b"fake version"),
                )
            )
            # The photo an edit replaced: its row and its file come along too.
            earlier = media / str(CHANNEL) / "earlier_photo_1.jpg"
            write_file(earlier, EARLIER_PHOTO_BYTES)
            await session.execute(
                sa.insert(MediaVersion).values(
                    account_id=account_a,
                    chat_id=CHANNEL,
                    message_id=1,
                    media_id=f"{CHANNEL}_1_photo_earlier",
                    type="photo",
                    file_path=str(earlier),
                    file_name="earlier_photo_1.jpg",
                    content_hash=sha(EARLIER_PHOTO_BYTES),
                    downloaded=1,
                    date=datetime(2024, 1, 1, 9, 0),
                    captured_at=datetime(2024, 1, 1, 9, 5),
                    source="listener",
                )
            )
            await session.commit()

        await db.upsert_forum_topic({"id": 5, "chat_id": FORUM, "title": "Test Topic"}, account_id=account_a)
        await db.upsert_chat_folder({"id": 2, "title": "Test Folder"}, account_id=account_a)
        await db.sync_folder_members(2, [CHANNEL, FORUM], account_id=account_a)
        await db.update_sync_status(CHANNEL, 3, 3, account_id=account_a)
        await db.update_sync_status(PRIVATE, 1, 1, account_id=account_b)

        first = await db.enqueue_media_transcript(f"{CHANNEL}_1_photo", account_id=account_a)
        await db.fill_media_transcript(first["id"], status="done", account_id=account_a, text="fake transcript")
        async with manager.async_session_factory() as session:
            await session.execute(
                sa.insert(MediaTranscript).values(
                    account_id=account_b,
                    media_id=f"{PRIVATE}_1_voice",
                    status="done",
                    text="fake transcript",
                    copied_from_id=first["id"],
                )
            )
            await session.commit()

        await db.set_metadata("followed_migrations", "[1]")
        await db.set_metadata(f"message_failures_{CHANNEL}", "{}")
        await db.set_metadata("followed_migrations_account_2", "[2]")
        await db.set_metadata("owner_id", "fake owner")
        await db.set_metadata("listener_active_since", "fake time")

        write_file(media / "avatars" / "chats" / f"{CHANNEL}_5550001.jpg", AVATAR_BYTES)
        write_file(media / "avatars" / "users" / f"{NEW_USER}_5550002.jpg", AVATAR_BYTES)
        write_file(media / "avatars" / "users" / "700000099_5550003.jpg", AVATAR_BYTES)  # nobody the source knows
    finally:
        await manager.close()


async def seed_target(url: str, media: Path) -> None:
    """One account holding the same channel, with the photo already on disk under another name."""
    manager, db = await open_adapter(url)
    try:
        account = await db.ensure_account(telegram_user_id=TARGET_USER, env_index=1, label="Account T")
        await db.upsert_user({"id": SHARED_USER, "first_name": "Target Copy"})
        await db.upsert_chat({"id": CHANNEL, "type": "channel", "title": "Test Channel"}, account_id=account)
        await db.insert_messages_batch([message(CHANNEL, 1, SHARED_USER, "fake message one")], account_id=account)
        link = write_blob(media, CHANNEL, "target_photo.jpg", PHOTO_BYTES)
        await db.insert_media(
            {
                "id": f"{CHANNEL}_1_photo",
                "message_id": 1,
                "chat_id": CHANNEL,
                "type": "photo",
                "file_path": str(link),
                "file_name": "target_photo.jpg",
                "content_hash": sha(PHOTO_BYTES),
                "downloaded": True,
            },
            account_id=account,
        )
        await db.set_metadata("followed_migrations", "[9]")
        await db.set_metadata("owner_id", "fake target owner")
    finally:
        await manager.close()


@functools.cache
def seeded_archives() -> Path:
    """Both archives, built once from the migrations and the adapter; each test copies them."""
    folder = tempfile.TemporaryDirectory(prefix="ta-merge-template-")
    atexit.register(folder.cleanup)
    root = Path(folder.name).resolve()
    head = root / "head.db"
    upgrade_to_head(sqlite_url(head))
    for name, seed in (("source", seed_source), ("target", seed_target)):
        backups = root / name / "backups"
        (backups / "media").mkdir(parents=True)
        shutil.copyfile(head, backups / "telegram_backup.db")
        asyncio.run(seed(sqlite_url(backups / "telegram_backup.db"), backups / "media"))
    return root


class MergeCase(unittest.TestCase):
    """Two fresh archives per test: ``self.source_*`` and ``self.target_*``."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ta-merge-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copytree(seeded_archives(), self.tmp, symlinks=True, dirs_exist_ok=True)
        self.source_db = self.tmp / "source" / "backups" / "telegram_backup.db"
        self.source_media = self.tmp / "source" / "backups" / "media"
        self.target_db = self.tmp / "target" / "backups" / "telegram_backup.db"
        self.target_media = self.tmp / "target" / "backups" / "media"

    def run_merge(self, **overrides) -> merge.MergeReport:
        options = {
            "source": str(self.source_db),
            "target_url": sqlite_url(self.target_db),
            "target_media": str(self.target_media),
        }
        options.update(overrides)
        return merge.merge_archives(**options)

    def target_rows(self, sql: str, **params) -> list[tuple]:
        with sqlite(self.target_db) as conn:
            return conn.execute(sql, params).fetchall()

    def target_value(self, sql: str, **params):
        rows = self.target_rows(sql, **params)
        return rows[0][0] if rows else None

    def target_dump(self) -> dict[str, list[tuple]]:
        with sqlite(self.target_db) as conn:
            # The full-text indexes' own tables change whenever a row is added.
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            names = [row[0] for row in tables if "_fts" not in row[0]]
            return {name: sorted(map(repr, conn.execute(f'SELECT * FROM "{name}"'))) for name in names}


class TestMergeCopiesEverything(MergeCase):
    def setUp(self):
        super().setUp()
        self.source_before = sha(self.source_db.read_bytes())
        self.target_before = self.target_dump()
        self.report = self.run_merge()

    def test_accounts_get_the_next_free_ids(self):
        self.assertEqual({1: 2, 2: 3}, self.report.account_ids)
        self.assertEqual(
            [(1, "Account T", TARGET_USER), (2, "Account A", SOURCE_USER_A), (3, "Account B", SOURCE_USER_B)],
            self.target_rows("SELECT id, label, telegram_user_id FROM accounts ORDER BY id"),
        )

    def test_row_counts_per_table(self):
        expected = {
            "accounts": 2,
            "users": 1,
            "chats": 3,
            "chat_folders": 1,
            "messages": 5,
            "forum_topics": 1,
            "sync_status": 2,
            "chat_folder_members": 2,
            "media": 5,
            "message_versions": 1,
            "media_versions": 1,
            "reactions": 1,
            "reaction_history": 1,
            "avatar_history": 1,
            "media_transcripts": 2,
            "metadata": 3,
        }
        self.assertEqual(expected, self.report.rows)
        for table in ("chats", "messages", "media", "reactions", "media_transcripts"):
            self.assertEqual(
                expected[table], self.target_value(f"SELECT count(*) FROM {table} WHERE account_id IN (2, 3)")
            )

    def test_the_target_keeps_every_row_it_had(self):
        after = self.target_dump()
        for table, rows in self.target_before.items():
            missing = set(rows) - set(after[table])
            self.assertEqual(set(), missing, table)

    def test_the_source_is_not_changed(self):
        self.assertEqual(self.source_before, sha(self.source_db.read_bytes()))

    def test_a_replaced_photo_follows_its_message_with_its_file(self):
        self.assertEqual(
            [(2, f"{CHANNEL}/earlier_photo_1.jpg")],
            self.target_rows("SELECT account_id, file_path FROM media_versions WHERE chat_id = :chat", chat=CHANNEL),
        )
        self.assertEqual(EARLIER_PHOTO_BYTES, (self.target_media / str(CHANNEL) / "earlier_photo_1.jpg").read_bytes())

    def test_a_messages_media_versions_and_reactions_follow_it(self):
        self.assertEqual(
            [(2, "fake message two, first draft")],
            self.target_rows(
                "SELECT account_id, text FROM message_versions WHERE chat_id = :chat AND message_id = 2", chat=CHANNEL
            ),
        )
        self.assertEqual(
            [(2, "👍", NEW_USER)],
            self.target_rows("SELECT account_id, emoji, user_id FROM reactions WHERE chat_id = :chat", chat=CHANNEL),
        )
        self.assertEqual(
            [(2, 1, "👍", 1, "listener")],
            self.target_rows(
                "SELECT account_id, message_id, emoji, count, source FROM reaction_history WHERE chat_id = :chat",
                chat=CHANNEL,
            ),
        )
        self.assertEqual(
            [(1, f"{CHANNEL}_1_photo"), (2, f"{CHANNEL}_1_photo")],
            self.target_rows(
                "SELECT account_id, id FROM media WHERE chat_id = :chat AND message_id = 1 ORDER BY account_id",
                chat=CHANNEL,
            ),
        )
        self.assertEqual(
            [(3, 1, "fake private message")],
            self.target_rows("SELECT account_id, id, text FROM messages WHERE chat_id = :chat", chat=PRIVATE),
        )

    def test_chat_refs_are_kept(self):
        with sqlite(self.source_db) as conn:
            source_refs = dict(conn.execute("SELECT id, ref FROM chats"))
        merged = dict(self.target_rows("SELECT id, ref FROM chats WHERE account_id IN (2, 3)"))
        self.assertEqual(source_refs, merged)

    def test_the_targets_user_rows_win(self):
        self.assertEqual(
            "Target Copy", self.target_value("SELECT first_name FROM users WHERE id = :id", id=SHARED_USER)
        )
        self.assertEqual(
            "Only In Source", self.target_value("SELECT first_name FROM users WHERE id = :id", id=NEW_USER)
        )

    def test_folders_topics_cursors_and_avatar_history_are_remapped(self):
        self.assertEqual([(2, 2, "Test Folder")], self.target_rows("SELECT account_id, id, title FROM chat_folders"))
        self.assertEqual(
            [(2, 2, CHANNEL), (2, 2, FORUM)],
            self.target_rows("SELECT account_id, folder_id, chat_id FROM chat_folder_members ORDER BY chat_id DESC"),
        )
        self.assertEqual([(2, FORUM, 5)], self.target_rows("SELECT account_id, chat_id, id FROM forum_topics"))
        self.assertEqual(
            [(2, CHANNEL, 3), (3, PRIVATE, 1)],
            self.target_rows(
                "SELECT account_id, chat_id, last_message_id FROM sync_status WHERE account_id > 1 ORDER BY account_id"
            ),
        )
        self.assertEqual(
            [(2, CHANNEL, 5550001)], self.target_rows("SELECT account_id, chat_id, photo_id FROM avatar_history")
        )

    def test_transcript_copy_links_point_at_the_new_rows(self):
        rows = self.target_rows("SELECT id, account_id, media_id, copied_from_id FROM media_transcripts ORDER BY id")
        (first_id, first_account, _, first_link), (_, second_account, second_media, second_link) = rows
        self.assertEqual((2, None), (first_account, first_link))
        self.assertEqual((3, f"{PRIVATE}_1_voice", first_id), (second_account, second_media, second_link))
        self.assertEqual(0, self.report.transcript_links_dropped)

    def test_per_account_metadata_is_rekeyed_and_nothing_else_is_copied(self):
        metadata = dict(self.target_rows("SELECT key, value FROM metadata"))
        self.assertEqual("[9]", metadata["followed_migrations"])
        self.assertEqual("[1]", metadata["followed_migrations_account_2"])
        self.assertEqual("[2]", metadata["followed_migrations_account_3"])
        self.assertEqual("{}", metadata[f"message_failures_{CHANNEL}_account_2"])
        self.assertEqual("fake target owner", metadata["owner_id"])
        self.assertNotIn("listener_active_since", metadata)

    def test_media_rows_point_at_paths_relative_to_the_media_folder(self):
        paths = dict(
            self.target_rows("SELECT id, file_path FROM media WHERE account_id IN (2, 3) AND file_path IS NOT NULL")
        )
        self.assertEqual(
            {
                f"{CHANNEL}_1_photo": f"{CHANNEL}/photo_1.jpg",
                f"{CHANNEL}_2_document": f"{CHANNEL}/doc_2.pdf",
                f"{CHANNEL}_3_document": f"{CHANNEL}/plain_3.bin",
                f"{PRIVATE}_1_voice": f"{PRIVATE}/voice_1.ogg",
            },
            paths,
        )

    def test_a_blob_the_target_has_is_reused_and_a_new_one_is_copied(self):
        photo = self.target_media / str(CHANNEL) / "photo_1.jpg"
        self.assertTrue(photo.is_symlink())
        existing_blob = self.target_media / "_shared" / sha(PHOTO_BYTES)[:2] / "target_photo.jpg"
        self.assertEqual(existing_blob.resolve(), photo.resolve())
        self.assertFalse((self.target_media / "_shared" / sha(PHOTO_BYTES)[:2] / "photo_1.jpg").exists())

        doc = self.target_media / str(CHANNEL) / "doc_2.pdf"
        self.assertTrue(doc.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(doc)))
        self.assertEqual(self.target_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf", doc.resolve())
        self.assertEqual(DOC_BYTES, doc.read_bytes())

    def test_plain_files_and_avatars_are_copied(self):
        plain = self.target_media / str(CHANNEL) / "plain_3.bin"
        self.assertFalse(plain.is_symlink())
        self.assertEqual(PLAIN_BYTES, plain.read_bytes())
        self.assertEqual(VOICE_BYTES, (self.target_media / str(PRIVATE) / "voice_1.ogg").read_bytes())
        self.assertEqual(
            AVATAR_BYTES, (self.target_media / "avatars" / "chats" / f"{CHANNEL}_5550001.jpg").read_bytes()
        )
        self.assertEqual(
            AVATAR_BYTES, (self.target_media / "avatars" / "users" / f"{NEW_USER}_5550002.jpg").read_bytes()
        )
        self.assertFalse((self.target_media / "avatars" / "users" / "700000099_5550003.jpg").exists())

    def test_the_media_plan(self):
        media = self.report.media
        # Files: the plain file, the voice note and the photo an edit replaced.
        self.assertEqual(
            (3, 1, 2, 0, 0, 2), (media.files, media.blobs, media.links, media.present, media.missing, media.avatars)
        )
        size = len(DOC_BYTES) + len(PLAIN_BYTES) + len(VOICE_BYTES) + len(EARLIER_PHOTO_BYTES) + 2 * len(AVATAR_BYTES)
        self.assertEqual(size, media.bytes)

    def test_the_report_lines(self):
        lines = merge.format_report(self.report)
        self.assertEqual("Merge complete:", lines[0])
        self.assertIn("  Source account 1 -> target account 2", lines)
        self.assertIn("    messages: 5", lines)
        self.assertIn("  Shared files copied: 1", lines)


class TestDryRun(MergeCase):
    def test_changes_nothing_and_reports_the_same_counts(self):
        database = sha(self.target_db.read_bytes())
        files = tree(self.target_media)

        report = self.run_merge(dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(database, sha(self.target_db.read_bytes()))
        self.assertEqual(files, tree(self.target_media))
        self.assertEqual(5, report.rows["messages"])
        self.assertEqual(
            (3, 1, 2, 2), (report.media.files, report.media.blobs, report.media.links, report.media.avatars)
        )
        self.assertEqual("[DRY RUN] Merge plan, nothing written:", merge.format_report(report)[0])

        # The plan is what the real run then does.
        real = self.run_merge()
        self.assertEqual(report.rows, real.rows)
        self.assertEqual(report.media, real.media)

    def test_two_source_blobs_with_the_same_bytes_are_planned_as_one(self):
        write_blob(self.source_media, CHANNEL, "doc_copy.pdf", DOC_BYTES)
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name, content_hash, downloaded)"
                " VALUES (1, 'fake_doc_copy', 2, ?, 'document', ?, 'doc_copy.pdf', ?, 1)",
                (CHANNEL, f"{CHANNEL}/doc_copy.pdf", sha(DOC_BYTES)),
            )

        dry = self.run_merge(dry_run=True)
        real = self.run_merge()

        self.assertEqual(dry.media, real.media)
        self.assertEqual((1, 3), (real.media.blobs, real.media.links))
        copy = self.target_media / str(CHANNEL) / "doc_copy.pdf"
        self.assertEqual((self.target_media / str(CHANNEL) / "doc_2.pdf").resolve(), copy.resolve())

    def test_a_target_file_named_like_a_merged_row_is_not_taken_for_its_blob(self):
        # Two source rows on one blob, the way the backup's dedup links them.
        # The target holds an unrelated older flat file under the second name.
        blob = self.source_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf"
        other = self.source_media / str(CHANNEL) / "other_2.pdf"
        os.symlink(os.path.relpath(blob, other.parent), other)
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name, content_hash, downloaded)"
                " VALUES (1, 'fake_other', 2, ?, 'document', ?, 'other_2.pdf', ?, 1)",
                (CHANNEL, f"{CHANNEL}/other_2.pdf", sha(DOC_BYTES)),
            )
        write_file(self.target_media / "_shared" / "other_2.pdf", b"fake unrelated target bytes")

        dry = self.run_merge(dry_run=True)
        real = self.run_merge()

        self.assertEqual(dry.media, real.media)
        self.assertEqual(1, real.media.blobs)
        for name in ("doc_2.pdf", "other_2.pdf"):
            self.assertEqual(DOC_BYTES, (self.target_media / str(CHANNEL) / name).read_bytes(), name)
        self.assertEqual(b"fake unrelated target bytes", (self.target_media / "_shared" / "other_2.pdf").read_bytes())


class TestAccountSelection(MergeCase):
    def test_one_account_by_label(self):
        report = self.run_merge(account="Account B")
        self.assertEqual({2: 2}, report.account_ids)
        self.assertEqual(1, report.rows["messages"])
        # Its transcript was copied from account A's row, which stays behind.
        self.assertEqual(1, report.transcript_links_dropped)
        self.assertIsNone(self.target_value("SELECT copied_from_id FROM media_transcripts WHERE account_id = 2"))
        self.assertIn("  Transcript copy links left empty (source row not merged): 1", merge.format_report(report))

    def test_one_account_by_id(self):
        self.assertEqual({1: 2}, self.run_merge(account="1").account_ids)

    def add_user_only_account_b_knows(self) -> int:
        user_id = 700000060
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO users (id, first_name, phone, is_bot) VALUES (?, 'Only Account B', '+1 555 0100', 0)",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO reactions (account_id, message_id, chat_id, emoji, user_id, count) VALUES (2, 1, ?, 'x', ?, 1)",
                (PRIVATE, user_id),
            )
        return user_id

    def test_users_only_a_left_out_account_references_are_not_copied(self):
        user_id = self.add_user_only_account_b_knows()
        dry = self.run_merge(account="Account A", dry_run=True)
        report = self.run_merge(account="Account A")
        self.assertEqual(dry.rows, report.rows)
        self.assertEqual(1, report.rows["users"])
        self.assertIsNone(self.target_value("SELECT phone FROM users WHERE id = :id", id=user_id))
        self.assertEqual(
            "Only In Source", self.target_value("SELECT first_name FROM users WHERE id = :id", id=NEW_USER)
        )

    def test_users_the_chosen_account_references_are_copied(self):
        user_id = self.add_user_only_account_b_knows()
        report = self.run_merge(account="Account B")
        self.assertEqual(2, report.rows["users"])
        self.assertEqual("+1 555 0100", self.target_value("SELECT phone FROM users WHERE id = :id", id=user_id))

    def test_a_label_made_of_digits_wins_over_an_account_id(self):
        with sqlite(self.source_db) as conn:
            conn.execute("UPDATE accounts SET label = '1' WHERE id = 2")
        self.assertEqual({2: 2}, self.run_merge(account="1").account_ids)

    def test_an_unknown_account_is_refused(self):
        with self.assertRaisesRegex(merge.MergeError, "no account matching"):
            self.run_merge(account="Account Z")

    def test_a_label_two_accounts_share_is_refused(self):
        with sqlite(self.source_db) as conn:
            conn.execute("UPDATE accounts SET label = 'Account A'")
        with self.assertRaisesRegex(merge.MergeError, "more than one source account"):
            self.run_merge(account="Account A")


class TestRefusals(MergeCase):
    def assert_refused(self, pattern: str, **overrides) -> None:
        database = sha(self.target_db.read_bytes())
        files = tree(self.target_media)
        with self.assertRaisesRegex(merge.MergeError, pattern):
            self.run_merge(**overrides)
        self.assertEqual(database, sha(self.target_db.read_bytes()))
        self.assertEqual(files, tree(self.target_media))

    def test_a_revision_mismatch(self):
        with sqlite(self.source_db) as conn:
            conn.execute("UPDATE alembic_version SET version_num = '032'")
        self.assert_refused("source is at 032")

    def test_a_target_without_migrations(self):
        with sqlite(self.target_db) as conn:
            conn.execute("DROP TABLE alembic_version")
        self.assert_refused("target at none")

    def test_the_same_telegram_account_on_both_sides(self):
        with sqlite(self.target_db) as conn:
            conn.execute("UPDATE accounts SET telegram_user_id = ?", (SOURCE_USER_B,))
        self.assert_refused("source account 2 is the same Telegram account as target account 1")

    def test_a_source_account_that_never_logged_in(self):
        with sqlite(self.source_db) as conn:
            conn.execute("UPDATE accounts SET telegram_user_id = NULL WHERE id = 2")
        self.assert_refused("source account 2 has never logged in")

    def test_a_target_account_that_never_logged_in(self):
        with sqlite(self.target_db) as conn:
            conn.execute("UPDATE accounts SET telegram_user_id = NULL")
        self.assert_refused("the target has an account that has never logged in")

    def test_a_source_with_no_accounts(self):
        with sqlite(self.source_db) as conn:
            conn.execute("DELETE FROM accounts")
        self.assert_refused("no accounts")

    def test_a_chat_ref_the_target_already_uses(self):
        with sqlite(self.source_db) as conn:
            ref = conn.execute("SELECT ref FROM chats WHERE id = ?", (PRIVATE,)).fetchone()[0]
        with sqlite(self.target_db) as conn:
            conn.execute("UPDATE chats SET ref = ?", (ref,))
        self.assert_refused("1 chat ref")

    def test_a_metadata_key_the_target_already_uses(self):
        with sqlite(self.target_db) as conn:
            conn.execute("INSERT INTO metadata (key, value) VALUES ('followed_migrations_account_3', '[]')")
        self.assert_refused("metadata key")

    def test_a_media_name_the_target_uses_for_other_bytes(self):
        write_file(self.target_media / str(CHANNEL) / "plain_3.bin", b"fake other bytes")
        self.assert_refused("same name as a different source file")

    def test_a_shared_name_the_target_uses_for_other_bytes(self):
        write_file(self.target_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf", b"fake other bytes")
        self.assert_refused("shared media file in the target has the same name")

    def test_a_link_name_the_target_uses_for_other_bytes(self):
        write_file(self.target_media / str(CHANNEL) / "doc_2.pdf", b"fake other bytes")
        self.assert_refused("same name as a different source file")

    def test_a_backup_running_on_the_target(self):
        with sqlite(self.target_db) as conn:
            conn.execute("INSERT INTO metadata (key, value) VALUES ('backup_in_progress', '1')")
        self.assert_refused("a backup is running")

    def test_a_finished_backup_on_the_target_is_no_refusal(self):
        with sqlite(self.target_db) as conn:
            conn.execute("INSERT INTO metadata (key, value) VALUES ('backup_in_progress', '0')")
        self.assertEqual({1: 2, 2: 3}, self.run_merge().account_ids)

    def test_another_writer_on_the_target(self):
        writer = sqlite3.connect(self.target_db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        try:
            with (
                patch.object(merge, "SQLITE_BUSY_TIMEOUT", 0.1),
                self.assertRaisesRegex(merge.MergeError, "another process is writing"),
            ):
                self.run_merge()
        finally:
            writer.execute("ROLLBACK")

    def test_the_same_database_twice(self):
        self.assert_refused("same database", source=str(self.target_db))

    def test_a_missing_source_file(self):
        self.assert_refused("source database file does not exist", source=str(self.tmp / "missing.db"))

    def test_a_missing_target_file(self):
        with self.assertRaisesRegex(merge.MergeError, "target database file does not exist"):
            self.run_merge(target_url=sqlite_url(self.tmp / "missing.db"))

    def test_a_missing_source_media_folder(self):
        self.assert_refused("--source-media folder does not exist", source_media=str(self.tmp / "missing"))

    def test_rows_that_collide_roll_back(self):
        # The id planner never picks a used id, so force one: account 1 exists.
        with sqlite(self.source_db) as conn:
            conn.execute("DELETE FROM metadata")
        with patch.object(merge, "plan_account_ids", return_value={1: 1, 2: 3}):
            self.assert_refused("rejected a accounts row")

    def test_a_count_mismatch_rolls_back(self):
        real_count = merge.count_account_rows

        def one_reaction_lost(conn, table, account_ids):
            landed = real_count(conn, table, account_ids)
            return landed - 1 if table.name == "reactions" and account_ids == [2, 3] else landed

        with patch.object(merge, "count_account_rows", side_effect=one_reaction_lost):
            self.assert_refused("do not match the source for: reactions")


class TestMergeEdges(MergeCase):
    def test_a_second_run_is_refused_by_the_account_check(self):
        self.run_merge()
        with self.assertRaisesRegex(merge.MergeError, "same Telegram account"):
            self.run_merge()

    def test_without_source_media_rows_are_merged_and_files_are_not(self):
        shutil.rmtree(self.source_media)
        report = self.run_merge()
        self.assertIsNone(report.media)
        self.assertEqual(5, report.rows["messages"])
        self.assertIn(
            "  Media files: not copied (no source media folder; pass --source-media)", merge.format_report(report)
        )

    def test_explicit_source_media_and_files_already_present(self):
        moved = self.tmp / "elsewhere"
        shutil.copytree(self.source_media, moved, symlinks=True)
        shutil.copytree(self.source_media / str(PRIVATE), self.target_media / str(PRIVATE))
        write_file(self.target_media / "avatars" / "chats" / f"{CHANNEL}_5550001.jpg", AVATAR_BYTES)
        write_file(self.target_media / "avatars" / "users" / f"{NEW_USER}_5550002.jpg", b"fake other avatar")
        report = self.run_merge(source_media=str(moved))
        # Files: the plain file and the photo an edit replaced; the voice note is present.
        self.assertEqual((2, 1, 1), (report.media.files, report.media.avatars_present, report.media.avatars_kept))
        self.assertEqual(1, report.media.present)
        self.assertEqual(
            b"fake other avatar", (self.target_media / "avatars" / "users" / f"{NEW_USER}_5550002.jpg").read_bytes()
        )

    def test_missing_source_files_are_counted(self):
        os.remove(self.source_media / str(CHANNEL) / "plain_3.bin")
        os.remove(self.source_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf")  # leaves a dangling link
        report = self.run_merge()
        self.assertEqual(2, report.media.missing)
        self.assertFalse(os.path.lexists(self.target_media / str(CHANNEL) / "doc_2.pdf"))

    def test_an_undownloaded_row_with_a_path_is_not_counted_missing(self):
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name, downloaded)"
                " VALUES (1, 'fake_never_downloaded', 3, ?, 'document', ?, 'never_3.bin', 0)",
                (CHANNEL, f"{CHANNEL}/never_3.bin"),
            )
        report = self.run_merge()
        self.assertEqual(0, report.media.missing)
        self.assertEqual(6, report.rows["media"])

    def test_a_link_to_a_blob_without_a_stored_hash_is_hashed(self):
        with sqlite(self.source_db) as conn:
            conn.execute("UPDATE media SET content_hash = NULL")
        self.run_merge()
        self.assertEqual(DOC_BYTES, (self.target_media / str(CHANNEL) / "doc_2.pdf").read_bytes())

    def test_two_rows_on_one_file(self):
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name, content_hash, downloaded)"
                " SELECT account_id, id || '_twin', message_id, chat_id, type, file_path, file_name, content_hash, 1"
                " FROM media WHERE file_path IS NOT NULL"
            )
        report = self.run_merge()
        # Files: the plain file, the voice note and the photo an edit replaced (kept once).
        self.assertEqual(
            (3, 1, 2, 4), (report.media.files, report.media.blobs, report.media.links, report.media.present)
        )

    def test_an_unusable_stored_path_is_kept_and_skipped(self):
        with sqlite(self.source_db) as conn:
            conn.execute(
                "UPDATE media SET file_path = '/somewhere/else/x.bin' WHERE id = ?", (f"{CHANNEL}_3_document",)
            )
        report = self.run_merge()
        self.assertEqual(1, report.media.missing)
        self.assertEqual(
            "/somewhere/else/x.bin",
            self.target_value(
                "SELECT file_path FROM media WHERE id = :id AND account_id = 2", id=f"{CHANNEL}_3_document"
            ),
        )

    def test_a_folder_that_is_not_a_file_counts_as_missing(self):
        target = self.source_media / str(CHANNEL) / "plain_3.bin"
        os.remove(target)
        target.mkdir()
        self.assertEqual(1, self.run_merge().media.missing)

    def test_a_shared_blob_already_at_its_name_is_reused(self):
        # Same bytes at the same _shared name, but no target media row names it.
        blob = self.target_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf"
        write_file(blob, DOC_BYTES)
        report = self.run_merge()
        self.assertEqual(0, report.media.blobs)
        self.assertEqual(blob, (self.target_media / str(CHANNEL) / "doc_2.pdf").resolve())

    def test_a_target_media_row_without_a_file_name_is_passed_over(self):
        with sqlite(self.target_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, content_hash, downloaded)"
                " VALUES (1, 'fake_nameless', 1, ?, 'document', ?, 0)",
                (CHANNEL, sha(DOC_BYTES)),
            )
        self.assertEqual(1, self.run_merge().media.blobs)

    def test_a_dangling_link_in_the_target_is_not_replaced(self):
        link = self.target_media / str(CHANNEL) / "plain_3.bin"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("nowhere", link)
        with self.assertRaisesRegex(merge.MergeError, "same name as a different source file"):
            self.run_merge()
        self.assertEqual("nowhere", os.readlink(link))

    def test_a_source_without_avatars(self):
        shutil.rmtree(self.source_media / "avatars")
        self.assertEqual(0, self.run_merge().media.avatars)

    def test_the_account_id_skips_rows_a_removed_account_left(self):
        with sqlite(self.target_db) as conn:
            conn.execute(
                "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id)"
                " VALUES (5, 1, 'fake-ref-left-behind00', 'private', 0)"
            )
        self.assertEqual({1: 6, 2: 7}, self.run_merge().account_ids)

    def test_the_account_id_skips_a_transcript_a_removed_account_left(self):
        with sqlite(self.target_db) as conn:
            conn.execute(
                "INSERT INTO media_transcripts (account_id, media_id, status, requested_at, created_at)"
                " VALUES (8, 'fake_left_behind', 'done', '2024-01-01', '2024-01-01')"
            )
        self.assertEqual({1: 9, 2: 10}, self.run_merge().account_ids)

    def test_an_account_row_without_rows_is_skipped(self):
        # Account 2 has logged in but holds nothing yet: its id is still taken.
        with sqlite(self.target_db) as conn:
            conn.execute("INSERT INTO accounts (id, label, telegram_user_id) VALUES (2, 'Account E', 900000010)")
        self.assertEqual({1: 3, 2: 4}, self.run_merge().account_ids)

    def test_a_source_media_folder_behind_a_symlinked_parent_keeps_its_links(self):
        alias = self.tmp / "alias"
        os.symlink(self.source_media.parent, alias)
        report = self.run_merge(source_media=str(alias / "media"))
        # Files: the plain file, the voice note and the photo an edit replaced.
        self.assertEqual((3, 1, 2), (report.media.files, report.media.blobs, report.media.links))
        doc = self.target_media / str(CHANNEL) / "doc_2.pdf"
        self.assertTrue(doc.is_symlink())
        self.assertEqual(self.target_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf", doc.resolve())

    def test_a_dangling_shared_entry_a_target_row_names_is_not_reused(self):
        gone = self.target_media / "_shared" / sha(DOC_BYTES)[:2] / "gone.pdf"
        gone.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("nowhere", gone)
        with sqlite(self.target_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_name, content_hash, downloaded)"
                " VALUES (1, 'fake_gone', 1, ?, 'document', 'gone.pdf', ?, 1)",
                (CHANNEL, sha(DOC_BYTES)),
            )
        report = self.run_merge()
        self.assertEqual(1, report.media.blobs)
        self.assertEqual(DOC_BYTES, (self.target_media / str(CHANNEL) / "doc_2.pdf").read_bytes())
        self.assertEqual("nowhere", os.readlink(gone))

    def test_a_target_row_whose_file_holds_other_bytes_is_not_reused(self):
        # An older flat _shared file that a target row names with the same hash,
        # but whose bytes differ.
        write_file(self.target_media / "_shared" / "old.pdf", b"fake other bytes")
        with sqlite(self.target_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_name, content_hash, downloaded)"
                " VALUES (1, 'fake_old', 1, ?, 'document', 'old.pdf', ?, 1)",
                (CHANNEL, sha(DOC_BYTES)),
            )
        dry = self.run_merge(dry_run=True)
        report = self.run_merge()
        self.assertEqual(dry.media, report.media)
        self.assertEqual(1, report.media.blobs)
        self.assertEqual(DOC_BYTES, (self.target_media / str(CHANNEL) / "doc_2.pdf").read_bytes())

    def test_a_second_source_blob_reuses_a_target_blob_found_at_its_name(self):
        existing = self.target_media / "_shared" / sha(DOC_BYTES)[:2] / "doc_2.pdf"
        write_file(existing, DOC_BYTES)
        write_blob(self.source_media, CHANNEL, "doc_copy.pdf", DOC_BYTES)
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name, content_hash, downloaded)"
                " VALUES (1, 'fake_doc_copy', 2, ?, 'document', ?, 'doc_copy.pdf', ?, 1)",
                (CHANNEL, f"{CHANNEL}/doc_copy.pdf", sha(DOC_BYTES)),
            )
        report = self.run_merge()
        self.assertEqual(0, report.media.blobs)
        self.assertEqual(existing, (self.target_media / str(CHANNEL) / "doc_copy.pdf").resolve())
        self.assertFalse((existing.parent / "doc_copy.pdf").exists())

    def lock_source_folder(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        folder = self.source_db.parent
        os.chmod(folder, 0o555)
        self.addCleanup(os.chmod, folder, 0o755)

    def test_a_source_in_a_read_only_folder_is_read_and_left_alone(self):
        with sqlite(self.source_db) as conn:
            self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0])
        before = sorted(os.listdir(self.source_db.parent))
        self.lock_source_folder()
        self.assertEqual({1: 2, 2: 3}, self.run_merge().account_ids)
        self.assertEqual(before, sorted(os.listdir(self.source_db.parent)))

    def wal_source(self) -> Path:
        """A copy of the source whose last change sits only in its -wal file."""
        copy = self.tmp / "walcopy" / "telegram_backup.db"
        copy.parent.mkdir()
        writer = sqlite3.connect(self.source_db)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            with writer:
                writer.execute("INSERT INTO metadata (key, value) VALUES ('import_progress', 'fake progress')")
            shutil.copyfile(self.source_db, copy)
            shutil.copyfile(f"{self.source_db}-wal", f"{copy}-wal")
        finally:
            writer.close()
        self.assertGreater(os.path.getsize(f"{copy}-wal"), 0)
        return copy

    def test_a_source_with_a_wal_file_is_read_with_it(self):
        copy = self.wal_source()
        self.run_merge(source=str(copy), source_media=str(self.source_media))
        self.assertEqual(
            "fake progress", self.target_value("SELECT value FROM metadata WHERE key = 'import_progress_account_2'")
        )

    def test_a_source_with_a_wal_file_in_a_read_only_folder_is_refused(self):
        copy = self.wal_source()
        if os.geteuid() == 0:
            self.skipTest("root writes into a read-only folder")
        os.chmod(copy.parent, 0o555)
        self.addCleanup(os.chmod, copy.parent, 0o755)
        with self.assertRaisesRegex(merge.MergeError, "changes still in its -wal file"):
            self.run_merge(source=str(copy))
        self.assertEqual(["telegram_backup.db", "telegram_backup.db-wal"], sorted(os.listdir(copy.parent)))

    def add_orphans(self) -> None:
        """Rows whose parent row the source lacks, one per kind of parent."""
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned)"
                " VALUES (1, 1, -1001000000099, '2024-01-01', 'fake', 0, 0)"
            )
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, downloaded)"
                " VALUES (2, 'fake_orphan', 9, ?, 'photo', 0)",
                (PRIVATE,),
            )
            conn.execute(
                "INSERT INTO reactions (account_id, message_id, chat_id, emoji, user_id, count) VALUES (1, 99, ?, 'x', NULL, 1)",
                (CHANNEL,),
            )
            conn.execute(
                "INSERT INTO reactions (account_id, message_id, chat_id, emoji, user_id, count)"
                " VALUES (1, 1, ?, 'y', 700000051, 1)",
                (CHANNEL,),
            )
            conn.execute(
                "INSERT INTO chat_folder_members (account_id, folder_id, chat_id) VALUES (1, 7, ?)", (CHANNEL,)
            )

    def test_missing_parents_get_placeholders_on_request(self):
        self.add_orphans()
        dry = self.run_merge(dry_run=True, add_missing_parents=True)
        report = self.run_merge(add_missing_parents=True)

        expected = {"chats": 1, "chat_folders": 1, "messages": 2, "users": 1}
        self.assertEqual(expected, dry.placeholders)
        self.assertEqual(expected, report.placeholders)
        self.assertEqual(dry.rows, report.rows)
        self.assertEqual((6, 3, 6), (report.rows["messages"], report.rows["reactions"], report.rows["media"]))
        self.assertIn("  Placeholder parent rows added:", merge.format_report(report))
        self.assertIn("    messages: 2", merge.format_report(report))

        self.assertEqual(
            [("supergroup", "")],
            self.target_rows("SELECT type, title FROM chats WHERE account_id = 2 AND id = -1001000000099"),
        )
        self.assertEqual(
            [(None, "1970-01-01 00:00:00.000000")],
            self.target_rows("SELECT text, date FROM messages WHERE account_id = 3 AND id = 9"),
        )
        self.assertEqual([("",)], self.target_rows("SELECT title FROM chat_folders WHERE account_id = 2 AND id = 7"))
        self.assertEqual([(None,)], self.target_rows("SELECT first_name FROM users WHERE id = 700000051"))
        # Every merged row now has its parent, the state a PostgreSQL target requires.
        with sqlite(self.target_db) as conn:
            self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_missing_parents_stay_missing_by_default_on_sqlite(self):
        self.add_orphans()
        report = self.run_merge()
        self.assertEqual({}, report.placeholders)
        self.assertEqual(0, self.target_value("SELECT count(*) FROM chats WHERE id = -1001000000099"))

    def test_orphan_rows_are_counted_per_table(self):
        with sqlite(self.source_db) as conn:
            conn.execute(
                "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned)"
                " VALUES (1, 1, -1001000000099, '2024-01-01', 'fake', 0, 0)"
            )
            conn.execute(
                "INSERT INTO media (account_id, id, message_id, chat_id, type, downloaded)"
                " VALUES (2, 'fake_orphan', 9, ?, 'photo', 0)",
                (PRIVATE,),
            )
            reactions = [
                (99, "🔥", None),  # its message is gone
                (1, "🎉", 700000050),  # a user only the target knows
                (1, "🙂", 700000051),  # a user neither archive knows
            ]
            for message_id, emoji, user_id in reactions:
                conn.execute(
                    "INSERT INTO reactions (account_id, message_id, chat_id, emoji, user_id, count)"
                    " VALUES (1, ?, ?, ?, ?, 1)",
                    (message_id, CHANNEL, emoji, user_id),
                )
        with sqlite(self.target_db) as conn:
            conn.execute("INSERT INTO users (id, first_name, is_bot) VALUES (700000050, 'Target Only', 0)")

        source = merge.open_engine(merge.sync_database_url(str(self.source_db)), read_only=True)
        target = merge.open_engine(merge.sync_database_url(str(self.target_db)), read_only=False)
        try:
            with source.connect() as source_conn, target.connect() as target_conn:
                counts = merge.count_orphans(source_conn, target_conn, [1, 2])
                # A SQLite target accepts them, so nothing is refused there.
                merge.check_orphans(source_conn, target_conn, [1, 2])
        finally:
            source.dispose()
            target.dispose()
        self.assertEqual({"messages": 1, "media": 1, "reactions": 2}, counts)


class TestSmallPieces(unittest.TestCase):
    def test_sync_database_url(self):
        self.assertEqual(f"sqlite:///{os.path.abspath('x.db')}", merge.sync_database_url("x.db"))
        self.assertEqual("sqlite:////data/a.db", merge.sync_database_url("sqlite+aiosqlite:////data/a.db"))
        self.assertEqual(
            "postgresql+psycopg2://u:p@db:5432/archive",
            merge.sync_database_url("postgresql+asyncpg://u:p@db:5432/archive"),
        )
        self.assertEqual("postgresql+psycopg2://u@db/archive", merge.sync_database_url("postgres://u@db/archive"))
        with self.assertRaisesRegex(merge.MergeError, "only SQLite and PostgreSQL"):
            merge.sync_database_url("mysql://u@db/archive")

    def test_target_database_url_reads_the_settings(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:////fake/target.db"}):
            self.assertEqual("sqlite:////fake/target.db", merge.target_database_url())

    def test_same_database(self):
        self.assertTrue(merge.same_database("postgresql://u@db/a", "postgresql://v@db:5432/a"))
        self.assertFalse(merge.same_database("postgresql://u@db/a", "postgresql://u@db/b"))
        self.assertFalse(merge.same_database("sqlite:////x.db", "postgresql://u@db/a"))
        self.assertFalse(merge.same_database("postgresql://u@db/a", "sqlite:////x.db"))

    def test_media_relative_path(self):
        cases = {
            None: None,
            "": None,
            "-100/a.jpg": "-100/a.jpg",
            "/data/backups/media/-100/a.jpg": "-100/a.jpg",
            "C:\\archive\\media\\-100\\a.jpg": "-100/a.jpg",
            "/data/other/a.jpg": None,
            "/data/backups/media/": None,
            "../a.jpg": None,
        }
        for stored, expected in cases.items():
            self.assertEqual(expected, merge.media_relative_path(stored), stored)

    def test_remap_metadata_key(self):
        mapping = {1: 4, 2: 5}
        self.assertEqual("import_progress_account_4", merge.remap_metadata_key("import_progress", mapping))
        self.assertEqual(
            "reaction_resweep_cycle_done_account_5",
            merge.remap_metadata_key("reaction_resweep_cycle_done_account_2", mapping),
        )
        self.assertEqual("message_failures_-100_account_4", merge.remap_metadata_key("message_failures_-100", mapping))
        self.assertIsNone(merge.remap_metadata_key("whitelist_unresolved_ids_account_3", mapping))
        self.assertIsNone(merge.remap_metadata_key("cached_stats", mapping))
        self.assertEqual("followed_migrations", merge.remap_metadata_key("followed_migrations_account_2", {2: 1}))

    def test_copy_new_file_never_replaces(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "a"
            source.write_bytes(b"fake")
            destination = Path(folder) / "b"
            destination.write_bytes(b"fake existing")
            with (
                patch.object(merge.os.path, "lexists", return_value=True),
                self.assertRaisesRegex(merge.MergeError, "appeared in the target"),
            ):
                merge.copy_new_file(str(source), str(destination))
            self.assertEqual(b"fake existing", destination.read_bytes())
            self.assertEqual(["a", "b"], sorted(os.listdir(folder)))

    def test_copy_new_file_removes_its_partial_file_on_failure(self):
        def half_copy(source, destination):
            Path(destination).write_bytes(b"fake half")
            raise OSError("fake disk full")

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "a"
            source.write_bytes(b"fake")
            with patch.object(merge.shutil, "copy2", side_effect=half_copy), self.assertRaises(OSError):
                merge.copy_new_file(str(source), str(Path(folder) / "sub" / "b"))
            self.assertEqual([], os.listdir(Path(folder) / "sub"))

            with (
                patch.object(merge.shutil, "copy2", side_effect=OSError("fake unreadable")),
                self.assertRaises(OSError),
            ):
                merge.copy_new_file(str(source), str(Path(folder) / "sub" / "c"))
            self.assertEqual([], os.listdir(Path(folder) / "sub"))

    def test_read_revision_with_two_rows(self):
        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('032'), ('033')"))
            self.assertIsNone(merge.read_revision(conn))
        engine.dispose()

    def test_open_engine_disposes_the_source_when_the_target_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.db"
            sqlite3.connect(source).close()
            with self.assertRaisesRegex(merge.MergeError, "target database file does not exist"):
                merge.merge_archives(
                    source=str(source), target_url=str(Path(folder) / "missing.db"), target_media=folder
                )

    def test_orphans_refuse_a_postgres_target(self):
        source, target = MagicMock(), MagicMock()
        source.dialect.name = "sqlite"
        target.dialect.name = "postgresql"
        with patch.object(merge, "count_orphans", return_value={}):
            merge.check_orphans(source, target, [1])
        with (
            patch.object(merge, "count_orphans", return_value={"media": 2, "reactions": 1}),
            self.assertRaisesRegex(merge.MergeError, r"parent row it lacks \(media: 2, reactions: 1\)"),
        ):
            merge.check_orphans(source, target, [1])

    def test_a_table_without_a_sequence_is_left_alone(self):
        target = MagicMock()
        target.execute.return_value.scalar.return_value = None
        merge.advance_sequence(target, "accounts", 5)
        self.assertEqual(1, target.execute.call_count)

    def test_a_sequence_is_only_moved_forward(self):
        # (last_value, is_called, floor) -> whether setval runs. The next value
        # is last_value + 1 once called, last_value itself before that.
        cases = {
            (10, True, 5): False,
            (5, True, 5): False,
            (4, True, 5): True,
            (5, False, 5): True,
            (6, False, 5): False,
        }
        for (last_value, is_called, floor), moves in cases.items():
            target = MagicMock()
            target.execute.return_value.scalar.return_value = "reactions_id_seq"
            target.execute.return_value.one.return_value = (last_value, is_called)
            merge.advance_sequence(target, "reactions", floor)
            self.assertEqual(3 if moves else 2, target.execute.call_count, (last_value, is_called, floor))
            if moves:
                self.assertEqual({"sequence": "reactions_id_seq", "floor": floor}, target.execute.call_args.args[1])

    def test_postgres_engines_are_built_read_only_for_the_source(self):
        with patch.object(merge.sa, "create_engine") as create_engine:
            merge.open_engine("postgresql+psycopg2://u@db/a", read_only=True)
            merge.open_engine("postgresql+psycopg2://u@db/a", read_only=False)
        read_only, writable = create_engine.call_args_list
        self.assertEqual({"options": "-c default_transaction_read_only=on"}, read_only.kwargs["connect_args"])
        self.assertNotIn("connect_args", writable.kwargs)


class TestCommandLine(unittest.TestCase):
    def test_the_parser(self):
        args = create_parser().parse_args(
            ["merge", "--source", "other.db", "--source-media", "m", "--account", "2", "--dry-run"]
        )
        self.assertEqual(
            ("merge", "other.db", "m", "2", True, False),
            (args.command, args.source, args.source_media, args.account, args.dry_run, args.add_missing_parents),
        )
        args = create_parser().parse_args(["merge", "--source", "other.db", "--add-missing-parents"])
        self.assertTrue(args.add_missing_parents)

    def test_success_prints_the_report(self):
        report = merge.MergeReport(dry_run=True, account_ids={1: 2}, rows={"messages": 3}, media=None)
        with (
            patch("sys.argv", ["telegram-archive", "merge", "--source", "other.db", "--dry-run"]),
            patch.object(merge, "merge_archives", return_value=report) as run,
            patch.object(merge, "target_database_url", return_value="sqlite:////fake/target.db"),
            patch("builtins.print") as printed,
        ):
            self.assertEqual(0, main())
        self.assertEqual("other.db", run.call_args.kwargs["source"])
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertFalse(run.call_args.kwargs["add_missing_parents"])
        self.assertEqual("sqlite:////fake/target.db", run.call_args.kwargs["target_url"])
        self.assertTrue(run.call_args.kwargs["target_media"].endswith("media"))
        self.assertIn("    messages: 3", [call.args[0] for call in printed.call_args_list])

    def test_a_refusal_exits_1(self):
        with (
            patch("sys.argv", ["telegram-archive", "merge", "--source", "other.db"]),
            patch.object(merge, "merge_archives", side_effect=merge.MergeError("fake reason")),
            patch.object(merge, "target_database_url", return_value="sqlite:////fake/target.db"),
            patch("sys.stderr") as stderr,
        ):
            self.assertEqual(1, main())
        self.assertIn("Merge refused: fake reason", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_a_failure_prints_only_the_error_type(self):
        with (
            patch("sys.argv", ["telegram-archive", "merge", "--source", "other.db"]),
            patch.object(merge, "merge_archives", side_effect=ValueError("fake detail with a path")),
            patch.object(merge, "target_database_url", return_value="sqlite:////fake/target.db"),
            patch("sys.stderr") as stderr,
        ):
            self.assertEqual(1, main())
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("Merge failed: ValueError", written)
        self.assertNotIn("fake detail", written)


# ---------------------------------------------------------------------------
# PostgreSQL: the same merge through the PostgreSQL branches (sequence resync,
# read-only source transaction, server-side cursors). Skips without a server,
# like every other PostgreSQL test; CI provides one.
# ---------------------------------------------------------------------------


def _postgres_archive(make_postgres_database, name: str, media: Path, seed) -> tuple[str, str]:
    async_url, sync_url = make_postgres_database(name)
    upgrade_to_head(async_url)
    asyncio.run(seed(async_url, media))
    return async_url, sync_url


def _merged_counts(sync_url: str) -> dict[str, int]:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return {
                table: conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE account_id IN (2, 3)")).scalar()
                for table in ("chats", "messages", "media", "message_versions", "reactions", "media_transcripts")
            }
    finally:
        engine.dispose()


def _pg_execute(sync_url: str, *statements: str) -> None:
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sa.text(statement))
    finally:
        engine.dispose()


def _next_account(async_url: str) -> int:
    """The id the target hands the next account that logs in."""

    async def ensure() -> int:
        manager, db = await open_adapter(async_url)
        try:
            return await db.ensure_account(telegram_user_id=900000077, env_index=4, label="Account N")
        finally:
            await manager.close()

    return asyncio.run(ensure())


def test_postgres_into_postgres(require_postgres, make_postgres_database, tmp_path):
    # Named "media", like every install's folder: an absolute stored path is cut after "/media/".
    source_media, target_media = tmp_path / "source" / "media", tmp_path / "target" / "media"
    _, source_url = _postgres_archive(
        make_postgres_database, "telegram_archive_merge_source", source_media, seed_source
    )
    target_async, target_url = _postgres_archive(
        make_postgres_database, "telegram_archive_merge_target", target_media, seed_target
    )

    # Ids up to 10 were handed out before; the merge must not move the sequence back.
    _pg_execute(target_url, "SELECT setval(pg_get_serial_sequence('accounts', 'id'), 10)")

    dry = merge.merge_archives(
        source=source_url,
        source_media=str(source_media),
        target_url=target_url,
        target_media=str(target_media),
        dry_run=True,
    )
    assert set(_merged_counts(target_url).values()) == {0}

    report = merge.merge_archives(
        source=source_url, source_media=str(source_media), target_url=target_async, target_media=str(target_media)
    )
    assert report.rows == dry.rows
    assert report.account_ids == {1: 2, 2: 3}
    assert _merged_counts(target_url) == {
        "chats": 3,
        "messages": 5,
        "media": 5,
        "message_versions": 1,
        "reactions": 1,
        "media_transcripts": 2,
    }
    assert (target_media / str(CHANNEL) / "doc_2.pdf").read_bytes() == DOC_BYTES

    # The sequence stayed ahead of every id, so the next login gets a fresh one.
    assert _next_account(target_async) == 11

    with pytest.raises(merge.MergeError, match="same Telegram account"):
        merge.merge_archives(source=source_url, target_url=target_url, target_media=str(target_media))


def test_sqlite_into_postgres(require_postgres, make_postgres_database, tmp_path):
    template = tmp_path / "source.db"
    upgrade_to_head(sqlite_url(template))
    source_media = tmp_path / "source" / "media"
    asyncio.run(seed_source(sqlite_url(template), source_media))
    target_media = tmp_path / "target" / "media"
    target_async, target_url = _postgres_archive(
        make_postgres_database, "telegram_archive_merge_target", target_media, seed_target
    )

    report = merge.merge_archives(
        source=str(template), source_media=str(source_media), target_url=target_url, target_media=str(target_media)
    )
    assert report.rows["messages"] == 5
    assert _merged_counts(target_url)["messages"] == 5
    # The accounts sequence moved past the merged ids.
    assert _next_account(target_async) == 4


def test_sqlite_into_postgres_with_sequences_behind_their_tables(require_postgres, make_postgres_database, tmp_path):
    template = tmp_path / "source.db"
    upgrade_to_head(sqlite_url(template))
    asyncio.run(seed_source(sqlite_url(template), tmp_path / "source" / "media"))
    target_media = tmp_path / "target" / "media"
    _, target_url = _postgres_archive(
        make_postgres_database, "telegram_archive_merge_target", target_media, seed_target
    )
    # Rows copied in with their ids, the way a data-only restore leaves them,
    # and every sequence set back to its start.
    _pg_execute(
        target_url,
        f"INSERT INTO reactions (id, account_id, message_id, chat_id, emoji, count) VALUES (1, 1, 1, {CHANNEL}, 'x', 1)",
        "INSERT INTO message_versions (id, account_id, message_id, chat_id, date, change_hash)"
        f" VALUES (1, 1, 1, {CHANNEL}, now(), 'fake')",
        f"INSERT INTO avatar_history (id, account_id, chat_id, seen_at) VALUES (1, 1, {CHANNEL}, now())",
        "INSERT INTO media_transcripts (id, account_id, media_id, status, requested_at, created_at)"
        " VALUES (1, 1, 'fake', 'done', now(), now())",
        *(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 1, false)"
            for table in ("reactions", "message_versions", "avatar_history", "media_transcripts")
        ),
    )

    report = merge.merge_archives(source=str(template), target_url=target_url, target_media=str(target_media))

    assert (report.rows["reactions"], report.rows["message_versions"], report.rows["media_transcripts"]) == (1, 1, 2)
    assert _merged_counts(target_url)["reactions"] == 1


def test_sqlite_orphans_are_refused_before_a_postgres_target_is_written(
    require_postgres, make_postgres_database, tmp_path
):
    template = tmp_path / "source.db"
    upgrade_to_head(sqlite_url(template))
    asyncio.run(seed_source(sqlite_url(template), tmp_path / "source" / "media"))
    with sqlite(template) as conn:
        conn.execute(
            "INSERT INTO reactions (account_id, message_id, chat_id, emoji, count) VALUES (1, 99, ?, 'x', 1)",
            (CHANNEL,),
        )
    target_media = tmp_path / "target" / "media"
    _, target_url = _postgres_archive(
        make_postgres_database, "telegram_archive_merge_target", target_media, seed_target
    )
    files = tree(target_media)

    with pytest.raises(merge.MergeError, match=r"parent row it lacks \(reactions: 1\).*--add-missing-parents"):
        merge.merge_archives(source=str(template), target_url=target_url, target_media=str(target_media))

    assert set(_merged_counts(target_url).values()) == {0}
    assert tree(target_media) == files

    report = merge.merge_archives(
        source=str(template), target_url=target_url, target_media=str(target_media), add_missing_parents=True
    )
    assert report.placeholders == {"messages": 1}
    assert report.rows["reactions"] == 2
    assert _merged_counts(target_url)["reactions"] == 2
    assert _merged_counts(target_url)["messages"] == 6


def test_postgres_into_sqlite_with_a_transcript_stored_after_its_copy(
    require_postgres, make_postgres_database, tmp_path
):
    source_media = tmp_path / "source" / "media"
    _, source_url = _postgres_archive(
        make_postgres_database, "telegram_archive_merge_source", source_media, seed_source
    )
    # An update writes a new row version at the end of the table, so a read
    # without an order returns the copy before the transcript it came from.
    _pg_execute(source_url, "UPDATE media_transcripts SET text = text WHERE copied_from_id IS NULL")
    target_db = tmp_path / "target" / "telegram_backup.db"
    target_media = tmp_path / "target" / "media"
    target_db.parent.mkdir(parents=True)
    upgrade_to_head(sqlite_url(target_db))
    asyncio.run(seed_target(sqlite_url(target_db), target_media))

    report = merge.merge_archives(
        source=source_url, source_media=str(source_media), target_url=str(target_db), target_media=str(target_media)
    )

    assert report.account_ids == {1: 2, 2: 3}
    assert report.transcript_links_dropped == 0
    with sqlite(target_db) as conn:
        rows = conn.execute(
            "SELECT id, account_id, copied_from_id FROM media_transcripts ORDER BY account_id"
        ).fetchall()
    (original_id, _, _), (_, copy_account, copy_link) = rows
    assert (copy_account, copy_link) == (3, original_id)
    assert (target_media / str(CHANNEL) / "doc_2.pdf").read_bytes() == DOC_BYTES
