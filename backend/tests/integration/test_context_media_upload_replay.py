from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from backend.app.container import assemble
from backend.app.db import make_engine, make_session_factory
from backend.app.settings import Settings
from backend.context_media.domain import CONTEXT_MEDIA_POLICY_REVISION
from backend.media.ports.contracts import IdempotencyConflictError
from backend.models import Base
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    ContextMediaUploadRequest,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaInstallRequest,
    MediaPhysicalCharge,
    MediaStagingLease,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaQuotaUsage,
)
from backend.models.upload import UploadAsset
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services import context_media_upload as context_media_upload_module
from backend.services.context_image_ingest import (
    ContextImageIngestCommand,
    ContextImageIngestError,
    ContextImageIngestService,
)
from backend.services.context_image_validation import validate_context_image
from backend.services.context_media_upload import ContextMediaUploadService
from backend.tests.integration.test_context_media_api import PNG_BYTES

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
MAX_BYTES = 1_000_000

LEDGER_MODELS = (
    ContextPage,
    ContextPageVersion,
    ContextMediaAsset,
    ContextMediaUploadRequest,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    StoredBlobRow,
    BlobIncarnationRow,
    StoredBlobInspectionRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    MediaInstallRequest,
    WorkspaceMediaQuotaUsage,
)


def _build_service(session_factory, tmp_path):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="context-media-replay-secret",
        library_blob_root=str(tmp_path / "library"),
    )
    container = assemble(settings, session_factory=session_factory)
    services = container.get_services()
    service = services.context_media_upload
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        assert workspace_id is not None
        page = ContextPage(workspace_id=workspace_id, slug=f"replay-{uuid4().hex}",
            title="Replay", page_type="overview")
        session.add(page)
        session.flush()
        version = ContextPageVersion(workspace_id=workspace_id, page_id=page.id,
            version_no=1, body_md="", content_hash="0" * 64, actor_type="human",
            created_by_actor_kind="human", created_by_actor_id=ACTOR_ID,
            version_status="editing")
        session.add(version)
        session.flush()
    return services, service, workspace_id, page.id, version.id


def _upload_arguments(
    *,
    session_factory,
    workspace_id: UUID,
    page_id: UUID,
    editing_version_id: UUID,
    marker_uuid: UUID,
) -> dict:
    return {
        "session_factory": session_factory,
        "workspace_id": workspace_id,
        "asset_id": marker_uuid,
        "original_filename": "canonical.png",
        "canonical_media_type": "image/png",
        "content": PNG_BYTES,
        "sha256": sha256(PNG_BYTES).hexdigest(),
        "page_id": page_id,
        "editing_version_id": editing_version_id,
        "actor_kind": "human",
        "actor_id": ACTOR_ID,
        "max_bytes": MAX_BYTES,
        "validated_image": validate_context_image(
            PNG_BYTES,
            declared_media_type="image/png",
        ),
        "policy_revision": context_media_upload_module.CONTEXT_MEDIA_POLICY_REVISION,
    }


def _current_ingest_command(arguments: dict) -> ContextImageIngestCommand:
    return ContextImageIngestCommand(
        workspace_id=arguments["workspace_id"],
        marker_uuid=arguments["asset_id"],
        filename=arguments["original_filename"],
        declared_media_type=arguments["canonical_media_type"],
        content=arguments["content"],
        page_id=arguments["page_id"],
        editing_version_id=arguments["editing_version_id"],
        actor_kind=arguments["actor_kind"],
        actor_id=arguments["actor_id"],
        policy_revision=CONTEXT_MEDIA_POLICY_REVISION,
        legacy_upload_asset_id=arguments.get("legacy_upload_asset_id"),
    )


def _seed_legacy_upload_asset(session_factory, workspace_id: UUID) -> UUID:
    digest = sha256(PNG_BYTES).hexdigest()
    with session_factory.begin() as session:
        legacy = UploadAsset(
            workspace_id=workspace_id,
            filename=f"{digest}.png",
            mime="image/png",
            byte_count=len(PNG_BYTES),
            sha256=digest,
            marker_uuid=uuid4(),
            page_id_hint=None,
            created_by_actor_kind="human",
            created_by_actor_id=ACTOR_ID,
        )
        session.add(legacy)
        session.flush()
        return legacy.id


