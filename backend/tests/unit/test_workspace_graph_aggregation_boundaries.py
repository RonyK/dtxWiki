from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

import backend.services.workspace_graph_service as graph
from backend.policy.auth import ActorContext
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _documents(count: int) -> dict:
    keys = [("context", UUID(int=index + 1)) for index in range(count)]
    return {
        key: graph._Document(
            key=key,
            entity_id=key[1],
            entity_kind="context",
            label=f"Document {index}",
            path=f"/context?target={key[1]}",
            head_id=UUID(int=count + index + 1),
            updated_at=_NOW,
        )
        for index, key in enumerate(keys)
    }


def _ring_relations(documents: dict, count: int) -> list:
    keys = list(documents)
    return [
        graph._Relation(
            keys[index % len(keys)],
            keys[(index % len(keys) + index // len(keys) + 1) % len(keys)],
            "wikilink",
            "knowledge",
            _NOW,
        )
        for index in range(count)
    ]


def _service(monkeypatch, documents: dict, relations: list):
    service = graph.WorkspaceGraphService(cursor_secret="boundary-test-secret")
    monkeypatch.setattr(service, "_load_documents", lambda *_args: dict(documents))
    monkeypatch.setattr(service, "_attach_tree_placements", lambda *_args: None)
    monkeypatch.setattr(
        service, "_load_relation_universe", lambda *_args: list(relations)
    )
    return service


def _query(service, **overrides):
    return service.query(
        None,
        UUID(int=42),
        request=WorkspaceGraphOverviewRequest(
            **{"budget": {"documents": 2_000, "relations": 6_000}, **overrides}
        ),
        actor=ActorContext(
            actor_kind="human",
            actor_id="visible-boundary-viewer",
            token_kind="session",
            scopes=(),
            workspace_role="viewer",
            role="viewer",
        ),
    )


@pytest.mark.parametrize("document_count", [1_999, 2_000, 2_001])
def test_document_boundary_keeps_coverage_and_almost_all_exact_documents(
    monkeypatch, document_count
):
    documents = _documents(document_count)
    relations = _ring_relations(documents, document_count)
    payload = _query(_service(monkeypatch, documents, relations))

    assert payload.counts.candidate_documents == document_count
    assert payload.counts.rendered_documents >= min(document_count, 1_998)
    assert len(payload.nodes) <= 2_000
    assert len(payload.edges) <= 6_000
    assert (
        sum(
            1 if node.grain == "document" else node.member_count
            for node in payload.nodes
        )
        == document_count
    )
    assert sum(edge.relation_count for edge in payload.edges) == len(relations)
    assert payload.representation == (
        "exact" if document_count <= 2_000 else "aggregated"
    )


@pytest.mark.parametrize("relation_count", [5_999, 6_000, 6_001])
def test_relation_boundary_preserves_informative_scene(monkeypatch, relation_count):
    documents = _documents(2_000)
    relations = _ring_relations(documents, relation_count)
    payload = _query(_service(monkeypatch, documents, relations))

    assert payload.counts.candidate_relations == relation_count
    assert payload.counts.rendered_documents > 1_900
    assert payload.counts.rendered_relations > 5_800
    assert len(payload.nodes) <= 2_000
    assert len(payload.edges) <= 6_000
    assert sum(edge.relation_count for edge in payload.edges) == relation_count
    assert (
        sum(
            1 if node.grain == "document" else node.member_count
            for node in payload.nodes
        )
        == 2_000
    )
    assert payload.representation == (
        "exact" if relation_count <= 6_000 else "aggregated"
    )


def test_aggregation_hashes_residuals_logarithmically_not_per_revealed_member(
    monkeypatch,
):
    documents = _documents(2_001)
    relations = _ring_relations(documents, 5_000)
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    original = graph._group_member_digest
    member_visits = 0

    def counted(members):
        nonlocal member_visits
        members = tuple(members)
        member_visits += len(members)
        return original(members)

    monkeypatch.setattr(graph, "_group_member_digest", counted)
    groups = graph.WorkspaceGraphService._aggregate_groups(
        documents, relations, document_budget=2_000, relation_budget=6_000, lens="links"
    )

    assert sum(group.is_document for group in groups) == 1_999
    # Allows all binary-search candidates, but not ~2 million residual members
    # from splitting/hash-scanning one member on each of 1,998 iterations.
    assert member_visits < len(documents) * 20


def test_orphan_partitions_do_not_consume_all_exact_document_slots(monkeypatch):
    documents = _documents(5_000)
    connected = dict(list(documents.items())[:2_000])
    relations = _ring_relations(connected, 5_000)
    payload = _query(_service(monkeypatch, documents, relations))
    exact_ids = {node.entity_id for node in payload.nodes if node.grain == "document"}

    assert len(exact_ids & {key[1] for key in connected}) >= 1_990
    assert len(exact_ids - {key[1] for key in connected}) == 0
    assert len(payload.nodes) <= 2_000
    assert (
        sum(
            1 if node.grain == "document" else node.member_count
            for node in payload.nodes
        )
        == 5_000
    )
    assert sum(edge.relation_count for edge in payload.edges) == 5_000


@pytest.mark.parametrize(
    "kind,label",
    [
        ("context", "한" * 201),
        ("library", "f" * 234),
        ("library", "https://example.test/" + "u" * 240),
    ],
)
def test_long_document_labels_are_bounded_at_the_payload_boundary(
    monkeypatch, kind, label
):
    documents = _documents(2)
    document = next(iter(documents.values()))
    document.entity_kind = kind
    document.label = label
    payload = _query(_service(monkeypatch, documents, []))

    node = next(node for node in payload.nodes if node.entity_id == document.entity_id)
    assert node.label == label[:200]
    assert len(payload.nodes) == 2


def test_long_folder_labels_paths_and_secondary_placements_are_bounded(monkeypatch):
    documents = _documents(2)
    placement = graph._Placement(
        tree_node_id=UUID(int=100),
        folder_id=UUID(int=101),
        folder_label="폴더" * 120,
        path="/" + "경로/" * 400,
        ancestor_ids=(),
        folder_ancestors=(
            graph._VisibleFolder(
                id=UUID(int=101), label="폴더" * 120, parent_folder_id=None
            ),
        ),
    )
    for document in documents.values():
        document.primary = placement
        document.secondary = [placement]
        document.secondary_revision = (placement,)
    payload = _query(_service(monkeypatch, documents, []))
    for node in payload.nodes:
        assert node.primary_folder_label == placement.folder_label[:200]
        assert node.also_appears_in[0].label == placement.folder_label[:200]
        assert node.also_appears_in[0].path == placement.path[:1024]
    assert payload.facet_counts.folders[0].label == placement.folder_label[:200]

    rendered = graph.WorkspaceGraphService(
        cursor_secret="folder-test"
    )._materialize_groups(
        [graph._Group(tuple(documents), cluster_kind="folder_cluster")],
        documents,
        [],
        omission_reasons=["document_budget"],
    )
    assert rendered.nodes[0].label == placement.folder_label[:200]


def test_ranked_prefix_is_maximal_under_dense_relation_budget():
    documents = _documents(40)
    keys = list(documents)
    relations = [
        graph._Relation(source, target, "wikilink", "knowledge", _NOW)
        for source in keys
        for target in keys
        if source != target
    ]
    graph.WorkspaceGraphService._apply_metrics(documents, relations)
    groups = graph.WorkspaceGraphService._aggregate_groups(
        documents, relations, document_budget=40, relation_budget=750, lens="links"
    )
    exact_keys = {group.members[0] for group in groups if group.is_document}
    assert len(exact_keys) == 26
    assert graph._mapped_relation_count(groups, relations) == 703
    assert exact_keys == set(keys[:26])
    next_groups = [graph._Group((key,)) for key in keys[:27]] + [
        graph._Group(tuple(keys[27:]), cluster_kind="link_cluster")
    ]
    assert graph._mapped_relation_count(next_groups, relations) > 750


def test_aggregation_is_independent_of_input_iteration_order(monkeypatch):
    # Input-order determinism is independent of the production capacity. Both
    # document and relation caps still force actual aggregation in this scene.
    documents = _documents(41)
    relations = _ring_relations(documents, 161)
    budget = {"documents": 40, "relations": 150}
    first = _query(_service(monkeypatch, documents, relations), budget=budget)
    second = _query(
        _service(
            monkeypatch,
            dict(reversed(list(documents.items()))),
            list(reversed(relations)),
        ),
        budget=budget,
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_boundary_residual_paginates_with_coverage_and_deterministic_cursors(
    monkeypatch,
):
    documents = _documents(2_001)
    relations = _ring_relations(documents, 5_000)
    service = _service(monkeypatch, documents, relations)
    base = _query(service)
    cluster = next(node for node in base.nodes if node.grain == "cluster")
    base_ids = {node.entity_id for node in base.nodes if node.grain == "document"}
    residual_ids = {key[1] for key in documents} - base_ids
    revealed = set()
    cursor = base.expansion_cursor
    for _ in range(cluster.member_count):
        assert cursor is not None
        expansion = {"cluster_id": cluster.id, "cursor": cursor}
        page = _query(service, expansion=expansion)
        replay = _query(service, expansion=expansion)
        assert page.model_dump_json() == replay.model_dump_json()
        assert len(page.nodes) <= 2_000
        assert len(page.edges) <= 6_000
        assert (
            sum(
                1 if node.grain == "document" else node.member_count
                for node in page.nodes
            )
            == 2_001
        )
        assert sum(edge.relation_count for edge in page.edges) == len(relations)
        revealed.update(
            node.entity_id
            for node in page.nodes
            if node.grain == "document" and node.entity_id in residual_ids
        )
        cursor = page.expansion_cursor
        if cursor is None:
            break
    assert cursor is None
    assert revealed == residual_ids


@pytest.mark.parametrize("relation_budget", [1, 2, 3])
def test_mixed_relation_identity_floor_obeys_tiny_caps_with_explicit_omission(
    monkeypatch, relation_budget
):
    documents = _documents(3)
    first, second, third = documents
    relations = [
        graph._Relation(first, second, "wikilink", "knowledge", _NOW),
        graph._Relation(second, third, "citation_page", "evidence", _NOW),
        graph._Relation(third, first, "citation_source", "evidence", _NOW),
    ]
    service = _service(monkeypatch, documents, relations)
    payload = _query(
        service, lens="compare", budget={"documents": 3, "relations": relation_budget}
    )
    replay = _query(
        service, lens="compare", budget={"documents": 3, "relations": relation_budget}
    )
    assert len(payload.edges) == relation_budget
    assert payload.counts.candidate_relations == 3
    assert payload.counts.rendered_relations == relation_budget
    assert (
        sum(
            1 if node.grain == "document" else node.member_count
            for node in payload.nodes
        )
        == 3
    )
    assert payload.model_dump_json() == replay.model_dump_json()
    if relation_budget == 3:
        assert payload.representation == "exact"
        # Compare is independently a partial projection, not a budget omission.
        assert "relation_budget" not in payload.omission_reasons
        assert sum(edge.relation_count for edge in payload.edges) == 3
    else:
        assert payload.representation == "aggregated"
        assert payload.truncated is True
        assert "relation_budget" in payload.omission_reasons
        assert sum(edge.relation_count for edge in payload.edges) < 3
        assert len(payload.nodes) == 1
        cluster = payload.nodes[0]
        assert cluster.cluster_kind == "other_cluster"
        assert cluster.relation_count == 3
        assert payload.expansion_cursor is None
        assert payload.capabilities.expand is False
        full_counts = {
            facet.value: facet.count for facet in payload.facet_counts.relation_kinds
        }
        assert sum(full_counts.values()) == 3
        assert (
            len({(edge.relation_kind, edge.layer) for edge in payload.edges})
            == relation_budget
        )
        for edge in payload.edges:
            assert edge.aggregate is True
            assert edge.source == edge.target == cluster.id
            assert edge.relation_count == full_counts[edge.relation_kind]
        assert full_counts.keys() - {edge.relation_kind for edge in payload.edges}


def test_tiny_mixed_identity_omission_is_capped_without_partial_compare_projection(
    monkeypatch,
):
    documents = _documents(3)
    first, second, third = documents
    relations = [
        graph._Relation(first, second, "citation_page", "evidence", _NOW),
        graph._Relation(second, third, "citation_source", "evidence", _NOW),
    ]
    service = _service(monkeypatch, documents, relations)
    capped = _query(service, lens="evidence", budget={"documents": 3, "relations": 1})
    complete = _query(service, lens="evidence", budget={"documents": 3, "relations": 2})
    assert capped.completeness == "capped"
    assert capped.omission_reasons == ["relation_budget"]
    assert capped.counts.candidate_relations == 2
    assert capped.counts.rendered_relations == 1
    assert len(capped.edges) == 1
    assert complete.completeness == "complete"
    assert complete.representation == "exact"
    assert complete.omission_reasons == []
    assert len(complete.edges) == 2


@pytest.mark.parametrize("pairs,document_budget", [(4, 3), (10, 5), (2_500, 2_000)])
def test_small_components_are_compacted_before_spending_exact_document_slots(
    monkeypatch, pairs, document_budget
):
    documents = _documents(pairs * 2)
    keys = list(documents)
    relations = [
        graph._Relation(keys[index], keys[index + 1], "wikilink", "knowledge", _NOW)
        for index in range(0, len(keys), 2)
    ]
    service = _service(monkeypatch, documents, relations)
    payload = _query(service, budget={"documents": document_budget, "relations": 6_000})
    assert payload.counts.rendered_documents == document_budget - 1
    assert payload.counts.rendered_clusters == 1
    cluster = next(node for node in payload.nodes if node.grain == "cluster")
    assert cluster.cluster_kind == "other_cluster"
    assert len(payload.nodes) == document_budget
    assert sum(edge.relation_count for edge in payload.edges) == len(relations)
    assert sum(
        1 if node.grain == "document" else node.member_count for node in payload.nodes
    ) == len(documents)
    replay = _query(
        _service(
            monkeypatch,
            dict(reversed(list(documents.items()))),
            list(reversed(relations)),
        ),
        budget={"documents": document_budget, "relations": 6_000},
    )
    assert replay.model_dump_json() == payload.model_dump_json()
