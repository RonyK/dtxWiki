from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.policy.rbac import list_user_workspaces
from backend.policy.trust_boundaries import actor_workspace_filter
from backend.repositories.context_page_repo import visible_page_body_totals
from backend.schemas.read import (
    GuidePayload,
    WorkspaceDetail,
    WorkspaceListData,
    WorkspaceRouting,
    WorkspaceSummary,
)
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_risk_policy import ai_writes_require_human_review
from backend.services.settings_service import normalized_library_policy
from backend.spi.tool_presentation import MCP_CONTEXT_LINK_GUIDANCE
from backend.spi.types import AI_PROVENANCES

CONTEXT_PAGE_TAXONOMY = [
    ("overview", "High-level summary page"),
    ("source", "Source-specific notes and extraction summaries"),
    ("entity", "Named entity profile"),
    ("concept", "Concept explanation"),
    ("synthesis", "Synthesis across multiple sources"),
    ("comparison", "Comparison across entities or options"),
    ("decision", "Decision record"),
    ("contradiction", "Conflicting evidence or claims"),
    ("query", "Saved research query"),
    ("guide", "Human or agent guide"),
]
AGENT_PAGE_TAXONOMY = [
    (
        "agent_guide",
        "Agent guide: system prompt, rules, and instructions (AGENT.md/CLAUDE.md)",
    ),
    ("persona", "Agent persona guidance"),
    ("skill", "Reusable skills and tool usage"),
    ("memory", "Reviewed memory, user profile, and reference material"),
]
AVAILABLE_READ_TOOLS = [
    "list_my_workspaces",
    "guide",
    "get_index",
    "read_concept",
    "search",
    "fetch",
    "grep_wiki",
    "what_changed",
    "read",
    "read_context_outline",
    "read_structure_map",
    "cite",
    # P8.6: the Context-editing-state-machine surface.
    "create_context_page",
    "begin_context_edit",
    "update_context_edit",
    "submit_context_edit_for_review",
    "discard_context_edit",
    "patch_context_edit",
    "list_my_context_edits",
    "upload_image",
    "list_changes",
    "submit_feedback",
    "list_agent_categories",
    "read_agent_page",
    "write_agent_page_draft",
    "build_agent_input_bundle",
    "list_requested_changes",
    "submit_revision_for_review",
    "wiki_maintenance",
]
AVAILABLE_PROMPTS = [
    "dtxwiki.context_workflow",
    "dtxwiki.agent_page",
    "dtxwiki.agent_page.{slug}",
]
AVAILABLE_RESOURCES = [
    "dtxwiki://workspaces/{workspace_id}/guide",
    "dtxwiki://workspaces/{workspace_id}/index",
    "dtxwiki://workspaces/{workspace_id}/bundle",
    "dtxwiki://workspaces/{workspace_id}/agent-injection-policy",
    "dtxwiki://workspaces/{workspace_id}/agent/pages/{slug}",
    "dtxwiki://workspaces/{workspace_id}/agent/pages/by-category/{category}",
    "dtxwiki://workspaces/{workspace_id}/context/pages/{slug}",
]
REQUIRED_WORKFLOW = [
    (
        "guide: this brief view is enough to read, edit and explore. Call "
        "guide(detail_level='normal') for taxonomies, prompts/resources, "
        "AgentPage policy, hot concepts and your full tool list, or "
        "guide(detail_level='detailed') for field explanations, only when a "
        "task needs them."
    ),
    (
        "find: search (ranked), then fetch a hit by its id and type; grep_wiki "
        "for exact text; get_index to browse; what_changed to re-sync instead "
        "of re-reading."
    ),
    (
        "read: read_context_outline to choose a section (with select it "
        "returns that section), read for a whole page, read_structure_map for "
        "block ids before block-addressed edits."
    ),
    "cite before a sourced write.",
    (
        "write: create_context_page (new page) or begin_context_edit "
        "(existing page); then patch_context_edit (local changes) or "
        "update_context_edit (whole body), taking the next editing_revision "
        "and block ids from each response's edit_receipt instead of "
        "re-reading; then submit_context_edit_for_review."
    ),
    (
        "place a new page: walk get_index(format='nodes') from /context and "
        "pass the chosen node_id as placement_parent_node_id (omit it for "
        "Inbox)."
    ),
    MCP_CONTEXT_LINK_GUIDANCE,
    (
        "for an image: upload_image first; store data.markdown or "
        "data.logical_ref, never data.content_url or an /api/v1 transport URL; "
        "upload_image always requires an existing page_id and your own open "
        "editing_version_id, so create the page first for a new page, then "
        "upload and call update_context_edit; never fall back to data:"
    ),
]
# Above this, the guide sends agents to search and get_index instead of
# offering the whole-workspace bundle: a page count says nothing about size
# (109 pages here weighed 7.3 MiB).
WHOLE_READ_BUDGET_BYTES = 64 * 1024
GUIDE_DETAIL_LEVELS = {
    "brief": (
        "Default: workspace, your permissions, how to find pages, and the "
        "read/write workflow. Enough to read, edit and explore."
    ),
    "normal": (
        "Adds page and AgentPage taxonomies, prompt and resource entry points, "
        "the AgentPage injection policy, hot concepts and your full tool list."
    ),
    "detailed": (
        "Adds the full workspace summary and actor context, and explains what "
        "each permission and routing field means."
    ),
}
GUIDE_LEVEL_ORDER = ("brief", "normal", "detailed")
# "full" was the old name of the richest level; it never meant every list.
GUIDE_LEVEL_ALIASES = {"full": "detailed"}
GUIDE_FIELD_EXPLANATIONS = {
    "actor_context.may_write_drafts": (
        "True when your credential can call create_context_page or "
        "begin_context_edit."
    ),
    "actor_context.my_writes_wait_for_human_review": (
        "True when what you write as an AI client waits for a human review "
        "decision in this workspace; false for browser writes, or when the "
        "workspace turned the requirement off."
    ),
    "actor_context.my_writes_stage_as_drafts": (
        "True for AI-authenticated writes: they never move the published "
        "version themselves; a review decision publishes them."
    ),
    "workspace_routing.whole_read": (
        "within_budget: the whole-workspace bundle is estimated at or under "
        "whole_read_budget_bytes, so reading it whole is an option for tasks "
        "that need the whole workspace. over_budget: find pages with search "
        "or get_index instead. The size is estimated from stored bodies."
    ),
}
_BRIEF_SUMMARY_FIELDS = ("id", "slug", "title", "goal", "default_language")
_BRIEF_ACTOR_FIELDS = (
    "workspace_role",
    "may_write_drafts",
    "my_writes_wait_for_human_review",
    "my_writes_stage_as_drafts",
)

