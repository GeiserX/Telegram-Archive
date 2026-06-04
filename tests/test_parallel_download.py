"""Tests for per-file parallel chunked downloads (issue #183).

Covers the failure modes that actually matter for safe, unattended reassembly:
exact-offset writes, coverage verification (gaps/overlaps/wrong size), dropped
or short chunks, racing writers, mid-transfer FileReferenceExpired, FloodWait
propagation through the single budget, transactional cleanup, capability-probe
fallback, and the backup-layer size/flag gating.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import utils
from telethon.errors import FileReferenceExpiredError, FloodWaitError

from src.parallel_download import (
    ParallelDownloader,
    ParallelDownloadUnavailable,
    _extract_file_size,
    _pwrite_all,
    _verify_coverage,
    is_valid_part_size,
    supports_parallel_download,
)
from src.telegram_backup import TelegramBackup

_HAS_PWRITE = hasattr(os, "pwrite")


def _read(path: str | os.PathLike[str]) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# Fake message (downloadable-media stand-in)
# --------------------------------------------------------------------------- #
class _FakeDoc:
    def __init__(self, size: int) -> None:
        self.size = size


class _FakeMedia:
    def __init__(self, size: int) -> None:
        self.document = _FakeDoc(size)
        self.photo = None


class _FakeMessage:
    def __init__(self, size: int) -> None:
        self.media = _FakeMedia(size)


def _make_message(size: int) -> _FakeMessage:
    return _FakeMessage(size)


# --------------------------------------------------------------------------- #
# Fake Telethon client / sender harness
# --------------------------------------------------------------------------- #
class _GetFileResult:
    def __init__(self, data: bytes) -> None:
        self.bytes = data


class FakeSender:
    """Records connect/disconnect; bytes come from the client's blob."""

    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self.dc_id: int | None = None
        self.auth_key: object = object()

    async def connect(self, _connection: Any) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send(self, _request: Any) -> None:
        return None


class FakeDC:
    ip_address = "127.0.0.1"
    port = 443

    def __init__(self, dc_id: int) -> None:
        self.id = dc_id


class FakeSession:
    def __init__(self, dc_id: int = 2) -> None:
        self.dc_id = dc_id
        self.auth_key: object = object()


class FakeLog(dict):
    def __getitem__(self, k: str) -> logging.Logger:
        return logging.getLogger("fake")


class FakeClient:
    """Minimal stand-in exposing the private internals the transferrer uses.

    ``blob`` is the full file content; ``_call`` slices it by the request's
    offset/limit, so a correctly reassembled output must equal ``blob``.
    """

    def __init__(
        self,
        blob: bytes,
        *,
        home_dc: int = 2,
        fail_at_offset: int | None = None,
        fail_exc: BaseException | None = None,
        short_at_offset: int | None = None,
    ) -> None:
        self.blob = blob
        self.session = FakeSession(home_dc)
        self._log = FakeLog()
        self._proxy = None
        self._local_addr = None
        self._init_request = type("Init", (), {"query": None})()
        self._borrow_sender_lock = asyncio.Lock()
        self.created_senders: list[FakeSender] = []
        self._fail_at_offset = fail_at_offset
        self._fail_exc = fail_exc
        self._short_at_offset = short_at_offset

    async def _get_dc(self, dc_id: int) -> FakeDC:
        return FakeDC(dc_id)

    def _connection(self, *a: Any, **k: Any) -> object:
        return object()

    async def _call(self, sender: Any, request: Any) -> _GetFileResult:
        offset = request.offset
        limit = request.limit
        if self._fail_at_offset is not None and offset == self._fail_at_offset:
            assert self._fail_exc is not None
            raise self._fail_exc
        data = self.blob[offset : offset + limit]
        if self._short_at_offset is not None and offset == self._short_at_offset:
            data = data[:-1]  # drop a byte to simulate a short/corrupt chunk
        return _GetFileResult(data)

    # Patched factory so we count and inspect senders without real I/O.
    def make_sender(self) -> FakeSender:
        s = FakeSender()
        self.created_senders.append(s)
        return s


class ForeignDCClient(FakeClient):
    """FakeClient whose ``client(request)`` records auth-export calls."""

    def __init__(self, blob: bytes, **kw: Any) -> None:
        super().__init__(blob, **kw)
        self.export_calls: list[Any] = []

    async def __call__(self, request: Any) -> Any:
        self.export_calls.append(request)
        return type("Auth", (), {"id": 1, "bytes": b"k"})()


