from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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

if TYPE_CHECKING:
    from backend.models.library_semantic_overlay import LibrarySemanticOverlay


class LibraryItem(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_item"
    __table_args__ = (
        UniqueConstraint("workspace_id", "uri", name="library_item_workspace_uri_uq"),
        # Epic-3 D-3 final schema: parent side of the composite FK its
        # children now use.
        UniqueConstraint("workspace_id", "id", name="uq_library_item_workspace_id"),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # item-scoped (was a 2-column ownership FK). Each column also
        # separately carries its own bare single-column FK
        # (`fk_library_item_current_ready_version_id_library_item_version`
        # / `fk_library_item_current_preview_version_id`, both ON DELETE
        # SET NULL) from before Epic-3 -- untouched here since it's a
        # stricter-is-fine redundancy (any row satisfying this ownership FK
        # already satisfies that one) and changing its ON DELETE behavior
        # is out of scope for this slice.
        ForeignKeyConstraint(
            ["workspace_id", "current_ready_version_id", "id"],
            [
                "library_item_version.workspace_id",
                "library_item_version.id",
                "library_item_version.library_item_id",
            ],
            name="fk_library_item_current_ready_version_id_workspace_scoped",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "current_preview_version_id", "id"],
            [
                "library_item_version.workspace_id",
                "library_item_version.id",
                "library_item_version.library_item_id",
            ],
            name="fk_library_item_current_preview_version_id_workspace_scoped",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "current_version_id", "id"],
            [
                "library_item_version.workspace_id",
                "library_item_version.id",
                "library_item_version.library_item_id",
            ],
            name="fk_library_item_current_version_id_workspace_scoped",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    connector_type: Mapped[str] = mapped_column(Text, nullable=False)
    format_hint: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    trust_level: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    lifecycle_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="registered",
    )
    current_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("library_item_version.id", ondelete="SET NULL"),
    )
    current_ready_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("library_item_version.id", ondelete="SET NULL"),
        index=True,
    )
    current_preview_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("library_item_version.id", ondelete="SET NULL"),
        index=True,
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

    current_version: Mapped["LibraryItemVersion | None"] = relationship(
        foreign_keys=[current_version_id],
        post_update=True,
    )
    current_ready_version: Mapped["LibraryItemVersion | None"] = relationship(
        foreign_keys=[current_ready_version_id],
        post_update=True,
    )
    current_preview_version: Mapped["LibraryItemVersion | None"] = relationship(
        foreign_keys=[current_preview_version_id],
        post_update=True,
    )
    versions: Mapped[list["LibraryItemVersion"]] = relationship(
        back_populates="library_item",
        foreign_keys="LibraryItemVersion.library_item_id",
    )


