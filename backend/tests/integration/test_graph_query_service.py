"""Actor-visible GraphScene construction and temporal truth tests."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select

from backend.models.ask import AskSession, AskTurn, AskTurnCitation
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.graph import GraphObservation
from backend.models.mcp_chat import McpChatSession, McpChatToolCallArtifact
from backend.models.mcp_trace import MCPToolCall
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryRequest,
    GraphSeed,
    GraphTimeQuery,
)
from backend.services import graph_service as graph_service_module
from backend.services.graph_observation_service import GraphObservationService
from backend.services.graph_query_service import (
    GRAPH_OBSERVATION_PAGE_LIMIT,
    GraphQueryService,
    graph_node_id,
)
from backend.services.graph_semantics import ACTION_SEMANTICS, EDGE_SEMANTICS
from backend.services.graph_service import VISIBLE_PROJECTION_PAGE_LIMIT
from backend.services.read_errors import ReadNotFoundError, ReadValidationError

pytestmark = pytest.mark.integration


def _actor(role="viewer"):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _pat_actor(*, role="editor", scopes=("context-read",)):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="api_token",
        scopes=scopes,
        workspace_role=role,
    )


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _page(session, workspace_id, *, slug, title, published=True):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=page.workspace_id,
        id=uuid4(),
        page_id=page.id,
        version_no=1,
        body_md=f"# {title}",
        content_hash=f"graph-query-{slug}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot" if published else "draft",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    if published:
        page.current_published_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    return page


def _edge(session, workspace_id, source, target, kind="wikilink", weight=1.0):
    edge = EdgeProjection(
        workspace_id=workspace_id,
        source_kind="context_page",
        source_id=source.id,
        target_kind="context_page",
        target_id=target.id,
        edge_kind=kind,
        provenance="usage" if kind == "co_access" else "curated",
        weight=weight,
        source_ref={},
    )
    session.add(edge)
    return edge


def _current_request(seed_id, **updates):
    values = {
        "seeds": [GraphSeed(kind="context_page", id=seed_id)],
        "edge_kinds": ["wikilink"],
        "layers": ["knowledge"],
        "budget": GraphBudget(nodes=10, edges=10),
    }
    values.update(updates)
    return GraphQueryRequest(**values)


def test_query_refills_after_1025_hidden_edges_and_ids_survive_rebuild(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="scene-seed",
            title="Scene Seed",
        )
        visible = _page(
            session,
            workspace_id,
            slug="scene-visible",
            title="Scene Visible",
        )
        for index in range(1_025):
            hidden = ContextPage(
                id=uuid4(),
                workspace_id=workspace_id,
                slug=f"scene-hidden-{index}",
                title=f"Hidden {index}",
                page_type="concept",
                lifecycle_status="draft",
            )
            session.add(hidden)
            _edge(
                session,
                workspace_id,
                seed,
                hidden,
                weight=float(2_000 - index),
            )
        visible_edge = _edge(session, workspace_id, seed, visible, weight=1.0)
        seed_id = seed.id
        visible_id = visible.id

    request = _current_request(
        seed_id,
        budget=GraphBudget(nodes=2, edges=1),
    )
    service = GraphQueryService()
    with session_factory.begin() as session:
        first = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        assert {node.entity_id for node in first.nodes} == {seed_id, visible_id}
        assert len(first.edges) == 1
        assert first.facets.node_kinds == ["context_page"]
        assert first.facets.edge_kinds == ["wikilink"]
        assert first.completeness == "complete"
        assert first.truncated is False
        first_ids = (
            [node.id for node in first.nodes],
            [edge.id for edge in first.edges],
        )

        projected = session.get(EdgeProjection, visible_edge.id)
        session.delete(projected)
        session.flush()
        _edge(
            session,
            workspace_id,
            session.get(ContextPage, seed_id),
            session.get(ContextPage, visible_id),
            weight=1.0,
        )
        session.flush()
        rebuilt = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )

    assert (
        [node.id for node in rebuilt.nodes],
        [edge.id for edge in rebuilt.edges],
    ) == (first_ids)


def test_missing_tree_projection_rows_do_not_spend_raw_cap(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="bounded-projection-seed",
            title="Bounded Projection Seed",
        )
        visible = _page(
            session,
            workspace_id,
            slug="bounded-projection-visible",
            title="Bounded Projection Visible",
        )
        session.add_all(
            [
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind="workspace_tree_node",
                    target_id=uuid4(),
                    edge_kind="wikilink",
                    provenance="curated",
                    weight=float(2_000 - index),
                    source_ref={},
                )
                for index in range(1_025)
            ]
        )
        _edge(session, workspace_id, seed, visible, weight=1.0)
        seed_id = seed.id
        visible_id = visible.id

    statements: list[str] = []
    request = _current_request(
        seed_id,
        direction="out",
        budget=GraphBudget(nodes=2, edges=1),
    )
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = GraphQueryService().query(
                session,
                workspace_id,
                request=request,
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert {node.entity_id for node in payload.nodes} == {seed_id, visible_id}
    assert len(payload.edges) == 1
    assert payload.completeness == "complete"
    assert payload.truncated is False
    assert payload.warnings == []
    edge_queries = [
        statement for statement in statements if "FROM edge_projection" in statement
    ]
    # Ranked frontiers, denominator reads and one induced-edge read remain
    # bounded independently of the 1,025 missing tree identifiers.
    assert 1 <= len(edge_queries) <= VISIBLE_PROJECTION_PAGE_LIMIT + 4


def test_dense_induced_relations_keep_class_shares_and_report_source_cap(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="induced-cap-seed",
            title="Induced Cap Seed",
        )
        peers = [
            _page(
                session,
                workspace_id,
                slug=f"induced-cap-peer-{index}",
                title=f"Induced Cap Peer {index}",
            )
            for index in range(30)
        ]
        for peer in peers:
            _edge(session, workspace_id, seed, peer)
        for source in peers:
            for target in peers:
                if source.id != target.id:
                    _edge(session, workspace_id, source, target, weight=9.0)
        seed_id = seed.id

    induced_statements: list[str] = []
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, context, _many):
            if context.execution_options.get("graph_rank_phase") == "induced_relations":
                induced_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = GraphQueryService().query(
                session,
                workspace_id,
                request=_current_request(
                    seed_id,
                    depth=1,
                    budget=GraphBudget(nodes=40, edges=10),
                ),
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    seed_node_id = graph_node_id("context_page", seed_id)
    assert len(payload.nodes) == 31
    assert len(payload.edges) == 10
    assert any(seed_node_id in (edge.source, edge.target) for edge in payload.edges)
    assert any(seed_node_id not in (edge.source, edge.target) for edge in payload.edges)
    assert any(
        omission.reason == "relation_source_cap" and omission.relation_class == "h1"
        for omission in payload.omissions
    ), payload.omissions
    assert not any(
        omission.reason == "relation_source_cap" and omission.relation_class == "seed"
        for omission in payload.omissions
    )
    assert payload.completeness == "bounded_sample"
    assert payload.truncated is True
    assert "graph_projection_source_bounded" in payload.warnings
    assert len(induced_statements) == 1
    assert "row_number()" in induced_statements[0].lower()


def test_induced_source_window_only_counts_requested_direction(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="direction-cap-seed",
            title="Direction Cap Seed",
        )
        for index in range(129):
            peer = _page(
                session,
                workspace_id,
                slug=f"direction-cap-peer-{index}",
                title=f"Direction Cap Peer {index}",
            )
            _edge(session, workspace_id, seed, peer, weight=1.0)
            _edge(session, workspace_id, peer, seed, weight=9.0)
        seed_id = seed.id

    with session_factory() as session:
        payload = GraphQueryService().query(
            session,
            workspace_id,
            request=_current_request(
                seed_id,
                direction="out",
                depth=1,
                budget=GraphBudget(nodes=160, edges=10),
            ),
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )

    seed_node_id = graph_node_id("context_page", seed_id)
    assert len(payload.nodes) == 130
    assert len(payload.edges) == 10
    assert all(edge.source == seed_node_id for edge in payload.edges)
    assert any(
        omission.reason == "relation_source_cap" and omission.relation_class == "seed"
        for omission in payload.omissions
    )
    assert payload.completeness == "bounded_sample"


def test_observation_sql_prefilter_preserves_visible_after_1025_hidden(
    session_factory,
):
    occurred_at = datetime(2025, 8, 4, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="observation-prefilter-seed",
            title="Observation Prefilter Seed",
        )
        visible = _page(
            session,
            workspace_id,
            slug="observation-prefilter-visible",
            title="Observation Prefilter Visible",
        )
        hidden_pages = [
            ContextPage(
                id=uuid4(),
                workspace_id=workspace_id,
                slug=f"observation-hidden-{index}",
                title=f"Observation Hidden {index}",
                page_type="concept",
                lifecycle_status="draft",
            )
            for index in range(1_025)
        ]
        session.add_all(hidden_pages)
        session.add_all(
            [
                GraphObservation(
                    workspace_id=workspace_id,
                    operation="edge_created",
                    workspace_sequence=index + 2,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind="context_page",
                    target_id=hidden.id,
                    edge_kind="wikilink",
                    occurred_at=occurred_at + timedelta(seconds=index + 2),
                    idempotency_key=f"{index + 2:064x}",
                )
                for index, hidden in enumerate(hidden_pages)
            ]
        )
        session.add(
            GraphObservation(
                workspace_id=workspace_id,
                operation="edge_created",
                workspace_sequence=1,
                source_kind="context_page",
                source_id=seed.id,
                target_kind="context_page",
                target_id=visible.id,
                edge_kind="wikilink",
                occurred_at=occurred_at,
                idempotency_key=f"{1:064x}",
            )
        )
        session.get(Workspace, workspace_id).graph_observation_sequence = 1_027
        # Current authored membership comes from the rebuildable projection;
        # the ledger supplies only this relation's dated occurrence.
        _edge(session, workspace_id, seed, visible)
        seed_id = seed.id
        visible_id = visible.id

    statements: list[str] = []
    request = _current_request(
        seed_id,
        direction="out",
        budget=GraphBudget(nodes=2, edges=1),
        time=GraphTimeQuery(mode="current", from_=occurred_at, to=occurred_at),
    )
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = GraphQueryService().query(
                session,
                workspace_id,
                request=request,
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert {node.entity_id for node in payload.nodes} == {seed_id, visible_id}
    assert len(payload.edges) == 1
    assert payload.completeness == "complete"
    assert payload.truncated is False
    observation_queries = [
        statement for statement in statements if "FROM graph_observation" in statement
    ]
    # Seed, H1 and selected induced scopes each need one bounded read. The
    # 1,025 invisible rows must not force additional tail paging in any scope.
    assert len(observation_queries) == 3


def test_observation_direction_is_enforced_for_scene_and_timeline(session_factory):
    occurred_at = datetime(2025, 8, 4, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        source = _page(
            session,
            workspace_id,
            slug="observation-direction-source",
            title="Observation Direction Source",
        )
        target = _page(
            session,
            workspace_id,
            slug="observation-direction-target",
            title="Observation Direction Target",
        )
        session.add(
            GraphObservation(
                workspace_id=workspace_id,
                operation="edge_created",
                workspace_sequence=1,
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=target.id,
                edge_kind="wikilink",
                occurred_at=occurred_at,
                idempotency_key=f"{1:064x}",
            )
        )
        session.get(Workspace, workspace_id).graph_observation_sequence = 1
        _edge(session, workspace_id, source, target)
        source_id = source.id
        target_id = target.id

    cases = (
        (source_id, "out", True),
        (source_id, "in", False),
        (target_id, "in", True),
        (target_id, "out", False),
    )
    with session_factory() as session:
        service = GraphQueryService()
        for seed_id, direction, expected in cases:
            current = service.query(
                session,
                workspace_id,
                request=_current_request(
                    seed_id,
                    direction=direction,
                    budget=GraphBudget(nodes=2, edges=1),
                    # An undated projection alone cannot satisfy this
                    # assertion: current evidence must obey direction too.
                    time=GraphTimeQuery(
                        mode="current", from_=occurred_at, to=occurred_at
                    ),
                ),
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
            assert bool(current.edges) is expected

            appearance = service.query(
                session,
                workspace_id,
                request=_current_request(
                    seed_id,
                    direction=direction,
                    time=GraphTimeQuery(mode="appearance"),
                ),
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
            observation_events = [
                item
                for item in appearance.timeline
                if item.evidence is not None
                and item.evidence.kind == "graph_observation"
            ]
            assert bool(observation_events) is expected


def test_missing_tree_observations_do_not_spend_raw_cap(session_factory):
    occurred_at = datetime(2025, 8, 4, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="observation-fallback-seed",
            title="Observation Fallback Seed",
        )
        visible = _page(
            session,
            workspace_id,
            slug="observation-fallback-visible",
            title="Observation Fallback Visible",
        )
        missing_tree_id = uuid4()
        session.add_all(
            [
                GraphObservation(
                    workspace_id=workspace_id,
                    operation="edge_created",
                    workspace_sequence=index + 1,
                    source_kind="context_page",
                    source_id=seed.id,
                    target_kind="workspace_tree_node",
                    target_id=missing_tree_id,
                    edge_kind="wikilink",
                    occurred_at=occurred_at,
                    idempotency_key=f"{index + 1:064x}",
                )
                for index in range(1, 1_026)
            ]
        )
        # This actor-visible relation is older than every missing-tree row.
        # The SQL visibility gate must still reach it without raw truncation.
        session.add(
            GraphObservation(
                workspace_id=workspace_id,
                operation="edge_created",
                workspace_sequence=1,
                source_kind="context_page",
                source_id=seed.id,
                target_kind="context_page",
                target_id=visible.id,
                edge_kind="wikilink",
                occurred_at=occurred_at - timedelta(seconds=1),
                idempotency_key=f"{1:064x}",
            )
        )
        session.get(Workspace, workspace_id).graph_observation_sequence = 1_026
        _edge(session, workspace_id, seed, visible)
        seed_id = seed.id
        visible_id = visible.id

    statements: list[str] = []
    request = _current_request(
        seed_id,
        direction="out",
        budget=GraphBudget(nodes=2, edges=1),
        time=GraphTimeQuery(
            mode="current",
            from_=occurred_at - timedelta(seconds=1),
            to=occurred_at - timedelta(seconds=1),
        ),
    )
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = GraphQueryService().query(
                session,
                workspace_id,
                request=request,
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert {node.entity_id for node in payload.nodes} == {seed_id, visible_id}
    assert len(payload.edges) == 1
    assert payload.completeness == "complete"
    assert payload.truncated is False
    assert payload.warnings == []
    observation_queries = [
        statement for statement in statements if "FROM graph_observation" in statement
    ]
    assert 1 <= len(observation_queries) <= GRAPH_OBSERVATION_PAGE_LIMIT
    # Missing tree identifiers are rejected in SQL, before the hydrator.
    assert not any(
        "workspace_tree_node.id in" in statement.lower() for statement in statements
    )


def test_co_access_is_returned_like_any_other_edge_kind(
    session_factory,
):
    """No relation-specific gate: visibility comes from the actor, not the kind.

    co_access used to fail closed for every role behind a k-anonymity
    threshold. A workspace is a team of a handful of named people, so that
    threshold was unreachable by construction and only guaranteed the
    relation stayed empty. Access control is what has to hold, and hydration
    already enforces it for every edge kind alike.
    """

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, slug="co-seed", title="Co Seed")
        target = _page(session, workspace_id, slug="co-target", title="Co Target")
        _edge(session, workspace_id, seed, target, kind="co_access")
        seed_id = seed.id

    request = _current_request(
        seed_id,
        edge_kinds=["co_access"],
        layers=["activity"],
    )
    with session_factory() as session:
        service = GraphQueryService()
        viewer = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        owner = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("owner"),
            usage_signals_enabled=True,
        )

    for scene in (viewer, owner):
        assert [edge.edge_kind for edge in scene.edges] == ["co_access"]
        assert scene.edges[0].direction == "undirected"
        assert scene.completeness == "complete"
        assert scene.warnings == []


def test_action_scene_uses_included_not_cited_and_never_returns_raw_inputs(
    session_factory,
):
    occurred_at = datetime(2026, 8, 4, 2, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="action-seed",
            title="Action Seed",
        )
        ask_session = AskSession(
            workspace_id=workspace_id,
            title="Safe Ask",
            started_at=occurred_at,
            actor_kind="human",
            placement_tree_node_id=uuid4(),
        )
        session.add(ask_session)
        session.flush()
        turn = AskTurn(
            workspace_id=ask_session.workspace_id,
            ask_session_id=ask_session.id,
            turn_no=1,
            user_message="SECRET_PROMPT_DO_NOT_RETURN",
            assistant_message="SECRET_ANSWER_DO_NOT_RETURN",
        )
        session.add(turn)
        session.flush()
        citation = AskTurnCitation(
            workspace_id=turn.workspace_id,
            ask_turn_id=turn.id,
            target_kind="context_page",
            target_id=seed.id,
            rank=1,
            created_at=occurred_at,
        )
        session.add(citation)

        client_id = uuid4()
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client_id,
            mcp_session_id="safe-session-token",
            started_at=occurred_at,
            last_activity_at=occurred_at,
        )
        session.add(chat)
        session.flush()
        tool_call = MCPToolCall(
            client_id=client_id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name="read_concept",
            input_summary={"query": "SECRET_TOOL_ARG_DO_NOT_RETURN"},
            result_status="ok",
            started_at=occurred_at,
        )
        session.add(tool_call)
        session.flush()
        session.add(
            McpChatToolCallArtifact(
                workspace_id=chat.workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=tool_call.id,
                resource_kind="context_page",
                resource_id=seed.id,
                verb="referenced",
                occurred_at=occurred_at,
            )
        )
        seed_id = seed.id

    request = _current_request(
        seed_id,
        time=GraphTimeQuery(mode="action"),
        layers=["action", "evidence", "activity"],
        edge_kinds=[],
        budget=GraphBudget(nodes=20, edges=20),
    )
    with session_factory() as session:
        payload = GraphQueryService().query(
            session,
            workspace_id,
            request=request,
            actor=_actor("editor"),
            usage_signals_enabled=True,
        )

    seed_transport_id = next(node.id for node in payload.nodes if node.is_seed)
    ask_edges = [
        edge
        for edge in payload.edges
        if edge.evidence is not None
        and edge.evidence.kind == "ask_turn_citation"
        and edge.action_kind == "included"
        and edge.target == seed_transport_id
    ]
    assert len(ask_edges) == 1
    assert ask_edges[0].action_kind == "included"
    assert "chat" in {node.family for node in payload.nodes}
    assert {"ask_session", "mcp_chat_session"} <= {
        node.entity_kind for node in payload.nodes
    }
    nodes_by_id = {node.id: node for node in payload.nodes}
    assert {
        node.entity_kind: node.family
        for node in payload.nodes
        if node.entity_kind in {"ask_session", "mcp_chat_session"}
    } == {"ask_session": "ask", "mcp_chat_session": "chat"}
    assert any(
        nodes_by_id[edge.source].entity_kind == "ask_session"
        and nodes_by_id[edge.target].entity_kind == "ask_turn"
        and edge.action_kind == "included"
        for edge in payload.edges
    )
    assert any(
        nodes_by_id[edge.source].entity_kind == "mcp_chat_session"
        and nodes_by_id[edge.target].entity_kind == "mcp_tool_call"
        and edge.action_kind == "tool_produced"
        for edge in payload.edges
    )
    unavailable_evidence = [
        *(node.evidence for node in payload.nodes if node.evidence is not None),
        *(edge.evidence for edge in payload.edges if edge.evidence is not None),
        *(event.evidence for event in payload.timeline if event.evidence is not None),
    ]
    assert all(evidence.available is False for evidence in unavailable_evidence)
    assert all(
        evidence.id is None and evidence.ref is None
        for evidence in unavailable_evidence
    )
    assert all(
        edge.edge_kind in EDGE_SEMANTICS
        or (edge.action_kind in ACTION_SEMANTICS and edge.edge_kind == edge.action_kind)
        for edge in payload.edges
    )
    serialized = payload.model_dump_json()
    assert str(citation.id) not in serialized
    assert "SECRET_PROMPT_DO_NOT_RETURN" not in serialized
    assert "SECRET_ANSWER_DO_NOT_RETURN" not in serialized
    assert "SECRET_TOOL_ARG_DO_NOT_RETURN" not in serialized


def test_domain_action_path_is_seed_relative_inbound_including_session_context(
    session_factory,
):
    occurred_at = datetime(2026, 8, 4, 2, 15, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="domain-direction-seed",
            title="Domain Direction Seed",
        )
        ask_session = AskSession(
            workspace_id=workspace_id,
            title="Direction Ask",
            started_at=occurred_at,
            actor_kind="human",
            placement_tree_node_id=uuid4(),
        )
        session.add(ask_session)
        session.flush()
        turn = AskTurn(
            workspace_id=ask_session.workspace_id,
            ask_session_id=ask_session.id,
            turn_no=1,
            user_message="Direction question",
            assistant_message="Direction answer",
        )
        session.add(turn)
        session.flush()
        session.add(
            AskTurnCitation(
                workspace_id=turn.workspace_id,
                ask_turn_id=turn.id,
                target_kind="context_page",
                target_id=seed.id,
                rank=1,
                created_at=occurred_at,
            )
        )
        seed_id = seed.id

    def request(direction: str) -> GraphQueryRequest:
        return _current_request(
            seed_id,
            direction=direction,
            time=GraphTimeQuery(mode="action"),
            layers=["action"],
            edge_kinds=[],
            budget=GraphBudget(nodes=10, edges=10),
        )

    with session_factory() as session:
        service = GraphQueryService()
        inbound = service.query(
            session,
            workspace_id,
            request=request("in"),
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        outbound = service.query(
            session,
            workspace_id,
            request=request("out"),
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )

    inbound_kinds = {node.entity_kind for node in inbound.nodes}
    assert {"ask_turn", "ask_session"} <= inbound_kinds
    assert any(
        edge.evidence is not None
        and edge.evidence.kind == "ask_turn_citation"
        and edge.action_kind == "included"
        for edge in inbound.edges
    )
    assert any(
        event.evidence is not None and event.evidence.kind == "ask_turn_citation"
        for event in inbound.timeline
    )

    outbound_kinds = {node.entity_kind for node in outbound.nodes}
    assert "ask_turn" not in outbound_kinds
    assert "ask_session" not in outbound_kinds
    assert all(
        edge.evidence is None or edge.evidence.kind != "ask_turn_citation"
        for edge in outbound.edges
    )
    assert all(
        event.evidence is None or event.evidence.kind != "ask_turn_citation"
        for event in outbound.timeline
    )


def test_old_session_context_is_excluded_from_narrow_action_window(
    session_factory,
):
    occurred_at = datetime(2026, 8, 4, 3, 0, tzinfo=UTC)
    old_session_at = occurred_at - timedelta(days=1)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="domain-session-window-seed",
            title="Domain Session Window Seed",
        )
        ask_session = AskSession(
            workspace_id=workspace_id,
            title="Old Ask Session",
            started_at=old_session_at,
            actor_kind="human",
            placement_tree_node_id=uuid4(),
        )
        session.add(ask_session)
        session.flush()
        turn = AskTurn(
            workspace_id=ask_session.workspace_id,
            ask_session_id=ask_session.id,
            turn_no=1,
            user_message="Window question",
            assistant_message="Window answer",
        )
        session.add(turn)
        session.flush()
        session.add(
            AskTurnCitation(
                workspace_id=turn.workspace_id,
                ask_turn_id=turn.id,
                target_kind="context_page",
                target_id=seed.id,
                rank=1,
                created_at=occurred_at,
            )
        )

        client_id = uuid4()
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client_id,
            mcp_session_id="old-window-session",
            started_at=old_session_at,
            last_activity_at=occurred_at,
        )
        session.add(chat)
        session.flush()
        tool_call = MCPToolCall(
            client_id=client_id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name="read_concept",
            input_summary={},
            result_status="ok",
            started_at=occurred_at,
        )
        session.add(tool_call)
        session.flush()
        session.add(
            McpChatToolCallArtifact(
                workspace_id=chat.workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=tool_call.id,
                resource_kind="context_page",
                resource_id=seed.id,
                verb="referenced",
                occurred_at=occurred_at,
            )
        )
        seed_id = seed.id

    request = _current_request(
        seed_id,
        direction="in",
        time=GraphTimeQuery(
            mode="action",
            from_=occurred_at - timedelta(minutes=1),
            to=occurred_at + timedelta(minutes=1),
        ),
        layers=["action", "evidence"],
        edge_kinds=[],
        budget=GraphBudget(nodes=20, edges=20),
    )
    with session_factory() as session:
        payload = GraphQueryService().query(
            session,
            workspace_id,
            request=request,
            actor=_actor("editor"),
            usage_signals_enabled=True,
        )

    kinds = {node.entity_kind for node in payload.nodes}
    assert {"ask_turn", "mcp_tool_call"} <= kinds
    assert "ask_session" not in kinds
    assert "mcp_chat_session" not in kinds
    assert {
        edge.evidence.kind for edge in payload.edges if edge.evidence is not None
    } >= {"ask_turn_citation", "mcp_tool_call_artifact"}
    assert {
        event.evidence.kind for event in payload.timeline if event.evidence is not None
    } >= {"ask_turn_citation", "mcp_tool_call_artifact"}


def test_viewer_history_reauthorizes_versions_and_sensitive_event_sources(
    session_factory,
):
    occurred_at = datetime(2026, 8, 4, 2, 30, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="acl-action-seed",
            title="ACL Action Seed",
        )
        draft = ContextPageVersion(
            workspace_id=seed.workspace_id,
            page_id=seed.id,
            version_no=2,
            body_md="# Private draft",
            content_hash="acl-private-draft",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="private-author",
            version_status="draft",
            created_at=occurred_at,
        )
        session.add(draft)
        session.flush()
        seed.latest_draft_version_id = draft.id
        seed.current_version_id = draft.id

        client_id = uuid4()
        chat = McpChatSession(
            workspace_id=workspace_id,
            client_id=client_id,
            mcp_session_id="acl-sensitive-chat",
            started_at=occurred_at,
            last_activity_at=occurred_at,
        )
        session.add(chat)
        session.flush()
        tool_call = MCPToolCall(
            client_id=client_id,
            workspace_id=workspace_id,
            request_id=uuid4(),
            tool_name="update_draft",
            input_summary={"body": "PRIVATE_TOOL_BODY"},
            result_status="ok",
            started_at=occurred_at,
        )
        session.add(tool_call)
        session.flush()
        session.add(
            McpChatToolCallArtifact(
                workspace_id=chat.workspace_id,
                chat_session_id=chat.id,
                mcp_tool_call_id=tool_call.id,
                resource_kind="context_page",
                resource_id=seed.id,
                verb="edited",
                occurred_at=occurred_at,
            )
        )

        change_set = ChangeSet(
            workspace_id=workspace_id,
            trigger_type="manual",
            actor_type="human",
            status="draft_created",
        )
        session.add(change_set)
        session.flush()
        operation = ChangeOperation(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            target_type="context_page",
            target_id=seed.id,
            operation="update",
            diff_summary="PRIVATE_DIFF",
            created_at=occurred_at,
        )
        session.add(operation)
        review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=change_set.id,
            review_type="context_publish",
            status="approved",
            risk_level="low",
        )
        session.add(review)
        session.flush()
        decision = ReviewDecision(
            workspace_id=review.workspace_id,
            review_item_id=review.id,
            decision_type="approve",
            actor_type="human",
            resulting_review_status="approved",
            resulting_change_set_status="approved",
            created_at=occurred_at,
        )
        session.add(decision)
        audit = AuditEvent(
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id="private-actor",
            actor_type="human",
            action="context_page.updated",
            target_kind="context_page",
            target_type="context_page",
            target_id=seed.id,
            payload={"body": "PRIVATE_AUDIT_PAYLOAD"},
            created_at=occurred_at,
        )
        session.add(audit)
        session.flush()
        seed_id = seed.id
        private_ids = {
            draft.id,
            tool_call.id,
            operation.id,
            audit.id,
        }
        deferred_review_id = decision.id

    request = _current_request(
        seed_id,
        time=GraphTimeQuery(mode="action"),
        layers=["action", "activity", "evidence"],
        edge_kinds=[],
        budget=GraphBudget(nodes=30, edges=30),
    )
    with session_factory() as session:
        service = GraphQueryService()
        viewer = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        context_only_pat = service.query(
            session,
            workspace_id,
            request=request,
            actor=_pat_actor(),
            usage_signals_enabled=True,
        )
        owner = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("owner"),
            usage_signals_enabled=True,
        )

    assert private_ids.isdisjoint({node.entity_id for node in viewer.nodes})
    audit_source_ids = {tool_call.id, operation.id, decision.id, audit.id}
    assert audit_source_ids.isdisjoint(
        {node.entity_id for node in context_only_pat.nodes}
    )
    assert private_ids <= {node.entity_id for node in owner.nodes}
    assert deferred_review_id not in {node.entity_id for node in owner.nodes}
    viewer_json = viewer.model_dump_json()
    assert "PRIVATE_TOOL_BODY" not in viewer_json
    assert "PRIVATE_DIFF" not in viewer_json
    assert "PRIVATE_AUDIT_PAYLOAD" not in viewer_json


def test_as_of_rejects_complete_edge_coverage_without_historical_node_snapshots(
    session_factory,
):
    occurred_at = datetime(2026, 8, 4, 3, 0, tzinfo=UTC)
    as_of = occurred_at + timedelta(minutes=1)
    watermark = as_of + timedelta(minutes=1)
    observations = GraphObservationService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="history-seed",
            title="History Seed",
        )
        target = _page(
            session,
            workspace_id,
            slug="history-target",
            title="History Target",
        )
        observations.record(
            session,
            workspace_id,
            operation="edge_created",
            source_kind="context_page",
            source_id=seed.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="wikilink",
            occurred_at=occurred_at,
            idempotency_key="history-fixture:edge-created",
        )
        seed_id = seed.id
        target_id = target.id

    request = _current_request(
        seed_id,
        time=GraphTimeQuery(mode="as_of", as_of=as_of),
    )
    service = GraphQueryService()
    with session_factory.begin() as session:
        unsupported = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        assert unsupported.history.status == "unsupported"
        assert unsupported.edges == []
        assert unsupported.capabilities.as_of is False

        observations.set_coverage(
            session,
            workspace_id,
            stream_kind="context_page",
            stream_id=seed_id,
            basis="observations",
            status="complete",
            extractor_version="graph-observation.v1",
            coverage_from=occurred_at - timedelta(minutes=1),
            watermark=watermark,
        )
        still_unsupported = service.query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        assert still_unsupported.history.status == "unsupported"
        assert {node.entity_id for node in still_unsupported.nodes} == {seed_id}
        assert still_unsupported.edges == []
        assert still_unsupported.capabilities.as_of is False

        appearance_request = _current_request(
            seed_id,
            time=GraphTimeQuery(mode="appearance"),
        )
        visible_now = service.query(
            session,
            workspace_id,
            request=appearance_request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )
        assert {node.entity_id for node in visible_now.nodes} == {seed_id, target_id}
        assert [edge.edge_kind for edge in visible_now.edges] == ["wikilink"]
        assert visible_now.edges[0].evidence.available is False
        assert visible_now.capabilities.evidence is False

        session.get(ContextPage, target_id).lifecycle_status = "archived"
        session.flush()
        revoked = service.query(
            session,
            workspace_id,
            request=appearance_request,
            actor=_actor("viewer"),
            usage_signals_enabled=True,
        )

    assert {node.entity_id for node in revoked.nodes} == {seed_id}
    assert revoked.edges == []


def test_hidden_seed_is_not_found_for_history_modes(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hidden = _page(
            session,
            workspace_id,
            slug="hidden-history-seed",
            title="Hidden History Seed",
            published=False,
        )
        hidden_id = hidden.id

    with session_factory() as session:
        with pytest.raises(ReadNotFoundError):
            GraphQueryService().query(
                session,
                workspace_id,
                request=_current_request(
                    hidden_id,
                    time=GraphTimeQuery(mode="action"),
                ),
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )


@pytest.mark.parametrize(
    "filters",
    [
        {"node_kinds": ["future_secret_node"]},
        {"edge_kinds": ["future_secret_edge"]},
        {"action_kinds": ["future_secret_action"]},
    ],
)
def test_query_rejects_unregistered_semantic_filter_kinds(
    session_factory,
    filters,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug=f"unknown-filter-{next(iter(filters))}",
            title="Unknown Filter Seed",
        )
        seed_id = seed.id

    request = _current_request(seed_id, **filters)
    with session_factory() as session:
        with pytest.raises(ReadValidationError, match="unsupported graph"):
            GraphQueryService().query(
                session,
                workspace_id,
                request=request,
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )


def test_history_sources_and_timeline_are_bounded_by_safe_public_limits(
    session_factory,
):
    occurred_at = datetime(2026, 8, 4, 4, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="bounded-history-seed",
            title="Bounded History Seed",
        )
        for version_no in range(2, 12):
            session.add(
                ContextPageVersion(
                    workspace_id=seed.workspace_id,
                    page_id=seed.id,
                    version_no=version_no,
                    body_md=f"# Version {version_no}",
                    content_hash=f"bounded-history-{version_no}",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="tester",
                    version_status="superseded_snapshot",
                    created_at=occurred_at + timedelta(minutes=version_no),
                )
            )
        seed_id = seed.id

    request = _current_request(
        seed_id,
        time=GraphTimeQuery(mode="appearance"),
        layers=["action"],
        edge_kinds=[],
        budget=GraphBudget(nodes=2, edges=1),
    )
    statements: list[str] = []
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = GraphQueryService().query(
                session,
                workspace_id,
                request=request,
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    version_queries = [
        statement
        for statement in statements
        if "FROM context_page_version" in statement and "version_status" in statement
    ]
    assert version_queries
    assert all("LIMIT" in statement.upper() for statement in version_queries)
    assert len(payload.nodes) <= request.budget.nodes
    assert len(payload.edges) <= request.budget.edges
    assert len(payload.timeline) <= request.budget.edges
    assert payload.truncated is True
    assert "graph_history_source_bounded" in payload.warnings
    assert all(
        not any(char.isdigit() for char in warning) for warning in payload.warnings
    )


def test_observation_read_has_non_leaky_hard_bound(session_factory):
    occurred_at = datetime(2026, 8, 4, 5, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session,
            workspace_id,
            slug="bounded-observation-seed",
            title="Bounded Observation Seed",
        )
        session.add_all(
            [
                GraphObservation(
                    workspace_id=workspace_id,
                    operation="action_occurred",
                    workspace_sequence=index,
                    source_kind="context_page",
                    source_id=seed.id,
                    action_kind="edited",
                    occurred_at=occurred_at + timedelta(seconds=index),
                    idempotency_key=f"{index:064x}",
                )
                for index in range(1, 258)
            ]
        )
        session.get(Workspace, workspace_id).graph_observation_sequence = 257
        seed_id = seed.id

    request = _current_request(
        seed_id,
        time=GraphTimeQuery(mode="action"),
        layers=["action"],
        edge_kinds=[],
        budget=GraphBudget(nodes=1, edges=1),
    )
    statements: list[str] = []
    with session_factory() as session:
        engine = session.get_bind()

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            payload = GraphQueryService().query(
                session,
                workspace_id,
                request=request,
                actor=_actor("viewer"),
                usage_signals_enabled=True,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    observation_queries = [
        statement for statement in statements if "FROM graph_observation" in statement
    ]
    assert observation_queries
    assert all("LIMIT" in statement.upper() for statement in observation_queries)
    assert len(payload.timeline) == 1
    assert payload.warnings == [
        "graph_history_source_bounded",
        "graph_timeline_catalog_bounded",
        "graph_budget_applied",
    ]


# F08-T5: the budget gives every seed and every relation kind its share
# before the remainder goes by rank.


def _tree_folder(session, workspace_id):
    folder = WorkspaceTreeNode(
        id=uuid4(),
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        sort_key=uuid4().hex,
        title="Budget Folder",
        slug=f"budget-folder-{uuid4()}",
        visibility_status="visible",
    )
    session.add(folder)
    session.flush()
    return folder


def _placement(session, workspace_id, folder, page):
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind="workspace_tree_node",
            source_id=folder.id,
            target_kind="context_page",
            target_id=page.id,
            edge_kind="tree",
            provenance="curated",
            weight=1.0,
            source_ref={},
        )
    )


def _all_relations_request(seed_ids, *, nodes=50, edges=150):
    return GraphQueryRequest(
        seeds=[GraphSeed(kind="context_page", id=seed_id) for seed_id in seed_ids],
        budget=GraphBudget(nodes=nodes, edges=edges),
    )


def _query_scene(session_factory, workspace_id, request, *, usage=True):
    with session_factory() as session:
        return GraphQueryService().query(
            session,
            workspace_id,
            request=request,
            actor=_actor("viewer"),
            usage_signals_enabled=usage,
        )


def _edge_kind_counts(payload):
    counts: dict[str, int] = {}
    for edge in payload.edges:
        counts[edge.edge_kind] = counts.get(edge.edge_kind, 0) + 1
    return counts


def test_edge_budget_reserves_folder_relations(session_factory):
    """A seed's link flood cannot erase its structural folder relations."""

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, slug="budget-seed", title="Budget Seed")
        for index in range(200):
            backlinker = _page(
                session,
                workspace_id,
                slug=f"budget-backlinker-{index}",
                title=f"Budget Backlinker {index}",
            )
            _edge(session, workspace_id, backlinker, seed, weight=5.0)
        for _index in range(3):
            _placement(session, workspace_id, _tree_folder(session, workspace_id), seed)
        for index in range(2):
            cited = _page(
                session,
                workspace_id,
                slug=f"budget-cited-{index}",
                title=f"Budget Cited {index}",
            )
            _edge(session, workspace_id, seed, cited, kind="citation_page")
        seed_id = seed.id

    payload = _query_scene(
        session_factory, workspace_id, _all_relations_request([seed_id], edges=20)
    )

    # F09' fills display edges by class after choosing nodes. The seed class
    # ranks all kinds together; the separate folder class keeps placements.
    assert _edge_kind_counts(payload) == {"wikilink": 17, "tree": 3}
    assert any(
        omission.reason == "relation_quota" and omission.relation_class == "seed"
        for omission in payload.omissions
    )
    assert payload.completeness == "capped"


