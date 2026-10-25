from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import urlparse
from uuid import UUID

from mcp import types
from mcp.server.lowlevel.server import ReadResourceContents
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData
from pydantic import AnyUrl

from backend.app.container import DatabaseProvider, ToolCallContext
from backend.mcp.protocol.lifecycle import (
    enforce_actor_mcp_surface_lifecycle,
    enforce_mcp_surface_lifecycle,
)
from backend.mcp.scopes import (
    AGENT_PAGE_BODY_SCOPES,
    CONTEXT_PAGE_BODY_SCOPES,
    has_required_scope,
)
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    GrantReplicaMissingError,
    WorkspaceAccess,
    WorkspacePlaneUnavailableError,
    get_workspace_access,
    scope_actor_to_workspace_grant,
)
from backend.policy.trust_boundaries import (
    actor_can_read_sensitive_agent_pages,
    actor_with_target_workspace_scope,
    check_actor_can_access,
)
from backend.services import mcp_surface_service as surface
from backend.services import okf
from backend.services.entity_ref import parse_context_resource_uri
from backend.services.mcp_surface_service import McpSurfaceNotFoundError
from backend.services.workspace_read import WorkspaceReadService
from backend.utils.uuid import parse_uuid_or_none

PROMPT_AGENT_PAGE_CATEGORIES = frozenset({"persona", "agent_guide"})
# M-05: the prompts/resources LIST advertises a bounded number of per-page
# entry points; beyond this the category rollups and the agent-page slug
# template keep every page reachable without a flat-catalog explosion.
MCP_AGENT_PAGE_LIST_LIMIT = 100
AGENT_PAGE_PROMPT_NAME = "dtxwiki.agent_page"
AGENT_PAGE_PROMPT_PREFIX = "dtxwiki.agent_page."
CONTEXT_WORKFLOW_PROMPT_NAME = "dtxwiki.context_workflow"
RESOURCE_SCHEME = "dtxwiki"
RESOURCE_AUTHORITY = "workspaces"
LEGACY_RESOURCE_AUTHORITY = "workspace"
SENSITIVE_RESOURCE_ERROR = "workspace_role_insufficient:sensitive_resource"
PROMPT_RESOURCE_REQUIRED_SCOPES = ("guide", "workspace-read", "read", "agent-page-read")


def list_dtxwiki_prompts(
    *,
    actor: ActorContext,
    session_factory,
    tool_context: ToolCallContext | None = None,
) -> list[types.Prompt]:
    actor, db = _resolve_bindings(actor, tool_context)
    was_workspace_unbound = actor.workspace_id is None
    # LIST is actor-scoped and can return generic descriptors even when the
    # actor's default Workspace is outside an OAuth token claim. Enforce every
    # live token-bound lifecycle before either that early return or scope
    # filtering, otherwise a disabled credential can still use prompts/list.
    actor = enforce_actor_mcp_surface_lifecycle(
        actor=actor,
        fallback_session_factory=session_factory,
        db=db,
    )
    prompts = [
        types.Prompt(
            name=CONTEXT_WORKFLOW_PROMPT_NAME,
            title="dtxWiki context workflow",
            description=(
                "Use dtxWiki's source-grounded Context workflow: guide, search, "
                "fetch the chosen result, cite, then create_context_page or "
                "begin_context_edit/update_context_edit/patch_context_edit when "
                "changes are needed."
            ),
            arguments=[
                types.PromptArgument(
                    name="workspace_id",
                    description="Workspace UUID.",
                    required=True,
                )
            ],
        ),
        types.Prompt(
            name=AGENT_PAGE_PROMPT_NAME,
            title="dtxWiki AgentPage prompt",
            description=(
                "Read an active AgentPage as prompt text. system_prompt and "
                "rule_policy categories require owner access."
            ),
            arguments=[
                types.PromptArgument(
                    name="workspace_id",
                    description="Workspace UUID.",
                    required=True,
                ),
                types.PromptArgument(
                    name="agent_page_id",
                    description="AgentPage UUID. Use this or slug.",
                    required=False,
                ),
                types.PromptArgument(
                    name="slug",
                    description="AgentPage slug. Use this or agent_page_id.",
                    required=False,
                ),
            ],
        ),
    ]
    workspace_id = actor.workspace_id
    if workspace_id is None:
        return prompts if _has_prompt_resource_scope(actor) else []
    control_factory = _control_session_factory(db, session_factory)
    workspace_factory = _workspace_session_factory(
        db,
        session_factory,
        workspace_id,
    )
    with control_factory() as control_session:
        try:
            scoped_actor, _access = _require_viewer(
                control_session,
                actor,
                workspace_id,
                workspace_session_factory=workspace_factory,
            )
        except McpError:
            return prompts if _has_prompt_resource_scope(actor) else []
    if not _has_prompt_resource_scope(scoped_actor):
        return []
    if was_workspace_unbound:
        # The static prompt descriptors require an explicit workspace. Do not
        # advertise per-page names selected from a lifecycle fallback workspace.
        return prompts
    enforce_mcp_surface_lifecycle(
        actor=scoped_actor,
        session_factory=workspace_factory,
        workspace_id=workspace_id,
    )
    with workspace_factory() as session:
        page_refs = surface.list_agent_page_refs(
            session,
            workspace_id,
            categories=PROMPT_AGENT_PAGE_CATEGORIES,
            actor=scoped_actor,
            limit=MCP_AGENT_PAGE_LIST_LIMIT,
        )
        for page in page_refs:
            prompts.append(
                types.Prompt(
                    name=f"{AGENT_PAGE_PROMPT_PREFIX}{page.slug}",
                    title=page.title,
                    description=f"Active AgentPage prompt ({page.category}).",
                    arguments=[
                        types.PromptArgument(
                            name="workspace_id",
                            description="Workspace UUID.",
                            required=True,
                        )
                    ],
                )
            )
    return prompts


