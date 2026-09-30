"""Add the integrated Shared Media, Library, and Context media schema.

This phase owns the Shared Media persistence foundation, Library multi-format
conversion provenance, and Context media attachment schema. Library and
Context remain separate bounded contexts in application code.
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

from backend.models.base import UTCDateTime


def upgrade() -> None:
    op.create_table(
        "workspace_media_write_intent",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "delete_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('open', 'deleting')",
            name="ck_media_intent_state",
        ),
        sa.CheckConstraint(
            "delete_generation >= 0",
            name="ck_media_intent_generation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    op.create_table(
        "workspace_media_quota_usage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("limit_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "charged_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("limit_bytes >= 0", name="ck_media_quota_limit"),
        sa.CheckConstraint("charged_bytes >= 0", name="ck_media_quota_charged"),
        sa.CheckConstraint(
            "charged_bytes <= limit_bytes",
            name="ck_media_quota_within_limit",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_media_quota_revision"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    # Existing Workspaces become media-capable immediately after the upgrade.
    # Workspaces created later are provisioned lazily by the first media write.
    op.execute(
        sa.text(
            "INSERT INTO workspace_media_write_intent "
            "(workspace_id, state, delete_generation, updated_at) "
            "SELECT id, 'open', 0, CURRENT_TIMESTAMP FROM workspace"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO workspace_media_quota_usage "
            "(workspace_id, limit_bytes, charged_bytes, revision, updated_at) "
            "SELECT id, 10000000000, 0, 0, CURRENT_TIMESTAMP FROM workspace"
        )
    )

    op.create_table(
        "media_staging_lease",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="reserved",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_media_staging_generation"),
        sa.CheckConstraint("max_bytes >= 0", name="ck_media_staging_max_bytes"),
        sa.CheckConstraint(
            "byte_size IS NULL OR (byte_size >= 0 AND byte_size <= max_bytes)",
            name="ck_media_staging_byte_size",
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR (length(sha256) = 64 AND sha256 = lower(sha256))",
            name="ck_media_staging_sha256",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'staged', 'install_pending', 'orphaned', "
            "'deletion_pending', 'deleted')",
            name="ck_media_staging_state",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 160",
            name="ck_media_staging_idempotency",
        ),
        sa.CheckConstraint(
            "length(session_key) BETWEEN 1 AND 160",
            name="ck_media_staging_session_key",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_staging_intent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "generation",
            name="uq_media_staging_generation",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_media_staging_idempotency",
        ),
        sa.UniqueConstraint("storage_key", name="uq_media_staging_storage_key"),
    )
    op.create_index(
        "ix_media_staging_reconcile",
        "media_staging_lease",
        ["state", "expires_at", "workspace_id", "id"],
    )
    op.create_index(
        "ix_media_staging_actor_cap",
        "media_staging_lease",
        ["workspace_id", "actor_id", "state", "expires_at"],
    )
    op.create_index(
        "ix_media_staging_session_cap",
        "media_staging_lease",
        ["workspace_id", "session_key", "state", "expires_at"],
    )

    op.create_table(
        "stored_blob",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column(
            "next_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="ck_stored_blob_sha256",
        ),
        sa.CheckConstraint("next_epoch > 0", name="ck_stored_blob_next_epoch"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_stored_blob_intent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "sha256",
            name="uq_stored_blob_workspace_sha256",
        ),
    )

    op.create_table(
        "blob_incarnation",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="pending_install",
        ),
        sa.Column(
            "publish_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "security_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "gc_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("blob_epoch > 0", name="ck_blob_incarnation_epoch"),
        sa.CheckConstraint(
            "byte_size >= 0",
            name="ck_blob_incarnation_byte_size",
        ),
        sa.CheckConstraint(
            "state IN ('pending_install', 'available', 'gc_pending', 'trashed', "
            "'missing', 'purged')",
            name="ck_blob_incarnation_state",
        ),
        sa.CheckConstraint(
            "publish_generation >= 0 AND security_generation >= 0 "
            "AND gc_generation >= 0",
            name="ck_blob_incarnation_generations",
        ),
        sa.CheckConstraint(
            "(state = 'purged' AND purged_at IS NOT NULL) OR "
            "(state <> 'purged' AND purged_at IS NULL)",
            name="ck_blob_incarnation_purged_at",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id"],
            ["stored_blob.workspace_id", "stored_blob.id"],
            name="fk_blob_incarnation_root",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "blob_id", "blob_epoch"),
        sa.UniqueConstraint("storage_key", name="uq_blob_incarnation_storage_key"),
    )
    op.create_index(
        "ix_blob_incarnation_reconcile",
        "blob_incarnation",
        ["state", "created_at", "workspace_id", "blob_id", "blob_epoch"],
    )

    op.create_table(
        "stored_blob_active_incarnation",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_stored_blob_active_incarnation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "blob_id"),
    )

    op.create_table(
        "stored_blob_inspection",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("registry_version", sa.Text(), nullable=False),
        sa.Column("detector_id", sa.Text(), nullable=False),
        sa.Column("detector_version", sa.Text(), nullable=False),
        sa.Column("format_id", sa.Text(), nullable=False),
        sa.Column("canonical_media_type", sa.Text(), nullable=False),
        sa.Column("validation_schema_version", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("scanner_verdict", sa.Text(), nullable=False),
        sa.Column("scanner_provider", sa.Text(), nullable=True),
        sa.Column("scanner_version", sa.Text(), nullable=True),
        sa.Column(
            "ordinary_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("blob_epoch > 0", name="ck_blob_inspection_epoch"),
        sa.CheckConstraint(
            "scanner_verdict IN ('not_configured', 'clean', 'suspicious', "
            "'malicious', 'unavailable')",
            name="ck_blob_inspection_scanner_verdict",
        ),
        sa.CheckConstraint(
            "(scanner_verdict = 'not_configured' AND scanner_provider IS NULL "
            "AND scanner_version IS NULL) OR "
            "(scanner_verdict <> 'not_configured' AND scanner_provider IS NOT NULL "
            "AND scanner_version IS NOT NULL)",
            name="ck_blob_inspection_scanner_identity",
        ),
        sa.CheckConstraint(
            "ordinary_available = false OR "
            "scanner_verdict IN ('not_configured', 'clean')",
            name="ck_blob_inspection_availability",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_stored_blob_inspection_incarnation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "blob_id",
            "blob_epoch",
            "id",
            name="uq_stored_blob_inspection_binding",
        ),
    )

    op.create_table(
        "media_physical_charge",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_object_id", sa.Uuid(), nullable=False),
        sa.Column("target_epoch", sa.BigInteger(), nullable=False),
        sa.Column("target_generation", sa.BigInteger(), nullable=False),
        sa.Column("staging_id", sa.Uuid(), nullable=True),
        sa.Column("blob_id", sa.Uuid(), nullable=True),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=True),
        sa.Column("accounting_kind", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="charged",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "deletion_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_generation > 0",
            name="ck_media_charge_generation",
        ),
        sa.CheckConstraint("target_epoch >= 0", name="ck_media_charge_epoch"),
        sa.CheckConstraint("byte_size >= 0", name="ck_media_charge_byte_size"),
        sa.CheckConstraint(
            "accounting_kind IN ('staging', 'pending', 'available', 'blocked', "
            "'trash', 'derived', 'staging_orphan', 'quarantine')",
            name="ck_media_charge_accounting_kind",
        ),
        sa.CheckConstraint(
            "state IN ('charged', 'deletion_pending', 'released')",
            name="ck_media_charge_state",
        ),
        sa.CheckConstraint(
            "(target_kind = 'staging_lease' AND staging_id IS NOT NULL "
            "AND blob_id IS NULL AND blob_epoch IS NULL "
            "AND target_object_id = staging_id AND target_epoch = 0) OR "
            "(target_kind = 'blob_incarnation' AND staging_id IS NULL "
            "AND blob_id IS NOT NULL AND blob_epoch IS NOT NULL "
            "AND target_object_id = blob_id AND target_epoch = blob_epoch)",
            name="ck_media_charge_concrete_target",
        ),
        sa.CheckConstraint(
            "(state = 'released' AND released_at IS NOT NULL) OR "
            "(state <> 'released' AND released_at IS NULL)",
            name="ck_media_charge_released_at",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_charge_intent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "staging_id", "target_generation"],
            [
                "media_staging_lease.workspace_id",
                "media_staging_lease.id",
                "media_staging_lease.generation",
            ],
            name="fk_media_charge_staging",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_charge_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "target_kind",
            "target_object_id",
            "target_epoch",
            "target_generation",
            name="uq_media_charge_target",
        ),
        sa.UniqueConstraint("storage_key", name="uq_media_charge_storage_key"),
        sa.UniqueConstraint(
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
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "staging_id",
            "target_generation",
            name="uq_media_charge_install_binding",
        ),
    )
    op.create_index(
        "ix_media_charge_reconcile",
        "media_physical_charge",
        ["state", "workspace_id", "created_at", "id"],
    )

    op.create_table(
        "media_deletion_receipt",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("physical_charge_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_object_id", sa.Uuid(), nullable=False),
        sa.Column("target_epoch", sa.BigInteger(), nullable=False),
        sa.Column("target_generation", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "disposition IN ('deleted', 'already_missing')",
            name="ck_media_receipt_disposition",
        ),
        sa.CheckConstraint(
            "target_generation > 0",
            name="ck_media_receipt_generation",
        ),
        sa.CheckConstraint("target_epoch >= 0", name="ck_media_receipt_epoch"),
        sa.CheckConstraint("byte_size >= 0", name="ck_media_receipt_byte_size"),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "physical_charge_id",
            name="uq_media_receipt_charge",
        ),
    )

    op.create_table(
        "media_install_request",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("write_generation", sa.BigInteger(), nullable=False),
        sa.Column("staging_id", sa.Uuid(), nullable=False),
        sa.Column("staging_generation", sa.BigInteger(), nullable=False),
        sa.Column("physical_charge_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("inspection_id", sa.Uuid(), nullable=False),
        sa.Column("publish_generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "write_generation >= 0",
            name="ck_media_install_write_gen",
        ),
        sa.CheckConstraint(
            "staging_generation > 0 AND blob_epoch > 0 AND publish_generation > 0",
            name="ck_media_install_generations",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_media_install_attempts"),
        sa.CheckConstraint(
            "state IN ('pending', 'publishing', 'available', 'retry', 'hold', "
            "'failed')",
            name="ck_media_install_state",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_install_intent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "staging_id", "staging_generation"],
            [
                "media_staging_lease.workspace_id",
                "media_staging_lease.id",
                "media_staging_lease.generation",
            ],
            name="fk_media_install_staging",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_install_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_media_install_idempotency",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "physical_charge_id",
            name="uq_media_install_charge",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "blob_id",
            "blob_epoch",
            "publish_generation",
            name="uq_media_install_publish_generation",
        ),
    )
    op.create_index(
        "ix_media_install_claim",
        "media_install_request",
        ["state", "next_attempt_at", "created_at", "id"],
    )

    op.create_table(
        "media_outbox",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("schema_revision", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=True),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "attempt_count",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'delivered', 'failed')",
            name="ck_media_outbox_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_media_outbox_attempts"),
        sa.CheckConstraint(
            "length(payload_hash) = 64 AND payload_hash = lower(payload_hash)",
            name="ck_media_outbox_payload_hash",
        ),
        sa.CheckConstraint(
            "(blob_id IS NULL AND blob_epoch IS NULL) OR "
            "(blob_id IS NOT NULL AND blob_epoch > 0)",
            name="ck_media_outbox_blob_reference",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_media_outbox_intent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_media_outbox_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "topic",
            "idempotency_key",
            name="uq_media_outbox_idempotency",
        ),
    )
    op.create_index(
        "ix_media_outbox_claim",
        "media_outbox",
        ["status", "next_attempt_at", "created_at", "id"],
    )

    op.create_table(
        "security_blocked_blob",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_security_block_generation"),
        sa.CheckConstraint(
            "state IN ('blocked_pending_move', 'quarantined', "
            "'restore_pending', 'restored_blocked')",
            name="ck_security_block_state",
        ),
        sa.CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 80",
            name="ck_security_block_reason",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_security_blocked_blob_incarnation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "blob_id", "blob_epoch"),
    )
    op.create_index(
        "ix_security_blocked_workflow",
        "security_blocked_blob",
        ["state", "updated_at", "workspace_id", "blob_id", "blob_epoch"],
    )

    _upgrade_library_multiformat()
    _upgrade_context_media()
    _create_evidence_guards(op.get_bind())
    _upgrade_preview_lifecycle()


def downgrade() -> None:
    # prepare_downgrade_data() is sequenced by versions/0002_epic1.py, which is
    # the single owner of prepare-step order across modules. Calling it again
    # here would be a second, differently-ordered source of that truth.
    _downgrade_context_media()
    _downgrade_semantic_overlay()
    _downgrade_library_multiformat()
    _drop_evidence_guards(op.get_bind())
    op.drop_index(
        "ix_security_blocked_workflow",
        table_name="security_blocked_blob",
    )
    op.drop_table("security_blocked_blob")
    op.drop_index("ix_media_outbox_claim", table_name="media_outbox")
    op.drop_table("media_outbox")
    op.drop_index("ix_media_install_claim", table_name="media_install_request")
    op.drop_table("media_install_request")
    op.drop_table("media_deletion_receipt")
    op.drop_index(
        "ix_media_charge_reconcile",
        table_name="media_physical_charge",
    )
    op.drop_table("media_physical_charge")
    op.drop_table("stored_blob_inspection")
    op.drop_table("stored_blob_active_incarnation")
    op.drop_index(
        "ix_blob_incarnation_reconcile",
        table_name="blob_incarnation",
    )
    op.drop_table("blob_incarnation")
    op.drop_table("stored_blob")
    op.drop_index(
        "ix_media_staging_session_cap",
        table_name="media_staging_lease",
    )
    op.drop_index(
        "ix_media_staging_actor_cap",
        table_name="media_staging_lease",
    )
    op.drop_index(
        "ix_media_staging_reconcile",
        table_name="media_staging_lease",
    )
    op.drop_table("media_staging_lease")
    op.drop_table("workspace_media_quota_usage")
    op.drop_table("workspace_media_write_intent")


def prepare_downgrade_data() -> None:
    """Refuse a lossy downgrade, then restore 0001 sync-run lineage.

    The refusal runs unconditionally. It used to sit inside the
    ``"sync_run_id" in columns`` branch below, which made it skip itself
    whenever that column had already been dropped -- a partial or repaired
    downgrade would then walk straight into ``_downgrade_context_media()`` and
    destroy the very rows the guard exists to protect. The precondition for
    refusing is the media DATA, which the guard probes for itself; the column
    probe is a precondition only for ``_restore_sync_run_arrays``, which reads
    ``library_item_version.sync_run_id``.
    """

    connection = op.get_bind()
    _refuse_lossy_integrated_media_downgrade(connection)
    columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("library_item_version")
    }
    if "sync_run_id" in columns:
        _restore_sync_run_arrays(connection)


def _create_evidence_guards(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        for table, prefix in (
            ("stored_blob_inspection", "blob_inspection"),
            ("media_deletion_receipt", "media_receipt"),
        ):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{prefix}_no_update "
                    f"BEFORE UPDATE ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'media evidence is immutable'); END"
                )
            )
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{prefix}_guard_delete "
                    f"BEFORE DELETE ON {table} "
                    "WHEN NOT EXISTS ("
                    "SELECT 1 FROM workspace_media_write_intent "
                    "WHERE workspace_id = OLD.workspace_id "
                    "AND state = 'deleting') BEGIN "
                    "SELECT RAISE(ABORT, 'media evidence delete requires barrier'); "
                    "END"
                )
            )
        return
    if connection.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "CREATE FUNCTION dtxwiki_guard_media_evidence() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN "
                "IF TG_OP = 'UPDATE' THEN "
                "RAISE EXCEPTION 'media evidence is immutable'; "
                "END IF; "
                "IF NOT EXISTS ("
                "SELECT 1 FROM workspace_media_write_intent "
                "WHERE workspace_id = OLD.workspace_id AND state = 'deleting'"
                ") THEN "
                "RAISE EXCEPTION 'media evidence delete requires barrier'; "
                "END IF; "
                "RETURN OLD; END; $$"
            )
        )
        for table, prefix in (
            ("stored_blob_inspection", "blob_inspection"),
            ("media_deletion_receipt", "media_receipt"),
        ):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{prefix}_guard "
                    f"BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION dtxwiki_guard_media_evidence()"
                )
            )
        return
    raise RuntimeError("media foundation supports only SQLite and PostgreSQL")


def _drop_evidence_guards(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        for prefix in ("blob_inspection", "media_receipt"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{prefix}_no_update"))
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{prefix}_guard_delete"))
        return
    if connection.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_blob_inspection_guard "
                "ON stored_blob_inspection"
            )
        )
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_media_receipt_guard "
                "ON media_deletion_receipt"
            )
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS dtxwiki_guard_media_evidence()"))
        return
    raise RuntimeError("media foundation supports only SQLite and PostgreSQL")


def _collect_sync_run_links(
    connection: Connection,
) -> list[tuple[UUID, UUID]]:
    """Decode the 0001 JSON lineage before its source column is removed."""

    sync_runs = sa.table(
        "library_sync_run",
        sa.column("id", sa.Uuid()),
        sa.column("created_library_item_version_ids", sa.JSON()),
    )
    links: list[tuple[UUID, UUID]] = []
    for run_id, raw_ids in connection.execute(
        sa.select(
            sync_runs.c.id,
            sync_runs.c.created_library_item_version_ids,
        )
    ):
        for raw_id in raw_ids or []:
            try:
                version_id = UUID(str(raw_id))
            except ValueError:
                raise RuntimeError(
                    f"library_sync_run {run_id} has a non-UUID version ref: "
                    f"{raw_id!r}"
                ) from None
            links.append((version_id, run_id))
    return links


def _apply_sync_run_links(
    connection: Connection,
    links: list[tuple[UUID, UUID]],
) -> None:
    versions = sa.table(
        "library_item_version",
        sa.column("id", sa.Uuid()),
        sa.column("sync_run_id", sa.Uuid()),
    )
    for version_id, run_id in links:
        connection.execute(
            versions.update()
            .where(versions.c.id == version_id)
            .values(sync_run_id=run_id)
        )


def _restore_sync_run_arrays(connection: Connection) -> None:
    """Reconstruct the immutable 0001 JSON lineage before dropping its FK."""

    sync_runs = sa.table(
        "library_sync_run",
        sa.column("id", sa.Uuid()),
        sa.column("created_library_item_version_ids", sa.JSON()),
    )
    versions = sa.table(
        "library_item_version",
        sa.column("id", sa.Uuid()),
        sa.column("sync_run_id", sa.Uuid()),
    )
    # tenancy_schema exposes the legacy column as nullable before this data
    # preparation step, then makes it NOT NULL in its one final table batch.
    versions_by_run: dict[UUID, list[str]] = {}
    for version_id, run_id in connection.execute(
        sa.select(versions.c.id, versions.c.sync_run_id)
        .where(versions.c.sync_run_id.is_not(None))
        .order_by(versions.c.id)
    ):
        versions_by_run.setdefault(run_id, []).append(str(version_id))
    for run_id, version_ids in versions_by_run.items():
        connection.execute(
            sync_runs.update()
            .where(sync_runs.c.id == run_id)
            .values(created_library_item_version_ids=version_ids)
        )
    connection.execute(
        sync_runs.update()
        .where(sync_runs.c.created_library_item_version_ids.is_(None))
        .values(created_library_item_version_ids=[])
    )
def _upgrade_library_multiformat() -> None:
    connection = op.get_bind()
    sync_run_links = _collect_sync_run_links(connection)
    # Keep the drained legacy array until tenancy_schema's final constraint
    # batch, where it can be dropped without another SQLite table copy.

    # Order note: the run/attempt tables below are created BEFORE the
    # library_item_version and library_item ALTERs, even though they read as
    # "later" schema. SQLite implements every batch_alter_table as a full
    # table rebuild, and both of those tables would otherwise be rebuilt twice
    # -- once for their columns and once for a foreign key into a table that
    # did not exist yet. The run/attempt tables' own foreign keys need only
    # library_item_version.id and workspace.id, both of which already exist,
    # so creating them first lets each of the two tables take all of its
    # columns, checks, unique key and foreign keys in a single table copy.
    #
    # A-1: conversion and
    # preview_generation used to be two independently-declared table
    # families (library_conversion_run/attempt and
    # library_preview_run/attempt/artifact). They are now one common
    # library_run/library_attempt/library_artifact storage,
    # stage-discriminated, with stage-specific fields moved into 1:1
    # extension tables. See backend/models/library.py for the exact target
    # shape this DDL reproduces.
    _LIBRARY_RUN_STAGES = "('conversion', 'preview_generation')"

    op.create_table(
        "library_run",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("run_seq", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_by_actor_type", sa.Text(), nullable=True),
        sa.Column("requested_by_actor_id", sa.Uuid(), nullable=True),
        sa.Column("provider_chain_json", sa.JSON(), nullable=False),
        sa.Column("policy_revision", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("finished_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"stage IN {_LIBRARY_RUN_STAGES}",
            name="library_run_stage_check",
        ),
        sa.CheckConstraint(
            "(stage = 'conversion' AND reason IN "
            "('initial', 'manual_reconvert', 'policy_migration')) OR "
            "(stage = 'preview_generation' AND reason IN "
            "('initial', 'manual_regenerate', 'dependency_change'))",
            name="library_run_reason_check",
        ),
        sa.CheckConstraint(
            "(stage = 'conversion' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'stored_unconverted')) OR "
            "(stage = 'preview_generation' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'unavailable'))",
            name="library_run_status_check",
        ),
        sa.CheckConstraint("run_seq > 0", name="library_run_seq_check"),
        sa.ForeignKeyConstraint(
            ["library_item_version_id"],
            ["library_item_version.id"],
            name="fk_library_run_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_library_run_workspace_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_item_version_id",
            "idempotency_key",
            name="library_run_version_idempotency_uq",
        ),
        sa.UniqueConstraint(
            "library_item_version_id",
            "run_seq",
            name="library_run_version_seq_uq",
        ),
        sa.UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_run_id_version_uq",
        ),
        sa.UniqueConstraint(
            "id",
            "stage",
            name="uq_library_run_id_stage",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_run_workspace_version_owner",
        ),
        # Required for SQLite: a composite FK's parent columns must exactly
        # match a UNIQUE constraint on the parent table, and library_
        # attempt's FK into this table references (workspace_id, id,
        # library_item_version_id, stage) as a unit -- one column wider
        # than uq_library_run_workspace_version_owner. Named to match
        # backend/models/library.py's LibraryRun.__table_args__ exactly
        # (also declared there, for the same reason, against
        # Base.metadata.create_all()-created schemas).
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            "stage",
            name="uq_library_run_workspace_version_stage_owner",
        ),
    )
    op.create_index(
        "idx_library_run_claim",
        "library_run",
        ["stage", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_library_run_version_status",
        "library_run",
        ["library_item_version_id", "stage", "status", "run_seq"],
        unique=False,
    )

    op.create_table(
        "library_run_conversion_detail",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="conversion",
        ),
        sa.Column("error_fallback_matrix_json", sa.JSON(), nullable=False),
        sa.Column("per_provider_budget_json", sa.JSON(), nullable=False),
        sa.Column("total_budget_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(
            ["run_id", "stage"],
            ["library_run.id", "library_run.stage"],
            name="fk_library_run_conversion_detail_stage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "library_item_version_id"],
            [
                "library_run.workspace_id",
                "library_run.id",
                "library_run.library_item_version_id",
            ],
            name="fk_library_run_conversion_detail_run_id_workspace_scoped",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "library_attempt",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        # Renamed from conversion's converter_id/converter_version/
        # converter_spi_version and preview's provider_id/provider_version/
        # provider_spi_version -- the same concept under two names. The
        # unified table uses preview's naming (backend/models/library.py's
        # own comment on LibraryAttempt).
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("provider_version", sa.Text(), nullable=False),
        sa.Column("provider_spi_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("chain_index", sa.Integer(), nullable=False),
        sa.Column("transport_retry_count", sa.Integer(), nullable=False),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("parent_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("finished_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("safe_error_summary", sa.Text(), nullable=True),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"stage IN {_LIBRARY_RUN_STAGES}",
            name="library_attempt_stage_check",
        ),
        sa.CheckConstraint(
            "(stage = 'conversion' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'unsupported')) OR "
            "(stage = 'preview_generation' AND status IN "
            "('queued', 'running', 'completed', 'failed', 'timed_out', "
            "'cancelled', 'unsupported'))",
            name="library_attempt_status_check",
        ),
        sa.CheckConstraint(
            "chain_index >= 0",
            name="library_attempt_chain_index_check",
        ),
        sa.CheckConstraint(
            "transport_retry_count >= 0",
            name="library_attempt_retry_count_check",
        ),
        sa.CheckConstraint(
            "execution_generation > 0",
            name="library_attempt_generation_check",
        ),
        sa.CheckConstraint(
            "length(input_sha256) = 64",
            name="library_attempt_input_sha256_check",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "library_item_version_id", "stage"],
            [
                "library_run.workspace_id",
                "library_run.id",
                "library_run.library_item_version_id",
                "library_run.stage",
            ],
            name="fk_library_attempt_run_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_item_version_id"],
            ["library_item_version.id"],
            name="fk_library_attempt_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_library_attempt_workspace_id",
        ),
        # workspace-scoped and self-referential from birth (library_attempt
        # already has workspace_id at creation, unlike the FK to
        # library_item_version below), matching the model exactly -- no
        # tenancy_schema deferral needed.
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_attempt_id"],
            ["library_attempt.workspace_id", "library_attempt.id"],
            name="fk_library_attempt_parent_id_workspace_scoped",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "chain_index",
            name="library_attempt_run_chain_uq",
        ),
        sa.UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_attempt_id_version_uq",
        ),
        sa.UniqueConstraint(
            "id",
            "stage",
            name="uq_library_attempt_id_stage",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_library_attempt_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_attempt_workspace_version_owner",
        ),
        # Synthetic, not in the ORM model -- same SQLite "foreign key
        # mismatch" reasoning as library_run's own widened constraint
        # above. library_artifact's FK into this table references
        # (workspace_id, id, library_item_version_id, stage) -- also
        # declared in backend/models/library.py's LibraryAttempt.
        # __table_args__ under this same name, for the same reason.
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            "stage",
            name="uq_library_attempt_workspace_version_stage_owner",
        ),
        # Migration-only (no equivalent in the ORM model, which declares
        # library_projection_outbox's FK into this table directly in its
        # final 4-column workspace-scoped form): this phase's interim,
        # pre-final-tenancy outbox FK references (id, library_item_
        # version_id, stage) as a unit. Left unnamed so compare_metadata
        # does not try to pair it against a model that declares no such
        # constraint.
        sa.UniqueConstraint(
            "id",
            "library_item_version_id",
            "stage",
        ),
        # Same SQLite requirement as above: library_run_preview_detail.
        # expected_selected_preview_attempt_id references (run_id, id) as
        # a unit -- also declared in the model under this same name.
        sa.UniqueConstraint(
            "run_id",
            "id",
            name="uq_library_attempt_run_id",
        ),
    )
    op.create_index(
        "idx_library_attempt_claim",
        "library_attempt",
        ["stage", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_library_attempt_version_status",
        "library_attempt",
        ["library_item_version_id", "stage", "status"],
        unique=False,
    )

    op.create_table(
        "library_attempt_conversion_detail",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="conversion",
        ),
        sa.Column("manual_retry_disposition", sa.Text(), nullable=True),
        sa.Column("disposition_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "stage"],
            ["library_attempt.id", "library_attempt.stage"],
            name="fk_library_attempt_conversion_detail_stage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "library_item_version_id"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
            ],
            name="fk_library_attempt_conv_detail_attempt_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "manual_retry_disposition IS NULL OR "
            "manual_retry_disposition IN ('allowed', 'denied')",
            name="library_attempt_conv_detail_manual_retry_disposition_check",
        ),
        sa.CheckConstraint(
            "(manual_retry_disposition IS NULL AND disposition_reason IS NULL) OR "
            "(manual_retry_disposition IS NOT NULL AND "
            "disposition_reason IS NOT NULL AND "
            "length(disposition_reason) BETWEEN 1 AND 128)",
            name="library_attempt_conversion_detail_retry_disposition_pair_check",
        ),
    )

    # Add only nullable/data-safe columns here. tenancy_schema owns the one
    # final table rebuild that installs every Library version FK, CHECK,
    # UNIQUE, workspace routing constraint, and raw_storage_ref nullability.
    op.add_column(
        "library_item_version",
        sa.Column("sync_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("source_blob_workspace_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("source_blob_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("source_blob_epoch", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("original_filename", sa.Text(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("declared_media_type", sa.Text(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("detected_media_type", sa.Text(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("format_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("detection_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column("selected_conversion_attempt_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "library_item_version",
        sa.Column(
            "preview_status",
            sa.Text(),
            nullable=False,
            server_default="unavailable",
        ),
    )
    op.add_column(
        "library_item_version",
        sa.Column("selected_preview_attempt_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_library_item_version_sync_run_id",
        "library_item_version",
        ["sync_run_id"],
        unique=False,
    )
    op.create_index(
        "idx_library_item_version_source_blob",
        "library_item_version",
        ["source_blob_workspace_id", "source_blob_id", "source_blob_epoch"],
        unique=False,
    )
    _apply_sync_run_links(connection, sync_run_links)
    op.create_index(
        "idx_library_item_version_processing_claim",
        "library_item_version",
        ["processing_status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_library_item_version_selected_conversion_attempt_id",
        "library_item_version",
        ["selected_conversion_attempt_id"],
        unique=False,
    )
    op.create_index(
        "ix_library_item_version_selected_preview_attempt_id",
        "library_item_version",
        ["selected_preview_attempt_id"],
    )

    # The references and ownership constraints join the rest of
    # library_item's final shape in tenancy_schema's single table batch.
    op.add_column(
        "library_item",
        sa.Column("current_ready_version_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "library_item",
        sa.Column("current_preview_version_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_library_item_current_ready_version_id",
        "library_item",
        ["current_ready_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_library_item_current_preview_version_id",
        "library_item",
        ["current_preview_version_id"],
    )

    _backfill_current_ready_versions(op.get_bind())

    # library_artifact (§6.3/§6.4): the legacy nullable-attempt /
    # storage_ref-only shape (a table library_media_schema used to ALTER in
    # place from 0001's simpler original) is retired -- every artifact now
    # belongs to a real attempt and a real verified blob, full stop. Dropping
    # and recreating fresh (rather than a long ALTER chain) is less
    # error-prone here since no real data exists in either case, matching
    # this phase's existing style elsewhere (several tables in this same
    # phase are dropped and recreated rather than ALTERed).
    _detach_library_artifact_dependents()
    op.drop_table("library_artifact")
    op.create_table(
        "library_artifact",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("blob_workspace_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_artifact_id_version_uq",
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "role",
            "ordinal",
            name="library_artifact_attempt_role_ordinal_uq",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["blob_workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_library_artifact_blob_incarnation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_item_version_id"],
            ["library_item_version.id"],
            name="fk_library_artifact_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_library_artifact_workspace_id",
        ),
        sa.CheckConstraint(
            f"stage IN {_LIBRARY_RUN_STAGES}",
            name="library_artifact_stage_check",
        ),
        sa.CheckConstraint(
            "blob_workspace_id = workspace_id",
            name="library_artifact_blob_workspace_check",
        ),
        sa.CheckConstraint(
            "role IN ("
            "'markdown', 'search_text', 'location_map', 'preview', 'asset', "
            "'metadata')",
            name="library_artifact_role_check",
        ),
        sa.CheckConstraint(
            "kind IN ('markdown', 'text', 'table', 'pdf', 'image', 'audio', "
            "'video', 'metadata')",
            name="library_artifact_kind_check",
        ),
        sa.CheckConstraint(
            "byte_size >= 0",
            name="library_artifact_byte_size_check",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="library_artifact_ordinal_check",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="library_artifact_content_sha256_check",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_library_artifact_workspace_id"
        ),
        sa.UniqueConstraint(
            "id",
            "stage",
            name="uq_library_artifact_id_stage",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            "library_item_version_id",
            name="uq_library_artifact_workspace_version_owner",
        ),
    )
    _reattach_library_artifact_dependents()
    op.create_index(
        "idx_library_artifact_attempt_role",
        "library_artifact",
        ["attempt_id", "role"],
        unique=False,
    )
    op.create_index(
        "idx_library_artifact_blob",
        "library_artifact",
        ["blob_workspace_id", "blob_id", "blob_epoch"],
        unique=False,
    )

    op.create_table(
        "library_artifact_conversion_detail",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="conversion",
        ),
        sa.Column(
            "is_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("sanitizer_version", sa.Text(), nullable=True),
        sa.Column("extraction_method", sa.Text(), nullable=True),
        sa.Column("index_text", sa.Text(), nullable=True),
        sa.Column("location_model", sa.JSON(), nullable=False),
        sa.Column("quality_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.ForeignKeyConstraint(
            ["artifact_id", "stage"],
            ["library_artifact.id", "library_artifact.stage"],
            name="fk_library_artifact_conversion_detail_stage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "artifact_id", "library_item_version_id"],
            [
                "library_artifact.workspace_id",
                "library_artifact.id",
                "library_artifact.library_item_version_id",
            ],
            name=(
                "fk_library_artifact_conv_detail_artifact_id_workspace_scoped"
            ),
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "library_artifact_preview_detail",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="preview_generation",
        ),
        sa.Column("renderer_contract", sa.Text(), nullable=False),
        sa.Column("delivery", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.ForeignKeyConstraint(
            ["artifact_id", "stage"],
            ["library_artifact.id", "library_artifact.stage"],
            name="fk_library_artifact_preview_detail_stage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "artifact_id", "library_item_version_id"],
            [
                "library_artifact.workspace_id",
                "library_artifact.id",
                "library_artifact.library_item_version_id",
            ],
            name="fk_library_artifact_preview_detail_artifact_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "delivery IN ('inline', 'range', 'text')",
            name="library_artifact_preview_detail_delivery_check",
        ),
        sa.CheckConstraint(
            "rank >= 0",
            name="library_artifact_preview_detail_rank_check",
        ),
    )

    op.create_table(
        "library_attempt_preview_detail",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="preview_generation",
        ),
        sa.Column("provider_distribution_digest", sa.Text(), nullable=False),
        sa.Column("input_kind", sa.Text(), nullable=False),
        sa.Column("input_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("input_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("renderer_contract_version", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "stage"],
            ["library_attempt.id", "library_attempt.stage"],
            name="fk_library_attempt_preview_detail_stage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "library_item_version_id"],
            [
                "library_attempt.workspace_id",
                "library_attempt.id",
                "library_attempt.library_item_version_id",
            ],
            name="fk_library_attempt_preview_detail_attempt_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "input_attempt_id"],
            ["library_attempt.workspace_id", "library_attempt.id"],
            name=(
                "fk_library_attempt_prev_detail_input_attempt_id_ws_scoped"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "input_artifact_id"],
            ["library_artifact.workspace_id", "library_artifact.id"],
            name=(
                "fk_library_attempt_prev_detail_input_artifact_id_ws_scoped"
            ),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(provider_distribution_digest) = 64",
            name="library_attempt_preview_detail_provider_digest_check",
        ),
        sa.CheckConstraint(
            "((input_kind = 'source' AND input_attempt_id IS NULL "
            "AND input_artifact_id IS NULL) OR "
            "(input_kind = 'conversion_artifact' "
            "AND input_attempt_id IS NOT NULL "
            "AND input_artifact_id IS NOT NULL))",
            name="library_attempt_preview_detail_input_dependency_check",
        ),
    )

    op.create_table(
        "library_run_preview_detail",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="preview_generation",
        ),
        sa.Column("recipe_json", sa.JSON(), nullable=False),
        sa.Column("limits_json", sa.JSON(), nullable=False),
        sa.Column("input_dependency_json", sa.JSON(), nullable=False),
        sa.Column("renderer_contract_version", sa.Text(), nullable=False),
        sa.Column("expected_selected_preview_attempt_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(
            ["run_id", "stage"],
            ["library_run.id", "library_run.stage"],
            name="fk_library_run_preview_detail_stage",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "library_item_version_id"],
            [
                "library_run.workspace_id",
                "library_run.id",
                "library_run.library_item_version_id",
            ],
            name="fk_library_run_preview_detail_run_id_workspace_scoped",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "expected_selected_preview_attempt_id"],
            ["library_attempt.run_id", "library_attempt.id"],
            name="fk_library_run_preview_detail_expected_attempt",
        ),
    )

    op.create_table(
        "library_projection_outbox",
        # This table is new in 0002. tenancy_schema fills this routing value
        # from library_item_version before installing final scoped FKs.
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column("conversion_attempt_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default="conversion",
        ),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("safe_error_code", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("completed_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "projection_kind IN ('fts', 'semantic', 'citation', 'preview')",
            name="library_projection_outbox_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="library_projection_outbox_status_check",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="library_projection_outbox_generation_check",
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="library_projection_outbox_retry_count_check",
        ),
        sa.CheckConstraint(
            "stage = 'conversion'",
            name="library_projection_outbox_stage_check",
        ),
        sa.ForeignKeyConstraint(
            ["conversion_attempt_id", "library_item_version_id", "stage"],
            [
                "library_attempt.id",
                "library_attempt.library_item_version_id",
                "library_attempt.stage",
            ],
            name="fk_library_projection_outbox_attempt_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_item_version_id"],
            ["library_item_version.id"],
            name="fk_library_projection_outbox_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversion_attempt_id",
            "projection_kind",
            "generation",
            name="library_projection_outbox_attempt_kind_generation_uq",
        ),
    )
    op.create_index(
        "idx_library_projection_outbox_claim",
        "library_projection_outbox",
        ["status", "next_attempt_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_library_projection_outbox_version",
        "library_projection_outbox",
        ["library_item_version_id", "projection_kind"],
        unique=False,
    )

    op.create_table(
        "library_logical_quota_usage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("quota_kind", sa.Text(), nullable=False),
        sa.Column("limit_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "charged_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quota_kind IN ('source', 'derived')",
            name="ck_library_logical_quota_kind",
        ),
        sa.CheckConstraint(
            "limit_bytes >= 0",
            name="ck_library_logical_quota_limit",
        ),
        sa.CheckConstraint(
            "charged_bytes >= 0",
            name="ck_library_logical_quota_charged",
        ),
        sa.CheckConstraint(
            "charged_bytes <= limit_bytes",
            name="ck_library_logical_quota_within_limit",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_library_logical_quota_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "quota_kind"),
    )
    op.create_table(
        "library_logical_charge",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("quota_kind", sa.Text(), nullable=False),
        sa.Column("association_id", sa.Uuid(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="charged",
        ),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("released_at", UTCDateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "quota_kind IN ('source', 'derived')",
            name="ck_library_logical_charge_kind",
        ),
        sa.CheckConstraint(
            "byte_size >= 0",
            name="ck_library_logical_charge_byte_size",
        ),
        sa.CheckConstraint(
            "state IN ('charged', 'released')",
            name="ck_library_logical_charge_state",
        ),
        sa.CheckConstraint(
            "(state = 'released' AND released_at IS NOT NULL) OR "
            "(state = 'charged' AND released_at IS NULL)",
            name="ck_library_logical_charge_release",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "quota_kind"],
            [
                "library_logical_quota_usage.workspace_id",
                "library_logical_quota_usage.quota_kind",
            ],
            name="fk_library_logical_charge_usage",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "quota_kind",
            "association_id",
            name="uq_library_logical_charge_association",
        ),
    )
    op.create_index(
        "ix_library_logical_charge_state",
        "library_logical_charge",
        ["workspace_id", "quota_kind", "state"],
    )
    op.execute(
        sa.text(
            "INSERT INTO library_logical_quota_usage "
            "(workspace_id, quota_kind, limit_bytes, charged_bytes, revision, "
            "updated_at) "
            "SELECT id, 'source', 10000000000, 0, 0, CURRENT_TIMESTAMP "
            "FROM workspace UNION ALL "
            "SELECT id, 'derived', 10000000000, 0, 0, CURRENT_TIMESTAMP "
            "FROM workspace"
        )
    )


def _downgrade_library_multiformat() -> None:
    op.drop_index(
        "ix_library_logical_charge_state",
        table_name="library_logical_charge",
    )
    op.drop_table("library_logical_charge")
    op.drop_table("library_logical_quota_usage")

    item_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("library_item")
    }
    if "current_ready_version_id" in item_columns:
        op.drop_index(
            "ix_library_item_current_ready_version_id",
            table_name="library_item",
        )
        op.drop_index(
            "ix_library_item_current_preview_version_id",
            table_name="library_item",
        )
        with op.batch_alter_table("library_item") as batch_op:
            batch_op.drop_column("current_ready_version_id")
            batch_op.drop_column("current_preview_version_id")

    op.drop_index(
        "idx_library_projection_outbox_version",
        table_name="library_projection_outbox",
    )
    op.drop_index(
        "idx_library_projection_outbox_claim",
        table_name="library_projection_outbox",
    )
    op.drop_table("library_projection_outbox")

    op.drop_table("library_run_preview_detail")
    op.drop_table("library_attempt_preview_detail")
    op.drop_table("library_artifact_preview_detail")
    op.drop_table("library_artifact_conversion_detail")

    op.drop_index("idx_library_artifact_blob", table_name="library_artifact")
    op.drop_index(
        "idx_library_artifact_attempt_role",
        table_name="library_artifact",
    )
    _detach_library_artifact_dependents()
    op.drop_table("library_artifact")
    # Restore 0001's original simple library_artifact shape exactly. Upgrade
    # replaces that whole table directly, so there is no separate transient
    # ALTER just to remove search_doc.
    op.create_table(
        "library_artifact",
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.Text(), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("extraction_method", sa.Text(), nullable=True),
        sa.Column("index_text", sa.Text(), nullable=True),
        sa.Column("search_doc", sa.Text(), nullable=True),
        sa.Column("location_model", sa.JSON(), nullable=False),
        sa.Column("quality_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 0001_initial_release's own _create_foreign_keys() step installs
        # this FK by name after its create_table('library_artifact', ...)
        # call, and its downgrade's _drop_foreign_keys() step later expects
        # to find it -- restore it here so that later step doesn't fail.
        sa.ForeignKeyConstraint(
            ["library_item_version_id"],
            ["library_item_version.id"],
            name="fk_library_artifact_library_item_version_id_library_dce074d6",
            ondelete="CASCADE",
        ),
    )
    _reattach_library_artifact_dependents()

    version_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("library_item_version")
    }
    # This module is driven two ways: by versions/0002_epic1.py, where
    # tenancy_schema._revert_final_table_constraints has already dropped every
    # column below (so this probe is False and the blocks are skipped), and
    # directly by its own tests, where nothing else has run and they are still
    # present. The probe distinguishes the two; it is not a defensive guess.
    #
    # The hazard it creates is that an entry vanishing from tenancy's list
    # would flip this probe True and be silently compensated for here, leaving
    # the round-trip schema comparison green. That list is therefore pinned by
    # test_tenancy_owns_the_library_item_version_multiformat_drop rather than
    # left to this probe to notice.
    owns_version_columns = "selected_conversion_attempt_id" in version_columns
    if owns_version_columns:
        op.drop_index(
            "ix_library_item_version_selected_conversion_attempt_id",
            table_name="library_item_version",
        )
        op.drop_index(
            "ix_library_item_version_selected_preview_attempt_id",
            table_name="library_item_version",
        )

    op.drop_table("library_attempt_conversion_detail")
    op.drop_index(
        "idx_library_attempt_version_status",
        table_name="library_attempt",
    )
    op.drop_index(
        "idx_library_attempt_claim",
        table_name="library_attempt",
    )
    op.drop_table("library_attempt")
    op.drop_table("library_run_conversion_detail")
    op.drop_index(
        "idx_library_run_version_status",
        table_name="library_run",
    )
    op.drop_index(
        "idx_library_run_claim",
        table_name="library_run",
    )
    op.drop_table("library_run")

    if owns_version_columns:
        op.drop_index(
            "idx_library_item_version_processing_claim",
            table_name="library_item_version",
        )
        op.drop_index(
            "idx_library_item_version_source_blob",
            table_name="library_item_version",
        )
        op.drop_index(
            "ix_library_item_version_sync_run_id",
            table_name="library_item_version",
        )
        with op.batch_alter_table("library_item_version") as batch_op:
            batch_op.drop_column("selected_semantic_overlay_id")
            batch_op.drop_column("selected_preview_attempt_id")
            batch_op.drop_column("preview_status")
            batch_op.drop_column("selected_conversion_attempt_id")
            batch_op.drop_column("detection_json")
            batch_op.drop_column("byte_size")
            batch_op.drop_column("format_id")
            batch_op.drop_column("detected_media_type")
            batch_op.drop_column("declared_media_type")
            batch_op.drop_column("original_filename")
            batch_op.drop_column("source_blob_epoch")
            batch_op.drop_column("source_blob_id")
            batch_op.drop_column("source_blob_workspace_id")
            batch_op.drop_column("sync_run_id")
            batch_op.alter_column(
                "raw_storage_ref",
                existing_type=sa.Text(),
                nullable=False,
            )

def _backfill_current_ready_versions(connection: Connection) -> None:
    connection.execute(
        sa.text(
            "UPDATE library_item SET current_ready_version_id = CASE "
            "WHEN EXISTS ("
            "SELECT 1 FROM library_item_version AS current_version "
            "WHERE current_version.id = library_item.current_version_id "
            "AND current_version.library_item_id = library_item.id "
            "AND current_version.processing_status = 'completed'"
            ") THEN current_version_id ELSE ("
            "SELECT completed_version.id FROM library_item_version "
            "AS completed_version "
            "WHERE completed_version.library_item_id = library_item.id "
            "AND completed_version.processing_status = 'completed' "
            "ORDER BY completed_version.version_no DESC, "
            "completed_version.created_at DESC, completed_version.id DESC LIMIT 1"
            ") END"
        )
    )


def _refuse_lossy_integrated_media_downgrade(connection: Connection) -> None:
    _refuse_lossy_library_downgrade(connection)
    populated_context_or_media = connection.scalar(
        sa.text(
            "SELECT CASE WHEN "
            "EXISTS (SELECT 1 FROM context_media_asset) OR "
            "EXISTS (SELECT 1 FROM context_media_usage) OR "
            "EXISTS (SELECT 1 FROM context_media_upload_request) OR "
            "EXISTS (SELECT 1 FROM context_media_gc_candidate) OR "
            "EXISTS (SELECT 1 FROM context_media_logical_charge) OR "
            "EXISTS (SELECT 1 FROM context_media_logical_quota_usage "
            "WHERE charged_bytes <> 0) OR "
            "EXISTS (SELECT 1 FROM media_staging_lease) OR "
            "EXISTS (SELECT 1 FROM stored_blob) OR "
            "EXISTS (SELECT 1 FROM blob_incarnation) OR "
            "EXISTS (SELECT 1 FROM stored_blob_inspection) OR "
            "EXISTS (SELECT 1 FROM media_physical_charge) OR "
            "EXISTS (SELECT 1 FROM media_deletion_receipt) OR "
            "EXISTS (SELECT 1 FROM media_install_request) OR "
            "EXISTS (SELECT 1 FROM media_outbox) OR "
            "EXISTS (SELECT 1 FROM security_blocked_blob) OR "
            "EXISTS (SELECT 1 FROM workspace_media_quota_usage "
            "WHERE charged_bytes <> 0) OR "
            "EXISTS (SELECT 1 FROM workspace_media_write_intent "
            "WHERE state <> 'open' OR delete_generation <> 0) "
            "THEN 1 ELSE 0 END"
        )
    )
    if populated_context_or_media:
        raise RuntimeError(
            "Refusing downgrade from the Epic-1 media phase: "
            "Context or Shared Media state would be lost. Export or remove "
            "the media data through an explicit compatibility procedure first."
        )


def _refuse_lossy_library_downgrade(connection: Connection) -> None:
    populated_new_tables = connection.scalar(
        sa.text(
            "SELECT CASE WHEN "
            "EXISTS (SELECT 1 FROM library_run) OR "
            "EXISTS (SELECT 1 FROM library_attempt) OR "
            "EXISTS (SELECT 1 FROM library_projection_outbox) OR "
            "EXISTS (SELECT 1 FROM library_logical_charge) OR "
            "EXISTS (SELECT 1 FROM library_logical_quota_usage "
            "WHERE charged_bytes <> 0) "
            "THEN 1 ELSE 0 END"
        )
    )
    version_provenance = connection.scalar(
        sa.text(
            "SELECT 1 FROM library_item_version WHERE "
            "source_blob_workspace_id IS NOT NULL OR source_blob_id IS NOT NULL OR "
            "source_blob_epoch IS NOT NULL OR original_filename IS NOT NULL OR "
            "declared_media_type IS NOT NULL OR detected_media_type IS NOT NULL OR "
            "format_id IS NOT NULL OR byte_size IS NOT NULL OR "
            "detection_json IS NOT NULL OR "
            "selected_conversion_attempt_id IS NOT NULL OR "
            "processing_status NOT IN ('pending', 'ready', 'completed', 'failed') "
            "LIMIT 1"
        )
    )
    # library_artifact's attempt/blob/role/media_type/content_sha256/byte_size
    # columns are all NOT NULL in the current shape (the legacy nullable
    # "storage_ref only" shape is retired -- §6.3), so any existing row
    # already carries full non-legacy provenance.
    artifact_provenance = connection.scalar(
        sa.text("SELECT 1 FROM library_artifact LIMIT 1")
    )
    if populated_new_tables or version_provenance or artifact_provenance:
        raise RuntimeError(
            "Refusing downgrade from the Epic-1 media phase: "
            "non-legacy Library binary or conversion provenance would be lost. "
            "Disable conversion with a schema-compatible release or export and "
            "restore through a verified compatibility procedure."
        )


def _upgrade_context_media() -> None:
    op.create_table(
        "context_media_logical_quota_usage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("limit_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "charged_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "limit_bytes >= 0",
            name="ck_context_logical_quota_limit",
        ),
        sa.CheckConstraint(
            "charged_bytes >= 0",
            name="ck_context_logical_quota_charged",
        ),
        sa.CheckConstraint(
            "charged_bytes <= limit_bytes",
            name="ck_context_logical_quota_within_limit",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_context_logical_quota_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace_media_write_intent.workspace_id"],
            name="fk_context_logical_quota_intent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "context_media_logical_charge",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="charged",
        ),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("released_at", UTCDateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "byte_size >= 0",
            name="ck_context_logical_charge_byte_size",
        ),
        sa.CheckConstraint(
            "state IN ('charged', 'released')",
            name="ck_context_logical_charge_state",
        ),
        sa.CheckConstraint(
            "(state = 'released' AND released_at IS NOT NULL) OR "
            "(state = 'charged' AND released_at IS NULL)",
            name="ck_context_logical_charge_release",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["context_media_logical_quota_usage.workspace_id"],
            name="fk_context_logical_charge_usage",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "asset_id",
            name="uq_context_logical_charge_asset",
        ),
    )
    op.create_index(
        "ix_context_logical_charge_state",
        "context_media_logical_charge",
        ["workspace_id", "state"],
    )
    op.execute(
        sa.text(
            "INSERT INTO context_media_logical_quota_usage "
            "(workspace_id, limit_bytes, charged_bytes, revision, updated_at) "
            "SELECT id, 10000000000, 0, 0, CURRENT_TIMESTAMP FROM workspace"
        )
    )

    op.create_table(
        "context_media_asset",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("created_for_version_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("inspection_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_upload_asset_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="image"),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("canonical_media_type", sa.Text(), nullable=False),
        sa.Column("logical_bytes", sa.BigInteger(), nullable=False),
        sa.Column("policy_revision", sa.Text(), nullable=False),
        sa.Column(
            "association_state", sa.Text(), nullable=False, server_default="active"
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_by_actor_kind", sa.Text(), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("tombstoned_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('image', 'audio', 'video')",
            name="ck_context_media_asset_kind",
        ),
        sa.CheckConstraint(
            "association_state IN ('pending_install', 'active', 'tombstoned')",
            name="ck_context_media_asset_state",
        ),
        sa.CheckConstraint("logical_bytes >= 0", name="ck_context_media_asset_bytes"),
        sa.CheckConstraint("blob_epoch > 0", name="ck_context_media_asset_epoch"),
        sa.CheckConstraint("revision > 0", name="ck_context_media_asset_revision"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_id"],
            ["context_page.workspace_id", "context_page.id"],
            name="fk_context_media_asset_page",
            ondelete="RESTRICT",
        ),
        # P3.1/P3.2: asset owner is page alone now (ContextMediaSession and
        # its claim/XOR are gone). `created_for_version_id` is provenance and
        # the orphan-protection/uninserted-preview basis, not an alternate
        # owner -- scoped to the SAME page as the asset via a 3-column FK
        # into `context_page_version` (workspace_id, page_id, id), so an
        # asset can never name a version belonging to a different page.
        # That FK is declared by tenancy_schema.py instead, in its _TABLES
        # entry for context_media_asset (_new_owned), and created by that
        # phase's _apply_final_table_constraints(). It is kept there for
        # uniformity with the other workspace-scoped FKs that phase owns --
        # NOT because the parent is unavailable here. The comment used to say
        # the parent key does not exist until tenancy_schema backfills it,
        # which stopped being true once context_schema.upgrade() was ordered
        # before this module (0002_epic1.py phase order): both
        # context_page_version.workspace_id and
        # uq_context_page_version_workspace_page_owner already exist by now.
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_context_media_asset_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["workspace_id", "legacy_upload_asset_id"],
            ["upload_asset.workspace_id", "upload_asset.id"],
            name="fk_context_media_asset_legacy_upload",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id", "page_id", "id", name="uq_context_media_asset_page"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "legacy_upload_asset_id",
            name="uq_context_media_asset_legacy_upload",
        ),
    )
    op.create_index(
        "ix_context_media_asset_page",
        "context_media_asset",
        ["workspace_id", "page_id", "created_at"],
    )
    op.create_index(
        "ix_context_media_asset_created_for_version",
        "context_media_asset",
        ["workspace_id", "created_for_version_id"],
    )

    op.create_table(
        "context_media_usage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("page_version_id", sa.Uuid(), nullable=False),
        sa.Column("reference_count", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reference_count > 0", name="ck_context_media_usage_reference_count"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_id", "asset_id"],
            [
                "context_media_asset.workspace_id",
                "context_media_asset.page_id",
                "context_media_asset.id",
            ],
            name="fk_context_media_usage_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["page_id", "page_version_id"],
            ["context_page_version.page_id", "context_page_version.id"],
            name="fk_context_media_usage_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "page_id", "asset_id", "page_version_id"
        ),
    )
    op.create_index(
        "ix_context_media_usage_version",
        "context_media_usage",
        ["page_id", "page_version_id", "asset_id"],
    )
    op.create_index(
        "ix_context_media_usage_asset",
        "context_media_usage",
        ["workspace_id", "asset_id", "page_version_id"],
    )

    op.create_table(
        "context_media_upload_request",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("marker_uuid", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("editing_version_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="ck_context_media_upload_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["context_media_asset.workspace_id", "context_media_asset.id"],
            name="fk_context_media_upload_asset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "marker_uuid"),
        sa.UniqueConstraint(
            "workspace_id", "asset_id", name="uq_context_media_upload_asset"
        ),
    )

    op.create_table(
        "context_media_gc_candidate",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("blob_epoch", sa.BigInteger(), nullable=False),
        sa.Column("asset_revision", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued', 'cancelled')",
            name="ck_context_media_gc_candidate_state",
        ),
        sa.CheckConstraint(
            "blob_epoch > 0 AND asset_revision > 0",
            name="ck_context_media_gc_candidate_generations",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["context_media_asset.workspace_id", "context_media_asset.id"],
            name="fk_context_media_gc_candidate_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "blob_id", "blob_epoch"],
            [
                "blob_incarnation.workspace_id",
                "blob_incarnation.blob_id",
                "blob_incarnation.blob_epoch",
            ],
            name="fk_context_media_gc_candidate_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id", "asset_id", name="uq_context_media_gc_candidate_asset"
        ),
    )
    op.create_index(
        "ix_context_media_gc_candidate_state",
        "context_media_gc_candidate",
        ["state", "created_at", "id"],
    )


def _downgrade_context_media() -> None:
    op.drop_index(
        "ix_context_media_gc_candidate_state",
        table_name="context_media_gc_candidate",
    )
    op.drop_table("context_media_gc_candidate")
    op.drop_table("context_media_upload_request")
    op.drop_index("ix_context_media_usage_asset", table_name="context_media_usage")
    op.drop_index("ix_context_media_usage_version", table_name="context_media_usage")
    op.drop_table("context_media_usage")
    op.drop_index(
        "ix_context_media_asset_created_for_version", table_name="context_media_asset"
    )
    op.drop_index("ix_context_media_asset_page", table_name="context_media_asset")
    op.drop_table("context_media_asset")
    op.drop_index(
        "ix_context_logical_charge_state",
        table_name="context_media_logical_charge",
    )
    op.drop_table("context_media_logical_charge")
    op.drop_table("context_media_logical_quota_usage")


def _upgrade_preview_lifecycle() -> None:
    # A-1: this phase used to
    # create a fully independent library_preview_run/attempt/artifact table
    # family here. preview_generation is now just a stage of the unified
    # library_run/library_attempt/library_artifact tables that
    # library_media_schema.py creates (earlier in this same 0002_epic1
    # revision -- see backend/models/library.py). Only the pieces below
    # that are NOT about that now-redundant table family survive: the
    # current_preview_version_id/preview_status columns are unrelated
    # per-column additions, and selected_preview_attempt_id's ownership FK
    # now simply targets the unified library_attempt table instead of the
    # retired library_preview_attempt.
    #
    # library_semantic_overlay is created after the final artifact table. Its
    # selected pointer is still only an additive nullable column here; the
    # reciprocal final FK joins library_item_version's one tenancy batch.
    _create_semantic_overlay_table()

    op.add_column(
        "library_item_version",
        sa.Column("selected_semantic_overlay_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_library_item_version_selected_semantic_overlay_id",
        "library_item_version",
        ["selected_semantic_overlay_id"],
    )

# Semantic Markdown overlay schema. The table stands on its own; the
# library_item_version pointer and ownership FK are finalized later by
# tenancy_schema.
def _create_semantic_overlay_table() -> None:
    # library_artifact_id_version_uq is now created directly on
    # library_artifact by library_media_schema.py (A-1 unification), so
    # there is nothing left to add here.
    op.create_table(
        "library_semantic_overlay",
        # This table is new in 0002. tenancy_schema fills this routing value
        # from library_item_version before installing final scoped FKs.
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("library_item_version_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("base_artifact_sha256", sa.Text(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_kind", sa.Text(), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("updated_by_actor_kind", sa.Text(), nullable=False),
        sa.Column("updated_by_actor_id", sa.Text(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(base_artifact_sha256) = 64",
            name="library_semantic_overlay_base_sha256_check",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="library_semantic_overlay_content_sha256_check",
        ),
        sa.CheckConstraint(
            "byte_size >= 0",
            name="library_semantic_overlay_byte_size_check",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="library_semantic_overlay_revision_check",
        ),
        sa.ForeignKeyConstraint(
            ["library_item_version_id"],
            ["library_item_version.id"],
            name="fk_library_semantic_overlay_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["base_artifact_id", "library_item_version_id"],
            ["library_artifact.id", "library_artifact.library_item_version_id"],
            name="fk_library_semantic_overlay_base_artifact_ownership",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "library_item_version_id",
            name="library_semantic_overlay_id_version_uq",
        ),
        sa.UniqueConstraint(
            "library_item_version_id",
            "base_artifact_id",
            name="library_semantic_overlay_version_base_uq",
        ),
    )
    op.create_index(
        "idx_library_semantic_overlay_version_base",
        "library_semantic_overlay",
        ["library_item_version_id", "base_artifact_id"],
    )


def _downgrade_semantic_overlay() -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("library_item_version")
    }
    if "ix_library_item_version_selected_semantic_overlay_id" in indexes:
        op.drop_index(
            "ix_library_item_version_selected_semantic_overlay_id",
            table_name="library_item_version",
        )
    op.drop_index(
        "idx_library_semantic_overlay_version_base",
        table_name="library_semantic_overlay",
    )
    op.drop_table("library_semantic_overlay")

    # library_artifact_id_version_uq belongs to library_artifact's own
    # creation (library_media_schema.py) now, so there is nothing to drop
    # here.


# PostgreSQL refuses ``DROP TABLE`` while another table's foreign key still
# references it (SQLSTATE 2BP01), and these two point at ``library_artifact``
# from tables this module does not own. SQLite neither enforces that
# dependency nor can ALTER a constraint outside a batch rebuild, so it keeps
# the plain drop/recreate. The pair is restored in its 0001 shape rather than
# its final one because ``tenancy_schema._apply_final_table_constraints`` still
# expects to find and replace exactly these names.
_LIBRARY_ARTIFACT_INBOUND_FKS = (
    (
        "agent_input_bundle_item",
        "fk_agent_input_bundle_item_library_artifact_id_libr_a4d03d74",
    ),
    (
        "context_citation",
        "fk_context_citation_library_artifact_id_library_artifact",
    ),
)


def _detach_library_artifact_dependents() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table_name, constraint_name in _LIBRARY_ARTIFACT_INBOUND_FKS:
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")


def _reattach_library_artifact_dependents() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table_name, constraint_name in _LIBRARY_ARTIFACT_INBOUND_FKS:
        op.create_foreign_key(
            constraint_name,
            table_name,
            "library_artifact",
            ["library_artifact_id"],
            ["id"],
            ondelete="RESTRICT",
        )
