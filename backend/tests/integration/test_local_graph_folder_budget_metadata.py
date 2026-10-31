"""Folder reservation omissions do not imply exhaustion of the total budget."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryPayload,
    GraphQueryRequest,
    GraphSeed,
)
from backend.services.graph_query_service import GraphQueryService
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.integration.test_local_graph_total_node_budget import (
    _NOW,
    _assert_total_cap,
    _folder,
    _link,
    _page,
    _place,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fixed_query_clock(monkeypatch):
    monkeypatch.setattr("backend.services.graph_query_service.utc_now", lambda: _NOW)


def _viewer_query(session, workspace_id, seed_id, *, depth=1, nodes=250, edges=750):
    return GraphQueryService().query(
        session,
        workspace_id,
        request=GraphQueryRequest(
            seeds=[GraphSeed(kind="context_page", id=seed_id)],
            depth=depth,
            layers=["knowledge"],
            edge_kinds=["wikilink", "tree"],
            budget=GraphBudget(nodes=nodes, edges=edges),
        ),
        actor=ActorContext(
            actor_kind="human",
            actor_id="folder-budget-metadata-viewer",
            token_kind="session",
            scopes=("read",),
            workspace_id=workspace_id,
            workspace_role="viewer",
            role="viewer",
        ),
        usage_signals_enabled=False,
    )


def _star_fixture(session, peer_count, *, primary_folders=True):
    workspace_id = session.scalar(
        select(Workspace.id).where(Workspace.slug == "default")
    )
    seed = _page(session, workspace_id, "folder-metadata-seed")
    for index in range(peer_count):
        peer = _page(session, workspace_id, f"folder-metadata-peer-{index:03}")
        _link(session, workspace_id, seed, peer)
        if primary_folders:
            folder = _folder(session, workspace_id, f"Visible folder {index:03}")
            _place(session, workspace_id, folder, peer)
    session.flush()
    return workspace_id, seed


def _omission_reasons(payload):
    return {omission.reason for omission in payload.omissions}


@pytest.mark.parametrize("depth", [1, 2])
def test_folder_reservation_only_omission_is_capped_without_total_budget_warning(
    session_factory, depth
):
    with session_factory.begin() as session:
        workspace_id, seed = _star_fixture(session, 152)
        seed_id = seed.id
    with session_factory() as session:
        payload = _viewer_query(session, workspace_id, seed_id, depth=depth)

    _assert_total_cap(payload, [seed_id])
    assert len(payload.nodes) == 173 < payload.budget.applied.nodes == 250
    assert len([node for node in payload.nodes if node.family == "document"]) == 153
    assert len([node for node in payload.nodes if node.family == "structural"]) == 20
    assert _omission_reasons(payload) == {"folder_node"}
    assert payload.completeness == "capped"
    assert payload.truncated is True
    assert "graph_budget_applied" not in payload.warnings


@pytest.mark.parametrize("depth", [1, 2])
def test_folder_omission_at_the_actual_total_cap_keeps_the_budget_warning(
    session_factory, depth
):
    with session_factory.begin() as session:
        workspace_id, seed = _star_fixture(session, 24)
        seed_id = seed.id
    with session_factory() as session:
        payload = _viewer_query(session, workspace_id, seed_id, depth=depth, nodes=30)

    _assert_total_cap(payload, [seed_id])
    assert len(payload.nodes) == payload.budget.applied.nodes == 30
    assert len([node for node in payload.nodes if node.family == "document"]) == 25
    assert len([node for node in payload.nodes if node.family == "structural"]) == 5
    # No document or relation quota is needed to justify this warning: the
    # omitted primary folders really have no space in the shared total cap.
    assert _omission_reasons(payload) == {"folder_node"}
    assert payload.completeness == "capped"
    assert payload.truncated is True
    assert "graph_budget_applied" in payload.warnings


def test_document_quota_still_reports_a_real_budget_warning(session_factory):
    with session_factory.begin() as session:
        workspace_id, seed = _star_fixture(session, 20, primary_folders=False)
        seed_id = seed.id
    with session_factory() as session:
        payload = _viewer_query(session, workspace_id, seed_id, nodes=10)

    _assert_total_cap(payload, [seed_id])
    assert len(payload.nodes) == 10
    assert _omission_reasons(payload) == {"node_quota"}
    assert payload.completeness == "capped"
    assert payload.truncated is True
    assert "graph_budget_applied" in payload.warnings


def test_relation_quota_with_spare_nodes_still_reports_a_real_budget_warning(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id, seed = _star_fixture(session, 8, primary_folders=False)
        seed_id = seed.id
    with session_factory() as session:
        payload = _viewer_query(session, workspace_id, seed_id, edges=2)

    _assert_total_cap(payload, [seed_id])
    assert len(payload.nodes) == 9 < payload.budget.applied.nodes
    assert len(payload.edges) == payload.budget.applied.edges == 2
    assert _omission_reasons(payload) == {"relation_quota"}
    assert payload.completeness == "capped"
    assert payload.truncated is True
    assert "graph_budget_applied" in payload.warnings


def test_hidden_primary_folder_and_draft_neighbor_do_not_change_folder_metadata(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id, seed = _star_fixture(session, 21)
        before = _viewer_query(session, workspace_id, seed.id)
        hidden_folder = _folder(
            session, workspace_id, "PRIVACY folder metadata sentinel", visible=False
        )
        _place(session, workspace_id, hidden_folder, seed)
        draft = _page(
            session, workspace_id, "PRIVACY draft metadata sentinel", published=False
        )
        _link(session, workspace_id, seed, draft, weight=99_999)
        session.flush()
        after = _viewer_query(session, workspace_id, seed.id)

    assert after.model_dump_json() == before.model_dump_json()
    assert "PRIVACY" not in after.model_dump_json()
    assert _omission_reasons(after) == {"folder_node"}
    assert after.completeness == "capped"
    assert after.truncated is True
    assert "graph_budget_applied" not in after.warnings


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize(
    "peer_count,node_budget,expected_nodes,expects_budget_warning",
    [(152, 250, 173, False), (24, 30, 30, True)],
)
def test_authenticated_viewer_rest_distinguishes_reserved_and_total_folder_caps(
    page_service,
    depth,
    peer_count,
    node_budget,
    expected_nodes,
    expects_budget_warning,
):
    with TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="local-folder-budget-metadata-test-secret",
            ),
            page_service=page_service,
        )
    ) as client:
        authenticated_workspace_id = authenticate_test_client_in_workspace(
            client,
            page_service.session_factory,
            email="local-folder-budget-metadata@example.com",
            workspace_slug="default",
            role="viewer",
        )
        with page_service.session_factory.begin() as session:
            workspace_id, seed = _star_fixture(session, peer_count)
            seed_id = seed.id
        assert str(workspace_id) == str(authenticated_workspace_id)
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/graph/query",
            json={
                "seeds": [{"kind": "context_page", "id": str(seed_id)}],
                "depth": depth,
                "edge_kinds": ["wikilink", "tree"],
                "layers": ["knowledge"],
                "budget": {"nodes": node_budget, "edges": node_budget * 3},
            },
        )

    assert response.status_code == 200, response.text
    payload = GraphQueryPayload.model_validate(response.json()["data"])
    _assert_total_cap(payload, [seed_id])
    assert len(payload.nodes) == expected_nodes
    assert _omission_reasons(payload) == {"folder_node"}
    assert payload.completeness == "capped"
    assert payload.truncated is True
    assert ("graph_budget_applied" in payload.warnings) is expects_budget_warning
    if not expects_budget_warning:
        assert len(payload.nodes) < payload.budget.applied.nodes
