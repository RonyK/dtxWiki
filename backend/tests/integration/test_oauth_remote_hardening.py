"""Sprint_5 Phase_3 Task 3.2 + 3.3 + 3.4 — DCR + consent CSRF + refresh
reuse / access JWT denylist hardening (ADRs A3, A5, A11).

Task 3.2 verifies the DCR endpoint (``POST /oauth/register``):
- Rejects empty ``redirect_uris``, oversized ``client_name``, and unknown
  ``scope`` values with HTTP 400 + a pinned-keyword body (ADR A6).
- Accepts the ``mcp:agent`` scope per H22 positive control.
- Emits exactly one ``audit_event`` row with ``action=oauth.client.registered``
  and ``workspace_id IS NULL`` per ADR A11 (the audit schema keeps
  ``audit_event.workspace_id`` nullable).

Task 3.3 verifies the consent flow's CSRF state binding:
- GET ``/oauth/authorize`` sets a one-time ``oauth_csrf_state`` cookie with
  HttpOnly + SameSite=Lax + Path=/oauth + Max-Age=600.
- POST ``/oauth/authorize`` rejects {missing, mismatch, replay, expired}
  state with 400 + an ``oauth.consent.csrf`` audit row carrying the reason.
- POST happy path consumes the cookie one-time (deleted on the response).

Task 3.4 verifies refresh-reuse compromise handling + access JWT denylist:
- Normal rotation succeeds and inserts a row in ``oauth_access_token_jti``.
- Refresh reuse triggers family revoke + ``oauth.refresh.reuse`` audit;
  the prior access JWT is rejected by ``/mcp`` with keyword ``revoked``.
- Missing-jti JWT is rejected (defense in depth).
- Denylist cache is invalidated immediately after revoke.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.oauth import OAuthAccessTokenJti, OAuthClient, OAuthRefreshToken
from backend.services.oauth_service import (
    _OAUTH_CSRF_ISSUED_STATES,
    OAUTH_CSRF_COOKIE_NAME,
    REFRESH_REFUSAL_REASONS,
    REFRESH_REUSE_GRACE_WINDOW_SECONDS,
    challenge_s256,
    token_hash,
)
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.oauth_flow


def make_client(
    page_service,
    *,
    security_headers: bool = False,
    settings_overrides: dict[str, Any] | None = None,
) -> TestClient:
    settings_values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "test-session-secret-for-oauth-hardening",
        "public_base_url": "http://testserver",
        "mcp_http_mode": "json_stateless",
        "app_mode": "team" if security_headers else "solo_local",
        "signup_enabled": True,
        **(settings_overrides or {}),
    }
    client = TestClient(
        create_app(
            settings=Settings(**settings_values),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def _valid_payload(**overrides):
    payload = {
        "client_name": "Hardening Test Client",
        "redirect_uris": ["http://127.0.0.1:53682/callback"],
        "scope": "mcp:read mcp:write",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Positive control: DCR registers + emits one audit row with workspace_id=NULL.
# ---------------------------------------------------------------------------


def test_register_emits_audit_with_null_workspace(page_service):
    client = make_client(page_service)
    resp = client.post("/oauth/register", json=_valid_payload())
    assert resp.status_code == 201, resp.text
    client_id = resp.json()["client_id"]

    with page_service.session_factory() as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oauth.client.registered")
        ).all()
    assert len(rows) == 1, f"expected 1 dcr audit row, got {len(rows)}"
    row = rows[0]
    assert row.workspace_id is None, (
        "DCR audit row must persist with workspace_id=NULL per ADR A11"
    )
    assert row.actor_kind == "system"
    assert row.actor_id == "dcr"
    assert row.target_kind == "oauth_client"
    assert row.retention_class == "sensitive"
    assert row.payload["target_external_id"] == client_id
    assert row.payload["client_id"] == client_id
    assert row.payload["scope"] == "mcp:read mcp:write"


def test_register_accepts_mcp_agent_scope(page_service):
    """H22 positive control: ``mcp:agent`` is in ALLOWED_OAUTH_SCOPES."""

    client = make_client(page_service)
    resp = client.post(
        "/oauth/register",
        json=_valid_payload(scope="mcp:agent"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scope"] == "mcp:agent"


# ---------------------------------------------------------------------------
# Negative tests (ADR A6: keyword-pinned, paired with positives above).
# ---------------------------------------------------------------------------


def test_register_rejects_empty_redirects_with_keyword(page_service):
    client = make_client(page_service)
    resp = client.post(
        "/oauth/register",
        json=_valid_payload(redirect_uris=[]),
    )
    assert resp.status_code == 400, resp.text
    assert "redirect_uris" in resp.text


def test_register_rejects_oversized_client_name_with_keyword(page_service):
    client = make_client(page_service)
    resp = client.post(
        "/oauth/register",
        json=_valid_payload(client_name="x" * 201),
    )
    assert resp.status_code == 400, resp.text
    assert "client_name" in resp.text


def test_register_accepts_max_length_client_name(page_service):
    """Positive control paired with the oversized rejection: exactly 200
    chars is accepted (boundary not off-by-one)."""

    client = make_client(page_service)
    resp = client.post(
        "/oauth/register",
        json=_valid_payload(client_name="x" * 200),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["client_name"] == "x" * 200


def test_register_rejects_unknown_scope_with_keyword(page_service):
    client = make_client(page_service)
    resp = client.post(
        "/oauth/register",
        json=_valid_payload(scope="mcp:admin"),
    )
    assert resp.status_code == 400, resp.text
    assert "scope" in resp.text


# ---------------------------------------------------------------------------
# Audit row uniqueness: a second DCR call writes a *second* row (no swap).
# ---------------------------------------------------------------------------


def test_register_twice_emits_two_audit_rows(page_service):
    """Positive control on row insertion semantics: each successful DCR
    appends a row; the helper does not de-duplicate by client_id."""

    client = make_client(page_service)
    client.post("/oauth/register", json=_valid_payload(client_name="A"))
    client.post("/oauth/register", json=_valid_payload(client_name="B"))

    with page_service.session_factory() as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oauth.client.registered")
        ).all()
    assert len(rows) == 2
    assert {r.workspace_id for r in rows} == {None}


def test_register_reuses_exact_client_identity(page_service):
    client = make_client(page_service)
    first = client.post("/oauth/register", json=_valid_payload())
    repeated = client.post("/oauth/register", json=_valid_payload())
    different_loopback_port = client.post(
        "/oauth/register",
        json=_valid_payload(redirect_uris=["http://127.0.0.1:53683/callback"]),
    )
    different_scope = client.post(
        "/oauth/register",
        json=_valid_payload(scope="mcp:read"),
    )

    assert (
        first.status_code
        == repeated.status_code
        == different_loopback_port.status_code
        == different_scope.status_code
        == 201
    )
    assert repeated.json()["client_id"] == first.json()["client_id"]
    assert different_loopback_port.json()["client_id"] == first.json()["client_id"]
    assert different_scope.json()["client_id"] != first.json()["client_id"]
    with page_service.session_factory() as session:
        rows = session.scalars(select(OAuthClient)).all()
    assert len(rows) == 2
    assert all(row.registration_identity for row in rows)
    assert len({row.registration_identity for row in rows}) == 2
    assert any(
        constraint.name == "uq_oauth_client_registration_identity"
        for constraint in OAuthClient.__table__.constraints
    )


# =========================================================================
# Task 3.3 — Consent POST CSRF state-bound cookie (ADR A3)
# =========================================================================


_CONSENT_STATE_GOOD = "consent-csrf-state-AAAAAAAAA"
_CONSENT_STATE_BAD = "consent-csrf-state-BBBBBBBBB"


def _register_consent_client(client: TestClient) -> str:
    """Register a DCR client for the consent-CSRF test matrix."""

    resp = client.post(
        "/oauth/register",
        json=_valid_payload(client_name="Consent CSRF Client"),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


def _authorize_params(client_id: str, *, state: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256("v" * 64),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": state,
    }


def _oauth_state(label: str) -> str:
    if len(label) >= 22:
        return label
    return f"{label}-0123456789abcdef"


def _current_workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def _consent_csrf_events(page_service) -> list[AuditEvent]:
    with page_service.session_factory() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "oauth.consent.csrf")
            ).all()
        )


def test_consent_csp_allows_only_validated_loopback_origin(page_service):
    client = make_client(page_service, security_headers=True)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)

    response = client.get("/oauth/authorize", params=params)

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "form-action 'self' http://127.0.0.1:53682" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert 'nonce="' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_consent_csp_does_not_widen_for_non_loopback_origin(page_service):
    client = make_client(page_service, security_headers=True)
    redirect_uri = "https://client.example/callback"
    registered = client.post(
        "/oauth/register",
        json=_valid_payload(redirect_uris=[redirect_uri]),
    )
    assert registered.status_code == 201, registered.text
    params = {
        **_authorize_params(registered.json()["client_id"], state=_CONSENT_STATE_GOOD),
        "redirect_uri": redirect_uri,
    }

    response = client.get("/oauth/authorize", params=params)

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "form-action 'self';" in csp
    assert "client.example" not in csp


def test_consent_csrf_failure_content_negotiates_html_and_json(page_service):
    html_client = make_client(page_service)
    client_id = _register_consent_client(html_client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    html_response = html_client.post(
        "/oauth/authorize",
        data={**params, "decision": "allow"},
        headers={"accept": "text/html"},
    )

    assert html_response.status_code == 400
    assert html_response.headers["content-type"].startswith("text/html")
    assert html_response.headers["cache-control"] == "no-store"
    assert "restart its OAuth connection" in html_response.text
    assert "codex" not in html_response.text.casefold()
    assert "이미 처리되었거나" in html_response.text

    json_client = html_client
    json_client_id = _register_consent_client(json_client)
    json_params = _authorize_params(json_client_id, state=_CONSENT_STATE_BAD)
    json_response = json_client.post(
        "/oauth/authorize",
        data={**json_params, "decision": "allow"},
        headers={"accept": "application/json"},
    )

    assert json_response.status_code == 400
    assert json_response.headers["content-type"].startswith("application/json")
    assert json_response.headers["cache-control"] == "no-store"
    assert json_response.json()["message"] == "state binding failed"


# --- Positive controls -----------------------------------------------------


def test_consent_state_happy_path(page_service):
    """GET sets the cookie, POST with matching state is accepted and the
    cookie is consumed (one-time). No ``oauth.consent.csrf`` audit row."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)

    get_resp = client.get("/oauth/authorize", params=params)
    assert get_resp.status_code == 200
    assert client.cookies.get(OAUTH_CSRF_COOKIE_NAME) == _CONSENT_STATE_GOOD

    post_resp = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code == 303, post_resp.text
    # One-time consumption: the response clears the cookie.
    assert client.cookies.get(OAUTH_CSRF_COOKIE_NAME) is None

    assert _consent_csrf_events(page_service) == []


