"""SQLAlchemy journal for the ordinary Shared Media install workflow."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.media.domain import (
    AvailableBlobCandidate,
    AvailableBlobRecord,
    BlobEpoch,
    BlobReference,
    BlobState,
    MediaStagingBytesExceededError,
    MediaStagingLimitExceededError,
    Sha256Digest,
    StoredBlob,
    WorkspaceBlobIdentity,
)
from backend.media.ports.contracts import (
    IdempotencyConflictError,
    PhysicalChargeTarget,
    PhysicalDeleteResult,
    PhysicalObjectKind,
    PublishReceipt,
    StagedBlob,
    StorageKey,
)
from backend.media.ports.coordination import OutboxMessage
from backend.media.ports.install import (
    BlobAssociationHold,
    MediaInstallInProgressError,
    PendingBlobInstall,
    PreparedBlobInstall,
    PreparedMediaOperation,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaBlobGcCandidate,
    MediaBlobGcHold,
    MediaInstallRequest,
    MediaPhysicalCharge,
    MediaStagingLease,
    StoredBlobInspectionRow,
    WorkspaceMediaQuotaUsage,
    WorkspaceMediaWriteIntent,
)
from backend.models.workspace import Workspace

from .available import SqlAlchemyAvailableBlobRepository
from .outbox import SqlAlchemyMediaOutbox
from .quota import SqlAlchemyPhysicalQuota
from .repository import SqlAlchemyMediaRepository

DEFAULT_WORKSPACE_MEDIA_QUOTA_BYTES = 10_000_000_000
DEFAULT_STAGING_ACTOR_MAX_COUNT = 16
DEFAULT_STAGING_SESSION_MAX_COUNT = 8
DEFAULT_STAGING_ACTOR_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_STAGING_SESSION_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_STAGING_ACTOR_RATE_PER_MINUTE = 60
DEFAULT_STAGING_SESSION_RATE_PER_MINUTE = 30
_ACTIVE_STAGING_STATES = (
    "reserved",
    "staged",
    "install_pending",
    "orphaned",
    "deletion_pending",
)
_INSTALL_CLAIM_TTL = timedelta(seconds=30)
_STAGING_START_CLAIM_TTL = timedelta(seconds=30)


class _BoundSessionFactory:
    """Expose one caller-owned transaction through the journal factory seam."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __call__(self):
        return nullcontext(self._session)

    def begin(self):
        return nullcontext(self._session)


