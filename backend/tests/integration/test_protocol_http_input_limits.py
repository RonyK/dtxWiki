from __future__ import annotations

import time
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from backend.api import mcp_http
from backend.app.container import DatabaseProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.oauth import OAuthClient, OAuthConsent
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.auth import authenticate_bearer_token
from backend.services import oauth_service
from backend.tests.conftest import authenticate_test_client


def make_client(page_service):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="protocol-input-test-secret",
                public_base_url="http://testserver",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client)
    return client


def fail(*args, **kwargs):
    pytest.fail("oversized input reached authentication or token parsing")


def test_dcr_20_callbacks_and_2048_byte_uri_round_trip(page_service):
    client = make_client(page_service)
    prefix = "https://callback.example/"
    uri = prefix + "a" * (2048 - len(prefix))
    redirects = [uri] + [f"https://callback.example/{index}" for index in range(19)]
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "a" * 200,
            "redirect_uris": redirects,
            "token_endpoint_auth_method": "client_secret_basic",
            "application_type": None,
            "scope": None,
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["redirect_uris"] == redirects
    assert registered.json()["token_endpoint_auth_method"] == "none"
    assert registered.json()["scope"] == "mcp:read"
    with page_service.session_factory() as session:
        row = session.get(OAuthClient, registered.json()["client_id"])
        assert row.application_type == "native"
        assert row.token_endpoint_auth_method == "none"
    custom = client.post(
        "/oauth/register",
        json={
            "redirect_uris": redirects,
            "application_type": "x" * 50_000,
            "client_name": None,
        },
    )
    assert custom.json()["client_name"] == "Dynamic MCP Client"
    with page_service.session_factory() as session:
        row = session.get(OAuthClient, custom.json()["client_id"])
        assert row.application_type == "native"
    web = client.post(
        "/oauth/register",
        json={"redirect_uris": redirects, "application_type": "web"},
    )
    assert web.json()["application_type"] == "web"


def test_mcp_bearer_envelope_on_every_transport_method(page_service, monkeypatch):
    """Each MCP method bounds the header before auth and strips bearer padding."""
    client = make_client(page_service)
    received = []

    def authenticate(request, token):
        received.append(token)
        return JSONResponse({"authenticated": True})

    monkeypatch.setattr(mcp_http, "_authenticate_mcp_actor", authenticate)
    for method in ("POST", "GET", "DELETE"):
        oversized = client.request(
            method, "/mcp", headers={"Authorization": "Bearer " + "a" * 20474}
        )
        assert oversized.status_code == 401
        assert "WWW-Authenticate" in oversized.headers
        padded = client.request(
            method, "/mcp", headers={"Authorization": "bEaReR  \tclient-token\t "}
        )
        assert padded.status_code == 200
    assert received == ["client-token"] * 3


def test_rest_header_bound_precedes_token_authentication(page_service, monkeypatch):
    from backend.policy import auth

    client = make_client(page_service)
    monkeypatch.setattr(auth, "_authenticate_api_token", fail)
    response = client.get(
        "/api/v1/workspaces", headers={"Authorization": "Bearer " + "a" * 20474}
    )
    assert response.status_code == 401


def test_token_input_limit_precedes_rate_limit_and_jwt_parsing(
    page_service, monkeypatch
):
    client = make_client(page_service)
    monkeypatch.setattr("backend.api.oauth.consume_token_slot", fail)
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "test",
            "refresh_token": "a" * 1025,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_authorize_query_limit_before_login_next(page_service, monkeypatch):
    client = make_client(page_service)
    client.cookies.clear()
    monkeypatch.setattr("starlette.requests.QueryParams", fail)
    response = client.get(
        "/oauth/authorize?padding=" + "a" * 16385, follow_redirects=False
    )
    assert response.status_code == 400
    assert "location" not in response.headers


