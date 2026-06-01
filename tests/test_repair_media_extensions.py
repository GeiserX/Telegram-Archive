"""Tests for the #175 media extension repair pass."""

import asyncio
import os
from unittest import mock

import pytest

from src.repair_media_extensions import (
    REPAIR_MARKER,
    _is_corrupt_basename,
    _iter_chat_dirs,
    _repair_records_sync,
    _scan_chat_symlinks,
    _sweep_orphan_links_sync,
    repair_media_extensions,
)


class _FakeDB:
    """Minimal async stand-in for the DatabaseAdapter media surface."""

    def __init__(self, records, fail_update_ids=None, batch_size=500):
        self._records = records
        self.updates = {}
        self._fail_update_ids = set(fail_update_ids or ())
        self._batch_size = batch_size

    async def iter_media_paths_for_repair(self, batch_size=None):
        size = batch_size or self._batch_size
        for start in range(0, len(self._records), size):
            yield [dict(r) for r in self._records[start : start + size]]

    async def update_media_file_path(self, media_id, file_path):
        if media_id in self._fail_update_ids:
            raise RuntimeError("simulated DB write failure")
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


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
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


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
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


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
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


# ---------------------------------------------------------------------------
# Marker is withheld on transient failure, so retries are not suppressed
# ---------------------------------------------------------------------------


async def test_repair_withholds_marker_when_db_repoint_fails(tmp_path):
    """A DB write failure AFTER the on-disk rename must not seal the pass.

    The file is renamed but the row is stale; the marker must be withheld so the
    next run's adoption branch repoints media.file_path instead of suppressing it.
    """
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    db = _FakeDB(
        [{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}],
        fail_update_ids={"m1"},
    )

    repaired = await repair_media_extensions(str(media), db)

    # Rename happened on disk, but the DB repoint failed -> not counted, marker withheld.
    assert repaired == 0
    assert (chat / "abc.mp4").read_bytes() == b"video"
    assert not (media / "_shared" / REPAIR_MARKER).exists()

    # Next run adopts the manually-present clean file and repoints the row.
    db2 = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])
    second = await repair_media_extensions(str(media), db2)
    assert second == 1
    assert db2.updates["m1"] == str(chat / "abc.mp4")
    assert (media / "_shared" / REPAIR_MARKER).exists()


async def test_repair_writes_marker_when_only_permanent_noops(tmp_path):
    """A genuinely distinct clean file is a permanent no-op, not a deferral.

    It must NOT block the marker — retrying can never resolve it.
    """
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"corrupt-copy")
    clean = chat / "abc.mp4"
    clean.write_bytes(b"distinct-real-file")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert (media / "_shared" / REPAIR_MARKER).exists()


# ---------------------------------------------------------------------------
# Multi-symlink dedup: sibling links self-heal after the blob is renamed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
async def test_repair_dedup_heals_sibling_symlinks_to_renamed_blob(tmp_path):
    """Two chats share one dedup'd blob. After the first link renames the blob,
    the sibling link (still pointing at the old corrupt name) must be retargeted
    to the clean blob in the same pass."""
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat_a = media / "-100111"
    chat_b = media / "-100222"
    chat_a.mkdir()
    chat_b.mkdir()
    link_a = chat_a / "abc.mp4"
    link_b = chat_b / "abc.mp4"
    link_a.symlink_to(os.path.relpath(corrupt_blob, chat_a))
    link_b.symlink_to(os.path.relpath(corrupt_blob, chat_b))

    db = _FakeDB(
        [
            {"id": "m1", "file_name": "abc.mp4", "file_path": str(link_a)},
            {"id": "m2", "file_name": "abc.mp4", "file_path": str(link_b)},
        ]
    )

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 2
    clean_blob = shared / "abc.mp4"
    assert clean_blob.read_bytes() == b"video"
    assert not corrupt_blob.exists()
    assert os.path.realpath(link_a) == str(clean_blob)
    assert os.path.realpath(link_b) == str(clean_blob)


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
async def test_repair_skips_symlink_when_basename_differs_from_clean_name(tmp_path):
    """A symlink whose basename doesn't match file_name is left untouched."""
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    # Link basename "renamed.mp4" != DB file_name "abc.mp4" -> skip.
    link = chat / "renamed.mp4"
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(link)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert corrupt_blob.exists()
    assert os.readlink(link) == os.path.relpath(corrupt_blob, chat)


