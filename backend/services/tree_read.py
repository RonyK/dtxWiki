from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.favorite import UserFavorite
from backend.models.library import LibraryItem
from backend.models.review import ReviewItem
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.read import (
    TreeListData,
    TreeNodePayload,
    TreePathEntry,
    TreeSectionPolicyPayload,
)
from backend.services.read_errors import ReadNotFoundError

if TYPE_CHECKING:
    from backend.policy.auth import ActorContext


class TreeReadService:
    def list_tree(
        self,
        session: Session,
        workspace_id: UUID,
        section: str | None = None,
        include_archived: bool = False,
        actor: "ActorContext | None" = None,
    ) -> list[TreeNodePayload]:
        return self.list_tree_data(
            session=session,
            workspace_id=workspace_id,
            section=section,
            include_archived=include_archived,
            actor=actor,
        ).nodes

    def list_tree_data(
        self,
        session: Session,
        workspace_id: UUID,
        section: str | None = None,
        include_archived: bool = False,
        actor: "ActorContext | None" = None,
    ) -> TreeListData:
        """List the tree through the shared actor-visible projection (B-7).

        ``actor=None`` keeps the internal/system full view; with an actor,
        entity-ref nodes whose targets the actor may not see (draft-only
        pages, restricted Library items — A-9) are dropped, and the badge
        counts come from the same in-memory projection (no per-node N+1).
        """
        from backend.services.tree_projection import TreeProjectionService

        projection = TreeProjectionService().load(
            session,
            workspace_id,
            actor,
            sections=(section,) if section is not None else None,
            include_archived=include_archived,
        )
        visible_nodes = [
            node for node in projection.nodes.values() if projection.is_visible(node.id)
        ]
        auto_summaries = self._auto_summaries(session, projection, visible_nodes)
        target_metadata = self._target_metadata(session, visible_nodes)
        target_states = self._target_states(session, workspace_id, visible_nodes, actor)
        return TreeListData(
            nodes=[
                self._node_payload_from_projection(
                    projection,
                    node,
                    nav_summary_auto=auto_summaries.get(node.id),
                    target_meta=target_metadata.get(node.id, {}),
                    target_state=target_states.get(
                        (node.target_kind, node.target_id), {}
                    ),
                )
                for node in depth_first_tree_nodes(visible_nodes)
            ],
            policies=self._policy_payloads(
                session=session,
                workspace_id=workspace_id,
                section=section,
            ),
        )

    @staticmethod
    def _auto_summaries(
        session: Session,
        projection,
        visible_nodes,
    ) -> dict[UUID, str]:
        """Batched 12.1 heuristic for folders without an authored summary —
        the SAME wording the index renders, one page_type query for the whole
        tree (the projection query budget stays bounded)."""
        from backend.models.context import ContextPage
        from backend.services.okf_index import (
            _HEURISTIC_TITLE_ROLLUP,
            heuristic_summary_text,
        )

        folders = [
            node
            for node in visible_nodes
            if node.node_kind == "folder" and node.nav_summary is None
        ]
        if not folders:
            return {}
        rollups: dict[UUID, tuple[list[str], list[UUID]]] = {}
        all_page_ids: set[UUID] = set()
        for folder in folders:
            children = projection.visible_children(folder.id, folders_first=True)
            titles = [child.title for child in children[:_HEURISTIC_TITLE_ROLLUP]]
            page_ids = [
                child.target_id
                for child in children
                if child.target_kind == "context_page" and child.target_id is not None
            ]
            all_page_ids.update(page_ids)
            rollups[folder.id] = (titles, page_ids)
        type_by_page: dict[UUID, str] = {}
        if all_page_ids:
            type_by_page = dict(
                session.execute(
                    select(ContextPage.id, ContextPage.page_type).where(
                        ContextPage.id.in_(all_page_ids)
                    )
                ).all()
            )
        summaries: dict[UUID, str] = {}
        for folder_id, (titles, page_ids) in rollups.items():
            type_counts: dict[str, int] = {}
            for page_id in page_ids:
                page_type = type_by_page.get(page_id)
                if page_type is not None:
                    type_counts[page_type] = type_counts.get(page_type, 0) + 1
            text = heuristic_summary_text(titles, type_counts)
            if text:
                summaries[folder_id] = text
        return summaries

    def _node_payload_from_projection(
        self,
        projection,
        node: WorkspaceTreeNode,
        nav_summary_auto: str | None = None,
        target_meta: dict[str, str] | None = None,
        target_state: dict[str, Any] | None = None,
    ) -> TreeNodePayload:
        breadcrumb = [self._path_entry(node)]
        current = node
        while current.parent_id is not None:
            parent = projection.nodes.get(current.parent_id)
            # only actor-visible ancestors may be named (the projection's
            # ancestor closure guarantees this for visible nodes; keep the
            # guard so a breadcrumb can never widen visibility)
            if parent is None or not projection.is_visible(parent.id):
                break
            breadcrumb.append(self._path_entry(parent))
            current = parent
        return TreeNodePayload(
            **self._path_entry(node).model_dump(),
            parent_id=node.parent_id,
            sort_key=node.sort_key,
            nav_summary=node.nav_summary,
            nav_summary_auto=nav_summary_auto,
            target_kind=node.target_kind,
            target_id=node.target_id,
            target_meta=target_meta or {},
            **(target_state or {}),
            is_system_node=node.is_system_node,
            can_have_children=node.can_have_children,
            primary_placement=node.primary_placement,
            visibility_status=node.visibility_status,
            children_count=projection.children_count(node.id),
            has_children=projection.has_children(node.id),
            breadcrumb=list(reversed(breadcrumb)),
        )

    def get_node(
        self,
        session: Session,
        workspace_id: UUID,
        node_id: UUID,
        actor: "ActorContext | None" = None,
    ) -> TreeNodePayload:
        node = session.get(WorkspaceTreeNode, node_id)
        if node is None or node.workspace_id != workspace_id:
            raise ReadNotFoundError("tree node not found")
        # An access surface (REST GET /tree/nodes/{id}) passes the actor so a
        # single-node read is gated by the SAME projection visibility as the
        # list — otherwise target_meta (sensitive agent category, draft
        # lifecycle) leaks to a viewer who guesses a node id. Internal/write
        # callers pass actor=None: they hold their own role gate and want the
        # node they just acted on back regardless of visibility.
        if actor is not None:
            from backend.policy.trust_boundaries import actor_can_read_drafts

            nodes = self.list_tree_data(
                session,
                workspace_id,
                section=node.section,
                include_archived=actor_can_read_drafts(actor),
                actor=actor,
            ).nodes
            payload = next(
                (payload for payload in nodes if payload.id == node_id), None
            )
            if payload is None:
                raise ReadNotFoundError("tree node not found")
            return payload
        return self._node_payload(session, node)

    @staticmethod
    def node_visible_to_actor(
        session: Session,
        node: WorkspaceTreeNode,
        actor: "ActorContext",
    ) -> bool:
        """``TreeProjectionService.load(...).is_visible`` for one node.

        A node is visible iff its own target and its whole ancestor chain are
        (``_ancestor_closure``), so evaluating that chain gives the projection's
        answer without loading every node and target in the section.
        """
        from backend.policy.trust_boundaries import actor_can_read_drafts
        from backend.services.tree_projection import TreeProjectionService

        # archived nodes are an editor affordance; a viewer's single-node
        # read of an archived node degrades to not_found like the list.
        include_archived = actor_can_read_drafts(actor)
        chain: list[WorkspaceTreeNode] = []
        current: WorkspaceTreeNode | None = node
        while current is not None:
            if (
                any(member.id == current.id for member in chain)
                or current.section != node.section
                or (not include_archived and current.visibility_status != "visible")
            ):
                return False
            chain.append(current)
            if current.parent_id is None:
                visible = TreeProjectionService._visible_ids(session, chain, actor)
                return all(member.id in visible for member in chain)
            current = get_in_workspace(
                session,
                WorkspaceTreeNode,
                workspace_id=node.workspace_id,
                resource_id=current.parent_id,
            )
        return False

    @staticmethod
    def visible_node_ids(
        session: Session,
        nodes: list[WorkspaceTreeNode],
        actor: "ActorContext",
    ) -> set[UUID]:
        """Return the subset of node ids visible to the actor, loading ONE
        projection for all sections (avoids an N+1 of per-node projections)."""
        from backend.policy.trust_boundaries import actor_can_read_drafts
        from backend.services.tree_projection import TreeProjectionService

        if not nodes:
            return set()
        sections = tuple({node.section for node in nodes})
        projection = TreeProjectionService().load(
            session,
            nodes[0].workspace_id,
            actor,
            sections=sections,
            include_archived=actor_can_read_drafts(actor),
        )
        return {node.id for node in nodes if projection.is_visible(node.id)}

    @staticmethod
    def all_nodes_visible_to_actor(
        session: Session,
        nodes: list[WorkspaceTreeNode],
        actor: "ActorContext",
    ) -> bool:
        """Whether the actor can see EVERY node in the list (one projection load).

        Used before a subtree mutation (archive) so an editor cannot archive a
        folder whose descendants include a node they cannot read (H-03 cascade).
        """
        from backend.policy.trust_boundaries import actor_can_read_drafts
        from backend.services.tree_projection import TreeProjectionService

        if not nodes:
            return True
        sections = tuple({node.section for node in nodes})
        projection = TreeProjectionService().load(
            session,
            nodes[0].workspace_id,
            actor,
            sections=sections,
            include_archived=actor_can_read_drafts(actor),
        )
        return all(projection.is_visible(node.id) for node in nodes)

    def path_for_target(
        self,
        session: Session,
        workspace_id: UUID,
        target_kind: str,
        target_id: UUID,
    ) -> list[TreePathEntry]:
        node = self.primary_node_for_target(
            session=session,
            workspace_id=workspace_id,
            target_kind=target_kind,
            target_id=target_id,
        )
        if node is None:
            return []
        return self.path_for_node(session=session, node=node)

    def primary_node_for_target(
        self,
        session: Session,
        workspace_id: UUID,
        target_kind: str,
        target_id: UUID,
    ) -> WorkspaceTreeNode | None:
        return session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == target_kind,
                WorkspaceTreeNode.target_id == target_id,
                WorkspaceTreeNode.primary_placement.is_(True),
                WorkspaceTreeNode.visibility_status == "visible",
            )
        )

    def path_for_node(
        self,
        session: Session,
        node: WorkspaceTreeNode,
    ) -> list[TreePathEntry]:
        path = [self._path_entry(node)]
        current = node
        while current.parent_id is not None:
            parent = session.get(WorkspaceTreeNode, current.parent_id)
            if parent is None:
                break
            path.append(self._path_entry(parent))
            current = parent
        return list(reversed(path))

    def parent_node_id_for_target(
        self,
        session: Session,
        workspace_id: UUID,
        target_kind: str,
        target_id: UUID,
    ) -> UUID | None:
        node = self.primary_node_for_target(
            session=session,
            workspace_id=workspace_id,
            target_kind=target_kind,
            target_id=target_id,
        )
        return node.parent_id if node is not None else None

    def children_count(
        self,
        session: Session,
        workspace_id: UUID,
        parent_id: UUID,
    ) -> int:
        """Count direct non-folder children for the visible badge."""
        return int(
            session.scalar(
                select(func.count(WorkspaceTreeNode.id)).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.parent_id == parent_id,
                    WorkspaceTreeNode.visibility_status == "visible",
                    WorkspaceTreeNode.node_kind != "folder",
                )
            )
            or 0
        )

    def has_children(
        self,
        session: Session,
        workspace_id: UUID,
        parent_id: UUID,
    ) -> bool:
        return (
            session.scalar(
                select(WorkspaceTreeNode.id)
                .where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.parent_id == parent_id,
                    WorkspaceTreeNode.visibility_status == "visible",
                )
                .limit(1)
            )
            is not None
        )

    def _node_payload(
        self,
        session: Session,
        node: WorkspaceTreeNode,
    ) -> TreeNodePayload:
        return TreeNodePayload(
            **self._path_entry(node).model_dump(),
            parent_id=node.parent_id,
            sort_key=node.sort_key,
            nav_summary=node.nav_summary,
            target_kind=node.target_kind,
            target_id=node.target_id,
            target_meta=self._target_metadata(session, [node]).get(node.id, {}),
            **self._target_states(session, node.workspace_id, [node], None).get(
                (node.target_kind, node.target_id), {}
            ),
            is_system_node=node.is_system_node,
            can_have_children=node.can_have_children,
            primary_placement=node.primary_placement,
            visibility_status=node.visibility_status,
            children_count=self.children_count(
                session=session,
                workspace_id=node.workspace_id,
                parent_id=node.id,
            ),
            has_children=self.has_children(
                session=session,
                workspace_id=node.workspace_id,
                parent_id=node.id,
            ),
            breadcrumb=self.path_for_node(session=session, node=node),
        )

    @staticmethod
    def _target_metadata(
        session: Session,
        nodes: list[WorkspaceTreeNode],
    ) -> dict[UUID, dict[str, str]]:
        """Hydrate compact UI metadata for all visible target nodes in bulk."""
        context_nodes: dict[UUID, list[UUID]] = {}
        agent_nodes: dict[UUID, list[UUID]] = {}
        for node in nodes:
            if node.target_id is None:
                continue
            if node.target_kind == "context_page":
                context_nodes.setdefault(node.target_id, []).append(node.id)
            elif node.target_kind == "agent_page":
                agent_nodes.setdefault(node.target_id, []).append(node.id)
        metadata: dict[UUID, dict[str, str]] = {}
        if context_nodes:
            for page_id, page_type, lifecycle_status in session.execute(
                select(
                    ContextPage.id,
                    ContextPage.page_type,
                    ContextPage.lifecycle_status,
                ).where(ContextPage.id.in_(context_nodes))
            ).all():
                for node_id in context_nodes[page_id]:
                    metadata[node_id] = {
                        "page_type": page_type,
                        "lifecycle_status": lifecycle_status,
                    }
        if agent_nodes:
            rows = session.execute(
                select(
                    AgentPage.id,
                    AgentPage.category,
                    AgentPage.injection_policy,
                    AgentPage.lifecycle_status,
                ).where(AgentPage.id.in_(agent_nodes))
            ).all()
            for page_id, category, injection_policy, lifecycle_status in rows:
                for node_id in agent_nodes[page_id]:
                    metadata[node_id] = {
                        "category": category,
                        "injection_policy": injection_policy,
                        "lifecycle_status": lifecycle_status,
                    }
        return metadata

    @staticmethod
    def _target_states(
        session: Session,
        workspace_id: UUID,
        nodes: list[WorkspaceTreeNode],
        actor: "ActorContext | None",
    ) -> dict[tuple[str, UUID], dict[str, Any]]:
        """Hydrate personal/lifecycle state in a bounded number of queries.

        Called only after the actor-visible tree projection. Multiple placements
        share a target's state. Draft timestamps never stand in for publication.
        """
        from backend.policy.trust_boundaries import actor_can_read_drafts
        from backend.services.review_service import ReviewService

        targets = {
            kind: {
                node.target_id
                for node in nodes
                if node.target_kind == kind and node.target_id
            }
            for kind in ("context_page", "library_item", "agent_page")
        }
        states: dict[tuple[str, UUID], dict[str, Any]] = {}
        current_drafts: dict[tuple[str, UUID], UUID] = {}
        for kind, model in (
            ("context_page", ContextPage),
            ("library_item", LibraryItem),
            ("agent_page", AgentPage),
        ):
            ids = targets[kind]
            if not ids:
                continue
            for target in session.scalars(
                select(model).where(
                    model.workspace_id == workspace_id, model.id.in_(ids)
                )
            ):
                states[(kind, target.id)] = {
                    "content_changed_at": target.content_changed_at
                }
                if (
                    kind != "library_item"
                    and target.latest_draft_version_id is not None
                ):
                    current_drafts[(kind, target.id)] = target.latest_draft_version_id

        if actor is not None and actor.user_id is not None and states:
            for favorite in session.scalars(
                select(UserFavorite).where(
                    UserFavorite.workspace_id == workspace_id,
                    UserFavorite.user_id == actor.user_id,
                    UserFavorite.target_id.in_({target_id for _, target_id in states}),
                )
            ):
                state = states.get((favorite.target_kind, favorite.target_id))
                if state is not None:
                    state["is_favorite"] = True

        if not current_drafts or not actor_can_read_drafts(actor):
            return states
        valid_refs: dict[tuple[str, UUID], str] = {}
        for kind, model, owner_column in (
            ("context_page", ContextPageVersion, ContextPageVersion.page_id),
            ("agent_page", AgentPageVersion, AgentPageVersion.agent_page_id),
        ):
            ids = {
                version_id
                for (target_kind, _), version_id in current_drafts.items()
                if target_kind == kind
            }
            if not ids:
                continue
            statement = select(model.id, owner_column).where(
                model.workspace_id == workspace_id,
                model.id.in_(ids),
                model.version_status == "draft",
            )
            if kind == "context_page":
                statement = statement.where(ContextPageVersion.graph_status == "linear")
            for version_id, owner_id in session.execute(statement):
                if current_drafts.get((kind, owner_id)) == version_id:
                    valid_refs[(kind, owner_id)] = f"{kind}_version:{version_id}"
        if not valid_refs:
            return states
        statement = (
            select(ReviewItem, ChangeOperation)
            .join(ChangeSet, ChangeSet.id == ReviewItem.change_set_id)
            .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ReviewItem.workspace_id == workspace_id,
                ChangeSet.workspace_id == workspace_id,
                ChangeOperation.workspace_id == workspace_id,
                ReviewItem.status.in_(("pending", "changes_requested")),
                ReviewItem.review_type.in_(("context_update", "agent_page_activation")),
                ChangeOperation.target_id.in_(
                    {target_id for _, target_id in valid_refs}
                ),
                ChangeOperation.diff_ref.in_(set(valid_refs.values())),
            )
            .order_by(ReviewItem.created_at.desc(), ReviewItem.id.desc())
        )
        statement, _ = ReviewService().apply_list_visibility(
            session=session,
            statement=statement,
            workspace_id=workspace_id,
            audience_for=actor.user_id,
            scope="all",
            actor_role=actor.role or actor.workspace_role,
        )
        for review, operation in session.execute(statement):
            key = (operation.target_type, operation.target_id)
            expected_type = {
                "context_page": "context_update",
                "agent_page": "agent_page_activation",
            }.get(operation.target_type)
            if (
                valid_refs.get(key) != operation.diff_ref
                or review.review_type != expected_type
                or "review_status" in states[key]
            ):
                continue
            states[key].update(
                review_status=review.status, review_requested_at=review.created_at
            )
        return states

    @staticmethod
    def _path_entry(node: WorkspaceTreeNode) -> TreePathEntry:
        return TreePathEntry(
            id=node.id,
            section=node.section,
            title=node.title,
            slug=node.slug,
            node_kind=node.node_kind,
        )

    def _policy_payloads(
        self,
        session: Session,
        workspace_id: UUID,
        section: str | None,
    ) -> dict[str, TreeSectionPolicyPayload]:
        statement = select(TreePolicy).where(TreePolicy.workspace_id == workspace_id)
        if section is not None:
            statement = statement.where(TreePolicy.section == section)
        return {
            policy.section: TreeSectionPolicyPayload(
                allow_user_folder_create=policy.allow_user_folder_create,
                allow_page_as_parent=policy.allow_page_as_parent,
            )
            for policy in session.scalars(statement).all()
        }


def depth_first_tree_nodes(
    nodes: list[WorkspaceTreeNode],
) -> list[WorkspaceTreeNode]:
    node_ids = {node.id for node in nodes}
    by_parent: dict[UUID, list[WorkspaceTreeNode]] = {}
    roots: list[WorkspaceTreeNode] = []
    for node in nodes:
        if node.parent_id is not None and node.parent_id in node_ids:
            by_parent.setdefault(node.parent_id, []).append(node)
        else:
            roots.append(node)

    def sort_key(node: WorkspaceTreeNode):
        return (node.section, node.sort_key, node.title, str(node.id))

    roots.sort(key=sort_key)
    for siblings in by_parent.values():
        siblings.sort(key=sort_key)

    ordered: list[WorkspaceTreeNode] = []

    def visit(node: WorkspaceTreeNode) -> None:
        ordered.append(node)
        for child in by_parent.get(node.id, []):
            visit(child)

    for root in roots:
        visit(root)
    return ordered
