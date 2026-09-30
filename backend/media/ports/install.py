"""Persistence journal for the ordinary durable media install workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    BlobReference,
    MediaStagingLimitExceededError,
    Sha256Digest,
)
from backend.media.ports.contracts import (
    PhysicalChargeTarget,
    PhysicalDeleteResult,
    PublishReceipt,
    StagedBlob,
    StorageKey,
)


class MediaInstallInProgressError(MediaStagingLimitExceededError):
    """A concurrent owner may still commit; callers must retry safely."""

    error_code = "media_install_in_progress"
    retryable = True
    outcome_unknown = True


class BlobAssociationHoldProducerKind(StrEnum):
    """Bounded-context operation that temporarily owns an installed blob."""

    CONTEXT_MEDIA_UPLOAD = "context_media_upload"
    LIBRARY_CONVERSION_ATTEMPT = "library_conversion_attempt"
    LIBRARY_PREVIEW_ATTEMPT = "library_preview_attempt"


@dataclass(frozen=True, slots=True)
class BlobAssociationHold:
    """Durable producer fence spanning install and domain association commits."""

    producer_kind: BlobAssociationHoldProducerKind
    producer_id: UUID
    producer_generation: int
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.producer_kind, BlobAssociationHoldProducerKind):
            raise ValueError("producer_kind must be a BlobAssociationHoldProducerKind")
        if not isinstance(self.producer_id, UUID):
            raise ValueError("producer_id must be a UUID")
        if (
            isinstance(self.producer_generation, bool)
            or not isinstance(self.producer_generation, int)
            or self.producer_generation <= 0
        ):
            raise ValueError("producer_generation must be a positive integer")
        if self.producer_kind is BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD:
            if (
                not isinstance(self.expires_at, datetime)
                or self.expires_at.utcoffset() is None
            ):
                raise ValueError("context media upload holds require an aware expiry")
        elif self.expires_at is not None:
            raise ValueError("asynchronous producer holds must not expire")


@dataclass(frozen=True, slots=True)
class PendingBlobInstall:
    request_id: UUID
    blob: BlobReference
    inspection_id: UUID
    sha256: Sha256Digest
    byte_size: int
    staged: StagedBlob
    staging_charge_id: UUID
    blob_charge_id: UUID
    destination: StorageKey
    publish_generation: int
    idempotency_key: str
    claim_token: str
    association_hold: BlobAssociationHold | None = None


@dataclass(frozen=True, slots=True)
class PreparedBlobInstall:
    reused: AvailableBlobRecord | None = None
    pending: PendingBlobInstall | None = None
    in_progress: bool = False

    def __post_init__(self) -> None:
        disposition_count = sum(
            (
                self.reused is not None,
                self.pending is not None,
                self.in_progress,
            )
        )
        if disposition_count != 1:
            raise ValueError("prepared install requires exactly one disposition")


@dataclass(frozen=True, slots=True)
class PreparedMediaOperation:
    reused: AvailableBlobRecord | None = None
    owns_staging: bool = False
    in_progress: bool = False
    claim_fence: datetime | None = None

    def __post_init__(self) -> None:
        disposition_count = sum(
            (
                self.reused is not None,
                self.owns_staging,
                self.in_progress,
            )
        )
        if disposition_count != 1:
            raise ValueError("prepared operation requires exactly one disposition")
        if self.owns_staging != (self.claim_fence is not None):
            raise ValueError("staging ownership requires an exact claim fence")


class MediaInstallJournal(Protocol):
    def resolve_install_blob(
        self,
        *,
        workspace_id: UUID,
        sha256: Sha256Digest,
        proposed_blob_id: UUID,
    ) -> BlobReference | None:
        """Return the current/next incarnation, or None while GC owns it."""
        ...

    def prepare_operation(
        self,
        *,
        candidate: AvailableBlobCandidate,
        target: PhysicalChargeTarget,
        actor_id: UUID,
        session_key: str,
        idempotency_key: str,
        request_id: UUID,
        storage_key: StorageKey,
        charge_id: UUID,
        proposed_blob_id: UUID,
        proposed_blob_epoch: int,
        publish_generation: int,
        admission_max_bytes: int,
        now: datetime,
        expires_at: datetime,
        admit_new_operation: bool = True,
        association_hold: BlobAssociationHold | None = None,
    ) -> PreparedMediaOperation:
        """Atomically bind and physically admit an operation before staging.

        The disposition either returns the stored result, grants this caller
        the staging workflow, or tells an identical concurrent replay to wait.
        """
        ...

    def find_available(
        self,
        workspace_id: UUID,
        sha256: Sha256Digest,
    ) -> AvailableBlobRecord | None: ...

    def reserve_staging(
        self,
        *,
        target: PhysicalChargeTarget,
        actor_id: UUID,
        session_key: str,
        idempotency_key: str,
        storage_key: StorageKey,
        max_bytes: int,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    def record_staged(
        self,
        staged: StagedBlob,
        *,
        charge_id: UUID,
        operation_claim: datetime,
    ) -> None: ...

    def discard_unaccounted_staging(
        self,
        staged: StagedBlob,
        *,
        deletion: PhysicalDeleteResult,
    ) -> None: ...

    def prepare_install(
        self,
        *,
        candidate: AvailableBlobCandidate,
        staged: StagedBlob,
        staging_charge_id: UUID,
        blob_charge_id: UUID,
        request_id: UUID,
        proposed_blob_id: UUID,
        proposed_blob_epoch: int,
        destination: StorageKey,
        publish_generation: int,
        idempotency_key: str,
        claim_token: str,
        operation_claim: datetime,
        association_hold: BlobAssociationHold | None = None,
    ) -> PreparedBlobInstall: ...

    def release_install_claim(
        self,
        pending: PendingBlobInstall,
        *,
        failure_code: str,
    ) -> None: ...

    def release_staging(
        self,
        staged: StagedBlob,
        *,
        charge_id: UUID,
        deletion: PhysicalDeleteResult,
    ) -> None: ...

    def complete_install(
        self,
        pending: PendingBlobInstall,
        *,
        publish: PublishReceipt,
        staging_deletion: PhysicalDeleteResult,
    ) -> AvailableBlobRecord: ...
