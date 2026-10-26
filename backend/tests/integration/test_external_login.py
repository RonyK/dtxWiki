# Budget exception: core-owned login, linking, confirmation, browser binding and
# concurrent first-login writes have distinct authorization failure mechanisms.
# Existing traversals share fixtures and count loop/parameter inputs separately;
# protocol crypto stays in unit tests, and only SameSite gets one browser case.
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import tomlkit
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select

from backend.app import plugins
from backend.app.db import make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.auth_identity import AuthLoginTransaction, UserIdentity
from backend.models.auth_verification import (
    EmailReauthenticationChallenge,
)
from backend.models.base import Base
from backend.models.mail import EmailOutbox
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.services import server_admin_service
from backend.services.audit_service import AuditService
from backend.services.auth.broker import AuthBroker
from backend.services.auth.login_transactions import (
    AuthFlowError,
    LoginTransactionStore,
    digest,
)
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.credential_hashing import CredentialHasher
from backend.services.mail_delivery import EmailDeliveryService
from backend.services.session_service import (
    AuthRateLimitError,
    SessionService,
    build_session_token_codec,
)
from backend.spi.auth import ExternalAssertion
from backend.spi.plugins import DtxwikiPlugin
from backend.testing.fake_auth_method import FAKE_ISSUER, FakeAuthMethod, FakeSamlMethod
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

ORIGIN = "https://wiki.example.test"
CALLBACK = "/api/v1/auth/sso/company/callback"


def setup_client(page_service, tmp_path, monkeypatch, extra_methods=(), **policy):
    provider_file = tmp_path / "providers.toml"
    provider_file.write_text(
        tomlkit.dumps(
            {
                "providers": [
                    {
                        "slug": "company",
                        "method": "test_oidc",
                        "jit_provisioning": True,
                        "trust_email_verified": True,
                        **policy,
                    }
                ]
            }
        )
    )
    settings = Settings(
        public_base_url=ORIGIN,
        session_secret="external-login-integration-secret",
        auth_providers_file=str(provider_file),
        plugins="test_login",
    )
    method = FakeAuthMethod()
    plugin = DtxwikiPlugin(
        name="test_login",
        version="1.0",
        requires_core=">=0.1,<1",
        requires_spi=">=1.1,<2",
        auth_methods=lambda: [method, *extra_methods],
    )
    monkeypatch.setattr(
        plugins.metadata,
        "entry_points",
        lambda **_: [
            SimpleNamespace(name="test_login", load=lambda: plugin),
        ],
    )
    app = create_app(settings=settings, page_service=page_service)
    registry = app.state.auth_providers
    return TestClient(app, base_url=ORIGIN, follow_redirects=False), method, registry


def begin(client, *, path="/api/v1/auth/sso/company/start", **params):
    response = client.get(path, params=params)
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(FAKE_ISSUER + "/authorize?")
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def complete(client, state, code="valid"):
    return client.get(CALLBACK, params={"state": state, "code": code})


def configure_external_mail(client, registry, enabled):
    for settings in (client.app.state.settings, registry.settings):
        settings.mail_provider = "smtp" if enabled else "none"
        settings.mail_from_address = "wiki@example.test"
        settings.smtp_host = "smtp.example.test"


def deliver_confirmation(page_service, client, kind):
    messages = []
    delivery = EmailDeliveryService(
        settings=client.app.state.settings,
        provider=SimpleNamespace(send=messages.append),
    )
    with page_service.session_factory() as session:
        outbox = session.scalar(
            select(EmailOutbox)
            .where(EmailOutbox.message_kind == kind)
            .order_by(EmailOutbox.created_at.desc())
            .limit(1)
        )
        token = delivery.cipher.decrypt(
            outbox_id=outbox.id, message_kind=kind, envelope=outbox.encrypted_token
        )
        outbox_id = outbox.id
    delivery.deliver(session_factory=page_service.session_factory, outbox_id=outbox_id)
    assert len(messages) == 1
    url = next(
        line for line in messages[0].text_body.splitlines() if line.startswith(ORIGIN)
    )
    assert "token" not in urlsplit(url).query
    assert parse_qs(urlsplit(url).fragment)["token"] == [token]
    return token


