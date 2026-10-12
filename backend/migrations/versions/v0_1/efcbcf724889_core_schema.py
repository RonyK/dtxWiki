"""Consolidate the complete Epic-1 schema into one migration.

This revision upgrades the immutable ``bd62193d5cf6`` baseline
directly to the final Epic-1 model. Revision-owned domain modules live outside Alembic's
``versions`` directory so they can keep the large migration reviewable without
creating intermediate revision boundaries.

The base schema work includes:

1. Repair MCP chat artifact links that stored content chunk ids
   (Sprint-1 data repair).
2. Sprint-2 search projections: version-scoped page_links, page_name_index,
   edge_projection (+ page_name_index backfill).
3. G0 item 1: normalize AskTurn citations into ask_turn_citation.
4. G0 items 2+5: normalize proposal sources into source_ask_turn_id +
   proposal_source_item.
5. G0 item 3: normalize page provenance into page_provenance.
6. Sprint-3 M0: client_delegated_task queue skeleton.
7. Sprint-4 N3: replace duplicated search_doc columns with rebuildable
   SQLite FTS5 document and page-name projections.

The remaining work is grouped by final-schema ownership under
``revision_modules/v0_1/efcbcf724889_core_schema``: workspace, identity/access,
Context schema and data, library media and policy, tenancy, search, and operations.
Deployment-only convergence
revisions and transient create-then-alter column shapes are folded into those
domain owners.

Revision ID: efcbcf724889
Revises: bd62193d5cf6
Create Date: 2026-07-10
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine import Connection

from backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema import (
    context_data,
    context_schema,
    identity_access_schema,
    library_media_schema,
    library_policy,
    operations_schema,
    search_schema,
    tenancy_schema,
    workspace_schema,
)

revision = "efcbcf724889"
down_revision = "bd62193d5cf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade the 0001 baseline directly to the final Epic-1 schema."""

    _run_epic1_atomically(_upgrade_epic1)


def downgrade() -> None:
    """Return the consolidated Epic-1 schema to the 0001 baseline."""

    _run_epic1_atomically(_downgrade_epic1)


def _upgrade_epic1() -> None:
    _upgrade_base_schema()
    workspace_schema.upgrade()
    identity_access_schema.upgrade()
    context_schema.upgrade()
    library_media_schema.upgrade()
    library_policy.upgrade()
    context_data.upgrade()
    tenancy_schema.upgrade()
    search_schema.upgrade()
    context_schema.upgrade_context_editing()
    operations_schema.upgrade()


def _downgrade_epic1() -> None:
    operations_schema.downgrade()
    context_schema.downgrade_context_editing()
    search_schema.downgrade()
    tenancy_schema.prepare_downgrade_data_columns()
    context_schema.prepare_downgrade_data()
    library_media_schema.prepare_downgrade_data()
    _prepare_base_schema_downgrade_data()
    tenancy_schema.downgrade()
    context_data.downgrade()
    library_policy.downgrade()
    library_media_schema.downgrade()
    context_schema.downgrade()
    identity_access_schema.downgrade()
    workspace_schema.downgrade()
    _downgrade_base_schema()


def _run_epic1_atomically(operation) -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "efcbcf724889 requires an online connection for data validation "
            "and SQLite batch-table operations"
        )

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        operation()
        return

    # Alembic treats SQLite DDL as non-transactional and pysqlite defers BEGIN.
    # Force a real outer transaction, then keep the whole consolidated revision
    # in one savepoint so a late module failure returns to the 0001 boundary.
    connection.exec_driver_sql("UPDATE workspace SET slug = slug WHERE 1 = 0")
    with connection.begin_nested():
        operation()


# Migration-local table shapes (no runtime imports). Typed sa.JSON columns
# come back as parsed Python values on SQLite and Postgres.
content_chunk = sa.table(
    "content_chunk",
    sa.column("id", sa.Uuid()),
    sa.column("document_kind", sa.Text()),
    sa.column("document_id", sa.Uuid()),
)

mcp_chat_artifact_link = sa.table(
    "mcp_chat_artifact_link",
    sa.column("id", sa.Uuid()),
    sa.column("chat_session_id", sa.Uuid()),
    sa.column("resource_kind", sa.Text()),
    sa.column("resource_id", sa.Uuid()),
    sa.column("verb", sa.Text()),
    sa.column("first_occurred_at", sa.DateTime(timezone=True)),
    sa.column("last_occurred_at", sa.DateTime(timezone=True)),
    sa.column("occurrence_count", sa.Integer()),
    sa.column("last_mcp_tool_call_id", sa.Uuid()),
)

mcp_chat_tool_call_artifact = sa.table(
    "mcp_chat_tool_call_artifact",
    sa.column("id", sa.Uuid()),
    sa.column("mcp_tool_call_id", sa.Uuid()),
    sa.column("resource_kind", sa.Text()),
    sa.column("resource_id", sa.Uuid()),
    sa.column("verb", sa.Text()),
)

context_page = sa.table(
    "context_page",
    sa.column("id", sa.Uuid()),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("slug", sa.Text()),
    sa.column("title", sa.Text()),
    sa.column("aliases", sa.JSON()),
    sa.column("current_version_id", sa.Uuid()),
    sa.column("current_published_version_id", sa.Uuid()),
    sa.column("latest_draft_version_id", sa.Uuid()),
    sa.column("created_from_ask_turn_ids", sa.JSON()),
)

context_page_version = sa.table(
    "context_page_version",
    sa.column("id", sa.Uuid()),
)

page_name_index = sa.table(
    "page_name_index",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("workspace_id", sa.Uuid()),
    sa.column("page_id", sa.Uuid()),
    sa.column("source_version_id", sa.Uuid()),
    sa.column("visibility_scope", sa.Text()),
    sa.column("name_norm", sa.Text()),
    sa.column("name", sa.Text()),
    sa.column("name_kind", sa.Text()),
)

