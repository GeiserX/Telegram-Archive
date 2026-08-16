"""Media symlinks must always resolve to real bytes.

Two ways an archived attachment used to become permanently unopenable:

* concurrent ingest of the same document published the blob under the plain
  ``_shared/<name>`` before moving it into its shard bucket, so a second task
  could symlink its chat dir to a name that was about to disappear;
* the flat-to-sharded migration moved a symlink one directory deeper without
  rewriting its relative target, and sealed its marker anyway.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.message_utils import download_and_shard_media, resolve_shared_file_path
from src.migrate_shared_media import SHARD_MARKER, migrate_shared_media

logger = logging.getLogger(__name__)


@unittest.skipIf(os.name == "nt", "Symlinks require administrator privileges on Windows")
class TestSharedStorePublishIsAtomic(unittest.TestCase):
    """download_and_shard_media must never expose an intermediate blob name."""

    FILE_NAME = "999_holiday.jpg"
    CONTENT = b"holiday photo bytes"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.tmpdir, "media")
        self.shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(self.shared_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _chat_dir(self, chat_id):
        chat_dir = os.path.join(self.media_path, str(chat_id))
        os.makedirs(chat_dir, exist_ok=True)
        return chat_dir

    async def _download(self, tmp_path):
        with open(tmp_path, "wb") as f:
            f.write(self.CONTENT)
        return tmp_path

    def _ingest(self, db, chat_dir):
        return download_and_shard_media(
            db=db,
            download_coro=self._download,
            shared_dir=self.shared_dir,
            chat_media_dir=chat_dir,
            file_name=self.FILE_NAME,
            file_path=os.path.join(chat_dir, self.FILE_NAME),
            logger=logger,
            account_id=1,
        )

    def _flat_entries(self):
        return sorted(name for name in os.listdir(self.shared_dir) if not name.endswith(".part"))

    def test_blob_is_not_discoverable_before_it_is_final(self):
        observed = {}

        async def _observe(content_hash, *, account_id):
            # Runs at the dedup await — exactly where a competing ingest gets to
            # look at the shared store while this download is still in flight.
            observed["resolved"] = resolve_shared_file_path(self.shared_dir, self.FILE_NAME, None)
            observed["flat_entries"] = self._flat_entries()
            return None

        db = AsyncMock()
        db.find_media_by_content_hash = AsyncMock(side_effect=_observe)
        chat_dir = self._chat_dir(100)

        shared_file_path, content_hash = self._run(self._ingest(db, chat_dir))

        assert observed["resolved"] is None, "in-flight blob was discoverable under its plain shared name"
        assert observed["flat_entries"] == [], "in-flight blob was published into the shared store root"
        assert content_hash == hashlib.sha256(self.CONTENT).hexdigest()
        assert shared_file_path == os.path.join(self.shared_dir, content_hash[:2], self.FILE_NAME)
        assert os.path.exists(shared_file_path)

    def test_concurrent_ingest_leaves_no_dangling_chat_symlink(self):
        first_at_dedup = asyncio.Event()
        second_done = asyncio.Event()

        async def _park(content_hash, *, account_id):
            first_at_dedup.set()
            await second_done.wait()
            return None

        db_first = AsyncMock()
        db_first.find_media_by_content_hash = AsyncMock(side_effect=_park)
        db_second = AsyncMock()
        db_second.find_media_by_content_hash = AsyncMock(return_value=None)

        chat_first = self._chat_dir(100)
        chat_second = self._chat_dir(200)

        async def scenario():
            first = asyncio.create_task(self._ingest(db_first, chat_first))
            await first_at_dedup.wait()
            # The second consumer runs start-to-finish while the first is parked
            # mid-ingest — the window in which the transient name was visible.
            second_result = await self._ingest(db_second, chat_second)
            second_done.set()
            return await first, second_result

        (first_path, _), (second_path, second_hash) = self._run(scenario())

        second_link = os.path.join(chat_second, self.FILE_NAME)
        assert os.path.islink(second_link)
        assert os.path.exists(second_link), "second consumer's chat symlink dangles"
        with open(second_link, "rb") as f:
            assert f.read() == self.CONTENT
        assert second_hash == hashlib.sha256(self.CONTENT).hexdigest()
        assert os.path.exists(second_path)
        # And the first consumer's own link survived the second's publish.
        assert os.path.exists(os.path.join(chat_first, self.FILE_NAME))
        assert os.path.exists(first_path)

    def test_unhashable_download_is_published_under_the_clean_name(self):
        # Hashing can fail (transient read error) — the blob must still land on a
        # clean name, never keep the private ".part" one (#175).
        db = AsyncMock()
        db.find_media_by_content_hash = AsyncMock(return_value=None)
        chat_dir = self._chat_dir(100)

        with patch("src.message_utils.compute_file_hash", return_value=None):
            shared_file_path, content_hash = self._run(self._ingest(db, chat_dir))

        assert content_hash is None
        assert shared_file_path == os.path.join(self.shared_dir, self.FILE_NAME)
        assert self._flat_entries() == [self.FILE_NAME]
        assert os.listdir(self.shared_dir) == [self.FILE_NAME]
        assert os.path.exists(os.path.join(chat_dir, self.FILE_NAME))


class TestMigrationKeepsSymlinksResolvable(unittest.TestCase):
    """Sharding migration must not break the media it relocates."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.tmpdir, "media")
        self.shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(self.shared_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_file(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _bucket_path(self, content, name):
        digest = hashlib.sha256(content.encode()).hexdigest()
        return os.path.join(self.shared_dir, digest[:2], name)

    @unittest.skipIf(os.name == "nt", "Symlinks require administrator privileges on Windows")
    def test_relative_symlink_still_resolves_after_migration(self):
        # git-annex shape: the shared entry is a relative link out of the tree.
        content = "annex object data"
        blob = self._create_file(os.path.join(self.tmpdir, "annexstore", "obj"), content)
        link_path = os.path.join(self.shared_dir, "annexed.jpg")
        os.symlink(os.path.relpath(blob, self.shared_dir), link_path)

        chat_dir = os.path.join(self.media_path, "-1001234")
        os.makedirs(chat_dir)
        chat_link = os.path.join(chat_dir, "annexed.jpg")
        os.symlink(os.path.relpath(link_path, chat_dir), chat_link)

        count = migrate_shared_media(self.media_path)

        assert count == 1
        sharded = self._bucket_path(content, "annexed.jpg")
        assert os.path.islink(sharded)
        assert os.path.exists(sharded), "relocated shared symlink no longer resolves"
        assert os.path.exists(chat_link), "chat symlink no longer resolves"
        with open(chat_link) as f:
            assert f.read() == content

    @unittest.skipIf(os.name == "nt", "Symlinks require administrator privileges on Windows")
    def test_absolute_symlink_still_resolves_after_migration(self):
        content = "absolute target data"
        blob = self._create_file(os.path.join(self.tmpdir, "elsewhere", "obj"), content)
        link_path = os.path.join(self.shared_dir, "external.jpg")
        os.symlink(blob, link_path)

        count = migrate_shared_media(self.media_path)

        assert count == 1
        sharded = self._bucket_path(content, "external.jpg")
        assert os.path.islink(sharded)
        assert os.path.exists(sharded)
        assert os.readlink(sharded) == blob

    def test_plain_file_still_resolves_after_migration(self):
        content = "plain blob"
        flat = self._create_file(os.path.join(self.shared_dir, "photo.jpg"), content)

        count = migrate_shared_media(self.media_path)

        assert count == 1
        assert not os.path.lexists(flat)
        sharded = self._bucket_path(content, "photo.jpg")
        assert os.path.isfile(sharded)
        with open(sharded) as f:
            assert f.read() == content


class TestMigrationContainsFilesystemErrors(unittest.TestCase):
    """A failing file must not abort startup, and must be retried later."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_path = os.path.join(self.tmpdir, "media")
        self.shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(self.shared_dir)
        self.marker = os.path.join(self.shared_dir, SHARD_MARKER)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_file(self, name, content):
        path = os.path.join(self.shared_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_oserror_on_one_file_does_not_abort_the_others(self):
        good = self._create_file("good.jpg", "good data")
        bad = self._create_file("bad.jpg", "bad data")
        real_replace = os.replace

        def _replace(src, dst):
            if os.path.basename(src) == "bad.jpg":
                raise PermissionError(13, "Permission denied")
            return real_replace(src, dst)

        with patch("src.migrate_shared_media.os.replace", side_effect=_replace):
            count = migrate_shared_media(self.media_path)

        assert count == 1
        assert not os.path.lexists(good)
        assert os.path.isfile(bad), "the failing file must be left where it was"
        assert not os.path.exists(self.marker), "marker sealed a migration that left work behind"

        # Next start (failure gone) picks the leftover up and seals the marker.
        count2 = migrate_shared_media(self.media_path)
        assert count2 == 1
        assert not os.path.lexists(bad)
        assert os.path.exists(self.marker)

    @unittest.skipIf(os.name == "nt", "Symlinks require administrator privileges on Windows")
    def test_unhashable_entry_withholds_the_marker(self):
        link_path = os.path.join(self.shared_dir, "broken.jpg")
        os.symlink("../../nowhere/missing.bin", link_path)

        count = migrate_shared_media(self.media_path)

        assert count == 0
        assert os.path.islink(link_path)
        assert not os.path.exists(self.marker), "unreadable entry was abandoned instead of retried"
