from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.domain.identity_policy import normalize_email, validate_display_name
from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)
from backend.models.normalized_keys import (
    normalize_search_key,
    user_member_search_key_default,
)

if TYPE_CHECKING:
    from backend.models.api_token import ApiToken
    from backend.models.password_reset import PasswordResetToken
    from backend.models.workspace import Workspace, WorkspaceMembership


class User(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "personal_workspace_id",
            name="uq_users_personal_workspace_id",
        ),
    )

    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    member_search_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        index=True,
        default=user_member_search_key_default,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    session_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    default_workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="SET NULL"),
    )
    personal_workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="SET NULL"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    password_reset_tokens: Mapped[list[PasswordResetToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="PasswordResetToken.user_id",
    )
    workspace_memberships: Mapped[list[WorkspaceMembership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    default_workspace: Mapped[Workspace | None] = relationship(
        foreign_keys=[default_workspace_id],
    )
    personal_workspace: Mapped[Workspace | None] = relationship(
        foreign_keys=[personal_workspace_id],
    )

    @validates("email", "display_name")
    def _maintain_member_search_key(self, key: str, value: str) -> str:
        value = (
            normalize_email(value) if key == "email" else validate_display_name(value)
        )
        email = value if key == "email" else (getattr(self, "email", "") or "")
        display_name = (
            value
            if key == "display_name"
            else (getattr(self, "display_name", "") or "")
        )
        self.member_search_key = normalize_search_key(f"{display_name} {email}")
        return value
