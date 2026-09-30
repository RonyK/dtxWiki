import hashlib
from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.library_media import LibraryBlobCandidate, RegisterLibraryBlob
from backend.library_media.adapters.sqlalchemy import SqlAlchemyLibraryBlobRepository
from backend.library_media.domain import LibraryBlobRecord
from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
from backend.media.adapters.sqlalchemy.quota import SqlAlchemyPhysicalQuota
from backend.media.adapters.sqlalchemy.repository import SqlAlchemyMediaRepository
from backend.media.application import InstallValidatedBlob
from backend.media.domain import (
    BlobEpoch,
    BlobReference,
    Sha256Digest,
    WorkspaceBlobIdentity,
)
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
from backend.models.media import BlobIncarnationRow, SecurityBlockedBlobRow
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.library_blob_storage import FilesystemLibraryBlobStore
from backend.services.library_content_service import (
    HttpByteRange,
    LibraryByteRangeError,
    LibraryContentIntegrityError,
    LibraryContentService,
    LibraryOriginalUnavailableError,
    _opaque_library_etag,
    parse_http_byte_range,
    safe_download_filename,
    safe_media_type,
)
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError


def _system_actor() -> ActorContext:
    return ActorContext(actor_kind="system", actor_id="library-content-test")


def _workspace_id(session) -> UUID:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
    assert workspace is not None
    return workspace.id


def _blob_repository(session) -> SqlAlchemyLibraryBlobRepository:
    return SqlAlchemyLibraryBlobRepository(
        session,
        media_repository=SqlAlchemyMediaRepository(session),
        physical_quota=SqlAlchemyPhysicalQuota(session),
    )


def _block_blob(session, reference: BlobReference) -> None:
    session.add(
        SecurityBlockedBlobRow(
            workspace_id=reference.workspace_id,
            blob_id=reference.blob_id,
            blob_epoch=reference.epoch.value,
            generation=1,
            state="quarantined",
            reason_code="unit_test_block",
        )
    )
    session.flush()


def _seed_source(
    session,
    store: FilesystemLibraryBlobStore,
    *,
    workspace_id: UUID,
    content: bytes = b"0123456789",
    filename: str = "report.pdf",
    media_type: str = "application/pdf",
    trust_level: str = "trusted",
    lifecycle_status: str = "ready",
) -> tuple[LibraryItem, LibraryItemVersion, LibraryBlobRecord]:
    stored = store.put_bytes(
        content,
        workspace_id=workspace_id,
        max_bytes=max(1, len(content)),
    )
    blob = RegisterLibraryBlob().execute(
        _blob_repository(session),
        LibraryBlobCandidate(
            workspace_id=workspace_id,
            sha256=Sha256Digest(stored.sha256),
            byte_size=stored.byte_size,
            storage_key=stored.storage_key,
            media_type=media_type,
            format_id="pdf",
            detector_id="test",
            metadata={},
        ),
    )
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="manual_upload",
        format_hint="pdf",
        uri=f"upload://{workspace_id}/{uuid4()}",
        title=filename,
        metadata_json={},
        trust_level=trust_level,
        lifecycle_status=lifecycle_status,
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash=stored.sha256,
        raw_storage_ref=None,
        source_blob_workspace_id=blob.reference.workspace_id,
        source_blob_id=blob.reference.blob_id,
        source_blob_epoch=blob.reference.epoch.value,
        original_filename=filename,
        declared_media_type="application/octet-stream",
        detected_media_type=media_type,
        format_id="pdf",
        byte_size=stored.byte_size,
        detection_json={"source": "test"},
        processing_status="ready",
    )
    session.add(version)
    session.flush()
    item.current_version_id = version.id
    item.current_ready_version_id = version.id
    session.flush()
    return item, version, blob