def _ledger_counts(session_factory) -> dict[str, int]:
    with session_factory() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in LEDGER_MODELS
        }


def _quota_snapshot(session_factory, workspace_id: UUID) -> tuple:
    with session_factory() as session:
        logical = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        physical = session.get(WorkspaceMediaQuotaUsage, workspace_id)
        return (
            None
            if logical is None
            else (logical.limit_bytes, logical.charged_bytes, logical.revision),
            None
            if physical is None
            else (physical.limit_bytes, physical.charged_bytes, physical.revision),
        )


def _residue_snapshot(
    session_factory,
    *,
    workspace_id: UUID,
    storage_root: Path,
) -> tuple:
    return (
        _ledger_counts(session_factory),
        _quota_snapshot(session_factory, workspace_id),
        _stored_files(storage_root),
    )


def _inspection_snapshot(
    session_factory,
    *,
    workspace_id: UUID,
    inspection_id: UUID,
) -> tuple[str, str, dict]:
    with session_factory() as session:
        inspection = session.get(
            StoredBlobInspectionRow,
            {"workspace_id": workspace_id, "id": inspection_id},
        )
        assert inspection is not None
        return (
            inspection.registry_version,
            inspection.validation_schema_version,
            dict(inspection.metadata_json),
        )


def _stored_binding(session_factory, workspace_id: UUID, marker_uuid: UUID) -> tuple:
    with session_factory() as session:
        request = session.get(
            ContextMediaUploadRequest,
            {"workspace_id": workspace_id, "marker_uuid": marker_uuid},
        )
        asset = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": marker_uuid},
        )
        assert request is not None
        assert asset is not None
        return (
            request.page_id,
            request.editing_version_id,
            request.asset_id,
            request.sha256,
            request.actor_kind,
            request.actor_id,
            asset.original_filename,
            asset.canonical_media_type,
            asset.logical_bytes,
            asset.policy_revision,
            asset.blob_id,
            asset.blob_epoch,
            asset.inspection_id,
            asset.legacy_upload_asset_id,
        )


def _stored_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }


def test_success_persists_durable_context_upload_operation_binding(
    session_factory,
    tmp_path,
) -> None:
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )

    result = service.register(**arguments)

    assert result.asset.asset_id == marker_uuid
    assert result.asset.created_for_version_id == editing_version_id
    assert result.reused is False
    with session_factory() as session:
        request = session.get(
            ContextMediaUploadRequest,
            {"workspace_id": workspace_id, "marker_uuid": marker_uuid},
        )
        asset = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": marker_uuid},
        )
        charge = session.scalar(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == marker_uuid,
            )
        )
        usage = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        blob = session.get(
            StoredBlobRow,
            {"workspace_id": workspace_id, "id": result.asset.blob.blob_id},
        )

    assert request is not None
    assert request.page_id == page_id
    assert request.editing_version_id == editing_version_id
    assert request.asset_id == marker_uuid
    assert request.sha256 == arguments["sha256"]
    assert request.actor_kind == "human"
    assert request.actor_id == ACTOR_ID
    assert asset is not None
    assert asset.legacy_upload_asset_id is None
    assert asset.original_filename == "canonical.png"
    assert asset.canonical_media_type == "image/png"
    assert asset.logical_bytes == len(PNG_BYTES)
    assert asset.policy_revision == arguments["policy_revision"]
    assert charge is not None and charge.byte_size == len(PNG_BYTES)
    assert charge.state == "charged"
    assert usage is not None and usage.charged_bytes == len(PNG_BYTES)
    assert blob is not None
    registry_version, validation_version, metadata = _inspection_snapshot(
        session_factory,
        workspace_id=workspace_id,
        inspection_id=result.asset.inspection_id,
    )
    assert registry_version == arguments["policy_revision"]
    assert validation_version == arguments["policy_revision"]
    assert metadata["context_policy_revision"] == arguments["policy_revision"]
    assert metadata["context_page_id"] == str(page_id)
    assert metadata["context_editing_version_id"] == str(editing_version_id)
    assert "context_owner_id" not in metadata
    assert "context_owner_kind" not in metadata
    assert metadata["context_image_frame_count"] == 1
    assert metadata["context_image_total_pixels"] == 1
    assert "context_legacy_upload_asset_id" not in metadata


