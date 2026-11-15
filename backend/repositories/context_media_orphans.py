"""The one definition of "orphan" for Context media.

An orphan is an asset a Page owns that no version of that page's Markdown ever
referenced -- draft, published or historical -- and that is not currently
protected by an open ``draft`` version.

This lives in ``backend/repositories`` because two callers need it and that is
the only package both may import: ``backend/services/context_media_orphan_sweep``
(which reclaims them on a retention schedule) and
``backend/context_media/adapters/sqlalchemy`` (which lets a workspace owner
filter a listing down to them). The adapter already imports
``backend/repositories/context_visibility`` for the same reason.

Keeping it here is not tidiness. Two copies of this predicate would drift, and
the failure would be silent in the worst direction: the sweep deleting rows the
UI never showed, or the UI offering "orphans" the sweep will not reclaim.
"""

from backend.models.context import ContextPageVersion
from backend.models.context_media import ContextMediaAsset, ContextMediaUsage
from backend.repositories.workspace_scoped import select_in_workspace


def context_media_orphan_clause():
    """The orphan predicate, correlated to :class:`ContextMediaAsset`.

    Deliberately carries no ``workspace_id`` term: every caller already scopes
    its own query to one workspace, and folding the scope in here would let a
    caller believe it was scoped when it was not.
    """

    usage_exists = (
        select_in_workspace(
            ContextMediaUsage,
            workspace_id=ContextMediaAsset.workspace_id,
            conditions=(
                ContextMediaUsage.page_id == ContextMediaAsset.page_id,
                ContextMediaUsage.asset_id == ContextMediaAsset.id,
            ),
        )
        .with_only_columns(ContextMediaUsage.asset_id)
        .exists()
    )
    created_for_version_is_editing = (
        select_in_workspace(
            ContextPageVersion,
            workspace_id=ContextMediaAsset.workspace_id,
            conditions=(
                ContextPageVersion.id == ContextMediaAsset.created_for_version_id,
                ContextPageVersion.version_status == "draft",
            ),
        )
        .with_only_columns(ContextPageVersion.id)
        .exists()
    )
    return ~usage_exists & ~created_for_version_is_editing
