"""Convert Context drafts to mutable rows with immutable commit parents.

Revision ID: c3d2e4f601ab
Revises: e4f7a2c901bd
"""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.unreleased._c3d2e4f601ab_drafts import (
    conversion,
)

revision = "c3d2e4f601ab"
down_revision = "e4f7a2c901bd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # This value is valid under both checks and releases the author-only index.
    connection.execute(
        sa.text(
            "UPDATE context_page_version SET version_status = 'draft' "
            "WHERE version_status = 'editing'"
        )
    )
    with op.batch_alter_table("context_page_version") as batch:
        batch.drop_index("uq_cpv_current_editing_per_author")
        batch.drop_constraint("context_page_version_status_check", type_="check")
        batch.create_check_constraint(
            "context_page_version_status_check",
            "version_status IN ('draft', 'closed', 'published_snapshot', "
            "'superseded_snapshot', 'discarded')",
        )
        batch.add_column(
            sa.Column("page_metadata", sa.JSON(), nullable=False, server_default="{}")
        )
    op.create_table(
        "context_version_parent",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("version_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 1", name="context_version_parent_ordinal_check"),
        sa.CheckConstraint(
            "version_id != parent_version_id", name="context_version_parent_self_check"
        ),
        sa.UniqueConstraint(
            "version_id", "parent_version_id", name="uq_context_version_parent_identity"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_id", "version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_version_parent_version_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_id", "parent_version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_version_parent_parent_owner",
        ),
    )
    with op.batch_alter_table("content_chunk") as batch:
        batch.drop_constraint("content_chunk_document_scope_ordinal_uq", type_="unique")
        batch.create_unique_constraint(
            "content_chunk_document_scope_ordinal_uq",
            [
                "workspace_id",
                "document_kind",
                "document_id",
                "content_scope",
                "source_version_id",
                "ordinal",
            ],
        )
    conversion.convert(connection, op.get_context().config.attributes)


def downgrade() -> None:
    raise RuntimeError(
        "Context draft conversion is not reversible; "
        "restore the pre-upgrade dtxWiki backup"
    )
