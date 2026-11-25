from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from backend.media.domain import (
    BlobEpoch,
    BlobIncarnation,
    BlobReference,
    BlobState,
    FailureDisposition,
    FormatCategory,
    FormatDescriptor,
    FormatFacts,
    MediaFailure,
    MediaFailureCode,
    MediaFailurePhase,
    PhysicalQuotaFacts,
    PhysicalQuotaKind,
    ScannerVerdict,
    SecurityBlockedBlob,
    SecurityState,
    Sha256Digest,
    StoredBlob,
    StoredBlobInspection,
    WorkspaceBlobIdentity,
)


def identity():
    return WorkspaceBlobIdentity(uuid4(), Sha256Digest("A" * 64))


@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_epoch_requires_a_positive_integer(value):
    with pytest.raises(ValueError):
        BlobEpoch(value)


def test_digest_is_canonical_and_workspace_scoped():
    digest = Sha256Digest("A" * 64)
    left = WorkspaceBlobIdentity(uuid4(), digest)
    right = WorkspaceBlobIdentity(uuid4(), digest)
    assert digest.value == "a" * 64
    assert left != right
    with pytest.raises(ValueError):
        Sha256Digest("not-a-digest")


def test_blob_reference_is_uuid_backed_frozen_and_epoch_scoped():
    reference = BlobReference(uuid4(), uuid4(), BlobEpoch(3))
    assert reference.epoch == BlobEpoch(3)
    with pytest.raises(FrozenInstanceError):
        reference.blob_id = uuid4()
    for args in (
        ("workspace", uuid4(), BlobEpoch(1)),
        (uuid4(), "blob", BlobEpoch(1)),
        (uuid4(), uuid4(), 1),
    ):
        with pytest.raises(ValueError):
            BlobReference(*args)


def test_workspace_blob_identity_validates_runtime_types():
    with pytest.raises(ValueError):
        WorkspaceBlobIdentity("workspace", Sha256Digest("a" * 64))
    with pytest.raises(ValueError):
        WorkspaceBlobIdentity(uuid4(), "a" * 64)


def test_blob_incarnation_is_frozen_and_validates_opaque_key_and_counts():
    incarnation = BlobIncarnation(
        uuid4(), uuid4(), BlobEpoch(1), 0, "workspaces/w/b/1/object"
    )
    with pytest.raises(FrozenInstanceError):
        incarnation.byte_size = 1
    defaults = {"byte_size": 0, "storage_key": "relative/key"}
    for kwargs in (
        {"byte_size": -1},
        {"storage_key": "/absolute"},
        {"storage_key": "C:/absolute"},
        {"storage_key": "folder\\object"},
        {"storage_key": "a/../b"},
        {"storage_key": "a//b"},
        {"gc_generation": -1},
    ):
        with pytest.raises(ValueError):
            BlobIncarnation(uuid4(), uuid4(), BlobEpoch(1), **(defaults | kwargs))
    with pytest.raises(ValueError):
        BlobIncarnation("workspace", uuid4(), BlobEpoch(1), 0, "relative/key")
    with pytest.raises(ValueError):
        BlobIncarnation(uuid4(), "blob", BlobEpoch(1), 0, "relative/key")


def test_incarnation_valid_state_flow_and_generations():
    value = BlobIncarnation(uuid4(), uuid4(), BlobEpoch(1), 4, "relative/key")
    available = value.transition(BlobState.AVAILABLE, generation=1)
    pending_gc = available.transition(BlobState.GC_PENDING, generation=1)
    trashed = pending_gc.transition(BlobState.TRASHED, generation=1)
    purged = trashed.transition(BlobState.PURGED, generation=1)
    assert (available.publish_generation, pending_gc.gc_generation, purged.state) == (
        1,
        1,
        BlobState.PURGED,
    )
    with pytest.raises(ValueError):
        value.transition(BlobState.TRASHED)
    with pytest.raises(ValueError):
        pending_gc.transition(BlobState.TRASHED, generation=2)


