"""Drive the redacted handlers and assert no path reaches the log.

The guard in test_no_account_pii_in_logs.py is static: it proves no handler
INTERPOLATES a raw exception. These tests are the behavioural half — they make
each handler actually fire and assert the emitted record contains neither the
chat-id folder nor the path.

That distinction matters here. The leak this release fixes was in code that
looked redacted: the message named no identifier, and only the exception text
carried it. A static check alone would not have shown the difference.
"""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CHAT_FOLDER = "-1001234567890"


class TestExceptionLogsHidePaths(unittest.TestCase):
    def assertNoPathLeaked(self, records, needle: str = CHAT_FOLDER) -> None:
        joined = " ".join(r.getMessage() for r in records)
        self.assertNotIn(needle, joined, f"a chat id reached the log: {joined!r}")
        self.assertNotIn("Errno", joined, f"raw OSError text reached the log: {joined!r}")

    def test_migration_marker_failure_hides_the_path(self) -> None:
        """open() on an unwritable path raises OSError naming that path."""
        from src.migrate_shared_media import _write_marker

        with tempfile.TemporaryDirectory() as tmp:
            # A directory cannot be opened for writing -> IsADirectoryError,
            # whose str() carries the full path including the chat folder.
            marker = os.path.join(tmp, CHAT_FOLDER)
            os.makedirs(marker)
            with self.assertLogs("src.migrate_shared_media", level="ERROR") as captured:
                _write_marker(marker)
            self.assertNoPathLeaked(captured.records)
            self.assertIn("IsADirectoryError", " ".join(r.getMessage() for r in captured.records))

    def test_video_thumbnail_failure_hides_the_path(self) -> None:
        """The handler wraps ffmpeg plus Image.open on a chat-scoped path."""
        from src.web import thumbnails

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / CHAT_FOLDER / "clip.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"not a real video")
            dest = Path(tmp) / "out.webp"
            with (
                patch.object(thumbnails.subprocess, "run", side_effect=OSError(2, "No such file", str(source))),
                self.assertLogs("src.web.thumbnails", level="WARNING") as captured,
            ):
                result = thumbnails._generate_video_sync(source, dest, 200)
            self.assertFalse(result)
            self.assertNoPathLeaked(captured.records)

    def test_a_non_oserror_keeps_its_detail_in_the_same_handler(self) -> None:
        """The redaction must not blind the operator to real failures."""
        from src.web import thumbnails

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / CHAT_FOLDER / "clip.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"not a real video")
            dest = Path(tmp) / "out.webp"
            with (
                patch.object(thumbnails.subprocess, "run", side_effect=RuntimeError("ffmpeg exploded")),
                self.assertLogs("src.web.thumbnails", level="WARNING") as captured,
            ):
                thumbnails._generate_video_sync(source, dest, 200)
            joined = " ".join(r.getMessage() for r in captured.records)
            self.assertIn("ffmpeg exploded", joined)
            self.assertIn("RuntimeError", joined)
            self.assertNotIn(CHAT_FOLDER, joined)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
