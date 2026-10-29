"""drop personal workspace pointer

Revision ID: 5c3395a75eb0
Revises: ef53a0a7061c
Create Date: 2026-10-27 16:22:14.572059

The workspace a new account starts with is an ordinary private workspace, so
nothing records which one it was. Upgrade drops only the pointer; the
workspaces and their memberships stay. Downgrade restores the nullable column
and its constraints without guessing values back.
"""

import sqlalchemy as sa
from alembic import op

revision = "5c3395a75eb0"
down_revision = "ef53a0a7061c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_personal_workspace_id", type_="unique")
        batch_op.drop_constraint(
            "fk_users_personal_workspace_id_workspace",
            type_="foreignkey",
        )
        batch_op.drop_column("personal_workspace_id")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("personal_workspace_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_users_personal_workspace_id_workspace",
            "workspace",
            ["personal_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_users_personal_workspace_id",
            ["personal_workspace_id"],
        )
