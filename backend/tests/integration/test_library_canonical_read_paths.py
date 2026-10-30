"""Cross-consumer regression tests for canonical Library projections."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.bundle import AgentInputBundleItem
from backend.models.context import ContextPage, ContextPageVersion, EdgeProjection
from backend.models.delegated_task import ClientDelegatedTask
from backend.models.job import Job
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
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.context_write import CitationInput
from backend.services.actor_visible_read import library_item_visible
from backend.services.bundle_service import AgentInputBundleService
from backend.services.context_write_service import ContextWriteService
from backend.services.coverage_service import coverage_candidate_visible
from backend.services.domain_read import DomainReadService
from backend.services.graph_service import GraphService
from backend.services.library_service import LibraryService
from backend.services.maintenance_rounds import TASK_BUILDERS
from backend.services.mcp_chat_service import McpChatService
from backend.services.read_errors import ReadNotFoundError
from backend.services.tree_projection import TreeProjectionService
from backend.services.tree_service import _revived_library_lifecycle
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _CanonicalFixture:
    item: LibraryItem
    ready_version: LibraryItemVersion
    pending_version: LibraryItemVersion
    selected_artifact: LibraryArtifact
    unselected_artifact: LibraryArtifact
    empty_attempt: LibraryAttempt


def _workspace_id(session) -> UUID:
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _actor(workspace_id: UUID) -> ActorContext:
    return ActorContext(
        actor_kind="human",
        actor_id=str(uuid4()),
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="viewer",
        role="viewer",
    )


def _seed_library_blob(session, workspace_id: UUID, *, salt: str) -> tuple[UUID, int]:
    """Minimal blob/incarnation plumbing a LibraryArtifact row's FKs require."""

    now = datetime.now(UTC)
    if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace_id,
                state="open",
                delete_generation=0,
                updated_at=now,
            )
        )
        session.flush()
    blob_id = uuid4()
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id, id=blob_id, sha256=salt * 64, next_epoch=2
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=1,
            storage_key=f"w/{workspace_id.hex}/{salt}/{blob_id.hex}",
            state="available",
            publish_generation=1,
            security_generation=0,
            gc_generation=0,
            available_at=now,
        )
    )
    session.flush()
    return blob_id, 1


def _attempt(
    *,
    run: LibraryRun,
    version: LibraryItemVersion,
    chain_index: int,
    converter_id: str,
) -> LibraryAttempt:
    attempt = LibraryAttempt(
        workspace_id=version.workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id=converter_id,
        provider_version="1.0.0",
        provider_spi_version="1.0",
        config_hash="c" * 64,
        input_sha256="a" * 64,
        output_schema_version="1.0",
        status="completed",
        chain_index=chain_index,
        transport_retry_count=0,
        execution_generation=1,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    return attempt


def _artifact(
    session,
    *,
    version: LibraryItemVersion,
    attempt: LibraryAttempt,
    text: str,
    digest_char: str,
) -> LibraryArtifact:
    blob_id, blob_epoch = _seed_library_blob(
        session, version.workspace_id, salt=digest_char
    )
    artifact = LibraryArtifact(
        workspace_id=version.workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=version.workspace_id,
        blob_id=blob_id,
        blob_epoch=blob_epoch,
        role="markdown",
        kind="markdown",
        media_type="text/markdown",
        content_sha256=digest_char * 64,
        byte_size=len(text.encode()),
        ordinal=0,
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        sanitizer_version="test-v1",
        index_text=text,
        location_model={},
        is_complete=True,
    )
    return artifact


def _seed_canonical_fixture(session, workspace_id: UUID) -> _CanonicalFixture:
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="upload",
        format_hint="pdf",
        uri=f"upload://canonical-{uuid4()}.pdf",
        title="Canonical Runbook",
        trust_level="trusted",
        lifecycle_status="ready",
    )
    session.add(item)
    session.flush()
    ready = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="1" * 64,
        raw_storage_ref="inline://ready-source",
        processing_status="ready",
    )
    pending = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=2,
        content_hash="2" * 64,
        raw_storage_ref="inline://pending-source",
        processing_status="converting",
        previous_version_id=ready.id,
    )
    session.add_all([ready, pending])
    session.flush()
    item.current_ready_version_id = ready.id
    item.current_version_id = pending.id

    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=ready.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test-v1",
        output_schema_version="1.0",
        idempotency_key=f"canonical:{ready.id}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    selected = _attempt(
        run=run,
        version=ready,
        chain_index=0,
        converter_id="selected",
    )
    stale = _attempt(
        run=run,
        version=ready,
        chain_index=1,
        converter_id="stale",
    )
    empty = _attempt(
        run=run,
        version=ready,
        chain_index=2,
        converter_id="empty",
    )
    session.add_all([selected, stale, empty])
    session.flush()
    ready.selected_conversion_attempt_id = selected.id
    selected_artifact = _artifact(
        session,
        version=ready,
        attempt=selected,
        text="# Selected\n\nSafe canonical body.",
        digest_char="3",
    )
    unselected_artifact = _artifact(
        session,
        version=ready,
        attempt=stale,
        text="# Stale\n\nMust not surface.",
        digest_char="4",
    )
    session.add_all([selected_artifact, unselected_artifact])
    session.flush()
    return _CanonicalFixture(
        item=item,
        ready_version=ready,
        pending_version=pending,
        selected_artifact=selected_artifact,
        unselected_artifact=unselected_artifact,
        empty_attempt=empty,
    )


