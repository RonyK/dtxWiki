"""Validated infrastructure-neutral values exchanged across media ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.media.domain import BlobReference, Sha256Digest


def _uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("byte_size must be a non-negative integer")


def _generation(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("generation must be a positive integer")


@dataclass(frozen=True, order=True, slots=True)
class StorageKey:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError("storage key must be a string")
        segments = self.value.split("/")
        has_drive = (
            len(self.value) >= 2 and self.value[0].isalpha() and self.value[1] == ":"
        )
        if (
            not self.value
            or self.value.startswith("/")
            or "\\" in self.value
            or has_drive
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise ValueError("storage key must be an opaque relative key")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class GenerationFence:
    blob: BlobReference
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.blob, BlobReference):
            raise ValueError("blob must be a BlobReference")
        _generation(self.generation)


class PhysicalObjectKind(StrEnum):
    STAGING_LEASE = "staging_lease"
    BLOB_INCARNATION = "blob_incarnation"
    QUARANTINED_UPLOAD = "quarantined_upload"
    DERIVED_ARTIFACT = "derived_artifact"


@dataclass(frozen=True, slots=True)
class PhysicalChargeTarget:
    workspace_id: UUID
    kind: PhysicalObjectKind
    object_id: UUID
    generation: int
    blob: BlobReference | None = None

    def __post_init__(self) -> None:
        _uuid(self.workspace_id, "workspace_id")
        if not isinstance(self.kind, PhysicalObjectKind):
            raise ValueError("kind must be a PhysicalObjectKind")
        _uuid(self.object_id, "object_id")
        _generation(self.generation)
        if self.kind is PhysicalObjectKind.BLOB_INCARNATION:
            if not isinstance(self.blob, BlobReference):
                raise ValueError("blob_incarnation target requires a BlobReference")
            if self.blob.workspace_id != self.workspace_id:
                raise ValueError("blob workspace must match target workspace")
            if self.blob.blob_id != self.object_id:
                raise ValueError("blob id must match target object_id")
        elif self.blob is not None:
            raise ValueError("only blob_incarnation target may carry a BlobReference")


PhysicalObjectReference = PhysicalChargeTarget


@dataclass(frozen=True, slots=True)
class StagedBlob:
    target: PhysicalChargeTarget
    storage_key: StorageKey
    byte_size: int
    sha256: Sha256Digest

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target, PhysicalChargeTarget)
            or self.target.kind is not PhysicalObjectKind.STAGING_LEASE
        ):
            raise ValueError("staged blob requires a staging lease target")
        if not isinstance(self.storage_key, StorageKey):
            raise ValueError("storage_key must be a StorageKey")
        _bytes(self.byte_size)
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")

    @property
    def staging_id(self) -> UUID:
        return self.target.object_id


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    receipt_id: UUID
    target: PhysicalChargeTarget
    storage_key: StorageKey
    physical_charge_id: UUID
    byte_size: int

    def __post_init__(self) -> None:
        _uuid(self.receipt_id, "receipt_id")
        if not isinstance(self.target, PhysicalChargeTarget):
            raise ValueError("target must be a PhysicalChargeTarget")
        if not isinstance(self.storage_key, StorageKey):
            raise ValueError("storage_key must be a StorageKey")
        _uuid(self.physical_charge_id, "physical_charge_id")
        _bytes(self.byte_size)


class PhysicalDeleteDisposition(StrEnum):
    """What a byte store observed while removing one opaque object key."""

    DELETED = "deleted"
    ALREADY_MISSING = "already_missing"


@dataclass(frozen=True, slots=True)
class PhysicalDeleteResult:
    """Stateless storage observation used to reconcile DB-owned quota.

    The result deliberately contains neither a byte size nor a quota charge.
    Those are authoritative database facts and cannot be reconstructed by an
    object store after a successful delete is replayed.
    """

    disposition: PhysicalDeleteDisposition
    target: PhysicalChargeTarget
    storage_key: StorageKey

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PhysicalDeleteDisposition):
            raise ValueError("disposition must be a PhysicalDeleteDisposition")
        if not isinstance(self.target, PhysicalChargeTarget):
            raise ValueError("target must be a PhysicalChargeTarget")
        if not isinstance(self.storage_key, StorageKey):
            raise ValueError("storage_key must be a StorageKey")


class PublishDisposition(StrEnum):
    PUBLISHED = "published"
    IDEMPOTENT_REPLAY = "idempotent_replay"


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    disposition: PublishDisposition
    fence: GenerationFence
    storage_key: StorageKey
    byte_size: int
    sha256: Sha256Digest

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PublishDisposition):
            raise ValueError("disposition must be a PublishDisposition")
        if not isinstance(self.fence, GenerationFence):
            raise ValueError("fence must be a GenerationFence")
        if not isinstance(self.storage_key, StorageKey):
            raise ValueError("storage_key must be a StorageKey")
        _bytes(self.byte_size)
        if not isinstance(self.sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")


class UnsupportedCapabilityError(RuntimeError):
    """Raised when a caller explicitly requests an absent optional capability."""


class GenerationFenceError(RuntimeError):
    """Raised when a stale operation attempts to mutate a newer generation."""


class IdempotencyConflictError(RuntimeError):
    """Raised when an idempotency key is replayed with a different payload."""


class NoOverwriteError(RuntimeError):
    """Raised when a publish target already contains different bytes."""
