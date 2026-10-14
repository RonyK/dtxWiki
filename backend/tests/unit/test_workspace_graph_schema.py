from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.schemas.workspace_graph import (
    WorkspaceGraphOverviewRequest,
    WorkspaceGraphScenePayload,
)


def _payload(**updates):
    node_id = f"document:context:{uuid4()}"
    values = {
        "graph_revision": "wgr1:" + "a" * 64,
        "generated_at": datetime(2026, 8, 5, tzinfo=UTC),
        "algorithm_version": "test.v1",
        "metric_basis": "visible-test.v1",
        "representation": "exact",
        "completeness": "complete",
        "truncated": False,
        "counts": {
            "candidate_documents": 1,
            "candidate_relations": 0,
            "rendered_documents": 1,
            "rendered_clusters": 0,
            "rendered_relations": 0,
        },
        "nodes": [
            {
                "grain": "document",
                "id": node_id,
                "entity_kind": "context",
                "entity_id": uuid4(),
                "label": "Visible page",
                "path": "/context?target=visible",
                "primary_folder_label": "No visible placement",
            }
        ],
        "edges": [],
        "facet_counts": {},
    }
    values.update(updates)
    return WorkspaceGraphScenePayload(**values)


def test_workspace_request_keeps_bounded_default_and_accepts_explicit_10k_scene():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    request = WorkspaceGraphOverviewRequest(
        time={"from": start, "to": start + timedelta(days=1)}
    )
    dumped = request.model_dump(mode="json", by_alias=True)
    assert dumped["lens"] == "combined"
    assert WorkspaceGraphOverviewRequest(lens="combined").lens == "combined"
    assert dumped["budget"] == {"documents": 250, "relations": 750}
    assert dumped["time"]["from"] == "2026-08-01T00:00:00Z"

    large = WorkspaceGraphOverviewRequest(
        budget={"documents": 2_000, "relations": 6_000}
    )
    assert large.budget.documents == 2_000
    assert large.budget.relations == 6_000
    explicit = WorkspaceGraphOverviewRequest(
        budget={"documents": 10_000, "relations": 100_000}
    )
    assert explicit.budget.documents == 10_000
    assert explicit.budget.relations == 100_000
    with pytest.raises(ValidationError):
        WorkspaceGraphOverviewRequest(budget={"documents": 10_001, "relations": 750})
    with pytest.raises(ValidationError):
        WorkspaceGraphOverviewRequest(budget={"documents": 250, "relations": 100_001})


def test_workspace_request_rejects_naive_or_reversed_time_and_folder_ambiguity():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        WorkspaceGraphOverviewRequest(time={"from": datetime(2026, 8, 1)})
    with pytest.raises(ValidationError):
        WorkspaceGraphOverviewRequest(time={"from": now, "to": now - timedelta(1)})

    folder_id = uuid4()
    with pytest.raises(ValidationError):
        WorkspaceGraphOverviewRequest(
            folders={"only": [folder_id], "include": [folder_id]}
        )


def test_scene_requires_truthful_document_and_relation_coverage():
    valid = _payload()
    assert valid.workspace_scope_considered is True

    with pytest.raises(ValidationError, match="represented exactly once"):
        _payload(
            counts={
                "candidate_documents": 2,
                "candidate_relations": 0,
                "rendered_documents": 1,
                "rendered_clusters": 0,
                "rendered_relations": 0,
            }
        )


def test_cluster_nodes_have_no_fake_entity_uuid_and_disclose_caps():
    cluster_id = "cluster:link_cluster:" + "b" * 24
    payload = _payload(
        representation="aggregated",
        completeness="capped",
        truncated=True,
        omission_reasons=["document_budget"],
        counts={
            "candidate_documents": 3,
            "candidate_relations": 0,
            "rendered_documents": 0,
            "rendered_clusters": 1,
            "rendered_relations": 0,
        },
        nodes=[
            {
                "grain": "cluster",
                "id": cluster_id,
                "cluster_kind": "link_cluster",
                "label": "One visible component",
                "member_count": 3,
                "relation_count": 0,
            }
        ],
    )
    assert payload.nodes[0].grain == "cluster"
    assert "entity_id" not in payload.nodes[0].model_dump()

    with pytest.raises(ValidationError):
        _payload(
            representation="aggregated",
            completeness="capped",
            truncated=True,
            omission_reasons=["document_budget"],
            counts={
                "candidate_documents": 3,
                "candidate_relations": 0,
                "rendered_documents": 0,
                "rendered_clusters": 1,
                "rendered_relations": 0,
            },
            nodes=[
                {
                    "grain": "cluster",
                    "id": cluster_id,
                    "cluster_kind": "link_cluster",
                    "label": "Bad cluster",
                    "member_count": 3,
                    "relation_count": 0,
                    "entity_id": uuid4(),
                }
            ],
        )


def _relation_omission_payload(
    *,
    representation="aggregated",
    completeness="capped",
    truncated=True,
    omission_reasons=("relation_budget",),
    relation_count=1,
):
    cluster_id = "cluster:other_cluster:" + "c" * 24
    if representation == "aggregated":
        nodes = [
            {
                "grain": "cluster",
                "id": cluster_id,
                "cluster_kind": "other_cluster",
                "label": "Visible mixed relationships",
                "member_count": 3,
                "relation_count": 3,
            }
        ]
        source = target = cluster_id
    else:
        nodes = [_payload().nodes[0].model_dump() for _ in range(3)]
        source, target = nodes[0]["id"], nodes[1]["id"]
    return _payload(
        representation=representation,
        completeness=completeness,
        truncated=truncated,
        omission_reasons=list(omission_reasons),
        counts={
            "candidate_documents": 3,
            "candidate_relations": 3,
            "rendered_documents": 0 if representation == "aggregated" else 3,
            "rendered_clusters": 1 if representation == "aggregated" else 0,
            "rendered_relations": 1,
        },
        nodes=nodes,
        edges=[
            {
                "id": "relation:citation_page:capped-test",
                "source": source,
                "target": target,
                "relation_kind": "citation_page",
                "layer": "evidence",
                "aggregate": representation == "aggregated",
                "relation_count": relation_count,
            }
        ],
        facet_counts={
            "relation_kinds": [
                {"value": kind, "label": kind, "count": 1}
                for kind in ("wikilink", "citation_page", "citation_source")
            ]
        },
    )


