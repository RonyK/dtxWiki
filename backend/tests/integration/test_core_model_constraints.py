from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.audit.emitter import emit_page_event
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.ask import AskSession, AskTurn, ContextIngressProposal
from backend.models.base import Base
from backend.models.bundle import AgentInputBundle, AgentInputBundleItem
from backend.models.context import (
    ContextCitation,
    ContextClaim,
    ContextPage,
    ContextPageVersion,
)
from backend.models.job import Job
from backend.models.library import (
    LibraryAttempt,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
)
from backend.models.library_discovery import (
    LibraryDiscoveryCandidate,
    LibraryDiscoveryRun,
)
from backend.models.mcp_trace import MCPToolCall
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import MCPClient, Workspace
from backend.scripts.seed_dev import (
    DEFAULT_AGENT_PAGE_SLUG,
    seed_default_workspace,
)
from backend.services.page_service import PageService
from backend.tests.utils.migrated_sqlite_templates import shared_templates

CORE_TABLES = {
    "agent_input_bundle",
    "agent_input_bundle_item",
    "agent_page",
    "agent_page_version",
    "audit_event",
    "change_operation",
    "change_set",
    "ask_session",
    "ask_turn",
    "context_citation",
    "context_claim",
    "context_ingress_proposal",
    "context_page",
    "context_page_version",
    "feedback_item",
    "job",
    "lint_finding",
    "lint_run",
    "mcp_client",
    "mcp_tool_call",
    "review_decision",
    "review_item",
    "library_artifact",
    "stored_blob",
    "blob_incarnation",
    # A-1: library_conversion_run/
    # attempt are now the unified library_run/library_attempt.
    "library_attempt",
    "library_run",
    "library_item",
    "library_discovery_candidate",
    "library_discovery_run",
    "library_sync_run",
    "library_item_version",
    "library_projection_outbox",
    "tree_policy",
    "workspace",
    "workspace_tree_node",
}


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


@pytest.fixture(scope="module")
def head_engine(sqlite_templates, tmp_path_factory):
    """One read-only migrated head schema for the schema-inspection tests."""
    database = tmp_path_factory.mktemp("core-schema-head") / "head.sqlite"
    engine = create_engine(sqlite_templates.clone("head", database))
    yield engine
    engine.dispose()


def _add_test_mcp_client(session, workspace: Workspace) -> MCPClient:
    client = MCPClient(
        workspace_id=workspace.id,
        name=f"schema-test-{uuid4()}",
        client_type="api_token",
        scopes=["workspace-read"],
        status="active",
    )
    session.add(client)
    session.flush()
    return client


def test_seed_creates_workspace_policy_without_an_mcp_client():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
    with session_factory.begin() as session:
        persisted_workspace = session.scalar(select(Workspace))
        clients = session.scalars(select(MCPClient)).all()

    assert workspace.slug == "default"
    assert persisted_workspace is not None
    assert persisted_workspace.name == "Default Workspace"
    assert persisted_workspace.goal
    assert persisted_workspace.visibility == "private"
    assert persisted_workspace.agent_injection_policy["bundle_ttl_default_seconds"]
    assert persisted_workspace.agent_injection_policy["bundle_ttl_max_seconds"]
    assert persisted_workspace.review_policy["preset"] == "balanced"
    assert UUID(str(persisted_workspace.owner_actor_id))
    assert clients == []