def _seed_graph_page(session, workspace_id: UUID) -> ContextPage:
    page = ContextPage(
        workspace_id=workspace_id,
        slug=f"canonical-graph-{uuid4()}",
        title="Canonical Graph Seed",
        page_type="concept",
        lifecycle_status="published",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="# Canonical Graph Seed",
        content_hash="canonical-graph-seed",
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="tester",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    page.current_published_version_id = version.id
    return page


def test_actor_and_bundle_keep_last_ready_and_reject_unselected_attempt(
    session_factory,
):
    service = AgentInputBundleService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = _seed_canonical_fixture(session, workspace_id)
        actor = _actor(workspace_id)

        assert library_item_visible(
            seeded.item,
            actor,
            workspace_id=workspace_id,
            version=seeded.ready_version,
        )
        assert not library_item_visible(
            seeded.item,
            actor,
            workspace_id=workspace_id,
            version=seeded.pending_version,
        )
        candidates = service._library_artifact_ref_candidates(
            session,
            workspace_id,
            [seeded.selected_artifact.id],
            actor,
        )
        assert [candidate.pinned_id for candidate in candidates] == [
            seeded.selected_artifact.id
        ]
        with pytest.raises(ReadNotFoundError):
            service._library_artifact_ref_candidates(
                session,
                workspace_id,
                [seeded.unselected_artifact.id],
                actor,
            )

        selected_item = AgentInputBundleItem(
            bundle_id=uuid4(),
            source_kind="library_artifact_excerpt",
            library_artifact_id=seeded.selected_artifact.id,
            included_reason="test",
        )
        stale_item = AgentInputBundleItem(
            bundle_id=uuid4(),
            source_kind="library_artifact_excerpt",
            library_artifact_id=seeded.unselected_artifact.id,
            included_reason="test",
        )
        assert service._bundle_item_visible(session, selected_item, None, workspace_id)
        assert not service._bundle_item_visible(session, stale_item, None, workspace_id)
        # ``actor=None`` remains the internal full-visibility compatibility
        # path; canonicality still applies, but it must not introduce a new
        # trust-level authorization gate.
        seeded.item.trust_level = "restricted"
        assert service._bundle_item_visible(session, selected_item, None, workspace_id)


def test_domain_detail_and_direct_artifact_read_expose_only_selected_ready_set(
    session_factory,
):
    service = DomainReadService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = _seed_canonical_fixture(session, workspace_id)
        actor = _actor(workspace_id)

        detail = service.get_library_item(
            session,
            workspace_id,
            seeded.item.id,
            actor=actor,
        )
        selected = service.get_library_artifact(
            session,
            workspace_id,
            seeded.selected_artifact.id,
            actor=actor,
        )

        assert [artifact.id for artifact in detail.artifacts] == [
            seeded.selected_artifact.id
        ]
        assert selected.id == seeded.selected_artifact.id
        with pytest.raises(ReadNotFoundError):
            service.get_library_artifact(
                session,
                workspace_id,
                seeded.unselected_artifact.id,
                actor=actor,
            )


def test_rest_detail_and_direct_artifact_route_hide_unselected_attempt(
    page_service,
):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    workspace_id = UUID(
        authenticate_test_client_in_workspace(
            client,
            page_service.session_factory,
            email="canonical-rest-reader@example.com",
            workspace_slug="default",
        )
    )
    with page_service.session_factory.begin() as session:
        seeded = _seed_canonical_fixture(session, workspace_id)
        item_id = seeded.item.id
        selected_artifact_id = seeded.selected_artifact.id
        unselected_artifact_id = seeded.unselected_artifact.id

    detail = client.get(f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}")
    selected = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/artifacts/{selected_artifact_id}"
    )
    unselected = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/artifacts/{unselected_artifact_id}"
    )
    client.close()

    assert detail.status_code == 200, detail.text
    assert [artifact["id"] for artifact in detail.json()["data"]["artifacts"]] == [
        str(selected_artifact_id)
    ]
    assert "Must not surface" not in detail.text
    assert selected.status_code == 200, selected.text
    assert unselected.status_code == 404, unselected.text
    assert unselected.json()["error_code"] == "not_found"


