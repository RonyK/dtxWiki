"""Record keyed MCP attempts outside their domain transaction.

Revision ID: 872d9c3e104b
Revises: 74e5d4f912a6
"""

import sqlalchemy as sa
from alembic import op

revision = "872d9c3e104b"
down_revision = "74e5d4f912a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_operation_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("authorization_context", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "actor_kind",
            "actor_id",
            "tool_name",
            "idempotency_key",
            name="uq_mcp_operation_attempt_scope",
        ),
    )
    op.create_index(
        "ix_mcp_operation_attempt_actor_key",
        "mcp_operation_attempt",
        ["workspace_id", "actor_kind", "actor_id", "idempotency_key", "started_at"],
    )
    op.create_index(
        "ix_mcp_operation_attempt_expiry",
        "mcp_operation_attempt",
        ["workspace_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("mcp_operation_attempt")
