"""Current relation periods select active occurrences, never historical state."""

from __future__ import annotations

from datetime import timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
    PageLink,
)
from backend.models.graph import GraphObservation
from backend.models.workspace import Workspace
from backend.schemas.graph import GraphQueryPayload
from backend.services.graph_observation_service import GraphObservationService
from backend.services.graph_query_service import graph_node_id
from backend.services.page_service import sync_page_projections
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.utils.markdown_links import rendered_wikilinks

pytestmark = pytest.mark.integration


@pytest.fixture
def graph_client(page_service):
    with TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="current-relation-window-test-secret",
            ),
            page_service=page_service,
        )
    ) as client:
        workspace_id = authenticate_test_client_in_workspace(
            client,
            page_service.session_factory,
            email="current-window-viewer@example.com",
            workspace_slug="default",
            role="viewer",
        )
        yield client, workspace_id


def _publish(page_service, slug, body, *, previous=None):
    return page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug,
        body_md=body,
        actor_kind="human",
        actor_id="current-window-publisher",
        actor_provenance="browser_session",
        base_version_id=None if previous is None else previous.version_id,
        base_content_hash=None if previous is None else previous.content_hash,
    )


def _observations(page_service, source_id):
    with page_service.session_factory() as session:
        return list(
            session.scalars(
                select(GraphObservation)
                .where(
                    GraphObservation.source_kind == "context_page",
                    GraphObservation.source_id == source_id,
                    GraphObservation.edge_kind == "wikilink",
                )
                .order_by(GraphObservation.workspace_sequence)
            ).all()
        )