def test_graph_hydrates_only_ready_version_and_selected_attempt(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = _seed_canonical_fixture(session, workspace_id)
        page = _seed_graph_page(session, workspace_id)
        targets = (
            ("library_artifact", seeded.selected_artifact.id),
            ("library_artifact", seeded.unselected_artifact.id),
            ("library_item_version", seeded.ready_version.id),
            ("library_item_version", seeded.pending_version.id),
        )
        for target_kind, target_id in targets:
            session.add(
                EdgeProjection(
                    workspace_id=workspace_id,
                    source_kind="context_page",
                    source_id=page.id,
                    target_kind=target_kind,
                    target_id=target_id,
                    edge_kind="citation_source",
                    provenance="curated",
                    weight=1.0,
                    source_ref={},
                )
            )
        page_id = page.id

    with session_factory() as session:
        neighbors = GraphService().neighbors(
            session,
            workspace_id,
            kind="context_page",
            entity_id=page_id,
            actor=_actor(workspace_id),
            edge_kinds=("citation_source",),
            direction="out",
            limit=10,
        )

    visible = {(entry.kind, entry.id) for entry in neighbors}
    assert ("library_artifact", seeded.selected_artifact.id) in visible
    assert ("library_item_version", seeded.ready_version.id) in visible
    assert ("library_artifact", seeded.unselected_artifact.id) not in visible
    assert ("library_item_version", seeded.pending_version.id) not in visible


def test_tree_projection_and_revival_use_selected_ready_artifacts(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = _seed_canonical_fixture(session, workspace_id)
        selected_node = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="library",
            node_kind="library_artifact_ref",
            sort_key="canonical-selected",
            title="Selected Artifact",
            target_kind="library_artifact",
            target_id=seeded.selected_artifact.id,
            can_have_children=False,
            visibility_status="visible",
        )
        stale_node = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="library",
            node_kind="library_artifact_ref",
            sort_key="canonical-stale",
            title="Stale Artifact",
            target_kind="library_artifact",
            target_id=seeded.unselected_artifact.id,
            can_have_children=False,
            visibility_status="visible",
        )
        session.add_all([selected_node, stale_node])
        session.flush()
        visible_ids = TreeProjectionService._visible_ids(
            session,
            [selected_node, stale_node],
            _actor(workspace_id),
        )
        assert selected_node.id in visible_ids
        assert stale_node.id not in visible_ids
        assert _revived_library_lifecycle(session, seeded.item) == "ready"

        seeded.ready_version.selected_conversion_attempt_id = seeded.empty_attempt.id
        session.flush()
        assert _revived_library_lifecycle(session, seeded.item) == "extract_pending"