async def test_repair_defers_and_withholds_marker_on_replace_oserror(tmp_path):
    """A transient os.replace failure during a no-dedup rename defers the row.

    The marker must be withheld so the next run retries instead of suppressing.
    """
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}])

    with mock.patch("src.repair_media_extensions.os.replace", side_effect=OSError("EIO")):
        repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert "m1" not in db.updates
    assert not (media / "_shared" / REPAIR_MARKER).exists()


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
async def test_repair_realpath_guard_blocks_symlinked_blob_dir(tmp_path):
    """A blob dir that is itself a symlink resolving outside _shared is rejected.

    normpath would be fooled; realpath is not. The link must be left untouched.
    """
    media = _media_root(tmp_path)
    external = tmp_path / "external_store"
    external.mkdir()
    external_blob = external / "abc.mp4.7.140234567890"
    external_blob.write_bytes(b"managed-elsewhere")

    # _shared/sneaky -> ../external_store (a symlinked bucket pointing outside)
    sneaky = media / "_shared" / "sneaky"
    sneaky.symlink_to(os.path.relpath(external, media / "_shared"))

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    # Link target goes through the in-_shared symlink, so normpath stays "inside".
    link.symlink_to(os.path.relpath(sneaky / "abc.mp4.7.140234567890", chat))

    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": str(link)}])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert external_blob.exists()
    assert not (external / "abc.mp4").exists()


# ---------------------------------------------------------------------------
# _repair_records_sync — the off-loop worker contract
# ---------------------------------------------------------------------------


def test_repair_records_sync_returns_pending_update_for_direct_file(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    records = [{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}]
    repaired, deferred, pending = _repair_records_sync(records, str(media / "_shared"))

    assert repaired == 1
    assert deferred == 0
    assert pending == [("m1", str(chat / "abc.mp4"))]
    assert (chat / "abc.mp4").read_bytes() == b"video"


def test_repair_records_sync_counts_oserror_as_deferred(tmp_path):
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    records = [{"id": "m1", "file_name": "abc.mp4", "file_path": str(corrupt)}]
    with mock.patch("src.repair_media_extensions.os.replace", side_effect=OSError("EIO")):
        repaired, deferred, pending = _repair_records_sync(records, str(media / "_shared"))

    assert repaired == 0
    assert deferred == 1
    assert pending == []


def test_repair_records_sync_skips_incomplete_records(tmp_path):
    media = _media_root(tmp_path)
    records = [
        {"id": None, "file_name": "abc.mp4", "file_path": "/x"},
        {"id": "m2", "file_name": None, "file_path": "/x"},
        {"id": "m3", "file_name": "abc.mp4", "file_path": None},
    ]
    repaired, deferred, pending = _repair_records_sync(records, str(media / "_shared"))
    assert (repaired, deferred, pending) == (0, 0, [])


async def test_repair_offloads_fs_work_to_thread(tmp_path):
    """repair_media_extensions runs the blocking sweeps via asyncio.to_thread.

    Two off-loop calls now happen: the DB-driven record repair, then the
    filesystem-driven orphan-link sweep.
    """
    media = _media_root(tmp_path)
    db = _FakeDB([{"id": "m1", "file_name": "abc.mp4", "file_path": "/x/abc.mp4"}])

    with mock.patch(
        "src.repair_media_extensions.asyncio.to_thread",
        new_callable=mock.AsyncMock,
        side_effect=[(0, 0, []), (0, 0)],
    ) as to_thread:
        await repair_media_extensions(str(media), db)

    assert to_thread.await_count == 2
    record_call = to_thread.await_args_list[0].args
    assert record_call[0] is _repair_records_sync
    assert record_call[2] == str(media / "_shared")
    sweep_call = to_thread.await_args_list[1].args
    assert sweep_call[0] is _sweep_orphan_links_sync
    assert sweep_call[1] == str(media)
    assert sweep_call[2] == str(media / "_shared")


async def test_repair_streams_in_batches_without_loading_whole_table(tmp_path):
    """Regression for the v7.11.3 OOM crash loop (#175).

    The repair must consume the media table in bounded batches, not materialize
    it all at once. Here three corrupt files span two batches of size 2; all
    must be repaired and the worker must be invoked once per non-empty batch.
    """
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    records = []
    for i in range(3):
        corrupt = chat / f"file{i}.mp4.7.140234567890"
        corrupt.write_bytes(b"video")
        records.append({"id": f"m{i}", "file_name": f"file{i}.mp4", "file_path": str(corrupt)})

    db = _FakeDB(records, batch_size=2)

    real_to_thread = asyncio.to_thread
    with mock.patch("src.repair_media_extensions.asyncio.to_thread", side_effect=real_to_thread) as to_thread:
        repaired = await repair_media_extensions(str(media), db)

    assert repaired == 3
    # ceil(3 / 2) record batches + 1 orphan-link sweep.
    assert to_thread.await_count == 3
    for i in range(3):
        assert (chat / f"file{i}.mp4").read_bytes() == b"video"
        assert db.updates[f"m{i}"] == str(chat / f"file{i}.mp4")
    assert (media / "_shared" / REPAIR_MARKER).exists()


async def test_repair_writes_marker_on_empty_table(tmp_path):
    """An archive with no media rows still seals the pass so it never re-runs."""
    media = _media_root(tmp_path)
    db = _FakeDB([])

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert (media / "_shared" / REPAIR_MARKER).exists()


# ---------------------------------------------------------------------------
# Orphan-link filesystem sweep: corrupt blobs whose media row is missing
# ---------------------------------------------------------------------------


def test_iter_chat_dirs_excludes_shared_and_files(tmp_path):
    media = _media_root(tmp_path)
    (media / "-100111").mkdir()
    (media / "-100222").mkdir()
    (media / "loose.txt").write_bytes(b"x")  # a stray file, not a chat dir

    chat_dirs = set(_iter_chat_dirs(str(media)))

    assert chat_dirs == {str(media / "-100111"), str(media / "-100222")}
    assert str(media / "_shared") not in chat_dirs


def test_iter_chat_dirs_raises_on_missing_root(tmp_path):
    """A failed root scan must surface, not be mistaken for an empty dir, so the
    sweep can defer it and withhold the marker."""
    import pytest

    with pytest.raises(OSError):
        _iter_chat_dirs(str(tmp_path / "nope"))


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
def test_scan_chat_symlinks_returns_only_symlink_strings(tmp_path):
    """The helper returns string paths (not DirEntry) and skips real files, so the
    caller can mutate the directory after the scandir handle is closed."""
    chat = tmp_path / "-100123"
    chat.mkdir()
    target = tmp_path / "blob.bin"
    target.write_bytes(b"x")
    (chat / "real.txt").write_bytes(b"y")  # a real file, not a symlink
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(target, chat))

    result = _scan_chat_symlinks(str(chat))

    assert result == [str(link)]
    assert all(isinstance(p, str) for p in result)


