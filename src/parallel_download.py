"""Per-file parallel chunked media downloads (issue #183).

This module provides a *bytes-fetching primitive* that can replace
``TelegramClient.download_media`` at the existing download seam in
``telegram_backup.py``. It splits one file into fixed-size chunks fetched
concurrently over several independent MTProto senders to a single DC, then
reassembles them by writing each chunk at its exact byte offset.

Design constraints (issue #183, all enforced here):

1. **Gated by the caller.** This module never decides *whether* to run in
   parallel; the backup layer gates on a config flag and a size threshold and
   only constructs/uses :class:`ParallelDownloader` for large files. The
   primitive still falls back defensively (raising
   :class:`ParallelDownloadUnavailable`) when it cannot operate safely.
2. **Bounded, low sender count.** Concurrency is capped by ``connections``,
   which the config layer clamps well under Telegram's ~20-connection cliff.
3. **Single flood budget.** A chunk that hits ``FLOOD_WAIT`` cancels its
   siblings and re-raises the *original* :class:`FloodWaitError`, so the
   caller's ``call_with_flood_retry`` wrapper applies the one shared budget and
   restarts the whole transfer — there is no second backoff scheme here.
4. **Deterministic, verified reassembly.** Each chunk is written with
   ``os.pwrite`` at its exact request offset (no shared seek pointer), and
   before the file is accepted we assert the written byte ranges cover
   ``[0, file_size)`` exactly once, with no gaps or overlaps.
5. **Transactional.** Any chunk failure cancels siblings, closes every sender,
   deletes the partial output, and re-raises the original exception type so the
   caller's retry / ``FileReferenceExpired`` refresh logic is unchanged.
6. **Bounded memory.** Chunks stream straight to disk; peak resident bytes are
   roughly ``connections * part_size`` (default 4 * 512 KiB = 2 MiB).
"""

import asyncio
import logging
import os

from telethon import utils
from telethon.errors import FileMigrateError, FileReferenceExpiredError, FloodWaitError
from telethon.network import MTProtoSender
from telethon.tl import functions
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)

logger = logging.getLogger(__name__)

# Telegram's upload.getFile constraints: ``offset`` and ``limit`` must be
# multiples of 4 KiB, a single request may not exceed 512 KiB, and one request
# must not straddle a 1 MiB boundary. A part size that is a multiple of 4 KiB
# and divides 1 MiB satisfies all three (offsets are multiples of the part
# size, so they stay 4 KiB-aligned and inside a single 1 MiB block).
CHUNK_ALIGN = 4096
MAX_PART_SIZE = 524288  # 512 KiB — Telegram's per-request ceiling
ONE_MB = 1048576


class ParallelDownloadUnavailable(Exception):
    """Raised when a file cannot be downloaded with the parallel transferrer.

    The backup layer treats this as a signal to fall back to the proven
    single-stream ``client.download_media`` path for this file (and, on the
    first occurrence, to stop attempting parallel downloads for the run).
    """


def is_valid_part_size(part_size: int) -> bool:
    """Return True if ``part_size`` satisfies Telegram's getFile constraints."""
    return (
        isinstance(part_size, int)
        and part_size > 0
        and part_size <= MAX_PART_SIZE
        and part_size % CHUNK_ALIGN == 0
        and ONE_MB % part_size == 0
    )


def supports_parallel_download(client) -> bool:
    """Probe whether the live Telethon client exposes the internals we need.

    Telethon v1 is archived but still receives occasional releases; if a future
    version renames these private members we degrade to single-stream instead
    of crashing.
    """
    required = ("_get_dc", "_connection", "_call", "_log", "session", "_init_request", "_borrow_sender_lock")
    if not all(hasattr(client, attr) for attr in required):
        return False
    if not hasattr(client, "_proxy"):
        return False
    # ``os.pwrite`` is POSIX-only; on platforms without it (e.g. Windows) we
    # degrade here at probe time rather than crashing mid-transfer.
    if not hasattr(os, "pwrite"):
        return False
    return hasattr(utils, "get_input_location")