def _make_backup(*, enabled: bool, min_mb: int = 20, conns: int = 4, part_kb: int = 512) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    cfg = MagicMock()
    cfg.should_skip_topic = MagicMock(return_value=False)
    cfg.parallel_download_enabled = enabled
    cfg.parallel_download_connections = conns
    cfg.parallel_download_part_size_kb = part_kb
    cfg.get_parallel_download_min_size_bytes = MagicMock(return_value=min_mb * 1024 * 1024)
    cfg.get_parallel_download_part_size_bytes = MagicMock(return_value=part_kb * 1024)
    cfg.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
    backup.config = cfg
    backup.client = MagicMock()
    backup._parallel_downloader = None
    backup._parallel_download_disabled = False
    return backup


# --------------------------------------------------------------------------- #
# Shared mixins: patch lifecycle + temp dir, modelled on the repo's other
# unittest suites (start/addCleanup instead of the pytest monkeypatch fixture).
# --------------------------------------------------------------------------- #
class _PatchHelpers:
    """Patch helpers that auto-revert via ``addCleanup`` (unittest idiom)."""

    addCleanup: Callable[..., None]

    def _patch(self, target: str, new: Any) -> Any:
        patcher = patch(target, new)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _patch_object(self, target: Any, attribute: str, new: Any) -> Any:
        patcher = patch.object(target, attribute, new)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _delattr_temp(self, obj: Any, name: str) -> None:
        sentinel = object()
        original = getattr(obj, name, sentinel)
        delattr(obj, name)

        def _restore() -> None:
            if original is not sentinel:
                setattr(obj, name, original)

        self.addCleanup(_restore)

    def _patch_sender(self, client: FakeClient) -> None:
        """Replace MTProtoSender construction with FakeSender bound to client."""

        def _connect_sender_stub(_self: ParallelDownloader, dc_id: int, auth_key: Any) -> Any:
            async def _inner() -> FakeSender:
                s = client.make_sender()
                s.dc_id = dc_id
                s.connected = True
                return s

            return _inner()

        self._patch_object(ParallelDownloader, "_connect_sender", _connect_sender_stub)
        # get_input_location returns (dc_id, location); location is opaque here.
        self._patch_object(utils, "get_input_location", lambda m: (client.session.dc_id, object()))


