"""SQLAlchemy registration of validated bytes in the Shared Media kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    BlobReference,
    BlobState,
    StoredBlob,
    WorkspaceBlobIdentity,
)
from backend.media.ports.contracts import (
    PhysicalChargeTarget,
    PhysicalObjectKind,
    StorageKey,
)
from backend.media.ports.persistence import MediaRepository
from backend.media.ports.quota import PhysicalQuota
from backend.models.media import (
    MediaPhysicalCharge,
    SecurityBlockedBlobRow,
    StoredBlobInspectionRow,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace

DEFAULT_WORKSPACE_MEDIA_QUOTA_BYTES = 10_000_000_000


class SqlAlchemyAvailableBlobRepository:
    def __init__(
        self,
        session: Session,
        *,
        media_repository: MediaRepository,
        physical_quota: PhysicalQuota,
    ) -> None:
        self._session = session
        self._media = media_repository
        self._physical_quota = physical_quota

    def find(self, identity: WorkspaceBlobIdentity) -> AvailableBlobRecord | None:
        blob = self._media.find_by_identity(identity)
        if blob is None or blob.active is None:
            return None
        if blob.active.state is not BlobState.AVAILABLE:
            raise ValueError("shared blob is not available")
        reference = BlobReference(
            workspace_id=identity.workspace_id,
            blob_id=blob.blob_id,
            epoch=blob.active.epoch,
        )
        if self._security_blocked(reference):
            raise ValueError("shared blob is security blocked")
        inspection = self._latest_inspection(
            identity.workspace_id,
            blob.blob_id,
            blob.active.epoch.value,
        )
        if inspection is None:
            raise RuntimeError("available blob inspection is missing")
        return AvailableBlobRecord(
            reference=reference,
            sha256=identity.sha256,
            byte_size=blob.active.byte_size,
            storage_key=blob.active.storage_key,
            canonical_media_type=inspection.canonical_media_type,
            inspection_id=inspection.id,
        )

    def add_available(self, candidate: AvailableBlobCandidate) -> AvailableBlobRecord:
        self._ensure_workspace_scope(candidate.workspace_id)
        aggregate = StoredBlob(
            blob_id=uuid4(),
            identity=WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256),
        ).allocate(byte_size=candidate.byte_size, storage_key=candidate.storage_key)
        self._media.put(aggregate)
        assert aggregate.active is not None
        reference = BlobReference(
            workspace_id=candidate.workspace_id,
            blob_id=aggregate.blob_id,
            epoch=aggregate.active.epoch,
        )
        charge_id = uuid5(
            NAMESPACE_URL,
            f"media-charge:{candidate.workspace_id}:{aggregate.blob_id}:"
            f"{aggregate.active.epoch.value}",
        )
        self._physical_quota.reserve(
            candidate.workspace_id,
            charge_id=charge_id,
            target=PhysicalChargeTarget(
                workspace_id=candidate.workspace_id,
                kind=PhysicalObjectKind.BLOB_INCARNATION,
                object_id=aggregate.blob_id,
                generation=1,
                blob=reference,
            ),
            storage_key=StorageKey(candidate.storage_key),
            bytes_=candidate.byte_size,
        )
        available = aggregate.update_active(
            aggregate.active.transition(BlobState.AVAILABLE, generation=1)
        )
        self._media.put(available)
        charge = self._session.get(
            MediaPhysicalCharge,
            {"workspace_id": candidate.workspace_id, "id": charge_id},
        )
        if charge is None:
            raise RuntimeError("shared blob charge was not persisted")
        charge.accounting_kind = "available"
        inspection_id = self._add_inspection(candidate, reference)
        self._session.flush()
        return AvailableBlobRecord(
            reference=reference,
            sha256=candidate.sha256,
            byte_size=candidate.byte_size,
            storage_key=candidate.storage_key,
            canonical_media_type=candidate.canonical_media_type,
            inspection_id=inspection_id,
        )

    def _ensure_workspace_scope(self, workspace_id: UUID) -> None:
        workspace = self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError("Workspace was not found")
        now = datetime.now(UTC)
        if self._session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
            self._session.add(
                WorkspaceMediaWriteIntent(
                    workspace_id=workspace_id,
                    state="open",
                    delete_generation=0,
                    updated_at=now,
                )
            )
            self._session.flush()
        if self._session.get(WorkspaceMediaQuotaUsage, workspace_id) is None:
            self._session.add(
                WorkspaceMediaQuotaUsage(
                    workspace_id=workspace_id,
                    limit_bytes=DEFAULT_WORKSPACE_MEDIA_QUOTA_BYTES,
                    charged_bytes=0,
                    revision=0,
                    updated_at=now,
                )
            )
            self._session.flush()

    def _add_inspection(
        self,
        candidate: AvailableBlobCandidate,
        reference: BlobReference,
    ) -> UUID:
        inspection_id = uuid5(
            NAMESPACE_URL,
            f"media-inspection:{reference.workspace_id}:{reference.blob_id}:"
            f"{reference.epoch.value}:{candidate.registry_version}:"
            f"{candidate.detector_id}:{candidate.detector_version}:"
            f"{candidate.format_id}:{candidate.canonical_media_type}:"
            f"{candidate.validation_schema_version}",
        )
        if (
            self._session.get(
                StoredBlobInspectionRow,
                {"workspace_id": reference.workspace_id, "id": inspection_id},
            )
            is None
        ):
            self._session.add(
                StoredBlobInspectionRow(
                    workspace_id=reference.workspace_id,
                    id=inspection_id,
                    blob_id=reference.blob_id,
                    blob_epoch=reference.epoch.value,
                    registry_version=candidate.registry_version,
                    detector_id=candidate.detector_id,
                    detector_version=candidate.detector_version,
                    format_id=candidate.format_id,
                    canonical_media_type=candidate.canonical_media_type,
                    validation_schema_version=candidate.validation_schema_version,
                    metadata_json=dict(candidate.metadata),
                    scanner_verdict="not_configured",
                    scanner_provider=None,
                    scanner_version=None,
                    ordinary_available=True,
                    created_at=datetime.now(UTC),
                )
            )
        return inspection_id

    def _latest_inspection(
        self,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
    ) -> StoredBlobInspectionRow | None:
        return self._session.scalar(
            select(StoredBlobInspectionRow)
            .where(
                StoredBlobInspectionRow.workspace_id == workspace_id,
                StoredBlobInspectionRow.blob_id == blob_id,
                StoredBlobInspectionRow.blob_epoch == blob_epoch,
                StoredBlobInspectionRow.ordinary_available.is_(True),
            )
            .order_by(StoredBlobInspectionRow.created_at.desc())
            .limit(1)
        )

    def _security_blocked(self, reference: BlobReference) -> bool:
        return (
            self._session.get(
                SecurityBlockedBlobRow,
                {
                    "workspace_id": reference.workspace_id,
                    "blob_id": reference.blob_id,
                    "blob_epoch": reference.epoch.value,
                },
            )
            is not None
        )
