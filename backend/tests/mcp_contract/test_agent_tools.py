from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.agent import AgentPage
from backend.models.bundle import AgentInputBundle
from backend.models.change import ChangeSet
from backend.models.mcp_chat import McpChatSession
from backend.models.workspace import MCPClient, Workspace


@pytest.fixture
def actor(mcp_default_actor):
    return mcp_default_actor(scopes=("mcp:write", "mcp:agent"))


def _workspace_and_client(page_service):
    with page_service.session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        return workspace.id, client.id


def test_mcp_write_agent_page_draft_never_activates(page_service, actor):
    workspace_id, _client_id = _workspace_and_client(page_service)

    response = dispatch(
        "write_agent_page_draft",
        {
            "workspace_id": str(workspace_id),
            "category": "persona",
            "title": "MCP Persona",
            "slug": "mcp-persona",
            "body_markdown": "# MCP Persona\n\nDraft only.",
            "reason": "create MCP persona draft",
        },
        actor=actor,
        page_service=page_service,
    )

    with page_service.session_factory() as session:
        page = session.get(AgentPage, response.data.agent_page_id)

    assert response.envelope.result_status == "succeeded"
    assert response.data.next_action == "review_required"
    assert response.data.review_item_id is not None
    assert page.latest_draft_version_id == response.data.draft_version_id
    assert page.current_active_version_id is None


def test_mcp_agent_page_draft_keeps_live_gating_metadata(page_service, actor):
    workspace_id, _client_id = _workspace_and_client(page_service)

    response = dispatch(
        "write_agent_page_draft",
        {
            "workspace_id": str(workspace_id),
            "category": "skill",
            "title": "Downgraded Prompt",
            "slug": "default-system-prompt",
            "body_markdown": "# Downgraded Prompt\n\nDraft only.",
            "sensitivity": "public",
            "reason": "attempt downgrade via MCP",
        },
        actor=actor,
        page_service=page_service,
    )

    with page_service.session_factory() as session:
        page = session.get(AgentPage, response.data.agent_page_id)

    assert response.envelope.result_status == "succeeded"
    assert response.data.next_action == "review_required"
    assert response.data.review_item_id is not None
    assert page.category == "agent_guide"
    assert page.title == "Default System Prompt"
    assert page.sensitivity == "workspace"


def test_mcp_agent_page_draft_first_write_stamps_origin_chat_session(
    page_service, actor
):
    # Codex #10 parity: the FIRST tool call of a brand-new conversation creates
    # the chat row up-front, so the agent-page draft's ChangeSet carries a
    # non-NULL origin_chat_session_id (chat_owner review routing).
    workspace_id, _client_id = _workspace_and_client(page_service)
    token = f"conv-{uuid4()}"

    response = dispatch(
        "write_agent_page_draft",
        {
            "workspace_id": str(workspace_id),
            "category": "persona",
            "title": "MCP Persona Origin",
            "slug": "mcp-persona-origin",
            "body_markdown": "# MCP Persona Origin\n\nDraft only.",
            "reason": "create MCP persona draft",
        },
        actor=actor,
        page_service=page_service,
        conversation_token=token,
    )

    assert response.envelope.result_status == "succeeded"
    with page_service.session_factory() as session:
        chat_id = session.scalar(
            select(McpChatSession.id).where(
                McpChatSession.workspace_id == workspace_id,
                McpChatSession.mcp_session_id == token,
            )
        )
        change_set = session.scalar(
            select(ChangeSet).where(
                ChangeSet.workspace_id == workspace_id,
                ChangeSet.trigger_type == "agent_page_draft",
            )
        )
    assert chat_id is not None
    assert change_set is not None
    assert change_set.origin_chat_session_id == chat_id


def test_mcp_agent_page_draft_without_token_leaves_origin_null(page_service, actor):
    workspace_id, _client_id = _workspace_and_client(page_service)

    response = dispatch(
        "write_agent_page_draft",
        {
            "workspace_id": str(workspace_id),
            "category": "persona",
            "title": "MCP Persona No Token",
            "slug": "mcp-persona-no-token",
            "body_markdown": "# MCP Persona No Token\n\nDraft only.",
            "reason": "create MCP persona draft",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    with page_service.session_factory() as session:
        change_set = session.scalar(
            select(ChangeSet).where(
                ChangeSet.workspace_id == workspace_id,
                ChangeSet.trigger_type == "agent_page_draft",
            )
        )
    assert change_set is not None
    assert change_set.origin_chat_session_id is None


def test_mcp_sensitive_agent_page_draft_returns_review_action(page_service, actor):
    workspace_id, _client_id = _workspace_and_client(page_service)

    response = dispatch(
        "write_agent_page_draft",
        {
            "workspace_id": str(workspace_id),
            "category": "memory",
            "title": "MCP Memory",
            "slug": "mcp-memory",
            "body_markdown": "# MCP Memory\n\nNeeds review.",
            "reason": "create MCP memory draft",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert response.data.next_action == "review_required"
    assert response.data.review_item_id is not None


def test_mcp_build_agent_input_bundle(page_service, actor):
    workspace_id, client_id = _workspace_and_client(page_service)

    response = dispatch(
        "build_agent_input_bundle",
        {
            "workspace_id": str(workspace_id),
            "task_intent": "MCP bundle",
            "client_id": str(client_id),
            "token_budget": 2048,
            "permission_context": {"role": "internal_agent"},
        },
        actor=actor,
        page_service=page_service,
    )

    with page_service.session_factory() as session:
        bundle = session.get(AgentInputBundle, response.data.bundle_id)

    assert response.envelope.result_status == "succeeded"
    assert response.data.status == "used"
    assert response.data.items[0].source_kind == "agent_page"
    assert response.data.items[0].included_reason == "always_policy"
    assert bundle.status == "used"
    assert bundle.used_at is not None


def test_mcp_build_agent_input_bundle_rejects_ttl_over_max(page_service, actor):
    workspace_id, client_id = _workspace_and_client(page_service)

    response = dispatch(
        "build_agent_input_bundle",
        {
            "workspace_id": str(workspace_id),
            "task_intent": "bad ttl",
            "client_id": str(client_id),
            "token_budget": 2048,
            "ttl_override_seconds": 999999,
            "permission_context": {"role": "internal_agent"},
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "rejected_by_policy"
    assert response.envelope.error_code == "rejected_by_policy"
