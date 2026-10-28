from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.domain.input_policy import (
    validate_text,
    validate_title,
)
from backend.models.base import utc_now
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import Workspace, default_actor_id
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.read import TreeNodePayload
from backend.schemas.tree import (
    TreeFolderCreateRequest,
    TreeMoveRequest,
    TreeNodeUpdate,
    TreeReorderByAnchorRequest,
    TreeReorderRequest,
)
from backend.services.audit_service import AuditService
from backend.services.context_media_legacy_cutover import (
    require_no_visible_legacy_media,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.read_errors import (
    DuplicateSlugError,
    ReadNotFoundError,
    RejectedByPolicyError,
)
from backend.services.review_risk_policy import ReviewRiskResolver
from backend.services.tree_read import TreeReadService
from backend.spi.types import AI_PROVENANCES
from backend.utils.uuid import parse_uuid_or_none

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext

logger = logging.getLogger(__name__)

ENTITY_NODE_KINDS = {
    "agent_page": "page_ref",
    "context_page": "page_ref",
    "library_item": "library_item_ref",
    "library_artifact": "library_artifact_ref",
}

DEFAULT_TREE_MOVE_REASON = "Tree node moved"


class TreeService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        tree_read_service: TreeReadService | None = None,
    ) -> None:
        self.audit_service = audit_service or AuditService()
        self.tree_read_service = tree_read_service or TreeReadService()

    def update_node(
        self,
        session: Session,
        workspace_id: UUID,
        node_id: UUID,
        request: TreeNodeUpdate,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> TreeNodePayload:
        validate_request_inputs(request)
        node = require_visible_tree_node(
            session, workspace_id, node_id, actor, self.tree_read_service
        )
        normalized_title = validate_title(
            request.display_name, max_length=80 if node.node_kind == "folder" else 256
        )
        if "nav_summary" in request.model_fields_set:
            if node.node_kind != "folder":
                raise RejectedByPolicyError(
                    "nav_summary is only valid on folder nodes",
                )
            node.nav_summary = request.nav_summary
        previous_title = node.title
        node.title = normalized_title
        if node.target_kind == "ask_session" and node.target_id is not None:
            from backend.models.ask import AskSession

            ask_session = get_in_workspace(
                session,
                AskSession,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            if ask_session is not None:
                ask_session.title = node.title
                ask_session.title_user_edited = True
        elif node.target_kind == "context_page" and node.target_id is not None:
            from backend.models.context import ContextPage
            from backend.services.page_service import sync_page_projections

            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            if page is not None:
                page.title = node.title
                # A tree rename mutates the canonical page title without a
                # new version, so the name projection must resync here or the
                # entity lane keeps resolving the old title (Sprint-2 review
                # BS-1). version=None leaves PageLink rows untouched.
                sync_page_projections(session, page=page)
        elif node.target_kind == "agent_page" and node.target_id is not None:
            from backend.models.agent import AgentPage
            from backend.services.indexing.fts5 import sync_agent_page_doc_sources

            agent_page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            if agent_page is not None:
                agent_page.title = node.title
                sync_agent_page_doc_sources(session, agent_page)
        change_set = create_tree_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=f"rename tree node {previous_title} to {node.title}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="workspace_tree_node",
                target_id=node.id,
                operation="tree_rename",
                diff_summary=f"{previous_title} -> {node.title}",
            )
        )
        audit = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="tree.node.rename",
            target_kind="workspace_tree_node",
            target_id=node.id,
            payload={
                "change_set_id": str(change_set.id),
                "previous_title": previous_title,
                "title": node.title,
            },
        )
        session.flush()
        if previous_title != node.title:
            _sync_subtree_search_doc_sources(session, workspace_id, node)
        assert audit.id
        return self.tree_read_service.get_node(session, workspace_id, node.id)

    def create_folder(
        self,
        session: Session,
        workspace_id: UUID,
        request: TreeFolderCreateRequest,
        actor_kind: str,
        actor_id: str,
    ) -> TreeNodePayload:
        validate_request_inputs(request)
        policy = session.get(TreePolicy, (workspace_id, request.section))
        if policy is None or not policy.allow_user_folder_create:
            raise RejectedByPolicyError(
                "folder creation is disabled for this tree",
                error_code="folder_creation_disabled",
            )
        parent = resolve_tree_parent(
            session=session,
            workspace_id=workspace_id,
            section=request.section,
            parent_id=request.parent_node_id,
            default_parent_slug=None,
        )
        title = request.title.strip()
        slug = request.slug or folder_slug(title)
        if sibling_slug_exists(
            session=session,
            workspace_id=workspace_id,
            parent_id=parent.id,
            slug=slug,
        ):
            raise DuplicateSlugError("duplicate slug under parent")
        node = create_tree_node(
            session,
            workspace_id=workspace_id,
            section=request.section,
            node_kind="folder",
            parent_id=parent.id,
            title=title,
            slug=slug,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        change_set = create_tree_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=f"create folder {slug}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="workspace_tree_node",
                target_id=node.id,
                operation="tree_create",
                diff_summary=f"+folder {slug}",
                diff_ref=f"parent:{parent.id};sort:{node.sort_key}",
            )
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="tree.node.create",
            target_kind="workspace_tree_node",
            target_id=node.id,
            payload={
                "change_set_id": str(change_set.id),
                "parent_node_id": str(parent.id),
                "slug": slug,
                "section": request.section,
                "title": title,
            },
        )
        session.flush()
        return self.tree_read_service.get_node(session, workspace_id, node.id)

    def move_node(
        self,
        session: Session,
        workspace_id: UUID,
        request: TreeMoveRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> TreeNodePayload:
        node = require_visible_tree_node(
            session, workspace_id, request.node_id, actor, self.tree_read_service
        )
        parent = require_visible_tree_node(
            session,
            workspace_id,
            request.new_parent_id,
            actor,
            self.tree_read_service,
        )
        if node.is_system_node and node.parent_id is None:
            raise RejectedByPolicyError("tree root cannot be moved")
        if parent.section != node.section:
            raise RejectedByPolicyError("tree node cannot move across sections")
        if parent.visibility_status != "visible":
            raise RejectedByPolicyError("target parent is not visible")
        if not parent.can_have_children:
            raise RejectedByPolicyError("target parent cannot have children")
        if node.id == parent.id or is_descendant(session, parent, node.id):
            raise RejectedByPolicyError("tree cycle detected")
        if (
            node.node_kind == "folder"
            and node.slug is not None
            and node.parent_id != parent.id
            and sibling_slug_exists(
                session,
                workspace_id=workspace_id,
                parent_id=parent.id,
                slug=node.slug,
            )
        ):
            # surface the same 409 the create path raises instead of letting
            # the visible-folder partial unique 500 at flush
            raise DuplicateSlugError(
                f"folder slug already exists under target parent: {node.slug}"
            )

        reason = (request.reason or "").strip() or DEFAULT_TREE_MOVE_REASON
        previous_parent_id = node.parent_id
        previous_sort_key = node.sort_key
        previous_parent = (
            get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=workspace_id,
                resource_id=previous_parent_id,
            )
            if previous_parent_id
            else None
        )
        previous_parent_path = (
            [
                {"id": str(entry.id), "title": entry.title}
                for entry in self.tree_read_service.path_for_node(
                    session, previous_parent
                )
            ]
            if previous_parent
            else []
        )
        new_parent_path = [
            {"id": str(entry.id), "title": entry.title}
            for entry in self.tree_read_service.path_for_node(session, parent)
        ]
        node.parent_id = parent.id
        node.sort_key = request.sort_key or next_child_sort_key(
            session=session,
            workspace_id=workspace_id,
            parent_id=parent.id,
        )
        change_set = create_tree_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=reason,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="workspace_tree_node",
                target_id=node.id,
                operation="tree_move",
                diff_summary=reason,
                diff_ref=(
                    f"from:{previous_parent_id}:{previous_sort_key};"
                    f"to:{parent.id}:{node.sort_key}"
                ),
            )
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="tree.node.move",
            target_kind="workspace_tree_node",
            target_id=node.id,
            payload={
                "change_set_id": str(change_set.id),
                "previous_parent_id": (
                    str(previous_parent_id) if previous_parent_id else None
                ),
                "new_parent_id": str(parent.id),
                "previous_parent_path": previous_parent_path,
                "new_parent_path": new_parent_path,
                "reason": reason,
            },
        )
        session.flush()
        _refresh_tree_edges(session, workspace_id, {node.id})
        _sync_subtree_search_doc_sources(session, workspace_id, node)
        return self.tree_read_service.get_node(session, workspace_id, node.id)

    def reorder_node(
        self,
        session: Session,
        workspace_id: UUID,
        request: TreeReorderRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> TreeNodePayload:
        reorder = request.root
        # SQLite's SELECT ... FOR UPDATE is a no-op, so acquire its database
        # writer lock before reading the node or siblings. PostgreSQL keeps the
        # narrower row locks below.
        acquire_write_intent(session)
        if session.get_bind().dialect.name == "sqlite":
            session.scalar(
                select(WorkspaceTreeNode)
                .where(WorkspaceTreeNode.id == reorder.node_id)
                .execution_options(populate_existing=True)
            )
        node = require_visible_tree_node(
            session, workspace_id, reorder.node_id, actor, self.tree_read_service
        )
        if node.is_system_node or node.parent_id is None:
            raise RejectedByPolicyError(
                "system tree node cannot be reordered",
                error_code="system_tree_node_protected",
            )

        # Serialize every reorder under this parent. PostgreSQL holds this row
        # lock until the endpoint transaction commits, so sibling reads and the
        # gap-free renumber cannot interleave with another reorder of the same
        # parent. The sibling locks also protect against direct row mutations.
        session.scalar(
            select(WorkspaceTreeNode.id)
            .where(WorkspaceTreeNode.id == node.parent_id)
            .with_for_update()
        )
        siblings = list(
            session.scalars(
                select(WorkspaceTreeNode)
                .where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.parent_id == node.parent_id,
                    WorkspaceTreeNode.visibility_status == "visible",
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            ).all()
        )
        siblings.sort(
            key=lambda candidate: (
                candidate.sort_key,
                candidate.title,
                str(candidate.id),
            )
        )
        index = next(
            (
                position
                for position, candidate in enumerate(siblings)
                if candidate.id == node.id
            ),
            -1,
        )
        if index < 0:
            raise ReadNotFoundError("tree node not found")

        if isinstance(reorder, TreeReorderByAnchorRequest):
            anchor = require_visible_tree_node(
                session,
                workspace_id,
                reorder.anchor_node_id,
                actor,
                self.tree_read_service,
            )
            if (
                anchor.id == node.id
                or anchor.visibility_status != "visible"
                or anchor.is_system_node
                or anchor.section != node.section
                or anchor.parent_id != node.parent_id
            ):
                raise RejectedByPolicyError(
                    "reorder anchor must be a visible non-system sibling",
                    error_code="invalid_tree_reorder_anchor",
                )
            without_node = [
                candidate for candidate in siblings if candidate.id != node.id
            ]
            anchor_index = next(
                (
                    position
                    for position, candidate in enumerate(without_node)
                    if candidate.id == anchor.id
                ),
                -1,
            )
            if anchor_index < 0:
                raise RejectedByPolicyError(
                    "reorder anchor must be a visible non-system sibling",
                    error_code="invalid_tree_reorder_anchor",
                )
            insertion_index = anchor_index + (1 if reorder.placement == "after" else 0)
            reordered_siblings = list(without_node)
            reordered_siblings.insert(insertion_index, node)
            # LEAK-04: the direction form refuses to move a node past a system
            # folder (below); the anchor form must refuse the same move, or
            # the two spellings of one operation enforce two different
            # policies. A system sibling's position relative to the moved
            # node must be unchanged by this reorder.
            order_before = [candidate.id for candidate in siblings]
            order_after = [candidate.id for candidate in reordered_siblings]
            for system_sibling in siblings:
                if not system_sibling.is_system_node:
                    continue
                was_before = order_before.index(node.id) < order_before.index(
                    system_sibling.id
                )
                now_before = order_after.index(node.id) < order_after.index(
                    system_sibling.id
                )
                if was_before != now_before:
                    raise RejectedByPolicyError(
                        "cannot reorder a node past a system folder",
                        error_code="system_tree_node_protected",
                    )
            reason = (
                f"reorder tree node {node.title} {reorder.placement} {anchor.title}"
            )
            diff_summary = f"reorder {reorder.placement} anchor"
            audit_payload = {
                "anchor_node_id": str(anchor.id),
                "placement": reorder.placement,
            }
        else:
            target_index = index - 1 if reorder.direction == "up" else index + 1
            if target_index < 0 or target_index >= len(siblings):
                raise RejectedByPolicyError(
                    "tree node is already at the edge",
                    error_code="tree_reorder_out_of_range",
                )
            if siblings[target_index].is_system_node:
                raise RejectedByPolicyError(
                    "cannot reorder a node past a system folder",
                    error_code="system_tree_node_protected",
                )
            reordered_siblings = list(siblings)
            reordered_siblings[index], reordered_siblings[target_index] = (
                reordered_siblings[target_index],
                reordered_siblings[index],
            )
            reason = f"reorder tree node {node.title} {reorder.direction}"
            diff_summary = f"reorder {reorder.direction}"
            audit_payload = {"direction": reorder.direction}

        # An explicit move immediately before its next sibling or immediately
        # after its previous sibling is already satisfied. Do not heal gaps or
        # duplicate keys here: a no-op must not churn any sort key or audit row.
        if [candidate.id for candidate in reordered_siblings] == [
            candidate.id for candidate in siblings
        ]:
            return self.tree_read_service.get_node(session, workspace_id, node.id)

        # Renumber the whole visible sibling set gap-free so the swap is
        # deterministic and any pre-existing duplicate sort_keys are healed.
        for position, sibling in enumerate(reordered_siblings, start=1):
            next_key = f"{position:04d}"
            if sibling.sort_key != next_key:
                sibling.sort_key = next_key
        change_set = create_tree_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=reason,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="workspace_tree_node",
                target_id=node.id,
                operation="tree_reorder",
                diff_summary=diff_summary,
                diff_ref=f"parent:{node.parent_id};sort:{node.sort_key}",
            )
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="tree.node.reorder",
            target_kind="workspace_tree_node",
            target_id=node.id,
            payload={
                "change_set_id": str(change_set.id),
                **audit_payload,
            },
        )
        session.flush()
        return self.tree_read_service.get_node(session, workspace_id, node.id)

    def archive_node(
        self,
        session: Session,
        workspace_id: UUID,
        node_id: UUID,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> TreeNodePayload:
        # Only the AI-carrying channels are refused. "internal" covers
        # background jobs and service-to-service calls, which are not an agent
        # reaching in over MCP; denying everything that is not a browser session
        # would take the maintenance paths down with it.
        if actor is not None and actor.provenance in AI_PROVENANCES:
            raise RejectedByPolicyError(
                "AI-authenticated actors cannot archive wiki subtrees"
            )
        node = require_visible_tree_node(
            session, workspace_id, node_id, actor, self.tree_read_service
        )
        if node.is_system_node or node.parent_id is None:
            raise RejectedByPolicyError(
                "system tree node cannot be deleted",
                error_code="system_tree_node_protected",
            )
        archived_nodes = collect_descendants(session, workspace_id, node)
        archived_nodes.insert(0, node)
        # H-03: archiving a folder cascades to its whole subtree (and the
        # underlying AgentPage/Context/Library lifecycle). The root passing the
        # visibility gate is not enough — refuse if any descendant is a node the
        # actor cannot read, so an editor cannot archive a sensitive AgentPage by
        # hiding it under a visible folder.
        if actor is not None and not self.tree_read_service.all_nodes_visible_to_actor(
            session, archived_nodes, actor
        ):
            raise RejectedByPolicyError(
                "cannot archive a subtree that contains items you cannot access",
                error_code="tree_archive_contains_hidden",
            )
        for archived_node in archived_nodes:
            archived_node.visibility_status = "archived"
        change_set = create_tree_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=f"delete tree node {node.title}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        # B-8: entity-ref archive cascades to the target entity lifecycle
        # (folder archive includes descendant pages) — an archived page must
        # leave search/index/read_concept together with the tree, not linger
        # as a tree-invisible-but-searchable ghost (design A-3 twin).
        _cascade_archive_to_targets(
            session,
            workspace_id=workspace_id,
            nodes=archived_nodes,
            change_set_id=change_set.id,
        )
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="workspace_tree_node",
                target_id=node.id,
                operation="tree_delete",
                diff_summary=f"archive {node.title}",
                diff_ref=",".join(
                    str(archived_node.id) for archived_node in archived_nodes
                ),
            )
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="tree.node.delete",
            target_kind="workspace_tree_node",
            target_id=node.id,
            payload={
                "change_set_id": str(change_set.id),
                "archived_node_ids": [
                    str(archived_node.id) for archived_node in archived_nodes
                ],
            },
        )
        session.flush()
        _refresh_tree_edges(
            session,
            workspace_id,
            {archived_node.id for archived_node in archived_nodes},
            remove_outgoing=True,
        )
        return self.tree_read_service.get_node(session, workspace_id, node.id)


