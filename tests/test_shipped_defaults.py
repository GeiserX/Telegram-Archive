"""The shipped compose file, .env.example and the settings reference agree with the code.

9.2.0 turned the listener on by default and made the full pass daily. A Docker
install reads its defaults from docker-compose.yml and .env.example, not from
config.py, so a default changed in one place and not the others is a default
that changed only for pip installs. These tests read the three files and
compare each value with what ``Config()`` resolves when nothing is set.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_archive.config import Config

REPO = Path(__file__).resolve().parents[1]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")
REFERENCE = (REPO / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")

# Variable -> Config attribute. The listener switches whose defaults are written
# out in the shipped files.
LISTENER_FLAGS = {
    "LISTEN_EDITS": "listen_edits",
    "LISTEN_DELETIONS": "listen_deletions",
    "LISTEN_NEW_MESSAGES": "listen_new_messages",
    "LISTEN_NEW_MESSAGES_MEDIA": "listen_new_messages_media",
    "LISTEN_CHAT_ACTIONS": "listen_chat_actions",
    "LISTEN_REACTIONS": "listen_reactions",
}


def _config(**env: str) -> Config:
    base = {"BACKUP_PATH": tempfile.mkdtemp(prefix="ta_test_shipped_"), **env}
    with patch.dict(os.environ, base, clear=True):
        return Config()


def _compose_default(name: str, *, commented: bool = False) -> str:
    """The ``${NAME:-default}`` fallback the backup service gives NAME."""
    prefix = r"#\s*" if commented else ""
    matches = re.findall(rf"^\s*{prefix}{name}: \$\{{{name}:-([^}}]*)\}}$", COMPOSE, re.MULTILINE)
    assert len(matches) == 1, f"{name}: expected one compose line, found {matches}"
    return matches[0]


def _env_example_value(name: str, *, commented: bool = False) -> str:
    prefix = r"#\s*" if commented else ""
    matches = re.findall(rf"^{prefix}{name}=([^#\n]*?)\s*(?:#.*)?$", ENV_EXAMPLE, re.MULTILINE)
    assert len(matches) == 1, f"{name}: expected one .env.example line, found {matches}"
    return matches[0]


def _reference_default(name: str) -> str:
    """The Default cell of NAME's row in the settings reference."""
    matches = re.findall(rf"^\| <span id=\"{name.lower()}\"></span>`{name}` \| ([^|]*) \|", REFERENCE, re.MULTILINE)
    assert len(matches) == 1, f"{name}: expected one reference row, found {matches}"
    return matches[0]


def _bool(text: str) -> bool:
    assert text in ("true", "false"), text
    return text == "true"


class TestShippedDefaults(unittest.TestCase):
    def test_the_listener_is_on_everywhere(self):
        default = _config().enable_listener
        self.assertIs(default, True)
        self.assertIs(_bool(_compose_default("ENABLE_LISTENER")), default)
        self.assertIs(_bool(_env_example_value("ENABLE_LISTENER")), default)
        self.assertEqual(_reference_default("ENABLE_LISTENER"), f"`{str(default).lower()}`")

    def test_listener_switches_match_the_code(self):
        config = _config()
        for name, attr in LISTENER_FLAGS.items():
            with self.subTest(name=name):
                default = getattr(config, attr)
                self.assertIs(_bool(_compose_default(name, commented=True)), default)
                self.assertIs(_bool(_env_example_value(name, commented=True)), default)
                self.assertEqual(_reference_default(name), f"`{str(default).lower()}`")

    def test_compose_leaves_the_schedule_to_the_code(self):
        """An empty fallback, so the code picks daily or 6-hourly from ENABLE_LISTENER."""
        self.assertEqual(_compose_default("SCHEDULE"), "")
        self.assertEqual(_config(SCHEDULE="").schedule, "0 3 * * *")
        self.assertEqual(_config(SCHEDULE="", ENABLE_LISTENER="false").schedule, "0 */6 * * *")

    def test_env_example_sets_no_schedule(self):
        """A copied .env.example must not pin a schedule; its commented example is the default."""
        self.assertIsNone(re.search(r"^SCHEDULE=", ENV_EXAMPLE, re.MULTILINE))
        self.assertEqual(_env_example_value("SCHEDULE", commented=True), _config().schedule)

    def test_the_reference_names_both_schedule_defaults(self):
        cell = _reference_default("SCHEDULE")
        self.assertTrue(cell.startswith(f"`{_config().schedule}`"), cell)
        self.assertIn(f"`{_config(ENABLE_LISTENER='false').schedule}` with `ENABLE_LISTENER=false`", cell)
