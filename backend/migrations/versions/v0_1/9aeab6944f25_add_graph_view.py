"""Add Graph View settings, relation projections and temporal evidence.

Revision ID: 9aeab6944f25
Revises: efcbcf724889
Create Date: 2026-10-14

The released core schema is immutable. Existing links remain wikilinks; the
ledger starts empty because current projections cannot prove past events.
Downgrade discards Graph View evidence and URL-derived link projections while
preserving the released workspace, edge and wikilink data.
"""

import sqlalchemy as sa
from alembic import op

revision = "9aeab6944f25"
down_revision = "efcbcf724889"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column(
            "graph_observation_sequence",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "workspace",
        sa.Column(
            "workspace_graph_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "workspace",
        sa.Column(
            "usage_relation_signals_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "edge_projection",
        sa.Column("support_session_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "edge_projection",
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    with op.batch_alter_table("page_links") as batch_op:
        batch_op.add_column(
            sa.Column(
                "link_syntax", sa.Text(), nullable=False, server_default="wikilink"
            )
        )
        batch_op.drop_constraint("uq_page_links_source_version_target", type_="unique")
        batch_op.create_unique_constraint(
            "uq_page_links_source_version_target",
            ["source_page_id", "source_version_id", "link_syntax", "target_slug"],
        )
        batch_op.create_check_constraint(
            "page_links_link_syntax_check", "link_syntax IN ('wikilink', 'context_url')"
        )
    _upgrade_ledger()


def downgrade() -> None:
    _downgrade_ledger()
    # URL links are a rebuildable Graph View projection unsupported by core.
    # Remove them before restoring the old unique key so a URL row can never
    # replace a pre-existing wikilink for the same source/version/target.
    op.execute(sa.text("DELETE FROM page_links WHERE link_syntax = 'context_url'"))
    with op.batch_alter_table("page_links") as batch_op:
        batch_op.drop_constraint("page_links_link_syntax_check", type_="check")
        batch_op.drop_constraint("uq_page_links_source_version_target", type_="unique")
        batch_op.create_unique_constraint(
            "uq_page_links_source_version_target",
            ["source_page_id", "source_version_id", "target_slug"],
        )
        batch_op.drop_column("link_syntax")
    with op.batch_alter_table("edge_projection") as batch_op:
        batch_op.drop_column("first_observed_at")
        batch_op.drop_column("support_session_count")
    with op.batch_alter_table("workspace") as batch_op:
        batch_op.drop_column("usage_relation_signals_enabled")
        batch_op.drop_column("workspace_graph_enabled")
        batch_op.drop_column("graph_observation_sequence")


def _upgrade_ledger() -> None:
    op.create_table(
        "graph_observation",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("workspace_sequence", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("edge_kind", sa.Text(), nullable=True),
        sa.Column("action_kind", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_kind", sa.Text(), nullable=True),
        sa.Column("evidence_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column("source_version_id", sa.Uuid(), nullable=True),
        sa.Column("weight_delta", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column(
            "extractor_version",
            sa.Text(),
            nullable=False,
            server_default="graph-observation.v1",
        ),
        sa.Column(
            "retention_class",
            sa.Text(),
            nullable=False,
            server_default="standard",
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "operation IN ('node_created', 'edge_created', 'edge_removed', "
            "'action_occurred')",
            name="graph_observation_operation_check",
        ),
        sa.CheckConstraint(
            "retention_class IN ('standard', 'sensitive', 'legal_hold')",
            name="graph_observation_retention_class_check",
        ),
        sa.CheckConstraint(
            "(target_kind IS NULL AND target_id IS NULL) OR "
            "(target_kind IS NOT NULL AND target_id IS NOT NULL)",
            name="graph_observation_target_pair_check",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_graph_observation_idempotency",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "workspace_sequence",
            name="uq_graph_observation_workspace_sequence",
        ),
    )
    op.create_index(
        "idx_graph_observation_ws_time",
        "graph_observation",
        ["workspace_id", "occurred_at", "id"],
        unique=False,
    )
    op.create_index(
        "idx_graph_observation_source_time",
        "graph_observation",
        ["workspace_id", "source_kind", "source_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "idx_graph_observation_target_time",
        "graph_observation",
        ["workspace_id", "target_kind", "target_id", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "graph_history_coverage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("stream_kind", sa.Text(), nullable=False),
        sa.Column("stream_id", sa.Uuid(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("coverage_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "basis IN ('domain_events', 'observations', 'mixed')",
            name="graph_history_coverage_basis_check",
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'partial', 'reconstructed', 'unsupported')",
            name="graph_history_coverage_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "stream_kind",
            "stream_id",
            name="uq_graph_history_coverage_stream",
        ),
    )
    op.create_index(
        "idx_graph_history_coverage_workspace",
        "graph_history_coverage",
        ["workspace_id", "status"],
        unique=False,
    )
    _create_append_only_guards()
    op.create_index(
        "idx_page_links_graph_head",
        "page_links",
        [
            "workspace_id",
            "source_page_id",
            "source_version_id",
            "target_page_id",
            "occurrence_count",
            "created_at",
        ],
        unique=False,
    )
    op.create_index(
        "idx_context_citation_graph_head",
        "context_citation",
        [
            "workspace_id",
            "target_kind",
            "target_id",
            "cited_context_page_version_id",
            "library_item_version_id",
            "library_artifact_id",
            "created_at",
        ],
        unique=False,
    )
    # Created after tenancy_schema rebuilds mcp_chat_tool_call_artifact, like
    # the relation-read indexes above: the co-read source reads a workspace's
    # 30-day window of per-call reads.
    op.create_index(
        "ix_mcp_chat_tool_call_artifact_workspace_time",
        "mcp_chat_tool_call_artifact",
        ["workspace_id", "occurred_at"],
        unique=False,
    )


def _downgrade_ledger() -> None:
    op.drop_index(
        "ix_mcp_chat_tool_call_artifact_workspace_time",
        table_name="mcp_chat_tool_call_artifact",
    )
    op.drop_index(
        "idx_context_citation_graph_head",
        table_name="context_citation",
    )
    op.drop_index(
        "idx_page_links_graph_head",
        table_name="page_links",
    )
    _drop_append_only_guards()
    op.drop_index(
        "idx_graph_history_coverage_workspace",
        table_name="graph_history_coverage",
    )
    op.drop_table("graph_history_coverage")
    op.drop_index(
        "idx_graph_observation_target_time",
        table_name="graph_observation",
    )
    op.drop_index(
        "idx_graph_observation_source_time",
        table_name="graph_observation",
    )
    op.drop_index("idx_graph_observation_ws_time", table_name="graph_observation")
    op.drop_table("graph_observation")


def _create_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER graph_observation_no_update "
            "BEFORE UPDATE ON graph_observation BEGIN "
            "SELECT RAISE(ABORT, 'graph_observation is append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER graph_observation_no_delete "
            "BEFORE DELETE ON graph_observation "
            "WHEN EXISTS (SELECT 1 FROM workspace WHERE id = OLD.workspace_id) "
            "BEGIN "
            "SELECT RAISE(ABORT, 'graph_observation is append-only'); END"
        )
    elif dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION dtxwiki_reject_graph_observation_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF TG_OP = 'DELETE' AND NOT EXISTS "
            "(SELECT 1 FROM workspace WHERE id = OLD.workspace_id) THEN "
            "RETURN OLD; END IF; "
            "RAISE EXCEPTION 'graph_observation is append-only'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER graph_observation_no_mutation "
            "BEFORE UPDATE OR DELETE ON graph_observation FOR EACH ROW "
            "EXECUTE FUNCTION dtxwiki_reject_graph_observation_mutation()"
        )


def _drop_append_only_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS graph_observation_no_update")
        op.execute("DROP TRIGGER IF EXISTS graph_observation_no_delete")
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS graph_observation_no_mutation ON graph_observation"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS dtxwiki_reject_graph_observation_mutation()"
        )