def ensure_primary_tree_placement(
    session: Session,
    workspace_id: UUID,
    target_kind: str,
    target_id: UUID,
    title: str,
    section: str,
    parent_id: UUID | None,
    default_parent_slug: str,
    actor_kind: str,
    actor_id: str,
    slug: str | None = None,
    can_have_children: bool = False,
    move_existing: bool = False,
) -> WorkspaceTreeNode:
    if target_kind not in ENTITY_NODE_KINDS:
        raise RejectedByPolicyError(f"unsupported tree target kind: {target_kind}")
    # Look up regardless of visibility: the partial unique on
    # (workspace, target_kind, target_id) ignores visibility, so an archived
    # primary node must be REVIVED, never duplicated (A-3: archive-then-rewrite
    # used to 500 on the unique).
    existing = primary_node_for_target(
        session=session,
        workspace_id=workspace_id,
        target_kind=target_kind,
        target_id=target_id,
        include_archived=True,
    )
    if existing is not None:
        title_changed = existing.title != title
        revived = existing.visibility_status != "visible"
        if revived:
            _revive_primary_node(
                session,
                workspace_id=workspace_id,
                node=existing,
                section=section,
                default_parent_slug=default_parent_slug,
                explicit_parent_id=parent_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
        existing.title = title
        existing.slug = slug
        moved = False
        if move_existing and parent_id is not None:
            parent = resolve_tree_parent(
                session=session,
                workspace_id=workspace_id,
                section=section,
                parent_id=parent_id,
                default_parent_slug=default_parent_slug,
            )
            if existing.id == parent.id or is_descendant(session, parent, existing.id):
                raise RejectedByPolicyError("tree cycle detected")
            if existing.parent_id != parent.id:
                existing.parent_id = parent.id
                existing.sort_key = next_child_sort_key(
                    session=session,
                    workspace_id=workspace_id,
                    parent_id=parent.id,
                )
                moved = True
        session.add(existing)
        session.flush()
        if revived or moved:
            # revive/reparent changes placement edges; without this the
            # graph keeps serving the stale parent until an unrelated tree
            # mutation or reindex (write-through contract, C2-T5)
            _refresh_tree_edges(session, workspace_id, {existing.id})
        if revived or moved or title_changed:
            _sync_subtree_search_doc_sources(session, workspace_id, existing)
        return existing

    parent = resolve_tree_parent(
        session=session,
        workspace_id=workspace_id,
        section=section,
        parent_id=parent_id,
        default_parent_slug=default_parent_slug,
    )
    return create_tree_node(
        session,
        workspace_id=workspace_id,
        section=section,
        node_kind=ENTITY_NODE_KINDS[target_kind],
        parent_id=parent.id,
        title=title,
        slug=slug,
        target_kind=target_kind,
        target_id=target_id,
        can_have_children=can_have_children,
        primary_placement=True,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )


def section_root_node(
    session: Session,
    workspace_id: UUID,
    section: str,
) -> WorkspaceTreeNode:
    """Resolve the section root from TreePolicy.fixed_root_node_ids (B-8).

    Policy wins; the historical slug convention (slug == section name) is the
    explicit fallback for workspaces without a policy row.
    """
    policy = session.get(TreePolicy, (workspace_id, section))
    if policy is not None:
        for root_id in policy.fixed_root_uuid_ids:
            node = get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=workspace_id,
                resource_id=root_id,
            )
            if (
                node is not None
                and node.section == section
                and node.visibility_status == "visible"
            ):
                return node
    root_slug = "library" if section == "library" else section
    node = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == section,
            WorkspaceTreeNode.slug == root_slug,
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )
    if node is None:
        raise ReadNotFoundError("tree section root not found")
    return node


