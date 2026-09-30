"""Install bytes that a surface validator has already classified.

The service owns the ordinary synchronous workflow while depending only on
inward ports.  Durable journal rows make a publish retryable; Library and
Context association activation remains outside this transaction boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic, sleep
from types import MappingProxyType
from typing import BinaryIO
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    BlobReference,
    Sha256Digest,
)
from backend.media.ports.contracts import (
    GenerationFence,
    PhysicalChargeTarget,
    PhysicalObjectKind,
)
from backend.media.ports.install import (
    BlobAssociationHold,
    BlobAssociationHoldProducerKind,
    MediaInstallInProgressError,
    MediaInstallJournal,
)
from backend.media.ports.storage import (
    BlobPublisher,
    BlobStager,
    BlobStorageLayout,
    PhysicalObjectDeleter,
)

_INSTALL_WAIT_TIMEOUT_SECONDS = 5.0
_INSTALL_WAIT_INTERVAL_SECONDS = 0.01
_STREAM_CHUNK_SIZE = 64 * 1024


class MediaSourceIntegrityError(ValueError):
    """The streamed bytes no longer match their validated digest or length."""


class InstallValidatedBlob:
    """Stage, journal, publish, and expose one validated immutable payload."""

    def __init__(
        self,
        *,
        stager: BlobStager,
        publisher: BlobPublisher,
        deleter: PhysicalObjectDeleter,
        layout: BlobStorageLayout,
        journal: MediaInstallJournal,
        clock=None,
        wait_timeout_seconds: float = _INSTALL_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(wait_timeout_seconds, bool)
            or not isinstance(wait_timeout_seconds, (int, float))
            or wait_timeout_seconds <= 0
        ):
            raise ValueError("wait_timeout_seconds must be positive")
        self._stager = stager
        self._publisher = publisher
        self._deleter = deleter
        self._layout = layout
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wait_timeout_seconds = float(wait_timeout_seconds)

    def execute(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        session_key: str,
        idempotency_key: str,
        content: bytes,
        max_bytes: int,
        canonical_media_type: str,
        format_id: str,
        detector_id: str,
        detector_version: str,
        registry_version: str,
        validation_schema_version: str,
        metadata: Mapping[str, str | int | bool] | None = None,
        admit_new_operation: bool = True,
        association_hold: BlobAssociationHold | None = None,
    ) -> AvailableBlobRecord:
        if not isinstance(content, bytes) or not content:
            raise ValueError("media content must be non-empty bytes")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if not isinstance(admit_new_operation, bool):
            raise ValueError("admit_new_operation must be a boolean")
        if association_hold is not None and not isinstance(
            association_hold, BlobAssociationHold
        ):
            raise ValueError("association_hold must be a BlobAssociationHold")
        _validate_association_hold(
            association_hold,
            actor_id=actor_id,
            session_key=session_key,
            idempotency_key=idempotency_key,
        )
        digest = Sha256Digest(sha256(content).hexdigest())
        blob_id = uuid5(
            NAMESPACE_URL,
            f"media-blob:{workspace_id}:{digest.value}",
        )
        target_wait_deadline = None
        while True:
            proposed_blob = self._journal.resolve_install_blob(
                workspace_id=workspace_id,
                sha256=digest,
                proposed_blob_id=blob_id,
            )
            if proposed_blob is not None:
                break
            target_wait_deadline = self._wait_for_owner(target_wait_deadline)
        operation_idempotency_key = _incarnation_idempotency_key(
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            blob=proposed_blob,
        )
        destination = self._layout.blob_key(
            proposed_blob,
            digest,
            publish_generation=1,
        )
        candidate = AvailableBlobCandidate(
            workspace_id=workspace_id,
            sha256=digest,
            byte_size=len(content),
            storage_key=destination.value,
            canonical_media_type=canonical_media_type,
            format_id=format_id,
            detector_id=detector_id,
            detector_version=detector_version,
            registry_version=registry_version,
            validation_schema_version=validation_schema_version,
            metadata=MappingProxyType(dict(metadata or {})),
        )
        request_id = uuid5(
            NAMESPACE_URL,
            f"media-install:{workspace_id}:{operation_idempotency_key}",
        )
        operation_fingerprint = _operation_fingerprint(
            candidate,
            actor_id=actor_id,
            session_key=session_key,
            idempotency_key=operation_idempotency_key,
            request_id=request_id,
        )
        staging_id = uuid5(
            NAMESPACE_URL,
            f"media-stage:{request_id}:{operation_fingerprint}",
        )
        staging_target = PhysicalChargeTarget(
            workspace_id=workspace_id,
            kind=PhysicalObjectKind.STAGING_LEASE,
            object_id=staging_id,
            generation=1,
        )
        staging_key = self._layout.staging_key(staging_target)
        staging_charge_id = uuid5(
            NAMESPACE_URL,
            f"media-stage-charge:{workspace_id}:{staging_id}:1",
        )
        operation_wait_deadline = None
        operation_claim = None
        while True:
            now = self._clock()
            operation = self._journal.prepare_operation(
                candidate=candidate,
                target=staging_target,
                actor_id=actor_id,
                session_key=session_key,
                idempotency_key=operation_idempotency_key,
                request_id=request_id,
                storage_key=staging_key,
                charge_id=staging_charge_id,
                proposed_blob_id=blob_id,
                proposed_blob_epoch=proposed_blob.epoch.value,
                publish_generation=1,
                admission_max_bytes=max_bytes,
                now=now,
                expires_at=now + timedelta(hours=1),
                admit_new_operation=admit_new_operation,
                association_hold=association_hold,
            )
            if operation.reused is not None:
                self._assert_existing(
                    operation.reused,
                    byte_size=len(content),
                    canonical_media_type=canonical_media_type,
                )
                return operation.reused
            if operation.owns_staging:
                operation_claim = operation.claim_fence
                break
            operation_wait_deadline = self._wait_for_owner(operation_wait_deadline)
        assert operation_claim is not None

        staged = self._stager.stage(
            (content,),
            target=staging_target,
            max_bytes=len(content),
        )
        if (
            staged.storage_key != staging_key
            or staged.sha256 != digest
            or staged.byte_size != len(content)
        ):
            raise RuntimeError("media stager changed immutable upload facts")

        try:
            self._journal.record_staged(
                staged,
                charge_id=staging_charge_id,
                operation_claim=operation_claim,
            )
        except MediaInstallInProgressError:
            raise
        except Exception:
            deletion = self._deleter.delete(
                staged.storage_key,
                target=staged.target,
            )
            self._journal.release_staging(
                staged,
                charge_id=staging_charge_id,
                deletion=deletion,
            )
            raise

        blob_charge_id = uuid5(
            NAMESPACE_URL,
            f"media-blob-charge:{workspace_id}:{blob_id}:"
            f"{proposed_blob.epoch.value}",
        )
        claim_token = uuid4().hex
        publish_wait_deadline = None
        while True:
            prepared = self._journal.prepare_install(
                candidate=candidate,
                staged=staged,
                staging_charge_id=staging_charge_id,
                blob_charge_id=blob_charge_id,
                request_id=request_id,
                proposed_blob_id=blob_id,
                proposed_blob_epoch=proposed_blob.epoch.value,
                destination=destination,
                publish_generation=1,
                idempotency_key=operation_idempotency_key,
                claim_token=claim_token,
                operation_claim=operation_claim,
                association_hold=association_hold,
            )
            if not prepared.in_progress:
                break
            publish_wait_deadline = self._wait_for_owner(publish_wait_deadline)
        if prepared.reused is not None:
            deletion = self._deleter.delete(staged.storage_key, target=staged.target)
            self._journal.release_staging(
                staged,
                charge_id=staging_charge_id,
                deletion=deletion,
            )
            self._assert_existing(
                prepared.reused,
                byte_size=len(content),
                canonical_media_type=canonical_media_type,
            )
            return prepared.reused

        pending = prepared.pending
        assert pending is not None
        try:
            publish = self._publisher.publish_no_overwrite(
                staged,
                target=pending.destination,
                fence=GenerationFence(pending.blob, pending.publish_generation),
            )
            staging_deletion = self._deleter.delete(
                staged.storage_key,
                target=staged.target,
            )
            return self._journal.complete_install(
                pending,
                publish=publish,
                staging_deletion=staging_deletion,
            )
        except Exception as exc:
            self._journal.release_install_claim(
                pending,
                failure_code=type(exc).__name__,
            )
            raise

    def execute_stream(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        session_key: str,
        idempotency_key: str,
        stream: BinaryIO,
        expected_sha256: str,
        expected_byte_size: int,
        max_bytes: int,
        canonical_media_type: str,
        format_id: str,
        detector_id: str,
        detector_version: str,
        registry_version: str,
        validation_schema_version: str,
        metadata: Mapping[str, str | int | bool] | None = None,
        admit_new_operation: bool = True,
        association_hold: BlobAssociationHold | None = None,
    ) -> AvailableBlobRecord:
        """Verify a seekable source before entering the durable install flow."""
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if (
            isinstance(expected_byte_size, bool)
            or not isinstance(expected_byte_size, int)
            or expected_byte_size <= 0
        ):
            raise ValueError("expected_byte_size must be a positive integer")
        digest = Sha256Digest(expected_sha256)
        content = b"".join(
            self._validated_stream_chunks(
                stream,
                expected_digest=digest,
                expected_byte_size=expected_byte_size,
            )
        )
        return self.execute(
            workspace_id=workspace_id,
            actor_id=actor_id,
            session_key=session_key,
            idempotency_key=idempotency_key,
            content=content,
            max_bytes=max_bytes,
            canonical_media_type=canonical_media_type,
            format_id=format_id,
            detector_id=detector_id,
            detector_version=detector_version,
            registry_version=registry_version,
            validation_schema_version=validation_schema_version,
            metadata=metadata,
            admit_new_operation=admit_new_operation,
            association_hold=association_hold,
        )

    def _wait_for_owner(self, deadline: float | None) -> float:
        now = monotonic()
        if deadline is None:
            deadline = now + self._wait_timeout_seconds
        remaining = deadline - now
        if remaining <= 0:
            raise MediaInstallInProgressError
        sleep(min(_INSTALL_WAIT_INTERVAL_SECONDS, remaining))
        return deadline

    @staticmethod
    def _validated_stream_chunks(
        stream: BinaryIO,
        *,
        expected_digest: Sha256Digest,
        expected_byte_size: int,
    ) -> Iterable[bytes]:
        actual_digest = sha256()
        actual_size = 0
        chunks: list[bytes] = []
        try:
            while True:
                chunk = stream.read(_STREAM_CHUNK_SIZE)
                if not isinstance(chunk, bytes) or len(chunk) > _STREAM_CHUNK_SIZE:
                    raise MediaSourceIntegrityError("media source integrity mismatch")
                if not chunk:
                    break
                actual_size += len(chunk)
                if actual_size > expected_byte_size:
                    raise MediaSourceIntegrityError("media source integrity mismatch")
                actual_digest.update(chunk)
                chunks.append(chunk)
        except MediaSourceIntegrityError:
            raise
        except Exception as exc:
            raise MediaSourceIntegrityError("media source integrity mismatch") from exc
        if (
            actual_size != expected_byte_size
            or actual_digest.hexdigest() != expected_digest.value
        ):
            raise MediaSourceIntegrityError("media source integrity mismatch")
        return chunks

    @staticmethod
    def _assert_existing(
        existing: AvailableBlobRecord,
        *,
        byte_size: int,
        canonical_media_type: str,
    ) -> None:
        if (
            existing.byte_size != byte_size
            or existing.canonical_media_type != canonical_media_type
        ):
            raise ValueError("available blob identity conflict")


def _operation_fingerprint(
    candidate: AvailableBlobCandidate,
    *,
    actor_id: UUID,
    session_key: str,
    idempotency_key: str,
    request_id: UUID,
) -> str:
    canonical = json.dumps(
        {
            "actor_id": str(actor_id),
            "byte_size": candidate.byte_size,
            "canonical_media_type": candidate.canonical_media_type,
            "detector_id": candidate.detector_id,
            "detector_version": candidate.detector_version,
            "format_id": candidate.format_id,
            "idempotency_key": idempotency_key,
            "metadata": dict(candidate.metadata),
            "registry_version": candidate.registry_version,
            "request_id": str(request_id),
            "session_key": session_key,
            "sha256": candidate.sha256.value,
            "storage_key": candidate.storage_key,
            "validation_schema_version": candidate.validation_schema_version,
            "workspace_id": str(candidate.workspace_id),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _incarnation_idempotency_key(
    *,
    workspace_id: UUID,
    idempotency_key: str,
    blob: BlobReference,
) -> str:
    """Keep epoch-one history stable and bind later incarnations distinctly."""

    if blob.epoch.value == 1:
        return idempotency_key
    fingerprint = sha256(
        (
            f"{workspace_id}:{idempotency_key}:"
            f"{blob.blob_id}:{blob.epoch.value}"
        ).encode()
    ).hexdigest()
    return f"media-reincarnation:{fingerprint}"


def _validate_association_hold(
    hold: BlobAssociationHold | None,
    *,
    actor_id: UUID,
    session_key: str,
    idempotency_key: str,
) -> None:
    if hold is None:
        return
    kind = hold.producer_kind
    if kind is BlobAssociationHoldProducerKind.CONTEXT_MEDIA_UPLOAD:
        valid = idempotency_key == f"context:{hold.producer_id}"
    elif kind is BlobAssociationHoldProducerKind.LIBRARY_CONVERSION_ATTEMPT:
        valid = actor_id == hold.producer_id and session_key == (
            f"library-conversion:{hold.producer_id}"
        )
    else:
        valid = actor_id == hold.producer_id and session_key == (
            f"library-preview:{hold.producer_id}"
        )
    if not valid:
        raise ValueError("association hold does not match its install operation")


__all__ = ["InstallValidatedBlob", "MediaSourceIntegrityError"]
