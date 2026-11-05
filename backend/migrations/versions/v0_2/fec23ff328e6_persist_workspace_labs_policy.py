"""Persist workspace Labs switches with an all-off default.

Revision ID: fec23ff328e6
Revises: 5b7e5c149279
Create Date: 2026-11-05 09:56:01.282836
"""

import sqlalchemy as sa
from alembic import op

revision = "fec23ff328e6"
down_revision = "5b7e5c149279"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column("labs_policy", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("workspace", "labs_policy")
