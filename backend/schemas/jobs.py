from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.schemas.common import RESTResponseBase

JobStatus = Literal["queued", "running", "completed", "failed"]


# Deliberately mirrors what the Job row already records: the fields all
# existed, only a read surface for them was missing.
class JobRead(BaseModel):
    """A background job's operator-facing shape.

    Returned by both the workspace-scoped listing and the server-admin
    listing; ``workspace_slug`` is populated only by the latter (null on the
    workspace-scoped route, where the URL already names the workspace).
    """

    id: UUID
    workspace_id: UUID
    workspace_slug: str | None = None
    job_type: str
    trigger_type: str
    status: JobStatus
    stage: str | None
    progress: int
    retry_count: int
    # Stable, bounded failure code (job_read_service.PUBLIC_JOB_FAILURE_CODES).
    # Safe for every reader: derived from status only, never from last_error's
    # text. Optional so the added field breaks no client.
    error_code: str | None = None
    started_at: datetime | None
    next_attempt_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Whether POST .../retry will accept this job. False for job types whose
    # on_failure handler also wrote a domain-specific terminal marker that a
    # generic Job-row requeue cannot undo (JOB_TYPES_WITH_DOMAIN_FAILURE_STATE).
    retryable: bool
    # status=="running" with started_at older than the worker lease timeout:
    # a crashed worker's job, pending the next reclaim_stale_jobs sweep.
    lease_expired: bool

    model_config = ConfigDict(extra="forbid")


class JobListData(BaseModel):
    items: list[JobRead]
    next_cursor: str | None

    model_config = ConfigDict(extra="forbid")


class JobListResponse(RESTResponseBase):
    data: JobListData


class AdminJobListData(JobListData):
    """Server-admin cross-workspace job listing.

    Adds the incompleteness signal the workspace-scoped listing has no use
    for: that route reads exactly one workspace, so it can never come back
    partially answered and keeps the plain JobListData shape above.
    """

    # Workspace ids whose shard could not be reached for this listing. Non-
    # empty means the page/next_cursor above is missing that workspace's
    # jobs -- paired with a "job_fanout_incomplete" envelope warning so this
    # is visible rather than read as a complete, just-short, result.
    unreachable_workspace_ids: list[UUID]

    model_config = ConfigDict(extra="forbid")


class AdminJobListResponse(RESTResponseBase):
    data: AdminJobListData


# Single producer (the server-admin summary endpoint), so the
# unreachable_workspace_ids signal lives on this model directly rather than in
# a subclass the way AdminJobListData layers over the shared JobListData above.
class JobQueueSummaryData(BaseModel):
    """System-wide background-job aggregate for the server-admin operator
    screen: a scan across every workspace's jobs, summed.
    """

    queued_count: int
    running_count: int
    failed_count: int
    lease_expired_count: int
    # Non-empty means the counts above are a sum over only the reachable
    # workspaces, not every workspace -- paired with a "job_fanout_incomplete"
    # envelope warning for the same reason as AdminJobListData's field.
    unreachable_workspace_ids: list[UUID]

    model_config = ConfigDict(extra="forbid")


class JobQueueSummaryResponse(RESTResponseBase):
    data: JobQueueSummaryData


class JobRetryResponse(RESTResponseBase):
    data: JobRead