class _TmpDirMixin:
    """Provide ``self.tmp`` as a Path to a per-test temporary directory."""

    addCleanup: Callable[..., None]

    def setUp(self) -> None:
        super().setUp()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmp = Path(tmpdir.name)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class TestPureHelpers(unittest.TestCase):
    def test_is_valid_part_size_accepts_only_telegram_constraints(self) -> None:
        self.assertTrue(is_valid_part_size(524288))  # 512 KiB max
        self.assertTrue(is_valid_part_size(131072))  # 128 KiB divides 1 MiB
        self.assertTrue(is_valid_part_size(4096))  # 4 KiB minimum alignment
        self.assertFalse(is_valid_part_size(524288 + 4096))  # exceeds max
        self.assertFalse(is_valid_part_size(1048576))  # exceeds max
        self.assertFalse(is_valid_part_size(3000))  # not a 4 KiB multiple
        self.assertFalse(is_valid_part_size(393216))  # 384 KiB does not divide 1 MiB
        self.assertFalse(is_valid_part_size(0))
        self.assertFalse(is_valid_part_size(-4096))

    def test_verify_coverage_accepts_exact_tiling(self) -> None:
        _verify_coverage([(0, 512), (512, 512), (1024, 100)], 1124)
        _verify_coverage([(1024, 100), (0, 512), (512, 512)], 1124)  # unsorted input

    def test_verify_coverage_rejects_gap(self) -> None:
        with self.assertRaises(ParallelDownloadUnavailable):
            _verify_coverage([(0, 512), (1024, 100)], 1124)

    def test_verify_coverage_rejects_overlap(self) -> None:
        with self.assertRaises(ParallelDownloadUnavailable):
            _verify_coverage([(0, 512), (256, 512)], 768)

    def test_verify_coverage_rejects_wrong_total_size(self) -> None:
        with self.assertRaises(ParallelDownloadUnavailable):
            _verify_coverage([(0, 512)], 1000)

    def test_verify_coverage_rejects_nonpositive_length(self) -> None:
        with self.assertRaises(ParallelDownloadUnavailable):
            _verify_coverage([(0, 0)], 0)

    @unittest.skipUnless(_HAS_PWRITE, "requires os.pwrite")
    def test_pwrite_all_writes_at_exact_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "out.bin"
            fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            try:
                os.ftruncate(fd, 10)
                _pwrite_all(fd, b"world", 5)
                _pwrite_all(fd, b"hello", 0)
            finally:
                os.close(fd)
            self.assertEqual(target.read_bytes(), b"helloworld")

    @unittest.skipUnless(_HAS_PWRITE, "requires os.pwrite")
    def test_pwrite_all_raises_on_nonpositive_write(self) -> None:
        with patch.object(os, "pwrite", lambda *a, **k: 0), self.assertRaises(OSError):
            _pwrite_all(0, b"data", 0)

    def test_extract_file_size_from_document(self) -> None:
        class Doc:
            size = 4242

        class Media:
            document = Doc()
            photo = None

        class Msg:
            media = Media()

        self.assertEqual(_extract_file_size(Msg()), 4242)

    def test_extract_file_size_from_photo_sizes(self) -> None:
        class Size:
            def __init__(self, size: int) -> None:
                self.size = size

        class Photo:
            sizes = [Size(100), Size(9000), Size(500)]

        class Media:
            document = None
            photo = Photo()

        class Msg:
            media = Media()

        self.assertEqual(_extract_file_size(Msg()), 9000)

    def test_extract_file_size_from_progressive_photo_sizes(self) -> None:
        class Progressive:
            size = None
            sizes = [10, 200, 3000]  # cumulative; total is the max

        class Photo:
            sizes = [Progressive()]

        class Media:
            document = None
            photo = Photo()

        class Msg:
            media = Media()

        self.assertEqual(_extract_file_size(Msg()), 3000)

    def test_extract_file_size_from_bare_media_size(self) -> None:
        class Media:
            document = None
            photo = None
            size = 777

        class Msg:
            media = Media()

        self.assertEqual(_extract_file_size(Msg()), 777)

    def test_extract_file_size_returns_none_when_absent(self) -> None:
        class Media:
            document = None
            photo = None
            size = None

        class Msg:
            media = Media()

        self.assertIsNone(_extract_file_size(Msg()))


# --------------------------------------------------------------------------- #
# End-to-end reassembly
# --------------------------------------------------------------------------- #
class TestReassembly(_PatchHelpers, _TmpDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_parallel_download_reassembles_exact_bytes(self) -> None:
        blob = os.urandom(512 * 1024 * 3 + 12345)  # 3 full parts + remainder
        client = FakeClient(blob)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        dest = str(self.tmp / "video.mp4")

        result = await dl.download_media(_make_message(len(blob)), dest)

        self.assertEqual(result, dest)
        self.assertEqual(_read(dest), blob)
        # Senders are created and cleaned up.
        self.assertGreaterEqual(len(client.created_senders), 1)
        self.assertTrue(all(s.disconnected for s in client.created_senders))

    async def test_parallel_download_single_chunk_file(self) -> None:
        blob = os.urandom(100)
        client = FakeClient(blob)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        dest = str(self.tmp / "small.bin")

        await dl.download_media(_make_message(len(blob)), dest)
        self.assertEqual(_read(dest), blob)
        # A single-chunk file needs exactly one sender (n = min(connections, offsets)).
        self.assertEqual(len(client.created_senders), 1)

    async def test_parallel_download_part_aligned_file(self) -> None:
        # Exactly 3 full parts with no remainder: the last chunk is a full part,
        # so the per-chunk length check must accept ``min(part_size,
        # file_size-offset)`` for every chunk including the final one.
        blob = os.urandom(524288 * 3)
        client = FakeClient(blob)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        dest = str(self.tmp / "aligned.bin")

        await dl.download_media(_make_message(len(blob)), dest)
        self.assertEqual(_read(dest), blob)

    async def test_parallel_download_rejects_non_path_destination(self) -> None:
        client = FakeClient(b"x")
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(1), object())

    async def test_parallel_download_unknown_size_refuses(self) -> None:
        client = FakeClient(b"")
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(0), str(self.tmp / "x"))

    async def test_parallel_download_refuses_size_over_ceiling(self) -> None:
        # The declared size comes from untrusted server metadata. A file larger
        # than the configured ceiling must fall back rather than pre-allocate.
        blob = os.urandom(1024)
        client = FakeClient(blob)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288, max_file_size=500)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(1024), str(self.tmp / "big.bin"))
        self.assertFalse(os.path.exists(str(self.tmp / "big.bin")))

    async def test_parallel_download_allows_size_at_ceiling(self) -> None:
        # A file exactly at the ceiling is still allowed (boundary is inclusive).
        blob = os.urandom(500)
        client = FakeClient(blob)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288, max_file_size=500)
        dest = str(self.tmp / "edge.bin")
        await dl.download_media(_make_message(500), dest)
        self.assertEqual(_read(dest), blob)


