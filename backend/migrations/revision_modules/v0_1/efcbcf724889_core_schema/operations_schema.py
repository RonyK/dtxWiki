"""Operations-plane tables and columns, appended last to the Epic-1 revision.

PURGE_AUTHZ_PLAN S-6: the single-use workspace-purge confirmation table.
``workspace_purge_grant`` is a leaf, workspace-plane-only table (its one FK
targets ``workspace``, itself in ``GLOBAL_TABLES``), so it needs no cycle-break
policy and no hand-placed slot in the purge order -- ``build_purge_plan``
derives that automatically.

``mcp_tool_call.result_detail`` holds the allowlisted facts Chats shows from a
call's response (a context edit's receipt). It is a nullable JSON column added
in place: calls captured before it existed simply have no detail, and nothing
backfills one because the response it would come from was never stored.

Appended last in ``_upgrade_epic1``/first in ``_downgrade_epic1`` per this
consolidated migration's time-ordered phase convention (see
``v0_1/efcbcf724889_core_schema.py``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "workspace_purge_grant",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "workspace_id"),
    )
    op.add_column(
        "mcp_tool_call",
        sa.Column("result_detail", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_tool_call", "result_detail")
    op.drop_table("workspace_purge_grant")