def _verify_coverage(written: list[tuple[int, int]], file_size: int) -> None:
    """Assert the written ``(offset, length)`` ranges tile ``[0, file_size)``.

    Raises :class:`ParallelDownloadUnavailable` on any gap, overlap, or size
    mismatch. This is the safety net against a mis-reassembled file that would
    otherwise pass the caller's size-only check, get hashed, and propagate
    through content-hash dedup (issue #183, hard requirement #4).
    """
    ranges = sorted(written)
    cursor = 0
    for offset, length in ranges:
        if offset != cursor:
            raise ParallelDownloadUnavailable(f"chunk coverage gap/overlap at offset {offset} (expected {cursor})")
        if length <= 0:
            raise ParallelDownloadUnavailable(f"non-positive chunk length {length} at offset {offset}")
        cursor += length
    if cursor != file_size:
        raise ParallelDownloadUnavailable(f"chunk coverage total {cursor} != file_size {file_size}")


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    """Write ``data`` at ``offset`` with ``os.pwrite``, handling short writes.

    ``os.pwrite`` uses an explicit offset and never touches the file's seek
    pointer, so concurrent workers sharing one fd never corrupt each other's
    write position.
    """
    view = memoryview(data)
    written = 0
    while written < len(view):
        n = os.pwrite(fd, view[written:], offset + written)
        if n <= 0:
            raise OSError("os.pwrite returned non-positive byte count")
        written += n


