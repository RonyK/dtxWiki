"""Read, tree and projection regressions.

One test per confirmed finding — each fails if its fix is reverted:
citation-edge aggregation, temporal tz/dominance/visibility, graph hydration
of library citation targets, tree revive/move/edge freshness, projection
ancestor closure, archived-page resurrection, queue reconciliation, the
bundle scope/hash contracts and llms.txt hardening.
"""

import importlib.util
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.base import Base
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import (
    ContextCitation,
    ContextPage,
    ContextPageVersion,
    EdgeProjection,
)
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.tree import TreeFolderCreateRequest, TreeMoveRequest
from backend.services.delegated_tasks import produce_folder_summary_tasks
from backend.services.entity_ref import EntityKind
from backend.services.graph_service import GraphService
from backend.services.indexing.edge_registry import (
    rebuild_edge_projection,
    refresh_page_edge_projection,
)
from backend.services.public_page_exposure import grant_public_page_exposure
from backend.services.read_errors import (
    DuplicateSlugError,
    RejectedByPolicyError,
)
from backend.services.temporal_read import (
    publish_timeline,
    resolve_version_as_of,
    what_changed,
)
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import (
    TreeService,
    create_tree_node,
    ensure_primary_tree_placement,
)
from backend.tests.conftest import (
    authenticate_test_client,
    authenticate_test_client_in_workspace,
)

pytestmark = pytest.mark.integration

KST = timezone(timedelta(hours=9))


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(role):
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role=role,
    )


def _seed_library_artifact(session, workspace_id, *, trust_level="normal"):
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="url",
        format_hint="html",
        uri=f"https://example.com/{uuid4()}",
        title="Cited Source",
        metadata_json={},
        trust_level=trust_level,
        lifecycle_status="ready",
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="cite-src-1",
        raw_storage_ref="mem://cite-src-1",
        processing_status="completed",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id

    # session_factory (backend/tests/conftest.py) uses a bare create_engine()
    # with no PRAGMA foreign_keys=ON, so attempt_id/blob_id need not
    # reference real rows -- only the NOT NULL/CHECK-satisfying columns and
    # the selected_conversion_attempt_id link (required by
    # canonical_library_artifact_belongs_to_item / GraphService's
    # _artifact_belongs_to_selected_attempt now that A-1 retired the
    # nullable-attempt "belongs by default" shape) matter for these tests.
    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test",
        output_schema_version="1",
        idempotency_key=f"test:{version.id}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    attempt = LibraryAttempt(
        workspace_id=workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id="test.provider",
        provider_version="1",
        provider_spi_version="1",
        config_hash="test",
        input_sha256="0" * 64,
        output_schema_version="1",
        status="completed",
        chain_index=0,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    artifact = LibraryArtifact(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=workspace_id,
        blob_id=uuid4(),
        blob_epoch=1,
        role="markdown",
        kind="text",
        media_type="text/plain",
        content_sha256="a" * 64,
        byte_size=len("Source text."),
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        index_text="Source text.",
    )
    session.add(artifact)
    session.flush()
    version.selected_conversion_attempt_id = attempt.id
    session.add(version)
    session.flush()
    return item, version, artifact


def _cite_artifact(session, page, artifact):
    assert page.current_published_version_id is not None
    citation = ContextCitation(
        workspace_id=page.workspace_id,
        target_kind="context_page_whole",
        target_id=page.current_published_version_id,
        citation_type="derived_artifact",
        library_artifact_id=artifact.id,
        snippet="Source text.",
    )
    session.add(citation)
    session.flush()
    return citation


def _context_root(session, workspace_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "context",
            WorkspaceTreeNode.slug == "context",
            WorkspaceTreeNode.parent_id.is_(None),
        )
    )


def _primary_node(session, page_id):
    return session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.target_kind == "context_page",
            WorkspaceTreeNode.target_id == page_id,
        )
    )


_TEMPORAL_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def _at(seconds):
    return _TEMPORAL_EPOCH + timedelta(seconds=seconds)


def _pin_version_times(
    session_factory,
    page_id,
    version_id,
    created_at,
    *,
    published_at=None,
):
    # Production history uses wall-clock ChangeSet timestamps; clock steps
    # can reorder or omit events. These tests supply an explicit chronology,
    # not a guarantee of production clock-step safety.
    with session_factory.begin() as session:
        version = session.get(ContextPageVersion, version_id)
        assert version is not None and version.page_id == page_id
        rows = session.execute(
            select(ChangeOperation, ChangeSet)
            .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page_id,
                ChangeOperation.diff_ref == f"context_page_version:{version_id}",
                ChangeOperation.operation.in_(
                    ("create_draft", "create", "update", "publish")
                ),
            )
        ).all()
        assert rows
        if published_at is not None:
            assert published_at > created_at
            assert any(op.operation == "publish" for op, _ in rows)
        version.created_at = created_at
        for operation, change_set in rows:
            instant = (
                published_at
                if operation.operation == "publish" and published_at is not None
                else created_at
            )
            change_set.created_at = instant
            operation.created_at = instant


def _pin_archive_time(session_factory, page_id, archived_at):
    with session_factory.begin() as session:
        operation, change_set = session.execute(
            select(ChangeOperation, ChangeSet)
            .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page_id,
                ChangeOperation.operation == "archive",
            )
        ).one()
        change_set.created_at = archived_at
        operation.created_at = archived_at


