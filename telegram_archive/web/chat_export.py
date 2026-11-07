"""One chat's export, as the viewer's download and the ``export-chat`` command both write it.

The viewer's ``GET /api/chats/{ref}/export`` and the command line share this
module, so a chat exported either way is the same file: the JSON byte for
byte, the HTML the same ZIP entries with the same pages. Nothing here imports
FastAPI. The route turns :class:`ExportArgumentError` into a 400 with the
same text; the command prints it and exits 2.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from .. import __version__
from ..message_utils import parse_utc_timestamp
from . import export_html
from .media_utils import media_relative_path, resolve_media_file


class ExportArgumentError(ValueError):
    """A refused export parameter. The text is the viewer's 400 detail."""


@dataclass(frozen=True)
class ExportWindow:
    """The date window and change filter of an export, as given and as parsed (naive UTC)."""

    from_date: str | None = None
    to_date: str | None = None
    parsed_from: datetime | None = None
    parsed_to: datetime | None = None
    changed_since: datetime | None = None

    def filters(self) -> dict | None:
        """What the file records about its window, or None for the whole history."""
        if not (self.parsed_from or self.parsed_to or self.changed_since):
            return None
        window = {"from": self.from_date, "to": self.to_date}
        if self.changed_since:
            window["changed_since"] = self.changed_since.isoformat()
        return window


def parse_export_bound(value: str, param: str, *, exclusive_end: bool) -> datetime:
    """Parse an export date bound to naive UTC (the #365 conversion contract).

    A bare date (``2026-01-31``) as the end bound means "through that whole
    day", so it becomes the NEXT midnight and the query compares with ``<``.
    Offset-bearing datetimes are converted to UTC and stripped — Message.date
    is naive UTC, so relabelling would shift the window by the client offset.
    """
    date_only = len(value) == 10
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExportArgumentError(f"Invalid {param} date. Use ISO 8601.") from None
    if parsed.tzinfo:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    if exclusive_end and date_only:
        try:
            parsed += timedelta(days=1)
        except OverflowError:
            # to=9999-12-31 means "no upper bound" — clamp instead of failing
            # on a valid ISO date.
            parsed = datetime.max
    return parsed


def parse_export_options(
    export_format: str, media: str | None, max_file_mb: str | None, players: str | None
) -> export_html.HtmlExportOptions | None:
    """The HTML options for ``format=html``, None for JSON, or ExportArgumentError.

    ``media``, ``max_file_mb`` and ``players`` are refused with JSON, which
    takes exactly the old path.
    """
    if export_format not in ("json", "html"):
        raise ExportArgumentError("Invalid format. Use json or html.")
    if export_format == "json":
        if media is not None or max_file_mb is not None or players is not None:
            raise ExportArgumentError("media, max_file_mb and players apply to format=html only")
        return None
    try:
        kinds = export_html.parse_media_kinds(media)
    except ValueError as e:
        raise ExportArgumentError(str(e)) from None
    limit_mb = export_html.DEFAULT_MAX_FILE_MB
    if max_file_mb is not None:
        limit_mb = int(max_file_mb) if re.fullmatch(r"[0-9]{1,4}", max_file_mb) else 0
        if not 1 <= limit_mb <= export_html.MAX_FILE_MB_LIMIT:
            raise ExportArgumentError(f"max_file_mb must be a whole number from 1 to {export_html.MAX_FILE_MB_LIMIT}")
    if players is None or players.lower() == "true":
        show_players = True
    elif players.lower() == "false":
        show_players = False
    else:
        raise ExportArgumentError("players must be true or false")
    return export_html.HtmlExportOptions(media=kinds, max_file_bytes=limit_mb * 1024 * 1024, players=show_players)


def parse_export_window(from_date: str | None, to_date: str | None, changed_since: str | None) -> ExportWindow:
    """The export's window, or ExportArgumentError.

    ``from`` is inclusive; ``to`` is inclusive of the named day when given as
    a bare date and exclusive when given as a full timestamp. ``changed_since``
    naive means UTC.
    """
    parsed_from = parse_export_bound(from_date, "from", exclusive_end=False) if from_date else None
    parsed_to = parse_export_bound(to_date, "to", exclusive_end=True) if to_date else None
    if parsed_from and parsed_to and parsed_from >= parsed_to:
        raise ExportArgumentError("'from' must be before 'to'")
    try:
        parsed_changed_since = parse_utc_timestamp(changed_since) if changed_since is not None else None
    except ValueError:
        raise ExportArgumentError("Invalid changed_since timestamp. Use ISO 8601.") from None
    return ExportWindow(from_date, to_date, parsed_from, parsed_to, parsed_changed_since)


