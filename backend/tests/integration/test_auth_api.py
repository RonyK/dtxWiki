from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.catalog.mcp_apps import get_mcp_app_catalog_repository
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.password_recovery_service import PasswordRecoveryService
from backend.services.session_service import (
    SessionService,
    SessionTokenCodec,
    build_session_token_codec,
)


def make_client(page_service, **overrides) -> TestClient:
    settings_values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "test-session-secret-for-auth-api-tests",
        "session_ttl_seconds": 3600,
    }
    settings_values.update(overrides)
    return TestClient(
        create_app(
            settings=Settings(**settings_values),
            page_service=page_service,
        )
    )


def hold_session_clock(client: TestClient, now: datetime) -> None:
    """Hold the clock of the app's session codec at ``now``.

    A token's issue time is not part of what ``verify_session`` returns, so a
    lifetime can only be measured from the instant the codec itself issued at.
    Comparing it with a separately read host clock is off by however far that
    clock stepped in between. Call this after the first request, which is what
    builds the service. The held codec is derived from the one the app built,
    so the lifetimes Settings configured stay under test.
    """

    service = client.app.state.session_service
    client.app.state.session_service = replace(
        service,
        token_codec=replace(service.token_codec, now=lambda: now),
    )


def sign_in_on_held_session_clock(
    client: TestClient, *, email: str, password: str
) -> None:
    """Swap the signup cookie for one that a held session clock issued.

    Signup stamps its cookie from the host clock, and the codec reads that clock
    again to judge the cookie on every later request. A host clock that jumps
    forward by more than the session ttl in between expires the cookie before
    the test has used it. Hold the codec, then sign in again, so that one
    instant both issues and judges the cookie. The login is the real one, so
    authentication and the Settings-built lifetimes stay under test.
    """

    hold_session_clock(client, datetime(2030, 6, 1, tzinfo=UTC))
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text


@pytest.fixture
def api_token_clock(monkeypatch) -> list[datetime]:
    """The API token service's "now", moved only when a test moves it.

    Expiry is judged against the service's current UTC time, not a durable
    "expired" state, so a host clock that steps backwards can make an expired
    token live again. A test that backdates ``expires_at`` against the host
    clock and lets the service read the host clock again later is right only
    while no step lands between the two reads. Preventing the reactivation in
    production needs a separate clock/expiration policy. ``created_at`` keeps
    coming from the host clock, so a test that depends on that order pins it.
    """

    clock = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    monkeypatch.setattr(
        "backend.services.api_token_service.utc_now",
        lambda: clock[0],
    )
    return clock


def test_auth_capabilities_exposes_effective_signup_and_recovery_policy(page_service):
    # The route projects app.state.settings at request time, so one app serves
    # every configuration.
    client = make_client(page_service)
    base_values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "test-session-secret-for-auth-api-tests",
        "session_ttl_seconds": 3600,
    }
    solo_local = client.get("/api/v1/auth/capabilities")
    client.app.state.settings = Settings(**base_values, app_mode="team")
    team_default = client.get("/api/v1/auth/capabilities")
    client.app.state.settings = Settings(
        **base_values,
        app_mode="team",
        signup_enabled=True,
    )
    team_explicit = client.get("/api/v1/auth/capabilities")

    assert solo_local.status_code == 200
    assert solo_local.json()["data"] == {
        "signup_enabled": True,
        "password_recovery": {"mode": "disabled", "admin_assisted": False},
    }
    assert team_default.json()["data"] == {
        "signup_enabled": False,
        "password_recovery": {"mode": "disabled", "admin_assisted": False},
    }
    assert team_explicit.json()["data"] == {
        "signup_enabled": True,
        "password_recovery": {"mode": "disabled", "admin_assisted": False},
    }
    assert solo_local.headers["cache-control"] == "no-store"
    assert solo_local.headers["pragma"] == "no-cache"


def test_signup_sets_session_cookie_and_me_returns_user(page_service):
    client = make_client(page_service)

    response = client.post(
        "/api/v1/auth/signup",
        json={
            "email": " User@Example.COM ",
            "password": "correct-password",
            "display_name": "Example User",
        },
    )

    assert response.status_code == 201
    assert "dtxwiki_session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    data = response.json()["data"]
    assert data["user"]["email"] == "user@example.com"
    assert data["user"]["display_name"] == "Example User"
    assert "session_token" not in data

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.headers["cache-control"] == "no-store"
    assert me.headers["pragma"] == "no-cache"
    me_data = me.json()["data"]
    # /me carries computed instance capabilities (oauth_available,
    # is_server_admin) that the signup response leaves at schema defaults;
    # compare the user identity rather than the whole payload (merged I5).
    assert me_data["user_id"] == data["user"]["user_id"]
    assert me_data["email"] == data["user"]["email"]
    assert me_data["display_name"] == data["user"]["display_name"]
    assert me_data["status"] == data["user"]["status"]
    assert me_data["email_verified_at"] is None
    # I5: /me carries the server's authoritative OAuth availability + reason so
    # the MCP-access UI steers from server truth (https + DCR policy), not a
    # client-side URL guess that can contradict the deployment tab.
    assert isinstance(me_data["oauth_available"], bool)
    # The default test app is served over plain HTTP, so OAuth is unavailable
    # with an actionable reason (not None).
    assert me_data["oauth_available"] is False
    assert me_data["oauth_unavailable_reason"]


