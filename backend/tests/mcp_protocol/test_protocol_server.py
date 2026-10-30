import json
from dataclasses import replace
from uuid import UUID, uuid4

import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import INVALID_PARAMS
from sqlalchemy import select

from backend.app.settings import Settings
from backend.mcp.protocol.server import create_mcp_server
from backend.mcp.scopes import visible_tool_names
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.oauth import OAuthClient, OAuthConsent
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.models.workspace_credential_grant import WorkspaceCredentialGrant
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership


def _default_workspace_id(page_service) -> str:
    """Return the workspace used by the authenticated HTTP MCP test actors."""
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert workspace is not None, "default workspace not seeded"
        return workspace.id


def _pat_actor(
    mcp_default_actor,
    *,
    scopes: tuple[str, ...],
    actor_id: str,
    user_id=None,
    role: str | None = None,
) -> ActorContext:
    actor = mcp_default_actor(
        scopes=scopes,
        actor_id=actor_id,
        user_id=user_id,
    )
    if role is None:
        return actor
    return replace(actor, workspace_role=role, role=role)


EXPECTED_TOOLS = {
    "build_agent_input_bundle",
    "cite",
    "fetch",
    "get_index",
    "grep_wiki",
    "what_changed",
    "guide",
    "list_agent_categories",
    "list_changes",
    "list_my_workspaces",
    "list_requested_changes",
    "read",
    "read_agent_page",
    "read_concept",
    "read_context_outline",
    "read_structure_map",
    "search",
    "submit_feedback",
    "submit_revision_for_review",
    "upload_image",
    "wiki_maintenance",
    "write_agent_page_draft",
    # P8.6
    "create_context_page",
    "begin_context_edit",
    "update_context_edit",
    "submit_context_edit_for_review",
    "discard_context_edit",
    "patch_context_edit",
    "list_my_context_edits",
}


def test_initialize_list_tools_and_call_create_context_page(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="protocol-test",
                scopes=("mcp:write", "mcp:agent"),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            tools_result = await client.list_tools()
            names = {tool.name for tool in tools_result.tools}
            assert names == EXPECTED_TOOLS

            call_result = await client.call_tool(
                "create_context_page",
                {
                    "workspace_id": str(workspace_id),
                    "title": "MCP Protocol Smoke",
                    "page_type": "overview",
                    "body_md": "# Hello from protocol smoke",
                    # Content tools must name their conversation; omitting the
                    # token used to start a new chat on every single call.
                    "conversation_token": "new",
                },
            )
            payload = json.loads(call_result.content[0].text)
            assert payload["envelope"]["result_status"] == "succeeded"
            return payload["data"]["id"]

    page_id = anyio.run(run)
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(page_id))
        assert page is not None
        assert page.title == "MCP Protocol Smoke"


def test_list_tools_filters_by_actor_scopes(page_service, mcp_default_actor):
    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="read-only-protocol-test",
                scopes=("mcp:read",),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            tools_result = await client.list_tools()
            names = {tool.name for tool in tools_result.tools}
            assert names == {
                "list_my_workspaces",
                "guide",
                "search",
                "fetch",
                "grep_wiki",
                "what_changed",
                "read",
                "get_index",
                "read_concept",
                "read_context_outline",
                "read_structure_map",
                "list_agent_categories",
                "read_agent_page",
            }

            call_result = await client.call_tool(
                "create_context_page",
                {
                    "workspace_id": str(_default_workspace_id(page_service)),
                    "title": "Denied by Scope",
                    "page_type": "overview",
                    "body_md": "# Denied by Scope",
                    "conversation_token": "new",
                },
            )
            payload = json.loads(call_result.content[0].text)
            assert call_result.isError is True
            assert payload["envelope"]["result_status"] == "rejected_by_policy"
            assert payload["envelope"]["error_code"] == "scope_denied"

    anyio.run(run)


def test_pat_list_my_workspaces_is_credential_scoped(page_service, mcp_default_actor):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        session.add(
            Workspace(
                slug="other-visible-workspace",
                title="Other Visible Workspace",
                name="Other Visible Workspace",
            )
        )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="protocol-directory-test",
                scopes=("mcp:write", "mcp:agent"),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("list_my_workspaces", {})
            payload = json.loads(result.content[0].text)
            assert result.isError is False
            assert [workspace["id"] for workspace in payload["data"]["workspaces"]] == [
                str(workspace_id)
            ]

    anyio.run(run)


