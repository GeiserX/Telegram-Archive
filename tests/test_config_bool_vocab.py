"""One boolean vocabulary for every flag, and failures that name the flag.

13 flags used to accept only the literal string "true": DOWNLOAD_MEDIA=1
disabled all media capture, ENABLE_LISTENER=yes disabled real-time capture —
silently, while four sibling flags accepted 1/yes/on. The mirror flaw: a typo
in those four raised "Invalid boolean value: ture" without saying which
variable carried it.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import Config, _parse_bool_env

BASE_ENV = {
    "TELEGRAM_API_ID": "12345",
    "TELEGRAM_API_HASH": "test@value/here",
    "TELEGRAM_PHONE": "+1234567890",
    "BACKUP_PATH": tempfile.mkdtemp(prefix="ta_test_boolvocab_"),
    # EVENT_WEBHOOK_ENABLED=true force-disables itself when the URL is absent
    # (side-channel warn+disable rule), which would break the read-back assert.
    "EVENT_WEBHOOK_URL": "https://hooks.example.test/x",
}

# Every boolean flag with its documented default (attr, env name, default).
FLAGS = [
    ("download_media", "DOWNLOAD_MEDIA", True),
    ("skip_media_delete_existing", "SKIP_MEDIA_DELETE_EXISTING", True),
    ("sync_deletions_edits", "SYNC_DELETIONS_EDITS", False),
    ("verify_media", "VERIFY_MEDIA", False),
    ("fill_gaps", "FILL_GAPS", False),
    ("enable_listener", "ENABLE_LISTENER", False),
    ("listen_edits", "LISTEN_EDITS", True),
    ("listen_new_messages", "LISTEN_NEW_MESSAGES", True),
    ("listen_new_messages_media", "LISTEN_NEW_MESSAGES_MEDIA", False),
    ("listen_chat_actions", "LISTEN_CHAT_ACTIONS", True),
    ("deduplicate_media", "DEDUPLICATE_MEDIA", True),
    ("enable_notifications", "ENABLE_NOTIFICATIONS", False),
    ("show_stats", "SHOW_STATS", True),
    ("parallel_download_enabled", "PARALLEL_DOWNLOAD_ENABLED", False),
    ("listen_deletions", "LISTEN_DELETIONS", False),
    ("listen_reactions", "LISTEN_REACTIONS", False),
    ("follow_chat_migrations", "FOLLOW_CHAT_MIGRATIONS", False),
    ("event_webhook_enabled", "EVENT_WEBHOOK_ENABLED", False),
]


class TestBooleanVocabulary(unittest.TestCase):
    def test_every_flag_accepts_the_full_true_vocabulary(self):
        for spelling in ("1", "yes", "on", "TRUE", "True"):
            for attr, env, _default in FLAGS:
                with (
                    self.subTest(flag=env, spelling=spelling),
                    patch.dict(os.environ, {**BASE_ENV, env: spelling}, clear=True),
                ):
                    self.assertIs(getattr(Config(), attr), True, f"{env}={spelling} read as False")

    def test_every_flag_accepts_the_full_false_vocabulary(self):
        for spelling in ("0", "no", "off", "FALSE"):
            for attr, env, _default in FLAGS:
                with (
                    self.subTest(flag=env, spelling=spelling),
                    patch.dict(os.environ, {**BASE_ENV, env: spelling}, clear=True),
                ):
                    self.assertIs(getattr(Config(), attr), False)

    def test_unset_flags_keep_their_documented_defaults(self):
        with patch.dict(os.environ, dict(BASE_ENV), clear=True):
            config = Config()
            for attr, env, default in FLAGS:
                with self.subTest(flag=env):
                    self.assertIs(getattr(config, attr), default)

    def test_a_typo_names_the_variable(self):
        with (
            patch.dict(os.environ, {**BASE_ENV, "DOWNLOAD_MEDIA": "ture"}, clear=True),
            self.assertRaises(ValueError) as ctx,
        ):
            Config()
        self.assertIn("DOWNLOAD_MEDIA", str(ctx.exception))

    def test_parse_bool_env_reads_the_named_variable(self):
        with patch.dict(os.environ, {"SOME_FLAG": "on"}, clear=True):
            self.assertIs(_parse_bool_env("SOME_FLAG", False), True)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(_parse_bool_env("SOME_FLAG", True), True)


_ENTRYPOINT = Path(__file__).resolve().parent.parent / "scripts" / "entrypoint.sh"


class TestEntrypointDbTypeNormalisation(unittest.TestCase):
    def test_entrypoint_lowercases_db_type_before_any_compare(self):
        script = _ENTRYPOINT.read_text()
        normalise = script.index("tr '[:upper:]' '[:lower:]'")
        first_compare = script.index('"$DB_TYPE" = "postgresql"')
        self.assertLess(normalise, first_compare, "DB_TYPE must be normalised before it is compared")

    def test_entrypoint_fails_loud_on_unrecognised_database_config(self):
        script = _ENTRYPOINT.read_text()
        self.assertIn("refusing to start with migrations skipped", script)


if __name__ == "__main__":
    unittest.main()
