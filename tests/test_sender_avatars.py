"""Tests for group sender avatars (#229) and the migration banner (#228).

Covers three slices:

* US-210 (slice 1): the pure-frontend initials circle — getSenderInitials /
  getAvatarFill exist in the template and the darker fill clears white-text
  contrast (WCAG AA) on every hue.
* US-211 (slice 2a): the media-ACL fix that serves member avatars for users
  who spoke in a visible chat, plus per-message sender_avatar_url resolution
  from files already on disk.
* US-203 (#228): the display-only group→supergroup migration banner.

The template assertions follow the string-matching idiom of
test_frontend_bootstrap.py; the backend assertions follow the temp-media-dir
idiom of test_database_viewer.py (TestAvatarPathLookup).
"""

import asyncio
import colorsys
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_backup_"))

from src.db.adapter import DatabaseAdapter  # noqa: E402
from src.db.base import DatabaseManager  # noqa: E402
from src.db.models import Base, Message  # noqa: E402
from src.web import main as web_main  # noqa: E402

try:
    from httpx import ASGITransport, AsyncClient

    _HTTPX_AVAILABLE = True
except Exception:
    _HTTPX_AVAILABLE = False

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


def _contrast_ratio_vs_white(hue: int, lightness: float) -> float:
    """WCAG contrast ratio of white text over hsl(hue, 65%, lightness)."""

    def _channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = colorsys.hls_to_rgb(hue / 360, lightness, 0.65)
    fill_luminance = 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)
    white_luminance = 1.0
    return (white_luminance + 0.05) / (fill_luminance + 0.05)


