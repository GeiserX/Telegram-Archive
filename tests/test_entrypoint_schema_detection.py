"""Regression tests for container entrypoint schema stamping guards."""

from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"


def test_entrypoint_detects_message_versions_for_revision_015():
    """Pre-Alembic stamp detection should use the revision-015 table name."""
    script = ENTRYPOINT.read_text(encoding="utf-8")
    old_table_name = "message_" + "edit_history"

    assert old_table_name not in script
    assert "message_versions" in script
    assert "has_015_message_versions" in script
    assert "if has_015_message_versions and has_014_soft_delete:" in script