def test_login_cookie_and_token_lifetime_follow_remember_me(page_service):
    client = make_client(
        page_service,
        session_remembered_ttl_seconds=100,
        session_remembered_refresh_threshold_seconds=20,
        session_remembered_absolute_ttl_seconds=250,
    )
    created = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "remember-policy@example.com",
            "password": "correct-password",
            "remember_me": False,
        },
    )
    assert created.status_code == 201
    assert "Max-Age" not in created.headers["set-cookie"]
    assert "expires=" not in created.headers["set-cookie"].lower()

    issued_at = datetime(2030, 6, 1, tzinfo=UTC)
    hold_session_clock(client, issued_at)

    client.cookies.clear()
    session_only = client.post(
        "/api/v1/auth/login",
        json={
            "email": "remember-policy@example.com",
            "password": "correct-password",
            "remember_me": False,
        },
    )
    assert session_only.status_code == 200
    assert "Max-Age" not in session_only.headers["set-cookie"]
    assert "expires=" not in session_only.headers["set-cookie"].lower()
    session_only_token = client.cookies.get("dtxwiki_session")
    assert session_only_token is not None
    session_only_verified = client.app.state.session_service.token_codec.verify_session(
        session_only_token
    )
    assert session_only_verified.remember_me is False
    # The ordinary ttl, from the instant the codec issued at.
    assert session_only_verified.expires_at - issued_at == timedelta(seconds=3600)

    client.cookies.clear()
    remembered = client.post(
        "/api/v1/auth/login",
        json={
            "email": "remember-policy@example.com",
            "password": "correct-password",
            "remember_me": True,
        },
    )
    assert remembered.status_code == 200
    # A non-default value proves the production get_session_service factory
    # wires Settings into the codec instead of merely relying on codec defaults.
    assert "Max-Age=100" in remembered.headers["set-cookie"]
    assert "expires=" in remembered.headers["set-cookie"].lower()
    remembered_token = client.cookies.get("dtxwiki_session")
    assert remembered_token is not None
    remembered_verified = client.app.state.session_service.token_codec.verify_session(
        remembered_token
    )
    assert remembered_verified.remember_me is True
    assert remembered_verified.authenticated_at is not None
    assert (
        remembered_verified.expires_at - remembered_verified.authenticated_at
        == timedelta(seconds=100)
    )


def test_omitted_remember_me_fails_closed_to_a_browser_session(page_service):
    """A caller that never opts in must not receive the remembered policy.

    `remember_me` defaults to False precisely because non-SPA callers
    (tests/e2e/auth.ts, packaging smokes, curl/CI scripts) post only
    email+password. Defaulting to True silently handed each of them a 30-day
    renewable credential the moment remembered sessions landed.
    """

    client = make_client(
        page_service,
        session_remembered_ttl_seconds=100,
        session_remembered_refresh_threshold_seconds=20,
        session_remembered_absolute_ttl_seconds=250,
    )
    created = client.post(
        "/api/v1/auth/signup",
        json={"email": "omitted-remember@example.com", "password": "correct-password"},
    )
    assert created.status_code == 201
    assert "Max-Age" not in created.headers["set-cookie"]
    assert "expires=" not in created.headers["set-cookie"].lower()

    issued_at = datetime(2030, 6, 1, tzinfo=UTC)
    hold_session_clock(client, issued_at)

    client.cookies.clear()
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"email": "omitted-remember@example.com", "password": "correct-password"},
    )
    assert logged_in.status_code == 200
    assert "Max-Age" not in logged_in.headers["set-cookie"]
    token = client.cookies.get("dtxwiki_session")
    assert token is not None
    verified = client.app.state.session_service.token_codec.verify_session(token)
    assert verified.remember_me is False
    assert verified.authenticated_at is None
    # The 3600s ordinary ttl, not the 100s remembered one.
    assert verified.expires_at - issued_at == timedelta(seconds=3600)


def test_me_marks_both_authenticated_and_rejected_responses_uncacheable(page_service):
    """FastAPI merges sub-response headers only when the handler returns a
    non-Response value, so the 401 branch has to carry them itself."""

    client = make_client(page_service)

    rejected = client.get("/api/v1/auth/me")
    assert rejected.status_code == 401
    assert rejected.json()["error_code"] == "invalid_session"
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.headers["pragma"] == "no-cache"

    client.post(
        "/api/v1/auth/signup",
        json={"email": "no-store@example.com", "password": "correct-password"},
    )
    authenticated = client.get("/api/v1/auth/me")
    assert authenticated.status_code == 200
    assert authenticated.headers["cache-control"] == "no-store"
    assert authenticated.headers["pragma"] == "no-cache"


def test_remembered_refresh_emits_a_renewal_audit_event(page_service):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="renewal-audit-secret",
        session_ttl_seconds=10,
        session_remembered_ttl_seconds=100,
        session_remembered_refresh_threshold_seconds=20,
        session_remembered_absolute_ttl_seconds=250,
    )
    app = create_app(settings=settings, page_service=page_service)
    client = TestClient(app)
    clock = [datetime(2030, 6, 1, tzinfo=UTC)]
    app.state.session_service = SessionService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=replace(
            build_session_token_codec(settings),
            now=lambda: clock[0],
        ),
        signup_enabled=True,
        audit_service=AuditService(),
    )

    signup = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "renewal-audit@example.com",
            "password": "correct-password",
            "remember_me": True,
        },
    )
    assert signup.status_code == 201
    assert "auth.session.renewed" not in _audit_actions(page_service)

    clock[0] += timedelta(seconds=50)
    outside_window = client.get("/api/v1/auth/me")
    assert outside_window.status_code == 200
    assert "set-cookie" not in outside_window.headers
    assert "auth.session.renewed" not in _audit_actions(page_service)

    clock[0] += timedelta(seconds=35)
    refreshed = client.get("/api/v1/auth/me")
    assert refreshed.status_code == 200
    assert "set-cookie" in refreshed.headers
    assert "auth.session.renewed" in _audit_actions(page_service)

    with page_service.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.session.renewed")
        )
    assert event is not None
    assert event.workspace_id is None
    assert event.payload["previous_expires_at"] == (
        datetime(2030, 6, 1, tzinfo=UTC) + timedelta(seconds=100)
    ).isoformat()
    assert event.payload["expires_at"] == (
        clock[0] + timedelta(seconds=100)
    ).isoformat()
    assert event.payload["authenticated_at"] == datetime(
        2030, 6, 1, tzinfo=UTC
    ).isoformat()


