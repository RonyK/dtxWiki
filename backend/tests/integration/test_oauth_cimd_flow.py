from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.oauth import OAuthClient
from backend.services import oauth_service
from backend.services.oauth_cimd import CIMDClientMetadata
from backend.services.oauth_service import challenge_s256
from backend.tests.conftest import authenticate_test_client

CLIENT_ID = "https://client.example/oauth/client.json"
REDIRECT_URI = "http://127.0.0.1:53682/callback"


def make_client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                mcp_http_mode="json_stateless",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client)
    return client


def _metadata(*, redirect_uris=(REDIRECT_URI,)) -> CIMDClientMetadata:
    return CIMDClientMetadata(
        client_id=CLIENT_ID,
        client_name="CIMD Integration Client",
        redirect_uris=tuple(redirect_uris),
        scope="mcp:read mcp:write",
        grant_types=("authorization_code", "refresh_token"),
        response_types=("code",),
        application_type="native",
    )


def _workspace_id(client: TestClient) -> str:
    response = client.get("/api/v1/workspaces")
    return response.json()["data"]["current_workspace_id"]


def _authorize_params(
    *,
    redirect_uri=REDIRECT_URI,
    state="cimd-state-0123456789abcdef",
):
    verifier = "v" * 64
    return verifier, {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read mcp:write",
        "state": state,
    }


def _issue_cimd_tokens(client: TestClient):
    verifier, params = _authorize_params()
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200, consent.text
    assert "CIMD Integration Client" in consent.text

    authorize = client.post(
        "/oauth/authorize",
        data={
            **params,
            # P10.3 T3: the checklist submits workspace_ids (repeatable),
            # not a singular workspace_id.
            "workspace_ids": _workspace_id(client),
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
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return token.json()


def test_authorization_server_metadata_advertises_cimd(page_service):
    client = make_client(page_service)

    response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert response.json()["client_id_metadata_document_supported"] is True
    assert "registration_endpoint" in response.json()


def test_valid_cimd_authorizes_and_exchanges_code_end_to_end(
    page_service,
    monkeypatch,
):
    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        lambda client_id: _metadata(),
    )
    client = make_client(page_service)

    tokens = _issue_cimd_tokens(client)

    assert tokens["scope"] == "mcp:read mcp:write"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    with page_service.session_factory() as session:
        stored = session.get(OAuthClient, CLIENT_ID)
        assert stored is not None
        assert stored.created_via == "cimd"
        assert stored.redirect_uris == [REDIRECT_URI]
        assert stored.application_type == "native"


def test_failed_refetch_before_first_grant_fails_closed(
    page_service,
    monkeypatch,
):
    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        lambda client_id: _metadata(),
    )
    client = make_client(page_service)
    _, params = _authorize_params(state="cimd-refetch-0123456789abcdef")
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200, consent.text

    def unavailable(client_id):
        raise RuntimeError("private fetch detail")

    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        unavailable,
    )
    authorize = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": _workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )

    assert authorize.status_code == 400
    assert "client metadata is unavailable" in authorize.text
    assert "private fetch detail" not in authorize.text


def test_cimd_change_cannot_widen_existing_grant_and_outage_does_not_revoke_it(
    page_service,
    monkeypatch,
):
    fetches = 0

    def initial_fetch(client_id):
        nonlocal fetches
        fetches += 1
        return _metadata()

    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        initial_fetch,
    )
    client = make_client(page_service)
    tokens = _issue_cimd_tokens(client)
    assert fetches >= 1

    widened_redirect = "https://added.example/callback"
    post_grant_fetches = 0

    def changed_document(client_id):
        nonlocal post_grant_fetches
        post_grant_fetches += 1
        return _metadata(redirect_uris=(REDIRECT_URI, widened_redirect))

    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        changed_document,
    )
    _, widened_params = _authorize_params(
        redirect_uri=widened_redirect,
        state="cimd-widened-0123456789abcdef",
    )
    widened = client.get("/oauth/authorize", params=widened_params)
    assert widened.status_code == 400
    assert "redirect_uri is not registered" in widened.text
    assert post_grant_fetches == 0

    outage_fetches = 0

    def unavailable(client_id):
        nonlocal outage_fetches
        outage_fetches += 1
        raise RuntimeError("document disappeared")

    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        unavailable,
    )
    _, existing_params = _authorize_params(
        state="cimd-existing-0123456789abcdef"
    )
    existing = client.get(
        "/oauth/authorize",
        params=existing_params,
        follow_redirects=False,
    )
    assert existing.status_code == 302
    assert outage_fetches == 0

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text


