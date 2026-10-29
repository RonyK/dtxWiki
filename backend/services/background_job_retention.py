"""Bounded retention of completed scheduled checks and expired MCP receipts."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from backend.models.job import Job
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.repositories.workspace_scoped import (
    delete_in_workspace,
    select_in_workspace,
    select_in_workspace_by_ids,
)
from backend.services.db_write_intent import acquire_workspace_write_intent
from backend.services.periodic_job_keys import PERIODIC_JOB_TYPES, periodic_key_end

COMPLETED_CHECK_RETENTION = timedelta(days=7)


@dataclass(frozen=True)
class RetentionBatch:
    jobs_deleted: int
    receipts_deleted: int
    next_job_cursor: UUID | None


def prune_background_history(
    session_factory: Callable[[], Session],
    workspace_id: UUID,
    *,
    now: datetime,
    batch_size: int = 200,
    job_cursor: UUID | None = None,
) -> RetentionBatch:
    """Inspect one bounded batch, then write only when something may be deleted.

    The worker carries ``next_job_cursor`` across sweeps. Unknown keys and
    checks whose encoded period is still open must survive, without keeping
    every later row permanently outside the bounded scan. A completed pass
    returns ``None`` so the next pass also revisits rows whose periods have
    since ended. Newly inserted UUIDs behind the cursor wait for that pass.

    The advisory read closes before taking SQLite's writer lock. Deletes in
    the fresh write transaction repeat the eligibility conditions and exact
    identities, so a changed job or renewed receipt survives. PostgreSQL
    skips selected rows another transaction has locked. An empty batch never
    acquires write intent. The caller owns the overall time budget.
    """
    if not 100 <= batch_size <= 500:
        raise ValueError("retention batch_size must be between 100 and 500")

    job_scope = (
        Job.job_type.in_(PERIODIC_JOB_TYPES),
        Job.trigger_type.in_(("schedule", "scheduled")),
        Job.status == "completed",
        Job.completed_at < now - COMPLETED_CHECK_RETENTION,
    )
    scan_scope = job_scope
    if job_cursor is not None:
        scan_scope += (Job.id > job_cursor,)
    candidates = (
        select_in_workspace(Job, workspace_id=workspace_id, conditions=scan_scope)
        .with_only_columns(Job.id, Job.job_type, Job.idempotency_key)
        .order_by(Job.id)
        .limit(batch_size)
    )
    receipt_scope = (MCPWriteIdempotency.expires_at < now,)
    receipts = (
        select_in_workspace(
            MCPWriteIdempotency, workspace_id=workspace_id, conditions=receipt_scope
        )
        .with_only_columns(MCPWriteIdempotency.id)
        .order_by(MCPWriteIdempotency.id)
        .limit(batch_size)
    )
    with session_factory() as session:
        scanned = session.execute(candidates).all()
        receipt_ids = list(session.scalars(receipts))
    expired_periods = [
        (job_id, key)
        for job_id, job_type, key in scanned
        if (end := periodic_key_end(job_type, key)) is not None and end <= now
    ]
    jobs_deleted = 0
    receipts_deleted = 0
    if expired_periods or receipt_ids:
        with session_factory() as session, session.begin():
            acquire_workspace_write_intent(session)
            if session.get_bind().dialect.name == "postgresql":
                if expired_periods:
                    locked_job_ids = set(
                        session.scalars(
                            select_in_workspace_by_ids(
                                Job,
                                workspace_id=workspace_id,
                                resource_ids=[
                                    job_id for job_id, _key in expired_periods
                                ],
                            )
                            .with_only_columns(Job.id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    expired_periods = [
                        pair for pair in expired_periods if pair[0] in locked_job_ids
                    ]
                if receipt_ids:
                    receipt_ids = list(
                        session.scalars(
                            select_in_workspace_by_ids(
                                MCPWriteIdempotency,
                                workspace_id=workspace_id,
                                resource_ids=receipt_ids,
                            )
                            .with_only_columns(MCPWriteIdempotency.id)
                            .with_for_update(skip_locked=True)
                        )
                    )
            if expired_periods:
                result = session.execute(
                    delete_in_workspace(
                        Job,
                        workspace_id=workspace_id,
                        conditions=(
                            *job_scope,
                            # Bounds let the existing (workspace_id, id)
                            # index drive a large-backlog delete without
                            # depending on fresh planner statistics.
                            Job.id >= expired_periods[0][0],
                            Job.id <= expired_periods[-1][0],
                            tuple_(Job.id, Job.idempotency_key).in_(expired_periods),
                        ),
                    ).execution_options(synchronize_session=False)
                )
                jobs_deleted = result.rowcount
            if receipt_ids:
                result = session.execute(
                    delete_in_workspace(
                        MCPWriteIdempotency,
                        workspace_id=workspace_id,
                        conditions=(
                            *receipt_scope,
                            MCPWriteIdempotency.id.in_(receipt_ids),
                        ),
                    ).execution_options(synchronize_session=False)
                )
                receipts_deleted = result.rowcount

    return RetentionBatch(
        jobs_deleted=jobs_deleted,
        receipts_deleted=receipts_deleted,
        next_job_cursor=scanned[-1].id if len(scanned) == batch_size else None,
    )
