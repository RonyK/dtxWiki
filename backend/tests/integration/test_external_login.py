import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from backend.models.base import Base
from backend.models.user import User
from backend.models.workspace import WorkspaceMembership
from backend.services import server_admin_service
from backend.services.audit_service import AuditService
from backend.services.auth.broker import AuthBroker
from backend.services.auth.login_transactions import AuthFlowError, digest
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.credential_hashing import CredentialHasher
from backend.services.session_service import SessionService, build_session_token_codec
from backend.spi.auth import ExternalAssertion
from backend.spi.plugins import DtxwikiPlugin
from backend.testing.fake_auth_method import FAKE_ISSUER, FakeAuthMethod
from backend.tests.utils.postgres import drop_reflected_postgres_metadata

ORIGIN = "https://wiki.example.test"
CALLBACK = "/api/v1/auth/sso/company/callback"


def setup_client(page_service, tmp_path, monkeypatch, **policy):
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
        auth_methods=lambda: [method],
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


def test_browser_bound_login_replay_account_and_oauth_return(
    page_service, tmp_path, monkeypatch
):
    client, method, registry = setup_client(page_service, tmp_path, monkeypatch)
    destination = "/oauth/authorize?client_id=sample&scope=mcp%3Aread%20mcp%3Awrite"
    state = begin(client, next=destination, remember_me=True)
    binding = client.cookies.get("dtxwiki_login_txn")
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
    assert (
        user["session_method"] == "ext" and user["session_provider_slug"] == "company"
    )
    assert len(method.callbacks) == 1
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
    assert client.get("/api/v1/auth/me").json()["data"]["email"] == "fake@example.test"
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
    client, method, registry = setup_client(page_service, tmp_path, monkeypatch)
    complete(client, begin(client))
    before = client.cookies.get("dtxwiki_session")
    identity = client.get("/api/v1/auth/identities").json()["data"]["identities"][0]
    assert not identity["can_unlink"]
    with page_service.session_factory.begin() as session:
        user_id = session.scalar(
            select(User.id).where(User.email == "fake@example.test")
        )
        server_admin_service.grant_server_admin(session, user_id, granted_by="test")
    assert (
        client.get(
            "/api/v1/admin/users", params={"query": "fake@example.test"}
        ).status_code
        == 401
    )
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

    def reauth(code="valid"):
        start = client.post(
            "/api/v1/auth/sso/company/reauthenticate",
            json={"return_to": "/account"},
            headers={"Origin": ORIGIN},
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["data"]["redirect_url"]).query)["state"][
            0
        ]
        return complete(client, state, code)

    assert "reauthentication_mismatch" in reauth("old").headers["location"]
    method.assertions["wrong"] = replace(
        method.assertions["valid"], subject="other-user"
    )
    assert "reauthentication_mismatch" in reauth("wrong").headers["location"]
    assert reauth().headers["location"] == "/account"
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


def test_account_decision_table_blocks_email_pre_hijack_and_policy_bypass(
    page_service, tmp_path, monkeypatch
):
    client, method, registry = setup_client(page_service, tmp_path, monkeypatch)
    # The same email cannot connect a new IdP to an unverified existing account.
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "fake@example.test", "password": "password"},
    )
    assert signup.status_code == 201
    registry.providers["company"] = replace(
        registry.get("company"),
        policy=registry.get("company").policy.model_copy(
            update={"link_policy": "auto_verified_email"}
        ),
    )
    assert (
        "account_link_required" in complete(client, begin(client)).headers["location"]
    )
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "fake@example.test"))
        user.email_verified_at = datetime.now(UTC)
    assert complete(client, begin(client)).headers["location"] == "/dashboard"
    with page_service.session_factory() as session:
        assert session.scalar(select(UserIdentity)).linked_via == "auto_verified_email"
    # Separate new identities exercise JIT, trust and domain gates.
    method.assertions["new"] = ExternalAssertion(
        issuer=FAKE_ISSUER, subject="new", email="new@example.test", email_verified=True
    )
    base = registry.get("company")
    for override, code in [
        ({"jit_provisioning": False}, "signup_not_allowed"),
        ({"trust_email_verified": False}, "email_unverified"),
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
    assert (
        "email_unverified"
        in complete(client, begin(client), "unverified").headers["location"]
    )
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
            session.scalar(select(User.id).where(User.email == "new@example.test"))
            is None
        )
    # Optional IdP profile data must not make an otherwise valid identity
    # unusable. Native signup still enforces its own strict display-name policy.
    del method.on_login
    method.assertions["new"] = replace(
        method.assertions["new"], display_name="x" * 33
    )
    assert complete(client, begin(client), "new").headers["location"] == "/dashboard"
    assert client.get("/api/v1/auth/me").json()["data"]["display_name"] == "new"


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
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, [pending(), pending()]))
        assert results[0].session.user.user_id == results[1].session.user.user_id
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.email == "concurrent@example.test")
                )
                == 1
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
                    select(User).where(User.email == "concurrent@example.test")
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
