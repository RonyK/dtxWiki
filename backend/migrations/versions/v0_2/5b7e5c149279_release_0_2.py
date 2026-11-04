"""Upgrade the published 0.1.4 schema directly to the final v0.2 schema.

The release owns one revision and fixed domain helpers. Preparatory branch
revisions and merge nodes are folded into the final DDL: users and page_links
are each rebuilt at most once per direction. Existing source data and required
backfills are preserved; graph history and bibliography are not invented.

Revision ID: 5b7e5c149279
Revises: ef53a0a7061c
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import context, op

from backend.migrations.revision_modules.v0_2._5b7e5c149279_release_0_2 import (
    co_access_schema,
    document_reference_schema,
    graph_schema,
    identity_schema,
    inbox_schema,
)

revision = "5b7e5c149279"
down_revision = "ef53a0a7061c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _run_release_atomically(_upgrade_release)


def downgrade() -> None:
    _run_release_atomically(_downgrade_release)


def _upgrade_release() -> None:
    identity_schema.upgrade()
    graph_schema.upgrade_columns()
    inbox_schema.upgrade()
    _upgrade_ledger()
    co_access_schema.upgrade()
    document_reference_schema.upgrade()
    op.create_index(
        "ix_review_decision_review_created_id",
        "review_decision",
        ["review_item_id", sa.column("created_at").desc(), sa.column("id").desc()],
        unique=False,
    )
    op.create_index(
        "ix_mcp_tool_call_workspace_session_started",
        "mcp_tool_call",
        ["workspace_id", "mcp_session_id", "started_at", "id"],
        unique=False,
    )


def _downgrade_release() -> None:
    # The full release must refuse credential/evidence/deadline loss before
    # dropping its first table or column, including unrelated graph data.
    _assert_downgrade_safe()
    op.drop_index(
        "ix_mcp_tool_call_workspace_session_started", table_name="mcp_tool_call"
    )
    op.drop_index("ix_review_decision_review_created_id", table_name="review_decision")
    document_reference_schema.downgrade()
    co_access_schema.downgrade()
    _downgrade_ledger()
    inbox_schema.downgrade()
    graph_schema.downgrade_columns()
    identity_schema.downgrade()


def _assert_downgrade_safe() -> None:
    identity_schema.assert_downgrade_safe()


def _upgrade_ledger() -> None:
    graph_schema.upgrade_ledger()


def _downgrade_ledger() -> None:
    graph_schema.downgrade_ledger()


def _run_release_atomically(operation) -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "5b7e5c149279 requires an online connection for data validation "
            "and SQLite batch-table operations"
        )
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        operation()
        return
    # Also preserve atomicity when invoked through an existing connection.
    # env.py owns the outer BEGIN IMMEDIATE for ordinary Alembic execution.
    connection.exec_driver_sql("UPDATE workspace SET slug = slug WHERE 1 = 0")
    with connection.begin_nested():
        operation()
