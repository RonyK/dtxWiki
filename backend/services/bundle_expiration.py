from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.base import utc_now
from backend.models.bundle import AgentInputBundle
from backend.models.job import Job
from backend.services.audit_service import AuditService
from backend.services.job_queue import JobQueueService


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

    def enqueue_sweep(
        self,
        session: Session,
        workspace_id: UUID,
        at: datetime | None = None,
    ) -> Job:
        bucket = self._minute_bucket(at or utc_now())
        return self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="bundle_expiration_sweep",
            trigger_type="schedule",
            idempotency_key=f"{workspace_id}:{bucket}",
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
        statement = select(AgentInputBundle).where(
            AgentInputBundle.status != "expired",
            AgentInputBundle.valid_until <= run_at,
        )
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

    @staticmethod
    def _minute_bucket(at: datetime) -> str:
        return at.replace(second=0, microsecond=0).isoformat()