def _query(graph_client, seed_id, *, start=None, end=None, mode="current", **overrides):
    client, workspace_id = graph_client
    time = {"mode": mode}
    if start is not None:
        time["from"] = start.isoformat()
    if end is not None:
        time["to"] = end.isoformat()
    request = {
        "seeds": [{"kind": "context_page", "id": str(seed_id)}],
        "depth": 2,
        "direction": "both",
        "preset": "linked-knowledge",
        "layers": ["knowledge"],
        "edge_kinds": ["wikilink"],
        "budget": {"nodes": 10, "edges": 10},
        "time": time,
    }
    request.update(overrides)
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/query", json=request
    )
    assert response.status_code == 200, response.text
    return GraphQueryPayload.model_validate(response.json()["data"])


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize(
    "seed_side,direction", [("source", "out"), ("target", "in"), ("source", "both")]
)
@pytest.mark.parametrize("window", ["closed", "upper_only"])
def test_current_creation_window_cannot_resurrect_a_published_link_removed_later(
    page_service, graph_client, depth, seed_side, direction, window
):
    target = _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created = _observations(page_service, source.id)[0]
    _publish(page_service, "period-source", "The link is gone.", previous=source)
    rows = _observations(page_service, source.id)
    assert [row.operation for row in rows] == ["edge_created", "edge_removed"]
    assert rows[1].occurred_at > created.occurred_at
    with page_service.session_factory() as session:
        assert (
            session.scalar(
                select(EdgeProjection.id).where(
                    EdgeProjection.source_id == source.id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            is None
        )

    seed = source if seed_side == "source" else target
    payload = _query(
        graph_client,
        seed.id,
        start=created.occurred_at if window == "closed" else None,
        end=created.occurred_at,
        depth=depth,
        direction=direction,
    )

    assert payload.edges == []
    assert {node.entity_id for node in payload.nodes} == {seed.id}
    assert payload.timeline == []
    assert payload.completeness == "complete"
    assert payload.truncated is False
    assert payload.history.mode == "current"
    assert payload.history.basis == "current_projection"
    assert payload.omissions == []


def test_current_readded_relation_is_not_selected_by_its_old_creation_period(
    page_service, graph_client
):
    _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    first_created = _observations(page_service, source.id)[0].occurred_at
    removed = _publish(page_service, "period-source", "Removed.", previous=source)
    _publish(
        page_service,
        "period-source",
        "See [[period-target]] again.",
        previous=removed,
    )
    rows = _observations(page_service, source.id)
    assert [row.operation for row in rows] == [
        "edge_created",
        "edge_removed",
        "edge_created",
    ]
    assert rows[2].occurred_at > first_created + timedelta(microseconds=1)

    old_period = _query(graph_client, source.id, start=first_created, end=first_created)
    latest_period = _query(
        graph_client, source.id, start=rows[2].occurred_at, end=rows[2].occurred_at
    )
    assert old_period.edges == []
    assert len(latest_period.edges) == 1
    assert latest_period.edges[0].occurred_at == rows[2].occurred_at


@pytest.mark.parametrize("offset_hours", [0, 9, -5])
@pytest.mark.parametrize("window", ["closed", "lower_only", "upper_only"])
def test_active_link_occurrence_period_is_inclusive_and_normalized_to_utc(
    page_service, graph_client, offset_hours, window
):
    target = _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created_at = _observations(page_service, source.id)[0].occurred_at
    offset_at = created_at.astimezone(timezone(timedelta(hours=offset_hours)))
    unfiltered = _query(graph_client, source.id)
    # Wikilink projections have no occurrence timestamp, so dated reads use
    # the authored ledger while undated reads prefer the curated projection.
    assert unfiltered.edges[0].occurred_at is None
    selected = _query(
        graph_client,
        source.id,
        start=None if window == "upper_only" else offset_at,
        end=None if window == "lower_only" else offset_at,
    )
    utc_selected = _query(
        graph_client,
        source.id,
        start=None if window == "upper_only" else created_at,
        end=None if window == "lower_only" else created_at,
    )
    earlier = _query(graph_client, source.id, end=offset_at - timedelta(microseconds=1))
    later = _query(
        graph_client,
        source.id,
        start=offset_at + timedelta(microseconds=1),
    )

    assert selected.edges == utc_selected.edges
    assert len(selected.edges) == 1
    assert selected.edges[0].id == unfiltered.edges[0].id
    assert selected.edges[0].provenance == "observed"
    assert selected.edges[0].occurred_at == created_at
    if window != "lower_only":
        assert selected.edges[0].occurred_at <= created_at
    if window != "upper_only":
        assert selected.edges[0].occurred_at >= created_at
    assert {node.entity_id for node in selected.nodes} == {source.id, target.id}
    assert selected.completeness == "complete"
    assert earlier.edges == later.edges == []
    assert {node.entity_id for node in earlier.nodes} == {source.id}
    assert {node.entity_id for node in later.nodes} == {source.id}


def test_unchanged_relation_keeps_its_original_occurrence_after_a_later_publication(
    page_service, graph_client
):
    _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created_at = _observations(page_service, source.id)[0].occurred_at
    before = _query(graph_client, source.id, start=created_at, end=created_at)
    updated = _publish(
        page_service,
        "period-source",
        "See [[period-target]].\n\nThe prose changed, not the relation.",
        previous=source,
    )
    assert updated.version_id != source.version_id
    assert len(_observations(page_service, source.id)) == 1
    after = _query(graph_client, source.id, start=created_at, end=created_at)

    assert after.edges == before.edges
    assert len(after.edges) == 1
    assert after.edges[0].occurred_at == created_at


def test_current_state_and_appearance_event_windows_remain_distinct_after_removal(
    page_service, graph_client
):
    target = _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    _publish(page_service, "period-source", "Removed.", previous=source)
    created, removed = _observations(page_service, source.id)
    current = _query(
        graph_client, source.id, start=created.occurred_at, end=created.occurred_at
    )
    created_history = _query(
        graph_client,
        source.id,
        start=created.occurred_at,
        end=created.occurred_at,
        mode="appearance",
    )
    removed_history = _query(
        graph_client,
        source.id,
        start=removed.occurred_at,
        end=removed.occurred_at,
        mode="appearance",
    )
    full_history = _query(graph_client, source.id, mode="appearance")
    full_period_history = _query(
        graph_client,
        source.id,
        start=created.occurred_at,
        end=removed.occurred_at,
        mode="appearance",
    )

    assert current.edges == current.timeline == []
    assert [event.operation for event in created_history.timeline] == ["edge_created"]
    historical_edge = created_history.timeline[0].edge_snapshots[0]
    assert historical_edge.occurred_at == created.occurred_at
    assert target.id in {
        node.entity_id
        for node in (created_history.nodes + created_history.timeline[0].node_snapshots)
    }
    # A removal-only catalog has no prior edge/node snapshot to address. It
    # keeps the existing honest omission rather than inventing that history.
    assert removed_history.timeline == []
    assert removed_history.edges == []
    assert removed_history.truncated is True
    assert "graph_timeline_catalog_bounded" in removed_history.warnings
    assert full_history.edges == []
    assert [event.operation for event in full_history.timeline] == [
        "edge_created",
        "edge_removed",
    ]
    assert full_period_history.edges == full_history.edges
    assert full_period_history.timeline == full_history.timeline
    assert full_history.timeline[1].edge_snapshots == []
    assert full_history.timeline[1].edge_ids == [historical_edge.id]
    assert full_history.history.status == "partial"
    assert full_history.capabilities.as_of is False


@pytest.mark.parametrize("mode", ["appearance", "action"])
def test_history_publication_actions_stay_in_the_requested_period(
    page_service, graph_client, mode
):
    _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    _publish(page_service, "period-source", "Removed.", previous=source)
    created, removed = _observations(page_service, source.id)
    history = _query(
        graph_client,
        source.id,
        start=removed.occurred_at,
        end=removed.occurred_at,
        mode=mode,
        preset="all",
        layers=["knowledge", "action"],
        edge_kinds=[],
    )
    creation_actions = _query(
        graph_client,
        source.id,
        start=created.occurred_at,
        end=created.occurred_at,
        mode=mode,
        preset="all",
        layers=["action"],
        action_kinds=["created"],
        edge_kinds=[],
    )

    assert history.timeline
    assert all(event.occurred_at == removed.occurred_at for event in history.timeline)
    assert any(event.action_kind == "edited" for event in history.timeline)
    assert not any(edge.edge_kind == "wikilink" for edge in history.edges)
    assert creation_actions.timeline
    assert {event.action_kind for event in creation_actions.timeline} == {"created"}
    assert all(
        event.occurred_at == created.occurred_at for event in creation_actions.timeline
    )
    if mode == "appearance":
        assert "graph_timeline_catalog_bounded" in history.warnings
    else:
        assert all(event.operation == "action_occurred" for event in history.timeline)


@pytest.mark.parametrize("final_state", ["removed", "readded"])
def test_current_bounded_tail_uses_latest_state_and_keeps_source_caps_truthful(
    page_service, graph_client, final_state
):
    _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    first_created = _observations(page_service, source.id)[0].occurred_at
    for index in range(4):
        source = _publish(
            page_service, "period-source", f"Removed {index}.", previous=source
        )
        if index != 3 or final_state == "readded":
            source = _publish(
                page_service,
                "period-source",
                f"See [[period-target]] again {index}.",
                previous=source,
            )
    rows = _observations(page_service, source.id)
    assert len(rows) > 3  # budget N=2 + E=1 bounds the visible ledger tail to 3.
    before_period = _query(
        graph_client,
        source.id,
        start=first_created,
        end=first_created,
        budget={"nodes": 2, "edges": 1},
    )
    latest_period = _query(
        graph_client,
        source.id,
        start=rows[-1].occurred_at,
        end=rows[-1].occurred_at,
        budget={"nodes": 2, "edges": 1},
    )

    assert before_period.edges == []
    assert len(latest_period.edges) == (1 if final_state == "readded" else 0)
    if latest_period.edges:
        assert latest_period.edges[0].occurred_at == rows[-1].occurred_at
    for payload in (before_period, latest_period):
        assert payload.completeness == "bounded_sample"
        assert payload.truncated is True
        assert "graph_history_source_bounded" in payload.warnings
        assert all(
            not any(char.isdigit() for char in item) for item in payload.warnings
        )
        assert len(payload.nodes) <= 2
        assert len(payload.edges) <= 1


def test_current_raw_scan_cap_cannot_drop_a_latest_removal_and_claim_completeness(
    page_service, graph_client, monkeypatch
):
    _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created_at = _observations(page_service, source.id)[0].occurred_at
    _publish(page_service, "period-source", "Removed.", previous=source)
    monkeypatch.setattr(
        "backend.services.graph_query_service.GRAPH_OBSERVATION_RAW_SCAN_LIMIT", 1
    )
    observation_sql = []
    engine = page_service.session_factory.kw["bind"]

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM graph_observation" in statement:
            observation_sql.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        payload = _query(graph_client, source.id, start=created_at, end=created_at)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert payload.edges == []
    assert payload.completeness == "bounded_sample"
    assert payload.truncated is True
    assert "graph_history_source_bounded" in payload.warnings
    assert observation_sql
    assert all("LIMIT" in statement.upper() for statement in observation_sql)
    assert "graph_observation.occurred_at >=" not in observation_sql[0]
    assert "graph_observation.occurred_at <=" not in observation_sql[0]


def test_current_max_budget_replay_is_still_bounded_to_the_latest_1000_observations(
    page_service, graph_client
):
    # One real publication creates 501 links; the next removes all 501.
    # N=250 + E=750 hits the hard visible-observation ceiling, independently
    # of the much smaller cap exercised above. Every latest removal fits in
    # the newest 1,000 rows even though the oldest creations do not.
    target_slugs = [f"ceiling-target-{index}" for index in range(501)]
    for slug in target_slugs:
        _publish(page_service, slug, f"# {slug}")
    source = _publish(
        page_service,
        "ceiling-source",
        "\n".join(f"See [[{slug}]]." for slug in target_slugs),
    )
    created_rows = _observations(page_service, source.id)
    assert len(created_rows) == 501
    created_at = created_rows[0].occurred_at
    _publish(page_service, "ceiling-source", "All links removed.", previous=source)
    rows = _observations(page_service, source.id)
    assert len(rows) == 1_002
    assert all(row.operation == "edge_removed" for row in rows[-501:])

    payload = _query(
        graph_client,
        source.id,
        start=created_at,
        end=created_at,
        budget={"nodes": 250, "edges": 750},
    )

    assert payload.edges == payload.timeline == []
    assert {node.entity_id for node in payload.nodes} == {source.id}
    assert payload.completeness == "bounded_sample"
    assert payload.truncated is True
    assert "graph_history_source_bounded" in payload.warnings
    assert payload.budget.applied.nodes == 250
    assert payload.budget.applied.edges == 750


def test_current_period_preserves_endpoint_visibility_and_hidden_history_does_not_cap(
    page_service, graph_client, monkeypatch
):
    _publish(page_service, "period-target", "# Target")
    hidden = _publish(page_service, "period-hidden", "# Hidden")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created_at = _observations(page_service, source.id)[0].occurred_at
    monkeypatch.setattr(
        "backend.services.graph_query_service.utc_now", lambda: created_at
    )
    baseline = _query(graph_client, source.id, start=created_at, end=created_at)
    for index in range(4):
        source = _publish(
            page_service,
            "period-source",
            f"See [[period-target]] and [[period-hidden]]. {index}",
            previous=source,
        )
        source = _publish(
            page_service,
            "period-source",
            f"See [[period-target]]. Hidden link removed {index}.",
            previous=source,
        )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, hidden.id)
        page.lifecycle_status = "draft"
        page.current_published_version_id = None
        page.latest_draft_version_id = page.current_version_id
    hidden_history = _query(
        graph_client,
        source.id,
        start=created_at,
        end=created_at,
        budget={"nodes": 2, "edges": 1},
    )

    assert hidden_history.nodes == baseline.nodes
    assert hidden_history.edges == baseline.edges
    assert hidden_history.facets == baseline.facets
    assert hidden_history.completeness == baseline.completeness == "complete"
    assert hidden_history.warnings == baseline.warnings == []
    assert hidden_history.omissions == baseline.omissions == []
    assert str(hidden.id) not in hidden_history.model_dump_json()


def test_current_period_still_honors_layers_and_workspace_usage_opt_out(
    page_service, graph_client
):
    target = _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created_at = _observations(page_service, source.id)[0].occurred_at
    workspace_id = UUID(graph_client[1])
    with page_service.session_factory.begin() as session:
        GraphObservationService().record(
            session,
            workspace_id,
            operation="edge_created",
            source_kind="context_page",
            source_id=source.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="co_access",
            occurred_at=created_at,
            valid_from=created_at,
            idempotency_key="current-period-usage-relation",
        )
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = True
    values = {
        "start": created_at,
        "end": created_at,
        "preset": "all",
        "layers": ["knowledge", "activity"],
        "edge_kinds": ["wikilink", "co_access"],
    }
    enabled = _query(graph_client, source.id, **values)
    enabled_history = _query(graph_client, source.id, mode="appearance", **values)
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = False
    disabled = _query(graph_client, source.id, **values)
    disabled_history = _query(graph_client, source.id, mode="appearance", **values)
    action_only = _query(
        graph_client,
        source.id,
        start=created_at,
        end=created_at,
        preset="all",
        layers=["action"],
        edge_kinds=[],
    )

    assert {edge.edge_kind for edge in enabled.edges} == {"wikilink", "co_access"}
    assert [edge.edge_kind for edge in disabled.edges] == ["wikilink"]
    assert "usage_relation_signals_disabled" in disabled.warnings
    assert {
        edge.edge_kind
        for item in enabled_history.timeline
        for edge in item.edge_snapshots
    } == {"wikilink", "co_access"}
    assert [edge.edge_kind for edge in disabled_history.edges] == ["wikilink"]
    assert {
        edge.edge_kind
        for item in disabled_history.timeline
        for edge in item.edge_snapshots
    } == {"wikilink"}
    assert "usage_relation_signals_disabled" in disabled_history.warnings
    assert action_only.edges == action_only.timeline == []
    assert {node.entity_id for node in action_only.nodes} == {source.id}


def test_disabled_usage_history_outside_period_cannot_evict_an_active_authored_link(
    page_service, graph_client
):
    target = _publish(page_service, "period-target", "# Target")
    source = _publish(page_service, "period-source", "See [[period-target]].")
    created_at = _observations(page_service, source.id)[0].occurred_at
    workspace_id = UUID(graph_client[1])
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).usage_relation_signals_enabled = False
    values = {
        "start": created_at,
        "end": created_at,
        "preset": "all",
        "layers": ["knowledge", "activity"],
        "edge_kinds": ["wikilink", "co_access"],
        "budget": {"nodes": 2, "edges": 1},
    }
    baseline = _query(graph_client, source.id, **values)
    with page_service.session_factory.begin() as session:
        observations = GraphObservationService()
        for index in range(1, 5):
            at = created_at + timedelta(microseconds=index)
            observations.record(
                session,
                workspace_id,
                operation="edge_created",
                source_kind="context_page",
                source_id=source.id,
                target_kind="context_page",
                target_id=target.id,
                edge_kind="co_access",
                occurred_at=at,
                valid_from=at,
                idempotency_key=f"current-period-disabled-usage-{index}",
            )
    flooded = _query(graph_client, source.id, **values)

    assert [edge.edge_kind for edge in baseline.edges] == ["wikilink"]
    assert flooded.nodes == baseline.nodes
    assert flooded.edges == baseline.edges
    assert flooded.completeness == baseline.completeness == "complete"
    assert flooded.truncated is baseline.truncated is False
    assert flooded.warnings == baseline.warnings == ["usage_relation_signals_disabled"]
    assert flooded.omissions == baseline.omissions == []


