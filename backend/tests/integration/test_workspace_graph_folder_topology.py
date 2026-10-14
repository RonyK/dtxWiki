"""Real authored heads and authorized primary placements feed folder layout."""

from __future__ import annotations

from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import backend.services.workspace_graph_service as graph
from backend.models.context import ContextCitation, ContextPageVersion, PageLink
from backend.models.tree import WorkspaceTreeNode
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    WorkspaceGraphCorpusBuilder,
    corpus_actor,
)
from backend.tests.utils.workspace_graph_fixture_identity import fixture_time

pytestmark = pytest.mark.integration


def _build(session, corpus_key):
    builder = WorkspaceGraphCorpusBuilder(
        session, corpus_key=corpus_key, title="Folder topology corpus"
    )
    folders = {
        name: builder.add_folder(index, section=section, title=name)
        for index, (name, section) in enumerate(
            [
                ("Parent", "context"),
                ("Child", "context"),
                ("Other", "context"),
                ("Secondary only", "context"),
                ("Hidden only", "context"),
                ("Library parent", "library"),
                ("Library child", "library"),
                ("Restricted only", "library"),
                ("Empty", "context"),
            ]
        )
    }
    session.flush()
    session.get(WorkspaceTreeNode, folders["Child"]).parent_id = folders["Parent"]
    session.get(WorkspaceTreeNode, folders["Library child"]).parent_id = folders[
        "Library parent"
    ]
    pages = {
        name: builder.add_context(
            index,
            slug=name.casefold().replace(" ", "-"),
            title=name,
            published=name != "Hidden draft",
        )
        for index, name in enumerate(
            ["Alpha", "Beta", "Unplaced", "Hidden ancestor child", "Hidden draft"]
        )
    }
    builder.place(
        0,
        entity_kind="context",
        entity_id=pages["Alpha"],
        title="Alpha",
        folder_id=folders["Child"],
    )
    builder.place(
        1,
        entity_kind="context",
        entity_id=pages["Beta"],
        title="Beta",
        folder_id=folders["Other"],
    )
    secondary_id = builder.place(
        2,
        entity_kind="context",
        entity_id=pages["Alpha"],
        title="Alpha elsewhere",
        folder_id=folders["Secondary only"],
        primary=False,
    )
    hidden_parent_id = builder.place(
        3,
        entity_kind="context",
        entity_id=pages["Hidden draft"],
        title="Hidden draft",
        folder_id=folders["Hidden only"],
    )
    hidden_child_id = builder.place(
        4,
        entity_kind="context",
        entity_id=pages["Hidden ancestor child"],
        title="Hidden ancestor child",
        folder_id=folders["Other"],
    )
    session.flush()
    session.get(WorkspaceTreeNode, hidden_child_id).parent_id = hidden_parent_id
    library, library_version = builder.add_library(
        0, title="Visible Library", uri="https://fixtures.invalid/topology/visible"
    )
    restricted, restricted_version = builder.add_library(
        1,
        title="Restricted Library",
        uri="https://fixtures.invalid/topology/restricted",
        restricted=True,
    )
    for index, (entity_id, name) in enumerate(
        [(library, "Library child"), (restricted, "Restricted only")], start=5
    ):
        builder.place(
            index,
            entity_kind="library",
            entity_id=entity_id,
            title=name,
            folder_id=folders[name],
        )
    for index, (source, target, weight) in enumerate(
        [("Alpha", "Beta", 7), ("Beta", "Alpha", 2), ("Alpha", "Unplaced", 3)]
    ):
        builder.add_relation(
            index,
            source_context_id=pages[source],
            target_kind="context_page",
            target_id=pages[target],
            edge_kind="wikilink",
            weight=weight,
        )
    builder.add_relation(
        3,
        source_context_id=pages["Alpha"],
        target_kind="library_item_version",
        target_id=library_version,
        edge_kind="citation_source",
        weight=300,
    )
    builder.add_relation(
        4,
        source_context_id=pages["Hidden draft"],
        target_kind="context_page",
        target_id=pages["Alpha"],
        edge_kind="wikilink",
        weight=999_999,
    )
    builder.add_relation(
        5,
        source_context_id=pages["Alpha"],
        target_kind="library_item_version",
        target_id=restricted_version,
        edge_kind="citation_source",
    )
    corpus = builder.finish(corpus_key)
    return corpus, {
        "folders": folders,
        "pages": pages,
        "library": library,
        "library_version": library_version,
        "restricted_version": restricted_version,
        "secondary_id": secondary_id,
    }


