from __future__ import annotations

from dataclasses import replace
from typing import get_type_hints
from uuid import UUID

import pytest

import backend.media.ports.storage as storage_ports
from backend.media.domain import (
    BlobEpoch,
    BlobReference,
    BlobState,
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
    StorageKey,
    UnsupportedCapabilityError,
)
from backend.media.ports.coordination import (
    OutboxMessage,
    RetentionDecision,
    RetentionResult,
)
from backend.media.ports.persistence import MediaRepository
from backend.tests.fakes.media import (
    FailClosedRetentionRegistry,
    InMemoryBlobStore,
    InMemoryOutbox,
    InMemoryQuota,
    InMemoryRepository,
    InMemoryUnitOfWork,
    StaticRetentionGuard,
)

WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")
FOREIGN_WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")
BLOB_ID = UUID("00000000-0000-0000-0000-000000000010")
FOREIGN_BLOB_ID = UUID("00000000-0000-0000-0000-000000000011")
CHARGE_ID = UUID("00000000-0000-0000-0000-000000000020")
FOREIGN_CHARGE_ID = UUID("00000000-0000-0000-0000-000000000021")
BLOB = BlobReference(WORKSPACE, BLOB_ID, BlobEpoch(1))
FOREIGN_BLOB = BlobReference(FOREIGN_WORKSPACE, FOREIGN_BLOB_ID, BlobEpoch(1))
KEY = StorageKey("opaque/key-1")
FENCE = GenerationFence(BLOB, 7)
BLOB_TARGET = PhysicalChargeTarget(
    WORKSPACE,
    PhysicalObjectKind.BLOB_INCARNATION,
    BLOB_ID,
    FENCE.generation,
    BLOB,
)


def staging_target(sequence: int) -> PhysicalChargeTarget:
    return PhysicalChargeTarget(
        WORKSPACE,
        PhysicalObjectKind.STAGING_LEASE,
        UUID(int=100 + sequence),
        1,
    )


def aggregate() -> StoredBlob:
    return StoredBlob(
        blob_id=BLOB_ID,
        identity=WorkspaceBlobIdentity(WORKSPACE, Sha256Digest("a" * 64)),
    ).allocate(byte_size=5, storage_key=KEY.value)


def publish(store: InMemoryBlobStore, content: bytes = b"12345") -> None:
    staged = store.stage(
        [content], target=staging_target(1), max_bytes=len(content)
    )
    store.publish_no_overwrite(staged, target=KEY, fence=FENCE)


def test_ports_reuse_the_domain_blob_identity() -> None:
    assert get_type_hints(GenerationFence)["blob"] is BlobReference
    hints = get_type_hints(MediaRepository.get_by_blob_id)
    assert hints["workspace_id"] is UUID
    assert hints["blob_id"] is UUID
    assert hints["return"] == StoredBlob | None
    assert (
        get_type_hints(MediaRepository.find_by_identity)["identity"]
        is WorkspaceBlobIdentity
    )


def test_range_read_is_an_explicit_optional_capability() -> None:
    store = InMemoryBlobStore(supports_ranges=False)
    publish(store, b"abc")

    with pytest.raises(UnsupportedCapabilityError, match="range_read"):
        store.open_range(KEY)


def test_publish_is_no_overwrite_and_replay_rejects_different_payload() -> None:
    store = InMemoryBlobStore()
    first = store.stage([b"same"], target=staging_target(1), max_bytes=4)
    replay = store.stage([b"same"], target=staging_target(2), max_bytes=4)
    different = store.stage([b"other"], target=staging_target(3), max_bytes=5)

    assert (
        store.publish_no_overwrite(first, target=KEY, fence=FENCE).disposition
        is PublishDisposition.PUBLISHED
    )
    assert (
        store.publish_no_overwrite(replay, target=KEY, fence=FENCE).disposition
        is PublishDisposition.IDEMPOTENT_REPLAY
    )
    with pytest.raises(NoOverwriteError):
        store.publish_no_overwrite(different, target=KEY, fence=FENCE)
    with pytest.raises(IdempotencyConflictError, match="publish"):
        store.publish_no_overwrite(
            replay, target=KEY, fence=GenerationFence(BLOB, FENCE.generation + 1)
        )


