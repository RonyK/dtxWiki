from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime, UuidPrimaryKeyMixin, utc_now


class WorkspaceJoinRequest(UuidPrimaryKeyMixin, Base):
    """The single mutable join-request row for one user/workspace pair.

    A new attempt reuses this row and increments ``attempt_no``.  Owner
    decisions carry the expected attempt number, so a response to an older
    notification cannot decide a newer request.
    """

    __tablename__ = "workspace_join_requests"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "requester_user_id",
            name="workspace_join_request_user_workspace_uq",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'closed')",
            name="workspace_join_request_status_check",
        ),
        CheckConstraint(
            "attempt_no >= 1",
            name="workspace_join_request_attempt_check",
        ),
        Index(
            "idx_workspace_join_request_queue",
            "workspace_id",
            "status",
            "requested_at",
            "id",
        ),
        Index(
            "idx_workspace_join_request_requester",
            "requester_user_id",
            "status",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    requester_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    requested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    decision_note: Mapped[str | None] = mapped_column(Text)
    closed_reason: Mapped[str | None] = mapped_column(Text)
