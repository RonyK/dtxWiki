"""Workspace Map reads one actor-visible database state per request."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.api import graph as graph_api
from backend.api.deps import get_workspace_session_factory
from backend.app.db import make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.base import Base
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_lifecycle import apply_context_publication
from backend.services.db_write_intent import acquire_write_intent
from backend.services.page_service import PageService
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration


def _client(page_service, *, enabled=True, service_available=True):
    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="workspace-map-read-snapshot-secret",
            graph_service_available=service_available,
        ),
        page_service=page_service,
    )
    client = TestClient(app)
    workspace_id = UUID(
        authenticate_test_client_in_workspace(
            client,
            page_service.session_factory,
            email="workspace-map-read-snapshot@example.com",
            workspace_slug="default",
            role="viewer",
        )
    )
    with page_service.session_factory.begin() as session:
        session.get(Workspace, workspace_id).workspace_graph_enabled = enabled
    return client, workspace_id


def _read_map(client, workspace_id):
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/graph/overview/query",
        json={"lens": "links"},
    )


def test_map_route_takes_one_fresh_snapshot_after_capability_before_query(
    page_service, monkeypatch
):
    client, workspace_id = _client(page_service)
    events = []
    real_capability = graph_api._capability
    real_snapshot = graph_api.acquire_consistent_read_snapshot
    real_query = WorkspaceGraphService.query

    def capability_spy(*args, **kwargs):
        events.append(("capability", None))
        return real_capability(*args, **kwargs)

    def data_sessions():
        session = page_service.session_factory()
        events.append(("data_session", session))
        return session

    def snapshot_spy(session):
        # No SQL/autobegin may precede the snapshot on the workspace Session.
        assert session.in_transaction() is False
        events.append(("snapshot", session))
        return real_snapshot(session)

    def query_spy(self, session, *args, **kwargs):
        events.append(("query", session))
        return real_query(self, session, *args, **kwargs)

    client.app.dependency_overrides[get_workspace_session_factory] = lambda: (
        data_sessions
    )
    monkeypatch.setattr(graph_api, "_capability", capability_spy)
    monkeypatch.setattr(graph_api, "acquire_consistent_read_snapshot", snapshot_spy)
    monkeypatch.setattr(WorkspaceGraphService, "query", query_spy)

    response = _read_map(client, workspace_id)

    assert response.status_code == 200, response.text
    assert [name for name, _session in events] == [
        "capability",
        "data_session",
        "snapshot",
        "query",
    ]
    data_session = events[1][1]
    assert events[2][1] is events[3][1] is data_session
    assert data_session.in_transaction() is False


@pytest.mark.parametrize(
    ("enabled", "service_available"), [(False, True), (True, False)]
)
def test_disabled_map_does_not_open_data_session_or_snapshot(
    page_service, monkeypatch, enabled, service_available
):
    client, workspace_id = _client(
        page_service, enabled=enabled, service_available=service_available
    )

    def unexpected_data_access(*_args, **_kwargs):
        pytest.fail("a disabled Map must not open a workspace read snapshot")

    client.app.dependency_overrides[get_workspace_session_factory] = lambda: (
        unexpected_data_access
    )
    monkeypatch.setattr(
        graph_api, "acquire_consistent_read_snapshot", unexpected_data_access
    )
    monkeypatch.setattr(WorkspaceGraphService, "query", unexpected_data_access)

    response = _read_map(client, workspace_id)

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "not_found"


@pytest.fixture
def wal_page_service(tmp_path):
    # Always a dedicated temporary file, never a configured application DB.
    engine = make_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'map-snapshot.sqlite').as_posix()}"
    )
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    try:
        with session_factory.begin() as session:
            assert session.scalar(text("PRAGMA journal_mode")) == "wal"
            seed_default_workspace(session)
        yield PageService(session_factory)
    finally:
        engine.dispose()


def _published_page(session, workspace_id, slug):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        title=slug,
        page_type="concept",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {slug}",
        content_hash=f"map-snapshot-{page.id}",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = page.current_published_version_id = version.id
    return page, version


def test_map_response_stays_coherent_across_concurrent_publication(
    wal_page_service, monkeypatch
):
    client, workspace_id = _client(wal_page_service)
    session_factory = wal_page_service.session_factory
    with session_factory.begin() as session:
        source, old_head = _published_page(session, workspace_id, "source")
        old_target, _ = _published_page(session, workspace_id, "old-target")
        new_target, _ = _published_page(session, workspace_id, "new-target")
        new_head = ContextPageVersion(
            id=uuid4(),
            workspace_id=workspace_id,
            page_id=source.id,
            parent_version_id=old_head.id,
            version_no=2,
            body_md="# Source\n\n[[new-target]]",
            content_hash="map-snapshot-source-v2",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="tester",
            version_status="draft",
            graph_status="linear",
        )
        session.add(new_head)
        session.flush()
        source.latest_draft_version_id = new_head.id
        source.current_version_id = new_head.id
        session.add_all(
            [
                PageLink(
                    workspace_id=workspace_id,
                    source_page_id=source.id,
                    source_version_id=head.id,
                    target_slug=target.slug,
                    target_page_id=target.id,
                    occurrence_count=1,
                )
                for head, target in ((old_head, old_target), (new_head, new_target))
            ]
        )
        source_id, old_head_id, new_head_id = source.id, old_head.id, new_head.id
        old_target_id, new_target_id = old_target.id, new_target.id

    before_response = _read_map(client, workspace_id)
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()["data"]
    assert before["counts"]["candidate_documents"] == 3
    assert before["counts"]["candidate_relations"] == 1
    assert before["edges"][0]["target"] == f"document:context:{old_target_id}"

    real_load_relations = WorkspaceGraphService._load_relation_universe
    publication_committed = []

    def publish_after_capture(reader, query_workspace_id, documents):
        if not publication_committed:
            assert documents[("context", source_id)].head_id == old_head_id
            with session_factory.begin() as writer:
                # A distinct connection commits while the Map reader is open.
                assert writer is not reader
                assert (
                    writer.connection().connection is not reader.connection().connection
                )
                acquire_write_intent(writer)
                page = writer.get(ContextPage, source_id)
                version = writer.get(ContextPageVersion, new_head_id)
                assert (
                    apply_context_publication(
                        writer,
                        page=page,
                        version=version,
                        require_latest_draft=True,
                    )
                    == old_head_id
                )
            with session_factory() as observer:
                assert (
                    observer.get(ContextPage, source_id).current_published_version_id
                    == new_head_id
                )
                assert (
                    observer.get(ContextPageVersion, old_head_id).version_status
                    == "superseded_snapshot"
                )
            publication_committed.append(True)
        return real_load_relations(reader, query_workspace_id, documents)

    monkeypatch.setattr(
        WorkspaceGraphService,
        "_load_relation_universe",
        staticmethod(publish_after_capture),
    )

    during_response = _read_map(client, workspace_id)
    assert during_response.status_code == 200, during_response.text
    assert publication_committed == [True]
    # All rows, metrics, revision, facets and freshness retain the old state;
    # fail-closed omission alone would incorrectly erase the old relation.
    assert during_response.json()["data"] == before

    after_response = _read_map(client, workspace_id)
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()["data"]
    assert after["graph_revision"] != before["graph_revision"]
    assert after["counts"]["candidate_documents"] == 3
    assert after["counts"]["candidate_relations"] == 1
    assert len(after["edges"]) == 1
    assert after["edges"][0]["source"] == f"document:context:{source_id}"
    assert after["edges"][0]["target"] == f"document:context:{new_target_id}"
    nodes = {node["entity_id"]: node for node in after["nodes"]}
    assert nodes[str(old_target_id)]["reference_count"] == 0
    assert nodes[str(new_target_id)]["reference_count"] == 1
