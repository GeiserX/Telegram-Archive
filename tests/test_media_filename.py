"""Regression tests for build_media_filename (#212 — Synology eCryptfs filename limits)
and fallback_media_filename (backup/listener ingest-path parity)."""

from unittest.mock import MagicMock, patch

from src.message_utils import _MEDIA_PART_SUFFIX_RESERVE, build_media_filename, fallback_media_filename

SYNOLOGY_BUDGET = 143


def test_short_latin_name_unchanged():
    """A name well within budget should pass through untouched."""
    assert build_media_filename("12345", "invoice.pdf", SYNOLOGY_BUDGET) == "12345_invoice.pdf"


def test_long_latin_stem_truncated_within_budget():
    """A long Latin stem is truncated so the full name (plus reserve) fits the budget."""
    name = "a" * 500 + ".pdf"
    result = build_media_filename("12345", name, SYNOLOGY_BUDGET)

    assert result.endswith(".pdf")
    assert len(result.encode("utf-8")) + _MEDIA_PART_SUFFIX_RESERVE <= SYNOLOGY_BUDGET


def test_long_multibyte_name_is_valid_utf8_and_budgeted():
    """Cyrillic (multibyte) stems must not split a codepoint and must stay within budget."""
    name = "документ" * 50 + ".mp4"
    result = build_media_filename("987654321", name, SYNOLOGY_BUDGET)

    encoded = result.encode("utf-8")
    assert encoded.decode("utf-8") == result
    assert len(encoded) + _MEDIA_PART_SUFFIX_RESERVE <= SYNOLOGY_BUDGET
    assert result.endswith(".mp4")


def test_extension_preserved_exactly():
    """The extension must survive truncation verbatim."""
    name = "b" * 300 + ".mp4"
    result = build_media_filename("111", name, SYNOLOGY_BUDGET)

    assert result.endswith(".mp4")
    assert not result.endswith(".mp4.mp4")


def test_deterministic_across_calls():
    """Same inputs must always produce the identical output (dedup/retry depend on this)."""
    name = "документ" * 50 + ".mp4"
    first = build_media_filename("42", name, SYNOLOGY_BUDGET)
    second = build_media_filename("42", name, SYNOLOGY_BUDGET)

    assert first == second


def test_file_id_prefix_always_present():
    """The file_id prefix (uniqueness) must appear in every produced name."""
    long_name = "c" * 500 + ".pdf"
    tiny_budget_name = build_media_filename("999", long_name, 20)
    normal_name = build_media_filename("999", "short.pdf", SYNOLOGY_BUDGET)

    assert tiny_budget_name.startswith("999")
    assert normal_name.startswith("999_")


def test_pathological_tiny_budget_uses_hash_fallback():
    """A budget too small for any truncated stem falls back to a deterministic hash."""
    name = "d" * 500 + ".pdf"
    result = build_media_filename("777", name, 20)

    assert result.startswith("777")
    assert result.endswith(".pdf")
    assert len(result.encode("utf-8")) <= 20


def test_name_with_no_extension_handled():
    """A name without an extension should not gain one and should still be prefixed."""
    result = build_media_filename("55", "no_extension_here", SYNOLOGY_BUDGET)

    assert result.startswith("55_")
    assert "." not in result.split("_", 1)[1] or result.split("_", 1)[1].count(".") == 0


def test_path_traversal_neutralized():
    """A traversal-laden original_name must still be neutralized via sanitize_media_filename."""
    result = build_media_filename("88", "../../etc/passwd", SYNOLOGY_BUDGET)

    assert ".." not in result
    assert "/" not in result
    assert result.startswith("88_")


