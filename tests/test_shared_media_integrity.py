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
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
    inspect_media_row,
    repair_media_row,
)
from telegram_archive.message_utils import compute_file_hash, download_and_shard_media
from telegram_archive.migrate_shared_media import migrate_shared_media
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
