"""Internal tree reads must not change a viewer's bounded Local Graph scene."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.graph import (
    GraphBudget,
    GraphQueryRequest,
    GraphSeed,
    GraphTimeQuery,
)
from backend.services import graph_query_service as query_module
from backend.services import graph_service as graph_module
from backend.services.graph_query_service import GraphQueryService

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 3, tzinfo=UTC)
_VIEWER = ActorContext(
    actor_kind="human",
    actor_id="cache-order-viewer",
    token_kind="session",
    scopes=(),
    workspace_role="viewer",
)
_CACHES = (
    "_TREE_CHAIN_PREDICATES",
    "_PARTITION_STATEMENTS",
    "_DENOMINATOR_STATEMENTS",
)


def _page(session, workspace_id, title, *, published=True):
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=f"cache-order-{uuid4().hex}",
        title=title,
        page_type="concept",
        lifecycle_status="published" if published else "draft",
    )
    session.add(page)
    session.flush()
    body = f"# {title}"
    head = ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="cache-order-owner",
        version_status="published_snapshot" if published else "draft",
    )
    session.add(head)
    session.flush()
    page.current_version_id = head.id
    if published:
        page.current_published_version_id = head.id
    else:
        page.latest_draft_version_id = head.id
    return page


def _tree_edge(workspace_id, source_id, target_id, *, weight):
    return EdgeProjection(
        workspace_id=workspace_id,
        source_kind="workspace_tree_node",
        source_id=source_id,
        target_kind="context_page",
        target_id=target_id,
        edge_kind="tree",
        provenance="derived",
        weight=weight,
        source_ref={},
    )


def _query(session, workspace_id, seed_id, actor):
    return GraphQueryService().query(
        session,
        workspace_id,
        request=GraphQueryRequest(
            seeds=[GraphSeed(kind="context_page", id=seed_id)],
            depth=2,
            edge_kinds=["tree"],
            layers=["knowledge"],
            budget=GraphBudget(nodes=50, edges=150),
        ),
        actor=actor,
        usage_signals_enabled=False,
    )


@pytest.mark.parametrize("first_reader", ["internal", "viewer"])
def test_reader_cache_order_preserves_each_readers_fresh_graph_scene(
    session_factory, monkeypatch, first_reader
):
    for cache_name in _CACHES:
        monkeypatch.setattr(graph_module, cache_name, {})
    monkeypatch.setattr(query_module, "utc_now", lambda: _NOW)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        seed = _page(session, workspace_id, "Visible Seed")
        hidden = _page(session, workspace_id, "PRIVATE DRAFT", published=False)
        folder = WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            sort_key="visible",
            title="Visible Folder",
            slug=f"visible-{uuid4().hex}",
            visibility_status="visible",
        )
        session.add(folder)
        session.add(_tree_edge(workspace_id, folder.id, seed.id, weight=1.0))
        # These folders are visible internally, but sit under a draft that
        # is invisible to the viewer. They must be excluded before the
        # viewer's 1,024-row partition window, even if an internal reader
        # populated all caches. (A draft's own placement is the draft
        # document, so the sentinels are folders below it.)
        draft_placement = WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="page_ref",
            sort_key="private",
            title="PRIVATE PLACEMENT",
            slug=f"private-{uuid4().hex}",
            target_kind="context_page",
            target_id=hidden.id,
            visibility_status="visible",
        )
        session.add(draft_placement)
        for index in range(1025):
            placement_id = uuid4()
            session.add(
                WorkspaceTreeNode(
                    id=placement_id,
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=draft_placement.id,
                    sort_key=f"private-{index}",
                    title=f"PRIVACY SENTINEL {index}",
                    slug=f"private-{uuid4().hex}",
                    visibility_status="visible",
                )
            )
            session.add(_tree_edge(workspace_id, placement_id, seed.id, weight=9.0))
        seed_id = seed.id

    def clear_caches():
        for cache_name in _CACHES:
            getattr(graph_module, cache_name).clear()

    readers = {"internal": None, "viewer": _VIEWER}
    with session_factory() as session:
        fresh = {}
        for name, actor in readers.items():
            clear_caches()
            fresh[name] = _query(session, workspace_id, seed_id, actor)
        clear_caches()
        second_reader = "viewer" if first_reader == "internal" else "internal"
        first = _query(session, workspace_id, seed_id, readers[first_reader])
        second = _query(session, workspace_id, seed_id, readers[second_reader])

    assert first.model_dump_json() == fresh[first_reader].model_dump_json()
    assert second.model_dump_json() == fresh[second_reader].model_dump_json()
    viewer = fresh["viewer"]
    assert viewer.completeness == "complete"
    assert viewer.truncated is False
    assert viewer.omissions == []
    assert {node.label for node in viewer.nodes} == {"Visible Seed", "Visible Folder"}
    assert "PRIVACY SENTINEL" not in viewer.model_dump_json()
    # Preserve the existing internal tree semantics rather than silently
    # narrowing actor=None to viewer visibility to make the cache test pass.
    assert any("PRIVACY SENTINEL" in node.label for node in fresh["internal"].nodes)


def _placement(workspace_id, page, *, parent_id=None):
    return WorkspaceTreeNode(
        id=uuid4(),
        workspace_id=workspace_id,
        section="context",
        node_kind="page_ref",
        parent_id=parent_id,
        sort_key=page.slug,
        title=page.title,
        slug=page.slug,
        target_kind="context_page",
        target_id=page.id,
        visibility_status="visible",
    )


def test_document_containment_follows_the_readers_treerail_placement(
    session_factory, monkeypatch
):
    # Independent failure path: a placement chain hidden above a visible
    # container document. Reading the placement as the document must not
    # skip the TreeRail chain check that the placement itself would get.
    monkeypatch.setattr(query_module, "utc_now", lambda: _NOW)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        draft = _page(session, workspace_id, "PRIVATE DRAFT", published=False)
        container = _page(session, workspace_id, "Visible Container")
        child = _page(session, workspace_id, "Visible Child")
        draft_placement = _placement(workspace_id, draft)
        container_placement = _placement(
            workspace_id, container, parent_id=draft_placement.id
        )
        session.add_all([draft_placement, container_placement])
        session.flush()
        session.add(_placement(workspace_id, child, parent_id=container_placement.id))
        session.add(
            _tree_edge(workspace_id, draft_placement.id, container.id, weight=1)
        )
        session.add(
            _tree_edge(workspace_id, container_placement.id, child.id, weight=1)
        )
        container_id, child_id = container.id, child.id

    editor = ActorContext(
        actor_kind="human",
        actor_id="cache-order-editor",
        token_kind="session",
        scopes=(),
        workspace_role="editor",
    )
    contained = {}
    with session_factory() as session:
        for name, actor in (("viewer", _VIEWER), ("editor", editor)):
            scene = GraphQueryService().query(
                session,
                workspace_id,
                request=GraphQueryRequest(
                    seeds=[GraphSeed(kind="context_page", id=container_id)],
                    preset="all",
                    direction="both",
                    time=GraphTimeQuery(mode="appearance"),
                    budget=GraphBudget(nodes=50, edges=150),
                ),
                actor=actor,
                usage_signals_enabled=False,
            )
            node_ids = {node.entity_id: node.id for node in scene.nodes}
            assert container_id in node_ids, name
            contained[name] = any(
                edge.edge_kind == "tree"
                and edge.source == node_ids[container_id]
                and edge.target == node_ids.get(child_id)
                for edge in scene.edges
            )

    assert contained == {"viewer": False, "editor": True}