def test_gc_may_restore_without_reusing_generation():
    value = BlobIncarnation(
        uuid4(), uuid4(), BlobEpoch(1), 4, "relative/key", state=BlobState.AVAILABLE
    )
    first = value.transition(BlobState.GC_PENDING, generation=1).transition(
        BlobState.AVAILABLE, generation=1
    )
    second = first.transition(BlobState.GC_PENDING, generation=2)
    assert second.gc_generation == 2


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (
            BlobIncarnation(
                uuid4(), uuid4(), BlobEpoch(1), 4, "pending/key"
            ),
            BlobState.AVAILABLE,
        ),
        (
            BlobIncarnation(
                uuid4(),
                uuid4(),
                BlobEpoch(1),
                4,
                "available/key",
                state=BlobState.AVAILABLE,
            ),
            BlobState.GC_PENDING,
        ),
        (
            BlobIncarnation(
                uuid4(),
                uuid4(),
                BlobEpoch(1),
                4,
                "gc/key",
                state=BlobState.GC_PENDING,
                gc_generation=3,
            ),
            BlobState.TRASHED,
        ),
    ],
)
def test_side_effecting_blob_transitions_require_an_explicit_generation(
    source, target
):
    with pytest.raises(ValueError, match="generation"):
        source.transition(target)


def test_stored_blob_allocates_monotonic_non_reused_epochs():
    blob = StoredBlob(uuid4(), identity()).allocate(byte_size=4, storage_key="one")
    assert blob.active_epoch == BlobEpoch(1)
    with pytest.raises(ValueError):
        blob.allocate(byte_size=4, storage_key="duplicate")
    missing = blob.active.transition(BlobState.MISSING)
    blob = blob.update_active(missing)
    purged = blob.active.transition(BlobState.PURGED)
    blob = blob.update_active(purged).allocate(byte_size=4, storage_key="two")
    assert blob.active_epoch == BlobEpoch(2)
    assert blob.next_epoch == BlobEpoch(3)


def test_stored_blob_rejects_foreign_or_inconsistent_active_incarnation():
    ident = identity()
    blob_id = uuid4()
    active = BlobIncarnation(uuid4(), blob_id, BlobEpoch(1), 1, "key")
    with pytest.raises(ValueError):
        StoredBlob(blob_id, ident, BlobEpoch(2), active)
    with pytest.raises(ValueError):
        StoredBlob(
            blob_id,
            ident,
            BlobEpoch(1),
            BlobIncarnation(ident.workspace_id, blob_id, BlobEpoch(1), 1, "key"),
        )


def test_stored_blob_rejects_forged_active_storage_or_lifecycle_jump():
    blob = StoredBlob(uuid4(), identity()).allocate(byte_size=4, storage_key="one")
    with pytest.raises(ValueError, match="immutable storage"):
        blob.update_active(replace(blob.active, storage_key="other"))
    with pytest.raises(ValueError, match="invalid blob transition"):
        blob.update_active(replace(blob.active, state=BlobState.PURGED))
    with pytest.raises(ValueError, match="publish generation"):
        blob.update_active(
            replace(
                blob.active,
                state=BlobState.AVAILABLE,
                publish_generation=2,
            )
        )


def test_inspection_provenance_is_complete_and_metadata_is_immutable():
    metadata = {"width": 10}
    inspection = StoredBlobInspection(
        uuid4(),
        uuid4(),
        uuid4(),
        BlobEpoch(1),
        "r1",
        "magic",
        "1",
        "png",
        "image/png",
        "schema1",
        metadata,
        ScannerVerdict.CLEAN,
        "clamav",
        "2",
    )
    metadata["width"] = 20
    assert inspection.metadata["width"] == 10
    with pytest.raises(TypeError):
        inspection.metadata["width"] = 30
    with pytest.raises(ValueError):
        StoredBlobInspection(
            uuid4(),
            uuid4(),
            uuid4(),
            BlobEpoch(1),
            "r1",
            "magic",
            "1",
            "png",
            "image/png",
            "schema1",
            {},
            ScannerVerdict.NOT_CONFIGURED,
            "scanner",
            "1",
        )