def ensure_context_inbox_node(
    session: Session,
    workspace_id: UUID,
) -> WorkspaceTreeNode:
    """Return the canonical Context Inbox, creating it exactly once.

    The visible-folder sibling uniqueness index is the concurrency boundary.
    The dialect-native conflict-ignore insert lets concurrent provisioners or
    lazy writers converge on the winner without poisoning the outer write
    transaction.
    """
    root = section_root_node(session, workspace_id, "context")
    inbox_query = select(WorkspaceTreeNode).where(
        WorkspaceTreeNode.workspace_id == workspace_id,
        WorkspaceTreeNode.section == "context",
        WorkspaceTreeNode.node_kind == "folder",
        WorkspaceTreeNode.parent_id == root.id,
        WorkspaceTreeNode.slug == "inbox",
        WorkspaceTreeNode.visibility_status == "visible",
    )
    inbox = session.scalar(inbox_query)
    if inbox is not None:
        # Adopt whatever is already there rather than rewriting it. A workspace
        # may have made its own folder at this slug, and quietly renaming it to
        # "Inbox" and stamping it a system node -- undeletable, unrenameable --
        # is not a side effect a document write is allowed to have. A folder
        # that cannot take children is not usable as a target, so that is an
        # ensure failure and the caller's root fallback applies.
        if not inbox.can_have_children:
            raise RejectedByPolicyError("the Context inbox node cannot contain pages")
        return inbox

    node_id = uuid4()
    created_at = utc_now()
    values = {
        "id": node_id,
        "workspace_id": workspace_id,
        "section": "context",
        "node_kind": "folder",
        "parent_id": root.id,
        "sort_key": next_child_sort_key(session, workspace_id, root.id),
        "title": "Inbox",
        "slug": "inbox",
        "target_kind": None,
        "target_id": None,
        "is_system_node": True,
        "can_have_children": True,
        "primary_placement": True,
        "visibility_status": "visible",
        "created_by_actor_type": "system",
        "created_by_actor_id": default_actor_id(),
        "created_at": created_at,
        "updated_at": created_at,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(WorkspaceTreeNode).values(**values)
    elif dialect == "postgresql":
        statement = postgresql_insert(WorkspaceTreeNode).values(**values)
    else:
        raise RuntimeError(f"unsupported tree dialect: {dialect}")

    with session.begin_nested():
        inserted_id = session.scalar(
            statement.on_conflict_do_nothing().returning(WorkspaceTreeNode.id)
        )
        if inserted_id is not None:
            _refresh_tree_edges(session, workspace_id, {inserted_id})

    inbox = session.scalar(inbox_query.execution_options(populate_existing=True))
    if inbox is None:
        raise RuntimeError("Context Inbox ensure did not produce a visible folder")
    return inbox


def resolve_tree_parent(
    session: Session,
    workspace_id: UUID,
    section: str,
    parent_id: UUID | None,
    default_parent_slug: str | None = None,
) -> WorkspaceTreeNode:
    if parent_id is None:
        root = section_root_node(session, workspace_id, section)
        if section == "context" and default_parent_slug == "inbox":
            try:
                return ensure_context_inbox_node(session, workspace_id)
            except Exception:
                # Inbox is a resilient default, not a reason to lose the page.
                # The ensure contains its mutation in a SAVEPOINT, so the outer
                # write can safely retain the historical section-root fallback.
                logger.exception(
                    "failed to ensure Context Inbox; using section root",
                    extra={"workspace_id": str(workspace_id)},
                )
                return root
        if default_parent_slug and default_parent_slug != (root.slug or ""):
            child = session.scalar(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.section == section,
                    WorkspaceTreeNode.parent_id == root.id,
                    WorkspaceTreeNode.slug == default_parent_slug,
                    WorkspaceTreeNode.visibility_status == "visible",
                )
            )
            if child is not None:
                return child
            # Legacy fallback: the default parent may live deeper in the
            # section (pre-policy trees); resolve by slug anywhere visible.
            anywhere = session.scalar(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.section == section,
                    WorkspaceTreeNode.slug == default_parent_slug,
                    WorkspaceTreeNode.visibility_status == "visible",
                )
            )
            if anywhere is not None:
                return anywhere
        return root

    parent = require_tree_node(session, workspace_id, parent_id)
    if parent.section != section:
        raise RejectedByPolicyError("tree parent section mismatch")
    if not parent.can_have_children:
        raise RejectedByPolicyError("tree parent cannot have children")
    if parent.visibility_status != "visible":
        raise RejectedByPolicyError("tree parent is not visible")
    return parent


