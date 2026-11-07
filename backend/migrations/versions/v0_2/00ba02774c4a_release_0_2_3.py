"""Upgrade the published v0.2.1/v0.2.2 schema to v0.2.3.

Persist workspace Labs switches with an all-off default and index Archived
Inbox entries in list order. This consolidates the unpublished Labs and Inbox
steps while retaining their terminal revision and complete schema.

Revision ID: 00ba02774c4a
Revises: 5b7e5c149279
Create Date: 2026-11-06 23:37:45.796314
"""

import sqlalchemy as sa
from alembic import op

revision = "00ba02774c4a"
down_revision = "5b7e5c149279"
branch_labels = None
depends_on = None

_ARCHIVED = "archived_revision IS NOT NULL"
_INDEXES = (
    (
        "ix_workspace_inbox_entry_archived",
        "workspace_inbox_entry",
        ["workspace_id", "user_id"],
    ),
    ("ix_account_inbox_entry_archived", "account_inbox_entry", ["user_id"]),
)


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column("labs_policy", sa.JSON(), nullable=False, server_default="{}"),
    )
    for name, table, owner in _INDEXES:
        op.create_index(
            name,
            table,
            [*owner, "updated_at", "kind", "item_key"],
            sqlite_where=sa.text(_ARCHIVED),
            postgresql_where=sa.text(_ARCHIVED),
        )


def downgrade() -> None:
    for name, table, _owner in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_column("workspace", "labs_policy")
