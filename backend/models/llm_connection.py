from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
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


def _connection_constraints(table_name: str) -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(
            "status IN ('enabled', 'disabled', 'deleted')",
            name=f"ck_{table_name}_status",
        ),
        CheckConstraint(
            "verification_status IN "
            "('unverified', 'verifying', 'ready', 'failed', 'stale')",
            name=f"ck_{table_name}_verification_status",
        ),
        CheckConstraint(
            "revision >= 1 AND secret_revision >= 0 AND config_schema_version >= 1",
            name=f"ck_{table_name}_revisions",
        ),
        CheckConstraint(
            "status <> 'deleted' OR active_credential IS NULL",
            name=f"ck_{table_name}_deleted_credentials",
        ),
    )


class _ConnectionFields:
    name: Mapped[str] = mapped_column(Text)
    adapter_id: Mapped[str] = mapped_column(Text)
    adapter_implementation_version: Mapped[str] = mapped_column(Text)
    config_schema_version: Mapped[int] = mapped_column()
    api_profile: Mapped[str | None] = mapped_column(Text)
    endpoint_profile: Mapped[dict] = mapped_column(JSON)
    provider_options: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
    model_profiles: Mapped[list[dict]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    active_credential: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    secret_revision: Mapped[int] = mapped_column(default=0, server_default="0")
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    status: Mapped[str] = mapped_column(
        Text, default="disabled", server_default="disabled"
    )
    verification_status: Mapped[str] = mapped_column(
        Text, default="unverified", server_default="unverified"
    )
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    credential_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now
    )


class UserLLMConnection(UuidPrimaryKeyMixin, CreatedAtMixin, _ConnectionFields, Base):
    """A connection owned by one account, independent of its Workspaces."""

    __tablename__ = "user_llm_connection"
    __table_args__ = (
        Index("ix_user_llm_connection_user_id", "user_id"),
        *_connection_constraints(__tablename__),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )


class WorkspaceLLMConnection(
    UuidPrimaryKeyMixin, CreatedAtMixin, _ConnectionFields, Base
):
    """A connection owned by the Workspace, independent of its creator."""

    __tablename__ = "workspace_llm_connection"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "id", name="uq_workspace_llm_connection_scope"
        ),
        *_connection_constraints(__tablename__),
    )

    # The Workspace registry lives on the control plane.
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    allowed_roles: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