class LibraryItemVersion(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_item_version"
    __table_args__ = (
        UniqueConstraint(
            "library_item_id", "version_no", name="library_item_version_no_uq"
        ),
        UniqueConstraint(
            "id",
            "library_item_id",
            name="library_item_version_id_item_uq",
        ),
        # Epic-3 D-3 final schema: parent side of the composite
        # ownership FK library_item now uses.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_id",
            name="uq_library_item_version_workspace_item_owner",
        ),
        # Epic-3 D-3 final schema: parent side of the composite FK its 8
        # direct children now use.
        UniqueConstraint(
            "workspace_id", "id", name="uq_library_item_version_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped -- these were already 2-column "ownership" FKs
        # (proving the selected row belongs to this exact version); adding
        # workspace_id as a 3rd component closes the tenancy gap the
        # ownership check alone didn't cover.
        # A-1: conversion and preview_generation attempts now share one
        # table (LibraryAttempt), so both selected-attempt pointers target
        # the same parent -- they remain two separate columns/FKs because
        # each stage keeps its own independently-selected canonical result.
        ForeignKeyConstraint(
            ["workspace_id", "selected_conversion_attempt_id", "id"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
            ],
            name=conv(
                "fk_library_item_version_selected_conversion_attempt_id_workspace_scoped"
            ),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "selected_preview_attempt_id", "id"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
            ],
            name=conv(
                "fk_library_item_version_selected_preview_attempt_id_workspace_scoped"
            ),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "selected_semantic_overlay_id", "id"],
            [
                "library_semantic_overlay.workspace_id",
                "library_semantic_overlay.id",
                "library_semantic_overlay.library_item_version_id",
            ],
            name=conv(
                "fk_library_item_version_selected_semantic_overlay_id_workspace_scoped"
            ),
        ),
        ForeignKeyConstraint(
            ["source_blob_workspace_id", "source_blob_id", "source_blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_library_item_version_source_blob_incarnation",
            ondelete="RESTRICT",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just run-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "sync_run_id"],
            ["library_sync_run.workspace_id", "library_sync_run.id"],
            name="fk_library_item_version_sync_run_id_workspace_scoped",
        ),
        CheckConstraint(
            "(source_blob_workspace_id IS NULL AND source_blob_id IS NULL AND "
            "source_blob_epoch IS NULL) OR "
            "(source_blob_workspace_id IS NOT NULL AND source_blob_id IS NOT NULL "
            "AND source_blob_epoch > 0)",
            name="library_item_version_source_blob_reference_check",
        ),
        CheckConstraint(
            "source_blob_workspace_id IS NULL OR "
            "source_blob_workspace_id = workspace_id",
            name="library_item_version_source_blob_workspace_check",
        ),
        CheckConstraint(
            "source_blob_id IS NOT NULL OR raw_storage_ref IS NOT NULL",
            name="library_item_version_source_check",
        ),
        CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name="library_item_version_byte_size_check",
        ),
        CheckConstraint(
            "source_blob_id IS NULL OR (original_filename IS NOT NULL AND "
            "detected_media_type IS NOT NULL AND format_id IS NOT NULL AND "
            "byte_size IS NOT NULL AND detection_json IS NOT NULL AND "
            "length(content_hash) = 64)",
            name="library_item_version_detected_source_metadata_check",
        ),
        CheckConstraint(
            "processing_status IN ("
            "'pending', 'completed', 'queued', 'converting', 'projecting', "
            "'ready', 'ready_partial', 'stored_unconverted', 'failed', "
            "'timed_out')",
            name="library_item_version_processing_status_check",
        ),
        CheckConstraint(
            "preview_status IN ("
            "'queued', 'generating', 'ready', 'ready_partial', "
            "'unavailable', 'failed', 'timed_out')",
            name="library_item_version_preview_status_check",
        ),
        Index(
            "idx_library_item_version_processing_claim",
            "processing_status",
            "created_at",
        ),
        Index(
            "idx_library_item_version_source_blob",
            "source_blob_workspace_id",
            "source_blob_id",
            "source_blob_epoch",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just item-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_id"],
            ["library_item.workspace_id", "library_item.id"],
            name="fk_library_item_version_library_item_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped (self-referential -- D-3b).
        ForeignKeyConstraint(
            ["workspace_id", "previous_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name="fk_library_item_version_previous_version_id_workspace_scoped",
        ),
    )

    # Epic-3 D-2: denormalized from `library_item` (see LibraryItem.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    library_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    raw_storage_ref: Mapped[str | None] = mapped_column(Text)
    source_blob_workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    source_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    source_blob_epoch: Mapped[int | None] = mapped_column(BigInteger)
    original_filename: Mapped[str | None] = mapped_column(Text)
    declared_media_type: Mapped[str | None] = mapped_column(Text)
    detected_media_type: Mapped[str | None] = mapped_column(Text)
    format_id: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    detection_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    processing_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
    )
    extraction_summary: Mapped[str | None] = mapped_column(Text)
    previous_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    sync_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
    )
    change_summary: Mapped[str | None] = mapped_column(Text)
    selected_conversion_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
    )
    selected_preview_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
    )
    selected_semantic_overlay_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
    )
    preview_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="unavailable",
        server_default="unavailable",
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    library_item: Mapped[LibraryItem] = relationship(
        back_populates="versions",
        foreign_keys=[library_item_id],
    )
    artifacts: Mapped[list["LibraryArtifact"]] = relationship(back_populates="version")
    # A-1: one shared run/attempt storage for both stages (conversion and
    # preview_generation) -- see LibraryRun.stage / LibraryAttempt.stage.
    runs: Mapped[list["LibraryRun"]] = relationship(
        back_populates="library_item_version",
        cascade="all, delete-orphan",
    )
    attempts: Mapped[list["LibraryAttempt"]] = relationship(
        back_populates="library_item_version",
        foreign_keys="LibraryAttempt.library_item_version_id",
    )
    selected_conversion_attempt: Mapped["LibraryAttempt | None"] = relationship(
        foreign_keys=[selected_conversion_attempt_id],
        post_update=True,
    )
    selected_preview_attempt: Mapped["LibraryAttempt | None"] = relationship(
        foreign_keys=[selected_preview_attempt_id],
        post_update=True,
    )
    semantic_overlays: Mapped[list["LibrarySemanticOverlay"]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        foreign_keys="LibrarySemanticOverlay.library_item_version_id",
        overlaps="base_artifact,version",
    )
    selected_semantic_overlay: Mapped["LibrarySemanticOverlay | None"] = relationship(
        foreign_keys=[selected_semantic_overlay_id],
        post_update=True,
        overlaps="semantic_overlays,version",
    )


