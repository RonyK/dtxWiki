"""Browser-bound external signup and purpose-bound email verification."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class AuthPendingSignup(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "auth_pending_signups"
    __table_args__ = (Index("ix_auth_pending_signups_expires_at", "expires_at"),)

    binding_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider_slug: Mapped[str] = mapped_column(Text, nullable=False)
    assertion_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    return_to: Mapped[str] = mapped_column(Text, nullable=False)
    remember_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    token_digest: Mapped[str | None] = mapped_column(Text, unique=True)
    email_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EmailReauthenticationChallenge(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "email_reauthentication_challenges"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('account', 'admin')", name="email_reauth_scope_check"
        ),
        Index("ix_email_reauth_expires_at", "expires_at"),
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