def primary_node_for_target(
    session: Session,
    workspace_id: UUID,
    target_kind: str,
    target_id: UUID,
    include_archived: bool = False,
) -> WorkspaceTreeNode | None:
    statement = select(WorkspaceTreeNode).where(
        WorkspaceTreeNode.workspace_id == workspace_id,
        WorkspaceTreeNode.target_kind == target_kind,
        WorkspaceTreeNode.target_id == target_id,
        WorkspaceTreeNode.primary_placement.is_(True),
    )
    if not include_archived:
        statement = statement.where(WorkspaceTreeNode.visibility_status == "visible")
    return session.scalar(statement)


def require_tree_node(
    session: Session,
    workspace_id: UUID,
    node_id: UUID,
) -> WorkspaceTreeNode:
    node = get_in_workspace(
        session,
        WorkspaceTreeNode,
        workspace_id=workspace_id,
        resource_id=node_id,
    )
    if node is None:
        raise ReadNotFoundError("tree node not found")
    return node


def require_visible_tree_node(
    session: Session,
    workspace_id: UUID,
    node_id: UUID,
    actor: ActorContext | None,
    tree_read_service: TreeReadService,
) -> WorkspaceTreeNode:
    """Load a tree node for a direct-ID MUTATION, enforcing the SAME actor
    visibility the GET path applies.

    H-03: the editor role gate on the write endpoints is weaker than node
    visibility (a sensitive AgentPage node needs owner), so a direct-ID
    mutation must 404 on a node the actor cannot read, exactly like GET.
    """

    node = require_tree_node(session, workspace_id, node_id)
    if actor is not None and not tree_read_service.node_visible_to_actor(
        session, node, actor
    ):
        raise ReadNotFoundError("tree node not found")
    return node