# A-1: conversion and
# preview_generation used to be two independently-declared table families
# (LibraryConversionRun/Attempt and LibraryPreviewRun/Attempt/Artifact) with
# ~21/26 identical Attempt columns and a near-identical Run shape. They are
# now one common Run/Attempt/Artifact storage, stage-discriminated, with
# stage-specific fields moved into 1:1 extension tables rather than left as
# nullable columns on the common row (invariant 2: "단계별 필드는 typed 1:1
# 확장 또는 명시적 판별 schema로 표현한다... nullable column/임의 JSON/거대한
# if-stage 분기에 몰아넣지 않는다"). `reason`/`status` are the one exception,
# kept as single columns with a stage-conditioned CHECK (invariant 2's other
# sanctioned option, "명시적 판별 schema"): they share most of their
# vocabulary (status shares 6 of 7 values across stages) and back the one
# shared claim/poll queue index both stage workers use.
LIBRARY_RUN_STAGES = ("conversion", "preview_generation")


class LibraryRun(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_run"
    __table_args__ = (
        UniqueConstraint(
            "library_item_version_id",
            "idempotency_key",
            name="library_run_version_idempotency_uq",
        ),
        UniqueConstraint(
            "library_item_version_id",
            "run_seq",
            name="library_run_version_seq_uq",
        ),
        UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_run_id_version_uq",
        ),
        # Backs both LibraryRunConversionDetail's and
        # LibraryRunPreviewDetail's 1:1 ownership FK, and lets a child
        # detail row's own FK prove (not just assume) it matches the
        # parent run's stage.
        UniqueConstraint(
            "id",
            "stage",
            name="uq_library_run_id_stage",
        ),
        CheckConstraint(
            f"stage IN {LIBRARY_RUN_STAGES!r}",
            name="library_run_stage_check",
        ),
        CheckConstraint(
            "(stage = 'conversion' AND reason IN "
            "('initial', 'manual_reconvert', 'policy_migration')) OR "
            "(stage = 'preview_generation' AND reason IN "
            "('initial', 'manual_regenerate', 'dependency_change'))",
            name="library_run_reason_check",
        ),
        CheckConstraint(
            "(stage = 'conversion' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'stored_unconverted')) OR "
            "(stage = 'preview_generation' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'unavailable'))",
            name="library_run_status_check",
        ),
        CheckConstraint("run_seq > 0", name="library_run_seq_check"),
        # Serves both stage workers' claim/poll queries without one stage's
        # backlog ever making the other scan past irrelevant rows.
        Index(
            "idx_library_run_claim",
            "stage",
            "status",
            "created_at",
        ),
        Index(
            "idx_library_run_version_status",
            "library_item_version_id",
            "stage",
            "status",
            "run_seq",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name=conv("fk_library_run_library_item_version_id_workspace_scoped"),
            ondelete="CASCADE",
        ),
        # Epic-3 D-3 final schema: parent side of the composite
        # ownership FK library_attempt now uses.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_run_workspace_version_owner",
        ),
        # SQLite requires a composite FK's parent columns to exactly match a
        # UNIQUE constraint on the parent table (not merely be implied by
        # overlapping narrower ones) or every INSERT/UPDATE/DELETE that
        # touches the relationship raises "foreign key mismatch" --
        # library_attempt.fk_library_attempt_run_id_workspace_scoped
        # references (workspace_id, id, library_item_version_id, stage) as
        # a unit, one column wider than uq_library_run_workspace_version_
        # owner above. `id` alone is already the primary key, so this
        # widened constraint can never reject a legitimate row.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            "stage",
            name="uq_library_run_workspace_version_stage_owner",
        ),
    )

    # Epic-3 D-2: denormalized from `library_item_version` (see
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
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    run_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    requested_by_actor_type: Mapped[str | None] = mapped_column(Text)
    requested_by_actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    provider_chain_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    policy_revision: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    library_item_version: Mapped[LibraryItemVersion] = relationship(
        back_populates="runs",
        foreign_keys=[library_item_version_id],
    )
    attempts: Mapped[list["LibraryAttempt"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        overlaps="attempts",
    )
    conversion_detail: Mapped["LibraryRunConversionDetail | None"] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys=(
            "[LibraryRunConversionDetail.workspace_id, "
            "LibraryRunConversionDetail.run_id, "
            "LibraryRunConversionDetail.library_item_version_id]"
        ),
    )
    preview_detail: Mapped["LibraryRunPreviewDetail | None"] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys=(
            "[LibraryRunPreviewDetail.workspace_id, "
            "LibraryRunPreviewDetail.run_id, "
            "LibraryRunPreviewDetail.library_item_version_id]"
        ),
    )