def test_unrecordable_renewal_keeps_the_presented_cookie(page_service):
    """A failed audit write must neither 500 the request nor ship a rotation
    that leaves no trail. The presented cookie is still valid, so the visitor
    keeps it and the next /auth/me retries."""

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="renewal-audit-failure-secret",
        session_ttl_seconds=10,
        session_remembered_ttl_seconds=100,
        session_remembered_refresh_threshold_seconds=20,
        session_remembered_absolute_ttl_seconds=250,
    )
    app = create_app(settings=settings, page_service=page_service)
    client = TestClient(app)
    clock = [datetime(2031, 1, 1, tzinfo=UTC)]

    class BrokenAuditService(AuditService):
        """Only the renewal write fails, so signup still establishes the
        remembered cookie this test then tries to rotate."""

        def emit(self, *args, **kwargs):
            if kwargs.get("action") == "auth.session.renewed":
                raise OperationalError("INSERT", {}, Exception("database is locked"))
            return super().emit(*args, **kwargs)

    app.state.session_service = SessionService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=replace(
            build_session_token_codec(settings),
            now=lambda: clock[0],
        ),
        signup_enabled=True,
        audit_service=BrokenAuditService(),
    )

    signup = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "renewal-audit-failure@example.com",
            "password": "correct-password",
            "remember_me": True,
        },
    )
    assert signup.status_code == 201
    presented = client.cookies.get("dtxwiki_session")

    clock[0] += timedelta(seconds=85)
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert client.cookies.get("dtxwiki_session") == presented


def test_build_session_token_codec_wires_every_operator_lifetime(page_service):
    """One factory, so a new lifetime field cannot be wired at one call site
    and silently defaulted at the other."""

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="factory-secret",
        session_ttl_seconds=11,
        session_remembered_ttl_seconds=22,
        session_remembered_refresh_threshold_seconds=7,
        session_remembered_absolute_ttl_seconds=33,
    )

    codec = build_session_token_codec(settings)

    assert codec.secret == settings.effective_session_secret
    assert codec.ttl_seconds == 11
    assert codec.remembered_ttl_seconds == 22
    assert codec.remembered_refresh_threshold_seconds == 7
    assert codec.remembered_absolute_ttl_seconds == 33


def test_me_refreshes_remembered_cookie_only_in_window_and_stops_at_cap(
    page_service,
):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="remembered-me-refresh-secret",
        session_ttl_seconds=10,
        session_remembered_ttl_seconds=100,
        session_remembered_refresh_threshold_seconds=20,
        session_remembered_absolute_ttl_seconds=250,
    )
    app = create_app(settings=settings, page_service=page_service)
    client = TestClient(app)
    signup = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "remembered-me@example.com",
            "password": "correct-password",
            "remember_me": False,
        },
    )
    assert signup.status_code == 201
    ordinary_me = client.get("/api/v1/auth/me")
    assert ordinary_me.status_code == 200
    assert "set-cookie" not in ordinary_me.headers
    client.cookies.clear()

    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    codec = SessionTokenCodec(
        secret=settings.effective_session_secret,
        ttl_seconds=settings.session_ttl_seconds,
        remembered_ttl_seconds=settings.session_remembered_ttl_seconds,
        remembered_refresh_threshold_seconds=(
            settings.session_remembered_refresh_threshold_seconds
        ),
        remembered_absolute_ttl_seconds=(
            settings.session_remembered_absolute_ttl_seconds
        ),
        now=lambda: clock[0],
    )
    app.state.session_service = SessionService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        token_codec=codec,
        signup_enabled=True,
    )

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "remembered-me@example.com",
            "password": "correct-password",
            "remember_me": True,
        },
    )
    assert login.status_code == 200
    assert "Max-Age=100" in login.headers["set-cookie"]
    initial_token = client.cookies.get("dtxwiki_session")
    assert initial_token is not None
    initial_verified = codec.verify_session(initial_token)

    clock[0] += timedelta(seconds=79)
    outside_window = client.get("/api/v1/auth/me")
    assert outside_window.status_code == 200
    assert "set-cookie" not in outside_window.headers

    clock[0] += timedelta(seconds=1)
    first_refresh = client.get("/api/v1/auth/me")
    assert first_refresh.status_code == 200
    assert "Max-Age=100" in first_refresh.headers["set-cookie"]
    refreshed_token = client.cookies.get("dtxwiki_session")
    assert refreshed_token is not None and refreshed_token != initial_token
    refreshed_verified = codec.verify_session(refreshed_token)
    assert refreshed_verified.authenticated_at == initial_verified.authenticated_at
    assert refreshed_verified.expires_at == clock[0] + timedelta(seconds=100)

    clock[0] += timedelta(seconds=81)
    capped_refresh = client.get("/api/v1/auth/me")
    assert capped_refresh.status_code == 200
    # The persistent cookie lifetime is shortened to the token's 90-day-style
    # absolute cap instead of advertising the full sliding TTL past token exp.
    assert "Max-Age=89" in capped_refresh.headers["set-cookie"]
    capped_token = client.cookies.get("dtxwiki_session")
    assert capped_token is not None
    capped_verified = codec.verify_session(capped_token)
    assert capped_verified.expires_at == initial_verified.authenticated_at + timedelta(
        seconds=250
    )

    clock[0] += timedelta(seconds=70)
    at_absolute_cap = client.get("/api/v1/auth/me")
    assert at_absolute_cap.status_code == 200
    assert "set-cookie" not in at_absolute_cap.headers