def test_legacy_provenance_is_immutable_in_asset_replay_and_inspection(
    session_factory,
    tmp_path,
) -> None:
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    legacy_upload_asset_id = _seed_legacy_upload_asset(
        session_factory,
        workspace_id,
    )
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    arguments["legacy_upload_asset_id"] = legacy_upload_asset_id

    first = service.register(**arguments)
    counts_before = _ledger_counts(session_factory)
    files_before = _stored_files(tmp_path / "library")
    replay = service.register(**arguments)

    assert first.asset.legacy_upload_asset_id == legacy_upload_asset_id
    assert replay.reused is True
    with session_factory() as session:
        row = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": marker_uuid},
        )
        assert row is not None
        assert row.legacy_upload_asset_id == legacy_upload_asset_id
    _registry, _validation, metadata = _inspection_snapshot(
        session_factory,
        workspace_id=workspace_id,
        inspection_id=first.asset.inspection_id,
    )
    assert metadata["context_legacy_upload_asset_id"] == str(legacy_upload_asset_id)

    with pytest.raises(
        ValueError,
        match="upload marker already identifies a different Context asset",
    ):
        service.register(**{**arguments, "legacy_upload_asset_id": None})

    with pytest.raises(
        ValueError,
        match="legacy upload asset already has a different Context anchor",
    ):
        service.register(**{**arguments, "asset_id": uuid4()})

    assert _ledger_counts(session_factory) == counts_before
    assert _stored_files(tmp_path / "library") == files_before


def test_exact_context_upload_replay_returns_same_asset_without_new_residue(
    session_factory,
    tmp_path,
) -> None:
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    first = service.register(**arguments)
    counts_before = _ledger_counts(session_factory)
    binding_before = _stored_binding(session_factory, workspace_id, marker_uuid)
    files_before = _stored_files(tmp_path / "library")

    replay = service.register(**arguments)

    assert first.reused is False
    assert replay.reused is True
    assert replay.asset == first.asset
    assert replay.asset.created_for_version_id == first.asset.created_for_version_id
    assert _ledger_counts(session_factory) == counts_before
    assert _stored_binding(session_factory, workspace_id, marker_uuid) == binding_before
    assert _stored_files(tmp_path / "library") == files_before


def test_concurrent_identical_marker_has_one_first_success_and_one_reuse(
    tmp_path,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'context-upload-race.sqlite').as_posix()}"
    )
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory.begin() as session:
        seed_default_workspace(session)
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    start = Barrier(2, timeout=10)

    def upload():
        start.wait()
        return service.register(**arguments)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(upload) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]

        assert sorted(result.reused for result in results) == [False, True]
        assert results[0].asset == results[1].asset
        with session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ContextMediaUploadRequest)
                    .where(
                        ContextMediaUploadRequest.workspace_id == workspace_id,
                        ContextMediaUploadRequest.marker_uuid == marker_uuid,
                    )
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ContextMediaLogicalCharge)
                    .where(
                        ContextMediaLogicalCharge.workspace_id == workspace_id,
                        ContextMediaLogicalCharge.asset_id == marker_uuid,
                    )
                )
                == 1
            )
            usage = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
            assert usage is not None
            assert usage.charged_bytes == len(PNG_BYTES)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mismatch",
    ("filename", "media-type", "editing-version", "page", "policy", "digest"),
)
def test_context_upload_replay_fingerprint_mismatch_rejects_without_residue(
    session_factory,
    tmp_path,
    mismatch,
) -> None:
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    service.register(**arguments)
    counts_before = _ledger_counts(session_factory)
    binding_before = _stored_binding(session_factory, workspace_id, marker_uuid)
    files_before = _stored_files(tmp_path / "library")
    replay_arguments = dict(arguments)
    if mismatch == "filename":
        replay_arguments["original_filename"] = "different.png"
    elif mismatch == "media-type":
        replay_arguments["canonical_media_type"] = "image/jpeg"
    elif mismatch == "editing-version":
        replay_arguments["editing_version_id"] = uuid4()
    elif mismatch == "page":
        replay_arguments["page_id"] = uuid4()
    elif mismatch == "policy":
        replay_arguments["policy_revision"] = "context-media-v2"
    else:
        replay_arguments["sha256"] = sha256(
            PNG_BYTES[:-1] + bytes([PNG_BYTES[-1] ^ 0x01])
        ).hexdigest()

    with pytest.raises(
        ValueError,
        match="upload marker already identifies a different Context asset",
    ):
        service.register(**replay_arguments)

    assert _ledger_counts(session_factory) == counts_before
    assert _stored_binding(session_factory, workspace_id, marker_uuid) == binding_before
    assert _stored_files(tmp_path / "library") == files_before


