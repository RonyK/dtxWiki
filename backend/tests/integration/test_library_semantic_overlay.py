from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.chunk import ContentChunk
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
from backend.models.library_semantic_overlay import LibrarySemanticOverlay
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.library_semantic import LibrarySemanticMarkdownUpdateRequest
from backend.services.library_canonical import get_effective_library_markdown
from backend.services.library_semantic_overlay_service import (
    LibrarySemanticOverlayService,
    invalidate_selected_library_semantic_overlay,
)
from backend.services.library_service import LibraryService
from backend.services.preview_manifest_service import LibraryPreviewManifestService
from backend.services.read_errors import ReadValidationError, StaleBaseVersionError
from backend.tests.conftest import authenticate_test_client_in_workspace

pytestmark = pytest.mark.integration


def test_overlay_content_time_changes_only_when_visible_markdown_changes(
    session_factory,
    monkeypatch,
):
    import backend.services.library_semantic_overlay_service as module

    first_time = datetime(2026, 10, 1, tzinfo=UTC)
    clock = {"now": first_time}
    monkeypatch.setattr(module, "utc_now", lambda: clock["now"])
    service = LibrarySemanticOverlayService()
    with session_factory.begin() as session:
        workspace_id, item, version, artifact = _seed_editable(session)
        item.content_changed_at = first_time - timedelta(days=5)
        original_time = item.content_changed_at

        def update(body, revision):
            return service.update_current(
                session=session,
                workspace_id=workspace_id,
                version_id=version.id,
                actor=_actor(),
                body_markdown=body,
                expected_revision=revision,
                expected_base_artifact_id=artifact.id,
                expected_base_content_sha256=artifact.content_sha256,
            )

        original_body = artifact.conversion_detail.index_text
        unchanged = update(original_body, 0)
        assert unchanged.revision == 0
        assert item.content_changed_at == original_time
        changed = update(original_body + "\n\nUser-authored edit.", 0)
        assert changed.revision == 1
        assert item.content_changed_at == first_time

        clock["now"] += timedelta(days=1)
        duplicate = update(changed.body_markdown, 1)
        assert duplicate.revision == 1
        assert item.content_changed_at == first_time
        # Returning to the converter's original text is also a visible change.
        restored = update(original_body, 1)
        assert restored.body_markdown == original_body
        assert item.content_changed_at == clock["now"]


def _actor(actor_id: str = "semantic-editor") -> ActorContext:
    return ActorContext(
        actor_kind="system",
        actor_id=actor_id,
        workspace_role="owner",
        role="owner",
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


def _seed_editable(
    session,
) -> tuple[UUID, LibraryItem, LibraryItemVersion, LibraryArtifact]:
    workspace_id = session.scalar(
        select(Workspace.id).where(Workspace.slug == "default")
    )
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="upload",
        format_hint="docx",
        uri=f"upload://semantic-{uuid4()}.docx",
        title="Semantic 문서",
        trust_level="trusted",
        lifecycle_status="ready",
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="a" * 64,
        raw_storage_ref="inline://source",
        processing_status="ready",
        format_id="docx",
    )
    session.add(version)
    session.flush()
    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test",
        output_schema_version="1.0",
        idempotency_key=f"run:{version.id}",
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
        provider_id="test.converter",
        provider_version="1.0.0",
        provider_spi_version="1.0",
        config_hash="b" * 64,
        input_sha256="a" * 64,
        output_schema_version="1.0",
        status="completed",
        chain_index=0,
        transport_retry_count=0,
        execution_generation=1,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    body = "# 기준 문서\n\n원본 semantic Markdown 😀"
    encoded = body.encode("utf-8")
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    blob_id, blob_epoch = _seed_library_blob(session, workspace_id, salt="e")
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
        content_sha256=content_sha256,
        byte_size=len(encoded),
        ordinal=0,
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        index_text=body,
        location_model={},
    )
    session.add(artifact)
    session.flush()
    version.selected_conversion_attempt_id = attempt.id
    item.current_version_id = version.id
    item.current_ready_version_id = version.id
    session.flush()
    return workspace_id, item, version, artifact


