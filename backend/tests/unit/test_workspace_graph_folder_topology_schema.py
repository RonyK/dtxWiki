"""Folder topology is additive, lossless, and safe for browser arithmetic."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.schemas.workspace_graph import (
    WORKSPACE_GRAPH_MAX_SAFE_INTEGER,
    WorkspaceGraphFolderTopology,
    WorkspaceGraphScenePayload,
)

_PARENT = UUID(int=1)
_CHILD = UUID(int=2)
_OTHER = UUID(int=3)


def _scene(*, aggregated=False):
    source, target = "document:context:first", "document:library:second"
    nodes = [
        {
            "grain": "document",
            "id": source,
            "entity_kind": "context",
            "entity_id": UUID(int=10),
            "label": "Context",
            "path": "/context?target=first",
            "primary_folder_id": _CHILD,
            "primary_folder_label": "Child",
        },
        {
            "grain": "document",
            "id": target,
            "entity_kind": "library",
            "entity_id": UUID(int=11),
            "label": "Library",
            "path": "/library?target=second",
            "primary_folder_id": None,
            "primary_folder_label": "No visible placement",
        },
    ]
    if aggregated:
        source = target = "cluster:other_cluster:mixed"
        nodes = [
            {
                "grain": "cluster",
                "id": source,
                "cluster_kind": "other_cluster",
                "label": "Mixed cluster",
                "member_count": 2,
                "relation_count": 1,
            }
        ]
    return {
        "graph_revision": "wgr1:" + "a" * 64,
        "generated_at": datetime(2026, 10, 4, tzinfo=UTC),
        "algorithm_version": "test.v1",
        "metric_basis": "test.v1",
        "representation": "aggregated" if aggregated else "exact",
        "completeness": "capped" if aggregated else "complete",
        "truncated": aggregated,
        "omission_reasons": ["document_budget"] if aggregated else [],
        "counts": {
            "candidate_documents": 2,
            "candidate_relations": 1,
            "rendered_documents": 0 if aggregated else 2,
            "rendered_clusters": 1 if aggregated else 0,
            "rendered_relations": 1,
        },
        "nodes": nodes,
        "edges": [
            {
                "id": "edge:first",
                "source": source,
                "target": target,
                "relation_kind": "citation_source",
                "layer": "evidence",
                "aggregate": aggregated,
                "relation_count": 1,
                "occurrence_weight": 7,
            }
        ],
        "facet_counts": {},
        "folder_topology": {
            "schema_version": "workspace-graph-folders.v1",
            "folders": [
                {
                    "id": _PARENT,
                    "label": "Parent",
                    "parent_folder_id": None,
                    "direct_member_count": 0,
                },
                {
                    "id": _CHILD,
                    "label": "Child",
                    "parent_folder_id": _PARENT,
                    "direct_member_count": 1,
                },
            ],
            "memberships": [
                {
                    "node_id": source,
                    "folder_id": _CHILD,
                    "entity_kind": "context",
                    "member_count": 1,
                },
                {
                    "node_id": target,
                    "folder_id": None,
                    "entity_kind": "library",
                    "member_count": 1,
                },
            ],
            "edge_slices": [
                {
                    "edge_id": "edge:first",
                    "source_folder_id": _CHILD,
                    "source_entity_kind": "context",
                    "target_folder_id": None,
                    "target_entity_kind": "library",
                    "relation_count": 1,
                    "occurrence_weight": 7,
                }
            ],
        },
    }


@pytest.mark.parametrize("aggregated", [False, True])
def test_topology_roundtrips_exact_and_mixed_cluster_self_edges(aggregated):
    scene = WorkspaceGraphScenePayload(**_scene(aggregated=aggregated))
    decoded = WorkspaceGraphScenePayload.model_validate_json(scene.model_dump_json())
    assert decoded == scene
    assert scene.folder_topology.schema_version == "workspace-graph-folders.v1"
    if aggregated:
        assert scene.edges[0].source == scene.edges[0].target
        assert {row.entity_kind for row in scene.folder_topology.memberships} == {
            "context",
            "library",
        }


def test_legacy_scene_without_topology_or_weights_remains_valid():
    values = _scene()
    values.pop("folder_topology")
    values["edges"][0].pop("occurrence_weight")
    scene = WorkspaceGraphScenePayload(**values)
    assert scene.folder_topology is None
    assert scene.edges[0].occurrence_weight is None


def _invalid_scene(case):
    values = _scene()
    topology = values["folder_topology"]
    folders = topology["folders"]
    memberships = topology["memberships"]
    parts = topology["edge_slices"]
    if case == "duplicate_folder":
        folders.append(deepcopy(folders[0]))
    elif case == "missing_parent":
        folders[1]["parent_folder_id"] = _OTHER
    elif case == "cycle":
        folders[0]["parent_folder_id"] = _CHILD
    elif case == "self_parent":
        folders[0]["parent_folder_id"] = _PARENT
    elif case == "duplicate_membership":
        memberships.append(deepcopy(memberships[0]))
    elif case == "missing_node":
        memberships[0]["node_id"] = "document:context:missing"
    elif case == "missing_folder":
        memberships[0]["folder_id"] = _OTHER
    elif case == "wrong_document_kind":
        memberships[0]["entity_kind"] = "library"
    elif case == "wrong_document_folder":
        memberships[0]["folder_id"] = _PARENT
    elif case == "document_member_count":
        memberships[0]["member_count"] = 2
    elif case == "missing_membership":
        memberships.pop()
    elif case == "wrong_direct_count":
        folders[1]["direct_member_count"] = 2
    elif case == "extraneous_empty_folder":
        folders.append(
            {
                "id": _OTHER,
                "label": "Not in member ancestry",
                "parent_folder_id": None,
                "direct_member_count": 0,
            }
        )
    elif case == "duplicate_slice":
        parts.append(deepcopy(parts[0]))
    elif case == "missing_edge":
        parts[0]["edge_id"] = "edge:missing"
    elif case == "wrong_slice_source_folder":
        parts[0]["source_folder_id"] = _PARENT
    elif case == "wrong_slice_source_kind":
        parts[0]["source_entity_kind"] = "library"
    elif case == "wrong_slice_target_folder":
        parts[0]["target_folder_id"] = _CHILD
    elif case == "wrong_slice_target_kind":
        parts[0]["target_entity_kind"] = "context"
    elif case == "wrong_slice_count":
        parts[0]["relation_count"] = 2
    elif case == "wrong_slice_weight":
        parts[0]["occurrence_weight"] = 6
    elif case == "missing_slice":
        parts.clear()
    elif case == "legacy_weight_with_topology":
        values["edges"][0].pop("occurrence_weight")
    elif case == "slice_weight_below_count":
        parts[0]["relation_count"] = 2
        parts[0]["occurrence_weight"] = 1
    elif case == "edge_weight_below_count":
        values["edges"][0]["relation_count"] = 2
        values["edges"][0]["occurrence_weight"] = 1
    else:
        raise AssertionError(case)
    return values


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_folder",
        "missing_parent",
        "cycle",
        "self_parent",
        "duplicate_membership",
        "missing_node",
        "missing_folder",
        "wrong_document_kind",
        "wrong_document_folder",
        "document_member_count",
        "missing_membership",
        "wrong_direct_count",
        "extraneous_empty_folder",
        "duplicate_slice",
        "missing_edge",
        "wrong_slice_source_folder",
        "wrong_slice_source_kind",
        "wrong_slice_target_folder",
        "wrong_slice_target_kind",
        "wrong_slice_count",
        "wrong_slice_weight",
        "missing_slice",
        "legacy_weight_with_topology",
        "slice_weight_below_count",
        "edge_weight_below_count",
    ],
)
def test_topology_rejects_invalid_cross_references_and_nonconserving_sums(case):
    with pytest.raises(ValidationError):
        WorkspaceGraphScenePayload(**_invalid_scene(case))


def test_cluster_membership_distribution_must_cover_exact_member_count():
    values = _scene(aggregated=True)
    values["folder_topology"]["memberships"][0]["member_count"] = 2
    with pytest.raises(ValidationError, match="conserve every node"):
        WorkspaceGraphScenePayload(**values)


@pytest.mark.parametrize("invalid", [0, -1, 1.5, True, "1", 2**53])
@pytest.mark.parametrize(
    "path",
    [
        ("edges", "occurrence_weight"),
        ("memberships", "member_count"),
        ("edge_slices", "relation_count"),
        ("edge_slices", "occurrence_weight"),
    ],
)
def test_topology_counts_and_weights_are_positive_safe_integers(path, invalid):
    values = _scene()
    section, field = path
    rows = values[section] if section == "edges" else values["folder_topology"][section]
    rows[0][field] = invalid
    with pytest.raises(ValidationError):
        WorkspaceGraphScenePayload(**values)


def test_occurrence_weight_above_uint32_remains_exact():
    values = _scene()
    weight = 2**32 + 17
    values["edges"][0]["occurrence_weight"] = weight
    values["folder_topology"]["edge_slices"][0]["occurrence_weight"] = weight
    scene = WorkspaceGraphScenePayload(**values)
    assert scene.edges[0].occurrence_weight == weight
    assert scene.folder_topology.edge_slices[0].occurrence_weight == weight
    values["edges"][0]["occurrence_weight"] = WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    values["folder_topology"]["edge_slices"][0]["occurrence_weight"] = (
        WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    )
    assert WorkspaceGraphScenePayload(**values).edges[0].occurrence_weight == (
        WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    )


def test_topology_row_budget_is_total_not_per_array(monkeypatch):
    import backend.schemas.workspace_graph as schema

    monkeypatch.setattr(schema, "WORKSPACE_GRAPH_FOLDER_TOPOLOGY_ROW_LIMIT", 4)
    with pytest.raises(ValidationError, match="row budget"):
        WorkspaceGraphFolderTopology(**_scene()["folder_topology"])


@pytest.mark.parametrize("invalid", [-1, 1.5, True, "0", 2**53])
def test_folder_direct_count_is_a_nonnegative_safe_integer(invalid):
    values = _scene()
    values["folder_topology"]["folders"][0]["direct_member_count"] = invalid
    with pytest.raises(ValidationError):
        WorkspaceGraphScenePayload(**values)


def test_empty_scene_has_empty_complete_topology():
    values = _scene()
    values.update(
        counts={
            "candidate_documents": 0,
            "candidate_relations": 0,
            "rendered_documents": 0,
            "rendered_clusters": 0,
            "rendered_relations": 0,
        },
        nodes=[],
        edges=[],
        folder_topology={"folders": [], "memberships": [], "edge_slices": []},
    )
    assert WorkspaceGraphScenePayload(**values).folder_topology.folders == []
