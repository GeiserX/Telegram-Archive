"""Tests for interactive authentication setup module."""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.setup_auth import _print_permission_error_help, main, setup_authentication


class TestPrintPermissionErrorHelp(unittest.TestCase):
    """Test _print_permission_error_help output."""

    def test_prints_permission_error_guidance(self):
        """Prints Podman and Docker permission guidance to stdout."""
        with patch("builtins.print") as mock_print:
            _print_permission_error_help()

        # Verify key guidance sections are printed
        printed_text = " ".join(str(call) for call in mock_print.call_args_list)
        assert "PERMISSION ERROR" in printed_text
        assert "Podman" in printed_text
        assert "Docker" in printed_text
        assert "userns=keep-id" in printed_text

    def test_includes_uid_and_gid(self):
        """Prints the current UID and GID for the docker --user suggestion."""
        with patch("builtins.print") as mock_print:
            _print_permission_error_help()

        printed_text = " ".join(str(call) for call in mock_print.call_args_list)
        assert str(os.getuid()) in printed_text
        assert str(os.getgid()) in printed_text


@pytest.mark.asyncio
async def test_setup_authentication_already_authorized():
    """Returns True and skips code request when already authorized."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(first_name="John", last_name="Doe", username="johndoe", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }
        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.setup_auth.TelegramClient", return_value=mock_client),
        ):
            result = await setup_authentication()

        assert result is True
        mock_client.connect.assert_awaited_once()
        mock_client.is_user_authorized.assert_awaited_once()
        mock_client.send_code_request.assert_not_awaited()
        mock_client.disconnect.assert_awaited_once()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_with_code_input():
    """Authenticates with verification code when not yet authorized."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.sign_in = AsyncMock()
        mock_me = MagicMock(first_name="John", last_name=None, username=None, phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }
        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.setup_auth.TelegramClient", return_value=mock_client),
            patch("builtins.input", return_value="12345"),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is True
        mock_client.send_code_request.assert_awaited_once_with("+1234567890")
        mock_client.sign_in.assert_awaited_once_with("+1234567890", "12345")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_with_2fa_password():
    """Authenticates with 2FA password when sign_in raises password error."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.sign_in = AsyncMock(side_effect=[Exception("Two-steps verification is enabled"), None])
        mock_me = MagicMock(first_name="John", last_name="Doe", username="johndoe", phone="+1234567890")
        mock_client.get_me = AsyncMock(return_value=mock_me)

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }
        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.setup_auth.TelegramClient", return_value=mock_client),
            patch("builtins.input", side_effect=["12345", "my2fapassword"]),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is True
        # First call: code sign-in (raises), second call: password sign-in
        assert mock_client.sign_in.await_count == 2
        mock_client.sign_in.assert_any_call(password="my2fapassword")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_sign_in_non_2fa_error_reraises():
    """Re-raises sign_in errors that are not 2FA-related."""
    temp_dir = tempfile.mkdtemp()
    try:
        mock_client = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.sign_in = AsyncMock(side_effect=Exception("Invalid code"))

        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
            "BACKUP_PATH": temp_dir,
        }
        with (
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.setup_auth.TelegramClient", return_value=mock_client),
            patch("builtins.input", return_value="12345"),
            patch("builtins.print"),
        ):
            # The non-2FA error bubbles up to the outer except, returns False
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_value_error_returns_false():
    """Returns False on ValueError (missing credentials)."""
    env_vars = {}
    with patch.dict(os.environ, env_vars, clear=True):
        result = await setup_authentication()
    assert result is False


@pytest.mark.asyncio
async def test_setup_authentication_permission_error_returns_false():
    """Returns False and prints help on PermissionError."""
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
            patch.dict(os.environ, env_vars, clear=True),
            patch("src.setup_auth.TelegramClient", side_effect=PermissionError("No write access")),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_sqlite_unable_to_open_returns_false():
    """Returns False and prints help on sqlite3 unable to open database."""
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
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "src.setup_auth.TelegramClient",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_sqlite_other_error_returns_false():
    """Returns False on non-permission sqlite3 errors."""
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
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "src.setup_auth.TelegramClient",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_generic_permission_denied_returns_false():
    """Returns False on generic exception containing 'permission denied'."""
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
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "src.setup_auth.TelegramClient",
                side_effect=RuntimeError("permission denied on /data"),
            ),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_generic_unable_to_open_db_returns_false():
    """Returns False on generic exception containing 'unable to open database file'."""
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
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "src.setup_auth.TelegramClient",
                side_effect=RuntimeError("unable to open database file: /data/session"),
            ),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_setup_authentication_generic_error_returns_false():
    """Returns False on generic unexpected exception."""
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
            patch.dict(os.environ, env_vars, clear=True),
            patch(
                "src.setup_auth.TelegramClient",
                side_effect=RuntimeError("unexpected failure"),
            ),
            patch("builtins.print"),
        ):
            result = await setup_authentication()

        assert result is False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class TestMain(unittest.TestCase):
    """Test main() entry point."""

    def test_main_success_exits_zero(self):
        """main() exits with code 0 on successful authentication."""
        with (
            patch("src.setup_auth.asyncio.run", return_value=True),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0

    def test_main_failure_exits_one(self):
        """main() exits with code 1 on failed authentication."""
        with (
            patch("src.setup_auth.asyncio.run", return_value=False),
            patch("builtins.print"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_main_prints_setup_banner(self):
        """main() prints the setup banner before running auth."""
        with (
            patch("src.setup_auth.asyncio.run", return_value=True),
            patch("builtins.print") as mock_print,
            pytest.raises(SystemExit),
        ):
            main()

        printed_text = " ".join(str(call) for call in mock_print.call_args_list)
        assert "Telegram Backup - Authentication Setup" in printed_text
        assert "Make sure you have" in printed_text

    def test_main_prints_next_steps_on_success(self):
        """main() prints next steps after successful auth."""
        with (
            patch("src.setup_auth.asyncio.run", return_value=True),
            patch("builtins.print") as mock_print,
            pytest.raises(SystemExit),
        ):
            main()

        printed_text = " ".join(str(call) for call in mock_print.call_args_list)
        assert "Setup completed successfully" in printed_text
        assert "Next steps" in printed_text

    def test_main_prints_failure_message_on_error(self):
        """main() prints failure message after failed auth."""
        with (
            patch("src.setup_auth.asyncio.run", return_value=False),
            patch("builtins.print") as mock_print,
            pytest.raises(SystemExit),
        ):
            main()

        printed_text = " ".join(str(call) for call in mock_print.call_args_list)
        assert "Setup failed" in printed_text


if __name__ == "__main__":
    unittest.main()
