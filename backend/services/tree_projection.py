"""Shared actor-visible tree projection (design B-7, fixes A-9).

One load = one node query + at most three batched target-hydration queries +
in-memory aggregation. Both the REST tree and ``get_index`` consume this
layer, so a viewer sees the same node set on every surface: restricted
Library titles and draft-only pages never leak through the tree, and the
per-node ``children_count``/``has_children`` N+1 disappears.

Fanout limits are deliberately NOT applied here — truncation is a render
policy that belongs to the index renderer, not the projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.context import ContextPage
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import (
    actor_can_read_drafts,
    actor_can_read_sensitive_agent_pages,
)
from backend.services.actor_visible_read import (
    agent_page_requires_sensitive_access,
    context_page_visible,
    library_item_visible,
)
from backend.services.library_canonical import get_canonical_library_version


@dataclass
class TreeProjection:
    """In-memory snapshot of the actor-visible tree for one workspace."""

    nodes: dict[UUID, WorkspaceTreeNode]
    visible_ids: set[UUID]
    _children: dict[UUID | None, list[WorkspaceTreeNode]] = field(default_factory=dict)

    def is_visible(self, node_id: UUID) -> bool:
        return node_id in self.visible_ids

    def visible_children(
        self,
        node_id: UUID | None,
        *,
        folders_first: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceTreeNode]:
        children = [
            child
            for child in self._children.get(node_id, [])
            if child.id in self.visible_ids
        ]
        if folders_first:
            children.sort(key=lambda node: 0 if node.node_kind == "folder" else 1)
        if limit is not None:
            return children[:limit]
        return children

    def children_count(self, node_id: UUID) -> int:
        """Direct visible non-folder children (the tree badge semantics)."""
        return sum(
            1 for child in self.visible_children(node_id) if child.node_kind != "folder"
        )

    def has_children(self, node_id: UUID) -> bool:
        return bool(self.visible_children(node_id))

    def subtree_content_counts(self, root_ids: list[UUID]) -> dict[UUID, int]:
        """Visible entity (non-folder) node count per subtree, one pass."""
        counts: dict[UUID, int] = {}
        for root_id in root_ids:
            total = 0
            stack = [root_id]
            while stack:
                current = stack.pop()
                for child in self.visible_children(current):
                    if child.node_kind == "folder":
                        stack.append(child.id)
                    else:
                        total += 1
            counts[root_id] = total
        return counts


class TreeProjectionService:
    def load(
        self,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
        *,
        sections: tuple[str, ...] | None = None,
        include_archived: bool = False,
    ) -> TreeProjection:
        statement = select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id
        )
        if sections is not None:
            statement = statement.where(WorkspaceTreeNode.section.in_(sections))
        if not include_archived:
            statement = statement.where(
                WorkspaceTreeNode.visibility_status == "visible"
            )
        nodes = session.scalars(
            statement.order_by(
                WorkspaceTreeNode.section,
                WorkspaceTreeNode.sort_key,
                WorkspaceTreeNode.title,
                WorkspaceTreeNode.id,
            )
        ).all()

        visible_ids = self._visible_ids(session, nodes, actor)
        visible_ids = self._ancestor_closure(nodes, visible_ids)
        projection = TreeProjection(
            nodes={node.id: node for node in nodes},
            visible_ids=visible_ids,
        )
        for node in nodes:
            projection._children.setdefault(node.parent_id, []).append(node)
        return projection

    @staticmethod
    def _ancestor_closure(
        nodes: list[WorkspaceTreeNode], visible_ids: set[UUID]
    ) -> set[UUID]:
        """A node is visible only if its whole ancestor chain is (§3.3).

        Without this, a visible node under an invisible parent (restricted
        item's artifact ref, a page placed under a draft-only page) surfaces
        as an orphan and its breadcrumb walks — and thereby names — the
        hidden ancestor. Route summaries must not even imply hidden nodes.
        """
        by_id = {node.id: node for node in nodes}
        closed: dict[UUID, bool] = {}

        def chain_visible(node_id: UUID) -> bool:
            cached = closed.get(node_id)
            if cached is not None:
                return cached
            # mark first to break pathological parent cycles
            closed[node_id] = False
            node = by_id.get(node_id)
            result = (
                node is not None
                and node_id in visible_ids
                and (node.parent_id is None or chain_visible(node.parent_id))
            )
            closed[node_id] = result
            return result

        return {node.id for node in nodes if chain_visible(node.id)}

    @staticmethod
    def _visible_ids(
        session: Session,
        nodes: list[WorkspaceTreeNode],
        actor: ActorContext | None,
    ) -> set[UUID]:
        """Single source of tree entity visibility (batched, no N+1).

        ``actor=None`` is the internal/system full view: every node passes.
        """
        if actor is None:
            return {node.id for node in nodes}

        page_targets: list[UUID] = []
        library_targets: list[UUID] = []
        artifact_targets: list[UUID] = []
        agent_targets: list[UUID] = []
        for node in nodes:
            if node.target_id is None:
                continue
            if node.target_kind == "context_page":
                page_targets.append(node.target_id)
            elif node.target_kind == "library_item":
                library_targets.append(node.target_id)
            elif node.target_kind == "library_artifact":
                artifact_targets.append(node.target_id)
            elif node.target_kind == "agent_page":
                agent_targets.append(node.target_id)

        # Targets must live in the referencing nodes' own workspace(s) — a
        # malformed/migrated cross-workspace target must not authorize a node
        # (Codex R13). A tree is per-workspace, so this is normally one id.
        target_workspaces = {
            node.workspace_id for node in nodes if node.target_id is not None
        }

        artifact_parent: dict[
            UUID, tuple[UUID, LibraryArtifact, LibraryItemVersion]
        ] = {}
        if artifact_targets:
            artifact_parent = {
                artifact.id: (item_id, artifact, version)
                for artifact, item_id, version in session.execute(
                    select(
                        LibraryArtifact,
                        LibraryItemVersion.library_item_id,
                        LibraryItemVersion,
                    )
                    .join(
                        LibraryItemVersion,
                        LibraryArtifact.library_item_version_id
                        == LibraryItemVersion.id,
                    )
                    .where(LibraryArtifact.id.in_(artifact_targets))
                ).all()
            }

        visible_pages: set[UUID] = set()
        if page_targets:
            for page in session.scalars(
                select(ContextPage).where(
                    ContextPage.id.in_(page_targets),
                    ContextPage.workspace_id.in_(target_workspaces),
                )
            ).all():
                if context_page_visible(page, actor):
                    visible_pages.add(page.id)

        # Keyed by item so the artifact branch can enforce current-version.
        visible_library: dict[UUID, LibraryItem] = {}
        library_gate_ids = set(library_targets) | {
            item_id for item_id, _artifact, _version in artifact_parent.values()
        }
        if library_gate_ids:
            for item in session.scalars(
                select(LibraryItem).where(
                    LibraryItem.id.in_(library_gate_ids),
                    LibraryItem.workspace_id.in_(target_workspaces),
                )
            ).all():
                if library_item_visible(item, actor):
                    visible_library[item.id] = item

        canonical_versions: dict[UUID, LibraryItemVersion] = {}
        artifact_item_ids = {
            item_id for item_id, _artifact, _version in artifact_parent.values()
        }
        canonical_items = tuple(
            item
            for item_id, item in visible_library.items()
            if item_id in artifact_item_ids
        )
        canonical_version_ids = {
            item.current_ready_version_id or item.current_version_id
            for item in canonical_items
            if item.current_ready_version_id is not None
            or item.current_version_id is not None
        }
        preloaded_versions: dict[UUID, LibraryItemVersion] = {}
        if canonical_version_ids:
            # Preload the identity map so canonical resolution stays batched.
            preloaded_versions = {
                version.id: version
                for version in session.scalars(
                    select(LibraryItemVersion).where(
                        LibraryItemVersion.id.in_(canonical_version_ids)
                    )
                ).all()
            }
        for item in canonical_items:
            candidate_id = item.current_ready_version_id or item.current_version_id
            if candidate_id not in preloaded_versions:
                continue
            canonical = get_canonical_library_version(session, item)
            if canonical is not None:
                canonical_versions[item.id] = canonical

        visible_agents: set[UUID] = set()
        if agent_targets:
            can_read_agent_drafts = actor_can_read_drafts(actor)
            can_read_sensitive_agents = actor_can_read_sensitive_agent_pages(actor)
            for agent_page in session.scalars(
                select(AgentPage).where(
                    AgentPage.id.in_(agent_targets),
                    AgentPage.workspace_id.in_(target_workspaces),
                )
            ).all():
                if agent_page.lifecycle_status == "archived":
                    continue
                if (
                    agent_page_requires_sensitive_access(agent_page)
                    and not can_read_sensitive_agents
                ):
                    continue
                if not can_read_agent_drafts and (
                    agent_page.lifecycle_status != "active"
                    or agent_page.current_active_version_id is None
                ):
                    continue
                visible_agents.add(agent_page.id)

        visible: set[UUID] = set()
        for node in nodes:
            if node.target_id is None:
                # folders and saved views are structurally visible
                visible.add(node.id)
            elif node.target_kind == "context_page":
                if node.target_id in visible_pages:
                    visible.add(node.id)
            elif node.target_kind == "library_item":
                if node.target_id in visible_library:
                    visible.add(node.id)
            elif node.target_kind == "library_artifact":
                # artifact refs are gated by their parent item (an artifact of a
                # restricted item must not name it to viewers) AND must belong to
                # the selected attempt of the last ready version.
                parent = artifact_parent.get(node.target_id)
                if parent is not None:
                    item_id, artifact, version = parent
                    item = visible_library.get(item_id)
                    canonical = canonical_versions.get(item_id)
                    selected_attempt_id = (
                        canonical.selected_conversion_attempt_id
                        if canonical is not None
                        else None
                    )
                    # A-1: LibraryArtifact.attempt_id is NOT NULL now (the
                    # legacy nullable-attempt shape is retired) -- "no
                    # attempt selected yet" means no artifact can match.
                    artifact_is_selected = (
                        False
                        if selected_attempt_id is None
                        else artifact.attempt_id == selected_attempt_id
                    )
                    if (
                        item is not None
                        and canonical is not None
                        and version.id == canonical.id
                        and artifact_is_selected
                    ):
                        visible.add(node.id)
            elif node.target_kind == "agent_page":
                if node.target_id in visible_agents:
                    visible.add(node.id)
            else:
                # other ref kinds (ask_session) follow the node's own
                # visibility_status, already filtered by the query
                visible.add(node.id)
        return visible