def test_consent_cookie_flags_set_correctly(page_service):
    """The GET ``Set-Cookie`` header must declare HttpOnly, SameSite=Lax,
    Path=/oauth, Max-Age=600 per ADR A3. Secure is environment-dependent;
    in the development test environment it is False, which we assert
    explicitly so a regression to True (without app_env=production) is
    caught."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    get_resp = client.get("/oauth/authorize", params=params)
    assert get_resp.status_code == 200
    set_cookie = get_resp.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/oauth" in set_cookie
    assert "Max-Age=600" in set_cookie


# --- Negative tests (ADR A6 keyword + audit assertion) ---------------------


def test_consent_state_missing_400(page_service):
    """Form ``state`` empty → 400 + audit reason=missing + keyword."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    get_resp = client.get("/oauth/authorize", params=params)
    assert get_resp.status_code == 200

    bad_params = {**params, "state": ""}
    post_resp = client.post(
        "/oauth/authorize",
        data={
            **bad_params,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code == 400
    body = post_resp.json()
    assert body["error_code"] == "invalid_request"
    assert "state binding" in body["message"]

    events = _consent_csrf_events(page_service)
    assert len(events) == 1
    assert events[0].payload["reason"] == "missing"
    assert events[0].payload["client_id"] == client_id
    assert events[0].actor_kind == "user_session"


def test_consent_csrf_audit_records_authenticated_workspace(page_service):
    """SEC-P3-1 regression: authenticated consent-CSRF failures should keep
    forensic workspace context when the user's current workspace can be safely
    resolved, instead of always persisting workspace_id=NULL."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    assert client.get("/oauth/authorize", params=params).status_code == 200
    workspace_id = _current_workspace_id(client)

    post_resp = client.post(
        "/oauth/authorize",
        data={
            **params,
            "state": "",
            "workspace_ids": [workspace_id],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code == 400

    events = _consent_csrf_events(page_service)
    assert len(events) == 1
    assert str(events[0].workspace_id) == workspace_id


def test_consent_state_mismatch_400(page_service):
    """Cookie=A, form=B → 400 + audit reason=mismatch."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params_a = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    get_resp = client.get("/oauth/authorize", params=params_a)
    assert get_resp.status_code == 200
    assert client.cookies.get(OAUTH_CSRF_COOKIE_NAME) == _CONSENT_STATE_GOOD

    post_resp = client.post(
        "/oauth/authorize",
        data={
            **params_a,
            "state": _CONSENT_STATE_BAD,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code == 400
    # Cookie deleted on failure (one-time).
    assert client.cookies.get(OAUTH_CSRF_COOKIE_NAME) is None

    events = _consent_csrf_events(page_service)
    assert len(events) == 1
    assert events[0].payload["reason"] == "mismatch"


def test_consent_state_replay_400(page_service):
    """First POST succeeds and consumes the state; a second POST with the
    same state must fail with reason=replay (the cookie has been deleted
    and the state is now in the consumed bucket)."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    client.get("/oauth/authorize", params=params)
    first = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert _consent_csrf_events(page_service) == []

    # Second POST: cookie has been deleted by the first response; the state
    # is in the server-side consumed bucket → classified as replay.
    replay = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert replay.status_code == 400
    # Sprint_5 Phase_3 Task 3.7 (TEST-FR3MID-2): keyword pin so the
    # failure mode is unambiguous in line with the other 3 CSRF variants.
    assert "state binding" in replay.json()["message"]
    events = _consent_csrf_events(page_service)
    assert len(events) == 1
    assert events[0].payload["reason"] == "replay"


def test_consent_state_expired_400(page_service):
    """A state issued > 600s ago whose cookie has been dropped by the
    browser is classified as expired (not replay, since the state was never
    consumed). The cookie max_age is enforced by the browser, not the
    server, so we simulate the elapsed time by backdating the
    issued-state bucket and clearing the TestClient cookie."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    get_resp = client.get("/oauth/authorize", params=params)
    assert get_resp.status_code == 200

    # Backdate the issued timestamp so classify_oauth_csrf_failure sees the
    # state as past the 600s window. Drop the cookie to mimic the browser
    # having discarded it after Max-Age.
    assert _CONSENT_STATE_GOOD in _OAUTH_CSRF_ISSUED_STATES
    _OAUTH_CSRF_ISSUED_STATES[_CONSENT_STATE_GOOD] = (
        _OAUTH_CSRF_ISSUED_STATES[_CONSENT_STATE_GOOD] - 601
    )
    client.cookies.delete(OAUTH_CSRF_COOKIE_NAME, path="/oauth")

    expired = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert expired.status_code == 400
    # Sprint_5 Phase_3 Task 3.7 (TEST-FR3MID-4): keyword pin so the
    # failure mode is unambiguous in line with the other 3 CSRF variants.
    assert "state binding" in expired.json()["message"]
    events = _consent_csrf_events(page_service)
    assert len(events) == 1
    assert events[0].payload["reason"] == "expired"


def test_consent_state_error_body_shape_matches_adr_a9(page_service):
    """The 400 body for a CSRF failure carries the established REST error
    shape (``error_code`` + ``message`` + envelope) per ADR A9. The
    ``error_code`` is the OAuth ``invalid_request`` slug; the ``message``
    contains the keyword ``state binding`` so callers can pin the failure
    mode (ADR A6)."""

    client = make_client(page_service)
    client_id = _register_consent_client(client)
    params = _authorize_params(client_id, state=_CONSENT_STATE_GOOD)
    client.get("/oauth/authorize", params=params)

    post_resp = client.post(
        "/oauth/authorize",
        data={
            **params,
            "state": _CONSENT_STATE_BAD,
            "workspace_ids": [_current_workspace_id(client)],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    body = post_resp.json()
    assert "error_code" in body
    assert "message" in body
    assert body["error_code"] == "invalid_request"
    assert "state binding" in body["message"]


# =========================================================================
# Task 3.4 — Refresh reuse compromise + access JWT denylist (ADR A5)
# =========================================================================


def _seed_oauth_tokens(
    client: TestClient,
    *,
    state: str,
    client_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Walk the full DCR -> authorize -> token flow and return the issued
    access + refresh tokens + client_id for refresh-reuse tests."""

    state = _oauth_state(state)
    if client_id is None:
        register = client.post(
            "/oauth/register",
            json=_valid_payload(client_name=f"Refresh Test {state}"),
        )
        assert register.status_code == 201, register.text
        client_id = register.json()["client_id"]
    verifier = "v" * 64
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": state,
    }
    if workspace_id is None:
        workspace_id = _current_workspace_id(client)
    # A second grant for a client the user already consented to skips the
    # approval screen and redirects straight to the callback, so this GET must
    # not follow redirects: the callback host is not part of the test app and
    # following it lands on the SPA fallback's 404 instead of the flow.
    get_resp = client.get(
        "/oauth/authorize",
        params={**params, "workspace_id": workspace_id},
        follow_redirects=False,
    )
    assert get_resp.status_code in (200, 302), get_resp.text
    from urllib.parse import parse_qs, urlparse

    if get_resp.status_code == 302:
        # Re-consent: the code is already in the redirect, and no approval form
        # was rendered, so there is no CSRF state to bind a POST to. Posting
        # anyway is what a browser never does, and it fails state binding.
        code = parse_qs(urlparse(get_resp.headers["location"]).query)["code"][0]
    else:
        authorize = client.post(
            "/oauth/authorize",
            data={**params, "workspace_ids": [workspace_id], "decision": "allow"},
            follow_redirects=False,
        )
        assert authorize.status_code == 303, authorize.text
        code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    body = token.json()
    return {
        "client_id": client_id,
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "refresh_token_expires_in": body["refresh_token_expires_in"],
    }


def _seed_second_workspace(page_service, *, slug: str) -> tuple[str, str]:
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership

    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        assert user is not None
        workspace = Workspace(
            slug=slug,
            title=f"Second {slug}",
            name=f"Second {slug}",
            description="OAuth revoke workspace-scope fixture",
            goal="OAuth revoke workspace-scope fixture",
        )
        session.add(workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        return str(workspace.id), workspace.slug


def _decode_access_jti(token: str) -> str:
    """Decode an RS256 JWT's unverified payload to read the ``jti`` claim."""

    import jwt

    payload = jwt.decode(token, options={"verify_signature": False})
    return payload["jti"]


def _refresh_reuse_audit_rows(page_service) -> list[AuditEvent]:
    with page_service.session_factory() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "oauth.refresh.reuse")
            ).all()
        )


def _expire_reuse_grace_window(page_service, refresh_token: str) -> None:
    """Push a just-rotated row's ``rotated_at`` outside the P10.3 T3 30s
    reuse-grace window (section 2.3, ``REFRESH_REUSE_GRACE_WINDOW_SECONDS``)
    so a subsequent replay of it exercises real reuse-detection (family
    revoke) instead of a grace reissue (a fresh sibling pair, 200)."""

    with page_service.session_factory.begin() as session:
        row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(refresh_token)
            )
        )
        assert row is not None
        assert row.rotated_at is not None
        row.rotated_at -= timedelta(seconds=REFRESH_REUSE_GRACE_WINDOW_SECONDS + 1)


# --- Positive controls ----------------------------------------------------


def test_refresh_rotation_happy_path(page_service):
    """Normal refresh rotates the refresh token, mints a new access JWT,
    persists a new jti row, and marks the prior refresh ``revoked_at`` +
    ``rotated_to_id``. No ``oauth.refresh.reuse`` audit row."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="refresh-happy")

    refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh.status_code == 200, refresh.text
    new_access = refresh.json()["access_token"]
    new_refresh = refresh.json()["refresh_token"]
    assert new_refresh != tokens["refresh_token"]
    assert new_access != tokens["access_token"]

    # Old refresh row is now revoked + rotated_to_id set.
    with page_service.session_factory() as session:
        old_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert old_row is not None
        assert old_row.revoked_at is not None
        assert old_row.rotated_to_id is not None

    assert _refresh_reuse_audit_rows(page_service) == []


def test_refresh_rotation_preserves_stable_grant_identity(page_service):
    import jwt

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="stable-grant")
    first_claims = jwt.decode(
        tokens["access_token"], options={"verify_signature": False}
    )

    refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh.status_code == 200, refresh.text
    second_claims = jwt.decode(
        refresh.json()["access_token"], options={"verify_signature": False}
    )
    assert second_claims["grant_id"] == first_claims["grant_id"]

    with page_service.session_factory() as session:
        rows = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.client_id == tokens["client_id"]
                )
            ).all()
        )
    assert len(rows) == 2
    assert {str(row.grant_id) for row in rows} == {first_claims["grant_id"]}


def test_refresh_token_lifetime_is_usable_and_rotation_slides(page_service):
    from backend.models.base import utc_now

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="o5-sliding-lifetime")
    # MCP-05: restored to the pre-branch 30-day default (was silently
    # widened to 180 days).
    assert tokens["refresh_token_expires_in"] == 60 * 60 * 24 * 30

    now = utc_now()
    with page_service.session_factory.begin() as session:
        original = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert original is not None
        original.created_at = now - timedelta(days=29)
        original.expires_at = now + timedelta(days=1)

    rotated_at = utc_now()
    refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh.status_code == 200, refresh.text
    assert refresh.json()["refresh_token_expires_in"] == 60 * 60 * 24 * 30

    with page_service.session_factory() as session:
        successor = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(refresh.json()["refresh_token"])
            )
        )
        assert successor is not None
        remaining = (successor.expires_at - rotated_at).total_seconds()
        assert 60 * 60 * 24 * 30 - 5 <= remaining <= 60 * 60 * 24 * 30 + 5
        row_lifetime = int(
            (successor.expires_at - successor.created_at).total_seconds()
        )
        assert abs(refresh.json()["refresh_token_expires_in"] - row_lifetime) <= 1


@pytest.mark.parametrize(
    "reason",
    sorted(REFRESH_REFUSAL_REASONS),
)
def test_refresh_refusal_reason_reaches_audit_and_metrics(page_service, reason):
    from backend.api import metrics
    from backend.models.base import utc_now
    from backend.models.oauth import OAuthConsent
    from backend.models.user import User

    metrics.reset_metrics_for_test()
    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state=f"o5-refusal-{reason}")

    if reason == "rotated_reuse":
        rotated = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": tokens["client_id"],
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert rotated.status_code == 200, rotated.text
        # Outside the P10.3 T3 30s reuse-grace window so the replay below
        # exercises real reuse-detection, not a grace reissue.
        _expire_reuse_grace_window(page_service, tokens["refresh_token"])
    else:
        with page_service.session_factory.begin() as session:
            row = session.scalar(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_lookup_hash
                    == token_hash(tokens["refresh_token"])
                )
            )
            assert row is not None
            if reason == "expired":
                row.created_at = utc_now() - timedelta(days=181)
                row.expires_at = utc_now() - timedelta(days=1)
            elif reason == "revoked":
                row.revoked_at = utc_now()
            elif reason == "consent_missing":
                # P10.3 T3: OAuthRefreshToken carries no workspace_id any more
                # (section 2.3) -- the consent row is keyed by (user, client)
                # only from this row's point of view (_seed_oauth_tokens grants
                # exactly one workspace per call, so there is exactly one live
                # consent row for this pair).
                consent = session.scalar(
                    select(OAuthConsent).where(
                        OAuthConsent.user_id == row.user_id,
                        OAuthConsent.client_id == row.client_id,
                    )
                )
                assert consent is not None
                session.delete(consent)
            elif reason == "user_inactive":
                user = session.get(User, row.user_id)
                assert user is not None
                user.status = "disabled"

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": (
                "different-client"
                if reason == "client_mismatch"
                else tokens["client_id"]
            ),
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert response.status_code == 400
    assert set(response.json()) == {"error", "error_description"}
    assert response.json()["error"] == "invalid_grant"

    with page_service.session_factory() as session:
        refusal = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "oauth.refresh.refused")
            .order_by(AuditEvent.created_at.desc())
        ).first()
        assert refusal is not None
        assert refusal.payload["reason"] == reason
        assert UUID(refusal.payload["family_key"])
        assert refusal.payload["token_age_seconds"] >= 0
        serialized_payload = str(refusal.payload)
        assert tokens["refresh_token"] not in serialized_payload
        assert token_hash(tokens["refresh_token"]) not in serialized_payload

    samples = metrics._counter_snapshot()["oauth_token_grants_total"]
    assert any(
        ("refusal_reason", reason) in labels and count == 1
        for labels, count in samples.items()
    )


def test_refresh_metric_uses_the_service_refusal_registry():
    from backend.api import metrics
    from backend.services import oauth_service

    assert metrics.REFRESH_REFUSAL_REASONS is oauth_service.REFRESH_REFUSAL_REASONS


def test_expired_refresh_is_restart_invariant(page_service):
    from backend.models.base import utc_now
    from backend.services.oauth_service import (
        InvalidOAuthGrantError,
        OAuthService,
    )

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="o5-restart-invariant")
    with page_service.session_factory.begin() as session:
        row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert row is not None
        row.created_at = utc_now() - timedelta(days=181)
        row.expires_at = utc_now() - timedelta(days=1)
        expected_family = row.grant_id or row.id

    for _ in range(2):
        fresh_service = OAuthService(
            session_factory=page_service.session_factory,
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
            ),
            public_base_url="http://testserver",
        )
        with pytest.raises(InvalidOAuthGrantError) as excinfo:
            fresh_service.refresh_access_token(
                refresh_token=tokens["refresh_token"],
                client_id=tokens["client_id"],
            )
        assert excinfo.value.refusal_reason == "expired"

    with page_service.session_factory() as session:
        row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert row is not None
        assert (row.grant_id or row.id) == expected_family
        assert row.revoked_at is None


def _o7_consent(client: TestClient, client_id: str) -> dict[str, Any]:
    response = client.get("/oauth/consents")
    assert response.status_code == 200, response.text
    return next(
        item for item in response.json()["data"] if item["client_id"] == client_id
    )


def _o7_update_consent(
    client: TestClient,
    consent: dict[str, Any],
    payload: dict[str, Any],
):
    # P10.3 T3 (section 2.3/2.4): the PATCH unit is the whole (user, client)
    # consent GROUP now -- ``client_id`` alone identifies it, no
    # ``workspace_id`` query param any more.
    return client.patch(
        "/oauth/consents",
        params={"client_id": consent["client_id"]},
        json=payload,
    )


def test_o7_non_sliding_family_expires_at_absolute_deadline(page_service):
    client = make_client(page_service)
    initial = _seed_oauth_tokens(client, state="o7-absolute-initial")
    consent = _o7_consent(client, initial["client_id"])
    updated = _o7_update_consent(
        client,
        consent,
        {"sliding_enabled": False},
    )
    assert updated.status_code == 200, updated.text

    tokens = _seed_oauth_tokens(
        client,
        state="o7-absolute-family",
        client_id=initial["client_id"],
        workspace_id=consent["workspaces"][0]["id"],
    )
    deadlines = []
    for _ in range(2):
        with page_service.session_factory() as session:
            current = session.scalar(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_lookup_hash
                    == token_hash(tokens["refresh_token"])
                )
            )
            assert current is not None
            deadlines.append(current.expires_at)
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": tokens["client_id"],
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert response.status_code == 200, response.text
        tokens["refresh_token"] = response.json()["refresh_token"]

    with page_service.session_factory.begin() as session:
        current = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert current is not None
        deadlines.append(current.expires_at)
        current.expires_at = utc_now() - timedelta(seconds=1)

    assert deadlines[0] == deadlines[1] == deadlines[2]
    expired = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert expired.status_code == 400
    assert expired.json() == {
        "error": "invalid_grant",
        "error_description": "refresh token is expired",
    }
    with page_service.session_factory() as session:
        refusal = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "oauth.refresh.refused")
            .order_by(AuditEvent.created_at.desc())
        ).first()
        assert refusal is not None
        assert refusal.payload["reason"] == "expired"


def test_o7_shorter_consent_ttl_expires_before_instance_default(page_service):
    client = make_client(page_service)
    initial = _seed_oauth_tokens(client, state="o7-short-initial")
    consent = _o7_consent(client, initial["client_id"])
    shorter_ttl = 30 * 24 * 60 * 60
    updated = _o7_update_consent(
        client,
        consent,
        {"refresh_ttl_seconds": shorter_ttl},
    )
    assert updated.status_code == 200, updated.text

    tokens = _seed_oauth_tokens(
        client,
        state="o7-short-family",
        client_id=initial["client_id"],
        workspace_id=consent["workspaces"][0]["id"],
    )
    assert tokens["refresh_token_expires_in"] == shorter_ttl
    assert tokens["refresh_token_expires_in"] < 180 * 24 * 60 * 60

    with page_service.session_factory.begin() as session:
        current = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert current is not None
        current.expires_at = utc_now() - timedelta(seconds=1)
    expired = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert expired.status_code == 400
    assert expired.json()["error"] == "invalid_grant"


def test_o7_setting_change_is_not_retroactive_and_next_rotation_uses_it(
    page_service,
):
    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="o7-next-rotation")
    consent = _o7_consent(client, tokens["client_id"])
    with page_service.session_factory() as session:
        current = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert current is not None
        issued_expiry = current.expires_at

    shorter_ttl = 30 * 24 * 60 * 60
    updated = _o7_update_consent(
        client,
        consent,
        {"refresh_ttl_seconds": shorter_ttl},
    )
    assert updated.status_code == 200, updated.text
    with page_service.session_factory() as session:
        current = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(tokens["refresh_token"])
            )
        )
        assert current is not None
        assert current.expires_at == issued_expiry

    rotated_at = utc_now()
    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token_expires_in"] == shorter_ttl
    with page_service.session_factory() as session:
        successor = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(rotated.json()["refresh_token"])
            )
        )
        assert successor is not None
        remaining = (successor.expires_at - rotated_at).total_seconds()
        assert shorter_ttl - 5 <= remaining <= shorter_ttl + 5
        audit = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "oauth.consent.settings.updated")
            .order_by(AuditEvent.created_at.desc())
        ).first()
        assert audit is not None
        assert audit.payload["new_refresh_ttl_seconds"] == shorter_ttl


def test_o7_ttl_above_instance_max_returns_422(page_service):
    instance_ttl = 90 * 24 * 60 * 60
    client = make_client(
        page_service,
        settings_overrides={"oauth_refresh_token_ttl_seconds": instance_ttl},
    )
    tokens = _seed_oauth_tokens(client, state="o7-above-instance")
    consent = _o7_consent(client, tokens["client_id"])

    response = _o7_update_consent(
        client,
        consent,
        {"refresh_ttl_seconds": 180 * 24 * 60 * 60},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "refresh_ttl_above_instance_max"


def test_o7_null_ttl_matches_instance_default(page_service):
    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="o7-null-default")
    consent = _o7_consent(client, tokens["client_id"])
    assert (
        _o7_update_consent(
            client,
            consent,
            {"refresh_ttl_seconds": 30 * 24 * 60 * 60},
        ).status_code
        == 200
    )
    reset = _o7_update_consent(
        client,
        consent,
        {"refresh_ttl_seconds": None},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["refresh_ttl_seconds"] is None
    # MCP-05: restored to the pre-branch 30-day instance default.
    assert reset.json()["data"]["effective_refresh_ttl_seconds"] == (30 * 24 * 60 * 60)

    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token_expires_in"] == 30 * 24 * 60 * 60


def test_jti_row_inserted_on_access_token_issuance(page_service):
    """ADR A5: every access JWT issuance writes one row in
    ``oauth_access_token_jti`` keyed by the JWT's ``jti`` claim."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="jti-row-1")
    jti = _decode_access_jti(tokens["access_token"])

    with page_service.session_factory() as session:
        row = session.get(OAuthAccessTokenJti, jti)
    assert row is not None, "jti row must be persisted on access JWT issuance"
    assert row.client_id == tokens["client_id"]
    assert row.revoked_at is None


def test_pre_rotation_access_jwt_still_valid_when_refresh_rotates(page_service):
    """Positive control (ADR A6 pair for refresh reuse): after a *normal*
    refresh rotation, the access JWT minted from the prior token is still
    accepted until its own expiry. Only refresh-reuse compromise revokes
    the family — a clean rotation does not."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="jwt-still-valid")

    refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh.status_code == 200

    # Use the original (pre-rotation) access JWT against /mcp.
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200, response.text


# --- Refresh reuse compromise --------------------------------------------
# Dedicated 30s reuse-grace-window regression coverage (R4) lives in
# backend/tests/integration/test_oauth_refresh_grace_window.py.


def test_refresh_reuse_revokes_family_with_audit(page_service):
    """Reusing a refresh token after it has been rotated triggers family
    revoke (ADR A5 + H26). The response is 400 ``invalid_grant`` with the
    keyword ``invalid``; one ``oauth.refresh.reuse`` audit row is emitted
    carrying the family key, ``detected_via``, and family sizes."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="reuse-attack-AAA")
    import jwt

    family_key = jwt.decode(
        tokens["access_token"], options={"verify_signature": False}
    )["grant_id"]

    # Legitimate refresh first.
    first = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert first.status_code == 200
    assert _refresh_reuse_audit_rows(page_service) == []
    _expire_reuse_grace_window(page_service, tokens["refresh_token"])

    # Attacker replays the stolen prior refresh token.
    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert replay.status_code == 400, replay.text
    # The token endpoint speaks RFC 6749 §5.2, not the REST envelope (O1).
    assert replay.json()["error"] == "invalid_grant"
    assert "invalid" in replay.json()["error_description"]

    rows = _refresh_reuse_audit_rows(page_service)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["client_id"] == tokens["client_id"]
    assert payload["detected_via"] in {"revoked_at", "rotated_to_id"}
    assert payload["family_key"] == family_key
    assert payload["family_refresh_count"] >= 1
    assert payload["family_access_count"] >= 1


def test_refresh_reuse_with_widened_scope_still_revokes_and_audits(page_service):
    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="reuse-widened-scope")
    rotated = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    _expire_reuse_grace_window(page_service, tokens["refresh_token"])

    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
            "scope": "mcp:write",
        },
    )
    assert replay.status_code == 400, replay.text
    assert replay.json()["error"] == "invalid_grant"
    rows = _refresh_reuse_audit_rows(page_service)
    assert len(rows) == 1
    assert rows[0].payload["detected_via"] == "rotated_to_id"
    with page_service.session_factory() as session:
        successor = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(rotated.json()["refresh_token"])
            )
        )
        assert successor is not None
        assert successor.revoked_at is not None


