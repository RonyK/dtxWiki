"""Index archived Inbox entries in Archived list order.

The Archived list pages archived entries newest archive first. Without an
index in that order each page read and sorted the recipient's whole history.
Both indexes are partial, so read marks and unarchived entries stay out.

Revision ID: 00ba02774c4a
Revises: fec23ff328e6
Create Date: 2026-11-06 23:37:45.796314
"""

import sqlalchemy as sa
from alembic import op

revision = "00ba02774c4a"
down_revision = "fec23ff328e6"
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