@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize(
    "knobs", ["authored_only", "knowledge_defaults", "mixed_layers"]
)
def test_current_period_keeps_authored_relations_at_the_second_frontier(
    page_service, graph_client, direction, knobs
):
    leaf = _publish(page_service, "window-leaf", "# Leaf")
    neighbor = _publish(page_service, "window-neighbor", "See [[window-leaf]].")
    seed = _publish(page_service, "window-seed", "See [[window-neighbor]].")
    first_at = _observations(page_service, neighbor.id)[0].occurred_at
    last_at = _observations(page_service, seed.id)[0].occurred_at
    query_seed = leaf if direction == "in" else seed
    overrides = {"direction": direction}
    if knobs == "knowledge_defaults":
        overrides["edge_kinds"] = []
    elif knobs == "mixed_layers":
        overrides.update(preset="all", layers=["knowledge", "activity"])
    unfiltered = _query(graph_client, query_seed.id, **overrides)
    filtered = _query(
        graph_client, query_seed.id, start=first_at, end=last_at, **overrides
    )
    expected_ids = {seed.id, neighbor.id, leaf.id}
    # Default knowledge also includes legal structural context. Count the
    # authored document path independently of folder/tree presentation.
    for payload in (unfiltered, filtered):
        assert {
            node.entity_id
            for node in payload.nodes
            if node.entity_kind == "context_page"
        } == expected_ids
        assert (
            len([edge for edge in payload.edges if edge.edge_kind == "wikilink"]) == 2
        )
        assert payload.completeness == "complete"
        assert payload.warnings == payload.omissions == []
    assert all(
        edge.occurred_at is not None
        for edge in filtered.edges
        if edge.edge_kind == "wikilink"
    )


