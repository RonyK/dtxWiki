"""Durable anonymous Context-page exposure grants.

``ContextPage.is_public`` is retained as a compatibility projection, but it is
never sufficient authority by itself.  Anonymous readers require a complete
scope/version grant that still matches both the Workspace visibility and the
page's current published snapshot.
"""

from __future__ import annotations

from uuid import UUID

from backend.models.context import ContextPage
from backend.models.workspace import Workspace

INDEXED_WEB = "indexed_web"
UNLISTED_WEB = "unlisted_web"


def exposure_scope_for_workspace(workspace: Workspace) -> str:
    return INDEXED_WEB if workspace.visibility == "public" else UNLISTED_WEB


def grant_public_page_exposure(
    page: ContextPage,
    *,
    scope: str,
    version_id: UUID,
) -> None:
    if scope not in {INDEXED_WEB, UNLISTED_WEB}:
        raise ValueError("unsupported public page exposure scope")
    page.is_public = True
    page.public_exposure_scope = scope
    page.public_exposed_version_id = version_id


def clear_public_page_exposure(page: ContextPage) -> None:
    page.is_public = False
    page.public_exposure_scope = None
    page.public_exposed_version_id = None


def valid_public_exposed_version_id(
    page: ContextPage,
    workspace: Workspace,
) -> UUID | None:
    """Return the stored exposed version only for a currently valid grant.

    All malformed, stale, archived, or visibility-mismatched states collapse
    to ``None`` so public routes can preserve their uniform-404 contract.
    """

    expected_scope = exposure_scope_for_workspace(workspace)
    if (
        not page.is_public
        or page.lifecycle_status == "archived"
        or page.public_exposure_scope != expected_scope
        or page.public_exposed_version_id is None
        or page.current_published_version_id is None
        or page.public_exposed_version_id != page.current_published_version_id
    ):
        return None
    return page.public_exposed_version_id


def reconcile_public_exposure_after_publication(
    page: ContextPage,
    *,
    previous_published_version_id: UUID | None,
    published_version_id: UUID,
) -> None:
    """Advance indexed grants and revoke version-pinned unlisted grants.

    Replaying the already-published version is a topology no-op and preserves a
    valid grant. Publishing any different version advances an indexed public
    Workspace grant, while an unlisted non-public Workspace grant is revoked so
    newly published bytes never become anonymous without a fresh acknowledgement.
    """

    if not page.is_public:
        clear_public_page_exposure(page)
        return
    complete_grant = (
        page.public_exposure_scope in {INDEXED_WEB, UNLISTED_WEB}
        and page.public_exposed_version_id is not None
        and page.public_exposed_version_id == previous_published_version_id
    )
    if not complete_grant:
        clear_public_page_exposure(page)
        return
    if published_version_id == previous_published_version_id:
        return
    if page.public_exposure_scope == INDEXED_WEB:
        page.public_exposed_version_id = published_version_id
        return
    clear_public_page_exposure(page)


__all__ = [
    "INDEXED_WEB",
    "UNLISTED_WEB",
    "clear_public_page_exposure",
    "exposure_scope_for_workspace",
    "grant_public_page_exposure",
    "reconcile_public_exposure_after_publication",
    "valid_public_exposed_version_id",
]
