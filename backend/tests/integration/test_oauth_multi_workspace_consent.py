"""P10.3 T3 (section 2.3) -- multi-workspace OAuth consent regression
coverage (plan section 5, R3).

D-01v2 replaced the one-workspace-per-credential binding with an
ALLOWED-WORKSPACE SET, checked live per request against ``oauth_consents``
(``removed_at IS NULL``) rather than baked into the access-token JWT. This
file walks the checklist authorize flow end-to-end: one token reaching two
workspaces, a third (ungranted) workspace denied, a live Settings PATCH
narrowing the set with no re-authentication, and revoke killing the whole
group so a subsequent refresh fails closed.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.read import SearchPayload
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.oauth_service import _DCR_BUCKET, challenge_s256
from backend.tests.conftest import authenticate_test_client_in_workspace


def _client(page_service) -> TestClient:
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
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def _extra_workspace(page_service, *, slug: str) -> str:
    """Seed one more workspace and grant the (already-authenticated) test
    user membership in it. Returns the workspace id as a string."""

    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        workspace = seed_default_workspace(session, slug=slug, title=slug)
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=workspace.id, role="viewer",
        )
        return str(workspace.id)


def _stub_search(client: TestClient) -> None:
    """Bearer-token workspace-reach probes only need the endpoint to reach
    the actor_with_target_workspace_scope authorization gate and return 200;
    the search implementation itself is irrelevant here (same technique as
    test_actor_invariants.py)."""

    client.app.state.search_service = SimpleNamespace(
        search=lambda **_kwargs: SearchPayload(
            ranked_results=[],
            supported_retrieval_modes=["keyword"],
            permission_note="multi-workspace consent probe",
            next_cursor=None,
        )
    )


def _probe(client: TestClient, *, workspace_id: str, access_token: str):
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"query": "needle", "scope": "all"},
    )


def test_multi_workspace_consent_reaches_only_the_granted_set(page_service):
    client = _client(page_service)
    _stub_search(client)
    workspaces_response = client.get("/api/v1/workspaces")
    workspace_a = workspaces_response.json()["data"]["current_workspace_id"]
    workspace_b = _extra_workspace(page_service, slug="r3-multi-ws-b")
    workspace_c = _extra_workspace(page_service, slug="r3-multi-ws-c")

    _DCR_BUCKET.clear()
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "R3 multi-workspace client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    state = "r3-multi-workspace-0123456789"

    consent = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": state,
        },
    )
    assert consent.status_code == 200, consent.text
    # The checklist offers all three memberships; workspace_c is deliberately
    # left unchecked in the submission below.
    assert workspace_a.encode() in consent.content
    assert workspace_b.encode() in consent.content
    assert workspace_c.encode() in consent.content

    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": state,
            "workspace_ids": [workspace_a, workspace_b],
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

    reach_a = _probe(client, workspace_id=workspace_a, access_token=access_token)
    reach_b = _probe(client, workspace_id=workspace_b, access_token=access_token)
    assert reach_a.status_code == 200, reach_a.text
    assert reach_b.status_code == 200, reach_b.text
    denied = _probe(client, workspace_id=workspace_c, access_token=access_token)
    assert denied.status_code in (403, 404), denied.text

    # GET /oauth/consents returns ONE (user, client) group row naming both
    # granted workspaces, not workspace_c.
    listing = client.get("/oauth/consents")
    assert listing.status_code == 200, listing.text
    rows = listing.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["client_id"] == client_id
    granted_ids = {entry["id"] for entry in row["workspaces"]}
    assert granted_ids == {workspace_a, workspace_b}

    # Settings PATCH narrows the set to workspace_a only -- takes effect on
    # the VERY NEXT request, no re-authentication.
    narrowed = client.patch(
        "/oauth/consents",
        params={"client_id": client_id},
        json={"workspace_ids": [workspace_a]},
    )
    assert narrowed.status_code == 200, narrowed.text
    assert {entry["id"] for entry in narrowed.json()["data"]["workspaces"]} == {
        workspace_a
    }

    still_reach_a = _probe(client, workspace_id=workspace_a, access_token=access_token)
    assert still_reach_a.status_code == 200, still_reach_a.text
    now_denied = _probe(client, workspace_id=workspace_b, access_token=access_token)
    assert now_denied.status_code in (403, 404), now_denied.text

    # Refresh keeps the NARROWED set -- the token's own live consent lookup
    # is not a snapshot from issuance time.
    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new_access_token = refreshed.json()["access_token"]
    new_reach_a = _probe(
        client, workspace_id=workspace_a, access_token=new_access_token
    )
    assert new_reach_a.status_code == 200, new_reach_a.text
    still_denied = _probe(
        client, workspace_id=workspace_b, access_token=new_access_token
    )
    assert still_denied.status_code in (403, 404), still_denied.text

    # Revoking the whole consent group makes the next refresh fail closed.
    revoked = client.delete("/oauth/consents", params={"client_id": client_id})
    assert revoked.status_code == 200, revoked.text
    dead_refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refreshed.json()["refresh_token"],
        },
    )
    assert dead_refresh.status_code == 400, dead_refresh.text
    assert dead_refresh.json()["error"] == "invalid_grant"
