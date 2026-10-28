"""REST endpoint scope registry for bearer-token actors.

Session cookie actors bypass scope enforcement (full-privilege). OAuth JWT and
Personal API Token actors must hold a scope that satisfies the registered
requirement for the matched route. Routes that are neither registered nor in
the exempt prefix list default-deny bearer access.

The registry stores the *internal* scopes each route requires (e.g. ``read``,
``draft-write``). Tokens that hold the umbrella scopes (``mcp:read`` /
``mcp:write`` / ``mcp:agent``) satisfy these requirements transparently via
``has_required_scope`` — see ``backend.mcp.scopes.expanded_actor_scopes``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request

from backend.app.state import get_container_from_app, get_session_factory_from_app
from backend.mcp.scopes import has_required_scope
from backend.policy.auth import ActorContext, get_rest_actor
from backend.policy.mcp_scopes import CONTEXT_PAGE_BODY_SCOPES
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspacePlaneUnavailableError,
    rebind_actor_to_workspace_access,
    require_workspace_role,
)
from backend.policy.trust_boundaries import actor_with_target_workspace_scope
from backend.policy.workspace_resolution import workspace_existence_is_hidden
from backend.services.read_errors import RejectedByPolicyError

# (HTTP method, FastAPI route path template) → required scopes (any-of).
REST_REQUIRED_SCOPES: dict[tuple[str, str], tuple[str, ...]] = {
    ("GET", "/api/v1/context/pages/{page_id}/location"): CONTEXT_PAGE_BODY_SCOPES,
    # /api/v1/workspaces/{workspace_id}/context/pages (P9.1 canonical Context
    # route family -- replaces the old /api/v1/pages* (PageService, P8) and
    # the intermediate context-pages* (DomainReadService/ContextWriteService)
    # surfaces). GET routes accept either scope: "read" and "context-read"
    # were the two prior surfaces' respective read grants, and
    # backend.policy.mcp_scopes.CONTEXT_PAGE_BODY_SCOPES already treats them
    # as equivalent for context page body access -- this consolidation makes
    # that equivalence the registered contract instead of two separate,
    # arbitrarily-scoped routes for the same capability.
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/tags",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/by-slug/{slug}",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/backlinks",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/versions",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        "/versions/{branch_id}/reconciliation",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/diff",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/comments",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/activity",
    ): CONTEXT_PAGE_BODY_SCOPES,
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/neighbors",
    ): CONTEXT_PAGE_BODY_SCOPES,
    ("POST", "/api/v1/workspaces/{workspace_id}/context/pages"): ("draft-write",),
    (
        "PATCH",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/autosave",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/begin-edit",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/submit-for-review",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/discard-edit",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/restore",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        "/versions/{version_id}/discard",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        "/versions/{branch_id}/reconciliation",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        "/versions/{version_id}/publish",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/publish",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/comments",
    ): ("draft-write",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/media/capabilities",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/media",
    ): ("read",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/media",
    ): ("draft-write",),
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/context/media/assets/{asset_id}",
    ): ("draft-write",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/pages/{page_id}"
        "/versions/{page_version_id}/media/{asset_id}/content",
    ): ("read",),
    # /api/v1/workspaces — read surface
    ("GET", "/api/v1/workspaces"): ("workspace-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}"): ("workspace-read",),
    ("POST", "/api/v1/workspaces"): ("workspace-admin",),
    ("GET", "/api/v1/workspaces/{workspace_id}/settings/policies"): ("workspace-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/settings/mcp-clients"): (
        "workspace-read",
    ),
    ("PATCH", "/api/v1/workspaces/{workspace_id}/settings/policies"): (
        "workspace-admin",
    ),
    ("PATCH", "/api/v1/workspaces/{workspace_id}/settings/policies/batch"): (
        "workspace-admin",
    ),
    (
        "PATCH",
        "/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{client_id}",
    ): ("workspace-admin",),
    # session-only; the handler enforces owner. (Was: "workspace-admin" is the
    # bearer-token SCOPE; the handler additionally enforces
    # require_workspace_role("owner")" -- PURGE_AUTHZ_PLAN S-1 moved
    # DELETE .../workspaces/{workspace_id} to REST_SESSION_ONLY_ROUTES, so no
    # bearer scope satisfies it any more; only a session cookie reaches the
    # handler's owner-role check at all.)
    ("GET", "/api/v1/workspaces/{workspace_id}/tree"): ("read",),
    (
        "PUT",
        "/api/v1/workspaces/{workspace_id}/favorites/{target_kind}/{target_id}",
    ): ("read",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/reconciliations/discard-bulk",
    ): ("draft-write",),
    ("GET", "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}"): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}/index-preview",
    ): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/library/items"): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/library/capabilities"): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/library/tombstones"): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/items/{library_item_id}",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/items/{library_item_id}/references",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/items/{item_id}/versions",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/content",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/preview",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/preview/source",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/semantic-markdown",
    ): ("read",),
    (
        "PUT",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/semantic-markdown",
    ): ("draft-write",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/semantic-overlays/{overlay_id}/content",
    ): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact_id}"): (
        "read",
    ),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact_id}/content",
    ): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/agent/categories"): ("agent-page-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/agent/pages"): ("agent-page-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}",
    ): ("agent-page-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/bundles",
    ): ("agent-bundle",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/diff",
    ): ("agent-page-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/agent/bundles/{bundle_id}",
    ): ("agent-bundle",),
    ("GET", "/api/v1/workspaces/{workspace_id}/ask/sessions"): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session_id}",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/ingress-proposals",
    ): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal_id}",
    ): ("read",),
    # Feedback bodies are audit-class (same tier as /activity and /changesets):
    # a plain read/mcp:read bearer token must not enumerate them. Session cookies
    # bypass this check (above) and are gated by role in the handler
    # (actor_can_read_audit_records requires editor+ for sessions).
    ("GET", "/api/v1/workspaces/{workspace_id}/feedback"): ("audit-read",),
    # Review surfaces are privileged: a plain read/mcp:read bearer token must not
    # enumerate workspace review state. review-read is granted only by mcp:write
    # (see backend/mcp/scopes.py). Session cookies bypass this check (above) and
    # are gated by role + a personal-queue projection in the handlers.
    ("GET", "/api/v1/workspaces/{workspace_id}/reviews"): ("review-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/reviews/counts"): ("review-read",),
    # Captures a signed read-only selection; approval remains a separate
    # draft-write operation with its own actor/provenance checks.
    ("POST", "/api/v1/workspaces/{workspace_id}/reviews/selection"): ("review-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/reviews/decisions/recent",
    ): ("review-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}",
    ): ("review-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/activity"): ("audit-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/activity/feed"): ("audit-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/activity/read-group"): ("audit-read",),
    # Same sensitivity tier as /activity (which surfaces job status and the
    # redacted error_code inline -- last_error itself never crosses either
    # API, admin included; it is a protected server-log/DB-diagnostic value
    # only): job internals are an operations concern, not a plain
    # read/mcp:read bearer grant.
    ("GET", "/api/v1/workspaces/{workspace_id}/jobs"): ("audit-read",),
    # Epic-2 Phase 5c (orphaned Context-media reclamation): same sensitivity
    # tier as /jobs -- filenames and uploader identities are an operations
    # concern, not a plain read/mcp:read bearer grant. Role gating is
    # separately admin-only (WorkspaceAdminAccessDep), stricter than /jobs'
    # viewer gate.
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/media/orphans",
    ): ("audit-read",),
    # The workspace attachment listing is the same class of data as the orphan
    # listing above -- filenames, uploader identities, the pages they hang off
    # -- so it takes the same tier, even though its role gate is looser
    # (viewer, with scope=workspace separately narrowed to owner). Loosening
    # this to plain `read` would let any read-scoped bearer enumerate every
    # file in the workspace, which is precisely what the orphan route was
    # deliberately kept out of.
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/context/media/attachments",
    ): ("audit-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/mcp-chats"): ("audit-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/export",
    ): ("workspace.audit_export",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_session_id}",
    ): ("audit-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/resource-provenance",
    ): ("audit-read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/resource-links",
    ): ("audit-read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/lint/findings"): ("read",),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/lint/runs/{run_id}",
    ): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/coverage-gaps"): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/lint/rules"): ("read",),
    ("GET", "/api/v1/workspaces/{workspace_id}/changesets"): ("audit-read",),
    # /api/v1/workspaces — read-ish POST (viewer-equivalent)
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/search/capabilities",
    ): ("search",),
    ("POST", "/api/v1/workspaces/{workspace_id}/search"): ("search",),
    ("POST", "/api/v1/workspaces/{workspace_id}/feedback"): ("feedback-write",),
    # /api/v1/workspaces — write surface
    (
        "PATCH",
        "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}",
    ): ("draft-write",),
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/tree/nodes/{node_id}",
    ): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/tree/nodes"): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/tree/move"): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/tree/reorder"): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/library/items"): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/library/items/{item_id}/versions",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/"
        "conversion/retry",
    ): ("draft-write",),
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/library/items/{library_item_id}",
    ): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/ask/sessions"): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session_id}/turns",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/ask/turns/{ask_turn_id}/promote",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal_id}/accept",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal_id}/reject",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/sweep-auto-approvals",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/claim",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/release",
    ): ("draft-write",),
    (
        "PATCH",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_session_id}",
    ): ("draft-write",),
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_session_id}",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_session_id}/unarchive",
    ): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/lint/runs"): ("draft-write",),
    ("POST", "/api/v1/workspaces/{workspace_id}/lint/format"): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/lint/findings/batch/decision",
    ): ("draft-write",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/lint/findings/{finding_id}/decision",
    ): ("draft-write",),
    # agent surface
    ("POST", "/api/v1/workspaces/{workspace_id}/agent/pages"): ("agent-page-draft",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/draft",
    ): ("agent-page-draft",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/restore",
    ): ("agent-page-draft",),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/agent/bundles/preview",
    ): ("agent-bundle",),
}


# REST routes that intentionally reject all bearer-token actors. Session-cookie
# callers still pass through workspace/role checks below. DEC-018 keeps
# AgentPage activation out of MCP/PAT/OAuth scope surfaces entirely.
REST_SESSION_ONLY_ROUTES: set[tuple[str, str]] = {
    ("PATCH", "/api/v1/users/me/default-workspace"),
    # PURGE_AUTHZ_PLAN S-1: workspace deletion (and the purge it can trigger)
    # is reachable only by the owner's own browser session -- no PAT/OAuth
    # scope, however broad, can satisfy this route any more.
    ("DELETE", "/api/v1/workspaces/{workspace_id}"),
    # Deleting a chat's stored edit previews erases part of the record of
    # what an AI client wrote. Only the owner's own browser session may do it;
    # no bearer token -- in particular not the client whose calls they are.
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/mcp-chats/{chat_session_id}/previews",
    ),
    ("GET", "/api/v1/workspace-directory"),
    ("GET", "/api/v1/workspaces/{workspace_id}/settings/access"),
    ("PATCH", "/api/v1/workspaces/{workspace_id}/settings/access"),
    ("GET", "/api/v1/workspaces/{workspace_id}/settings/mcp-apps"),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/settings/mcp-apps/{app_id}/setup-plan",
    ),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/settings/mcp-generic-profiles/"
        "{profile_id}/setup-plan",
    ),
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/settings/mcp-app-icons/{app_id}",
    ),
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/settings/mcp-clients/{client_id}",
    ),
    ("GET", "/api/v1/workspaces/{workspace_id}/access"),
    ("PATCH", "/api/v1/workspaces/{workspace_id}/access"),
    ("POST", "/api/v1/workspaces/{workspace_id}/join-requests"),
    ("DELETE", "/api/v1/workspaces/{workspace_id}/join-requests/me"),
    ("GET", "/api/v1/workspaces/{workspace_id}/join-requests"),
    (
        "PATCH",
        "/api/v1/workspaces/{workspace_id}/join-requests/{request_id}",
    ),
    ("GET", "/api/v1/workspaces/{workspace_id}/members"),
    ("GET", "/api/v1/workspaces/{workspace_id}/member-candidates"),
    ("POST", "/api/v1/workspaces/{workspace_id}/members"),
    (
        "PATCH",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}",
    ),
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}",
    ),
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/activate",
    ),
    # Undoing a review decision reverses a person's judgement (and may
    # withdraw a publication); no AI-held token may do it.
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decisions/"
        "{decision_id}/undo",
    ),
    # The Inbox is the signed-in person's own message box. Whether agents may
    # read or mark it is a later MCP decision, so bearer tokens stay out.
    ("GET", "/api/v1/users/me/inbox"),
    ("POST", "/api/v1/users/me/inbox/marks"),
    ("GET", "/api/v1/workspaces/{workspace_id}/inbox"),
    ("POST", "/api/v1/workspaces/{workspace_id}/inbox/marks"),
}

# These browser-session routes intentionally operate before membership exists.
# Their domain services perform the live user/owner checks appropriate to the
# operation; applying the generic viewer gate would make requesting membership
# impossible by definition.
REST_SESSION_UNSCOPED_ROUTES: set[tuple[str, str]] = {
    ("GET", "/api/v1/workspace-directory"),
    ("POST", "/api/v1/workspaces/{workspace_id}/join-requests"),
    ("DELETE", "/api/v1/workspaces/{workspace_id}/join-requests/me"),
}


# Path prefixes whose endpoints accept bearer tokens without scope enforcement.
# Examples: session lifecycle (``/api/v1/auth``), OAuth metadata + token
# endpoints (``/oauth``, ``/.well-known``), and the MCP transport itself.
# Health probes (``/health/live``, ``/health/ready``) need no entry here --
# they declare no ``RestActorDep`` at all, so no scope check ever applies.
#
# Why ``/mcp`` is kept in this list (Fix-5 plan §5.4 + Fix-6 Task 3.10):
# The MCP dispatcher (``backend/mcp/router.py::dispatch`` +
# ``resolve_target_actor``) performs its own per-tool scope + role check
# against ``TOOL_REQUIRED_SCOPES`` / ``TOOL_REQUIRED_ROLES`` before invoking
# any handler. Plan §5.4 considered "Plan B1-1 drop /mcp from
# SCOPE_EXEMPT_PREFIXES" but rejected it because route-level enforcement
# would double-check the same scope against a coarser registry and reject
# tools (e.g. ``list_my_workspaces``) that intentionally fall outside the
# REST scope hierarchy. Keep the per-tool path as the single source of
# truth — and keep this comment in sync with the dispatcher when adding
# new MCP tools.
SCOPE_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/v1/capabilities",  # Public instance feature/login metadata.
    "/api/v1/auth",
    # Instance server-admin surface is session-only (gated by
    # require_server_admin); bearer tokens carry no session and get 401, so no
    # token scope applies — scope-exempt by design.
    "/api/v1/admin",
    # T16: the public read surface is structurally unauthenticated (no
    # RestActorDep) — no token scope applies, so it is scope-exempt.
    "/api/v1/public",
    "/oauth",
    "/.well-known",
    "/mcp",
)


def _is_exempt(path: str) -> bool:
    for prefix in SCOPE_EXEMPT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _request_route_path(request: Request) -> str:
    """Return the fully-prefixed route template for old and new FastAPI routers.

    FastAPI's lazy included-router implementation leaves ``scope["route"]``
    pointing at the original child route (for example, ``/workspaces/{id}``)
    and keeps the include prefix in its effective route context.  Older
    versions expose the fully-prefixed template directly on the route.
    """

    fastapi_scope = request.scope.get("fastapi")
    if isinstance(fastapi_scope, dict):
        effective_context = fastapi_scope.get("effective_route_context")
        effective_path = getattr(effective_context, "path", None)
        if isinstance(effective_path, str) and effective_path:
            return effective_path
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    # Never request.url.path: Starlette rebuilds it from the Host header.
    return str(request.scope.get("path") or "")


def require_token_scope(
    request: Request,
    actor: Annotated[ActorContext, Depends(get_rest_actor)],
) -> ActorContext:
    """Enforce token-scope on REST endpoints for bearer-token actors.

    Session cookie actors return immediately. Bearer actors (``api_token`` or
    ``oauth_jwt``) must satisfy the matched route's registry entry.
    """

    path = _request_route_path(request)
    method = request.method
    if _is_exempt(path):
        return actor
    _reject_bearer_for_session_only_route(
        method=method,
        path=path,
        actor=actor,
    )
    if actor.token_kind == "session" and (method, path) in REST_SESSION_UNSCOPED_ROUTES:
        return actor
    if request.path_params.get("workspace_id") is not None:
        return scope_actor_to_request_workspace(request, actor)
    actor = _require_live_consent_for_bound_workspace(request, actor)
    return require_effective_token_scope(request, actor)


def _require_live_consent_for_bound_workspace(
    request: Request, actor: ActorContext
) -> ActorContext:
    """Fail closed for an OAuth actor with no live-consented workspace.

    D-01v2 (P10.3 T2 fix -- this function was still written for D-01's single
    ``actor.workspace_id`` claim and, left unchanged, would 403 EVERY OAuth
    call to a route with no path ``workspace_id`` such as ``GET
    /api/v1/workspaces``, since that field is now unresolved (``None``) until
    a target-scoped route narrows it): ``token_workspace_ids`` is already
    read live from ``oauth_consents`` (``removed_at IS NULL``) at
    authentication time, for EVERY request, not only target-scoped ones (see
    ``backend.policy.auth._authenticate_oauth_token``). A route with no path
    target has no single workspace left to re-check consent against -- the
    live read that mattered already ran during authentication. What remains
    this function's job is the same "no allowed workspace" fail-closed
    contract every target-scoped route enforces via
    ``actor_with_target_workspace_scope``.

    A PAT actor needs no equivalent check: ``ApiTokenService.authenticate_
    token`` already fails authentication outright when its allowed set is
    empty (backend/services/api_token_service.py).
    """

    if actor.token_kind != "oauth_jwt":
        return actor
    if not actor.token_workspace_ids:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "token_workspace_denied",
                "message": (
                    "token_workspace_denied: OAuth credential has no allowed workspace"
                ),
            },
        )
    return actor


def defer_token_scope_until_target(
    request: Request,
    actor: Annotated[ActorContext, Depends(get_rest_actor)],
) -> ActorContext:
    """Authenticate a late-target route while retaining registry fail-closed.

    The route capability itself is intentionally deferred until the handler
    resolves and narrows its target. Unregistered bearer routes and
    session-only routes are still rejected here, so choosing this dependency
    can never become an authorization bypass by omission.
    """

    path = _request_route_path(request)
    method = request.method
    if _is_exempt(path):
        return actor
    _reject_bearer_for_session_only_route(
        method=method,
        path=path,
        actor=actor,
    )
    if actor.token_kind == "session":
        return actor
    required = REST_REQUIRED_SCOPES.get((method, path))
    if required is None and method == "HEAD":
        required = REST_REQUIRED_SCOPES.get(("GET", path))
    if required is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "scope_denied",
                "message": (
                    f"REST endpoint not registered for bearer access: {method} {path}"
                ),
            },
        )
    return actor


def _reject_bearer_for_session_only_route(
    *,
    method: str,
    path: str,
    actor: ActorContext,
) -> None:
    """Reject bearer actors before resolving any target workspace.

    Target scoping may consult membership, workspace existence, or a workspace
    shard.  A route that accepts browser sessions only must not perform those
    reads for a bearer credential, both to keep the denial uniform and to
    avoid turning the route into a workspace-existence oracle.
    """

    if actor.token_kind == "session" or (method, path) not in REST_SESSION_ONLY_ROUTES:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "scope_denied",
            "message": f"REST endpoint requires a user session: {method} {path}",
        },
    )


def require_effective_token_scope(
    request: Request,
    actor: ActorContext,
) -> ActorContext:
    """Enforce this REST route against an already target-scoped actor.

    Most routes know ``workspace_id`` while dependencies run and reach this
    through :func:`require_token_scope`. Routes that resolve a workspace from
    a slug or page id inside their handler must authenticate first, resolve and
    narrow the actor, then call this same capability gate.
    """

    path = _request_route_path(request)
    method = request.method
    if _is_exempt(path):
        return actor
    _reject_bearer_for_session_only_route(
        method=method,
        path=path,
        actor=actor,
    )
    if (
        actor.token_kind == "session"
        and (
            method,
            path,
        )
        in REST_SESSION_UNSCOPED_ROUTES
    ):
        return actor
    if actor.token_kind == "session":
        return actor
    required = REST_REQUIRED_SCOPES.get((method, path))
    if required is None and method == "HEAD":
        required = REST_REQUIRED_SCOPES.get(("GET", path))
    if required is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "scope_denied",
                "message": (
                    f"REST endpoint not registered for bearer access: {method} {path}"
                ),
            },
        )
    if not has_required_scope(actor, required):
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "scope_denied",
                "message": (
                    f"insufficient token scope (required any of: {list(required)})"
                ),
            },
        )
    return actor


def scope_actor_to_request_workspace(
    request: Request,
    actor: ActorContext,
) -> ActorContext:
    """Return an actor whose role and token scope reflect the route target.

    D-01: a PAT or OAuth access token is bound to exactly one workspace at
    issuance -- only a session-cookie actor is built from the user's default
    workspace (backend/policy/auth.py `_default_workspace_access`).
    Workspace-scoped REST routes must not let a session's default role, or a
    token bound to a DIFFERENT workspace, leak into downstream services.
    OAuth authority is further narrowed to the target workspace's live
    consent (``actor_with_target_workspace_scope``); a PAT's authority is
    simply its own single bound workspace's exact token scope.
    """

    raw_workspace_id = request.path_params.get("workspace_id")
    if raw_workspace_id is None:
        return actor
    try:
        workspace_id = UUID(str(raw_workspace_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "invalid_request",
                "message": "workspace_id is invalid",
            },
        ) from exc

    try:
        scoped_actor = scope_actor_to_resolved_workspace(
            request,
            actor,
            workspace_id,
        )
        with _session_factory(request)() as session:
            access = require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role="viewer",
                # MCPClient is workspace-plane data; _session_factory above is
                # the control plane, so the disable check needs its own
                # session on the target workspace's own shard.
                workspace_session_factory=(
                    get_container_from_app(request).db.session_for(workspace_id)
                ),
            )
    except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
        # D-01v3-e/D-01v3-b: neither is a RejectedByPolicyError, precisely so
        # the broader-looking except just below (PermissionError,
        # RejectedByPolicyError) cannot silently fold either into a 403
        # "considered, stable decision" -- both mean "retry later", not
        # "denied". This is the primary workspace-scoped REST route family
        # (every {workspace_id}-addressed route registered in
        # REST_REQUIRED_SCOPES above); before this, a workspace-plane
        # failure or a not-yet-synced credential grant reaching this
        # function fell through uncaught to a generic 500 -- T0 wired
        # router.py's MCP path and deps.py's separate REST dependency, but
        # never this one (see this change's own report for the finding).
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": exc.error_code,
                "message": str(exc),
            },
        ) from exc
    except (PermissionError, RejectedByPolicyError) as exc:
        error_code = getattr(exc, "error_code", None)
        if not isinstance(error_code, str) or not error_code:
            error_code = str(exc).split(":", 1)[0] or "workspace_access_denied"
        with _session_factory(request)() as session:
            if workspace_existence_is_hidden(
                session,
                actor=actor,
                workspace_id=workspace_id,
            ):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": "not_found",
                        "message": "workspace not found",
                    },
                ) from exc
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": error_code,
                "message": str(exc) or "workspace access denied",
            },
        ) from exc
    scoped_actor = rebind_actor_to_workspace_access(scoped_actor, access)
    # The first capability check above sees only the control-plane grant.
    # require_workspace_role then intersects that actor with the replica that
    # actually landed on the target workspace plane. Re-check the exact REST
    # capability against the rebound actor so a stale-narrow replica cannot be
    # bypassed by a wider control grant.
    return require_effective_token_scope(request, scoped_actor)


def scope_actor_to_resolved_workspace(
    request: Request,
    actor: ActorContext,
    workspace_id: UUID,
) -> ActorContext:
    """Narrow a late-resolved REST actor to the target's exact capability.

    Slug-, page-, session-, and version-addressed routes learn their workspace
    only after dependency resolution. Keep their identity-plane lookup behind
    the REST policy boundary, then apply the same route-capability check used
    by ordinary ``{workspace_id}`` routes. Callers retain responsibility for
    resource-specific opaque error mapping.
    """

    with _session_factory(request)() as session:
        scoped_actor = actor_with_target_workspace_scope(
            actor,
            workspace_id,
            session=session,
        )
    return require_effective_token_scope(request, scoped_actor)


def _session_factory(request: Request):
    return get_session_factory_from_app(request)


def registered_routes() -> Iterable[tuple[str, str]]:
    """Return the (method, path) keys covered by the registry."""

    return REST_REQUIRED_SCOPES.keys()


def iter_app_bearer_routes(app: FastAPI) -> Iterable[tuple[str, str]]:
    """Yield (method, path) pairs for FastAPI routes that should require a scope.

    Excludes exempt prefixes, ``HEAD``/``OPTIONS`` (auto-generated by FastAPI),
    and routes without an explicit method set.
    """

    routes: list[object] = []
    for route in app.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            routes.extend(effective_route_contexts())
        else:
            routes.append(route)

    for route in routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1/"):
            continue
        if _is_exempt(path):
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            yield (method, path)
