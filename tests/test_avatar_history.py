"""Every profile photo an account saw is kept, and the viewer can show earlier ones.

``chats.avatar_photo_id`` (029) is one pointer per (account, chat) that the
backup overwrites, so the archive forgot every earlier photo and could not tell
"this account saw the photo removed" from "never recorded". Migration 031 adds
the append-only ``avatar_history``: ``upsert_chat`` writes a row whenever the
recorded id changes, a removal is a row with ``photo_id`` NULL, and the viewer
reads it to list earlier photos, to serve one on request, and to answer a seen
removal with a 404 instead of another account's newest file.

Covered here: the migration, the write and read paths on real engines, the
history API, the avatar route, and the info panel's "Previous photos" row.
"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from src.db.models import AvatarHistory

# Obviously fake ids.
CHAT = -420300001
PHOTO_1 = 1111
PHOTO_2 = 2222

# ============================================================================
# Migration 031
# ============================================================================

_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260924_031_add_avatar_history.py"
_spec = importlib.util.spec_from_file_location("migration_031", _MIGRATION_PATH)
migration_031 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_031)

TABLE = "avatar_history"
INDEX = "ix_avatar_history_account_chat_seen"


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _create_chats_table(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE chats (account_id INTEGER NOT NULL DEFAULT 1, id BIGINT NOT NULL, "
            "type VARCHAR(50) NOT NULL, avatar_photo_id BIGINT, updated_at DATETIME, "
            "PRIMARY KEY (account_id, id))"
        )
    )


def _history_rows(conn) -> list[tuple]:
    return list(
        conn.execute(sa.text("SELECT account_id, chat_id, photo_id FROM avatar_history ORDER BY account_id, chat_id"))
    )


class TestMigration031:
    def test_revision_chain(self):
        assert migration_031.revision == "031"
        assert migration_031.down_revision == "030"

    def test_upgrade_creates_the_table_and_index_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn)
            _run(conn, migration_031.upgrade)
            inspector = sa.inspect(conn)
            assert TABLE in inspector.get_table_names()
            columns = {c["name"]: c for c in inspector.get_columns(TABLE)}
            assert set(columns) == {"id", "account_id", "chat_id", "photo_id", "seen_at"}
            assert columns["photo_id"]["nullable"] is True
            assert columns["seen_at"]["nullable"] is False
            indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes(TABLE)}
            assert indexes == {INDEX: ["account_id", "chat_id", "seen_at"]}

            _run(conn, migration_031.upgrade)  # re-run must be a no-op
            assert INDEX in {i["name"] for i in sa.inspect(conn).get_indexes(TABLE)}

    def test_upgrade_seeds_each_recorded_photo_once(self):
        """A photo recorded before the upgrade enters the history, so it is not
        forgotten the first time it changes; re-running adds nothing."""
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, type, avatar_photo_id, updated_at) VALUES "
                    f"(1, {CHAT}, 'group', {PHOTO_1}, '2026-01-02 03:04:05'), "
                    f"(2, {CHAT}, 'group', NULL, '2026-01-02 03:04:05')"
                )
            )
            _run(conn, migration_031.upgrade)
            assert _history_rows(conn) == [(1, CHAT, PHOTO_1)]
            _run(conn, migration_031.upgrade)
            assert _history_rows(conn) == [(1, CHAT, PHOTO_1)]

    def test_upgrade_noop_on_create_all_shape(self):
        """create_all() already built the table; a half-applied one gets its index."""
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn)
            AvatarHistory.__table__.create(conn)
            conn.execute(sa.text(f"DROP INDEX {INDEX}"))
            _run(conn, migration_031.upgrade)
            assert INDEX in {i["name"] for i in sa.inspect(conn).get_indexes(TABLE)}
            _run(conn, migration_031.upgrade)
            assert INDEX in {i["name"] for i in sa.inspect(conn).get_indexes(TABLE)}

    def test_upgrade_without_chats_table_creates_the_table_only(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_031.upgrade)
            tables = sa.inspect(conn).get_table_names()
            assert TABLE in tables and "chats" not in tables

    def test_downgrade_drops_the_table_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn)
            _run(conn, migration_031.upgrade)
            _run(conn, migration_031.downgrade)
            assert TABLE not in sa.inspect(conn).get_table_names()
            _run(conn, migration_031.downgrade)
            assert TABLE not in sa.inspect(conn).get_table_names()


# ============================================================================
# Write and read paths, on real engines
# ============================================================================


async def _see(adapter, photo_id, account_id: int = 1, chat_id: int = CHAT) -> None:
    await adapter.upsert_chat({"id": chat_id, "type": "group", "avatar_photo_id": photo_id}, account_id=account_id)


async def _photo_ids(adapter, account_id: int = 1, chat_id: int = CHAT) -> list:
    return [row["photo_id"] for row in await adapter.get_avatar_history(chat_id, account_id=account_id)]


class TestWritePath:
    def test_upsert_chat_keeps_its_lock_retry(self):
        """The sighting helper sits right above upsert_chat; the retry decorator stays on the upsert."""
        from src.db.adapter import DatabaseAdapter

        assert hasattr(DatabaseAdapter.upsert_chat, "__wrapped__")
        assert not hasattr(DatabaseAdapter._record_avatar_sighting, "__wrapped__")

    async def test_every_change_is_a_row_including_a_repeat(self, real_adapter):
        """1111 -> 2222 -> 1111 is three sightings; nothing is unique."""
        await _see(real_adapter, PHOTO_1)
        await _see(real_adapter, PHOTO_2)
        await _see(real_adapter, PHOTO_1)
        assert await _photo_ids(real_adapter) == [PHOTO_1, PHOTO_2, PHOTO_1]  # newest first

    async def test_removal_is_a_null_row(self, real_adapter):
        await _see(real_adapter, PHOTO_1)
        await _see(real_adapter, None)
        assert await _photo_ids(real_adapter) == [None, PHOTO_1]

    async def test_an_unchanged_photo_writes_nothing(self, real_adapter):
        await _see(real_adapter, PHOTO_1)
        await _see(real_adapter, PHOTO_1)
        assert await _photo_ids(real_adapter) == [PHOTO_1]

    async def test_a_new_chat_without_a_photo_writes_nothing(self, real_adapter):
        """A missing chat row counts as a stored None."""
        await _see(real_adapter, None)
        assert await _photo_ids(real_adapter) == []

    async def test_a_partial_upsert_writes_nothing(self, real_adapter):
        """The listener's metadata upserts carry no photo key."""
        await _see(real_adapter, PHOTO_1)
        await real_adapter.upsert_chat({"id": CHAT, "type": "group", "title": "Renamed"}, account_id=1)
        assert await _photo_ids(real_adapter) == [PHOTO_1]

    async def test_another_accounts_history_is_untouched(self, real_adapter):
        await _see(real_adapter, PHOTO_1, account_id=1)
        await _see(real_adapter, PHOTO_2, account_id=2)
        await _see(real_adapter, None, account_id=2)
        assert await _photo_ids(real_adapter, account_id=1) == [PHOTO_1]
        assert await _photo_ids(real_adapter, account_id=2) == [None, PHOTO_2]

    async def test_a_first_sighting_compares_against_its_own_accounts_row(self, real_adapter):
        """Account 2 seeing the photo account 1 already saw is still account 2's first sighting."""
        await _see(real_adapter, PHOTO_1, account_id=1)
        await _see(real_adapter, PHOTO_1, account_id=2)
        assert await _photo_ids(real_adapter, account_id=2) == [PHOTO_1]

    async def test_the_row_carries_the_account_chat_and_a_time(self, real_adapter):
        await _see(real_adapter, PHOTO_1)
        async with real_adapter.db_manager.async_session_factory() as session:
            row = (await session.execute(select(AvatarHistory))).scalar_one()
        assert (row.account_id, row.chat_id, row.photo_id) == (1, CHAT, PHOTO_1)
        assert isinstance(row.seen_at, datetime)


class TestReadPath:
    async def test_ties_on_seen_at_order_by_id_descending(self, real_adapter):
        # The higher id is written first, so storage order is the opposite of id
        # order and only the explicit tiebreak gives id descending on PostgreSQL.
        # (On SQLite the id is the rowid, so the index already orders ties by it.)
        same = datetime(2026, 1, 2, 3, 4, 5)
        for row_id, photo_id in ((2, PHOTO_2), (1, PHOTO_1)):
            async with real_adapter.db_manager.async_session_factory() as session:
                session.add(AvatarHistory(id=row_id, account_id=1, chat_id=CHAT, photo_id=photo_id, seen_at=same))
                await session.commit()
        history = await real_adapter.get_avatar_history(CHAT, account_id=1)
        assert history == [{"photo_id": PHOTO_2, "seen_at": same}, {"photo_id": PHOTO_1, "seen_at": same}]

    async def test_other_chats_and_accounts_are_not_read(self, real_adapter):
        await _see(real_adapter, PHOTO_1, account_id=1, chat_id=CHAT)
        await _see(real_adapter, PHOTO_2, account_id=1, chat_id=CHAT - 1)
        await _see(real_adapter, PHOTO_2, account_id=2, chat_id=CHAT)
        assert await _photo_ids(real_adapter, account_id=1, chat_id=CHAT) == [PHOTO_1]


