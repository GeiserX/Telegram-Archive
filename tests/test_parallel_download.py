"""Tests for per-file parallel chunked downloads (issue #183).

Covers the failure modes that actually matter for safe, unattended reassembly:
exact-offset writes, coverage verification (gaps/overlaps/wrong size), dropped
or short chunks, racing writers, mid-transfer FileReferenceExpired, FloodWait
propagation through the single budget, transactional cleanup, capability-probe
fallback, and the backup-layer size/flag gating.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
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


def _read(path):
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_is_valid_part_size_accepts_only_telegram_constraints():
    assert is_valid_part_size(524288)  # 512 KiB max
    assert is_valid_part_size(131072)  # 128 KiB divides 1 MiB
    assert is_valid_part_size(4096)  # 4 KiB minimum alignment
    assert not is_valid_part_size(524288 + 4096)  # exceeds max
    assert not is_valid_part_size(1048576)  # exceeds max
    assert not is_valid_part_size(3000)  # not a 4 KiB multiple
    assert not is_valid_part_size(393216)  # 384 KiB does not divide 1 MiB
    assert not is_valid_part_size(0)
    assert not is_valid_part_size(-4096)


def test_verify_coverage_accepts_exact_tiling():
    _verify_coverage([(0, 512), (512, 512), (1024, 100)], 1124)
    _verify_coverage([(1024, 100), (0, 512), (512, 512)], 1124)  # unsorted input


def test_verify_coverage_rejects_gap():
    with pytest.raises(ParallelDownloadUnavailable):
        _verify_coverage([(0, 512), (1024, 100)], 1124)


def test_verify_coverage_rejects_overlap():
    with pytest.raises(ParallelDownloadUnavailable):
        _verify_coverage([(0, 512), (256, 512)], 768)


def test_verify_coverage_rejects_wrong_total_size():
    with pytest.raises(ParallelDownloadUnavailable):
        _verify_coverage([(0, 512)], 1000)


def test_verify_coverage_rejects_nonpositive_length():
    with pytest.raises(ParallelDownloadUnavailable):
        _verify_coverage([(0, 0)], 0)


def test_pwrite_all_writes_at_exact_offset(tmp_path):
    target = tmp_path / "out.bin"
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.ftruncate(fd, 10)
        _pwrite_all(fd, b"world", 5)
        _pwrite_all(fd, b"hello", 0)
    finally:
        os.close(fd)
    assert target.read_bytes() == b"helloworld"


def test_extract_file_size_from_document():
    class Doc:
        size = 4242

    class Media:
        document = Doc()
        photo = None

    class Msg:
        media = Media()

    assert _extract_file_size(Msg()) == 4242


def test_extract_file_size_from_photo_sizes():
    class Size:
        def __init__(self, size):
            self.size = size

    class Photo:
        sizes = [Size(100), Size(9000), Size(500)]

    class Media:
        document = None
        photo = Photo()

    class Msg:
        media = Media()

    assert _extract_file_size(Msg()) == 9000


# --------------------------------------------------------------------------- #
# Fake Telethon client / sender harness
# --------------------------------------------------------------------------- #
class _GetFileResult:
    def __init__(self, data):
        self.bytes = data


class FakeSender:
    """Records connect/disconnect; bytes come from the client's blob."""

    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.dc_id = None
        self.auth_key = object()

    async def connect(self, _connection):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def send(self, _request):
        return None


class FakeDC:
    ip_address = "127.0.0.1"
    port = 443

    def __init__(self, dc_id):
        self.id = dc_id


class FakeSession:
    def __init__(self, dc_id=2):
        self.dc_id = dc_id
        self.auth_key = object()


class FakeLog(dict):
    def __getitem__(self, k):
        import logging

        return logging.getLogger("fake")


