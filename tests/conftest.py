"""Shared pytest fixtures.

The point of this file is ``real_adapter``: a genuine :class:`DatabaseAdapter`
bound to a genuine engine, parameterised over SQLite *and* PostgreSQL.

Why it exists
-------------
Most adapter tests build a ``MagicMock`` DatabaseManager and assert that
``session.execute`` was awaited. That proves the Python branch was taken; it
does not prove the statement compiles, and it certainly does not prove the
database accepts it. Every dialect-specific branch in ``src/db/adapter.py``
(``sqlite_insert`` vs ``pg_insert``, ``SELECT ... FOR UPDATE`` vs the SQLite
no-op-write lock, ``ilike`` escaping) is therefore untested against the backend
it exists for. A test that cannot go red is not a test.

Using it
--------
Request ``real_adapter`` (or ``real_db`` for the manager itself) and the test
runs twice: once on a throwaway SQLite file, once on PostgreSQL. The PostgreSQL
leg skips cleanly when no server is reachable, so a laptop without Docker still
gets a green suite — it just gets half the coverage, and says so.

Pointing it at a server
-----------------------
``TEST_POSTGRES_URL`` (preferred) or ``DATABASE_URL``. Either way the suite
connects to the *server* in that URL but never reads or writes the database
named in it: it creates and uses a dedicated ``telegram_archive_pytest``
database, whose ``public`` schema is dropped and rebuilt once per session.
"""

import os
from urllib.parse import urlsplit, urlunsplit

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base

# Backends every ``real_adapter`` test runs against. PostgreSQL is a
# first-class supported backend, so it is in this list unconditionally and is
# skipped only when no server answers.
REAL_BACKENDS = ("sqlite", "postgresql")

# The suite owns this database entirely and recreates its schema each session.
# It is never the database named in TEST_POSTGRES_URL/DATABASE_URL.
POSTGRES_TEST_DB = "telegram_archive_pytest"

_POSTGRES_SCHEMES = ("postgresql", "postgres")

# Skip reason for the PostgreSQL leg. .github/workflows/tests.yml greps pytest's
# `-rs` output for this exact string and fails the job if it appears: in CI a
# skipped PostgreSQL leg is a broken service container, not a valid outcome.
# Change it here and there together.
NO_POSTGRES_REASON = "no PostgreSQL server reachable — set TEST_POSTGRES_URL or DATABASE_URL"


def rewrite_url(url: str, *, driver: str | None = None, database: str | None = None) -> str:
    """Return ``url`` with its DBAPI driver and/or database name replaced.

    ``postgres://`` is normalised to ``postgresql://`` because SQLAlchemy 2.x
    rejects the legacy scheme outright.
    """
    parts = urlsplit(url)
    backend = parts.scheme.split("+", 1)[0]
    if backend == "postgres":
        backend = "postgresql"
    scheme = f"{backend}+{driver}" if driver else backend
    path = f"/{database}" if database is not None else parts.path
    return urlunsplit((scheme, parts.netloc, path, parts.query, parts.fragment))


def _configured_postgres_url() -> str | None:
    """Read the PostgreSQL URL the developer or CI supplied, if any."""
    for var in ("TEST_POSTGRES_URL", "DATABASE_URL"):
        raw = (os.getenv(var) or "").strip()
        if raw and urlsplit(raw).scheme.split("+", 1)[0] in _POSTGRES_SCHEMES:
            return raw
    return None


@pytest.fixture(scope="session")
def postgres_server_url() -> str | None:
    """A reachable PostgreSQL server URL, or ``None``.

    Probing here (once per session) is what lets the PostgreSQL leg skip
    cleanly instead of erroring on every test that wants it.
    """
    url = _configured_postgres_url()
    if not url:
        return None
    engine = sa.create_engine(rewrite_url(url, driver="psycopg2"), connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depends on the environment
        # Type name only: a connection error can carry the host and user.
        print(f"PostgreSQL leg disabled: {type(exc).__name__} while probing the configured server")
        return None
    finally:
        engine.dispose()
    return url


def create_postgres_database(server_url: str, database: str) -> str:
    """Create ``database`` on ``server_url`` with an empty ``public`` schema.

    Returns the asyncpg URL for it. Safe to call repeatedly: the database is
    created only if missing, and its schema is always reset, so a run never
    inherits objects from the previous one.
    """
    admin = sa.create_engine(
        rewrite_url(server_url, driver="psycopg2"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}).scalar()
            if not exists:
                # Identifier is a module constant, never user input.
                conn.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        admin.dispose()

    target = sa.create_engine(
        rewrite_url(server_url, driver="psycopg2", database=database),
        connect_args={"connect_timeout": 5},
    )
    try:
        with target.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        target.dispose()

    return rewrite_url(server_url, driver="asyncpg", database=database)


@pytest.fixture
def require_postgres(postgres_server_url: str | None) -> str:
    """Skip the requesting test unless a PostgreSQL server is reachable."""
    if not postgres_server_url:
        pytest.skip(NO_POSTGRES_REASON)
    return postgres_server_url


@pytest.fixture(scope="session")
def make_postgres_database(postgres_server_url: str | None):
    """Factory: ``name -> (asyncpg url, psycopg2 url)`` for a clean database.

    Used by the schema-parity harness, which needs two independent databases to
    build the ORM schema and the Alembic schema side by side.
    """

    def factory(name: str) -> tuple[str, str]:
        if not postgres_server_url:
            raise RuntimeError("no PostgreSQL server reachable")
        async_url = create_postgres_database(postgres_server_url, name)
        return async_url, rewrite_url(async_url, driver="psycopg2")

    return factory


@pytest.fixture(scope="session")
def postgres_test_url(postgres_server_url: str | None) -> str | None:
    """asyncpg URL for a dedicated, ORM-provisioned PostgreSQL test database."""
    if not postgres_server_url:
        return None
    async_url = create_postgres_database(postgres_server_url, POSTGRES_TEST_DB)
    sync_engine = sa.create_engine(
        rewrite_url(postgres_server_url, driver="psycopg2", database=POSTGRES_TEST_DB),
        connect_args={"connect_timeout": 5},
    )
    try:
        with sync_engine.begin() as conn:
            Base.metadata.create_all(conn)
    finally:
        sync_engine.dispose()
    return async_url


async def _truncate_all(manager: DatabaseManager) -> None:
    """Empty every ORM table so each test starts from a known state."""
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with manager.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(params=REAL_BACKENDS)
async def real_db(request, postgres_test_url, tmp_path):
    """An initialised :class:`DatabaseManager` on a real engine, per backend."""
    if request.param == "postgresql":
        if not postgres_test_url:
            pytest.skip(NO_POSTGRES_REASON)
        manager = DatabaseManager(postgres_test_url)
        await manager.init()
        await _truncate_all(manager)
    else:
        manager = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}")
        await manager.init()

    try:
        yield manager
    finally:
        await manager.close()


@pytest.fixture
async def real_adapter(real_db):
    """A :class:`DatabaseAdapter` whose SQL is really compiled and really run."""
    return DatabaseAdapter(real_db)
