import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, text

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.migrations.revision_modules.v0_1.efcbcf724889_core_schema import (
    search_schema,
)
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.graph import KnowledgeNodeStat
from backend.models.lint import LintFinding, LintRun
from backend.models.mcp_chat import (
    McpChatArtifactKind,
    McpChatArtifactLink,
    McpChatArtifactVerb,
    McpChatToolCallArtifact,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.workspace import MCPClient, Workspace
from backend.services.indexing.edge_registry import refresh_co_access_edge_projection
from backend.services.mcp_chat_service import McpChatService
from backend.services.trail_analysis import recompute_authority
from backend.tests.utils.migrated_sqlite_templates import shared_templates

NOW = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


def test_core_schema_upgrade_reclassifies_rows_from_the_initial_release(
    tmp_path: Path, sqlite_templates
):
    database_url = sqlite_templates.clone("base_0001", tmp_path / "search-match.db")
    config = Config(str(resolve_alembic_ini()))
    config.set_main_option("script_location", str(resolve_migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    engine = create_engine(database_url)
    workspace_id = uuid4()
    client_id = uuid4()
    chat_id = uuid4()
    call_id = uuid4()
    resource_id = uuid4()
    link_id = uuid4()
    edge_id = uuid4()
    actor_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspace (
                    slug, title, name, description, goal, default_language,
                    library_policy, write_policy, citation_policy,
                    agent_injection_policy, review_policy, lint_policy,
                    auto_fix_enabled, visibility, created_by_actor_id,
                    owner_actor_id, owner_user_id, updated_at, id, created_at
                ) VALUES (
                    'search-repair', 'Search repair', 'Search repair', NULL,
                    'Migration test', 'en', '{}', '{}', '{}', '{}', '{}', '{}',
                    0, 'private', :actor_id, :actor_id, NULL,
                    :now, :workspace_id, :now
                )
                """
            ),
            {
                "actor_id": actor_id.hex,
                "now": NOW,
                "workspace_id": workspace_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mcp_client (
                    workspace_id, name, client_type, scopes, status,
                    created_by, issued_to_user_id, brand_hint, last_used_at,
                    token_metadata, updated_at, id, created_at
                ) VALUES (
                    :workspace_id, 'Migration client', 'codex', '[]', 'active',
                    NULL, NULL, 'codex', NULL, '{}', :now, :client_id, :now
                )
                """
            ),
            {
                "workspace_id": workspace_id.hex,
                "now": NOW,
                "client_id": client_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mcp_chat_session (
                    workspace_id, client_id, owner_user_id, mcp_session_id,
                    started_at, last_activity_at, tool_call_count, write_count,
                    error_count, title, title_user_edited, summary,
                    frozen_summary, frozen_at, archived_at, id
                ) VALUES (
                    :workspace_id, :client_id, NULL, 'migration-chat',
                    :now, :now, 1, 0, 0, NULL, 0, NULL,
                    NULL, NULL, NULL, :chat_id
                )
                """
            ),
            {
                "workspace_id": workspace_id.hex,
                "client_id": client_id.hex,
                "now": NOW,
                "chat_id": chat_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mcp_tool_call (
                    client_id, workspace_id, mcp_session_id, request_id,
                    schema_version, tool_name, input_summary, result_status,
                    result_summary, warnings, error_code, audit_event_id,
                    change_set_id, agent_input_bundle_id, provider, model_id,
                    model_version, params_summary, client_self_reported,
                    cost_estimate, outcome, started_at, completed_at, latency_ms,
                    id
                ) VALUES (
                    :client_id, :workspace_id, :session_id, :request_id,
                    'llmwiki.mcp.v1', 'search', '{}', 'succeeded',
                    'Tool call succeeded.', '[]', NULL, NULL,
                    NULL, NULL, 'unknown', 'unknown', NULL, '{}', 0,
                    NULL, NULL, :now, :now, 1, :call_id
                )
                """
            ),
            {
                "client_id": client_id.hex,
                "workspace_id": workspace_id.hex,
                "session_id": uuid4().hex,
                "request_id": uuid4().hex,
                "now": NOW,
                "call_id": call_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mcp_chat_artifact_link (
                    chat_session_id, resource_kind, resource_id, verb,
                    first_occurred_at, last_occurred_at, occurrence_count,
                    last_mcp_tool_call_id, id
                ) VALUES (
                    :chat_id, 'context_page', :resource_id, 'referenced',
                    :now, :now, 1, :call_id, :link_id
                )
                """
            ),
            {
                "chat_id": chat_id.hex,
                "resource_id": resource_id.hex,
                "now": NOW,
                "call_id": call_id.hex,
                "link_id": link_id.hex,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO mcp_chat_tool_call_artifact (
                    chat_session_id, mcp_tool_call_id, resource_kind,
                    resource_id, verb, occurred_at, id
                ) VALUES (
                    :chat_id, :call_id, 'context_page', :resource_id,
                    'referenced', :now, :edge_id
                )
                """
            ),
            {
                "chat_id": chat_id.hex,
                "call_id": call_id.hex,
                "resource_id": resource_id.hex,
                "now": NOW,
                "edge_id": edge_id.hex,
            },
        )

    command.upgrade(config, "efcbcf724889")

    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT verb, occurrence_count FROM mcp_chat_artifact_link "
                "WHERE chat_session_id = :chat_id"
            ),
            {"chat_id": chat_id.hex},
        ).all() == [("search_match", 1)]
        assert (
            connection.scalar(
                text(
                    "SELECT verb FROM mcp_chat_tool_call_artifact WHERE id = :edge_id"
                ),
                {"edge_id": edge_id.hex},
            )
            == "search_match"
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "efcbcf724889"
        )
    engine.dispose()


