from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.bundle import AgentInputBundle
from backend.models.job import Job
from backend.repositories.workspace_scoped import select_in_workspace
from backend.services.audit_service import AuditService
from backend.services.job_queue import JobQueueService
from backend.services.periodic_job_keys import minute_bucket, periodic_job_key


@dataclass(frozen=True)
class BundleExpirationSweepResult:
    expired_count: int
    inspected_count: int
    run_at: datetime


class BundleExpirationService:
    def __init__(
        self,
        job_queue: JobQueueService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self.job_queue = job_queue or JobQueueService()
        self.audit_service = audit_service or AuditService()

    @staticmethod
    def _due_predicates(now: datetime):
        return (
            AgentInputBundle.status != "expired",
            AgentInputBundle.valid_until <= now,
        )

    @classmethod
    def has_due_work(
        cls, session: Session, *, workspace_id: UUID, now: datetime
    ) -> bool:
        # The worker executes this job at its minute bucket, so probing must
        # use the same cutoff rather than include later seconds in the minute.
        cutoff = now.replace(second=0, microsecond=0)
        with session.no_autoflush:
            return (
                session.scalar(
                    select_in_workspace(
                        AgentInputBundle,
                        workspace_id=workspace_id,
                        conditions=cls._due_predicates(cutoff),
                    )
                    .with_only_columns(AgentInputBundle.id)
                    .limit(1)
                )
                is not None
            )

    def enqueue_sweep(
        self,
        session: Session,
        workspace_id: UUID,
        at: datetime | None = None,
    ) -> Job:
        run_at = at or utc_now()
        bucket = minute_bucket(run_at)
        return self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="bundle_expiration_sweep",
            trigger_type="schedule",
            idempotency_key=periodic_job_key(
                "bundle_expiration_sweep", workspace_id, run_at
            ),
            target_ref={
                "workspace_id": str(workspace_id),
                "minute_bucket": bucket,
            },
        )

    def sweep_expired(
        self,
        session: Session,
        workspace_id: UUID | None = None,
        now: datetime | None = None,
        job: Job | None = None,
        actor_type: str = "system",
        actor_id: UUID | None = None,
    ) -> BundleExpirationSweepResult:
        run_at = now or utc_now()
        statement = select(AgentInputBundle).where(*self._due_predicates(run_at))
        if workspace_id is not None:
            statement = statement.where(AgentInputBundle.workspace_id == workspace_id)

        due_bundles = session.scalars(statement).all()
        expired_count = 0
        for bundle in due_bundles:
            if bundle.status == "expired":
                continue
            bundle.status = "expired"
            bundle.expiration_reason = "ttl_elapsed"
            bundle.expired_at = run_at
            session.add(bundle)
            session.flush()
            expired_count += 1
            self.audit_service.emit(
                session=session,
                workspace_id=bundle.workspace_id,
                actor_kind=actor_type,
                actor_id=str(actor_id or actor_type),
                action="bundle.expire",
                target_kind="agent_input_bundle",
                target_id=bundle.id,
                payload={
                    "valid_until": bundle.valid_until.isoformat(),
                    "expiration_reason": bundle.expiration_reason,
                },
            )

        if job is not None:
            self.job_queue.complete(session=session, job=job)
        session.flush()
        return BundleExpirationSweepResult(
            expired_count=expired_count,
            inspected_count=len(due_bundles),
            run_at=run_at,
        )