def test_sweep_defers_when_root_unreadable(tmp_path):
    """A transient failure enumerating chat dirs defers the whole sweep."""
    media = _media_root(tmp_path)

    with mock.patch("src.repair_media_extensions._iter_chat_dirs", side_effect=OSError("EIO")):
        repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (0, 1)


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
def test_sweep_heals_orphan_link_with_no_db_row(tmp_path):
    """The #175 v7.11.4 residue: a dedup symlink whose media row is gone.

    The DB-driven pass never sees it, so the orphan sweep must rename the blob
    and retarget the link on its own.
    """
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"  # clean link name, no DB row references it
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (1, 0)
    clean_blob = shared / "abc.mp4"
    assert clean_blob.read_bytes() == b"video"
    assert not corrupt_blob.exists()
    assert os.path.realpath(link) == str(clean_blob)


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
def test_sweep_leaves_clean_links_untouched(tmp_path):
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    clean_blob = shared / "abc.mp4"
    clean_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(clean_blob, chat))

    repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (0, 0)
    assert os.path.realpath(link) == str(clean_blob)


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
def test_sweep_never_renames_blob_outside_shared(tmp_path):
    media = _media_root(tmp_path)
    external = tmp_path / "external_store"
    external.mkdir()
    external_blob = external / "abc.mp4.7.140234567890"
    external_blob.write_bytes(b"managed-elsewhere")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(external_blob, chat))

    repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (0, 0)
    assert external_blob.exists()
    assert not (external / "abc.mp4").exists()


def test_sweep_ignores_non_symlink_files_in_chat_dir(tmp_path):
    """A real (non-symlink) corrupt-named file in a chat dir is a no-dedup row;
    the DB pass owns it, so the orphan-link sweep must skip it."""
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()
    corrupt = chat / "abc.mp4.7.140234567890"
    corrupt.write_bytes(b"video")

    repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (0, 0)
    assert corrupt.exists()


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
async def test_repair_end_to_end_heals_orphan_link_after_db_pass(tmp_path):
    """Full driver: an orphan dedup link (no DB row) is healed by the sweep,
    and the versioned marker is written so the pass is idempotent afterward."""
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    db = _FakeDB([])  # no media rows at all -> DB pass repairs nothing

    repaired = await repair_media_extensions(str(media), db)

    assert repaired == 1
    assert (shared / "abc.mp4").read_bytes() == b"video"
    assert not corrupt_blob.exists()
    assert os.path.realpath(link) == str(shared / "abc.mp4")
    assert (media / "_shared" / REPAIR_MARKER).exists()