@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize("depth", [1, 2])
def test_current_period_preserves_induced_h1_and_h2_relations(
    page_service, graph_client, direction, depth
):
    pages = {
        name: _publish(page_service, f"induced-{name}", f"# {name}")
        for name in ("seed", "left", "right", "leaf-one", "leaf-two")
    }
    pairs = [
        ("seed", "left"),
        ("seed", "right"),
        ("left", "right"),
        ("left", "leaf-one"),
        ("right", "leaf-two"),
        ("leaf-one", "leaf-two"),
    ]
    if direction == "in":
        pairs = [(target, source) for source, target in pairs]
    for name, page in list(pages.items()):
        targets = [target for source, target in pairs if source == name]
        if targets:
            pages[name] = _publish(
                page_service,
                f"induced-{name}",
                "\n".join(f"See [[induced-{target}]]." for target in targets),
                previous=page,
            )
    observations = [
        row for page in pages.values() for row in _observations(page_service, page.id)
    ]
    start = min(row.occurred_at for row in observations)
    end = max(row.occurred_at for row in observations)
    unfiltered = _query(
        graph_client, pages["seed"].id, direction=direction, depth=depth
    )
    filtered = _query(
        graph_client,
        pages["seed"].id,
        direction=direction,
        depth=depth,
        start=start,
        end=end,
    )
    selected_names = {"seed", "left", "right"}
    if depth == 2:
        selected_names |= {"leaf-one", "leaf-two"}
    expected_pairs = {
        (
            graph_node_id("context_page", pages[source].id),
            graph_node_id("context_page", pages[target].id),
        )
        for source, target in pairs
        if source in selected_names and target in selected_names
    }
    for payload in (unfiltered, filtered):
        assert {node.entity_id for node in payload.nodes} == {
            pages[name].id for name in selected_names
        }
        assert {(edge.source, edge.target) for edge in payload.edges} == expected_pairs
        assert payload.completeness == "complete"
        assert payload.warnings == payload.omissions == []


