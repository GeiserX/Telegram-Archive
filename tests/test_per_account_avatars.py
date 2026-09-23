"""Each account's avatars stay its own when accounts share the avatar folder.

Avatar files are shared across accounts and named ``{chat_id}_{photo_id}.jpg``;
the viewer used to serve whichever was modified last. Two accounts can see
different photos for the same user — a photo one account set for a contact is
visible to that account only — so as soon as a second account downloaded its
own copy, the first account's viewer flipped to it.

The fix records the photo id each account sees on its own ``chats`` row
(migration 029) and the viewer serves that file first. Covered here: the id
helper, the backup's chat row, the migration, the adapter on real engines, and
both avatar routes end to end on a real adapter.
"""

import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from telethon.tl.types import ChatPhoto, ChatPhotoEmpty, User, UserProfilePhoto, UserProfilePhotoEmpty

from src.avatar_utils import avatar_photo_id, get_avatar_paths
from src.telegram_backup import TelegramBackup

# A user both accounts hold a one-to-one chat with, and a group account 1 holds.
# Obviously fake ids.
PEER = 420100001
GROUP = -420100002
PHOTO_SEEN_BY_A = 1111
PHOTO_SEEN_BY_B = 2222


def _user(photo) -> User:
    return User(id=PEER, first_name="Test", photo=photo)


# ============================================================================
# avatar_photo_id
# ============================================================================


class TestAvatarPhotoId:
    def test_user_photo(self):
        assert avatar_photo_id(_user(UserProfilePhoto(photo_id=PHOTO_SEEN_BY_A, dc_id=1))) == PHOTO_SEEN_BY_A

    def test_chat_photo(self):
        entity = MagicMock(photo=ChatPhoto(photo_id=PHOTO_SEEN_BY_B, dc_id=1))
        assert avatar_photo_id(entity) == PHOTO_SEEN_BY_B

    @pytest.mark.parametrize("photo", [None, UserProfilePhotoEmpty(), ChatPhotoEmpty()])
    def test_no_avatar_is_none(self, photo):
        assert avatar_photo_id(MagicMock(photo=photo)) is None

    def test_matches_the_file_name(self, tmp_path):
        """The recorded id must be the one in the downloaded file's name."""
        entity = _user(UserProfilePhoto(photo_id=PHOTO_SEEN_BY_A, dc_id=1))
        target, _legacy = get_avatar_paths(str(tmp_path), entity, PEER)
        assert os.path.basename(target) == f"{PEER}_{avatar_photo_id(entity)}.jpg"


# ============================================================================
# The backup writes it on the chat row
# ============================================================================


class TestExtractChatData:
    def _backup(self):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock()
        return backup

    def test_records_the_photo_this_account_sees(self):
        data = self._backup()._extract_chat_data(_user(UserProfilePhoto(photo_id=PHOTO_SEEN_BY_A, dc_id=1)))
        assert data["avatar_photo_id"] == PHOTO_SEEN_BY_A

    def test_records_none_when_the_avatar_is_gone(self):
        """Explicit None, so a removed photo clears what an earlier run recorded."""
        data = self._backup()._extract_chat_data(_user(None))
        assert "avatar_photo_id" in data
        assert data["avatar_photo_id"] is None


# ============================================================================
# Migration 029
# ============================================================================

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_spec = importlib.util.spec_from_file_location(
    "migration_029", _VERSIONS_DIR / "20260923_029_add_chat_avatar_photo_id.py"
)
migration_029 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_029)


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _chat_columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("chats")}


def _create_chats_table(conn, with_column: bool = False):
    extra = ", avatar_photo_id BIGINT" if with_column else ""
    conn.execute(
        sa.text(
            "CREATE TABLE chats (account_id INTEGER NOT NULL DEFAULT 1, id BIGINT NOT NULL, "
            f"type VARCHAR(50) NOT NULL{extra}, PRIMARY KEY (account_id, id))"
        )
    )


class TestMigration029:
    def test_revision_chain(self):
        assert migration_029.revision == "029"
        assert migration_029.down_revision == "028"

    def test_upgrade_adds_the_column_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn)
            _run(conn, migration_029.upgrade)
            assert "avatar_photo_id" in _chat_columns(conn)
            _run(conn, migration_029.upgrade)  # re-run must be a no-op
            assert "avatar_photo_id" in _chat_columns(conn)

    def test_upgrade_noop_on_create_all_shape(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn, with_column=True)
            _run(conn, migration_029.upgrade)
            assert "avatar_photo_id" in _chat_columns(conn)

    def test_upgrade_noop_without_chats_table(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_029.upgrade)
            assert "chats" not in sa.inspect(conn).get_table_names()

    def test_downgrade_drops_the_column_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_chats_table(conn, with_column=True)
            _run(conn, migration_029.downgrade)
            assert "avatar_photo_id" not in _chat_columns(conn)
            _run(conn, migration_029.downgrade)
            assert "avatar_photo_id" not in _chat_columns(conn)


# ============================================================================
# Adapter, on real engines
# ============================================================================


async def _seed_peer(adapter, account_id: int, photo_id: int | None) -> None:
    await adapter.upsert_chat({"id": PEER, "type": "private", "avatar_photo_id": photo_id}, account_id=account_id)