def get_dtxwiki_prompt(
    *,
    name: str,
    arguments: dict[str, str] | None,
    actor: ActorContext,
    session_factory,
    tool_context: ToolCallContext | None = None,
) -> types.GetPromptResult:
    actor, db = _resolve_bindings(actor, tool_context)
    args = arguments or {}
    if name == CONTEXT_WORKFLOW_PROMPT_NAME:
        workspace_id = _required_workspace_id(args)
        control_factory = _control_session_factory(db, session_factory)
        workspace_factory = _workspace_session_factory(
            db,
            session_factory,
            workspace_id,
        )
        with control_factory() as control_session:
            scoped_actor, _access = _require_viewer(
                control_session,
                actor,
                workspace_id,
                workspace_session_factory=workspace_factory,
            )
            if not _has_prompt_resource_scope(scoped_actor):
                raise _invalid_params("scope_denied")
            # Workspace identity/title is control-plane data (E-2/D-E1): read
            # it from the control session even though the rest of this
            # prompt's content is selected from the workspace plane below --
            # a workspace-plane DB may mirror a stale/different title.
            workspace_label = (
                surface.workspace_title(control_session, workspace_id)
                or "workspace"
            )
        enforce_mcp_surface_lifecycle(
            actor=scoped_actor,
            session_factory=workspace_factory,
            workspace_id=workspace_id,
        )
        return types.GetPromptResult(
            description="dtxWiki context workflow",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=(
                            f"Work inside dtxWiki workspace {workspace_label}. "
                            "Call guide first when policy is unclear. Use keyword "
                            "search then fetch by id/type for grounding; use "
                            "read/list_changes for direct reads, and cite before "
                            "drafting factual changes. Use create_context_page for "
                            "a new page, or begin_context_edit then "
                            "update_context_edit/patch_context_edit for an existing "
                            "one, followed by submit_context_edit_for_review -- "
                            "never publish directly. A new "
                            "image must be uploaded with upload_image first. Store "
                            "data.markdown or data.logical_ref, never data.content_url "
                            "or another /api/v1 transport URL. upload_image always "
                            "requires an existing page_id and your own open "
                            "editing_version_id -- for a new page, create it first, "
                            "then upload into it and call update_context_edit. Never "
                            "fall back to a data: "
                            "URI. A new "
                            "page with no placement goes to Inbox; to choose a "
                            "location, read guide.placement_parent_candidates and "
                            "pass its node_id as placement_parent_node_id. "
                            "Read and discovery tools accept conversation_token "
                            "optionally. Content tools (writes, citations, feedback, "
                            "bundles, and maintenance) require it: echo "
                            "envelope.conversation_token from an earlier call or "
                            'pass the literal "new" to start a conversation.'
                        ),
                    ),
                )
            ],
        )
    page_slug_from_name = None
    if name.startswith(AGENT_PAGE_PROMPT_PREFIX):
        page_slug_from_name = name.removeprefix(AGENT_PAGE_PROMPT_PREFIX)
    elif name != AGENT_PAGE_PROMPT_NAME:
        raise ValueError("unknown prompt")

    workspace_id = _required_workspace_id(args)
    agent_page_id = _optional_uuid(args, "agent_page_id")
    slug = page_slug_from_name or args.get("slug")
    control_factory = _control_session_factory(db, session_factory)
    workspace_factory = _workspace_session_factory(
        db,
        session_factory,
        workspace_id,
    )
    with control_factory() as control_session:
        scoped_actor, _access = _require_viewer(
            control_session,
            actor,
            workspace_id,
            workspace_session_factory=workspace_factory,
        )
        if not _has_prompt_resource_scope(scoped_actor):
            raise _invalid_params("scope_denied")
    enforce_mcp_surface_lifecycle(
        actor=scoped_actor,
        session_factory=workspace_factory,
        workspace_id=workspace_id,
    )
    with workspace_factory() as session:
        _require_body_scope(scoped_actor, AGENT_PAGE_BODY_SCOPES)
        try:
            meta = surface.resolve_agent_page_meta(
                session,
                workspace_id,
                agent_page_id=agent_page_id,
                slug=slug,
            )
        except McpSurfaceNotFoundError as exc:
            raise _invalid_params(exc.error_code) from exc
        if meta.category not in PROMPT_AGENT_PAGE_CATEGORIES:
            raise _invalid_params("agent_page_prompt_not_supported")
        if meta.requires_sensitive_access and not actor_can_read_sensitive_agent_pages(
            scoped_actor
        ):
            raise _invalid_params(SENSITIVE_RESOURCE_ERROR)
        # Load the body only after the category/owner gates pass (B#8).
        body = surface.load_agent_page_body(session, meta.version_id) or ""
        title = meta.title
        category = meta.category

    return types.GetPromptResult(
        description=f"{title} ({category})",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=body),
            )
        ],
    )