page_links = sa.table(
    "page_links",
    sa.column("id", sa.Uuid()),
    sa.column("source_page_id", sa.Uuid()),
    sa.column("source_version_id", sa.Uuid()),
    sa.column("target_slug", sa.Text()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)

ask_turn = sa.table(
    "ask_turn",
    sa.column("id", sa.Uuid()),
    sa.column("cited_context_page_ids", sa.JSON()),
    sa.column("cited_library_artifact_ids", sa.JSON()),
    sa.column("context_ingress_proposal_ids", sa.JSON()),
)

ask_turn_citation = sa.table(
    "ask_turn_citation",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("ask_turn_id", sa.Uuid()),
    sa.column("target_kind", sa.Text()),
    sa.column("target_id", sa.Uuid()),
    sa.column("rank", sa.Integer()),
)

proposal = sa.table(
    "context_ingress_proposal",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("source_kind", sa.Text()),
    sa.column("source_ask_turn_id", sa.Uuid()),
    sa.column("library_item_ids", sa.JSON()),
)

proposal_source_item = sa.table(
    "proposal_source_item",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("proposal_id", sa.Uuid()),
    sa.column("library_item_id", sa.Uuid()),
    sa.column("rank", sa.Integer()),
)

page_provenance = sa.table(
    "page_provenance",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("page_id", sa.Uuid()),
    sa.column("source_kind", sa.Text()),
    sa.column("source_id", sa.Uuid()),
)

_SEARCH_DOC_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_doc_fts USING fts5(
    context_header,
    body,
    workspace_id UNINDEXED,
    source_kind UNINDEXED,
    entity_id UNINDEXED,
    version_id UNINDEXED,
    visibility_scope UNINDEXED,
    category UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

_PAGE_NAME_TRGM_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS page_name_trgm USING fts5(
    name,
    workspace_id UNINDEXED,
    page_id UNINDEXED,
    source_version_id UNINDEXED,
    visibility_scope UNINDEXED,
    name_kind UNINDEXED,
    name_norm UNINDEXED,
    tokenize = 'trigram'
)
"""

_FTS5_PROBE_TABLE = "__dtxwiki_migration_fts5_probe"


def _require_sqlite_fts5(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    created = False
    try:
        connection.exec_driver_sql(
            f"CREATE VIRTUAL TABLE temp.{_FTS5_PROBE_TABLE} USING fts5(content)"
        )
        created = True
    except sa.exc.SQLAlchemyError as exc:
        raise RuntimeError(
            "SQLite FTS5 support is required before upgrading Epic-1 search"
        ) from exc
    finally:
        if created:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS temp.{_FTS5_PROBE_TABLE}")


def _upgrade_base_schema() -> None:
    bind = op.get_bind()

    # --- 1. Sprint-1 data repair: chunk-contaminated artifact links --------
    _repair_mcp_chunk_artifact_links(bind)

    # --- 2. Sprint-2 search projections -------------------------------------
    # page_links' version-scoped key is installed with its workspace-scoped
    # foreign keys in tenancy_schema's one final table batch.

    op.create_table(
        "page_name_index",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("source_version_id", sa.Uuid(), nullable=False),
        sa.Column("visibility_scope", sa.Text(), nullable=False),
        sa.Column("name_norm", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_kind", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "name_kind IN ('title', 'slug', 'alias')",
            name="page_name_index_name_kind_check",
        ),
        sa.CheckConstraint(
            "visibility_scope IN ('published', 'draft')",
            name="page_name_index_visibility_scope_check",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["context_page.id"],
            ondelete="CASCADE",
            name="fk_page_name_index_page_id_context_page",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"],
            ["context_page_version.id"],
            ondelete="CASCADE",
            name="fk_page_name_index_source_version_id_context_page_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_page_name_index_lookup",
        "page_name_index",
        ["workspace_id", "name_norm"],
        unique=False,
    )
    op.create_index(
        "idx_page_name_index_page",
        "page_name_index",
        ["page_id", "source_version_id"],
        unique=False,
    )

    op.create_table(
        "edge_projection",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("edge_kind", sa.Text(), nullable=False),
        sa.Column(
            "provenance",
            sa.Text(),
            sa.CheckConstraint(
                "provenance IN ('curated', 'derived', 'usage')",
                name="edge_projection_provenance_check",
            ),
            nullable=False,
        ),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_ref", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_kind",
            "source_id",
            "target_kind",
            "target_id",
            "edge_kind",
            name="uq_edge_projection_identity",
        ),
    )
    op.create_index(
        "idx_edge_projection_source",
        "edge_projection",
        ["workspace_id", "source_kind", "source_id"],
        unique=False,
    )
    op.create_index(
        "idx_edge_projection_target",
        "edge_projection",
        ["workspace_id", "target_kind", "target_id"],
        unique=False,
    )
    op.create_index(
        "idx_edge_projection_kind",
        "edge_projection",
        ["workspace_id", "edge_kind"],
        unique=False,
    )
    op.create_index(
        "idx_edge_projection_ppr_source",
        "edge_projection",
        [
            "workspace_id",
            "source_kind",
            "source_id",
            "provenance",
            "weight",
            "id",
        ],
        unique=False,
    )
    op.create_index(
        "idx_edge_projection_ppr_target",
        "edge_projection",
        [
            "workspace_id",
            "target_kind",
            "target_id",
            "provenance",
            "weight",
            "id",
        ],
        unique=False,
    )

    _backfill_page_name_index(bind)

    # --- 3. G0 item 1: ask_turn_citation ------------------------------------
    op.create_table(
        "ask_turn_citation",
        # Populated from ask_turn by tenancy_schema before becoming NOT NULL.
        # Declaring it here avoids creating this 0002-owned table and then
        # adding another 0002 column from a later module.
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("ask_turn_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('context_page', 'library_artifact')",
            name="ask_turn_citation_target_kind_check",
        ),
        sa.ForeignKeyConstraint(
            ["ask_turn_id"],
            ["ask_turn.id"],
            ondelete="CASCADE",
            name="fk_ask_turn_citation_ask_turn_id_ask_turn",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ask_turn_id", "target_kind", "target_id", name="ask_turn_citation_uq"
        ),
    )
    op.create_index(
        "idx_ask_turn_citation_target",
        "ask_turn_citation",
        ["target_kind", "target_id"],
        unique=False,
    )

    _backfill_citations(bind)

    # Same-migration drops throughout: no dual-encoding window (§19.5.7). The
    # `cited_*` columns are dropped in ask_turn's single rebuild below, once
    # _backfill_sources has also finished reading this table -- SQLite pays a
    # full table copy per batch, so ask_turn is visited exactly once.

    # --- 4. G0 items 2+5: proposal sources ----------------------------------
    op.add_column(
        "context_ingress_proposal",
        sa.Column("source_ask_turn_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "idx_context_ingress_source_ask_turn",
        "context_ingress_proposal",
        ["source_ask_turn_id"],
        unique=False,
    )
    op.create_table(
        "proposal_source_item",
        # Populated from context_ingress_proposal by tenancy_schema.
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("library_item_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["context_ingress_proposal.id"],
            ondelete="CASCADE",
            name="fk_proposal_source_item_proposal_id_context_ingress_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["library_item_id"],
            ["library_item.id"],
            ondelete="CASCADE",
            name="fk_proposal_source_item_library_item_id_library_item",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_id", "library_item_id", name="proposal_source_item_uq"
        ),
    )

    _backfill_sources(bind)

    # The drained legacy arrays stay in place until tenancy_schema folds their
    # removal, the source FK, and all final workspace constraints into each
    # table's one final batch.

    # --- 5. G0 item 3: page provenance --------------------------------------
    op.create_table(
        "page_provenance",
        # Populated from context_page by tenancy_schema.
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["context_page.id"],
            ondelete="CASCADE",
            name="fk_page_provenance_page_id_context_page",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "page_id", "source_kind", "source_id", name="page_provenance_uq"
        ),
    )
    op.create_index(
        "idx_page_provenance_source",
        "page_provenance",
        ["source_kind", "source_id"],
        unique=False,
    )

    _backfill_provenance(bind)

    # --- 7. Sprint-3 navigation substrate ------------------------------------
    op.add_column(
        "workspace_tree_node",
        sa.Column(
            "nav_summary",
            sa.Text(),
            sa.CheckConstraint(
                "nav_summary IS NULL OR node_kind = 'folder'",
                name="workspace_tree_node_nav_summary_folder_only",
            ),
            nullable=True,
        ),
    )
    _dedupe_visible_folder_slugs(bind)
    op.create_index(
        # NOTE: the partial unique cannot constrain parent_id IS NULL rows
        # (NULLs are distinct on SQLite and default Postgres), so section
        # roots rely on bootstrap-assigned distinct slugs; the dedupe below
        # still removes any accidental root-level slug collision.
        "workspace_tree_node_folder_slug_uniq",
        "workspace_tree_node",
        ["workspace_id", "parent_id", "slug"],
        unique=True,
        sqlite_where=sa.text("node_kind = 'folder' AND visibility_status = 'visible'"),
        postgresql_where=sa.text(
            "node_kind = 'folder' AND visibility_status = 'visible'"
        ),
    )

    # --- 8. Sprint-3 M0: client_delegated_task -------------------------------
    op.create_table(
        "client_delegated_task",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_ref", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'answered', 'staged',"
            " 'dismissed', 'expired')",
            name="client_delegated_task_status_check",
        ),
        sa.CheckConstraint(
            "source IN ('lint', 'coverage_gap', 'publish', 'import')",
            name="client_delegated_task_source_check",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_cdt_drain",
        "client_delegated_task",
        ["workspace_id", "status", "priority", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_cdt_recon",
        "client_delegated_task",
        ["workspace_id", "task_type", "target_kind", "target_id", "fingerprint"],
        unique=False,
    )

    # --- 9. Sprint-4 N3: FTS5 single indexing seam --------------------------
    _require_sqlite_fts5(bind)
    # Legacy search_doc columns are dropped in tenancy_schema's final batch
    # for each owning table, avoiding a separate SQLite rebuild here.
    if bind.dialect.name == "sqlite":
        op.execute(_SEARCH_DOC_FTS_DDL)
        op.execute(_PAGE_NAME_TRGM_DDL)

    # --- 10. Sprint-5: ask evidence pack + coverage gap ----------------------
    # The ask_turn evidence-pack columns (retrieval_trace, tried_lanes,
    # fallback_disclosure, warning_codes) are added by
    # tenancy_schema._add_feature_columns, so ask_turn is
    # rebuilt exactly once; only the coverage-gap table is created here.
    op.create_table(
        "knowledge_coverage_gap",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("query_norm", sa.Text(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("tried_lanes", sa.JSON(), nullable=False),
        sa.Column("nearest_candidates", sa.JSON(), nullable=False),
        sa.Column("rewrite_attempts", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "surface IN ('ask', 'search_rest', 'search_mcp')",
            name="knowledge_coverage_gap_surface_check",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'triaged', 'dismissed', 'resolved')",
            name="knowledge_coverage_gap_status_check",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_kcg_open",
        "knowledge_coverage_gap",
        ["workspace_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_kcg_recon",
        "knowledge_coverage_gap",
        ["workspace_id", "query_norm"],
        unique=False,
    )
    op.create_index(
        "uq_kcg_open_query",
        "knowledge_coverage_gap",
        ["workspace_id", "query_norm"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )

    # --- 11. Sprint-6: maintenance loop --------------------------------------
    with op.batch_alter_table("lint_finding") as batch_op:
        batch_op.add_column(sa.Column("message_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("message_args", sa.JSON(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE lint_run SET lint_scope = "
            "CASE lint_scope "
            "WHEN 'workspace' THEN '{\"kind\":\"workspace\"}' "
            "WHEN 'maintenance' THEN '{\"kind\":\"maintenance\"}' "
            "ELSE lint_scope END"
        )
    )
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "lint_run",
            "lint_scope",
            existing_type=sa.Text(),
            type_=sa.JSON(),
            existing_nullable=False,
            postgresql_using="lint_scope::json",
        )
        op.create_unique_constraint(
            "uq_lint_run_workspace_id",
            "lint_run",
            ["workspace_id", "id"],
        )
    else:
        with op.batch_alter_table("lint_run") as batch_op:
            batch_op.alter_column(
                "lint_scope",
                existing_type=sa.Text(),
                type_=sa.JSON(),
                existing_nullable=False,
            )
            batch_op.create_unique_constraint(
                "uq_lint_run_workspace_id",
                ["workspace_id", "id"],
            )
    # context_page.stale_candidate is added in step 5's single context_page
    # rebuild. Workspace projection and retrieval-policy columns are owned by
    # revision_modules/v0_1/efcbcf724889_core_schema.workspace_schema so that table is
    # not rebuilt here as well.
    op.create_table(
        "knowledge_node_stat",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("authority", sa.Float(), nullable=False),
        sa.Column("in_degree", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "kind",
            "entity_id",
            name="uq_knowledge_node_stat_entity",
        ),
    )
    op.create_index(
        "idx_knowledge_node_stat_authority",
        "knowledge_node_stat",
        ["workspace_id", "authority"],
        unique=False,
    )

    # --- 12. Epic-1 completion visibility snapshots -----------------------
    with op.batch_alter_table("library_item_tombstone") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trust_level_snapshot",
                sa.Text(),
                nullable=False,
                # Existing tombstones have no surviving source row to consult;
                # classify them conservatively until an administrator audits
                # or recreates the source.
                server_default="restricted",
            )
        )
    op.create_table(
        "upload_asset",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("marker_uuid", sa.Uuid(), nullable=False),
        sa.Column("page_id_hint", sa.Uuid(), nullable=True),
        sa.Column("created_by_actor_kind", sa.Text(), nullable=False),
        sa.Column("created_by_actor_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["page_id_hint"],
            ["context_page.id"],
            ondelete="SET NULL",
            name="fk_upload_asset_page_id_hint_context_page",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_upload_asset_workspace_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "marker_uuid",
            name="uq_upload_asset_workspace_marker",
        ),
    )
    op.create_index(
        "idx_upload_asset_workspace_filename",
        "upload_asset",
        ["workspace_id", "filename"],
        unique=False,
    )
    op.create_table(
        "upload_asset_binding",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("upload_asset_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("page_version_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["upload_asset_id"],
            ["upload_asset.id"],
            ondelete="CASCADE",
            name="fk_upload_asset_binding_upload_asset_id_upload_asset",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["context_page.id"],
            ondelete="CASCADE",
            name="fk_upload_asset_binding_page_id_context_page",
        ),
        sa.ForeignKeyConstraint(
            ["page_version_id"],
            ["context_page_version.id"],
            ondelete="CASCADE",
            name="fk_upload_asset_binding_page_version_id_context_page_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upload_asset_id",
            "page_version_id",
            name="uq_upload_asset_binding_version",
        ),
    )
    op.create_index(
        "idx_upload_asset_binding_version",
        "upload_asset_binding",
        ["page_version_id"],
        unique=False,
    )

    # --- 13-14. MCP chat final session shape -------------------------------
    # `error_count` sums failed, rejected_by_policy and conflict, so an auditor
    # cannot tell "the tool broke" from "workspace policy said no". Split the
    # two an operator acts on differently; `error_count` keeps its meaning.
    with op.batch_alter_table("mcp_chat_session") as batch_op:
        batch_op.add_column(
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "rejected_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(
            sa.Column("conversation_group_id", sa.Uuid(), nullable=True)
        )
    # Deliberately not backfilled. `mcp_tool_call.mcp_session_id` is a UUID
    # column and `mcp_chat_session.mcp_session_id` is the conversation token, so
    # the two only join through the same two-path lookup the service does in
    # Python (uuid match, else the per-call edge table). A SQL approximation
    # would silently undercount, and a chat reading "0 refused" when calls were
    # refused is worse than one reporting no breakdown at all. Existing rows
    # keep `error_count` and are rendered as a combined chip; the split fills in
    # as new calls land.

    # --- 15. Bound legacy Context page slugs -------------------------------
    _truncate_overlength_page_slugs(bind)

    op.create_index(
        "ix_mcp_chat_session_workspace_conversation_group",
        "mcp_chat_session",
        ["workspace_id", "conversation_group_id"],
        unique=False,
    )

    # --- 17. Retry-safe MCP writes ---------------------------------------
    op.create_table(
        "mcp_write_idempotency",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "actor_kind",
            sa.Text(),
            nullable=False,
            server_default="human",
        ),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "actor_kind",
            "actor_id",
            "tool_name",
            "idempotency_key",
            name="uq_mcp_write_idempotency_scope",
        ),
    )


def _prepare_base_schema_downgrade_data() -> None:
    """Fold normalized citation/proposal rows back into the 0001 JSON arrays."""

    connection = op.get_bind()
    items_by_proposal: dict[UUID, list[str]] = {}
    for proposal_id, library_item_id in connection.execute(
        sa.select(
            proposal_source_item.c.proposal_id,
            proposal_source_item.c.library_item_id,
        ).order_by(proposal_source_item.c.proposal_id, proposal_source_item.c.rank)
    ):
        items_by_proposal.setdefault(proposal_id, []).append(str(library_item_id))
    proposals_by_turn: dict[UUID, list[str]] = {}
    proposal_rows = (
        connection.execute(
            sa.select(proposal).order_by(proposal.c.created_at, proposal.c.id)
        )
        .mappings()
        .all()
    )
    for row in proposal_rows:
        if row["source_ask_turn_id"] is not None:
            proposals_by_turn.setdefault(row["source_ask_turn_id"], []).append(
                str(row["id"])
            )
        if row["source_kind"] == "ask_turn":
            legacy_items = (
                [str(row["source_ask_turn_id"])]
                if row["source_ask_turn_id"] is not None
                else []
            )
        else:
            legacy_items = items_by_proposal.get(row["id"], [])
        connection.execute(
            proposal.update()
            .where(proposal.c.id == row["id"])
            .values(library_item_ids=legacy_items)
        )
    for turn_id, proposal_ids in proposals_by_turn.items():
        connection.execute(
            ask_turn.update()
            .where(ask_turn.c.id == turn_id)
            .values(context_ingress_proposal_ids=proposal_ids)
        )
    connection.execute(
        ask_turn.update()
        .where(ask_turn.c.context_ingress_proposal_ids.is_(None))
        .values(context_ingress_proposal_ids=[])
    )
    connection.execute(
        proposal.update()
        .where(proposal.c.library_item_ids.is_(None))
        .values(library_item_ids=[])
    )

    rows = connection.execute(
        sa.select(
            ask_turn_citation.c.ask_turn_id,
            ask_turn_citation.c.target_kind,
            ask_turn_citation.c.target_id,
            ask_turn_citation.c.rank,
        ).order_by(ask_turn_citation.c.ask_turn_id, ask_turn_citation.c.rank)
    ).all()
    by_turn: dict[UUID, dict[str, list[str]]] = {}
    for turn_id, kind, target_id, _rank in rows:
        by_turn.setdefault(turn_id, {"context_page": [], "library_artifact": []})[
            kind
        ].append(str(target_id))
    for turn_id, kinds in by_turn.items():
        connection.execute(
            ask_turn.update()
            .where(ask_turn.c.id == turn_id)
            .values(
                cited_context_page_ids=kinds["context_page"],
                cited_library_artifact_ids=kinds["library_artifact"],
            )
        )
    connection.execute(
        ask_turn.update()
        .where(ask_turn.c.cited_context_page_ids.is_(None))
        .values(cited_context_page_ids=[])
    )
    connection.execute(
        ask_turn.update()
        .where(ask_turn.c.cited_library_artifact_ids.is_(None))
        .values(cited_library_artifact_ids=[])
    )

    # The 0001 key cannot accept duplicate targets from multiple live
    # versions. Resolve them before tenancy_schema restores that key.
    _dedupe_page_links_for_downgrade(connection)


def _downgrade_base_schema() -> None:
    connection = op.get_bind()

    # --- 17'. Retry-safe MCP writes --------------------------------------
    op.drop_table("mcp_write_idempotency")

    # --- 16'. Logical MCP conversation groups ----------------------------
    # Restored with mcp_chat_session's tenancy batch.

    # --- 15'. Bound legacy Context page slugs -----------------------------
    # Truncated source text cannot be reconstructed; retain the valid slugs.

    # --- 14'. MCP chat failure breakdown ----------------------------------
    # Restored with mcp_chat_session's tenancy batch.

    # --- 12'. Epic-1 completion visibility snapshots ----------------------
    op.drop_index(
        "idx_upload_asset_binding_version",
        table_name="upload_asset_binding",
    )
    op.drop_table("upload_asset_binding")
    op.drop_index(
        "idx_upload_asset_workspace_filename",
        table_name="upload_asset",
    )
    op.drop_table("upload_asset")
    with op.batch_alter_table("library_item_tombstone") as batch_op:
        batch_op.drop_column("trust_level_snapshot")

    # --- 11'. Sprint-6 --------------------------------------------------------
    op.drop_index("idx_knowledge_node_stat_authority", table_name="knowledge_node_stat")
    op.drop_table("knowledge_node_stat")
    # lint_finding's message columns were removed with its tenancy batch.
    if connection.dialect.name == "postgresql":
        op.drop_constraint(
            "uq_lint_run_workspace_id",
            "lint_run",
            type_="unique",
        )
        op.alter_column(
            "lint_run",
            "lint_scope",
            existing_type=sa.JSON(),
            type_=sa.Text(),
            existing_nullable=False,
            postgresql_using="lint_scope::text",
        )
    else:
        with op.batch_alter_table("lint_run") as batch_op:
            batch_op.drop_constraint(
                "uq_lint_run_workspace_id",
                type_="unique",
            )
            batch_op.alter_column(
                "lint_scope",
                existing_type=sa.JSON(),
                type_=sa.Text(),
                existing_nullable=False,
            )
    op.execute(
        sa.text(
            "UPDATE lint_run SET lint_scope = "
            "CASE lint_scope "
            "WHEN '{\"kind\": \"workspace\"}' THEN 'workspace' "
            "WHEN '{\"kind\":\"workspace\"}' THEN 'workspace' "
            "WHEN '{\"kind\": \"maintenance\"}' THEN 'maintenance' "
            "WHEN '{\"kind\":\"maintenance\"}' THEN 'maintenance' "
            "ELSE lint_scope END"
        )
    )

    # --- 10'. Sprint-5 --------------------------------------------------------
    op.drop_index("uq_kcg_open_query", table_name="knowledge_coverage_gap")
    op.drop_index("idx_kcg_recon", table_name="knowledge_coverage_gap")
    op.drop_index("idx_kcg_open", table_name="knowledge_coverage_gap")
    op.drop_table("knowledge_coverage_gap")
    # tenancy_schema already removed the evidence-pack columns while
    # restoring the three legacy citation/proposal arrays as nullable.

    # --- 9'. Sprint-4 N3 -----------------------------------------------------
    if connection.dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS page_name_trgm")
        op.execute("DROP TABLE IF EXISTS search_doc_fts")
    # tenancy_schema already restored the 0001 search_doc columns while
    # reverting each table's final constraints.

    # --- 8'. Sprint-3 M0 -------------------------------------------------------
    op.drop_index("idx_cdt_recon", table_name="client_delegated_task")
    op.drop_index("idx_cdt_drain", table_name="client_delegated_task")
    op.drop_table("client_delegated_task")

    # --- 7'. Sprint-3 navigation substrate ------------------------------------
    # nav_summary and its constraints were removed with the node's tenancy batch.
    # --- 5'. page provenance -------------------------------------------------
    # context_schema has already reconstructed created_from_ask_turn_ids.
    op.drop_index("idx_page_provenance_source", table_name="page_provenance")
    op.drop_table("page_provenance")

    # --- 4'. proposal sources -> library_item_ids dual encoding -------------
    op.drop_table("proposal_source_item")

    # --- 3'. ask_turn_citation -> cited_* arrays -----------------------------
    op.drop_index("idx_ask_turn_citation_target", table_name="ask_turn_citation")
    op.drop_table("ask_turn_citation")

    # --- 2'. search projections ----------------------------------------------
    op.drop_index("idx_edge_projection_ppr_target", table_name="edge_projection")
    op.drop_index("idx_edge_projection_ppr_source", table_name="edge_projection")
    op.drop_index("idx_edge_projection_kind", table_name="edge_projection")
    op.drop_index("idx_edge_projection_target", table_name="edge_projection")
    op.drop_index("idx_edge_projection_source", table_name="edge_projection")
    op.drop_table("edge_projection")
    op.drop_index("idx_page_name_index_page", table_name="page_name_index")
    op.drop_index("idx_page_name_index_lookup", table_name="page_name_index")
    op.drop_table("page_name_index")

    # page_links was deduplicated before its tenancy batch restored the 0001 key.

    # --- 1'. artifact link repair is data-only and irreversible --------------


# --- Sprint-1 data repair helpers -------------------------------------------


def _repair_mcp_chunk_artifact_links(connection: Connection) -> None:
    _repair_rollup_links(connection)
    _repair_per_call_edges(connection)


def _repair_rollup_links(connection: Connection) -> None:
    rows = connection.execute(
        sa.select(
            mcp_chat_artifact_link.c.id,
            mcp_chat_artifact_link.c.chat_session_id,
            mcp_chat_artifact_link.c.resource_kind,
            mcp_chat_artifact_link.c.verb,
            mcp_chat_artifact_link.c.first_occurred_at,
            mcp_chat_artifact_link.c.last_occurred_at,
            mcp_chat_artifact_link.c.occurrence_count,
            mcp_chat_artifact_link.c.last_mcp_tool_call_id,
            content_chunk.c.document_id,
        )
        .select_from(
            mcp_chat_artifact_link.join(
                content_chunk,
                mcp_chat_artifact_link.c.resource_id == content_chunk.c.id,
            )
        )
        .where(mcp_chat_artifact_link.c.resource_kind == content_chunk.c.document_kind)
        .where(
            mcp_chat_artifact_link.c.resource_kind.in_(("context_page", "library_item"))
        )
    ).mappings()

    for row in list(rows):
        existing = (
            connection.execute(
                sa.select(mcp_chat_artifact_link).where(
                    mcp_chat_artifact_link.c.chat_session_id == row["chat_session_id"],
                    mcp_chat_artifact_link.c.resource_kind == row["resource_kind"],
                    mcp_chat_artifact_link.c.resource_id == row["document_id"],
                    mcp_chat_artifact_link.c.verb == row["verb"],
                    mcp_chat_artifact_link.c.id != row["id"],
                )
            )
            .mappings()
            .first()
        )
        if existing is None:
            connection.execute(
                mcp_chat_artifact_link.update()
                .where(mcp_chat_artifact_link.c.id == row["id"])
                .values(resource_id=row["document_id"])
            )
            continue

        first_occurred_at = min(
            existing["first_occurred_at"],
            row["first_occurred_at"],
        )
        if row["last_occurred_at"] > existing["last_occurred_at"]:
            last_occurred_at = row["last_occurred_at"]
            last_mcp_tool_call_id = row["last_mcp_tool_call_id"]
        else:
            last_occurred_at = existing["last_occurred_at"]
            last_mcp_tool_call_id = existing["last_mcp_tool_call_id"]

        connection.execute(
            mcp_chat_artifact_link.update()
            .where(mcp_chat_artifact_link.c.id == existing["id"])
            .values(
                first_occurred_at=first_occurred_at,
                last_occurred_at=last_occurred_at,
                occurrence_count=(
                    existing["occurrence_count"] + row["occurrence_count"]
                ),
                last_mcp_tool_call_id=last_mcp_tool_call_id,
            )
        )
        connection.execute(
            mcp_chat_artifact_link.delete().where(
                mcp_chat_artifact_link.c.id == row["id"]
            )
        )


def _repair_per_call_edges(connection: Connection) -> None:
    rows = connection.execute(
        sa.select(
            mcp_chat_tool_call_artifact.c.id,
            mcp_chat_tool_call_artifact.c.mcp_tool_call_id,
            mcp_chat_tool_call_artifact.c.resource_kind,
            mcp_chat_tool_call_artifact.c.verb,
            content_chunk.c.document_id,
        )
        .select_from(
            mcp_chat_tool_call_artifact.join(
                content_chunk,
                mcp_chat_tool_call_artifact.c.resource_id == content_chunk.c.id,
            )
        )
        .where(
            mcp_chat_tool_call_artifact.c.resource_kind == content_chunk.c.document_kind
        )
        .where(
            mcp_chat_tool_call_artifact.c.resource_kind.in_(
                ("context_page", "library_item")
            )
        )
    ).mappings()

    for row in list(rows):
        existing = connection.execute(
            sa.select(mcp_chat_tool_call_artifact.c.id).where(
                mcp_chat_tool_call_artifact.c.mcp_tool_call_id
                == row["mcp_tool_call_id"],
                mcp_chat_tool_call_artifact.c.resource_kind == row["resource_kind"],
                mcp_chat_tool_call_artifact.c.resource_id == row["document_id"],
                mcp_chat_tool_call_artifact.c.verb == row["verb"],
                mcp_chat_tool_call_artifact.c.id != row["id"],
            )
        ).first()
        if existing is None:
            connection.execute(
                mcp_chat_tool_call_artifact.update()
                .where(mcp_chat_tool_call_artifact.c.id == row["id"])
                .values(resource_id=row["document_id"])
            )
            continue

        connection.execute(
            mcp_chat_tool_call_artifact.delete().where(
                mcp_chat_tool_call_artifact.c.id == row["id"]
            )
        )


# --- Sprint-2 search projection helpers --------------------------------------


def _normalize_entity_name(value: str | None) -> str:
    # Mirrors backend.services.entity_ref.normalize_entity_name; copied so the
    # migration never imports runtime code.
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _backfill_page_name_index(connection: Connection) -> None:
    """Project existing pages' live published/draft heads into the new table.

    Without this, upgraded installations lose exact title/slug/alias
    resolution until a page write or a manual ``dtxwiki reindex``. Name
    sources mirror the runtime projection: canonical ContextPage fields.
    """

    valid_version_ids = set(
        connection.execute(sa.select(context_page_version.c.id)).scalars()
    )
    # Explicit columns: the stub also lists created_from_ask_turn_ids, which
    # step 5 drops — this helper must keep working on the post-drop schema
    # (it is exercised directly against head-state databases in tests).
    pages = (
        connection.execute(
            sa.select(
                context_page.c.id,
                context_page.c.workspace_id,
                context_page.c.slug,
                context_page.c.title,
                context_page.c.aliases,
                context_page.c.current_version_id,
                context_page.c.current_published_version_id,
                context_page.c.latest_draft_version_id,
            )
        )
        .mappings()
        .all()
    )
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for page in pages:
        published_id = page["current_published_version_id"]
        live: list[tuple[object, str]] = []
        if published_id is not None:
            live.append((published_id, "published"))
        draft_id = page["latest_draft_version_id"]
        if draft_id is None and page["current_version_id"] != published_id:
            draft_id = page["current_version_id"]
        if draft_id is not None and draft_id != published_id:
            live.append((draft_id, "draft"))

        candidates: list[tuple[str, str]] = [
            (page["slug"], "slug"),
            (page["title"], "title"),
            *[(alias, "alias") for alias in page["aliases"] or []],
        ]
        seen: set[tuple[str, str]] = set()
        names: list[tuple[str, str, str]] = []
        for name, kind in candidates:
            norm = _normalize_entity_name(name)
            if not norm or (norm, kind) in seen:
                continue
            seen.add((norm, kind))
            names.append((name.strip(), norm, kind))

        for version_id, scope in live:
            if version_id not in valid_version_ids:
                continue
            for name, norm, kind in names:
                rows.append(
                    {
                        "id": uuid4(),
                        "created_at": now,
                        "workspace_id": page["workspace_id"],
                        "page_id": page["id"],
                        "source_version_id": version_id,
                        "visibility_scope": scope,
                        "name_norm": norm,
                        "name": name,
                        "name_kind": kind,
                    }
                )
    if rows:
        connection.execute(page_name_index.insert(), rows)
    inserted = connection.execute(
        sa.select(sa.func.count()).select_from(page_name_index)
    ).scalar_one()
    if inserted != len(rows):
        raise RuntimeError(
            "page_name_index backfill mismatch: "
            f"expected {len(rows)} rows, found {inserted}"
        )


def _dedupe_page_links_for_downgrade(connection: Connection) -> None:
    published_by_page = {
        row["id"]: row["current_published_version_id"]
        for row in connection.execute(
            sa.select(
                context_page.c.id,
                context_page.c.current_published_version_id,
            )
        ).mappings()
    }
    rows = connection.execute(sa.select(page_links)).mappings().all()

    def rank(row) -> tuple[int, object, str]:
        is_published = row["source_version_id"] == published_by_page.get(
            row["source_page_id"]
        )
        return (1 if is_published else 0, row["created_at"], str(row["id"]))

    keep: dict[tuple[object, str], dict] = {}
    for row in rows:
        key = (row["source_page_id"], row["target_slug"])
        current = keep.get(key)
        if current is None or rank(row) > rank(current):
            keep[key] = row
    keep_ids = {row["id"] for row in keep.values()}
    drop_ids = [row["id"] for row in rows if row["id"] not in keep_ids]
    if drop_ids:
        connection.execute(page_links.delete().where(page_links.c.id.in_(drop_ids)))


# --- G0 normalization helpers -------------------------------------------------


def _backfill_citations(connection: Connection) -> None:
    turns = (
        connection.execute(
            sa.select(
                ask_turn.c.id,
                ask_turn.c.cited_context_page_ids,
                ask_turn.c.cited_library_artifact_ids,
            )
        )
        .mappings()
        .all()
    )
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for turn in turns:
        for kind, raw_ids in (
            ("context_page", turn["cited_context_page_ids"]),
            ("library_artifact", turn["cited_library_artifact_ids"]),
        ):
            seen: set[str] = set()
            rank = 0
            for raw_id in raw_ids or []:
                value = str(raw_id)
                if value in seen:
                    continue
                seen.add(value)
                try:
                    target_id = UUID(value)
                except ValueError:
                    raise RuntimeError(
                        f"ask_turn {turn['id']} has a non-UUID citation: {value!r}"
                    ) from None
                rows.append(
                    {
                        "id": uuid4(),
                        "created_at": now,
                        "ask_turn_id": turn["id"],
                        "target_kind": kind,
                        "target_id": target_id,
                        "rank": rank,
                    }
                )
                rank += 1
    if rows:
        connection.execute(ask_turn_citation.insert(), rows)
    inserted = connection.execute(
        sa.select(sa.func.count()).select_from(ask_turn_citation)
    ).scalar_one()
    if inserted != len(rows):
        raise RuntimeError(
            f"ask_turn_citation backfill mismatch: expected {len(rows)}, "
            f"found {inserted}"
        )


def _backfill_sources(connection: Connection) -> None:
    reverse_by_turn: dict[str, set[str]] = {}
    for turn_id, proposal_ids in connection.execute(
        sa.select(ask_turn.c.id, ask_turn.c.context_ingress_proposal_ids)
    ):
        reverse_by_turn[str(turn_id)] = {str(pid) for pid in proposal_ids or []}

    now = datetime.now(UTC)
    item_rows: list[dict[str, object]] = []
    for row in connection.execute(sa.select(proposal)).mappings().all():
        raw_ids = [str(value) for value in row["library_item_ids"] or []]
        if row["source_kind"] == "ask_turn":
            if not raw_ids:
                continue
            try:
                turn_id = UUID(raw_ids[0])
            except ValueError:
                raise RuntimeError(
                    f"proposal {row['id']} has a non-UUID source turn ref:"
                    f" {raw_ids[0]!r}"
                ) from None
            # Cross-verify the legacy dual encoding before dropping it: the
            # source turn must point back at this proposal.
            reverse = reverse_by_turn.get(str(turn_id))
            if reverse is None or str(row["id"]) not in reverse:
                raise RuntimeError(
                    f"proposal {row['id']} source turn {turn_id} does not"
                    " reference it in context_ingress_proposal_ids"
                )
            connection.execute(
                proposal.update()
                .where(proposal.c.id == row["id"])
                .values(source_ask_turn_id=turn_id)
            )
            continue
        seen: set[str] = set()
        rank = 0
        for value in raw_ids:
            if value in seen:
                continue
            seen.add(value)
            try:
                library_item_id = UUID(value)
            except ValueError:
                raise RuntimeError(
                    f"proposal {row['id']} has a non-UUID library item id: {value!r}"
                ) from None
            item_rows.append(
                {
                    "id": uuid4(),
                    "created_at": now,
                    "proposal_id": row["id"],
                    "library_item_id": library_item_id,
                    "rank": rank,
                }
            )
            rank += 1
    if item_rows:
        connection.execute(proposal_source_item.insert(), item_rows)
    inserted = connection.execute(
        sa.select(sa.func.count()).select_from(proposal_source_item)
    ).scalar_one()
    if inserted != len(item_rows):
        raise RuntimeError(
            f"proposal_source_item backfill mismatch: expected {len(item_rows)},"
            f" found {inserted}"
        )


def _dedupe_visible_folder_slugs(connection: Connection) -> None:
    """Pre-Sprint-3 tree moves could leave duplicate visible sibling folder
    slugs; rename all but the first (sort-key order) so the partial unique
    index builds instead of aborting the deploy on legacy data.

    Two passes: pass 1 collects every slug currently held per sibling group,
    so a rename's suffix probe checks against ALL originals (not just those
    seen so far) and can never steal a slug a not-yet-scanned sibling owns.
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, workspace_id, parent_id, slug FROM workspace_tree_node"
            " WHERE node_kind = 'folder' AND visibility_status = 'visible'"
            " AND slug IS NOT NULL"
            " ORDER BY workspace_id, parent_id, slug, sort_key, id"
        )
    ).all()
    # pass 1: all original slugs per (workspace, parent) group
    taken: dict[tuple[object, object], set[str]] = {}
    for _node_id, workspace_id, parent_id, slug in rows:
        taken.setdefault((workspace_id, parent_id), set()).add(slug)
    # pass 2: first occurrence keeps its slug; later duplicates take the
    # lowest free suffix that collides with no original and no prior rename.
    claimed: dict[tuple[object, object], set[str]] = {}
    for node_id, workspace_id, parent_id, slug in rows:
        group = (workspace_id, parent_id)
        seen = claimed.setdefault(group, set())
        if slug not in seen:
            seen.add(slug)
            continue
        suffix = 2
        while f"{slug}-{suffix}" in taken[group]:
            suffix += 1
        new_slug = f"{slug}-{suffix}"
        seen.add(new_slug)
        taken[group].add(new_slug)
        connection.execute(
            sa.text("UPDATE workspace_tree_node SET slug = :slug WHERE id = :id"),
            {"slug": new_slug, "id": node_id},
        )


