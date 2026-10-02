"""A media folder that is not there never marks a file not downloaded.

When the media volume is not mounted, or a network share dropped, every stored
path reads as missing. The transcription drain, ``check-media --repair`` and
``VERIFY_MEDIA`` used to mark each such file not downloaded; the downloads then
failed until they gave up, and once the volume was back the files stayed
hidden. A file is marked only when it is provably gone: the media folder is
there and not empty, and the row's folder exists under it.

Each case runs on SQLite and on PostgreSQL (``real_adapter``). The media folder
lives in its own directory beside the database, so the SQLite file never makes
it look non-empty.
"""

import hashlib
import io
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_archive.__main__ import create_parser, run_check_media
from telegram_archive.media_integrity import (
    MISSING,
    NOT_VISIBLE,
    REFETCH,
    check_media,
    format_media_check,
    repair_media_row,
)
from telegram_archive.telegram_backup import TelegramBackup
from telegram_archive.transcription import drain_transcriptions

sys.path.insert(0, os.path.dirname(__file__))

from test_transcription import FakeServer, _client, _config, _rows  # noqa: E402

CHAT = -1004400550066
FILE_ID = "5100000000000000001"
FILE = f"{FILE_ID}.ogg"
BYTES = b"archived voice bytes " * 16
SHA256 = hashlib.sha256(BYTES).hexdigest()

# How the media folder looks when the check runs. Only "present" lets a row be
# marked: the folder is there, holds something, and holds the row's folder.
ROOT_STATES = ("missing", "empty", "parent_missing")


def _lay_out(media: str, state: str) -> str:
    """Build the media folder in ``state``; return the row's (absent) file path."""
    path = os.path.join(media, str(CHAT), FILE)
    if state == "missing":
        return path
    os.makedirs(media)
    if state == "empty":
        return path
    # Something else lives in the folder, so it is visibly there.
    os.makedirs(os.path.join(media, "avatars"))
    if state == "present":
        os.makedirs(os.path.dirname(path))
    return path


async def _seed(adapter, file_path: str, *, downloaded: bool = True, media_type: str = "voice", **extra) -> dict:
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Demo group"}, account_id=1)
    await adapter.insert_message(
        {"id": 7, "chat_id": CHAT, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}}, account_id=1
    )
    row = {
        "id": f"{CHAT}_7_{media_type}",
        "message_id": 7,
        "chat_id": CHAT,
        "type": media_type,
        "file_name": FILE,
        "file_path": file_path,
        "file_size": len(BYTES),
        "duration": 12,
        "downloaded": downloaded,
        "download_date": datetime(2026, 1, 2, 3, 4, 5),
        **extra,
    }
    await adapter.insert_media(row, account_id=1)
    return row


async def _stored(adapter) -> tuple:
    [row] = await adapter.get_media_for_chat(CHAT, account_id=1)
    return row["downloaded"], row["file_path"]


class TestRepairMediaRow:
    @pytest.mark.parametrize("state", ROOT_STATES)
    async def test_a_file_not_provably_gone_is_never_marked(self, real_adapter, tmp_path, state):
        media = str(tmp_path / "media")
        path = _lay_out(media, state)
        row = await _seed(real_adapter, path)

        outcome = await repair_media_row(real_adapter, row, media, account_id=1)

        assert outcome == (MISSING if state == "parent_missing" else NOT_VISIBLE)
        assert await _stored(real_adapter) == (1, path)

    async def test_control_a_file_gone_from_a_visible_folder_is_marked(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "present")
        row = await _seed(real_adapter, path)

        assert await repair_media_row(real_adapter, row, media, account_id=1) == REFETCH
        assert await _stored(real_adapter) == (0, path)


class TestCheckMedia:
    @pytest.mark.parametrize("state", ("missing", "empty"))
    async def test_a_folder_not_visible_is_reported_and_nothing_changes(self, real_adapter, tmp_path, state):
        media = str(tmp_path / "media")
        path = _lay_out(media, state)
        await _seed(real_adapter, path)

        report = await check_media(real_adapter, media, repair=True)

        assert report["media_root_not_visible"] == 1
        assert (report["checked"], report["refetch"], report["missing_files"]) == (0, 0, 0)
        assert await _stored(real_adapter) == (1, path)
        assert format_media_check(report, repair=True)[0].startswith(
            "Media check: the media folder is not visible here"
        )

    async def test_a_row_whose_folder_is_gone_is_counted_and_never_marked(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "parent_missing")
        await _seed(real_adapter, path)

        report = await check_media(real_adapter, media, repair=True)

        assert (report["missing_files"], report["not_provable"], report["refetch"]) == (1, 1, 0)
        assert await _stored(real_adapter) == (1, path)
        assert "Not marked:                1" in "\n".join(format_media_check(report, repair=True))

    async def test_control_a_visible_folder_still_marks_a_missing_file(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "present")
        await _seed(real_adapter, path)

        report = await check_media(real_adapter, media, repair=True)

        assert (report["media_root_not_visible"], report["not_provable"], report["refetch"]) == (0, 0, 1)
        assert await _stored(real_adapter) == (0, path)


