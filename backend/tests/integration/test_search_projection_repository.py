"""Workspace boundary regressions for the fixed FTS5 row repository."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.app.container import DatabaseProvider
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.workspace import Workspace
from backend.repositories.search_projection import (
    FtsDocumentHit,
    PageNameTrigramHit,
    WorkspaceFts5ProjectionRepository,
    _plain_text_snippet,
)
from backend.services.indexing.fts5 import Fts5Indexer

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _Document:
    workspace_id: UUID
    source_kind: str
    entity_id: UUID
    version_id: UUID | None
    visibility_scope: str = "published"
    category: str = "concept"
    context_header: str = "Projection repository"
    body: str = "projectionrepositorymarker"


@dataclass(frozen=True, slots=True)
class _PageName:
    workspace_id: UUID
    page_id: UUID
    source_version_id: UUID
    visibility_scope: str = "published"
    name_kind: str = "title"
    name_norm: str = "projection repository"
    name: str = "Projection Repository"


def _default_workspace_id(session) -> UUID:
    workspace_id = session.scalar(
        select(Workspace.id).where(Workspace.slug == "default")
    )
    assert workspace_id is not None
    return workspace_id


def _foreign_workspace(session, *, slug: str) -> Workspace:
    workspace = Workspace(slug=slug, title=slug, name=slug)
    session.add(workspace)
    session.flush()
    return workspace


def test_repository_rejects_foreign_document_and_page_name_before_mutation(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _default_workspace_id(session)
        foreign_workspace = _foreign_workspace(
            session,
            slug="fts-repository-foreign-input",
        )
        repository = WorkspaceFts5ProjectionRepository(session, workspace_id)
        page_id = uuid4()
        version_id = uuid4()
        repository.replace_page_names(
            page_id=page_id,
            rows=[
                _PageName(
                    workspace_id=workspace_id,
                    page_id=page_id,
                    source_version_id=version_id,
                )
            ],
        )

        with pytest.raises(ValueError, match="different workspace"):
            repository.insert_documents(
                [
                    _Document(
                        workspace_id=foreign_workspace.id,
                        source_kind="context_page",
                        entity_id=uuid4(),
                        version_id=uuid4(),
                    )
                ]
            )
        with pytest.raises(ValueError, match="different workspace"):
            repository.replace_page_names(
                page_id=page_id,
                rows=[
                    _PageName(
                        workspace_id=foreign_workspace.id,
                        page_id=page_id,
                        source_version_id=uuid4(),
                    )
                ],
            )

        name_hits = repository.search_page_names(
            match_query='"pro"',
            visibility_scopes=["published"],
            take=10,
        )

    assert [hit.page_id for hit in name_hits] == [page_id]


def test_search_and_idempotent_purge_are_workspace_scoped(session_factory):
    with session_factory.begin() as session:
        workspace_id = _default_workspace_id(session)
        foreign_workspace = _foreign_workspace(
            session,
            slug="fts-repository-purge-foreign",
        )
        repository = WorkspaceFts5ProjectionRepository(session, workspace_id)
        foreign_repository = WorkspaceFts5ProjectionRepository(
            session,
            foreign_workspace.id,
        )
        local_page_id = uuid4()
        foreign_page_id = uuid4()
        repository.insert_documents(
            [
                _Document(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    entity_id=local_page_id,
                    version_id=uuid4(),
                )
            ]
        )
        foreign_repository.insert_documents(
            [
                _Document(
                    workspace_id=foreign_workspace.id,
                    source_kind="context_page",
                    entity_id=foreign_page_id,
                    version_id=uuid4(),
                )
            ]
        )
        repository.replace_page_names(
            page_id=local_page_id,
            rows=[
                _PageName(
                    workspace_id=workspace_id,
                    page_id=local_page_id,
                    source_version_id=uuid4(),
                )
            ],
        )
        foreign_repository.replace_page_names(
            page_id=foreign_page_id,
            rows=[
                _PageName(
                    workspace_id=foreign_workspace.id,
                    page_id=foreign_page_id,
                    source_version_id=uuid4(),
                )
            ],
        )

        local_documents, local_scanned = repository.search_documents(
            match_query="projectionrepositorymarker",
            source_kinds=["context_page"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )
        local_names = repository.search_page_names(
            match_query='"pro"',
            visibility_scopes=["published"],
            take=10,
        )
        repository.purge_all()
        repository.purge_all()
        purged_documents, _ = repository.search_documents(
            match_query="projectionrepositorymarker",
            source_kinds=["context_page"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )
        foreign_documents, foreign_scanned = foreign_repository.search_documents(
            match_query="projectionrepositorymarker",
            source_kinds=["context_page"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )
        foreign_names = foreign_repository.search_page_names(
            match_query='"pro"',
            visibility_scopes=["published"],
            take=10,
        )

    assert local_scanned == 1
    assert len(local_documents) == 1
    assert isinstance(local_documents[0], FtsDocumentHit)
    assert local_documents[0].entity_id == local_page_id
    assert len(local_names) == 1
    assert isinstance(local_names[0], PageNameTrigramHit)
    assert local_names[0].page_id == local_page_id
    assert purged_documents == []
    assert foreign_scanned == 1
    assert [hit.entity_id for hit in foreign_documents] == [foreign_page_id]
    assert [hit.page_id for hit in foreign_names] == [foreign_page_id]


def test_orphan_cleanup_does_not_accept_matching_source_ids_from_other_workspace(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _default_workspace_id(session)
        foreign_workspace = _foreign_workspace(
            session,
            slug="fts-repository-orphan-foreign",
        )
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-orphan-source",
            title="Foreign orphan source",
            page_type="concept",
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=foreign_workspace.id,
            page_id=foreign_page.id,
            version_no=1,
            body_md="# Foreign\n",
            content_hash="foreign-orphan-context",
            actor_type="system",
            created_by_actor_kind="system",
            created_by_actor_id="fts-repository-test",
            version_status="published_snapshot",
        )
        foreign_item = LibraryItem(
            workspace_id=foreign_workspace.id,
            connector_type="manual_upload",
            format_hint="text",
            uri=f"test://{uuid4()}",
            metadata_json={},
        )
        session.add_all([foreign_version, foreign_item])
        session.flush()
        foreign_library_version = LibraryItemVersion(
            workspace_id=foreign_workspace.id,
            library_item_id=foreign_item.id,
            version_no=1,
            content_hash="f" * 64,
            raw_storage_ref="test://foreign-source",
        )
        session.add(foreign_library_version)
        session.flush()
        foreign_artifact = LibraryArtifact(
            workspace_id=foreign_workspace.id,
            library_item_version_id=foreign_library_version.id,
            attempt_id=uuid4(),
            stage="conversion",
            blob_workspace_id=foreign_workspace.id,
            blob_id=uuid4(),
            blob_epoch=1,
            role="search_text",
            kind="text",
            media_type="text/plain",
            content_sha256="a" * 64,
            byte_size=1,
        )
        session.add(foreign_artifact)
        session.flush()

        repository = WorkspaceFts5ProjectionRepository(session, workspace_id)
        foreign_repository = WorkspaceFts5ProjectionRepository(
            session,
            foreign_workspace.id,
        )
        local_claim = _Document(
            workspace_id=workspace_id,
            source_kind="context_claim",
            entity_id=uuid4(),
            version_id=foreign_version.id,
            body="crossworkspaceclaimmarker",
        )
        local_artifact = _Document(
            workspace_id=workspace_id,
            source_kind="library_artifact",
            entity_id=foreign_artifact.id,
            version_id=foreign_library_version.id,
            body="crossworkspaceartifactmarker",
        )
        repository.insert_documents([local_claim, local_artifact])
        foreign_repository.insert_documents(
            [
                _Document(
                    workspace_id=foreign_workspace.id,
                    source_kind="context_claim",
                    entity_id=uuid4(),
                    version_id=foreign_version.id,
                    body="crossworkspaceclaimmarker",
                ),
                _Document(
                    workspace_id=foreign_workspace.id,
                    source_kind="library_artifact",
                    entity_id=foreign_artifact.id,
                    version_id=foreign_library_version.id,
                    body="crossworkspaceartifactmarker",
                ),
            ]
        )

        repository.delete_orphan_claim_documents()
        repository.delete_orphan_artifact_documents()
        local_claim_hits, _ = repository.search_documents(
            match_query="crossworkspaceclaimmarker",
            source_kinds=["context_claim"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )
        local_artifact_hits, _ = repository.search_documents(
            match_query="crossworkspaceartifactmarker",
            source_kinds=["library_artifact"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )
        foreign_claim_hits, _ = foreign_repository.search_documents(
            match_query="crossworkspaceclaimmarker",
            source_kinds=["context_claim"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )
        foreign_artifact_hits, _ = foreign_repository.search_documents(
            match_query="crossworkspaceartifactmarker",
            source_kinds=["library_artifact"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )

    assert local_claim_hits == []
    assert local_artifact_hits == []
    assert len(foreign_claim_hits) == 1
    assert len(foreign_artifact_hits) == 1


def test_indexer_direct_ids_cannot_cross_the_workspace_boundary(session_factory):
    indexer = Fts5Indexer(
        DatabaseProvider(None, session_factory=session_factory),
    )
    marker = "foreignindexerdirectidmarker"

    with session_factory.begin() as session:
        workspace_id = _default_workspace_id(session)
        foreign_workspace = _foreign_workspace(
            session,
            slug="fts-indexer-direct-id-foreign",
        )
        foreign_page = ContextPage(
            workspace_id=foreign_workspace.id,
            slug="foreign-indexer-direct-id",
            title="Foreign Indexer Direct Id",
            page_type="concept",
            lifecycle_status="published",
        )
        session.add(foreign_page)
        session.flush()
        foreign_version = ContextPageVersion(
            workspace_id=foreign_workspace.id,
            page_id=foreign_page.id,
            version_no=1,
            body_md=f"# Foreign\n\n{marker}",
            content_hash="foreign-indexer-direct-id",
            actor_type="system",
            created_by_actor_kind="system",
            created_by_actor_id="fts-repository-test",
            version_status="published_snapshot",
        )
        session.add(foreign_version)
        session.flush()
        foreign_page.current_version_id = foreign_version.id
        foreign_page.current_published_version_id = foreign_version.id
        session.flush()

        indexer.upsert_in_session(
            session,
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=foreign_page.id,
        )
        indexer.upsert_in_session(
            session,
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=uuid4(),
        )
        local_repository = WorkspaceFts5ProjectionRepository(session, workspace_id)
        local_hits, _ = local_repository.search_documents(
            match_query=marker,
            source_kinds=["context_page"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )

        foreign_repository = WorkspaceFts5ProjectionRepository(
            session,
            foreign_workspace.id,
        )
        foreign_repository.insert_documents(
            [
                _Document(
                    workspace_id=foreign_workspace.id,
                    source_kind="context_page",
                    entity_id=foreign_page.id,
                    version_id=foreign_version.id,
                    body=marker,
                )
            ]
        )
        indexer.delete_in_session(
            session,
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=foreign_page.id,
        )
        foreign_hits, _ = foreign_repository.search_documents(
            match_query=marker,
            source_kinds=["context_page"],
            visibility_scopes=["published"],
            allow_sensitive=False,
            sensitive_categories=[],
            take=10,
            offset=0,
        )

    assert local_hits == []
    assert [hit.entity_id for hit in foreign_hits] == [foreign_page.id]


class TestPlainTextSnippet:
    """M-10 (260908 UI audit): a search result snippet showed raw markdown
    syntax verbatim -- FTS5's snippet() function extracts a substring of the
    indexed body as-is. These pin the specific patterns the audit found."""

    def test_strips_heading_marker(self):
        assert _plain_text_snippet(
            "# Retention & Deletion Policy\nWe retain raw event data."
        ) == ("Retention & Deletion Policy\nWe retain raw event data.")

    def test_strips_list_markers(self):
        raw = "- Raw events: 90d\n- Aggregates: 5y\n1. Ordered item"
        assert (
            _plain_text_snippet(raw) == "Raw events: 90d\nAggregates: 5y\nOrdered item"
        )

    def test_resolves_wikilink_to_alias_or_target(self):
        assert (
            _plain_text_snippet("Related: [[billing-overview]]")
            == "Related: billing-overview"
        )
        assert (
            _plain_text_snippet("Related: [[billing-overview|Billing Overview]]")
            == "Related: Billing Overview"
        )

    def test_strips_markdown_link_and_keeps_label(self):
        assert _plain_text_snippet("See the [docs](https://example.com) here.") == (
            "See the docs here."
        )

    def test_strips_emphasis_markers(self):
        assert _plain_text_snippet("This is **bold** and *italic* text.") == (
            "This is bold and italic text."
        )

    def test_strips_inline_code_backticks(self):
        assert (
            _plain_text_snippet("Run `npm install` first.") == "Run npm install first."
        )

    def test_strips_table_separator_row_and_pipes(self):
        raw = "| Stage | Owner |\n| --- | --- |\n| Metering | platform |"
        result = _plain_text_snippet(raw)
        assert "---" not in result
        assert "|" not in result
        assert "Stage" in result and "Owner" in result and "Metering" in result

    def test_leaves_plain_prose_untouched(self):
        assert _plain_text_snippet("Needle context body guidance") == (
            "Needle context body guidance"
        )

    def test_does_not_treat_a_prose_hyphen_as_a_list_marker(self):
        # Only a hyphen at a real line start (still intact before whitespace
        # collapse) is a list marker; mid-sentence hyphens are prose.
        assert _plain_text_snippet("usage - not applicable here") == (
            "usage - not applicable here"
        )
