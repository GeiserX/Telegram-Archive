"""TRANSCRIPTION_* parsing (docs/TRANSCRIPTION.md): warn and degrade, never abort.

The feature is on by default with no server: that is the nudge state and it
is valid. A bad server URL disables the feature; a bad callback URL drops the
callback and a malformed secret drops the secret, and polling keeps going.
The callback URL (backup) and the secret (viewer) are independent, since each
process normally holds only one of them. Warnings name the variable and never
echo the value, and ``log_summary`` prints the scheme and host only, never
the key or the secret.
"""

import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.config import Config, setup_logging

KEY = "test@value/here"
SECRET = "whsec_dGVzdEB2YWx1ZS9oZXJl"


def _base_env(temp_dir: str) -> dict:
    return {
        "CHAT_TYPES": "private",
        "BACKUP_PATH": temp_dir,
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "abcdef",
        "TELEGRAM_PHONE": "+1234567890",
    }


class TestTranscriptionConfig(unittest.TestCase):
    URL = "https://akou.example.test:8443/base/path?token=" + KEY

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra):
        with patch.dict(os.environ, _base_env(self.temp_dir) | extra, clear=True):
            return Config()

    def test_defaults_are_on_with_no_server(self):
        config = self._config()
        self.assertTrue(config.transcription_enabled)
        self.assertEqual(config.transcription_url, "")
        self.assertEqual(config.transcription_api_key, "")
        self.assertEqual(config.transcription_preset, "auto")
        # Every media with sound is eligible out of the box; the variable narrows it.
        self.assertEqual(config.transcription_types, {"voice", "video_note", "audio", "video", "document"})
        self.assertEqual(config.transcription_max_seconds, 1800)
        self.assertEqual(config.transcription_language, "")
        self.assertEqual(config.transcription_callback_url, "")
        self.assertEqual(config.transcription_webhook_secret, "")
        self.assertEqual(config.transcription_backfill_per_run, 50)

    def test_no_server_is_valid_and_logs_no_warning(self):
        with self.assertNoLogs("src.config", level="WARNING"):
            config = self._config(TRANSCRIPTION_ENABLED="true")
        self.assertTrue(config.transcription_enabled)

    def test_disabled_reads_nothing_else(self):
        config = self._config(TRANSCRIPTION_ENABLED="false", TRANSCRIPTION_URL="ftp://bad")
        self.assertFalse(config.transcription_enabled)

    def test_full_configuration(self):
        config = self._config(
            TRANSCRIPTION_URL=self.URL,
            TRANSCRIPTION_API_KEY=KEY,
            TRANSCRIPTION_PRESET="Best",
            TRANSCRIPTION_TYPES=" voice, Audio ",
            TRANSCRIPTION_MAX_SECONDS="600",
            TRANSCRIPTION_LANGUAGE="es",
            TRANSCRIPTION_CALLBACK_URL="https://viewer.example.test/api/transcriptions/callback",
            TRANSCRIPTION_WEBHOOK_SECRET=SECRET,
            TRANSCRIPTION_BACKFILL_PER_RUN="7",
        )
        self.assertTrue(config.transcription_enabled)
        self.assertEqual(config.transcription_url, self.URL)
        self.assertEqual(config.transcription_api_key, KEY)
        self.assertEqual(config.transcription_preset, "best")
        self.assertEqual(config.transcription_types, {"voice", "audio"})
        self.assertEqual(config.transcription_max_seconds, 600)
        self.assertEqual(config.transcription_language, "es")
        self.assertEqual(config.transcription_callback_url, "https://viewer.example.test/api/transcriptions/callback")
        self.assertEqual(config.transcription_webhook_secret, SECRET)
        self.assertEqual(config.transcription_backfill_per_run, 7)

    def test_bad_url_disables_without_echoing_value(self):
        for url in ("ftp://files.example.test/x", "https://", "https:///path", "akou.example.test"):
            with self.assertLogs("src.config", level="WARNING") as logs:
                config = self._config(TRANSCRIPTION_URL=url)
            self.assertFalse(config.transcription_enabled)
            joined = "\n".join(logs.output)
            self.assertIn("TRANSCRIPTION_URL", joined)
            self.assertNotIn("example.test", joined)

    def test_a_malformed_bracketed_url_degrades_instead_of_aborting(self):
        """urlparse raises on an unclosed IPv6 bracket; the warning paths still apply."""
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(TRANSCRIPTION_URL="https://[akou.example.test")
        self.assertFalse(config.transcription_enabled)
        self.assertEqual(config.transcription_url, "")
        self.assertTrue(any("TRANSCRIPTION_URL" in line for line in logs.output))

        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(
                TRANSCRIPTION_URL=self.URL, TRANSCRIPTION_CALLBACK_URL="https://[viewer.example.test/cb"
            )
        self.assertTrue(config.transcription_enabled)
        self.assertEqual(config.transcription_callback_url, "")
        joined = "\n".join(logs.output)
        self.assertIn("TRANSCRIPTION_CALLBACK_URL", joined)
        self.assertNotIn("example.test", joined)

    def test_disabled_ignores_a_typo_in_its_numeric_settings(self):
        """A setting of a feature the operator turned off must not stop the archiver."""
        config = self._config(
            TRANSCRIPTION_ENABLED="false", TRANSCRIPTION_MAX_SECONDS="30m", TRANSCRIPTION_BACKFILL_PER_RUN="lots"
        )
        self.assertFalse(config.transcription_enabled)
        self.assertEqual(config.transcription_max_seconds, 1800)
        self.assertEqual(config.transcription_backfill_per_run, 50)
        # On, the same typo still fails by name, like every other numeric setting.
        with self.assertRaisesRegex(ValueError, "TRANSCRIPTION_MAX_SECONDS"):
            self._config(TRANSCRIPTION_MAX_SECONDS="30m")

    def test_bad_preset_falls_back_to_auto(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(TRANSCRIPTION_PRESET="turbo")
        self.assertEqual(config.transcription_preset, "auto")
        self.assertTrue(any("TRANSCRIPTION_PRESET" in line for line in logs.output))

    def test_unknown_types_are_dropped_with_a_warning(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(TRANSCRIPTION_TYPES="voice,sticker")
        self.assertEqual(config.transcription_types, {"voice"})
        self.assertTrue(any("TRANSCRIPTION_TYPES" in line for line in logs.output))

    def test_only_unknown_types_keeps_the_default(self):
        with self.assertLogs("src.config", level="WARNING"):
            config = self._config(TRANSCRIPTION_TYPES="sticker")
        self.assertEqual(config.transcription_types, {"voice", "video_note", "audio", "video", "document"})

    def test_document_is_a_type_name_and_animation_is_not(self):
        """``document`` means an audio or video file sent as a document; GIF-style clips have no sound."""
        config = self._config(TRANSCRIPTION_TYPES="Document, voice")
        self.assertEqual(config.transcription_types, {"document", "voice"})
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(TRANSCRIPTION_TYPES="document,animation")
        self.assertEqual(config.transcription_types, {"document"})
        self.assertTrue(any("video and document" in line for line in logs.output))

    def test_bad_callback_url_drops_the_callback_and_keeps_the_feature(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(
                TRANSCRIPTION_URL=self.URL, TRANSCRIPTION_CALLBACK_URL="not a url", TRANSCRIPTION_WEBHOOK_SECRET=SECRET
            )
        self.assertTrue(config.transcription_enabled)
        self.assertEqual(config.transcription_callback_url, "")
        self.assertTrue(any("TRANSCRIPTION_CALLBACK_URL" in line for line in logs.output))

    def test_callback_url_without_secret_is_the_backup_process(self):
        """The backup sends the URL, the viewer holds the secret: no warning, URL kept."""
        with self.assertNoLogs("src.config", level="WARNING"):
            config = self._config(
                TRANSCRIPTION_URL=self.URL, TRANSCRIPTION_CALLBACK_URL="https://viewer.example.test/cb"
            )
        self.assertTrue(config.transcription_enabled)
        self.assertEqual(config.transcription_callback_url, "https://viewer.example.test/cb")
        self.assertEqual(config.transcription_webhook_secret, "")

    def test_secret_must_start_with_whsec(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(
                TRANSCRIPTION_URL=self.URL,
                TRANSCRIPTION_CALLBACK_URL="https://viewer.example.test/cb",
                TRANSCRIPTION_WEBHOOK_SECRET="plain-" + KEY,
            )
        self.assertEqual(config.transcription_webhook_secret, "")
        self.assertEqual(config.transcription_callback_url, "https://viewer.example.test/cb")  # independent
        joined = "\n".join(logs.output)
        self.assertIn("TRANSCRIPTION_WEBHOOK_SECRET", joined)
        self.assertNotIn(KEY, joined)

    def test_backfill_per_run_is_at_least_one(self):
        config = self._config(TRANSCRIPTION_BACKFILL_PER_RUN="0")
        self.assertEqual(config.transcription_backfill_per_run, 1)

    def test_log_summary_prints_scheme_and_host_only(self):
        config = self._config(
            TRANSCRIPTION_URL=self.URL,
            TRANSCRIPTION_API_KEY=KEY,
            TRANSCRIPTION_TYPES="voice,video_note",
            TRANSCRIPTION_CALLBACK_URL="https://viewer.example.test/cb",
            TRANSCRIPTION_WEBHOOK_SECRET=SECRET,
        )
        with self.assertLogs("src.config", level="INFO") as logs:
            config.log_summary()
        lines = [line for line in logs.output if "TRANSCRIPTION" in line]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("https://akou.example.test", line)
        self.assertIn("preset: auto", line)
        self.assertIn("video_note, voice", line)
        self.assertNotIn("8443", line)
        self.assertNotIn("/base/path", line)
        self.assertNotIn("token=", line)
        self.assertNotIn(KEY, line)
        self.assertNotIn(SECRET, line)

    def test_log_summary_names_the_nudge_state(self):
        config = self._config()
        with self.assertLogs("src.config", level="INFO") as logs:
            config.log_summary()
        self.assertTrue(any("TRANSCRIPTION enabled" in line and "no server configured" in line for line in logs.output))

    def test_log_summary_when_disabled(self):
        config = self._config(TRANSCRIPTION_ENABLED="false")
        with self.assertLogs("src.config", level="INFO") as logs:
            config.log_summary()
        self.assertTrue(any("TRANSCRIPTION disabled" in line for line in logs.output))

    def test_setup_logging_silences_httpx_request_lines(self):
        """httpx logs every request URL at INFO; the server URL must not reach the log."""
        httpx_logger = logging.getLogger("httpx")
        previous = httpx_logger.level
        try:
            httpx_logger.setLevel(logging.NOTSET)
            setup_logging(self._config())
            self.assertEqual(httpx_logger.level, logging.WARNING)
        finally:
            httpx_logger.setLevel(previous)