def test_rebuilt_reader_links_override_current_but_not_immutable_history(
    page_service, graph_client, monkeypatch
):
    target = _publish(page_service, "legacy-highlight-target", "# Target")
    original_body = "==Read [[legacy-highlight-target]] first=="
    assert rendered_wikilinks(original_body) == ()
    # Simulate only the old highlight-oblivious extractor. Same-width
    # delimiters preserve source spans; the real publication pipeline writes
    # the old projection and immutable evidence, rather than a fake response.
    with monkeypatch.context() as legacy:
        legacy.setattr(
            "backend.services.page_service.rendered_wikilinks",
            lambda body, **_kwargs: rendered_wikilinks(body.replace("==", "  ")),
        )
        source = _publish(page_service, "legacy-highlight-source", original_body)
    assert [item.id for item in page_service.list_backlinks(target.id)] == [source.id]
    created = _observations(page_service, source.id)
    assert len(created) == 1
    assert created[0].operation == "edge_created"
    assert len(_query(graph_client, source.id).edges) == 1
    root = _publish(
        page_service, "legacy-highlight-root", "See [[legacy-highlight-source]]."
    )
    root_at = _observations(page_service, root.id)[0].occurred_at
    assert len(_query(graph_client, root.id).edges) == 2
    before_history = _query(
        graph_client,
        source.id,
        mode="appearance",
        start=created[0].occurred_at,
        end=created[0].occurred_at,
    )

    # This is the shared seam used by reindex_workspace. Correcting current
    # projections must neither erase evidence nor invent removal events.
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, source.id)
        version = session.get(ContextPageVersion, page.current_published_version_id)
        assert version.body_md == original_body
        sync_page_projections(
            session, page=page, version=version, record_graph_observations=False
        )
    assert page_service.list_backlinks(target.id) == []
    with page_service.session_factory() as session:
        assert (
            session.scalar(
                select(func.count(PageLink.id)).where(
                    PageLink.source_page_id == source.id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(EdgeProjection.id)).where(
                    EdgeProjection.source_id == source.id,
                    EdgeProjection.edge_kind == "wikilink",
                )
            )
            == 0
        )
    after_rows = _observations(page_service, source.id)
    assert [(row.id, row.operation, row.workspace_sequence) for row in after_rows] == [
        (row.id, row.operation, row.workspace_sequence) for row in created
    ]
    for period in (
        {},
        {"start": created[0].occurred_at, "end": created[0].occurred_at},
    ):
        # The added root has a legitimate inbound link. This direct negative
        # control selects only the corrected source's authored outgoing set.
        current = _query(graph_client, source.id, direction="out", **period)
        assert current.edges == []
        assert {node.entity_id for node in current.nodes} == {source.id}
        assert current.completeness == "complete"
        assert current.warnings == current.omissions == []
    for period in ({}, {"start": created[0].occurred_at, "end": root_at}):
        current = _query(
            graph_client,
            root.id,
            budget={"nodes": 3, "edges": 3},
            **period,
        )
        assert {node.entity_id for node in current.nodes} == {root.id, source.id}
        assert len(current.edges) == 1
        assert current.edges[0].target == graph_node_id("context_page", source.id)
        assert current.completeness == "complete"
        assert current.warnings == current.omissions == []
    after_history = _query(
        graph_client,
        source.id,
        mode="appearance",
        start=created[0].occurred_at,
        end=created[0].occurred_at,
    )
    # History still exposes the same immutable occurrence. Its display rank
    # may change when current projection denominators are rebuilt.
    assert [
        (
            edge.id,
            edge.source,
            edge.target,
            edge.occurred_at,
            edge.sequence,
            edge.evidence,
        )
        for edge in after_history.edges
    ] == [
        (
            edge.id,
            edge.source,
            edge.target,
            edge.occurred_at,
            edge.sequence,
            edge.evidence,
        )
        for edge in before_history.edges
    ]
    assert after_history.timeline == before_history.timeline
    assert len(after_history.edges) == 1


