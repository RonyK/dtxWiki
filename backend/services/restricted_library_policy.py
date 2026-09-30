from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.repositories.workspace_scoped import get_in_workspace

RESTRICTED_LIBRARY_TRUST_LEVELS = {"restricted"}


def is_restricted_library_target(
    *,
    session: Session,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
) -> bool | None:
    library_item = _resolve_library_item_for_target(
        session=session,
        workspace_id=workspace_id,
        target_type=target_type,
        target_id=target_id,
    )
    if library_item is None:
        return None
    return library_item.trust_level in RESTRICTED_LIBRARY_TRUST_LEVELS


def resolve_restricted_library_target(
    *,
    session: Session,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
) -> tuple[UUID, bool] | None:
    """Return ``(workspace_id, is_restricted)`` for a Library read target
    already known to belong to ``workspace_id``, or ``None`` if it does not
    resolve there.

    Epic-3 D-5: a target that belongs to a *different* workspace than
    ``workspace_id`` must resolve as not-found for this call, the same as it
    would on a real per-workspace shard. Every ownership hop is resolved
    through ``get_in_workspace`` below, so a shared development database has
    the same opaque-miss behavior and never becomes a reverse locator.

    Callers re-scope the actor to the returned ``workspace_id`` (== the
    ``workspace_id`` argument) before the restricted check — a bearer
    actor's ``workspace_role`` reflects the *default* workspace, so the
    restricted predicate must be evaluated against the actor's role in the
    target item's workspace, not the default.
    """
    library_item = _resolve_library_item_for_target(
        session=session,
        workspace_id=workspace_id,
        target_type=target_type,
        target_id=target_id,
    )
    if library_item is None:
        return None
    return (
        workspace_id,
        library_item.trust_level in RESTRICTED_LIBRARY_TRUST_LEVELS,
    )


def _resolve_library_item_for_target(
    *,
    session: Session,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
) -> LibraryItem | None:
    """Resolve every Library ownership hop within one workspace.

    A miss at any hop is deliberately opaque: a missing id and an id owned by
    another workspace both return ``None``.
    """

    if target_type == "library_item_version":
        version = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=target_id,
        )
    else:
        artifact = get_in_workspace(
            session,
            LibraryArtifact,
            workspace_id=workspace_id,
            resource_id=target_id,
        )
        version = (
            get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=workspace_id,
                resource_id=artifact.library_item_version_id,
            )
            if artifact is not None
            else None
        )
    if version is None:
        return None
    return get_in_workspace(
        session,
        LibraryItem,
        workspace_id=workspace_id,
        resource_id=version.library_item_id,
    )