def test_oversized_revoke_token_is_an_authenticated_no_op_without_jwt_decode(
    page_service, monkeypatch
):
    client = make_client(page_service)
    monkeypatch.setattr(oauth_service.jwt, "decode", fail)
    # RFC 7009: an oversized token is an unknown token, not a 400.
    response = client.post("/oauth/revoke", data={"token": "a" * 16385})
    assert response.status_code == 200, response.text
    for body in ("token=", "token=%FF"):
        response = client.post(
            "/oauth/revoke",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 400
    client.cookies.clear()
    response = client.post("/oauth/revoke", data={"token": "a" * 16385})
    assert response.status_code == 401


def test_dcr_deeply_nested_json_consumes_one_slot_before_parsing(
    page_service, monkeypatch
):
    client = make_client(page_service)
    oauth_service._DCR_BUCKET["testclient"] = [time.monotonic()] * 19
    response = client.post(
        "/oauth/register",
        content=b"[" * 20_000 + b"]" * 20_000,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    monkeypatch.setattr("backend.api.oauth._parse_unique_json_body", fail)
    limited = client.post("/oauth/register", content=b"{}")
    assert limited.status_code == 400
    assert "rate limit" in limited.json()["message"]


def test_legacy_oversized_cimd_id_is_manageable_via_consent_endpoints(page_service):
    client = make_client(page_service)
    client_id = "https://client.example/" + "한" * 700
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        session.add(
            OAuthClient(
                id=client_id,
                client_name="Legacy client",
                redirect_uris=["https://client.example/cb"],
                scope="mcp:read",
                created_via="cimd",
            )
        )
        session.flush()
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=client_id,
                workspace_id=user.default_workspace_id,
                scope="mcp:read",
            )
        )
    updated = client.patch(
        "/oauth/consents",
        params={"client_id": client_id},
        json={"refresh_ttl_seconds": 3600},
    )
    assert updated.status_code == 200, updated.text
    revoked = client.delete("/oauth/consents", params={"client_id": client_id})
    assert revoked.status_code == 200, revoked.text
    with page_service.session_factory() as session:
        assert session.get(OAuthClient, client_id).revoked_at is not None


def test_repeated_resources_survive_consent_and_duplicate_singleton_fails(
    page_service,
):
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register", json={"redirect_uris": ["https://client.example/cb"]}
    )
    params = [
        ("client_id", registered.json()["client_id"]),
        ("response_type", "code"),
        ("redirect_uri", "https://client.example/cb"),
        ("state", "resource-consent-state-0123456789"),
        ("scope", "mcp:read"),
        ("code_challenge", oauth_service.challenge_s256("v" * 43)),
        ("code_challenge_method", "S256"),
        ("resource", "https://resource.example/one"),
        ("resource", "https://resource.example/two"),
    ]
    duplicate = params + [("state", "another-state-0123456789")]
    assert client.get("/oauth/authorize", params=duplicate).status_code == 400
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200, consent.text
    assert consent.text.count('name="resource"') == 2
    with page_service.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        workspace_id = str(user.default_workspace_id)
    submitted = client.post(
        "/oauth/authorize",
        content=urlencode(
            params + [("decision", "allow"), ("workspace_ids", workspace_id)]
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303, submitted.text
    assert "code=" in submitted.headers["location"]


def test_oauth_duplicate_json_keys_rejected_before_service(page_service, monkeypatch):
    client = make_client(page_service)
    monkeypatch.setattr("backend.api.oauth.oauth_service", fail)
    for endpoint, body, expected_error in (
        (
            "/oauth/register",
            '{"redirect_uris":[],"redirect_uris":["https://callback.example/cb"]}',
            "invalid_client_metadata",
        ),
        (
            "/oauth/authorize",
            '{"client_id":"one","client_id":"two"}',
            "invalid_request",
        ),
    ):
        response = client.post(
            endpoint, content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == expected_error


def test_existing_257_oauth_grants_survive_auth_readback_and_ttl_only_update(
    page_service,
):
    client = make_client(page_service)
    registration = client.post(
        "/oauth/register",
        json={
            "client_name": "Legacy large grants",
            "redirect_uris": ["https://callback.example/cb"],
        },
    ).json()
    client_id = registration["client_id"]
    with page_service.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        user_id = user.id
        initial_workspace = user.default_workspace_id
    service = oauth_service.OAuthService(
        session_factory=page_service.session_factory,
        settings=client.app.state.settings,
        public_base_url="http://testserver",
        database_provider=DatabaseProvider(
            None, session_factory=page_service.session_factory
        ),
    )
    code = service.issue_authorization_code(
        user_id=user_id,
        client_id=client_id,
        redirect_uri="https://callback.example/cb",
        code_challenge=oauth_service.challenge_s256("v" * 43),
        code_challenge_method="S256",
        scope="mcp:read",
        state="legacy-grants-state-0123456789",
        workspace_ids=[initial_workspace],
    )
    tokens = service.exchange_authorization_code(
        code=code,
        client_id=client_id,
        redirect_uri="https://callback.example/cb",
        code_verifier="v" * 43,
    )
    with page_service.session_factory.begin() as session:
        workspace_ids = [uuid4() for _ in range(256)]
        session.add_all(
            [
                Workspace(
                    id=workspace_id,
                    slug=f"legacy-grant-{index}",
                    title=f"Legacy {index}",
                    name=f"Legacy {index}",
                    goal="Legacy grant test",
                )
                for index, workspace_id in enumerate(workspace_ids)
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    workspace_id=workspace_id, user_id=user_id, role="owner"
                )
                for workspace_id in workspace_ids
            ]
        )
        session.add_all(
            [
                OAuthConsent(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    client_id=client_id,
                    scope="mcp:read",
                )
                for workspace_id in workspace_ids
            ]
        )
    all_ids = {initial_workspace, *workspace_ids}
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/api/v1/workspaces",
            "query_string": b"",
            "headers": [],
            "app": client.app,
        }
    )
    actor = authenticate_bearer_token(request, tokens.access_token)
    assert set(actor.token_workspace_ids) == all_ids
    listing = client.get("/oauth/consents")
    assert listing.status_code == 200, listing.text
    assert {entry["id"] for entry in listing.json()["data"][0]["workspaces"]} == {
        str(value) for value in all_ids
    }
    updated = client.patch(
        "/oauth/consents",
        params={"client_id": client_id},
        json={"refresh_ttl_seconds": 3600},
    )
    assert updated.status_code == 200, updated.text
    assert len(updated.json()["data"]["workspaces"]) == 257
    with page_service.session_factory() as session:
        rows = session.scalars(
            select(OAuthConsent).where(OAuthConsent.client_id == client_id)
        ).all()
        assert {row.workspace_id for row in rows if row.removed_at is None} == all_ids
        assert all(row.refresh_ttl_seconds == 3600 for row in rows)
    selected_ids = list(all_ids)[:256]
    replaced = client.patch(
        "/oauth/consents",
        params={"client_id": client_id},
        json={"workspace_ids": [str(value) for value in selected_ids]},
    )
    assert replaced.status_code == 200, replaced.text
    assert set(
        authenticate_bearer_token(request, tokens.access_token).token_workspace_ids
    ) == set(selected_ids)


