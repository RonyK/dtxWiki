"""Replace stored document locators with identities and canonical URLs.

Revision ID: e4f7a2c901bd
Revises: 872d9c3e104b
"""

from alembic import op

from backend.migrations.revision_modules.unreleased._e4f7a2c901bd_locators import (
    conversion,
)

revision = "e4f7a2c901bd"
down_revision = "872d9c3e104b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    conversion.convert_locators(connection)
    conversion.convert_command_receipts(connection)
    conversion.enqueue_reindex(connection)


def downgrade() -> None:
    raise RuntimeError(
        "Document identity conversion is not reversible; "
        "restore the pre-upgrade dtxWiki backup"
    )
