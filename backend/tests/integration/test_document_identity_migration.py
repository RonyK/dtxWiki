"""One populated locator traversal per supported dialect."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.models import (
    ClientDelegatedTask,
    ContextPage,
    ContextPageVersion,
    LibraryItem,
    LintFinding,
    LintRun,
)
from backend.models.ask import AskSession, AskTurn
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.mcp_trace import MCPWriteIdempotency
from backend.models.tree import WorkspaceTreeNode
from backend.scripts.seed_dev import seed_default_workspace
from backend.tests.utils.postgres import drop_reflected_postgres_metadata
from backend.utils.content_hash import content_hash

PARENT = "872d9c3e104b"
REVISION = "e4f7a2c901bd"


@pytest.mark.release_migration
@pytest.mark.parametrize(
    "dialect",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.xdist_group("postgres"))],
)
def test_document_identity_conversion_traversal(tmp_path, dialect):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'identities.sqlite'}"
        if dialect == "sqlite"
        else os.getenv("DTXWIKI_POSTGRES_MIGRATION_TEST_DATABASE_URL")
    )
    if not url or not url.startswith(dialect):
        pytest.skip("requires a dedicated PostgreSQL migration test database")
    engine = sa.create_engine(url)
    assert not (set(sa.inspect(engine).get_table_names()) - {"alembic_version"})
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["context_repositories_root"] = tmp_path / "repositories"
    now = datetime(2026, 11, 15, tzinfo=UTC)
    (
        page_id,
        version_id,
        library_id,
        task_id,
        finding_id,
        orphan_id,
        library_finding_id,
        receipt_id,
    ) = (uuid4() for _ in range(8))
    ask_session_id, ask_turn_id, gap_id, augment_id, missing_augment_id = (
        uuid4() for _ in range(5)
    )
    try:
        command.upgrade(config, PARENT)
        with Session(engine) as session:
            workspace_id = seed_default_workspace(session).id
            session.add(
                ContextPage(
                    id=page_id,
                    workspace_id=workspace_id,
                    slug="renamed-page",
                    title="Page",
                )
            )
            session.flush()
            session.add(
                ContextPageVersion(
                    id=version_id,
                    workspace_id=workspace_id,
                    page_id=page_id,
                    body_md="# Editing\n",
                    content_hash=content_hash("# Editing\n"),
                    version_status="editing",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="kept-author",
                )
            )
            session.flush()
            session.get(ContextPage, page_id).current_version_id = version_id
            session.add(
                LibraryItem(
                    id=library_id,
                    workspace_id=workspace_id,
                    connector_type="url",
                    uri="https://example.com/retained",
                    title="Library",
                )
            )
            run = LintRun(
                workspace_id=workspace_id, trigger_type="manual", status="completed"
            )
            session.add(run)
            session.flush()
            mixed = {
                "concept_path": "/context/old-page.md",
                "nested": [
                    {"page_id": str(page_id), "concept_path": "/context/older-page.md"},
                    {
                        "concept_path": "/context/missing.md",
                        "note": "retained evidence",
                    },
                    {"expected_concept_path": f"/library/{library_id}.md"},
                ],
            }
            session.add_all(
                [
                    ClientDelegatedTask(
                        id=task_id,
                        workspace_id=workspace_id,
                        task_type="page_meta_refresh",
                        target_kind="context_page",
                        target_id=page_id,
                        payload=mixed,
                        source="lint",
                        fingerprint="mixed",
                    ),
                    LintFinding(
                        id=finding_id,
                        workspace_id=workspace_id,
                        lint_run_id=run.id,
                        target_type="context_page",
                        target_id=page_id,
                        rule_id="maintenance.page_meta_suggestion",
                        message_args=mixed,
                        suggested_action=json.dumps(
                            {
                                "concept_path": "/context/old-page.md",
                                "aliases": ["Keep"],
                            }
                        ),
                    ),
                    LintFinding(
                        id=library_finding_id,
                        workspace_id=workspace_id,
                        lint_run_id=run.id,
                        target_type="library_item",
                        target_id=library_id,
                        rule_id="maintenance.import_batch_classification",
                        suggested_action=json.dumps(
                            {
                                "expected_concept_path": f"/library/{library_id}.md",
                                "note": "Keep",
                            }
                        ),
                    ),
                    LintFinding(
                        id=orphan_id,
                        workspace_id=workspace_id,
                        lint_run_id=run.id,
                        target_type="context_page",
                        target_id=uuid4(),
                        rule_id="maintenance.page_meta_suggestion",
                        suggested_action=json.dumps(
                            {
                                "concept_path": "/context/renamed-page.md",
                                "aliases": ["Must not retarget"],
                            }
                        ),
                    ),
                    MCPWriteIdempotency(
                        id=receipt_id,
                        workspace_id=workspace_id,
                        actor_kind="human",
                        actor_id="kept-author",
                        tool_name="begin_context_edit",
                        idempotency_key="receipt-key",
                        request_fingerprint="same-fingerprint",
                        result_payload={
                            "page_id": str(page_id),
                            "version_id": str(version_id),
                            "version_status": "editing",
                            "concept_path": "/context/old-page.md",
                            "stable_url": f"/p/{workspace_id}/{page_id}",
                            "latest_draft_version_id": str(version_id),
                        },
                        created_at=now,
                        expires_at=now + timedelta(days=1),
                    ),
                ]
            )
            placement_id = session.scalar(
                sa.select(WorkspaceTreeNode.id)
                .where(WorkspaceTreeNode.workspace_id == workspace_id)
                .limit(1)
            )
            session.add(
                AskSession(
                    id=ask_session_id,
                    workspace_id=workspace_id,
                    title="Kept Ask",
                    actor_kind="human",
                    placement_tree_node_id=placement_id,
                )
            )
            session.flush()
            session.add(
                AskTurn(
                    id=ask_turn_id,
                    workspace_id=workspace_id,
                    ask_session_id=ask_session_id,
                    turn_no=1,
                    user_message="Kept question",
                    assistant_message="Kept answer",
                    retrieval_trace=[
                        {
                            "type": "context_page",
                            "id": str(page_id),
                            "concept_path": "/context/old-page.md",
                            "title": "Kept title",
                            "score": "1.0",
                        },
                        {
                            "type": "library_item",
                            "id": str(library_id),
                            "concept_path": "/library/old-label.md",
                            "title": "Kept library",
                        },
                    ],
                )
            )
            session.add(
                KnowledgeCoverageGap(
                    id=gap_id,
                    workspace_id=workspace_id,
                    query_norm="retained query",
                    surface="search_mcp",
                    nearest_candidates=[
                        {
                            "kind": "context_page",
                            "id": str(page_id),
                            "concept_path": "/context/old-page.md",
                            "title": "Keep candidate",
                        }
                    ],
                    rewrite_attempts=[
                        {
                            "candidates": [
                                {
                                    "kind": "library_item",
                                    "id": str(library_id),
                                    "concept_path": "/library/renamed.md",
                                }
                            ]
                        }
                    ],
                )
            )
            session.add_all(
                [
                    LintFinding(
                        id=augment_id,
                        workspace_id=workspace_id,
                        lint_run_id=run.id,
                        target_type="knowledge_coverage_gap",
                        target_id=gap_id,
                        rule_id="maintenance.coverage_gap_verdict",
                        suggested_action=json.dumps(
                            {
                                "verdict": "augment_existing",
                                "concept_path": "/context/renamed-page.md",
                                "rationale": "Keep",
                            }
                        ),
                    ),
                    LintFinding(
                        id=missing_augment_id,
                        workspace_id=workspace_id,
                        lint_run_id=run.id,
                        target_type="knowledge_coverage_gap",
                        target_id=gap_id,
                        rule_id="maintenance.coverage_gap_verdict",
                        suggested_action=json.dumps(
                            {
                                "verdict": "augment_existing",
                                "concept_path": "/context/missing.md",
                                "rationale": "Missing",
                            }
                        ),
                    ),
                ]
            )
            session.commit()
        if dialect == "sqlite":
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO search_doc_fts "
                        "(context_header, body, source_kind, entity_id, workspace_id) "
                        "VALUES (:header, '', 'context_page', :id, :workspace)"
                    ),
                    {
                        "header": "Title: Page\nPath: /context/old-page.md\nTags: keep",
                        "id": str(page_id),
                        "workspace": str(workspace_id),
                    },
                )
        command.upgrade(config, REVISION)
        with Session(engine) as session:
            task = session.get(ClientDelegatedTask, task_id)
            assert task.payload["page_id"] == str(page_id)
            assert task.payload["stable_url"] == f"/p/{workspace_id}/{page_id}"
            assert task.payload["nested"][0] == {
                "page_id": str(page_id),
                "stable_url": f"/p/{workspace_id}/{page_id}",
            }
            assert task.payload["nested"][1] == {
                "locator": "/context/missing.md",
                "note": "retained evidence",
            }
            assert task.payload["nested"][2] == {
                "expected_kind": "library_item",
                "expected_id": str(library_id),
            }
            assert json.loads(
                session.get(LintFinding, finding_id).suggested_action
            ) == {
                "page_id": str(page_id),
                "stable_url": f"/p/{workspace_id}/{page_id}",
                "aliases": ["Keep"],
            }
            assert json.loads(
                session.get(LintFinding, library_finding_id).suggested_action
            ) == {
                "expected_kind": "library_item",
                "expected_id": str(library_id),
                "note": "Keep",
            }
            assert session.get(LintFinding, orphan_id) is None
            assert session.get(LintFinding, missing_augment_id) is None
            assert json.loads(
                session.get(LintFinding, augment_id).suggested_action
            ) == {
                "verdict": "augment_existing",
                "page_id": str(page_id),
                "stable_url": f"/p/{workspace_id}/{page_id}",
                "rationale": "Keep",
            }
            trace = session.get(AskTurn, ask_turn_id).retrieval_trace
            assert trace == [
                {
                    "type": "context_page",
                    "id": str(page_id),
                    "stable_url": f"/p/{workspace_id}/{page_id}",
                    "title": "Kept title",
                    "score": "1.0",
                },
                {
                    "type": "library_item",
                    "id": str(library_id),
                    "stable_url": (
                        f"/workspaces/{workspace_id}/library/items/{library_id}"
                    ),
                    "title": "Kept library",
                },
            ]
            gap = session.get(KnowledgeCoverageGap, gap_id)
            assert gap.nearest_candidates == [
                {
                    "kind": "context_page",
                    "id": str(page_id),
                    "stable_url": f"/p/{workspace_id}/{page_id}",
                    "title": "Keep candidate",
                }
            ]
            assert gap.rewrite_attempts == [
                {
                    "candidates": [
                        {
                            "kind": "library_item",
                            "id": str(library_id),
                            "stable_url": (
                                f"/workspaces/{workspace_id}/library/items/{library_id}"
                            ),
                        }
                    ]
                }
            ]
            receipt = session.get(MCPWriteIdempotency, receipt_id)
            assert receipt.request_fingerprint == "same-fingerprint"
            assert receipt.result_payload == {
                "page_id": str(page_id),
                "version_id": str(version_id),
                "version_status": "editing",
                "stable_url": f"/p/{workspace_id}/{page_id}",
                "latest_draft_version_id": str(version_id),
            }
            assert (
                session.get(ContextPageVersion, version_id).version_status == "editing"
            )
            assert session.get(ContextPage, page_id).current_version_id == version_id
        with engine.connect() as connection:
            jobs = sa.Table("job", sa.MetaData(), autoload_with=connection)
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(jobs)
                    .where(jobs.c.idempotency_key.like("locator-index:%"))
                )
                == 2
            )
            if dialect == "sqlite":
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT context_header FROM search_doc_fts "
                            "WHERE entity_id = :id"
                        ),
                        {"id": str(page_id)},
                    )
                    == "Title: Page\nTags: keep"
                )
        # Original locator text cannot be reconstructed after renames. Verify
        # the explicit rollback boundary, then replay the data conversion to
        # exercise restart safety within this same dialect traversal.
        with pytest.raises(RuntimeError, match="pre-upgrade dtxWiki backup"):
            command.downgrade(config, PARENT)
        command.upgrade(config, REVISION)
        from backend.migrations.revision_modules.unreleased._e4f7a2c901bd_locators import (  # noqa: E501
            conversion,
        )

        with engine.begin() as connection:
            conversion.convert_locators(connection)
            conversion.convert_command_receipts(connection)
            conversion.enqueue_reindex(connection)
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(jobs)
                    .where(jobs.c.idempotency_key.like("locator-index:%"))
                )
                == 2
            )
        assert "context_version_parent" not in sa.inspect(engine).get_table_names()
        assert "uq_cpv_current_editing_per_author" in {
            index["name"]
            for index in sa.inspect(engine).get_indexes("context_page_version")
        }
    finally:
        if dialect == "postgresql":
            drop_reflected_postgres_metadata(engine)
        engine.dispose()
