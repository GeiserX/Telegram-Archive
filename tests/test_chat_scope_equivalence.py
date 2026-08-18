"""The chat-list entitlement filter, proved equivalent to the per-row rule.

``/api/chats`` used to load EVERY chat row in the archive and drop the ones the
viewer may not see in Python. That is correct and unusable: a viewer entitled to
one chat paid for all 4,784 of them, each carrying a correlated ``MAX(date)``
subquery. The grant now rides into SQL as ``WHERE`` predicates, so the page, the
``total`` and the ordering all describe the same visible row set and a one-chat
viewer touches one row.

Moving an access-control decision from Python into SQL is only safe if the two
say EXACTLY the same thing, so this file is the proof rather than the assertion:

* ``ChatScope.allows`` (websocket delivery, the ref resolver) and
  ``ChatScope.sql_predicates`` (the chat list) are run over the whole rule space
  — every combination of account grant, ref grant and DISPLAY_CHAT_IDS — and
  their answers must be set-equal in every cell, on both supported backends.
* A third, independently written oracle (set intersection instead of a per-row
  rule chain) checks the shared rule itself, so a wrong rule cannot pass merely
  by being consistently wrong in both surfaces.
* The empty grant is the cell that matters most. ``None`` means "unrestricted";
  ``[]`` means "entitled to nothing" and MUST match zero rows. Rendering it as a
  skipped filter is a total entitlement bypass, so every empty-grant cell is in
  the matrix and is asserted to return nothing.
* Ordering and paging are checked against the unrestricted path, because a
  restricted viewer now uses the same ORDER BY / LIMIT / OFFSET machinery.

The universe below is deliberately awkward: chat id 700000001 exists under TWO
accounts with different refs (so an account rule and a ref rule can disagree),
and the multi-ref grant names refs from three different accounts (so a ref that
belongs to another account can be caught being let through).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from src.db.adapter import ChatScope

BASE_DATE = datetime(2026, 2, 1, 9, 0, 0)

# (account_id, chat_id, ref, message_count)
UNIVERSE = [
    (1, 700000001, "scopeRefA1c1000000001", 3),
    (1, 700000002, "scopeRefA1c2000000002", 1),
    (1, 700000003, "scopeRefA1c3000000003", 0),  # message-less: the NULLS LAST leg
    (2, 700000001, "scopeRefA2c1000000001", 2),  # SAME chat id as row 1, other account
    (2, 700000004, "scopeRefA2c4000000004", 0),  # message-less
    (3, 700000005, "scopeRefA3c5000000005", 4),
]

ROWS = [{"account_id": acc, "id": cid, "ref": ref} for acc, cid, ref, _ in UNIVERSE]

ONE_REF = frozenset({"scopeRefA1c2000000002"})
# Spans three accounts on purpose: paired with accounts={1} this asks whether a
# ref belonging to another account can slip through the account rule.
MANY_REFS = frozenset({"scopeRefA1c2000000002", "scopeRefA2c1000000001", "scopeRefA3c5000000005"})

ACCOUNT_GRANTS = {
    "accounts=None": None,
    "accounts={1}": frozenset({1}),
    "accounts={1,2}": frozenset({1, 2}),
    "accounts=EMPTY": frozenset(),
}
REF_GRANTS = {
    "refs=None": None,
    "refs={one}": ONE_REF,
    "refs={many,cross-account}": MANY_REFS,
    "refs=EMPTY": frozenset(),
}
DISPLAY_FILTERS = {
    "display=unset": None,
    "display={2 ids, both accounts}": frozenset({700000001, 700000002}),
    "display=disjoint": frozenset({999999999}),
}

MATRIX = [
    (f"{d} x {a} x {r}", ChatScope(ids=dv, accounts=av, refs=rv))
    for d, dv in DISPLAY_FILTERS.items()
    for a, av in ACCOUNT_GRANTS.items()
    for r, rv in REF_GRANTS.items()
]
# 3 display filters x 4 account grants x 4 ref grants
MATRIX_CELLS = len(MATRIX)


def oracle_refs(scope: ChatScope) -> set[str]:
    """Visible refs, computed independently of ChatScope by set intersection.

    Same three rules, different shape: start from everything and intersect once
    per active rule, rather than walking a per-row chain of early returns. An
    empty grant intersects to the empty set on its own, with no special case —
    which is the property the production code has to spell out explicitly.
    """
    visible = {row["ref"] for row in ROWS}
    if scope.ids is not None:
        visible &= {row["ref"] for row in ROWS if row["id"] in scope.ids}
    if scope.accounts is not None:
        visible &= {row["ref"] for row in ROWS if row["account_id"] in scope.accounts}
    if scope.refs is not None:
        visible &= set(scope.refs)
    return visible


async def seed_universe(adapter) -> None:
    """Insert the chat rows, and enough messages to make the ordering non-trivial."""
    for account_id, chat_id, _ref, message_count in UNIVERSE:
        await adapter.upsert_chat(
            {"id": chat_id, "type": "group", "title": f"scope fixture {chat_id}"}, account_id=account_id
        )
        for index in range(message_count):
            await adapter.insert_message(
                {
                    "id": 1000 + index,
                    "chat_id": chat_id,
                    "sender_id": 4242,
                    "date": BASE_DATE + timedelta(minutes=chat_id % 97 + index),
                    "text": "scope fixture message",
                    "raw_data": {},
                },
                account_id=account_id,
            )
    # upsert_chat mints a random ref; pin the refs so the grants above are stable.
    async with adapter.db_manager.async_session_factory() as session:
        for account_id, chat_id, ref, _ in UNIVERSE:
            await session.execute(
                text("UPDATE chats SET ref = :ref WHERE account_id = :a AND id = :c"),
                {"ref": ref, "a": account_id, "c": chat_id},
            )
        await session.commit()


@pytest.fixture
async def seeded_adapter(real_adapter):
    await seed_universe(real_adapter)
    return real_adapter


# ============================================================================
# The matrix: SQL == ChatScope.allows == an independent oracle, every cell
# ============================================================================


async def test_sql_scope_matches_the_python_rule_in_every_cell(seeded_adapter, subtests):
    """For all 48 cells: what SQL returns is exactly what the per-row rule allows.

    ``subtests`` reports each cell by name, so a regression says WHICH grant
    combination broke rather than just "the matrix failed".
    """
    assert MATRIX_CELLS == 48, MATRIX_CELLS
    for name, scope in MATRIX:
        with subtests.test(cell=name):
            from_sql = {chat["ref"] for chat in await seeded_adapter.get_all_chats(scope=scope)}
            from_python = {row["ref"] for row in ROWS if scope.allows(row)}
            assert from_sql == from_python, f"SQL and ChatScope.allows disagree for {name}"
            assert from_sql == oracle_refs(scope), f"the shared rule itself is wrong for {name}"
            assert await seeded_adapter.get_chat_count(scope=scope) == len(from_sql)


async def test_every_empty_grant_cell_returns_nothing(seeded_adapter, subtests):
    """The bypass this design exists to prevent: [] is "nothing", never "no filter"."""
    empty_cells = [
        (name, scope)
        for name, scope in MATRIX
        if (scope.accounts is not None and not scope.accounts) or (scope.refs is not None and not scope.refs)
    ]
    assert len(empty_cells) == 21, len(empty_cells)
    for name, scope in empty_cells:
        with subtests.test(cell=name):
            assert await seeded_adapter.get_all_chats(scope=scope) == []
            assert await seeded_adapter.get_chat_count(scope=scope) == 0


async def test_ref_from_another_account_does_not_escape_the_account_grant(seeded_adapter):
    """A ref naming a chat in account 2 is still denied when the grant is account 1."""
    scope = ChatScope(accounts=frozenset({1}), refs=frozenset({"scopeRefA2c1000000001"}))
    assert await seeded_adapter.get_all_chats(scope=scope) == []
    assert await seeded_adapter.get_chat_count(scope=scope) == 0


async def test_shared_chat_id_is_separated_by_ref(seeded_adapter):
    """Chat id 700000001 exists under two accounts; a ref grant picks exactly one."""
    scope = ChatScope(refs=frozenset({"scopeRefA2c1000000001"}))
    rows = await seeded_adapter.get_all_chats(scope=scope)
    assert [(row["account_id"], row["id"]) for row in rows] == [(2, 700000001)]

    # ...and the DISPLAY_CHAT_IDS filter, which is id-keyed, deliberately binds
    # BOTH copies — that is the operator's rule, and the SQL must not narrow it.
    both = await seeded_adapter.get_all_chats(scope=ChatScope(ids=frozenset({700000001})))
    assert sorted(row["account_id"] for row in both) == [1, 2]


# ============================================================================
# Same machinery as the unrestricted path: ordering, paging, total
# ============================================================================


async def test_scoped_ordering_is_the_unrestricted_ordering_filtered(seeded_adapter):
    """A restricted page is a subsequence of the unrestricted page, same order."""
    scope = ChatScope(accounts=frozenset({1, 2}))
    unrestricted = [(row["account_id"], row["id"]) for row in await seeded_adapter.get_all_chats()]
    scoped = [(row["account_id"], row["id"]) for row in await seeded_adapter.get_all_chats(scope=scope)]
    assert scoped == [key for key in unrestricted if key[0] in {1, 2}]


async def test_scoped_paging_is_stable_and_total_counts_only_visible_chats(seeded_adapter):
    """Paging a scoped list covers every visible chat exactly once."""
    scope = ChatScope(accounts=frozenset({1, 2}))
    total = await seeded_adapter.get_chat_count(scope=scope)
    assert total == 5  # 3 chats on account 1, 2 on account 2; account 3 excluded

    single_shot = [(row["account_id"], row["id"]) for row in await seeded_adapter.get_all_chats(scope=scope)]
    paged = []
    for offset in range(0, total, 2):
        page = await seeded_adapter.get_all_chats(limit=2, offset=offset, scope=scope)
        paged += [(row["account_id"], row["id"]) for row in page]
    assert paged == single_shot
    assert len(set(paged)) == len(paged)


async def test_scope_composes_with_the_other_filters(seeded_adapter):
    """search/archived/folder_id still apply on top of the grant, not instead of it."""
    scope = ChatScope(refs=MANY_REFS)
    assert await seeded_adapter.get_chat_count(scope=scope) == 3
    assert await seeded_adapter.get_chat_count(search="700000002", scope=scope) == 1
    # Nothing in the fixture is archived, so the archived filter must empty the page.
    assert await seeded_adapter.get_all_chats(archived=True, scope=scope) == []
    assert await seeded_adapter.get_chat_count(archived=True, scope=scope) == 0


async def test_no_scope_is_identical_to_an_unrestricted_scope(seeded_adapter):
    """Passing scope=None and passing an all-None scope must not differ."""
    without = await seeded_adapter.get_all_chats()
    with_empty_scope = await seeded_adapter.get_all_chats(scope=ChatScope())
    assert [(row["account_id"], row["id"]) for row in without] == [
        (row["account_id"], row["id"]) for row in with_empty_scope
    ]
    assert await seeded_adapter.get_chat_count() == await seeded_adapter.get_chat_count(scope=ChatScope())


# ============================================================================
# The rendered predicate itself: "nothing" must never compile to "no filter"
# ============================================================================


@pytest.mark.parametrize(
    ("dialect_name", "matches_nothing"),
    [("postgresql", "WHERE false"), ("sqlite", "WHERE 0 = 1")],
)
@pytest.mark.parametrize(
    ("label", "scope"),
    [
        ("empty ref grant", ChatScope(refs=frozenset())),
        ("empty account grant", ChatScope(accounts=frozenset())),
        ("empty display filter", ChatScope(ids=frozenset())),
    ],
)
def test_an_empty_grant_compiles_to_an_always_false_predicate(label, scope, dialect_name, matches_nothing):
    """Pinned at the SQL text, not just at the row count.

    ``column.in_([])`` has changed rendering across SQLAlchemy versions and is
    a dialect's judgement call; an access-control filter cannot depend on that.
    The builder emits a literal false instead, and this asserts it stays that
    way on both supported dialects.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql, sqlite

    from src.db.models import Chat

    dialect = {"postgresql": postgresql, "sqlite": sqlite}[dialect_name].dialect()
    predicates = scope.sql_predicates()
    assert len(predicates) == 1, label
    stmt = select(Chat.id).where(predicates[0])
    sql = " ".join(str(stmt.compile(dialect=dialect, compile_kwargs={"literal_binds": True})).split())
    assert sql.endswith(matches_nothing), sql


def test_an_unrestricted_scope_adds_no_predicate_at_all():
    """The unrestricted path must keep issuing exactly the SQL it issued before."""
    assert ChatScope().sql_predicates() == []
    assert ChatScope().unrestricted is True


# ============================================================================
# The folder-count filter carries the same empty-grant rule
# ============================================================================


async def test_folder_counts_honour_an_empty_grant(seeded_adapter):
    """get_all_folders(allowed_chat_ids=set()) must count nothing, not everything.

    /api/folders feeds this the ids from _visible_chat_id_set, so an empty
    grant reaches it as an empty set. If that degraded to "no filter" the
    folder tabs would name chats the viewer may not open.
    """
    await seeded_adapter.upsert_chat_folder({"id": 2, "title": "scope fixture folder"}, account_id=1)
    await seeded_adapter.sync_folder_members(2, [700000001, 700000002], account_id=1)

    unrestricted = await seeded_adapter.get_all_folders()
    assert [folder["chat_count"] for folder in unrestricted] == [2]

    one_chat = await seeded_adapter.get_all_folders(allowed_chat_ids={700000001})
    assert [folder["chat_count"] for folder in one_chat] == [1]

    assert await seeded_adapter.get_all_folders(allowed_chat_ids=set()) == []
