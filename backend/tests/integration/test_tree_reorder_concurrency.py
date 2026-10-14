import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlparse
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.tree import TreeFolderCreateRequest, TreeReorderRequest
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.page_service import PageService
from backend.services.tree_service import TreeService
from backend.tests.conftest import authenticate_test_client
from backend.tests.utils.postgres import create_isolated_postgres_metadata
from backend.tests.utils.postgres_reset import reset_postgres_test_database


def _postgres_test_url() -> str | None:
    url = os.environ.get("DTXWIKI_POSTGRES_TEST_DATABASE_URL")
    if not url:
        return None
    database_name = urlparse(url).path.rsplit("/", 1)[-1]
    if not database_name.endswith(("_test", "-test")):
        pytest.skip(
            "DTXWIKI_POSTGRES_TEST_DATABASE_URL must point to a *_test or "
            "*-test database because this test drops all tables."
        )
    return url


@pytest.mark.xdist_group("postgres")
@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="DTXWIKI_POSTGRES_TEST_DATABASE_URL not set; SQLite StaticPool cannot "
    "exercise row-lock concurrency.",
)
def test_same_parent_reorders_are_serialized_and_leave_consistent_order():
    engine = create_engine(_postgres_test_url())
    reset_postgres_test_database(engine)
    create_isolated_postgres_metadata(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    page_service = PageService(session_factory)
    client = TestClient(
        create_app(
            settings=Settings(
                database_url=_postgres_test_url(),
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client, email="tree-reorder-race@example.com")
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    created: list[dict] = []
    for word in ("Alpha", "Bravo", "Charlie", "Delta"):
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/nodes",
            json={
                "section": "context",
                "title": f"Race {word}",
                "slug": f"race-{word.lower()}",
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["data"])

    start = Barrier(2, timeout=30)

    def reorder(payload: dict):
        start.wait()
        return client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/reorder",
            json=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            reorder,
            {
                "node_id": created[3]["id"],
                "anchor_node_id": created[1]["id"],
                "placement": "before",
            },
        )
        second = executor.submit(
            reorder,
            {
                "node_id": created[0]["id"],
                "anchor_node_id": created[2]["id"],
                "placement": "after",
            },
        )
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert [response.status_code for response in responses] == [200, 200]
    with session_factory() as session:
        siblings = session.scalars(
            select(WorkspaceTreeNode)
            .where(
                WorkspaceTreeNode.workspace_id == UUID(workspace_id),
                WorkspaceTreeNode.parent_id == UUID(created[0]["parent_id"]),
                WorkspaceTreeNode.visibility_status == "visible",
            )
            .order_by(WorkspaceTreeNode.sort_key)
        ).all()
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "tree.node.reorder")
        )

    assert [node.sort_key for node in siblings] == [
        f"{position:04d}" for position in range(1, len(siblings) + 1)
    ]
    assert len({node.id for node in siblings}) == len(siblings)
    assert {node.id for node in siblings}.issuperset(
        {UUID(node["id"]) for node in created}
    )
    assert audit_count == 2


def test_same_node_reorder_refreshes_stale_sqlite_snapshot(tmp_path):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'tree-race.sqlite').as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    tree = TreeService()
    try:
        with session_factory.begin() as session:
            seed_default_workspace(session)
            workspace_id = session.scalar(
                select(Workspace.id).where(Workspace.slug == "default")
            )
            root = session.scalar(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.slug == "context",
                )
            )
            nodes = [
                tree.create_folder(
                    session=session,
                    workspace_id=workspace_id,
                    request=TreeFolderCreateRequest(
                        parent_id=root.id,
                        section="context",
                        title=title,
                    ),
                    actor_kind="human",
                    actor_id="tester",
                )
                for title in ("Alpha", "Bravo", "Charlie", "Delta")
            ]
            node_ids = [node.id for node in nodes]
            parent_id = root.id

        with session_factory() as stale_session:
            stale_siblings = stale_session.scalars(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.parent_id == parent_id,
                    WorkspaceTreeNode.visibility_status == "visible",
                )
            ).all()
            assert {node.id for node in stale_siblings}.issuperset(node_ids)

            with session_factory.begin() as concurrent_session:
                tree.reorder_node(
                    session=concurrent_session,
                    workspace_id=workspace_id,
                    request=TreeReorderRequest.model_validate(
                        {
                            "node_id": node_ids[3],
                            "anchor_node_id": node_ids[1],
                            "placement": "before",
                        }
                    ),
                    actor_kind="human",
                    actor_id="tester",
                )

            # This is a no-op in the stale A/B/C/D identity map, but a real
            # move after the concurrent D-before-B commit. The SQLite write
            # intent + refresh must observe and reverse that intervening move.
            tree.reorder_node(
                session=stale_session,
                workspace_id=workspace_id,
                request=TreeReorderRequest.model_validate(
                    {
                        "node_id": node_ids[3],
                        "anchor_node_id": node_ids[2],
                        "placement": "after",
                    }
                ),
                actor_kind="human",
                actor_id="tester",
            )
            stale_session.commit()

        with session_factory() as session:
            siblings = session.scalars(
                select(WorkspaceTreeNode)
                .where(
                    WorkspaceTreeNode.parent_id == parent_id,
                    WorkspaceTreeNode.visibility_status == "visible",
                )
                .order_by(WorkspaceTreeNode.sort_key)
            ).all()
            audit_count = session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "tree.node.reorder")
            )

        assert [node.id for node in siblings if not node.is_system_node] == node_ids
        assert audit_count == 2
    finally:
        engine.dispose()