class SqlAlchemyMediaInstallJournal:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | _BoundSessionFactory,
        *,
        actor_max_count: int = DEFAULT_STAGING_ACTOR_MAX_COUNT,
        session_max_count: int = DEFAULT_STAGING_SESSION_MAX_COUNT,
        actor_max_bytes: int = DEFAULT_STAGING_ACTOR_MAX_BYTES,
        session_max_bytes: int = DEFAULT_STAGING_SESSION_MAX_BYTES,
        actor_rate_per_minute: int = DEFAULT_STAGING_ACTOR_RATE_PER_MINUTE,
        session_rate_per_minute: int = DEFAULT_STAGING_SESSION_RATE_PER_MINUTE,
    ) -> None:
        self._session_factory = session_factory
        limits = (
            actor_max_count,
            session_max_count,
            actor_max_bytes,
            session_max_bytes,
            actor_rate_per_minute,
            session_rate_per_minute,
        )
        if any(
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
            for limit in limits
        ):
            raise ValueError("media staging limits must be positive integers")
        self._actor_max_count = actor_max_count
        self._session_max_count = session_max_count
        self._actor_max_bytes = actor_max_bytes
        self._session_max_bytes = session_max_bytes
        self._actor_rate_per_minute = actor_rate_per_minute
        self._session_rate_per_minute = session_rate_per_minute

    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyMediaInstallJournal:
        """Join an existing bounded-context unit of work without closing it."""

        return cls(_BoundSessionFactory(session))

    def resolve_install_blob(
        self,
        *,
        workspace_id: UUID,
        sha256: Sha256Digest,
        proposed_blob_id: UUID,
    ) -> BlobReference | None:
        """Resolve an optimistic incarnation without reviving a GC-owned one."""

        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id must be a UUID")
        if not isinstance(sha256, Sha256Digest):
            raise ValueError("sha256 must be a Sha256Digest")
        expected_blob_id = uuid5(
            NAMESPACE_URL,
            f"media-blob:{workspace_id}:{sha256.value}",
        )
        if proposed_blob_id != expected_blob_id:
            raise ValueError("media blob identity mismatch")
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, workspace_id)
            aggregate = SqlAlchemyMediaRepository(session).find_by_identity(
                WorkspaceBlobIdentity(workspace_id, sha256)
            )
            if aggregate is None:
                return BlobReference(workspace_id, proposed_blob_id, BlobEpoch(1))
            if aggregate.blob_id != proposed_blob_id:
                raise IdempotencyConflictError("workspace_blob_identity")
            if aggregate.active is None:
                return BlobReference(
                    workspace_id,
                    proposed_blob_id,
                    aggregate.next_epoch,
                )
            if aggregate.active.state in {
                BlobState.PENDING_INSTALL,
                BlobState.AVAILABLE,
                # Preserve exact durable failure replay while a failed
                # installation awaits reconciliation/GC. A new operation still
                # cannot allocate beside this active incarnation and waits.
                BlobState.MISSING,
            }:
                return BlobReference(
                    workspace_id,
                    proposed_blob_id,
                    aggregate.active.epoch,
                )
            # GC owns this incarnation until its separately committed finalize
            # removes the active pointer. Callers wait/retry instead of ever
            # publishing into a key that an older generation may still purge.
            return None

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
        """Persist the complete admission bundle before physical staging.

        The deterministic staging target commits the full application-level
        fingerprint.  Persisting it under the Workspace-scoped idempotency key
        lets a retry prove equality before consulting the digest index.
        """

        if target.kind is not PhysicalObjectKind.STAGING_LEASE:
            raise ValueError("install operation requires a staging target")
        if target.workspace_id != candidate.workspace_id:
            raise ValueError("install operation workspace mismatch")
        if not isinstance(admit_new_operation, bool):
            raise ValueError("admit_new_operation must be a boolean")
        if association_hold is not None and not isinstance(
            association_hold, BlobAssociationHold
        ):
            raise ValueError("association_hold must be a BlobAssociationHold")
        if (
            isinstance(admission_max_bytes, bool)
            or not isinstance(admission_max_bytes, int)
            or admission_max_bytes <= 0
        ):
            raise ValueError("admission_max_bytes must be a positive integer")
        expected_request_id = uuid5(
            NAMESPACE_URL,
            f"media-install:{candidate.workspace_id}:{idempotency_key}",
        )
        if request_id != expected_request_id:
            raise ValueError("media install request identity mismatch")
        expected_charge_id = uuid5(
            NAMESPACE_URL,
            f"media-stage-charge:{candidate.workspace_id}:{target.object_id}:"
            f"{target.generation}",
        )
        if charge_id != expected_charge_id:
            raise ValueError("media staging charge identity mismatch")
        expected_blob_id = uuid5(
            NAMESPACE_URL,
            f"media-blob:{candidate.workspace_id}:{candidate.sha256.value}",
        )
        if proposed_blob_id != expected_blob_id:
            raise ValueError("media blob identity mismatch")
        if (
            isinstance(proposed_blob_epoch, bool)
            or not isinstance(proposed_blob_epoch, int)
            or proposed_blob_epoch <= 0
        ):
            raise ValueError("media blob epoch must be positive")
        if (
            isinstance(publish_generation, bool)
            or not isinstance(publish_generation, int)
            or publish_generation <= 0
        ):
            raise ValueError("publish_generation must be positive")

        with self._session_factory.begin() as session:
            # A stale-policy caller may only resume an already-bound exact
            # operation. This read intentionally precedes every lookup or
            # write that could admit a new operation.
            if not admit_new_operation:
                bound_id = session.scalar(
                    select(MediaStagingLease.id).where(
                        MediaStagingLease.workspace_id == candidate.workspace_id,
                        MediaStagingLease.idempotency_key == idempotency_key,
                    )
                )
                if bound_id is None:
                    raise ValueError("media_policy_changed")

            self._ensure_workspace_scope(session, candidate.workspace_id, now=now)
            existing = session.scalar(
                select(MediaStagingLease)
                .where(
                    MediaStagingLease.workspace_id == candidate.workspace_id,
                    MediaStagingLease.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            expected = (
                target.object_id,
                target.generation,
                actor_id,
                session_key,
                storage_key.value,
                candidate.byte_size,
            )
            if existing is not None:
                actual = (
                    existing.id,
                    existing.generation,
                    existing.actor_id,
                    existing.session_key,
                    existing.storage_key,
                    existing.max_bytes,
                )
                immutable_bytes_changed = (
                    existing.byte_size != candidate.byte_size
                    or existing.sha256 != candidate.sha256.value
                )
                if actual != expected or immutable_bytes_changed:
                    raise IdempotencyConflictError("media_install_operation")
                if existing.state == "deleted":
                    available = self._find_available_candidate(session, candidate)
                    if available is None:
                        failed_request = session.get(
                            MediaInstallRequest,
                            {
                                "workspace_id": candidate.workspace_id,
                                "id": request_id,
                            },
                        )
                        if (
                            failed_request is not None
                            and failed_request.idempotency_key == idempotency_key
                            and failed_request.staging_id == target.object_id
                            and failed_request.staging_generation == target.generation
                            and failed_request.state == "failed"
                            and failed_request.last_failure_code
                        ):
                            raise ValueError(failed_request.last_failure_code)
                        raise ValueError(
                            "completed media install result is unavailable"
                        )
                    self._upsert_association_hold(
                        session,
                        blob=available.reference,
                        hold=association_hold,
                    )
                    return PreparedMediaOperation(reused=available)
                if existing.state in {"orphaned", "deletion_pending"}:
                    return PreparedMediaOperation(in_progress=True)

                charge = session.get(
                    MediaPhysicalCharge,
                    {
                        "workspace_id": candidate.workspace_id,
                        "id": charge_id,
                    },
                )
                self._assert_staging_charge_facts(
                    charge,
                    target=target,
                    storage_key=storage_key,
                    byte_size=candidate.byte_size,
                    allowed_states={"charged"},
                )
                request = session.get(
                    MediaInstallRequest,
                    {
                        "workspace_id": candidate.workspace_id,
                        "id": request_id,
                    },
                )
                if request is not None:
                    if (
                        request.idempotency_key != idempotency_key
                        or request.staging_id != target.object_id
                        or request.staging_generation != target.generation
                        or request.physical_charge_id != charge_id
                        or request.blob_id != proposed_blob_id
                        or request.blob_epoch != proposed_blob_epoch
                        or request.publish_generation != publish_generation
                    ):
                        raise IdempotencyConflictError("media_install_operation")
                    aggregate = SqlAlchemyMediaRepository(session).find_by_identity(
                        WorkspaceBlobIdentity(
                            candidate.workspace_id,
                            candidate.sha256,
                        )
                    )
                    if (
                        aggregate is None
                        or aggregate.active is None
                        or aggregate.blob_id != proposed_blob_id
                        or aggregate.active.epoch.value != request.blob_epoch
                        or aggregate.active.storage_key != candidate.storage_key
                        or aggregate.active.byte_size != candidate.byte_size
                    ):
                        raise ValueError("media install operation bundle facts changed")
                    self._assert_inspection_facts(
                        session,
                        candidate=candidate,
                        blob_id=aggregate.blob_id,
                        blob_epoch=aggregate.active.epoch.value,
                        inspection_id=request.inspection_id,
                    )
                    if request.state == "available":
                        available = self._find_available_candidate(session, candidate)
                        if available is None:
                            raise ValueError(
                                "completed media install result is unavailable"
                            )
                        self._upsert_association_hold(
                            session,
                            blob=available.reference,
                            hold=association_hold,
                        )
                        return PreparedMediaOperation(reused=available)
                    if (
                        request.state == "publishing"
                        and request.claim_token is not None
                        and request.claim_expires_at is not None
                        and request.claim_expires_at > now
                    ):
                        return PreparedMediaOperation(in_progress=True)
                    if request.state not in {"pending", "publishing", "retry"}:
                        raise ValueError("media install request is not resumable")
                else:
                    raise ValueError("media install operation bundle is missing")

                if (
                    existing.state in {"reserved", "staged"}
                    and existing.updated_at + _STAGING_START_CLAIM_TTL > now
                ):
                    return PreparedMediaOperation(in_progress=True)
                if existing.state not in {
                    "reserved",
                    "staged",
                    "install_pending",
                }:
                    raise ValueError("media install operation is not resumable")
                existing.updated_at = now
                existing.expires_at = expires_at
                return PreparedMediaOperation(
                    owns_staging=True,
                    claim_fence=expires_at,
                )

            # A durable binding means the original admission decision survives
            # policy changes. Only a genuinely new operation evaluates today's
            # size ceiling; an exact retry above already bypassed this branch.
            if not admit_new_operation:
                raise ValueError("media_policy_changed")
            if candidate.byte_size > admission_max_bytes:
                raise ValueError("media_too_large")

            available = self._find_available_candidate(session, candidate)
            if available is not None:
                session.add(
                    MediaStagingLease(
                        workspace_id=candidate.workspace_id,
                        id=target.object_id,
                        generation=target.generation,
                        actor_id=actor_id,
                        session_key=session_key,
                        idempotency_key=idempotency_key,
                        storage_key=storage_key.value,
                        max_bytes=candidate.byte_size,
                        byte_size=candidate.byte_size,
                        sha256=candidate.sha256.value,
                        state="deleted",
                        expires_at=expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
                self._upsert_association_hold(
                    session,
                    blob=available.reference,
                    hold=association_hold,
                )
                return PreparedMediaOperation(reused=available)

            media = SqlAlchemyMediaRepository(session)
            identity = WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256)
            aggregate = media.find_by_identity(identity)
            if aggregate is not None and aggregate.active is not None:
                return PreparedMediaOperation(in_progress=True)
            if (
                aggregate is not None
                and aggregate.next_epoch.value != proposed_blob_epoch
            ):
                return PreparedMediaOperation(in_progress=True)
            if aggregate is None and proposed_blob_epoch != 1:
                return PreparedMediaOperation(in_progress=True)

            self._enforce_staging_limits(
                session,
                workspace_id=candidate.workspace_id,
                actor_id=actor_id,
                session_key=session_key,
                requested_bytes=candidate.byte_size,
                now=now,
            )
            session.add(
                MediaStagingLease(
                    workspace_id=candidate.workspace_id,
                    id=target.object_id,
                    generation=target.generation,
                    actor_id=actor_id,
                    session_key=session_key,
                    idempotency_key=idempotency_key,
                    storage_key=storage_key.value,
                    max_bytes=candidate.byte_size,
                    byte_size=candidate.byte_size,
                    sha256=candidate.sha256.value,
                    state="reserved",
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            SqlAlchemyPhysicalQuota(session).reserve(
                candidate.workspace_id,
                charge_id=charge_id,
                target=target,
                storage_key=storage_key,
                bytes_=candidate.byte_size,
            )

            if aggregate is None:
                aggregate = StoredBlob(
                    blob_id=proposed_blob_id,
                    identity=identity,
                )
            aggregate = aggregate.allocate(
                byte_size=candidate.byte_size,
                storage_key=candidate.storage_key,
            )
            media.put(aggregate)
            assert aggregate.active is not None
            if aggregate.active.epoch.value != proposed_blob_epoch:
                raise RuntimeError("media install epoch allocation changed")
            blob = BlobReference(
                candidate.workspace_id,
                aggregate.blob_id,
                aggregate.active.epoch,
            )
            inspection_id = self._add_inspection(session, candidate, blob)
            intent = session.get(WorkspaceMediaWriteIntent, candidate.workspace_id)
            assert intent is not None
            session.add(
                MediaInstallRequest(
                    workspace_id=candidate.workspace_id,
                    id=request_id,
                    idempotency_key=idempotency_key,
                    write_generation=intent.delete_generation,
                    staging_id=target.object_id,
                    staging_generation=target.generation,
                    physical_charge_id=charge_id,
                    blob_id=blob.blob_id,
                    blob_epoch=blob.epoch.value,
                    inspection_id=inspection_id,
                    publish_generation=publish_generation,
                    state="pending",
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            SqlAlchemyMediaOutbox(session).add(
                OutboxMessage(
                    workspace_id=candidate.workspace_id,
                    idempotency_key=f"install-requested:{request_id}",
                    topic="InstallRequested",
                    schema_revision="1",
                    payload=(
                        ("request_id", str(request_id)),
                        ("staging_id", str(target.object_id)),
                        ("blob_id", str(blob.blob_id)),
                        ("blob_epoch", str(blob.epoch.value)),
                        ("publish_generation", str(publish_generation)),
                    ),
                    blob=blob,
                )
            )
            session.flush()
            return PreparedMediaOperation(
                owns_staging=True,
                claim_fence=expires_at,
            )

    def find_available(
        self,
        workspace_id: UUID,
        sha256: Sha256Digest,
    ) -> AvailableBlobRecord | None:
        with self._session_factory() as session:
            media = SqlAlchemyMediaRepository(session)
            aggregate = media.find_by_identity(
                WorkspaceBlobIdentity(workspace_id, sha256)
            )
            if (
                aggregate is None
                or aggregate.active is None
                or aggregate.active.state is not BlobState.AVAILABLE
            ):
                return None
            return self._available_repository(session).find(aggregate.identity)

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
    ) -> None:
        if target.kind is not PhysicalObjectKind.STAGING_LEASE:
            raise ValueError("staging reservation requires a staging target")
        with self._session_factory.begin() as session:
            self._ensure_workspace_scope(session, target.workspace_id, now=now)
            existing = session.scalar(
                select(MediaStagingLease)
                .where(
                    MediaStagingLease.workspace_id == target.workspace_id,
                    MediaStagingLease.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            expected = (
                target.object_id,
                target.generation,
                actor_id,
                session_key,
                storage_key.value,
                max_bytes,
            )
            if existing is not None:
                actual = (
                    existing.id,
                    existing.generation,
                    existing.actor_id,
                    existing.session_key,
                    existing.storage_key,
                    existing.max_bytes,
                )
                if actual != expected or existing.state in {
                    "orphaned",
                    "deletion_pending",
                    "deleted",
                }:
                    raise ValueError("media staging idempotency conflict")
                return
            self._enforce_staging_limits(
                session,
                workspace_id=target.workspace_id,
                actor_id=actor_id,
                session_key=session_key,
                requested_bytes=max_bytes,
                now=now,
            )
            session.add(
                MediaStagingLease(
                    workspace_id=target.workspace_id,
                    id=target.object_id,
                    generation=target.generation,
                    actor_id=actor_id,
                    session_key=session_key,
                    idempotency_key=idempotency_key,
                    storage_key=storage_key.value,
                    max_bytes=max_bytes,
                    state="reserved",
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
            )

    def _enforce_staging_limits(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        session_key: str,
        requested_bytes: int,
        now: datetime,
    ) -> None:
        """Enforce caller-scoped staging count, byte, and rate budgets.

        The Workspace media-intent lock is already held by ``reserve_staging``,
        so each decision is serialized with competing reservations. Reserved
        rows are charged at their declared maximum until their exact byte count
        is recorded; this keeps admission fail-closed before filesystem I/O.
        """

        dimensions = (
            (
                MediaStagingLease.actor_id == actor_id,
                self._actor_max_count,
                self._actor_max_bytes,
                self._actor_rate_per_minute,
            ),
            (
                MediaStagingLease.session_key == session_key,
                self._session_max_count,
                self._session_max_bytes,
                self._session_rate_per_minute,
            ),
        )
        for identity_filter, max_count, max_bytes, rate_per_minute in dimensions:
            active_filter = (
                MediaStagingLease.workspace_id == workspace_id,
                identity_filter,
                MediaStagingLease.state.in_(_ACTIVE_STAGING_STATES),
                MediaStagingLease.expires_at > now,
            )
            active_count = (
                session.scalar(
                    select(func.count(MediaStagingLease.id)).where(*active_filter)
                )
                or 0
            )
            if active_count >= max_count:
                raise MediaStagingLimitExceededError
            active_bytes = (
                session.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                func.coalesce(
                                    MediaStagingLease.byte_size,
                                    MediaStagingLease.max_bytes,
                                )
                            ),
                            0,
                        )
                    ).where(*active_filter)
                )
                or 0
            )
            if active_bytes + requested_bytes > max_bytes:
                raise MediaStagingBytesExceededError
            recent_count = (
                session.scalar(
                    select(func.count(MediaStagingLease.id)).where(
                        MediaStagingLease.workspace_id == workspace_id,
                        identity_filter,
                        MediaStagingLease.created_at >= now - timedelta(minutes=1),
                    )
                )
                or 0
            )
            if recent_count >= rate_per_minute:
                raise MediaStagingLimitExceededError

    def record_staged(
        self,
        staged: StagedBlob,
        *,
        charge_id: UUID,
        operation_claim: datetime,
    ) -> None:
        if staged.target.kind is not PhysicalObjectKind.STAGING_LEASE:
            raise ValueError("recorded staging requires a staging target")
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, staged.target.workspace_id)
            self._lock_staging_lease(session, staged.target)
            lease = self._locked_lease(session, staged.target)
            request = session.scalar(
                select(MediaInstallRequest).where(
                    MediaInstallRequest.workspace_id == staged.target.workspace_id,
                    MediaInstallRequest.staging_id == staged.target.object_id,
                    MediaInstallRequest.staging_generation == staged.target.generation,
                )
            )
            if request is None or request.physical_charge_id != charge_id:
                raise ValueError("media install operation bundle is missing")
            self._assert_staged_lease_facts(lease, staged)
            charge = session.get(
                MediaPhysicalCharge,
                {
                    "workspace_id": staged.target.workspace_id,
                    "id": charge_id,
                },
            )
            if lease.state == "deleted":
                self._assert_staging_charge_facts(
                    charge,
                    target=staged.target,
                    storage_key=staged.storage_key,
                    byte_size=staged.byte_size,
                    allowed_states={"released"},
                )
                return
            self._assert_staging_charge_facts(
                charge,
                target=staged.target,
                storage_key=staged.storage_key,
                byte_size=staged.byte_size,
                allowed_states={"charged"},
            )
            if (
                request.state in {"publishing", "available"}
                and lease.state == "install_pending"
            ):
                return
            if lease.expires_at != operation_claim:
                raise MediaInstallInProgressError
            if lease.state not in {"reserved", "staged", "install_pending"}:
                raise ValueError("media staging lease is not writable")
            if lease.state == "reserved":
                lease.state = "staged"
            lease.updated_at = datetime.now(UTC)

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
    ) -> PreparedBlobInstall:
        if candidate.storage_key != destination.value:
            raise ValueError("candidate destination mismatch")
        if (
            isinstance(proposed_blob_epoch, bool)
            or not isinstance(proposed_blob_epoch, int)
            or proposed_blob_epoch <= 0
        ):
            raise ValueError("media blob epoch must be positive")
        if (
            candidate.workspace_id != staged.target.workspace_id
            or candidate.sha256 != staged.sha256
            or candidate.byte_size != staged.byte_size
        ):
            raise ValueError("candidate does not match staged bytes")
        if (
            not isinstance(claim_token, str)
            or not claim_token
            or len(claim_token) > 160
        ):
            raise ValueError("media install claim token must be non-blank and bounded")
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, candidate.workspace_id)
            self._lock_staging_lease(session, staged.target)
            lease = self._locked_lease(session, staged.target)
            self._assert_staged_lease_facts(lease, staged)
            media = SqlAlchemyMediaRepository(session)
            identity = WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256)
            aggregate = media.find_by_identity(identity)
            if (
                aggregate is not None
                and aggregate.active is not None
                and aggregate.active.state is BlobState.AVAILABLE
            ):
                reused = self._find_available_candidate(session, candidate)
                assert reused is not None
                self._upsert_association_hold(
                    session,
                    blob=reused.reference,
                    hold=association_hold,
                )
                return PreparedBlobInstall(reused=reused)
            if (
                aggregate is None
                or aggregate.active is None
                or aggregate.blob_id != proposed_blob_id
                or aggregate.active.epoch.value != proposed_blob_epoch
            ):
                raise ValueError("media install operation bundle is missing")
            if lease.expires_at != operation_claim:
                raise MediaInstallInProgressError
            if lease.state not in {"staged", "install_pending"}:
                raise ValueError("media staging lease facts changed")
            staging_charge = session.get(
                MediaPhysicalCharge,
                {
                    "workspace_id": candidate.workspace_id,
                    "id": staging_charge_id,
                },
            )
            self._assert_staging_charge_facts(
                staging_charge,
                target=staged.target,
                storage_key=staged.storage_key,
                byte_size=staged.byte_size,
                allowed_states={"charged"},
            )
            pending = self._existing_pending(
                session,
                aggregate=aggregate,
                candidate=candidate,
                staged=staged,
                staging_charge_id=staging_charge_id,
                blob_charge_id=blob_charge_id,
                request_id=request_id,
                destination=destination,
                publish_generation=publish_generation,
                idempotency_key=idempotency_key,
                claim_token=claim_token,
                association_hold=association_hold,
            )
            if pending is None:
                return PreparedBlobInstall(in_progress=True)
            lease.state = "install_pending"
            lease.updated_at = datetime.now(UTC)
            return PreparedBlobInstall(pending=pending)

    def discard_unaccounted_staging(
        self,
        staged: StagedBlob,
        *,
        deletion: PhysicalDeleteResult,
    ) -> None:
        if (
            deletion.target != staged.target
            or deletion.storage_key != staged.storage_key
        ):
            raise ValueError("staging deletion does not match staged bytes")
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, staged.target.workspace_id)
            lease = self._locked_lease(session, staged.target)
            charge = session.scalar(
                select(MediaPhysicalCharge.id).where(
                    MediaPhysicalCharge.workspace_id == staged.target.workspace_id,
                    MediaPhysicalCharge.staging_id == staged.staging_id,
                    MediaPhysicalCharge.target_generation == staged.target.generation,
                )
            )
            if charge is not None:
                raise ValueError("accounted staging must use quota release")
            lease.state = "deleted"
            lease.updated_at = datetime.now(UTC)

    def release_staging(
        self,
        staged: StagedBlob,
        *,
        charge_id: UUID,
        deletion: PhysicalDeleteResult,
    ) -> None:
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, staged.target.workspace_id)
            SqlAlchemyPhysicalQuota(session).release_after_deletion(
                staged.target.workspace_id,
                charge_id=charge_id,
                deletion=deletion,
            )
            self._lock_staging_lease(session, staged.target)
            lease = self._locked_lease(session, staged.target)
            lease.state = "deleted"
            lease.updated_at = datetime.now(UTC)

    def release_install_claim(
        self,
        pending: PendingBlobInstall,
        *,
        failure_code: str,
    ) -> None:
        if (
            not isinstance(failure_code, str)
            or not failure_code
            or len(failure_code) > 160
        ):
            raise ValueError("failure_code must be non-blank and bounded")
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, pending.blob.workspace_id)
            request = session.scalar(
                select(MediaInstallRequest)
                .where(
                    MediaInstallRequest.workspace_id == pending.blob.workspace_id,
                    MediaInstallRequest.id == pending.request_id,
                )
                .with_for_update()
            )
            if request is None:
                raise ValueError("media install request was not found")
            if (
                request.state == "available"
                or request.claim_token != pending.claim_token
            ):
                return
            if (
                request.staging_id != pending.staged.staging_id
                or request.staging_generation != pending.staged.target.generation
                or request.physical_charge_id != pending.staging_charge_id
                or request.blob_id != pending.blob.blob_id
                or request.blob_epoch != pending.blob.epoch.value
                or request.inspection_id != pending.inspection_id
                or request.publish_generation != pending.publish_generation
            ):
                raise ValueError("media install request facts changed")
            now = datetime.now(UTC)
            request.state = "pending"
            request.claim_token = None
            request.claim_expires_at = None
            request.next_attempt_at = None
            request.last_failure_code = failure_code
            request.updated_at = now

    def complete_install(
        self,
        pending: PendingBlobInstall,
        *,
        publish: PublishReceipt,
        staging_deletion: PhysicalDeleteResult,
    ) -> AvailableBlobRecord:
        if (
            publish.fence.blob != pending.blob
            or publish.fence.generation != pending.publish_generation
            or publish.storage_key != pending.destination
            or publish.byte_size != pending.byte_size
            or publish.sha256 != pending.sha256
        ):
            raise ValueError("publish receipt does not match pending install")
        with self._session_factory.begin() as session:
            self._lock_operation_intent(session, pending.blob.workspace_id)
            request = session.scalar(
                select(MediaInstallRequest)
                .where(
                    MediaInstallRequest.workspace_id == pending.blob.workspace_id,
                    MediaInstallRequest.id == pending.request_id,
                )
                .with_for_update()
            )
            if request is None:
                raise ValueError("media install request was not found")
            identity = WorkspaceBlobIdentity(
                pending.blob.workspace_id,
                pending.sha256,
            )
            if request.state == "available":
                available = self._available_repository(session).find(identity)
                if available is None:
                    raise RuntimeError("available install lost its blob record")
                self._upsert_association_hold(
                    session,
                    blob=available.reference,
                    hold=pending.association_hold,
                )
                return available
            if request.state not in {"pending", "publishing", "retry"}:
                raise ValueError("media install request is not completable")
            if (
                request.claim_token != pending.claim_token
                or request.claim_expires_at is None
                or request.staging_id != pending.staged.staging_id
                or request.staging_generation != pending.staged.target.generation
                or request.physical_charge_id != pending.staging_charge_id
                or request.blob_id != pending.blob.blob_id
                or request.blob_epoch != pending.blob.epoch.value
                or request.inspection_id != pending.inspection_id
                or request.publish_generation != pending.publish_generation
            ):
                raise ValueError("media install request facts changed")

            quota = SqlAlchemyPhysicalQuota(session)
            quota.release_after_deletion(
                pending.blob.workspace_id,
                charge_id=pending.staging_charge_id,
                deletion=staging_deletion,
            )
            media = SqlAlchemyMediaRepository(session)
            aggregate = media.get_by_blob_id(
                pending.blob.workspace_id,
                pending.blob.blob_id,
            )
            if aggregate is None or aggregate.active is None:
                raise RuntimeError("pending blob incarnation is missing")
            quota.reserve(
                pending.blob.workspace_id,
                charge_id=pending.blob_charge_id,
                target=PhysicalChargeTarget(
                    workspace_id=pending.blob.workspace_id,
                    kind=PhysicalObjectKind.BLOB_INCARNATION,
                    object_id=pending.blob.blob_id,
                    generation=pending.publish_generation,
                    blob=pending.blob,
                ),
                storage_key=pending.destination,
                bytes_=pending.byte_size,
            )
            available_aggregate = aggregate.update_active(
                aggregate.active.transition(
                    BlobState.AVAILABLE,
                    generation=pending.publish_generation,
                )
            )
            media.put(available_aggregate)
            self._upsert_association_hold(
                session,
                blob=pending.blob,
                hold=pending.association_hold,
            )
            charge = session.get(
                MediaPhysicalCharge,
                {
                    "workspace_id": pending.blob.workspace_id,
                    "id": pending.blob_charge_id,
                },
            )
            if charge is None:
                raise RuntimeError("available blob charge was not persisted")
            charge.accounting_kind = "available"
            lease = self._locked_lease(session, pending.staged.target)
            lease.state = "deleted"
            lease.updated_at = datetime.now(UTC)
            request.state = "available"
            request.attempt_count += 1
            request.claim_token = None
            request.claim_expires_at = None
            request.last_failure_code = None
            available_at = datetime.now(UTC)
            request.updated_at = available_at
            # Availability is durably committed before several bounded-domain
            # association transactions. Queue the zero-owner incarnation by
            # default in this same transaction; the first successful ledger
            # registration cancels it under the blob lock. If every caller
            # fails after physical install, the candidate remains reachable
            # instead of leaking charged bytes forever.
            gc_candidate = session.get(
                MediaBlobGcCandidate,
                {
                    "workspace_id": pending.blob.workspace_id,
                    "blob_id": pending.blob.blob_id,
                    "blob_epoch": pending.blob.epoch.value,
                },
            )
            if gc_candidate is None:
                session.add(
                    MediaBlobGcCandidate(
                        workspace_id=pending.blob.workspace_id,
                        blob_id=pending.blob.blob_id,
                        blob_epoch=pending.blob.epoch.value,
                        reason="unowned_install",
                        requested_at=available_at,
                        eligible_at=available_at,
                    )
                )
            SqlAlchemyMediaOutbox(session).add(
                OutboxMessage(
                    workspace_id=pending.blob.workspace_id,
                    idempotency_key=(
                        f"blob-available:{pending.blob.blob_id}:"
                        f"{pending.blob.epoch.value}:{pending.publish_generation}"
                    ),
                    topic="BlobAvailable",
                    schema_revision="1",
                    payload=(
                        ("inspection_id", str(pending.inspection_id)),
                        ("byte_size", str(pending.byte_size)),
                        ("publish_generation", str(pending.publish_generation)),
                    ),
                    blob=pending.blob,
                )
            )
            inspection = session.get(
                StoredBlobInspectionRow,
                {
                    "workspace_id": pending.blob.workspace_id,
                    "id": pending.inspection_id,
                },
            )
            if inspection is None:
                raise RuntimeError("available blob inspection is missing")
            session.flush()
            return AvailableBlobRecord(
                reference=pending.blob,
                sha256=pending.sha256,
                byte_size=pending.byte_size,
                storage_key=pending.destination.value,
                canonical_media_type=inspection.canonical_media_type,
                inspection_id=pending.inspection_id,
            )

    def _existing_pending(
        self,
        session: Session,
        *,
        aggregate: StoredBlob,
        candidate: AvailableBlobCandidate,
        staged: StagedBlob,
        staging_charge_id: UUID,
        blob_charge_id: UUID,
        request_id: UUID,
        destination: StorageKey,
        publish_generation: int,
        idempotency_key: str,
        claim_token: str,
        association_hold: BlobAssociationHold | None,
    ) -> PendingBlobInstall | None:
        active = aggregate.active
        assert active is not None
        request = session.get(
            MediaInstallRequest,
            {"workspace_id": candidate.workspace_id, "id": request_id},
        )
        if (
            request is None
            or request.idempotency_key != idempotency_key
            or request.staging_id != staged.staging_id
            or request.staging_generation != staged.target.generation
            or request.physical_charge_id != staging_charge_id
            or request.blob_id != aggregate.blob_id
            or request.blob_epoch != active.epoch.value
            or request.publish_generation != publish_generation
            or active.state is not BlobState.PENDING_INSTALL
            or active.storage_key != destination.value
            or active.byte_size != candidate.byte_size
        ):
            raise ValueError("another media install is already pending")
        self._assert_inspection_facts(
            session,
            candidate=candidate,
            blob_id=aggregate.blob_id,
            blob_epoch=active.epoch.value,
            inspection_id=request.inspection_id,
        )
        if request.state not in {"pending", "publishing", "retry"}:
            raise ValueError("media install request is not claimable")
        now = datetime.now(UTC)
        if (
            request.claim_token is not None
            and request.claim_token != claim_token
            and request.claim_expires_at is not None
            and request.claim_expires_at > now
        ):
            return None
        request.state = "publishing"
        request.claim_token = claim_token
        request.claim_expires_at = now + _INSTALL_CLAIM_TTL
        request.next_attempt_at = None
        request.updated_at = now
        return PendingBlobInstall(
            request_id=request.id,
            blob=BlobReference(
                candidate.workspace_id,
                aggregate.blob_id,
                active.epoch,
            ),
            inspection_id=request.inspection_id,
            sha256=candidate.sha256,
            byte_size=candidate.byte_size,
            staged=staged,
            staging_charge_id=staging_charge_id,
            blob_charge_id=blob_charge_id,
            destination=destination,
            publish_generation=publish_generation,
            idempotency_key=idempotency_key,
            claim_token=claim_token,
            association_hold=association_hold,
        )

    @staticmethod
    def _upsert_association_hold(
        session: Session,
        *,
        blob: BlobReference,
        hold: BlobAssociationHold | None,
    ) -> None:
        if hold is None:
            return
        incarnation = session.scalar(
            select(BlobIncarnationRow)
            .where(
                BlobIncarnationRow.workspace_id == blob.workspace_id,
                BlobIncarnationRow.blob_id == blob.blob_id,
                BlobIncarnationRow.blob_epoch == blob.epoch.value,
            )
            .with_for_update()
        )
        if incarnation is None or incarnation.state != BlobState.AVAILABLE.value:
            raise ValueError("association hold requires an available incarnation")
        key = {
            "workspace_id": blob.workspace_id,
            "producer_kind": hold.producer_kind.value,
            "producer_id": hold.producer_id,
            "producer_generation": hold.producer_generation,
            "blob_id": blob.blob_id,
            "blob_epoch": blob.epoch.value,
        }
        existing = session.get(MediaBlobGcHold, key)
        if existing is None:
            session.add(MediaBlobGcHold(**key, expires_at=hold.expires_at))
            session.flush()
            return
        if existing.expires_at != hold.expires_at:
            if existing.expires_at is None or hold.expires_at is None:
                raise IdempotencyConflictError("media_blob_association_hold")
            existing.expires_at = max(existing.expires_at, hold.expires_at)
            session.flush()

    def _find_available_candidate(
        self,
        session: Session,
        candidate: AvailableBlobCandidate,
    ) -> AvailableBlobRecord | None:
        identity = WorkspaceBlobIdentity(candidate.workspace_id, candidate.sha256)
        aggregate = SqlAlchemyMediaRepository(session).find_by_identity(identity)
        if (
            aggregate is None
            or aggregate.active is None
            or aggregate.active.state is not BlobState.AVAILABLE
        ):
            return None
        available = self._available_repository(session).find(identity)
        assert available is not None
        if (
            available.byte_size != candidate.byte_size
            or available.canonical_media_type != candidate.canonical_media_type
        ):
            raise IdempotencyConflictError("media_digest_identity")
        return available

    @staticmethod
    def _lock_operation_intent(session: Session, workspace_id: UUID) -> None:
        # PostgreSQL takes an exclusive row lock; on SQLite the no-op UPDATE is
        # the first DML and therefore obtains write intent before the binding
        # read. The lock remains the first Shared Media lock in both dialects.
        result = session.execute(
            update(WorkspaceMediaWriteIntent)
            .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
            .values(updated_at=WorkspaceMediaWriteIntent.updated_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ValueError("workspace_media_intent_not_configured")
        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        if intent is None:
            raise ValueError("workspace_media_intent_not_configured")
        if intent.state == "deleting":
            raise ValueError("workspace_media_deleting")
        if intent.state != "open":
            raise RuntimeError("invalid_workspace_media_intent_state")

    @staticmethod
    def _locked_lease(
        session: Session,
        target: PhysicalChargeTarget,
    ) -> MediaStagingLease:
        lease = session.scalar(
            select(MediaStagingLease)
            .where(
                MediaStagingLease.workspace_id == target.workspace_id,
                MediaStagingLease.id == target.object_id,
            )
            .with_for_update()
        )
        if lease is None or lease.generation != target.generation:
            raise ValueError("media staging lease was not found")
        return lease

    @staticmethod
    def _lock_staging_lease(
        session: Session,
        target: PhysicalChargeTarget,
    ) -> None:
        # The caller already owns the Workspace media-intent row, the first
        # lock in the universal order. PostgreSQL then locks only this exact
        # lease; SQLite's no-op UPDATE gives the same deterministic fence.
        result = session.execute(
            update(MediaStagingLease)
            .where(
                MediaStagingLease.workspace_id == target.workspace_id,
                MediaStagingLease.id == target.object_id,
                MediaStagingLease.generation == target.generation,
            )
            .values(updated_at=MediaStagingLease.updated_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ValueError("media staging lease was not found")

    @staticmethod
    def _assert_staged_lease_facts(
        lease: MediaStagingLease,
        staged: StagedBlob,
    ) -> None:
        if (
            lease.storage_key != staged.storage_key.value
            or lease.byte_size != staged.byte_size
            or lease.sha256 != staged.sha256.value
        ):
            raise ValueError("media staging lease facts changed")

    @staticmethod
    def _assert_staging_charge_facts(
        charge: MediaPhysicalCharge | None,
        *,
        target: PhysicalChargeTarget,
        storage_key: StorageKey,
        byte_size: int,
        allowed_states: set[str],
    ) -> None:
        if (
            charge is None
            or charge.target_kind != PhysicalObjectKind.STAGING_LEASE.value
            or charge.target_object_id != target.object_id
            or charge.target_epoch != 0
            or charge.target_generation != target.generation
            or charge.staging_id != target.object_id
            or charge.blob_id is not None
            or charge.blob_epoch is not None
            or charge.accounting_kind != "staging"
            or charge.storage_key != storage_key.value
            or charge.byte_size != byte_size
            or charge.state not in allowed_states
        ):
            raise ValueError("media staging charge facts changed")

    @staticmethod
    def _assert_inspection_facts(
        session: Session,
        *,
        candidate: AvailableBlobCandidate,
        blob_id: UUID,
        blob_epoch: int,
        inspection_id: UUID,
    ) -> None:
        inspection = session.get(
            StoredBlobInspectionRow,
            {
                "workspace_id": candidate.workspace_id,
                "id": inspection_id,
            },
        )
        if (
            inspection is None
            or inspection.blob_id != blob_id
            or inspection.blob_epoch != blob_epoch
            or inspection.registry_version != candidate.registry_version
            or inspection.detector_id != candidate.detector_id
            or inspection.detector_version != candidate.detector_version
            or inspection.format_id != candidate.format_id
            or inspection.canonical_media_type != candidate.canonical_media_type
            or inspection.validation_schema_version
            != candidate.validation_schema_version
            or inspection.metadata_json != dict(candidate.metadata)
        ):
            raise ValueError("media install inspection facts changed")

    @staticmethod
    def _add_inspection(
        session: Session,
        candidate: AvailableBlobCandidate,
        blob: BlobReference,
    ) -> UUID:
        inspection_id = uuid5(
            NAMESPACE_URL,
            f"media-inspection:{blob.workspace_id}:{blob.blob_id}:"
            f"{blob.epoch.value}:{candidate.registry_version}:"
            f"{candidate.detector_id}:{candidate.detector_version}:"
            f"{candidate.format_id}:{candidate.canonical_media_type}:"
            f"{candidate.validation_schema_version}",
        )
        session.add(
            StoredBlobInspectionRow(
                workspace_id=blob.workspace_id,
                id=inspection_id,
                blob_id=blob.blob_id,
                blob_epoch=blob.epoch.value,
                registry_version=candidate.registry_version,
                detector_id=candidate.detector_id,
                detector_version=candidate.detector_version,
                format_id=candidate.format_id,
                canonical_media_type=candidate.canonical_media_type,
                validation_schema_version=candidate.validation_schema_version,
                metadata_json=dict(candidate.metadata),
                scanner_verdict="not_configured",
                scanner_provider=None,
                scanner_version=None,
                ordinary_available=True,
                created_at=datetime.now(UTC),
            )
        )
        session.flush()
        return inspection_id

    @staticmethod
    def _ensure_workspace_scope(
        session: Session,
        workspace_id: UUID,
        *,
        now: datetime,
    ) -> None:
        if session.get(Workspace, workspace_id) is None:
            raise ValueError("Workspace was not found")
        if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
            session.add(
                WorkspaceMediaWriteIntent(
                    workspace_id=workspace_id,
                    state="open",
                    delete_generation=0,
                    updated_at=now,
                )
            )
            session.flush()
        SqlAlchemyMediaInstallJournal._lock_operation_intent(session, workspace_id)
        if session.get(WorkspaceMediaQuotaUsage, workspace_id) is None:
            session.add(
                WorkspaceMediaQuotaUsage(
                    workspace_id=workspace_id,
                    limit_bytes=DEFAULT_WORKSPACE_MEDIA_QUOTA_BYTES,
                    charged_bytes=0,
                    revision=0,
                    updated_at=now,
                )
            )
            session.flush()

    @staticmethod
    def _available_repository(session: Session) -> SqlAlchemyAvailableBlobRepository:
        return SqlAlchemyAvailableBlobRepository(
            session,
            media_repository=SqlAlchemyMediaRepository(session),
            physical_quota=SqlAlchemyPhysicalQuota(session),
        )


__all__ = [
    "DEFAULT_STAGING_ACTOR_MAX_BYTES",
    "DEFAULT_STAGING_ACTOR_MAX_COUNT",
    "DEFAULT_STAGING_ACTOR_RATE_PER_MINUTE",
    "DEFAULT_STAGING_SESSION_MAX_BYTES",
    "DEFAULT_STAGING_SESSION_MAX_COUNT",
    "DEFAULT_STAGING_SESSION_RATE_PER_MINUTE",
    "SqlAlchemyMediaInstallJournal",
]