def test_cimd_failure_response_audit_and_log_do_not_leak_probe_details(
    page_service,
    monkeypatch,
    caplog,
):
    secrets = (
        "FETCHED_BODY_CANARY",
        "10.23.45.67",
        "socket exploded canary",
    )

    def fail(client_id):
        raise RuntimeError(" ".join(secrets))

    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        fail,
    )
    client = make_client(page_service)
    _, params = _authorize_params(state="cimd-error-0123456789abcdef")

    response = client.get("/oauth/authorize", params=params)

    assert response.status_code == 400
    assert "client metadata is unavailable" in response.text
    for secret in secrets:
        assert secret not in response.text
        assert secret not in caplog.text
    with page_service.session_factory() as session:
        audit_payloads = [
            str(payload)
            for payload in session.scalars(select(AuditEvent.payload)).all()
        ]
    for secret in secrets:
        assert all(secret not in payload for payload in audit_payloads)


def test_cimd_redirects_use_canonical_redirect_validator(
    page_service,
    monkeypatch,
):
    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        lambda client_id: _metadata(
            redirect_uris=("https://shown.example\\@actual.example/callback",)
        ),
    )
    client = make_client(page_service)
    _, params = _authorize_params(state="cimd-bad-redirect-0123456789abcdef")

    response = client.get("/oauth/authorize", params=params)

    assert response.status_code == 400
    assert "client metadata is unavailable" in response.text
    with page_service.session_factory() as session:
        assert session.get(OAuthClient, CLIENT_ID) is None


def test_cimd_authorizes_and_exchanges_even_when_dcr_is_disabled(
    page_service,
    monkeypatch,
):
    """MCP-10: DCR off is NOT a hard OAuth-availability gate. Before this
    fix, oauth_unavailable_reason() folded oauth_allow_dcr into the same
    "OAuth unavailable" verdict the UI used to steer operators to a weaker
    Access Key -- but _resolve_authorization_client (the code this
    end-to-end flow exercises) never reads oauth_allow_dcr at all, so a URL
    client identifier (CIMD) authorizes fine either way. Pins both halves
    together: DCR itself is blocked, CIMD is not.
    """

    monkeypatch.setattr(
        oauth_service,
        "fetch_client_metadata_document",
        lambda client_id: _metadata(),
    )
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                mcp_http_mode="json_stateless",
                oauth_allow_dcr=False,
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client)

    denied = client.post(
        "/oauth/register",
        json={
            "client_name": "Should Be Refused",
            "redirect_uris": [REDIRECT_URI],
            "scope": "mcp:read",
        },
    )
    assert denied.status_code == 400, denied.text

    tokens = _issue_cimd_tokens(client)
    assert tokens["access_token"]
    assert tokens["refresh_token"]


def test_dcr_compatibility_remains_enabled(page_service):
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "DCR Compatibility Client",
            "redirect_uris": [REDIRECT_URI],
            "scope": "mcp:read",
        },
    )
    assert registered.status_code == 201, registered.text

    verifier, params = _authorize_params(state="dcr-still-0123456789abcdef")
    params["client_id"] = registered.json()["client_id"]
    params["scope"] = "mcp:read"
    params["code_challenge"] = challenge_s256(verifier)
    authorize = client.get("/oauth/authorize", params=params)

    assert authorize.status_code == 200, authorize.text
