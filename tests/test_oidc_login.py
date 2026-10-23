"""OpenID Connect single sign-on: state cookie, ID-token checks and the login routes."""

import base64
import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from telegram_archive.web import oidc

CFG = oidc.OIDCConfig(
    issuer="https://idp.example/oidc/abc",
    client_id="client-1",
    client_secret="s3cret",
    redirect_uri="https://viewer.example/api/oidc/callback",
    admin_users=frozenset({"admin@example.com"}),
    user_map={"boss@example.com": "hyper", "jo@example.com": "jordan"},
)


def _jwt(claims: dict) -> str:
    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{enc(b'{}')}.{enc(json.dumps(claims).encode())}.sig"


def _claims(**over):
    base = {
        "iss": CFG.issuer,
        "aud": CFG.client_id,
        "exp": time.time() + 300,
        "nonce": "n1",
        "email": "Admin@Example.com",
    }
    base.update(over)
    return base


def test_state_round_trip_and_tamper():
    cookie = oidc.sign_state("k", "st", "no", "ve")
    assert oidc.verify_state("k", cookie) == {"state": "st", "nonce": "no", "verifier": "ve"}
    assert oidc.verify_state("other", cookie) is None
    assert oidc.verify_state("k", cookie[:-2] + "xx") is None
    assert oidc.verify_state("k", None) is None
    assert oidc.verify_state("k", cookie, now=time.time() + oidc.STATE_TTL_SECONDS + 5) is None


def test_pkce_challenge_matches_verifier():
    import hashlib

    login = oidc.new_login_state()
    digest = hashlib.sha256(login["verifier"].encode()).digest()
    assert login["challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "over",
    [
        {"iss": "https://evil.example"},
        {"aud": "someone-else"},
        {"aud": ["client-1", "other"]},
        {"exp": time.time() - 3600},
        {"nonce": "wrong"},
    ],
)
def test_id_token_rejections(over):
    with pytest.raises(oidc.OIDCError):
        oidc.validate_id_token(_jwt(_claims(**over)), CFG.issuer, CFG, "n1")


def test_id_token_missing_nonce_rejected():
    claims = _claims()
    del claims["nonce"]
    with pytest.raises(oidc.OIDCError):
        oidc.validate_id_token(_jwt(claims), CFG.issuer, CFG, "n1")


def test_identity_lowercased_and_unverified_email_rejected():
    claims = oidc.validate_id_token(_jwt(_claims()), CFG.issuer, CFG, "n1")
    assert oidc.identity_from_claims(claims, CFG) == "admin@example.com"
    with pytest.raises(oidc.OIDCError):
        oidc.identity_from_claims({"email": "a@b.c", "email_verified": False}, CFG)
    with pytest.raises(oidc.OIDCError):
        oidc.identity_from_claims({}, CFG)


def test_config_from_env_needs_all_four(monkeypatch):
    for k in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("OIDC_USER_MAP", "A@B.c=bob, bad ,=x")
    monkeypatch.setenv("OIDC_ADMIN_USERS", "Root@B.c")
    cfg = oidc.OIDCConfig.from_env()
    assert cfg.user_map == {"a@b.c": "bob"}
    assert cfg.admin_users == {"root@b.c"}
    monkeypatch.delenv("OIDC_CLIENT_SECRET")
    assert oidc.OIDCConfig.from_env() is None


@pytest.fixture
def client():
    import telegram_archive.web.main as m

    db = AsyncMock()
    db.get_viewer_by_username = AsyncMock(return_value=None)
    db.create_viewer_account = AsyncMock()
    db.create_audit_log = AsyncMock()
    db.save_session = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    meta = {
        "issuer": CFG.issuer,
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
    }
    with (
        patch.dict(os.environ, {"VIEWER_USERNAME": "hyper", "VIEWER_PASSWORD": "pw"}),
        patch.object(m, "VIEWER_USERNAME", "hyper"),
        patch.object(m, "VIEWER_PASSWORD", "pw"),
        patch.object(m, "AUTH_ENABLED", True),
        patch.object(m, "OIDC_CONFIG", CFG),
        patch.object(m, "db", db),
        patch.object(oidc, "discover", AsyncMock(return_value=meta)),
    ):
        m._sessions.clear()
        m._login_attempts.clear()
        yield TestClient(m.app, follow_redirects=False), m, db
        m._sessions.clear()


def _begin(tc):
    resp = tc.get("/api/oidc/login")
    assert resp.status_code == 302
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [CFG.redirect_uri]
    return query["state"][0], query["nonce"][0]


def _finish(tc, state, nonce, email, **claim_over):
    token = _jwt(_claims(nonce=nonce, email=email, **claim_over))
    with patch.object(oidc, "exchange_code", AsyncMock(return_value=token)):
        return tc.get("/api/oidc/callback", params={"code": "c", "state": state})


def test_auth_check_advertises_sso(client):
    tc, _, _ = client
    assert tc.get("/api/auth/check").json()["oidc_enabled"] is True


def test_admin_gets_master_session(client):
    tc, m, _ = client
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "admin@example.com")
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    assert [s.role for s in m._sessions.values()] == ["master"]
    assert tc.get("/api/auth/check").json()["authenticated"] is True


def test_mapped_identity_becomes_master_account(client):
    tc, m, _ = client
    state, nonce = _begin(tc)
    _finish(tc, state, nonce, "boss@example.com")
    (session,) = m._sessions.values()
    assert (session.username, session.role) == ("hyper", "master")