class FakeClient:
    """Minimal stand-in exposing the private internals the transferrer uses.

    ``blob`` is the full file content; ``_call`` slices it by the request's
    offset/limit, so a correctly reassembled output must equal ``blob``.
    """

    def __init__(self, blob, *, home_dc=2, fail_at_offset=None, fail_exc=None, short_at_offset=None):
        self.blob = blob
        self.session = FakeSession(home_dc)
        self._log = FakeLog()
        self._proxy = None
        self._local_addr = None
        self._init_request = type("Init", (), {"query": None})()
        self._borrow_sender_lock = asyncio.Lock()
        self.created_senders = []
        self._fail_at_offset = fail_at_offset
        self._fail_exc = fail_exc
        self._short_at_offset = short_at_offset

    async def _get_dc(self, dc_id):
        return FakeDC(dc_id)

    def _connection(self, *a, **k):
        return object()

    async def _call(self, sender, request):
        offset = request.offset
        limit = request.limit
        if self._fail_at_offset is not None and offset == self._fail_at_offset:
            raise self._fail_exc
        data = self.blob[offset : offset + limit]
        if self._short_at_offset is not None and offset == self._short_at_offset:
            data = data[:-1]  # drop a byte to simulate a short/corrupt chunk
        return _GetFileResult(data)

    # Patched factory so we count and inspect senders without real I/O.
    def make_sender(self):
        s = FakeSender()
        self.created_senders.append(s)
        return s


@pytest.fixture
def patch_sender(monkeypatch):
    """Replace MTProtoSender construction with FakeSender bound to the client."""

    def _apply(client):
        def _connect_sender_stub(self, dc_id, auth_key):
            async def _inner():
                s = client.make_sender()
                s.dc_id = dc_id
                s.connected = True
                return s

            return _inner()

        monkeypatch.setattr(ParallelDownloader, "_connect_sender", _connect_sender_stub)
        # get_input_location returns (dc_id, location); location is opaque here.
        monkeypatch.setattr(utils, "get_input_location", lambda m: (client.session.dc_id, object()))

    return _apply


def _make_message(size):
    class Doc:
        def __init__(self, size):
            self.size = size

    class Media:
        def __init__(self, size):
            self.document = Doc(size)
            self.photo = None

    class Msg:
        def __init__(self, size):
            self.media = Media(size)

    return Msg(size)


# --------------------------------------------------------------------------- #
# End-to-end reassembly
# --------------------------------------------------------------------------- #
async def test_parallel_download_reassembles_exact_bytes(tmp_path, patch_sender):
    blob = os.urandom(512 * 1024 * 3 + 12345)  # 3 full parts + remainder
    client = FakeClient(blob)
    patch_sender(client)
    dl = ParallelDownloader(client, connections=4, part_size=524288)
    dest = str(tmp_path / "video.mp4")

    result = await dl.download_media(_make_message(len(blob)), dest)

    assert result == dest
    assert _read(dest) == blob
    # Senders are created and cleaned up.
    assert len(client.created_senders) >= 1
    assert all(s.disconnected for s in client.created_senders)


async def test_parallel_download_single_chunk_file(tmp_path, patch_sender):
    blob = os.urandom(100)
    client = FakeClient(blob)
    patch_sender(client)
    dl = ParallelDownloader(client, connections=4, part_size=524288)
    dest = str(tmp_path / "small.bin")

    await dl.download_media(_make_message(len(blob)), dest)
    assert _read(dest) == blob


async def test_parallel_download_rejects_non_path_destination(tmp_path, patch_sender):
    client = FakeClient(b"x")
    patch_sender(client)
    dl = ParallelDownloader(client, connections=4, part_size=524288)
    with pytest.raises(ParallelDownloadUnavailable):
        await dl.download_media(_make_message(1), object())


async def test_parallel_download_unknown_size_refuses(tmp_path, patch_sender):
    client = FakeClient(b"")
    patch_sender(client)
    dl = ParallelDownloader(client, connections=4, part_size=524288)
    with pytest.raises(ParallelDownloadUnavailable):
        await dl.download_media(_make_message(0), str(tmp_path / "x"))


# --------------------------------------------------------------------------- #
# Failure modes — transactional cleanup + propagation
# --------------------------------------------------------------------------- #
async def test_floodwait_propagates_and_cleans_up(tmp_path, patch_sender):
    blob = os.urandom(524288 * 3)
    client = FakeClient(blob, fail_at_offset=524288, fail_exc=FloodWaitError(request=None))
    patch_sender(client)
    dl = ParallelDownloader(client, connections=4, part_size=524288)
    dest = str(tmp_path / "video.mp4")

    with pytest.raises(FloodWaitError):
        await dl.download_media(_make_message(len(blob)), dest)
    # Transactional: partial output removed, all senders closed.
    assert not os.path.exists(dest)
    assert all(s.disconnected for s in client.created_senders)