def is_descendant(
    session: Session,
    candidate: WorkspaceTreeNode,
    ancestor_id: UUID,
) -> bool:
    current = candidate
    while current.parent_id is not None:
        if current.parent_id == ancestor_id:
            return True
        parent = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=candidate.workspace_id,
            resource_id=current.parent_id,
        )
        if parent is None:
            return False
        current = parent
    return False


def collect_descendants(
    session: Session,
    workspace_id: UUID,
    node: WorkspaceTreeNode,
) -> list[WorkspaceTreeNode]:
    descendants: list[WorkspaceTreeNode] = []
    stack = [node.id]
    while stack:
        parent_id = stack.pop()
        children = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.parent_id == parent_id,
                WorkspaceTreeNode.visibility_status == "visible",
            )
        ).all()
        descendants.extend(children)
        stack.extend(child.id for child in children)
    return descendants


def create_tree_change_set(
    session: Session,
    workspace_id: UUID,
    reason: str,
    actor_kind: str,
    actor_id: str,
) -> ChangeSet:
    if reason is not None:
        validate_text(reason, field="reason", max_length=2000)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise ReadNotFoundError("workspace not found")
    risk_resolution = ReviewRiskResolver().resolve(
        workspace=workspace,
        event="tree_mutation",
    )
    change_set = ChangeSet(
        workspace_id=workspace_id,
        reason=reason,
        trigger_type="tree_mutation",
        actor_type=actor_kind,
        actor_id=parse_uuid_or_none(actor_id),
        status="applied",
        risk_level=risk_resolution.risk_level,
    )
    session.add(change_set)
    session.flush()
    return change_set