def test_refresh_reuse_revokes_only_replayed_grant(page_service):
    """A stale token from grant A must not kill a separately authorized grant
    B that shares the same user and dynamic ``client_id``."""

    import jwt

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    grant_a = _seed_oauth_tokens(client, state="reuse-isolation-grant-A")
    grant_b = _seed_oauth_tokens(
        client,
        state="reuse-isolation-grant-B",
        client_id=grant_a["client_id"],
    )
    grant_a_key = UUID(
        jwt.decode(grant_a["access_token"], options={"verify_signature": False})[
            "grant_id"
        ]
    )
    grant_b_key = UUID(
        jwt.decode(grant_b["access_token"], options={"verify_signature": False})[
            "grant_id"
        ]
    )
    assert grant_a_key != grant_b_key

    rotation_a = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": grant_a["client_id"],
            "refresh_token": grant_a["refresh_token"],
        },
    )
    assert rotation_a.status_code == 200, rotation_a.text
    _expire_reuse_grace_window(page_service, grant_a["refresh_token"])

    replay_a = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": grant_a["client_id"],
            "refresh_token": grant_a["refresh_token"],
        },
    )
    assert replay_a.status_code == 400, replay_a.text
    assert replay_a.json()["error"] == "invalid_grant"

    with page_service.session_factory() as session:
        refresh_a = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    func.coalesce(
                        OAuthRefreshToken.grant_id,
                        OAuthRefreshToken.id,
                    )
                    == grant_a_key
                )
            ).all()
        )
        refresh_b = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    func.coalesce(
                        OAuthRefreshToken.grant_id,
                        OAuthRefreshToken.id,
                    )
                    == grant_b_key
                )
            ).all()
        )
        access_a = list(
            session.scalars(
                select(OAuthAccessTokenJti)
                .join(
                    OAuthRefreshToken,
                    OAuthRefreshToken.id == OAuthAccessTokenJti.refresh_token_id,
                )
                .where(
                    func.coalesce(
                        OAuthRefreshToken.grant_id,
                        OAuthRefreshToken.id,
                    )
                    == grant_a_key
                )
            ).all()
        )
        access_b = session.get(
            OAuthAccessTokenJti,
            _decode_access_jti(grant_b["access_token"]),
        )
    assert len(refresh_a) == 2
    assert all(row.revoked_at is not None for row in refresh_a)
    assert refresh_b
    assert all(row.revoked_at is None for row in refresh_b)
    assert len(access_a) == 2
    assert all(row.revoked_at is not None for row in access_a)
    assert access_b is not None
    assert access_b.revoked_at is None

    still_authenticated = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {grant_b['access_token']}"},
    )
    assert still_authenticated.status_code == 200, still_authenticated.text
    refresh_b_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": grant_b["client_id"],
            "refresh_token": grant_b["refresh_token"],
        },
    )
    assert refresh_b_response.status_code == 200, refresh_b_response.text


