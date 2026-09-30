from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class WorkspaceTreeNode(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "workspace_tree_node"
    __table_args__ = (
        CheckConstraint(
            "(node_kind IN ('folder', 'saved_view') AND target_id IS NULL) "
            "OR (node_kind NOT IN ('folder', 'saved_view') "
            "AND target_id IS NOT NULL)",
            name="tree_node_kind_target_match",
        ),
        CheckConstraint(
            "nav_summary IS NULL OR node_kind = 'folder'",
            name="workspace_tree_node_nav_summary_folder_only",
        ),
        Index(
            "workspace_tree_node_primary_placement_uniq",
            "workspace_id",
            "target_kind",
            "target_id",
            unique=True,
            sqlite_where=text("primary_placement = 1 AND target_id IS NOT NULL"),
            postgresql_where=text(
                "primary_placement = true AND target_id IS NOT NULL",
            ),
        ),
        Index(
            "workspace_tree_node_folder_slug_uniq",
            "workspace_id",
            "parent_id",
            "slug",
            unique=True,
            sqlite_where=text("node_kind = 'folder' AND visibility_status = 'visible'"),
            postgresql_where=text(
                "node_kind = 'folder' AND visibility_status = 'visible'",
            ),
        ),
        Index(
            "workspace_tree_node_parent_sort_idx",
            "workspace_id",
            "parent_id",
            "sort_key",
        ),
        Index(
            "workspace_tree_node_section_idx",
            "workspace_id",
            "section",
            "visibility_status",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_workspace_tree_node_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # node-scoped (self-referential -- D-3b).
        ForeignKeyConstraint(
            ["workspace_id", "parent_id"],
            ["workspace_tree_node.workspace_id", "workspace_tree_node.id"],
            name="fk_workspace_tree_node_parent_id_workspace_scoped",
            ondelete="RESTRICT",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    section: Mapped[str] = mapped_column(Text, nullable=False)
    node_kind: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    sort_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(Text)
    target_kind: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    is_system_node: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    can_have_children: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    primary_placement: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    visibility_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="visible",
    )
    # Discriminative folder routing summary (design §5.2). NULL means the
    # render-time heuristic fallback and is never stored by that fallback.
    nav_summary: Mapped[str | None] = mapped_column(Text)
    created_by_actor_type: Mapped[str | None] = mapped_column(Text)
    created_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    parent: Mapped["WorkspaceTreeNode | None"] = relationship(
        remote_side="WorkspaceTreeNode.id",
    )


class TreePolicy(Base):
    __tablename__ = "tree_policy"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    section: Mapped[str] = mapped_column(Text, primary_key=True)
    allow_user_folder_create: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    allow_page_as_parent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    fixed_root_node_ids: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    inherit_permissions_from_parent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    inherit_review_policy_from_parent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    max_depth_warning_threshold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=8,
    )

    @property
    def fixed_root_uuid_ids(self) -> list[UUID]:
        return [UUID(str(root_id)) for root_id in self.fixed_root_node_ids]
