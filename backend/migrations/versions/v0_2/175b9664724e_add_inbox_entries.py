"""Add per-user Inbox entries for workspaces and accounts.

Revision ID: 175b9664724e
Revises: 333a67e22cd7
Create Date: 2026-10-28 14:23:11.150546

What this adds
--------------
``workspace_inbox_entry`` (workspace plane) and ``account_inbox_entry``
(control plane) share one shape. A ``stored`` row is a message a producer
wrote in the same transaction as its source change, plus the recipient's
marks. A ``mark`` row holds only the recipient's marks for an item a computed
source derives on read. The mode CHECK keeps the two shapes apart: a stored
row needs ``revision`` and ``event_at``; a mark row has neither, and no
``ref``.

The workspace table carries bare user UUIDs, like ``user_favorite``, so it
stays on the workspace shard. The account table references ``users`` with
ON DELETE CASCADE. ``kind`` has no CHECK: kinds are registered in code, so
adding one needs no migration.

Both list indexes lead with the recipient and mode, then follow the keyset
order (event_at, kind, item_key) the list endpoint pages by.

Deployment and rollback
-----------------------
New tables only; no existing row, column or constraint changes, and no
backfill. Downgrade drops both tables with their rows (personal marks and any
stored messages). Re-upgrade recreates them empty.
"""

import sqlalchemy as sa
from alembic import op

revision = "175b9664724e"
down_revision = "333a67e22cd7"
branch_labels = None
depends_on = None

_MODE_CHECK = "mode IN ('stored', 'mark')"
_MODE_COLUMNS_CHECK = (
    "(mode = 'stored' AND revision IS NOT NULL AND event_at IS NOT NULL)"
    " OR (mode = 'mark' AND revision IS NULL AND ref IS NULL"
    " AND event_at IS NULL)"
)


def _entry_columns() -> list[sa.Column]:
    return [
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("item_key", sa.Text(), primary_key=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=True),
        sa.Column("ref", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_revision", sa.Text(), nullable=True),
        sa.Column("archived_revision", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "workspace_inbox_entry",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        *_entry_columns(),
        sa.CheckConstraint(_MODE_CHECK, name="workspace_inbox_entry_mode_check"),
        sa.CheckConstraint(
            _MODE_COLUMNS_CHECK, name="workspace_inbox_entry_mode_columns_check"
        ),
    )
    op.create_index(
        "ix_workspace_inbox_entry_list",
        "workspace_inbox_entry",
        ["workspace_id", "user_id", "mode", "event_at", "kind", "item_key"],
    )
    op.create_table(
        "account_inbox_entry",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        *_entry_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_MODE_CHECK, name="account_inbox_entry_mode_check"),
        sa.CheckConstraint(
            _MODE_COLUMNS_CHECK, name="account_inbox_entry_mode_columns_check"
        ),
    )
    op.create_index(
        "ix_account_inbox_entry_list",
        "account_inbox_entry",
        ["user_id", "mode", "event_at", "kind", "item_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_inbox_entry_list", table_name="account_inbox_entry")
    op.drop_table("account_inbox_entry")
    op.drop_index("ix_workspace_inbox_entry_list", table_name="workspace_inbox_entry")
    op.drop_table("workspace_inbox_entry")
