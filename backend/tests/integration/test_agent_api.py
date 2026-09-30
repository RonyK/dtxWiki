from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.workspace import MCPClient, Workspace
from backend.services.oauth_service import _DCR_BUCKET, challenge_s256
from backend.tests.conftest import authenticate_test_client_in_workspace


def make_client(
    page_service,
    email: str = "test@example.com",
    *,
    role: str = "owner",
) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
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
    return client


def _workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]


def test_agent_api_creates_and_reads_agent_page_draft(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "API Persona",
            "slug": "api-persona",
            "body_markdown": "# API Persona\n\nUse concise answers.",
            "reason": "create API persona draft",
        },
    )

    assert created.status_code == 201
    result = created.json()["data"]
    assert result["next_action"] == "owner_activation_required"
    assert result["review_item_id"] is None

    draft = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{result['agent_page_id']}?version=draft",
    )
    assert draft.status_code == 200
    assert (
        draft.json()["data"]["body_markdown"]
        == "# API Persona\n\nUse concise answers."
    )
    assert draft.json()["data"]["current_active_version_id"] is None

    reused = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "API Persona",
            "slug": "api-persona",
            "body_markdown": "# API Persona\n\nSecond draft, same slug.",
            "reason": "redraft API persona",
        },
    )
    assert reused.status_code == 200
    assert reused.json()["data"]["agent_page_id"] == result["agent_page_id"]


def test_agent_api_places_new_page_under_requested_tree_parent(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=agent")
    assert tree.status_code == 200
    persona_root = next(
        node for node in tree.json()["data"]["nodes"] if node["slug"] == "persona"
    )
    folder_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "parent_id": persona_root["id"],
            "section": "agent",
            "title": "Research Agents",
            "slug": "research-agents",
        },
    )
    assert folder_response.status_code == 201
    folder = folder_response.json()["data"]

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "Nested API Persona",
            "slug": "nested-api-persona",
            "body_markdown": "# Nested API Persona\n\nUse selected folder.",
            "parent_node_id": folder["id"],
            "reason": "create nested API persona draft",
        },
    )

    assert created.status_code == 201
    agent_page_id = created.json()["data"]["agent_page_id"]
    updated_tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=agent")
    assert updated_tree.status_code == 200
    page_node = next(
        node
        for node in updated_tree.json()["data"]["nodes"]
        if node["target_id"] == agent_page_id
    )
    assert page_node["parent_id"] == folder["id"]


def test_agent_api_updates_and_activates_agent_page(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages?category=agent_guide",
    ).json()["data"]
    agent_page_id = listed[0]["id"]
    active_version_id = listed[0]["current_active_version_id"]

    draft = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/draft",
        json={
            "category": "agent_guide",
            "title": "Default System Prompt",
            "slug": "default-system-prompt",
            "body_markdown": "# API Activated System Prompt\n\nOwner approved.",
            "base_version_id": active_version_id,
            "reason": "create activation draft",
        },
    )
    assert draft.status_code == 200
    draft_version_id = draft.json()["data"]["draft_version_id"]

    activated = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/activate",
        json={
            "version_id": draft_version_id,
            "reason": "activate system prompt draft",
        },
    )

    assert activated.status_code == 200
    data = activated.json()["data"]
    assert data["current_active_version_id"] == draft_version_id
    assert data["latest_draft_version_id"] is None
    assert data["body_markdown"] == "# API Activated System Prompt\n\nOwner approved."


