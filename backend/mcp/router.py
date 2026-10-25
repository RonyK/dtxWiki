from dataclasses import dataclass, replace
from uuid import UUID

from backend.app.container import (
    DatabaseProvider,
    ServiceRegistry,
    ToolCallContext,
    WorkspaceDirectory,
)
from backend.mcp import (
    agent_tools,
    context_tools,
    maintenance_tools,
    read_tools,
    review_tools,
    upload_tools,
)
from backend.mcp.envelope import MCPErrorResponse, error_envelope
from backend.mcp.scopes import (
    TOOL_REQUIRED_ROLES,
    TOOL_REQUIRED_SCOPES,
    has_required_scope,
)
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspacePlaneUnavailableError,
    rebind_actor_to_workspace_access,
    require_workspace_role,
)
from backend.policy.trust_boundaries import actor_with_target_workspace_scope
from backend.services.context_links import PageLocatorNotFound
from backend.services.mcp_read_projection import (
    capture_served_reads,
    finish_served_read,
)
from backend.services.page_service import PageService
from backend.services.read_errors import ReadServiceError

TOOL_HANDLERS = {
    "list_my_workspaces": read_tools.list_my_workspaces,
    "guide": read_tools.guide,
    "search": read_tools.search,
    "fetch": read_tools.fetch,
    "grep_wiki": read_tools.grep_wiki,
    "what_changed": read_tools.what_changed,
    "read": read_tools.read,
    "get_index": read_tools.get_index,
    "read_concept": read_tools.read_concept,
    "list_changes": read_tools.list_changes,
    "list_agent_categories": read_tools.list_agent_categories,
    "read_agent_page": read_tools.read_agent_page,
    "create_context_page": context_tools.create_context_page,
    "begin_context_edit": context_tools.begin_context_edit,
    "update_context_edit": context_tools.update_context_edit,
    "submit_context_edit_for_review": context_tools.submit_context_edit_for_review,
    "discard_context_edit": context_tools.discard_context_edit,
    "patch_context_edit": context_tools.patch_context_edit,
    "list_my_context_edits": context_tools.list_my_context_edits,
    "upload_image": upload_tools.upload_image,
    "cite": context_tools.cite,
    "submit_feedback": context_tools.submit_feedback,
    "write_agent_page_draft": agent_tools.write_agent_page_draft,
    "build_agent_input_bundle": agent_tools.build_agent_input_bundle,
    "list_requested_changes": review_tools.list_requested_changes,
    "submit_revision_for_review": review_tools.submit_revision_for_review,
    "wiki_maintenance": maintenance_tools.wiki_maintenance,
    "read_context_outline": read_tools.read_context_outline,
    "read_structure_map": read_tools.read_structure_map,
}


# Tools whose payload may name a page_id without an explicit workspace_id
# (or name both, which must then agree) -- see resolve_workspace_id_for_tool's
# identifier consistency matrix docstring.
_PAGE_ID_CONSISTENCY_TOOLS = frozenset(
    {
        "upload_image",
        "begin_context_edit",
        "update_context_edit",
        "submit_context_edit_for_review",
        "discard_context_edit",
        "patch_context_edit",
    }
)


@dataclass(frozen=True)
class PolicyRejection:
    error_code: str


