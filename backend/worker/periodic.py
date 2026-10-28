"""Periodic registration rules and advisory checks before enqueueing."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.container import (
    DatabaseProvider,
    has_due_media_gc_work,
    has_due_media_install_work,
)
from backend.app.settings import Settings
from backend.models.job import Job
from backend.repositories.workspace_scoped import select_in_workspace
from backend.services.bundle_expiration import BundleExpirationService
from backend.services.context_media_policy import ContextMediaPolicy
from backend.services.credential_replica_service import (
    has_due_credential_replica_work,
    has_due_credential_replica_work_in_session,
)
from backend.services.job_queue import JobQueueService
from backend.services.periodic_job_keys import (
    MINUTE_JOB_TYPES,
    PERIODIC_JOB_TYPES,
    periodic_job_key,
)

logger = logging.getLogger(__name__)


def _orphan_interval(settings: Settings, policy: ContextMediaPolicy) -> int:
    hours = policy.effective_hours
    return max(1, min(24, hours)) if hours > 0 else 1


def _credential_probe(session: Session, *, workspace_id: UUID, now: datetime) -> bool:
    del now
    return has_due_credential_replica_work_in_session(
        session, workspace_id=workspace_id
    )


def _enqueue_bundle(
    session: Session, workspace_id: UUID, now: datetime, queue: JobQueueService
) -> Job:
    return BundleExpirationService(job_queue=queue).enqueue_sweep(
        session, workspace_id, at=now
    )


@dataclass(frozen=True)
class PeriodicJob:
    job_type: str
    interval_hours: Callable[[Settings, ContextMediaPolicy], int] | None = None
    session_probe: Callable[..., bool] | None = None
    provider_probe: Callable[..., bool] | None = None
    enqueue: Callable[..., Job] | None = None


PERIODIC_JOBS = (
    PeriodicJob(
        "bundle_expiration_sweep",
        session_probe=BundleExpirationService.has_due_work,
        enqueue=_enqueue_bundle,
    ),
    PeriodicJob("media_install_reconcile", session_probe=has_due_media_install_work),
    PeriodicJob(
        "credential_replica_reconcile",
        session_probe=_credential_probe,
        provider_probe=has_due_credential_replica_work,
    ),
    PeriodicJob(
        "lint_tick", lambda settings, policy: settings.lint_tick_interval_hours
    ),
    PeriodicJob(
        "trail_tick", lambda settings, policy: settings.trail_tick_interval_hours
    ),
    PeriodicJob(
        "librarian_tick",
        lambda settings, policy: settings.librarian_tick_interval_days * 24,
    ),
    PeriodicJob("context_media_orphan_sweep", _orphan_interval),
    PeriodicJob(
        "media_blob_gc", lambda settings, policy: 1, session_probe=has_due_media_gc_work
    ),
)
# Retention reads keys through periodic_job_keys, which cannot import this
# worker registry. A type missing there would never be pruned, so fail at
# import instead (not an assert: python -O strips those).
if {rule.job_type for rule in PERIODIC_JOBS} != PERIODIC_JOB_TYPES or {
    rule.job_type for rule in PERIODIC_JOBS if rule.interval_hours is None
} != MINUTE_JOB_TYPES:
    raise RuntimeError("PERIODIC_JOBS and periodic_job_keys disagree")


def _connection_lost(exc: BaseException) -> bool:
    """Also see through service errors raised ``from`` a dropped connection."""
    cause = exc if isinstance(exc, DBAPIError) else exc.__cause__
    return isinstance(cause, DBAPIError) and cause.connection_invalidated


def scheduled_jobs(
    workspace_id: UUID, *, now: datetime, settings: Settings, policy: ContextMediaPolicy
) -> Iterator[tuple[PeriodicJob, str]]:
    """One registry determines enabled schedules and their current keys."""
    policy_revision = policy.revision
    for rule in PERIODIC_JOBS:
        interval = (
            rule.interval_hours(settings, policy) if rule.interval_hours else None
        )
        if interval is not None and interval <= 0:
            continue
        yield (
            rule,
            periodic_job_key(
                rule.job_type, workspace_id, now, interval, policy_revision
            ),
        )


def due_job_types(
    workspace_id: UUID,
    *,
    now: datetime,
    settings: Settings,
    policy: ContextMediaPolicy,
    database_provider: DatabaseProvider | None = None,
    session: Session | None = None,
) -> frozenset[str]:
    """Skip existing keys, then share one reader for the workspace probes.

    The initial key lookup also establishes workspace availability. Its failure
    aborts this workspace once. Individual probe errors remain fail-open;
    savepoints isolate query failures, and a lost connection aborts the pass.
    Credential planning reads both planes after this shared reader is closed.
    """
    if session is None and database_provider is None:
        raise ValueError("periodic checks require a session or database provider")
    planned = list(
        scheduled_jobs(workspace_id, now=now, settings=settings, policy=policy)
    )
    read_scope = (
        nullcontext(session)
        if session is not None
        else database_provider.session_for(workspace_id)()
    )
    due: set[str] = set()
    provider_rules: list[PeriodicJob] = []
    with read_scope as read_session:
        existing = set(
            read_session.execute(
                select_in_workspace(
                    Job,
                    workspace_id=workspace_id,
                    conditions=(
                        tuple_(Job.job_type, Job.idempotency_key).in_(
                            [(rule.job_type, key) for rule, key in planned]
                        ),
                    ),
                ).with_only_columns(Job.job_type, Job.idempotency_key)
            )
        )
        for rule, key in planned:
            if (rule.job_type, key) in existing:
                continue
            if rule.provider_probe is not None and database_provider is not None:
                provider_rules.append(rule)
                continue
            if rule.session_probe is None:
                due.add(rule.job_type)
                continue
            try:
                with read_session.begin_nested():
                    has_work = rule.session_probe(
                        read_session, workspace_id=workspace_id, now=now
                    )
            except Exception as exc:
                if _connection_lost(exc):
                    raise
                logger.exception(
                    "periodic precheck failed; registering %s for workspace %s",
                    rule.job_type,
                    workspace_id,
                )
                has_work = True
            if has_work:
                due.add(rule.job_type)
    for rule in provider_rules:
        try:
            has_work = rule.provider_probe(database_provider, workspace_id=workspace_id)
        except Exception as exc:
            if _connection_lost(exc):
                raise
            logger.exception(
                "periodic precheck failed; registering %s for workspace %s",
                rule.job_type,
                workspace_id,
            )
            has_work = True
        if has_work:
            due.add(rule.job_type)
    return frozenset(due)
