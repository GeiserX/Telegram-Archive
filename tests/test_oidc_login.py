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
    assert resp.status_code == 302
    assert resp.headers["location"] in ("/?sso_error=expired", "/?sso_error=invalid_token")
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


@pytest.mark.parametrize("exp", ["nan", "inf", None, "soon", 10**400])
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
        oidc._discovery_failed_at.clear()
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
    oidc._discovery_failed_at.clear()


def test_unmapped_identity_cannot_adopt_a_password_account(client):
    tc, m, db = client
    db.get_viewer_by_username = AsyncMock(
        return_value={"username": "new@example.com", "is_active": 1, "created_by": "hyper", "allowed_chat_refs": None}
    )
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "new@example.com")
    assert resp.headers["location"] == "/?sso_error=refused" and not m._sessions


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
    assert resp.status_code == 302 and resp.headers["location"] == "/?sso_error=expired"
    assert db.create_audit_log.await_args.kwargs["action"] == "login_failed"


def test_mapped_master_session_uses_the_configured_spelling():
    import asyncio

    import telegram_archive.web.main as m

    cfg = oidc.OIDCConfig(CFG.issuer, "c", "s", "https://x/cb", user_map={"boss@x.c": "Hyper"})
    with patch.object(m, "VIEWER_USERNAME", "hyper"):
        user = asyncio.run(m._resolve_oidc_user(cfg, "boss@x.c"))
    assert (user.username, user.role) == ("hyper", "master")


# --- review follow-ups -------------------------------------------------------


def _resolve(cfg, identity, row=None, **patches):
    import asyncio

    import telegram_archive.web.main as m

    db = AsyncMock()
    db.get_viewer_by_username = AsyncMock(return_value=row)
    db.create_viewer_account = AsyncMock()
    with (
        patch.object(m, "VIEWER_USERNAME", patches.get("viewer_username", "hyper")),
        patch.object(m, "AUTH_PROXY_ADMIN_USERS", patches.get("proxy_admins", set())),
        patch.object(m, "db", db),
    ):
        return asyncio.run(m._resolve_oidc_user(cfg, identity)), db


PREF = oidc.OIDCConfig(
    CFG.issuer, "c", "s", "https://x/cb", username_claim="preferred_username", user_map={"jo@example.com": "jordan"}
)


def test_missing_email_verified_is_accepted_on_purpose():
    # Cloudflare Access never sends email_verified; only an explicit false refuses.
    assert oidc.identity_from_claims({"email": "a@b.c"}, CFG) == "a@b.c"


def test_map_target_without_row_is_created_for_the_mapped_person():
    user, db = _resolve(PREF, "jo@example.com")
    assert (user.username, user.role) == ("jordan", "viewer")
    assert db.create_viewer_account.await_args.kwargs["username"] == "jordan"
    assert db.create_viewer_account.await_args.kwargs["created_by"] == "oidc"


@pytest.mark.parametrize("row", [None, {"username": "jordan", "created_by": "oidc", "is_active": 1}])
def test_unmapped_identity_spelling_a_map_target_is_refused(row):
    import telegram_archive.web.main as m

    with pytest.raises(m.HTTPException) as exc:
        _resolve(PREF, "jordan", row=row)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("identity,admins", [("anonymous", set()), ("ops", {"Ops"})])
def test_reserved_names_refused(identity, admins):
    import telegram_archive.web.main as m

    with pytest.raises(m.HTTPException) as exc:
        _resolve(PREF, identity, proxy_admins=admins)
    assert exc.value.status_code == 403


def test_password_account_stamped_oidc_is_not_adoptable():
    # A master literally named "oidc" stamps its password accounts created_by="oidc".
    import telegram_archive.web.main as m

    row = {"username": "carol", "created_by": "oidc", "password_hash": "h", "is_active": 1}
    with pytest.raises(m.HTTPException) as exc:
        _resolve(PREF, "carol", row=row, viewer_username="oidc")
    assert exc.value.status_code == 403
    user, _ = _resolve(PREF, "carol", row={**row, "password_hash": ""}, viewer_username="oidc")
    assert user.username == "carol"


