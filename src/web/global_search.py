from typing import Any

from sqlalchemy import text

from ..db.fts import PG_TSQUERY_FROM_SEARCH, SQLITE_FTS_TABLE, fts_match_query, search_has_words


def _scope_sql(scope: Any, params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    grants = (
        ("chat_id", "c.id", scope.ids),
        ("account_id", "c.account_id", scope.accounts),
        ("chat_ref", "c.ref", scope.refs),
    )
    for prefix, column, grant in grants:
        if grant is None:
            continue
        if not grant:
            return ["1 = 0"]
        placeholders = []
        for index, value in enumerate(sorted(grant, key=str)):
            name = f"scope_{prefix}_{index}"
            placeholders.append(f":{name}")
            params[name] = value
        clauses.append(f"{column} IN ({', '.join(placeholders)})")
    return clauses


async def search_messages_global(
    db: Any,
    *,
    query: str,
    scope: Any,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Search message text across every chat visible in ``scope``.

    Uses the v8.5 FTS5 index on SQLite and the generated tsvector/GIN index on
    PostgreSQL. The extra row is fetched only to determine ``has_more``.
    """
    if not search_has_words(query):
        return [], False

    params: dict[str, Any] = {"limit": limit + 1, "offset": offset}
    where = ["m.text IS NOT NULL", "m.text <> ''"]
    where.extend(_scope_sql(scope, params))

    if db._is_sqlite:
        fts_query = fts_match_query(query)
        if not fts_query:
            return [], False
        params["fts_match"] = fts_query
        from_sql = (
            f"{SQLITE_FTS_TABLE} AS f "
            "JOIN messages AS m ON m.rowid = f.rowid "
            "JOIN chats AS c ON c.account_id = m.account_id AND c.id = m.chat_id"
        )
        where.insert(0, "f.messages_fts MATCH :fts_match")
    else:
        params["fts_search"] = query
        from_sql = (
            "messages AS m "
            "JOIN chats AS c ON c.account_id = m.account_id AND c.id = m.chat_id"
        )
        where.insert(0, f"m.text_search @@ {PG_TSQUERY_FROM_SEARCH}")

    sql = text(
        "SELECT "
        "m.account_id, m.chat_id, m.id AS message_id, m.sender_id, m.sender_name, "
        "m.date, m.text, m.reply_to_msg_id, m.reply_to_top_id, "
        "c.ref AS chat_ref, c.title AS chat_title, c.type AS chat_type "
        f"FROM {from_sql} "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY m.date DESC, m.id DESC "
        "LIMIT :limit OFFSET :offset"
    )

    async with db.db_manager.async_session_factory() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    results = []
    for row in rows:
        date_value = row["date"]
        if date_value is not None and hasattr(date_value, "isoformat"):
            date_value = date_value.isoformat()
        results.append(
            {
                "account_id": row["account_id"],
                "chat_id": row["chat_id"],
                "chat": {
                    "ref": row["chat_ref"],
                    "title": row["chat_title"],
                    "type": row["chat_type"],
                },
                "message_id": row["message_id"],
                "sender_id": row["sender_id"],
                "sender_name": row["sender_name"],
                "date": date_value,
                "text": row["text"],
                "reply_to_msg_id": row["reply_to_msg_id"],
                "reply_to_top_id": row["reply_to_top_id"],
            }
        )
    return results, has_more
