from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime, utc_now


class ServerAdmin(Base):
    """An instance-level (server) admin grant. Sparse: one row per admin, so a
    row's existence *is* the grant. Distinct from workspace RBAC — it confers
    instance/system privileges (deployment status, server settings) only, never
    workspace content access (that stays membership-based)."""

    __tablename__ = "server_admins"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    # Who granted it: a user id (string) or "cli" for shell-access grants.
    granted_by: Mapped[str | None] = mapped_column(Text)
