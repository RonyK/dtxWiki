from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.models.user import User


class PasswordResetToken(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name="password_reset_token_purpose_check",
        ),
        CheckConstraint(
            "delivery IN ('email', 'link')",
            name="password_reset_token_delivery_check",
        ),
        CheckConstraint(
            "requested_via IN ('public', 'server_admin', 'cli')",
            name="password_reset_token_requested_via_check",
        ),
        Index(
            "idx_password_reset_token_user_purpose",
            "user_id",
            "purpose",
            "created_at",
        ),
        Index(
            "uq_password_reset_token_active_user_purpose",
            "user_id",
            "purpose",
            unique=True,
            sqlite_where=text("used_at IS NULL AND revoked_at IS NULL"),
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    delivery: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    requested_via: Mapped[str] = mapped_column(Text, nullable=False)
    revoke_api_tokens: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    user: Mapped[User] = relationship(
        back_populates="password_reset_tokens",
        foreign_keys=[user_id],
    )
