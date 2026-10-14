"""Deterministic Workspace Knowledge Map corpus acceptance tests."""

from __future__ import annotations

from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    build_folder_mismatch_80,
    build_knowledge_40,
    build_star_250_750,
    corpus_actor,
    fixture_time,
)


def _service() -> WorkspaceGraphService:
    return WorkspaceGraphService(cursor_secret="workspace-graph-acceptance-secret")


def _facet_counts(values) -> dict[str, int]:
    return {value.value: value.count for value in values}


def _facet_label_counts(values) -> dict[str, int]:
    return {value.label: value.count for value in values}


def test_knowledge_40_is_exact_canonical_and_uses_distinct_authored_metrics(
    session_factory,
):
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session)
        service = _service()
        payload = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=corpus_actor(corpus, role="owner"),
        )
        evidence_payload = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="evidence"),
            actor=corpus_actor(corpus, role="owner"),
        )

    document_nodes = [node for node in payload.nodes if node.grain == "document"]
    context_nodes = [node for node in document_nodes if node.entity_kind == "context"]
    library_nodes = [node for node in document_nodes if node.entity_kind == "library"]

    assert payload.representation == "exact"
    assert payload.completeness == "complete"
    assert payload.truncated is False
    assert payload.omission_reasons == []
    assert payload.counts.model_dump() == {
        "candidate_documents": 40,
        "candidate_relations": 40,
        "rendered_documents": 40,
        "rendered_clusters": 0,
        "rendered_relations": 40,
    }
    assert len(context_nodes) == 32
    assert len(library_nodes) == 8
    assert {node.entity_id for node in library_nodes} == set(corpus.library_ids)
    assert all(
        node.entity_id not in corpus.library_version_ids for node in library_nodes
    )

    # The fixture contains one high-occurrence wikilink and a self-link.  Node
    # metrics count distinct visible source documents: occurrence weight does
    # not multiply the metric and the self-link does not survive normalization.
    assert sum(node.reference_count for node in document_nodes) == 40
    assert sum(node.evidence_count for node in document_nodes) == 0
    assert all(edge.source != edge.target for edge in payload.edges)
    assert {edge.relation_kind for edge in payload.edges} == {"wikilink"}
    assert all(
        edge.aggregate is False and edge.relation_count == 1 for edge in payload.edges
    )
    assert payload.generated_at == fixture_time(5_048)
    assert len(payload.graph_revision) >= 16

    assert _facet_counts(payload.facet_counts.document_kinds) == {
        "context": 32,
        "library": 8,
    }
    assert _facet_counts(payload.facet_counts.relation_kinds) == {"wikilink": 40}
    assert payload.capabilities.evidence is False

    evidence_nodes = [
        node for node in evidence_payload.nodes if node.grain == "document"
    ]
    assert evidence_payload.representation == "exact"
    assert evidence_payload.completeness == "complete"
    assert evidence_payload.counts.candidate_documents == 40
    assert evidence_payload.counts.candidate_relations == 8
    assert sum(node.reference_count for node in evidence_nodes) == 0
    assert sum(node.evidence_count for node in evidence_nodes) == 8
    assert _facet_counts(evidence_payload.facet_counts.relation_kinds) == {
        "citation_source": 8
    }
    assert evidence_payload.capabilities.evidence is True
    assert evidence_payload.graph_revision == payload.graph_revision
    assert evidence_payload.generated_at == payload.generated_at


def test_knowledge_40_compare_discloses_its_truthful_partial_boundary(
    session_factory,
):
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session)
        payload = _service().query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="compare"),
            actor=corpus_actor(corpus, role="owner"),
        )

    assert payload.representation == "exact"
    assert payload.completeness == "partial"
    assert payload.truncated is True
    assert payload.counts.candidate_documents == 40
    assert payload.counts.rendered_documents == 40
    assert payload.counts.candidate_relations == 48
    assert payload.counts.rendered_relations == 48
    assert payload.omission_reasons == ["projection_partial"]
    assert payload.warnings == ["compare_projection_not_available"]
    assert payload.capabilities.compare is False


def test_folder_mismatch_80_uses_primary_tree_placement_not_uri_text(
    session_factory,
):
    with session_factory.begin() as session:
        corpus = build_folder_mismatch_80(session)
        payload = _service().query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="directory"),
            actor=corpus_actor(corpus, role="viewer"),
        )

    document_nodes = [node for node in payload.nodes if node.grain == "document"]
    assert payload.representation == "exact"
    assert payload.completeness == "complete"
    assert payload.counts.candidate_documents == 80
    assert payload.counts.candidate_relations == 0
    assert payload.edges == []

    by_id = {node.entity_id: node for node in document_nodes}
    assert set(by_id) == set(corpus.document_ids)
    for entity_id, expected_label in corpus.primary_folder_labels.items():
        node = by_id[entity_id]
        assert node.primary_folder_label == expected_label
        # Source slugs say "claimed-folder-*" while placement comes from the
        # actual tree. Public paths remain valid SPA navigation hrefs; an
        # implementation deriving folders from source URI text cannot satisfy
        # the slug/title mismatch and the placement assertion together.
        if node.entity_kind == "context":
            slug = corpus.context_slugs[entity_id]
            assert slug.startswith("claimed-folder-")
            claimed_index = int(slug.split("-")[2])
            assert expected_label != f"Actual Context Folder {claimed_index}"
            assert node.path == f"/context?target={entity_id}"
        else:
            assert "claimed-folder-" in corpus.library_uris[entity_id]
            assert node.path == f"/library?target={entity_id}"

    secondary_nodes = {
        entity_id: by_id[entity_id] for entity_id in corpus.secondary_folder_labels
    }
    assert len(secondary_nodes) == 16
    for entity_id, expected_labels in corpus.secondary_folder_labels.items():
        node = secondary_nodes[entity_id]
        assert tuple(placement.label for placement in node.also_appears_in) == (
            expected_labels
        )
        assert all(
            placement.tree_node_id != node.primary_folder_id
            for placement in node.also_appears_in
        )

    folder_facets = _facet_label_counts(payload.facet_counts.folders)
    assert folder_facets == {
        **{f"Actual Context Folder {index}": 8 for index in range(8)},
        **{f"Actual Library Folder {index}": 2 for index in range(8)},
    }


def test_star_250_750_remains_exact_at_the_provisional_boundary(session_factory):
    with session_factory.begin() as session:
        corpus = build_star_250_750(session)
        payload = _service().query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=corpus_actor(corpus, role="viewer"),
        )

    assert len(corpus.document_ids) == 250
    assert len(corpus.relation_ids) == 750
    assert payload.representation == "exact"
    assert payload.completeness == "complete"
    assert payload.truncated is False
    assert payload.omission_reasons == []
    assert payload.counts.model_dump() == {
        "candidate_documents": 250,
        "candidate_relations": 750,
        "rendered_documents": 250,
        "rendered_clusters": 0,
        "rendered_relations": 750,
    }
    assert len(payload.nodes) == 250
    assert len(payload.edges) == 750
    assert sum(edge.relation_count for edge in payload.edges) == 750
    assert all(edge.aggregate is False for edge in payload.edges)

    nodes_by_entity = {
        node.entity_id: node for node in payload.nodes if node.grain == "document"
    }
    assert nodes_by_entity[corpus.context_ids[0]].reference_count == 249
    assert sum(node.reference_count for node in nodes_by_entity.values()) == 750
    assert _facet_counts(payload.facet_counts.relation_kinds) == {"wikilink": 750}
