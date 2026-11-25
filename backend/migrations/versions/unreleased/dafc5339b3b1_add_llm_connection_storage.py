"""Add personal and Workspace LLM connection storage.

Revision ID: dafc5339b3b1
Revises: 002398e0fcdc
Create Date: 2026-11-23 14:40:31.379076
"""

import sqlalchemy as sa
from alembic import op

revision = "dafc5339b3b1"
down_revision = "002398e0fcdc"
branch_labels = None
depends_on = None

_PERSONAL = "user_llm_connection"
_WORKSPACE = "workspace_llm_connection"
_SWITCH = "llm_custom_endpoints_enabled"


def _connection_fields(table: str) -> tuple:
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("adapter_id", sa.Text(), nullable=False),
        sa.Column("adapter_implementation_version", sa.Text(), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("api_profile", sa.Text()),
        sa.Column("endpoint_profile", sa.JSON(), nullable=False),
        sa.Column("provider_options", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("model_profiles", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active_credential", sa.JSON(none_as_null=True)),
        sa.Column("secret_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="disabled"),
        sa.Column(
            "verification_status",
            sa.Text(),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("credential_updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('enabled', 'disabled', 'deleted')",
            name=f"ck_{table}_status",
        ),
        sa.CheckConstraint(
            "verification_status IN "
            "('unverified', 'verifying', 'ready', 'failed', 'stale')",
            name=f"ck_{table}_verification_status",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND secret_revision >= 0 AND config_schema_version >= 1",
            name=f"ck_{table}_revisions",
        ),
        sa.CheckConstraint(
            "status <> 'deleted' OR active_credential IS NULL",
            name=f"ck_{table}_deleted_credentials",
        ),
    )


def upgrade() -> None:
    op.create_table(
        _PERSONAL,
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *_connection_fields(_PERSONAL),
    )
    op.create_index("ix_user_llm_connection_user_id", _PERSONAL, ["user_id"])

    op.create_table(
        _WORKSPACE,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("allowed_roles", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_workspace_llm_connection_scope"
        ),
        *_connection_fields(_WORKSPACE),
    )
    op.add_column(
        "workspace",
        sa.Column(_SWITCH, sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("workspace", _SWITCH)
    op.drop_table(_WORKSPACE)
    op.drop_table(_PERSONAL)
