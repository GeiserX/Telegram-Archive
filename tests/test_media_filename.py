"""Regression tests for build_media_filename (#212 — Synology eCryptfs filename limits)."""

from src.message_utils import _MEDIA_PART_SUFFIX_RESERVE, build_media_filename

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
