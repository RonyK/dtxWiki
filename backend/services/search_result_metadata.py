"""Actor-visible display metadata for a single, already paginated search window."""

from uuid import UUID

from sqlalchemy.orm import Session

from backend.policy.auth import ActorContext
from backend.schemas.read import RankedResult
from backend.services.tree_locations import visible_target_locations
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
    locations = visible_target_locations(session, workspace_id, target_results, actor)
    for result in results:
        direct = (result.type, result.id)
        key = direct if direct in locations else result._location_fallback
        if key in locations:
            result.location = locations[key]