def test_staging_is_lease_bound_consumed_by_publish_and_replayable() -> None:
    store = InMemoryBlobStore()
    target = staging_target(7)
    staged = store.stage([b"same"], target=target, max_bytes=4)
    assert staged.target == target
    assert target in store.staged

    first = store.publish_no_overwrite(staged, target=KEY, fence=FENCE)
    replay = store.publish_no_overwrite(staged, target=KEY, fence=FENCE)

    assert target not in store.staged
    assert first.disposition is PublishDisposition.PUBLISHED
    assert replay.disposition is PublishDisposition.IDEMPOTENT_REPLAY
    with pytest.raises(IdempotencyConflictError, match="stage_after_publish"):
        store.stage([b"same"], target=target, max_bytes=4)


def test_non_blob_deletion_receipt_is_issued_only_after_exact_object_delete() -> None:
    store = InMemoryBlobStore()
    target = staging_target(8)
    staged = store.stage([b"12345"], target=target, max_bytes=5)

    deletion = store.delete(
        staged.storage_key,
        target=target,
    )
    assert deletion.disposition is PhysicalDeleteDisposition.DELETED
    replay = store.delete(
        staged.storage_key,
        target=target,
    )
    assert replay == replace(
        deletion, disposition=PhysicalDeleteDisposition.ALREADY_MISSING
    )
    assert not hasattr(deletion, "byte_size")
    with pytest.raises(ValueError, match="gc_trash_store"):
        store.delete(KEY, target=BLOB_TARGET)


def test_outbox_replay_rejects_same_key_with_different_payload() -> None:
    outbox = InMemoryOutbox()
    first = OutboxMessage(
        WORKSPACE,
        "install:1",
        "BlobAvailable",
        "1",
        (("generation", "7"),),
    )
    outbox.add(first)
    outbox.add(first)

    with pytest.raises(IdempotencyConflictError, match="outbox"):
        outbox.add(
            OutboxMessage(
                WORKSPACE,
                "install:1",
                "BlobAvailable",
                "1",
                (("generation", "8"),),
            )
        )


def test_security_move_is_generation_fenced_and_replay_safe() -> None:
    store = InMemoryBlobStore()
    publish(store)
    store.set_generation(BLOB, 2)

    with pytest.raises(GenerationFenceError):
        store.quarantine(KEY, fence=GenerationFence(BLOB, 1))
    assert KEY in store.objects

    current = GenerationFence(BLOB, 2)
    store.quarantine(KEY, fence=current)
    store.quarantine(KEY, fence=current)
    assert KEY in store.quarantine_objects


def test_retention_registry_fails_closed_for_missing_and_failed_guards() -> None:
    registry = FailClosedRetentionRegistry(("library", "context"))
    registry.register(StaticRetentionGuard("library", error=TimeoutError()))

    assert registry.evaluate_all(BLOB) == (
        RetentionResult(RetentionDecision.HOLD, "failed:library"),
        RetentionResult(RetentionDecision.HOLD, "missing:context"),
    )


def issue_blob_deletion(
    store: InMemoryBlobStore,
    *,
    blob: BlobReference,
    generation: int,
    stage_sequence: int,
) -> PhysicalDeleteResult:
    staging = PhysicalChargeTarget(
        blob.workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        UUID(int=1000 + stage_sequence),
        1,
    )
    staged = store.stage([b"12345"], target=staging, max_bytes=5)
    fence = GenerationFence(blob, generation)
    store.publish_no_overwrite(staged, target=KEY, fence=fence)
    store.set_generation(blob, generation)
    store.trash(KEY, fence=fence)
    target = PhysicalChargeTarget(
        blob.workspace_id,
        PhysicalObjectKind.BLOB_INCARNATION,
        blob.blob_id,
        generation,
        blob,
    )
    return store.purge(KEY, fence=fence, target=target)