class TestCheckMediaCommand:
    async def _run(self, adapter, data_dir, *argv):
        args = create_parser().parse_args(["check-media", *argv])
        env = {"DATABASE_URL": adapter.db_manager.database_url, "BACKUP_PATH": str(data_dir)}
        out = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out):
            code = await run_check_media(args)
        return code, out.getvalue()

    @pytest.mark.parametrize("repair", (False, True))
    async def test_it_says_the_folder_is_not_visible_and_exits_1(self, real_adapter, tmp_path, repair):
        data_dir = tmp_path / "backups"
        path = _lay_out(str(data_dir / "media"), "missing")
        await _seed(real_adapter, path)

        code, out = await self._run(real_adapter, data_dir, *(["--repair"] if repair else []))

        assert code == 1
        assert "the media folder is not visible here" in out
        assert await _stored(real_adapter) == (1, path)


class TestTranscriptionDrain:
    async def _drain(self, adapter, media):
        config = _config(media)
        return await drain_transcriptions(
            config, adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )

    @pytest.mark.parametrize("state", ROOT_STATES)
    async def test_a_file_not_provably_gone_stays_downloaded_and_fails_as_missing(self, real_adapter, tmp_path, state):
        """As in 8.18: the drain records file_missing, which never counts toward
        the three, and the media row keeps its flag and its path."""
        media = str(tmp_path / "media")
        path = _lay_out(media, state)
        row = await _seed(real_adapter, path)

        stats = await self._drain(real_adapter, media)

        assert (stats["refetch"], stats["failed"]) == (0, 1)
        assert await _stored(real_adapter) == (1, path)
        assert (await _rows(real_adapter, row["id"]))[0]["error"] == "file_missing"

    async def test_control_a_file_gone_from_a_visible_folder_goes_back_to_download(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "present")
        row = await _seed(real_adapter, path)

        stats = await self._drain(real_adapter, media)

        assert stats["refetch"] == 1
        assert await _stored(real_adapter) == (0, path)
        assert await _rows(real_adapter, row["id"]) == []


def _verify_backup(adapter, media: str) -> TelegramBackup:
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    backup.config = MagicMock()
    backup.config.media_path = media
    backup.config.skip_media_chat_ids = set()
    backup.config.deduplicate_media = True
    backup.db = adapter
    backup.client = AsyncMock()
    backup.client.get_messages = AsyncMock(return_value=[MagicMock(id=7, media=MagicMock())])
    backup._keep_replaced_media = AsyncMock(return_value=False)
    backup._process_media = AsyncMock(return_value=None)  # the download fails
    return backup


class TestVerifyMedia:
    @pytest.mark.parametrize("state", ("missing", "empty"))
    async def test_a_folder_not_visible_skips_the_run(self, real_adapter, tmp_path, state):
        media = str(tmp_path / "media")
        path = _lay_out(media, state)
        await _seed(real_adapter, path)
        backup = _verify_backup(real_adapter, media)

        await backup._verify_and_redownload_media()

        backup.client.get_messages.assert_not_awaited()
        backup._process_media.assert_not_awaited()
        assert await _stored(real_adapter) == (1, path)

    async def test_a_failed_download_of_a_file_whose_folder_is_gone_keeps_the_row(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "parent_missing")
        await _seed(real_adapter, path)
        backup = _verify_backup(real_adapter, media)

        await backup._verify_and_redownload_media()

        backup._process_media.assert_awaited_once()
        assert await _stored(real_adapter) == (1, path)

    async def test_control_a_failed_download_of_a_file_gone_from_a_visible_folder_goes_back_to_pending(
        self, real_adapter, tmp_path
    ):
        media = str(tmp_path / "media")
        path = _lay_out(media, "present")
        await _seed(real_adapter, path)
        backup = _verify_backup(real_adapter, media)

        await backup._verify_and_redownload_media()

        backup._process_media.assert_awaited_once()
        assert (await _stored(real_adapter))[0] == 0


