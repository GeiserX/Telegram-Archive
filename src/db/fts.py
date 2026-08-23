"""Full-text search plumbing shared by the migration, the adapter and tests.

SQLite gets an external-content FTS5 table over ``messages.text`` kept in
sync by triggers; PostgreSQL gets a STORED generated tsvector column with a
GIN index. Both tokenize language-neutrally (``unicode61`` / ``'simple'``) —
Telegram archives are multilingual, so stemming would help one language and
hurt the rest. One module owns the SQL so the migration, the capability
probe and the test fixtures can never drift apart.

Query semantics on both engines: every word of the search is a required
prefix (``hola mun`` matches a message containing ``hola`` and ``mundo``) —
the official clients' word-prefix behavior, replacing ILIKE's substring
scan. Input is reduced to word characters before it reaches either query
parser, so FTS5 operators and tsquery syntax cannot be injected.
"""

import re

SQLITE_FTS_TABLE = "messages_fts"

SQLITE_CREATE_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "text, content='messages', content_rowid='rowid', "
    "tokenize='unicode61 remove_diacritics 2'"
    ")"
)

# External-content protocol: the index never stores the text itself, so
# deletes/updates must hand FTS5 the OLD value to un-index.
SQLITE_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END""",
)

SQLITE_REBUILD = "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')"

PG_TSVECTOR_COLUMN = "text_search"
PG_ADD_COLUMN = (
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS text_search tsvector "
    "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
)
PG_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_messages_text_search ON messages USING GIN (text_search)"

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def fts_match_query(search: str) -> str | None:
    """FTS5 MATCH string: every word as a quoted prefix term (``"tok"*``).

    Word extraction (``\\w+``) mirrors unicode61's own alnum tokenization
    ("covid-19" becomes the terms covid and 19 in the index, so it must
    become two query terms too) and leaves nothing that FTS5's parser could
    read as an operator. None when no word survives — callers fall back to
    ILIKE for punctuation-only searches.
    """
    tokens = [f'"{word}"*' for word in _WORD_RE.findall(search or "")]
    return " ".join(tokens) or None


def pg_tsquery(search: str) -> str | None:
    """``to_tsquery('simple', ...)`` input: ``'tok':* & 'tok2':*``.

    Same word extraction as the FTS5 side so both engines agree on what a
    query means; quoting each lexeme keeps tsquery syntax uninjectable.
    """
    tokens = [f"'{word}':*" for word in _WORD_RE.findall(search or "")]
    return " & ".join(tokens) or None


def install_fts_ddl_listener(metadata) -> None:
    """Make ``create_all()`` produce the same FTS layer migration 028 does.

    The schema has two authors — the ORM (fresh databases) and Alembic
    (upgrades) — and the parity gate requires them to agree exactly. The
    layer is not a model (FTS5 is a virtual table; the tsvector column is
    generated), so the ORM side installs it here, right after its tables
    are created. Idempotent on both engines; skipped on SQLite builds
    without FTS5 (the adapter probes and keeps ILIKE there).
    """
    from sqlalchemy import event

    @event.listens_for(metadata, "after_create")
    def _create_fts_layer(target, connection, **kw):
        if connection.dialect.name == "sqlite":
            fts5_row = connection.exec_driver_sql(
                "SELECT 1 FROM pragma_compile_options WHERE compile_options='ENABLE_FTS5'"
            ).first()
            if fts5_row is None:
                return
            connection.exec_driver_sql(SQLITE_CREATE_FTS)
            for trigger_sql in SQLITE_TRIGGERS:
                connection.exec_driver_sql(trigger_sql)
        elif connection.dialect.name == "postgresql":
            connection.exec_driver_sql(PG_ADD_COLUMN)
            connection.exec_driver_sql(PG_CREATE_INDEX)
