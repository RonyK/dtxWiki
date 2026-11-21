"""Upgrade the published v0.2.3/v0.2.4 schema to v0.3.1.

Revision ID: c3d2e4f601ab
Revises: 00ba02774c4a
"""

from backend.migrations.revision_modules.v0_3._c3d2e4f601ab_release_0_3_1 import schema

revision = "c3d2e4f601ab"
down_revision = "00ba02774c4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.add_context_git_snapshots()
    schema.release_historical_context_bodies()
    schema.track_mcp_call_lifecycle()
    schema.convert_document_identity()
    schema.convert_context_drafts()


def downgrade() -> None:
    raise RuntimeError(
        "Context draft conversion is not reversible. "
        "Restore the pre-upgrade dtxWiki backup."
    )