class LibraryRunConversionDetail(Base):
    """1:1 conversion-only extension of LibraryRun (stage='conversion')."""

    __tablename__ = "library_run_conversion_detail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "stage"],
            ["library_run.id", "library_run.stage"],
            name="fk_library_run_conversion_detail_stage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "library_item_version_id"],
            [
                "library_run.workspace_id",
                "library_run.id",
                "library_run.library_item_version_id",
            ],
            name=conv("fk_library_run_conversion_detail_run_id_workspace_scoped"),
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    stage: Mapped[str] = mapped_column(
        Text, nullable=False, default="conversion", server_default="conversion"
    )
    error_fallback_matrix_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    per_provider_budget_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    total_budget_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    run: Mapped[LibraryRun] = relationship(
        back_populates="conversion_detail",
        foreign_keys=[workspace_id, run_id, library_item_version_id],
    )


class LibraryRunPreviewDetail(Base):
    """1:1 preview_generation-only extension of LibraryRun."""

    __tablename__ = "library_run_preview_detail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "stage"],
            ["library_run.id", "library_run.stage"],
            name="fk_library_run_preview_detail_stage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "library_item_version_id"],
            [
                "library_run.workspace_id",
                "library_run.id",
                "library_run.library_item_version_id",
            ],
            name=conv("fk_library_run_preview_detail_run_id_workspace_scoped"),
            ondelete="CASCADE",
        ),
        # expected_selected_preview_attempt_id names an attempt that, if it
        # exists at all, must belong to this same run.
        ForeignKeyConstraint(
            ["run_id", "expected_selected_preview_attempt_id"],
            ["library_attempt.run_id", "library_attempt.id"],
            name="fk_library_run_preview_detail_expected_attempt",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    stage: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="preview_generation",
        server_default="preview_generation",
    )
    recipe_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    limits_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    input_dependency_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    renderer_contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    expected_selected_preview_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True)
    )

    run: Mapped[LibraryRun] = relationship(
        back_populates="preview_detail",
        foreign_keys=[workspace_id, run_id, library_item_version_id],
    )


