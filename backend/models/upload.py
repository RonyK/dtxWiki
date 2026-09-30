from uuid import UUID

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin


class UploadAsset(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Content-addressed upload plus the actor who staged it."""

    __tablename__ = "upload_asset"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "marker_uuid",
            name="uq_upload_asset_workspace_marker",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_upload_asset_workspace_id"),
        Index("idx_upload_asset_workspace_filename", "workspace_id", "filename"),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_id_hint"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_upload_asset_page_id_hint_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    marker_uuid: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    page_id_hint: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)


class UploadAssetBinding(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """A verified Markdown reference from a Context version to an upload."""

    __tablename__ = "upload_asset_binding"
    __table_args__ = (
        UniqueConstraint(
            "upload_asset_id",
            "page_version_id",
            name="uq_upload_asset_binding_version",
        ),
        Index("idx_upload_asset_binding_version", "page_version_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_upload_asset_binding_page_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just asset-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "upload_asset_id"],
            ["upload_asset.workspace_id", "upload_asset.id"],
            name="fk_upload_asset_binding_upload_asset_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # page-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "page_version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_upload_asset_binding_page_version_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    upload_asset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    page_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
