"""Multi-account RUNTIME (v8.0.0 phase 5), proved on real engines and real flows.

``tests/test_multiaccount_schema.py`` proves the keys admit N accounts and
``tests/test_account_isolation.py`` proves the adapter keeps their rows apart.
This file proves the piece that connects a CONFIGURED account (an env index) to
a DATABASE account (an ``accounts`` row): ``DatabaseAdapter.ensure_account``,
and the sequential per-account sweep built on top of it.

The resolution rule under test, in one line: the Telegram user id owns the row,
the env index owns nothing — except that index 1 is defined as the continuation
of a pre-8.0 archive, whose migrated row 1 carries ``telegram_user_id NULL``.
Get this wrong in either direction and data is silently orphaned (a migrated
archive's rows left under a row nobody resolves to) or silently merged (a
reshuffled index stealing another identity's rows), so the resolution tests all
run against real engines on both backends via ``real_db``/``real_adapter``.

The sweep tests pin the other phase-5 promise: accounts are processed
SEQUENTIALLY (account 1's ``backup_all`` completes before account 2's client is
even built — Telegram tolerates N sessions, not N concurrent full sweeps from
one box), one account's failure does not consume the others' turns, and log
output identifies accounts by index or row id only — never by phone, label or
Telegram user id (#272).
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select, text

from src.config import Config
from src.db.models import DEFAULT_ACCOUNT_ID, Account, Chat, Message

# NOTE: src.telegram_backup members are imported inside the sweep tests, not
# here. Other test files (test_flood_wait_visibility, test_telegram_proxy)
# reload that module around a stubbed src.db; a from-import at collection time
# would freeze the pre-reload objects and make these tests order-dependent —
# patch targets and the code under test would come from different module
# generations.

# Obviously-fake Telegram user ids (synthetic archives only). Real user ids are
# PII; these exist to prove they never reach logs.
UID_ONE = 900001111
UID_TWO = 900002222

CHAT_ID = -100901
MESSAGE_ID = 601
BASE_DATE = datetime(2026, 8, 16, 12, 0, 0)


def _message_data(message_text: str) -> dict:
    return {
        "id": MESSAGE_ID,
        "chat_id": CHAT_ID,
        "sender_id": 4242,
        "date": BASE_DATE,
        "text": message_text,
        "raw_data": {},
    }


async def _seed_migrated_accounts_table(db) -> None:
    """Shape ``accounts`` exactly as migration 022 leaves a 7.x archive.

    Row 1 is ``('default', NULL)`` — the label 022 seeds, and no Telegram user
    id because pre-8.0 rows never stored one. On PostgreSQL the seed writes the
    id explicitly, which does not advance the SERIAL sequence, so mirror 022's
    ``setval`` — without it the next DEFAULT insert would collide with the
    seeded row, a failure mode production cannot have.
    """
    async with db.async_session_factory() as session:
        await session.merge(Account(id=DEFAULT_ACCOUNT_ID, label="default", telegram_user_id=None))
        await session.commit()
    if db.engine.dialect.name == "postgresql":
        async with db.engine.begin() as conn:
            await conn.execute(
                text("SELECT setval(pg_get_serial_sequence('accounts', 'id'), (SELECT MAX(id) FROM accounts))")
            )


async def _seed_archive_owned_by_row_one(adapter) -> None:
    """A migrated archive's data: one chat and one message under account 1."""
    await adapter.upsert_chat(
        {"id": CHAT_ID, "type": "supergroup", "title": "pre-8.0 archive"}, account_id=DEFAULT_ACCOUNT_ID
    )
    await adapter.insert_message(
        _message_data("kept since 7.x"),
        account_id=DEFAULT_ACCOUNT_ID,
    )


async def _account_rows(db) -> list[Account]:
    async with db.async_session_factory() as session:
        return list((await session.execute(select(Account).order_by(Account.id))).scalars())


