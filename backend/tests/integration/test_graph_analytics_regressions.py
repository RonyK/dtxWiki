import json
import os
import struct
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, insert, select

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.chunk import ChunkEmbedding, ContentChunk
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.graph import KnowledgeNodeStat
from backend.models.library import LibraryItem
from backend.models.lint import LintFinding, LintRun
from backend.models.mcp_chat import McpChatArtifactLink, McpChatSession
from backend.models.mcp_trace import MCPToolCall
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.schemas.read import GraphNeighborEntry, GraphNeighborsPayload
from backend.services import graph_ppr
from backend.services.eval.harness import (
    LaneMetrics,
    ModeMetrics,
    compare_baseline,
)
from backend.services.graph_cache import bump_edge_version, edge_version
from backend.services.graph_ppr import _frontier_edges, _subgraph_cache, subgraph_ppr
from backend.services.graph_service import GraphService
from backend.services.indexing.edge_registry import refresh_co_access_edge_projection
from backend.services.librarian import _stage_misplaced_pages, run_librarian_tick
from backend.services.page_service import PageService
from backend.services.search_service import SearchService
from backend.services.trail_analysis import (
    _authority_stat_rows,
    list_hot_concepts,
    recompute_authority,
    run_trail_tick,
)

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _elapsed(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


@contextmanager
def _captured_statements(session):
    """Every statement the engine runs inside the block, with its parameters."""

    statements: list[tuple[str, tuple]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _bound_limit(statement: str, parameters: tuple) -> int | None:
    """The LIMIT bound into a captured SQLite statement, or None without one."""

    if statement.rstrip().endswith("LIMIT ? OFFSET ?"):
        return parameters[-2]
    return None


def _publish(service: PageService, slug: str, title: str):
    return service.write_page(
        workspace_slug="default",
        slug=slug,
        title=title,
        body_md=f"# {title}\n\nBody.",
        actor_kind="human",
        actor_id="sprint6-c2",
        actor_provenance="browser_session",
    )


def _actor(role="viewer"):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _seed_chat_pair(
    session,
    workspace_id,
    left_id,
    right_id,
    *,
    occurred_at,
    verbs=("referenced", "referenced"),
):
    client = MCPClient(
        workspace_id=workspace_id,
        name=f"c2-{uuid4()}",
        client_type="generic",
        scopes=[],
        status="active",
    )
    session.add(client)
    session.flush()
    tool_call = MCPToolCall(
        client_id=client.id,
        workspace_id=workspace_id,
        request_id=uuid4(),
        tool_name="read",
        result_status="success",
    )
    session.add(tool_call)
    session.flush()
    chat = McpChatSession(
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=str(uuid4()),
        started_at=occurred_at,
        last_activity_at=occurred_at,
    )
    session.add(chat)
    session.flush()
    for page_id, verb in zip((left_id, right_id), verbs, strict=True):
        session.add(
            McpChatArtifactLink(
                workspace_id=chat.workspace_id,
                chat_session_id=chat.id,
                resource_kind="context_page",
                resource_id=page_id,
                verb=verb,
                first_occurred_at=occurred_at,
                last_occurred_at=occurred_at,
                occurrence_count=1,
                last_mcp_tool_call_id=tool_call.id,
            )
        )


def _seed_chat_artifacts(
    session, workspace_id, artifacts, *, occurred_at, verb="referenced"
):
    client = MCPClient(
        workspace_id=workspace_id,
        name=f"c2-many-{uuid4()}",
        client_type="generic",
        scopes=[],
        status="active",
    )
    session.add(client)
    session.flush()
    tool_call = MCPToolCall(
        client_id=client.id,
        workspace_id=workspace_id,
        request_id=uuid4(),
        tool_name="read",
        result_status="success",
    )
    chat = McpChatSession(
        workspace_id=workspace_id,
        client_id=client.id,
        mcp_session_id=str(uuid4()),
        started_at=occurred_at,
        last_activity_at=occurred_at,
    )
    session.add_all((tool_call, chat))
    session.flush()
    for page_id, count in artifacts:
        session.add(
            McpChatArtifactLink(
                workspace_id=chat.workspace_id,
                chat_session_id=chat.id,
                resource_kind="context_page",
                resource_id=page_id,
                verb=verb,
                first_occurred_at=occurred_at,
                last_occurred_at=occurred_at,
                occurrence_count=count,
                last_mcp_tool_call_id=tool_call.id,
            )
        )


def test_co_access_rebuild_decays_and_discards_session_identity(
    session_factory, page_service
):
    left = _publish(page_service, "co-left", "Co Left")
    right = _publish(page_service, "co-right", "Co Right")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_pair(session, workspace_id, left.id, right.id, occurred_at=now)
        _seed_chat_pair(
            session,
            workspace_id,
            left.id,
            right.id,
            occurred_at=now - timedelta(days=30),
        )
        before = edge_version(session, workspace_id)
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1
        edge = session.scalar(
            select(EdgeProjection).where(EdgeProjection.edge_kind == "co_access")
        )
        assert edge is not None
        assert edge.weight == pytest.approx(1.5, rel=0.02)
        assert edge.provenance == "usage"
        assert edge.source_ref == {"table": "mcp_chat_artifact_link"}
        assert "session" not in json.dumps(edge.source_ref).lower()
        assert edge_version(session, workspace_id) > before


def test_co_access_caps_each_session_to_top_50_artifacts(session_factory):
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        artifacts = [(uuid4(), count) for count in range(1, 61)]
        _seed_chat_artifacts(
            session,
            workspace_id,
            artifacts,
            occurred_at=now,
        )
        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1225
        edge_ids = set(
            session.scalars(
                select(EdgeProjection.source_id).where(
                    EdgeProjection.edge_kind == "co_access"
                )
            ).all()
        ) | set(
            session.scalars(
                select(EdgeProjection.target_id).where(
                    EdgeProjection.edge_kind == "co_access"
                )
            ).all()
        )

    assert {page_id for page_id, count in artifacts if count <= 10}.isdisjoint(edge_ids)


def test_co_access_canonicalizes_pairs_across_reversed_session_rankings(
    session_factory,
):
    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_artifacts(
            session,
            workspace_id,
            [(left_id, 2), (right_id, 1)],
            occurred_at=now,
        )
        _seed_chat_artifacts(
            session,
            workspace_id,
            [(left_id, 1), (right_id, 2)],
            occurred_at=now,
        )

        assert refresh_co_access_edge_projection(session, workspace_id, now=now) == 1
        edges = session.scalars(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.edge_kind == "co_access",
            )
        ).all()

    assert len(edges) == 1
    assert {edges[0].source_id, edges[0].target_id} == {left_id, right_id}
    assert edges[0].weight == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("verbs", "expected_edges"),
    [
        (("search_match", "search_match"), 0),
        (("referenced", "referenced"), 1),
        (("referenced", "search_match"), 0),
    ],
)
def test_co_access_requires_both_artifacts_to_be_consumed(
    session_factory, verbs, expected_edges
):
    now = datetime.now(UTC)
    left_id, right_id = uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_pair(
            session,
            workspace_id,
            left_id,
            right_id,
            occurred_at=now,
            verbs=verbs,
        )

        assert (
            refresh_co_access_edge_projection(session, workspace_id, now=now)
            == expected_edges
        )


