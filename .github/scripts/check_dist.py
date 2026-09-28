"""Fail unless dist/ holds a wheel and an sdist that ship exactly the package.

Usage: python3 .github/scripts/check_dist.py dist

The wheel must have one top-level package, telegram_archive, with its
templates, static files, alembic.ini, script.py.mako and every migration in
the repository. The sdist must carry no src/, tests/ or scripts/.
"""

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "telegram_archive"


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def check_wheel(path: Path) -> None:
    names = set(zipfile.ZipFile(path).namelist())
    tops = {name.split("/", 1)[0] for name in names}
    tops = {top for top in tops if not top.endswith(".dist-info")}
    if tops != {"telegram_archive"}:
        fail(f"wheel top-level entries must be only telegram_archive, got {sorted(tops)}")
    if any("__pycache__" in name for name in names):
        fail("wheel ships __pycache__")

    expected = [
        "telegram_archive/__main__.py",
        "telegram_archive/alembic.ini",
        "telegram_archive/alembic/env.py",
        "telegram_archive/alembic/script.py.mako",
        "telegram_archive/web/templates/index.html",
    ]
    for relative in ("alembic/versions", "web/static"):
        for file in (PACKAGE / relative).rglob("*"):
            if file.is_file() and "__pycache__" not in file.parts:
                expected.append(file.relative_to(ROOT).as_posix())
    missing = [name for name in expected if name not in names]
    if missing:
        fail(f"wheel is missing {missing}")
    print(f"{path.name}: {len(names)} files, {len(expected)} required files present")


def check_sdist(path: Path) -> None:
    with tarfile.open(path) as archive:
        names = archive.getnames()
    tops = {name.split("/")[1] for name in names if name.count("/") >= 1 and name.split("/")[1]}
    for banned in ("src", "tests", "scripts"):
        if banned in tops:
            fail(f"sdist must not ship {banned}/")
    if "telegram_archive" not in tops:
        fail("sdist has no telegram_archive/")
    print(f"{path.name}: top-level entries {sorted(tops)}")


def main() -> None:
    dist = Path(sys.argv[1])
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected one wheel and one sdist in {dist}, got {wheels + sdists}")
    check_wheel(wheels[0])
    check_sdist(sdists[0])


if __name__ == "__main__":
    main()