def _query(session, corpus, **kwargs):
    return WorkspaceGraphService(cursor_secret="folder-topology-test-secret").query(
        session,
        corpus.workspace_id,
        request=WorkspaceGraphOverviewRequest(**kwargs),
        actor=corpus_actor(corpus, role="viewer"),
    )


def _membership_counts(scene):
    return Counter(
        {
            (row.node_id, row.folder_id, row.entity_kind): row.member_count
            for row in scene.folder_topology.memberships
        }
    )


def test_exact_topology_has_primary_nested_closure_unplaced_and_no_secondary_island(
    session_factory,
):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, "folder-topology-exact")
        scene = _query(session, corpus)
    topology = scene.folder_topology
    folders = fixture["folders"]
    assert scene.representation == "exact"
    assert scene.counts.candidate_documents == 5
    assert scene.counts.candidate_relations == 4
    assert topology.schema_version == "workspace-graph-folders.v1"
    assert {
        (row.id, row.parent_folder_id, row.direct_member_count)
        for row in topology.folders
    } == {
        (folders["Parent"], None, 0),
        (folders["Child"], folders["Parent"], 1),
        (folders["Other"], None, 1),
        (folders["Library parent"], None, 0),
        (folders["Library child"], folders["Library parent"], 1),
    }
    assert _membership_counts(scene) == Counter(
        {
            (
                f"document:context:{fixture['pages'][name]}",
                folders[folder] if folder is not None else None,
                "context",
            ): 1
            for name, folder in [
                ("Alpha", "Child"),
                ("Beta", "Other"),
                ("Unplaced", None),
                ("Hidden ancestor child", None),
            ]
        }
        | {
            (
                f"document:library:{fixture['library']}",
                folders["Library child"],
                "library",
            ): 1
        }
    )
    alpha = next(
        node for node in scene.nodes if node.entity_id == fixture["pages"]["Alpha"]
    )
    assert [row.tree_node_id for row in alpha.also_appears_in] == [
        fixture["secondary_id"]
    ]
    assert folders["Secondary only"] not in {row.id for row in topology.folders}
    assert {edge.relation_count for edge in scene.edges} == {1}
    assert sorted(edge.occurrence_weight for edge in scene.edges) == [1, 2, 3, 7]
    assert sorted(row.occurrence_weight for row in topology.edge_slices) == [1, 2, 3, 7]
    assert not any(
        key in scene.model_dump_json()
        for key in ['"x":', '"y":', '"community":', '"coordinates":']
    )


