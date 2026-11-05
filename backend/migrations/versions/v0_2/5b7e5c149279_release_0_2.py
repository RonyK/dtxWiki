"""Upgrade the published 0.1.4 schema to the v0.2 release schema.

The parent is the published 0.1.4 boundary eb86fdacb1bb. Its external login,
account retention, personal-pointer removal and review index already exist,
so only the remaining v0.2 DDL runs here, and downgrade returns to exactly
that release. The graph column operations below are revision-owned copies
without 0.1.4's account-deletion pointer/index, which must survive downgrade.

The 0.2.1 package shipped this revision ID over a development-only parent that
no release ever recorded. Databases already at this ID have the same schema;
the development parent is not distributed.

Revision ID: 5b7e5c149279
Revises: eb86fdacb1bb
"""

import sqlalchemy as sa
from alembic import op

from backend.migrations.revision_modules.v0_2._5b7e5c149279_release_0_2 import (
    co_access_schema,
    document_reference_schema,
    graph_schema,
    inbox_schema,
)

revision = "5b7e5c149279"
down_revision = "eb86fdacb1bb"
branch_labels = None
depends_on = None


def _assert_published_parent() -> None:
    """Do not skip 0.1.4 operations on a DB carrying only its stamp."""
    inspector = sa.inspect(op.get_bind())
    required = {
        "users": {
            "pending_email",
            "deletion_requested_at",
            "purge_after",
            "deletion_retention_days",
            "deletion_prior_status",
            "deletion_failure_count",
            "deletion_last_error",
        },
        "workspace": {"account_deletion_user_id"},
        "user_identities": {"user_id", "provider_slug", "issuer", "subject"},
        "auth_login_transactions": {"id", "provider_slug"},
        "email_reauthentication_challenges": {"id", "user_id"},
        "auth_email_conflict_requests": {"id", "target_user_id"},
        "email_outbox": {"reauthentication_challenge_id"},
    }
    present = set(inspector.get_table_names())
    for table, expected in required.items():
        if table not in present or not expected <= {
            column["name"] for column in inspector.get_columns(table)
        }:
            raise RuntimeError("published 0.1.4 stamp/schema conflict")
    users = {column["name"]: column for column in inspector.get_columns("users")}
    if "personal_workspace_id" in users or not all(
        users[name]["nullable"] for name in ("email", "password_hash")
    ):
        raise RuntimeError("published 0.1.4 stamp/schema conflict")
    for table, index in (
        ("change_operation", "ix_change_operation_change_set_id"),
        ("review_decision", "ix_review_decision_review_created_id"),
        ("workspace", "ix_workspace_account_deletion_user_id"),
        ("users", "ix_users_purge_after"),
    ):
        if index not in {item["name"] for item in inspector.get_indexes(table)}:
            raise RuntimeError("published 0.1.4 stamp/schema conflict")


def upgrade() -> None:
    # env.py owns the transaction, including Alembic's version update.
    _assert_published_parent()
    _upgrade_columns()
    inbox_schema.upgrade()
    graph_schema.upgrade_ledger()
    co_access_schema.upgrade()
    document_reference_schema.upgrade()
    op.create_index(
        "ix_mcp_tool_call_workspace_session_started",
        "mcp_tool_call",
        ["workspace_id", "mcp_session_id", "started_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_tool_call_workspace_session_started", table_name="mcp_tool_call"
    )
    document_reference_schema.downgrade()
    co_access_schema.downgrade()
    graph_schema.downgrade_ledger()
    inbox_schema.downgrade()
    _downgrade_columns()


def _upgrade_columns() -> None:
    for column in (
        sa.Column(
            "graph_observation_sequence",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "workspace_graph_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "usage_relation_signals_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    ):
        op.add_column("workspace", column)
    op.add_column("edge_projection", sa.Column("support_session_count", sa.Integer()))
    op.add_column(
        "edge_projection", sa.Column("first_observed_at", sa.DateTime(timezone=True))
    )

    # The released parent has only slug-authored wikilinks. Populate its new
    # identity before one rebuild installs the final locator and syntax shape.
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
        batch.add_column(
            sa.Column(
                "link_syntax", sa.Text(), nullable=False, server_default="wikilink"
            )
        )
        batch.create_check_constraint(
            "page_links_link_syntax_check", "link_syntax IN ('wikilink', 'context_url')"
        )


def _downgrade_columns() -> None:
    # A pre-v0.2 runtime cannot represent non-wikilink projections. Their
    # source bodies survive; a later reindex can rebuild these derived rows.
    op.execute(
        sa.text(
            "DELETE FROM page_links WHERE locator_key NOT LIKE 'wikilink:%' "
            "OR target_slug IS NULL"
        )
    )
    with op.batch_alter_table("page_links") as batch:
        batch.drop_constraint("page_links_link_syntax_check", type_="check")
        batch.drop_constraint("uq_page_links_source_version_locator", type_="unique")
        batch.alter_column("target_slug", existing_type=sa.Text(), nullable=False)
        batch.drop_column("link_syntax")
        batch.drop_column("locator_key")
        batch.create_unique_constraint(
            "uq_page_links_source_version_target",
            ["source_page_id", "source_version_id", "target_slug"],
        )
    with op.batch_alter_table("edge_projection") as batch:
        batch.drop_column("first_observed_at")
        batch.drop_column("support_session_count")
    with op.batch_alter_table("workspace") as batch:
        batch.drop_column("usage_relation_signals_enabled")
        batch.drop_column("workspace_graph_enabled")
        batch.drop_column("graph_observation_sequence")
