from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin

if TYPE_CHECKING:
    from backend.models.user import User


class ApiToken(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "api_tokens"
    __table_args__ = (
        Index(
            "ix_api_tokens_user_created_id",
            "user_id",
            "created_at",
            "id",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_lookup_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
    )
    hashed_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_preview: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Catalog metadata is presentation-only. Authentication and authorization
    # deliberately ignore these fields and continue to rely on the credential,
    # Workspace binding, live membership/role, and scopes. The label is an
    # immutable issuance-time snapshot so a later catalog rename or tombstone
    # cannot make an existing key unmanageable.
    catalog_app_id: Mapped[str | None] = mapped_column(Text)
    catalog_app_label_snapshot: Mapped[str | None] = mapped_column(Text)
    catalog_revision: Mapped[str | None] = mapped_column(Text)
    # Kept as a compatibility projection for older `/auth/tokens` consumers.
    # New code should prefer the catalog_* fields above.
    mcp_client_type: Mapped[str | None] = mapped_column(Text)
    mcp_client_label: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    user: Mapped["User"] = relationship(back_populates="api_tokens")
    workspaces: Mapped[list["ApiTokenWorkspace"]] = relationship(
        back_populates="token", cascade="all, delete-orphan"
    )


class ApiTokenWorkspace(Base):
    """D-01v2/D-01v3 (control plane): one row per workspace a PAT is allowed
    to reach, replacing the old ``ApiToken.workspace_id`` singular binding.
    Also the control-plane sync INDEX for this credential/workspace pair
    (``desired_scopes``/``revision``/``sync_state``) that the ``workspace_
    credential_grant`` replica in that workspace's OWN database is fanned
    out from -- see the workspace-database-separation design (D-9) and
    .claude/plan/260909-p10-3-multi-workspace-credential.md section 2.6.
    """

    __tablename__ = "api_token_workspaces"
    __table_args__ = (
        Index(
            "ix_api_token_workspaces_workspace_token",
            "workspace_id",
            "token_id",
        ),
    )

    token_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("api_tokens.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # MVP always mirrors the token's own ceiling `scopes` (validated a
    # subset of it when set explicitly) -- see D-01v3-f. Effective scope at
    # request time is ceiling ∩ desired_scopes ∩ the workspace replica's own
    # granted_scopes (section 2.2).
    desired_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sync_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )
    # Set when this row is being shrunk out of the allowed set. The row
    # itself is kept until the workspace-plane replica's tombstone fan-out
    # has actually applied (section 2.6) -- soft delete, not a hard DELETE,
    # so a failed fan-out can be retried against a durable "meant to remove"
    # marker instead of losing intent.
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_sync_error: Mapped[str | None] = mapped_column(Text)

    token: Mapped["ApiToken"] = relationship(back_populates="workspaces")