async def _message_owned_by(db, account_id: int) -> Message | None:
    async with db.async_session_factory() as session:
        return await session.get(Message, (account_id, CHAT_ID, MESSAGE_ID))


class TestEnsureAccountResolution:
    """``ensure_account`` on real engines: idempotent, claim-once, steal-never."""

    async def test_two_startups_same_user_id_resolve_to_one_row(self, real_adapter, caplog):
        """(a) Re-running never duplicates: same user id -> same row, once.

        Also the PII gate: resolution happens right after login, exactly where
        the user id is in hand, so assert it never reaches a log record (#272).
        """
        await _seed_migrated_accounts_table(real_adapter.db_manager)

        with caplog.at_level(logging.DEBUG):
            first = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")
            second = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")

        assert first == second == DEFAULT_ACCOUNT_ID
        rows = await _account_rows(real_adapter.db_manager)
        assert len(rows) == 1
        assert rows[0].telegram_user_id == UID_ONE
        # Scope: the app's own loggers. Third-party driver channels (aiosqlite)
        # echo every bound parameter at DEBUG by design; the app-side defence
        # there is ``hide_parameters=True`` on the engine, and #272 is about
        # what OUR log lines say — at most 'account <env_index> -> row <id>'.
        for record in caplog.records:
            if record.name.startswith("src"):
                assert str(UID_ONE) not in record.getMessage()

    async def test_fresh_database_insert_path_is_idempotent_too(self, real_adapter):
        """(a) No seeded row at all (create_all fallback): insert, then reuse.

        The claim guard's WHERE must MISS an absent row 1, not explode on it.
        """
        first = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")
        second = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")
        assert first == second
        assert len(await _account_rows(real_adapter.db_manager)) == 1

    async def test_account_one_claims_the_migrated_row_and_its_archive(self, real_adapter):
        """(b) The zero-config upgrade: row 1 (NULL) is claimed, data stays owned.

        A migrated archive's rows all carry ``account_id=1`` and row 1 carries
        no user id. The account at env index 1 must resolve to THAT row — the
        message written under 7.x must be reachable under the id returned —
        or the whole pre-8.0 archive is orphaned under a row nobody maps to.
        """
        await _seed_migrated_accounts_table(real_adapter.db_manager)
        await _seed_archive_owned_by_row_one(real_adapter)

        row_id = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")

        assert row_id == DEFAULT_ACCOUNT_ID
        rows = await _account_rows(real_adapter.db_manager)
        assert len(rows) == 1, "claiming must rewrite row 1, never add a row"
        assert rows[0].telegram_user_id == UID_ONE
        surviving = await _message_owned_by(real_adapter.db_manager, row_id)
        assert surviving is not None, "the 7.x archive must be owned by the resolved row"
        assert surviving.text == "kept since 7.x"

    async def test_second_account_gets_its_own_row_and_writes_do_not_cross(self, real_adapter):
        """(c) Account 2 lands under a NEW row; account 1's rows stay untouched."""
        await _seed_migrated_accounts_table(real_adapter.db_manager)
        await _seed_archive_owned_by_row_one(real_adapter)
        row_one = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")

        row_two = await real_adapter.ensure_account(telegram_user_id=UID_TWO, env_index=2, label="account2")

        assert row_two == 2, "the sequence must be past the seeded row (migration 022's setval)"
        assert row_two != row_one

        # Account 2 archives the very same coordinates; both copies must exist.
        await real_adapter.upsert_chat(
            {"id": CHAT_ID, "type": "supergroup", "title": "copy of two"}, account_id=row_two
        )
        await real_adapter.insert_message(_message_data("written by two"), account_id=row_two)

        one_copy = await _message_owned_by(real_adapter.db_manager, row_one)
        two_copy = await _message_owned_by(real_adapter.db_manager, row_two)
        assert one_copy.text == "kept since 7.x"
        assert two_copy.text == "written by two"
        async with real_adapter.db_manager.async_session_factory() as session:
            chats_of_one = (
                await session.execute(select(func.count()).select_from(Chat).where(Chat.account_id == row_one))
            ).scalar()
        assert chats_of_one == 1

    async def test_reshuffled_index_finds_the_same_row_and_steals_nothing(self, real_adapter):
        """(d) The user id owns the row: index 2 -> index 1 is a no-op.

        The second start also puts UID_TWO at env index 1, so a broken
        resolution keyed on the index would claim the migrated row — assert
        row 1 keeps ``telegram_user_id NULL``, exactly as it started.
        """
        await _seed_migrated_accounts_table(real_adapter.db_manager)
        original = await real_adapter.ensure_account(telegram_user_id=UID_TWO, env_index=2, label="account2")

        moved = await real_adapter.ensure_account(telegram_user_id=UID_TWO, env_index=1, label="account2")

        assert moved == original
        rows = await _account_rows(real_adapter.db_manager)
        assert len(rows) == 2
        assert rows[0].id == DEFAULT_ACCOUNT_ID
        assert rows[0].telegram_user_id is None, "reshuffling must never claim the migrated row"
        assert rows[1].telegram_user_id == UID_TWO

    async def test_an_already_claimed_row_one_is_never_stolen(self, real_adapter):
        """(d) A DIFFERENT identity at index 1 falls through to a new row.

        Once row 1 belongs to UID_ONE, the guarded claim (``WHERE id=1 AND
        telegram_user_id IS NULL``) must miss for UID_TWO even at env index 1;
        anything else would merge two identities' archives under one row.
        """
        await _seed_migrated_accounts_table(real_adapter.db_manager)
        row_one = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")

        row_two = await real_adapter.ensure_account(telegram_user_id=UID_TWO, env_index=1, label="intruder")

        assert row_one == DEFAULT_ACCOUNT_ID
        assert row_two != row_one
        rows = await _account_rows(real_adapter.db_manager)
        assert rows[0].telegram_user_id == UID_ONE, "the claimed row keeps its owner"

    async def test_label_follows_the_env_on_every_start(self, real_adapter):
        """The env is the display-name source of truth: a changed label lands."""
        await _seed_migrated_accounts_table(real_adapter.db_manager)
        row_id = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="default")

        again = await real_adapter.ensure_account(telegram_user_id=UID_ONE, env_index=1, label="personal")

        assert again == row_id
        rows = await _account_rows(real_adapter.db_manager)
        assert rows[0].label == "personal"


