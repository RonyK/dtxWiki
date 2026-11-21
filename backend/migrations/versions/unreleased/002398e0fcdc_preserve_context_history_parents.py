"""Preserve v0.2 Context history parents across Git snapshot conversion.

Revision ID: 002398e0fcdc
Revises: c3d2e4f601ab
Create Date: 2026-11-21 15:03:48.752581
"""

import sqlalchemy as sa
from alembic import context, op

from backend.migrations.revision_modules.unreleased._002398e0fcdc_capture import (
    capture,
)

revision = "002398e0fcdc"
down_revision = "c3d2e4f601ab"
branch_labels = None
depends_on = None

_HISTORY_TABLE = "context_version_history_parent"


def upgrade() -> None:
    if context.is_offline_mode():
        # A capture staged by an earlier run is found only by reading the DB.
        raise RuntimeError("Revision 002398e0fcdc requires an online database")
    history = op.create_table(
        _HISTORY_TABLE,
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), primary_key=True),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        # The first Git parent the conversion assigned. History draws the
        # version's other Git parents beside the saved one.
        sa.Column("converted_parent_version_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "version_id != parent_version_id",
            name="ck_context_version_history_parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_id", "version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_version_history_parent_version_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_id", "parent_version_id"],
            [
                "context_page_version.workspace_id",
                "context_page_version.page_id",
                "context_page_version.id",
            ],
            name="fk_context_version_history_parent_parent_owner",
        ),
    )
    connection = op.get_bind()
    staged = capture.pending(connection)
    if staged is not None:
        versions = sa.table(
            "context_page_version",
            sa.column("workspace_id"),
            sa.column("page_id"),
            sa.column("id"),
        )
        child, parent = versions.alias("child"), versions.alias("parent")
        # Keep only the links the conversion replaced, between versions that
        # still exist on the same page.
        op.execute(
            history.insert().from_select(
                [
                    "workspace_id",
                    "page_id",
                    "version_id",
                    "parent_version_id",
                    "converted_parent_version_id",
                ],
                sa.select(
                    staged.c.workspace_id,
                    staged.c.page_id,
                    staged.c.version_id,
                    staged.c.parent_version_id,
                    staged.c.converted_parent_version_id,
                )
                .select_from(
                    staged.join(
                        child,
                        sa.and_(
                            child.c.workspace_id == staged.c.workspace_id,
                            child.c.page_id == staged.c.page_id,
                            child.c.id == staged.c.version_id,
                        ),
                    ).outerjoin(
                        parent,
                        sa.and_(
                            parent.c.workspace_id == staged.c.workspace_id,
                            parent.c.page_id == staged.c.page_id,
                            parent.c.id == staged.c.parent_version_id,
                        ),
                    )
                )
                .where(
                    staged.c.parent_version_id.is_distinct_from(
                        staged.c.converted_parent_version_id
                    ),
                    sa.or_(
                        staged.c.parent_version_id.is_(None),
                        parent.c.id.is_not(None),
                    ),
                ),
            )
        )
        capture.clear(connection, staged)
    op.create_index(
        "ix_context_version_history_parent_parent_owner",
        _HISTORY_TABLE,
        ["workspace_id", "page_id", "parent_version_id"],
    )


def downgrade() -> None:
    op.drop_table(_HISTORY_TABLE)
