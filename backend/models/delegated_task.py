from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class ClientDelegatedTask(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Borrowed-intelligence task queue (design §17.3.1-c, M0).

    Producers (lint gaps, publish hooks) enqueue Q-* maintenance tasks; the
    Tier-B ``wiki_maintenance`` MCP consumer (M1) and the Tier-S sleep-time
    scheduler drain the SAME queue — the task model, dedup discipline and
    staging paths are tier-shared. Sprint-3 ships producers only.
    """

    __tablename__ = "client_delegated_task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'leased', 'answered', 'staged',"
            " 'dismissed', 'expired')",
            name="client_delegated_task_status_check",
        ),
        CheckConstraint(
            "source IN ('lint', 'coverage_gap', 'publish', 'import')",
            name="client_delegated_task_source_check",
        ),
        Index(
            "idx_cdt_drain",
            "workspace_id",
            "status",
            "priority",
            "created_at",
        ),
        Index(
            "idx_cdt_recon",
            "workspace_id",
            "task_type",
            "target_kind",
            "target_id",
            "fingerprint",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Reconciliation key part: dismissal suppresses re-creation while the
    # same fingerprint holds (lint-reconciliation semantics).
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_ref: Mapped[str | None] = mapped_column(Text)
