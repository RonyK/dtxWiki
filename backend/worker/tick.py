from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.settings import Settings
from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.services.context_media_policy import (
    ContextMediaPolicy,
    resolve_context_media_policy,
)
from backend.services.job_queue import JobQueueService
from backend.worker.periodic import due_job_types, scheduled_jobs


def seed_jobs_for_workspace(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime,
    job_queue: JobQueueService,
    settings: Settings,
    policy: ContextMediaPolicy,
    due_types: frozenset[str],
) -> list[Job]:
    """Enqueue the rules checked before this workspace's write transaction."""
    seeded: list[Job] = []
    for rule, key in scheduled_jobs(
        workspace_id, now=now, settings=settings, policy=policy
    ):
        if rule.job_type not in due_types:
            continue
        if rule.enqueue is not None:
            job = rule.enqueue(session, workspace_id, now, job_queue)
        else:
            job = job_queue.enqueue(
                session=session,
                workspace_id=workspace_id,
                job_type=rule.job_type,
                trigger_type="scheduled",
                idempotency_key=key,
                target_ref={"scheduled_for": now.isoformat()},
            )
        seeded.append(job)
    return seeded


def seed_due_jobs(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    job_queue: JobQueueService | None = None,
) -> list[Job]:
    """Single-DB convenience path; production fans out through the provider."""
    now = now or datetime.now(UTC)
    queue = job_queue or JobQueueService()
    workspaces = session.scalars(select(Workspace).order_by(Workspace.id)).all()
    prepared = []
    for workspace in workspaces:
        policy = resolve_context_media_policy(
            workspace, settings.context_media_orphan_retention_hours
        )
        due = due_job_types(
            workspace.id, now=now, settings=settings, policy=policy, session=session
        )
        prepared.append((workspace.id, policy, due))
    seeded: list[Job] = []
    for workspace_id, policy, due in prepared:
        seeded.extend(
            seed_jobs_for_workspace(
                session,
                workspace_id,
                now=now,
                job_queue=queue,
                settings=settings,
                policy=policy,
                due_types=due,
            )
        )
    return seeded