# --------------------------------------------------------------------------- #
# Failure modes — transactional cleanup + propagation
# --------------------------------------------------------------------------- #
class TestFailureModes(_PatchHelpers, _TmpDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_floodwait_propagates_and_cleans_up(self) -> None:
        blob = os.urandom(524288 * 3)
        client = FakeClient(blob, fail_at_offset=524288, fail_exc=FloodWaitError(request=None))
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        dest = str(self.tmp / "video.mp4")

        with self.assertRaises(FloodWaitError):
            await dl.download_media(_make_message(len(blob)), dest)
        # Transactional: partial output removed, all senders closed.
        self.assertFalse(os.path.exists(dest))
        self.assertTrue(all(s.disconnected for s in client.created_senders))

    async def test_file_reference_expired_propagates(self) -> None:
        blob = os.urandom(524288 * 3)
        client = FakeClient(blob, fail_at_offset=0, fail_exc=FileReferenceExpiredError(request=None))
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        dest = str(self.tmp / "video.mp4")

        with self.assertRaises(FileReferenceExpiredError):
            await dl.download_media(_make_message(len(blob)), dest)
        self.assertFalse(os.path.exists(dest))

    async def test_file_reference_expired_mid_transfer_propagates(self) -> None:
        # A stale reference can surface on a later chunk, not just the first one.
        # It must still propagate unchanged (so the caller refreshes), cancel
        # siblings, and remove the partial output.
        blob = os.urandom(524288 * 4)
        client = FakeClient(blob, fail_at_offset=524288 * 2, fail_exc=FileReferenceExpiredError(request=None))
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        dest = str(self.tmp / "video.mp4")

        with self.assertRaises(FileReferenceExpiredError):
            await dl.download_media(_make_message(len(blob)), dest)
        self.assertFalse(os.path.exists(dest))
        self.assertTrue(all(s.disconnected for s in client.created_senders))

    async def test_blob_shorter_than_declared_size_aborts(self) -> None:
        # If the server returns fewer bytes than the declared size (so a tail
        # chunk comes back empty/short), the guard fires and the partial is gone.
        declared = 524288 * 3
        client = FakeClient(os.urandom(524288 * 2))  # blob is a full part short
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=2, part_size=524288)
        dest = str(self.tmp / "truncated.bin")

        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(declared), dest)
        self.assertFalse(os.path.exists(dest))

    async def test_short_chunk_is_detected_and_aborts(self) -> None:
        blob = os.urandom(524288 * 2)
        # Drop a byte from the FIRST part (a full part), so the length check fires.
        client = FakeClient(blob, short_at_offset=0)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=2, part_size=524288)
        dest = str(self.tmp / "video.mp4")

        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(len(blob)), dest)
        self.assertFalse(os.path.exists(dest))

    async def test_concurrent_workers_do_not_corrupt_offsets(self) -> None:
        # Many small parts across several senders; the only way the output equals
        # the blob is if every worker wrote at its exact offset (os.pwrite).
        part = 4096
        blob = os.urandom(part * 50 + 17)
        client = FakeClient(blob)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=8, part_size=part)
        dest = str(self.tmp / "many.bin")

        await dl.download_media(_make_message(len(blob)), dest)
        self.assertEqual(_read(dest), blob)

    async def test_cleanup_tolerates_missing_partial_file(self) -> None:
        # A chunk failure triggers cleanup; if the partial file is already gone,
        # os.remove's OSError must be swallowed and the original error re-raised.
        blob = os.urandom(524288 * 2)
        client = FakeClient(blob, short_at_offset=0)
        self._patch_sender(client)
        dl = ParallelDownloader(client, connections=2, part_size=524288)
        dest = str(self.tmp / "video.mp4")

        real_remove = os.remove

        def _remove_then_vanish(path: Any) -> None:
            real_remove(path)
            raise FileNotFoundError(path)  # simulate a concurrent unlink

        self._patch_object(os, "remove", _remove_then_vanish)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(len(blob)), dest)
        self.assertFalse(os.path.exists(dest))


