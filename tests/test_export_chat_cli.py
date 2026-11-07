# ``viewer`` is the fixture imported below, taken as an argument.
# ruff: noqa: F811
"""``telegram-archive export-chat``: one chat, the same file as the viewer's Export chat.

The command writes the viewer's JSON byte for byte and the viewer's HTML ZIP
with the same entries and pages, from the same archive, without the web
viewer running and without FastAPI imported. Runs on SQLite and PostgreSQL
(``real_adapter``) against the fixture the viewer's own export tests use.
"""

import io
import os
import re
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("bs4")

from test_export_html_endpoint import (  # noqa: E402
    ALL_MEDIA,
    CHAT,
    OTHER_CHAT,
    _client,
    viewer,  # noqa: F401 - the fixture
)

from telegram_archive import __main__ as cli  # noqa: E402
from telegram_archive.web import main as web_main  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _args(*argv: str):
    return cli.create_parser().parse_args(["export-chat", *argv])


async def _run(real_adapter, tmp_path, *argv: str) -> int:
    return await cli.export_chat_to_file(
        real_adapter,
        _args(*argv),
        media_root=(tmp_path / "media").resolve(),
        tz=ZoneInfo(web_main.config.viewer_timezone),
    )


async def _viewer_bytes(ref: str, params: dict) -> bytes:
    async with _client() as client:
        resp = await client.get(f"/api/chats/{ref}/export", params=params)
    assert resp.status_code == 200, resp.text
    return resp.content


def _entries(data: bytes) -> dict[str, bytes]:
    """Every entry of a ZIP, with the export time on the index page masked."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    entries = {}
    for name in archive.namelist():
        body = archive.read(name)
        if name.endswith("/index.html"):
            body = re.sub(rb"<p>Exported \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC ", b"<p>Exported @TIME@ ", body)
        entries[name] = body
    return entries


class TestTheArguments:
    def test_defaults(self):
        args = _args("--chat", "@fixture")
        assert args.export_format == "json"
        assert (args.media, args.max_file_mb, args.no_players, args.output, args.force) == (
            None,
            None,
            False,
            None,
            False,
        )

    @pytest.mark.parametrize("argv", [[], ["--chat", "x", "--format", "pdf"], ["--chat", "x", "--account", "one"]])
    def test_argparse_refuses_with_exit_code_2(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            _args(*argv)
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        ("argv", "params"),
        [
            (["--media", "photo"], {"media": "photo"}),
            (["--no-players"], {"players": "false"}),
            (["--max-file-mb", "50"], {"max_file_mb": "50"}),
            (["--format", "html", "--media", "foo"], {"format": "html", "media": "foo"}),
            (["--format", "html", "--max-file-mb", "0"], {"format": "html", "max_file_mb": "0"}),
            (["--format", "html", "--max-file-mb", "3901"], {"format": "html", "max_file_mb": "3901"}),
            (["--from", "yesterday"], {"from": "yesterday"}),
            (["--to", "2026-13-01"], {"to": "2026-13-01"}),
            (["--from", "2026-03-02", "--to", "2026-03-01"], {"from": "2026-03-02", "to": "2026-03-01"}),
            (["--changed-since", "soon"], {"changed_since": "soon"}),
        ],
    )
    async def test_refused_with_the_viewers_own_words(
        self, viewer, real_adapter, tmp_path, monkeypatch, capsys, argv, params
    ):
        """Exit 2, nothing written, and the text the viewer's 400 carries for the same request."""
        monkeypatch.chdir(tmp_path)
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", *argv) == 2
        assert not list(tmp_path.glob("*_export.*"))
        async with _client() as client:
            resp = await client.get(f"/api/chats/{viewer[CHAT]}/export", params=params)
        assert resp.status_code == 400
        assert capsys.readouterr().err == f"Export refused: {resp.json()['detail']}\n"


