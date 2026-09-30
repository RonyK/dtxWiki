"""Streaming Library upload admission before database registration.

This service owns the untrusted-byte boundary only: stage, hash, detect, apply
the explicit format/provider policy, and atomically publish an immutable blob.
It deliberately does not open a database transaction or import a converter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from backend.media.application import InstallValidatedBlob
from backend.media.application.install_validated import MediaSourceIntegrityError
from backend.media.domain import AvailableBlobRecord
from backend.media.ports.contracts import IdempotencyConflictError
from backend.services.library_blob_storage import (
    FilesystemBlobHandle,
    FilesystemLibraryBlobStore,
    LibraryBlobStorageError,
    LibraryBlobTooLargeError,
    StagedLibraryBlob,
    StoredLibraryBlob,
)
from backend.services.library_formats import (
    DetectionResult,
    FormatDescriptor,
    FormatRegistry,
    LibraryFormatError,
    normalize_library_filename,
)


@dataclass(frozen=True, slots=True)
class AcceptedLibraryUpload:
    blob: StoredLibraryBlob
    detection: DetectionResult
    descriptor: FormatDescriptor
    processing_disposition: Literal["queued", "stored_unconverted"]


class LibraryFileEmptyError(RuntimeError):
    """A Library upload contained no source bytes."""

    error_code = "library_file_empty"
    safe_message = "Empty files cannot be uploaded to the Library."


class LibraryUploadIdempotencyConflictError(RuntimeError):
    """One client upload operation identity was replayed with different facts."""

    error_code = "library_upload_idempotency_conflict"
    safe_message = "The upload operation was already used for different content."


class LibraryUploadAdmissionService:
    def __init__(
        self,
        *,
        store_for: Callable[[UUID], FilesystemLibraryBlobStore],
        format_registry: FormatRegistry,
        media_installer: InstallValidatedBlob,
    ) -> None:
        # D-5: resolved per call (admit() already carries workspace_id), not
        # cached here -- see LibraryContentService.__init__ for the reasoning.
        self._store_for = store_for
        self._format_registry = format_registry
        self._media_installer = media_installer

    async def admit(
        self,
        upload: Any,
        *,
        workspace_id: UUID,
        filename: str,
        declared_media_type: str | None,
        max_bytes: int,
        enabled_formats: frozenset[str],
        converter_formats: frozenset[str],
        allow_store_without_converter: bool,
        format_max_bytes: Mapping[str, int] | None = None,
        converter_max_bytes: Mapping[str, int | None] | None = None,
        custom_descriptors: tuple[FormatDescriptor, ...] = (),
        builtin_descriptor_overrides: tuple[FormatDescriptor, ...] = (),
        actor_kind: str = "human",
        actor_id: str = "library-upload",
        upload_operation_id: UUID | None = None,
        upload_parent_node_id: UUID | None = None,
        existing_item_id: UUID | None = None,
        before_install: Callable[[DetectionResult, str, int], None] | None = None,
    ) -> AcceptedLibraryUpload:
        clean_name = normalize_library_filename(filename)
        operation_id = upload_operation_id or uuid4()
        # P4.2: the idempotency key's scope now includes which command this
        # operation id names -- "new item" vs "a version of this exact
        # item" -- so replaying the same operation id against a different
        # target conflicts instead of silently reusing the first target.
        command_scope = (
            "new" if existing_item_id is None else f"item:{existing_item_id}"
        )
        staged = await self._store_for(workspace_id).stage_upload(
            upload,
            workspace_id=workspace_id,
            max_bytes=max_bytes,
        )
        try:
            if staged.byte_size == 0:
                raise LibraryFileEmptyError
            detection = await asyncio.to_thread(
                self._format_registry.detect,
                staged.path,
                filename=clean_name,
                declared_media_type=declared_media_type,
                custom_descriptors=custom_descriptors,
                builtin_descriptor_overrides=builtin_descriptor_overrides,
            )
            descriptor = self._format_registry.descriptor(
                detection.format_id,
                custom_descriptors=custom_descriptors,
                builtin_descriptor_overrides=builtin_descriptor_overrides,
            )
            if format_max_bytes is not None:
                detected_limit = format_max_bytes.get(descriptor.format_id)
                if (
                    isinstance(detected_limit, bool)
                    or not isinstance(detected_limit, int)
                    or detected_limit <= 0
                ):
                    raise ValueError("detected format limit must be positive")
                if staged.byte_size > detected_limit:
                    raise LibraryBlobTooLargeError(limit_bytes=detected_limit)
            if (
                descriptor.admission == "deny"
                or descriptor.format_id not in enabled_formats
            ):
                raise LibraryFormatError(
                    "library_format_disabled",
                    "This Library file format is disabled by policy.",
                )
            has_converter = (
                descriptor.admission == "convert"
                and descriptor.format_id in converter_formats
            )
            converter_limit = (
                converter_max_bytes.get(descriptor.format_id)
                if converter_max_bytes is not None
                else None
            )
            if converter_limit is not None and (
                isinstance(converter_limit, bool)
                or not isinstance(converter_limit, int)
                or converter_limit <= 0
            ):
                raise ValueError("converter format limit must be positive")
            converter_input_too_large = (
                has_converter
                and converter_limit is not None
                and staged.byte_size > converter_limit
            )
            if converter_input_too_large:
                has_converter = False
            if not has_converter and not allow_store_without_converter:
                raise LibraryFormatError(
                    (
                        "library_converter_input_too_large"
                        if converter_input_too_large
                        else "library_converter_unavailable"
                    ),
                    (
                        "This file can be stored but exceeds the converter's "
                        "safe input limit."
                        if converter_input_too_large
                        else "No approved converter is available for this file format."
                    ),
                )
            if before_install is not None:
                before_install(detection, staged.sha256, staged.byte_size)
            available = await asyncio.to_thread(
                self._install_staged,
                staged=staged,
                workspace_id=workspace_id,
                actor_id=_stable_actor_uuid(actor_kind, actor_id),
                session_key=f"library:{workspace_id}",
                idempotency_key=f"library-upload:{operation_id}:{command_scope}",
                max_bytes=max_bytes,
                detection=detection,
                upload_parent_node_id=upload_parent_node_id,
            )
            if (
                available.sha256.value != staged.sha256
                or available.byte_size != staged.byte_size
                or available.reference.workspace_id != workspace_id
            ):
                raise RuntimeError(
                    "Shared Media installer changed immutable Library upload facts"
                )
            blob = StoredLibraryBlob(
                workspace_id=workspace_id,
                sha256=available.sha256.value,
                byte_size=available.byte_size,
                storage_key=available.storage_key,
                created=True,
            )
            return AcceptedLibraryUpload(
                blob=blob,
                detection=detection,
                descriptor=descriptor,
                processing_disposition=(
                    "queued" if has_converter else "stored_unconverted"
                ),
            )
        finally:
            await asyncio.to_thread(staged.cleanup)

    def _install_staged(
        self,
        *,
        staged: StagedLibraryBlob,
        workspace_id: UUID,
        actor_id: UUID,
        session_key: str,
        idempotency_key: str,
        max_bytes: int,
        detection: DetectionResult,
        upload_parent_node_id: UUID | None,
    ) -> AvailableBlobRecord:
        handle = FilesystemBlobHandle(
            path=staged.path,
            byte_size=staged.byte_size,
            expected_sha256=staged.sha256,
        )
        with handle.open_binary() as stream:
            try:
                return self._media_installer.execute_stream(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    session_key=session_key,
                    idempotency_key=idempotency_key,
                    stream=stream,
                    expected_sha256=staged.sha256,
                    expected_byte_size=staged.byte_size,
                    max_bytes=max_bytes,
                    canonical_media_type=detection.detected_media_type,
                    format_id=detection.format_id,
                    detector_id=detection.detector,
                    detector_version="1",
                    registry_version="library-v1",
                    validation_schema_version="library-v1",
                    metadata={
                        "original_filename": detection.original_filename,
                        "extension": detection.extension,
                        "confidence_milli": int(detection.confidence * 1000),
                        "parent_node_id": (
                            str(upload_parent_node_id)
                            if upload_parent_node_id is not None
                            else ""
                        ),
                    },
                )
            except MediaSourceIntegrityError as exc:
                raise LibraryBlobStorageError(
                    "library_blob_staging_integrity",
                    "Staged Library content failed validation.",
                ) from exc
            except IdempotencyConflictError as exc:
                raise LibraryUploadIdempotencyConflictError from exc


def _stable_actor_uuid(actor_kind: str, actor_id: str) -> UUID:
    try:
        return UUID(actor_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"media-actor:{actor_kind}:{actor_id}")


__all__ = [
    "AcceptedLibraryUpload",
    "LibraryFileEmptyError",
    "LibraryUploadAdmissionService",
    "LibraryUploadIdempotencyConflictError",
]