# --------------------------------------------------------------------------- #
# Capability probe (synchronous)
# --------------------------------------------------------------------------- #
class TestCapabilityProbe(_PatchHelpers, unittest.TestCase):
    @unittest.skipUnless(_HAS_PWRITE, "probe requires os.pwrite")
    def test_supports_parallel_download_true_for_complete_client(self) -> None:
        client = FakeClient(b"x")
        self.assertIs(supports_parallel_download(client), True)

    def test_supports_parallel_download_false_when_internal_missing(self) -> None:
        class Incomplete:
            # Missing _call, _connection, etc.
            session = FakeSession()

        self.assertIs(supports_parallel_download(Incomplete()), False)

    def test_supports_parallel_download_false_when_get_input_location_gone(self) -> None:
        client = FakeClient(b"x")
        self._delattr_temp(utils, "get_input_location")
        self.assertIs(supports_parallel_download(client), False)

    def test_supports_parallel_download_false_without_pwrite(self) -> None:
        # On non-POSIX platforms os.pwrite is absent; the probe must degrade.
        client = FakeClient(b"x")
        self._delattr_temp(os, "pwrite")
        self.assertIs(supports_parallel_download(client), False)

    def test_invalid_part_size_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            ParallelDownloader(FakeClient(b"x"), connections=4, part_size=3000)


# --------------------------------------------------------------------------- #
# Sender construction / teardown
# --------------------------------------------------------------------------- #
class TestSenderLifecycle(_PatchHelpers, _TmpDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_download_media_refuses_when_client_incomplete(self) -> None:
        client = FakeClient(b"x" * 100)
        self._patch("src.parallel_download.supports_parallel_download", lambda c: False)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(100), str(self.tmp / "x.bin"))

    async def test_build_senders_closes_on_unavailable(self) -> None:
        client = FakeClient(b"x" * 100)
        closed: list[Any] = []

        def _connect(_self: ParallelDownloader, dc_id: int, auth_key: Any) -> Any:
            async def _inner() -> None:
                raise ParallelDownloadUnavailable("nope")

            return _inner()

        self._patch_object(ParallelDownloader, "_connect_sender", _connect)

        async def _track_close(_self: ParallelDownloader, senders: Any) -> None:
            closed.append(senders)

        self._patch_object(ParallelDownloader, "_close_senders", _track_close)
        dl = ParallelDownloader(client, connections=2, part_size=524288)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl._build_senders(client.session.dc_id, 2)
        self.assertTrue(closed)  # cleanup ran on the ParallelDownloadUnavailable path

    async def test_close_senders_swallows_disconnect_errors(self) -> None:
        dl = ParallelDownloader(FakeClient(b"x"), connections=4, part_size=524288)

        class BadSender:
            async def disconnect(self) -> None:
                raise RuntimeError("boom")

        # Must not raise even though disconnect() fails.
        await dl._close_senders([BadSender(), BadSender()])

    async def test_download_media_falls_back_when_location_unresolvable(self) -> None:
        client = FakeClient(b"x" * 100)
        self._patch_object(ParallelDownloader, "_connect_sender", lambda self, d, k: None)

        def _boom(_message: Any) -> Any:
            raise TypeError("not a downloadable message")

        self._patch_object(utils, "get_input_location", _boom)
        dl = ParallelDownloader(client, connections=4, part_size=524288)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl.download_media(_make_message(100), str(self.tmp / "x.bin"))

    async def test_build_senders_wraps_non_flood_errors(self) -> None:
        client = FakeClient(b"x" * 100)

        def _boom(_self: ParallelDownloader, dc_id: int, auth_key: Any) -> Any:
            async def _inner() -> None:
                raise RuntimeError("connect failed")

            return _inner()

        self._patch_object(ParallelDownloader, "_connect_sender", _boom)
        dl = ParallelDownloader(client, connections=2, part_size=524288)
        with self.assertRaises(ParallelDownloadUnavailable):
            await dl._build_senders(client.session.dc_id, 2)

    async def test_build_senders_propagates_floodwait(self) -> None:
        client = FakeClient(b"x" * 100)

        def _flood(_self: ParallelDownloader, dc_id: int, auth_key: Any) -> Any:
            async def _inner() -> None:
                raise FloodWaitError(request=None)

            return _inner()

        self._patch_object(ParallelDownloader, "_connect_sender", _flood)
        dl = ParallelDownloader(client, connections=2, part_size=524288)
        # FloodWait must surface unchanged so the single budget governs it.
        with self.assertRaises(FloodWaitError):
            await dl._build_senders(client.session.dc_id, 2)