def test_mcp_client_name_is_unique_per_workspace():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add(
            MCPClient(
                workspace_id=workspace.id,
                name="duplicate-client-name",
                client_type="api_token",
                scopes=["workspace-read"],
                status="active",
            )
        )
        session.flush()
        session.add(
            MCPClient(
                workspace_id=workspace.id,
                name="duplicate-client-name",
                client_type="api_token",
                scopes=["workspace-read"],
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_job_idempotency_key_is_unique_per_workspace_and_type():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add(
            Job(
                workspace_id=workspace.id,
                job_type="library_discovery",
                trigger_type="manual",
                idempotency_key="scan-default",
            )
        )
        session.flush()
        session.add(
            Job(
                workspace_id=workspace.id,
                job_type="library_discovery",
                trigger_type="manual",
                idempotency_key="scan-default",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_accepted_candidate_requires_accepted_library_item_id():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        run = LibraryDiscoveryRun(
            workspace_id=workspace.id,
            policy_snapshot_hash="policy-1",
            run_token="run-1",
        )
        session.add(run)
        session.flush()
        session.add(
            LibraryDiscoveryCandidate(
                workspace_id=workspace.id,
                discovery_run_id=run.id,
                canonical_uri="https://example.com/source",
                connector_type="web",
                decision_status="accepted",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_library_multiformat_schema_has_provenance_ownership_and_claim_indexes(
    head_engine,
):
    engine = head_engine
    inspector = inspect(engine)

    # A-1: library_conversion_run/
    # attempt are the unified library_run/library_attempt now; their
    # run_id/attempt_id-facing columns are renamed conversion_run_id->run_id
    # and conversion_attempt_id->attempt_id to match (library_projection_
    # outbox's own conversion_attempt_id column name is unchanged -- it
    # still means "the conversion-stage attempt that produced this
    # projection work", just pointing at the unified table now).
    run_uniques = {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("library_run")
    }
    attempt_uniques = {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("library_attempt")
    }
    artifact_uniques = {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("library_artifact")
    }
    outbox_uniques = {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("library_projection_outbox")
    }
    version_fks = {
        tuple(foreign_key["constrained_columns"]): foreign_key
        for foreign_key in inspector.get_foreign_keys("library_item_version")
    }
    artifact_fks = {
        tuple(foreign_key["constrained_columns"]): foreign_key
        for foreign_key in inspector.get_foreign_keys("library_artifact")
    }

    assert {
        ("library_item_version_id", "idempotency_key"),
        ("library_item_version_id", "run_seq"),
    } <= run_uniques
    assert ("run_id", "chain_index") in attempt_uniques
    assert ("attempt_id", "role", "ordinal") in artifact_uniques
    assert (
        "conversion_attempt_id",
        "projection_kind",
        "generation",
    ) in outbox_uniques
    # The final 0015 table plans extend both 2-column ownership FKs to
    # 3-column workspace-scoped FKs -- same ownership shape, plus workspace_id
    # on both sides.
    assert version_fks[("workspace_id", "selected_conversion_attempt_id", "id")][
        "referred_columns"
    ] == ["workspace_id", "id", "library_item_version_id"]
    assert version_fks[
        ("source_blob_workspace_id", "source_blob_id", "source_blob_epoch")
    ]["referred_columns"] == ["workspace_id", "blob_id", "blob_epoch"]
    # A-1: library_artifact's ownership FK into its attempt is a 4-column
    # composite now (stage folded in, since attempt and artifact both carry
    # a stage discriminator on the unified tables) rather than 3.
    assert artifact_fks[
        ("workspace_id", "attempt_id", "library_item_version_id", "stage")
    ]["referred_columns"] == [
        "workspace_id",
        "id",
        "library_item_version_id",
        "stage",
    ]
    assert artifact_fks[("blob_workspace_id", "blob_id", "blob_epoch")][
        "referred_columns"
    ] == ["workspace_id", "blob_id", "blob_epoch"]
    assert {
        "idx_library_run_claim",
        "idx_library_run_version_status",
    } <= {index["name"] for index in inspector.get_indexes("library_run")}
    assert "idx_library_projection_outbox_claim" in {
        index["name"] for index in inspector.get_indexes("library_projection_outbox")
    }


def test_library_multiformat_models_reject_invalid_reference_and_attempt_ownership():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            uri="upload://ownership.md",
        )
        session.add(item)
        session.flush()
        invalid_reference = LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=item.id,
            version_no=99,
            content_hash="a" * 64,
            raw_storage_ref=None,
            source_blob_id=uuid4(),
        )
        session.add(invalid_reference)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        item = LibraryItem(
            workspace_id=workspace.id,
            connector_type="upload",
            uri="upload://ownership.md",
        )
        session.add(item)
        session.flush()
        first_version = LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=item.id,
            version_no=1,
            content_hash="first",
            raw_storage_ref="inline://first",
        )
        second_version = LibraryItemVersion(
            workspace_id=workspace.id,
            library_item_id=item.id,
            version_no=2,
            content_hash="second",
            raw_storage_ref="inline://second",
        )
        session.add_all([first_version, second_version])
        session.flush()
        run = LibraryRun(
            workspace_id=workspace.id,
            library_item_version_id=first_version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            provider_chain_json=[],
            policy_revision="policy-1",
            output_schema_version="1",
            idempotency_key="initial:first",
        )
        session.add(run)
        session.flush()
        attempt = LibraryAttempt(
            workspace_id=workspace.id,
            run_id=run.id,
            library_item_version_id=first_version.id,
            stage="conversion",
            provider_id="builtin.text",
            provider_version="1",
            provider_spi_version="1",
            config_hash="config-1",
            input_sha256="a" * 64,
            output_schema_version="1",
            chain_index=0,
        )
        session.add(attempt)
        session.flush()
        second_version.selected_conversion_attempt_id = attempt.id
        with pytest.raises(IntegrityError):
            session.flush()


def test_source_uri_is_unique_per_workspace():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add(
            LibraryItem(
                workspace_id=workspace.id,
                connector_type="web",
                uri="https://example.com/source",
            )
        )
        session.flush()
        session.add(
            LibraryItem(
                workspace_id=workspace.id,
                connector_type="web",
                uri="https://example.com/source",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_library_item_version_no_is_unique_per_source():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        source = LibraryItem(
            workspace_id=workspace.id,
            connector_type="web",
            uri="https://example.com/source",
        )
        session.add(source)
        session.flush()
        session.add(
            LibraryItemVersion(
                workspace_id=workspace.id,
                library_item_id=source.id,
                version_no=1,
                content_hash="hash-1",
                raw_storage_ref="raw/source/v1",
            )
        )
        session.flush()
        session.add(
            LibraryItemVersion(
                workspace_id=workspace.id,
                library_item_id=source.id,
                version_no=1,
                content_hash="hash-2",
                raw_storage_ref="raw/source/v1b",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_accepted_candidate_can_link_to_source():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        source = LibraryItem(
            workspace_id=workspace.id,
            connector_type="web",
            uri="https://example.com/source",
        )
        run = LibraryDiscoveryRun(
            workspace_id=workspace.id,
            policy_snapshot_hash="policy-1",
            run_token="run-1",
        )
        session.add_all([source, run])
        session.flush()
        candidate = LibraryDiscoveryCandidate(
            workspace_id=workspace.id,
            discovery_run_id=run.id,
            canonical_uri="https://example.com/source",
            connector_type="web",
            decision_status="accepted",
            accepted_library_item_id=source.id,
        )
        session.add(candidate)
        session.flush()

    assert candidate.accepted_library_item_id == source.id


def test_agent_page_slug_is_unique_per_workspace():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add(
            AgentPage(
                workspace_id=workspace.id,
                category="persona",
                title="Duplicate",
                slug=DEFAULT_AGENT_PAGE_SLUG,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_agent_page_version_no_is_unique_per_page():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        seed_default_workspace(session)
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        assert agent_page is not None
        session.add(
            AgentPageVersion(
                agent_page_id=agent_page.id,
                version_no=1,
                body_markdown="# Duplicate",
                actor_type="system",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_seed_creates_active_system_prompt_agent_page():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        seed_default_workspace(session)
    with session_factory.begin() as session:
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        active_version = session.get(
            AgentPageVersion,
            agent_page.current_active_version_id if agent_page else None,
        )

    assert agent_page is not None
    assert agent_page.category == "agent_guide"
    assert agent_page.lifecycle_status == "active"
    assert active_version is not None
    assert active_version.version_status == "active_snapshot"


def test_bundle_item_requires_exactly_one_source_fk():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        client = _add_test_mcp_client(session, workspace)
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        version = session.get(AgentPageVersion, agent_page.current_active_version_id)
        bundle = AgentInputBundle(
            workspace_id=workspace.id,
            client_id=client.id,
            task_intent="answer with default policy",
            token_budget=2048,
            ttl_seconds=3600,
            valid_until=datetime(2026, 5, 9, 16, tzinfo=UTC),
        )
        session.add(bundle)
        session.flush()
        session.add(
            AgentInputBundleItem(
                bundle_id=bundle.id,
                source_kind="agent_page",
                agent_page_version_id=version.id,
                context_page_version_id=version.id,
                included_reason="policy_always",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_bundle_item_cannot_be_included_and_omitted():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        client = _add_test_mcp_client(session, workspace)
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        version = session.get(AgentPageVersion, agent_page.current_active_version_id)
        bundle = AgentInputBundle(
            workspace_id=workspace.id,
            client_id=client.id,
            task_intent="answer with default policy",
            token_budget=2048,
            ttl_seconds=3600,
            valid_until=datetime(2026, 5, 9, 16, tzinfo=UTC),
        )
        session.add(bundle)
        session.flush()
        session.add(
            AgentInputBundleItem(
                bundle_id=bundle.id,
                source_kind="agent_page",
                agent_page_version_id=version.id,
                included_reason="policy_always",
                omission_reason="policy_disabled",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_page_service_populates_design_context_version_fields():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    service = PageService(session_factory)

    created = service.write_page(
        workspace_slug="default",
        slug="context-page",
        title="Context Page",
        body_md="# One",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    updated = service.write_page(
        workspace_slug="default",
        slug="context-page",
        title="Context Page",
        body_md="# Two",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        page = session.scalar(
            select(ContextPage).where(ContextPage.slug == "context-page")
        )
        versions = session.scalars(
            select(ContextPageVersion)
            .where(ContextPageVersion.page_id == page.id)
            .order_by(ContextPageVersion.version_no)
        ).all()

    assert page.current_published_version_id == updated.version_id
    assert [version.version_no for version in versions] == [1, 2]
    assert [version.body_md for version in versions] == ["# One", "# Two"]
    assert [version.version_status for version in versions] == [
        "superseded_snapshot",
        "published_snapshot",
    ]


def test_raw_source_citation_requires_library_item_version():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="context-page",
            title="Context Page",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace.id,
            page_id=page.id,
            version_no=1,
            body_md="# One",
            content_hash="hash-1",
            graph_status="linear",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="dev-local-user",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        claim = ContextClaim(
            workspace_id=workspace.id,
            page_version_id=version.id,
            claim_text="A source-backed claim",
        )
        session.add(claim)
        session.flush()
        session.add(
            ContextCitation(
                workspace_id=workspace.id,
                target_kind="context_claim",
                target_id=claim.id,
                citation_type="raw_source",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_mcp_request_id_is_unique_per_workspace():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        client = _add_test_mcp_client(session, workspace)
        request_id = uuid4()
        session.add(
            MCPToolCall(
                workspace_id=workspace.id,
                client_id=client.id,
                request_id=request_id,
                tool_name="write_draft",
                result_status="succeeded",
            )
        )
        session.flush()
        session.add(
            MCPToolCall(
                workspace_id=workspace.id,
                client_id=client.id,
                request_id=request_id,
                tool_name="write_draft",
                result_status="succeeded",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_audit_emitter_populates_legacy_and_design_columns():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        page = ContextPage(
            workspace_id=workspace.id,
            slug="audit-page",
            title="Audit Page",
        )
        session.add(page)
        session.flush()
        event = emit_page_event(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="dev-local-user",
            action="page.write",
            target_kind="context_page",
            target_id=page.id,
            payload={"slug": page.slug},
        )

    assert event.actor_kind == "human"
    assert event.actor_type == "human"
    assert event.target_kind == "context_page"
    assert event.target_type == "context_page"
    assert event.event_summary == "page.write"
    assert event.retention_class == "standard"
    assert event.payload == {"slug": "audit-page"}


def test_core_migration_replaces_duplicate_search_columns_with_fts5(head_engine):
    engine = head_engine
    inspector = inspect(engine)
    context_version_columns = {
        column["name"] for column in inspector.get_columns("context_page_version")
    }
    claim_columns = {
        column["name"] for column in inspector.get_columns("context_claim")
    }
    artifact_columns = {
        column["name"] for column in inspector.get_columns("library_artifact")
    }
    # A-1: index_text moved off
    # the common library_artifact row onto this 1:1 conversion-only
    # extension table (see test_migration_adds_library_item_version_
    # artifact above for the rest of that move).
    artifact_conversion_detail_columns = {
        column["name"]
        for column in inspector.get_columns("library_artifact_conversion_detail")
    }
    citation_columns = {
        column["name"] for column in inspector.get_columns("context_citation")
    }
    agent_page_columns = {
        column["name"] for column in inspector.get_columns("agent_page")
    }
    audit_columns = {column["name"] for column in inspector.get_columns("audit_event")}

    assert "search_doc" not in context_version_columns
    assert "search_doc" not in claim_columns
    assert "index_text" in artifact_conversion_detail_columns
    assert "search_doc" not in artifact_columns
    assert "search_doc" not in artifact_conversion_detail_columns
    assert "search_doc" not in citation_columns
    assert "search_doc" not in agent_page_columns
    assert "summary_search_doc" not in audit_columns

    table_names = set(inspector.get_table_names())
    assert CORE_TABLES.issubset(table_names)
    assert "search_doc_fts" in table_names
    assert "page_name_trgm" in table_names
    assert "knowledge_coverage_gap" in table_names
    assert "edge_projection_version" in {
        column["name"] for column in inspector.get_columns("workspace")
    }
    assert {
        "retrieval_trace",
        "tried_lanes",
        "fallback_disclosure",
    } <= {column["name"] for column in inspector.get_columns("ask_turn")}
    # The rewritten 0002 is now the complete Epic-1 schema.
    assert "email_verified_at" in {
        column["name"] for column in inspector.get_columns("users")
    }
    assert "password_reset_tokens" in table_names


def test_core_migration_accepts_runtime_precreated_fts_tables(
    tmp_path, sqlite_templates
):
    from backend.models.search_index import ensure_fts5_tables

    database_url = sqlite_templates.clone("base_0001", tmp_path / "precreated-fts.db")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        ensure_fts5_tables(connection)

    command.upgrade(config, "efcbcf724889")

    assert {"search_doc_fts", "page_name_trgm"} <= set(
        inspect(engine).get_table_names()
    )


def test_seed_creates_system_tree_roots_and_policies():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        seed_default_workspace(session)

    with session_factory.begin() as session:
        nodes = session.scalars(select(WorkspaceTreeNode)).all()
        policies = session.scalars(select(TreePolicy)).all()
        slugs = {node.slug for node in nodes}
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.slug == DEFAULT_AGENT_PAGE_SLUG)
        )
        placement = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "agent_page",
                WorkspaceTreeNode.target_id == agent_page.id,
            )
        )

    assert {"agent", "context", "library", "ask"}.issubset(slugs)
    assert {"agent_guide", "memory", "memory_candidates"}.issubset(slugs)
    assert {policy.section for policy in policies} == {
        "agent",
        "context",
        "library",
        "ask",
    }
    assert placement is not None
    assert placement.primary_placement is True


def test_tree_node_requires_target_for_entity_refs():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        session.add(
            WorkspaceTreeNode(
                workspace_id=workspace.id,
                section="context",
                node_kind="page_ref",
                sort_key="0001",
                title="Broken Page Ref",
                target_kind="context_page",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_primary_placement_is_unique_per_entity():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        target_id = uuid4()
        first = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="page_ref",
            sort_key="0001",
            title="Primary",
            target_kind="context_page",
            target_id=target_id,
        )
        duplicate = WorkspaceTreeNode(
            workspace_id=workspace.id,
            section="context",
            node_kind="page_ref",
            sort_key="0002",
            title="Duplicate",
            target_kind="context_page",
            target_id=target_id,
        )
        session.add_all([first, duplicate])
        with pytest.raises(IntegrityError):
            session.flush()


def test_ask_turn_no_is_unique_per_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        ask_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.slug == "ask",
            )
        )
        ask_session = AskSession(
            workspace_id=workspace.id,
            title="Daily capture",
            actor_kind="user",
            placement_tree_node_id=ask_root.id,
        )
        session.add(ask_session)
        session.flush()
        session.add_all(
            [
                AskTurn(ask_session_id=ask_session.id, turn_no=1),
                AskTurn(ask_session_id=ask_session.id, turn_no=1),
            ]
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_context_ingress_rejects_review_only_in_p0():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        context_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace.id,
                WorkspaceTreeNode.slug == "context",
            )
        )
        session.add(
            ContextIngressProposal(
                workspace_id=workspace.id,
                source_kind="ask_turn",
                proposed_context_page_type="overview",
                proposed_parent_tree_node_id=context_root.id,
                proposed_title="Review Only",
                proposed_save_mode="review_only",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
