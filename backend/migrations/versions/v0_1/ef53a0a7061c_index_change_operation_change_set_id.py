"""index change_operation change_set_id

Revision ID: ef53a0a7061c
Revises: 19db79d3e622
Create Date: 2026-10-23 23:07:35.435820
"""

from alembic import op

revision = "ef53a0a7061c"
down_revision = "19db79d3e622"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_change_operation_change_set_id",
        "change_operation",
        ["change_set_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_change_operation_change_set_id", table_name="change_operation")