class TestTheFile:
    @pytest.mark.parametrize(
        ("argv", "params"),
        [
            ([], {}),
            (
                ["--from", "2026-03-01", "--to", "2026-03-01T09:15:00", "--changed-since", "2026-03-01"],
                {"from": "2026-03-01", "to": "2026-03-01T09:15:00", "changed_since": "2026-03-01"},
            ),
        ],
    )
    async def test_json_is_the_viewers_bytes(self, viewer, real_adapter, tmp_path, argv, params):
        out = tmp_path / "out.json"
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out), *argv) == 0
        assert out.read_bytes() == await _viewer_bytes(viewer[CHAT], params)

    @pytest.mark.parametrize(
        ("argv", "params"),
        [
            ([], {}),
            (["--media", ALL_MEDIA], {"media": ALL_MEDIA}),
            (
                ["--media", ALL_MEDIA, "--max-file-mb", "1", "--no-players", "--from", "2026-03-01"],
                {"media": ALL_MEDIA, "max_file_mb": "1", "players": "false", "from": "2026-03-01"},
            ),
        ],
    )
    async def test_html_is_the_viewers_zip(self, viewer, real_adapter, tmp_path, monkeypatch, argv, params):
        # The viewer's fixture keeps its thumbnails here; the command finds the same cache.
        monkeypatch.setenv("THUMBNAIL_CACHE_DIR", str(web_main._thumb_cache_dir))
        out = tmp_path / "out.zip"
        assert (
            await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "--format", "html", "-o", str(out), *argv) == 0
        )
        mine = _entries(out.read_bytes())
        theirs = _entries(await _viewer_bytes(viewer[CHAT], {"format": "html", **params}))
        assert list(mine) == list(theirs)
        for name in theirs:
            assert mine[name] == theirs[name], name

    async def test_default_name_is_the_viewers_download_name(self, viewer, real_adapter, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}") == 0
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "--format", "html") == 0
        assert (tmp_path / "Fixture Export Chat_export.json").is_file()
        assert (tmp_path / "Fixture Export Chat_export.zip").is_file()
        err = capsys.readouterr().err
        assert "Exported to " in err and str(CHAT) not in err

    async def test_the_chat_by_id_ref_or_username(self, viewer, real_adapter, tmp_path):
        await real_adapter.upsert_chat({"id": CHAT, "username": "Fixture_Chat"}, account_id=1)
        files = []
        for name in (str(CHAT), viewer[CHAT], "@fixture_chat"):
            out = tmp_path / f"{len(files)}.json"
            assert await _run(real_adapter, tmp_path, f"--chat={name}", "-o", str(out)) == 0, name
            files.append(out.read_bytes())
        assert files[0] == files[1] == files[2]
        assert b"other chat text" not in files[0]

    async def test_json_to_standard_output(self, viewer, real_adapter, tmp_path, capsysbinary):
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[OTHER_CHAT]}", "-o", "-") == 0
        assert capsysbinary.readouterr().out == await _viewer_bytes(viewer[OTHER_CHAT], {})

    async def test_html_never_to_standard_output(self, viewer, real_adapter, tmp_path, capsysbinary):
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "--format", "html", "-o", "-") == 2
        assert capsysbinary.readouterr().out == b""


class TestRefusals:
    async def test_an_existing_file_is_kept_unless_forced(self, viewer, real_adapter, tmp_path, capsys):
        out = tmp_path / "out.json"
        out.write_bytes(b"keep me")
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out)) == 2
        assert out.read_bytes() == b"keep me"
        assert "--force" in capsys.readouterr().err
        assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out), "--force") == 0
        assert out.read_bytes() == await _viewer_bytes(viewer[CHAT], {})

    @pytest.mark.parametrize("name", ["-1009900000999", "@nobody_here", "AAAAAAAAAAAAAAAAAAAAAA", "not a chat"])
    async def test_an_unknown_chat_is_exit_code_1(self, viewer, real_adapter, tmp_path, monkeypatch, capsys, name):
        monkeypatch.chdir(tmp_path)
        assert await _run(real_adapter, tmp_path, f"--chat={name}") == 1
        assert capsys.readouterr().err == "Chat not found.\n"
        assert not list(tmp_path.glob("*_export.*"))

    async def test_a_chat_two_accounts_hold_needs_the_account(
        self, viewer, real_adapter, tmp_path, monkeypatch, capsys
    ):
        await real_adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Fixture Copy"}, account_id=2)
        monkeypatch.chdir(tmp_path)
        assert await _run(real_adapter, tmp_path, f"--chat={CHAT}") == 2
        assert "--account" in capsys.readouterr().err
        out = tmp_path / "one.json"
        assert await _run(real_adapter, tmp_path, f"--chat={CHAT}", "--account", "1", "-o", str(out)) == 0
        assert out.read_bytes() == await _viewer_bytes(viewer[CHAT], {})

    async def test_a_username_two_chats_of_one_account_carry_names_the_way_out(
        self, viewer, real_adapter, tmp_path, monkeypatch, capsys
    ):
        """--account cannot pick between two chats of the same account; the refusal says what can."""
        await real_adapter.upsert_chat({"id": CHAT, "username": "Fixture_Chat"}, account_id=1)
        await real_adapter.upsert_chat({"id": OTHER_CHAT, "username": "fixture_chat"}, account_id=1)
        monkeypatch.chdir(tmp_path)
        for argv in ([], ["--account", "1"]):
            assert await _run(real_adapter, tmp_path, "--chat=@fixture_chat", *argv) == 2
            err = capsys.readouterr().err
            assert err == "Export refused: more than one chat holds that username; pass the chat id or ref.\n"
        assert not list(tmp_path.glob("*_export.*"))

    @pytest.mark.parametrize("export_format", ["json", "html"])
    @pytest.mark.parametrize("force", [False, True])
    async def test_a_failed_export_leaves_no_partial_file_and_keeps_the_old_one(
        self, viewer, real_adapter, tmp_path, export_format, force
    ):
        out = tmp_path / "out.bin"
        if force:
            out.write_bytes(b"the previous export")
        argv = [f"--chat={viewer[CHAT]}", "--format", export_format, "-o", str(out)] + (["--force"] if force else [])
        with (
            patch.object(real_adapter, "get_messages_for_export", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            await _run(real_adapter, tmp_path, *argv)
        if force:
            assert out.read_bytes() == b"the previous export"
        else:
            assert not out.exists()
        assert not list(tmp_path.glob(".*.part"))

    async def test_a_file_that_appears_during_the_export_is_never_overwritten(
        self, viewer, real_adapter, tmp_path, capsys
    ):
        out = tmp_path / "out.json"
        real_link = os.link

        def someone_else_writes_first(src, dst):
            Path(dst).write_bytes(b"theirs")
            return real_link(src, dst)

        with patch.object(cli.os, "link", someone_else_writes_first):
            assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out)) == 2
        assert out.read_bytes() == b"theirs"
        assert "--force" in capsys.readouterr().err
        assert not list(tmp_path.glob(".*.part"))

    async def test_a_filesystem_without_hard_links_still_gets_the_file(self, viewer, real_adapter, tmp_path):
        out = tmp_path / "out.json"
        with patch.object(cli.os, "link", side_effect=PermissionError("no hard links here")):
            assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out)) == 0
        assert out.read_bytes() == await _viewer_bytes(viewer[CHAT], {})
        assert not list(tmp_path.glob(".*.part"))

    async def test_without_hard_links_a_file_that_appears_is_still_kept(self, viewer, real_adapter, tmp_path, capsys):
        out = tmp_path / "out.json"

        def no_links_and_someone_else_writes(src, dst):
            Path(dst).write_bytes(b"theirs")
            raise PermissionError("no hard links here")

        with patch.object(cli.os, "link", no_links_and_someone_else_writes):
            assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out)) == 2
        assert out.read_bytes() == b"theirs"
        assert "--force" in capsys.readouterr().err
        assert not list(tmp_path.glob(".*.part"))

    async def test_the_file_gets_the_mode_a_plain_open_gives(self, viewer, real_adapter, tmp_path):
        umask = os.umask(0o022)
        try:
            out = tmp_path / "out.json"
            assert await _run(real_adapter, tmp_path, f"--chat={viewer[CHAT]}", "-o", str(out)) == 0
        finally:
            os.umask(umask)
        assert out.stat().st_mode & 0o777 == 0o644