@pytest.mark.parametrize(
    ("verdict", "provider", "version", "available", "valid"),
    [
        (ScannerVerdict.NOT_CONFIGURED, None, None, True, True),
        (ScannerVerdict.NOT_CONFIGURED, "scanner", "1", True, False),
        (ScannerVerdict.CLEAN, "scanner", "1", True, True),
        (ScannerVerdict.CLEAN, None, None, True, False),
        (ScannerVerdict.SUSPICIOUS, "scanner", "1", False, True),
        (ScannerVerdict.SUSPICIOUS, "scanner", "1", True, False),
        (ScannerVerdict.MALICIOUS, "scanner", "1", False, True),
        (ScannerVerdict.MALICIOUS, "scanner", "1", True, False),
        (ScannerVerdict.UNAVAILABLE, "scanner", "1", False, True),
        (ScannerVerdict.UNAVAILABLE, "scanner", "1", True, False),
    ],
)
def test_scanner_verdict_provenance_and_availability_combinations(
    verdict, provider, version, available, valid
):
    kwargs = {
        "inspection_id": uuid4(),
        "workspace_id": uuid4(),
        "blob_id": uuid4(),
        "epoch": BlobEpoch(1),
        "registry_version": "r1",
        "detector_id": "magic",
        "detector_version": "1",
        "format_id": "png",
        "canonical_media_type": "image/png",
        "validation_schema_version": "schema1",
        "metadata": {},
        "scanner_verdict": verdict,
        "scanner_provider": provider,
        "scanner_version": version,
        "ordinary_available": available,
    }
    if valid:
        assert StoredBlobInspection(**kwargs).scanner_verdict is verdict
    else:
        with pytest.raises(ValueError):
            StoredBlobInspection(**kwargs)


def test_inspection_and_security_overlay_validate_uuid_fields():
    with pytest.raises(ValueError):
        StoredBlobInspection(
            "inspection",
            uuid4(),
            uuid4(),
            BlobEpoch(1),
            "r1",
            "magic",
            "1",
            "png",
            "image/png",
            "schema1",
            {},
            ScannerVerdict.NOT_CONFIGURED,
        )
    with pytest.raises(ValueError):
        SecurityBlockedBlob(
            "workspace",
            uuid4(),
            BlobEpoch(1),
            1,
            SecurityState.BLOCKED_PENDING_MOVE,
            "malware",
        )
    with pytest.raises(ValueError, match="scanner_verdict"):
        StoredBlobInspection(
            uuid4(),
            uuid4(),
            uuid4(),
            BlobEpoch(1),
            "r1",
            "magic",
            "1",
            "png",
            "image/png",
            "schema1",
            {},
            "clean",
        )
    with pytest.raises(ValueError, match="metadata"):
        StoredBlobInspection(
            uuid4(),
            uuid4(),
            uuid4(),
            BlobEpoch(1),
            "r1",
            "magic",
            "1",
            "png",
            "image/png",
            "schema1",
            {"nested": {}},
            ScannerVerdict.NOT_CONFIGURED,
        )
    with pytest.raises(ValueError, match="SecurityState"):
        SecurityBlockedBlob(
            uuid4(), uuid4(), BlobEpoch(1), 1, "quarantined", "malware"
        )


def test_security_blocker_is_generation_fenced_and_must_complete_ordered_flow():
    blocker = SecurityBlockedBlob(
        uuid4(),
        uuid4(),
        BlobEpoch(1),
        2,
        SecurityState.BLOCKED_PENDING_MOVE,
        "malware_found",
    )
    quarantined = blocker.transition(SecurityState.QUARANTINED, generation=2)
    restored = quarantined.transition(
        SecurityState.RESTORE_PENDING, generation=2
    ).transition(SecurityState.RESTORED_BLOCKED, generation=2)
    assert restored.state is SecurityState.RESTORED_BLOCKED
    with pytest.raises(ValueError):
        blocker.transition(SecurityState.QUARANTINED, generation=1)
    with pytest.raises(ValueError):
        blocker.transition(SecurityState.RESTORE_PENDING, generation=2)


def test_format_descriptor_is_policy_neutral_normalized_and_frozen():
    descriptor = FormatDescriptor(
        "PNG",
        FormatCategory.IMAGE,
        (".PNG",),
        ("image/PNG",),
        "magic",
        "png",
        "image-core",
    )
    assert descriptor.extensions == ("png",)
    assert descriptor.media_types == ("image/png",)
    assert not hasattr(descriptor, "uploadable")
    with pytest.raises(FrozenInstanceError):
        descriptor.format_id = "jpeg"
    with pytest.raises(ValueError):
        FormatDescriptor(
            "png", FormatCategory.IMAGE, ("png", "png"), ("image/png",), "d", "v", "l"
        )
    with pytest.raises(ValueError, match="FormatCategory"):
        FormatDescriptor(
            "png", "image", ("png",), ("image/png",), "d", "v", "l"
        )


def test_format_facts_record_exact_detector_and_validator_revisions():
    facts = FormatFacts("PNG", "image/PNG", "magic", "1", "png", "schema-1")
    assert facts.canonical_media_type == "image/png"