def test_reclassification_splits_search_from_a_real_versioned_read(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        page_a, version_a = _page_with_version(session, workspace.id, "repair-a")
        page_b, _version_b = _page_with_version(session, workspace.id, "repair-b")
        conversation_id = uuid4()
        search_call, chat = _add_call(
            session,
            service,
            workspace.id,
            client.id,
            conversation_id,
            "search",
            NOW,
        )
        for page in (page_a, page_b):
            service.link_artifact(
                session=session,
                chat_session=chat,
                tool_call=search_call,
                resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                resource_id=page.id,
                verb=McpChatArtifactVerb.REFERENCED,
            )
        read_call, chat = _add_call(
            session,
            service,
            workspace.id,
            client.id,
            conversation_id,
            "read",
            NOW + timedelta(minutes=1),
        )
        service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=read_call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page_a.id,
            verb=McpChatArtifactVerb.REFERENCED,
            version_id=version_a.id,
            root_hash="read-root-a",
        )
        chat_id = chat.id
        page_a_id = page_a.id
        page_b_id = page_b.id
        version_a_id = version_a.id

    result = _run_reclassification(session_factory)

    assert result == {
        "per_call_links": 2,
        "rollup_groups": 2,
        "workspaces": 1,
        "dismissed_hot_paths": 0,
    }
    with session_factory() as session:
        links = _rollups(session, chat_id)
        assert set(links) == {
            (page_a_id, "referenced"),
            (page_a_id, "search_match"),
            (page_b_id, "search_match"),
        }
        read_link = links[(page_a_id, "referenced")]
        assert read_link.occurrence_count == 1
        assert read_link.last_mcp_tool_call_id == read_call.id
        assert read_link.last_read_version_id == version_a_id
        assert read_link.last_read_root_hash == "read-root-a"
        assert read_link.last_read_at == read_call.completed_at
        assert links[(page_a_id, "search_match")].occurrence_count == 1
        assert links[(page_b_id, "search_match")].occurrence_count == 1
        assert set(
            session.scalars(
                select(McpChatToolCallArtifact.verb).where(
                    McpChatToolCallArtifact.chat_session_id == chat_id
                )
            )
        ) == {"referenced", "search_match"}

    first_snapshot = _full_reclassification_snapshot(session_factory)
    second_result = _run_reclassification(session_factory)
    second_snapshot = _full_reclassification_snapshot(session_factory)
    assert second_result == {
        "per_call_links": 0,
        "rollup_groups": 0,
        "workspaces": 0,
        "dismissed_hot_paths": 0,
    }
    assert second_snapshot == first_snapshot


