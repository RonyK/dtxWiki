"""SQLAlchemy bridge from Library provenance to shared-media persistence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.library_media.domain import LibraryBlobCandidate, LibraryBlobRecord
from backend.library_media.ports import (
    LibraryLogicalQuotaExceededError,
    LibraryLogicalQuotaKind,
)
from backend.media.domain import (
    BlobReference,
    BlobState,
    Sha256Digest,
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
from backend.models.library import LibraryLogicalCharge, LibraryLogicalQuotaUsage
from backend.models.media import (
    BlobIncarnationRow,
    MediaPhysicalCharge,
    SecurityBlockedBlobRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace
from backend.services.db_write_intent import acquire_write_intent

DEFAULT_WORKSPACE_MEDIA_QUOTA_BYTES = 10_000_000_000
DEFAULT_LIBRARY_LOGICAL_QUOTA_BYTES = 10_000_000_000


class SqlAlchemyLibraryBlobRepository:
    """Store Library byte identity without introducing a Library blob table."""

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

    def find(self, identity: WorkspaceBlobIdentity) -> LibraryBlobRecord | None:
        blob = self._media.find_by_identity(identity)
        if blob is None or blob.active is None:
            return None
        if blob.active.state is not BlobState.AVAILABLE:
            raise ValueError("Library shared blob is not available")
        reference = BlobReference(
            workspace_id=identity.workspace_id,
            blob_id=blob.blob_id,
            epoch=blob.active.epoch,
        )
        if self._security_blocked(reference):
            raise ValueError("Library shared blob is security blocked")
        inspection = self._latest_inspection(
            identity.workspace_id,
            blob.blob_id,
            blob.active.epoch.value,
        )
        if inspection is None:
            raise ValueError("Library shared blob has no ordinary inspection")
        return LibraryBlobRecord(
            reference=reference,
            sha256=identity.sha256,
            byte_size=blob.active.byte_size,
            storage_key=blob.active.storage_key,
            media_type=inspection.canonical_media_type,
        )

    def get(self, reference: BlobReference) -> LibraryBlobRecord | None:
        root = self._session.get(
            StoredBlobRow,
            {"workspace_id": reference.workspace_id, "id": reference.blob_id},
        )
        incarnation = self._session.get(
            BlobIncarnationRow,
            {
                "workspace_id": reference.workspace_id,
                "blob_id": reference.blob_id,
                "blob_epoch": reference.epoch.value,
            },
        )
        if root is None or incarnation is None:
            return None
        if incarnation.state != BlobState.AVAILABLE.value:
            return None
        if self._security_blocked(reference):
            return None
        inspection = self._latest_inspection(
            reference.workspace_id,
            reference.blob_id,
            reference.epoch.value,
        )
        if inspection is None:
            return None
        return LibraryBlobRecord(
            reference=reference,
            sha256=Sha256Digest(root.sha256),
            byte_size=incarnation.byte_size,
            storage_key=incarnation.storage_key,
            media_type=inspection.canonical_media_type,
        )

    def add_available(self, candidate: LibraryBlobCandidate) -> LibraryBlobRecord:
        self._ensure_workspace_scope(candidate.workspace_id)
        aggregate = StoredBlob(
            blob_id=uuid4(),
            identity=WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256),
        ).allocate(
            byte_size=candidate.byte_size,
            storage_key=candidate.storage_key,
        )
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
            raise RuntimeError("Library blob charge was not persisted")
        charge.accounting_kind = "available"
        self._add_inspection(candidate, reference)
        self._session.flush()
        return LibraryBlobRecord(
            reference=reference,
            sha256=candidate.sha256,
            byte_size=candidate.byte_size,
            storage_key=candidate.storage_key,
            media_type=candidate.media_type,
        )

    def _ensure_workspace_scope(self, workspace_id: UUID) -> None:
        workspace = self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError("Workspace was not found")
        intent = self._session.get(WorkspaceMediaWriteIntent, workspace_id)
        now = datetime.now(UTC)
        if intent is None:
            self._session.add(
                WorkspaceMediaWriteIntent(
                    workspace_id=workspace_id,
                    state="open",
                    delete_generation=0,
                    updated_at=now,
                )
            )
            self._session.flush()
        usage = self._session.get(WorkspaceMediaQuotaUsage, workspace_id)
        if usage is None:
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
        candidate: LibraryBlobCandidate,
        reference: BlobReference,
    ) -> None:
        inspection_id = uuid5(
            NAMESPACE_URL,
            f"library-inspection:{reference.workspace_id}:{reference.blob_id}:"
            f"{reference.epoch.value}:{candidate.detector_id}:"
            f"{candidate.format_id}:{candidate.media_type}",
        )
        existing = self._session.get(
            StoredBlobInspectionRow,
            {"workspace_id": reference.workspace_id, "id": inspection_id},
        )
        if existing is not None:
            return
        self._session.add(
            StoredBlobInspectionRow(
                workspace_id=reference.workspace_id,
                id=inspection_id,
                blob_id=reference.blob_id,
                blob_epoch=reference.epoch.value,
                registry_version="library-v1",
                detector_id=candidate.detector_id,
                detector_version="1",
                format_id=candidate.format_id,
                canonical_media_type=candidate.media_type,
                validation_schema_version="library-v1",
                metadata_json=dict(candidate.metadata),
                scanner_verdict="not_configured",
                scanner_provider=None,
                scanner_version=None,
                ordinary_available=True,
                created_at=datetime.now(UTC),
            )
        )

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


class SqlAlchemyLibraryLogicalQuota:
    """Atomic Library source/derived association quota ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure_capacity(
        self,
        workspace_id: UUID,
        *,
        quota_kind: LibraryLogicalQuotaKind,
        additional_bytes: int,
    ) -> None:
        if (
            isinstance(additional_bytes, bool)
            or not isinstance(additional_bytes, int)
            or additional_bytes < 0
        ):
            raise ValueError("Library logical quota bytes must be non-negative")
        usage = self._locked_logical_usage(workspace_id, quota_kind)
        if usage.charged_bytes + additional_bytes > usage.limit_bytes:
            raise LibraryLogicalQuotaExceededError(limit_bytes=usage.limit_bytes)

    def reserve(
        self,
        workspace_id: UUID,
        *,
        quota_kind: LibraryLogicalQuotaKind,
        associations: Mapping[UUID, int],
    ) -> None:
        normalized = _normalized_associations(associations)
        if not normalized:
            return
        usage = self._locked_logical_usage(workspace_id, quota_kind)
        existing = {
            row.association_id: row
            for row in self._session.scalars(
                select(LibraryLogicalCharge)
                .where(
                    LibraryLogicalCharge.workspace_id == workspace_id,
                    LibraryLogicalCharge.quota_kind == quota_kind,
                    LibraryLogicalCharge.association_id.in_(normalized),
                )
                .with_for_update()
            )
        }
        delta = 0
        for association_id, byte_size in normalized.items():
            charge = existing.get(association_id)
            if charge is None:
                delta += byte_size
                continue
            if charge.byte_size != byte_size or charge.state != "charged":
                raise ValueError("library logical quota association conflict")
        if usage.charged_bytes + delta > usage.limit_bytes:
            raise LibraryLogicalQuotaExceededError(limit_bytes=usage.limit_bytes)
        now = datetime.now(UTC)
        for association_id, byte_size in normalized.items():
            if association_id in existing:
                continue
            self._session.add(
                LibraryLogicalCharge(
                    workspace_id=workspace_id,
                    id=uuid5(
                        NAMESPACE_URL,
                        f"library-logical:{workspace_id}:{quota_kind}:{association_id}",
                    ),
                    quota_kind=quota_kind,
                    association_id=association_id,
                    byte_size=byte_size,
                    state="charged",
                    created_at=now,
                )
            )
        if delta:
            usage.charged_bytes += delta
            usage.revision += 1
            usage.updated_at = now
        self._session.flush()

    def release(
        self,
        workspace_id: UUID,
        *,
        quota_kind: LibraryLogicalQuotaKind,
        association_ids: set[UUID],
    ) -> None:
        if not association_ids:
            return
        usage = self._locked_logical_usage(workspace_id, quota_kind)
        charges = tuple(
            self._session.scalars(
                select(LibraryLogicalCharge)
                .where(
                    LibraryLogicalCharge.workspace_id == workspace_id,
                    LibraryLogicalCharge.quota_kind == quota_kind,
                    LibraryLogicalCharge.association_id.in_(association_ids),
                )
                .with_for_update()
            )
        )
        released = sum(row.byte_size for row in charges if row.state == "charged")
        if released > usage.charged_bytes:
            raise RuntimeError("Library logical quota counter drift")
        now = datetime.now(UTC)
        for charge in charges:
            if charge.state == "charged":
                charge.state = "released"
                charge.released_at = now
        if released:
            usage.charged_bytes -= released
            usage.revision += 1
            usage.updated_at = now
        self._session.flush()

    def _locked_logical_usage(
        self,
        workspace_id: UUID,
        quota_kind: LibraryLogicalQuotaKind,
    ) -> LibraryLogicalQuotaUsage:
        if quota_kind not in {"source", "derived"}:
            raise ValueError("Library logical quota kind is invalid")
        acquire_write_intent(self._session)
        _lock_open_workspace_media_intent(self._session, workspace_id)
        values = {
            "workspace_id": workspace_id,
            "quota_kind": quota_kind,
            "limit_bytes": DEFAULT_LIBRARY_LOGICAL_QUOTA_BYTES,
            "charged_bytes": 0,
            "revision": 0,
            "updated_at": datetime.now(UTC),
        }
        dialect = self._session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(LibraryLogicalQuotaUsage).values(**values)
        elif dialect == "postgresql":
            statement = postgresql_insert(LibraryLogicalQuotaUsage).values(**values)
        else:
            raise RuntimeError("unsupported Library logical quota database")
        self._session.execute(
            statement.on_conflict_do_nothing(
                index_elements=["workspace_id", "quota_kind"]
            )
        )
        usage = self._session.scalar(
            select(LibraryLogicalQuotaUsage)
            .where(
                LibraryLogicalQuotaUsage.workspace_id == workspace_id,
                LibraryLogicalQuotaUsage.quota_kind == quota_kind,
            )
            .with_for_update()
        )
        if usage is None:
            raise RuntimeError("Library logical quota usage was not provisioned")
        return usage


