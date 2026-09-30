"""Verify the final Epic-1 phase backfill for the second ownership-chain family.

Same pattern as test_workspace_tenancy_columns_migration.py: a real
Alembic Config creates the 0001 baseline against a temp-file SQLite database,
then the Epic-1 phase helpers build the pre-final boundary. Rows are seeded via
Core tables reflected at that boundary, not through current-head ORM models
that already carry the columns the final phase adds.

Two independent workspaces are seeded (at least for the simplest tables) so
a backfill that cross-wires parent and child shows up as a wrong-but-present
workspace_id, not merely a missing one.
"""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

ACTOR_ID = "00000000-0000-0000-0000-000000000010"

epic1 = import_module("backend.migrations.versions.0002_epic1")
workspace_phase = import_module(
    "backend.migrations.revision_0002.workspace_schema"
)
mcp_phase = import_module("backend.migrations.revision_0002.identity_access_schema")
media_phase = import_module("backend.migrations.revision_0002.library_media_schema")
public_phase = import_module(
    "backend.migrations.revision_0002.context_schema"
)
library_phase = import_module(
    "backend.migrations.revision_0002.library_policy"
)
content_hash_phase = import_module(
    "backend.migrations.revision_0002.context_data"
)
final_phase = import_module("backend.migrations.revision_0002.tenancy_schema")


def _config(database_url: str) -> Config:
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{tmp_path / name}"


# Present at the pre-final boundary (0001 plus 0002's base schema).
ASK_TURN_0001_ARRAY_COLUMNS = (
    "cited_context_page_ids",
    "cited_library_artifact_ids",
    "context_ingress_proposal_ids",
)
# Added afterwards by tenancy_schema._add_feature_columns, so absent when the
# fixtures below seed their rows.
ASK_TURN_EVIDENCE_PACK_COLUMNS = (
    "retrieval_trace",
    "tried_lanes",
    "warning_codes",
    "fallback_disclosure",
)


def _run_steps(engine, *steps) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in steps:
                step()


def _upgrade_to_pre_final_boundary(config: Config, engine) -> None:
    command.upgrade(config, "0001_initial_release")
    _run_steps(
        engine,
        epic1._upgrade_base_schema,
        workspace_phase.upgrade,
        mcp_phase.upgrade,
        public_phase.upgrade,
        media_phase.upgrade,
        library_phase.upgrade,
        content_hash_phase.upgrade,
    )


def _insert_workspace(connection, workspaces, *, workspace_id, actor_id, slug, now):
    values = {
        "id": workspace_id.hex,
        "slug": slug,
        "title": "Tenancy Migration Workspace",
        "name": "Tenancy Migration Workspace",
        "description": None,
        "goal": "Migration coverage",
        "default_language": "en",
        "library_policy": {},
        "write_policy": {},
        "citation_policy": {},
        "agent_injection_policy": {},
        "review_policy": {},
        "lint_policy": {},
        "auto_fix_enabled": False,
        "visibility": "private",
        "created_by_actor_id": actor_id.hex,
        "owner_actor_id": actor_id.hex,
        "owner_user_id": None,
        "updated_at": now,
        "created_at": now,
    }
    if "directory_title_key" in workspaces.c:
        values["directory_title_key"] = slug.replace("-", " ")
    connection.execute(workspaces.insert().values(**values))


def _insert_media_write_intent(connection, table, *, workspace_id, now):
    connection.execute(
        table.insert().values(workspace_id=workspace_id.hex, updated_at=now)
    )


def _insert_stored_blob(connection, table, *, workspace_id, blob_id, sha256, now):
    connection.execute(
        table.insert().values(
            workspace_id=workspace_id.hex,
            id=blob_id.hex,
            sha256=sha256,
            created_at=now,
            updated_at=now,
        )
    )


def _insert_blob_incarnation(
    connection, table, *, workspace_id, blob_id, blob_epoch, storage_key, now
):
    connection.execute(
        table.insert().values(
            workspace_id=workspace_id.hex,
            blob_id=blob_id.hex,
            blob_epoch=blob_epoch,
            byte_size=10,
            storage_key=storage_key,
            created_at=now,
        )
    )


