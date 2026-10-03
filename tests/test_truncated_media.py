"""A media file whose download stopped early is found, refused and replaced.

The production shape: a release from late 2025 stored videos whose download
stopped at a chunk boundary as complete. Telethon ends a download at the first
answer shorter than its request (an empty one included) and never compares the
total with the size Telegram declared. The file on disk is an exact multiple of
1 MiB, the media row says downloaded with ``file_size`` equal to that short
size, and the MP4 has no ``moov`` box, so nothing can play or decode it.

* ``check-media`` finds such a file (no index, a size a stopped download
  leaves) and ``--repair`` marks it for a new download, keeping its path;
* a marked file is never counted as back in place, which would mark it
  downloaded again before the new download arrives;
* the backup and the listener refuse a download shorter than the declared
  size, and download again an entry that is shorter, replacing it only after
  its bytes prove to be the start of the new ones;
* a transcription failure about the earlier bytes does not count toward the
  three failures that end the retries.

Tests that need a database run on SQLite and on PostgreSQL (``real_adapter``).
The media folder lives beside the SQLite file, never around it.
"""

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
)

from telegram_archive.__main__ import create_parser, run_check_media
from telegram_archive.db.models import Media
from telegram_archive.listener import TelegramListener
from telegram_archive.media_integrity import (
    SUSPICIOUS,
    TRUNCATED,
    check_media,
    cut_short_state,
    file_in_place,
    format_media_check,
    iso_bmff_incomplete,
)
from telegram_archive.message_utils import (
    ShortDownloadError,
    ShortFileMismatchError,
    check_complete_download,
    declared_document_size,
    download_and_shard_media,
    utcnow_naive,
)
from telegram_archive.parallel_download import ParallelDownloadUnavailable
from telegram_archive.telegram_backup import MEDIA_REFRESH_MAX_ATTEMPTS, TelegramBackup

MIB = 1024 * 1024
CHAT = -1001234567890
MESSAGE_ID = 7
FILE_ID = "5000000000000000001"
FILE = f"{FILE_ID}_clip.mp4"
MEDIA_ID = f"{CHAT}_{MESSAGE_ID}_video"


# --- MP4 bytes ---------------------------------------------------------------


def _box(kind: bytes, payload: bytes = b"", *, size: int | None = None) -> bytes:
    """One ISO BMFF box: a 32-bit size, a four-letter type, the payload."""
    return struct.pack(">I4s", 8 + len(payload) if size is None else size, kind) + payload


FTYP = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isommp41")
assert len(FTYP) == 24


