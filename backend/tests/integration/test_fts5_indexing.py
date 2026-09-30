"""Sprint-4 C1: FTS5 document projection and sessionless indexer contracts."""

import warnings
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError

from backend.app.container import DatabaseProvider
from backend.models.context import ContextClaim, ContextPage, ContextPageVersion
from backend.models.search_index import (
    Fts5UnavailableError,
    require_fts5_available,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.read import SearchFilters
from backend.schemas.tree import (
    TreeFolderCreateRequest,
    TreeMoveRequest,
    TreeNodeUpdate,
)
from backend.services.indexing import Fts5Indexer
from backend.services.indexing import fts5 as fts5_module
from backend.services.page_service import PageService, sync_page_projections
from backend.services.retrieval.keyword import KeywordRetriever
from backend.services.tree_service import TreeService, ensure_primary_tree_placement

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(role: str) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _retrieve(session, workspace_id, query, *, actor=None, limit=10):
    return KeywordRetriever().retrieve(
        session=session,
        workspace_id=workspace_id,
        query=query,
        scope="context_page",
        filters=SearchFilters(),
        limit=limit,
        actor=actor,
    )


def test_context_write_projects_contextual_header_and_stripped_body(
    session_factory,
):
    payload = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="fts-context-header",
        title="Quasar Operations",
        body_md=(
            "# Quasar Operations\n\n"
            "The quasar rollout procedure is the routing summary.\n\n"
            "Second paragraph."
        ),
        aliases=["QSR Runbook"],
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )

    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT context_header, body, visibility_scope "
                "FROM search_doc_fts "
                "WHERE source_kind = 'context_page' AND entity_id = :page_id"
            ),
            {"page_id": str(payload.id)},
        ).mappings().one()

    assert "Title: Quasar Operations" in row["context_header"]
    assert "Aliases: QSR Runbook" in row["context_header"]
    assert "Path: /context/fts-context-header.md" in row["context_header"]
    assert "Summary: The quasar rollout procedure" in row["context_header"]
    assert "Heading: Quasar Operations" in row["context_header"]
    assert row["body"].startswith("# Quasar Operations")
    assert not row["body"].lstrip().startswith("---")
    assert row["visibility_scope"] == "published"


def test_projection_write_through_rolls_back_with_page_transaction(session_factory):
    page_id = uuid4()
    with pytest.raises(RuntimeError, match="force rollback"):
        with session_factory.begin() as session:
            workspace_id = _workspace_id(session)
            page = ContextPage(
                id=page_id,
                workspace_id=workspace_id,
                slug="fts-rollback",
                title="FTS Rollback",
                page_type="concept",
            )
            session.add(page)
            session.flush()
            version = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                body_md="# FTS Rollback\n\nrollback-only-marker",
                content_hash="fts-rollback-hash",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="fts-test",
                version_status="published_snapshot",
            )
            session.add(version)
            session.flush()
            page.current_version_id = version.id
            page.current_published_version_id = version.id
            sync_page_projections(session, page=page, version=version)
            raise RuntimeError("force rollback")

    with session_factory() as session:
        assert session.get(ContextPage, page_id) is None
        projected = session.scalar(
            text(
                "SELECT count(*) FROM search_doc_fts "
                "WHERE entity_id = :page_id"
            ),
            {"page_id": str(page_id)},
        )
    assert projected == 0