class TestSenderInitialsTemplate(unittest.TestCase):
    """US-210: initials helper contract, expressed via the template source."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_initials_and_fill_helpers_exist_and_are_exported(self):
        self.assertIn("const getSenderInitials = (msg) =>", self.html)
        self.assertIn("const getAvatarFill = (msg) =>", self.html)
        # Must be returned from setup() or Vue cannot resolve them in-template.
        self.assertIn("getSenderInitials,", self.html)
        self.assertIn("getAvatarFill,", self.html)

    def test_initials_mirror_sender_name_sources(self):
        """The monogram must derive from getSenderName, not a private name chain,
        so it matches the visible label; only '?' for the Deleted Account terminal."""
        start = self.html.index("const getSenderInitials = (msg) =>")
        body = self.html[start : start + 700]
        # Derives from the SAME source as the visible name label.
        self.assertIn("getSenderName(msg)", body)
        # '?' only when getSenderName itself has nothing real.
        self.assertIn("name === 'Deleted Account'", body)
        self.assertIn("return '?'", body)
        # Must NOT reintroduce the getChatName 'DA' fallback.
        self.assertNotIn("getChatName", body)

    def test_avatar_fill_is_the_darker_gradient(self):
        start = self.html.index("const getAvatarFill = (msg) =>")
        body = self.html[start : start + 500]
        self.assertIn("linear-gradient(135deg, hsl(${hue}, 65%, 27%), hsl(${hue}, 65%, 18%))", body)


class TestSenderInitialsLogic(unittest.TestCase):
    """US-210: replicate getSenderInitials semantics to lock the contract.

    getSenderInitials mirrors getSenderName's source chain (post_author →
    first/last → username → 'User <id>') and only yields '?' for the terminal
    'Deleted Account' — so the monogram always matches the visible name label.
    """

    @staticmethod
    def _sender_name(msg):
        """Mirror of the JS getSenderName resolution chain."""
        raw = msg.get("raw_data") or {}
        if raw.get("post_author"):
            return raw["post_author"]
        first, last = msg.get("first_name"), msg.get("last_name")
        if first or last:
            return f"{first or ''} {last or ''}".strip()
        if msg.get("username"):
            return msg["username"]
        if msg.get("sender_id"):
            return f"User {msg['sender_id']}"
        return "Deleted Account"

    def _initials(self, msg):
        """Mirror of the JS getSenderInitials."""
        name = self._sender_name(msg)
        if not name or name == "Deleted Account":
            return "?"
        return "".join(w[0] for w in name.split() if w)[:2].upper() or "?"

    def test_two_names_two_letters(self):
        self.assertEqual(self._initials({"first_name": "Ada", "last_name": "Lovelace"}), "AL")

    def test_one_name_one_letter(self):
        self.assertEqual(self._initials({"first_name": "Grace"}), "G")
        self.assertEqual(self._initials({"first_name": "grace", "last_name": ""}), "G")

    def test_username_fallback_matches_label(self):
        # No first/last but a username → monogram from the username (visible label).
        self.assertEqual(self._initials({"username": "grace"}), "G")

    def test_post_author_signature(self):
        # Channel post signature is the visible label → monogram from it.
        self.assertEqual(self._initials({"raw_data": {"post_author": "John Doe"}}), "JD")

    def test_sender_id_fallback_is_not_question_mark(self):
        # getSenderName renders 'User <id>' → a real label, so NOT '?'.
        self.assertEqual(self._initials({"sender_id": 12345}), "U1")

    def test_empty_returns_question_mark(self):
        # Only when getSenderName would have nothing real.
        self.assertEqual(self._initials({}), "?")
        self.assertEqual(self._initials({"first_name": "", "last_name": ""}), "?")


class TestAvatarFillContrast(unittest.TestCase):
    """US-210: white text over the WHOLE avatar circle must clear WCAG AA (4.5:1).

    Both gradient stops sit in the safe zone so tall letters / anti-aliasing
    reaching the lighter (top-left) corner stay legible — not just the center.
    """

    # Both stops of getAvatarFill: linear-gradient hsl(h,65%,27%) -> hsl(h,65%,18%).
    LIGHTER_STOP = 0.27
    DARKER_STOP = 0.18

    def test_both_stops_meet_aa_on_all_hues(self):
        # The lighter stop is the contrast-limiting one; assert BOTH stops clear
        # 4.5:1 on every hue so the entire circle is guaranteed legible. (27%
        # gives min ~5.08, 18% gives min ~8.94 — both above 4.5 with margin.)
        for hue in range(0, 360, 5):
            for stop in (self.LIGHTER_STOP, self.DARKER_STOP):
                with self.subTest(hue=hue, stop=stop):
                    self.assertGreaterEqual(_contrast_ratio_vs_white(hue, stop), 4.5)

    def test_lighter_stop_is_below_the_safe_ceiling(self):
        # Guards against a future edit lightening the top stop past the point
        # where every hue clears AA: at S=65 the ceiling is ~29% lightness.
        self.assertLessEqual(self.LIGHTER_STOP, 0.29)

    def test_bright_name_palette_would_fail_contrast(self):
        # Sanity anchor: the old bright name color is NOT contrast-safe for a
        # white-text fill, documenting why a darker fill was introduced.
        failing = [h for h in range(360) if _contrast_ratio_vs_white(h, 0.65) < 4.5]
        self.assertTrue(failing)


class TestSenderAvatarUrl(unittest.TestCase):
    """US-211: per-message sender_avatar_url resolved from on-disk files."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_media_path = web_main.config.media_path
        web_main.config.media_path = self.temp_dir.name
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

    def tearDown(self):
        web_main.config.media_path = self.original_media_path
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def _touch_avatar(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as avatar_file:
            avatar_file.write("x")

    def test_present_when_file_globs(self):
        user_id = 555000111
        avatars_dir = os.path.join(self.temp_dir.name, "avatars", "users")
        self._touch_avatar(os.path.join(avatars_dir, f"{user_id}_42.jpg"))

        self.assertEqual(
            web_main._sender_avatar_url(user_id),
            f"/media/avatars/users/{user_id}_42.jpg",
        )

    def test_null_when_absent(self):
        self.assertIsNone(web_main._sender_avatar_url(999888777))

    def test_null_for_missing_or_non_user_sender(self):
        self.assertIsNone(web_main._sender_avatar_url(None))
        # Negative ids are channels/groups, never a users/ avatar.
        self.assertIsNone(web_main._sender_avatar_url(-1001234))


class TestMemberAvatarAcl(unittest.TestCase):
    """US-211: ACL fix — member avatars for visible members are served."""

    def setUp(self):
        web_main._avatar_member_cache.clear()
        web_main._avatar_member_cache_time = None

    def tearDown(self):
        web_main._avatar_member_cache.clear()
        web_main._avatar_member_cache_time = None

    def _viewer(self):
        return web_main.UserContext(username="viewer", role="viewer", allowed_chat_ids={-1001})

    def test_user_avatar_allowed_when_member_ok(self):
        """A user who spoke in a visible chat: served (no raise)."""
        user = self._viewer()
        web_main._enforce_media_acl("avatars/users/555_9.jpg", user, member_ok=True)

    def test_user_avatar_blocked_when_not_member(self):
        """A user not in any visible chat: 403."""
        user = self._viewer()
        with self.assertRaises(web_main.HTTPException) as ctx:
            web_main._enforce_media_acl("avatars/users/555_9.jpg", user, member_ok=False)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_chat_avatar_behavior_unchanged(self):
        """avatars/chats/ still gates purely on the allowed chat id."""
        user = self._viewer()
        # Allowed chat id in the filename -> served.
        web_main._enforce_media_acl("avatars/chats/-1001_7.jpg", user)
        # Different chat id -> blocked, regardless of member_ok.
        with self.assertRaises(web_main.HTTPException) as ctx:
            web_main._enforce_media_acl("avatars/chats/-1009_7.jpg", user, member_ok=True)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_one_to_one_contact_avatar_allowed_without_probe(self):
        """A user whose id is itself a visible (private) chat is served directly."""
        user = web_main.UserContext(username="v", role="viewer", allowed_chat_ids={555})
        web_main._enforce_media_acl("avatars/users/555_9.jpg", user, member_ok=False)

    def test_membership_probe_only_hits_db_for_user_avatars(self):
        """_avatar_user_visible_member queries membership only for avatars/users/."""
        user = self._viewer()
        probe = AsyncMock(return_value=True)
        original_db = web_main.db
        web_main.db = type("D", (), {"sender_has_message_in_chats": staticmethod(probe)})()
        try:
            # User avatar for a non-1:1 user -> DB probe decides.
            allowed = asyncio.run(web_main._avatar_user_visible_member("avatars/users/555_9.jpg", user))
            self.assertTrue(allowed)
            probe.assert_awaited_once()

            # Chat avatar -> never probes the DB.
            probe.reset_mock()
            self.assertFalse(asyncio.run(web_main._avatar_user_visible_member("avatars/chats/-1001_9.jpg", user)))
            probe.assert_not_awaited()

            # Regular media -> never probes the DB.
            self.assertFalse(asyncio.run(web_main._avatar_user_visible_member("-1001/photo.jpg", user)))
            probe.assert_not_awaited()
        finally:
            web_main.db = original_db

    def test_membership_probe_skips_db_for_one_to_one_contact(self):
        """A user id already in the viewer's chat set needs no DB probe."""
        user = web_main.UserContext(username="v", role="viewer", allowed_chat_ids={555})
        probe = AsyncMock(return_value=False)
        original_db = web_main.db
        web_main.db = type("D", (), {"sender_has_message_in_chats": staticmethod(probe)})()
        try:
            self.assertTrue(asyncio.run(web_main._avatar_user_visible_member("avatars/users/555_9.jpg", user)))
            probe.assert_not_awaited()
        finally:
            web_main.db = original_db

    def test_membership_probe_is_cached_across_requests(self):
        """The DB probe runs once per (user, chat-set); repeats hit the cache."""
        user = self._viewer()
        probe = AsyncMock(return_value=True)
        original_db = web_main.db
        web_main.db = type("D", (), {"sender_has_message_in_chats": staticmethod(probe)})()
        try:
            for _ in range(3):
                self.assertTrue(asyncio.run(web_main._avatar_user_visible_member("avatars/users/555_9.jpg", user)))
            probe.assert_awaited_once()
        finally:
            web_main.db = original_db

    def test_membership_probe_fails_closed_on_db_error(self):
        """A DB error in the probe denies the avatar (False) and is NOT cached."""
        user = self._viewer()
        probe = AsyncMock(side_effect=RuntimeError("db down"))
        original_db = web_main.db
        web_main.db = type("D", (), {"sender_has_message_in_chats": staticmethod(probe)})()
        try:
            self.assertFalse(asyncio.run(web_main._avatar_user_visible_member("avatars/users/555_9.jpg", user)))
            # Not cached: a transient failure must be retried, not stuck for the TTL.
            self.assertEqual(web_main._avatar_member_cache, {})
        finally:
            web_main.db = original_db


class TestMigrationBannerTemplate(unittest.TestCase):
    """US-203 (#228): the display-only migration banner."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_banner_computed_reads_migrate_pointers(self):
        self.assertIn("const migrationBanner = computed(() =>", self.html)
        start = self.html.index("const migrationBanner = computed(() =>")
        body = self.html[start : start + 900]
        self.assertIn("raw.migrate_to_id != null", body)
        self.assertIn("raw.migrate_from_id != null", body)
        self.assertIn("This group continues as a supergroup →", body)
        self.assertIn("← Migrated from ", body)

    def test_banner_is_rendered_and_exported(self):
        # Rendered only when the pointer data exists (degrades to no banner).
        self.assertIn('v-if="migrationBanner && !showPinnedOnly"', self.html)
        self.assertIn("{{ migrationBanner.text }}", self.html)
        self.assertIn("migrationBanner,", self.html)  # exported from setup()


class TestGroupAvatarRenderTemplate(unittest.TestCase):
    """US-210/US-211: the avatar slot renders photo-or-initials in group chats."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_avatar_gutter_is_group_and_non_own_only(self):
        self.assertIn('v-if="isGroup && !isOwnMessage(msg)"', self.html)

    def test_photo_falls_back_to_initials_on_error(self):
        # Mirror the chat.avatar_url @error pattern: null the url so the
        # initials template renders instead.
        self.assertIn('v-if="msg.sender_avatar_url"', self.html)
        self.assertIn('@error="msg.sender_avatar_url = null"', self.html)
        self.assertIn("{{ getSenderInitials(msg) }}", self.html)

    def test_sender_avatar_img_is_lazy(self):
        # The sender-avatar <img> must be lazy like every other <img> so a group
        # page doesn't eagerly fetch every member avatar.
        start = self.html.index('v-if="msg.sender_avatar_url"')
        img = self.html[start : start + 300]
        self.assertIn('loading="lazy"', img)

    def test_deferred_download_is_documented(self):
        # A visible note that proactive member-avatar download is deferred.
        self.assertIn("slice 2b", self.html)


# ---------------------------------------------------------------------------
# US-211 backend coverage (FIX 4): real adapter query + real-endpoint ACL /
# sender_avatar_url wiring.
# ---------------------------------------------------------------------------


@pytest.fixture
async def adapter():
    """Real in-memory SQLite adapter (mirrors test_messages_page_batching)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    yield DatabaseAdapter(db_manager)
    await engine.dispose()


async def _seed_message(adapter, *, msg_id, chat_id, sender_id):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            Message(
                id=msg_id,
                chat_id=chat_id,
                sender_id=sender_id,
                date=datetime(2026, 1, 1, 12, 0, 0),
                text="hi",
            )
        )
        await session.commit()


class TestSenderHasMessageInChats:
    """FIX 4a: direct adapter test of the membership probe (real SQLite)."""

    async def test_true_when_user_spoke_in_visible_chat(self, adapter):
        await _seed_message(adapter, msg_id=1, chat_id=-500, sender_id=42)
        assert await adapter.sender_has_message_in_chats(42, [-500]) is True

    async def test_false_when_user_not_in_visible_chats(self, adapter):
        # User 42 spoke only in -500; probing a different chat set → False.
        await _seed_message(adapter, msg_id=1, chat_id=-500, sender_id=42)
        assert await adapter.sender_has_message_in_chats(42, [-999]) is False
        # A different user who never spoke → False.
        assert await adapter.sender_has_message_in_chats(77, [-500]) is False

    async def test_false_for_empty_chat_ids_must_not_match_all(self, adapter):
        # Empty scope must NEVER match-all (would leak avatars to unauthorized
        # viewers). Even with a matching message present, empty → False.
        await _seed_message(adapter, msg_id=1, chat_id=-500, sender_id=42)
        assert await adapter.sender_has_message_in_chats(42, []) is False


@unittest.skipUnless(_HTTPX_AVAILABLE, "httpx not available")
class TestMemberAvatarAclEndpoint(unittest.IsolatedAsyncioTestCase):
    """FIX 4b: ACL allow/block through the REAL /media endpoint."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "avatars" / "users").mkdir(parents=True)
        (root / "avatars" / "chats").mkdir(parents=True)
        (root / "avatars" / "users" / "42_1.jpg").write_bytes(b"x")  # member
        (root / "avatars" / "users" / "77_1.jpg").write_bytes(b"x")  # non-member
        (root / "avatars" / "chats" / "-500_1.jpg").write_bytes(b"x")  # visible chat
        (root / "avatars" / "chats" / "-999_1.jpg").write_bytes(b"x")  # other chat

        self._saved_root = web_main._media_root
        self._saved_db = web_main.db
        self._saved_display = web_main.config.display_chat_ids
        web_main._media_root = root.resolve()
        web_main.config.display_chat_ids = set()
        web_main._avatar_member_cache.clear()
        web_main._avatar_member_cache_time = None

        # Restricted viewer authorized only for chat -500.
        viewer = web_main.UserContext(username="v", role="viewer", allowed_chat_ids={-500})
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: viewer

        # Membership probe: user 42 spoke in -500, user 77 did not.
        async def _probe(user_id, chat_ids):
            return user_id == 42 and -500 in set(chat_ids)

        self.mock_db = AsyncMock()
        self.mock_db.sender_has_message_in_chats = AsyncMock(side_effect=_probe)
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.app.dependency_overrides.pop(web_main.require_auth, None)
        web_main._media_root = self._saved_root
        web_main.db = self._saved_db
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_member_cache.clear()
        web_main._avatar_member_cache_time = None
        self.temp_dir.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def test_member_avatar_allowed(self):
        async with self._client() as client:
            resp = await client.get("/media/avatars/users/42_1.jpg")
        self.assertEqual(resp.status_code, 200)

    async def test_non_member_avatar_blocked(self):
        async with self._client() as client:
            resp = await client.get("/media/avatars/users/77_1.jpg")
        self.assertEqual(resp.status_code, 403)

    async def test_chat_avatar_visible_allowed_and_other_blocked(self):
        async with self._client() as client:
            ok = await client.get("/media/avatars/chats/-500_1.jpg")
            blocked = await client.get("/media/avatars/chats/-999_1.jpg")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(blocked.status_code, 403)

    async def test_db_error_fails_closed_403_not_500(self):
        """A DB error in the membership probe denies the avatar (403), never 500."""
        self.mock_db.sender_has_message_in_chats = AsyncMock(side_effect=RuntimeError("db down"))
        async with self._client() as client:
            resp = await client.get("/media/avatars/users/42_1.jpg")
        self.assertEqual(resp.status_code, 403)


@unittest.skipUnless(_HTTPX_AVAILABLE, "httpx not available")
class TestMessagesEndpointAvatarWiring(unittest.IsolatedAsyncioTestCase):
    """FIX 4c: GET /messages attaches sender_avatar_url via the endpoint."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        avatars = Path(self.temp_dir.name) / "avatars" / "users"
        avatars.mkdir(parents=True)
        (avatars / "42_1.jpg").write_bytes(b"x")  # user 42 has a file on disk

        # _sender_avatar_url resolves via config.media_path + _avatar_cache.
        self._saved_media_path = web_main.config.media_path
        self._saved_db = web_main.db
        self._saved_display = web_main.config.display_chat_ids
        web_main.config.media_path = self.temp_dir.name
        web_main.config.display_chat_ids = set()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

        viewer = web_main.UserContext(username="v", role="master")
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: viewer

        self.mock_db = AsyncMock()
        self.mock_db.get_messages_paginated = AsyncMock(
            return_value=[
                {"id": 1, "sender_id": 42, "chat_id": -500},
                {"id": 2, "sender_id": 77, "chat_id": -500},
            ]
        )
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.app.dependency_overrides.pop(web_main.require_auth, None)
        web_main.config.media_path = self._saved_media_path
        web_main.db = self._saved_db
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def test_sender_avatar_url_present_when_file_globs_and_null_when_absent(self):
        async with self._client() as client:
            resp = await client.get("/api/chats/-500/messages")
        self.assertEqual(resp.status_code, 200)
        by_id = {m["id"]: m for m in resp.json()}
        self.assertEqual(by_id[1]["sender_avatar_url"], "/media/avatars/users/42_1.jpg")
        self.assertIsNone(by_id[2]["sender_avatar_url"])


if __name__ == "__main__":
    unittest.main()