def test_signup_marks_session_cookie_secure_for_forwarded_https(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                behind_proxy=True,
            ),
            page_service=page_service,
        )
    )

    response = client.post(
        "/api/v1/auth/signup",
        headers={"X-Forwarded-Proto": "https"},
        json={
            "email": "secure@example.com",
            "password": "correct-password",
            "display_name": "Secure User",
        },
    )

    assert response.status_code == 201
    assert "Secure" in response.headers["set-cookie"]


def test_signup_rejects_duplicate_email(page_service):
    client = make_client(page_service)

    assert client.post(
        "/api/v1/auth/signup",
        json={"email": "same@example.com", "password": "first-password-123"},
    ).status_code == 201
    duplicate = client.post(
        "/api/v1/auth/signup",
        json={"email": " SAME@example.com ", "password": "second-password-123"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "duplicate_email"


def test_signup_disabled_returns_403_with_error_code(page_service):
    from backend.services import session_service as session_service_module

    client = make_client(page_service, signup_enabled=False)

    response = client.post(
        "/api/v1/auth/signup",
        json={"email": "blocked@example.com", "password": "blocked-password-123"},
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "signup_disabled"
    assert "blocked@example.com" not in session_service_module._AUTH_EMAIL_BUCKET

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "blocked@example.com", "password": "blocked-password-123"},
    )
    assert login.status_code == 401


def test_team_mode_signup_grants_personal_owner_only(page_service):
    client = make_client(page_service, app_mode="team", signup_enabled=True)

    response = client.post(
        "/api/v1/auth/signup",
        json={"email": "team-viewer@example.com", "password": "viewer-password-123"},
    )

    assert response.status_code == 201
    user_id = response.json()["data"]["user"]["user_id"]
    with page_service.session_factory() as session:
        user = session.get(User, UUID(user_id))
        assert user is not None
        rows = session.execute(
            select(Workspace, WorkspaceMembership)
            .join(
                WorkspaceMembership,
                WorkspaceMembership.workspace_id == Workspace.id,
            )
            .where(WorkspaceMembership.user_id == UUID(user_id))
        ).all()
    assert len(rows) == 1
    workspace, membership = rows[0]
    assert workspace.id == user.default_workspace_id
    assert workspace.slug.startswith("personal-")
    assert membership.role == "owner"


def test_login_rejects_invalid_credentials(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "login@example.com", "password": "right-password-123"},
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": "wrong-pass"},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_credentials"


def test_login_rate_limited_per_email_returns_429(page_service):
    from backend.services.session_service import AUTH_RATE_LIMIT_PER_EMAIL

    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "bruteforce@example.com", "password": "right-password-123"},
    )
    client.cookies.clear()

    # The signup above already consumed one slot for this email; burn the
    # rest of the per-email budget with wrong-password attempts.
    for _ in range(AUTH_RATE_LIMIT_PER_EMAIL - 1):
        attempt = client.post(
            "/api/v1/auth/login",
            json={"email": "bruteforce@example.com", "password": "wrong-pass"},
        )
        assert attempt.status_code == 401

    # The throttle fires before the credential check — even the correct
    # password is rejected with 429 + the standard envelope.
    blocked = client.post(
        "/api/v1/auth/login",
        json={"email": "BruteForce@Example.COM", "password": "right-password-123"},
    )
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error_code"] == "rate_limited"
    assert body["envelope"]["result_status"] == "failed"
    assert body["envelope"]["error_code"] == "rate_limited"

    # A different account is untouched (positive control).
    other = client.post(
        "/api/v1/auth/login",
        json={"email": "other@example.com", "password": "whatever-pass"},
    )
    assert other.status_code == 401


