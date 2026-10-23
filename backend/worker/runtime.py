from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.container import BlobStoreProvider, DatabaseProvider, ServiceRegistry
from backend.app.settings import Settings
from backend.models.job import Job
from backend.models.library import (
    LibraryAttempt,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
)
from backend.models.lint import LintRun
from backend.models.workspace import Workspace
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.actor_identities import SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID
from backend.services.bundle_expiration import BundleExpirationService
from backend.services.context_media_orphan_sweep import ContextMediaOrphanSweepService
from backend.services.context_media_policy import read_context_media_policy
from backend.services.context_reconciliation_reminder import (
    RECONCILIATION_REMINDER_JOB_TYPE,
    ContextReconciliationReminderService,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.extraction_service import ExtractionService
from backend.services.feedback_errors import FeedbackItemNotFoundError
from backend.services.feedback_resolver_service import FeedbackResolverService
from backend.services.indexing import CompositeIndexer, build_indexer
from backend.services.job_hints import (
    clear_hint_if_not_refreshed,
    peek_oldest_hinted_workspace,
)
from backend.services.job_queue import DEFAULT_JOB_MAX_ATTEMPTS, JobQueueService
from backend.services.library_service import LibraryCandidateInput, LibraryService
from backend.services.mail_delivery import EmailDeliveryService
from backend.worker.registry import JobHandler, get_handler, register

logger = logging.getLogger(__name__)


class _MediaInstallReconciler(Protocol):
    def sweep_expired(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        now: datetime | None = None,
    ) -> Any: ...


if TYPE_CHECKING:
    from backend.services.conversion_orchestrator import (
        LibraryConversionOrchestrator,
    )
    from backend.services.preview_orchestrator import LibraryPreviewOrchestrator

# Errors that signal a missing target or a malformed job: retrying cannot
# succeed, so the job fails terminally on its first attempt (max_retries=0).
# Everything else is treated as potentially transient and retried up to the
# shared DEFAULT_JOB_MAX_ATTEMPTS budget (max-total-attempts semantics,
# matching reclaim_stale_jobs).
_TERMINAL_JOB_ERRORS = (FeedbackItemNotFoundError, ValueError)


class WorkerShutdownRequested(BaseException):
    """SIGTERM/SIGINT arrived while the worker was inside an interruptible step.

    A ``BaseException`` on purpose: the dispatcher's ``except Exception``
    branches turn handler errors into a failed attempt, and a shutdown is not
    a failure of the job. The interrupted job's lease is released instead (see
    ``OneShotWorker._release_interrupted_claim``).
    """

    def __init__(self, signal_name: str) -> None:
        super().__init__(signal_name)
        self.signal_name = signal_name


@dataclass(frozen=True)
class InitialRecoveryResult:
    """Outcome of the first stale-lease pass, before worker readiness.

    A control-plane failure means the pass could not run. Individual workspace
    failures remain isolated, as they are during periodic recovery.
    """

    roster_available: bool
    reclaimed_count: int = 0
    failed_workspace_ids: tuple[UUID, ...] = ()

    @property
    def status(self) -> str:
        if not self.roster_available:
            return "roster_failed"
        return "partial" if self.failed_workspace_ids else "complete"


@dataclass(frozen=True)
class WorkerDispatchResult:
    job_id: UUID
    job_type: str
    status: str
    detail: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "job_type": self.job_type,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class _ClaimedJob:
    """Workspace-routable job input detached from the control-plane session."""

    id: UUID
    workspace_id: UUID
    job_type: str
    trigger_type: str
    target_ref: dict[str, Any]
    idempotency_key: str | None
    # The claim's own lease stamp. Releasing an interrupted claim is guarded by
    # it, so a lease that was reclaimed (and possibly re-claimed) meanwhile is
    # never touched.
    started_at: datetime | None = None

    @classmethod
    def from_model(cls, job: Job) -> _ClaimedJob:
        return cls(
            id=job.id,
            workspace_id=job.workspace_id,
            job_type=job.job_type,
            trigger_type=job.trigger_type,
            target_ref=dict(job.target_ref or {}),
            idempotency_key=job.idempotency_key,
            started_at=job.started_at,
        )


class OneShotWorker:
    def __init__(
        self,
        job_queue: JobQueueService | None = None,
        library_service: LibraryService | None = None,
        extraction_service: ExtractionService | None = None,
        bundle_expiration_service: BundleExpirationService | None = None,
        feedback_resolver: FeedbackResolverService | None = None,
        email_delivery_service: EmailDeliveryService | None = None,
        media_install_reconciler: _MediaInstallReconciler | None = None,
        context_media_orphan_sweep_service: ContextMediaOrphanSweepService
        | None = None,
        indexer: object | None = None,
        settings: Settings | None = None,
        database_provider: DatabaseProvider | None = None,
        services: ServiceRegistry | None = None,
        blob_store_provider: BlobStoreProvider | None = None,
    ) -> None:
        self.job_queue = job_queue or JobQueueService()
        self.library_service = library_service or LibraryService(
            job_queue=self.job_queue
        )
        self._extraction_service_override = extraction_service
        self.bundle_expiration_service = (
            bundle_expiration_service
            or BundleExpirationService(job_queue=self.job_queue)
        )
        self.feedback_resolver = feedback_resolver or FeedbackResolverService()
        self.email_delivery_service = email_delivery_service
        self.reconciliation_reminder_service = ContextReconciliationReminderService()
        self.media_install_reconciler = media_install_reconciler
        self.context_media_orphan_sweep_service = context_media_orphan_sweep_service
        self._indexer = indexer
        # Keep one application composition graph for the worker's lazily
        # resolved services.  Constructing a fresh ServiceRegistry inside each
        # handler silently discarded an overlay's BlobStoreProvider and sent
        # conversion, Preview, and reconciliation back to the default store.
        self._services = services if services is not None else ServiceRegistry()
        provider_settings = (
            getattr(database_provider, "settings", None)
            if database_provider is not None
            else None
        )
        blob_settings = (
            getattr(blob_store_provider, "settings", None)
            if blob_store_provider is not None
            else None
        )
        resolved_settings = (
            settings or self._services.settings or provider_settings or blob_settings
        )
        # The entrypoint's resolved settings (including --db overrides). A
        # fresh Settings() here would read the process env instead (R1′).
        self._settings = resolved_settings
        if resolved_settings is not None:
            self._services.settings = resolved_settings
        if database_provider is not None:
            # Keep workspace-owned application services on the worker's exact
            # routing provider.  In particular media_blob_gc must open and
            # commit its own short phases after the job claim transaction.
            self._services.context_media_session_for = database_provider.session_for
        if blob_store_provider is not None:
            # Match assemble(): seed the cached-property slot before any
            # dependent service is resolved, preserving the exact overlay
            # provider instead of wrapping or replacing it.
            self._services.__dict__["blob_store_provider"] = blob_store_provider
        self._database_provider = database_provider
        self.extraction_service = (
            self._extraction_service_override
            or ExtractionService(
                job_queue=self.job_queue,
                media_installer_factory=(
                    self._services.validated_media_installer_for_session
                ),
            )
        )
        self.library_conversion_service: LibraryConversionOrchestrator | None = None
        self.library_preview_service: LibraryPreviewOrchestrator | None = None
        # Phase 6a round-robin fan-out (plan §3 D-2 point 2): the workspace_id
        # to try FIRST on the *next* round-robin claim sweep. Lives on the
        # worker instance -- entry.py's `run` loop constructs one OneShotWorker
        # and calls dispatch_once_with_session_factory repeatedly against it --
        # so it survives across polls. Deliberately NOT reset to the front of
        # the roster after a hit: doing so would let a workspace that is always
        # first in id order win every sweep before anything after it is even
        # tried, which is the same starvation drain-then-advance causes, just
        # at sweep-start granularity instead of within-sweep. See
        # _claim_via_round_robin.
        self._rr_cursor_workspace_id: UUID | None = None
        # entry.py installs the SIGTERM/SIGINT controller's interruptible
        # section here, so a shutdown can interrupt the business phase of a
        # claimed job -- and only that phase: claims and status transactions
        # always run to completion.
        self.business_phase_guard: Callable[[], AbstractContextManager[Any]] = (
            nullcontext
        )

    def dispatch_once(
        self,
        session: Session,
        workspace_id: UUID | None = None,
        job_types: set[str] | None = None,
    ) -> WorkerDispatchResult | None:
        """Legacy single-phase dispatch: the claim and the business logic
        share the caller's transaction. Production entrypoints use the
        two-phase ``dispatch_once_with_session_factory``; here the business
        handler runs inside a savepoint so a failure that poisons its flush
        (e.g. an IntegrityError) rolls back to the savepoint instead of
        reverting the claim - the failure bookkeeping below still advances
        retry_count rather than looping on the same poison job forever.

        The claim UPDATE owns the Job row for this transaction before a
        conversion handler locks its domain lineage, preserving the shared
        Job, item, version, run, attempt order on PostgreSQL.
        """
        job = self.job_queue.claim_next(
            session=session,
            workspace_id=workspace_id,
            job_types=job_types,
        )
        if job is None:
            return None

        try:
            with session.begin_nested():
                return self._dispatch_claimed(session=session, job=job)
        except _TERMINAL_JOB_ERRORS as exc:
            status = self._fail_claimed(
                session=session,
                job=job,
                error=str(exc),
                max_retries=0,
            )
            if status == "queued":
                self._reset_requeued_domain_state(session, job)
            if status == "failed":
                self._apply_failure_markers(
                    session=session,
                    job=job,
                    error=str(exc),
                    manual_retryable=False,
                )
                self._notify_terminal_failure(session=session, job=job, error=str(exc))
            return WorkerDispatchResult(
                job_id=job.id,
                job_type=job.job_type,
                status=status,
                detail={"error": str(exc), "terminal": True},
            )
        except Exception as exc:
            status = self._fail_claimed(
                session=session,
                job=job,
                error=str(exc),
                max_retries=DEFAULT_JOB_MAX_ATTEMPTS,
            )
            if status == "queued":
                self._reset_requeued_domain_state(session, job)
            if status == "failed":
                self._apply_failure_markers(
                    session=session,
                    job=job,
                    error=str(exc),
                    manual_retryable=True,
                )
                self._notify_terminal_failure(session=session, job=job, error=str(exc))
            return WorkerDispatchResult(
                job_id=job.id,
                job_type=job.job_type,
                status=status,
                detail={"error": str(exc)},
            )

    def _fail_claimed(
        self,
        session: Session,
        job: Job,
        error: str,
        max_retries: int,
    ) -> str:
        """Record a claimed job's failure on the caller's session, unless the
        business handler already owned the failed/queued transition. Returns
        the resulting job status."""
        if job.status not in {"failed", "queued"}:
            self.job_queue.fail(
                session=session,
                job=job,
                error=error,
                max_retries=max_retries,
            )
        return job.status

    def _apply_failure_markers(
        self,
        session: Session,
        job: Job,
        error: str,
        *,
        manual_retryable: bool,
    ) -> None:
        """Single-phase variant of ``_apply_failure_markers_isolated``: the
        business savepoint rollback discarded any failure markers the handler
        wrote, so re-apply them once the job is terminally failed. Runs in its
        own savepoint so a marker failure cannot poison the caller's
        transaction or mask the dispatch outcome."""
        handler = get_handler(job.job_type)
        if handler is None or handler.on_failure is None:
            return
        try:
            with session.begin_nested():
                if job.job_type == "library_conversion":
                    handler.on_failure(
                        self,
                        session,
                        job,
                        error,
                        manual_retryable=manual_retryable,
                    )
                else:
                    handler.on_failure(self, session, job, error)
        except Exception:
            logger.exception(
                "failed to persist domain failure markers for job %s",
                job.id,
            )

    def _notify_terminal_failure(self, session: Session, job: Job, error: str) -> None:
        """Queue the secondary email notification for a job that just went
        terminally failed, in its own savepoint so a notification failure can
        never mask or roll back the job's own failure bookkeeping (same
        isolation shape as _apply_failure_markers)."""
        try:
            with session.begin_nested():
                self._email_delivery_service().queue_job_failure_notifications(
                    session=session,
                    workspace_id=job.workspace_id,
                    job_id=job.id,
                    job_type=job.job_type,
                    error=error,
                )
        except Exception:
            logger.exception(
                "failed to queue terminal-failure notification for job %s", job.id
            )

    def _notify_terminal_failure_isolated(
        self,
        session_factory: sessionmaker,
        workspace_id: UUID,
        job_id: UUID,
        job_type: str,
        error: str,
    ) -> None:
        """Two-phase-dispatch variant of ``_notify_terminal_failure``: runs in
        its own transaction (mirrors _apply_failure_markers_isolated) so it
        cannot ride on -- or poison -- the transaction that already committed
        the job's failed status."""
        try:
            with session_factory.begin() as notify_session:
                self._email_delivery_service().queue_job_failure_notifications(
                    session=notify_session,
                    workspace_id=workspace_id,
                    job_id=job_id,
                    job_type=job_type,
                    error=error,
                )
        except Exception:
            logger.exception(
                "failed to queue terminal-failure notification for job %s", job_id
            )

    def reclaim_stale_jobs_with_session_factory(
        self,
        session_factory: sessionmaker,
        lease_timeout_seconds: int,
    ) -> int:
        """Best-effort periodic recovery; preserve the reclaimed-count API."""
        return self.recover_initial_stale_jobs_with_session_factory(
            session_factory=session_factory,
            lease_timeout_seconds=lease_timeout_seconds,
        ).reclaimed_count

    def recover_initial_stale_jobs_with_session_factory(
        self,
        session_factory: sessionmaker,
        lease_timeout_seconds: int,
    ) -> InitialRecoveryResult:
        """Requeue (or terminally fail) every workspace's jobs stuck in
        'running' past their lease.

        Phase 6a (plan §3 D-2 point 1): ``JobQueueService.reclaim_stale_jobs``
        now requires a ``workspace_id`` -- there is no single query that can
        answer "every stale job anywhere" once ``Job`` lives on
        per-workspace planes, and the pre-6a version that tried (no filter at
        all) is exactly how a crashed worker's jobs went unreclaimed forever
        after sharding: silently, with no error to notice. So this reads the
        workspace roster from the control plane (``Workspace`` is
        identity-plane data, D-2/D-4) and then reclaims each workspace's
        stale jobs in its own transaction on ``session_for(workspace_id)``.

        Unlike ``_claim_via_round_robin``, this always visits every
        workspace on every call -- there is no "stop at the first hit", so
        there is no cross-sweep starvation to guard against here (it reclaims
        ALL matching jobs in every workspace, not one). The only fairness
        concern is a broken workspace: one workspace's reclaim raising (its
        DB unreachable, a bad connection, etc.) must not abort the rest of
        the sweep, so each workspace's reclaim is wrapped individually and a
        failure is logged and skipped rather than raised -- unreclaimed
        stale jobs in that one workspace wait for the next periodic sweep
        instead of blocking every other workspace's reclaim this round.

        ``session_factory`` is the pre-fan-out contract's factory, used
        as-is for both the roster read and every workspace's reclaim when no
        ``self._database_provider`` is injected (unit tests exercising a
        single un-sharded DB) -- unchanged from before Phase 6a in that case.

        Report whether the required roster pass ran, and which workspaces
        were deferred. The periodic wrapper keeps its existing integer API.
        """
        try:
            control_session_factory = (
                self._database_provider.control_session_factory()
                if self._database_provider is not None
                else session_factory
            )
            with control_session_factory.begin() as control_session:
                workspace_ids = self._workspace_roster(control_session)
        except Exception:
            logger.exception("stale-job reclaim failed reading workspace roster")
            return InitialRecoveryResult(roster_available=False)

        total_reclaimed = 0
        failed_workspace_ids = []
        for workspace_id in workspace_ids:
            try:
                workspace_session_factory = (
                    self._database_provider.session_for(workspace_id)
                    if self._database_provider is not None
                    else session_factory
                )
                with workspace_session_factory.begin() as session:
                    reclaimed = self.job_queue.reclaim_stale_jobs(
                        session=session,
                        workspace_id=workspace_id,
                        lease_timeout_seconds=lease_timeout_seconds,
                    )
                    for job in reclaimed:
                        error = job.last_error or "worker lease expired"
                        if job.status == "failed":
                            # Domain failure markers are workspace-plane
                            # child-table writes (D-3) -- stay on this
                            # workspace's own session.
                            self._apply_failure_markers(
                                session,
                                job,
                                error,
                                manual_retryable=True,
                            )
                        elif job.status == "queued":
                            self._reset_requeued_domain_state(session, job)
                        logger.warning(
                            "reclaimed stale job %s (job_type=%s) -> %s "
                            "(retry_count=%d)",
                            job.id,
                            job.job_type,
                            job.status,
                            job.retry_count,
                        )
                    total_reclaimed += len(reclaimed)
            except Exception:
                failed_workspace_ids.append(workspace_id)
                logger.exception(
                    "stale-job reclaim failed for workspace %s; continuing "
                    "the sweep for the remaining workspaces",
                    workspace_id,
                )
                continue
            # The failure-notification email reads User/WorkspaceMembership
            # (identity-plane, D-4) and must not ride on the workspace
            # session above -- unlike _apply_failure_markers, it cannot be
            # nested inside that transaction. Its own isolated transaction on
            # the control plane, same as the two-phase dispatcher's failure
            # path (_notify_terminal_failure_isolated).
            for job in reclaimed:
                if job.status != "failed":
                    continue
                self._notify_terminal_failure_isolated(
                    session_factory=control_session_factory,
                    workspace_id=job.workspace_id,
                    job_id=job.id,
                    job_type=job.job_type,
                    error=job.last_error or "worker lease expired",
                )
        return InitialRecoveryResult(
            roster_available=True,
            reclaimed_count=total_reclaimed,
            failed_workspace_ids=tuple(failed_workspace_ids),
        )

    def _release_interrupted_claim(
        self,
        claimed_job: _ClaimedJob,
        *,
        session_factory: sessionmaker,
    ) -> bool:
        """Give an interrupted claim back to the queue instead of letting it age.

        Without this a job claimed by a worker that is being stopped stays
        ``running`` until its lease expires (``worker_lease_timeout_seconds``),
        and only then does a reclaim sweep requeue it -- charging it a retry it
        never used. The update is guarded by the claim's own lease stamp, so a
        job that completed, failed, or was reclaimed meanwhile is left alone.
        Domain state follows the same reset a stale-lease reclaim applies.
        """

        try:
            with session_factory.begin() as session:
                acquire_write_intent(session)
                job = get_in_workspace(
                    session,
                    Job,
                    workspace_id=claimed_job.workspace_id,
                    resource_id=claimed_job.id,
                )
                if (
                    job is None
                    or job.status != "running"
                    or job.started_at != claimed_job.started_at
                ):
                    return False
                job.status = "queued"
                job.stage = None
                job.started_at = None
                job.next_attempt_at = None
                job.last_error = "requeued after the worker was stopped"
                session.flush()
                self._reset_requeued_domain_state(session, job)
        except Exception:
            logger.exception(
                "could not release job %s on worker shutdown; it is reclaimed "
                "when its lease expires",
                claimed_job.id,
            )
            return False
        logger.warning(
            "released job %s (job_type=%s) back to the queue on worker shutdown",
            claimed_job.id,
            claimed_job.job_type,
        )
        return True

    def _workspace_roster(self, control_session: Session) -> list[UUID]:
        """Every workspace's id, in a deterministic order, read from the
        control plane. Workspace is identity-plane data (plan §3 D-2/D-4) --
        this is the one query the fan-out mechanisms below are allowed to run
        against ``control_session_factory()``; everything else about a job
        runs on ``session_for(workspace_id)``."""
        return list(
            control_session.scalars(select(Workspace.id).order_by(Workspace.id))
        )

    def _claim_via_hint(
        self,
        control_session_factory: sessionmaker,
        job_types: set[str] | None,
    ) -> _ClaimedJob | None:
        """Advisory fast path (Phase 6b, plan §3 D-2): try the ONE hinted
        workspace with the oldest still-pending hint before the caller falls
        back to the mandatory ``_claim_via_round_robin`` sweep.

        A hint is a pure latency optimization -- see backend/services/
        job_hints.py's module docstring. This method:

        * Reads at most one hint per call (FIFO: oldest ``hinted_at``
          first), so it cannot itself starve other hinted workspaces the
          way an unbounded "drain every hint" loop would.
        * ALWAYS clears the hint it read -- via ``clear_hint_if_not_
          refreshed``'s compare-and-clear, so a hint re-written
          concurrently survives -- regardless of hit, miss, or error. This
          mirrors ``_claim_via_round_robin``'s "advance regardless of
          outcome" contract: a workspace that keeps getting re-hinted keeps
          winning the fast path, one that stops getting hinted falls back
          to being swept only by the round-robin/periodic-full-sweep
          backstop below -- exactly D-2's "hints decide order, not whether
          a workspace gets its turn".
        * Returns ``None`` -- telling the caller to fall through to
          ``_claim_via_round_robin`` -- on: no hint at all, the hinted
          workspace's claim finding nothing (a stale hint, or another
          worker already took the job -- this method cannot and does not
          need to tell the two apart, since both mean "nothing for the
          fast path to hand back right now"), or the hinted workspace being
          unreachable. It never raises.
        """
        try:
            with control_session_factory.begin() as control_session:
                hint = peek_oldest_hinted_workspace(control_session)
        except Exception:
            logger.exception(
                "job hint lookup failed; falling back to round-robin sweep"
            )
            return None
        if hint is None:
            return None
        workspace_id, observed_hinted_at = hint

        job: Job | None = None
        try:
            workspace_session_factory = self._database_provider.session_for(
                workspace_id
            )
            with workspace_session_factory.begin() as claim_session:
                job = self.job_queue.claim_next(
                    session=claim_session,
                    workspace_id=workspace_id,
                    job_types=job_types,
                )
        except Exception:
            logger.exception(
                "claim_next failed for hinted workspace %s; falling back to "
                "round-robin sweep",
                workspace_id,
            )
            job = None
        finally:
            # Clear regardless of hit/miss/error -- see docstring above.
            # Runs in its own control-plane transaction; a failure here just
            # leaves a stale hint sitting around (cost: one more wasted
            # fast-path attempt on a future call, same as a hint that was
            # never cleared), never a correctness problem -- nothing reads
            # this table to decide whether a workspace gets swept.
            try:
                with control_session_factory.begin() as control_session:
                    clear_hint_if_not_refreshed(
                        control_session,
                        workspace_id=workspace_id,
                        observed_hinted_at=observed_hinted_at,
                    )
            except Exception:
                logger.exception(
                    "failed to clear job hint for workspace %s", workspace_id
                )

        if job is None:
            return None
        return _ClaimedJob.from_model(job)

    def _claim_via_round_robin(
        self,
        control_session_factory: sessionmaker,
        job_types: set[str] | None,
    ) -> _ClaimedJob | None:
        """Fan-out claim across every workspace's plane (Phase 6a, plan §3
        D-2 point 2), used when the caller did not name a workspace.

        Strategy is round robin, not drain-then-advance: each workspace gets
        exactly ONE ``claim_next`` attempt this sweep, then the sweep moves
        on regardless of whether that attempt found a job. The natural way to
        write this loop is to keep pulling from a workspace while it has
        work and only move to the next once it is empty -- but a
        continuously-busy workspace then never releases the loop, and every
        workspace after it in roster order is starved indefinitely. One
        attempt each bounds that: a workspace can be skipped for at most one
        sweep's worth of *other* workspaces' single attempts before its own
        turn comes back around.

        ``self._rr_cursor_workspace_id`` is where this sweep starts (the
        roster is rotated so that workspace is tried first), and it is
        updated after every attempt in the sweep -- hit, miss, or
        error -- to the workspace immediately after whichever one was just
        tried. That means the position where this call stops (a hit, or the
        end of a full miss) is exactly where the *next* call starts. Without
        this, every sweep would restart at roster[0], and if that workspace
        is continuously busy, a call that stops at the first hit (this
        method returns as soon as one is found) would never even reach the
        rest of the roster across any number of calls -- the same
        starvation the round-robin sweep exists to prevent, just moved from
        "within a sweep" to "across sweeps".

        A workspace whose session cannot be resolved or whose claim raises
        (D-2's "unreachable workspace mid-sweep") is logged and skipped, not
        allowed to abort the sweep -- the cursor still advances past it so it
        is retried on a later sweep instead of wedging every sweep at the
        same spot.

        Cost: a full sweep is up to N round trips (one claim_next per
        workspace) before concluding "idle", where N tracks subscriber count
        -- every user gets a personal workspace (plan §3 D-2 "규모 감각") --
        not organization count. Pre-fan-out this was a single claim_next
        answering "is there work anywhere"; at hundreds of workspaces a full
        sweep approaches today's ~1s poll interval, and at thousands it can
        take noticeably longer to notice a new job than a poll tick. 6b's
        advisory hint (not built in this phase) is what brings that back
        down; 6a is required to be correct without it.
        """
        with control_session_factory.begin() as control_session:
            roster = self._workspace_roster(control_session)
        if not roster:
            return None
        order = self._rotate_roster(roster)
        for position, workspace_id in enumerate(order):
            try:
                workspace_session_factory = self._database_provider.session_for(
                    workspace_id
                )
                with workspace_session_factory.begin() as claim_session:
                    job = self.job_queue.claim_next(
                        session=claim_session,
                        workspace_id=workspace_id,
                        job_types=job_types,
                    )
            except Exception:
                logger.exception(
                    "claim_next failed for workspace %s during round-robin "
                    "sweep; skipping to the next workspace",
                    workspace_id,
                )
                job = None
            finally:
                # Advance regardless of outcome (hit, miss, or error) -- see
                # docstring -- before deciding below whether this attempt's
                # result ends the sweep.
                self._rr_cursor_workspace_id = order[(position + 1) % len(order)]
            if job is not None:
                return _ClaimedJob.from_model(job)
        return None

    def _rotate_roster(self, roster: list[UUID]) -> list[UUID]:
        """Rotate ``roster`` so it starts at ``self._rr_cursor_workspace_id``.

        Falls back to the roster's natural (id) order when the cursor is
        unset (first call) or names a workspace no longer in the roster (it
        was deleted between sweeps, plan §3 D-2's "workspace deleted mid-
        sweep" concern) -- restarting from the front rather than raising.
        """
        cursor = self._rr_cursor_workspace_id
        if cursor is None:
            return roster
        try:
            start = roster.index(cursor)
        except ValueError:
            return roster
        return roster[start:] + roster[:start]

    def _reset_requeued_domain_state(self, session: Session, job: Job) -> None:
        """Keep a retryable job's domain run aligned with the queue state.

        Callers have already mutated or locked ``job``. Conversion and Preview
        recovery therefore follow the shared PostgreSQL order Job, item,
        version, run, attempt. Manual Library conversion retry must use this
        same order; taking the item first would deadlock against legacy
        dispatch, stale-lease reclaim, or isolated failure recovery.
        """

        if job.job_type == "library_preview":
            self._reset_requeued_preview_state(session, job)
            return
        if job.job_type == "library_conversion":
            raw_attempt_id = job.target_ref.get("library_conversion_attempt_id")
            raw_generation = job.target_ref.get("execution_generation")
            if (
                raw_attempt_id is None
                or isinstance(raw_generation, bool)
                or not isinstance(raw_generation, int)
            ):
                job.status = "failed"
                return
            try:
                attempt_id = UUID(str(raw_attempt_id))
            except ValueError:
                job.status = "failed"
                return
            seed_attempt = get_in_workspace(
                session,
                LibraryAttempt,
                workspace_id=job.workspace_id,
                resource_id=attempt_id,
            )
            if seed_attempt is None or seed_attempt.stage != "conversion":
                job.status = "failed"
                return
            seed_run = get_in_workspace(
                session,
                LibraryRun,
                workspace_id=job.workspace_id,
                resource_id=seed_attempt.run_id,
            )
            if seed_run is not None and seed_run.stage != "conversion":
                seed_run = None
            seed_version = get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=job.workspace_id,
                resource_id=seed_attempt.library_item_version_id,
            )
            if seed_run is None or seed_version is None:
                job.status = "failed"
                return

            # The Job is already owned by every caller. Continue the global
            # queue/domain order through the four lineage rows. A stale worker
            # may be finishing while its lease is reclaimed; refreshing them
            # prevents reclaim from resurrecting a completion that won.
            acquire_write_intent(session)
            item = session.scalar(
                select(LibraryItem)
                .where(
                    LibraryItem.workspace_id == job.workspace_id,
                    LibraryItem.id == seed_version.library_item_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            version = session.scalar(
                select(LibraryItemVersion)
                .where(
                    LibraryItemVersion.workspace_id == job.workspace_id,
                    LibraryItemVersion.id == seed_version.id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            run = session.scalar(
                select(LibraryRun)
                .where(
                    LibraryRun.workspace_id == job.workspace_id,
                    LibraryRun.id == seed_run.id,
                    LibraryRun.stage == "conversion",
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            attempt = session.scalar(
                select(LibraryAttempt)
                .where(
                    LibraryAttempt.workspace_id == job.workspace_id,
                    LibraryAttempt.id == attempt_id,
                    LibraryAttempt.stage == "conversion",
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                item is None
                or version is None
                or run is None
                or attempt is None
                or item.workspace_id != job.workspace_id
                or version.library_item_id != item.id
                or run.library_item_version_id != version.id
                or attempt.library_item_version_id != version.id
                or attempt.run_id != run.id
            ):
                job.status = "failed"
                return
            if attempt.status in {
                "completed",
                "failed",
                "timed_out",
                "cancelled",
                "unsupported",
            }:
                self.job_queue.complete(session=session, job=job)
                return
            if attempt.execution_generation != raw_generation:
                job.status = "failed"
                return
            attempt.execution_generation += 1
            attempt.transport_retry_count += 1
            attempt.status = "queued"
            selected_attempt = (
                get_in_workspace(
                    session,
                    LibraryAttempt,
                    workspace_id=job.workspace_id,
                    resource_id=version.selected_conversion_attempt_id,
                )
                if version.selected_conversion_attempt_id is not None
                else None
            )
            if selected_attempt is not None and selected_attempt.stage != "conversion":
                selected_attempt = None
            selected_run = (
                get_in_workspace(
                    session,
                    LibraryRun,
                    workspace_id=job.workspace_id,
                    resource_id=selected_attempt.run_id,
                )
                if selected_attempt is not None
                else None
            )
            if selected_run is not None and selected_run.stage != "conversion":
                selected_run = None
            superseded = (
                selected_attempt is not None
                and selected_attempt.id != attempt.id
                and selected_run is not None
                and selected_run.run_seq >= run.run_seq
            )
            if not superseded:
                run.status = "queued"
                run.finished_at = None
                version.processing_status = "queued"
            target_ref = dict(job.target_ref)
            target_ref["execution_generation"] = attempt.execution_generation
            job.target_ref = target_ref
            job.idempotency_key = f"{attempt.id}:{attempt.execution_generation}"
            session.add_all([attempt, run, version, job])
            return
        if job.job_type != "lint_tick":
            return
        raw_run_id = job.target_ref.get("lint_run_id")
        if raw_run_id is None:
            return
        try:
            run_id = UUID(str(raw_run_id))
        except ValueError:
            return
        lint_run = get_in_workspace(
            session,
            LintRun,
            workspace_id=job.workspace_id,
            resource_id=run_id,
        )
        if lint_run is None or lint_run.status == "completed":
            return
        lint_run.status = "queued"
        lint_run.started_at = None
        lint_run.completed_at = None

    def _reset_requeued_preview_state(self, session: Session, job: Job) -> None:
        raw_attempt_id = job.target_ref.get("library_preview_attempt_id")
        raw_generation = job.target_ref.get("execution_generation")
        if (
            raw_attempt_id is None
            or isinstance(raw_generation, bool)
            or not isinstance(raw_generation, int)
        ):
            job.status = "failed"
            return
        try:
            attempt_id = UUID(str(raw_attempt_id))
        except ValueError:
            job.status = "failed"
            return
        seed_attempt = get_in_workspace(
            session,
            LibraryAttempt,
            workspace_id=job.workspace_id,
            resource_id=attempt_id,
        )
        if seed_attempt is not None and seed_attempt.stage != "preview_generation":
            seed_attempt = None
        seed_run = (
            get_in_workspace(
                session,
                LibraryRun,
                workspace_id=job.workspace_id,
                resource_id=seed_attempt.run_id,
            )
            if seed_attempt is not None
            else None
        )
        if seed_run is not None and seed_run.stage != "preview_generation":
            seed_run = None
        seed_version = (
            get_in_workspace(
                session,
                LibraryItemVersion,
                workspace_id=job.workspace_id,
                resource_id=seed_attempt.library_item_version_id,
            )
            if seed_attempt is not None
            else None
        )
        if seed_attempt is None or seed_run is None or seed_version is None:
            job.status = "failed"
            return
        acquire_write_intent(session)
        item = session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.workspace_id == job.workspace_id,
                LibraryItem.id == seed_version.library_item_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        version = session.scalar(
            select(LibraryItemVersion)
            .where(
                LibraryItemVersion.workspace_id == job.workspace_id,
                LibraryItemVersion.id == seed_version.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        run = session.scalar(
            select(LibraryRun)
            .where(
                LibraryRun.workspace_id == job.workspace_id,
                LibraryRun.id == seed_run.id,
                LibraryRun.stage == "preview_generation",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        attempt = session.scalar(
            select(LibraryAttempt)
            .where(
                LibraryAttempt.workspace_id == job.workspace_id,
                LibraryAttempt.id == attempt_id,
                LibraryAttempt.stage == "preview_generation",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            item is None
            or version is None
            or run is None
            or attempt is None
            or item.workspace_id != job.workspace_id
            or version.library_item_id != item.id
            or run.library_item_version_id != version.id
            or attempt.library_item_version_id != version.id
            or attempt.run_id != run.id
        ):
            job.status = "failed"
            return
        if attempt.status in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "unsupported",
        }:
            self.job_queue.complete(session=session, job=job)
            return
        if attempt.execution_generation != raw_generation:
            job.status = "failed"
            return
        attempt.execution_generation += 1
        attempt.transport_retry_count += 1
        attempt.status = "queued"
        attempt.finished_at = None
        run.status = "queued"
        run.finished_at = None
        if version.selected_preview_attempt_id is None:
            version.preview_status = "queued"
        target_ref = dict(job.target_ref)
        target_ref["execution_generation"] = attempt.execution_generation
        job.target_ref = target_ref
        job.idempotency_key = f"{attempt.id}:{attempt.execution_generation}"
        session.add_all([attempt, run, version, job])

    def dispatch_once_with_session_factory(
        self,
        session_factory: sessionmaker,
        workspace_id: UUID | None = None,
        job_types: set[str] | None = None,
        bypass_hint: bool = False,
    ) -> WorkerDispatchResult | None:
        """Two-phase dispatch (BIZ-H-4): business work and job status updates run
        in separate transactions so a commit-time failure on the status update
        cannot roll back already-applied business writes (or vice versa).

        Phase 0: claim a job (own transaction).
        Phase 0.5: commit conversion running/converting domain markers.
        Phase 1: run the business handler (own transaction); on failure mark
                 the job failed in a *separate* transaction so the fail update
                 survives even if phase 1 was rolled back.
        Phase 2: mark the job completed in its own transaction. If phase 2
                 fails, phase 1 writes are still durable - we log and surface
                 the partial-success state to the caller for reconciliation.

        Phase 6a (plan §3 D-2 point 3): ``Job`` is workspace-plane data, so
        claim, both failure branches, and completion all run on
        ``session_for(workspace_id)`` now, not ``control_session_factory()``
        -- the control plane is used ONLY to resolve *which* workspace to
        claim from, either directly (``workspace_id`` given) or via a
        round-robin fan-out across the whole roster (``workspace_id`` is
        None; see ``_claim_via_round_robin``). The one exception is the
        terminal-failure email notification, which reads User/
        WorkspaceMembership (identity-plane, D-4) and stays on the control
        plane in its own isolated transaction, same as before.

        Phase 6b (plan §3 D-2 point 2, advisory hint): when no explicit
        ``workspace_id`` is given, the fan-out tries ``_claim_via_hint``
        FIRST and only falls back to the mandatory ``_claim_via_round_robin``
        sweep when the hint path returns nothing (no hint, a stale hint, or
        an error) -- so a dropped/never-written hint costs exactly one
        extra round-robin fan-out's worth of latency on THIS call and
        nothing else; round robin is still what actually finds the job.
        ``bypass_hint=True`` (worker/entry.py's periodic full-sweep timer)
        skips the hint path entirely and goes straight to round robin, so
        that guarantee holds even in the degenerate case where the hint path
        happens to keep hitting on OTHER workspaces every single call and an
        un-hinted (or hint-cleared) workspace's job would otherwise only ever
        be reached by the fall-through path.
        """
        control_session_factory = (
            self._database_provider.control_session_factory()
            if self._database_provider is not None
            else session_factory
        )

        # Phase 0: claim. An explicit workspace_id claims directly on that
        # workspace's plane (no fan-out needed -- the caller already knows
        # where to look, e.g. `dtxwiki worker run --workspace-id`). With no
        # workspace_id and a real provider, round-robin across every
        # workspace. With no workspace_id and no provider (unit-test
        # convenience / a single un-sharded DB reached only through
        # session_factory), claim directly against session_factory with no
        # workspace filter -- the pre-fan-out behavior, unchanged.
        if workspace_id is not None:
            claim_factory = (
                self._database_provider.session_for(workspace_id)
                if self._database_provider is not None
                else session_factory
            )
            with claim_factory.begin() as claim_session:
                job = self.job_queue.claim_next(
                    session=claim_session,
                    workspace_id=workspace_id,
                    job_types=job_types,
                )
                if job is None:
                    return None
                claimed_job = _ClaimedJob.from_model(job)
        elif self._database_provider is not None:
            claimed_job = None
            if not bypass_hint:
                claimed_job = self._claim_via_hint(
                    control_session_factory=control_session_factory,
                    job_types=job_types,
                )
            if claimed_job is None:
                claimed_job = self._claim_via_round_robin(
                    control_session_factory=control_session_factory,
                    job_types=job_types,
                )
            if claimed_job is None:
                return None
        else:
            with session_factory.begin() as claim_session:
                job = self.job_queue.claim_next(
                    session=claim_session,
                    workspace_id=None,
                    job_types=job_types,
                )
                if job is None:
                    return None
                claimed_job = _ClaimedJob.from_model(job)

        job_id = claimed_job.id
        job_type = claimed_job.job_type

        # Phase 1: business logic
        workspace_session_factory = None
        try:
            workspace_session_factory = (
                self._database_provider.session_for(claimed_job.workspace_id)
                if self._database_provider is not None
                else session_factory
            )
            with self.business_phase_guard():
                if claimed_job.job_type == "library_conversion":
                    with workspace_session_factory.begin() as marker_session:
                        self._mark_library_conversion_started(
                            marker_session,
                            claimed_job,
                        )
                elif claimed_job.job_type == "library_preview":
                    with workspace_session_factory.begin() as marker_session:
                        self._mark_library_preview_started(
                            marker_session,
                            claimed_job,
                        )
                conversion_service = (
                    self._library_conversion_service()
                    if claimed_job.job_type == "library_conversion"
                    else None
                )
                if (
                    conversion_service is not None
                    and conversion_service.durable_media_installer_factory is not None
                ):
                    detail = self._business_library_conversion_durable(
                        workspace_session_factory,
                        claimed_job,
                    )
                elif (
                    claimed_job.job_type == "library_preview"
                    and self._library_preview_service().durable_media_installer_factory
                    is not None
                ):
                    detail = self._business_library_preview_durable(
                        workspace_session_factory,
                        claimed_job,
                    )
                elif claimed_job.job_type == "context_media_orphan_sweep":
                    if self._settings is None:
                        raise RuntimeError(
                            "Attachment retention settings are unavailable"
                        )
                    with control_session_factory() as policy_session:
                        policy = read_context_media_policy(
                            policy_session,
                            claimed_job.workspace_id,
                            self._settings.context_media_orphan_retention_hours,
                        )
                    run_at = datetime.now(UTC)
                    with workspace_session_factory.begin() as work_session:
                        detail = self._sweep_context_media(
                            work_session,
                            claimed_job.workspace_id,
                            policy.effective_hours,
                            run_at,
                        )
                else:
                    with workspace_session_factory.begin() as work_session:
                        detail = self._run_business_logic(
                            session=work_session,
                            job=claimed_job,
                        )
                if (
                    claimed_job.job_type == RECONCILIATION_REMINDER_JOB_TYPE
                    and detail.get("unresolved") is True
                ):
                    detail["email_outbox_count"] = (
                        self._queue_reconciliation_reminder_email_isolated(
                            session_factory=control_session_factory,
                            job=claimed_job,
                        )
                    )
        except WorkerShutdownRequested:
            # Not an attempt failure: the job goes back to the queue without
            # consuming retry budget, and the shutdown continues.
            self._release_interrupted_claim(
                claimed_job,
                session_factory=workspace_session_factory or session_factory,
            )
            raise
        except _TERMINAL_JOB_ERRORS as exc:
            logger.exception(
                "job %s business logic failed with terminal error",
                job_id,
            )
            final_status = self._mark_failed_isolated(
                session_factory=workspace_session_factory,
                workspace_id=claimed_job.workspace_id,
                job_id=job_id,
                error=str(exc),
                max_retries=0,
                expected_job=claimed_job,
            )
            if final_status == "failed" and workspace_session_factory is not None:
                self._apply_failure_markers_isolated(
                    session_factory=workspace_session_factory,
                    job=claimed_job,
                    error=str(exc),
                    manual_retryable=False,
                )
            if final_status == "failed":
                self._notify_terminal_failure_isolated(
                    session_factory=control_session_factory,
                    workspace_id=claimed_job.workspace_id,
                    job_id=job_id,
                    job_type=job_type,
                    error=str(exc),
                )
            return WorkerDispatchResult(
                job_id=job_id,
                job_type=job_type,
                status=final_status,
                detail={"error": str(exc), "terminal": True},
            )
        except Exception as exc:
            logger.exception("job %s business logic failed", job_id)
            final_status = self._mark_failed_isolated(
                session_factory=workspace_session_factory,
                workspace_id=claimed_job.workspace_id,
                job_id=job_id,
                error=str(exc),
                max_retries=DEFAULT_JOB_MAX_ATTEMPTS,
                expected_job=claimed_job,
            )
            if final_status == "failed" and workspace_session_factory is not None:
                self._apply_failure_markers_isolated(
                    session_factory=workspace_session_factory,
                    job=claimed_job,
                    error=str(exc),
                    manual_retryable=True,
                )
            if final_status == "failed":
                self._notify_terminal_failure_isolated(
                    session_factory=control_session_factory,
                    workspace_id=claimed_job.workspace_id,
                    job_id=job_id,
                    job_type=job_type,
                    error=str(exc),
                )
            return WorkerDispatchResult(
                job_id=job_id,
                job_type=job_type,
                status=final_status,
                detail={"error": str(exc)},
            )

        # Phase 2: mark complete in its own transaction. If this fails, phase 1
        # writes remain durable; the job row may still read "running" until
        # reconciliation - that is preferable to losing the business writes.
        # workspace_session_factory is always resolved by this point (Phase 1
        # only reaches here without raising, and it is the first thing Phase 1
        # sets).
        try:
            with workspace_session_factory.begin() as status_session:
                if claimed_job.job_type in {"library_conversion", "library_preview"}:
                    acquire_write_intent(status_session)
                    status_job = status_session.scalar(
                        select(Job)
                        .where(Job.id == job_id)
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                else:
                    status_job = get_in_workspace(
                        status_session,
                        Job,
                        workspace_id=claimed_job.workspace_id,
                        resource_id=job_id,
                    )
                if status_job is None:
                    raise RuntimeError(
                        f"job disappeared before completion update: {job_id}"
                    )
                stale_conversion_claim = claimed_job.job_type in {
                    "library_conversion",
                    "library_preview",
                } and (
                    status_job.status not in {"running", "completed"}
                    or not self._same_attempt_generation_claim(status_job, claimed_job)
                )
                if stale_conversion_claim:
                    final_status = "stale"
                elif status_job.status != "completed":
                    self.job_queue.complete(session=status_session, job=status_job)
                    final_status = "completed"
                else:
                    final_status = "completed"
        except Exception:
            logger.exception(
                "job %s business work committed but status update failed; "
                "reconciliation required",
                job_id,
            )
            final_status = "completed_pending_status"

        return WorkerDispatchResult(
            job_id=job_id,
            job_type=job_type,
            status=final_status,
            detail=detail,
        )

    def _run_business_logic(
        self,
        session: Session,
        job: Job | _ClaimedJob,
    ) -> dict[str, Any]:
        """Phase-1 helper for the two-phase dispatch: run the job_type's business
        handler without touching queue state (the orchestrator owns complete/fail).
        Returns the ``detail`` dict for ``WorkerDispatchResult``.
        """
        return self._handler_for(job).business(self, session, job, None)

    def _handler_for(self, job: Job | _ClaimedJob) -> JobHandler:
        handler = get_handler(job.job_type)
        if handler is None:
            raise ValueError(f"unsupported worker job type: {job.job_type}")
        return handler

    def _business_library_discovery(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        workspace = session.get(Workspace, job.workspace_id)
        if workspace is None:
            raise ValueError(f"workspace not found: {job.workspace_id}")
        candidates = [
            LibraryCandidateInput(**candidate)
            for candidate in job.target_ref.get("candidates", [])
        ]
        run = self.library_service.run_discovery(
            session=session,
            workspace=workspace,
            run_token=str(
                job.target_ref.get("run_token") or job.idempotency_key or job.id
            ),
            candidates=candidates,
            job_id=job.id,
        )
        return {
            "library_discovery_run_id": str(run.id),
            "candidate_count": run.candidate_count,
            "accepted_count": run.accepted_count,
            "excluded_count": run.excluded_count,
        }

    def _business_extraction(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        library_item_version_id = self._uuid_target(
            job=job, key="library_item_version_id"
        )
        # queue_job=job (single-phase) lets the service own complete/fail;
        # queue_job=None (two-phase) leaves queue state to the orchestrator.
        artifact = self.extraction_service.extract_library_item_version(
            session=session,
            library_item_version_id=library_item_version_id,
            job=queue_job,
        )
        return {"library_artifact_id": str(artifact.id)}

    def _business_library_conversion(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise ValueError("library conversion job generation is invalid")
        # The legacy one-transaction dispatcher has no phase 0.5. Mark it in
        # the ambient transaction for state-machine consistency; production's
        # two-phase path commits the same marker before converter execution.
        if isinstance(job, Job):
            self._mark_library_conversion_started(session, job)
        result = self._library_conversion_service().execute_attempt(
            session=session,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_conversion_attempt_id",
            ),
            execution_generation=raw_generation,
        )
        return self._library_conversion_detail(result)

    def _business_library_conversion_durable(
        self,
        session_factory: sessionmaker,
        job: _ClaimedJob,
    ) -> dict[str, Any]:
        """Run conversion without an ambient Library writer transaction."""

        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise ValueError("library conversion job generation is invalid")
        result = self._library_conversion_service().execute_attempt_durable(
            session_factory=session_factory,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_conversion_attempt_id",
            ),
            execution_generation=raw_generation,
        )
        return self._library_conversion_detail(result)

    @staticmethod
    def _library_conversion_detail(result) -> dict[str, Any]:
        return {
            "library_conversion_attempt_id": str(result.attempt_id),
            "library_conversion_run_id": str(result.run_id),
            "library_item_version_id": str(result.version_id),
            "conversion_status": result.status,
            "selected": result.selected,
            "next_attempt_id": (
                str(result.next_attempt_id)
                if result.next_attempt_id is not None
                else None
            ),
        }

    def _mark_library_conversion_started(
        self,
        session: Session,
        job: Job | _ClaimedJob,
    ) -> bool:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise ValueError("library conversion job generation is invalid")
        return self._library_conversion_service().mark_attempt_running(
            session=session,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_conversion_attempt_id",
            ),
            execution_generation=raw_generation,
        )

    @staticmethod
    def _same_library_conversion_claim(
        persisted_job: Job,
        claimed_job: Job | _ClaimedJob,
    ) -> bool:
        """Match the immutable attempt/generation identity of one claim.

        Reclaim advances ``execution_generation`` on the same queue row. A
        worker holding the older detached snapshot must therefore be unable to
        complete or fail the row now owned by generation N+1.
        """

        persisted_attempt = persisted_job.target_ref.get(
            "library_conversion_attempt_id"
        )
        claimed_attempt = claimed_job.target_ref.get("library_conversion_attempt_id")
        persisted_generation = persisted_job.target_ref.get("execution_generation")
        claimed_generation = claimed_job.target_ref.get("execution_generation")
        return (
            str(persisted_attempt) == str(claimed_attempt)
            and type(persisted_generation) is type(claimed_generation)
            and persisted_generation == claimed_generation
        )

    def _library_conversion_service(self) -> LibraryConversionOrchestrator:
        if self.library_conversion_service is not None:
            return self.library_conversion_service
        if self._settings is None:
            raise ValueError("library conversion worker requires resolved settings")
        self.library_conversion_service = self._services.library_conversion
        return self.library_conversion_service

    def _business_library_preview(
        self,
        session: Session,
        job: Job | _ClaimedJob,
        queue_job: Job | None,
    ) -> dict[str, Any]:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise ValueError("library Preview job generation is invalid")
        if isinstance(job, Job):
            self._mark_library_preview_started(session, job)
        result = self._library_preview_service().execute_attempt(
            session=session,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_preview_attempt_id",
            ),
            execution_generation=raw_generation,
        )
        return self._library_preview_detail(result)

    def _business_library_preview_durable(
        self,
        session_factory: sessionmaker,
        job: _ClaimedJob,
    ) -> dict[str, Any]:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise ValueError("library Preview job generation is invalid")
        result = self._library_preview_service().execute_attempt_durable(
            session_factory=session_factory,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_preview_attempt_id",
            ),
            execution_generation=raw_generation,
        )
        return self._library_preview_detail(result)

    @staticmethod
    def _library_preview_detail(result) -> dict[str, Any]:
        return {
            "library_preview_attempt_id": str(result.attempt_id),
            "library_preview_run_id": str(result.run_id),
            "library_item_version_id": str(result.version_id),
            "preview_status": result.status,
            "selected": result.selected,
            "next_attempt_id": (
                str(result.next_attempt_id)
                if result.next_attempt_id is not None
                else None
            ),
        }

    def _mark_library_preview_started(
        self,
        session: Session,
        job: Job | _ClaimedJob,
    ) -> bool:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise ValueError("library Preview job generation is invalid")
        return self._library_preview_service().mark_attempt_running(
            session=session,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_preview_attempt_id",
            ),
            execution_generation=raw_generation,
        )

    @staticmethod
    def _same_attempt_generation_claim(
        persisted_job: Job,
        claimed_job: Job | _ClaimedJob,
    ) -> bool:
        if claimed_job.job_type == "library_conversion":
            key = "library_conversion_attempt_id"
        elif claimed_job.job_type == "library_preview":
            key = "library_preview_attempt_id"
        else:
            return True
        persisted_attempt = persisted_job.target_ref.get(key)
        claimed_attempt = claimed_job.target_ref.get(key)
        persisted_generation = persisted_job.target_ref.get("execution_generation")
        claimed_generation = claimed_job.target_ref.get("execution_generation")
        return (
            str(persisted_attempt) == str(claimed_attempt)
            and type(persisted_generation) is type(claimed_generation)
            and persisted_generation == claimed_generation
        )

    def _library_preview_service(self) -> LibraryPreviewOrchestrator:
        if self.library_preview_service is not None:
            return self.library_preview_service
        if self._settings is None:
            raise ValueError("library Preview worker requires resolved settings")
        self.library_preview_service = self._services.library_preview_orchestrator
        return self.library_preview_service

    def _business_feedback(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        feedback_item_id = self._uuid_target(job=job, key="target_id")
        result = self.feedback_resolver.resolve(
            session=session,
            workspace_id=job.workspace_id,
            feedback_item_id=feedback_item_id,
            actor_id=str(SYSTEM_FEEDBACK_RESOLVER_ACTOR_ID),
        )
        return {
            "feedback_item_id": str(result.feedback_item_id),
            "feedback_status": result.feedback_status,
            "audit_event_id": str(result.audit_event_id),
        }

    def _business_bundle(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        target_workspace = job.target_ref.get("workspace_id")
        if target_workspace is not None:
            if UUID(str(target_workspace)) != job.workspace_id:
                raise ValueError(
                    "job target_ref workspace_id does not match job.workspace_id"
                )
        result = self.bundle_expiration_service.sweep_expired(
            session=session,
            workspace_id=job.workspace_id,
            now=self._datetime_target(job=job, key="minute_bucket"),
            job=queue_job,
        )
        return {
            "expired_count": result.expired_count,
            "inspected_count": result.inspected_count,
        }

    def _business_embed(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        indexer = self._embed_indexer()
        if indexer is None:
            return {"skipped": "embedding_provider=none"}
        source_kind = job.target_ref.get("kind")
        raw_id = job.target_ref.get("id")
        if not source_kind or raw_id is None:
            # A malformed embed job (missing kind/id) can never index anything;
            # retrying cannot help. Raise a terminal ValueError so it fails
            # visibly instead of silently completing as a skip and leaving the
            # document unindexed with clean job health (Codex F3 / merged I3).
            raise ValueError(
                "embed job target_ref is missing kind or id "
                f"(kind={source_kind!r}, id={raw_id!r})"
            )
        source_id = UUID(str(raw_id))
        if isinstance(job, Job):
            ambient = getattr(indexer, "upsert_in_session", None)
            if ambient is None:
                raise RuntimeError(
                    "single-phase embed indexing requires an ambient-session bridge"
                )
            ambient(
                session,
                workspace_id=job.workspace_id,
                source_kind=str(source_kind),
                source_id=source_id,
            )
        else:
            indexer.upsert(
                workspace_id=job.workspace_id,
                source_kind=str(source_kind),
                source_id=source_id,
            )
        detail: dict[str, Any] = {
            "indexed_kind": str(source_kind),
            "indexed_id": str(raw_id),
        }
        # Name the document in the worker's per-job output (entry.py echoes
        # `to_payload()` as JSON) so an operator who repairs the configuration
        # can see which documents were indexed symbolically only. The job still
        # completes -- retrying cannot fix a settings mistake -- and without
        # this line a misconfigured provider was indistinguishable from a
        # deliberate `embedding_provider=none`.
        degraded_reason = getattr(indexer, "degraded_reason", None)
        if degraded_reason:
            detail["semantic_skipped"] = degraded_reason
        return detail

    def _business_lint_tick(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        run_id = job.target_ref.get("lint_run_id")
        result = self._services.lint.execute_queued_run(
            session,
            workspace_id=job.workspace_id,
            run_id=UUID(str(run_id)) if run_id else None,
            trigger_type=job.trigger_type,
            actor_kind=str(job.target_ref.get("actor_kind") or "system"),
            actor_id=str(job.target_ref.get("actor_id") or "lint-worker"),
        )
        return {"lint_run_id": str(result.id), "status": result.status}

    def _business_trail_tick(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        from backend.services.trail_analysis import run_trail_tick

        return run_trail_tick(
            session,
            job.workspace_id,
            now=self._datetime_target(job=job, key="scheduled_for"),
        )

    def _business_librarian_tick(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        from backend.services.librarian import run_librarian_tick

        return run_librarian_tick(
            session,
            job.workspace_id,
            embedding_provider=(
                self._settings.embedding_provider if self._settings else "none"
            ),
            embedding_model=(
                self._settings.embedding_model if self._settings else None
            ),
            now=self._datetime_target(job=job, key="scheduled_for"),
        )

    def _business_media_install_reconcile(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        del queue_job
        result = self._media_install_reconciler().sweep_expired(
            session,
            workspace_id=job.workspace_id,
            now=self._datetime_target(job=job, key="scheduled_for"),
        )
        return {
            "inspected_count": result.inspected_count,
            "resumed_count": result.resumed_count,
            "cleaned_count": result.cleaned_count,
            "deferred_count": result.deferred_count,
        }

    def _media_install_reconciler(self) -> _MediaInstallReconciler:
        if self.media_install_reconciler is not None:
            return self.media_install_reconciler
        settings = self._settings or (
            self._database_provider.settings
            if self._database_provider is not None
            else None
        )
        if settings is None:
            raise ValueError("media reconciliation worker requires resolved settings")
        self.media_install_reconciler = self._services.media_install_reconciler
        return self.media_install_reconciler

    def _business_credential_replica_reconcile(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        """P10.3 T4.6 (plan section 2.6's reconciler row, regression R12).

        Deliberately does not touch the handler's own ``session``. The
        reconcile reads and writes BOTH planes -- the control index
        (ApiTokenWorkspace/OAuthConsent) and this workspace's replica set --
        so it has to route through the provider rather than through whichever
        single session this handler happens to be handed; and leaving the
        outer work session with no SQL issued on it means it never checks out
        a connection, which is what keeps the reconcile's own short
        transactions from self-locking file SQLite (the same reason
        ``_business_media_blob_gc`` above requires two-phase dispatch).

        No ``ActorContext`` is constructed: the worker opens sessions
        directly, as every other handler here does, and the reconciler's
        audit events carry ``actor_kind="system"``.
        """

        del session, queue_job
        from backend.services.credential_replica_service import (
            reconcile_workspace_credential_replicas,
        )

        if isinstance(job, Job):
            raise RuntimeError(
                "credential_replica_reconcile_requires_two_phase_dispatch"
            )
        if self._database_provider is None:
            raise RuntimeError(
                "credential_replica_reconcile_requires_database_provider"
            )
        result = reconcile_workspace_credential_replicas(
            self._database_provider,
            workspace_id=job.workspace_id,
        )
        return {
            "index_row_count": result.index_row_count,
            "replica_count": result.replica_count,
            "reapplied_count": result.reapplied_count,
            "not_ready_count": result.not_ready_count,
            "orphan_tombstoned_count": result.orphan_tombstoned_count,
        }

    def _business_context_media_orphan_sweep(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        # Legacy single-DB dispatch only. Production resolves CONTROL before
        # opening the workspace mutation transaction in the orchestrator.
        del queue_job
        if self._settings is None:
            raise RuntimeError("Attachment retention settings are unavailable")
        if self._database_provider is not None:
            with self._database_provider.control_session_factory()() as control_session:
                policy = read_context_media_policy(
                    control_session,
                    job.workspace_id,
                    self._settings.context_media_orphan_retention_hours,
                )
        else:
            policy = read_context_media_policy(
                session,
                job.workspace_id,
                self._settings.context_media_orphan_retention_hours,
            )
        return self._sweep_context_media(
            session, job.workspace_id, policy.effective_hours, datetime.now(UTC)
        )

    def _sweep_context_media(
        self, session: Session, workspace_id: UUID, hours: int, now: datetime
    ) -> dict[str, Any]:
        result = self._context_media_orphan_sweep_service().sweep(
            session,
            workspace_id=workspace_id,
            retention_hours=hours,
            now=now,
        )
        return {
            "inspected_count": result.inspected_count,
            "reclaimed_count": result.reclaimed_count,
            "drained_count": result.drained_count,
        }

    def _business_media_blob_gc(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        # Deliberately do not touch the handler's outer session.  Production
        # dispatch uses the two-phase worker: its work-session context has not
        # issued SQL, while the GC application service opens and commits
        # AVAILABLE->GC_PENDING, TRASHED, and PURGED in its own routed
        # transactions.  Calling this from the Context orphan transaction
        # would hide freshly queued references and self-lock file SQLite.
        del session, queue_job
        if isinstance(job, Job):
            raise RuntimeError("media_blob_gc_requires_two_phase_dispatch")
        result = self._services.media_blob_gc.sweep(workspace_id=job.workspace_id)
        return {
            "inspected_count": result.inspected_count,
            "purged_count": result.purged_count,
            "cancelled_count": result.cancelled_count,
            "repaired_count": result.repaired_count,
        }

    def _business_context_reconciliation_reminder(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        del queue_job
        result = self.reconciliation_reminder_service.execute(
            session=session,
            workspace_id=job.workspace_id,
            page_id=self._uuid_target(job=job, key="page_id"),
            branch_version_id=self._uuid_target(job=job, key="branch_version_id"),
            review_item_id=self._uuid_target(job=job, key="review_item_id"),
            reminder_after_hours=self._int_target(
                job=job, key="reminder_after_hours", minimum=1
            ),
        )
        return {
            "outcome": result.outcome,
            "unresolved": result.unresolved,
            "activity_created": result.activity_created,
            "activity_event_id": (
                str(result.activity_event_id)
                if result.activity_event_id is not None
                else None
            ),
        }

    def _queue_reconciliation_reminder_email_isolated(
        self,
        *,
        session_factory: sessionmaker,
        job: Job | _ClaimedJob,
    ) -> int:
        # EmailOutbox and the owner roster are identity-plane data. Keep this
        # phase outside the workspace transaction that emitted Activity so a
        # split deployment never joins User/WorkspaceMembership on a data DB.
        if self.email_delivery_service is None and (
            self._settings is None or not self._settings.mail_configured
        ):
            return 0
        with session_factory.begin() as control_session:
            delivery_service = self._email_delivery_service()
            queued = delivery_service.queue_reconciliation_reminder_notifications(
                session=control_session,
                workspace_id=job.workspace_id,
                branch_version_id=self._uuid_target(job=job, key="branch_version_id"),
            )
        return len(queued)

    def _context_media_orphan_sweep_service(self) -> ContextMediaOrphanSweepService:
        if self.context_media_orphan_sweep_service is not None:
            return self.context_media_orphan_sweep_service
        # G1a confines concrete context_media.adapters construction to
        # backend/app/container.py; use the worker's composed registry rather
        # than opening a second service graph for this handler.
        self.context_media_orphan_sweep_service = ContextMediaOrphanSweepService(
            context_media_repository_factory=(
                self._services.context_media_repository_factory
            )
        )
        return self.context_media_orphan_sweep_service

    def _business_email_delivery(
        self, session: Session, job: Job | _ClaimedJob, queue_job: Job | None
    ) -> dict[str, Any]:
        """Tombstone for the deleted email_delivery Job path (MAIL-02).

        Nothing enqueues this job type any more: EmailOutbox is
        identity-plane data with no workspace_id column, so this handler ran
        against the WRONG session -- under a split it would have raised
        "email outbox row not found", and its SMTP send would have happened
        inside a workspace transaction. Delivery is the control-plane sweep's
        alone (EmailDeliveryService.sweep_due).

        Rows queued before this change still exist in runtime databases and
        still get claimed, so this must COMPLETE them rather than fail them:
        the outbox row they point at is delivered by the sweep regardless.
        Removing the job type itself needs a migration for those rows and is
        a post-MIG-02 follow-up.
        """
        return {
            "outbox_id": str(job.target_ref.get("outbox_id") or ""),
            "superseded_by": "email_outbox_sweep",
        }

    def _email_delivery_service(self) -> EmailDeliveryService:
        if self.email_delivery_service is not None:
            return self.email_delivery_service
        if self._settings is None:
            raise ValueError("email delivery worker requires resolved settings")
        self.email_delivery_service = EmailDeliveryService(settings=self._settings)
        return self.email_delivery_service

    def _embed_indexer(self) -> CompositeIndexer | object | None:
        if self._indexer is not None:
            return self._indexer
        if self._settings is None and self._database_provider is None:
            # No injected settings: skip embedding, matching the default
            # embedding_provider=none path (never read a fresh Settings()).
            return None
        provider = self._database_provider or DatabaseProvider(self._settings)
        self._indexer = build_indexer(
            self._settings,
            provider,
        )
        return self._indexer

    def _mark_failed_isolated(
        self,
        session_factory: sessionmaker | None,
        workspace_id: UUID,
        job_id: UUID,
        error: str,
        max_retries: int,
        expected_job: _ClaimedJob | None = None,
    ) -> str:
        """Record `job_id`'s failure - requeue or terminal fail per the
        ``max_retries`` budget - in a *separate* transaction. Returns the
        resulting job status (or "unknown" if the update itself failed).

        Conversion/Preview recovery locks the Job here before
        ``_reset_requeued_domain_state`` takes lineage locks, matching legacy
        dispatch, stale reclaim, and manual Library conversion retry.

        ``session_factory`` is None only when the caller could not even
        resolve ``session_for(workspace_id)`` for this job's workspace (Phase
        6a: an unreachable workspace mid-dispatch). There is no other plane
        left to fall back to -- Job lives on that workspace's plane, not
        control -- so this is treated the same as a write failure: logged,
        "unknown" returned, reconciliation left for the next reclaim sweep.
        """
        if session_factory is None:
            logger.error(
                "cannot mark job %s failed: workspace session factory "
                "unavailable; reconciliation required",
                job_id,
            )
            return "unknown"
        try:
            with session_factory.begin() as fail_session:
                if expected_job is not None and expected_job.job_type in {
                    "library_conversion",
                    "library_preview",
                }:
                    acquire_write_intent(fail_session)
                    fail_job = fail_session.scalar(
                        select(Job)
                        .where(
                            Job.workspace_id == expected_job.workspace_id,
                            Job.id == job_id,
                        )
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                else:
                    fail_job = get_in_workspace(
                        fail_session,
                        Job,
                        workspace_id=workspace_id,
                        resource_id=job_id,
                    )
                if fail_job is None:
                    logger.error(
                        "cannot mark job %s failed: row no longer exists",
                        job_id,
                    )
                    return "unknown"
                if (
                    expected_job is not None
                    and expected_job.job_type
                    in {"library_conversion", "library_preview"}
                    and (
                        fail_job.status != "running"
                        or not self._same_attempt_generation_claim(
                            fail_job, expected_job
                        )
                    )
                ):
                    return "stale"
                if fail_job.status not in {"failed", "queued"}:
                    self.job_queue.fail(
                        session=fail_session,
                        job=fail_job,
                        error=error,
                        max_retries=max_retries,
                    )
                if fail_job.status == "queued":
                    self._reset_requeued_domain_state(fail_session, fail_job)
                return fail_job.status
        except Exception:
            logger.exception(
                "failed to record job %s failure status; reconciliation required",
                job_id,
            )
            return "unknown"

    def _apply_failure_markers_isolated(
        self,
        session_factory: sessionmaker,
        job: Job | _ClaimedJob,
        error: str,
        *,
        manual_retryable: bool,
    ) -> None:
        """Persist job-type domain failure markers in their own transaction.
        The phase-1 rollback discards any failure status the business handler
        wrote (e.g. extraction's version/source markers + audit event), so once
        the job is terminally failed they must be re-applied here; otherwise
        the domain rows stay in their pre-job pending state forever. Errors are
        logged rather than raised so a marker write cannot mask the dispatch
        outcome."""
        handler = get_handler(job.job_type)
        if handler is None or handler.on_failure is None:
            return
        try:
            with session_factory.begin() as marker_session:
                if job.job_type == "library_conversion":
                    handler.on_failure(
                        self,
                        marker_session,
                        job,
                        error,
                        manual_retryable=manual_retryable,
                    )
                else:
                    handler.on_failure(self, marker_session, job, error)
        except Exception:
            logger.exception(
                "failed to persist domain failure markers for job %s",
                job.id,
            )

    def _extraction_failure_markers(
        self, session: Session, job: Job | _ClaimedJob, error: str
    ) -> None:
        self.extraction_service.mark_version_failed(
            session=session,
            library_item_version_id=self._uuid_target(
                job=job, key="library_item_version_id"
            ),
            error=error,
        )

    def _library_conversion_failure_markers(
        self,
        session: Session,
        job: Job | _ClaimedJob,
        error: str,
        *,
        manual_retryable: bool,
    ) -> None:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            return
        self._library_conversion_service().mark_terminal_failure(
            session=session,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_conversion_attempt_id",
            ),
            execution_generation=raw_generation,
            manual_retryable=manual_retryable,
        )

    def _library_preview_failure_markers(
        self, session: Session, job: Job | _ClaimedJob, error: str
    ) -> None:
        raw_generation = job.target_ref.get("execution_generation")
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            return
        self._library_preview_service().mark_terminal_failure(
            session=session,
            workspace_id=job.workspace_id,
            attempt_id=self._uuid_target(
                job=job,
                key="library_preview_attempt_id",
            ),
            execution_generation=raw_generation,
        )

    def _lint_failure_markers(
        self, session: Session, job: Job | _ClaimedJob, error: str
    ) -> None:
        raw_run_id = job.target_ref.get("lint_run_id")
        if raw_run_id is None:
            return
        try:
            run_id = UUID(str(raw_run_id))
        except ValueError:
            return
        lint_run = get_in_workspace(
            session,
            LintRun,
            workspace_id=job.workspace_id,
            resource_id=run_id,
        )
        if lint_run is None or lint_run.status == "completed":
            return
        lint_run.status = "failed"
        lint_run.completed_at = datetime.now(UTC)

    def _dispatch_claimed(self, session: Session, job: Job) -> WorkerDispatchResult:
        handler = self._handler_for(job)
        # Single-phase legacy path: extraction/bundle let the service own the
        # complete/fail transition (queue_job=job); discovery/feedback complete
        # here in the wrapper (their services do not take a job).
        detail = handler.business(
            self, session, job, job if handler.completes_in_service else None
        )
        if (
            job.job_type == RECONCILIATION_REMINDER_JOB_TYPE
            and detail.get("unresolved") is True
        ):
            if self.email_delivery_service is None and (
                self._settings is None or not self._settings.mail_configured
            ):
                detail["email_outbox_count"] = 0
            else:
                detail["email_outbox_count"] = len(
                    self._email_delivery_service().queue_reconciliation_reminder_notifications(
                        session=session,
                        workspace_id=job.workspace_id,
                        branch_version_id=self._uuid_target(
                            job=job, key="branch_version_id"
                        ),
                    )
                )
        if not handler.completes_in_service:
            self.job_queue.complete(session=session, job=job)
        return WorkerDispatchResult(
            job_id=job.id,
            job_type=job.job_type,
            status=job.status,
            detail=detail,
        )

    @staticmethod
    def _uuid_target(
        job: Job | _ClaimedJob, key: str, default: UUID | None = None
    ) -> UUID:
        raw_value = job.target_ref.get(key)
        if raw_value is None:
            if default is not None:
                return default
            raise ValueError(f"job target_ref missing {key}")
        return UUID(str(raw_value))

    @staticmethod
    def _int_target(
        job: Job | _ClaimedJob,
        key: str,
        *,
        minimum: int | None = None,
    ) -> int:
        raw_value = job.target_ref.get(key)
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(f"job target_ref {key} must be an integer")
        if minimum is not None and raw_value < minimum:
            raise ValueError(f"job target_ref {key} is below its minimum")
        return raw_value

    @staticmethod
    def _datetime_target(job: Job | _ClaimedJob, key: str) -> datetime | None:
        raw_value = job.target_ref.get(key)
        if raw_value is None:
            return None
        return datetime.fromisoformat(str(raw_value))


register(
    "library_discovery",
    JobHandler(OneShotWorker._business_library_discovery, completes_in_service=False),
)
register(
    "extraction",
    JobHandler(
        OneShotWorker._business_extraction,
        completes_in_service=True,
        on_failure=OneShotWorker._extraction_failure_markers,
    ),
)
register(
    "library_conversion",
    JobHandler(
        OneShotWorker._business_library_conversion,
        completes_in_service=False,
        on_failure=OneShotWorker._library_conversion_failure_markers,
    ),
)
register(
    "library_preview",
    JobHandler(
        OneShotWorker._business_library_preview,
        completes_in_service=False,
        on_failure=OneShotWorker._library_preview_failure_markers,
    ),
)
register(
    "feedback_resolution",
    JobHandler(OneShotWorker._business_feedback, completes_in_service=False),
)
register(
    "bundle_expiration_sweep",
    JobHandler(OneShotWorker._business_bundle, completes_in_service=True),
)
register(
    "embed",
    JobHandler(OneShotWorker._business_embed, completes_in_service=False),
)
register(
    "lint_tick",
    JobHandler(
        OneShotWorker._business_lint_tick,
        completes_in_service=False,
        on_failure=OneShotWorker._lint_failure_markers,
    ),
)
register(
    "trail_tick",
    JobHandler(OneShotWorker._business_trail_tick, completes_in_service=False),
)
register(
    "librarian_tick",
    JobHandler(OneShotWorker._business_librarian_tick, completes_in_service=False),
)
register(
    "media_install_reconcile",
    JobHandler(
        OneShotWorker._business_media_install_reconcile,
        completes_in_service=False,
    ),
)
register(
    "context_media_orphan_sweep",
    JobHandler(
        OneShotWorker._business_context_media_orphan_sweep,
        completes_in_service=False,
    ),
)
register(
    "credential_replica_reconcile",
    JobHandler(
        OneShotWorker._business_credential_replica_reconcile,
        completes_in_service=False,
    ),
)
register(
    "media_blob_gc",
    JobHandler(
        OneShotWorker._business_media_blob_gc,
        completes_in_service=False,
    ),
)
register(
    RECONCILIATION_REMINDER_JOB_TYPE,
    JobHandler(
        OneShotWorker._business_context_reconciliation_reminder,
        completes_in_service=False,
    ),
)
# Tombstone only -- see OneShotWorker._business_email_delivery. No on_failure
# marker: it ran record_terminal_failure on the WORKSPACE session, which is
# the wrong plane for EmailOutbox, and the outbox's own retry budget
# (EmailDeliveryService._record_attempt_failure) is what terminalizes now.
register(
    "email_delivery",
    JobHandler(
        OneShotWorker._business_email_delivery,
        completes_in_service=False,
    ),
)