@pytest.mark.parametrize(
    ("verb", "expected_findings"),
    [("search_match", 0), ("referenced", 1)],
)
def test_trail_tick_stages_only_consumed_unlinked_repeated_distant_paths(
    session_factory, page_service, verb, expected_findings
):
    left = _publish(page_service, "trail-left", "Trail Left")
    right = _publish(page_service, "trail-right", "Trail Right")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        placements = {
            node.target_id: node
            for node in session.scalars(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.target_id.in_((left.id, right.id)),
                )
            ).all()
        }
        for side, page_id in (("left", left.id), ("right", right.id)):
            parent = placements[page_id].parent_id
            for depth in range(2):
                folder = WorkspaceTreeNode(
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=parent,
                    sort_key=f"{side}-{depth}",
                    title=f"{side} {depth}",
                    slug=f"{side}-{depth}-{uuid4()}",
                    is_system_node=False,
                    can_have_children=True,
                    primary_placement=False,
                    visibility_status="visible",
                )
                session.add(folder)
                session.flush()
                parent = folder.id
            placements[page_id].parent_id = parent
        for _ in range(5):
            _seed_chat_pair(
                session,
                workspace_id,
                left.id,
                right.id,
                occurred_at=now,
                verbs=(verb, verb),
            )
        result = run_trail_tick(session, workspace_id, now=now)
        assert result["hot_path_findings"] == expected_findings
        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.hot_path_unmaterialized"
            )
        )
        if expected_findings:
            assert finding is not None
            assert json.loads(finding.suggested_action)["session_count"] == 5
        else:
            assert finding is None


def test_hot_concepts_require_consumption_not_search_exposure(
    session_factory, page_service
):
    page = _publish(page_service, "hot-exposure", "Hot Exposure")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _seed_chat_artifacts(
            session,
            workspace_id,
            [(page.id, 5)],
            occurred_at=now,
            verb="search_match",
        )
        assert list_hot_concepts(
            session, workspace_id, actor=_actor(), now=now
        ) == []

        link = session.scalar(
            select(McpChatArtifactLink).where(
                McpChatArtifactLink.resource_id == page.id
            )
        )
        assert link is not None
        link.verb = "referenced"
        session.flush()
        hot = list_hot_concepts(session, workspace_id, actor=_actor(), now=now)
        assert [item["id"] for item in hot] == [page.id]