def quota_with_deletion() -> tuple[
    InMemoryQuota, PhysicalDeleteResult, InMemoryBlobStore
]:
    store = InMemoryBlobStore()
    quota = InMemoryQuota()
    quota.reserve(
        WORKSPACE,
        charge_id=CHARGE_ID,
        target=BLOB_TARGET,
        storage_key=KEY,
        bytes_=5,
    )
    deletion = issue_blob_deletion(
        store,
        blob=BLOB,
        generation=FENCE.generation,
        stage_sequence=1,
    )
    return quota, deletion, store


@pytest.mark.parametrize(
    "variant",
    ("same-size-foreign-blob", "stale-generation", "charge-mismatch"),
)
def test_physical_quota_rejects_foreign_or_stale_receipt(variant: str) -> None:
    quota, _, store = quota_with_deletion()
    blob = FOREIGN_BLOB if variant == "same-size-foreign-blob" else BLOB
    generation = (
        FENCE.generation - 1
        if variant == "stale-generation"
        else FENCE.generation
    )
    wrong = issue_blob_deletion(
        store,
        blob=blob,
        generation=generation,
        stage_sequence={
            "same-size-foreign-blob": 2,
            "stale-generation": 3,
            "charge-mismatch": 4,
        }[variant],
    )
    release_charge_id = (
        FOREIGN_CHARGE_ID if variant == "charge-mismatch" else CHARGE_ID
    )
    expected = "not_found" if variant == "charge-mismatch" else "binding_mismatch"
    with pytest.raises(ValueError, match=expected):
        quota.release_after_deletion(
            WORKSPACE, charge_id=release_charge_id, deletion=wrong
        )
    assert quota.charged_bytes(WORKSPACE) == 5


def test_physical_charge_remains_until_exact_deletion_receipt() -> None:
    quota, deletion, store = quota_with_deletion()

    assert not hasattr(quota, "release")
    receipt = quota.release_after_deletion(
        WORKSPACE, charge_id=CHARGE_ID, deletion=deletion
    )
    assert receipt.byte_size == 5
    replay = store.purge(KEY, fence=FENCE, target=BLOB_TARGET)
    assert replay.disposition is PhysicalDeleteDisposition.ALREADY_MISSING
    assert quota.release_after_deletion(
        WORKSPACE, charge_id=CHARGE_ID, deletion=replay
    ) == receipt
    assert quota.deletion_receipts[receipt.receipt_id] == receipt
    assert quota.charged_bytes(WORKSPACE) == 0


def test_storage_replay_does_not_need_deleted_object_size() -> None:
    quota, deletion, store = quota_with_deletion()

    assert deletion.disposition is PhysicalDeleteDisposition.DELETED
    replay = store.purge(KEY, fence=FENCE, target=BLOB_TARGET)
    assert replay.disposition is PhysicalDeleteDisposition.ALREADY_MISSING
    receipt = quota.release_after_deletion(
        WORKSPACE, charge_id=CHARGE_ID, deletion=replay
    )
    assert receipt.byte_size == 5
    assert quota.charged_bytes(WORKSPACE) == 0


def test_unrecorded_field_identical_receipt_cannot_release_physical_charge() -> None:
    quota, deletion, store = quota_with_deletion()
    forged = DeletionReceipt(
        receipt_id=FOREIGN_CHARGE_ID,
        target=deletion.target,
        storage_key=deletion.storage_key,
        physical_charge_id=CHARGE_ID,
        byte_size=5,
    )

    with pytest.raises(ValueError, match="PhysicalDeleteResult"):
        quota.release_after_deletion(
            WORKSPACE,
            charge_id=CHARGE_ID,
            deletion=forged,  # type: ignore[arg-type]
        )
    assert quota.charged_bytes(WORKSPACE) == 5
    assert not hasattr(quota, "record_deletion_receipt")
    assert not hasattr(store, "deletion_receipts")
    assert not hasattr(storage_ports, "DeletionReceiptSource")


