"""Hosted Claude and ChatGPT OAuth compatibility characterization.

O3 advertises and carries ``offline_access`` as a protocol-only scope while
preserving the pre-O3 behavior that refresh-token issuance is unconditional.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.services.oauth_service import challenge_s256, redirect_uri_matches
from backend.tests.conftest import authenticate_test_client
from backend.tests.integration.test_oauth_mcp_http import module_scoped_page_service

pytestmark = pytest.mark.oauth_flow

ISSUER = "https://wiki.example.test"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_REDIRECT = "https://chatgpt.com/connector/oauth/hosted-client-test"


def make_app_client(page_service, *, public_base_url: str = ISSUER) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                session_cookie_secure=False,
                public_base_url=public_base_url,
                mcp_http_mode="json_stateless",
            ),
            page_service=page_service,
        )
    )


def make_client(page_service, *, public_base_url: str = ISSUER) -> TestClient:
    client = make_app_client(page_service, public_base_url=public_base_url)
    authenticate_test_client(client)
    return client


@pytest.fixture(scope="module")
def shared_client(_empty_sqlite_schema):
    """One signed-in client for the redirect-matching cases.

    They register clients under their own names and redirects, so no case can
    inherit another's consent, and none counts rows or audit events.
    """

    with module_scoped_page_service(_empty_sqlite_schema) as page_service:
        yield make_client(page_service)


def register_client(
    client: TestClient,
    redirect_uri: str,
    *,
    name: str = "Hosted OAuth Characterization",
    scope: str = "mcp:read",
) -> dict:
    response = client.post(
        "/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "scope": scope,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def current_workspace_id(client: TestClient) -> str:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200, response.text
    return response.json()["data"]["current_workspace_id"]


def authorization_params(
    client_id: str,
    redirect_uri: str,
    verifier: str,
    *,
    state: str,
    scope: str = "mcp:read",
) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": scope,
        "state": f"{state}-0123456789abcdef",
    }


def submit_authorization(
    client: TestClient,
    params: dict[str, str],
    *,
    decision: str = "allow",
):
    consent = client.get(
        "/oauth/authorize",
        params=params,
        headers={"accept": "text/html"},
    )
    assert consent.status_code == 200, consent.text
    response = client.post(
        "/oauth/authorize",
        data={
            **params,
            # P10.3 T3: the checklist submits workspace_ids (repeatable),
            # not a singular workspace_id.
            "workspace_ids": current_workspace_id(client),
            "decision": decision,
        },
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


def authorize_and_exchange(
    client: TestClient,
    registration: dict,
    redirect_uri: str,
    *,
    state: str,
    scope: str = "mcp:read",
) -> dict:
    verifier = "v" * 64
    params = authorization_params(
        registration["client_id"],
        redirect_uri,
        verifier,
        state=state,
        scope=scope,
    )
    authorization = submit_authorization(client, params)
    authorization_query = parse_qs(
        urlparse(authorization.headers["location"]).query
    )
    code = authorization_query["code"][0]
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def listed_tool_names(client: TestClient, access_token: str) -> set[str]:
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in response.json()["result"]["tools"]}


@pytest.mark.parametrize(
    ("registered_redirect", "runtime_redirect"),
    [
        pytest.param(CLAUDE_REDIRECT, CLAUDE_REDIRECT, id="claude-hosted"),
        pytest.param(CHATGPT_REDIRECT, CHATGPT_REDIRECT, id="chatgpt-hosted"),
        pytest.param(
            "http://127.0.0.1:49152/callback",
            "http://127.0.0.1:61234/callback",
            id="rfc8252-ipv4-alternate-port",
        ),
        pytest.param(
            "http://[::1]:49152/callback",
            "http://[::1]:61234/callback",
            id="rfc8252-ipv6-alternate-port",
        ),
    ],
)
def test_dcr_and_code_exchange_accept_hosted_and_loopback_redirects(
    shared_client,
    request,
    registered_redirect: str,
    runtime_redirect: str,
):
    client = shared_client

    # A distinct registration identity per case, so consent granted to one
    # case cannot be inherited by another.
    registration = register_client(
        client,
        registered_redirect,
        name=f"Hosted OAuth {request.node.callspec.id}",
    )
    assert registration["redirect_uris"] == [registered_redirect]

    tokens = authorize_and_exchange(
        client,
        registration,
        runtime_redirect,
        state="accepted-redirect",
    )
    assert tokens["access_token"]
    assert tokens["refresh_token"]


UNSAFE_REDIRECT_URIS = [
    pytest.param(
        "http://example.com/callback",
        id="non-loopback-http",
    ),
    pytest.param(
        "https://user:pw@claude.ai/api/mcp/auth_callback",
        id="userinfo",
    ),
    pytest.param(
        "https://claude.ai/api/mcp/auth_callback#fragment",
        id="fragment",
    ),
    pytest.param(
        r"https://claude.ai\evil.example/callback",
        id="host-backslash",
    ),
    pytest.param(
        r"https://claude.ai/api\mcp/auth_callback",
        id="path-backslash",
    ),
    pytest.param(
        "https://claude.ai./api/mcp/auth_callback",
        id="trailing-dot-host",
    ),
]


@pytest.mark.parametrize("redirect_uri", UNSAFE_REDIRECT_URIS)
def test_dcr_and_runtime_matcher_reject_the_same_unsafe_redirects(
    shared_client,
    redirect_uri: str,
):
    client = shared_client

    registration = client.post(
        "/oauth/register",
        json={
            "client_name": "Unsafe Hosted Redirect",
            "redirect_uris": [redirect_uri],
            "scope": "mcp:read",
        },
    )

    assert registration.status_code == 400, registration.text
    assert registration.json()["error_code"] == "invalid_client_metadata"
    # Defense in depth for legacy or manually seeded registrations: runtime
    # matching must apply the same safety rule before its exact-match shortcut.
    assert redirect_uri_matches(redirect_uri, [redirect_uri]) is False


def test_runtime_loopback_port_relaxation_cannot_bypass_userinfo_rejection(
    shared_client,
):
    client = shared_client
    registration = register_client(
        client,
        "http://127.0.0.1:49152/callback",
        name="Loopback Userinfo Rejection",
    )
    requested = "http://user:pw@127.0.0.1:61234/callback"
    params = authorization_params(
        registration["client_id"],
        requested,
        "v" * 64,
        state="userinfo-port-bypass",
    )

    response = client.get(
        "/oauth/authorize",
        params=params,
        headers={"accept": "application/json"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "invalid_request"
    assert redirect_uri_matches(
        requested,
        ["http://127.0.0.1:49152/callback"],
    ) is False


def test_runtime_https_redirect_port_must_match_exactly(shared_client):
    client = shared_client
    registration = register_client(
        client,
        "https://claude.ai:443/api/mcp/auth_callback",
        name="HTTPS Exact Port",
    )
    params = authorization_params(
        registration["client_id"],
        "https://claude.ai:444/api/mcp/auth_callback",
        "v" * 64,
        state="https-port-mismatch",
    )

    response = client.get(
        "/oauth/authorize",
        params=params,
        headers={"accept": "application/json"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "invalid_request"


@pytest.mark.parametrize(
    ("public_base_url", "expected_resource"),
    [
        pytest.param(
            "https://wiki.example.test",
            "https://wiki.example.test/mcp",
            id="bare-host",
        ),
        pytest.param(
            "https://proxy.example.test/dtxwiki",
            "https://proxy.example.test/dtxwiki/mcp",
            id="reverse-proxy-path-prefix",
        ),
    ],
)
def test_protected_resource_metadata_preserves_public_base_url_path(
    page_service,
    public_base_url: str,
    expected_resource: str,
):
    # The metadata route is unauthenticated: no sign-in needed.
    client = make_app_client(page_service, public_base_url=public_base_url)

    response = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200, response.text
    assert response.json()["resource"] == expected_resource


def test_token_endpoint_accepts_form_for_code_and_refresh_and_rejects_json(
    page_service,
):
    client = make_client(page_service)
    registration = register_client(client, CLAUDE_REDIRECT)
    verifier = "v" * 64
    params = authorization_params(
        registration["client_id"],
        CLAUDE_REDIRECT,
        verifier,
        state="form-code-exchange",
    )
    authorization = submit_authorization(client, params)
    code = parse_qs(urlparse(authorization.headers["location"]).query)["code"][0]

    initial = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "redirect_uri": CLAUDE_REDIRECT,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert initial.status_code == 200, initial.text
    assert initial.request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": initial.json()["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )

    json_request = client.post(
        "/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": refreshed.json()["refresh_token"],
        },
    )
    assert json_request.status_code == 400, json_request.text
    assert json_request.json()["error"] == "invalid_request"
    assert "form-urlencoded" in json_request.json()["error_description"]


def test_discovery_documents_use_the_protected_resource_authorization_server(
    page_service,
):
    client = make_client(page_service)

    protected_resource = client.get(
        "/.well-known/oauth-protected-resource"
    )
    authorization_server = client.get(
        "/.well-known/oauth-authorization-server"
    )

    assert protected_resource.status_code == 200, protected_resource.text
    assert authorization_server.status_code == 200, authorization_server.text
    assert protected_resource.json()["authorization_servers"][0] == ISSUER
    assert authorization_server.json()["issuer"] == ISSUER
    assert (
        authorization_server.json()["authorization_endpoint"]
        == f"{ISSUER}/oauth/authorize"
    )
    assert authorization_server.json()["token_endpoint"] == f"{ISSUER}/oauth/token"


def test_authorization_responses_include_rfc9207_issuer(page_service):
    client = make_client(page_service)
    registration = register_client(client, CLAUDE_REDIRECT)

    for decision in ("deny", "allow"):
        params = authorization_params(
            registration["client_id"],
            CLAUDE_REDIRECT,
            "v" * 64,
            state=f"issuer-{decision}",
        )
        response = submit_authorization(client, params, decision=decision)
        query = parse_qs(urlparse(response.headers["location"]).query)

        assert query["iss"] == [ISSUER]
        assert query["state"] == [params["state"]]
        if decision == "deny":
            assert query["error"] == ["access_denied"]
        else:
            assert query["code"]

    reconnect_params = authorization_params(
        registration["client_id"],
        CLAUDE_REDIRECT,
        "v" * 64,
        state="issuer-existing-consent",
    )
    reconnect = client.get(
        "/oauth/authorize",
        params=reconnect_params,
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    reconnect_query = parse_qs(urlparse(reconnect.headers["location"]).query)

    assert reconnect.status_code == 302, reconnect.text
    assert reconnect_query["iss"] == [ISSUER]
    assert reconnect_query["state"] == [reconnect_params["state"]]
    assert reconnect_query["code"]


def test_refresh_token_is_issued_without_offline_access_while_scope_is_advertised(
    page_service,
):
    """O2 characterization, updated for O3's discovery advertisement."""

    client = make_client(page_service)
    registration = register_client(client, CHATGPT_REDIRECT, scope="mcp:read")

    tokens = authorize_and_exchange(
        client,
        registration,
        CHATGPT_REDIRECT,
        state="refresh-before-o3",
        scope="mcp:read",
    )
    protected_resource = client.get(
        "/.well-known/oauth-protected-resource"
    ).json()
    authorization_server = client.get(
        "/.well-known/oauth-authorization-server"
    ).json()

    assert "offline_access" not in registration["scope"].split()
    assert tokens["scope"] == "mcp:read"
    assert tokens["refresh_token"]
    assert "offline_access" in protected_resource["scopes_supported"]
    assert "offline_access" in authorization_server["scopes_supported"]

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["scope"] == "mcp:read"