def _pin_update_time(session_factory, page_id, version_id, updated_at):
    # A metadata-only save records its update op against the already-published
    # head, so it shares that version reference with the original publication.
    # Select the update op by kind (exactly one) and pin it alone: the
    # version-wide helper above would also move the publication.
    with session_factory.begin() as session:
        operation, change_set = session.execute(
            select(ChangeOperation, ChangeSet)
            .join(ChangeSet, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page_id,
                ChangeOperation.diff_ref == f"context_page_version:{version_id}",
                ChangeOperation.operation == "update",
            )
        ).one()
        change_set.created_at = updated_at
        operation.created_at = updated_at


# --- CRITICAL-1: duplicate citations must aggregate, not 500 the write path --


def test_duplicate_citations_aggregate_into_one_weighted_edge(
    session_factory, page_service
):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="cite-dup",
        title="Cite Dup",
        body_md="# Cite Dup\n\nTwo claims, one source.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.get(ContextPage, payload.id)
        _item, _version, artifact = _seed_library_artifact(session, workspace_id)
        _cite_artifact(session, page, artifact)
        _cite_artifact(session, page, artifact)
        # before the aggregation fix this raised IntegrityError on the
        # identity unique — failing the publish/write transaction
        refresh_page_edge_projection(session, workspace_id, page.id)
        edges = session.scalars(
            select(EdgeProjection).where(
                EdgeProjection.edge_kind == "citation_source",
                EdgeProjection.source_id == page.id,
            )
        ).all()
        assert len(edges) == 1
        assert edges[0].weight == 2.0
        # rebuild stays idempotent with the same aggregation
        counts = rebuild_edge_projection(session, workspace_id)
        assert counts["citation_source"] == 1


# --- MAJOR-3: citation targets hydrate as their parent Library item ----------


def test_citation_source_neighbors_name_the_library_item(session_factory, page_service):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="cite-neighbor",
        title="Cite Neighbor",
        body_md="# Cite Neighbor\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.get(ContextPage, payload.id)
        item, _version, artifact = _seed_library_artifact(session, workspace_id)
        _cite_artifact(session, page, artifact)
        refresh_page_edge_projection(session, workspace_id, page.id)
        neighbors = GraphService().neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page.id,
            actor=_actor("viewer"),
            edge_kinds=("citation_source",),
            direction="out",
        )
        assert [n.title for n in neighbors] == ["Cited Source"]
        assert neighbors[0].path == f"/library/{item.id}.md"


def test_restricted_citation_source_hidden_from_viewer(session_factory, page_service):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="cite-restricted",
        title="Cite Restricted",
        body_md="# Cite Restricted\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        page = session.get(ContextPage, payload.id)
        _item, _version, artifact = _seed_library_artifact(
            session, workspace_id, trust_level="restricted"
        )
        _cite_artifact(session, page, artifact)
        refresh_page_edge_projection(session, workspace_id, page.id)
        neighbors = GraphService().neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=page.id,
            actor=_actor("viewer"),
            edge_kinds=("citation_source",),
        )
        assert neighbors == []


# --- MAJOR-4: aware non-UTC inputs convert instead of stripping the offset ---