def test_disabled_account_redirects_with_code(client):
    tc, m, db = client
    db.get_viewer_by_username = AsyncMock(
        return_value={"username": "jordan", "is_active": 0, "created_by": "hyper", "allowed_chat_refs": None}
    )
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "jo@example.com")
    assert resp.headers["location"] == "/?sso_error=disabled" and not m._sessions


def test_non_ascii_state_is_refused_and_audited(client):
    tc, m, db = client
    _begin(tc)
    resp = tc.get("/api/oidc/callback", params={"code": "c", "state": "é"})
    assert resp.status_code == 302 and resp.headers["location"] == "/?sso_error=expired"
    assert m._login_attempts and db.create_audit_log.await_args.kwargs["action"] == "login_failed"


def test_non_ascii_nonce_is_refused(client):
    tc, m, _ = client
    state, _ = _begin(tc)
    resp = _finish(tc, state, "é", "admin@example.com")
    assert resp.headers["location"] == "/?sso_error=invalid_token" and not m._sessions


def test_provider_error_is_refused_and_audited(client):
    tc, m, db = client
    resp = tc.get("/api/oidc/callback", params={"error": "access_denied"})
    assert resp.headers["location"] == "/?sso_error=cancelled"
    assert db.create_audit_log.await_args.kwargs["action"] == "login_failed"


def test_redirect_never_carries_provider_text(client):
    tc, _, _ = client
    resp = tc.get("/api/oidc/callback", params={"error": "<script>"})
    assert resp.headers["location"] == "/?sso_error=cancelled"


def test_viewer_only_header_blocks_master_but_not_viewers(client):
    tc, m, _ = client
    state, nonce = _begin(tc)
    token = _jwt(_claims(nonce=nonce, email="admin@example.com"))
    with patch.object(oidc, "exchange_code", AsyncMock(return_value=token)):
        resp = tc.get("/api/oidc/callback", params={"code": "c", "state": state}, headers={"X-Viewer-Only": "true"})
    assert resp.headers["location"] == "/?sso_error=refused" and not m._sessions

    state, nonce = _begin(tc)
    token = _jwt(_claims(nonce=nonce, email="new@example.com"))
    with patch.object(oidc, "exchange_code", AsyncMock(return_value=token)):
        resp = tc.get("/api/oidc/callback", params={"code": "c", "state": state}, headers={"X-Viewer-Only": "true"})
    assert resp.headers["location"] == "/"
    assert [s.role for s in m._sessions.values()] == ["viewer"]


def test_refused_callbacks_hit_the_rate_limit(client):
    tc, m, _ = client
    with patch.object(m, "_LOGIN_RATE_LIMIT", 2):
        for _ in range(2):
            assert tc.get("/api/oidc/callback", params={"state": "x"}).headers["location"] == "/?sso_error=expired"
        assert tc.get("/api/oidc/callback", params={"state": "x"}).headers["location"] == "/?sso_error=rate_limited"
        assert tc.get("/api/oidc/login").headers["location"] == "/?sso_error=rate_limited"


def test_login_counts_only_failed_discovery(client):
    tc, m, _ = client
    with patch.object(m, "_LOGIN_RATE_LIMIT", 3):
        for _ in range(5):
            assert tc.get("/api/oidc/login").status_code == 302
        assert not any(m._login_attempts.values())
        with patch.object(oidc, "discover", AsyncMock(side_effect=oidc.OIDCError("down"))):
            codes = [tc.get("/api/oidc/login").headers["location"] for _ in range(4)]
    assert codes[:3] == ["/?sso_error=provider_unavailable"] * 3
    assert codes[3] == "/?sso_error=rate_limited"