@pytest.mark.parametrize("missing", ["page_id", "editing_version_id"])
def test_upload_requires_explicit_page_and_editing_version_even_on_replay(
    session_factory, tmp_path, missing,
) -> None:
    # Implicit media-session allocation is gone. Neither first call nor
    # replay may omit either component of the explicit upload target.
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=uuid4(),
    )
    incomplete = {key: value for key, value in arguments.items() if key != missing}
    before = _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    )
    with pytest.raises(TypeError, match=missing):
        service.register(**incomplete)
    assert _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    ) == before
    first = service.register(**arguments)
    before = _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    )
    with pytest.raises(TypeError, match=missing):
        service.register(**incomplete)
    assert _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    ) == before
    replay = service.register(**arguments)
    assert replay.asset == first.asset
    assert replay.reused is True


def test_missing_operation_binding_does_not_adopt_another_editing_target(
    session_factory,
    tmp_path,
) -> None:
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    service.register(**arguments)
    with session_factory.begin() as session:
        request = session.get(
            ContextMediaUploadRequest,
            {"workspace_id": workspace_id, "marker_uuid": marker_uuid},
        )
        assert request is not None
        session.delete(request)
    residue_before = _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    )

    with pytest.raises(
        ValueError,
        match="upload marker already identifies a different Context asset",
    ):
        service.register(**{**arguments, "editing_version_id": uuid4()})

    assert (
        _residue_snapshot(
            session_factory,
            workspace_id=workspace_id,
            storage_root=tmp_path / "library",
        )
        == residue_before
    )


def test_editing_submitted_during_install_rejects_without_association(
    session_factory,
    tmp_path,
) -> None:
    services, _service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    inner_factory = services.durable_media_installer_factory

    class SubmitDuringInstall:
        def __init__(self, inner) -> None:
            self._inner = inner

        def execute(self, **kwargs):
            available = self._inner.execute(**kwargs)
            with session_factory.begin() as session:
                session.get(
                    ContextPageVersion, editing_version_id
                ).version_status = "draft"
            return available

    race_upload = ContextMediaUploadService(
        media_installer_factory=lambda factory, wid: SubmitDuringInstall(
            inner_factory(factory, wid)
        ),
        logical_quota_factory=services.context_logical_quota_factory,
        context_media_repository_factory=services.context_media_repository_factory,
    )
    ingest = ContextImageIngestService(race_upload)
    logical_quota_before = _quota_snapshot(session_factory, workspace_id)[0]
    blob_count_before = _ledger_counts(session_factory)[StoredBlobRow.__tablename__]

    with pytest.raises(ContextImageIngestError) as closed:
        ingest.ingest(
            session_factory=session_factory,
            command=_current_ingest_command(arguments),
        )

    assert closed.value.error_code == "media_editing_not_open"
    with session_factory() as session:
        assert (
            session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": marker_uuid},
            )
            is None
        )
        assert (
            session.get(
                ContextMediaUploadRequest,
                {"workspace_id": workspace_id, "marker_uuid": marker_uuid},
            )
            is None
        )
        assert (
            session.scalar(
                select(ContextMediaLogicalCharge).where(
                    ContextMediaLogicalCharge.workspace_id == workspace_id,
                    ContextMediaLogicalCharge.asset_id == marker_uuid,
                )
            )
            is None
        )
    assert _quota_snapshot(session_factory, workspace_id)[0] == logical_quota_before
    assert (
        _ledger_counts(session_factory)[StoredBlobRow.__tablename__]
        == blob_count_before + 1
    )


