from datetime import datetime
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class KnowledgeNodeStat(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_node_stat"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "kind",
            "entity_id",
            name="uq_knowledge_node_stat_entity",
        ),
        Index("idx_knowledge_node_stat_authority", "workspace_id", "authority"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    in_degree: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
