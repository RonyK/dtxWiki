"""Revision bytes stay canonical while digest scratch space is record-sized."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

import backend.services.workspace_graph_service as graph
from backend.tests.utils.workspace_graph_reference import new_document, seeded_universe


def _earlier_bytes(documents, relations):
    """Frozen full-buffer contract, independent of the streaming encoder."""

    def placement(value):
        return asdict(value) if value is not None else None

    def default(value):
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(type(value))

    state = {
        "algorithm": graph.ALGORITHM_VERSION,
        "visibility_policy": graph.VISIBILITY_POLICY_VERSION,
        "documents": [
            {
                "kind": document.entity_kind,
                "id": str(document.key[1]),
                "head_id": str(document.head_id),
                "head_updated_at": document.updated_at,
                "label": document.label,
                "path": document.path,
                "primary_placement": placement(document.primary),
                "secondary_placements": [
                    placement(p) for p in document.secondary_revision
                ],
            }
            for document in sorted(
                documents.values(), key=lambda d: (d.key[0], str(d.key[1]))
            )
        ],
        "relations": [
            {
                "source": f"{relation.source[0]}:{relation.source[1]}",
                "target": f"{relation.target[0]}:{relation.target[1]}",
                "kind": relation.kind,
                "occurred_at": relation.occurred_at,
                "occurrence_weight": relation.occurrence_weight,
            }
            for relation in sorted(
                relations,
                key=lambda r: (
                    r.source[0],
                    str(r.source[1]),
                    r.target[0],
                    str(r.target[1]),
                    r.kind,
                ),
            )
        ],
    }
    return json.dumps(
        state,
        default=default,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.mark.parametrize("seed", [None, 0, 17, 48, "large"])
def test_streamed_hash_input_is_byte_identical_and_record_bounded(monkeypatch, seed):
    if seed == "large":
        documents = {d.key: d for d in map(new_document, range(600))}
        relations = [
            graph._Relation(d.key, d.key, "wikilink", "knowledge", d.updated_at)
            for d in documents.values()
        ]
    else:
        documents, relations = ({}, []) if seed is None else seeded_universe(seed)
    _assert_stream(monkeypatch, documents, relations)


def _assert_stream(monkeypatch, documents, relations):
    expected = _earlier_bytes(documents, relations)
    expected_revision = "wgr1:" + hashlib.sha256(expected).hexdigest()
    chunks = []
    batch_sizes = []
    real_hash = hashlib.sha256
    real_json = graph._canonical_json

    def encode(value):
        if not isinstance(value, str):
            assert isinstance(value, tuple)
            batch_sizes.append(len(value))
            assert len(value) <= 256
        return real_json(value)

    class RecordingHash:
        def __init__(self):
            self.inner = real_hash()

        def update(self, value):
            chunks.append(value)
            self.inner.update(value)

        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(graph.hashlib, "sha256", RecordingHash)
    monkeypatch.setattr(graph, "_canonical_json", encode)
    assert (
        graph.WorkspaceGraphService._graph_revision(documents, relations)
        == expected_revision
    )
    assert b"".join(chunks) == expected
    assert sum(batch_sizes) == len(documents) + len(relations)
    assert max(map(len, chunks)) < len(expected)


def test_unicode_offsets_all_secondary_placements_and_stable_relation_ties(monkeypatch):
    document = new_document(0)
    document.label = '한글 😀 e\u0301 "quote" \\path\n\t\x00'
    document.updated_at = datetime(
        2026, 10, 19, 12, 30, 1, 123456, tzinfo=timezone(timedelta(hours=9))
    )
    folder = graph._VisibleFolder(UUID(int=99), document.label, None)
    placements = tuple(
        graph._Placement(
            tree_node_id=UUID(int=100 + index),
            folder_id=folder.id,
            folder_label=folder.label,
            path=f"/{folder.label}/{index}",
            ancestor_ids=(folder.id,),
            folder_ancestors=(folder,),
        )
        for index in range(35)
    )
    document.primary = placements[0]
    document.secondary = list(placements[1:33])
    document.secondary_revision = placements[1:]
    document.secondary_capped = True
    # Same sort key: time and occurrence weight must not become tie breakers.
    relations = [
        graph._Relation(
            document.key,
            document.key,
            "wikilink",
            "knowledge",
            document.updated_at + timedelta(seconds=offset),
            weight,
        )
        for weight, offset in ((9, 1), (1, 0))
    ]
    _assert_stream(monkeypatch, {document.key: document}, relations)
