"""Compact capture retains revision inputs; only admitted detail expands routes."""

from dataclasses import fields
from uuid import UUID

import pytest
from sqlalchemy import select

import backend.services.workspace_graph_service as graph
from backend.models.context import ContextPage
from backend.models.tree import WorkspaceTreeNode
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.tests.utils.workspace_graph_corpora import build_knowledge_40, corpus_actor

pytestmark = pytest.mark.integration


def test_details_are_admitted_after_grouping_filtering_expansion_and_focus(
    session_factory,
    monkeypatch,
):
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="detail-admission")
        primaries = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == corpus.workspace_id,
                WorkspaceTreeNode.target_id.is_not(None),
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        ).all()
        for index, primary in enumerate(primaries):
            session.add(
                WorkspaceTreeNode(
                    id=UUID(int=50_000 + index),
                    workspace_id=corpus.workspace_id,
                    section=primary.section,
                    node_kind=primary.node_kind,
                    parent_id=primary.parent_id,
                    sort_key=f"detail-secondary-{index}",
                    title="Secondary detail",
                    slug=f"detail-secondary-{index}",
                    target_kind=primary.target_kind,
                    target_id=primary.target_id,
                    primary_placement=False,
                    visibility_status="visible",
                )
            )
    service = graph.WorkspaceGraphService(cursor_secret="detail-admission-secret")
    captured = {}
    detailed = []
    expanded_routes = []
    real_attach = service._attach_tree_placements
    real_detail = service._materialize_document_detail
    real_path = graph._CapturedPlacement.path.fget

    def attach(session, workspace_id, documents):
        real_attach(session, workspace_id, documents)
        captured.clear()
        captured.update(documents)

    def detail(document):
        detailed.append(document.key)
        return real_detail(document)

    def path(placement):
        expanded_routes.append(placement.tree_node_id)
        return real_path(placement)

    monkeypatch.setattr(service, "_attach_tree_placements", attach)
    monkeypatch.setattr(service, "_materialize_document_detail", detail)
    monkeypatch.setattr(graph._CapturedPlacement, "path", property(path))

    def query(**kwargs):
        detailed.clear()
        expanded_routes.clear()
        with session_factory() as session:
            payload = service.query(
                session,
                corpus.workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget={"documents": 6, "relations": 100},
                    **kwargs,
                ),
                actor=corpus_actor(corpus, role="owner"),
            )
        expected = {
            (node.entity_kind, node.entity_id)
            for node in payload.nodes
            if node.grain == "document"
        }
        assert set(detailed) == expected
        assert len(detailed) == len(expected) <= 6
        assert all(
            isinstance(d.primary, graph._CapturedPlacement)
            for d in captured.values()
            if d.primary is not None
        )
        # The capture stores no expanded path/ancestor copies per placement.
        assert {f.name for f in fields(graph._CapturedPlacement)} == {
            "tree_node_id",
            "index",
            "folder",
        }
        if not kwargs.get("search"):
            assert set(expanded_routes) == {
                p.tree_node_id for key in expected for p in captured[key].secondary
            }
            if expected:
                assert expanded_routes
                assert all(
                    len(node.also_appears_in) == 1
                    and node.also_appears_in[0].path.endswith(" / Secondary detail")
                    for node in payload.nodes
                    if node.grain == "document"
                )
        assert (
            sum(1 if n.grain == "document" else n.member_count for n in payload.nodes)
            == payload.counts.candidate_documents
        )
        return payload, set(captured) - expected

    base, clustered = query()
    assert clustered
    cluster = next(n for n in base.nodes if n.grain == "cluster")
    expanded, _ = query(
        expansion={"cluster_id": cluster.id, "cursor": base.expansion_cursor}
    )
    assert expanded.graph_revision == base.graph_revision
    focused_id = next(key[1] for key in clustered if key[0] == "context")
    focused, _ = query(focus_entity_id=focused_id)
    assert focused_id in {n.entity_id for n in focused.nodes if n.grain == "document"}
    filtered, _ = query(document_kinds=["library"])
    assert filtered.counts.candidate_documents == len(corpus.library_ids)
    assert all(key[0] == "library" for key in detailed)


def test_clustered_metadata_and_beyond_cap_placement_still_invalidate_revision(
    session_factory,
    monkeypatch,
):
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="compact-revision")
    service = graph.WorkspaceGraphService(cursor_secret="compact-revision-secret")
    detailed = []
    real_detail = service._materialize_document_detail

    def detail(document):
        detailed.append(document.key)
        return real_detail(document)

    monkeypatch.setattr(service, "_materialize_document_detail", detail)

    def query():
        detailed.clear()
        with session_factory() as session:
            return service.query(
                session,
                corpus.workspace_id,
                request=WorkspaceGraphOverviewRequest(
                    budget={"documents": 1, "relations": 100},
                ),
                actor=corpus_actor(corpus, role="owner"),
            )

    base = query()
    assert base.counts.rendered_documents == 0
    assert detailed == []
    target = corpus.context_ids[0]
    with session_factory.begin() as session:
        primary = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == corpus.workspace_id,
                WorkspaceTreeNode.target_id == target,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        assert primary is not None
        primary_id = primary.id
        original_parent = primary.parent_id
        for index in range(35):
            session.add(
                WorkspaceTreeNode(
                    id=UUID(int=index + 1),
                    workspace_id=corpus.workspace_id,
                    section="context",
                    node_kind="context_page",
                    parent_id=primary.parent_id,
                    sort_key=f"secondary-{index}",
                    title=f"Secondary {index}",
                    slug=f"secondary-{index}",
                    target_kind="context_page",
                    target_id=target,
                    primary_placement=False,
                    visibility_status="visible",
                )
            )
    placed = query()
    assert placed.graph_revision != base.graph_revision
    with session_factory.begin() as session:
        outside_cap = session.get(WorkspaceTreeNode, UUID(int=35))
        outside_cap.title = "Changed beyond 32 visible payload placements"
    changed = query()
    assert changed.graph_revision != placed.graph_revision
    assert changed.counts == placed.counts == base.counts
    assert changed.folder_topology == placed.folder_topology == base.folder_topology
    assert changed.facet_counts == placed.facet_counts == base.facet_counts
    with session_factory.begin() as session:
        page = session.get(ContextPage, target)
        page.title = "Changed clustered member title"
    titled = query()
    assert titled.graph_revision != changed.graph_revision
    with session_factory.begin() as session:
        primary = session.get(WorkspaceTreeNode, primary_id)
        primary.parent_id = next(f for f in corpus.folder_ids if f != original_parent)
    moved = query()
    assert moved.graph_revision != titled.graph_revision
    assert moved.counts == titled.counts
    assert moved.folder_topology != titled.folder_topology
    assert detailed == []
