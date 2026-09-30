"""Transactional cross-domain blob-reference ledger."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.media.domain import BlobReference, BlobState
from backend.media.ports.contracts import IdempotencyConflictError
from backend.models.base import utc_now
from backend.models.media import (
    BlobIncarnationRow,
    MediaBlobGcCandidate,
    MediaBlobGcHold,
    MediaBlobReference,
    WorkspaceMediaWriteIntent,
)


class BlobReferenceOwnerKind(StrEnum):
    CONTEXT_MEDIA_ASSET = "context_media_asset"
    LIBRARY_ITEM_VERSION = "library_item_version"
    # A-1: conversion- and preview-stage artifacts share one physical table
    # now, so both stages register under this same owner_kind.
    LIBRARY_ARTIFACT = "library_artifact"


class BlobReferenceUnavailableError(RuntimeError):
    """The selected incarnation can no longer accept a new durable owner."""


def lock_workspace_blob_reference_intent(
    session: Session,
    workspace_id: UUID,
    *,
    allow_deleting: bool,
) -> WorkspaceMediaWriteIntent:
    """Take the first lock shared by domain writers and physical blob GC."""

    if not isinstance(workspace_id, UUID):
        raise ValueError("workspace_id must be a UUID")
    intent = session.scalar(
        select(WorkspaceMediaWriteIntent)
        .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
        .with_for_update(read=True)
    )
    if intent is None:
        raise ValueError("workspace_media_intent_not_configured")
    if intent.state == "deleting" and not allow_deleting:
        raise ValueError("workspace_media_deleting")
    if intent.state not in {"open", "deleting"}:
        raise RuntimeError("invalid_workspace_media_intent_state")
    return intent


class SqlAlchemyBlobReferenceLedger:
    """Maintain domain ownership edges under the incarnation's GC lock.

    Every registration and removal takes the same
    ``workspace-media-intent -> blob-incarnation`` lock order used by physical
    GC. A registration can cancel a queued (not yet marked) collection; once
    GC has advanced the incarnation away from ``available``, callers must retry
    their install and receive a new incarnation instead of reviving stale
    bytes.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def register(
        self,
        *,
        owner_kind: BlobReferenceOwnerKind,
        owner_id: UUID,
        blob: BlobReference,
    ) -> bool:
        owner_kind = _owner_kind(owner_kind)
        _owner_id(owner_id)
        if not isinstance(blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        incarnation = self._lock_incarnation(blob, allow_deleting=False)
        if incarnation.state != BlobState.AVAILABLE.value:
            raise BlobReferenceUnavailableError(
                f"blob incarnation is not available: {incarnation.state}"
            )

        existing = self._session.scalar(
            select(MediaBlobReference)
            .where(
                MediaBlobReference.workspace_id == blob.workspace_id,
                MediaBlobReference.owner_kind == owner_kind.value,
                MediaBlobReference.owner_id == owner_id,
            )
            .with_for_update()
        )
        if existing is not None and (existing.blob_id, existing.blob_epoch) != (
            blob.blob_id,
            blob.epoch.value,
        ):
            raise IdempotencyConflictError("media_blob_reference_owner")

        # The candidate has not advanced the DB generation yet: cancelling it
        # in this transaction is safe and makes an exact association replay
        # win before physical GC starts. Also self-heal an impossible queued
        # candidate found beside an existing reference.
        self._session.execute(
            delete(MediaBlobGcCandidate).where(
                MediaBlobGcCandidate.workspace_id == blob.workspace_id,
                MediaBlobGcCandidate.blob_id == blob.blob_id,
                MediaBlobGcCandidate.blob_epoch == blob.epoch.value,
            )
        )
        if owner_kind is BlobReferenceOwnerKind.CONTEXT_MEDIA_ASSET:
            # Context's synchronous association lease is completed by the
            # exact owner write. Deleting the hold beside the reference and
            # candidate means GC observes either the pending lease or the
            # durable owner after commit, never an unfenced gap.
            self._session.execute(
                delete(MediaBlobGcHold).where(
                    MediaBlobGcHold.workspace_id == blob.workspace_id,
                    MediaBlobGcHold.producer_kind == "context_media_upload",
                    MediaBlobGcHold.producer_id == owner_id,
                    MediaBlobGcHold.blob_id == blob.blob_id,
                    MediaBlobGcHold.blob_epoch == blob.epoch.value,
                )
            )
        if existing is not None:
            return False
        self._session.add(
            MediaBlobReference(
                workspace_id=blob.workspace_id,
                owner_kind=owner_kind.value,
                owner_id=owner_id,
                blob_id=blob.blob_id,
                blob_epoch=blob.epoch.value,
            )
        )
        self._session.flush()
        return True

    def unregister(
        self,
        *,
        owner_kind: BlobReferenceOwnerKind,
        owner_id: UUID,
        blob: BlobReference,
    ) -> bool:
        owner_kind = _owner_kind(owner_kind)
        _owner_id(owner_id)
        if not isinstance(blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        incarnation = self._lock_incarnation(blob, allow_deleting=True)
        existing = self._session.scalar(
            select(MediaBlobReference)
            .where(
                MediaBlobReference.workspace_id == blob.workspace_id,
                MediaBlobReference.owner_kind == owner_kind.value,
                MediaBlobReference.owner_id == owner_id,
            )
            .with_for_update()
        )
        if existing is not None and (existing.blob_id, existing.blob_epoch) != (
            blob.blob_id,
            blob.epoch.value,
        ):
            raise IdempotencyConflictError("media_blob_reference_owner")
        if existing is not None:
            self._session.delete(existing)
            self._session.flush()

        another_owner = self._session.scalar(
            select(MediaBlobReference.owner_id)
            .where(
                MediaBlobReference.workspace_id == blob.workspace_id,
                MediaBlobReference.blob_id == blob.blob_id,
                MediaBlobReference.blob_epoch == blob.epoch.value,
            )
            .limit(1)
        )
        if another_owner is None and incarnation.state != BlobState.PURGED.value:
            candidate = self._session.get(
                MediaBlobGcCandidate,
                {
                    "workspace_id": blob.workspace_id,
                    "blob_id": blob.blob_id,
                    "blob_epoch": blob.epoch.value,
                },
            )
            if candidate is None:
                now = utc_now()
                self._session.add(
                    MediaBlobGcCandidate(
                        workspace_id=blob.workspace_id,
                        blob_id=blob.blob_id,
                        blob_epoch=blob.epoch.value,
                        reason="last_reference_removed",
                        requested_at=now,
                        eligible_at=now,
                    )
                )
                self._session.flush()
        return existing is not None

    def _lock_incarnation(
        self,
        blob: BlobReference,
        *,
        allow_deleting: bool,
    ) -> BlobIncarnationRow:
        lock_workspace_blob_reference_intent(
            self._session,
            blob.workspace_id,
            allow_deleting=allow_deleting,
        )
        incarnation = self._session.scalar(
            select(BlobIncarnationRow)
            .where(
                BlobIncarnationRow.workspace_id == blob.workspace_id,
                BlobIncarnationRow.blob_id == blob.blob_id,
                BlobIncarnationRow.blob_epoch == blob.epoch.value,
            )
            .with_for_update()
        )
        if incarnation is None:
            raise BlobReferenceUnavailableError("blob incarnation was not found")
        return incarnation


def _owner_kind(value: BlobReferenceOwnerKind) -> BlobReferenceOwnerKind:
    if not isinstance(value, BlobReferenceOwnerKind):
        raise ValueError("owner_kind must be a BlobReferenceOwnerKind")
    return value


def _owner_id(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError("owner_id must be a UUID")
    return value


__all__ = [
    "BlobReferenceOwnerKind",
    "BlobReferenceUnavailableError",
    "SqlAlchemyBlobReferenceLedger",
    "lock_workspace_blob_reference_intent",
]