def test_state_cookie_attributes_and_deletion(client):
    tc, _, _ = client
    resp = tc.get("/api/oidc/login")
    cookie = resp.headers["set-cookie"].lower()
    for part in ("httponly", "path=/api/oidc", "samesite=lax", "max-age=600"):
        assert part in cookie
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(resp.headers["location"]).query)
    done = _finish(tc, q["state"][0], q["nonce"][0], "admin@example.com")
    cookies = [c for c in done.headers.get_list("set-cookie") if c.startswith(oidc.STATE_COOKIE_NAME)]
    assert cookies and "max-age=0" in cookies[0].lower()


def test_callback_forwards_the_pkce_verifier(client):
    import hashlib

    tc, _, _ = client
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(tc.get("/api/oidc/login").headers["location"]).query)
    exchange = AsyncMock(return_value=_jwt(_claims(nonce=q["nonce"][0], email="admin@example.com")))
    with patch.object(oidc, "exchange_code", exchange):
        assert tc.get("/api/oidc/callback", params={"code": "c", "state": q["state"][0]}).status_code == 302
    meta, cfg, code, verifier = exchange.await_args.args
    assert code == "c" and cfg is CFG and meta["token_endpoint"] == "https://idp.example/token"
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert digest == q["code_challenge"][0]