def test_current_period_h1_hidden_history_cannot_spend_the_shared_scan_budget(
    page_service, graph_client, monkeypatch
):
    leaf = _publish(page_service, "h1-visible-leaf", "# Leaf")
    hidden = _publish(page_service, "h1-hidden-leaf", "# Hidden")
    neighbor = _publish(page_service, "h1-neighbor", "See [[h1-visible-leaf]].")
    seed = _publish(page_service, "h1-seed", "See [[h1-neighbor]].")
    start = _observations(page_service, neighbor.id)[0].occurred_at
    end = _observations(page_service, seed.id)[0].occurred_at
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: end)
    options = {"start": start, "end": end, "budget": {"nodes": 3, "edges": 2}}
    baseline = _query(graph_client, seed.id, **options)
    assert {node.entity_id for node in baseline.nodes} == {
        seed.id,
        neighbor.id,
        leaf.id,
    }
    for index in range(4):
        neighbor = _publish(
            page_service,
            "h1-neighbor",
            f"See [[h1-visible-leaf]] and [[h1-hidden-leaf]]. {index}",
            previous=neighbor,
        )
        neighbor = _publish(
            page_service,
            "h1-neighbor",
            f"See [[h1-visible-leaf]]. {index}",
            previous=neighbor,
        )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, hidden.id)
        page.lifecycle_status = "draft"
        page.current_published_version_id = None
        page.latest_draft_version_id = page.current_version_id
    flooded = _query(graph_client, seed.id, **options)
    assert flooded.nodes == baseline.nodes
    assert flooded.edges == baseline.edges
    assert flooded.facets == baseline.facets
    assert flooded.completeness == baseline.completeness == "complete"
    assert flooded.warnings == baseline.warnings == []
    assert flooded.omissions == baseline.omissions == []
    assert str(hidden.id) not in flooded.model_dump_json()


