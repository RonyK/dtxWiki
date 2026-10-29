"""Index Context links by their authored locator, preserving existing pins.

Revision ID: af62c98d0731
Revises: 175b9664724e

Downgrade retains wikilink pins only. Other rebuildable link rows and their
pins are discarded before restoring the old slug-based unique constraint;
run ``dtxwiki reindex`` after upgrading again to rebuild those rows.
"""

import sqlalchemy as sa
from alembic import op

revision = "af62c98d0731"
down_revision = "175b9664724e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("page_links", sa.Column("locator_key", sa.Text(), nullable=True))
    op.execute(
        sa.text("UPDATE page_links SET locator_key = 'wikilink:' || target_slug")
    )
    with op.batch_alter_table("page_links") as batch:
        batch.drop_constraint("uq_page_links_source_version_target", type_="unique")
        batch.alter_column("target_slug", existing_type=sa.Text(), nullable=True)
        batch.alter_column("locator_key", existing_type=sa.Text(), nullable=False)
        batch.create_unique_constraint(
            "uq_page_links_source_version_locator",
            ["source_page_id", "source_version_id", "locator_key"],
        )


def downgrade() -> None:
    # NULL-only deletion is insufficient: concept_path:foo and wikilink:foo
    # both carry target_slug=foo but collide under the historical unique key.
    op.execute(
        sa.text(
            "DELETE FROM page_links WHERE locator_key NOT LIKE 'wikilink:%' "
            "OR target_slug IS NULL"
        )
    )
    with op.batch_alter_table("page_links") as batch:
        batch.drop_constraint("uq_page_links_source_version_locator", type_="unique")
        batch.alter_column("target_slug", existing_type=sa.Text(), nullable=False)
        batch.drop_column("locator_key")
        batch.create_unique_constraint(
            "uq_page_links_source_version_target",
            ["source_page_id", "source_version_id", "target_slug"],
        )