# --------------------------------------------------------------------------- #
# Foreign-DC export path
# --------------------------------------------------------------------------- #
class TestForeignDC(_PatchHelpers, _TmpDirMixin, unittest.IsolatedAsyncioTestCase):
    def _patch_foreign_sender(self, client: FakeClient) -> None:
        def _connect_sender_stub(_self: ParallelDownloader, dc_id: int, auth_key: Any) -> Any:
            async def _inner() -> FakeSender:
                s = client.make_sender()
                s.dc_id = dc_id
                s.connected = True
                return s

            return _inner()

        self._patch_object(ParallelDownloader, "_connect_sender", _connect_sender_stub)

    async def test_foreign_dc_exports_auth_once(self) -> None:
        blob = os.urandom(524288 * 4)  # 4 parts so a 3-sender pool is fully built
        client = ForeignDCClient(blob, home_dc=2)

        # File lives on DC 4 (foreign). get_input_location reports dc_id=4.
        self._patch_object(utils, "get_input_location", lambda m: (4, object()))
        self._patch_foreign_sender(client)

        dl = ParallelDownloader(client, connections=3, part_size=524288)
        dest = str(self.tmp / "foreign.bin")
        await dl.download_media(_make_message(len(blob)), dest)

        self.assertEqual(_read(dest), blob)
        # Auth exported exactly once even though 3 senders were created.
        self.assertEqual(len(client.export_calls), 1)
        self.assertEqual(len(client.created_senders), 3)
        # The shared init request's query must be restored after the transient
        # ImportAuthorizationRequest, so concurrent users never observe it.
        self.assertIsNone(client._init_request.query)

    async def test_foreign_dc_export_floodwait_propagates_and_restores_query(self) -> None:
        # A FloodWait raised by the auth export must surface unchanged (single
        # budget), and the shared _init_request.query must still be restored.
        blob = os.urandom(524288 * 2)

        class _FloodOnExport(FakeClient):
            async def __call__(self, request: Any) -> Any:
                raise FloodWaitError(request=None)

        client = _FloodOnExport(blob, home_dc=2)
        self._patch_object(utils, "get_input_location", lambda m: (4, object()))
        self._patch_foreign_sender(client)

        dl = ParallelDownloader(client, connections=3, part_size=524288)
        with self.assertRaises(FloodWaitError):
            await dl.download_media(_make_message(len(blob)), str(self.tmp / "f.bin"))
        # Even on the export failure the query must be restored (try/finally).
        self.assertIsNone(client._init_request.query)
        # All senders opened so far are cleaned up.
        self.assertTrue(all(s.disconnected for s in client.created_senders))


# --------------------------------------------------------------------------- #
# Backup-layer gating (_should_parallelize)
# --------------------------------------------------------------------------- #
class TestBackupGating(_PatchHelpers, unittest.TestCase):
    def test_gate_off_by_default(self) -> None:
        backup = _make_backup(enabled=False)
        self.assertIs(
            backup._should_parallelize(_make_message(100 * 1024 * 1024), 100 * 1024 * 1024),
            False,
        )

    def test_gate_skips_small_files_when_enabled(self) -> None:
        backup = _make_backup(enabled=True, min_mb=20)
        # 5 MB < 20 MB threshold -> single stream
        self.assertIs(
            backup._should_parallelize(_make_message(5 * 1024 * 1024), 5 * 1024 * 1024),
            False,
        )

    @unittest.skipUnless(_HAS_PWRITE, "probe requires os.pwrite")
    def test_gate_enables_for_large_files(self) -> None:
        backup = _make_backup(enabled=True, min_mb=20)
        # supports_parallel_download is True for a fully-mocked client only if it
        # has the internals; MagicMock auto-provides every attribute, so the
        # probe passes (on platforms that have os.pwrite).
        self.assertIs(
            backup._should_parallelize(_make_message(50 * 1024 * 1024), 50 * 1024 * 1024),
            True,
        )

    def test_gate_disabled_after_capability_probe_fails(self) -> None:
        backup = _make_backup(enabled=True, min_mb=1)
        self._patch("src.telegram_backup.supports_parallel_download", lambda c: False)
        self.assertIs(
            backup._should_parallelize(_make_message(50 * 1024 * 1024), 50 * 1024 * 1024),
            False,
        )
        # Latches off for the rest of the run.
        self.assertIs(backup._parallel_download_disabled, True)


