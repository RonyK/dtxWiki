"""Deterministic, infrastructure-free conformance fakes for media ports."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
from io import BytesIO
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.media.domain import (
    BlobReference,
    FormatFacts,
    Sha256Digest,
    StoredBlob,
    WorkspaceBlobIdentity,
)
from backend.media.ports.contracts import (
    DeletionReceipt,
    GenerationFence,
    GenerationFenceError,
    IdempotencyConflictError,
    NoOverwriteError,
    PhysicalChargeTarget,
    PhysicalDeleteDisposition,
    PhysicalDeleteResult,
    PhysicalObjectKind,
    PublishDisposition,
    PublishReceipt,
    StagedBlob,
    StorageKey,
    UnsupportedCapabilityError,
)
from backend.media.ports.coordination import (
    BlobRetentionGuard,
    OutboxMessage,
    RetentionDecision,
    RetentionResult,
)
from backend.media.ports.inspection import ScanResult


class InMemoryReadHandle:
    def __init__(self, content: bytes, *, supports_ranges: bool = True) -> None:
        self._content = content
        self._stream = BytesIO(content)
        self._supports_ranges = supports_ranges

    @property
    def byte_size(self) -> int:
        return len(self._content)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def read_range(self, start: int, end_exclusive: int) -> bytes:
        if not self._supports_ranges:
            raise UnsupportedCapabilityError("range_read")
        return self._content[start:end_exclusive]

    def close(self) -> None:
        self._stream.close()


class InMemoryBlobStore:
    def __init__(self, *, supports_ranges: bool = True) -> None:
        self.supports_ranges = supports_ranges
        self.staged: dict[PhysicalChargeTarget, tuple[StagedBlob, bytes]] = {}
        self.published_staging: dict[PhysicalChargeTarget, PublishReceipt] = {}
        self.auxiliary_objects: dict[
            tuple[PhysicalChargeTarget, StorageKey], bytes
        ] = {}
        self.objects: dict[StorageKey, bytes] = {}
        self.publish_fences: dict[StorageKey, GenerationFence] = {}
        self.quarantine_objects: dict[StorageKey, bytes] = {}
        self.trash_objects: dict[StorageKey, bytes] = {}
        self.generations: dict[BlobReference, int] = {}
    def stage(
        self,
        chunks: Iterable[bytes],
        *,
        target: PhysicalChargeTarget,
        max_bytes: int,
    ) -> StagedBlob:
        if target.kind is not PhysicalObjectKind.STAGING_LEASE:
            raise ValueError("stage_requires_staging_lease")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 0
        ):
            raise ValueError("max_bytes must be a non-negative integer")
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise ValueError("stage_limit_exceeded")
        storage_key = StorageKey(
            f"staging/{target.workspace_id.hex}/{target.object_id.hex}/"
            f"{target.generation}"
        )
        staged = StagedBlob(
            target,
            storage_key,
            len(content),
            Sha256Digest(sha256(content).hexdigest()),
        )
        existing = self.staged.get(target)
        if existing is not None:
            if existing != (staged, content):
                raise IdempotencyConflictError("stage")
            return existing[0]
        if target in self.published_staging:
            raise IdempotencyConflictError("stage_after_publish")
        self.staged[target] = (staged, content)
        return staged

    def publish_no_overwrite(
        self, staged: StagedBlob, *, target: StorageKey, fence: GenerationFence
    ) -> PublishReceipt:
        staged_record = self.staged.get(staged.target)
        if staged_record is None:
            replay = self.published_staging.get(staged.target)
            if (
                replay is None
                or replay.fence != fence
                or replay.storage_key != target
                or replay.byte_size != staged.byte_size
                or replay.sha256 != staged.sha256
            ):
                raise IdempotencyConflictError("publish")
            return replace(replay, disposition=PublishDisposition.IDEMPOTENT_REPLAY)
        recorded_staged, content = staged_record
        if recorded_staged != staged:
            raise IdempotencyConflictError("staged_blob")
        current = self.objects.get(target)
        disposition = PublishDisposition.PUBLISHED
        if current is not None:
            if current != content:
                raise NoOverwriteError(str(target))
            if self.publish_fences[target] != fence:
                raise IdempotencyConflictError("publish")
            disposition = PublishDisposition.IDEMPOTENT_REPLAY
        else:
            self.objects[target] = content
            self.publish_fences[target] = fence
        receipt = PublishReceipt(
            disposition,
            fence,
            target,
            len(content),
            Sha256Digest(sha256(content).hexdigest()),
        )
        self.staged.pop(staged.target)
        self.published_staging[staged.target] = receipt
        return receipt

    def put_auxiliary(
        self,
        *,
        target: PhysicalChargeTarget,
        storage_key: StorageKey,
        content: bytes,
    ) -> None:
        if target.kind in {
            PhysicalObjectKind.STAGING_LEASE,
            PhysicalObjectKind.BLOB_INCARNATION,
        }:
            raise ValueError("auxiliary object requires a non-blob auxiliary kind")
        key = (target, storage_key)
        current = self.auxiliary_objects.setdefault(key, content)
        if current != content:
            raise IdempotencyConflictError("auxiliary_object")

    def delete(
        self,
        key: StorageKey,
        *,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult:
        if not isinstance(key, StorageKey):
            raise ValueError("key must be a StorageKey")
        if not isinstance(target, PhysicalChargeTarget):
            raise ValueError("target must be a PhysicalChargeTarget")
        if target.kind is PhysicalObjectKind.BLOB_INCARNATION:
            raise ValueError("blob_incarnation_requires_gc_trash_store")
        deleted = False
        if target.kind is PhysicalObjectKind.STAGING_LEASE:
            record = self.staged.get(target)
            if record is None:
                return PhysicalDeleteResult(
                    PhysicalDeleteDisposition.ALREADY_MISSING, target, key
                )
            staged, _content = record
            if staged.storage_key != key:
                raise ValueError("physical_object_key_mismatch")
            self.staged.pop(target)
            deleted = True
        else:
            deleted = self.auxiliary_objects.pop((target, key), None) is not None
        return PhysicalDeleteResult(
            PhysicalDeleteDisposition.DELETED
            if deleted
            else PhysicalDeleteDisposition.ALREADY_MISSING,
            target,
            key,
        )

    def open(self, key: StorageKey) -> InMemoryReadHandle:
        return InMemoryReadHandle(
            self.objects[key], supports_ranges=self.supports_ranges
        )

    def open_range(self, key: StorageKey) -> InMemoryReadHandle:
        if not self.supports_ranges:
            raise UnsupportedCapabilityError("range_read")
        return self.open(key)

    def set_generation(self, blob: BlobReference, generation: int) -> None:
        self.generations[blob] = generation

    def _check(self, fence: GenerationFence) -> None:
        if self.generations.get(fence.blob) != fence.generation:
            raise GenerationFenceError(f"stale_generation:{fence.generation}")

    def quarantine(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self._check(fence)
        if key in self.quarantine_objects:
            return
        self.quarantine_objects[key] = self.objects.pop(key)

    def restore_from_quarantine(
        self, key: StorageKey, *, fence: GenerationFence
    ) -> None:
        self._check(fence)
        if key in self.objects:
            return
        self.objects[key] = self.quarantine_objects.pop(key)

    def trash(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self._check(fence)
        if key in self.trash_objects:
            return
        self.trash_objects[key] = self.objects.pop(key)

    def restore_from_trash(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self._check(fence)
        if key in self.objects:
            return
        self.objects[key] = self.trash_objects.pop(key)

    def purge(
        self,
        key: StorageKey,
        *,
        fence: GenerationFence,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult:
        self._check(fence)
        if (
            target.kind is not PhysicalObjectKind.BLOB_INCARNATION
            or target.workspace_id != fence.blob.workspace_id
            or target.object_id != fence.blob.blob_id
            or target.blob != fence.blob
        ):
            raise ValueError("physical_gc_target_mismatch")
        deleted = self.trash_objects.pop(key, None) is not None
        return PhysicalDeleteResult(
            PhysicalDeleteDisposition.DELETED
            if deleted
            else PhysicalDeleteDisposition.ALREADY_MISSING,
            target,
            key,
        )


class InMemorySecurityQuarantineStore:
    def __init__(self, storage: InMemoryBlobStore) -> None:
        self.storage = storage

    def quarantine(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self.storage.quarantine(key, fence=fence)

    def restore(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self.storage.restore_from_quarantine(key, fence=fence)


class InMemoryGcTrashStore:
    def __init__(self, storage: InMemoryBlobStore) -> None:
        self.storage = storage

    def trash(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self.storage.trash(key, fence=fence)

    def restore(self, key: StorageKey, *, fence: GenerationFence) -> None:
        self.storage.restore_from_trash(key, fence=fence)

    def purge(
        self,
        key: StorageKey,
        *,
        fence: GenerationFence,
        target: PhysicalChargeTarget,
    ) -> PhysicalDeleteResult:
        return self.storage.purge(key, fence=fence, target=target)


class InMemoryQuota:
    def __init__(self) -> None:
        self.charges: dict[
            tuple[UUID, UUID], tuple[PhysicalChargeTarget, StorageKey, int]
        ] = {}
        self.released_charges: dict[tuple[UUID, UUID], UUID] = {}
        self.deletion_receipts: dict[UUID, DeletionReceipt] = {}

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
        if target.workspace_id != workspace_id:
            raise ValueError("physical target workspace mismatch")
        if not isinstance(storage_key, StorageKey):
            raise ValueError("storage_key must be a StorageKey")
        if isinstance(bytes_, bool) or not isinstance(bytes_, int) or bytes_ < 0:
            raise ValueError("bytes_ must be a non-negative integer")
        payload = (target, storage_key, bytes_)
        if (workspace_id, charge_id) in self.released_charges:
            raise IdempotencyConflictError("released_physical_quota_reservation")
        current = self.charges.setdefault((workspace_id, charge_id), payload)
        if current != payload:
            raise IdempotencyConflictError("physical_quota_reservation")

    def release_after_deletion(
        self,
        workspace_id: UUID,
        *,
        charge_id: UUID,
        deletion: PhysicalDeleteResult,
    ) -> DeletionReceipt:
        if not isinstance(deletion, PhysicalDeleteResult):
            raise ValueError("deletion must be a PhysicalDeleteResult")
        charge_key = (workspace_id, charge_id)
        released_by = self.released_charges.get(charge_key)
        if released_by is not None:
            receipt = self.deletion_receipts[released_by]
            if (deletion.target, deletion.storage_key) != (
                receipt.target,
                receipt.storage_key,
            ):
                raise IdempotencyConflictError("physical_quota_release")
            return receipt
        charge = self.charges.get(charge_key)
        if charge is None:
            raise ValueError("physical_charge_not_found")
        target, storage_key, amount = charge
        if (deletion.target, deletion.storage_key) != (target, storage_key):
            raise ValueError("physical_deletion_binding_mismatch")
        receipt_id = uuid5(
            NAMESPACE_URL,
            f"physical-delete:{workspace_id}:{target.kind}:{target.object_id}:"
            f"{target.generation}:{charge_id}:{storage_key.value}:{amount}",
        )
        receipt = DeletionReceipt(
            receipt_id=receipt_id,
            target=target,
            storage_key=storage_key,
            physical_charge_id=charge_id,
            byte_size=amount,
        )
        current = self.deletion_receipts.setdefault(receipt_id, receipt)
        if current != receipt:
            raise IdempotencyConflictError("physical_deletion_receipt")
        self.charges.pop(charge_key)
        self.released_charges[charge_key] = receipt_id
        return receipt

    def charged_bytes(self, workspace_id: UUID) -> int:
        return sum(
            payload[2]
            for (owner, _), payload in self.charges.items()
            if owner == workspace_id
        )

class InMemoryRepository:
    def __init__(self) -> None:
        self.values: dict[tuple[UUID, UUID], StoredBlob] = {}

    def get_by_blob_id(self, workspace_id: UUID, blob_id: UUID) -> StoredBlob | None:
        return self.values.get((workspace_id, blob_id))

    def find_by_identity(
        self, identity: WorkspaceBlobIdentity
    ) -> StoredBlob | None:
        matches = [blob for blob in self.values.values() if blob.identity == identity]
        if len(matches) > 1:
            raise RuntimeError("duplicate_workspace_blob_identity")
        return None if not matches else matches[0]

    def put(self, blob: StoredBlob) -> None:
        if not isinstance(blob, StoredBlob):
            raise ValueError("blob must be a StoredBlob")
        key = (blob.identity.workspace_id, blob.blob_id)
        for existing_key, existing in self.values.items():
            if existing.identity == blob.identity and existing_key != key:
                raise IdempotencyConflictError("workspace_blob_identity")
        current = self.values.get(key)
        if current is not None and current.identity != blob.identity:
            raise IdempotencyConflictError("blob_identity")
        self.values[key] = blob


class InMemoryUnitOfWork:
    def __init__(
        self,
        repository: InMemoryRepository,
        physical_quota: InMemoryQuota,
        outbox: InMemoryOutbox,
    ) -> None:
        self.repository = repository
        self.physical_quota = physical_quota
        self.outbox = outbox
        self._snapshot: dict[tuple[UUID, UUID], StoredBlob] | None = None
        self._quota_snapshot: dict[
            tuple[UUID, UUID], tuple[PhysicalChargeTarget, StorageKey, int]
        ] | None = None
        self._released_quota_snapshot: dict[tuple[UUID, UUID], UUID] | None = None
        self._deletion_receipt_snapshot: dict[UUID, DeletionReceipt] | None = None
        self._outbox_snapshot: dict[
            tuple[UUID, str, str], OutboxMessage
        ] | None = None
        self._committed = False

    def __enter__(self) -> InMemoryUnitOfWork:
        self._snapshot = deepcopy(self.repository.values)
        self._quota_snapshot = deepcopy(self.physical_quota.charges)
        self._released_quota_snapshot = deepcopy(
            self.physical_quota.released_charges
        )
        self._deletion_receipt_snapshot = deepcopy(
            self.physical_quota.deletion_receipts
        )
        self._outbox_snapshot = deepcopy(self.outbox.messages)
        self._committed = False
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if exc_type is not None or not self._committed:
            self.rollback()
        return False

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        if self._snapshot is not None:
            self.repository.values = self._snapshot
        if self._quota_snapshot is not None:
            self.physical_quota.charges = self._quota_snapshot
        if self._released_quota_snapshot is not None:
            self.physical_quota.released_charges = self._released_quota_snapshot
        if self._deletion_receipt_snapshot is not None:
            self.physical_quota.deletion_receipts = self._deletion_receipt_snapshot
        if self._outbox_snapshot is not None:
            self.outbox.messages = self._outbox_snapshot


@dataclass(slots=True)
class StaticRetentionGuard:
    name: str
    result: RetentionResult | None = None
    error: Exception | None = None

    def evaluate(self, blob: BlobReference) -> RetentionResult:
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("guard_result_missing")
        return self.result


class FailClosedRetentionRegistry:
    def __init__(self, required_names: tuple[str, ...]) -> None:
        self.required_names = required_names
        self.guards: dict[str, BlobRetentionGuard] = {}

    def register(self, guard: BlobRetentionGuard) -> None:
        self.guards[guard.name] = guard

    def evaluate_all(self, blob: BlobReference) -> tuple[RetentionResult, ...]:
        results: list[RetentionResult] = []
        for name in self.required_names:
            guard = self.guards.get(name)
            if guard is None:
                results.append(
                    RetentionResult(RetentionDecision.HOLD, f"missing:{name}")
                )
                continue
            try:
                results.append(guard.evaluate(blob))
            except Exception:
                results.append(
                    RetentionResult(RetentionDecision.HOLD, f"failed:{name}")
                )
        return tuple(results)


class DeterministicScanner:
    def __init__(self, result: ScanResult | Exception) -> None:
        self.result = result

    def scan(self, content: InMemoryReadHandle, *, byte_budget: int) -> ScanResult:
        if content.byte_size > byte_budget:
            raise ValueError("scan_budget_exceeded")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class DeterministicFormatValidator:
    def __init__(self, result: FormatFacts | Exception) -> None:
        self.result = result

    def validate(self, content: InMemoryReadHandle, *, byte_budget: int) -> FormatFacts:
        if content.byte_size > byte_budget:
            raise ValueError("validation_budget_exceeded")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class InMemoryOutbox:
    def __init__(self) -> None:
        self.messages: dict[tuple[UUID, str, str], OutboxMessage] = {}

    def add(self, message: OutboxMessage) -> None:
        key = (message.workspace_id, message.topic, message.idempotency_key)
        current = self.messages.setdefault(key, message)
        if current != message:
            raise IdempotencyConflictError("outbox")


class RecordingUniversalLockCoordinator:
    """Rejects callers that deviate from the one universal acquisition order."""

    _ORDER = ("workspace", "quota", "domain", "digest", "association", "outbox")

    def __init__(self) -> None:
        self.acquired: list[tuple[str, object]] = []

    def _acquire(self, kind: str, value: object) -> None:
        position = self._ORDER.index(kind)
        if self.acquired and position < self._ORDER.index(self.acquired[-1][0]):
            raise RuntimeError("universal_lock_order_violation")
        self.acquired.append((kind, value))

    def acquire_workspace_intent(self, workspace_id: UUID) -> None:
        self._acquire("workspace", workspace_id)

    def acquire_quota_or_lease(self, key: str) -> None:
        self._acquire("quota", key)

    def acquire_domain_aggregate(self, *, rank: int, aggregate_id: str) -> None:
        self._acquire("domain", (rank, aggregate_id))

    def acquire_digest_and_incarnation(self, blob: BlobReference) -> None:
        self._acquire("digest", blob)

    def acquire_association(self, association_id: str) -> None:
        self._acquire("association", association_id)

    def acquire_outbox(self) -> None:
        self._acquire("outbox", None)
