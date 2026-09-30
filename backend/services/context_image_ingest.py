"""Canonical Context image admission shared by REST and MCP transports."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from backend.context_media.domain import (
    CONTEXT_MEDIA_UPLOAD_POLICY,
    ContextMediaError,
    context_image_format_for_media_type,
)
from backend.context_media.ports import ContextLogicalQuotaExceededError
from backend.media.domain import MediaStagingError
from backend.media.ports.contracts import IdempotencyConflictError
from backend.services.context_image_validation import (
    ContextImageValidationError,
    ValidatedContextImage,
    validate_context_image,
)
from backend.services.context_media_upload import ContextMediaUploadService
from backend.services.library_blob_storage import LibraryBlobStorageError

CONTEXT_IMAGE_INGEST_ERROR_CODES = frozenset(
    {
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
)

_CONTEXT_MEDIA_UPLOAD_ERROR_CODES = frozenset(
    {
        "not_found",
        "media_owner_mismatch",
        "media_editing_not_open",
        "media_unavailable",
    }
)


class ContextImageIngestError(ValueError):
    """Stable transport-neutral failure emitted before durable admission."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        reason_code: str | None = None,
    ) -> None:
        if error_code not in CONTEXT_IMAGE_INGEST_ERROR_CODES:
            raise ValueError("unsupported Context image ingest error code")
        super().__init__(message)
        self.error_code = error_code
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ContextImageIngestCommand:
    workspace_id: UUID
    marker_uuid: UUID
    filename: str
    declared_media_type: str
    content: bytes
    page_id: UUID
    editing_version_id: UUID
    actor_kind: str
    actor_id: str
    policy_revision: str
    legacy_upload_asset_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PreparedContextImageIngest:
    """Validated immutable facts reusable by reconciliation apply."""

    command: ContextImageIngestCommand
    canonical_media_type: str
    validated_image: ValidatedContextImage
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ContextImageIngestResult:
    asset_id: UUID
    logical_ref: str
    content_url: str
    canonical_media_type: str
    byte_size: int
    sha256: str
    reused: bool