def test_offline_access_is_accepted_echoed_refreshed_and_audited(page_service):
    client = make_client(page_service)
    requested_scope = "mcp:read offline_access"
    registration = register_client(
        client,
        CLAUDE_REDIRECT,
        name="Offline Access Flow",
        scope=requested_scope,
    )
    assert registration["scope"] == requested_scope

    verifier = "v" * 64
    params = authorization_params(
        registration["client_id"],
        CLAUDE_REDIRECT,
        verifier,
        state="offline-access-flow",
        scope=requested_scope,
    )
    consent = client.get(
        "/oauth/authorize",
        params=params,
        headers={"accept": "text/html"},
    )
    assert consent.status_code == 200, consent.text
    assert "Stay signed in without asking you to authorize again" in consent.text

    authorization = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert authorization.status_code == 303, authorization.text
    code = parse_qs(urlparse(authorization.headers["location"]).query)["code"][0]
    issued = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "redirect_uri": CLAUDE_REDIRECT,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert issued.status_code == 200, issued.text
    assert issued.json()["scope"] == requested_scope

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": issued.json()["refresh_token"],
            "scope": requested_scope,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["scope"] == requested_scope

    with page_service.session_factory() as session:
        consent_audit = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oauth.consent.granted")
        ).one()
    assert consent_audit.payload["scope"] == requested_scope


