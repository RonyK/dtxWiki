import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.workspace import MCPClient, Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import credential_hashing
from backend.services.oauth_service import OAUTH_CSRF_COOKIE_NAME, challenge_s256
from backend.services.page_service import PageService
from backend.tests.conftest import _FAST_ARGON2, authenticate_test_client_in_workspace
from backend.tests.utils.postgres import create_isolated_postgres_metadata


@contextmanager
def module_scoped_page_service(empty_schema):
    """A seeded in-memory ``PageService`` for module-scoped fixtures.

    Mirrors the function-scoped ``session_factory`` fixture. For modules whose
    tests only read or add independent rows, so one store and one signed-in
    client can serve them all. Tests that count rows, grant consent or
    exercise a user's lifecycle keep the per-test ``page_service`` instead.
    The suite-wide ``fast_argon2`` fixture is function-scoped, so the
    cheap hasher is installed here for the sign-in the fixture performs.
    """

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        creator=lambda: connection,
        poolclass=StaticPool,
    )
    patch = pytest.MonkeyPatch()
    try:
        empty_schema.backup(connection)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory.begin() as session:
            workspace = seed_default_workspace(session)
            session.add(
                MCPClient(
                    workspace_id=workspace.id,
                    name="fixture-system-client",
                    client_type="system_managed",
                    scopes=["*"],
                    status="active",
                )
            )
        patch.setattr(
            credential_hashing,
            "PasswordHasher",
            lambda **_cost: PasswordHasher(**_FAST_ARGON2),
        )
        yield PageService(factory)
    finally:
        patch.undo()
        engine.dispose()
        connection.close()


def make_client(
    page_service,
    *,
    email: str = "test@example.com",
    role: str = "owner",
) -> TestClient:
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
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        role=role,
    )
    # Sprint_5 Phase_3 Task 3.3: the consent POST now requires a
    # state-bound CSRF cookie established by a prior GET (ADR A3). Existing
    # regression tests POST directly without doing a GET first. To keep the
    # blast radius of Task 3.3 small while still exercising the production
    # check, wrap the TestClient so a POST to /oauth/authorize automatically
    # issues the corresponding GET when no CSRF cookie is yet present in the
    # session and the POST carries a valid ``state`` value. Tests that
    # explicitly exercise the CSRF failure path (missing/mismatch/replay/
    # expired) bypass the wrapper by setting/clearing cookies directly.
    _install_oauth_authorize_csrf_priming(client)
    return client


_CSRF_PRIMING_BYPASS_KEYWORDS = (
    "csrf",
    "state_missing",
    "state_mismatch",
    "state_replay",
    "state_expired",
    "state_binding",
    "consent_state_",
    "_no_csrf",  # explicit opt-out for future tests
)


def _install_oauth_authorize_csrf_priming(client: TestClient) -> None:
    """Auto-prime the ``oauth_csrf_state`` cookie on POST /oauth/authorize.

    Sprint_5 Phase_3 Task 3.7 hardening (TEST-FR3MID-1 mid-review): the
    wrapper now BYPASSES priming for any test whose pytest node id
    contains a CSRF-related keyword (see ``_CSRF_PRIMING_BYPASS_KEYWORDS``).
    Without that guard, a future CSRF negative test added to this file
    would be silently rescued by the auto-prime and the assertion would
    flip to a false positive. The bypass forces the test author to set
    up cookies explicitly when exercising the CSRF failure path.

    Append ``_no_csrf`` to a test function name as an explicit opt-out
    when the test legitimately wants the prime but happens to mention
    CSRF in its docstring.
    """

    original_post = client.post

    def _should_bypass() -> bool:
        # ``PYTEST_CURRENT_TEST`` is "<nodeid> (<phase>)" during test
        # execution; missing during fixture setup, in which case the
        # wrapper is a no-op anyway.
        import os

        node = os.environ.get("PYTEST_CURRENT_TEST", "").lower()
        return any(keyword in node for keyword in _CSRF_PRIMING_BYPASS_KEYWORDS)

    def post(url, *args, **kwargs):
        path = url.split("?", 1)[0]
        if path == "/oauth/authorize" and not _should_bypass():
            data = kwargs.get("data") or {}
            json_body = kwargs.get("json") or {}
            payload = data if data else json_body
            form_state = str(payload.get("state") or "") if payload else ""
            cookie_state = client.cookies.get(OAUTH_CSRF_COOKIE_NAME)
            if form_state and cookie_state != form_state:
                # ``decision`` is a POST-only form field; ``workspace_id`` IS
                # accepted on GET (and required for multi-membership users
                # to disambiguate). When the GET's workspace_id resolution
                # fails (e.g. the test is specifically exercising a foreign
                # workspace POST), fall back to a GET without workspace_id
                # so the CSRF cookie is still seeded for the POST path's
                # workspace assertion.
                get_params = {
                    str(k): str(v)
                    for k, v in payload.items()
                    if k != "decision" and v != ""
                }
                get_resp = client.get("/oauth/authorize", params=get_params)
                if (
                    get_resp.status_code != 200
                    and "workspace_id" in get_params
                ):
                    fallback = {
                        k: v
                        for k, v in get_params.items()
                        if k != "workspace_id"
                    }
                    client.get("/oauth/authorize", params=fallback)
        return original_post(url, *args, **kwargs)

    client.post = post  # type: ignore[method-assign]


def current_workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def assert_json_rpc_error(
    body: dict,
    *,
    request_id: int | str | None,
    error: str,
    message_contains: str | None = None,
) -> dict:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    payload = body["error"]
    assert payload["code"] == -32000
    if message_contains is not None:
        assert message_contains in payload["message"]
    assert payload["data"]["error"] == error
    return payload


def assert_oauth_token_no_store(response) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def assert_oauth_token_error(response, error: str) -> dict:
    assert_oauth_token_no_store(response)
    body = response.json()
    assert set(body) == {"error", "error_description"}
    assert body["error"] == error
    assert "envelope" not in body
    return body


def register_oauth_client(client: TestClient, *, name: str = "Test MCP Client") -> str:
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read mcp:write",
        },
    )
    assert registered.status_code == 201, registered.text
    return registered.json()["client_id"]


def register_oauth_client_without_scope(
    client: TestClient,
    *,
    name: str = "Test MCP Client",
) -> dict:
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
        },
    )
    assert registered.status_code == 201, registered.text
    return registered.json()


def oauth_state(label: str) -> str:
    if len(label) >= 22:
        return label
    return f"{label}-0123456789abcdef"


def authorize_params(client_id: str, verifier: str, *, state: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state(state),
    }


def _workspace_checkbox_checked(html: str, workspace_id) -> bool:
    """Whether the P10.3 T3 section 2.3 consent checklist's
    ``<input type="checkbox" name="workspace_ids" value="{workspace_id}" ...>``
    row is pre-checked. Matches from ``value="<id>"`` up to the tag's
    closing ``>`` (the ``checked`` token, if present, is the last attribute
    before it -- see ``_consent_workspace_checklist`` in backend/api/
    oauth.py) so it is robust to incidental whitespace/formatting changes.
    """
    import re

    match = re.search(rf'value="{workspace_id}"(.*?)>', html, re.DOTALL)
    assert match, f"workspace_ids checkbox for {workspace_id} not found in html"
    return "checked" in match.group(1)


def test_mcp_http_requires_bearer_and_advertises_oauth_metadata(page_service):
    client = make_client(page_service)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]
    metadata = client.get("/.well-known/oauth-protected-resource")
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://testserver/mcp"


def test_oauth_authorize_post_requires_explicit_decision(page_service):
    client = make_client(page_service)
    client_id = register_oauth_client(client, name="Decision Required Client")
    verifier = "v" * 64
    params = authorize_params(client_id, verifier, state="missing-decision")

    # Sprint_5 Phase_3 Task 3.3: GET first to populate the CSRF cookie so the
    # subsequent POST exercises decision-validation rather than the new
    # state-binding short-circuit.
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200

    response = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_id": current_workspace_id(client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"
    assert "decision" in response.json()["message"]
    assert client.get("/oauth/consents").json()["data"] == []


def test_oauth_authorize_get_records_account_scoped_activity(page_service):
    """P10.3 T3 (section 2.3): a genuinely new authorization request is
    audited as an account-scoped (``workspace_id=None``) event now -- the
    checklist the user is about to see may cover several workspaces at
    once, so no single workspace owns the request any more (mirroring
    ``ApiTokenService.create_token``'s ``auth.api_token.created``). That
    means it no longer shows up in any single workspace's activity feed
    (``ActivityService.list_activity`` filters on
    ``AuditEvent.workspace_id == workspace_id``).
    """
    client = make_client(page_service)
    client_id = register_oauth_client(client, name="Activity OAuth Client")
    verifier = "v" * 64
    params = authorize_params(client_id, verifier, state="activity-request")
    workspace_id = current_workspace_id(client)

    consent = client.get("/oauth/authorize", params=params)

    assert consent.status_code == 200
    with page_service.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "oauth.authorization.requested"
            )
        )
        assert event is not None
        assert event.workspace_id is None
        assert event.actor_kind == "human"
        assert event.target_kind == "oauth_authorization"
        assert event.event_summary == "Activity OAuth Client requested MCP OAuth access"
        assert event.retention_class == "sensitive"
        assert event.payload == {
            "client_id": client_id,
            "client_name": "Activity OAuth Client",
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "scope": "mcp:read",
            "existing_consent": False,
        }

    activity = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity",
        params={"period": "all", "limit": 50},
    )
    assert activity.status_code == 200
    items = activity.json()["data"]["items"]
    assert not any(
        item["body"] == "oauth.authorization.requested" for item in items
    )


