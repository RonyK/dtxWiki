"""Add version-owned bibliography and final publication citation projections.

Revision ID: c58f1d2e890a
Revises: 1d8f1c08ca36

New tables only. Existing bodies, evidence and graph rows are untouched.
Downgrade removes the new bibliography data; re-upgrade recreates empty tables.
"""

import sqlalchemy as sa
from alembic import op

revision = "c58f1d2e890a"
down_revision = "1d8f1c08ca36"
branch_labels = None
depends_on = None


def _target_columns():
    return [
        sa.Column("target_kind", sa.Text(), nullable=False),
        *(
            sa.Column(name, sa.Uuid(), nullable=True)
            for name in ("context_page_id", "library_item_id", "agent_page_id")
        ),
    ]


def _constraints(prefix, owner):
    return [
        sa.ForeignKeyConstraint(
            ["workspace_id", owner],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name=f"fk_{prefix}_owner",
            ondelete="CASCADE",
        ),
        *(
            sa.ForeignKeyConstraint(
                ["workspace_id", column],
                [f"{target}.workspace_id", f"{target}.id"],
                name=f"fk_{prefix}_{kind}",
                ondelete="RESTRICT",
            )
            for kind, column, target in (
                ("context", "context_page_id", "context_page"),
                ("library", "library_item_id", "library_item"),
                ("agent", "agent_page_id", "agent_page"),
            )
        ),
        sa.CheckConstraint(
            "(target_kind = 'context_page' AND context_page_id IS NOT NULL "
            "AND library_item_id IS NULL AND agent_page_id IS NULL) "
            "OR (target_kind = 'library_item' AND library_item_id IS NOT NULL "
            "AND context_page_id IS NULL AND agent_page_id IS NULL) "
            "OR (target_kind = 'agent_page' AND agent_page_id IS NOT NULL "
            "AND context_page_id IS NULL AND library_item_id IS NULL)",
            name=f"ck_{prefix}_target",
        ),
        *(
            sa.UniqueConstraint(owner, column, name=f"uq_{prefix}_{column}")
            for column in ("context_page_id", "library_item_id", "agent_page_id")
        ),
    ]


def upgrade():
    op.create_table(
        "context_document_reference",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_version_id", sa.Uuid(), nullable=False),
        *_target_columns(),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_by_actor_kind", sa.Text(), nullable=True),
        sa.Column("created_by_actor_id", sa.Text(), nullable=True),
        *_constraints("context_doc_ref", "owner_version_id"),
    )
    op.create_index(
        "ix_context_document_reference_owner_version_id",
        "context_document_reference",
        ["owner_version_id"],
    )
    op.create_table(
        "context_published_citation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_version_id", sa.Uuid(), nullable=False),
        *_target_columns(),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("metadata_present", sa.Boolean(), nullable=False),
        sa.Column("inline_count", sa.Integer(), nullable=False),
        sa.Column("legacy_count", sa.Integer(), nullable=False),
        sa.Column("occurrences", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        *_constraints("context_pub_cite", "source_version_id"),
    )
    op.create_index(
        "ix_context_published_citation_source_version_id",
        "context_published_citation",
        ["source_version_id"],
    )
    op.create_table(
        "context_citation_publication",
        sa.Column("version_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("format_revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "version_id"],
            ["context_page_version.workspace_id", "context_page_version.id"],
            name="fk_context_cite_publication_owner",
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("context_citation_publication")
    op.drop_index(
        "ix_context_published_citation_source_version_id",
        table_name="context_published_citation",
    )
    op.drop_table("context_published_citation")
    op.drop_index(
        "ix_context_document_reference_owner_version_id",
        table_name="context_document_reference",
    )
    op.drop_table("context_document_reference")
