import importlib

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.tests.conftest import authenticate_test_client_in_workspace


def _write_draft(session_factory, title: str) -> str:
    with session_factory.begin() as session:
        workspace = session.query(Workspace).filter_by(slug="default").one()
        result = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace.id,
            request=WriteDraftRequest(
                title=title,
                page_type="overview",
                body_markdown=f"# {title}\n\nBody.",
                reason="verify MCP-authored slug reachability",
            ),
            actor_kind="mcp_client",
            actor_id="dev-local-mcp",
            actor_role="owner",
        )
        return session.get(ContextPage, result.page_id).slug


def _client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(client, page_service.session_factory)
    return client


def test_korean_write_draft_is_reachable_by_slug(page_service, session_factory):
    slug = _write_draft(
        session_factory, "운영 문서 작성 경험 기반 개선 분석 2026 07 27"
    )

    client = _client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/by-slug/{slug}"
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["slug"] == slug


def test_long_english_write_draft_is_bounded_and_reachable(
    page_service, session_factory
):
    slug = _write_draft(session_factory, "long english context title " * 5)

    client = _client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/by-slug/{slug}"
    )

    assert len(slug) <= 64
    assert response.status_code == 200, response.text


def test_migration_truncates_and_disambiguates_slugs_with_tree_sync(
    session_factory,
):
    page_ids = []
    for title in ("reserved", "long x", "long y"):
        _write_draft(session_factory, title)
        with session_factory() as session:
            page_ids.append(
                session.scalar(select(ContextPage.id).where(ContextPage.title == title))
            )

    reserved = "a" * 64
    long_x = "a" * 70 + "x"
    long_y = "a" * 70 + "y"
    with session_factory.begin() as session:
        for page_id, slug in zip(page_ids, (reserved, long_x, long_y), strict=True):
            session.get(ContextPage, page_id).slug = slug
            node = session.scalar(
                select(WorkspaceTreeNode).where(WorkspaceTreeNode.target_id == page_id)
            )
            node.slug = slug

    migration = importlib.import_module(
        "backend.migrations.versions.v0_1.efcbcf724889_core_schema"
    )
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        migration._truncate_overlength_page_slugs(connection)

    with session_factory() as session:
        page_slugs = {
            page.id: page.slug
            for page in session.scalars(
                select(ContextPage).where(ContextPage.id.in_(page_ids))
            )
        }
        tree_slugs = {
            node.target_id: node.slug
            for node in session.scalars(
                select(WorkspaceTreeNode).where(
                    WorkspaceTreeNode.target_id.in_(page_ids)
                )
            )
        }

    assert page_slugs[page_ids[0]] == reserved
    assert page_slugs[page_ids[1]] == "a" * 62 + "-2"
    assert page_slugs[page_ids[2]] == "a" * 62 + "-3"
    assert tree_slugs == page_slugs