def test_external_signup_mail_policy_and_browser_bound_confirmation(
    page_service, tmp_path, monkeypatch
):
    """Untrusted profile claims never reserve the account email namespace."""
    client, method, registry = setup_client(
        page_service, tmp_path, monkeypatch, trust_email_verified=False
    )
    assert complete(client, begin(client)).headers["location"] == "/dashboard"
    user = client.get("/api/v1/auth/me").json()["data"]
    user_id = UUID(user["user_id"])
    assert user["email"] is None and not user["has_password"]
    with page_service.session_factory() as session:
        identity = session.scalar(select(UserIdentity))
        assert identity.last_email == "fake@example.test"
        assert session.scalar(select(func.count()).select_from(EmailOutbox)) == 0
    method.assertions["collision"] = replace(
        method.assertions["valid"], subject="another-subject"
    )
    assert (
        complete(client, begin(client), "collision").headers["location"] == "/dashboard"
    )
    other_id = client.get("/api/v1/auth/me").json()["data"]["user_id"]
    assert other_id != str(user_id)
    method.assertions["missing"] = replace(
        method.assertions["valid"],
        subject="missing-email",
        email=None,
        display_name=None,
    )
    assert (
        complete(client, begin(client), "missing").headers["location"] == "/dashboard"
    )
    missing_user = client.get("/api/v1/auth/me").json()["data"]
    assert missing_user["email"] is None and missing_user["display_name"] == "User"
    # Extend this existing DB traversal for late ownership and its collision:
    # untrusted profile duplicates must not reserve an address, and promoting
    # one identity later must not stop another established identity's login.
    provider = registry.get("company")
    registry.providers["company"] = replace(
        provider,
        policy=provider.policy.model_copy(update={"trust_email_verified": True}),
    )
    method.assertions["missing"] = replace(
        method.assertions["missing"], email="fake@example.test"
    )
    assert (
        complete(client, begin(client), "missing").headers["location"] == "/dashboard"
    )
    promoted = client.get("/api/v1/auth/me").json()["data"]
    assert promoted["user_id"] == missing_user["user_id"]
    assert promoted["email"] == "fake@example.test"
    assert promoted["email_verified_at"] is not None

    # A conflicting later claim remains profile-only. Mail enrollment is an
    # explicit, same-session confirmation of another available account address.
    assert complete(client, begin(client)).headers["location"] == "/dashboard"
    unchanged = client.get("/api/v1/auth/me").json()["data"]
    assert unchanged["user_id"] == str(user_id) and unchanged["email"] is None
    endpoint = "/api/v1/auth/email-verification/request"
    rejected = client.post(
        endpoint, json={"email": "native@example.test"}, headers={"Origin": ORIGIN}
    )
    assert rejected.status_code == 422
    configure_external_mail(client, registry, True)
    assert client.get("/api/v1/auth/capabilities").json()["data"]["password_login"][
        "email_enrollment_enabled"
    ]
    sent = client.post(
        endpoint, json={"email": "native@example.test"}, headers={"Origin": ORIGIN}
    )
    assert sent.status_code == 202, sent.text
    token = deliver_confirmation(page_service, client, "email_verification")
    stranger = TestClient(client.app, base_url=ORIGIN, follow_redirects=False)
    confirm = "/api/v1/auth/email-verification/confirm"
    assert stranger.post(confirm, json={"token": token}).status_code == 401
    assert client.post(confirm, json={"token": token}).status_code == 200
    assert (
        client.get("/api/v1/auth/me").json()["data"]["email"] == "native@example.test"
    )
    assert client.post(confirm, json={"token": token}).status_code == 400
    installed = client.post(
        "/api/v1/auth/password/set",
        json={"new_password": "local-password"},
        headers={"Origin": ORIGIN},
    )
    assert installed.status_code == 200, installed.text
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "native@example.test", "password": "local-password"},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me").json()["data"]["user_id"] == str(user_id)