def list_dtxwiki_resources(
    *,
    actor: ActorContext,
    session_factory,
    tool_context: ToolCallContext | None = None,
) -> list[types.Resource]:
    actor, db = _resolve_bindings(actor, tool_context)
    was_workspace_unbound = actor.workspace_id is None
    actor = enforce_actor_mcp_surface_lifecycle(
        actor=actor,
        fallback_session_factory=session_factory,
        db=db,
    )
    if was_workspace_unbound:
        return []
    workspace_id = actor.workspace_id
    if workspace_id is None:
        return []
    control_factory = _control_session_factory(db, session_factory)
    workspace_factory = _workspace_session_factory(
        db,
        session_factory,
        workspace_id,
    )
    with control_factory() as control_session:
        try:
            scoped_actor, _access = _require_viewer(
                control_session,
                actor,
                workspace_id,
                workspace_session_factory=workspace_factory,
            )
        except McpError:
            return []
    if not _has_prompt_resource_scope(scoped_actor):
        return []
    enforce_mcp_surface_lifecycle(
        actor=scoped_actor,
        session_factory=workspace_factory,
        workspace_id=workspace_id,
    )
    with workspace_factory() as session:
        resources = [
            types.Resource(
                name="workspace-guide",
                title="Workspace guide",
                uri=_guide_resource_uri(workspace_id),
                description=(
                    "Read-only guide for workflow, tools, prompts, and resources."
                ),
                mimeType="application/json",
                annotations=types.Annotations(audience=["assistant"]),
            ),
            types.Resource(
                name="agent-injection-policy",
                title="Agent injection policy",
                uri=_agent_policy_resource_uri(workspace_id),
                description="Read-only AgentPage inclusion policy for this workspace.",
                mimeType="application/json",
                annotations=types.Annotations(audience=["assistant"]),
            ),
        ]
        pages = surface.list_agent_page_refs(
            session, workspace_id, actor=scoped_actor, limit=MCP_AGENT_PAGE_LIST_LIMIT
        )
        advertised_categories: set[str] = set()
        for page in pages:
            advertised_categories.add(page.category)
            resources.append(
                types.Resource(
                    name=f"agent-page:{page.slug}",
                    title=page.title,
                    uri=_agent_page_resource_uri(workspace_id, page.slug),
                    description=f"Active AgentPage resource ({page.category}).",
                    mimeType="text/markdown",
                    annotations=types.Annotations(audience=["assistant"]),
                    _meta={"category": page.category},
                )
            )
        for category in sorted(advertised_categories):
            resources.append(
                types.Resource(
                    name=f"agent-pages:{category}",
                    title=f"AgentPages: {category}",
                    uri=_agent_page_category_resource_uri(workspace_id, category),
                    description=f"Active AgentPage resources in {category}.",
                    mimeType="text/markdown",
                    annotations=types.Annotations(audience=["assistant"]),
                    _meta={"category": category},
                )
            )
        # E-2: entry points only — per-context-page advertising is dropped
        # from the LIST (direct context-page URIs still read fine); large
        # workspaces must not re-create the flat-catalog explosion here.
        # The LIST also must not pay a whole-bundle render per call: it
        # advertises lastModified from one aggregate query and clients get
        # the authoritative content_hash from the READ meta.
        last_modified = okf.workspace_published_last_modified(session, workspace_id)
        bundle_annotations = types.Annotations(audience=["assistant"])
        if last_modified is not None:
            # mcp Annotations is extra="allow": lastModified rides along per
            # the 2025-11-25 stable spec without an SDK upgrade.
            bundle_annotations = types.Annotations(
                audience=["assistant"],
                lastModified=last_modified.isoformat(),
            )
        resources.append(
            types.Resource(
                name="workspace-index",
                title="Workspace index",
                uri=_workspace_index_resource_uri(workspace_id),
                description=(
                    "Navigational index.md of visible concepts (flat for "
                    "small workspaces, routing summary for large ones)."
                ),
                mimeType="text/markdown",
                annotations=bundle_annotations,
            )
        )
        resources.append(
            types.Resource(
                name="workspace-bundle",
                title="Workspace bundle",
                uri=_workspace_bundle_resource_uri(workspace_id),
                description=(
                    "Whole-workspace OKF bundle (CAG): index.md plus every "
                    "visible concept. Cache by the READ meta content_hash; "
                    "invalidate via what_changed or a newer lastModified."
                ),
                mimeType="text/markdown",
                annotations=bundle_annotations,
            )
        )
        return resources


