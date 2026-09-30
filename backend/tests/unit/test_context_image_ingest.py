from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from backend.context_media.domain import (
    CONTEXT_MEDIA_POLICY_REVISION,
    CONTEXT_MEDIA_UPLOAD_POLICY,
    ContextMediaEditingNotOpenError,
    ContextMediaError,
    ContextMediaNotFoundError,
    ContextMediaOwnershipError,
    ContextMediaUnavailableError,
)
from backend.context_media.ports import ContextLogicalQuotaExceededError
from backend.media.domain import (
    MediaStagingBytesExceededError,
    MediaStagingLimitExceededError,
)
from backend.media.ports.contracts import IdempotencyConflictError
from backend.media.ports.install import MediaInstallInProgressError
from backend.services import context_image_ingest as ingest_module
from backend.services.context_image_ingest import (
    CONTEXT_IMAGE_INGEST_ERROR_CODES,
    ContextImageIngestCommand,
    ContextImageIngestError,
    ContextImageIngestService,
)
from backend.services.library_blob_storage import LibraryBlobStorageError


def _image_bytes(image_format: str) -> bytes:
    output = BytesIO()
    mode = "RGB" if image_format == "JPEG" else "RGBA"
    Image.new(mode, (2, 3), (15, 31, 63)).save(output, format=image_format)
    return output.getvalue()


VALID_IMAGES = {
    "image/png": _image_bytes("PNG"),
    "image/jpeg": _image_bytes("JPEG"),
    "image/gif": _image_bytes("GIF"),
    "image/webp": _image_bytes("WEBP"),
}


class _UploadStub:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict] = []

    def register(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        asset = SimpleNamespace(
            asset_id=kwargs["asset_id"],
            canonical_media_type=kwargs["canonical_media_type"],
            logical_bytes=len(kwargs["content"]),
        )
        return SimpleNamespace(
            asset=asset,
            logical_ref=f"media:{kwargs['asset_id']}",
            content_url=f"/content/{kwargs['asset_id']}",
            reused=True,
        )


def _command(
    *,
    content: bytes = VALID_IMAGES["image/png"],
    media_type: str = "image/png",
    policy_revision: str = CONTEXT_MEDIA_POLICY_REVISION,
    page_id=None,
    editing_version_id=None,
    legacy_upload_asset_id=None,
) -> ContextImageIngestCommand:
    return ContextImageIngestCommand(
        workspace_id=uuid4(),
        marker_uuid=uuid4(),
        filename="canonical-image",
        declared_media_type=media_type,
        content=content,
        page_id=page_id if page_id is not None else uuid4(),
        editing_version_id=(
            editing_version_id if editing_version_id is not None else uuid4()
        ),
        actor_kind="mcp_client",
        actor_id="canonical-ingest-test",
        policy_revision=policy_revision,
        legacy_upload_asset_id=legacy_upload_asset_id,
    )


@pytest.mark.parametrize(("media_type", "content"), VALID_IMAGES.items())
def test_ingest_decodes_real_format_once_and_passes_validated_facts(
    monkeypatch,
    media_type,
    content,
) -> None:
    upload = _UploadStub()
    service = ContextImageIngestService(upload)
    calls = 0
    real_validate = ingest_module.validate_context_image

    def recording_validate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "validate_context_image", recording_validate)
    command = _command(
        content=content,
        media_type=f" {media_type.upper()} ; charset=binary ",
    )

    result = service.ingest(session_factory=object(), command=command)

    assert calls == 1
    assert result.asset_id == command.marker_uuid
    assert result.canonical_media_type == media_type
    assert result.byte_size == len(content)
    assert result.sha256 == sha256(content).hexdigest()
    assert result.reused is True
    assert len(upload.calls) == 1
    registered = upload.calls[0]
    assert registered["canonical_media_type"] == media_type
    assert registered["max_bytes"] == CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes
    assert registered["policy_revision"] == CONTEXT_MEDIA_POLICY_REVISION
    assert registered["validated_image"].canonical_media_type == media_type
    assert registered["legacy_upload_asset_id"] is None


def test_prepare_reuses_decode_and_carries_legacy_provenance(monkeypatch) -> None:
    upload = _UploadStub()
    service = ContextImageIngestService(upload)
    legacy_upload_asset_id = uuid4()
    command = _command(legacy_upload_asset_id=legacy_upload_asset_id)
    prepared = service.prepare(command)

    def unexpected_validate(*args, **kwargs):
        del args, kwargs
        raise AssertionError("ingest_prepared decoded the image again")

    monkeypatch.setattr(ingest_module, "validate_context_image", unexpected_validate)
    result = service.ingest_prepared(
        session_factory=object(),
        command=command,
        prepared=prepared,
    )

    assert result.sha256 == prepared.sha256
    assert upload.calls[0]["legacy_upload_asset_id"] == legacy_upload_asset_id