def test_overlay_save_is_immediate_cas_and_preserves_converter_artifact(
    session_factory,
) -> None:
    service = LibrarySemanticOverlayService()
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id, item, version, artifact = _seed_editable(session)
        base = service.get_current(
            session=session,
            workspace_id=workspace_id,
            version_id=version.id,
            actor=actor,
        )
        assert base.revision == 0
        assert base.source == "base"
        assert base.body_markdown == artifact.conversion_detail.index_text
        original_hash = artifact.content_sha256
        original_body = artifact.conversion_detail.index_text
        session.add(
            ContentChunk(
                workspace_id=workspace_id,
                document_kind="library_item",
                document_id=item.id,
                content_scope="published",
                source_version_id=version.id,
                ordinal=0,
                heading_path=None,
                char_start=0,
                char_end=5,
                chunk_text="stale",
                chunk_text_hash="stale",
                token_count=1,
                source_content_hash="stale",
            )
        )
        session.flush()

        edited_body = "# 편집 문서\n\n검색 즉시 반영 😀"
        saved = service.update_current(
            session=session,
            workspace_id=workspace_id,
            version_id=version.id,
            actor=actor,
            body_markdown=edited_body,
            expected_revision=0,
            expected_base_artifact_id=artifact.id,
            expected_base_content_sha256=artifact.content_sha256,
        )

        assert saved.revision == 1
        assert saved.source == "overlay"
        assert saved.body_markdown == edited_body
        assert artifact.conversion_detail.index_text == original_body
        assert artifact.content_sha256 == original_hash
        assert session.scalars(select(ContentChunk)).all() == []
        fts_bodies = session.scalars(
            text(
                "SELECT body FROM search_doc_fts WHERE source_kind = 'library_artifact'"
            )
        ).all()
        assert edited_body in fts_bodies
        assert original_body not in fts_bodies
        job = session.scalar(
            select(Job).where(Job.trigger_type == "library_semantic_overlay")
        )
        assert job is not None
        assert f":1:{saved.content_sha256}" in job.idempotency_key
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "library.semantic_overlay.saved"
            )
        )
        assert audit is not None
        assert session.scalar(select(func.count()).select_from(ReviewItem)) == 0

        second = service.update_current(
            session=session,
            workspace_id=workspace_id,
            version_id=version.id,
            actor=actor,
            body_markdown=edited_body + "\n두 번째 저장",
            expected_revision=1,
            expected_base_artifact_id=artifact.id,
            expected_base_content_sha256=artifact.content_sha256,
        )
        assert second.overlay_id == saved.overlay_id
        assert second.revision == 2
        with pytest.raises(StaleBaseVersionError):
            service.update_current(
                session=session,
                workspace_id=workspace_id,
                version_id=version.id,
                actor=actor,
                body_markdown="stale write",
                expected_revision=1,
                expected_base_artifact_id=artifact.id,
                expected_base_content_sha256=artifact.content_sha256,
            )


def test_overlay_preview_is_first_actor_bound_and_invalidation_falls_back(
    session_factory,
) -> None:
    overlay_service = LibrarySemanticOverlayService()
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id, item, version, artifact = _seed_editable(session)
        saved = overlay_service.update_current(
            session=session,
            workspace_id=workspace_id,
            version_id=version.id,
            actor=actor,
            body_markdown="# Authored\n\nOverlay wins.",
            expected_revision=0,
            expected_base_artifact_id=artifact.id,
            expected_base_content_sha256=artifact.content_sha256,
        )
        manifest_service = LibraryPreviewManifestService(
            store_for=lambda _workspace_id: object(),
            blob_repository_factory=object(),
        )
        manifest = manifest_service.resolve_manifest(
            session=session,
            workspace_id=workspace_id,
            version_id=version.id,
            actor=actor,
        )
        assert manifest.variants[0].id == saved.overlay_id
        assert manifest.variants[0].renderer_contract == "core.markdown.v1"
        assert manifest.variants[0].authored_overlay is True
        assert "/semantic-overlays/" in manifest.variants[0].content_url

        content = overlay_service.resolve_content(
            session=session,
            workspace_id=workspace_id,
            overlay_id=saved.overlay_id,
            actor=actor,
        )
        other_content = overlay_service.resolve_content(
            session=session,
            workspace_id=workspace_id,
            overlay_id=saved.overlay_id,
            actor=_actor("other-editor"),
        )
        assert content.body.decode("utf-8") == saved.body_markdown
        assert content.etag != other_content.etag

        invalidated = invalidate_selected_library_semantic_overlay(
            session,
            version=version,
            reason="selected_conversion_attempt_changed",
        )
        assert invalidated is not None
        assert version.selected_semantic_overlay_id is None
        effective = get_effective_library_markdown(session, version)
        assert effective is not None
        assert effective.overlay is None
        assert effective.body_markdown == artifact.conversion_detail.index_text


def test_overlay_utf8_byte_limit_rejects_emoji_without_mutation(
    session_factory,
) -> None:
    service = LibrarySemanticOverlayService()
    with session_factory.begin() as session:
        workspace_id, item, version, artifact = _seed_editable(session)
        oversized = "😀" * 250_001
        with pytest.raises(ReadValidationError, match="1000000-byte"):
            service.update_current(
                session=session,
                workspace_id=workspace_id,
                version_id=version.id,
                actor=_actor(),
                body_markdown=oversized,
                expected_revision=0,
                expected_base_artifact_id=artifact.id,
                expected_base_content_sha256=artifact.content_sha256,
            )
        assert version.selected_semantic_overlay_id is None
        with pytest.raises(ValidationError, match="1000000-byte"):
            LibrarySemanticMarkdownUpdateRequest(
                body_markdown=oversized,
                expected_revision=0,
                expected_base_artifact_id=artifact.id,
                expected_base_content_sha256=artifact.content_sha256,
            )