def _truncate_overlength_page_slugs(connection: Connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT id, workspace_id, slug FROM context_page"
            " ORDER BY workspace_id, slug, id"
        )
    ).all()
    taken: dict[object, set[str]] = {}
    for _page_id, workspace_id, slug in rows:
        if len(slug) <= 64:
            taken.setdefault(workspace_id, set()).add(slug)

    for page_id, workspace_id, slug in rows:
        if len(slug) <= 64:
            continue
        workspace_slugs = taken.setdefault(workspace_id, set())
        base_slug = unicodedata.normalize("NFKC", slug)[:64].rstrip("-")
        base_slug = base_slug or "context-page"
        candidate = base_slug
        suffix = 2
        while candidate in workspace_slugs:
            suffix_text = f"-{suffix}"
            candidate = f"{base_slug[: 64 - len(suffix_text)].rstrip('-')}{suffix_text}"
            suffix += 1
        workspace_slugs.add(candidate)
        connection.execute(
            sa.text("UPDATE context_page SET slug = :slug WHERE id = :id"),
            {"slug": candidate, "id": page_id},
        )
        connection.execute(
            sa.text(
                "UPDATE workspace_tree_node SET slug = :slug"
                " WHERE target_kind = 'context_page' AND target_id = :page_id"
            ),
            {"slug": candidate, "page_id": page_id},
        )


def _backfill_provenance(connection: Connection) -> None:
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for page_id, raw_ids in connection.execute(
        sa.select(context_page.c.id, context_page.c.created_from_ask_turn_ids)
    ).all():
        seen: set[str] = set()
        for raw_id in raw_ids or []:
            value = str(raw_id)
            if value in seen:
                continue
            seen.add(value)
            try:
                source_id = UUID(value)
            except ValueError:
                raise RuntimeError(
                    f"context_page {page_id} has a non-UUID provenance ref: {value!r}"
                ) from None
            rows.append(
                {
                    "id": uuid4(),
                    "created_at": now,
                    "page_id": page_id,
                    "source_kind": "ask_turn",
                    "source_id": source_id,
                }
            )
    if rows:
        connection.execute(page_provenance.insert(), rows)
    inserted = connection.execute(
        sa.select(sa.func.count()).select_from(page_provenance)
    ).scalar_one()
    if inserted != len(rows):
        raise RuntimeError(
            f"page_provenance backfill mismatch: expected {len(rows)}, found {inserted}"
        )
