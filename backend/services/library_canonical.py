"""One canonical Library version/artifact selection rule for all consumers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.orm import Session, selectinload

from backend.library_media.ports import LibraryBlobRepositoryFactory
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryArtifactPreviewDetail,
    LibraryAttempt,
    LibraryAttemptPreviewDetail,
    LibraryItem,
    LibraryItemVersion,
)
from backend.models.library_semantic_overlay import LibrarySemanticOverlay
from backend.models.media import SecurityBlockedBlobRow
from backend.repositories.workspace_scoped import (
    get_in_workspace,
    select_in_workspace_by_ids,
)
from backend.services.preview_contracts import renderer_accepts_media_type

_LEGACY_READY_STATUSES = frozenset({"completed", "ready", "ready_partial"})


def library_original_content_url(
    *,
    workspace_id: UUID,
    version_id: UUID,
    source_blob_id: UUID | None,
) -> str | None:
    """The one original-download URL projection, shared by version detail and
    the Preview manifest (P4.4 invariant 4) -- so the two can never silently
    diverge on whether a version's original is actually downloadable.
    """

    if source_blob_id is None:
        return None
    return f"/api/v1/workspaces/{workspace_id}/library/versions/{version_id}/content"


def library_original_download_available(
    session: Session,
    *,
    workspace_id: UUID,
    version: LibraryItemVersion,
    blob_repository_factory: LibraryBlobRepositoryFactory | None,
) -> bool:
    """Whether this version's original bytes are still retrievable right now.

    Doc §3-D (History "download this version"): the frontend must not infer
    availability from `source_blob_id`/`original_url` presence alone -- a
    version row survives long after its source blob can be GC'd, retention-
    expired, or security-blocked, and `library_original_content_url` above
    only ever checks for a non-null id. This is the server-authoritative
    capability History uses to decide whether to show a download action at
    all, deliberately kept separate from `original_url` so the *current*
    version's shared P4.4 projection is never touched by this check.
    """

    if (
        blob_repository_factory is None
        or version.source_blob_workspace_id != workspace_id
        or version.source_blob_id is None
        or version.source_blob_epoch is None
        or version.byte_size is None
    ):
        return False
    reference = BlobReference(
        workspace_id=workspace_id,
        blob_id=version.source_blob_id,
        epoch=BlobEpoch(version.source_blob_epoch),
    )
    blocked = session.get(
        SecurityBlockedBlobRow,
        {
            "workspace_id": reference.workspace_id,
            "blob_id": reference.blob_id,
            "blob_epoch": reference.epoch.value,
        },
    )
    if blocked is not None:
        return False
    blob = blob_repository_factory(session).get(reference)
    return (
        blob is not None
        and blob.sha256.value == version.content_hash.lower()
        and blob.byte_size == version.byte_size
    )


@dataclass(frozen=True, slots=True)
class EffectiveLibraryMarkdown:
    """The selected immutable Markdown base plus its optional authored body."""

    base_artifact: LibraryArtifact
    body_markdown: str
    content_sha256: str
    byte_size: int
    revision: int
    overlay: LibrarySemanticOverlay | None


def get_canonical_library_version(
    session: Session, item: LibraryItem
) -> LibraryItemVersion | None:
    """Return the last successfully projected version, never a pending latest.

    The fallback is intentionally limited to completed pre-0005 rows. It keeps
    metadata-created test/legacy databases readable while the migration
    backfill establishes ``current_ready_version_id`` for durable databases.
    """

    if item.current_ready_version_id is not None:
        version = session.get(LibraryItemVersion, item.current_ready_version_id)
        if version is not None and version.library_item_id == item.id:
            return version
        return None
    if item.current_version_id is None:
        return None
    legacy = session.get(LibraryItemVersion, item.current_version_id)
    if (
        legacy is not None
        and legacy.library_item_id == item.id
        and legacy.processing_status in _LEGACY_READY_STATUSES
    ):
        return legacy
    return None


def list_canonical_library_artifacts(
    session: Session,
    version: LibraryItemVersion,
) -> tuple[LibraryArtifact, ...]:
    """Return only the selected attempt's immutable artifact set.

    A-1: the legacy "artifact with no attempt at all" shape is retired --
    ``LibraryArtifact.attempt_id`` is NOT NULL now, so a version with no
    selected conversion attempt yet has no canonical artifacts at all,
    full stop (there is no more nullable-FK row to fall back to).
    """

    if version.selected_conversion_attempt_id is None:
        return ()
    statement = select(LibraryArtifact).where(
        LibraryArtifact.library_item_version_id == version.id,
        LibraryArtifact.attempt_id == version.selected_conversion_attempt_id,
        LibraryArtifact.stage == "conversion",
    )
    return tuple(
        session.scalars(
            statement.order_by(
                LibraryArtifact.ordinal.asc(),
                LibraryArtifact.created_at.asc(),
                LibraryArtifact.id.asc(),
            )
        )
    )


def get_canonical_markdown_artifact(
    session: Session,
    version: LibraryItemVersion,
) -> LibraryArtifact | None:
    """Select one canonical Markdown/search artifact deterministically."""

    if version.selected_conversion_attempt_id is None:
        return None
    statement = select(LibraryArtifact).where(
        LibraryArtifact.library_item_version_id == version.id,
        LibraryArtifact.attempt_id == version.selected_conversion_attempt_id,
        LibraryArtifact.stage == "conversion",
        LibraryArtifact.role.in_(("markdown", "search_text")),
    )
    return session.scalar(
        statement.order_by(
            case(
                (LibraryArtifact.role == "markdown", 0),
                else_=1,
            ),
            LibraryArtifact.ordinal.asc(),
            LibraryArtifact.created_at.asc(),
            LibraryArtifact.id.asc(),
        ).limit(1)
    )


def get_canonical_editable_markdown_artifact(
    session: Session,
    version: LibraryItemVersion,
) -> LibraryArtifact | None:
    """Return only an exact selected-attempt ``text/markdown`` base.

    Legacy text and ``search_text`` fallbacks remain useful to read/search
    callers, but they are intentionally not editable: an author overlay must
    be fenced to one complete, immutable Markdown artifact selected by the
    current conversion attempt.
    """

    if version.selected_conversion_attempt_id is None:
        return None
    return session.scalar(
        select(LibraryArtifact)
        .join(
            LibraryArtifactConversionDetail,
            LibraryArtifactConversionDetail.artifact_id == LibraryArtifact.id,
        )
        .where(
            LibraryArtifact.library_item_version_id == version.id,
            LibraryArtifact.attempt_id == version.selected_conversion_attempt_id,
            LibraryArtifact.stage == "conversion",
            LibraryArtifact.role == "markdown",
            LibraryArtifact.media_type == "text/markdown",
            LibraryArtifactConversionDetail.is_complete.is_(True),
            LibraryArtifactConversionDetail.index_text.is_not(None),
        )
        .order_by(
            LibraryArtifact.ordinal.asc(),
            LibraryArtifact.created_at.asc(),
            LibraryArtifact.id.asc(),
        )
        .limit(1)
    )


def get_effective_library_markdown(
    session: Session,
    version: LibraryItemVersion,
) -> EffectiveLibraryMarkdown | None:
    """Resolve an overlay only while its exact base remains selected."""

    base = get_canonical_editable_markdown_artifact(session, version)
    if base is None or base.conversion_detail is None:
        return None
    # content_sha256 is NOT NULL on LibraryArtifact now; index_text lives on
    # the joined conversion detail row (the selecting query already required
    # it IS NOT NULL, but the base object itself may have been loaded without
    # the join re-verifying it -- keep the guard for clarity/safety).
    if base.conversion_detail.index_text is None:
        return None
    base_body = base.conversion_detail.index_text
    base_bytes = base_body.encode("utf-8")
    base_digest = hashlib.sha256(base_bytes).hexdigest()
    if base.byte_size != len(base_bytes) or base.content_sha256.lower() != base_digest:
        return None
    base_projection = EffectiveLibraryMarkdown(
        base_artifact=base,
        body_markdown=base_body,
        content_sha256=base.content_sha256.lower(),
        byte_size=len(base_bytes),
        revision=0,
        overlay=None,
    )
    if version.selected_semantic_overlay_id is None:
        return base_projection
    overlay = session.get(
        LibrarySemanticOverlay,
        version.selected_semantic_overlay_id,
    )
    if (
        overlay is None
        or overlay.library_item_version_id != version.id
        or overlay.base_artifact_id != base.id
        or overlay.base_artifact_sha256.lower() != base.content_sha256.lower()
        or overlay.invalidated_at is not None
    ):
        return base_projection
    overlay_bytes = overlay.body_markdown.encode("utf-8")
    overlay_hash = hashlib.sha256(overlay_bytes).hexdigest()
    if (
        overlay.byte_size != len(overlay_bytes)
        or overlay.content_sha256 != overlay_hash
    ):
        return base_projection
    return EffectiveLibraryMarkdown(
        base_artifact=base,
        body_markdown=overlay.body_markdown,
        content_sha256=overlay.content_sha256,
        byte_size=overlay.byte_size,
        revision=overlay.revision,
        overlay=overlay,
    )


def canonical_library_artifact_belongs_to_item(
    session: Session,
    *,
    item: LibraryItem,
    artifact_id: UUID,
) -> bool:
    version = get_canonical_library_version(session, item)
    if version is None:
        return False
    return any(
        artifact.id == artifact_id
        for artifact in list_canonical_library_artifacts(session, version)
    )


def preview_generation_dependency_matches(
    session: Session,
    version: LibraryItemVersion,
    attempt: LibraryAttempt,
) -> bool:
    """Detect a preview-generation attempt whose input has since gone stale.

    Shared by manifest variant listing and the flat artifact-content lookup
    below so the two can never disagree on whether a generated artifact is
    still backed by the version's currently selected input (P9.2 unifies
    their transport onto one route).
    """

    detail = attempt.preview_detail
    if detail is None:
        return False
    artifact = (
        get_in_workspace(
            session,
            LibraryArtifact,
            workspace_id=version.workspace_id,
            resource_id=detail.input_artifact_id,
        )
        if detail.input_artifact_id is not None
        else None
    )
    return _preview_generation_dependency_matches_data(
        version, attempt, detail, artifact
    )


def _preview_generation_dependency_matches_data(
    version: LibraryItemVersion,
    attempt: LibraryAttempt,
    detail: LibraryAttemptPreviewDetail,
    input_artifact: LibraryArtifact | None,
) -> bool:
    """Pure dependency gate shared by scalar and batched canonical lookup."""
    if detail.input_kind == "source":
        return (
            detail.input_attempt_id is None
            and detail.input_artifact_id is None
            and attempt.input_sha256 == version.content_hash.lower()
        )
    if (
        detail.input_kind != "conversion_artifact"
        or version.selected_conversion_attempt_id != detail.input_attempt_id
        or detail.input_artifact_id is None
    ):
        return False
    return (
        input_artifact is not None
        and input_artifact.library_item_version_id == version.id
        and input_artifact.attempt_id == detail.input_attempt_id
        and input_artifact.content_sha256 is not None
        and input_artifact.content_sha256.lower() == attempt.input_sha256
    )


def _canonical_artifact_is_valid(
    item: LibraryItem,
    version: LibraryItemVersion,
    artifact: LibraryArtifact,
    *,
    canonical_version: LibraryItemVersion | None,
    attempt: LibraryAttempt | None = None,
    attempt_preview_detail: LibraryAttemptPreviewDetail | None = None,
    input_artifact: LibraryArtifact | None = None,
    artifact_preview_detail: LibraryArtifactPreviewDetail | None = None,
) -> bool:
    """Shared stage and selection policy for scalar and batched lookups."""
    if artifact.stage == "conversion":
        return (
            canonical_version is not None
            and canonical_version.id == version.id
            and artifact.attempt_id == version.selected_conversion_attempt_id
        )
    if artifact.stage != "preview_generation":
        return False
    if (
        item.current_preview_version_id != version.id
        or version.selected_preview_attempt_id is None
        or version.preview_status not in {"ready", "ready_partial"}
        or version.selected_preview_attempt_id != artifact.attempt_id
        or attempt is None
        or attempt_preview_detail is None
        or artifact_preview_detail is None
    ):
        return False
    return (
        _preview_generation_dependency_matches_data(
            version, attempt, attempt_preview_detail, input_artifact
        )
        and renderer_accepts_media_type(
            artifact_preview_detail.renderer_contract, artifact.media_type
        )
    )


def canonical_library_artifact_lookup(
    session: Session,
    *,
    workspace_id: UUID,
    artifact_id: UUID,
) -> tuple[LibraryItem, LibraryItemVersion, LibraryArtifact] | None:
    """Resolve one artifact's canonical lineage for the flat
    ``library/artifacts/{artifact_id}`` family (P9.2).

    This is the single membership gate the family's metadata and content
    routes share across every stage they now serve: a canonical ``stage ==
    "conversion"`` artifact of the item's canonical version (the pre-P9.2
    rule, unchanged), or the item's currently selected, non-stale
    ``stage == "preview_generation"`` artifact. A "semantic artifact" is
    simply a conversion-stage artifact that also renders as a semantic
    presentation -- it needs no separate rule; the dedicated
    ``preview/semantic-artifacts`` transport it used to require is retired.
    """

    artifact = get_in_workspace(
        session,
        LibraryArtifact,
        workspace_id=workspace_id,
        resource_id=artifact_id,
    )
    if artifact is None:
        return None
    version = get_in_workspace(
        session,
        LibraryItemVersion,
        workspace_id=workspace_id,
        resource_id=artifact.library_item_version_id,
    )
    if version is None:
        return None
    item = get_in_workspace(
        session,
        LibraryItem,
        workspace_id=workspace_id,
        resource_id=version.library_item_id,
    )
    if item is None:
        return None

    canonical_version = (
        get_canonical_library_version(session, item)
        if artifact.stage == "conversion"
        else None
    )
    attempt = None
    attempt_preview_detail = None
    input_artifact = None
    artifact_preview_detail = None
    if artifact.stage == "preview_generation":
        attempt = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=workspace_id,
            resource_id=artifact.attempt_id,
        )
        if attempt is not None:
            attempt_preview_detail = attempt.preview_detail
            if (
                attempt_preview_detail is not None
                and attempt_preview_detail.input_artifact_id is not None
            ):
                input_artifact = get_in_workspace(
                    session,
                    LibraryArtifact,
                    workspace_id=workspace_id,
                    resource_id=attempt_preview_detail.input_artifact_id,
                )
        artifact_preview_detail = artifact.preview_detail
    if _canonical_artifact_is_valid(
        item,
        version,
        artifact,
        canonical_version=canonical_version,
        attempt=attempt,
        attempt_preview_detail=attempt_preview_detail,
        input_artifact=input_artifact,
        artifact_preview_detail=artifact_preview_detail,
    ):
        return item, version, artifact
    return None


def canonical_library_artifact_lookup_many(
    session: Session,
    *,
    workspace_id: UUID,
    artifact_ids: set[UUID] | list[UUID] | tuple[UUID, ...],
    chunk_size: int = 400,
) -> dict[UUID, tuple[LibraryItem, LibraryItemVersion, LibraryArtifact] | None]:
    """Resolve canonical artifact lineages with bounded workspace-scoped reads.

    This is for read projections that may contain many artifact links. It
    applies the same stage, selected-version/attempt, preview dependency and
    renderer-contract gates as :func:`canonical_library_artifact_lookup`.
    Missing, foreign-workspace, stale, and unsupported artifacts map to None.
    """
    ids = list(dict.fromkeys(artifact_ids))
    result: dict[
        UUID, tuple[LibraryItem, LibraryItemVersion, LibraryArtifact] | None
    ] = {artifact_id: None for artifact_id in ids}
    if not ids:
        return result

    def load(model, resource_ids: set[UUID], *, options=()):
        rows = {}
        if not resource_ids:
            return rows
        ordered_ids = list(resource_ids)
        for offset in range(0, len(ordered_ids), chunk_size):
            rows.update(
                (
                    row.id,
                    row,
                )
                for row in session.scalars(
                    select_in_workspace_by_ids(
                        model,
                        workspace_id=workspace_id,
                        resource_ids=ordered_ids[offset : offset + chunk_size],
                    )
                    .options(*options)
                )
            )
        return rows

    artifacts = load(
        LibraryArtifact,
        set(ids),
        options=(selectinload(LibraryArtifact.preview_detail),),
    )
    version_ids = {
        artifact.library_item_version_id for artifact in artifacts.values()
    }
    versions = load(LibraryItemVersion, version_ids)
    item_ids = {version.library_item_id for version in versions.values()}
    items = load(LibraryItem, item_ids)

    # Include both the artifact's version and the version pointer used by the
    # scalar canonical-version helper, including its legacy ready fallback.
    pointer_version_ids = {
        selected_id
        for item in items.values()
        for selected_id in (
            item.current_ready_version_id or item.current_version_id,
        )
        if selected_id is not None
    }
    missing_pointer_ids = pointer_version_ids - versions.keys()
    versions.update(load(LibraryItemVersion, missing_pointer_ids))

    preview_attempt_ids = {
        artifact.attempt_id
        for artifact in artifacts.values()
        if artifact.stage == "preview_generation"
    }
    attempts = load(
        LibraryAttempt,
        preview_attempt_ids,
        options=(selectinload(LibraryAttempt.preview_detail),),
    )
    preview_details = {
        attempt_id: attempt.preview_detail
        for attempt_id, attempt in attempts.items()
        if attempt.preview_detail is not None
    }
    artifact_preview_details = {
        artifact_id: artifact.preview_detail
        for artifact_id, artifact in artifacts.items()
        if artifact.preview_detail is not None
    }
    input_artifact_ids = {
        detail.input_artifact_id
        for detail in preview_details.values()
        if detail.input_artifact_id is not None
    }
    input_artifacts = load(LibraryArtifact, input_artifact_ids)

    for artifact_id, artifact in artifacts.items():
        version = versions.get(artifact.library_item_version_id)
        if version is None:
            continue
        item = items.get(version.library_item_id)
        if item is None:
            continue
        canonical_version = None
        canonical_pointer = item.current_ready_version_id
        if canonical_pointer is None and item.current_version_id is not None:
            legacy_version = versions.get(item.current_version_id)
            if (
                legacy_version is not None
                and legacy_version.library_item_id == item.id
                and legacy_version.processing_status in _LEGACY_READY_STATUSES
            ):
                canonical_pointer = legacy_version.id
        if canonical_pointer == version.id:
            canonical_version = version
        elif canonical_pointer is not None:
            canonical_version = versions.get(canonical_pointer)
        attempt = attempts.get(artifact.attempt_id)
        detail = preview_details.get(artifact.attempt_id)
        input_artifact = (
            input_artifacts.get(detail.input_artifact_id)
            if detail is not None and detail.input_artifact_id is not None
            else None
        )
        artifact_detail = artifact_preview_details.get(artifact_id)
        if _canonical_artifact_is_valid(
            item,
            version,
            artifact,
            canonical_version=canonical_version,
            attempt=attempt,
            attempt_preview_detail=detail,
            input_artifact=input_artifact,
            artifact_preview_detail=artifact_detail,
        ):
            result[artifact_id] = (item, version, artifact)
    return result


__all__ = [
    "EffectiveLibraryMarkdown",
    "canonical_library_artifact_belongs_to_item",
    "canonical_library_artifact_lookup",
    "canonical_library_artifact_lookup_many",
    "get_canonical_editable_markdown_artifact",
    "get_canonical_library_version",
    "get_canonical_markdown_artifact",
    "get_effective_library_markdown",
    "library_original_download_available",
    "list_canonical_library_artifacts",
    "preview_generation_dependency_matches",
]