class _Resp(__import__("io").BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_exchange_code_posts_the_full_form():
    import asyncio
    from urllib.parse import parse_qs

    seen = {}

    def fake_open(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            form=parse_qs(request.data.decode()),
            ctype=request.get_header("Content-type"),
        )
        return _Resp(json.dumps({"id_token": "a.b.c"}).encode())

    meta = {
        "token_endpoint": "https://idp.example/token",
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }
    with patch.object(oidc._opener, "open", fake_open):
        token = asyncio.run(oidc.exchange_code(meta, CFG, "code-1", "ver-1"))
    assert token == "a.b.c"
    assert seen["method"] == "POST" and seen["ctype"] == "application/x-www-form-urlencoded"
    assert seen["form"] == {
        "grant_type": ["authorization_code"],
        "code": ["code-1"],
        "redirect_uri": [CFG.redirect_uri],
        "client_id": [CFG.client_id],
        "client_secret": [CFG.client_secret],
        "code_verifier": ["ver-1"],
    }


@pytest.mark.parametrize("body", [{}, {"id_token": None}, {"id_token": {"x": 1}}, {"id_token": 5}])
def test_exchange_code_needs_a_string_id_token(body):
    import asyncio

    with (
        patch.object(oidc._opener, "open", lambda r, timeout: _Resp(json.dumps(body).encode())),
        pytest.raises(oidc.OIDCError),
    ):
        asyncio.run(oidc.exchange_code({"token_endpoint": "https://idp.example/token"}, CFG, "c", "v"))


def test_exchange_code_refuses_plain_http():
    import asyncio

    with pytest.raises(oidc.OIDCError):
        asyncio.run(oidc.exchange_code({"token_endpoint": "http://idp.example/token"}, CFG, "c", "v"))


def test_garbled_provider_response_is_an_oidc_error():
    import http.client
    import urllib.request

    def boom(request, timeout):
        raise http.client.IncompleteRead(b"")

    with patch.object(oidc._opener, "open", boom), pytest.raises(oidc.OIDCError):
        oidc._http_json(urllib.request.Request("https://idp.example/token"))


def test_multi_audience_with_matching_azp_is_accepted():
    claims = oidc.validate_id_token(_jwt(_claims(aud=["client-1", "x"], azp="client-1")), CFG.issuer, CFG, "n1")
    assert claims["azp"] == "client-1"


def test_discovery_is_cached_single_flight_and_fails_fast():
    import asyncio
    import time as _time
    from unittest.mock import MagicMock

    good = {"issuer": CFG.issuer, "authorization_endpoint": "https://i/a", "token_endpoint": "https://i/t"}
    oidc._discovery_cache.clear()
    oidc._discovery_failed_at.clear()
    try:
        fetch = MagicMock(return_value=good)
        with patch.object(oidc, "_http_json", fetch):
            asyncio.run(oidc.discover(CFG))
            asyncio.run(oidc.discover(CFG))
            assert fetch.call_count == 1
            oidc._discovery_cache[CFG.issuer] = (_time.time() - oidc._DISCOVERY_TTL_SECONDS - 1, good)
            asyncio.run(oidc.discover(CFG))
            assert fetch.call_count == 2

        oidc._discovery_cache.clear()

        def slow_fail(request):
            _time.sleep(0.2)
            raise oidc.OIDCError("down")

        failing = MagicMock(side_effect=slow_fail)

        async def many():
            return await asyncio.gather(*(oidc.discover(CFG) for _ in range(8)), return_exceptions=True)

        with patch.object(oidc, "_http_json", failing):
            results = asyncio.run(many())
            assert all(isinstance(r, oidc.OIDCError) for r in results)
            assert failing.call_count == 1
            with pytest.raises(oidc.OIDCError):
                asyncio.run(oidc.discover(CFG))
            assert failing.call_count == 1
    finally:
        oidc._discovery_cache.clear()
        oidc._discovery_failed_at.clear()


# --- 9.4.0 review follow-ups ---------------------------------------------------


def _token_request(meta, cfg=CFG):
    """The token request exchange_code sends for ``meta``: (Authorization header, parsed form)."""
    import asyncio
    from urllib.parse import parse_qs

    seen = {}

    def fake_open(request, timeout):
        seen.update(auth=request.get_header("Authorization"), form=parse_qs(request.data.decode()))
        return _Resp(json.dumps({"id_token": "a.b.c"}).encode())

    with patch.object(oidc._opener, "open", fake_open):
        asyncio.run(oidc.exchange_code({"token_endpoint": "https://idp.example/token", **meta}, cfg, "c", "v"))
    return seen["auth"], seen["form"]


@pytest.mark.parametrize(
    "methods",
    [None, [], ["client_secret_basic"], ["client_secret_post", "client_secret_basic"], ["private_key_jwt"]],
)
def test_exchange_code_uses_http_basic_by_default(methods):
    meta = {} if methods is None else {"token_endpoint_auth_methods_supported": methods}
    cfg = oidc.OIDCConfig(CFG.issuer, "client 1:x", "s3cret/+&", CFG.redirect_uri)
    auth, form = _token_request(meta, cfg)
    # RFC 6749 2.3.1: each part form-urlencoded, then joined with ":".
    assert auth == "Basic " + base64.b64encode(b"client+1%3Ax:s3cret%2F%2B%26").decode()
    assert "client_secret" not in form and "client_id" not in form


def test_exchange_code_posts_the_secret_when_only_post_is_offered():
    auth, form = _token_request({"token_endpoint_auth_methods_supported": ["client_secret_post"]})
    assert auth is None
    assert form["client_id"] == [CFG.client_id] and form["client_secret"] == [CFG.client_secret]


def test_token_endpoint_error_names_the_status():
    import io
    import urllib.error
    import urllib.request

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"x"}'))

    with patch.object(oidc._opener, "open", refuse), pytest.raises(oidc.OIDCError, match="HTTP 401"):
        oidc._http_json(urllib.request.Request("https://idp.example/token"))


def test_discovery_rejects_an_incomplete_document():
    import asyncio

    with (
        patch.object(oidc, "_http_json", lambda r: {"issuer": CFG.issuer, "token_endpoint": "https://i/t"}),
        pytest.raises(oidc.OIDCError, match="incomplete"),
    ):
        asyncio.run(oidc._fetch_discovery(CFG))


def test_concurrent_cold_discoveries_share_one_fetch():
    import asyncio
    import time as _time
    from unittest.mock import MagicMock

    good = {"issuer": CFG.issuer, "authorization_endpoint": "https://i/a", "token_endpoint": "https://i/t"}

    def slow_ok(request):
        _time.sleep(0.2)
        return good

    oidc._discovery_cache.clear()
    oidc._discovery_failed_at.clear()
    try:
        fetch = MagicMock(side_effect=slow_ok)

        async def many():
            return await asyncio.gather(*(oidc.discover(CFG) for _ in range(8)))

        with patch.object(oidc, "_http_json", fetch):
            assert asyncio.run(many()) == [good] * 8
        assert fetch.call_count == 1
    finally:
        oidc._discovery_cache.clear()
        oidc._discovery_failed_at.clear()