@pytest.mark.parametrize(
    ("role", "write_allowed"),
    # Owner takes the editor branch for writes; viewer is the refused side.
    (("viewer", False), ("editor", True)),
)
def test_mcp_http_enforces_live_workspace_role_matrix(
    page_service,
    role: str,
    write_allowed: bool,
):
    email = f"mcp-live-{role}@example.com"
    issuance_role = "editor" if role == "viewer" else role
    client = make_client(
        page_service,
        email=email,
        role=issuance_role,
    )
    workspace_id = current_workspace_id(client)
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": f"MCP live {role}",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    assert issued.status_code == 201
    if role != issuance_role:
        from uuid import UUID

        from backend.models.user import User
        from backend.models.workspace import WorkspaceMembership

        with page_service.session_factory.begin() as session:
            user = session.scalar(select(User).where(User.email == email))
            assert user is not None
            membership = session.get(
                WorkspaceMembership,
                (user.id, UUID(workspace_id)),
            )
            assert membership is not None
            membership.role = role
    token = issued.json()["data"]["plain_token"]
    headers = {"Authorization": f"Bearer {token}"}

    listed = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "guide",
                "arguments": {"workspace_id": workspace_id},
            },
        },
    )
    assert listed.status_code == 200
    assert (
        listed.json()["result"]["structuredContent"]["envelope"]["result_status"]
        == "succeeded"
    )

    written = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "create_context_page",
                "arguments": {
                    "workspace_id": workspace_id,
                    "title": f"MCP live {role}",
                    "page_type": "overview",
                    "body_md": f"# MCP live {role}",
                    "conversation_token": "new",
                },
            },
        },
    )
    assert written.status_code == 200
    envelope = written.json()["result"]["structuredContent"]["envelope"]
    if write_allowed:
        assert envelope["result_status"] == "succeeded"
    else:
        assert envelope["result_status"] == "rejected_by_policy"
        assert envelope["error_code"] == "workspace_role_insufficient"


def test_mcp_http_sdk_stateless_json_response_does_not_require_session(page_service):
    """A personal API token is accepted on /mcp; the stateless JSON mode needs
    no session id; an Accept header without JSON is refused with 406."""
    client = make_client(page_service)
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": current_workspace_id(client)}],
            "name": "MCP HTTP",
            "scopes": ["mcp:read", "mcp:write"],
        },
    )
    token = issued.json()["data"]["plain_token"]

    listed = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"list_my_workspaces", "create_context_page"}.issubset(names)

    unsupported = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/plain"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert unsupported.status_code == 406
    assert_json_rpc_error(
        unsupported.json(),
        request_id=1,
        error="not_acceptable",
        message_contains="Accept must include",
    )

    first = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    second = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert "mcp-session-id" not in first.headers
    assert "mcp-session-id" not in second.headers
    assert first.headers["content-type"].startswith("application/json")
    assert second.json()["id"] == 2


def test_oauth_dcr_pkce_token_and_mcp_call(page_service):
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Test MCP Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read mcp:write",
        },
    )
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    state = oauth_state("state-123")
    authorize_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read mcp:write",
        "state": state,
    }
    consent = client.get(
        "/oauth/authorize",
        params=authorize_params,
    )
    assert consent.status_code == 200
    assert "Authorize dtxWiki MCP access" in consent.text
    assert "Dynamic client — Registered during this login" in consent.text
    assert "Deny" in consent.text

    authorize = client.post(
        "/oauth/authorize",
        data={
            **authorize_params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    location = authorize.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query["state"] == [state]

    skip_reprompt = client.get(
        "/oauth/authorize",
        params={**authorize_params, "state": oauth_state("state-456")},
        follow_redirects=False,
    )
    assert skip_reprompt.status_code == 302
    skip_query = parse_qs(urlparse(skip_reprompt.headers["location"]).query)
    assert skip_query["state"] == [oauth_state("state-456")]
    assert "code" in skip_query

    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": query["code"][0],
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    assert_oauth_token_no_store(token)
    token_payload = token.json()
    assert set(token_payload) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "refresh_token_expires_in",
        "scope",
    }
    refresh_token = token_payload["refresh_token"]
    assert client.get("/.well-known/jwks.json").json()["keys"][0]["alg"] == "RS256"

    reused_code = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": query["code"][0],
            "code_verifier": verifier,
        },
    )
    assert reused_code.status_code == 400
    assert_oauth_token_error(reused_code, "invalid_grant")

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    assert refreshed.status_code == 200
    assert_oauth_token_no_store(refreshed)
    refreshed_payload = refreshed.json()
    assert set(refreshed_payload) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "refresh_token_expires_in",
        "scope",
    }
    rotated_refresh_token = refreshed_payload["refresh_token"]
    rotated_access_token = refreshed_payload["access_token"]
    assert rotated_refresh_token != refresh_token

    # Sprint_5 Phase_3 Task 3.4 (ADR A5): exercise the access token
    # against /mcp *before* the refresh-reuse compromise. Reuse fires
    # family revoke and revokes every access JWT in the (user, client)
    # pair, including the original ``access_token`` issued at line ~317
    # and the rotated successor — so any /mcp call after the reuse must
    # 401 (test_refresh_reuse_revokes_access_jti_in_family pins that
    # contract). Pre-Task 3.4 the original access JWT remained valid.
    called = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {rotated_access_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_my_workspaces", "arguments": {}},
        },
    )
    assert called.status_code == 200

    # P10.3 T3 section 2.3 (ADR A5 amendment): reuse of an already-rotated
    # refresh token within the 30s grace window (REFRESH_REUSE_GRACE_WINDOW_
    # SECONDS) mints a fresh sibling pair instead of revoking the family --
    # this reuse happens well inside that window, so it is no longer
    # invalid_grant.
    reused_refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )
    assert reused_refresh.status_code == 200, reused_refresh.text
    assert_oauth_token_no_store(reused_refresh)
    grace_payload = reused_refresh.json()
    assert set(grace_payload) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "refresh_token_expires_in",
        "scope",
    }
    assert grace_payload["refresh_token"] not in {refresh_token, rotated_refresh_token}
    assert grace_payload["access_token"] != rotated_access_token
    structured = called.json()["result"]["structuredContent"]
    assert structured["envelope"]["result_status"] == "succeeded"
    assert structured["data"]["workspaces"][0]["slug"] == "default"
    with page_service.session_factory() as session:
        actions = {
            event.action
            for event in session.scalars(select(AuditEvent)).all()
        }
    assert {
        "oauth.code.issued",
        "oauth.token.issued",
        "oauth.token.refreshed",
        "oauth.consent.granted",
    }.issubset(actions)


def test_oauth_dcr_without_scope_defaults_to_read_only(page_service):
    """DCR with an omitted scope registers least-privilege (mcp:read), NOT every
    scope (Codex #2). A client that needs write/agent must request it explicitly
    at registration; the user still approves it at /oauth/authorize.
    """

    client = make_client(page_service)
    registered = register_oauth_client_without_scope(
        client, name="Scope Omitted Client"
    )
    assert registered["scope"] == "mcp:read"

    # The read-only client can authorize for its registered scope.
    verifier = "v" * 64
    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("scope-omitted"),
        },
    )

    assert response.status_code == 200, response.text
    assert "Authorize dtxWiki MCP access" in response.text
    # NEW-5: the trust label carries Korean copy alongside the English term.
    assert "동적 클라이언트" in response.text
    assert "신원은 독립적으로 검증되지 않았습니다" in response.text
    assert "Dynamic client — Registered during this login" in response.text


def test_oauth_authorize_browser_errors_render_html(page_service):
    client = make_client(page_service)
    client_id = register_oauth_client(client, name="Read Only Browser Client")
    verifier = "v" * 64

    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:agent",
            "state": oauth_state("browser-error"),
        },
        headers={"accept": "text/html"},
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "OAuth request could not continue" in response.text
    assert "requested scope is not registered" in response.text


