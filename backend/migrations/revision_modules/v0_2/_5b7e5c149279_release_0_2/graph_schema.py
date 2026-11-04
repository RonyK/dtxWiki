"""Final v0.2 graph ledger and single-pass authored-link schema changes."""

import sqlalchemy as sa
from alembic import op


def upgrade_columns() -> None:
    for column in (
        sa.Column("account_deletion_user_id", sa.Uuid(as_uuid=True)),
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
    op.create_index(
        "ix_workspace_account_deletion_user_id",
        "workspace",
        ["account_deletion_user_id"],
        unique=False,
    )
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


def downgrade_columns() -> None:
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
    op.drop_index("ix_workspace_account_deletion_user_id", table_name="workspace")
    with op.batch_alter_table("workspace") as batch:
        batch.drop_column("usage_relation_signals_enabled")
        batch.drop_column("workspace_graph_enabled")
        batch.drop_column("graph_observation_sequence")
        batch.drop_column("account_deletion_user_id")


def upgrade_ledger() -> None:
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


def downgrade_ledger() -> None:
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
