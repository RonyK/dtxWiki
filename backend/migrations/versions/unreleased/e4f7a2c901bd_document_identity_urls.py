"""Replace stored document locators with identities and canonical URLs.

Revision ID: e4f7a2c901bd
Revises: 872d9c3e104b
"""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.unreleased._e4f7a2c901bd_locators import (
    conversion,
    rollback,
)

revision = "e4f7a2c901bd"
down_revision = "872d9c3e104b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_locator_rollback",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("deltas", sa.JSON(), nullable=False),
        sa.Column("row_state", sa.JSON(), nullable=True),
        sa.Column("identity_guard", sa.JSON(), nullable=False),
        sa.Column("text_format", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "table_name", "record_id"),
        sa.CheckConstraint(
            "operation IN ('update', 'insert', 'delete')",
            name="document_locator_rollback_operation_check",
        ),
    )
    rollback.convert(op.get_bind(), conversion)


def downgrade() -> None:
    rollback.restore(op.get_bind())
    op.drop_table("document_locator_rollback")