def test_as_of_and_since_convert_timezone_aware_inputs(session_factory, page_service):
    # Explicit chronology; production clock-step semantics remain unresolved.
    before = _at(10)
    payload = page_service.write_page(
        workspace_slug="default",
        slug="tz-page",
        title="TZ Page",
        body_md="# TZ\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    _pin_version_times(session_factory, payload.id, payload.version_id, _at(20))
    after = _at(30)
    with session_factory() as session:
        page = session.get(ContextPage, payload.id)
        # same instants expressed in +09:00 must resolve identically; the
        # old offset-strip made `before` look 9h later and resolve the page
        assert resolve_version_as_of(session, page, before.astimezone(KST)) is None
        resolved = resolve_version_as_of(session, page, after.astimezone(KST))
        assert resolved is not None
        entries, _ = what_changed(
            session,
            page.workspace_id,
            since=before.astimezone(KST),
            actor=None,
        )
        assert any(entry.page_id == page.id for entry in entries)
        entries_after, _ = what_changed(
            session,
            page.workspace_id,
            since=after.astimezone(KST),
            actor=None,
        )
        assert not any(entry.page_id == page.id for entry in entries_after)


# --- MAJOR-5: restore drafts are not publish events --------------------------


def test_restore_version_does_not_poison_the_publish_timeline(
    session_factory, page_service
):
    v1 = page_service.write_page(
        workspace_slug="default",
        slug="restore-timeline",
        title="Restore Timeline",
        body_md="# RT\n\nVersion one.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    v2 = page_service.write_page(
        workspace_slug="default",
        slug="restore-timeline",
        title="Restore Timeline",
        body_md="# RT\n\nVersion two.",
        actor_kind="human",
        actor_id="tester",
        base_version_id=v1.version_id,
        base_content_hash=v1.content_hash,
        actor_provenance="browser_session",
    )
    # Explicit chronology; production clock-step semantics remain unresolved.
    _pin_version_times(session_factory, v1.id, v1.version_id, _at(10))
    _pin_version_times(session_factory, v2.id, v2.version_id, _at(20))
    since = _at(30)
    restored = page_service.restore_version(
        page_id=v2.id,
        version_id=v1.version_id,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    _pin_version_times(session_factory, restored.id, restored.version_id, _at(40))
    with session_factory() as session:
        page = session.get(ContextPage, v2.id)
        # after the restore, as_of still resolves the published head — the
        # restore draft event must not shadow it into not_found
        resolved = resolve_version_as_of(session, page, _at(50))
        assert resolved is not None
        assert resolved.id == page.current_published_version_id
        # and the restore is not a published change for re-syncing agents
        entries, _ = what_changed(session, page.workspace_id, since=since, actor=None)
        assert not any(entry.page_id == page.id for entry in entries)
        # the timeline itself carries only real publish instants
        timeline = publish_timeline(session, page.id)
        operations = [e.operation for e in timeline]
        assert len(operations) == 2  # create + update, no restore event
        assert [e.version_id for e in timeline] == [v1.version_id, v2.version_id]


# --- MAJOR-2: what_changed never names pages the actor could not read --------


def test_what_changed_hides_archived_never_published_page(
    session_factory,
):
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService

    # Explicit chronology; production clock-step semantics remain unresolved.
    since = _at(0)
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Secret Draft",
                page_type="concept",
                body_markdown="# Secret\n\nNever published.",
                reason="draft only",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
        node = _primary_node(session, created.page_id)
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )
        page_id = created.page_id
    _pin_version_times(session_factory, page_id, created.draft_version_id, _at(10))
    _pin_archive_time(session_factory, page_id, _at(20))

    with session_factory() as session:
        workspace_id = _workspace_id(session)
        viewer_entries, _ = what_changed(
            session, workspace_id, since=since, actor=_actor("viewer")
        )
        editor_entries, _ = what_changed(
            session, workspace_id, since=since, actor=_actor("editor")
        )
    assert not any(entry.page_id == page_id for entry in viewer_entries)
    archived = [e for e in editor_entries if e.page_id == page_id]
    assert [e.change_kind for e in archived] == ["archived"]


# --- MAJOR-6 (C2) / M5 (C1): revive and move keep graph + write path alive ---


def test_page_move_refreshes_tree_edges_without_reindex(session_factory, page_service):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="edge-move",
        title="Edge Move",
        body_md="# EM\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Edge Dest"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="context_page",
            target_id=payload.id,
            title="Edge Move",
            slug="edge-move",
            section="context",
            parent_id=folder.id,
            default_parent_slug="inbox",
            actor_kind="human",
            actor_id="tester",
            move_existing=True,
        )
        parents = GraphService().neighbors(
            session,
            workspace_id,
            kind=EntityKind.CONTEXT_PAGE.value,
            entity_id=payload.id,
            actor=None,
            edge_kinds=("tree",),
            direction="in",
        )
        assert [p.id for p in parents] == [folder.id]


def test_write_draft_revives_page_under_archived_folder(session_factory, page_service):
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService

    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Doomed Folder"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        folder_id = folder.id
    payload = page_service.write_page(
        workspace_slug="default",
        slug="revive-under-archived",
        title="Revive Under Archived",
        body_md="# RUA\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        placement_parent_node_id=folder_id,
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder_id,
            actor_kind="human",
            actor_id="tester",
        )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        # the stale placement pointer must not reject the rewrite — the
        # revive lands under the section default instead (A-3 semantics)
        created = ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Revive Under Archived",
                page_type="concept",
                body_markdown="# RUA\n\nRewritten.",
                reason="rewrite after folder archive",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
        assert created.page_id == payload.id
        node = _primary_node(session, payload.id)
        assert node.visibility_status == "visible"
        assert node.parent_id != folder_id


def test_restore_version_revives_archived_tree_node(session_factory, page_service):
    v1 = page_service.write_page(
        workspace_slug="default",
        slug="restore-revive",
        title="Restore Revive",
        body_md="# RR\n\nVersion one.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    v2 = page_service.write_page(
        workspace_slug="default",
        slug="restore-revive",
        title="Restore Revive",
        body_md="# RR\n\nVersion two.",
        actor_kind="human",
        actor_id="tester",
        base_version_id=v1.version_id,
        base_content_hash=v1.content_hash,
        actor_provenance="browser_session",
    )
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        node = _primary_node(session, v2.id)
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )
    page_service.restore_version(
        page_id=v2.id,
        version_id=v1.version_id,
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        node = _primary_node(session, v2.id)
        # no searchable-but-tree-invisible ghost: the resurrect revived it
        assert node.visibility_status == "visible"


# --- C1 m1/m2/m3: move guards + revive sort keys -----------------------------


def test_move_folder_duplicate_slug_conflicts_cleanly(session_factory):
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        left = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Docs Left"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        docs_a = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Shared Name"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        docs_b = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=left.id, section="context", title="Shared Name"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        with pytest.raises(DuplicateSlugError):
            tree.move_node(
                session=session,
                workspace_id=workspace_id,
                request=TreeMoveRequest(
                    node_id=docs_b.id,
                    new_parent_id=root.id,
                    reason="collide",
                ),
                actor_kind="human",
                actor_id="tester",
            )
        del docs_a


def test_move_under_archived_parent_rejected(session_factory):
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        dead = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Dead Parent"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        mover = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Mover"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=dead.id,
            actor_kind="human",
            actor_id="tester",
        )
        with pytest.raises(RejectedByPolicyError):
            tree.move_node(
                session=session,
                workspace_id=workspace_id,
                request=TreeMoveRequest(
                    node_id=mover.id,
                    new_parent_id=dead.id,
                    reason="into the void",
                ),
                actor_kind="human",
                actor_id="tester",
            )