def test_bm25_prefers_header_and_pushes_limit_into_fts_sql(session_factory):
    service = PageService(session_factory)
    header_hit = service.write_page(
        workspace_slug="default",
        slug="header-weighted",
        title="Quasar Handbook",
        body_md="# Handbook\n\nRoutine deployment notes.",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    service.write_page(
        workspace_slug="default",
        slug="body-weighted",
        title="Deployment Notes",
        body_md="# Notes\n\nThe quasar procedure is documented here.",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if "search_doc_fts" in statement:
            statements.append(statement)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with session_factory() as session:
            workspace_id = _workspace_id(session)
            results = _retrieve(
                session,
                workspace_id,
                "quasar",
                actor=_actor("viewer"),
                limit=1,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert results[0].id == header_hit.id
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert selects
    assert all("LIMIT" in statement.upper() for statement in selects)


def test_fts5_rebuild_is_idempotent_and_rebuilds_page_name_trigrams(
    session_factory,
):
    payload = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="rebuildable-name",
        title="Rebuildable Quasar",
        body_md="# Rebuildable Quasar\n\nrebuild-body-marker",
        aliases=["RQ Manual"],
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    provider = DatabaseProvider(None, session_factory=session_factory)
    indexer = Fts5Indexer(provider)

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.execute(
            text("DELETE FROM search_doc_fts WHERE workspace_id = :workspace_id"),
            {"workspace_id": str(workspace_id)},
        )
        session.execute(
            text("DELETE FROM page_name_trgm WHERE workspace_id = :workspace_id"),
            {"workspace_id": str(workspace_id)},
        )

    first = indexer.rebuild(workspace_id=workspace_id)
    second = indexer.rebuild(workspace_id=workspace_id)

    with session_factory() as session:
        document_count = session.scalar(
            text(
                "SELECT count(*) FROM search_doc_fts "
                "WHERE source_kind = 'context_page' AND entity_id = :page_id"
            ),
            {"page_id": str(payload.id)},
        )
        name_rows = session.execute(
            text(
                "SELECT name_kind, name FROM page_name_trgm "
                "WHERE page_id = :page_id ORDER BY name_kind, name"
            ),
            {"page_id": str(payload.id)},
        ).all()

    assert first == second
    assert document_count == 1
    assert set(name_rows) == {
        ("alias", "RQ Manual"),
        ("slug", "rebuildable-name"),
        ("title", "Rebuildable Quasar"),
    }


def test_tree_moves_and_folder_rename_reindex_descendant_breadcrumbs(
    session_factory,
):
    page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="breadcrumb-target",
        title="Breadcrumb Target",
        body_md="# Breadcrumb Target\n\nOrdinary body text.",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    tree = TreeService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
        page_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == page.id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        assert root is not None and page_node is not None
        old_parent = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id,
                section="context",
                title="AlphaParentMarker",
                slug="alpha-parent-marker",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )
        new_parent = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id,
                section="context",
                title="GammaParentMarker",
                slug="gamma-parent-marker",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )
        nested = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=old_parent.id,
                section="context",
                title="NestedFolderMarker",
                slug="nested-folder-marker",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )

        tree.move_node(
            session=session,
            workspace_id=workspace_id,
            request=TreeMoveRequest(
                node_id=page_node.id,
                new_parent_id=nested.id,
                reason="place page under breadcrumb fixture",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )
        assert page.id in {
            result.id
            for result in _retrieve(
                session, workspace_id, "alphaparentmarker", actor=_actor("viewer")
            )
        }

        tree.update_node(
            session=session,
            workspace_id=workspace_id,
            node_id=nested.id,
            request=TreeNodeUpdate(display_name="BetaFolderMarker"),
            actor_kind="human",
            actor_id="fts-test",
        )
        assert page.id not in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "nestedfoldermarker",
                actor=_actor("viewer"),
            )
        }
        assert page.id in {
            result.id
            for result in _retrieve(
                session, workspace_id, "betafoldermarker", actor=_actor("viewer")
            )
        }

        tree.move_node(
            session=session,
            workspace_id=workspace_id,
            request=TreeMoveRequest(
                node_id=nested.id,
                new_parent_id=new_parent.id,
                reason="move subtree to a new breadcrumb parent",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )
        assert page.id not in {
            result.id
            for result in _retrieve(
                session, workspace_id, "alphaparentmarker", actor=_actor("viewer")
            )
        }
        assert page.id in {
            result.id
            for result in _retrieve(
                session, workspace_id, "gammaparentmarker", actor=_actor("viewer")
            )
        }


def test_page_ancestor_rename_and_placement_sync_descendant_breadcrumbs(
    session_factory,
):
    service = PageService(session_factory)
    ancestor = service.write_page(
        workspace_slug="default",
        slug="page-ancestor",
        title="PageAncestorMarker",
        body_md="# Page ancestor\n\nAncestor body.",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    child = service.write_page(
        workspace_slug="default",
        slug="page-ancestor-child",
        title="Page Ancestor Child",
        body_md="# Child\n\nOrdinary body text.",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    tree = TreeService()

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        ancestor_page = session.get(ContextPage, ancestor.id)
        ancestor_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == ancestor.id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        child_node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == child.id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
        assert ancestor_page is not None
        assert ancestor_node is not None and child_node is not None and root is not None

        tree.move_node(
            session=session,
            workspace_id=workspace_id,
            request=TreeMoveRequest(
                node_id=child_node.id,
                new_parent_id=ancestor_node.id,
                reason="nest a page below another page",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )
        assert child.id in {
            result.id
            for result in _retrieve(
                session, workspace_id, "pageancestormarker", actor=_actor("viewer")
            )
        }

        tree.update_node(
            session=session,
            workspace_id=workspace_id,
            node_id=ancestor_node.id,
            request=TreeNodeUpdate(display_name="RenamedPageAncestorMarker"),
            actor_kind="human",
            actor_id="fts-test",
        )
        assert child.id not in {
            result.id
            for result in _retrieve(
                session, workspace_id, "pageancestormarker", actor=_actor("viewer")
            )
        }
        assert child.id in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "renamedpageancestormarker",
                actor=_actor("viewer"),
            )
        }

        destination = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id,
                section="context",
                title="PlacementDestinationMarker",
                slug="placement-destination-marker",
            ),
            actor_kind="human",
            actor_id="fts-test",
        )
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=ancestor_page.id,
            title=ancestor_page.title,
            slug=ancestor_page.slug,
            section="context",
            parent_id=destination.id,
            default_parent_slug="context",
            actor_kind="human",
            actor_id="fts-test",
            can_have_children=True,
            move_existing=True,
        )
        assert child.id in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "renamedpageancestormarker",
                actor=_actor("viewer"),
            )
        }
        assert child.id in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "placementdestinationmarker",
                actor=_actor("viewer"),
            )
        }

        ancestor_page.title = "EnsuredPageAncestorMarker"
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=ancestor_page.id,
            title=ancestor_page.title,
            slug=ancestor_page.slug,
            section="context",
            parent_id=destination.id,
            default_parent_slug="context",
            actor_kind="human",
            actor_id="fts-test",
            can_have_children=True,
            move_existing=True,
        )
        assert child.id not in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "renamedpageancestormarker",
                actor=_actor("viewer"),
            )
        }
        assert child.id in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "ensuredpageancestormarker",
                actor=_actor("viewer"),
            )
        }
        assert child.id in {
            result.id
            for result in _retrieve(
                session,
                workspace_id,
                "placementdestinationmarker",
                actor=_actor("viewer"),
            )
        }


