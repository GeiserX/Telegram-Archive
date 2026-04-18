"""Tests for export backup module."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.export_backup import BackupExporter, async_main, main


class TestBackupExporterInit(unittest.TestCase):
    """Test BackupExporter initialization."""

    def test_init_stores_db(self):
        """Constructor stores database adapter reference."""
        mock_db = MagicMock()
        exporter = BackupExporter(mock_db)
        assert exporter.db is mock_db


@pytest.mark.asyncio
async def test_create_factory_initializes_db_and_returns_instance():
    """create() factory method initializes database and returns BackupExporter."""
    mock_db = AsyncMock()

    with (
        patch("src.export_backup.init_database", new_callable=AsyncMock) as mock_init,
        patch("src.db.get_adapter", new_callable=AsyncMock, return_value=mock_db) as mock_get,
    ):
        config = MagicMock()
        exporter = await BackupExporter.create(config)

    mock_init.assert_awaited_once()
    mock_get.assert_awaited_once()
    assert exporter.db is mock_db


@pytest.mark.asyncio
async def test_export_to_json_writes_correct_structure():
    """export_to_json writes messages and chats to JSON with correct structure."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_db = AsyncMock()
        mock_db.get_messages_by_date_range = AsyncMock(
            return_value=[
                {"id": 1, "text": "hello", "date": "2024-01-01"},
                {"id": 2, "text": "world", "date": "2024-01-02"},
            ]
        )
        mock_db.get_all_chats = AsyncMock(
            return_value=[
                {"id": -100123, "type": "group", "title": "Test Group"},
            ]
        )

        exporter = BackupExporter(mock_db)
        output_file = os.path.join(temp_dir, "export.json")

        await exporter.export_to_json(output_file)

        assert os.path.isfile(output_file)
        with open(output_file, encoding="utf-8") as f:
            data = json.load(f)

        assert "export_date" in data
        assert data["statistics"]["total_messages"] == 2
        assert data["statistics"]["total_chats"] == 1
        assert len(data["messages"]) == 2
        assert len(data["chats"]) == 1
        assert data["filters"]["chat_id"] is None
        assert data["filters"]["start_date"] is None
        assert data["filters"]["end_date"] is None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_export_to_json_with_date_filters():
    """export_to_json passes parsed dates to the database query."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_db = AsyncMock()
        mock_db.get_messages_by_date_range = AsyncMock(return_value=[])
        mock_db.get_all_chats = AsyncMock(return_value=[])

        exporter = BackupExporter(mock_db)
        output_file = os.path.join(temp_dir, "export.json")

        await exporter.export_to_json(output_file, chat_id=123, start_date="2024-01-01", end_date="2024-06-30")

        mock_db.get_messages_by_date_range.assert_awaited_once()
        call_args = mock_db.get_messages_by_date_range.call_args
        assert call_args[0][0] == 123
        assert call_args[0][1] == datetime(2024, 1, 1)
        assert call_args[0][2] == datetime(2024, 6, 30)

        with open(output_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["filters"]["chat_id"] == 123
        assert data["filters"]["start_date"] == "2024-01-01"
        assert data["filters"]["end_date"] == "2024-06-30"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_export_to_json_creates_parent_directories():
    """export_to_json creates parent directories for output file."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_db = AsyncMock()
        mock_db.get_messages_by_date_range = AsyncMock(return_value=[])
        mock_db.get_all_chats = AsyncMock(return_value=[])

        exporter = BackupExporter(mock_db)
        output_file = os.path.join(temp_dir, "nested", "deep", "export.json")

        await exporter.export_to_json(output_file)

        assert os.path.isfile(output_file)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_list_chats_prints_chat_table():
    """list_chats prints a formatted table of backed up chats."""
    mock_db = AsyncMock()
    mock_db.get_all_chats = AsyncMock(
        return_value=[
            {
                "id": -100123,
                "type": "group",
                "title": "Test Group",
                "first_name": None,
                "last_name": None,
                "updated_at": datetime(2024, 3, 15, 10, 30),
            },
            {
                "id": 456,
                "type": "private",
                "title": None,
                "first_name": "John",
                "last_name": "Doe",
                "updated_at": "2024-03-15T10:30:00",
            },
            {
                "id": 789,
                "type": "private",
                "title": None,
                "first_name": "Jane",
                "last_name": None,
                "updated_at": None,
            },
        ]
    )

    exporter = BackupExporter(mock_db)

    with patch("builtins.print") as mock_print:
        await exporter.list_chats()

    printed_text = " ".join(str(call) for call in mock_print.call_args_list)
    assert "Backed Up Chats" in printed_text
    assert "Total: 3 chats" in printed_text
    assert "Test Group" in printed_text
    assert "John Doe" in printed_text
    assert "N/A" in printed_text