def test_signup_rate_limited_per_email_returns_429(page_service):
    from backend.services.session_service import AUTH_RATE_LIMIT_PER_EMAIL

    client = make_client(page_service)

    for i in range(AUTH_RATE_LIMIT_PER_EMAIL):
        attempt = client.post(
            "/api/v1/auth/signup",
            json={
                "email": "signup-flood@example.com",
                "password": "first-password-123",
            },
        )
        assert attempt.status_code == (201 if i == 0 else 409)

    blocked = client.post(
        "/api/v1/auth/signup",
        json={"email": "signup-flood@example.com", "password": "first-password-123"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "rate_limited"


def test_password_change_rotates_password_and_session(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={
            "email": "change@example.com",
            "password": "old-password-123",
            # Explicit: remember_me now defaults to False, and this test is
            # about a remembered session surviving a password change.
            "remember_me": True,
        },
    )
    old_cookie = client.cookies.get("dtxwiki_session")
    assert old_cookie is not None
    old_verified = client.app.state.session_service.token_codec.verify_session(
        old_cookie
    )
    assert old_verified.remember_me is True
    assert old_verified.authenticated_at is not None
    service = client.app.state.session_service
    password_change_time = old_verified.authenticated_at + timedelta(days=1)
    client.app.state.session_service = replace(
        service,
        token_codec=replace(service.token_codec, now=lambda: password_change_time),
    )
    stolen_client = make_client(page_service)
    stolen_client.cookies.set("dtxwiki_session", old_cookie)

    changed = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "old-password-123",
            "new_password": "new-password-123",
        },
    )

    assert changed.status_code == 200
    assert changed.json()["data"]["user"]["email"] == "change@example.com"
    assert "dtxwiki_session=" in changed.headers["set-cookie"]
    changed_cookie = client.cookies.get("dtxwiki_session")
    assert changed_cookie is not None
    changed_verified = client.app.state.session_service.token_codec.verify_session(
        changed_cookie
    )
    assert changed_verified.remember_me is True
    assert changed_verified.authenticated_at == password_change_time
    assert changed_verified.authenticated_at != old_verified.authenticated_at
    assert "Max-Age=2592000" in changed.headers["set-cookie"]

    stolen_me = stolen_client.get("/api/v1/auth/me")
    assert stolen_me.status_code == 401
    assert stolen_me.json()["error_code"] == "invalid_session"

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    assert logout.json()["data"]["logged_out"] is True
    assert "dtxwiki_session=" in logout.headers["set-cookie"]

    old_login = client.post(
        "/api/v1/auth/login",
        json={"email": "change@example.com", "password": "old-password-123"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login",
        json={"email": "change@example.com", "password": "new-password-123"},
    )
    assert new_login.status_code == 200
    assert new_login.json()["data"]["user"]["email"] == "change@example.com"


def test_password_change_preserves_session_only_cookie_policy(page_service):
    client = make_client(page_service)
    signup = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "session-only-password@example.com",
            "password": "old-password-123",
            "remember_me": False,
        },
    )
    assert signup.status_code == 201
    assert "Max-Age" not in signup.headers["set-cookie"]

    changed = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "old-password-123",
            "new_password": "new-password-123",
        },
    )

    assert changed.status_code == 200
    assert "Max-Age" not in changed.headers["set-cookie"]
    assert "expires=" not in changed.headers["set-cookie"].lower()
    changed_token = client.cookies.get("dtxwiki_session")
    assert changed_token is not None
    verified = client.app.state.session_service.token_codec.verify_session(
        changed_token
    )
    assert verified.remember_me is False
    assert verified.authenticated_at is None


def test_password_reset_confirm_is_single_use_and_does_not_auto_login(page_service):
    client = make_client(
        page_service,
        public_base_url="https://wiki.example.com",
        session_cookie_secure=False,
    )
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "reset-web@example.com", "password": "original-password"},
    )
    assert signup.status_code == 201
    service = PasswordRecoveryService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    )
    issued = service.issue_password_reset(email="reset-web@example.com")
    token = issued.reset_url.split("token=", maxsplit=1)[1]

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "replacement-password"},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["data"] == {"reset": True}
    assert confirmed.headers["cache-control"] == "no-store"
    assert confirmed.headers["pragma"] == "no-cache"
    assert "dtxwiki_session=" in confirmed.headers["set-cookie"]
    assert "Max-Age=0" in confirmed.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "reset-web@example.com", "password": "original-password"},
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "reset-web@example.com", "password": "replacement-password"},
    ).status_code == 200

    reused = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "another-password"},
    )
    invalid = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "dtxr_invalid-token-value", "new_password": "another-password"},
    )
    assert reused.status_code == invalid.status_code == 400
    assert reused.json()["error_code"] == "password_reset_token_used"
    assert invalid.json()["error_code"] == "invalid_password_reset_token"
    assert reused.headers["cache-control"] == "no-store"
    assert invalid.headers["cache-control"] == "no-store"
    assert reused.json()["message"] != invalid.json()["message"]


def test_logout_revokes_presented_session_cookie(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "logout@example.com", "password": "logout-password-123"},
    )
    session_cookie = client.cookies.get("dtxwiki_session")
    assert session_cookie is not None
    stolen_client = make_client(page_service)
    stolen_client.cookies.set("dtxwiki_session", session_cookie)

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 200

    stolen_me = stolen_client.get("/api/v1/auth/me")
    assert stolen_me.status_code == 401
    assert stolen_me.json()["error_code"] == "invalid_session"


def test_api_token_issue_list_and_revoke_are_session_scoped(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "token-owner@example.com", "password": "owner-password-123"},
    )
    sign_in_on_held_session_clock(
        client, email="token-owner@example.com", password="owner-password-123"
    )
    with page_service.session_factory() as session:
        user = session.scalar(
            select(User).where(User.email == "token-owner@example.com")
        )
        assert user is not None
        assert user.default_workspace_id is not None
        owner_user_id = str(user.id)
        workspace_id = str(user.default_workspace_id)

    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": "CI",
            "scopes": ["mcp:read", "mcp:write"],
            "mcp_client_type": "codex",
        },
    )

    assert issued.status_code == 201
    issue_data = issued.json()["data"]
    plain_token = issue_data["plain_token"]
    assert plain_token.startswith("dtxw_")
    assert issue_data["api_token"]["name"] == "CI"
    assert issue_data["api_token"]["scopes"] == ["mcp:read", "mcp:write"]
    assert issue_data["api_token"]["mcp_client_type"] == "codex"
    assert issue_data["api_token"]["mcp_client_label"] == "Codex CLI"
    assert issue_data["api_token"]["catalog_app_id"] == "codex"
    assert issue_data["api_token"]["catalog_app_label_snapshot"] == "Codex CLI"
    assert issue_data["api_token"]["catalog_revision"] == (
        get_mcp_app_catalog_repository().snapshot().catalog_revision
    )
    assert plain_token not in str(issue_data["api_token"])

    duplicate = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": "CI",
            "scopes": ["mcp:read"],
        },
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["data"]["api_token"]["name"] == "CI 2"

    # Listing orders stored timestamps, which need not follow insertion order
    # when the host clock steps backward. Make the expected order explicit.
    first_id = UUID(issue_data["api_token"]["id"])
    second_id = UUID(duplicate.json()["data"]["api_token"]["id"])
    created_at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    with page_service.session_factory.begin() as session:
        first_model = session.get(ApiToken, first_id)
        second_model = session.get(ApiToken, second_id)
        assert first_model is not None
        assert second_model is not None
        first_model.created_at = created_at
        second_model.created_at = created_at + timedelta(seconds=1)

    listed = client.get("/api/v1/auth/tokens")
    assert listed.status_code == 200
    tokens = listed.json()["data"]
    assert len(tokens) == 2
    assert tokens[0]["name"] == "CI 2"
    assert tokens[1]["name"] == "CI"
    assert "plain_token" not in tokens[0]
    assert plain_token not in str(tokens)

    renamed = client.patch(
        f"/api/v1/auth/tokens/{tokens[0]['id']}",
        json={"name": "CI"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["name"] == "CI 2"

    revoked = client.delete(f"/api/v1/auth/tokens/{tokens[1]['id']}")
    assert revoked.status_code == 200
    assert revoked.json()["data"]["revoked_at"] is not None

    active_delete = client.delete(f"/api/v1/auth/tokens/{tokens[0]['id']}/record")
    assert active_delete.status_code == 422

    deleted = client.delete(f"/api/v1/auth/tokens/{tokens[1]['id']}/record")
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"id": tokens[1]["id"], "deleted": True}

    with page_service.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.record_deleted",
                AuditEvent.target_id == UUID(tokens[1]["id"]),
            )
        )
        assert event is not None
        assert event.workspace_id == UUID(workspace_id)
        assert event.actor_id == owner_user_id
        assert event.payload == {
            "workspace_id": workspace_id,
            "inactive_reason": "revoked",
        }
        audit_text = str(event.payload)
        assert plain_token not in audit_text
        assert tokens[1]["token_preview"] not in audit_text

    listed_after_delete = client.get("/api/v1/auth/tokens")
    assert listed_after_delete.status_code == 200
    assert [token["id"] for token in listed_after_delete.json()["data"]] == [
        tokens[0]["id"]
    ]