def test_maintenance_card_reads_selected_ready_markdown(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = _seed_canonical_fixture(session, workspace_id)
        ready_task = ClientDelegatedTask(
            workspace_id=workspace_id,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=seeded.item.id,
            payload={"library_item_version_id": str(seeded.ready_version.id)},
            priority=10,
            status="pending",
            source="import",
            fingerprint=str(seeded.ready_version.id),
        )
        pending_task = ClientDelegatedTask(
            workspace_id=workspace_id,
            task_type="import_batch_classify",
            target_kind="library_item",
            target_id=seeded.item.id,
            payload={"library_item_version_id": str(seeded.pending_version.id)},
            priority=10,
            status="pending",
            source="import",
            fingerprint=str(seeded.pending_version.id),
        )
        session.add_all([ready_task, pending_task])
        session.flush()

        builder = TASK_BUILDERS["import_batch_classify"]
        card = builder(session, ready_task)
        assert card is not None
        context = json.loads(card.untrusted_context.split("\n", 1)[1])
        assert context["text_excerpt"] == "# Selected\n\nSafe canonical body."
        assert builder(session, pending_task) is None


@pytest.mark.parametrize("latest_status", ("converting", "failed"))
def test_residual_consumers_keep_ready_selected_content(
    session_factory,
    latest_status,
):
    """A failed/in-flight latest source must not replace the last ready view."""

    read_service = DomainReadService()
    chat_service = McpChatService()
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        seeded = _seed_canonical_fixture(session, workspace_id)
        seeded.pending_version.processing_status = latest_status
        actor = _actor(workspace_id)

        selected_candidate = {
            "id": str(seeded.selected_artifact.id),
            "kind": "library_artifact",
        }
        stale_candidate = {
            "id": str(seeded.unselected_artifact.id),
            "kind": "library_artifact",
        }
        pending_candidate = {
            "id": str(seeded.pending_version.id),
            "kind": "library_item_version",
        }
        assert coverage_candidate_visible(
            session, selected_candidate, actor, workspace_id
        )
        assert not coverage_candidate_visible(
            session, stale_candidate, actor, workspace_id
        )
        assert not coverage_candidate_visible(
            session, stale_candidate, None, workspace_id
        )
        assert not coverage_candidate_visible(
            session, pending_candidate, actor, workspace_id
        )

        assert chat_service._artifact_title_and_path(
            session,
            "library_item",
            seeded.item.id,
            actor,
            workspace_id,
        ) == (
            seeded.item.title,
            f"/workspaces/{workspace_id}/library/items/{seeded.item.id}",
        )
        # P5.1: every mcp_chat_artifact_link producer normalizes a
        # LibraryArtifact id to its parent LibraryItem id before writing a
        # library_item-kind link (mcp_trace_capture.py's
        # _library_item_id_for_artifact), so this reader no longer probes
        # LibraryArtifact ids at all -- one is simply not-found here now,
        # canonical or not.
        assert chat_service._artifact_title_and_path(
            session,
            "library_item",
            seeded.selected_artifact.id,
            actor,
            workspace_id,
        ) == (None, None)
        assert chat_service._artifact_title_and_path(
            session,
            "library_item",
            seeded.unselected_artifact.id,
            actor,
            workspace_id,
        ) == (None, None)

        item_target = read_service.fetch_target(
            session,
            workspace_id,
            seeded.item.id,
            "library_item",
            actor=actor,
        )
        assert item_target["text"] == "# Selected\n\nSafe canonical body."
        assert item_target["metadata"]["version_id"] == str(seeded.ready_version.id)
        selected_target = read_service.fetch_target(
            session,
            workspace_id,
            seeded.selected_artifact.id,
            "library_artifact",
            actor=actor,
        )
        assert selected_target["text"] == "# Selected\n\nSafe canonical body."
        with pytest.raises(ReadNotFoundError):
            read_service.fetch_target(
                session,
                workspace_id,
                seeded.unselected_artifact.id,
                "library_artifact",
                actor=actor,
            )

        ContextWriteService._validate_citation_ref(
            session,
            workspace_id,
            CitationInput(
                citation_type="raw_source",
                library_item_version_id=seeded.ready_version.id,
            ),
        )
        ContextWriteService._validate_citation_ref(
            session,
            workspace_id,
            CitationInput(
                citation_type="derived_artifact",
                library_artifact_id=seeded.selected_artifact.id,
            ),
        )
        with pytest.raises(ReadNotFoundError, match="source version not found"):
            ContextWriteService._validate_citation_ref(
                session,
                workspace_id,
                CitationInput(
                    citation_type="raw_source",
                    library_item_version_id=seeded.pending_version.id,
                ),
            )
        with pytest.raises(ReadNotFoundError, match="source artifact not found"):
            ContextWriteService._validate_citation_ref(
                session,
                workspace_id,
                CitationInput(
                    citation_type="derived_artifact",
                    library_artifact_id=seeded.unselected_artifact.id,
                ),
            )


def test_residual_consumers_accept_completed_legacy_projection(session_factory):
    """Downstream read consumers accept a completed source's canonical artifact.

    A-1 retired the shape this test
    used to construct -- a "residual pre-0005" artifact with no owning
    attempt at all (``artifact_type``/``storage_ref``, no attempt reference).
    That shape is now provably impossible: those columns don't exist, and
    ``attempt_id`` is NOT NULL. The real invariant this test protects --
    that coverage/DomainRead/McpChat/citation-validation all resolve a
    completed source's canonical artifact correctly -- still applies to the
    current required shape (a real attempt, here a synthetic
    "library_extraction_service" one exactly as the inline-text extraction
    lane creates), so this seeds that instead of the now-impossible legacy
    row.
    """

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item = LibraryItem(
            workspace_id=workspace_id,
            connector_type="upload",
            format_hint="markdown",
            uri=f"upload://legacy-{uuid4()}.md",
            title="Legacy Canonical Source",
            trust_level="trusted",
            lifecycle_status="ready",
        )
        session.add(item)
        session.flush()
        version = LibraryItemVersion(
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_no=1,
            content_hash="8" * 64,
            raw_storage_ref="inline://legacy-source",
            processing_status="completed",
        )
        session.add(version)
        session.flush()
        item.current_version_id = version.id
        run = LibraryRun(
            workspace_id=workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="completed",
            provider_chain_json=[],
            policy_revision="test-v1",
            output_schema_version="1.0",
            idempotency_key=f"legacy:{version.id}",
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
            provider_id="library_extraction_service",
            provider_version="1",
            provider_spi_version="none",
            config_hash="d" * 64,
            input_sha256=version.content_hash,
            output_schema_version="1.0",
            status="completed",
            chain_index=0,
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        session.add(attempt)
        session.flush()
        blob_id, blob_epoch = _seed_library_blob(session, workspace_id, salt="8")
        artifact = LibraryArtifact(
            workspace_id=workspace_id,
            library_item_version_id=version.id,
            attempt_id=attempt.id,
            stage="conversion",
            blob_workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=blob_epoch,
            role="markdown",
            kind="markdown",
            media_type="text/markdown",
            content_sha256="8" * 64,
            byte_size=len(b"# Legacy\n\nCompleted body."),
            ordinal=0,
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            index_text="# Legacy\n\nCompleted body.",
            location_model={},
            is_complete=True,
        )
        session.add(artifact)
        session.flush()
        version.selected_conversion_attempt_id = attempt.id
        actor = _actor(workspace_id)

        assert coverage_candidate_visible(
            session,
            {"id": str(artifact.id), "kind": "library_artifact"},
            actor,
            workspace_id,
        )
        target = DomainReadService().fetch_target(
            session,
            workspace_id,
            item.id,
            "library_item",
            actor=actor,
        )
        assert target["text"] == "# Legacy\n\nCompleted body."
        assert McpChatService()._artifact_title_and_path(
            session,
            "library_item",
            item.id,
            actor,
            workspace_id,
        ) == (item.title, f"/workspaces/{workspace_id}/library/items/{item.id}")
        # P5.1: producers normalize a LibraryArtifact id to its parent
        # LibraryItem id before writing a library_item-kind link, so this
        # reader no longer probes LibraryArtifact ids -- see the matching
        # comment in test_residual_consumers_keep_ready_selected_content.
        assert McpChatService()._artifact_title_and_path(
            session,
            "library_item",
            artifact.id,
            actor,
            workspace_id,
        ) == (None, None)
        ContextWriteService._validate_citation_ref(
            session,
            workspace_id,
            CitationInput(
                citation_type="derived_artifact",
                library_artifact_id=artifact.id,
            ),
        )


def test_context_citation_validation_is_opaque_across_workspaces(session_factory):
    with session_factory.begin() as session:
        requested_workspace_id = _workspace_id(session)
        foreign_workspace = Workspace(
            slug="foreign-context-citation",
            title="Foreign Context Citation",
            name="Foreign Context Citation",
        )
        session.add(foreign_workspace)
        session.flush()
        foreign = _seed_canonical_fixture(session, foreign_workspace.id)
        session.flush()
        state_before = (
            foreign.item.current_version_id,
            foreign.item.current_ready_version_id,
            foreign.ready_version.processing_status,
            foreign.ready_version.selected_conversion_attempt_id,
        )

        cases = (
            (
                "source version not found",
                "raw_source",
                "library_item_version_id",
                foreign.ready_version.id,
            ),
            (
                "source artifact not found",
                "derived_artifact",
                "library_artifact_id",
                foreign.selected_artifact.id,
            ),
        )
        for message, citation_type, id_field, foreign_id in cases:
            with pytest.raises(ReadNotFoundError) as missing_error:
                ContextWriteService._validate_citation_ref(
                    session,
                    requested_workspace_id,
                    CitationInput(
                        citation_type=citation_type,
                        **{id_field: uuid4()},
                    ),
                )
            with pytest.raises(ReadNotFoundError) as foreign_error:
                ContextWriteService._validate_citation_ref(
                    session,
                    requested_workspace_id,
                    CitationInput(
                        citation_type=citation_type,
                        **{id_field: foreign_id},
                    ),
                )
            assert str(missing_error.value) == message
            assert str(foreign_error.value) == message
            assert str(foreign_workspace.id) not in str(foreign_error.value)

        state_after = (
            foreign.item.current_version_id,
            foreign.item.current_ready_version_id,
            foreign.ready_version.processing_status,
            foreign.ready_version.selected_conversion_attempt_id,
        )

    assert state_after == state_before


def test_raw_text_citation_projects_legacy_inline_source_before_validation(
    session_factory, media_installer_factory
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri=f"https://example.test/inline-citation-{uuid4()}",
            title="Inline citation compatibility",
            format_hint="markdown",
            raw_text="# Inline\n\nSafe legacy evidence.",
        )
        assert registration.library_item.current_ready_version_id is None
        assert registration.version.processing_status == "pending"

        ContextWriteService._validate_citation_ref(
            session,
            workspace_id,
            CitationInput(
                citation_type="raw_source",
                library_item_version_id=registration.version.id,
            ),
            media_installer_factory=media_installer_factory,
        )
        artifacts = session.scalars(
            select(LibraryArtifact).where(
                LibraryArtifact.library_item_version_id == registration.version.id
            )
        ).all()
        extraction_job = session.get(Job, registration.extraction_job.id)

        assert registration.library_item.current_ready_version_id == (
            registration.version.id
        )
        assert registration.version.processing_status == "completed"
        assert extraction_job is not None
        assert extraction_job.status == "completed"
        assert len(artifacts) == 1
        # A-1 unification retired the legacy nullable-attempt artifact shape
        # this used to assert (artifact_type == "extracted_text" with no
        # owning attempt at all -- both provably impossible now: artifact_type
        # doesn't exist and attempt_id is NOT NULL). The equivalent real
        # invariant: extraction still owns its artifact through its own
        # synthetic attempt/provider identity rather than a real converter
        # chain attempt.
        assert artifacts[0].role == "markdown"
        assert artifacts[0].attempt_id is not None
        owning_attempt = session.get(LibraryAttempt, artifacts[0].attempt_id)
        assert owning_attempt is not None
        assert owning_attempt.provider_id == "library_extraction_service"

        ContextWriteService._validate_citation_ref(
            session,
            workspace_id,
            CitationInput(
                citation_type="derived_artifact",
                library_artifact_id=artifacts[0].id,
            ),
        )
