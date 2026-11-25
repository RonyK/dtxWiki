from uuid import uuid4

import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import func, select

from backend.app.settings import Settings
from backend.mcp.protocol.server import create_mcp_server
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.audit import AuditEvent
from backend.models.base import utc_now
from backend.models.bundle import AgentInputBundle
from backend.models.mcp_chat import McpChatSession
from backend.models.mcp_trace import MCPToolCall
from backend.models.oauth import OAuthClient, OAuthConsent
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership


@pytest.mark.parametrize(
    ("desired_scopes", "replica_scopes"),
    [
        (["mcp:agent"], ["mcp:read", "mcp:agent"]),
        (["mcp:read", "mcp:agent"], ["mcp:agent"]),
    ],
)
def test_pat_discovery_uses_control_and_replica_scope_intersection(
    page_service,
    desired_scopes,
    replica_scopes,
):
    """Neither control intent nor a stale-wide replica may widen discovery."""

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        user = User(
            email=f"discovery-scope-{uuid4()}@example.com",
            password_hash="test-hash",
            display_name="Discovery scope reviewer",
            default_workspace_id=workspace.id,
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        token = ApiToken(
            user_id=user.id,
            name=f"discovery-scope-{uuid4()}",
            token_lookup_hash=f"discovery-scope-{uuid4()}",
            hashed_token="unused",
            scopes=["mcp:read", "mcp:agent"],
        )
        session.add(token)
        session.flush()
        session.add_all(
            [
                ApiTokenWorkspace(
                    token_id=token.id,
                    workspace_id=workspace.id,
                    desired_scopes=desired_scopes,
                    revision=1,
                    sync_state="synced",
                ),
                WorkspaceCredentialGrant(
                    workspace_id=workspace.id,
                    credential_kind="pat",
                    credential_id=token.id,
                    user_id=user.id,
                    granted_scopes=replica_scopes,
                    synced_revision=1,
                ),
            ]
        )
        workspace_id = workspace.id
        user_id = user.id
        token_id = token.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read", "mcp:agent"),
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="api_token",
        token_id=token_id,
        token_workspace_ids=(workspace_id,),
        provenance="personal_access_token",
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            tool_names = {tool.name for tool in (await client.list_tools()).tools}
            assert "write_agent_page_draft" in tool_names
            assert "read" not in tool_names
            assert (await client.list_prompts()).prompts == []
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resourceTemplates == []
            with pytest.raises(McpError) as prompt_error:
                await client.get_prompt(
                    "dtxwiki.context_workflow",
                    {"workspace_id": str(workspace_id)},
                )
            with pytest.raises(McpError) as resource_error:
                await client.read_resource(
                    f"dtxwiki://workspaces/{workspace_id}/guide"
                )
            assert prompt_error.value.error.data["error_code"] == "scope_denied"
            assert resource_error.value.error.data["error_code"] == "scope_denied"

    anyio.run(run)


@pytest.mark.parametrize("replica_state", ["missing", "revoked"])
def test_oauth_protocol_surfaces_require_replica_before_lifecycle_first_touch(
    page_service,
    replica_state,
):
    """Discovery and content reads fail closed without creating client state."""

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        user = User(
            email=f"protocol-replica-{replica_state}@example.com",
            password_hash="test-hash",
            display_name="Protocol replica reviewer",
            default_workspace_id=workspace.id,
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="viewer",
        )
        client_id = f"protocol-replica-{replica_state}"
        grant_id = uuid4()
        session.add_all(
            [
                OAuthClient(
                    id=client_id,
                    client_name="Protocol replica review",
                    redirect_uris=["http://127.0.0.1/callback"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    scope="mcp:read",
                ),
                OAuthConsent(
                    user_id=user.id,
                    client_id=client_id,
                    workspace_id=workspace.id,
                    scope="mcp:read",
                    desired_scopes=["mcp:read"],
                ),
            ]
        )
        if replica_state == "revoked":
            session.add(
                WorkspaceCredentialGrant(
                    workspace_id=workspace.id,
                    credential_kind="oauth",
                    credential_id=grant_id,
                    user_id=user.id,
                    granted_scopes=["mcp:read"],
                    synced_revision=1,
                    revoked_at=utc_now(),
                )
            )
        session.flush()
        workspace_id = workspace.id
        user_id = user.id
        client_count = session.scalar(select(func.count()).select_from(MCPClient))

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read",),
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id=client_id,
        token_workspace_ids=(workspace_id,),
        provenance="oauth_client",
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            assert (await client.list_prompts()).prompts == []
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resourceTemplates == []
            with pytest.raises(McpError) as prompt_error:
                await client.get_prompt(
                    "dtxwiki.context_workflow",
                    {"workspace_id": str(workspace_id)},
                )
            with pytest.raises(McpError) as resource_error:
                await client.read_resource(
                    f"dtxwiki://workspaces/{workspace_id}/guide"
                )
            assert prompt_error.value.error.data["error_code"] == (
                "grant_replica_missing"
            )
            assert resource_error.value.error.data["error_code"] == (
                "grant_replica_missing"
            )

    anyio.run(run)

    with page_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MCPClient)) == (
            client_count
        )
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0


