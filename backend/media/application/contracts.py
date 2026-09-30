"""Framework-free commands, results, and events for media use cases.

These records define what an outer bounded context may ask the shared kernel to
do.  They contain no ORM rows, filesystem paths, HTTP objects, or concrete
adapter types.  Association activation deliberately remains outside the media
installer transaction and is driven by :class:`BlobAvailable`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from backend.media.domain import (
    BlobReference,
    FailureDisposition,
    MediaFailure,
    MediaFailurePhase,
    Sha256Digest,
    StoredBlobInspection,
)
from backend.media.ports.contracts import StagedBlob


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _require_positive(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_non_negative(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_key(value: str, name: str, *, maximum: int = 160) -> None:
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    )
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(char not in allowed for char in value)
    ):
        raise ValueError(f"{name} must be a bounded opaque identifier")


@dataclass(frozen=True, slots=True)
class AdmittedBlob:
    """Immutable admission facts carried through install and availability."""

    blob: BlobReference
    inspection_id: UUID
    sha256: Sha256Digest
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        _require_uuid(self.inspection_id, "inspection_id")
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")
        _require_non_negative(self.byte_size, "byte_size")


@dataclass(frozen=True, slots=True)
class AdmissionCommand:
    workspace_id: UUID
    actor_id: UUID
    idempotency_key: str
    staged: StagedBlob
    physical_charge_id: UUID
    policy_revision: str
    declared_filename: str | None = None
    declared_media_type: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.actor_id, "actor_id")
        _require_uuid(self.physical_charge_id, "physical_charge_id")
        if not isinstance(self.staged, StagedBlob):
            raise ValueError("staged must be a StagedBlob")
        if self.staged.target.workspace_id != self.workspace_id:
            raise ValueError("staged blob must belong to the command workspace")
        _require_key(self.idempotency_key, "idempotency_key")
        _require_key(self.policy_revision, "policy_revision")
        if self.declared_filename is not None and (
            not isinstance(self.declared_filename, str)
            or not self.declared_filename
            or len(self.declared_filename) > 255
        ):
            raise ValueError("declared_filename must be non-blank and bounded")
        if self.declared_media_type is not None:
            if not isinstance(self.declared_media_type, str):
                raise ValueError("declared_media_type must be a bounded MIME hint")
            normalized = self.declared_media_type.strip().lower()
            if (
                not normalized
                or len(normalized) > 160
                or normalized.count("/") != 1
                or any(char.isspace() for char in normalized)
            ):
                raise ValueError("declared_media_type must be a bounded MIME hint")
            object.__setattr__(self, "declared_media_type", normalized)


class AdmissionDisposition(StrEnum):
    PENDING_INSTALL = "pending_install"
    REUSED_AVAILABLE = "reused_available"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    disposition: AdmissionDisposition
    admitted: AdmittedBlob | None = None
    inspection: StoredBlobInspection | None = None
    failure: MediaFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, AdmissionDisposition):
            raise ValueError("disposition must be an AdmissionDisposition")
        success = self.disposition in {
            AdmissionDisposition.PENDING_INSTALL,
            AdmissionDisposition.REUSED_AVAILABLE,
        }
        if success:
            if (
                not isinstance(self.admitted, AdmittedBlob)
                or not isinstance(self.inspection, StoredBlobInspection)
                or self.failure is not None
            ):
                raise ValueError(
                    "successful admission requires blob and inspection only"
                )
            if (
                self.inspection.workspace_id != self.admitted.blob.workspace_id
                or self.inspection.blob_id != self.admitted.blob.blob_id
                or self.inspection.epoch != self.admitted.blob.epoch
                or self.inspection.inspection_id != self.admitted.inspection_id
            ):
                raise ValueError("inspection must describe the admitted incarnation")
            return
        if (
            self.admitted is not None
            or self.inspection is not None
            or not isinstance(self.failure, MediaFailure)
        ):
            raise ValueError("non-success admission requires only a safe failure")
        if self.failure.phase not in {
            MediaFailurePhase.ADMISSION,
            MediaFailurePhase.SECURITY,
        }:
            raise ValueError("admission failure has an unrelated phase")
        allowed = {
            AdmissionDisposition.REJECTED: {FailureDisposition.REJECT},
            AdmissionDisposition.QUARANTINED: {
                FailureDisposition.SECURITY_BLOCK
            },
            AdmissionDisposition.HOLD: {
                FailureDisposition.HOLD,
                FailureDisposition.RETRY,
            },
        }[self.disposition]
        if self.failure.disposition not in allowed:
            raise ValueError("admission disposition contradicts failure disposition")

    @property
    def blob(self) -> BlobReference | None:
        return None if self.admitted is None else self.admitted.blob


@dataclass(frozen=True, slots=True)
class InstallCommand:
    admitted: AdmittedBlob
    staged: StagedBlob
    physical_charge_id: UUID
    publish_generation: int
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, AdmittedBlob):
            raise ValueError("admitted must be an AdmittedBlob")
        if not isinstance(self.staged, StagedBlob):
            raise ValueError("staged must be a StagedBlob")
        if self.staged.target.workspace_id != self.admitted.blob.workspace_id:
            raise ValueError("staged blob and destination blob must share a workspace")
        if (
            self.staged.sha256 != self.admitted.sha256
            or self.staged.byte_size != self.admitted.byte_size
        ):
            raise ValueError("staged payload does not match admitted digest and size")
        _require_uuid(self.physical_charge_id, "physical_charge_id")
        _require_positive(self.publish_generation, "publish_generation")
        _require_key(self.idempotency_key, "idempotency_key")

    @property
    def blob(self) -> BlobReference:
        return self.admitted.blob


@dataclass(frozen=True, slots=True)
class BlobAvailable:
    event_id: UUID
    admitted: AdmittedBlob
    publish_generation: int

    def __post_init__(self) -> None:
        _require_uuid(self.event_id, "event_id")
        if not isinstance(self.admitted, AdmittedBlob):
            raise ValueError("admitted must be an AdmittedBlob")
        _require_positive(self.publish_generation, "publish_generation")

    @property
    def blob(self) -> BlobReference:
        return self.admitted.blob

    @property
    def inspection_id(self) -> UUID:
        return self.admitted.inspection_id

    @property
    def sha256(self) -> Sha256Digest:
        return self.admitted.sha256

    @property
    def byte_size(self) -> int:
        return self.admitted.byte_size


class InstallDisposition(StrEnum):
    AVAILABLE = "available"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    RETRY = "retry"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class InstallResult:
    disposition: InstallDisposition
    admitted: AdmittedBlob
    publish_generation: int
    available_event: BlobAvailable | None = None
    failure: MediaFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, InstallDisposition):
            raise ValueError("disposition must be an InstallDisposition")
        if not isinstance(self.admitted, AdmittedBlob):
            raise ValueError("admitted must be an AdmittedBlob")
        _require_positive(self.publish_generation, "publish_generation")
        success = self.disposition in {
            InstallDisposition.AVAILABLE,
            InstallDisposition.IDEMPOTENT_REPLAY,
        }
        if success:
            if (
                not isinstance(self.available_event, BlobAvailable)
                or self.failure is not None
            ):
                raise ValueError("successful install requires BlobAvailable only")
            if (
                self.available_event.admitted != self.admitted
                or self.available_event.publish_generation
                != self.publish_generation
            ):
                raise ValueError("BlobAvailable must match the installed generation")
            return
        if self.available_event is not None or not isinstance(
            self.failure, MediaFailure
        ):
            raise ValueError("failed install requires a safe failure and no event")
        if self.failure.phase is not MediaFailurePhase.INSTALL:
            raise ValueError("install failure must use the install phase")
        expected = {
            InstallDisposition.RETRY: FailureDisposition.RETRY,
            InstallDisposition.HOLD: FailureDisposition.HOLD,
        }[self.disposition]
        if self.failure.disposition is not expected:
            raise ValueError("install disposition contradicts failure disposition")

    @property
    def blob(self) -> BlobReference:
        return self.admitted.blob


class SecurityOperation(StrEnum):
    QUARANTINE = "quarantine"
    REINSPECT = "reinspect"
    RESTORE = "restore"
    REMOVE_BLOCKER = "remove_blocker"


class GcOperation(StrEnum):
    MARK = "mark"
    TRASH = "trash"
    RESTORE = "restore"
    PURGE = "purge"


@dataclass(frozen=True, slots=True)
class GenerationFencedCommand:
    blob: BlobReference
    generation: int
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        _require_positive(self.generation, "generation")
        _require_key(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True, slots=True)
class SecurityCommand(GenerationFencedCommand):
    operation: SecurityOperation

    def __post_init__(self) -> None:
        GenerationFencedCommand.__post_init__(self)
        if not isinstance(self.operation, SecurityOperation):
            raise ValueError("operation must be a SecurityOperation")


@dataclass(frozen=True, slots=True)
class GcCommand(GenerationFencedCommand):
    operation: GcOperation

    def __post_init__(self) -> None:
        GenerationFencedCommand.__post_init__(self)
        if not isinstance(self.operation, GcOperation):
            raise ValueError("operation must be a GcOperation")


class FencedDisposition(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    RETRY = "retry"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class GenerationFencedResult:
    disposition: FencedDisposition
    blob: BlobReference
    generation: int
    failure: MediaFailure | None = None

    _failure_phase: ClassVar[MediaFailurePhase | None] = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, FencedDisposition):
            raise ValueError("disposition must be a FencedDisposition")
        if not isinstance(self.blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        _require_positive(self.generation, "generation")
        success = self.disposition in {
            FencedDisposition.APPLIED,
            FencedDisposition.IDEMPOTENT_REPLAY,
        }
        if success and self.failure is not None:
            raise ValueError("successful fenced result cannot contain a failure")
        if not success and self.failure is None:
            raise ValueError("non-success fenced result requires a failure")
        if (
            self.failure is not None
            and self._failure_phase is not None
            and self.failure.phase is not self._failure_phase
        ):
            raise ValueError("fenced result failure has an unrelated phase")
        if self.failure is not None:
            expected = {
                FencedDisposition.RETRY: FailureDisposition.RETRY,
                FencedDisposition.HOLD: FailureDisposition.HOLD,
            }[self.disposition]
            if self.failure.disposition is not expected:
                raise ValueError(
                    "fenced disposition contradicts failure disposition"
                )


@dataclass(frozen=True, slots=True)
class SecurityResult(GenerationFencedResult):
    _failure_phase = MediaFailurePhase.SECURITY


@dataclass(frozen=True, slots=True)
class GcResult(GenerationFencedResult):
    _failure_phase = MediaFailurePhase.GC
