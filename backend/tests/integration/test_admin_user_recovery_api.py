from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.mail import EmailOutbox
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User
from backend.services import server_admin_service
from backend.services import session_service as session_service_module
from backend.services.credential_hashing import CredentialHasher
from backend.services.password_recovery_service import (
    PasswordRecoveryAuthorizationError,
    PasswordRecoveryRecentReauthenticationRequiredError,
    PasswordRecoveryService,
)
from backend.services.session_service import RecentReauthenticationCodec
from backend.services.user_admin_service import UserAdminService


def admin_client(page_service, *, server_admin=True, mail=False):
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "admin-recovery-api-test-secret",
        "public_base_url": "https://wiki.example.com",
        "session_cookie_secure": False,
    }
    if mail:
        values.update(
            {
                "mail_provider": "smtp",
                "mail_from_address": "wiki@example.com",
                "smtp_host": "smtp.example.com",
            }
        )
    settings = Settings(**values)
    client = TestClient(create_app(settings=settings, page_service=page_service))
    users = UserAdminService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    )
    admin = users.add_user(
        email="admin@example.com",
        password="correct-password",
    )
    target = users.add_user(
        email="target@example.com",
        password="target-password",
        display_name="Recovery Target",
    )
    if server_admin:
        with page_service.session_factory.begin() as session:
            server_admin_service.grant_server_admin(
                session,
                admin.user_id,
                granted_by="test",
            )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "correct-password"},
    )
    assert login.status_code == 200, login.text
    return client, admin, target


def test_server_admin_search_requires_recent_http_only_proof(page_service):
    client, admin, target = admin_client(page_service)

    missing_proof = client.get(
        "/api/v1/admin/users",
        params={"query": "Recovery Target"},
    )
    primary_cookie = client.cookies.get("dtxwiki_session")
    reauthenticated = client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    )
    response = client.get("/api/v1/admin/users", params={"query": "Recovery Target"})

    assert missing_proof.status_code == 401
    assert (
        missing_proof.json()["error_code"]
        == "recent_reauthentication_required"
    )
    assert reauthenticated.status_code == 200, reauthenticated.text
    assert reauthenticated.json()["data"]["reauthenticated_until"]
    set_cookies = reauthenticated.headers.get_list("set-cookie")
    proof_cookie = next(
        header
        for header in set_cookies
        if header.startswith("dtxwiki_session_reauth=")
    )
    assert "HttpOnly" in proof_cookie
    assert "Max-Age=300" in proof_cookie
    assert "Path=/api/v1/admin" in proof_cookie
    assert "SameSite=strict" in proof_cookie
    assert not any(header.startswith("dtxwiki_session=") for header in set_cookies)
    assert client.cookies.get("dtxwiki_session") == primary_cookie
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["user_id"] == str(target.user_id)
    assert data["items"][0]["email"] == "target@example.com"
    assert data["items"][0]["workspaces"]
    with page_service.session_factory() as session:
        event = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "admin.users.searched")
            .order_by(AuditEvent.created_at.desc())
        )
        reauthentication_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.reauthentication.succeeded"
            )
        )
        assert event is not None and event.target_id == admin.user_id
        assert "Recovery Target" not in str(event.payload)
        assert "target@example.com" not in str(event.payload)
        assert reauthentication_event is not None
        assert reauthentication_event.target_id == admin.user_id
        assert "reauthenticated_until" in reauthentication_event.payload
        assert "password" not in str(reauthentication_event.payload)


def test_reauthentication_audits_failures_and_rate_limits(page_service, monkeypatch):
    client, admin, _ = admin_client(page_service)

    def failure_events() -> list[AuditEvent]:
        with page_service.session_factory() as session:
            return list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "auth.reauthentication.failed",
                        AuditEvent.target_id == admin.user_id,
                    )
                ).all()
            )

    denied = client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "wrong-password"},
    )
    assert denied.status_code == 401
    assert denied.json()["error_code"] == "invalid_credentials"
    # Attribute each audit row to its request by the request boundary. Ordering
    # by created_at would not do: that is wall-clock time, so it need not follow
    # request order when the host clock steps backward.
    after_denied = failure_events()
    assert [event.payload["reason"] for event in after_denied] == [
        "invalid_credentials"
    ]

    monkeypatch.setattr(session_service_module, "AUTH_RATE_LIMIT_PER_EMAIL", 1)
    rate_limited = client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    )
    assert rate_limited.status_code == 429

    events = failure_events()
    assert len(events) == 2
    reasons = {event.id: event.payload["reason"] for event in events}
    denied_event_id = after_denied[0].id
    assert reasons[denied_event_id] == "invalid_credentials"
    new_event_ids = set(reasons) - {denied_event_id}
    assert [reasons[event_id] for event_id in new_event_ids] == ["rate_limited"]
    assert all("password" not in str(event.payload) for event in events)


