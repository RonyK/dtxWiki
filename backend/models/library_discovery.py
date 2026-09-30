from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import conv

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class LibraryDiscoveryRun(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_discovery_run"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "policy_snapshot_hash",
            "run_token",
            name="library_discovery_run_workspace_policy_token_uq",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_library_discovery_run_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just job-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["job.workspace_id", "job.id"],
            name="fk_library_discovery_run_job_id_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    policy_snapshot_hash: Mapped[str] = mapped_column(Text, nullable=False)
    run_token: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision_summary: Mapped[str | None] = mapped_column(Text)
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    job: Mapped["Job | None"] = relationship(back_populates="discovery_runs")
    candidates: Mapped[list["LibraryDiscoveryCandidate"]] = relationship(
        back_populates="discovery_run"
    )


class LibraryDiscoveryCandidate(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_discovery_candidate"
    __table_args__ = (
        CheckConstraint(
            "(decision_status = 'accepted' AND accepted_library_item_id IS NOT NULL) "
            "OR (decision_status <> 'accepted' AND accepted_library_item_id IS NULL)",
            name="library_discovery_candidate_acceptance_requires_source",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just item-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "accepted_library_item_id"],
            ["library_item.workspace_id", "library_item.id"],
            name=conv(
                "fk_library_discovery_candidate_accepted_library_item_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just run-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "discovery_run_id"],
            ["library_discovery_run.workspace_id", "library_discovery_run.id"],
            name=conv(
                "fk_library_discovery_candidate_discovery_run_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    discovery_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    canonical_uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    connector_type: Mapped[str] = mapped_column(Text, nullable=False)
    format_hint: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    trust_level: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    decision_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending_review",
    )
    decision_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    # ON DELETE CASCADE (not SET NULL): the acceptance CHECK above forbids an
    # accepted candidate without a reference, so deleting the accepted library
    # item removes the candidate row instead of nulling the pointer.
    accepted_library_item_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    discovery_run: Mapped[LibraryDiscoveryRun] = relationship(
        back_populates="candidates"
    )


class LibrarySyncRun(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_sync_run"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_library_sync_run_workspace_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just item-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_id"],
            ["library_item.workspace_id", "library_item.id"],
            name="fk_library_sync_run_library_item_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just job-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["job.workspace_id", "job.id"],
            name="fk_library_sync_run_job_id_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    library_item_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_run_id: Mapped[str | None] = mapped_column(Text)
    webhook_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    changed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    noop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    job: Mapped["Job | None"] = relationship(back_populates="sync_runs")


from backend.models.job import Job  # noqa: E402