def test_hot_concepts_batch_hydrate_and_enforce_actor_visibility(
    session_factory, page_service
):
    visible = _publish(page_service, "hot-visible", "Hot Visible")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        draft = ContextPage(
            workspace_id=workspace_id,
            slug="hot-draft",
            title="Hot Draft",
            page_type="concept",
            lifecycle_status="draft",
        )
        restricted = LibraryItem(
            workspace_id=workspace_id,
            connector_type="manual",
            format_hint="markdown",
            uri="https://example.com/hot-restricted",
            title="Hot Restricted",
            trust_level="restricted",
            lifecycle_status="registered",
        )
        sensitive = AgentPage(
            workspace_id=workspace_id,
            category="memory",
            title="Hot Sensitive",
            slug="hot-sensitive",
            lifecycle_status="active",
            sensitivity="private",
        )
        session.add_all((draft, restricted, sensitive))
        session.flush()
        sensitive_version = AgentPageVersion(
            workspace_id=sensitive.workspace_id,
            agent_page_id=sensitive.id,
            version_no=1,
            body_markdown="# Sensitive",
            frontmatter={},
            actor_type="human",
            version_status="active",
        )
        session.add(sensitive_version)
        session.flush()
        sensitive.current_active_version_id = sensitive_version.id

        client = MCPClient(
            workspace_id=workspace_id,
            name=f"hot-{uuid4()}",
            client_type="generic",
            scopes=[],
            status="active",
        )
        session.add(client)
        session.flush()
        tool_call = MCPToolCall(
            client_id=client.id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name="search",
            result_status="success",
        )
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client.id,
            mcp_session_id=str(uuid4()),
            started_at=now,
            last_activity_at=now,
        )
        session.add_all((tool_call, chat))
        session.flush()
        for kind, entity_id, count in (
            ("context_page", draft.id, 100),
            ("library_item", restricted.id, 90),
            ("agent_page", sensitive.id, 80),
            ("context_page", visible.id, 70),
        ):
            session.add(
                McpChatArtifactLink(
                    workspace_id=chat.workspace_id,
                    chat_session_id=chat.id,
                    resource_kind=kind,
                    resource_id=entity_id,
                    verb="referenced",
                    first_occurred_at=now,
                    last_occurred_at=now,
                    occurrence_count=count,
                    last_mcp_tool_call_id=tool_call.id,
                )
            )
        session.flush()

        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_query)
        try:
            viewer = list_hot_concepts(
                session, workspace_id, actor=_actor("viewer"), now=now
            )
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", count_query)
        owner = list_hot_concepts(session, workspace_id, actor=_actor("owner"), now=now)

    assert {item["id"] for item in viewer} == {visible.id}
    assert {item["id"] for item in owner} == {
        visible.id,
        restricted.id,
        sensitive.id,
    }
    assert query_count <= 4


def test_ppr_visibility_and_authority_projection(session_factory, page_service):
    source = _publish(page_service, "ppr-source", "PPR Source")
    visible = _publish(page_service, "ppr-visible", "PPR Visible")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hidden = ContextPage(
            workspace_id=workspace_id,
            slug="ppr-hidden",
            title="PPR Hidden",
            page_type="concept",
            lifecycle_status="draft",
        )
        session.add(hidden)
        session.flush()
        for target, weight in ((visible.id, 2.0), (hidden.id, 10.0)):
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=source.id,
                    target_kind="context_page",
                    target_id=target,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=weight,
                    source_ref={},
                )
            )
        assert recompute_authority(session, workspace_id) >= 3
        stats = session.scalars(
            select(KnowledgeNodeStat).where(
                KnowledgeNodeStat.workspace_id == workspace_id
            )
        ).all()
        assert len(stats) >= 3
        results = GraphService().ppr(
            session,
            workspace_id,
            seeds=[("context_page", source.id, 1.0)],
            actor=_actor("viewer"),
        )
        assert visible.id in {result.id for result in results}
        assert hidden.id not in {result.id for result in results}


def test_associative_search_lane_is_opt_in_and_keeps_exact_hit_first(
    session_factory, page_service
):
    source = _publish(page_service, "assoc-source", "Associative Source")
    related = _publish(page_service, "assoc-related", "Associative Related")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=related.id,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        session.flush()
        off = SearchService().search(
            session,
            workspace_id,
            "Associative Source",
            actor=_actor("viewer"),
        )
        on = SearchService(associative_lane=True).search(
            session,
            workspace_id,
            "Associative Source",
            actor=_actor("viewer"),
        )
        assert "associative" not in off.tried_lanes
        assert on.ranked_results[0].id == source.id
        assert any(
            result.id == related.id and result.match_reason == "associative"
            for result in on.ranked_results
        )
        assert "associative" in on.tried_lanes


