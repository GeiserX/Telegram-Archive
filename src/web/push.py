"""
Web Push notifications for Telegram Archive viewer.

This module handles:
- VAPID key generation and management
- Push subscription storage and retrieval
- Sending push notifications to subscribed clients
"""

import asyncio
import ipaddress
import json
import logging
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

from ..db.adapter import parse_entitlement_column
from ..message_utils import utcnow_naive

logger = logging.getLogger(__name__)

_LOCAL_HOSTNAME_SUFFIXES = (".local", ".internal")

# Share-token sessions (and so their push rows) are stored under this prefix.
_TOKEN_USERNAME_PREFIX = "token:"

# webpush() is pywebpush's synchronous requests.post, and it always forwards its
# own timeout argument -- so leaving it out means requests.post(timeout=None),
# i.e. no timeout at all. One blackholed push endpoint would then hold a thread
# until the OS gives up on the TCP connection. Note requests applies this value
# per socket operation (the connect, then each read), not to the request as a
# whole: a fully silent endpoint is cut off after one interval, but an endpoint
# that keeps trickling bytes can hold its thread longer than this value.
_PUSH_TIMEOUT_SECONDS = 10
# Sends run on the loop's default thread executor, which thumbnail generation
# also uses. Cap the fan-out so a large subscriber list cannot occupy every
# worker thread for the length of a timeout.
_PUSH_CONCURRENCY = 8


def validate_push_endpoint(endpoint: str) -> bool:
    """Check that a push subscription endpoint is a public https URL.

    The endpoint is POSTed to by the server later (see send_notification), so an
    unvalidated value is a blind SSRF vector into internal networks. Real browser
    push services (FCM, Mozilla autopush, WNS, Apple) are always public https
    hosts, so rejecting anything else breaks no legitimate client.

    This resolves the hostname and inspects the actual IP address(es) rather
    than pattern-matching the literal string: alternate numeric encodings
    (decimal/hex/octal IPv4, "127.1" shorthand) and trailing-dot FQDNs all
    resolve, at the OS level, to the same address a browser would connect to,
    so checking the resolved address is the only way to catch all of them.
    This is a blocking call (DNS/resolver lookup) — callers on an event loop
    must run it off-thread (see push_subscribe's asyncio.to_thread usage).

    Residual gap, accepted as out of scope: this is a point-in-time check, so
    a TOCTOU/DNS-rebind attack (the name resolves differently at send time)
    isn't closed here. Closing that needs pinning the webpush connection to
    the address validated here, which is a larger change — tracked as a
    follow-up, not implemented now.
    """
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False

    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    hostname = hostname.lower().rstrip(".")
    if not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(_LOCAL_HOSTNAME_SUFFIXES):
        return False

    try:
        addr_infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        # Unresolvable — can't prove it's safe to contact, so reject.
        return False

    for *_rest, sockaddr in addr_infos:
        try:
            resolved_ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if (
            resolved_ip.is_private
            or resolved_ip.is_loopback
            or resolved_ip.is_link_local
            or resolved_ip.is_reserved
            or resolved_ip.is_multicast
            or resolved_ip.is_unspecified
        ):
            return False

    return True