RECOMMENDED_AGENT_GUIDANCE_FLOW = [
    "Call guide first to understand workspace policy and available MCP surfaces.",
    (
        "Use prompts/list for reusable AgentPage prompts when persona or "
        "policy guidance is needed."
    ),
    (
        "Use resources/list for entry points (workspace index/bundle, "
        "AgentPages); Context pages read directly via context/pages/{slug} "
        "URIs without being listed."
    ),
    (
        "Use build_agent_input_bundle when task-scoped AgentPage and source "
        "context should be packed together."
    ),
    (
        "For lookups use search (ranked), then fetch the chosen result by its "
        "id/type pair; use grep_wiki for exact text, descend get_index nodes "
        "and read_concept for navigation, and re-sync with what_changed "
        "instead of re-crawling."
    ),
    (
        "A new Context draft with no placement goes to Inbox. To choose a "
        "location, walk get_index(format='nodes') and pass a node_id as "
        "placement_parent_node_id; paths are discovery hints, not write "
        "inputs."
    ),
    (
        "For images, call upload_image first and store data.markdown or "
        "data.logical_ref. Never persist data.content_url or another /api/v1 "
        "transport URL. upload_image always requires an existing page_id and "
        "your own currently-open editing_version_id -- create the page first "
        "for a new page, then upload with page_id and call "
        "update_context_edit. If upload fails, stop and report the failure; "
        "never substitute a data: URI."
    ),
]

_WITHIN_BUDGET_ROUTING = (
    "The whole workspace is about {size} ({pages} pages), within the {budget} "
    "budget: read the bundle resource only for tasks that need the whole "
    "workspace; otherwise find pages with search or get_index, and re-sync "
    "with what_changed."
)
_OVER_BUDGET_ROUTING = (
    "This workspace is too large to read whole (about {size}, {pages} pages, "
    "over the {budget} budget). Find pages with search or get_index, read only "
    "the sections you need, and re-sync with what_changed."
)


