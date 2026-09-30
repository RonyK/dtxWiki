"""Context image and upload resource policy."""

from __future__ import annotations

from dataclasses import dataclass

from .image_formats import CONTEXT_IMAGE_FORMATS, ContextImageMediaType

CONTEXT_MEDIA_POLICY_REVISION = "context-image-v2"
_LEGACY_INSPECTION_REVISIONS = {"context-mvp-v1": "context-media-v1"}

MAX_CONTEXT_IMAGE_DIMENSION = 16_384
MAX_CONTEXT_IMAGE_PIXELS = 40_000_000
MAX_CONTEXT_IMAGE_FRAMES = 128
MAX_CONTEXT_IMAGE_TOTAL_PIXELS = 40_000_000
MAX_CONTEXT_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ContextMediaUploadPolicy:
    """Wire capability, decoder, and raw-request resource policy."""

    policy_revision: str
    uploadable_media_types: tuple[ContextImageMediaType, ...]
    max_upload_bytes: int
    max_files_per_batch: int
    max_batch_bytes: int
    max_parallel_uploads: int
    max_request_body_bytes: int
    max_multipart_parts: int
    max_headers_per_part: int
    max_header_line_bytes: int
    spool_memory_bytes: int


CONTEXT_MEDIA_UPLOAD_POLICY = ContextMediaUploadPolicy(
    policy_revision=CONTEXT_MEDIA_POLICY_REVISION,
    uploadable_media_types=tuple(CONTEXT_IMAGE_FORMATS),
    max_upload_bytes=MAX_CONTEXT_IMAGE_UPLOAD_BYTES,
    max_files_per_batch=10,
    max_batch_bytes=50 * 1024 * 1024,
    max_parallel_uploads=2,
    # The REST route accepts exactly one file plus its two required form
    # fields (marker_uuid, editing_version_id). Encoded multipart overhead is
    # bounded separately from the advertised decoded-file ceiling.
    max_request_body_bytes=MAX_CONTEXT_IMAGE_UPLOAD_BYTES + (256 * 1024),
    max_multipart_parts=3,
    max_headers_per_part=8,
    max_header_line_bytes=8 * 1024,
    spool_memory_bytes=1024 * 1024,
)


def context_image_inspection_schema_revision(policy_revision: str) -> str:
    """Derive installer provenance while preserving historical fingerprints."""

    return _LEGACY_INSPECTION_REVISIONS.get(policy_revision, policy_revision)


__all__ = [
    "CONTEXT_MEDIA_POLICY_REVISION",
    "CONTEXT_MEDIA_UPLOAD_POLICY",
    "MAX_CONTEXT_IMAGE_DIMENSION",
    "MAX_CONTEXT_IMAGE_FRAMES",
    "MAX_CONTEXT_IMAGE_PIXELS",
    "MAX_CONTEXT_IMAGE_TOTAL_PIXELS",
    "MAX_CONTEXT_IMAGE_UPLOAD_BYTES",
    "ContextMediaUploadPolicy",
    "context_image_inspection_schema_revision",
]
