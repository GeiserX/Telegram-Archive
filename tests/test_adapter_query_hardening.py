"""Data-layer hardening regressions: upsert data loss, audit clamping, query cost.

Everything here runs against a real SQLite database through the production
``DatabaseAdapter`` so the actual SQL is exercised — the query-shape tests in
particular are worthless against a mocked session.

Covers:
  * message upserts must not erase columns the writer never supplied
  * the real importer must not assert flags the export does not carry
  * ``create_audit_log`` must fit its declared column widths on both backends
    and must never let a NUL byte reach the insert
  * the chat list must not aggregate the whole archive, and must page on a
    total (tie-free) ordering
  * a message page must read the media table once, not twice
  * a gallery page must sort narrow keys, not whole rows
  * ``viewer_audit_log``'s indexes must exist in the ORM metadata
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import event, inspect, select

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message, User, ViewerAuditLog
from src.telegram_import import TelegramImporter

CHAT_ID = -1001234567890


@pytest.fixture
async def sqlite_adapter(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'telegram_archive.db'}")
    await manager.init()
    try:
        yield DatabaseAdapter(manager)
    finally:
        await manager.close()


async def _get_message(adapter: DatabaseAdapter, message_id: int, chat_id: int) -> Message:
    async with adapter.db_manager.async_session_factory() as session:
        # v8.0.0 PK order: (account_id, chat_id, id).
        message = await session.get(Message, (1, chat_id, message_id))
        assert message is not None
        return message


class _SQLRecorder:
    """Capture every statement a block of adapter code actually emits."""

    def __init__(self, adapter: DatabaseAdapter):
        self._engine = adapter.db_manager.engine.sync_engine
        self.statements: list[str] = []

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(" ".join(statement.split()))

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on_execute)
        return False

    def matching(self, *needles: str) -> list[str]:
        return [s for s in self.statements if all(n in s for n in needles)]


# ---------------------------------------------------------------------------
# S1 — an upsert must not NULL out columns the writer never supplied
# ---------------------------------------------------------------------------


# The exact shape src/telegram_import.py builds for `import --merge`: it omits
# reply_to_top_id and reply_to_text entirely, carries an empty raw_data, and —
# because the export carries none of them — never supplies forward_from_id,
# is_outgoing or is_pinned.
def _merge_import_message(message_id: int, *, date: datetime) -> dict:
    return {
        "id": message_id,
        "chat_id": CHAT_ID,
        "sender_id": 4242,
        "sender_name": "Imported Sender",
        "date": date,
        "text": "hello",
        "reply_to_msg_id": None,
        "edit_date": None,
        "raw_data": {},
    }


async def _archive_backup_message(adapter: DatabaseAdapter, message_id: int, date: datetime) -> None:
    """A fully-populated row as the live backup writes it."""
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": CHAT_ID,
            "sender_id": 4242,
            "sender_name": "Imported Sender",
            "date": date,
            "text": "hello",
            "reply_to_msg_id": 70,
            "reply_to_top_id": 77,
            "reply_to_text": "the parent message",
            "forward_from_id": 999,
            "edit_date": None,
            "raw_data": {"grouped_id": "5150", "action_type": "chat_migrate_to", "migrate_to_id": -1009999999999},
            "is_outgoing": 1,
            "is_pinned": 1,
        },
        account_id=1,
    )


@pytest.mark.asyncio
async def test_merge_import_upsert_keeps_columns_it_never_supplied(sqlite_adapter):
    """`import --merge` over an archived message must not blank its metadata."""
    date = datetime(2026, 3, 1, 12, 0)
    await _archive_backup_message(sqlite_adapter, 1, date)

    await sqlite_adapter.insert_messages_batch([_merge_import_message(1, date=date)], account_id=1)

    message = await _get_message(sqlite_adapter, 1, CHAT_ID)
    # Absent from the import payload -> the archived value stands. Losing
    # reply_to_top_id collapses forum messages out of their topic into General.
    assert message.reply_to_top_id == 77
    assert message.reply_to_text == "the parent message"
    # Also absent: the export knows nothing about these three, so the captured
    # values survive the merge.
    assert message.is_outgoing == 1
    assert message.is_pinned == 1
    assert message.forward_from_id == 999
    # Supplied as an empty blob, which is not an observation that the archived
    # extras are gone: grouped_id drives album rendering and migrate_to_id is
    # what get_migration_markers reads back.
    assert '"grouped_id": "5150"' in message.raw_data
    assert (CHAT_ID, -1009999999999) in await sqlite_adapter.get_migration_markers(account_id=1)


@pytest.mark.asyncio
async def test_upsert_without_optional_keys_preserves_every_archived_column(sqlite_adapter):
    """The minimal payload — only the required keys — changes nothing else."""
    date = datetime(2026, 3, 1, 12, 0)
    await _archive_backup_message(sqlite_adapter, 2, date)

    await sqlite_adapter.insert_message({"id": 2, "chat_id": CHAT_ID, "date": date}, account_id=1)

    message = await _get_message(sqlite_adapter, 2, CHAT_ID)
    assert message.reply_to_top_id == 77
    assert message.reply_to_msg_id == 70
    assert message.reply_to_text == "the parent message"
    assert message.forward_from_id == 999
    assert message.sender_id == 4242
    assert message.is_outgoing == 1
    assert message.is_pinned == 1
    assert message.text == "hello"
    assert '"grouped_id": "5150"' in message.raw_data


@pytest.mark.asyncio
async def test_upsert_still_writes_the_columns_it_does_supply(sqlite_adapter):
    """Guard against over-correcting: supplied values must still be applied."""
    date = datetime(2026, 3, 1, 12, 0)
    await _archive_backup_message(sqlite_adapter, 3, date)

    await sqlite_adapter.insert_message(
        {
            "id": 3,
            "chat_id": CHAT_ID,
            "date": date,
            "reply_to_top_id": 88,
            "forward_from_id": 1234,
            "is_pinned": 0,
            "raw_data": {"grouped_id": "6000"},
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 3, CHAT_ID)
    assert message.reply_to_top_id == 88
    assert message.forward_from_id == 1234
    assert message.is_pinned == 0  # an explicit unpin still lands
    assert '"grouped_id": "6000"' in message.raw_data


@pytest.mark.asyncio
async def test_upsert_may_hydrate_a_column_that_was_never_captured(sqlite_adapter):
    """An absent-key rule must not block filling a genuinely empty column."""
    date = datetime(2026, 3, 1, 12, 0)
    await sqlite_adapter.insert_message({"id": 4, "chat_id": CHAT_ID, "date": date, "text": "hello"}, account_id=1)

    await sqlite_adapter.insert_message(
        {
            "id": 4,
            "chat_id": CHAT_ID,
            "date": date,
            "reply_to_top_id": 77,
            "raw_data": {"grouped_id": "7000"},
        },
        account_id=1,
    )

    message = await _get_message(sqlite_adapter, 4, CHAT_ID)
    assert message.reply_to_top_id == 77
    assert '"grouped_id": "7000"' in message.raw_data


# ---------------------------------------------------------------------------
# S1 mirror — the real importer must not assert flags the export never carries
# ---------------------------------------------------------------------------


def _write_export(tmp_path) -> str:
    """A minimal Telegram Desktop JSON export whose derived chat id is CHAT_ID."""
    export_dir = tmp_path / "export"
    export_dir.mkdir(exist_ok=True)
    (export_dir / "result.json").write_text(
        json.dumps(
            {
                "name": "Backup Target",
                "type": "private_supergroup",
                "id": 1234567890,  # derive_chat_id -> -1001234567890 == CHAT_ID
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2026-03-01T12:00:00",
                        "from": "Imported Sender",
                        "from_id": "user4242",
                        "text": "hello",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(export_dir)


@pytest.mark.asyncio
async def test_fresh_import_still_defaults_the_flags_it_cannot_know(sqlite_adapter, tmp_path):
    """First-time import: absent keys must still land as sane column defaults."""
    importer = TelegramImporter(sqlite_adapter, media_path=str(tmp_path / "media"), account_id=1)

    await importer.run(_write_export(tmp_path), skip_media=True)

    message = await _get_message(sqlite_adapter, 1, CHAT_ID)
    assert message.text == "hello"
    assert message.is_outgoing == 0
    assert message.is_pinned == 0
    assert message.forward_from_id is None


@pytest.mark.asyncio
async def test_merge_import_preserves_outgoing_pinned_and_forward_source(sqlite_adapter, tmp_path):
    """`import --merge` over an archived chat must not falsify what it never saw.

    Telegram Desktop exports carry no outgoing flag, no pinned flag and no
    forwarder id, but the importer used to supply hard-coded stand-ins for all
    three — so the upsert treated them as observations and the owner's own
    messages stopped rendering as outgoing, pins dropped, and the forward
    source id was lost. This drives the real TelegramImporter end to end, so a
    reintroduced key in its message dict fails here even though the adapter's
    absent-key rule is pinned above.
    """
    date = datetime(2026, 3, 1, 12, 0)
    await _archive_backup_message(sqlite_adapter, 1, date)
    importer = TelegramImporter(sqlite_adapter, media_path=str(tmp_path / "media"), account_id=1)

    await importer.run(_write_export(tmp_path), merge=True, skip_media=True)

    message = await _get_message(sqlite_adapter, 1, CHAT_ID)
    assert message.is_outgoing == 1
    assert message.is_pinned == 1
    assert message.forward_from_id == 999


# ---------------------------------------------------------------------------
# S13 — audit rows must fit their columns, or PostgreSQL never records them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_audit_log_clamps_values_to_their_column_widths(sqlite_adapter):
    """A failed login with an over-long username must still be recorded.

    SQLite ignores VARCHAR lengths; PostgreSQL raises SQLSTATE 22001 and the
    caller's bare `except Exception` swallows it, so the login_failed record
    silently never exists there. Asserting against the declared widths states
    the invariant in a backend-independent way.
    """
    widths = {c.name: getattr(c.type, "length", None) for c in ViewerAuditLog.__table__.columns}

    await sqlite_adapter.create_audit_log(
        username="u" * 400,
        role="viewer" * 20,
        action="login_failed" * 40,
        endpoint="/api/login" * 80,
        ip_address="203.0.113.9," * 40,
        user_agent="agent" * 500,
    )

    logs = await sqlite_adapter.get_audit_logs(limit=10)
    assert len(logs) == 1, "the audit row must exist at all"
    for column in ("username", "role", "action", "endpoint", "ip_address"):
        assert len(logs[0][column]) <= widths[column], f"{column} exceeds its declared width"
    assert logs[0]["username"].startswith("uuu")
    # user_agent is a Text column with no width, so it is stored whole.
    assert len(logs[0]["user_agent"]) == 2500


@pytest.mark.asyncio
async def test_create_audit_log_leaves_short_values_untouched(sqlite_adapter):
    await sqlite_adapter.create_audit_log(
        username="viewer-1",
        role="viewer",
        action="login_failed",
        endpoint="/api/login",
        ip_address="203.0.113.9",
    )

    logs = await sqlite_adapter.get_audit_logs(limit=10)
    assert logs[0]["username"] == "viewer-1"
    assert logs[0]["ip_address"] == "203.0.113.9"
    assert logs[0]["endpoint"] == "/api/login"


@pytest.mark.asyncio
async def test_create_audit_log_writes_the_row_even_when_fields_carry_nul(sqlite_adapter):
    """A NUL byte in any login field must not erase the audit trail.

    PostgreSQL rejects \\x00 in text values outright and the caller's bare
    `except Exception` swallows the error, so a "user\\x00" login attempt used
    to leave ZERO audit rows there — length clamping alone never caught it.
    SQLite stores NUL happily, so the backend-independent invariant is that no
    NUL may reach the insert at all, in the clamped columns and in the
    width-less user_agent Text column alike.
    """
    await sqlite_adapter.create_audit_log(
        username="attacker\x00",
        role="viewer\x00",
        action="login_failed\x00",
        endpoint="/api/login\x00",
        ip_address="203.0.113.9\x00",
        user_agent="probe\x00agent",
    )

    logs = await sqlite_adapter.get_audit_logs(limit=10)
    assert len(logs) == 1, "the audit row must exist at all"
    for column in ("username", "role", "action", "endpoint", "ip_address", "user_agent"):
        assert "\x00" not in logs[0][column], f"{column} still carries a NUL byte"
    # Replaced, not stripped: a NUL-suffixed impersonation of a real username
    # must stay distinguishable from the genuine one in the audit record.
    assert logs[0]["username"] == "attacker�"
    assert logs[0]["user_agent"] == "probe�agent"


# ---------------------------------------------------------------------------
# S35 — the audit indexes must live in the ORM metadata, not only migration 007
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_all_provisions_the_viewer_audit_log_indexes(sqlite_adapter):
    """Fresh installs are provisioned by create_all(), which never runs 007."""
    declared = {index.name for index in ViewerAuditLog.__table__.indexes}
    assert declared == {"idx_audit_log_username", "idx_audit_log_created"}

    async with sqlite_adapter.db_manager.engine.connect() as conn:
        created = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("viewer_audit_log"))
    assert {index["name"] for index in created} == declared


# ---------------------------------------------------------------------------
# S9 / S33 — the chat list
# ---------------------------------------------------------------------------


async def _seed_chats(adapter: DatabaseAdapter, with_messages: int, without_messages: int) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        # Ascending ids, so an untied ORDER BY yields them ascending and the
        # descending tiebreaker is a real assertion rather than a coincidence.
        for i in range(with_messages + without_messages):
            session.add(Chat(id=-2000 + i, type="group", title=f"Chat {i}"))
        await session.flush()
        for i in range(with_messages):
            session.add(
                Message(
                    id=1,
                    chat_id=-2000 + i,
                    date=datetime(2026, 1, 1, 0, i),
                    text="hi",
                    raw_data="{}",
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_chat_list_does_not_aggregate_the_whole_messages_table(sqlite_adapter):
    """Listing a page of chats must cost O(chats returned), not O(all messages).

    The aggregate has to be correlated to the chat row. An uncorrelated
    `GROUP BY chat_id` over `messages` cannot be pruned by the LIMIT, so the
    sidebar's latency grew with the size of the whole archive.
    """
    await _seed_chats(sqlite_adapter, with_messages=4, without_messages=3)

    with _SQLRecorder(sqlite_adapter) as recorder:
        await sqlite_adapter.get_all_chats(limit=5)

    reads = recorder.matching("FROM chats")
    assert len(reads) == 1
    statement = reads[0]
    assert "GROUP BY" not in statement, "the message aggregate must not be an unbounded GROUP BY"
    assert "max(messages.date)" in statement
    # v8.0.0: the correlation carries the account too — a chat id repeats
    # across accounts, so correlating on chat_id alone reads both copies.
    assert "WHERE messages.account_id = chats.account_id AND messages.chat_id = chats.id" in statement, (
        "the aggregate must be correlated to the chat row"
    )

    async with sqlite_adapter.db_manager.engine.connect() as conn:
        plan = await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement.replace("?", "50"))
        detail = " | ".join(row[3] for row in plan)
    assert "SCAN messages" not in detail, f"every message row is still being read: {detail}"


@pytest.mark.asyncio
async def test_chat_list_pages_on_a_total_ordering(sqlite_adapter):
    """Chats with no messages all tie on NULL; ties need a unique tiebreaker.

    Without one the split between LIMIT/OFFSET pages is arbitrary and need not
    agree across two executions, so a chat can appear on two pages while another
    never appears at all — and an absent chat is unreachable from the sidebar.
    """
    await _seed_chats(sqlite_adapter, with_messages=3, without_messages=6)

    chats = await sqlite_adapter.get_all_chats(limit=50)
    assert len(chats) == 9

    keys = [(chat["last_message_date"] is None, chat["last_message_date"], chat["id"]) for chat in chats]
    dated = [k for k in keys if not k[0]]
    undated = [k for k in keys if k[0]]
    assert keys == dated + undated, "chats with messages must sort before chats without"
    assert [k[1] for k in dated] == sorted((k[1] for k in dated), reverse=True)
    # The tie group must come back in a defined order, not an arbitrary one.
    assert [k[2] for k in undated] == sorted((k[2] for k in undated), reverse=True)

    # And the walk must be stable: paging over the tie group loses nobody.
    paged: list[int] = []
    for offset in range(0, 9, 3):
        paged += [chat["id"] for chat in await sqlite_adapter.get_all_chats(limit=3, offset=offset)]
    assert len(paged) == len(set(paged)) == 9


# ---------------------------------------------------------------------------
# S51 — one message page, one read of the media table
# ---------------------------------------------------------------------------


async def _seed_media_chat(adapter: DatabaseAdapter, messages: int, media_per_message: int = 1) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="channel", title="Gallery"))
        session.add(User(id=100, first_name="Alice", last_name="Smith", username="alice"))
        await session.flush()
        for i in range(1, messages + 1):
            session.add(
                Message(
                    id=i,
                    chat_id=CHAT_ID,
                    sender_id=100,
                    # Deliberate date ties, so the (date, message_id, id) triple
                    # is exercised rather than the leading key alone.
                    date=datetime(2026, 1, 1, 0, i // 3),
                    text="hi",
                    raw_data="{}",
                )
            )
            for k in range(media_per_message):
                session.add(
                    Media(
                        id=f"{CHAT_ID}_{i}_{k}",
                        message_id=i,
                        chat_id=CHAT_ID,
                        type="photo",
                        file_path=f"{CHAT_ID}/{i}_{k}.jpg",
                        file_name=f"{i}_{k}.jpg",
                        downloaded=1,
                    )
                )
        await session.commit()


@pytest.mark.asyncio
async def test_message_page_reads_the_media_table_once(sqlite_adapter):
    """The page already outer-joins media; the selectin loader read it again.

    Nothing in src/ reads Message.media_items, so that second read built a full
    set of Media ORM objects per page and threw them away.
    """
    await _seed_media_chat(sqlite_adapter, messages=6)

    with _SQLRecorder(sqlite_adapter) as recorder:
        messages = await sqlite_adapter.get_messages_paginated(CHAT_ID, limit=5)

    assert messages, "the page must still carry its messages"
    assert any(message.get("media") for message in messages), "media must still be joined in"

    eager_reads = [
        statement
        for statement in recorder.statements
        if statement.startswith("SELECT media.") and "JOIN" not in statement and "messages" not in statement
    ]
    assert eager_reads == [], f"the media table is still being read a second time: {eager_reads}"


# ---------------------------------------------------------------------------
# S27 — the gallery page must sort keys, not whole rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_page_sorts_page_keys_not_whole_rows(sqlite_adapter):
    """Only Media.id may go through the sorter that the LIMIT is applied to."""
    await _seed_media_chat(sqlite_adapter, messages=12)

    with _SQLRecorder(sqlite_adapter) as recorder:
        await sqlite_adapter.get_media_paginated(CHAT_ID, limit=5)

    reads = recorder.matching("FROM media", "ORDER BY")
    assert len(reads) == 1
    statement = reads[0]
    assert "(SELECT" in statement, f"the page keys must be picked by a subquery: {statement}"
    ordered_subquery = statement[statement.index("(SELECT") : statement.index("LIMIT") + len("LIMIT ?")]
    assert "ORDER BY" in ordered_subquery, "the LIMIT must be applied inside the key subquery"
    assert "media.file_path" not in ordered_subquery, "wide media columns must not go through the sorter"
    assert "users." not in ordered_subquery, "joined user columns must not go through the sorter"


@pytest.mark.asyncio
async def test_media_page_cursor_walk_still_yields_every_row_once(sqlite_adapter):
    """The rewrite must not disturb the cursor/ORDER BY identity.

    A full backward walk has to return every media row exactly once, in the
    same (message date, message id, media id) order — including across the
    deliberate timestamp ties in the fixture.
    """
    await _seed_media_chat(sqlite_adapter, messages=12, media_per_message=2)

    walked: list[str] = []
    cursor = None
    # 24 rows at 5 per page is 5 round trips; the bound turns a cursor that
    # stops advancing into a failure rather than a hung test run.
    for _ in range(10):
        page = await sqlite_adapter.get_media_paginated(CHAT_ID, limit=5, before_id=cursor)
        walked += [item["id"] for item in page["items"]]
        if not page["has_more"]:
            break
        cursor = page["items"][-1]["id"]
    else:
        pytest.fail(f"the cursor never reached the end of the gallery: {len(walked)} rows walked")

    async with sqlite_adapter.db_manager.async_session_factory() as session:
        expected = list(
            (
                await session.execute(
                    select(Media.id)
                    .join(Message, (Media.message_id == Message.id) & (Media.chat_id == Message.chat_id))
                    .where(Media.chat_id == CHAT_ID)
                    .order_by(Message.date.desc(), Media.message_id.desc(), Media.id.desc())
                )
            ).scalars()
        )

    assert walked == expected
    assert len(walked) == len(set(walked)) == 24


@pytest.mark.asyncio
async def test_media_page_forward_and_backward_pages_agree(sqlite_adapter):
    await _seed_media_chat(sqlite_adapter, messages=8)

    newest = await sqlite_adapter.get_media_paginated(CHAT_ID, limit=3)
    older = await sqlite_adapter.get_media_paginated(CHAT_ID, limit=3, before_id=newest["items"][-1]["id"])
    back = await sqlite_adapter.get_media_paginated(CHAT_ID, limit=3, after_id=older["items"][0]["id"])

    # Walking forward out of the older page lands back on the newest page,
    # oldest-first, so the two directions describe the same ordering.
    assert [item["id"] for item in back["items"]] == [item["id"] for item in reversed(newest["items"])]
    # Hydration still happens outside the sorter, so every column is populated.
    assert all(item["message_date"] for item in newest["items"])
    assert all(item["file_name"] for item in newest["items"])


# ---------------------------------------------------------------------------
# schema sanity — the models still build a database
# ---------------------------------------------------------------------------


def test_models_metadata_declares_the_message_media_relationship_lazily():
    """media_items must not eager-load: every read path joins media itself."""
    assert Message.__mapper__.relationships["media_items"].lazy == "select"
    assert "media" in Base.metadata.tables