def test_node_budget_sets_the_guaranteed_share(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, slug="share-seed", title="Share Seed")
        for index in range(50):
            target = _page(
                session, workspace_id, slug=f"share-out-{index}", title=f"Out {index}"
            )
            _edge(session, workspace_id, seed, target)
        for index in range(150):
            backlinker = _page(
                session, workspace_id, slug=f"share-in-{index}", title=f"In {index}"
            )
            _edge(session, workspace_id, backlinker, seed, weight=2.0)
        # X is cited by the seed and links it too, among 30 links, so its
        # backlink ranks below every other and misses the wikilink-in share.
        linker = _page(session, workspace_id, slug="share-x", title="Share X")
        _edge(session, workspace_id, linker, seed)
        for index in range(29):
            elsewhere = _page(
                session,
                workspace_id,
                slug=f"share-x-target-{index}",
                title=f"X Target {index}",
            )
            _edge(session, workspace_id, linker, elsewhere)
        _edge(session, workspace_id, seed, linker, kind="citation_page")
        other_cited = _page(
            session, workspace_id, slug="share-cited", title="Share Cited"
        )
        _edge(session, workspace_id, seed, other_cited, kind="citation_page")
        for _index in range(3):
            _placement(session, workspace_id, _tree_folder(session, workspace_id), seed)
        seed_id, linker_id = seed.id, linker.id

    payload = _query_scene(
        session_factory, workspace_id, _all_relations_request([seed_id], nodes=10)
    )

    # The old node selector's exact queue shares are unit-tested separately.
    # F09' reserves a folder slot, then selects document frontiers before it
    # fills relations between selected endpoints.
    seed_node = graph_node_id("context_page", seed_id)
    linker_node = graph_node_id("context_page", linker_id)
    links_out = [
        edge
        for edge in payload.edges
        if edge.edge_kind == "wikilink" and edge.source == seed_node
    ]
    links_in = [
        edge
        for edge in payload.edges
        if edge.edge_kind == "wikilink" and edge.target == seed_node
    ]
    assert links_out and links_in
    assert linker_node in {node.id for node in payload.nodes}
    assert all(
        edge.source in {node.id for node in payload.nodes}
        and edge.target in {node.id for node in payload.nodes}
        for edge in payload.edges
    )
    assert len(payload.nodes) == 10