def test_agent_api_activation_uses_membership_role(page_service):
    owner = make_client(page_service)
    workspace_id = _workspace_id(owner)

    persona = _create_agent_page_draft(
        client=owner,
        workspace_id=workspace_id,
        category="persona",
        slug="api-owner-persona",
        title="API Owner Persona",
        body="# API Owner Persona\n\nOwner can activate.",
    )

    owner_activation = owner.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{persona['agent_page_id']}/activate",
        json={
            "version_id": persona["draft_version_id"],
            "reason": "owner activates non-sensitive persona",
        },
    )
    assert owner_activation.status_code == 200

    editor = make_client(
        page_service,
        email="editor@example.com",
        role="editor",
    )
    blocked = _create_agent_page_draft(
        client=owner,
        workspace_id=workspace_id,
        category="persona",
        slug="api-editor-persona",
        title="API Editor Persona",
        body="# API Editor Persona\n\nEditor should be blocked.",
    )
    denied = editor.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{blocked['agent_page_id']}/activate",
        json={
            "version_id": blocked["draft_version_id"],
            "reason": "editor tries activation",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "scope_denied"


def test_agent_api_activation_ignores_workspace_role_header(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    persona = _create_agent_page_draft(
        client=client,
        workspace_id=workspace_id,
        category="persona",
        slug="api-header-persona",
        title="API Header Persona",
        body="# API Header Persona\n\nSession role is used.",
    )

    activation = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{persona['agent_page_id']}/activate",
        headers={"X-LLMWiki-Workspace-Role": "superuser"},
        json={
            "version_id": persona["draft_version_id"],
            "reason": "legacy header is ignored",
        },
    )

    assert activation.status_code == 200


def test_agent_api_activation_uses_target_workspace_role(page_service):
    """F4-12: activation must use the role in the path target workspace.

    The same user is owner in the default workspace but only viewer in the
    target workspace. If actor re-scoping regresses, the default owner role would
    incorrectly allow activation in the target workspace.
    """
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership
    from backend.schemas.agent import AgentPageDraftRequest
    from backend.scripts.seed_dev import seed_default_workspace
    from backend.services.agent_page_service import AgentPageService

    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        user = session.scalars(select(User)).first()
        target_workspace = seed_default_workspace(
            session,
            slug="agent-viewer-target",
            title="Agent Viewer Target",
        )
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=target_workspace.id,
            role="viewer",
        )
        draft = AgentPageService().write_draft(
            session=session,
            workspace_id=target_workspace.id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Target Persona",
                slug="target-persona",
                body_markdown="# Target Persona\n\nViewer must not activate.",
                reason="seed cross-workspace activation draft",
            ),
            actor_kind="human",
            actor_id=str(user.id),
            actor_provenance="browser_session",
        )
        target_workspace_id = target_workspace.id
        agent_page_id = draft.agent_page_id
        draft_version_id = draft.draft_version_id

    denied = client.post(
        f"/api/v1/workspaces/{target_workspace_id}/agent/pages/"
        f"{agent_page_id}/activate",
        json={
            "version_id": str(draft_version_id),
            "reason": "viewer tries target workspace activation",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["error_code"] in {
        "scope_denied",
        "rejected_by_policy",
        "workspace_role_insufficient",
    }


def test_agent_api_pat_bearer_cannot_activate_agent_page(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    persona = _create_agent_page_draft(
        client=client,
        workspace_id=workspace_id,
        category="persona",
        slug="api-pat-denied-persona",
        title="API PAT Denied Persona",
        body="# API PAT Denied Persona\n\nBearer token must not activate.",
    )
    pat = _issue_pat(client, workspace_id, ["mcp:agent"])
    client.cookies.clear()

    denied = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{persona['agent_page_id']}/activate",
        headers={"Authorization": f"Bearer {pat}"},
        json={
            "version_id": persona["draft_version_id"],
            "reason": "PAT bearer tries activation",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["error_code"] == "scope_denied"


def test_agent_api_oauth_bearer_cannot_activate_agent_page(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    persona = _create_agent_page_draft(
        client=client,
        workspace_id=workspace_id,
        category="persona",
        slug="api-oauth-denied-persona",
        title="API OAuth Denied Persona",
        body="# API OAuth Denied Persona\n\nOAuth token must not activate.",
    )
    oauth_access_token = _issue_oauth_access_token(
        client,
        workspace_id=workspace_id,
        scope="mcp:agent",
    )
    client.cookies.clear()

    denied = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{persona['agent_page_id']}/activate",
        headers={"Authorization": f"Bearer {oauth_access_token}"},
        json={
            "version_id": persona["draft_version_id"],
            "reason": "OAuth bearer tries activation",
        },
    )

    assert denied.status_code == 403
    assert denied.json()["error_code"] == "scope_denied"


def test_agent_api_builds_and_reads_bundle_preview(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    agent_pages = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages?category=agent_guide",
    ).json()["data"]
    client_id = _internal_client_id(page_service, workspace_id)

    bundle = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/bundles/preview",
        json={
            "task_intent": "API bundle preview",
            "client_id": client_id,
            "token_budget": 2048,
            "permission_context": {"role": "internal_agent"},
        },
    )

    assert bundle.status_code == 200
    data = bundle.json()["data"]
    assert data["status"] == "generated"
    assert data["items"][0]["source_kind"] == "agent_page"
    assert data["items"][0]["pinned_id"] == agent_pages[0]["current_active_version_id"]

    read_back = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/bundles/{data['bundle_id']}",
    )
    assert read_back.status_code == 200
    assert read_back.json()["data"]["bundle_id"] == data["bundle_id"]


def test_agent_api_rejects_bundle_ttl_over_max(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    client_id = _internal_client_id(page_service, workspace_id)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/bundles/preview",
        json={
            "task_intent": "bad ttl",
            "client_id": client_id,
            "token_budget": 2048,
            "ttl_override_seconds": 999999,
            "permission_context": {"role": "internal_agent"},
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "rejected_by_policy"


def _internal_client_id(page_service, workspace_id: str) -> str:
    with page_service.session_factory() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        return str(client.id)


def _issue_pat(
    client: TestClient,
    workspace_id: str,
    scopes: list[str],
) -> str:
    response = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": "Agent activation bearer test",
            "scopes": scopes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["plain_token"]


def _issue_oauth_access_token(
    client: TestClient,
    *,
    workspace_id: str,
    scope: str,
) -> str:
    _DCR_BUCKET.clear()
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "Agent Activation OAuth Test",
            "redirect_uris": ["http://127.0.0.1:53682/callback"],
            "scope": scope,
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    state = "agent-activation-0123456789abcdef"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "code_challenge": challenge_s256(verifier),
        "code_challenge_method": "S256",
        "scope": scope,
        "state": state,
        # GET's workspace_id is only a preselect hint (P10.3 T3); POST needs
        # the checklist's workspace_ids field to actually grant access.
        "workspace_id": workspace_id,
    }
    consent = client.get("/oauth/authorize", params=params)
    assert consent.status_code == 200, consent.text
    post_params = {key: value for key, value in params.items() if key != "workspace_id"}
    authorized = client.post(
        "/oauth/authorize",
        data={**post_params, "workspace_ids": workspace_id, "decision": "allow"},
        follow_redirects=False,
    )
    assert authorized.status_code == 303, authorized.text
    code = parse_qs(urlparse(authorized.headers["location"]).query)["code"][0]
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
    return token.json()["access_token"]


def _create_agent_page_draft(
    *,
    client: TestClient,
    workspace_id: str,
    category: str,
    slug: str,
    title: str,
    body: str,
) -> dict:
    client.app.dependency_overrides.clear()
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": category,
            "title": title,
            "slug": slug,
            "body_markdown": body,
            "reason": f"create {slug} draft",
        },
    )
    assert created.status_code == 201
    return created.json()["data"]


def _create_system_prompt_draft(client: TestClient, workspace_id: str) -> dict:
    client.app.dependency_overrides.clear()
    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages?category=agent_guide",
    ).json()["data"]
    agent_page = listed[0]
    draft = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/"
        f"{agent_page['id']}/draft",
        json={
            "category": "agent_guide",
            "title": "Default System Prompt",
            "slug": "default-system-prompt",
            "body_markdown": "# Sensitive API Draft\n\nOwner only.",
            "base_version_id": agent_page["current_active_version_id"],
            "reason": "create sensitive draft",
        },
    )
    assert draft.status_code == 200
    return draft.json()["data"]
