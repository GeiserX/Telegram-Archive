"""Diff the two schemas this project can produce, on both supported backends.

There are two ways this project can build a schema:

* ``alembic upgrade head``     — ``scripts/entrypoint.sh`` runs this on both
  backends before the app starts. It is the authority.
* ``Base.metadata.create_all`` — ``src/db/base.py`` falls back to this on
  SQLite for processes that never pass through that entrypoint (the viewer
  image ships no ``alembic/``), and ``src/db/migrate.py`` uses it to provision
  the target of a SQLite-to-PostgreSQL move.

Nothing compared them until this module, and they had drifted to 54 structural
differences on SQLite and 50 on PostgreSQL. It builds both, on each backend,
and reports every structural difference: tables, columns, types, nullability,
server defaults, indexes, unique constraints, primary keys and foreign keys.

``KNOWN_DIFFERENCES`` is empty, so this is a strict parity gate: a new
difference fails it, and so does an allow-list entry that no longer describes a
real difference.

The PostgreSQL leg skips when no server is reachable (see ``conftest.py``).
Both tests are deliberately synchronous: Alembic's ``env.py`` calls
``asyncio.run()``, which cannot be nested inside a running loop.
"""

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from src.db.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = REPO_ROOT / "alembic"

# Alembic's own bookkeeping table has no ORM counterpart by design.
IGNORED_TABLES = {"alembic_version"}


# ---------------------------------------------------------------------------
# Known differences. Key -> why it is here. Delete an entry when it is fixed.
# ---------------------------------------------------------------------------

# Empty, and it stays empty. Migration 021 closed all 54 differences on SQLite
# and all 50 on PostgreSQL, so the two schema authors now agree exactly and any
# difference at all — a column added to models.py without a migration, or a
# migration that lands somewhere the ORM does not — fails this gate on both
# backends. Adding an entry here silences a real divergence: fix the schema
# instead.
KNOWN_DIFFERENCES: dict[str, dict[str, str]] = {
    "sqlite": {},
    "postgresql": {},
}


# ---------------------------------------------------------------------------
# Schema capture
# ---------------------------------------------------------------------------