def _filler(length: int, seed: int = 0) -> bytes:
    """Deterministic bytes that are not zeros, so a prefix check means something."""
    block = bytes((i * 31 + seed) % 251 for i in range(4096))
    return (block * (length // len(block) + 1))[:length]


def _complete_mp4(total: int, seed: int = 0) -> bytes:
    """ftyp, a media data box and the index at the end: the shape a phone records."""
    moov = _box(b"moov", _filler(1000, seed + 1))
    mdat_payload = total - len(FTYP) - 8 - len(moov)
    data = FTYP + _box(b"mdat", _filler(mdat_payload, seed)) + moov
    assert len(data) == total
    return data


FULL = _complete_mp4(3 * MIB)
CUT = FULL[:MIB]  # a download that stopped at the first 1 MiB block edge


def _write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _shared_layout(media: str, data: bytes, *, chats=(CHAT,), name: str = FILE) -> tuple[str, list[str]]:
    """The DEDUPLICATE_MEDIA layout: the blob in its _shared bucket, a link per chat."""
    blob = _write(os.path.join(media, "_shared", _sha(data)[:2], name), data)
    links = []
    for chat in chats:
        folder = os.path.join(media, str(chat))
        os.makedirs(folder, exist_ok=True)
        link = os.path.join(folder, name)
        os.symlink(os.path.relpath(blob, folder), link)
        links.append(link)
    return blob, links


# --- Telegram doubles ----------------------------------------------------------


def _document(size: int = len(FULL)) -> Document:
    return Document(
        id=int(FILE_ID),
        access_hash=1,
        file_reference=b"",
        date=datetime(2025, 12, 6, 12),
        mime_type="video/mp4",
        size=size,
        dc_id=2,
        attributes=[DocumentAttributeVideo(duration=5, w=320, h=240), DocumentAttributeFilename(file_name="clip.mp4")],
    )


def _message(size: int = len(FULL)) -> MagicMock:
    message = MagicMock(id=MESSAGE_ID, edit_date=None, edit_hide=False)
    message.media = MessageMediaDocument(document=_document(size))
    message.date = datetime(2025, 12, 6, 12)
    return message


def _writer(*payloads: bytes):
    """A ``client.download_media`` that writes the next payload to the path it is given."""
    remaining = list(payloads)

    async def download(_message, path):
        data = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        _write(path, data)
        return path

    return AsyncMock(side_effect=download)


def _backup(adapter, media: str, *, dedup: bool = True) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = MagicMock()
    backup.config.media_path = media
    backup.config.deduplicate_media = dedup
    backup.config.download_youtube_videos = True
    backup.config.verify_media = False
    backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * MIB)
    backup.config.max_media_download_attempts = 5
    backup.config.skip_media_chat_ids = set()
    backup.config.download_media_types = set()
    backup.config.download_document_mime_types = set()
    backup.config.download_document_mime_extensions = set()
    backup.config.parallel_download_enabled = False
    backup.db = adapter
    backup.client = AsyncMock()
    backup._parallel_downloader = None
    backup._parallel_download_disabled = False
    backup._get_media_filename = MagicMock(return_value=FILE)
    return backup


async def _seed(adapter, *, file_path: str, data: bytes, downloaded: bool = True) -> None:
    """The row as the late-2025 release wrote it: downloaded, sized and hashed from the short file."""
    await adapter.upsert_chat({"id": CHAT, "type": "channel", "title": "Channel A"}, account_id=1)
    await adapter.insert_message(
        {"id": MESSAGE_ID, "chat_id": CHAT, "text": "", "date": datetime(2025, 12, 6, 12), "raw_data": {}},
        account_id=1,
    )
    await adapter.insert_media(
        {
            "id": MEDIA_ID,
            "message_id": MESSAGE_ID,
            "chat_id": CHAT,
            "type": "video",
            "file_name": FILE,
            "file_path": file_path,
            "file_size": len(data),
            "mime_type": "video/mp4",
            "content_hash": _sha(data),
            "telegram_file_id": FILE_ID,
            "downloaded": downloaded,
            "download_date": datetime(2025, 12, 6, 12),
        },
        account_id=1,
    )


async def _row(adapter, media_id: str = MEDIA_ID) -> dict:
    async with adapter.db_manager.async_session_factory() as session:
        media = (await session.execute(select(Media).where(Media.account_id == 1, Media.id == media_id))).scalar_one()
        return {
            "downloaded": media.downloaded,
            "download_attempts": media.download_attempts,
            "file_path": media.file_path,
            "file_size": media.file_size,
            "content_hash": media.content_hash,
        }


async def _check_media_cli(adapter, backup_path, *argv) -> tuple[int, str]:
    args = create_parser().parse_args(["check-media", *argv])
    env = {"DATABASE_URL": adapter.db_manager.database_url, "BACKUP_PATH": str(backup_path)}
    out = io.StringIO()
    with patch.dict(os.environ, env), redirect_stdout(out):
        code = await run_check_media(args)
    return code, out.getvalue()


# --- (e) the box walk -------------------------------------------------------------


class TestBoxWalk:
    def test_the_cut_file_is_incomplete_and_the_whole_file_is_not(self, tmp_path):
        assert iso_bmff_incomplete(_write(str(tmp_path / "cut.mp4"), CUT)) is True
        assert iso_bmff_incomplete(_write(str(tmp_path / "full.mp4"), FULL)) is False

    def test_a_largesize_box_is_read_as_64_bits(self, tmp_path):
        moov = _box(b"moov", b"x" * 10)
        payload = b"y" * 100
        largesize = struct.pack(">I4sQ", 1, b"mdat", 16 + len(payload)) + payload
        assert iso_bmff_incomplete(_write(str(tmp_path / "a.mp4"), FTYP + largesize + moov)) is False
        # The same box, cut: its 64-bit size runs past the end.
        assert iso_bmff_incomplete(_write(str(tmp_path / "b.mp4"), FTYP + largesize[:-10])) is True

    def test_a_box_of_size_zero_runs_to_the_end_and_the_moov_rule_still_applies(self, tmp_path):
        moov = _box(b"moov", b"x" * 10)
        to_end = _box(b"mdat", b"z" * 500, size=0)
        assert iso_bmff_incomplete(_write(str(tmp_path / "a.mp4"), FTYP + moov + to_end)) is False
        assert iso_bmff_incomplete(_write(str(tmp_path / "b.mp4"), FTYP + to_end)) is True

    def test_a_faststart_file_with_the_index_first_and_its_data_cut_is_incomplete(self, tmp_path):
        moov = _box(b"moov", b"x" * 10)
        mdat = _box(b"mdat", b"d" * 50, size=10 * MIB)
        assert iso_bmff_incomplete(_write(str(tmp_path / "a.mp4"), FTYP + moov + mdat)) is True

    def test_a_file_that_ends_inside_a_box_header_is_incomplete(self, tmp_path):
        moov = _box(b"moov", b"x" * 10)
        assert iso_bmff_incomplete(_write(str(tmp_path / "a.mp4"), FTYP + moov + b"\x00\x00\x01")) is True

    def test_what_is_not_an_iso_file_is_not_judged(self, tmp_path):
        webm = b"\x1a\x45\xdf\xa3" + _filler(MIB - 4)
        assert iso_bmff_incomplete(_write(str(tmp_path / "w.mp4"), webm)) is None
        assert iso_bmff_incomplete(_write(str(tmp_path / "e.mp4"), b"")) is None
        assert iso_bmff_incomplete(str(tmp_path / "absent.mp4")) is None
        # A box smaller than its own header is malformed, not cut.
        assert iso_bmff_incomplete(_write(str(tmp_path / "m.mp4"), FTYP + struct.pack(">I4s", 4, b"free"))) is None


class TestCutShortState:
    def test_the_size_grain_tells_a_stopped_download_from_other_damage(self, tmp_path):
        assert cut_short_state(_write(str(tmp_path / "a.mp4"), CUT)) == TRUNCATED
        assert cut_short_state(_write(str(tmp_path / "b.mp4"), FULL[: MIB + 100])) == SUSPICIOUS
        assert cut_short_state(_write(str(tmp_path / "c.mp4"), FULL[: 3 * 128 * 1024])) == TRUNCATED

    def test_the_extension_decides_which_files_are_walked(self, tmp_path):
        for ext in (".mp4", ".M4V", ".m4a", ".mov", ".3gp"):
            assert cut_short_state(_write(str(tmp_path / f"x{ext}"), CUT)) == TRUNCATED, ext
        assert cut_short_state(_write(str(tmp_path / "x.mkv"), CUT)) is None
        assert cut_short_state(_write(str(tmp_path / "x.bin"), CUT)) is None

    def test_a_complete_file_whose_size_is_a_multiple_of_the_grain_is_not_cut(self, tmp_path):
        """(b) The control: ftyp, moov and mdat ending exactly at the end of 1 MiB."""
        moov = _box(b"moov", b"x" * 100)
        data = FTYP + moov + _box(b"mdat", _filler(MIB - len(FTYP) - len(moov) - 8))
        assert len(data) == MIB
        assert cut_short_state(_write(str(tmp_path / "ok.mp4"), data)) is None


# --- (a)-(d), (f) check-media ---------------------------------------------------------


class TestCheckMediaFindsCutShortFiles:
    async def test_a_dry_run_counts_it_changes_nothing_and_exits_1_then_repair_marks_it(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)
        await _seed(real_adapter, file_path=link, data=CUT)
        before = await _row(real_adapter)

        code, out = await _check_media_cli(real_adapter, tmp_path)
        assert code == 1
        assert "Cut short:                 1" in out
        assert "Files in place:            0" in out
        assert await _row(real_adapter) == before

        code, out = await _check_media_cli(real_adapter, tmp_path, "--repair")
        assert code == 0
        assert "Cut short:                 1" in out
        assert "Marked to download again:  1" in out
        row = await _row(real_adapter)
        assert (row["downloaded"], row["download_attempts"], row["file_path"]) == (0, 0, link)
        assert _read(blob) == CUT  # the bytes stay until the new download replaces them

    async def test_a_complete_file_is_in_place(self, real_adapter, tmp_path):
        """(b)"""
        media = str(tmp_path / "media")
        moov = _box(b"moov", b"x" * 100)
        data = FTYP + moov + _box(b"mdat", _filler(MIB - len(FTYP) - len(moov) - 8))
        _blob, (link,) = _shared_layout(media, data)
        await _seed(real_adapter, file_path=link, data=data)

        report = await check_media(real_adapter, media, repair=True)

        assert (report["present"], report["truncated"], report["suspicious"], report["refetch"]) == (1, 0, 0, 0)
        assert (await _row(real_adapter))["downloaded"] == 1

    async def test_no_index_at_another_size_is_counted_and_never_marked(self, real_adapter, tmp_path):
        """(c)"""
        media = str(tmp_path / "media")
        data = FULL[: MIB + 100]
        _blob, (link,) = _shared_layout(media, data)
        await _seed(real_adapter, file_path=link, data=data)

        code, out = await _check_media_cli(real_adapter, tmp_path)
        assert code == 0
        assert "Possibly damaged:          1" in out
        code, _out = await _check_media_cli(real_adapter, tmp_path, "--repair")
        assert code == 0
        assert (await _row(real_adapter))["downloaded"] == 1

    async def test_webm_bytes_under_an_mp4_name_are_not_judged(self, real_adapter, tmp_path):
        """(d)"""
        media = str(tmp_path / "media")
        data = b"\x1a\x45\xdf\xa3" + _filler(MIB - 4)
        _blob, (link,) = _shared_layout(media, data)
        await _seed(real_adapter, file_path=link, data=data)

        report = await check_media(real_adapter, media, repair=True)

        assert (report["present"], report["truncated"], report["suspicious"]) == (1, 0, 0)
        assert (await _row(real_adapter))["downloaded"] == 1

    async def test_a_marked_file_still_at_its_path_is_not_marked_downloaded_again(self, real_adapter, tmp_path):
        """(f) Its hash and size match the row, which was written from the short file."""
        media = str(tmp_path / "media")
        _blob, (link,) = _shared_layout(media, CUT)
        await _seed(real_adapter, file_path=link, data=CUT)
        await check_media(real_adapter, media, repair=True)

        again = await check_media(real_adapter, media, repair=True)

        assert (again["recoverable"], again.get("recovered", 0)) == (0, 0)
        assert (await _row(real_adapter))["downloaded"] == 0
        assert not file_in_place(await _row(real_adapter) | {"type": "video"}, media)

    def test_the_report_lines(self):
        report = dict.fromkeys(
            ("checked", "present", "broken_links", "missing_files", "restorable", "refetch", "restored", "kept"), 0
        )
        report.update(restore_failed=0, refetch_failed=0, placeholders=0, truncated=2, suspicious=1)
        lines = "\n".join(format_media_check(report, repair=False))
        assert "Cut short:                 2  (a video or audio file whose download stopped early;" in lines
        assert "Possibly damaged:          1  (no index, but the size does not match" in lines


# --- (g) the backup refuses a short download -------------------------------------------


class TestTheBackupRefusesAShortDownload:
    async def _seed_message(self, adapter):
        await adapter.upsert_chat({"id": CHAT, "type": "channel", "title": "Channel A"}, account_id=1)
        await adapter.insert_message(
            {"id": MESSAGE_ID, "chat_id": CHAT, "text": "", "date": datetime(2025, 12, 6, 12), "raw_data": {}},
            account_id=1,
        )

    async def test_a_short_download_is_tried_again_and_recorded_not_downloaded(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        await self._seed_message(real_adapter)
        backup = _backup(real_adapter, media)
        backup.client.download_media = _writer(CUT)

        result = await backup._process_media(_message(), CHAT)

        assert not result.get("downloaded")
        assert backup.client.download_media.await_count == MEDIA_REFRESH_MAX_ATTEMPTS
        await real_adapter.insert_media(result, account_id=1)
        assert (await _row(real_adapter))["downloaded"] == 0
        shared = os.path.join(media, "_shared")
        assert [f for _root, _dirs, files in os.walk(shared) for f in files] == []
        assert not os.path.lexists(os.path.join(media, str(CHAT), FILE))

    async def test_control_a_full_download_is_stored(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        await self._seed_message(real_adapter)
        backup = _backup(real_adapter, media)
        backup.client.download_media = _writer(FULL)

        result = await backup._process_media(_message(), CHAT)

        assert result["downloaded"] is True
        assert result["file_size"] == len(FULL)
        assert _read(os.path.join(media, str(CHAT), FILE)) == FULL

    async def test_a_second_attempt_that_completes_is_stored(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        await self._seed_message(real_adapter)
        backup = _backup(real_adapter, media)
        backup.client.download_media = _writer(CUT, FULL)

        result = await backup._process_media(_message(), CHAT)

        assert result["downloaded"] is True
        assert backup.client.download_media.await_count == 2
        assert _read(os.path.join(media, str(CHAT), FILE)) == FULL

    async def test_a_parallel_failure_that_falls_back_to_a_short_single_stream_is_refused(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        await self._seed_message(real_adapter)
        backup = _backup(real_adapter, media)
        backup._should_parallelize = MagicMock(return_value=True)
        backup._parallel_downloader = MagicMock()
        backup._parallel_downloader.download_media = AsyncMock(
            side_effect=ParallelDownloadUnavailable("a chunk came back short")
        )
        backup.client.download_media = _writer(CUT)

        result = await backup._process_media(_message(), CHAT)

        assert not result.get("downloaded")
        assert backup._parallel_downloader.download_media.await_count == MEDIA_REFRESH_MAX_ATTEMPTS

    async def test_without_dedup_a_short_download_is_refused_too(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        await self._seed_message(real_adapter)
        backup = _backup(real_adapter, media, dedup=False)
        backup.client.download_media = _writer(CUT)

        result = await backup._process_media(_message(), CHAT)

        assert not result.get("downloaded")
        assert os.listdir(os.path.join(media, str(CHAT))) == []


class TestDeclaredSize:
    def test_only_a_real_document_size_counts(self):
        assert declared_document_size(_message(1234)) == 1234
        assert declared_document_size(MagicMock()) is None  # a test double stays inert
        assert declared_document_size(MagicMock(media=None)) is None

    def test_check_complete_download(self, tmp_path):
        path = _write(str(tmp_path / "f.mp4"), CUT)
        with pytest.raises(ShortDownloadError) as caught:
            check_complete_download(path, len(FULL))
        assert str(tmp_path) not in str(caught.value)  # sizes only, no path
        check_complete_download(path, len(CUT))
        check_complete_download(path, None)
        check_complete_download(str(tmp_path / "absent"), len(FULL))


# --- (h) the shared store replaces a short blob ---------------------------------------


def _dedup_db():
    db = AsyncMock()
    db.find_media_by_content_hash = AsyncMock(return_value=None)
    return db


async def _shard(media: str, link: str, download, *, declared_size):
    return await download_and_shard_media(
        db=_dedup_db(),
        download_coro=download,
        shared_dir=os.path.join(media, "_shared"),
        chat_media_dir=os.path.dirname(link),
        file_name=FILE,
        file_path=link,
        logger=MagicMock(),
        account_id=1,
        declared_size=declared_size,
    )


def _download_of(data: bytes):
    async def download(tmp_path):
        _write(tmp_path, data)
        return tmp_path

    return AsyncMock(side_effect=download)


def _part_files(media: str) -> list[str]:
    return [f for _root, _dirs, files in os.walk(media) for f in files if f.endswith(".part")]


class TestTheSharedStoreReplacesAShortBlob:
    async def test_the_blob_is_replaced_and_every_link_reads_the_complete_bytes(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (link_a, link_b) = _shared_layout(media, CUT, chats=(CHAT, -1009999999999))
        targets = (os.readlink(link_a), os.readlink(link_b))
        download = _download_of(FULL)

        path, digest = await _shard(media, link_a, download, declared_size=len(FULL))

        download.assert_awaited_once()
        assert digest == _sha(FULL)
        assert _read(path) == FULL
        assert _read(blob) == FULL
        assert (_read(link_a), _read(link_b)) == (FULL, FULL)
        assert (os.readlink(link_a), os.readlink(link_b)) == targets  # no link rewritten
        assert _part_files(media) == []

    async def test_a_blob_found_by_name_with_no_chat_link_yet_is_replaced(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (other,) = _shared_layout(media, CUT, chats=(-1009999999999,))
        link = os.path.join(media, str(CHAT), FILE)
        os.makedirs(os.path.dirname(link))

        await _shard(media, link, _download_of(FULL), declared_size=len(FULL))

        assert (_read(blob), _read(other), _read(link)) == (FULL, FULL, FULL)

    async def test_new_bytes_that_do_not_start_with_the_old_ones_leave_the_old_blob_untouched(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)
        other = _complete_mp4(len(FULL), seed=7)

        with pytest.raises(ShortFileMismatchError):
            await _shard(media, link, _download_of(other), declared_size=len(FULL))

        assert _read(blob) == CUT
        assert _read(link) == CUT
        assert _part_files(media) == []
        assert not os.path.exists(os.path.join(media, "_shared", _sha(other)[:2], FILE))

    async def test_a_new_download_that_is_short_too_publishes_nothing(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)

        with pytest.raises(ShortDownloadError):
            await _shard(media, link, _download_of(FULL[: 2 * MIB]), declared_size=len(FULL))

        assert _read(blob) == CUT
        assert _part_files(media) == []

    async def test_without_a_declared_size_the_entry_is_reused_as_before(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)
        download = _download_of(FULL)

        path, digest = await _shard(media, link, download, declared_size=None)

        download.assert_not_awaited()
        assert (path, digest) == (blob, _sha(CUT))
        assert _read(blob) == CUT

    async def test_a_complete_entry_is_reused(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, FULL)
        download = _download_of(FULL)

        assert await _shard(media, link, download, declared_size=len(FULL)) == (blob, _sha(FULL))
        download.assert_not_awaited()

    async def test_a_link_out_of_the_archive_is_never_judged(self, tmp_path):
        """git-annex (#143): an entry this archive did not make is left alone."""
        media = str(tmp_path / "media")
        outside = _write(str(tmp_path / "annex" / "object"), CUT)
        link = os.path.join(media, str(CHAT), FILE)
        os.makedirs(os.path.dirname(link))
        os.makedirs(os.path.join(media, "_shared"))
        os.symlink(outside, link)
        download = _download_of(FULL)

        await _shard(media, link, download, declared_size=len(FULL))

        download.assert_not_awaited()
        assert _read(outside) == CUT


# --- (i) the backup without dedup replaces a short file ------------------------------------


class TestTheBackupWithoutDedupReplacesAShortFile:
    async def test_a_short_file_at_the_rows_path_is_downloaded_again_and_replaced(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _write(os.path.join(media, str(CHAT), FILE), CUT)
        await _seed(real_adapter, file_path=path, data=CUT, downloaded=False)
        backup = _backup(real_adapter, media, dedup=False)
        backup.client.download_media = _writer(FULL)

        result = await backup._process_media(_message(), CHAT)

        assert result["downloaded"] is True
        assert (result["file_size"], result["content_hash"]) == (len(FULL), _sha(FULL))
        assert _read(path) == FULL
        assert _part_files(media) == []

    async def test_a_link_left_from_a_dedup_period_has_its_target_replaced(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)
        await _seed(real_adapter, file_path=link, data=CUT, downloaded=False)
        backup = _backup(real_adapter, media, dedup=False)
        backup.client.download_media = _writer(FULL)

        result = await backup._process_media(_message(), CHAT)

        assert result["downloaded"] is True
        assert os.path.islink(link)
        assert (_read(blob), _read(link)) == (FULL, FULL)

    async def test_a_mismatch_keeps_the_old_file_and_records_a_retry(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _write(os.path.join(media, str(CHAT), FILE), CUT)
        await _seed(real_adapter, file_path=path, data=CUT, downloaded=False)
        backup = _backup(real_adapter, media, dedup=False)
        backup.client.download_media = _writer(_complete_mp4(len(FULL), seed=7))

        result = await backup._process_media(_message(), CHAT)

        assert not result.get("downloaded")
        assert _read(path) == CUT
        assert _part_files(media) == []


# --- (j) the listener ---------------------------------------------------------------


def _listener(media: str, *, dedup: bool = True) -> TelegramListener:
    config = MagicMock()
    config.media_path = media
    config.deduplicate_media = dedup
    config.get_max_media_size_bytes = MagicMock(return_value=100 * MIB)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    db = AsyncMock()
    db.find_media_by_content_hash = AsyncMock(return_value=None)
    listener = TelegramListener(config, db, account_id=1)
    listener.client = AsyncMock()
    listener._get_media_filename = MagicMock(return_value=FILE)
    return listener


class TestTheListener:
    async def test_a_short_download_is_refused(self, tmp_path):
        media = str(tmp_path / "media")
        listener = _listener(media)
        listener.client.download_media = _writer(CUT)

        assert await listener._download_media(_message(), CHAT) is None
        assert not os.path.lexists(os.path.join(media, str(CHAT), FILE))
        assert _part_files(media) == []

    async def test_control_a_full_download_is_kept(self, tmp_path):
        media = str(tmp_path / "media")
        listener = _listener(media)
        listener.client.download_media = _writer(FULL)

        result = await listener._download_media(_message(), CHAT)

        assert result is not None and result[2] == _sha(FULL)
        assert _read(os.path.join(media, str(CHAT), FILE)) == FULL

    async def test_an_existing_short_blob_is_downloaded_again(self, tmp_path):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)
        listener = _listener(media)
        listener.client.download_media = _writer(FULL)

        result = await listener._download_media(_message(), CHAT)

        assert result is not None and result[2] == _sha(FULL)
        assert (_read(blob), _read(link)) == (FULL, FULL)

    async def test_without_dedup_a_short_download_is_refused(self, tmp_path):
        media = str(tmp_path / "media")
        listener = _listener(media, dedup=False)
        listener.client.download_media = _writer(CUT)

        assert await listener._download_media(_message(), CHAT) is None
        assert os.listdir(os.path.join(media, str(CHAT))) == []

    async def test_without_dedup_an_existing_short_file_is_replaced(self, tmp_path):
        media = str(tmp_path / "media")
        path = _write(os.path.join(media, str(CHAT), FILE), CUT)
        listener = _listener(media, dedup=False)
        listener.client.download_media = _writer(FULL)

        result = await listener._download_media(_message(), CHAT)

        assert result is not None and result[2] == _sha(FULL)
        assert _read(path) == FULL
        assert _part_files(media) == []


# --- (k) end to end ------------------------------------------------------------------


class TestEndToEnd:
    async def test_check_media_marks_it_the_pending_drain_replaces_it_and_the_next_check_is_clean(
        self, real_adapter, tmp_path
    ):
        media = str(tmp_path / "media")
        blob, (link,) = _shared_layout(media, CUT)
        await _seed(real_adapter, file_path=link, data=CUT)

        code, _out = await _check_media_cli(real_adapter, tmp_path, "--repair")
        assert code == 0
        assert (await _row(real_adapter))["downloaded"] == 0

        backup = _backup(real_adapter, media)
        message = _message()
        backup.client.get_messages = AsyncMock(return_value=[message])
        backup.client.download_media = _writer(FULL)
        await backup._retry_pending_media_downloads()

        row = await _row(real_adapter)
        assert row["downloaded"] == 1
        assert (row["file_size"], row["content_hash"], row["file_path"]) == (len(FULL), _sha(FULL), link)
        assert _read(link) == FULL
        assert _read(blob) == FULL

        code, out = await _check_media_cli(real_adapter, tmp_path)
        assert code == 0
        assert "Cut short" not in out
        assert "Files in place:            1" in out


# --- (l) the transcription drain ------------------------------------------------------------


TYPES = ("video", "voice")


async def _drain(adapter) -> list[str]:
    rows = await adapter.get_media_awaiting_transcription(
        account_id=1, types=TYPES, per_run=50, stale_before=utcnow_naive() - timedelta(minutes=10)
    )
    return [row["id"] for row in rows]


async def _fail_three_times(adapter, content_hash: str | None) -> None:
    for _ in range(3):
        row = await adapter.enqueue_media_transcript(MEDIA_ID, account_id=1, content_hash=content_hash)
        await adapter.fill_media_transcript(row["id"], status="failed", error="decode_failed")


class TestTheDrainForgetsFailuresAboutEarlierBytes:
    async def test_failures_about_the_short_file_do_not_count_after_a_new_download(self, real_adapter, tmp_path):
        await _seed(real_adapter, file_path=f"{CHAT}/{FILE}", data=CUT)
        await _fail_three_times(real_adapter, _sha(CUT))
        assert await _drain(real_adapter) == []

        # The new download changes the row's hash.
        await real_adapter.insert_media(
            {"id": MEDIA_ID, "message_id": MESSAGE_ID, "chat_id": CHAT, "type": "video", "content_hash": _sha(FULL)},
            account_id=1,
        )
        assert (await _row(real_adapter))["content_hash"] == _sha(FULL)
        assert await _drain(real_adapter) == [MEDIA_ID]

    async def test_control_failures_about_the_same_bytes_still_end_the_retries(self, real_adapter):
        await _seed(real_adapter, file_path=f"{CHAT}/{FILE}", data=FULL)
        await _fail_three_times(real_adapter, _sha(FULL))
        assert await _drain(real_adapter) == []

    async def test_a_failure_with_no_hash_still_counts(self, real_adapter):
        await _seed(real_adapter, file_path=f"{CHAT}/{FILE}", data=FULL)
        await _fail_three_times(real_adapter, None)
        assert await _drain(real_adapter) == []


# --- the viewer words a marked file as not downloaded yet ----------------------------


INDEX_HTML = Path(__file__).resolve().parents[1] / "telegram_archive" / "web" / "templates" / "index.html"


class TestTheMessagePayloadSaysWhetherTheFileIsDownloaded:
    async def test_a_marked_row_reads_not_downloaded_and_a_kept_one_downloaded(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        _blob, (link,) = _shared_layout(media, CUT)
        await _seed(real_adapter, file_path=link, data=CUT)

        def media_of(messages):
            return [m["media"] for m in messages if m["id"] == MESSAGE_ID][0]

        page = await real_adapter.get_messages_paginated(CHAT, limit=10, account_id=1)
        assert media_of(page)["downloaded"] is True
        await check_media(real_adapter, media, repair=True)
        page = await real_adapter.get_messages_paginated(CHAT, limit=10, account_id=1)
        assert media_of(page)["downloaded"] is False
        assert media_of(page)["file_path"] == link  # the path stays, so the bytes can still be served

        found = await real_adapter.find_message_by_date_with_joins(CHAT, datetime(2025, 12, 6, 12), account_id=1)
        assert found["media"]["downloaded"] is False
        await real_adapter.update_message_pinned(CHAT, MESSAGE_ID, True, account_id=1)
        pinned = await real_adapter.get_pinned_messages(CHAT, account_id=1)
        assert media_of(pinned)["downloaded"] is False


def _matching_brace(source: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced")


def _function_source(name: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index(f"const {name} = (msg) => {{")
    return html[start : _matching_brace(html, html.index("{", start)) + 1]


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")
    # The script is built here from the shipped template; no input reaches a shell.
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestThePlaceholderReason:
    def test_a_failed_load_of_a_row_marked_for_a_new_download_is_not_downloaded_yet(self):
        cases = {
            "marked": {"file_path": "a.mp4", "downloaded": False},
            "kept": {"file_path": "a.mp4", "downloaded": True},
            "older_payload": {"file_path": "a.mp4"},
            "no_path": {"downloaded": False},
            "oversize": {"downloaded": False, "skip_reason": "oversize"},
        }
        script = f"""
{_function_source("mediaMissingReason")}
const cases = {json.dumps(cases)}
const out = {{}}
for (const [name, media] of Object.entries(cases)) {{
    out[name] = mediaMissingReason({{ media, mediaLoadFailed: !!media.file_path }})
}}
console.log(JSON.stringify(out))
"""
        assert json.loads(_run_node(script)) == {
            "marked": "pending",
            "kept": "missing",
            "older_payload": "missing",
            "no_path": "pending",
            "oversize": "oversize",
        }
