"""check-media fills the size of a photo stored without one, from its file header.

Releases before 7.32.0 read ``photo.w`` from a Photo object, which has no such
attribute, so a photo from the full pass was stored with no width or height.
The viewer sizes a picture from them. ``check-media --repair`` reads the size
from the file's header, writes it only on a row with neither value, and never
touches the file. Rows run on SQLite and on PostgreSQL (``real_adapter``).
Demo data only.
"""

import io
import os
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy import select

from telegram_archive.__main__ import create_parser, run_check_media
from telegram_archive.db.models import Media
from telegram_archive.media_integrity import check_media, format_media_check, image_header_size

CHAT = -1001234500001


def _jpeg(path, size, *, orientation=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image = Image.new("RGB", size, (90, 160, 230))
    if orientation is None:
        image.save(path, "JPEG", quality=80)
        return
    exif = Image.Exif()
    exif[0x0112] = orientation
    image.save(path, "JPEG", quality=80, exif=exif.tobytes())


async def _seed(adapter, path, *, message_id, width=None, height=None, media_type="photo", chat_id=CHAT):
    await adapter.upsert_chat({"id": chat_id, "type": "channel", "title": "Channel A"}, account_id=1)
    await adapter.insert_message(
        {"id": message_id, "chat_id": chat_id, "text": "", "date": datetime(2026, 7, 1, 12), "raw_data": {}},
        account_id=1,
    )
    media_id = f"{chat_id}_{message_id}_{media_type}"
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "type": media_type,
            "file_name": os.path.basename(path),
            "file_path": path,
            "file_size": os.path.getsize(path),
            "width": width,
            "height": height,
            "downloaded": True,
            "download_date": datetime(2026, 7, 1, 12),
        },
        account_id=1,
    )
    return media_id


async def _size(adapter, media_id, chat_id=CHAT):
    async with adapter.db_manager.async_session_factory() as session:
        row = (
            await session.execute(
                select(Media.width, Media.height).where(
                    Media.account_id == 1, Media.chat_id == chat_id, Media.id == media_id
                )
            )
        ).one()
    return (row[0], row[1])


def _media_root(tmp_path):
    return os.path.join(str(tmp_path), "media")


class TestImageHeaderSize:
    def test_reads_the_size_without_decoding(self, tmp_path):
        path = str(tmp_path / "a.jpg")
        _jpeg(path, (720, 1280))
        assert image_header_size(path) == (720, 1280)

    def test_a_quarter_turn_swaps_width_and_height(self, tmp_path):
        for orientation, expected in ((6, (1280, 720)), (8, (1280, 720)), (3, (720, 1280)), (1, (720, 1280))):
            path = str(tmp_path / f"o{orientation}.jpg")
            _jpeg(path, (720, 1280), orientation=orientation)
            assert image_header_size(path) == expected, orientation

    def test_garbage_a_missing_file_and_no_path_give_none(self, tmp_path):
        garbage = tmp_path / "g.jpg"
        garbage.write_bytes(b"not a picture at all" * 8)
        assert image_header_size(str(garbage)) is None
        assert image_header_size(str(tmp_path / "gone.jpg")) is None
        assert image_header_size(None) is None

    def test_over_the_pixel_limit_gives_none(self, tmp_path):
        path = str(tmp_path / "big.jpg")
        _jpeg(path, (400, 300))
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100_000):
            # 120 000 pixels: over the limit, under twice it, where Pillow only warns.
            assert image_header_size(path) is None
        with patch.object(Image, "MAX_IMAGE_PIXELS", 50_000):
            # Over twice the limit, where Pillow raises.
            assert image_header_size(path) is None


