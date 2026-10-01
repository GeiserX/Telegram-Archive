"""A shared media file is never removed while something refers to it, and a
missing one is put back.

The production shape these tests rebuild, in the order it happened:

1. Before v4.0.5 a channel's media went to its plain-id folder (``1234567890``)
   as real files.
2. A later pass wrote a link in the marked folder (``-1001234567890``) to
   ``_shared/<name>``, and that ``_shared`` entry was gone afterwards.
3. Migration 013 rewrote every such row's path from the plain folder to the
   marked one, without moving a file. The row now named the broken link while
   the real file sat beside it, in the other id-form folder.

Nothing noticed: VERIFY_MEDIA trusted every symlink, the download path trusted
any entry that existed, and the viewer served the plain-folder copy through its
legacy fallback. The transcription drain was the first reader to fail.

Tests that need a database run on SQLite and on PostgreSQL (``real_adapter``).
"""

import errno
import hashlib
import io
import os
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_archive.__main__ import create_parser, run_check_media
from telegram_archive.media_integrity import (
    KEPT,
    MISSING,
    PLACEHOLDER,
    PRESENT,
    REFETCH,
    RESTORABLE,
    RESTORED,
    chat_folder_alternates,
    check_media,
    format_media_check,
    inspect_media_row,
    repair_media_row,
    restore_from_copy,
)
from telegram_archive.message_utils import _link_chat_entry, compute_file_hash, download_and_shard_media, place_copy
from telegram_archive.migrate_shared_media import _index_renamed_flat_links, migrate_shared_media
from telegram_archive.telegram_backup import TelegramBackup

CHANNEL = -1001234567890
LEGACY_FOLDER = "1234567890"
FILE = "5000000000000000001.jpg"
BYTES = b"archived photo bytes " * 16
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _production_shape(media_root, *, legacy_copy=True):
    """Lay out steps 1 and 2: the plain-folder file and the broken marked link."""
    legacy = os.path.join(media_root, LEGACY_FOLDER, FILE)
    if legacy_copy:
        os.makedirs(os.path.dirname(legacy), exist_ok=True)
        with open(legacy, "wb") as f:
            f.write(BYTES)
    os.makedirs(os.path.join(media_root, "_shared"), exist_ok=True)
    marked = os.path.join(media_root, str(CHANNEL))
    os.makedirs(marked, exist_ok=True)
    link = os.path.join(marked, FILE)
    os.symlink(os.path.join("..", "_shared", FILE), link)
    return legacy, link


async def _seed_row(adapter, *, file_path, account_id=1, chat_id=CHANNEL, message_id=7, **extra):
    """Step 3: the row as migration 013 left it, downloaded and pointing at the link."""
    await adapter.upsert_chat({"id": chat_id, "type": "channel", "title": "Channel A"}, account_id=account_id)
    await adapter.insert_message(
        {"id": message_id, "chat_id": chat_id, "text": "", "date": datetime(2025, 12, 6, 12), "raw_data": {}},
        account_id=account_id,
    )
    row = {
        "id": f"{chat_id}_{message_id}_photo",
        "message_id": message_id,
        "chat_id": chat_id,
        "type": "photo",
        "file_name": os.path.basename(file_path),
        "file_path": file_path,
        "file_size": len(BYTES),
        "downloaded": True,
        "download_date": datetime(2025, 12, 6, 12),
        **extra,
    }
    await adapter.insert_media(row, account_id=account_id)
    return row


async def _downloaded(adapter, chat_id=CHANNEL, account_id=1):
    return {m["id"]: m["downloaded"] for m in await adapter.get_media_for_chat(chat_id, account_id=account_id)}


# --- the production sequence, found and repaired ---------------------------


