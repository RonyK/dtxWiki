"""Phase 6b advisory job hints.

doc/design/workspace-database-separation-plan.md §3 D-2, §4 Phase 6: 6a's
round-robin fan-out made the worker correct once ``Job`` lives on a
per-workspace plane, at the cost of up to N round trips (one ``claim_next``
per workspace) to conclude "nothing to do", where N tracks subscriber count
(every user gets a personal workspace).

This module is the latency fix, not a second correctness mechanism:

* ``record_workspace_job_hint`` -- write "this workspace may have work".
  Called only from a session's ``after_commit`` listener (see
  ``JobQueueService.enqueue``), i.e. strictly after the job row itself is
  durable. A dropped or failed hint write costs nothing but latency: the
  worker's round-robin sweep and periodic full sweep (worker/entry.py) never
  read this table to decide whether a workspace gets visited, only to decide
  which workspace to try FIRST.
* ``peek_oldest_hinted_workspace`` -- read the single oldest pending hint
  (FIFO), without clearing it.
* ``clear_hint_if_not_refreshed`` -- compare-and-clear: delete a hint only if
  it is still exactly the one the caller observed. A concurrent producer
  that re-hinted the same workspace (a newer ``hinted_at``) wins -- the
  delete's WHERE guard matches nothing, so the fresher hint survives. This
  is what lets the worker clear a hint unconditionally (hit, miss, or a
  claim error) without racing a concurrent enqueue into a lost wakeup.

See ``backend.models.job.WorkspaceJobHint`` for why this is a single-column
table with row presence/absence as the only state, and
``backend.worker.runtime.OneShotWorker._claim_via_hint`` for the consumer.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.models.base import utc_now
from backend.models.job import WorkspaceJobHint

logger = logging.getLogger(__name__)


class ControlPlaneSessionSource(Protocol):
    """The one thing ``record_workspace_job_hint`` needs: a way to open a
    control-plane transaction independent of whatever session the caller's
    job enqueue used. ``DatabaseProvider`` satisfies this; tests can pass any
    object exposing the same method."""

    def control_session_factory(self) -> sessionmaker[Session]: ...


def record_workspace_job_hint(
    database_provider: ControlPlaneSessionSource, workspace_id: UUID
) -> None:
    """Best-effort control-plane write: upsert "workspace_id may have work".

    This must never raise. It runs from a session ``after_commit`` listener,
    which fires *after* the real COMMIT has already happened -- an exception
    here cannot roll anything back, it can only be misreported to whatever
    called ``session.commit()`` as if the commit itself had failed. Every
    failure is therefore caught and logged, never propagated. That is also
    what keeps the hint write from ever being able to fail the job enqueue
    it followed: by the time this runs, the enqueue already succeeded and is
    durable, on a transaction this function does not touch.
    """
    try:
        with database_provider.control_session_factory().begin() as session:
            _upsert_hint(session, workspace_id=workspace_id, hinted_at=utc_now())
    except Exception:
        logger.exception(
            "failed to record job hint for workspace %s (advisory only -- "
            "the periodic full sweep still finds the job)",
            workspace_id,
        )


def _upsert_hint(session: Session, *, workspace_id: UUID, hinted_at: datetime) -> None:
    """Race-safe upsert: two concurrent enqueues for the same workspace both
    converge on one row holding the LATEST hinted_at, mirroring
    JobQueueService._insert_on_conflict_do_nothing's dialect handling."""
    values = {"workspace_id": workspace_id, "hinted_at": hinted_at}
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(WorkspaceJobHint).values(**values)
    elif dialect == "postgresql":
        statement = postgresql_insert(WorkspaceJobHint).values(**values)
    else:
        existing = session.get(WorkspaceJobHint, workspace_id)
        if existing is None:
            session.add(WorkspaceJobHint(**values))
        else:
            existing.hinted_at = hinted_at
        return
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["workspace_id"],
            set_={"hinted_at": hinted_at},
        )
    )


def peek_oldest_hinted_workspace(session: Session) -> tuple[UUID, datetime] | None:
    """The single oldest still-pending hint, FIFO order. Read-only -- callers
    that act on the result are responsible for clearing it afterward via
    ``clear_hint_if_not_refreshed``."""
    row = session.execute(
        select(WorkspaceJobHint.workspace_id, WorkspaceJobHint.hinted_at)
        .order_by(
            WorkspaceJobHint.hinted_at.asc(),
            WorkspaceJobHint.workspace_id.asc(),
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    return (row.workspace_id, row.hinted_at)


def clear_hint_if_not_refreshed(
    session: Session,
    *,
    workspace_id: UUID,
    observed_hinted_at: datetime,
) -> None:
    """Delete ``workspace_id``'s hint only if it is still no newer than
    ``observed_hinted_at`` -- the value read by the ``peek`` that led to this
    call. A hint refreshed concurrently (a real new job landing while this
    workspace was being tried) has a newer ``hinted_at`` and is left alone."""
    session.execute(
        delete(WorkspaceJobHint).where(
            WorkspaceJobHint.workspace_id == workspace_id,
            WorkspaceJobHint.hinted_at <= observed_hinted_at,
        )
    )