def test_oauth_discovery_lists_cannot_bypass_disabled_nondefault_binding(
    page_service,
):
    """OAuth uses a human default Workspace plus a token-bound Workspace.

    Generic prompt descriptors used to return before lifecycle evaluation when
    those differed, while resource lists returned an apparently successful
    empty result. Both were valid MCP requests from a disabled credential.
    """

    with page_service.session_factory.begin() as session:
        default_workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        user = User(
            email="oauth-lifecycle@example.com",
            password_hash="test-hash",
            display_name="OAuth lifecycle reviewer",
            default_workspace_id=default_workspace.id,
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=default_workspace.id,
            role="owner",
        )
        token_workspace = Workspace(
            slug="oauth-lifecycle-target",
            title="OAuth lifecycle target",
            name="OAuth lifecycle target",
        )
        session.add(token_workspace)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=token_workspace.id,
            role="viewer",
        )
        grant_id = token_workspace.id
        client = MCPClient(
            workspace_id=token_workspace.id,
            name="disabled-oauth-discovery",
            client_type="oauth",
            scopes=["mcp:read"],
            status="disabled",
            issued_to_user_id=user.id,
            runtime_binding_key=f"oauth_grant:{grant_id}",
        )
        oauth_client_id = "oauth-runtime-review"
        session.add_all(
            [
                OAuthClient(
                    id=oauth_client_id,
                    client_name="OAuth runtime review",
                    redirect_uris=["http://127.0.0.1/callback"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    scope="mcp:read",
                ),
                OAuthConsent(
                    user_id=user.id,
                    client_id=oauth_client_id,
                    workspace_id=token_workspace.id,
                    scope="mcp:read",
                    desired_scopes=["mcp:read"],
                ),
                WorkspaceCredentialGrant(
                    workspace_id=token_workspace.id,
                    credential_kind="oauth",
                    credential_id=grant_id,
                    user_id=user.id,
                    granted_scopes=["mcp:read"],
                    synced_revision=1,
                ),
                client,
            ]
        )
        session.flush()
        user_id = user.id
        token_workspace_id = token_workspace.id
        client_id = client.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read",),
        user_id=user_id,
        # D-01: workspace_id mirrors the token's own singular claim -- this
        # is the shape HTTP OAuth authentication now always produces
        # (backend/policy/auth.py _authenticate_oauth_token). The prior
        # "workspace_id = user's unrelated default membership" shape this
        # test used to construct is exactly the auth.py bug D-01 fixes, and
        # is no longer reachable: it was accidentally short-circuiting this
        # test on a token_workspace_denied mismatch BEFORE the call ever
        # reached evaluate_mcp_client_runtime's disabled-binding check below,
        # which is the actual mechanism this test exists to guard.
        workspace_id=token_workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id=oauth_client_id,
        token_workspace_ids=(token_workspace_id,),
        provenance="oauth_client",
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            calls = (
                client.list_prompts(),
                client.list_resources(),
                client.list_resource_templates(),
            )
            for call in calls:
                with pytest.raises(McpError) as error:
                    await call
                assert error.value.error.data["error_code"] == (
                    "mcp_client_disabled"
                )

    anyio.run(run)

    with page_service.session_factory() as session:
        denials = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "mcp.client.runtime.denied",
                AuditEvent.target_id == client_id,
            )
        ).all()
        assert len(denials) == 3
        assert session.scalar(select(func.count()).select_from(MCPToolCall)) == 0
        assert session.scalar(select(func.count()).select_from(McpChatSession)) == 0
        assert session.scalar(select(func.count()).select_from(AgentInputBundle)) == 0