class TestTheDownloadPathKeepsTheFlag:
    """``_process_media`` over a row that says downloaded whose file is not on disk."""

    def _backup(self, adapter, media: str) -> TelegramBackup:
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.config.media_path = media
        backup.config.deduplicate_media = False
        backup.config.download_youtube_videos = True
        backup.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        backup.config.max_filename_bytes = 255
        backup.db = adapter
        backup._get_media_type = MagicMock(return_value="voice")
        backup._get_media_filename = MagicMock(return_value=FILE)
        backup._get_media_size = MagicMock(return_value=len(BYTES))
        backup._download_media_to_path = AsyncMock(side_effect=OSError("share dropped"))
        return backup

    @staticmethod
    def _message():
        return MagicMock(id=7, media=MagicMock(), edit_date=None, edit_hide=False)

    @staticmethod
    def _telegram():
        """The same file Telegram serves for the message, and no filter declines it."""
        patcher = patch.multiple(
            "telegram_archive.telegram_backup",
            media_file_id=MagicMock(return_value=FILE_ID),
            media_download_allowed=MagicMock(return_value=True),
        )
        return patcher

    async def test_a_folder_not_visible_keeps_the_row_and_downloads_nothing(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "missing")
        await _seed(real_adapter, path)
        backup = self._backup(real_adapter, media)

        with self._telegram():
            result = await backup._process_media(self._message(), CHAT)
        await real_adapter.insert_media(result, account_id=1)

        backup._download_media_to_path.assert_not_awaited()
        assert await _stored(real_adapter) == (1, path)

    async def test_a_failed_download_into_a_gone_folder_keeps_the_flag(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "parent_missing")
        await _seed(real_adapter, path)
        backup = self._backup(real_adapter, media)

        with self._telegram():
            result = await backup._process_media(self._message(), CHAT)
        assert "downloaded" not in result
        await real_adapter.insert_media(result, account_id=1)

        backup._download_media_to_path.assert_awaited()
        assert (await _stored(real_adapter))[0] == 1

    async def test_control_a_failed_download_of_a_file_gone_from_a_visible_folder_is_pending(
        self, real_adapter, tmp_path
    ):
        media = str(tmp_path / "media")
        path = _lay_out(media, "present")
        await _seed(real_adapter, path)
        backup = self._backup(real_adapter, media)

        with self._telegram():
            result = await backup._process_media(self._message(), CHAT)
        await real_adapter.insert_media(result, account_id=1)

        assert (await _stored(real_adapter))[0] == 0


class TestCheckMediaBringsBackWhatAnOutageLeft:
    """A row an outage marked, whose attempts ran out, with its file back at its path."""

    async def _capped(self, adapter, media: str, *, content: bytes = BYTES, **extra) -> tuple[dict, str]:
        path = _lay_out(media, "present")
        with open(path, "wb") as handle:
            handle.write(content)
        row = await _seed(adapter, path, **extra)
        await adapter.mark_media_for_redownload(row["id"], account_id=1, keep_path=True)
        for _ in range(5):
            await adapter.increment_media_download_attempts(row["id"], account_id=1)
        assert await adapter.count_capped_media_downloads(5, account_id=1) == 1
        return row, path

    async def test_repair_marks_it_downloaded_again(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        _row, path = await self._capped(real_adapter, media, content_hash=SHA256)

        dry = await check_media(real_adapter, media)
        assert dry["recoverable"] == 1
        assert await _stored(real_adapter) == (0, path)  # a dry run writes nothing

        repaired = await check_media(real_adapter, media, repair=True)

        assert (repaired["recoverable"], repaired["recovered"]) == (1, 1)
        assert await _stored(real_adapter) == (1, path)
        assert await real_adapter.count_capped_media_downloads(5, account_id=1) == 0
        again = await check_media(real_adapter, media)
        assert (again["recoverable"], again["present"]) == (0, 1)

    async def test_a_file_whose_hash_differs_is_not_taken(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        await self._capped(real_adapter, media, content=b"other bytes", content_hash=SHA256)

        report = await check_media(real_adapter, media, repair=True)

        assert (report["recoverable"], report["recovered"]) == (0, 0)
        assert (await _stored(real_adapter))[0] == 0

    async def test_a_row_skipped_on_purpose_is_left_alone(self, real_adapter, tmp_path):
        media = str(tmp_path / "media")
        path = _lay_out(media, "present")
        with open(path, "wb") as handle:
            handle.write(BYTES)
        await _seed(real_adapter, path, downloaded=False, skip_reason="filtered")

        report = await check_media(real_adapter, media, repair=True)

        assert report["recoverable"] == 0
        assert (await _stored(real_adapter))[0] == 0

    async def test_the_command_counts_it_in_a_dry_run_and_exits_1(self, real_adapter, tmp_path):
        data_dir = tmp_path / "backups"
        await self._capped(real_adapter, str(data_dir / "media"))

        code, out = await TestCheckMediaCommand()._run(real_adapter, data_dir)
        assert code == 1
        assert "File back at its path:     1" in out
        code, out = await TestCheckMediaCommand()._run(real_adapter, data_dir, "--repair")
        assert code == 0
        assert "Marked downloaded again:   1" in out