# ---------------------------------------------------------------------------
# The sequential sweep (run_backup over config.accounts)
# ---------------------------------------------------------------------------

PHONE_ONE = "+34600000001"  # obviously-fake; exist to prove they never reach logs
PHONE_TWO = "+34600000002"

# Session-file basename -> the user id Telegram "answers" with for that session.
# Keyed on the session file because that is the one per-account value the client
# constructor receives that the fake can read back out.
UID_BY_SESSION = {"telegram_backup": UID_ONE, "telegram_backup_account2": UID_TWO}


def _two_account_env(tmp_path) -> dict:
    return {
        "CHAT_TYPES": "private",
        "BACKUP_PATH": str(tmp_path / "backups"),
        "SESSION_DIR": str(tmp_path / "session"),
        "DATABASE_PATH": str(tmp_path / "runtime.db"),
        "TG_ACCOUNT_1_API_ID": "10001",
        "TG_ACCOUNT_1_API_HASH": "test-hash-account-1",
        "TG_ACCOUNT_1_PHONE_NUMBER": PHONE_ONE,
        "TG_ACCOUNT_2_API_ID": "10002",
        "TG_ACCOUNT_2_API_HASH": "test-hash-account-2",
        "TG_ACCOUNT_2_PHONE_NUMBER": PHONE_TWO,
    }