def test_ppr_cache_uses_database_revision_across_sessions(session_factory):
    seed, first_target, second_target = uuid4(), uuid4(), uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed,
                target_kind="context_page",
                target_id=first_target,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        bump_edge_version(session, workspace_id)

    with session_factory() as session:
        first = subgraph_ppr(session, workspace_id, [("context_page", seed, 1.0)])
        assert first_target in {result.entity_id for result in first}

    with session_factory.begin() as session:
        session.execute(
            delete(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        )
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed,
                target_kind="context_page",
                target_id=second_target,
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        bump_edge_version(session, workspace_id)

    with session_factory() as session:
        second = subgraph_ppr(session, workspace_id, [("context_page", seed, 1.0)])
        ids = {result.entity_id for result in second}
        assert second_target in ids
        assert first_target not in ids


def test_ppr_frontier_reserves_capacity_for_incoming_edges(session_factory):
    seed = uuid4()
    incoming_id = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        outgoing = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=seed,
                target_kind="context_page",
                target_id=uuid4(),
                edge_kind="wikilink",
                provenance="curated",
                weight=10.0 - index,
                source_ref={},
            )
            for index in range(8)
        ]
        incoming = EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=incoming_id,
            target_kind="context_page",
            target_id=seed,
            edge_kind="wikilink",
            provenance="curated",
            weight=0.1,
            source_ref={},
        )
        session.add_all([*outgoing, incoming])
        session.flush()

        rows = _frontier_edges(
            session,
            workspace_id,
            {("context_page", seed)},
            limit=4,
        )

    assert len(rows) == 4
    assert incoming.id in {row.id for row in rows}
    assert {row.id for row in outgoing} & {row.id for row in rows}


def test_ppr_frontier_reservation_is_not_spent_by_intra_frontier_edges(
    session_factory,
):
    """An A->B edge inside the frontier tops BOTH directional batches. Reserving a
    positional slice let it consume the incoming quota while adding no incoming
    edge, starving true-incoming edges on exactly the mutually-linked seeds
    associative search produces."""

    left = uuid4()
    right = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        shared = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=left,
                target_kind="context_page",
                target_id=right,
                edge_kind=edge_kind,
                provenance="curated",
                weight=100.0 - index,
                source_ref={},
            )
            for index, edge_kind in enumerate(
                (
                    "wikilink",
                    "tree",
                    "citation_page",
                    "citation_source",
                    "promoted_from",
                )
            )
        ]
        outgoing = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=left,
                target_kind="context_page",
                target_id=uuid4(),
                edge_kind="wikilink",
                provenance="curated",
                weight=90.0 - index,
                source_ref={},
            )
            for index in range(10)
        ]
        incoming = [
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=uuid4(),
                target_kind="context_page",
                target_id=right,
                edge_kind="wikilink",
                provenance="curated",
                weight=10.0 - index,
                source_ref={},
            )
            for index in range(10)
        ]
        session.add_all([*shared, *outgoing, *incoming])
        session.flush()

        rows = _frontier_edges(
            session,
            workspace_id,
            {("context_page", left), ("context_page", right)},
            limit=10,
        )
        selected_ids = {row.id for row in rows}
        incoming_ids = {edge.id for edge in incoming}

    assert len(rows) == 10
    assert selected_ids & incoming_ids, "true-incoming edges were starved"


def test_ppr_ties_use_context_trust_and_cut_usage_after_curated(
    session_factory, page_service
):
    source = _publish(page_service, "trust-source", "Trust Source")
    human = _publish(page_service, "trust-human", "Trust Human")
    agent = _publish(page_service, "trust-agent", "Trust Agent")
    library_node = uuid4()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.execute(
            delete(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        )
        agent_page = session.get(ContextPage, agent.id)
        agent_version = session.get(
            ContextPageVersion, agent_page.current_published_version_id
        )
        agent_version.created_by_actor_kind = "agent"
        for kind, target in (
            ("context_page", human.id),
            ("context_page", agent.id),
            ("library_item", library_node),
        ):
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=source.id,
                    target_kind=kind,
                    target_id=target,
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=1.0,
                    source_ref={},
                )
            )
        usage_target = uuid4()
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=usage_target,
                edge_kind="co_access",
                provenance="usage",
                weight=100.0,
                source_ref={},
            )
        )
        bump_edge_version(session, workspace_id)
        results = subgraph_ppr(
            session,
            workspace_id,
            [("context_page", source.id, 1.0)],
            top_k=10,
            node_budget=4,
        )

    by_id = {result.entity_id: result for result in results}
    assert by_id[human.id].trust_prior > by_id[agent.id].trust_prior
    assert by_id[library_node].trust_prior == 0
    assert usage_target not in by_id


def test_mermaid_projection_escapes_labels_and_is_opt_in():
    neighbor = GraphNeighborEntry(
        kind="context_page",
        id=uuid4(),
        title='Injected "]\nclick evil',
        path="/context/target.md",
        edge_kind="wikilink",
        direction="out",
    )
    payload = GraphNeighborsPayload(outbound=[neighbor])
    rendered = GraphService.render_context_page_mermaid('Seed "]', payload)
    assert rendered.startswith("graph LR")
    # The frontend renders with Mermaid securityLevel=strict. Its click
    # directives fail parsing there, so the server emits the planned static
    # fallback and leaves navigation to the structured neighbor lists.
    assert "\n  click " not in rendered
    assert '\nclick evil"]' not in rendered
    assert "#quot;" in rendered
    assert "\\]" not in rendered
    assert payload.mermaid is None


def _add_embedding(session, workspace_id, page_id, vector):
    chunk = ContentChunk(
        workspace_id=workspace_id,
        document_kind="context_page",
        document_id=page_id,
        content_scope="published",
        ordinal=0,
        char_start=0,
        char_end=4,
        chunk_text="Body",
        chunk_text_hash=str(uuid4()),
        token_count=1,
    )
    session.add(chunk)
    session.flush()
    session.add(
        ChunkEmbedding(
            workspace_id=chunk.workspace_id,
            chunk_id=chunk.id,
            provider="deterministic",
            model="test",
            dim=len(vector),
            embedding=struct.pack(f"<{len(vector)}f", *vector),
        )
    )