class LibraryAttempt(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_attempt"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "chain_index",
            name="library_attempt_run_chain_uq",
        ),
        UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_attempt_id_version_uq",
        ),
        # Backs both detail tables' 1:1 ownership FK, same reasoning as
        # LibraryRun's uq_library_run_id_stage.
        UniqueConstraint(
            "id",
            "stage",
            name="uq_library_attempt_id_stage",
        ),
        CheckConstraint(
            f"stage IN {LIBRARY_RUN_STAGES!r}",
            name="library_attempt_stage_check",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped (was a 2-column ownership FK). Also proves this
        # attempt's own `stage` matches its parent run's.
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "library_item_version_id", "stage"],
            [
                "library_run.workspace_id",
                "library_run.id",
                "library_run.library_item_version_id",
                "library_run.stage",
            ],
            name=conv("fk_library_attempt_run_id_workspace_scoped"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(stage = 'conversion' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'unsupported')) OR "
            "(stage = 'preview_generation' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'unsupported'))",
            name="library_attempt_status_check",
        ),
        CheckConstraint(
            "chain_index >= 0",
            name="library_attempt_chain_index_check",
        ),
        CheckConstraint(
            "transport_retry_count >= 0",
            name="library_attempt_retry_count_check",
        ),
        CheckConstraint(
            "execution_generation > 0",
            name="library_attempt_generation_check",
        ),
        CheckConstraint(
            "length(input_sha256) = 64",
            name="library_attempt_input_sha256_check",
        ),
        Index(
            "idx_library_attempt_claim",
            "stage",
            "status",
            "created_at",
        ),
        Index(
            "idx_library_attempt_version_status",
            "library_item_version_id",
            "stage",
            "status",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name=conv("fk_library_attempt_library_item_version_id_workspace_scoped"),
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_library_attempt_workspace_id"
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # attempt-scoped (self-referential -- D-3b).
        ForeignKeyConstraint(
            ["workspace_id", "parent_attempt_id"],
            ["library_attempt.workspace_id", "library_attempt.id"],
            name="fk_library_attempt_parent_id_workspace_scoped",
        ),
        # Epic-3 D-3 final schema: parent side of the composite
        # ownership FKs library_artifact/library_projection_outbox/
        # library_item_version now use.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_attempt_workspace_version_owner",
        ),
        # SQLite exact-match FK parent-key requirement (see library_run's
        # uq_library_run_workspace_version_stage_owner for the full
        # rationale). library_artifact.fk_library_artifact_attempt_id_
        # workspace_scoped and library_projection_outbox.fk_library_
        # projection_outbox_conversion_attempt_id_workspace_scoped both
        # reference (workspace_id, id, library_item_version_id, stage) as a
        # unit.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            "stage",
            name="uq_library_attempt_workspace_version_stage_owner",
        ),
        # Same SQLite requirement: library_run_preview_detail.
        # fk_library_run_preview_detail_expected_attempt references
        # (run_id, id) as a unit.
        UniqueConstraint(
            "run_id",
            "id",
            name="uq_library_attempt_run_id",
        ),
    )

    # Epic-3 D-2: denormalized from `library_item_version` (see
    # LibraryItemVersion.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    # Renamed from conversion's converter_id/converter_version/
    # converter_spi_version and preview's provider_id/provider_version/
    # provider_spi_version -- the same concept ("which plugin executed
    # this") under two different names. This unified table uses preview's
    # naming since it is the more stage-neutral of the two.
    provider_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_version: Mapped[str] = mapped_column(Text, nullable=False)
    provider_spi_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    chain_index: Mapped[int] = mapped_column(Integer, nullable=False)
    transport_retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    execution_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    parent_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_code: Mapped[str | None] = mapped_column(Text)
    safe_error_summary: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    provenance_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    run: Mapped[LibraryRun] = relationship(
        back_populates="attempts",
        foreign_keys=[run_id],
    )
    library_item_version: Mapped[LibraryItemVersion] = relationship(
        back_populates="attempts",
        foreign_keys=[library_item_version_id],
        overlaps="attempts,run",
    )
    artifacts: Mapped[list["LibraryArtifact"]] = relationship(
        back_populates="attempt",
        overlaps="artifacts,version",
    )
    projection_outbox: Mapped[list["LibraryProjectionOutbox"]] = relationship(
        back_populates="conversion_attempt",
        cascade="all, delete-orphan",
    )
    conversion_detail: Mapped["LibraryAttemptConversionDetail | None"] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys=(
            "[LibraryAttemptConversionDetail.workspace_id, "
            "LibraryAttemptConversionDetail.attempt_id, "
            "LibraryAttemptConversionDetail.library_item_version_id]"
        ),
    )
    preview_detail: Mapped["LibraryAttemptPreviewDetail | None"] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys=(
            "[LibraryAttemptPreviewDetail.workspace_id, "
            "LibraryAttemptPreviewDetail.attempt_id, "
            "LibraryAttemptPreviewDetail.library_item_version_id]"
        ),
    )