async def test_file_reference_expired_propagates(tmp_path, patch_sender):
    blob = os.urandom(524288 * 3)
    client = FakeClient(blob, fail_at_offset=0, fail_exc=FileReferenceExpiredError(request=None))
    patch_sender(client)
    dl = ParallelDownloader(client, connections=4, part_size=524288)
    dest = str(tmp_path / "video.mp4")

    with pytest.raises(FileReferenceExpiredError):
        await dl.download_media(_make_message(len(blob)), dest)
    assert not os.path.exists(dest)


async def test_short_chunk_is_detected_and_aborts(tmp_path, patch_sender):
    blob = os.urandom(524288 * 2)
    # Drop a byte from the FIRST part (a full part), so the length check fires.
    client = FakeClient(blob, short_at_offset=0)
    patch_sender(client)
    dl = ParallelDownloader(client, connections=2, part_size=524288)
    dest = str(tmp_path / "video.mp4")

    with pytest.raises(ParallelDownloadUnavailable):
        await dl.download_media(_make_message(len(blob)), dest)
    assert not os.path.exists(dest)


async def test_concurrent_workers_do_not_corrupt_offsets(tmp_path, patch_sender):
    # Many small parts across several senders; the only way the output equals
    # the blob is if every worker wrote at its exact offset (os.pwrite).
    part = 4096
    blob = os.urandom(part * 50 + 17)
    client = FakeClient(blob)
    patch_sender(client)
    dl = ParallelDownloader(client, connections=8, part_size=part)
    dest = str(tmp_path / "many.bin")

    await dl.download_media(_make_message(len(blob)), dest)
    assert _read(dest) == blob


# --------------------------------------------------------------------------- #
# Capability probe
# --------------------------------------------------------------------------- #
def test_supports_parallel_download_true_for_complete_client():
    client = FakeClient(b"x")
    assert supports_parallel_download(client) is True


def test_supports_parallel_download_false_when_internal_missing():
    class Incomplete:
        # Missing _call, _connection, etc.
        session = FakeSession()

    assert supports_parallel_download(Incomplete()) is False


def test_supports_parallel_download_false_when_get_input_location_gone(monkeypatch):
    client = FakeClient(b"x")
    monkeypatch.delattr(utils, "get_input_location")
    assert supports_parallel_download(client) is False


async def test_invalid_part_size_rejected_at_construction():
    with pytest.raises(ValueError):
        ParallelDownloader(FakeClient(b"x"), connections=4, part_size=3000)


# --------------------------------------------------------------------------- #
# Foreign-DC export path
# --------------------------------------------------------------------------- #
class ForeignDCClient(FakeClient):
    """FakeClient whose ``client(request)`` records auth-export calls."""

    def __init__(self, blob, **kw):
        super().__init__(blob, **kw)
        self.export_calls = []

    async def __call__(self, request):
        self.export_calls.append(request)
        return type("Auth", (), {"id": 1, "bytes": b"k"})()


async def test_foreign_dc_exports_auth_once(tmp_path, monkeypatch):
    blob = os.urandom(524288 * 4)  # 4 parts so a 3-sender pool is fully built
    client = ForeignDCClient(blob, home_dc=2)

    # File lives on DC 4 (foreign). get_input_location reports dc_id=4.
    monkeypatch.setattr(utils, "get_input_location", lambda m: (4, object()))

    def _connect_sender_stub(self, dc_id, auth_key):
        async def _inner():
            s = client.make_sender()
            s.dc_id = dc_id
            s.connected = True
            return s

        return _inner()

    monkeypatch.setattr(ParallelDownloader, "_connect_sender", _connect_sender_stub)

    dl = ParallelDownloader(client, connections=3, part_size=524288)
    dest = str(tmp_path / "foreign.bin")
    await dl.download_media(_make_message(len(blob)), dest)

    assert _read(dest) == blob
    # Auth exported exactly once even though 3 senders were created.
    assert len(client.export_calls) == 1
    assert len(client.created_senders) == 3


