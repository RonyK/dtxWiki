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
            lint_run_id = run.id
            mixed = {
                "concept_path": "/context/old-page.md",
                "nested": [
                    {"page_id": str(page_id), "concept_path": "/context/older-page.md"},
                    {
                        "concept_path": "/context/missing.md",
                        "note": "retained evidence",
                    },
                    {"expected_concept_path": f"/library/{library_id}.md"},
                    {"page_id": str(page_id), "note": "identity only"},
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
                        lint_run_id=lint_run_id,
                        target_type="context_page",
                        target_id=page_id,
                        rule_id="maintenance.page_meta_suggestion",
                        message_args=mixed,
                        suggested_action=json.dumps(
                            {
                                "concept_path": "/context/old-page.md",
                                "aliases": ["Keep"],
                            },
                            separators=(",", ":"),
                            indent=2,
                        ),
                    ),
                    LintFinding(
                        id=library_finding_id,
                        workspace_id=workspace_id,
                        lint_run_id=lint_run_id,
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
                        lint_run_id=lint_run_id,
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
                            "body_md": "PRIVATE RECEIPT BODY",
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
                            "debug": "PRIVATE TRACE BODY",
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
                        lint_run_id=lint_run_id,
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
                        lint_run_id=lint_run_id,
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

        def stored_state():
            with engine.connect() as connection:
                names = (
                    "client_delegated_task",
                    "lint_finding",
                    "knowledge_coverage_gap",
                    "ask_turn",
                    "mcp_write_idempotency",
                    "job",
                )
                state = {}
                for name in names:
                    table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                    state[name] = [
                        dict(row)
                        for row in connection.execute(
                            sa.select(table).order_by(table.c.id)
                        ).mappings()
                    ]
                if dialect == "sqlite":
                    state["fts"] = connection.execute(
                        sa.text(
                            "SELECT rowid, context_header FROM search_doc_fts "
                            "ORDER BY rowid"
                        )
                    ).all()
                return state

        original = stored_state()
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
                    "debug": "PRIVATE TRACE BODY",
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
                "body_md": "PRIVATE RECEIPT BODY",
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
        # The private journal contains locator deltas rather than copies of
        # unrelated receipts, Ask bodies or trace content.
        with engine.connect() as connection:
            journal = sa.Table(
                "document_locator_rollback", sa.MetaData(), autoload_with=connection
            )
            private_state = json.dumps(
                [
                    dict(row)
                    for row in connection.execute(sa.select(journal)).mappings()
                ],
                default=str,
            )
            assert "PRIVATE RECEIPT BODY" not in private_state
            assert "PRIVATE TRACE BODY" not in private_state
            assert "Kept question" not in private_state
            assert "Kept answer" not in private_state
        command.downgrade(config, PARENT)
        assert "document_locator_rollback" not in sa.inspect(engine).get_table_names()
        assert stored_state() == original
        command.upgrade(config, REVISION)
        # Later authored metadata survives reversal, and canonical objects
        # appended to an existing trace still get the previous wire fields.
        with Session(engine) as session:
            task = session.get(ClientDelegatedTask, task_id)
            task.payload = {**task.payload, "later_note": "Preserve later edit"}
            turn = session.get(AskTurn, ask_turn_id)
            turn.retrieval_trace = [
                {
                    "type": "library_item",
                    "id": str(library_id),
                    "stable_url": (
                        f"/workspaces/{workspace_id}/library/items/{library_id}"
                    ),
                    "title": "Retargeted",
                },
                turn.retrieval_trace[1],
                {
                    "type": "context_page",
                    "id": str(page_id),
                    "stable_url": f"/p/{workspace_id}/{page_id}",
                    "title": "New hit",
                },
            ]
            turn.assistant_message = "Later answer"
            new_turn_id = uuid4()
            session.add(
                AskTurn(
                    id=new_turn_id,
                    workspace_id=workspace_id,
                    ask_session_id=ask_session_id,
                    turn_no=2,
                    user_message="New question",
                    assistant_message="New answer",
                    retrieval_trace=[
                        {
                            "type": "library_item",
                            "id": str(library_id),
                            "stable_url": (
                                f"/workspaces/{workspace_id}/library/items/{library_id}"
                            ),
                        }
                    ],
                )
            )
            new_receipt_id = uuid4()
            new_receipt = {
                "page_id": str(page_id),
                "version_id": str(version_id),
                "citation_id": str(uuid4()),
            }
            session.add(
                MCPWriteIdempotency(
                    id=new_receipt_id,
                    workspace_id=workspace_id,
                    actor_kind="human",
                    actor_id="kept-author",
                    tool_name="cite",
                    idempotency_key="new-cite-key",
                    request_fingerprint="new-cite-fingerprint",
                    result_payload=new_receipt,
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
            from backend.models.agent import AgentPage

            agent_id = session.scalar(
                sa.select(AgentPage.id)
                .where(AgentPage.workspace_id == workspace_id)
                .limit(1)
            )
            new_golden_id = uuid4()
            session.add(
                LintFinding(
                    id=new_golden_id,
                    workspace_id=workspace_id,
                    lint_run_id=lint_run_id,
                    target_type="agent_page",
                    target_id=agent_id,
                    rule_id="maintenance.golden_query_candidates",
                    suggested_action=json.dumps(
                        {
                            "queries": [
                                {
                                    "query": "New agent",
                                    "expected_kind": "agent_page",
                                    "expected_id": str(agent_id),
                                }
                            ]
                        }
                    ),
                )
            )
            session.commit()
        with engine.begin() as connection:
            completed_job_id = connection.scalar(
                sa.select(jobs.c.id)
                .where(jobs.c.idempotency_key.like("locator-index:%"))
                .limit(1)
            )
            connection.execute(
                jobs.update()
                .where(jobs.c.id == completed_job_id)
                .values(status="completed", progress=100)
            )
        command.downgrade(config, PARENT)
        with Session(engine) as session:
            assert session.get(ClientDelegatedTask, task_id).payload == {
                **original["client_delegated_task"][0]["payload"],
                "later_note": "Preserve later edit",
            }
            turn = session.get(AskTurn, ask_turn_id)
            assert turn.assistant_message == "Later answer"
            assert turn.retrieval_trace[0] == {
                "type": "library_item",
                "id": str(library_id),
                "concept_path": f"/library/{library_id}.md",
                "title": "Retargeted",
            }
            assert (
                turn.retrieval_trace[1] == original["ask_turn"][0]["retrieval_trace"][1]
            )
            assert (
                session.get(MCPWriteIdempotency, new_receipt_id).result_payload
                == new_receipt
            )
            assert json.loads(
                session.get(LintFinding, new_golden_id).suggested_action
            ) == {
                "queries": [
                    {
                        "query": "New agent",
                        "expected_concept_path": f"agent_page:{agent_id}",
                    }
                ]
            }
            assert turn.retrieval_trace[2] == {
                "type": "context_page",
                "id": str(page_id),
                "concept_path": "/context/renamed-page.md",
                "title": "New hit",
            }
            assert session.get(AskTurn, new_turn_id).retrieval_trace == [
                {
                    "type": "library_item",
                    "id": str(library_id),
                    "concept_path": f"/library/{library_id}.md",
                }
            ]
        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(jobs)
                    .where(
                        jobs.c.idempotency_key.like("locator-index:%"),
                        jobs.c.status == "queued",
                    )
                )
                == 2
            )
            assert (
                connection.scalar(
                    sa.select(jobs.c.status).where(jobs.c.id == completed_job_id)
                )
                == "completed"
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
