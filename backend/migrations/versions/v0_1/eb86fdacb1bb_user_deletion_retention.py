"""Persist account deletion requests and their retention deadlines.

Revision ID: eb86fdacb1bb
Revises: ef53a0a7061c
"""

import sqlalchemy as sa
from alembic import op

revision = "eb86fdacb1bb"
down_revision = "ef53a0a7061c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing users stay active and have no deletion deadline. Nullable
    # columns need no backfill or SQLite table rebuild on upgrade.
    op.add_column(
        "users", sa.Column("deletion_requested_at", sa.DateTime(timezone=True))
    )
    op.add_column("users", sa.Column("purge_after", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("deletion_retention_days", sa.Integer()))
    op.add_column("users", sa.Column("deletion_prior_status", sa.Text()))
    op.add_column("users", sa.Column("deletion_failure_count", sa.Integer()))
    op.add_column("users", sa.Column("deletion_last_error", sa.Text()))
    op.create_index("ix_users_purge_after", "users", ["purge_after"], unique=False)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM users WHERE status = 'deletion_pending')")
    ):
        raise RuntimeError(
            "cannot remove account deletion deadlines while deletion requests "
            "are pending; finish account cleanup before downgrading"
        )
    op.drop_index("ix_users_purge_after", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deletion_last_error")
        batch.drop_column("deletion_failure_count")
        batch.drop_column("deletion_prior_status")
        batch.drop_column("deletion_retention_days")
        batch.drop_column("purge_after")
        batch.drop_column("deletion_requested_at")