def test_reclassification_subtracts_from_repaired_rollup_count(session_factory):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        page, _version = _page_with_version(session, workspace.id, "repair-count")
        call, chat = _add_call(
            session,
            service,
            workspace.id,
            client.id,
            uuid4(),
            "search",
            NOW,
        )
        referenced = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.REFERENCED,
        )
        referenced.occurrence_count = 5
        chat_id = chat.id
        page_id = page.id

    _run_reclassification(session_factory)

    with session_factory() as session:
        links = _rollups(session, chat_id)
        assert links[(page_id, "referenced")].occurrence_count == 4
        assert links[(page_id, "search_match")].occurrence_count == 1


def test_reclassification_preserves_a_read_ledger_without_a_remaining_edge(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        page, version = _page_with_version(session, workspace.id, "repair-ledger")
        call, chat = _add_call(
            session,
            service,
            workspace.id,
            client.id,
            uuid4(),
            "search",
            NOW,
        )
        referenced = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.REFERENCED,
            version_id=version.id,
            root_hash="ledger-root",
        )
        chat_id = chat.id
        page_id = page.id
        ledger = (
            referenced.last_read_version_id,
            referenced.last_read_root_hash,
            referenced.last_read_at,
        )

    _run_reclassification(session_factory)

    with session_factory() as session:
        links = _rollups(session, chat_id)
        assert links[(page_id, "referenced")].occurrence_count == 1
        assert (
            links[(page_id, "referenced")].last_read_version_id,
            links[(page_id, "referenced")].last_read_root_hash,
            links[(page_id, "referenced")].last_read_at,
        ) == ledger
        assert links[(page_id, "search_match")].occurrence_count == 1


def test_reclassification_does_not_guess_for_a_rollup_without_per_call_provenance(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        page, _version = _page_with_version(session, workspace.id, "repair-unknown")
        call, chat = _add_call(
            session,
            service,
            workspace.id,
            client.id,
            uuid4(),
            "search",
            NOW,
        )
        link = service.link_artifact(
            session=session,
            chat_session=chat,
            tool_call=call,
            resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
            resource_id=page.id,
            verb=McpChatArtifactVerb.REFERENCED,
        )
        session.execute(
            delete(McpChatToolCallArtifact).where(
                McpChatToolCallArtifact.chat_session_id == chat.id
            )
        )
        link_id = link.id

    before = _table_snapshot(session_factory, McpChatArtifactLink)
    result = _run_reclassification(session_factory)
    after = _table_snapshot(session_factory, McpChatArtifactLink)

    assert result["per_call_links"] == 0
    assert after == before
    assert any(row["id"] == link_id and row["verb"] == "referenced" for row in after)
    assert all(row["verb"] != "search_match" for row in after)


def test_reclassification_refreshes_authority_and_dismisses_invalid_hot_path(
    session_factory,
):
    service = McpChatService()
    with session_factory.begin() as session:
        workspace, client = _workspace_and_client(session)
        page_a, _version_a = _page_with_version(session, workspace.id, "repair-hot-a")
        page_b, _version_b = _page_with_version(session, workspace.id, "repair-hot-b")
        page_c, _version_c = _page_with_version(session, workspace.id, "repair-hot-c")
        for index in range(5):
            call, chat = _add_call(
                session,
                service,
                workspace.id,
                client.id,
                uuid4(),
                "search",
                NOW + timedelta(minutes=index),
            )
            for page in (page_a, page_b):
                service.link_artifact(
                    session=session,
                    chat_session=chat,
                    tool_call=call,
                    resource_kind=McpChatArtifactKind.CONTEXT_PAGE,
                    resource_id=page.id,
                    verb=McpChatArtifactVerb.REFERENCED,
                )
        session.add(
            EdgeProjection(
                workspace_id=workspace.id,
                source_kind="context_page",
                source_id=page_a.id,
                target_kind="context_page",
                target_id=page_c.id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                occurred_at=NOW,
                source_ref={"table": "test"},
                last_seen_at=NOW,
            )
        )
        refresh_co_access_edge_projection(session, workspace.id, now=NOW)
        recompute_authority(session, workspace.id, now=NOW)
        run = LintRun(
            workspace_id=workspace.id,
            lint_scope={"kind": "maintenance"},
            trigger_type="trail_tick",
            status="completed",
            finding_count_by_severity={"info": 1},
            started_at=NOW,
            completed_at=NOW,
        )
        session.add(run)
        session.flush()
        ordered = sorted((page_a.id, page_b.id), key=str)
        finding = LintFinding(
            lint_run_id=run.id,
            workspace_id=workspace.id,
            target_type="context_page",
            target_id=page_a.id,
            rule_id="kb.hot_path_unmaterialized",
            severity="info",
            status="open",
            suggested_action=json.dumps({"proposal": "add_cross_link"}),
            fingerprint=f"hot-path:{ordered[0]}:{ordered[1]}",
        )
        session.add(finding)
        session.flush()
        assert session.scalar(
            select(KnowledgeNodeStat.in_degree).where(
                KnowledgeNodeStat.workspace_id == workspace.id,
                KnowledgeNodeStat.entity_id == page_a.id,
            )
        ) in {0, 1}
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.workspace_id == workspace.id,
                    EdgeProjection.edge_kind == "co_access",
                )
            )
            is not None
        )
        workspace_id = workspace.id
        page_a_id = page_a.id
        finding_id = finding.id

    result = _run_reclassification(session_factory)

    assert result["dismissed_hot_paths"] == 1
    with session_factory() as session:
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.workspace_id == workspace_id,
                    EdgeProjection.edge_kind == "co_access",
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(KnowledgeNodeStat.in_degree).where(
                    KnowledgeNodeStat.workspace_id == workspace_id,
                    KnowledgeNodeStat.entity_id == page_a_id,
                )
            )
            == 0
        )
        repaired_finding = session.get(LintFinding, finding_id)
        assert repaired_finding.status == "dismissed"
        assert repaired_finding.resolved_at == NOW
        assert json.loads(repaired_finding.suggested_action) == {
            "dismissed_by": "search_match_reclassification",
            "proposal": "add_cross_link",
        }