def test_oauth_rejects_invalid_pkce_and_state_parameters(page_service):
    """One registered client, five malformed authorization requests.

    GET /oauth/authorize goes through validate_authorization_request ->
    validate_pkce_request / validate_state. The POST handler rejects on
    `authorization decision is required` BEFORE those validators fire, so a
    POST would pass regardless of whether they ran (a false positive found in
    Fix-7). Each case pins the validator's own failure keyword, so a refactor
    that re-orders the validations cannot silently bypass one of them while
    still returning `invalid_request`.
    """
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Bad PKCE",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
        },
    ).json()
    valid = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256("v" * 64),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state("state-123"),
    }
    cases = [
        # Only S256 is accepted.
        (
            "plain method",
            {"code_challenge": "p" * 43, "code_challenge_method": "plain"},
            "S256",
        ),
        # RFC 7636 section 4.2: 43-char minimum, 128-char maximum.
        ("undersized challenge", {"code_challenge": "a" * 42}, "challenge"),
        ("oversized challenge", {"code_challenge": "a" * 129}, "challenge"),
        ("missing state", {"state": None}, "state"),
        ("undersized state", {"state": "short-state"}, "at least 22 chars"),
    ]

    for label, override, keyword in cases:
        params = {
            key: value for key, value in {**valid, **override}.items() if value
        }
        authorize = client.get("/oauth/authorize", params=params)
        assert authorize.status_code == 400, label
        body = authorize.json()
        assert body["error_code"] == "invalid_request", label
        assert keyword in body["message"], (
            f"{label}: validator did not fire - got message: {body['message']!r}"
        )


def test_oauth_refresh_rejects_legacy_null_hashed_token(page_service):
    """F5/P1-2: refresh tokens issued before the Argon2id rollout (where
    ``hashed_token IS NULL``) must be rejected, forcing the client to
    re-authorize.
    """

    from backend.models.oauth import OAuthRefreshToken

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Legacy Refresh Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state("legacy-state"),
    }
    authorize = client.post(
        "/oauth/authorize",
        data={
            **authorize_params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_payload = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    ).json()
    refresh_token = token_payload["refresh_token"]

    # Simulate legacy row (no Argon2id verifier) by clearing the column.
    with page_service.session_factory.begin() as session:
        row = session.scalars(select(OAuthRefreshToken)).first()
        row.hashed_token = None

    rejected = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": refresh_token,
        },
    )
    assert rejected.status_code == 400
    assert_oauth_token_error(rejected, "invalid_grant")


def test_oauth_dcr_rate_limit_uses_forwarded_client_ip_when_behind_proxy(page_service):
    """`_consume_dcr_slot` rejects after 20 registrations per 60s from one
    client IP with 400 ``invalid_client_metadata``, keyed by the resolved
    remote IP. Which address that is (peer without a proxy, the peer for an
    untrusted or spoofed X-Forwarded-For, the rightmost untrusted hop behind
    a trusted proxy) is pinned by backend/tests/unit/test_remote_ip.py; this
    case proves DCR keys on that resolution rather than the raw peer."""
    from backend.services import oauth_service as oauth_service_module

    oauth_service_module._DCR_BUCKET.clear()
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                behind_proxy=True,
                # Trust the intermediate proxy 10.0.0.1 so the originating client
                # (203.0.113.4x) is the resolved rate-limit identity.
                forwarded_allow_ips="testclient,10.0.0.1",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    headers_a = {"X-Forwarded-For": "203.0.113.42, 10.0.0.1"}
    headers_b = {"X-Forwarded-For": "203.0.113.43, 10.0.0.1"}

    # Start the resolved client's bucket 19 deep: the 20th request passes and
    # the 21st is refused.
    oauth_service_module._DCR_BUCKET["203.0.113.42"] = [time.monotonic()] * 19
    response = client.post(
        "/oauth/register",
        headers=headers_a,
        json={
            "client_name": "Forwarded Rate Limit Probe",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    )
    assert response.status_code == 201

    limited = client.post(
        "/oauth/register",
        headers=headers_a,
        json={
            "client_name": "Forwarded Rate Limit Overflow",
            "redirect_uris": ["http://127.0.0.1:53750/callback"],
            "scope": "mcp:read",
        },
    )
    assert limited.status_code == 400
    assert limited.json()["error_code"] == "invalid_client_metadata"
    assert "rate limit" in limited.json()["message"].lower()

    other_client_ip = client.post(
        "/oauth/register",
        headers=headers_b,
        json={
            "client_name": "Forwarded Rate Limit Other IP",
            "redirect_uris": ["http://127.0.0.1:53751/callback"],
            "scope": "mcp:read",
        },
    )
    assert other_client_ip.status_code == 201
    oauth_service_module._DCR_BUCKET.clear()


def test_oauth_token_endpoint_rate_limit_returns_429(page_service, monkeypatch):
    """Sprint-4 closure R-F6-1: /oauth/token enforces per-IP + per-client
    rate-limit.

    Pre-fix the token endpoint accepted unlimited authorization_code /
    refresh_token grants per IP. A stolen refresh token could be brute-forced
    against the endpoint without throttle. DCR endpoint had its own bucket
    via `_consume_dcr_slot` but the token grant path did not.

    `consume_token_slot` enforces:
      - per-IP: TOKEN_RATE_LIMIT_PER_IP (120) requests / 60s
      - per-client_id: TOKEN_RATE_LIMIT_PER_CLIENT (60) requests / 60s

    The first slot to fill rejects with HTTP 429 + `rate_limited`.
    """
    from backend.services import oauth_service as oauth_service_module

    oauth_service_module._TOKEN_IP_BUCKET.clear()
    oauth_service_module._TOKEN_CLIENT_BUCKET.clear()
    # The production threshold (60) and its boundary are pinned by
    # test_rate_limit_in_process; a small one keeps the HTTP mapping cheap.
    monkeypatch.setattr(oauth_service_module, "TOKEN_RATE_LIMIT_PER_CLIENT", 3)
    client = make_client(page_service)
    # Use a single client_id so the per-client bucket hits first.
    last_response = None
    for index in range(
        oauth_service_module.TOKEN_RATE_LIMIT_PER_CLIENT + 1
    ):
        last_response = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "ratelimit-probe-client",
                "refresh_token": f"obviously-invalid-{index}",
            },
        )
        if last_response.status_code == 429:
            break

    assert last_response is not None
    assert last_response.status_code == 429, last_response.text
    body = assert_oauth_token_error(last_response, "rate_limited")
    assert "rate limit" in body["error_description"].lower()
    # Cleanup so subsequent tests start fresh.
    oauth_service_module._TOKEN_IP_BUCKET.clear()
    oauth_service_module._TOKEN_CLIENT_BUCKET.clear()


def test_oauth_token_sqlite_lock_timeout_returns_retryable_503(tmp_path):
    from backend.models.base import Base
    from backend.scripts.seed_dev import seed_default_workspace
    from backend.services.page_service import PageService

    database_path = tmp_path / "oauth-token-busy.sqlite"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    client = make_client(PageService(session_factory))

    try:
        with sqlite3.connect(database_path, timeout=0) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": "busy-client",
                    "refresh_token": "rt_busy",
                },
            )
    finally:
        engine.dispose()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert_oauth_token_no_store(response)
    assert response.json() == {
        "message": "token service is temporarily unavailable; retry later"
    }


def test_oauth_token_does_not_mask_non_lock_operational_error(
    page_service,
    monkeypatch,
):
    from backend.services.oauth_service import OAuthService

    def fail_with_disk_error(*_args, **_kwargs):
        raise OperationalError(
            "SELECT hidden",
            {},
            sqlite3.OperationalError("disk I/O error"),
        )

    monkeypatch.setattr(OAuthService, "refresh_access_token", fail_with_disk_error)
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                public_base_url="http://testserver",
                mcp_http_mode="json_stateless",
            ),
            page_service=page_service,
        ),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "disk-error-client",
            "refresh_token": "rt_disk_error",
        },
    )

    assert response.status_code == 500
    assert response.json()["error_code"] == "internal_error"
    assert "Retry-After" not in response.headers


def test_oauth_token_endpoint_rejects_malformed_requests(page_service):
    client = make_client(page_service)

    json_body = client.post(
        "/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": "json-client",
            "refresh_token": "rt_json",
        },
    )
    assert json_body.status_code == 400
    body = assert_oauth_token_error(json_body, "invalid_request")
    assert "form-urlencoded" in body["error_description"]

    missing_form = client.post("/oauth/token")
    assert missing_form.status_code == 400
    # A missing grant_type is a missing required parameter, which RFC 6749
    # §5.2 calls invalid_request -- not a dead credential.
    body = assert_oauth_token_error(missing_form, "invalid_request")
    assert "grant_type" in body["error_description"]

    unsupported = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "test-client"},
    )
    assert unsupported.status_code == 400
    # RFC 6749 §5.2 keeps these apart on purpose: invalid_grant tells the
    # client to throw its refresh token away, which is the wrong advice when
    # only the grant_type was wrong.
    body = assert_oauth_token_error(unsupported, "unsupported_grant_type")
    assert "unsupported grant_type" in body["error_description"]

    unknown_refresh = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": "unknown-refresh-client",
            "refresh_token": "unknown-refresh-token",
        },
    )
    assert unknown_refresh.status_code == 400
    assert_oauth_token_error(unknown_refresh, "invalid_grant")


