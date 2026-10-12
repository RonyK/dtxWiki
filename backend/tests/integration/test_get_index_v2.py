"""get_index v2: auto 2-mode, fanout 50, export contract split (C2-T3)."""

import sqlite3

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.schemas.tree import TreeFolderCreateRequest, TreeNodeUpdate
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.okf import build_workspace_index, render_export_manifest
from backend.services.page_service import sync_page_projections
from backend.services.tree_service import TreeService, ensure_primary_tree_placement

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _context_root(session, workspace_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "context",
            WorkspaceTreeNode.slug == "context",
        )
    )


def _bulk_page(session, workspace_id, slug, title, parent_id):
    page = ContextPage(
        workspace_id=workspace_id,
        slug=slug,
        title=title,
        page_type="concept",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=f"# {title}\n\n{title} body paragraph.",
        content_hash=f"{slug}-1",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    page.current_version_id = version.id
    sync_page_projections(session, page=page, version=version)
    ensure_primary_tree_placement(
        session=session,
        workspace_id=workspace_id,
        target_kind="context_page",
        target_id=page.id,
        title=title,
        section="context",
        parent_id=parent_id,
        default_parent_slug="inbox",
        actor_kind="human",
        actor_id="tester",
        move_existing=True,
    )
    return page


def _build_large_workspace(factory):
    """>200 content nodes: one folder with 60 pages + 150 root-inbox pages."""

    tree = TreeService()
    with factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Payments"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        for i in range(60):
            _bulk_page(
                session,
                workspace_id,
                f"pay-{i:03d}",
                f"Payment Doc {i:03d}",
                folder.id,
            )
        for i in range(150):
            _bulk_page(
                session,
                workspace_id,
                f"gen-{i:03d}",
                f"General Doc {i:03d}",
                root.id,
            )
        return {"workspace_id": workspace_id, "folder_id": folder.id}


@pytest.fixture(scope="module")
def large_workspace_db(_empty_sqlite_schema):
    """One committed 210-page workspace shared by the read-only index tests.

    Building it costs ~1.6 s, so the module builds it once on its own engine
    (the function-scoped ``session_factory`` is not available at module scope).
    Readers open fresh sessions and never write; the one test that writes
    clones the database first.
    """

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    engine = None
    try:
        _empty_sqlite_schema.backup(connection)
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            creator=lambda: connection,
            poolclass=StaticPool,
        )
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory.begin() as session:
            seed_default_workspace(session)
        yield factory, _build_large_workspace(factory), connection
    finally:
        try:
            if engine is not None:
                engine.dispose()
        finally:
            connection.close()


@pytest.fixture
def large_workspace(large_workspace_db):
    """Read-only handle on the shared large workspace."""

    factory, ids, _connection = large_workspace_db
    return factory, ids


@pytest.fixture
def large_workspace_clone(large_workspace_db):
    """A private, writable copy of the large workspace."""

    _factory, ids, source = large_workspace_db
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    engine = None
    try:
        source.backup(connection)
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            creator=lambda: connection,
            poolclass=StaticPool,
        )
        yield sessionmaker(bind=engine, expire_on_commit=False), ids
    finally:
        try:
            if engine is not None:
                engine.dispose()
        finally:
            connection.close()


def test_small_workspace_stays_flat(session_factory, page_service):
    page_service.write_page(
        workspace_slug="default",
        slug="flat-page",
        title="Flat Page",
        body_md="# Flat Page\n\nSmall workspace body.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        index = build_workspace_index(
            session, _workspace_id(session), actor=None, workspace_slug="default"
        )
    # flat mode: full catalog sections, no routing node headers
    assert "## Context" in index
    assert "(node:" not in index
    assert "/context/flat-page.md" in index


def test_large_workspace_routes_and_descends(large_workspace):
    session_factory, ids = large_workspace
    with session_factory() as session:
        workspace_id = ids["workspace_id"]
        root_index = build_workspace_index(
            session, workspace_id, actor=None, workspace_slug="default"
        )
        # routing root: folder line with subtree count + node link
        assert "📁 [Payments/](node:/context/payments) (60)" in root_index
        # fanout: root has 150+ direct pages -> truncated with honest tail
        assert "…and" in root_index and "more — narrow via a child folder" in (
            root_index
        )
        # NULL nav_summary -> render-time heuristic (child rollup + type mix)
        assert "includes Payment Doc 000" in root_index
        assert "mostly concept" in root_index

        node_index = build_workspace_index(
            session,
            workspace_id,
            actor=None,
            workspace_slug="default",
            node="/context/payments",
        )
        assert "(node: /context/payments)" in node_index
        # 60 children -> exactly 50 entries + "…and 10 more"
        assert node_index.count("* [Payment Doc") == 50
        assert "…and 10 more" in node_index
        # entries carry write-time nav descriptions
        assert "body paragraph." in node_index


def test_stored_nav_summary_beats_heuristic(large_workspace_clone):
    session_factory, ids = large_workspace_clone
    tree = TreeService()
    with session_factory.begin() as session:
        tree.update_node(
            session=session,
            workspace_id=ids["workspace_id"],
            node_id=ids["folder_id"],
            request=TreeNodeUpdate(
                display_name="Payments",
                nav_summary="결제 승인·정산·환불. 정책은 /policy 참조.",
            ),
            actor_kind="human",
            actor_id="tester",
        )
    with session_factory() as session:
        index = build_workspace_index(
            session,
            ids["workspace_id"],
            actor=None,
            workspace_slug="default",
        )
    assert "결제 승인·정산·환불" in index
    assert "includes Payment Doc" not in index


def test_export_manifest_is_never_truncated(large_workspace):
    """B-6 contract split: navigation truncates, exports never do."""

    session_factory, ids = large_workspace
    with session_factory() as session:
        manifest = render_export_manifest(
            session,
            ids["workspace_id"],
            actor=None,
            workspace_slug="default",
            sections=("context",),
        )
    # every one of the 210 seeded pages is present
    for i in range(60):
        assert f"/context/pay-{i:03d}.md" in manifest
    for i in range(150):
        assert f"/context/gen-{i:03d}.md" in manifest
    assert "…and" not in manifest


def test_unknown_node_raises_not_found(session_factory):
    from backend.services.okf_index import IndexNodeNotFoundError

    with session_factory() as session:
        with pytest.raises(IndexNodeNotFoundError):
            build_workspace_index(
                session,
                _workspace_id(session),
                actor=None,
                workspace_slug="default",
                node="/context/does-not-exist",
            )