def test_versioned_semantic_api_and_isolated_content_headers(
    page_service,
    tmp_path,
) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="semantic-api-test-secret",
        force_security_headers=True,
        library_blob_root=str(tmp_path / "library-blobs"),
    )
    with TestClient(create_app(settings=settings, page_service=page_service)) as client:
        workspace_id = UUID(
            authenticate_test_client_in_workspace(
                client,
                page_service.session_factory,
                email="semantic-owner@example.com",
                workspace_slug="default",
            )
        )
        with page_service.session_factory.begin() as session:
            seeded_workspace_id, item, version, artifact = _seed_editable(session)
            assert seeded_workspace_id == workspace_id
            version_id = version.id
            artifact_id = artifact.id
            artifact_hash = artifact.content_sha256

        url = (
            f"/api/v1/workspaces/{workspace_id}/library/versions/{version_id}"
            "/semantic-markdown"
        )
        base = client.get(url)
        assert base.status_code == 200, base.text
        assert base.json()["data"]["revision"] == 0
        assert base.headers["cache-control"] == "private, no-store"

        saved = client.put(
            url,
            json={
                "body_markdown": "# API 편집 😀",
                "expected_revision": 0,
                "expected_base_artifact_id": str(artifact_id),
                "expected_base_content_sha256": artifact_hash,
            },
        )
        assert saved.status_code == 200, saved.text
        payload = saved.json()["data"]
        assert payload["revision"] == 1
        overlay_id = payload["overlay_id"]

        content_url = (
            f"/api/v1/workspaces/{workspace_id}/library/semantic-overlays/"
            f"{overlay_id}/content"
        )
        content = client.get(content_url)
        assert content.status_code == 200, content.text
        assert content.content.decode("utf-8") == "# API 편집 😀"
        assert content.headers["cache-control"] == "private, no-store"
        assert content.headers["x-content-type-options"] == "nosniff"
        assert content.headers["content-security-policy"] == (
            "default-src 'none'; sandbox"
        )
        assert content.headers["cross-origin-resource-policy"] == "same-origin"
        assert content.headers["content-type"].startswith("text/markdown")
        etag = content.headers["etag"]
        not_modified = client.get(content_url, headers={"If-None-Match": etag})
        assert not_modified.status_code == 304
        assert not_modified.headers["cache-control"] == "private, no-store"


def test_semantic_routes_hide_restricted_item_existence_from_editor(
    page_service,
    tmp_path,
) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="semantic-non-oracle-test-secret",
        library_blob_root=str(tmp_path / "library-blobs"),
    )
    with TestClient(create_app(settings=settings, page_service=page_service)) as client:
        workspace_id = UUID(
            authenticate_test_client_in_workspace(
                client,
                page_service.session_factory,
                email="semantic-editor@example.com",
                workspace_slug="default",
                role="editor",
            )
        )
        with page_service.session_factory.begin() as session:
            seeded_workspace_id, item, version, artifact = _seed_editable(session)
            assert seeded_workspace_id == workspace_id
            item.trust_level = "restricted"
            version_id = version.id
            artifact_id = artifact.id
            artifact_hash = artifact.content_sha256

        missing_version_id = uuid4()
        restricted_url = (
            f"/api/v1/workspaces/{workspace_id}/library/versions/"
            f"{version_id}/semantic-markdown"
        )
        missing_url = (
            f"/api/v1/workspaces/{workspace_id}/library/versions/"
            f"{missing_version_id}/semantic-markdown"
        )
        for url in (restricted_url, missing_url):
            response = client.get(url)
            assert response.status_code == 404
            assert response.json()["error_code"] == "not_found"

            update = client.put(
                url,
                json={
                    "body_markdown": "# hidden",
                    "expected_revision": 0,
                    "expected_base_artifact_id": str(artifact_id),
                    "expected_base_content_sha256": artifact_hash,
                },
            )
            assert update.status_code == 404
            assert update.json()["error_code"] == "not_found"


def test_library_delete_breaks_overlay_pointer_and_removes_owned_row(
    session_factory,
) -> None:
    semantic_service = LibrarySemanticOverlayService()
    library_service = LibraryService()
    actor = _actor()
    with session_factory.begin() as session:
        workspace_id, item, version, artifact = _seed_editable(session)
        saved = semantic_service.update_current(
            session=session,
            workspace_id=workspace_id,
            version_id=version.id,
            actor=actor,
            body_markdown="# Delete me",
            expected_revision=0,
            expected_base_artifact_id=artifact.id,
            expected_base_content_sha256=artifact.content_sha256,
        )
        overlay_id = saved.overlay_id
        assert overlay_id is not None

        library_service.delete_library_item(
            session=session,
            workspace_id=workspace_id,
            library_item_id=item.id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor=actor,
        )

        assert session.get(LibrarySemanticOverlay, overlay_id) is None
        assert session.get(LibraryItem, item.id) is None