class TestAdapter:
    async def test_each_account_keeps_its_own_photo(self, real_adapter):
        await _seed_peer(real_adapter, 1, PHOTO_SEEN_BY_A)
        await _seed_peer(real_adapter, 2, PHOTO_SEEN_BY_B)
        assert await real_adapter.get_avatar_photo_id(PEER, account_id=1) == PHOTO_SEEN_BY_A
        assert await real_adapter.get_avatar_photo_id(PEER, account_id=2) == PHOTO_SEEN_BY_B

    async def test_partial_upsert_keeps_the_recorded_photo(self, real_adapter):
        """The listener's metadata upserts carry no photo and must not erase it."""
        await _seed_peer(real_adapter, 1, PHOTO_SEEN_BY_A)
        await real_adapter.upsert_chat({"id": PEER, "type": "private", "first_name": "Renamed"}, account_id=1)
        assert await real_adapter.get_avatar_photo_id(PEER, account_id=1) == PHOTO_SEEN_BY_A

    async def test_explicit_none_clears_it(self, real_adapter):
        await _seed_peer(real_adapter, 1, PHOTO_SEEN_BY_A)
        await _seed_peer(real_adapter, 1, None)
        assert await real_adapter.get_avatar_photo_id(PEER, account_id=1) is None

    async def test_unknown_chat_is_none(self, real_adapter):
        assert await real_adapter.get_avatar_photo_id(PEER, account_id=1) is None


# ============================================================================
# The viewer serves each account its own photo
# ============================================================================

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_avatars_"))

from datetime import datetime  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402

from src.web import main as web_main  # noqa: E402

BYTES_A = b"photo account A sees"
BYTES_B = b"photo account B sees"


@pytest.fixture
async def viewer(real_adapter, tmp_path):
    """The real app on a real adapter, with both photos on disk and B's newest."""
    users = tmp_path / "avatars" / "users"
    users.mkdir(parents=True)
    older = users / f"{PEER}_{PHOTO_SEEN_BY_A}.jpg"
    newer = users / f"{PEER}_{PHOTO_SEEN_BY_B}.jpg"
    older.write_bytes(BYTES_A)
    newer.write_bytes(BYTES_B)
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))

    saved = (
        web_main.db,
        web_main.AUTH_ENABLED,
        web_main.ALLOW_ANONYMOUS_VIEWER,
        web_main.config.display_chat_ids,
        web_main.config.media_path,
        web_main._media_root,
    )
    web_main.db = real_adapter
    web_main.AUTH_ENABLED = False
    web_main.ALLOW_ANONYMOUS_VIEWER = True
    web_main.config.display_chat_ids = set()
    web_main.config.media_path = str(tmp_path)
    web_main._media_root = tmp_path.resolve()
    web_main._avatar_cache.clear()
    web_main._avatar_cache_time = None
    web_main._avatar_dir_index.clear()
    web_main._sender_lookup_cache.clear()
    user = web_main.UserContext(username="avatars-test", role="master")
    web_main.app.dependency_overrides[web_main.require_auth] = lambda: user
    try:
        yield real_adapter
    finally:
        web_main.app.dependency_overrides.clear()
        (
            web_main.db,
            web_main.AUTH_ENABLED,
            web_main.ALLOW_ANONYMOUS_VIEWER,
            web_main.config.display_chat_ids,
            web_main.config.media_path,
            web_main._media_root,
        ) = saved
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._avatar_dir_index.clear()
        web_main._sender_lookup_cache.clear()


async def _ref(adapter, chat_id: int, account_id: int) -> str:
    return (await adapter.get_chat_by_id(chat_id, account_id=account_id))["ref"]


async def _get(url: str):
    async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
        return await client.get(url)


class TestViewer:
    async def test_chat_avatar_is_the_owning_accounts_photo(self, viewer):
        await _seed_peer(viewer, 1, PHOTO_SEEN_BY_A)
        await _seed_peer(viewer, 2, PHOTO_SEEN_BY_B)

        resp_a = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}")
        resp_b = await _get(f"/media/avatar/{await _ref(viewer, PEER, 2)}")

        assert resp_a.status_code == 200 and resp_a.content == BYTES_A  # older file, still A's
        assert resp_b.status_code == 200 and resp_b.content == BYTES_B

    async def test_unrecorded_photo_falls_back_to_newest(self, viewer):
        """A row no backup has refreshed since 029 keeps the old behaviour."""
        await _seed_peer(viewer, 1, None)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}")
        assert resp.status_code == 200 and resp.content == BYTES_B

    async def test_recorded_photo_missing_on_disk_falls_back_to_newest(self, viewer):
        await _seed_peer(viewer, 1, 9999)
        resp = await _get(f"/media/avatar/{await _ref(viewer, PEER, 1)}")
        assert resp.status_code == 200 and resp.content == BYTES_B

    async def test_sender_avatar_uses_the_chats_account(self, viewer):
        """In a group, a sender's avatar is the photo the group's account sees."""
        await _seed_peer(viewer, 1, PHOTO_SEEN_BY_A)
        await _seed_peer(viewer, 2, PHOTO_SEEN_BY_B)
        await viewer.upsert_chat({"id": GROUP, "type": "group", "title": "Test Group"}, account_id=1)
        await viewer.insert_message(
            {
                "id": 7,
                "chat_id": GROUP,
                "sender_id": PEER,
                "date": datetime(2026, 4, 1, 12, 0, 0),
                "text": "hi",
                "raw_data": {},
            },
            account_id=1,
        )
        resp = await _get(f"/media/avatar/{await _ref(viewer, GROUP, 1)}/7")
        assert resp.status_code == 200 and resp.content == BYTES_A