def _seed_artifact(
    session_factory,
    store: FilesystemLibraryBlobStore,
    *,
    workspace_id: UUID,
    content: bytes = b"derived text",
) -> tuple[LibraryItem, LibraryItemVersion, LibraryArtifact]:
    installed = InstallValidatedBlob(
        stager=store,
        publisher=store,
        deleter=store,
        layout=store,
        journal=SqlAlchemyMediaInstallJournal(session_factory),
    ).execute(
        workspace_id=workspace_id,
        actor_id=uuid4(),
        session_key="library:test-artifact",
        idempotency_key=f"library:test-artifact:{uuid4()}",
        content=content,
        max_bytes=1024,
        canonical_media_type="text/plain",
        format_id="derived-preview",
        detector_id="library-conversion-output",
        detector_version="1",
        registry_version="test-registry-v1",
        validation_schema_version="test-schema-v1",
    )
    with session_factory.begin() as session:
        item, version, _source_blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=b"source",
            filename="source.txt",
            media_type="text/plain",
        )
        run = LibraryRun(
            workspace_id=version.workspace_id,
            library_item_version_id=version.id,
            stage="conversion",
            run_seq=1,
            reason="initial",
            status="completed",
            requested_by_actor_type="user",
            requested_by_actor_id=uuid4(),
            provider_chain_json=[],
            policy_revision="test-policy",
            output_schema_version="test-output-v1",
            idempotency_key=f"initial:{version.id}",
        )
        run.conversion_detail = LibraryRunConversionDetail(
            error_fallback_matrix_json={},
            per_provider_budget_json={},
            total_budget_json={},
        )
        session.add(run)
        session.flush()
        attempt = LibraryAttempt(
            workspace_id=version.workspace_id,
            run_id=run.id,
            library_item_version_id=version.id,
            stage="conversion",
            provider_id="test.converter",
            provider_version="1",
            provider_spi_version="1",
            config_hash="a" * 64,
            input_sha256=version.content_hash,
            output_schema_version="test-output-v1",
            status="completed",
            chain_index=0,
            transport_retry_count=0,
            execution_generation=1,
            provenance_json={},
            metrics_json={},
            warnings_json=[],
        )
        attempt.conversion_detail = LibraryAttemptConversionDetail(
            manual_retry_disposition=None,
            disposition_reason=None,
        )
        session.add(attempt)
        session.flush()
        artifact = LibraryArtifact(
            workspace_id=version.workspace_id,
            library_item_version_id=version.id,
            attempt_id=attempt.id,
            stage="conversion",
            blob_workspace_id=installed.reference.workspace_id,
            blob_id=installed.reference.blob_id,
            blob_epoch=installed.reference.epoch.value,
            role="preview",
            kind="text",
            media_type="text/plain",
            content_sha256=installed.sha256.value,
            byte_size=installed.byte_size,
            ordinal=0,
        )
        artifact.conversion_detail = LibraryArtifactConversionDetail(
            is_complete=True,
            sanitizer_version="test",
            extraction_method="test.converter",
            index_text=content.decode(),
            location_model={"filename": "preview.txt"},
        )
        session.add(artifact)
        session.flush()
        version.selected_conversion_attempt_id = attempt.id
        item.current_ready_version_id = version.id
        session.flush()
        return item, version, artifact


def test_resolve_original_returns_path_free_verified_handle_and_safe_metadata(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )
    content = b"original-pdf"

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=content,
            filename='folder\\quarterly; "report".PDF',
            media_type="Application/PDF; reflected=parameter",
        )

        result = service.resolve_original(
            session=session,
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_id=version.id,
            actor=_system_actor(),
        )

        assert result.filename == "quarterly_ _report_.PDF"
        assert result.media_type == "application/pdf"
        assert result.byte_size == len(content)
        assert result.content_length == len(content)
        assert result.content_range is None
        digest = hashlib.sha256(content).hexdigest()
        assert result.etag.startswith('"lib-v1-')
        assert digest not in result.etag
        assert result.etag != f'"sha256-{digest}"'
        assert not hasattr(result, "storage_key")
        assert not hasattr(result.blob, "storage_key")
        assert not hasattr(result.blob, "path")
        assert str(tmp_path) not in repr(result)
        with result.blob.open_binary() as stream:
            assert stream.read() == content