def test_sso_only_confirmation_policy_and_purpose_bound_email_proofs(
    page_service, tmp_path, monkeypatch
):
    """One step-up traversal covers session fallback, mail scope and revocation."""
    # This combined authorization traversal exceeds one minute's normal account
    # quota. Dedicated auth API tests own throttling; keep this failure boundary
    # focused on identity, purpose, expiry and revocation checks.
    monkeypatch.setattr(
        "backend.services.session_service.AUTH_RATE_LIMIT_PER_EMAIL", 100
    )

    client, method, registry = setup_client(
        page_service, tmp_path, monkeypatch, trust_email_verified=False
    )
    method.descriptor = replace(
        method.descriptor,
        capabilities=replace(method.descriptor.capabilities, reauthentication=False),
    )
    complete(client, begin(client))
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User)
            .join(UserIdentity)
            .where(UserIdentity.subject == "fake-subject")
        )
        user_id = user.id
        server_admin_service.grant_server_admin(session, user_id, granted_by="test")
    status = client.get("/api/v1/auth/verification").json()["data"]
    assert status["mode"] == "session" and status["verified"] is True
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 200
    )
    service = client.app.state.session_service
    primary = service.token_codec.verify_session(client.cookies.get("dtxwiki_session"))
    assert status["reauthenticated_until"] is not None
    assert status["allowed_methods"] == ["sso"]
    later = primary.authenticated_at + timedelta(seconds=3600)
    service = replace(
        service, token_codec=replace(service.token_codec, now=lambda: later)
    )
    client.app.state.session_service = service
    aged = client.get("/api/v1/auth/verification").json()["data"]
    assert aged["mode"] == "sso" and not aged["verified"]
    # No email or password is required, and cap=false suppresses forced IdP login.
    start = client.post(
        "/api/v1/auth/sso/company/reauthenticate", json={}, headers={"Origin": ORIGIN}
    )
    assert start.status_code == 200, start.text
    assert not method.requests[-1].force_reauthentication
    state = parse_qs(urlsplit(start.json()["data"]["redirect_url"]).query)["state"][0]
    assert complete(client, state).headers["location"] == "/account"
    assert client.get("/api/v1/auth/verification").json()["data"]["verified"]

    # Removing the source identity leaves the replacement external session
    # usable when another SSO method remains, even without a local password.
    registry.add({"slug": "backup", "method": "test_oidc"})
    method.assertions["backup"] = replace(
        method.assertions["valid"], subject="backup-subject"
    )
    started = client.post(
        "/api/v1/auth/sso/backup/link",
        json={"return_to": "/account"},
        headers={"Origin": ORIGIN},
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["data"]["redirect_url"]).query)["state"][0]
    assert (
        client.get(
            "/api/v1/auth/sso/backup/callback",
            params={"state": state, "code": "backup"},
        ).status_code
        == 302
    )
    identities = client.get("/api/v1/auth/identities").json()["data"]["identities"]
    source = next(row for row in identities if row["provider_slug"] == "company")
    assert (
        client.delete(
            f"/api/v1/auth/identities/{source['id']}", headers={"Origin": ORIGIN}
        ).status_code
        == 200
    )
    status = client.get("/api/v1/auth/verification").json()["data"]
    assert status["mode"] == "sso" and not status["verified"]
    start = client.post(
        "/api/v1/auth/sso/backup/reauthenticate", json={}, headers={"Origin": ORIGIN}
    )
    state = parse_qs(urlsplit(start.json()["data"]["redirect_url"]).query)["state"][0]
    assert (
        client.get(
            "/api/v1/auth/sso/backup/callback",
            params={"state": state, "code": "backup"},
        ).headers["location"]
        == "/account"
    )
    with page_service.session_factory.begin() as session:
        session.get(User, user_id).email = "fake@example.test"
    installed = client.post(
        "/api/v1/auth/password/set",
        json={"new_password": "local-password"},
        headers={"Origin": ORIGIN},
    )
    assert installed.status_code == 200, installed.text
    configure_external_mail(client, registry, True)
    service = client.app.state.session_service
    later = service.token_codec.now() + timedelta(seconds=3601)
    service = replace(
        service, token_codec=replace(service.token_codec, now=lambda: later)
    )
    client.app.state.session_service = service
    status = client.get("/api/v1/auth/verification").json()["data"]
    assert status["mode"] == "sso" and status["verified"] is False
    assert status["allowed_methods"] == ["password", "sso", "email"]
    request_path = "/api/v1/auth/email-reauthentication/request"
    confirm_path = "/api/v1/auth/email-reauthentication/confirm"
    assert (
        client.post(
            request_path,
            json={"scope": "account", "return_to": "/account"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )
    token = deliver_confirmation(page_service, client, "email_reauthentication")
    assert (
        client.post(
            confirm_path,
            json={"token": token, "scope": "admin"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 400
    )
    stranger = TestClient(client.app, base_url=ORIGIN, follow_redirects=False)
    stranger.cookies.set("dtxwiki_session", client.cookies.get("dtxwiki_session"))
    assert (
        stranger.post(
            confirm_path,
            json={"token": token, "scope": "account"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 400
    )
    # A second valid session of the same account/generation is still a
    # different browser authorization, even if the binding cookie is copied.
    with page_service.session_factory() as session:
        alternate = client.app.state.session_service.issue_session(
            session.get(User, user_id), amr=("ext",), idp="backup"
        )
    stranger.cookies.set("dtxwiki_session", alternate.session_token)
    stranger.cookies.set(
        "dtxwiki_email_reauth_account",
        client.cookies.get("dtxwiki_email_reauth_account"),
    )
    assert (
        stranger.post(
            confirm_path,
            json={"token": token, "scope": "account"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 400
    )
    with page_service.session_factory.begin() as session:
        challenge = session.scalar(select(EmailReauthenticationChallenge))
        deadline = challenge.expires_at
        challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        client.post(
            confirm_path,
            json={"token": token, "scope": "account"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 400
    )
    with page_service.session_factory.begin() as session:
        session.scalar(select(EmailReauthenticationChallenge)).expires_at = deadline
    with page_service.session_factory.begin() as session:
        user = session.get(User, user_id)
        user.email = "changed@example.test"
    assert (
        client.post(
            confirm_path,
            json={"token": token, "scope": "account"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 400
    )
    with page_service.session_factory.begin() as session:
        session.get(User, user_id).email = "fake@example.test"
    confirmed = client.post(
        confirm_path,
        json={"token": token, "scope": "account"},
        headers={"Origin": ORIGIN},
    )
    assert confirmed.status_code == 200, confirmed.text
    account_proof = client.cookies.get("dtxwiki_session_reauth_account")
    assert client.get("/api/v1/auth/verification").json()["data"]["verified"] is True
    # The cookie path is not the scope boundary: copying an account proof
    # into both admin paths still cannot authorize an admin operation.
    for path in ("/api/v1/admin", "/api/v1/auth/verification"):
        client.cookies.set(
            "dtxwiki_session_reauth",
            account_proof,
            domain="wiki.example.test",
            path=path,
        )
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 401
    )
    assert (
        client.get("/api/v1/auth/verification", params={"scope": "admin"}).json()[
            "data"
        ]["verified"]
        is False
    )
    assert (
        client.post(
            confirm_path,
            json={"token": token, "scope": "account"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 400
    )
    with page_service.session_factory.begin() as session:
        session.scalar(select(EmailReauthenticationChallenge)).created_at -= timedelta(
            seconds=61
        )
    assert (
        client.post(
            request_path,
            json={"scope": "admin", "return_to": "/account?tab=security"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )
    token = deliver_confirmation(page_service, client, "email_reauthentication")
    with page_service.session_factory.begin() as session:
        server_admin_service.revoke_server_admin(session, user_id)
    denied = client.post(
        confirm_path,
        json={"token": token, "scope": "admin"},
        headers={"Origin": ORIGIN},
    )
    assert (
        denied.status_code == 403
        and denied.json()["error_code"] == "server_admin_required"
    )
    with page_service.session_factory.begin() as session:
        server_admin_service.grant_server_admin(session, user_id, granted_by="test")
    result = client.post(
        confirm_path,
        json={"token": token, "scope": "admin"},
        headers={"Origin": ORIGIN},
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["return_to"] == "/account?tab=security"
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 200
    )
    # Purpose-specific confirmations coexist rather than overwriting each other.
    assert client.cookies.get("dtxwiki_session_reauth_account") == account_proof
    assert client.get("/api/v1/auth/verification").json()["data"]["verified"] is True
    assert (
        client.get("/api/v1/auth/verification", params={"scope": "admin"}).json()[
            "data"
        ]["verified"]
        is True
    )
    # Policy removal is distinct from proof expiry: a valid scoped proof keeps
    # its original deadline even when no method remains for another challenge.
    registry.providers.clear()
    monkeypatch.setattr(client.app.state.settings, "mail_provider", "none")
    assert client.get("/api/v1/admin/users").status_code == 200
    with page_service.session_factory.begin() as session:
        session.get(User, user_id).session_generation += 1
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 401
    )


def test_browser_bound_login_replay_account_and_oauth_return(
    page_service, tmp_path, monkeypatch
):
    client, method, registry = setup_client(page_service, tmp_path, monkeypatch)
    destination = "/oauth/authorize?client_id=sample&scope=mcp%3Aread%20mcp%3Awrite"
    state = begin(client, next=destination, remember_me=True)
    binding = client.cookies.get("dtxwiki_login_txn")

    def no_unbound_quota(*_args, **_kwargs):
        pytest.fail("an unbound callback consumed authentication quota")

    with monkeypatch.context() as patch:
        patch.setattr("backend.api.auth_sso.consume_auth_slot", no_unbound_quota)
        invalid = complete(client, "invalid-state")
    assert "sso_transaction_invalid" in invalid.headers["location"]
    assert "set-cookie" not in invalid.headers
    assert client.cookies.get("dtxwiki_login_txn") == binding
    with page_service.session_factory() as session:
        transaction = session.scalar(select(AuthLoginTransaction))
        assert transaction.state_digest == digest(state)
        assert transaction.binding_digest == digest(binding)
        assert transaction.return_to == destination
        assert (
            state not in transaction.state_digest
            and binding not in transaction.binding_digest
        )
    # A stolen callback URL cannot log another browser into the attacker's account.
    other = TestClient(client.app, base_url=ORIGIN, follow_redirects=False)
    assert "sso_transaction_invalid" in complete(other, state).headers["location"]
    assert not method.callbacks
    success = complete(client, state)
    assert success.headers["location"] == destination
    assert success.headers["cache-control"] == "no-store"
    assert (
        "HttpOnly" in success.headers["set-cookie"]
        and "Secure" in success.headers["set-cookie"]
    )
    user = client.get("/api/v1/auth/me").json()["data"]
    assert user["email"] == "fake@example.test" and not user["has_password"]
    assert user["email_verified_at"] is not None
    assert (
        user["session_method"] == "ext" and user["session_provider_slug"] == "company"
    )
    assert len(method.callbacks) == 1
    assert "dtxwiki_login_txn" not in success.headers.get("set-cookie", "")
    assert "sso_transaction_invalid" in complete(client, state).headers["location"]
    with page_service.session_factory() as session:
        native = session.get(User, UUID(user["user_id"]))
        assert native.personal_workspace_id is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(WorkspaceMembership.user_id == native.id)
            )
            == 1
        )
        identity = session.scalar(select(UserIdentity))
        assert identity.user_id == native.id and identity.linked_via == "jit"
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action.like("auth.%"))
        ).all()
        assert audit and all(
            row.workspace_id is None and row.retention_class == "sensitive"
            for row in audit
        )
        assert "fake@example.test" not in str([row.payload for row in audit])
    # An established identity stays bound even after the IdP email changes.
    method.assertions["valid"] = replace(
        method.assertions["valid"], email="changed@example.test", email_verified=False
    )
    assert complete(client, begin(client)).status_code == 302
    unchanged = client.get("/api/v1/auth/me").json()["data"]
    assert unchanged["user_id"] == user["user_id"]
    assert unchanged["email"] == "fake@example.test"
    # These two optional-profile inputs exercise missing and malformed claims;
    # both retain the last address and its matching unverified status, rather
    # than inheriting a verification flag from an absent/invalid new address.
    for profile_email in (None, "not-an-email"):
        method.assertions["valid"] = replace(
            method.assertions["valid"], email=profile_email, email_verified=True
        )
        assert complete(client, begin(client)).headers["location"] == "/dashboard"
        with page_service.session_factory() as session:
            identity = session.scalar(select(UserIdentity))
            assert identity.last_email == "changed@example.test"
            assert not identity.last_email_verified
    with page_service.session_factory.begin() as session:
        identity = session.scalar(select(UserIdentity))
        assert (
            identity.last_email == "changed@example.test"
            and not identity.last_email_verified
        )
        session.get(User, identity.user_id).status = "disabled"
    assert "account_disabled" in complete(client, begin(client)).headers["location"]
    caps = client.get("/api/v1/auth/capabilities").json()["data"]
    assert caps["login_methods"][0]["slug"] == "company"
    assert "issuer" not in str(caps)
    assert client.get("/api/v1/capabilities").json()["data"]["auth"] == caps
    # Browser errors preserve the original OAuth authorize request. A callback
    # throttled before exchange can recover only a browser-bound destination.
    throttled_state = begin(client, next=destination)

    def rate_limited(*_args, **_kwargs):
        raise AuthRateLimitError()

    with monkeypatch.context() as patch:
        patch.setattr("backend.api.auth_sso.consume_auth_slot", rate_limited)
        limited = complete(client, throttled_state)
        parsed = parse_qs(urlsplit(limited.headers["location"]).query)
        assert parsed == {"auth_error": ["rate_limited"], "next": [destination]}
        assert "set-cookie" not in limited.headers
        limited_start = client.get(
            "/api/v1/auth/sso/company/start", params={"next": destination}
        )
        assert limited_start.status_code == 302
        assert parse_qs(urlsplit(limited_start.headers["location"]).query) == parsed
    with page_service.session_factory() as session:
        failure = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "auth.sso.login.failed")
            .order_by(AuditEvent.created_at.desc())
        )
        assert failure.payload["reason"] == "rate_limited"
    # A restart can disable a provider while its transaction is still pending.
    # Recover only saved, fully bound routing; never trust request-supplied next.
    removed_state = begin(client, next=destination)
    removed = registry.providers.pop("company")
    unavailable = complete(client, removed_state)
    assert parse_qs(urlsplit(unavailable.headers["location"]).query) == {
        "auth_error": ["sso_provider_unavailable"],
        "next": [destination],
    }
    assert "next" not in parse_qs(
        urlsplit(complete(other, removed_state).headers["location"]).query
    )
    assert "next" not in parse_qs(
        urlsplit(complete(client, "invalid-state").headers["location"]).query
    )
    with page_service.session_factory.begin() as session:
        transaction = session.scalar(
            select(AuthLoginTransaction).where(
                AuthLoginTransaction.state_digest == digest(removed_state)
            )
        )
        transaction.consumed_at = datetime.now(UTC)
    assert "next" not in parse_qs(
        urlsplit(complete(client, removed_state).headers["location"]).query
    )
    with page_service.session_factory.begin() as session:
        transaction = session.scalar(
            select(AuthLoginTransaction).where(
                AuthLoginTransaction.state_digest == digest(removed_state)
            )
        )
        transaction.consumed_at = None
        transaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert "next" not in parse_qs(
        urlsplit(complete(client, removed_state).headers["location"]).query
    )
    registry.providers["company"] = removed
    # Disabled local login is enforced at the server, not just hidden by UI.
    client.app.state.settings.auth_password_login_enabled = False
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "fake@example.test", "password": "password"},
        ).status_code
        == 403
    )
    # SSO has no email identifier at start. Unrelated login attempts must not
    # share the ten-per-email quota (the existing IP ceiling still applies).
    for _ in range(11):
        begin(client)


def test_account_link_reauthentication_last_method_and_revocation(
    page_service, tmp_path, monkeypatch
):
    saml = FakeSamlMethod()
    # This combined authorization traversal exceeds one minute's normal account
    # quota. Dedicated auth API tests own throttling; keep this failure boundary
    # focused on identity, purpose, expiry and revocation checks.
    monkeypatch.setattr(
        "backend.services.session_service.AUTH_RATE_LIMIT_PER_EMAIL", 100
    )

    client, method, registry = setup_client(
        page_service, tmp_path, monkeypatch, extra_methods=[saml]
    )
    complete(client, begin(client))
    before = client.cookies.get("dtxwiki_session")
    service = client.app.state.session_service
    later = service.token_codec.now() + timedelta(seconds=3601)
    service = replace(
        service, token_codec=replace(service.token_codec, now=lambda: later)
    )
    client.app.state.session_service = service
    method.assertions["valid"] = replace(
        method.assertions["valid"], authenticated_at=later
    )
    original_complete = saml.complete
    saml.complete = lambda provider, callback: replace(
        original_complete(provider, callback), authenticated_at=later
    )
    identity = client.get("/api/v1/auth/identities").json()["data"]["identities"][0]
    assert not identity["can_unlink"]
    with page_service.session_factory.begin() as session:
        user_id = session.scalar(
            select(User.id)
            .join(UserIdentity)
            .where(UserIdentity.subject == "fake-subject")
        )
        server_admin_service.grant_server_admin(session, user_id, granted_by="test")
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 401
    )
    # A configured origin's spelling must not make equivalent browser origins
    # fail (the public URL itself remains unchanged for issuer contracts).
    client.app.state.settings.public_base_url = "https://WIKI.EXAMPLE.TEST:443"
    # Same-site sibling origins are refused, and a proof is required to link.
    assert (
        client.post("/api/v1/auth/sso/company/reauthenticate", json={}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/sso/company/link", json={}, headers={"Origin": ORIGIN}
        ).status_code
        == 401
    )
    method.assertions["old"] = replace(
        method.assertions["valid"],
        authenticated_at=datetime.now(UTC) - timedelta(hours=1),
    )

    def reauth(code="valid", *, post_callback=False):
        start = client.post(
            "/api/v1/auth/sso/company/reauthenticate",
            json={"return_to": "/account"},
            headers={"Origin": ORIGIN},
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["data"]["redirect_url"]).query)["state"][
            0
        ]
        if not post_callback:
            return complete(client, state, code)
        # A real cross-site POST omits native Lax cookies. The first hop must
        # echo bounded escaped fields without consuming state or calling IdP.
        external = TestClient(client.app, base_url=ORIGIN, follow_redirects=False)
        calls = len(method.callbacks)
        invalid = external.post(CALLBACK, data={str(i): "v" for i in range(21)})
        assert invalid.status_code == 302
        invalid = external.post(CALLBACK, data={"code": "x" * (64 * 1024)})
        assert invalid.status_code == 413
        first = external.post(
            CALLBACK,
            data={"state": state, "code": code, "extra": '"><script>alert(1)</script>'},
        )
        assert first.status_code == 200 and len(method.callbacks) == calls
        assert 'value="&quot;&gt;&lt;script&gt;' in first.text
        assert "'nonce-" in first.headers["content-security-policy"]
        assert "set-cookie" not in first.headers
        with page_service.session_factory() as session:
            assert (
                session.scalar(
                    select(AuthLoginTransaction.consumed_at).where(
                        AuthLoginTransaction.state_digest == digest(state)
                    )
                )
                is None
            )

        class Continuation(HTMLParser):
            action = None

            def __init__(self):
                super().__init__()
                self.fields = {}

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "form":
                    self.action = values["action"]
                elif tag == "input":
                    self.fields[values["name"]] = values["value"]

        form = Continuation()
        form.feed(first.text)
        assert form.action == CALLBACK + "/continue"
        assert client.post(form.action, data=form.fields).status_code == 403
        return client.post(form.action, data=form.fields, headers={"Origin": ORIGIN})

    failed = reauth("old")
    assert failed.headers["location"] == "/account?auth_error=reauthentication_mismatch"
    method.assertions["wrong"] = replace(
        method.assertions["valid"], subject="other-user"
    )
    assert "reauthentication_mismatch" in reauth("wrong").headers["location"]
    assert reauth(post_callback=True).headers["location"] == "/account"
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 200
    )
    refused = client.delete(
        f"/api/v1/auth/identities/{identity['id']}", headers={"Origin": ORIGIN}
    )
    assert (
        refused.status_code == 409
        and refused.json()["error_code"] == "last_login_method"
    )
    with page_service.session_factory.begin() as session:
        session.get(User, user_id).email = "native-linked@example.test"
    installed = client.post(
        "/api/v1/auth/password/set",
        json={"new_password": "new-password"},
        headers={"Origin": ORIGIN},
    )
    assert installed.status_code == 200, installed.text
    stale = TestClient(client.app, base_url=ORIGIN, follow_redirects=False)
    stale.cookies.set("dtxwiki_session", before)
    assert stale.get("/api/v1/auth/me").status_code == 401
    # Adding a password changed the generation and invalidated the old proof.
    assert (
        client.delete(
            f"/api/v1/auth/identities/{identity['id']}", headers={"Origin": ORIGIN}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/reauthenticate", json={"current_password": "new-password"}
        ).status_code
        == 200
    )

    def failed_unlink(*_args):
        raise RuntimeError("secret-from-provider-sdk")

    method.on_identity_unlinked = failed_unlink
    assert (
        client.delete(
            f"/api/v1/auth/identities/{identity['id']}", headers={"Origin": ORIGIN}
        ).status_code
        == 200
    )
    assert not client.get("/api/v1/auth/identities").json()["data"]["identities"]
    with page_service.session_factory() as session:
        failure = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.lifecycle.hook_failed",
            )
        )
        assert failure.payload == {
            "provider_slug": "company",
            "hook": "on_identity_unlinked",
        }
        assert failure.retention_class == "sensitive" and failure.workspace_id is None
    # Explicit linking requires a fresh password proof and binds to this user.
    assert (
        client.post(
            "/api/v1/auth/reauthenticate", json={"current_password": "new-password"}
        ).status_code
        == 200
    )
    start = client.post(
        "/api/v1/auth/sso/company/link",
        json={"return_to": "/account"},
        headers={"Origin": ORIGIN},
    )
    state = parse_qs(urlsplit(start.json()["data"]["redirect_url"]).query)["state"][0]
    assert complete(client, state).headers["location"] == "/account"
    with page_service.session_factory() as session:
        assert session.scalar(select(UserIdentity)).linked_via == "user_link"
    registry.add({"slug": "saml", "method": "test_saml"})
    # A natural SAML descriptor reaches the same POST callback path without
    # fake OIDC capabilities. An unusable request must not persist a row.
    with page_service.session_factory() as session:
        rows_before = session.scalar(
            select(func.count()).select_from(AuthLoginTransaction)
        )
    original_begin = saml.begin
    transactions = LoginTransactionStore(page_service.session_factory)
    for request_id, http_method in (
        (None, "GET"),
        ("", "GET"),
        ("x" * 513, "GET"),
        ("bad\nrequest", "GET"),
        ("non-ascii-é", "GET"),
        ("valid", "POST"),
    ):

        def malformed(
            provider, request, request_id=request_id, http_method=http_method
        ):
            return replace(
                original_begin(provider, request),
                protocol_request_id=request_id,
                http_method=http_method,
            )

        with monkeypatch.context() as patch:
            patch.setattr(saml, "begin", malformed)
            with pytest.raises(AuthFlowError, match="sso_provider_unavailable"):
                transactions.begin(registry.get("saml"), return_to="/account")
    with page_service.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(AuthLoginTransaction))
            == rows_before
        )

    def saml_finish(purpose, *, correlated=True):
        start = client.post(
            f"/api/v1/auth/sso/saml/{purpose}",
            json={"return_to": "/account"},
            headers={"Origin": ORIGIN},
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["data"]["redirect_url"]).query)[
            "RelayState"
        ][0]
        with page_service.session_factory() as session:
            request_id = session.scalar(
                select(AuthLoginTransaction.protocol_request_id).where(
                    AuthLoginTransaction.state_digest == digest(state)
                )
            )
        assert request_id and saml.requests[-1].nonce is None
        assert saml.requests[-1].code_challenge is None
        form = {
            "RelayState": state,
            "SAMLResponse": "valid",
            "InResponseTo": request_id if correlated else "wrong-request",
        }
        assert (
            client.post("/api/v1/auth/sso/saml/callback", data=form).status_code == 200
        )
        return client.post(
            "/api/v1/auth/sso/saml/callback/continue",
            data=form,
            headers={"Origin": ORIGIN},
        )

    assert (
        saml_finish("link", correlated=False).headers["location"]
        == "/account?auth_error=sso_assertion_invalid"
    )
    assert saml_finish("link").headers["location"] == "/account"
    assert saml_finish("reauthenticate").headers["location"] == "/account"
    assert saml.callbacks[-1].protocol_request_id
    assert saml.callbacks[-1].expected_nonce is None
    assert saml.callbacks[-1].pkce_verifier is None
    assert {
        row["provider_slug"]
        for row in client.get("/api/v1/auth/identities").json()["data"]["identities"]
    } == {"company", "saml"}