def next_child_sort_key(session: Session, workspace_id: UUID, parent_id: UUID) -> str:
    """Allocate the next sort key as max+1 over ALL siblings.

    Counting only visible siblings reused keys after an archive (two
    children could land on the same sort_key); max+1 over every row is
    collision-free regardless of visibility churn.
    """
    keys = session.scalars(
        select(WorkspaceTreeNode.sort_key).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.parent_id == parent_id,
        )
    ).all()
    highest = 0
    for key in keys:
        try:
            highest = max(highest, int(key))
        except (TypeError, ValueError):
            continue
    return f"{highest + 1:04d}"


def sibling_slug_exists(
    session: Session,
    workspace_id: UUID,
    parent_id: UUID,
    slug: str,
) -> bool:
    return (
        session.scalar(
            select(func.count(WorkspaceTreeNode.id)).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.parent_id == parent_id,
                WorkspaceTreeNode.slug == slug,
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )
        > 0
    )


def _refresh_tree_edges(
    session: Session,
    workspace_id: UUID,
    node_ids: set[UUID],
    *,
    remove_outgoing: bool = False,
) -> None:
    from backend.services.indexing.edge_registry import (
        refresh_tree_node_edge_projection,
    )

    refresh_tree_node_edge_projection(
        session,
        workspace_id,
        node_ids,
        remove_outgoing=remove_outgoing,
    )


