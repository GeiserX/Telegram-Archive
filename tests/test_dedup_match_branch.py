"""The dedupe MATCH branch: reuse, guards and fallbacks, on real files.

Every pre-existing dedup test pinned find_media_by_content_hash to None, so
the entire reuse path ran dark: a regression there ships green and the
failure mode is destroyed archive media, not a crash.
"""

import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from src.message_utils import deduplicate_shared_file, resolve_shared_file_path

CONTENT = b"identical media bytes for dedup"
SHA = hashlib.sha256(CONTENT).hexdigest()


class TestDeduplicateSharedFileMatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shared = os.path.join(self.tmp.name, "_shared")
        os.makedirs(os.path.join(self.shared, SHA[:2]))
        self.canonical = os.path.join(self.shared, SHA[:2], "canonical.jpg")
        with open(self.canonical, "wb") as f:
            f.write(CONTENT)
        self.duplicate = os.path.join(self.shared, "fresh-download.jpg")
        with open(self.duplicate, "wb") as f:
            f.write(CONTENT)
        self.db = AsyncMock()
        self.db.find_media_by_content_hash.return_value = {
            "file_name": "canonical.jpg",
            "content_hash": SHA,
            "file_path": "media/whatever.jpg",
        }

    def tearDown(self):
        self.tmp.cleanup()

    async def test_match_reuses_canonical_and_deletes_the_duplicate(self):
        path, content_hash, reused = await deduplicate_shared_file(self.db, self.duplicate, self.shared, account_id=1)

        self.assertEqual(path, self.canonical)
        self.assertEqual(content_hash, SHA)
        self.assertTrue(reused)  # the caller must NOT move/delete this blob
        self.assertFalse(os.path.exists(self.duplicate))  # duplicate removed
        with open(self.canonical, "rb") as f:
            self.assertEqual(f.read(), CONTENT)  # canonical intact
        self.db.find_media_by_content_hash.assert_awaited_once_with(SHA, account_id=1)

    async def test_traversal_escape_is_refused_and_nothing_is_deleted(self):
        outside = os.path.join(self.tmp.name, "outside-target.jpg")
        with open(outside, "wb") as f:
            f.write(CONTENT)
        os.remove(self.canonical)
        os.symlink(outside, self.canonical)  # resolves OUTSIDE _shared

        path, content_hash, reused = await deduplicate_shared_file(self.db, self.duplicate, self.shared, account_id=1)

        self.assertEqual(path, self.duplicate)
        self.assertFalse(reused)
        self.assertTrue(os.path.exists(self.duplicate))  # nothing deleted
        self.assertTrue(os.path.exists(outside))

    async def test_match_on_the_same_path_is_not_a_reuse(self):
        self.db.find_media_by_content_hash.return_value = {
            "file_name": "fresh-download.jpg",
            "content_hash": SHA,
        }

        path, _, reused = await deduplicate_shared_file(self.db, self.duplicate, self.shared, account_id=1)

        self.assertEqual(path, self.duplicate)
        self.assertFalse(reused)
        self.assertTrue(os.path.exists(self.duplicate))

    async def test_dangling_canonical_is_not_reused(self):
        os.remove(self.canonical)
        os.symlink(os.path.join(self.tmp.name, "gone.jpg"), self.canonical)  # lexists, not exists

        path, _, reused = await deduplicate_shared_file(self.db, self.duplicate, self.shared, account_id=1)

        self.assertEqual(path, self.duplicate)
        self.assertFalse(reused)
        self.assertTrue(os.path.exists(self.duplicate))


class TestResolveSharedFilePathFallbacks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shared = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_hash_scans_shard_buckets(self):
        os.makedirs(os.path.join(self.shared, "cd"))
        os.makedirs(os.path.join(self.shared, "not-a-bucket"))
        target = os.path.join(self.shared, "cd", "file.jpg")
        open(target, "wb").close()
        # A same-named file in a non-bucket dir must not win
        open(os.path.join(self.shared, "not-a-bucket", "file.jpg"), "wb").close()

        self.assertEqual(resolve_shared_file_path(self.shared, "file.jpg", None), target)

    def test_flat_layout_fallback(self):
        target = os.path.join(self.shared, "legacy.jpg")
        open(target, "wb").close()
        self.assertEqual(resolve_shared_file_path(self.shared, "legacy.jpg", "zz00"), target)

    def test_missing_everywhere_returns_none(self):
        self.assertIsNone(resolve_shared_file_path(self.shared, "absent.jpg", None))


async def _seed_media(adapter, chat_id, message_id, media_id, *, downloaded, content_hash):
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": datetime(2026, 1, 1, 12, 0, 0),
            "text": "seed",
            "is_outgoing": 0,
            "sender_name": "Fixture Sender",
            "raw_data": {},
        },
        account_id=1,
    )
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "type": "photo",
            "file_path": f"media/{media_id}.jpg",
            "file_name": f"{media_id}.jpg",
            "content_hash": content_hash,
            "downloaded": downloaded,
        },
        account_id=1,
    )


class TestFindMediaByContentHashRealEngines:
    async def test_only_downloaded_rows_match(self, real_adapter):
        chat_id = 940001
        await real_adapter.upsert_chat({"id": chat_id, "type": "group", "title": "dedup"}, account_id=1)
        await _seed_media(real_adapter, chat_id, 1, "m-pending", downloaded=False, content_hash=SHA)

        assert await real_adapter.find_media_by_content_hash(SHA, account_id=1) is None

        await _seed_media(real_adapter, chat_id, 2, "m-done", downloaded=True, content_hash=SHA)
        found = await real_adapter.find_media_by_content_hash(SHA, account_id=1)
        assert found is not None and found["file_name"] == "m-done.jpg"

        # Another account never reuses this blob
        assert await real_adapter.find_media_by_content_hash(SHA, account_id=9) is None


if __name__ == "__main__":
    unittest.main()
