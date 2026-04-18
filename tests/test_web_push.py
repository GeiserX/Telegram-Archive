"""Tests for web push notification manager (src/web/push.py).

The push module depends on py_vapid and pywebpush which may not be installed
locally.  A module-level guard skips all tests gracefully when unavailable.
They will pass on CI where all dependencies are present.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import src.web.push as push_mod
    from src.web.push import PushNotificationManager

    _PUSH_AVAILABLE = True
except Exception:
    _PUSH_AVAILABLE = False
    push_mod = None  # type: ignore[assignment]
    PushNotificationManager = None  # type: ignore[assignment, misc]


def _skip_unless_push(cls_or_fn):
    """Skip test class/method when push module could not be imported."""
    return unittest.skipUnless(_PUSH_AVAILABLE, "src.web.push import failed (missing py_vapid/pywebpush)")(cls_or_fn)


def _make_manager(push_setting="full", vapid_private=None, vapid_public=None):
    """Helper: create a PushNotificationManager with mock db/config."""
    db = MagicMock()
    cfg = MagicMock()
    cfg.push_notifications = push_setting
    cfg.vapid_private_key = vapid_private or ""
    cfg.vapid_public_key = vapid_public or ""
    cfg.vapid_contact = "mailto:test@example.com"
    return PushNotificationManager(db, cfg)


# ============================================================================
# PushNotificationManager.__init__ and properties
# ============================================================================


@_skip_unless_push
class TestPushNotificationManagerInit(unittest.TestCase):
    """Test PushNotificationManager.__init__ and property defaults."""

    def test_initial_state_has_no_vapid(self):
        """New manager has no VAPID keys and is not enabled."""
        mgr = _make_manager()
        self.assertIsNone(mgr.public_key)
        self.assertFalse(mgr.is_enabled)

    def test_is_enabled_requires_vapid_and_full_mode(self):
        """is_enabled is False when _vapid is None even if mode is full."""
        mgr = _make_manager(push_setting="full")
        self.assertFalse(mgr.is_enabled)

    def test_is_enabled_false_when_mode_is_basic(self):
        """is_enabled is False when mode is basic regardless of _vapid."""
        mgr = _make_manager(push_setting="basic")
        mgr._vapid = MagicMock()
        self.assertFalse(mgr.is_enabled)

    def test_is_enabled_true_when_vapid_set_and_full(self):
        """is_enabled is True when _vapid exists and mode is full."""
        mgr = _make_manager(push_setting="full")
        mgr._vapid = MagicMock()
        self.assertTrue(mgr.is_enabled)


# ============================================================================
# initialize() — disabled modes
# ============================================================================


@_skip_unless_push
class TestInitializeDisabledModes(unittest.IsolatedAsyncioTestCase):
    """Test initialize() returns False for off/basic modes."""

    async def test_initialize_returns_false_when_off(self):
        """initialize() returns False when push_notifications=off."""
        mgr = _make_manager("off")
        result = await mgr.initialize()
        self.assertFalse(result)

    async def test_initialize_returns_false_when_basic(self):
        """initialize() returns False when push_notifications=basic."""
        mgr = _make_manager("basic")
        result = await mgr.initialize()
        self.assertFalse(result)


# ============================================================================
# initialize() — key loading and generation
# ============================================================================


@_skip_unless_push
class TestInitializeWithEnvKeys(unittest.IsolatedAsyncioTestCase):
    """Test initialize() with VAPID keys from environment/config."""

    async def test_uses_env_keys_when_provided(self):
        """initialize() uses keys from config when both are set."""
        mgr = _make_manager(
            vapid_private="-----BEGIN EC PRIVATE KEY-----\nfake\n-----END EC PRIVATE KEY-----",
            vapid_public="BFAKE_PUBLIC_KEY_BASE64",
        )

        with patch("src.web.push.Vapid") as mock_vapid_cls:
            mock_vapid_cls.from_pem.return_value = MagicMock()
            result = await mgr.initialize()

        self.assertTrue(result)
        self.assertEqual(mgr._public_key, "BFAKE_PUBLIC_KEY_BASE64")
        mock_vapid_cls.from_pem.assert_called_once()

    async def test_loads_keys_from_database_when_env_empty(self):
        """initialize() loads keys from database when env keys are empty."""
        mgr = _make_manager()
        mgr.db.get_metadata = AsyncMock(
            side_effect=lambda k: {
                "vapid_private_key": "-----BEGIN EC PRIVATE KEY-----\ndb_key\n-----END EC PRIVATE KEY-----",
                "vapid_public_key": "BDB_PUBLIC_KEY",
            }.get(k)
        )

        with patch("src.web.push.Vapid") as mock_vapid_cls:
            mock_vapid_cls.from_pem.return_value = MagicMock()
            result = await mgr.initialize()

        self.assertTrue(result)
        self.assertEqual(mgr._public_key, "BDB_PUBLIC_KEY")

    async def test_generates_new_keys_when_none_exist(self):
        """initialize() generates new VAPID keys when none found anywhere."""
        mgr = _make_manager()
        mgr.db.get_metadata = AsyncMock(return_value=None)
        mgr.db.set_metadata = AsyncMock()

        mock_vapid_instance = MagicMock()
        mock_vapid_instance.private_pem.return_value = (
            b"-----BEGIN EC PRIVATE KEY-----\nnew\n-----END EC PRIVATE KEY-----"
        )
        mock_pub_key = MagicMock()
        mock_pub_key.public_bytes.return_value = b"\x04fake_public_bytes"
        mock_vapid_instance.public_key = mock_pub_key

        with (
            patch("src.web.push.Vapid") as mock_vapid_cls,
            patch("src.web.push.b64urlencode", return_value="BNEW_ENCODED_KEY"),
        ):
            mock_vapid_cls.return_value = mock_vapid_instance
            mock_vapid_cls.from_pem.return_value = MagicMock()
            result = await mgr.initialize()

        self.assertTrue(result)
        self.assertEqual(mgr._public_key, "BNEW_ENCODED_KEY")
        # Should store both keys in DB
        self.assertEqual(mgr.db.set_metadata.await_count, 2)

    async def test_initialize_returns_false_on_vapid_creation_error(self):
        """initialize() returns False when VAPID key parsing fails."""
        mgr = _make_manager()
        mgr.db.get_metadata = AsyncMock(return_value=None)
        mgr.db.set_metadata = AsyncMock()

        mock_vapid_instance = MagicMock()
        mock_vapid_instance.private_pem.return_value = b"badkey"
        mock_pub_key = MagicMock()
        mock_pub_key.public_bytes.return_value = b"\x04data"
        mock_vapid_instance.public_key = mock_pub_key

        with (
            patch("src.web.push.Vapid") as mock_vapid_cls,
            patch("src.web.push.b64urlencode", return_value="BKEY"),
        ):
            mock_vapid_cls.return_value = mock_vapid_instance
            # from_string also fails since key doesn't contain BEGIN
            mock_vapid_cls.from_string.side_effect = Exception("bad key format")
            result = await mgr.initialize()

        self.assertFalse(result)


# ============================================================================
# send_notification
# ============================================================================


def _make_enabled_manager():
    """Helper: create a manager with push enabled and _vapid set."""
    mgr = _make_manager(push_setting="full")
    mgr._vapid = MagicMock()
    mgr._vapid.sign.return_value = {"Authorization": "vapid t=token"}
    return mgr


@_skip_unless_push
class TestSendNotification(unittest.IsolatedAsyncioTestCase):
    """Test send_notification behavior."""

    async def test_returns_zero_when_not_enabled(self):
        """send_notification returns 0 when push is not enabled."""
        mgr = _make_manager(push_setting="off")
        result = await mgr.send_notification("title", "body")
        self.assertEqual(result, 0)

    async def test_returns_zero_when_no_subscriptions(self):
        """send_notification returns 0 when there are no subscribers."""
        mgr = _make_enabled_manager()
        mgr.get_subscriptions = AsyncMock(return_value=[])
        result = await mgr.send_notification("title", "body")
        self.assertEqual(result, 0)

    async def test_sends_to_all_subscriptions(self):
        """send_notification sends to each subscription and returns count."""
        mgr = _make_enabled_manager()
        subs = [
            {"endpoint": "https://push.example.com/sub1", "keys": {"p256dh": "k1", "auth": "a1"}},
            {"endpoint": "https://push.example.com/sub2", "keys": {"p256dh": "k2", "auth": "a2"}},
        ]
        mgr.get_subscriptions = AsyncMock(return_value=subs)

        with patch("src.web.push.webpush") as mock_webpush:
            result = await mgr.send_notification("Test", "Hello", chat_id=123)

        self.assertEqual(result, 2)
        self.assertEqual(mock_webpush.call_count, 2)

    async def test_handles_expired_subscription_410(self):
        """send_notification removes expired (410) subscriptions."""
        mgr = _make_enabled_manager()
        subs = [
            {"endpoint": "https://push.example.com/expired", "keys": {"p256dh": "k", "auth": "a"}},
        ]
        mgr.get_subscriptions = AsyncMock(return_value=subs)
        mgr.unsubscribe = AsyncMock()

        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 410

        with patch("src.web.push.webpush", side_effect=WebPushException("Gone", response=mock_response)):
            result = await mgr.send_notification("Test", "Expired")

        self.assertEqual(result, 0)
        mgr.unsubscribe.assert_awaited_once_with("https://push.example.com/expired")

    async def test_handles_blocked_subscription_403(self):
        """send_notification removes blocked (403) subscriptions."""
        mgr = _make_enabled_manager()
        subs = [
            {"endpoint": "https://push.example.com/blocked", "keys": {"p256dh": "k", "auth": "a"}},
        ]
        mgr.get_subscriptions = AsyncMock(return_value=subs)
        mgr.unsubscribe = AsyncMock()

        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("src.web.push.webpush", side_effect=WebPushException("Forbidden", response=mock_response)):
            result = await mgr.send_notification("Test", "Blocked")

        self.assertEqual(result, 0)
        mgr.unsubscribe.assert_awaited_once()

    async def test_payload_includes_required_fields(self):
        """send_notification payload includes title, body, icon, tag, data, timestamp."""
        mgr = _make_enabled_manager()
        subs = [{"endpoint": "https://push.example.com/s", "keys": {"p256dh": "k", "auth": "a"}}]
        mgr.get_subscriptions = AsyncMock(return_value=subs)

        with patch("src.web.push.webpush") as mock_webpush:
            await mgr.send_notification("Title", "Body", chat_id=42, icon="/icon.png", tag="my-tag")

        call_kwargs = mock_webpush.call_args
        payload = json.loads(call_kwargs.kwargs["data"] if "data" in call_kwargs.kwargs else call_kwargs[1]["data"])
        self.assertEqual(payload["title"], "Title")
        self.assertEqual(payload["body"], "Body")
        self.assertEqual(payload["icon"], "/icon.png")
        self.assertEqual(payload["tag"], "my-tag")
        self.assertIn("timestamp", payload)


# ============================================================================
# notify_new_message
# ============================================================================


@_skip_unless_push
class TestNotifyNewMessage(unittest.IsolatedAsyncioTestCase):
    """Test notify_new_message convenience method."""

    async def test_truncates_long_messages(self):
        """notify_new_message truncates message preview at 100 chars."""
        mgr = _make_manager()
        mgr.send_notification = AsyncMock(return_value=1)

        long_text = "x" * 200
        await mgr.notify_new_message(
            chat_id=1, chat_title="Chat", sender_name="Alice", message_text=long_text, message_id=99
        )

        call_kwargs = mgr.send_notification.call_args.kwargs
        self.assertTrue(call_kwargs["body"].endswith("..."))
        self.assertIn("Alice: ", call_kwargs["body"])

    async def test_short_message_not_truncated(self):
        """notify_new_message does not truncate short messages."""
        mgr = _make_manager()
        mgr.send_notification = AsyncMock(return_value=1)

        await mgr.notify_new_message(
            chat_id=1, chat_title="Chat", sender_name="Bob", message_text="Hello", message_id=10
        )

        call_kwargs = mgr.send_notification.call_args.kwargs
        self.assertEqual(call_kwargs["body"], "Bob: Hello")

    async def test_no_sender_name_omits_prefix(self):
        """notify_new_message omits sender prefix when sender_name is empty."""
        mgr = _make_manager()
        mgr.send_notification = AsyncMock(return_value=1)

        await mgr.notify_new_message(chat_id=1, chat_title="Group", sender_name="", message_text="Hi", message_id=5)

        call_kwargs = mgr.send_notification.call_args.kwargs
        self.assertEqual(call_kwargs["body"], "Hi")

    async def test_data_includes_url_and_message_id(self):
        """notify_new_message passes correct data with url and type."""
        mgr = _make_manager()
        mgr.send_notification = AsyncMock(return_value=1)

        await mgr.notify_new_message(chat_id=42, chat_title="Test", sender_name="X", message_text="msg", message_id=7)

        call_kwargs = mgr.send_notification.call_args.kwargs
        self.assertEqual(call_kwargs["data"]["type"], "new_message")
        self.assertEqual(call_kwargs["data"]["chat_id"], 42)
        self.assertEqual(call_kwargs["data"]["message_id"], 7)
        self.assertEqual(call_kwargs["data"]["url"], "/?chat=42&msg=7")
        self.assertEqual(call_kwargs["tag"], "chat-42")


# ============================================================================
# get_push_manager singleton
# ============================================================================


@_skip_unless_push
class TestGetPushManagerSingleton(unittest.IsolatedAsyncioTestCase):
    """Test get_push_manager singleton factory."""

    async def test_creates_singleton_on_first_call(self):
        """get_push_manager creates a new manager on first call."""
        push_mod._push_manager = None

        mock_db = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.push_notifications = "off"

        mgr = await push_mod.get_push_manager(mock_db, mock_cfg)
        self.assertIsNotNone(mgr)
        self.assertIs(push_mod._push_manager, mgr)

    async def test_returns_same_instance_on_second_call(self):
        """get_push_manager returns cached singleton on subsequent calls."""
        push_mod._push_manager = None

        mock_db = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.push_notifications = "off"

        mgr1 = await push_mod.get_push_manager(mock_db, mock_cfg)
        mgr2 = await push_mod.get_push_manager(mock_db, mock_cfg)
        self.assertIs(mgr1, mgr2)

    def tearDown(self):
        if push_mod is not None:
            push_mod._push_manager = None


if __name__ == "__main__":
    unittest.main()