def test_refresh_reuse_revokes_entire_rotated_chain(page_service):
    """Replaying the middle member of a three-token chain revokes its live
    grandchild plus every access JTI linked to the stable grant."""

    import jwt

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="reuse-three-token-chain")
    family_key = UUID(
        jwt.decode(tokens["access_token"], options={"verify_signature": False})[
            "grant_id"
        ]
    )
    child = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert child.status_code == 200, child.text
    grandchild = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": child.json()["refresh_token"],
        },
    )
    assert grandchild.status_code == 200, grandchild.text
    _expire_reuse_grace_window(page_service, child.json()["refresh_token"])

    replay_child = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": child.json()["refresh_token"],
        },
    )
    assert replay_child.status_code == 400, replay_child.text

    with page_service.session_factory() as session:
        refresh_rows = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    func.coalesce(
                        OAuthRefreshToken.grant_id,
                        OAuthRefreshToken.id,
                    )
                    == family_key
                )
            ).all()
        )
        access_rows = list(
            session.scalars(
                select(OAuthAccessTokenJti)
                .join(
                    OAuthRefreshToken,
                    OAuthRefreshToken.id == OAuthAccessTokenJti.refresh_token_id,
                )
                .where(
                    func.coalesce(
                        OAuthRefreshToken.grant_id,
                        OAuthRefreshToken.id,
                    )
                    == family_key
                )
            ).all()
        )
    assert len(refresh_rows) == 3
    assert all(row.revoked_at is not None for row in refresh_rows)
    assert len(access_rows) == 3
    assert all(row.revoked_at is not None for row in access_rows)


def test_refresh_reuse_keeps_unrelated_legacy_null_grant_independent(
    page_service,
):
    """Two legacy rows with NULL ``grant_id`` resolve to their own IDs and
    remain independent even when they share the same user and client."""

    from backend.models.base import utc_now

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    legacy_a = _seed_oauth_tokens(client, state="legacy-null-grant-A")
    legacy_b = _seed_oauth_tokens(
        client,
        state="legacy-null-grant-B",
        client_id=legacy_a["client_id"],
    )
    jti_a = _decode_access_jti(legacy_a["access_token"])
    jti_b = _decode_access_jti(legacy_b["access_token"])
    with page_service.session_factory.begin() as session:
        access_a = session.get(OAuthAccessTokenJti, jti_a)
        access_b = session.get(OAuthAccessTokenJti, jti_b)
        assert access_a is not None
        assert access_b is not None
        refresh_a = session.get(OAuthRefreshToken, access_a.refresh_token_id)
        refresh_b = session.get(OAuthRefreshToken, access_b.refresh_token_id)
        assert refresh_a is not None
        assert refresh_b is not None
        refresh_a.grant_id = None
        refresh_b.grant_id = None
        refresh_a.revoked_at = utc_now()
        legacy_a_family_key = refresh_a.id
        legacy_b_family_key = refresh_b.id

    replay_a = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": legacy_a["client_id"],
            "refresh_token": legacy_a["refresh_token"],
        },
    )
    assert replay_a.status_code == 400, replay_a.text

    with page_service.session_factory() as session:
        refresh_a = session.get(OAuthRefreshToken, legacy_a_family_key)
        refresh_b = session.get(OAuthRefreshToken, legacy_b_family_key)
        access_a = session.get(OAuthAccessTokenJti, jti_a)
        access_b = session.get(OAuthAccessTokenJti, jti_b)
        assert refresh_a is not None
        assert refresh_b is not None
        assert access_a is not None
        assert access_b is not None
        assert refresh_a.grant_id is None
        assert refresh_a.revoked_at is not None
        assert access_a.revoked_at is not None
        assert refresh_b.grant_id is None
        assert refresh_b.revoked_at is None
        assert access_b.revoked_at is None

    still_authenticated = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {legacy_b['access_token']}"},
    )
    assert still_authenticated.status_code == 200, still_authenticated.text
    refresh_b_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": legacy_b["client_id"],
            "refresh_token": legacy_b["refresh_token"],
        },
    )
    assert refresh_b_response.status_code == 200, refresh_b_response.text