def test_librarian_uses_existing_embeddings_and_stages_duplicate(
    session_factory, page_service
):
    left = _publish(page_service, "lib-left", "Librarian Left")
    right = _publish(page_service, "lib-right", "Librarian Right")
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _add_embedding(session, workspace_id, left.id, [1.0, 0.0])
        _add_embedding(session, workspace_id, right.id, [0.99, 0.01])
        result = run_librarian_tick(
            session,
            workspace_id,
            embedding_provider="deterministic",
        )
        assert result["near_duplicate_findings"] == 1
        assert session.scalar(
            select(LintFinding).where(
                LintFinding.rule_id == "kb.semantic_near_duplicate"
            )
        )
        assert session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.task_type == "near_duplicate_verdict"
            )
        )
    with session_factory.begin() as session:
        assert run_librarian_tick(
            session,
            workspace_id,
            embedding_provider="none",
        ) == {"skipped": "embedding_provider=none"}


def test_librarian_caps_recent_pages_and_avoids_pairwise_sql(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        pages = [
            ContextPage(
                workspace_id=workspace_id,
                slug=f"librarian-cap-{index}",
                title=f"Librarian Cap {index}",
                page_type="concept",
                lifecycle_status="published",
            )
            for index in range(205)
        ]
        session.add_all(pages)
        session.flush()
        versions = [
            ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                body_md=f"# {page.title}",
                content_hash=uuid4().hex,
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="librarian-cap-test",
                version_status="published_snapshot",
            )
            for page in pages
        ]
        session.add_all(versions)
        session.flush()
        for page, version in zip(pages, versions, strict=True):
            page.current_version_id = version.id
            page.current_published_version_id = version.id
        chunks = [
            ContentChunk(
                workspace_id=workspace_id,
                document_kind="context_page",
                document_id=page.id,
                content_scope="published",
                ordinal=0,
                char_start=0,
                char_end=4,
                chunk_text="Body",
                chunk_text_hash=uuid4().hex,
                token_count=1,
            )
            for page in pages
        ]
        session.add_all(chunks)
        session.flush()
        session.add_all(
            ChunkEmbedding(
                workspace_id=chunk.workspace_id,
                chunk_id=chunk.id,
                provider="deterministic",
                model="cap-test",
                dim=2,
                embedding=struct.pack("<2f", 1.0, 0.0),
            )
            for chunk in chunks
        )
        session.flush()

        observed: dict[str, int] = {}

        def no_duplicates(_session, _run, _workspace_id, _pages, vectors, _active):
            observed["duplicate_vectors"] = len(vectors)
            return 0

        def no_misplaced(_session, _run, _workspace_id, _pages, vectors, _active):
            observed["misplaced_vectors"] = len(vectors)
            return 0

        monkeypatch.setattr(
            "backend.services.librarian._stage_near_duplicates", no_duplicates
        )
        monkeypatch.setattr(
            "backend.services.librarian._stage_misplaced_pages", no_misplaced
        )
        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_query)
        try:
            result = run_librarian_tick(
                session,
                workspace_id,
                embedding_provider="deterministic",
                embedding_model="cap-test",
            )
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", count_query)

    assert result["analyzed_pages"] == 200
    assert result["truncated"] == 1
    assert observed == {"duplicate_vectors": 200, "misplaced_vectors": 200}
    assert query_count <= 8


def test_librarian_stages_a_misplaced_page_finding(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        folder = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            sort_key="misplaced-folder",
            title="Placement Folder",
            slug="placement-folder",
            visibility_status="visible",
        )
        pages = [
            ContextPage(
                workspace_id=workspace_id,
                slug=f"placement-{index}",
                title=f"Placement {index}",
                page_type="concept",
                lifecycle_status="published",
            )
            for index in range(4)
        ]
        session.add_all([folder, *pages])
        session.flush()
        session.add_all(
            WorkspaceTreeNode(
                workspace_id=workspace_id,
                section="context",
                node_kind="context_page",
                parent_id=folder.id,
                sort_key=f"placement-{index}",
                title=page.title,
                target_kind="context_page",
                target_id=page.id,
                primary_placement=True,
                visibility_status="visible",
            )
            for index, page in enumerate(pages)
        )
        run = LintRun(
            workspace_id=workspace_id,
            lint_scope={"kind": "maintenance"},
            trigger_type="librarian_tick",
            status="completed",
            finding_count_by_severity={},
        )
        session.add(run)
        session.flush()
        created = _stage_misplaced_pages(
            session,
            run,
            workspace_id,
            {page.id: page for page in pages},
            {
                pages[0].id: [1.0, 0.0],
                pages[1].id: [1.0, 0.0],
                pages[2].id: [1.0, 0.0],
                pages[3].id: [-1.0, 0.0],
            },
            set(),
        )

        finding = session.scalar(
            select(LintFinding).where(LintFinding.rule_id == "kb.misplaced_page")
        )
        assert created == 1
        assert finding is not None
        assert finding.target_id == pages[3].id