class _SweepHarness:
    """Everything below ``backup_all`` faked; everything ABOVE it real.

    The Telethon client class is a mock factory (no network, no real session)
    and ``backup_all`` is a recorder — but ``TelegramBackup.create``,
    ``connect()`` and the account resolver all run for real, against the real
    adapter on a real SQLite file (DATABASE_PATH above). So the sweep tests
    prove the shipped composition path: per-account client construction, the
    deferred ``ensure_account`` resolution keyed on that client's ``get_me()``,
    and only then the sweep.
    """

    def __init__(self, fail_indexes: set[int] = frozenset()):
        self.fail_indexes = fail_indexes  # AccountConfig.index values whose sweep raises
        self.events: list[tuple[str, int]] = []  # (event, resolved accounts.id)
        self.constructed_clients: list[tuple[str, int, str]] = []  # (session_path, api_id, api_hash)
        self.clients_by_session: dict[str, MagicMock] = {}

    def fake_client(self, session_path, api_id, api_hash, **kwargs):
        # The contract pins **config.get_telegram_client_kwargs() for every
        # account — same kwargs, no per-account overrides in 8.0.0. The
        # flood threshold staying 0 is what keeps FloodWaits visible (#124).
        assert kwargs.get("flood_sleep_threshold") == 0
        self.constructed_clients.append((str(session_path), api_id, api_hash))
        uid = UID_BY_SESSION[os.path.basename(str(session_path))]
        client = MagicMock(name=f"client[{os.path.basename(str(session_path))}]")
        client.connect = AsyncMock()
        client.is_connected = MagicMock(return_value=True)
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=SimpleNamespace(id=uid))
        client.disconnect = AsyncMock()
        self.clients_by_session[os.path.basename(str(session_path))] = client
        return client

    def patches(self):
        from src.telegram_backup import TelegramBackup

        harness = self

        async def recording_backup_all(backup_self):
            # account_id here is what connect()'s resolver produced — the test
            # reads the REAL resolution, not a mock of it.
            harness.events.append(("sweep-start", backup_self.account_id))
            # A parallel implementation (gather/TaskGroup) interleaves at this
            # checkpoint, so the strict-ordering assertion genuinely can fail.
            await asyncio.sleep(0)
            if backup_self.account.index in harness.fail_indexes:
                raise RuntimeError("sweep exploded mid-dialog")
            harness.events.append(("sweep-end", backup_self.account_id))

        return (
            patch("src.telegram_backup.TelegramClient", side_effect=self.fake_client),
            patch.object(TelegramBackup, "backup_all", new=recording_backup_all),
            patch("src.repair_media_extensions.repair_media_extensions", new=AsyncMock(return_value=None)),
        )


