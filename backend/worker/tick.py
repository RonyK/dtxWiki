from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.settings import Settings
from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.services.bundle_expiration import BundleExpirationService
from backend.services.context_media_policy import (
    ContextMediaPolicy,
    resolve_context_media_policy,
)
from backend.services.job_queue import JobQueueService


def tick_schedules(
    settings: Settings, policy: ContextMediaPolicy | None = None
) -> tuple[tuple[str, int], ...]:
    """The (job_type, interval_hours) schedule seed_jobs_for_workspace walks
    for its interval-bucketed jobs, pulled out of seed_due_jobs so
    entry.py's per-workspace fan-out (Epic-2 Phase 6a) can compute it once
    per tick instead of re-deriving it inside every workspace's session.

    Epic-2 D-7: context_media_orphan_sweep's threshold is day-scale (168h
    default), not minute-scale like bundle_expiration_sweep/
    media_install_reconcile/credential_replica_reconcile, so it belongs in
    the interval-bucketed schedule
    here, not the per-minute one in seed_jobs_for_workspace -- per-minute-
    per-workspace seeding is exactly the thing Phase 6's job-queue fan-out
    (plan §4) has to scale, and workspace count tracks subscriber count
    (every user gets a personal workspace), so a third per-minute job here
    is not free. The interval is derived from the retention setting itself
    rather than a dedicated new setting -- "how often to check" is not an
    independent product decision, only "often enough not to matter" relative
    to the grace period -- bounded to [1h, 24h]: never coarser than daily
    even for a very long retention window (no benefit to checking less
    often), and never coarser than the window itself for a short one (so a
    short retention setting is not silently rounded up to a day of slop).
    Disabled automatic reclamation retains hourly drain-only maintenance.
    """
    hours = (
        policy.effective_hours
        if policy is not None
        else settings.context_media_orphan_retention_hours
    )
    context_media_orphan_sweep_interval_hours = (
        max(1, min(24, hours)) if hours > 0 else 1
    )
    return (
        ("lint_tick", settings.lint_tick_interval_hours),
        ("trail_tick", settings.trail_tick_interval_hours),
        ("librarian_tick", settings.librarian_tick_interval_days * 24),
        (
            "context_media_orphan_sweep",
            context_media_orphan_sweep_interval_hours,
        ),
        # Physical GC is independent of Context retention: Library deletion
        # and the manual Context delete path can both enqueue the last shared
        # reference while automatic orphan reclamation is disabled.
        ("media_blob_gc", 1),
    )


