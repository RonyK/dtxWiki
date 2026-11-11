"""Add exact Git snapshots while retaining every DB body and block map.

Revision ID: 74e5d4f912a6
Revises: 00ba02774c4a
"""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_2._74e5d4f912a6_context_git import (
    active_block_maps,
)
from backend.migrations.revision_modules.v0_2._74e5d4f912a6_context_git import (
    context_content_to_git as conversion,
)

revision = "74e5d4f912a6"
down_revision = "00ba02774c4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    root = conversion.repository_root(op.get_context().config.attributes)
    connection = op.get_bind()
    conversion.preflight(connection)
    active_block_maps.upgrade(connection)
    for column in (
        sa.Column("commit_oid", sa.Text(), nullable=True),
        sa.Column(
            "content_format", sa.Text(), nullable=False, server_default="markdown"
        ),
        sa.Column("format_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "media_type", sa.Text(), nullable=False, server_default="text/markdown"
        ),
        sa.Column("encoding", sa.Text(), nullable=True, server_default="utf-8"),
        sa.Column("content_byte_size", sa.BigInteger(), nullable=True),
    ):
        op.add_column("context_page_version", column)
    conversion.convert(connection, root)


def downgrade() -> None:
    conversion.repository_root(op.get_context().config.attributes)
    connection = op.get_bind()
    conversion.require_database_bodies(connection)
    for name in (
        "content_byte_size",
        "encoding",
        "media_type",
        "format_version",
        "content_format",
        "commit_oid",
    ):
        op.drop_column("context_page_version", name)
