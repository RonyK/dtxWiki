from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


def default_selection_modifiers() -> dict[str, bool]:
    return {
        "policy_matched": False,
        "recency_aware": False,
        "user_invoked": False,
    }


class AgentPage(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_page"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="agent_page_workspace_slug_uq"),
        UniqueConstraint("workspace_id", "id", name="uq_agent_page_workspace_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "current_active_version_id"],
            ["agent_page_version.workspace_id", "agent_page_version.id"],
            name="fk_agent_page_current_active_version_id_workspace_scoped",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "latest_draft_version_id"],
            ["agent_page_version.workspace_id", "agent_page_version.id"],
            name="fk_agent_page_latest_draft_version_id_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    lifecycle_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="draft",
    )
    injection_policy: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="task_matched",
    )
    injection_trigger: Mapped[str | None] = mapped_column(Text)
    selection_modifiers: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_selection_modifiers,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, default="workspace")
    current_active_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    latest_draft_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    content_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    current_active_version: Mapped["AgentPageVersion | None"] = relationship(
        foreign_keys=[current_active_version_id],
        post_update=True,
    )
    latest_draft_version: Mapped["AgentPageVersion | None"] = relationship(
        foreign_keys=[latest_draft_version_id],
        post_update=True,
    )
    versions: Mapped[list["AgentPageVersion"]] = relationship(
        back_populates="agent_page",
        foreign_keys="AgentPageVersion.agent_page_id",
    )


class AgentPageVersion(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_page_version"
    __table_args__ = (
        UniqueConstraint(
            "agent_page_id",
            "version_no",
            name="agent_page_version_no_uq",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_agent_page_version_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "agent_page_id"],
            ["agent_page.workspace_id", "agent_page.id"],
            name="fk_agent_page_version_agent_page_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `agent_page` (see
    # AgentPage.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    agent_page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_display_name: Mapped[str | None] = mapped_column(Text)
    change_set_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    version_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="draft",
    )
    activation_reason: Mapped[str | None] = mapped_column(Text)

    agent_page: Mapped[AgentPage] = relationship(
        back_populates="versions",
        foreign_keys=[agent_page_id],
    )