# ============================================================================
# The viewer: the history API and the avatar routes
# ============================================================================

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from test_per_account_avatars import (  # noqa: E402
    BYTES_A,
    BYTES_B,
    PEER,
    PHOTO_SEEN_BY_A,
    PHOTO_SEEN_BY_B,
    _get,
    _ref,
    _seed_peer,
    viewer,  # noqa: F401 - the real-adapter viewer fixture
)

# The fixture puts PEER_{1111}.jpg (older) and PEER_{2222}.jpg (newest) on disk.
assert (PHOTO_SEEN_BY_A, PHOTO_SEEN_BY_B) == (PHOTO_1, PHOTO_2)


class TestHistoryApi:
    async def test_shape_newest_first_with_urls_and_availability(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, PHOTO_1)
        await _seed_peer(viewer, 1, 9999)  # recorded, file never downloaded
        await _seed_peer(viewer, 1, None)
        ref = await _ref(viewer, PEER, 1)

        resp = await _get(f"/api/chats/{ref}/avatars")

        assert resp.status_code == 200
        body = resp.json()
        assert [(e["photo_id"], e["url"], e["available"]) for e in body] == [
            (None, None, False),
            (9999, f"/media/avatar/{ref}?photo_id=9999", False),
            (PHOTO_1, f"/media/avatar/{ref}?photo_id={PHOTO_1}", True),
        ]
        for entry in body:
            assert set(entry) == {"photo_id", "seen_at", "url", "available"}
            datetime.fromisoformat(entry["seen_at"])
        assert str(PEER) not in resp.text, "the history API must never carry a chat id"

    async def test_only_the_refs_account(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, PHOTO_1)
        await _seed_peer(viewer, 2, PHOTO_2)
        resp = await _get(f"/api/chats/{await _ref(viewer, PEER, 2)}/avatars")
        assert [e["photo_id"] for e in resp.json()] == [PHOTO_2]

    async def test_unknown_ref_is_404(self, viewer):  # noqa: F811
        resp = await _get("/api/chats/AAAAAAAAAAAAAAAAAAAAAA/avatars")
        assert resp.status_code == 404


class TestAvatarRoute:
    async def test_a_photo_in_the_history_is_served(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, PHOTO_1)
        await _seed_peer(viewer, 1, PHOTO_2)
        ref = await _ref(viewer, PEER, 1)
        resp = await _get(f"/media/avatar/{ref}?photo_id={PHOTO_1}")
        assert resp.status_code == 200 and resp.content == BYTES_A

    async def test_the_current_photo_is_served_by_id(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, PHOTO_2)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}?photo_id={PHOTO_2}")
        assert resp.status_code == 200 and resp.content == BYTES_B

    async def test_a_photo_not_in_this_accounts_history_is_404(self, viewer):  # noqa: F811
        """The file is on disk (account 2 saw it), but account 1 never did."""
        await _seed_peer(viewer, 1, PHOTO_1)
        await _seed_peer(viewer, 2, PHOTO_2)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}?photo_id={PHOTO_2}")
        assert resp.status_code == 404

    async def test_a_recorded_photo_without_a_file_is_404_not_a_fallback(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, 9999)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}?photo_id=9999")
        assert resp.status_code == 404

    async def test_a_seen_removal_is_404(self, viewer):  # noqa: F811
        """Recorded None with a NULL newest row: this account saw the photo go."""
        await _seed_peer(viewer, 1, PHOTO_1)
        await _seed_peer(viewer, 1, None)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}")
        assert resp.status_code == 404

    async def test_never_recorded_still_falls_back_to_newest(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, None)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}")
        assert resp.status_code == 200 and resp.content == BYTES_B

    async def test_a_photo_id_request_does_not_change_the_default_answer(self, viewer):  # noqa: F811
        await _seed_peer(viewer, 1, PHOTO_2)
        await _seed_peer(viewer, 1, PHOTO_1)  # current is the OLDER file on disk
        ref = await _ref(viewer, PEER, 1)
        earlier = await _get(f"/media/avatar/{ref}?photo_id={PHOTO_2}")
        assert earlier.status_code == 200 and earlier.content == BYTES_B
        default = await _get(f"/media/avatar/{ref}")
        assert default.status_code == 200 and default.content == BYTES_A

    async def test_sender_avatar_of_a_seen_removal_is_404(self, viewer):  # noqa: F811
        """Same rule on the sender route: the group's account saw the DM peer remove it."""
        await _seed_peer(viewer, 1, PHOTO_1)
        await _seed_peer(viewer, 1, None)
        await viewer.upsert_chat({"id": CHAT, "type": "group", "title": "Test Group"}, account_id=1)
        await viewer.insert_message(
            {"id": 9, "chat_id": CHAT, "sender_id": PEER, "date": datetime(2026, 4, 1), "text": "hi", "raw_data": {}},
            account_id=1,
        )
        resp = await _get(f"/media/avatar/{await _ref(viewer, CHAT, 1)}/9")
        assert resp.status_code == 404


