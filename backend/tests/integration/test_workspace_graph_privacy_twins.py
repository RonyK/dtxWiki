"""Exact Workspace Knowledge Map privacy-twin acceptance tests."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest
from sqlalchemy import select

from backend.models.context import ContextCitation, ContextPage
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.tree import WorkspaceTreeNode
from backend.schemas.workspace_graph import WorkspaceGraphOverviewRequest
from backend.services.workspace_graph_service import WorkspaceGraphService
from backend.tests.utils.workspace_graph_corpora import (
    WorkspaceGraphCorpusBuilder,
    add_cross_workspace_decoys,
    add_hidden_draft_decoys,
    build_knowledge_40,
    build_synthetic_workspace,
    corpus_actor,
)
from backend.tests.utils.workspace_graph_fixture_identity import fixture_uuid

pytestmark = pytest.mark.integration


def _service() -> WorkspaceGraphService:
    return WorkspaceGraphService(cursor_secret="workspace-graph-privacy-secret")


def _wire(payload) -> bytes:
    """Serialize the full public scene without omitting defaults or nulls."""

    return payload.model_dump_json().encode("utf-8")


def test_1025_hidden_draft_high_weight_decoys_leave_every_actor_payload_exact(
    session_factory,
):
    request = WorkspaceGraphOverviewRequest(lens="compare")
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="privacy-twin")
        actors = {
            "viewer": corpus_actor(corpus, role="viewer"),
            "editor": corpus_actor(corpus, role="editor"),
            "owner": corpus_actor(corpus, role="owner"),
            "restricted_pat": corpus_actor(
                corpus,
                role="viewer",
                token_kind="api_token",
                scopes=("library:restricted-read",),
            ),
        }
        service = _service()
        before = {
            name: _wire(
                service.query(
                    session,
                    corpus.workspace_id,
                    request=request,
                    actor=actor,
                )
            )
            for name, actor in actors.items()
        }

        hidden_ids = add_hidden_draft_decoys(session, corpus, count=1_025)
        after = {
            name: _wire(
                service.query(
                    session,
                    corpus.workspace_id,
                    request=request,
                    actor=actor,
                )
            )
            for name, actor in actors.items()
        }

    assert len(hidden_ids) == 1_025
    assert after == before
    for payload_bytes in after.values():
        assert b"PRIVACY SENTINEL" not in payload_bytes
        assert all(
            str(hidden_id).encode() not in payload_bytes for hidden_id in hidden_ids
        )


def test_cross_workspace_published_decoys_leave_target_payload_byte_exact(
    session_factory,
):
    request = WorkspaceGraphOverviewRequest(lens="compare")
    with session_factory.begin() as session:
        corpus = build_knowledge_40(
            session,
            corpus_key="cross-workspace-privacy-twin",
        )
        actor = corpus_actor(corpus, role="viewer")
        service = _service()
        before = _wire(
            service.query(
                session,
                corpus.workspace_id,
                request=request,
                actor=actor,
            )
        )
        foreign_workspace_id = add_cross_workspace_decoys(
            session,
            target_corpus=corpus,
            count=1_025,
        )
        after = _wire(
            service.query(
                session,
                corpus.workspace_id,
                request=request,
                actor=actor,
            )
        )

    assert foreign_workspace_id != corpus.workspace_id
    assert after == before
    assert b"FOREIGN PRIVACY SENTINEL" not in after
    assert str(foreign_workspace_id).encode() not in after


def test_restricted_library_visibility_forms_exact_role_and_scope_cohorts(
    session_factory,
):
    request = WorkspaceGraphOverviewRequest(lens="compare")
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="restricted-role-cohorts")
        service = _service()
        viewer = service.query(
            session,
            corpus.workspace_id,
            request=request,
            actor=corpus_actor(corpus, role="viewer"),
        )
        editor = service.query(
            session,
            corpus.workspace_id,
            request=request,
            actor=corpus_actor(corpus, role="editor"),
        )
        owner = service.query(
            session,
            corpus.workspace_id,
            request=request,
            actor=corpus_actor(corpus, role="owner"),
        )
        restricted_pat = service.query(
            session,
            corpus.workspace_id,
            request=request,
            actor=corpus_actor(
                corpus,
                role="viewer",
                token_kind="api_token",
                scopes=("library:restricted-read",),
            ),
        )

    restricted_id = corpus.restricted_library_ids[0]

    def visible_entity_ids(payload):
        return {node.entity_id for node in payload.nodes if node.grain == "document"}

    assert restricted_id not in visible_entity_ids(viewer)
    assert restricted_id not in visible_entity_ids(editor)
    assert restricted_id in visible_entity_ids(owner)
    assert restricted_id in visible_entity_ids(restricted_pat)

    assert viewer.counts.candidate_documents == 39
    assert viewer.counts.candidate_relations == 47
    assert editor.counts == viewer.counts
    assert owner.counts.candidate_documents == 40
    assert owner.counts.candidate_relations == 48
    assert restricted_pat.counts == owner.counts

    # Cohort equality covers revision, deterministic generated_at, facets,
    # metrics, ordering, warnings, capabilities, and all scene transport IDs.
    assert _wire(editor) == _wire(viewer)
    assert _wire(restricted_pat) == _wire(owner)
    assert viewer.graph_revision != owner.graph_revision


@pytest.mark.parametrize(
    "documents,representation,relations,lens",
    [(2_000, "exact", 5_929, "compare"), (2_001, "aggregated", 6_090, "combined")],
)
# Both projections use the same visibility admission. The small aggregated
# compare case is also covered by test_workspace_graph_service.py.
def test_ui2k_restricted_library_insertions_leave_full_viewer_wire_exact(
    session_factory, documents, representation, relations, lens
):
    request = WorkspaceGraphOverviewRequest(
        lens=lens, budget={"documents": 2_000, "relations": 6_000}
    )
    with session_factory.begin() as session:
        corpus = build_synthetic_workspace(
            session,
            documents=documents,
            profile="synthetic",
            seed=260917,
            workspace_index=0,
            restricted_library_pct=0,
            corpus_key=f"restricted-ui2k-twin-{documents}",
        )
        service = _service()
        viewer = corpus_actor(corpus, role="viewer")
        owner = corpus_actor(corpus, role="owner")
        before_viewer = service.query(
            session, corpus.workspace_id, request=request, actor=viewer
        )
        before_owner = service.query(
            session, corpus.workspace_id, request=request, actor=owner
        )
        assert before_viewer.representation == representation
        assert before_viewer.counts.candidate_documents == documents
        assert before_viewer.counts.candidate_relations == relations
        assert before_owner.counts == before_viewer.counts

        builder = WorkspaceGraphCorpusBuilder(
            session,
            corpus_key=f"{corpus.name}-restricted-additions",
            title="Restricted Library privacy additions",
            existing_corpus=corpus,
        )
        hidden_items = []
        hidden_versions = []
        hidden_placements = []
        hidden_citations = []
        expected_citations = set()
        expected_placements = {}
        for index in range(60):
            title = f"RESTRICTED TWIN SENTINEL {index:04d}"
            item_id, version_id = builder.add_library(
                index,
                title=title,
                uri=f"https://example.test/restricted-twin/{index}",
                restricted=True,
                timestamp_offset=100_000 + index,
            )
            folder_id = corpus.folder_ids[index % len(corpus.folder_ids)]
            placement_id = builder.place(
                index,
                entity_kind="library",
                entity_id=item_id,
                title=title,
                folder_id=folder_id,
            )
            hidden_items.append(item_id)
            hidden_versions.append(version_id)
            hidden_placements.append(placement_id)
            expected_placements[placement_id] = (item_id, folder_id)
            for offset in range(5):
                source_index = (index * 7 + offset) % len(corpus.context_ids)
                relation_index = index * 5 + offset
                builder.add_relation(
                    relation_index,
                    source_context_id=corpus.context_ids[source_index],
                    target_kind="library_item_version",
                    target_id=version_id,
                    edge_kind="citation_source",
                    timestamp_offset=110_000 + relation_index,
                )
                hidden_citations.append(
                    fixture_uuid(builder.corpus_key, "citation", relation_index)
                )
                expected_citations.add(
                    (corpus.context_version_ids[source_index], version_id)
                )
        session.flush()

        # Positive DB controls survive payload aggregation: each new restricted
        # item has its own canonical head, five current published sources and a
        # primary placement inside an existing visible folder, never a decoy
        # workspace or a malformed historical citation.
        assert set(
            session.execute(
                select(LibraryItem.id, LibraryItem.current_version_id).where(
                    LibraryItem.workspace_id == corpus.workspace_id,
                    LibraryItem.id.in_(hidden_items),
                    LibraryItem.trust_level == "restricted",
                    LibraryItem.lifecycle_status == "ready",
                )
            ).all()
        ) == set(zip(hidden_items, hidden_versions, strict=True))
        assert set(
            session.execute(
                select(LibraryItemVersion.library_item_id, LibraryItemVersion.id).where(
                    LibraryItemVersion.workspace_id == corpus.workspace_id,
                    LibraryItemVersion.id.in_(hidden_versions),
                    LibraryItemVersion.processing_status == "ready",
                )
            ).all()
        ) == set(zip(hidden_items, hidden_versions, strict=True))
        placement_rows = session.execute(
            select(
                WorkspaceTreeNode.id,
                WorkspaceTreeNode.target_id,
                WorkspaceTreeNode.parent_id,
            ).where(
                WorkspaceTreeNode.workspace_id == corpus.workspace_id,
                WorkspaceTreeNode.id.in_(hidden_placements),
                WorkspaceTreeNode.node_kind == "library_item_ref",
                WorkspaceTreeNode.target_kind == "library_item",
                WorkspaceTreeNode.primary_placement.is_(True),
                WorkspaceTreeNode.visibility_status == "visible",
            )
        ).all()
        assert {row.id: (row.target_id, row.parent_id) for row in placement_rows} == (
            expected_placements
        )
        assert set(
            session.scalars(
                select(WorkspaceTreeNode.id).where(
                    WorkspaceTreeNode.workspace_id == corpus.workspace_id,
                    WorkspaceTreeNode.id.in_(corpus.folder_ids),
                    WorkspaceTreeNode.node_kind == "folder",
                    WorkspaceTreeNode.visibility_status == "visible",
                    WorkspaceTreeNode.parent_id.is_(None),
                )
            ).all()
        ) == set(corpus.folder_ids)
        citation_rows = session.execute(
            select(ContextCitation.target_id, ContextCitation.library_item_version_id)
            .join(
                ContextPage,
                ContextPage.current_published_version_id == ContextCitation.target_id,
            )
            .where(
                ContextCitation.workspace_id == corpus.workspace_id,
                ContextCitation.id.in_(hidden_citations),
                ContextCitation.target_kind == "context_page_version",
                ContextPage.workspace_id == corpus.workspace_id,
                ContextPage.id.in_(corpus.context_ids),
                ContextPage.lifecycle_status == "published",
            )
        ).all()
        assert len(citation_rows) == 300
        assert set(citation_rows) == expected_citations
        assert Counter(row.library_item_version_id for row in citation_rows) == (
            Counter({version_id: 5 for version_id in hidden_versions})
        )

        after_viewer = service.query(
            session, corpus.workspace_id, request=request, actor=viewer
        )
        after_owner = service.query(
            session, corpus.workspace_id, request=request, actor=owner
        )

    assert after_viewer.representation == representation
    assert _wire(after_viewer) == _wire(before_viewer)
    viewer_wire = _wire(after_viewer)
    assert b"RESTRICTED TWIN SENTINEL" not in viewer_wire
    for entity_id in hidden_items + hidden_versions + hidden_placements:
        assert str(entity_id).encode("utf-8") not in viewer_wire
    assert after_owner.counts.candidate_documents == documents + 60
    assert after_owner.counts.candidate_relations == relations + 300
    assert after_owner.graph_revision != before_owner.graph_revision
    owner_before_relations = {
        facet.value: facet.count for facet in before_owner.facet_counts.relation_kinds
    }
    owner_after_relations = {
        facet.value: facet.count for facet in after_owner.facet_counts.relation_kinds
    }
    assert owner_after_relations["citation_source"] == (
        owner_before_relations["citation_source"] + 300
    )
    owner_before_folders = {
        facet.value: facet.count for facet in before_owner.facet_counts.folders
    }
    owner_after_folders = {
        facet.value: facet.count for facet in after_owner.facet_counts.folders
    }
    for folder_id, count in Counter(
        folder_id for _item_id, folder_id in expected_placements.values()
    ).items():
        assert owner_after_folders[str(folder_id)] == (
            owner_before_folders[str(folder_id)] + count
        )


@pytest.mark.parametrize("invalid_part", ["workspace", "context", "library", "folder"])
def test_existing_corpus_builder_rejects_foreign_workspace_members_and_heads(
    session_factory, invalid_part
):
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="builder-existing-corpus")
        foreign = build_knowledge_40(session, corpus_key="builder-foreign-corpus")
        if invalid_part == "workspace":
            corpus = replace(corpus, workspace_id=fixture_uuid("missing-workspace"))
        elif invalid_part == "context":
            corpus = replace(
                corpus,
                context_ids=foreign.context_ids,
                context_version_ids=foreign.context_version_ids,
            )
        elif invalid_part == "library":
            corpus = replace(
                corpus,
                library_ids=foreign.library_ids,
                library_version_ids=foreign.library_version_ids,
            )
        else:
            corpus = replace(corpus, folder_ids=foreign.folder_ids)
        with pytest.raises(ValueError, match="existing corpus"):
            WorkspaceGraphCorpusBuilder(
                session,
                corpus_key="invalid-corpus-extension",
                title="Invalid corpus extension",
                existing_corpus=corpus,
            )


def test_existing_corpus_builder_rejects_a_stale_published_source_head(session_factory):
    with session_factory.begin() as session:
        corpus = build_knowledge_40(session, corpus_key="builder-stale-source")
        source = session.get(ContextPage, corpus.context_ids[0])
        source.current_published_version_id = None
        session.flush()
        with pytest.raises(ValueError, match="current workspace heads"):
            WorkspaceGraphCorpusBuilder(
                session,
                corpus_key="stale-corpus-extension",
                title="Stale corpus extension",
                existing_corpus=corpus,
            )
