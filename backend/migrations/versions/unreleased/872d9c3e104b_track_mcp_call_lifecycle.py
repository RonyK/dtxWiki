"""Track keyed MCP call lifecycles in the existing trace table.

Revision ID: 872d9c3e104b
Revises: 9d8695419436
"""

import sqlalchemy as sa
from alembic import op

revision = "872d9c3e104b"
down_revision = "9d8695419436"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_mcp_tool_call_actor_key_started", table_name="mcp_tool_call")
    op.drop_column("mcp_tool_call", "retryable")
    op.drop_column("mcp_tool_call", "idempotency_key")
    op.drop_column("mcp_tool_call", "actor_id")
    op.drop_column("mcp_tool_call", "actor_kind")