def test_library_etag_changes_with_authorization_association_and_incarnation(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )
    shared_content = b"same immutable content"

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        first_item, first_version, blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=shared_content,
        )
        second_item, second_version, _same_blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=shared_content,
        )
        first = service.resolve_original(
            session=session,
            workspace_id=workspace_id,
            library_item_id=first_item.id,
            version_id=first_version.id,
            actor=_system_actor(),
        )
        other_actor = service.resolve_original(
            session=session,
            workspace_id=workspace_id,
            library_item_id=first_item.id,
            version_id=first_version.id,
            actor=ActorContext(actor_kind="system", actor_id="other-system"),
        )
        other_association = service.resolve_original(
            session=session,
            workspace_id=workspace_id,
            library_item_id=second_item.id,
            version_id=second_version.id,
            actor=_system_actor(),
        )
        next_incarnation = _opaque_library_etag(
            content_kind="original",
            actor=_system_actor(),
            membership_role=None,
            workspace_id=workspace_id,
            library_item_id=first_item.id,
            version_id=first_version.id,
            artifact_id=None,
            lifecycle_status=first_item.lifecycle_status,
            trust_level=first_item.trust_level,
            blob=BlobReference(
                workspace_id=blob.reference.workspace_id,
                blob_id=blob.reference.blob_id,
                epoch=BlobEpoch(blob.reference.epoch.value + 1),
            ),
        )

    assert first.etag != other_actor.etag
    assert first.etag != other_association.etag
    assert first.etag != next_incarnation
    assert hashlib.sha256(shared_content).hexdigest() not in first.etag


def test_resolve_artifact_returns_only_selected_ready_content_with_range(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
    item, version, artifact = _seed_artifact(
        session_factory,
        store,
        workspace_id=workspace_id,
    )

    with session_factory() as session:
        result = service.resolve_artifact(
            session=session,
            workspace_id=workspace_id,
            artifact_id=artifact.id,
            actor=_system_actor(),
            range_header="bytes=2-6",
        )

        assert result.library_item_id == item.id
        assert result.version_id == version.id
        assert result.filename == "preview.txt"
        assert result.media_type == "text/plain"
        assert result.content_range == "bytes 2-6/12"
        assert result.etag.startswith('"lib-v1-')
        assert result.sha256 not in result.etag
        with result.blob.open_binary() as stream:
            stream.seek(result.byte_range.start if result.byte_range else 0)
            assert stream.read(result.content_length) == b"rived"


def test_resolve_artifact_hides_foreign_workspace_artifact(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
    _item, _version, artifact = _seed_artifact(
        session_factory,
        store,
        workspace_id=workspace_id,
    )
    with session_factory.begin() as session:
        foreign_workspace = Workspace(
            slug="library-content-foreign",
            title="Library content foreign",
            name="Library content foreign",
        )
        session.add(foreign_workspace)
        session.flush()

        with pytest.raises(ReadNotFoundError) as denied:
            service.resolve_artifact(
                session=session,
                workspace_id=foreign_workspace.id,
                artifact_id=artifact.id,
                actor=_system_actor(),
            )

        assert denied.value.error_code == "not_found"


def test_resolve_artifact_hides_non_selected_attempt_content(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
    _item, version, artifact = _seed_artifact(
        session_factory,
        store,
        workspace_id=workspace_id,
    )
    with session_factory.begin() as session:
        persisted = session.get(LibraryItemVersion, version.id)
        assert persisted is not None
        persisted.selected_conversion_attempt_id = None

    with session_factory() as session:
        with pytest.raises(ReadNotFoundError):
            service.resolve_artifact(
                session=session,
                workspace_id=workspace_id,
                artifact_id=artifact.id,
                actor=_system_actor(),
            )


def test_library_original_and_dedupe_fail_closed_for_security_blocker(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
        )
        _block_blob(session, blob.reference)

        repository = _blob_repository(session)
        assert repository.get(blob.reference) is None
        with pytest.raises(ValueError, match="security blocked"):
            repository.find(WorkspaceBlobIdentity(workspace_id, blob.sha256))
        with pytest.raises(ReadNotFoundError):
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=_system_actor(),
            )


def test_library_artifact_read_fails_closed_for_security_blocker(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )
    with session_factory() as session:
        workspace_id = _workspace_id(session)
    _item, _version, artifact = _seed_artifact(
        session_factory,
        store,
        workspace_id=workspace_id,
    )

    with session_factory.begin() as session:
        persisted = session.get(LibraryArtifact, artifact.id)
        assert persisted is not None
        reference = BlobReference(
            workspace_id=persisted.blob_workspace_id,
            blob_id=persisted.blob_id,
            epoch=BlobEpoch(persisted.blob_epoch),
        )
        _block_blob(session, reference)

        with pytest.raises(ReadNotFoundError):
            service.resolve_artifact(
                session=session,
                workspace_id=workspace_id,
                artifact_id=persisted.id,
                actor=_system_actor(),
            )


def test_resolve_original_requires_workspace_item_version_association(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        first_item, _first_version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=b"same bytes",
        )
        _second_item, second_version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=b"same bytes",
        )

        with pytest.raises(ReadNotFoundError):
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=first_item.id,
                version_id=second_version.id,
                actor=_system_actor(),
            )

        with pytest.raises(ReadNotFoundError):
            service.resolve_original(
                session=session,
                workspace_id=uuid4(),
                library_item_id=first_item.id,
                version_id=second_version.id,
                actor=_system_actor(),
            )