def _insert_agent_page(connection, table, *, page_id, workspace_id, slug, now):
    connection.execute(
        table.insert().values(
            id=page_id.hex,
            workspace_id=workspace_id.hex,
            category="general",
            title="Agent Page",
            slug=slug,
            tags=[],
            lifecycle_status="draft",
            injection_policy="task_matched",
            selection_modifiers={
                "policy_matched": False,
                "recency_aware": False,
                "user_invoked": False,
            },
            priority=100,
            sensitivity="workspace",
            updated_at=now,
            created_at=now,
        )
    )


def _insert_agent_page_version(connection, table, *, version_id, agent_page_id, now):
    connection.execute(
        table.insert().values(
            id=version_id.hex,
            agent_page_id=agent_page_id.hex,
            version_no=1,
            body_markdown="# Agent Page\n\nbody",
            frontmatter={},
            actor_type="human",
            version_status="draft",
            created_at=now,
        )
    )


def _insert_tree_node(connection, table, *, node_id, workspace_id, now):
    connection.execute(
        table.insert().values(
            id=node_id.hex,
            workspace_id=workspace_id.hex,
            section="context",
            node_kind="folder",
            sort_key="0001",
            title="Root",
            is_system_node=False,
            can_have_children=True,
            primary_placement=True,
            visibility_status="visible",
            updated_at=now,
            created_at=now,
        )
    )


def _insert_ask_session(
    connection, table, *, session_id, workspace_id, tree_node_id, now
):
    connection.execute(
        table.insert().values(
            id=session_id.hex,
            workspace_id=workspace_id.hex,
            title="Ask Session",
            started_at=now,
            actor_kind="human",
            title_user_edited=False,
            default_topic_tags=[],
            placement_tree_node_id=tree_node_id.hex,
            updated_at=now,
            created_at=now,
        )
    )


def _insert_ask_turn(connection, table, *, turn_id, ask_session_id, turn_no, now):
    values = {
        "id": turn_id.hex,
        "ask_session_id": ask_session_id.hex,
        "turn_no": turn_no,
        "topic_tags": [],
        "promotion_state": "not_promoted",
        "created_at": now,
    }
    # Seeding happens at the pre-final boundary, so the split below is a
    # statement about that revision, not a shrug at whatever schema turns up.
    # The three array columns come from 0001; the evidence-pack columns are
    # added later by tenancy_schema._add_feature_columns. Asserting the split
    # means a column that stops being added fails here instead of quietly
    # changing what this fixture inserts.
    present = {name for name in ASK_TURN_0001_ARRAY_COLUMNS if name in table.c}
    assert present == set(ASK_TURN_0001_ARRAY_COLUMNS), (
        "ask_turn lost a 0001 array column before the final boundary: "
        f"{sorted(set(ASK_TURN_0001_ARRAY_COLUMNS) - present)}"
    )
    early = {name for name in ASK_TURN_EVIDENCE_PACK_COLUMNS if name in table.c}
    assert not early, (
        "ask_turn gained an evidence-pack column before tenancy_schema runs; "
        f"this fixture's revision assumption is stale: {sorted(early)}"
    )
    for column_name in ASK_TURN_0001_ARRAY_COLUMNS:
        values[column_name] = []
    connection.execute(table.insert().values(**values))


def _insert_ask_turn_citation(connection, table, *, ask_turn_id, now):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            ask_turn_id=ask_turn_id.hex,
            target_kind="context_page",
            target_id=uuid4().hex,
            rank=0,
            created_at=now,
        )
    )


def _insert_change_set(connection, table, *, change_set_id, workspace_id, now):
    connection.execute(
        table.insert().values(
            id=change_set_id.hex,
            workspace_id=workspace_id.hex,
            trigger_type="manual",
            actor_type="human",
            status="draft_created",
            risk_level="low",
            cite_call_skipped=False,
            updated_at=now,
            created_at=now,
        )
    )


def _insert_change_operation(connection, table, *, change_set_id, now):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            change_set_id=change_set_id.hex,
            target_type="context_page",
            target_id=uuid4().hex,
            operation="update",
            created_at=now,
        )
    )


def _insert_library_item(connection, items, *, item_id, workspace_id, uri, now):
    connection.execute(
        items.insert().values(
            id=item_id.hex,
            workspace_id=workspace_id.hex,
            connector_type="manual",
            format_hint="unknown",
            uri=uri,
            metadata={},
            trust_level="unknown",
            lifecycle_status="registered",
            created_at=now,
            updated_at=now,
        )
    )


