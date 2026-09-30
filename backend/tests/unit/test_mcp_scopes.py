from backend.mcp.protocol import tool_descriptors
from backend.mcp.router import TOOL_HANDLERS
from backend.mcp.scopes import (
    TOOL_REQUIRED_ROLES,
    TOOL_REQUIRED_SCOPES,
    expanded_actor_scopes,
    visible_tool_names,
)
from backend.policy.auth import ActorContext


def test_mcp_write_scope_includes_read_tools_without_agent_draft_tools():
    names = set(visible_tool_names(actor_with_scopes(("mcp:write",))))

    assert {"list_my_workspaces", "read", "create_context_page"}.issubset(names)
    assert "wiki_maintenance" in names
    assert "write_agent_page_draft" not in names


def test_mcp_agent_scope_includes_agent_bundle_and_agent_draft_tools():
    names = set(visible_tool_names(actor_with_scopes(("mcp:agent",))))

    assert {"build_agent_input_bundle", "write_agent_page_draft"}.issubset(names)


def test_mcp_tool_registries_have_same_tool_names():
    descriptor_names = {tool.name for tool in tool_descriptors.list_dtxwiki_tools()}
    scope_names = set(TOOL_REQUIRED_SCOPES)
    role_names = set(TOOL_REQUIRED_ROLES)
    handler_names = set(TOOL_HANDLERS)
    metadata_names = set(tool_descriptors.TOOL_META)
    descriptor_role_names = {
        tool.name
        for tool in tool_descriptors.list_dtxwiki_tools()
        if tool.meta and "required_role" in tool.meta
    }

    assert handler_names == scope_names
    assert handler_names == role_names
    assert handler_names == descriptor_names
    assert handler_names == metadata_names
    assert handler_names == descriptor_role_names


def test_mcp_read_umbrella_excludes_privileged_scopes():
    # Codex #4/#7: read-only must NOT grant audit-read or review-read.
    scopes = expanded_actor_scopes(actor_with_scopes(("mcp:read",)))
    assert "audit-read" not in scopes
    assert "review-read" not in scopes
    assert "maintenance-write" not in scopes
    assert {"read", "search", "context-read", "workspace-read"}.issubset(scopes)


def test_mcp_write_umbrella_still_includes_privileged_scopes():
    scopes = expanded_actor_scopes(actor_with_scopes(("mcp:write",)))
    assert {
        "audit-read",
        "review-read",
        "draft-write",
        "maintenance-write",
    }.issubset(scopes)


def test_mcp_read_cannot_see_review_tool():
    names = set(visible_tool_names(actor_with_scopes(("mcp:read",))))
    assert "list_requested_changes" not in names
    assert "list_changes" not in names


def test_mcp_audit_scope_exposes_change_history():
    names = set(visible_tool_names(actor_with_scopes(("audit-read",))))

    assert "list_changes" in names


def test_offline_access_token_has_exactly_the_same_capabilities_as_without_it():
    without_offline_access = actor_with_scopes(("mcp:read",))
    with_offline_access = actor_with_scopes(("mcp:read", "offline_access"))

    assert expanded_actor_scopes(with_offline_access) == expanded_actor_scopes(
        without_offline_access
    )
    assert "offline_access" not in expanded_actor_scopes(with_offline_access)
    assert visible_tool_names(with_offline_access) == visible_tool_names(
        without_offline_access
    )


def actor_with_scopes(scopes: tuple[str, ...]) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id="test-user",
        scopes=scopes,
        workspace_role="owner",
    )
