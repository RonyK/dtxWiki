from __future__ import annotations

import hashlib
from datetime import datetime

from backend.policy.auth import ActorContext
from backend.schemas.workspace_graph import (
    WorkspaceGraphBudget,
    WorkspaceGraphOverviewRequest,
    WorkspaceGraphScenePayload,
)
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    add_hidden_draft_decoys,
    build_folder_mismatch_80,
    build_knowledge_40,
    build_star_250_750,
    build_synthetic_workspace,
    corpus_actor,
)
from backend.tests.utils.workspace_graph_fixture_identity import (
    fixture_time,
    fixture_uuid,
)

_PAYLOAD_SHA256 = {
    "knowledge-40": (
        "36f442b7e594ed8597ced8b90bb3202a1356a788b24beaa05c5160f87ffb8834"
    ),
    "folder-mismatch-80": (
        "cb6e1c000ff976b4a282771f58318bf11f29acc7d7c6ff235e5c2df42d1b64ec"
    ),
    "star-250-750": (
        "818dc77f89ca5b2419e123275a75992e0cd1eae656876b1144bb610ebd7eb6ec"
    ),
    "scale-2000": ("8e483212e8987480a30eb3d8e71371452794cadbc4b656182f1f52b38075cd90"),
    "scale-2000-exact": (
        "6bce2797c1d26bef5e98ee1ca6cdc018a46c64fdb311dd87606c8414dedb453e"
    ),
}


def _service() -> WorkspaceGraphService:
    return WorkspaceGraphService(cursor_secret="workspace-graph-golden-secret")


def _assert_complete_wire_contract(
    payload: WorkspaceGraphScenePayload,
    *,
    corpus_key: str,
    generated_at: datetime,
) -> None:
    # generated_at is fixture-derived and belongs to the wire contract. Keep
    # it in the serialized bytes so every field remains pinned byte-for-byte.
    assert payload.generated_at == generated_at
    # These pins deliberately include the new visible-only folder input and
    # occurrence fields, not a stripped legacy payload. Separate topology and
    # privacy-twin tests assert their semantics in addition to byte stability.
    assert payload.folder_topology is not None
    assert payload.folder_topology.schema_version == "workspace-graph-folders.v1"
    assert all(
        edge.occurrence_weight is not None
        and edge.occurrence_weight >= edge.relation_count
        for edge in payload.edges
    )
    actual = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
    assert actual == _PAYLOAD_SHA256[corpus_key], actual


def test_knowledge_40_payload_is_byte_stable(session_factory) -> None:
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session)
        payload = _service().query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=corpus_actor(corpus, role="owner"),
        )

    _assert_complete_wire_contract(
        payload,
        corpus_key="knowledge-40",
        generated_at=fixture_time(5_048),
    )


def test_folder_mismatch_80_payload_is_byte_stable(session_factory) -> None:
    with session_factory.begin() as session:
        corpus = build_folder_mismatch_80(session)
        payload = _service().query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="directory"),
            actor=corpus_actor(corpus, role="viewer"),
        )

    _assert_complete_wire_contract(
        payload,
        corpus_key="folder-mismatch-80",
        generated_at=fixture_time(1_015),
    )


def test_star_250_750_payload_is_byte_stable(session_factory) -> None:
    with session_factory.begin() as session:
        corpus = build_star_250_750(session)
        payload = _service().query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(lens="links"),
            actor=corpus_actor(corpus, role="owner"),
        )

    _assert_complete_wire_contract(
        payload,
        corpus_key="star-250-750",
        generated_at=fixture_time(5_749),
    )


def test_scale_2000_payload_is_byte_stable(session_factory) -> None:
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=2_000,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key="scale-2000",
        )
        actor_id = fixture_uuid("scale-2000", "actor", "owner")
        actor = ActorContext(
            actor_kind="human",
            actor_id=str(actor_id),
            user_id=actor_id,
            token_kind="session",
            scopes=(),
            workspace_role="owner",
            role="owner",
            workspace_id=corpus.workspace_id,
            provenance="browser_session",
        )
        service = WorkspaceGraphService(cursor_secret="bench-secret")
        payload = service.query(
            session,
            corpus.workspace_id,
            request=WorkspaceGraphOverviewRequest(
                lens="links",
                budget=WorkspaceGraphBudget(
                    documents=250,
                    relations=750,
                ),
            ),
            actor=actor,
        )

        # The same 2,000-document universe used to be aggregate-only because
        # its 5,057 visible relations exceed the default 750-edge budget.
        # Explicit UI-2K budgets must preserve every actor-visible identity.
        exact_request = WorkspaceGraphOverviewRequest(
            lens="links", budget=WorkspaceGraphBudget(documents=2_000, relations=6_000)
        )
        exact = service.query(
            session,
            corpus.workspace_id,
            request=exact_request,
            actor=actor,
        )
        exact_wire = exact.model_dump_json().encode("utf-8")
        hidden_ids = add_hidden_draft_decoys(session, corpus, count=1)
        after_hidden = service.query(
            session,
            corpus.workspace_id,
            request=exact_request,
            actor=actor,
        )

    assert payload.counts.model_dump() == {
        "candidate_documents": 2_000,
        "candidate_relations": 5_057,
        "rendered_documents": 248,
        "rendered_clusters": 2,
        # v1's one-edge result encoded the boundary collapse, not a useful
        # overview. v2 keeps a ranked document prefix and authored edges.
        "rendered_relations": 711,
    }
    _assert_complete_wire_contract(
        payload,
        corpus_key="scale-2000",
        generated_at=fixture_time(10_928),
    )
    assert exact.representation == "exact"
    assert exact.completeness == "complete"
    assert exact.truncated is False
    assert exact.omission_reasons == []
    assert exact.counts.model_dump() == {
        "candidate_documents": 2_000,
        "candidate_relations": 5_057,
        "rendered_documents": 2_000,
        "rendered_clusters": 0,
        "rendered_relations": 5_057,
    }
    assert len(exact.nodes) == 2_000
    assert len(exact.edges) == 5_057
    assert {node.entity_id for node in exact.nodes} == set(corpus.document_ids)
    assert all(edge.aggregate is False for edge in exact.edges)
    assert after_hidden.model_dump_json().encode("utf-8") == exact_wire
    assert str(hidden_ids[0]).encode("utf-8") not in exact_wire
    _assert_complete_wire_contract(
        exact,
        corpus_key="scale-2000-exact",
        generated_at=fixture_time(10_928),
    )