def test_legacy_null_parent_and_rotated_child_share_family(page_service):
    """A NULL-grant legacy parent and its child whose ``grant_id`` is the
    parent ID resolve to one family and are revoked together."""

    client = make_client(page_service)
    parent_tokens = _seed_oauth_tokens(client, state="legacy-parent-child")
    parent_jti = _decode_access_jti(parent_tokens["access_token"])
    with page_service.session_factory.begin() as session:
        parent_access = session.get(OAuthAccessTokenJti, parent_jti)
        assert parent_access is not None
        parent = session.get(
            OAuthRefreshToken,
            parent_access.refresh_token_id,
        )
        assert parent is not None
        parent.grant_id = None
        parent_id = parent.id

    child_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": parent_tokens["client_id"],
            "refresh_token": parent_tokens["refresh_token"],
        },
    )
    assert child_response.status_code == 200, child_response.text
    child_jti = _decode_access_jti(child_response.json()["access_token"])
    with page_service.session_factory() as session:
        child_access = session.get(OAuthAccessTokenJti, child_jti)
        assert child_access is not None
        child = session.get(OAuthRefreshToken, child_access.refresh_token_id)
        assert child is not None
        assert child.grant_id == parent_id
        child_id = child.id

    _expire_reuse_grace_window(page_service, parent_tokens["refresh_token"])
    replay_parent = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": parent_tokens["client_id"],
            "refresh_token": parent_tokens["refresh_token"],
        },
    )
    assert replay_parent.status_code == 400, replay_parent.text

    with page_service.session_factory() as session:
        parent = session.get(OAuthRefreshToken, parent_id)
        child = session.get(OAuthRefreshToken, child_id)
        parent_access = session.get(OAuthAccessTokenJti, parent_jti)
        child_access = session.get(OAuthAccessTokenJti, child_jti)
        assert parent is not None
        assert child is not None
        assert parent_access is not None
        assert child_access is not None
        assert parent.grant_id is None
        assert parent.revoked_at is not None
        assert child.grant_id == parent_id
        assert child.revoked_at is not None
        assert parent_access.revoked_at is not None
        assert child_access.revoked_at is not None


def test_refresh_reuse_revokes_access_jti_in_family(page_service):
    """After family revoke, the access JWTs minted from the family (both
    the original and any rotated successor) must be rejected. The /mcp
    transport returns 401 + the generic envelope; the REST API surfaces
    the underlying ``revoked`` keyword from the OAuth service, which we
    pin so a future refactor cannot silently downgrade the rejection to
    ``expired`` or ``invalid``."""

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    tokens = _seed_oauth_tokens(client, state="reuse-access-rev-BBB")

    # Rotate once so we have two access JWTs in the family (original +
    # rotated successor).
    rotation = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert rotation.status_code == 200
    new_access = rotation.json()["access_token"]
    _expire_reuse_grace_window(page_service, tokens["refresh_token"])

    # Trigger reuse compromise.
    reuse = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert reuse.status_code == 400

    # The original access JWT is revoked (/mcp 401 + REST 401 with keyword).
    response_old_mcp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response_old_mcp.status_code == 401
    response_old_rest = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response_old_rest.status_code == 401, response_old_rest.text
    assert "revoked" in response_old_rest.text

    # The rotated access JWT is also revoked (family cascade).
    response_new_mcp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {new_access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response_new_mcp.status_code == 401
    response_new_rest = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {new_access}"},
    )
    assert response_new_rest.status_code == 401, response_new_rest.text
    assert "revoked" in response_new_rest.text

    audit = _refresh_reuse_audit_rows(page_service)
    assert len(audit) == 1
    assert audit[0].payload["family_access_count"] >= 2


# --- Defense in depth + cache invalidation --------------------------------


def test_jti_denylist_cache_invalidation(page_service):
    """Immediately after family revoke (or any cache_clear path), the next
    bearer-decode for a revoked jti must hit the DB and return 401. The
    REST 401 carries the ``revoked`` keyword pin; the /mcp 401 confirms
    the cross-transport effect."""

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    tokens = _seed_oauth_tokens(client, state="cache-inval-CCC")

    # Prime the cache by hitting REST once (jti unrevoked here).
    primer = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert primer.status_code == 200, primer.text

    # Rotate then replay to trigger family revoke + cache invalidation.
    client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    _expire_reuse_grace_window(page_service, tokens["refresh_token"])
    reuse = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert reuse.status_code == 400

    # Same access JWT that just returned 200 now 401s without delay
    # because revoke_refresh_token_grant_family cleared the cache.
    after = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert after.status_code == 401, after.text
    assert "revoked" in after.text


def test_access_jwt_without_jti_claim_is_rejected(page_service):
    """Defense in depth (ADR A5): a JWT whose ``jti`` is missing or whose
    jti row was never persisted must be rejected with the ``revoked``
    keyword. We forge a JWT manually using the live signing key to bypass
    ``_issue_tokens``."""

    import jwt as pyjwt

    from backend.services.oauth_service import (
        JWT_KID,
        _private_key_pem,
    )

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    tokens = _seed_oauth_tokens(client, state="no-jti-DDD")

    # Decode the legit JWT to copy its claims, then re-sign without jti.
    legit_payload = pyjwt.decode(
        tokens["access_token"], options={"verify_signature": False}
    )
    legit_payload.pop("jti", None)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
    )
    forged = pyjwt.encode(
        legit_payload,
        _private_key_pem(settings),
        algorithm="RS256",
        headers={"kid": JWT_KID},
    )

    response = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert response.status_code == 401, response.text
    assert "revoked" in response.text


def test_consent_revoke_invalidates_live_access_jwt(page_service):
    """Disconnecting a client from the integrations UI must kill the
    already-minted access JWT immediately, not leave it valid until exp:
    ``revoke_consent`` fans out to the jti denylist exactly like the
    refresh-reuse family revoke and RFC 7009 ``/oauth/revoke`` paths,
    and clears the denylist cache so the rejection is zero-latency."""

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    tokens = _seed_oauth_tokens(client, state="consent-revoke-access-EEE")

    # Prime the denylist cache with the still-valid jti so the test also
    # proves revoke_consent invalidates the cache, not just the DB row.
    primer = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert primer.status_code == 200, primer.text

    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": tokens["client_id"]},
    )
    assert revoked.status_code == 200, revoked.text

    jti = _decode_access_jti(tokens["access_token"])
    with page_service.session_factory() as session:
        row = session.get(OAuthAccessTokenJti, jti)
        assert row is not None
        assert row.revoked_at is not None, (
            "consent revoke must set revoked_at on the access jti row"
        )

    after_mcp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert after_mcp.status_code == 401
    after_rest = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert after_rest.status_code == 401, after_rest.text
    assert "revoked" in after_rest.text


def test_consent_revoke_revokes_all_grants_for_user_client(page_service):
    """Consent revocation is deliberately wider than reuse handling: every
    grant for the user/client pair loses both refresh and access tokens."""

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    grant_a = _seed_oauth_tokens(client, state="consent-wide-grant-A")
    grant_b = _seed_oauth_tokens(
        client,
        state="consent-wide-grant-B",
        client_id=grant_a["client_id"],
    )
    outside_grant = _seed_oauth_tokens(
        client,
        state="consent-wide-outside-grant",
    )

    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": grant_a["client_id"]},
    )
    assert revoked.status_code == 200, revoked.text

    with page_service.session_factory() as session:
        refresh_rows = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.client_id == grant_a["client_id"]
                )
            ).all()
        )
        access_rows = list(
            session.scalars(
                select(OAuthAccessTokenJti).where(
                    OAuthAccessTokenJti.client_id == grant_a["client_id"]
                )
            ).all()
        )
    assert len(refresh_rows) == 2
    assert all(row.revoked_at is not None for row in refresh_rows)
    assert len(access_rows) == 2
    assert all(row.revoked_at is not None for row in access_rows)
    with page_service.session_factory() as session:
        outside_refresh = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(outside_grant["refresh_token"])
            )
        )
        outside_access = session.get(
            OAuthAccessTokenJti,
            _decode_access_jti(outside_grant["access_token"]),
        )
        assert outside_refresh is not None
        assert outside_refresh.revoked_at is None
        assert outside_access is not None
        assert outside_access.revoked_at is None

    for grant in (grant_a, grant_b):
        refresh = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": grant["client_id"],
                "refresh_token": grant["refresh_token"],
            },
        )
        assert refresh.status_code == 400, refresh.text
        bearer = client.get(
            f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
            headers={"Authorization": f"Bearer {grant['access_token']}"},
        )
        assert bearer.status_code == 401, bearer.text
        assert "revoked" in bearer.text