def test_revive_reallocates_sort_key_after_reorder(session_factory, page_service):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="sortkey-revive",
        title="Sortkey Revive",
        body_md="# SR\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        node = _primary_node(session, payload.id)
        parent_id = node.parent_id
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )
        # a sibling created while archived may take the retired key range
        create_tree_node(
            session,
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            parent_id=parent_id,
            title="Key Squatter",
            slug="key-squatter",
            actor_kind="human",
            actor_id="tester",
        )
    page_service.write_page(
        workspace_slug="default",
        slug="sortkey-revive",
        title="Sortkey Revive",
        body_md="# SR\n\nRewritten.",
        actor_kind="human",
        actor_id="tester",
        base_version_id=payload.version_id,
        base_content_hash=payload.content_hash,
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        node = _primary_node(session, payload.id)
        siblings = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.parent_id == node.parent_id,
                WorkspaceTreeNode.visibility_status == "visible",
            )
        ).all()
        keys = [sibling.sort_key for sibling in siblings]
        assert len(keys) == len(set(keys)), keys


# --- C1 M1/M2/M3: projection closure + include_archived gate -----------------


def test_restricted_artifact_ref_node_invisible_to_viewer(session_factory):
    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, _version, artifact = _seed_library_artifact(
            session, workspace_id, trust_level="restricted"
        )
        item_node = ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="library_item",
            target_id=item.id,
            title=item.title,
            section="library",
            parent_id=None,
            default_parent_slug="library",
            actor_kind="human",
            actor_id="tester",
            can_have_children=True,
        )
        ensure_primary_tree_placement(
            session=session,
            workspace_id=workspace_id,
            target_kind="library_artifact",
            target_id=artifact.id,
            title="Clean Text",
            slug=None,
            section="library",
            parent_id=item_node.id,
            default_parent_slug="library",
            actor_kind="human",
            actor_id="tester",
            can_have_children=False,
        )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        viewer_kinds = {
            (node.target_kind, node.title)
            for node in reader.list_tree_data(
                session=session,
                workspace_id=workspace_id,
                actor=_actor("viewer"),
            ).nodes
        }
    # neither the restricted item nor its artifact ref (which would name it
    # via breadcrumb) may surface
    assert ("library_item", "Cited Source") not in viewer_kinds
    assert ("library_artifact", "Clean Text") not in viewer_kinds


def test_breadcrumbs_never_name_invisible_ancestors(session_factory):
    """Ancestor closure: a visible node under an invisible parent is dropped
    entirely — no orphan whose breadcrumb walks the hidden ancestor."""
    from backend.tests.integration.test_tree_projection import (
        _seed_draft_only_page,
    )

    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        draft_page = _seed_draft_only_page(session, workspace_id)
        draft_node = _primary_node(session, draft_page.id)
        create_tree_node(
            session,
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            parent_id=draft_node.id,
            title="Under Draft",
            slug="under-draft",
            actor_kind="human",
            actor_id="tester",
        )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        viewer_nodes = reader.list_tree_data(
            session=session, workspace_id=workspace_id, actor=_actor("viewer")
        ).nodes
        editor_nodes = reader.list_tree_data(
            session=session, workspace_id=workspace_id, actor=_actor("editor")
        ).nodes
    viewer_titles = {node.title for node in viewer_nodes}
    assert "Under Draft" not in viewer_titles
    assert "A9 Draft Only" not in viewer_titles
    for node in viewer_nodes:
        assert all(entry.title != "A9 Draft Only" for entry in node.breadcrumb)
    assert "Under Draft" in {node.title for node in editor_nodes}


def test_viewer_include_archived_degrades_to_visible_tree(page_service):
    tree = TreeService()
    with page_service.session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Archived Secret"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=folder.id,
            actor_kind="human",
            actor_id="tester",
        )
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
        ),
        page_service=page_service,
    )
    client = TestClient(app)
    authenticate_test_client(client, email="s3-viewer@example.com")
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "s3-viewer@example.com")
        )
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=workspace_id,
            role="viewer",
        )
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree",
        params={"section": "context", "include_archived": "true"},
    )
    assert response.status_code == 200
    nodes = response.json()["data"]["nodes"]
    assert all(node["visibility_status"] == "visible" for node in nodes)
    assert "Archived Secret" not in {node["title"] for node in nodes}


# --- C1 m6: migration dedupes legacy duplicate folder slugs -------------------


