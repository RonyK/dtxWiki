"""Batched actor-visible tree locations shared by read projections."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import list_in_workspace
from backend.services.tree_projection import TreeProjectionService


def visible_target_locations(
    session: Session,
    workspace_id: UUID,
    targets: Collection[tuple[str, UUID]],
    actor: ActorContext | None,
) -> dict[tuple[str, UUID], list[str] | None]:
    """Read only requested primary placements and their visible ancestors.

    A present but hidden placement maps to None, so a caller cannot replace
    its private path with another placement's breadcrumb.
    """
    if not targets:
        return {}
    leaves = list_in_workspace(
        session,
        WorkspaceTreeNode,
        workspace_id=workspace_id,
        conditions=(
            tuple_(WorkspaceTreeNode.target_kind, WorkspaceTreeNode.target_id).in_(
                targets
            ),
            WorkspaceTreeNode.primary_placement.is_(True),
            WorkspaceTreeNode.visibility_status == "visible",
        ),
    )
    by_id = {node.id: node for node in leaves}
    attempted = set(by_id)
    frontier = {node.parent_id for node in leaves if node.parent_id is not None}
    while frontier := frontier - attempted:
        attempted.update(frontier)
        parents = list_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=workspace_id,
            conditions=(
                WorkspaceTreeNode.id.in_(frontier),
                WorkspaceTreeNode.visibility_status == "visible",
            ),
        )
        by_id.update((node.id, node) for node in parents)
        frontier = {node.parent_id for node in parents if node.parent_id is not None}
    nodes = list(by_id.values())
    # Read projections treat an absent actor as anonymous, while the shared
    # tree implementation uses None for its internal full-view mode.
    visibility_actor = actor or ActorContext(
        actor_kind="anonymous",
        actor_id="tree-location-read",
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )
    visible = TreeProjectionService._ancestor_closure(
        nodes, TreeProjectionService._visible_ids(session, nodes, visibility_actor)
    )
    locations = {(leaf.target_kind, leaf.target_id): None for leaf in leaves}
    for leaf in leaves:
        if leaf.id not in visible:
            continue
        path: list[str] = []
        current = leaf
        while current.parent_id is not None:
            current = by_id[current.parent_id]
            path.append(current.title)
        locations[(leaf.target_kind, leaf.target_id)] = list(reversed(path)) or None
    return locations
