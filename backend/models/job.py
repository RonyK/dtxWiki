from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class Job(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "job_type",
            "idempotency_key",
            name="job_workspace_type_idempotency_uq",
        ),
        # Supports claim_next's scan: status='queued' AND next_attempt_at due,
        # ordered by created_at (FIFO). Without it the claim does a full table
        # scan once the embed workload fills the queue.
        Index("idx_job_claim", "status", "next_attempt_at", "created_at"),
        # Epic-3 D-3 final schema: parent side of the composite FK its
        # children now use.
        UniqueConstraint("workspace_id", "id", name="uq_job_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    stage: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_ref: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    discovery_runs: Mapped[list["LibraryDiscoveryRun"]] = relationship(
        back_populates="job"
    )
    sync_runs: Mapped[list["LibrarySyncRun"]] = relationship(back_populates="job")


from backend.models.library_discovery import (  # noqa: E402
    LibraryDiscoveryRun,
    LibrarySyncRun,
)


class WorkspaceJobHint(Base):
    """Advisory hint: "workspace_id may have queued work".

    Control-plane only -- see backend/services/job_hints.py for
    the write/peek/clear contract and worker/runtime.py's ``_claim_via_hint``
    for the consumer.

    One row per workspace, upserted on every ``JobQueueService.enqueue`` (via
    a session ``after_commit`` listener, so the hint can never precede the
    job's own commit) and compare-and-cleared by the worker after it tries
    that workspace. Row ABSENCE is the "no hint" state deliberately -- there
    is no separate boolean column to drift out of sync with it, the same
    "one source of truth, not two" lesson the media-orphan lifecycle design
    already applied (plan §3 D-7's rejected "new orphan status column").

    The hint is advisory: a false positive (row present, no job found) costs
    one wasted claim attempt; a false negative (row absent, or cleared, while
    a job is still queued) costs latency only -- the round-robin fan-out and
    the periodic full sweep (worker/entry.py) never consult this table and
    still find the job. Nothing reads this table to decide correctness, only
    claim ORDER.
    """

    __tablename__ = "workspace_job_hint"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    hinted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