def test_migration_dedupe_renames_duplicate_visible_folder_slugs():
    spec = importlib.util.spec_from_file_location(
        "migration_efcbcf724889",
        Path("backend/migrations/versions/v0_1/efcbcf724889_core_schema.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    # The dedupe helper reads and updates only this table.
    Base.metadata.create_all(engine, tables=[WorkspaceTreeNode.__table__])
    workspace_id, parent_id = uuid4(), uuid4()
    from sqlalchemy import text

    def insert(node_slug, sort_key, parent):
        with engine.begin() as connection:
            connection.execute(
                WorkspaceTreeNode.__table__.insert().values(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    section="context",
                    node_kind="folder",
                    parent_id=parent,
                    sort_key=sort_key,
                    title=node_slug,
                    slug=node_slug,
                    is_system_node=False,
                    can_have_children=True,
                    primary_placement=False,
                    visibility_status="visible",
                    created_at=datetime.now(UTC),
                )
            )

    with engine.begin() as connection:
        # legacy shape: the partial unique does not exist yet when the
        # migration's dedupe pass runs
        connection.execute(text("DROP INDEX workspace_tree_node_folder_slug_uniq"))
    # Slug-steal case: two 'dup' plus a PRE-EXISTING 'dup-2' under one parent —
    # a renamed duplicate must not steal the genuine 'dup-2' owner's slug.
    insert("dup", "0001", parent_id)
    insert("dup", "0002", parent_id)
    insert("dup-2", "0003", parent_id)
    # a same-slug node under a different (NULL) parent is a separate group
    insert("dup", "0004", None)

    with engine.begin() as connection:
        module._dedupe_visible_folder_slugs(connection)
        rows = connection.execute(
            select(
                WorkspaceTreeNode.__table__.c.parent_id,
                WorkspaceTreeNode.__table__.c.sort_key,
                WorkspaceTreeNode.__table__.c.slug,
            ).where(WorkspaceTreeNode.__table__.c.node_kind == "folder")
        ).all()
    by_sort = {sort_key: slug for _parent, sort_key, slug in rows}
    # genuine dup-2 keeps its slug; the second dup is renamed off both
    assert by_sort["0003"] == "dup-2"
    assert by_sort["0001"] == "dup"
    assert by_sort["0002"] not in ("dup", "dup-2")
    # NULL-parent group is independent — its 'dup' is untouched
    assert by_sort["0004"] == "dup"
    # every sibling group ends collision-free
    per_parent: dict = {}
    for row_parent, _sort_key, slug in rows:
        assert slug not in per_parent.setdefault(row_parent, set())
        per_parent[row_parent].add(slug)


# --- C3 M-6 / Q-2: queue reconciliation + REST publish hook -------------------


def test_folder_summary_dismissal_expires_when_gap_resolves(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = TreeService().create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Recon Folder"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        for index in range(11):
            create_tree_node(
                session,
                workspace_id=workspace_id,
                section="context",
                node_kind="folder",
                parent_id=folder.id,
                title=f"Recon Child {index}",
                slug=f"recon-child-{index}",
                actor_kind="human",
                actor_id="tester",
            )
        folder_id = folder.id

    def open_task(session):
        return session.scalar(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.target_id == folder_id,
                ClientDelegatedTask.status.in_(("pending", "dismissed")),
            )
        )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        assert produce_folder_summary_tasks(session, workspace_id) == 1
        task = open_task(session)
        task.status = "dismissed"
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        # gap persists -> dismissal keeps suppressing
        assert produce_folder_summary_tasks(session, workspace_id) == 0
        folder = session.get(WorkspaceTreeNode, folder_id)
        folder.nav_summary = "Authored summary."
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        # gap resolved -> reconciliation retires the dismissed row
        assert produce_folder_summary_tasks(session, workspace_id) == 0
        assert open_task(session) is None
        folder = session.get(WorkspaceTreeNode, folder_id)
        folder.nav_summary = None
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        # gap re-appears -> a FRESH task is created (not suppressed forever)
        assert produce_folder_summary_tasks(session, workspace_id) == 1


def test_rest_direct_write_produces_page_meta_refresh(session_factory, page_service):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="rest-q2",
        title="Rest Q2",
        body_md="# Q2\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        tasks = session.scalars(
            select(ClientDelegatedTask).where(
                ClientDelegatedTask.target_id == payload.id
            )
        ).all()
    assert [task.task_type for task in tasks] == ["page_meta_refresh"]
    assert tasks[0].fingerprint == str(payload.version_id)


# --- C3 M-1: viewer bundle hash ignores invisible draft activity --------------


def test_bundle_hash_stable_across_draft_only_edits(session_factory, page_service):
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService
    from backend.services.okf import render_workspace_bundle_markdown

    payload = page_service.write_page(
        workspace_slug="default",
        slug="hash-stable",
        title="Hash Stable",
        body_md="# HS\n\nPublished body.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        _markdown, hash_before, viewer_modified_before = (
            render_workspace_bundle_markdown(
                session, workspace_id, actor=None, workspace_slug="default"
            )
        )
        _markdown, editor_hash_before, editor_modified_before = (
            render_workspace_bundle_markdown(
                session,
                workspace_id,
                actor=_actor("editor"),
                workspace_slug="default",
            )
        )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Hash Stable",
                page_type="concept",
                body_markdown="# HS\n\nInvisible draft edit.",
                reason="draft edit",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        _markdown, hash_after, viewer_modified_after = render_workspace_bundle_markdown(
            session, workspace_id, actor=None, workspace_slug="default"
        )
        _markdown, editor_hash_after, editor_modified_after = (
            render_workspace_bundle_markdown(
                session,
                workspace_id,
                actor=_actor("editor"),
                workspace_slug="default",
            )
        )
    # published-only render: an invisible draft edit must not change the
    # viewer's cache key (nor leak WHEN draft work happened)
    assert hash_after == hash_before
    assert viewer_modified_after == viewer_modified_before
    # the editor's bundle content IS draft-inclusive, so content_hash (the
    # precise cache key) changes; lastModified is deliberately published-only
    # so it pairs consistently with what_changed (which reports published
    # deltas only) and never churns on a draft edit that produced no delta.
    assert editor_hash_after != editor_hash_before
    assert editor_modified_after == editor_modified_before
    del payload


# --- C3 M-7: heuristic fallback rides the tree DTO ----------------------------


def test_tree_payload_carries_heuristic_auto_summary(session_factory, page_service):
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        root = _context_root(session, workspace_id)
        folder = tree.create_folder(
            session=session,
            workspace_id=workspace_id,
            request=TreeFolderCreateRequest(
                parent_id=root.id, section="context", title="Auto Folder"
            ),
            actor_kind="human",
            actor_id="tester",
        )
        folder_id = folder.id
    page_service.write_page(
        workspace_slug="default",
        slug="auto-child",
        title="Auto Child",
        body_md="# AC\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        placement_parent_node_id=folder_id,
        actor_provenance="browser_session",
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        nodes = (
            TreeReadService()
            .list_tree_data(
                session=session, workspace_id=workspace_id, actor=_actor("editor")
            )
            .nodes
        )
    folder_payload = next(node for node in nodes if node.id == folder_id)
    assert folder_payload.nav_summary is None
    assert folder_payload.nav_summary_auto == "includes Auto Child; mostly overview"


# --- C2 MINOR-10: ask-section structure never projects -------------------------


def test_ask_section_nodes_do_not_project_tree_edges(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        ask_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "ask",
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
        node = create_tree_node(
            session,
            workspace_id=workspace_id,
            section="ask",
            node_kind="folder",
            parent_id=ask_root.id,
            title="What is the secret question?",
            slug="ask-day",
            actor_kind="human",
            actor_id="tester",
        )
        rebuild_edge_projection(session, workspace_id)
        ask_edges = session.scalars(
            select(EdgeProjection).where(
                EdgeProjection.edge_kind == "tree",
                EdgeProjection.target_id == node.id,
            )
        ).all()
        assert ask_edges == []


# --- C3 M-5 / llms.txt leak pins ----------------------------------------------


def _llms_app(page_service, **settings_overrides):
    return TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                **settings_overrides,
            ),
            page_service=page_service,
        )
    )