class TestProductionShape:
    async def test_check_media_finds_the_broken_link_and_repair_restores_it_from_the_legacy_folder(
        self, real_adapter, tmp_path
    ):
        media = str(tmp_path)
        legacy, link = _production_shape(media)
        original_target = os.readlink(link)
        await _seed_row(real_adapter, file_path=link)

        dry = await check_media(real_adapter, media)
        assert dry["broken_links"] == 1
        assert dry["restorable"] == 1
        assert dry["refetch"] == 0
        assert not os.path.exists(link)  # a dry run writes nothing

        repaired = await check_media(real_adapter, media, repair=True)
        assert repaired["restored"] == 1
        assert os.readlink(link) == original_target  # the link itself is never rewritten
        with open(link, "rb") as f:
            assert f.read() == BYTES
        with open(legacy, "rb") as f:
            assert f.read() == BYTES  # the copy it came from stays

        again = await check_media(real_adapter, media)
        assert again["present"] == 1
        assert again["broken_links"] == again["missing_files"] == 0

    async def test_a_row_with_no_copy_anywhere_is_handed_back_to_the_download(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        row = await _seed_row(real_adapter, file_path=link)

        assert await repair_media_row(real_adapter, row, media, account_id=1) == REFETCH
        assert await _downloaded(real_adapter) == {row["id"]: 0}
        # The row keeps the link it names, so the download fills that link's target.
        [kept] = await real_adapter.get_media_for_chat(CHANNEL, account_id=1)
        assert kept["file_path"] == link

    async def test_mark_for_redownload_clears_the_path_unless_asked_to_keep_it(self, real_adapter, tmp_path):
        media = str(tmp_path)
        row = await _seed_row(real_adapter, file_path=os.path.join(media, str(CHANNEL), FILE))
        await real_adapter.mark_media_for_redownload(row["id"], account_id=1, keep_path=True)
        [kept] = await real_adapter.get_media_for_chat(CHANNEL, account_id=1)
        assert (kept["downloaded"], kept["file_path"]) == (0, row["file_path"])
        await real_adapter.mark_media_for_redownload(row["id"], account_id=1)
        [cleared] = await real_adapter.get_media_for_chat(CHANNEL, account_id=1)
        assert (cleared["downloaded"], cleared["file_path"]) == (0, None)

    async def test_a_repair_that_cannot_mark_a_row_counts_it_and_goes_on(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        await _seed_row(real_adapter, file_path=link)
        real_adapter.mark_media_for_redownload = AsyncMock(side_effect=RuntimeError("db down"))

        report = await check_media(real_adapter, media, repair=True)

        assert (report["refetch"], report["refetch_failed"]) == (0, 1)
        assert "Could not mark:            1" in "\n".join(format_media_check(report, repair=True))
        dry = await check_media(real_adapter, media)
        assert (dry["refetch"], dry["refetch_failed"]) == (1, 0)

    async def test_a_missing_chat_entry_is_restored_as_a_file_from_the_legacy_folder(self, real_adapter, tmp_path):
        """No link at all: the row points into the marked folder, the file is in the plain one."""
        media = str(tmp_path)
        legacy = os.path.join(media, LEGACY_FOLDER, FILE)
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "wb") as f:
            f.write(BYTES)
        target = os.path.join(media, str(CHANNEL), FILE)
        row = await _seed_row(real_adapter, file_path=target)

        assert await repair_media_row(real_adapter, row, media, account_id=1) == RESTORED
        with open(target, "rb") as f:
            assert f.read() == BYTES

    async def test_another_row_with_the_same_hash_is_a_copy(self, real_adapter, tmp_path):
        """The same bytes archived by another account in another chat."""
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        twin = os.path.join(media, "-1009999999999", "twin.jpg")
        os.makedirs(os.path.dirname(twin))
        with open(twin, "wb") as f:
            f.write(BYTES)
        digest = compute_file_hash(twin)
        await _seed_row(real_adapter, file_path=twin, account_id=2, chat_id=-1009999999999, content_hash=digest)
        row = await _seed_row(real_adapter, file_path=link, content_hash=digest)

        assert await repair_media_row(real_adapter, row, media, account_id=1) == RESTORED
        with open(link, "rb") as f:
            assert f.read() == BYTES


class TestMetadataOnlyPlaceholders:
    """Locations, contacts and polls have no file. Older releases still gave
    some of these rows a ``.bin`` path and a link into ``_shared`` whose target
    never existed. Such a link is a leftover, not a missing file."""

    async def _geo_row(self, adapter, media):
        os.makedirs(os.path.join(media, "_shared"), exist_ok=True)
        folder = os.path.join(media, "-5")
        os.makedirs(folder)
        link = os.path.join(folder, "921316_20240922_171306.bin")
        os.symlink("../_shared/921316_20240922_171306.bin", link)
        row = await _seed_row(adapter, file_path=link, chat_id=-5, type="geo", file_size=0)
        return row, link

    async def test_check_media_counts_them_as_placeholders_and_repair_leaves_them(self, real_adapter, tmp_path):
        media = str(tmp_path)
        row, link = await self._geo_row(real_adapter, media)

        report = await check_media(real_adapter, media, repair=True)

        assert report["placeholders"] == 1
        assert report["broken_links"] == report["missing_files"] == report["refetch"] == 0
        assert "Placeholders:              1" in "\n".join(format_media_check(report, repair=True))
        assert await repair_media_row(real_adapter, row, media, account_id=1) == PLACEHOLDER
        assert await _downloaded(real_adapter, chat_id=-5) == {row["id"]: 1}
        assert os.path.islink(link) and not os.path.exists(link)

    async def test_verify_media_never_fetches_them(self, real_adapter, tmp_path):
        media = str(tmp_path)
        await self._geo_row(real_adapter, media)
        backup = TestVerifyMedia()._backup(real_adapter, media)

        await backup._verify_and_redownload_media()

        backup.client.get_messages.assert_not_awaited()
        backup._process_media.assert_not_awaited()


class TestVerifyMedia:
    def _backup(self, adapter, media):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = media
        backup.config.skip_media_chat_ids = set()
        backup.config.deduplicate_media = True
        backup.db = adapter
        backup.client = AsyncMock()
        backup._keep_replaced_media = AsyncMock(return_value=False)
        backup._process_media = AsyncMock()
        return backup

    async def test_verify_restores_a_broken_link_from_a_copy_without_asking_telegram(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media)
        await _seed_row(real_adapter, file_path=link)
        backup = self._backup(real_adapter, media)

        await backup._verify_and_redownload_media()

        with open(link, "rb") as f:
            assert f.read() == BYTES
        backup.client.get_messages.assert_not_awaited()
        backup._process_media.assert_not_awaited()

    async def test_verify_downloads_a_broken_link_with_no_copy_and_leaves_the_link_in_place(
        self, real_adapter, tmp_path
    ):
        """The link is not sidestepped: the download fills its _shared entry."""
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        original_target = os.readlink(link)
        row = await _seed_row(real_adapter, file_path=link)
        backup = self._backup(real_adapter, media)
        message = MagicMock(id=7, media=MagicMock())
        backup.client.get_messages = AsyncMock(return_value=[message])
        seen = []

        async def process(msg, chat_id):
            seen.append(os.readlink(link))
            return {**row, "downloaded": True}

        backup._process_media = AsyncMock(side_effect=process)

        await backup._verify_and_redownload_media()

        assert seen == [original_target]
        assert not os.path.lexists(link + ".verify-bak")

    async def test_verify_still_trusts_a_shared_entry_it_cannot_follow(self, real_adapter, tmp_path):
        """git-annex (#143): the _shared entry is present, as a link into an
        object store that is not mounted here. It is not broken; it is left alone."""
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        os.symlink("/nonexistent-annex/objects/x", os.path.join(media, "_shared", FILE))
        await _seed_row(real_adapter, file_path=link)
        backup = self._backup(real_adapter, media)

        await backup._verify_and_redownload_media()

        backup.client.get_messages.assert_not_awaited()
        assert inspect_media_row({"file_path": link, "file_name": FILE}, media).state == KEPT


class TestCopyMatching:
    def _row(self, path, **extra):
        return {"file_path": path, "file_name": os.path.basename(path), "file_size": len(BYTES), **extra}

    def _plant(self, media, folder, name, data=BYTES):
        path = os.path.join(media, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_a_present_file_is_present(self, tmp_path):
        path = self._plant(str(tmp_path), str(CHANNEL), FILE)
        assert inspect_media_row(self._row(path), str(tmp_path)).state == PRESENT

    def test_without_a_hash_a_message_id_name_is_never_matched(self, tmp_path):
        """``<message_id>_<type>.<ext>`` names a different file in every chat."""
        media = str(tmp_path)
        self._plant(media, LEGACY_FOLDER, "380_photo.jpg")
        row = self._row(os.path.join(media, str(CHANNEL), "380_photo.jpg"))
        assert inspect_media_row(row, media).state == MISSING

    def test_without_a_hash_the_size_must_match(self, tmp_path):
        media = str(tmp_path)
        self._plant(media, LEGACY_FOLDER, FILE, data=b"other")
        assert inspect_media_row(self._row(os.path.join(media, str(CHANNEL), FILE)), media).state == MISSING

    def test_a_known_hash_decides_alone(self, tmp_path):
        media = str(tmp_path)
        good = self._plant(media, LEGACY_FOLDER, "380_photo.jpg")
        row = self._row(os.path.join(media, str(CHANNEL), "380_photo.jpg"), content_hash=compute_file_hash(good))
        assert inspect_media_row(row, media).state == RESTORABLE
        wrong = self._row(os.path.join(media, str(CHANNEL), "380_photo.jpg"), content_hash="00" * 32)
        assert inspect_media_row(wrong, media).state == MISSING

    def test_the_other_id_forms_of_a_chat_folder(self):
        assert chat_folder_alternates("-1001234567890") == ["1234567890", "-1234567890"]
        assert chat_folder_alternates("1234567890") == ["-1234567890", "-1001234567890"]
        assert chat_folder_alternates("-42") == ["42", "-1000000000042"]
        assert chat_folder_alternates("_shared") == []
        assert chat_folder_alternates("0") == []


# --- nothing removes a shared file another reference still needs -----------


class TestSymlinkFailureNeverMovesTheBlob:
    async def test_another_chat_linking_the_new_blob_first_keeps_it(self, tmp_path):
        """The 2026-03 era code moved ``_shared/<name>`` into the chat folder
        when its own symlink failed. A chat that linked the blob in between kept
        a link to nothing. The blob is published before the link, so this race
        is real; the chat folder now gets a copy instead."""
        media = str(tmp_path)
        shared = os.path.join(media, "_shared")
        chat_a = os.path.join(media, "-1")
        chat_b = os.path.join(media, "-2")
        for folder in (shared, chat_a, chat_b):
            os.makedirs(folder)
        link_a = os.path.join(chat_a, FILE)
        real_symlink = os.symlink

        def symlink(src, dst, *args, **kwargs):
            if dst == os.path.join(chat_b, FILE):
                # Chat A ingests the same media in between and links the
                # freshly published blob, then chat B's own link fails.
                blob = os.path.normpath(os.path.join(chat_b, src))
                real_symlink(os.path.relpath(blob, chat_a), link_a)
                raise OSError(errno.EPERM, "Operation not permitted")
            return real_symlink(src, dst, *args, **kwargs)

        async def download(path):
            with open(path, "wb") as f:
                f.write(BYTES)
            return path

        db = AsyncMock()
        db.find_media_by_content_hash = AsyncMock(return_value=None)
        with patch("os.symlink", side_effect=symlink):
            await download_and_shard_media(
                db, download, shared, chat_b, FILE, os.path.join(chat_b, FILE), MagicMock(), account_id=1
            )

        with open(link_a, "rb") as f:
            assert f.read() == BYTES
        with open(os.path.join(chat_b, FILE), "rb") as f:
            assert f.read() == BYTES


async def _seed_youtube_row(adapter, *, account_id, chat_id, message_id, file_path, content_hash):
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "Group A"}, account_id=account_id)
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "text": "look",
            "date": datetime(2026, 9, 11, 12),
            "raw_data": {"webpage": {"url": YOUTUBE_URL}},
        },
        account_id=account_id,
    )
    await adapter.insert_media(
        {
            "id": f"{chat_id}_{message_id}_webpage",
            "message_id": message_id,
            "chat_id": chat_id,
            "type": "webpage",
            "file_path": file_path,
            "file_name": os.path.basename(file_path),
            "file_size": len(BYTES),
            "mime_type": "video/mp4",
            "content_hash": content_hash,
            "downloaded": True,
        },
        account_id=account_id,
    )