def _workspace_and_client(session):
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    client = session.scalar(
        select(MCPClient).where(MCPClient.workspace_id == workspace.id)
    )
    return workspace, client


def _page_with_version(session, workspace_id: UUID, slug: str):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=slug.replace("-", " ").title(),
        page_type="overview",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {page.title}",
        content_hash=f"hash-{slug}",
        actor_type="agent",
        version_status="draft",
        created_by_actor_kind="system",
        created_by_actor_id="migration-test",
    )
    session.add(version)
    session.flush()
    return page, version


def _add_call(
    session,
    service: McpChatService,
    workspace_id: UUID,
    client_id: UUID,
    conversation_id: UUID,
    tool_name: str,
    occurred_at: datetime,
):
    call = MCPToolCall(
        client_id=client_id,
        workspace_id=workspace_id,
        mcp_session_id=conversation_id,
        request_id=uuid4(),
        tool_name=tool_name,
        input_summary={"query": "repair"},
        result_status="succeeded",
        result_summary="Tool call succeeded.",
        warnings=[],
        provider="unknown",
        model_id="unknown",
        params_summary={},
        client_self_reported=False,
        started_at=occurred_at,
        completed_at=occurred_at,
    )
    session.add(call)
    session.flush()
    chat = service.upsert_session_from_tool_call(
        session=session,
        workspace_id=workspace_id,
        client_id=client_id,
        mcp_session_id=str(conversation_id),
        tool_call=call,
    )
    return call, chat


def _run_reclassification(session_factory):
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        return search_schema.reclassify_search_matches(connection, now=NOW)


def _rollups(session, chat_id: UUID):
    return {
        (row.resource_id, row.verb): row
        for row in session.scalars(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.chat_session_id == chat_id
            )
        )
    }


def _full_reclassification_snapshot(session_factory):
    with session_factory() as session:
        workspace_versions = [
            {
                "id": workspace.id,
                "edge_projection_version": workspace.edge_projection_version,
                "authority_projection_version": workspace.authority_projection_version,
            }
            for workspace in session.scalars(select(Workspace)).all()
        ]
    return {
        "rollups": _table_snapshot(session_factory, McpChatArtifactLink),
        "per_call": _table_snapshot(session_factory, McpChatToolCallArtifact),
        "workspace_versions": workspace_versions,
    }


def _table_snapshot(session_factory, model):
    with session_factory() as session:
        rows = session.execute(select(*model.__table__.c)).mappings().all()
        return sorted((dict(row) for row in rows), key=lambda row: str(row["id"]))
