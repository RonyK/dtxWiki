from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class FeedbackItem(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "feedback_item"
    __table_args__ = (
        # Epic-3 D-3 final schema: workspace-scoped, not just set-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "resolved_by_change_set_id"],
            ["change_set.workspace_id", "change_set.id"],
            name="fk_feedback_item_resolved_by_change_set_id_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="minor")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolved_by_change_set_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