def test_completed_old_revision_exact_marker_replay_converges_after_transition(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    historical_policy_revision = "context-mvp-v1"
    arguments["policy_revision"] = historical_policy_revision
    monkeypatch.setattr(
        context_media_upload_module,
        "CONTEXT_MEDIA_POLICY_REVISION",
        historical_policy_revision,
    )
    first = service.register(**arguments)
    stored_revision = first.asset.policy_revision
    assert stored_revision == historical_policy_revision
    inspection_before = _inspection_snapshot(
        session_factory,
        workspace_id=workspace_id,
        inspection_id=first.asset.inspection_id,
    )
    assert inspection_before[:2] == ("context-media-v1", "context-media-v1")
    assert inspection_before[2]["context_policy_revision"] == stored_revision
    assert "context_image_frame_count" not in inspection_before[2]
    assert "context_image_total_pixels" not in inspection_before[2]
    counts_before = _ledger_counts(session_factory)
    binding_before = _stored_binding(session_factory, workspace_id, marker_uuid)
    files_before = _stored_files(tmp_path / "library")
    residue_before = _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    )
    monkeypatch.setattr(
        context_media_upload_module,
        "CONTEXT_MEDIA_POLICY_REVISION",
        CONTEXT_MEDIA_POLICY_REVISION,
    )

    with pytest.raises(ContextImageIngestError) as current_replay:
        services.context_image_ingest.ingest(
            session_factory=session_factory,
            command=_current_ingest_command(arguments),
        )
    assert current_replay.value.error_code == "media_policy_changed"
    assert (
        _residue_snapshot(
            session_factory,
            workspace_id=workspace_id,
            storage_root=tmp_path / "library",
        )
        == residue_before
    )
    assert _stored_binding(session_factory, workspace_id, marker_uuid) == binding_before
    assert _stored_files(tmp_path / "library") == files_before

    replay = service.register(**{**arguments, "policy_revision": stored_revision})

    assert first.reused is False
    assert replay.reused is True
    assert replay.asset == first.asset
    assert replay.asset.created_for_version_id == first.asset.created_for_version_id
    assert _ledger_counts(session_factory) == counts_before
    assert _stored_binding(session_factory, workspace_id, marker_uuid) == binding_before
    assert _stored_files(tmp_path / "library") == files_before
    assert (
        _inspection_snapshot(
            session_factory,
            workspace_id=workspace_id,
            inspection_id=replay.asset.inspection_id,
        )
        == inspection_before
    )


class _FailFirstAssetAssociation:
    def __init__(self, delegate, state: dict[str, bool]) -> None:
        self._delegate = delegate
        self._state = state

    def add_asset(self, asset):
        if self._state["fail"]:
            self._state["fail"] = False
            raise RuntimeError("injected Context asset association failure")
        return self._delegate.add_asset(asset)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


