"""The info panel's Open buttons run an operator-written command on the server (#433).

A browser cannot open a file on the machine that serves it, so the feature only
means anything where the viewer and the browser share that machine. That shapes
every rule here: nothing runs unless the operator wrote the command, only the
master account can trigger it, the request is a POST (a GET with a side effect
is one <img src> away from cross-site abuse), and every substituted value is one
shell word, filled in a single pass so a file name that spells a placeholder is
never expanded twice.
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from httpx import ASGITransport, AsyncClient

    _HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _HTTPX_AVAILABLE = False

try:
    from src.web import main as web_main

    _WEB_AVAILABLE = True
except Exception:  # pragma: no cover
    web_main = None
    _WEB_AVAILABLE = False


def _skip_unless_web(cls_or_fn):
    return unittest.skipUnless(_WEB_AVAILABLE and _HTTPX_AVAILABLE, "web_main or httpx import failed")(cls_or_fn)


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


@_skip_unless_web
class TestRenderMediaCommand(unittest.TestCase):
    def test_posix_values_are_single_shell_words(self):
        with patch.object(web_main.platform, "system", return_value="Linux"):
            command = web_main._render_media_command(
                "viewer %PATH% ; cd %DIR% && echo %FILENAME%",
                Path("/data/media/-100123/dir with space/it's $(evil) `x`.jpg"),
            )
        self.assertEqual(
            command,
            "viewer '/data/media/-100123/dir with space/it'\"'\"'s $(evil) `x`.jpg' ; "
            "cd '/data/media/-100123/dir with space' && "
            "echo 'it'\"'\"'s $(evil) `x`.jpg'",
        )

    def test_substitution_is_single_pass(self):
        """A file literally named %DIR%.jpg must reach the command as that name, not as the directory."""
        with patch.object(web_main.platform, "system", return_value="Linux"):
            command = web_main._render_media_command("open %PATH% %FILENAME%", Path("/root/%DIR%.jpg"))
        self.assertEqual(command, "open /root/%DIR%.jpg %DIR%.jpg")  # shlex leaves a '%' bare; the pass is single

    def test_unknown_placeholders_and_literal_percent_signs_are_left_alone(self):
        with patch.object(web_main.platform, "system", return_value="Linux"):
            command = web_main._render_media_command("run %FOO% 100%% %PATH%", Path("/root/a.jpg"))
        self.assertEqual(command, "run %FOO% 100%% /root/a.jpg")

    def test_windows_values_are_quoted_for_createprocess(self):
        with patch.object(web_main.platform, "system", return_value="Windows"):
            command = web_main._render_media_command(
                '"C:\\Program Files\\IrfanView\\i_view64.exe" %PATH%', Path("C:/media/dir with space/a b.jpg")
            )
        # Path renders with the host's separator; the quoting is what is under test.
        self.assertEqual(command, '"C:\\Program Files\\IrfanView\\i_view64.exe" "C:/media/dir with space/a b.jpg"')

    def test_windows_refuses_a_percent_sign_in_the_name(self):
        """cmd.exe expands %NAME% inside quotes and has no escape for it: refuse rather than guess."""
        with (
            patch.object(web_main.platform, "system", return_value="Windows"),
            self.assertRaises(ValueError),
        ):
            web_main._render_media_command("open %PATH%", Path("C:/media/%COMSPEC%.jpg"))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class _OpenEndpointBase(unittest.IsolatedAsyncioTestCase):
    """A master session, an entitled media row on disk under a temp media root, and no configured command."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_root = Path(self.temp_dir).resolve()
        (self.media_root / "-100123").mkdir()
        self.file = self.media_root / "-100123" / "12_photo it's %COMSPEC%.jpg"
        self.file.write_bytes(b"x")

        self._saved = {
            name: getattr(web_main, name)
            for name in ("db", "AUTH_ENABLED", "ALLOW_ANONYMOUS_VIEWER", "push_manager", "_media_root")
        }
        self._saved_cmds = (web_main.config.media_open_cmd, web_main.config.media_open_path_cmd)
        self._saved_display = web_main.config.display_chat_ids

        self.mock_db = AsyncMock()
        self.mock_db.get_chat_by_ref = AsyncMock(
            side_effect=lambda ref, **kwargs: {"id": -100123, "account_id": 1, "ref": ref, "type": "group"}
        )
        self.mock_db.get_media_for_message = AsyncMock(return_value={"file_path": str(self.file)})
        web_main.db = self.mock_db
        web_main.AUTH_ENABLED = False
        web_main.ALLOW_ANONYMOUS_VIEWER = True
        web_main.push_manager = None
        web_main._media_root = self.media_root
        web_main.config.display_chat_ids = set()
        web_main.config.media_open_cmd = ""
        web_main.config.media_open_path_cmd = ""
        web_main._sessions.clear()
        web_main.app.dependency_overrides[web_main.require_master] = lambda: web_main.UserContext(
            username="admin-test", role="master"
        )
        self.spawn = AsyncMock(return_value=MagicMock())
        self._spawn_patch = patch.object(web_main.asyncio, "create_subprocess_shell", self.spawn)
        self._spawn_patch.start()
        self._platform_patch = patch.object(web_main.platform, "system", return_value="Linux")
        self._platform_patch.start()

    def tearDown(self):
        self._spawn_patch.stop()
        self._platform_patch.stop()
        web_main.app.dependency_overrides.pop(web_main.require_master, None)
        for name, value in self._saved.items():
            setattr(web_main, name, value)
        web_main.config.media_open_cmd, web_main.config.media_open_path_cmd = self._saved_cmds
        web_main.config.display_chat_ids = self._saved_display
        web_main._sessions.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