def _insert_library_item_version(
    connection, versions, *, version_id, item_id, workspace_id, now
):
    values = {
        "id": version_id.hex,
        "library_item_id": item_id.hex,
        "version_no": 1,
        "content_hash": "0" * 64,
        "raw_storage_ref": "inline-text:0" * 5,
        "fetched_at": now,
        "processing_status": "completed",
        "preview_status": "unavailable",
        "created_at": now,
        "updated_at": now,
    }
    if "workspace_id" in versions.c:
        values["workspace_id"] = workspace_id.hex
    connection.execute(versions.insert().values(**values))


# A-1: library_run/library_attempt/
# library_artifact now carry workspace_id directly from creation (see the
# equivalent comment in test_workspace_tenancy_columns_migration.py), so
# they're seeded here only as required FK-target data (library_artifact is
# still referenced by library_semantic_overlay/agent_input_bundle_item
# below), not as a backfill assertion subject. The legacy nullable-attempt
# shape _insert_library_artifact used to build (no attempt_id, no blob) is
# retired -- every artifact now needs a real attempt and a real blob.
def _insert_library_run(
    connection, table, *, run_id, workspace_id, library_item_version_id, stage, now
):
    connection.execute(
        table.insert().values(
            id=run_id.hex,
            created_at=now,
            workspace_id=workspace_id.hex,
            library_item_version_id=library_item_version_id.hex,
            stage=stage,
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="v1",
            output_schema_version="v1",
            idempotency_key=uuid4().hex,
            updated_at=now,
        )
    )


def _insert_library_attempt(
    connection,
    table,
    *,
    attempt_id,
    run_id,
    workspace_id,
    library_item_version_id,
    stage,
    now,
):
    connection.execute(
        table.insert().values(
            id=attempt_id.hex,
            created_at=now,
            workspace_id=workspace_id.hex,
            run_id=run_id.hex,
            library_item_version_id=library_item_version_id.hex,
            stage=stage,
            provider_id="provider",
            provider_version="1",
            provider_spi_version="1",
            config_hash="0" * 64,
            input_sha256="0" * 64,
            output_schema_version="v1",
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
            warnings_json=[],
            provenance_json={},
            metrics_json={},
        )
    )


def _insert_library_artifact(
    connection,
    table,
    *,
    artifact_id,
    library_item_version_id,
    workspace_id,
    attempt_id,
    stage,
    blob_id,
    now,
):
    connection.execute(
        table.insert().values(
            id=artifact_id.hex,
            created_at=now,
            workspace_id=workspace_id.hex,
            library_item_version_id=library_item_version_id.hex,
            attempt_id=attempt_id.hex,
            stage=stage,
            blob_workspace_id=workspace_id.hex,
            blob_id=blob_id.hex,
            blob_epoch=1,
            role="markdown",
            kind="markdown",
            media_type="text/markdown",
            content_sha256="0" * 64,
            byte_size=4,
            ordinal=0,
        )
    )


def _insert_semantic_overlay(
    connection, table, *, library_item_version_id, base_artifact_id, now
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            library_item_version_id=library_item_version_id.hex,
            base_artifact_id=base_artifact_id.hex,
            base_artifact_sha256="0" * 64,
            body_markdown="# Overlay",
            content_sha256="1" * 64,
            byte_size=10,
            revision=1,
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
            updated_by_actor_kind="human",
            updated_by_actor_id=ACTOR_ID,
            updated_at=now,
            created_at=now,
        )
    )


def _insert_ingress_proposal(
    connection, table, *, proposal_id, workspace_id, tree_node_id, now
):
    values = {
        "id": proposal_id.hex,
        "workspace_id": workspace_id.hex,
        "source_kind": "library",
        "proposed_context_page_type": "overview",
        "proposed_parent_tree_node_id": tree_node_id.hex,
        "proposed_title": "Proposal",
        "proposed_save_mode": "new_page",
        "placement_confidence": "medium",
        "alternatives": [],
        "status": "proposed",
        "updated_at": now,
        "created_at": now,
    }
    assert "library_item_ids" in table.c, (
        "context_ingress_proposal.library_item_ids is missing at the "
        "pre-final boundary; 0002's base schema should have created it"
    )
    values["library_item_ids"] = []
    connection.execute(table.insert().values(**values))