def test_one_mixed_aggregate_retains_correlated_folder_kind_self_edge_slices(
    session_factory,
):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, "folder-topology-mixed-aggregate")
        scene = _query(session, corpus, budget={"documents": 1, "relations": 6})
    assert len(scene.nodes) == 1
    cluster = scene.nodes[0]
    assert cluster.grain == "cluster"
    assert cluster.member_count == 5
    assert all(edge.source == edge.target == cluster.id for edge in scene.edges)
    assert _membership_counts(scene) == Counter(
        {
            (cluster.id, fixture["folders"]["Child"], "context"): 1,
            (cluster.id, fixture["folders"]["Other"], "context"): 1,
            (cluster.id, None, "context"): 2,
            (cluster.id, fixture["folders"]["Library child"], "library"): 1,
        }
    )
    edges = {edge.relation_kind: edge for edge in scene.edges}
    assert edges["wikilink"].relation_count == 3
    assert edges["wikilink"].occurrence_weight == 12
    assert edges["citation_source"].relation_count == 1
    assert edges["citation_source"].occurrence_weight == 1
    parts = {
        (
            row.edge_id,
            row.source_folder_id,
            row.source_entity_kind,
            row.target_folder_id,
            row.target_entity_kind,
            row.relation_count,
            row.occurrence_weight,
        )
        for row in scene.folder_topology.edge_slices
    }
    assert parts == {
        (
            edges["wikilink"].id,
            fixture["folders"][source],
            "context",
            fixture["folders"][target] if target else None,
            "context",
            1,
            weight,
        )
        for source, target, weight in [
            ("Child", "Other", 7),
            ("Other", "Child", 2),
            ("Child", None, 3),
        ]
    } | {
        (
            edges["citation_source"].id,
            fixture["folders"]["Child"],
            "context",
            fixture["folders"]["Library child"],
            "library",
            1,
            1,
        )
    }


def test_topology_is_after_filters_not_self_excluding_folder_facets(session_factory):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, "folder-topology-filtered")
        scene = _query(session, corpus, search="Alpha")
        folder_scene = _query(
            session, corpus, folders={"only": [fixture["folders"]["Child"]]}
        )
    assert (
        scene.counts.candidate_documents == folder_scene.counts.candidate_documents == 1
    )
    expected = {fixture["folders"]["Parent"], fixture["folders"]["Child"]}
    assert {row.id for row in scene.folder_topology.folders} == expected
    assert {row.id for row in folder_scene.folder_topology.folders} == expected
    assert str(fixture["folders"]["Other"]) in {
        row.value for row in folder_scene.facet_counts.folders
    }
    assert scene.folder_topology.edge_slices == []
    assert folder_scene.folder_topology.edge_slices == []


def test_kind_floor_omission_retains_only_slices_for_retained_real_edges(
    session_factory,
):
    with session_factory.begin() as session:
        corpus, _fixture = _build(session, "folder-topology-kind-floor")
        scene = _query(session, corpus, budget={"documents": 1, "relations": 1})
    assert scene.counts.candidate_documents == 5
    assert scene.counts.candidate_relations == 4
    assert len(scene.edges) == 1
    assert {row.edge_id for row in scene.folder_topology.edge_slices} == {
        scene.edges[0].id
    }
    assert sum(row.relation_count for row in scene.folder_topology.edge_slices) == (
        scene.edges[0].relation_count
    )
    assert sum(row.occurrence_weight for row in scene.folder_topology.edge_slices) == (
        scene.edges[0].occurrence_weight
    )
    assert sum(row.member_count for row in scene.folder_topology.memberships) == 5


@pytest.mark.parametrize("aggregated", [False, True])
def test_hidden_folder_decoys_and_hidden_weights_preserve_entire_wire(
    session_factory, aggregated
):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, f"folder-topology-privacy-{aggregated}")
        kwargs = {"budget": {"documents": 1, "relations": 6}} if aggregated else {}
        before = _query(session, corpus, **kwargs).model_dump_json()
        hidden_folder = session.get(
            WorkspaceTreeNode, fixture["folders"]["Hidden only"]
        )
        restricted_folder = session.get(
            WorkspaceTreeNode, fixture["folders"]["Restricted only"]
        )
        hidden_folder.title = "HIDDEN PRIVACY SENTINEL"
        hidden_folder.parent_id = fixture["folders"]["Other"]
        restricted_folder.title = "RESTRICTED PRIVACY SENTINEL"
        restricted_folder.parent_id = fixture["folders"]["Library parent"]
        hidden_link = session.scalar(
            select(PageLink).where(
                PageLink.workspace_id == corpus.workspace_id,
                PageLink.source_page_id == fixture["pages"]["Hidden draft"],
            )
        )
        hidden_link.occurrence_count = 2_000_000
        session.add(
            WorkspaceTreeNode(
                id=uuid4(),
                workspace_id=corpus.workspace_id,
                section="context",
                node_kind="folder",
                parent_id=None,
                sort_key="private-empty",
                title="EMPTY PRIVACY SENTINEL",
                slug="private-empty",
                target_id=None,
                visibility_status="visible",
            )
        )
        session.flush()
        after = _query(session, corpus, **kwargs).model_dump_json()
    assert after == before
    assert "PRIVACY SENTINEL" not in after


