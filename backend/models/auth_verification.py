"""Single-use email confirmation and admin-reviewed email conflict requests."""

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


class EmailConflictRequest(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """A verified SSO identity asking an admin to release an unverified address.

    No account exists for the requester until the owner of ``target_user_id``
    links the identity or a server admin releases the address.
    """

    __tablename__ = "auth_email_conflict_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'resolved', "
            "'auto_released')",
            name="auth_email_conflict_status_check",
        ),
        Index("ix_auth_email_conflict_status_created", "status", "created_at"),
        Index("ix_auth_email_conflict_target", "target_user_id"),
    )

    target_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    provider_slug: Mapped[str] = mapped_column(Text, nullable=False)
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
