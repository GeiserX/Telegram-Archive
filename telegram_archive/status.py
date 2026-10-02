"""Is the archive healthy right now: one answer for the viewer and the command line.

``collect_status`` builds the payload the viewer's ``GET /api/status`` returns.
``health_problems`` reads that payload against the ``SCHEDULE`` and
``ENABLE_LISTENER`` settings for ``telegram-archive status``. Counts and timestamps only: never ids, titles or
content.

The viewer image imports this module but ships without APScheduler, so the
cron import stays inside ``health_problems``, which only the command calls.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .db.models import DEFAULT_ACCOUNT_ID, account_metadata_key

if TYPE_CHECKING:
    from datetime import tzinfo

    from .config import Config
    from .db.adapter import DatabaseAdapter

# How far back to look for the schedule's last two runs. The first window
# that holds two is used, so a frequent schedule never walks a long one.
_LOOKBACK_WINDOWS = (timedelta(hours=1), timedelta(days=1), timedelta(days=32), timedelta(days=3700))


async def collect_status(db: DatabaseAdapter, config: Config) -> dict[str, Any]:
    """Last backup run, listener liveness, media pipeline, stats freshness, database size."""
    payload: dict[str, Any] = {
        "backup": {
            "last_run": await db.get_metadata("last_backup_time"),
            "in_progress": (await db.get_metadata("backup_in_progress")) == "1",
        },
        "stats_calculated_at": await db.get_metadata("stats_calculated_at"),
    }
    try:
        account_ids = list(await db.get_account_ids())
    except Exception:
        account_ids = [DEFAULT_ACCOUNT_ID]
    listeners = []
    for account_id in account_ids:
        since = await db.get_metadata(account_metadata_key("listener_active_since", account_id))
        listeners.append({"account_id": account_id, "active": bool(since), "active_since": since})
    payload["listeners"] = listeners
    payload["media"] = await db.get_operator_status_counts(max_attempts=config.max_media_download_attempts)
    payload["database"] = {
        "backend": "sqlite" if db.db_manager._is_sqlite else "postgresql",
        "size_bytes": await db.get_database_size_bytes(),
    }
    return payload


def parse_utc(value: str | None) -> datetime | None:
    """A stored timestamp as an aware UTC datetime. Both stored forms are UTC, with or without a Z."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def previous_fire_times(trigger: Any, now: datetime, count: int = 2) -> list[datetime]:
    """The last ``count`` times ``trigger`` fired at or before ``now``, oldest first.

    Empty when the schedule has not fired ``count`` times in the longest window.
    """
    for window in _LOOKBACK_WINDOWS:
        fires: list[datetime] = []
        fire = trigger.get_next_fire_time(None, now - window)
        while fire is not None and fire <= now:
            fires.append(fire)
            fire = trigger.get_next_fire_time(fire, fire + timedelta(microseconds=1))
        if len(fires) >= count:
            return fires[-count:]
    return []


def health_problems(
    status: dict[str, Any],
    schedule: str,
    now: datetime | None = None,
    timezone: tzinfo | None = None,
    listener_accounts: int = 0,
) -> list[str]:
    """Why the archive is unhealthy, one sentence per reason. Empty means healthy.

    Four reasons, all read from the status payload:

    - no backup has ever started;
    - the last backup did not finish: it is not running, and the statistics it
      writes after its message sweep are older than its start;
    - the schedule missed a run: SCHEDULE has fired twice since the last backup
      started. One missed tick is tolerated, because a run still going when
      the next tick fires skips that tick;
    - a listener is not running: ``listener_accounts`` is how many accounts
      should have one (the configured accounts when ENABLE_LISTENER is on, 0
      when it is off), and fewer are active. With the listener on, the
      scheduled pass runs once a day, so this is what shows a stopped backup
      within minutes. The account ids are named when the database holds no
      rows beyond the configured accounts; otherwise only the count is, since
      a row left by an account no longer configured never has a listener.

    The statistics are written after the message sweep and before the media
    retries, media verification, transcription and gap-fill, so a failure in
    those later steps is not reported; the logs are the signal for them. A
    per-run finish time stored beside ``last_backup_time`` would make this exact.

    The statistics are also refreshed by the viewer (daily, and on demand), so a
    refresh after a failed run hides that failure until the next run starts.
    With several accounts, the start and the statistics are shared, so a failed
    account followed by one that completes also reads as finished.

    ``timezone`` defaults to the local zone, the one the scheduler uses.
    """
    from apscheduler.triggers.cron import CronTrigger

    # Parsed first, so an invalid SCHEDULE raises even before any backup has run.
    trigger = CronTrigger.from_crontab(schedule, timezone=timezone)

    backup = status["backup"]
    last_run = parse_utc(backup["last_run"])
    if last_run is None:
        return ["no backup has run yet"]

    problems = []
    if not backup["in_progress"]:
        stats_at = parse_utc(status["stats_calculated_at"])
        if stats_at is None or stats_at < last_run:
            problems.append(f"the last backup, started {backup['last_run']}, did not finish")

    fires = previous_fire_times(trigger, now or datetime.now(UTC))
    if fires and last_run < fires[0]:
        problems.append(
            f"no backup has started since the run SCHEDULE ({schedule}) expected at "
            f"{fires[0].astimezone(UTC).isoformat()}"
        )
    problems.extend(_listener_problems(status.get("listeners") or [], listener_accounts))
    return problems


def _listener_problems(listeners: list[dict[str, Any]], expected: int) -> list[str]:
    """One sentence per missing listener, or one for the shortfall when ids would mislead."""
    active = sum(1 for listener in listeners if listener["active"])
    missing = expected - active
    if missing <= 0:
        return []
    inactive = [listener["account_id"] for listener in listeners if not listener["active"]]
    if len(inactive) == missing:
        return [f"the listener of account {account_id} is not running" for account_id in inactive]
    return [f"{missing} of {expected} configured account(s) have no running listener"]


def _format_bytes(size: int | None) -> str:
    if size is None:
        return "size unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_status(status: dict[str, Any], problems: list[str]) -> str:
    """The status as a few plain lines for a terminal."""
    backup = status["backup"]
    running = " (running now)" if backup["in_progress"] else ""
    lines = [
        f"Archive status: {'UNHEALTHY' if problems else 'healthy'}",
        f"  Last backup started:  {backup['last_run'] or 'never'}{running}",
        f"  Statistics updated:   {status['stats_calculated_at'] or 'never'}",
    ]
    for listener in status["listeners"]:
        state = f"active since {listener['active_since']}" if listener["active"] else "not running"
        lines.append(f"  Listener, account {listener['account_id']}: {state}")
    media = ", ".join(f"{count} {name}" for name, count in status["media"].items())
    lines.append(f"  Media files:          {media}")
    database = status["database"]
    lines.append(f"  Database:             {database['backend']}, {_format_bytes(database['size_bytes'])}")
    if problems:
        lines.append("Problems:")
        lines.extend(f"  - {problem}" for problem in problems)
    return "\n".join(lines)