def test_membership_loss_revokes_workspace_access_but_keeps_other_workspace_live(
    page_service,
):
    """P10.3 T3 (D-01v3-g) rewrite of the retired "membership loss revokes
    only grants for that workspace" property.

    Under D-01v2/D-01v3, ``OAuthRefreshToken`` carries no ``workspace_id`` at
    all any more (section 2.3) -- a refresh token is a (user, client) fact,
    not a (user, client, workspace) one, so refresh-token-level revocation
    can no longer express "only this workspace". Workspace isolation moved to
    REQUEST TIME instead: ``backend.policy.auth._authenticate_oauth_token``
    reads the credential's live allowed-workspace set from ``oauth_consents``
    (``removed_at IS NULL``) on every request, and
    ``trust_boundaries.actor_with_target_workspace_scope`` denies any target
    workspace outside that set. Losing membership in one workspace deletes
    only that workspace's ``oauth_consents`` row
    (``revoke_workspace_oauth_consent``, called from
    ``WorkspaceMembershipService.remove_member_by_id``) -- no refresh/access
    token row is touched eagerly any more (its docstring: "the workspace
    becomes unreachable via OAuth on the very next request with no
    token/family revocation needed"). So the new-model equivalent of the old
    assertion is: the SAME still-valid access token can no longer reach the
    removed workspace, while it still reaches a workspace the (user, client)
    pair separately holds live consent for, and the refresh grant itself
    keeps working (consent existence is checked per-client now, not
    per-workspace).

    Building two independent per-workspace ``oauth_consents`` rows under ONE
    client requires the authorize checklist's explicit multi-select
    (``workspace_ids=[a, b]`` in one POST) -- the ``_seed_oauth_tokens``
    reconnect path used elsewhere in this file reuses whichever consent
    already exists for the client and does not create a second per-workspace
    row (``has_existing_consent`` is any-workspace now, section 2.3), so it
    cannot build this fixture.
    """

    from urllib.parse import parse_qs, urlparse

    from backend.models.oauth import OAuthConsent
    from backend.models.user import User
    from backend.models.workspace import Workspace, WorkspaceMembership
    from backend.services.personal_workspace_service import ensure_personal_workspace
    from backend.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )

    client = make_client(page_service)
    default_workspace_id = _current_workspace_id(client)
    outside_workspace_id, _ = _seed_second_workspace(
        page_service,
        slug="membership-family-outside",
    )

    register = client.post(
        "/oauth/register",
        json=_valid_payload(client_name="Membership Loss Multi-Workspace"),
    )
    assert register.status_code == 201, register.text
    client_id = register.json()["client_id"]
    verifier = "v" * 64
    state = _oauth_state("membership-loss-multi")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": state,
    }
    get_resp = client.get("/oauth/authorize", params=params)
    assert get_resp.status_code == 200, get_resp.text
    authorize = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": [default_workspace_id, outside_workspace_id],
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303, authorize.text
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    access_token = token.json()["access_token"]
    refresh_token = token.json()["refresh_token"]

    with page_service.session_factory() as session:
        consent_workspaces = set(
            session.scalars(
                select(OAuthConsent.workspace_id).where(
                    OAuthConsent.client_id == client_id,
                    OAuthConsent.removed_at.is_(None),
                )
            ).all()
        )
    assert consent_workspaces == {
        UUID(default_workspace_id),
        UUID(outside_workspace_id),
    }

    # Sanity: the token reaches both workspaces before membership loss.
    assert (
        client.get(
            f"/api/v1/workspaces/{default_workspace_id}/context/pages",
            headers={"Authorization": f"Bearer {access_token}"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/workspaces/{outside_workspace_id}/context/pages",
            headers={"Authorization": f"Bearer {access_token}"},
        ).status_code
        == 200
    )

    # Workspace removal now protects the last active owner. Keep that invariant
    # active while this fixture isolates the independent OAuth consent-row
    # removal behavior.
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None
        replacement_owner = User(
            email="membership-replacement-owner@example.com",
            password_hash="not-used-by-this-test",
            display_name="Membership replacement owner",
            status="active",
        )
        session.add(replacement_owner)
        session.flush()
        ensure_personal_workspace(session, user=replacement_owner)
        session.add(
            WorkspaceMembership(
                user_id=replacement_owner.id,
                workspace_id=workspace.id,
                role="owner",
            )
        )

    removed = WorkspaceMembershipService(
        session_factory=page_service.session_factory
    ).remove_member(
        workspace_slug="default",
        email="test@example.com",
    )
    assert removed is True

    with page_service.session_factory() as session:
        consent_workspaces_after = set(
            session.scalars(
                select(OAuthConsent.workspace_id).where(
                    OAuthConsent.client_id == client_id,
                    OAuthConsent.removed_at.is_(None),
                )
            ).all()
        )
        refresh_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(refresh_token)
            )
        )
        assert refresh_row is not None
        assert refresh_row.revoked_at is None, (
            "membership loss must not eagerly revoke the refresh row -- only "
            "the removed workspace's oauth_consents row is deleted"
        )
    assert consent_workspaces_after == {UUID(outside_workspace_id)}, (
        "revoke_workspace_oauth_consent must delete only the removed "
        "workspace's oauth_consents row"
    )

    # Same access token, same jti: the removed workspace is now denied at
    # request time (token_workspace_denied)...
    denied = client.get(
        f"/api/v1/workspaces/{default_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert denied.status_code != 200, denied.text
    # ...while the still-consented workspace remains reachable.
    still_reachable = client.get(
        f"/api/v1/workspaces/{outside_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert still_reachable.status_code == 200, still_reachable.text

    # The (user, client) pair still has a live consent (for the outside
    # workspace), so the refresh grant itself keeps working -- consent
    # existence is a per-client fact now, not a per-workspace one.
    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    assert refreshed.status_code == 200, refreshed.text


def test_user_deactivation_revokes_only_that_users_grant_families(page_service):
    from backend.models.base import utc_now
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership
    from backend.services.credential_hashing import CredentialHasher
    from backend.services.user_admin_service import UserAdminService

    client = make_client(page_service)
    affected_a = _seed_oauth_tokens(client, state="deactivate-family-a")
    affected_b = _seed_oauth_tokens(client, state="deactivate-family-b")
    outsider_grant_id = uuid4()
    affected_user_id = None
    outsider_refresh_id = None
    outsider_jti = f"o5-outsider-{uuid4()}"

    with page_service.session_factory.begin() as session:
        active_user = session.scalars(
            select(User).where(User.email == "test@example.com")
        ).one()
        affected_user_id = active_user.id
        workspace = session.scalars(select(Workspace)).first()
        assert workspace is not None
        outsider = User(
            email="o5-outsider@example.com",
            password_hash="not-used-by-this-test",
            display_name="O5 outsider",
            status="active",
        )
        session.add(outsider)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=outsider.id,
            workspace_id=workspace.id,
            role="viewer",
        )
        # P10.3 T3: OAuthRefreshToken carries no workspace_id column any
        # more (section 2.3) -- the row is a (user, client) fact only.
        outsider_refresh = OAuthRefreshToken(
            token_lookup_hash=f"o5-outsider-{uuid4()}",
            grant_id=outsider_grant_id,
            hashed_token=None,
            user_id=outsider.id,
            client_id=affected_a["client_id"],
            scope="mcp:read",
            expires_at=utc_now() + timedelta(days=180),
        )
        session.add(outsider_refresh)
        session.flush()
        outsider_refresh_id = outsider_refresh.id
        session.add(
            OAuthAccessTokenJti(
                jti=outsider_jti,
                refresh_token_id=outsider_refresh.id,
                user_id=outsider.id,
                client_id=affected_a["client_id"],
                expires_at=utc_now() + timedelta(hours=1),
            )
        )
        assert active_user.status == "active"

    changed = UserAdminService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    ).deactivate_user(email="test@example.com")
    assert changed is True

    with page_service.session_factory() as session:
        assert affected_user_id is not None
        affected_rows = list(
            session.scalars(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.user_id == affected_user_id
                )
            ).all()
        )
        assert len(affected_rows) == 2
        assert all(row.revoked_at is not None for row in affected_rows)
        outsider_refresh = session.get(OAuthRefreshToken, outsider_refresh_id)
        outsider_access = session.get(OAuthAccessTokenJti, outsider_jti)
        assert outsider_refresh is not None
        assert outsider_refresh.grant_id == outsider_grant_id
        assert outsider_refresh.revoked_at is None
        assert outsider_access is not None
        assert outsider_access.revoked_at is None

    refused = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": affected_b["client_id"],
            "refresh_token": affected_b["refresh_token"],
        },
    )
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant"
    with page_service.session_factory() as session:
        event = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "oauth.refresh.refused")
            .order_by(AuditEvent.created_at.desc())
        ).first()
        assert event is not None
        assert event.payload["reason"] == "user_inactive"


# =========================================================================
# Task 3.5 — RFC 7009 /oauth/revoke endpoint (ADR A4, A5, A11)
# =========================================================================


def _issue_pat(client: TestClient, *, name: str = "Revoke Test PAT") -> str:
    """Mint a personal API token for the seeded user. Used as the PAT
    bearer in /oauth/revoke ADR A4 case 1 tests."""

    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": _current_workspace_id(client)}],
            "name": name,
            "scopes": ["mcp:read"],
        },
    )
    assert issued.status_code == 201, issued.text
    return issued.json()["data"]["plain_token"]


def _issue_pat_for_new_user(page_service, *, email: str) -> str:
    """Mint a PAT for a SECOND, freshly signed-up user.

    Same shape as the wrong-user guard at
    ``test_revoke_wrong_user_pat_silent_no_op``: a separate TestClient over
    the same page_service so the new user gets their own session cookie.
    """

    second = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                signup_enabled=True,
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        second,
        page_service.session_factory,
        email=email,
    )
    return _issue_pat(second, name=f"PAT for {email}")


def _revoke_audit_rows(page_service, action: str) -> list[AuditEvent]:
    with page_service.session_factory() as session:
        return list(
            session.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()
        )


# --- Positive controls ----------------------------------------------------


def test_revoke_unknown_returns_200(page_service):
    """RFC 7009 §2.2: an unknown token still returns 200 so the endpoint
    does not function as a token-existence oracle. No audit row is
    emitted because nothing was revoked."""

    client = make_client(page_service)
    pat = _issue_pat(client)

    response = client.post(
        "/oauth/revoke",
        data={"token": "rt_does_not_exist_anywhere"},
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert response.status_code == 200
    assert response.json() == {}
    assert _revoke_audit_rows(page_service, "oauth.revoke.success") == []
    assert _revoke_audit_rows(page_service, "oauth.token.access_revoked") == []


def test_revoke_refresh_token_by_user_pat(page_service):
    """ADR A4 case 1: PAT bearer revokes the user's refresh token.
    Deliberate security fan-out kills every still-live access JWT for the
    (user, client) pair; one ``oauth.revoke.success`` audit row is emitted
    carrying ``family_access_count >= 1``."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-by-pat-AAA")
    pat = _issue_pat(client)

    response = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
        },
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert response.status_code == 200, response.text

    # The refresh token is now revoked; a subsequent refresh attempt
    # returns 400 invalid_grant.
    after = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert after.status_code == 400
    assert after.json()["error"] == "invalid_grant"

    # Audit row + family fan-out.
    rows = _revoke_audit_rows(page_service, "oauth.revoke.success")
    assert len(rows) == 1
    assert rows[0].payload["token_type"] == "refresh_token"
    assert rows[0].payload["family_access_count"] >= 1


def test_revoke_refresh_token_by_client_native(page_service):
    """ADR A4 case 3: a native (``token_endpoint_auth_method=none``)
    client revokes its own token using only the ``client_id`` form
    field. No bearer required. We clear the session cookie set up by
    ``authenticate_test_client`` so the bearer/session path doesn't
    short-circuit before client_id form matching."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-native-BBB")
    # Drop the session cookie so this exercises ADR A4 case 3 (native
    # client_id self-revoke) rather than case 2 (session bearer).
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": tokens["client_id"],
        },
    )
    assert response.status_code == 200, response.text
    rows = _revoke_audit_rows(page_service, "oauth.revoke.success")
    assert len(rows) == 1
    assert rows[0].actor_kind == "oauth_client"
    assert rows[0].actor_id == tokens["client_id"]