def _snapshot(sync_url: str) -> dict:
    """Reflect a live schema into a plain comparable structure."""
    engine = sa.create_engine(sync_url)
    try:
        inspector = sa.inspect(engine)
        schema = {}
        for table in sorted(inspector.get_table_names()):
            if table in IGNORED_TABLES:
                continue
            schema[table] = {
                "columns": {
                    column["name"]: {
                        "type": str(column["type"]),
                        "nullable": bool(column["nullable"]),
                        "server_default": None if column.get("default") is None else str(column["default"]),
                    }
                    for column in inspector.get_columns(table)
                },
                "indexes": {
                    index["name"]: {
                        "columns": list(index.get("column_names") or []),
                        "unique": bool(index.get("unique")),
                    }
                    for index in inspector.get_indexes(table)
                },
                "unique_constraints": {
                    constraint["name"]: sorted(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints(table)
                },
                "primary_key": sorted(inspector.get_pk_constraint(table).get("constrained_columns") or []),
                # Keyed by what the constraint *does*, not by its name: an
                # unnamed FK and a named one that constrain the same columns are
                # the same constraint with a naming nit, and that is a much less
                # interesting finding than a missing constraint.
                "foreign_keys": {
                    (
                        f"{','.join(fk['constrained_columns'])}"
                        f"->{fk['referred_table']}({','.join(fk['referred_columns'])})"
                    ): fk.get("name")
                    for fk in inspector.get_foreign_keys(table)
                },
            }
        return schema
    finally:
        engine.dispose()


def _diff(orm: dict, alembic: dict) -> dict[str, str]:
    """Return ``{difference key: human-readable description}``."""
    found: dict[str, str] = {}

    for table in sorted(set(orm) - set(alembic)):
        found[f"table-only-in-orm:{table}"] = f"table {table} exists only in the ORM schema"
    for table in sorted(set(alembic) - set(orm)):
        found[f"table-only-in-alembic:{table}"] = f"table {table} exists only in the Alembic schema"

    for table in sorted(set(orm) & set(alembic)):
        left, right = orm[table], alembic[table]

        for column in sorted(set(left["columns"]) - set(right["columns"])):
            found[f"column-only-in-orm:{table}.{column}"] = f"{table}.{column} exists only in the ORM schema"
        for column in sorted(set(right["columns"]) - set(left["columns"])):
            found[f"column-only-in-alembic:{table}.{column}"] = f"{table}.{column} exists only in the Alembic schema"

        for column in sorted(set(left["columns"]) & set(right["columns"])):
            lc, rc = left["columns"][column], right["columns"][column]
            if lc["nullable"] != rc["nullable"]:
                found[f"nullable:{table}.{column}"] = (
                    f"{table}.{column} nullable: ORM={lc['nullable']} Alembic={rc['nullable']}"
                )
            if lc["type"] != rc["type"]:
                found[f"type:{table}.{column}"] = f"{table}.{column} type: ORM={lc['type']} Alembic={rc['type']}"
            if lc["server_default"] != rc["server_default"]:
                found[f"server-default:{table}.{column}"] = (
                    f"{table}.{column} server default: ORM={lc['server_default']} Alembic={rc['server_default']}"
                )

        for index in sorted(set(left["indexes"]) - set(right["indexes"])):
            found[f"index-only-in-orm:{table}.{index}"] = (
                f"index {index} on {left['indexes'][index]['columns']} exists only in the ORM schema"
            )
        for index in sorted(set(right["indexes"]) - set(left["indexes"])):
            found[f"index-only-in-alembic:{table}.{index}"] = (
                f"index {index} on {right['indexes'][index]['columns']} exists only in the Alembic schema"
            )
        for index in sorted(set(left["indexes"]) & set(right["indexes"])):
            if left["indexes"][index] != right["indexes"][index]:
                found[f"index-differs:{table}.{index}"] = (
                    f"index {index}: ORM={left['indexes'][index]} Alembic={right['indexes'][index]}"
                )

        for name in sorted(set(left["unique_constraints"]) - set(right["unique_constraints"])):
            found[f"unique-only-in-orm:{table}.{name}"] = f"unique constraint {name} exists only in the ORM schema"
        for name in sorted(set(right["unique_constraints"]) - set(left["unique_constraints"])):
            found[f"unique-only-in-alembic:{table}.{name}"] = (
                f"unique constraint {name} exists only in the Alembic schema"
            )
        for name in sorted(set(left["unique_constraints"]) & set(right["unique_constraints"])):
            if left["unique_constraints"][name] != right["unique_constraints"][name]:
                found[f"unique-differs:{table}.{name}"] = (
                    f"unique constraint {name}: ORM={left['unique_constraints'][name]} "
                    f"Alembic={right['unique_constraints'][name]}"
                )

        if left["primary_key"] != right["primary_key"]:
            found[f"primary-key:{table}"] = (
                f"{table} primary key: ORM={left['primary_key']} Alembic={right['primary_key']}"
            )

        for fk in sorted(set(left["foreign_keys"]) - set(right["foreign_keys"])):
            found[f"fk-only-in-orm:{table}:{fk}"] = f"{table} FK {fk} exists only in the ORM schema"
        for fk in sorted(set(right["foreign_keys"]) - set(left["foreign_keys"])):
            found[f"fk-only-in-alembic:{table}:{fk}"] = f"{table} FK {fk} exists only in the Alembic schema"
        for fk in sorted(set(left["foreign_keys"]) & set(right["foreign_keys"])):
            if left["foreign_keys"][fk] != right["foreign_keys"][fk]:
                found[f"fk-name:{table}:{fk}"] = (
                    f"{table} FK {fk} name: ORM={left['foreign_keys'][fk]} Alembic={right['foreign_keys'][fk]}"
                )

    return found


# ---------------------------------------------------------------------------
# Schema construction
# ---------------------------------------------------------------------------


def _build_orm_schema(sync_url: str) -> None:
    engine = sa.create_engine(sync_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _build_alembic_schema(async_url: str, target: str = "head") -> None:
    """Run ``alembic upgrade`` against a database.

    A ``Config`` with no ini file is deliberate: ``alembic/env.py`` only calls
    ``fileConfig`` when ``config_file_name`` is set, and letting it run would
    reconfigure pytest's logging. env.py reads the URL from ``DATABASE_URL``,
    so that is what has to be set.
    """
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", async_url)

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = async_url
    try:
        command.upgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _assert_parity(backend: str, orm: dict, alembic: dict, allowed: dict[str, str]) -> None:
    """Fail unless the ORM/Alembic diff is exactly ``allowed``.

    ``allowed`` is passed in rather than looked up so the positive controls
    below can exercise this function against a diff they control.
    """
    __tracebackhide__ = True  # the report is the message; the frame locals are noise
    found = _diff(orm, alembic)

    new = {key: text for key, text in found.items() if key not in allowed}
    stale = sorted(key for key in allowed if key not in found)

    if not new and not stale:
        return

    report = [f"ORM vs Alembic schema parity failed on {backend}.", ""]
    if new:
        report.append(f"{len(new)} NEW difference(s) — the two schema paths just drifted further apart:")
        report += [f"  {key}\n      {text}" for key, text in sorted(new.items())]
        report.append("")
    if stale:
        report.append(
            f"{len(stale)} allow-listed difference(s) no longer exist. Delete them from "
            f"KNOWN_DIFFERENCES['{backend}'] so the allow-list keeps shrinking:"
        )
        report += [f"  {key}" for key in stale]
        report.append("")
    report.append(f"Full diff on {backend} ({len(found)} difference(s) total):")
    report += [f"  {key}\n      {text}" for key, text in sorted(found.items())]
    raise AssertionError("\n".join(report))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_orm_and_alembic_schemas_agree_on_sqlite(tmp_path):
    """A create_all SQLite install and a migrated one must be the same schema."""
    orm_db = tmp_path / "orm.db"
    alembic_db = tmp_path / "alembic.db"

    _build_orm_schema(f"sqlite:///{orm_db}")
    _build_alembic_schema(f"sqlite+aiosqlite:///{alembic_db}")

    _assert_parity(
        "sqlite",
        _snapshot(f"sqlite:///{orm_db}"),
        _snapshot(f"sqlite:///{alembic_db}"),
        KNOWN_DIFFERENCES["sqlite"],
    )


def test_stepwise_upgrade_lands_where_a_jump_to_head_lands(tmp_path):
    """One-revision-at-a-time must reach the same schema as a single jump.

    The parity tests build the Alembic side with one ``upgrade head``. A real
    long-lived install got there one migration at a time. If the two routes
    diverged, the parity result would describe a schema nobody actually runs,
    and a migration that only works when jumped over would go unnoticed.
    """
    stepwise = tmp_path / "stepwise.db"
    jumped = tmp_path / "jumped.db"

    _build_alembic_schema(f"sqlite+aiosqlite:///{jumped}")
    revisions = list(reversed(list(ScriptDirectory(str(ALEMBIC_DIR)).walk_revisions())))
    for revision in revisions:
        _build_alembic_schema(f"sqlite+aiosqlite:///{stepwise}", revision.revision)

    differences = _diff(_snapshot(f"sqlite:///{stepwise}"), _snapshot(f"sqlite:///{jumped}"))
    assert not differences, "stepwise upgrade and `upgrade head` produced different schemas:\n" + "\n".join(
        f"  {key}: {text}" for key, text in sorted(differences.items())
    )


def test_orm_and_alembic_schemas_agree_on_postgresql(require_postgres, make_postgres_database):
    """Same check on PostgreSQL, where Alembic is the only schema author."""
    _, orm_sync_url = make_postgres_database("telegram_archive_parity_orm")
    alembic_async_url, alembic_sync_url = make_postgres_database("telegram_archive_parity_alembic")

    _build_orm_schema(orm_sync_url)
    _build_alembic_schema(alembic_async_url)

    _assert_parity(
        "postgresql",
        _snapshot(orm_sync_url),
        _snapshot(alembic_sync_url),
        KNOWN_DIFFERENCES["postgresql"],
    )


class TestDiffCanGoRed:
    """Positive controls: prove ``_diff`` actually detects each difference.

    An empty diff is the pass condition of the two tests above, and an empty
    diff is also what a broken comparator returns. These pin the comparator so
    a silent pass cannot be mistaken for parity.
    """

    @staticmethod
    def _base() -> dict:
        return {
            "t": {
                "columns": {"c": {"type": "INTEGER", "nullable": False, "server_default": None}},
                "indexes": {"ix_c": {"columns": ["c"], "unique": False}},
                "unique_constraints": {"uq_c": ["c"]},
                "primary_key": ["c"],
                "foreign_keys": {"c->other(id)": "fk_c"},
            }
        }

    def test_identical_schemas_produce_no_differences(self):
        assert _diff(self._base(), self._base()) == {}

    def test_detects_a_missing_table(self):
        assert "table-only-in-orm:t" in _diff(self._base(), {})

    def test_detects_an_extra_table(self):
        assert "table-only-in-alembic:t" in _diff({}, self._base())

    def test_detects_a_missing_column(self):
        other = self._base()
        other["t"]["columns"] = {}
        assert "column-only-in-orm:t.c" in _diff(self._base(), other)

    def test_detects_a_nullability_change(self):
        other = self._base()
        other["t"]["columns"]["c"]["nullable"] = True
        assert "nullable:t.c" in _diff(self._base(), other)

    def test_detects_a_type_change(self):
        other = self._base()
        other["t"]["columns"]["c"]["type"] = "BIGINT"
        assert "type:t.c" in _diff(self._base(), other)

    def test_detects_a_server_default_change(self):
        other = self._base()
        other["t"]["columns"]["c"]["server_default"] = "0"
        assert "server-default:t.c" in _diff(self._base(), other)

    def test_detects_a_missing_index(self):
        other = self._base()
        other["t"]["indexes"] = {}
        assert "index-only-in-orm:t.ix_c" in _diff(self._base(), other)

    def test_detects_a_changed_index(self):
        other = self._base()
        other["t"]["indexes"]["ix_c"]["unique"] = True
        assert "index-differs:t.ix_c" in _diff(self._base(), other)

    def test_detects_a_missing_unique_constraint(self):
        other = self._base()
        other["t"]["unique_constraints"] = {}
        assert "unique-only-in-orm:t.uq_c" in _diff(self._base(), other)

    def test_detects_a_changed_primary_key(self):
        other = self._base()
        other["t"]["primary_key"] = []
        assert "primary-key:t" in _diff(self._base(), other)

    def test_detects_a_missing_foreign_key(self):
        other = self._base()
        other["t"]["foreign_keys"] = {}
        assert "fk-only-in-orm:t:c->other(id)" in _diff(self._base(), other)

    def test_detects_a_renamed_foreign_key(self):
        other = self._base()
        other["t"]["foreign_keys"]["c->other(id)"] = None
        assert "fk-name:t:c->other(id)" in _diff(self._base(), other)

    def test_new_difference_fails_the_gate(self):
        """An unlisted difference must fail, not be quietly tolerated."""
        other = self._base()
        other["t"]["columns"]["c"]["nullable"] = True
        with pytest.raises(AssertionError) as excinfo:
            _assert_parity("sqlite", self._base(), other, allowed={})
        assert "NEW difference" in str(excinfo.value)
        assert "nullable:t.c" in str(excinfo.value)

    def test_allow_listed_difference_passes_the_gate(self):
        """The listed difference is tolerated — that is what keeps this green."""
        other = self._base()
        other["t"]["columns"]["c"]["nullable"] = True
        _assert_parity("sqlite", self._base(), other, allowed={"nullable:t.c": "known"})

    def test_stale_allow_list_entry_fails_the_gate(self):
        """A fixed difference must fail until its allow-list entry is removed."""
        with pytest.raises(AssertionError) as excinfo:
            _assert_parity("sqlite", self._base(), self._base(), allowed={"nullable:t.c": "already fixed"})
        assert "no longer exist" in str(excinfo.value)

    def test_every_allow_list_entry_carries_a_reason(self):
        """An entry with no explanation is a silenced failure, not a decision."""
        for backend, entries in KNOWN_DIFFERENCES.items():
            for key, reason in entries.items():
                assert reason.strip(), f"{backend}:{key} has no reason"
