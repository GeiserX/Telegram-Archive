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
    """US-211 as reshaped by v8.0: per-message sender_avatar_url points at
    /media/avatar/{chat_ref}/{message_id} — present only when the sender's
    avatar file is already on disk, and carrying no user id in the URL."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_media_path = web_main.config.media_path
        web_main.config.media_path = self.temp_dir.name
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.chat = web_main.ChatContext(account_id=1, chat_id=-1001, ref="senderAvatarRef001AB", type="group")

    def tearDown(self):
        web_main.config.media_path = self.original_media_path
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def _touch_avatar(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as avatar_file:
            avatar_file.write("x")

    def _attached(self, message: dict) -> dict:
        web_main._attach_message_payload_urls([message], self.chat)
        return message

    def test_present_when_file_globs(self):
        user_id = 555000111
        avatars_dir = os.path.join(self.temp_dir.name, "avatars", "users")
        self._touch_avatar(os.path.join(avatars_dir, f"{user_id}_42.jpg"))

        message = self._attached({"id": 7, "sender_id": user_id})
        self.assertEqual(message["sender_avatar_url"], "/media/avatar/senderAvatarRef001AB/7")
        # The proof of the phase: neither the user id nor a filename in the URL.
        self.assertNotIn(str(user_id), message["sender_avatar_url"])

    def test_null_when_absent(self):
        message = self._attached({"id": 7, "sender_id": 999888777})
        self.assertIsNone(message["sender_avatar_url"])

    def test_null_for_missing_or_non_user_sender(self):
        self.assertIsNone(self._attached({"id": 7, "sender_id": None})["sender_avatar_url"])
        # Negative ids are channels/groups, never a users/ avatar.
        self.assertIsNone(self._attached({"id": 7, "sender_id": -1001234})["sender_avatar_url"])


class TestSenderResolution(unittest.TestCase):
    """US-211's successor: the membership probe is gone. Entitlement to the chat
    is the membership proof, and the sender is resolved from the MESSAGE row
    (_message_sender_id), so no arbitrary user id can be probed at all."""

    def setUp(self):
        web_main._sender_lookup_cache.clear()
        self.chat = web_main.ChatContext(account_id=1, chat_id=-1001, ref="senderLookupRef001AB", type="group")

    def tearDown(self):
        web_main._sender_lookup_cache.clear()

    def _with_db(self, lookup):
        original_db = web_main.db
        web_main.db = type("D", (), {"get_message_sender_id": staticmethod(lookup)})()
        return original_db

    def test_sender_resolved_from_the_message_row(self):
        """The db lookup receives the resolved chat's identity, never URL input."""
        lookup = AsyncMock(return_value=555)
        original_db = self._with_db(lookup)
        try:
            self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 9)), 555)
            lookup.assert_awaited_once_with(-1001, 9, account_id=1)
        finally:
            web_main.db = original_db

    def test_sender_lookup_is_cached_across_requests(self):
        """The row lookup runs once per (account, chat, message); repeats hit the cache."""
        lookup = AsyncMock(return_value=555)
        original_db = self._with_db(lookup)
        try:
            for _ in range(3):
                self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 9)), 555)
            lookup.assert_awaited_once()
        finally:
            web_main.db = original_db

    def test_missing_sender_is_cached_as_none(self):
        """A message without a sender resolves (and caches) None — the route 404s."""
        lookup = AsyncMock(return_value=None)
        original_db = self._with_db(lookup)
        try:
            self.assertIsNone(asyncio.run(web_main._message_sender_id(self.chat, 9)))
        finally:
            web_main.db = original_db

    def test_cache_is_scoped_by_message(self):
        """Different messages never share a cached sender."""
        lookup = AsyncMock(side_effect=[555, 777])
        original_db = self._with_db(lookup)
        try:
            self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 1)), 555)
            self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 2)), 777)
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

    def test_imported_supergroups_are_treated_as_groups(self):
        start = self.html.index("const isGroup = computed(() =>")
        body = self.html[start : start + 400]
        self.assertIn("'supergroup'", body)

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
    """FIX 4b reshaped: avatar allow/deny through the REAL v8.0 avatar routes.

    /media/avatar/{chat_ref}[/{message_id}] — entitlement to the chat is the
    membership proof; the sender comes from the message row; user-id-addressed
    avatar URLs no longer exist, so no arbitrary user can even be probed.
    """

    VISIBLE_REF = "visibleChatRef0500AB"
    OTHER_REF = "otherChatRef00999ABC"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "avatars" / "users").mkdir(parents=True)
        (root / "avatars" / "chats").mkdir(parents=True)
        (root / "avatars" / "users" / "42_1.jpg").write_bytes(b"x")  # sender of msg 1
        (root / "avatars" / "chats" / "-500_1.jpg").write_bytes(b"x")  # visible chat
        (root / "avatars" / "chats" / "-999_1.jpg").write_bytes(b"x")  # other chat

        self._saved_root = web_main._media_root
        self._saved_media_path = web_main.config.media_path
        self._saved_db = web_main.db
        self._saved_display = web_main.config.display_chat_ids
        web_main._media_root = root.resolve()
        web_main.config.media_path = self.temp_dir.name  # avatar lookup root
        web_main.config.display_chat_ids = set()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._sender_lookup_cache.clear()

        # Restricted viewer authorized only for the visible chat's ref.
        viewer = web_main.UserContext(username="v", role="viewer", allowed_chat_refs={self.VISIBLE_REF})
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: viewer

        chats = {
            self.VISIBLE_REF: {"id": -500, "account_id": 1, "ref": self.VISIBLE_REF, "type": "group"},
            self.OTHER_REF: {"id": -999, "account_id": 1, "ref": self.OTHER_REF, "type": "group"},
        }

        async def _by_ref(ref, **kwargs):
            return chats.get(ref)

        # Message 1 in the visible chat was sent by user 42; message 2 has no sender.
        async def _sender(chat_id, message_id, account_id=None):
            if chat_id == -500 and message_id == 1:
                return 42
            return None

        self.mock_db = AsyncMock()
        self.mock_db.get_chat_by_ref = AsyncMock(side_effect=_by_ref)
        self.mock_db.get_message_sender_id = AsyncMock(side_effect=_sender)
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.app.dependency_overrides.pop(web_main.require_auth, None)
        web_main._media_root = self._saved_root
        web_main.config.media_path = self._saved_media_path
        web_main.db = self._saved_db
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._sender_lookup_cache.clear()
        self.temp_dir.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def test_member_avatar_allowed(self):
        """The avatar of a message's sender in an entitled chat is served."""
        async with self._client() as client:
            resp = await client.get(f"/media/avatar/{self.VISIBLE_REF}/1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("private", resp.headers.get("cache-control", ""))

    async def test_unknown_message_or_forbidden_chat_blocked(self):
        """No sender resolvable → 404; a chat outside the grant → the SAME 404."""
        async with self._client() as client:
            no_sender = await client.get(f"/media/avatar/{self.VISIBLE_REF}/2")
            forbidden = await client.get(f"/media/avatar/{self.OTHER_REF}/1")
        self.assertEqual(no_sender.status_code, 404)
        self.assertEqual(forbidden.status_code, 404)

    async def test_chat_avatar_visible_allowed_and_other_blocked(self):
        async with self._client() as client:
            ok = await client.get(f"/media/avatar/{self.VISIBLE_REF}")
            blocked = await client.get(f"/media/avatar/{self.OTHER_REF}")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(blocked.status_code, 404)

    async def test_legacy_user_addressed_avatar_url_is_gone(self):
        """The old /media/avatars/users/{id}_... shape 404s at routing — user ids
        are simply unaddressable now."""
        async with self._client() as client:
            resp = await client.get("/media/avatars/users/42_1.jpg")
        self.assertEqual(resp.status_code, 404)

    async def test_db_error_fails_closed_no_bytes(self):
        """A DB error in the sender lookup serves no bytes: a connection-shaped
        error answers 503 (the redacting handler's split), never the avatar."""
        self.mock_db.get_message_sender_id = AsyncMock(side_effect=ConnectionRefusedError("db down"))
        async with self._client() as client:
            resp = await client.get(f"/media/avatar/{self.VISIBLE_REF}/1")
        self.assertEqual(resp.status_code, 503)
        self.assertNotEqual(resp.content, b"x")


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
        self.mock_db.get_chat_by_ref = AsyncMock(
            return_value={"id": -500, "account_id": 1, "ref": "wiringChatRef0500ABC", "type": "group"}
        )
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
            resp = await client.get("/api/chats/wiringChatRef0500ABC/messages")
        self.assertEqual(resp.status_code, 200)
        by_id = {m["id"]: m for m in resp.json()}
        # Ref+message addressed: no user id, no filename in the URL.
        self.assertEqual(by_id[1]["sender_avatar_url"], "/media/avatar/wiringChatRef0500ABC/1")
        self.assertIsNone(by_id[2]["sender_avatar_url"])


if __name__ == "__main__":
    unittest.main()
