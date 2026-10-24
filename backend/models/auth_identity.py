from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class UserIdentity(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """A provider-verified identity bound to one native user account."""

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
        UniqueConstraint(
            "user_id", "provider_slug", name="uq_user_identities_user_provider"
        ),
        CheckConstraint(
            "linked_via IN ('jit', 'jit_email_confirmed', 'auto_verified_email', "
            "'user_link', 'admin', 'scim')",
            name="user_identity_linked_via_check",
        ),
        Index("ix_user_identities_provider_slug", "provider_slug"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_slug: Mapped[str] = mapped_column(Text, nullable=False)
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    email_at_link: Mapped[str | None] = mapped_column(Text)
    last_email: Mapped[str | None] = mapped_column(Text)
    last_email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    linked_via: Mapped[str] = mapped_column(Text, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuthLoginTransaction(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """One browser-bound, single-use external authentication attempt."""

    __tablename__ = "auth_login_transactions"
    __table_args__ = (
        UniqueConstraint(
            "state_digest", name="uq_auth_login_transactions_state_digest"
        ),
        CheckConstraint(
            "purpose IN ('login', 'link', 'reauth')",
            name="auth_login_transaction_purpose_check",
        ),
        Index("ix_auth_login_transactions_expires_at", "expires_at"),
    )

    state_digest: Mapped[str] = mapped_column(Text, nullable=False)
    binding_digest: Mapped[str] = mapped_column(Text, nullable=False)
    provider_slug: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    session_generation: Mapped[int | None] = mapped_column(Integer)
    return_to: Mapped[str] = mapped_column(Text, nullable=False)
    remember_me: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    protocol_request_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
