"""Index workspace-scoped MCP conversation reads.

Revision ID: b1d98889a4ad
Revises: 1d8f1c08ca36
Create Date: 2026-11-03 00:36:09.836473

Chats lists and details select calls by workspace and conversation, ordered
by occurrence with a stable ID tie-breaker. The existing workspace unique
keys cannot narrow those reads to one conversation. This non-unique index
also supports the list projection's page-wide conversation IN lookup.

Only an index is added: existing traces and their JSON payloads are unchanged.
Building it scans existing rows and takes the dialect's normal index-build
locks; use the ordinary upgrade flow. Downgrade drops only this index, and
re-upgrade recreates it. Keep the ORM declaration aligned with this revision.
"""

from alembic import op

revision = "b1d98889a4ad"
down_revision = "1d8f1c08ca36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_mcp_tool_call_workspace_session_started",
        "mcp_tool_call",
        ["workspace_id", "mcp_session_id", "started_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_tool_call_workspace_session_started", table_name="mcp_tool_call"
    )