def test_fts5_unavailable_error_is_actionable():
    class MissingFtsConnection:
        dialect = SimpleNamespace(name="sqlite")
        info: dict[str, object] = {}

        @staticmethod
        def exec_driver_sql(statement):
            raise OperationalError(statement, {}, RuntimeError("no such module: fts5"))

    with pytest.raises(Fts5UnavailableError, match="ENABLE_FTS5"):
        require_fts5_available(MissingFtsConnection())


def test_non_sqlite_write_through_is_a_warn_once_noop():
    class NonSqliteSession:
        @staticmethod
        def connection():
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    fts5_module._warn_non_sqlite_noop.cache_clear()
    session = NonSqliteSession()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = fts5_module.sync_context_page_doc_sources(session, object())
        second = fts5_module.sync_context_page_doc_sources(session, object())

    assert first == second == 0
    assert len(caught) == 1
    assert "search-plane Indexer" in str(caught[0].message)


def test_delete_projection_is_safe_after_source_of_truth_was_deleted(
    session_factory,
):
    provider = DatabaseProvider(None, session_factory=session_factory)
    indexer = Fts5Indexer(provider)
    context_page_id = uuid4()
    claim_id = uuid4()
    library_item_id = uuid4()
    artifact_id = uuid4()

    valid_page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="valid-claim-neighbor",
        title="Valid Claim Neighbor",
        body_md="# Valid Claim Neighbor\n\nValid body.",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        persisted_valid_page = session.get(ContextPage, valid_page.id)
        valid_version = session.get(
            ContextPageVersion,
            persisted_valid_page.current_published_version_id,
        )
        valid_claim = ContextClaim(
            workspace_id=workspace_id,
            page_version_id=valid_version.id,
            claim_text="valid neighboring claim",
            claim_type="fact",
        )
        session.add(valid_claim)
        session.flush()
        fts5_module.sync_context_page_doc_sources(session, persisted_valid_page)
        session.execute(
            text(
                "INSERT INTO search_doc_fts "
                "(context_header, body, workspace_id, source_kind, entity_id, "
                "version_id, visibility_scope, category) VALUES "
                "(:header, :body, :workspace_id, :source_kind, :entity_id, "
                ":version_id, 'published', :category)"
            ),
            [
                {
                    "header": "orphan page",
                    "body": "orphan page",
                    "workspace_id": str(workspace_id),
                    "source_kind": "context_page",
                    "entity_id": str(context_page_id),
                    "version_id": str(uuid4()),
                    "category": "concept",
                },
                {
                    "header": "orphan claim",
                    "body": "orphan claim",
                    "workspace_id": str(workspace_id),
                    "source_kind": "context_claim",
                    "entity_id": str(claim_id),
                    "version_id": str(uuid4()),
                    "category": "fact",
                },
                {
                    "header": "orphan item",
                    "body": "orphan item",
                    "workspace_id": str(workspace_id),
                    "source_kind": "library_item",
                    "entity_id": str(library_item_id),
                    "version_id": str(uuid4()),
                    "category": "manual",
                },
                {
                    "header": "orphan artifact",
                    "body": "orphan artifact",
                    "workspace_id": str(workspace_id),
                    "source_kind": "library_artifact",
                    "entity_id": str(artifact_id),
                    "version_id": str(uuid4()),
                    "category": "text",
                },
            ],
        )
        session.execute(
            text(
                "INSERT INTO page_name_trgm "
                "(name, workspace_id, page_id, source_version_id, "
                "visibility_scope, name_kind, name_norm) VALUES "
                "('orphan-page', :workspace_id, :page_id, :version_id, "
                "'published', 'slug', 'orphan-page')"
            ),
            {
                "workspace_id": str(workspace_id),
                "page_id": str(context_page_id),
                "version_id": str(uuid4()),
            },
        )

        indexer.delete_in_session(
            session,
            workspace_id=workspace_id,
            source_kind="context_page",
            source_id=context_page_id,
        )
        indexer.delete_in_session(
            session,
            workspace_id=workspace_id,
            source_kind="library_item",
            source_id=library_item_id,
        )

        assert session.scalar(
            text(
                "SELECT count(*) FROM search_doc_fts "
                "WHERE workspace_id = :workspace_id AND entity_id IN "
                "(:page_id, :claim_id, :item_id, :artifact_id)"
            ),
            {
                "workspace_id": str(workspace_id),
                "page_id": str(context_page_id),
                "claim_id": str(claim_id),
                "item_id": str(library_item_id),
                "artifact_id": str(artifact_id),
            },
        ) == 0
        assert session.scalar(
            text(
                "SELECT count(*) FROM page_name_trgm "
                "WHERE workspace_id = :workspace_id AND page_id = :page_id"
            ),
            {"workspace_id": str(workspace_id), "page_id": str(context_page_id)},
        ) == 0
        assert session.scalar(
            text(
                "SELECT count(*) FROM search_doc_fts "
                "WHERE workspace_id = :workspace_id "
                "AND source_kind = 'context_claim' AND entity_id = :claim_id"
            ),
            {"workspace_id": str(workspace_id), "claim_id": str(valid_claim.id)},
        ) == 1