def test_api_token_create_requires_supported_scope(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "bad-scope@example.com", "password": "owner-password-123"},
    )
    with page_service.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "bad-scope@example.com"))
        assert user is not None
        assert user.default_workspace_id is not None
        workspace_id = str(user.default_workspace_id)

    response = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": "bad",
            "scopes": ["workspace-admin"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "validation_failed"


def test_user_tokens_paginate_filter_and_delete_record(page_service, api_token_clock):
    """D-01v2/T4: this is the account-scoped ``GET/DELETE /auth/tokens``
    listing that replaced the deleted workspace-scoped
    ``/workspaces/{id}/settings/access-keys``."""

    client = make_client(page_service)
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "paged-keys@example.com", "password": "owner-password-123"},
    )
    assert signup.status_code == 201
    sign_in_on_held_session_clock(
        client, email="paged-keys@example.com", password="owner-password-123"
    )
    with page_service.session_factory() as session:
        user = session.scalar(
            select(User).where(User.email == "paged-keys@example.com")
        )
        assert user is not None
        assert user.default_workspace_id is not None
        workspace_id = str(user.default_workspace_id)

    def issue(name: str):
        payload = {
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": name,
            "scopes": ["mcp:read"],
        }
        response = client.post("/api/v1/auth/tokens", json=payload)
        assert response.status_code == 201, response.text
        return response.json()["data"]

    active = [issue(f"Active {index}") for index in range(3)]
    expired = issue("Expired")
    revoked_and_expired = issue("Revoked and expired")

    # The create schema only offers a bounded set of forward expiries
    # (expires_in_days in {30, 90, 180, 365} or unlimited) -- there is no
    # REST-level way to mint an already-expired PAT anymore, so "expired" is
    # simulated the same way other tests in this suite construct fixture
    # rows directly: by moving expires_at into the past after creation. The
    # service judges expiry on its own clock, so that is the clock to move it
    # behind.
    now = api_token_clock[0]
    with page_service.session_factory.begin() as session:
        for item in (expired, revoked_and_expired):
            model = session.get(ApiToken, UUID(item["api_token"]["id"]))
            assert model is not None
            model.expires_at = now - timedelta(seconds=1)

    # Listing is newest first with ties broken by id, and created_at is the host
    # clock, which need not follow insertion order. Pin it: one newest active
    # token, then an older tie that straddles the page edge.
    with page_service.session_factory.begin() as session:
        for index, item in enumerate([*active, expired, revoked_and_expired]):
            model = session.get(ApiToken, UUID(item["api_token"]["id"]))
            assert model is not None
            model.created_at = now - timedelta(seconds=min(index, 1))
    active_ids = [item["api_token"]["id"] for item in active]
    expected_active_order = [active_ids[0], *sorted(active_ids[1:], reverse=True)]

    revoke = client.delete(
        f"/api/v1/auth/tokens/{revoked_and_expired['api_token']['id']}"
    )
    assert revoke.status_code == 200

    base_url = "/api/v1/auth/tokens"
    first = client.get(base_url, params={"status": "active", "limit": 2})
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert len(first_payload["data"]) == 2
    assert first_payload["next_cursor"]
    assert first_payload["total_count"] == 5
    assert first_payload["filtered_count"] == 3
    assert first_payload["status_counts"] == {
        "active": 3,
        "expired": 1,
        "revoked": 1,
    }
    assert {row["id"] for row in first_payload["data"]} <= {
        item["api_token"]["id"] for item in active
    }
    assert [row["id"] for row in first_payload["data"]] == expected_active_order[:2]
    assert {row["workspaces"][0]["id"] for row in first_payload["data"]} == {
        workspace_id
    }

    # The cursor carries page one's evaluation instant. Move the service clock,
    # so that a page two which read its own would show it.
    api_token_clock[0] += timedelta(seconds=10)
    second = client.get(
        base_url,
        params={
            "status": "active",
            "limit": 2,
            "cursor": first_payload["next_cursor"],
        },
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert len(second_payload["data"]) == 1
    assert second_payload["next_cursor"] is None
    assert second_payload["evaluated_at"] == first_payload["evaluated_at"]
    assert {row["id"] for row in first_payload["data"] + second_payload["data"]} == {
        item["api_token"]["id"] for item in active
    }
    assert [row["id"] for row in second_payload["data"]] == expected_active_order[2:]

    inactive = client.get(base_url, params={"status": "inactive"})
    assert inactive.status_code == 200
    assert inactive.json()["filtered_count"] == 2
    assert {row["id"] for row in inactive.json()["data"]} == {
        expired["api_token"]["id"],
        revoked_and_expired["api_token"]["id"],
    }

    wrong_query_cursor = client.get(
        base_url,
        params={
            "status": "inactive",
            "limit": 2,
            "cursor": first_payload["next_cursor"],
        },
    )
    assert wrong_query_cursor.status_code == 422
    assert wrong_query_cursor.json()["error_code"] == "invalid_access_key_cursor"

    # /record accepts an inactive token -- expired or revoked, matching the
    # deleted workspace-scoped delete_inactive_token's contract exactly. Only
    # a still-active token 422s.
    active_delete = client.delete(
        f"{base_url}/{active[0]['api_token']['id']}/record"
    )
    assert active_delete.status_code == 422
    assert active_delete.json()["error_code"] == "invalid_api_token_request"

    expired_id = expired["api_token"]["id"]
    expired_not_revoked_delete = client.delete(f"{base_url}/{expired_id}/record")
    assert expired_not_revoked_delete.status_code == 200, (
        expired_not_revoked_delete.text
    )
    assert expired_not_revoked_delete.json()["data"] == {
        "id": expired_id,
        "deleted": True,
    }

    revoked_id = revoked_and_expired["api_token"]["id"]
    deleted = client.delete(f"{base_url}/{revoked_id}/record")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {
        "id": revoked_and_expired["api_token"]["id"],
        "deleted": True,
    }

    remaining = client.get(base_url, params={"status": "all", "limit": 10})
    assert remaining.status_code == 200
    assert len(remaining.json()["data"]) == 3

    # /auth/tokens has always been (and remains) session-cookie-only --
    # current_session_user reads only the cookie, so a bearer PAT is simply
    # ignored, not explicitly rejected, whenever a valid session cookie is
    # also present. Drop the cookie to actually exercise "no session".
    client.cookies.clear()
    bearer_denied = client.get(
        base_url,
        headers={"Authorization": f"Bearer {active[0]['plain_token']}"},
    )
    assert bearer_denied.status_code == 401

    with page_service.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.record_deleted",
                AuditEvent.target_id == UUID(revoked_and_expired["api_token"]["id"]),
            )
        )
        assert event is not None
        assert event.payload["inactive_reason"] == "revoked"
        audit_text = str(event.payload)
        assert revoked_and_expired["plain_token"] not in audit_text
        assert revoked_and_expired["api_token"]["token_preview"] not in audit_text

        expired_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.api_token.record_deleted",
                AuditEvent.target_id == UUID(expired_id),
            )
        )
        assert expired_event is not None
        assert expired_event.payload["inactive_reason"] == "expired"