def list_dtxwiki_resource_templates(
    *,
    actor: ActorContext,
    session_factory,
    tool_context: ToolCallContext | None = None,
) -> list[types.ResourceTemplate]:
    """Advertise direct Context reads without flattening every page into list."""

    actor, db = _resolve_bindings(actor, tool_context)
    was_workspace_unbound = actor.workspace_id is None
    actor = enforce_actor_mcp_surface_lifecycle(
        actor=actor,
        fallback_session_factory=session_factory,
        db=db,
    )
    if was_workspace_unbound:
        if not actor.token_workspace_ids or not _has_prompt_resource_scope(actor):
            return []
        return [
            types.ResourceTemplate(
                name="context-page",
                title="Context page",
                uriTemplate=(
                    f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{{workspace_id}}"
                    "/context/pages/{slug}"
                ),
                description=(
                    "Read one actor-visible Context page by slug. Set workspace_id "
                    "to an authorized workspace UUID."
                ),
                mimeType="text/markdown",
                annotations=types.Annotations(audience=["assistant"]),
            ),
            types.ResourceTemplate(
                name="agent-page",
                title="Agent page",
                uriTemplate=(
                    f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{{workspace_id}}"
                    "/agent/pages/{slug}"
                ),
                description=(
                    "Read one active AgentPage by slug. Set workspace_id to an "
                    "authorized workspace UUID."
                ),
                mimeType="text/markdown",
                annotations=types.Annotations(audience=["assistant"]),
            ),
        ]
    if actor.workspace_id is None:
        return []
    workspace_id = actor.workspace_id
    control_factory = _control_session_factory(db, session_factory)
    workspace_factory = _workspace_session_factory(
        db,
        session_factory,
        workspace_id,
    )
    with control_factory() as control_session:
        try:
            scoped_actor, _access = _require_viewer(
                control_session,
                actor,
                workspace_id,
                workspace_session_factory=workspace_factory,
            )
        except McpError:
            return []
    if not _has_prompt_resource_scope(scoped_actor):
        return []
    enforce_mcp_surface_lifecycle(
        actor=scoped_actor,
        session_factory=workspace_factory,
        workspace_id=workspace_id,
    )
    return [
        types.ResourceTemplate(
            name="context-page",
            title="Context page",
            uriTemplate=(
                f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}"
                "/context/pages/{slug}"
            ),
            description=(
                "Read one actor-visible Context page by slug. Use search or the "
                "workspace index to discover the slug."
            ),
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"]),
        ),
        types.ResourceTemplate(
            name="agent-page",
            title="Agent page",
            uriTemplate=(
                f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}"
                "/agent/pages/{slug}"
            ),
            description=(
                "Read one active AgentPage by slug. The resources LIST advertises "
                "a bounded set of pages plus per-category rollups; use this "
                "template or a category rollup to reach any page beyond the cap."
            ),
            mimeType="text/markdown",
            annotations=types.Annotations(audience=["assistant"]),
        ),
    ]


