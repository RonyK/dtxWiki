"""Mutable, DB-native author overlay for one canonical Library Markdown artifact."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import conv

from backend.models.base import (
    Base,
    CreatedAtMixin,
    UTCDateTime,
    UuidPrimaryKeyMixin,
    utc_now,
)


class LibrarySemanticOverlay(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Editor-authored Markdown layered over an immutable conversion artifact.

    The row is deliberately not a Shared Media artifact: it is bounded text
    state whose identity and revision are fenced by the exact selected base
    artifact. Converter/source bytes remain immutable and independently
    downloadable.
    """

    __tablename__ = "library_semantic_overlay"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_semantic_overlay_id_version_uq",
        ),
        UniqueConstraint(
            "library_item_version_id",
            "base_artifact_id",
            name="library_semantic_overlay_version_base_uq",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped (was a 2-column ownership FK).
        ForeignKeyConstraint(
            ["workspace_id", "base_artifact_id", "library_item_version_id"],
            [
                "library_artifact.workspace_id",
                "library_artifact.id",
                "library_artifact.library_item_version_id",
            ],
            name="fk_library_semantic_overlay_base_artifact_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "length(base_artifact_sha256) = 64",
            name="library_semantic_overlay_base_sha256_check",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="library_semantic_overlay_content_sha256_check",
        ),
        CheckConstraint(
            "byte_size >= 0",
            name="library_semantic_overlay_byte_size_check",
        ),
        CheckConstraint(
            "revision > 0",
            name="library_semantic_overlay_revision_check",
        ),
        Index(
            "idx_library_semantic_overlay_version_base",
            "library_item_version_id",
            "base_artifact_id",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name=conv(
                "fk_library_semantic_overlay_library_item_version_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: parent side of the composite
        # ownership FK library_item_version now uses.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_semantic_overlay_workspace_version_owner",
        ),
    )

    # Epic-3 D-2 Phase B2: denormalized from `library_item_version` (see
    # LibraryItemVersion.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    base_artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    base_artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by_actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    invalidation_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    version = relationship(
        "LibraryItemVersion",
        back_populates="semantic_overlays",
        foreign_keys=[library_item_version_id],
        overlaps="base_artifact,semantic_overlays",
    )
    base_artifact = relationship(
        "LibraryArtifact",
        foreign_keys=[base_artifact_id, library_item_version_id],
        overlaps="semantic_overlays,version",
    )


__all__ = ["LibrarySemanticOverlay"]