def dispatch(
    tool_name: str,
    payload: dict,
    actor: ActorContext | None = None,
    page_service: PageService | None = None,
    session_factory=None,
    conversation_token: str | None = None,
    search_service=None,
    tool_context: ToolCallContext | None = None,
):
    """Route a tool call through one uniform ``handler(payload, ctx)`` seam.

    Every handler receives the same frozen ``ToolCallContext``; the resolved
    conversation token (runner) and the per-app search service override the
    context's values when passed explicitly. Callers without a context (bare
    dispatch in contract tests) get one synthesized around their arguments.
    """

    if tool_context is None:
        tool_context = ToolCallContext(
            actor=actor,
            db=DatabaseProvider(
                None,
                session_factory=session_factory or page_service.session_factory,
            ),
            page_service=page_service,
            search_service=search_service,
            conversation_token=conversation_token,
        )
    else:
        overrides = {}
        if conversation_token is not None:
            overrides["conversation_token"] = conversation_token
        if search_service is not None:
            overrides["search_service"] = search_service
        if overrides:
            tool_context = replace(tool_context, **overrides)
    if tool_context.services is None:
        tool_context = replace(tool_context, services=ServiceRegistry())
    actor = tool_context.actor
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return MCPErrorResponse(envelope=error_envelope(actor, "unknown_tool"))
    scoped_actor = resolve_target_actor(
        tool_name=tool_name,
        payload=payload,
        actor=actor,
        session_factory=tool_context.session_factory,
        db=tool_context.db,
        directory=tool_context.services.workspace_directory,
    )
    if isinstance(scoped_actor, PolicyRejection):
        return MCPErrorResponse(envelope=error_envelope(actor, scoped_actor.error_code))
    if not has_required_scope(scoped_actor, TOOL_REQUIRED_SCOPES[tool_name]):
        return MCPErrorResponse(envelope=error_envelope(scoped_actor, "scope_denied"))
    # Rebind the context to the rescoped actor and the resolved target
    # workspace: ctx.session_factory now routes via db.session_for(...), so a
    # sharding-capable DatabaseProvider sees every workspace-scoped call.
    ctx = replace(
        tool_context,
        actor=scoped_actor,
        workspace_id=(
            scoped_actor.workspace_id
            if TOOL_REQUIRED_ROLES[tool_name] is not None
            else None
        ),
    )
    with capture_served_reads() as snapshots:
        try:
            normalized_payload = _normalize_page_inputs(tool_name, payload, ctx)
        except ReadServiceError as exc:
            return MCPErrorResponse(envelope=error_envelope(actor, exc.error_code))
        except PageLocatorNotFound:
            return MCPErrorResponse(envelope=error_envelope(actor, "not_found"))
        except ValueError:
            return MCPErrorResponse(envelope=error_envelope(actor, "validation_failed"))
        response = handler(normalized_payload, ctx)
    # An internal attribute deliberately bypasses the wire schema. The runner
    # passes it separately to trace persistence; model_dump never includes it.
    object.__setattr__(
        response,
        "_served_read_projection",
        finish_served_read(tool_name, payload, snapshots),
    )
    return response


def _normalize_page_inputs(tool_name: str, payload: dict, ctx: ToolCallContext) -> dict:
    from backend.services.context_links import (
        LinkContext,
        parse_page_input,
        resolve_page_input,
    )

    result = dict(payload)
    field = None
    if tool_name in _PAGE_ID_CONSISTENCY_TOOLS:
        field = "page_id"
    elif tool_name == "cite":
        field = "target_page_id"
    elif tool_name == "fetch" and payload.get("type") == "context_page":
        field = "id"
    elif (
        tool_name in {"read", "list_changes", "submit_feedback"}
        and payload.get("target_type") == "context_page"
    ):
        field = "target_id"
    if field is not None and payload.get(field) is not None:
        locator = parse_page_input(payload[field])
        if (
            locator.workspace_id is not None
            and locator.workspace_id != ctx.workspace_id
        ):
            raise PageLocatorNotFound("Context page not found")
        if locator.page_id is not None:
            result[field] = locator.page_id
        else:
            with ctx.session_factory() as session:
                result[field] = resolve_page_input(
                    payload[field], LinkContext(session, ctx.workspace_id, ctx.actor)
                )
    return result


