from uuid import uuid4

import pytest

from backend.services.entity_ref import (
    EntityKind,
    EntityRef,
    coerce_entity_kind,
    context_resource_uri,
    parse_context_resource_uri,
    ranked_result_entity_ref,
)


def test_entity_ref_uses_validated_kind_and_serializes_string_value():
    entity_id = uuid4()
    ref = EntityRef(kind=EntityKind.FEEDBACK, id=entity_id)

    assert isinstance(ref.kind, EntityKind)
    assert ref.source_ref() == {"kind": "feedback", "id": str(entity_id)}
    assert coerce_entity_kind("change_set") is EntityKind.CHANGE_SET


def test_ranked_result_resolves_content_chunk_to_document_kind():
    document_id = uuid4()
    chunk_id = uuid4()

    ref = ranked_result_entity_ref(
        {
            "id": str(chunk_id),
            "type": "context_page",
            "source_ref": {
                "kind": "content_chunk",
                "document_kind": "context_page",
                "document_id": str(document_id),
            },
        }
    )

    assert ref.kind is EntityKind.CONTEXT_PAGE
    assert ref.id == document_id


def test_ranked_result_rejects_unknown_entity_kind():
    with pytest.raises(ValueError):
        ranked_result_entity_ref(
            {
                "id": str(uuid4()),
                "type": "invented_kind",
                "source_ref": {},
            }
        )


def test_context_resource_uri_round_trips_korean_slug_exactly_once():
    workspace_id = uuid4()
    slug = "한국어-페이지"

    uri = context_resource_uri(workspace_id, slug)

    assert uri.endswith("/%ED%95%9C%EA%B5%AD%EC%96%B4-%ED%8E%98%EC%9D%B4%EC%A7%80")
    assert parse_context_resource_uri(uri) == (workspace_id, slug)