def test_physical_reserve_replay_rejects_different_payload() -> None:
    quota, _, _ = quota_with_deletion()

    with pytest.raises(IdempotencyConflictError):
        quota.reserve(
            WORKSPACE,
            charge_id=CHARGE_ID,
            target=PhysicalChargeTarget(
                WORKSPACE,
                PhysicalObjectKind.BLOB_INCARNATION,
                FOREIGN_BLOB_ID,
                FENCE.generation,
                BlobReference(WORKSPACE, FOREIGN_BLOB_ID, BlobEpoch(1)),
            ),
            storage_key=KEY,
            bytes_=5,
        )


def test_physical_target_requires_blob_only_for_blob_incarnation() -> None:
    with pytest.raises(ValueError, match="requires a BlobReference"):
        PhysicalChargeTarget(WORKSPACE, PhysicalObjectKind.BLOB_INCARNATION, BLOB_ID, 1)
    with pytest.raises(ValueError, match="only blob_incarnation"):
        PhysicalChargeTarget(
            WORKSPACE, PhysicalObjectKind.STAGING_LEASE, BLOB_ID, 1, BLOB
        )
    with pytest.raises(ValueError, match="blob id must match"):
        PhysicalChargeTarget(
            WORKSPACE,
            PhysicalObjectKind.BLOB_INCARNATION,
            FOREIGN_BLOB_ID,
            1,
            BLOB,
        )


@pytest.mark.parametrize(
    "kind",
    (PhysicalObjectKind.STAGING_LEASE, PhysicalObjectKind.QUARANTINED_UPLOAD),
)
def test_no_blob_charge_is_retained_until_its_own_receipt(kind) -> None:
    store = InMemoryBlobStore()
    quota = InMemoryQuota()
    target = PhysicalChargeTarget(WORKSPACE, kind, BLOB_ID, 3)
    if kind is PhysicalObjectKind.STAGING_LEASE:
        staged = store.stage([b"12345"], target=target, max_bytes=5)
        storage_key = staged.storage_key
    else:
        storage_key = StorageKey("quarantine/object-3")
        store.put_auxiliary(target=target, storage_key=storage_key, content=b"12345")
    quota.reserve(
        WORKSPACE,
        charge_id=CHARGE_ID,
        target=target,
        storage_key=storage_key,
        bytes_=5,
    )

    assert quota.charged_bytes(WORKSPACE) == 5
    deletion = store.delete(
        storage_key,
        target=target,
    )
    receipt = quota.release_after_deletion(
        WORKSPACE, charge_id=CHARGE_ID, deletion=deletion
    )
    assert receipt.byte_size == 5
    assert quota.charged_bytes(WORKSPACE) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda target: replace(target, kind=PhysicalObjectKind.DERIVED_ARTIFACT),
        lambda target: replace(target, object_id=FOREIGN_BLOB_ID),
        lambda target: replace(target, generation=target.generation + 1),
    ],
    ids=("foreign-kind", "foreign-object", "foreign-generation"),
)
def test_no_blob_receipt_rejects_same_size_foreign_target(mutate) -> None:
    store = InMemoryBlobStore()
    quota = InMemoryQuota()
    target = PhysicalChargeTarget(
        WORKSPACE, PhysicalObjectKind.QUARANTINED_UPLOAD, BLOB_ID, 3
    )
    quota.reserve(
        WORKSPACE,
        charge_id=CHARGE_ID,
        target=target,
        storage_key=KEY,
        bytes_=5,
    )
    foreign_target = mutate(target)
    store.put_auxiliary(
        target=foreign_target,
        storage_key=KEY,
        content=b"12345",
    )
    deletion = store.delete(
        KEY,
        target=foreign_target,
    )
    with pytest.raises(ValueError, match="binding_mismatch"):
        quota.release_after_deletion(
            WORKSPACE,
            charge_id=CHARGE_ID,
            deletion=deletion,
        )
    assert quota.charged_bytes(WORKSPACE) == 5