def _cleanup_backup(adapter, media, account_id):
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = account_id
    backup.config = MagicMock()
    backup.config.media_path = media
    backup.config.download_youtube_videos = False
    backup.config.youtube_videos_delete_existing = True
    backup.db = adapter
    return backup


def _blob_with_links(media, name, chats):
    blob = os.path.join(media, "_shared", hashlib.sha256(BYTES).hexdigest()[:2], name)
    os.makedirs(os.path.dirname(blob))
    with open(blob, "wb") as f:
        f.write(BYTES)
    links = []
    for chat in chats:
        folder = os.path.join(media, str(chat))
        os.makedirs(folder, exist_ok=True)
        link = os.path.join(folder, name)
        os.symlink(os.path.relpath(blob, folder), link)
        links.append(link)
    return blob, links


class TestYouTubeReapCountsEveryReference:
    async def test_a_blob_an_older_row_without_a_hash_still_names_survives(self, real_adapter, tmp_path):
        """Rows written before content hashing carry no hash, so counting by hash
        alone called the blob unreferenced. The name counts too. Here the older
        row's own link is already gone (the production shape): no link on disk
        protects the blob, only the row, and the blob is what a repair restores
        that link from."""
        media = str(tmp_path)
        name = "5000000000000000002.mp4"
        blob, (link_new, link_old) = _blob_with_links(media, name, [-1, -2])
        digest = compute_file_hash(blob)
        await _seed_youtube_row(
            real_adapter, account_id=2, chat_id=-1, message_id=1, file_path=link_new, content_hash=digest
        )
        old_row = await _seed_row(real_adapter, file_path=link_old, account_id=1, chat_id=-2, message_id=2)
        os.unlink(link_old)

        await _cleanup_backup(real_adapter, media, account_id=2)._cleanup_youtube_videos()

        assert not os.path.lexists(link_new)  # what the operator asked to remove
        assert os.path.isfile(blob)
        assert await repair_media_row(real_adapter, old_row, media, account_id=1) == RESTORED
        with open(link_old, "rb") as f:
            assert f.read() == BYTES

    async def test_a_blob_a_chat_folder_still_links_to_survives(self, real_adapter, tmp_path):
        """A link no row describes still keeps its blob."""
        media = str(tmp_path)
        name = "5000000000000000003.mp4"
        blob, (link_new, link_orphan) = _blob_with_links(media, name, [-1, -3])
        await _seed_youtube_row(
            real_adapter,
            account_id=1,
            chat_id=-1,
            message_id=1,
            file_path=link_new,
            content_hash=compute_file_hash(blob),
        )

        await _cleanup_backup(real_adapter, media, account_id=1)._cleanup_youtube_videos()

        assert os.path.isfile(blob)
        assert os.path.isfile(link_orphan)

    async def test_the_last_reference_gone_frees_the_blob(self, real_adapter, tmp_path):
        """The removal the operator asked for still happens."""
        media = str(tmp_path)
        name = "5000000000000000004.mp4"
        blob, (link,) = _blob_with_links(media, name, [-1])
        await _seed_youtube_row(
            real_adapter, account_id=1, chat_id=-1, message_id=1, file_path=link, content_hash=compute_file_hash(blob)
        )

        await _cleanup_backup(real_adapter, media, account_id=1)._cleanup_youtube_videos()

        assert not os.path.lexists(link)
        assert not os.path.exists(blob)