def test_prompt_and_resource_surfaces_require_mcp_read_scope(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="no-scope-client",
                scopes=("mcp:agent",),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            assert prompts.prompts == []
            assert resources.resources == []

            with pytest.raises(McpError) as prompt_error:
                await client.get_prompt(
                    "dtxwiki.context_workflow",
                    {"workspace_id": str(workspace_id)},
                )
            assert prompt_error.value.error.data["error_code"] == "scope_denied"

            with pytest.raises(McpError) as resource_error:
                await client.read_resource(f"dtxwiki://workspaces/{workspace_id}/guide")
            assert resource_error.value.error.data["error_code"] == "scope_denied"

    anyio.run(run)


def test_unknown_conversation_token_self_corrects_on_next_echo(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)
    actor = _pat_actor(
        mcp_default_actor,
        actor_id="conversation-token-test-client",
        scopes=("mcp:write", "mcp:agent"),
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            first = await client.call_tool(
                "guide",
                {
                    "conversation_token": "conv_missing",
                    "workspace_id": str(workspace_id),
                },
            )
            first_payload = json.loads(first.content[0].text)
            next_token = first_payload["envelope"]["conversation_token"]

            assert first.isError is True
            assert (
                first_payload["envelope"]["error_code"] == "conversation_token_unknown"
            )
            assert first_payload["envelope"]["result_status"] == "failed"
            assert "for a new request" in first_payload["envelope"]["error_message"]
            assert 'pass "new"' in first_payload["envelope"]["error_message"]
            assert next_token != "conv_missing"

            second = await client.call_tool(
                "guide",
                {
                    "conversation_token": next_token,
                    "workspace_id": str(workspace_id),
                    "detail_level": "normal",
                },
            )
            second_payload = json.loads(second.content[0].text)

            assert second.isError is False
            assert second_payload["envelope"]["result_status"] == "succeeded"
            assert second_payload["envelope"]["conversation_token"] == next_token
            assert set(second_payload["data"]["available_tools"]) == set(
                visible_tool_names(actor)
            )

            malformed = await client.call_tool(
                "guide",
                {
                    "conversation_token": 123,
                    "workspace_id": str(workspace_id),
                },
            )
            malformed_payload = json.loads(malformed.content[0].text)
            assert (
                malformed_payload["envelope"]["error_message"]
                == first_payload["envelope"]["error_message"]
            )
            assert malformed.isError is True
            assert (
                malformed_payload["envelope"]["error_code"]
                == "conversation_token_malformed"
            )
            assert malformed_payload["envelope"]["conversation_token"].startswith(
                "conv_"
            )

    anyio.run(run)


def test_workspace_less_call_rejects_another_clients_conversation_token(
    page_service,
    mcp_default_actor,
):
    workspace_id = _default_workspace_id(page_service)
    first_actor = _pat_actor(
        mcp_default_actor,
        actor_id="workspace-less-token-first-client",
        scopes=("mcp:write", "mcp:agent"),
    )
    second_actor = _pat_actor(
        mcp_default_actor,
        actor_id="workspace-less-token-second-client",
        scopes=("mcp:write", "mcp:agent"),
    )

    async def run():
        first_server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=first_actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(first_server) as client:
            issued = await client.call_tool(
                "guide",
                {"workspace_id": str(workspace_id)},
            )
            issued_payload = json.loads(issued.content[0].text)
            token = issued_payload["envelope"]["conversation_token"]

        second_server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=second_actor,
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(second_server) as client:
            rejected = await client.call_tool(
                "list_my_workspaces",
                {"conversation_token": token},
            )
            rejected_payload = json.loads(rejected.content[0].text)

            assert rejected.isError is True
            assert (
                rejected_payload["envelope"]["error_code"]
                == "conversation_token_unknown"
            )
            assert rejected_payload["envelope"]["conversation_token"] != token

    anyio.run(run)


def test_prompts_and_resources_are_exposed_for_active_agent_pages(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        page = AgentPage(
            workspace_id=workspace_id,
            category="persona",
            title="Research Instructions",
            slug="research-instructions",
            lifecycle_status="active",
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=page.workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="Use cited sources.",
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="protocol-test",
                scopes=("mcp:write", "mcp:agent"),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            prompts = await client.list_prompts()
            assert {
                "dtxwiki.agent_page",
                "dtxwiki.agent_page.research-instructions",
                "dtxwiki.context_workflow",
            }.issubset({prompt.name for prompt in prompts.prompts})

            # Even a single bound workspace cannot supply a missing prompt
            # target; the actor otherwise has access (positive controls below).
            with pytest.raises(McpError) as missing_workspace:
                await client.get_prompt("dtxwiki.context_workflow", {})
            assert (
                missing_workspace.value.error.data["error_code"] == "validation_failed"
            )

            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            resource_names = {resource.name for resource in resources.resources}
            assert {
                "workspace-guide",
                "agent-injection-policy",
                "agent-page:research-instructions",
            }.issubset(resource_names)
            agent_resource = next(
                resource
                for resource in resources.resources
                if resource.name == "agent-page:research-instructions"
            )
            assert (
                str(agent_resource.uri)
                == f"dtxwiki://workspaces/{workspace_id}/agent/pages/research-instructions"
            )
            assert (
                f"dtxwiki://workspaces/{workspace_id}/agent/pages/by-category/persona"
                in {str(resource.uri) for resource in resources.resources}
            )
            assert [template.name for template in templates.resourceTemplates] == [
                "context-page",
                "agent-page",
            ]
            template_by_name = {
                template.name: template for template in templates.resourceTemplates
            }
            assert str(template_by_name["context-page"].uriTemplate) == (
                f"dtxwiki://workspaces/{workspace_id}/context/pages/{{slug}}"
            )
            assert str(template_by_name["agent-page"].uriTemplate) == (
                f"dtxwiki://workspaces/{workspace_id}/agent/pages/{{slug}}"
            )
            contents = await client.read_resource(agent_resource.uri)
            assert contents.contents[0].text == "Use cited sources."
            category_contents = await client.read_resource(
                f"dtxwiki://workspaces/{workspace_id}/agent/pages/by-category/persona"
            )
            assert "Use cited sources." in category_contents.contents[0].text

    anyio.run(run)


def test_context_resources_honor_published_and_draft_visibility(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        viewer_user = User(
            email="resource-viewer@example.com",
            password_hash="hash",
            display_name="Resource Viewer",
        )
        editor_user = User(
            email="resource-editor@example.com",
            password_hash="hash",
            display_name="Resource Editor",
        )
        session.add_all([viewer_user, editor_user])
        session.flush()
        grant_workspace_membership(
            session,
            user_id=viewer_user.id,
            workspace_id=workspace_id,
            role="viewer",
        )
        grant_workspace_membership(
            session,
            user_id=editor_user.id,
            workspace_id=workspace_id,
            role="editor",
        )
        published_page = ContextPage(
            workspace_id=workspace_id,
            slug="resource-dual-head",
            title="Resource Dual Head",
            page_type="overview",
            lifecycle_status="published",
        )
        draft_only_page = ContextPage(
            workspace_id=workspace_id,
            slug="resource-draft-only",
            title="Resource Draft Only",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add_all([published_page, draft_only_page])
        session.flush()
        published = ContextPageVersion(
            workspace_id=published_page.workspace_id,
            page_id=published_page.id,
            version_no=1,
            body_md="Published resource body.",
            content_hash="resource-dual-head-published",
            actor_type="human",
            version_status="published_snapshot",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        newer_draft = ContextPageVersion(
            workspace_id=published_page.workspace_id,
            page_id=published_page.id,
            version_no=2,
            body_md="Draft resource body.",
            content_hash="resource-dual-head-draft",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        draft_only = ContextPageVersion(
            workspace_id=draft_only_page.workspace_id,
            page_id=draft_only_page.id,
            version_no=1,
            body_md="Draft-only resource body.",
            content_hash="resource-draft-only",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        session.add_all([published, newer_draft, draft_only])
        session.flush()
        published_page.current_version_id = published.id
        published_page.current_published_version_id = published.id
        published_page.latest_draft_version_id = newer_draft.id
        draft_only_page.current_version_id = draft_only.id
        draft_only_page.latest_draft_version_id = draft_only.id
        viewer_user_id = viewer_user.id
        editor_user_id = editor_user.id

    dual_uri = f"dtxwiki://workspaces/{workspace_id}/context/pages/resource-dual-head"
    draft_only_uri = (
        f"dtxwiki://workspaces/{workspace_id}/context/pages/resource-draft-only"
    )

    async def read_as(role: str, user_id):
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id=f"{role}-resource-test",
                scopes=("mcp:write", "mcp:agent"),
                user_id=user_id,
                role=role,
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            resources = await client.list_resources()
            uris = {str(resource.uri) for resource in resources.resources}
            dual_contents = await client.read_resource(dual_uri)
            draft_contents = None
            draft_error = None
            try:
                draft_contents = await client.read_resource(draft_only_uri)
            except McpError as exc:
                draft_error = exc
            return uris, dual_contents.contents[0].text, draft_contents, draft_error

    async def run():
        viewer_uris, viewer_body, viewer_draft, viewer_error = await read_as(
            "viewer", viewer_user_id
        )
        editor_uris, editor_body, editor_draft, editor_error = await read_as(
            "editor", editor_user_id
        )

        # E-2 slimming: the LIST advertises entry points only — no per-page
        # resources for ANY role (a stronger negative pin than the old
        # per-page membership check). Direct URI reads keep the original
        # visibility contract verbatim below.
        assert dual_uri not in viewer_uris
        assert draft_only_uri not in viewer_uris
        assert dual_uri not in editor_uris
        assert draft_only_uri not in editor_uris
        assert any(uri.endswith("/bundle") for uri in viewer_uris)
        assert any(uri.endswith("/index") for uri in viewer_uris)

        assert viewer_body == "Published resource body."
        assert viewer_draft is None
        assert viewer_error is not None

        assert editor_body == "Draft resource body."
        assert editor_error is None
        assert editor_draft is not None
        assert editor_draft.contents[0].text == "Draft-only resource body."

    anyio.run(run)


def test_context_resource_read_uses_target_workspace_role(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        user = User(
            email="resource-rescope@example.com",
            password_hash="hash",
            display_name="Resource Rescope",
        )
        session.add(user)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace_id,
            role="viewer",
        )
        page = ContextPage(
            workspace_id=workspace_id,
            slug="target-resource",
            title="Target Resource",
            page_type="overview",
            lifecycle_status="published",
        )
        draft_only_page = ContextPage(
            workspace_id=workspace_id,
            slug="target-draft-only",
            title="Target Draft Only",
            page_type="overview",
            lifecycle_status="draft",
        )
        session.add_all([page, draft_only_page])
        session.flush()
        published = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="Target published body.",
            content_hash="target-resource-published",
            actor_type="human",
            version_status="published_snapshot",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        draft = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=2,
            body_md="Target draft body should not leak.",
            content_hash="target-resource-draft",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        draft_only = ContextPageVersion(
            workspace_id=draft_only_page.workspace_id,
            page_id=draft_only_page.id,
            version_no=1,
            body_md="Draft-only target body should not leak.",
            content_hash="target-draft-only",
            actor_type="human",
            version_status="draft",
            created_by_actor_kind="human",
            created_by_actor_id="user-1",
        )
        session.add_all([published, draft, draft_only])
        session.flush()
        page.current_version_id = published.id
        page.current_published_version_id = published.id
        page.latest_draft_version_id = draft.id
        draft_only_page.current_version_id = draft_only.id
        draft_only_page.latest_draft_version_id = draft_only.id
        user_id = user.id

    published_uri = f"dtxwiki://workspaces/{workspace_id}/context/pages/target-resource"
    draft_only_uri = (
        f"dtxwiki://workspaces/{workspace_id}/context/pages/target-draft-only"
    )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="resource-rescope-test",
                scopes=("mcp:write", "mcp:agent"),
                user_id=user_id,
                role="editor",
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            published_contents = await client.read_resource(published_uri)
            assert published_contents.contents[0].text == "Target published body."
            with pytest.raises(McpError) as draft_error:
                await client.read_resource(draft_only_uri)
            assert draft_error.value.error.message == "resource not found"

    anyio.run(run)


def test_sensitive_prompts_and_resources_are_gated(page_service, mcp_default_actor):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        page = AgentPage(
            workspace_id=workspace_id,
            category="agent_guide",
            title="Sensitive System Prompt",
            slug="sensitive-system-prompt",
            lifecycle_status="active",
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=page.workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="Do not expose this to non-owners.",
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        viewer = User(
            email="sensitive-resource-viewer@example.com",
            password_hash="hash",
            display_name="Sensitive Resource Viewer",
        )
        session.add(viewer)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=viewer.id,
            workspace_id=workspace_id,
            role="viewer",
        )
        viewer_id = viewer.id
        resource_uri = (
            f"dtxwiki://workspaces/{workspace_id}/agent/pages/sensitive-system-prompt"
        )
        category_resource_uri = (
            f"dtxwiki://workspaces/{workspace_id}/agent/pages/by-category/agent_guide"
        )

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="protocol-test",
                scopes=("mcp:read",),
                user_id=viewer_id,
                role="viewer",
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            prompts = await client.list_prompts()
            assert "dtxwiki.agent_page.sensitive-system-prompt" not in {
                prompt.name for prompt in prompts.prompts
            }

            resources = await client.list_resources()
            assert resource_uri not in {
                str(resource.uri) for resource in resources.resources
            }
            assert category_resource_uri not in {
                str(resource.uri) for resource in resources.resources
            }

            with pytest.raises(McpError) as prompt_error:
                await client.get_prompt(
                    "dtxwiki.agent_page",
                    {
                        "workspace_id": str(workspace_id),
                        "agent_page_id": str(page.id),
                    },
                )
            assert prompt_error.value.error.code == INVALID_PARAMS
            assert (
                prompt_error.value.error.data["error_code"]
                == "workspace_role_insufficient:sensitive_resource"
            )

            with pytest.raises(McpError) as resource_error:
                await client.read_resource(resource_uri)
            assert resource_error.value.error.code == INVALID_PARAMS
            assert (
                resource_error.value.error.data["error_code"]
                == "workspace_role_insufficient:sensitive_resource"
            )

            with pytest.raises(McpError) as category_resource_error:
                await client.read_resource(category_resource_uri)
            assert category_resource_error.value.error.message == "resource not found"

    anyio.run(run)


def test_read_only_owner_oauth_cannot_read_sensitive_prompt_or_resource(page_service):
    workspace_id = _default_workspace_id(page_service)
    oauth_client_id = "sensitive-prompt-protocol-test"
    grant_id = uuid4()
    with page_service.session_factory.begin() as session:
        owner = User(
            email="sensitive-owner-pat@example.com",
            password_hash="hash",
            display_name="Sensitive Owner PAT",
        )
        session.add(owner)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=owner.id,
            workspace_id=workspace_id,
            role="owner",
        )
        page = AgentPage(
            workspace_id=workspace_id,
            category="agent_guide",
            title="PAT Sensitive Guide",
            slug="pat-sensitive-guide",
            lifecycle_status="active",
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=page.workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="Owner write scope only.",
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        session.add(
            OAuthClient(
                id=oauth_client_id,
                client_name="Sensitive prompt protocol test",
                redirect_uris=["http://127.0.0.1/callback"],
                grant_types=["authorization_code"],
                response_types=["code"],
                scope="mcp:read mcp:write",
            )
        )
        session.add(
            OAuthConsent(
                user_id=owner.id,
                client_id=oauth_client_id,
                workspace_id=workspace_id,
                scope="mcp:read",
                desired_scopes=["mcp:read"],
            )
        )
        session.add(
            WorkspaceCredentialGrant(
                workspace_id=workspace_id,
                credential_kind="oauth",
                credential_id=grant_id,
                user_id=owner.id,
                granted_scopes=["mcp:read", "mcp:write"],
                synced_revision=1,
            )
        )
        owner_id = owner.id
        page_id = page.id

    resource_uri = (
        f"dtxwiki://workspaces/{workspace_id}/agent/pages/pat-sensitive-guide"
    )

    async def exercise(scopes: tuple[str, ...]):
        with page_service.session_factory.begin() as session:
            consent = session.get(
                OAuthConsent,
                (owner_id, oauth_client_id, workspace_id),
            )
            assert consent is not None
            consent.scope = " ".join(scopes)
            consent.desired_scopes = list(scopes)
        # D-01: no cache to clear -- oauth_consented_workspace_scope reads
        # the control DB fresh on every call.
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=ActorContext(
                actor_kind="human",
                actor_id=str(owner_id),
                scopes=scopes,
                user_id=owner_id,
                workspace_id=workspace_id,
                workspace_role="owner",
                role="owner",
                token_kind="oauth_jwt",
                token_id=grant_id,
                client_id=oauth_client_id,
                # D-01: the token's own claim mirrors its one bound workspace.
                token_workspace_ids=(workspace_id,),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            listed_prompts = {prompt.name for prompt in prompts.prompts}
            listed_resources = {str(resource.uri) for resource in resources.resources}
            if scopes == ("mcp:read",):
                assert "dtxwiki.agent_page.pat-sensitive-guide" not in listed_prompts
                assert resource_uri not in listed_resources
                with pytest.raises(McpError) as prompt_error:
                    await client.get_prompt(
                        "dtxwiki.agent_page",
                        {
                            "workspace_id": str(workspace_id),
                            "agent_page_id": str(page_id),
                        },
                    )
                with pytest.raises(McpError) as resource_error:
                    await client.read_resource(resource_uri)
                assert (
                    prompt_error.value.error.data["error_code"]
                    == "workspace_role_insufficient:sensitive_resource"
                )
                assert (
                    resource_error.value.error.data["error_code"]
                    == "workspace_role_insufficient:sensitive_resource"
                )
            else:
                prompt = await client.get_prompt(
                    "dtxwiki.agent_page",
                    {
                        "workspace_id": str(workspace_id),
                        "agent_page_id": str(page_id),
                    },
                )
                resource = await client.read_resource(resource_uri)
                assert prompt.messages[0].content.text == "Owner write scope only."
                assert resource.contents[0].text == "Owner write scope only."

    async def run():
        await exercise(("mcp:read",))
        await exercise(("mcp:write",))

    anyio.run(run)


def test_user_profile_and_memory_are_not_prompt_targets(
    page_service, mcp_default_actor
):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        page = AgentPage(
            workspace_id=workspace_id,
            category="memory",
            title="Private Memory",
            slug="private-memory",
            lifecycle_status="active",
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=page.workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="Do not expose as prompt.",
            actor_type="human",
            version_status="active_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="protocol-test",
                scopes=("mcp:write", "mcp:agent"),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            prompts = await client.list_prompts()
            assert "dtxwiki.agent_page.private-memory" not in {
                prompt.name for prompt in prompts.prompts
            }
            with pytest.raises(McpError) as prompt_error:
                await client.get_prompt(
                    "dtxwiki.agent_page",
                    {"workspace_id": str(workspace_id), "slug": page.slug},
                )
            assert prompt_error.value.error.code == INVALID_PARAMS
            assert (
                prompt_error.value.error.data["error_code"]
                == "agent_page_prompt_not_supported"
            )

    anyio.run(run)


def test_agent_page_prompts_require_active_pages(page_service, mcp_default_actor):
    workspace_id = _default_workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        page = AgentPage(
            workspace_id=workspace_id,
            category="instruction",
            title="Draft Instructions",
            slug="draft-instructions",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()

    async def run():
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="protocol-test",
                scopes=("mcp:write", "mcp:agent"),
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            with pytest.raises(McpError) as prompt_error:
                await client.get_prompt(
                    "dtxwiki.agent_page",
                    {"workspace_id": str(workspace_id), "slug": page.slug},
                )
            assert prompt_error.value.error.code == INVALID_PARAMS
            assert prompt_error.value.error.data["error_code"] == "agent_page_not_found"

    anyio.run(run)


def test_bundle_and_index_reads_require_mcp_read_scope(page_service, mcp_default_actor):
    """An agent-only PAT cannot read workspace bodies or the title index."""
    workspace_id = _default_workspace_id(page_service)
    page_service.write_page(
        workspace_slug="default",
        slug="scope-gate-page",
        title="Scope Gate Page",
        body_md="# SG\n\nGated body.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    bundle_uri = f"dtxwiki://workspaces/{workspace_id}/bundle"
    index_uri = f"dtxwiki://workspaces/{workspace_id}/index"

    async def read_with_scopes(scopes, uri):
        server = create_mcp_server(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            actor=_pat_actor(
                mcp_default_actor,
                actor_id="scope-gate-test",
                scopes=scopes,
            ),
            page_service=page_service,
        )
        async with create_connected_server_and_client_session(server) as client:
            try:
                contents = await client.read_resource(uri)
                return contents.contents[0].text, None
            except McpError as exc:
                return None, exc

    async def run():
        for uri in (bundle_uri, index_uri):
            denied_body, denied_error = await read_with_scopes(("mcp:agent",), uri)
            assert denied_body is None
            assert denied_error is not None
            assert denied_error.error.code == INVALID_PARAMS
        bundle_body, bundle_error = await read_with_scopes(("mcp:read",), bundle_uri)
        assert bundle_error is None
        assert "Gated body." in bundle_body

    anyio.run(run)