def _lock_open_workspace_media_intent(
    session: Session, workspace_id: UUID
) -> WorkspaceMediaWriteIntent:
    intent = session.scalar(
        select(WorkspaceMediaWriteIntent)
        .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
        .with_for_update(read=True)
    )
    if intent is None:
        if session.get(Workspace, workspace_id) is None:
            raise ValueError("Workspace was not found")
        intent = WorkspaceMediaWriteIntent(
            workspace_id=workspace_id,
            state="open",
            delete_generation=0,
            updated_at=datetime.now(UTC),
        )
        session.add(intent)
        session.flush()
    if intent.state == "deleting":
        raise ValueError("workspace_media_deleting")
    if intent.state != "open":
        raise RuntimeError("invalid_workspace_media_intent_state")
    return intent


def _normalized_associations(associations: Mapping[UUID, int]) -> dict[UUID, int]:
    normalized: dict[UUID, int] = {}
    for association_id, byte_size in associations.items():
        if not isinstance(association_id, UUID):
            raise ValueError("logical quota association id must be a UUID")
        if (
            isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 0
        ):
            raise ValueError("logical quota bytes must be non-negative")
        normalized[association_id] = byte_size
    return normalized


__all__ = ["SqlAlchemyLibraryBlobRepository", "SqlAlchemyLibraryLogicalQuota"]
