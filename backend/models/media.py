"""SQLAlchemy records for the shared media kernel infrastructure.

The framework-free aggregate remains in :mod:`backend.media.domain`.  These
records deliberately contain no Library or Context ownership/authorization
columns; outer bounded contexts add their own association tables later.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base, UTCDateTime, utc_now


class WorkspaceMediaWriteIntent(Base):
    """Short-lived writer coordination row and Workspace delete barrier."""

    __tablename__ = "workspace_media_write_intent"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'deleting')",
            name="ck_media_intent_state",
        ),
        CheckConstraint(
            "delete_generation >= 0",
            name="ck_media_intent_generation",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="open",
        server_default="open",
    )
    delete_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class WorkspaceMediaQuotaUsage(Base):
    """Locked hard-quota counter; aggregate scans are reconciliation only."""

    __tablename__ = "workspace_media_quota_usage"
    __table_args__ = (
        CheckConstraint("limit_bytes >= 0", name="ck_media_quota_limit"),
        CheckConstraint("charged_bytes >= 0", name="ck_media_quota_charged"),
        CheckConstraint(
            "charged_bytes <= limit_bytes",
            name="ck_media_quota_within_limit",
        ),
        CheckConstraint("revision >= 0", name="ck_media_quota_revision"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "workspace_media_write_intent.workspace_id",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    limit_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class MediaStagingLease(Base):
    """Durable bounded lease for bytes written before admission/install."""

    __tablename__ = "media_staging_lease"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_staging_intent",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            "generation",
            name="uq_media_staging_generation",
        ),
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_media_staging_idempotency",
        ),
        UniqueConstraint("storage_key", name="uq_media_staging_storage_key"),
        CheckConstraint("generation > 0", name="ck_media_staging_generation"),
        CheckConstraint("max_bytes >= 0", name="ck_media_staging_max_bytes"),
        CheckConstraint(
            "byte_size IS NULL OR (byte_size >= 0 AND byte_size <= max_bytes)",
            name="ck_media_staging_byte_size",
        ),
        CheckConstraint(
            "sha256 IS NULL OR (length(sha256) = 64 AND sha256 = lower(sha256))",
            name="ck_media_staging_sha256",
        ),
        CheckConstraint(
            "state IN ('reserved', 'staged', 'install_pending', 'orphaned', "
            "'deletion_pending', 'deleted')",
            name="ck_media_staging_state",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="ck_media_staging_idempotency",
        ),
        CheckConstraint(
            "length(session_key) BETWEEN 1 AND 160",
            name="ck_media_staging_session_key",
        ),
        Index(
            "ix_media_staging_reconcile",
            "state",
            "expires_at",
            "workspace_id",
            "id",
        ),
        Index(
            "ix_media_staging_actor_cap",
            "workspace_id",
            "actor_id",
            "state",
            "expires_at",
        ),
        Index(
            "ix_media_staging_session_cap",
            "workspace_id",
            "session_key",
            "state",
            "expires_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_key: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="reserved",
        server_default="reserved",
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class StoredBlobRow(Base):
    """Workspace-scoped digest identity and monotonic epoch allocator."""

    __tablename__ = "stored_blob"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_stored_blob_intent",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "sha256",
            name="uq_stored_blob_workspace_sha256",
        ),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="ck_stored_blob_sha256",
        ),
        CheckConstraint("next_epoch > 0", name="ck_stored_blob_next_epoch"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    next_epoch: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class BlobIncarnationRow(Base):
    """Immutable object identity with generation-fenced mutable lifecycle."""

    __tablename__ = "blob_incarnation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id"],
            ["stored_blob.workspace_id", "stored_blob.id"],
            name="fk_blob_incarnation_root",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("storage_key", name="uq_blob_incarnation_storage_key"),
        CheckConstraint("blob_epoch > 0", name="ck_blob_incarnation_epoch"),
        CheckConstraint("byte_size >= 0", name="ck_blob_incarnation_byte_size"),
        CheckConstraint(
            "state IN ('pending_install', 'available', 'gc_pending', 'trashed', "
            "'missing', 'purged')",
            name="ck_blob_incarnation_state",
        ),
        CheckConstraint(
            "publish_generation >= 0 AND security_generation >= 0 "
            "AND gc_generation >= 0",
            name="ck_blob_incarnation_generations",
        ),
        CheckConstraint(
            "(state = 'purged' AND purged_at IS NOT NULL) OR "
            "(state <> 'purged' AND purged_at IS NULL)",
            name="ck_blob_incarnation_purged_at",
        ),
        Index(
            "ix_blob_incarnation_reconcile",
            "state",
            "created_at",
            "workspace_id",
            "blob_id",
            "blob_epoch",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending_install",
        server_default="pending_install",
    )
    publish_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    security_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    gc_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    trashed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MediaBlobReference(Base):
    """Durable cross-domain ownership edge for one blob incarnation.

    The owner identity is intentionally polymorphic: bounded-context rows stay
    in their own model modules, while this shared-kernel ledger gives physical
    GC one authoritative, transactionally maintained zero-reference test.
    """

    __tablename__ = "media_blob_reference"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_blob_reference_incarnation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "owner_kind IN ('context_media_asset', 'library_item_version', "
            "'library_artifact')",
            name="ck_media_blob_reference_owner_kind",
        ),
        CheckConstraint("blob_epoch > 0", name="ck_media_blob_reference_epoch"),
        Index(
            "ix_media_blob_reference_incarnation",
            "workspace_id",
            "blob_id",
            "blob_epoch",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    owner_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class MediaBlobGcHold(Base):
    """Generation-fenced producer ownership before a domain edge is durable.

    One producer can materialize several output blobs, so the concrete blob
    incarnation participates in the primary key. Context upload holds are
    bounded by an expiry; asynchronous Library attempt holds are released by
    their terminal generation transition and therefore must not expire.
    """

    __tablename__ = "media_blob_gc_hold"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_blob_gc_hold_incarnation",
            ondelete="RESTRICT",
        ),
        CheckConstraint("blob_epoch > 0", name="ck_media_blob_gc_hold_epoch"),
        CheckConstraint(
            "producer_generation > 0",
            name="ck_media_blob_gc_hold_generation",
        ),
        CheckConstraint(
            "producer_kind IN ('context_media_upload', "
            "'library_conversion_attempt', 'library_preview_attempt')",
            name="ck_media_blob_gc_hold_producer_kind",
        ),
        CheckConstraint(
            "(producer_kind = 'context_media_upload' AND expires_at IS NOT NULL) "
            "OR (producer_kind IN ('library_conversion_attempt', "
            "'library_preview_attempt') AND expires_at IS NULL)",
            name="ck_media_blob_gc_hold_expiry",
        ),
        Index(
            "ix_media_blob_gc_hold_incarnation",
            "workspace_id",
            "blob_id",
            "blob_epoch",
            "expires_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    producer_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    producer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    producer_generation: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class MediaBlobGcCandidate(Base):
    """Durable request to collect an ownerless blob incarnation.

    Domain deletion and a newly available install both produce an immediately
    eligible request. The install-to-association race is fenced explicitly by
    ``MediaBlobGcHold`` (a synchronous lease or an exact asynchronous producer
    generation), never by guessing how long a caller might take. The candidate
    remains separate from ``BlobIncarnationRow.state`` so either producer can
    commit before any physical side effect. A later sweep removes it only after
    generation-fenced purge and exact quota-release evidence are durable.
    """

    __tablename__ = "media_blob_gc_candidate"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_blob_gc_candidate_incarnation",
            ondelete="RESTRICT",
        ),
        CheckConstraint("blob_epoch > 0", name="ck_media_blob_gc_candidate_epoch"),
        CheckConstraint(
            "reason IN ('unowned_install', 'last_reference_removed')",
            name="ck_media_blob_gc_candidate_reason",
        ),
        Index(
            "ix_media_blob_gc_candidate_queue",
            "eligible_at",
            "requested_at",
            "workspace_id",
            "blob_id",
            "blob_epoch",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    eligible_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class StoredBlobActiveIncarnationRow(Base):
    """Portable active pointer that avoids a circular root/incarnation FK."""

    __tablename__ = "stored_blob_active_incarnation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_stored_blob_active_incarnation",
            ondelete="RESTRICT",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class StoredBlobInspectionRow(Base):
    """Immutable detector, validator, and scanner admission provenance."""

    __tablename__ = "stored_blob_inspection"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_stored_blob_inspection_incarnation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "blob_id",
            "blob_epoch",
            "id",
            name="uq_stored_blob_inspection_binding",
        ),
        CheckConstraint("blob_epoch > 0", name="ck_blob_inspection_epoch"),
        CheckConstraint(
            "scanner_verdict IN ('not_configured', 'clean', 'suspicious', "
            "'malicious', 'unavailable')",
            name="ck_blob_inspection_scanner_verdict",
        ),
        CheckConstraint(
            "(scanner_verdict = 'not_configured' AND scanner_provider IS NULL "
            "AND scanner_version IS NULL) OR "
            "(scanner_verdict <> 'not_configured' AND scanner_provider IS NOT NULL "
            "AND scanner_version IS NOT NULL)",
            name="ck_blob_inspection_scanner_identity",
        ),
        CheckConstraint(
            "ordinary_available = false OR "
            "scanner_verdict IN ('not_configured', 'clean')",
            name="ck_blob_inspection_availability",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    registry_version: Mapped[str] = mapped_column(Text, nullable=False)
    detector_id: Mapped[str] = mapped_column(Text, nullable=False)
    detector_version: Mapped[str] = mapped_column(Text, nullable=False)
    format_id: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_media_type: Mapped[str] = mapped_column(Text, nullable=False)
    validation_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    scanner_verdict: Mapped[str] = mapped_column(Text, nullable=False)
    scanner_provider: Mapped[str | None] = mapped_column(Text)
    scanner_version: Mapped[str | None] = mapped_column(Text)
    ordinary_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class MediaPhysicalCharge(Base):
    """Authoritative charge facts for exactly one concrete physical object."""

    __tablename__ = "media_physical_charge"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_charge_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "staging_id", "target_generation"],
            [
                "media_staging_lease.workspace_id",
                "media_staging_lease.id",
                "media_staging_lease.generation",
            ],
            name="fk_media_charge_staging",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_charge_blob",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "target_kind",
            "target_object_id",
            "target_epoch",
            "target_generation",
            name="uq_media_charge_target",
        ),
        UniqueConstraint("storage_key", name="uq_media_charge_storage_key"),
        UniqueConstraint(
            "workspace_id",
            "id",
            "target_kind",
            "target_object_id",
            "target_epoch",
            "target_generation",
            "storage_key",
            "byte_size",
            name="uq_media_charge_receipt_facts",
        ),
        UniqueConstraint(
            "workspace_id",
            "id",
            "staging_id",
            "target_generation",
            name="uq_media_charge_install_binding",
        ),
        CheckConstraint("target_generation > 0", name="ck_media_charge_generation"),
        CheckConstraint("target_epoch >= 0", name="ck_media_charge_epoch"),
        CheckConstraint("byte_size >= 0", name="ck_media_charge_byte_size"),
        CheckConstraint(
            "accounting_kind IN ('staging', 'pending', 'available', 'blocked', "
            "'trash', 'derived', 'staging_orphan', 'quarantine')",
            name="ck_media_charge_accounting_kind",
        ),
        CheckConstraint(
            "state IN ('charged', 'deletion_pending', 'released')",
            name="ck_media_charge_state",
        ),
        CheckConstraint(
            "(target_kind = 'staging_lease' AND staging_id IS NOT NULL "
            "AND blob_id IS NULL AND blob_epoch IS NULL "
            "AND target_object_id = staging_id AND target_epoch = 0) OR "
            "(target_kind = 'blob_incarnation' AND staging_id IS NULL "
            "AND blob_id IS NOT NULL AND blob_epoch IS NOT NULL "
            "AND target_object_id = blob_id AND target_epoch = blob_epoch)",
            name="ck_media_charge_concrete_target",
        ),
        CheckConstraint(
            "(state = 'released' AND released_at IS NOT NULL) OR "
            "(state <> 'released' AND released_at IS NULL)",
            name="ck_media_charge_released_at",
        ),
        Index(
            "ix_media_charge_reconcile",
            "state",
            "workspace_id",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_object_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    staging_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    blob_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    blob_epoch: Mapped[int | None] = mapped_column(BigInteger)
    accounting_kind: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="charged", server_default="charged"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    deletion_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MediaDeletionReceipt(Base):
    """Immutable exact deletion evidence bound to one charge fact tuple."""

    __tablename__ = "media_deletion_receipt"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "workspace_id",
                "physical_charge_id",
                "target_kind",
                "target_object_id",
                "target_epoch",
                "target_generation",
                "storage_key",
                "byte_size",
            ],
            [
                "media_physical_charge.workspace_id",
                "media_physical_charge.id",
                "media_physical_charge.target_kind",
                "media_physical_charge.target_object_id",
                "media_physical_charge.target_epoch",
                "media_physical_charge.target_generation",
                "media_physical_charge.storage_key",
                "media_physical_charge.byte_size",
            ],
            name="fk_media_receipt_charge_facts",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "physical_charge_id",
            name="uq_media_receipt_charge",
        ),
        CheckConstraint(
            "disposition IN ('deleted', 'already_missing')",
            name="ck_media_receipt_disposition",
        ),
        CheckConstraint("target_generation > 0", name="ck_media_receipt_generation"),
        CheckConstraint("target_epoch >= 0", name="ck_media_receipt_epoch"),
        CheckConstraint("byte_size >= 0", name="ck_media_receipt_byte_size"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    physical_charge_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_object_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class MediaInstallRequest(Base):
    """Durable no-overwrite publish request and crash-replay claim state."""

    __tablename__ = "media_install_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_install_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "staging_id", "staging_generation"],
            [
                "media_staging_lease.workspace_id",
                "media_staging_lease.id",
                "media_staging_lease.generation",
            ],
            name="fk_media_install_staging",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "physical_charge_id",
                "staging_id",
                "staging_generation",
            ],
            [
                "media_physical_charge.workspace_id",
                "media_physical_charge.id",
                "media_physical_charge.staging_id",
                "media_physical_charge.target_generation",
            ],
            name="fk_media_install_charge",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_install_blob",
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
            name="fk_media_install_inspection",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_media_install_idempotency",
        ),
        UniqueConstraint(
            "workspace_id",
            "physical_charge_id",
            name="uq_media_install_charge",
        ),
        UniqueConstraint(
            "workspace_id",
            "blob_id",
            "blob_epoch",
            "publish_generation",
            name="uq_media_install_publish_generation",
        ),
        CheckConstraint("write_generation >= 0", name="ck_media_install_write_gen"),
        CheckConstraint(
            "staging_generation > 0 AND blob_epoch > 0 "
            "AND publish_generation > 0",
            name="ck_media_install_generations",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_media_install_attempts"),
        CheckConstraint(
            "state IN ('pending', 'publishing', 'available', 'retry', 'hold', "
            "'failed')",
            name="ck_media_install_state",
        ),
        Index(
            "ix_media_install_claim",
            "state",
            "next_attempt_at",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    write_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    staging_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    staging_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    physical_charge_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inspection_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    publish_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claim_token: Mapped[str | None] = mapped_column(Text)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_failure_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class MediaOutboxRow(Base):
    """Workspace-bound kernel command/event delivery state."""

    __tablename__ = "media_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_outbox_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_outbox_blob",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "topic",
            "idempotency_key",
            name="uq_media_outbox_idempotency",
        ),
        CheckConstraint(
            "status IN ('queued', 'processing', 'delivered', 'failed')",
            name="ck_media_outbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_media_outbox_attempts"),
        CheckConstraint(
            "length(payload_hash) = 64 AND payload_hash = lower(payload_hash)",
            name="ck_media_outbox_payload_hash",
        ),
        CheckConstraint(
            "(blob_id IS NULL AND blob_epoch IS NULL) OR "
            "(blob_id IS NOT NULL AND blob_epoch > 0)",
            name="ck_media_outbox_blob_reference",
        ),
        Index(
            "ix_media_outbox_claim",
            "status",
            "next_attempt_at",
            "created_at",
            "id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    schema_revision: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column("payload", JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    blob_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    blob_epoch: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued", server_default="queued"
    )
    attempt_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claim_token: Mapped[str | None] = mapped_column(Text)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_failure_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SecurityBlockedBlobRow(Base):
    """Single authoritative read-blocking overlay for one incarnation."""

    __tablename__ = "security_blocked_blob"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_security_blocked_blob_incarnation",
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation > 0", name="ck_security_block_generation"),
        CheckConstraint(
            "state IN ('blocked_pending_move', 'quarantined', "
            "'restore_pending', 'restored_blocked')",
            name="ck_security_block_state",
        ),
        CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 80",
            name="ck_security_block_reason",
        ),
        Index(
            "ix_security_blocked_workflow",
            "state",
            "updated_at",
            "workspace_id",
            "blob_id",
            "blob_epoch",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    blob_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    blob_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