def read_dtxwiki_resource(
    *,
    uri: AnyUrl,
    actor: ActorContext,
    session_factory,
    tool_context: ToolCallContext | None = None,
) -> list[ReadResourceContents]:
    actor, db = _resolve_bindings(actor, tool_context)
    workspace_id, resource_kind, resource_id = _parse_resource_uri(str(uri))
    control_factory = _control_session_factory(db, session_factory)
    workspace_factory = _workspace_session_factory(
        db,
        session_factory,
        workspace_id,
    )
    with control_factory() as control_session:
        scoped_actor, _access = _require_viewer(
            control_session,
            actor,
            workspace_id,
            workspace_session_factory=workspace_factory,
        )
        if not _has_prompt_resource_scope(scoped_actor):
            raise _invalid_params("scope_denied")
    enforce_mcp_surface_lifecycle(
        actor=scoped_actor,
        session_factory=workspace_factory,
        workspace_id=workspace_id,
    )
    with workspace_factory() as session:
        if resource_kind in ("index", "bundle"):
            # Same gate as reading a page body through the read/get_index
            # tools or a context-page URI: a guide-only token must not pull
            # every visible body (or the index of titles) through this seam.
            _require_body_scope(scoped_actor, CONTEXT_PAGE_BODY_SCOPES)
            workspace_slug = okf.resolve_workspace_slug(session, workspace_id)
            if workspace_slug is None:
                raise _invalid_params("not_found")
            if resource_kind == "index":
                content = okf.build_workspace_index(
                    session,
                    workspace_id,
                    actor=scoped_actor,
                    workspace_slug=workspace_slug,
                )
                import hashlib

                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                last_modified = None
            else:
                content, content_hash, last_modified_at = (
                    okf.render_workspace_bundle_markdown(
                        session,
                        workspace_id,
                        actor=scoped_actor,
                        workspace_slug=workspace_slug,
                    )
                )
                last_modified = (
                    last_modified_at.isoformat() if last_modified_at else None
                )
            meta = {
                "workspace_id": str(workspace_id),
                "resource": resource_kind,
                "content_hash": content_hash,
            }
            if last_modified is not None:
                meta["last_modified"] = last_modified
            return [
                ReadResourceContents(
                    content=content,
                    mime_type="text/markdown",
                    meta=meta,
                )
            ]
        if resource_kind == "guide":
            payload = WorkspaceReadService().guide(
                session,
                workspace_id,
                detail_level="full",
                actor=scoped_actor,
            )
            return [
                ReadResourceContents(
                    content=payload.model_dump_json(indent=2),
                    mime_type="application/json",
                    meta={"workspace_id": str(workspace_id), "resource": "guide"},
                )
            ]
        if resource_kind == "agent-injection-policy":
            policy = surface.workspace_agent_injection_policy(session, workspace_id)
            if policy is None:
                raise ValueError("resource not found")
            return [
                ReadResourceContents(
                    content=json.dumps(
                        policy,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    mime_type="application/json",
                    meta={
                        "workspace_id": str(workspace_id),
                        "resource": "agent-injection-policy",
                    },
                )
            ]
        if resource_kind == "context-pages":
            if resource_id is None:
                raise ValueError("resource not found")
            _require_body_scope(scoped_actor, CONTEXT_PAGE_BODY_SCOPES)
            context = surface.read_context_page_content(
                session,
                workspace_id,
                resource_id,
                scoped_actor,
            )
            if context is None:
                raise ValueError("resource not found")
            import hashlib

            return [
                ReadResourceContents(
                    content=context.body,
                    mime_type="text/markdown",
                    meta={
                        "workspace_id": str(workspace_id),
                        "context_page_id": str(context.page_id),
                        "page_type": context.page_type,
                        "version_id": str(context.version_id),
                        # per-page cache keys, same contract as the bundle
                        "content_hash": hashlib.sha256(
                            context.body.encode("utf-8")
                        ).hexdigest(),
                    },
                )
            ]
        if resource_kind == "agent-pages-by-category":
            if resource_id is None:
                raise ValueError("resource not found")
            category = resource_id
            _require_body_scope(scoped_actor, AGENT_PAGE_BODY_SCOPES)
            contents = surface.read_agent_pages_by_category(
                session,
                workspace_id,
                category,
                actor=scoped_actor,
            )
            entries: list[str] = []
            page_ids: list[str] = []
            version_ids: list[str] = []
            for content in contents:
                page_ids.append(str(content.page_id))
                version_ids.append(str(content.version_id))
                entries.append(f"# {content.title}\n\n{content.body}")
            if not entries:
                raise ValueError("resource not found")
            return [
                ReadResourceContents(
                    content="\n\n---\n\n".join(entries),
                    mime_type="text/markdown",
                    meta={
                        "workspace_id": str(workspace_id),
                        "category": category,
                        "agent_page_ids": page_ids,
                        "version_ids": version_ids,
                    },
                )
            ]
        if resource_kind != "agent-pages" or resource_id is None:
            raise ValueError("unsupported resource uri")
        _require_body_scope(scoped_actor, AGENT_PAGE_BODY_SCOPES)
        maybe_id = parse_uuid_or_none(resource_id)
        try:
            meta = surface.resolve_agent_page_meta(
                session,
                workspace_id,
                agent_page_id=maybe_id,
                slug=None if maybe_id is not None else resource_id,
            )
        except McpSurfaceNotFoundError as exc:
            raise _invalid_params(exc.error_code) from exc
        if meta.requires_sensitive_access and not actor_can_read_sensitive_agent_pages(
            scoped_actor
        ):
            raise _invalid_params(SENSITIVE_RESOURCE_ERROR)
        # Load the body only after the sensitive-category owner gate (B#8).
        body = surface.load_agent_page_body(session, meta.version_id)
        if meta.version_id is None or body is None:
            raise ValueError("resource not found")
        return [
            ReadResourceContents(
                content=body,
                mime_type="text/markdown",
                meta={
                    "workspace_id": str(workspace_id),
                    "agent_page_id": str(meta.page_id),
                    "category": meta.category,
                    "version_id": str(meta.version_id),
                },
            )
        ]


def _agent_page_resource_uri(workspace_id: UUID, slug: str) -> str:
    return f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}/agent/pages/{slug}"