def _sync_subtree_search_doc_sources(
    session: Session,
    workspace_id: UUID,
    node: WorkspaceTreeNode,
) -> None:
    """Refresh breadcrumb-bearing search documents after a tree mutation."""

    from backend.models.agent import AgentPage
    from backend.models.context import ContextPage
    from backend.services.indexing.fts5 import (
        sync_agent_page_doc_sources,
        sync_context_page_doc_sources,
    )

    subtree = [node, *collect_descendants(session, workspace_id, node)]
    context_ids = {
        candidate.target_id
        for candidate in subtree
        if candidate.target_kind == "context_page" and candidate.target_id is not None
    }
    agent_ids = {
        candidate.target_id
        for candidate in subtree
        if candidate.target_kind == "agent_page" and candidate.target_id is not None
    }
    if context_ids:
        pages = session.scalars(
            select(ContextPage).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id.in_(context_ids),
            )
        ).all()
        for page in pages:
            sync_context_page_doc_sources(session, page)
    if agent_ids:
        agent_pages = session.scalars(
            select(AgentPage).where(
                AgentPage.workspace_id == workspace_id,
                AgentPage.id.in_(agent_ids),
            )
        ).all()
        for agent_page in agent_pages:
            sync_agent_page_doc_sources(session, agent_page)


def create_tree_node(
    session: Session,
    *,
    workspace_id: UUID,
    section: str,
    node_kind: str,
    parent_id: UUID | None,
    title: str,
    actor_kind: str,
    actor_id: str,
    slug: str | None = None,
    target_kind: str | None = None,
    target_id: UUID | None = None,
    is_system_node: bool = False,
    can_have_children: bool = True,
    primary_placement: bool = False,
    sort_key: str | None = None,
) -> WorkspaceTreeNode:
    """Single WorkspaceTreeNode constructor (B-8 unification).

    Every creation path (folders, primary placements, ask session/day nodes)
    goes through here so sort-key allocation and field assembly cannot drift.
    """
    node = WorkspaceTreeNode(
        workspace_id=workspace_id,
        section=section,
        node_kind=node_kind,
        parent_id=parent_id,
        sort_key=sort_key
        if sort_key is not None
        else next_child_sort_key(
            session=session,
            workspace_id=workspace_id,
            parent_id=parent_id,
        ),
        title=title,
        slug=slug,
        target_kind=target_kind,
        target_id=target_id,
        is_system_node=is_system_node,
        can_have_children=can_have_children,
        primary_placement=primary_placement,
        visibility_status="visible",
        created_by_actor_type=actor_kind,
        created_by_actor_id=parse_uuid_or_none(actor_id),
    )
    session.add(node)
    session.flush()
    _refresh_tree_edges(session, workspace_id, {node.id})
    return node


def folder_slug(title: str) -> str:
    slug = "".join(
        character.lower() if character.isalnum() else "-" for character in title.strip()
    )
    parts = [part for part in slug.split("-") if part]
    return "-".join(parts)[:80] or "folder"


