import json
from pathlib import Path

from backend.mcp.protocol.tool_descriptors import (
    TOOL_ACTIONS,
    TOOL_META,
    list_dtxwiki_tools,
)
from backend.mcp.scopes import TOOL_REQUIRED_ROLES
from backend.scripts.export_mcp_schemas import build_mcp_schemas

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


def test_lists_tools_with_input_schemas_and_no_wire_output_schema():
    tools = list_dtxwiki_tools()
    exported_schemas = build_mcp_schemas()

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.inputSchema["type"] == "object"
        assert "properties" in tool.inputSchema
        assert tool.inputSchema["additionalProperties"] is False
        # outputSchema is deliberately never advertised on the wire (75% of
        # the tools/list payload, never read by a model -- see tool()'s
        # docstring in tool_descriptors.py). The checked-in export stays the
        # full, real contract for the runner's own output validation.
        assert tool.outputSchema is None
        exported = exported_schemas[tool.name]["output"]
        assert exported["type"] == "object"
        assert "properties" in exported
        assert exported["additionalProperties"] is False


def test_tool_descriptors_expose_required_role_metadata():
    tools = {tool.name: tool for tool in list_dtxwiki_tools()}

    assert tools["list_my_workspaces"].meta["required_role"] is None
    assert tools["read"].meta["required_role"] == "viewer"
    assert tools["create_context_page"].meta["required_role"] == "editor"


def test_tool_descriptor_roles_match_runtime_registry():
    tools = {tool.name: tool for tool in list_dtxwiki_tools()}

    assert set(tools) == set(TOOL_REQUIRED_ROLES)
    for tool_name, required_role in TOOL_REQUIRED_ROLES.items():
        assert tools[tool_name].meta["required_role"] == required_role


def test_search_descriptor_advertises_planner_and_optional_modes():
    search = next(tool for tool in list_dtxwiki_tools() if tool.name == "search")

    assert "direct paths" in search.description
    assert "semantic and hybrid" in search.description
    assert "all scope excludes feedback and changes" in search.description
    assert "explicit feedback or change scope" in search.description
    assert "rejected" not in search.description


def test_fetch_descriptor_is_read_only_and_idempotent():
    tool = next(tool for tool in list_dtxwiki_tools() if tool.name == "fetch")

    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True
    assert tool.annotations.openWorldHint is False
    assert "conversation_token" not in tool.inputSchema["required"]


def test_wiki_maintenance_descriptor_discloses_budget_and_staging():
    tool = next(
        tool for tool in list_dtxwiki_tools() if tool.name == "wiki_maintenance"
    )

    assert tool.annotations.readOnlyHint is False
    assert "up to 8" in tool.description
    assert "~2k tokens" in tool.description
    assert "staged for human review" in tool.description


def test_upload_image_descriptor_is_idempotent_and_localized():
    tool = next(tool for tool in list_dtxwiki_tools() if tool.name == "upload_image")

    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True
    assert tool.annotations.openWorldHint is False
    assert "conversation_token" in tool.inputSchema["required"]
    assert "data.logical_ref" in tool.description
    assert "editing_version_id" in tool.description
    assert "content_url" in tool.description
    assert "relative same-origin URL" not in tool.description
    assert tool.meta["com.dtxwiki/localizedTitles"] == {
        "en": "Upload image",
        "ko": "이미지 업로드",
    }


def test_every_backend_tool_has_frontend_en_and_ko_presentation_coverage():
    repo_root = Path(__file__).resolve().parents[3]
    frontend_root = repo_root / "frontend" / "src"
    registry = json.loads(
        (
            frontend_root / "features" / "mcp-chats" / "toolPresentationRegistry.json"
        ).read_text(encoding="utf-8")
    )
    locales = {
        locale: json.loads(
            (
                frontend_root / "shared" / "i18n" / "locales" / locale / "chats.json"
            ).read_text(encoding="utf-8")
        )
        for locale in ("en", "ko")
    }

    exposed_tools = {tool.name for tool in list_dtxwiki_tools()}
    assert exposed_tools == set(TOOL_META) == set(TOOL_ACTIONS) == set(registry)
    for tool_name, meta in TOOL_META.items():
        assert meta.localized_titles is not None
        assert set(meta.localized_titles) >= {"en", "ko"}
        title_key = registry[tool_name]
        for locale in ("en", "ko"):
            assert (
                locales[locale]["presentation"]["toolTitles"][title_key]
                == meta.localized_titles[locale]
            )


def test_every_advertised_input_schema_is_host_portable():
    """Hosts convert inputSchema with narrow JSON Schema support: a `$ref`
    item reached one agent as Array<unknown>. What is advertised must use only
    keywords every host reads, and keep every field."""

    from backend.mcp.protocol.schema_projection import (
        schema_uses_only_portable_keywords,
    )

    tools = {tool.name: tool for tool in list_dtxwiki_tools()}
    for name, tool in tools.items():
        assert schema_uses_only_portable_keywords(tool.inputSchema), name

    operations = tools["patch_context_edit"].inputSchema["properties"]["operations"]
    item = operations["items"]
    assert item["type"] == "object"
    assert len(item["properties"]["op"]["enum"]) == 11
    for field in ("old_text", "new_text", "block", "expected", "content", "at"):
        assert field in item["properties"], field
    assert "replace_block (requires block, expected, content" in (
        item["properties"]["op"]["description"]
    )
    assert "title" in item["properties"], "a field named title must survive"


def test_flattened_operation_fields_keep_their_size_limits():
    """Merging variants must not drop a bound: a model that sees no maxLength
    sends a body the server rejects. Upper bounds keep the strictest declared
    value; a lower bound survives only if every variant declares it."""

    from backend.schemas.mcp.context_tools import PatchContextEditInput

    raw_defs = PatchContextEditInput.model_json_schema()["$defs"]
    variants = [
        definition
        for name, definition in raw_defs.items()
        if name.endswith("Operation") and "op" in definition.get("properties", {})
    ]
    projected = {tool.name: tool for tool in list_dtxwiki_tools()}[
        "patch_context_edit"
    ].inputSchema["properties"]["operations"]["items"]["properties"]

    for field in ("content", "new_text", "old_text"):
        declared = [
            variant["properties"][field]
            for variant in variants
            if field in variant["properties"]
        ]
        max_values = [spec["maxLength"] for spec in declared if "maxLength" in spec]
        if max_values:
            assert projected[field]["maxLength"] == min(max_values), field
        if all("minLength" in spec for spec in declared):
            assert projected[field]["minLength"] == min(
                spec["minLength"] for spec in declared
            ), field
        else:
            assert "minLength" not in projected[field], field
    # replace_text deletes by sending an empty new_text; that must stay legal.
    assert "minLength" not in projected["new_text"]


def test_nested_address_unions_keep_every_variant_after_flattening():
    """Several operations carry an `at` object with its own `by` union.
    Flattening merged them field by field only at the top level, so the
    advertised `at.by` kept one operation's variants and dropped the
    replace_text block address a model needs to scope a forgiving match."""

    item = {tool.name: tool for tool in list_dtxwiki_tools()}["patch_context_edit"]
    at = item.inputSchema["properties"]["operations"]["items"]["properties"]["at"]
    by = at["properties"]["by"]
    assert set(by["enum"]) == {"section_id", "heading_path", "anchor", "block"}
    assert "block (requires block, expected)" in by["description"]
    assert "anchor (requires anchor_text" in by["description"]
    assert at["required"] == ["by"]
    for field in ("block", "expected", "section_id", "heading_path", "anchor_text"):
        assert field in at["properties"], field