def test_sweep_defers_when_chat_dir_unreadable(tmp_path):
    """A chat dir that can't be scanned is deferred, not crashed past."""
    media = _media_root(tmp_path)
    chat = media / "-100123"
    chat.mkdir()

    real_scandir = os.scandir

    def scandir(path):
        if path == str(chat):
            raise OSError("EIO")  # the in-dir scan fails
        return real_scandir(path)  # the chat-dir enumeration succeeds

    with mock.patch("src.repair_media_extensions.os.scandir", side_effect=scandir):
        repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (0, 1)


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
def test_sweep_defers_on_per_entry_oserror(tmp_path):
    """A symlink whose repair raises OSError increments deferred, not repaired."""
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    with mock.patch(
        "src.repair_media_extensions._repair_symlink_blob",
        side_effect=OSError("EIO"),
    ):
        repaired, deferred = _sweep_orphan_links_sync(str(media), str(media / "_shared"))

    assert (repaired, deferred) == (0, 1)


async def test_repair_defers_when_sweep_raises(tmp_path):
    """A non-OSError escaping the sweep is caught: deferred++, marker withheld."""
    media = _media_root(tmp_path)
    db = _FakeDB([])

    real_to_thread = asyncio.to_thread

    async def flaky(func, *args):
        if func is _sweep_orphan_links_sync:
            raise RuntimeError("boom")
        return await real_to_thread(func, *args)

    with mock.patch("src.repair_media_extensions.asyncio.to_thread", side_effect=flaky):
        repaired = await repair_media_extensions(str(media), db)

    assert repaired == 0
    assert not (media / "_shared" / REPAIR_MARKER).exists()


class _ReadFailDB:
    """A DB whose media-table read fails, to exercise the sweep-only fallback."""

    def __init__(self):
        self.updates = {}

    async def iter_media_paths_for_repair(self, batch_size=None):
        raise RuntimeError("simulated DB read failure")
        yield  # pragma: no cover - makes this an async generator

    async def update_media_file_path(self, media_id, file_path):  # pragma: no cover
        self.updates[media_id] = file_path


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink privileges on Windows")
async def test_repair_runs_sweep_even_when_db_read_fails(tmp_path):
    """A DB read failure must not skip the DB-independent orphan sweep.

    The sweep is exactly what heals corrupt blobs whose media row is missing, so
    a transient DB outage must still let it run. The DB failure is deferred, so
    the marker is withheld and the DB pass retries next run.
    """
    media = _media_root(tmp_path)
    shared = media / "_shared" / "ab"
    shared.mkdir()
    corrupt_blob = shared / "abc.mp4.7.140234567890"
    corrupt_blob.write_bytes(b"video")

    chat = media / "-100123"
    chat.mkdir()
    link = chat / "abc.mp4"
    link.symlink_to(os.path.relpath(corrupt_blob, chat))

    repaired = await repair_media_extensions(str(media), _ReadFailDB())

    # Sweep healed the orphan even though the DB read blew up.
    assert repaired == 1
    assert (shared / "abc.mp4").read_bytes() == b"video"
    assert not corrupt_blob.exists()
    assert os.path.realpath(link) == str(shared / "abc.mp4")
    # DB failure was deferred -> marker withheld so the DB pass retries.
    assert not (media / "_shared" / REPAIR_MARKER).exists()


async def test_repair_withholds_marker_when_db_read_fails_and_sweep_empty(tmp_path):
    """DB outage on an archive with no orphan residue must still withhold the marker.

    This isolates the marker invariant from sweep success: the sweep repairs
    nothing (repaired==0) yet the deferred DB read must keep the marker unwritten
    so the DB pass retries next run. Writing it here would permanently seal the
    pass after a transient DB failure on a clean archive (#175 deferred-marker).
    """
    media = _media_root(tmp_path)
    # A clean chat dir with nothing corrupt: the sweep finds no work.
    (media / "-100123").mkdir()

    repaired = await repair_media_extensions(str(media), _ReadFailDB())

    assert repaired == 0
    assert not (media / "_shared" / REPAIR_MARKER).exists()


async def test_repair_summary_logged_at_warning_level(tmp_path, caplog):
    """LOG_LEVEL=warn users must see the repair ran (#175 zey0n: 'no logs')."""
    import logging

    media = _media_root(tmp_path)
    db = _FakeDB([])

    with caplog.at_level(logging.WARNING, logger="src.repair_media_extensions"):
        await repair_media_extensions(str(media), db)

    assert any(
        record.levelno == logging.WARNING and "Media extension repair (#175)" in record.getMessage()
        for record in caplog.records
    )