# --------------------------------------------------------------------------- #
# Backup-layer seam (_fetch_media_bytes)
# --------------------------------------------------------------------------- #
class TestFetchMediaBytes(_PatchHelpers, unittest.IsolatedAsyncioTestCase):
    async def test_fetch_media_bytes_uses_single_stream_when_disabled(self) -> None:
        backup = _make_backup(enabled=False)
        backup.client.download_media = AsyncMock(return_value="/tmp/out")
        result = await backup._fetch_media_bytes(_make_message(99 * 1024 * 1024), "/tmp/out", 99 * 1024 * 1024)
        self.assertEqual(result, "/tmp/out")
        backup.client.download_media.assert_awaited_once()

    async def test_fetch_media_bytes_falls_back_on_unavailable(self) -> None:
        backup = _make_backup(enabled=True, min_mb=1)
        backup.client.download_media = AsyncMock(return_value="/tmp/out")

        class _DL:
            async def download_media(self, message: Any, path: str) -> str:
                raise ParallelDownloadUnavailable("nope")

        self._patch("src.telegram_backup.ParallelDownloader", lambda *a, **k: _DL())
        result = await backup._fetch_media_bytes(_make_message(50 * 1024 * 1024), "/tmp/out", 50 * 1024 * 1024)
        # Transparent fallback to single-stream for this file.
        self.assertEqual(result, "/tmp/out")
        backup.client.download_media.assert_awaited_once()

    async def test_fetch_media_bytes_propagates_floodwait(self) -> None:
        backup = _make_backup(enabled=True, min_mb=1)
        backup.client.download_media = AsyncMock(return_value="/tmp/out")

        class _DL:
            async def download_media(self, message: Any, path: str) -> str:
                raise FloodWaitError(request=None)

        self._patch("src.telegram_backup.ParallelDownloader", lambda *a, **k: _DL())
        with self.assertRaises(FloodWaitError):
            await backup._fetch_media_bytes(_make_message(50 * 1024 * 1024), "/tmp/out", 50 * 1024 * 1024)
        # FloodWait must NOT be swallowed into a single-stream retry here.
        backup.client.download_media.assert_not_awaited()

    async def test_fetch_media_bytes_uses_parallel_when_enabled(self) -> None:
        # Happy path: gate passes -> ParallelDownloader is used and its path
        # returned, and the single-stream client.download_media is NOT touched.
        backup = _make_backup(enabled=True, min_mb=1)
        backup.client.download_media = AsyncMock(return_value="/tmp/single")

        calls: list[str] = []

        class _DL:
            async def download_media(self, message: Any, path: str) -> str:
                calls.append(path)
                return path

        self._patch("src.telegram_backup.ParallelDownloader", lambda *a, **k: _DL())
        result = await backup._fetch_media_bytes(_make_message(50 * 1024 * 1024), "/tmp/parallel", 50 * 1024 * 1024)
        self.assertEqual(result, "/tmp/parallel")
        self.assertEqual(calls, ["/tmp/parallel"])
        backup.client.download_media.assert_not_awaited()

    async def test_fetch_media_bytes_reuses_cached_downloader(self) -> None:
        # The ParallelDownloader is built once and cached on the backup instance.
        backup = _make_backup(enabled=True, min_mb=1)
        backup.client.download_media = AsyncMock(return_value="/tmp/single")

        build_count = {"n": 0}

        class _DL:
            async def download_media(self, message: Any, path: str) -> str:
                return path

        def _factory(*a: Any, **k: Any) -> _DL:
            build_count["n"] += 1
            return _DL()

        self._patch("src.telegram_backup.ParallelDownloader", _factory)
        msg = _make_message(50 * 1024 * 1024)
        await backup._fetch_media_bytes(msg, "/tmp/a", 50 * 1024 * 1024)
        await backup._fetch_media_bytes(msg, "/tmp/b", 50 * 1024 * 1024)
        self.assertEqual(build_count["n"], 1)


if __name__ == "__main__":
    unittest.main()
