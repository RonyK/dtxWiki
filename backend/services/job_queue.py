import logging
import random
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import event, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.job import Job

logger = logging.getLogger(__name__)

# Max total attempts for a retryable job. Both fail() and
# reclaim_stale_jobs() compare the post-increment retry_count against this
# budget, so a value of N means N attempts in total; 0 (or 1) marks the job
# terminally failed on its first failure.
DEFAULT_JOB_MAX_ATTEMPTS = 3

# Base seconds for exponential retry backoff. fail() sets next_attempt_at to
# now + base * 2**(retry_count-1); claim_next skips jobs whose next_attempt_at
# is still in the future, so a rate-limited provider (e.g. an embedding API at
# 429) is not re-hit every ~1s poll. Lands before the embed workload (Epic-1).
RETRY_BACKOFF_BASE_SECONDS = 30.0
# Hard ceiling on a single retry delay. Without it a large max_retries would let
# the exponential term overflow timedelta (datetime + timedelta raises past
# ~year 9999); 1 hour is well beyond any transient-provider recovery window.
MAX_RETRY_BACKOFF_SECONDS = 3600.0

# Job types the Activity feed hides UNCONDITIONALLY (ActivityService._jobs):
# a context edit/publish or a library extraction enqueues an `embed` job on
# every change, and a flood of routine embed rows would drown Activity's
# recency-capped feed. This constant is Activity's alone -- job visibility
# (GET /workspaces/{id}/jobs, the failure-notification email) uses the two
# status-aware sets below instead, NOT this one, because a terminally FAILED
# embed job is exactly what an operator needs to see; see their docstrings.
HIDDEN_JOB_TYPES = frozenset(
    {
        "embed",
        "email_delivery",
        "context_reconciliation_reminder",
        # P10.3 T4.6: credential_replica_reconcile is seeded once per
        # workspace per MINUTE and does nothing at all on a healthy
        # workspace, so its successes are pure feed noise (1440 silent rows
        # per workspace per day) -- exactly the flood this constant exists
        # to keep out of Activity. Its FAILURES are surfaced instead through
        # NOISY_UNTIL_FAILED_JOB_TYPES below and the owner failure email, the
        # same split embed already has.
        "credential_replica_reconcile",
    }
)

# Job types the failure-notification email and the workspace jobs listing
# (1b) hide in EVERY status, not just while non-terminal. email_delivery is a
# TOMBSTONE (MAIL-02): nothing enqueues it any more -- mail is delivered by
# the control-plane sweep, EmailDeliveryService.sweep_due -- but legacy rows
# still drain out of runtime databases and must stay invisible on the way,
# exactly as they were when the type was live. It was never really "this
# workspace's" job either: legacy producers attached it to an installation
# workspace, regardless of which workspace's action triggered the email.
ALWAYS_HIDDEN_JOB_TYPES = frozenset({"email_delivery"})

# Job types hidden ONLY while non-terminal (queued/running/completed) --
# visible once they reach status="failed". embed is the original member: its
# routine per-edit volume is exactly what HIDDEN_JOB_TYPES filters out of
# Activity, but a job that reaches "failed" here is rare and consequential --
# unlike a routine embed job, it means this workspace's semantic index
# silently regressed on one page, and nothing else in the product catches
# that by default. The two mechanisms an earlier version of this constant
# cited turned out not to cover it on inspection: SemanticRetriever.
# _warn_if_stale_index only fires on a zero-result SEARCH and only logs a
# server-side warning for a provider/model MISMATCH (a page with no
# embedding at all, because its job failed, produces neither symptom), and
# `dtxwiki embedding doctor` is a manual connectivity probe with no
# awareness of Job rows at all. So a failed embed job stays visible here.
#
# P10.3 T4.6 registers credential_replica_reconcile on the same terms, and
# for a sharper version of the same reason: it is the ONLY thing that repairs
# a credential grant whose workspace-plane replica has drifted, and a missing
# replica is a hard denial (D-01v3-b grant_replica_missing). A terminally
# failed reconcile therefore means "MCP access to this workspace may be
# silently broken and nothing is fixing it" -- while the Settings sync chip
# only reports rows the reconcile managed to reach, so it cannot report the
# reconcile itself dying. Its per-minute successes stay hidden (see
# HIDDEN_JOB_TYPES).
NOISY_UNTIL_FAILED_JOB_TYPES = frozenset(
    {"embed", "context_reconciliation_reminder", "credential_replica_reconcile"}
)