class TestCheckMediaFillsPhotoSizes:
    async def test_a_dry_run_counts_and_writes_nothing(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000101.jpg")
        _jpeg(path, (900, 1200))
        media_id = await _seed(real_adapter, path, message_id=101)

        report = await check_media(real_adapter, _media_root(tmp_path))

        assert report["present"] == 1
        assert report["dimensions_missing"] == 1
        assert report["dimensions_filled"] == 0
        assert await _size(real_adapter, media_id) == (None, None)
        lines = format_media_check(report, repair=False)
        assert "  Photos without size:       1  (--repair reads it from the file header)" in lines

    async def test_repair_fills_the_size_and_leaves_the_file_alone(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000102.jpg")
        _jpeg(path, (900, 1200))
        before = (Path(path).read_bytes(), os.stat(path).st_mtime_ns)
        media_id = await _seed(real_adapter, path, message_id=102)

        report = await check_media(real_adapter, _media_root(tmp_path), repair=True)

        assert report["dimensions_filled"] == 1
        assert await _size(real_adapter, media_id) == (900, 1200)
        assert (Path(path).read_bytes(), os.stat(path).st_mtime_ns) == before
        lines = format_media_check(report, repair=True)
        assert "  Photo sizes filled:        1  (read from the file header)" in lines
        # A second run finds nothing left to fill.
        again = await check_media(real_adapter, _media_root(tmp_path), repair=True)
        assert again["dimensions_missing"] == 0
        assert again["dimensions_filled"] == 0

    async def test_a_stored_size_is_never_overwritten(self, real_adapter, tmp_path):
        """The header says 900x1200, the row 1280x853: the row keeps its value.

        Telegram's own size wins over a file. The adapter's IS NULL guard is
        tested directly too, so dropping it fails even when the caller skips
        the row first.
        """
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000103.jpg")
        _jpeg(path, (900, 1200))
        media_id = await _seed(real_adapter, path, message_id=103, width=1280, height=853)

        report = await check_media(real_adapter, _media_root(tmp_path), repair=True)

        assert report["dimensions_missing"] == 0
        assert report["dimensions_filled"] == 0
        assert await _size(real_adapter, media_id) == (1280, 853)
        assert not await real_adapter.fill_media_dimensions(media_id, account_id=1, width=900, height=1200)
        assert await _size(real_adapter, media_id) == (1280, 853)

    async def test_the_adapter_writes_only_photos_with_no_size(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000104.mp4")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * 64)
        video_id = await _seed(real_adapter, path, message_id=104, media_type="video")
        assert not await real_adapter.fill_media_dimensions(video_id, account_id=1, width=640, height=360)
        assert await _size(real_adapter, video_id) == (None, None)
        # Another account's row of the same id is not this one.
        jpg = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000105.jpg")
        _jpeg(jpg, (10, 20))
        photo_id = await _seed(real_adapter, jpg, message_id=105)
        assert not await real_adapter.fill_media_dimensions(photo_id, account_id=2, width=10, height=20)
        assert await real_adapter.fill_media_dimensions(photo_id, account_id=1, width=10, height=20)
        assert await _size(real_adapter, photo_id) == (10, 20)

    async def test_garbage_bytes_are_counted_and_left_alone(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000106.jpg")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"not a picture at all" * 8)
        media_id = await _seed(real_adapter, path, message_id=106)

        report = await check_media(real_adapter, _media_root(tmp_path), repair=True)

        assert report["dimensions_unreadable"] == 1
        assert report["dimensions_missing"] == 0
        assert await _size(real_adapter, media_id) == (None, None)
        assert "  Size not readable:         1  (left as it is)" in format_media_check(report, repair=True)

    async def test_an_exif_turned_photo_gets_width_and_height_swapped(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000107.jpg")
        _jpeg(path, (1280, 720), orientation=6)
        media_id = await _seed(real_adapter, path, message_id=107)

        await check_media(real_adapter, _media_root(tmp_path), repair=True)

        assert await _size(real_adapter, media_id) == (720, 1280)

    async def test_over_the_pixel_limit_is_skipped(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000108.jpg")
        _jpeg(path, (400, 300))
        media_id = await _seed(real_adapter, path, message_id=108)

        with patch.object(Image, "MAX_IMAGE_PIXELS", 50_000):
            report = await check_media(real_adapter, _media_root(tmp_path), repair=True)

        assert report["dimensions_unreadable"] == 1
        assert await _size(real_adapter, media_id) == (None, None)

    async def test_the_chat_filter_is_honoured(self, real_adapter, tmp_path):
        other = -1001234500002
        mine = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000109.jpg")
        theirs = os.path.join(_media_root(tmp_path), str(other), "5000000000000000110.jpg")
        _jpeg(mine, (300, 200))
        _jpeg(theirs, (300, 200))
        mine_id = await _seed(real_adapter, mine, message_id=109)
        theirs_id = await _seed(real_adapter, theirs, message_id=110, chat_id=other)

        report = await check_media(real_adapter, _media_root(tmp_path), repair=True, chat_id=CHAT)

        assert report["dimensions_filled"] == 1
        assert await _size(real_adapter, mine_id) == (300, 200)
        assert await _size(real_adapter, theirs_id, chat_id=other) == (None, None)

    async def test_a_failed_write_is_counted(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000111.jpg")
        _jpeg(path, (300, 200))
        await _seed(real_adapter, path, message_id=111)

        with patch.object(type(real_adapter), "fill_media_dimensions", side_effect=RuntimeError("boom")):
            report = await check_media(real_adapter, _media_root(tmp_path), repair=True)

        assert report["dimensions_fill_failed"] == 1
        assert "  Could not store a size:    1  (see the log)" in format_media_check(report, repair=True)


class TestTheExitCodeDoesNotChange:
    async def _run(self, real_adapter, tmp_path, *argv):
        args = create_parser().parse_args(["check-media", *argv])
        env = {"DATABASE_URL": real_adapter.db_manager.database_url, "BACKUP_PATH": str(tmp_path)}
        out = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out):
            code = await run_check_media(args)
        return code, out.getvalue()

    async def test_photos_without_size_exit_0_in_the_dry_run_and_the_repair(self, real_adapter, tmp_path):
        path = os.path.join(_media_root(tmp_path), str(CHAT), "5000000000000000112.jpg")
        _jpeg(path, (640, 480))
        media_id = await _seed(real_adapter, path, message_id=112)

        code, out = await self._run(real_adapter, tmp_path)
        assert code == 0
        assert "Photos without size:       1" in out
        assert str(CHAT) not in out and "5000000000000000112" not in out

        code, out = await self._run(real_adapter, tmp_path, "--repair")
        assert code == 0
        assert "Photo sizes filled:        1" in out
        assert await _size(real_adapter, media_id) == (640, 480)


@pytest.mark.parametrize("repair", [False, True])
def test_no_line_when_nothing_to_report(repair):
    report = dict.fromkeys(
        (
            "checked",
            "present",
            "broken_links",
            "missing_files",
            "restorable",
            "refetch",
            "restored",
            "kept",
            "restore_failed",
            "refetch_failed",
            "placeholders",
        ),
        0,
    )
    text = "\n".join(format_media_check(report, repair=repair))
    assert "size" not in text.lower()
