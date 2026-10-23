"""Actor-visible display metadata for a single, already paginated search window."""

from uuid import UUID

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from backend.models.tree import WorkspaceTreeNode
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import list_in_workspace
from backend.schemas.read import RankedResult
from backend.services.tree_projection import TreeProjectionService
from backend.utils.page_frontmatter import derive_nav_description


def enrich_search_result_metadata(
    session: Session,
    workspace_id: UUID,
    results: list[RankedResult],
    actor: ActorContext | None,
) -> None:
    """Batch ancestors by depth, with the same visibility closure as the tree.

    Body text comes from retrieval hydration, including for legacy rows whose
    stored nav_description still contains metadata. Nothing is backfilled.
    Query count depends on ancestor depth and kinds, never the window size.
    """
    for result in results:
        if result._summary_markdown is not None:
            result.summary = derive_nav_description(result._summary_markdown)
    target_results: dict[tuple[str, UUID], list[RankedResult]] = {}
    for result in results:
        if result.type in {
            "context_page",
            "library_item",
            "library_artifact",
            "agent_page",
        }:
            target_results.setdefault((result.type, result.id), []).append(result)
            if result._location_fallback is not None:
                target_results.setdefault(result._location_fallback, []).append(result)
    if not target_results:
        return
    leaves = list_in_workspace(
        session,
        WorkspaceTreeNode,
        workspace_id=workspace_id,
        conditions=(
            tuple_(WorkspaceTreeNode.target_kind, WorkspaceTreeNode.target_id).in_(
                target_results
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
    # TreeProjection's None actor means an internal full view, whereas search
    # treats None as anonymous. Keep the search visibility boundary here.
    visibility_actor = actor or ActorContext(
        actor_kind="anonymous",
        actor_id="search",
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )
    visible = TreeProjectionService._ancestor_closure(
        nodes, TreeProjectionService._visible_ids(session, nodes, visibility_actor)
    )
    leaves_by_target = {(leaf.target_kind, leaf.target_id): leaf for leaf in leaves}
    for leaf in leaves:
        if leaf.id not in visible:
            continue
        path: list[str] = []
        current = leaf
        while current.parent_id is not None:
            current = by_id[current.parent_id]
            path.append(current.title)
        for result in target_results[(leaf.target_kind, leaf.target_id)]:
            direct = leaves_by_target.get((result.type, result.id))
            if direct is not None and direct.id != leaf.id:
                continue
            result.location = list(reversed(path)) or None
