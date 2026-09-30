from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings


def make_client(page_service) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )


def personal_workspace_slug(client: TestClient) -> str:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200
    workspaces = response.json()["data"]["workspaces"]
    assert len(workspaces) == 1
    assert workspaces[0]["title"] == "Personal Workspace"
    return str(workspaces[0]["slug"])


def personal_workspace_id(client: TestClient) -> str:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200
    workspaces = response.json()["data"]["workspaces"]
    assert len(workspaces) == 1
    return str(workspaces[0]["id"])


def test_rest_auth_accepts_session_cookie(page_service):
    client = make_client(page_service)
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "session@example.com", "password": "correct-password"},
    )
    assert signup.status_code == 201

    response = client.get(
        f"/api/v1/workspaces/{personal_workspace_id(client)}/context/pages",
    )

    assert response.status_code == 200


def test_rest_auth_rejects_legacy_dev_token_without_session(page_service):
    client = make_client(page_service)

    response = client.get(
        f"/api/v1/workspaces/{uuid4()}/context/pages",
        headers={"Authorization": "Bearer dev-local"},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"


def test_rest_auth_accepts_personal_api_token_without_session_cookie(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "pat@example.com", "password": "correct-password"},
    )
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": personal_workspace_id(client)}],
            "name": "test",
            "scopes": ["mcp:read"],
        },
    )
    assert issued.status_code == 201, issued.text
    token = issued.json()["data"]["plain_token"]
    workspace_id = personal_workspace_id(client)
    client.cookies.clear()

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_personal_api_token_cannot_cross_its_issued_workspace(page_service):
    client = make_client(page_service)
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": "pat-bound@example.com", "password": "correct-password"},
    )
    assert signup.status_code == 201
    bound_workspace_id = personal_workspace_id(client)
    other = client.post(
        "/api/v1/workspaces",
        json={"title": "Other Workspace", "slug": "pat-other"},
    )
    assert other.status_code == 201, other.text
    other_workspace_id = other.json()["data"]["id"]
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": bound_workspace_id}],
            "name": "bound",
            "scopes": ["mcp:read"],
        },
    )
    assert issued.status_code == 201, issued.text
    token = issued.json()["data"]["plain_token"]
    client.cookies.clear()

    denied = client.get(
        f"/api/v1/workspaces/{other_workspace_id}/context/pages",
        headers={"Authorization": f"Bearer {token}"},
    )

    # P9.1: workspace_id now lives independently in the URL path, so
    # `scope_actor_to_request_workspace` (backend/policy/rest_scopes.py)
    # catches the token/workspace mismatch at the FastAPI dependency layer.
    # `workspace_existence_is_hidden` only masks that as a 404 for an actor
    # with NO relationship to the workspace at all -- this token's owner
    # genuinely is a member of "pat-other" (they created it via session
    # auth), so the mismatch is a transparent 403 now, not a 404. The
    # invariant this test still pins -- the bound PAT cannot reach it even
    # though its owner has a live membership there -- still holds either way.
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "token_workspace_denied"


def test_legacy_workspace_role_header_does_not_override_session_role(page_service):
    client = make_client(page_service)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "role@example.com", "password": "correct-password"},
    )
    workspace_id = personal_workspace_id(client)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        headers={"X-LLMWiki-Workspace-Role": "superuser"},
    )

    assert response.status_code == 200


def test_authentication_channel_sets_honest_actor_provenance():
    from uuid import uuid4

    from backend.policy.auth import _actor_from_user
    from backend.services.session_service import AuthenticatedUser

    user = AuthenticatedUser(
        user_id=uuid4(),
        email="actor@example.com",
        display_name="Actor",
        status="active",
    )
    expected = {
        "session": "browser_session",
        "oauth_jwt": "oauth_client",
        "api_token": "personal_access_token",
    }
    for token_kind, provenance in expected.items():
        actor = _actor_from_user(
            user=user, workspace_id=None, role=None, token_kind=token_kind
        )
        assert actor.actor_kind == "human"
        assert actor.provenance == provenance


def _pat_actor(*, workspace_id, scopes=("mcp:write",)):
    from uuid import uuid4

    from backend.policy.auth import ActorContext

    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        user_id=uuid4(),
        token_kind="api_token",
        scopes=scopes,
        workspace_id=workspace_id,
        token_workspace_ids=(workspace_id,),
    )


