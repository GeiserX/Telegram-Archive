"""Telegram Archive: incremental Telegram backups you can browse.

The Python API is small on purpose::

    import asyncio

    from telegram_archive import Config, run_backup

    asyncio.run(run_backup(Config()))

``Config`` reads the same environment variables as the Docker image.
``run_backup`` is a coroutine that backs up every configured account once.
``TelegramBackup`` is the class it drives, for callers that need more control.

The names load on first use. The viewer image installs no telethon, and
``import telegram_archive`` must keep working there.
"""

import importlib

__version__ = "8.16.1"

__all__ = ["Config", "TelegramBackup", "run_backup", "__version__"]

_LAZY_EXPORTS = {
    "Config": "telegram_archive.config",
    "TelegramBackup": "telegram_archive.telegram_backup",
    "run_backup": "telegram_archive.telegram_backup",
}


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