def test_eval_baseline_gate_reports_only_material_drops():
    modes = [ModeMetrics("keyword", 1, 0.90, 0.80, 1.0)]
    lanes = [LaneMetrics("entity", 1, 1.0, 1.0, 1.0)]
    baseline = {
        "threshold": 0.05,
        "modes": {"keyword": {"recall_at_k": 0.96, "mrr": 0.82}},
        "lanes": {"entity": {"recall_at_k": 1.0}},
    }
    regressions = compare_baseline(modes, lanes, baseline)
    assert [(item.scope, item.name, item.metric) for item in regressions] == [
        ("modes", "keyword", "recall_at_k")
    ]


@pytest.mark.parametrize(
    ("current_recall", "is_regression"),
    [
        (0.95, False),
        (0.9499999995, False),
        (0.949999, True),
    ],
)
def test_eval_baseline_gate_honors_epsilon_at_threshold(
    current_recall,
    is_regression,
):
    regressions = compare_baseline(
        [ModeMetrics("keyword", 1, current_recall, 1.0, 1.0)],
        [],
        {
            "threshold": 0.05,
            "modes": {"keyword": {"recall_at_k": 1.0}},
        },
    )

    assert bool(regressions) is is_regression


def _seed_ppr_star(session, workspace_id):
    """Ten thousand edges fanning out of one seed; returns the seed."""

    seed = uuid4()
    session.execute(
        insert(EdgeProjection),
        [
            {
                "workspace_id": workspace_id,
                "source_kind": "context_page",
                "source_id": seed,
                "target_kind": "context_page",
                "target_id": uuid4(),
                "edge_kind": "wikilink",
                "provenance": "curated",
                "weight": 1.0,
                "source_ref": {},
            }
            for _ in range(10_000)
        ],
    )
    return seed


def _cold_ppr(session, workspace_id, seed):
    # Drop the memoized subgraph so every run pays the full cold path
    # (frontier SQL + power iteration). The oversized request proves the
    # budget is clamped rather than trusted.
    _subgraph_cache.clear()
    return subgraph_ppr(
        session,
        workspace_id,
        [("context_page", seed, 1.0)],
        top_k=20,
        node_budget=20_000,
    )


def test_ppr_work_is_bounded_at_10k_edges(session_factory, monkeypatch):
    # The 50ms budget (§19.5.7) rests on PPR never materialising more than
    # PPR_NODE_BUDGET nodes, whatever the fan-out or the requested budget. That
    # bound is asserted on every run, as work rather than time: a 10k-edge star
    # must load one seed plus 499 neighbours, where dropping the bound loads all
    # 10k, and the SQL it issues must carry that same bound. Under four xdist
    # workers on a shared CI runner a healthy run measured 59ms, so a timing
    # gate failed without a regression; the wall clock lives in the opt-in
    # benchmark below.
    loaded = []
    frontier_sizes = []
    ranked_pages = []
    original_load = graph_ppr._load_subgraph
    original_frontier = graph_ppr._frontier_edges
    original_priors = graph_ppr.context_page_trust_priors

    def spy_load(session, workspace_id, seeds, *, node_budget):
        edges = original_load(session, workspace_id, seeds, node_budget=node_budget)
        nodes = set(seeds)
        for source_kind, source_id, target_kind, target_id, _weight in edges:
            nodes.update(((source_kind, source_id), (target_kind, target_id)))
        loaded.append((node_budget, len(edges), len(nodes)))
        return edges

    def spy_frontier(session, workspace_id, frontier, *, limit):
        rows = original_frontier(session, workspace_id, frontier, limit=limit)
        frontier_sizes.append(len(rows))
        return rows

    def spy_priors(session, workspace_id, page_ids, **kwargs):
        ranked_pages.append(len(page_ids))
        return original_priors(session, workspace_id, page_ids, **kwargs)

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_ppr_star(session, workspace_id)

        try:
            assert graph_ppr.PPR_NODE_BUDGET == 500
            with (
                monkeypatch.context() as patch,
                _captured_statements(session) as statements,
            ):
                patch.setattr(graph_ppr, "_load_subgraph", spy_load)
                patch.setattr(graph_ppr, "_frontier_edges", spy_frontier)
                patch.setattr(graph_ppr, "context_page_trust_priors", spy_priors)
                result = _cold_ppr(session, workspace_id, seed)

            assert loaded == [(500, 499, 500)]
            assert frontier_sizes == [500]
            assert len(result) == 20

            # The rank vector is the loaded subgraph: the budgeted nodes, no more.
            assert ranked_pages == [500]
            # The single hop reads edges once per direction, and both reads
            # carry the clamped budget rather than the 20_000 asked for.
            edge_reads = [
                (statement, parameters)
                for statement, parameters in statements
                if "FROM edge_projection" in statement
            ]
            assert [_bound_limit(*read) for read in edge_reads] == [500, 500]
            # Revision lookup, those two reads and one trust-prior read: the
            # whole cold path.
            assert len(statements) <= 4, "unexpected extra statements"
        finally:
            _subgraph_cache.clear()


