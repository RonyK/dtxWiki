"""Single-use email confirmation bound to one account session and purpose."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class EmailReauthenticationChallenge(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "email_reauthentication_challenges"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('account', 'admin')", name="email_reauth_scope_check"
        ),
        Index("ix_email_reauth_expires_at", "expires_at"),
        Index("ix_email_reauth_user_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    session_digest: Mapped[str] = mapped_column(Text, nullable=False)
    binding_digest: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_digest: Mapped[str] = mapped_column(Text, nullable=False)
    token_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    return_to: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
