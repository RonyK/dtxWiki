"""Generation-fenced physical GC driven by the shared reference ledger."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from backend.media.domain import BlobEpoch, BlobReference, BlobState
from backend.media.ports.contracts import (
    GenerationFence,
    PhysicalChargeTarget,
    PhysicalDeleteResult,
    PhysicalObjectKind,
    StorageKey,
)
from backend.media.ports.storage import GcTrashStore
from backend.models.media import (
    BlobIncarnationRow,
    MediaBlobGcCandidate,
    MediaBlobGcHold,
    MediaBlobReference,
    MediaPhysicalCharge,
    StoredBlobActiveIncarnationRow,
    StoredBlobRow,
)

from .coordination import lock_workspace_media_intent
from .quota import SqlAlchemyPhysicalQuota

logger = logging.getLogger(__name__)

#: Phases that moved bytes towards deletion while they can still come back.
_RESTORABLE_STATES = (BlobState.GC_PENDING.value, BlobState.TRASHED.value)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class BlobGcSweepResult:
    inspected_count: int
    purged_count: int
    cancelled_count: int
    #: Owned incarnations found mid-collection and restored to AVAILABLE.
    repaired_count: int = 0


class _OwnerAppearedDuringCollection(RuntimeError):
    """A durable owner exists for an incarnation GC already claimed."""


@dataclass(frozen=True, slots=True)
class _GcWork:
    blob: BlobReference
    generation: int
    storage_key: StorageKey
    charge_id: UUID
    charge_target: PhysicalChargeTarget
    state: BlobState

    @property
    def fence(self) -> GenerationFence:
        return GenerationFence(blob=self.blob, generation=self.generation)


class BlobGcHoldPolicy(Protocol):
    def __call__(
        self,
        session: Session,
        hold: MediaBlobGcHold,
        *,
        now: datetime,
    ) -> bool: ...


class SqlAlchemyBlobGcService:
    """Collect candidates through separately committed database phases.

    Phase 1 commits ``AVAILABLE -> GC_PENDING`` before storage is touched.
    Registration therefore fails closed for the whole physical-operation
    window. Phase 2 trashes bytes and commits ``TRASHED``. Phase 3 purges bytes
    and then durably releases the exact physical charge and records ``PURGED``.
    A crash after either filesystem mutation leaves a non-available DB state;
    replay is safe and no writer can attach new ownership to missing bytes.

    Every phase reads its decision state only after taking the first lock of
    the universal order, which on SQLite is the database write lock itself
    (see ``reserve_sqlite_writer``). An incarnation that nevertheless carries a
    durable owner while ``GC_PENDING`` or ``TRASHED`` -- left by an older
    build, or by a writer that read stale state -- is repaired rather than
    collected: its bytes come back from the generation-fenced trash and it
    becomes ``AVAILABLE`` again. Each sweep first looks for such incarnations,
    because their queue candidate is usually gone.
    """

    def __init__(
        self,
        *,
        session_for: Callable[[UUID], sessionmaker[Session]],
        store_for: Callable[[UUID], GcTrashStore],
        hold_is_active: BlobGcHoldPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_for = session_for
        self._store_for = store_for
        self._hold_is_active = hold_is_active
        self._clock = clock or _utc_now

    def sweep(
        self,
        *,
        workspace_id: UUID,
        limit: int = 100,
    ) -> BlobGcSweepResult:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace_id must be a UUID")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        run_at = self._clock()
        if run_at.tzinfo is None or run_at.utcoffset() is None:
            raise ValueError("GC clock must return a timezone-aware datetime")
        store = self._store_for(workspace_id)
        repaired_count = self._repair_owned_incarnations(
            workspace_id=workspace_id,
            store=store,
            limit=limit,
        )
        with self._session_for(workspace_id)() as session:
            identities = session.execute(
                select(
                    MediaBlobGcCandidate.blob_id,
                    MediaBlobGcCandidate.blob_epoch,
                )
                .where(
                    MediaBlobGcCandidate.workspace_id == workspace_id,
                    MediaBlobGcCandidate.eligible_at <= run_at,
                )
                .order_by(
                    MediaBlobGcCandidate.eligible_at,
                    MediaBlobGcCandidate.requested_at,
                    MediaBlobGcCandidate.blob_id,
                    MediaBlobGcCandidate.blob_epoch,
                )
                .limit(limit)
            ).all()

        inspected_count = 0
        purged_count = 0
        cancelled_count = 0
        for blob_id, blob_epoch in identities:
            disposition = self._collect_one(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
                store=store,
                run_at=run_at,
            )
            inspected_count += 1
            if disposition == "purged":
                purged_count += 1
            elif disposition == "cancelled":
                cancelled_count += 1
            elif disposition == "repaired":
                repaired_count += 1
        return BlobGcSweepResult(
            inspected_count=inspected_count,
            purged_count=purged_count,
            cancelled_count=cancelled_count,
            repaired_count=repaired_count,
        )

    def _collect_one(
        self,
        *,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
        store: GcTrashStore,
        run_at: datetime,
    ) -> str:
        prepared = self._prepare(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=blob_epoch,
            run_at=run_at,
        )
        if prepared == "repair":
            return self._repair_disposition(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
                store=store,
            )
        if isinstance(prepared, str):
            return prepared
        work = prepared
        if work.state in {BlobState.GC_PENDING, BlobState.MISSING}:
            store.trash(work.storage_key, fence=work.fence)
            if work.state is BlobState.GC_PENDING:
                try:
                    self._mark_trashed(work)
                except _OwnerAppearedDuringCollection:
                    # The bytes were moved to trash a moment ago and are still
                    # there: put them back instead of failing the sweep.
                    return self._repair_disposition(
                        workspace_id=workspace_id,
                        blob_id=blob_id,
                        blob_epoch=blob_epoch,
                        store=store,
                    )
                work = replace(work, state=BlobState.TRASHED)
        deletion = store.purge(
            work.storage_key,
            fence=work.fence,
            target=work.charge_target,
        )
        return self._finalize_purged(work, deletion)

    def _repair_disposition(
        self,
        *,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
        store: GcTrashStore,
    ) -> str:
        repaired = self._repair_one(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=blob_epoch,
            store=store,
        )
        return "repaired" if repaired else "deferred"

    def _repair_owned_incarnations(
        self,
        *,
        workspace_id: UUID,
        store: GcTrashStore,
        limit: int,
    ) -> int:
        """Restore owned incarnations a collection left mid-way.

        Owner registration deletes the queue candidate, so an incarnation
        that was claimed for collection and then (wrongly) acquired an owner is
        invisible to the candidate scan below; this is the only path that
        still finds it.
        """

        with self._session_for(workspace_id)() as session:
            stranded = session.execute(
                select(BlobIncarnationRow.blob_id, BlobIncarnationRow.blob_epoch)
                .where(
                    BlobIncarnationRow.workspace_id == workspace_id,
                    BlobIncarnationRow.state.in_(_RESTORABLE_STATES),
                    exists().where(
                        MediaBlobReference.workspace_id
                        == BlobIncarnationRow.workspace_id,
                        MediaBlobReference.blob_id == BlobIncarnationRow.blob_id,
                        MediaBlobReference.blob_epoch == BlobIncarnationRow.blob_epoch,
                    ),
                )
                .order_by(BlobIncarnationRow.blob_id, BlobIncarnationRow.blob_epoch)
                .limit(limit)
            ).all()
        return sum(
            self._repair_one(
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
                store=store,
            )
            for blob_id, blob_epoch in stranded
        )

    def _repair_one(
        self,
        *,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
        store: GcTrashStore,
    ) -> bool:
        """Bring an owned ``GC_PENDING``/``TRASHED`` incarnation back.

        Runs under the first lock of the universal order, re-checks state and
        ownership under it, restores the bytes from the incarnation's
        generation-fenced trash (a no-op when they never left the live path),
        and only then commits ``AVAILABLE`` and drops any queued collection.
        Restore is idempotent, so a crash before the commit is repaired by the
        next sweep.
        """

        with self._session_for(workspace_id).begin() as session:
            lock_workspace_media_intent(session, workspace_id, allow_deleting=True)
            incarnation = self._locked_incarnation(
                session,
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
            )
            if incarnation is None or incarnation.state not in _RESTORABLE_STATES:
                return False
            blob = BlobReference(workspace_id, blob_id, BlobEpoch(blob_epoch))
            if not self._has_reference(session, blob):
                return False
            try:
                store.restore(
                    StorageKey(incarnation.storage_key),
                    fence=GenerationFence(
                        blob=blob, generation=incarnation.gc_generation
                    ),
                )
            except Exception:
                logger.exception(
                    "media GC could not restore owned blob %s epoch %s in "
                    "workspace %s from its trash; it stays %s",
                    blob_id,
                    blob_epoch,
                    workspace_id,
                    incarnation.state,
                )
                return False
            logger.warning(
                "media GC restored owned blob %s epoch %s in workspace %s "
                "from %s back to available",
                blob_id,
                blob_epoch,
                workspace_id,
                incarnation.state,
            )
            incarnation.state = BlobState.AVAILABLE.value
            incarnation.trashed_at = None
            candidate = self._locked_candidate(
                session,
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
            )
            if candidate is not None:
                session.delete(candidate)
            return True

    def _prepare(
        self,
        *,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
        run_at: datetime,
    ) -> _GcWork | str:
        with self._session_for(workspace_id).begin() as session:
            lock_workspace_media_intent(session, workspace_id, allow_deleting=True)
            # Keep the universal writer order: workspace intent -> incarnation
            # -> GC candidate/reference rows. Registration holds the
            # incarnation before deleting a candidate, so taking the candidate
            # first here would deadlock with it on PostgreSQL.
            incarnation = self._locked_incarnation(
                session,
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
            )
            if incarnation is None:
                raise RuntimeError("gc_candidate_blob_incarnation_missing")
            candidate = self._locked_candidate(
                session,
                workspace_id=workspace_id,
                blob_id=blob_id,
                blob_epoch=blob_epoch,
            )
            if candidate is None:
                return "replay"
            if candidate.eligible_at > run_at:
                return "deferred"
            holds = session.scalars(
                select(MediaBlobGcHold)
                .where(
                    MediaBlobGcHold.workspace_id == workspace_id,
                    MediaBlobGcHold.blob_id == blob_id,
                    MediaBlobGcHold.blob_epoch == blob_epoch,
                )
                .with_for_update()
            ).all()
            active_hold = False
            for hold in holds:
                if self._hold_is_active(session, hold, now=run_at):
                    active_hold = True
                else:
                    session.delete(hold)
            if active_hold:
                return "deferred"
            owner_id = session.scalar(
                select(MediaBlobReference.owner_id)
                .where(
                    MediaBlobReference.workspace_id == workspace_id,
                    MediaBlobReference.blob_id == blob_id,
                    MediaBlobReference.blob_epoch == blob_epoch,
                )
                .limit(1)
            )
            state = BlobState(incarnation.state)
            if owner_id is not None:
                if state in {BlobState.GC_PENDING, BlobState.TRASHED}:
                    return "repair"
                if state is BlobState.MISSING:
                    # The bytes are gone and an owner still names them; there
                    # is nothing to collect while it does. Removing that owner
                    # queues a new candidate, which purges and releases quota.
                    logger.error(
                        "media GC found owned blob %s epoch %s in workspace %s "
                        "without its bytes; its owners read it as unavailable",
                        blob_id,
                        blob_epoch,
                        workspace_id,
                    )
                elif state is not BlobState.AVAILABLE:
                    raise RuntimeError("gc_non_available_blob_has_reference")
                session.delete(candidate)
                return "cancelled"
            if state is BlobState.PURGED:
                session.delete(candidate)
                return "replay"
            if state is BlobState.PENDING_INSTALL:
                raise RuntimeError("gc_candidate_blob_install_pending")
            if state is BlobState.AVAILABLE:
                incarnation.gc_generation += 1
                incarnation.state = BlobState.GC_PENDING.value
                session.flush()
                state = BlobState.GC_PENDING
            elif state is BlobState.MISSING and incarnation.gc_generation <= 0:
                # MISSING already rejects registration, so it is a valid
                # fail-closed phase-1 state; allocate the physical-operation
                # fence before probing live/trash paths and releasing quota.
                incarnation.gc_generation += 1
                session.flush()
            generation = incarnation.gc_generation
            if generation <= 0:
                raise RuntimeError("gc_candidate_generation_missing")
            blob = BlobReference(workspace_id, blob_id, BlobEpoch(blob_epoch))
            storage_key = StorageKey(incarnation.storage_key)
            charge = self._locked_charge(
                session,
                workspace_id=workspace_id,
                blob=blob,
                storage_key=storage_key,
            )
            target = PhysicalChargeTarget(
                workspace_id=workspace_id,
                kind=PhysicalObjectKind.BLOB_INCARNATION,
                object_id=blob_id,
                generation=charge.target_generation,
                blob=blob,
            )
            return _GcWork(
                blob=blob,
                generation=generation,
                storage_key=storage_key,
                charge_id=charge.id,
                charge_target=target,
                state=state,
            )

    def _mark_trashed(self, work: _GcWork) -> None:
        with self._session_for(work.blob.workspace_id).begin() as session:
            lock_workspace_media_intent(
                session, work.blob.workspace_id, allow_deleting=True
            )
            incarnation = self._locked_incarnation(
                session,
                workspace_id=work.blob.workspace_id,
                blob_id=work.blob.blob_id,
                blob_epoch=work.blob.epoch.value,
            )
            candidate = self._locked_candidate(
                session,
                workspace_id=work.blob.workspace_id,
                blob_id=work.blob.blob_id,
                blob_epoch=work.blob.epoch.value,
            )
            if incarnation is None:
                raise RuntimeError("gc_trash_state_missing")
            if incarnation.gc_generation != work.generation:
                raise RuntimeError("gc_trash_generation_changed")
            state = BlobState(incarnation.state)
            # Owner registration is what deletes a queued candidate, so a
            # missing one means an owner was registered after phase 1.
            if state in {BlobState.GC_PENDING, BlobState.TRASHED} and (
                candidate is None or self._has_reference(session, work.blob)
            ):
                raise _OwnerAppearedDuringCollection("gc_pending_blob_has_reference")
            if state is BlobState.TRASHED:
                return
            if state is not BlobState.GC_PENDING:
                raise RuntimeError(f"gc_trash_state_changed:{state.value}")
            incarnation.state = BlobState.TRASHED.value
            incarnation.trashed_at = incarnation.trashed_at or self._clock()

    def _finalize_purged(
        self,
        work: _GcWork,
        deletion: PhysicalDeleteResult,
    ) -> str:
        with self._session_for(work.blob.workspace_id).begin() as session:
            lock_workspace_media_intent(
                session, work.blob.workspace_id, allow_deleting=True
            )
            incarnation = self._locked_incarnation(
                session,
                workspace_id=work.blob.workspace_id,
                blob_id=work.blob.blob_id,
                blob_epoch=work.blob.epoch.value,
            )
            candidate = self._locked_candidate(
                session,
                workspace_id=work.blob.workspace_id,
                blob_id=work.blob.blob_id,
                blob_epoch=work.blob.epoch.value,
            )
            if incarnation is None:
                raise RuntimeError("gc_finalize_incarnation_missing")
            if BlobState(incarnation.state) is BlobState.PURGED:
                if candidate is not None:
                    session.delete(candidate)
                return "purged"
            if incarnation.gc_generation != work.generation:
                raise RuntimeError("gc_finalize_generation_changed")
            state = BlobState(incarnation.state)
            if state not in {BlobState.TRASHED, BlobState.MISSING}:
                raise RuntimeError(f"gc_finalize_state_changed:{state.value}")
            if self._has_reference(session, work.blob):
                # Only a writer that decided on stale state can register an
                # owner after phase 2, and the bytes are already purged. Say
                # so in the state instead of failing every later sweep: the
                # incarnation is MISSING, its owners read it as unavailable,
                # and removing them queues the purge that releases its quota.
                logger.error(
                    "media GC purged blob %s epoch %s in workspace %s while an "
                    "owner registered; it is now missing",
                    work.blob.blob_id,
                    work.blob.epoch.value,
                    work.blob.workspace_id,
                )
                incarnation.state = BlobState.MISSING.value
                if candidate is not None:
                    session.delete(candidate)
                return "lost"
            if candidate is None:
                raise RuntimeError("gc_finalize_candidate_missing")
            charge = self._locked_charge(
                session,
                workspace_id=work.blob.workspace_id,
                blob=work.blob,
                storage_key=work.storage_key,
            )
            if charge.id != work.charge_id:
                raise RuntimeError("gc_finalize_charge_changed")
            SqlAlchemyPhysicalQuota(session).release_after_deletion(
                work.blob.workspace_id,
                charge_id=work.charge_id,
                deletion=deletion,
            )
            incarnation.state = BlobState.PURGED.value
            incarnation.purged_at = self._clock()
            pointer = session.scalar(
                select(StoredBlobActiveIncarnationRow)
                .where(
                    StoredBlobActiveIncarnationRow.workspace_id
                    == work.blob.workspace_id,
                    StoredBlobActiveIncarnationRow.blob_id == work.blob.blob_id,
                )
                .with_for_update()
            )
            if pointer is not None:
                if pointer.blob_epoch != work.blob.epoch.value:
                    raise RuntimeError("gc_active_blob_epoch_changed")
                session.delete(pointer)
            root = session.scalar(
                select(StoredBlobRow)
                .where(
                    StoredBlobRow.workspace_id == work.blob.workspace_id,
                    StoredBlobRow.id == work.blob.blob_id,
                )
                .with_for_update()
            )
            if root is None:
                raise RuntimeError("gc_stored_blob_root_missing")
            root.updated_at = self._clock()
            session.delete(candidate)
            return "purged"

    @staticmethod
    def _locked_candidate(
        session: Session,
        *,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
    ) -> MediaBlobGcCandidate | None:
        return session.scalar(
            select(MediaBlobGcCandidate)
            .where(
                MediaBlobGcCandidate.workspace_id == workspace_id,
                MediaBlobGcCandidate.blob_id == blob_id,
                MediaBlobGcCandidate.blob_epoch == blob_epoch,
            )
            .with_for_update()
        )

    @staticmethod
    def _locked_incarnation(
        session: Session,
        *,
        workspace_id: UUID,
        blob_id: UUID,
        blob_epoch: int,
    ) -> BlobIncarnationRow | None:
        return session.scalar(
            select(BlobIncarnationRow)
            .where(
                BlobIncarnationRow.workspace_id == workspace_id,
                BlobIncarnationRow.blob_id == blob_id,
                BlobIncarnationRow.blob_epoch == blob_epoch,
            )
            .with_for_update()
        )

    @staticmethod
    def _has_reference(session: Session, blob: BlobReference) -> bool:
        return (
            session.scalar(
                select(MediaBlobReference.owner_id)
                .where(
                    MediaBlobReference.workspace_id == blob.workspace_id,
                    MediaBlobReference.blob_id == blob.blob_id,
                    MediaBlobReference.blob_epoch == blob.epoch.value,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _locked_charge(
        session: Session,
        *,
        workspace_id: UUID,
        blob: BlobReference,
        storage_key: StorageKey,
    ) -> MediaPhysicalCharge:
        charge = session.scalar(
            select(MediaPhysicalCharge)
            .where(
                MediaPhysicalCharge.workspace_id == workspace_id,
                MediaPhysicalCharge.target_kind
                == PhysicalObjectKind.BLOB_INCARNATION.value,
                MediaPhysicalCharge.blob_id == blob.blob_id,
                MediaPhysicalCharge.blob_epoch == blob.epoch.value,
                MediaPhysicalCharge.storage_key == storage_key.value,
            )
            .with_for_update()
        )
        if charge is None:
            raise RuntimeError("gc_blob_physical_charge_missing")
        if (
            charge.target_object_id != blob.blob_id
            or charge.target_epoch != blob.epoch.value
            or charge.target_generation <= 0
        ):
            raise RuntimeError("gc_blob_physical_charge_mismatch")
        return charge


__all__ = ["BlobGcHoldPolicy", "BlobGcSweepResult", "SqlAlchemyBlobGcService"]
