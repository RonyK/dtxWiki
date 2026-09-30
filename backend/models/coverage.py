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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class KnowledgeCoverageGap(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_coverage_gap"
    __table_args__ = (
        CheckConstraint(
            "surface IN ('ask', 'search_rest', 'search_mcp')",
            name="knowledge_coverage_gap_surface_check",
        ),
        CheckConstraint(
            "status IN ('open', 'triaged', 'dismissed', 'resolved')",
            name="knowledge_coverage_gap_status_check",
        ),
        Index("idx_kcg_open", "workspace_id", "status", "created_at"),
        Index("idx_kcg_recon", "workspace_id", "query_norm"),
        Index(
            "uq_kcg_open_query",
            "workspace_id",
            "query_norm",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    query_norm: Mapped[str] = mapped_column(Text, nullable=False)
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    tried_lanes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    nearest_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    rewrite_attempts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