def test_llms_txt_prefers_configured_public_base_url(page_service, session_factory):
    payload = page_service.write_page(
        workspace_slug="default",
        slug="llms-base",
        title="LLMS Base",
        body_md="# LB\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).visibility = "public"
        page = session.get(ContextPage, payload.id)
        grant_public_page_exposure(
            page,
            scope="indexed_web",
            version_id=page.current_published_version_id,
        )
    client = _llms_app(page_service, public_base_url="https://wiki.example.com")
    body = client.get("/llms.txt").text
    # the publicly-cacheable response must not bake the transport host in
    assert "https://wiki.example.com/api/v1/public/w/default/pages/llms-base" in body
    assert "http://testserver" not in body


def test_llms_txt_excludes_draft_only_and_archived_public_pages(
    page_service, session_factory
):
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.context_write_service import ContextWriteService

    published = page_service.write_page(
        workspace_slug="default",
        slug="llms-live",
        title="LLMS Live",
        body_md="# LL\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    doomed = page_service.write_page(
        workspace_slug="default",
        slug="llms-archived",
        title="LLMS Archived",
        body_md="# LA\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        ).visibility = "public"
        ContextWriteService().write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="LLMS Draft Only",
                page_type="concept",
                body_markdown="# LD\n\nDraft body.",
                reason="draft",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
        for page_id in (published.id, doomed.id):
            page = session.get(ContextPage, page_id)
            grant_public_page_exposure(
                page,
                scope="indexed_web",
                version_id=page.current_published_version_id,
            )
        node = _primary_node(session, doomed.id)
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )
    body = _llms_app(page_service).get("/llms.txt").text
    assert "[LL]" in body
    # is_public alone is not enough: draft-only and archived pages would
    # 404 on the public read path and must never be linked
    assert "LLMS Draft Only" not in body
    assert "llms-archived" not in body


def test_llms_txt_uses_published_heading_not_mutable_draft_title(
    page_service, session_factory
):
    published = page_service.write_page(
        workspace_slug="default",
        slug="llms-published-title",
        title="Published title",
        body_md="# Published heading\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = session.get(ContextPage, published.id)
        assert workspace is not None and page is not None
        workspace.visibility = "public"
        grant_public_page_exposure(
            page,
            scope="indexed_web",
            version_id=page.current_published_version_id,
        )
        page.title = "Private draft rename"

    body = _llms_app(page_service).get("/llms.txt").text

    assert "Published heading" in body
    assert "Private draft rename" not in body


# --- C3 M-2: index preview works for agent-section folders ---------------------


def test_index_preview_renders_agent_section_folder(page_service, session_factory):
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        persona = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "agent",
                WorkspaceTreeNode.slug == "persona",
            )
        )
        persona_id = persona.id
    client = _llms_app(page_service)
    authenticate_test_client_in_workspace(
        client,
        session_factory,
        email="agent-preview@example.com",
    )
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{persona_id}/index-preview"
    )
    assert response.status_code == 200, response.text
    # before the section fix the (context, library) projection 404'd here
    assert "(node: /agent/persona)" in response.json()["data"]["markdown"]


# --- A-5 defensive scope: agent lane joins the visibility family ---------------