def test_sso_is_off_without_password_auth(client):
    """OIDC_* on an anonymous or unconfigured viewer must not serve SSO at all."""
    tc, m, _ = client
    with patch.object(m, "AUTH_ENABLED", False):
        assert tc.get("/api/oidc/login").status_code == 404
        assert tc.get("/api/oidc/callback", params={"code": "c", "state": "x"}).status_code == 404
    assert not m._sessions


def test_callback_is_404_without_config(client):
    tc, m, _ = client
    with patch.object(m, "OIDC_CONFIG", None):
        assert tc.get("/api/oidc/callback", params={"code": "c", "state": "x"}).status_code == 404
    assert not m._sessions


def test_no_database_refuses_a_viewer_identity(client):
    tc, m, _ = client
    state, nonce = _begin(tc)
    with patch.object(m, "db", None):
        resp = _finish(tc, state, nonce, "new@example.com")
    assert resp.headers["location"] == "/?sso_error=refused"
    assert not m._sessions


def test_default_access_all_grants_every_chat(client):
    tc, m, db = client
    cfg = oidc.OIDCConfig(**{**CFG.__dict__, "default_access": "all"})
    with patch.object(m, "OIDC_CONFIG", cfg):
        state, nonce = _begin(tc)
        _finish(tc, state, nonce, "new@example.com")
    (session,) = m._sessions.values()
    assert session.role == "viewer" and session.allowed_chat_refs is None
    assert db.create_viewer_account.await_args.kwargs["allowed_chat_refs"] is None


def test_broken_audit_log_never_breaks_sso(client):
    tc, m, db = client
    db.create_audit_log = AsyncMock(side_effect=Exception("audit table gone"))
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "admin@example.com")
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    assert m.AUTH_COOKIE_NAME in resp.cookies
    resp = tc.get("/api/oidc/callback", params={"code": "c", "state": "forged"})
    assert resp.headers["location"] == "/?sso_error=expired"


def test_database_error_resolving_the_account_redirects(client):
    tc, m, db = client
    db.get_viewer_by_username = AsyncMock(side_effect=OSError("database restarting"))
    state, nonce = _begin(tc)
    resp = _finish(tc, state, nonce, "new@example.com")
    assert resp.status_code == 302 and resp.headers["location"] == "/?sso_error=unavailable"
    assert not m._sessions
    assert db.create_audit_log.await_args.kwargs["action"] == "login_failed"


@pytest.mark.parametrize("identity", ["token:family", "Token:Family"])
def test_unmapped_identity_in_the_share_token_namespace_is_refused(identity):
    import telegram_archive.web.main as m

    with pytest.raises(m.HTTPException) as exc:
        _resolve(PREF, identity.lower())
    assert exc.value.status_code == 403


def test_removed_oidc_admin_loses_the_master_session(client):
    tc, m, db = client
    state, nonce = _begin(tc)
    _finish(tc, state, nonce, "admin@example.com")
    (token,) = m._sessions
    assert tc.get("/api/auth/check").json()["role"] == "master"

    demoted = oidc.OIDCConfig(**{**CFG.__dict__, "admin_users": frozenset()})
    with patch.object(m, "OIDC_CONFIG", demoted):
        assert tc.get("/api/auth/check").json()["authenticated"] is False
        assert tc.get("/api/admin/viewers").status_code == 401
    assert token not in m._sessions
    db.delete_session.assert_any_await(token)