def test_resolve_original_distinguishes_no_preserved_original_from_not_found(
    session_factory,
    tmp_path,
):
    """A version with no source blob is a distinct case from a missing version.

    Both used to raise the same generic ReadNotFoundError, so a viewer saw an
    identical "not found" message for "this version doesn't exist" and "this
    version exists but never had a preserved original" -- collapsing the
    distinction the P6.3 UI contract requires.
    """

    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
        )
        persisted = session.get(LibraryItemVersion, version.id)
        assert persisted is not None
        # library_item_version_source_check requires source_blob_id OR
        # raw_storage_ref -- a version can only drop the blob reference by
        # falling back to the older raw_storage_ref-backed storage shape,
        # which resolve_original doesn't know how to serve.
        persisted.raw_storage_ref = "legacy://unused"
        persisted.source_blob_workspace_id = None
        persisted.source_blob_id = None
        persisted.source_blob_epoch = None
        session.flush()

    with session_factory() as session:
        with pytest.raises(LibraryOriginalUnavailableError) as excinfo:
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=_system_actor(),
            )
        assert excinfo.value.error_code == "library_original_unavailable"


def test_resolve_original_rechecks_workspace_access(
    session_factory,
    tmp_path,
    mcp_default_actor,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    actor = mcp_default_actor(
        actor_id="library-cross-workspace-pat",
        scopes=("mcp:read",),
    )
    with session_factory.begin() as session:
        workspace = Workspace(
            slug="library-content-other",
            title="Library content other",
            name="Library content other",
        )
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        item, version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
        )

        with pytest.raises(PermissionError, match="token_workspace_denied"):
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=actor,
            )


def test_resolve_original_hides_archived_item(session_factory, tmp_path):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            lifecycle_status="archived",
        )

        with pytest.raises(ReadNotFoundError):
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=_system_actor(),
            )


def test_resolve_original_enforces_restricted_trust_policy(
    session_factory,
    tmp_path,
    mcp_default_actor,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    viewer_pat = mcp_default_actor(
        actor_id="restricted-library-viewer-pat",
        scopes=("mcp:read",),
        role="viewer",
    )
    owner_pat = mcp_default_actor(
        actor_id="restricted-library-owner-pat",
        scopes=("mcp:read",),
    )
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, _blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            trust_level="restricted",
        )

        with pytest.raises(RejectedByPolicyError) as denied:
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=viewer_pat,
            )
        assert denied.value.error_code == "restricted_library_denied"

        permitted = service.resolve_original(
            session=session,
            workspace_id=workspace_id,
            library_item_id=item.id,
            version_id=version.id,
            actor=owner_pat,
        )
        assert permitted.version_id == version.id


