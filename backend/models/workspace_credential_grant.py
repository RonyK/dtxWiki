from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class WorkspaceCredentialGrant(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """D-01v3 workspace-plane replica of one control-plane allowed-workspace
    row (``ApiTokenWorkspace`` or ``OAuthConsent``). Deliberately NOT FK'd to
    either control table -- ``credential_id``/``user_id`` are plain UUID
    values (the workspace-database-separation design's D-1: no cross-plane
    FK) -- and deliberately a separate table from ``MCPClient``,
    whose ``(workspace_id, name)`` unique constraint and observed-connection
    semantics answer a different question ("what identity called in") from
    this row's ("is this credential currently allowed to reach this
    workspace at all"). Authorization reads this row; it never reads
    ``MCPClient`` for the allowed-set question.

    Fanned out from the control-plane index row's ``desired_scopes``/
    ``revision`` after that row commits (section 2.6) -- never written
    directly by the request path that grants/revokes access, only by the
    fan-out/reconciler. ``revoked_at`` is this replica's own tombstone,
    separate from the control row's ``removed_at`` (which marks INTENT to
    remove before the replica has caught up).
    """

    __tablename__ = "workspace_credential_grant"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "credential_kind",
            "credential_id",
            name="uq_workspace_credential_grant_target",
        ),
        Index(
            "ix_workspace_credential_grant_workspace_user",
            "workspace_id",
            "user_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # "pat" | "oauth" -- which control-plane table credential_id names.
    credential_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # ApiToken.id for "pat", OAuthRefreshToken.grant_id for "oauth". No FK:
    # this row lives in a DIFFERENT physical database once workspace DB
    # separation is active, so a cross-database foreign key is not
    # representable -- see workspace-database-separation-plan.md D-1.
    credential_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    synced_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )
