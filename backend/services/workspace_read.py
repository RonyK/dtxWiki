from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.policy.rbac import list_user_workspaces
from backend.policy.trust_boundaries import actor_workspace_filter
from backend.policy.workspace_resolution import repair_default_workspace_to_personal
from backend.schemas.read import (
    GuidePayload,
    WorkspaceDetail,
    WorkspaceListData,
    WorkspaceSummary,
)
from backend.services.actor_visible_read import agent_page_requires_sensitive_access
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_risk_policy import ai_writes_require_human_review
from backend.services.settings_service import normalized_library_policy
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
    "guide",
    "get_index",
    "read_concept",
    "search",
    "fetch",
    "grep_wiki",
    "what_changed",
    "read",
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
    "guide",
    "search or get_index",
    "fetch a search result, or use read_concept/read for direct navigation",
    "cite before a sourced write",
    (
        "for an image: upload_image first; store data.markdown or "
        "data.logical_ref, never data.content_url or an /api/v1 transport URL; "
        "upload_image always requires an existing page_id and your own open "
        "editing_version_id, so create the page first for a new page, then "
        "upload and call update_context_edit; never fall back to data:"
    ),
    (
        "to write: create_context_page (new page) or begin_context_edit "
        "(existing page); then patch_context_edit (local changes) or "
        "update_context_edit (whole body); then submit_context_edit_for_review"
    ),
]
CAG_BUNDLE_PAGE_LIMIT = 200

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
        "location, read placement_parent_candidates and pass that entry's "
        "node_id as placement_parent_node_id; paths are discovery hints, not "
        "write inputs."
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

