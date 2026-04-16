"""Regression tests for the NOTIFY bind-parameter bug (yrru-ks3d).

The original implementation f-string-interpolated the JSON payload directly
into the SQL string: ``NOTIFY telegram_updates, '<json>'``. asyncpg scans
the final SQL for ``$N`` positional placeholders and blows up whenever a
message body contains tokens like ``$1`` or ``$D``.

Fix: use ``pg_notify(channel, payload)`` with SQLAlchemy bound parameters so
the payload is never part of the SQL string.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.realtime import NotificationType, RealtimeNotifier


def _make_notifier_with_fake_session():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    session_factory = MagicMock(return_value=session)
    db_manager = MagicMock()
    db_manager.async_session_factory = session_factory
    db_manager._is_sqlite = False

    notifier = RealtimeNotifier(db_manager=db_manager)
    notifier._is_postgresql = True
    notifier._initialized = True
    return notifier, session


@pytest.mark.asyncio
async def test_notify_does_not_interpolate_payload_into_sql():
    """Payload must be a bound parameter, never embedded in the SQL string."""
    notifier, session = _make_notifier_with_fake_session()

    await notifier.notify(
        NotificationType.NEW_MESSAGE,
        chat_id=1011405549,
        data={"message": {"id": 1, "text": "this will $1 break $D asyncpg"}},
    )

    assert session.execute.await_count == 1
    call_args = session.execute.await_args
    stmt = call_args.args[0]
    sql = str(stmt)

    # The payload (and any $-tokens it contains) must not be in the SQL.
    assert "$1 break" not in sql
    assert "$D" not in sql
    # The SQL must use pg_notify with bound parameter names.
    assert "pg_notify" in sql.lower()

    params = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs
    assert params["channel"] == "telegram_updates"
    assert "$1 break $D asyncpg" in params["payload"]


@pytest.mark.asyncio
async def test_notify_survives_dollar_tokens_without_warning(caplog):
    """End-to-end: .notify() with dollar-tokens must not emit the old warning."""
    notifier, _session = _make_notifier_with_fake_session()

    with caplog.at_level("WARNING", logger="src.realtime"):
        await notifier.notify(
            NotificationType.EDIT,
            chat_id=42,
            data={
                "chat_id": 42,
                "message_id": 1,
                "new_text": "sshhhh $1 will take LONGER! $O",
            },
        )

    # No "Failed to send realtime notification" warning should appear.
    messages = [r.getMessage() for r in caplog.records]
    assert not any("Failed to send realtime notification" in m for m in messages), messages