def test_unknown_identity_is_auto_created_with_no_access(client):
    tc, m, db = client
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "new@example.com")
    assert resp.status_code == 302
    (session,) = m._sessions.values()
    assert session.role == "viewer" and session.allowed_chat_refs == set()
    assert db.create_viewer_account.await_args.kwargs["created_by"] == "oidc"


def test_unmapped_identity_spelling_master_name_refused():
    import asyncio

    import telegram_archive.web.main as m

    cfg = oidc.OIDCConfig(CFG.issuer, "c", "s", "https://x/cb", username_claim="preferred_username")
    with patch.object(m, "VIEWER_USERNAME", "hyper"), pytest.raises(m.HTTPException) as exc:
        asyncio.run(m._resolve_oidc_user(cfg, "hyper"))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("tamper", ["state", "cookie", "nonce"])
def test_callback_rejects_forged_login(client, tamper):
    tc, m, _ = client
    state, nonce = _begin(tc)
    if tamper == "state":
        state = "forged"
    elif tamper == "cookie":
        tc.cookies.clear()
    else:
        nonce = "forged"
    resp = _finish(tc, state, nonce, "admin@example.com")
    assert resp.status_code in (400, 401)
    assert not m._sessions


def test_disabled_without_config(client):
    tc, m, _ = client
    with patch.object(m, "OIDC_CONFIG", None):
        assert tc.get("/api/oidc/login").status_code == 404
        assert tc.get("/api/auth/check").json().get("oidc_enabled") is False


@pytest.mark.parametrize("value", [False, "false", "False", "0"])
def test_unverified_email_in_any_spelling_rejected(value):
    with pytest.raises(oidc.OIDCError):
        oidc.identity_from_claims({"email": "a@b.c", "email_verified": value}, CFG)
    assert oidc.identity_from_claims({"email": "a@b.c", "email_verified": "true"}, CFG) == "a@b.c"


@pytest.mark.parametrize("exp", ["nan", "inf", None, "soon"])
def test_non_finite_expiry_rejected(exp):
    with pytest.raises(oidc.OIDCError):
        oidc.validate_id_token(_jwt(_claims(exp=exp)), CFG.issuer, CFG, "n1")


def test_oversized_or_control_identity_rejected():
    for bad in ("a" * 300 + "@x.c", "a\nb@x.c"):
        with pytest.raises(oidc.OIDCError):
            oidc.identity_from_claims({"email": bad}, CFG)


def test_redirects_from_provider_are_refused():
    assert oidc._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://internal/") is None
    with pytest.raises(oidc.OIDCError):
        oidc._require_https("http://idp.example/token")


def test_discovery_rejects_issuer_mismatch_and_plain_http():
    import asyncio

    def run(meta):
        oidc._discovery_cache.clear()
        with patch.object(oidc, "_http_json", return_value=meta):
            return asyncio.run(oidc.discover(CFG))

    good = {"issuer": CFG.issuer, "authorization_endpoint": "https://i/a", "token_endpoint": "https://i/t"}
    assert run(good)["token_endpoint"] == "https://i/t"
    for bad in (
        {**good, "issuer": "https://evil.example"},
        {**good, "authorization_endpoint": "http://i/a"},
        {**good, "token_endpoint": "http://i/t"},
    ):
        with pytest.raises(oidc.OIDCError):
            run(bad)
    oidc._discovery_cache.clear()


def test_unmapped_identity_cannot_adopt_a_password_account(client):
    tc, m, db = client
    db.get_viewer_by_username = AsyncMock(
        return_value={"username": "new@example.com", "is_active": 1, "created_by": "hyper", "allowed_chat_refs": None}
    )
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "new@example.com")
    assert resp.status_code == 403 and not m._sessions


def test_mapped_identity_may_use_an_existing_password_account(client):
    tc, m, db = client
    db.get_viewer_by_username = AsyncMock(
        return_value={"username": "jordan", "is_active": 1, "created_by": "hyper", "allowed_chat_refs": '["a"]'}
    )
    state, nonce = _begin(tc)
    assert _finish(tc, state, nonce, "jo@example.com").status_code == 302
    (session,) = m._sessions.values()
    assert (session.username, session.role) == ("jordan", "viewer")


def test_oidc_created_account_is_reusable(client):
    tc, m, db = client
    db.get_viewer_by_username = AsyncMock(
        return_value={"username": "new@example.com", "is_active": 1, "created_by": "oidc", "allowed_chat_refs": "[]"}
    )
    state, nonce = _begin(tc)
    assert _finish(tc, state, nonce, "new@example.com").status_code == 302


def test_master_name_guard_ignores_case():
    import asyncio

    import telegram_archive.web.main as m

    cfg = oidc.OIDCConfig(CFG.issuer, "c", "s", "https://x/cb", username_claim="preferred_username")
    with patch.object(m, "VIEWER_USERNAME", "Admin"), pytest.raises(m.HTTPException) as exc:
        asyncio.run(m._resolve_oidc_user(cfg, "admin"))
    assert exc.value.status_code == 403


def test_oidc_admins_are_configured_principals(client):
    _, m, _ = client
    assert "admin@example.com" in m._configured_principals()


def test_failed_callback_is_audited(client):
    tc, _, db = client
    resp = tc.get("/api/oidc/callback", params={"code": "c", "state": "forged"})
    assert resp.status_code == 400
    assert db.create_audit_log.await_args.kwargs["action"] == "login_failed"