class TestOperatorRemovalsKeepWhatAnotherAccountUses:
    async def test_deleting_one_accounts_copy_keeps_the_chat_folder_the_other_uses(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _blob, (link,) = _blob_with_links(media, FILE, [-5])
        await _seed_row(real_adapter, file_path=link, account_id=1, chat_id=-5)
        await _seed_row(real_adapter, file_path=link, account_id=2, chat_id=-5)

        await real_adapter.delete_chat_and_related_data(-5, media, account_id=2)
        assert os.path.isfile(link)

        await real_adapter.delete_chat_and_related_data(-5, media, account_id=1)
        assert not os.path.exists(os.path.join(media, "-5"))

    async def test_skip_media_delete_existing_keeps_an_entry_the_other_account_names(self, real_adapter, tmp_path):
        media = str(tmp_path)
        blob, (link,) = _blob_with_links(media, FILE, [-6])
        await _seed_row(real_adapter, file_path=link, account_id=1, chat_id=-6)
        await _seed_row(real_adapter, file_path=link, account_id=2, chat_id=-6)
        backup = _cleanup_backup(real_adapter, media, account_id=2)

        await backup._cleanup_existing_media(-6)

        assert os.path.isfile(link)
        assert await _downloaded(real_adapter, chat_id=-6, account_id=2) == {}
        assert await _downloaded(real_adapter, chat_id=-6, account_id=1) == {"-6_7_photo": 1}
        assert os.path.isfile(blob)

    async def test_reference_counts_see_names_hashes_and_kept_versions(self, real_adapter, tmp_path):
        media = str(tmp_path)
        path = os.path.join(media, "-7", FILE)
        await _seed_row(real_adapter, file_path=path, account_id=1, chat_id=-7)
        counts = await real_adapter.count_shared_blob_references([(FILE, None), ("unknown.jpg", "ff" * 32)])
        assert counts == {FILE: 1}
        assert await real_adapter.referenced_file_paths([path, path + ".x"]) == {path}
        assert await real_adapter.count_media_rows_in_folder(-8, [os.path.join(media, "-7") + os.sep]) == 1
        assert await real_adapter.count_media_rows_in_folder(-8, [os.path.join(media, "-9") + os.sep]) == 0


class TestShardingMigrationRelinksEveryLink:
    def test_a_link_named_differently_from_its_flat_blob_is_relinked_before_the_blob_moves(self, tmp_path):
        """Content-hash dedup reused an existing blob for a duplicate under
        another name. The per-name probe never saw such links, so removing the
        flat blob left them pointing at nothing."""
        media = str(tmp_path)
        shared = os.path.join(media, "_shared")
        os.makedirs(shared)
        with open(os.path.join(shared, "a.jpg"), "wb") as f:
            f.write(BYTES)
        chat = os.path.join(media, "-1")
        os.makedirs(chat)
        same_name = os.path.join(chat, "a.jpg")
        other_name = os.path.join(chat, "b.jpg")
        os.symlink("../_shared/a.jpg", same_name)
        os.symlink("../_shared/a.jpg", other_name)

        assert migrate_shared_media(media) == 1

        for link in (same_name, other_name):
            with open(link, "rb") as f:
                assert f.read() == BYTES
        assert not os.path.lexists(os.path.join(shared, "a.jpg"))


class TestCheckMediaCommand:
    async def _run(self, real_adapter, tmp_path, *argv):
        args = create_parser().parse_args(["check-media", *argv])
        env = {"DATABASE_URL": real_adapter.db_manager.database_url, "BACKUP_PATH": str(tmp_path)}
        out = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out):
            code = await run_check_media(args)
        return code, out.getvalue()

    async def test_dry_run_reports_and_exits_1_then_repair_fixes_and_the_next_check_is_clean(
        self, real_adapter, tmp_path
    ):
        media = os.path.join(str(tmp_path), "media")
        _legacy, link = _production_shape(media)
        await _seed_row(real_adapter, file_path=link)

        code, out = await self._run(real_adapter, tmp_path)
        assert code == 1
        assert out.startswith("Media check (dry run")
        assert "Broken links:              1" in out
        assert not os.path.exists(link)

        code, out = await self._run(real_adapter, tmp_path, "--repair")
        assert code == 0
        assert "Restored from a copy:      1" in out
        assert os.path.isfile(link)

        code, out = await self._run(real_adapter, tmp_path, "--chat-id", str(CHANNEL))
        assert code == 0
        assert "Files in place:            1" in out

    async def test_a_chat_filter_skips_other_chats(self, real_adapter, tmp_path):
        media = os.path.join(str(tmp_path), "media")
        _legacy, link = _production_shape(media)
        await _seed_row(real_adapter, file_path=link)

        code, out = await self._run(real_adapter, tmp_path, "-c", "-1009999999999")
        assert code == 0
        assert "Rows checked:              0" in out

    async def test_a_mark_failure_makes_the_repair_exit_1(self, tmp_path):
        report = dict.fromkeys(
            ("checked", "present", "broken_links", "missing_files", "restorable", "refetch", "restored", "kept"), 0
        )
        report.update(restore_failed=0, refetch_failed=1, placeholders=0)
        args = create_parser().parse_args(["check-media", "--repair"])
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"BACKUP_PATH": str(tmp_path)}),
            patch("telegram_archive.db.init_database", AsyncMock(return_value=MagicMock())),
            patch("telegram_archive.db.close_database", AsyncMock()),
            patch("telegram_archive.media_integrity.check_media", AsyncMock(return_value=report)),
            redirect_stdout(out),
        ):
            assert await run_check_media(args) == 1
        assert "Could not mark:            1" in out.getvalue()

    async def test_a_database_error_prints_its_type_only_and_exits_1(self, tmp_path):
        args = create_parser().parse_args(["check-media"])
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"BACKUP_PATH": str(tmp_path)}),
            patch("telegram_archive.db.init_database", AsyncMock(side_effect=RuntimeError("/secret/path"))),
            patch("telegram_archive.db.close_database", AsyncMock()),
            redirect_stderr(err),
        ):
            assert await run_check_media(args) == 1
        assert err.getvalue().strip() == "Media check failed: RuntimeError"

    def test_main_dispatches_check_media(self):
        from telegram_archive.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "check-media", "--repair"]),
            patch("telegram_archive.__main__.run_check_media", new=MagicMock(return_value="coroutine")) as run,
            patch("telegram_archive.__main__.asyncio.run", return_value=0) as mock_run,
        ):
            assert main() == 0
        assert run.call_args.args[0].repair is True
        mock_run.assert_called_once_with("coroutine")