def test_reserved_suffix_is_accounted_for():
    """A name that fits raw 143 bytes but not 143-reserve bytes must still be truncated."""
    # stem + "_" prefix (len("1_") == 2) sized to fit exactly 143 raw bytes, no extension.
    stem = "e" * (SYNOLOGY_BUDGET - 2)
    name = stem  # no extension
    result = build_media_filename("1", name, SYNOLOGY_BUDGET)

    encoded_result = result.encode("utf-8")
    assert len(encoded_result) <= SYNOLOGY_BUDGET
    # Must have actually been truncated (reserve eats into the raw-fits budget).
    assert len(encoded_result) + _MEDIA_PART_SUFFIX_RESERVE <= SYNOLOGY_BUDGET
    assert len(result) < len(f"1_{name}")


# ===========================================================================
# fallback_media_filename — shared no-original-name path (backup/listener parity)
# ===========================================================================


def test_fallback_with_file_id_and_mime_type():
    """A recognized mime_type wins over the media_type default."""
    assert fallback_media_filename("99", "photo", "image/png", message_id=1) == "99.png"


def test_fallback_jpe_corrected_to_jpg():
    """The image/jpeg -> .jpe mimetypes quirk is corrected to .jpg."""
    with patch("mimetypes.guess_extension", return_value=".jpe"):
        result = fallback_media_filename("50", "photo", "image/jpeg", message_id=1)
    assert result == "50.jpg"


def test_fallback_unknown_mime_type_uses_media_type_default():
    """An unrecognized mime_type falls back to the per-media_type default extension."""
    assert fallback_media_filename("77", "video", "application/x-not-a-real-type", message_id=1) == "77.mp4"


def test_fallback_no_mime_type_uses_media_type_default():
    assert fallback_media_filename("42", "voice", None, message_id=1) == "42.ogg"


def test_fallback_file_id_slashes_sanitized():
    assert fallback_media_filename("a/b\\c", "document", None, message_id=1) == "a_b_c.bin"


def test_fallback_without_file_id_uses_message_id():
    """No telegram_file_id falls back to a deterministic <message_id>_<media_type> name."""
    assert fallback_media_filename(None, "video", None, message_id=42) == "42_video.mp4"


def test_fallback_extension_table_matches_for_all_media_types():
    """Every known media_type maps to its expected extension, with and without a file_id."""
    expected = {
        "photo": "jpg",
        "video": "mp4",
        "animation": "mp4",
        "voice": "ogg",
        "audio": "mp3",
        "sticker": "webp",
        "document": "bin",
    }
    for media_type, ext in expected.items():
        assert fallback_media_filename("id1", media_type, None, message_id=1) == f"id1.{ext}"
        assert fallback_media_filename(None, media_type, None, message_id=7) == f"7_{media_type}.{ext}"


def test_fallback_unknown_media_type_defaults_to_bin():
    assert fallback_media_filename("id1", "some_new_type", None, message_id=1) == "id1.bin"


# ===========================================================================
# Cross-module parity: TelegramBackup and TelegramListener must produce the
# SAME filename for the SAME inputs when there is no usable original name.
# ===========================================================================


def _make_media_message(*, msg_id, mime_type=None):
    """A mock message with document media but no file_name attribute (no original name)."""
    msg = MagicMock()
    msg.id = msg_id
    doc = MagicMock()
    doc.mime_type = mime_type
    attr = MagicMock(spec=[])  # no file_name attribute
    doc.attributes = [attr]
    msg.media.document = doc
    return msg


def test_backup_and_listener_fallback_filenames_match():
    """The two ingest paths must converge on identical names (#issue)."""
    from src.listener import TelegramListener
    from src.telegram_backup import TelegramBackup

    backup = TelegramBackup.__new__(TelegramBackup)
    listener = TelegramListener.__new__(TelegramListener)

    cases = [
        ("photo", "image/png", "12345"),
        ("video", None, "999"),
        ("document", None, None),
        ("sticker", "image/webp", "1"),
    ]
    for media_type, mime_type, telegram_file_id in cases:
        msg = _make_media_message(msg_id=123, mime_type=mime_type)
        backup_result = backup._get_media_filename(msg, media_type, telegram_file_id)
        listener_result = listener._get_media_filename(msg, media_type, telegram_file_id)
        assert backup_result == listener_result, f"Mismatch for {media_type}/{mime_type}/{telegram_file_id}"
