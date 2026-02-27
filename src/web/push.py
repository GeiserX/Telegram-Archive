"""
Web Push notifications for Telegram Archive viewer.

This module handles:
- VAPID key generation and management
- Push subscription storage and retrieval
- Sending push notifications to subscribed clients
"""

import json
import logging
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)


class PushNotificationManager:
    """Manages Web Push notifications for the viewer."""

    def __init__(self, db_adapter, config):
        self.db = db_adapter
        self.config = config
        self._vapid: Vapid | None = None
        self._public_key: str | None = None
        self._private_key: str | None = None

    async def initialize(self) -> bool:
        """
        Initialize push notifications.

        Loads or generates VAPID keys and stores them persistently.
        Returns True if push notifications are enabled and ready.
        """
        if self.config.push_notifications == "off":
            logger.info("Push notifications disabled (PUSH_NOTIFICATIONS=off)")
            return False

        if self.config.push_notifications == "basic":
            logger.info("Using basic in-browser notifications (PUSH_NOTIFICATIONS=basic)")
            return False

        # Full push notifications mode
        logger.info("Initializing Web Push notifications (PUSH_NOTIFICATIONS=full)")

        # Check for existing VAPID keys in config or database
        if self.config.vapid_private_key and self.config.vapid_public_key:
            # Use keys from environment
            self._private_key = self.config.vapid_private_key
            self._public_key = self.config.vapid_public_key
            logger.info("Using VAPID keys from environment variables")
        else:
            # Try to load from database
            stored_private = await self.db.get_metadata("vapid_private_key")
            stored_public = await self.db.get_metadata("vapid_public_key")

            if stored_private and stored_public:
                self._private_key = stored_private
                self._public_key = stored_public
                logger.info("Loaded VAPID keys from database")
            else:
                # Generate new keys
                logger.info("Generating new VAPID keys...")
                vapid = Vapid()
                vapid.generate_keys()

                # Get private key as PEM
                private_pem = vapid.private_pem()
                self._private_key = private_pem.decode("utf-8") if isinstance(private_pem, bytes) else private_pem

                # Get public key as URL-safe base64
                public_bytes = vapid.public_key.public_bytes(
                    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
                )
                self._public_key = b64urlencode(public_bytes)

                # Store in database for persistence across restarts
                await self.db.set_metadata("vapid_private_key", self._private_key)
                await self.db.set_metadata("vapid_public_key", self._public_key)
                logger.info("Generated and stored new VAPID keys")

        # Create VAPID instance from the stored private key
        try:
            # Try PEM format first (our default storage format)
            if "-----BEGIN" in self._private_key:
                self._vapid = Vapid.from_pem(self._private_key.encode("utf-8"))
            else:
                # Try DER/raw format
                self._vapid = Vapid.from_string(self._private_key)
            logger.info(f"Web Push initialized. Public key: {self._public_key[:20]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize VAPID: {e}")
            return False

    @property
    def public_key(self) -> str | None:
        """Get the VAPID public key for client subscription."""
        return self._public_key

    @property
    def is_enabled(self) -> bool:
        """Check if full push notifications are enabled."""
        return self._vapid is not None and self.config.push_notifications == "full"

    async def subscribe(
        self,
        endpoint: str,
        p256dh: str,
        auth: str,
        chat_id: int | None = None,
        user_agent: str | None = None,
        username: str | None = None,
        allowed_chat_ids: list[int] | None = None,
    ) -> bool:
        """
        Store a push subscription with user ownership.

        Args:
            endpoint: Push service URL
            p256dh: Client public key (base64)
            auth: Auth secret (base64)
            chat_id: Optional chat ID for chat-specific subscriptions
            user_agent: Browser user agent for debugging
            username: The authenticated user who created this subscription
            allowed_chat_ids: Snapshot of the user's allowed chats (None = master/all access)

        Returns:
            True if subscription was stored successfully
        """
        try:
            from sqlalchemy import select

            from src.db.models import PushSubscription

            allowed_json = json.dumps(allowed_chat_ids) if allowed_chat_ids is not None else None

            async with self.db.db_manager.async_session_factory() as session:
                result = await session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
                existing = result.scalar_one_or_none()

                if existing:
                    existing.p256dh = p256dh
                    existing.auth = auth
                    existing.chat_id = chat_id
                    existing.user_agent = user_agent
                    existing.username = username
                    existing.allowed_chat_ids = allowed_json
                    existing.last_used_at = datetime.utcnow()
                else:
                    sub = PushSubscription(
                        endpoint=endpoint,
                        p256dh=p256dh,
                        auth=auth,
                        chat_id=chat_id,
                        user_agent=user_agent,
                        username=username,
                        allowed_chat_ids=allowed_json,
                        created_at=datetime.utcnow(),
                    )
                    session.add(sub)

                await session.commit()
                logger.info(f"Push subscription stored for {username or 'anonymous'}: {endpoint[:50]}...")
                return True

        except Exception as e:
            logger.error(f"Failed to store push subscription: {e}")
            return False

    async def unsubscribe(self, endpoint: str) -> bool:
        """Remove a push subscription."""
        try:
            from sqlalchemy import delete

            from src.db.models import PushSubscription

            async with self.db.db_manager.async_session_factory() as session:
                await session.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))
                await session.commit()
                logger.info(f"Push subscription removed: {endpoint[:50]}...")
                return True

        except Exception as e:
            logger.error(f"Failed to remove push subscription: {e}")
            return False

    async def get_subscriptions(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        """
        Get push subscriptions for a given chat, filtered by per-user permissions.

        Only returns subscriptions where:
        - The user is master (allowed_chat_ids is NULL) and subscribed globally or to this chat
        - The user is a viewer whose allowed_chat_ids includes this chat_id
        """
        try:
            from sqlalchemy import or_, select

            from src.db.models import PushSubscription

            async with self.db.db_manager.async_session_factory() as session:
                query = select(PushSubscription)

                if chat_id is not None:
                    query = query.where(or_(PushSubscription.chat_id.is_(None), PushSubscription.chat_id == chat_id))

                result = await session.execute(query)
                subs = result.scalars().all()

                filtered = []
                for sub in subs:
                    if sub.allowed_chat_ids is not None:
                        try:
                            user_chats = json.loads(sub.allowed_chat_ids)
                            if chat_id not in user_chats:
                                continue
                        except (json.JSONDecodeError, TypeError):
                            continue
                    filtered.append({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}})

                return filtered

        except Exception as e:
            logger.error(f"Failed to get push subscriptions: {e}")
            return []

    async def send_notification(
        self,
        title: str,
        body: str,
        chat_id: int | None = None,
        data: dict[str, Any] | None = None,
        icon: str | None = None,
        tag: str | None = None,
    ) -> int:
        """
        Send push notification to all relevant subscribers.

        Args:
            title: Notification title
            body: Notification body text
            chat_id: Optional chat ID to filter subscribers
            data: Additional data to include in notification
            icon: URL for notification icon
            tag: Tag for notification grouping/replacement

        Returns:
            Number of notifications successfully sent
        """
        if not self.is_enabled:
            return 0

        subscriptions = await self.get_subscriptions(chat_id)

        if not subscriptions:
            return 0

        payload = {
            "title": title,
            "body": body,
            "icon": icon or "/static/favicon.ico",
            "tag": tag or f"telegram-archive-{chat_id or 'all'}",
            "data": data or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

        sent = 0
        failed_endpoints = []

        for sub in subscriptions:
            try:
                # Extract origin from endpoint for VAPID audience claim
                from urllib.parse import urlparse

                endpoint_url = urlparse(sub["endpoint"])
                audience = f"{endpoint_url.scheme}://{endpoint_url.netloc}"

                # Generate VAPID headers using py_vapid
                vapid_headers = self._vapid.sign({"sub": self.config.vapid_contact, "aud": audience})

                webpush(subscription_info=sub, data=json.dumps(payload), headers=vapid_headers)
                sent += 1
            except WebPushException as e:
                if e.response and e.response.status_code in (404, 410):
                    # Subscription expired or unsubscribed
                    failed_endpoints.append(sub["endpoint"])
                    logger.debug(f"Push subscription expired: {sub['endpoint'][:50]}...")
                elif e.response and e.response.status_code == 403:
                    # Permission denied - user blocked notifications
                    failed_endpoints.append(sub["endpoint"])
                    logger.info(f"Push blocked by user (403): {sub['endpoint'][:50]}...")
                else:
                    logger.warning(f"Push notification failed: {e}")
            except Exception as e:
                logger.warning(f"Push notification error: {e}")

        # Clean up expired subscriptions
        for endpoint in failed_endpoints:
            await self.unsubscribe(endpoint)

        if sent > 0:
            logger.info(f"Sent {sent} push notifications for chat {chat_id}")

        return sent

    async def notify_new_message(
        self, chat_id: int, chat_title: str, sender_name: str, message_text: str, message_id: int
    ) -> int:
        """
        Send notification for a new message.

        Args:
            chat_id: The chat ID where the message was posted
            chat_title: Display name of the chat
            sender_name: Name of the message sender
            message_text: Preview of the message text
            message_id: ID of the message (for click navigation)

        Returns:
            Number of notifications sent
        """
        # Truncate message preview
        preview = message_text[:100] + "..." if len(message_text) > 100 else message_text

        title = chat_title
        body = f"{sender_name}: {preview}" if sender_name else preview

        return await self.send_notification(
            title=title,
            body=body,
            chat_id=chat_id,
            data={
                "type": "new_message",
                "chat_id": chat_id,
                "message_id": message_id,
                "url": f"/?chat={chat_id}&msg={message_id}",
            },
            tag=f"chat-{chat_id}",  # Group by chat, replace previous
        )


# Singleton instance
_push_manager: PushNotificationManager | None = None


async def get_push_manager(db_adapter, config) -> PushNotificationManager:
    """Get or create the push notification manager singleton."""
    global _push_manager

    if _push_manager is None:
        _push_manager = PushNotificationManager(db_adapter, config)
        await _push_manager.initialize()

    return _push_manager
