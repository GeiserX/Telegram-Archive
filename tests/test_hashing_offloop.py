"""Off-loop, memoized media hashing (9t6.5.28 / 9t6.6.6 / 9t6.6.7).

Whole-file SHA-256 of large media ran inline on the event loop the realtime
listener and Telethon transport share, and the shared store re-hashed the
same canonical blob for every duplicate reference. These pin the worker-hop
contract, the (mtime, size)-validated memo, and the converted call sites.
"""

import asyncio
import unittest.mock
from pathlib import Path

import src.message_utils as mu

REPO = Path(__file__).resolve().parents[1]


class TestMemo:
    def setup_method(self):
        mu._hash_cache.clear()

    def test_same_unchanged_file_hashes_once(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"payload")
        with unittest.mock.patch.object(mu, "compute_file_hash", wraps=mu.compute_file_hash) as spy:
            first = mu.compute_file_hash_cached(str(f))
            second = mu.compute_file_hash_cached(str(f))
        assert first == second and first is not None
        assert spy.call_count == 1

    def test_changed_file_invalidates(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"payload")
        first = mu.compute_file_hash_cached(str(f))
        f.write_bytes(b"different payload")
        second = mu.compute_file_hash_cached(str(f))
        assert first != second

    def test_failed_hash_is_never_stored(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"payload")
        with unittest.mock.patch.object(mu, "compute_file_hash", return_value=None) as spy:
            assert mu.compute_file_hash_cached(str(f)) is None
            assert mu.compute_file_hash_cached(str(f)) is None
        assert spy.call_count == 2  # a patched/flaky read cannot poison the cache

    def test_missing_file_is_none(self, tmp_path):
        assert mu.compute_file_hash_cached(str(tmp_path / "gone")) is None


class TestOffLoopContract:
    async def test_async_form_hops_through_a_worker_thread(self, tmp_path):
        mu._hash_cache.clear()
        f = tmp_path / "blob.bin"
        f.write_bytes(b"payload")

        calls = []
        real_to_thread = asyncio.to_thread

        async def counting_to_thread(fn, *args, **kwargs):
            calls.append(fn.__name__)
            return await real_to_thread(fn, *args, **kwargs)

        with unittest.mock.patch.object(mu.asyncio, "to_thread", counting_to_thread):
            digest = await mu.compute_file_hash_async(str(f))
        assert digest is not None
        assert calls == ["compute_file_hash_cached"]


class TestCallSiteConversion:
    def test_no_inline_hashing_remains_on_async_capture_paths(self):
        listener = (REPO / "src" / "listener.py").read_text()
        backup = (REPO / "src" / "telegram_backup.py").read_text()
        for src, name in ((listener, "listener"), (backup, "telegram_backup")):
            assert "= compute_file_hash(" not in src, name
            assert "compute_file_hash_async" in src, name
        # Thumbnail generation moved off-loop too.
        assert "asyncio.to_thread(_pre_generate_thumbnail" in backup

    def test_dedup_and_reuse_paths_await_the_async_form(self):
        src = (REPO / "src" / "message_utils.py").read_text()
        assert src.count("await compute_file_hash_async(") == 3