def test_unit_of_work_rolls_back_uncommitted_and_failed_transactions() -> None:
    repository = InMemoryRepository()
    quota = InMemoryQuota()
    outbox = InMemoryOutbox()

    def new_uow() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(repository, quota, outbox)

    def stage_transaction(uow: InMemoryUnitOfWork) -> None:
        uow.repository.put(aggregate())
        uow.physical_quota.reserve(
            WORKSPACE,
            charge_id=CHARGE_ID,
            target=BLOB_TARGET,
            storage_key=KEY,
            bytes_=5,
        )
        uow.outbox.add(
            OutboxMessage(
                WORKSPACE,
                "install:1",
                "InstallRequested",
                "1",
                (("blob", str(BLOB_ID)),),
            )
        )

    with new_uow() as uow:
        stage_transaction(uow)
    assert repository.get_by_blob_id(WORKSPACE, BLOB_ID) is None
    assert quota.charged_bytes(WORKSPACE) == 0
    assert outbox.messages == {}

    with pytest.raises(RuntimeError, match="boom"):
        with new_uow() as uow:
            stage_transaction(uow)
            uow.commit()
            raise RuntimeError("boom")
    assert repository.get_by_blob_id(WORKSPACE, BLOB_ID) is None
    assert quota.charged_bytes(WORKSPACE) == 0
    assert outbox.messages == {}

    with new_uow() as uow:
        stage_transaction(uow)
        uow.commit()
    assert repository.get_by_blob_id(WORKSPACE, BLOB_ID) == aggregate()
    assert repository.find_by_identity(aggregate().identity) == aggregate()
    assert quota.charged_bytes(WORKSPACE) == 5
    assert tuple(outbox.messages) == (
        (WORKSPACE, "InstallRequested", "install:1"),
    )


def test_unit_of_work_atomically_rolls_back_quota_release_and_receipt() -> None:
    quota, deletion, _ = quota_with_deletion()
    repository = InMemoryRepository()
    outbox = InMemoryOutbox()
    uow = InMemoryUnitOfWork(repository, quota, outbox)

    with uow:
        receipt = uow.physical_quota.release_after_deletion(
            WORKSPACE, charge_id=CHARGE_ID, deletion=deletion
        )
    assert quota.charged_bytes(WORKSPACE) == 5
    assert receipt.receipt_id not in quota.deletion_receipts

    with uow:
        receipt = uow.physical_quota.release_after_deletion(
            WORKSPACE, charge_id=CHARGE_ID, deletion=deletion
        )
        uow.commit()
    assert quota.charged_bytes(WORKSPACE) == 0
    assert quota.deletion_receipts[receipt.receipt_id] == receipt


def test_repository_persists_epoch_seed_across_purge_reload_and_reupload() -> None:
    repository = InMemoryRepository()
    quota = InMemoryQuota()
    outbox = InMemoryOutbox()

    def new_uow() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(repository, quota, outbox)

    with new_uow() as uow:
        uow.repository.put(aggregate())
        uow.commit()

    root = repository.get_by_blob_id(WORKSPACE, BLOB_ID)
    assert root is not None and root.active is not None
    missing = root.update_active(root.active.transition(BlobState.MISSING))
    with new_uow() as uow:
        uow.repository.put(missing)
        uow.commit()

    root = repository.find_by_identity(aggregate().identity)
    assert root is not None and root.active is not None
    purged = root.update_active(root.active.transition(BlobState.PURGED))
    with new_uow() as uow:
        uow.repository.put(purged)
        uow.commit()

    reloaded = repository.find_by_identity(aggregate().identity)
    assert reloaded is not None and reloaded.active is None
    next_incarnation = reloaded.allocate(byte_size=6, storage_key="opaque/key-2")
    with new_uow() as uow:
        uow.repository.put(next_incarnation)
        uow.commit()

    assert next_incarnation.active is not None
    assert next_incarnation.active.epoch == BlobEpoch(2)