def test_hydration_rejects_archived_page_even_when_fts_row_is_stale(
    session_factory,
):
    page = PageService(session_factory).write_page(
        workspace_slug="default",
        slug="stale-archive-row",
        title="Stale Archive Row",
        body_md="# Stale Archive Row\n\narchivedhydrationmarker",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        persisted = session.get(ContextPage, page.id)
        persisted.lifecycle_status = "archived"
        session.flush()

        results = _retrieve(
            session,
            workspace_id,
            "archivedhydrationmarker",
            actor=_actor("viewer"),
        )

    assert all(result.id != page.id for result in results)


def test_overfetch_hydrates_only_new_or_repeated_entities_per_batch(
    session_factory,
    monkeypatch,
):
    service = PageService(session_factory)
    hidden_ids = []
    for index in range(33):
        hidden_ids.append(
            service.write_page(
                workspace_slug="default",
                slug=f"hydration-row-{index:02d}",
                title=f"Hydration Row {index:02d}",
                body_md="# Hydration Row\n\nincrementalhydrationmarker",
                actor_kind="human",
                actor_id="fts-test",
                actor_provenance="browser_session",
            ).id
        )
    visible = service.write_page(
        workspace_slug="default",
        slug="hydration-row-99",
        title="Hydration Row 99",
        body_md="# Hydration Row\n\nincrementalhydrationmarker",
        actor_kind="human",
        actor_id="fts-test",
        actor_provenance="browser_session",
    )
    retriever = KeywordRetriever()
    batch_sizes: list[int] = []
    original = retriever._hydrate_hit_pairs

    def record_batch(**kwargs):
        batch_sizes.append(len(kwargs["hits"]))
        return original(**kwargs)

    monkeypatch.setattr(retriever, "_hydrate_hit_pairs", record_batch)
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.execute(
            update(ContextPage)
            .where(ContextPage.id.in_(hidden_ids))
            .values(lifecycle_status="archived")
        )
        results = retriever.retrieve(
            session=session,
            workspace_id=workspace_id,
            query="incrementalhydrationmarker",
            scope="context_page",
            filters=SearchFilters(),
            limit=1,
            actor=_actor("viewer"),
        )

    assert [result.id for result in results] == [visible.id]
    assert len(batch_sizes) >= 2
    assert batch_sizes[0] == 32
    assert max(batch_sizes) <= 32
