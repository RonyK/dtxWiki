from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from backend.media.application import (
    AdmissionCommand,
    AdmissionDisposition,
    AdmissionResult,
    AdmittedBlob,
    BlobAvailable,
    FencedDisposition,
    FormatRegistry,
    FormatRegistryContractError,
    GcCommand,
    GcOperation,
    GcResult,
    InstallCommand,
    InstallDisposition,
    InstallResult,
    SecurityCommand,
    SecurityOperation,
    SecurityResult,
)
from backend.media.domain import (
    BlobEpoch,
    BlobReference,
    FailureDisposition,
    FormatCategory,
    FormatDescriptor,
    FormatFacts,
    MediaFailure,
    MediaFailureCode,
    MediaFailurePhase,
    ScannerVerdict,
    Sha256Digest,
    StoredBlobInspection,
)
from backend.media.ports.contracts import (
    PhysicalChargeTarget,
    PhysicalObjectKind,
    StagedBlob,
    StorageKey,
    UnsupportedCapabilityError,
)


class BytesHandle:
    def __init__(self, content: bytes) -> None:
        self.content = content

    @property
    def byte_size(self) -> int:
        return len(self.content)

    def read(self, size: int = -1) -> bytes:
        return self.content[:size] if size >= 0 else self.content

    def close(self) -> None:
        return None


class StaticValidator:
    def __init__(self, result: object) -> None:
        self.result = result
        self.budgets: list[int] = []

    def validate(self, content: BytesHandle, *, byte_budget: int):
        self.budgets.append(byte_budget)
        return self.result


def blob_reference() -> BlobReference:
    return BlobReference(uuid4(), uuid4(), BlobEpoch(2))


def staged_blob(workspace_id: UUID) -> StagedBlob:
    target = PhysicalChargeTarget(
        workspace_id,
        PhysicalObjectKind.STAGING_LEASE,
        uuid4(),
        1,
    )
    return StagedBlob(
        target,
        StorageKey(f"staging/{workspace_id.hex}/{target.object_id.hex}/1"),
        10,
        Sha256Digest("a" * 64),
    )


def inspection(
    blob: BlobReference, *, inspection_id: UUID | None = None
) -> StoredBlobInspection:
    return StoredBlobInspection(
        inspection_id=inspection_id or uuid4(),
        workspace_id=blob.workspace_id,
        blob_id=blob.blob_id,
        epoch=blob.epoch,
        registry_version="registry_1",
        detector_id="magic",
        detector_version="1",
        format_id="png",
        canonical_media_type="image/png",
        validation_schema_version="schema_1",
        metadata={},
        scanner_verdict=ScannerVerdict.CLEAN,
        scanner_provider="scanner",
        scanner_version="1",
    )


def admitted_blob(
    blob: BlobReference,
    *,
    inspection_id: UUID | None = None,
    byte_size: int = 10,
    digest: str = "a" * 64,
) -> AdmittedBlob:
    return AdmittedBlob(
        blob=blob,
        inspection_id=inspection_id or uuid4(),
        sha256=Sha256Digest(digest),
        byte_size=byte_size,
    )


def failure(
    phase: MediaFailurePhase,
    disposition: FailureDisposition = FailureDisposition.HOLD,
) -> MediaFailure:
    return MediaFailure(
        code=MediaFailureCode.STATE_CHANGED,
        phase=phase,
        reason_code="state_changed",
        disposition=disposition,
        retryable=disposition is FailureDisposition.RETRY,
        safe_details={"state": "changed"},
        internal_causal_audit_id=uuid4(),
    )


def test_admission_command_is_framework_free_immutable_and_validated() -> None:
    workspace_id = uuid4()
    command = AdmissionCommand(
        workspace_id=workspace_id,
        actor_id=uuid4(),
        idempotency_key="upload:1",
        staged=staged_blob(workspace_id),
        physical_charge_id=uuid4(),
        policy_revision="context_policy:1",
        declared_filename="image.png",
        declared_media_type=" IMAGE/PNG ",
    )

    assert command.declared_media_type == "image/png"
    with pytest.raises(FrozenInstanceError):
        command.policy_revision = "context_policy:2"
    with pytest.raises(ValueError):
        workspace_id = uuid4()
        AdmissionCommand(
            workspace_id=workspace_id,
            actor_id=uuid4(),
            idempotency_key="contains a space",
            staged=staged_blob(workspace_id),
            physical_charge_id=uuid4(),
            policy_revision="policy:1",
        )
    with pytest.raises(ValueError, match="declared_filename"):
        workspace_id = uuid4()
        AdmissionCommand(
            workspace_id=workspace_id,
            actor_id=uuid4(),
            idempotency_key="upload:2",
            staged=staged_blob(workspace_id),
            physical_charge_id=uuid4(),
            policy_revision="policy:1",
            declared_filename=1,
        )

    with pytest.raises(ValueError, match="command workspace"):
        AdmissionCommand(
            workspace_id=uuid4(),
            actor_id=uuid4(),
            idempotency_key="upload:3",
            staged=staged_blob(uuid4()),
            physical_charge_id=uuid4(),
            policy_revision="policy:1",
        )


