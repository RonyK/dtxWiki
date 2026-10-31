from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.schemas.graph import GraphQueryRequest
from backend.schemas.read import GraphNeighborsPayload
from backend.services.graph_semantics import EDGE_SEMANTICS
from backend.services.indexing.edge_registry import PROVENANCE_BY_KIND


def _request(**overrides):
    payload = {"seeds": [{"kind": "context_page", "id": uuid4()}]}
    payload.update(overrides)
    return GraphQueryRequest.model_validate(payload)


def test_graph_query_serializes_canonical_time_alias_and_defaults():
    request = _request()

    serialized = request.model_dump(mode="json", by_alias=True)

    assert serialized["schema_version"] == "dtxwiki.graph-query.v1"
    assert serialized["time"] == {
        "mode": "current",
        "from": None,
        "to": None,
        "as_of": None,
    }
    assert serialized["budget"] == {"nodes": 50, "edges": 150}


def test_graph_query_rejects_invalid_time_windows_and_fake_as_of():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        _request(time={"mode": "action", "from": now, "to": now - timedelta(1)})
    with pytest.raises(ValidationError):
        _request(time={"mode": "as_of"})
    with pytest.raises(ValidationError):
        _request(time={"mode": "current", "as_of": now})


@pytest.mark.parametrize("field", ["from", "to", "as_of"])
def test_graph_query_rejects_timezone_naive_time_values(field):
    value = datetime(2026, 8, 4, 12, 0)
    mode = "as_of" if field == "as_of" else "action"

    with pytest.raises(ValidationError, match="timezone"):
        _request(time={"mode": mode, field: value})


@pytest.mark.parametrize("field", ["from", "to", "as_of"])
def test_graph_query_normalizes_aware_time_values_to_utc(field):
    mode = "as_of" if field == "as_of" else "action"

    request = _request(
        time={"mode": mode, field: "2026-08-05T09:00:00+09:00"},
    )

    value = getattr(request.time, "from_" if field == "from" else field)
    assert value == datetime(2026, 8, 5, tzinfo=UTC)
    assert value.tzinfo is UTC


def test_graph_query_rejects_unbounded_or_duplicated_filters():
    with pytest.raises(ValidationError):
        _request(budget={"nodes": 251, "edges": 150})
    with pytest.raises(ValidationError):
        _request(layers=["knowledge", "knowledge"])
    with pytest.raises(ValidationError):
        _request(seeds=[])
    with pytest.raises(ValidationError):
        _request(node_kinds=[f"kind-{index}" for index in range(33)])
    with pytest.raises(ValidationError):
        _request(edge_kinds=["x" * 65])
    with pytest.raises(ValidationError):
        _request(action_kinds=[""])


def test_projection_and_transport_share_one_edge_semantics_registry():
    assert set(PROVENANCE_BY_KIND) == set(EDGE_SEMANTICS)
    assert PROVENANCE_BY_KIND == {
        kind: semantic.provenance for kind, semantic in EDGE_SEMANTICS.items()
    }
    assert EDGE_SEMANTICS["ask_citation"].action_kind == "included"
    assert EDGE_SEMANTICS["co_access"].default_visible is True
    assert EDGE_SEMANTICS["co_access"].directed is False


def test_legacy_graph_neighbors_defaults_expose_truthful_boundedness_fields():
    payload = GraphNeighborsPayload()

    assert payload.model_dump() == {
        "backlinks": [],
        "outbound": [],
        "citations": [],
        "co_access": [],
        "mermaid": None,
        "truncated": False,
        "warnings": [],
    }