class LibraryAttemptConversionDetail(Base):
    """1:1 conversion-only extension of LibraryAttempt (stage='conversion')."""

    __tablename__ = "library_attempt_conversion_detail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "stage"],
            ["library_attempt.id", "library_attempt.stage"],
            name="fk_library_attempt_conversion_detail_stage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "library_item_version_id"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
            ],
            name=conv(
                "fk_library_attempt_conv_detail_attempt_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "manual_retry_disposition IS NULL OR "
            "manual_retry_disposition IN ('allowed', 'denied')",
            name="library_attempt_conv_detail_manual_retry_disposition_check",
        ),
        CheckConstraint(
            "(manual_retry_disposition IS NULL AND disposition_reason IS NULL) OR "
            "(manual_retry_disposition IS NOT NULL AND "
            "disposition_reason IS NOT NULL AND "
            "length(disposition_reason) BETWEEN 1 AND 128)",
            name="library_attempt_conversion_detail_retry_disposition_pair_check",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    stage: Mapped[str] = mapped_column(
        Text, nullable=False, default="conversion", server_default="conversion"
    )
    manual_retry_disposition: Mapped[str | None] = mapped_column(Text)
    disposition_reason: Mapped[str | None] = mapped_column(Text)

    attempt: Mapped[LibraryAttempt] = relationship(
        back_populates="conversion_detail",
        foreign_keys=[workspace_id, attempt_id, library_item_version_id],
    )


class LibraryAttemptPreviewDetail(Base):
    """1:1 preview_generation-only extension of LibraryAttempt."""

    __tablename__ = "library_attempt_preview_detail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "stage"],
            ["library_attempt.id", "library_attempt.stage"],
            name="fk_library_attempt_preview_detail_stage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "library_item_version_id"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
            ],
            name=conv(
                "fk_library_attempt_preview_detail_attempt_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
        # Renamed from input_conversion_attempt_id: the input is now always
        # an attempt reference (validated by application code to actually
        # be a conversion-stage one when input_kind='conversion_artifact' --
        # a plain CHECK cannot cross-reference another table's row).
        ForeignKeyConstraint(
            ["workspace_id", "input_attempt_id"],
            ["library_attempt.workspace_id", "library_attempt.id"],
            name=conv(
                "fk_library_attempt_prev_detail_input_attempt_id_ws_scoped"
            ),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "input_artifact_id"],
            ["library_artifact.workspace_id", "library_artifact.id"],
            name=conv(
                "fk_library_attempt_prev_detail_input_artifact_id_ws_scoped"
            ),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(provider_distribution_digest) = 64",
            name="library_attempt_preview_detail_provider_digest_check",
        ),
        CheckConstraint(
            "((input_kind = 'source' AND input_attempt_id IS NULL "
            "AND input_artifact_id IS NULL) OR "
            "(input_kind = 'conversion_artifact' "
            "AND input_attempt_id IS NOT NULL "
            "AND input_artifact_id IS NOT NULL))",
            name="library_attempt_preview_detail_input_dependency_check",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    stage: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="preview_generation",
        server_default="preview_generation",
    )
    provider_distribution_digest: Mapped[str] = mapped_column(Text, nullable=False)
    input_kind: Mapped[str] = mapped_column(Text, nullable=False)
    input_attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    input_artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    renderer_contract_version: Mapped[str] = mapped_column(Text, nullable=False)

    attempt: Mapped[LibraryAttempt] = relationship(
        back_populates="preview_detail",
        foreign_keys=[workspace_id, attempt_id, library_item_version_id],
    )