def test_association_failure_then_exact_retry_converges_without_duplicate_blob(
    session_factory,
    tmp_path,
) -> None:
    services, _service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    state = {"fail": True}
    repository_factory = services.context_media_repository_factory

    def failing_repository_factory(transaction):
        return _FailFirstAssetAssociation(
            repository_factory(transaction),
            state,
        )

    service = ContextMediaUploadService(
        media_installer_factory=services.durable_media_installer_factory,
        logical_quota_factory=services.context_logical_quota_factory,
        context_media_repository_factory=failing_repository_factory,
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    counts_before = _ledger_counts(session_factory)

    with pytest.raises(
        RuntimeError,
        match="injected Context asset association failure",
    ):
        service.register(**arguments)

    counts_after_failure = _ledger_counts(session_factory)
    assert (
        counts_after_failure[ContextMediaAsset.__tablename__]
        == counts_before[ContextMediaAsset.__tablename__]
    )
    assert (
        counts_after_failure[ContextMediaUploadRequest.__tablename__]
        == counts_before[ContextMediaUploadRequest.__tablename__]
    )
    assert (
        counts_after_failure[ContextMediaLogicalCharge.__tablename__]
        == counts_before[ContextMediaLogicalCharge.__tablename__]
    )
    assert counts_after_failure[StoredBlobRow.__tablename__] == (
        counts_before[StoredBlobRow.__tablename__] + 1
    )
    files_after_failure = _stored_files(tmp_path / "library")
    assert len(files_after_failure) == 1

    recovered = service.register(**arguments)

    counts_after_retry = _ledger_counts(session_factory)
    assert recovered.asset.asset_id == marker_uuid
    assert recovered.asset.created_for_version_id == editing_version_id
    assert recovered.reused is False
    assert counts_after_retry[ContextMediaAsset.__tablename__] == (
        counts_before[ContextMediaAsset.__tablename__] + 1
    )
    assert counts_after_retry[ContextMediaUploadRequest.__tablename__] == (
        counts_before[ContextMediaUploadRequest.__tablename__] + 1
    )
    assert counts_after_retry[ContextMediaLogicalCharge.__tablename__] == (
        counts_before[ContextMediaLogicalCharge.__tablename__] + 1
    )
    assert (
        counts_after_retry[StoredBlobRow.__tablename__]
        == counts_after_failure[StoredBlobRow.__tablename__]
    )
    assert (
        counts_after_retry[BlobIncarnationRow.__tablename__]
        == counts_after_failure[BlobIncarnationRow.__tablename__]
    )
    assert (
        counts_after_retry[StoredBlobInspectionRow.__tablename__]
        == (counts_after_failure[StoredBlobInspectionRow.__tablename__])
    )
    assert (
        counts_after_retry[MediaPhysicalCharge.__tablename__]
        == counts_after_failure[MediaPhysicalCharge.__tablename__]
    )
    assert (
        counts_after_retry[MediaInstallRequest.__tablename__]
        == counts_after_failure[MediaInstallRequest.__tablename__]
    )
    assert _stored_files(tmp_path / "library") == files_after_failure
    assert _stored_binding(session_factory, workspace_id, marker_uuid)[2] == marker_uuid


def test_association_failure_keeps_full_context_fingerprint_bound_before_retry(
    session_factory,
    tmp_path,
) -> None:
    services, _service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    state = {"fail": True}
    repository_factory = services.context_media_repository_factory

    def failing_repository_factory(transaction):
        return _FailFirstAssetAssociation(repository_factory(transaction), state)

    service = ContextMediaUploadService(
        media_installer_factory=services.durable_media_installer_factory,
        logical_quota_factory=services.context_logical_quota_factory,
        context_media_repository_factory=failing_repository_factory,
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )

    with pytest.raises(RuntimeError, match="association failure"):
        service.register(**arguments)

    counts_after_failure = _ledger_counts(session_factory)
    files_after_failure = _stored_files(tmp_path / "library")
    with pytest.raises(IdempotencyConflictError):
        service.register(**{**arguments, "original_filename": "changed.png"})

    assert _ledger_counts(session_factory) == counts_after_failure
    assert _stored_files(tmp_path / "library") == files_after_failure
    with session_factory() as session:
        assert (
            session.get(
                ContextMediaUploadRequest,
                {"workspace_id": workspace_id, "marker_uuid": marker_uuid},
            )
            is None
        )
        assert (
            session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": marker_uuid},
            )
            is None
        )


def test_old_revision_new_marker_rejects_without_any_residue_after_transition(
    session_factory,
    tmp_path,
) -> None:
    _services, service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=uuid4(),
    )
    arguments["policy_revision"] = "context-mvp-v1"
    assert context_media_upload_module.CONTEXT_MEDIA_POLICY_REVISION == (
        CONTEXT_MEDIA_POLICY_REVISION
    )
    residue_before = _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    )

    with pytest.raises(ValueError, match="^media_policy_changed$"):
        service.register(**arguments)

    assert (
        _residue_snapshot(
            session_factory,
            workspace_id=workspace_id,
            storage_root=tmp_path / "library",
        )
        == residue_before
    )
    with session_factory() as session:
        assert (
            session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": arguments["asset_id"]},
            )
            is None
        )
        assert (
            session.get(
                ContextMediaUploadRequest,
                {"workspace_id": workspace_id, "marker_uuid": arguments["asset_id"]},
            )
            is None
        )
        assert (
            session.scalar(
                select(MediaStagingLease).where(
                    MediaStagingLease.workspace_id == workspace_id,
                    MediaStagingLease.idempotency_key
                    == f"context:{arguments['asset_id']}",
                )
            )
            is None
        )


