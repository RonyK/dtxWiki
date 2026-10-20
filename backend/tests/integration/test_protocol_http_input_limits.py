from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from backend.api import mcp_http
from backend.app.container import DatabaseProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.oauth import OAuthConsent
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
        "/oauth/register", json={"client_name": "a" * 200, "redirect_uris": redirects}
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["redirect_uris"] == redirects
    for invalid in (redirects + ["https://callback.example/extra"], [uri + "a"]):
        response = client.post("/oauth/register", json={"redirect_uris": invalid})
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_client_metadata"


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_mcp_authorization_header_bound_precedes_auth(
    page_service, monkeypatch, method
):
    client = make_client(page_service)
    monkeypatch.setattr(mcp_http, "_authenticate_mcp_actor", fail)
    response = client.request(
        method, "/mcp", headers={"Authorization": "Bearer " + "a" * 20474}
    )
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


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


def test_authorize_query_limit_before_login_next(page_service):
    client = make_client(page_service)
    client.cookies.clear()
    response = client.get(
        "/oauth/authorize?padding=" + "a" * 16385, follow_redirects=False
    )
    assert response.status_code == 400
    assert "location" not in response.headers


def test_unknown_revoke_response_preserved_and_oversized_jwt_not_decoded(
    page_service, monkeypatch
):
    client = make_client(page_service)
    # Unknown, well-formed opaque values retain RFC7009's successful no-op.
    response = client.post("/oauth/revoke", data={"token": "unknown-token"})
    assert response.status_code == 200
    monkeypatch.setattr(oauth_service.jwt, "decode", fail)
    response = client.post("/oauth/revoke", data={"token": "a" * 16385})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "body", [[], {"client_name": 123, "redirect_uris": ["https://callback.example/cb"]}]
)
def test_dcr_json_types_are_rejected(page_service, body):
    response = make_client(page_service).post("/oauth/register", json=body)
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_client_metadata"


@pytest.mark.parametrize(
    ("endpoint", "body", "expected_error"),
    [
        (
            "/oauth/register",
            '{"redirect_uris":[],"redirect_uris":["https://callback.example/cb"]}',
            "invalid_client_metadata",
        ),
        (
            "/oauth/register",
            '{"client_name":123,"client_name":"valid name",'
            '"redirect_uris":["https://callback.example/cb"]}',
            "invalid_client_metadata",
        ),
        (
            "/oauth/authorize",
            '{"client_id":"one","client_id":"two"}',
            "invalid_request",
        ),
        (
            "/oauth/authorize",
            '{"workspace_ids":["one"],"workspace_ids":["two"]}',
            "invalid_request",
        ),
    ],
)
def test_oauth_duplicate_json_keys_rejected_before_service(
    page_service, monkeypatch, endpoint, body, expected_error
):
    client = make_client(page_service)
    monkeypatch.setattr("backend.api.oauth.oauth_service", fail)
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
        initial_workspace = user.personal_workspace_id
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
    rejected = client.patch(
        "/oauth/consents",
        params={"client_id": client_id},
        json={"workspace_ids": [str(value) for value in all_ids]},
    )
    assert rejected.status_code == 422
    with page_service.session_factory() as session:
        rows = session.scalars(
            select(OAuthConsent).where(OAuthConsent.client_id == client_id)
        ).all()
        assert {row.workspace_id for row in rows if row.removed_at is None} == all_ids
        assert all(row.refresh_ttl_seconds == 3600 for row in rows)
    assert (
        set(authenticate_bearer_token(request, tokens.access_token).token_workspace_ids)
        == all_ids
    )
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
    revoked = client.delete("/oauth/consents", params={"client_id": client_id})
    assert revoked.status_code == 200, revoked.text
    assert (
        client.get(
            "/api/v1/workspaces",
            headers={"Authorization": f"Bearer {tokens.access_token}"},
        ).status_code
        == 401
    )


def test_maximum_client_id_scope_and_issuer_claims_round_trip(page_service):
    client_id = "https://client.example/" + "a" * (
        2048 - len("https://client.example/")
    )
    public_base = "https://wiki.example/" + "a" * (2048 - len("https://wiki.example/"))
    scope = "mcp:read mcp:write mcp:agent offline_access"
    from backend.models.oauth import OAuthClient

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