def _cascade_archive_to_targets(
    session: Session,
    *,
    workspace_id: UUID,
    nodes: list[WorkspaceTreeNode],
    change_set_id: UUID,
) -> None:
    """Archive the entities behind archived tree nodes (B-8).

    Context/agent pages and library items follow the tree into the archived
    lifecycle; context pages also drop out of the name index immediately and
    leave an ``archive`` ChangeOperation so ``what_changed`` can surface the
    delta. Library artifact refs are gated by their parent item (no-op).
    """
    from backend.models.agent import AgentPage
    from backend.models.context import ContextPage
    from backend.models.library import LibraryItem
    from backend.services.indexing.db_native import remove_document_chunks
    from backend.services.indexing.fts5 import (
        sync_agent_page_doc_sources,
        sync_library_doc_sources,
    )
    from backend.services.page_service import sync_page_projections
    from backend.services.public_page_exposure import clear_public_page_exposure

    for node in nodes:
        if node.target_kind == "context_page" and node.target_id is not None:
            page = get_in_workspace(
                session,
                ContextPage,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            if page is None:
                continue
            if page.lifecycle_status == "archived":
                continue
            page.lifecycle_status = "archived"
            # Archiving revokes public exposure until an operator republishes it.
            clear_public_page_exposure(page)
            head_id = page.current_published_version_id or page.current_version_id
            session.add(
                ChangeOperation(
                    workspace_id=workspace_id,
                    change_set_id=change_set_id,
                    target_type="context_page",
                    target_id=page.id,
                    operation="archive",
                    diff_summary=f"archive {page.title}",
                    diff_ref=(f"context_page_version:{head_id}" if head_id else None),
                )
            )
            remove_document_chunks(
                session,
                document_kind="context_page",
                document_id=page.id,
            )
            sync_page_projections(session, page=page)
        elif node.target_kind == "agent_page" and node.target_id is not None:
            agent_page = get_in_workspace(
                session,
                AgentPage,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            if agent_page is not None:
                agent_page.lifecycle_status = "archived"
                sync_agent_page_doc_sources(session, agent_page)
        elif node.target_kind == "library_item" and node.target_id is not None:
            item = get_in_workspace(
                session,
                LibraryItem,
                workspace_id=workspace_id,
                resource_id=node.target_id,
            )
            if item is not None:
                item.lifecycle_status = "archived"
                sync_library_doc_sources(session, item)
    session.flush()


def _revive_primary_node(
    session: Session,
    *,
    workspace_id: UUID,
    node: WorkspaceTreeNode,
    section: str,
    default_parent_slug: str,
    explicit_parent_id: UUID | None,
    actor_kind: str,
    actor_id: str,
) -> None:
    """Bring an archived primary-placement node (and its target) back.

    An archived ancestor chain is NOT mass-revived (deliberate folder archives
    stay archived): the node reparents to the section default parent instead.
    Target lifecycle restores deterministically from its published/current
    head, then projections resync.
    """
    from backend.models.agent import AgentPage
    from backend.models.context import ContextPage, ContextPageVersion
    from backend.models.library import LibraryItem
    from backend.services.context_lifecycle import record_unarchive
    from backend.services.indexing.fts5 import (
        sync_agent_page_doc_sources,
        sync_library_doc_sources,
    )
    from backend.services.page_service import sync_page_projections

    context_page = None
    if node.target_kind == "context_page" and node.target_id is not None:
        candidate_page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=node.target_id,
        )
        if candidate_page is not None:
            context_page = candidate_page
            visible_version_ids = {
                version_id
                for version_id in (
                    context_page.latest_draft_version_id,
                    context_page.current_version_id,
                    context_page.current_published_version_id,
                )
                if version_id is not None
            }
            for version_id in visible_version_ids:
                version = get_in_workspace(
                    session,
                    ContextPageVersion,
                    workspace_id=workspace_id,
                    resource_id=version_id,
                )
                if version is not None and version.page_id == context_page.id:
                    require_no_visible_legacy_media(
                        workspace_id=workspace_id,
                        body_markdown=version.body_md,
                    )

    node.visibility_status = "visible"
    reparent_to: WorkspaceTreeNode | None = None
    if explicit_parent_id is not None:
        explicit_parent = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=workspace_id,
            resource_id=explicit_parent_id,
        )
        if (
            explicit_parent is not None
            and explicit_parent.section == section
            and explicit_parent.visibility_status == "visible"
            and explicit_parent.can_have_children
        ):
            reparent_to = explicit_parent
        else:
            # Stale placement pointer (the remembered parent was archived
            # with its folder): a rewrite must revive under the section
            # default, not fail rejected_by_policy (A-3 semantics).
            reparent_to = resolve_tree_parent(
                session=session,
                workspace_id=workspace_id,
                section=section,
                parent_id=None,
                default_parent_slug=default_parent_slug,
            )
    elif _has_archived_ancestor(session, workspace_id, node):
        reparent_to = resolve_tree_parent(
            session=session,
            workspace_id=workspace_id,
            section=section,
            parent_id=None,
            default_parent_slug=default_parent_slug,
        )
    if reparent_to is not None and reparent_to.id != node.parent_id:
        if reparent_to.id == node.id or is_descendant(session, reparent_to, node.id):
            raise RejectedByPolicyError("tree cycle detected")
        node.parent_id = reparent_to.id
    # Always reallocate the sort key: reorder renumbers VISIBLE siblings
    # gap-free, so the key this node held while archived may have been
    # handed to another sibling in the meantime.
    node.sort_key = next_child_sort_key(
        session=session,
        workspace_id=workspace_id,
        parent_id=node.parent_id,
    )

    if node.target_kind == "context_page" and node.target_id is not None:
        page = context_page or get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=node.target_id,
        )
        if page is not None:
            was_archived = page.lifecycle_status == "archived"
            page.lifecycle_status = (
                "published" if page.current_published_version_id else "draft"
            )
            if was_archived:
                record_unarchive(
                    session, page=page, actor_kind=actor_kind, actor_id=actor_id
                )
            sync_page_projections(session, page=page)
    elif node.target_kind == "agent_page" and node.target_id is not None:
        agent_page = get_in_workspace(
            session,
            AgentPage,
            workspace_id=workspace_id,
            resource_id=node.target_id,
        )
        if agent_page is not None:
            agent_page.lifecycle_status = (
                "active" if agent_page.current_active_version_id else "draft"
            )
            sync_agent_page_doc_sources(session, agent_page)
    elif node.target_kind == "library_item" and node.target_id is not None:
        item = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=workspace_id,
            resource_id=node.target_id,
        )
        if item is not None:
            item.lifecycle_status = _revived_library_lifecycle(session, item)
            sync_library_doc_sources(session, item)
    session.flush()


def _revived_library_lifecycle(session: Session, item) -> str:
    """Revive as ready only when the canonical version has selected artifacts.

    A newer conversion still in flight does not hide the prior ready projection.
    """
    from backend.services.library_canonical import (
        get_canonical_library_version,
        list_canonical_library_artifacts,
    )

    if item.current_version_id is None and item.current_ready_version_id is None:
        return "registered"
    version = get_canonical_library_version(session, item)
    if version is None:
        return "extract_pending"
    artifacts = list_canonical_library_artifacts(session, version)
    return "ready" if artifacts else "extract_pending"


def _has_archived_ancestor(
    session: Session,
    workspace_id: UUID,
    node: WorkspaceTreeNode,
) -> bool:
    parent_id = node.parent_id
    seen: set[UUID] = set()
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        parent = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=workspace_id,
            resource_id=parent_id,
        )
        if parent is None:
            return False
        if parent.visibility_status != "visible":
            return True
        parent_id = parent.parent_id
    return False
