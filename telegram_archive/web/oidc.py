"""OpenID Connect single sign-on for the viewer (authorization code flow with PKCE).

Pure helpers: configuration, discovery, the signed login-state cookie, the code
exchange and ID-token claim checks. The routes and the mapping of an identity to
a viewer account live in ``main.py``.

The ID token is read straight from the token endpoint over TLS, so its signature
is not verified (OpenID Connect Core 3.1.3.7 step 6 allows this); every claim
that binds it to this login (issuer, audience, expiry, nonce) is.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlencode

STATE_COOKIE_NAME = "oidc_state"
STATE_TTL_SECONDS = 600
_DISCOVERY_TTL_SECONDS = 3600
_HTTP_TIMEOUT_SECONDS = 10.0
_CLOCK_SKEW_SECONDS = 60
_MAX_IDENTITY_LENGTH = 254


class OIDCError(Exception):
    """A login that cannot be completed; the message is safe to show the user."""


def _csv(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _pairs(value: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in value.split(","):
        identity, sep, username = item.partition("=")
        if sep and identity.strip() and username.strip():
            mapping[identity.strip().lower()] = username.strip()
    return mapping


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = "openid email profile"
    username_claim: str = "email"
    admin_users: frozenset[str] = field(default_factory=frozenset)
    user_map: dict[str, str] = field(default_factory=dict)
    default_access: str = "none"

    @classmethod
    def from_env(cls) -> OIDCConfig | None:
        """The configuration, or None unless issuer, client id/secret and redirect URI are all set."""
        issuer = os.getenv("OIDC_ISSUER", "").strip().rstrip("/")
        client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
        client_secret = os.getenv("OIDC_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("OIDC_REDIRECT_URI", "").strip()
        if not (issuer and client_id and client_secret and redirect_uri):
            return None
        return cls(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=os.getenv("OIDC_SCOPES", "openid email profile").strip() or "openid email profile",
            username_claim=os.getenv("OIDC_USERNAME_CLAIM", "email").strip() or "email",
            admin_users=frozenset(_csv(os.getenv("OIDC_ADMIN_USERS", ""))),
            user_map=_pairs(os.getenv("OIDC_USER_MAP", "")),
            default_access=os.getenv("OIDC_DEFAULT_ACCESS", "none").strip().lower(),
        )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_login_state() -> dict[str, str]:
    """Fresh state, nonce and PKCE verifier/challenge for one login attempt."""
    verifier = secrets.token_urlsafe(48)
    return {
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": verifier,
        "challenge": _b64e(hashlib.sha256(verifier.encode()).digest()),
    }


def sign_state(secret: str, state: str, nonce: str, verifier: str, now: float | None = None) -> str:
    """Cookie value carrying the login state, HMAC-signed so no server-side store is needed."""
    body = _b64e(
        json.dumps({"s": state, "n": nonce, "v": verifier, "e": int((now or time.time()) + STATE_TTL_SECONDS)}).encode()
    )
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(mac)}"


def verify_state(secret: str, cookie: str | None, now: float | None = None) -> dict[str, str] | None:
    """The state/nonce/verifier of a genuine, unexpired cookie, else None."""
    if not cookie or cookie.count(".") != 1:
        return None
    body, _, mac = cookie.partition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_b64d(mac), expected):
            return None
        data = json.loads(_b64d(body))
        if int(data["e"]) < (now or time.time()):
            return None
        return {"state": str(data["s"]), "nonce": str(data["n"]), "verifier": str(data["v"])}
    except ValueError, KeyError, TypeError:
        return None


_discovery_cache: dict[str, tuple[float, dict]] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Provider endpoints answer directly; a redirect could downgrade to http or point inside the network."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _require_https(url: object) -> str:
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise OIDCError("Single sign-on endpoints must use https")
    return url


def _http_json(request: urllib.request.Request) -> dict:
    """One blocking HTTPS round trip returning a JSON object; the viewer image carries no HTTP client library."""
    _require_https(request.full_url)
    try:
        with _opener.open(request, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 - scheme checked above
            body = json.loads(resp.read(1_000_000))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OIDCError("Single sign-on provider request failed") from e
    if not isinstance(body, dict):
        raise OIDCError("Single sign-on provider request failed")
    return body


async def discover(cfg: OIDCConfig) -> dict:
    """The provider's discovery document, cached for an hour."""
    cached = _discovery_cache.get(cfg.issuer)
    if cached and time.time() - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    meta = await asyncio.to_thread(_http_json, urllib.request.Request(f"{cfg.issuer}/.well-known/openid-configuration"))
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise OIDCError("Single sign-on provider returned an incomplete configuration")
    _require_https(meta["authorization_endpoint"])
    _require_https(meta["token_endpoint"])
    if str(meta.get("issuer", "")).rstrip("/") != cfg.issuer.rstrip("/"):
        raise OIDCError("Single sign-on provider issuer does not match OIDC_ISSUER")
    _discovery_cache[cfg.issuer] = (time.time(), meta)
    return meta


def authorize_url(meta: dict, cfg: OIDCConfig, login: dict[str, str]) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": cfg.scopes,
            "state": login["state"],
            "nonce": login["nonce"],
            "code_challenge": login["challenge"],
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return f"{meta['authorization_endpoint']}{sep}{query}"


async def exchange_code(meta: dict, cfg: OIDCConfig, code: str, verifier: str) -> str:
    """Trade the authorization code for the ID token."""
    form = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code_verifier": verifier,
        }
    ).encode()
    request = urllib.request.Request(
        meta["token_endpoint"],
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    id_token = (await asyncio.to_thread(_http_json, request)).get("id_token")
    if not isinstance(id_token, str):
        raise OIDCError("Single sign-on provider returned no ID token")
    return id_token


def validate_id_token(
    id_token: str, issuer: str, cfg: OIDCConfig, nonce: str, now: float | None = None
) -> dict[str, object]:
    """The ID token's claims once issuer, audience, expiry and nonce check out."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OIDCError("Malformed ID token")
    try:
        claims = json.loads(_b64d(parts[1]))
    except ValueError as e:
        raise OIDCError("Malformed ID token") from e
    if not isinstance(claims, dict):
        raise OIDCError("Malformed ID token")

    if str(claims.get("iss", "")).rstrip("/") != issuer.rstrip("/"):
        raise OIDCError("ID token issuer mismatch")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if cfg.client_id not in audiences or (len(audiences) > 1 and claims.get("azp") != cfg.client_id):
        raise OIDCError("ID token audience mismatch")
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError) as e:
        raise OIDCError("ID token has no valid expiry") from e
    if not math.isfinite(exp):
        raise OIDCError("ID token has no valid expiry")
    if exp < (now or time.time()) - _CLOCK_SKEW_SECONDS:
        raise OIDCError("ID token expired")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise OIDCError("ID token nonce mismatch")
    return claims


def identity_from_claims(claims: dict[str, object], cfg: OIDCConfig) -> str:
    """The lowercased username claim, or OIDCError when the provider did not send a usable one."""
    if cfg.username_claim == "email" and str(claims.get("email_verified", True)).strip().lower() in ("false", "0"):
        raise OIDCError("The provider has not verified this email address")
    value = claims.get(cfg.username_claim)
    if not isinstance(value, str) or not value.strip():
        raise OIDCError(f"ID token has no '{cfg.username_claim}' claim")
    identity = value.strip().lower()
    if len(identity) > _MAX_IDENTITY_LENGTH or not identity.isprintable():
        raise OIDCError(f"ID token '{cfg.username_claim}' claim is not a usable username")
    return identity