# ============================================================================
# The info panel's "Previous photos" row
# ============================================================================

from test_frontend_audit_fixes import INDEX_HTML, _run_node  # noqa: E402
from test_info_panel_frontend import PRELUDE, _panel_block  # noqa: E402

_PRELUDE_FETCH = (
    "const fetch = async (url, init) => { requests.push([url, init && init.method]); "
    "return { ok: fetchOk, status: fetchOk ? 200 : 500 }; };"
)


def _avatar_script(body: str) -> str:
    assert _PRELUDE_FETCH in PRELUDE, "the info panel harness changed its fetch stub"
    prelude = PRELUDE.replace(
        _PRELUDE_FETCH,
        "let fetchBody = [];\n"
        "const fetch = async (url, init) => { requests.push([url, init && init.method]); "
        "return { ok: fetchOk, status: fetchOk ? 200 : 500, json: async () => fetchBody }; };",
    )
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The lightbox lives outside the panel block; openPreviousAvatar reaches it by name.
    lightbox = (
        "const lightboxList = ref([]); const lightboxIndex = ref(0); const lightboxMedia = ref(null);\n"
        "const handleLightboxKeydown = () => {};"
    )
    run = "(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });"
    return "\n".join([prelude, _panel_block(html, {"file": False, "path": False}), lightbox, run])


def _row(photo_id, available=True, ref="c1"):
    return {
        "photo_id": photo_id,
        "seen_at": "2026-01-02T03:04:05",
        "url": None if photo_id is None else f"/media/avatar/{ref}?photo_id={photo_id}",
        "available": available,
    }


class TestPreviousPhotosRow:
    def test_markup_is_in_the_info_panel(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        panel = html[html.index('<aside v-if="showInfoPanel && selectedChat" id="info-panel"') :]
        panel = panel[: panel.index("</aside>")]
        assert 'v-if="previousAvatars.length"' in panel
        assert ">Previous photos<" in panel
        assert '@click="openPreviousAvatar(index)"' in panel
        assert ':src="entry.url"' in panel

    def test_the_avatar_lightbox_offers_no_download_link(self):
        """The media route's ?download=1 does not exist on the avatar route."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert '<a v-if="lightboxMedia && !noDownload && !lightboxMedia.isAvatar"' in html

    def test_entries_exclude_the_current_photo_missing_files_and_repeats(self):
        rows = [_row(PHOTO_1), _row(PHOTO_2), _row(9999, available=False), _row(PHOTO_1), _row(3333), _row(PHOTO_2)]
        _run_node(
            _avatar_script(f"""
assert.deepEqual(previousAvatarEntries({json.dumps(rows)}).map(r => r.photo_id), [2222, 3333]);
assert.deepEqual(previousAvatarEntries([{json.dumps(_row(PHOTO_1))}]), [], 'only the current photo: no row');
assert.deepEqual(previousAvatarEntries([]), []);
assert.deepEqual(
    previousAvatarEntries({json.dumps([_row(None), _row(PHOTO_1)])}).map(r => r.photo_id), [1111],
    'after a removal every photo still on disk is an earlier one');
""")
        )

    def test_opening_the_panel_loads_the_history_and_a_click_opens_the_lightbox(self):
        rows = [_row(PHOTO_1), _row(PHOTO_2)]
        _run_node(
            _avatar_script(f"""
const avatarWatch = watchers.find(w => String(w.source).includes('showInfoPanel'));
fetchBody = {json.dumps(rows)};
showInfoPanel.value = true;
avatarWatch.fn(avatarWatch.source());
await new Promise(r => setTimeout(r, 0));
assert.deepEqual(requests.at(-1), ['/api/chats/c1/avatars', undefined]);
assert.deepEqual(previousAvatars.value.map(r => r.photo_id), [2222]);

openPreviousAvatar(0);
assert.equal(lightboxOpen.value, true);
assert.equal(lightboxMedia.value.media.url, '/media/avatar/c1?photo_id=2222');
assert.equal(lightboxMedia.value.isAvatar, true);

// A response for a chat the viewer has since left is dropped.
fetchBody = {json.dumps([_row(PHOTO_2), _row(PHOTO_1)])};
avatarWatch.fn('c1');
selectedChat.value = {{ ref: 'c2', type: 'group' }};
await new Promise(r => setTimeout(r, 0));
assert.deepEqual(previousAvatars.value, [], 'the stale answer never decorates the next chat');
""")
        )
