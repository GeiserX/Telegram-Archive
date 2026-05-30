"""Tests for the #175 media extension repair pass."""

import os

from src.repair_media_extensions import (
    REPAIR_MARKER,
    _is_corrupt_basename,
    repair_media_extensions,
)


class _FakeDB:
    """Minimal async stand-in for the DatabaseAdapter media surface."""

    def __init__(self, records):
        self._records = records
        self.updates = {}

    async def get_media_for_verification(self):
        return list(self._records)

    async def update_media_file_path(self, media_id, file_path):
        self.updates[media_id] = file_path


def _media_root(tmp_path):
    media = tmp_path / "media"
    (media / "_shared").mkdir(parents=True)
    return media


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_is_corrupt_basename_matches_pid_task_tail():
    assert _is_corrupt_basename("123.mp4.7.140234567890", "123.mp4")


def test_is_corrupt_basename_rejects_clean_name():
    assert not _is_corrupt_basename("123.mp4", "123.mp4")


def test_is_corrupt_basename_rejects_legit_digit_names():
    # A real filename that merely ends in digits must not be flagged.
    assert not _is_corrupt_basename("backup_2024.7z", "backup_2024.7z")
    assert not _is_corrupt_basename("report.v2", "report.v2")


def test_is_corrupt_basename_requires_clean_prefix():
    assert not _is_corrupt_basename("other.mp4.7.999999", "123.mp4")


# ---------------------------------------------------------------------------
# No-dedup repair: corrupt file recorded directly in DB
# ---------------------------------------------------------------------------


async def test_repair_no_dedup_renames_file_and_updates_db(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 1
    clean = chat / "abc.mp4"
    assert clean.read_bytes() == b"video"
    assert not corrupt.exists()
    assert db.updates["m1"] == str(clean)


async def test_repair_no_dedup_skips_when_clean_exists_with_different_content(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"corrupt-copy")
    clean = chat / "abc.mp4"
    clean.write_bytes(b"the-real-distinct-file")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    # Neither file is destroyed.
    assert clean.read_bytes() == b"the-real-distinct-file"
    assert corrupt.read_bytes() == b"corrupt-copy"
    assert "m1" not in db.updates


async def test_repair_no_dedup_adopts_manually_renamed_file(tmp_path):
    """The #175 reporter renamed files by hand; repair must still fix the DB row.

    Clean file exists, corrupt path is already gone -> adopt the clean file and
    repoint media.file_path (otherwise the marker suppresses any later retry).
    """
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    clean = chat / "abc.mp4"
    clean.write_bytes(b"video")
    corrupt_recorded = chat / "abc.mp4.7.140234567890"  # in DB, no longer on disk

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt_recorded)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 1
    assert db.updates["m1"] == str(clean)
    assert clean.read_bytes() == b"video"


async def test_repair_no_dedup_adopts_clean_when_content_matches(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"same")
    clean = chat / "abc.mp4"
    clean.write_bytes(b"same")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])

    repaired = await repair_media_extensions(str(media), db)

    # Treated as repaired (clean copy already holds the content); DB row points clean.
    assert repaired == 1
    assert db.updates["m1"] == str(clean)
    assert clean.read_bytes() == b"same"


# ---------------------------------------------------------------------------
# Dedup repair: clean symlink -> corrupt shared blob
# ---------------------------------------------------------------------------


async def test_repair_dedup_renames_blob_and_retargets_symlink(tmp_path):
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(link)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 1
    clean_blob = shared / "abc.mp4"
    assert clean_blob.read_bytes() == b"video"
    assert not corrupt_blob.exists()
    # Symlink still resolves, now to the clean blob.
    assert os.path.islink(link)
    assert os.path.realpath(link) == str(clean_blob)


async def test_repair_dedup_relinks_when_clean_blob_already_present(tmp_path):
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")
    clean_blob = shared / "abc.mp4"
    clean_blob.write_bytes(b"video")  # identical content

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(link)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 1
    assert os.path.realpath(link) == str(clean_blob)
    assert clean_blob.read_bytes() == b"video"


async def test_repair_dedup_never_renames_blob_outside_shared(tmp_path):
    """A symlink pointing at an externally managed store must not be touched."""
    media = _media_root(tmp_path)
    external = tmp_path / "external_store"
    external.mkdir()
    external_blob = external / "abc.mp4.7.140234567890"
    external_blob.write_bytes(b"managed-elsewhere")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(external_blob, chat))

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(link)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    # External file untouched; no clean sibling created next to it.
    assert external_blob.exists()
    assert not (external / "abc.mp4").exists()
    assert os.readlink(link) == os.path.relpath(external_blob, chat)


# ---------------------------------------------------------------------------
# Safety / idempotency
# ---------------------------------------------------------------------------


async def test_repair_leaves_clean_records_untouched(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    clean = chat / "abc.mp4"
    clean.write_bytes(b"video")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(clean)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert "m1" not in db.updates
    assert clean.read_bytes() == b"video"


async def test_repair_never_deletes_orphan_part_files(tmp_path):
    media = _media_root(tmp_path)
    orphan = media / "_shared" / "leftover.mp4.7.99.part"
    orphan.write_bytes(b"partial")

    db = _FakeDB([])

    await repair_media_extensions(str(media), db)

    assert orphan.exists()  # counted, not deleted


async def test_repair_is_idempotent_via_marker(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])

    first = await repair_media_extensions(str(media), db)
    assert first == 1
    assert (media / "_shared" / REPAIR_MARKER).exists()

    # Second run short-circuits on the marker.
    db2 = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])
    second = await repair_media_extensions(str(media), db2)
    assert second == 0
    assert db2.updates == {}


async def test_repair_noop_when_media_path_missing(tmp_path):
    db = _FakeDB([])
    assert await repair_media_extensions(str(tmp_path / "nope"), db) == 0
