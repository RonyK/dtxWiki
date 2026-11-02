"""Batch the ordinary read boundaries for legacy citation evidence."""

from sqlalchemy.orm import load_only, selectinload

from backend.models.context import ContextClaim, ContextPage, ContextPageVersion
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.repositories.workspace_scoped import select_in_workspace_by_ids
from backend.services.actor_visible_read import (
    context_page_visible,
    context_version_is_visible,
    library_item_visible,
)
from backend.services.library_canonical import canonical_library_artifact_lookup_many

_VERSION_COLUMNS = (
    ContextPageVersion.id,
    ContextPageVersion.workspace_id,
    ContextPageVersion.page_id,
    ContextPageVersion.version_status,
    ContextPageVersion.created_by_actor_kind,
    ContextPageVersion.created_by_actor_id,
)


def context_visibility_options():
    return (
        selectinload(ContextPage.current_version).load_only(*_VERSION_COLUMNS),
        selectinload(ContextPage.latest_draft_version).load_only(*_VERSION_COLUMNS),
    )


def legacy_citation_visibility(session, *, workspace_id, citations, actor):
    """Never load/parse Context bodies to decide whether evidence may be read."""
    citations = list(citations)
    if not citations:
        return {}

    def load(model, ids, *, options=()):
        ids, rows = list(set(ids)), {}
        for offset in range(0, len(ids), 400):
            statement = select_in_workspace_by_ids(
                model,
                workspace_id=workspace_id,
                resource_ids=ids[offset : offset + 400],
            ).options(*options)
            rows.update((row.id, row) for row in session.scalars(statement))
        return rows

    claims = load(
        ContextClaim,
        [row.target_id for row in citations if row.target_kind == "context_claim"],
        options=(load_only(ContextClaim.id, ContextClaim.page_version_id),),
    )
    version_ids = {
        row.target_id for row in citations if row.target_kind != "context_claim"
    } | {claim.page_version_id for claim in claims.values()}
    version_ids.update(
        row.cited_context_page_version_id
        for row in citations
        if row.cited_context_page_version_id is not None
    )
    versions = load(
        ContextPageVersion, version_ids, options=(load_only(*_VERSION_COLUMNS),)
    )
    pages = load(
        ContextPage,
        {version.page_id for version in versions.values()}
        | {row.target_id for row in citations if row.target_kind == "context_page"},
        options=context_visibility_options(),
    )
    # Old page-owned evidence follows that page's current published head.
    fallback_ids = {
        page.current_published_version_id
        for page in pages.values()
        if page.current_published_version_id is not None
    } - versions.keys()
    versions.update(
        load(ContextPageVersion, fallback_ids, options=(load_only(*_VERSION_COLUMNS),))
    )
    library_versions = load(
        LibraryItemVersion,
        [
            row.library_item_version_id
            for row in citations
            if row.library_item_version_id
        ],
        options=(
            load_only(
                LibraryItemVersion.id,
                LibraryItemVersion.workspace_id,
                LibraryItemVersion.library_item_id,
            ),
        ),
    )
    library_items = load(
        LibraryItem,
        [version.library_item_id for version in library_versions.values()],
    )
    artifacts = canonical_library_artifact_lookup_many(
        session,
        workspace_id=workspace_id,
        artifact_ids={
            row.library_artifact_id for row in citations if row.library_artifact_id
        },
        metadata_only=True,
    )

    def readable_context(version):
        return bool(
            version is not None
            and context_version_is_visible(version, actor)
            and context_page_visible(
                pages.get(version.page_id), actor, workspace_id=workspace_id
            )
        )

    result = {}
    for citation in citations:
        if citation.target_kind == "context_claim":
            claim = claims.get(citation.target_id)
            owner = versions.get(claim.page_version_id) if claim else None
        else:
            owner = versions.get(citation.target_id)
            if owner is None and citation.target_kind == "context_page":
                page = pages.get(citation.target_id)
                owner = (
                    versions.get(page.current_published_version_id) if page else None
                )
        visible = readable_context(owner)
        if citation.library_item_version_id is not None:
            version = library_versions.get(citation.library_item_version_id)
            visible = (
                visible
                and version is not None
                and library_item_visible(
                    library_items.get(version.library_item_id),
                    actor,
                    workspace_id=workspace_id,
                )
            )
        if citation.library_artifact_id is not None:
            lookup = artifacts.get(citation.library_artifact_id)
            visible = (
                visible
                and lookup is not None
                and library_item_visible(lookup[0], actor, workspace_id=workspace_id)
            )
            if citation.library_item_version_id is not None:
                visible = visible and lookup[1].id == citation.library_item_version_id
        if citation.cited_context_page_version_id is not None:
            visible = visible and readable_context(
                versions.get(citation.cited_context_page_version_id)
            )
        if not any(
            (
                citation.library_item_version_id,
                citation.library_artifact_id,
                citation.cited_context_page_version_id,
            )
        ):
            visible = False
        result[citation.id] = bool(visible)
    return result