def test_sensitive_agent_page_gate_denies_a_workspace_the_token_is_not_bound_to():
    """D-01: the sensitive-body scope gate must key on the workspace being
    read -- a token bound to workspace A is denied outright for workspace
    B's sensitive body even though it holds admin-capable scope and an
    owner-role row for B, because its own claim never named B."""
    from uuid import uuid4

    from backend.api.read import _require_sensitive_agent_page_access
    from backend.policy.rbac import WorkspaceAccess
    from backend.services.read_errors import RejectedByPolicyError

    workspace_a, workspace_b = uuid4(), uuid4()
    actor = _pat_actor(workspace_id=workspace_a)

    with pytest.raises(RejectedByPolicyError) as excinfo:
        _require_sensitive_agent_page_access(
            actor,
            WorkspaceAccess(workspace_id=workspace_b, role="owner"),
            resource_workspace_id=workspace_b,
        )
    assert excinfo.value.error_code == "workspace_role_insufficient"


def test_sensitive_agent_page_gate_scopes_to_the_path_workspace():
    """A REST actor is never rebound to the path workspace the way MCP's
    ``resolve_target_actor`` rebinds an MCP one, so this gate takes the path
    workspace explicitly rather than trusting the actor's bound one: passing
    the actor's OWN workspace as the resource succeeds, a different
    ``resource_workspace_id`` is denied regardless of what ``access`` says.
    """
    from uuid import uuid4

    from backend.api.read import _require_sensitive_agent_page_access
    from backend.policy.rbac import WorkspaceAccess
    from backend.services.read_errors import RejectedByPolicyError

    workspace_a, workspace_b = uuid4(), uuid4()
    actor = _pat_actor(workspace_id=workspace_a)

    _require_sensitive_agent_page_access(
        actor,
        WorkspaceAccess(workspace_id=workspace_a, role="owner"),
        resource_workspace_id=workspace_a,
    )
    with pytest.raises(RejectedByPolicyError):
        _require_sensitive_agent_page_access(
            actor,
            WorkspaceAccess(workspace_id=workspace_b, role="owner"),
            resource_workspace_id=workspace_b,
        )


def _seed_user_and_two_workspaces(session_factory):
    """A real user with owner membership in two real workspaces.

    ``check_actor_can_access`` reads a WorkspaceMembership row and a
    workspace slug must resolve against a real Workspace row, so the tests
    below need persisted rows rather than bare uuid4s.
    """
    from uuid import uuid4

    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership

    with session_factory.begin() as session:
        user = User(
            email=f"{uuid4()}@example.com",
            password_hash="hash",
            display_name="Pin User",
            status="active",
        )
        workspace_a = Workspace(
            slug=f"pin-a-{uuid4().hex[:8]}", title="A", name="A", goal="a"
        )
        workspace_b = Workspace(
            slug=f"pin-b-{uuid4().hex[:8]}", title="B", name="B", goal="b"
        )
        session.add_all([user, workspace_a, workspace_b])
        session.flush()
        for workspace in (workspace_a, workspace_b):
            grant_workspace_membership(
                session,
                user_id=user.id,
                workspace_id=workspace.id,
                role="owner",
            )
        return (
            user.id,
            (workspace_a.id, workspace_a.slug),
            (workspace_b.id, workspace_b.slug),
        )


def test_workspace_pin_header_no_longer_has_any_effect(page_service):
    """DECISION ON THE RECORD (supersedes the prior 260901 5안 종합형 pin
    design): the PAT bundle and the ``X-Dtxwiki-Pin`` header are deleted
    outright, not preserved behind a flag. A PAT now authenticates as
    exactly the one workspace it was issued for; a pin header naming a
    DIFFERENT workspace this same user owns must have NO effect at all -- a
    request is scoped by the presented PAT alone, with no header able to
    widen or redirect it."""
    from backend.services.api_token_service import (
        ApiTokenService,
        WorkspaceGrantRequest,
    )
    from backend.services.credential_hashing import CredentialHasher

    user_id, (workspace_a, _slug_a), (workspace_b, slug_b) = (
        _seed_user_and_two_workspaces(page_service.session_factory)
    )
    token_a = ApiTokenService(
        session_factory=page_service.session_factory,
        credential_hasher=CredentialHasher(),
    ).create_token(
        user_id=user_id,
        workspace_grants=[WorkspaceGrantRequest(workspace_id=workspace_a)],
        name="a",
        scopes=["mcp:read"],
    ).plain_token

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
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token_a}",
            "X-Dtxwiki-Pin": slug_b,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "guide",
                "arguments": {"workspace_id": str(workspace_b)},
            },
        },
    )

    assert response.status_code == 400
    error_code = response.json()["error"]["data"]["error"]
    assert error_code in {"workspace_ambiguous", "workspace_access_denied"}
