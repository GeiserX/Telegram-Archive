"""Every user-facing image pin must name the version being released.

docker-compose.yml sat on 8.1.0 - the last amd64-only release - while main
was three releases ahead (#417): nothing made the pins move with a release,
so they drifted until an arm64 user hit the one tag that could not run on
his machine. A release bumps src/__init__.py; these assertions make the
same commit move every shipped pin with it, so the release PR goes red
until compose, README and the migration helper all point at the release.

docs/UPGRADING-8.0.md is deliberately not covered: it documents the one-off
7.x -> 8.0 migration and its 8.0.1 pins are the point.
"""

import re
from pathlib import Path

from src import __version__

REPO = Path(__file__).resolve().parent.parent

# A drumsergio/telegram-archive[-viewer] reference immediately followed by a
# tag. Bare-name references (Docker Hub URLs, badge links, image tables)
# carry no ":" and are exempt by construction; every tagged reference in the
# covered files is whitespace-delimited.
PIN = re.compile(r"drumsergio/telegram-archive(?:-viewer)?:(\S+)")

PINNED_FILES = (
    "docker-compose.yml",
    "README.md",
    "scripts/migrate-sqlite-to-postgres.py",
)


def _pins(name: str) -> list[tuple[str, int, str]]:
    text = (REPO / name).read_text(encoding="utf-8")
    return [
        (name, lineno, match.group(1))
        for lineno, line in enumerate(text.splitlines(), start=1)
        for match in PIN.finditer(line)
    ]


def test_every_pinned_file_actually_contains_pins():
    """Guard the guard: a rename or rewrite that drops every reference would
    otherwise turn the version assertion into vacuous green."""
    for name in PINNED_FILES:
        assert _pins(name), f"{name} no longer pins any image; update PINNED_FILES"


def test_shipped_image_pins_match_the_released_version():
    stale = [pin for name in PINNED_FILES for pin in _pins(name) if pin[2] != __version__]
    assert not stale, f"image pins must equal src.__version__ ({__version__}); stale (file, line, tag): {stale}"
