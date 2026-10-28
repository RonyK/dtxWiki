from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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


class ChangeSet(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "change_set"
    __table_args__ = (
        # Epic-3 D-3 final schema: workspace-scoped, not just session-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "origin_chat_session_id"],
            ["mcp_chat_session.workspace_id", "mcp_chat_session.id"],
            name="fk_change_set_origin_chat_session_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: parent side of the composite FK its
        # children now use.
        UniqueConstraint("workspace_id", "id", name="uq_change_set_workspace_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    origin_chat_session_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="draft_created",
    )
    risk_level: Mapped[str] = mapped_column(Text, nullable=False, default="low")
    cite_call_skipped: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    operations: Mapped[list["ChangeOperation"]] = relationship(
        back_populates="change_set",
        cascade="all, delete-orphan",
    )


class ChangeOperation(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "change_operation"
    __table_args__ = (
        Index("ix_change_operation_change_set_id", "change_set_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just set-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "change_set_id"],
            ["change_set.workspace_id", "change_set.id"],
            name="fk_change_operation_change_set_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `change_set` (see
    # ChangeSet.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    change_set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    diff_summary: Mapped[str | None] = mapped_column(Text)
    diff_ref: Mapped[str | None] = mapped_column(Text)

    change_set: Mapped[ChangeSet] = relationship(back_populates="operations")
