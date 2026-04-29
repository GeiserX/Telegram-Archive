"""Tests for shared atomic media download finalization helpers."""

from src import listener, telegram_backup


def test_telegram_backup_finalize_atomic_download_uses_temporary_fallback(tmp_path):
    """When Telethon leaves the requested temp file behind, move it to the fallback path."""
    temporary_path = tmp_path / "download.part"
    fallback_path = tmp_path / "download.jpg"
    temporary_path.write_bytes(b"image")

    result = telegram_backup._finalize_atomic_download(None, str(temporary_path), str(fallback_path))

    assert result == str(fallback_path)
    assert fallback_path.read_bytes() == b"image"
    assert not temporary_path.exists()


def test_telegram_backup_finalize_atomic_download_strips_part_suffix(tmp_path):
    """When Telethon returns a .part path, remove the suffix atomically."""
    actual_path = tmp_path / "chosen.jpg.part"
    actual_path.write_bytes(b"image")
    fallback_path = tmp_path / "fallback.jpg"

    result = telegram_backup._finalize_atomic_download(str(actual_path), str(actual_path), str(fallback_path))

    assert result == str(tmp_path / "chosen.jpg")
    assert (tmp_path / "chosen.jpg").read_bytes() == b"image"
    assert not actual_path.exists()


def test_listener_finalize_atomic_download_uses_temporary_fallback(tmp_path):
    """Listener downloads share the same temporary-file fallback behavior."""
    temporary_path = tmp_path / "listener.part"
    fallback_path = tmp_path / "listener.jpg"
    temporary_path.write_bytes(b"image")

    result = listener._finalize_atomic_download(None, str(temporary_path), str(fallback_path))

    assert result == str(fallback_path)
    assert fallback_path.read_bytes() == b"image"
    assert not temporary_path.exists()
