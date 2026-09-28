"""The old package name, kept so existing Docker deployments keep working.

The package is ``telegram_archive``. Compose files written for older images
run ``python -m src schedule`` or ``uvicorn src.web.main:app``, so both images
still ship this directory. It is not part of the PyPI package.

Every ``src.<name>`` import returns the ``telegram_archive.<name>`` module
object itself. Nothing is loaded twice, so module state (config, database
engines, the FastAPI app, realtime hubs) is shared: ``src.web.main.app`` is
``telegram_archive.web.main.app``.
"""

import importlib
import importlib.abc
import importlib.util
import sys

_OLD = __name__
_NEW = "telegram_archive"


class _AliasLoader(importlib.abc.Loader):
    """Hands back the already-importable ``telegram_archive`` module."""

    def __init__(self, target: str, target_spec):
        self._target = target
        self._target_spec = target_spec

    def create_module(self, spec):
        return importlib.import_module(self._target)

    def exec_module(self, module):
        # Creating the module under the old name overwrote __spec__ with the
        # alias spec. The object is the real module, so give it its spec back.
        module.__spec__ = self._target_spec

    def is_package(self, fullname):
        return self._target_spec.submodule_search_locations is not None

    def get_code(self, fullname):
        # Used by ``python -m src.<module>`` (runpy runs the code as __main__).
        return self._target_spec.loader.get_code(self._target)


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + ".") or fullname == _OLD + ".__main__":
            return None
        real = _NEW + fullname[len(_OLD) :]
        try:
            real_spec = importlib.util.find_spec(real)
        except ModuleNotFoundError:
            return None
        if real_spec is None:
            return None
        loader = _AliasLoader(real, real_spec)
        return importlib.util.spec_from_loader(fullname, loader, is_package=loader.is_package(fullname))


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
    print(
        f"Note: the '{_OLD}' module name is deprecated and will keep working for now. "
        f"Use 'python -m {_NEW}' or '{_NEW}.web.main:app' instead.",
        file=sys.stderr,
    )


def __getattr__(name):
    return getattr(importlib.import_module(_NEW), name)