def test_ppr_power_iteration_stops_after_12_steps(session_factory, monkeypatch):
    # Rank mass moves one hop per power iteration, so along a path the nodes
    # holding any rank count the iterations that ran: 12 of them reach the seed
    # and the twelve nodes after it, and no further. The loader is replaced
    # because the real one never reaches past two hops.
    path = [("context_page", uuid4()) for _ in range(30)]
    edges = tuple(
        (*source, *target, 1.0) for source, target in zip(path, path[1:], strict=False)
    )

    def path_subgraph(_session, _workspace_id, _seeds, *, node_budget):
        return edges

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        try:
            monkeypatch.setattr(graph_ppr, "_load_subgraph", path_subgraph)
            result = subgraph_ppr(
                session, workspace_id, [(*path[0], 1.0)], top_k=len(path)
            )
        finally:
            _subgraph_cache.clear()

    assert {item.entity_id for item in result if item.score > 0} == {
        node_id for _kind, node_id in path[:13]
    }


@pytest.fixture
def benchmark_lane(request):
    """Gate for wall-clock budgets: opt-in, and never beside xdist workers.

    A latency measured on a shared runner proves nothing about the code, so the
    functional lanes assert counted work and the budgets run only when asked
    for, one process at a time: ``make test-backend-benchmarks``.
    """

    if os.environ.get("DTXWIKI_TEST_BENCHMARKS") != "1":
        pytest.skip("wall-clock budget; run `make test-backend-benchmarks`")
    if hasattr(request.config, "workerinput"):
        pytest.fail(
            "DTXWIKI_TEST_BENCHMARKS=1 under pytest-xdist: a budget timed beside "
            "other workers is not a measurement; run the lane without -n, as "
            "`make test-backend-benchmarks` does",
            pytrace=False,
        )