# --------------------------------------------------------------------------- #
# Backup-layer gating (_should_parallelize / _fetch_media_bytes)
# --------------------------------------------------------------------------- #
def _make_backup(*, enabled, min_mb=20, conns=4, part_kb=512):
    backup = TelegramBackup.__new__(TelegramBackup)
    cfg = MagicMock()
    cfg.parallel_download_enabled = enabled
    cfg.parallel_download_connections = conns
    cfg.parallel_download_part_size_kb = part_kb
    cfg.get_parallel_download_min_size_bytes = MagicMock(return_value=min_mb * 1024 * 1024)
    cfg.get_parallel_download_part_size_bytes = MagicMock(return_value=part_kb * 1024)
    backup.config = cfg
    backup.client = MagicMock()
    backup._parallel_downloader = None
    backup._parallel_download_disabled = False
    return backup


def test_gate_off_by_default():
    backup = _make_backup(enabled=False)
    assert backup._should_parallelize(_make_message(100 * 1024 * 1024), 100 * 1024 * 1024) is False


def test_gate_skips_small_files_when_enabled():
    backup = _make_backup(enabled=True, min_mb=20)
    # 5 MB < 20 MB threshold -> single stream
    assert backup._should_parallelize(_make_message(5 * 1024 * 1024), 5 * 1024 * 1024) is False


def test_gate_enables_for_large_files():
    backup = _make_backup(enabled=True, min_mb=20)
    # supports_parallel_download is True for a fully-mocked client only if it has
    # the internals; MagicMock auto-provides every attribute, so the probe passes.
    assert backup._should_parallelize(_make_message(50 * 1024 * 1024), 50 * 1024 * 1024) is True


def test_gate_disabled_after_capability_probe_fails(monkeypatch):
    backup = _make_backup(enabled=True, min_mb=1)
    monkeypatch.setattr("src.telegram_backup.supports_parallel_download", lambda c: False)
    assert backup._should_parallelize(_make_message(50 * 1024 * 1024), 50 * 1024 * 1024) is False
    # Latches off for the rest of the run.
    assert backup._parallel_download_disabled is True


async def test_fetch_media_bytes_uses_single_stream_when_disabled():
    backup = _make_backup(enabled=False)
    backup.client.download_media = AsyncMock(return_value="/tmp/out")
    result = await backup._fetch_media_bytes(_make_message(99 * 1024 * 1024), "/tmp/out", 99 * 1024 * 1024)
    assert result == "/tmp/out"
    backup.client.download_media.assert_awaited_once()


async def test_fetch_media_bytes_falls_back_on_unavailable(monkeypatch):
    backup = _make_backup(enabled=True, min_mb=1)
    backup.client.download_media = AsyncMock(return_value="/tmp/out")

    class _DL:
        async def download_media(self, message, path):
            raise ParallelDownloadUnavailable("nope")

    monkeypatch.setattr("src.telegram_backup.ParallelDownloader", lambda *a, **k: _DL())
    result = await backup._fetch_media_bytes(_make_message(50 * 1024 * 1024), "/tmp/out", 50 * 1024 * 1024)
    # Transparent fallback to single-stream for this file.
    assert result == "/tmp/out"
    backup.client.download_media.assert_awaited_once()


async def test_fetch_media_bytes_propagates_floodwait(monkeypatch):
    backup = _make_backup(enabled=True, min_mb=1)
    backup.client.download_media = AsyncMock(return_value="/tmp/out")

    class _DL:
        async def download_media(self, message, path):
            raise FloodWaitError(request=None)

    monkeypatch.setattr("src.telegram_backup.ParallelDownloader", lambda *a, **k: _DL())
    with pytest.raises(FloodWaitError):
        await backup._fetch_media_bytes(_make_message(50 * 1024 * 1024), "/tmp/out", 50 * 1024 * 1024)
    # FloodWait must NOT be swallowed into a single-stream retry here.
    backup.client.download_media.assert_not_awaited()
