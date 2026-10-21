"""add workspace context media retention policy

Revision ID: d148b9746ff5
Revises: efcbcf724889
Create Date: 2026-10-19 00:30:21.955131
"""

import sqlalchemy as sa
from alembic import op

revision = "d148b9746ff5"
down_revision = "efcbcf724889"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column(
            "context_media_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspace", "context_media_policy")
