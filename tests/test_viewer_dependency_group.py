"""The viewer-runtime dependency group must mirror [project] dependencies.

Dockerfile.viewer installs ONLY this group. An entry that drifts from the
main list (version bumped in one place, not the other) would ship the
exposed image with a silently different dependency than the one the test
suite runs against.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_viewer_group_is_an_exact_subset_of_project_dependencies():
    data = tomllib.loads(PYPROJECT.read_text())
    project_deps = set(data["project"]["dependencies"])
    group = data["dependency-groups"]["viewer-runtime"]

    assert group, "viewer-runtime group must not be empty"
    missing = [entry for entry in group if entry not in project_deps]
    assert not missing, f"viewer-runtime entries must appear VERBATIM in [project] dependencies; drifted: {missing}"


def test_viewer_group_excludes_the_capture_stack():
    data = tomllib.loads(PYPROJECT.read_text())
    joined = " ".join(data["dependency-groups"]["viewer-runtime"]).lower()
    for banned in ("telethon", "cryptg", "psycopg2", "apscheduler", "python-socks"):
        assert banned not in joined, f"{banned} must never ship in the viewer image"