def test_results_reject_stringly_typed_dispositions() -> None:
    blob = blob_reference()
    admitted = admitted_blob(blob)
    with pytest.raises(ValueError, match="AdmissionDisposition"):
        AdmissionResult(
            "pending_install",
            admitted=admitted,
            inspection=inspection(blob, inspection_id=admitted.inspection_id),
        )
    with pytest.raises(ValueError, match="InstallDisposition"):
        InstallResult(
            "available",
            admitted,
            1,
            available_event=BlobAvailable(uuid4(), admitted, 1),
        )
    with pytest.raises(ValueError, match="FencedDisposition"):
        GcResult("applied", blob, 1)


def test_successful_admission_binds_exact_inspection_to_blob() -> None:
    blob = blob_reference()
    admitted = admitted_blob(blob)
    accepted = AdmissionResult(
        AdmissionDisposition.PENDING_INSTALL,
        admitted=admitted,
        inspection=inspection(blob, inspection_id=admitted.inspection_id),
    )
    assert accepted.blob == blob

    other = blob_reference()
    with pytest.raises(ValueError, match="inspection"):
        AdmissionResult(
            AdmissionDisposition.PENDING_INSTALL,
            admitted=admitted_blob(other),
            inspection=inspection(blob),
        )


def test_rejection_never_returns_an_ordinary_blob_or_inspection() -> None:
    rejected = AdmissionResult(
        AdmissionDisposition.REJECTED,
        failure=failure(
            MediaFailurePhase.ADMISSION,
            FailureDisposition.REJECT,
        ),
    )
    assert rejected.blob is None

    blob = blob_reference()
    with pytest.raises(ValueError, match="safe failure"):
        AdmissionResult(
            AdmissionDisposition.QUARANTINED,
            admitted=admitted_blob(blob),
            failure=failure(MediaFailurePhase.SECURITY),
        )


def test_installer_emits_only_media_owned_blob_available() -> None:
    blob = blob_reference()
    admitted = admitted_blob(blob)
    event = BlobAvailable(uuid4(), admitted, publish_generation=3)
    installed = InstallResult(
        InstallDisposition.AVAILABLE,
        admitted,
        publish_generation=3,
        available_event=event,
    )
    assert installed.available_event == event
    assert not hasattr(event, "association_id")

    command = InstallCommand(
        admitted,
        staged_blob(blob.workspace_id),
        uuid4(),
        publish_generation=3,
        idempotency_key="install:3",
    )
    assert command.staged.byte_size == event.byte_size
    with pytest.raises(ValueError, match="share a workspace"):
        InstallCommand(
            admitted,
            staged_blob(uuid4()),
            uuid4(),
            publish_generation=3,
            idempotency_key="install:foreign",
        )

    with pytest.raises(ValueError, match="installed generation"):
        InstallResult(
            InstallDisposition.AVAILABLE,
            admitted,
            publish_generation=2,
            available_event=event,
        )


def test_install_failure_requires_install_phase_and_has_no_event() -> None:
    blob = blob_reference()
    admitted = admitted_blob(blob)
    result = InstallResult(
        InstallDisposition.HOLD,
        admitted,
        publish_generation=1,
        failure=failure(MediaFailurePhase.INSTALL),
    )
    assert result.available_event is None

    with pytest.raises(ValueError, match="install phase"):
        InstallResult(
            InstallDisposition.RETRY,
            admitted,
            publish_generation=1,
            failure=failure(MediaFailurePhase.GC),
        )


def test_install_binds_admitted_digest_size_and_event_provenance() -> None:
    blob = blob_reference()
    admitted = admitted_blob(blob)
    staged = staged_blob(blob.workspace_id)

    InstallCommand(
        admitted,
        staged,
        uuid4(),
        publish_generation=1,
        idempotency_key="install:bound",
    )
    with pytest.raises(ValueError, match="digest and size"):
        InstallCommand(
            replace(admitted, byte_size=999),
            staged,
            uuid4(),
            publish_generation=1,
            idempotency_key="install:wrong-size",
        )
    with pytest.raises(ValueError, match="digest and size"):
        InstallCommand(
            replace(admitted, sha256=Sha256Digest("b" * 64)),
            staged,
            uuid4(),
            publish_generation=1,
            idempotency_key="install:wrong-digest",
        )

    event = BlobAvailable(uuid4(), admitted, publish_generation=1)
    InstallResult(
        InstallDisposition.AVAILABLE,
        admitted,
        publish_generation=1,
        available_event=event,
    )
    with pytest.raises(ValueError, match="BlobAvailable"):
        InstallResult(
            InstallDisposition.AVAILABLE,
            admitted,
            publish_generation=1,
            available_event=BlobAvailable(
                uuid4(), replace(admitted, byte_size=999), publish_generation=1
            ),
        )