# --- error paths and the remaining branches --------------------------------


class TestRepairEdges:
    async def test_repair_mode_marks_a_row_with_no_copy_and_keeps_its_path(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        row = await _seed_row(real_adapter, file_path=link)

        report = await check_media(real_adapter, media, repair=True)

        assert (report["refetch"], report["refetch_failed"]) == (1, 0)
        [kept] = await real_adapter.get_media_for_chat(CHANNEL, account_id=1)
        assert (kept["id"], kept["downloaded"], kept["file_path"]) == (row["id"], 0, link)

    async def test_a_restore_that_cannot_write_is_counted_and_shown(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media)
        await _seed_row(real_adapter, file_path=link)

        with patch("telegram_archive.media_integrity.place_copy", side_effect=PermissionError("denied")):
            report = await check_media(real_adapter, media, repair=True)

        assert (report["restored"], report["restore_failed"]) == (0, 1)
        assert "Could not restore:         1" in "\n".join(format_media_check(report, repair=True))
        assert not os.path.exists(link)

    async def test_entries_it_cannot_read_and_rows_not_downloaded_are_not_counted_as_broken(
        self, real_adapter, tmp_path
    ):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        os.symlink("/nonexistent-annex/objects/x", os.path.join(media, "_shared", FILE))
        await _seed_row(real_adapter, file_path=link)
        pending = os.path.join(media, str(CHANNEL), "5000000000000000009.jpg")
        await _seed_row(real_adapter, file_path=pending, message_id=8, downloaded=False)

        report = await check_media(real_adapter, media)

        assert (report["checked"], report["kept"], report["broken_links"], report["missing_files"]) == (1, 1, 0, 0)
        assert "Left alone:                1" in "\n".join(format_media_check(report, repair=False))

    async def test_an_unreadable_account_list_falls_back_to_the_default_account(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media)
        await _seed_row(real_adapter, file_path=link)
        real_adapter.get_account_ids = AsyncMock(side_effect=RuntimeError("no accounts table"))

        report = await check_media(real_adapter, media)

        assert (report["checked"], report["restorable"]) == (1, 1)

    async def test_a_row_that_cannot_be_marked_is_reported_missing(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        row = await _seed_row(real_adapter, file_path=link)
        real_adapter.mark_media_for_redownload = AsyncMock(side_effect=RuntimeError("db down"))

        assert await repair_media_row(real_adapter, row, media, account_id=1) == MISSING
        assert await _downloaded(real_adapter) == {row["id"]: 1}

    async def test_a_hash_with_no_twin_or_a_failed_lookup_still_goes_back_to_download(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        row = await _seed_row(real_adapter, file_path=link, content_hash="ab" * 32)
        assert await repair_media_row(real_adapter, row, media, account_id=1) == REFETCH

        real_adapter.get_media_paths_by_content_hash = AsyncMock(side_effect=RuntimeError("db down"))
        assert await repair_media_row(real_adapter, row, media, account_id=1) == REFETCH

    async def test_a_db_without_the_hash_lookup_still_repairs_from_disk_and_marks_the_rest(self, tmp_path):
        """A minimal adapter with no hash lookup: disk copies still count, and a
        row whose hash no disk copy matches is handed back to the download."""
        media = str(tmp_path)
        _legacy, link = _production_shape(media)

        class MinimalDb:
            def __init__(self):
                self.marked = []

            async def mark_media_for_redownload(self, media_id, **kwargs):
                self.marked.append((media_id, kwargs["keep_path"]))

        db = MinimalDb()
        row = {"id": "x", "file_path": link, "file_name": FILE, "file_size": len(BYTES), "content_hash": "ff" * 32}
        assert await repair_media_row(db, row, media, account_id=1) == REFETCH
        assert db.marked == [("x", True)]
        assert not os.path.exists(link)

        row["content_hash"] = None
        assert await repair_media_row(db, row, media, account_id=1) == RESTORED
        assert os.path.isfile(link)

    def test_a_row_without_a_path_is_missing(self, tmp_path):
        assert inspect_media_row({"file_path": None, "file_name": FILE}, str(tmp_path)).state == MISSING

    def test_the_hash_bucket_copy_is_linked_into_a_missing_chat_entry(self, tmp_path):
        media = str(tmp_path)
        digest = hashlib.sha256(BYTES).hexdigest()
        blob = os.path.join(media, "_shared", digest[:2], FILE)
        os.makedirs(os.path.dirname(blob))
        with open(blob, "wb") as f:
            f.write(BYTES)
        target = os.path.join(media, str(CHANNEL), FILE)
        inspection = inspect_media_row({"file_path": target, "file_name": FILE, "content_hash": digest}, media)

        assert inspection.state == RESTORABLE
        assert restore_from_copy(inspection, media)
        assert os.path.islink(target)
        with open(target, "rb") as f:
            assert f.read() == BYTES

    def test_a_copy_under_the_rows_own_file_name_counts_when_the_path_name_differs(self, tmp_path):
        media = str(tmp_path)
        legacy = os.path.join(media, LEGACY_FOLDER, FILE)
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "wb") as f:
            f.write(BYTES)
        row = {"file_path": os.path.join(media, str(CHANNEL), "renamed.jpg"), "file_name": FILE}

        inspection = inspect_media_row(row, media)  # no size recorded: any non-empty copy

        assert (inspection.state, inspection.source) == (RESTORABLE, legacy)

    def test_restore_reports_what_it_could_not_do(self, tmp_path):
        media = str(tmp_path)
        assert restore_from_copy(inspect_media_row({"file_path": None}, media), media) is False

        legacy, link = _production_shape(media)
        inspection = inspect_media_row({"file_path": link, "file_name": FILE, "file_size": len(BYTES)}, media)
        target = inspection.restore_to

        def appears_meanwhile(source, dest):
            os.link(source, dest)
            raise FileExistsError(errno.EEXIST, "exists")

        with patch("telegram_archive.media_integrity.place_copy", side_effect=appears_meanwhile):
            assert restore_from_copy(inspection, media) is True
        assert os.path.isfile(target)


class TestPlaceCopy:
    def _source(self, tmp_path):
        source = tmp_path / "source.bin"
        source.write_bytes(BYTES)
        return str(source)

    def test_without_hardlinks_it_copies_under_a_private_name(self, tmp_path):
        source = self._source(tmp_path)
        dest = str(tmp_path / "out" / "dest.bin")
        with patch("telegram_archive.message_utils.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            place_copy(source, dest)
        with open(dest, "rb") as f:
            assert f.read() == BYTES
        assert os.stat(dest).st_ino != os.stat(source).st_ino
        assert os.listdir(os.path.dirname(dest)) == ["dest.bin"]

    def test_it_never_replaces_an_existing_entry(self, tmp_path):
        source = self._source(tmp_path)
        dest = tmp_path / "dest.bin"
        dest.write_bytes(b"kept")
        with pytest.raises(FileExistsError):
            place_copy(source, str(dest))
        assert dest.read_bytes() == b"kept"

    def test_a_destination_written_during_the_copy_is_not_replaced(self, tmp_path):
        source = self._source(tmp_path)
        late = tmp_path / "late.bin"
        real_copy2 = shutil.copy2

        def copy_then_someone_writes_dest(src, dst):
            real_copy2(src, dst)
            late.write_bytes(b"other writer")

        with (
            patch("telegram_archive.message_utils.os.link", side_effect=OSError(errno.EXDEV, "cross-device")),
            patch("telegram_archive.message_utils.shutil.copy2", side_effect=copy_then_someone_writes_dest),
            pytest.raises(FileExistsError),
        ):
            place_copy(source, str(late))
        assert late.read_bytes() == b"other writer"
        assert not [n for n in os.listdir(tmp_path) if n.endswith(".part")]

    def test_a_broken_link_restored_by_another_task_is_left_as_it_is(self, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media)
        blob = self._source(tmp_path)
        target = os.path.join(media, "_shared", FILE)
        with open(target, "wb") as f:
            f.write(b"restored first")
        _link_chat_entry(blob, os.path.dirname(link), link, target, MagicMock())
        with open(link, "rb") as f:
            assert f.read() == b"restored first"


class TestRemainingGuards:
    async def test_inputs_with_nothing_to_ask_return_empty(self, real_adapter):
        assert await real_adapter.get_media_paths_by_content_hash("") == []
        assert await real_adapter.count_shared_blob_references([]) == {}
        assert await real_adapter.referenced_file_paths([]) == set()

    def test_the_renamed_link_index_skips_a_folder_it_cannot_read(self, tmp_path):
        assert _index_renamed_flat_links([str(tmp_path / "gone")]) == {}

    async def test_skip_media_cleanup_keeps_every_file_when_the_reference_check_fails(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _blob, (link,) = _blob_with_links(media, FILE, [-6])
        await _seed_row(real_adapter, file_path=link, account_id=2, chat_id=-6)
        real_adapter.referenced_file_paths = AsyncMock(side_effect=RuntimeError("db down"))

        await _cleanup_backup(real_adapter, media, account_id=2)._cleanup_existing_media(-6)

        assert os.path.isfile(link)

    async def test_verify_keeps_the_path_of_a_broken_link_it_could_not_fetch(self, real_adapter, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        row = await _seed_row(real_adapter, file_path=link)
        backup = TestVerifyMedia()._backup(real_adapter, media)
        backup.client.get_messages = AsyncMock(return_value=[MagicMock(id=7, media=MagicMock())])
        backup._process_media = AsyncMock(return_value=None)

        await backup._verify_and_redownload_media()

        [kept] = await real_adapter.get_media_for_chat(CHANNEL, account_id=1)
        assert (kept["id"], kept["downloaded"], kept["file_path"]) == (row["id"], 0, link)

    def test_a_refetch_that_cannot_fill_the_old_target_keeps_the_new_path(self, tmp_path):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        fresh = os.path.join(media, str(CHANNEL), "fresh.jpg")
        with open(fresh, "wb") as f:
            f.write(BYTES)
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock(media_path=media)
        existing = {"id": "r", "downloaded": 0, "file_path": link}
        result = {"id": "r", "downloaded": True, "file_path": fresh}

        with patch("telegram_archive.telegram_backup.place_copy", side_effect=PermissionError("denied")):
            assert backup._fill_broken_row_path(existing, result)["file_path"] == fresh

        def appears_meanwhile(source, dest):
            os.link(source, dest)
            raise FileExistsError(errno.EEXIST, "exists")

        with patch("telegram_archive.telegram_backup.place_copy", side_effect=appears_meanwhile):
            assert backup._fill_broken_row_path(existing, result)["file_path"] == link
        assert backup._fill_broken_row_path(None, result) is result


class TestBackupTellsTheOperatorAboutBrokenLinks:
    """A backup run that meets downloaded rows behind broken links says so once.

    Without check-media, VERIFY_MEDIA or transcription, nothing else would tell
    the operator. Only rows the run reads are counted, and the warning carries
    a count and the command, never a path, chat id or file name.
    """

    def _backup(self, media, *, verify_media=False):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock(media_path=media, verify_media=verify_media, download_youtube_videos=False)
        backup._broken_links_met = set()
        return backup

    async def _meet(self, backup, row, message_id=7):
        """The reuse check of _media_row_for, stopped right after it (a declined
        YouTube preview returns None before any download)."""
        with patch("telegram_archive.telegram_backup.is_youtube_preview_video", return_value=True):
            return await backup._media_row_for(MagicMock(id=message_id), CHANNEL, MagicMock(), "photo", None, row)

    async def test_a_run_counts_each_broken_row_once_and_warns_with_the_count_only(self, tmp_path, caplog):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        other = os.path.join(media, str(CHANNEL), "5000000000000000002.jpg")
        os.symlink(os.path.join("..", "_shared", "5000000000000000002.jpg"), other)
        backup = self._backup(media)
        first = {"id": f"{CHANNEL}_7_photo", "downloaded": 1, "file_path": link}
        second = {"id": f"{CHANNEL}_8_photo", "downloaded": 1, "file_path": other}

        # The broken row is not reused: the run goes on to fetch it.
        assert await self._meet(backup, first) is None
        await self._meet(backup, first)
        await self._meet(backup, second, message_id=8)
        with caplog.at_level("WARNING", logger="telegram_archive.telegram_backup"):
            backup._warn_broken_media_links()

        [record] = [r for r in caplog.records if "check-media" in r.getMessage()]
        message = record.getMessage()
        assert "met 2 downloaded media file(s)" in message
        assert "`telegram-archive check-media`" in message
        for secret in (str(CHANNEL), LEGACY_FOLDER, FILE, media):
            assert secret not in message
        # Nothing on disk changed: the link is still there, pointing where it did.
        assert os.readlink(link) == os.path.join("..", "_shared", FILE)

    async def test_a_present_file_is_reused_and_a_clean_run_says_nothing(self, tmp_path, caplog):
        media = str(tmp_path)
        path = os.path.join(media, str(CHANNEL), FILE)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(BYTES)
        backup = self._backup(media)
        row = {"id": f"{CHANNEL}_7_photo", "downloaded": 1, "file_path": path}

        assert await self._meet(backup, row) is row
        with caplog.at_level("WARNING", logger="telegram_archive.telegram_backup"):
            backup._warn_broken_media_links()

        assert backup._broken_links_met == set()
        assert not [r for r in caplog.records if "check-media" in r.getMessage()]

    async def test_with_verify_media_the_run_repairs_and_adds_no_warning(self, tmp_path, caplog):
        media = str(tmp_path)
        _legacy, link = _production_shape(media, legacy_copy=False)
        backup = self._backup(media, verify_media=True)

        await self._meet(backup, {"id": f"{CHANNEL}_7_photo", "downloaded": 1, "file_path": link})
        with caplog.at_level("WARNING", logger="telegram_archive.telegram_backup"):
            backup._warn_broken_media_links()

        assert not [r for r in caplog.records if "check-media" in r.getMessage()]

    async def test_each_run_starts_from_zero_and_warns_even_when_it_fails(self, tmp_path):
        backup = self._backup(str(tmp_path))
        backup._broken_links_met = {"left over from the run before"}
        backup.client = MagicMock()
        backup.client.start = AsyncMock(side_effect=RuntimeError("offline"))
        backup.db = AsyncMock()
        seen = []
        backup._warn_broken_media_links = lambda: seen.append(set(backup._broken_links_met))

        with pytest.raises(RuntimeError):
            await backup.backup_all()

        assert seen == [set()]
