"""Integration tests for the instance (server-admin) deployment endpoint."""

from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.services import server_admin_service
from backend.services.credential_hashing import CredentialHasher
from backend.services.user_admin_service import UserAdminService


def make_client(
    page_service, *, email="test@example.com", server_admin=True, **overrides
) -> TestClient:
    # An https public_base_url would flag the session cookie Secure, which the
    # http TestClient won't echo back; pin it off so auth works under test.
    overrides.setdefault("session_cookie_secure", False)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret-for-deployment-api",
        **overrides,
    )
    client = TestClient(create_app(settings=settings, page_service=page_service))
    UserAdminService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    ).add_user(email=email, password="correct-password")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-password"},
    )
    assert login.status_code == 200, login.text
    # /admin/deployment is server-admin only (instance scope, not workspace
    # role). Grant it the way `dtxwiki admin grant` / setup does.
    if server_admin:
        user_id = UUID(client.get("/api/v1/auth/me").json()["data"]["user_id"])
        with page_service.session_factory.begin() as session:
            server_admin_service.grant_server_admin(
                session, user_id, granted_by="test"
            )
    return client


def test_deployment_status_returns_current_config_for_server_admin(page_service):
    client = make_client(
        page_service,
        public_base_url="https://wiki.example.com",
        app_mode="team",
        behind_proxy=True,
        mail_provider="smtp",
        mail_from_address="wiki@example.com",
        smtp_host="smtp.example.com",
        smtp_username="mailer",
        smtp_password="smtp-secret",
    )

    response = client.get("/api/v1/admin/deployment")
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["public_base_url"] == "https://wiki.example.com"
    assert data["mcp_url"] == "https://wiki.example.com/mcp"
    assert data["app_mode"] == "team"
    assert data["behind_proxy"] is True
    assert data["https_configured"] is True
    assert data["tls_mode"] == "acme"
    assert data["oauth_available"] is True
    assert data["oauth_unavailable_reason"] is None
    # MCP-10: DCR is advisory, not a hard OAuth-availability gate.
    assert data["oauth_dcr_enabled"] is True
    assert data["oauth_token_lifetimes"] == {
        "access_token": {
            "seconds": 60 * 60,
            "source": "built-in default",
        },
        "refresh_token": {
            # MCP-05: restored to the pre-branch 30-day default (was
            # silently widened to 180 days).
            "seconds": 60 * 60 * 24 * 30,
            "source": "built-in default",
        },
        "refresh_sliding": {
            "enabled": True,
            "source": "built-in default",
        },
    }
    assert data["env_file_hint"] == "/etc/dtxwiki/dtxwiki.env"
    assert data["signup"] == {
        "configured": None,
        "effective": False,
        "source": "mode_default",
        "mode_defaults": {
            "solo_local": True,
            "solo_remote": False,
            "team": False,
        },
        "restart_required": True,
        "impact": "new_signups_only",
        "systemd_command": (
            "sudo dtxwiki config set DTXWIKI_SIGNUP_ENABLED=true\n"
            "sudo systemctl restart dtxwiki dtxwiki-worker"
        ),
        "compose_command": (
            "# infra/prod/.env\n"
            "DTXWIKI_SIGNUP_ENABLED=true\n"
            "docker compose up -d --force-recreate app worker"
        ),
    }
    recovery = data["recovery"]
    assert recovery["mode"] == "email"
    assert recovery["admin_assisted"] is True
    assert recovery["link_delivery_available"] is True
    assert recovery["mail_provider"] == "smtp"
    assert recovery["mail_status"] == "configured"
    assert recovery["mail_connection_status"] == "not_checked"
    assert recovery["mail_last_attempt_at"] is None
    assert recovery["mail_from_address"] == "wiki@example.com"
    assert recovery["smtp_credentials_configured"] is True
    assert recovery["canonical_public_url"] == "https://wiki.example.com"
    assert recovery["https_ready"] is True
    assert recovery["outbox_queued"] == 0
    assert recovery["outbox_failed"] == 0
    assert recovery["worker_required"] is True
    assert "DTXWIKI_MAIL_PROVIDER=smtp" in recovery["systemd_command"]
    assert "docker compose" in recovery["compose_command"]
    assert isinstance(data["health_components"], dict)
    assert "oauth_jwk" in data["health_components"]
    # Suggested reconfig commands are contextualized to the current domain.
    assert data["suggested_commands"]
    assert any(
        "config set" in cmd["command"] or "setup production" in cmd["command"]
        for cmd in data["suggested_commands"]
    )
    assert any(
        cmd["blast_radius"] == "session_invalidation"
        for cmd in data["suggested_commands"]
    )

    # Regression guard (review L10): the read-only status must NEVER surface a
    # secret. Neither the session secret nor the DB URL may appear anywhere in
    # the body, and no secret-bearing field may be present in the payload.
    body = response.text
    assert "test-session-secret" not in body
    assert "smtp-secret" not in body
    assert "sqlite+pysqlite" not in body  # DATABASE_URL must not be echoed
    for forbidden in (
        "session_secret",
        "database_url",
        "oauth_jwt_private_key_pem",
        "oauth_jwt_private_key_path",
        "metrics_bearer_token",
    ):
        assert forbidden not in data


def test_deployment_status_surfaces_tls_not_ready_without_https(page_service):
    client = make_client(page_service, public_base_url=None)

    response = client.get("/api/v1/admin/deployment")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["https_configured"] is False
    assert data["public_base_url"] is None
    assert data["mcp_url"] is None
    # Without an https issuer, OAuth cannot be offered.
    assert data["oauth_available"] is False
    assert data["oauth_unavailable_reason"] is not None
    assert data["recovery"]["mode"] == "disabled"
    assert data["recovery"]["link_delivery_available"] is False
    assert data["recovery"]["worker_required"] is False


def test_deployment_status_distinguishes_explicit_signup_value(page_service):
    client = make_client(page_service, app_mode="team", signup_enabled=True)

    response = client.get("/api/v1/admin/deployment")

    assert response.status_code == 200
    signup = response.json()["data"]["signup"]
    assert signup["configured"] is True
    assert signup["effective"] is True
    assert signup["source"] == "explicit_environment"
    assert "DTXWIKI_SIGNUP_ENABLED=false" in signup["systemd_command"]


def test_deployment_status_requires_authentication(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    # No signup/login: anonymous request must be rejected.
    response = client.get("/api/v1/admin/deployment")
    assert response.status_code in {401, 403}


def test_deployment_status_forbidden_for_non_server_admin(page_service):
    # Authenticated but NOT granted server-admin → 403.
    client = make_client(
        page_service, email="member@example.com", server_admin=False
    )
    response = client.get("/api/v1/admin/deployment")
    assert response.status_code == 403, response.text