def test_revoke_refresh_token_by_oauth_bearer_same_workspace(page_service):
    """OAuth JWT bearers may revoke tokens for the same user/client/workspace.

    This pins the documented bearer path separately from the session-cookie
    path by clearing cookies before calling /oauth/revoke.
    """

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-oauth-same-AAA")
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
        },
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 200, response.text

    after = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert after.status_code == 400

    rows = _revoke_audit_rows(page_service, "oauth.revoke.success")
    assert len(rows) == 1
    assert rows[0].actor_kind == "oauth_client"
    assert rows[0].actor_id == tokens["client_id"]


def test_revoke_access_token_invalidates_via_jti_denylist(page_service):
    """Revoking an access JWT writes ``revoked_at`` on its jti row +
    clears the denylist cache. The next REST request with the same JWT
    must 401 carrying the ``revoked`` keyword (Task 3.4 cross-effect)."""

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    tokens = _seed_oauth_tokens(client, state="revoke-access-CCC")
    pat = _issue_pat(client)

    # Sanity: token works before revoke.
    primer = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert primer.status_code == 200, primer.text

    revoke = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["access_token"],
            "token_type_hint": "access_token",
        },
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert revoke.status_code == 200

    # Post-revoke: same JWT is now 401 with the ``revoked`` keyword.
    after = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert after.status_code == 401, after.text
    assert "revoked" in after.text

    rows = _revoke_audit_rows(page_service, "oauth.token.access_revoked")
    assert len(rows) == 1
    assert rows[0].payload["token_type"] == "access_token"
    assert rows[0].payload["jti"] == _decode_access_jti(tokens["access_token"])
    assert rows[0].workspace_id is None  # ADR A11 NULL for access revoke


def test_oauth_bearer_can_revoke_refresh_token_for_other_workspace_same_client(
    page_service,
):
    """P10.3 T3 (D-01v2/D-01v3, section 2.3) intentionally widens RFC 7009
    revoke scope: the family unit is (user, client) now, not (user, client,
    workspace) -- ``OAuthRefreshToken`` carries no ``workspace_id`` column at
    all any more, and ``revoke_token``'s former ``revoker_workspace_ids``
    restriction parameter is gone entirely (see the ``RevokeActor`` and
    ``revoke_token`` docstrings in ``backend/api/oauth.py`` /
    ``oauth_service.py``). An oauth_jwt bearer for (user, client) may
    therefore revoke ANY refresh token grant issued to that SAME (user,
    client) pair, even one originally authorized against a DIFFERENT
    workspace -- a workspace-scoped access reduction is a separate mechanism
    now (``revoke_workspace_oauth_consent``, exercised by
    ``test_membership_loss_revokes_workspace_access_but_keeps_other_workspace_live``
    above), not something RFC 7009 revoke can express or attempts any more.

    This replaces the retired
    ``test_oauth_bearer_cannot_revoke_refresh_token_for_other_workspace``,
    which pinned the OLD single-workspace-claim deny behavior -- that
    mechanism no longer exists. Cross-USER and cross-CLIENT isolation remain
    enforced and are covered separately by
    ``test_revoke_wrong_user_pat_silent_no_op`` and
    ``test_oauth_bearer_cannot_revoke_another_clients_access_token`` below.
    """

    client = make_client(page_service)
    default_workspace_id = _current_workspace_id(client)
    revoker_tokens = _seed_oauth_tokens(
        client,
        state="revoke-oauth-default-BBB",
        workspace_id=default_workspace_id,
    )
    other_workspace_id, _other_slug = _seed_second_workspace(
        page_service,
        slug="oauth-revoke-cross-workspace",
    )
    # Same client_id as revoker_tokens -- has_existing_consent is any-workspace
    # now (section 2.3), so this reuses the SAME (user, client) consent group
    # via the reconnect path rather than adding a second per-workspace row;
    # either way both grants are separate refresh-token rows sharing one
    # (user, client) family for RFC 7009 purposes.
    target_tokens = _seed_oauth_tokens(
        client,
        state="revoke-oauth-cross-CCC",
        client_id=revoker_tokens["client_id"],
        workspace_id=other_workspace_id,
    )
    assert target_tokens["refresh_token"] != revoker_tokens["refresh_token"]
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={
            "token": target_tokens["refresh_token"],
            "token_type_hint": "refresh_token",
        },
        headers={"Authorization": f"Bearer {revoker_tokens['access_token']}"},
    )
    assert response.status_code == 200, response.text

    with page_service.session_factory() as session:
        target_row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash
                == token_hash(target_tokens["refresh_token"])
            )
        )
        assert target_row is not None
        assert target_row.revoked_at is not None, (
            "an oauth_jwt bearer must be able to revoke a refresh token for "
            "the same (user, client) pair regardless of which workspace it "
            "was originally authorized against (P10.3 T3 section 2.3)"
        )

    rows = _revoke_audit_rows(page_service, "oauth.revoke.success")
    assert len(rows) == 1
    assert rows[0].actor_kind == "oauth_client"
    assert rows[0].actor_id == revoker_tokens["client_id"]


@pytest.mark.parametrize("target", ["self", "sibling"])
def test_oauth_bearer_can_revoke_access_token_in_consented_workspace(
    page_service, target
):
    """An oauth_jwt bearer must actually be able to revoke an access JWT.

    P10.3 T3 (D-01v2/D-01v3): ``_revoke_as_access`` matches only
    ``jti_row.user_id`` and ``jti_row.client_id`` against the bearer's own
    claims -- no workspace comparison exists any more, since access JWTs
    carry no workspace claim (D-01v3-d) and ``OAuthAccessTokenJti`` has no
    ``workspace_id`` column. That symmetric (user, client) match is what lets
    an access revoke actually succeed instead of silently no-op'ing per RFC
    7009 §2.2.

    The ``sibling`` cell revokes a DIFFERENT access JWT minted for the same
    (user, client) pair, which proves two things the ``self`` cell cannot:
    the fix is not a self-revoke-only special case, and the access path
    revokes EXACTLY ONE jti -- unlike the refresh path it must not fan out
    across the (user, client) radius, so the revoker's own token stays live.
    """

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    revoker_tokens = _seed_oauth_tokens(
        client, state=f"revoke-oauth-access-{target}-III"
    )
    if target == "sibling":
        # Same client, same default workspace -> the second authorize round
        # takes _seed_oauth_tokens' 302 re-consent branch, so one live
        # OAuthConsent(user, client, default) covers both tokens.
        target_tokens = _seed_oauth_tokens(
            client,
            state=f"revoke-oauth-access-{target}-MMM",
            client_id=revoker_tokens["client_id"],
        )
    else:
        target_tokens = revoker_tokens
    jti = _decode_access_jti(target_tokens["access_token"])
    revoker_jti = _decode_access_jti(revoker_tokens["access_token"])

    # Cookies go FIRST so neither the primer nor the revoke can be answered
    # by the seeded browser session instead of the bearer under test.
    client.cookies.clear()
    # The target JWT is live before the revoke, so a post-revoke difference
    # cannot be an already-dead token.
    primer = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {target_tokens['access_token']}"},
    )
    assert primer.status_code == 200, primer.text

    response = client.post(
        "/oauth/revoke",
        data={
            "token": target_tokens["access_token"],
            "token_type_hint": "access_token",
        },
        headers={"Authorization": f"Bearer {revoker_tokens['access_token']}"},
    )
    assert response.status_code == 200, response.text

    with page_service.session_factory() as session:
        jti_row = session.get(OAuthAccessTokenJti, jti)
        assert jti_row is not None
        assert jti_row.revoked_at is not None, (
            "an oauth_jwt bearer revoking an access token in a consented "
            "workspace must mark the jti row revoked, not silently no-op"
        )
        if target == "sibling":
            revoker_row = session.get(OAuthAccessTokenJti, revoker_jti)
            assert revoker_row is not None
            assert revoker_row.revoked_at is None, (
                "the access path revokes exactly one jti -- it must not fan "
                "out across the (user, client) radius and kill the revoker's "
                "own token"
            )
    # The same request that just returned 200 is now refused, so the revoke
    # reached the live denylist and not only the row.
    after = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {target_tokens['access_token']}"},
    )
    assert after.status_code == 401, after.text
    assert "revoked" in after.text

    rows = _revoke_audit_rows(page_service, "oauth.token.access_revoked")
    assert len(rows) == 1
    assert rows[0].payload["token_type"] == "access_token"
    assert rows[0].payload["jti"] == jti
    assert rows[0].actor_kind == "oauth_client"
    assert rows[0].actor_id == revoker_tokens["client_id"]
    assert rows[0].workspace_id is None  # ADR A11 NULL for access revoke