def resolve_target_actor(
    *,
    tool_name: str,
    payload: dict,
    actor: ActorContext,
    session_factory,
    db: DatabaseProvider,
    directory: WorkspaceDirectory | None = None,
) -> ActorContext | PolicyRejection:
    required_role = TOOL_REQUIRED_ROLES[tool_name]
    if required_role is None:
        return actor
    resolved = resolve_workspace_id_for_tool(
        tool_name=tool_name,
        payload=payload,
        session_factory=session_factory,
        directory=directory,
    )
    if isinstance(resolved, PolicyRejection):
        return resolved
    workspace_id = resolved
    if workspace_id is None:
        return actor
    with session_factory() as session:
        try:
            scoped_actor = actor_with_target_workspace_scope(
                actor,
                workspace_id,
                session=session,
            )
            if not has_required_scope(
                scoped_actor,
                TOOL_REQUIRED_SCOPES[tool_name],
            ):
                return PolicyRejection("scope_denied")
            access = require_workspace_role(
                session=session,
                actor=scoped_actor,
                workspace_id=workspace_id,
                required_role=required_role,
                # MCPClient (the disable check) is workspace-plane data --
                # session_factory above is the control plane, so the
                # disabled-connection check needs its own session on the
                # target workspace's own shard, not the identity-plane one
                # the role/membership check just used.
                workspace_session_factory=db.session_for(workspace_id),
            )
        except (WorkspacePlaneUnavailableError, GrantReplicaMissingError) as exc:
            return PolicyRejection(exc.error_code)
        except (PermissionError, ReadServiceError) as exc:
            if isinstance(exc, PermissionError):
                return PolicyRejection(
                    str(exc).split(":", 1)[0] or "workspace_access_denied"
                )
            return PolicyRejection(exc.error_code)
    return rebind_actor_to_workspace_access(scoped_actor, access)


def resolve_workspace_id_for_tool(
    *,
    tool_name: str,
    payload: dict,
    session_factory,
    directory: WorkspaceDirectory | None = None,
) -> UUID | PolicyRejection | None:
    """Resolve the target workspace_id for the given MCP tool payload.

    Fix-6 Task 1.1 (B-F5-CODEX-1): when the payload mixes identifiers
    pointing to different workspaces, this function returns a
    PolicyRejection("inconsistent_identifier") so the dispatcher can
    deny the call before any handler reads cross-workspace data.

    Identifier consistency matrix (upload_image, and the P8.6 page_id-bearing
    Context-editing tools: begin_context_edit, update_context_edit,
    submit_context_edit_for_review, discard_context_edit, patch_context_edit):
    - workspace_id only          -> use workspace_id
    - workspace_id + page_id     -> page must belong to workspace_id, else reject
    - workspace_slug only        -> resolve by slug
    - page_id only               -> resolve from page's owning workspace
    - workspace_slug + page_id   -> both must resolve to same workspace, else reject
    """

    explicit_workspace_id: UUID | None = None
    if "workspace_id" in payload:
        try:
            explicit_workspace_id = UUID(str(payload["workspace_id"]))
        except ValueError:
            return None

    page_id: UUID | None = None
    if tool_name in _PAGE_ID_CONSISTENCY_TOOLS and "page_id" in payload:
        raw_page_id = payload.get("page_id")
        if raw_page_id is not None:
            try:
                from backend.services.context_links import parse_page_input
                locator = parse_page_input(raw_page_id)
                page_id = locator.page_id
                if (
                    locator.workspace_id is not None
                    and explicit_workspace_id is not None
                    and locator.workspace_id != explicit_workspace_id
                ):
                    return PolicyRejection("not_found")
            except ValueError:
                return None

    workspace_slug = (
        payload.get("workspace_slug")
        if isinstance(payload.get("workspace_slug"), str)
        else None
    )

    resolved_directory = directory or WorkspaceDirectory()
    with session_factory() as session:
        slug_workspace_id: UUID | None = None
        if workspace_slug is not None:
            slug_workspace_id = resolved_directory.workspace_id_by_slug(
                session, workspace_slug
            )

        page_workspace_id: UUID | None = None
        if page_id is not None:
            page_workspace_id = resolved_directory.workspace_id_by_context_page(
                session, page_id
            )

    # A page mismatch is indistinguishable from a missing page. Only two
    # conflicting workspace locators remain a malformed identifier pair.
    if (
        explicit_workspace_id is not None and slug_workspace_id is not None
        and explicit_workspace_id != slug_workspace_id
    ):
        return PolicyRejection("inconsistent_identifier")
    target_workspace_id = explicit_workspace_id or slug_workspace_id
    if (
        target_workspace_id is not None and page_workspace_id is not None
        and target_workspace_id != page_workspace_id
    ):
        return PolicyRejection("not_found")

    # Priority: explicit workspace_id > page_id > workspace_slug
    if explicit_workspace_id is not None:
        return explicit_workspace_id
    if page_workspace_id is not None:
        return page_workspace_id
    if slug_workspace_id is not None:
        return slug_workspace_id
    return None