@_skip_unless_web
class TestOpenEndpoints(_OpenEndpointBase):
    async def test_nothing_is_reachable_until_the_operator_configures_a_command(self):
        async with self._client() as client:
            for route in ("/media/open/c1/12_photo", "/media/open-path/c1/12_photo"):
                resp = await client.post(route)
                self.assertEqual(resp.status_code, 404, route)
        self.spawn.assert_not_called()

    async def test_each_button_answers_to_its_own_variable(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        async with self._client() as client:
            self.assertEqual((await client.post("/media/open/c1/12_photo")).status_code, 200)
            self.assertEqual((await client.post("/media/open-path/c1/12_photo")).status_code, 404)
        self.spawn.assert_awaited_once()

    async def test_a_get_is_not_a_way_to_run_the_command(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        async with self._client() as client:
            resp = await client.get("/media/open/c1/12_photo")
        self.assertEqual(resp.status_code, 405)
        self.spawn.assert_not_called()

    async def test_the_command_runs_detached_with_the_entitled_file_substituted(self):
        web_main.config.media_open_path_cmd = "filer %DIR% --select %FILENAME%"
        async with self._client() as client:
            resp = await client.post("/media/open-path/c1/12_photo")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.mock_db.get_media_for_message.assert_awaited_once_with(-100123, 12, "photo", account_id=1)
        args, kwargs = self.spawn.await_args
        self.assertEqual(
            args[0],
            f"filer {web_main.shlex.quote(str(self.file.parent))} --select {web_main.shlex.quote(self.file.name)}",
        )
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(
            (kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]),
            (asyncio.subprocess.DEVNULL, asyncio.subprocess.DEVNULL, asyncio.subprocess.DEVNULL),
        )

    async def test_only_the_master_account_can_press_the_button(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        web_main.app.dependency_overrides.pop(web_main.require_master, None)  # back to the anonymous viewer
        async with self._client() as client:
            resp = await client.post("/media/open/c1/12_photo")
        self.assertEqual(resp.status_code, 403)
        self.spawn.assert_not_called()

    async def test_a_file_the_row_does_not_own_is_a_404_not_a_launch(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        self.mock_db.get_media_for_message.return_value = None
        async with self._client() as client:
            resp = await client.post("/media/open/c1/12_photo")
        self.assertEqual(resp.status_code, 404)
        self.spawn.assert_not_called()

    async def test_a_row_whose_file_left_the_media_root_is_a_404_not_a_launch(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        outside = Path(self.temp_dir).parent / "outside.jpg"
        self.mock_db.get_media_for_message.return_value = {"file_path": str(outside)}
        async with self._client() as client:
            resp = await client.post("/media/open/c1/12_photo")
        self.assertEqual(resp.status_code, 404)
        self.spawn.assert_not_called()

    async def test_a_name_the_shell_cannot_take_is_refused_rather_than_mangled(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        with patch.object(web_main.platform, "system", return_value="Windows"):
            async with self._client() as client:
                resp = await client.post("/media/open/c1/12_photo")
        self.assertEqual(resp.status_code, 400)
        self.spawn.assert_not_called()

    async def test_a_command_that_fails_to_start_is_a_500_without_the_path_in_the_log(self):
        web_main.config.media_open_cmd = "viewer %PATH%"
        self.spawn.side_effect = FileNotFoundError(2, "No such file", "/bin/sh")
        with self.assertLogs(web_main.logger, level="WARNING") as captured:
            async with self._client() as client:
                resp = await client.post("/media/open/c1/12_photo")
        self.assertEqual(resp.status_code, 500)
        joined = "\n".join(captured.output)
        self.assertIn("FileNotFoundError", joined)
        self.assertNotIn(str(self.file), joined)
        self.assertNotIn("-100123", joined)


# ---------------------------------------------------------------------------
# What the page learns
# ---------------------------------------------------------------------------


@_skip_unless_web
class TestCapabilityFlags(unittest.TestCase):
    def test_flags_follow_the_configured_commands(self):
        saved = (web_main.config.media_open_cmd, web_main.config.media_open_path_cmd)
        try:
            web_main.config.media_open_cmd, web_main.config.media_open_path_cmd = "", ""
            self.assertEqual(web_main._media_open_capabilities(), {"file": False, "path": False})
            web_main.config.media_open_cmd = "viewer %PATH%"
            self.assertEqual(web_main._media_open_capabilities(), {"file": True, "path": False})
            web_main.config.media_open_path_cmd = "filer %DIR%"
            self.assertEqual(web_main._media_open_capabilities(), {"file": True, "path": True})
        finally:
            web_main.config.media_open_cmd, web_main.config.media_open_path_cmd = saved

    def test_the_shipped_page_declares_the_flags(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html").read_text()
        # assertTrue, not assertIn: a failure must not print the whole page.
        self.assertTrue("__VIEWER_MEDIA_OPEN__" in html, "the template does not declare __VIEWER_MEDIA_OPEN__")


class TestConfig(unittest.TestCase):
    def test_commands_are_read_trimmed_and_default_empty(self):
        from src.config import Config

        base = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_PHONE": "+1"}
        with patch.dict(os.environ, base, clear=True), patch("os.makedirs"):
            config = Config()
            self.assertEqual((config.media_open_cmd, config.media_open_path_cmd), ("", ""))
            self.assertFalse(config.download_chat_description)
        with (
            patch.dict(
                os.environ,
                {
                    **base,
                    "MEDIA_OPEN_CMD": "  viewer %PATH%  ",
                    "MEDIA_OPEN_PATH_CMD": "filer %DIR%",
                    "DOWNLOAD_CHAT_DESCRIPTION": "yes",
                },
                clear=True,
            ),
            patch("os.makedirs"),
        ):
            config = Config()
            self.assertEqual((config.media_open_cmd, config.media_open_path_cmd), ("viewer %PATH%", "filer %DIR%"))
            self.assertTrue(config.download_chat_description)


if __name__ == "__main__":
    unittest.main()