def _insert_proposal_source_item(
    connection, table, *, proposal_id, library_item_id, now
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            proposal_id=proposal_id.hex,
            library_item_id=library_item_id.hex,
            rank=0,
            created_at=now,
        )
    )


def _insert_review_item(
    connection, table, *, review_item_id, workspace_id, change_set_id, now
):
    connection.execute(
        table.insert().values(
            id=review_item_id.hex,
            workspace_id=workspace_id.hex,
            change_set_id=change_set_id.hex,
            review_type="context_write",
            status="pending",
            risk_level="low",
            updated_at=now,
            created_at=now,
        )
    )


def _insert_review_decision(connection, table, *, review_item_id, now):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            review_item_id=review_item_id.hex,
            decision_type="approve",
            actor_type="human",
            resulting_review_status="resolved",
            resulting_change_set_status="published",
            created_at=now,
        )
    )


def _insert_mcp_client(connection, table, *, client_id, workspace_id, name, now):
    connection.execute(
        table.insert().values(
            id=client_id.hex,
            workspace_id=workspace_id.hex,
            name=name,
            client_type="generic",
            scopes=[],
            status="active",
            token_metadata={},
            updated_at=now,
            created_at=now,
        )
    )


def _insert_mcp_chat_session(
    connection, table, *, session_id, workspace_id, client_id, mcp_session_id, now
):
    connection.execute(
        table.insert().values(
            id=session_id.hex,
            workspace_id=workspace_id.hex,
            client_id=client_id.hex,
            mcp_session_id=mcp_session_id,
            started_at=now,
            last_activity_at=now,
            tool_call_count=0,
            write_count=0,
            error_count=0,
            title_user_edited=False,
        )
    )


def _insert_mcp_tool_call(
    connection, table, *, call_id, client_id, workspace_id, request_id, now
):
    connection.execute(
        table.insert().values(
            id=call_id.hex,
            client_id=client_id.hex,
            workspace_id=workspace_id.hex,
            request_id=request_id.hex,
            schema_version="llmwiki.mcp.v1",
            tool_name="read",
            input_summary={},
            result_status="success",
            warnings=[],
            provider="unknown",
            model_id="unknown",
            params_summary={},
            client_self_reported=False,
            started_at=now,
        )
    )


def _insert_chat_artifact_link(
    connection, table, *, chat_session_id, last_mcp_tool_call_id, now
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            chat_session_id=chat_session_id.hex,
            resource_kind="context_page",
            resource_id=uuid4().hex,
            verb="cited",
            first_occurred_at=now,
            last_occurred_at=now,
            occurrence_count=1,
            last_mcp_tool_call_id=last_mcp_tool_call_id.hex,
        )
    )


def _insert_chat_tool_call_artifact(
    connection, table, *, chat_session_id, mcp_tool_call_id, now
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            chat_session_id=chat_session_id.hex,
            mcp_tool_call_id=mcp_tool_call_id.hex,
            resource_kind="context_page",
            resource_id=uuid4().hex,
            verb="cited",
            occurred_at=now,
        )
    )


def _insert_agent_input_bundle(
    connection, table, *, bundle_id, workspace_id, client_id, now
):
    connection.execute(
        table.insert().values(
            id=bundle_id.hex,
            workspace_id=workspace_id.hex,
            client_id=client_id.hex,
            task_intent="answer",
            token_budget=1000,
            status="generated",
            ttl_seconds=3600,
            valid_until=now,
            invalidation_keys={},
            created_at=now,
        )
    )


def _insert_agent_input_bundle_item(
    connection, table, *, bundle_id, library_artifact_id, now
):
    connection.execute(
        table.insert().values(
            id=uuid4().hex,
            bundle_id=bundle_id.hex,
            source_kind="library_artifact_excerpt",
            library_artifact_id=library_artifact_id.hex,
            priority=100,
            included_reason="relevant",
            created_at=now,
        )
    )