def test_oauth_revoke_endpoint_rejects_json_body(page_service):
    client = make_client(page_service)

    response = client.post(
        "/oauth/revoke",
        json={"token": "rt_json"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "invalid_request"
    assert "form-urlencoded" in body["message"]


def _postgres_test_url() -> str | None:
    url = os.environ.get("DTXWIKI_POSTGRES_TEST_DATABASE_URL")
    if not url:
        return None
    database_name = urlparse(url).path.rsplit("/", 1)[-1]
    if not database_name.endswith(("_test", "-test")):
        pytest.skip(
            "DTXWIKI_POSTGRES_TEST_DATABASE_URL must point to a *_test or "
            "*-test database because this test drops all tables."
        )
    return url


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="DTXWIKI_POSTGRES_TEST_DATABASE_URL not set; SQLite StaticPool cannot "
    "exercise real concurrency.",
)
def test_oauth_code_redeem_locks_under_concurrency():
    """Two concurrent ``/oauth/token`` redeems of the same authorization code
    must yield exactly one success and one ``invalid_grant`` on PostgreSQL.

    Exercises the ``with_for_update`` row lock added to
    ``OAuthService.exchange_authorization_code``. SQLite has separate
    file-backed coverage for its explicit pre-read write intent.
    """

    from backend.scripts.seed_dev import seed_default_workspace
    from backend.services.page_service import PageService
    from backend.tests.utils.postgres_reset import reset_postgres_test_database

    engine = create_engine(_postgres_test_url())
    reset_postgres_test_database(engine)
    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page_service = PageService(session_factory)
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Race Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state("race-state"),
    }
    authorize = client.post(
        "/oauth/authorize",
        data={
            **authorize_params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    client.cookies.clear()

    def _redeem(_):
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "redirect_uri": "http://127.0.0.1:53682/callback",
                "code": code,
                "code_verifier": verifier,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(_redeem, range(2)))

    status_codes = sorted(r.status_code for r in responses)
    assert status_codes == [200, 400], status_codes
    failure = next(r for r in responses if r.status_code == 400)
    assert_oauth_token_error(failure, "invalid_grant")


def test_oauth_code_redeem_serializes_file_backed_sqlite(
    tmp_path,
    monkeypatch,
):
    from backend.models.base import Base
    from backend.scripts.seed_dev import seed_default_workspace
    from backend.services import oauth_service as oauth_service_module
    from backend.services.page_service import PageService

    database_url = f"sqlite+pysqlite:///{tmp_path / 'oauth-code-race.sqlite'}"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page_service = PageService(session_factory)
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "SQLite Race Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state("sqlite-race-state"),
    }
    authorize = client.post(
        "/oauth/authorize",
        data={
            **authorize_params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    client.cookies.clear()

    first_validated = threading.Event()
    release_first = threading.Event()
    validation_lock = threading.Lock()
    validation_count = 0
    original_validate = oauth_service_module._validate_token_issuance

    def hold_first_validation(*args, **kwargs):
        nonlocal validation_count
        original_validate(*args, **kwargs)
        with validation_lock:
            validation_count += 1
            is_first = validation_count == 1
        if is_first:
            first_validated.set()
            assert release_first.wait(timeout=2)

    monkeypatch.setattr(
        oauth_service_module,
        "_validate_token_issuance",
        hold_first_validation,
    )

    def redeem():
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "redirect_uri": "http://127.0.0.1:53682/callback",
                "code": code,
                "code_verifier": verifier,
            },
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(redeem)
            assert first_validated.wait(timeout=2)
            second = executor.submit(redeem)
            time.sleep(0.1)
            release_first.set()
            responses = [first.result(timeout=5), second.result(timeout=5)]
    finally:
        release_first.set()
        engine.dispose()

    status_codes = sorted(response.status_code for response in responses)
    assert status_codes == [200, 400], status_codes
    failure = next(response for response in responses if response.status_code == 400)
    assert_oauth_token_error(failure, "invalid_grant")


def test_oauth_authorize_rejects_loopback_path_or_host_mismatch(page_service):
    """F6: the RFC 8252 §7.3 loopback exception allows only a runtime port
    change -- not another path, and not a non-loopback (LAN) host."""
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Loopback Mismatch",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()

    for redirect_uri in (
        "http://127.0.0.1:53682/other",
        "http://192.168.1.50:53682/callback",
    ):
        consent = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": registered["client_id"],
                "redirect_uri": redirect_uri,
                "code_challenge": challenge_s256("v" * 64),
                "code_challenge_method": "S256",
                "scope": "mcp:read",
                "state": oauth_state("mismatch-state"),
            },
        )
        assert consent.status_code == 400, redirect_uri


def test_oauth_authorize_post_requires_workspace_id_from_consent_form(page_service):
    """I3/F4-4: approve POST must carry the workspace_ids selected on the
    consent form (P10.3 T3: plural, a full checklist set now)."""
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Missing Workspace POST Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()

    response = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256("v" * 64),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("missing-workspace"),
            "decision": "allow",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"
    assert "workspace_ids" in response.json()["message"]
    assert client.get("/oauth/consents").json()["data"] == []


def test_oauth_authorize_integrity_error_returns_invalid_request(
    page_service,
    monkeypatch,
):
    """F4-8: FK/TOCTOU failures while issuing the code should be 400, not 500."""
    from backend.models.user import User
    from backend.policy.rbac import list_user_workspaces

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Integrity Error Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    with page_service.session_factory() as session:
        user = session.scalars(select(User)).first()
        workspace_id = list_user_workspaces(session, user.id)[0][0].id

    target_engine = page_service.session_factory.kw["bind"]
    original_flush = Session.flush

    def fail_on_target_engine_flush(self, *args, **kwargs):
        from backend.models.oauth import OAuthCode

        if self.bind is target_engine and any(
            isinstance(obj, OAuthCode) for obj in self.new
        ):
            raise IntegrityError("insert oauth_codes", {}, Exception("fk"))
        return original_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_on_target_engine_flush)

    response = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256("v" * 64),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("integrity-error"),
            "workspace_ids": str(workspace_id),
            "decision": "allow",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"


def test_consent_list_and_revoke_round_trip(page_service):
    """C5 backend: list + revoke returns 404 for unknown, 200 + reprompt for
    successful revoke. Revoking also invalidates outstanding refresh tokens so
    the OAuth-aware client must restart the authorize flow.
    """

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Revoke Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state("revoke-state"),
    }
    authorize = client.post(
        "/oauth/authorize",
        data={
            **authorize_params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    refresh_token = token_response.json()["refresh_token"]

    listed = client.get("/oauth/consents")
    assert listed.status_code == 200
    payload = listed.json()["data"]
    assert len(payload) == 1
    assert payload[0]["client_id"] == registered["client_id"]
    assert payload[0]["client_name"] == "Revoke Client"
    # P10.3 T3 (section 2.3): a consent GROUP now carries a list of
    # workspaces, not a singular workspace_id/workspace_title/workspace_slug.
    assert len(payload[0]["workspaces"]) == 1
    assert payload[0]["workspaces"][0]["id"] is not None
    assert payload[0]["workspaces"][0]["title"]
    assert payload[0]["workspaces"][0]["slug"]

    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": registered["client_id"]},
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["revoked"] is True

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": refresh_token,
        },
    )
    assert refreshed.status_code == 400
    assert_oauth_token_error(refreshed, "invalid_grant")

    after_revoke = client.get("/oauth/consents")
    assert after_revoke.json()["data"] == []


def test_url_shaped_cimd_consent_can_be_updated_and_revoked(page_service):
    from datetime import timedelta

    from backend.models.base import utc_now
    from backend.models.oauth import OAuthClient, OAuthConsent, OAuthRefreshToken
    from backend.models.user import User
    from backend.models.workspace import Workspace

    client = make_client(page_service)
    client_id = "https://client.example/oauth/client.json"
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User))
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert user is not None and workspace is not None
        session.add(
            OAuthClient(
                id=client_id,
                client_name="CIMD URL Client",
                redirect_uris=["http://127.0.0.1:53682/callback"],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                application_type="native",
                scope="mcp:read",
                created_via="cimd",
            )
        )
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=client_id,
                workspace_id=workspace.id,
                scope="mcp:read",
            )
        )
        session.add(
            OAuthRefreshToken(
                token_lookup_hash="cimd-url-consent-refresh",
                user_id=user.id,
                client_id=client_id,
                scope="mcp:read",
                expires_at=utc_now() + timedelta(days=1),
            )
        )
        workspace_id = workspace.id

    updated = client.patch(
        "/oauth/consents",
        params={"client_id": client_id, "workspace_id": str(workspace_id)},
        json={"sliding_enabled": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["client_id"] == client_id
    assert updated.json()["data"]["sliding_enabled"] is False

    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": client_id},
    )
    assert revoked.status_code == 200, revoked.text
    assert client.get("/oauth/consents").json()["data"] == []


def test_consent_revoke_invalidates_unconsumed_authorization_code(page_service):
    client = make_client(page_service)
    client_id = register_oauth_client(client, name="Revoke Pending Code Client")
    verifier = "v" * 64
    params = authorize_params(client_id, verifier, state="pending-code")
    authorize = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]

    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": client_id},
    )
    assert revoked.status_code == 200

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )

    assert token_response.status_code == 400
    assert_oauth_token_error(token_response, "invalid_grant")


