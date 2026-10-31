"""MCP actor-scope primitives shared below the mcp/ layer.

``backend/services`` must not import ``backend.mcp`` (see
tests/static/test_architecture_boundaries.py) -- services is a lower layer
that mcp/ builds on, not the reverse. ``has_required_scope`` and the
body-scope gates it evaluates need to be callable from both a services-layer
read seam (``backend.services.domain_read``) and the mcp-layer tool/protocol
code, so the primitives live here; ``backend.mcp.scopes`` re-exports them for
its existing callers.
"""

from backend.policy.auth import ActorContext

# MCP-02: per-entity-kind body scope gates. These must be applied by every
# surface that can return an entity BODY (fetch, read_agent_page, the
# prompts/resources lane, ...) so a narrow tool a client's scopes cannot
# reach cannot be bypassed by a wider one reading the same bytes. Mirrors
# backend.mcp.scopes.TOOL_REQUIRED_SCOPES: AgentPage bodies require
# agent-page-read (as read_agent_page does); context page bodies require
# read/context-read (as read does).
AGENT_PAGE_BODY_SCOPES = ("agent-page-read",)
CONTEXT_PAGE_BODY_SCOPES = ("read", "context-read")


def has_required_scope(actor: ActorContext, required_scopes: tuple[str, ...]) -> bool:
    actor_scopes = expanded_actor_scopes(actor)
    return "*" in actor_scopes or any(
        scope in actor_scopes for scope in required_scopes
    )


def expanded_actor_scopes(actor: ActorContext) -> set[str]:
    # OAuth protocol scopes can travel in the token without being MCP
    # capabilities. In particular, ``offline_access`` describes session
    # continuity only and must never satisfy a tool permission.
    scopes = set(actor.scopes) - {"offline_access"}
    if "mcp:read" in scopes:
        # mcp:read is page-read only. audit-read and review-read are privileged
        # surfaces (MCP-chat audit, review queue) granted ONLY by mcp:write — a
        # read-only token must not reach them.
        scopes.update(
            {
                "guide",
                "search",
                "read",
                "context-read",
                "workspace-read",
                "agent-page-read",
            }
        )
    if "mcp:write" in scopes:
        scopes.update(
            {
                "guide",
                "search",
                "read",
                "context-read",
                "workspace-read",
                "agent-page-read",
                "audit-read",
                "review-read",
                "draft-write",
                "review-write",
                "cite",
                "feedback-write",
                "maintenance-write",
            }
        )
    if "mcp:agent" in scopes:
        scopes.update({"agent-bundle", "agent-page-draft"})
    return scopes


# The shared definition of a "focused read": a tool call whose result is one
# named artifact the caller asked for by identity. Search and discovery tools
# (search, grep_wiki, list_pages, get_index) are deliberately absent — they
# return a ranked candidate list the caller has not committed to reading, so
# treating their results as reads overstates what happened.
#
# Two surfaces depend on this being one definition: MCP-chat session-title
# evidence (backend.services.mcp_chat_service) and the behavioural read
# aggregates in backend.services.indexing.edge_registry / trail_analysis. If
# they drift, a document can be "often read together" on one surface and
# never read on the other.
FOCUSED_READ_TOOLS = frozenset(
    {"fetch", "read", "read_agent_page", "read_concept", "read_page"}
)
