"""Read-side queries for background jobs (Epic-2 Phase 1 / D-6).

Job already records the structured state an operator or a workspace member
needs (status, stage, progress, retry_count and timestamps -- see
backend.models.job.Job). It also stores a raw ``last_error`` for protected
diagnostics, but this read surface deliberately exposes only the derived stable
failure code. The module stays separate from JobQueueService (which owns the
write-side claim/fail/complete/manual_retry state machine).

list_workspace_jobs backs GET /workspaces/{id}/jobs (workspace-scoped from the
start, per D-6/D-2: after Phase 5 shards the DB by workspace this only needs
session_for(workspace_id) swapped in, no rewrite). list_all_jobs and
queue_summary back the separate server-admin surface -- deliberately not the
same query with an extra filter, so a rewrite of one never has to touch the
other.

list_all_jobs/queue_summary now fan out across the
workspace roster instead of running one cross-workspace query, because once
Job is workspace-plane data a single query has nothing left to read. See
each method's docstring for the cursor and partial-failure design.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.models.base import utc_now
from backend.models.job import Job
from backend.models.workspace import Workspace
from backend.services.job_queue import (
    ALWAYS_HIDDEN_JOB_TYPES,
    JOB_TYPES_WITH_DOMAIN_FAILURE_STATE,
    NOISY_UNTIL_FAILED_JOB_TYPES,
)

logger = logging.getLogger(__name__)

_JOB_CURSOR_PREFIX = "job-offset:"
_MAX_JOB_OFFSET = 10_000

# Phase 6a fan-out cursor (list_all_jobs only -- list_workspace_jobs keeps the
# offset cursor above, it never fans out). An offset has no meaning once the
# "one query" it indexed into becomes N per-shard queries merged together, so
# this encodes a seek position instead: the (created_at, id) of the last item
# already served. Each shard is re-queried with "give me your rows ranked
# below this position" and the results are merged -- see list_all_jobs'
# docstring for why this, instead of a per-workspace offset, survives a
# workspace being added/removed/unreachable between pages.
_FANOUT_CURSOR_PREFIX = "job-seek:"


def _encode_job_cursor(offset: int) -> str:
    token = f"{_JOB_CURSOR_PREFIX}{offset}".encode()
    return base64.urlsafe_b64encode(token).decode()


def _decode_job_cursor(cursor: str | None) -> int:
    # Tolerant: a missing/garbled cursor restarts at offset 0 rather than
    # 500ing (mirrors activity_service._decode_activity_cursor).
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return 0
    if not raw.startswith(_JOB_CURSOR_PREFIX):
        return 0
    try:
        offset = int(raw[len(_JOB_CURSOR_PREFIX) :])
    except ValueError:
        return 0
    return min(max(0, offset), _MAX_JOB_OFFSET)


def _next_job_cursor(*, offset: int, limit: int, has_more: bool) -> str | None:
    """Return the next bounded offset without issuing a cursor that decodes
    back to the current page.

    ``_decode_job_cursor`` caps forged or stale cursors to protect the database
    from arbitrarily large OFFSET queries. Once a listing reaches that cap,
    issuing ``offset + limit`` would be decoded back to the cap and make the
    client append the same page forever. Treat the cap as the pagination
    boundary instead.
    """
    if not has_more:
        return None
    next_offset = offset + limit
    if next_offset > _MAX_JOB_OFFSET:
        return None
    return _encode_job_cursor(next_offset)


_FANOUT_CURSOR_V2_PREFIX = "job-seek2:"

# A shard that was unreachable on one page still owes its rows from wherever it
# was asked. Carrying more than this many such positions would grow the cursor
# without bound, so above the cap the listing stops issuing a cursor: restarting
# from the top is lossless, and the caller already knows the page was partial
# from ``unreachable_workspace_ids``.
_MAX_LAGGING_SHARDS = 32

_Position = tuple[datetime, UUID] | None


@dataclass(frozen=True)
class _FanoutSeek:
    """Where each shard should resume.

    ``global_seek`` is the position for every shard the cursor says nothing
    special about. ``lagging`` overrides it per shard, and an explicit ``None``
    there means "from the top" -- distinct from "not present", which means
    "use the global". A shard appears in ``lagging`` only while it still owes
    rows ranked above ``global_seek``.
    """

    global_seek: _Position = None
    lagging: Mapping[UUID, _Position] = field(default_factory=dict)

    def asked_position(self, workspace_id: UUID) -> _Position:
        if workspace_id in self.lagging:
            return self.lagging[workspace_id]
        return self.global_seek


def _ranks_at_or_below(position: _Position, boundary: _Position) -> bool:
    """True if ``position`` is no higher than ``boundary`` in (created_at, id) desc.

    ``None`` means "no seek yet", i.e. the very top of the listing, so it is
    higher than every real position.
    """
    if position is None:
        return boundary is None
    if boundary is None:
        return True
    return position <= boundary


def _encode_fanout_cursor(
    global_seek: _Position, lagging: Mapping[UUID, _Position]
) -> str:
    payload: dict[str, Any] = {
        "seek": None
        if global_seek is None
        else [global_seek[0].isoformat(), str(global_seek[1])],
        "lag": {
            str(workspace_id): None
            if position is None
            else [position[0].isoformat(), str(position[1])]
            for workspace_id, position in lagging.items()
        },
    }
    token = _FANOUT_CURSOR_V2_PREFIX + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )
    return base64.urlsafe_b64encode(token.encode()).decode()


def _decode_position(raw: Any) -> _Position:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("position must be [created_at, id]")
    return datetime.fromisoformat(raw[0]), UUID(raw[1])


def _decode_fanout_cursor(cursor: str | None) -> _FanoutSeek | None:
    """Tolerant decode, like _decode_job_cursor.

    A missing, garbled, or pre-fan-out ("job-offset:") cursor restarts the
    listing from the top rather than 500ing or misreading an offset as a seek
    key. The single-key "job-seek:" shape this replaced is still accepted, so a
    page already in flight during a deploy keeps walking instead of resetting;
    it simply carries no lag map, which is exactly its old behavior.
    """
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None

    if raw.startswith(_FANOUT_CURSOR_V2_PREFIX):
        body = raw[len(_FANOUT_CURSOR_V2_PREFIX) :]
        try:
            payload = json.loads(body)
            lagging = {
                UUID(workspace_id): _decode_position(position)
                for workspace_id, position in (payload.get("lag") or {}).items()
            }
            return _FanoutSeek(
                global_seek=_decode_position(payload.get("seek")), lagging=lagging
            )
        except (ValueError, AttributeError, TypeError):
            return None

    if raw.startswith(_FANOUT_CURSOR_PREFIX):
        body = raw[len(_FANOUT_CURSOR_PREFIX) :]
        created_at_raw, separator, id_raw = body.partition("|")
        if not separator:
            return None
        try:
            return _FanoutSeek(
                global_seek=(datetime.fromisoformat(created_at_raw), UUID(id_raw))
            )
        except ValueError:
            return None

    return None


def _seek_before_clause(seek: tuple[datetime, UUID] | None):
    """(created_at, id) DESC keyset predicate: 'strictly ranked below seek'.

    Applied identically to every shard so the merged result stays consistent
    with the single global order the endpoint already advertised (created_at
    desc, id desc) -- fan-out changes how pagination finds its place in that
    order, not the order itself.
    """
    if seek is None:
        return None
    created_at, job_id = seek
    return or_(
        Job.created_at < created_at,
        and_(Job.created_at == created_at, Job.id < job_id),
    )


def _resolve_workspace_roster(
    control_session: Session, *, workspace_id: UUID | None
) -> list[tuple[UUID, str]]:
    """Workspace.id/slug is identity-plane data (plan §3 D-4), so this reads
    the control session directly -- the fan-out below is what routes each
    workspace's actual Job rows through session_for(workspace_id) instead.
    """
    statement = select(Workspace.id, Workspace.slug).order_by(Workspace.id)
    if workspace_id is not None:
        statement = statement.where(Workspace.id == workspace_id)
    return [(row.id, row.slug) for row in control_session.execute(statement)]


def _lease_expired_clause(lease_timeout_seconds: int):
    cutoff = utc_now() - timedelta(seconds=lease_timeout_seconds)
    return and_(
        Job.status == "running",
        Job.started_at.is_not(None),
        Job.started_at < cutoff,
    )


def _visible_job_type_clause():
    """Status-aware counterpart to Activity's unconditional HIDDEN_JOB_TYPES.

    A NOISY_UNTIL_FAILED_JOB_TYPES row (embed,
    context_reconciliation_reminder, credential_replica_reconcile) is hidden
    while queued/running/completed -- same noise reasoning as Activity -- but
    visible once status="failed", because that IS the case an operator or
    workspace owner needs to see (see NOISY_UNTIL_FAILED_JOB_TYPES'
    docstring for why nothing else surfaces it; for
    credential_replica_reconcile this listing and the owner failure email are
    the ONLY places a dead reconciler becomes visible, since the Settings
    sync chip reports per-row state the reconciler itself has to write).
    ALWAYS_HIDDEN_JOB_TYPES (email_delivery) is excluded regardless of
    status.
    """
    return and_(
        Job.job_type.not_in(ALWAYS_HIDDEN_JOB_TYPES),
        or_(
            Job.job_type.not_in(NOISY_UNTIL_FAILED_JOB_TYPES),
            Job.status == "failed",
        ),
    )


def _apply_job_filters(
    statement: Select,
    *,
    status: str | None,
    job_type: str | None,
    problems_only: bool,
    lease_timeout_seconds: int,
) -> Select:
    if status is not None:
        statement = statement.where(Job.status == status)
    if job_type is not None:
        statement = statement.where(Job.job_type == job_type)
    if problems_only:
        statement = statement.where(
            or_(Job.status == "failed", _lease_expired_clause(lease_timeout_seconds))
        )
    return statement


# The closed vocabulary of failure reasons a workspace-plane reader may learn.
# The wire carries the code, not a rendered sentence: /jobs and /activity are
# bilingual surfaces, and the client renders these through
# activity:jobs.errorCode.* (frontend/src/shared/lib/activityLabels.ts).
PUBLIC_JOB_FAILURE_CODES = frozenset({"job_failed", "job_retry_scheduled"})


def public_job_failure_code(job: Job) -> str | None:
    """The workspace-plane reason a job failed, as a stable code.

    ``job.last_error`` is read for its TRUTHINESS only -- never its content,
    never a substring, never a regex capture -- and the code itself is derived
    from ``job.status``, so no worker ``str(exc)`` byte can reach a workspace
    surface by construction. Redacting last_error without this left a
    workspace member with no reason at all, which is why the Activity feed
    grew its own unredacted copy of the column instead.

    Deliberately two codes rather than a per-exception taxonomy: a
    per-exception code would have to be captured at write time and persisted,
    and Job has no column for it. If a ``last_error_code`` column ever lands,
    this is the one function that grows to read it, and both readers of
    last_error inherit it for free.

    Session-free by contract: Job rows are workspace-plane data, and this
    stays callable from either plane.
    """
    if not job.last_error:
        return None
    if job.status == "failed":
        return "job_failed"
    if job.status in {"queued", "running"}:
        # An attempt failed and another is pending -- the state a workspace
        # member most needs named, since silence here reads as "nothing wrong".
        return "job_retry_scheduled"
    # completed: a stale error from an earlier attempt is not a live problem.
    return None


def job_failure_disclosure(job: Job) -> dict[str, str | None]:
    """Return the bounded failure shape shared by every API reader.

    Worker handlers persist arbitrary ``str(exc)`` diagnostics. Those bytes
    stay available to protected operator logs and direct database diagnostics,
    but never cross an API boundary: an exception may contain credentials,
    provider details, or host paths, and server-admin authentication is not a
    redaction mechanism. ``error_code`` carries the actionable stable state
    instead.
    """
    return {
        "error_code": public_job_failure_code(job),
    }


def job_read_projection(
    job: Job,
    *,
    workspace_slug: str | None,
    lease_timeout_seconds: int,
) -> dict[str, Any]:
    lease_expired = (
        job.status == "running"
        and job.started_at is not None
        and job.started_at < utc_now() - timedelta(seconds=lease_timeout_seconds)
    )
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "workspace_slug": workspace_slug,
        "job_type": job.job_type,
        "trigger_type": job.trigger_type,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "retry_count": job.retry_count,
        # Every API reader gets the same bounded code. The raw worker exception
        # remains an operator-log/database diagnostic and never reaches JSON.
        **job_failure_disclosure(job),
        "started_at": job.started_at,
        "next_attempt_at": job.next_attempt_at,
        "completed_at": job.completed_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "retryable": (
            job.status == "failed"
            and job.job_type not in JOB_TYPES_WITH_DOMAIN_FAILURE_STATE
        ),
        "lease_expired": lease_expired,
    }


class JobReadService:
    def list_workspace_jobs(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        status: str | None,
        job_type: str | None,
        problems_only: bool,
        limit: int,
        cursor: str | None,
        lease_timeout_seconds: int,
    ) -> dict[str, Any]:
        statement = select(Job).where(
            Job.workspace_id == workspace_id,
            _visible_job_type_clause(),
        )
        statement = _apply_job_filters(
            statement,
            status=status,
            job_type=job_type,
            problems_only=problems_only,
            lease_timeout_seconds=lease_timeout_seconds,
        )
        offset = _decode_job_cursor(cursor)
        # Over-fetch by one to detect a further page without a second COUNT
        # query (mirrors activity_service's paginated branch).
        rows = session.scalars(
            statement.order_by(Job.created_at.desc(), Job.id.desc())
            .offset(offset)
            .limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "items": [
                job_read_projection(
                    job,
                    workspace_slug=None,
                    lease_timeout_seconds=lease_timeout_seconds,
                )
                for job in rows
            ],
            "next_cursor": _next_job_cursor(
                offset=offset,
                limit=limit,
                has_more=has_more,
            ),
        }

    def list_all_jobs(
        self,
        *,
        control_session: Session,
        session_for: Callable[[UUID], sessionmaker[Session]],
        status: str | None,
        job_type: str | None,
        workspace_id: UUID | None,
        problems_only: bool,
        limit: int,
        cursor: str | None,
        lease_timeout_seconds: int,
    ) -> dict[str, Any]:
        """Server-admin, cross-workspace listing (Phase 6a fan-out, plan §4).

        Single query -> N shard queries + merge, because once Job is
        workspace-plane data there is no cross-workspace query left to run.

        Cursor: a global (created_at, id) seek key plus, only where needed, a
        per-shard override. Each shard is asked for its own rows ranked below
        its position (``_seek_before_clause``), and every shard's own
        top-``limit`` already contains anything that could rank in the
        *global* top ``limit`` -- a row cannot outrank its own shard's
        internal order. A workspace added between pages is picked up
        automatically: its rows satisfying "< seek key" slot into the
        still-unpaged region like any other shard's.

        The override exists because an unreachable shard cannot be treated as
        "contributes nothing". An earlier version issued one global seek key
        derived from the REACHABLE shards' rows, which advanced the key past a
        region the broken shard had never been asked to serve; on the next page
        that shard's rows above the key were excluded by "< seek" forever. So a
        shard that fails keeps the position it was asked with, and is asked
        again from there once it comes back.

        Two consequences worth stating, because both are contract:

        * The global key never moves UP in rank (``min`` against the incoming
          key). A recovered shard's backlog can outrank the current position,
          and letting the key follow it would re-serve rows other shards had
          already delivered. A shard whose backlog is still above the global
          key keeps its override until the key passes it.
        * A recovered shard's backlog therefore arrives on the page where it
          recovers, later than its true global rank. Rows are never duplicated
          and never dropped, but across a failure the walk is no longer
          globally monotonic. Out of order beats silently missing.

        The cost is O(workspace count) shard queries per page rather than one;
        see queue_summary for the same trade-off. This
        repository defers the round-robin/hint optimization used by the
        worker -- this is an operator screen, not the claim loop.

        Ordering contract: unchanged from the single-query version --
        created_at desc, id desc, same as list_workspace_jobs. What changes
        is only the cursor's opaque internal shape (offset -> seek key); a
        cursor captured before this change fails the new prefix check and
        restarts the listing at the top, same tolerant behavior
        _decode_job_cursor already has for a garbled cursor.

        Partial failure: a shard that raises is skipped, not fatal -- one
        unreachable workspace must not 500 the whole admin listing. Its id is
        returned in ``unreachable_workspace_ids`` so the caller can flag the
        response incomplete instead of silently shipping a short list that
        reads as complete, and its position is carried in the cursor so the
        rows it owes are still reachable on a later page.

        Termination: a shard that stays down keeps its position pinned and
        never advances, while reachable shards exhaust; once a page comes back
        empty there is no cursor, so the walk ends rather than spinning on a
        permanently broken shard.
        """
        roster = _resolve_workspace_roster(control_session, workspace_id=workspace_id)
        incoming = _decode_fanout_cursor(cursor) or _FanoutSeek()
        candidates: list[tuple[Job, str]] = []
        unreachable_workspace_ids: list[UUID] = []
        asked_by_shard: dict[UUID, _Position] = {}
        reached_workspace_ids: set[UUID] = set()
        for shard_workspace_id, slug in roster:
            asked = incoming.asked_position(shard_workspace_id)
            asked_by_shard[shard_workspace_id] = asked
            seek_clause = _seek_before_clause(asked)
            statement = select(Job).where(
                # Every shard's session_for(workspace_id) is the same single
                # database in today's non-sharded deployment, so this filter
                # is what keeps each iteration scoped to its own workspace's
                # rows -- not a defensive extra, load-bearing today.
                Job.workspace_id == shard_workspace_id,
                _visible_job_type_clause(),
            )
            statement = _apply_job_filters(
                statement,
                status=status,
                job_type=job_type,
                problems_only=problems_only,
                lease_timeout_seconds=lease_timeout_seconds,
            )
            if seek_clause is not None:
                statement = statement.where(seek_clause)
            # limit (not limit+1) suffices for correctness here -- a shard's
            # own top-`limit` already covers anything that could land in the
            # global top-`limit`. Over-fetch by one only to let has_more
            # (below) detect a further page without a second COUNT query.
            statement = statement.order_by(
                Job.created_at.desc(), Job.id.desc()
            ).limit(limit + 1)
            try:
                with session_for(shard_workspace_id)() as shard_session:
                    rows = shard_session.scalars(statement).all()
            except SQLAlchemyError:
                logger.warning(
                    "admin job listing: workspace %s shard unreachable",
                    shard_workspace_id,
                    exc_info=True,
                )
                unreachable_workspace_ids.append(shard_workspace_id)
                continue
            reached_workspace_ids.add(shard_workspace_id)
            candidates.extend((job, slug) for job in rows)

        candidates.sort(key=lambda pair: (pair[0].created_at, pair[0].id), reverse=True)
        page = candidates[:limit]

        # The global key may only descend. A recovered shard can contribute rows
        # ranked above the incoming key, and following them would re-serve rows
        # other shards already delivered.
        page_tail: _Position = (
            (page[-1][0].created_at, page[-1][0].id) if page else None
        )
        if page_tail is None:
            next_global = incoming.global_seek
        elif incoming.global_seek is None:
            next_global = page_tail
        else:
            next_global = min(page_tail, incoming.global_seek)

        # Lowest-ranked row each shard actually got onto this page.
        served_by_shard: dict[UUID, _Position] = {}
        for job, _slug in page:
            served_by_shard[job.workspace_id] = (job.created_at, job.id)

        lagging: dict[UUID, _Position] = {
            shard_id: asked_by_shard[shard_id]
            for shard_id in unreachable_workspace_ids
        }
        for shard_id in reached_workspace_ids:
            if shard_id not in incoming.lagging:
                # No override in, none out: the global key covers this shard.
                continue
            # A shard that was behind is only caught up once the position it
            # still owes from has been passed by the global key.
            position = served_by_shard.get(shard_id, asked_by_shard[shard_id])
            if not _ranks_at_or_below(position, next_global):
                lagging[shard_id] = position

        # More rows exist if the shards we could reach over-filled the page, or
        # if some shard still owes rows. The `page` guard is what terminates the
        # walk against a permanently unreachable shard.
        has_more = len(candidates) > limit or (bool(lagging) and bool(page))
        next_cursor = None
        if has_more and page and len(lagging) <= _MAX_LAGGING_SHARDS:
            next_cursor = _encode_fanout_cursor(next_global, lagging)
        return {
            "items": [
                job_read_projection(
                    job,
                    workspace_slug=slug,
                    lease_timeout_seconds=lease_timeout_seconds,
                )
                for job, slug in page
            ],
            "next_cursor": next_cursor,
            "unreachable_workspace_ids": unreachable_workspace_ids,
        }

    def job_read_by_id(
        self, session: Session, job_id: UUID, *, lease_timeout_seconds: int
    ) -> dict[str, Any] | None:
        """Single-job projection, used to render the result of a mutation
        (e.g. admin_retry_job) without the caller touching the Job/Workspace
        models directly -- api/ imports services, never models (G1a)."""
        job = session.get(Job, job_id)
        if job is None:
            return None
        workspace = session.get(Workspace, job.workspace_id)
        return job_read_projection(
            job,
            workspace_slug=workspace.slug if workspace is not None else None,
            lease_timeout_seconds=lease_timeout_seconds,
        )

    def queue_summary(
        self,
        *,
        control_session: Session,
        session_for: Callable[[UUID], sessionmaker[Session]],
        lease_timeout_seconds: int,
    ) -> dict[str, Any]:
        """Instance-wide queue depth (Phase 6a fan-out, plan §4).

        Same shard-per-workspace + skip-and-flag partial-failure shape as
        list_all_jobs (see that docstring for the reasoning); there is no
        cursor here since this is a scalar aggregate, not a page. A queue
        depth number that quietly omits a shard is actively misleading -- an
        operator reading "3 failed" when a fourth workspace's shard was just
        unreachable draws the wrong conclusion -- so a partial result is
        summed from whatever shards answered *and* reported via
        ``unreachable_workspace_ids``, never returned indistinguishably from
        a complete one.
        """
        roster = _resolve_workspace_roster(control_session, workspace_id=None)
        totals = {"queued": 0, "running": 0, "failed": 0}
        lease_expired_count = 0
        unreachable_workspace_ids: list[UUID] = []
        for shard_workspace_id, _slug in roster:
            try:
                with session_for(shard_workspace_id)() as shard_session:
                    counts = dict(
                        shard_session.execute(
                            select(Job.status, func.count())
                            .where(Job.workspace_id == shard_workspace_id)
                            .group_by(Job.status)
                        ).all()
                    )
                    shard_lease_expired = int(
                        shard_session.scalar(
                            select(func.count())
                            .select_from(Job)
                            .where(
                                Job.workspace_id == shard_workspace_id,
                                _lease_expired_clause(lease_timeout_seconds),
                            )
                        )
                        or 0
                    )
            except SQLAlchemyError:
                logger.warning(
                    "admin job queue summary: workspace %s shard unreachable",
                    shard_workspace_id,
                    exc_info=True,
                )
                unreachable_workspace_ids.append(shard_workspace_id)
                continue
            for key in totals:
                totals[key] += int(counts.get(key, 0))
            lease_expired_count += shard_lease_expired
        return {
            "queued_count": totals["queued"],
            "running_count": totals["running"],
            "failed_count": totals["failed"],
            "lease_expired_count": lease_expired_count,
            "unreachable_workspace_ids": unreachable_workspace_ids,
        }