def test_expanded_seed_keeps_its_share_next_to_a_hub(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        hub = _page(session, workspace_id, slug="share-hub", title="Share Hub")
        expanded = _page(
            session, workspace_id, slug="share-expanded", title="Share Expanded"
        )
        for index in range(200):
            backlinker = _page(
                session, workspace_id, slug=f"hub-in-{index}", title=f"Hub In {index}"
            )
            _edge(session, workspace_id, backlinker, hub, weight=3.0)
        expanded_neighbours = []
        for index in range(3):
            backlinker = _page(
                session,
                workspace_id,
                slug=f"expanded-in-{index}",
                title=f"Expanded In {index}",
            )
            _edge(session, workspace_id, backlinker, expanded)
            expanded_neighbours.append(backlinker.id)
        hub_id, expanded_id = hub.id, expanded.id

    payload = _query_scene(
        session_factory,
        workspace_id,
        _all_relations_request([hub_id, expanded_id], nodes=20),
    )

    shown = {node.entity_id for node in payload.nodes}
    assert set(expanded_neighbours) <= shown
    assert len(payload.nodes) == 20


def test_domain_tier_round_robin_by_action_kind_then_recency(session_factory):
    occurred_at = datetime(2026, 8, 4, 4, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(
            session, workspace_id, slug="domain-share-seed", title="Domain Share Seed"
        )
        created_version_id = seed.current_version_id
        session.get(ContextPageVersion, created_version_id).created_at = occurred_at
        # Ids below the created version's make an id-ordered budget pick the
        # two oldest edits; the newest edit has the largest id. Five versions
        # stay inside the domain source bound (nodes + edges), which reads the
        # oldest first.
        edited_ids = {}
        for version_no in range(2, 6):
            version_id = (
                UUID(int=2**128 - 1) if version_no == 5 else UUID(int=version_no)
            )
            session.add(
                ContextPageVersion(
                    workspace_id=seed.workspace_id,
                    id=version_id,
                    page_id=seed.id,
                    version_no=version_no,
                    body_md=f"# Version {version_no}",
                    content_hash=f"domain-share-{version_no}",
                    actor_type="human",
                    created_by_actor_kind="human",
                    created_by_actor_id="tester",
                    version_status="superseded_snapshot",
                    created_at=occurred_at + timedelta(minutes=version_no),
                )
            )
            edited_ids[version_no] = version_id
        seed_id = seed.id

    payload = _query_scene(
        session_factory,
        workspace_id,
        _current_request(
            seed_id,
            time=GraphTimeQuery(mode="appearance"),
            layers=["action"],
            edge_kinds=[],
            budget=GraphBudget(nodes=3, edges=2),
        ),
    )

    sources = {
        node.entity_id
        for node in payload.nodes
        if node.entity_kind == "context_page_version"
    }
    assert "graph_history_source_bounded" not in payload.warnings
    assert sources == {created_version_id, edited_ids[5]}
    assert {edge.action_kind for edge in payload.edges} == {"created", "edited"}
    shown_edges = {edge.id for edge in payload.edges}
    assert len(payload.timeline) == 2
    assert all(set(event.edge_ids) <= shown_edges for event in payload.timeline)


def test_equal_rank_selection_is_rebuild_stable(session_factory, monkeypatch):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, slug="stable-seed", title="Stable Seed")
        for index in range(12):
            backlinker = _page(
                session, workspace_id, slug=f"stable-in-{index}", title=f"In {index}"
            )
            _edge(session, workspace_id, backlinker, seed)
        seed_id = seed.id

    request = _all_relations_request([seed_id], nodes=6, edges=5)
    before = [
        edge.id for edge in _query_scene(session_factory, workspace_id, request).edges
    ]
    # Twelve equal ranks and a window of six: the budget must break the tie
    # the way the collector's queue did, or the window would miss its picks.
    with monkeypatch.context() as patch:
        patch.setattr(graph_service_module, "_hydration_window", lambda _budget: 10**9)
        fully_hydrated = [
            edge.id
            for edge in _query_scene(session_factory, workspace_id, request).edges
        ]
    assert fully_hydrated == before
    with session_factory.begin() as session:
        rows = session.scalars(
            select(EdgeProjection).where(EdgeProjection.workspace_id == workspace_id)
        ).all()
        copies = [
            {
                column.key: getattr(row, column.key)
                for column in EdgeProjection.__table__.columns
                if column.key not in {"id", "created_at"}
            }
            for row in rows
        ]
        for row in rows:
            session.delete(row)
        session.flush()
        for values in reversed(copies):
            session.add(EdgeProjection(id=uuid4(), **values))
    after = [
        edge.id for edge in _query_scene(session_factory, workspace_id, request).edges
    ]

    assert len(before) == 5
    assert after == before


def test_disabled_usage_signals_do_not_take_a_queue(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seed = _page(session, workspace_id, slug="quiet-seed", title="Quiet Seed")
        for index in range(10):
            backlinker = _page(
                session, workspace_id, slug=f"quiet-in-{index}", title=f"In {index}"
            )
            _edge(session, workspace_id, backlinker, seed)
            partner = _page(
                session,
                workspace_id,
                slug=f"quiet-partner-{index}",
                title=f"Partner {index}",
            )
            _edge(session, workspace_id, seed, partner, kind="co_access", weight=9.0)
        seed_id = seed.id

    payload = _query_scene(
        session_factory,
        workspace_id,
        _all_relations_request([seed_id], nodes=6),
        usage=False,
    )

    assert _edge_kind_counts(payload) == {"wikilink": 5}
    assert "usage_relation_signals_disabled" in payload.warnings