def test_ppr_latency_benchmark_10k_edges(session_factory, benchmark_lane):
    """19.5.7 budget: a cold 10k-edge PPR in <= 50ms, best of three."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _seed_ppr_star(session, workspace_id)

        try:
            # The first run is the unmeasured warm-up.
            _cold_ppr(session, workspace_id, seed)
            elapsed = min(
                _elapsed(lambda: _cold_ppr(session, workspace_id, seed))
                for _ in range(3)
            )
            assert elapsed <= 0.05, f"PPR budget exceeded: {elapsed:.3f}s"
        finally:
            _subgraph_cache.clear()


def _seed_authority_chain(session, workspace_id):
    """A 100k-edge chain on top of the workspace's own provisioned graph.

    Returns the distinct nodes the workspace already carried, the chain's nodes
    in order, and the edge rows exactly as ``recompute_authority`` selects them.
    """

    # A provisioned workspace already carries tree edges of its own, so
    # the budget is measured against the seeded graph plus the 100k this
    # test adds, not against 100k alone.
    seeded_nodes = _distinct_edge_endpoints(session, workspace_id)
    nodes = [uuid4() for _ in range(100_001)]
    session.execute(
        insert(EdgeProjection),
        [
            {
                "workspace_id": workspace_id,
                "source_kind": "context_page",
                "source_id": nodes[index],
                "target_kind": "context_page",
                "target_id": nodes[index + 1],
                "edge_kind": "wikilink",
                "provenance": "curated",
                "weight": 1.0,
                "source_ref": {},
            }
            for index in range(100_000)
        ],
    )

    edge_rows = session.execute(
        select(
            EdgeProjection.source_kind,
            EdgeProjection.source_id,
            EdgeProjection.target_kind,
            EdgeProjection.target_id,
            EdgeProjection.edge_kind,
            EdgeProjection.weight,
        ).where(EdgeProjection.workspace_id == workspace_id)
    ).all()
    return seeded_nodes, nodes, edge_rows


def _metered_edges(rows, *, probe_budget):
    """Wrap authority-calculation input so the algorithm reports its own work.

    The calculation is a pure function over its edges, so the edges are the one
    place to watch it from. Every wrapper delegates to the value it stands for,
    leaving the ranks exactly as the plain call computes them; each only counts.
    ``passes`` and ``rows`` count reads of the edge sequence, ``transitions`` the
    weights scaled into transition probabilities (by whichever of ``*`` and ``/``
    reaches a weight first, so the order the code writes them in is immaterial),
    ``probes`` the hash-table probes made on node ids. A probe past
    ``probe_budget`` raises on the spot, so a quadratic node index fails at once
    instead of running for hours.
    """

    meter = SimpleNamespace(passes=0, rows=0, transitions=0, probes=0)

    def probe():
        meter.probes += 1
        if meter.probes > probe_budget:
            raise AssertionError(f"node ids probed more than {probe_budget} times")

    class Weight(float):
        def __mul__(self, other):
            meter.transitions += 1
            return float(self) * other

        __rmul__ = __mul__

        def __truediv__(self, other):
            meter.transitions += 1
            return float(self) / other

    class NodeId(UUID):
        def __hash__(self):
            probe()
            return super().__hash__()

        def __eq__(self, other):
            probe()
            return super().__eq__(other)

    class Edges:
        def __iter__(self):
            meter.passes += 1
            for source_kind, source_id, target_kind, target_id, kind, weight in rows:
                meter.rows += 1
                yield (
                    source_kind,
                    NodeId(int=source_id.int),
                    target_kind,
                    NodeId(int=target_id.int),
                    kind,
                    Weight(weight),
                )

    return Edges(), meter


def test_authority_work_is_bounded_at_100k_edges(session_factory):
    # The 5s budget (§19.5.7) rests on the calculation doing a bounded amount of
    # work per edge. That is counted on every run instead of timed (the wall
    # clock lives in the opt-in benchmark below): the edges are read once, one
    # transition is built per edge, the node index is probed a constant number
    # of times per edge, and rank propagates for the 20-iteration cap, no more.
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded_nodes, nodes, edge_rows = _seed_authority_chain(session, workspace_id)

    edges, meter = _metered_edges(edge_rows, probe_budget=8 * len(edge_rows))
    stats = _authority_stat_rows(
        edges,
        workspace_id=workspace_id,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert len(stats) == 100_001 + seeded_nodes
    assert (meter.passes, meter.rows) == (1, len(edge_rows))
    # A co_access edge also builds its reverse, so two per edge is the ceiling;
    # these are all wikilinks, which build exactly one.
    assert meter.transitions == len(edge_rows)
    # Four probes per edge here (a hit costs a hash and a compare, a miss a hash
    # and a store), budgeted at eight so that only a super-linear index trips it.
    assert 0 < meter.probes <= 8 * len(edge_rows)

    # Along a chain rank travels one hop per iteration, so a node's value only
    # depends on min(position, iterations) and levels off at position ==
    # iterations: the plateau starts at 20 only if exactly 20 iterations ran.
    authority = {row["entity_id"].int: row["authority"] for row in stats}
    chain = [authority[node.int] for node in nodes]
    plateau = chain[-1]
    assert chain.index(plateau) == 20
    assert set(chain[20:]) == {plateau}


def test_authority_builds_at_most_two_transitions_per_edge():
    # A co_access edge is mutual, so it builds its reverse as well; every other
    # kind builds one. Two per edge is therefore the ceiling on inner-loop work.
    kinds = ["wikilink", "co_access", "wikilink", "co_access", "co_access"]
    ids = [uuid4() for _ in range(len(kinds) + 1)]
    rows = [
        ("context_page", ids[index], "context_page", ids[index + 1], kind, 1.0)
        for index, kind in enumerate(kinds)
    ]
    edges, meter = _metered_edges(rows, probe_budget=8 * len(rows))

    _authority_stat_rows(
        edges, workspace_id=uuid4(), now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert meter.transitions == len(rows) + kinds.count("co_access")
    assert meter.transitions <= 2 * len(rows)


def test_authority_latency_benchmark_100k_edges(session_factory, benchmark_lane):
    """19.5.7 budget: authority over a 100k-edge graph in <= 5s, best of two."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded_nodes, _nodes, edge_rows = _seed_authority_chain(session, workspace_id)

        def calculate():
            return _authority_stat_rows(
                edge_rows,
                workspace_id=workspace_id,
                now=datetime.now(UTC),
            )

        # The algorithm budget is measured apart from DB scheduling noise.
        # Persistence and revision-based no-op behavior have separate tests.
        stats = calculate()
        elapsed = min(_elapsed(calculate) for _ in range(2))
        assert len(stats) == 100_001 + seeded_nodes
        assert elapsed <= 5.0, f"authority budget exceeded: {elapsed:.3f}s"


def _distinct_edge_endpoints(session, workspace_id) -> int:
    """Count the nodes a workspace's existing edges already contribute.

    Provisioning gives every workspace a tree, and the tree projects edges, so
    a test that adds edges of its own cannot assume it started from nothing.
    """

    rows = session.execute(
        select(
            EdgeProjection.source_kind,
            EdgeProjection.source_id,
            EdgeProjection.target_kind,
            EdgeProjection.target_id,
        ).where(EdgeProjection.workspace_id == workspace_id)
    ).all()
    endpoints = set()
    for source_kind, source_id, target_kind, target_id in rows:
        endpoints.add((source_kind, source_id))
        endpoints.add((target_kind, target_id))
    return len(endpoints)


def test_authority_projection_skips_unchanged_edge_revision(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded_nodes = _distinct_edge_endpoints(session, workspace_id)
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=uuid4(),
                target_kind="context_page",
                target_id=uuid4(),
                edge_kind="wikilink",
                provenance="curated",
                weight=1.0,
                source_ref={},
            )
        )
        session.flush()
        assert recompute_authority(session, workspace_id) == 2 + seeded_nodes

        statements: list[str] = []

        def capture(_conn, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", capture)
        try:
            assert recompute_authority(session, workspace_id) == 2 + seeded_nodes
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", capture)

        assert not any("FROM edge_projection" in statement for statement in statements)