def test_visible_occurrence_and_folder_parent_changes_invalidate_revision(
    session_factory,
):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, "folder-topology-revision")
        before = _query(session, corpus)
        page_link = session.scalar(
            select(PageLink).where(
                PageLink.workspace_id == corpus.workspace_id,
                PageLink.source_page_id == fixture["pages"]["Alpha"],
                PageLink.target_page_id == fixture["pages"]["Beta"],
            )
        )
        page_link.occurrence_count = 11
        session.flush()
        changed_weight = _query(session, corpus)
        assert changed_weight.graph_revision != before.graph_revision
        assert changed_weight.generated_at == before.generated_at
        assert sorted(edge.occurrence_weight for edge in changed_weight.edges) == [
            1,
            2,
            3,
            11,
        ]
        session.get(WorkspaceTreeNode, fixture["folders"]["Child"]).parent_id = fixture[
            "folders"
        ]["Other"]
        session.flush()
        changed_parent = _query(session, corpus)
    assert changed_parent.graph_revision != changed_weight.graph_revision
    assert {row.id for row in changed_parent.folder_topology.folders} == {
        fixture["folders"][name]
        for name in ["Other", "Child", "Library parent", "Library child"]
    }
    child = next(
        row
        for row in changed_parent.folder_topology.folders
        if row.id == fixture["folders"]["Child"]
    )
    assert child.parent_folder_id == fixture["folders"]["Other"]


def test_distinct_authored_link_syntax_weights_sum_without_duplicate_relation(
    session_factory,
):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, "folder-topology-occurrences")
        session.add(
            PageLink(
                workspace_id=corpus.workspace_id,
                source_page_id=fixture["pages"]["Alpha"],
                source_version_id=corpus.context_version_ids[0],
                link_syntax="context_url",
                target_slug="beta",
                target_page_id=fixture["pages"]["Beta"],
                occurrence_count=5,
                created_at=fixture_time(8_000),
            )
        )
        # Distinct accepted citation rows contribute one occurrence each, not
        # the arbitrary 300 weight on the non-authoritative EdgeProjection.
        session.add(
            ContextCitation(
                workspace_id=corpus.workspace_id,
                target_kind="context_page_version",
                target_id=corpus.context_version_ids[0],
                target_location={},
                library_item_version_id=fixture["library_version"],
                location={},
                citation_type="raw_source",
                verification_status="verified",
                created_via="manual_input",
                created_by_actor_type="human",
                created_at=fixture_time(8_001),
            )
        )
        session.flush()
        scene = _query(session, corpus)
    assert scene.counts.candidate_relations == 4
    assert sorted(edge.occurrence_weight for edge in scene.edges) == [2, 2, 3, 12]
    assert sorted(
        row.occurrence_weight for row in scene.folder_topology.edge_slices
    ) == [2, 2, 3, 12]


def test_row_budget_disables_whole_optional_topology_without_clipping_scene(
    session_factory, monkeypatch
):
    with session_factory.begin() as session:
        corpus, _fixture = _build(session, "folder-topology-row-budget")
        before = _query(session, corpus)
        monkeypatch.setattr(graph, "WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT", 4)
        capped = _query(session, corpus)
    assert capped.folder_topology is None
    assert capped.warnings == ["folder_topology_row_budget_exceeded"]
    assert capped.nodes == before.nodes
    assert capped.edges == before.edges
    assert capped.counts == before.counts
    assert capped.graph_revision == before.graph_revision
    assert capped.completeness == before.completeness == "complete"
    assert not capped.truncated


