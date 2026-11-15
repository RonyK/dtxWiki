"""Internal migration rollback state, with no application read/write surface."""

from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class DocumentLocatorRollback(Base):
    __tablename__ = "document_locator_rollback"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('update', 'insert', 'delete')",
            name="document_locator_rollback_operation_check",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    record_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    deltas: Mapped[list] = mapped_column(JSON, nullable=False)
    row_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    identity_guard: Mapped[dict] = mapped_column(JSON, nullable=False)
    text_format: Mapped[dict | None] = mapped_column(JSON, nullable=True)