def _assert_rest_error_envelope(body: dict, error_code: str) -> None:
    """Consent endpoints serve the authenticated frontend, so their errors
    must carry the full RESTErrorBody shape (envelope, request_id, warnings)
    that the typed frontend client declares as required."""
    assert body["error_code"] == error_code
    assert body["envelope"]["error_code"] == error_code
    assert body["request_id"]
    assert body["warnings"] == []


def test_consent_endpoints_reject_unknown_client_and_missing_session(page_service):
    client = make_client(page_service)
    unknown = client.delete(
        "/oauth/consents",
        params={"client_id": "non-existent-client"},
    )
    assert unknown.status_code == 404
    _assert_rest_error_envelope(unknown.json(), "consent_not_found")

    client.cookies.clear()
    listed = client.get("/oauth/consents")
    assert listed.status_code == 401
    _assert_rest_error_envelope(listed.json(), "unauthorized")
    revoked = client.delete(
        "/oauth/consents",
        params={"client_id": "some-client"},
    )
    assert revoked.status_code == 401
    _assert_rest_error_envelope(revoked.json(), "unauthorized")


def test_oauth_grant_replaces_previous_workspace_set_and_tokens_carry_no_claim(
    page_service,
):
    """P10.3 T3 (section 2.3, D-01v2/D-01v3-d): supersedes the pre-T3 'code/
    refresh chain stays bound to its issuing workspace' pin, which relied on
    two things this redesign removed: (1) the access JWT carrying a
    ``workspace_id`` claim at all -- D-01v2/D-01v3-d dropped it, a
    credential's access is the live ``oauth_consents`` allowed-workspace set
    read per request, never baked into the token -- and (2)
    ``issue_authorization_code`` accepting a single ``workspace_id`` (now
    ``workspace_ids``, a full SET). What replaces the old pin:
    ``issue_authorization_code(grant_consent=True)`` is a full-set REPLACE
    of the (user, client) consent group -- granting a second, different
    ``workspace_ids`` set supersedes (hard-deletes) the first grant's row
    rather than adding to it -- and the resulting access/refresh tokens
    carry no workspace claim whatsoever.
    """
    import jwt as pyjwt

    from backend.app.settings import Settings
    from backend.models.oauth import OAuthConsent
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership, list_user_workspaces
    from backend.services.oauth_service import OAuthService

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Workspace Binding Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        default_workspace_id = list_user_workspaces(session, user.id)[0][0].id
        second_workspace = Workspace(
            slug="second",
            title="Second Workspace",
            name="Second Workspace",
            description="Regression workspace",
            goal="OAuth boundary regression",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=second_workspace.id,
            role="owner",
        )
        second_workspace_id = second_workspace.id

    base_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
    }
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
            public_base_url="http://testserver",
        ),
        public_base_url="http://testserver",
    )
    first_code = service.issue_authorization_code(
        user_id=user.id,
        state=oauth_state("first-workspace"),
        workspace_ids=[default_workspace_id],
        **base_params,
    )
    service.issue_authorization_code(
        user_id=user.id,
        state=oauth_state("second-workspace"),
        workspace_ids=[second_workspace_id],
        **base_params,
    )

    # The second grant call replaced the first's allowed-workspace set
    # entirely -- only second_workspace_id's consent row is still live.
    with page_service.session_factory() as session:
        live_workspace_ids = set(
            session.scalars(
                select(OAuthConsent.workspace_id).where(
                    OAuthConsent.user_id == user.id,
                    OAuthConsent.client_id == registered["client_id"],
                    OAuthConsent.removed_at.is_(None),
                )
            ).all()
        )
    assert live_workspace_ids == {second_workspace_id}

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": first_code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    token_payload = token_response.json()
    access_payload = pyjwt.decode(
        token_payload["access_token"],
        options={"verify_signature": False, "verify_aud": False},
    )
    assert "workspace_id" not in access_payload
    assert "workspace_ids" not in access_payload
    assert set(access_payload) == {
        "iss",
        "sub",
        "aud",
        "exp",
        "iat",
        "jti",
        "client_id",
        "grant_id",
        "scope",
    }

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": registered["client_id"],
            "refresh_token": token_payload["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_payload = pyjwt.decode(
        refreshed.json()["access_token"],
        options={"verify_signature": False, "verify_aud": False},
    )
    assert "workspace_id" not in refreshed_payload


def test_oauth_authorize_get_skips_checklist_for_any_hinted_workspace_once_consented(
    page_service,
):
    """P10.3 T3 (section 2.3): supersedes the pre-T3 'GET must re-prompt for
    an unconsented requested workspace' pin. ``has_existing_consent`` is a
    (user, client)-GROUP fact now, not per-workspace, so once ANY live
    consent exists for this client, GET always skips the checklist and
    goes straight to the handoff page -- regardless of which workspace the
    (now purely informational) ``workspace_id`` hint names. Expanding the
    credential's allowed-workspace set to cover a workspace named only by a
    GET hint is not possible any more; that needs an explicit checklist-
    driven POST (Settings' ``PATCH /oauth/consents``, or a fresh
    ``/oauth/authorize`` POST naming it in ``workspace_ids``)."""
    from backend.models.oauth import OAuthConsent
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Workspace Prompt Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        second_workspace = Workspace(
            slug="prompt-second",
            title="Prompt Second Workspace",
            name="Prompt Second Workspace",
            description="GET consent prompt regression",
            goal="OAuth prompt regression",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=second_workspace.id,
            role="owner",
        )
        second_workspace_id = second_workspace.id

    base_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256("v" * 64),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
    }
    default_grant = client.post(
        "/oauth/authorize",
        data={
            **base_params,
            "state": oauth_state("default-grant"),
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert default_grant.status_code == 303

    second_prompt = client.get(
        "/oauth/authorize",
        params={
            **base_params,
            "state": oauth_state("second-prompt"),
            "workspace_id": str(second_workspace_id),
        },
        follow_redirects=False,
    )

    assert second_prompt.status_code == 302, second_prompt.text
    query = parse_qs(urlparse(second_prompt.headers["location"]).query)
    assert query["state"] == [oauth_state("second-prompt")]
    assert "code" in query

    # The granted set is still just the original default workspace -- the
    # GET hint for second_workspace_id never expanded it.
    with page_service.session_factory() as session:
        live_workspace_ids = set(
            session.scalars(
                select(OAuthConsent.workspace_id).where(
                    OAuthConsent.user_id == user.id,
                    OAuthConsent.client_id == registered["client_id"],
                    OAuthConsent.removed_at.is_(None),
                )
            ).all()
        )
    assert second_workspace_id not in live_workspace_ids


def test_oauth_workspace_list_denies_after_consent_revoked(page_service):
    """Real-Codex review finding (2026-09-06): ``GET /api/v1/workspaces`` has
    no path ``workspace_id``, so it used to reach ``require_effective_token_
    scope`` directly, skipping the live-consent re-check every ``{workspace_
    id}``-scoped REST route already gets via ``actor_with_target_workspace_
    scope``. That let a revoked-but-not-yet-expired OAuth access JWT keep
    listing the workspace it names, violating CORE §5.4 ("a request starting
    after a revoke commits must read the new authority state"). Deleting the
    OAuthConsent row directly (rather than calling revoke_consent, which also
    jti-revokes the token) isolates this from token-level revocation and
    proves the live-consent gate itself, independent of it. Before the
    revoke, the list holds only the consented workspace, not every
    membership of the user."""
    from sqlalchemy import delete

    from backend.models.oauth import OAuthConsent
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        extra_workspace = Workspace(
            slug="extra",
            title="Extra Workspace",
            name="Extra Workspace",
            description="Should not be listed for scoped token",
            goal="OAuth list filtering regression",
        )
        session.add(extra_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=extra_workspace.id,
            role="owner",
        )
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Workspace List Revoke Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    workspace_id = current_workspace_id(client)
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("workspace-list-revoke"),
            "workspace_ids": workspace_id,
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    access_token = token_response.json()["access_token"]
    client.cookies.clear()

    before_revoke = client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert before_revoke.status_code == 200
    slugs = [item["slug"] for item in before_revoke.json()["data"]["workspaces"]]
    assert slugs == ["default"]

    with page_service.session_factory.begin() as session:
        session.execute(
            delete(OAuthConsent).where(
                OAuthConsent.client_id == registered["client_id"],
            )
        )

    after_revoke = client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert after_revoke.status_code == 403
    assert after_revoke.json()["error_code"] == "token_workspace_denied"


def test_oauth_jwt_read_scope_rejected_on_rest_write(page_service):
    """Read-only OAuth JWT must not satisfy REST write endpoints."""
    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Read Only Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize_params = {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp:read",
        "state": oauth_state("state-readonly"),
    }
    authorize = client.post(
        "/oauth/authorize",
        data={
            **authorize_params,
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    access_token = token_response.json()["access_token"]
    workspace_id = current_workspace_id(client)
    client.cookies.clear()

    write_attempt = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"title": "Should fail", "body_md": "# nope"},
    )

    assert write_attempt.status_code == 403
    assert write_attempt.json()["error_code"] == "scope_denied"


def test_oauth_jwt_uses_target_workspace_role_for_restricted_search(
    page_service, media_installer_factory
):
    """Fix-4 F3-2: default-workspace owner role must not leak into a
    target workspace where the same user is only a viewer.
    """
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership
    from backend.scripts.seed_dev import seed_default_workspace
    from backend.services.extraction_service import ExtractionService
    from backend.services.library_service import LibraryService

    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        target_workspace = seed_default_workspace(
            session,
            slug="viewer-target",
            title="Viewer Target",
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=target_workspace.id,
            role="viewer",
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=target_workspace.id,
            canonical_uri="https://example.com/target-restricted-library",
            title="Target Restricted Source",
            trust_level="restricted",
            raw_text=(
                "# Target Restricted Source\n\n"
                "Needle target restricted artifact body"
            ),
        )
        ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        target_workspace_id = target_workspace.id

    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Target Role Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("target-role"),
            "workspace_ids": str(target_workspace_id),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    client.cookies.clear()

    search_response = client.post(
        f"/api/v1/workspaces/{target_workspace_id}/search",
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
        json={"query": "needle target restricted", "scope": "all"},
    )

    assert search_response.status_code == 200
    payload = search_response.json()["data"]
    assert {result["type"] for result in payload["ranked_results"]}.isdisjoint(
        {"library_item", "library_artifact"}
    )
    assert (
        payload["permission_note"]
        == "Restricted Library items and artifacts were excluded."
    )

    mcp_search = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {
                    "workspace_id": str(target_workspace_id),
                    "query": "needle target restricted",
                    "scope": "all",
                },
            },
        },
    )
    mcp_payload = mcp_search.json()["result"]["structuredContent"]["data"]

    assert mcp_search.status_code == 200
    assert {result["type"] for result in mcp_payload["ranked_results"]}.isdisjoint(
        {"library_item", "library_artifact"}
    )
    assert (
        mcp_payload["permission_note"]
        == "Restricted Library items and artifacts were excluded."
    )