@pytest.mark.parametrize("source_limit", ["raw", "pages"])
def test_current_period_frontiers_share_one_bounded_evidence_catalog(
    page_service, graph_client, monkeypatch, source_limit
):
    from backend.services import graph_query_service as query_module

    leaf = _publish(page_service, "scan-leaf", "# Leaf")
    neighbor = _publish(page_service, "scan-neighbor", "See [[scan-leaf]].")
    seed = _publish(page_service, "scan-seed", "See [[scan-neighbor]].")
    start = _observations(page_service, neighbor.id)[0].occurred_at
    end = _observations(page_service, seed.id)[0].occurred_at
    snapshots = []
    ordered = query_module._ObservationScanState.ordered

    def capture(scan):
        snapshots.append(
            (id(scan), scan.raw_scanned, scan.pages_scanned, len(scan.visible))
        )
        return ordered(scan)

    monkeypatch.setattr(query_module._ObservationScanState, "ordered", capture)
    limit_name = (
        "GRAPH_OBSERVATION_RAW_SCAN_LIMIT"
        if source_limit == "raw"
        else "GRAPH_OBSERVATION_PAGE_LIMIT"
    )
    monkeypatch.setattr(query_module, limit_name, 2)
    payload = _query(graph_client, seed.id, start=start, end=end)
    # Seed and H1 reads already consume the entire shared raw/page budget.
    # The selected induced read may reuse evidence, never start a fresh cap.
    assert {node.entity_id for node in payload.nodes} == {seed.id, neighbor.id, leaf.id}
    assert len(payload.edges) == 2
    assert payload.completeness == "bounded_sample"
    assert payload.truncated is True
    assert payload.warnings == ["graph_history_source_bounded"]
    assert len({identity for identity, *_counts in snapshots}) == 1
    assert max(raw for _identity, raw, _pages, _visible in snapshots) == 2
    assert max(pages for _identity, _raw, pages, _visible in snapshots) == 2
    assert max(visible for _identity, _raw, _pages, visible in snapshots) == 2
    assert len(payload.nodes) <= payload.budget.applied.nodes
    assert len(payload.edges) <= payload.budget.applied.edges
