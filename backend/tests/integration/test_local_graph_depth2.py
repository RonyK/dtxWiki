"""Local Graph depth and induced relations are actor-visible graph facts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import GraphBudget, GraphQueryRequest, GraphSeed
from backend.services import graph_query_service as query_module
from backend.services import graph_service as graph_service_module
from backend.services.graph_query_service import GraphQueryService, graph_node_id

pytestmark = pytest.mark.integration


def _page(session, workspace_id, name: str, *, published: bool = True):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=f"depth2-{name}-{uuid4().hex}",
        title=name,
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {name}",
        content_hash=f"depth2-{page.id}",
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


def _link(session, workspace_id, source, target, *, weight=1.0):
    session.add(
        EdgeProjection(
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=source.id,
            target_kind="context_page",
            target_id=target.id,
            edge_kind="wikilink",
            provenance="curated",
            weight=weight,
            source_ref={},
        )
    )


def _query(
    session,
    workspace_id,
    seed_id,
    depth,
    *,
    edge_kinds=("wikilink",),
    **request_overrides,
):
    options = dict(
        seeds=[GraphSeed(kind="context_page", id=seed_id)],
        depth=depth,
        edge_kinds=list(edge_kinds),
        layers=["knowledge"],
        budget=GraphBudget(nodes=10, edges=10),
    )
    options.update(request_overrides)
    request = GraphQueryRequest(**options)
    actor = ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role="viewer",
    )
    return GraphQueryService().query(
        session,
        workspace_id,
        request=request,
        actor=actor,
        usage_signals_enabled=False,
    )


def _labels(payload):
    return {node.id: node.label for node in payload.nodes}


def _relations(payload):
    labels = _labels(payload)
    return {(labels[edge.source], labels[edge.target]) for edge in payload.edges}


def test_depth_two_admits_second_hop_and_induced_neighbor_relations(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        left = _page(session, workspace_id, "left")
        right = _page(session, workspace_id, "right")
        second = _page(session, workspace_id, "second")
        _link(session, workspace_id, seed, left)
        _link(session, workspace_id, seed, right)
        _link(session, workspace_id, left, right)  # induced h1 relation
        _link(session, workspace_id, right, second)  # h1 -> h2
        _link(session, workspace_id, left, second)  # another induced h1 -> h2
        seed_id = seed.id

    with session_factory() as session:
        depth1 = _query(session, workspace_id, seed_id, 1)
        depth2 = _query(session, workspace_id, seed_id, 2)

    assert {node.label for node in depth1.nodes} == {"seed", "left", "right"}
    assert ("left", "right") in _relations(depth1)
    assert {node.label for node in depth2.nodes} == {"seed", "left", "right", "second"}
    assert {("right", "second"), ("left", "second")} <= _relations(depth2)
    assert len(depth2.edges) <= depth2.budget.applied.edges

    # Reprojection replaces UUIDv4 rows. Transport ordering and ids must use
    # entity/semantic keys, not the row ids that were just regenerated.
    with session_factory.begin() as session:
        old = session.scalar(
            select(EdgeProjection).where(
                EdgeProjection.workspace_id == workspace_id,
                EdgeProjection.source_id == left.id,
                EdgeProjection.target_id == second.id,
                EdgeProjection.edge_kind == "wikilink",
            )
        )
        session.delete(old)
        session.flush()
        _link(session, workspace_id, left, second)
    with session_factory() as session:
        rebuilt = _query(session, workspace_id, seed_id, 2)
    assert [node.id for node in rebuilt.nodes] == [node.id for node in depth2.nodes]
    assert [edge.id for edge in rebuilt.edges] == [edge.id for edge in depth2.edges]


def test_direct_neighbor_reached_from_first_hop_keeps_shortest_hop(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        neighbors = [
            _page(session, workspace_id, f"neighbor-{index}") for index in range(4)
        ]
        for neighbor in neighbors:
            _link(session, workspace_id, seed, neighbor)
        for source in neighbors:
            for target in neighbors:
                if source.id != target.id:
                    _link(session, workspace_id, source, target)
        seed_id = seed.id
        neighbor_ids = {graph_node_id("context_page", node.id) for node in neighbors}

    actual_hops: dict[str, int] = {}
    select_edges = query_module._select_induced_edges

    def capture_hops(candidates, hops, nodes, edge_budget, depth):
        actual_hops.update(hops)
        return select_edges(candidates, hops, nodes, edge_budget, depth)

    monkeypatch.setattr(query_module, "_select_induced_edges", capture_hops)
    with session_factory() as session:
        payload = _query(
            session,
            workspace_id,
            seed_id,
            2,
            budget=GraphBudget(nodes=5, edges=20),
        )

    assert {node.id for node in payload.nodes} == {
        graph_node_id("context_page", seed_id),
        *neighbor_ids,
    }
    assert {actual_hops[node_id] for node_id in neighbor_ids} == {1}


def test_current_induced_relations_keep_requested_direction(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        neighbor = _page(session, workspace_id, "neighbor")
        _link(session, workspace_id, seed, neighbor)
        _link(session, workspace_id, neighbor, seed)
        seed_id = seed.id

    with session_factory() as session:
        outbound = _query(session, workspace_id, seed_id, 2, direction="out")
        inbound = _query(session, workspace_id, seed_id, 2, direction="in")

    assert _relations(outbound) == {("seed", "neighbor")}
    assert _relations(inbound) == {("neighbor", "seed")}


@pytest.mark.parametrize(
    "filters",
    [
        {"node_kinds": ["context_page"]},
        {"node_families": ["document"]},
        {"edge_kinds": ("wikilink",)},
        {"layers": ["evidence"]},
    ],
)
def test_primary_folder_obeys_graph_filters(session_factory, filters):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        folder = WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            sort_key="folder",
            title="Filtered Folder",
            slug=f"filtered-folder-{uuid4().hex}",
            visibility_status="visible",
        )
        session.add(folder)
        session.flush()
        session.add(
            WorkspaceTreeNode(
                id=uuid4(),
                workspace_id=workspace_id,
                section="context",
                node_kind="page",
                parent_id=folder.id,
                sort_key="seed",
                title=seed.title,
                slug=f"filtered-placement-{uuid4().hex}",
                target_kind="context_page",
                target_id=seed.id,
                primary_placement=True,
                visibility_status="visible",
            )
        )
        seed_id = seed.id

    with session_factory() as session:
        options = dict(filters)
        edge_kinds = options.pop("edge_kinds", ("wikilink", "tree"))
        payload = _query(
            session,
            workspace_id,
            seed_id,
            2,
            edge_kinds=edge_kinds,
            **options,
        )

    assert {node.entity_kind for node in payload.nodes} == {"context_page"}
    assert payload.edges == []


def test_hidden_second_hop_does_not_change_visible_scene_or_omissions(
    session_factory, monkeypatch
):
    fixed = datetime(2026, 9, 28, tzinfo=UTC)
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: fixed)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        first = _page(session, workspace_id, "first")
        second = _page(session, workspace_id, "second")
        _link(session, workspace_id, seed, first)
        _link(session, workspace_id, first, second)
        seed_id = seed.id
        before = _query(session, workspace_id, seed_id, 2).model_dump_json()

        hidden = _page(session, workspace_id, "PRIVACY SENTINEL", published=False)
        _link(session, workspace_id, first, hidden, weight=99999)
        for index in range(1_024):
            decoy = ContextPage(
                id=uuid4(),
                workspace_id=workspace_id,
                slug=f"depth2-private-{index}",
                title=f"PRIVACY SENTINEL {index}",
                page_type="concept",
                lifecycle_status="draft",
            )
            session.add(decoy)
            _link(session, workspace_id, first, decoy, weight=99999 - index)
        session.flush()
        after = _query(session, workspace_id, seed_id, 2).model_dump_json()

    assert after == before
    assert "PRIVACY SENTINEL" not in after


def test_second_hop_primary_folder_uses_reserved_slot_and_membership(session_factory):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        first = _page(session, workspace_id, "first")
        second = _page(session, workspace_id, "second")
        _link(session, workspace_id, seed, first)
        _link(session, workspace_id, first, second)
        folder = WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            sort_key="folder",
            title="Second Folder",
            slug=f"second-folder-{uuid4().hex}",
            visibility_status="visible",
        )
        session.add(folder)
        session.flush()
        placement = WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="page",
            parent_id=folder.id,
            sort_key="placement",
            title=second.title,
            slug=f"second-placement-{uuid4().hex}",
            target_kind="context_page",
            target_id=second.id,
            primary_placement=True,
            visibility_status="visible",
        )
        session.add(placement)
        seed_id = seed.id

    with session_factory() as session:
        payload = _query(
            session, workspace_id, seed_id, 2, edge_kinds=("wikilink", "tree")
        )
        outbound = _query(
            session,
            workspace_id,
            seed_id,
            2,
            edge_kinds=("wikilink", "tree"),
            direction="out",
        )

    assert "Second Folder" in {node.label for node in payload.nodes}
    assert ("Second Folder", "second") in _relations(payload)
    assert ("Second Folder", "second") not in _relations(outbound)
    assert not any(omission.reason == "folder_node" for omission in payload.omissions)


def test_primary_folder_hydration_cap_marks_scene_as_bounded(
    session_factory, monkeypatch
):
    monkeypatch.setattr(graph_service_module, "GRAPH_TREE_HYDRATION_NODE_LIMIT", 2)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        peer = _page(session, workspace_id, "peer")
        _link(session, workspace_id, seed, peer)
        for index, page in enumerate((seed, peer)):
            folder = WorkspaceTreeNode(
                id=uuid4(),
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                sort_key=f"folder-{index}",
                title=f"Folder {index}",
                slug=f"folder-cap-{index}-{uuid4().hex}",
                visibility_status="visible",
            )
            session.add(folder)
            session.flush()
            session.add(
                WorkspaceTreeNode(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="page_ref",
                    parent_id=folder.id,
                    sort_key=f"placement-{index}",
                    title=page.title,
                    slug=f"placement-cap-{index}-{uuid4().hex}",
                    target_kind="context_page",
                    target_id=page.id,
                    primary_placement=True,
                    visibility_status="visible",
                )
            )
        seed_id = seed.id

    with session_factory() as session:
        payload = _query(
            session,
            workspace_id,
            seed_id,
            1,
            edge_kinds=("wikilink", "tree"),
            budget=GraphBudget(nodes=6, edges=10),
        )

    assert {node.label for node in payload.nodes} == {"seed", "peer"}
    assert any(
        omission.reason == "folder_source_cap" and omission.scope == "node"
        for omission in payload.omissions
    )
    assert payload.completeness == "bounded_sample"
    assert payload.truncated is True
    assert "graph_projection_source_bounded" in payload.warnings
    assert "graph_budget_applied" not in payload.warnings