def test_reauthentication_proof_is_revoked_by_generation_change_and_logout(
    page_service,
):
    client, _, _ = admin_client(page_service)
    reauthenticated = client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    )
    assert reauthenticated.status_code == 200
    changed = client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "correct-password",
            "new_password": "replacement-password",
        },
    )
    assert changed.status_code == 200
    revoked = client.get("/api/v1/admin/users")
    assert revoked.status_code == 401
    assert revoked.json()["error_code"] == "recent_reauthentication_required"

    renewed = client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "replacement-password"},
    )
    assert renewed.status_code == 200
    assert client.cookies.get("dtxwiki_session_reauth") is not None
    logged_out = client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 200
    assert client.cookies.get("dtxwiki_session_reauth") is None
    logout_cookie = logged_out.headers.get("set-cookie", "")
    assert "dtxwiki_session_reauth=" in logout_cookie
    assert "Path=/api/v1/admin" in logout_cookie
    assert "SameSite=strict" in logout_cookie


def test_admin_link_reset_requires_recent_proof_and_is_one_time_reveal(
    page_service,
):
    client, _, target = admin_client(page_service)
    request = {
        "delivery": "link",
        "expires_in_seconds": 1800,
        "revoke_api_tokens": False,
    }

    denied = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json=request,
    )
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 0
    reauthenticated = client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    )
    issued = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json=request,
    )

    assert denied.status_code == 401
    assert denied.json()["error_code"] == "recent_reauthentication_required"
    assert reauthenticated.status_code == 200
    assert issued.status_code == 200, issued.text
    data = issued.json()["data"]
    assert data["delivery"] == "link"
    # M-2: fragment, not query -- the link the admin hands over must not put
    # the token into an access log when the user opens it.
    assert data["reset_url"].startswith(
        "https://wiki.example.com/reset-password#token=dtxr_"
    )
    assert "?" not in data["reset_url"]
    # There is deliberately no GET/reveal endpoint for an issued secret.
    assert client.get(
        f"/api/v1/admin/users/{target.user_id}/password-reset"
    ).status_code in {404, 405}


def test_admin_reset_rejects_expired_and_wrong_user_proofs(page_service):
    client, admin, target = admin_client(page_service)
    settings = client.app.state.settings
    cookie_name = f"{settings.session_cookie_name}_reauth"
    expired = RecentReauthenticationCodec(
        secret=settings.effective_session_secret,
        now=lambda: datetime.now(UTC) - timedelta(minutes=10),
    ).issue(user_id=admin.user_id, session_generation=0)
    wrong_user = RecentReauthenticationCodec(
        secret=settings.effective_session_secret,
    ).issue(user_id=target.user_id, session_generation=0)
    payload = {
        "delivery": "link",
        "expires_in_seconds": 1800,
        "revoke_api_tokens": False,
    }

    client.cookies.set(cookie_name, expired.proof_token)
    expired_response = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json=payload,
    )
    client.cookies.set(cookie_name, wrong_user.proof_token)
    wrong_user_response = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json=payload,
    )

    assert expired_response.status_code == 401
    assert expired_response.json()["error_code"] == (
        "recent_reauthentication_required"
    )
    assert wrong_user_response.status_code == 401
    assert wrong_user_response.json()["error_code"] == (
        "recent_reauthentication_required"
    )
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 0


def test_admin_email_reset_requires_verified_email_and_never_returns_link(
    page_service,
):
    client, _, target = admin_client(page_service, mail=True)
    payload = {
        "delivery": "email",
        "expires_in_seconds": 1800,
        "revoke_api_tokens": True,
    }
    assert client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    ).status_code == 200

    unverified = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json=payload,
    )
    with page_service.session_factory.begin() as session:
        target_model = session.get(User, target.user_id)
        assert target_model is not None
        target_model.email_verified_at = datetime.now(UTC)
    queued = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json=payload,
    )

    assert unverified.status_code == 422
    assert "dtxr_" not in unverified.text
    assert queued.status_code == 200, queued.text
    assert queued.json()["data"]["reset_url"] is None
    assert "dtxr_" not in queued.text
    with page_service.session_factory() as session:
        outbox = session.scalar(
            select(EmailOutbox).where(
                EmailOutbox.message_kind == "password_reset"
            )
        )
        assert outbox is not None


