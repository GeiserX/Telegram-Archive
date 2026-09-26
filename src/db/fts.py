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

# Transcript search (migration 032, docs/TRANSCRIPTION.md). A PostgreSQL
# generated column cannot read another table, so transcripts get their own
# objects, built by the same parser as the messages ones. The SQLite index
# keys on ``id``: media_transcripts has a single INTEGER primary key, which
# SQLite aliases to the rowid, unlike the composite key of messages.
SQLITE_TRANSCRIPT_FTS_TABLE = "media_transcripts_fts"

SQLITE_CREATE_TRANSCRIPT_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS media_transcripts_fts USING fts5("
    "text, content='media_transcripts', content_rowid='id', "
    "tokenize='unicode61 remove_diacritics 2'"
    ")"
)

SQLITE_TRANSCRIPT_TRIGGER_NAMES = ("media_transcripts_fts_ai", "media_transcripts_fts_ad", "media_transcripts_fts_au")

SQLITE_TRANSCRIPT_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS media_transcripts_fts_ai AFTER INSERT ON media_transcripts BEGIN
  INSERT INTO media_transcripts_fts(rowid, text) VALUES (new.id, new.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS media_transcripts_fts_ad AFTER DELETE ON media_transcripts BEGIN
  INSERT INTO media_transcripts_fts(media_transcripts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS media_transcripts_fts_au AFTER UPDATE OF text ON media_transcripts BEGIN
  INSERT INTO media_transcripts_fts(media_transcripts_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO media_transcripts_fts(rowid, text) VALUES (new.id, new.text);
END""",
)

SQLITE_TRANSCRIPT_REBUILD = "INSERT INTO media_transcripts_fts(media_transcripts_fts) VALUES ('rebuild')"

PG_TRANSCRIPT_ADD_COLUMN = (
    "ALTER TABLE media_transcripts ADD COLUMN IF NOT EXISTS text_search tsvector "
    "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
)
PG_TRANSCRIPT_INDEX_NAME = "idx_media_transcripts_text_search"
PG_TRANSCRIPT_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_media_transcripts_text_search ON media_transcripts USING GIN (text_search)"
)

# NOT \w+: unicode61 treats '_' as a separator (\w keeps it), so foo_bar is
# indexed as foo,bar and the query must split the same way.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def fts_match_query(search: str) -> str | None:
    """FTS5 MATCH string: every word as a quoted prefix term (``"tok"*``).

    Word extraction mirrors unicode61's own tokenization ("covid-19" and
    "foo_bar" are both two terms in the index, so each must become two
    required query terms) and leaves nothing that FTS5's parser could read
    as an operator. None when no word survives — callers fall back to ILIKE
    for punctuation-only searches.
    """
    tokens = [f'"{word}"*' for word in _WORD_RE.findall(search or "")]
    return " ".join(tokens) or None


def search_has_words(search: str) -> bool:
    """Gate for the indexed path: does the search carry any word at all?"""
    return _WORD_RE.search(search or "") is not None


# PostgreSQL builds the query from ITS OWN parser's lexemes: 'simple' keeps
# token classes \w+ cannot predict (covid-19 -> covid,'-19'; foo@bar.com is
# ONE email lexeme), so a Python-side split produced prefixes like '19':*
# that can never match the indexed '-19'. Aggregating quote_literal'd
# lexemes from to_tsvector makes the query agree with the index for every
# token class, keeps word-prefix AND semantics, and is injection-proof by
# construction — the search string only ever travels as a bind parameter
# into to_tsvector. (Verified against postgres:16-alpine: covid-19, email
# and hostile-quote inputs all behave; see PR #404.)
PG_TSQUERY_FROM_SEARCH = (
    "(SELECT to_tsquery('simple', string_agg(quote_literal(lexeme) || ':*', ' & ')) "
    "FROM unnest(to_tsvector('simple', :fts_search)))"
)

# The same query built for the transcript side of a search. Its own bind name:
# chat and global search put both sides in one statement, and two text()
# clauses may not share a bind parameter name.
PG_TRANSCRIPT_TSQUERY_FROM_SEARCH = PG_TSQUERY_FROM_SEARCH.replace(":fts_search", ":transcript_search")


def sqlite_has_fts5(connection) -> bool:
    """Whether this SQLite build compiled FTS5 in (exotic builds leave it out)."""
    row = connection.exec_driver_sql("SELECT 1 FROM pragma_compile_options WHERE compile_options='ENABLE_FTS5'").first()
    return row is not None


def install_fts_ddl_listener(metadata) -> None:
    """Make ``create_all()`` produce the same FTS layer migrations 028 and 032 do.

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
            if not sqlite_has_fts5(connection):
                return
            connection.exec_driver_sql(SQLITE_CREATE_FTS)
            for trigger_sql in SQLITE_TRIGGERS:
                connection.exec_driver_sql(trigger_sql)
            connection.exec_driver_sql(SQLITE_CREATE_TRANSCRIPT_FTS)
            for trigger_sql in SQLITE_TRANSCRIPT_TRIGGERS:
                connection.exec_driver_sql(trigger_sql)
        elif connection.dialect.name == "postgresql":
            connection.exec_driver_sql(PG_ADD_COLUMN)
            connection.exec_driver_sql(PG_CREATE_INDEX)
            connection.exec_driver_sql(PG_TRANSCRIPT_ADD_COLUMN)
            connection.exec_driver_sql(PG_TRANSCRIPT_CREATE_INDEX)