class PushNotificationManager:
    """Manages Web Push notifications for the viewer."""

    def __init__(self, db_adapter, config, *, configured_principals: Iterable[str] = ()):
        self.db = db_adapter
        self.config = config
        # Usernames that exist by configuration rather than by a viewer_accounts
        # row (the env master, proxy admins, the anonymous viewer). The owner
        # liveness check below can never prove those dead, so it leaves them be.
        self.configured_principals = frozenset(configured_principals)
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
        allowed_accounts: list[int] | None = None,
        allowed_chat_refs: list[str] | None = None,
    ) -> bool:
        """
        Store a push subscription with user ownership.

        Args:
            endpoint: Push service URL
            p256dh: Client public key (base64)
            auth: Auth secret (base64)
            chat_id: Optional resolved chat ID for chat-specific subscriptions
                (the API takes a chat_ref; the caller resolves it before this)
            user_agent: Browser user agent for debugging
            username: The authenticated user who created this subscription
            allowed_accounts: Snapshot of the user's account grant (None = all)
            allowed_chat_refs: Snapshot of the user's chat-ref grant (None = all)

        The legacy ``allowed_chat_ids`` column is written as a rollback
        tombstone only — "[]" for a restricted subscriber so a 7.x binary
        denies, NULL for an unrestricted one. 8.0 code never reads it.

        Returns:
            True if subscription was stored successfully
        """
        try:
            from sqlalchemy import select

            from src.db.models import PushSubscription

            accounts_json = json.dumps(allowed_accounts) if allowed_accounts is not None else None
            refs_json = json.dumps(allowed_chat_refs) if allowed_chat_refs is not None else None
            restricted = accounts_json is not None or refs_json is not None
            legacy_tombstone = "[]" if restricted else None

            async with self.db.db_manager.async_session_factory() as session:
                result = await session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
                existing = result.scalar_one_or_none()

                if existing:
                    existing.p256dh = p256dh
                    existing.auth = auth
                    existing.chat_id = chat_id
                    existing.user_agent = user_agent
                    existing.username = username
                    existing.allowed_chat_ids = legacy_tombstone
                    existing.allowed_accounts = accounts_json
                    existing.allowed_chat_refs = refs_json
                    existing.last_used_at = utcnow_naive()
                else:
                    sub = PushSubscription(
                        endpoint=endpoint,
                        p256dh=p256dh,
                        auth=auth,
                        chat_id=chat_id,
                        user_agent=user_agent,
                        username=username,
                        allowed_chat_ids=legacy_tombstone,
                        allowed_accounts=accounts_json,
                        allowed_chat_refs=refs_json,
                        created_at=utcnow_naive(),
                    )
                    session.add(sub)

                await session.commit()
                logger.info(f"Push subscription stored for {username or 'anonymous'}: {endpoint[:50]}...")
                return True

        except Exception as e:
            logger.error(f"Failed to store push subscription: {e}")
            return False

    async def unsubscribe(self, endpoint: str, username: str | None = None) -> bool:
        """Remove a push subscription. Scoped to the requesting user to prevent cross-user unsubscribe."""
        try:
            from sqlalchemy import and_, delete

            from src.db.models import PushSubscription

            async with self.db.db_manager.async_session_factory() as session:
                conditions = [PushSubscription.endpoint == endpoint]
                if username:
                    conditions.append(PushSubscription.username == username)
                await session.execute(delete(PushSubscription).where(and_(*conditions)))
                await session.commit()
                logger.info(f"Push subscription removed for {username or 'anonymous'}: {endpoint[:50]}...")
                return True

        except Exception as e:
            logger.error(f"Failed to remove push subscription: {e}")
            return False

    async def get_subscriptions(
        self,
        chat_id: int | None = None,
        *,
        account_id: int | None = None,
        chat_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get push subscriptions entitled to a given chat, fail-closed.

        ``chat_id`` narrows to subscriptions whose TOPIC is this chat (or
        global); ``account_id``/``chat_ref`` are the chat's identity for the
        entitlement check against the v8.0.0 grant columns. The legacy
        ``allowed_chat_ids`` column is never read — a subscription whose grant
        exists only there (or cannot be parsed) receives nothing, exactly like
        the viewer's own resolver. Without a chat context only unrestricted
        subscriptions qualify.

        The grant is a SNAPSHOT taken at subscribe time, so it cannot notice
        that its owner was disabled, deleted or revoked since; owner liveness
        is therefore checked here as well, as the backstop for a revocation
        whose push purge never ran.
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

                entitled = []
                for sub in subs:
                    accounts = parse_entitlement_column(sub.allowed_accounts, int)
                    refs = parse_entitlement_column(sub.allowed_chat_refs, str)
                    # Deny-only legacy guard, the twin of main._grants_from_row:
                    # a legacy grant with BOTH new columns NULL is an unconverted
                    # 7.x restriction and must deny, never widen.
                    if accounts is None and refs is None and sub.allowed_chat_ids is not None:
                        continue
                    if accounts is not None and account_id not in accounts:
                        continue
                    if refs is not None and chat_ref not in refs:
                        continue
                    entitled.append(sub)

                live = await self._live_owners(session, {sub.username for sub in entitled if sub.username})

                return [
                    {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
                    for sub in entitled
                    if not sub.username or sub.username in live
                ]

        except Exception as e:
            logger.error(f"Failed to get push subscriptions: {e}")
            return []

    async def _live_owners(self, session, usernames: set[str]) -> set[str]:
        """Which of ``usernames`` still resolve to a principal allowed to receive.

        Two queries at most, never one per row: ``token:`` owners are checked
        against the share tokens, everyone else against the viewer accounts.
        A name is live when

        - it is a configured principal (see ``configured_principals``), or
        - it is an ACTIVE viewer account, or
        - it is the session username of a share token that is neither revoked
          nor expired.

        Everything else — a deleted account, a disabled one, a revoked or
        expired token — is dropped. A row with no owner recorded predates
        ownership and is left to the grant columns, which still gate it.

        Share-token labels are not unique, so two tokens sharing a label also
        share one session username, and revoking just one of them leaves that
        name live. The purge on the revoking path is the precise instrument;
        this is the backstop behind it.
        """
        if not usernames:
            return set()

        from sqlalchemy import or_, select

        from src.db.models import ViewerAccount, ViewerToken

        live = {name for name in usernames if name in self.configured_principals}
        remaining = usernames - live
        token_names = {name for name in remaining if name.startswith(_TOKEN_USERNAME_PREFIX)}
        account_names = remaining - token_names

        if account_names:
            result = await session.execute(
                select(ViewerAccount.username).where(
                    ViewerAccount.username.in_(account_names), ViewerAccount.is_active == 1
                )
            )
            live.update(result.scalars().all())

        if token_names:
            now = utcnow_naive()
            result = await session.execute(
                select(ViewerToken.id, ViewerToken.label).where(
                    ViewerToken.is_revoked == 0,
                    or_(ViewerToken.expires_at.is_(None), ViewerToken.expires_at > now),
                )
            )
            for token_id, label in result.all():
                # Mirrors the username auth_via_token mints in main.py:
                # f"token:{label or f'token:{id}'}". A label-less token really
                # does end up doubled, and reversing it must match exactly.
                minted = f"{_TOKEN_USERNAME_PREFIX}{label or f'{_TOKEN_USERNAME_PREFIX}{token_id}'}"
                if minted in token_names:
                    live.add(minted)

        return live

    async def send_notification(
        self,
        title: str,
        body: str,
        chat_id: int | None = None,
        data: dict[str, Any] | None = None,
        icon: str | None = None,
        tag: str | None = None,
        *,
        account_id: int | None = None,
        chat_ref: str | None = None,
    ) -> int:
        """
        Send push notification to all relevant subscribers.

        Args:
            title: Notification title
            body: Notification body text
            chat_id: Optional chat ID to filter subscribers (topic match only —
                it never reaches the payload; the ref is the outward identity)
            data: Additional data to include in notification
            icon: URL for notification icon
            tag: Tag for notification grouping/replacement
            account_id: The chat's account, for the entitlement filter
            chat_ref: The chat's opaque ref, for the entitlement filter and tag

        Returns:
            Number of notifications successfully sent
        """
        if not self.is_enabled:
            return 0

        subscriptions = await self.get_subscriptions(chat_id, account_id=account_id, chat_ref=chat_ref)

        if not subscriptions:
            return 0

        payload = {
            "title": title,
            "body": body,
            "icon": icon or "/static/favicon.ico",
            "tag": tag or f"telegram-archive-{chat_ref or 'all'}",
            "data": data or {},
            "timestamp": utcnow_naive().isoformat(),
        }

        semaphore = asyncio.Semaphore(_PUSH_CONCURRENCY)

        async def deliver(sub: dict[str, Any]) -> tuple[bool, str | None]:
            """Send one notification. Returns (sent, endpoint to prune)."""
            try:
                # Extract origin from endpoint for VAPID audience claim
                endpoint_url = urlparse(sub["endpoint"])
                audience = f"{endpoint_url.scheme}://{endpoint_url.netloc}"

                # Generate VAPID headers using py_vapid
                vapid_headers = self._vapid.sign({"sub": self.config.vapid_contact, "aud": audience})

                async with semaphore:
                    # webpush() blocks: run it off the event loop and bound it,
                    # or one slow push service stalls the whole viewer.
                    await asyncio.to_thread(
                        webpush,
                        subscription_info=sub,
                        data=json.dumps(payload),
                        headers=vapid_headers,
                        timeout=_PUSH_TIMEOUT_SECONDS,
                    )
                return True, None
            except WebPushException as e:
                if e.response and e.response.status_code in (404, 410):
                    # Subscription expired or unsubscribed
                    logger.debug(f"Push subscription expired: {sub['endpoint'][:50]}...")
                    return False, sub["endpoint"]
                if e.response and e.response.status_code == 403:
                    # Permission denied - user blocked notifications
                    logger.info(f"Push blocked by user (403): {sub['endpoint'][:50]}...")
                    return False, sub["endpoint"]
                logger.warning(f"Push notification failed: {e}")
                return False, None
            except Exception as e:
                logger.warning(f"Push notification error: {e}")
                return False, None

        results = await asyncio.gather(*(deliver(sub) for sub in subscriptions))

        sent = sum(1 for delivered, _ in results if delivered)
        failed_endpoints = [endpoint for _, endpoint in results if endpoint]

        # Clean up expired subscriptions
        for endpoint in failed_endpoints:
            await self.unsubscribe(endpoint)

        if sent > 0:
            logger.info(f"Sent {sent} push notifications")

        return sent

    async def notify_new_message(
        self,
        chat_id: int,
        chat_ref: str,
        chat_title: str,
        sender_name: str,
        message_text: str,
        message_id: int,
        *,
        account_id: int | None = None,
    ) -> int:
        """
        Send notification for a new message.

        Args:
            chat_id: The chat ID where the message was posted (topic filter only)
            chat_ref: The chat's opaque ref — the ONLY chat identity that enters
                the payload, its URL, and the grouping tag
            chat_title: Display name of the chat
            sender_name: Name of the message sender
            message_text: Preview of the message text
            message_id: ID of the message (for click navigation)
            account_id: The chat's account, for the entitlement filter

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
                "chat_ref": chat_ref,
                "message_id": message_id,
                "url": f"/?chat={chat_ref}&msg={message_id}",
            },
            tag=f"chat-{chat_ref}",  # Group by chat, replace previous
            account_id=account_id,
            chat_ref=chat_ref,
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