def test_partial_install_old_revision_exact_marker_replay_converges_after_transition(
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    services, _service, workspace_id, page_id, editing_version_id = _build_service(
        session_factory, tmp_path
    )
    state = {"fail": True}
    repository_factory = services.context_media_repository_factory

    def failing_repository_factory(transaction):
        return _FailFirstAssetAssociation(repository_factory(transaction), state)

    service = ContextMediaUploadService(
        media_installer_factory=services.durable_media_installer_factory,
        logical_quota_factory=services.context_logical_quota_factory,
        context_media_repository_factory=failing_repository_factory,
    )
    marker_uuid = uuid4()
    arguments = _upload_arguments(
        session_factory=session_factory,
        workspace_id=workspace_id,
        page_id=page_id,
        editing_version_id=editing_version_id,
        marker_uuid=marker_uuid,
    )
    historical_policy_revision = "context-mvp-v1"
    arguments["policy_revision"] = historical_policy_revision
    monkeypatch.setattr(
        context_media_upload_module,
        "CONTEXT_MEDIA_POLICY_REVISION",
        historical_policy_revision,
    )
    stored_revision = arguments["policy_revision"]
    assert stored_revision == historical_policy_revision
    counts_before = _ledger_counts(session_factory)

    with pytest.raises(RuntimeError, match="association failure"):
        service.register(**arguments)

    counts_after_failure = _ledger_counts(session_factory)
    files_after_failure = _stored_files(tmp_path / "library")
    assert (
        counts_after_failure[ContextPageVersion.__tablename__]
        == counts_before[ContextPageVersion.__tablename__]
    )
    assert counts_after_failure[StoredBlobRow.__tablename__] == (
        counts_before[StoredBlobRow.__tablename__] + 1
    )
    with session_factory() as session:
        partial_inspection = session.scalars(
            select(StoredBlobInspectionRow).where(
                StoredBlobInspectionRow.workspace_id == workspace_id
            )
        ).one()
        partial_inspection_id = partial_inspection.id
    partial_inspection_before = _inspection_snapshot(
        session_factory,
        workspace_id=workspace_id,
        inspection_id=partial_inspection_id,
    )
    assert partial_inspection_before[:2] == (
        "context-media-v1",
        "context-media-v1",
    )
    assert partial_inspection_before[2]["context_policy_revision"] == stored_revision
    assert "context_image_frame_count" not in partial_inspection_before[2]
    assert "context_image_total_pixels" not in partial_inspection_before[2]
    assert "context_legacy_upload_asset_id" not in partial_inspection_before[2]

    monkeypatch.setattr(
        context_media_upload_module,
        "CONTEXT_MEDIA_POLICY_REVISION",
        CONTEXT_MEDIA_POLICY_REVISION,
    )
    residue_before_mismatch = _residue_snapshot(
        session_factory,
        workspace_id=workspace_id,
        storage_root=tmp_path / "library",
    )
    with pytest.raises(ContextImageIngestError) as current_replay:
        services.context_image_ingest.ingest(
            session_factory=session_factory,
            command=_current_ingest_command(arguments),
        )
    assert current_replay.value.error_code == "media_policy_changed"
    assert (
        _residue_snapshot(
            session_factory,
            workspace_id=workspace_id,
            storage_root=tmp_path / "library",
        )
        == residue_before_mismatch
    )

    recovered = service.register(**{**arguments, "policy_revision": stored_revision})

    counts_after_replay = _ledger_counts(session_factory)
    assert recovered.asset.asset_id == marker_uuid
    assert recovered.asset.legacy_upload_asset_id is None
    assert recovered.asset.created_for_version_id is not None
    assert recovered.reused is False
    assert counts_after_replay[ContextPageVersion.__tablename__] == (
        counts_before[ContextPageVersion.__tablename__]
    )
    assert (
        counts_after_replay[StoredBlobRow.__tablename__]
        == counts_after_failure[StoredBlobRow.__tablename__]
    )
    assert _stored_files(tmp_path / "library") == files_after_failure
    assert recovered.asset.inspection_id == partial_inspection_id
    assert (
        _inspection_snapshot(
            session_factory,
            workspace_id=workspace_id,
            inspection_id=recovered.asset.inspection_id,
        )
        == partial_inspection_before
    )
