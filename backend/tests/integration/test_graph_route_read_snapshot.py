"""The Local Graph route reads one database state.

A scene is built from several statements -- the ranked candidates, the
denominators their ranks divide by, hydration and history. A commit landing
between two of them could rank an edge by a count that no longer matches the
rows beside it, so the route opens one read snapshot before the first.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api import graph as graph_api
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion
from backend.services.graph_query_service import GraphQueryService
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration


def _published_page(session_factory, workspace_id: UUID) -> UUID:
    with session_factory.begin() as session:
        page = ContextPage(
            id=uuid4(),
            workspace_id=workspace_id,
            slug=f"snapshot-{uuid4()}",
            title="Snapshot",
            page_type="concept",
            lifecycle_status="published",
        )
        session.add(page)
        session.flush()
        version = ContextPageVersion(
            workspace_id=workspace_id,
            id=uuid4(),
            page_id=page.id,
            version_no=1,
            body_md="# Snapshot",
            content_hash=f"snapshot-{page.id}",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="published_snapshot",
        )
        session.add(version)
        session.flush()
        page.current_version_id = version.id
        page.current_published_version_id = version.id
        return page.id


def test_route_takes_one_read_snapshot_before_query(page_service, monkeypatch):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="graph-snapshot-secret",
            ),
            page_service=page_service,
        )
    )
    workspace_id = UUID(
        str(
            authenticate_test_client_in_workspace(
                client,
                page_service.session_factory,
                email="graph-snapshot@example.com",
                workspace_slug="default",
                role="viewer",
            )
        )
    )
    seed_id = _published_page(page_service.session_factory, workspace_id)

    events: list[tuple[str, object]] = []
    real_snapshot = graph_api.acquire_consistent_read_snapshot
    real_query = GraphQueryService.query

    def snapshot_spy(session):
        # A fresh session has run nothing: autobegin has not started.
        events.append(("snapshot", session, session.in_transaction()))
        return real_snapshot(session)

    def query_spy(self, session, *args, **kwargs):
        events.append(("query", session, None))
        return real_query(self, session, *args, **kwargs)

    monkeypatch.setattr(graph_api, "acquire_consistent_read_snapshot", snapshot_spy)
    monkeypatch.setattr(GraphQueryService, "query", query_spy)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/query",
        json={"seeds": [{"kind": "context_page", "id": str(seed_id)}]},
    )

    assert response.status_code == 200, response.text
    assert [event[0] for event in events] == ["snapshot", "query"]
    (_snapshot, snapshot_session, had_run_statements), (_query, query_session, _) = (
        events
    )
    assert snapshot_session is query_session
    assert had_run_statements is False