def test_account_decision_table_blocks_email_pre_hijack_and_policy_bypass(
    page_service, tmp_path, monkeypatch
):
    client, method, registry = setup_client(page_service, tmp_path, monkeypatch)
    # A trusted IdP email cannot silently merge with a native account or create
    # a second account. Ownership of the existing session is required to link.
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "fake@example.test", "password": "password"},
    )
    assert signup.status_code == 201
    native_id = signup.json()["data"]["user"]["user_id"]
    rejected = complete(client, begin(client))
    assert "account_link_required" in rejected.headers["location"]
    current = client.get("/api/v1/auth/me").json()["data"]
    assert current["email"] == "fake@example.test" and current["user_id"] == native_id
    with page_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UserIdentity)) == 0
        assert session.get(User, UUID(native_id)).email == "fake@example.test"
        assert session.get(User, UUID(native_id)).email_verified_at is None
    linked = client.post(
        "/api/v1/auth/sso/company/link",
        json={"return_to": "/account"},
        headers={"Origin": ORIGIN},
    )
    assert linked.status_code == 200, linked.text
    state = parse_qs(urlsplit(linked.json()["data"]["redirect_url"]).query)["state"][0]
    assert complete(client, state).headers["location"] == "/account"
    with page_service.session_factory() as session:
        identity = session.scalar(select(UserIdentity))
        assert (
            identity.user_id == UUID(native_id) and identity.linked_via == "user_link"
        )
        assert session.get(User, UUID(native_id)).email_verified_at is not None
    assert client.post("/api/v1/auth/logout").status_code == 200
    # Separate new identities exercise JIT, trust and domain gates.
    method.assertions["new"] = ExternalAssertion(
        issuer=FAKE_ISSUER, subject="new", email="new@example.test", email_verified=True
    )
    base = registry.get("company")
    for override, code in [
        ({"jit_provisioning": False}, "signup_not_allowed"),
        ({"allowed_email_domains": ["elsewhere.test"]}, "signup_not_allowed"),
    ]:
        registry.providers["company"] = replace(
            base, policy=base.policy.model_copy(update=override)
        )
        assert code in complete(client, begin(client), "new").headers["location"]
    registry.providers["company"] = base
    method.assertions["unverified"] = replace(
        method.assertions["new"], email_verified=False
    )
    # Trust/email policy is exercised below in the dedicated signup traversal;
    # here keep the same uncreated identity for issuer/hook rollback checks.
    method.assertions["badissuer"] = replace(
        method.assertions["new"], issuer="https://attacker.test"
    )
    assert (
        "sso_assertion_invalid"
        in complete(client, begin(client), "badissuer").headers["location"]
    )

    def failed_provision(*_args):
        raise RuntimeError("secret-from-provider-sdk")

    method.on_login = failed_provision
    rejected = complete(client, begin(client), "new")
    assert "sso_assertion_invalid" in rejected.headers["location"]
    assert "secret-from-provider-sdk" not in rejected.text
    with page_service.session_factory() as session:
        assert (
            session.scalar(select(UserIdentity.id).where(UserIdentity.subject == "new"))
            is None
        )
    # Optional IdP profile data must not make an otherwise valid identity
    # unusable. Native signup still enforces its own strict display-name policy.
    from uuid import uuid4

    from backend.scripts.seed_dev import seed_default_workspace
    from backend.spi.auth import LoginContext, ProvisioningResult, WorkspaceRoleRequest

    with page_service.session_factory.begin() as session:
        workspace_id = seed_default_workspace(session).id
    missing_workspace = uuid4()
    registry.providers["company"] = replace(
        base,
        policy=base.policy.model_copy(
            update={
                "provisioning_workspace_roles": {
                    str(workspace_id): ["viewer", "editor"],
                    str(missing_workspace): ["viewer"],
                },
            }
        ),
    )
    # Each rejected declaration must roll back native JIT and identity binding.
    for request in (
        WorkspaceRoleRequest(uuid4(), "viewer"),
        WorkspaceRoleRequest(workspace_id, "owner"),
        WorkspaceRoleRequest(missing_workspace, "viewer"),
    ):
        method.on_login = lambda _context, request=request: ProvisioningResult(
            (request,)
        )
        assert (
            "sso_assertion_invalid"
            in complete(client, begin(client), "new").headers["location"]
        )
        with page_service.session_factory() as session:
            assert (
                session.scalar(
                    select(UserIdentity.id).where(UserIdentity.subject == "new")
                )
                is None
            )

    def provision(context):
        assert isinstance(context, LoginContext)
        return ProvisioningResult((WorkspaceRoleRequest(workspace_id, "viewer"),))

    method.on_login = provision
    method.assertions["new"] = replace(method.assertions["new"], display_name="x" * 33)
    assert complete(client, begin(client), "new").headers["location"] == "/dashboard"
    current = client.get("/api/v1/auth/me").json()["data"]
    assert current["display_name"] == "User"
    assert current["email"] == "new@example.test"
    granted_user = UUID(current["user_id"])
    # Same-role requests are idempotent; a differing role preserves the existing
    # human grant and produces the explicit diagnostic instead of upgrading it.
    assert complete(client, begin(client), "new").headers["location"] == "/dashboard"
    method.on_login = lambda _context: ProvisioningResult(
        (WorkspaceRoleRequest(workspace_id, "editor"),)
    )
    assert complete(client, begin(client), "new").headers["location"] == "/dashboard"
    with page_service.session_factory() as session:
        assert (
            session.get(WorkspaceMembership, (granted_user, workspace_id)).role
            == "viewer"
        )
        preserved = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.provisioning.role_preserved"
            )
        )
        assert preserved.payload["requested_role"] == "editor"
        assert preserved.payload["existing_role"] == "viewer"
    # Reuse this two-account traversal for the explicit-link collision: proving
    # the new IdP is insufficient to claim another account's representative email.
    registry.add(
        {"slug": "backup", "method": "test_oidc", "trust_email_verified": True}
    )
    method.assertions["occupied"] = replace(
        method.assertions["valid"], subject="unbound-email-collision"
    )
    linked = client.post(
        "/api/v1/auth/sso/backup/link", json={}, headers={"Origin": ORIGIN}
    )
    assert linked.status_code == 200, linked.text
    state = parse_qs(urlsplit(linked.json()["data"]["redirect_url"]).query)["state"][0]
    rejected = client.get(
        "/api/v1/auth/sso/backup/callback",
        params={"state": state, "code": "occupied"},
    )
    assert "account_link_required" in rejected.headers["location"]
    with page_service.session_factory() as session:
        assert (
            session.scalar(
                select(UserIdentity).where(
                    UserIdentity.subject == "unbound-email-collision"
                )
            )
            is None
        )
        assert session.get(User, granted_user).email == "new@example.test"


