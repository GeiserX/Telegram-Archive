"""Tests for shared atomic media download finalization helpers."""

from src.message_utils import finalize_atomic_download, sanitize_media_filename


def test_finalize_atomic_download_uses_temporary_fallback(tmp_path):
    """When Telethon leaves the requested temp file behind, move it to the fallback path."""
    temporary_path = tmp_path / "download.part"
    fallback_path = tmp_path / "download.jpg"
    temporary_path.write_bytes(b"image")

    result = finalize_atomic_download(None, str(temporary_path), str(fallback_path))

    assert result == str(fallback_path)
    assert fallback_path.read_bytes() == b"image"
    assert not temporary_path.exists()


def test_finalize_atomic_download_renames_actual_to_intended_name(tmp_path):
    """Telethon returns the temp path verbatim; finalize moves it to the clean name."""
    actual_path = tmp_path / "chosen.jpg.part"
    actual_path.write_bytes(b"image")
    fallback_path = tmp_path / "fallback.jpg"

    result = finalize_atomic_download(str(actual_path), str(actual_path), str(fallback_path))

    assert result == str(fallback_path)
    assert fallback_path.read_bytes() == b"image"
    assert not actual_path.exists()


def test_finalize_atomic_download_does_not_leak_unique_temp_suffix(tmp_path):
    """Regression for #175: the unique .{pid}.{task}.part temp name must never reach disk.

    Telethon treats the trailing .part as the extension and returns our exact temp
    path. Finalize must produce the clean intended name, not video.mp4.<pid>.<task>.
    """
    intended = tmp_path / "1234567890.mp4"
    temp_path = tmp_path / "1234567890.mp4.7.140234567890.part"
    temp_path.write_bytes(b"video-bytes")

    # Telethon returns the temp path it was handed, untouched.
    result = finalize_atomic_download(str(temp_path), str(temp_path), str(intended))

    assert result == str(intended)
    assert intended.read_bytes() == b"video-bytes"
    assert not temp_path.exists()
    # No corrupted siblings left behind.
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == ["1234567890.mp4"], leftovers


def test_finalize_atomic_download_cleans_stale_temp_when_real_file_elsewhere(tmp_path):
    """If Telethon writes the real file at a different path, the temp stub is removed."""
    actual_path = tmp_path / "real.mp4"
    actual_path.write_bytes(b"video")
    temp_path = tmp_path / "real.mp4.7.99.part"
    temp_path.write_bytes(b"")  # stale zero-byte stub
    intended = tmp_path / "real.mp4.final"

    result = finalize_atomic_download(str(actual_path), str(temp_path), str(intended))

    assert result == str(intended)
    assert intended.read_bytes() == b"video"
    assert not temp_path.exists()
    assert not actual_path.exists()


def test_finalize_atomic_download_returns_none_when_no_file_was_created(tmp_path):
    """Helper must not report success when Telethon did not create a file."""
    missing_temp = tmp_path / "missing.part"
    fallback_path = tmp_path / "fallback.jpg"

    assert finalize_atomic_download(None, str(missing_temp), str(fallback_path)) is None
    assert not fallback_path.exists()


# ---------------------------------------------------------------------------
# sanitize_media_filename — attacker-controlled Telegram document file names
# ---------------------------------------------------------------------------


def test_sanitize_strips_posix_traversal():
    assert sanitize_media_filename("../../etc/passwd") == "passwd"


def test_sanitize_strips_windows_traversal():
    assert sanitize_media_filename("..\\..\\windows\\system32\\evil.dll") == "evil.dll"


def test_sanitize_collapses_leading_path_to_basename():
    assert sanitize_media_filename("/abs/path/movie.mp4") == "movie.mp4"


def test_sanitize_passes_clean_names_through():
    assert sanitize_media_filename("1234_video.mp4") == "1234_video.mp4"


def test_sanitize_neutralises_pure_traversal_tokens():
    assert sanitize_media_filename("..") == "_"
    assert sanitize_media_filename("") == "_"
    assert sanitize_media_filename(".") == "_"


def test_sanitize_drops_nul_byte():
    assert "\x00" not in sanitize_media_filename("evil\x00.mp4")
