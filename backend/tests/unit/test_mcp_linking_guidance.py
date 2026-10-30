from sqlalchemy import select

from backend.mcp.protocol.instructions import MCP_SERVER_INSTRUCTIONS
from backend.mcp.protocol.tool_descriptors import list_dtxwiki_tools
from backend.models.workspace import Workspace
from backend.services.workspace_read import WorkspaceReadService


def test_mcp_write_surfaces_explain_context_linking() -> None:
    tools = {tool.name: tool for tool in list_dtxwiki_tools()}

    assert "link_markdown" in MCP_SERVER_INSTRUCTIONS
    assert "stable_url or page_id unchanged" in MCP_SERVER_INSTRUCTIONS
    assert "authored context-to-context link relations" in (
        MCP_SERVER_INSTRUCTIONS.lower()
    )

    for tool_name in (
        "create_context_page",
        "update_context_edit",
        "patch_context_edit",
        "submit_context_edit_for_review",
    ):
        description = tools[tool_name].description
        assert "link_markdown" in description, tool_name
        assert (
            "authored context-to-context link relations" in description.lower()
        ), tool_name
        assert "appear after publication" in description, tool_name

    submit_description = tools["submit_context_edit_for_review"].description
    assert "context_page_has_no_graph_visible_links" in submit_description
    assert "submission still succeeds" in submit_description


def test_guide_returns_typed_machine_readable_linking_guidance(page_service) -> None:
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        assert workspace_id is not None
        guide = WorkspaceReadService().guide(
            session=session,
            workspace_id=workspace_id,
            detail_level="brief",
        )

    assert [syntax.link_syntax for syntax in guide.linking.syntaxes] == [
        "wikilink",
        "context_url",
    ]
    assert [syntax.template for syntax in guide.linking.syntaxes] == [
        "[[slug]]",
        "[title](/p/<workspace_id>/<page_id>)",
    ]
    assert all(syntax.target_source for syntax in guide.linking.syntaxes)
    sources = {
        syntax.link_syntax: syntax.target_source
        for syntax in guide.linking.syntaxes
    }
    assert "known page slug" in sources["wikilink"]
    assert "link_markdown" in sources["wikilink"]
    assert "link_markdown" in sources["context_url"]
    assert "stable_url unchanged" in sources["context_url"]
    assert guide.linking.graph_visibility == "published_head_only"
    assert (
        guide.linking.missing_links_warning
        == "context_page_has_no_graph_visible_links"
    )
    assert "published" in guide.linking.note.lower()
