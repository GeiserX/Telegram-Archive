"""The exception-text leak route, and the helper that closes it.

`OSError` stringifies with the offending filename. Media paths carry the
chat-id folder, so `logger.error(f"...: {e}")` on a filesystem operation put the
chat id back into a log line that had just been redacted. That is how a leak
shipped in v7.33.4's predecessor: `message_utils.py` line 443 kept `{e}` while
its sibling at 389 was fixed, because a replace-all edit matched only one
indentation.

`describe_exception` keeps the message wherever it cannot contain a path,
because that is where the diagnostic value is — a FloodWaitError's wait, an RPC
error's reason — and drops it only for `OSError`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.message_utils import describe_exception


class TestDescribeException(unittest.TestCase):
    def test_an_oserror_never_reveals_its_path(self) -> None:
        """The whole point: the path is the leak."""
        try:
            os.rmdir("/nonexistent/-1001234567890/media")
        except OSError as exc:
            self.assertIn("-1001234567890", str(exc), "precondition: OSError does embed the path")
            described = describe_exception(exc)
            self.assertNotIn("-1001234567890", described)
            self.assertNotIn("/nonexistent", described)
            self.assertEqual("FileNotFoundError", described)
        else:
            self.fail("expected an OSError")

    def test_every_oserror_subclass_is_covered(self) -> None:
        """PermissionError, IsADirectoryError and friends all carry filename."""
        for exc in (
            PermissionError(13, "Permission denied", "/data/media/-100999/x.jpg"),
            IsADirectoryError(21, "Is a directory", "/data/media/-100999"),
            FileExistsError(17, "File exists", "/data/media/-100999/y.jpg"),
        ):
            described = describe_exception(exc)
            self.assertNotIn("-100999", described)
            self.assertEqual(type(exc).__name__, described)

    def test_a_subprocess_error_carrying_a_command_is_reduced(self) -> None:
        """Not an OSError, but its str() prints the whole argv.

        ``TimeoutExpired``/``CalledProcessError`` expose ``cmd``, and the ffmpeg
        invocation contains the media path. A type check on OSError alone let
        this straight through, which is why the rule keys on the attributes that
        make a message carry a path rather than on a list of types.
        """
        import subprocess

        for exc in (
            subprocess.TimeoutExpired(["ffmpeg", "-i", "/data/media/-1001234/v.mp4"], 15),
            subprocess.CalledProcessError(1, ["ffmpeg", "-i", "/data/media/-1001234/v.mp4"]),
        ):
            self.assertIn("-1001234", str(exc), "precondition: the raw str does leak")
            described = describe_exception(exc)
            self.assertNotIn("-1001234", described)
            self.assertEqual(type(exc).__name__, described)

    def test_a_non_oserror_keeps_its_message(self) -> None:
        """Stripping every exception would cost debuggability for no privacy gain."""
        described = describe_exception(ValueError("wait 30 seconds before retrying"))
        self.assertIn("wait 30 seconds", described)
        self.assertIn("ValueError", described)

    def test_the_type_is_always_present(self) -> None:
        """Whatever else it drops, the caller can still tell what went wrong."""
        for exc in (OSError("boom"), RuntimeError("boom"), TimeoutError("boom")):
            self.assertIn(type(exc).__name__, describe_exception(exc))

    def test_a_bare_oserror_without_a_filename_is_still_reduced(self) -> None:
        """Not every OSError carries a path, but the rule stays simple."""
        self.assertEqual("OSError", describe_exception(OSError("no filename here")))


if __name__ == "__main__":
    unittest.main()