class ContextImageIngestService:
    """Own Context image policy, decoding, digesting, and durable registration."""

    def __init__(self, context_media_upload: ContextMediaUploadService) -> None:
        self._context_media_upload = context_media_upload

    def ingest(
        self,
        *,
        session_factory: sessionmaker[Session],
        command: ContextImageIngestCommand,
    ) -> ContextImageIngestResult:
        return self.ingest_prepared(
            session_factory=session_factory,
            command=command,
            prepared=self.prepare(command),
        )

    def prepare(
        self,
        command: ContextImageIngestCommand,
    ) -> PreparedContextImageIngest:
        if not command.policy_revision or len(command.policy_revision) > 160:
            raise ContextImageIngestError(
                "validation_failed",
                "Context media policy revision is invalid",
                reason_code="policy_revision_invalid",
            )
        if command.legacy_upload_asset_id is not None and not isinstance(
            command.legacy_upload_asset_id, UUID
        ):
            raise ContextImageIngestError(
                "validation_failed",
                "legacy_upload_asset_id must be a UUID",
                reason_code="legacy_upload_asset_id_invalid",
            )

        canonical_media_type = (
            (command.declared_media_type or "").split(";", 1)[0].strip().lower()
        )
        if context_image_format_for_media_type(canonical_media_type) is None:
            raise ContextImageIngestError(
                "media_type_unsupported",
                "Context media supports PNG, JPEG, GIF, and WebP images only.",
                reason_code="media_type_unsupported",
            )
        if not isinstance(command.content, bytes) or not command.content:
            raise ContextImageIngestError(
                "media_content_mismatch",
                "Uploaded file is empty or is not a byte string.",
                reason_code="content_empty",
            )
        byte_size = len(command.content)
        if byte_size > CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes:
            raise ContextImageIngestError(
                "media_too_large",
                (
                    "Image upload exceeds the "
                    f"{CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes} byte limit."
                ),
                reason_code="byte_limit_exceeded",
            )

        try:
            validated_image = validate_context_image(
                command.content,
                declared_media_type=canonical_media_type,
            )
        except ContextImageValidationError as exc:
            raise ContextImageIngestError(
                "media_content_mismatch",
                (
                    "Uploaded file is truncated, unsafe, or does not match its "
                    "declared type."
                ),
                reason_code=exc.reason_code,
            ) from exc

        return PreparedContextImageIngest(
            command=command,
            canonical_media_type=validated_image.canonical_media_type,
            validated_image=validated_image,
            byte_size=byte_size,
            sha256=sha256(command.content).hexdigest(),
        )

    def ingest_prepared(
        self,
        *,
        session_factory: sessionmaker[Session],
        command: ContextImageIngestCommand,
        prepared: PreparedContextImageIngest,
    ) -> ContextImageIngestResult:
        if not isinstance(prepared, PreparedContextImageIngest):
            raise ContextImageIngestError(
                "media_state_changed",
                "Prepared Context image validation is invalid.",
                reason_code="prepared_ingest_invalid",
            )
        if command != prepared.command:
            content_changed = command.content != prepared.command.content
            raise ContextImageIngestError(
                "media_state_changed",
                "Context image command changed after validation.",
                reason_code=(
                    "prepared_content_drift"
                    if content_changed
                    else "prepared_command_drift"
                ),
            )
        digest = sha256(command.content).hexdigest()
        if prepared.byte_size != len(command.content) or prepared.sha256 != digest:
            raise ContextImageIngestError(
                "media_state_changed",
                "Context image content changed after validation.",
                reason_code="prepared_content_drift",
            )
        if (
            prepared.canonical_media_type
            != prepared.validated_image.canonical_media_type
        ):
            raise ContextImageIngestError(
                "media_state_changed",
                "Prepared Context image validation facts are inconsistent.",
                reason_code="prepared_ingest_invalid",
            )
        try:
            registered = self._context_media_upload.register(
                session_factory=session_factory,
                workspace_id=command.workspace_id,
                asset_id=command.marker_uuid,
                original_filename=command.filename,
                canonical_media_type=prepared.canonical_media_type,
                content=command.content,
                sha256=digest,
                page_id=command.page_id,
                editing_version_id=command.editing_version_id,
                actor_kind=command.actor_kind,
                actor_id=command.actor_id,
                max_bytes=CONTEXT_MEDIA_UPLOAD_POLICY.max_upload_bytes,
                policy_revision=command.policy_revision,
                validated_image=prepared.validated_image,
                legacy_upload_asset_id=command.legacy_upload_asset_id,
            )
        except ContextLogicalQuotaExceededError as exc:
            raise _downstream_error(
                "media_quota_exceeded",
                "The Workspace media quota has been reached.",
            ) from exc
        except ContextMediaError as exc:
            error_code = (
                exc.error_code
                if exc.error_code in _CONTEXT_MEDIA_UPLOAD_ERROR_CODES
                else "media_state_changed"
            )
            raise _downstream_error(
                error_code,
                _safe_downstream_message(error_code),
            ) from exc
        except MediaStagingError as exc:
            error_code = exc.error_code
            if error_code not in CONTEXT_IMAGE_INGEST_ERROR_CODES:
                error_code = "media_install_unavailable"
            raise _downstream_error(
                error_code,
                _safe_downstream_message(error_code),
            ) from exc
        except LibraryBlobStorageError as exc:
            raise _downstream_error(
                "media_install_unavailable",
                "Media storage is temporarily unavailable.",
            ) from exc
        except IdempotencyConflictError as exc:
            raise _downstream_error(
                "media_state_changed",
                "The upload marker conflicts with existing media state.",
            ) from exc
        except ValueError as exc:
            error_code = (
                str(exc)
                if str(exc) in {"media_policy_changed", "media_quota_exceeded"}
                else "media_state_changed"
            )
            raise _downstream_error(
                error_code,
                _safe_downstream_message(error_code),
            ) from exc
        return ContextImageIngestResult(
            asset_id=registered.asset.asset_id,
            logical_ref=registered.logical_ref,
            content_url=registered.content_url,
            canonical_media_type=registered.asset.canonical_media_type,
            byte_size=registered.asset.logical_bytes,
            sha256=digest,
            reused=registered.reused,
        )


def _downstream_error(error_code: str, message: str) -> ContextImageIngestError:
    return ContextImageIngestError(
        error_code,
        message,
        reason_code=error_code,
    )


def _safe_downstream_message(error_code: str) -> str:
    return {
        "not_found": "The media target was not found.",
        "media_policy_changed": (
            "Media policy changed; refresh capabilities and retry the upload."
        ),
        "media_owner_mismatch": "The media target belongs to another actor.",
        "media_editing_not_open": "The editing session is no longer open.",
        "media_unavailable": "The media target is temporarily unavailable.",
        "media_quota_exceeded": "The Workspace media quota has been reached.",
        "media_staging_limit_exceeded": ("Media staging is temporarily rate limited."),
        "media_staging_bytes_exceeded": (
            "Media staging exceeds the active byte limit."
        ),
        "media_install_in_progress": "An identical media install is still active.",
        "media_install_unavailable": "Media storage is temporarily unavailable.",
        "media_state_changed": (
            "The upload marker conflicts with existing media state."
        ),
    }[error_code]


__all__ = [
    "CONTEXT_IMAGE_INGEST_ERROR_CODES",
    "ContextImageIngestCommand",
    "ContextImageIngestError",
    "ContextImageIngestResult",
    "ContextImageIngestService",
    "PreparedContextImageIngest",
]