def test_oauth_consent_screen_names_target_workspace(page_service):
    """Fix-4 F3-3: consent page must visibly name the target workspace."""
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership
    from backend.scripts.seed_dev import seed_default_workspace

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Visible Workspace Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        workspace = seed_default_workspace(
            session,
            slug="visible-consent",
            title="Visible Consent Workspace",
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        workspace_id = workspace.id

    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256("v" * 64),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("visible-workspace"),
            "workspace_id": str(workspace_id),
        },
    )

    assert response.status_code == 200
    assert "Visible Consent Workspace" in response.text
    assert "visible-consent" in response.text


def test_consent_form_preselects_resolved_workspace_when_no_hint_given(page_service):
    """P10.3 T3 (section 2.3): supersedes the pre-T3 'GET consent must pin
    the resolved workspace into POST via a hidden field' pin. There is no
    hidden ``workspace_id`` field any more -- the resolved default
    workspace (the same ``resolve_workspace_for_actor`` single-membership
    fallback the old pin exercised) is instead the pre-checked row in the
    ``workspace_ids`` checklist, which the user can still uncheck before
    submitting."""
    from datetime import timedelta

    from backend.models.user import User
    from backend.models.workspace import WorkspaceMembership

    client = make_client(page_service)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Hidden Workspace Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    # make_client's user holds two workspaces: the seeded "default" it selects
    # and the Personal one signup created. They are stamped a fraction of a
    # second apart, so which one a created_at-ordered listing puts first is
    # whatever the wall clock did between the two inserts (a backward step
    # lists Personal first). Name the resolved workspace by slug, not by list
    # position, and pin Personal as the older one so the checklist has to
    # follow the user's default rather than the first row.
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        seeded = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        personal = session.scalars(
            select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(WorkspaceMembership.user_id == user.id, Workspace.id != seeded.id)
        ).one()
        personal.created_at = seeded.created_at - timedelta(seconds=1)
        default_workspace_id = seeded.id

    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256("v" * 64),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("hidden-workspace"),
        },
    )

    assert response.status_code == 200
    assert 'type="hidden" name="workspace_id"' not in response.text
    assert _workspace_checkbox_checked(response.text, default_workspace_id)


def test_mcp_http_envelope_preserves_token_workspace_denied(page_service):
    """Fix-6 Task 1.2-E (M-F5-2): when the token's live allowed-workspace set
    does not cover the target workspace, the MCP layer must surface the
    distinct workspace ambiguity error.

    Sprint_5 Phase_0~2 fix-round (SEC-1): the handler-entry workspace check
    now raises eagerly (no silent swallow), so this test asserts the 400
    JSON-RPC transport envelope with the `workspace_ambiguous` keyword
    pinned in `error.data.error`. The per-tool envelope path is still
    covered by test_mcp_router.py.

    P10.3 T3 (D-01v2/D-01v3-d) permanently drops the JWT's singular
    `workspace_id` claim -- the access token now carries no workspace claim
    at all. This scenario still reaches `resolve_workspace_for_actor`'s
    mismatch branch, though: `token_workspace_ids` is no longer read off the
    JWT, it is the token's LIVE `oauth_consents` allowed-workspace set,
    fetched per request by `backend.policy.auth._authenticate_oauth_token`
    (T2) and threaded into `ActorContext` exactly as an API token's
    workspace grants already are. Since that set is non-empty ({the granted
    workspace}) and does not contain the foreign one requested,
    `resolve_workspace_for_actor` still raises `WorkspaceAmbiguityError`
    before the request ever reaches `check_actor_can_access` -- the same
    400 `workspace_ambiguous` shape as before T3, just resolved from a live
    query instead of a token claim.
    """
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership

    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        foreign = Workspace(
            slug="mcp-foreign-token",
            title="MCP Foreign Token",
            name="MCP Foreign Token",
            description="Fix-6 Task 1.2-E envelope preservation",
            goal="Fix-6 MCP error_code distinctness",
        )
        session.add(foreign)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=foreign.id,
            role="owner",
        )
        foreign_workspace_id = foreign.id

    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Token Denied Client",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": "mcp:read",
        },
    ).json()
    verifier = "v" * 64
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": "mcp:read",
            "state": oauth_state("token-denied"),
            "workspace_ids": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    access_token = token_response.json()["access_token"]
    client.cookies.clear()

    mcp_search = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {
                    "workspace_id": str(foreign_workspace_id),
                    "query": "needle",
                },
            },
        },
    )

    assert mcp_search.status_code == 400
    payload = assert_json_rpc_error(
        mcp_search.json(),
        request_id=1,
        error="workspace_ambiguous",
    )
    assert "workspace" in payload["message"].lower()


def _bootstrap_refresh_session(
    client: TestClient,
    *,
    scope: str = "mcp:read",
    state: str = "refresh-invariant",
):
    """Helper: register OAuth client, authorize, exchange code, return
    (client_id, refresh_token, workspace_id) for Task 1.3 invariant tests."""

    from uuid import UUID as _UUID

    state = oauth_state(state)
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": f"Refresh Invariant {state}",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": scope,
        },
    ).json()
    workspace_id = current_workspace_id(client)
    verifier = "v" * 64
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code_challenge": challenge_s256(verifier),
            "code_challenge_method": "S256",
            "scope": scope,
            "state": state,
            "workspace_ids": workspace_id,
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 303
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "redirect_uri": "http://127.0.0.1:53682/callback",
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    return (
        registered["client_id"],
        token_response.json()["refresh_token"],
        _UUID(workspace_id),
    )


def test_oauth_refresh_rejects_when_consent_scope_narrowed(page_service):
    """Fix-6 Task 1.3 (H-F5-CODEX-2) invariant 2 — scope coverage.

    If the consent.scope is narrowed below the refresh-token scope (e.g. a
    future consent re-prompt downgrades from mcp:write to mcp:read while a
    write-scoped refresh token survives), refresh must reject.
    """
    from backend.models.oauth import OAuthConsent
    from backend.models.user import User

    client = make_client(page_service)
    client_id, refresh_token, workspace_id = _bootstrap_refresh_session(
        client, scope="mcp:write", state="consent-narrow-scope"
    )
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        consent = session.get(OAuthConsent, (user.id, client_id, workspace_id))
        assert consent is not None
        consent.scope = "mcp:read"
        consent.desired_scopes = ["mcp:read"]

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )

    assert refreshed.status_code == 400
    body = assert_oauth_token_error(refreshed, "invalid_grant")
    assert "consent" in body["error_description"], (
        "_validate_token_issuance did not fire — got message: "
        f"{body['error_description']!r}"
    )


