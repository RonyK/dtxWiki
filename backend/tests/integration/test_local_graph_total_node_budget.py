"""F09' has one total node cap: seeds, documents and primary folders share it."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryPayload,
    GraphQueryRequest,
    GraphSeed,
)
from backend.services.graph_query_service import GraphQueryService
from backend.services.read_errors import ReadNotFoundError
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 3, tzinfo=UTC)
_NAMESPACE = UUID("238c7623-748c-4242-a66e-108bd2163b30")
_ACTOR = ActorContext(
    actor_kind="human",
    actor_id="total-node-budget-viewer",
    token_kind="session",
    scopes=(),
    workspace_role="viewer",
)


def _id(workspace_id, name):
    return uuid5(_NAMESPACE, f"{workspace_id}:{name}")


def _page(session, workspace_id, name, *, published=True):
    page = ContextPage(
        id=_id(workspace_id, name),
        workspace_id=workspace_id,
        slug=name,
        title=name,
        page_type="concept",
        lifecycle_status="published" if published else "draft",
        created_at=_NOW,
    )
    session.add(page)
    session.flush()
    body = f"# {name}"
    head = ContextPageVersion(
        id=_id(workspace_id, f"head:{name}"),
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="total-node-budget",
        version_status="published_snapshot" if published else "draft",
        created_at=_NOW,
    )
    session.add(head)
    session.flush()
    page.current_version_id = head.id
    if published:
        page.current_published_version_id = head.id
    else:
        page.latest_draft_version_id = head.id
    return page


def _link(session, workspace_id, source, target, weight=1.0):
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
            created_at=_NOW,
        )
    )


def _folder(session, workspace_id, name, *, visible=True):
    folder = WorkspaceTreeNode(
        id=_id(workspace_id, f"folder:{name}"),
        workspace_id=workspace_id,
        section="context",
        node_kind="folder",
        sort_key=name,
        title=name,
        slug=name,
        visibility_status="visible" if visible else "hidden",
    )
    session.add(folder)
    session.flush()
    return folder


def _place(session, workspace_id, folder, page):
    session.add(
        WorkspaceTreeNode(
            id=_id(workspace_id, f"placement:{page.slug}"),
            workspace_id=workspace_id,
            section="context",
            node_kind="page_ref",
            parent_id=folder.id,
            sort_key=page.slug,
            title=page.title,
            slug=f"placement-{page.slug}",
            target_kind="context_page",
            target_id=page.id,
            primary_placement=True,
            visibility_status="visible",
        )
    )


def _query(session, workspace_id, seeds, depth, node_budget, **overrides):
    values = dict(
        seeds=[GraphSeed(kind="context_page", id=seed_id) for seed_id in seeds],
        depth=depth,
        layers=["knowledge"],
        edge_kinds=["wikilink", "tree"],
        budget=GraphBudget(nodes=node_budget, edges=min(750, node_budget * 3)),
    )
    values.update(overrides)
    return GraphQueryService().query(
        session,
        workspace_id,
        request=GraphQueryRequest(**values),
        actor=_ACTOR,
        usage_signals_enabled=False,
    )


def _assert_total_cap(payload, seeds):
    assert len(payload.nodes) <= payload.budget.applied.nodes
    assert len(payload.edges) <= payload.budget.applied.edges
    assert payload.budget.applied == payload.budget.requested == payload.query.budget
    assert {node.entity_id for node in payload.nodes if node.is_seed} == set(seeds)
    ids = {node.id for node in payload.nodes}
    assert len(ids) == len(payload.nodes)
    assert all(edge.source in ids and edge.target in ids for edge in payload.edges)
    assert all(
        set(omission.model_dump()) == {"scope", "reason", "hop", "relation_class"}
        for omission in payload.omissions
    )


def _refill_fixture(session, node_budget, depth, *, initial_folder=False, seed_count=1):
    workspace_id = session.scalar(
        select(Workspace.id).where(Workspace.slug == "default")
    )
    seeds = [
        _page(session, workspace_id, f"seed-{index}") for index in range(seed_count)
    ]
    reserve = min(20, (node_budget - seed_count) // 5)
    slots = node_budget - seed_count - reserve
    first_slots = slots if depth == 1 else (3 * slots + 4) // 5
    first = [
        _page(session, workspace_id, f"first-{index}") for index in range(first_slots)
    ]
    home = (
        _folder(session, workspace_id, "Popular initial folder")
        if initial_folder
        else None
    )
    for index, page in enumerate(first):
        _link(session, workspace_id, seeds[0], page, weight=1_000 - index)
        if home is not None:
            _place(session, workspace_id, home, page)
    second = []
    for index in range(slots - first_slots):
        page = _page(session, workspace_id, f"second-{index}")
        _link(session, workspace_id, first[index % len(first)], page, weight=5)
        second.append(page)
        if home is not None:
            _place(session, workspace_id, home, page)
    for index in range(max(4, reserve * 2)):
        page = _page(session, workspace_id, f"refill-{index}")
        _link(session, workspace_id, seeds[0], page, weight=10 - index * 0.01)
        folder = _folder(session, workspace_id, f"New refill folder {index}")
        _place(session, workspace_id, folder, page)
    session.flush()
    return workspace_id, [seed.id for seed in seeds], home


@pytest.mark.parametrize(
    "depth,node_budget", [(1, 10), (2, 10), (1, 30), (2, 30), (1, 250), (2, 250)]
)
def test_refill_documents_cannot_add_folders_beyond_the_total_cap(
    session_factory, depth, node_budget, monkeypatch
):
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: _NOW)
    with session_factory.begin() as session:
        workspace_id, seeds, _home = _refill_fixture(session, node_budget, depth)
    with session_factory() as session:
        payload = _query(session, workspace_id, seeds, depth, node_budget)
        replay = _query(
            session, workspace_id, list(reversed(seeds)), depth, node_budget
        )

    _assert_total_cap(payload, seeds)
    assert len(payload.nodes) == node_budget
    assert all(node.family != "structural" for node in payload.nodes)
    assert any(omission.reason == "folder_node" for omission in payload.omissions)
    assert payload.model_dump_json() == replay.model_dump_json()


@pytest.mark.parametrize("depth", [1, 2])
def test_refill_retains_the_popular_folder_only_within_remaining_capacity(
    session_factory, depth
):
    with session_factory.begin() as session:
        workspace_id, seeds, home = _refill_fixture(
            session, 30, depth, initial_folder=True
        )
        home_id = home.id
    with session_factory() as session:
        payload = _query(session, workspace_id, seeds, depth, 30)

    _assert_total_cap(payload, seeds)
    assert len(payload.nodes) == 30
    assert [
        node.entity_id for node in payload.nodes if node.family == "structural"
    ] == [home_id]
    assert any(omission.reason == "folder_node" for omission in payload.omissions)
    assert any(edge.edge_kind == "tree" for edge in payload.edges)


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("node_budget", [1, 6, 10, 30])
def test_no_folder_candidates_fill_document_capacity_without_folder_omission(
    session_factory, depth, node_budget
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        for index in range(node_budget - 1):
            _link(
                session,
                workspace_id,
                seed,
                _page(session, workspace_id, f"peer-{index}"),
            )
        seeds = [seed.id]
    with session_factory() as session:
        payload = _query(session, workspace_id, seeds, depth, node_budget)

    _assert_total_cap(payload, seeds)
    assert len(payload.nodes) == node_budget
    assert len(payload.edges) == node_budget - 1
    assert all(node.family == "document" for node in payload.nodes)
    assert payload.omissions == []
    assert payload.completeness == "complete"


@pytest.mark.parametrize("depth", [1, 2])
def test_spare_capacity_keeps_all_available_primary_folders(session_factory, depth):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        peer = _page(session, workspace_id, "peer")
        _link(session, workspace_id, seed, peer)
        folder = _folder(session, workspace_id, "Shared folder")
        _place(session, workspace_id, folder, seed)
        _place(session, workspace_id, folder, peer)
        seeds, folder_id = [seed.id], folder.id
    with session_factory() as session:
        payload = _query(session, workspace_id, seeds, depth, 10)

    _assert_total_cap(payload, seeds)
    assert len(payload.nodes) == 3
    assert [
        node.entity_id for node in payload.nodes if node.family == "structural"
    ] == [folder_id]
    assert len([edge for edge in payload.edges if edge.edge_kind == "tree"]) == 2
    assert not any(omission.reason == "folder_node" for omission in payload.omissions)
    assert payload.completeness == "complete"


@pytest.mark.parametrize("depth", [1, 2])
def test_folder_selection_never_exceeds_the_twenty_slot_reservation(
    session_factory, depth
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        for index in range(21):
            peer = _page(session, workspace_id, f"peer-{index}")
            _link(session, workspace_id, seed, peer)
            _place(
                session,
                workspace_id,
                _folder(session, workspace_id, f"Folder {index}"),
                peer,
            )
        seeds = [seed.id]
    with session_factory() as session:
        payload = _query(session, workspace_id, seeds, depth, 250)

    _assert_total_cap(payload, seeds)
    assert len(payload.nodes) == 22 + 20
    assert len([node for node in payload.nodes if node.family == "structural"]) == 20
    assert any(omission.reason == "folder_node" for omission in payload.omissions)


@pytest.mark.parametrize("depth", [1, 2])
def test_structural_only_selection_keeps_the_seed_and_its_folder(
    session_factory, depth
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        peer = _page(session, workspace_id, "Filtered document")
        _link(session, workspace_id, seed, peer)
        folder = _folder(session, workspace_id, "Seed folder")
        _place(session, workspace_id, folder, seed)
        seeds, folder_id = [seed.id], folder.id
    with session_factory() as session:
        payload = _query(
            session, workspace_id, seeds, depth, 6, node_families=["structural"]
        )

    _assert_total_cap(payload, seeds)
    assert {node.entity_id for node in payload.nodes} == {seeds[0], folder_id}
    assert [edge.edge_kind for edge in payload.edges] == ["tree"]
    assert payload.omissions == []


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("node_budget", [2, 10])
def test_all_seeds_survive_total_capacity_and_seed_input_order(
    session_factory, monkeypatch, depth, node_budget
):
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: _NOW)
    with session_factory.begin() as session:
        if node_budget == 10:
            workspace_id, seeds, _home = _refill_fixture(
                session, node_budget, depth, seed_count=2
            )
        else:
            workspace_id = session.scalar(
                select(Workspace.id).where(Workspace.slug == "default")
            )
            seeds = [
                _page(session, workspace_id, f"seed-{index}").id for index in range(2)
            ]
            for seed_id in seeds:
                seed = session.get(ContextPage, seed_id)
                _place(
                    session,
                    workspace_id,
                    _folder(session, workspace_id, f"Seed folder {seed.slug}"),
                    seed,
                )
    with session_factory() as session:
        forward = _query(session, workspace_id, seeds, depth, node_budget)
        reverse = _query(
            session, workspace_id, list(reversed(seeds)), depth, node_budget
        )

    _assert_total_cap(forward, seeds)
    assert len(forward.nodes) == node_budget
    assert forward.nodes == reverse.nodes
    assert forward.edges == reverse.edges
    assert forward.omissions == reverse.omissions


@pytest.mark.parametrize("seed_kind", ["unknown", "hidden", "other_workspace"])
def test_unknown_or_invisible_seeds_fail_before_folder_selection(
    session_factory, seed_kind
):
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        if seed_kind == "unknown":
            seed_id = _id(workspace_id, "does-not-exist")
        elif seed_kind == "hidden":
            seed = _page(session, workspace_id, "PRIVACY SENTINEL", published=False)
            _place(
                session,
                workspace_id,
                _folder(session, workspace_id, "Hidden page folder"),
                seed,
            )
            seed_id = seed.id
        else:
            foreign = Workspace(slug="foreign-budget", title="Foreign", name="Foreign")
            session.add(foreign)
            session.flush()
            seed_id = _page(session, foreign.id, "foreign-seed").id
    with session_factory() as session:
        with pytest.raises(ReadNotFoundError, match="context page not found"):
            _query(session, workspace_id, [seed_id], 2, 10)


@pytest.mark.parametrize("depth", [1, 2])
def test_hidden_folder_and_neighbor_rows_do_not_change_budget_or_omissions(
    session_factory, monkeypatch, depth
):
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: _NOW)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "seed")
        peer = _page(session, workspace_id, "Visible peer")
        _link(session, workspace_id, seed, peer)
        seeds = [seed.id]
        session.flush()
        before = _query(session, workspace_id, seeds, depth, 10)
        hidden = _page(session, workspace_id, "PRIVACY SENTINEL", published=False)
        _link(session, workspace_id, seed, hidden, weight=99_999)
        hidden_folder = _folder(session, workspace_id, "PRIVACY FOLDER", visible=False)
        _place(session, workspace_id, hidden_folder, peer)
        session.flush()
        after = _query(session, workspace_id, seeds, depth, 10)

    _assert_total_cap(after, seeds)
    assert after.model_dump_json() == before.model_dump_json()
    assert "PRIVACY" not in after.model_dump_json()


@pytest.mark.parametrize("depth,node_budget", [(1, 10), (2, 250)])
def test_authenticated_rest_query_reports_the_real_total_node_budget(
    page_service, depth, node_budget
):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="local-total-node-budget-test-secret",
            ),
            page_service=page_service,
        )
    )
    authenticated_workspace_id = authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email="local-total-budget@example.com",
        workspace_slug="default",
        role="viewer",
    )
    with page_service.session_factory.begin() as session:
        workspace_id, seeds, _home = _refill_fixture(session, node_budget, depth)
    assert str(workspace_id) == str(authenticated_workspace_id)
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/query",
        json={
            "seeds": [
                {"kind": "context_page", "id": str(seed_id)} for seed_id in seeds
            ],
            "depth": depth,
            "edge_kinds": ["wikilink", "tree"],
            "layers": ["knowledge"],
            "budget": {"nodes": node_budget, "edges": node_budget * 3},
        },
    )
    assert response.status_code == 200, response.text
    payload = GraphQueryPayload.model_validate(response.json()["data"])
    _assert_total_cap(payload, seeds)
    assert len(payload.nodes) == node_budget
    assert any(omission.reason == "folder_node" for omission in payload.omissions)