def seed_jobs_for_workspace(
    session: Session,
    workspace_id: UUID,
    *,
    now: datetime,
    job_queue: JobQueueService,
    bundle_expiration: BundleExpirationService,
    schedules: tuple[tuple[str, int], ...],
    context_media_policy_revision: str | None = None,
) -> list[Job]:
    """Seed one workspace's periodic jobs on ``session`` (already scoped to
    that workspace's plane).

    Pulled out of ``seed_due_jobs`` for Epic-2 Phase 6a: the fan-out caller
    (entry.py's periodic seed loop) resolves the workspace roster from the
    control plane once and the schedule (``tick_schedules(settings)``) once,
    then calls this per ``session_for(workspace_id)`` -- it never touches
    ``Workspace`` or ``Settings`` itself, since by the time this runs both
    are already resolved by the caller. ``seed_due_jobs`` below is the
    single-session convenience wrapper that resolves the roster AND calls
    this for every workspace on the SAME session; it is what direct callers
    (tests, any single-DB deployment where control and workspace planes
    coincide) still use.
    """
    seeded: list[Job] = []
    minute_bucket = now.replace(second=0, microsecond=0).isoformat()
    minute_idempotency_key = f"{workspace_id}:{minute_bucket}"
    # P10.3 T4.6 (plan section 2.6's reconciler row): credential_replica_
    # reconcile is minute-bucketed, "bundle_expiration_sweep과 같은 자리" --
    # NOT in tick_schedules' interval-hours tuple above, whose members are
    # day/hour-scale. Its unit of work is one workspace: the roster walk the
    # plan's prose mentions is the seeding fan-out itself (worker/entry.py's
    # _seed_due_jobs reads the roster once per tick and calls this function
    # per session_for(workspace_id)), so each seeded job already knows the
    # only workspace it has to reconcile. It takes the plain enqueue branch
    # below -- bundle_expiration_sweep needs its own helper only because
    # BundleExpirationService owns that job's target_ref shape.
    minute_job_types = (
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
    )
    for minute_job_type in minute_job_types:
        if (
            job_queue.get_by_idempotency_key(
                session=session,
                workspace_id=workspace_id,
                job_type=minute_job_type,
                idempotency_key=minute_idempotency_key,
            )
            is not None
        ):
            continue
        if minute_job_type == "bundle_expiration_sweep":
            seeded.append(
                bundle_expiration.enqueue_sweep(
                    session=session,
                    workspace_id=workspace_id,
                    at=now,
                )
            )
        else:
            seeded.append(
                job_queue.enqueue(
                    session=session,
                    workspace_id=workspace_id,
                    job_type=minute_job_type,
                    trigger_type="scheduled",
                    idempotency_key=minute_idempotency_key,
                    target_ref={"scheduled_for": now.isoformat()},
                )
            )
    for job_type, interval_hours in schedules:
        if interval_hours <= 0:
            continue
        interval_seconds = interval_hours * 3600
        bucket = int(now.timestamp()) // interval_seconds
        idempotency_key = f"{job_type}:{interval_hours}h:{bucket}"
        if (
            job_type == "context_media_orphan_sweep"
            and context_media_policy_revision is not None
        ):
            idempotency_key += f":{context_media_policy_revision}"
        if (
            job_queue.get_by_idempotency_key(
                session=session,
                workspace_id=workspace_id,
                job_type=job_type,
                idempotency_key=idempotency_key,
            )
            is not None
        ):
            continue
        seeded.append(
            job_queue.enqueue(
                session=session,
                workspace_id=workspace_id,
                job_type=job_type,
                trigger_type="scheduled",
                idempotency_key=idempotency_key,
                target_ref={"scheduled_for": now.isoformat()},
            )
        )
    return seeded


def seed_due_jobs(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    job_queue: JobQueueService | None = None,
) -> list[Job]:
    """Single-session convenience wrapper: reads the workspace roster AND
    seeds every workspace's periodic jobs, all on the same ``session``.

    This is what tests and any single-DB deployment call directly -- control
    and workspace planes coincide there, so reading the roster and writing
    each workspace's jobs on one shared session is correct and matches the
    pre-Phase-6a behavior exactly. The fanned-out path (entry.py's periodic
    seed loop, Epic-2 Phase 6a) does NOT call this: it reads the roster from
    the control plane itself and calls ``seed_jobs_for_workspace`` once per
    ``session_for(workspace_id)`` instead, so that a real overlay's sharded
    DBs each only ever see writes for their own workspace.
    """
    now = now or datetime.now(UTC)
    queue = job_queue or JobQueueService()
    bundle_expiration = BundleExpirationService(job_queue=queue)
    workspaces = session.scalars(select(Workspace).order_by(Workspace.id)).all()
    seeded: list[Job] = []
    for workspace in workspaces:
        workspace_id = workspace.id
        policy = resolve_context_media_policy(
            workspace, settings.context_media_orphan_retention_hours
        )
        seeded.extend(
            seed_jobs_for_workspace(
                session,
                workspace_id,
                now=now,
                job_queue=queue,
                bundle_expiration=bundle_expiration,
                schedules=tick_schedules(settings, policy),
                context_media_policy_revision=policy.revision,
            )
        )
    return seeded