class LibraryArtifact(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Unified derived-file identity (A-1).

    Two axes, deliberately not conflated (invariant 5): `role` is WHY the
    artifact exists (search_text, location_map, preview, metadata, asset --
    conversion's historical vocabulary), `kind` is WHAT it is represented as
    (markdown, text, table, pdf, image, audio, video, metadata -- preview's
    historical vocabulary). The doc does not rename these to "purpose"/
    "representation"; it only requires both stay distinct columns.

    The legacy nullable-attempt / storage_ref-only shape is retired
    (§6.3): every artifact now belongs to a real LibraryAttempt and a real
    verified blob, full stop.
    """

    __tablename__ = "library_artifact"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_artifact_id_version_uq",
        ),
        UniqueConstraint(
            "attempt_id",
            "role",
            "ordinal",
            name="library_artifact_attempt_role_ordinal_uq",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped (was a 2-column ownership FK). Also proves this
        # artifact's own `stage` matches its parent attempt's.
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "library_item_version_id", "stage"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
                "library_attempt.stage",
            ],
            name="fk_library_artifact_attempt_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["blob_workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_library_artifact_blob_incarnation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"stage IN {LIBRARY_RUN_STAGES!r}",
            name="library_artifact_stage_check",
        ),
        CheckConstraint(
            "blob_workspace_id = workspace_id",
            name="library_artifact_blob_workspace_check",
        ),
        CheckConstraint(
            "role IN ("
            "'markdown', 'search_text', 'location_map', 'preview', 'asset', "
            "'metadata')",
            name="library_artifact_role_check",
        ),
        CheckConstraint(
            "kind IN ('markdown', 'text', 'table', 'pdf', 'image', 'audio', "
            "'video', 'metadata')",
            name="library_artifact_kind_check",
        ),
        CheckConstraint(
            "byte_size >= 0",
            name="library_artifact_byte_size_check",
        ),
        CheckConstraint(
            "ordinal >= 0",
            name="library_artifact_ordinal_check",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="library_artifact_content_sha256_check",
        ),
        Index("idx_library_artifact_attempt_role", "attempt_id", "role"),
        Index(
            "idx_library_artifact_blob",
            "blob_workspace_id",
            "blob_id",
            "blob_epoch",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name="fk_library_artifact_library_item_version_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_library_artifact_workspace_id"),
        # Backs both detail tables' 1:1 ownership FK.
        UniqueConstraint(
            "id",
            "stage",
            name="uq_library_artifact_id_stage",
        ),
        # Epic-3 D-3 final schema: parent side of the composite
        # ownership FK library_semantic_overlay now uses.
        UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_artifact_workspace_version_owner",
        ),
    )

    # Epic-3 D-2: a gap the plan's own audit missed -- this table had no
    # workspace_id at all (blob_workspace_id is the *referenced blob's*
    # workspace, not this row's own tenancy, same distinction the plan
    # draws for library_item_version.source_blob_workspace_id). Denormalized
    # from `library_item_version` (see LibraryItemVersion.workspace_id).
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id"),
        nullable=False,
    )
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    blob_workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    version: Mapped[LibraryItemVersion] = relationship(
        back_populates="artifacts",
        overlaps="artifacts,attempt",
    )
    attempt: Mapped[LibraryAttempt] = relationship(
        back_populates="artifacts",
        foreign_keys=[attempt_id, library_item_version_id],
        overlaps="artifacts,version",
    )
    conversion_detail: Mapped["LibraryArtifactConversionDetail | None"] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys=(
            "[LibraryArtifactConversionDetail.workspace_id, "
            "LibraryArtifactConversionDetail.artifact_id, "
            "LibraryArtifactConversionDetail.library_item_version_id]"
        ),
    )
    preview_detail: Mapped["LibraryArtifactPreviewDetail | None"] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys=(
            "[LibraryArtifactPreviewDetail.workspace_id, "
            "LibraryArtifactPreviewDetail.artifact_id, "
            "LibraryArtifactPreviewDetail.library_item_version_id]"
        ),
    )


class LibraryArtifactConversionDetail(Base):
    """1:1 conversion-only extension of LibraryArtifact (stage='conversion')."""

    __tablename__ = "library_artifact_conversion_detail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_id", "stage"],
            ["library_artifact.id", "library_artifact.stage"],
            name="fk_library_artifact_conversion_detail_stage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "artifact_id", "library_item_version_id"],
            [
                "library_artifact.workspace_id",
                "library_artifact.id",
                "library_artifact.library_item_version_id",
            ],
            name=conv(
                "fk_library_artifact_conv_detail_artifact_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
    )

    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    stage: Mapped[str] = mapped_column(
        Text, nullable=False, default="conversion", server_default="conversion"
    )
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sanitizer_version: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(Text)
    index_text: Mapped[str | None] = mapped_column(Text)
    location_model: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    artifact: Mapped[LibraryArtifact] = relationship(
        back_populates="conversion_detail",
        foreign_keys=[workspace_id, artifact_id, library_item_version_id],
    )


class LibraryArtifactPreviewDetail(Base):
    """1:1 preview_generation-only extension of LibraryArtifact."""

    __tablename__ = "library_artifact_preview_detail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_id", "stage"],
            ["library_artifact.id", "library_artifact.stage"],
            name="fk_library_artifact_preview_detail_stage",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "artifact_id", "library_item_version_id"],
            [
                "library_artifact.workspace_id",
                "library_artifact.id",
                "library_artifact.library_item_version_id",
            ],
            name=conv(
                "fk_library_artifact_preview_detail_artifact_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "delivery IN ('inline', 'range', 'text')",
            name="library_artifact_preview_detail_delivery_check",
        ),
        CheckConstraint(
            "rank >= 0",
            name="library_artifact_preview_detail_rank_check",
        ),
    )

    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    library_item_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    stage: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="preview_generation",
        server_default="preview_generation",
    )
    renderer_contract: Mapped[str] = mapped_column(Text, nullable=False)
    delivery: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )

    artifact: Mapped[LibraryArtifact] = relationship(
        back_populates="preview_detail",
        foreign_keys=[workspace_id, artifact_id, library_item_version_id],
    )


class LibraryProjectionOutbox(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "library_projection_outbox"
    __table_args__ = (
        UniqueConstraint(
            "conversion_attempt_id",
            "projection_kind",
            "generation",
            name="library_projection_outbox_attempt_kind_generation_uq",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # version-scoped (was a 2-column ownership FK). The extra `stage`
        # column/FK-leg is a fixed 'conversion' shadow (projection outbox
        # is a conversion-only concept -- FTS/semantic/citation projection
        # and the legacy 'preview'-kind trigger both only ever fire off a
        # completed conversion attempt), enforced at the DB level rather
        # than trusted to application code alone.
        ForeignKeyConstraint(
            [
                "workspace_id",
                "conversion_attempt_id",
                "library_item_version_id",
                "stage",
            ],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
                "library_attempt.stage",
            ],
            name=conv(
                "fk_library_projection_outbox_conversion_attempt_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "stage = 'conversion'",
            name="library_projection_outbox_stage_check",
        ),
        CheckConstraint(
            "projection_kind IN ('fts', 'semantic', 'citation', 'preview')",
            name="library_projection_outbox_kind_check",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="library_projection_outbox_status_check",
        ),
        CheckConstraint(
            "generation > 0",
            name="library_projection_outbox_generation_check",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="library_projection_outbox_retry_count_check",
        ),
        Index(
            "idx_library_projection_outbox_claim",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "idx_library_projection_outbox_version",
            "library_item_version_id",
            "projection_kind",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just version-scoped.
        ForeignKeyConstraint(
            ["workspace_id", "library_item_version_id"],
            ["library_item_version.workspace_id", "library_item_version.id"],
            name=conv(
                "fk_library_projection_outbox_library_item_version_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
    )

    # Epic-3 D-2: denormalized from `library_item_version` (see
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
    conversion_attempt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(
        Text, nullable=False, default="conversion", server_default="conversion"
    )
    projection_kind: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    conversion_attempt: Mapped[LibraryAttempt] = relationship(
        back_populates="projection_outbox",
        foreign_keys=[conversion_attempt_id, library_item_version_id],
    )


class LibraryLogicalQuotaUsage(Base):
    """Locked Library-owned counters, separated for source and derived bytes."""

    __tablename__ = "library_logical_quota_usage"
    __table_args__ = (
        CheckConstraint(
            "quota_kind IN ('source', 'derived')",
            name="ck_library_logical_quota_kind",
        ),
        CheckConstraint("limit_bytes >= 0", name="ck_library_logical_quota_limit"),
        CheckConstraint("charged_bytes >= 0", name="ck_library_logical_quota_charged"),
        CheckConstraint(
            "charged_bytes <= limit_bytes",
            name="ck_library_logical_quota_within_limit",
        ),
        CheckConstraint("revision >= 0", name="ck_library_logical_quota_revision"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace_media_write_intent.workspace_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quota_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    limit_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class LibraryLogicalCharge(Base):
    """Idempotent logical charge keyed by the retained Library association."""

    __tablename__ = "library_logical_charge"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "quota_kind"],
            [
                "library_logical_quota_usage.workspace_id",
                "library_logical_quota_usage.quota_kind",
            ],
            name="fk_library_logical_charge_usage",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "quota_kind",
            "association_id",
            name="uq_library_logical_charge_association",
        ),
        CheckConstraint(
            "quota_kind IN ('source', 'derived')",
            name="ck_library_logical_charge_kind",
        ),
        CheckConstraint("byte_size >= 0", name="ck_library_logical_charge_byte_size"),
        CheckConstraint(
            "state IN ('charged', 'released')",
            name="ck_library_logical_charge_state",
        ),
        CheckConstraint(
            "(state = 'released' AND released_at IS NOT NULL) OR "
            "(state = 'charged' AND released_at IS NULL)",
            name="ck_library_logical_charge_release",
        ),
        Index(
            "ix_library_logical_charge_state",
            "workspace_id",
            "quota_kind",
            "state",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    quota_kind: Mapped[str] = mapped_column(Text, nullable=False)
    association_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="charged", server_default="charged"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class LibraryItemTombstone(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Render-time tombstone for a hard-deleted Library source.

    A referenced source can now be deleted (its citation rows are removed first),
    so the inline ``@cite{...}`` tokens left behind in context page bodies would
    otherwise resolve to nothing. This row preserves enough of the source
    (``uri`` + ``title`` + last ``version_no``) for the reader to render a
    "(deleted)" tombstone in place of the dead citation. ``library_item_id`` is
    intentionally NOT a foreign key — the item it points at no longer exists. The
    frontend recomputes the citation key from ``uri``/``title`` to match tokens,
    so the inline-cite slug never has to be stored server-side."""

    __tablename__ = "library_item_tombstone"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "library_item_id",
            name="library_item_tombstone_item_uq",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    library_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    trust_level_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="trusted",
    )
    last_version_no: Mapped[int | None] = mapped_column(Integer)
    # Codex #14: per-citation provenance preserved at delete time (snippet,
    # location, citation_type, cited version/artifact id, citing page/claim), so
    # review/stale/audit/export lineage survives the hard delete.
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
    )
    deleted_by_actor_type: Mapped[str | None] = mapped_column(Text)
    deleted_by_actor_id: Mapped[str | None] = mapped_column(Text)
