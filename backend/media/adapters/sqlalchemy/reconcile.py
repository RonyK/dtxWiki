"""Crash reconciliation for durable Shared Media install operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from backend.media.domain import (
    AvailableBlobCandidate,
    BlobState,
    Sha256Digest,
)
from backend.media.ports.contracts import (
    GenerationFence,
    PhysicalChargeTarget,
    PhysicalObjectKind,
    StagedBlob,
    StorageKey,
)
from backend.media.ports.storage import (
    BlobPublisher,
    PhysicalObjectDeleter,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    StoredBlobInspectionRow,
)

from .install import SqlAlchemyMediaInstallJournal
from .repository import SqlAlchemyMediaRepository

_RECONCILE_BATCH_SIZE = 16
_RECONCILE_CLAIM_TTL = timedelta(hours=1)
_RESUMABLE_LEASE_STATES = ("reserved", "staged", "install_pending")
_RESUMABLE_REQUEST_STATES = ("pending", "publishing", "retry")
_TERMINAL_STAGING_FAILURE_CODES = {
    "media_staging_integrity",
    "media_staging_missing",
}


def _due_leases(workspace_id: UUID, now: datetime) -> Select[tuple[MediaStagingLease]]:
    """The same expired, reclaimable installs for probing and execution."""
    return (
        select(MediaStagingLease)
        .join(
            MediaInstallRequest,
            and_(
                MediaInstallRequest.workspace_id == MediaStagingLease.workspace_id,
                MediaInstallRequest.staging_id == MediaStagingLease.id,
                MediaInstallRequest.staging_generation == MediaStagingLease.generation,
            ),
        )
        .where(
            MediaStagingLease.workspace_id == workspace_id,
            MediaStagingLease.state.in_(_RESUMABLE_LEASE_STATES),
            MediaStagingLease.expires_at <= now,
            MediaInstallRequest.state.in_(_RESUMABLE_REQUEST_STATES),
            or_(
                MediaInstallRequest.state != "publishing",
                MediaInstallRequest.claim_token.is_(None),
                MediaInstallRequest.claim_expires_at.is_(None),
                MediaInstallRequest.claim_expires_at <= now,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class MediaInstallReconcileResult:
    inspected_count: int
    resumed_count: int
    cleaned_count: int
    deferred_count: int


class _WorkspaceBlobStore(BlobPublisher, PhysicalObjectDeleter, Protocol):
    """Structural shape of what ``store_for(workspace_id)`` returns here.

    D-5's BlobStoreProvider.store_for hands back one concrete object that
    plays both the publisher and deleter role (container.py's
    ``_new_validated_media_installer`` wires the same object into all four
    InstallValidatedBlob ports) -- this Protocol just names that shape
    without this adapters-layer module importing the concrete Library store
    type, which would invert the media/library layering.
    """


class SqlAlchemyMediaInstallReconciler:
    """Resume or safely terminalize expired pre-admitted installs.

    The caller owns one transaction. Filesystem effects happen while the
    Workspace media-intent lock is held; a crash before commit is safe because
    publication is no-overwrite and deletion replays as ``already_missing``.
    """

    def __init__(
        self,
        *,
        store_for: Callable[[UUID], _WorkspaceBlobStore],
        batch_size: int = _RECONCILE_BATCH_SIZE,
    ) -> None:
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size <= 0
        ):
            raise ValueError("media reconcile batch_size must be positive")
        # D-5: resolved per sweep call (each lease already carries its own
        # workspace_id), not cached at construction -- this reconciler is
        # built once per worker process and sweeps every workspace's expired
        # installs over its lifetime, so a store fixed here would route every
        # workspace's cleanup through whichever workspace happened to be
        # default at construction.
        self._store_for = store_for
        self._batch_size = batch_size

    @staticmethod
    def has_due_work(
        session: Session,
        *,
        workspace_id: UUID,
        now: datetime | None = None,
    ) -> bool:
        """Read without locks or storage access before scheduling a sweep."""
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id must be a UUID")
        now = now or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("media reconcile now must be timezone-aware")
        with session.no_autoflush:
            return bool(session.scalar(select(_due_leases(workspace_id, now).exists())))

    def sweep_expired(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        now: datetime | None = None,
    ) -> MediaInstallReconcileResult:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id must be a UUID")
        now = now or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("media reconcile now must be timezone-aware")

        journal = SqlAlchemyMediaInstallJournal.for_session(session)
        # The intent row is the first universal lock and serializes concurrent
        # sweepers with upload, install completion, quota, security, and GC.
        journal._lock_operation_intent(session, workspace_id)
        leases = session.scalars(
            _due_leases(workspace_id, now)
            .order_by(MediaStagingLease.expires_at, MediaStagingLease.id)
            .limit(self._batch_size)
        ).all()

        store = self._store_for(workspace_id)
        resumed_count = 0
        cleaned_count = 0
        deferred_count = 0
        for lease in leases:
            disposition = self._reconcile_one(
                session,
                journal=journal,
                lease=lease,
                now=now,
                store=store,
            )
            if disposition == "resumed":
                resumed_count += 1
            elif disposition == "cleaned":
                cleaned_count += 1
            else:
                deferred_count += 1
        return MediaInstallReconcileResult(
            inspected_count=len(leases),
            resumed_count=resumed_count,
            cleaned_count=cleaned_count,
            deferred_count=deferred_count,
        )

    def _reconcile_one(
        self,
        session: Session,
        *,
        journal: SqlAlchemyMediaInstallJournal,
        lease: MediaStagingLease,
        now: datetime,
        store: _WorkspaceBlobStore,
    ) -> str:
        charge = session.scalar(
            select(MediaPhysicalCharge).where(
                MediaPhysicalCharge.workspace_id == lease.workspace_id,
                MediaPhysicalCharge.staging_id == lease.id,
                MediaPhysicalCharge.target_generation == lease.generation,
            )
        )
        request = session.scalar(
            select(MediaInstallRequest).where(
                MediaInstallRequest.workspace_id == lease.workspace_id,
                MediaInstallRequest.staging_id == lease.id,
                MediaInstallRequest.staging_generation == lease.generation,
            )
        )
        if (
            charge is None
            or charge.state != "charged"
            or request is None
            or request.physical_charge_id != charge.id
            or lease.byte_size is None
            or lease.sha256 is None
        ):
            raise ValueError("expired media install bundle facts changed")
        incarnation = session.get(
            BlobIncarnationRow,
            {
                "workspace_id": lease.workspace_id,
                "blob_id": request.blob_id,
                "blob_epoch": request.blob_epoch,
            },
        )
        inspection = session.get(
            StoredBlobInspectionRow,
            {
                "workspace_id": lease.workspace_id,
                "id": request.inspection_id,
            },
        )
        if (
            incarnation is None
            or inspection is None
            or inspection.blob_id != request.blob_id
            or inspection.blob_epoch != request.blob_epoch
        ):
            raise ValueError("expired media install domain facts changed")

        target = PhysicalChargeTarget(
            workspace_id=lease.workspace_id,
            kind=PhysicalObjectKind.STAGING_LEASE,
            object_id=lease.id,
            generation=lease.generation,
        )
        staged = StagedBlob(
            target=target,
            storage_key=StorageKey(lease.storage_key),
            byte_size=lease.byte_size,
            sha256=Sha256Digest(lease.sha256),
        )
        candidate = AvailableBlobCandidate(
            workspace_id=lease.workspace_id,
            sha256=staged.sha256,
            byte_size=staged.byte_size,
            storage_key=incarnation.storage_key,
            canonical_media_type=inspection.canonical_media_type,
            format_id=inspection.format_id,
            detector_id=inspection.detector_id,
            detector_version=inspection.detector_version,
            registry_version=inspection.registry_version,
            validation_schema_version=inspection.validation_schema_version,
            metadata=inspection.metadata_json,
        )
        operation = journal.prepare_operation(
            candidate=candidate,
            target=target,
            actor_id=lease.actor_id,
            session_key=lease.session_key,
            idempotency_key=lease.idempotency_key,
            request_id=request.id,
            storage_key=staged.storage_key,
            charge_id=charge.id,
            proposed_blob_id=request.blob_id,
            proposed_blob_epoch=request.blob_epoch,
            publish_generation=request.publish_generation,
            admission_max_bytes=lease.max_bytes,
            now=now,
            expires_at=now + _RECONCILE_CLAIM_TTL,
            admit_new_operation=False,
        )
        if operation.in_progress:
            return "deferred"
        if operation.reused is not None:
            deletion = store.delete(
                staged.storage_key,
                target=staged.target,
            )
            journal.release_staging(
                staged,
                charge_id=charge.id,
                deletion=deletion,
            )
            return "resumed"

        operation_claim = operation.claim_fence
        assert operation_claim is not None
        journal.record_staged(
            staged,
            charge_id=charge.id,
            operation_claim=operation_claim,
        )
        blob_charge_id = uuid5(
            NAMESPACE_URL,
            f"media-blob-charge:{lease.workspace_id}:{request.blob_id}:"
            f"{request.blob_epoch}",
        )
        prepared = journal.prepare_install(
            candidate=candidate,
            staged=staged,
            staging_charge_id=charge.id,
            blob_charge_id=blob_charge_id,
            request_id=request.id,
            proposed_blob_id=request.blob_id,
            proposed_blob_epoch=request.blob_epoch,
            destination=StorageKey(incarnation.storage_key),
            publish_generation=request.publish_generation,
            idempotency_key=lease.idempotency_key,
            claim_token=uuid4().hex,
            operation_claim=operation_claim,
        )
        if prepared.in_progress:
            return "deferred"
        if prepared.reused is not None:
            deletion = store.delete(
                staged.storage_key,
                target=staged.target,
            )
            journal.release_staging(
                staged,
                charge_id=charge.id,
                deletion=deletion,
            )
            return "resumed"

        pending = prepared.pending
        assert pending is not None
        try:
            publish = store.publish_no_overwrite(
                staged,
                target=pending.destination,
                fence=GenerationFence(pending.blob, pending.publish_generation),
            )
        except Exception as exc:
            failure_code = getattr(exc, "code", None)
            if failure_code not in _TERMINAL_STAGING_FAILURE_CODES:
                raise
            deletion = store.delete(
                staged.storage_key,
                target=staged.target,
            )
            journal.release_staging(
                staged,
                charge_id=charge.id,
                deletion=deletion,
            )
            self._mark_source_missing(
                session,
                request=request,
                failure_code=failure_code,
            )
            return "cleaned"

        staging_deletion = store.delete(
            staged.storage_key,
            target=staged.target,
        )
        journal.complete_install(
            pending,
            publish=publish,
            staging_deletion=staging_deletion,
        )
        return "resumed"

    @staticmethod
    def _mark_source_missing(
        session: Session,
        *,
        request: MediaInstallRequest,
        failure_code: str,
    ) -> None:
        media = SqlAlchemyMediaRepository(session)
        aggregate = media.get_by_blob_id(request.workspace_id, request.blob_id)
        if aggregate is None or aggregate.active is None:
            raise ValueError("missing media install lost its pending blob")
        if aggregate.active.state is BlobState.PENDING_INSTALL:
            media.put(
                aggregate.update_active(aggregate.active.transition(BlobState.MISSING))
            )
        elif aggregate.active.state is not BlobState.MISSING:
            raise ValueError("missing media install blob state changed")

        request.state = "failed"
        request.claim_token = None
        request.claim_expires_at = None
        request.next_attempt_at = None
        request.last_failure_code = failure_code
        request.updated_at = datetime.now(UTC)
        outbox = session.scalar(
            select(MediaOutboxRow).where(
                MediaOutboxRow.workspace_id == request.workspace_id,
                MediaOutboxRow.topic == "InstallRequested",
                MediaOutboxRow.idempotency_key == f"install-requested:{request.id}",
            )
        )
        if outbox is not None and outbox.status != "delivered":
            outbox.status = "failed"
            outbox.claim_token = None
            outbox.claim_expires_at = None
            outbox.next_attempt_at = None
            outbox.last_failure_code = failure_code


__all__ = [
    "MediaInstallReconcileResult",
    "SqlAlchemyMediaInstallReconciler",
]
