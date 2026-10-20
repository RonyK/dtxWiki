"""Immediate, review-free editing of canonical Library semantic Markdown."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.library_semantic_overlay import LibrarySemanticOverlay
from backend.models.workspace import WorkspaceMembership
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import check_actor_can_access
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.library_semantic import LIBRARY_SEMANTIC_MARKDOWN_MAX_BYTES
from backend.schemas.page import BODY_MD_MAX_LENGTH
from backend.services.actor_visible_read import actor_can_read_library_trust_level
from backend.services.audit_service import AuditService
from backend.services.db_write_intent import acquire_write_intent
from backend.services.indexing.db_native import remove_document_chunks
from backend.services.indexing.fts5 import sync_library_doc_sources
from backend.services.job_queue import JobQueueService
from backend.services.library_canonical import (
    EffectiveLibraryMarkdown,
    get_canonical_library_version,
    get_effective_library_markdown,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadValidationError,
    StaleBaseVersionError,
)


@dataclass(frozen=True, slots=True)
class LibrarySemanticMarkdownProjection:
    library_item_id: UUID
    version_id: UUID
    base_artifact_id: UUID
    base_content_sha256: str
    overlay_id: UUID | None
    revision: int
    source: str
    body_markdown: str
    content_sha256: str
    byte_size: int
    updated_at: datetime

    def to_payload(self) -> dict[str, object]:
        return {
            "library_item_id": self.library_item_id,
            "version_id": self.version_id,
            "base_artifact_id": self.base_artifact_id,
            "base_content_sha256": self.base_content_sha256,
            "overlay_id": self.overlay_id,
            "revision": self.revision,
            "source": self.source,
            "body_markdown": self.body_markdown,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class LibrarySemanticOverlayContent:
    body: bytes
    etag: str
    filename: str
    library_item_id: UUID
    library_item_version_id: UUID
    media_type: str = "text/markdown"


class LibrarySemanticOverlayService:
    """Own the mutable overlay while leaving conversion/source lineage intact."""

    def __init__(
        self,
        *,
        job_queue: JobQueueService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self.job_queue = job_queue or JobQueueService()
        self.audit_service = audit_service or AuditService()

    def get_current(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        actor: ActorContext,
    ) -> LibrarySemanticMarkdownProjection:
        item, version = _authorize_current_ready(
            session,
            workspace_id=workspace_id,
            version_id=version_id,
            actor=actor,
            action="read",
        )
        effective = get_effective_library_markdown(session, version)
        if effective is None:
            raise ReadNotFoundError("Editable Library Markdown not found")
        return _projection(item, version, effective)

    def update_current(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        version_id: UUID,
        actor: ActorContext,
        body_markdown: str,
        expected_revision: int,
        expected_base_artifact_id: UUID,
        expected_base_content_sha256: str,
    ) -> LibrarySemanticMarkdownProjection:
        if not body_markdown or len(body_markdown) > BODY_MD_MAX_LENGTH:
            raise ReadValidationError(
                f"body_markdown must contain 1..{BODY_MD_MAX_LENGTH} characters"
            )
        encoded = body_markdown.encode("utf-8")
        if len(encoded) > LIBRARY_SEMANTIC_MARKDOWN_MAX_BYTES:
            raise ReadValidationError(
                "body_markdown exceeds the 1000000-byte UTF-8 limit"
            )
        if expected_revision < 0:
            raise ReadValidationError("expected_revision must be non-negative")

        acquire_write_intent(session)
        check_actor_can_access(actor, workspace_id, "write", session=session)
        # Flat lookup (P9.2): derive the item from the version rather than
        # carrying it in the URL, then lock the same way as before.
        version_seed = get_in_workspace(
            session,
            LibraryItemVersion,
            workspace_id=workspace_id,
            resource_id=version_id,
        )
        if version_seed is None:
            raise ReadNotFoundError("Editable Library Markdown not found")
        library_item_id = version_seed.library_item_id
        item = session.scalar(
            select(LibraryItem)
            .where(LibraryItem.id == library_item_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            item is None
            or item.workspace_id != workspace_id
            or item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
            or item.current_ready_version_id is None
            or item.current_ready_version_id != version_id
        ):
            raise ReadNotFoundError("Editable Library Markdown not found")
        if not actor_can_read_library_trust_level(item.trust_level, actor):
            # Keep this exact-item route non-oracular: callers without
            # restricted visibility must receive the same response as a
            # missing, foreign, stale, or otherwise non-canonical target.
            raise ReadNotFoundError("Editable Library Markdown not found")
        version = session.scalar(
            select(LibraryItemVersion)
            .where(
                LibraryItemVersion.id == item.current_ready_version_id,
                LibraryItemVersion.library_item_id == item.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if version is None:
            raise ReadNotFoundError("Editable Library Markdown not found")
        effective = get_effective_library_markdown(session, version)
        if effective is None:
            raise ReadNotFoundError("Editable Library Markdown not found")
        base_hash = effective.base_artifact.content_sha256
        if base_hash is None:
            raise ReadNotFoundError("Editable Library Markdown not found")
        if (
            effective.base_artifact.id != expected_base_artifact_id
            or not hmac.compare_digest(
                base_hash.lower(),
                expected_base_content_sha256.lower(),
            )
            or effective.revision != expected_revision
        ):
            raise StaleBaseVersionError(
                "Library Markdown base artifact or overlay revision is stale",
                latest_version_id=version.id,
                latest_content_hash=effective.content_sha256,
            )
        if body_markdown == effective.body_markdown:
            return _projection(item, version, effective)

        content_sha256 = hashlib.sha256(encoded).hexdigest()
        now = utc_now()
        overlay = effective.overlay
        if overlay is None:
            overlay = session.scalar(
                select(LibrarySemanticOverlay)
                .where(
                    LibrarySemanticOverlay.library_item_version_id == version.id,
                    LibrarySemanticOverlay.base_artifact_id
                    == effective.base_artifact.id,
                )
                .with_for_update()
            )
            if overlay is None:
                overlay = LibrarySemanticOverlay(
                    workspace_id=version.workspace_id,
                    library_item_version_id=version.id,
                    base_artifact_id=effective.base_artifact.id,
                    base_artifact_sha256=base_hash.lower(),
                    body_markdown=body_markdown,
                    content_sha256=content_sha256,
                    byte_size=len(encoded),
                    revision=1,
                    created_by_actor_kind=actor.actor_kind,
                    created_by_actor_id=str(actor.actor_id),
                    updated_by_actor_kind=actor.actor_kind,
                    updated_by_actor_id=str(actor.actor_id),
                    updated_at=now,
                )
                session.add(overlay)
                session.flush()
            else:
                overlay.body_markdown = body_markdown
                overlay.content_sha256 = content_sha256
                overlay.byte_size = len(encoded)
                overlay.revision += 1
                overlay.base_artifact_sha256 = base_hash.lower()
                overlay.invalidated_at = None
                overlay.invalidation_reason = None
                overlay.updated_by_actor_kind = actor.actor_kind
                overlay.updated_by_actor_id = str(actor.actor_id)
                overlay.updated_at = now
            version.selected_semantic_overlay_id = overlay.id
        else:
            overlay.body_markdown = body_markdown
            overlay.content_sha256 = content_sha256
            overlay.byte_size = len(encoded)
            overlay.revision += 1
            overlay.updated_by_actor_kind = actor.actor_kind
            overlay.updated_by_actor_id = str(actor.actor_id)
            overlay.updated_at = now
        item.updated_at = now
        item.content_changed_at = now
        session.flush()

        # Lexical search changes in the same transaction. Existing semantic
        # chunks are removed before the content-addressed replacement is queued,
        # so stale vectors cannot survive an authored save.
        sync_library_doc_sources(session, item)
        remove_document_chunks(
            session,
            document_kind="library_item",
            document_id=item.id,
        )
        self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="embed",
            trigger_type="library_semantic_overlay",
            idempotency_key=(
                f"library-semantic-overlay:{overlay.id}:"
                f"{overlay.revision}:{content_sha256}"
            ),
            target_ref={
                "kind": "library_item",
                "id": str(item.id),
                "library_item_version_id": str(version.id),
                "semantic_overlay_id": str(overlay.id),
                "semantic_overlay_revision": overlay.revision,
                "semantic_overlay_content_sha256": content_sha256,
            },
        )
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=str(actor.actor_id),
            action="library.semantic_overlay.saved",
            target_kind="library_semantic_overlay",
            target_id=overlay.id,
            payload={
                "library_item_id": str(item.id),
                "library_item_version_id": str(version.id),
                "base_artifact_id": str(effective.base_artifact.id),
                "base_artifact_sha256": base_hash.lower(),
                "revision": overlay.revision,
                "content_sha256": content_sha256,
                "byte_size": len(encoded),
            },
        )
        saved = get_effective_library_markdown(session, version)
        if saved is None or saved.overlay is None or saved.overlay.id != overlay.id:
            raise RuntimeError("saved Library semantic overlay is not effective")
        return _projection(item, version, saved)

    def resolve_content(
        self,
        *,
        session: Session,
        workspace_id: UUID,
        overlay_id: UUID,
        actor: ActorContext,
    ) -> LibrarySemanticOverlayContent:
        # Fully flat lookup (P9.2): item and version are both derived from the
        # overlay -- neither appears in the URL any more.
        overlay_seed = get_in_workspace(
            session,
            LibrarySemanticOverlay,
            workspace_id=workspace_id,
            resource_id=overlay_id,
        )
        if overlay_seed is None:
            raise ReadNotFoundError("Library Preview content not found")
        item, version = _authorize_current_ready(
            session,
            workspace_id=workspace_id,
            version_id=overlay_seed.library_item_version_id,
            actor=actor,
            action="read",
        )
        effective = get_effective_library_markdown(session, version)
        overlay = effective.overlay if effective is not None else None
        if overlay is None or overlay.id != overlay_id:
            raise ReadNotFoundError("Library Preview content not found")
        body = overlay.body_markdown.encode("utf-8")
        if (
            len(body) != overlay.byte_size
            or hashlib.sha256(body).hexdigest() != overlay.content_sha256
        ):
            raise ReadNotFoundError("Library Preview content not found")
        return LibrarySemanticOverlayContent(
            body=body,
            etag=_content_etag(
                session=session,
                actor=actor,
                item=item,
                version=version,
                overlay=overlay,
            ),
            filename=f"library-{item.id}-edited.md",
            library_item_id=item.id,
            library_item_version_id=version.id,
        )


def invalidate_selected_library_semantic_overlay(
    session: Session,
    *,
    version: LibraryItemVersion,
    reason: str,
) -> LibrarySemanticOverlay | None:
    """Detach the authored layer before a different base attempt is selected."""

    if version.selected_semantic_overlay_id is None:
        return None
    overlay = session.get(
        LibrarySemanticOverlay,
        version.selected_semantic_overlay_id,
    )
    if overlay is not None and overlay.library_item_version_id == version.id:
        overlay.invalidated_at = utc_now()
        overlay.invalidation_reason = reason
    version.selected_semantic_overlay_id = None
    return overlay


def _authorize_current_ready(
    session: Session,
    *,
    workspace_id: UUID,
    version_id: UUID,
    actor: ActorContext,
    action: str,
) -> tuple[LibraryItem, LibraryItemVersion]:
    """Flat lookup (P9.2): the item is derived from the version."""

    check_actor_can_access(actor, workspace_id, action, session=session)
    version_seed = get_in_workspace(
        session,
        LibraryItemVersion,
        workspace_id=workspace_id,
        resource_id=version_id,
    )
    if version_seed is None:
        raise ReadNotFoundError("Editable Library Markdown not found")
    item = session.get(LibraryItem, version_seed.library_item_id)
    if (
        item is None
        or item.workspace_id != workspace_id
        or item.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
        or item.current_ready_version_id is None
        or item.current_ready_version_id != version_id
    ):
        raise ReadNotFoundError("Editable Library Markdown not found")
    if not actor_can_read_library_trust_level(item.trust_level, actor):
        raise ReadNotFoundError("Editable Library Markdown not found")
    version = get_canonical_library_version(session, item)
    if version is None or version.id != item.current_ready_version_id:
        raise ReadNotFoundError("Editable Library Markdown not found")
    return item, version


def _projection(
    item: LibraryItem,
    version: LibraryItemVersion,
    effective: EffectiveLibraryMarkdown,
) -> LibrarySemanticMarkdownProjection:
    base_hash = effective.base_artifact.content_sha256
    if base_hash is None:
        raise ReadNotFoundError("Editable Library Markdown not found")
    overlay = effective.overlay
    return LibrarySemanticMarkdownProjection(
        library_item_id=item.id,
        version_id=version.id,
        base_artifact_id=effective.base_artifact.id,
        base_content_sha256=base_hash.lower(),
        overlay_id=overlay.id if overlay is not None else None,
        revision=effective.revision,
        source="overlay" if overlay is not None else "base",
        body_markdown=effective.body_markdown,
        content_sha256=effective.content_sha256,
        byte_size=effective.byte_size,
        updated_at=(
            overlay.updated_at
            if overlay is not None
            else effective.base_artifact.created_at
        ),
    )


def _content_etag(
    *,
    session: Session,
    actor: ActorContext,
    item: LibraryItem,
    version: LibraryItemVersion,
    overlay: LibrarySemanticOverlay,
) -> str:
    membership_role = None
    if actor.user_id is not None:
        membership = session.get(
            WorkspaceMembership,
            (actor.user_id, item.workspace_id),
        )
        membership_role = membership.role if membership is not None else None
    facts = {
        "schema": "library-semantic-overlay-content-v1",
        "actor": {
            "actor_kind": actor.actor_kind,
            "actor_id": str(actor.actor_id),
            "user_id": str(actor.user_id) if actor.user_id is not None else None,
            "workspace_id": (
                str(actor.workspace_id) if actor.workspace_id is not None else None
            ),
            "membership_role": membership_role,
            "scopes": sorted(actor.scopes),
            "token_kind": actor.token_kind,
            "token_id": str(actor.token_id) if actor.token_id is not None else None,
            "client_id": actor.client_id,
            "provenance": actor.provenance,
        },
        "association": {
            "workspace_id": str(item.workspace_id),
            "library_item_id": str(item.id),
            "version_id": str(version.id),
            "selected_conversion_attempt_id": (
                str(version.selected_conversion_attempt_id)
                if version.selected_conversion_attempt_id is not None
                else None
            ),
            "overlay_id": str(overlay.id),
            "overlay_revision": overlay.revision,
            "overlay_content_sha256": overlay.content_sha256,
            "base_artifact_id": str(overlay.base_artifact_id),
            "base_artifact_sha256": overlay.base_artifact_sha256,
            "lifecycle_status": item.lifecycle_status,
            "trust_level": item.trust_level,
        },
    }
    encoded = json.dumps(
        facts,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f'"lib-semantic-v1-{hashlib.sha256(encoded).hexdigest()[:32]}"'


__all__ = [
    "LibrarySemanticMarkdownProjection",
    "LibrarySemanticOverlayContent",
    "LibrarySemanticOverlayService",
    "invalidate_selected_library_semantic_overlay",
]
