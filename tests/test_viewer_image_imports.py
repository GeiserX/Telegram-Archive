"""The viewer app imports from exactly what Dockerfile.viewer copies and installs.

The suite runs from an editable install, which puts all of src/ on the path,
so a module the image never copies, or a package its dependency group never
installs, imports fine here and crashes the container at start. This test
rebuilds the image's view: a directory holding only the Dockerfile's COPY
set, ``python -S`` so no ``.pth`` file adds the worktree back, the installed
packages added by hand, and every distribution outside the closure of the
``viewer-runtime`` group in uv.lock refused at import.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHILD = """
import importlib.abc
import json
import sys

app_dir, site_dirs, blocked = json.loads(sys.argv[1])
sys.path.insert(0, app_dir)
sys.path.extend(site_dirs)
blocked = set(blocked)


class NotInTheImage(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"No module named {name!r} in the viewer image", name=name)
        return None


sys.meta_path.insert(0, NotInTheImage())
import src.web.main  # noqa: E402,F401

print("imported")
"""


def _copied_src_paths() -> list[str]:
    """The ``COPY src/...`` sources of Dockerfile.viewer."""
    lines = (ROOT / "Dockerfile.viewer").read_text(encoding="utf-8").splitlines()
    return [match.group(1) for line in lines if (match := re.match(r"COPY (src/\S*)\s", line))]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _viewer_group_closure() -> set[str]:
    """Every distribution ``uv sync --only-group viewer-runtime`` installs, markers ignored."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {_norm(package["name"]): package for package in lock["package"]}
    project = next(p for p in lock["package"] if p.get("source", {}).get("editable") == ".")
    pending = [
        (entry["name"], tuple(entry.get("extra", ()))) for entry in project["dev-dependencies"]["viewer-runtime"]
    ]
    seen: set[tuple[str, str]] = set()
    closure: set[str] = set()
    while pending:
        name, extras = pending.pop()
        key = _norm(name)
        package = packages[key]
        for part in ("", *extras):
            if (key, part) in seen:
                continue
            seen.add((key, part))
            closure.add(key)
            entries = package.get("dependencies", []) if not part else package["optional-dependencies"][part]
            pending.extend((entry["name"], tuple(entry.get("extra", ()))) for entry in entries)
    return closure


def _modules_outside(closure: set[str]) -> list[str]:
    """Top-level modules installed here whose every distribution the image lacks."""
    blocked = []
    for module, dists in metadata.packages_distributions().items():
        if module == "src":
            continue  # the project itself, copied file by file instead
        if not any(_norm(dist) in closure for dist in dists):
            blocked.append(module)
    return sorted(blocked)


def test_the_viewer_app_imports_from_the_image_copy_set(tmp_path):
    app_dir = tmp_path / "app"
    for source in _copied_src_paths():
        origin = ROOT / source
        target = app_dir / source
        if origin.is_dir():
            shutil.copytree(origin, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    closure = _viewer_group_closure()
    assert "fastapi" in closure and "httpx" not in closure, "the closure is the viewer group, not the project"
    blocked = _modules_outside(closure)
    assert "httpx" in blocked and "telethon" in blocked

    site_dirs = sorted({sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]})
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["BACKUP_PATH"] = str(tmp_path / "backups")
    result = subprocess.run(
        [sys.executable, "-S", "-c", CHILD, json.dumps([str(app_dir), site_dirs, blocked])],
        cwd=app_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0 and "imported" in result.stdout, result.stderr[-3000:]