def test_unknown_scope_is_still_rejected_by_dcr_authorize_and_refresh(
    page_service,
):
    client = make_client(page_service)
    unknown_scope = "mcp:read offline_access mcp:unknown"

    dcr = client.post(
        "/oauth/register",
        json={
            "client_name": "Unknown Scope DCR",
            "redirect_uris": [CLAUDE_REDIRECT],
            "scope": unknown_scope,
        },
    )
    assert dcr.status_code == 400, dcr.text
    assert dcr.json()["error_code"] == "invalid_client_metadata"
    assert "unsupported scope: mcp:unknown" in dcr.json()["message"]

    requested_scope = "mcp:read offline_access"
    registration = register_client(
        client,
        CLAUDE_REDIRECT,
        name="Unknown Scope Runtime",
        scope=requested_scope,
    )
    verifier = "v" * 64
    authorize = client.get(
        "/oauth/authorize",
        params=authorization_params(
            registration["client_id"],
            CLAUDE_REDIRECT,
            verifier,
            state="unknown-authorize",
            scope=unknown_scope,
        ),
        headers={"accept": "application/json"},
    )
    assert authorize.status_code == 400, authorize.text
    assert authorize.json()["error_code"] == "invalid_request"
    assert "unsupported scope: mcp:unknown" in authorize.json()["message"]

    issued = authorize_and_exchange(
        client,
        registration,
        CLAUDE_REDIRECT,
        state="unknown-refresh-setup",
        scope=requested_scope,
    )
    refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": issued["refresh_token"],
            "scope": unknown_scope,
        },
    )
    assert refresh.status_code == 400, refresh.text
    assert refresh.json()["error"] == "invalid_request"
    assert "unsupported scope: mcp:unknown" in refresh.json()["error_description"]


def test_offline_access_token_has_no_additional_mcp_capability(page_service):
    client = make_client(page_service)
    read_registration = register_client(
        client,
        CLAUDE_REDIRECT,
        name="Read Capability Baseline",
        scope="mcp:read",
    )
    offline_registration = register_client(
        client,
        CHATGPT_REDIRECT,
        name="Offline Capability Comparison",
        scope="mcp:read offline_access",
    )
    read_tokens = authorize_and_exchange(
        client,
        read_registration,
        CLAUDE_REDIRECT,
        state="read-capability-baseline",
        scope="mcp:read",
    )
    offline_tokens = authorize_and_exchange(
        client,
        offline_registration,
        CHATGPT_REDIRECT,
        state="offline-capability-comparison",
        scope="mcp:read offline_access",
    )

    assert offline_tokens["scope"] == "mcp:read offline_access"
    assert listed_tool_names(
        client, offline_tokens["access_token"]
    ) == listed_tool_names(client, read_tokens["access_token"])