@pytest.mark.parametrize("kind", list(PhysicalQuotaKind))
def test_all_physical_kinds_remain_charged_until_deletion_receipt(kind):
    charged = PhysicalQuotaFacts(uuid4(), kind, 10)
    deleted = PhysicalQuotaFacts(uuid4(), kind, 10, deletion_confirmed=True)
    assert charged.charge_bytes == 10
    assert deleted.charge_bytes == 0


def test_quota_facts_validate_uuid_fields():
    with pytest.raises(ValueError):
        PhysicalQuotaFacts("workspace", PhysicalQuotaKind.AVAILABLE, 1)


def test_quota_bytes_reject_negative_and_boolean_values():
    with pytest.raises(ValueError):
        PhysicalQuotaFacts(uuid4(), PhysicalQuotaKind.AVAILABLE, -1)
    with pytest.raises(ValueError, match="PhysicalQuotaKind"):
        PhysicalQuotaFacts(uuid4(), "available", 1)
    with pytest.raises(ValueError, match="boolean"):
        PhysicalQuotaFacts(
            uuid4(), PhysicalQuotaKind.AVAILABLE, 1, deletion_confirmed=1
        )


def test_media_failure_exposes_only_closed_safe_taxonomy_and_bounded_context():
    audit_id = uuid4()
    source_details = {"limit_bytes": 10}
    failure = MediaFailure(
        MediaFailureCode.TOO_LARGE,
        MediaFailurePhase.ADMISSION,
        "category_limit_exceeded",
        FailureDisposition.REJECT,
        False,
        source_details,
        audit_id,
    )
    source_details["limit_bytes"] = 20
    assert failure.code.value == "media_too_large"
    assert failure.safe_details["limit_bytes"] == 10
    assert failure.internal_causal_audit_id == audit_id
    with pytest.raises(TypeError):
        failure.safe_details["path"] = "secret"
    with pytest.raises(ValueError):
        MediaFailure(
            MediaFailureCode.SCAN_REJECTED,
            MediaFailurePhase.SECURITY,
            "scanner_rejected",
            FailureDisposition.SECURITY_BLOCK,
            False,
            {"detail": "x" * 161},
            uuid4(),
        )
    assert {code.value for code in MediaFailureCode} == {
        "media_type_unsupported",
        "media_content_mismatch",
        "media_container_invalid",
        "media_too_large",
        "media_quota_exceeded",
        "media_scan_unavailable",
        "media_scan_rejected",
        "media_in_use",
        "media_used_in_history",
        "media_state_changed",
    }


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "path",
        "local_path",
        "storage_key",
        "content_digest",
        "sha256_hash",
        "scanner_name",
        "provider_name",
        "foreign_id",
        "audit_id",
    ],
)
def test_media_failure_rejects_sensitive_safe_detail_keys(unsafe_key):
    with pytest.raises(ValueError):
        MediaFailure(
            MediaFailureCode.SCAN_REJECTED,
            MediaFailurePhase.SECURITY,
            "unsafe_content",
            FailureDisposition.SECURITY_BLOCK,
            False,
            {unsafe_key: "secret"},
            uuid4(),
        )


def test_media_failure_validates_closed_types_reason_and_internal_audit_uuid():
    base = {
        "code": MediaFailureCode.STATE_CHANGED,
        "phase": MediaFailurePhase.INSTALL,
        "reason_code": "generation_changed",
        "disposition": FailureDisposition.RETRY,
        "retryable": True,
        "safe_details": {},
        "internal_causal_audit_id": uuid4(),
    }
    for override in (
        {"phase": "install"},
        {"reason_code": "contains spaces"},
        {"disposition": "retry"},
        {"retryable": 1},
        {"internal_causal_audit_id": "audit"},
        {"safe_details": []},
    ):
        with pytest.raises(ValueError):
            MediaFailure(**(base | override))


@pytest.mark.parametrize(
    ("disposition", "retryable"),
    [
        (FailureDisposition.RETRY, False),
        (FailureDisposition.REJECT, True),
        (FailureDisposition.SECURITY_BLOCK, True),
    ],
)
def test_media_failure_rejects_retryability_contradictions(
    disposition, retryable
):
    with pytest.raises(ValueError, match="retry"):
        MediaFailure(
            MediaFailureCode.STATE_CHANGED,
            MediaFailurePhase.INSTALL,
            "generation_changed",
            disposition,
            retryable,
            {},
            uuid4(),
        )
