"""Folders are scene nodes, containment is an ordinary relation, weights stay exact."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.schemas.workspace_graph import (
    WORKSPACE_GRAPH_MAX_SAFE_INTEGER,
    WorkspaceGraphScenePayload,
)

_PARENT = UUID(int=1)
_CHILD = UUID(int=2)


def _contains(source, target, *, primary=True, count=1):
    return {
        "id": f"contains:{source}|{target}",
        "source": source,
        "target": target,
        "relation_kind": "tree",
        "label": "contains",
        "layer": "knowledge",
        "aggregate": count > 1 or source.startswith("cluster:"),
        "relation_count": count,
        "occurrence_weight": count,
        "primary": primary,
    }


def _scene(*, aggregated=False):
    source, target = "document:context:first", "document:library:second"
    parent, child = f"folder:{_PARENT}", f"folder:{_CHILD}"
    folders = [
        {
            "grain": "folder",
            "id": parent,
            "folder_id": _PARENT,
            "label": "Parent",
            "path": "Parent",
        },
        {
            "grain": "folder",
            "id": child,
            "folder_id": _CHILD,
            "label": "Child",
            "path": "Parent / Child",
        },
    ]
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
    # Each document also sits under the other: a containment cycle is valid.
    containment = [
        _contains(parent, child),
        _contains(child, source),
        _contains(source, target),
        _contains(target, source, primary=False),
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
        containment = [_contains(parent, child), _contains(child, source)]
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
        "nodes": [*nodes, *folders],
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
            },
            *containment,
        ],
        "facet_counts": {},
    }


@pytest.mark.parametrize("aggregated", [False, True])
def test_folders_and_containment_roundtrip_outside_relation_counts(aggregated):
    scene = WorkspaceGraphScenePayload(**_scene(aggregated=aggregated))
    decoded = WorkspaceGraphScenePayload.model_validate_json(scene.model_dump_json())
    assert decoded == scene
    assert [node.grain for node in scene.nodes].count("folder") == 2
    assert sum(edge.relation_kind == "tree" for edge in scene.edges) == (
        2 if aggregated else 4
    )


def test_legacy_scene_without_weights_remains_valid():
    values = _scene()
    values["edges"][0].pop("occurrence_weight")
    scene = WorkspaceGraphScenePayload(**values)
    assert scene.edges[0].occurrence_weight is None
    assert scene.edges[1].primary is True


def test_edge_weight_must_cover_every_relation():
    values = _scene()
    values["edges"][0]["relation_count"] = 2
    values["edges"][0]["occurrence_weight"] = 1
    with pytest.raises(ValidationError):
        WorkspaceGraphScenePayload(**values)


@pytest.mark.parametrize("invalid", [0, -1, 1.5, True, "1", 2**53])
def test_edge_weights_are_positive_safe_integers(invalid):
    values = _scene()
    values["edges"][0]["occurrence_weight"] = invalid
    with pytest.raises(ValidationError):
        WorkspaceGraphScenePayload(**values)


def test_occurrence_weight_above_uint32_remains_exact():
    values = _scene()
    weight = 2**32 + 17
    values["edges"][0]["occurrence_weight"] = weight
    assert WorkspaceGraphScenePayload(**values).edges[0].occurrence_weight == weight
    values["edges"][0]["occurrence_weight"] = WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    assert WorkspaceGraphScenePayload(**values).edges[0].occurrence_weight == (
        WORKSPACE_GRAPH_MAX_SAFE_INTEGER
    )