def test_oauth_refresh_survives_workspace_membership_removal_but_requests_are_denied(
    page_service,
):
    """P10.3 T3 (D-01v3-g) supersedes the pre-T3 'refresh must reject when
    workspace membership is removed' invariant. ``_validate_token_issuance``
    (shared by exchange + refresh) dropped the membership check entirely --
    issuance is control-plane only and does not know a target workspace yet
    (see its docstring), so a refresh token now SURVIVES membership removal
    and mints a fresh access/refresh pair. The membership boundary moved to
    REQUEST time instead: ``check_actor_can_access`` (backend/policy/
    trust_boundaries.py) re-checks live ``WorkspaceMembership`` on every
    call naming a target workspace and raises ``PermissionError(
    "workspace_membership_missing: ...")`` when it is gone -- so the
    credential is just as dead in practice, only the failure point moved
    from refresh to first use. A ``{workspace_id}``-scoped REST route (this
    one included) additionally collapses that denial to a uniform 404
    ``not_found`` rather than a 403, via ``scope_actor_to_request_workspace``
    / ``workspace_existence_is_hidden`` (backend/policy/rest_scopes.py,
    backend/policy/workspace_resolution.py) -- pre-existing, unrelated-to-T3
    behavior that hides a private workspace's existence from a caller with
    no live membership, so an ex-member cannot even confirm the workspace
    still exists.
    """
    from backend.models.user import User
    from backend.policy.rbac import remove_workspace_membership

    client = make_client(page_service)
    client_id, refresh_token, workspace_id = _bootstrap_refresh_session(
        client, state="membership-removed"
    )
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        remove_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace_id,
        )

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )

    assert refreshed.status_code == 200, refreshed.text
    access_token = refreshed.json()["access_token"]

    denied = client.get(
        f"/api/v1/workspaces/{workspace_id}/activity",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"period": "all", "limit": 50},
    )
    assert denied.status_code == 404, denied.text
    assert denied.json()["error_code"] == "not_found"


def _craft_signed_jwt(page_service, *, workspace_id_claim, user_id):
    """Sign a JWT against the live OAuth service so verify_access_token's
    decode path runs. Used by the R-F6-3 malformed-claim regression tests.

    Sprint_5 Phase_3 Task 3.4 (ADR A5): verify_access_token's jti denylist
    check now runs immediately after JWT decode. To exercise the
    workspace_ids claim guards downstream, the test must seed a matching
    row in ``oauth_access_token_jti`` so the denylist check passes and
    the claim validator is the next gate the JWT meets.
    """
    import time as _time
    from datetime import timedelta
    from uuid import uuid4

    import jwt as pyjwt

    from backend.app.settings import Settings
    from backend.models.base import utc_now as _utc_now
    from backend.models.oauth import (
        OAuthAccessTokenJti,
        OAuthClient,
        OAuthRefreshToken,
    )
    from backend.services.oauth_service import (
        JWT_KID,
        OAuthService,
        _private_key_pem,
    )

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        public_base_url="http://testserver",
    )
    service = OAuthService(
        session_factory=page_service.session_factory,
        settings=settings,
        public_base_url="http://testserver",
    )
    jti = f"malformed-claim-probe-{uuid4()}"
    client_id = f"probe-client-{uuid4().hex[:8]}"
    now = int(_time.time())
    payload = {
        "iss": service.issuer,
        "sub": str(user_id),
        "aud": service.resource,
        "exp": now + 60,
        "iat": now,
        "jti": jti,
        "client_id": client_id,
        "scope": "mcp:read",
        "workspace_id": workspace_id_claim,
    }
    token = pyjwt.encode(
        payload,
        _private_key_pem(settings),
        algorithm="RS256",
        headers={"kid": JWT_KID},
    )
    with page_service.session_factory.begin() as session:
        client = OAuthClient(
            id=client_id,
            client_name="probe",
            redirect_uris=["http://127.0.0.1:1/cb"],
            token_endpoint_auth_method="none",
            grant_types=["refresh_token"],
            response_types=["code"],
            application_type="native",
            scope="mcp:read",
            created_via="probe",
        )
        session.add(client)
        session.flush()
        refresh_row = OAuthRefreshToken(
            token_lookup_hash=f"probe-lookup-{jti}",
            hashed_token=None,
            user_id=user_id,
            client_id=client_id,
            scope="mcp:read",
            expires_at=_utc_now() + timedelta(seconds=3600),
        )
        session.add(refresh_row)
        session.flush()
        session.add(
            OAuthAccessTokenJti(
                jti=jti,
                refresh_token_id=refresh_row.id,
                user_id=user_id,
                client_id=client_id,
                expires_at=_utc_now() + timedelta(seconds=3600),
            )
        )
    return service, token


def test_oauth_jwt_ignores_the_workspace_claim_whatever_it_holds(page_service):
    """P10.3 T3 (D-01v2/D-01v3-d) supersedes the pre-T3 quartet of tests
    that pinned ``verify_access_token`` rejecting a null/empty/malformed
    ``workspace_id`` JWT claim and accepting a well-formed one as "the
    actor's one bound workspace". That whole claim-validation gate is gone:
    ``_issue_tokens`` stopped minting a ``workspace_id`` claim at all, and
    ``verify_access_token`` never reads ``payload.get("workspace_id")`` --
    ``OAuthAccessTokenActor`` (backend/services/oauth_service.py) has no
    ``workspace_id`` field to populate. The original security property
    ("a token with no valid workspace binding must not silently pass
    authentication") is preserved by a DIFFERENT mechanism now: a live
    ``oauth_consents`` lookup, made per request by
    ``backend.policy.auth._authenticate_oauth_token``, not by this
    claim-shape check. So a hand-crafted JWT's ``workspace_id`` claim --
    whatever value it holds, valid UUID or garbage -- is simply inert:
    verification succeeds or fails for entirely unrelated reasons (jti
    denylist, user status), and the claim's value never appears on the
    resulting actor.

    Covers a malformed claim (formerly rejected) and a well-formed one (the
    seeded workspace's UUID, formerly bound as the actor's workspace): one
    app and user, one crafted JWT per value. A null or empty claim takes the
    same never-read path.
    """
    from backend.models.user import User
    from backend.models.workspace import Workspace

    make_client(page_service)  # seed default user
    with page_service.session_factory() as session:
        user = session.scalars(select(User)).first()
        assert user is not None
        user_id = user.id
        workspace = session.scalars(select(Workspace)).first()
        assert workspace is not None
        workspace_id = workspace.id

    for workspace_id_claim in ("not-a-uuid", str(workspace_id)):
        service, token = _craft_signed_jwt(
            page_service, workspace_id_claim=workspace_id_claim, user_id=user_id
        )
        actor = service.verify_access_token(token)
        assert not hasattr(actor, "workspace_id"), workspace_id_claim
        assert actor.user.user_id == user_id, workspace_id_claim
        assert actor.scopes == ("mcp:read",), workspace_id_claim


# ---------------------------------------------------------------------------
# Sprint_5 Phase_2 fix-round-3 (Bot-W2) + Sprint_6 OAuth browser UX — OAuth
# consent multi-workspace contract pin. A multi-membership user who hits
# GET /oauth/authorize without an explicit workspace_id must choose a single
# workspace in a browser UI before consent. The final consent form still
# carries workspace_id as a hidden input so POST cannot silently shift scope.
# ---------------------------------------------------------------------------


def _seed_second_workspace_for_w2(page_service, *, slug: str):
    """Add a second workspace membership for the seeded test user so the user
    has multi-membership ambiguity at consent time.

    The general legacy client fixture explicitly selects ``default``. These
    picker tests clear that choice after adding the extra membership rather
    than accidentally testing the persisted-default fast path.
    """
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership

    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        assert user is not None
        second_workspace = Workspace(
            slug=slug,
            title=f"Second {slug}",
            name=f"Second {slug}",
            description="Bot-W2 fix-round-3 multi-membership fixture",
            goal="Bot-W2 multi-membership UX pin",
        )
        session.add(second_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=second_workspace.id,
            role="owner",
        )
        user.default_workspace_id = None
        return user.id, second_workspace.id


def _seeded_default_workspace_id(page_service):
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
    assert workspace_id is not None
    return workspace_id


