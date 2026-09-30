from pathlib import Path

import pytest

from backend.mcp.protocol.tool_descriptors import list_dtxwiki_tools
from backend.mcp.router import TOOL_HANDLERS
from backend.mcp.scopes import (
    CONTENT_TOOLS,
    TOOL_REQUIRED_ROLES,
    TOOL_REQUIRED_SCOPES,
    visible_tool_names,
)
from backend.policy.auth import ActorContext

REPO_ROOT = Path(__file__).resolve().parents[3]

TOOL_LIST_SNAPSHOTS = {
    "mcp:read": (
        "list_my_workspaces",
        "guide",
        "search",
        "fetch",
        "grep_wiki",
        "what_changed",
        "read",
        "get_index",
        "read_concept",
        "list_agent_categories",
        "read_agent_page",
        "read_context_outline",
        "read_structure_map",
    ),
    "mcp:write": (
        "list_my_workspaces",
        "guide",
        "search",
        "fetch",
        "grep_wiki",
        "what_changed",
        "read",
        "get_index",
        "read_concept",
        "list_changes",
        "list_agent_categories",
        "read_agent_page",
        "create_context_page",
        "begin_context_edit",
        "update_context_edit",
        "submit_context_edit_for_review",
        "discard_context_edit",
        "patch_context_edit",
        "list_my_context_edits",
        "upload_image",
        "cite",
        "submit_feedback",
        "list_requested_changes",
        "submit_revision_for_review",
        "wiki_maintenance",
        "read_context_outline",
        "read_structure_map",
    ),
    "mcp:agent": (
        "write_agent_page_draft",
        "build_agent_input_bundle",
    ),
}


def _assert_consistent_tool_inventory(
    registries: dict[str, set[str]],
    content_tools: set[str],
) -> None:
    all_tools = set().union(*registries.values())
    problems: list[str] = []
    for tool_name in sorted(all_tools):
        missing_from = [
            registry_name
            for registry_name, tools in registries.items()
            if tool_name not in tools
        ]
        if missing_from:
            problems.append(
                f"{tool_name}: missing from {', '.join(missing_from)}"
            )
    for tool_name in sorted(content_tools - all_tools):
        problems.append(f"{tool_name}: CONTENT_TOOLS has no registered tool")
    assert not problems, "MCP tool inventory drift:\n" + "\n".join(problems)


def _current_tool_registries() -> dict[str, set[str]]:
    schema_dir = REPO_ROOT / "shared/schemas/mcp/v1"
    return {
        "TOOL_REQUIRED_SCOPES": set(TOOL_REQUIRED_SCOPES),
        "TOOL_HANDLERS": set(TOOL_HANDLERS),
        "tool descriptors": {tool.name for tool in list_dtxwiki_tools()},
        "shared/schemas/mcp/v1": {
            path.stem for path in schema_dir.glob("*.json")
        },
        "TOOL_REQUIRED_ROLES": set(TOOL_REQUIRED_ROLES),
    }


def test_mcp_tool_inventory_registries_are_consistent():
    _assert_consistent_tool_inventory(
        _current_tool_registries(),
        set(CONTENT_TOOLS),
    )


@pytest.mark.parametrize(
    "missing_registry",
    (
        "TOOL_REQUIRED_SCOPES",
        "TOOL_HANDLERS",
        "tool descriptors",
        "shared/schemas/mcp/v1",
        "TOOL_REQUIRED_ROLES",
    ),
)
def test_inventory_assertion_names_tool_and_missing_registry(missing_registry):
    registries = {
        registry_name: {"guide", "search"}
        for registry_name in (
            "TOOL_REQUIRED_SCOPES",
            "TOOL_HANDLERS",
            "tool descriptors",
            "shared/schemas/mcp/v1",
            "TOOL_REQUIRED_ROLES",
        )
    }
    registries[missing_registry].remove("search")

    with pytest.raises(AssertionError) as exc_info:
        _assert_consistent_tool_inventory(registries, {"search"})

    message = str(exc_info.value)
    assert "search" in message
    assert f"missing from {missing_registry}" in message


def test_inventory_assertion_rejects_unregistered_content_tool():
    registries = {
        registry_name: {"guide"}
        for registry_name in (
            "TOOL_REQUIRED_SCOPES",
            "TOOL_HANDLERS",
            "tool descriptors",
            "shared/schemas/mcp/v1",
            "TOOL_REQUIRED_ROLES",
        )
    }

    with pytest.raises(AssertionError, match="upload_image") as exc_info:
        _assert_consistent_tool_inventory(registries, {"upload_image"})

    assert "CONTENT_TOOLS has no registered tool" in str(exc_info.value)


def test_patch_context_edit_permission_and_conversation_contract():
    assert TOOL_REQUIRED_SCOPES["patch_context_edit"] == ("draft-write",)
    assert TOOL_REQUIRED_ROLES["patch_context_edit"] == "editor"
    assert "patch_context_edit" in CONTENT_TOOLS


@pytest.mark.parametrize(
    ("scope", "expected_names"),
    TOOL_LIST_SNAPSHOTS.items(),
)
def test_tools_list_snapshot_for_scope(scope, expected_names):
    actor = ActorContext(
        actor_kind="mcp_client",
        actor_id=f"{scope}-snapshot",
        scopes=(scope,),
        workspace_role="owner",
    )

    actual_names = tuple(
        tool.name
        for tool in list_dtxwiki_tools(visible_tool_names(actor))
    )

    assert actual_names == expected_names