def _agent_page_category_resource_uri(workspace_id: UUID, category: str) -> str:
    return (
        f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}"
        f"/agent/pages/by-category/{category}"
    )


def _workspace_index_resource_uri(workspace_id: UUID) -> str:
    return f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}/index"


def _workspace_bundle_resource_uri(workspace_id: UUID) -> str:
    return f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}/bundle"


def _guide_resource_uri(workspace_id: UUID) -> str:
    return f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}/guide"


def _agent_policy_resource_uri(workspace_id: UUID) -> str:
    return f"{RESOURCE_SCHEME}://{RESOURCE_AUTHORITY}/{workspace_id}/agent-injection-policy"


def _parse_resource_uri(raw_uri: str) -> tuple[UUID, str, str | None]:
    # Context-page URIs go through the shared resolver first (G0-2) so
    # read_concept/search/trace/resources agree on one grammar.
    context_ref = parse_context_resource_uri(raw_uri)
    if context_ref is not None:
        workspace_id, slug = context_ref
        return workspace_id, "context-pages", slug
    parsed = urlparse(raw_uri)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != RESOURCE_SCHEME or parsed.netloc not in {
        RESOURCE_AUTHORITY,
        LEGACY_RESOURCE_AUTHORITY,
    }:
        raise ValueError("unsupported resource uri")
    if len(parts) == 2 and parts[1] in {
        "guide",
        "agent-injection-policy",
        "index",
        "bundle",
    }:
        return UUID(parts[0]), parts[1], None
    if (
        len(parts) == 5
        and parts[1] == "agent"
        and parts[2] == "pages"
        and parts[3] == "by-category"
    ):
        return UUID(parts[0]), "agent-pages-by-category", parts[4]
    if len(parts) == 4 and parts[1] == "agent" and parts[2] == "pages":
        return UUID(parts[0]), "agent-pages", parts[3]
    if len(parts) == 3 and parts[1] == "context-pages":
        return UUID(parts[0]), parts[1], parts[2]
    raise ValueError("unsupported resource uri")