def _accounts_in_sqlite(db_path: str) -> list[tuple[int, int | None]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT id, telegram_user_id FROM accounts ORDER BY id").fetchall()


class TestSequentialSweeps:
    """(e)/(f): the run_backup loop — order, resolved ids, failure isolation."""

    async def test_account_one_completes_before_account_two_begins(self, tmp_path):
        """(e) Strictly sequential, and each backup got its RESOLVED row id.

        Fresh database: ensure_account inserts row 1 for the account at index 1
        and row 2 for index 2, so the ids handed to ``TelegramBackup.create``
        must be exactly [1, 2] — read back from the real SQLite file, not from
        a mock — and account 1's sweep must END before account 2's STARTS.
        """
        from src.telegram_backup import run_backup

        env = _two_account_env(tmp_path)
        harness = _SweepHarness()
        with patch.dict(os.environ, env, clear=True):
            config = Config()
            client_patch, backup_all_patch, repair_patch = harness.patches()
            with client_patch, backup_all_patch, repair_patch:
                await run_backup(config)

        # The ids in the events are the ones connect()'s resolver handed each
        # backup: [1, 2] proves both the strict ordering AND that every backup
        # swept under its own resolved row id.
        assert harness.events == [
            ("sweep-start", 1),
            ("sweep-end", 1),
            ("sweep-start", 2),
            ("sweep-end", 2),
        ]
        # The exact per-account client construction the contract pins.
        assert harness.constructed_clients == [
            (config.accounts[0].session_path, 10001, "test-hash-account-1"),
            (config.accounts[1].session_path, 10002, "test-hash-account-2"),
        ]
        # The ids were minted by the real ensure_account, keyed on get_me()'s id.
        assert _accounts_in_sqlite(env["DATABASE_PATH"]) == [(1, UID_ONE), (2, UID_TWO)]

    async def test_a_failing_sweep_does_not_consume_the_next_accounts_turn(self, tmp_path, caplog):
        """(f) Account 1 raising leaves account 2's sweep intact.

        The failure must be LOGGED by exception type name and the log must not
        carry the phone number, the Telegram user id or the label — an
        account's crash report identifies it by index/row id only (#272).
        """
        from src.telegram_backup import run_backup

        env = _two_account_env(tmp_path)
        harness = _SweepHarness(fail_indexes={1})
        with patch.dict(os.environ, env, clear=True):
            config = Config()
            client_patch, backup_all_patch, repair_patch = harness.patches()
            with client_patch, backup_all_patch, repair_patch, caplog.at_level(logging.DEBUG):
                await run_backup(config)

        assert ("sweep-start", 1) in harness.events, "account 1's sweep did start"
        assert ("sweep-end", 1) not in harness.events, "account 1's sweep did fail"
        assert ("sweep-end", 2) in harness.events, "account 2 still got its full turn"
        # The failed account's teardown still ran: its client was disconnected.
        harness.clients_by_session["telegram_backup"].disconnect.assert_awaited()
        harness.clients_by_session["telegram_backup_account2"].disconnect.assert_awaited()

        # Same scope rule as the resolution tests: the app's own loggers. The
        # aiosqlite driver channel echoes bound parameters at DEBUG by design.
        log_text = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("src"))
        assert "RuntimeError" in log_text, "the failure is reported by exception type"
        for pii in (PHONE_ONE, PHONE_TWO, str(UID_ONE), str(UID_TWO)):
            assert pii not in log_text


class TestResolutionIsSerialized:
    """The sweep, a listener start and the initial backup can all ask for row
    resolution concurrently; without serialization two interleaved misses would
    both INSERT a row for the same Telegram account (accounts has no DB-level
    unique on telegram_user_id)."""

    async def test_concurrent_resolvers_call_ensure_account_once_per_account(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from unittest.mock import patch as mock_patch

        from src.scheduler import BackupScheduler, _AccountRuntime

        with mock_patch("src.scheduler.signal.signal"):
            config = MagicMock()
            scheduler = BackupScheduler.__new__(BackupScheduler)
            scheduler.config = config
            scheduler._resolve_rows_lock = asyncio.Lock()
            scheduler._accounts = [
                _AccountRuntime(
                    account=SimpleNamespace(index=n, label=f"account{n}"),
                    connection=SimpleNamespace(me=SimpleNamespace(id=900_000 + n)),
                )
                for n in (1, 2)
            ]

        calls: list[int] = []

        async def slow_ensure_account(*, telegram_user_id, env_index, label):
            calls.append(telegram_user_id)
            # Long enough that an unserialized second resolver re-reads
            # row_id=None and duplicates every call.
            await asyncio.sleep(0.05)
            return env_index

        adapter = SimpleNamespace(ensure_account=slow_ensure_account, close=AsyncMock())
        with mock_patch("src.db.create_adapter", AsyncMock(return_value=adapter)):
            await asyncio.gather(
                scheduler._resolve_account_rows(),
                scheduler._resolve_account_rows(),
            )

        assert sorted(calls) == [900_001, 900_002], calls
        assert [e.row_id for e in scheduler._accounts] == [1, 2]
