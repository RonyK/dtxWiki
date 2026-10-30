"""Client-facing instructions shared by the HTTP MCP server implementations.

Kept in its own module because ``create_mcp_server`` and
``_build_dispatcher_server`` each used to carry their own byte-identical copy
of this string. Two copies meant neither was updated when the legacy Pages
tools were deleted, so both HTTP modes kept advertising tools that returned
``unknown_tool``.
"""

from backend.spi.tool_presentation import MCP_CONTEXT_LINK_GUIDANCE

MCP_SERVER_INSTRUCTIONS = (
    "Call list_my_workspaces to choose a workspace_id, then guide for that "
    "workspace's workflow and policy context; every other tool is "
    "workspace-scoped and takes workspace_id explicitly. dtxWiki exposes "
    "keyword search followed by fetch, direct read, heading outline and "
    "block-level structure map reads, change history, Context page "
    "create/edit/submit/discard tools, AgentPage read/draft tools, "
    "AgentInputBundle tools, review resubmission, maintenance, and MCP "
    "prompts/resources for active AgentPages. tools/list is the authoritative "
    "catalog. To create a new Context page, call create_context_page; to edit "
    "an existing one, call begin_context_edit then update_context_edit (whole "
    "body) or patch_context_edit (typed block/section operations addressed by "
    "read_structure_map ids) followed by submit_context_edit_for_review. "
    "Read "
    "and discovery tools accept "
    "conversation_token "
    "optionally. Content tools (writes, citations, feedback, bundles, and "
    "maintenance) require it: echo envelope.conversation_token from an earlier "
    'call or pass the literal "new" to start a conversation. A result field '
    "that is left out has its default (false, 0, empty, or null); a field "
    "whose null means unknown is always sent. envelope.request_id names the "
    "call when you report a problem."
) + " " + MCP_CONTEXT_LINK_GUIDANCE