@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_durable_transaction_and_first_login_have_one_database_winner(
    tmp_path, dialect
):
    # PostgreSQL uses row locks and unique-conflict retry; SQLite's database
    # writer lock alone cannot prove that independent concurrency mechanism.
    database_url = (
        f"sqlite+pysqlite:///{tmp_path / 'race.db'}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_CONCURRENCY_TEST_DATABASE_URL")
    )
    if not database_url or not database_url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL concurrency test database")
    engine = make_engine(database_url)
    assert not inspect(engine).get_table_names(), "concurrency database must be empty"
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    settings = Settings(
        public_base_url=ORIGIN, session_secret="concurrency-session-secret"
    )
    method = FakeAuthMethod()
    registry = ProviderRegistry(settings, [method])
    registry.add(
        {
            "slug": "company",
            "method": "test_oidc",
            "jit_provisioning": True,
            "trust_email_verified": True,
        }
    )
    sessions = SessionService(
        factory,
        CredentialHasher(),
        build_session_token_codec(settings),
        audit_service=AuditService(),
    )
    broker = AuthBroker(factory, registry, sessions)
    provider = registry.get("company")

    def pending():
        instruction, binding = broker.transactions.begin(
            provider, return_to="/dashboard"
        )
        state = parse_qs(urlsplit(instruction.url).query)["state"][0]
        return state, binding

    def finish(values):
        state, binding = values
        try:
            return broker.complete(
                "company",
                state=state,
                binding=binding,
                query={"state": state, "code": "valid"},
            )
        except AuthFlowError as error:
            return error.code

    try:
        values = pending()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, [values, values]))
        assert sum(result == "sso_transaction_invalid" for result in results) == 1
        assert len(method.callbacks) == 1
        # Two distinct login transactions race to create the same external account.
        method.assertions["valid"] = replace(
            method.assertions["valid"],
            subject="concurrent-new",
            email="concurrent@example.test",
        )
        with factory() as session:
            users_before = session.scalar(select(func.count()).select_from(User))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, [pending(), pending()]))
        assert results[0].session.user.user_id == results[1].session.user.user_id
        with factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(User))
                == users_before + 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(UserIdentity)
                    .where(UserIdentity.subject == "concurrent-new")
                )
                == 1
            )
        expired_state, expired_binding = pending()
        with factory.begin() as session:
            transaction = session.scalar(
                select(AuthLoginTransaction).where(
                    AuthLoginTransaction.state_digest == digest(expired_state)
                )
            )
            transaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert finish((expired_state, expired_binding)) == "sso_transaction_invalid"
        if dialect == "postgresql":
            with factory.begin() as session:
                old_owner = session.scalar(
                    select(User)
                    .join(UserIdentity)
                    .where(UserIdentity.subject == "concurrent-new")
                )
                new_owner = sessions.provision_user(
                    session, email="new-owner@example.test"
                )
                old_id, new_id = old_owner.id, new_owner.id
            moved = False

            def rebind_between_lookup_and_lock(_connection, _cursor, statement, *_args):
                nonlocal moved
                if moved or not statement.startswith("SELECT user_identities.user_id"):
                    return
                moved = True
                with factory.begin() as session:
                    session.get(User, old_id).session_generation += 1
                    identity = session.scalar(
                        select(UserIdentity).where(
                            UserIdentity.subject == "concurrent-new"
                        )
                    )
                    identity.user_id = new_id

            values = pending()
            event.listen(engine, "after_cursor_execute", rebind_between_lookup_and_lock)
            try:
                assert finish(values) == "sso_transaction_invalid"
            finally:
                event.remove(
                    engine, "after_cursor_execute", rebind_between_lookup_and_lock
                )
            assert moved
            assert finish(pending()).session.user.user_id == new_id
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
