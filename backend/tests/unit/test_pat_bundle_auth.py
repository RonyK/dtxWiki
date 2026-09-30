"""Singular-workspace PAT auth layer.

D-01: the ``X-Dtxwiki-Tokens`` bundle header and the ``X-Dtxwiki-Pin`` header
(260901 5안 종합형's "one MCP client registration reaches every workspace the
caller holds a PAT for") were removed outright, not preserved behind a flag.
A PAT now authenticates as exactly the one workspace it was issued for, full
stop -- no bundle, no aggregation, no pin. This file used to exercise that
removed multi-workspace mechanism; it now exercises the equivalent singular-
credential invariants the bundle/pin tests used to guard: a token cannot
reach a workspace other than its own, a revoked token is denied outright
with no fallback identity, and the scope actually enforced for a call is
always the presented token's own grant for its own workspace, never a wider
one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.rbac import grant_workspace_membership
from backend.services.api_token_service import ApiTokenService, WorkspaceGrantRequest
from backend.services.credential_hashing import CredentialHasher
from backend.tests.conftest import authenticate_test_client_in_workspace


def _make_client(
    page_service,
    *,
    settings_overrides: dict[str, Any] | None = None,
) -> TestClient:
    settings_values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": "test-session-secret",
        "public_base_url": "http://testserver",
        "mcp_http_mode": "json_stateless",
        "signup_enabled": True,
        **(settings_overrides or {}),
    }
    client = TestClient(
        create_app(settings=Settings(**settings_values), page_service=page_service)
    )
    default_workspace_id = authenticate_test_client_in_workspace(
        client, page_service.session_factory
    )
    return client, default_workspace_id


def _second_workspace(page_service, *, user_id) -> str:
    with page_service.session_factory.begin() as session:
        workspace = Workspace(
            id=uuid4(),
            slug=f"second-{uuid4().hex[:8]}",
            title="Second Workspace",
            name="Second Workspace",
        )
        session.add(workspace)
        session.flush()
        grant_workspace_membership(
            session, user_id=user_id, workspace_id=workspace.id, role="owner"
        )
        workspace_id = workspace.id
    return str(workspace_id)


def _issue_pat(
    page_service,
    *,
    user_id,
    workspace_id: str,
    name: str,
    scopes: list[str] | None = None,
) -> str:
    result = ApiTokenService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    ).create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=UUID(workspace_id))],
        name=name,
        scopes=scopes or ["mcp:read", "mcp:write"],
    )
    return result.plain_token


def _user_id(page_service, *, email: str = "test@example.com"):
    with page_service.session_factory() as session:
        return session.scalar(select(User).where(User.email == email)).id


def _guide_call(
    client: TestClient,
    token: str,
    workspace_id: str,
):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "guide",
                "arguments": {"workspace_id": workspace_id},
            },
        },
    )


def _assert_workspace_denied(response) -> None:
    assert response.status_code == 400
    error_code = response.json()["error"]["data"]["error"]
    assert error_code in {"workspace_ambiguous", "workspace_access_denied"}


def test_pat_cannot_reach_a_different_workspace(page_service):
    client, workspace_a = _make_client(page_service)
    user_id = _user_id(page_service)
    workspace_b = _second_workspace(page_service, user_id=user_id)
    token_a = _issue_pat(
        page_service, user_id=user_id, workspace_id=workspace_a, name="a"
    )

    response = _guide_call(client, token_a, workspace_b)

    _assert_workspace_denied(response)


def test_revoked_pat_is_denied_outright(page_service):
    client, workspace_a = _make_client(page_service)
    user_id = _user_id(page_service)
    token_a = _issue_pat(
        page_service, user_id=user_id, workspace_id=workspace_a, name="a"
    )
    service = ApiTokenService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    )
    revoke_id = next(t.id for t in service.list_tokens(user_id) if t.name == "a")
    service.revoke_token(user_id=user_id, token_id=revoke_id)

    response = _guide_call(client, token_a, workspace_a)

    assert response.status_code == 401


def _write_call(
    client: TestClient,
    token: str,
    workspace_id: str,
):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_context_page",
                "arguments": {
                    "workspace_id": workspace_id,
                    "title": "Singular PAT scope regression",
                    "page_type": "overview",
                    "body_md": "# Singular PAT scope regression",
                    "conversation_token": "new",
                },
            },
        },
    )


def _feedback_call(
    client: TestClient,
    token: str,
    workspace_id: str,
):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "submit_feedback",
                "arguments": {
                    "workspace_id": workspace_id,
                    "target_type": "answer",
                    "target_id": str(uuid4()),
                    "body": "Workspace-specific scope regression.",
                    "severity": "minor",
                    "conversation_token": "new",
                },
            },
        },
    )


def test_pat_write_scope_does_not_reach_a_different_workspace(page_service):
    """D-01 regression: a write-scoped PAT bound to workspace A must never
    let a write reach workspace B just because the request names B -- the
    token_workspace_denied guard has to fire before scope is even checked."""

    client, workspace_a = _make_client(page_service)
    user_id = _user_id(page_service)
    workspace_b = _second_workspace(page_service, user_id=user_id)
    token_a = _issue_pat(
        page_service,
        user_id=user_id,
        workspace_id=workspace_a,
        name="a",
        scopes=["mcp:write"],
    )

    write_to_b = _write_call(client, token_a, workspace_b)
    write_to_a = _write_call(client, token_a, workspace_a)

    # A workspace claim mismatch is denied at the router seam (before a tool
    # envelope even exists), the same as any other cross-workspace call.
    _assert_workspace_denied(write_to_b)
    write_to_a_envelope = write_to_a.json()["result"]["structuredContent"]["envelope"]
    assert write_to_a_envelope["result_status"] == "succeeded"


def test_pat_scope_enforced_for_specialized_tool(page_service):
    """A viewer-level PAT must not reach a write-gated tool on its own
    workspace; a write-scoped PAT for that same workspace must."""

    client, _workspace_a = _make_client(page_service)
    user_id = _user_id(page_service)
    workspace_b = _second_workspace(page_service, user_id=user_id)
    token_b_read = _issue_pat(
        page_service,
        user_id=user_id,
        workspace_id=workspace_b,
        name="b-read",
        scopes=["mcp:read"],
    )
    token_b_write = _issue_pat(
        page_service,
        user_id=user_id,
        workspace_id=workspace_b,
        name="b-write",
        scopes=["mcp:write"],
    )

    denied = _feedback_call(client, token_b_read, workspace_b)
    allowed = _feedback_call(client, token_b_write, workspace_b)

    denied_envelope = denied.json()["result"]["structuredContent"]["envelope"]
    assert denied_envelope["error_code"] == "scope_denied"
    allowed_envelope = allowed.json()["result"]["structuredContent"]["envelope"]
    assert allowed_envelope["result_status"] == "succeeded"