def test_migration_backfills_workspace_id_from_the_correct_parent_chain(
    tmp_path: Path,
):
    database_url = _database_url(tmp_path, "tenancy-columns-b2.db")
    config = _config(database_url)
    engine = create_engine(database_url)
    _upgrade_to_pre_final_boundary(config, engine)

    now = datetime.now(UTC)
    actor_id = uuid4()

    ws_a = uuid4()
    ws_b = uuid4()

    agent_page_a = uuid4()
    agent_page_version_a = uuid4()
    agent_page_b = uuid4()
    agent_page_version_b = uuid4()

    tree_node_a = uuid4()
    ask_session_a = uuid4()
    ask_turn_a = uuid4()

    change_set_a = uuid4()
    change_set_b = uuid4()

    item_a = uuid4()
    item_version_a = uuid4()
    run_a = uuid4()
    attempt_a = uuid4()
    artifact_a = uuid4()
    blob_id_a = uuid4()

    proposal_a = uuid4()
    review_item_a = uuid4()

    mcp_client_a = uuid4()
    mcp_chat_session_a = uuid4()
    mcp_tool_call_a = uuid4()

    bundle_a = uuid4()

    try:
        metadata = sa.MetaData()

        def tbl(name):
            return sa.Table(name, metadata, autoload_with=engine)

        workspaces = tbl("workspace")
        media_write_intent = tbl("workspace_media_write_intent")
        stored_blob = tbl("stored_blob")
        blob_incarnation = tbl("blob_incarnation")
        agent_page = tbl("agent_page")
        agent_page_version = tbl("agent_page_version")
        tree_node = tbl("workspace_tree_node")
        ask_session = tbl("ask_session")
        ask_turn = tbl("ask_turn")
        ask_turn_citation = tbl("ask_turn_citation")
        change_set = tbl("change_set")
        change_operation = tbl("change_operation")
        library_item = tbl("library_item")
        library_item_version = tbl("library_item_version")
        library_run = tbl("library_run")
        library_attempt = tbl("library_attempt")
        library_artifact = tbl("library_artifact")
        library_semantic_overlay = tbl("library_semantic_overlay")
        context_ingress_proposal = tbl("context_ingress_proposal")
        proposal_source_item = tbl("proposal_source_item")
        review_item = tbl("review_item")
        review_decision = tbl("review_decision")
        mcp_client = tbl("mcp_client")
        mcp_chat_session = tbl("mcp_chat_session")
        mcp_tool_call = tbl("mcp_tool_call")
        mcp_chat_artifact_link = tbl("mcp_chat_artifact_link")
        mcp_chat_tool_call_artifact = tbl("mcp_chat_tool_call_artifact")
        agent_input_bundle = tbl("agent_input_bundle")
        agent_input_bundle_item = tbl("agent_input_bundle_item")

        with engine.begin() as connection:
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=ws_a,
                actor_id=actor_id,
                slug="tenancy-b2-a",
                now=now,
            )
            _insert_workspace(
                connection,
                workspaces,
                workspace_id=ws_b,
                actor_id=actor_id,
                slug="tenancy-b2-b",
                now=now,
            )
            _insert_media_write_intent(
                connection, media_write_intent, workspace_id=ws_a, now=now
            )

            # 1. agent_page_version (+ workspace-B counterpart)
            _insert_agent_page(
                connection,
                agent_page,
                page_id=agent_page_a,
                workspace_id=ws_a,
                slug="agent-a",
                now=now,
            )
            _insert_agent_page_version(
                connection,
                agent_page_version,
                version_id=agent_page_version_a,
                agent_page_id=agent_page_a,
                now=now,
            )
            _insert_agent_page(
                connection,
                agent_page,
                page_id=agent_page_b,
                workspace_id=ws_b,
                slug="agent-b",
                now=now,
            )
            _insert_agent_page_version(
                connection,
                agent_page_version,
                version_id=agent_page_version_b,
                agent_page_id=agent_page_b,
                now=now,
            )

            # 2/3. ask_turn, ask_turn_citation
            _insert_tree_node(
                connection, tree_node, node_id=tree_node_a, workspace_id=ws_a, now=now
            )
            _insert_ask_session(
                connection,
                ask_session,
                session_id=ask_session_a,
                workspace_id=ws_a,
                tree_node_id=tree_node_a,
                now=now,
            )
            _insert_ask_turn(
                connection,
                ask_turn,
                turn_id=ask_turn_a,
                ask_session_id=ask_session_a,
                turn_no=1,
                now=now,
            )
            _insert_ask_turn_citation(
                connection, ask_turn_citation, ask_turn_id=ask_turn_a, now=now
            )

            # 4. change_operation (+ workspace-B counterpart)
            _insert_change_set(
                connection,
                change_set,
                change_set_id=change_set_a,
                workspace_id=ws_a,
                now=now,
            )
            _insert_change_operation(
                connection, change_operation, change_set_id=change_set_a, now=now
            )
            _insert_change_set(
                connection,
                change_set,
                change_set_id=change_set_b,
                workspace_id=ws_b,
                now=now,
            )
            _insert_change_operation(
                connection, change_operation, change_set_id=change_set_b, now=now
            )

            # Library chain shared by #5, #6, #11. library_run/library_
            # attempt/library_artifact carry workspace_id from creation now
            # (A-1), so it's supplied directly here as required FK-target
            # data rather than left for backfill -- see _insert_library_
            # artifact's comment above. The former separate #5 (library_
            # preview_artifact, backfilled from library_item_version) is
            # gone: preview_generation is now just a stage of this same
            # already-workspace_id-populated library_artifact, and that "no
            # backfill needed" invariant is already covered in test_
            # workspace_tenancy_columns_migration.py.
            _insert_library_item(
                connection,
                library_item,
                item_id=item_a,
                workspace_id=ws_a,
                uri="file:///b2-a",
                now=now,
            )
            _insert_library_item_version(
                connection,
                library_item_version,
                version_id=item_version_a,
                item_id=item_a,
                workspace_id=ws_a,
                now=now,
            )
            _insert_stored_blob(
                connection,
                stored_blob,
                workspace_id=ws_a,
                blob_id=blob_id_a,
                sha256="2" * 64,
                now=now,
            )
            _insert_blob_incarnation(
                connection,
                blob_incarnation,
                workspace_id=ws_a,
                blob_id=blob_id_a,
                blob_epoch=1,
                storage_key="b2-a-storage-key",
                now=now,
            )
            _insert_library_run(
                connection,
                library_run,
                run_id=run_a,
                workspace_id=ws_a,
                library_item_version_id=item_version_a,
                stage="conversion",
                now=now,
            )
            _insert_library_attempt(
                connection,
                library_attempt,
                attempt_id=attempt_a,
                run_id=run_a,
                workspace_id=ws_a,
                library_item_version_id=item_version_a,
                stage="conversion",
                now=now,
            )
            _insert_library_artifact(
                connection,
                library_artifact,
                artifact_id=artifact_a,
                library_item_version_id=item_version_a,
                workspace_id=ws_a,
                attempt_id=attempt_a,
                stage="conversion",
                blob_id=blob_id_a,
                now=now,
            )

            # 6. library_semantic_overlay
            _insert_semantic_overlay(
                connection,
                library_semantic_overlay,
                library_item_version_id=item_version_a,
                base_artifact_id=artifact_a,
                now=now,
            )

            # 7. proposal_source_item
            _insert_ingress_proposal(
                connection,
                context_ingress_proposal,
                proposal_id=proposal_a,
                workspace_id=ws_a,
                tree_node_id=tree_node_a,
                now=now,
            )
            _insert_proposal_source_item(
                connection,
                proposal_source_item,
                proposal_id=proposal_a,
                library_item_id=item_a,
                now=now,
            )

            # 8. review_decision
            _insert_review_item(
                connection,
                review_item,
                review_item_id=review_item_a,
                workspace_id=ws_a,
                change_set_id=change_set_a,
                now=now,
            )
            _insert_review_decision(
                connection, review_decision, review_item_id=review_item_a, now=now
            )

            # 9/10. mcp_chat_artifact_link, mcp_chat_tool_call_artifact
            _insert_mcp_client(
                connection,
                mcp_client,
                client_id=mcp_client_a,
                workspace_id=ws_a,
                name="test-client",
                now=now,
            )
            _insert_mcp_chat_session(
                connection,
                mcp_chat_session,
                session_id=mcp_chat_session_a,
                workspace_id=ws_a,
                client_id=mcp_client_a,
                mcp_session_id="mcp-session-a",
                now=now,
            )
            _insert_mcp_tool_call(
                connection,
                mcp_tool_call,
                call_id=mcp_tool_call_a,
                client_id=mcp_client_a,
                workspace_id=ws_a,
                request_id=uuid4(),
                now=now,
            )
            _insert_chat_artifact_link(
                connection,
                mcp_chat_artifact_link,
                chat_session_id=mcp_chat_session_a,
                last_mcp_tool_call_id=mcp_tool_call_a,
                now=now,
            )
            _insert_chat_tool_call_artifact(
                connection,
                mcp_chat_tool_call_artifact,
                chat_session_id=mcp_chat_session_a,
                mcp_tool_call_id=mcp_tool_call_a,
                now=now,
            )

            # 11. agent_input_bundle_item
            _insert_agent_input_bundle(
                connection,
                agent_input_bundle,
                bundle_id=bundle_a,
                workspace_id=ws_a,
                client_id=mcp_client_a,
                now=now,
            )
            _insert_agent_input_bundle_item(
                connection,
                agent_input_bundle_item,
                bundle_id=bundle_a,
                library_artifact_id=artifact_a,
                now=now,
            )

        _run_steps(engine, final_phase.upgrade)

        engine.dispose()
        engine = create_engine(database_url)
        with engine.connect() as connection:

            def scalar(table_name, id_col, id_value):
                table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
                return connection.execute(
                    sa.select(table.c.workspace_id).where(
                        table.c[id_col] == id_value.hex
                    )
                ).scalar_one()

            def scalar_where(table_name, where_col, where_value):
                table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
                return connection.execute(
                    sa.select(table.c.workspace_id).where(
                        table.c[where_col] == where_value.hex
                    )
                ).scalar_one()

            assert scalar("agent_page_version", "id", agent_page_version_a) == ws_a.hex
            assert scalar("agent_page_version", "id", agent_page_version_b) == ws_b.hex

            assert scalar("ask_turn", "id", ask_turn_a) == ws_a.hex
            assert (
                scalar_where("ask_turn_citation", "ask_turn_id", ask_turn_a) == ws_a.hex
            )

            assert (
                scalar_where("change_operation", "change_set_id", change_set_a)
                == ws_a.hex
            )
            assert (
                scalar_where("change_operation", "change_set_id", change_set_b)
                == ws_b.hex
            )

            assert (
                scalar_where(
                    "library_semantic_overlay",
                    "library_item_version_id",
                    item_version_a,
                )
                == ws_a.hex
            )
            # library_run/library_attempt/library_artifact are not backfilled
            # any more (A-1: workspace_id set directly at creation) -- these
            # confirm the final phase leaves their already-correct
            # workspace_id alone rather than testing a backfill that no
            # longer applies to them (that invariant is covered where it
            # still applies in test_workspace_tenancy_columns_migration.py).
            assert scalar("library_run", "id", run_a) == ws_a.hex
            assert scalar("library_attempt", "id", attempt_a) == ws_a.hex
            assert scalar("library_artifact", "id", artifact_a) == ws_a.hex
            assert (
                scalar_where("proposal_source_item", "proposal_id", proposal_a)
                == ws_a.hex
            )
            assert (
                scalar_where("review_decision", "review_item_id", review_item_a)
                == ws_a.hex
            )
            assert (
                scalar_where(
                    "mcp_chat_artifact_link", "chat_session_id", mcp_chat_session_a
                )
                == ws_a.hex
            )
            assert (
                scalar_where(
                    "mcp_chat_tool_call_artifact",
                    "chat_session_id",
                    mcp_chat_session_a,
                )
                == ws_a.hex
            )
            assert (
                scalar_where("agent_input_bundle_item", "bundle_id", bundle_a)
                == ws_a.hex
            )

            for table_name in (
                "agent_page_version",
                "ask_turn",
                "ask_turn_citation",
                "change_operation",
                "library_run",
                "library_attempt",
                "library_artifact",
                "library_semantic_overlay",
                "proposal_source_item",
                "review_decision",
                "mcp_chat_artifact_link",
                "mcp_chat_tool_call_artifact",
                "agent_input_bundle_item",
            ):
                table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
                assert not table.c.workspace_id.nullable, table_name
    finally:
        engine.dispose()
