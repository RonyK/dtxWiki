from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class LintRun(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "lint_run"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_lint_run_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lint_scope: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {"kind": "workspace"},
        server_default='{"kind":"workspace"}',
    )
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    finding_count_by_severity: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    findings: Mapped[list["LintFinding"]] = relationship(
        back_populates="lint_run",
        cascade="all, delete-orphan",
    )


class LintFinding(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "lint_finding"
    # LG-F5: the Lint screen/API repeatedly query by workspace (+ status) ordered
    # by created_at,id (keyset paging), and run_lint reconciles by
    # (workspace, rule, target, fingerprint) every run. Rows are retained for
    # history, so back these with composite indexes to avoid full scans at scale.
    __table_args__ = (
        Index("idx_lint_finding_ws_created", "workspace_id", "created_at", "id"),
        Index(
            "idx_lint_finding_ws_status_created",
            "workspace_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "idx_lint_finding_recon",
            "workspace_id",
            "rule_id",
            "target_id",
            "fingerprint",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just run-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "lint_run_id"],
            ["lint_run.workspace_id", "lint_run.id"],
            name="fk_lint_finding_lint_run_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    lint_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="warning")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    suggested_action: Mapped[str | None] = mapped_column(Text)
    message_key: Mapped[str | None] = mapped_column(Text)
    message_args: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Stable identity for current-state reconciliation (M3); see LintFindingDraft.
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    lint_run: Mapped[LintRun] = relationship(back_populates="findings")