def test_oauth_authorize_GET_multi_membership_without_workspace_id_prompts_picker(
    page_service,
):
    """Browser users with multiple memberships should not see raw JSON or a
    dead-end error when a native MCP client cannot send workspace_id.

    P10.3 T3 (section 2.3) replaced the old single-``<select>``/hidden-field
    design with a checkbox checklist (``workspace_ids``): with no existing
    consent and genuine membership ambiguity (default pointer cleared, see
    ``_seed_second_workspace_for_w2``), nothing is
    auto-checked; a GET ``workspace_id`` hint only pre-checks the matching
    row, it does not bypass the checklist or pin a hidden field.
    """
    from uuid import uuid4

    client = make_client(page_service)
    _, second_workspace_id = _seed_second_workspace_for_w2(
        page_service, slug="w2-get-multi"
    )
    client_id = register_oauth_client(client, name="W2 GET Multi Client")
    verifier = "v" * 64

    response = client.get(
        "/oauth/authorize",
        params=authorize_params(client_id, verifier, state="w2-get-multi"),
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "Choose which workspaces may access this connection" in response.text
    assert f'value="{second_workspace_id}"' in response.text
    assert not _workspace_checkbox_checked(response.text, second_workspace_id)
    assert '<select name="workspace_id"' not in response.text
    assert '<select name="workspace_ids"' not in response.text

    selected = client.get(
        "/oauth/authorize",
        params={
            **authorize_params(client_id, verifier, state="w2-get-multi-selected"),
            "workspace_id": str(second_workspace_id),
        },
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert selected.status_code == 200, selected.text
    assert "Authorize dtxWiki MCP access" in selected.text
    # The GET workspace_id is a preselect HINT into the checklist now, not a
    # bypassing hidden field (P10.3 T3 removed the hidden-field surface: a
    # GET handler resolving and silently pinning exactly one workspace).
    assert _workspace_checkbox_checked(selected.text, second_workspace_id)
    assert 'type="hidden" name="workspace_id"' not in selected.text
    assert '<select name="workspace_id"' not in selected.text
    assert '<select name="workspace_ids"' not in selected.text
    assert 'name="workspace_ids"' in selected.text

    # A hint naming a workspace the user is not a member of is
    # membership-validated inside ``authorize_workspace_options`` and silently
    # dropped (never a 400): the checklist cannot be bypassed by a GET query
    # param, and POST's ``issue_authorization_code`` is the enforcement point
    # (test_oauth_authorize_POST_multi_membership_workspace_must_be_a_membership).
    foreign_workspace_id = uuid4()
    foreign = client.get(
        "/oauth/authorize",
        params={
            **authorize_params(client_id, verifier, state="w2-get-multi-foreign"),
            "workspace_id": str(foreign_workspace_id),
        },
        headers={"accept": "text/html"},
    )
    assert foreign.status_code == 200, foreign.text
    assert str(foreign_workspace_id) not in foreign.text
    # An unmatched hint falls through to the ambiguous-membership case, same
    # as no hint at all -- nothing gets auto-checked.
    assert not _workspace_checkbox_checked(foreign.text, second_workspace_id)


def test_oauth_authorize_POST_multi_membership_workspace_must_be_a_membership(
    page_service,
):
    """Bot-W2 pin with its ADR A6 positive control: a multi-membership user
    who POSTs a workspace_ids entry they have NO membership in receives a
    400 mentioning 'workspace' (P10.3 T3's issue_authorization_code
    validates every requested id against live membership: "workspace access
    is required"), while naming one of their memberships gets the 303
    redirect carrying the authorization code (P10.3 T3: the field is
    workspace_ids now, plural, not the old singular workspace_id)."""
    from uuid import uuid4

    client = make_client(page_service)
    _, second_workspace_id = _seed_second_workspace_for_w2(
        page_service, slug="w2-post-membership"
    )
    client_id = register_oauth_client(client, name="W2 POST Membership Client")
    verifier = "v" * 64
    foreign_workspace_id = uuid4()  # never granted membership

    # Sprint_5 Phase_3 Task 3.3: prime the CSRF cookie via a GET against a
    # workspace the user *does* have membership in, so the POST's
    # state-binding check passes and the request reaches the workspace
    # access validation we want to pin here.
    client.get(
        "/oauth/authorize",
        params={
            **authorize_params(client_id, verifier, state="w2-post-denied"),
            "workspace_id": str(second_workspace_id),
        },
    )

    denied = client.post(
        "/oauth/authorize",
        data={
            **authorize_params(client_id, verifier, state="w2-post-denied"),
            "decision": "allow",
            "workspace_ids": str(foreign_workspace_id),
        },
        follow_redirects=False,
    )

    assert denied.status_code == 400, denied.text
    body = denied.json()
    assert body["error_code"] == "invalid_request"
    assert "workspace" in body["message"].lower()

    allowed = client.post(
        "/oauth/authorize",
        data={
            **authorize_params(client_id, verifier, state="w2-post-allowed"),
            "decision": "allow",
            "workspace_ids": str(second_workspace_id),
        },
        follow_redirects=False,
    )

    assert allowed.status_code == 303, allowed.text
    qs = parse_qs(urlparse(allowed.headers["location"]).query)
    assert qs.get("state") == [oauth_state("w2-post-allowed")]
    assert "code" in qs and qs["code"][0]


# ---------------------------------------------------------------------------
# Sprint_5 Phase_3 Task 3.7 (TEST-FR3MID-1): CSRF auto-prime bypass guard.
# Future negative CSRF tests added to this file must not be silently
# rescued by the auto-prime wrapper. The bypass triggers whenever the
# pytest node id contains a CSRF keyword.
# ---------------------------------------------------------------------------


def test_csrf_auto_prime_is_bypassed_for_csrf_negative_tests(page_service):
    """The auto-prime wrapper installed by ``make_client`` must NOT fire
    when the running test name contains a CSRF keyword. Without this
    guard a future ``test_..._csrf_...`` negative test that POSTs without
    a cookie would still pass because the wrapper would issue a GET +
    set the cookie before the POST — flipping a CSRF negative into a
    false positive.

    The test's own name contains ``csrf`` so PYTEST_CURRENT_TEST triggers
    the bypass; we exercise the bypass by posting WITHOUT priming first
    and asserting the server rejects the missing cookie + form state
    with the standard ``state binding failed`` body."""

    client = make_client(page_service)
    client_id = register_oauth_client(client, name="CSRF Bypass Guard Client")
    verifier = "v" * 64
    params = authorize_params(client_id, verifier, state="csrf-bypass-AAAA")
    # No prior GET; no cookie set. The wrapper must NOT auto-issue a GET
    # here because the test name carries the ``csrf`` keyword.
    response = client.post(
        "/oauth/authorize",
        data={
            **params,
            "workspace_id": current_workspace_id(client),
            "decision": "allow",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "invalid_request"
    assert "state binding" in body["message"]


def _record_oauth_consent(
    page_service,
    *,
    user_id,
    client_id,
    workspace_id,
    scope="mcp:read",
):
    from uuid import UUID

    from backend.models.oauth import OAuthConsent

    if isinstance(workspace_id, str):
        workspace_id = UUID(workspace_id)
    with page_service.session_factory.begin() as session:
        session.add(
            OAuthConsent(
                user_id=user_id,
                client_id=client_id,
                workspace_id=workspace_id,
                scope=scope,
            )
        )


def test_oauth_authorize_zero_consent_shows_picker_any_existing_consent_skips_it(
    page_service,
):
    """P10.3 T3 (section 2.3) supersedes the pre-T3 'zero or two covering
    consents keep the picker' pin. ``has_existing_consent`` is a (user,
    client) GROUP fact now, not per-workspace -- a live consent covering
    even ONE workspace is enough to skip the checklist entirely on GET and
    go straight to the handoff page. The old distinction between "one
    workspace already consented" (ambiguous, kept the picker) and "several"
    is gone: set-editing after the first grant is Settings' job now, not
    this screen's. Only genuinely zero live consents still show the
    checklist."""
    client = make_client(page_service)
    user_id, second_workspace_id = _seed_second_workspace_for_w2(
        page_service, slug="a5-ambiguous"
    )
    client_id = register_oauth_client(client, name="A5 Ambiguous Client")
    params = authorize_params(client_id, "v" * 64, state="a5-no-consent")

    no_consent = client.get(
        "/oauth/authorize", params=params, headers={"accept": "text/html"}
    )
    assert no_consent.status_code == 200
    assert "Choose which workspaces may access this connection" in no_consent.text

    first_workspace_id = _seeded_default_workspace_id(page_service)
    _record_oauth_consent(
        page_service,
        user_id=user_id,
        client_id=client_id,
        workspace_id=first_workspace_id,
    )
    one_consent = client.get(
        "/oauth/authorize",
        params={**params, "state": oauth_state("a5-one-consent")},
        headers={"accept": "text/html"},
    )
    assert one_consent.status_code == 200
    assert "Returning to the app" in one_consent.text

    _record_oauth_consent(
        page_service,
        user_id=user_id,
        client_id=client_id,
        workspace_id=second_workspace_id,
    )
    two_consents = client.get(
        "/oauth/authorize",
        params={**params, "state": oauth_state("a5-two-consents")},
        headers={"accept": "text/html"},
    )
    assert two_consents.status_code == 200
    assert "Returning to the app" in two_consents.text
