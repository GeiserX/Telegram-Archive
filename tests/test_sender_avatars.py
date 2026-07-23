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
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_backup_"))

from src.web import main as web_main  # noqa: E402

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

    def test_nameless_sender_falls_back_to_question_mark(self):
        """Deleted / nameless senders must render '?' not the 'DA' chat fallback."""
        start = self.html.index("const getSenderInitials = (msg) =>")
        body = self.html[start : start + 600]
        self.assertIn("let initials = '?'", body)
        # It must build from first_name/last_name, not getChatName.
        self.assertIn("[msg.first_name, msg.last_name].filter(Boolean)", body)
        self.assertNotIn("getChatName", body)

    def test_avatar_fill_is_the_darker_gradient(self):
        start = self.html.index("const getAvatarFill = (msg) =>")
        body = self.html[start : start + 500]
        self.assertIn("linear-gradient(135deg, hsl(${hue}, 65%, 27%), hsl(${hue}, 65%, 18%))", body)


class TestSenderInitialsLogic(unittest.TestCase):
    """US-210: replicate getSenderInitials semantics to lock the contract."""

    @staticmethod
    def _initials(first, last):
        name = " ".join(p for p in (first, last) if p).strip()
        if not name:
            return "?"
        return "".join(part[0] for part in name.split())[:2].upper()

    def test_two_names_two_letters(self):
        self.assertEqual(self._initials("Ada", "Lovelace"), "AL")

    def test_one_name_one_letter(self):
        self.assertEqual(self._initials("Grace", None), "G")
        self.assertEqual(self._initials("grace", ""), "G")

    def test_empty_returns_question_mark(self):
        self.assertEqual(self._initials(None, None), "?")
        self.assertEqual(self._initials("", ""), "?")


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

    def test_deferred_download_is_documented(self):
        # A visible note that proactive member-avatar download is deferred.
        self.assertIn("slice 2b", self.html)


if __name__ == "__main__":
    unittest.main()
