"""
Web Push notifications for Telegram Archive viewer.

This module handles:
- VAPID key generation and management
- Push subscription storage and retrieval
- Sending push notifications to subscribed clients
"""

import json
import logging
import secrets
from datetime import datetime
from typing import Optional, List, Dict, Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import webpush, WebPushException

logger = logging.getLogger(__name__)


class PushNotificationManager:
    """Manages Web Push notifications for the viewer."""
    
    def __init__(self, db_adapter, config):
        self.db = db_adapter
        self.config = config
        self._vapid: Optional[Vapid] = None
        self._public_key: Optional[str] = None
        self._private_key: Optional[str] = None
    
    async def initialize(self) -> bool:
        """
        Initialize push notifications.
        
        Loads or generates VAPID keys and stores them persistently.
        Returns True if push notifications are enabled and ready.
        """
        if self.config.push_notifications == 'off':
            logger.info("Push notifications disabled (PUSH_NOTIFICATIONS=off)")
            return False
        
        if self.config.push_notifications == 'basic':
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
            stored_private = await self.db.get_metadata('vapid_private_key')
            stored_public = await self.db.get_metadata('vapid_public_key')
            
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
                self._private_key = private_pem.decode('utf-8') if isinstance(private_pem, bytes) else private_pem
                
                # Get public key as URL-safe base64
                public_bytes = vapid.public_key.public_bytes(
                    serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint
                )
                self._public_key = b64urlencode(public_bytes)
                
                # Store in database for persistence across restarts
                await self.db.set_metadata('vapid_private_key', self._private_key)
                await self.db.set_metadata('vapid_public_key', self._public_key)
                logger.info("Generated and stored new VAPID keys")
        
        # Create VAPID instance from the stored private key
        try:
            # Try PEM format first (our default storage format)
            if '-----BEGIN' in self._private_key:
                self._vapid = Vapid.from_pem(self._private_key.encode('utf-8'))
            else:
                # Try DER/raw format
                self._vapid = Vapid.from_string(self._private_key)
            logger.info(f"Web Push initialized. Public key: {self._public_key[:20]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize VAPID: {e}")
            return False
    
    @property
    def public_key(self) -> Optional[str]:
        """Get the VAPID public key for client subscription."""
        return self._public_key
    
    @property
    def is_enabled(self) -> bool:
        """Check if full push notifications are enabled."""
        return self._vapid is not None and self.config.push_notifications == 'full'
    
    async def subscribe(
        self,
        endpoint: str,
        p256dh: str,
        auth: str,
        chat_id: Optional[int] = None,
        user_agent: Optional[str] = None
    ) -> bool:
        """
        Store a push subscription.
        
        Args:
            endpoint: Push service URL
            p256dh: Client public key (base64)
            auth: Auth secret (base64)
            chat_id: Optional chat ID for chat-specific subscriptions
            user_agent: Browser user agent for debugging
            
        Returns:
            True if subscription was stored successfully
        """
        try:
            from sqlalchemy import select, delete
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            from src.db.models import PushSubscription
            
            async with self.db._session_factory() as session:
                # Check if subscription already exists
                result = await session.execute(
                    select(PushSubscription).where(PushSubscription.endpoint == endpoint)
                )
                existing = result.scalar_one_or_none()
                
                if existing:
                    # Update existing subscription
                    existing.p256dh = p256dh
                    existing.auth = auth
                    existing.chat_id = chat_id
                    existing.user_agent = user_agent
                    existing.last_used_at = datetime.utcnow()
                else:
                    # Create new subscription
                    sub = PushSubscription(
                        endpoint=endpoint,
                        p256dh=p256dh,
                        auth=auth,
                        chat_id=chat_id,
                        user_agent=user_agent,
                        created_at=datetime.utcnow()
                    )
                    session.add(sub)
                
                await session.commit()
                logger.info(f"Push subscription stored: {endpoint[:50]}...")
                return True
                
        except Exception as e:
            logger.error(f"Failed to store push subscription: {e}")
            return False
    
    async def unsubscribe(self, endpoint: str) -> bool:
        """Remove a push subscription."""
        try:
            from sqlalchemy import delete
            from src.db.models import PushSubscription
            
            async with self.db._session_factory() as session:
                await session.execute(
                    delete(PushSubscription).where(PushSubscription.endpoint == endpoint)
                )
                await session.commit()
                logger.info(f"Push subscription removed: {endpoint[:50]}...")
                return True
                
        except Exception as e:
            logger.error(f"Failed to remove push subscription: {e}")
            return False
    
    async def get_subscriptions(self, chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get all push subscriptions, optionally filtered by chat_id.
        
        Returns subscriptions for:
        - Global subscribers (chat_id is NULL)
        - Chat-specific subscribers (if chat_id provided)
        """
        try:
            from sqlalchemy import select, or_
            from src.db.models import PushSubscription
            
            async with self.db._session_factory() as session:
                query = select(PushSubscription)
                
                if chat_id is not None:
                    # Get global subscriptions OR chat-specific ones
                    query = query.where(
                        or_(
                            PushSubscription.chat_id.is_(None),
                            PushSubscription.chat_id == chat_id
                        )
                    )
                
                result = await session.execute(query)
                subs = result.scalars().all()
                
                return [
                    {
                        'endpoint': sub.endpoint,
                        'keys': {
                            'p256dh': sub.p256dh,
                            'auth': sub.auth
                        }
                    }
                    for sub in subs
                ]
                
        except Exception as e:
            logger.error(f"Failed to get push subscriptions: {e}")
            return []
    
    async def send_notification(
        self,
        title: str,
        body: str,
        chat_id: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
        icon: Optional[str] = None,
        tag: Optional[str] = None
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
            'title': title,
            'body': body,
            'icon': icon or '/static/favicon.ico',
            'tag': tag or f'telegram-archive-{chat_id or "all"}',
            'data': data or {},
            'timestamp': datetime.utcnow().isoformat()
        }
        
        sent = 0
        failed_endpoints = []
        
        for sub in subscriptions:
            try:
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=self._private_key,
                    vapid_claims={
                        'sub': self.config.vapid_contact
                    }
                )
                sent += 1
            except WebPushException as e:
                if e.response and e.response.status_code in (404, 410):
                    # Subscription expired or unsubscribed
                    failed_endpoints.append(sub['endpoint'])
                    logger.debug(f"Push subscription expired: {sub['endpoint'][:50]}...")
                elif e.response and e.response.status_code == 403:
                    # Permission denied - user blocked notifications
                    failed_endpoints.append(sub['endpoint'])
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
        self,
        chat_id: int,
        chat_title: str,
        sender_name: str,
        message_text: str,
        message_id: int
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
        preview = message_text[:100] + '...' if len(message_text) > 100 else message_text
        
        title = chat_title
        body = f"{sender_name}: {preview}" if sender_name else preview
        
        return await self.send_notification(
            title=title,
            body=body,
            chat_id=chat_id,
            data={
                'type': 'new_message',
                'chat_id': chat_id,
                'message_id': message_id,
                'url': f'/?chat={chat_id}&msg={message_id}'
            },
            tag=f'chat-{chat_id}'  # Group by chat, replace previous
        )


# Singleton instance
_push_manager: Optional[PushNotificationManager] = None


async def get_push_manager(db_adapter, config) -> PushNotificationManager:
    """Get or create the push notification manager singleton."""
    global _push_manager
    
    if _push_manager is None:
        _push_manager = PushNotificationManager(db_adapter, config)
        await _push_manager.initialize()
    
    return _push_manager