@pytest.mark.parametrize("completeness", ["capped", "partial"])
def test_irreducible_relation_floor_can_disclose_a_capped_deficit(completeness):
    payload = _relation_omission_payload(completeness=completeness)
    assert payload.counts.candidate_relations == 3
    assert payload.counts.rendered_relations == 1
    assert sum(edge.relation_count for edge in payload.edges) == 1
    assert payload.truncated is True
    assert "relation_budget" in payload.omission_reasons


@pytest.mark.parametrize(
    "updates",
    [
        {"representation": "exact"},
        {"completeness": "complete", "truncated": False, "omission_reasons": ()},
        {"truncated": False},
        {"omission_reasons": ()},
        {"omission_reasons": ("document_budget",)},
        {"relation_count": 4},
    ],
)
def test_relation_deficits_require_explicit_aggregate_caps_and_never_allow_overcounts(
    updates,
):
    with pytest.raises(ValidationError, match="relation_count"):
        _relation_omission_payload(**updates)


@pytest.mark.parametrize(
    "invalid_shape",
    [
        "partial_same_kind",
        "zero_edges",
        "ordinary_multi_cluster",
        "wrong_layer",
        "non_aggregate",
        "missing_facets",
        "duplicate_facets",
        "semantic_cluster",
        "wrong_cluster_relation_count",
    ],
)
def test_capped_deficit_guard_does_not_mask_ordinary_edge_loss(invalid_shape):
    values = _relation_omission_payload().model_dump(mode="python")
    if invalid_shape == "partial_same_kind":
        values["facet_counts"]["relation_kinds"] = [
            {"value": "citation_page", "label": "Cites", "count": 2},
            {"value": "wikilink", "label": "Links", "count": 1},
        ]
    elif invalid_shape == "zero_edges":
        values["edges"] = []
        values["counts"]["rendered_relations"] = 0
    elif invalid_shape == "ordinary_multi_cluster":
        values["nodes"][0]["member_count"] = 2
        values["nodes"].append(
            {
                "grain": "cluster",
                "id": "cluster:other_cluster:second",
                "cluster_kind": "other_cluster",
                "label": "Second residual",
                "member_count": 1,
                "relation_count": 0,
            }
        )
        values["counts"]["rendered_clusters"] = 2
    elif invalid_shape == "wrong_layer":
        values["edges"][0]["layer"] = "knowledge"
    elif invalid_shape == "non_aggregate":
        values["edges"][0]["aggregate"] = False
    elif invalid_shape == "missing_facets":
        values["facet_counts"]["relation_kinds"] = []
    elif invalid_shape == "duplicate_facets":
        values["facet_counts"]["relation_kinds"].append(
            {
                "value": "citation_page",
                "label": "Duplicate",
                "count": 0,
            }
        )
    elif invalid_shape == "semantic_cluster":
        values["nodes"][0]["cluster_kind"] = "link_cluster"
    elif invalid_shape == "wrong_cluster_relation_count":
        values["nodes"][0]["relation_count"] = 2
    with pytest.raises(ValidationError, match="relation_count"):
        WorkspaceGraphScenePayload(**values)


@pytest.mark.parametrize("mode", ["include", "exclude", "only"])
def test_folder_filters_accept_exact_unplaced_sentinel_with_uuid(mode):
    folder_id = uuid4()
    request = WorkspaceGraphOverviewRequest(folders={mode: [folder_id, "unplaced"]})
    assert getattr(request.folders, mode) == [folder_id, "unplaced"]
    assert request.model_dump(mode="json")["folders"][mode] == [
        str(folder_id),
        "unplaced",
    ]


@pytest.mark.parametrize("mode", ["include", "exclude", "only"])
@pytest.mark.parametrize("invalid", ["UNPLACED", "unplaced ", "not-a-folder"])
def test_folder_filters_do_not_accept_other_strings(mode, invalid):
    with pytest.raises(ValidationError):
        WorkspaceGraphOverviewRequest(folders={mode: [invalid]})


def test_unplaced_folder_filters_preserve_bounds_duplicates_and_conflicts():
    folder_id = uuid4()
    for folders in (
        {"include": ["unplaced", "unplaced"]},
        {"exclude": ["unplaced", "unplaced"]},
        {"only": [folder_id, str(folder_id)]},
        {"include": ["unplaced"], "exclude": ["unplaced"]},
        {"only": ["unplaced"], "include": [folder_id]},
        {"only": ["unplaced"], "exclude": [folder_id]},
        {"include": [uuid4() for _ in range(64)] + ["unplaced"]},
    ):
        with pytest.raises(ValidationError):
            WorkspaceGraphOverviewRequest(folders=folders)
    accepted = WorkspaceGraphOverviewRequest(
        folders={"include": [uuid4() for _ in range(63)] + ["unplaced"]}
    )
    assert len(accepted.folders.include) == 64
