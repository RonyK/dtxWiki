"""DB-authoritative physical quota and deletion-receipt adapter."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.media.domain import BlobEpoch, BlobReference
from backend.media.ports.contracts import (
    DeletionReceipt,
    IdempotencyConflictError,
    PhysicalChargeTarget,
    PhysicalDeleteResult,
    PhysicalObjectKind,
    StorageKey,
    UnsupportedCapabilityError,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaDeletionReceipt,
    MediaPhysicalCharge,
    MediaStagingLease,
    WorkspaceMediaQuotaUsage,
)

from .coordination import lock_workspace_media_intent

_ChargePayload = tuple[
    str,
    UUID,
    int,
    int,
    UUID | None,
    UUID | None,
    int | None,
    str,
    str,
    int,
]


class SqlAlchemyPhysicalQuota:
    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve(
        self,
        workspace_id: UUID,
        *,
        charge_id: UUID,
        target: PhysicalChargeTarget,
        storage_key: StorageKey,
        bytes_: int,
    ) -> None:
        if not isinstance(workspace_id, UUID) or not isinstance(charge_id, UUID):
            raise ValueError("quota identities must be UUIDs")
        if not isinstance(target, PhysicalChargeTarget):
            raise ValueError("target must be a PhysicalChargeTarget")
        if target.workspace_id != workspace_id:
            raise ValueError("physical target workspace mismatch")
        if not isinstance(storage_key, StorageKey):
            raise ValueError("storage_key must be a StorageKey")
        if isinstance(bytes_, bool) or not isinstance(bytes_, int) or bytes_ < 0:
            raise ValueError("bytes_ must be a non-negative integer")

        lock_workspace_media_intent(
            self._session,
            workspace_id,
            allow_deleting=False,
        )
        usage = self._locked_usage(workspace_id)
        facts = self._target_facts(target, storage_key, bytes_)
        existing = self._session.scalar(
            select(MediaPhysicalCharge)
            .where(
                MediaPhysicalCharge.workspace_id == workspace_id,
                MediaPhysicalCharge.id == charge_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if self._charge_payload(existing) != facts or existing.state == "released":
                raise IdempotencyConflictError("physical_quota_reservation")
            return
        if usage.charged_bytes + bytes_ > usage.limit_bytes:
            raise ValueError("media_quota_exceeded")
        now = datetime.now(UTC)
        self._session.add(
            MediaPhysicalCharge(
                workspace_id=workspace_id,
                id=charge_id,
                target_kind=facts[0],
                target_object_id=facts[1],
                target_epoch=facts[2],
                target_generation=facts[3],
                staging_id=facts[4],
                blob_id=facts[5],
                blob_epoch=facts[6],
                accounting_kind=facts[7],
                storage_key=facts[8],
                byte_size=facts[9],
                state="charged",
                created_at=now,
            )
        )
        usage.charged_bytes += bytes_
        usage.revision += 1
        usage.updated_at = now
        self._session.flush()

    def release_after_deletion(
        self,
        workspace_id: UUID,
        *,
        charge_id: UUID,
        deletion: PhysicalDeleteResult,
    ) -> DeletionReceipt:
        if not isinstance(deletion, PhysicalDeleteResult):
            raise ValueError("deletion must be a PhysicalDeleteResult")
        lock_workspace_media_intent(
            self._session,
            workspace_id,
            allow_deleting=True,
        )
        usage = self._locked_usage(workspace_id)
        charge = self._session.scalar(
            select(MediaPhysicalCharge)
            .where(
                MediaPhysicalCharge.workspace_id == workspace_id,
                MediaPhysicalCharge.id == charge_id,
            )
            .with_for_update()
        )
        if charge is None:
            raise ValueError("physical_charge_not_found")
        target = self._domain_target(charge)
        storage_key = StorageKey(charge.storage_key)
        if (deletion.target, deletion.storage_key) != (target, storage_key):
            raise ValueError("physical_deletion_binding_mismatch")
        existing = self._receipt_for_charge(workspace_id, charge_id)
        if charge.state == "released":
            if existing is None:
                raise RuntimeError("released_charge_missing_receipt")
            return self._domain_receipt(existing, target)
        if existing is not None:
            raise RuntimeError("unreleased_charge_has_receipt")
        if charge.state not in {"charged", "deletion_pending"}:
            raise RuntimeError("invalid_physical_charge_state")
        if usage.charged_bytes < charge.byte_size:
            raise RuntimeError("physical_quota_counter_drift")

        receipt_id = uuid5(
            NAMESPACE_URL,
            f"physical-delete:{workspace_id}:{charge.target_kind}:"
            f"{charge.target_object_id}:{charge.target_epoch}:"
            f"{charge.target_generation}:{charge_id}:{charge.storage_key}:"
            f"{charge.byte_size}",
        )
        now = datetime.now(UTC)
        receipt = MediaDeletionReceipt(
            workspace_id=workspace_id,
            id=receipt_id,
            physical_charge_id=charge_id,
            target_kind=charge.target_kind,
            target_object_id=charge.target_object_id,
            target_epoch=charge.target_epoch,
            target_generation=charge.target_generation,
            storage_key=charge.storage_key,
            byte_size=charge.byte_size,
            disposition=deletion.disposition.value,
            created_at=now,
        )
        self._session.add(receipt)
        charge.state = "released"
        charge.released_at = now
        usage.charged_bytes -= charge.byte_size
        usage.revision += 1
        usage.updated_at = now
        self._session.flush()
        return self._domain_receipt(receipt, target)

    def charged_bytes(self, workspace_id: UUID) -> int:
        value = self._session.scalar(
            select(WorkspaceMediaQuotaUsage.charged_bytes).where(
                WorkspaceMediaQuotaUsage.workspace_id == workspace_id
            )
        )
        return 0 if value is None else value

    def _locked_usage(self, workspace_id: UUID) -> WorkspaceMediaQuotaUsage:
        usage = self._session.scalar(
            select(WorkspaceMediaQuotaUsage)
            .where(WorkspaceMediaQuotaUsage.workspace_id == workspace_id)
            .with_for_update()
        )
        if usage is None:
            raise ValueError("workspace_media_quota_not_configured")
        return usage

    def _target_facts(
        self,
        target: PhysicalChargeTarget,
        storage_key: StorageKey,
        byte_size: int,
    ) -> _ChargePayload:
        if target.kind is PhysicalObjectKind.STAGING_LEASE:
            lease = self._session.get(
                MediaStagingLease,
                {"workspace_id": target.workspace_id, "id": target.object_id},
            )
            if lease is None or lease.generation != target.generation:
                raise ValueError("staging_lease_not_found")
            if lease.storage_key != storage_key.value:
                raise ValueError("staging_storage_key_mismatch")
            if lease.byte_size is not None and lease.byte_size != byte_size:
                raise ValueError("staging_byte_size_mismatch")
            return (
                target.kind.value,
                target.object_id,
                0,
                target.generation,
                target.object_id,
                None,
                None,
                "staging",
                storage_key.value,
                byte_size,
            )
        if target.kind is PhysicalObjectKind.BLOB_INCARNATION:
            assert target.blob is not None
            row = self._session.get(
                BlobIncarnationRow,
                {
                    "workspace_id": target.workspace_id,
                    "blob_id": target.blob.blob_id,
                    "blob_epoch": target.blob.epoch.value,
                },
            )
            if row is None:
                raise ValueError("blob_incarnation_not_found")
            if row.storage_key != storage_key.value or row.byte_size != byte_size:
                raise ValueError("blob_charge_storage_facts_mismatch")
            return (
                target.kind.value,
                target.object_id,
                target.blob.epoch.value,
                target.generation,
                None,
                target.blob.blob_id,
                target.blob.epoch.value,
                "pending",
                storage_key.value,
                byte_size,
            )
        raise UnsupportedCapabilityError(
            f"physical charge target not implemented in I3a: {target.kind.value}"
        )

    @staticmethod
    def _charge_payload(
        charge: MediaPhysicalCharge,
    ) -> _ChargePayload:
        return (
            charge.target_kind,
            charge.target_object_id,
            charge.target_epoch,
            charge.target_generation,
            charge.staging_id,
            charge.blob_id,
            charge.blob_epoch,
            charge.accounting_kind,
            charge.storage_key,
            charge.byte_size,
        )

    @staticmethod
    def _domain_target(charge: MediaPhysicalCharge) -> PhysicalChargeTarget:
        kind = PhysicalObjectKind(charge.target_kind)
        blob = None
        if kind is PhysicalObjectKind.BLOB_INCARNATION:
            if charge.blob_id is None or charge.blob_epoch is None:
                raise RuntimeError("blob_charge_missing_incarnation")
            blob = BlobReference(
                workspace_id=charge.workspace_id,
                blob_id=charge.blob_id,
                epoch=BlobEpoch(charge.blob_epoch),
            )
        return PhysicalChargeTarget(
            workspace_id=charge.workspace_id,
            kind=kind,
            object_id=charge.target_object_id,
            generation=charge.target_generation,
            blob=blob,
        )

    def _receipt_for_charge(
        self,
        workspace_id: UUID,
        charge_id: UUID,
    ) -> MediaDeletionReceipt | None:
        return self._session.scalar(
            select(MediaDeletionReceipt).where(
                MediaDeletionReceipt.workspace_id == workspace_id,
                MediaDeletionReceipt.physical_charge_id == charge_id,
            )
        )

    @staticmethod
    def _domain_receipt(
        receipt: MediaDeletionReceipt,
        target: PhysicalChargeTarget,
    ) -> DeletionReceipt:
        return DeletionReceipt(
            receipt_id=receipt.id,
            target=target,
            storage_key=StorageKey(receipt.storage_key),
            physical_charge_id=receipt.physical_charge_id,
            byte_size=receipt.byte_size,
        )