@pytest.mark.asyncio
async def test_list_chats_with_datetime_updated_at():
    """list_chats formats datetime objects with isoformat."""
    mock_db = AsyncMock()
    mock_db.get_all_chats = AsyncMock(
        return_value=[
            {
                "id": 100,
                "type": "private",
                "title": "Test",
                "updated_at": datetime(2024, 6, 15, 14, 30, 45),
            },
        ]
    )

    exporter = BackupExporter(mock_db)

    with patch("builtins.print") as mock_print:
        await exporter.list_chats()

    printed_text = " ".join(str(call) for call in mock_print.call_args_list)
    assert "2024-06-15T14:30:45" in printed_text


@pytest.mark.asyncio
async def test_show_statistics_prints_stats():
    """show_statistics prints formatted backup statistics."""
    mock_db = AsyncMock()
    mock_db.get_statistics = AsyncMock(
        return_value={
            "chats": 42,
            "messages": 10000,
            "media_files": 500,
            "total_size_mb": 1024,
        }
    )

    exporter = BackupExporter(mock_db)

    with patch("builtins.print") as mock_print:
        await exporter.show_statistics()

    printed_text = " ".join(str(call) for call in mock_print.call_args_list)
    assert "Backup Statistics" in printed_text
    assert "42" in printed_text
    assert "10000" in printed_text
    assert "500" in printed_text
    assert "1024" in printed_text


@pytest.mark.asyncio
async def test_close_calls_close_database():
    """close() delegates to close_database()."""
    mock_db = AsyncMock()
    exporter = BackupExporter(mock_db)

    with patch("src.export_backup.close_database", new_callable=AsyncMock) as mock_close:
        await exporter.close()

    mock_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_no_command_prints_help():
    """async_main returns 0 and prints help when no command given."""
    with patch("sys.argv", ["export_backup"]):
        result = await async_main()
    assert result == 0


@pytest.mark.asyncio
async def test_async_main_export_command():
    """async_main export command calls export_to_json with correct args."""
    temp_dir = tempfile.mkdtemp()
    try:
        output_file = os.path.join(temp_dir, "out.json")
        mock_exporter = AsyncMock(spec=BackupExporter)

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }

        with (
            patch("sys.argv", ["export_backup", "export", "-o", output_file, "-c", "123"]),
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.export_backup.BackupExporter.create", new_callable=AsyncMock, return_value=mock_exporter),
        ):
            result = await async_main()

        assert result == 0
        mock_exporter.export_to_json.assert_awaited_once_with(output_file, 123, None, None)
        mock_exporter.close.assert_awaited_once()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_async_main_list_chats_command():
    """async_main list-chats command calls list_chats."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_exporter = AsyncMock(spec=BackupExporter)

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }

        with (
            patch("sys.argv", ["export_backup", "list-chats"]),
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.export_backup.BackupExporter.create", new_callable=AsyncMock, return_value=mock_exporter),
        ):
            result = await async_main()

        assert result == 0
        mock_exporter.list_chats.assert_awaited_once()
        mock_exporter.close.assert_awaited_once()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_async_main_stats_command():
    """async_main stats command calls show_statistics."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_exporter = AsyncMock(spec=BackupExporter)

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }

        with (
            patch("sys.argv", ["export_backup", "stats"]),
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.export_backup.BackupExporter.create", new_callable=AsyncMock, return_value=mock_exporter),
        ):
            result = await async_main()

        assert result == 0
        mock_exporter.show_statistics.assert_awaited_once()
        mock_exporter.close.assert_awaited_once()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_async_main_handles_exception():
    """async_main returns 1 when an exception occurs."""
    temp_dir = tempfile.mkdtemp()
    try:
        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }

        with (
            patch("sys.argv", ["export_backup", "stats"]),
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "src.export_backup.BackupExporter.create",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB init failed"),
            ),
        ):
            result = await async_main()

        assert result == 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class TestMainEntryPoint(unittest.TestCase):
    """Test main() synchronous entry point."""

    def test_main_delegates_to_async_main(self):
        """main() calls asyncio.run(async_main()) and returns result."""
        with patch("src.export_backup.asyncio.run", return_value=0) as mock_run:
            result = main()

        assert result == 0
        mock_run.assert_called_once()

    def test_main_returns_error_code(self):
        """main() returns non-zero on failure."""
        with patch("src.export_backup.asyncio.run", return_value=1):
            result = main()

        assert result == 1


if __name__ == "__main__":
    unittest.main()
