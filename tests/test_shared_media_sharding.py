"""Tests for hierarchical _shared/ media sharding (issue #149)."""

import hashlib
import os
import tempfile
import unittest

from src.message_utils import get_shared_file_path, resolve_shared_file_path
from src.migrate_shared_media import migrate_shared_media


class TestGetSharedFilePath(unittest.TestCase):
    """Test get_shared_file_path utility."""

    def test_with_hash_returns_sharded_path(self):
        result = get_shared_file_path("/data/_shared", "photo.jpg", "abcdef1234567890")
        assert result == "/data/_shared/ab/photo.jpg"

    def test_with_short_hash_returns_sharded_path(self):
        result = get_shared_file_path("/data/_shared", "photo.jpg", "ff")
        assert result == "/data/_shared/ff/photo.jpg"

    def test_without_hash_returns_flat_path(self):
        result = get_shared_file_path("/data/_shared", "photo.jpg", None)
        assert result == "/data/_shared/photo.jpg"

    def test_with_empty_hash_returns_flat_path(self):
        result = get_shared_file_path("/data/_shared", "photo.jpg", "")
        assert result == "/data/_shared/photo.jpg"

    def test_with_single_char_hash_returns_flat_path(self):
        result = get_shared_file_path("/data/_shared", "photo.jpg", "a")
        assert result == "/data/_shared/photo.jpg"

    def test_strips_directory_from_filename(self):
        result = get_shared_file_path("/data/_shared", "../../etc/passwd", "abcdef")
        assert result == "/data/_shared/ab/passwd"

    def test_strips_nested_path_from_filename(self):
        result = get_shared_file_path("/data/_shared", "subdir/photo.jpg", "abcdef")
        assert result == "/data/_shared/ab/photo.jpg"


class TestResolveSharedFilePath(unittest.TestCase):
    """Test resolve_shared_file_path with fallback logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.shared_dir = os.path.join(self.tmpdir, "_shared")
        os.makedirs(self.shared_dir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir)

    def test_finds_file_in_sharded_location(self):
        bucket_dir = os.path.join(self.shared_dir, "ab")
        os.makedirs(bucket_dir)
        filepath = os.path.join(bucket_dir, "photo.jpg")
        with open(filepath, "w") as f:
            f.write("data")

        result = resolve_shared_file_path(self.shared_dir, "photo.jpg", "abcdef1234")
        assert result == filepath

    def test_falls_back_to_flat_location(self):
        filepath = os.path.join(self.shared_dir, "photo.jpg")
        with open(filepath, "w") as f:
            f.write("data")

        result = resolve_shared_file_path(self.shared_dir, "photo.jpg", "abcdef1234")
        assert result == filepath

    def test_prefers_sharded_over_flat(self):
        # Both exist — sharded wins
        flat = os.path.join(self.shared_dir, "photo.jpg")
        with open(flat, "w") as f:
            f.write("flat")
        bucket_dir = os.path.join(self.shared_dir, "ab")
        os.makedirs(bucket_dir)
        sharded = os.path.join(bucket_dir, "photo.jpg")
        with open(sharded, "w") as f:
            f.write("sharded")

        result = resolve_shared_file_path(self.shared_dir, "photo.jpg", "abcdef1234")
        assert result == sharded

    def test_returns_none_when_not_found(self):
        result = resolve_shared_file_path(self.shared_dir, "missing.jpg", "abcdef1234")
        assert result is None

    def test_finds_flat_when_no_hash(self):
        filepath = os.path.join(self.shared_dir, "photo.jpg")
        with open(filepath, "w") as f:
            f.write("data")

        result = resolve_shared_file_path(self.shared_dir, "photo.jpg", None)
        assert result == filepath

    def test_recognizes_symlinks_via_lexists(self):
        bucket_dir = os.path.join(self.shared_dir, "ab")
        os.makedirs(bucket_dir)
        link_path = os.path.join(bucket_dir, "photo.jpg")
        os.symlink("/nonexistent/target", link_path)

        result = resolve_shared_file_path(self.shared_dir, "photo.jpg", "abcdef1234")
        assert result == link_path


class TestMigrateSharedMedia(unittest.TestCase):
    """Test the flat-to-sharded migration."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.media_path = self.tmpdir
        self.shared_dir = os.path.join(self.media_path, "_shared")
        os.makedirs(self.shared_dir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir)

    def _create_file(self, path, content="test data"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _content_hash(self, content="test data"):
        return hashlib.sha256(content.encode()).hexdigest()

    def test_migrates_flat_files_to_sharded(self):
        content = "hello world"
        flat_file = self._create_file(os.path.join(self.shared_dir, "photo.jpg"), content)
        expected_hash = self._content_hash(content)
        expected_bucket = expected_hash[:2]

        count = migrate_shared_media(self.media_path)

        assert count == 1
        assert not os.path.exists(flat_file)
        sharded = os.path.join(self.shared_dir, expected_bucket, "photo.jpg")
        assert os.path.exists(sharded)

    def test_updates_chat_symlinks(self):
        content = "media content"
        flat_file = self._create_file(os.path.join(self.shared_dir, "video.mp4"), content)

        # Create a chat dir with a symlink pointing to the flat location
        chat_dir = os.path.join(self.media_path, "-1001234")
        os.makedirs(chat_dir)
        link_path = os.path.join(chat_dir, "video.mp4")
        os.symlink(os.path.relpath(flat_file, chat_dir), link_path)

        migrate_shared_media(self.media_path)

        # Symlink should be updated to point to sharded location
        assert os.path.islink(link_path)
        target = os.readlink(link_path)
        expected_hash = self._content_hash(content)
        assert expected_hash[:2] in target

    def test_idempotent_with_marker(self):
        self._create_file(os.path.join(self.shared_dir, "photo.jpg"), "data")

        count1 = migrate_shared_media(self.media_path)
        assert count1 == 1

        # Second run should do nothing (marker exists)
        count2 = migrate_shared_media(self.media_path)
        assert count2 == 0

    def test_skips_hidden_files(self):
        self._create_file(os.path.join(self.shared_dir, ".gitkeep"), "")
        count = migrate_shared_media(self.media_path)
        assert count == 0

    def test_no_shared_dir_returns_zero(self):
        import shutil

        shutil.rmtree(self.shared_dir)
        count = migrate_shared_media(self.media_path)
        assert count == 0

    def test_skips_files_already_in_buckets(self):
        # File already in a shard bucket should not be touched
        bucket_dir = os.path.join(self.shared_dir, "ab")
        os.makedirs(bucket_dir)
        self._create_file(os.path.join(bucket_dir, "photo.jpg"), "data")

        count = migrate_shared_media(self.media_path)
        assert count == 0
        assert os.path.exists(os.path.join(bucket_dir, "photo.jpg"))