def test_oauth_bearer_can_revoke_access_token_across_workspaces_same_client(
    page_service,
):
    """Counterpart to
    ``test_oauth_bearer_can_revoke_refresh_token_for_other_workspace_same_client``
    for the access-token path.

    P10.3 T3 (D-01v2/D-01v3): ``_revoke_as_access`` matches only
    ``jti_row.user_id`` / ``jti_row.client_id`` against the bearer's own
    claims -- no workspace comparison exists any more (access JWTs carry no
    workspace claim, D-01v3-d, and ``OAuthAccessTokenJti`` has no
    ``workspace_id`` column). An oauth_jwt bearer may therefore revoke an
    access token minted for a DIFFERENT workspace under the same (user,
    client) pair.

    This replaces the retired
    ``test_oauth_bearer_cannot_revoke_access_token_for_unconsented_workspace``,
    which pinned the OLD deny behavior for exactly this scenario -- that
    mechanism no longer exists. Cross-USER and cross-CLIENT isolation on the
    access path remain enforced and are covered separately by
    ``test_other_users_pat_cannot_revoke_access_token`` and
    ``test_oauth_bearer_cannot_revoke_another_clients_access_token`` below.
    """

    client = make_client(page_service)
    revoker_tokens = _seed_oauth_tokens(
        client,
        state="revoke-oauth-access-default-JJJ",
        workspace_id=_current_workspace_id(client),
    )
    other_workspace_id, _other_slug = _seed_second_workspace(
        page_service,
        slug="oauth-revoke-access-cross-workspace",
    )
    target_tokens = _seed_oauth_tokens(
        client,
        state="revoke-oauth-access-cross-KKK",
        client_id=revoker_tokens["client_id"],
        workspace_id=other_workspace_id,
    )
    target_jti = _decode_access_jti(target_tokens["access_token"])
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={
            "token": target_tokens["access_token"],
            "token_type_hint": "access_token",
        },
        headers={"Authorization": f"Bearer {revoker_tokens['access_token']}"},
    )
    assert response.status_code == 200, response.text

    with page_service.session_factory() as session:
        jti_row = session.get(OAuthAccessTokenJti, target_jti)
        assert jti_row is not None
        assert jti_row.revoked_at is not None, (
            "an oauth_jwt bearer must be able to revoke an access token for "
            "the same (user, client) pair regardless of which workspace it "
            "was minted for (P10.3 T3 -- no workspace claim on access JWTs "
            "any more, D-01v3-d)"
        )

    rows = _revoke_audit_rows(page_service, "oauth.token.access_revoked")
    assert len(rows) == 1
    assert rows[0].payload["token_type"] == "access_token"
    assert rows[0].payload["jti"] == target_jti
    assert rows[0].workspace_id is None  # ADR A11 NULL for access revoke


# P10.3 T3 (D-01v2/D-01v3, section 2.3) retired the former
# revoker_workspace_ids restriction on OAuthService.revoke_token entirely --
# there is no more "bearer has no bound workspace" tri-state to guard, since
# revoke_token no longer accepts (or resolves) a workspace scope at all: RFC
# 7009 revoke always kills the whole (user, client) family now. The retired
# test_oauth_bearer_cannot_revoke_refresh_token_for_other_workspace and
# test_oauth_bearer_cannot_revoke_access_token_for_unconsented_workspace
# above are replaced by
# test_oauth_bearer_can_revoke_refresh_token_for_other_workspace_same_client
# and test_oauth_bearer_can_revoke_access_token_across_workspaces_same_client,
# which pin the new, intentionally-widened behavior instead. The retired
# test_revoke_token_denies_when_bearer_has_no_bound_workspace called
# revoke_token(..., revoker_workspace_ids=()), which now raises TypeError
# (the parameter is gone) -- there is no remaining fail-closed contract at
# that call site to test; a workspace-scoped access reduction is expressed
# purely via revoke_workspace_oauth_consent instead (see
# test_membership_loss_revokes_workspace_access_but_keeps_other_workspace_live
# above).


def test_other_users_pat_cannot_revoke_access_token(page_service):
    """GUARD (documentation, not a regression): cross-USER ownership.

    A PAT bearer is an unrestricted revoker -- ``workspace_ids is None`` --
    so the only thing standing between user B's PAT and user A's access JWT
    is ``_revoke_as_access``'s ``jti_row.user_id != revoker_user_id`` check.
    Nothing else in this file exercises that check on the ACCESS path, so a
    future refactor of the scope gate could delete it unnoticed.
    """

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-otheruser-OOO")
    target_jti = _decode_access_jti(tokens["access_token"])
    other_pat = _issue_pat_for_new_user(
        page_service, email="revoke-guard-other-user@example.com"
    )

    response = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["access_token"],
            "token_type_hint": "access_token",
        },
        headers={"Authorization": f"Bearer {other_pat}"},
    )
    # Wrong-user oracle avoidance: 200 as if the token were unknown.
    assert response.status_code == 200, response.text
    assert _revoke_audit_rows(page_service, "oauth.token.access_revoked") == []

    with page_service.session_factory() as session:
        jti_row = session.get(OAuthAccessTokenJti, target_jti)
        assert jti_row is not None
        assert jti_row.revoked_at is None


def test_oauth_bearer_cannot_revoke_another_clients_access_token(page_service):
    """GUARD (documentation, not a regression): cross-CLIENT ownership.

    Same user, same workspace, same live consent -- so the workspace-scope
    gate cannot be what denies this. Two OAuth clients are two credentials:
    client C1's bearer must not reach an access JWT issued to C2, and only
    ``_revoke_as_access``'s ``jti_row.client_id != revoker_client_id`` check
    says so.
    """

    client = make_client(page_service)
    revoker_tokens = _seed_oauth_tokens(client, state="revoke-otherclient-PPP")
    other_client_tokens = _seed_oauth_tokens(
        client, state="revoke-otherclient-QQQ"
    )
    assert other_client_tokens["client_id"] != revoker_tokens["client_id"]
    target_jti = _decode_access_jti(other_client_tokens["access_token"])
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={
            "token": other_client_tokens["access_token"],
            "token_type_hint": "access_token",
        },
        headers={"Authorization": f"Bearer {revoker_tokens['access_token']}"},
    )
    assert response.status_code == 200, response.text
    assert _revoke_audit_rows(page_service, "oauth.token.access_revoked") == []

    with page_service.session_factory() as session:
        jti_row = session.get(OAuthAccessTokenJti, target_jti)
        assert jti_row is not None
        assert jti_row.revoked_at is None


def test_revoke_refresh_invalidates_family_access_jwt(page_service):
    """Cross-effect: explicitly revoking a refresh token cascades to every
    access JWT in the (user, client) security radius. The original JWT (issued
    alongside the revoked refresh) must 401 carrying ``revoked``."""

    client = make_client(page_service)
    _check_workspace_id = _current_workspace_id(client)
    tokens = _seed_oauth_tokens(client, state="revoke-cascade-DDD")
    pat = _issue_pat(client)

    revoke = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
        },
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert revoke.status_code == 200

    after = client.get(
        f"/api/v1/workspaces/{_check_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert after.status_code == 401, after.text
    assert "revoked" in after.text


def test_metadata_advertises_revocation_endpoint(page_service):
    """RFC 7009 + RFC 8414: the authorization-server metadata document
    must advertise ``revocation_endpoint`` so native discoverers don't
    have to hardcode the path."""

    client = make_client(page_service)
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert "revocation_endpoint" in meta
    assert meta["revocation_endpoint"].endswith("/oauth/revoke")
    assert meta.get("revocation_endpoint_auth_methods_supported") == ["none"]


# --- Negative tests (ADR A6 keyword + paired positive controls) ----------


def test_revoke_auth_fail_returns_401(page_service):
    """RFC 7009 §2.1: no bearer + no matching client_id → 401 with a
    WWW-Authenticate challenge."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-noauth-EEE")
    # Clear the session cookie so the request is fully unauthenticated.
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"]},
    )
    assert response.status_code == 401, response.text
    assert "WWW-Authenticate" in response.headers
    assert response.headers["WWW-Authenticate"].lower().startswith("bearer")
    assert response.json()["error_code"] == "unauthorized"


def test_revoke_wrong_client_id_returns_401_without_oracle(page_service):
    """ADR A4 case 3 wrong-input oracle guard: if ``client_id`` form
    does not match the token's client_id and no bearer is supplied, the
    endpoint must NOT reveal token existence. It returns the same 401
    challenge as a fully missing-auth call."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-wrongclient-FFF")
    client.cookies.clear()

    response = client.post(
        "/oauth/revoke",
        data={
            "token": tokens["refresh_token"],
            "client_id": "dyn_not_the_real_client",
        },
    )
    assert response.status_code == 401, response.text
    # The refresh token is still alive (no audit row).
    assert _revoke_audit_rows(page_service, "oauth.revoke.success") == []


def test_revoke_wrong_user_pat_silent_no_op(page_service):
    """ADR A4 case 1 wrong-user guard: a PAT belonging to user A cannot
    revoke user B's token. The endpoint returns 200 (RFC 7009 §2.2 no
    token-existence oracle) but does NOT emit an audit row and does NOT
    actually mark the token revoked."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-wronguser-GGG")

    # Authenticate a second user and mint a PAT for them.
    second = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                signup_enabled=True,
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        second,
        page_service.session_factory,
        email="user-b@example.com",
    )
    foreign_pat = _issue_pat(second, name="user-b PAT")

    response = client.post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {foreign_pat}"},
    )
    # Silent success per RFC 7009 §2.2 — no oracle leak — but the token
    # is NOT actually revoked because the PAT belongs to a different user.
    assert response.status_code == 200
    assert _revoke_audit_rows(page_service, "oauth.revoke.success") == []

    # The original refresh token still works.
    refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refresh.status_code == 200, refresh.text


def test_revoke_idempotent_second_call_returns_200(page_service):
    """RFC 7009 §2.2 idempotency: a second revoke of the same token is
    a 200 no-op. Positive control paired with the wrong-user negative
    above — we exercise the same 200 status code through two different
    paths so the assertion is meaningful."""

    client = make_client(page_service)
    tokens = _seed_oauth_tokens(client, state="revoke-idem-HHH")
    pat = _issue_pat(client)

    first = client.post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert first.status_code == 200
    second = client.post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {pat}"},
    )
    assert second.status_code == 200

    # One audit row from the first call; the second call is a no-op.
    rows = _revoke_audit_rows(page_service, "oauth.revoke.success")
    assert len(rows) == 1


def test_handoff_page_script_is_nonce_matched_under_security_headers(page_service):
    """Without a matching nonce the countdown never runs and the user is
    stranded on a page whose only way forward is a button they may not read."""

    client = make_client(page_service, security_headers=True)
    registered = client.post("/oauth/register", json=_valid_payload())
    assert registered.status_code == 201, registered.text
    params = _authorize_params(
        registered.json()["client_id"], state=_CONSENT_STATE_GOOD
    )
    client.get("/oauth/authorize", params=params)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    response = client.post(
        "/oauth/authorize",
        data={**params, "workspace_ids": [workspace_id], "decision": "allow"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce, csp
    assert f'<script nonce="{nonce.group(1)}">' in response.text
    # The forward is a top-level navigation, so nothing had to be widened.
    assert "form-action 'self';" in csp