_SMALL_WORKSPACE_ROUTING = (
    "This workspace is small ({count} visible pages): read the whole bundle "
    "resource {bundle_uri} once and re-sync with what_changed (CAG routing)."
)
_LARGE_WORKSPACE_ROUTING = (
    "This workspace is large ({count} visible pages): descend get_index "
    "nodes and read_concept selected paths; use search/grep_wiki for "
    "lookups and what_changed for deltas."
)


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
        user_default_id, personal_workspace_id = self._user_workspace_pointers(
            session,
            actor,
        )
        current = self._resolve_current_workspace(
            rows=rows,
            user_default_id=user_default_id,
            personal_workspace_id=personal_workspace_id,
        )
        # default_workspace_id reflects the user's persisted landing pointer,
        # either explicitly selected or repaired to a live Personal workspace.
        # It remains distinct from current_workspace_id, which can be an
        # unpersisted single-membership fallback. Expose the pointer only when
        # it is accessible to this actor (S2/C9).
        accessible_default_id = (
            user_default_id
            if user_default_id is not None
            and any(workspace.id == user_default_id for workspace, _role in rows)
            else None
        )
        # Same visibility rule as accessible_default_id: an OAuth actor
        # credential-scoped to one workspace must not learn the id of a
        # DIFFERENT workspace -- even just the Personal anchor's id, with no
        # other metadata attached -- if that workspace fell outside `rows`.
        accessible_personal_id = (
            personal_workspace_id
            if personal_workspace_id is not None
            and any(
                workspace.id == personal_workspace_id for workspace, _role in rows
            )
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
            personal_workspace_id=accessible_personal_id,
            other_membership_count=other_membership_count,
        )

    @staticmethod
    def _user_workspace_pointers(
        session: Session,
        actor=None,
    ) -> tuple[UUID | None, UUID | None]:
        user_id = getattr(actor, "user_id", None)
        if user_id is None:
            return None, None
        user = session.get(User, user_id)
        if user is None:
            return None, None
        repair_default_workspace_to_personal(session=session, user=user)
        return user.default_workspace_id, user.personal_workspace_id

    @staticmethod
    def _resolve_current_workspace(
        *,
        rows: list[tuple[Workspace, str | None]],
        user_default_id: UUID | None,
        personal_workspace_id: UUID | None,
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
        if personal_workspace_id is not None:
            personal = next(
                (
                    workspace
                    for workspace, _role in rows
                    if workspace.id == personal_workspace_id
                ),
                None,
            )
            if personal is not None:
                return personal
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
        detail_level: str = "normal",
        available_tools: list[str] | None = None,
        actor: ActorContext | None = None,
    ) -> GuidePayload:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        from backend.services.trail_analysis import list_hot_concepts

        return GuidePayload(
            workspace_summary=self._summary(
                workspace, role=getattr(actor, "workspace_role", None)
            ).model_dump(mode="json"),
            actor_context={
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
            },
            library_policy_summary=self._library_policy_summary(workspace),
            context_write_policy={},
            agent_injection_policy=workspace.agent_injection_policy,
            citation_policy=workspace.citation_policy,
            context_page_taxonomy=self._taxonomy(CONTEXT_PAGE_TAXONOMY),
            agent_page_taxonomy=self._taxonomy(AGENT_PAGE_TAXONOMY),
            available_tools=self._available_tools(detail_level, available_tools),
            available_prompts=AVAILABLE_PROMPTS,
            available_resources=AVAILABLE_RESOURCES,
            active_agent_pages=self._active_agent_pages(
                session,
                workspace_id,
                include_sensitive=_actor_is_owner(session, actor, workspace_id),
            ),
            hot_concepts=list_hot_concepts(
                session,
                workspace_id,
                actor=actor,
            ),
            placement_parent_candidates=self._placement_parent_candidates(
                session, workspace_id
            ),
            recommended_agent_guidance_flow=self._guidance_flow(
                session, workspace_id, actor
            ),
            required_workflow=REQUIRED_WORKFLOW,
        )

    @staticmethod
    def _placement_parent_candidates(
        session: Session, workspace_id: UUID
    ) -> list[dict[str, str | int | None]]:
        from backend.models.tree import WorkspaceTreeNode

        nodes = session.scalars(
            select(WorkspaceTreeNode)
            .where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.visibility_status == "visible",
            )
        ).all()
        nodes_by_id = {node.id: node for node in nodes}

        def hierarchy(node: WorkspaceTreeNode) -> tuple[int, str]:
            lineage: list[WorkspaceTreeNode] = []
            seen: set[UUID] = set()
            current: WorkspaceTreeNode | None = node
            while current is not None and current.id not in seen:
                seen.add(current.id)
                lineage.append(current)
                current = (
                    nodes_by_id.get(current.parent_id)
                    if current.parent_id is not None
                    else None
                )
            lineage.reverse()
            path_parts = [
                candidate.slug or candidate.title.strip().replace(" ", "-").lower()
                for candidate in lineage
            ]
            return max(len(lineage) - 1, 0), "/" + "/".join(path_parts)

        candidates = []
        for node in sorted(nodes, key=lambda item: (item.title, item.id)):
            if not node.can_have_children:
                continue
            depth, path = hierarchy(node)
            # No slug or parent_node_id: the path already ends in the slug and
            # spells out the parents, and this list is most of every guide
            # response (78 entries were 23.6k of a 32k-character payload).
            candidates.append(
                {
                    "node_id": str(node.id),
                    "title": node.title,
                    "depth": depth,
                    "path": path,
                }
            )
        return candidates

    @staticmethod
    def _guidance_flow(
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
    ) -> list[str]:
        """E-3 scale-aware routing: small workspaces read the CAG bundle
        whole; large ones descend the index — the guide must say which."""
        from backend.models.context import ContextPage

        count = (
            session.scalar(
                select(func.count())
                .select_from(ContextPage)
                .where(ContextPage.workspace_id == workspace_id)
                .where(ContextPage.lifecycle_status != "archived")
                .where(ContextPage.current_published_version_id.is_not(None))
            )
            or 0
        )
        bundle_uri = f"dtxwiki://workspaces/{workspace_id}/bundle"
        if count <= CAG_BUNDLE_PAGE_LIMIT:
            routing = _SMALL_WORKSPACE_ROUTING.format(
                count=count, bundle_uri=bundle_uri
            )
        else:
            routing = _LARGE_WORKSPACE_ROUTING.format(count=count)
        return [*RECOMMENDED_AGENT_GUIDANCE_FLOW, routing]

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
    def _active_agent_pages(
        session: Session,
        workspace_id: UUID,
        include_sensitive: bool,
    ) -> list[dict[str, str]]:
        rows = session.scalars(
            select(AgentPage)
            .where(AgentPage.workspace_id == workspace_id)
            .where(AgentPage.lifecycle_status == "active")
            .order_by(AgentPage.category, AgentPage.priority, AgentPage.slug)
        ).all()
        return [
            {
                "id": str(page.id),
                "slug": page.slug,
                "title": page.title,
                "category": page.category,
                "injection_policy": page.injection_policy,
            }
            for page in rows
            if include_sensitive or not agent_page_requires_sensitive_access(page)
        ]

    @staticmethod
    def _taxonomy(entries: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [
            {
                "type": entry_type,
                "description": description,
                "example_use": description,
            }
            for entry_type, description in entries
        ]

    @staticmethod
    def _library_policy_summary(workspace: Workspace) -> dict[str, object]:
        policy = normalized_library_policy(workspace.library_policy)
        return {
            "manual_approval_required": policy["manual_approval_required"],
        }

    @staticmethod
    def _available_tools(
        detail_level: str,
        available_tools: list[str] | None = None,
    ) -> list[str]:
        if detail_level == "brief":
            tools = [
                "guide",
                "search",
                "fetch",
                "grep_wiki",
                "read",
                "list_agent_categories",
            ]
        else:
            tools = AVAILABLE_READ_TOOLS
        if available_tools is None:
            return tools
        allowed = set(available_tools)
        return [tool for tool in tools if tool in allowed]


def _actor_is_owner(
    session: Session,
    actor: ActorContext | None,
    workspace_id: UUID,
) -> bool:
    if actor is None:
        return True
    if actor.user_id is None:
        return False
    return any(
        workspace.id == workspace_id and role == "owner"
        for workspace, role in WorkspaceReadService._workspace_rows(session, actor)
    )