# Job types whose failure state is NOT confined to the Job row: on terminal
# failure, worker/registry.py's on_failure handler also writes a domain-specific
# marker (e.g. LibraryAttempt(stage="conversion").status="failed"). Manual retry (Epic-2
# Phase 1c) only flips the Job row back to "queued" -- it does not know how to
# undo those domain markers -- so retrying one of these types here would leave
# the Job re-running against an attempt/run/version the domain layer still
# considers terminally done. Kept in sync with worker/registry.py by
# test_worker_runtime.py::test_job_types_with_domain_failure_state_matches_registry.
JOB_TYPES_WITH_DOMAIN_FAILURE_STATE = frozenset(
    {
        "extraction",
        "library_conversion",
        "library_preview",
        "lint_tick",
    }
)


def calculate_retry_delay(
    retry_count: int,
    base_seconds: float = RETRY_BACKOFF_BASE_SECONDS,
    max_seconds: float = MAX_RETRY_BACKOFF_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Calculate equal-jitter exponential retry backoff.

    Equal jitter preserves exponential backoff scaling while adding bounded
    randomness with a nonzero lower bound (half_delay) to prevent thundering
    herds when external providers recover.
    """
    count = max(1, retry_count)
    base_delay = min(max_seconds, base_seconds * (2.0 ** (count - 1)))
    half_delay = base_delay / 2.0
    jitter = (
        rng.uniform(0, half_delay) if rng is not None else random.uniform(0, half_delay)
    )
    return half_delay + jitter


def context_page_embed_idempotency_key(
    page_id: UUID,
    marker: str,
    version_id: UUID,
    content_hash: str,
    *,
    decision_id: UUID | None = None,
) -> str:
    """Address a page-head trigger by version or by a review transition.

    After undo, a fresh decision can publish or reject the same version again.
    Its decision ID distinguishes that transition from the earlier completed job.
    """

    if marker not in {"draft", "published"}:
        raise ValueError("context embed marker must be draft or published")
    if decision_id is not None:
        return f"context_page:{page_id}:decision:{decision_id}"
    return f"context_page:{page_id}:{marker}:{version_id}:{content_hash}"


def context_page_undo_embed_idempotency_key(page_id: UUID, undo_id: UUID) -> str:
    """Return the key of the reindex an undone review decision queues.

    The heads an undo restores were each indexed before, under keys a repeat
    enqueue would match, so the undo addresses its own transition.
    """

    return f"context_page:{page_id}:undo:{undo_id}"


class JobRetryUnsupportedTypeError(RuntimeError):
    """manual_retry() refused a job_type in JOB_TYPES_WITH_DOMAIN_FAILURE_STATE."""

    def __init__(self, job_type: str) -> None:
        self.job_type = job_type
        super().__init__(f"job_type {job_type!r} cannot be retried generically")


class JobQueueService:
    def enqueue(
        self,
        session: Session,
        workspace_id: UUID,
        job_type: str,
        trigger_type: str,
        idempotency_key: str | None = None,
        target_ref: dict[str, Any] | None = None,
    ) -> Job:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "job_type": job_type,
            "trigger_type": trigger_type,
            "idempotency_key": idempotency_key,
            "target_ref": target_ref or {},
            "status": "queued",
        }
        if idempotency_key is None:
            job = self._insert_job(session, values)
            self._register_job_hint(session, workspace_id)
            return job

        existing = self.get_by_idempotency_key(
            session=session,
            workspace_id=workspace_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            # Already queued (or further along) -- a duplicate enqueue is not
            # new work, so it does not warrant a fresh hint. Whatever hint
            # accompanied the original enqueue (or the periodic full sweep)
            # already covers this job.
            return existing

        statement = self._insert_on_conflict_do_nothing(session, values)
        if statement is None:
            job = self._insert_job(session, values)
            self._register_job_hint(session, workspace_id)
            return job
        session.execute(statement)
        job = self.get_by_idempotency_key(
            session=session,
            workspace_id=workspace_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
        )
        if job is None:
            raise RuntimeError(
                "enqueue conflict left no visible job row: "
                f"workspace={workspace_id} job_type={job_type} "
                f"idempotency_key={idempotency_key}"
            )
        # Whether this call won the race (inserted) or lost it to a
        # concurrent enqueue of the same idempotency key, a Job row now
        # exists that did not before this statement (ON CONFLICT DO NOTHING
        # only ever no-ops against a row from an EARLIER enqueue -- see
        # get_by_idempotency_key above, which already returned early for
        # that case). Either way it is new work worth a hint.
        self._register_job_hint(session, workspace_id)
        return job

    @staticmethod
    def _register_job_hint(session: Session, workspace_id: UUID) -> None:
        """Phase 6b (plan §3 D-2): best-effort "this workspace may have
        work" -- see backend/services/job_hints.py for the full contract.

        Registered as a ``session.after_commit`` listener rather than
        written here directly, for two reasons that both matter:

        1. It must never precede the job's own commit. Writing it now would
           advertise a job that might still roll back (e.g. the caller's
           surrounding transaction fails later for an unrelated reason) --
           still only a latency-neutral false positive per D-2, but
           after_commit gets it for free with no extra reasoning required:
           a rolled-back session never fires after_commit at all, so an
           enqueue that never commits produces no hint, ever.
        2. It must never be able to fail the enqueue. after_commit runs
           after the real COMMIT already happened -- record_workspace_job_hint
           itself catches everything, but running it post-commit is what
           makes that safe: even an uncaught exception there could not roll
           back or otherwise affect the job row.

        SQLAlchemy's ``after_commit`` event also fires when a SAVEPOINT is
        released. Treating that as the real transaction commit can publish a
        hint too early and, with SQLite's StaticPool, let the hint session
        commit the connection out from under the still-open SAVEPOINT. Track
        registrations by transaction scope instead: a nested commit promotes
        its workspace ids to the parent, a nested rollback drops only its own
        ids, and only the outer commit drains the durable set.

        No-ops when the session was not created by a DatabaseProvider (most
        unit tests use a bare session fixture) -- session.info simply will
        not carry the "database_provider" key in that case, matching this
        being a pure latency optimization with no behavioral dependency.
        """
        database_provider = session.info.get("database_provider")
        if database_provider is None:
            return
        transaction = session.get_nested_transaction() or session.get_transaction()
        if transaction is None:  # pragma: no cover - enqueue has already flushed
            raise RuntimeError("job hint registration requires an active transaction")
        pending_by_transaction: dict[object, set[UUID]] = session.info.setdefault(
            "_pending_job_hint_workspace_ids_by_transaction", {}
        )
        pending_by_transaction.setdefault(transaction, set()).add(workspace_id)

        if session.info.get("_job_hint_transaction_listeners_registered"):
            return
        session.info["_job_hint_transaction_listeners_registered"] = True

        def _on_commit(_session: Session) -> None:
            pending_scopes: dict[object, set[UUID]] = _session.info.setdefault(
                "_pending_job_hint_workspace_ids_by_transaction", {}
            )
            nested = _session.get_nested_transaction()
            if nested is not None:
                nested_ids = pending_scopes.pop(nested, set())
                parent = nested.parent
                if parent is not None and nested_ids:
                    pending_scopes.setdefault(parent, set()).update(nested_ids)
                return

            workspace_ids = (
                set().union(*pending_scopes.values()) if pending_scopes else set()
            )
            pending_scopes.clear()
            provider = _session.info.get("database_provider")
            if provider is None:
                return
            from backend.services.job_hints import record_workspace_job_hint

            for pending_workspace_id in workspace_ids:
                record_workspace_job_hint(provider, pending_workspace_id)

        def _on_rollback(_session: Session) -> None:
            pending_scopes: dict[object, set[UUID]] = _session.info.setdefault(
                "_pending_job_hint_workspace_ids_by_transaction", {}
            )
            nested = _session.get_nested_transaction()
            if nested is not None:
                pending_scopes.pop(nested, None)
            else:
                pending_scopes.clear()

        event.listen(session, "after_commit", _on_commit)
        event.listen(session, "after_rollback", _on_rollback)

    @staticmethod
    def _insert_job(session: Session, values: dict[str, Any]) -> Job:
        job = Job(**values)
        session.add(job)
        session.flush()
        return job

    @staticmethod
    def _insert_on_conflict_do_nothing(session: Session, values: dict[str, Any]):
        """Race-safe idempotent insert: two transactions enqueuing the same
        (workspace_id, job_type, idempotency_key) both converge on the winning
        row instead of one raising IntegrityError into (and rolling back) the
        caller's transaction. Returns None for dialects without ON CONFLICT
        support, falling back to plain check-then-insert."""
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(Job)
        elif dialect == "postgresql":
            statement = postgresql_insert(Job)
        else:
            return None
        return statement.values(**values).on_conflict_do_nothing(
            index_elements=["workspace_id", "job_type", "idempotency_key"]
        )

    def get_by_idempotency_key(
        self,
        session: Session,
        workspace_id: UUID,
        job_type: str,
        idempotency_key: str,
    ) -> Job | None:
        return session.scalar(
            select(Job).where(
                Job.workspace_id == workspace_id,
                Job.job_type == job_type,
                Job.idempotency_key == idempotency_key,
            )
        )

    def claim_next(
        self,
        session: Session,
        workspace_id: UUID | None = None,
        job_types: set[str] | None = None,
    ) -> Job | None:
        now = utc_now()
        statement = select(Job.id).where(
            Job.status == "queued",
            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
        )
        if workspace_id is not None:
            statement = statement.where(Job.workspace_id == workspace_id)
        if job_types:
            statement = statement.where(Job.job_type.in_(job_types))
        statement = statement.order_by(Job.created_at.asc(), Job.id.asc()).limit(1)
        for _attempt in range(5):
            job_id = session.scalar(statement)
            if job_id is None:
                return None
            claimed_at = utc_now()
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(
                    status="running",
                    stage="claimed",
                    started_at=claimed_at,
                    next_attempt_at=None,
                )
            )
            if result.rowcount == 1:
                session.flush()
                return session.get(Job, job_id)
            session.expire_all()
        return None

    def reclaim_stale_jobs(
        self,
        session: Session,
        workspace_id: UUID,
        lease_timeout_seconds: int,
        max_retries: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> list[Job]:
        """Requeue jobs stuck in 'running' past their lease, or terminally fail
        them once the retry budget is exhausted. A job becomes terminal when
        retry_count reaches max_retries after the increment, matching fail()'s
        max-total-attempts semantics. Updates are status-guarded so a job
        completed or failed concurrently is left alone.

        ``workspace_id`` is required, not optional: the
        pre-6a signature had no workspace filter at all and scanned every
        'running' row globally, which is exactly the shape that goes silent
        once the DB is sharded per workspace -- a caller that forgot to
        filter would simply never notice, because there was nothing to force
        it to name a workspace. Making the parameter required means a caller
        that wants "every workspace" (there is no legitimate one left; see
        OneShotWorker.reclaim_stale_jobs_with_session_factory) has to fan out
        and say so explicitly, one workspace at a time, rather than getting
        an all-workspaces scan by omission."""
        cutoff = utc_now() - timedelta(seconds=lease_timeout_seconds)
        stale_ids = session.scalars(
            select(Job.id)
            .where(
                Job.workspace_id == workspace_id,
                Job.status == "running",
                Job.started_at.is_not(None),
                Job.started_at < cutoff,
            )
            .order_by(Job.started_at.asc(), Job.id.asc())
        ).all()
        if not stale_ids:
            return []
        error = (
            "reclaimed after worker lease expired "
            f"({lease_timeout_seconds}s in status 'running')"
        )
        reclaimed_ids: list[UUID] = []
        for job_id in stale_ids:
            stale_guard = (
                Job.id == job_id,
                Job.status == "running",
                Job.started_at.is_not(None),
                Job.started_at < cutoff,
            )
            requeued = session.execute(
                update(Job)
                .where(*stale_guard, Job.retry_count + 1 < max_retries)
                .values(
                    status="queued",
                    stage=None,
                    started_at=None,
                    retry_count=Job.retry_count + 1,
                    last_error=error,
                    next_attempt_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if requeued.rowcount == 1:
                reclaimed_ids.append(job_id)
                continue
            failed = session.execute(
                update(Job)
                .where(*stale_guard, Job.retry_count + 1 >= max_retries)
                .values(
                    status="failed",
                    stage=None,
                    retry_count=Job.retry_count + 1,
                    last_error=error,
                    next_attempt_at=None,
                    completed_at=utc_now(),
                )
                .execution_options(synchronize_session=False)
            )
            if failed.rowcount == 1:
                reclaimed_ids.append(job_id)
        if not reclaimed_ids:
            return []
        session.expire_all()
        return [session.get(Job, job_id) for job_id in reclaimed_ids]

    def manual_retry(
        self, session: Session, job_id: UUID, workspace_id: UUID
    ) -> Job | None:
        """Requeue a terminally-failed job for exactly one more attempt.

        ``retry_count`` is intentionally NOT reset. fail()'s max-total-attempts
        check compares the post-increment retry_count against max_retries, so
        leaving it at its already-exhausted value means: if this one extra
        attempt fails too, the very next fail() call terminates it again
        immediately -- it does not grant a fresh multi-attempt budget. A reset
        counter would let repeated manual retries retry the same job forever.

        ``workspace_id`` must be the job's own workspace. ``session`` is
        already routed to that workspace's shard by the caller
        (``session_for(workspace_id)``), which is enough to make a wrong
        guess miss entirely once the DB is actually split -- a shard simply
        does not have another workspace's rows. It is NOT enough on its own
        in the still-common unsharded deployment, where every workspace's
        session_for resolves to the same physical database: there, a caller
        that named the wrong workspace_id would otherwise retry a
        DIFFERENT workspace's job undetected. This check closes that gap
        directly instead of depending on physical shard isolation to do it.

        Raises JobRetryUnsupportedTypeError for a job_type in
        JOB_TYPES_WITH_DOMAIN_FAILURE_STATE: this only flips the Job row, and
        has no way to undo the domain-specific terminal marker (e.g. a
        LibraryAttempt(stage="conversion") left status="failed") those types'
        on_failure handlers wrote. Checked inside the same transaction as the
        flip (not by the caller) so the two can never drift apart.

        Returns None if no row was terminally failed under this (id,
        workspace_id) pair -- the id does not exist, exists under a
        DIFFERENT workspace than the one named, or is not currently "failed"
        (already retried, still running, or never failed). These are folded
        into one outcome deliberately: a caller must not be able to use the
        response to learn that a job id exists under some other workspace it
        did not name. The final UPDATE is still status- and workspace-guarded
        so the check-and-flip stays atomic against a concurrent reclaim/retry
        even though the checks above already read the row once.
        """
        job = session.get(Job, job_id)
        if job is None or job.workspace_id != workspace_id or job.status != "failed":
            return None
        if job.job_type in JOB_TYPES_WITH_DOMAIN_FAILURE_STATE:
            raise JobRetryUnsupportedTypeError(job.job_type)
        result = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.workspace_id == workspace_id,
                Job.status == "failed",
            )
            .values(
                status="queued",
                stage=None,
                progress=0,
                started_at=None,
                next_attempt_at=None,
                completed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        session.expire_all()
        return session.get(Job, job_id)

    def complete(
        self,
        session: Session,
        job: Job,
        completed_at: datetime | None = None,
    ) -> Job:
        job.status = "completed"
        job.stage = None
        job.progress = 100
        job.completed_at = completed_at or utc_now()
        job.next_attempt_at = None
        session.add(job)
        session.flush()
        return job

    def fail(
        self,
        session: Session,
        job: Job,
        error: str,
        max_retries: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Job:
        """Record a failed attempt. ``max_retries`` is a max-total-attempts
        budget: the post-increment retry_count is compared against it, so
        ``max_retries=N`` requeues the job until it has failed N times in
        total, and ``max_retries=0`` (or 1) fails terminally on the first
        failure."""
        job.retry_count += 1
        job.last_error = error
        job.stage = None
        if job.retry_count < max_retries:
            job.status = "queued"
            job.started_at = None
            delay = calculate_retry_delay(job.retry_count)
            job.next_attempt_at = utc_now() + timedelta(seconds=delay)
        else:
            job.status = "failed"
            job.next_attempt_at = None
            job.completed_at = utc_now()
        session.add(job)
        session.flush()
        return job