def test_security_and_gc_commands_are_generation_fenced() -> None:
    blob = blob_reference()
    security = SecurityCommand(
        blob,
        generation=4,
        idempotency_key="security:4",
        operation=SecurityOperation.QUARANTINE,
    )
    gc = GcCommand(
        blob,
        generation=7,
        idempotency_key="gc:7",
        operation=GcOperation.PURGE,
    )
    assert (security.generation, gc.generation) == (4, 7)

    with pytest.raises(ValueError, match="positive"):
        GcCommand(
            blob,
            generation=0,
            idempotency_key="gc:0",
            operation=GcOperation.MARK,
        )


def test_fenced_results_reject_cross_phase_failures() -> None:
    blob = blob_reference()
    SecurityResult(
        FencedDisposition.HOLD,
        blob,
        generation=2,
        failure=failure(MediaFailurePhase.SECURITY),
    )
    GcResult(
        FencedDisposition.HOLD,
        blob,
        generation=2,
        failure=failure(MediaFailurePhase.GC),
    )

    with pytest.raises(ValueError, match="unrelated phase"):
        SecurityResult(
            FencedDisposition.RETRY,
            blob,
            generation=2,
            failure=failure(MediaFailurePhase.GC),
        )


def test_successful_fenced_result_cannot_smuggle_a_failure() -> None:
    with pytest.raises(ValueError, match="cannot contain"):
        GcResult(
            FencedDisposition.APPLIED,
            blob_reference(),
            generation=1,
            failure=failure(MediaFailurePhase.GC),
        )


def png_descriptor() -> FormatDescriptor:
    return FormatDescriptor(
        "png",
        FormatCategory.IMAGE,
        ("png",),
        ("image/png",),
        "magic",
        "png_validator",
        "image_core",
    )


def png_facts() -> FormatFacts:
    return FormatFacts(
        "png",
        "image/png",
        "magic",
        "1",
        "png_validator",
        "schema_1",
    )


def test_format_registry_uses_hints_without_turning_them_into_identity() -> None:
    png = png_descriptor()
    pdf = FormatDescriptor(
        "pdf",
        FormatCategory.DOCUMENT,
        ("pdf",),
        ("application/pdf",),
        "magic",
        "pdf_validator",
        "document_core",
    )
    registry = FormatRegistry(
        registry_version="registry-1",
        descriptors=(png, pdf),
    )

    assert registry.candidates(extension=".PNG") == (png,)
    assert registry.candidates(declared_media_type="APPLICATION/PDF") == (pdf,)
    assert registry.candidates(
        extension="png", declared_media_type="application/pdf"
    ) == (png, pdf)
    assert not hasattr(png, "renderable")


def test_format_registry_reports_missing_optional_validator_explicitly() -> None:
    registry = FormatRegistry(
        registry_version="registry-1",
        descriptors=(png_descriptor(),),
    )
    with pytest.raises(UnsupportedCapabilityError, match="png_validator"):
        registry.validate("png", BytesHandle(b"png"), byte_budget=32)


def test_format_registry_checks_validator_result_against_descriptor() -> None:
    validator = StaticValidator(png_facts())
    registry = FormatRegistry(
        registry_version="registry-1",
        descriptors=(png_descriptor(),),
        validators={"png_validator": validator},
    )
    assert registry.validate(
        "PNG", BytesHandle(b"png"), byte_budget=32
    ) == png_facts()
    assert validator.budgets == [32]

    mismatched = StaticValidator(
        FormatFacts(
            "jpeg",
            "image/jpeg",
            "magic",
            "1",
            "png_validator",
            "schema_1",
        )
    )
    registry = FormatRegistry(
        registry_version="registry-1",
        descriptors=(png_descriptor(),),
        validators={"png_validator": mismatched},
    )
    with pytest.raises(FormatRegistryContractError, match="format identity"):
        registry.validate("png", BytesHandle(b"png"), byte_budget=32)


def test_format_registry_rejects_ambiguous_ids_and_orphan_validators() -> None:
    with pytest.raises(ValueError, match="duplicate format_id"):
        FormatRegistry(
            registry_version="registry-1",
            descriptors=(png_descriptor(), png_descriptor()),
        )
    with pytest.raises(ValueError, match="no descriptor"):
        FormatRegistry(
            registry_version="registry-1",
            descriptors=(png_descriptor(),),
            validators={"orphan": StaticValidator(png_facts())},
        )