def export_timezone(name: str | None) -> tzinfo:
    """The zone the HTML export prints times in: ``name`` (``VIEWER_TIMEZONE``), UTC when unset or unknown."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError, ValueError:
        return UTC


def export_chat_metadata(chat: dict) -> dict:
    """Return the minimal chat metadata needed by JSON exports.

    The export FILE keeps the chat id — it is the user's own data leaving the
    system, not a URL — and gains the ref so an export can be correlated with
    the viewer's addressing.
    """
    return {key: chat.get(key) for key in ("id", "ref", "type", "title", "username")}


def export_names(chat_row: dict) -> tuple[str, str]:
    """``(chat_name, safe_name)``: the name the export shows, and the one its file is named after.

    The file name never falls back to the chat id: the saved name lands in
    filesystem listings, which are the same surface as a URL.
    """
    chat_name = chat_row.get("title") or chat_row.get("username") or chat_row["ref"]
    safe_name = "".join(c for c in chat_name if c.isalnum() or c in (" ", "-", "_")).strip()
    return chat_name, safe_name


def export_filename(safe_name: str, export_format: str) -> str:
    """The download's file name: ``<name>_export.json`` or ``<name>_export.zip``."""
    return f"{safe_name}_export.{'zip' if export_format == 'html' else 'json'}"


async def iter_export_json(
    db, chat_row: dict, window: ExportWindow, *, chat_id: int, account_id: int
) -> AsyncIterator[str]:
    """The JSON export of the chat ``chat_id`` of ``account_id``, whose ``get_chat_by_id`` row is ``chat_row``."""
    yield "{\n"
    yield f'  "chat": {json.dumps(export_chat_metadata(chat_row), ensure_ascii=False, default=str)},\n'
    filters = window.filters()
    if filters:
        # Record what this file contains — a windowed export must not
        # masquerade as the full history.
        yield f'  "filters": {json.dumps(filters, ensure_ascii=False)},\n'
    yield '  "messages": [\n'
    first = True
    async for msg in db.get_messages_for_export(
        chat_id,
        account_id=account_id,
        from_date=window.parsed_from,
        to_date=window.parsed_to,
        changed_since=window.changed_since,
    ):
        if not first:
            yield ",\n"
        first = False
        # Ensure UTF-8 encoding for non-Latin characters
        yield "    " + json.dumps(msg, ensure_ascii=False, default=str)
    yield "\n  ]\n"
    yield "}"


def media_source(file_path: str | None, media_root: Path | None) -> Path | None:
    """The real file behind a media row's stored path, inside the media root, or None."""
    if not media_root:
        return None
    relative = media_relative_path(file_path, media_root)
    return resolve_media_file(relative, media_root) if relative else None


async def media_poster(media: dict, media_root: Path, cache_dir: Path, may_generate: bool) -> Path | None:
    """The viewer's own thumbnail of a video, from its cache under ``cache_dir``, as the export's poster.

    ``may_generate`` False (the export spent its poster time) takes a cached one only.
    """
    relative = media_relative_path(media.get("file_path"), media_root)
    folder, _, filename = (relative or "").rpartition("/")
    if not folder:
        return None
    from .thumbnails import ensure_thumbnail

    result = await ensure_thumbnail(media_root, 400, folder, filename, cache_dir=cache_dir, generate=may_generate)
    return result[0] if result else None


def iter_export_html(
    db,
    chat_row: dict,
    options: export_html.HtmlExportOptions,
    window: ExportWindow,
    *,
    chat_id: int,
    account_id: int,
    resolve_media: Callable[[str], Path | None],
    poster: Callable[[dict, Path, bool], Awaitable[Path | None]] | None,
    tz: tzinfo,
) -> AsyncIterator[bytes]:
    """The HTML export of ``chat_row`` as ZIP bytes (``export_html.iter_html_zip``).

    The messages are spooled first, so the database snapshot closes before
    the first byte goes out.
    """
    chat_name, safe_name = export_names(chat_row)
    messages = db.get_messages_for_export(
        chat_id,
        account_id=account_id,
        from_date=window.parsed_from,
        to_date=window.parsed_to,
        changed_since=window.changed_since,
        render=True,
    )
    return export_html.iter_html_zip(
        export_html.spool_messages(messages),
        chat={key: chat_row.get(key) for key in ("type", "title", "username")} | {"title": chat_name},
        filters=window.filters(),
        options=options,
        resolve_media=resolve_media,
        poster=poster,
        tz=tz,
        top=f"ChatExport_{export_html.safe_top_name(safe_name)}",
        version=__version__,
    )
