"""Release verified historical Context bodies from SQL.

Revision ID: 9d8695419436
Revises: 74e5d4f912a6
Create Date: 2026-11-10 17:27:47.603375
"""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.unreleased._9d8695419436_bodies import (
    context_git_bodies as bodies,
)

revision = "9d8695419436"
down_revision = "74e5d4f912a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    root = bodies.repository_root(op.get_context().config.attributes)
    connection = op.get_bind()
    bodies.verify(connection, root, require_database_bodies=True)
    with op.batch_alter_table("context_page_version") as batch:
        batch.alter_column("body_md", existing_type=sa.Text(), nullable=True)
        # Existing bodies already satisfy this CHECK. Install it with the
        # nullable change so SQLite rebuilds the table once and the following
        # removal remains protected throughout the migration transaction.
        batch.create_check_constraint(
            "context_page_version_body_stored_check",
            "body_md IS NOT NULL OR commit_oid IS NOT NULL",
        )
    bodies.release_historical_bodies(connection)


def downgrade() -> None:
    root = bodies.repository_root(op.get_context().config.attributes)
    connection = op.get_bind()
    # Validate every needed object before changing either SQL data or DDL.
    bodies.verify(connection, root, require_database_bodies=False)
    bodies.restore_database_bodies(connection, root)
    with op.batch_alter_table("context_page_version") as batch:
        batch.drop_constraint("context_page_version_body_stored_check", type_="check")
        batch.alter_column("body_md", existing_type=sa.Text(), nullable=False)