class TestTheEntryPoint:
    def test_main_dispatches_and_a_failure_names_only_the_type(self, capsys):
        with (
            patch.object(sys, "argv", ["telegram-archive", "export-chat", "--chat", "@x"]),
            patch("telegram_archive.db.create_adapter", AsyncMock(side_effect=OSError("/private/path"))),
        ):
            assert cli.main() == 1
        err = capsys.readouterr().err
        assert "Export failed: OSError" in err and "/private/path" not in err

    def test_runs_without_fastapi(self, tmp_path):
        """A backup-only or pip install: the whole command, HTML included, with fastapi unimportable."""
        script = textwrap.dedent(
            """
            import asyncio, importlib.abc, io, json, sys

            class Block(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in ("fastapi", "starlette", "uvicorn"):
                        raise ImportError(f"blocked: {name}")

            sys.meta_path.insert(0, Block())
            try:
                import fastapi  # the positive control: the block works
                raise SystemExit("fastapi imported")
            except ImportError:
                pass

            from telegram_archive.db import close_database, create_adapter

            async def seed():
                db = await create_adapter()
                await db.upsert_chat({"id": -1009900000001, "type": "group", "title": "No Viewer Chat"}, account_id=1)
                await close_database()

            asyncio.run(seed())
            from telegram_archive.__main__ import main

            sys.argv = ["telegram-archive", "export-chat", "--chat", "-1009900000001", "--format", "html"]
            code = main()
            sys.argv = ["telegram-archive", "export-chat", "--chat", "-1009900000001"]
            code = code or main()
            # The real entry point to standard output: nothing but the JSON may land there.
            stdout, sys.stdout = sys.stdout, io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
            sys.argv = ["telegram-archive", "export-chat", "--chat", "-1009900000001", "-o", "-"]
            code = code or main()
            sys.stdout.flush()
            piped = sys.stdout.buffer.getvalue().decode("utf-8")
            sys.stdout = stdout
            assert piped.startswith('{\\n  "chat": '), piped[:200]
            assert json.loads(piped)["chat"]["title"] == "No Viewer Chat"
            print(json.dumps({"code": code, "fastapi": sorted(m for m in sys.modules if m.startswith(("fastapi", "starlette")))}))
            """
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("DATABASE_URL", "DB_TYPE", "DATABASE_PATH", "DATABASE_DIR")
        }
        env |= {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "DB_PATH": str(tmp_path / "archive.db"),
            "PYTHONPATH": str(REPO),
            "DOWNLOAD_MEDIA": "true",
        }
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == '{"code": 0, "fastapi": []}'
        archive = zipfile.ZipFile(tmp_path / "No Viewer Chat_export.zip")
        assert "ChatExport_No Viewer Chat/index.html" in archive.namelist()
        assert (tmp_path / "No Viewer Chat_export.json").read_text(encoding="utf-8").startswith('{\n  "chat": ')
