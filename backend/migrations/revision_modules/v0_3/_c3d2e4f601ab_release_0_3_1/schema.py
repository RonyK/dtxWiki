"""Apply the schema and data changes of the 0.3.1 release."""

import sqlalchemy as sa
from alembic import op

from . import (
    active_block_maps,
    context_content_to_git,
    context_drafts,
    context_git_bodies,
    document_identity,
)


def add_context_git_snapshots() -> None:
    root = context_content_to_git.repository_root(op.get_context().config.attributes)
    connection = op.get_bind()
    context_content_to_git.preflight(connection)
    active_block_maps.upgrade(connection)
    for column in (
        sa.Column("commit_oid", sa.Text(), nullable=True),
        sa.Column(
            "content_format", sa.Text(), nullable=False, server_default="markdown"
        ),
        sa.Column("format_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "media_type", sa.Text(), nullable=False, server_default="text/markdown"
        ),
        sa.Column("encoding", sa.Text(), nullable=True, server_default="utf-8"),
        sa.Column("content_byte_size", sa.BigInteger(), nullable=True),
    ):
        op.add_column("context_page_version", column)
    context_content_to_git.convert(connection, root)


def release_historical_context_bodies() -> None:
    root = context_git_bodies.repository_root(op.get_context().config.attributes)
    connection = op.get_bind()
    context_git_bodies.verify(connection, root)
    with op.batch_alter_table("context_page_version") as batch:
        batch.alter_column("body_md", existing_type=sa.Text(), nullable=True)
        # Existing bodies already satisfy this CHECK. Install it with the
        # nullable change so SQLite rebuilds the table once and the following
        # removal remains protected throughout the migration transaction.
        batch.create_check_constraint(
            "context_page_version_body_stored_check",
            "body_md IS NOT NULL OR commit_oid IS NOT NULL",
        )
    context_git_bodies.release_historical_bodies(connection, root)


def track_mcp_call_lifecycle() -> None:
    op.add_column("mcp_tool_call", sa.Column("actor_kind", sa.Text(), nullable=True))
    op.add_column("mcp_tool_call", sa.Column("actor_id", sa.Text(), nullable=True))
    op.add_column(
        "mcp_tool_call", sa.Column("idempotency_key", sa.Text(), nullable=True)
    )
    op.add_column("mcp_tool_call", sa.Column("retryable", sa.Boolean(), nullable=True))
    op.create_index(
        "ix_mcp_tool_call_actor_key_started",
        "mcp_tool_call",
        [
            "workspace_id",
            "actor_kind",
            "actor_id",
            "idempotency_key",
            "started_at",
            "id",
        ],
        unique=False,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )


def convert_document_identity() -> None:
    connection = op.get_bind()
    document_identity.convert_locators(connection)
    document_identity.convert_command_receipts(connection)
    document_identity.enqueue_reindex(connection)


def convert_context_drafts() -> None:
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
    context_drafts.convert(connection, op.get_context().config.attributes)