def test_maximum_client_id_scope_and_issuer_claims_round_trip(page_service):
    client_id = "https://client.example/" + "a" * (
        2048 - len("https://client.example/")
    )
    public_base = "https://wiki.example/" + "a" * (2048 - len("https://wiki.example/"))
    scope = "mcp:read mcp:write mcp:agent offline_access"
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).limit(1))
        user = User(
            email="max-claims@example.com",
            display_name="Maximum claims",
            password_hash="unused",
            status="active",
        )
        session.add(user)
        session.add(
            OAuthClient(
                id=client_id,
                client_name="이" * 200,
                redirect_uris=["https://callback.example/cb"],
                scope=scope,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.flush()
        user_id, workspace_id = user.id, workspace.id
        session.add(
            WorkspaceMembership(
                user_id=user_id, workspace_id=workspace_id, role="owner"
            )
        )
    service = oauth_service.OAuthService(
        session_factory=page_service.session_factory,
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            public_base_url=public_base,
            session_secret="max-claims-test-secret",
        ),
        public_base_url=public_base,
        database_provider=DatabaseProvider(
            None, session_factory=page_service.session_factory
        ),
    )
    code = service.issue_authorization_code(
        user_id=user_id,
        client_id=client_id,
        redirect_uri="https://callback.example/cb",
        code_challenge=oauth_service.challenge_s256("v" * 128),
        code_challenge_method="S256",
        scope=scope,
        state=";" * 512,
        workspace_ids=[workspace_id],
    )
    tokens = service.exchange_authorization_code(
        code=code,
        client_id=client_id,
        redirect_uri="https://callback.example/cb",
        code_verifier="v" * 128,
    )
    assert len(tokens.access_token.encode()) <= 16 * 1024
    actor = service.verify_access_token(tokens.access_token)
    assert actor.client_id == client_id
    assert set(actor.scopes) == set(scope.split())