def _agent_page(session, workspace_id, *, category, title, slug, body, active):
    from backend.models.agent import AgentPage, AgentPageVersion

    page = AgentPage(
        workspace_id=workspace_id,
        category=category,
        title=title,
        slug=slug,
        lifecycle_status="active" if active else "draft",
    )
    session.add(page)
    session.flush()
    version = AgentPageVersion(
        workspace_id=workspace_id,
        agent_page_id=page.id,
        version_no=1,
        body_markdown=body,
        actor_type="human",
        version_status="active_snapshot" if active else "draft",
    )
    session.add(version)
    session.flush()
    if active:
        page.current_active_version_id = version.id
    else:
        page.latest_draft_version_id = version.id
    from backend.services.indexing.fts5 import sync_agent_page_doc_sources

    sync_agent_page_doc_sources(session, page)
    return page


def test_agent_search_lane_gates_drafts_and_sensitive_categories(
    session_factory,
):
    from backend.services.search_service import SearchService

    search_service = SearchService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _agent_page(
            session,
            workspace_id,
            category="skill",
            title="Draft Skill",
            slug="draft-skill",
            body="zebra-draft-needle secret draft body",
            active=False,
        )
        _agent_page(
            session,
            workspace_id,
            category="memory",
            title="Team Memory",
            slug="team-memory",
            body="zebra-memory-needle remembered fact",
            active=True,
        )

        def titles(query, actor):
            results = search_service.search(
                session=session,
                workspace_id=workspace_id,
                query=query,
                scope="agent_page",
                actor=actor,
            )
            return {result.title for result in results.ranked_results}

        # draft body: invisible to viewers/anonymous, visible to editors
        assert titles("zebra-draft-needle", None) == set()
        assert titles("zebra-draft-needle", _actor("viewer")) == set()
        assert titles("zebra-draft-needle", _actor("editor")) == {"Draft Skill"}
        # sensitive category (memory): owner-only, mirroring resources
        assert titles("zebra-memory-needle", _actor("editor")) == set()
        assert titles("zebra-memory-needle", _actor("owner")) == {"Team Memory"}


# --- Review round 2 regressions (post-review fixes) --------------------------


def test_get_node_gates_sensitive_and_draft_targets_for_viewer(session_factory):
    """#1: GET /tree/nodes/{id} passes the actor so a viewer cannot read a
    sensitive agent-page node's target_meta by guessing its id."""
    from backend.services.read_errors import ReadNotFoundError

    reader = TreeReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        guide = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.section == "agent",
                WorkspaceTreeNode.target_kind == "agent_page",
            )
        )
        # the seeded default agent guide page is an agent_guide (sensitive)
        assert guide is not None
        node_id = guide.id
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        # owner sees it; ungated internal call (no actor) still works too
        assert reader.get_node(session, workspace_id, node_id) is not None
        assert (
            reader.get_node(session, workspace_id, node_id, actor=_actor("owner"))
            is not None
        )
        # a viewer is denied: uniform not_found, no target_meta disclosure
        with pytest.raises(ReadNotFoundError):
            reader.get_node(session, workspace_id, node_id, actor=_actor("viewer"))


def test_owner_pat_scope_controls_sensitive_agent_page_discovery(
    session_factory,
    mcp_default_actor,
):
    """#7: an owner PAT needs write scope to discover sensitive agent pages.

    The retired user-less stdio actor no longer exists. The server HTTP path
    supplies a persisted PAT/OAuth human principal, so keep the original
    write-vs-read scope assertion on that reachable identity shape.
    """
    from backend.schemas.read import SearchFilters
    from backend.services.retrieval.keyword import KeywordRetriever

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _agent_page(
            session,
            workspace_id,
            category="memory",
            title="Local Memory",
            slug="local-memory",
            body="zebra-local-needle remembered fact",
            active=True,
        )

    def titles(actor):
        with session_factory() as session:
            workspace_id = _workspace_id(session)
            results = KeywordRetriever().retrieve(
                session=session,
                workspace_id=workspace_id,
                query="zebra-local-needle",
                scope="agent_page",
                filters=SearchFilters(),
                limit=10,
                actor=actor,
            )
            return {r.title for r in results}

    write_client = mcp_default_actor(
        actor_id="sensitive-agent-write-pat",
        scopes=("mcp:write",),
    )
    read_client = mcp_default_actor(
        actor_id="sensitive-agent-read-pat",
        scopes=("mcp:read",),
    )
    assert titles(write_client) == {"Local Memory"}
    # A read-only owner PAT still cannot discover the sensitive page.
    assert titles(read_client) == set()


def test_publish_draft_revives_archived_page_tree_node(session_factory, page_service):
    """#8: approving+publishing a draft whose page was archived by a folder
    cascade must revive the tree node (no searchable-but-invisible ghost)."""
    from backend.schemas.context_write import (
        PublishContextPageRequest,
        ReviewDecisionRequest,
        WriteDraftRequest,
    )
    from backend.services.context_write_service import ContextWriteService
    from backend.services.review_service import ReviewService

    write = ContextWriteService()
    tree = TreeService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        created = write.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=WriteDraftRequest(
                title="Ghost Publish",
                page_type="concept",
                body_markdown="# GP\n\nBody.",
                reason="draft before archive",
            ),
            actor_kind="human",
            actor_id="tester",
            actor_role="owner",
        )
        page_id = created.page_id
        review_id = created.review_item_id
        draft_version_id = created.draft_version_id
    # archive the page's tree node while the draft is pending
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        node = _primary_node(session, page_id)
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="tester",
        )
        # the node exists but is archived (hidden from the tree)
        assert _primary_node(session, page_id).visibility_status == "archived"
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        if review_id is not None:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=review_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id="tester",
                actor_role="owner",
                actor_provenance="browser_session",
            )
        write.publish_draft(
            session=session,
            workspace_id=workspace_id,
            page_id=page_id,
            request=PublishContextPageRequest(
                version_id=draft_version_id, reason="publish ghost"
            ),
            actor_kind="human",
            actor_id="tester",
        )
    with session_factory() as session:
        # the node is revived: the published page is visible in the tree again
        assert _primary_node(session, page_id).visibility_status == "visible"