def test_restored_master_row_of_a_removed_admin_is_not_master():
    """After a restart the session comes back from its row; the current config decides."""
    import asyncio

    import telegram_archive.web.main as m

    now = time.time()
    row = {"username": "gone@example.com", "role": "master", "created_at": now, "last_accessed": now}
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=row)
    with patch.object(m, "db", db), patch.object(m, "OIDC_CONFIG", CFG), patch.object(m, "VIEWER_USERNAME", "hyper"):
        m._sessions.clear()
        assert asyncio.run(m._resolve_session("tok-1")) is None
        assert "tok-1" not in m._sessions
        db.get_session = AsyncMock(return_value={**row, "username": "admin@example.com"})
        assert asyncio.run(m._resolve_session("tok-2")).role == "master"
        db.get_session = AsyncMock(return_value={**row, "username": "hyper"})
        assert asyncio.run(m._resolve_session("tok-3")).role == "master"
        m._sessions.clear()


def test_end_session_url_returns_to_the_viewer_root():
    from urllib.parse import parse_qs, urlparse

    assert oidc.end_session_url({}, CFG) is None
    assert oidc.end_session_url({"end_session_endpoint": "http://idp.example/logout"}, CFG) is None
    url = oidc.end_session_url({"end_session_endpoint": "https://idp.example/logout?x=1"}, CFG)
    parsed = urlparse(url)
    assert (parsed.netloc, parsed.path) == ("idp.example", "/logout")
    assert parse_qs(parsed.query) == {
        "x": ["1"],
        "client_id": [CFG.client_id],
        "post_logout_redirect_uri": ["https://viewer.example/"],
    }
    sub = oidc.OIDCConfig(CFG.issuer, "c", "s", "https://viewer.example/archive/api/oidc/callback")
    url = oidc.end_session_url({"end_session_endpoint": "https://idp.example/logout"}, sub)
    assert parse_qs(urlparse(url).query)["post_logout_redirect_uri"] == ["https://viewer.example/archive/"]


LOGOUT_META = {
    "issuer": CFG.issuer,
    "authorization_endpoint": "https://idp.example/authorize",
    "token_endpoint": "https://idp.example/token",
    "end_session_endpoint": "https://idp.example/logout",
}


def test_sso_logout_also_ends_the_provider_session(client):
    tc, m, db = client
    with patch.object(oidc, "discover", AsyncMock(return_value=LOGOUT_META)):
        state, nonce = _begin(tc)
        _finish(tc, state, nonce, "new@example.com")
        (token,) = m._sessions
        resp = tc.post("/api/logout")
    assert resp.json()["logout_url"].startswith("https://idp.example/logout?client_id=client-1")
    # The local teardown is unchanged.
    assert not m._sessions
    db.delete_session.assert_any_await(token)
    assert db.create_audit_log.await_args.kwargs["action"] == "logout"


def test_logout_without_end_session_endpoint_stays_local(client):
    tc, m, _ = client
    state, nonce = _begin(tc)
    _finish(tc, state, nonce, "new@example.com")
    resp = tc.post("/api/logout")
    assert resp.json() == {"success": True}
    assert not m._sessions


def test_password_login_clears_a_stale_sso_marker(client):
    """An SSO session that expired without logout must not make a later
    password session's logout go to the provider."""
    tc, _, _ = client
    tc.cookies.set("oidc_session", "1", domain="testserver.local", path="/api/logout")
    with patch.object(oidc, "discover", AsyncMock(return_value=LOGOUT_META)):
        resp = tc.post("/api/login", json={"username": "hyper", "password": "pw"})
        assert resp.status_code == 200
        assert 'oidc_session=""' in resp.headers.get("set-cookie", "")
        assert tc.post("/api/logout").json() == {"success": True}


def test_password_logout_never_redirects_to_the_provider(client):
    tc, m, _ = client
    with patch.object(oidc, "discover", AsyncMock(return_value=LOGOUT_META)):
        assert tc.post("/api/login", json={"username": "hyper", "password": "pw"}).status_code == 200
        resp = tc.post("/api/logout")
    assert resp.json() == {"success": True}
    assert not m._sessions
