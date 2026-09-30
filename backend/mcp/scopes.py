"""MCP tool scope registry."""

from collections.abc import Iterable

from backend.policy.auth import ActorContext

# Actor-scope primitives (and the body-scope gates built on them) live in
# backend.policy.mcp_scopes -- backend/services must not import backend/mcp
# (tests/static/test_architecture_boundaries.py), and
# backend.services.domain_read needs these too. Re-exported here so every
# existing ``from backend.mcp.scopes import ...`` caller is unaffected.
from backend.policy.mcp_scopes import (  # noqa: F401
    AGENT_PAGE_BODY_SCOPES,
    CONTEXT_PAGE_BODY_SCOPES,
    expanded_actor_scopes,
    has_required_scope,
)

TOOL_REQUIRED_SCOPES = {
    "list_my_workspaces": ("workspace-read", "guide", "read"),
    "guide": ("guide", "workspace-read"),
    "search": ("search",),
    "fetch": ("read",),
    "grep_wiki": ("search", "read"),
    "what_changed": ("read",),
    "read": ("read",),
    "get_index": ("read",),
    "read_concept": ("read",),
    "list_changes": ("audit-read",),
    "list_agent_categories": ("guide", "agent-page-read"),
    "read_agent_page": ("agent-page-read",),
    "create_context_page": ("draft-write",),
    "begin_context_edit": ("draft-write",),
    "update_context_edit": ("draft-write",),
    "submit_context_edit_for_review": ("draft-write",),
    "discard_context_edit": ("draft-write",),
    "patch_context_edit": ("draft-write",),
    "list_my_context_edits": ("draft-write",),
    "upload_image": ("draft-write",),
    "cite": ("cite",),
    "submit_feedback": ("feedback-write",),
    "write_agent_page_draft": ("agent-page-draft",),
    "build_agent_input_bundle": ("agent-bundle",),
    "list_requested_changes": ("review-read",),
    # Mutates review state (claims + supersedes the original review), so it must
    # require a review-WRITE capability — a draft-write-only token must not be
    # able to close a routed review by known id. review-write is granted only by
    # the mcp:write umbrella (below), never by mcp:read.
    "submit_revision_for_review": ("review-write",),
    "wiki_maintenance": ("maintenance-write",),
    "read_context_outline": ("read",),
    "read_structure_map": ("read",),
}

# Tools that write workspace CONTENT, as opposed to discovery/read tools.
#
# Conversation continuity keys off this set: a content call must carry an
# explicit conversation_token (or "new"). Discovery/read calls accept it
# optionally; without one they mint a fresh token and their persisted trace
# creates a separate visible MCP chat session.
#
# This gate covers calls that write content or create content-related records,
# plus citation lookup and bundle construction because they participate in the
# same stateful authoring workflow. Some write handlers also establish a chat
# session up-front to stamp ChangeSet.origin_chat_session_id; the gate does not
# depend on that implementation detail.
CONTENT_TOOLS = frozenset(
    {
        "create_context_page",
        "begin_context_edit",
        "update_context_edit",
        "submit_context_edit_for_review",
        "discard_context_edit",
        "patch_context_edit",
        "upload_image",
        "cite",
        "submit_feedback",
        "write_agent_page_draft",
        "build_agent_input_bundle",
        "submit_revision_for_review",
        "wiki_maintenance",
    }
)

TOOL_REQUIRED_ROLES = {
    "list_my_workspaces": None,
    "guide": "viewer",
    "search": "viewer",
    "fetch": "viewer",
    "grep_wiki": "viewer",
    "what_changed": "viewer",
    "read": "viewer",
    "get_index": "viewer",
    "read_concept": "viewer",
    "list_changes": "viewer",
    "list_agent_categories": "viewer",
    "read_agent_page": "viewer",
    "create_context_page": "editor",
    "begin_context_edit": "editor",
    "update_context_edit": "editor",
    "submit_context_edit_for_review": "editor",
    "discard_context_edit": "editor",
    "patch_context_edit": "editor",
    "list_my_context_edits": "editor",
    "upload_image": "editor",
    "cite": "editor",
    "submit_feedback": "viewer",
    "write_agent_page_draft": "editor",
    "build_agent_input_bundle": "editor",
    "list_requested_changes": "editor",
    "submit_revision_for_review": "editor",
    "wiki_maintenance": "editor",
    "read_context_outline": "viewer",
    "read_structure_map": "viewer",
}


def visible_tool_names(
    actor: ActorContext,
    tool_names: Iterable[str] | None = None,
) -> list[str]:
    candidates = tool_names or TOOL_REQUIRED_SCOPES.keys()
    return [
        tool_name
        for tool_name in candidates
        if has_required_scope(actor, TOOL_REQUIRED_SCOPES[tool_name])
    ]