class ParallelDownloader:
    """Fetches one file at a time over a bounded pool of independent senders."""

    def __init__(self, client, *, connections: int, part_size: int, max_file_size: int | None = None):
        if not is_valid_part_size(part_size):
            raise ValueError(f"invalid part_size {part_size}; must be a 4 KiB multiple dividing 1 MiB, <= 512 KiB")
        self._client = client
        self._connections = max(1, int(connections))
        self._part_size = int(part_size)
        # Defence-in-depth: the declared size comes from untrusted server
        # metadata. A ceiling caps how many chunks (and how large a
        # pre-allocation) one transfer can request before we fall back.
        self._max_file_size = int(max_file_size) if max_file_size and max_file_size > 0 else None

    async def download_media(self, message, file) -> str:
        """Download ``message``'s media to path ``file`` and return ``file``.

        Mirrors the subset of ``TelegramClient.download_media`` the backup seam
        relies on (message in, destination path in, path out), so it is a
        drop-in replacement under ``call_with_flood_retry``.
        """
        if not isinstance(file, str):
            raise ParallelDownloadUnavailable("parallel download requires a destination path")
        if not supports_parallel_download(self._client):
            raise ParallelDownloadUnavailable("Telethon client is missing required internals")

        try:
            dc_id, location = utils.get_input_location(message)
        except Exception as exc:  # noqa: BLE001 — any cast failure means fall back
            raise ParallelDownloadUnavailable(f"cannot resolve input location: {exc}") from exc

        # ``get_input_location`` drops the size; recover it from the file info so
        # we can chunk deterministically. Without an exact size we cannot verify
        # coverage, so we refuse rather than guess.
        file_size = _extract_file_size(message)
        if not file_size or file_size <= 0:
            raise ParallelDownloadUnavailable("unknown file size")
        if self._max_file_size is not None and file_size > self._max_file_size:
            raise ParallelDownloadUnavailable(f"declared file size {file_size} exceeds ceiling {self._max_file_size}")

        await self._download_location(location, dc_id, file_size, file)
        return file

    async def _download_location(self, location, dc_id, file_size: int, dest_path: str) -> None:
        offsets = list(range(0, file_size, self._part_size))
        queue: asyncio.Queue[int] = asyncio.Queue()
        for off in offsets:
            queue.put_nowait(off)

        written: list[tuple[int, int]] = []
        senders: list[MTProtoSender] = []
        # O_NOFOLLOW (where available) refuses to follow a pre-planted symlink at
        # dest_path, so a shared/world-writable media dir can't redirect our write.
        open_flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(dest_path, open_flags, 0o644)
        try:
            os.ftruncate(fd, file_size)
            n = min(self._connections, len(offsets)) or 1
            senders = await self._build_senders(dc_id, n)

            workers = [
                asyncio.create_task(self._worker(sender, location, file_size, queue, fd, written)) for sender in senders
            ]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                # Transactional cancel: stop every sibling before propagating so
                # no worker keeps issuing requests after the transfer has failed.
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise

            _verify_coverage(written, file_size)
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                # Closing the fd must never mask the original chunk failure.
                pass
            fd = -1
            try:
                os.remove(dest_path)
            except OSError:
                pass
            raise
        finally:
            if fd != -1:
                os.close(fd)
            await self._close_senders(senders)

    async def _worker(self, sender, location, file_size, queue, fd, written) -> None:
        while True:
            try:
                offset = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            request = functions.upload.GetFileRequest(location, offset=offset, limit=self._part_size)
            try:
                result = await self._client._call(sender, request)
            except FloodWaitError:
                # Must propagate unchanged so the caller's single flood budget
                # (call_with_flood_retry) governs it — never a second backoff.
                raise
            except FileReferenceExpiredError:
                # Propagate unchanged so the caller's file-reference refresh
                # loop can re-resolve the message and retry, rather than
                # silently degrading to single-stream with a stale reference.
                raise
            except FileMigrateError as exc:
                # The file actually lives on another DC; native download_media
                # handles migration, so bail out to single-stream for this file.
                raise ParallelDownloadUnavailable(f"file migrated to DC {exc.new_dc}") from exc
            # Any other RPCError (incl. LOCATION_NOT_AVAILABLE / LOCATION_INVALID
            # from upload.GetFile) intentionally propagates unchanged so the
            # caller's refresh + retry loop can handle it. Do NOT add a broad
            # `except Exception -> ParallelDownloadUnavailable` here: it would
            # mask those as a stale single-stream fallback (see is_media_location_error).
            data = result.bytes
            if not data:
                # Empty response before EOF would leave a gap; coverage check
                # would fail anyway, but fail fast with a clear cause.
                raise ParallelDownloadUnavailable(f"empty chunk at offset {offset}")
            expected = min(self._part_size, file_size - offset)
            if len(data) != expected:
                raise ParallelDownloadUnavailable(
                    f"chunk at offset {offset} returned {len(data)} bytes, expected {expected}"
                )
            _pwrite_all(fd, data, offset)
            written.append((offset, len(data)))

    async def _build_senders(self, dc_id, n: int) -> list[MTProtoSender]:
        """Create ``n`` connected senders to ``dc_id`` sharing one auth key.

        Home DC: reuse the session auth key directly (Telegram forbids
        exporting auth to the DC you are already on). Foreign DC: export auth
        from the main connection once, import it into the first sender, then
        reuse that sender's negotiated key for the remaining siblings.
        """
        client = self._client
        home_dc = client.session.dc_id
        senders: list[MTProtoSender] = []
        try:
            if dc_id is None or dc_id == home_dc:
                target_dc = home_dc
                auth_key = client.session.auth_key
                for _ in range(n):
                    senders.append(await self._connect_sender(target_dc, auth_key))
            else:
                first = await self._connect_sender(dc_id, None)
                senders.append(first)
                # Hold the borrow lock while mutating the shared init request,
                # mirroring Telethon's own _create_exported_sender. Restore the
                # original query afterwards so concurrent users of _init_request
                # never observe our transient ImportAuthorizationRequest.
                async with client._borrow_sender_lock:
                    saved_query = client._init_request.query
                    try:
                        auth = await client(ExportAuthorizationRequest(dc_id))
                        client._init_request.query = ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
                        await first.send(InvokeWithLayerRequest(LAYER, client._init_request))
                    finally:
                        client._init_request.query = saved_query
                auth_key = first.auth_key
                for _ in range(n - 1):
                    senders.append(await self._connect_sender(dc_id, auth_key))
            return senders
        except ParallelDownloadUnavailable:
            await self._close_senders(senders)
            raise
        except Exception as exc:  # noqa: BLE001
            await self._close_senders(senders)
            if isinstance(exc, (FloodWaitError, FileReferenceExpiredError)):
                # Surface flood (single budget) and stale-reference (refresh
                # loop) unchanged so the caller's existing handling governs
                # them, rather than masking them as a generic fallback.
                raise
            raise ParallelDownloadUnavailable(f"failed to establish parallel senders: {exc}") from exc

    async def _connect_sender(self, dc_id, auth_key) -> MTProtoSender:
        client = self._client
        dc = await client._get_dc(dc_id)
        sender = MTProtoSender(auth_key, loggers=client._log)
        await sender.connect(
            client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=client._log,
                proxy=client._proxy,
                local_addr=getattr(client, "_local_addr", None),
            )
        )
        return sender

    async def _close_senders(self, senders) -> None:
        for sender in senders:
            try:
                await sender.disconnect()
            except Exception:  # noqa: BLE001 — disconnect must never mask the real error
                pass


def _extract_file_size(message) -> int | None:
    """Best-effort exact byte size for a message's downloadable media."""
    media = getattr(message, "media", message)
    document = getattr(media, "document", None)
    if document is not None and getattr(document, "size", None):
        return int(document.size)
    photo = getattr(media, "photo", None)
    if photo is not None:
        sizes = getattr(photo, "sizes", None) or []
        best = 0
        for s in sizes:
            size = getattr(s, "size", None)
            if size:
                best = max(best, int(size))
            else:
                # PhotoSizeProgressive carries a list of cumulative sizes.
                sizes_list = getattr(s, "sizes", None)
                if sizes_list:
                    best = max(best, int(max(sizes_list)))
        return best or None
    size = getattr(media, "size", None)
    return int(size) if size else None