def test_metadata_only_update_is_not_a_fresh_publish_event(
    session_factory, page_service
):
    """#13: a metadata-only save (body unchanged) of a published page must not
    surface as a fresh publish delta with a misleading previous version."""
    published = page_service.write_page(
        workspace_slug="default",
        slug="meta-only",
        title="Meta Only",
        body_md="# MO\n\nStable body.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    # Explicit chronology; production clock-step semantics remain unresolved.
    # The publication precedes `since`; the update is pinned after it.
    _pin_version_times(session_factory, published.id, published.version_id, _at(10))
    since = _at(20)
    # a body-unchanged update (title differs, body identical) records a
    # page.update op referencing the already-published head
    page_service.update_page(
        page_id=published.id,
        title="Meta Only Renamed",
        body_md="# MO\n\nStable body.",
        actor_kind="human",
        actor_id="tester",
        base_version_id=published.version_id,
        base_content_hash=published.content_hash,
        actor_provenance="browser_session",
    )
    # the update falls inside the window, so only its suppression can keep
    # the page out of the delta
    _pin_update_time(session_factory, published.id, published.version_id, _at(30))
    with session_factory() as session:
        workspace_id = _workspace_id(session)
        entries, _ = what_changed(session, workspace_id, since=since, actor=None)
    # the already-published head version must not re-surface as a publish event
    assert all(entry.page_id != published.id for entry in entries)


def test_reindex_tolerates_duplicate_placement_edges(session_factory, page_service):
    """#15: two visible ref nodes with the same target under one parent must
    not abort rebuild_edge_projection on the identity unique."""
    payload = page_service.write_page(
        workspace_slug="default",
        slug="dup-placement",
        title="Dup Placement",
        body_md="# DP\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        node = _primary_node(session, payload.id)
        parent_id = node.parent_id
        # a second, non-primary ref node for the same page under the same parent
        create_tree_node(
            session,
            workspace_id=workspace_id,
            section="context",
            node_kind="page",
            parent_id=parent_id,
            title="Dup Placement",
            slug="dup-placement-alt",
            target_kind="context_page",
            target_id=payload.id,
            primary_placement=False,
            actor_kind="human",
            actor_id="tester",
        )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        # must not raise IntegrityError on the identity unique
        counts = rebuild_edge_projection(session, workspace_id)
        assert counts["tree"] >= 1


def test_sensitive_agent_page_body_is_owner_gated_on_rest(page_service):
    """Sensitive AgentPage metadata and bodies stay owner-only on REST."""
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    app = create_app(
        settings=Settings(
            database_url="sqlite+pysqlite:///:memory:",
            session_secret="test-session-secret",
        ),
        page_service=page_service,
    )
    owner_client = TestClient(app)
    ws_id = authenticate_test_client_in_workspace(
        owner_client,
        page_service.session_factory,
    )
    guide_id = owner_client.get(
        f"/api/v1/workspaces/{ws_id}/agent/pages?category=agent_guide"
    ).json()["data"][0]["id"]
    persona = owner_client.post(
        f"/api/v1/workspaces/{ws_id}/agent/pages",
        json={
            "category": "persona",
            "title": "Public Persona",
            "slug": "public-persona",
            "body_markdown": "# Public Persona\n\nBody.",
            "reason": "seed persona",
        },
    ).json()["data"]
    persona_id = persona["agent_page_id"]
    activated = owner_client.post(
        f"/api/v1/workspaces/{ws_id}/agent/pages/{persona_id}/activate",
        json={
            "version_id": persona["draft_version_id"],
            "reason": "publish viewer-visible persona",
        },
    )
    assert activated.status_code == 200, activated.text
    # owner reads the sensitive body fine
    assert (
        owner_client.get(
            f"/api/v1/workspaces/{ws_id}/agent/pages/{guide_id}"
        ).status_code
        == 200
    )

    viewer_client = TestClient(app)
    authenticate_test_client(viewer_client, email="s3-body-viewer@example.com")
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "s3-body-viewer@example.com")
        )
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=UUID(ws_id),
            role="viewer",
        )
    # Sensitive titles are private metadata, so the viewer sees only ordinary
    # workspace-visible pages.
    listed = viewer_client.get(f"/api/v1/workspaces/{ws_id}/agent/pages")
    assert listed.status_code == 200
    assert not any(page["category"] == "agent_guide" for page in listed.json()["data"])
    assert any(page["id"] == persona_id for page in listed.json()["data"])
    # but its BODY is owner-only -> 403
    denied = viewer_client.get(f"/api/v1/workspaces/{ws_id}/agent/pages/{guide_id}")
    assert denied.status_code == 403
    # a non-sensitive persona body stays readable by the viewer
    allowed = viewer_client.get(f"/api/v1/workspaces/{ws_id}/agent/pages/{persona_id}")
    assert allowed.status_code == 200