def test_actor_discovery_skips_claim_without_live_membership(page_service):
    """An unexpired multi-workspace claim must not recreate rows after access ends."""

    with page_service.session_factory.begin() as session:
        default_workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        user = User(
            email="removed-lifecycle@example.com",
            password_hash="test-hash",
            display_name="Removed lifecycle reviewer",
            default_workspace_id=default_workspace.id,
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=default_workspace.id,
            role="owner",
        )
        removed_workspace = Workspace(
            slug="removed-oauth-lifecycle-target",
            title="Removed OAuth lifecycle target",
            name="Removed OAuth lifecycle target",
        )
        session.add(removed_workspace)
        session.flush()
        user_id = user.id
        default_workspace_id = default_workspace.id
        removed_workspace_id = removed_workspace.id
        before = session.scalar(select(func.count()).select_from(MCPClient))

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:read",),
        user_id=user_id,
        workspace_id=default_workspace_id,
        workspace_role="owner",
        role="owner",
        token_kind="oauth_jwt",
        token_id=removed_workspace_id,
        client_id="removed-membership-review",
        token_workspace_ids=(removed_workspace_id,),
        provenance="oauth_client",
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            # The historical actor-scoped tools/list behavior remains; the key
            # assertion is that lifecycle registration does not write into a
            # Workspace the actor can no longer enter.
            await client.list_tools()

    anyio.run(run)

    with page_service.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MCPClient)) == before


def test_oauth_tool_call_respects_its_own_workspaces_disabled_binding(page_service):
    """D-01 removes the identity-only design's live-consent multi-workspace
    directory this test used to exercise (formerly
    ``test_multi_workspace_oauth_checks_every_live_candidate_once``: a
    SINGLE oauth_jwt token could enumerate and reach every workspace the
    (user, client) pair had ever separately consented to, and this test
    checked that one disabled peer workspace was isolated from an active
    one). A token now names exactly ONE workspace, so "isolate one disabled
    peer while another stays active" has no equivalent any more -- there is
    no peer. What remains true, and is what this test guards instead, is
    that disabling the MCP client binding for the token's OWN (only)
    workspace still blocks its tool calls with ``mcp_client_disabled``,
    exactly as it did before.
    """

    from uuid import uuid4

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        user = User(
            email="single-lifecycle@example.com",
            password_hash="test-hash",
            display_name="Single lifecycle reviewer",
            default_workspace_id=workspace.id,
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=workspace.id, role="viewer"
        )
        grant_id = uuid4()
        oauth_client_id = "single-workspace-oauth"
        session.add(
            OAuthClient(
                id=oauth_client_id,
                client_name="Single-workspace OAuth",
                redirect_uris=["http://127.0.0.1/callback"],
                grant_types=["authorization_code"],
                response_types=["code"],
                scope="mcp:read mcp:write",
            )
        )
        session.add(
            OAuthConsent(
                user_id=user.id,
                client_id=oauth_client_id,
                workspace_id=workspace.id,
                scope="mcp:write",
                desired_scopes=["mcp:write"],
            )
        )
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace.id,
                credential_kind="oauth",
                credential_id=grant_id,
                user_id=user.id,
                granted_scopes=["mcp:write"],
                synced_revision=1,
            )
        )
        user_id = user.id
        workspace_id = workspace.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("mcp:write",),
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
        token_kind="oauth_jwt",
        token_id=grant_id,
        client_id=oauth_client_id,
        token_workspace_ids=(workspace_id,),
        provenance="oauth_client",
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            first_list = await client.list_tools()
            assert "submit_feedback" in {tool.name for tool in first_list.tools}

            called = await client.call_tool("list_my_workspaces", {})
            assert called.isError is False
            listed_ids = {
                item["id"] for item in called.structuredContent["data"]["workspaces"]
            }
            assert listed_ids == {str(workspace_id)}

            with page_service.session_factory.begin() as session:
                persisted = session.scalar(
                    select(MCPClient).where(
                        MCPClient.runtime_binding_key == f"oauth_grant:{grant_id}"
                    )
                )
                assert persisted is not None
                assert persisted.scopes == ["mcp:write"]
                persisted.status = "disabled"

            # Unlike the 2-workspace design this test replaces (where an
            # active peer workspace kept list_tools() alive), a token that
            # names only one workspace has no other candidate to fall back
            # on once that workspace's binding is disabled: listing itself
            # is denied too, not just dispatch.
            with pytest.raises(McpError) as list_error:
                await client.list_tools()
            assert "disabled" in str(list_error.value).lower()

            denied = await client.call_tool(
                "guide",
                {"workspace_id": str(workspace_id)},
            )
            assert denied.isError is True
            assert denied.structuredContent["envelope"]["error_code"] == (
                "mcp_client_disabled"
            )

    anyio.run(run)

    with page_service.session_factory() as session:
        disabled_client = session.scalar(
            select(MCPClient).where(
                MCPClient.workspace_id == workspace_id,
                MCPClient.runtime_binding_key == f"oauth_grant:{grant_id}",
            )
        )
        assert disabled_client is not None
        denials = session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "mcp.client.runtime.denied",
                AuditEvent.target_id == disabled_client.id,
            )
        ).all()
        # The post-disable list_tools() and the denied guide call.
        assert len(denials) == 2