@pytest.mark.parametrize("corruption", ["hash", "size", "storage_key"])
def test_resolve_original_rejects_persisted_blob_identity_conflicts(
    session_factory,
    tmp_path,
    corruption,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
        )
        if corruption == "hash":
            version.content_hash = "f" * 64
        elif corruption == "size":
            version.byte_size = blob.byte_size + 1
        else:
            incarnation = session.get(
                BlobIncarnationRow,
                {
                    "workspace_id": blob.reference.workspace_id,
                    "blob_id": blob.reference.blob_id,
                    "blob_epoch": blob.reference.epoch.value,
                },
            )
            assert incarnation is not None
            incarnation.storage_key = "workspaces/invalid/sha256/ff/ff/" + "f" * 64

        with pytest.raises(LibraryContentIntegrityError):
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=_system_actor(),
            )


def test_resolve_original_detects_corrupted_bytes_before_return(
    session_factory,
    tmp_path,
):
    store = FilesystemLibraryBlobStore(tmp_path / "library")
    service = LibraryContentService(
        store_for=lambda _workspace_id: store,
        blob_repository_factory=_blob_repository,
    )

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        item, version, blob = _seed_source(
            session,
            store,
            workspace_id=workspace_id,
            content=b"good",
        )
        store.path_for_key(blob.storage_key).write_bytes(b"evil")

        with pytest.raises(LibraryContentIntegrityError):
            service.resolve_original(
                session=session,
                workspace_id=workspace_id,
                library_item_id=item.id,
                version_id=version.id,
                actor=_system_actor(),
            )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-3", (0, 3)),
        ("bytes=5-", (5, 9)),
        ("bytes=-3", (7, 9)),
        ("bytes=-20", (0, 9)),
        ("BYTES=1-999", (1, 9)),
    ],
)
def test_parse_http_byte_range_normalizes_single_explicit_open_and_suffix_ranges(
    header,
    expected,
):
    result = parse_http_byte_range(
        header,
        complete_length=10,
        max_range_bytes=10,
    )

    assert result is not None
    assert (result.start, result.end) == expected
    assert result.content_range == f"bytes {expected[0]}-{expected[1]}/10"
    assert result.length == expected[1] - expected[0] + 1


@pytest.mark.parametrize(
    "header",
    [
        "items=0-1",
        "bytes=",
        "bytes=-",
        "bytes=-0",
        "bytes=3-2",
        "bytes=10-10",
        "bytes=0-1,3-4",
        "bytes =0-1",
        "bytes=0 -1",
        "bytes=" + "9" * 21 + "-",
    ],
)
def test_parse_http_byte_range_rejects_multi_or_invalid_ranges(header):
    with pytest.raises(LibraryByteRangeError) as error:
        parse_http_byte_range(
            header,
            complete_length=10,
            max_range_bytes=10,
        )

    assert error.value.complete_length == 10


def test_parse_http_byte_range_rejects_unbounded_response_and_empty_resource():
    with pytest.raises(LibraryByteRangeError):
        parse_http_byte_range(
            "bytes=0-5",
            complete_length=10,
            max_range_bytes=5,
        )
    with pytest.raises(LibraryByteRangeError) as empty_error:
        parse_http_byte_range(
            "bytes=0-0",
            complete_length=0,
            max_range_bytes=5,
        )
    assert empty_error.value.complete_length == 0
    assert (
        parse_http_byte_range(
            None,
            complete_length=0,
            max_range_bytes=5,
        )
        is None
    )


def test_http_byte_range_is_immutable():
    value = HttpByteRange(start=0, end=0, complete_length=1)

    with pytest.raises(FrozenInstanceError):
        value.start = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("../report.pdf", "report.pdf"),
        ("CON.txt", "library-aaaaaaaaaaaa.bin"),
        ("line\r\nbreak.txt", "line_break.txt"),
        (None, "library-aaaaaaaaaaaa.bin"),
    ],
)
def test_safe_download_filename_never_reflects_path_or_header_characters(
    value,
    expected,
):
    assert safe_download_filename(value, sha256="a" * 64) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("IMAGE/PNG", "image/png"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("text/plain\r\nX-Evil: yes", "application/octet-stream"),
        (None, "application/octet-stream"),
    ],
)
def test_safe_media_type_only_returns_a_valid_bare_media_type(value, expected):
    assert safe_media_type(value) == expected