def test_user_tokens_cursor_traverses_more_than_one_default_page_exactly_once(
    page_service,
):
    client = make_client(page_service)
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "many-keys@example.com", "password": "owner-password-123"},
    )
    assert signup.status_code == 201
    now = utc_now()
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "many-keys@example.com")
        )
        assert user is not None
        assert user.default_workspace_id is not None
        rows = []
        for index in range(23):
            row = ApiToken(
                user_id=user.id,
                name=f"Cursor traversal key {index:02d}",
                token_lookup_hash=f"cursor-traversal-lookup-{index}",
                hashed_token=f"cursor-traversal-hash-{index}",
                token_preview=f"dtxw_...{index:04d}",
                scopes=["mcp:read"],
                created_at=now - timedelta(seconds=index),
            )
            session.add(row)
            rows.append(row)
        session.flush()
        for row in rows:
            session.add(
                ApiTokenWorkspace(
                    token_id=row.id,
                    workspace_id=user.default_workspace_id,
                    desired_scopes=["mcp:read"],
                    revision=1,
                    sync_state="synced",
                )
            )
        expected_ids = [str(row.id) for row in rows]

    url = "/api/v1/auth/tokens"
    seen_ids: list[str] = []
    page_sizes: list[int] = []
    evaluated_at = None
    cursor = None
    page_count = 0
    while True:
        params = (
            {"status": "active", "cursor": cursor}
            if cursor is not None
            else {"status": "active"}
        )
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        page_ids = [row["id"] for row in payload["data"]]
        assert not set(page_ids).intersection(seen_ids)
        assert payload["total_count"] == 23
        assert payload["filtered_count"] == 23
        assert payload["status_counts"] == {
            "active": 23,
            "expired": 0,
            "revoked": 0,
        }
        if evaluated_at is None:
            evaluated_at = payload["evaluated_at"]
        else:
            assert payload["evaluated_at"] == evaluated_at
        seen_ids.extend(page_ids)
        page_sizes.append(len(page_ids))
        page_count += 1
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert page_count == 2
    assert page_sizes == [20, 3]
    assert seen_ids == expected_ids
    assert len(seen_ids) == len(set(seen_ids)) == 23


def _audit_actions(page_service) -> set[str]:
    with page_service.session_factory() as session:
        return {event.action for event in session.scalars(select(AuditEvent)).all()}