@dataclass(frozen=True)
class _GuideInputs:
    session: Session
    workspace: Workspace
    actor: ActorContext | None
    available_tools: list[str] | None


class WorkspaceReadService:
    def list_workspaces(
        self,
        session: Session,
        actor=None,
    ) -> WorkspaceListData:
        """List workspaces visible to ``actor``.

        D-01v2-f: for a pat/oauth_jwt actor this is the credential's allowed
        set (``actor.token_workspace_ids``) intersected with live
        WorkspaceMembership (see ``actor_workspace_filter``) -- both for the
        REST ``GET /workspaces`` listing and for the ``list_my_workspaces``
        MCP tool. A membership the credential's allowed set does not (yet)
        cover is not named here; the caller surfaces only its count.
        """
        rows = self._workspace_rows(
            session=session,
            actor=actor,
        )
        other_membership_count = 0
        actor_user_id = getattr(actor, "user_id", None)
        if actor_user_id is not None:
            total_membership_count = len(list_user_workspaces(session, actor_user_id))
            other_membership_count = max(total_membership_count - len(rows), 0)
        user_default_id = self._user_default_workspace_id(session, actor)
        current = self._resolve_current_workspace(
            rows=rows,
            user_default_id=user_default_id,
        )
        # default_workspace_id reflects the user's persisted landing pointer.
        # It remains distinct from current_workspace_id, which can be an
        # unpersisted single-membership fallback. Expose the pointer only when
        # it is accessible to this actor (S2/C9).
        accessible_default_id = (
            user_default_id
            if user_default_id is not None
            and any(workspace.id == user_default_id for workspace, _role in rows)
            else None
        )
        workspace_ids = [workspace.id for workspace, _role in rows]
        member_counts = {
            workspace_id: int(count)
            for workspace_id, count in session.execute(
                select(WorkspaceMembership.workspace_id, func.count())
                .where(WorkspaceMembership.workspace_id.in_(workspace_ids))
                .group_by(WorkspaceMembership.workspace_id)
            )
        }
        # WS-09: keyed by workspace (not owner_user_id) because a workspace's
        # `owner_user_id` projection can be null between an owner leaving and
        # `_sync_owner_projection` re-running.
        owner_names = {
            workspace_id: display_name
            for workspace_id, display_name in session.execute(
                select(Workspace.id, User.display_name)
                .join(User, User.id == Workspace.owner_user_id)
                .where(Workspace.id.in_(workspace_ids))
            )
        }
        return WorkspaceListData(
            workspaces=[
                self._summary(
                    workspace,
                    role=role,
                    member_count=member_counts.get(workspace.id, 0),
                    owner_display_name=owner_names.get(workspace.id),
                )
                for workspace, role in rows
            ],
            current_workspace_id=current.id if current is not None else None,
            default_workspace_id=accessible_default_id,
            other_membership_count=other_membership_count,
        )

    @staticmethod
    def _user_default_workspace_id(
        session: Session,
        actor=None,
    ) -> UUID | None:
        user_id = getattr(actor, "user_id", None)
        if user_id is None:
            return None
        user = session.get(User, user_id)
        return user.default_workspace_id if user is not None else None

    @staticmethod
    def _resolve_current_workspace(
        *,
        rows: list[tuple[Workspace, str | None]],
        user_default_id: UUID | None,
    ) -> Workspace | None:
        if not rows:
            return None
        if user_default_id is not None:
            user_default = next(
                (
                    workspace
                    for workspace, _role in rows
                    if workspace.id == user_default_id
                ),
                None,
            )
            if user_default is not None:
                return user_default
        # A sole visible workspace is unambiguous. Multiple rows without a live
        # user pointer require an explicit picker choice, including for
        # non-user actors; no installation-wide workspace is inferred.
        return rows[0][0] if len(rows) == 1 else None

    def get_workspace(self, session: Session, workspace_id: UUID) -> WorkspaceDetail:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        return self._detail(workspace)

    def guide(
        self,
        session: Session,
        workspace_id: UUID,
        detail_level: str = "brief",
        available_tools: list[str] | None = None,
        actor: ActorContext | None = None,
    ) -> GuidePayload:
        """The guide at ``detail_level``: each level adds the views registered
        for it on top of the levels below (``_GUIDE_VIEWS``)."""

        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        level = GUIDE_LEVEL_ALIASES.get(detail_level, detail_level)
        rank = GUIDE_LEVEL_ORDER.index(level)
        inputs = _GuideInputs(
            session=session,
            workspace=workspace,
            actor=actor,
            available_tools=available_tools,
        )
        fields: dict[str, Any] = {
            "detail_level": level,
            "detail_levels": GUIDE_DETAIL_LEVELS,
        }
        for view_level, view in self._GUIDE_VIEWS:
            if GUIDE_LEVEL_ORDER.index(view_level) <= rank:
                fields.update(view(self, inputs))
        return GuidePayload(**fields)

    def _brief_view(self, inputs: _GuideInputs) -> dict[str, Any]:
        """What reading, editing and exploring need, and nothing else."""

        summary = self._workspace_summary(inputs)
        actor_context = self._actor_context(inputs)
        return {
            "workspace_summary": {key: summary[key] for key in _BRIEF_SUMMARY_FIELDS},
            "actor_context": {key: actor_context[key] for key in _BRIEF_ACTOR_FIELDS},
            "citation_policy": inputs.workspace.citation_policy,
            "workspace_routing": self._workspace_routing(inputs),
            "required_workflow": REQUIRED_WORKFLOW,
        }

    def _normal_view(self, inputs: _GuideInputs) -> dict[str, Any]:
        from backend.services.trail_analysis import list_hot_concepts

        return {
            "library_policy_summary": self._library_policy_summary(inputs.workspace),
            "agent_injection_policy": inputs.workspace.agent_injection_policy,
            "context_page_taxonomy": self._taxonomy(CONTEXT_PAGE_TAXONOMY),
            "agent_page_taxonomy": self._taxonomy(AGENT_PAGE_TAXONOMY),
            "available_tools": self._available_tools(inputs.available_tools),
            "available_prompts": AVAILABLE_PROMPTS,
            "available_resources": AVAILABLE_RESOURCES,
            "hot_concepts": list_hot_concepts(
                inputs.session,
                inputs.workspace.id,
                actor=inputs.actor,
            ),
            "recommended_agent_guidance_flow": RECOMMENDED_AGENT_GUIDANCE_FLOW,
        }

    def _detailed_view(self, inputs: _GuideInputs) -> dict[str, Any]:
        return {
            "workspace_summary": self._workspace_summary(inputs),
            "actor_context": self._actor_context(inputs),
            "field_explanations": GUIDE_FIELD_EXPLANATIONS,
        }

    # Lowest level that includes each view; a later view may replace a field
    # an earlier one set (detailed's full summary over brief's subset). A new
    # guide field is a new view here, not a branch in guide().
    _GUIDE_VIEWS = (
        ("brief", _brief_view),
        ("normal", _normal_view),
        ("detailed", _detailed_view),
    )

    def _workspace_summary(self, inputs: _GuideInputs) -> dict[str, Any]:
        return self._summary(
            inputs.workspace, role=getattr(inputs.actor, "workspace_role", None)
        ).model_dump(mode="json")

    @staticmethod
    def _actor_context(inputs: _GuideInputs) -> dict[str, Any]:
        actor = inputs.actor
        workspace = inputs.workspace
        available_tools = inputs.available_tools
        return {
            "actor_kind": getattr(actor, "actor_kind", "anonymous"),
            "actor_id": getattr(actor, "actor_id", None),
            "workspace_id": str(workspace.id),
            "workspace_slug": workspace.slug,
            "workspace_role": getattr(actor, "workspace_role", "none"),
            "may_write_drafts": any(
                name in set(available_tools or [])
                for name in ("create_context_page", "begin_context_edit")
            ),
            # Both of these used to be read out of `write_policy`, a blob
            # nothing enforced and no surface could edit, under names that
            # promised more than they delivered. They now report what is
            # actually true of this caller's writes.
            #
            # `my_writes_wait_for_human_review` is the question an agent is
            # really asking: will what I write sit in a queue, or land? It
            # is false for a person writing in the browser, and for an agent
            # in a workspace that has turned the requirement off.
            "my_writes_wait_for_human_review": (
                getattr(actor, "provenance", "internal") in AI_PROVENANCES
                and ai_writes_require_human_review(workspace)
            ),
            # An invariant, not a preference: an AI-authenticated write never
            # moves the published pointer itself. It either waits for a
            # decision or is published by one.
            "my_writes_stage_as_drafts": (
                getattr(actor, "provenance", "internal") in AI_PROVENANCES
            ),
        }

    @staticmethod
    def _workspace_routing(inputs: _GuideInputs) -> WorkspaceRouting:
        """Offer the whole-workspace bundle only when it is small enough to
        read whole; otherwise route to search and get_index."""

        pages, body_bytes = visible_page_body_totals(
            inputs.session, inputs.workspace.id, inputs.actor
        )
        within = body_bytes <= WHOLE_READ_BUDGET_BYTES
        template = _WITHIN_BUDGET_ROUTING if within else _OVER_BUDGET_ROUTING
        return WorkspaceRouting(
            visible_pages=pages,
            bundle_estimated_bytes=body_bytes,
            whole_read_budget_bytes=WHOLE_READ_BUDGET_BYTES,
            whole_read="within_budget" if within else "over_budget",
            bundle_resource=(
                f"dtxwiki://workspaces/{inputs.workspace.id}/bundle"
                if within
                else None
            ),
            guidance=template.format(
                size=_human_bytes(body_bytes),
                pages=pages,
                budget=_human_bytes(WHOLE_READ_BUDGET_BYTES),
            ),
        )

    @staticmethod
    def _workspace_rows(
        session: Session,
        actor=None,
    ) -> list[tuple[Workspace, str | None]]:
        if actor is not None and getattr(actor, "user_id", None) is not None:
            # A workspace held for an account's deletion is closed to its
            # members, so it is not listed until an admin reopens it.
            rows = [
                (workspace, role)
                for workspace, role in list_user_workspaces(session, actor.user_id)
                if workspace.account_deletion_user_id is None
            ]
            return actor_workspace_filter(actor, rows)
        actor_workspace_id = getattr(actor, "workspace_id", None)
        if actor is not None and actor_workspace_id is not None:
            workspace = session.get(Workspace, actor_workspace_id)
            if workspace is None:
                return []
            return [
                (
                    workspace,
                    getattr(actor, "workspace_role", None)
                    or getattr(actor, "role", None),
                )
            ]
        return [
            (workspace, None)
            for workspace in session.scalars(
                select(Workspace).order_by(Workspace.created_at, Workspace.slug)
            )
        ]

    @staticmethod
    def _summary(
        workspace: Workspace,
        role: str | None = None,
        member_count: int = 0,
        owner_display_name: str | None = None,
    ) -> WorkspaceSummary:
        return WorkspaceSummary(
            id=workspace.id,
            slug=workspace.slug,
            title=workspace.title,
            name=workspace.name,
            goal=workspace.goal,
            default_language=workspace.default_language,
            visibility=workspace.visibility,
            join_policy=workspace.join_policy,
            member_count=member_count,
            role=role,
            owner_display_name=owner_display_name,
        )

    def _detail(self, workspace: Workspace) -> WorkspaceDetail:
        return WorkspaceDetail(
            **self._summary(workspace).model_dump(),
            description=workspace.description,
            library_policy=workspace.library_policy,
            write_policy={},
            citation_policy=workspace.citation_policy,
            agent_injection_policy=workspace.agent_injection_policy,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
        )

    @staticmethod
    def _taxonomy(entries: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [
            {"type": entry_type, "description": description}
            for entry_type, description in entries
        ]

    @staticmethod
    def _library_policy_summary(workspace: Workspace) -> dict[str, object]:
        policy = normalized_library_policy(workspace.library_policy)
        return {
            "manual_approval_required": policy["manual_approval_required"],
        }

    @staticmethod
    def _available_tools(available_tools: list[str] | None = None) -> list[str]:
        # MCP entry points supply the actor-filtered live catalog. Only direct
        # internal callers without a catalog use the fallback list.
        return list(
            AVAILABLE_READ_TOOLS if available_tools is None else available_tools
        )


def _human_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    kib = size / 1024
    return f"{kib:.0f} KiB" if size % 1024 == 0 else f"{kib:.1f} KiB"