def _require_viewer(
    session,
    actor: ActorContext,
    workspace_id: UUID,
    *,
    workspace_session_factory,
) -> tuple[ActorContext, WorkspaceAccess]:
    try:
        check_actor_can_access(
            actor,
            workspace_id,
            "read",
            session=session,
        )
    except PermissionError as exc:
        error_code = str(exc).split(":", 1)[0] or "workspace_membership_missing"
        raise _invalid_params(error_code) from exc
    if actor.user_id is None:
        access = WorkspaceAccess(workspace_id=workspace_id, role="owner")
    else:
        access = get_workspace_access(
            session,
            user_id=actor.user_id,
            workspace_id=workspace_id,
        )
        if access is None:  # pragma: no cover - guarded by trust-boundary check
            raise _invalid_params("workspace_membership_missing")
    try:
        scoped_actor = _scope_actor_to_workspace(actor, access, session=session)
    except PermissionError as exc:
        error_code = str(exc).split(":", 1)[0] or "workspace_access_denied"
        raise _invalid_params(error_code) from exc
    try:
        scoped_actor = scope_actor_to_workspace_grant(
            actor=scoped_actor,
            workspace_id=workspace_id,
            workspace_session_factory=workspace_session_factory,
        )
    except (GrantReplicaMissingError, WorkspacePlaneUnavailableError) as exc:
        raise _invalid_params(exc.error_code) from exc
    return scoped_actor, access


def _scope_actor_to_workspace(actor: ActorContext, access, *, session) -> ActorContext:
    scoped_actor = actor_with_target_workspace_scope(
        actor,
        access.workspace_id,
        session=session,
    )
    return replace(
        scoped_actor,
        workspace_id=access.workspace_id,
        workspace_role=access.role,
        role=access.role,
    )


def _optional_uuid(arguments: dict[str, str], key: str) -> UUID | None:
    value = arguments.get(key)
    return UUID(value) if value else None


def _required_workspace_id(
    arguments: dict[str, str],
) -> UUID:
    value = arguments.get("workspace_id")
    if not value:
        raise _invalid_params("validation_failed")
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise _invalid_params("validation_failed") from exc


def _invalid_params(error_code: str) -> McpError:
    return McpError(
        ErrorData(
            code=INVALID_PARAMS,
            message=error_code,
            data={"error_code": error_code},
        )
    )


def _resolve_bindings(
    actor: ActorContext,
    tool_context: ToolCallContext | None,
) -> tuple[ActorContext, DatabaseProvider | None]:
    if tool_context is None:
        return actor, None
    return tool_context.actor, tool_context.db


def _workspace_session_factory(
    db: DatabaseProvider | None,
    fallback_session_factory,
    workspace_id: UUID,
):
    if db is None:
        return fallback_session_factory
    return db.session_for(workspace_id)


def _control_session_factory(
    db: DatabaseProvider | None,
    fallback_session_factory,
):
    if db is None:
        return fallback_session_factory
    return db.control_session_factory()


def _has_prompt_resource_scope(actor: ActorContext) -> bool:
    if actor.token_kind is None:
        return True
    return has_required_scope(actor, PROMPT_RESOURCE_REQUIRED_SCOPES)


# Per-resource-kind scope gates mirror TOOL_REQUIRED_SCOPES so prompts/resources
# cannot be used to read a body a token-scoped client could not read through the
# equivalent narrower tool: AgentPage bodies require agent-page-read (as
# read_agent_page does); Context page bodies require read/context-read (as
# read does). Human/session actors are gated by workspace role instead.
# MCP-02: promoted to backend/mcp/scopes.py so domain_read.fetch_target's
# agent_page branch can share the exact same gate (imported here for
# backward-compat re-export -- this module's call sites are unchanged).


def _require_body_scope(actor: ActorContext, required_scopes: tuple[str, ...]) -> None:
    if actor.token_kind is None:
        return
    if not has_required_scope(actor, required_scopes):
        raise _invalid_params("scope_denied")