def _audit_action_counts(page_service) -> Counter[str]:
    with page_service.session_factory() as session:
        return Counter(
            event.action for event in session.scalars(select(AuditEvent)).all()
        )


def test_auth_lifecycle_emits_an_audit_event_per_step(page_service):
    client = make_client(page_service)

    before = _audit_action_counts(page_service)
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": "audit-lifecycle@example.com", "password": "owner-password-123"},
    )
    assert response.status_code == 201
    assert (_audit_action_counts(page_service) - before)["auth.user.signed_up"] == 1

    # Drop session so login is a fresh authentication.
    client.cookies.clear()
    before = _audit_action_counts(page_service)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "audit-lifecycle@example.com", "password": "owner-password-123"},
    )
    assert response.status_code == 200
    assert (_audit_action_counts(page_service) - before)["auth.session.created"] == 1

    before = _audit_action_counts(page_service)
    response = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "owner-password-123",
            "new_password": "rotated-password-123",
        },
    )
    assert response.status_code == 200
    assert (_audit_action_counts(page_service) - before)["auth.password.changed"] == 1

    before = _audit_action_counts(page_service)
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert (_audit_action_counts(page_service) - before)["auth.session.destroyed"] == 1


def _password_change_failure_events(page_service) -> list[AuditEvent]:
    with page_service.session_factory() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.password.change.failed"
                )
            ).all()
        )


def test_password_change_wrong_guess_is_rate_limited_and_audited(page_service):
    # AUTH-04: /auth/password previously had no rate limit and no audit trail
    # for failed attempts — a stolen session cookie let an attacker grind
    # Argon2 verification against current_password with zero throttle and
    # zero forensic trace. Mirror reauthenticate's contract: every wrong
    # guess is audited, and the throttle itself is audited when it fires.
    from backend.services.session_service import AUTH_RATE_LIMIT_PER_EMAIL

    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "pw-audit@example.com", "password": "correct-password"},
    )

    first_wrong = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "wrong-password",
            "new_password": "irrelevant-new-password",
        },
    )
    assert first_wrong.status_code == 401
    assert first_wrong.json()["error_code"] == "invalid_credentials"

    failed_events = _password_change_failure_events(page_service)
    assert len(failed_events) == 1
    assert failed_events[0].payload == {"reason": "invalid_credentials"}

    # Signup already consumed one slot for this email; burn the rest of the
    # per-email budget (one more consumed above) with further wrong guesses.
    for _ in range(AUTH_RATE_LIMIT_PER_EMAIL - 2):
        attempt = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "wrong-password",
                "new_password": "irrelevant-new-password",
            },
        )
        assert attempt.status_code == 401

    # The throttle fires before the credential check — even the correct
    # password is rejected with 429, and the rejection itself is audited so a
    # blocked guessing campaign is not invisible after the fact.
    blocked = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "correct-password",
            "new_password": "irrelevant-new-password",
        },
    )
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "rate_limited"

    failed_events = _password_change_failure_events(page_service)
    reasons = [event.payload.get("reason") for event in failed_events]
    assert reasons.count("rate_limited") == 1
    assert reasons.count("invalid_credentials") == AUTH_RATE_LIMIT_PER_EMAIL - 1


def test_session_lifecycle_audit_is_system_scoped_not_workspace_scoped(page_service):
    # AUTH-05: session lifecycle audit rows used to be scoped to the acting
    # user's OLDEST workspace membership. For a user later invited into a
    # pre-existing shared workspace, that "oldest" workspace IS the shared
    # one (it predates the user's own signup) — so any co-member with viewer
    # access could read that user's login/logout/password-change timeline
    # via kind=system activity on the shared workspace. Session lifecycle
    # must be system-scoped (workspace_id=None), like PasswordRecoveryService
    # already emits its auth.* rows.
    from backend.tests.conftest import authenticate_test_client

    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="session-audit-scope-secret",
        ),
        page_service=page_service,
    )
    owner_client = TestClient(app)
    member_client = TestClient(app)
    authenticate_test_client(owner_client, email="scope-owner@example.com")
    authenticate_test_client(member_client, email="scope-member@example.com")

    with page_service.session_factory() as session:
        owner = session.scalar(
            select(User).where(User.email == "scope-owner@example.com")
        )
        assert owner is not None and owner.default_workspace_id is not None
        workspace_id = owner.default_workspace_id

    shared = owner_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={
            "email": "scope-member@example.com",
            "confirm_first_share": True,
        },
    )
    assert shared.status_code == 201, shared.text

    # Fresh login: owner's own (now-shared) workspace is trivially their
    # oldest/only workspace, so the pre-fix code scoped this event to it.
    owner_client.cookies.clear()
    fresh_login = owner_client.post(
        "/api/v1/auth/login",
        json={"email": "scope-owner@example.com", "password": "correct-password"},
    )
    assert fresh_login.status_code == 200

    activity = member_client.get(
        f"/api/v1/workspaces/{workspace_id}/activity?period=all&kind=system"
    )
    assert activity.status_code == 200
    items = activity.json()["data"]["items"]
    assert not any(
        item["body"] == "auth.session.created"
        and item["actor"] is not None
        and item["actor"]["actor_id"] == str(owner.id)
        for item in items
    )

    with page_service.session_factory() as session:
        login_event = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "auth.session.created")
            .order_by(AuditEvent.created_at.desc())
        )
        assert login_event is not None
        assert login_event.workspace_id is None
        # AUTH-10: system-scoped rows are retained as sensitive (matching
        # PasswordRecoveryService's auth.* rows), not the "standard" default
        # — and no longer carry the user's email in the payload, since
        # target_id already provides a queryable identity.
        assert login_event.retention_class == "sensitive"
        assert "email" not in (login_event.payload or {})
