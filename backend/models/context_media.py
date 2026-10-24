"""Context-owned media sessions, assets, immutable version usage, and cleanup."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime, utc_now


class ContextMediaLogicalQuotaUsage(Base):
    """Locked Context-owned counter for live media asset associations."""

    __tablename__ = "context_media_logical_quota_usage"
    __table_args__ = (
        CheckConstraint("limit_bytes >= 0", name="ck_context_logical_quota_limit"),
        CheckConstraint("charged_bytes >= 0", name="ck_context_logical_quota_charged"),
        CheckConstraint(
            "charged_bytes <= limit_bytes",
            name="ck_context_logical_quota_within_limit",
        ),
        CheckConstraint("revision >= 0", name="ck_context_logical_quota_revision"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_context_logical_quota_intent",
            ondelete="RESTRICT",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
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


class ContextMediaLogicalCharge(Base):
    """Idempotent Context logical charge, retained even past the asset's
    own deletion -- not just past tombstoning.

    Epic-2 D-7's row-level GC drain (``SqlAlchemyContextMediaRepository.
    drain_gc_candidate``) deletes the ``ContextMediaAsset`` row itself once
    an orphan clears retention, but deliberately never this table: it has
    no FK to ``context_media_asset`` (only to
    ``context_media_logical_quota_usage.workspace_id``) precisely so it can
    outlive the row it was charged for.
    ``backend/services/workspace_write.py``'s ``count_media_dependencies``
    counts every row here -- released or not -- as a retained provenance
    root that blocks workspace deletion, the same family as
    ``StoredBlobRow``/``LibraryLogicalCharge``/``MediaStagingLease``. That is
    also why ``SqlAlchemyContextLogicalQuota.reserve`` re-charges a
    ``released`` row in place for the same ``asset_id`` instead of raising:
    ``asset_id`` is the client-supplied upload marker, so after a drain a
    repeat upload under that marker is a legitimate new reservation, and
    the row it reactivates is this one, unbroken.
    """

    __tablename__ = "context_media_logical_charge"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["context_media_logical_quota_usage.workspace_id"],
            name="fk_context_logical_charge_usage",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "asset_id",
            name="uq_context_logical_charge_asset",
        ),
        CheckConstraint("byte_size >= 0", name="ck_context_logical_charge_byte_size"),
        CheckConstraint(
            "state IN ('charged', 'released')",
            name="ck_context_logical_charge_state",
        ),
        CheckConstraint(
            "(state = 'released' AND released_at IS NOT NULL) OR "
            "(state = 'charged' AND released_at IS NULL)",
            name="ck_context_logical_charge_release",
        ),
        Index(
            "ix_context_logical_charge_state",
            "workspace_id",
            "state",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="charged", server_default="charged"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ContextMediaAsset(Base):
    __tablename__ = "context_media_asset"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('image', 'audio', 'video')",
            name="ck_context_media_asset_kind",
        ),
        CheckConstraint(
            "association_state IN ('pending_install', 'active', 'tombstoned')",
            name="ck_context_media_asset_state",
        ),
        CheckConstraint("logical_bytes >= 0", name="ck_context_media_asset_bytes"),
        CheckConstraint("blob_epoch > 0", name="ck_context_media_asset_epoch"),
        CheckConstraint("revision > 0", name="ck_context_media_asset_revision"),
        ForeignKeyConstraint(
            ["workspace_id", "page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_context_media_asset_page",
            ondelete="RESTRICT",
        ),
        # P3.1/P3.2: asset owner is page alone now (ContextMediaSession and
        # its claim/XOR are gone). `created_for_version_id` is provenance and
        # the orphan-protection/uninserted-preview basis, not an alternate
        # owner -- scoped to the SAME page as the asset via this 3-column FK
        # (context_page_version.uq_context_page_version_workspace_page_owner),
        # so an asset can never name a version belonging to a different page.
        ForeignKeyConstraint(
            ["workspace_id", "page_id", "created_for_version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_media_asset_created_for_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_context_media_asset_blob",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch", "inspection_id"],
            [
                "stored_blob_inspection.workspace_id",
                "stored_blob_inspection.blob_id",
                "stored_blob_inspection.blob_epoch",
                "stored_blob_inspection.id",
            ],
            name="fk_context_media_asset_inspection",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "legacy_upload_asset_id"],
            ["upload_asset.workspace_id", "upload_asset.id"],
            name="fk_context_media_asset_legacy_upload",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "page_id", "id", name="uq_context_media_asset_page"
        ),
        UniqueConstraint(
            "workspace_id",
            "legacy_upload_asset_id",
            name="uq_context_media_asset_legacy_upload",
        ),
        Index("ix_context_media_asset_page", "workspace_id", "page_id", "created_at"),
        Index(
            "ix_context_media_asset_created_for_version",
            "workspace_id",
            "created_for_version_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_for_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inspection_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    legacy_upload_asset_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="image")
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_media_type: Mapped[str] = mapped_column(Text, nullable=False)
    logical_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_revision: Mapped[str] = mapped_column(Text, nullable=False)
    association_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="active"
    )
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_by_actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    tombstoned_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class ContextMediaUsage(Base):
    __tablename__ = "context_media_usage"
    __table_args__ = (
        CheckConstraint(
            "reference_count > 0", name="ck_context_media_usage_reference_count"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "page_id", "asset_id"],
            [
                "context_media_asset.workspace_id",
                "context_media_asset.page_id",
                "context_media_asset.id",
            ],
            name="fk_context_media_usage_asset",
            ondelete="RESTRICT",
        ),
        # Epic-3 D-3 final schema: workspace-scoped, not just
        # page-scoped (was a 2-column ownership FK). workspace_id is
        # already part of this table's own primary key.
        ForeignKeyConstraint(
            ["workspace_id", "page_id", "page_version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_media_usage_page_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        Index(
            "ix_context_media_usage_version",
            "page_id",
            "page_version_id",
            "asset_id",
        ),
        Index(
            "ix_context_media_usage_asset",
            "workspace_id",
            "asset_id",
            "page_version_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    reference_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class ContextMediaUploadRequest(Base):
    __tablename__ = "context_media_upload_request"
    __table_args__ = (
        CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="ck_context_media_upload_sha256",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["context_media_asset.workspace_id", "context_media_asset.id"],
            name="fk_context_media_upload_asset",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "asset_id", name="uq_context_media_upload_asset"
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    marker_uuid: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    page_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    editing_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class ContextMediaGcCandidate(Base):
    """Queued row-level teardown work for a tombstoned asset.

    ``state='cancelled'`` has no writer today. It was designed as a
    rescue for a candidate whose asset regained a usage edge before being
    drained, but that can never happen: ``replace_version_usage`` only
    attaches usage to ``association_state='active'`` assets, and both
    tombstone entry points (``tombstone_and_enqueue_gc``,
    ``tombstone_session_asset_and_enqueue_gc`` in
    ``context_media/adapters/sqlalchemy.py``) re-validate zero usage under a
    row lock immediately before tombstoning and enqueuing here -- tombstoned
    is a one-way door, not a timing race the drainer needs to out-run. A
    ``cancel_gc_candidate_if_reused`` method existed briefly to fill this
    state and was deleted as unreachable dead code rather than kept "just in
    case"; if a future change ever lets a tombstoned asset un-tombstone,
    that change is the one that should reintroduce a canceller, informed by
    whatever made resurrection possible.
    """

    __tablename__ = "context_media_gc_candidate"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'cancelled')",
            name="ck_context_media_gc_candidate_state",
        ),
        CheckConstraint(
            "blob_epoch > 0 AND asset_revision > 0",
            name="ck_context_media_gc_candidate_generations",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["context_media_asset.workspace_id", "context_media_asset.id"],
            name="fk_context_media_gc_candidate_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_context_media_gc_candidate_blob",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id", "asset_id", name="uq_context_media_gc_candidate_asset"
        ),
        Index("ix_context_media_gc_candidate_state", "state", "created_at", "id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