@pytest.mark.parametrize(
    ("changed", "reason_code"),
    (
        (
            lambda command: replace(command, filename="changed.png"),
            "prepared_command_drift",
        ),
        (
            lambda command: replace(command, content=command.content + b"x"),
            "prepared_content_drift",
        ),
        (
            lambda command: replace(command, legacy_upload_asset_id=uuid4()),
            "prepared_command_drift",
        ),
    ),
)
def test_ingest_prepared_rejects_command_or_content_drift_before_registration(
    changed,
    reason_code,
) -> None:
    upload = _UploadStub()
    service = ContextImageIngestService(upload)
    command = _command(legacy_upload_asset_id=uuid4())
    prepared = service.prepare(command)

    with pytest.raises(ContextImageIngestError) as captured:
        service.ingest_prepared(
            session_factory=object(),
            command=changed(command),
            prepared=prepared,
        )

    assert captured.value.error_code == "media_state_changed"
    assert captured.value.reason_code == reason_code
    assert upload.calls == []


def test_ingest_prepared_rejects_tampered_digest_before_registration() -> None:
    upload = _UploadStub()
    service = ContextImageIngestService(upload)
    command = _command()
    prepared = replace(service.prepare(command), sha256="0" * 64)

    with pytest.raises(ContextImageIngestError) as captured:
        service.ingest_prepared(
            session_factory=object(),
            command=command,
            prepared=prepared,
        )

    assert captured.value.reason_code == "prepared_content_drift"
    assert upload.calls == []


@pytest.mark.parametrize(
    ("command", "error_code", "reason_code"),
    (
        (
            _command(media_type="text/plain"),
            "media_type_unsupported",
            "media_type_unsupported",
        ),
        (_command(content=b""), "media_content_mismatch", "content_empty"),
        (
            _command(content=b"x" * (CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes + 1)),
            "media_too_large",
            "byte_limit_exceeded",
        ),
        (
            _command(content=VALID_IMAGES["image/png"][:-1]),
            "media_content_mismatch",
            "container_boundary_invalid",
        ),
        (
            _command(policy_revision=""),
            "validation_failed",
            "policy_revision_invalid",
        ),
    ),
)
def test_ingest_rejects_before_registration(command, error_code, reason_code) -> None:
    upload = _UploadStub()

    with pytest.raises(ContextImageIngestError) as captured:
        ContextImageIngestService(upload).ingest(
            session_factory=object(), command=command
        )

    assert captured.value.error_code == error_code
    assert captured.value.reason_code == reason_code
    assert upload.calls == []


@pytest.mark.parametrize(
    ("failure", "error_code"),
    (
        (ContextMediaNotFoundError("missing"), "not_found"),
        (ContextMediaOwnershipError("foreign"), "media_owner_mismatch"),
        (
            ContextMediaEditingNotOpenError("not open"),
            "media_editing_not_open",
        ),
        (ContextMediaUnavailableError("unavailable"), "media_unavailable"),
        (ContextMediaError("other context state"), "media_state_changed"),
        (ContextLogicalQuotaExceededError(limit_bytes=1), "media_quota_exceeded"),
        (MediaStagingLimitExceededError(), "media_staging_limit_exceeded"),
        (MediaStagingBytesExceededError(), "media_staging_bytes_exceeded"),
        (MediaInstallInProgressError(), "media_install_in_progress"),
        (
            LibraryBlobStorageError("private_storage_code", "private detail"),
            "media_install_unavailable",
        ),
        (IdempotencyConflictError("private fingerprint"), "media_state_changed"),
        (ValueError("media_policy_changed"), "media_policy_changed"),
        (ValueError("media_quota_exceeded"), "media_quota_exceeded"),
        (ValueError("private marker detail"), "media_state_changed"),
    ),
)
def test_ingest_normalizes_every_known_registration_failure(
    failure, error_code
) -> None:
    upload = _UploadStub(failure)

    with pytest.raises(ContextImageIngestError) as captured:
        ContextImageIngestService(upload).ingest(
            session_factory=object(), command=_command()
        )

    assert captured.value.error_code == error_code
    assert captured.value.reason_code == error_code
    assert str(captured.value) != str(failure)
    if "private" in str(failure):
        assert "private" not in str(captured.value)


def test_ingest_error_code_taxonomy_is_closed_and_transport_neutral() -> None:
    assert CONTEXT_IMAGE_INGEST_ERROR_CODES == {
        "validation_failed",
        "media_type_unsupported",
        "media_content_mismatch",
        "media_too_large",
        "not_found",
        "media_policy_changed",
        "media_owner_mismatch",
        "media_editing_not_open",
        "media_unavailable",
        "media_quota_exceeded",
        "media_staging_limit_exceeded",
        "media_staging_bytes_exceeded",
        "media_install_in_progress",
        "media_install_unavailable",
        "media_state_changed",
    }
