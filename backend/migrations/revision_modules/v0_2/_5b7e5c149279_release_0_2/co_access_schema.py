"""Final v0.2 lifetime co-access storage and fixed historical replay."""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_2 import lifetime_co_access_v1

VIEWED_SESSION_INDEX = "ix_mcp_chat_tool_call_artifact_viewed_session"
VIEWED_PAIR_INDEX = "ix_mcp_chat_tool_call_artifact_viewed_pair"


def upgrade() -> None:
    op.create_table(
        "co_access_pair",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("support_session_count", sa.Integer(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            name="uq_co_access_pair_identity",
        ),
        sa.CheckConstraint(
            "source_kind < target_kind OR "
            "(source_kind = target_kind AND source_id < target_id)",
            name="ck_co_access_pair_canonical_order",
        ),
        sa.CheckConstraint(
            "support_session_count >= 1",
            name="ck_co_access_pair_positive_support",
        ),
        sa.CheckConstraint(
            "first_observed_at <= occurred_at",
            name="ck_co_access_pair_observed_span",
        ),
    )
    op.create_index(
        VIEWED_SESSION_INDEX,
        "mcp_chat_tool_call_artifact",
        [
            "workspace_id",
            "chat_session_id",
            "occurred_at",
            "mcp_tool_call_id",
            "id",
        ],
        sqlite_where=sa.text("verb = 'viewed'"),
        postgresql_where=sa.text("verb = 'viewed'"),
    )
    op.add_column(
        "mcp_chat_session", sa.Column("co_access_state", sa.JSON(), nullable=True)
    )
    op.add_column(
        "mcp_chat_tool_call_artifact",
        sa.Column("co_access_segment", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mcp_chat_tool_call_artifact",
        sa.Column("co_access_position", sa.Integer(), nullable=True),
    )
    # Replay each workspace's views once for pair counts and cursor positions.
    # Build the position index afterwards to avoid per-row index rewrites.
    lifetime_co_access_v1.upgrade(op.get_bind())
    op.create_index(
        VIEWED_PAIR_INDEX,
        "mcp_chat_tool_call_artifact",
        [
            "workspace_id",
            "chat_session_id",
            "resource_kind",
            "resource_id",
            "co_access_segment",
            "co_access_position",
        ],
        sqlite_where=sa.text("verb = 'viewed'"),
        postgresql_where=sa.text("verb = 'viewed'"),
    )


def downgrade() -> None:
    lifetime_co_access_v1.downgrade(op.get_bind())
    op.drop_index(VIEWED_PAIR_INDEX, table_name="mcp_chat_tool_call_artifact")
    with op.batch_alter_table("mcp_chat_tool_call_artifact") as batch:
        batch.drop_column("co_access_position")
        batch.drop_column("co_access_segment")
    op.drop_column("mcp_chat_session", "co_access_state")
    op.drop_index(VIEWED_SESSION_INDEX, table_name="mcp_chat_tool_call_artifact")
    op.drop_table("co_access_pair")