def test_workspace_owner_without_server_admin_cannot_search_or_reset(page_service):
    client, _, target = admin_client(page_service, server_admin=False)

    assert client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    ).status_code == 200
    assert client.get("/api/v1/admin/users").status_code == 403
    denied = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json={
            "delivery": "link",
            "expires_in_seconds": 1800,
            "revoke_api_tokens": False,
        },
    )
    assert denied.status_code == 403


def test_admin_reset_rechecks_live_grant_after_reauthentication(page_service):
    client, admin, target = admin_client(page_service)
    assert client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    ).status_code == 200
    with page_service.session_factory.begin() as session:
        assert server_admin_service.revoke_server_admin(session, admin.user_id)

    denied = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json={
            "delivery": "link",
            "expires_in_seconds": 1800,
            "revoke_api_tokens": False,
        },
    )

    assert denied.status_code == 403
    assert denied.json()["error_code"] == "server_admin_required"
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 0


def test_admin_reset_issue_transaction_rejects_stale_grant(page_service):
    _client, admin, target = admin_client(page_service)
    recovery = PasswordRecoveryService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    )
    with page_service.session_factory.begin() as session:
        admin_model = session.get(User, admin.user_id)
        assert admin_model is not None
        session_generation = admin_model.session_generation
        assert server_admin_service.revoke_server_admin(session, admin.user_id)

    with pytest.raises(
        PasswordRecoveryAuthorizationError,
        match="server-admin grant is required",
    ):
        recovery.issue_password_reset_for_user(
            target_user_id=target.user_id,
            delivery="link",
            requested_by_user_id=admin.user_id,
            requested_by_session_generation=session_generation,
            actor_kind="human",
            actor_id=str(admin.user_id),
        )

    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 0


def test_admin_reset_issue_transaction_rejects_stale_reauthentication_generation(
    page_service,
):
    _client, admin, target = admin_client(page_service)
    recovery = PasswordRecoveryService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
        public_base_url="https://wiki.example.com",
    )
    with page_service.session_factory.begin() as session:
        admin_model = session.get(User, admin.user_id)
        assert admin_model is not None
        stale_generation = admin_model.session_generation
        admin_model.session_generation += 1

    with pytest.raises(PasswordRecoveryRecentReauthenticationRequiredError):
        recovery.issue_password_reset_for_user(
            target_user_id=target.user_id,
            delivery="link",
            requested_by_user_id=admin.user_id,
            requested_by_session_generation=stale_generation,
            actor_kind="human",
            actor_id=str(admin.user_id),
        )

    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 0


def test_admin_reset_generation_change_at_write_boundary_returns_exact_401(
    page_service,
    monkeypatch,
):
    client, admin, target = admin_client(page_service)
    assert client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    ).status_code == 200
    original_verify = (
        session_service_module.SessionService.verify_recent_reauthentication
    )

    def verify_then_revoke(self, *, proof_token, user_id):
        verified = original_verify(
            self,
            proof_token=proof_token,
            user_id=user_id,
        )
        with page_service.session_factory.begin() as session:
            admin_model = session.get(User, admin.user_id)
            assert admin_model is not None
            admin_model.session_generation += 1
        return verified

    monkeypatch.setattr(
        session_service_module.SessionService,
        "verify_recent_reauthentication",
        verify_then_revoke,
    )

    response = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json={
            "delivery": "link",
            "expires_in_seconds": 1800,
            "revoke_api_tokens": False,
        },
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "recent_reauthentication_required"
    with page_service.session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PasswordResetToken)
        ) == 0


def test_admin_reset_is_bound_to_target_user_id_not_reassigned_email(page_service):
    client, _admin, target = admin_client(page_service)
    users = UserAdminService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    )
    decoy = users.add_user(
        email="decoy@example.com",
        password="decoy-password",
    )
    with page_service.session_factory.begin() as session:
        target_model = session.get(User, target.user_id)
        decoy_model = session.get(User, decoy.user_id)
        assert target_model is not None and decoy_model is not None
        target_model.email = "renamed-target@example.com"
        session.flush()
        decoy_model.email = target.email
    assert client.post(
        "/api/v1/auth/reauthenticate",
        json={"current_password": "correct-password"},
    ).status_code == 200

    issued = client.post(
        f"/api/v1/admin/users/{target.user_id}/password-reset",
        json={
            "delivery": "link",
            "expires_in_seconds": 1800,
            "revoke_api_tokens": False,
        },
    )

    assert issued.status_code == 200, issued.text
    assert issued.json()["data"]["user_id"] == str(target.user_id)
    with page_service.session_factory() as session:
        token = session.scalar(
            select(PasswordResetToken).order_by(PasswordResetToken.created_at.desc())
        )
        assert token is not None and token.user_id == target.user_id
