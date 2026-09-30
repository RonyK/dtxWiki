from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import conv

from backend.models.base import Base, CreatedAtMixin, UTCDateTime, UuidPrimaryKeyMixin


class AgentInputBundle(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_input_bundle"
    __table_args__ = (
        # Epic-3 D-3 final schema: workspace-scoped, not just client-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "client_id"],
            ["mcp_client.workspace_id", "mcp_client.id"],
            name="fk_agent_input_bundle_client_id_workspace_scoped",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_agent_input_bundle_workspace_id"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    client_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    task_intent: Mapped[str] = mapped_column(Text, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="generated")
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ttl_override_seconds: Mapped[int | None] = mapped_column(Integer)
    valid_until: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    invalidation_keys: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    expiration_reason: Mapped[str | None] = mapped_column(Text)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    items: Mapped[list["AgentInputBundleItem"]] = relationship(back_populates="bundle")


class AgentInputBundleItem(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_input_bundle_item"
    __table_args__ = (
        CheckConstraint(
            "(source_kind = 'agent_page' "
            "AND agent_page_version_id IS NOT NULL "
            "AND context_page_version_id IS NULL "
            "AND library_artifact_id IS NULL) "
            "OR (source_kind = 'context_page_excerpt' "
            "AND agent_page_version_id IS NULL "
            "AND context_page_version_id IS NOT NULL "
            "AND library_artifact_id IS NULL) "
            "OR (source_kind = 'library_artifact_excerpt' "
            "AND agent_page_version_id IS NULL "
            "AND context_page_version_id IS NULL "
            "AND library_artifact_id IS NOT NULL)",
            name="agent_input_bundle_item_exactly_one_source",
        ),
        CheckConstraint(
            "(included_reason IS NOT NULL AND omission_reason IS NULL) "
            "OR (included_reason IS NULL AND omission_reason IS NOT NULL)",
            name="agent_input_bundle_item_included_xor_omitted",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # bundle-/content-scoped. The three content refs are still one arm
        # each of the source_kind XOR check above.
        ForeignKeyConstraint(
            ["workspace_id", "bundle_id"],
            ["agent_input_bundle.workspace_id", "agent_input_bundle.id"],
            name="fk_agent_input_bundle_item_bundle_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_page_version_id"],
            ["agent_page_version.workspace_id", "agent_page_version.id"],
            name=conv(
                "fk_agent_input_bundle_item_agent_page_version_id_workspace_scoped"
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "context_page_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name=conv(
                "fk_agent_input_bundle_item_context_page_version_id_workspace_scoped"
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "library_artifact_id"],
            ["library_artifact.workspace_id", "library_artifact.id"],
            name="fk_agent_input_bundle_item_library_artifact_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `bundle` (see
    # AgentInputBundle.workspace_id). The 3 content refs below are an XOR
    # pointer to whichever source_kind this row cites, not the tenancy
    # source -- bundle_id always resolves, they may not.
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    bundle_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    agent_page_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    context_page_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    library_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    category_or_type: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    included_range: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    included_reason: Mapped[str | None] = mapped_column(Text)
    omission_reason: Mapped[str | None] = mapped_column(Text)

    bundle: Mapped[AgentInputBundle] = relationship(back_populates="items")