def test_citation_occurrence_revision_ignores_stale_heads_and_restricted_targets(
    session_factory,
):
    with session_factory.begin() as session:
        corpus, fixture = _build(session, "folder-topology-citation-revision")
        before = _query(session, corpus)
        existing = session.scalar(
            select(ContextCitation).where(
                ContextCitation.workspace_id == corpus.workspace_id,
                ContextCitation.library_item_version_id == fixture["library_version"],
            )
        )
        timestamp = existing.created_at
        source_head = corpus.context_version_ids[0]

        def citation(target_id, library_version):
            return ContextCitation(
                workspace_id=corpus.workspace_id,
                target_kind="context_page_version",
                target_id=target_id,
                target_location={},
                library_item_version_id=library_version,
                location={},
                citation_type="raw_source",
                verification_status="verified",
                created_via="manual_input",
                created_by_actor_type="human",
                created_at=timestamp,
            )

        stale_head = ContextPageVersion(
            id=uuid4(),
            workspace_id=corpus.workspace_id,
            page_id=fixture["pages"]["Alpha"],
            version_no=2,
            body_md="Unpublished alternative head",
            content_hash="folder-topology-stale-citation",
            actor_type="human",
            created_by_actor_kind="human",
            created_by_actor_id="folder-topology-test",
            version_status="draft",
            last_saved_at=timestamp,
            created_at=timestamp,
        )
        session.add(stale_head)
        session.flush()
        session.add_all(
            [
                citation(stale_head.id, fixture["library_version"]),
                citation(source_head, fixture["restricted_version"]),
            ]
        )
        session.flush()
        noisy = _query(session, corpus)
        assert noisy.model_dump_json() == before.model_dump_json()

        session.add(citation(source_head, fixture["library_version"]))
        session.flush()
        accepted_row_count = session.scalar(
            select(func.count())
            .select_from(ContextCitation)
            .where(
                ContextCitation.workspace_id == corpus.workspace_id,
                ContextCitation.target_kind == "context_page_version",
                ContextCitation.target_id == source_head,
                ContextCitation.library_item_version_id == fixture["library_version"],
            )
        )
        after = _query(session, corpus)

    assert after.generated_at == before.generated_at
    assert after.counts == before.counts
    assert after.graph_revision != before.graph_revision
    citation_edge = next(
        edge for edge in after.edges if edge.relation_kind == "citation_source"
    )
    assert citation_edge.relation_count == 1
    # Exactly two accepted authored rows must produce weight two, with no
    # join multiplication and no deduplication of distinct occurrences.
    assert citation_edge.occurrence_weight == accepted_row_count == 2
    assert (
        next(
            part
            for part in after.folder_topology.edge_slices
            if part.edge_id == citation_edge.id
        ).occurrence_weight
        == 2
    )


@pytest.mark.parametrize("cap", [9, 12, 14])
def test_row_budget_boundary_counts_membership_and_correlated_slices(
    session_factory, monkeypatch, cap
):
    with session_factory.begin() as session:
        corpus, _fixture = _build(session, f"folder-topology-row-stages-{cap}")
        before = _query(session, corpus)
        assert (
            len(before.folder_topology.folders)
            + len(before.folder_topology.memberships)
            + len(before.folder_topology.edge_slices)
        ) == 14
        monkeypatch.setattr(graph, "WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT", cap)
        after = _query(session, corpus)
    assert after.nodes == before.nodes
    assert after.edges == before.edges
    if cap == 14:
        assert after.folder_topology == before.folder_topology
        assert after.warnings == []
    else:
        assert after.folder_topology is None
        assert after.warnings == ["folder_topology_row_budget_exceeded"]
